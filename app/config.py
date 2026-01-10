VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
AUDIO_EXT = ".mp3"
TRANSCRIPT_EXT = ".srt"
WHISPER_MODEL = "base"
WHISPER_LANGUAGE = ""
SCAN_INTERVAL_MS = 1500
VLC_PATH = "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
DEBUG_WHISPER_PROGRESS = True
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.1:8b"
OLLAMA_EMBED_MODEL = "nomic-embed-text"
LLM_DIRNAME = "LLM"
LLM_TOP_K = 6
LLM_MAX_CHUNK_SECONDS = 20
LLM_MAX_CHUNK_CHARS = 600
ANALYSIS_PROMPT = (
    "Responde SOLO usando la evidencia del TXT adjunto.\n"
    "Para cada punto, incluye el nombre del archivo y el tiempo exacto.\n\n"
    "Formato obligatorio por cada evidencia:\n"
    "nombre_archivo.ext - HH:MM:SS\n"
    "BUSCA: \n\n"
    "Si no hay evidencia, responde: NO ENCONTRADO.\n"
)
