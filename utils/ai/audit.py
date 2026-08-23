"""Index hygiene for the ATLAS doc assistant.

Reports (and optionally fixes) three quality problems that degrade retrieval:

  1. Failed extractions  - PDFs that yielded ~no text (scanned/table images),
                           sitting in the index as 1-2 useless chunks.
  2. Duplicate files     - the same document indexed twice under two filenames
                           (e.g. a descriptive-prefixed copy AND the canonical
                           name). Detected when one filename ends with another,
                           which is the exact "<desc> - <canonical>.pdf" pattern
                           in the corpus. Returns near-identical chunks that
                           waste top-k slots.
  3. Version groups      - the same document number present in multiple releases
                           (R16.9 + R17.0, OLS 23.6/24.6/25.3/26.3). NOT deleted
                           automatically — mixing releases risks citing the
                           wrong syntax, but which releases to keep is a policy
                           call, so this is reported for you to decide.

Default action is a read-only report. Removals require an explicit flag.

Usage (from project root):
    python -m utils.ai.audit                 # report only
    python -m utils.ai.audit --remove-empty       # drop failed extractions
    python -m utils.ai.audit --remove-duplicates  # drop redundant dup copies
    python -m utils.ai.audit --remove "<exact doc_name>"
"""

from __future__ import annotations

import argparse
import re
from typing import Dict, List, Tuple

from utils.ai.doc_index import DocIndex

# A 1-chunk doc extracted essentially nothing; 2 is borderline. Tune if needed.
EMPTY_CHUNK_THRESHOLD = 2

# Leading vendor document number, e.g. 323-1851-193, 3KC91891AAAA, 3HE16156AAAB.
_DOCNUM_RE = re.compile(r"^(\d{3}-\d{4}-\d{3}|3KC\d+[A-Z]*|3HE\d+[A-Z]*)")

# Release/version markers used to tell editions of the same doc apart.
_RELEASE_RE = re.compile(
    r"R\d+\.\d+|REL\d+|\b\d{2}\.\d+\.R\d+|OLS.*?Release\s*\d+\.\d+|"
    r"SAOS[_ ]?\d+[._-]\d+|\d{2}-\d{2}-\d{2}",
    re.IGNORECASE,
)


def find_failed(stats: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    return [(name, n) for name, n in stats if n <= EMPTY_CHUNK_THRESHOLD]


def find_duplicates(stats: List[Tuple[str, int]]) -> List[Tuple[str, str]]:
    """Return (redundant_name, canonical_name) pairs.

    One filename being a suffix of another (ignoring the leading descriptive
    prefix) means the same .pdf was indexed twice. The longer, prefixed name is
    treated as the redundant copy to remove.
    """
    names = [name for name, _ in stats]
    pairs = []
    for short in names:
        core = short[:-4] if short.lower().endswith(".pdf") else short
        for other in names:
            if other == short or len(other) <= len(short):
                continue
            # other is longer; redundant if it ends with the canonical name.
            if other.endswith(short) or other.endswith(core + ".pdf"):
                pairs.append((other, short))
    return pairs


def find_version_groups(stats: List[Tuple[str, int]]) -> Dict[str, List[str]]:
    """Group docs by document number where 2+ distinct releases are present."""
    groups: Dict[str, List[str]] = {}
    for name, _ in stats:
        m = _DOCNUM_RE.match(name)
        if not m:
            continue
        groups.setdefault(m.group(1), []).append(name)
    # Keep only groups whose members show more than one distinct release marker.
    multi: Dict[str, List[str]] = {}
    for docnum, members in groups.items():
        releases = {
            (_RELEASE_RE.search(n).group(0).lower() if _RELEASE_RE.search(n) else "")
            for n in members
        }
        if len(members) > 1 and len(releases) > 1:
            multi[docnum] = sorted(members)
    return multi


def report(index: DocIndex) -> dict:
    stats = index.doc_stats()
    failed = find_failed(stats)
    dups = find_duplicates(stats)
    versions = find_version_groups(stats)

    total_chunks = sum(n for _, n in stats)
    print(f"Index: {len(stats)} docs, {total_chunks:,} chunks\n")

    print(f"1) Failed extractions (<= {EMPTY_CHUNK_THRESHOLD} chunks): {len(failed)}")
    for name, n in failed:
        print(f"     {n:>4} chunk  {name}")

    print(f"\n2) Duplicate files (same doc, two filenames): {len(dups)}")
    for redundant, canonical in dups:
        print(f"     redundant: {redundant}")
        print(f"     canonical: {canonical}")

    print(f"\n3) Multi-release groups (review - version contamination risk): {len(versions)}")
    for docnum, members in sorted(versions.items()):
        print(f"     {docnum}:")
        for m in members:
            print(f"        {m}")

    return {"failed": failed, "duplicates": dups, "versions": versions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Audit/clean the ATLAS doc index.")
    parser.add_argument("--remove-empty", action="store_true",
                        help="delete docs at/below the failed-extraction threshold")
    parser.add_argument("--remove-duplicates", action="store_true",
                        help="delete the redundant copy of each duplicate pair")
    parser.add_argument("--remove", metavar="DOC_NAME",
                        help="delete one doc by its exact indexed name")
    args = parser.parse_args(argv)

    with DocIndex() as index:
        findings = report(index)

        removed = 0
        if args.remove_empty:
            for name, _ in findings["failed"]:
                removed += index.remove_doc(name)
                print(f"removed (empty): {name}")
        if args.remove_duplicates:
            for redundant, _ in findings["duplicates"]:
                removed += index.remove_doc(redundant)
                print(f"removed (duplicate): {redundant}")
        if args.remove:
            n = index.remove_doc(args.remove)
            removed += n
            print(f"removed: {args.remove} ({n} chunks)" if n
                  else f"no such doc indexed: {args.remove}")

        if removed:
            print(f"\nDeleted {removed:,} chunks. New total: "
                  f"{sum(n for _, n in index.doc_stats()):,}")
        elif not (args.remove_empty or args.remove_duplicates or args.remove):
            print("\n(report only - pass --remove-empty / --remove-duplicates / "
                  "--remove to act)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
