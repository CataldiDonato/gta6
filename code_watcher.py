#!/usr/bin/env python3
"""
code-watcher: detecta códigos (formato LLLL-LLLL-LLLL, solo letras) que aparecen en un
stream de Twitch, los copia al portapapeles y avisa. Con --auto-enter además hace
Ctrl+V + Enter en la ventana que tengas enfocada.

Uso:
  python code_watcher.py --twitch rubius --pick   # elegís la zona del código sobre un cuadro del stream
  python code_watcher.py --twitch rubius          # vigila video + chat (streamer/mods/VIPs)
  python code_watcher.py --twitch rubius --auto-enter   # además pega y da Enter solo
  python code_watcher.py --pick / sin --twitch    # modo captura de pantalla (fallback)

Opciones útiles:
  --auto-enter      pega (Ctrl+V) y da Enter cuando confirma un código. Dejá el campo de
                    canje enfocado y no toques la compu.
  --instant         no esperar a confirmar (2 lecturas iguales): más rápido, más errores
  --digits          si el código también puede tener números
  --fast            menos variantes de OCR: más rápido, lee peor sobre fondos complicados
  --no-chat         no leer el chat
  --chat-all        aceptar códigos del chat de cualquiera (por defecto solo mods/streamer)
  --quality 720p60  menos CPU (peor lectura)
  --debug           muestra cada lectura
"""
import argparse
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytesseract
from PIL import Image, ImageChops, ImageOps

HERE = Path(__file__).parent
SCREEN_CONFIG = HERE / "region.json"
TWITCH_CONFIG = HERE / "twitch_region.json"

# En Windows el instalador de Tesseract no lo agrega al PATH
_WIN_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if sys.platform == "win32" and _WIN_TESSERACT.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_WIN_TESSERACT)
if os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


# =============================================================== formato del código
class CodeFormat:
    """LLLL-LLLL-LLLL. Tolera que el OCR se coma o separe los guiones."""

    def __init__(self, digits: bool):
        chars = "A-Z0-9" if digits else "A-Z"
        self.whitelist = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + ("0123456789" if digits else "") + "-"
        self.regex = re.compile(rf"(?<![{chars}])([{chars}]{{4}})[-\s]?([{chars}]{{4}})[-\s]?([{chars}]{{4}})(?![{chars}])")

    def find(self, text: str) -> set[str]:
        text = re.sub(r"[ \t]*-[ \t]*", "-", text.upper())
        return {"-".join(m) for m in self.regex.findall(text)}


def distance(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


# =============================================================== selector de zona
def drag_rectangle(background: Image.Image | None = None) -> tuple[int, int, int, int] | None:
    """Sin background: sobre el monitor. Con background: sobre la imagen (coords de la imagen)."""
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
        canvas._photo = photo
        cx = shown.width // 2
    canvas.create_text(cx, 30, fill="yellow", font=("Sans", 16, "bold"),
                       text="Marcá la zona del código, bien ajustada · Esc cancela")
    state: dict = {}

    def pos(e):
        return (e.x_root, e.y_root) if background is None else (e.x, e.y)

    def down(e):
        state["p0"], state["c0"] = pos(e), (e.x, e.y)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

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
    """Decodifica el stream en un thread y guarda siempre el último cuadro."""

    def __init__(self, channel: str, quality: str, pick: bool):
        self.channel = channel.lower().removeprefix("https://").removeprefix("www.") \
            .removeprefix("twitch.tv/").strip("/")
        self.quality = quality
        self._lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self._consumed_id = 0

        if pick or not TWITCH_CONFIG.exists():
            log("Bajando un cuadro del stream para elegir la zona...")
            img = self._grab_one_frame()
            box = drag_rectangle(img)
            if not box:
                sys.exit("Zona inválida o cancelada.")
            x0, y0, x1, y1 = box
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
def chat_reader(channel: str, fmt: CodeFormat, out: queue.Queue, everyone: bool):
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
                    if not everyone and not any(b in tags.get("badges", "") for b in trusted):
                        continue
                    msg = line.split(" PRIVMSG ", 1)[1].split(" :", 1)[-1]
                    for code in fmt.find(msg):
                        out.put(code)
        except Exception as e:
            log(f"Chat desconectado ({e}). Reintento en 3s...")
            time.sleep(3)


# =============================================================== OCR
def _otsu(g: Image.Image) -> int:
    h = g.histogram()
    total, sum_all = sum(h), sum(i * h[i] for i in range(256))
    wb = sb = 0
    best_v, best_t = 0, 128
    for t in range(256):
        wb += h[t]
        if wb == 0 or wb == total:
            continue
        sb += t * h[t]
        mb, mf = sb / wb, (sum_all - sb) / (total - wb)
        v = wb * (total - wb) * (mb - mf) ** 2
        if v > best_v:
            best_v, best_t = v, t
    return best_t


def variants(img: Image.Image, fast: bool) -> list[Image.Image]:
    """
    Versiones de la imagen para Tesseract (texto negro sobre blanco).
    - Por color: se reduce a 6 colores y se aísla cada uno. El texto de un overlay suele ser
      un color parejo, así que una de las máscaras queda con el texto limpio sin el fondo
      del juego. Es lo que más mejora la lectura sobre gameplay.
    - Umbral automático (Otsu) en gris, en las dos polaridades.
    """
    k = 4 if fast else 6
    q = img.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    raw = Image.frombytes("L", q.size, q.tobytes())
    out = [raw.point([0 if i == idx else 255 for i in range(256)]) for idx in range(k)]
    if not fast:
        g = ImageOps.grayscale(img)
        th = _otsu(g)
        bw = g.point(lambda p: 255 if p > th else 0)
        out += [bw, ImageOps.invert(bw)]
    return out


class Reader:
    def __init__(self, fmt: CodeFormat, fast: bool, debug: bool):
        self.fmt, self.fast, self.debug = fmt, fast, debug
        self.config = f"--psm 6 -c tessedit_char_whitelist={fmt.whitelist}"
        self.pool = ThreadPoolExecutor(max(2, min(8, os.cpu_count() or 4)))

    def _ocr(self, v: Image.Image) -> set[str]:
        text = pytesseract.image_to_string(v, config=self.config)
        if self.debug and text.strip():
            log(f"[ocr] {text.strip()!r}")
        return self.fmt.find(text)

    def read(self, img: Image.Image) -> dict[str, int]:
        """Devuelve {código: cantidad de variantes que lo leyeron}."""
        votes: dict[str, int] = {}
        for found in self.pool.map(self._ocr, variants(img, self.fast)):
            for c in found:
                votes[c] = votes.get(c, 0) + 1
        return votes


# =============================================================== confirmación / anti-duplicados
class Tracker:
    """
    Junta las lecturas parecidas (≤3 letras distintas = el mismo código mal leído) en un grupo.
    Un código se confirma con 2 votos (de variantes o cuadros distintos, en los últimos 4s).
    Si después otra lectura del mismo grupo junta claramente más votos, se toma como corrección.
    """
    WINDOW = 4.0

    def __init__(self, need: int):
        self.need = need
        self.groups: list[dict] = []   # {"votes": {code: [timestamps]}, "sent": code|None, "fixes": int}

    def _group(self, code):
        for g in self.groups:
            if any(distance(code, c) <= 3 for c in g["votes"]):
                return g
        g = {"votes": {}, "sent": None, "fixes": 0}
        self.groups.append(g)
        return g

    def add(self, code: str, n: int = 1, trusted: bool = False) -> tuple[str, str] | None:
        """Devuelve (código, 'nuevo'|'corrección') si hay que avisar."""
        now = time.time()
        g = self._group(code)
        g["votes"].setdefault(code, []).extend([now] * (99 if trusted else n))
        score = {c: sum(1 for t in ts if now - t <= self.WINDOW) if g["sent"] is None else len(ts)
                 for c, ts in g["votes"].items()}
        best = max(score, key=score.get)
        if g["sent"] is None:
            if score[best] >= self.need:
                g["sent"] = best
                return best, "nuevo"
        elif best != g["sent"] and score[best] >= score[g["sent"]] + 2 and g["fixes"] < 2:
            g["sent"] = best
            g["fixes"] += 1
            return best, "corrección"
        return None


# =============================================================== acciones
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


def paste_and_enter():
    """Ctrl+V y Enter en la ventana enfocada, sin dependencias extra."""
    if sys.platform == "win32":
        import ctypes
        kbd = ctypes.windll.user32.keybd_event
        VK_CONTROL, VK_V, VK_RETURN, UP = 0x11, 0x56, 0x0D, 0x0002
        kbd(VK_CONTROL, 0, 0, 0); kbd(VK_V, 0, 0, 0); kbd(VK_V, 0, UP, 0); kbd(VK_CONTROL, 0, UP, 0)
        time.sleep(0.05)
        kbd(VK_RETURN, 0, 0, 0); kbd(VK_RETURN, 0, UP, 0)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down',
                        "-e", "delay 0.05", "-e", 'tell application "System Events" to key code 36'])
    else:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v", "Return"])


def alert(code: str, kind: str):
    title, body = f"¡CÓDIGO! ({kind})", f"{code} (copiado)"
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
    ap.add_argument("--auto-enter", action="store_true", help="Ctrl+V + Enter al confirmar un código")
    ap.add_argument("--instant", action="store_true", help="no esperar confirmación (más errores)")
    ap.add_argument("--digits", action="store_true", help="el código puede tener números")
    ap.add_argument("--fast", action="store_true", help="menos variantes de OCR")
    ap.add_argument("--no-chat", action="store_true", help="no leer el chat de Twitch")
    ap.add_argument("--chat-all", action="store_true", help="aceptar códigos del chat de cualquiera")
    ap.add_argument("--debug", action="store_true", help="mostrar cada lectura")
    args = ap.parse_args()

    fmt = CodeFormat(args.digits)
    source = TwitchSource(args.twitch, args.quality, args.pick) if args.twitch else ScreenSource(args.pick)
    reader = Reader(fmt, args.fast, args.debug)
    tracker = Tracker(need=1 if args.instant else 2)

    chat_q: queue.Queue = queue.Queue()
    if args.twitch and not args.no_chat:
        threading.Thread(target=chat_reader, args=(source.channel, fmt, chat_q, args.chat_all),
                         daemon=True).start()

    clip = Clipboard()
    log(f"Vigilando {source.describe()}. Ctrl+C para salir.")
    if args.auto_enter:
        log("AUTO-ENTER activado: dejá enfocado el campo donde se pega el código y no toques la compu.")

    def handle(hit, origin):
        if not hit:
            return
        code, kind = hit
        clip.copy(code)
        if args.auto_enter:
            paste_and_enter()
        alert(code, kind)
        print("\a", end="")
        extra = " + Ctrl+V + Enter" if args.auto_enter else ""
        log(f">>> [{origin}] {kind.upper()}: {code}  (copiado{extra})")

    try:
        while True:
            img = source.latest()
            if img is None:
                time.sleep(0.01)
            else:
                t0 = time.perf_counter()
                votes = reader.read(img)
                if args.debug:
                    log(f"[frame] {1000 * (time.perf_counter() - t0):.0f}ms  {votes}")
                for code, n in sorted(votes.items(), key=lambda kv: -kv[1]):
                    handle(tracker.add(code, n), "video")
            while not chat_q.empty():
                handle(tracker.add(chat_q.get(), trusted=True), "chat")
            clip.pump()
    except KeyboardInterrupt:
        print("\nChau.")


if __name__ == "__main__":
    main()
