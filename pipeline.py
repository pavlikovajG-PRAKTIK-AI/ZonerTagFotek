"""
WildSort - rizeni celeho zpracovani.

Pipeline bezi na pozadi ve vlakne, aby webove rozhrani zustalo
responzivni. Stav se drzi v pameti i v databazi, takze po prerusení
(vypadek proudu, zavreni okna) staci spustit znovu - hotove kroky se
preskoci.

Poradi kroku:
  import -> nahledy -> detekce + metriky -> serie -> skore
"""

import threading
import traceback
from datetime import datetime

import config
import db
import detect
import grouping
import ingest
import metrics
import proxy
import scoring

# Stav bezici ulohy, cteny webovym rozhranim
JOB = {
    "running": False,
    "step": "",
    "done": 0,
    "total": 0,
    "message": "Pripraveno",
    "error": None,
    "finished_at": None,
}

_lock = threading.Lock()


def _set(**kwargs):
    with _lock:
        JOB.update(kwargs)


def _progress(step):
    def cb(done, total):
        _set(step=step, done=done, total=total)
    return cb


def analyze_step(root_id=None, progress=None):
    """Detekce zvirete + vypocet metrik. Nejpomalejsi krok pipeline.

    Zvire se HLEDA na malem nahledu (rychle), ale ostrost se MERI na
    vyrezu z plneho rozliseni. Docasny soubor se po kazdem snimku maze,
    takze na disku nepribyva nic navic.
    """
    with db.connect() as conn:
        where = "WHERE stage='proxied' AND proxy_path IS NOT NULL"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        rows = conn.execute(f"SELECT * FROM photos {where} ORDER BY id", params).fetchall()
        total = len(rows)

        for i, row in enumerate(rows, start=1):
            proxy_abs = config.PROXY_DIR / row["proxy_path"]
            temp_full = config.PROXY_DIR / f"{row['id']}_full.jpg"
            full_path = None
            is_temp = False

            try:
                box = detect.detect(proxy_abs)

                if config.SHARPNESS_USE_FULLRES:
                    src = db.absolute_photo_path(conn, row)
                    if src.exists():
                        full_path, is_temp = proxy.fullres_source(src, temp_full)

                m = metrics.analyze(proxy_abs, box, full_path)

                conn.execute(
                    """UPDATE photos SET
                        detection_conf=?, subject_x=?, subject_y=?, subject_w=?, subject_h=?,
                        is_empty=?, sharpness=?, sharpness_mean=?, sharpness_src=?,
                        exposure=?, clipped_high=?, clipped_low=?,
                        subject_area=?, edge_cut=?, light_asym=?, box_aspect=?,
                        content=?, stage='analyzed', error=NULL
                       WHERE id=?""",
                    (
                        box["conf"], box["x"], box["y"], box["w"], box["h"],
                        box["is_empty"], m["sharpness"], m["sharpness_mean"],
                        m["sharpness_src"], m["exposure"], m["clipped_high"],
                        m["clipped_low"], m["subject_area"], m["edge_cut"],
                        m["light_asym"], m["box_aspect"], m["content"], row["id"],
                    ),
                )
            except Exception as e:
                conn.execute(
                    "UPDATE photos SET stage='analyzed', error=? WHERE id=?",
                    (f"analyza: {e}", row["id"]),
                )
            finally:
                if is_temp and temp_full.exists():
                    temp_full.unlink()

            if progress and i % 5 == 0:
                progress(i, total)
            if i % 50 == 0:
                conn.commit()

    return {"analyzed": total}


def run_full(folder, label=None, root_id=None):
    """Spusti celou pipeline. Blokujici - volej pres start_background()."""
    db.init_db()

    _set(running=True, error=None, message="Import souboru", step="import")
    if folder:
        result = ingest.import_folder(folder, label=label, progress=_progress("import"))
        root_id = result["root_id"]
        msg = f"Importovano {result['added']} novych"
        if result["duplicates"]:
            msg += f", {result['duplicates']} duplicit oznaceno"
        _set(message=msg)

    _set(step="proxy", message="Generovani nahledu", done=0, total=0)
    proxy.run(root_id, progress=_progress("proxy"))

    _set(step="analyze", message=f"Analyza snimku ({detect.status()})", done=0, total=0)
    analyze_step(root_id, progress=_progress("analyze"))

    _set(step="grouping", message="Skladani scen a serii", done=0, total=0)
    g = grouping.run(root_id)

    _set(step="scoring", message="Vypocet skore", done=0, total=0)
    scoring.run(root_id)

    _set(running=False, step="done",
         message=f"Hotovo. {g['photos']} snimku, {g['bursts']} serii, "
                 f"{g['scenes']} scen.",
         finished_at=datetime.now().isoformat())
    return root_id


def start_background(folder=None, label=None, root_id=None):
    """Spusti pipeline ve vlakne. Vraci False, pokud uz neco bezi."""
    if JOB["running"]:
        return False

    def worker():
        try:
            run_full(folder, label, root_id)
        except Exception as e:
            _set(running=False, error=f"{e}\n{traceback.format_exc()}",
                 message="Zpracovani selhalo")

    threading.Thread(target=worker, daemon=True).start()
    return True


def job_status():
    with _lock:
        return dict(JOB)
