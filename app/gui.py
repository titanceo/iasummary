import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk
from typing import Dict, Iterable, List, Optional

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

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
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

        ttk.Button(controls, text="Salir", command=self._on_close).pack(side="right")

        progress_frame = ttk.Frame(top_frame)
        progress_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w")
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", pady=(4, 0))

        log_frame = ttk.LabelFrame(top_frame, text="Actividad")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=6, state="disabled")
        self.log_text.pack(fill="both", expand=True)

        list_frame = ttk.LabelFrame(top_frame, text="Transcripciones")
        list_frame.pack(fill="both", expand=False, pady=(8, 0))
        search_frame = ttk.Frame(list_frame)
        search_frame.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(search_frame, text="Buscar:").pack(side="left")
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.transcript_list = tk.Listbox(list_frame, height=10)
        self.transcript_list.pack(fill="both", expand=True)
        self.transcript_list.bind("<<ListboxSelect>>", self._show_transcript)

        ttk.Label(right_frame, text="Transcripcion").pack(anchor="w", padx=10, pady=(10, 0))
        self.transcript_text = tk.Text(right_frame, state="disabled", wrap="word")
        self.transcript_text.pack(fill="both", expand=True, padx=10, pady=10)

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
        self._threadsafe_log("-" * 40)
        self._threadsafe_log(f"Archivo detectado: {next_file.name}")

        desired_name = self._ask_for_name(next_file)
        if desired_name is None:
            self.watcher.mark_processed(next_file)
            self._threadsafe_log(f"Archivo omitido: {next_file.name}")
            self.processing = False
            return

        if desired_name and desired_name != next_file.stem:
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

    def _refresh_transcripts(self) -> None:
        directory = Path(self.directory_var.get()).expanduser()
        if not directory.exists():
            return
        self.transcript_paths = sorted(directory.glob(f"*{config.TRANSCRIPT_EXT}"))
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
            self.transcript_list.insert("end", path.name)

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
        self.transcript_text.config(state="normal")
        self.transcript_text.delete("1.0", "end")
        self.transcript_text.insert("end", content)
        self.transcript_text.config(state="disabled")

    def _on_search_change(self, *_args: object) -> None:
        self._apply_transcript_filter()

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
