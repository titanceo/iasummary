import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Event
from typing import Callable, List, Optional

from app import config

ProgressCallback = Callable[[Optional[float], str], None]
ProcessCallback = Callable[[Optional[subprocess.Popen]], None]


def _terminate_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _timestamp_to_seconds(hours: Optional[str], minutes: str, seconds: str) -> float:
    total = int(minutes) * 60 + float(seconds)
    if hours:
        total += int(hours) * 3600
    return total


def convert_with_progress(
    input_path: Path,
    output_path: Path,
    progress_cb: ProgressCallback,
    stop_event: Optional[Event] = None,
    process_cb: Optional[ProcessCallback] = None,
) -> None:
    """
    Convierte video a audio usando ffmpeg y notifica progreso.
    progress_cb(valor, mensaje): valor en 0-100 o None si no hay progreso determinable.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg no esta instalado o no se encuentra en PATH.")

    total_duration = get_duration_seconds(input_path)

    cmd: List[str] = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
    ]

    # Cuando conocemos duracion, pedimos progreso detallado
    if total_duration and total_duration > 0:
        cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error", str(output_path)]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process_cb:
            process_cb(process)
        try:
            for line in process.stdout:  # type: ignore[attr-defined]
                if stop_event and stop_event.is_set():
                    _terminate_process(process)
                    return
                if line.startswith("out_time_ms="):
                    try:
                        ms = float(line.split("=", 1)[1].strip())
                        percent = min(100.0, (ms / 1000.0) / total_duration * 100.0)
                        progress_cb(percent, f"Convirtiendo {input_path.name} ({percent:.0f}%)")
                    except ValueError:
                        pass
            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            progress_cb(100.0, f"Completado {input_path.name} (100%)")
        finally:
            if process_cb:
                process_cb(None)
    else:
        # Sin duracion conocida: barra indeterminada
        progress_cb(None, f"Convirtiendo {input_path.name}")
        process = subprocess.Popen(
            cmd + [str(output_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if process_cb:
            process_cb(process)
        try:
            while True:
                if stop_event and stop_event.is_set():
                    _terminate_process(process)
                    return
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)
            progress_cb(100.0, f"Completado {input_path.name} (100%)")
        finally:
            if process_cb:
                process_cb(None)


def get_duration_seconds(file_path: Path) -> Optional[float]:
    """Obtiene duracion en segundos usando ffprobe (si esta disponible)."""
    if shutil.which("ffprobe") is None:
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    try:
        output = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()
        return float(output)
    except Exception:
        return None


def transcribe_with_timestamps(
    audio_path: Path,
    output_path: Path,
    progress_cb: ProgressCallback,
    stop_event: Optional[Event] = None,
    process_cb: Optional[ProcessCallback] = None,
) -> None:
    """Transcribe audio con timestamps usando whisper CLI."""
    if shutil.which("whisper") is None:
        raise RuntimeError("whisper no esta instalado o no se encuentra en PATH.")

    total_duration = get_duration_seconds(audio_path)
    progress_cb(0.0, f"Transcribiendo {audio_path.name} (0%)")
    cmd: List[str] = [
        "whisper",
        str(audio_path),
        "--model",
        config.WHISPER_MODEL,
        "--output_format",
        output_path.suffix.lstrip("."),
        "--output_dir",
        str(output_path.parent),
        "--verbose",
        "True",
    ]
    if config.WHISPER_LANGUAGE:
        cmd += ["--language", config.WHISPER_LANGUAGE]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if process_cb:
        process_cb(process)
    try:
        last_percent = -1
        timestamp_re = re.compile(
            r"\[(?:(\d{2}):)?(\d{2}):(\d{2}\.\d+)\s*-->\s*(?:(\d{2}):)?(\d{2}):(\d{2}\.\d+)\]"
        )
        for line in process.stdout:  # type: ignore[attr-defined]
            if stop_event and stop_event.is_set():
                _terminate_process(process)
                return
            if config.DEBUG_WHISPER_PROGRESS:
                print(f"[whisper] {line.rstrip()}")
            if not total_duration or total_duration <= 0:
                continue
            match = timestamp_re.search(line)
            if not match:
                continue
            end_seconds = _timestamp_to_seconds(match.group(4), match.group(5), match.group(6))
            percent = min(99, int((end_seconds / total_duration) * 100))
            if percent != last_percent:
                last_percent = percent
                if config.DEBUG_WHISPER_PROGRESS:
                    print(f"[whisper] progress: {percent}%")
                progress_cb(float(percent), f"Transcribiendo {audio_path.name} ({percent}%)")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, cmd)
        progress_cb(100.0, f"Transcripcion completada {audio_path.name} (100%)")
    finally:
        if process_cb:
            process_cb(None)
