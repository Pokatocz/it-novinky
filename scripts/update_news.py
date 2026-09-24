#!/usr/bin/env python3
"""
Denní IT novinky — stáhne RSS z ověřených českých zdrojů, vybere 3 novinky
a ke každé nechá AI napsat samostatný mluvený výklad na cca 3 minuty.
Výklady píše VŽDY AI (Google Gemini, záložně GitHub Models) — když žádná AI
není dostupná, běh skončí chybou a na stránce zůstane poslední úspěšný den.
Výsledkem je stránka docs/index.html se třemi výklady — každý čte jiný člověk.

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
CILOVA_SLOVA = (380, 440)            # cíl délky jednoho výkladu (~3 minuty při 130 slovech/min)
GEMINI_MODELY = ("gemini-3.6-flash", "gemini-flash-latest")  # Google Gemini (klíč GEMINI_API_KEY)
# Kdyby Google modely přejmenoval, skript se sám zeptá API na aktuální seznam (viz gemini_dostupne_modely).
GH_MODELY = ("openai/gpt-4.1", "openai/gpt-4o-mini")      # záloha: GitHub Models (končí)
TZ = ZoneInfo("Europe/Prague")

SYSTEM_PROMPT = (
    "Jsi redaktor školního zpravodajství. Z podkladu napiš souvislý mluvený výklad "
    "JEDNÉ novinky ze světa IT v češtině, který přečte nahlas před třídou jeden člověk. "
    "Délka musí vydat na zhruba 3 minuty mluvení: napiš 380 až 440 slov. "
    "Rozděl text do 3 až 4 odstavců oddělených prázdným řádkem — žádné odrážky, "
    "žádné nadpisy, žádné uvozovky kolem textu. Postupuj takto: nejdřív jednou dvěma "
    "větami uveď, o čem novinka je; pak podrobně vylož, co přesně se stalo, s konkrétními "
    "čísly, jmény a detaily z podkladu; potom vysvětli souvislosti a všechny cizí nebo "
    "odborné pojmy tak, aby je pochopili i spolužáci bez znalosti IT; na závěr řekni, "
    "proč je novinka důležitá nebo co může následovat. "
    "Vycházej POUZE z informací v podkladu, nic si nedomýšlej ani nepřidávej — pokud je "
    "podklad krátký, věnuj více prostoru vysvětlení pojmů a souvislostí, které v něm jsou. "
    "Ignoruj části podkladu, které vypadají jako navigace webu, reklama, komentáře, "
    "související články nebo výzvy k odběru. Nezmiňuj název zdroje ani autora — ty budou "
    "uvedeny zvlášť."
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


def strip_blocks(raw: str) -> str:
    """Odstraní bloky, které do textu článku nepatří."""
    return re.sub(
        r"(?is)<(script|style|nav|header|footer|aside|form|iframe|figure)[^>]*>.*?</\1>",
        " ", raw)


def clean_text(raw: str) -> str:
    """Odstraní HTML značky a přebytečné mezery."""
    raw = strip_blocks(raw)
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


def fetch_article_paragraphs(url: str):
    """Best-effort stažení odstavců článku (<p>…</p>) jako podklad pro výklad."""
    try:
        raw = http_get(url).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  ! Článek se nepodařilo stáhnout ({e}), použije se jen perex.")
        return []
    raw = strip_blocks(raw)
    paras = []
    seen = set()
    for m in re.findall(r"(?is)<p[^>]*>(.*?)</p>", raw):
        p = clean_text(m)
        # vložené widgety na začátku odstavce (Lupa/Root: "Přidat mezi oblíbené…")
        p = re.sub(r"^Přidat mezi oblíbené zdroje na Googlu\s*", "", p)
        if len(p) < 80:            # krátké kousky = popisky, tlačítka, podpisy
            continue
        # popisky fotek a lišty sdílení, které se do mluveného textu nehodí
        if re.match(r"(?i)^\s*(foto|zdroj obrázku|ilustrační foto)\s*[::]", p) \
                or "Sdílet na Facebooku" in p:
            continue
        if p in seen:
            continue
        seen.add(p)
        paras.append(p)
    return paras


def word_count(text: str) -> int:
    return len(text.split())


def trim_to_words(text: str, max_words: int) -> str:
    """Zkrátí text na max_words, pokud možno na hranici věty."""
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words])
    m = re.search(r"^(.*[.!?])\s", cut + " ")
    return (m.group(1) if m and word_count(m.group(1)) >= max_words - 60 else cut + "…")


def call_github_models(model: str, material: str):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("MODELS_TOKEN")
    if not token:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": material},
        ],
        "temperature": 0.4,
        "max_tokens": 1100,
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
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def gemini_dostupne_modely():
    """Zeptá se Gemini API, které modely umí generateContent — pojistka proti přejmenování."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! Nepodařilo se načíst seznam modelů: {e}")
        return []
    jmena = []
    for m in data.get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        jm = (m.get("name") or "").split("/")[-1]
        if not jm or "flash" not in jm:
            continue
        if any(z in jm for z in ("embedding", "vision", "tts", "image", "live", "audio")):
            continue
        jmena.append(jm)

    def poradi(jm):
        # nejdřív běžný flash, pak lite; preview/exp až nakonec; novější verze dřív
        cislo = re.search(r"(\d+(?:\.\d+)?)", jm)
        return (("preview" in jm or "exp" in jm), ("lite" in jm), -float(cislo.group(1)) if cislo else 0)

    jmena.sort(key=poradi)
    if jmena:
        print(f"  i Dostupné modely podle API: {', '.join(jmena[:5])}")
    return jmena[:3]


def call_gemini(model: str, material: str):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None

    def posli(s_thinking: bool):
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": material}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4000},
        }
        if s_thinking:
            # vypnout "přemýšlení", ať se nespotřebuje limit výstupu
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = posli(True)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        # model nemusí umět thinkingConfig — zkus to bez něj
        data = posli(False)

    kandidati = data.get("candidates") or []
    if not kandidati:
        raise RuntimeError(f"odpověď bez kandidátů ({str(data)[:200]})")
    obsah = kandidati[0].get("content") or {}
    parts = obsah.get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError(f"prázdná odpověď (finishReason={kandidati[0].get('finishReason')})")
    return text


def normalize_paragraphs(text: str) -> str:
    """Uklidí bílé znaky, ale zachová odstavce (oddělené prázdným řádkem)."""
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


_GEMINI_EXTRA = None


def gemini_extra_modely():
    """Seznam modelů z API — zjistí se nejvýš jednou za běh."""
    global _GEMINI_EXTRA
    if _GEMINI_EXTRA is None:
        _GEMINI_EXTRA = gemini_dostupne_modely()
    return _GEMINI_EXTRA


def summarize(title: str, source: str, perex: str, paragraphs):
    """Vrátí AI výklad, nebo None, když žádná AI není dostupná."""
    body = "\n\n".join(paragraphs)[:6000]
    material = (f"Titulek: {title}\nZdroj: {source}\n\nPodklad z článku:\nPerex: {perex}"
                + (f"\n\nText článku:\n{body}" if body else ""))
    modely = list(GEMINI_MODELY)
    for m in gemini_extra_modely():
        if m not in modely:
            modely.append(m)
    pokusy = ([("Gemini", m, call_gemini) for m in modely]
              + [("GitHub Models", m, call_github_models) for m in GH_MODELY])
    for sluzba, model, fn in pokusy:
        try:
            out = fn(model, material)
            if out is None:
                continue  # chybí klíč/token pro tuhle službu
            out = normalize_paragraphs(out.strip().strip('"'))
            if word_count(out) >= 300:  # pojistka proti krátké/useknuté odpovědi
                print(f"  ✓ výklad napsal {sluzba} ({model})")
                return out
            print(f"  ! {sluzba} ({model}): výklad moc krátký ({word_count(out)} slov), zkouším dál.")
        except urllib.error.HTTPError as e:
            print(f"  ! {sluzba} ({model}): HTTP {e.code} — {e.read()[:200]!r}")
        except Exception as e:
            print(f"  ! {sluzba} ({model}): {e}")
    return None


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


def minutes_txt(words: int) -> str:
    minutes = max(0.5, round(words / 130 * 2) / 2)  # ~130 slov za minutu, na půlminuty
    return f"{minutes:g}".replace(".", ",")


def render(data) -> None:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    d = dt.date.fromisoformat(data["date"])

    cards, spokens = [], []
    for n, it in enumerate(data["items"], start=1):
        spokens.append(it["summary"])
        words = word_count(it["summary"])
        pub = ""
        if it.get("published"):
            p = dt.datetime.fromisoformat(it["published"]).astimezone(TZ)
            pub = f" · vydáno {p.day}. {p.month}. {p.year}"
        vyklad = "\n".join(f"            <p>{html.escape(p)}</p>"
                           for p in it["summary"].split("\n\n"))
        cards.append(f"""
      <article class="card" id="novinka-{n}">
        <div class="num" aria-hidden="true">{n}</div>
        <div class="card-body">
          <h2>{html.escape(it["title"])}</h2>
          <p class="meta-line"><span class="chip">&#127908; cca {minutes_txt(words)} min ({words} slov)</span></p>
          <div class="vyklad">
{vyklad}
          </div>
          <p class="src">Zdroj: <a href="{html.escape(it["url"], quote=True)}" target="_blank"
             rel="noopener">{html.escape(it["source"])}</a>{pub}</p>
          <div class="btns">
            <button class="primary copy-btn" data-i="{n - 1}" type="button">Zkopírovat výklad {n}</button>
          </div>
        </div>
      </article>""")

    out = (tpl
           .replace("%%DATE_HUMAN%%", date_human(d))
           .replace("%%DATE_ISO%%", data["date"])
           .replace("%%CARDS%%", "\n".join(cards))
           .replace("%%SPOKENS_JSON%%", json.dumps(spokens, ensure_ascii=False))
           .replace("%%GENERATED%%", dt.datetime.now(TZ).strftime("%d. %m. %Y %H:%M"))
           .replace("%%SOURCES%%", ", ".join(f["name"] for f in FEEDS)))
    INDEX_HTML.parent.mkdir(parents=True, exist_ok=True)
    INDEX_HTML.write_text(out, encoding="utf-8")
    delky = ", ".join(f"{word_count(s)} slov" for s in spokens)
    print(f"✓ Stránka vygenerována: {INDEX_HTML} (výklady: {delky})")


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
        paragraphs = fetch_article_paragraphs(it["url"])
        summary = summarize(it["title"], it["source"], it["perex"], paragraphs)
        if summary is None:
            print("!! Žádná AI teď není dostupná — stránka zůstává na posledním úspěšném dni.")
            sys.exit(1)
        print(f"  → výklad: {word_count(summary)} slov")
        out_items.append({
            "source": it["source"],
            "source_home": it["source_home"],
            "title": it["title"],
            "url": it["url"],
            "published": it["published"].isoformat() if it["published"] else None,
            "summary": summary,
            "via": "ai",
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
