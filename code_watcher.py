#!/usr/bin/env python3
"""
code-watcher: detecta códigos XXXX-XXXX-XXXX (letras y números) que aparecen en un stream de
Twitch, los copia al portapapeles y avisa. Con --auto-enter además hace Ctrl+V + Enter en la
ventana enfocada.

Uso:
  python code_watcher.py --twitch rubius --pick         # elegís la zona del código sobre un cuadro del stream
  python code_watcher.py --twitch rubius                # vigila esa zona
  python code_watcher.py --twitch rubius --auto-enter   # además pega y da Enter solo
  python code_watcher.py --pick / sin --twitch          # modo captura de pantalla (fallback)

Opciones:
  --auto-enter      Ctrl+V + Enter al detectar un código. Dejá enfocado el campo de canje.
  --instant         no esperar la 2da lectura cuando el código no tiene "CODIGO:" al lado
  --quality 720p60  menos CPU
  --debug           muestra cada lectura y cuánto tarda
"""
import argparse
import json
import queue
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).parent
SCREEN_CONFIG = HERE / "region.json"
TWITCH_CONFIG = HERE / "twitch_region.json"

CODE = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4})\s*-\s*([A-Z0-9]{4})\s*-\s*([A-Z0-9]{4})(?![A-Z0-9])")
# "CODIGO:" / "CÓDIGO" / "CODE" pegado o no al código
ANCHOR = re.compile(r"C[O0Ó]D[I1]G[O0]|C[O0]DE")
MIN_SCORE = 0.8
INSTANT_SCORE = 0.9   # con "CODIGO" al lado y esta confianza, se acepta en la 1ra lectura


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


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
                       text="Marcá la zona donde sale el código · Esc cancela")
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

    def latest(self) -> np.ndarray | None:
        shot = self.sct.grab(self.region)
        return np.array(Image.frombytes("RGB", shot.size, shot.rgb))


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

    def latest(self) -> np.ndarray | None:
        with self._lock:
            if self._frame is None or self._frame_id == self._consumed_id:
                return None
            frame, self._consumed_id = self._frame, self._frame_id
        img = frame.to_ndarray(format="rgb24")
        h, w = img.shape[:2]
        c = self.crop
        return img[int(c["y0"] * h):int(c["y1"] * h), int(c["x0"] * w):int(c["x1"] * w)]


# =============================================================== chat de Twitch
def parse_privmsg(line: str) -> tuple[dict, str, str] | None:
    """'@tags :user!user@... PRIVMSG #canal :mensaje' -> (tags, usuario, mensaje)."""
    if " PRIVMSG " not in line or not line.startswith("@"):
        return None
    tags_str, rest = line[1:].split(" ", 1)
    tags = dict(t.split("=", 1) for t in tags_str.split(";") if "=" in t)
    user = rest[1:].split("!", 1)[0]
    msg = rest.split(" PRIVMSG ", 1)[1].split(" :", 1)[1] if " :" in rest.split(" PRIVMSG ", 1)[1] else ""
    return tags, user, msg


def is_mod(tags: dict) -> bool:
    """Lo decide el servidor de Twitch (tags), no el nombre: no se puede falsificar."""
    badges = tags.get("badges", "")
    return tags.get("mod") == "1" or "moderator/" in badges or "broadcaster/" in badges


def chat_codes(msg: str) -> list[str]:
    t = msg.upper().replace("—", "-").replace("–", "-")
    return ["-".join(m) for m in CODE.findall(t)]


def chat_reader(channel: str, out: "queue.Queue", debug: bool):
    """IRC anónimo de Twitch (solo lectura). Pasa los códigos que escriben mods o el streamer."""
    while True:
        try:
            sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=360)
            sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
            sock.sendall(b"NICK justinfan%d\r\n" % (int(time.time() * 1000) % 89999 + 10000))
            sock.sendall(f"JOIN #{channel}\r\n".encode())
            buf = b""
            while True:
                data = sock.recv(8192)
                if not data:
                    raise ConnectionError("el servidor cerró la conexión")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="ignore")
                    if line.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " JOIN #" in line or "ROOMSTATE" in line:
                        if "ROOMSTATE" in line:
                            log(f"Chat conectado (#{channel}). Solo se toman códigos de mods y del streamer.")
                    elif (p := parse_privmsg(line)):
                        tags, user, msg = p
                        codes = chat_codes(msg)
                        if not codes:
                            continue
                        if is_mod(tags):
                            for c in codes:
                                out.put((c, user))
                        elif debug:
                            log(f"[chat] ignorado (no es mod) {user}: {msg}")
        except Exception as e:
            log(f"Chat desconectado ({e}). Reconectando en 2s...")
            time.sleep(2)


# =============================================================== OCR
class Reader:
    """RapidOCR (red neuronal, ONNX). Detecta cada línea de texto y la lee."""

    def __init__(self, debug: bool):
        from rapidocr import RapidOCR
        self.debug = debug
        self.engine = RapidOCR(params={
            "Global.use_cls": False,          # el texto no viene rotado
            "Global.log_level": "error",
            "Det.limit_type": "max",          # no agrandar recortes chicos (era lo que lo hacía lento)
            "Det.limit_side_len": 960,
            "Rec.lang_type": "en",
            # Usar todos los núcleos no lo hace más rápido y le roba CPU a la decodificación del video
            "EngineConfig.onnxruntime.intra_op_num_threads": max(1, min(4, (os.cpu_count() or 4) // 2)),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,            # modelo de letras latinas (no inventa caracteres chinos)
        })

    def read(self, img: np.ndarray) -> list[tuple[str, bool]]:
        """Devuelve [(código, tiene "CODIGO" al lado)]."""
        r = self.engine(img)
        out = []
        for text, score in zip(r.txts or (), r.scores or ()):
            if self.debug:
                log(f"[ocr] {text!r} ({score:.2f})")
            if score < MIN_SCORE:
                continue
            t = text.upper().replace("—", "-").replace("–", "-")
            anchored = bool(ANCHOR.search(t)) and score >= INSTANT_SCORE
            if anchored:  # "CODIGO:3G2C-..." o "CODIGO3G2C-...": sacar la etiqueta
                t = ANCHOR.sub(" ", t).replace(":", " ")
            for m in CODE.findall(t):
                out.append(("-".join(m), anchored))
        return out


# =============================================================== confirmación / anti-duplicados
class Tracker:
    """
    Lecturas parecidas (≤3 caracteres distintos) = el mismo código mal leído: se agrupan y se
    avisa una sola vez. Se confirma al instante si al lado dice "CODIGO", si no con 2 lecturas.
    Si después otra versión del mismo grupo junta claramente más lecturas, avisa una corrección.
    """

    def __init__(self, need: int):
        self.need = need
        self.groups: list[dict] = []

    def _group(self, code):
        for g in self.groups:
            if any(distance(code, c) <= 3 for c in g["votes"]):
                return g
        g = {"votes": {}, "first": time.time(), "sent": None, "fixes": 0}
        self.groups.append(g)
        return g

    def add(self, code: str, anchored: bool) -> tuple[str, str] | None:
        g = self._group(code)
        g["votes"][code] = g["votes"].get(code, 0) + 1
        best = max(g["votes"], key=g["votes"].get)
        if g["sent"] is None:
            if anchored or g["votes"][best] >= self.need:
                g["sent"] = best if not anchored else code
                return g["sent"], "nuevo"
        elif best != g["sent"] and g["votes"][best] >= g["votes"].get(g["sent"], 0) + 2 and g["fixes"] < 2:
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
    """Ctrl+A, Ctrl+V y Enter en la ventana enfocada, sin dependencias extra."""
    if sys.platform == "win32":
        import ctypes
        kbd = ctypes.windll.user32.keybd_event
        VK_CONTROL, VK_A, VK_V, VK_RETURN, UP = 0x11, 0x41, 0x56, 0x0D, 0x0002
        kbd(VK_CONTROL, 0, 0, 0)
        kbd(VK_A, 0, 0, 0); kbd(VK_A, 0, UP, 0)   # seleccionar todo: el pegado reemplaza lo que haya
        kbd(VK_V, 0, 0, 0); kbd(VK_V, 0, UP, 0)
        kbd(VK_CONTROL, 0, UP, 0)
        time.sleep(0.05)
        kbd(VK_RETURN, 0, 0, 0); kbd(VK_RETURN, 0, UP, 0)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "a" using command down',
                        "-e", 'tell application "System Events" to keystroke "v" using command down',
                        "-e", "delay 0.05", "-e", 'tell application "System Events" to key code 36'])
    else:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+a", "ctrl+v", "Return"])


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
    ap.add_argument("--twitch", metavar="CANAL", help="canal de Twitch")
    ap.add_argument("--chat", action="store_true", help="SOLO chat: códigos que escriban mods/streamer (no mira el video)")
    ap.add_argument("--quality", default="best", help="calidad del stream (default: best)")
    ap.add_argument("--pick", action="store_true", help="elegir la zona del código")
    ap.add_argument("--auto-enter", action="store_true", help="Ctrl+A + Ctrl+V + Enter al detectar un código")
    ap.add_argument("--instant", action="store_true", help="no esperar la 2da lectura")
    ap.add_argument("--debug", action="store_true", help="mostrar cada lectura")
    args = ap.parse_args()
    if args.chat and not args.twitch:
        sys.exit("Con --chat tenés que poner el canal: --twitch rubius --chat")

    clip = Clipboard()

    def deliver(code: str, label: str):
        clip.copy(code)
        if args.auto_enter:
            paste_and_enter()
        alert(code, label)
        print("\a", end="")
        log(f">>> {label}: {code}  (copiado{' + Ctrl+V + Enter' if args.auto_enter else ''})")

    if args.auto_enter:
        log("AUTO-ENTER activado: dejá enfocado el campo donde se pega el código y no toques la compu.")

    try:
        if args.chat:
            run_chat(args, clip, deliver)
        else:
            run_video(args, clip, deliver)
    except KeyboardInterrupt:
        print("\nChau.")


def run_chat(args, clip, deliver):
    channel = args.twitch.lower().removeprefix("https://").removeprefix("www.").removeprefix("twitch.tv/").strip("/")
    q: queue.Queue = queue.Queue()
    threading.Thread(target=chat_reader, args=(channel, q, args.debug), daemon=True).start()
    log(f"Escuchando el chat de #{channel} (solo mods y streamer). Ctrl+C para salir.")
    seen: set[str] = set()
    while True:
        try:
            code, user = q.get(timeout=0.05)
        except queue.Empty:
            clip.pump()
            continue
        if code in seen:
            if args.debug:
                log(f"[chat] repetido de {user}: {code}")
            continue
        seen.add(code)
        deliver(code, f"CHAT ({user})")


def run_video(args, clip, deliver):
    source = TwitchSource(args.twitch, args.quality, args.pick) if args.twitch else ScreenSource(args.pick)
    log("Cargando OCR...")
    reader = Reader(args.debug)
    tracker = Tracker(need=1 if args.instant else 2)
    log(f"Vigilando {source.describe()}. Ctrl+C para salir.")
    prev_small, prev_found, skipped = None, [], 0
    while True:
        img = source.latest()
        if img is None or img.size == 0:
            time.sleep(0.01)
            clip.pump()
            continue
        # Si la zona no cambió desde la última lectura (y no había código), no releer
        small = img[::8, ::8].astype(np.int16)
        if (prev_small is not None and small.shape == prev_small.shape and not prev_found
                and skipped < 15 and np.abs(small - prev_small).mean() < 1.5):
            skipped += 1
            time.sleep(0.01)
            continue
        prev_small, skipped = small, 0
        t0 = time.perf_counter()
        found = reader.read(img)
        prev_found = found
        if args.debug:
            log(f"[frame] {1000 * (time.perf_counter() - t0):.0f}ms  {found}")
        for code, anchored in found:
            hit = tracker.add(code, anchored)
            if hit:
                deliver(hit[0], f"VIDEO {hit[1].upper()}")
        clip.pump()


if __name__ == "__main__":
    main()
