"""
WildSort - spousteci skript.

Spusti server a otevre prohlizec. Pro bezne pouziti staci:
    python run.py
"""

import sys
import threading
import webbrowser
import subprocess

import config
import db


def check_exiftool():
    try:
        out = subprocess.run([config.EXIFTOOL_PATH, "-ver"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def main():
    config.ensure_dirs()
    db.init_db()

    version = check_exiftool()
    if version:
        print(f"ExifTool {version} nalezen")
    else:
        print("VAROVANI: ExifTool nenalezen. Bez nej nelze cist EXIF, "
              "vytahovat nahledy z RAWu ani zapisovat XMP.")
        print("Stahni z https://exiftool.org a dej na PATH, nebo uprav "
              "EXIFTOOL_PATH v config.py.")

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
