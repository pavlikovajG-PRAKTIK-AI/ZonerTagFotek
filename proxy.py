"""
WildSort - krok 2: nahledy (proxy).

Z kazdeho RAWu se vytahne VESTAVENY JPEG nahled pomoci exiftool.
Uplne dekodovani RAWu by u 10 000 snimku trvalo hodiny; vestaveny
nahled je hotovy za zlomek casu a na posouzeni ostrosti staci,
protoze fotoaparat ho generuje z plneho rozliseni.

Vznikaji dva soubory na fotku:
  - proxy: delsi strana 1600 px, pro detekci a detailni prohlizeni
  - thumb: delsi strana 400 px, pro mrizku v UI
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageOps

import config
import db


def extract_preview(src_path, dest_path):
    """Vytahne nejvetsi vestaveny nahled z RAWu do dest_path.

    Zkousi postupne JpgFromRaw, PreviewImage, ThumbnailImage - podle
    vyrobce je k dispozici jina znacka.
    """
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        cmd = [config.EXIFTOOL_PATH, "-b", tag, str(src_path)]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            continue
        if out.returncode == 0 and len(out.stdout) > 20000:
            with open(dest_path, "wb") as f:
                f.write(out.stdout)
            return True
    return False


def resize_to(src, dest, long_edge, quality):
    """Zmensi obrazek na zadanou delsi stranu a ulozi jako JPEG.
    Respektuje EXIF orientaci, aby snimky na vysku nebyly polozene."""
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        w, h = img.size
        scale = long_edge / max(w, h)
        if scale < 1:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img.save(dest, "JPEG", quality=quality, optimize=True)


def fullres_source(src_path, dest_path):
    """Pripravi zdroj v PLNEM rozliseni pro mereni ostrosti.

    U JPEGu je to primo original. U RAWu se vytahne vestaveny nahled,
    ktery fotoaparat generuje z plneho rozliseni.

    Vraci cestu k souboru, nebo None. Volajici je zodpovedny za smazani
    docasneho souboru - proto se vraci i priznak, zda je docasny.
    """
    src_path = Path(src_path)
    if src_path.suffix.lower() not in config.RAW_EXTENSIONS:
        return src_path, False
    if extract_preview(src_path, dest_path):
        return Path(dest_path), True
    return None, False


def build_proxy(conn, photo_row):
    """Vytvori proxy a thumb pro jednu fotku. Vraci (proxy_rel, thumb_rel)."""
    src = db.absolute_photo_path(conn, photo_row)
    photo_id = photo_row["id"]

    proxy_rel = f"{photo_id}_p.jpg"
    thumb_rel = f"{photo_id}_t.jpg"
    proxy_abs = config.PROXY_DIR / proxy_rel
    thumb_abs = config.PROXY_DIR / thumb_rel

    suffix = src.suffix.lower()
    temp = config.PROXY_DIR / f"{photo_id}_raw.jpg"

    if suffix in config.RAW_EXTENSIONS:
        if not extract_preview(src, temp):
            raise RuntimeError("V RAWu nebyl nalezen vestaveny nahled")
        source_for_resize = temp
    else:
        source_for_resize = src

    try:
        resize_to(source_for_resize, proxy_abs, config.PROXY_LONG_EDGE,
                  config.PROXY_JPEG_QUALITY)
        resize_to(proxy_abs, thumb_abs, config.THUMB_LONG_EDGE,
                  config.THUMB_JPEG_QUALITY)
    finally:
        if temp.exists():
            temp.unlink()

    return proxy_rel, thumb_rel


def run(root_id=None, progress=None):
    """Vytvori nahledy pro vsechny fotky ve stavu 'ingested'."""
    config.ensure_dirs()
    with db.connect() as conn:
        where = "WHERE stage='ingested'"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        rows = conn.execute(f"SELECT * FROM photos {where} ORDER BY id", params).fetchall()

        total = len(rows)
        for i, row in enumerate(rows, start=1):
            try:
                proxy_rel, thumb_rel = build_proxy(conn, row)
                conn.execute(
                    "UPDATE photos SET proxy_path=?, thumb_path=?, stage='proxied', error=NULL "
                    "WHERE id=?",
                    (proxy_rel, thumb_rel, row["id"]),
                )
            except Exception as e:
                conn.execute(
                    "UPDATE photos SET stage='proxied', error=? WHERE id=?",
                    (f"proxy: {e}", row["id"]),
                )
            if progress and i % 10 == 0:
                progress(i, total)
            if i % 50 == 0:
                conn.commit()

    return {"processed": total}
