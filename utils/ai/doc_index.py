"""SQLite-backed vector store for the ATLAS doc assistant.

Holds one row per text chunk: the chunk text, its embedding (stored as packed
float32 bytes), and citation metadata (source doc + page). Lives at
``%APPDATA%\\ATLAS\\doc_index.db`` so it is writable on a Program Files install
and survives upgrades — the same convention as the inventory DB, logs, and
known_hosts (see utils/helpers.py).

Retrieval is brute-force cosine similarity in numpy. For a corpus of a few
thousand chunks (a handful of vendor manuals) this is sub-millisecond and needs
no native vector extension — keeping the dependency surface and the installer
small. If the corpus ever grows past ~100k chunks, swap this one method for a
proper ANN index without touching callers.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence

import numpy as np

import config
from utils.ai.chunking import Chunk


def get_index_db_path() -> Path:
    """Path to the doc index DB, creating %APPDATA%\\ATLAS if needed.

    Mirrors utils.helpers.get_database_path so the AI index sits alongside the
    inventory DB and is writable without elevated privileges.
    """
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    atlas_dir = Path(app_data) / "ATLAS"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    return (atlas_dir / config.AI_INDEX_DB_FILE).resolve()


class Hit(NamedTuple):
    """One retrieval result, carrying everything needed to cite it."""

    doc_name: str
    page: Optional[int]
    text: str
    score: float
    chunk_index: int = -1  # position within its doc; enables neighbor expansion


class DocIndex:
    """Create/populate/search the chunk-embedding store."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else get_index_db_path()
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        # In-memory cache of the full embedding matrix + parallel metadata.
        # Loading 100k+ vectors from SQLite on every query is the dominant
        # cost; we load once on the first search and reuse it. Writes
        # (add_chunks/remove_doc) invalidate the cache so it rebuilds lazily.
        self._cache = None  # tuple(doc_names, pages, texts, matrix) or None

    # -- lifecycle -------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY,
                doc_name    TEXT    NOT NULL,
                page        INTEGER,
                chunk_index INTEGER NOT NULL,
                text        TEXT    NOT NULL,
                embedding   BLOB    NOT NULL,
                model       TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_name);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "DocIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- write -----------------------------------------------------------

    def list_docs(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT doc_name FROM chunks ORDER BY doc_name"
        ).fetchall()
        return [r["doc_name"] for r in rows]

    def doc_stats(self) -> List[tuple]:
        """Return (doc_name, chunk_count) for every doc, fewest chunks first.

        Used by the hygiene audit to spot failed extractions (near-zero
        chunks) and to size duplicate/version groups.
        """
        rows = self._conn.execute(
            "SELECT doc_name, COUNT(*) AS n FROM chunks GROUP BY doc_name ORDER BY n"
        ).fetchall()
        return [(r["doc_name"], r["n"]) for r in rows]

    def remove_doc(self, doc_name: str) -> int:
        """Drop all chunks for a doc (used to re-ingest a revised manual)."""
        cur = self._conn.execute("DELETE FROM chunks WHERE doc_name = ?", (doc_name,))
        self._conn.commit()
        self._cache = None  # invalidate: matrix no longer matches the table
        return cur.rowcount

    def add_chunks(
        self,
        doc_name: str,
        chunks: Sequence["Chunk"],
        embeddings: Sequence[Sequence[float]],
        model: str,
    ) -> int:
        """Insert chunks + their embeddings. Vectors are L2-normalized on the
        way in so search is a plain dot product later."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for chunk, vec in zip(chunks, embeddings):
            arr = np.asarray(vec, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            rows.append(
                (
                    doc_name,
                    chunk.page,
                    chunk.chunk_index,
                    chunk.text,
                    arr.tobytes(),
                    model,
                    now,
                )
            )
        self._conn.executemany(
            """INSERT INTO chunks
               (doc_name, page, chunk_index, text, embedding, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self._conn.commit()
        self._cache = None  # invalidate: new vectors not yet in the matrix
        return len(rows)

    # -- read ------------------------------------------------------------

    def _load_matrix(self):
        """Return (doc_names, pages, texts, matrix) for all chunks.

        The matrix is (n_chunks, dim) of pre-normalized float32 vectors. The
        result is cached on the instance; subsequent calls are free until a
        write invalidates it. This is what keeps per-query latency flat as the
        corpus grows into the 100k+ chunk range.
        """
        if self._cache is not None:
            return self._cache
        rows = self._conn.execute(
            "SELECT doc_name, page, chunk_index, text, embedding FROM chunks"
        ).fetchall()
        if not rows:
            empty = ([], [], [], [], np.empty((0, config.AI_EMBED_DIM), dtype=np.float32))
            self._cache = empty
            return empty
        doc_names = [r["doc_name"] for r in rows]
        pages = [r["page"] for r in rows]
        chunk_idxs = [r["chunk_index"] for r in rows]
        texts = [r["text"] for r in rows]
        matrix = np.vstack(
            [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
        )
        self._cache = (doc_names, pages, chunk_idxs, texts, matrix)
        return self._cache

    def search(
        self,
        query_vec: Sequence[float],
        top_k: int,
        keyword_terms: Optional[Sequence[str]] = None,
        keyword_k: int = 0,
        doc_filter: Optional[Sequence[str]] = None,
    ) -> List[Hit]:
        """Return chunks most relevant to a query embedding.

        Vector top_k by cosine similarity, optionally augmented by a keyword
        pass: up to ``keyword_k`` extra chunks that literally contain one of
        ``keyword_terms`` are forced in (highest-cosine first among matches),
        even if they fell below the vector cut. This guarantees an exact term
        like 'network-type' surfaces the authoritative chunk that pure vector
        ranking can miss.

        ``doc_filter`` restricts results to docs whose name contains one of the
        given substrings (case-insensitive). The platform name lives in the
        filename, not the chunk text, so this is how an 'RLS' question is kept
        from retrieving SAOS chunks. If no doc matches the filter, it is ignored
        (fallback to the full corpus) rather than returning nothing.

        Results are returned highest-cosine first.
        """
        doc_names, pages, chunk_idxs, texts, matrix = self._load_matrix()
        if matrix.shape[0] == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        qnorm = np.linalg.norm(q)
        if qnorm > 0:
            q = q / qnorm
        # Stored vectors are already normalized -> dot product == cosine sim.
        scores = matrix @ q

        # Platform scoping: blank out docs that don't match the filter so they
        # can't be selected. Skip entirely if nothing matches.
        allowed = None
        if doc_filter:
            fl = [f.lower() for f in doc_filter if f]
            allowed = [bool(dn) and any(f in dn.lower() for f in fl) for dn in doc_names]
            if any(allowed):
                neg_inf = np.float32(-np.inf)
                scores = np.where(np.asarray(allowed), scores, neg_inf)
            else:
                allowed = None  # no matches -> ignore filter

        k = min(top_k, scores.shape[0])
        top_idx = np.argpartition(-scores, k - 1)[:k]
        selected = list(top_idx)
        selected_set = set(int(i) for i in selected)

        if keyword_terms and keyword_k > 0:
            terms = [t.lower() for t in keyword_terms if t]
            matches = [
                i for i, txt in enumerate(texts)
                if i not in selected_set
                and (allowed is None or allowed[i])
                and any(term in txt.lower() for term in terms)
            ]
            # Force in the best keyword matches by cosine score.
            matches.sort(key=lambda i: -scores[i])
            for i in matches[:keyword_k]:
                selected.append(i)
                selected_set.add(int(i))

        # Order the final set highest-cosine first, dropping any filtered-out
        # (-inf) rows that argpartition may have included when matches < k.
        selected = [i for i in selected if np.isfinite(scores[i])]
        selected.sort(key=lambda i: -scores[i])
        return [
            Hit(
                doc_name=doc_names[i],
                page=pages[i],
                text=texts[i],
                score=float(scores[i]),
                chunk_index=chunk_idxs[i],
            )
            for i in selected
        ]

    def get_window(self, doc_name: str, center_index: int, window: int):
        """Return chunks around a center chunk (inclusive) as
        [(chunk_index, page, text), ...] ordered by position.

        This is the neighbor-expansion primitive: a retrieved chunk is only a
        ~220-word slice, so a multi-command procedure can straddle the chunk
        boundary. Pulling +/- `window` adjacent chunks from the same doc keeps
        the full procedure (e.g. the OSPF protocol-creation step plus its
        interface steps) in the context handed to the model.
        """
        if window <= 0:
            rows = self._conn.execute(
                "SELECT page, chunk_index, text FROM chunks "
                "WHERE doc_name = ? AND chunk_index = ?",
                (doc_name, center_index),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT page, chunk_index, text FROM chunks "
                "WHERE doc_name = ? AND chunk_index BETWEEN ? AND ? "
                "ORDER BY chunk_index",
                (doc_name, center_index - window, center_index + window),
            ).fetchall()
        return [(r["chunk_index"], r["page"], r["text"]) for r in rows]
