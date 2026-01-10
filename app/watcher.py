import queue
from pathlib import Path
from typing import Iterable, Optional, Set

from app import config


class DirectoryWatcher:
    """Vigila una carpeta por polling simple y encola archivos nuevos que coincidan."""

    def __init__(self) -> None:
        self.processed: Set[Path] = set()
        self.enqueued: Set[Path] = set()
        self.in_progress: Set[Path] = set()
        self.pending: queue.Queue[Path] = queue.Queue()

    def scan(self, directory: Path, allowed_exts: Iterable[str]) -> None:
        for file in directory.iterdir():
            if (
                file.is_file()
                and file.suffix.lower() in allowed_exts
                and file not in self.processed
                and file not in self.enqueued
                and file not in self.in_progress
            ):
                audio_path = file.with_suffix(config.AUDIO_EXT)
                transcript_path = audio_path.with_suffix(config.TRANSCRIPT_EXT)
                if audio_path.exists() and transcript_path.exists():
                    self.processed.add(file)
                    continue
                self.pending.put(file)
                self.enqueued.add(file)

    def next_file(self) -> Optional[Path]:
        if self.pending.empty():
            return None
        file = self.pending.get()
        self.enqueued.discard(file)
        self.in_progress.add(file)
        return file

    def mark_processed(self, file: Path) -> None:
        self.processed.add(file)
        self.enqueued.discard(file)
        self.in_progress.discard(file)

    def replace_in_progress(self, old_path: Path, new_path: Path) -> None:
        if old_path in self.in_progress:
            self.in_progress.discard(old_path)
            self.in_progress.add(new_path)
        if old_path in self.enqueued:
            self.enqueued.discard(old_path)
            self.enqueued.add(new_path)
