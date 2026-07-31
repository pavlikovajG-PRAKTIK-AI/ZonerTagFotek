"""
WildSort - zpetna vazba po finalni editaci v Zoneru.

PROC TO EXISTUJE

Trideni ve WildSortu je predvyber. Ktere snimky se skutecne dostanou
"na prvni stranku", ukaze az dalsi prace v Zoneru - a Zoner zapisuje
zmenene hvezdicky do TYCH SAMYCH XMP sidecaru, ktere WildSort zalozil.
Finalni stav se tedy da precist zpatky a porovnat se dvema drivejsimi
vrstvami rozhodnuti:

    navrh systemu (auto_rating)  ->  prvni trideni (rating)  ->  final (XMP)

Z porovnani vypadne:
  - jak dobre system navrhoval PROTI FINALNIMU vysledku, ne jen proti
    prvnimu dojmu pri trideni
  - ktere snimky fotografka pri finalni editaci povysila/ponizila -
    presne ty jsou nejcennejsi trenovaci data pro budouci model vkusu
    (tabulka decisions zachycuje prvni dojem, final_rating vysledek)

Finalni hodnoceni se ulozi do photos.final_rating a NIKDY se nedotyka
sloupce rating - ten patri prvnimu trideni a je to historicky zaznam.
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import config
import db


def _read_sidecar_ratings(paths, chunk=200):
    """Precte XMP:Rating z mnoha sidecaru. Vraci {absolutni cesta: rating}.

    Jeden proces exiftoolu na davku 200 souboru - stejny princip jako
    u exportu (start exiftoolu je drahy, cteni levne).
    """
    result = {}
    for start in range(0, len(paths), chunk):
        part = [str(p) for p in paths[start:start + chunk]]
        fd, argfile = tempfile.mkstemp(suffix=".args", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(
                    ["-charset", "filename=utf8", "-j", "-n", "-XMP:Rating"]
                    + part) + "\n")
            out = subprocess.run(
                [config.EXIFTOOL_PATH, "-@", argfile],
                capture_output=True, text=True, timeout=600,
                encoding="utf-8", errors="replace")
            data = json.loads(out.stdout) if out.stdout.strip() else []
        except Exception:
            data = []
        finally:
            try:
                os.unlink(argfile)
            except OSError:
                pass

        for item in data:
            src = item.get("SourceFile")
            if not src:
                continue
            try:
                rating = int(item.get("Rating") or 0)
            except (TypeError, ValueError):
                rating = 0
            result[str(Path(src))] = rating
    return result


def audit(root_id):
    """Porovna finalni hvezdicky v XMP s navrhem systemu a prvnim tridenim.

    Cte sidecary, uklada photos.final_rating a vraci souhrn. Nic v XMP
    nemeni a sloupec rating (prvni trideni) nechava byt.

    Skala je obracena: 1* = nejlepsi ("prvni stranka"), 5* = k vymazani.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.*, b.profile AS b_profile FROM photos p "
            "LEFT JOIN bursts b ON b.id = p.burst_id "
            "WHERE p.root_id=? AND p.reviewed=1", (root_id,)).fetchall()
        if not rows:
            return {"error": "Zadne vyrizene snimky k porovnani."}

        sides = []
        by_side = {}
        for r in rows:
            path = db.absolute_photo_path(conn, r)
            side = path.with_suffix(".xmp")
            if side.exists():
                sides.append(side)
                by_side[str(side)] = r

        if not sides:
            return {"error": "Zadne XMP sidecary nenalezeny - nejdriv zapis do XMP."}

        final_map = _read_sidecar_ratings(sides)

        now = datetime.now().isoformat()
        n = with_final = 0
        promoted = demoted = confirmed = 0
        first_page = 0
        system_hit_final = system_total = 0
        changes = []

        for side_str, row in by_side.items():
            final = final_map.get(side_str)
            if final is None:
                continue
            n += 1
            conn.execute(
                "UPDATE photos SET final_rating=?, final_checked_at=? WHERE id=?",
                (final, now, row["id"]))

            first = row["rating"] or 0
            auto = row["auto_rating"] or 0
            if final:
                with_final += 1
            if final == 1:
                first_page += 1

            # Zmeny mezi prvnim tridenim a finalem (obracena skala:
            # mensi cislo = lepsi)
            if first and final and final != first:
                if final < first:
                    promoted += 1
                else:
                    demoted += 1
                if len(changes) < 40:
                    changes.append({
                        "filename": row["filename"],
                        "navrh": auto, "trideni": first, "final": final,
                        "profil": row["b_profile"],
                    })
            elif first and final == first:
                confirmed += 1

            # Uspesnost SYSTEMU proti finalu: navrh 1* se pocita za zasah,
            # kdyz snimek ve finale prezil jako 1-2*
            if auto == 1:
                system_total += 1
                if final in (1, 2):
                    system_hit_final += 1

        conn.commit()

    return {
        "root_id": root_id,
        "checked": n,
        "first_page": first_page,
        "confirmed": confirmed,
        "promoted": promoted,
        "demoted": demoted,
        "system_proposals": system_total,
        "system_survived": system_hit_final,
        "system_rate": round(system_hit_final / system_total * 100, 1)
                       if system_total else None,
        "changes": changes,
    }
