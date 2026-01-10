import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from app import config

ProgressCallback = Callable[[str], None]


def parse_srt_segments(path: Path) -> List[Dict[str, Any]]:
    timestamp_re = (
        r"^(?P<start>\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*"
        r"(?P<end>\d{2}:\d{2}:\d{2}[,\.]\d{3})"
    )
    segments: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            if current and current["text"]:
                segments.append(current)
            current = None
            continue
        match = re.match(timestamp_re, line)
        if match:
            if current and current["text"]:
                segments.append(current)
            current = {
                "start": srt_time_to_seconds(match.group("start")),
                "end": srt_time_to_seconds(match.group("end")),
                "text": [],
            }
            continue
        if line.isdigit():
            continue
        if current is not None:
            current["text"].append(line)
    if current and current["text"]:
        segments.append(current)
    return segments


def srt_time_to_seconds(timestamp: str) -> float:
    parts = timestamp.replace(",", ".").split(":")
    if len(parts) != 3:
        return 0.0
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def seconds_to_hhmmss(value: float) -> str:
    total = max(0, int(value))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def chunk_segments(
    segments: Iterable[Dict[str, Any]],
    max_seconds: int,
    max_chars: int,
) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    current_text: List[str] = []
    current_start: Optional[float] = None
    current_end: Optional[float] = None
    for segment in segments:
        text = " ".join(segment["text"]).strip()
        if not text:
            continue
        if current_start is None:
            current_start = segment["start"]
            current_end = segment["end"]
            current_text = [text]
            continue
        proposed_end = segment["end"]
        proposed_text = " ".join(current_text + [text])
        if (
            proposed_end - current_start > max_seconds
            or len(proposed_text) > max_chars
        ):
            chunks.append(
                {
                    "start": current_start,
                    "end": current_end if current_end is not None else current_start,
                    "text": " ".join(current_text).strip(),
                }
            )
            current_start = segment["start"]
            current_end = segment["end"]
            current_text = [text]
        else:
            current_end = proposed_end
            current_text.append(text)
    if current_text and current_start is not None:
        chunks.append(
            {
                "start": current_start,
                "end": current_end if current_end is not None else current_start,
                "text": " ".join(current_text).strip(),
            }
        )
    return chunks


def load_note_chunks(transcript_path: Path) -> List[Dict[str, Any]]:
    notes_path = transcript_path.with_suffix(".notes.json")
    if not notes_path.exists():
        return []
    try:
        data = json.loads(notes_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    chunks: List[Dict[str, Any]] = []
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text:
            continue
        try:
            start = float(key)
        except (TypeError, ValueError):
            start = 0.0
        chunks.append(
            {
                "start": start,
                "end": start,
                "text": text,
                "kind": "note",
            }
        )
    return chunks


def build_index(root_dir: Path, progress_cb: Optional[ProgressCallback] = None) -> Dict[str, Any]:
    llm_dir = root_dir / config.LLM_DIRNAME
    llm_dir.mkdir(parents=True, exist_ok=True)
    srt_paths = sorted(root_dir.glob(f"*{config.TRANSCRIPT_EXT}"))
    chunks: List[Dict[str, Any]] = []
    for path in srt_paths:
        if progress_cb:
            progress_cb(f"Indexando {path.name}...")
        segments = parse_srt_segments(path)
        for chunk in chunk_segments(
            segments,
            max_seconds=config.LLM_MAX_CHUNK_SECONDS,
            max_chars=config.LLM_MAX_CHUNK_CHARS,
        ):
            chunk["kind"] = "srt"
            chunk["transcript_path"] = str(path)
            chunk["file_name"] = path.name
            chunks.append(chunk)
        for note_chunk in load_note_chunks(path):
            note_chunk["transcript_path"] = str(path)
            note_chunk["file_name"] = path.name
            chunks.append(note_chunk)

    if not chunks:
        index = {
            "version": 1,
            "created_at": datetime.utcnow().isoformat(),
            "root": str(root_dir),
            "chunks": [],
        }
        save_index(llm_dir, index)
        return index

    embeddings: List[List[float]] = []
    for idx, chunk in enumerate(chunks, start=1):
        if progress_cb:
            progress_cb(f"Embeddings {idx}/{len(chunks)}...")
        embeddings.append(ollama_embed(chunk["text"]))

    for chunk, embedding in zip(chunks, embeddings):
        chunk["embedding"] = embedding

    index = {
        "version": 1,
        "created_at": datetime.utcnow().isoformat(),
        "root": str(root_dir),
        "chunks": chunks,
    }
    save_index(llm_dir, index)
    return index


def save_index(llm_dir: Path, index: Dict[str, Any]) -> None:
    index_path = llm_dir / "index.json"
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def load_index(llm_dir: Path) -> Optional[Dict[str, Any]]:
    index_path = llm_dir / "index.json"
    if not index_path.exists():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or "chunks" not in data:
        return None
    return data


def search_index(
    query: str,
    index: Dict[str, Any],
    top_k: int,
) -> List[Dict[str, Any]]:
    chunks = index.get("chunks", [])
    if not chunks:
        return []
    query_embedding = ollama_embed(query)
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for chunk in chunks:
        embedding = chunk.get("embedding")
        if not embedding:
            continue
        score = cosine_similarity(query_embedding, embedding)
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _score, chunk in scored[:top_k]]


def build_prompt(question: str, sources: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    formatted_sources: List[str] = []
    for idx, source in enumerate(sources, start=1):
        start = seconds_to_hhmmss(source.get("start", 0.0))
        end = seconds_to_hhmmss(source.get("end", source.get("start", 0.0)))
        file_name = source.get("file_name", "desconocido")
        label = f"[C{idx}] archivo={file_name} inicio={start} fin={end}"
        formatted_sources.append(f"{label}\n{source.get('text', '')}")
    context_block = "\n\n".join(formatted_sources)
    system = (
        "Eres un asistente que responde en espanol. "
        "Usa solo la informacion de las fuentes entregadas. "
        "Si la respuesta no esta en las fuentes, di que no esta disponible. "
        "Incluye citas usando el formato [C#] al final de cada frase relevante."
    )
    user = f"Pregunta: {question}\n\nFuentes:\n{context_block}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ollama_embed(text: str) -> List[float]:
    url = f"{config.OLLAMA_URL.rstrip('/')}/api/embeddings"
    payload = {"model": config.OLLAMA_EMBED_MODEL, "prompt": text}
    response = requests.post(url, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    embedding = data.get("embedding")
    if not embedding:
        raise RuntimeError("Respuesta de embeddings vacia.")
    return embedding


def ollama_chat(messages: List[Dict[str, str]]) -> str:
    url = f"{config.OLLAMA_URL.rstrip('/')}/api/chat"
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.2},
    }
    response = requests.post(url, json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    message = data.get("message", {})
    content = message.get("content")
    if not content:
        raise RuntimeError("Respuesta de chat vacia.")
    return str(content)


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))

