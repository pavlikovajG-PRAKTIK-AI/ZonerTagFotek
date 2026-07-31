"""
WildSort - spousteci skript.

Bezne pouziti (databaze a nahledy zustanou u programu):
    python run.py

PRENOSNY PROJEKT - fotky na prenosnem disku, trideni na dvou pocitacich:
    python run.py --projekt D:\\Kena2026

Databaze i nahledy se ulozi do D:\\Kena2026\\_wildsort, tedy PRIMO K FOTKAM.
Disk se pak prepoji k druhemu pocitaci, tam se spusti totez a trideni
pokracuje presne tam, kde skoncilo - nic se nepocita znovu.

Na druhem pocitaci ma disk obvykle JINE PISMENO jednotky. Nemusi se
zjistovat - staci nazev disku, ktery se prenosem nemeni:

    python run.py --projekt 8T_mainBP

A kdyz clovek nevi ani to, vypise se, co je rozdelane:

    python run.py --najdi
"""

import argparse
import subprocess
import sys
import threading
import webbrowser

import config
import db
import project
import volume


def check_exiftool():
    try:
        out = subprocess.run([config.EXIFTOOL_PATH, "-ver"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="run.py", description="WildSort - trideni fotografii z expedice")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--projekt", "--project", dest="project", metavar="SLOZKA",
                   help="slozka s fotkami; databaze a nahledy se ulozi do "
                        "jeji podslozky _wildsort (prenosny projekt). Lze zadat "
                        "i NAZEV DISKU - projekt se na nem najde sam.")
    g.add_argument("--workspace", dest="workspace", metavar="SLOZKA",
                   help="presna cesta k pracovni slozce (databaze + nahledy)")
    g.add_argument("--najdi", "--find", dest="find", action="store_true",
                   help="vypise rozdelane projekty na pripojenych discich")
    return p.parse_args(argv)


def list_projects():
    """Vypise rozdelane projekty na pripojenych discich."""
    found = project.scan()
    if not found:
        print("Na pripojenych discich zadny rozdelany projekt WildSort.")
        print("Prenosny projekt zaklada:  python run.py --projekt <slozka s fotkami>")
        return
    print(f"Nalezene projekty ({len(found)}):\n")
    for p in found:
        disk = volume.describe(p.get("volume"))
        print(f"  {p['photos']}")
        print(f"      disk: {disk}")
        if p.get("photo_count") is not None:
            print(f"      snimku v databazi: {p['photo_count']}"
                  f"   vyrizeno: {p.get('reviewed', '?')}")
        print(f"      pokracovat:  python run.py --projekt \"{p['photos']}\"\n")


def resolve_project_folder(value):
    """Prevede zadani na slozku s fotkami.

    Prijima cestu, ale i NAZEV DISKU. Nazev disku je pri praci na dvou
    pocitacich prakticky jediny udaj, ktery si clovek pamatuje - pismeno
    jednotky se meni a nikdo nema duvod ho sledovat.
    """
    from pathlib import Path

    folder = Path(value).expanduser()
    if folder.is_dir():
        return folder

    # Neni to cesta - zkus nazev disku
    drive = volume.find_drive(label=value)
    if drive is not None:
        candidates = project.scan(drives=[drive])
        if len(candidates) == 1:
            found = Path(candidates[0]["photos"])
            print(f"Disk {volume.describe(drive)} - nalezen projekt: {found}")
            return found
        if len(candidates) > 1:
            print(f"Na disku {volume.describe(drive)} je vic projektu:")
            for c in candidates:
                print(f"   {c['photos']}")
            print("Zadej konkretni slozku.")
            sys.exit(2)
        print(f"Disk {volume.describe(drive)} nalezen, ale zadny projekt na nem neni.")
        sys.exit(2)

    print(f"CHYBA: '{value}' neni ani existujici slozka, ani nazev pripojeneho disku.")
    print("Rozdelane projekty vypises:  python run.py --najdi")
    sys.exit(2)


def prepare_workspace(args):
    """Nastavi workspace podle argumentu. Vraci popis pro vypis."""
    if args.project:
        folder = resolve_project_folder(args.project)
        ws = config.set_workspace(config.portable_workspace_for(folder))
        # Znacka projektu: podle ni se projekt najde na jinem pocitaci a pozna
        # se, ze slozka obsahuje rozdelanou praci.
        project.write_marker(ws, folder)
        disk = volume.describe(volume.info(folder))
        return f"prenosny projekt u fotek: {ws}\ndisk: {disk}"

    if args.workspace:
        ws = config.set_workspace(args.workspace)
        return f"workspace: {ws}"

    config.ensure_dirs()
    return f"workspace u programu: {config.WORKSPACE_DIR}"


def main(argv=None):
    args = parse_args(argv)

    if args.find:
        list_projects()
        return

    where = prepare_workspace(args)

    db.init_db()

    # Po prenosu disku muze mit koren jine pismeno jednotky - srovnej to,
    # aby se nedohledavalo pri kazdem pristupu k souboru.
    with db.connect() as conn:
        fixed = db.refresh_root_locations(conn)
    if fixed:
        print(f"Cesty k fotkam srovnany po prenosu ({fixed} importu)")

    print(where)

    version = check_exiftool()
    if version:
        print(f"ExifTool {version} nalezen ({config.EXIFTOOL_PATH})")
    else:
        print("VAROVANI: ExifTool nenalezen. Bez nej nelze cist EXIF, "
              "vytahovat nahledy z RAWu ani zapisovat XMP.")
        print("Stahni z https://exiftool.org a rozbal do "
              r"%LOCALAPPDATA%\Programs\ExifTool, nebo dej na PATH.")

    import detect
    print(f"Detektor: {detect.status()}")

    url = f"http://{config.SERVER_HOST}:{config.SERVER_PORT}"
    print(f"\nWildSort bezi na {url}")
    print("Ukoncis stiskem Ctrl+C.\n")

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nUkonceno.")
        sys.exit(0)
