"""
WildSort - obrazovy popis snimku pro rozpoznani scen.

PROC TO EXISTUJE

Cas sam scenu neurci. Kdyz fotograf za dve minuty otoci objektiv od capa
v trave na ptaky na drate, je to podle casu jedna scena, ale podle obsahu
dve uplne jine situace. Roztrideni podle casu pak slepi snimky, ktere
spolu nemaji nic spolecneho, a vyber nejlepsiho uvnitr takove skupiny
nema smysl.

CO SE MERI

Popis je zamerne hruby - nema poznat druh zvirete, ma poznat ZMENU
SITUACE:

  (1) HSV histogram celeho snimku
      Zelena trava, modra obloha, hneda savana. Nejsilnejsi signal:
      prechod z trávy na oblohu je skokova zmena.

  (2) Mrizka 4x4 prumernych barev (Lab)
      Kde je co. Odlisi dve zelene scény s jinou kompozici - vetev
      vlevo nahore versus zvire uprostred.

Histogram sam nestaci, protoze dve ruzne situace ve stejnem prostredi
maji podobne barvy. Mrizka sama nestaci, protoze se meni uz tim, ze se
zvire v zaberu posune. Dohromady drzi obe slabiny na uzde: v mereni na
skutecnych snimcich davaji souvisle zabery vzdalenost do 0.25, zmena
situace nad 0.6.

ZADNY DALSI MODEL

Zamerne se nepouziva neuronova sit na priznaky. MegaDetector uz na disku
je, ale rozliseni "cap versus drobny pevec" by chtelo dalsi model,
dalsi stahovani a delsi vypocet. Pro rozhodnuti "je to jina situace?"
staci barvy a kompozice, a pocita se to v jednotkach milisekund
z nahledu, ktery uz existuje.
"""

import cv2
import numpy as np

# Pocet kosu HSV histogramu. 8x8x8 = 512 hodnot; hrubsi deleni uz plete
# zelenou travu s hnedym rakosim.
HSV_BINS = (8, 8, 8)

# Deleni snimku pro prumerne barvy. 4x4 = 16 bunek po 3 hodnotach.
GRID = 4

# Vahy slozek ve vysledne vzdalenosti
W_HIST = 0.6
W_GRID = 0.4

_HIST_LEN = HSV_BINS[0] * HSV_BINS[1] * HSV_BINS[2]
_GRID_LEN = GRID * GRID * 3
VECTOR_LEN = _HIST_LEN + _GRID_LEN


def _hsv_hist(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, list(HSV_BINS),
                        [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten()


def _grid_color(img):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    h, w = lab.shape[:2]
    cells = []
    for gy in range(GRID):
        for gx in range(GRID):
            cell = lab[gy * h // GRID:(gy + 1) * h // GRID,
                       gx * w // GRID:(gx + 1) * w // GRID]
            if cell.size:
                cells.append(cell.reshape(-1, 3).mean(axis=0))
            else:
                cells.append(np.zeros(3, dtype=np.float32))
    return (np.concatenate(cells) / 255.0).astype(np.float32)


def describe(img):
    """Spocita popis snimku. Vraci pole float32 delky VECTOR_LEN."""
    if img is None or img.size == 0:
        return None
    # Zmenseni na jednotnou velikost: popis nesmi zalezet na tom, jestli
    # prisel z nahledu 400 px nebo 1600 px.
    small = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
    return np.concatenate([_hsv_hist(small), _grid_color(small)]).astype(np.float32)


def to_blob(vector):
    """Do databaze jako binarni blob. float16 staci - popis je hruby
    a plna presnost by databazi zvetsila na dvojnasobek pro nic."""
    if vector is None:
        return None
    return np.asarray(vector, dtype=np.float16).tobytes()


def from_blob(blob):
    if not blob:
        return None
    vector = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
    if vector.size != VECTOR_LEN:
        return None
    return vector


def distance(a, b, grid_weight=W_GRID):
    """Vzdalenost dvou popisu: 0 = shodne, 1 = uplne jina situace.

    grid_weight urcuje, kolik vahy dostane KOMPOZICE (mrizka prumernych
    barev) vedle barevneho slozeni (histogram):

      pro SCENY  vychozi vaha. Jina kompozice je legitimni znamka jine
                 situace - vetev vlevo nahore versus zvire uprostred.

      pro SERIE  grid_weight=0. Uvnitr serie se zvire hybe po zaberu
                 a mrizka se tim meni dramaticky, i kdyz je to porad ten
                 samy pták v teze poze. Merene: mezi dvema zabery teze
                 situace byla vzdalenost mrizky 0.52, zatimco histogram
                 0.06. Kompozice by tu delila serie na jednotlive snimky,
                 ve kterych uz neni co porovnavat.

    Vraci None, kdyz jeden z popisu chybi - volajici pak musi rozhodnout
    bez obsahu (typicky podle casu).
    """
    if a is None or b is None:
        return None

    hist_a, grid_a = a[:_HIST_LEN], a[_HIST_LEN:]
    hist_b, grid_b = b[:_HIST_LEN], b[_HIST_LEN:]

    # Bhattacharyya vraci 0 az 1 a na histogramy je citlivejsi nez
    # korelace, ktera u prevazujici jedne barvy saturuje.
    d_hist = float(cv2.compareHist(hist_a.reshape(-1, 1), hist_b.reshape(-1, 1),
                                   cv2.HISTCMP_BHATTACHARYYA))

    if grid_weight <= 0:
        return d_hist

    d_grid = min(1.0, float(np.linalg.norm(grid_a - grid_b)))
    total = W_HIST + grid_weight
    return (W_HIST * d_hist + grid_weight * d_grid) / total
