"""
WildSort - krok 1: import.

Projde zadany adresar, najde podporovane soubory, spocita rychly hash
pro detekci duplicit a vytahne zakladni EXIF udaje. Nic nekopiruje ani
neupravuje - jen zapisuje zaznamy do databaze.
"""

import hashlib
import os
import subprocess
import json
from datetime import datetime
from pathlib import Path

import config
import db


def quick_hash(path, chunk_size=65536):
    """Rychly hash: velikost souboru + prvni a posledni blok.

    Cteni celeho 50MB RAWu jen kvuli duplicitam by import zbytecne
    prodlouzilo o hodiny. Tahle varianta je na detekci dvou kopii teze
    fotky naprosto dostacujici.
    """
    size = os.path.getsize(path)
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(chunk_size))
        if size > chunk_size * 2:
            f.seek(-chunk_size, os.SEEK_END)
            h.update(f.read(chunk_size))
    return h.hexdigest()


def read_exif_batch(paths):
    """Precte EXIF z davky souboru jednim volanim exiftool.

    Spousteni exiftool pro kazdy soubor zvlast je u 10 000 fotek
    nekolikanasobne pomalejsi nez davkove volani.
    """
    if not paths:
        return {}
    cmd = [
        config.EXIFTOOL_PATH, "-j", "-n",
        "-DateTimeOriginal", "-CreateDate",
        "-Model", "-LensModel", "-ISO",
        "-ExposureTime", "-FNumber", "-FocalLength",
    ] + [str(p) for p in paths]

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        data = json.loads(out.stdout) if out.stdout.strip() else []
    except Exception:
        return {}

    result = {}
    for item in data:
        src = item.get("SourceFile")
        if src:
            result[os.path.normpath(src)] = item
    return result


def parse_capture_time(exif, fallback_path):
    """Vrati cas porizeni. EXIF ma prednost, jinak cas souboru."""
    for key in ("DateTimeOriginal", "CreateDate"):
        value = exif.get(key)
        if value:
            try:
                return datetime.strptime(str(value)[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                pass
    return datetime.fromtimestamp(os.path.getmtime(fallback_path))


def find_photos(root):
    """Rekurzivne najde vsechny podporovane soubory pod korenem."""
    root = Path(root)
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if Path(name).suffix.lower() in config.SUPPORTED_EXTENSIONS:
                found.append(Path(dirpath) / name)
    return sorted(found)


def import_folder(folder, label=None, progress=None):
    """Naimportuje adresar. Vraci slovnik se souhrnem.

    Opakovany import teze slozky je bezpecny - existujici zaznamy se
    preskoci, prida se jen to nove.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f"Adresar neexistuje: {folder}")

    db.init_db()
    files = find_photos(folder)
    added = skipped = duplicates = 0

    with db.connect() as conn:
        cur = conn.execute("SELECT id FROM roots WHERE path=?", (str(folder),))
        row = cur.fetchone()
        if row:
            root_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO roots (path, label, imported_at) VALUES (?,?,?)",
                (str(folder), label or folder.name, datetime.now().isoformat()),
            )
            root_id = cur.lastrowid

        existing = {
            r["rel_path"]
            for r in conn.execute("SELECT rel_path FROM photos WHERE root_id=?", (root_id,))
        }
        # Hash -> id originalu. Duplicity se NEZAHAZUJI, jen se oznaci
        # odkazem na original. Kdyz omylem naimportujes zalohu, chces to
        # vedet, ne aby soubory tise zmizely.
        known_hashes = {
            r["file_hash"]: r["id"]
            for r in conn.execute("SELECT id, file_hash FROM photos")
            if r["file_hash"]
        }

        # zpracovani po davkach kvuli exiftool
        for i in range(0, len(files), config.BATCH_SIZE):
            batch = files[i:i + config.BATCH_SIZE]
            batch = [p for p in batch if str(p.relative_to(folder)) not in existing]
            if not batch:
                skipped += len(files[i:i + config.BATCH_SIZE])
                continue

            exif_map = read_exif_batch(batch)

            for path in batch:
                rel = str(path.relative_to(folder))
                exif = exif_map.get(os.path.normpath(str(path)), {})
                fhash = quick_hash(path)

                original_id = known_hashes.get(fhash)
                capture = parse_capture_time(exif, path)

                # Duplicita se zapise take, ale ve stavu 'duplicate' -
                # nezpracovava se dal a v rozhrani se neobjevi. Zustane
                # ale dohledatelna, vcetne odkazu na original.
                stage = "duplicate" if original_id else "ingested"

                cur2 = conn.execute(
                    """INSERT INTO photos
                       (root_id, rel_path, filename, file_hash, duplicate_of,
                        file_size, capture_time, camera, lens, iso, shutter,
                        aperture, focal_length, stage)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        root_id, rel, path.name, fhash, original_id,
                        os.path.getsize(path), capture.isoformat(),
                        exif.get("Model"), exif.get("LensModel"), exif.get("ISO"),
                        str(exif.get("ExposureTime", "")), exif.get("FNumber"),
                        exif.get("FocalLength"), stage,
                    ),
                )

                if original_id:
                    duplicates += 1
                else:
                    known_hashes[fhash] = cur2.lastrowid
                    added += 1

            if progress:
                progress(min(i + config.BATCH_SIZE, len(files)), len(files))

    return {
        "root_id": root_id,
        "found": len(files),
        "added": added,
        "skipped": skipped,
        "duplicates": duplicates,
    }
