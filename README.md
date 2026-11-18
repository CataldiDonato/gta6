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

**Marcá la zona bien ajustada al código** (un poco de margen, sin mucho fondo): es lo que
más mejora la lectura.

Busca códigos con formato `LLLL-LLLL-LLLL` (solo letras). Para no avisar lecturas erróneas,
un código se confirma cuando se lee 2 veces; las lecturas parecidas (1-3 letras distintas)
se toman como el mismo código y no vuelven a avisar. Si más tarde una versión distinta
junta claramente más lecturas, avisa una **corrección**.

| Opción | Para qué |
|---|---|
| `--auto-enter` | al confirmar un código hace **Ctrl+V + Enter** solo. Dejá enfocado el campo de canje y no toques la compu. En Linux necesita `xdotool` |
| `--pick` | volver a elegir la zona del código |
| `--debug` | ver lo que lee el OCR y cuánto tarda cada lectura |
| `--instant` | no esperar la 2da lectura: más rápido pero más errores |
| `--fast` | menos variantes de OCR: más rápido en PCs lentas, lee peor |
| `--digits` | si el código también puede tener números |
| `--quality 720p60` | si la compu va lenta (menos CPU, peor lectura) |
| `--chat-all` | tomar códigos del chat de cualquiera (por defecto solo streamer/mods/VIPs) |
| `--no-chat` | no leer el chat |

## Actualizar

```bash
git pull
```
