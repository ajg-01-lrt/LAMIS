"""Ingest vendor PDFs into the ATLAS doc index.

Usage (from the project root, with OPENAI_API_KEY set in the environment):

    python -m utils.ai.ingest path\\to\\Nokia_1830_manual.pdf
    python -m utils.ai.ingest path\\to\\docs_folder         # all *.pdf within
    python -m utils.ai.ingest --list                        # show indexed docs

Re-ingesting a doc of the same filename replaces its existing chunks, so
dropping in a revised manual and re-running is safe and idempotent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

import config
from utils.ai.chunking import chunk_pdf
from utils.ai.doc_index import DocIndex
from utils.ai.provider import default_provider


def _embed_in_batches(provider, texts: List[str], batch: int = 64) -> List[List[float]]:
    """Embed many chunks without sending one giant request."""
    out: List[List[float]] = []
    for start in range(0, len(texts), batch):
        out.extend(provider.embed(texts[start : start + batch]))
    return out


def ingest_pdf(index: DocIndex, provider, pdf_path: Path, force: bool = False) -> int:
    doc_name = pdf_path.name
    # Resumability: a folder ingest that hit a rate limit (or was Ctrl-C'd)
    # can be re-run and will pick up where it left off instead of re-embedding
    # — and re-paying for — docs already in the index. Use --force to rebuild.
    if not force and doc_name in index.list_docs():
        print(f"  skipping {doc_name} (already indexed; --force to rebuild)")
        return 0
    print(f"  reading {doc_name} ...", flush=True)
    chunks = chunk_pdf(pdf_path)
    if not chunks:
        print(f"  ! no extractable text in {doc_name} (scanned image?) — skipped")
        return 0
    removed = index.remove_doc(doc_name)
    if removed:
        print(f"  replacing {removed} existing chunks for {doc_name}")
    print(f"  embedding {len(chunks)} chunks via {config.AI_EMBED_MODEL} ...", flush=True)
    vectors = _embed_in_batches(provider, [c.text for c in chunks])
    added = index.add_chunks(doc_name, chunks, vectors, config.AI_EMBED_MODEL)
    print(f"  indexed {added} chunks from {doc_name}")
    return added


def _collect_pdfs(target: Path) -> List[Path]:
    if target.is_dir():
        return sorted(target.glob("*.pdf"))
    if target.suffix.lower() == ".pdf":
        return [target]
    return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ingest vendor PDFs into the ATLAS doc index.")
    parser.add_argument("path", nargs="?", help="PDF file or folder of PDFs to ingest")
    parser.add_argument("--list", action="store_true", help="list indexed docs and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-ingest docs even if already indexed (default: skip them)",
    )
    args = parser.parse_args(argv)

    with DocIndex() as index:
        if args.list:
            docs = index.list_docs()
            if docs:
                print("Indexed docs:")
                for d in docs:
                    print(f"  - {d}")
            else:
                print("No docs indexed yet.")
            return 0

        if not args.path:
            parser.error("provide a PDF/folder path, or --list")

        target = Path(args.path).expanduser()
        if not target.exists():
            print(f"error: path not found: {target}", file=sys.stderr)
            return 2

        pdfs = _collect_pdfs(target)
        if not pdfs:
            print(f"error: no PDF(s) found at {target}", file=sys.stderr)
            return 2

        provider = default_provider()
        total = 0
        print(f"Ingesting {len(pdfs)} PDF(s) into {index.db_path}")
        for pdf in pdfs:
            total += ingest_pdf(index, provider, pdf, force=args.force)
        print(f"Done. {total} chunks indexed across {len(pdfs)} doc(s).")
        # Rebuild the measurement/parameter glossary so 'find/display X' lookups
        # stay in sync with the corpus. Pure text parsing, no API cost.
        from utils.ai.glossary import build_glossary
        gloss = build_glossary(index)
        print(f"Rebuilt parameter glossary: {gloss} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
