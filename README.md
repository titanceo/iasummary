# iasummary

GUI para convertir videos a audio y transcribir con Whisper, con visor de transcripciones y reproductor embebido (VLC).

## Requisitos

- Windows 10/11
- Python 3.10+ (recomendado 3.11)
- ffmpeg (incluye `ffprobe`) en PATH
- VLC instalado (ejemplo: `C:\Program Files\VideoLAN\VLC\vlc.exe`)

## Instalacion (venv)

En PowerShell desde el repo:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Configuracion

Edita `app/config.py` y define la ruta de VLC:

```python
VLC_PATH = "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
```

Si `ffmpeg` no esta en PATH, instalalo y verifica.

## Instalar ffmpeg (Windows + Chocolatey)

Si no tienes Chocolatey, sigue la guia oficial: https://chocolatey.org/install

Luego instala ffmpeg:

```powershell
choco install ffmpeg -y
```

Verifica:

```powershell
ffmpeg -version
ffprobe -version
```

## Ejecutar

```powershell
python .\convert_video_to_audio.py
```

## Notas

- La aplicacion vigila una carpeta y procesa solo archivos nuevos.
- Si ya existen `*.mp3` y `*.srt` con el mismo nombre base, no reprocesa.
- Los tiempos en la transcripcion son clicables y saltan el reproductor.
