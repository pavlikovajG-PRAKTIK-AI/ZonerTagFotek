"""
WildSort - kalibrace: nakolik se systemu da verit.

Duvera v automatiku ma byt cislo, ne dojem. Kalibrace porovnava, jak
casto se fotografova volba shoduje s navrhem systemu, a to na datech,
ktera uz vznikla behem bezne prace - nic navic se nemusi delat.

Vysledek rika, jestli lze mackat Enter, nebo je nutne prochazet rucne:

  nad 80 %   navrhum lze verit, Enter je bezpecny
  60 - 80 %  navrhy jsou dobry vychozi bod, ale kontroluj
  pod 60 %   profil je spatne nastaveny, nebo tvuj vkus systemu unika

Meri se az od 10 vyrizenych serii - pod tim je cislo jen sum.
"""

import db

MIN_SAMPLE = 10


def measure(root_id=None, profile=None):
    """Spocita shodu mezi navrhem systemu a volbou fotografa.

    Za "volbu fotografa" se bere snimek s nejvyssim hodnocenim v serii.
    Serie, kde fotograf nic nevybral (vsechno vyradil), se do vzorku
    nepocitaji - tam neni co porovnavat.
    """
    with db.connect() as conn:
        where = "WHERE b.reviewed=1 AND b.best_photo_id IS NOT NULL"
        params = []
        if root_id:
            where += " AND b.root_id=?"
            params.append(root_id)
        if profile:
            where += " AND b.profile=?"
            params.append(profile)

        bursts = conn.execute(
            f"SELECT b.id, b.best_photo_id, b.profile FROM bursts b {where}", params
        ).fetchall()

        agreed = considered = near_miss = 0
        per_profile = {}

        for b in bursts:
            photos = conn.execute(
                "SELECT id, rating, flag, score FROM photos WHERE burst_id=? "
                "ORDER BY rating DESC, score DESC", (b["id"],)
            ).fetchall()
            if not photos:
                continue

            chosen = [p for p in photos if (p["rating"] or 0) >= 2 or p["flag"] == "pick"]
            if not chosen:
                continue   # fotograf nevybral nic - neni co porovnavat

            considered += 1
            top = chosen[0]
            hit = top["id"] == b["best_photo_id"]

            # Tesny zasah: system navrhl snimek, ktery fotograf sice
            # nevybral jako prvni, ale taky si ho nechal.
            near = not hit and any(p["id"] == b["best_photo_id"] for p in chosen)

            if hit:
                agreed += 1
            elif near:
                near_miss += 1

            key = b["profile"] or "standard"
            stat = per_profile.setdefault(key, {"agreed": 0, "total": 0})
            stat["total"] += 1
            if hit:
                stat["agreed"] += 1

    rate = (agreed / considered * 100) if considered else None
    near_rate = ((agreed + near_miss) / considered * 100) if considered else None

    for stat in per_profile.values():
        stat["rate"] = round(stat["agreed"] / stat["total"] * 100, 1) if stat["total"] else None

    return {
        "sample": considered,
        "agreed": agreed,
        "near_miss": near_miss,
        "rate": round(rate, 1) if rate is not None else None,
        "rate_with_near": round(near_rate, 1) if near_rate is not None else None,
        "enough_data": considered >= MIN_SAMPLE,
        "min_sample": MIN_SAMPLE,
        "per_profile": per_profile,
        "verdict": verdict(rate, considered),
    }


def verdict(rate, sample):
    """Slovni zaver, ktery rika, co s tim delat."""
    if sample < MIN_SAMPLE:
        return (f"Zatim malo dat ({sample} z {MIN_SAMPLE} serii). "
                f"Prochazej dal rucne, cislo se dopocita samo.")
    if rate is None:
        return "Nelze zmerit."
    if rate >= 80:
        return (f"Shoda {rate:.0f} %. Navrhum lze verit - Enter na prijeti "
                f"cele serie je bezpecny.")
    if rate >= 60:
        return (f"Shoda {rate:.0f} %. Navrhy jsou dobry vychozi bod, ale "
                f"kazdou serii se jeste podivej.")
    return (f"Shoda {rate:.0f} %. Systemu tvuj vyber unika. Zkus jiny profil "
            f"serie, nebo uprav vahy v profiles.json.")
