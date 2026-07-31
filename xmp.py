"""
WildSort - krok 7: zapis metadat pro Zoner.

Do RAW souboru se NIKDY nezapisuje. Vse jde do XMP sidecar souboru
vedle originalu (IMG_1234.CR3 -> IMG_1234.xmp). Zoner Photo Studio i
ostatni bezne prohlizece tyto soubory ctou.

Sidecar se zaklada jinym prikazem, nez se aktualizuje - exiftool neumi
zapsat do souboru, ktery jeste neexistuje. Obe varianty resi
write_metadata().
"""

import os
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


def _run_argfile(lines, timeout=900):
    """Spusti JEDEN proces exiftoolu pro mnoho souboru pres argfile.

    PROC DAVKOVE

    Spusteni exiftoolu na Windows trva okolo 0,8 s - je v nem zabaleny cely
    Perl. Pri jednom procesu na soubor je to u 1356 snimku pres pul hodiny
    cekani, prestoze samotny zapis metadat je otazkou milisekund. Merene:
    jednotlive 830 ms/soubor, davkove 149 ms/soubor u sedmi souboru, a cim
    vetsi davka, tim vic se startovaci cena rozpusti (marginalne ~30 ms).

    Prikazy se v argfile oddeluji radkem -execute. Vraci (returncode, stdout).
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".args", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        out = subprocess.run(
            [config.EXIFTOOL_PATH, "-charset", "filename=utf8", "-@", path],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_ratings(sidecars):
    """Precte XMP:Rating z davky sidecaru jednim procesem.

    Vraci {cesta: hodnoceni}. Chybejici nebo neprecteny soubor v mape neni.
    """
    existing = [s for s in sidecars if s.exists()]
    if not existing:
        return {}

    import json as _json

    try:
        out = subprocess.run(
            [config.EXIFTOOL_PATH, "-charset", "filename=utf8", "-j", "-n",
             "-XMP:Rating"] + [str(s) for s in existing],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace")
        data = _json.loads(out.stdout) if out.stdout.strip() else []
    except Exception:
        return {}

    result = {}
    for item in data:
        src = item.get("SourceFile")
        if not src:
            continue
        try:
            value = int(item.get("Rating") or 0)
        except (TypeError, ValueError):
            value = 0
        # exiftool vraci lomitka dopredu, porovnava se s Path
        result[str(Path(src))] = value
    return result


def _write_batch(jobs):
    """Zapise davku snimku. jobs = [(photo_path, side_path, tags), ...]

    Drzi stejny DVOUKROKOVY postup jako write_metadata, jen kazdy krok
    provede jednim procesem:

      1) zaloz chybejici sidecar jako kopii metadat originalu
      2) zapis hodnoty do sidecaru

    Jednim prikazem to nejde - exiftool by prirazene hodnoty prebil metadaty
    z RAWu a sidecar by vznikl s Rating 0, navic bez jakekoliv hlasky.

    Vraci (mnozina uspesnych indexu, prvni chyba nebo None).
    """
    if not jobs:
        return set(), None

    first_error = None

    # --- 1) zalozeni chybejicich sidecaru ---
    to_create = [(i, photo, side) for i, (photo, side, _t) in enumerate(jobs)
                 if not side.exists()]
    if to_create:
        lines = []
        for _i, photo, side in to_create:
            lines += ["-o", str(side), "-P", str(photo), "-execute"]
        code, out = _run_argfile(lines)
        if code != 0 and "Error" in out and first_error is None:
            for line in out.splitlines():
                if line.startswith("Error"):
                    first_error = line.strip()
                    break

    # Sidecar, ktery se nepodarilo zalozit, nema smysl dal zapisovat
    ok_indexes = {i for i, (photo, side, _t) in enumerate(jobs) if side.exists()}
    if len(ok_indexes) < len(jobs) and first_error is None:
        missing = next(side for i, (photo, side, _t) in enumerate(jobs)
                       if i not in ok_indexes)
        first_error = f"Sidecar se nepodarilo zalozit: {missing}"

    # --- 1b) OCHRANA RUCNIHO HODNOCENI ZE ZONERU ---
    #
    # Sidecar uz muze existovat s hvezdickami, ktere fotograf pridelil
    # v Zoneru. Zapsat do nej Rating=0 by tichou cestou smazalo lidske
    # rozhodnuti, a to je vzdy cennejsi nez automatika. Nula znamena
    # "WildSort nema nazor", ne "smaz, co tam je".
    #
    # Hvezdicku 1-5 z WildSortu zapsat chceme - to uz je vedomy vyber.
    # Prectou se davkove jednim procesem, takze to nic nestoji.
    if config.PRESERVE_EXISTING_RATINGS:
        zero_writes = [(i, jobs[i]) for i in sorted(ok_indexes)
                       if any(t.startswith("-XMP-xmp:Rating=0") for t in jobs[i][2])]
        if zero_writes:
            existing = _read_ratings([side for _i, (_p, side, _t) in zero_writes])
            for i, (_photo, side, tags) in zero_writes:
                if existing.get(str(side), 0) > 0:
                    # Ponech hvezdicku ze Zoneru, ostatni znacky zapis dal
                    jobs[i] = (jobs[i][0], side,
                               [t for t in tags if not t.startswith("-XMP-xmp:Rating=")])

    # --- 2) zapis hodnot do sidecaru ---
    writable = [(i, jobs[i]) for i in sorted(ok_indexes) if jobs[i][2]]
    if writable:
        lines = []
        for _i, (_photo, side, tags) in writable:
            lines += ["-overwrite_original", "-P"] + tags + [str(side), "-execute"]
        code, out = _run_argfile(lines)

        updated = out.count("image files updated") + out.count("image files created")
        if updated < len(writable):
            # Neco se nezapsalo a z davkoveho vystupu nepoznam co. Presnou
            # informaci ma cenu koupit zpomalenim jen u te jedne davky.
            ok_indexes = set()
            for i, (photo, side, tags) in writable:
                try:
                    res = subprocess.run(
                        [config.EXIFTOOL_PATH, "-overwrite_original", "-P"] + tags
                        + [str(side)], capture_output=True, text=True, timeout=120)
                    if res.returncode == 0:
                        ok_indexes.add(i)
                    elif first_error is None:
                        first_error = res.stderr.strip() or "exiftool selhal"
                except Exception as e:
                    if first_error is None:
                        first_error = str(e)

    return ok_indexes, first_error


def export_decisions(root_id=None, only_reviewed=True, progress=None,
                     batch_size=100):
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
        done = 0

        if progress:
            progress(0, total)

        # Zpracovani po davkach: jedna davka = dva procesy exiftoolu bez
        # ohledu na to, kolik je v ni souboru. Davka 100 drzi hlaseni
        # o postupu dost caste, aby bylo videt, ze se neco deje.
        for start in range(0, total, max(1, batch_size)):
            chunk = rows[start:start + max(1, batch_size)]
            jobs = []
            job_rows = []

            for row in chunk:
                path = db.absolute_photo_path(conn, row)
                if not path.exists():
                    failed += 1
                    first_error = first_error or f"Soubor chybi: {path}"
                    continue

                keywords = [k.strip() for k in (row["keywords"] or "").split(",")
                            if k.strip()]
                if row["flag"] == "pick":
                    keywords.append(config.KEYWORD_PICK)
                elif row["flag"] == "reject":
                    keywords.append(config.KEYWORD_REJECT)
                if row["is_empty"]:
                    keywords.append(config.KEYWORD_EMPTY)
                if row["species"]:
                    keywords.append(row["species"])

                tags = _tag_args(row["rating"], keywords, None)
                jobs.append((path, sidecar_path(path), tags))
                job_rows.append(row)

            ok, err = _write_batch(jobs)
            first_error = first_error or err

            now = datetime.now().isoformat()
            for i, row in enumerate(job_rows):
                if i in ok:
                    # Zaznam o zapisu. Diky nemu jde poznat, ze fotograf po
                    # zapisu jeste neco zmenil a soubory na disku uz neodpovidaji
                    # databazi - jinak by v Zoneru koukal na stara data a nemel
                    # jak to zjistit.
                    conn.execute("UPDATE photos SET exported_at=? WHERE id=?",
                                 (now, row["id"]))
                    written += 1
                else:
                    failed += 1

            done += len(chunk)
            conn.commit()          # postup je videt i pri prerusení
            if progress:
                progress(done, total)

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
