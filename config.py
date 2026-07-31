"""
WildSort - centralni konfigurace.

Vsechny cesty jsou relativni k WORKSPACE_DIR, aby slo cely projekt
presunout nebo zalohovat bez rozbiti databaze.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Cesty
# ---------------------------------------------------------------------------

# Korenovy adresar projektu (kde lezi tento soubor)
BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Pracovni adresar (workspace)
# ---------------------------------------------------------------------------
#
# Workspace = databaze + nahledy. Muze byt na dvou mistech:
#
#   VEDLE PROGRAMU  (vychozi)  ...\ZonerTagFotek\workspace
#       Bezny pripad: fotky mam na internim disku a tridim na jednom stroji.
#
#   VEDLE FOTEK  (prenosny)    E:\Kena2026\_wildsort
#       Fotky jsou na prenosnem disku a trideni pokracuje na jinem pocitaci.
#       Databaze i nahledy cestuji s fotkami, takze druhy stroj nemusi nic
#       pocitat znovu - navaze presne tam, kde prvni skoncil.
#
# Nazev prenosne slozky. Podtrzitko na zacatku ji v Prizkumniku i v Zoneru
# drzi na zacatku vypisu a odlisuje ji od slozek scen (001, 002, ...).
PORTABLE_DIR_NAME = "_wildsort"

# Prenosny workspace se pozna podle tohoto souboru. Znaci "tady je projekt",
# takze pri pripojeni disku na druhem stroji staci ukazat na slozku s fotkami.
PORTABLE_MARKER = "wildsort-project.txt"


def default_workspace():
    """Vrati workspace, se kterym se ma startovat.

    Promenna prostredi ma prednost, aby se dal workspace vybrat z launcheru
    bez zasahu do kodu (WildSort.bat, prenosny disk).
    """
    env = os.environ.get("WILDSORT_WORKSPACE")
    if env:
        return Path(env)
    return BASE_DIR / "workspace"


WORKSPACE_DIR = default_workspace()

# Databaze
DB_PATH = WORKSPACE_DIR / "wildsort.db"

# Adresar s vygenerovanymi nahledy (proxy JPEG)
PROXY_DIR = WORKSPACE_DIR / "proxy"


def set_workspace(path):
    """Prepne workspace za behu.

    Vsechny moduly ctou cesty pres config.X (nikde neni "from config import
    DB_PATH"), takze prepsani tady plati okamzite pro celou aplikaci.
    """
    global WORKSPACE_DIR, DB_PATH, PROXY_DIR
    WORKSPACE_DIR = Path(path).expanduser().resolve()
    DB_PATH = WORKSPACE_DIR / "wildsort.db"
    PROXY_DIR = WORKSPACE_DIR / "proxy"
    ensure_dirs()
    return WORKSPACE_DIR


def portable_workspace_for(photo_folder):
    """Vrati cestu prenosneho workspace pro danou slozku s fotkami."""
    return Path(photo_folder).expanduser().resolve() / PORTABLE_DIR_NAME


def is_portable():
    """Lezi workspace u fotek (prenosny), nebo u programu?"""
    return WORKSPACE_DIR.name == PORTABLE_DIR_NAME


def project_root():
    """Slozka, ke ktere se vztahuji relativni cesty korenu importu.

    U prenosneho workspace je to slozka s fotkami (rodic _wildsort) - proto
    je jedno, jestli disk dostane na jednom pocitaci D: a na druhem E:.
    U workspace vedle programu nic takoveho neexistuje a cesty zustavaji
    absolutni.
    """
    return WORKSPACE_DIR.parent if is_portable() else None

# Adresar pro modely (MegaDetector apod.)
MODELS_DIR = BASE_DIR / "models"

# Cesta k MegaDetectoru. Pokud soubor neexistuje, detekce se preskoci
# a metriky se pocitaji z celeho snimku.
MEGADETECTOR_PATH = MODELS_DIR / "md_v5a.0.0.pt"

# ExifTool. Hleda se sam, protoze "musi byt na PATH" je nejcastejsi duvod,
# proc aplikace na cizim stroji nenabehne: rozbaleny exiftool.exe ve slozce
# Programs na PATH neni a uzivatel nema duvod tusit, ze ho tam ma pridat.
#
# Poradi hledani: promenna prostredi -> PATH -> obvykla mista rozbaleni.
# Kdyz se nic nenajde, zustane "exiftool" a chyba se ohlasi az pri pouziti
# (spravne - bez ExifToolu nelze cist EXIF, nahledy z RAWu ani zapisovat XMP).
def _find_exiftool():
    import shutil

    override = os.environ.get("WILDSORT_EXIFTOOL")
    if override and Path(override).is_file():
        return override

    found = shutil.which("exiftool")
    if found:
        return found

    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local) / "Programs" / "ExifTool" / "exiftool.exe",
        BASE_DIR / "exiftool" / "exiftool.exe",
        BASE_DIR / "exiftool.exe",
        Path(r"C:\Program Files\ExifTool\exiftool.exe"),
        Path(r"C:\Windows\exiftool.exe"),
    ]
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            pass
    return "exiftool"


EXIFTOOL_PATH = _find_exiftool()

# ---------------------------------------------------------------------------
# Vstupni soubory
# ---------------------------------------------------------------------------

RAW_EXTENSIONS = [".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".dng"]
JPEG_EXTENSIONS = [".jpg", ".jpeg"]
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS + JPEG_EXTENSIONS

# ---------------------------------------------------------------------------
# Nahledy
# ---------------------------------------------------------------------------

# Delsi strana nahledu v pixelech. 1600 staci na posouzeni ostrosti na
# obrazovce a drzi velikost okolo 250-400 kB na snimek.
PROXY_LONG_EDGE = 1600
PROXY_JPEG_QUALITY = 85

# Nahled pro mrizku (maly, rychly)
THUMB_LONG_EDGE = 400
THUMB_JPEG_QUALITY = 78

# ---------------------------------------------------------------------------
# Mereni ostrosti
# ---------------------------------------------------------------------------

# Merit ostrost na vyrezu subjektu z PLNEHO rozliseni, ne ze zmenseneho
# nahledu. Pri 1600 px vypada lehce chybne zaostreny snimek stejne jako
# presne zaostreny - prave ten rozdil, ktery uvnitr serie rozhoduje, se
# zmensenim zahodi. Detekce zvirete bezi dal na malem nahledu (rychla),
# meri se az na velkem vyrezu.
SHARPNESS_USE_FULLRES = True

# Vyrez subjektu se rozdeli na mrizku a bere se NEJOSTREJSI bunka, ne
# prumer. Prumer pres cele zvire je zavadejici: ostry bok a mekka hlava
# vyjdou jako "dobre". Ostry snimek ma aspon jedno velmi ostre misto,
# rozhybany nema ostre ani jedno.
SHARPNESS_GRID = 6

# Kolik nejostrejsich bunek zprumerovat (1 = ciste maximum). Vyssi cislo
# je odolnejsi vuci nahodnemu sumu v jedne bunce.
SHARPNESS_TOP_CELLS = 3

# ---------------------------------------------------------------------------
# Seskupovani do serii a scen
# ---------------------------------------------------------------------------

# Maximalni mezera mezi snimky (v sekundach), aby patrily do jedne serie.
#
# POZOR NA MALOU HODNOTU. Puvodni 2 s pocitaly s tim, ze serie vznika jen
# serioveho snimani. Kdyz ale fotograf mackne spust tri krat po sobe
# s odstupem tri a pet sekund na tehoz ptaka, rozpadne se to na tri serie
# po jednom snimku - a v serii o jednom snimku neni co porovnavat, takze
# vyber nejlepsiho ztraci smysl.
#
# Merene na 202 snimcich z Pantanalu (odstupy uvnitr scen a obrazova
# vzdalenost na stejnych prechodech):
#
#   odstup    prechodu   median obrazove vzdalenosti   totez zvire
#   do 2 s          85                         0.046        95 %
#   2 az 5 s        27                         0.146        88 %
#   5 az 12 s       14                         0.202        71 %
#   12 az 30 s      22                         0.464        36 %
#   nad 30 s        19                         0.567        26 %
#
# Do 12 s je obsah temer vzdy stejny, nad 12 s se rozchazi. Hranice 10 s
# tedy lezi bezpecne uvnitr oblasti, kde jde jeste o tentyz zaber.
BURST_GAP_SECONDS = 10.0

# Prah obrazove zmeny, ktery zacina novou serii UVNITR sceny. Je tesnejsi
# nez u scen: scena je "stejna situace", serie je "skoro stejny zaber,
# ktery ma smysl porovnavat mezi sebou". Kdyz pták preskoci na jinou
# vetev, je to porad ta sama scena, ale uz jiny zaber.
#
# Uvnitr serie se porovnava POUZE barevne slozeni, bez kompozice - viz
# content.distance(). Merene na skutecnych snimcich: dva zabery teze
# situace maji histogramovou vzdalenost do 0.24, jiny subjekt od 0.57.
# Prah 0.35 lezi mezi tim.
BURST_CONTENT_THRESHOLD = 0.35

# Serie delsi nez tento pocet snimku se rozdeli, aby se v UI dala prochazet.
MAX_BURST_SIZE = 40

# Mezera, ktera oddeluje SCENY. Lev u napajedla focený dvacet minut
# s pauzami da padesat serii tehoz lva. Scena je nadrazena uroven: uvnitr
# ni spolu soutezi jen vitezove jednotlivych serii.
SCENE_GAP_SECONDS = 180.0

# Sceny i serie se deli podle OBSAHU, ne jen podle casu. Kdyz fotograf za
# dve minuty otoci objektiv od capa v trave na ptaky na drate, je to podle
# casu jedna scena, ale ve skutecnosti dve uplne jine situace. Vypnutim se
# vrati puvodni chovani jen podle casu.
GROUP_BY_CONTENT = True

# Prah obrazove zmeny, ktery zacina novou scenu (viz content.py).
# NIZSI CISLO DELI OCHOTNEJI, VYSSI SLUCUJE.
#
# Merene na 202 snimcich z Pantanalu (import 20260719):
#
#   prah   scen   z toho o 1 snimku   median velikosti
#   0.45     53                  26                  2
#   0.55     47                  22                  2
#   0.65     37                  16                  2
#   0.75     26                   7                  6
#   0.85     18                   3                 10
#
# Rucni roztrideni jineho dne (20260720) dalo 16 slozek s medianem 10.
# Hodnota 0.75 lezi nejbliz tomu, jak sceny deli clovek: skutecne zmeny
# situace zachyti (prechod z trávy na oblohu meri 0.88 az 0.94), ale
# nerozpada se na desitky scen o jednom snimku.
SCENE_CONTENT_THRESHOLD = 0.75

# ---------------------------------------------------------------------------
# Souboj dvou nejlepsich
# ---------------------------------------------------------------------------

# Kdyz jsou prvni dva snimky serie blize nez tento relativni rozdil,
# algoritmus mezi nimi rozhodnout neumi a nema to predstirat. Serie se
# oznaci a rozhrani nabidne primy souboj vedle sebe.
DUEL_THRESHOLD = 0.05

# ---------------------------------------------------------------------------
# Automaticky navrh profilu z EXIF
# ---------------------------------------------------------------------------

# Zamer je casto zapsany uz v nastaveni fotoaparatu: 1/2000 s na 600 mm je
# pták v letu, 1/30 s na 400 mm je zamerny svenk. Systemu staci profil
# navrhnout, fotograf ho prepise, kdyz se netrefi.
AUTO_PROFILE = True

# ---------------------------------------------------------------------------
# Detekce zvirete
# ---------------------------------------------------------------------------

# Minimalni jistota detekce, aby se vyrez bral jako platny subjekt
DETECTION_CONFIDENCE_MIN = 0.30

# Snimky bez jakekoliv detekce nad timto prahem se oznaci jako "prazdne"
EMPTY_FRAME_CONFIDENCE = 0.20

# ---------------------------------------------------------------------------
# Hodnoceni
# ---------------------------------------------------------------------------

# Absolutni spodni hranice ostrosti (variance Laplaciánu na vyrezu subjektu).
# Slouzi POUZE k odfiltrovani uplnych zmetku, ne k vyberu nejlepsich.
HARD_SHARPNESS_FLOOR = 15.0

# Minimalni podil plochy subjektu na snimku. Pod timto je zvire prilis male.
MIN_SUBJECT_AREA_RATIO = 0.005

# Vahy pro vysledne skore v ramci serie (soucet nemusi byt 1)
SCORE_WEIGHTS = {
    "sharpness": 1.00,   # ostrost vyrezu subjektu - nejdulezitejsi
    "subject_size": 0.25,  # vetsi subjekt = obvykle lepsi zaber
    "exposure": 0.20,      # penalizace vypalenych svetel a zalitych stinu
    "centering": 0.10,     # subjekt uriznuty okrajem je horsi
}

# ---------------------------------------------------------------------------
# Jedinecnost hvezdicek v serii
# ---------------------------------------------------------------------------

# Z kazde serie ma AUTOMATIKA navrhnout prave jednu * a prave jednu **.
# Kdyz hvezdicku priradi jine fotce, ta predchozi se uvolni - jinak by
# v serii byly dve stejne a filtr v Zoneru by ukazal obe.
#
# Klic = prirazovane hodnoceni, hodnota = kam se preradi predchozi drzitel.
# Prazdny slovnik {} vynucovani vypne.
#
# Vychozi 3 hvezdicky znamenaji "ponechat, ale neni to vyber": neni to
# vyrazeni (to je 5) ani vyber (1 a 2). Snimek zustane k dispozici, kdyby
# se rozhodnuti jeste zmenilo.
UNIQUE_RATINGS = {1: 3, 2: 3}

# Vynucovat jedinecnost i pri RUCNIM hodnoceni?
#
# Vychozi NE. Kdyz fotograf vedome da jednu hvezdicku dvema snimkum teze
# serie, je to jeho rozhodnuti - treba proto, ze jsou dobre oba a chce si
# je oba nechat. Program mu do toho mluvit nema; jeho ukolem je navrhovat,
# ne prepisovat cloveka.
#
# True vrati chovani, kdy i rucni prirazeni hvezdicky uvolni predchoziho
# drzitele.
UNIQUE_RATINGS_MANUAL = False

# ---------------------------------------------------------------------------
# Zapis metadat
# ---------------------------------------------------------------------------

# Do RAW souboru se NIKDY nezapisuje. Vse jde do XMP sidecar souboru.
WRITE_SIDECAR_ONLY = True

# Klicova slova pridavana automaticky
KEYWORD_PICK = "WildSort:Pick"
KEYWORD_REJECT = "WildSort:Reject"
KEYWORD_EMPTY = "WildSort:Empty"

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8756

# Kolik snimku zpracovat najednou v jedne davce (setri pamet)
BATCH_SIZE = 32


def ensure_dirs():
    """Vytvori pracovni adresare, pokud jeste neexistuji."""
    for d in (WORKSPACE_DIR, PROXY_DIR, MODELS_DIR):
        os.makedirs(d, exist_ok=True)
