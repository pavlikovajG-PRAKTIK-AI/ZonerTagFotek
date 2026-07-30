"""
WildSort - krok 4: metriky kvality.

Dve veci tady rozhoduji o pouzitelnosti celeho systemu:

(1) MERIT NA PLNEM ROZLISENI, NE NA ZMENSENEM NAHLEDU
    Pri 1600 px vypada lehce chybne zaostreny snimek stejne jako presne
    zaostreny. Prave ten rozdil ale uvnitr serie rozhoduje. Zvire se
    proto HLEDA na malem nahledu (rychle) a ostrost se MERI az na vyrezu
    z vestaveneho JPEGu v plnem rozliseni.

(2) BRAT NEJOSTREJSI MISTO, NE PRUMER
    Prumerna ostrost pres cele zvire je zavadejici: ostry bok a mekka
    hlava vyjdou jako "dobre", pritom je to zmetek. Vyrez se rozdeli na
    mrizku a bere se nekolik nejostrejsich bunek. Ostry snimek ma aspon
    jedno velmi ostre misto, rozhybany nema ostre ani jedno.
"""

import cv2
import numpy as np

import config


def crop_subject(img, box, padding=0.05):
    """Vyrizne oblast subjektu s malym okrajem."""
    h, w = img.shape[:2]
    x1 = int(max(0, (box["x"] - padding) * w))
    y1 = int(max(0, (box["y"] - padding) * h))
    x2 = int(min(w, (box["x"] + box["w"] + padding) * w))
    y2 = int(min(h, (box["y"] + box["h"] + padding) * h))
    if x2 - x1 < 24 or y2 - y1 < 24:
        return img
    return img[y1:y2, x1:x2]


def laplacian_variance(gray):
    """Klasicka variance Laplacianu. Vyssi cislo = ostrejsi."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def peak_sharpness(gray, grid=None, top_cells=None):
    """Ostrost nejostrejsiho mista vyrezu.

    Vyrez se rozdeli na mrizku grid x grid a v kazde bunce se spocita
    variance Laplacianu. Vysledkem je prumer nekolika nejvyssich hodnot.

    U divociny obvykle rozhoduje jedina vec - je ostre oko? Tahle metrika
    se tomu blizi mnohem vic nez prumer pres cele telo, a nepotrebuje
    zadny dalsi model.
    """
    grid = grid or config.SHARPNESS_GRID
    top_cells = top_cells or config.SHARPNESS_TOP_CELLS

    h, w = gray.shape[:2]
    if h < grid * 8 or w < grid * 8:
        # Prilis maly vyrez na deleni - vrat celkovou hodnotu
        return laplacian_variance(gray), laplacian_variance(gray)

    cell_h, cell_w = h // grid, w // grid
    values = []
    for gy in range(grid):
        for gx in range(grid):
            cell = gray[gy * cell_h:(gy + 1) * cell_h,
                        gx * cell_w:(gx + 1) * cell_w]
            if cell.size:
                values.append(laplacian_variance(cell))

    if not values:
        return laplacian_variance(gray), laplacian_variance(gray)

    values.sort(reverse=True)
    peak = float(np.mean(values[:max(1, top_cells)]))
    mean = float(np.mean(values))
    return peak, mean


def exposure_stats(gray):
    """Vrati (prumerny jas, podil vypalenych svetel, podil zalitych stinu)."""
    total = gray.size
    mean = float(np.mean(gray))
    clipped_high = float(np.count_nonzero(gray >= 252) / total)
    clipped_low = float(np.count_nonzero(gray <= 3) / total)
    return mean, clipped_high, clipped_low


def light_asymmetry(gray):
    """Nerovnomernost osvetleni subjektu: 0 = obe poloviny stejne,
    1 = jedna strana uplne ve stinu.

    Tvar osvetlena jen z poloviny je klasicka vada portretu zvirete.
    Merit primo oblicej neumime, ale u vyrezu subjektu se pulene svetlo
    projevi rozdilem jasu leve a prave poloviny - a uvnitr serie, kde
    je svetlo konstantni, vyhraje snimek s natocenim ke svetlu.
    """
    h, w = gray.shape[:2]
    if w < 16:
        return 0.0
    left = float(np.mean(gray[:, : w // 2]))
    right = float(np.mean(gray[:, w // 2:]))
    return abs(left - right) / max(left + right, 1.0)


def edge_cut(box):
    """Odhad, jak moc je subjekt uriznuty okrajem snimku.
    0 = cely v zaberu, 1 = tesne u okraje ze vsech stran."""
    left = max(0.0, 0.01 - box["x"])
    top = max(0.0, 0.01 - box["y"])
    right = max(0.0, (box["x"] + box["w"]) - 0.99)
    bottom = max(0.0, (box["y"] + box["h"]) - 0.99)
    return float(min(1.0, (left + top + right + bottom) * 25))


def analyze(proxy_path, box, fullres_path=None):
    """Spocita vsechny metriky pro jeden snimek.

    proxy_path   - zmenseny nahled, ze ktereho se bere expozice a kompozice
    box          - ohranicujici obdelnik subjektu (normalizovany 0-1)
    fullres_path - volitelny vestaveny JPEG v plnem rozliseni. Je-li zadan
                   a povoleny v config, ostrost se meri z nej.
    """
    img = cv2.imread(str(proxy_path))
    if img is None:
        raise ValueError(f"Nelze nacist nahled: {proxy_path}")

    crop = crop_subject(img, box)
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mean, high, low = exposure_stats(gray_crop)
    light_asym = light_asymmetry(gray_crop)

    # Pomer stran ramecku subjektu V PIXELECH (normalizovane souradnice
    # by u nectvercoveho snimku lhaly). Ptak z profilu je siroky, anfas
    # uzky - slouzi jako priblizny ukazatel natoceni.
    img_h, img_w = img.shape[:2]
    box_w_px = max(1.0, box["w"] * img_w)
    box_h_px = max(1.0, box["h"] * img_h)
    box_aspect = float(box_w_px / box_h_px)

    # Ostrost: prednostne z plneho rozliseni
    sharpness_src = "proxy"
    peak, avg = peak_sharpness(gray_crop)

    if fullres_path and config.SHARPNESS_USE_FULLRES:
        full = cv2.imread(str(fullres_path))
        if full is not None and full.shape[0] > img.shape[0]:
            full_crop = crop_subject(full, box)
            gray_full = cv2.cvtColor(full_crop, cv2.COLOR_BGR2GRAY)
            peak, avg = peak_sharpness(gray_full)
            sharpness_src = "full"

    return {
        "sharpness": peak,
        "sharpness_mean": avg,
        "sharpness_src": sharpness_src,
        "exposure": mean,
        "clipped_high": high,
        "clipped_low": low,
        "subject_area": float(box["w"] * box["h"]),
        "edge_cut": edge_cut(box),
        "light_asym": light_asym,
        "box_aspect": box_aspect,
    }
