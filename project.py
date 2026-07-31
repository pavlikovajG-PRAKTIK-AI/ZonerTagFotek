"""
WildSort - prenosny projekt: znacka u fotek a hledani rozdelane prace.

CO JE PROJEKT

Slozka s fotkami, ve ktere lezi podslozka _wildsort s databazi a nahledy.
Tim se rozdelane trideni veze s fotkami: disk se prepoji k jinemu pocitaci,
tam se otevre a pokracuje se - nic se nepocita znovu.

PROC SE PROJEKT HLEDA PODLE NAZVU DISKU

Pismeno jednotky patri pocitaci, ne disku. Na stolnim je disk D:, na
notebooku F:. Nazev svazku ("8T_mainBP") se prenosem nemeni, takze je to
udaj, kterym se disk poznat da - a navic jediny, ktery si clovek pamatuje.
Proto se do znacky uklada nazev i seriove cislo svazku a cesta k fotkam
od korene disku, ne cesta s pismenem.

PROHLEDAVANI JE ZAMERNE OMEZENE DO HLOUBKY

Projit cely 8TB disk kvuli jedne slozce by trvalo minuty. Fotky z expedice
ale nikdo nezanori deset urovni hluboko - lezi v korenu disku nebo v jedne
az dvou slozkach pod nim. Hloubka 3 to pokryje a vejde se do sekund.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import config
import volume

MARKER_JSON = "project.json"

# Do jake hloubky pod korenem disku se hleda podslozka _wildsort
SCAN_DEPTH = 3

# Slozky, do kterych nema smysl chodit: systemove, kosy, vyvojarske
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "$windows.~bt",
    "node_modules", ".git", "recovery", "msocache", "config.msi",
    "perflogs", ".venv", "venv", "__pycache__",
}


def write_marker(workspace, photos_folder):
    """Zapise znacku projektu do slozky _wildsort.

    Znacka slouzi dvema vecem: podle ni se projekt najde na jinem pocitaci,
    a za pul roku z ni pozna clovek, co ta slozka vlastne je.
    """
    workspace = Path(workspace)
    photos_folder = Path(photos_folder).resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    vol = volume.info(photos_folder) or {}
    existing = read_marker(workspace) or {}

    data = {
        "app": "WildSort",
        "photos_drive_rel": volume.relative_to_drive(photos_folder),
        "vol_label": vol.get("label"),
        "vol_serial": vol.get("serial"),
        "last_path": str(photos_folder),
        "created": existing.get("created") or datetime.now().isoformat(timespec="seconds"),
        "last_opened": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        (workspace / MARKER_JSON).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    # Lidsky citelna poznamka vedle strojove znacky
    try:
        (workspace / config.PORTABLE_MARKER).write_text(
            "WildSort - rozdelane trideni fotografii\n"
            "=======================================\n\n"
            f"Fotky:    {photos_folder}\n"
            f"Disk:     {vol.get('label') or '?'} (seriove cislo "
            f"{vol.get('serial') or '?'})\n"
            f"Zalozeno: {data['created']}\n\n"
            "V teto slozce je databaze vsech rozhodnuti (wildsort.db) a nahledy\n"
            "snimku. Diky tomu jde trideni dokoncit na jinem pocitaci, aniz by\n"
            "se cokoliv pocitalo znovu.\n\n"
            "NEMAZ tuto slozku, dokud neni trideni zapsane do XMP souboru.\n\n"
            "Pokracovani na jinem pocitaci (pismeno jednotky nehraje roli):\n"
            f"    python run.py --projekt \"{vol.get('label') or photos_folder}\"\n\n"
            "Prehled rozdelanych projektu:\n"
            "    python run.py --najdi\n",
            encoding="utf-8")
    except OSError:
        pass
    return data


def read_marker(workspace):
    """Precte znacku projektu. None, kdyz tam zadna neni."""
    try:
        raw = (Path(workspace) / MARKER_JSON).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _db_summary(workspace):
    """Kolik snimku a kolik vyrizenych je v databazi projektu.

    Cte se pres samostatne spojeni jen pro cteni - hledani projektu nesmi
    zavisle na tom, jaky workspace je prave nastaveny.
    """
    db_file = Path(workspace) / "wildsort.db"
    if not db_file.is_file():
        return None, None
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            total = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM photos WHERE reviewed=1").fetchone()[0]
            return total, done
        finally:
            conn.close()
    except Exception:
        return None, None


def _scan_tree(root, depth):
    """Najde slozky _wildsort pod korenem do zadane hloubky."""
    found = []
    root = Path(root)

    def walk(folder, level):
        if level > depth:
            return
        try:
            entries = list(os.scandir(folder))
        except (OSError, PermissionError):
            return
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if name == config.PORTABLE_DIR_NAME:
                found.append(Path(entry.path))
                continue           # uvnitr workspace uz hledat netreba
            if name.lower() in SKIP_DIRS or name.startswith("$"):
                continue
            walk(entry.path, level + 1)

    walk(root, 1)
    return found


def scan(drives=None, depth=SCAN_DEPTH):
    """Najde rozdelane projekty na pripojenych discich.

    drives = seznam identit svazku (z volume.attached()), None = vsechny.
    Vraci seznam slovniku: photos, workspace, volume, photo_count, reviewed.
    """
    volumes = drives if drives is not None else volume.attached()
    results = []

    for vol in volumes:
        for workspace in _scan_tree(vol["drive"], depth):
            photos = workspace.parent
            total, done = _db_summary(workspace)
            marker = read_marker(workspace)
            results.append({
                "photos": str(photos),
                "workspace": str(workspace),
                "volume": vol,
                "photo_count": total,
                "reviewed": done,
                "created": (marker or {}).get("created"),
                "last_opened": (marker or {}).get("last_opened"),
            })

    results.sort(key=lambda r: (r.get("last_opened") or "", r["photos"]), reverse=True)
    return results
