# code-watcher

Vigila un stream de Twitch y, cuando aparece un código `XXXX-XXXX-XXXX` (letras y números),
lo copia al portapapeles y avisa con sonido. Con `--auto-enter` además lo pega (Ctrl+V) y da Enter.

Usa **RapidOCR** (red neuronal, corre en la CPU): no hace falta instalar Tesseract.

## Instalación

### Windows

1. Instalá **Python 3.11 o más nuevo** desde https://www.python.org/downloads/ (tildá **"Add python.exe to PATH"**).
2. En la consola:

```
git clone https://github.com/CataldiDonato/gta6.git
cd gta6
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Linux (Ubuntu/Debian)

```bash
sudo apt install -y python3-venv python3-tk git xdotool
git clone https://github.com/CataldiDonato/gta6.git
cd gta6
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

La primera vez que arranca baja los modelos del OCR (~30 MB). Arrancalo una vez antes del sorteo.

## Uso

En Windows usá `.venv\Scripts\python`, en Linux `.venv/bin/python`.

```
# 1. Primera vez: marcá la zona donde sale el código
.venv\Scripts\python code_watcher.py --twitch rubius --pick

# 2. Las siguientes veces
.venv\Scripts\python code_watcher.py --twitch rubius --auto-enter
```

- **Zona ajustada = más rápido** (~0,2 s por lectura). Media pantalla tarda ~0,7 s.
- Si al lado del código dice **"CODIGO:"**, avisa en la primera lectura. Si no, espera a leerlo 2 veces.
- Lecturas parecidas (1-3 caracteres distintos) cuentan como el mismo código: avisa una sola vez.
- Con `--auto-enter`: dejá enfocado el campo de canje y no toques la compu.

| Opción | Para qué |
|---|---|
| `--auto-enter` | Ctrl+V + Enter al detectar un código |
| `--pick` | volver a elegir la zona |
| `--debug` | ver lo que lee y cuánto tarda cada lectura |
| `--instant` | no esperar la 2da lectura aunque no diga "CODIGO" |
| `--quality 720p60` | si la compu va lenta |

## Actualizar

```
git pull
.venv\Scripts\pip install -r requirements.txt
```
