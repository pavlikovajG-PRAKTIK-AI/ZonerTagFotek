"""
WildSort - automaticky navrh profilu z EXIF.

Zamer je casto zapsany uz v nastaveni fotoaparatu. Kdyz nekdo na 600mm
teleobjektiv nastavi 1/2000 s, chce zmrazit pohyb - je to pták v letu.
Kdyz na stejnou ohnisku nastavi 1/30 s, mrazit nechce a rozmaz je zamer.

System profil jen NAVRHNE. Fotograf ho v roletce prepise, kdyz se
netrefi. Navrh je vzdy lepsi vychozi bod nez jednotny "standard" pro
celou expedici.

Pravidla se vyhodnocuji shora dolu, prvni sedici vyhrava.
"""

import re

import config
import profiles

# ---------------------------------------------------------------------------
# Prahy pro rozhodovani podle NAMERENYCH hodnot
# ---------------------------------------------------------------------------
# Vsechny hodnoty vybrany z rozdeleni na 1356 skutecnych snimcich z Pantanalu
# (169 serii). U kazde je v zavorce, kolik serii pod ni na tech datech spadlo.

# Jas subjektu 0-255, pod timto je snimek skutecne tmavy. Kvartily na jejich
# datech: 25 % = 59, median = 82, 75 % = 113. (24 serii)
DARK_EXPOSURE = 45.0

# Podil zalitych stinu, nad kterym uz je scena problematicka i pri vyssim jasu
DARK_SHADOWS = 0.05

# Podil vypalenych svetel, nad kterym se ma expozice prestat trestat
BRIGHT_CLIPPING = 0.05

# Plocha zvirete v zaberu. Pod SMALL je nesmysl trestat velikost a umisteni
# (pták v letu, zvire v siroke krajine); nad BIG je subjekt cely v zaberu
# a na kompozici i expozici je cas.
# Kvartily: 25 % = 0.041, median = 0.117, 75 % = 0.360.
SMALL_SUBJECT = 0.03    # (36 serii)
BIG_SUBJECT = 0.15      # (74 serii)

# Jak moc musi byt subjekt uriznuty okrajem, aby se to bralo jako zamer
# (u letu bezne) a neslo to k tizi. (7 serii)
EDGE_HEAVY = 0.10


def parse_shutter(value):
    """Prevede zapis casu zavěrky na sekundy. Zvlada '1/2000', '0.5', 2."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den = re.sub(r"[^0-9.]", "", den)
            return float(num) / float(den)
        return float(re.sub(r"[^0-9.]", "", text))
    except (ValueError, ZeroDivisionError):
        return None


def suggest(shutter=None, focal=None, iso=None, aperture=None):
    """Vrati (nazev_profilu, duvod) podle parametru snimku.

    Vsechny vstupy jsou nepovinne - chybejici udaj pravidlo jen preskoci.
    """
    speed = parse_shutter(shutter)
    focal = float(focal) if focal else None
    iso = int(iso) if iso else None

    # 1) Dlouhy cas na dlouhem skle = zamerny svenk nebo panorámovani.
    #    Nikdo nefoti lva na 1/30 s omylem, kdyz ma 600 mm v ruce.
    if speed and focal and speed >= 1 / 60 and focal >= 200:
        return "umelecky", f"dlouhy cas {shutter} na {focal:.0f} mm - zamerny rozmaz"

    # 2) Velmi kratky cas na dlouhem skle = mrazeni rychleho pohybu.
    if speed and focal and speed <= 1 / 1250 and focal >= 300:
        return "let_ptaka", f"kratky cas {shutter} na {focal:.0f} mm - rychly subjekt"

    # 3) Vysoke ISO = sero, mlha, hustý porost, svitani.
    if iso and iso >= 6400:
        return "spatne_svetlo", f"ISO {iso} - malo svetla"

    # 4) Delsi cas pri nizkem ISO na kratsim skle = staticky subjekt.
    if speed and iso and speed >= 1 / 250 and iso <= 1600:
        return "klidny_savec", f"cas {shutter} pri ISO {iso} - klidna scena"

    # 5) Kratky cas bez dlouheho skla - stale spis pohyb nez portret.
    if speed and speed <= 1 / 2000:
        return "let_ptaka", f"velmi kratky cas {shutter}"

    return "standard", "bez vyrazneho znaku v EXIF"


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[len(vals) // 2]


def _field(rows, name):
    """Median jednoho sloupce, kdyz v datech vubec je."""
    if not rows:
        return None
    try:
        rows[0][name]
    except (IndexError, KeyError):
        return None
    return _median([r[name] for r in rows])


def suggest_from_metrics(rows):
    """Navrhne profil podle NAMERENYCH vlastnosti snimku.

    PROC NE PODLE EXIF

    Nastaveni fotoaparatu popisuje zvyk fotografa, ne scenu. Merene na 1356
    snimcich z Pantanalu:

      - pravidlo "cas <= 1/2000 -> let ptaka" oznacilo 94 z 169 serii, tedy
        56 %. Cas 1/2500 na 500 mm je ale bezny zpusob focení VSEHO, takze
        oznaceni nic nerika: jaguar sedici na vetvi dostal "let ptaka".

      - pravidlo "ISO >= 6400 -> sero a mlha" oznacilo 21 serii, z nichz
        SKUTECNE tmave byly 2. Vysoke ISO neznamena tmavy snimek - byva
        nastavene proto, aby se zmrazil pohyb. Serie oznacene jako "sero"
        mely median jasu 72, zatimco "standard" 59, tedy byly SVETLEJSI
        nez prumer.

    Profil pritom neni nazev situace, ale sada pravidel hodnoceni. Rozhoduje
    o tom, jestli se ma trestat maly subjekt, subjekt u okraje a spatna
    expozice. Na tyto otazky odpovidaji NAMERENE hodnoty, ktere uz v databazi
    jsou, mnohem lip nez cas zaverky:

      jas subjektu       skutecna tma, ne domnenka podle ISO
      plocha subjektu    male zvire v siroke krajine vs. portret
      subjekt u okraje   uriznuty subjekt je u letu bezny, u portretu vada

    Rozdeleni na jejich datech (169 serii): tmavych 24, s malym subjektem 36,
    s velkym subjektem 74, se subjektem u okraje 7.

    Vraci (nazev_profilu, duvod) nebo None, kdyz metriky nejsou k dispozici
    (stara data, jeste nezanalyzovane snimky) - pak rozhoduje EXIF.
    """
    expo = _field(rows, "exposure")
    area = _field(rows, "subject_area")
    if expo is None and area is None:
        return None

    clow = _field(rows, "clipped_low") or 0.0
    chigh = _field(rows, "clipped_high") or 0.0
    edge = _field(rows, "edge_cut") or 0.0

    # 1) Skutecna tma nebo zalite stiny. Nizka podlaha ostrosti a expozice
    #    se skoro netresta - v seru je sum a mekkost dana svetlem, ne chybou.
    if expo is not None and (expo < DARK_EXPOSURE or clow > DARK_SHADOWS):
        return "spatne_svetlo", f"jas subjektu {expo:.0f} - malo svetla"

    # 2) Maly subjekt nebo subjekt u okraje. Presne situace, kde je nesmysl
    #    trestat velikost a umisteni: pták v letu, zvire v siroke krajine.
    if area is not None and area < SMALL_SUBJECT:
        return "let_ptaka", f"zvire zabira {area*100:.1f} % zaberu - maly subjekt"
    if edge > EDGE_HEAVY:
        return "let_ptaka", f"zvire u okraje ({edge:.2f}) - uriznuty subjekt"

    # 3) Velky subjekt cely v zaberu. Na velikost a cistou expozici je cas,
    #    takze se obe vazi vic.
    if area is not None and area > BIG_SUBJECT and edge <= EDGE_HEAVY:
        return "klidny_savec", f"zvire zabira {area*100:.0f} % zaberu - velky subjekt"

    # Vypalena svetla se ZAMERNE neresi vlastnim profilem. Skore se pocita
    # relativne uvnitr serie, takze problem, ktery maji vsechny snimky serie
    # stejne, se pri normalizaci vykrati a na poradi nema vliv. Drive tenhle
    # pripad padal na "spatne_svetlo", coz u snimku s jasem 206 znamenalo
    # oznaceni "sero a mlha" - pravidlo spravne, nazev nesmysl.
    return "standard", "bezny zaber"


def suggest_for_burst(rows):
    """Navrhne profil pro celou serii.

    Prednost maji NAMERENE vlastnosti snimku; EXIF slouzi jako zaloha, kdyz
    metriky jeste nejsou spoctene. Vsude se bere median pres serii, aby jeden
    vybocujici snimek neprevalcoval celek.
    """
    if not config.AUTO_PROFILE or not rows:
        return profiles.DEFAULT_NAME, "automaticky navrh vypnuty"

    from_metrics = suggest_from_metrics(rows)
    if from_metrics is not None:
        return from_metrics

    return suggest(
        shutter=_median([r["shutter"] for r in rows if r["shutter"]]),
        focal=_median([r["focal_length"] for r in rows if r["focal_length"]]),
        iso=_median([r["iso"] for r in rows if r["iso"]]),
    )
