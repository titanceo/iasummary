import os
import re
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app import config
from app.converter import convert_with_progress, transcribe_with_timestamps
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
        self.list_frame: Optional[ttk.LabelFrame] = None

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._init_vlc()
        self._refresh_transcripts()
        self.search_var.trace_add("write", self._on_search_change)

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

        ttk.Label(right_frame, text="Transcripcion").pack(anchor="w", padx=10, pady=(10, 0))
        transcript_controls = ttk.Frame(right_frame)
        transcript_controls.pack(fill="x", padx=10, pady=(6, 0))
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
        self.transcript_text = tk.Text(right_frame, state="normal", wrap="word")
        self.transcript_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.transcript_search_var.trace_add("write", self._on_transcript_search_change)

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

        timestamp_re = re.compile(
            r"^(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})"
        )

        for line in content.splitlines():
            match = timestamp_re.match(line)
            if match:
                start = match.group(1)
                end = match.group(2)
                seconds = self._srt_time_to_seconds(start)
                tag = f"time_{len(self.timestamp_tags)}"
                self.timestamp_tags[tag] = seconds
                self.transcript_text.insert("end", start, (tag,))
                self.transcript_text.insert("end", f" --> {end}\n")
                self.transcript_text.tag_config(tag, foreground="blue", underline=True)
                self.transcript_text.tag_bind(
                    tag, "<Button-1>", lambda _event, key=tag: self._on_timestamp_click(key)
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
            content = self.transcript_text.get("1.0", "end").rstrip() + "\n"
            self.current_transcript_path.write_text(content, encoding="utf-8")
            self._threadsafe_log(f"SRT guardado: {self.current_transcript_path.name}")
        except OSError as exc:
            self._threadsafe_log(f"Error guardando SRT: {exc}")

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
