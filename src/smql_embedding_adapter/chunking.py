"""Document chunking."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class TextChunk:
    id: str
    text: str
    start: int
    end: int


class TextChunker:
    def __init__(self, chunk_chars: int = 1600, overlap_chars: int = 160) -> None:
        if chunk_chars <= 0:
            raise ValueError("chunk_chars must be positive")
        if overlap_chars < 0 or overlap_chars >= chunk_chars:
            raise ValueError("overlap_chars must be >= 0 and < chunk_chars")
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars

    def chunk_text(self, text: str, *, prefix: str = "chunk") -> list[TextChunk]:
        chunks: list[TextChunk] = []
        start = 0
        n = len(text)
        idx = 0
        while start < n:
            end = min(n, start + self.chunk_chars)
            boundary = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if boundary > start + self.chunk_chars // 2:
                end = boundary + 1
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(TextChunk(id=f"{prefix}-{idx:06d}", text=chunk, start=start, end=end))
                idx += 1
            if end >= n:
                break
            start = max(0, end - self.overlap_chars)
        return chunks

    def chunk_file(self, path: str | Path, *, encoding: str = "utf-8") -> list[TextChunk]:
        p = Path(path)
        text = p.read_text(encoding=encoding)
        return self.chunk_text(text, prefix=p.stem)
