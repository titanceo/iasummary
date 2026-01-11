import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
import wave
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import sounddevice as sd

from app import config
from app.converter import (
    convert_with_progress,
    get_duration_seconds,
    transcribe_with_timestamps,
)
from app.rag import parse_srt_segments
from app.watcher import DirectoryWatcher


class ConverterUI:
    """Interfaz Tkinter: selecciona carpeta, define extensiones y muestra progreso."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video a Audio")
        self.root.state("zoomed")

        self.directory_var = tk.StringVar(value=str(Path.cwd()))
        self.status_var = tk.StringVar(value="Selecciona una carpeta para vigilar.")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.extension_vars: Dict[str, tk.BooleanVar] = {
            ext: tk.BooleanVar(value=True) for ext in config.VIDEO_EXTENSIONS
        }

        self.watching = False
        self.processing = False
        self.watcher = DirectoryWatcher()

        self.stop_event = threading.Event()
        self.active_process: Optional[subprocess.Popen] = None
        self.closing = False

        self.transcript_paths: List[Path] = []
        self.filtered_transcripts: List[Path] = []
        self.search_var = tk.StringVar()
        self.transcript_search_var = tk.StringVar()
        self.timestamp_tags: Dict[str, float] = {}
        self.timestamp_ranges: Dict[str, Tuple[float, float]] = {}
        self.note_tags: Dict[str, str] = {}
        self.annotations: Dict[str, str] = {}
        self.search_hits: List[str] = []
        self.search_index = -1
        self.search_count_label: Optional[ttk.Label] = None

        self.vlc: Optional[Any] = None
        self.vlc_instance: Optional[Any] = None
        self.vlc_player: Optional[Any] = None
        self.player_window: Optional[tk.Toplevel] = None
        self.player_frame: Optional[tk.Frame] = None
        self.player_controls: Optional[ttk.Frame] = None
        self.progress_scale: Optional[tk.Scale] = None
        self.subtitle_var = tk.BooleanVar(value=True)
        self.progress_updater_id: Optional[str] = None
        self.user_seeking = False
        self.current_media_duration_ms = 0
        self.time_label: Optional[ttk.Label] = None
        self.current_audio_path: Optional[Path] = None
        self.current_transcript_path: Optional[Path] = None
        self.current_transcript_content: str = ""
        self.list_frame: Optional[ttk.LabelFrame] = None
        self.analysis_links: Dict[str, Tuple[Path, float]] = {}
        self.analysis_input: Optional[tk.Text] = None
        self.analysis_output: Optional[tk.Text] = None
        self.recording_note = False
        self.recording_stream: Optional[sd.InputStream] = None
        self.recording_frames: List[np.ndarray] = []
        self.recording_timestamp: Optional[float] = None

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_menu()
        self._build_ui()
        self._init_vlc()
        self._refresh_transcripts()
        self.search_var.trace_add("write", self._on_search_change)
        self.transcript_search_var.trace_add("write", self._on_transcript_search_change)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Configuracion...", command=self._open_settings_window)
        menubar.add_cascade(label="Configuracion", menu=settings_menu)
        self.root.config(menu=menubar)

    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left_frame = ttk.Frame(paned)
        right_frame = ttk.Frame(paned)
        paned.add(left_frame, weight=3)
        paned.add(right_frame, weight=2)

        top_frame = ttk.Frame(left_frame, padding=10)
        top_frame.pack(fill="both", expand=True)

        ttk.Label(top_frame, text="Carpeta a vigilar:").pack(anchor="w")
        dir_frame = ttk.Frame(top_frame)
        dir_frame.pack(fill="x", pady=(0, 8))

        entry = ttk.Entry(dir_frame, textvariable=self.directory_var)
        entry.pack(side="left", fill="x", expand=True)

        ttk.Button(dir_frame, text="Buscar...", command=self._choose_directory).pack(
            side="left", padx=(6, 0)
        )

        ext_frame = ttk.LabelFrame(top_frame, text="Extensiones admitidas (selecciona/deselecciona):")
        ext_frame.pack(fill="x", pady=(0, 8))

        for idx, ext in enumerate(self.extension_vars):
            ttk.Checkbutton(ext_frame, text=ext, variable=self.extension_vars[ext]).grid(
                row=idx // 3, column=idx % 3, sticky="w", padx=4, pady=2
            )

        controls = ttk.Frame(top_frame)
        controls.pack(fill="x", pady=(0, 8))
        self.toggle_btn = ttk.Button(controls, text="Iniciar vigilancia", command=self._toggle_watch)
        self.toggle_btn.pack(side="left")

        progress_frame = ttk.Frame(top_frame)
        progress_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=(4, 0))

        log_frame = ttk.LabelFrame(top_frame, text="Actividad")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=6, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        self.list_frame = ttk.LabelFrame(top_frame, text="Transcripciones")
        self.list_frame.pack(fill="both", expand=True, pady=(8, 0))
        search_frame = ttk.Frame(self.list_frame)
        search_frame.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(search_frame, text="Buscar:").pack(side="left")
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.transcript_list = tk.Listbox(self.list_frame, height=10)
        self.transcript_list.pack(fill="both", expand=True)
        self.transcript_list.bind("<<ListboxSelect>>", self._show_transcript)

        notebook = ttk.Notebook(right_frame)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        transcript_tab = ttk.Frame(notebook)
        analysis_tab = ttk.Frame(notebook)
        notebook.add(transcript_tab, text="Transcripcion")
        notebook.add(analysis_tab, text="Analisis")

        transcript_controls = ttk.Frame(transcript_tab)
        transcript_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(transcript_controls, text="Buscar en SRT:").pack(side="left")
        transcript_search = ttk.Entry(transcript_controls, textvariable=self.transcript_search_var)
        transcript_search.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(transcript_controls, text="◀", command=self._search_prev).pack(side="left")
        ttk.Button(transcript_controls, text="▶", command=self._search_next).pack(side="left", padx=(4, 6))
        self.search_count_label = ttk.Label(transcript_controls, text="0/0")
        self.search_count_label.pack(side="left")
        ttk.Button(transcript_controls, text="Guardar SRT", command=self._save_transcript).pack(
            side="right"
        )
        self.transcript_text = tk.Text(transcript_tab, state="normal", wrap="word")
        self.transcript_text.pack(fill="both", expand=True, pady=(6, 0))
        self.transcript_search_var.trace_add("write", self._on_transcript_search_change)

        analysis_controls = ttk.Frame(analysis_tab, padding=10)
        analysis_controls.pack(fill="x")
        ttk.Button(
            analysis_controls, text="Generar TXT", command=self._generate_analysis_txt
        ).pack(side="left")
        ttk.Button(
            analysis_controls, text="Ver prompt", command=self._show_analysis_prompt
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            analysis_controls,
            text="Pega la respuesta de ChatGPT y luego procesa para activar enlaces.",
        ).pack(side="left", padx=(8, 0))

        analysis_paned = ttk.PanedWindow(analysis_tab, orient="vertical")
        analysis_paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        analysis_top = ttk.Frame(analysis_paned)
        analysis_bottom = ttk.Frame(analysis_paned)
        analysis_paned.add(analysis_top, weight=3)
        analysis_paned.add(analysis_bottom, weight=2)

        ttk.Label(analysis_top, text="Respuesta (pegar aqui):").pack(anchor="w")
        self.analysis_input = tk.Text(analysis_top, height=8, wrap="word")
        self.analysis_input.pack(fill="both", expand=True, pady=(4, 6))
        ttk.Button(
            analysis_top, text="Procesar respuesta", command=self._process_analysis_response
        ).pack(anchor="e")

        ttk.Label(analysis_bottom, text="Resultado con enlaces:").pack(anchor="w")
        self.analysis_output = tk.Text(analysis_bottom, state="disabled", wrap="word")
        self.analysis_output.pack(fill="both", expand=True, pady=(4, 0))

    def _init_vlc(self) -> None:
        if not config.VLC_PATH:
            self._log("Configura VLC_PATH en app/config.py para habilitar el reproductor.")
            return

        vlc_path = Path(config.VLC_PATH)
        vlc_dir = vlc_path.parent if vlc_path.is_file() else vlc_path
        if not vlc_dir.exists():
            self._log(f"No se encontro VLC_PATH: {config.VLC_PATH}")
            return

        os.environ["PATH"] = f"{vlc_dir};{os.environ.get('PATH', '')}"
        plugins_dir = vlc_dir / "plugins"
        if plugins_dir.exists():
            os.environ["VLC_PLUGIN_PATH"] = str(plugins_dir)

        try:
            import vlc  # type: ignore
        except Exception as exc:
            self._log(f"No se pudo importar python-vlc: {exc}")
            return

        try:
            self.vlc = vlc
            self.vlc_instance = vlc.Instance("--no-video-title-show")
            self.vlc_player = self.vlc_instance.media_player_new()
        except Exception as exc:
            self._log(f"No se pudo inicializar VLC: {exc}")

    def _ensure_player_window(self) -> bool:
        if not self.vlc_player:
            self._threadsafe_log("Reproductor no disponible. Revisa VLC_PATH y python-vlc.")
            return False
        if self.player_window and self.player_window.winfo_exists():
            return True
        self.player_window = tk.Toplevel(self.root)
        self.player_window.title("Reproductor")
        half_width = max(480, self.root.winfo_screenwidth() // 2)
        height = int(half_width * 9 / 16)
        self.player_window.geometry(f"{half_width}x{height}")
        self.player_window.attributes("-topmost", True)
        self.player_window.transient(self.root)
        self.player_window.protocol("WM_DELETE_WINDOW", self._close_player_window)
        container = ttk.Frame(self.player_window)
        container.pack(fill="both", expand=True)
        self.player_controls = ttk.Frame(container)
        self.player_controls.pack(fill="x", padx=8, pady=6)
        ttk.Button(self.player_controls, text="Play/Pause", command=self._toggle_playback).pack(
            side="left"
        )
        ttk.Button(self.player_controls, text="Stop", command=self._stop_playback).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(self.player_controls, text="Back 10s", command=lambda: self._seek_relative(-10)).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(
            self.player_controls, text="Forward 10s", command=lambda: self._seek_relative(10)
        ).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(
            self.player_controls,
            text="Subtitulos",
            variable=self.subtitle_var,
            command=self._toggle_subtitles,
        ).pack(side="left", padx=(10, 0))
        self.time_label = ttk.Label(self.player_controls, text="00:00 / 00:00")
        self.time_label.pack(side="right")
        self.progress_scale = tk.Scale(
            self.player_controls,
            from_=0,
            to=100,
            orient="horizontal",
            showvalue=0,
            length=260,
        )
        self.progress_scale.pack(side="right", fill="x", expand=True, padx=(8, 8))
        self.progress_scale.bind("<ButtonPress-1>", self._on_progress_press)
        self.progress_scale.bind("<ButtonRelease-1>", self._on_progress_release)
        self.player_frame = tk.Frame(container, bg="black")
        self.player_frame.pack(fill="both", expand=True)
        self._attach_vlc_to_frame()
        self._start_progress_updates()
        return True

    def _close_player_window(self) -> None:
        if self.vlc_player:
            try:
                self.vlc_player.stop()
            except Exception:
                pass
        self._stop_progress_updates()
        if self.player_window and self.player_window.winfo_exists():
            self.player_window.destroy()

    def _attach_vlc_to_frame(self) -> None:
        if not self.vlc_player or not self.player_frame:
            return
        self.root.update_idletasks()
        self.player_frame.update_idletasks()
        try:
            self.vlc_player.set_hwnd(self.player_frame.winfo_id())
        except Exception as exc:
            self._threadsafe_log(f"No se pudo incrustar VLC: {exc}")

    def _start_progress_updates(self) -> None:
        if self.progress_updater_id is not None:
            return
        self.progress_updater_id = self.root.after(500, self._update_progress_scale)

    def _stop_progress_updates(self) -> None:
        if self.progress_updater_id is None:
            return
        try:
            self.root.after_cancel(self.progress_updater_id)
        except Exception:
            pass
        self.progress_updater_id = None

    def _update_progress_scale(self) -> None:
        self.progress_updater_id = None
        if not self.vlc_player or not self.progress_scale:
            return
        try:
            length = self.vlc_player.get_length()
            if length and length > 0:
                self.current_media_duration_ms = length
                if int(self.progress_scale.cget("to")) != length:
                    self.progress_scale.config(to=length)
                if not self.user_seeking:
                    current = self.vlc_player.get_time()
                    if current >= 0:
                        self.progress_scale.set(current)
                        if self.time_label:
                            self.time_label.config(
                                text=f"{self._format_ms(current)} / {self._format_ms(length)}"
                            )
        finally:
            self.progress_updater_id = self.root.after(500, self._update_progress_scale)

    def _on_progress_press(self, _event: tk.Event) -> None:
        self.user_seeking = True

    def _on_progress_release(self, _event: tk.Event) -> None:
        if self.progress_scale and self.vlc_player:
            try:
                self.vlc_player.set_time(int(self.progress_scale.get()))
            except Exception:
                pass
        self.user_seeking = False

    def _seek_relative(self, seconds: int) -> None:
        if not self.vlc_player:
            return
        current = self.vlc_player.get_time()
        if current < 0:
            return
        target = max(0, current + seconds * 1000)
        self.vlc_player.set_time(target)

    def _toggle_playback(self) -> None:
        if not self.vlc_player:
            return
        try:
            if self.vlc_player.is_playing():
                self.vlc_player.pause()
            else:
                self.vlc_player.play()
        except Exception:
            pass

    def _stop_playback(self) -> None:
        if not self.vlc_player:
            return
        try:
            self.vlc_player.stop()
            if self.progress_scale:
                self.progress_scale.set(0)
        except Exception:
            pass

    def _toggle_subtitles(self) -> None:
        if not self.current_audio_path:
            return
        self._apply_subtitle_toggle()

    def _apply_subtitle_toggle(self) -> None:
        if not self.vlc_player:
            return
        if not self.current_transcript_path or not self.current_transcript_path.exists():
            return
        try:
            if self.subtitle_var.get():
                self.vlc_player.video_set_subtitle_file(str(self.current_transcript_path))
            else:
                self.vlc_player.video_set_spu(-1)
        except Exception:
            self._reload_media_with_subtitles()

    def _format_ms(self, value_ms: int) -> str:
        total_seconds = max(0, value_ms // 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.directory_var.get())
        if selected:
            self.directory_var.set(selected)
            self._log(f"Carpeta seleccionada: {selected}")
            self._refresh_transcripts()

    def _toggle_watch(self) -> None:
        if not self.watching:
            self._start_watch()
        else:
            self._stop_watch()

    def _start_watch(self) -> None:
        directory = Path(self.directory_var.get()).expanduser()
        if not directory.exists():
            self.status_var.set(f"Carpeta no encontrada: {directory}")
            return

        self.watching = True
        self.toggle_btn.config(text="Detener vigilancia")
        self.status_var.set(f"Vigilando {directory}")
        self._log(f"Vigilando carpeta: {directory}")
        self._schedule_scan()

    def _stop_watch(self) -> None:
        self.watching = False
        self.toggle_btn.config(text="Iniciar vigilancia")
        self.status_var.set("Vigilancia detenida.")
        self._log("Vigilancia detenida.")

    def _schedule_scan(self) -> None:
        if self.watching:
            self.root.after(config.SCAN_INTERVAL_MS, self._scan_directory)

    def _scan_directory(self) -> None:
        if not self.watching or self.closing:
            return

        directory = Path(self.directory_var.get()).expanduser()
        allowed_exts = self._selected_extensions()
        if not allowed_exts:
            self.status_var.set("Selecciona al menos una extension.")
            self._schedule_scan()
            return

        try:
            self.watcher.scan(directory, allowed_exts)
        except Exception as exc:
            self._log(f"Error al leer la carpeta: {exc}")

        if not self.processing:
            self._start_next_conversion()

        self._schedule_scan()

    def _start_next_conversion(self) -> None:
        if self.stop_event.is_set() or self.closing:
            return

        next_file = self.watcher.next_file()
        if next_file is None:
            self.status_var.set("En espera de un nuevo archivo.")
            return

        self.processing = True
        self.status_var.set(f"Archivo detectado: {next_file.name}")
        created_at = self._format_created_at(next_file)
        self._threadsafe_log("-" * 40)
        self._threadsafe_log(f"Archivo detectado: {next_file.name}")
        self._threadsafe_log(f"Creado: {created_at}")

        desired_name = self._ask_for_name(next_file)
        if desired_name is None:
            self.watcher.mark_processed(next_file)
            self._threadsafe_log(f"Archivo omitido: {next_file.name}")
            self.processing = False
            return

        desired_name = self._with_created_prefix(next_file, desired_name)
        if desired_name != next_file.stem:
            renamed = self._rename_input_file(next_file, desired_name)
            if renamed is None:
                self.watcher.mark_processed(next_file)
                self._threadsafe_log(f"No se pudo renombrar {next_file.name}.")
                self.processing = False
                return
            if renamed != next_file:
                self.watcher.replace_in_progress(next_file, renamed)
                next_file = renamed

        audio_path = next_file.with_suffix(config.AUDIO_EXT)
        transcript_path = audio_path.with_suffix(config.TRANSCRIPT_EXT)
        if audio_path.exists() and transcript_path.exists():
            self.watcher.mark_processed(next_file)
            self._threadsafe_log(f"Ya existe salida para {next_file.name}.")
            self.processing = False
            self.status_var.set("En espera de un nuevo archivo.")
            return

        self._threadsafe_log(f"Archivo: {next_file.name}")
        self._threadsafe_log("Iniciando conversion a audio...")

        def worker() -> None:
            if self.stop_event.is_set() or self.closing:
                return
            try:
                convert_with_progress(
                    next_file,
                    audio_path,
                    progress_cb=self._threadsafe_progress,
                    stop_event=self.stop_event,
                    process_cb=self._set_active_process,
                )
                if self.stop_event.is_set() or self.closing:
                    return
                self._threadsafe_log("Iniciando transcripcion...")
                transcribe_with_timestamps(
                    audio_path,
                    transcript_path,
                    progress_cb=self._threadsafe_progress,
                    stop_event=self.stop_event,
                    process_cb=self._set_active_process,
                )
                if self.stop_event.is_set() or self.closing:
                    return
                self.watcher.mark_processed(next_file)
                self._threadsafe_log(f"Audio listo: {audio_path.name}")
                self._threadsafe_log(f"Transcripcion lista: {transcript_path.name}")
                self._threadsafe_refresh_transcripts()
            except Exception as exc:
                # Marcar como procesado para evitar reintentos infinitos; el usuario puede moverlo y reintentar.
                self.watcher.mark_processed(next_file)
                self._threadsafe_log(f"Error procesando {next_file.name}: {exc}")
            finally:
                if not self.closing:
                    try:
                        self.root.after(0, self._conversion_finished)
                    except tk.TclError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def _conversion_finished(self) -> None:
        if self.closing:
            return
        self.processing = False
        self.progress.stop()
        self.progress_var.set(0.0)
        if self.watching:
            self._start_next_conversion()

    def _selected_extensions(self) -> Iterable[str]:
        return [ext for ext, var in self.extension_vars.items() if var.get()]

    def _set_active_process(self, process: Optional[subprocess.Popen]) -> None:
        self.active_process = process

    def _ask_for_name(self, file_path: Path) -> Optional[str]:
        prompt = "Nombre para salida (sin extension). Deja igual o cambia:"
        name = simpledialog.askstring(
            "Nombre del archivo",
            prompt,
            initialvalue=file_path.stem,
            parent=self.root,
        )
        if name is None:
            return None
        return name.strip() or file_path.stem

    def _rename_input_file(self, file_path: Path, new_base: str) -> Optional[Path]:
        new_path = file_path.with_name(f"{new_base}{file_path.suffix}")
        if new_path == file_path:
            return file_path
        if new_path.exists():
            self._threadsafe_log(f"Ya existe un archivo llamado {new_path.name}.")
            return file_path
        try:
            file_path.rename(new_path)
            self._threadsafe_log(f"Renombrado a: {new_path.name}")
            return new_path
        except OSError as exc:
            self._threadsafe_log(f"Error renombrando {file_path.name}: {exc}")
            return None

    def _with_created_prefix(self, file_path: Path, base_name: str) -> str:
        prefix = self._created_prefix(file_path)
        if base_name.startswith(prefix):
            return base_name
        return f"{prefix}{base_name}"

    def _created_prefix(self, file_path: Path) -> str:
        try:
            created = datetime.fromtimestamp(file_path.stat().st_ctime)
            return created.strftime("%Y-%m-%d_%H%M%S_")
        except OSError:
            return "fecha_desconocida_"

    def _format_created_at(self, file_path: Path) -> str:
        try:
            created = datetime.fromtimestamp(file_path.stat().st_ctime)
            return created.strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            return "desconocido"

    def _refresh_transcripts(self) -> None:
        directory = Path(self.directory_var.get()).expanduser()
        if not directory.exists():
            return
        self.transcript_paths = sorted(
            directory.glob(f"*{config.TRANSCRIPT_EXT}"),
            key=lambda path: path.stat().st_ctime if path.exists() else 0,
            reverse=True,
        )
        self._apply_transcript_filter()

    def _apply_transcript_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        if query:
            self.filtered_transcripts = [
                path for path in self.transcript_paths if query in path.name.lower()
            ]
        else:
            self.filtered_transcripts = list(self.transcript_paths)
        self.transcript_list.delete(0, "end")
        for path in self.filtered_transcripts:
            created_label = self._format_created_at(path)
            self.transcript_list.insert("end", f"{created_label} - {path.name}")

    def _threadsafe_refresh_transcripts(self) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, self._refresh_transcripts)
        except tk.TclError:
            pass

    def _show_transcript(self, _event: object) -> None:
        selection = self.transcript_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.filtered_transcripts):
            return
        path = self.filtered_transcripts[index]
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            content = f"No se pudo leer {path.name}: {exc}"

        self.current_transcript_path = path
        self.current_transcript_content = content
        self._load_annotations()
        media_info = self._find_media_for_transcript(path)
        if media_info:
            media_path, is_video = media_info
            if not is_video:
                self._threadsafe_log(f"No se encontro video, reproduciendo audio: {media_path.name}")
            self._load_media(media_path)
        self._render_transcript(content)

    def _render_transcript(self, content: str) -> None:
        self.transcript_text.config(state="normal")
        self.transcript_text.delete("1.0", "end")
        self.timestamp_tags.clear()
        self.timestamp_ranges.clear()
        self.note_tags.clear()

        timestamp_re = re.compile(
            r"^(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})"
        )

        for line in content.splitlines():
            match = timestamp_re.match(line)
            if match:
                start = match.group(1)
                end = match.group(2)
                seconds = self._srt_time_to_seconds(start)
                end_seconds = self._srt_time_to_seconds(end)
                tag = f"time_{len(self.timestamp_tags)}"
                note_tag = f"note_{len(self.note_tags)}"
                note_key = self._timestamp_key(seconds)
                self.timestamp_tags[tag] = seconds
                self.timestamp_ranges[tag] = (seconds, end_seconds)
                self.note_tags[note_tag] = note_key
                self.transcript_text.insert("end", start, (tag,))
                self.transcript_text.insert("end", f" --> {end}")
                self.transcript_text.insert("end", " ")
                self.transcript_text.insert("end", "●", (note_tag,))
                self.transcript_text.insert("end", "\n")
                self.transcript_text.tag_config(tag, foreground="blue", underline=True)
                self.transcript_text.tag_bind(
                    tag, "<Button-1>", lambda _event, key=tag: self._on_timestamp_click(key)
                )
                self.transcript_text.tag_bind(
                    tag, "<Button-3>", lambda event, key=tag: self._on_timestamp_context(event, key)
                )
                color = "#d32f2f" if note_key in self.annotations else "#9e9e9e"
                self.transcript_text.tag_config(
                    note_tag, foreground=color, underline=True, font="TkDefaultFont 14 bold"
                )
                self.transcript_text.tag_bind(
                    note_tag, "<Button-1>", lambda _event, key=note_key: self._open_annotation_editor(key)
                )
            else:
                self.transcript_text.insert("end", f"{line}\n")

        self._apply_transcript_search()

    def _srt_time_to_seconds(self, timestamp: str) -> float:
        parts = timestamp.replace(",", ".").split(":")
        if len(parts) != 3:
            return 0.0
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _timestamp_key(self, seconds: float) -> str:
        return f"{seconds:.3f}"

    def _annotations_path(self) -> Optional[Path]:
        if not self.current_transcript_path:
            return None
        return self.current_transcript_path.with_suffix(".notes.json")

    def _load_annotations(self) -> None:
        self.annotations = {}
        path = self._annotations_path()
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.annotations = {str(k): str(v) for k, v in data.items()}
        except Exception:
            self.annotations = {}

    def _save_annotations(self) -> None:
        path = self._annotations_path()
        if not path:
            return
        try:
            path.write_text(json.dumps(self.annotations, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._threadsafe_log(f"Error guardando anotaciones: {exc}")

    def _open_annotation_editor(self, note_key: str) -> None:
        if not self.current_transcript_path:
            return
        window = tk.Toplevel(self.root)
        window.title("Anotacion")
        width = 420
        height = 260
        window.geometry(f"{width}x{height}")
        window.transient(self.root)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")

        ttk.Label(window, text=f"Tiempo: {note_key}s").pack(anchor="w", padx=10, pady=(10, 4))
        status_label = ttk.Label(window, text="Estado: listo")
        status_label.pack(anchor="w", padx=10, pady=(0, 4))
        text = tk.Text(window, wrap="word", height=6)
        text.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        existing = self.annotations.get(note_key, "")
        if existing:
            text.insert("end", existing)

        btns = ttk.Frame(window)
        btns.pack(fill="x", padx=10, pady=(0, 10))

        def save_note() -> None:
            value = text.get("1.0", "end").strip()
            if value:
                self.annotations[note_key] = value
            else:
                self.annotations.pop(note_key, None)
            self._save_annotations()
            self._update_note_tag_color(note_key)
            window.destroy()

        def delete_note() -> None:
            self.annotations.pop(note_key, None)
            self._save_annotations()
            self._update_note_tag_color(note_key)
            window.destroy()

        ttk.Button(btns, text="Guardar", command=save_note).pack(side="left")
        record_btn = ttk.Button(btns, text="Grabar nota")
        record_btn.pack(side="left", padx=(6, 0))
        record_btn.config(
            command=lambda key=note_key, btn=record_btn, lbl=status_label, txt=text: (
                self._toggle_record_note(key, btn, lbl, txt)
            )
        )
        ttk.Button(btns, text="Eliminar", command=delete_note).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Cerrar", command=window.destroy).pack(side="right")

    def _toggle_record_note(
        self,
        timestamp_key: Optional[str] = None,
        button: Optional[ttk.Button] = None,
        status_label: Optional[ttk.Label] = None,
        text_widget: Optional[tk.Text] = None,
    ) -> None:
        if self.recording_note:
            self._stop_record_note(button, status_label, text_widget)
        else:
            self._start_record_note(timestamp_key, button, status_label, text_widget)

    def _start_record_note(
        self,
        timestamp_key: Optional[str] = None,
        button: Optional[ttk.Button] = None,
        status_label: Optional[ttk.Label] = None,
        text_widget: Optional[tk.Text] = None,
    ) -> None:
        if not self.current_transcript_path:
            self._threadsafe_log("Selecciona una transcripcion antes de grabar notas.")
            return
        if timestamp_key is not None:
            try:
                self.recording_timestamp = float(timestamp_key)
            except ValueError:
                self._threadsafe_log("Timestamp invalido para la nota.")
                return
        else:
            if not self.vlc_player:
                self._threadsafe_log("Reproductor no disponible para asignar tiempo.")
                return
            current_ms = self.vlc_player.get_time()
            if current_ms is None or current_ms < 0:
                self._threadsafe_log("No se pudo obtener el tiempo actual del reproductor.")
                return
            self.recording_timestamp = current_ms / 1000.0
        self.recording_frames = []
        try:
            self.recording_stream = sd.InputStream(
                samplerate=config.NOTE_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                callback=self._on_record_audio,
            )
            self.recording_stream.start()
        except Exception as exc:
            self.recording_stream = None
            self._threadsafe_log(f"No se pudo iniciar grabacion: {exc}")
            return
        self.recording_note = True
        if button:
            button.config(text="Detener grabacion")
        if status_label:
            status_label.config(text="Estado: grabando...")
        self._threadsafe_log("Grabando nota... vuelve a pulsar para detener.")

    def _on_record_audio(self, indata: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
        self.recording_frames.append(indata.copy())

    def _stop_record_note(
        self,
        button: Optional[ttk.Button] = None,
        status_label: Optional[ttk.Label] = None,
        text_widget: Optional[tk.Text] = None,
    ) -> None:
        if not self.recording_note:
            return
        stream = self.recording_stream
        self.recording_stream = None
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.recording_note = False
        if button:
            button.config(text="Grabar nota")
        if status_label:
            status_label.config(text="Estado: transcribiendo...")
        if not self.recording_frames or self.recording_timestamp is None:
            self._threadsafe_log("Grabacion vacia.")
            if status_label:
                status_label.config(text="Estado: sin audio")
            return
        audio = np.concatenate(self.recording_frames, axis=0)
        threading.Thread(
            target=self._transcribe_recorded_note,
            args=(audio, self.recording_timestamp, status_label, text_widget),
            daemon=True,
        ).start()
        self.recording_frames = []

    def _transcribe_recorded_note(
        self,
        audio: np.ndarray,
        timestamp: float,
        status_label: Optional[ttk.Label],
        text_widget: Optional[tk.Text],
    ) -> None:
        if not self.current_transcript_path:
            return
        root_dir = self.current_transcript_path.parent
        llm_dir = root_dir / config.LLM_DIRNAME
        llm_dir.mkdir(parents=True, exist_ok=True)
        stem = self.current_transcript_path.stem
        safe_ts = int(timestamp * 1000)
        audio_path = llm_dir / f"note_{stem}_{safe_ts}.wav"
        text_path = audio_path.with_suffix(".txt")
        try:
            self._write_wav(audio_path, audio, config.NOTE_SAMPLE_RATE)
        except Exception as exc:
            self._threadsafe_log(f"No se pudo guardar audio: {exc}")
            if status_label:
                self.root.after(0, status_label.config, {"text": "Estado: error al guardar"})
            return
        try:
            self._run_whisper_note(audio_path, llm_dir)
            if not text_path.exists():
                self._threadsafe_log("No se encontro texto transcrito.")
                if status_label:
                    self.root.after(0, status_label.config, {"text": "Estado: sin transcripcion"})
                return
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:
            self._threadsafe_log(f"Error transcribiendo nota: {exc}")
            if status_label:
                self.root.after(0, status_label.config, {"text": "Estado: error al transcribir"})
            return
        if not text:
            self._threadsafe_log("Transcripcion vacia.")
            if status_label:
                self.root.after(0, status_label.config, {"text": "Estado: transcripcion vacia"})
            return
        note_key = self._timestamp_key(timestamp)
        self.annotations[note_key] = text
        self._save_annotations()
        self._update_note_tag_color(note_key)
        self._threadsafe_log("Nota de voz guardada.")
        if status_label:
            self.root.after(0, status_label.config, {"text": "Estado: nota guardada"})
        if text_widget:
            def fill_text() -> None:
                text_widget.delete("1.0", "end")
                text_widget.insert("end", text)
            self.root.after(0, fill_text)

    def _write_wav(self, path: Path, audio: np.ndarray, sample_rate: int) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio.tobytes())

    def _run_whisper_note(self, audio_path: Path, output_dir: Path) -> None:
        if shutil.which("whisper") is None:
            raise RuntimeError("whisper no esta instalado o no se encuentra en PATH.")
        cmd: List[str] = [
            "whisper",
            str(audio_path),
            "--model",
            config.WHISPER_MODEL,
            "--output_format",
            "txt",
            "--output_dir",
            str(output_dir),
            "--verbose",
            "False",
        ]
        if config.WHISPER_LANGUAGE:
            cmd += ["--language", config.WHISPER_LANGUAGE]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _update_note_tag_color(self, note_key: str) -> None:
        for tag, key in self.note_tags.items():
            if key == note_key:
                color = "#2e7d32" if note_key in self.annotations else "#9e9e9e"
                self.transcript_text.tag_config(tag, foreground=color)
                break

    def _find_media_for_transcript(self, transcript_path: Path) -> Optional[Tuple[Path, bool]]:
        directory = transcript_path.parent
        base_name = transcript_path.stem
        for ext in config.VIDEO_EXTENSIONS:
            candidate = directory / f"{base_name}{ext}"
            if candidate.exists():
                return candidate, True
        audio_path = directory / f"{base_name}{config.AUDIO_EXT}"
        if audio_path.exists():
            return audio_path, False
        return None

    def _load_media(self, media_path: Path) -> None:
        if not self.vlc_player or not self.vlc_instance:
            return
        if not media_path.exists():
            self._threadsafe_log(f"No se encontro el archivo: {media_path.name}")
            return
        if self.current_audio_path == media_path:
            return
        media = self.vlc_instance.media_new(str(media_path))
        self._attach_subtitles(media)
        self.vlc_player.set_media(media)
        self.current_audio_path = media_path
        self._apply_subtitle_toggle()

    def _attach_subtitles(self, media: Any) -> None:
        if not self.subtitle_var.get():
            return
        subtitle_path = None
        if self.current_transcript_path and self.current_transcript_path.exists():
            subtitle_path = self.current_transcript_path
        if not subtitle_path:
            return
        try:
            if hasattr(media, "add_slave"):
                media.add_slave(self.vlc.MediaSlaveType.subtitle, str(subtitle_path), True)
            else:
                media.add_option(f"sub-file={subtitle_path}")
        except Exception:
            pass

    def _reload_media_with_subtitles(self) -> None:
        if not self.vlc_player or not self.vlc_instance or not self.current_audio_path:
            return
        current_time = self.vlc_player.get_time()
        media = self.vlc_instance.media_new(str(self.current_audio_path))
        if self.subtitle_var.get():
            self._attach_subtitles(media)
        self.vlc_player.set_media(media)
        self.vlc_player.play()
        if current_time > 0:
            self.vlc_player.set_time(current_time)

    def _on_timestamp_click(self, tag: str) -> None:
        seconds = self.timestamp_tags.get(tag)
        if seconds is None:
            return
        if not self._ensure_player_window():
            return
        self.vlc_player.play()
        self.root.after(200, lambda: self._seek_to(seconds))

    def _on_timestamp_context(self, event: tk.Event, tag: str) -> None:
        if self.closing:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Ir a este tiempo", command=lambda: self._on_timestamp_click(tag))
        menu.add_command(label="Eliminar esta seccion", command=lambda: self._confirm_cut_segment(tag))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _confirm_cut_segment(self, tag: str) -> None:
        if not self.current_transcript_path:
            self._threadsafe_log("Selecciona un SRT para recortar.")
            return
        if tag not in self.timestamp_ranges:
            self._threadsafe_log("No se pudo obtener el rango del SRT.")
            return
        start, end = self.timestamp_ranges[tag]
        if end <= start:
            self._threadsafe_log("Rango de recorte invalido.")
            return
        start_label = self._format_seconds_hhmmss(start)
        end_label = self._format_seconds_hhmmss(end)
        prompt = (
            f"Se cortara la seccion del {start_label} al {end_label}.\n\n"
            "Se actualizaran el video/audio asociados y el SRT. "
            "Se guardara un .bak de cada archivo antes del cambio.\n\n"
            "¿Continuar?"
        )
        if not messagebox.askyesno("Confirmar recorte", prompt):
            return
        threading.Thread(target=self._cut_segment_worker, args=(start, end), daemon=True).start()

    def _cut_segment_worker(self, start: float, end: float) -> None:
        if self.closing or not self.current_transcript_path:
            return
        transcript_path = self.current_transcript_path
        delta = end - start
        if delta <= 0:
            self._threadsafe_log("Rango de recorte invalido.")
            return
        self._threadsafe_log(
            f"Cortando segmento {self._format_seconds_hhmmss(start)} -> {self._format_seconds_hhmmss(end)}..."
        )
        try:
            self.root.after(0, self._stop_playback)
        except Exception:
            pass

        media_info = self._find_media_for_transcript(transcript_path)
        audio_path = transcript_path.with_suffix(config.AUDIO_EXT)
        targets: List[Tuple[Path, bool]] = []
        if media_info:
            targets.append(media_info)
        if audio_path.exists() and (not media_info or audio_path != media_info[0]):
            targets.append((audio_path, False))
        if not targets:
            self._threadsafe_log("No se encontro video ni audio para recortar.")
            return

        errors: List[str] = []
        for path, is_video in targets:
            try:
                self._cut_media_segment(path, is_video, start, end)
                self._threadsafe_log(f"Recorte aplicado: {path.name}")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        try:
            self._cut_srt_file(transcript_path, start, end)
            self._threadsafe_log(f"SRT actualizado: {transcript_path.name}")
        except Exception as exc:
            errors.append(f"SRT: {exc}")

        try:
            self._shift_notes_after_cut(start, end)
        except Exception as exc:
            self._threadsafe_log(f"No se pudieron ajustar notas: {exc}")

        if errors:
            for msg in errors:
                self._threadsafe_log(f"Error recortando: {msg}")
        else:
            self._threadsafe_log("Recorte finalizado.")

        self._threadsafe_refresh_transcripts()
        try:
            self.root.after(0, self._reload_after_cut)
        except Exception:
            pass

    def _cut_media_segment(self, path: Path, is_video: bool, start: float, end: float) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg no esta instalado o no se encuentra en PATH.")

        duration = get_duration_seconds(path)
        safe_start = max(0.0, start)
        safe_end = max(safe_start, end)
        if duration:
            safe_start = min(safe_start, duration)
            safe_end = min(safe_end, duration)
            if safe_start >= safe_end:
                raise RuntimeError("Rango de recorte fuera de los limites del archivo.")
            if safe_end - safe_start >= duration - 0.05:
                raise RuntimeError("El rango eliminaria todo el archivo.")

        temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
        backup_path = path.with_suffix(path.suffix + ".bak")
        if temp_path.exists():
            temp_path.unlink()

        has_video = self._probe_has_stream(path, "v", default=is_video)
        has_audio = self._probe_has_stream(path, "a", default=not is_video or True)

        if not has_video and not has_audio:
            raise RuntimeError("No se encontraron streams en el archivo.")

        cut_front = safe_start <= 0.05
        cut_tail = duration is not None and (duration - safe_end) <= 0.05
        filter_parts: List[str] = []
        map_parts: List[str] = []
        audio_codec = self._audio_codec_for_ext(path)

        if cut_front:
            if has_video:
                filter_parts.append(f"[0:v]trim={safe_end},setpts=PTS-STARTPTS[v_keep]")
                map_parts.extend(["-map", "[v_keep]"])
            if has_audio:
                filter_parts.append(f"[0:a]atrim={safe_end},asetpts=PTS-STARTPTS[a_keep]")
                map_parts.extend(["-map", "[a_keep]"])
        elif cut_tail:
            if has_video:
                filter_parts.append(f"[0:v]trim=0:{safe_start},setpts=PTS-STARTPTS[v_keep]")
                map_parts.extend(["-map", "[v_keep]"])
            if has_audio:
                filter_parts.append(f"[0:a]atrim=0:{safe_start},asetpts=PTS-STARTPTS[a_keep]")
                map_parts.extend(["-map", "[a_keep]"])
        else:
            if has_video:
                filter_parts.append(f"[0:v]trim=0:{safe_start},setpts=PTS-STARTPTS[v0]")
                filter_parts.append(f"[0:v]trim={safe_end},setpts=PTS-STARTPTS[v1]")
            if has_audio:
                filter_parts.append(f"[0:a]atrim=0:{safe_start},asetpts=PTS-STARTPTS[a0]")
                filter_parts.append(f"[0:a]atrim={safe_end},asetpts=PTS-STARTPTS[a1]")

            if has_video and has_audio:
                filter_parts.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]")
                map_parts.extend(["-map", "[outv]", "-map", "[outa]"])
            elif has_video:
                filter_parts.append("[v0][v1]concat=n=2:v=1[outv]")
                map_parts.extend(["-map", "[outv]"])
            else:
                filter_parts.append("[a0][a1]concat=n=2:v=0:a=1[outa]")
                map_parts.extend(["-map", "[outa]"])

        cmd: List[str] = ["ffmpeg", "-y", "-i", str(path)]
        if filter_parts:
            cmd.extend(["-filter_complex", ";".join(filter_parts)])
        if has_video:
            cmd.extend(["-c:v", "libx264", "-preset", "veryfast"])
        if has_audio:
            cmd.extend(["-c:a", audio_codec])
        if not map_parts:
            raise RuntimeError("No se generaron streams de salida para recortar.")
        cmd.extend(map_parts)
        out_format = self._ffmpeg_format_for_ext(path)
        if out_format:
            cmd.extend(["-f", out_format])
        cmd.append(str(temp_path))

        try:
            result = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode != 0:
                tail = "\n".join(result.stderr.splitlines()[-10:])
                temp_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"ffmpeg fallo (code {result.returncode}). Ultimas lineas:\n{tail}"
                )
        except subprocess.CalledProcessError as exc:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg fallo: {exc}") from exc

        try:
            path.replace(backup_path)
            temp_path.replace(path)
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise

    def _probe_has_stream(self, path: Path, selector: str, default: bool = True) -> bool:
        if shutil.which("ffprobe") is None:
            return default
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
            return bool(output)
        except Exception:
            return default

    def _audio_codec_for_ext(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".mp3":
            return "libmp3lame"
        if ext in {".wav", ".wave"}:
            return "pcm_s16le"
        if ext in {".aac", ".m4a"}:
            return "aac"
        return "aac"

    def _ffmpeg_format_for_ext(self, path: Path) -> Optional[str]:
        ext = path.suffix.lower()
        if ext in {".mp4", ".m4v"}:
            return "mp4"
        if ext == ".mkv":
            return "matroska"
        if ext == ".webm":
            return "webm"
        if ext == ".mov":
            return "mov"
        if ext == ".avi":
            return "avi"
        if ext == ".mp3":
            return "mp3"
        if ext in {".wav", ".wave"}:
            return "wav"
        if ext == ".aac":
            return "adts"
        return None

    def _cut_srt_file(self, path: Path, start: float, end: float) -> None:
        segments = parse_srt_segments(path)
        if not segments:
            raise RuntimeError("SRT vacio.")
        delta = end - start
        eps = 0.001
        new_segments: List[Tuple[float, float, List[str]]] = []
        for segment in segments:
            seg_start = segment["start"]
            seg_end = segment["end"]
            if seg_end <= start + eps:
                new_segments.append((seg_start, seg_end, segment["text"]))
            elif seg_start >= end - eps:
                new_segments.append((seg_start - delta, seg_end - delta, segment["text"]))
            else:
                continue

        if not new_segments:
            raise RuntimeError("El recorte eliminaria todo el SRT.")

        lines: List[str] = []
        for idx, (seg_start, seg_end, texts) in enumerate(new_segments, start=1):
            lines.append(str(idx))
            lines.append(f"{self._format_srt_time(seg_start)} --> {self._format_srt_time(seg_end)}")
            lines.extend(texts)
            lines.append("")

        temp_path = path.with_suffix(path.suffix + ".tmp")
        backup_path = path.with_suffix(path.suffix + ".bak")
        temp_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        path.replace(backup_path)
        temp_path.replace(path)

    def _format_srt_time(self, value: float) -> str:
        if value < 0:
            value = 0.0
        hours = int(value // 3600)
        minutes = int((value % 3600) // 60)
        seconds = int(value % 60)
        millis = int(round((value - int(value)) * 1000))
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    def _shift_notes_after_cut(self, start: float, end: float) -> None:
        delta = end - start
        if delta <= 0:
            return
        self._load_annotations()
        if not self.annotations:
            return
        updated: Dict[str, str] = {}
        eps = 0.001
        for key, text in self.annotations.items():
            try:
                ts = float(key)
            except (TypeError, ValueError):
                continue
            if start - eps <= ts <= end + eps:
                continue
            if ts > end:
                ts -= delta
            updated[self._timestamp_key(ts)] = text
        self.annotations = updated
        self._save_annotations()

    def _reload_after_cut(self) -> None:
        if not self.current_transcript_path or self.closing:
            return
        try:
            content = self.current_transcript_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._threadsafe_log(f"No se pudo recargar {self.current_transcript_path.name}: {exc}")
            return
        self.current_transcript_content = content
        self._load_annotations()
        media_info = self._find_media_for_transcript(self.current_transcript_path)
        if media_info:
            self._load_media(media_info[0])
        self._render_transcript(content)

    def _seek_to(self, seconds: float) -> None:
        if not self.vlc_player:
            return
        self.vlc_player.set_time(int(seconds * 1000))

    def _on_search_change(self, *_args: object) -> None:
        self._apply_transcript_filter()

    def _on_transcript_search_change(self, *_args: object) -> None:
        self._apply_transcript_search()

    def _apply_transcript_search(self) -> None:
        query = self.transcript_search_var.get()
        self.transcript_text.tag_remove("search_hit", "1.0", "end")
        self.transcript_text.tag_remove("search_current", "1.0", "end")
        self.search_hits = []
        self.search_index = -1
        if not query:
            if self.search_count_label:
                self.search_count_label.config(text="0/0")
            return
        start = "1.0"
        while True:
            idx = self.transcript_text.search(query, start, stopindex="end", nocase=True)
            if not idx:
                break
            end = f"{idx}+{len(query)}c"
            self.transcript_text.tag_add("search_hit", idx, end)
            self.search_hits.append(idx)
            start = end
        self.transcript_text.tag_config("search_hit", background="#fff59d")
        self.transcript_text.tag_config("search_current", background="#ffcc80")
        if self.search_hits:
            self.search_index = 0
            self._focus_search_hit()
        if self.search_count_label:
            total = len(self.search_hits)
            current = self.search_index + 1 if total else 0
            self.search_count_label.config(text=f"{current}/{total}")

    def _open_settings_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Configuracion")
        width = 520
        height = 220
        window.geometry(f"{width}x{height}")
        window.transient(self.root)
        window.grab_set()
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Ruta de VLC:").pack(anchor="w")
        vlc_var = tk.StringVar(value=config.VLC_PATH)
        entry = ttk.Entry(frame, textvariable=vlc_var)
        entry.pack(fill="x", pady=(4, 8))

        def browse_vlc() -> None:
            path = filedialog.askopenfilename(
                title="Seleccionar VLC",
                filetypes=[("VLC", "vlc.exe"), ("Todos", "*.*")],
            )
            if path:
                vlc_var.set(path)

        def save_settings() -> None:
            config.VLC_PATH = vlc_var.get().strip()
            self._init_vlc()
            self._threadsafe_log("Configuracion actualizada: VLC_PATH.")
            window.destroy()

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Buscar...", command=browse_vlc).pack(side="left")
        ttk.Button(btns, text="Guardar", command=save_settings).pack(side="right")
        ttk.Button(btns, text="Cancelar", command=window.destroy).pack(side="right", padx=(0, 6))

    def _focus_search_hit(self) -> None:
        if not self.search_hits or self.search_index < 0:
            return
        idx = self.search_hits[self.search_index]
        end = f"{idx}+{len(self.transcript_search_var.get())}c"
        self.transcript_text.tag_remove("search_current", "1.0", "end")
        self.transcript_text.tag_add("search_current", idx, end)
        self.transcript_text.see(idx)

    def _search_next(self) -> None:
        if not self.search_hits:
            return
        self.search_index = (self.search_index + 1) % len(self.search_hits)
        self._focus_search_hit()
        if self.search_count_label:
            self.search_count_label.config(text=f"{self.search_index + 1}/{len(self.search_hits)}")

    def _search_prev(self) -> None:
        if not self.search_hits:
            return
        self.search_index = (self.search_index - 1) % len(self.search_hits)
        self._focus_search_hit()
        if self.search_count_label:
            self.search_count_label.config(text=f"{self.search_index + 1}/{len(self.search_hits)}")

    def _save_transcript(self) -> None:
        if not self.current_transcript_path:
            self._threadsafe_log("No hay transcripcion seleccionada para guardar.")
            return
        try:
            raw = self.transcript_text.get("1.0", "end")
            content = self._strip_note_icons(raw).rstrip() + "\n"
            self.current_transcript_path.write_text(content, encoding="utf-8")
            self.current_transcript_content = content
            self._threadsafe_log(f"SRT guardado: {self.current_transcript_path.name}")
        except OSError as exc:
            self._threadsafe_log(f"Error guardando SRT: {exc}")

    def _strip_note_icons(self, text: str) -> str:
        cleaned_lines: List[str] = []
        for line in text.splitlines():
            if line.endswith(" ●"):
                cleaned_lines.append(line[:-2])
            elif line.endswith("●"):
                cleaned_lines.append(line[:-1])
            else:
                cleaned_lines.append(line)
        return "\n".join(cleaned_lines)

    def _generate_analysis_txt(self) -> None:
        root_dir = Path(self.directory_var.get()).expanduser()
        if not root_dir.exists():
            self._threadsafe_log(f"Carpeta no encontrada: {root_dir}")
            return
        srt_paths = sorted(root_dir.glob(f"*{config.TRANSCRIPT_EXT}"))
        if not srt_paths:
            self._threadsafe_log("No se encontraron SRT para exportar.")
            return
        self._open_srt_selection_window(srt_paths)

    def _open_srt_selection_window(self, srt_paths: List[Path]) -> None:
        window = tk.Toplevel(self.root)
        window.title("Seleccionar archivos")
        width = 520
        height = 420
        window.geometry(f"{width}x{height}")
        window.transient(self.root)
        window.grab_set()
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")

        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Selecciona los SRT a incluir:").pack(anchor="w")

        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, pady=(6, 6))
        listbox = tk.Listbox(list_frame, selectmode="extended")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.config(yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for path in srt_paths:
            listbox.insert("end", path.name)
        listbox.select_set(0, "end")

        def select_all() -> None:
            listbox.select_set(0, "end")

        def select_none() -> None:
            listbox.selection_clear(0, "end")

        def confirm() -> None:
            selection = listbox.curselection()
            if not selection:
                self._threadsafe_log("No se seleccionaron archivos.")
                return
            chosen = [srt_paths[idx] for idx in selection]
            window.destroy()
            self._generate_analysis_txt_for_paths(chosen)

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Seleccionar todo", command=select_all).pack(side="left")
        ttk.Button(btns, text="Limpiar", command=select_none).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Cancelar", command=window.destroy).pack(side="right")
        ttk.Button(btns, text="Generar", command=confirm).pack(side="right", padx=(0, 6))

    def _generate_analysis_txt_for_paths(self, srt_paths: List[Path]) -> None:
        root_dir = Path(self.directory_var.get()).expanduser()
        llm_dir = root_dir / config.LLM_DIRNAME
        llm_dir.mkdir(parents=True, exist_ok=True)
        output_path = llm_dir / "analysis_source.txt"
        blocks: List[str] = []
        for path in srt_paths:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self._threadsafe_log(f"No se pudo leer {path.name}: {exc}")
                continue
            blocks.append(f"ARCHIVO: {path.name}")
            blocks.extend(self._clean_srt_lines(content))
            notes = self._load_notes_for_transcript(path)
            if notes:
                blocks.append("NOTAS:")
                for seconds, text in notes:
                    time_label = self._format_seconds_hhmmss(seconds)
                    blocks.append(f"NOTA {time_label} {text}")
            blocks.append("")
        output_path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
        self._threadsafe_log(f"TXT generado: {output_path}")

    def _clean_srt_lines(self, content: str) -> List[str]:
        cleaned: List[str] = []
        timestamp_re = re.compile(
            r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}$"
        )
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.isdigit():
                continue
            if timestamp_re.match(line):
                cleaned.append(line)
            else:
                cleaned.append(" ".join(line.split()))
        return cleaned

    def _load_notes_for_transcript(self, transcript_path: Path) -> List[Tuple[float, str]]:
        notes_path = transcript_path.with_suffix(".notes.json")
        if not notes_path.exists():
            return []
        try:
            data = json.loads(notes_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        notes: List[Tuple[float, str]] = []
        for key, value in data.items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            try:
                seconds = float(key)
            except (TypeError, ValueError):
                continue
            notes.append((seconds, text))
        notes.sort(key=lambda item: item[0])
        return notes

    def _format_seconds_hhmmss(self, seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _process_analysis_response(self) -> None:
        if not self.analysis_input or not self.analysis_output:
            return
        raw = self.analysis_input.get("1.0", "end").strip()
        if not raw:
            return
        self.analysis_links = {}
        self.analysis_output.config(state="normal")
        self.analysis_output.delete("1.0", "end")
        pattern = re.compile(r"^(?P<file>.+?)\s*-\s*(?P<time>\d{2}:\d{2}:\d{2})\b")
        for line in raw.splitlines():
            match = pattern.match(line.strip())
            if not match:
                self.analysis_output.insert("end", f"{line}\n")
                continue
            file_name = match.group("file").strip()
            time_str = match.group("time")
            seconds = self._analysis_time_to_seconds(time_str)
            transcript = self._analysis_find_transcript(file_name)
            if transcript:
                tag = f"analysis_{len(self.analysis_links)}"
                self.analysis_links[tag] = (transcript, seconds)
                self.analysis_output.insert("end", f"{line}\n", (tag,))
                self.analysis_output.tag_config(tag, foreground="blue", underline=True)
                self.analysis_output.tag_bind(
                    tag, "<Button-1>", lambda _event, key=tag: self._on_analysis_link_click(key)
                )
            else:
                self.analysis_output.insert("end", f"{line}\n")
        self.analysis_output.see("end")
        self.analysis_output.config(state="disabled")

    def _show_analysis_prompt(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Prompt de Analisis")
        width = 640
        height = 420
        window.geometry(f"{width}x{height}")
        window.transient(self.root)
        window.grab_set()
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")

        ttk.Label(window, text="Prompt sugerido:").pack(anchor="w", padx=10, pady=(10, 4))
        text = tk.Text(window, wrap="word")
        text.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        text.insert("end", config.ANALYSIS_PROMPT)
        text.config(state="disabled")

        ttk.Button(window, text="Cerrar", command=window.destroy).pack(pady=(0, 10))

    def _analysis_time_to_seconds(self, value: str) -> float:
        parts = value.split(":")
        if len(parts) != 3:
            return 0.0
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _analysis_find_transcript(self, file_name: str) -> Optional[Path]:
        root_dir = Path(self.directory_var.get()).expanduser()
        candidate = root_dir / file_name
        if candidate.exists():
            if candidate.suffix.lower() == config.TRANSCRIPT_EXT:
                return candidate
            transcript = candidate.with_suffix(config.TRANSCRIPT_EXT)
            if transcript.exists():
                return transcript
        stem = Path(file_name).stem
        for path in root_dir.glob(f"*{config.TRANSCRIPT_EXT}"):
            if path.stem == stem:
                return path
        return None

    def _on_analysis_link_click(self, tag: str) -> None:
        link = self.analysis_links.get(tag)
        if not link:
            return
        transcript_path, seconds = link
        self._open_transcript_at(transcript_path, seconds)

    def _open_transcript_at(self, transcript_path: Path, seconds: float) -> None:
        if not transcript_path.exists():
            self._threadsafe_log(f"No se encontro la transcripcion: {transcript_path.name}")
            return
        try:
            content = transcript_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._threadsafe_log(f"No se pudo leer {transcript_path.name}: {exc}")
            return
        self.current_transcript_path = transcript_path
        self.current_transcript_content = content
        self._load_annotations()
        media_info = self._find_media_for_transcript(transcript_path)
        if media_info:
            media_path, is_video = media_info
            if not is_video:
                self._threadsafe_log(f"No se encontro video, reproduciendo audio: {media_path.name}")
            self._load_media(media_path)
        self._render_transcript(content)
        if not self._ensure_player_window():
            return
        self.vlc_player.play()
        self.root.after(200, lambda: self._seek_to(seconds))

    def _on_close(self) -> None:
        self.closing = True
        self.watching = False
        self.stop_event.set()

        process = self.active_process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

        if self.vlc_player:
            try:
                self.vlc_player.stop()
            except Exception:
                pass
        self._stop_progress_updates()
        if self.player_window and self.player_window.winfo_exists():
            try:
                self.player_window.destroy()
            except Exception:
                pass

        self.root.destroy()

    # Metodos thread-safe para actualizar UI desde hilos de trabajo
    def _threadsafe_progress(self, value: Optional[float], message: str) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, self._update_progress_ui, value, message)
        except tk.TclError:
            pass

    def _threadsafe_log(self, message: str) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, self._log, message)
        except tk.TclError:
            pass

    def _update_progress_ui(self, value: Optional[float], message: str) -> None:
        self.status_var.set(message)
        if value is None:
            self.progress.config(mode="indeterminate")
            self.progress.start(20)
        else:
            if str(self.progress["mode"]) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress_var.set(value)
            self.progress["value"] = value

    def _log(self, message: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
