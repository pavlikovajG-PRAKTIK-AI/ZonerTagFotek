"""
WildSort - krok 3: detekce zvirete.

Najde v nahledu zvire a vrati jeho ohranicujici obdelnik. Diky tomu se
ostrost meri na SUBJEKTU, ne na celem snimku - to je zasadni rozdil,
protoze ostra trava v popredi a rozmazany gepard by jinak dostaly
vysoke skore.

MegaDetector je volitelny. Pokud model neni stazeny, modul se prepne do
rezimu bez detekce a metriky se pocitaji ze stredove oblasti snimku.
Pipeline tim neselze, jen bude mene presna.

DVE VECI, KTERE TU MUSI BYT NAROVNANE

(1) VERZE YOLOV5 SE PINUJE NA v7.0
    Vetev master dnes zacina radkem "from ultralytics.utils.patches import
    torch_load", takze si vynucuje cely balik ultralytics. Ten s sebou tahne
    opencv-python, ktery koliduje s opencv-python-headless z requirements -
    instalace pak spadne na zamcenem cv2.pyd. Tag v7.0 zadny ultralytics
    nepotrebuje a MegaDetector v5 je presne yolov5 checkpoint teto generace.

(2) torch.load SE MUSI ZAVOLAT S weights_only=False
    Od torch 2.6 je vychozi weights_only=True a odpickleni yolov5
    checkpointu tim selze. Model je vlastni soubor z oficialniho vydani
    MegaDetectoru, ktery si uzivatel stahl sam, takze plne odpickleni je
    v poradku. Patch plati jen po dobu nacitani.

A jedna vec navic: kdyz nacteni selze, ulozi se SKUTECNY DUVOD. Puvodni
hlaseni "nelze nacist (chybi torch?)" pri nainstalovanem torchi posilalo
hledani uplne spatnym smerem.
"""

import numpy as np

import config

_model = None
_model_state = "unloaded"   # unloaded | ready | unavailable
_error = None               # skutecny duvod, proc se model nenacetl

# Poradi, ve kterem se zkousi zdroj kodu yolov5. v7.0 je hlavni cesta,
# master zaloha pro pripad, ze by v7.0 pro nejakou verzi torche prestal jit.
_HUB_SOURCES = ("ultralytics/yolov5:v7.0", "ultralytics/yolov5")


def model_available():
    """Vrati True, pokud je soubor s modelem na disku."""
    return config.MEGADETECTOR_PATH.exists()


def _load_with(source):
    """Nacte model z jedne varianty zdroje. Vyhodi vyjimku pri selhani."""
    import torch

    original = torch.load
    try:
        def relaxed(*args, **kwargs):
            kwargs["weights_only"] = False
            return original(*args, **kwargs)

        torch.load = relaxed
        return torch.hub.load(source, "custom",
                              path=str(config.MEGADETECTOR_PATH),
                              trust_repo=True, verbose=False)
    finally:
        torch.load = original


def load_model():
    """Nacte MegaDetector. Vraci None, pokud neni dostupny."""
    global _model, _model_state, _error
    if _model_state in ("ready", "unavailable"):
        return _model

    if not model_available():
        _model_state = "unavailable"
        _error = f"soubor modelu nenalezen: {config.MEGADETECTOR_PATH}"
        return None

    try:
        import torch  # noqa: F401
    except ImportError as e:
        _model_state = "unavailable"
        _error = f"chybi PyTorch ({e}). Nainstaluj: pip install torch torchvision"
        return None

    problems = []
    for source in _HUB_SOURCES:
        try:
            model = _load_with(source)
        except Exception as e:
            problems.append(f"{source}: {type(e).__name__}: {e}")
            continue

        model.conf = config.EMPTY_FRAME_CONFIDENCE
        _model = model
        _model_state = "ready"
        _error = None
        return _model

    _model = None
    _model_state = "unavailable"
    _error = " | ".join(problems)
    return None


def last_error():
    """Skutecny duvod, proc se model nenacetl. None, kdyz je vse v poradku."""
    return _error


def _animals(results, index=0):
    """Vytahne detekce tridy 'zvire' z vysledku modelu."""
    boxes = (results.xyxyn[index].cpu().numpy()
             if len(results.xyxyn) > index else np.empty((0, 6)))
    # MegaDetector trida 0 = zvire, 1 = clovek, 2 = vozidlo
    return [b for b in boxes if int(b[5]) == 0]


def _tiled_retry(model, image_path):
    """Druhy pokus na dlazdicich 2x2 s presahem.

    Male zvire v siroke krajine zabira na 1600px nahledu jen par desitek
    pixelu - pod rozlisovaci schopnost modelu. Na ctvrtinovem vyrezu je
    dvakrat vetsi a detekce ho casto najde. Bezi jen u snimku, kde detekce
    na celku nic nenasla, takze cena je 4x detekce u ~10 % davky.

    Vraci nejlepsi nalez v souradnicich CELEHO snimku, nebo None.
    """
    import cv2

    img = cv2.imread(str(image_path))
    if img is None:
        return None
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = img.shape[:2]
    ov = config.TILE_OVERLAP

    tiles = []
    origins = []
    for gy in range(2):
        for gx in range(2):
            x1 = int(max(0, (gx * 0.5 - ov * 0.5) * w))
            y1 = int(max(0, (gy * 0.5 - ov * 0.5) * h))
            x2 = int(min(w, ((gx + 1) * 0.5 + ov * 0.5) * w))
            y2 = int(min(h, ((gy + 1) * 0.5 + ov * 0.5) * h))
            tiles.append(img[y1:y2, x1:x2])
            origins.append((x1, y1, x2 - x1, y2 - y1))

    results = model(tiles)

    best = None
    for i, (ox, oy, ow, oh) in enumerate(origins):
        for b in _animals(results, i):
            conf = float(b[4])
            if best is None or conf > best["conf"]:
                # prepocet z dlazdice do souradnic celeho snimku
                bx1 = (ox + float(b[0]) * ow) / w
                by1 = (oy + float(b[1]) * oh) / h
                bx2 = (ox + float(b[2]) * ow) / w
                by2 = (oy + float(b[3]) * oh) / h
                best = {"conf": conf, "x": bx1, "y": by1,
                        "w": bx2 - bx1, "h": by2 - by1}
    return best


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
    animals = _animals(results)

    if not animals:
        # Druha sance na dlazdicich - male zvire v siroke krajine
        if config.TILED_RETRY_EMPTY:
            found = _tiled_retry(model, image_path)
            if found and found["conf"] >= config.EMPTY_FRAME_CONFIDENCE:
                found.update(is_empty=0, fallback=False, tiled=True)
                return found
        return {"conf": 0.0, "x": 0.20, "y": 0.20, "w": 0.60, "h": 0.60,
                "is_empty": 1, "fallback": False}

    best = max(animals, key=lambda b: b[4])
    x1, y1, x2, y2, conf = (float(best[0]), float(best[1]), float(best[2]),
                            float(best[3]), float(best[4]))

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
    if not model_available():
        return "Bez detekce - metriky se pocitaji ze stredu snimku"

    load_model()
    if _model_state == "ready":
        return "MegaDetector aktivni"

    # Konkretni duvod, ne vseobecny dotaz na torch
    reason = _error or "neznamy duvod"
    if len(reason) > 160:
        reason = reason[:157] + "..."
    return f"MegaDetector nelze nacist - {reason}"
