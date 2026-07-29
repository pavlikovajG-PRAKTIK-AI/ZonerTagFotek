"""
WildSort - krok 3: detekce zvirete.

Najde v nahledu zvire a vrati jeho ohranicujici obdelnik. Diky tomu se
ostrost meri na SUBJEKTU, ne na celem snimku - to je zasadni rozdil,
protoze ostra trava v popredi a rozmazany gepard by jinak dostaly
vysoke skore.

MegaDetector je volitelny. Pokud model neni stazeny, modul se prepne do
rezimu bez detekce a metriky se pocitaji ze stredove oblasti snimku.
Pipeline tim neselze, jen bude mene presna.
"""

import numpy as np

import config

_model = None
_model_state = "unloaded"   # unloaded | ready | unavailable


def model_available():
    """Vrati True, pokud je MegaDetector k dispozici."""
    return config.MEGADETECTOR_PATH.exists()


def load_model():
    """Nacte MegaDetector. Vraci None, pokud neni dostupny."""
    global _model, _model_state
    if _model_state in ("ready", "unavailable"):
        return _model

    if not model_available():
        _model_state = "unavailable"
        return None

    try:
        import torch
        _model = torch.hub.load(
            "ultralytics/yolov5", "custom",
            path=str(config.MEGADETECTOR_PATH), trust_repo=True,
        )
        _model.conf = config.EMPTY_FRAME_CONFIDENCE
        _model_state = "ready"
    except Exception:
        _model = None
        _model_state = "unavailable"
    return _model


def detect(image_path):
    """Vrati nejjistejsi detekci zvirete jako slovnik.

    Klice: conf, x, y, w, h (normalizovane 0-1), is_empty.
    Pokud model neni dostupny, vraci stredovy vyrez s conf=None.
    """
    model = load_model()

    if model is None:
        # Zaloha bez modelu: stredovych 60 % snimku. Neni to detekce,
        # jen rozumny predpoklad, ze subjekt je zhruba uprostred.
        return {"conf": None, "x": 0.20, "y": 0.20, "w": 0.60, "h": 0.60,
                "is_empty": 0, "fallback": True}

    results = model(str(image_path))
    boxes = results.xyxyn[0].cpu().numpy() if len(results.xyxyn) else np.empty((0, 6))

    # MegaDetector trida 0 = zvire, 1 = clovek, 2 = vozidlo
    animals = [b for b in boxes if int(b[5]) == 0]

    if not animals:
        return {"conf": 0.0, "x": 0.20, "y": 0.20, "w": 0.60, "h": 0.60,
                "is_empty": 1, "fallback": False}

    best = max(animals, key=lambda b: b[4])
    x1, y1, x2, y2, conf = float(best[0]), float(best[1]), float(best[2]), float(best[3]), float(best[4])

    return {
        "conf": conf,
        "x": max(0.0, x1),
        "y": max(0.0, y1),
        "w": min(1.0, x2) - max(0.0, x1),
        "h": min(1.0, y2) - max(0.0, y1),
        "is_empty": 1 if conf < config.EMPTY_FRAME_CONFIDENCE else 0,
        "fallback": False,
    }


def status():
    """Textovy stav detektoru pro zobrazeni v UI."""
    if model_available():
        load_model()
        if _model_state == "ready":
            return "MegaDetector aktivni"
        return "MegaDetector nalezen, ale nelze nacist (chybi torch?)"
    return "Bez detekce - metriky se pocitaji ze stredu snimku"
