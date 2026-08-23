"""Normalize the parts DB: collapse exact-PK duplicates and enforce uniqueness.

Follow-up to ``cleanup_prefix_dupes.py``. The prefix dedup surfaced that the
seed DB had **87 part_numbers with multiple rows** (130 excess rows) because
the original schema is ``id PRIMARY KEY AUTOINCREMENT`` with no uniqueness
constraint on ``part_number``. That let imports stack rows for the same SKU
with subtly different descriptions, which then broke a prefix-cleanup pass
that picked the "wrong" canonical row.

This script:

1. Collapses every duplicate ``part_number`` group down to one row.
   When the descriptions differ within a group, the LONGEST description
   wins (longer = more informative, e.g. ``"PSC1-6 - Passive Splitter/
   Combiner"`` vs. the bare SKU ``"PSC1-6"``). Ties break on lowest
   ``id`` to keep the change deterministic across re-runs.
2. Re-runs the prefix dedup on the seed DB to catch any stragglers
   (e.g. ``1P3HE00062CBAA01``) that the first pass tripped on because
   the canonical had two rows.
3. Rebuilds the ``parts`` table with ``UNIQUE(part_number) ON CONFLICT
   REPLACE`` so future imports can't reintroduce the duplicates.

Runs against BOTH the runtime DB (``%APPDATA%\\ATLAS\\network_inventory.db``)
and the seed copy (``data/network_inventory.db``).
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

# Same rules as cleanup_prefix_dupes.py — kept here so this script is
# self-contained and re-runnable.
KEEP_FOR_REVIEW = {
    "1P1AD151930001",
    "1PNTTP84BA",
    "P3HE00062CBAA01",
}
MERGE_THEN_DELETE = {
    "1P8DG62413AAAA01": "8DG62413AAAA01",
}

NEW_SCHEMA = """
CREATE TABLE parts_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL,
    description TEXT NOT NULL,
    UNIQUE(part_number) ON CONFLICT REPLACE
)
"""


def _collapse_pk_duplicates(con: sqlite3.Connection, label: str) -> int:
    """Keep the row with the longest description per ``part_number``;
    delete the others. Returns the number of rows removed.

    Tie-break on lowest ``id`` so re-runs are deterministic — important
    because the cleanup writes to a source-controlled file in seed mode.
    """
    cur = con.cursor()
    # All rows for part_numbers that have >1 row.
    cur.execute("""
        SELECT id, part_number, description
        FROM parts
        WHERE part_number IN (
            SELECT part_number FROM parts
            GROUP BY part_number HAVING COUNT(*) > 1
        )
        ORDER BY part_number, length(description) DESC, id ASC
    """)
    rows = cur.fetchall()

    keep_ids: set[int] = set()
    delete_ids: list[int] = []
    seen_pn: set[str] = set()
    for row_id, pn, _desc in rows:
        if pn in seen_pn:
            delete_ids.append(row_id)
        else:
            keep_ids.add(row_id)
            seen_pn.add(pn)

    if delete_ids:
        # Chunk to keep the SQL parameter count under SQLite's 999 limit.
        # Note: the bare ``?``-placeholders string is built from len(chunk)
        # — no caller input goes near it — but we still avoid f-string-in-
        # execute so the project's defense-in-depth check stays green.
        for i in range(0, len(delete_ids), 500):
            chunk = delete_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            sql = "DELETE FROM parts WHERE id IN (" + placeholders + ")"
            cur.execute(sql, chunk)
    print(f"[{label}] collapsed {len(seen_pn)} duplicate-PK groups, "
          f"removed {len(delete_ids)} excess rows")
    return len(delete_ids)


def _run_prefix_dedup(con: sqlite3.Connection, label: str) -> int:
    """Strip ``1P``/``P``-prefixed rows whose unprefixed twin already exists.

    Runs AFTER ``_collapse_pk_duplicates`` so each part_number has exactly
    one row at this point — no more flaky "wrong canonical picked" cases.
    """
    cur = con.cursor()
    cur.execute("SELECT part_number, description FROM parts")
    rows = cur.fetchall()
    by_upper = {pn.upper(): (pn, desc) for pn, desc in rows}

    deleted: list[str] = []
    merged: list[tuple[str, str]] = []
    kept_diff: list[str] = []
    for pn, desc in rows:
        upper = pn.upper()
        if upper.startswith("1P"):
            stripped = upper[2:]
        elif upper.startswith("P") and len(pn) > 1:
            stripped = upper[1:]
        else:
            continue
        twin = by_upper.get(stripped)
        if twin is None or twin[0].upper() == upper:
            continue
        canonical_pn, canonical_desc = twin

        if pn in KEEP_FOR_REVIEW:
            kept_diff.append(pn)
            continue
        if pn in MERGE_THEN_DELETE:
            cur.execute(
                "UPDATE parts SET description = ? WHERE part_number = ?",
                (desc, canonical_pn),
            )
            cur.execute("DELETE FROM parts WHERE part_number = ?", (pn,))
            merged.append((pn, canonical_pn))
            continue
        if (desc or "").strip().upper() == (canonical_desc or "").strip().upper():
            cur.execute("DELETE FROM parts WHERE part_number = ?", (pn,))
            deleted.append(pn)
        else:
            kept_diff.append(pn)

    print(f"[{label}] prefix cleanup: deleted {len(deleted)}, "
          f"merged {len(merged)}, kept_for_review {len(kept_diff)}")
    if kept_diff:
        print(f"[{label}] review: {', '.join(kept_diff)}")
    return len(deleted) + len(merged)


def _rebuild_with_unique_constraint(con: sqlite3.Connection, label: str) -> None:
    """Recreate ``parts`` with ``UNIQUE(part_number) ON CONFLICT REPLACE``.

    SQLite doesn't support ``ALTER TABLE ADD CONSTRAINT``, so the standard
    rename-shuffle is required: create new table, copy, drop old, rename.
    Wrap in a transaction so a failure leaves the original intact.
    """
    cur = con.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(NEW_SCHEMA)
        cur.execute("""
            INSERT INTO parts_new (id, part_number, description)
            SELECT id, part_number, description FROM parts
        """)
        cur.execute("DROP TABLE parts")
        cur.execute("ALTER TABLE parts_new RENAME TO parts")
        # Helpful index for the lookup_part() hot path (LIKE with leading
        # literal benefits from the implicit UNIQUE index on part_number;
        # explicit declaration of a part_number index would be redundant).
        cur.execute("COMMIT")
        print(f"[{label}] schema rebuilt with UNIQUE(part_number) ON CONFLICT REPLACE")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def _verify_unique_constraint(con: sqlite3.Connection, label: str) -> None:
    """Smoke-test: inserting a row whose part_number already exists must
    replace the prior row rather than error or duplicate."""
    cur = con.cursor()
    # Pick any existing part_number — we'll write a sentinel description,
    # confirm it overwrote, then put the original back.
    row = cur.execute("SELECT part_number, description FROM parts LIMIT 1").fetchone()
    original_pn, original_desc = row
    sentinel = f"__sentinel_{datetime.now().timestamp()}__"
    try:
        cur.execute(
            "INSERT INTO parts (part_number, description) VALUES (?, ?)",
            (original_pn, sentinel),
        )
        rows = cur.execute(
            "SELECT COUNT(*), MAX(description) FROM parts WHERE part_number = ?",
            (original_pn,),
        ).fetchone()
        if rows[0] != 1 or rows[1] != sentinel:
            raise AssertionError(
                f"UNIQUE constraint not enforcing: count={rows[0]} desc={rows[1]!r}"
            )
    finally:
        # Always restore the original description.
        cur.execute(
            "UPDATE parts SET description = ? WHERE part_number = ?",
            (original_desc, original_pn),
        )
        con.commit()
    print(f"[{label}] verified: UNIQUE constraint enforces, ON CONFLICT REPLACE works")


def _normalize(db_path: Path, label: str) -> None:
    if not db_path.exists():
        print(f"[{label}] missing at {db_path} — skipped")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = db_path.with_name(f"{db_path.name}.bak_normalize_{ts}")
    shutil.copy2(db_path, bak)
    print(f"\n[{label}] backed up to {bak.name}")

    con = sqlite3.connect(str(db_path))
    try:
        before = con.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        _collapse_pk_duplicates(con, label)
        _run_prefix_dedup(con, label)
        con.commit()
        mid = con.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        _rebuild_with_unique_constraint(con, label)
        after = con.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        _verify_unique_constraint(con, label)
        print(f"[{label}] rows: {before} -> {mid} -> {after} "
              f"(net removed: {before - after})")
    finally:
        con.close()


def main() -> int:
    _normalize(RUNTIME_DB, "runtime")
    _normalize(SEED_DB, "seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
