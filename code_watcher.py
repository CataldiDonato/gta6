#!/usr/bin/env python3
"""
code-watcher: detecta códigos que aparecen en un stream, los copia al portapapeles
y te avisa (notificación + sonido). NO canjea nada: el canje lo hacés vos (Ctrl+V).

Dos fuentes de video:
  - Twitch directo (recomendado): baja el stream con streamlink en baja latencia y
    calidad original, sin depender del navegador ni de la ventana.
  - Captura de pantalla (fallback): lee una zona de tu monitor.

Uso:
  python code_watcher.py --twitch rubius --pick   # elegís la zona del código sobre un cuadro del stream
  python code_watcher.py --twitch rubius          # vigila video + chat (mods/streamer)
  python code_watcher.py --pick                   # modo pantalla: elegís la zona del monitor
  python code_watcher.py                          # modo pantalla: vigila esa zona

Opciones útiles:
  --pattern REGEX   formato del código si no es del tipo ABCD-1234-EFGH
  --no-chat         no leer el chat de Twitch
  --chat-all        aceptar códigos del chat de cualquiera (por defecto solo mods/streamer,
                    porque la gente spamea códigos falsos)
  --quality 720p60  calidad del stream (default: best). Menos calidad = menos CPU, peor OCR
  --debug           muestra todo lo que lee el OCR
"""
import argparse
import json
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps

HERE = Path(__file__).parent
SCREEN_CONFIG = HERE / "region.json"
TWITCH_CONFIG = HERE / "twitch_region.json"

# Default: bloques de 4-6 letras/números separados por guiones (ej: ABCD-1234-EFGH-5678)
DEFAULT_PATTERN = r"\b[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,5}\b"
OCR_CONFIG = "--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

# En Windows el instalador de Tesseract no lo agrega al PATH
_WIN_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if sys.platform == "win32" and _WIN_TESSERACT.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_WIN_TESSERACT)


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


# =============================================================== selector de zona
def drag_rectangle(background: Image.Image | None = None) -> tuple[int, int, int, int] | None:
    """
    Sin background: pantalla completa semitransparente, devuelve coords absolutas del monitor.
    Con background: muestra la imagen (escalada) y devuelve coords dentro de la imagen original.
    """
    root = tk.Tk()
    root.attributes("-topmost", True)
    scale = 1.0
    if background is None:
        root.attributes("-fullscreen", True)
        root.attributes("-alpha", 0.3)
        canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        cx = root.winfo_screenwidth() // 2
    else:
        from PIL import ImageTk
        root.title("Arrastrá sobre la zona del código")
        max_w = int(root.winfo_screenwidth() * 0.9)
        max_h = int(root.winfo_screenheight() * 0.85)
        scale = min(1.0, max_w / background.width, max_h / background.height)
        shown = background.resize((int(background.width * scale), int(background.height * scale)))
        photo = ImageTk.PhotoImage(shown)
        canvas = tk.Canvas(root, width=shown.width, height=shown.height, cursor="cross",
                           highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, image=photo, anchor="nw")
        canvas._photo = photo  # que no lo junte el GC
        cx = shown.width // 2
    canvas.create_text(cx, 30, fill="yellow", font=("Sans", 16, "bold"),
                       text="Arrastrá sobre la zona donde aparece el código · Esc cancela")

    state: dict = {}

    def pos(e):
        return (e.x_root, e.y_root) if background is None else (e.x, e.y)

    def down(e):
        state["p0"] = pos(e)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)
        state["c0"] = (e.x, e.y)

    def drag(e):
        canvas.coords(state["rect"], *state["c0"], e.x, e.y)

    def up(e):
        (x0, y0), (x1, y1) = state["p0"], pos(e)
        state["box"] = tuple(int(v / scale) for v in (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        root.destroy()

    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()

    box = state.get("box")
    if not box or box[2] - box[0] < 10 or box[3] - box[1] < 10:
        return None
    return box


# =============================================================== fuentes de video
class ScreenSource:
    def __init__(self, pick: bool):
        import mss
        if pick or not SCREEN_CONFIG.exists():
            box = drag_rectangle()
            if not box:
                sys.exit("Zona inválida o cancelada.")
            x0, y0, x1, y1 = box
            SCREEN_CONFIG.write_text(json.dumps({"left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}))
            log(f"Zona guardada en {SCREEN_CONFIG}")
        self.region = json.loads(SCREEN_CONFIG.read_text())
        self.sct = mss.mss()

    def describe(self):
        return f"pantalla {self.region}"

    def latest(self) -> Image.Image | None:
        shot = self.sct.grab(self.region)
        return Image.frombytes("RGB", shot.size, shot.rgb)


class _Readable:
    """PyAV pide readable(); el stream de streamlink no lo tiene."""

    def __init__(self, fd):
        self.fd = fd

    def read(self, n=-1):
        return self.fd.read(n)

    def readable(self):
        return True

    def close(self):
        self.fd.close()


class TwitchSource:
    """Decodifica el stream en un thread y guarda siempre el último cuadro (ya recortado)."""

    def __init__(self, channel: str, quality: str, pick: bool):
        self.channel = channel.lower().removeprefix("https://").removeprefix("www.") \
            .removeprefix("twitch.tv/").strip("/")
        self.quality = quality
        self._lock = threading.Lock()
        self._frame = None          # av.VideoFrame más reciente
        self._frame_id = 0
        self._consumed_id = 0

        if pick or not TWITCH_CONFIG.exists():
            log("Bajando un cuadro del stream para elegir la zona...")
            img = self._grab_one_frame()
            box = drag_rectangle(img)
            if not box:
                sys.exit("Zona inválida o cancelada.")
            x0, y0, x1, y1 = box
            # Se guarda como fracción del cuadro, así sirve en cualquier calidad
            TWITCH_CONFIG.write_text(json.dumps({
                "x0": x0 / img.width, "y0": y0 / img.height,
                "x1": x1 / img.width, "y1": y1 / img.height,
            }))
            log(f"Zona guardada en {TWITCH_CONFIG}")
        self.crop = json.loads(TWITCH_CONFIG.read_text())
        threading.Thread(target=self._run, daemon=True).start()

    def describe(self):
        return f"twitch.tv/{self.channel} ({self.quality}, baja latencia)"

    def _open(self):
        from streamlink import Streamlink
        from streamlink.options import Options
        session = Streamlink()
        session.set_option("hls-live-edge", 2)
        streams = session.streams(f"https://twitch.tv/{self.channel}",
                                  options=Options({"low-latency": True, "disable-ads": True}))
        if not streams:
            raise RuntimeError(f"{self.channel} no está en vivo")
        if self.quality not in streams:
            raise RuntimeError(f"Calidad '{self.quality}' no disponible. Hay: {', '.join(streams)}")
        return _Readable(streams[self.quality].open())

    def _grab_one_frame(self) -> Image.Image:
        import av
        fd = self._open()
        try:
            with av.open(fd) as container:
                for frame in container.decode(video=0):
                    return frame.to_image()
        finally:
            fd.close()
        sys.exit("No se pudo leer ningún cuadro del stream.")

    def _run(self):
        import av
        while True:
            try:
                fd = self._open()
                log("Stream conectado.")
                with av.open(fd) as container:
                    vstream = container.streams.video[0]
                    vstream.thread_type = "AUTO"
                    for frame in container.decode(vstream):
                        with self._lock:
                            self._frame = frame
                            self._frame_id += 1
                fd.close()
                log("El stream se cortó, reconectando...")
            except Exception as e:
                log(f"Error con el stream ({e}). Reintento en 3s...")
            time.sleep(3)

    def latest(self) -> Image.Image | None:
        with self._lock:
            if self._frame is None or self._frame_id == self._consumed_id:
                return None
            frame, self._consumed_id = self._frame, self._frame_id
        img = frame.to_image()
        c = self.crop
        return img.crop((int(c["x0"] * img.width), int(c["y0"] * img.height),
                         int(c["x1"] * img.width), int(c["y1"] * img.height)))


# =============================================================== chat de Twitch
def chat_reader(channel: str, pattern: re.Pattern, out: queue.Queue, everyone: bool):
    """IRC anónimo de Twitch. Por defecto solo toma códigos del streamer, mods o VIPs."""
    trusted = ("broadcaster/", "moderator/", "vip/")
    while True:
        try:
            sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=300)
            sock.sendall(b"CAP REQ :twitch.tv/tags\r\nNICK justinfan%d\r\n" % (int(time.time()) % 100000))
            sock.sendall(f"JOIN #{channel}\r\n".encode())
            log(f"Chat conectado (#{channel}, {'todos' if everyone else 'solo streamer/mods/VIPs'}).")
            buf = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("chat cerrado")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode(errors="ignore")
                    if line.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    if " PRIVMSG " not in line:
                        continue
                    tags_str = line[1:].split(" ", 1)[0] if line.startswith("@") else ""
                    tags = dict(t.split("=", 1) for t in tags_str.split(";") if "=" in t)
                    badges = tags.get("badges", "")
                    if not everyone and not any(b in badges for b in trusted):
                        continue
                    msg = line.split(" PRIVMSG ", 1)[1].split(" :", 1)[-1].upper()
                    msg = re.sub(r"\s*-\s*", "-", msg)
                    for code in pattern.findall(msg):
                        out.put(("chat", code))
        except Exception as e:
            log(f"Chat desconectado ({e}). Reintento en 3s...")
            time.sleep(3)


# =============================================================== OCR
def preprocess(img: Image.Image) -> list[Image.Image]:
    """Dos variantes (texto claro sobre oscuro y al revés), agrandadas x2."""
    g = ImageOps.grayscale(img)
    g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    bw = g.point(lambda p: 255 if p > 140 else 0)
    return [bw, ImageOps.invert(bw)]


def read_codes(img: Image.Image, pattern: re.Pattern, debug: bool) -> set[str]:
    found = set()
    for variant in preprocess(img):
        text = pytesseract.image_to_string(variant, config=OCR_CONFIG).upper()
        if debug and text.strip():
            log(f"[ocr] {text.strip()!r}")
        text = re.sub(r"\s*-\s*", "-", text)  # tesseract a veces mete espacios en los guiones
        found.update(pattern.findall(text))
    return found


# =============================================================== avisos
class Clipboard:
    """Portapapeles via Tk (en X11 el dueño del portapapeles tiene que seguir vivo)."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def copy(self, text: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    def pump(self):
        self.root.update()


def alert(code: str, origin: str):
    """Notificación + sonido según el sistema. Si algo falla, igual queda el aviso en la terminal."""
    title, body = f"¡CÓDIGO! ({origin})", f"{code} (ya está en el portapapeles)"
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        elif sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", f'display notification "{body}" with title "{title}" sound name "Glass"'])
        else:
            subprocess.Popen(["notify-send", "-u", "critical", title, body])
            subprocess.Popen(["canberra-gtk-play", "-i", "complete"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# =============================================================== main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--twitch", metavar="CANAL", help="leer el stream directo de Twitch")
    ap.add_argument("--quality", default="best", help="calidad del stream (default: best)")
    ap.add_argument("--pick", action="store_true", help="elegir la zona del código")
    ap.add_argument("--pattern", default=DEFAULT_PATTERN, help="regex del formato del código")
    ap.add_argument("--interval", type=float, default=0.2, help="segundos entre lecturas OCR")
    ap.add_argument("--no-chat", action="store_true", help="no leer el chat de Twitch")
    ap.add_argument("--chat-all", action="store_true", help="aceptar códigos del chat de cualquiera")
    ap.add_argument("--debug", action="store_true", help="mostrar todo lo que lee el OCR")
    args = ap.parse_args()

    pattern = re.compile(args.pattern)
    source = TwitchSource(args.twitch, args.quality, args.pick) if args.twitch else ScreenSource(args.pick)

    found: queue.Queue = queue.Queue()
    if args.twitch and not args.no_chat:
        threading.Thread(target=chat_reader, args=(source.channel, pattern, found, args.chat_all),
                         daemon=True).start()

    clip = Clipboard()
    seen: set[str] = set()
    log(f"Vigilando {source.describe()}. Ctrl+C para salir.")

    try:
        while True:
            t0 = time.perf_counter()
            img = source.latest()
            if img is not None:
                for code in read_codes(img, pattern, args.debug):
                    found.put(("video", code))
            while not found.empty():
                origin, code = found.get()
                if code in seen:
                    continue
                seen.add(code)
                clip.copy(code)
                alert(code, origin)
                print(f"\a", end="")
                log(f">>> [{origin}]  {code}  (copiado)")
            clip.pump()
            time.sleep(max(0, args.interval - (time.perf_counter() - t0)))
    except KeyboardInterrupt:
        print("\nChau.")


if __name__ == "__main__":
    main()
