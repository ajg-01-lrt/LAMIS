"""Tests for the Part Lookup helper (Help → Part Lookup).

The dialog widget itself needs a Tk root and isn't unit-tested here;
those paths get hit during manual smoke-checks. The ``lookup_part``
function does all the actual DB work and is pure logic against a real
SQLite file, so we test it end-to-end against a temp DB seeded with
known rows.
"""
from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gui.part_lookup_dialog import _strip_vendor_prefix, lookup_part


def _make_test_db(rows: list[tuple[str, str]]) -> str:
    """Write *rows* into a fresh temp SQLite DB matching the parts
    schema and return the path. Caller cleans up the temp file.

    Close the ``mkstemp`` fd BEFORE touching the file — on Windows a
    file with an open handle can't be deleted or reopened, which the
    first cut of this helper learned the hard way.
    """
    import os
    fd, path = tempfile.mkstemp(suffix=".db", prefix="part_lookup_test_")
    os.close(fd)
    # sqlite3.connect happily treats an empty 0-byte file as a fresh DB
    # and writes the header on the first commit; no need to delete and
    # recreate.
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE parts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " part_number TEXT NOT NULL,"
            " description TEXT NOT NULL,"
            " UNIQUE(part_number) ON CONFLICT REPLACE"
            ")"
        )
        con.executemany(
            "INSERT INTO parts (part_number, description) VALUES (?, ?)", rows
        )
        con.commit()
    finally:
        con.close()
    return path


class TestStripVendorPrefix(unittest.TestCase):
    """The canonicalization rule must match what the BoM compare layer
    uses — strip a leading ``1P`` / ``P``, then uppercase."""

    def test_strips_1p_prefix(self):
        self.assertEqual(_strip_vendor_prefix("1PXCVR-A10Y31"), "XCVR-A10Y31")

    def test_strips_p_prefix(self):
        self.assertEqual(_strip_vendor_prefix("P3HE00027CAAA01"), "3HE00027CAAA01")

    def test_leaves_unprefixed_intact(self):
        self.assertEqual(_strip_vendor_prefix("XCVR-A10Y31"), "XCVR-A10Y31")

    def test_uppercases_lowercase_input(self):
        self.assertEqual(_strip_vendor_prefix("xcvr-a10y31"), "XCVR-A10Y31")

    def test_strips_then_uppercases(self):
        self.assertEqual(_strip_vendor_prefix("1pxcvr-a10y31"), "XCVR-A10Y31")

    def test_strips_whitespace(self):
        self.assertEqual(_strip_vendor_prefix("  XCVR-A10Y31  "), "XCVR-A10Y31")


class TestLookupPart(unittest.TestCase):

    SEED_ROWS = [
        ("XCVR-A10Y31", "100M/1GIG, SM SFP OPTIC, LC CONNECTOR, 10 KM, 1310 NM"),
        ("XCVR-A10Y32", "100M/1GIG, SM SFP OPTIC, LC CONNECTOR, 40 KM, 1310 NM"),
        ("XCVR-B00CRJ", "SFP+ 10GE SR-LC"),
        ("3HE04824AA", "SFP+ 10GE SR-LC variant"),
        ("3HE00027CAAA01", "Some Ciena card"),
        ("NTK803DA", "6500-R2 4-Slot Shelf Assembly"),
    ]

    def setUp(self):
        self.db_path = _make_test_db(self.SEED_ROWS)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    # ── Exact match ─────────────────────────────────────────────────────

    def test_exact_match_returns_description(self):
        exact, _ = lookup_part(self.db_path, "XCVR-A10Y31")
        self.assertEqual(
            exact, "100M/1GIG, SM SFP OPTIC, LC CONNECTOR, 10 KM, 1310 NM"
        )

    def test_case_insensitive_exact_match(self):
        # Operators paste in mixed case all the time; the lookup must
        # ignore case rather than silently return None.
        exact, _ = lookup_part(self.db_path, "xcvr-a10y31")
        self.assertIsNotNone(exact)
        self.assertIn("SM SFP OPTIC", exact)

    def test_1p_prefix_resolves_to_canonical(self):
        # ``1PXCVR-A10Y31`` is the price-book form; the DB only stores
        # the canonical ``XCVR-A10Y31`` after the v2.0.6.x dedup. The
        # lookup must strip the prefix so the operator still gets a hit.
        exact, _ = lookup_part(self.db_path, "1PXCVR-A10Y31")
        self.assertIsNotNone(exact)
        self.assertIn("SM SFP OPTIC", exact)

    def test_p_prefix_resolves_to_canonical(self):
        exact, _ = lookup_part(self.db_path, "P3HE00027CAAA01")
        self.assertEqual(exact, "Some Ciena card")

    # ── No match ────────────────────────────────────────────────────────

    def test_no_match_returns_none(self):
        exact, suggestions = lookup_part(self.db_path, "NOT-A-REAL-PART")
        self.assertIsNone(exact)
        self.assertEqual(suggestions, [])

    def test_empty_query_returns_none_and_empty(self):
        self.assertEqual(lookup_part(self.db_path, ""), (None, []))
        self.assertEqual(lookup_part(self.db_path, "   "), (None, []))

    def test_whitespace_only_after_strip_returns_none(self):
        # After stripping ``1P`` / ``P`` and whitespace the query could
        # collapse to empty — must not crash or return random rows.
        self.assertEqual(lookup_part(self.db_path, "  P  "), (None, []))

    # ── Prefix suggestions ─────────────────────────────────────────────

    def test_partial_query_returns_prefix_suggestions(self):
        exact, suggestions = lookup_part(self.db_path, "XCVR-A10")
        # No exact match for the truncated form
        self.assertIsNone(exact)
        # Both XCVR-A10Y31 and XCVR-A10Y32 should surface
        pns = {pn for pn, _ in suggestions}
        self.assertIn("XCVR-A10Y31", pns)
        self.assertIn("XCVR-A10Y32", pns)

    def test_exact_hit_excluded_from_suggestions(self):
        # When the query is the exact full SKU the prefix LIKE has no
        # longer match to extend (``XCVR-A10Y31%`` doesn't match
        # ``XCVR-A10Y32``), so suggestions come back empty — but the
        # exact-hit row itself must also NOT appear in suggestions if
        # somehow it did (defense against double-listing).
        exact, suggestions = lookup_part(self.db_path, "XCVR-A10Y31")
        self.assertIsNotNone(exact)
        pns = {pn for pn, _ in suggestions}
        self.assertNotIn("XCVR-A10Y31", pns)
        self.assertEqual(suggestions, [])

    def test_shorter_prefix_returns_other_matches(self):
        # If the operator drops a character we ARE looking up via prefix
        # — ``XCVR-A10Y3`` matches both Y31 and Y32 (no exact hit).
        exact, suggestions = lookup_part(self.db_path, "XCVR-A10Y3")
        self.assertIsNone(exact)
        pns = {pn for pn, _ in suggestions}
        self.assertIn("XCVR-A10Y31", pns)
        self.assertIn("XCVR-A10Y32", pns)

    def test_suggestions_capped_at_max(self):
        # Seed enough rows to exceed the cap and confirm we don't return
        # more than asked for.
        rows = [(f"BULKPART{i:04d}", f"desc {i}") for i in range(25)]
        path = _make_test_db(rows)
        try:
            _, suggestions = lookup_part(path, "BULKPART", max_suggestions=10)
            self.assertEqual(len(suggestions), 10)
            _, suggestions = lookup_part(path, "BULKPART", max_suggestions=5)
            self.assertEqual(len(suggestions), 5)
        finally:
            Path(path).unlink(missing_ok=True)


class TestHelpMenuRegistration(unittest.TestCase):
    """Source-level check that the Help dropdown gained a Part Lookup
    entry wired to the right handler. Avoids needing a Tk root."""

    def test_help_menu_adds_part_lookup_command(self):
        import inspect
        from gui import gui4_0
        src = inspect.getsource(gui4_0)
        self.assertIn(
            'label="Part Lookup"', src,
            "Help menu must add a 'Part Lookup' command",
        )
        self.assertIn(
            "self._open_part_lookup", src,
            "Part Lookup menu item must dispatch to _open_part_lookup",
        )

    def test_open_part_lookup_handler_exists(self):
        from gui.gui4_0 import InventoryGUI
        self.assertTrue(
            callable(getattr(InventoryGUI, "_open_part_lookup", None)),
            "InventoryGUI._open_part_lookup must be defined",
        )


if __name__ == "__main__":
    unittest.main()
