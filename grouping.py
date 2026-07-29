"""
WildSort - krok 5: seskupeni do serii a scen.

Dve urovne, protoze jedna nestaci:

  SERIE  - davka ze serioveho snimani. Mezera nad 2 s zacina novou.
           Uvnitr serie soutezi snimky mezi sebou.

  SCENA  - cela situace. Mezera nad 3 minuty zacina novou. Lev u
           napajedla focený dvacet minut s pauzami da padesat serii
           tehoz lva; jako scena je to jedna udalost. Uvnitr sceny
           spolu soutezi jen VITEZOVE jednotlivych serii.

Bez teto druhe urovne dostane fotograf 400 rozhodnuti misto 80, a vetsina
z nich se tyka tehoz zvirete v temer stejne poze.

Kazde serii se zaroven navrhne profil hodnoceni podle EXIF.
"""

from datetime import datetime

import config
import db
import exif_profile


def _flush_burst(conn, group, scene_id):
    """Zalozi serii ze skupiny snimku. Vraci id serie."""
    if not group:
        return None

    profile, reason = exif_profile.suggest_for_burst(group)

    cur = conn.execute(
        "INSERT INTO bursts (root_id, start_time, end_time, photo_count, "
        "scene_id, profile, auto_profile) VALUES (?,?,?,?,?,?,?)",
        (group[0]["root_id"], group[0]["capture_time"], group[-1]["capture_time"],
         len(group), scene_id, profile, profile),
    )
    burst_id = cur.lastrowid

    conn.executemany(
        "UPDATE photos SET burst_id=?, scene_id=? WHERE id=?",
        [(burst_id, scene_id, r["id"]) for r in group],
    )
    return burst_id


def _flush_scene(conn, conn_rows, root_id):
    """Zalozi scenu. Vraci id sceny."""
    if not conn_rows:
        return None
    cur = conn.execute(
        "INSERT INTO scenes (root_id, start_time, end_time, photo_count) "
        "VALUES (?,?,?,?)",
        (root_id, conn_rows[0]["capture_time"], conn_rows[-1]["capture_time"],
         len(conn_rows)),
    )
    return cur.lastrowid


def run(root_id=None):
    """Rozdeli fotky do scen a serii. Existujici rozdeleni prepise.

    Rozhodnuti fotografa (hvezdicky, priznaky) zustavaji nedotcena -
    meni se jen struktura.
    """
    with db.connect() as conn:
        where = "WHERE capture_time IS NOT NULL AND stage != 'duplicate'"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)

        rows = conn.execute(
            f"SELECT id, root_id, capture_time, filename, shutter, focal_length, iso "
            f"FROM photos {where} ORDER BY capture_time, filename", params
        ).fetchall()

        if root_id:
            conn.execute("DELETE FROM bursts WHERE root_id=?", (root_id,))
            conn.execute("DELETE FROM scenes WHERE root_id=?", (root_id,))
            conn.execute(
                "UPDATE photos SET burst_id=NULL, scene_id=NULL WHERE root_id=?",
                (root_id,))
        else:
            conn.execute("UPDATE photos SET burst_id=NULL, scene_id=NULL")
            conn.execute("DELETE FROM bursts")
            conn.execute("DELETE FROM scenes")

        # --- 1. prochod: rozdeleni na sceny ---
        scenes = []
        current_scene = []
        prev_time = None
        prev_root = None

        for row in rows:
            t = datetime.fromisoformat(row["capture_time"])
            if prev_time is not None:
                gap = (t - prev_time).total_seconds()
                if gap > config.SCENE_GAP_SECONDS or row["root_id"] != prev_root:
                    scenes.append(current_scene)
                    current_scene = []
            current_scene.append(row)
            prev_time = t
            prev_root = row["root_id"]

        if current_scene:
            scenes.append(current_scene)

        # --- 2. prochod: uvnitr kazde sceny rozdeleni na serie ---
        burst_count = 0

        for scene_rows in scenes:
            if not scene_rows:
                continue
            scene_id = _flush_scene(conn, scene_rows, scene_rows[0]["root_id"])

            group = []
            prev = None
            bursts_here = 0

            for row in scene_rows:
                t = datetime.fromisoformat(row["capture_time"])
                new_group = False
                if prev is not None and (t - prev).total_seconds() > config.BURST_GAP_SECONDS:
                    new_group = True
                if len(group) >= config.MAX_BURST_SIZE:
                    new_group = True

                if new_group:
                    _flush_burst(conn, group, scene_id)
                    bursts_here += 1
                    group = []

                group.append(row)
                prev = t

            if group:
                _flush_burst(conn, group, scene_id)
                bursts_here += 1

            burst_count += bursts_here
            conn.execute("UPDATE scenes SET burst_count=? WHERE id=?",
                         (bursts_here, scene_id))

    return {"scenes": len(scenes), "bursts": burst_count, "photos": len(rows)}
