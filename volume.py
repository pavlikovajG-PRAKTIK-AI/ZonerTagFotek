"""
WildSort - identifikace disku podle NAZVU, ne podle pismene jednotky.

PROC TO EXISTUJE

Pismeno jednotky patri pocitaci, ne disku. Tentyz prenosny disk je na
stolnim pocitaci D:, na notebooku F: a po pripojeni fotoaparatu klidne G:.
Cesta ulozena jako "D:\\Kena2026" je proto udaj s omezenou trvanlivosti.

Nazev svazku (label) a jeho seriove cislo naopak patri disku a pri prenosu
se nemeni. Podle nich lze rozdelanou praci najit sama - uzivatel nemusi
zjistovat, jake pismeno disk tentokrat dostal.

CO SE UKLADA

  label   nazev svazku, jak ho ukazuje Prizkumnik ("KENA2026")
  serial  seriove cislo svazku, osm hexa cislic

Label sam nestaci: dva disky se muzou jmenovat stejne (nebo nijak). Serial
sam je nesdelny cloveku. Dohromady jedno identifikuje a druhe pojmenuje.
Serial se meni pri formatovani, coz je spravne - naformatovany disk uz je
jiny disk.

Modul je zamerne bez zavislosti (jen ctypes ze standardni knihovny) a na
jinych systemech nez Windows tise vraci None, aby na nem nic nestalo.
"""

import ctypes
import os
import string
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# GetDriveTypeW
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5

_TYPE_NAMES = {
    DRIVE_REMOVABLE: "vymenitelny",
    DRIVE_FIXED: "pevny",
    DRIVE_REMOTE: "sitovy",
    DRIVE_CDROM: "optycky",
}


def _kernel32():
    if not IS_WINDOWS:
        return None
    return ctypes.WinDLL("kernel32", use_last_error=True)


def info(path):
    """Vrati identitu svazku, na kterem lezi zadana cesta.

    {"drive": "E:\\", "label": "KENA2026", "serial": "1A2B3C4D",
     "fs": "NTFS", "type": "vymenitelny"}

    None znamena, ze se identita zjistit neda (jiny system, sitova cesta,
    odpojeny disk). Volajici pak pracuje jen s cestou.
    """
    k = _kernel32()
    if k is None:
        return None

    try:
        drive = os.path.splitdrive(str(Path(path).resolve()))[0]
    except OSError:
        drive = os.path.splitdrive(str(path))[0]
    if not drive:
        return None
    root = drive + "\\"

    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong(0)
    max_len = ctypes.c_ulong(0)
    flags = ctypes.c_ulong(0)

    ok = k.GetVolumeInformationW(
        ctypes.c_wchar_p(root), label, ctypes.sizeof(label) // 2,
        ctypes.byref(serial), ctypes.byref(max_len), ctypes.byref(flags),
        fs, ctypes.sizeof(fs) // 2,
    )
    if not ok:
        return None

    drive_type = k.GetDriveTypeW(ctypes.c_wchar_p(root))
    return {
        "drive": root,
        "label": label.value,
        "serial": f"{serial.value:08X}",
        "fs": fs.value,
        "type": _TYPE_NAMES.get(drive_type, "neznamy"),
        "removable": drive_type == DRIVE_REMOVABLE,
    }


def attached():
    """Vrati identitu vsech pripojenych svazku, ktere lze cist."""
    k = _kernel32()
    if k is None:
        return []

    mask = k.GetLogicalDrives()
    found = []
    for i, letter in enumerate(string.ascii_uppercase):
        if not (mask >> i) & 1:
            continue
        root = f"{letter}:\\"
        drive_type = k.GetDriveTypeW(ctypes.c_wchar_p(root))
        # Optiku a sitove disky preskoc - hledani projektu na nich nema smysl
        # a u prazdne mechaniky to jen ceka na timeout.
        if drive_type not in (DRIVE_REMOVABLE, DRIVE_FIXED):
            continue
        data = info(root)
        if data:
            found.append(data)
    return found


def describe(data):
    """Kratky popis pro vypis a rozhrani: 'KENA2026 (E:, vymenitelny)'."""
    if not data:
        return "neznamy disk"
    name = data.get("label") or "bez nazvu"
    return f"{name} ({data['drive'].rstrip(chr(92))}, {data.get('type', '?')})"


def find_drive(label=None, serial=None):
    """Najde pripojeny svazek podle nazvu a/nebo serioveho cisla.

    Serial rozhoduje - je jednoznacny. Label je zaloha pro pripad, ze disk
    byl mezitim naformatovan nebo se identita ulozila jen castecne.

    Vraci identitu svazku, nebo None.
    """
    volumes = attached()
    if serial:
        for v in volumes:
            if v["serial"].upper() == str(serial).upper():
                return v
    if label:
        wanted = str(label).strip().lower()
        matches = [v for v in volumes if (v["label"] or "").strip().lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        # Vic disku stejneho nazvu bez serialu = nelze rozhodnout; radeji nic
        # nez tipovat a zapisovat XMP na cizi disk.
        if matches:
            return None
    return None


def resolve(stored_path, label=None, serial=None, relative_to_drive=None):
    """Najde slozku, i kdyz disk dostal jine pismeno.

    stored_path         cesta, jak byla ulozena (muze uz neplatit)
    label / serial      identita disku z doby ulozeni
    relative_to_drive   cesta slozky od korene disku ("Kena2026\\karta1")

    Vraci Path, ktera existuje, nebo None.
    """
    if stored_path:
        p = Path(stored_path)
        if p.is_dir():
            return p

    drive = find_drive(label, serial)
    if drive is None:
        return None

    if relative_to_drive:
        candidate = Path(drive["drive"]) / relative_to_drive
        if candidate.is_dir():
            return candidate

    # Aspon stejna slozka na spravnem disku
    if stored_path:
        tail = os.path.splitdrive(str(stored_path))[1].lstrip("\\/")
        if tail:
            candidate = Path(drive["drive"]) / tail
            if candidate.is_dir():
                return candidate
    return None


def relative_to_drive(path):
    """Cesta od korene disku, bez pismene jednotky. 'E:\\A\\B' -> 'A\\B'."""
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = str(path)
    return os.path.splitdrive(resolved)[1].lstrip("\\/")
