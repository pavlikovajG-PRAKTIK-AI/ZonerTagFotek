# ZonerTagovatFotek (WildSort)

Local tool for culling large batches of wildlife photos (thousands of RAW
frames) before further work in **Zoner Photo Studio**. It finds the animal in
each frame, measures sharpness *on the animal*, groups shots into bursts and
scenes, proposes the best frame of each burst, and writes star ratings +
keywords into XMP sidecar files that Zoner reads.

Kompletní český návod je v souboru **[NAVOD.txt](NAVOD.txt)**.

## Rychlý start

Na Windows stačí poklepat na **`WildSort.hta`** — okno v HTML zkontroluje
prostředí, nastartuje server a otevře rozhraní. Otevřené minimalizované okno
příkazové řádky *je* běžící server; jeho zavřením práci ukončíš.

Ručně:

```bash
pip install -r requirements.txt
python run.py
```

Prohlížeč se otevře na `http://127.0.0.1:8756`. Klikni na **Načíst složku**,
zadej cestu ke snímkům a nech proběhnout zpracování (detekce zvířat je
nejpomalejší krok — u velkých dávek přes noc).

Rozhraní není soubor, na který se klikne: `web/index.html` samo o sobě nic
neumí, protože veškerou práci dělá lokální server běžící vedle něj.

### Nutné

- **Python 3.10+**
- **ExifTool** — <https://exiftool.org>, musí být na PATH
  (nebo uprav `EXIFTOOL_PATH` v `config.py`)

### Volitelné (doporučené)

- **MegaDetector** `md_v5a.0.0.pt` do složky `models/` —
  <https://github.com/agentmorris/MegaDetector>
- `pip install torch torchvision ultralytics`

Bez detektoru systém funguje, ale ostrost měří ze středových 60 % snímku.

## Principy

1. **Ostrost se měří na zvířeti, ne na snímku** — ostrá tráva a rozmazaný
   gepard jinak vyhrají.
2. **Porovnává se uvnitř série, ne proti pevnému prahu** — světlo i objektiv
   jsou uvnitř série stejné, absolutní práh napříč expedicí nefunguje.
3. **Rozhodnutí žijí v databázi, do souborů se zapisuje až nakonec** —
   nic se nemaže, originály zůstávají bitově nedotčené.

## Struktura

| Soubor | Role |
|---|---|
| `run.py` | spouštěč — server + prohlížeč |
| `server.py` | FastAPI backend (localhost only) |
| `pipeline.py` | řízení kroků: import → náhledy → detekce → metriky → série → skóre |
| `ingest.py` / `proxy.py` / `detect.py` / `metrics.py` | jednotlivé kroky |
| `grouping.py` / `scoring.py` | série, scény, návrh nejlepšího |
| `profiles.json` / `profiles.py` / `exif_profile.py` | profily hodnocení + návrh z EXIF |
| `xmp.py` | zápis XMP sidecarů pro Zoner |
| `calibration.py` | měření shody návrhů s tvým výběrem |
| `db.py` | SQLite vrstva (workspace/wildsort.db) |
| `web/` | rozhraní (vanilla JS, bez build kroku) |

Pracovní data (databáze, náhledy) vznikají ve složce `workspace/` — není
ve gitu, smazáním se nic z fotek neztratí, jen rozhodnutí.
