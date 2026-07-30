"""
WildSort - krok 5: seskupeni do serii a scen.

Dve urovne, protoze jedna nestaci:

  SERIE  - snimky teze pozy, ktere ma smysl porovnavat mezi sebou. Novou
           zacina bud mezera nad 12 s, nebo zmena obsahu. Uvnitr serie
           soutezi snimky mezi sebou, takze serie o jednom snimku je
           k nicemu - a prave ty vznikaly, dokud rozhodoval jen cas
           s dvousekundovou mezerou.

  SCENA  - cela situace. Zacina novou, kdyz je mezera nad 3 minuty NEBO
           kdyz se zmeni obsah snimku. Lev u napajedla focený dvacet minut
           s pauzami da padesat serii tehoz lva; jako scena je to jedna
           udalost. Uvnitr sceny spolu soutezi jen VITEZOVE serii.

Bez teto druhe urovne dostane fotograf 400 rozhodnuti misto 80, a vetsina
z nich se tyka tehoz zvirete v temer stejne poze.

PROC SE SCENY DELI I PODLE OBSAHU

Cas sam nestaci. Kdyz fotograf za dve minuty otoci objektiv od capa
v trave na ptaky na drate, je to podle casu jedna scena, ale ve
skutecnosti dve uplne jine situace - a vyber "nejlepsiho" uvnitr takove
smesi nema smysl. Porovnava se proto i obrazovy popis (viz content.py).

Nova scena zacne, az kdyz se snimek lisi OD PREDCHOZIHO SNIMKU
I OD ZACATKU SCENY. Jedina podminka by nestacila:
  - jen proti predchozimu: jeden nepovedeny zaber uprostred serie
    (rozmazany pohyb, zavreny zaber do trávy) by scenu zbytecne rozsekl
  - jen proti zacatku: pozvolna zmena by se nikdy neprojevila

Kazde serii se zaroven navrhne profil hodnoceni podle EXIF.
"""

from datetime import datetime

import config
import content
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
            f"SELECT id, root_id, capture_time, filename, shutter, focal_length, "
            f"iso, content FROM photos {where} ORDER BY capture_time, filename",
            params
        ).fetchall()

        # Poradi je zavazne: nejdriv se z fotek odstrani odkazy na serie,
        # az potom se serie smazou. Obracene poradi shodi cizi klic.
        if root_id:
            conn.execute(
                "UPDATE photos SET burst_id=NULL, scene_id=NULL WHERE root_id=?",
                (root_id,))
            conn.execute("DELETE FROM bursts WHERE root_id=?", (root_id,))
            conn.execute("DELETE FROM scenes WHERE root_id=?", (root_id,))
        else:
            conn.execute("UPDATE photos SET burst_id=NULL, scene_id=NULL")
            conn.execute("DELETE FROM bursts")
            conn.execute("DELETE FROM scenes")

        # --- 1. prochod: rozdeleni na sceny ---
        scenes = []
        current_scene = []
        prev_time = None
        prev_root = None
        prev_desc = None      # popis predchoziho snimku
        anchor_desc = None    # popis prvniho snimku soucasne sceny

        for row in rows:
            t = datetime.fromisoformat(row["capture_time"])
            desc = content.from_blob(row["content"]) if config.GROUP_BY_CONTENT else None

            new_scene = False
            if prev_time is not None:
                gap = (t - prev_time).total_seconds()
                if gap > config.SCENE_GAP_SECONDS or row["root_id"] != prev_root:
                    new_scene = True
                elif config.GROUP_BY_CONTENT:
                    # Musi se lisit od predchoziho snimku i od zacatku sceny.
                    # Chybi-li popis (starsi data), rozhoduje dal jen cas.
                    d_prev = content.distance(prev_desc, desc)
                    d_anchor = content.distance(anchor_desc, desc)
                    if (d_prev is not None and d_anchor is not None
                            and d_prev > config.SCENE_CONTENT_THRESHOLD
                            and d_anchor > config.SCENE_CONTENT_THRESHOLD):
                        new_scene = True

            if new_scene:
                scenes.append(current_scene)
                current_scene = []
                anchor_desc = None

            current_scene.append(row)
            if anchor_desc is None:
                anchor_desc = desc
            prev_time = t
            prev_root = row["root_id"]
            # Snimek bez popisu nesmi vymazat pamet scény, jinak by se
            # dalsi porovnani delalo naslepo.
            if desc is not None:
                prev_desc = desc

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
            prev_desc = None
            bursts_here = 0

            for row in scene_rows:
                t = datetime.fromisoformat(row["capture_time"])
                desc = (content.from_blob(row["content"])
                        if config.GROUP_BY_CONTENT else None)

                new_group = False
                if prev is not None:
                    if (t - prev).total_seconds() > config.BURST_GAP_SECONDS:
                        new_group = True
                    elif config.GROUP_BY_CONTENT:
                        # Uvnitr serie se porovnava jen s predchozim snimkem
                        # a jen barevne slozeni, bez kompozice: zvire se po
                        # zaberu hybe, ale je to porad tentyz zaber.
                        d = content.distance(prev_desc, desc, grid_weight=0.0)
                        if d is not None and d > config.BURST_CONTENT_THRESHOLD:
                            new_group = True
                if len(group) >= config.MAX_BURST_SIZE:
                    new_group = True

                if new_group:
                    _flush_burst(conn, group, scene_id)
                    bursts_here += 1
                    group = []

                group.append(row)
                prev = t
                if desc is not None:
                    prev_desc = desc

            if group:
                _flush_burst(conn, group, scene_id)
                bursts_here += 1

            burst_count += bursts_here
            conn.execute("UPDATE scenes SET burst_count=? WHERE id=?",
                         (bursts_here, scene_id))

    return {"scenes": len(scenes), "bursts": burst_count, "photos": len(rows)}
