"""
WildSort - databazova vrstva (SQLite).

Databaze je pamet celeho systemu. Kazdy krok pipeline zapisuje svuj
vysledek sem, takze zpracovani lze kdykoliv prerusit a navazat.

Cesty k souborum se ukladaji RELATIVNE k importovanemu korenu, aby
zmena pismene jednotky nebo presun slozky nerozbily databazi.
"""

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import config
import volume

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,   -- absolutni koren importu
    rel_to_ws    TEXT,                   -- cesta relativne ke slozce projektu
                                         -- (prenosny disk meni pismeno jednotky)
    vol_label    TEXT,                   -- nazev svazku ("KENA2026")
    vol_serial   TEXT,                   -- seriove cislo svazku
    drive_rel    TEXT,                   -- cesta od korene disku
    label        TEXT,                   -- napr. "Kena 2026 - karta 1"
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id),
    rel_path        TEXT NOT NULL,        -- cesta relativne ke koreni
    filename        TEXT NOT NULL,
    file_hash       TEXT,                 -- pro detekci duplicit
    duplicate_of    INTEGER,              -- id originalu, je-li to kopie
    file_size       INTEGER,
    capture_time    TEXT,                 -- ISO 8601
    camera          TEXT,
    lens            TEXT,
    iso             INTEGER,
    shutter         TEXT,
    aperture        REAL,
    focal_length    REAL,

    proxy_path      TEXT,                 -- relativne k PROXY_DIR
    thumb_path      TEXT,

    burst_id        INTEGER REFERENCES bursts(id),
    scene_id        INTEGER REFERENCES scenes(id),

    -- vysledky detekce
    detection_conf  REAL,
    subject_x       REAL,                 -- normalizovany bbox 0-1
    subject_y       REAL,
    subject_w       REAL,
    subject_h       REAL,
    species         TEXT,                 -- navrh druhu
    species_conf    REAL,

    -- metriky
    sharpness       REAL,                 -- nejostrejsi misto subjektu
    sharpness_mean  REAL,                 -- prumer pres subjekt, pro srovnani
    sharpness_src   TEXT,                 -- 'full' nebo 'proxy'
    exposure        REAL,
    clipped_high    REAL,
    clipped_low     REAL,
    subject_area    REAL,
    edge_cut        REAL,
    light_asym      REAL,                 -- nerovnomernost osvetleni subjektu 0-1
    box_aspect      REAL,                 -- pomer stran ramecku (pixely), natoceni
    box_src         TEXT,                 -- NULL/'detected' = z detektoru,
                                          -- 'inherited' = pujceny od souseda v serii
    content         BLOB,                 -- obrazovy popis pro rozpoznani scen

    score           REAL,                 -- relativni skore v ramci serie
    scene_rank      INTEGER,              -- poradi mezi vitezi serii ve scene
    auto_rating     INTEGER,              -- navrh systemu 0-5
    is_empty        INTEGER DEFAULT 0,    -- 1 = zadne zvire nenalezeno

    -- rozhodnuti fotografa
    rating          INTEGER DEFAULT 0,    -- 0-5 hvezdicek
    flag            TEXT DEFAULT '',      -- '', 'pick', 'reject'
    keywords        TEXT DEFAULT '',      -- carkami oddelene
    reviewed        INTEGER DEFAULT 0,    -- 1 = fotograf uz videl
    rescued         INTEGER DEFAULT 0,    -- 1 = vytazen zpet ze zavrzenych
    decided_at      TEXT,
    exported_at     TEXT,                 -- kdy naposled zapsano do XMP

    stage           TEXT DEFAULT 'ingested',  -- ingested|proxied|analyzed|scored
    error           TEXT,

    UNIQUE(root_id, rel_path)
);

CREATE TABLE IF NOT EXISTS scenes (
    id           INTEGER PRIMARY KEY,
    root_id      INTEGER NOT NULL REFERENCES roots(id),
    start_time   TEXT,
    end_time     TEXT,
    burst_count  INTEGER DEFAULT 0,
    photo_count  INTEGER DEFAULT 0,
    best_photo_id INTEGER,
    label        TEXT
);

CREATE TABLE IF NOT EXISTS bursts (
    id           INTEGER PRIMARY KEY,
    root_id      INTEGER NOT NULL REFERENCES roots(id),
    start_time   TEXT,
    end_time     TEXT,
    photo_count  INTEGER DEFAULT 0,
    best_photo_id INTEGER,
    reviewed     INTEGER DEFAULT 0,
    profile      TEXT DEFAULT 'standard',  -- profil hodnoceni teto serie
    auto_profile TEXT,                     -- co navrhl EXIF, pro srovnani
    scene_id     INTEGER REFERENCES scenes(id),
    duel_a       INTEGER,                  -- dva nejlepsi, kdyz jsou tesne
    duel_b       INTEGER
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    photo_id    INTEGER NOT NULL REFERENCES photos(id),
    field       TEXT NOT NULL,     -- rating | flag | keywords
    old_value   TEXT,
    new_value   TEXT,
    created_at  TEXT NOT NULL,
    action_id   TEXT               -- jeden stisk klavesy = jedno action_id
);

CREATE INDEX IF NOT EXISTS idx_photos_stage    ON photos(stage);
CREATE INDEX IF NOT EXISTS idx_photos_burst    ON photos(burst_id);
CREATE INDEX IF NOT EXISTS idx_photos_hash     ON photos(file_hash);
CREATE INDEX IF NOT EXISTS idx_photos_capture  ON photos(capture_time);
CREATE INDEX IF NOT EXISTS idx_bursts_root     ON bursts(root_id);
CREATE INDEX IF NOT EXISTS idx_bursts_scene    ON bursts(scene_id);
CREATE INDEX IF NOT EXISTS idx_photos_scene    ON photos(scene_id);
CREATE INDEX IF NOT EXISTS idx_photos_dup      ON photos(duplicate_of);
CREATE INDEX IF NOT EXISTS idx_decisions_photo ON decisions(photo_id);
"""


# Rezim zurnalu se voli podle toho, KDE databaze lezi - a pamatuje se,
# aby se PRAGMA nevyhodnocovalo pri kazdem spojeni.
#
#   WAL       vychozi; nejrychlejsi, ale drzi vedle databaze soubory
#             -wal a -shm. Na internim NTFS disku bez rizika.
#
#   TRUNCATE  pro PRENOSNY projekt na exFAT/FAT32. WAL se spoleha na
#             sdilenou pamet mapovanou do -shm souboru; exFAT nema zadny
#             zurnal souboroveho systemu, takze vytazeni disku ve spatne
#             chvili muze -wal soubor (a s nim posledni rozhodnuti)
#             zahodit. TRUNCATE je o neco pomalejsi na commit, ale po
#             kazdem commitu je vse v hlavnim souboru databaze.
_journal_cache = {}


def _journal_mode():
    key = str(config.WORKSPACE_DIR)
    mode = _journal_cache.get(key)
    if mode is None:
        mode = "WAL"
        if config.is_portable():
            info = volume.info(config.WORKSPACE_DIR) or {}
            if (info.get("fs") or "").upper() not in ("NTFS",):
                mode = "TRUNCATE"
        _journal_cache[key] = mode
    return mode


@contextmanager
def connect():
    """Otevre spojeni s databazi. Vraci sqlite3.Connection s row_factory."""
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    mode = _journal_mode()
    conn.execute(f"PRAGMA journal_mode={mode}")
    if mode == "WAL":
        # Ve WAL je synchronous=NORMAL bezpecne (commit prezije pad
        # aplikace; o poradi se stara WAL) a znatelne rychlejsi nez FULL.
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backup_db(reason="", keep=5):
    """Zkopiruje databazi do workspace/backup. Vraci cestu, nebo None.

    Vola se po dokoncenych krocich (import, export) - databaze ma ~3 MB,
    takze zaloha nic nestoji, ale nese VSECHNA rozhodnuti fotografa.
    Pouziva sqlite backup API, ktere je konzistentni i za behu.
    """
    from datetime import datetime

    if not config.DB_PATH.exists():
        return None
    backup_dir = config.WORKSPACE_DIR / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{reason}" if reason else ""
    dest = backup_dir / f"wildsort-{stamp}{suffix}.db"

    try:
        src = sqlite3.connect(str(config.DB_PATH), timeout=30)
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception:
        return None

    # Drz jen poslednich `keep` zaloh, at slozka neroste donekonecna
    existing = sorted(backup_dir.glob("wildsort-*.db"))
    for old in existing[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def init_db():
    """Vytvori schema, pokud jeste neexistuje, a dodela chybejici sloupce."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate(conn)


def migrate(conn):
    """Prida sloupce, ktere prisly az v novejsi verzi.

    Existujici databaze se tim neztrati - rozhodnuti fotografa zustanou.
    """
    root_cols = {r["name"] for r in conn.execute("PRAGMA table_info(roots)")}
    for col, ddl in (
        ("rel_to_ws", "ALTER TABLE roots ADD COLUMN rel_to_ws TEXT"),
        ("vol_label", "ALTER TABLE roots ADD COLUMN vol_label TEXT"),
        ("vol_serial", "ALTER TABLE roots ADD COLUMN vol_serial TEXT"),
        ("drive_rel", "ALTER TABLE roots ADD COLUMN drive_rel TEXT"),
    ):
        if col not in root_cols:
            conn.execute(ddl)

    existing = {r["name"] for r in conn.execute("PRAGMA table_info(bursts)")}
    if "profile" not in existing:
        conn.execute("ALTER TABLE bursts ADD COLUMN profile TEXT DEFAULT 'standard'")

    for col, ddl in (
        ("auto_profile", "ALTER TABLE bursts ADD COLUMN auto_profile TEXT"),
        ("scene_id", "ALTER TABLE bursts ADD COLUMN scene_id INTEGER"),
        ("duel_a", "ALTER TABLE bursts ADD COLUMN duel_a INTEGER"),
        ("duel_b", "ALTER TABLE bursts ADD COLUMN duel_b INTEGER"),
    ):
        if col not in existing:
            conn.execute(ddl)

    decision_cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
    if "action_id" not in decision_cols:
        conn.execute("ALTER TABLE decisions ADD COLUMN action_id TEXT")

    photo_cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    for col, ddl in (
        ("rescued", "ALTER TABLE photos ADD COLUMN rescued INTEGER DEFAULT 0"),
        ("duplicate_of", "ALTER TABLE photos ADD COLUMN duplicate_of INTEGER"),
        ("scene_id", "ALTER TABLE photos ADD COLUMN scene_id INTEGER"),
        ("sharpness_mean", "ALTER TABLE photos ADD COLUMN sharpness_mean REAL"),
        ("sharpness_src", "ALTER TABLE photos ADD COLUMN sharpness_src TEXT"),
        ("scene_rank", "ALTER TABLE photos ADD COLUMN scene_rank INTEGER"),
        ("light_asym", "ALTER TABLE photos ADD COLUMN light_asym REAL"),
        ("box_aspect", "ALTER TABLE photos ADD COLUMN box_aspect REAL"),
        ("box_src", "ALTER TABLE photos ADD COLUMN box_src TEXT"),
        ("final_rating", "ALTER TABLE photos ADD COLUMN final_rating INTEGER"),
        ("final_checked_at", "ALTER TABLE photos ADD COLUMN final_checked_at TEXT"),
        ("content", "ALTER TABLE photos ADD COLUMN content BLOB"),
        ("exported_at", "ALTER TABLE photos ADD COLUMN exported_at TEXT"),
    ):
        if col not in photo_cols:
            conn.execute(ddl)


def get_root_path(conn, root_id):
    """Vrati absolutni cestu ke koreni importu.

    PROC TO NENI JEN Path(row["path"])

    Prenosny disk dostane na kazdem pocitaci jine pismeno - na stolnim D:,
    na notebooku E:. Ulozena absolutni cesta "D:\\Kena2026" pak na notebooku
    neexistuje a rozbije se vsechno, co se souboru dotyka: nahledy, zapis
    XMP i roztrideni do slozek. Pritom slozka je na miste, jen jinak
    pojmenovana.

    Proto se u prenosneho projektu drzi i cesta RELATIVNI ke slozce projektu
    (rel_to_ws) a ta ma prednost. "." znamena "koren je prave slozka
    projektu". Absolutni cesta zustava jako zaloha a pro projekty vedle
    programu, kde se pismeno jednotky nemeni.
    """
    row = conn.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
    if not row:
        return None

    keys = row.keys()

    # 1) Relativne ke slozce projektu - u prenosneho projektu nejspolehlivejsi
    base = config.project_root()
    rel = row["rel_to_ws"] if "rel_to_ws" in keys else None
    if base is not None and rel:
        candidate = base if rel == "." else base / rel
        if candidate.is_dir():
            return candidate

    # 2) Ulozena absolutni cesta - plati, dokud se nezmeni pismeno jednotky
    absolute = Path(row["path"])
    if absolute.is_dir():
        return absolute

    # 3) Podle NAZVU A SERIOVEHO CISLA DISKU. Pismeno jednotky patri pocitaci,
    #    nazev svazku patri disku - po prenosu na jiny stroj je tohle ta
    #    informace, ktera jeste plati.
    if "vol_serial" in keys:
        found = volume.resolve(
            None,
            label=row["vol_label"] if "vol_label" in keys else None,
            serial=row["vol_serial"],
            relative_to_drive=row["drive_rel"] if "drive_rel" in keys else None,
        )
        if found is not None:
            return found

    # 4) Stejne pojmenovana slozka pod projektem (projekt se presunul jinam)
    if base is not None:
        by_name = base / absolute.name
        if by_name.is_dir():
            return by_name
        if base.is_dir():
            return base

    # Vrat absolutni cestu i kdyz neexistuje - volajici chybu ohlasi
    # s konkretni cestou, coz je lepsi nez None a pad na NoneType.
    return absolute


def relative_to_project(folder):
    """Vrati cestu slozky relativne ke slozce projektu, nebo None.

    None znamena "tento koren pod projektem nelezi" - pak se pouzije jen
    absolutni cesta.
    """
    base = config.project_root()
    if base is None:
        return None
    folder = Path(folder).expanduser().resolve()
    try:
        rel = folder.relative_to(base)
    except ValueError:
        return None
    return "." if str(rel) in ("", ".") else str(rel)


def root_location_fields(folder):
    """Vrati vsechny udaje o umisteni korenu, ktere se ukladaji.

    Ctyri zpusoby, jak tutez slozku najit, protoze kazdy prezije jinou
    zmenu: relativni cesta prezije zmenu pismene jednotky, nazev disku
    prezije i presun projektu, absolutni cesta je nejpresnejsi, dokud se
    nic nezmeni.
    """
    folder = Path(folder).expanduser().resolve()
    vol = volume.info(folder) or {}
    return {
        "path": str(folder),
        "rel_to_ws": relative_to_project(folder),
        "vol_label": vol.get("label"),
        "vol_serial": vol.get("serial"),
        "drive_rel": volume.relative_to_drive(folder),
    }


def refresh_root_locations(conn):
    """Po pripojeni disku na jinem pocitaci srovna ulozene udaje se skutecnymi.

    Bez toho by v databazi zustalo pismeno z predchoziho stroje a kazdy dalsi
    pristup k souboru by slozku dohledaval znovu.
    """
    fixed = 0
    for row in conn.execute("SELECT id, path FROM roots").fetchall():
        actual = get_root_path(conn, row["id"])
        if actual is None or not actual.is_dir():
            continue
        fields = root_location_fields(actual)
        if fields["path"] != row["path"]:
            conn.execute(
                "UPDATE roots SET path=?, rel_to_ws=?, vol_label=?, vol_serial=?, "
                "drive_rel=? WHERE id=?",
                (fields["path"], fields["rel_to_ws"], fields["vol_label"],
                 fields["vol_serial"], fields["drive_rel"], row["id"]))
            fixed += 1
        else:
            # Cesta plati, ale identita disku mohla chybet (stara databaze)
            conn.execute(
                "UPDATE roots SET rel_to_ws=?, vol_label=?, vol_serial=?, drive_rel=? "
                "WHERE id=? AND (vol_serial IS NULL OR rel_to_ws IS NOT ?)",
                (fields["rel_to_ws"], fields["vol_label"], fields["vol_serial"],
                 fields["drive_rel"], row["id"], fields["rel_to_ws"]))
    return fixed


def absolute_photo_path(conn, photo_row):
    """Slozi absolutni cestu k originalnimu souboru fotky."""
    root = get_root_path(conn, photo_row["root_id"])
    return root / photo_row["rel_path"]


def new_action():
    """Vrati identifikator jedne akce fotografa (jeden stisk klavesy).

    Zamerne NENI to casovy udaj: hodiny ve Windows maji hrube rozliseni
    a dva ruzne stisky mohou dostat stejny cas na mikrosekundu. Krok zpet
    by pak vratil i rozhodnuti, ktera k akci nepatri - u serie o dvaceti
    snimcich klidne celou serii.
    """
    return uuid.uuid4().hex


def log_decision(conn, photo_id, field, old_value, new_value, at=None, action=None):
    """Zaznamena rozhodnuti fotografa. Slouzi ke kroku zpet a jako
    trenovaci data pro budouci model osobniho vkusu.

    Zapisy se stejnym `action` patri k JEDNOMU stisku klavesy a krok zpet
    je vraci najednou. Jeden stisk muze zmenit vic fotek: prirazeni
    hvezdicky preradi jejiho predchoziho drzitele.
    """
    from datetime import datetime
    conn.execute(
        "INSERT INTO decisions "
        "(photo_id, field, old_value, new_value, created_at, action_id) "
        "VALUES (?,?,?,?,?,?)",
        (photo_id, field, str(old_value), str(new_value),
         at or datetime.now().isoformat(), action),
    )


def enforce_unique_rating(conn, burst_id, rating, keep_photo_id, at=None, action=None):
    """Uvolni hvezdicku v serii: ostatni fotky se stejnym hodnocenim
    preradi podle config.UNIQUE_RATINGS.

    Z kazde serie ma vzejit prave jedna * a prave jedna **. Bez tohoto
    kroku by prirazeni hvezdicky jine fotce nechalo predchozi drzitele
    na miste a v serii by byly dve stejne - filtr v Zoneru by ukazal obe.

    Vraci seznam {"photo_id", "rating"} preradenych snimku, aby rozhrani
    mohlo rict, co se stalo, a prekreslit je.
    """
    target = config.UNIQUE_RATINGS.get(rating)
    if target is None or not burst_id:
        return []

    rows = conn.execute(
        "SELECT id, rating FROM photos WHERE burst_id=? AND rating=? AND id!=?",
        (burst_id, rating, keep_photo_id),
    ).fetchall()

    moved = []
    for row in rows:
        log_decision(conn, row["id"], "rating", row["rating"], target,
                     at=at, action=action)
        conn.execute("UPDATE photos SET rating=? WHERE id=?", (target, row["id"]))
        moved.append({"photo_id": row["id"], "rating": target})
    return moved


# Pole, ktera lze vratit zpet, a jak se prevede ulozena hodnota
_UNDOABLE = {
    "rating": lambda v: int(v) if str(v).lstrip("-").isdigit() else 0,
    "flag": lambda v: "" if v in ("None", "none", None) else str(v),
    "keywords": lambda v: "" if v in ("None", "none", None) else str(v),
    "rescue": lambda v: "" if v in ("None", "none", None) else str(v),
}


def undo_last(conn):
    """Vrati posledni rozhodnuti fotografa.

    Jeden stisk klavesy zapise vic zaznamu, a nemusi jit ani o jednu
    fotku: prirazeni hvezdicky zapise zmenu u ni a zaroven preradi
    predchoziho drzitele te hvezdicky. Vraci se proto cela SKUPINA
    navazujicich zaznamu, ktere patri k jedne akci - jinak by jeden omyl
    vyzadoval nekolik stisknuti Ctrl+Z a mezistav by byl nesmyslny.

    Skupina se pozna podle action_id, ktere zapisy z jednoho stisku
    dostanou shodne. Starsi zaznamy bez action_id (z verze pred timto
    sloupcem) se resi zaloznim pravidlem: navazujici id u tehoz snimku.

    Pri rytmu jednoho stisku za vterinu je omyl otazkou casu, a bez
    kroku zpet znamena hledat, ktera fotka to vlastne byla.
    """
    FIELDS = "('rating','flag','keywords','rescue')"

    last = conn.execute(
        f"SELECT * FROM decisions WHERE field IN {FIELDS} "
        f"ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not last:
        return None

    if last["action_id"]:
        action = conn.execute(
            f"SELECT * FROM decisions WHERE action_id=? AND field IN {FIELDS} "
            f"ORDER BY id DESC",
            (last["action_id"],),
        ).fetchall()
    else:
        # Zaloha pro zaznamy z drivejsi verze: navazujici id u tehoz snimku
        group = conn.execute(
            f"SELECT * FROM decisions WHERE photo_id=? AND id<=? "
            f"AND field IN {FIELDS} ORDER BY id DESC",
            (last["photo_id"], last["id"]),
        ).fetchall()
        action = []
        expected_id = last["id"]
        for row in group:
            if row["id"] != expected_id:
                break
            action.append(row)
            expected_id -= 1

    photo = conn.execute("SELECT filename, burst_id FROM photos WHERE id=?",
                         (last["photo_id"],)).fetchone()
    restored = {}
    touched = []

    for row in action:
        field = row["field"]
        old_value = _UNDOABLE[field](row["old_value"])

        if field == "rescue":
            # Zachrana se vraci tak, ze snimek jde zpet mezi zavrzene
            conn.execute(
                "UPDATE photos SET flag=?, rescued=0, rating=0 WHERE id=?",
                (old_value or "reject", row["photo_id"]),
            )
        else:
            conn.execute(f"UPDATE photos SET {field}=? WHERE id=?",
                         (old_value, row["photo_id"]))
        if row["photo_id"] == last["photo_id"]:
            restored[field] = old_value
        if row["photo_id"] not in touched:
            touched.append(row["photo_id"])
        conn.execute("DELETE FROM decisions WHERE id=?", (row["id"],))

    # Snimek uz neni "vyrizeny", pokud po nem nezustalo zadne jine rozhodnuti
    for pid in touched:
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM decisions WHERE photo_id=?", (pid,)
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute("UPDATE photos SET reviewed=0, decided_at=NULL WHERE id=?",
                         (pid,))
        burst = conn.execute("SELECT burst_id FROM photos WHERE id=?",
                             (pid,)).fetchone()
        if burst and burst["burst_id"]:
            conn.execute("UPDATE bursts SET reviewed=0 WHERE id=?",
                         (burst["burst_id"],))

    return {
        "photo_id": last["photo_id"],
        "filename": photo["filename"] if photo else None,
        "burst_id": photo["burst_id"] if photo else None,
        "restored": restored,
        "photos": touched,
    }


def stats(conn, root_id=None):
    """Vrati souhrn stavu zpracovani."""
    where = "WHERE root_id=?" if root_id else ""
    params = (root_id,) if root_id else ()
    total = conn.execute(f"SELECT COUNT(*) c FROM photos {where}", params).fetchone()["c"]
    by_stage = {
        r["stage"]: r["c"]
        for r in conn.execute(
            f"SELECT stage, COUNT(*) c FROM photos {where} GROUP BY stage", params
        )
    }
    reviewed = conn.execute(
        f"SELECT COUNT(*) c FROM photos {where + (' AND' if where else 'WHERE')} reviewed=1",
        params,
    ).fetchone()["c"]
    return {"total": total, "by_stage": by_stage, "reviewed": reviewed}
