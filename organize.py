"""
WildSort - roztrideni snimku do slozek podle scen.

Kazda scena je jedna situace: jeden pták na jedne vetvi, jedno zvire
u napajedla. Rucne se takova slozka pojmenuje podle zvirete, coz ale
znamena projit tisic snimku dopredu jen kvuli tomu, aby se vedelo, jak
je pojmenovat. Tohle udela mechanickou cast prace: slozky 001, 002, 003
podle casu, kazda s jednou scenou. Jmena zvirat pak lze doplnit
prejmenovanim slozky, uz s vedomim, co v ni je.

DVE ZASADY, KTERE TENTO MODUL DRZI:

(1) SNIMKY, KTERE UZ V PODSLOZCE JSOU, SE NEDOTKNE.
    Rucne pojmenovana slozka (DatelOranzovy, Ara5) je lidske rozhodnuti
    a to je vzdy cennejsi nez automatika. Presouvaji se jen snimky lezici
    volne v korenu importu.

(2) NIC SE NEPREPISUJE ANI NEMAZE.
    Kdyz uz soubor v cili existuje, snimek se preskoci a nahlasi.
    XMP sidecar jde s originalem, jinak by se rozhodnuti ztratilo.

Cesty v databazi se prubezne aktualizuji, takze nahledy, hodnoceni ani
zapis do XMP presunem netrpi.
"""

import re
import shutil
from pathlib import Path

import db

# Nazev slozky sceny: 001, 002, ... Trojmistne cislo staci na 999 scen,
# coz je pri 3 minutach mezery vic, nez se za expedici nafoti.
FOLDER_PATTERN = re.compile(r"^(\d{3,})$")


def folder_name(number):
    return f"{number:03d}"


def _next_free_number(root_path):
    """Vrati prvni volne cislo slozky.

    Pri opakovanem spusteni se navazuje za jiz vytvorene slozky, aby se
    scena nikdy nepridala do slozky, ktera uz patri jine.
    """
    highest = 0
    if root_path.is_dir():
        for entry in root_path.iterdir():
            if entry.is_dir():
                m = FOLDER_PATTERN.match(entry.name)
                if m:
                    highest = max(highest, int(m.group(1)))
    return highest + 1


def plan(root_id):
    """Spocita, co by presun udelal. Nic nemeni.

    Vraci scény, ktere maji aspon jeden snimek volne v korenu importu,
    v poradi podle casu, spolu s navrzenym cislem slozky.
    """
    with db.connect() as conn:
        root_path = db.get_root_path(conn, root_id)
        if root_path is None:
            return {"error": "Import nenalezen"}

        rows = conn.execute(
            "SELECT p.id, p.rel_path, p.filename, p.scene_id, p.capture_time, "
            "       s.start_time AS scene_start "
            "FROM photos p LEFT JOIN scenes s ON s.id = p.scene_id "
            "WHERE p.root_id=? ORDER BY p.capture_time, p.filename",
            (root_id,),
        ).fetchall()

    loose_by_scene = {}
    in_subfolder = 0
    without_scene = 0

    for row in rows:
        # Snimek uz v podslozce = rucne zarazeny, nechat byt
        if Path(row["rel_path"]).parent != Path("."):
            in_subfolder += 1
            continue
        if not row["scene_id"]:
            without_scene += 1
            continue
        loose_by_scene.setdefault(row["scene_id"], []).append(dict(row))

    # Poradi slozek podle casu prvniho snimku sceny
    ordered = sorted(loose_by_scene.items(),
                     key=lambda kv: kv[1][0]["capture_time"] or "")

    number = _next_free_number(root_path)
    scenes = []
    for scene_id, photos in ordered:
        scenes.append({
            "scene_id": scene_id,
            "folder": folder_name(number),
            "count": len(photos),
            "start_time": photos[0]["capture_time"],
            "end_time": photos[-1]["capture_time"],
            "first_file": photos[0]["filename"],
            "photo_ids": [p["id"] for p in photos],
        })
        number += 1

    return {
        "root_id": root_id,
        "root_path": str(root_path),
        "scenes": scenes,
        "to_move": sum(s["count"] for s in scenes),
        "in_subfolder": in_subfolder,
        "without_scene": without_scene,
        "first_folder": scenes[0]["folder"] if scenes else None,
        "last_folder": scenes[-1]["folder"] if scenes else None,
    }


def apply(root_id, progress=None):
    """Vytvori slozky scen a presune do nich snimky.

    Presouva se soubor i jeho XMP sidecar. Cesta v databazi se upravi
    hned po kazdem presunu, takze prerusení uprostred nic nerozbije -
    cast snimku bude v novych slozkach, cast v korenu, a databaze bude
    u obou vedet, kde je.
    """
    proposal = plan(root_id)
    if proposal.get("error"):
        return proposal

    moved = skipped = failed = 0
    created = []
    first_error = None
    done = 0
    total = proposal["to_move"]

    with db.connect() as conn:
        root_path = db.get_root_path(conn, root_id)

        for scene in proposal["scenes"]:
            target_dir = root_path / scene["folder"]
            target_dir.mkdir(exist_ok=True)
            moved_here = 0

            for photo_id in scene["photo_ids"]:
                row = conn.execute(
                    "SELECT rel_path, filename FROM photos WHERE id=?",
                    (photo_id,)).fetchone()
                if not row:
                    continue

                src = root_path / row["rel_path"]
                dest = target_dir / row["filename"]
                done += 1

                if not src.exists():
                    skipped += 1
                    first_error = first_error or f"Soubor chybi: {src}"
                    continue
                if dest.exists():
                    skipped += 1
                    first_error = first_error or f"V cili uz existuje: {dest}"
                    continue

                try:
                    shutil.move(str(src), str(dest))
                except Exception as e:
                    failed += 1
                    first_error = first_error or f"{row['filename']}: {e}"
                    continue

                # Sidecar jde s originalem, jinak se rozhodnuti ztrati
                side = src.with_suffix(".xmp")
                if side.exists():
                    side_dest = dest.with_suffix(".xmp")
                    if not side_dest.exists():
                        try:
                            shutil.move(str(side), str(side_dest))
                        except Exception:
                            pass

                conn.execute(
                    "UPDATE photos SET rel_path=? WHERE id=?",
                    (str(dest.relative_to(root_path)), photo_id))
                moved += 1
                moved_here += 1

                if progress and done % 20 == 0:
                    progress(done, total)

            # Kdyz se do slozky nic nepresunulo, nema tam co zustat -
            # prazdna slozka 007 by jen pletla pri prohlizeni v Prizkumniku.
            if moved_here:
                created.append(scene["folder"])
            else:
                try:
                    target_dir.rmdir()
                except OSError:
                    pass

    result = {
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "folders": len(created),
        "first_folder": created[0] if created else None,
        "last_folder": created[-1] if created else None,
    }
    if first_error:
        result["message"] = first_error
    return result
