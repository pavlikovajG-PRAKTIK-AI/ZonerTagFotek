"""
WildSort - krok 7: zapis metadat pro Zoner.

Do RAW souboru se NIKDY nezapisuje. Vse jde do XMP sidecar souboru
vedle originalu (IMG_1234.CR3 -> IMG_1234.xmp). Zoner Photo Studio i
ostatni bezne prohlizece tyto soubory ctou.

Sidecar se zaklada jinym prikazem, nez se aktualizuje - exiftool neumi
zapsat do souboru, ktery jeste neexistuje. Obe varianty resi
write_metadata().
"""

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import config
import db


def exiftool_available():
    try:
        out = subprocess.run([config.EXIFTOOL_PATH, "-ver"],
                             capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except Exception:
        return False


def sidecar_path(photo_path):
    """Vrati cestu k XMP sidecaru pro dany soubor."""
    return Path(photo_path).with_suffix(".xmp")


def _tag_args(rating, keywords, label):
    args = []
    if rating is not None:
        args.append(f"-XMP-xmp:Rating={max(0, min(5, int(rating)))}")
    if keywords:
        # Nejdriv odstranime jen sve vlastni znacky, uzivatelska zustanou
        for own in (config.KEYWORD_PICK, config.KEYWORD_REJECT, config.KEYWORD_EMPTY):
            args.append(f"-XMP-dc:Subject-={own}")
        for kw in keywords:
            args.append(f"-XMP-dc:Subject+={kw}")
    if label:
        args.append(f"-XMP-xmp:Label={label}")
    return args


def write_metadata(photo_path, rating=None, keywords=None, label=None):
    """Zapise hodnoceni a klicova slova do XMP sidecaru.

    rating   - 0 az 5 hvezdicek
    keywords - seznam retezcu
    label    - barevny stitek (napr. "Red", "Green")
    """
    photo_path = Path(photo_path)
    is_raw = photo_path.suffix.lower() in config.RAW_EXTENSIONS
    use_sidecar = is_raw or config.WRITE_SIDECAR_ONLY

    tags = _tag_args(rating, keywords, label)
    if not tags:
        return True

    def run(cmd):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "exiftool selhal")

    if not use_sidecar:
        # JPEG: zapis primo do souboru
        run([config.EXIFTOOL_PATH, "-overwrite_original", "-P"] + tags
            + [str(photo_path)])
        return True

    side = sidecar_path(photo_path)

    # ZAKLADANI A ZAPIS MUSI BYT DVA KROKY.
    #
    # Jedinym prikazem to nejde: pri "exiftool -o side.xmp -XMP:Rating=1 foto.CR3"
    # exiftool prevezme metadata z RAWu a prirazene hodnoty se v nich ztrati -
    # vysledny sidecar ma Rating 0. Chyba je zradna tim, ze prikaz projde bez
    # jakekoliv hlasky a soubor vznikne, takze se pozna teprve v Zoneru,
    # kde nejsou hvezdicky.
    #
    # Nejdriv se tedy sidecar zalozi jako kopie metadat z originalu a teprve
    # potom se do NEHO zapisou hodnoty - tam uz zadne kopirovani neprobiha
    # a prirazeni plati.
    if not side.exists():
        run([config.EXIFTOOL_PATH, "-o", str(side), "-P", str(photo_path)])

    run([config.EXIFTOOL_PATH, "-overwrite_original", "-P"] + tags + [str(side)])
    return True


def export_decisions(root_id=None, only_reviewed=True, progress=None):
    """Zapise rozhodnuti fotografa z databaze do XMP souboru.

    Toto je jediny krok, ktery se dotyka slozky s fotkami. Az do jeho
    spusteni je vsechno jen v databazi a lze to beztrestne menit.
    """
    if not exiftool_available():
        return {"written": 0, "failed": 0, "total": 0,
                "message": "ExifTool nenalezen. Nainstaluj ho z exiftool.org "
                           "nebo uprav EXIFTOOL_PATH v config.py."}

    with db.connect() as conn:
        where = "WHERE 1=1"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)
        if only_reviewed:
            where += " AND reviewed=1"

        rows = conn.execute(f"SELECT * FROM photos {where}", params).fetchall()
        total = len(rows)
        written = failed = 0
        first_error = None

        for i, row in enumerate(rows, start=1):
            path = db.absolute_photo_path(conn, row)
            if not path.exists():
                failed += 1
                first_error = first_error or f"Soubor chybi: {path}"
                continue

            keywords = [k.strip() for k in (row["keywords"] or "").split(",") if k.strip()]
            if row["flag"] == "pick":
                keywords.append(config.KEYWORD_PICK)
            elif row["flag"] == "reject":
                keywords.append(config.KEYWORD_REJECT)
            if row["is_empty"]:
                keywords.append(config.KEYWORD_EMPTY)
            if row["species"]:
                keywords.append(row["species"])

            try:
                write_metadata(path, rating=row["rating"], keywords=keywords)
                # Zaznam o zapisu. Diky nemu jde poznat, ze fotograf po
                # zapisu jeste neco zmenil a soubory na disku uz neodpovidaji
                # databazi - jinak by v Zoneru koukal na stara data a nemel
                # jak to zjistit.
                conn.execute("UPDATE photos SET exported_at=? WHERE id=?",
                             (datetime.now().isoformat(), row["id"]))
                written += 1
            except Exception as e:
                failed += 1
                first_error = first_error or str(e)

            if progress and i % 20 == 0:
                progress(i, total)

    result = {"written": written, "failed": failed, "total": total}
    if first_error:
        result["message"] = first_error
    return result


def move_rejected(root_id=None, folder_name="_rejected"):
    """Presune odmitnute snimky do podslozky. Nic nemaze.

    Sidecar soubor se presouva spolu s originalem, jinak by se
    rozhodnuti ztratilo.
    """
    moved = 0
    with db.connect() as conn:
        where = "WHERE flag='reject'"
        params = []
        if root_id:
            where += " AND root_id=?"
            params.append(root_id)

        rows = conn.execute(f"SELECT * FROM photos {where}", params).fetchall()

        for row in rows:
            src = db.absolute_photo_path(conn, row)
            if not src.exists() or src.parent.name == folder_name:
                continue

            dest_dir = src.parent / folder_name
            dest_dir.mkdir(exist_ok=True)

            shutil.move(str(src), str(dest_dir / src.name))
            side = sidecar_path(src)
            if side.exists():
                shutil.move(str(side), str(dest_dir / side.name))

            root = db.get_root_path(conn, row["root_id"])
            new_rel = str((dest_dir / src.name).relative_to(root))
            conn.execute("UPDATE photos SET rel_path=? WHERE id=?", (new_rel, row["id"]))
            moved += 1

    return {"moved": moved}
