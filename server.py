"""
WildSort - webovy server.

Bezi lokalne na 127.0.0.1, takze nic neopousti pocitac. Prohlizec je
pouze zobrazovaci vrstva; vsechna data zustavaji v SQLite a ve slozce
s fotkami.
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import calibration
import config
import db
import detect
import organize
import pipeline
import profiles
import scoring
import finalize
import xmp

app = FastAPI(title="WildSort")

WEB_DIR = Path(__file__).resolve().parent / "web"

# Sloupce, ktere se do prohlizece neposilaji. Obrazovy popis (content) je
# binarni blob pro rozpoznani scen - JSON ho neumi zakodovat a rozhrani ho
# k nicemu nepotrebuje. Kazdy radek fotky musi projit photo_json().
HIDDEN_PHOTO_COLUMNS = ("content",)


def photo_json(row):
    """Radek fotky pripraveny k odeslani do prohlizece."""
    data = dict(row)
    for column in HIDDEN_PHOTO_COLUMNS:
        data.pop(column, None)
    return data


# ---------------------------------------------------------------------------
# Modely pozadavku
# ---------------------------------------------------------------------------

class ImportRequest(BaseModel):
    folder: str
    label: str | None = None


class DecisionRequest(BaseModel):
    photo_id: int
    rating: int | None = None
    flag: str | None = None
    keywords: str | None = None


class ExportRequest(BaseModel):
    root_id: int | None = None
    only_reviewed: bool = True
    move_rejected: bool = False


class ProfileRequest(BaseModel):
    profile: str


class RescueRequest(BaseModel):
    photo_id: int
    rating: int = 2


class ResetRequest(BaseModel):
    burst_id: int | None = None
    root_id: int | None = None


# ---------------------------------------------------------------------------
# Staticke soubory a nahledy
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/image/{photo_id}")
def image(photo_id: int, size: str = "proxy"):
    """Vrati nahled fotky. size = proxy | thumb."""
    with db.connect() as conn:
        row = conn.execute("SELECT proxy_path, thumb_path FROM photos WHERE id=?",
                           (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fotka nenalezena")

    rel = row["thumb_path"] if size == "thumb" else row["proxy_path"]
    if not rel:
        raise HTTPException(404, "Nahled zatim neexistuje")

    path = config.PROXY_DIR / rel
    if not path.exists():
        raise HTTPException(404, "Soubor nahledu chybi")
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Zpracovani
# ---------------------------------------------------------------------------

# Schema se zaklada JEDNOU pri startu, ne pri kazdem dotazu na stav.
# Rozhrani se pta kazdych 1,5 s; opakovane spousteni CREATE/ALTER skriptu
# se zbytecne prida do fronty na zapis presne ve chvilich, kdy je databaze
# nejvytizenejsi (import, export).
db.init_db()


@app.get("/api/status")
def status():
    with db.connect() as conn:
        roots = [dict(r) for r in conn.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM photos p WHERE p.root_id=r.id) AS photo_count "
            "FROM roots r ORDER BY r.id DESC")]
        s = db.stats(conn)
    return {"job": pipeline.job_status(), "roots": roots, "stats": s,
            "detector": detect.status(), "workspace": workspace_info()}


def workspace_info():
    """Kde lezi databaze a nahledy.

    V rozhrani to musi byt videt: pri praci na dvou pocitacich je rozdil mezi
    "databaze u fotek na disku" a "databaze u programu" tim, co rozhoduje,
    jestli druhy stroj navaze, nebo pocita vsechno znovu.
    """
    return {
        "path": str(config.WORKSPACE_DIR),
        "portable": config.is_portable(),
        "project": str(config.project_root()) if config.project_root() else None,
    }


@app.post("/api/import")
def start_import(req: ImportRequest):
    folder = Path(req.folder).expanduser()
    if not folder.is_dir():
        raise HTTPException(400, f"Adresar neexistuje: {folder}")
    if not pipeline.start_background(str(folder), req.label):
        raise HTTPException(409, "Zpracovani uz bezi")
    return {"started": True}


@app.post("/api/reprocess/{root_id}")
def reprocess(root_id: int, deep: bool = False):
    """Zopakuje zpracovani bez noveho importu.

    deep=False  Jen preskupi serie a prepocita skore. Presne to, co je
                potreba po zmene vah v profiles.json - profily se ctou
                znovu, trva to sekundy a detekce se nesaha.

    deep=True   Zahodi vysledky detekce a metrik a spocita je znovu.
                Potreba jen po zmene metrik nebo prahu v config.py
                (SHARPNESS_*, DETECTION_*). Je to nejpomalejsi krok
                pipeline - u velkych davek zalezitost na hodiny.

    Rozhodnuti fotografa zustavaji v obou pripadech nedotcena. Novy
    import slozky by tohle NEudelal: import preskoci uz zname soubory,
    takze by se nic neprepocitalo.

    POZOR NA PORADI: kontrola bezici ulohy MUSI byt driv nez zapis do
    databaze. Bezici analyza databazi drzi, takze UPDATE by spadl na
    "database is locked" a uzivatel by misto hlasky "uz to bezi" dostal
    chybu 500.
    """
    if pipeline.job_status()["running"]:
        raise HTTPException(409, "Zpracovani uz bezi - pockej, az dobehne")

    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
            raise HTTPException(404, "Import nenalezen")
        if deep:
            # Zpet na 'proxied' - odtud analyze_step snimky znovu vezme.
            conn.execute(
                "UPDATE photos SET stage='proxied' "
                "WHERE root_id=? AND stage IN ('analyzed','scored')",
                (root_id,))

    if not pipeline.start_background(None, None, root_id):
        raise HTTPException(409, "Zpracovani uz bezi")
    return {"started": True, "deep": deep}


@app.post("/api/reset")
def reset_decisions(req: ResetRequest):
    """Zrusi hodnoceni - vrati snimky do stavu pred rozhodovanim.

    Maze hvezdicky, priznaky a razitko "videno", takze serie vypada jako
    ceve nactena. Navrh systemu (auto_rating) zustava - ten se rusi
    prepocitanim, ne timto.

    Maze i zapsana rozhodnuti v tabulce decisions. Je to zamer: kdyz je
    hodnoceni zruseno jako omyl, nema se z nej ucit ani kalibrace.
    """
    if not req.burst_id and not req.root_id:
        raise HTTPException(400, "Chybi burst_id nebo root_id")

    with db.connect() as conn:
        if req.burst_id:
            where, params = "burst_id=?", [req.burst_id]
            if not conn.execute("SELECT 1 FROM bursts WHERE id=?",
                                (req.burst_id,)).fetchone():
                raise HTTPException(404, "Serie nenalezena")
        else:
            where, params = "root_id=?", [req.root_id]
            if not conn.execute("SELECT 1 FROM roots WHERE id=?",
                                (req.root_id,)).fetchone():
                raise HTTPException(404, "Import nenalezen")

        photo_ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM photos WHERE {where}", params)]

        cleared = conn.execute(
            f"UPDATE photos SET rating=0, flag='', reviewed=0, rescued=0, "
            f"decided_at=NULL WHERE {where} AND "
            f"(rating!=0 OR flag!='' OR reviewed!=0 OR rescued!=0)", params
        ).rowcount

        for pid in photo_ids:
            conn.execute("DELETE FROM decisions WHERE photo_id=?", (pid,))

        if req.burst_id:
            conn.execute("UPDATE bursts SET reviewed=0 WHERE id=?", (req.burst_id,))
        else:
            conn.execute("UPDATE bursts SET reviewed=0 WHERE root_id=?", (req.root_id,))

    return {"ok": True, "cleared": cleared, "photos": len(photo_ids)}


# ---------------------------------------------------------------------------
# Prochazeni serii
# ---------------------------------------------------------------------------

@app.get("/api/bursts")
def bursts(root_id: int | None = None, unreviewed_only: bool = False):
    with db.connect() as conn:
        where, params = "WHERE 1=1", []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        if unreviewed_only:
            where += " AND reviewed=0"
        rows = conn.execute(
            f"SELECT * FROM bursts {where} ORDER BY start_time", params).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/burst/{burst_id}")
def burst_detail(burst_id: int):
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM bursts WHERE id=?", (burst_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Serie nenalezena")
        photos = conn.execute(
            "SELECT * FROM photos WHERE burst_id=? ORDER BY capture_time, filename",
            (burst_id,)).fetchall()
        return {"burst": dict(b), "photos": [photo_json(p) for p in photos]}


@app.post("/api/decision")
def decision(req: DecisionRequest):
    """Ulozi rozhodnuti fotografa. Zapisuje se jen do databaze -
    soubory na disku se dotkne az export."""
    now = datetime.now().isoformat()
    action = db.new_action()

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (req.photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Fotka nenalezena")

        updates, params = [], []
        rating = None
        if req.rating is not None:
            rating = max(0, min(5, req.rating))
            db.log_decision(conn, req.photo_id, "rating", row["rating"], rating,
                            at=now, action=action)
            updates.append("rating=?")
            params.append(rating)
        if req.flag is not None:
            db.log_decision(conn, req.photo_id, "flag", row["flag"], req.flag,
                            at=now, action=action)
            updates.append("flag=?")
            params.append(req.flag)
        if req.keywords is not None:
            db.log_decision(conn, req.photo_id, "keywords", row["keywords"],
                            req.keywords, at=now, action=action)
            updates.append("keywords=?")
            params.append(req.keywords)

        updates += ["reviewed=1", "decided_at=?"]
        params.append(now)
        params.append(req.photo_id)

        conn.execute(f"UPDATE photos SET {', '.join(updates)} WHERE id=?", params)

        # Rucni hodnoceni se do jedinecnosti hvezdicek NEPLETE: kdyz
        # fotograf vedome da * dvema snimkum serie, je to jeho rozhodnuti.
        # Vynucovani lze zapnout pres UNIQUE_RATINGS_MANUAL - shodne
        # action_id pak sváže obe zmeny do jedne akce, aby je Ctrl+Z
        # vratil najednou.
        demoted = []
        if rating is not None and config.UNIQUE_RATINGS_MANUAL:
            demoted = db.enforce_unique_rating(
                conn, row["burst_id"], rating, req.photo_id, at=now, action=action)

        # Serie je hotova, kdyz je videna kazda fotka v ni
        if row["burst_id"]:
            left = conn.execute(
                "SELECT COUNT(*) c FROM photos WHERE burst_id=? AND reviewed=0",
                (row["burst_id"],)).fetchone()["c"]
            if left == 0:
                conn.execute("UPDATE bursts SET reviewed=1 WHERE id=?", (row["burst_id"],))

    return {"ok": True, "demoted": demoted}


@app.post("/api/accept-burst/{burst_id}")
def accept_burst(burst_id: int):
    """Prijme navrh systemu pro celou serii. Hodnoceni je obracene
    (Zoner): nejlepsi snimek dostane 1 hvezdicku, druhy nejlepsi 2,
    vsechno ostatni 5 hvezdicek = k vymazani.
    Jedno stisknuti klavesy misto dvaceti.

    Prepisuje KAZDY snimek serie, takze z ni vzejde prave jedna * a prave
    jedna ** i bez zvlastniho uvolnovani hvezdicek. Rucni hodnoceni v teto
    serii se tim ovsem prepise - Enter je vedomy prikaz "vezmi svuj navrh".
    """
    with db.connect() as conn:
        photos = conn.execute("SELECT * FROM photos WHERE burst_id=?", (burst_id,)).fetchall()
        b = conn.execute("SELECT best_photo_id FROM bursts WHERE id=?", (burst_id,)).fetchone()
        best = b["best_photo_id"] if b else None
        now = datetime.now().isoformat()
        # Enter je jeden stisk, takze Ctrl+Z vrati celou serii najednou.
        action = db.new_action()

        for p in photos:
            if p["id"] == best:
                rating, flag = 1, "pick"
            elif (p["auto_rating"] or 0) == 2:
                rating, flag = 2, "pick"
            else:
                rating, flag = 5, "reject"
            db.log_decision(conn, p["id"], "rating", p["rating"], rating,
                            at=now, action=action)
            conn.execute(
                "UPDATE photos SET rating=?, flag=?, reviewed=1, decided_at=? WHERE id=?",
                (rating, flag, now, p["id"]))

        conn.execute("UPDATE bursts SET reviewed=1 WHERE id=?", (burst_id,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Roztrideni do slozek podle scen
# ---------------------------------------------------------------------------

@app.get("/api/organize/plan")
def organize_plan(root_id: int):
    """Co by presun udelal. Nic nemeni - slouzi k potvrzeni pred akci,
    ktera se jako jedina v celem systemu dotyka umisteni originalu."""
    result = organize.plan(root_id)
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


@app.post("/api/organize")
def organize_apply(req: ResetRequest):
    """Vytvori slozky 001, 002, ... a presune do nich snimky jednotlivych
    scen. Snimky, ktere uz v podslozce jsou, se nedotkne."""
    if not req.root_id:
        raise HTTPException(400, "Chybi root_id")
    result = organize.apply(req.root_id)
    if result.get("error"):
        raise HTTPException(404, result["error"])
    return result


# ---------------------------------------------------------------------------
# Profily hodnoceni
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
def list_profiles():
    """Seznam dostupnych profilu pro vyber v rozhrani."""
    return profiles.names()


@app.post("/api/burst/{burst_id}/profile")
def set_burst_profile(burst_id: int, req: ProfileRequest):
    """Priradi serii jiny profil a hned ji prepocita.

    Rozhodnuti fotografa zustavaji - meni se jen navrh a poradi.
    """
    available = {p["name"] for p in profiles.names()}
    if req.profile not in available:
        raise HTTPException(400, f"Neznamy profil: {req.profile}")

    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM bursts WHERE id=?", (burst_id,)).fetchone():
            raise HTTPException(404, "Serie nenalezena")

    return scoring.rescore_burst(burst_id, req.profile)


# ---------------------------------------------------------------------------
# Zachranny rezim - prohlidka zavrzenych
# ---------------------------------------------------------------------------

@app.get("/api/rejected")
def rejected(root_id: int | None = None, limit: int = 500, include_empty: bool = False):
    """Vrati zavrzene snimky serazene od NEJHORSIHO skore.

    Zamerne od nejhorsiho: prave tam konci zamerny rozmaz, silueta
    v protisvetle a detail, na kterem detektor zvire nenajde. Prochazeni
    od nejlepsiho by tyhle snimky nechalo az na konec, kam se nikdo
    nedostane.
    """
    with db.connect() as conn:
        where = "WHERE (flag='reject' OR auto_rating>=5) AND rescued=0"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        if not include_empty:
            where += " AND is_empty=0"

        rows = conn.execute(
            f"SELECT * FROM photos {where} ORDER BY score ASC, sharpness ASC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [photo_json(r) for r in rows]


@app.post("/api/rescue")
def rescue(req: RescueRequest):
    """Vytahne snimek zpet mezi vybrane."""
    from datetime import datetime as _dt

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (req.photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Fotka nenalezena")

        now = _dt.now().isoformat()
        rating = max(1, min(5, req.rating))
        action = db.new_action()

        db.log_decision(conn, req.photo_id, "rescue", row["flag"], "pick",
                        at=now, action=action)
        conn.execute(
            "UPDATE photos SET flag='pick', rating=?, rescued=1, reviewed=1, decided_at=? "
            "WHERE id=?",
            (rating, now, req.photo_id),
        )
        # Zachrana je rucni rozhodnuti, takze stejne pravidlo jako u nej.
        demoted = []
        if config.UNIQUE_RATINGS_MANUAL:
            demoted = db.enforce_unique_rating(
                conn, row["burst_id"], rating, req.photo_id, at=now, action=action)

    return {"ok": True, "demoted": demoted}


@app.post("/api/dismiss/{photo_id}")
def dismiss(photo_id: int):
    """Potvrdi, ze zavrzeny snimek je opravdu k nicemu.
    Zmizi ze zachranneho seznamu, ale nikam se nemaze."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE photos SET rescued=1, flag='reject', reviewed=1 WHERE id=?",
            (photo_id,),
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sceny - nadrazena uroven nad seriemi
# ---------------------------------------------------------------------------

@app.get("/api/scenes")
def scenes(root_id: int | None = None):
    """Seznam scen. Kazda scena je jedna situace (lev u napajedla),
    ktera se muze skladat z desitek serii."""
    with db.connect() as conn:
        where, params = "WHERE 1=1", []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        rows = conn.execute(
            f"SELECT * FROM scenes {where} ORDER BY start_time", params).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["reviewed"] = conn.execute(
                "SELECT COUNT(*) c FROM bursts WHERE scene_id=? AND reviewed=0",
                (r["id"],)).fetchone()["c"] == 0
            result.append(d)
        return result


@app.get("/api/scene/{scene_id}")
def scene_detail(scene_id: int):
    """Vitezove vsech serii ve scene, serazeni od nejlepsiho.

    Toto je nejrychlejsi cesta expedici: misto padesati serii tehoz lva
    vidis rovnou nejlepsi zaber cele situace.
    """
    with db.connect() as conn:
        scene = conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise HTTPException(404, "Scena nenalezena")

        winners = conn.execute(
            "SELECT p.*, b.id AS burst_id_ref, b.profile, b.photo_count AS burst_size "
            "FROM photos p JOIN bursts b ON b.best_photo_id = p.id "
            "WHERE b.scene_id=? ORDER BY p.scene_rank", (scene_id,)
        ).fetchall()
        return {"scene": dict(scene), "winners": [photo_json(w) for w in winners]}


# ---------------------------------------------------------------------------
# Souboj dvou nejlepsich
# ---------------------------------------------------------------------------

@app.get("/api/duel/{burst_id}")
def duel(burst_id: int):
    """Vrati dva nejlepsi snimky serie, kdyz jsou tak blizko, ze mezi nimi
    algoritmus rozhodnout neumi. Jinak vraci null."""
    with db.connect() as conn:
        b = conn.execute("SELECT duel_a, duel_b FROM bursts WHERE id=?",
                         (burst_id,)).fetchone()
        if not b or not b["duel_a"] or not b["duel_b"]:
            return {"duel": None}

        photos = conn.execute(
            "SELECT * FROM photos WHERE id IN (?,?)", (b["duel_a"], b["duel_b"])
        ).fetchall()
        by_id = {p["id"]: photo_json(p) for p in photos}
        return {"duel": {"a": by_id.get(b["duel_a"]), "b": by_id.get(b["duel_b"])}}


@app.post("/api/duel/{burst_id}/resolve/{photo_id}")
def resolve_duel(burst_id: int, photo_id: int):
    """Vyhlasi viteze souboje: dostane 1 hvezdicku (nejlepsi) a stane se
    vitezem serie, porazeny dostane 2 (druhy nejlepsi) - z kazde serie
    tak zustava jedna * a jedna **."""
    from datetime import datetime as _dt

    with db.connect() as conn:
        b = conn.execute("SELECT duel_a, duel_b FROM bursts WHERE id=?",
                         (burst_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Serie nenalezena")
        if photo_id not in (b["duel_a"], b["duel_b"]):
            raise HTTPException(400, "Snimek neni ucastnikem souboje")

        loser = b["duel_b"] if photo_id == b["duel_a"] else b["duel_a"]
        now = _dt.now().isoformat()
        action = db.new_action()

        for pid, rating, flag in ((photo_id, 1, "pick"), (loser, 2, "pick")):
            old = conn.execute("SELECT rating FROM photos WHERE id=?", (pid,)).fetchone()
            db.log_decision(conn, pid, "rating", old["rating"] if old else 0,
                            rating, at=now, action=action)
            conn.execute(
                "UPDATE photos SET rating=?, flag=?, reviewed=1, decided_at=? WHERE id=?",
                (rating, flag, now, pid))
            # Serie uz mohla mit * nebo ** z RUCNIHO hodnoceni. Uvolnit ji
            # smi jen tehdy, kdyz je vynucovani zapnute i pro rucni volby -
            # jinak by souboj potichu smazal hvezdicku, kterou tam dal
            # fotograf vedome.
            if config.UNIQUE_RATINGS_MANUAL:
                db.enforce_unique_rating(conn, burst_id, rating, pid,
                                         at=now, action=action)

        conn.execute(
            "UPDATE bursts SET best_photo_id=?, duel_a=NULL, duel_b=NULL WHERE id=?",
            (photo_id, burst_id))
        scoring.rank_scenes(conn)

    return {"ok": True, "winner": photo_id}


# ---------------------------------------------------------------------------
# Kalibrace a krok zpet
# ---------------------------------------------------------------------------

@app.get("/api/calibration")
def calibration_status(root_id: int | None = None):
    """Nakolik se shoduje navrh systemu s tvym vyberem."""
    return calibration.measure(root_id)


@app.post("/api/undo")
def undo():
    """Vrati posledni rozhodnuti."""
    with db.connect() as conn:
        result = db.undo_last(conn)
    if not result:
        return {"ok": False, "message": "Neni co vracet"}
    return {"ok": True, **result}


@app.get("/api/duplicates")
def duplicates(root_id: int | None = None):
    """Seznam nalezenych duplicit. Nezahazuji se tise - kdyz omylem
    naimportujes zalohu, chces to vedet."""
    with db.connect() as conn:
        where, params = "WHERE d.duplicate_of IS NOT NULL", []
        if root_id:
            where += " AND d.root_id=?"
            params.append(root_id)
        rows = conn.execute(
            f"SELECT d.id, d.filename AS copy, d.rel_path AS copy_path, "
            f"o.filename AS original, o.rel_path AS original_path "
            f"FROM photos d LEFT JOIN photos o ON o.id = d.duplicate_of {where} "
            f"ORDER BY d.filename", params
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Export do XMP
# ---------------------------------------------------------------------------

@app.post("/api/export")
def export(req: ExportRequest):
    """Spusti zapis do XMP NA POZADI a hned se vrati.

    Zapis tisice sidecaru trva minuty. Kdyby se na nej cekalo v odpovedi,
    uzivatel by u mlciciho tlacitka nemel jak poznat, kolik je hotovo -
    proto se postup hlasi do stavu ulohy a ukazuje na liste v hlavicce.
    """
    if not xmp.exiftool_available():
        raise HTTPException(
            400, "ExifTool nenalezen. Nainstaluj ho z exiftool.org nebo rozbal "
                 r"do %LOCALAPPDATA%\Programs\ExifTool.")
    if not pipeline.start_export(req.root_id, req.only_reviewed, req.move_rejected):
        raise HTTPException(409, "Jina uloha uz bezi - pockej, az dobehne")
    return {"started": True}


@app.post("/api/final-audit/{root_id}")
def final_audit(root_id: int):
    """Porovna finalni hvezdicky v XMP (po editaci v Zoneru) s navrhy
    systemu a prvnim tridenim. Cte sidecary, do XMP nic nezapisuje.

    Vysledek se uklada do photos.final_rating - to jsou trenovaci data
    pro budouci model osobniho vkusu: prvni dojem (rating) versus
    definitivni vysledek (final_rating).
    """
    if not xmp.exiftool_available():
        raise HTTPException(400, "ExifTool nenalezen.")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
            raise HTTPException(404, "Import nenalezen")
    result = finalize.audit(root_id)
    if result.get("error"):
        raise HTTPException(400, result["error"])
    return result


@app.get("/api/summary")
def summary(root_id: int | None = None):
    """Souhrn pro zaverecnou kontrolu pred exportem."""
    with db.connect() as conn:
        where, params = "WHERE 1=1", []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        total = conn.execute(f"SELECT COUNT(*) c FROM photos {where}", params).fetchone()["c"]
        picks = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND flag='pick'", params).fetchone()["c"]
        rejects = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND flag='reject'", params).fetchone()["c"]
        reviewed = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND reviewed=1", params).fetchone()["c"]
        empty = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND is_empty=1", params).fetchone()["c"]
        errors = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND error IS NOT NULL", params).fetchone()["c"]
        dupes = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND duplicate_of IS NOT NULL",
            params).fetchone()["c"]
        scenes_count = conn.execute(
            f"SELECT COUNT(*) c FROM scenes {'WHERE root_id=?' if root_id else ''}",
            params).fetchone()["c"]

        # Rozhodnuti, ktera jeste nejsou v souborech na disku. Bez tohoto
        # cisla nema fotograf jak poznat, ze v Zoneru koukа na stara data:
        # hvezdicky v rozhrani vidi, v XMP uz jsou jine. Presne tohle se
        # stalo: zapis probehl a hodnoceni prislo az potom.
        pending = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND reviewed=1 AND ("
            f"  exported_at IS NULL OR (decided_at IS NOT NULL "
            f"  AND decided_at > exported_at))", params).fetchone()["c"]
        exported = conn.execute(
            f"SELECT COUNT(*) c FROM photos {where} AND exported_at IS NOT NULL",
            params).fetchone()["c"]

    return {"total": total, "picks": picks, "rejects": rejects,
            "reviewed": reviewed, "empty": empty, "errors": errors,
            "duplicates": dupes, "scenes": scenes_count,
            "pending": pending, "exported": exported}
