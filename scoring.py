"""
WildSort - krok 6: skore a navrh hodnoceni.

Klicovy princip: snimky se hodnoti RELATIVNE v ramci sve serie, ne proti
pevnemu prahu. Absolutni prah ostrosti selhava napric scenami - hodnota,
ktera je vyborna pro ptaka v mlze, je katastrofalni pro geparda v poledni
savane. Uvnitr serie jsou vsak svetlo, objektiv i vzdalenost temer stejne,
takze porovnani je smysluplne.

Kazda serie muze mit vlastni PROFIL (viz profiles.py). Tim se resi to, ze
let ptaka, klidny portret a umelecky zamerny rozmaz potrebuji uplne jina
pravidla. Zmena profilu prepocita jen dotcenou serii.
"""

import config
import db
import profiles


def normalize(values):
    """Prevede seznam hodnot na rozsah 0-1 v ramci serie."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def exposure_penalty(row):
    """Penalizace za vypalena svetla a zalite stiny. 0 = bez problemu."""
    high = row["clipped_high"] or 0.0
    low = row["clipped_low"] or 0.0
    # Vypalena svetla jsou horsi - z prepalu se nic nevytahne.
    return min(1.0, high * 8.0 + low * 3.0)


def score_burst(rows, params):
    """Spocita skore pro jednu serii podle zadanych parametru profilu.

    Vraci seznam (photo_id, score, auto_rating) serazeny od nejlepsiho.
    """
    if not rows:
        return []

    # Rezim bez razeni: algoritmus se vzda hodnoceni. Vsechny snimky
    # dostanou stejny navrh 3 hvezdicky a poradi zustane podle casu.
    # Je to jediny poctivy postup u zameru, ktery technicke metriky
    # z principu nemeri - zamerny rozmaz by jinak vzdy skoncil posledni
    # prave proto, ze je zamerny.
    if params.get("no_ranking"):
        return [(r["id"], 1.0, 3) for r in rows]

    sharp_norm = normalize([r["sharpness"] or 0.0 for r in rows])
    size_norm = normalize([r["subject_area"] or 0.0 for r in rows])

    scored = []
    for row, s_n, sz_n in zip(rows, sharp_norm, size_norm):
        value = (
            params["w_sharpness"] * s_n
            + params["w_subject_size"] * sz_n
            - params["w_exposure"] * exposure_penalty(row)
            - params["w_centering"] * (row["edge_cut"] or 0.0)
        )
        scored.append([row, max(0.0, value)])

    # Tvrde vyrazeni. Profil "umelecky" ma podlahu na nule, takze
    # zamerne rozmazany snimek se nevyradi - jen se zaradi niz.
    for pair in scored:
        row = pair[0]
        if row["is_empty"]:
            pair[1] = 0.0
        elif (row["sharpness"] or 0.0) < params["sharpness_floor"]:
            pair[1] = 0.0
        elif (row["subject_area"] or 0.0) < params["min_subject_area"]:
            pair[1] *= 0.3

    ranked = sorted(scored, key=lambda p: p[1], reverse=True)

    results = []
    for rank, (row, value) in enumerate(ranked):
        if value <= 0.0:
            rating = 1        # kandidat na vyrazeni
        elif rank == 0:
            rating = 4        # nejlepsi ze serie
        elif rank == 1 and value > 0.75:
            rating = 3        # tesna druha, stoji za pohled
        elif value > 0.5:
            rating = 2
        else:
            rating = 1
        results.append((row["id"], value, rating))

    return results


def _apply(conn, burst_id, profile_name):
    """Prepocita jednu serii a zapise vysledky. Vraci pocet snimku."""
    rows = conn.execute(
        "SELECT * FROM photos WHERE burst_id=? AND stage IN ('analyzed','scored')",
        (burst_id,),
    ).fetchall()
    if not rows:
        return 0

    params = profiles.params(profile_name)
    results = score_burst(rows, params)

    for photo_id, value, rating in results:
        conn.execute(
            "UPDATE photos SET score=?, auto_rating=?, stage='scored' WHERE id=?",
            (value, rating, photo_id),
        )

    # Souboj: kdyz jsou prvni dva tesne u sebe, algoritmus mezi nimi
    # rozhodnout neumi a nema to predstirat. Serie se oznaci a rozhrani
    # nabidne primy souboj vedle sebe.
    duel_a = duel_b = None
    if len(results) >= 2 and not params.get("no_ranking"):
        best, second = results[0][1], results[1][1]
        if best > 0 and (best - second) / best <= config.DUEL_THRESHOLD:
            duel_a, duel_b = results[0][0], results[1][0]

    conn.execute(
        "UPDATE bursts SET best_photo_id=?, profile=?, duel_a=?, duel_b=? WHERE id=?",
        (results[0][0], profile_name, duel_a, duel_b, burst_id),
    )
    return len(results)


def rank_scenes(conn, root_id=None):
    """Seradi VITEZE serii uvnitr kazde sceny.

    Pozor na past: skore je normalizovane UVNITR serie, takze vitez kazde
    serie ma skore blizko 1 a porovnavat je mezi sebou nema smysl - vsichni
    by vysli stejne. Radi se proto podle absolutni ostrosti, ktera je uvnitr
    jedne sceny porovnatelna (stejne svetlo, stejny objektiv, podobna
    vzdalenost). Prazdne snimky jdou vzdy na konec.
    """
    where = ""
    params = []
    if root_id:
        where = "WHERE root_id=?"
        params.append(root_id)

    scenes = conn.execute(f"SELECT id FROM scenes {where}", params).fetchall()

    for scene in scenes:
        winners = conn.execute(
            "SELECT p.id FROM photos p "
            "JOIN bursts b ON b.best_photo_id = p.id "
            "WHERE b.scene_id=? "
            "ORDER BY p.is_empty ASC, p.sharpness DESC",
            (scene["id"],),
        ).fetchall()

        for rank, row in enumerate(winners, start=1):
            conn.execute("UPDATE photos SET scene_rank=? WHERE id=?", (rank, row["id"]))

        if winners:
            conn.execute("UPDATE scenes SET best_photo_id=? WHERE id=?",
                         (winners[0]["id"], scene["id"]))

    return len(scenes)


def rescore_burst(burst_id, profile_name):
    """Prepocita JEDNU serii pod jinym profilem.

    Rozhodnuti fotografa (hvezdicky, priznaky) zustavaji nedotcena -
    meni se jen navrh systemu a poradi.
    """
    with db.connect() as conn:
        count = _apply(conn, burst_id, profile_name)
        row = conn.execute("SELECT scene_id, root_id FROM bursts WHERE id=?",
                           (burst_id,)).fetchone()
        # Zmena vitize serie muze zmenit i poradi ve scene
        if row and row["scene_id"]:
            rank_scenes(conn, row["root_id"])
    return {"burst_id": burst_id, "profile": profile_name, "photos": count}


def run(root_id=None, profile_name=None):
    """Ohodnoti vsechny serie.

    profile_name = None znamena "pouzij profil, ktery ma serie ulozeny".
    Jinak se zadany profil vnuti vsem seriim.
    """
    with db.connect() as conn:
        where = ""
        params_sql = []
        if root_id:
            where = "WHERE root_id=?"
            params_sql.append(root_id)

        bursts = conn.execute(
            f"SELECT id, profile FROM bursts {where}", params_sql).fetchall()

        for b in bursts:
            name = profile_name or b["profile"] or profiles.DEFAULT_NAME
            _apply(conn, b["id"], name)

        scene_count = rank_scenes(conn, root_id)

    return {"bursts": len(bursts), "scenes": scene_count}
