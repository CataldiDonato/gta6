# code-watcher

Vigila un stream de Twitch (video + chat) y, cuando aparece un código, lo copia al
portapapeles y avisa con sonido. **No canjea nada**: el código lo pegás vos (Ctrl+V).

## Instalación

### Windows

1. Instalá **Python 3.11 o más nuevo** desde https://www.python.org/downloads/ — en el
   instalador tildá **"Add python.exe to PATH"**.
2. Instalá **Tesseract OCR** desde https://github.com/UB-Mannheim/tesseract/wiki
   (dejá la carpeta por defecto: `C:\Program Files\Tesseract-OCR`).
3. En PowerShell:

```powershell
git clone https://github.com/CataldiDonato/gta6.git
cd gta6
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

(Si no tenés `git`, bajá el ZIP desde GitHub → Code → Download ZIP, descomprimilo y
entrá a la carpeta con `cd`.)

### Linux (Ubuntu/Debian)

```bash
sudo apt install -y tesseract-ocr python3-venv python3-tk git
git clone https://github.com/CataldiDonato/gta6.git
cd gta6
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

En Windows usá `.venv\Scripts\python`, en Linux `.venv/bin/python`.

```bash
# 1. Primera vez: elegí con el mouse la zona del stream donde aparece el código
.venv/bin/python code_watcher.py --twitch rubius --pick

# 2. Las siguientes veces
.venv/bin/python code_watcher.py --twitch rubius
```

Cortar con **Ctrl+C**.

| Opción | Para qué |
|---|---|
| `--pick` | volver a elegir la zona del código |
| `--debug` | ver lo que lee el OCR (para chequear que lea bien la zona) |
| `--quality 720p60` | si la compu va lenta (menos CPU, peor lectura) |
| `--chat-all` | tomar códigos del chat de cualquiera (por defecto solo streamer/mods/VIPs) |
| `--no-chat` | no leer el chat |
| `--pattern REGEX` | si el código tiene otro formato que `ABCD-1234-EFGH` |

Tips: el OCR a veces confunde **O/0** e **I/1** — si el canje falla, probá cambiándolas.
