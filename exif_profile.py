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


def suggest_for_burst(rows):
    """Navrhne profil pro celou serii z jejich snimku.

    Bere median casu a ohniska - jeden vybocujici snimek tak neprevalcuje
    celou serii.
    """
    if not config.AUTO_PROFILE or not rows:
        return profiles.DEFAULT_NAME, "automaticky navrh vypnuty"

    speeds = [r["shutter"] for r in rows if r["shutter"]]
    focals = [r["focal_length"] for r in rows if r["focal_length"]]
    isos = [r["iso"] for r in rows if r["iso"]]

    def median(values):
        if not values:
            return None
        s = sorted(values)
        return s[len(s) // 2]

    return suggest(
        shutter=median(speeds),
        focal=median(focals),
        iso=median(isos),
    )
