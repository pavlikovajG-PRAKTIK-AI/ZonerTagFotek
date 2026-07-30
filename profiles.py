"""
WildSort - profily hodnoceni.

Profil je pojmenovana sada parametru, kterou lze priradit KONKRETNI SERII.
Ruzne situace maji ruzna pravidla: u letu ptaka je nesmysl trestat maly
subjekt u okraje, u umelecky zamerneho rozmazu je nesmysl trestat cokoliv.

Profily jsou v souboru profiles.json a lze je volne pridavat a upravovat.
Zmena profilu prepocita jen dotcenou serii - trva to milisekundy, takze
lze zkouset naslepo a hned videt vysledek.
"""

import json
from pathlib import Path

import config

PROFILES_FILE = Path(__file__).resolve().parent / "profiles.json"

DEFAULT_NAME = "standard"

# Zaloha pro pripad, ze profiles.json chybi nebo je poskozeny
_FALLBACK = {
    "standard": {
        "label": "Standardni",
        "note": "Vestavena zaloha.",
        "sharpness_floor": config.HARD_SHARPNESS_FLOOR,
        "min_subject_area": config.MIN_SUBJECT_AREA_RATIO,
        "weights": dict(config.SCORE_WEIGHTS),
    }
}

_cache = None
_mtime = None


def load():
    """Nacte profily. Soubor se cte znovu, jen kdyz se zmenil,
    takze lze profiles.json upravovat za behu serveru."""
    global _cache, _mtime

    try:
        mtime = PROFILES_FILE.stat().st_mtime
    except OSError:
        return dict(_FALLBACK)

    if _cache is None or mtime != _mtime:
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                raise ValueError("prazdny nebo neplatny soubor")
            _cache = data
            _mtime = mtime
        except Exception:
            return dict(_FALLBACK)

    return _cache


def get(name):
    """Vrati profil podle jmena. Neznamy nazev spadne na vychozi."""
    profiles = load()
    if name and name in profiles:
        return profiles[name]
    return profiles.get(DEFAULT_NAME) or next(iter(profiles.values()))


def names():
    """Seznam profilu pro rozhrani: [{name, label, note}, ...]"""
    return [
        {"name": key, "label": p.get("label", key), "note": p.get("note", "")}
        for key, p in load().items()
    ]


def params(name):
    """Vrati normalizovane parametry profilu jako plochy slovnik."""
    p = get(name)
    weights = p.get("weights", {})
    return {
        "no_ranking": bool(p.get("no_ranking", False)),
        "prefer_side_pose": bool(p.get("prefer_side_pose", False)),
        "sharpness_floor": float(p.get("sharpness_floor", config.HARD_SHARPNESS_FLOOR)),
        "min_subject_area": float(p.get("min_subject_area", config.MIN_SUBJECT_AREA_RATIO)),
        "w_sharpness": float(weights.get("sharpness", 1.0)),
        "w_sharpness_mean": float(weights.get("sharpness_mean", 0.30)),
        "w_subject_size": float(weights.get("subject_size", 0.25)),
        "w_exposure": float(weights.get("exposure", 0.20)),
        "w_centering": float(weights.get("centering", 0.10)),
        "w_light": float(weights.get("light", 0.15)),
        "w_pose": float(weights.get("pose", 0.15)),
    }
