# 📰 IT novinky dne

Stránka, která **každý den v 5:00 ráno** sama stáhne aktuální články z ověřených českých
IT zdrojů (Root.cz, Lupa.cz, CzechCrunch), vybere **3 novinky** a ke každé nechá umělou
inteligencí napsat **samostatný mluvený výklad na cca 3 minuty** — každý výklad čte jiný
člověk. Vše se zveřejní na GitHub Pages a u každé novinky je vždy **odkaz na původní
článek** — zdroj pro učitele.

## Jak to funguje

```
GitHub Actions (každý den v 5:00 pražského času)
   └─ scripts/update_news.py
        1. stáhne RSS z Root.cz, Lupa.cz a CzechCrunch
        2. vybere 3 nejnovější IT novinky (z každého zdroje jednu)
        3. stáhne text článků a nechá GitHub Models (AI) napsat 3 výklady po ~3 minutách
        4. uloží docs/data.json a vygeneruje stránku docs/index.html
        5. workflow změny commitne → GitHub Pages stránku obnoví
```

- AI výklady běží přes **GitHub Models** — zdarma, bez API klíče, stačí vestavěný
  `GITHUB_TOKEN` (workflow má `permissions: models: read`).
- Když AI zrovna nejede, výklad se poskládá z perexu a začátku článku ze zdroje
  (u novinky se pak ukáže štítek „text ze zdroje (bez AI)“).
- Vše je čistý Python bez závislostí + jeden HTML soubor. Nic se neinstaluje.

## Nasazení na GitHub (jednou, cca 5 minut)

1. **Vytvoř repozitář** na github.com → „New repository“ → název např. `it-novinky`,
   viditelnost **Public** (kvůli GitHub Pages zdarma) → „Create repository“.

2. **Nahraj soubory** — buď příkazy v terminálu ve složce projektu:

   ```bash
   git init -b main
   git add .
   git commit -m "IT novinky - prvni verze"
   git remote add origin https://github.com/TVOJE_JMENO/it-novinky.git
   git push -u origin main
   ```

   …nebo přes web: „uploading an existing file“ a přetáhni tam obsah složky.
   **Pozor:** pokud web odmítne složku `.github`, vytvoř soubor ručně —
   „Add file → Create new file“, jako název napiš
   `.github/workflows/novinky.yml` a vlož do něj obsah stejného souboru z projektu.

3. **Povol workflow zápis do repozitáře:**
   Settings → Actions → General → dole „Workflow permissions“ →
   zaškrtni **Read and write permissions** → Save.

4. **Zapni GitHub Pages:**
   Settings → Pages → Source: **Deploy from a branch** →
   Branch: `main`, složka **/docs** → Save.
   Stránka pak poběží na `https://TVOJE_JMENO.github.io/it-novinky/`
   (první nasazení trvá pár minut).

5. **Vyzkoušej:** záložka **Actions** → „Denní IT novinky“ → **Run workflow**.
   Po doběhnutí se na stránce objeví čerstvé novinky. Od té chvíle se to děje
   samo každý den v 5:00.

## Dobré vědět

- Naplánované běhy GitHub spouští s možným zpožděním v řádu minut — skript proto
  hlídá pražský čas sám a novinky vygeneruje jen jednou denně.
- Když se v repozitáři ~60 dní nic neděje, GitHub může naplánované workflow
  pozastavit a pošle o tom e-mail — stačí je jedním kliknutím znovu povolit.
- Zdroje a jejich filtry se dají upravit nahoře v `scripts/update_news.py`
  (seznam `FEEDS`), vzhled stránky v `scripts/template.html`.
- Ruční spuštění na počítači: `python3 scripts/update_news.py`
  (bez tokenu se místo AI použijí perexy; s tokenem: nastav proměnnou
  `MODELS_TOKEN` na GitHub fine-grained token s oprávněním *models: read*).

## Struktura projektu

```
├── .github/workflows/novinky.yml   # denní automatizace (GitHub Actions)
├── scripts/update_news.py          # stažení, výběr, AI shrnutí, render
├── scripts/template.html           # šablona stránky
└── docs/                           # publikovaná stránka (GitHub Pages)
    ├── index.html                  # vygenerovaná stránka
    └── data.json                   # data aktuálního dne
```
