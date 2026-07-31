"""
WildSort - trvaly proces ExifToolu (-stay_open).

PROC TO EXISTUJE

Kazde spusteni exiftool.exe na Windows stoji okolo 0,8 sekundy - je v nem
zabaleny cely Perl, ktery se musi rozbalit a nastartovat. Pipeline ho vsak
potrebuje pro KAZDY snimek dvakrat: jednou pri vytahovani nahledu, podruhe
pri vytahovani full-res JPEGu na mereni ostrosti. U davky 2300 snimku je to
pres 3600 startu, tedy zhruba pul hodiny cisteho cekani na start Perlu,
zatimco samotna prace trva milisekundy.

ExifTool ma pro presne tento pripad rezim -stay_open: proces se spusti
jednou, prikazy se mu posilaji rourou a po kazdem "-execute" vrati vysledek
zakonceny znackou {ready}. Startovaci cena se tak plati jedinkrat.

Stejny princip (jeden proces pro mnoho souboru) uz pouziva export XMP pres
argfile - tady je potreba interaktivni varianta, protoze vysledky se
zpracovavaji prubezne po jednom souboru.

KDYZ SE DAEMON NEPODARI SPUSTIT, NIC SE NEDEJE

Vsichni volajici maji zachovanou puvodni cestu pres jednorazove procesy.
Daemon je zrychleni, ne zavislost.
"""

import subprocess
import threading

import config

# Znacka, kterou exiftool ukoncuje kazdou odpoved v -stay_open rezimu.
# Binarni JPEG by ji teoreticky mohl obsahovat; prakticky na tom stoji
# vsechny exiftool wrappery vcetne PyExifTool a za leta provozu se to
# neukazalo jako problem.
_READY = b"{ready}"

_lock = threading.Lock()
_daemon = None
_failed = False


class ExifToolDaemon:
    """Jeden trvale bezici exiftool. Neni thread-safe sam o sobe -
    pristup serializuje modulovy zamek v execute()."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [config.EXIFTOOL_PATH, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def alive(self):
        return self.proc.poll() is None

    def execute_raw(self, args, timeout=120):
        """Posle prikaz a vrati syrovy vystup (bytes) az po znacku {ready}."""
        payload = "\n".join(args) + "\n-execute\n"
        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()

        # Cteni po blocich, dokud se neobjevi znacka. Timeout resi watchdog
        # ve volajicim - tady by se selhani projevilo mrtvym procesem.
        chunks = []
        buffer = b""
        while True:
            block = self.proc.stdout.read1(65536)
            if not block:
                raise RuntimeError("exiftool daemon neocekavane skoncil")
            buffer += block
            idx = buffer.find(_READY)
            if idx >= 0:
                chunks.append(buffer[:idx])
                break
            # Znacka muze byt rozrizla mezi bloky - drz posledni kousek
            if len(buffer) > len(_READY):
                chunks.append(buffer[:-len(_READY)])
                buffer = buffer[-len(_READY):]
        return b"".join(chunks).rstrip(b"\r\n")

    def extract_binary(self, tag, src_path):
        """Vytahne binarni obsah znacky (napr. -JpgFromRaw). None = neni."""
        data = self.execute_raw(["-charset", "filename=utf8", "-b", tag,
                                 str(src_path)])
        return data if data else None

    def close(self):
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def extract_preview(src_path, dest_path, min_bytes=20000):
    """Vytahne nejvetsi vestaveny nahled z RAWu pres daemon.

    Zkousi stejne znacky jako proxy.extract_preview. Vraci True pri uspechu.
    Pri jakemkoliv selhani daemonu vraci None - volajici pak pouzije
    jednorazovy proces (pomalejsi, ale funkcni vzdy).
    """
    global _daemon, _failed

    with _lock:
        if _failed:
            return None
        if _daemon is None or not _daemon.alive():
            try:
                _daemon = ExifToolDaemon()
            except Exception:
                _failed = True
                return None

        try:
            for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
                data = _daemon.extract_binary(tag, src_path)
                if data and len(data) >= min_bytes:
                    with open(dest_path, "wb") as f:
                        f.write(data)
                    return True
            return False
        except Exception:
            # Rozbity daemon zahod; pristi volani zkusi novy, po druhem
            # selhani se modul vypne a vsechno jede postaru.
            try:
                _daemon.close()
            except Exception:
                pass
            if _daemon is not None:
                _daemon = None
            else:
                _failed = True
            return None


def shutdown():
    """Ukonci daemon (konec pipeline, konec serveru)."""
    global _daemon
    with _lock:
        if _daemon is not None:
            _daemon.close()
            _daemon = None
