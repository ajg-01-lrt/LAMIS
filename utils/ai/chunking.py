"""PDF text extraction and chunking for the ATLAS doc assistant.

Turns a vendor manual into a list of :class:`Chunk` objects, each carrying the
page it came from so answers can cite "<doc>, p.<n>". Chunks are word-windowed
with overlap so a procedure that straddles a window boundary still appears
intact in at least one chunk.

``pypdf`` is imported lazily so this module is importable (for the Chunk type)
on a build that does not have the optional dependency installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple, Optional

import config


class Chunk(NamedTuple):
    page: Optional[int]   # 1-based source page, or None if unknown
    chunk_index: int      # position within the document
    text: str


def _window(words: List[str], size: int, overlap: int):
    """Yield overlapping word windows. Step is size-overlap (>=1)."""
    step = max(1, size - overlap)
    for start in range(0, len(words), step):
        window = words[start : start + size]
        if window:
            yield " ".join(window)
        if start + size >= len(words):
            break


def extract_pages(pdf_path: Path) -> List[str]:
    """Return one text string per page of the PDF (empty string if a page has
    no extractable text, e.g. a scanned image)."""
    try:
        from pypdf import PdfReader  # lazy: optional dependency
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "The 'pypdf' package is required to ingest PDFs "
            "(pip install pypdf)."
        ) from exc
    reader = PdfReader(str(pdf_path))
    return [(page.extract_text() or "") for page in reader.pages]


def chunk_pdf(
    pdf_path: Path,
    chunk_words: Optional[int] = None,
    overlap_words: Optional[int] = None,
) -> List[Chunk]:
    """Extract and chunk a PDF into citation-tagged windows."""
    size = chunk_words or config.AI_CHUNK_WORDS
    overlap = overlap_words if overlap_words is not None else config.AI_CHUNK_OVERLAP_WORDS

    chunks: List[Chunk] = []
    idx = 0
    for page_num, page_text in enumerate(extract_pages(pdf_path), start=1):
        words = page_text.split()
        if not words:
            continue
        for window_text in _window(words, size, overlap):
            chunks.append(Chunk(page=page_num, chunk_index=idx, text=window_text))
            idx += 1
    return chunks
