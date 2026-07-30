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


def normalize_weighted(values, full_range=0.15):
    """Normalizace, ktera respektuje VELIKOST realneho rozdilu.

    Prosta normalizace roztahne kazdy rozdil na 0-1: dva snimky s ostrosti
    500 a 490 (2 %) by se lisily stejne jako 500 a 8. Tim by vedlejsi
    kriteria (osvetleni, natoceni) nikdy nedostala slovo. Kdyz je relativni
    rozpeti v serii mensi nez full_range, normalizovana skala se umerne
    stlaci - stejna filozofie jako DUEL_THRESHOLD: male rozdily nemaji
    rozhodovat vsechno.
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [1.0] * len(values)
    significance = min(1.0, ((hi - lo) / max(hi, 1e-9)) / full_range)
    return [(v - lo) / (hi - lo) * significance for v in values]


def normalize_absolute(values, full_range):
    """Jako normalize_weighted, ale vyznamnost se meri ABSOLUTNIM
    rozpetim - pro veliciny, ktere uz samy jsou na skale 0-1
    (nerovnomernost osvetleni). Rozdil 0.04 vs 0.06 je sum, rozdil
    0.04 vs 0.35 je tvar napul ve stinu."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0] * len(values)
    significance = min(1.0, (hi - lo) / full_range)
    return [(v - lo) / (hi - lo) * significance for v in values]


def exposure_penalty(row):
    """Penalizace za vypalena svetla a zalite stiny. 0 = bez problemu."""
    high = row["clipped_high"] or 0.0
    low = row["clipped_low"] or 0.0
    # Vypalena svetla jsou horsi - z prepalu se nic nevytahne.
    return min(1.0, high * 8.0 + low * 3.0)


def _column(row, name):
    """Bezpecne precte sloupec, ktery ve starsi databazi nemusi byt."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def score_burst(rows, params):
    """Spocita skore pro jednu serii podle zadanych parametru profilu.

    Vraci seznam (photo_id, score, auto_rating) serazeny od nejlepsiho.

    Kriteria (vahy urcuje profil):
      sharpness       - nejostrejsi misto subjektu; u divociny je to
                        temer vzdy oko, presne oko bez dalsiho modelu
                        najit neumime, tohle je nejblizsi merena vec
      sharpness_mean  - prumerna ostrost pres cele zvire
      light           - penalizace tvare osvetlene jen z poloviny
                        (rozdil jasu leve a prave pulky subjektu)
      pose            - jen s prefer_side_pose (ptaci): sirsi ramecek
                        znamena profil, uzsi anfas; profil se preferuje
    """
    if not rows:
        return []

    # Rezim bez razeni: algoritmus se vzda hodnoceni. Zadny navrh
    # hvezdicek a poradi zustane podle casu. Je to jediny poctivy postup
    # u zameru, ktery technicke metriky z principu nemeri - zamerny
    # rozmaz by jinak vzdy skoncil posledni prave proto, ze je zamerny.
    if params.get("no_ranking"):
        return [(r["id"], 1.0, 0) for r in rows]

    sharp_norm = normalize_weighted([r["sharpness"] or 0.0 for r in rows])
    mean_norm = normalize_weighted([r["sharpness_mean"] or 0.0 for r in rows])
    size_norm = normalize([r["subject_area"] or 0.0 for r in rows])
    pose_norm = normalize([_column(r, "box_aspect") or 1.0 for r in rows])
    # Svetlo je uvnitr serie stejne, meni se jen natoceni zvirete vuci
    # nemu - proto se nerovnomernost porovnava relativne v ramci serie.
    light_norm = normalize_absolute(
        [_column(r, "light_asym") or 0.0 for r in rows], full_range=0.25)

    scored = []
    for row, s_n, m_n, sz_n, p_n, l_n in zip(rows, sharp_norm, mean_norm,
                                             size_norm, pose_norm, light_norm):
        value = (
            params["w_sharpness"] * s_n
            + params["w_sharpness_mean"] * m_n
            + params["w_subject_size"] * sz_n
            - params["w_exposure"] * exposure_penalty(row)
            - params["w_centering"] * (row["edge_cut"] or 0.0)
            - params["w_light"] * l_n
        )
        if params.get("prefer_side_pose"):
            value += params["w_pose"] * p_n
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

    # Hodnoceni v Zoneru obracene: 1* = nejlepsi serie, 2** = druhy
    # nejlepsi, 5***** = rozmazane / k vymazani. Z kazde serie se tak
    # navrhne prave jedna * a prave jedna **; ostatni bez navrhu.
    results = []
    for rank, (row, value) in enumerate(ranked):
        if value <= 0.0:
            rating = 5        # rozmazane nebo prazdne - k vymazani
        elif rank == 0:
            rating = 1        # nejlepsi ze serie
        elif rank == 1:
            rating = 2        # druhy nejlepsi
        else:
            rating = 0        # bez navrhu
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
