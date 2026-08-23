"""One-shot cleanup: drop `1P`/`P`-prefixed duplicates from the parts DB.

The compare layer already strips ``1P``/``P`` prefixes at lookup time so the
duplicates were silent — but they waste rows, can drift apart on description
edits, and confuse ``db_cache.lookup_part`` (which slices to 10 chars and
hits a different row depending on which form was queried).

Scope per beta-test follow-up:
* 30 same-desc duplicates: prefixed row removed, canonical kept.
* 1 merge case (``1P8DG62413AAAA01``): canonical desc was just ``"PSC1-6"``
  — copy the prefixed row's fuller description over first, then delete.
* 3 ambiguous DIFF rows left untouched (see ``KEEP_FOR_REVIEW``).

Cleans both the runtime DB (``%APPDATA%\\ATLAS\\network_inventory.db``) and
the seed copy (``data/network_inventory.db``) so a fresh install doesn't
re-introduce the duplicates.
"""
from __future__ import annotations
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RUNTIME_DB = Path(os.environ.get("APPDATA", "")) / "ATLAS" / "network_inventory.db"
SEED_DB = Path(__file__).resolve().parent.parent / "data" / "network_inventory.db"

# DIFF-desc rows kept as-is. ODF_KIT vs FIBER STORAGE TRAY (2U) may actually
# be distinct kit/bare-part listings; the NTTP84BA and 3HE00062 cases are
# wording variants where neither side is clearly correct.
KEEP_FOR_REVIEW = {
    "1P1AD151930001",
    "1PNTTP84BA",
    "P3HE00062CBAA01",
}

# Prefixed row carries the better description — merge into canonical then drop.
MERGE_THEN_DELETE = {
    "1P8DG62413AAAA01": "8DG62413AAAA01",
}


def _find_prefix_dupes(cur: sqlite3.Cursor) -> list[tuple[str, str, str, str]]:
    """Return ``(prefixed, canonical, prefixed_desc, canonical_desc)`` for
    every prefixed row whose unprefixed twin also exists in the table.

    Computes the candidate list in Python rather than a SQL self-join —
    the self-join with LIKE + SUBSTR + UPPER on a 43k-row table choked
    badly the first time around (~25 min CPU and counting before we
    killed it). A single index-friendly scan plus an in-memory lookup
    runs in well under a second.
    """
    cur.execute("SELECT part_number, description FROM parts")
    rows = cur.fetchall()
    by_upper = {pn.upper(): (pn, desc) for pn, desc in rows}

    dupes: list[tuple[str, str, str, str]] = []
    for pn, desc in rows:
        upper = pn.upper()
        if upper.startswith("1P"):
            stripped = upper[2:]
        elif upper.startswith("P") and len(pn) > 1:
            stripped = upper[1:]
        else:
            continue
        twin = by_upper.get(stripped)
        if twin is None:
            continue
        canonical_pn, canonical_desc = twin
        if canonical_pn.upper() == upper:
            continue  # Shouldn't happen but guards against same-row matches.
        dupes.append((pn, canonical_pn, desc or "", canonical_desc or ""))
    return dupes


def _cleanup(db_path: Path, label: str) -> None:
    if not db_path.exists():
        print(f"[{label}] missing at {db_path} — skipped")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db_path.with_name(f"{db_path.name}.bak_{ts}")
    shutil.copy2(db_path, bak)
    print(f"\n[{label}] backed up to {bak.name}")

    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        before = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]

        dupes = _find_prefix_dupes(cur)
        print(f"[{label}] found {len(dupes)} prefix duplicates")

        deleted_same: list[str] = []
        merged: list[tuple[str, str]] = []
        kept_diff: list[str] = []
        for prefixed, canonical, p_desc, c_desc in dupes:
            if prefixed in KEEP_FOR_REVIEW:
                kept_diff.append(prefixed)
                continue
            if prefixed in MERGE_THEN_DELETE:
                expected = MERGE_THEN_DELETE[prefixed]
                if canonical != expected:
                    raise RuntimeError(
                        f"Merge target mismatch: {prefixed!r} -> {canonical!r}, "
                        f"expected {expected!r}"
                    )
                cur.execute(
                    "UPDATE parts SET description = ? WHERE part_number = ?",
                    (p_desc, canonical),
                )
                cur.execute("DELETE FROM parts WHERE part_number = ?", (prefixed,))
                merged.append((prefixed, canonical))
                continue
            if p_desc.strip().upper() == c_desc.strip().upper():
                cur.execute("DELETE FROM parts WHERE part_number = ?", (prefixed,))
                deleted_same.append(prefixed)
            else:
                kept_diff.append(prefixed)

        con.commit()
        after = cur.execute("SELECT COUNT(*) FROM parts").fetchone()[0]

        print(f"[{label}] before={before}  after={after}  removed={before - after}")
        print(f"[{label}] same-desc deletes ({len(deleted_same)}): "
              f"{', '.join(deleted_same) or '(none)'}")
        for prefixed, canonical in merged:
            print(f"[{label}] merged then deleted: {prefixed} -> {canonical}")
        print(f"[{label}] kept for manual review ({len(kept_diff)}): "
              f"{', '.join(kept_diff) or '(none)'}")
    finally:
        con.close()


def main() -> int:
    _cleanup(RUNTIME_DB, "runtime")
    _cleanup(SEED_DB, "seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
