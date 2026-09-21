#!/usr/bin/env python3
"""
Denní IT novinky — stáhne RSS z ověřených českých zdrojů, vybere 3 novinky,
shrne je do mluveného textu (GitHub Models) a vygeneruje stránku docs/index.html.

Spouští se automaticky v GitHub Actions (viz .github/workflows/novinky.yml),
ale jde spustit i ručně:

    python3 scripts/update_news.py               # plná aktualizace
    python3 scripts/update_news.py --render-only # jen znovu vykreslí stránku z docs/data.json

Žádné závislosti — stačí Python 3.9+ (používá jen standardní knihovnu).
"""

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# NASTAVENÍ — tady se dá všechno snadno změnit
# ---------------------------------------------------------------------------

FEEDS = [
    {
        "name": "Root.cz",
        "home": "https://www.root.cz/",
        "rss": "https://www.root.cz/rss/clanky/",
        "skip_categories": [],
        "require_categories": [],  # prázdné = bere se všechno
    },
    {
        "name": "Lupa.cz",
        "home": "https://www.lupa.cz/",
        "rss": "https://www.lupa.cz/rss/clanky/",
        "skip_categories": [],
        "require_categories": [],
    },
    {
        "name": "CzechCrunch",
        "home": "https://cc.cz/",
        "rss": "https://cc.cz/feed/",
        # "Native" = placené/reklamní články, "Weekly"/"Newsletter" = souhrny
        "skip_categories": ["Native", "Newsletter", "Weekly"],
        # CzechCrunch píše i o pivu a módě — chceme jen IT témata:
        "require_categories": [
            "Tech", "Umělá inteligence", "Startupy", "Věda a vesmír",
            "Hry", "Kyberbezpečnost", "Software", "Hardware", "Aplikace",
        ],
    },
]

POCET_NOVINEK = 3
MODEL = "openai/gpt-4.1"             # model na GitHub Models
MODEL_ZALOHA = "openai/gpt-4o-mini"  # zkusí se, kdyby první model nebyl dostupný
TZ = ZoneInfo("Europe/Prague")

SYSTEM_PROMPT = (
    "Jsi redaktor školního zpravodajství. Z podkladu napiš souvislé mluvené shrnutí "
    "jedné novinky ze světa IT v češtině: 5 až 7 vět, přibližně 90 až 120 slov. "
    "Piš tak, aby se text dal přirozeně přečíst nahlas před třídou — žádné odrážky, "
    "žádné nadpisy, žádné uvozovky. Cizí pojmy krátce vysvětli. "
    "Vycházej POUZE z informací v podkladu, nic si nedomýšlej ani nepřidávej. "
    "Ignoruj části podkladu, které vypadají jako navigace webu, reklama, komentáře "
    "nebo výzvy k odběru. Nezmiňuj název zdroje ani autora — ty budou uvedeny zvlášť."
)

ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = ROOT / "docs" / "data.json"
TEMPLATE = ROOT / "scripts" / "template.html"
INDEX_HTML = ROOT / "docs" / "index.html"

DNY = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
MESICE = ["ledna", "února", "března", "dubna", "května", "června",
          "července", "srpna", "září", "října", "listopadu", "prosince"]

# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------


def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; it-novinky/1.0; skolni projekt, GitHub Actions)",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean_text(raw: str) -> str:
    """Odstraní HTML značky a přebytečné mezery."""
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside|form|iframe)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def parse_feed(xml_bytes: bytes):
    """Zpracuje RSS 2.0 a vrátí seznam položek (nejnovější první)."""
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = clean_text(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        desc = clean_text(it.findtext("description") or "")
        # CzechCrunch přidává na konec popisku patičku "Článek … se nejdříve objevil na …"
        desc = re.sub(r"Článek .{0,300}? se nejdříve objevil na .*$", "", desc).strip()
        cats = [(c.text or "").strip() for c in it.findall("category")]
        pub_raw = (it.findtext("pubDate") or "").strip()
        try:
            published = email.utils.parsedate_to_datetime(pub_raw)
            if published.tzinfo is None:
                published = published.replace(tzinfo=dt.timezone.utc)
        except Exception:
            published = None
        if title and link:
            items.append({
                "title": title,
                "url": link.split("?utm_")[0],  # pryč s utm parametry
                "perex": desc,
                "categories": cats,
                "published": published,
            })
    items.sort(key=lambda x: x["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
               reverse=True)
    return items


def item_allowed(item, feed) -> bool:
    cats = set(item["categories"])
    if cats & set(feed["skip_categories"]):
        return False
    req = feed["require_categories"]
    if req and not (cats & set(req)):
        return False
    return True


def title_words(title: str):
    return {w for w in re.findall(r"\w+", title.lower()) if len(w) > 3}


def similar(a: str, b: str) -> bool:
    """Hrubá kontrola, jestli dva titulky nepopisují stejnou zprávu."""
    wa, wb = title_words(a), title_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.5


def fetch_article_text(url: str) -> str:
    """Best-effort stažení textu článku jako podklad pro shrnutí."""
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
        return clean_text(raw)[:4000]
    except Exception as e:
        print(f"  ! Článek se nepodařilo stáhnout ({e}), použije se jen perex.")
        return ""


def call_github_models(model: str, title: str, source: str, material: str):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MODELS_TOKEN")
    if not token:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Titulek: {title}\nZdroj: {source}\n\nPodklad z článku:\n{material}"},
        ],
        "temperature": 0.4,
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        "https://models.github.ai/inference/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def summarize(title: str, source: str, perex: str, article_text: str):
    """Vrátí (shrnutí, způsob). Když AI selže, použije se perex ze zdroje."""
    material = f"Perex: {perex}\n\nText článku: {article_text}" if article_text else f"Perex: {perex}"
    for model in (MODEL, MODEL_ZALOHA):
        try:
            out = call_github_models(model, title, source, material)
            if out:
                out = re.sub(r"\s+", " ", out).strip().strip('"')
                if len(out.split()) >= 30:  # pojistka proti prázdné/useknuté odpovědi
                    return out, "ai"
        except urllib.error.HTTPError as e:
            print(f"  ! GitHub Models ({model}): HTTP {e.code} — {e.read()[:200]!r}")
        except Exception as e:
            print(f"  ! GitHub Models ({model}): {e}")
    print("  ! AI shrnutí se nepovedlo, použije se perex ze zdroje.")
    return perex, "perex"


# ---------------------------------------------------------------------------
# Výběr novinek
# ---------------------------------------------------------------------------


def pick_items():
    per_feed = []
    for feed in FEEDS:
        try:
            items = parse_feed(http_get(feed["rss"]))
        except Exception as e:
            print(f"! Feed {feed['name']} se nepodařilo stáhnout: {e}")
            items = []
        allowed = [i for i in items if item_allowed(i, feed)]
        for i in allowed:
            i["source"] = feed["name"]
            i["source_home"] = feed["home"]
        per_feed.append(allowed)
        print(f"- {feed['name']}: {len(allowed)} použitelných položek")

    chosen = []
    # nejdřív po jedné nejnovější z každého zdroje
    for items in per_feed:
        for it in items:
            if not any(similar(it["title"], c["title"]) for c in chosen):
                chosen.append(it)
                break
        if len(chosen) >= POCET_NOVINEK:
            break
    # kdyby některý zdroj vypadl, doplní se dalšími položkami z ostatních
    if len(chosen) < POCET_NOVINEK:
        zbytek = sorted(
            (it for items in per_feed for it in items if it not in chosen),
            key=lambda x: x["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )
        for it in zbytek:
            if not any(similar(it["title"], c["title"]) for c in chosen):
                chosen.append(it)
            if len(chosen) >= POCET_NOVINEK:
                break

    chosen.sort(key=lambda x: x["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                reverse=True)
    return chosen[:POCET_NOVINEK]


# ---------------------------------------------------------------------------
# Render stránky
# ---------------------------------------------------------------------------


def date_human(d: dt.date) -> str:
    return f"{DNY[d.weekday()]} {d.day}. {MESICE[d.month - 1]} {d.year}"


def build_spoken(data) -> str:
    d = dt.date.fromisoformat(data["date"])
    parts = [f"Dobrý den, mám pro vás tři aktuální novinky ze světa IT. Je {date_human(d)}."]
    for n, it in enumerate(data["items"], start=1):
        text = it["summary"]
        if it.get("via") == "perex":
            # bez AI shrnutí přečteme i titulek, aby mluvení dávalo smysl
            text = f"{it['title']}. {text}"
        parts.append(f"Novinka číslo {n}, ze serveru {it['source']}: {text}")
    parts.append("To je z dnešních novinek všechno, děkuji za pozornost. "
                 "Odkazy na všechny zdroje jsou uvedené na stránce.")
    return "\n\n".join(parts)


def render(data) -> None:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    d = dt.date.fromisoformat(data["date"])
    spoken = build_spoken(data)
    words = len(spoken.split())
    minutes = max(1.0, round(words / 130 * 2) / 2)  # ~130 slov za minutu, na půlminuty
    minutes_txt = f"{minutes:g}".replace(".", ",")

    cards = []
    for n, it in enumerate(data["items"], start=1):
        pub = ""
        if it.get("published"):
            p = dt.datetime.fromisoformat(it["published"]).astimezone(TZ)
            pub = f" · vydáno {p.day}. {p.month}. {p.year}"
        badge = "" if it.get("via") != "perex" else \
            ' <span class="chip warn" title="AI shrnutí se dnes nepovedlo, zobrazen je perex ze zdroje">perex zdroje</span>'
        cards.append(f"""
      <article class="card">
        <div class="num" aria-hidden="true">{n}</div>
        <div class="card-body">
          <h2>{html.escape(it["title"])}</h2>
          <p class="summary">{html.escape(it["summary"])}</p>
          <p class="src">Zdroj: <a href="{html.escape(it["url"], quote=True)}" target="_blank"
             rel="noopener">{html.escape(it["source"])}</a>{pub}{badge}</p>
        </div>
      </article>""")

    spoken_html = "\n".join(f"      <p>{html.escape(p)}</p>" for p in spoken.split("\n\n"))

    out = (tpl
           .replace("%%DATE_HUMAN%%", date_human(d))
           .replace("%%DATE_ISO%%", data["date"])
           .replace("%%MINUTES%%", minutes_txt)
           .replace("%%WORDS%%", str(words))
           .replace("%%CARDS%%", "\n".join(cards))
           .replace("%%SPOKEN_HTML%%", spoken_html)
           .replace("%%SPOKEN_JSON%%", json.dumps(spoken, ensure_ascii=False))
           .replace("%%GENERATED%%", dt.datetime.now(TZ).strftime("%d. %m. %Y %H:%M"))
           .replace("%%SOURCES%%", ", ".join(f["name"] for f in FEEDS)))
    INDEX_HTML.parent.mkdir(parents=True, exist_ok=True)
    INDEX_HTML.write_text(out, encoding="utf-8")
    print(f"✓ Stránka vygenerována: {INDEX_HTML} (mluvení cca {minutes_txt} min, {words} slov)")


# ---------------------------------------------------------------------------
# Hlavní běh
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render-only", action="store_true",
                    help="jen znovu vykreslí stránku z docs/data.json (bez stahování)")
    args = ap.parse_args()

    if args.render_only:
        render(json.loads(DATA_JSON.read_text(encoding="utf-8")))
        return

    now = dt.datetime.now(TZ)

    # Pojistky pro naplánované běhy: cron běží v UTC ve 3, 4 a 5 hodin,
    # takže jeden z běhů vždy trefí 5. hodinu pražského času (letní i zimní čas).
    if os.environ.get("SCHEDULED"):
        if now.hour not in (5, 6):
            print(f"Naplánovaný běh v {now:%H:%M} pražského času — mimo okno 5–6 h, končím.")
            return
        try:
            stara = json.loads(DATA_JSON.read_text(encoding="utf-8"))
            if stara.get("date") == now.date().isoformat():
                print("Dnešní novinky už jsou vygenerované, končím.")
                return
        except Exception:
            pass

    print(f"Aktualizace novinek — {now:%d.%m.%Y %H:%M} (Praha)")
    items = pick_items()
    if not items:
        print("!! Nepodařilo se získat žádné novinky, stránka zůstává beze změny.")
        sys.exit(1)

    out_items = []
    for it in items:
        print(f"* {it['source']}: {it['title']}")
        article = fetch_article_text(it["url"])
        summary, via = summarize(it["title"], it["source"], it["perex"], article)
        out_items.append({
            "source": it["source"],
            "source_home": it["source_home"],
            "title": it["title"],
            "url": it["url"],
            "published": it["published"].isoformat() if it["published"] else None,
            "summary": summary,
            "via": via,
        })

    data = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "items": out_items,
    }
    DATA_JSON.parent.mkdir(parents=True, exist_ok=True)
    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ Data uložena: {DATA_JSON}")
    render(data)


if __name__ == "__main__":
    main()
