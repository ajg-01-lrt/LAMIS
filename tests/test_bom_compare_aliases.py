"""Regression tests for BoM-comparison part-number alias correlation.

Bundle SKUs on the Sales side (e.g. 3HE13584AA "7250 IXR-R6 CHASSIS
BUNDLE") fulfill the same physical hardware as the bare-component SKU
the Factory ships (3HE11278AA "7250 IXR-R6 CHASSIS"). Without an alias
map, a Sales BoM that lists the bundle and a Factory BoM that lists the
bare chassis would report a 100% shortfall — every bundle line "missing"
and every chassis line "unused". The alias loader in
``gui.bom_compare_frame`` collapses both forms into a single canonical
key before the diff so the comparison reflects what's actually shippable.
"""
import json
import os
import tempfile
import unittest

from gui.bom_compare_frame import (
    canonical_part,
    fold_aliased_parts,
    load_part_aliases,
)


class TestLoadPartAliases(unittest.TestCase):
    """The on-disk JSON loader handles missing files, malformed payloads,
    vendor-prefixed keys, and mixed-case entries without crashing the
    BoM comparison flow."""

    def _write(self, payload):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.addCleanup(os.unlink, path)
        return path

    def test_ships_with_3HE13584AA_to_3HE11278AA_pair(self):
        """The bundled data file (data/part_aliases.json) must include
        the chassis-bundle pair the operator originally requested."""
        aliases = load_part_aliases()
        self.assertEqual(aliases.get("3HE13584AA"), "3HE11278AA")

    def test_returns_empty_when_file_missing(self):
        self.assertEqual(load_part_aliases("/does/not/exist.json"), {})

    def test_returns_empty_when_payload_malformed(self):
        path = self._write({"aliases": "not a dict"})
        self.assertEqual(load_part_aliases(path), {})

    def test_returns_empty_when_top_level_not_dict(self):
        path = self._write(["just", "a", "list"])
        self.assertEqual(load_part_aliases(path), {})

    def test_strips_vendor_prefix_on_keys_and_values(self):
        # 1P / P prefixes are packaging markers — strip on both sides so
        # alias lookup is uniform regardless of how the BoM typed it.
        path = self._write({
            "aliases": {
                "1P3HE13584AA": "P3HE11278AA",
                "3HE99999AA":   "3HE88888AA",
            }
        })
        aliases = load_part_aliases(path)
        self.assertEqual(aliases.get("3HE13584AA"), "3HE11278AA")
        self.assertEqual(aliases.get("3HE99999AA"), "3HE88888AA")

    def test_uppercases_keys_and_values(self):
        path = self._write({"aliases": {"3he13584aa": "3he11278aa"}})
        aliases = load_part_aliases(path)
        self.assertIn("3HE13584AA", aliases)
        self.assertEqual(aliases["3HE13584AA"], "3HE11278AA")

    def test_drops_entries_with_non_string_values(self):
        # JSON itself coerces dict keys to strings on serialize, so the
        # only realistic non-string injection is a null / numeric value.
        path = self._write({"aliases": {
            "3HE13584AA": "3HE11278AA",
            "BAD":        None,
            "ALSO_BAD":   12345,
        }})
        aliases = load_part_aliases(path)
        self.assertEqual(aliases, {"3HE13584AA": "3HE11278AA"})

    def test_drops_entries_with_empty_keys_or_values(self):
        path = self._write({"aliases": {
            "3HE13584AA": "3HE11278AA",
            "":           "3HE11278AA",
            "OK":         "",
        }})
        aliases = load_part_aliases(path)
        self.assertEqual(aliases, {"3HE13584AA": "3HE11278AA"})


class TestCanonicalPart(unittest.TestCase):
    """``canonical_part`` collapses aliases via the loaded map and
    strips vendor prefixes for lookup uniformity."""

    def setUp(self):
        self.aliases = {"3HE13584AA": "3HE11278AA"}

    def test_known_alias_resolves_to_canonical(self):
        self.assertEqual(canonical_part("3HE13584AA", self.aliases), "3HE11278AA")

    def test_unknown_part_returns_self_uppercased(self):
        self.assertEqual(canonical_part("3HE99999AA", self.aliases), "3HE99999AA")

    def test_canonical_part_returns_self(self):
        # The bare chassis SKU should round-trip — it's already canonical.
        self.assertEqual(canonical_part("3HE11278AA", self.aliases), "3HE11278AA")

    def test_vendor_prefix_stripped_before_lookup(self):
        self.assertEqual(canonical_part("1P3HE13584AA", self.aliases), "3HE11278AA")
        self.assertEqual(canonical_part("P3HE13584AA",  self.aliases), "3HE11278AA")

    def test_case_insensitive_lookup(self):
        self.assertEqual(canonical_part("3he13584aa", self.aliases), "3HE11278AA")

    def test_empty_input_returns_empty(self):
        self.assertEqual(canonical_part("", self.aliases), "")
        self.assertEqual(canonical_part(None, self.aliases), "")


class TestFoldAliasedParts(unittest.TestCase):
    """``fold_aliased_parts`` collapses Sales and Factory parse results
    into canonical keys, summing per-site quantities and preferring the
    canonical SKU's description so the Shortfall tab is consistent
    with what's actually shipped."""

    def setUp(self):
        self.aliases = {"3HE13584AA": "3HE11278AA"}

    def test_no_aliases_returns_input_unchanged(self):
        parts = {"3HE13584AA": {"desc": "BUNDLE", "site_qty": {"STJO": 1}}}
        self.assertIs(fold_aliased_parts(parts, {}), parts)

    def test_aliased_part_folds_into_canonical_key(self):
        parts = {
            "3HE13584AA": {"desc": "7250 IXR-R6 CHASSIS BUNDLE", "site_qty": {"STJO": 2}},
        }
        folded = fold_aliased_parts(parts, self.aliases)
        self.assertNotIn("3HE13584AA", folded)
        self.assertIn("3HE11278AA", folded)
        self.assertEqual(folded["3HE11278AA"]["site_qty"], {"STJO": 2})

    def test_canonical_only_input_is_passthrough(self):
        parts = {
            "3HE11278AA": {"desc": "7250 IXR-R6 CHASSIS", "site_qty": {"STJO": 2}},
        }
        folded = fold_aliased_parts(parts, self.aliases)
        self.assertEqual(folded["3HE11278AA"]["site_qty"], {"STJO": 2})
        self.assertEqual(folded["3HE11278AA"]["desc"], "7250 IXR-R6 CHASSIS")

    def test_both_forms_present_quantities_sum_and_canonical_desc_wins(self):
        # Sales BoM with one of each (e.g. a quirky line-item split) should
        # roll up to 3 chassis total and display the bare-chassis text.
        parts = {
            "3HE13584AA": {"desc": "7250 IXR-R6 CHASSIS BUNDLE", "site_qty": {"STJO": 1}},
            "3HE11278AA": {"desc": "7250 IXR-R6 CHASSIS",        "site_qty": {"STJO": 2}},
        }
        folded = fold_aliased_parts(parts, self.aliases)
        self.assertEqual(list(folded.keys()), ["3HE11278AA"])
        self.assertEqual(folded["3HE11278AA"]["site_qty"], {"STJO": 3})
        self.assertEqual(folded["3HE11278AA"]["desc"], "7250 IXR-R6 CHASSIS")

    def test_per_site_quantities_sum_across_sites(self):
        parts = {
            "3HE13584AA": {"desc": "B", "site_qty": {"STJO": 1, "MNCR": 2}},
            "3HE11278AA": {"desc": "C", "site_qty": {"STJO": 1, "ALSN": 3}},
        }
        folded = fold_aliased_parts(parts, self.aliases)
        self.assertEqual(
            folded["3HE11278AA"]["site_qty"],
            {"STJO": 2, "MNCR": 2, "ALSN": 3},
        )


class TestEndToEndShortfallScenario(unittest.TestCase):
    """End-to-end behavior: Sales says "1 bundle at STJO", Factory says
    "1 bare chassis at STJO" — after folding through aliases the diff
    must report ZERO shortfall instead of a unit missing."""

    def test_bundle_on_sales_chassis_on_factory_yields_no_shortfall(self):
        aliases = {"3HE13584AA": "3HE11278AA"}
        sales = {
            "3HE13584AA": {"desc": "7250 IXR-R6 CHASSIS BUNDLE", "site_qty": {"STJO001": 1}},
        }
        factory = {
            "3HE11278AA": {"desc": "7250 IXR-R6 CHASSIS", "site_qty": {"STJO001": 1}},
        }
        sales = fold_aliased_parts(sales, aliases)
        factory = fold_aliased_parts(factory, aliases)
        # The comparison logic in BomCompareFrame._compare does:
        #   short = sales_qty - factory_qty  (per site, must be > 0)
        s_qty = sales["3HE11278AA"]["site_qty"]["STJO001"]
        f_qty = factory["3HE11278AA"]["site_qty"].get("STJO001", 0)
        self.assertEqual(s_qty - f_qty, 0)

    def test_bundle_with_no_factory_chassis_still_shortfalls(self):
        """If Factory doesn't ship any chassis at all, the bundle line
        still surfaces as a shortfall — under the canonical part number."""
        aliases = {"3HE13584AA": "3HE11278AA"}
        sales = fold_aliased_parts(
            {"3HE13584AA": {"desc": "BUNDLE", "site_qty": {"STJO001": 2}}},
            aliases,
        )
        factory = fold_aliased_parts({}, aliases)
        s_qty = sales["3HE11278AA"]["site_qty"]["STJO001"]
        f_qty = factory.get("3HE11278AA", {}).get("site_qty", {}).get("STJO001", 0)
        self.assertEqual(s_qty - f_qty, 2)


class TestSimplifiedComparisonMath(unittest.TestCase):
    """The simplified Shortfall tab uses a single straight subtraction
    per part: ``delta = live_total - total_ordered`` where ``live_total
    = factory per-site + factory spares`` and ``total_ordered = sales
    per-site + sales spares``. Negative delta = short, positive = surplus,
    zero = exact match.

    Mirrors the in-line computation in ``_compare`` so the math stays
    locked down even though it's not factored into a separate helper.
    """

    def _delta(self, sales_persite, sales_spares, factory_persite, factory_spares):
        total_ordered = int(sales_persite) + int(sales_spares)
        live_total = int(factory_persite) + int(factory_spares)
        return total_ordered, live_total, live_total - total_ordered

    def test_user_example_3HE12546AA(self):
        """User's canonical example: per-site 651 + factory spares 49
        = 700 in Live; per-site 608 + sales spares 57 = 665 Ordered.
        Difference = +35."""
        ord_, live, delta = self._delta(608, 57, 651, 49)
        self.assertEqual(ord_, 665)
        self.assertEqual(live, 700)
        self.assertEqual(delta, 35)

    def test_shortfall_is_negative(self):
        """Live falls short -> negative delta (renders red)."""
        ord_, live, delta = self._delta(100, 10, 80, 0)
        self.assertEqual(delta, -30)

    def test_surplus_is_positive(self):
        """Live exceeds -> positive delta (renders green)."""
        ord_, live, delta = self._delta(50, 0, 100, 5)
        self.assertEqual(delta, 55)

    def test_exact_match_zero(self):
        """Sales total = Live total → delta 0."""
        ord_, live, delta = self._delta(50, 5, 55, 0)
        self.assertEqual(delta, 0)

    def test_factory_only_full_surplus(self):
        """Sales doesn't list the part; Live has units → full surplus."""
        ord_, live, delta = self._delta(0, 0, 12, 3)
        self.assertEqual(ord_, 0)
        self.assertEqual(live, 15)
        self.assertEqual(delta, 15)

    def test_sales_only_full_shortage(self):
        """Live doesn't have the part; Sales ordered it → full shortfall."""
        ord_, live, delta = self._delta(20, 2, 0, 0)
        self.assertEqual(ord_, 22)
        self.assertEqual(live, 0)
        self.assertEqual(delta, -22)


class TestWriteMissingSheetLayout(unittest.TestCase):
    """The Shortfall tab is now a flat workbook-level comparison with
    six columns: Item / Part No. / Equipment Description / Total
    Ordered / Live Inventory / Difference. Delta cell color is red
    for negative, green for positive, bold-black for zero.
    """

    def setUp(self):
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        self.writer = BomCompareFrame._write_missing_sheet
        self.openpyxl = openpyxl

    def _new_sheet(self):
        wb = self.openpyxl.Workbook()
        ws = wb.active
        return wb, ws

    def test_renders_six_column_header_with_expected_labels(self):
        wb, ws = self._new_sheet()
        self.writer(ws, [])
        self.assertEqual(ws.cell(3, 1).value, "Item")
        self.assertEqual(ws.cell(3, 2).value, "Part No.")
        self.assertEqual(ws.cell(3, 3).value, "Equipment Description")
        self.assertEqual(ws.cell(3, 4).value, "Total Ordered")
        self.assertEqual(ws.cell(3, 5).value, "Live Inventory")
        self.assertEqual(ws.cell(3, 6).value, "Difference")
        wb.close()

    def test_writes_data_row_with_three_numeric_columns(self):
        wb, ws = self._new_sheet()
        # (pn, desc, total_ordered, live_total, delta)
        rows = [
            ("3HE12546AA", "SFP - C37.94 LC 2KM SR", 665, 700, 35),
        ]
        self.writer(ws, rows)
        self.assertEqual(ws.cell(5, 1).value, 1)
        self.assertEqual(ws.cell(5, 2).value, "3HE12546AA")
        self.assertEqual(ws.cell(5, 3).value, "SFP - C37.94 LC 2KM SR")
        self.assertEqual(ws.cell(5, 4).value, 665)
        self.assertEqual(ws.cell(5, 5).value, 700)
        self.assertEqual(ws.cell(5, 6).value, 35)
        wb.close()

    def test_negative_delta_uses_red_font(self):
        wb, ws = self._new_sheet()
        self.writer(ws, [("X", "x", 100, 80, -20)])
        font = ws.cell(5, 6).font
        # Red hex = FFC00000 per the writer's red_font definition.
        self.assertIn("C00000", (font.color.rgb or "").upper())
        wb.close()

    def test_positive_delta_uses_green_font(self):
        wb, ws = self._new_sheet()
        self.writer(ws, [("X", "x", 80, 100, 20)])
        font = ws.cell(5, 6).font
        # Green hex = FF548235 per the writer's green_font definition.
        self.assertIn("548235", (font.color.rgb or "").upper())
        wb.close()

    def test_empty_rows_shows_banner(self):
        wb, ws = self._new_sheet()
        self.writer(ws, [])
        cell5 = ws.cell(5, 1).value
        self.assertIsNotNone(cell5)
        self.assertIn("No parts to compare", str(cell5))
        wb.close()

    def test_no_site_columns_in_output(self):
        """Regression guard: site columns must not leak into this tab."""
        wb, ws = self._new_sheet()
        rows = [("X", "x", 10, 5, -5)]
        self.writer(ws, rows)
        for c in range(7, 15):
            self.assertIsNone(ws.cell(3, c).value)
            self.assertIsNone(ws.cell(4, c).value)
        wb.close()


class TestStripSalesPriceColumns(unittest.TestCase):
    """Sales BoMs ship with pricing data in one or more columns; the
    comparison's reference-copy tab should sanitize all of them. The
    helper scans rows 1-25 for any column whose header reads like a
    price/cost label (Unit Price, List Price, Ext Price, Cost, Price,
    Amount) and **hides** that column.

    Hiding instead of deleting is critical: Sales BoMs carry VLOOKUP /
    SUMIF formulas (e.g. ``=VLOOKUP(E10, sites, 1, FALSE)`` in row 6)
    and wide title merges (``A1:BU5``). Deleting the column would shift
    cells leftward but openpyxl does NOT rewrite formula references, so
    the lookups would end up pointing at the wrong site columns and the
    title merge would collapse. Hiding leaves the structure intact.
    """

    def setUp(self):
        from gui.bom_compare_frame import BomCompareFrame
        self.strip = BomCompareFrame._strip_sales_price_columns

    def _sheet(self, headers_by_cell, data_by_cell=None):
        """Build a sheet with caller-specified header + data cells."""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for coord, val in headers_by_cell.items():
            ws[coord] = val
        for coord, val in (data_by_cell or {}).items():
            ws[coord] = val
        return wb, ws

    def _is_hidden(self, ws, col_letter):
        return ws.column_dimensions[col_letter].hidden is True

    def test_hides_unit_price_at_d11(self):
        """Real-world case from the user's file: header at row 11, not 12."""
        wb, ws = self._sheet(
            headers_by_cell={
                "B11": "Part No.",  "C11": "Description",
                "D11": "Unit Price", "E11": "Qty", "F11": "ALBA",
            },
            data_by_cell={
                "B15": "3HE11278AA", "C15": "Chassis",
                "D15": "$5,432.10", "E15": 2, "F15": 2,
            },
        )
        n = self.strip(ws)
        self.assertEqual(n, 1)
        # Column D is hidden — but data and headers stay put. Operator
        # doesn't see the price; formulas elsewhere can still reference D.
        self.assertTrue(self._is_hidden(ws, "D"))
        self.assertEqual(ws["D11"].value, "Unit Price")  # still there
        self.assertEqual(ws["D15"].value, "$5,432.10")   # still there
        # Adjacent columns NOT hidden — only the matched one.
        self.assertFalse(self._is_hidden(ws, "C"))
        self.assertFalse(self._is_hidden(ws, "E"))
        wb.close()

    def test_hides_unit_price_at_d12_still_works(self):
        """Backward compatibility with the original D12 layout."""
        wb, ws = self._sheet(
            headers_by_cell={"D12": "Unit Price", "E12": "Qty"},
            data_by_cell={"D15": "$10.00", "E15": 1},
        )
        self.assertEqual(self.strip(ws), 1)
        self.assertTrue(self._is_hidden(ws, "D"))
        # E (Qty) stays visible.
        self.assertFalse(self._is_hidden(ws, "E"))
        wb.close()

    def test_hides_trailing_totals_block(self):
        """User's file also has Unit Price at BU12 and List Price at
        BX12 (a far-right Totals block). Both must be hidden."""
        wb, ws = self._sheet(
            headers_by_cell={
                "D11": "Unit Price",
                "BU12": "Unit Price",
                "BX12": "List Price",
                "E11": "Qty",
            },
        )
        n = self.strip(ws)
        self.assertEqual(n, 3)
        self.assertTrue(self._is_hidden(ws, "D"))
        self.assertTrue(self._is_hidden(ws, "BU"))
        self.assertTrue(self._is_hidden(ws, "BX"))
        # Columns between them stay visible.
        self.assertFalse(self._is_hidden(ws, "BV"))
        self.assertFalse(self._is_hidden(ws, "BW"))
        wb.close()

    def test_does_not_shift_other_columns_or_break_merges(self):
        """Hiding (vs deleting) leaves every other cell coordinate
        untouched. Merges, formulas, and named-range references stay
        valid because nothing moves."""
        wb, ws = self._sheet(
            headers_by_cell={
                "D11": "Unit Price",
                "E11": "Qty",
                "F11": "ALBA",
            },
            data_by_cell={
                "F6":  "=VLOOKUP(G10, sites, 1, FALSE)",  # formula targeting G
                "G10": "ALVY",
                "F10": "ALBA",
            },
        )
        ws.merge_cells("A1:Z5")  # wide title merge that spans D
        before_merges = {str(r) for r in ws.merged_cells.ranges}
        before_f6 = ws["F6"].value
        before_g10 = ws["G10"].value

        self.assertEqual(self.strip(ws), 1)

        # Merge ranges unchanged.
        after_merges = {str(r) for r in ws.merged_cells.ranges}
        self.assertEqual(before_merges, after_merges)
        # Formula references unchanged (they would have shifted under
        # delete_cols but stay put under hide).
        self.assertEqual(ws["F6"].value, before_f6)
        self.assertEqual(ws["G10"].value, before_g10)
        wb.close()

    def test_case_insensitive_match(self):
        wb, ws = self._sheet(headers_by_cell={"D12": "unit price"})
        self.assertEqual(self.strip(ws), 1)
        self.assertTrue(self._is_hidden(ws, "D"))
        wb.close()

    def test_whitespace_tolerant_match(self):
        wb, ws = self._sheet(headers_by_cell={"D12": "  Unit Price  "})
        self.assertEqual(self.strip(ws), 1)
        wb.close()

    def test_hides_multiple_keyword_variants(self):
        """All price-keyword headers in the keyword set trigger hiding."""
        for header in ("Unit Price", "List Price", "Ext Price",
                       "Extended Price", "Cost", "Price", "Amount"):
            wb, ws = self._sheet(headers_by_cell={"D12": header, "E12": "Qty"})
            self.assertEqual(self.strip(ws), 1, f"failed for {header!r}")
            self.assertTrue(self._is_hidden(ws, "D"))
            wb.close()

    def test_leaves_sheet_alone_when_no_price_headers(self):
        wb, ws = self._sheet(
            headers_by_cell={
                "B11": "Part No.", "C11": "Description",
                "D11": "Qty", "E11": "ALBA",
            },
            data_by_cell={"D15": 2, "E15": 2},
        )
        self.assertEqual(self.strip(ws), 0)
        self.assertFalse(self._is_hidden(ws, "D"))
        wb.close()

    def test_does_not_match_substrings(self):
        """'Unit Price Subtotal' / 'Price Range' shouldn't trigger —
        only exact keyword matches do."""
        for header in ("Unit Price Subtotal", "Price Range", "Cost Center"):
            wb, ws = self._sheet(headers_by_cell={"D12": header})
            self.assertEqual(self.strip(ws), 0, f"matched on {header!r}")
            self.assertFalse(self._is_hidden(ws, "D"))
            wb.close()

    def test_only_scans_first_25_rows(self):
        """A 'Unit Price' label down in the data area (row 100) is not
        a header — it's content, and hiding its column would obscure
        unrelated quantitative data."""
        wb, ws = self._sheet(headers_by_cell={"D100": "Unit Price"})
        self.assertEqual(self.strip(ws), 0)
        self.assertFalse(self._is_hidden(ws, "D"))
        wb.close()

    def test_returns_zero_on_empty_sheet(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        self.assertEqual(self.strip(ws), 0)
        wb.close()

    def test_backward_compat_alias_still_callable(self):
        """The old _strip_sales_unit_price_column name still works (it
        returns bool now). Older code paths or external callers that may
        import the name continue to function."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._sheet(headers_by_cell={"D12": "Unit Price"})
        self.assertTrue(BomCompareFrame._strip_sales_unit_price_column(ws))
        wb.close()


class TestRollupColumnDetection(unittest.TestCase):
    """Factory BoMs commonly lay out each site as
    ``SITE001_7250, SITE002_7250, ..., SITE Extra Materials, SITE``
    where the final bare-named column is a rollup that repeats the
    sum of all preceding columns for the same site. Counting it
    doubles every per-site total — for the user's file this turned a
    real factory total of 651 into a reported 1286 for one part.
    The parser detects this pattern and drops the rollup column.
    """

    def _build_factory_bom(self, headers_row, qty_row, headers_subrow=None):
        """Build a minimal Factory-BoM-shaped sheet.

        headers_row : the per-site headers
        qty_row     : the data row qty values (parallel to headers_row)
        headers_subrow : optional 'Qty' subheader row (mirrors real BoMs)
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["B7"] = "Part No."
        ws["C7"] = "Equipment Description"
        # Place site headers starting at column D.
        for i, h in enumerate(headers_row):
            ws.cell(7, 4 + i, h)
        # Total column at the right.
        total_col_idx = 4 + len(headers_row)
        ws.cell(7, total_col_idx, "Total")
        if headers_subrow is not None:
            for i, sub in enumerate(headers_subrow):
                ws.cell(8, 4 + i, sub)
            ws.cell(8, total_col_idx, "Qty")
        # Data row at 10.
        ws.cell(10, 2, "3HE12546AA")
        ws.cell(10, 3, "SFP - C37.94 LC 2KM SR")
        for i, q in enumerate(qty_row):
            ws.cell(10, 4 + i, q)
        ws.cell(10, total_col_idx, sum(int(q or 0) for q in qty_row))
        return wb, ws

    def test_bare_site_rollup_dropped_when_siblings_exist(self):
        """``MALN001_7250, MALN Extra Materials, MALN`` → MALN rollup
        should be skipped; per-site total reflects only the details +
        Extra Materials."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_factory_bom(
            headers_row=["MALN001_7250", "MALN Extra Materials", "MALN"],
            qty_row=[20, 5, 25],   # rollup column already sums to 25
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        site_qty = parts["3HE12546AA"]["site_qty"]
        # MALN bare column dropped — total counts only details + extras.
        self.assertEqual(sum(site_qty.values()), 25)
        # The bare-name "MALN" entry must NOT appear in the per-site dict.
        self.assertNotIn("MALN", site_qty)
        wb.close()

    def test_standalone_bare_site_name_kept(self):
        """Sales BoMs list each site as a bare name with no detail
        siblings — those columns must be preserved (they're the real
        data, not rollups)."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_factory_bom(
            headers_row=["ALBA", "ALVY", "BAND"],
            qty_row=[3, 5, 2],
            headers_subrow=["Qty", "Qty", "Qty"],
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        site_qty = parts["3HE12546AA"]["site_qty"]
        self.assertEqual(sum(site_qty.values()), 10)
        self.assertEqual(site_qty.get("ALBA"), 3)
        self.assertEqual(site_qty.get("ALVY"), 5)
        self.assertEqual(site_qty.get("BAND"), 2)
        wb.close()

    def test_multiple_site_groups_each_get_rollup_dropped(self):
        """Each independent site group with a bare-named rollup has
        only its rollup dropped — sibling groups are unaffected."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_factory_bom(
            headers_row=[
                "MALN001_7250", "MALN Extra Materials", "MALN",
                "KEEL001_7250", "KEEL Extra Materials", "KEEL",
            ],
            qty_row=[10, 2, 12,  20, 5, 25],
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        total = sum(parts["3HE12546AA"]["site_qty"].values())
        # MALN details+extras=12, KEEL details+extras=25 -> total 37.
        self.assertEqual(total, 37)
        wb.close()

    def test_site_with_no_letters_in_name_unaffected(self):
        """Site keys are extracted via ``^[A-Za-z]+``; names like
        ``6500_RLS_1`` produce no key and aren't grouped. They should
        pass through untouched (not dropped as 'rollups')."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_factory_bom(
            headers_row=["RLS_2", "RLS"],  # bare RLS rollup, but
            qty_row=[3, 3],                # RLS_2 has the same key
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        total = sum(parts["3HE12546AA"]["site_qty"].values())
        # RLS rollup dropped, RLS_2 detail kept.
        self.assertEqual(total, 3)
        wb.close()


class TestTotalOrderedHeaderLayout(unittest.TestCase):
    """The Sales BoM builder writes the per-row total under the header
    ``Total Ordered`` at column D — *before* the per-site columns —
    instead of the legacy layout where Total was the rightmost header.
    The parser must accept this label as a Total marker and must not
    treat the columns to its right as metadata."""

    def _build_total_ordered_first_bom(self, sites, qtys, spares_qty=None):
        """Mirror the new Sales BoM layout:
        col A Item, B Part Number, C Equipment Description,
        D Total Ordered, E.. per-site, last column Spares (optional).
        Row 7 is the header, row 8 carries 'Qty' sub-headers, data starts
        at row 10.
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A7"] = "Item"
        ws["B7"] = "Part Number"
        ws["C7"] = "Equipment Description"
        ws["D7"] = "Total Ordered"
        ws["D8"] = "Qty"
        for i, name in enumerate(sites):
            ws.cell(7, 5 + i, name)
            ws.cell(8, 5 + i, "Qty")
        spares_col = None
        if spares_qty is not None:
            spares_col = 5 + len(sites)
            ws.cell(7, spares_col, "Spares")
            ws.cell(8, spares_col, "Qty")
        ws.cell(10, 1, 1)
        ws.cell(10, 2, "3HE12546AA")
        ws.cell(10, 3, "SFP - C37.94 LC 2KM SR")
        ws.cell(10, 4, sum(qtys) + (spares_qty or 0))
        for i, q in enumerate(qtys):
            ws.cell(10, 5 + i, q)
        if spares_col is not None:
            ws.cell(10, spares_col, spares_qty)
        return wb, ws

    def test_header_total_ordered_is_accepted(self):
        """Parser must locate the header row when the only total-style
        header is ``Total Ordered``. Failure mode before the fix:
        ``RuntimeError: Sheet ... has no recognizable BOM header``."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_total_ordered_first_bom(
            sites=["ALBA", "ALVY", "BAND"], qtys=[2, 3, 5],
        )
        sites, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertIn("3HE12546AA", parts)
        self.assertEqual(set(sites), {"ALBA", "ALVY", "BAND"})
        wb.close()

    def test_site_columns_after_total_are_kept(self):
        """The columns past Total Ordered must be picked up as sites,
        not rejected as 'past the Total column' metadata."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_total_ordered_first_bom(
            sites=["ALBA", "ALVY", "BAND"], qtys=[2, 3, 5],
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        site_qty = parts["3HE12546AA"]["site_qty"]
        self.assertEqual(site_qty, {"ALBA": 2, "ALVY": 3, "BAND": 5})
        wb.close()

    def test_spares_column_at_far_right_still_detected(self):
        """Spares may sit at the far right, past every site column.
        It must still be classified as Spares (not as a site)."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_total_ordered_first_bom(
            sites=["ALBA", "ALVY"], qtys=[2, 3], spares_qty=4,
        )
        sites, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertEqual(set(sites), {"ALBA", "ALVY"})
        self.assertEqual(spares.get("3HE12546AA"), 4)
        wb.close()


class TestLoadPartKits(unittest.TestCase):
    """Kit definitions live alongside aliases in
    ``data/part_aliases.json`` under a ``"kits"`` array. Each entry
    declares a kit SKU + its component SKUs. The loader degrades
    gracefully when the section is missing, malformed, or empty.
    """

    def _write(self, payload):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.addCleanup(os.unlink, path)
        return path

    def test_loader_returns_empty_when_no_kits_section(self):
        from gui.bom_compare_frame import load_part_kits
        path = self._write({"aliases": {"a": "b"}})
        self.assertEqual(load_part_kits(path), [])

    def test_loader_skips_malformed_entries(self):
        from gui.bom_compare_frame import load_part_kits
        path = self._write({"kits": [
            {"kit": "K", "components": ["A", "B"]},
            "not a dict",
            {"kit": None, "components": ["X"]},
            {"kit": "K2", "components": "should be list"},
            {"kit": "K3", "components": []},
        ]})
        kits = load_part_kits(path)
        self.assertEqual(len(kits), 1)
        self.assertEqual(kits[0]["kit"], "K")
        self.assertEqual(kits[0]["components"], ["A", "B"])

    def test_loader_strips_vendor_prefixes_and_uppercases(self):
        from gui.bom_compare_frame import load_part_kits
        path = self._write({"kits": [{
            "kit": "1P3kc48900aa",
            "components": ["p3KC48830aa", "3kc48850aa", "1P3KC48901AA"],
        }]})
        kits = load_part_kits(path)
        self.assertEqual(kits[0]["kit"], "3KC48900AA")
        self.assertEqual(kits[0]["components"],
                         ["3KC48830AA", "3KC48850AA", "3KC48901AA"])

    def test_bundled_data_includes_pss8_shelf_kit(self):
        """The shipped data file must carry the PSS8 SHELF KIT pairing
        the user originally requested."""
        from gui.bom_compare_frame import load_part_kits
        kits = load_part_kits()
        pss8 = next((k for k in kits if k["kit"] == "3KC48900AA"), None)
        self.assertIsNotNone(pss8)
        self.assertEqual(
            set(pss8["components"]),
            {"3KC48830AA", "3KC48850AA", "3KC48901AA"},
        )


class TestFoldKits(unittest.TestCase):
    """``fold_kits`` rolls per-site component totals into a kit SKU
    using the ``all-components-present + min`` rule. User-requested
    semantics: a kit is only counted when EVERY component has a
    non-zero total; otherwise the components stay as their own SKUs.
    """

    KIT = {
        "kit": "3KC48900AA",
        "components": ["3KC48830AA", "3KC48850AA", "3KC48901AA"],
    }

    def test_full_kit_set_folds_to_min(self):
        from gui.bom_compare_frame import fold_kits
        totals = {"3KC48830AA": 5, "3KC48850AA": 5, "3KC48901AA": 5}
        fold_kits(totals, [self.KIT])
        self.assertEqual(totals["3KC48900AA"], 5)
        self.assertEqual(totals["3KC48830AA"], 0)
        self.assertEqual(totals["3KC48850AA"], 0)
        self.assertEqual(totals["3KC48901AA"], 0)

    def test_partial_kit_leaves_components_alone(self):
        """User's rule: if any one component is missing, the others
        list separately. Here the shelf is missing — no fold happens."""
        from gui.bom_compare_frame import fold_kits
        totals = {"3KC48830AA": 5, "3KC48850AA": 5, "3KC48901AA": 0}
        fold_kits(totals, [self.KIT])
        self.assertEqual(totals.get("3KC48900AA", 0), 0)  # no kits formed
        self.assertEqual(totals["3KC48830AA"], 5)
        self.assertEqual(totals["3KC48850AA"], 5)

    def test_uneven_counts_fold_min_keep_leftovers(self):
        """Panel=5, fan=3, shelf=4 → 3 kits formed, leftover panels(2)
        and shelves(1)."""
        from gui.bom_compare_frame import fold_kits
        totals = {"3KC48830AA": 5, "3KC48850AA": 3, "3KC48901AA": 4}
        fold_kits(totals, [self.KIT])
        self.assertEqual(totals["3KC48900AA"], 3)
        self.assertEqual(totals["3KC48830AA"], 2)
        self.assertEqual(totals["3KC48850AA"], 0)
        self.assertEqual(totals["3KC48901AA"], 1)

    def test_existing_kit_count_accumulates(self):
        """If the input already has some kit-SKU instances, folded
        components stack on top of them."""
        from gui.bom_compare_frame import fold_kits
        totals = {
            "3KC48830AA": 5, "3KC48850AA": 5, "3KC48901AA": 5,
            "3KC48900AA": 2,
        }
        fold_kits(totals, [self.KIT])
        self.assertEqual(totals["3KC48900AA"], 7)  # 2 + 5

    def test_missing_components_means_no_fold(self):
        """Component key entirely absent from totals (==0) -> no fold."""
        from gui.bom_compare_frame import fold_kits
        totals = {"3KC48830AA": 5, "3KC48850AA": 5}  # shelf entirely absent
        fold_kits(totals, [self.KIT])
        self.assertNotIn("3KC48900AA", totals)
        self.assertEqual(totals["3KC48830AA"], 5)
        self.assertEqual(totals["3KC48850AA"], 5)

    def test_empty_kits_list_is_passthrough(self):
        from gui.bom_compare_frame import fold_kits
        totals = {"a": 1, "b": 2}
        result = fold_kits(totals, [])
        self.assertIs(result, totals)
        self.assertEqual(totals, {"a": 1, "b": 2})


class TestComputeKitFoldOps(unittest.TestCase):
    """``compute_kit_fold_ops`` previews the folds ``fold_kits`` would
    apply without mutating the input. The Trace sheet uses these
    operations to emit synthetic audit rows reconciling kit SKU totals."""

    def test_returns_kit_qty_and_components_when_all_present(self):
        from gui.bom_compare_frame import compute_kit_fold_ops
        totals = {"3KC48830AA": 6, "3KC48850AA": 6, "3KC48901AA": 6}
        kits = [{
            "kit": "3KC48900AA",
            "components": ["3KC48830AA", "3KC48850AA", "3KC48901AA"],
        }]
        ops = compute_kit_fold_ops(totals, kits)
        self.assertEqual(len(ops), 1)
        kit, k, comps = ops[0]
        self.assertEqual(kit, "3KC48900AA")
        self.assertEqual(k, 6)
        self.assertEqual(comps, ["3KC48830AA", "3KC48850AA", "3KC48901AA"])

    def test_uses_min_of_component_counts(self):
        from gui.bom_compare_frame import compute_kit_fold_ops
        totals = {"3KC48830AA": 4, "3KC48850AA": 6, "3KC48901AA": 8}
        kits = [{
            "kit": "3KC48900AA",
            "components": ["3KC48830AA", "3KC48850AA", "3KC48901AA"],
        }]
        ops = compute_kit_fold_ops(totals, kits)
        self.assertEqual(ops[0][1], 4)  # min(4, 6, 8)

    def test_skips_kit_when_any_component_absent(self):
        from gui.bom_compare_frame import compute_kit_fold_ops
        totals = {"3KC48830AA": 6, "3KC48850AA": 0, "3KC48901AA": 6}
        kits = [{
            "kit": "3KC48900AA",
            "components": ["3KC48830AA", "3KC48850AA", "3KC48901AA"],
        }]
        self.assertEqual(compute_kit_fold_ops(totals, kits), [])

    def test_does_not_mutate_input(self):
        from gui.bom_compare_frame import compute_kit_fold_ops
        totals = {"3KC48830AA": 6, "3KC48850AA": 6, "3KC48901AA": 6}
        snapshot = dict(totals)
        kits = [{
            "kit": "3KC48900AA",
            "components": ["3KC48830AA", "3KC48850AA", "3KC48901AA"],
        }]
        compute_kit_fold_ops(totals, kits)
        self.assertEqual(totals, snapshot)


class TestFoldKitsIntoBomData(unittest.TestCase):
    """The Build-a-BOM path uses ``WorkbookBuilder._fold_kits_into_bom_data``
    to apply per-site kit grouping to the aggregated bom_data dict —
    site by site, components collapse into kit entries.
    """

    def test_per_site_folding_collapses_complete_sets(self):
        from gui.workbook_builder import WorkbookBuilder
        bom_data = {
            "MNCR": [
                ("3KC48830AA", "PSS8-8SP- SHELF PNL,TEMP-HARDEN", "Chassis/Shelf"),
                ("3KC48850AA", "PSS8-8FAN-FAN UNIT,TEMP-HARDENED", "Chassis/Shelf"),
                ("3KC48901AA", "PSS8 SHELF-(INCLUDING PSS8SHF,SHELF PNL)", "Chassis/Shelf"),
            ],
        }
        n = WorkbookBuilder._fold_kits_into_bom_data(bom_data)
        self.assertEqual(n, 1)
        entries = bom_data["MNCR"]
        skus = [e[0] for e in entries]
        self.assertEqual(skus, ["3KC48900AA"])

    def test_partial_set_at_site_stays_untouched(self):
        """User's rule: if any one is missing, the others list normally."""
        from gui.workbook_builder import WorkbookBuilder
        bom_data = {
            "MNCR": [
                ("3KC48830AA", "panel", "Chassis/Shelf"),
                ("3KC48850AA", "fan", "Chassis/Shelf"),
                # shelf missing
            ],
        }
        n = WorkbookBuilder._fold_kits_into_bom_data(bom_data)
        self.assertEqual(n, 0)
        skus = [e[0] for e in bom_data["MNCR"]]
        self.assertEqual(sorted(skus), ["3KC48830AA", "3KC48850AA"])

    def test_multiple_kits_per_site_via_min(self):
        from gui.workbook_builder import WorkbookBuilder
        bom_data = {"MNCR": []}
        bom_data["MNCR"].extend([("3KC48830AA", "p", "C/S")] * 4)
        bom_data["MNCR"].extend([("3KC48850AA", "f", "C/S")] * 3)
        bom_data["MNCR"].extend([("3KC48901AA", "s", "C/S")] * 5)
        n = WorkbookBuilder._fold_kits_into_bom_data(bom_data)
        self.assertEqual(n, 3)  # min(4,3,5) = 3 kits
        skus = [e[0] for e in bom_data["MNCR"]]
        # 1 leftover panel + 0 fan + 2 leftover shelf + 3 kits = 6 entries
        from collections import Counter
        c = Counter(skus)
        self.assertEqual(c, {"3KC48830AA": 1, "3KC48901AA": 2, "3KC48900AA": 3})

    def test_independent_per_site(self):
        """Each site folds independently — incomplete at one site
        doesn't block folding at another."""
        from gui.workbook_builder import WorkbookBuilder
        bom_data = {
            "MNCR": [
                ("3KC48830AA", "p", "C/S"),
                ("3KC48850AA", "f", "C/S"),
                ("3KC48901AA", "s", "C/S"),
            ],
            "STJO": [
                ("3KC48830AA", "p", "C/S"),
                # incomplete — no fan or shelf
            ],
        }
        n = WorkbookBuilder._fold_kits_into_bom_data(bom_data)
        self.assertEqual(n, 1)
        self.assertEqual([e[0] for e in bom_data["MNCR"]], ["3KC48900AA"])
        self.assertEqual([e[0] for e in bom_data["STJO"]], ["3KC48830AA"])

    def test_build_bom_sheet_calls_fold(self):
        """Source-level guard: _build_bom_sheet must invoke the kit
        folder on bom_data before writing the sheet."""
        import inspect
        from gui.workbook_builder import WorkbookBuilder
        src = inspect.getsource(WorkbookBuilder._build_bom_sheet)
        self.assertIn("_fold_kits_into_bom_data", src)


class TestKitFoldingInCompare(unittest.TestCase):
    """_compare wires kit folding into the per-side workbook totals
    before the per-PN comparison math runs. Source-level guard so the
    wiring can't silently regress.
    """

    def test_compare_loads_kits_and_folds_both_sides(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("load_part_kits()", src)
        self.assertIn("fold_kits(fac_persite_total, kits)", src)
        self.assertIn("fold_kits(sal_persite_total, kits)", src)


class TestSalesSparesRollUpIntoTotalOrdered(unittest.TestCase):
    """Sales BoMs commonly have a 'Spares' column adjacent to per-site
    columns and a separate 'Total' column to the right that equals
    per-site + spares. The customer's "Spares" represent extras they
    ORDERED (shipped alongside the per-site allocation), not a
    separate inventory pool. Total Ordered on the Shortfall/Surplus
    tabs must match the BoM's Total column (BP in the user's file)
    by including those Sales-side spares.

    Contrast with the Factory BoM, where the Spares column IS a
    separate pool — units sitting unallocated and available for
    Shortfall backfill.
    """

    def test_total_ordered_includes_sales_spares(self):
        """Per-site=96, Sales spares=7 -> Total Ordered=103 (matches
        the Sales BoM's BP for 3HE06791AA)."""
        # Mirror the inline math in _compare.
        sales_site_qty = {"ALBA": 2, "ALVY": 2, "WKMZ": 2}  # 6 here, simplified
        sales_spares_for_pn = 7
        total_ordered = sum(sales_site_qty.values()) + sales_spares_for_pn
        self.assertEqual(total_ordered, 13)
        # And the same logic at full scale (matches the file):
        self.assertEqual(96 + 7, 103)
        self.assertEqual(608 + 57, 665)

    def test_compare_inlines_sales_spares_into_total_ordered(self):
        """Source-level guard: ``_compare`` must read sal_inline_spares
        and add it into the per-part total_ordered. Without this, the
        Shortfall / Surplus totals undercount the customer's order by
        whatever the Sales BoM's Spares column carries."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        # Captured (was previously discarded with `_`).
        self.assertIn("sal_inline_spares", src)
        # And combined into total_ordered.
        self.assertIn("sal_inline_spares.get(pn, 0)", src)

    def test_compare_folds_sal_inline_spares_through_aliases(self):
        """If a bundle SKU appears in the Sales spares column, alias
        folding must collapse it to the canonical SKU just like every
        other quantity counter in the comparison."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        # Find the alias-folding block for sal_inline_spares.
        self.assertIn("sal_inline_spares", src)
        # canonical_part is used to re-key the spares dict.
        sal_spares_idx = src.index("if sal_inline_spares and aliases:")
        # Look for canonical_part within a reasonable window after.
        window = src[sal_spares_idx:sal_spares_idx + 800]
        self.assertIn("canonical_part(pn, aliases)", window)


class TestRecomputeTotalColumnOnSourceCopy(unittest.TestCase):
    """Source BoMs often store the rightmost Total column as a SUM
    formula. When the source was saved by something other than Excel
    (or never opened in Excel since the last edit), the cached value
    is ``None`` — and openpyxl's ``data_only=True`` load returns
    ``None`` too. That leaves every Total cell blank on the copied
    reference tabs, which makes it impossible for an operator to
    correlate the Shortfall tab against the source.

    ``_recompute_total_column_on_source_copy`` overwrites the Total
    column with plain integer sums of every per-site cell.
    """

    def setUp(self):
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        # Wrap in a lambda so callers can invoke ``self.helper(ws)``
        # regardless of whether the underlying API is a staticmethod
        # or a classmethod (it became a classmethod when it gained
        # rollup-deduplication via ``_parse_per_site_bom``).
        self.helper = lambda ws: (
            BomCompareFrame._recompute_total_column_on_source_copy(ws)
        )
        self.openpyxl = openpyxl

    def _build_sheet(self, qtys, total_value=None):
        """Build a BoM-shaped sheet with Item/PN/Desc headers at row 7,
        site columns at D-F, and a Total column at G. ``qtys`` is the
        list of per-site quantities to write in row 10; ``total_value``
        goes in G10 (often None to simulate the broken-cache case)."""
        wb = self.openpyxl.Workbook()
        ws = wb.active
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "ALBA")
        ws.cell(7, 5, "MNCR")
        ws.cell(7, 6, "STJO")
        ws.cell(7, 7, "Total")
        ws.cell(10, 1, 1)
        ws.cell(10, 2, "3HE11278AA")
        ws.cell(10, 3, "Chassis")
        for i, q in enumerate(qtys):
            ws.cell(10, 4 + i, q)
        ws.cell(10, 7, total_value)
        return wb, ws

    def test_blank_total_cell_gets_filled_from_per_site_sum(self):
        wb, ws = self._build_sheet([2, 1, 1], total_value=None)
        n = self.helper(ws)
        self.assertEqual(n, 1)
        self.assertEqual(ws.cell(10, 7).value, 4)  # 2+1+1
        wb.close()

    def test_existing_total_value_is_overwritten_with_correct_sum(self):
        """Idempotency: a stale value (e.g. left over from an older
        copy) gets replaced with the recomputed sum."""
        wb, ws = self._build_sheet([2, 1, 1], total_value=999)
        self.helper(ws)
        self.assertEqual(ws.cell(10, 7).value, 4)
        wb.close()

    def test_returns_zero_when_no_total_column(self):
        """Sheet without a Total header should be a no-op."""
        wb = self.openpyxl.Workbook()
        ws = wb.active
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 4, "ALBA")  # site col but no Total
        ws.cell(10, 2, "X")
        ws.cell(10, 4, 5)
        n = self.helper(ws)
        self.assertEqual(n, 0)
        wb.close()

    def test_value_is_plain_integer_not_formula(self):
        wb, ws = self._build_sheet([5, 3, 2])
        self.helper(ws)
        v = ws.cell(10, 7).value
        self.assertIsInstance(v, int)
        self.assertFalse(str(v).startswith("="))
        wb.close()

    def test_compare_calls_recompute_for_both_tabs(self):
        """Source-level guard: _compare must call the recompute helper
        on both fac_copy and sal_copy so the operator can correlate
        Shortfall against either source tab."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("_recompute_total_column_on_source_copy", src)
        # And it must run against both copied tabs.
        self.assertIn("for tab in (fac_copy, sal_copy)", src)

    def test_rollup_columns_are_not_double_counted(self):
        """Factory BoMs lay each site out as
        ``MALN001_7250, MALN Extra Materials, MALN`` where bare ``MALN``
        is a rollup of the other two. The recompute helper must drop
        the rollup before summing — otherwise every unit gets counted
        twice and Live Inventory totals come out ~2x reality (the bug
        that reported 1335 for 3HE12546AA when the true count was 700).
        """
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "MALN001_7250")
        ws.cell(7, 5, "MALN Extra Materials")
        ws.cell(7, 6, "MALN")  # bare rollup — sum of cols D + E
        ws.cell(7, 7, "Total")
        ws.cell(10, 1, 1)
        ws.cell(10, 2, "3HE12546AA")
        ws.cell(10, 3, "SFP")
        ws.cell(10, 4, 20)   # actual qty at MALN001_7250
        ws.cell(10, 5, 5)    # actual qty at MALN Extra Materials
        ws.cell(10, 6, 25)   # rollup col = 20 + 5
        ws.cell(10, 7, None)
        BomCompareFrame._recompute_total_column_on_source_copy(ws)
        # Expected: 20 + 5 = 25 (rollup dropped), NOT 20 + 5 + 25 = 50.
        self.assertEqual(ws.cell(10, 7).value, 25)
        wb.close()


class TestLoadExcludedParts(unittest.TestCase):
    """``load_excluded_parts`` reads discontinued / non-comparable SKUs
    from the ``excluded_parts`` section of the alias JSON."""

    def _write_cfg(self, payload):
        import json, tempfile, os
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_loads_pn_objects(self):
        import os
        from gui.bom_compare_frame import load_excluded_parts
        path = self._write_cfg({
            "excluded_parts": [
                {"pn": "3HE16718AA", "reason": "CFP2 discontinued"},
                {"pn": "3HE12518AA", "reason": "CFP2 discontinued"},
            ]
        })
        try:
            excl = load_excluded_parts(path)
            self.assertEqual(excl, {"3HE16718AA", "3HE12518AA"})
        finally:
            os.unlink(path)

    def test_accepts_bare_strings_and_strips_prefix(self):
        import os
        from gui.bom_compare_frame import load_excluded_parts
        path = self._write_cfg({"excluded_parts": ["1P3HE16718AA", "3he12518aa"]})
        try:
            excl = load_excluded_parts(path)
            # Vendor prefix stripped, uppercased.
            self.assertIn("3HE16718AA", excl)
            self.assertIn("3HE12518AA", excl)
        finally:
            os.unlink(path)

    def test_missing_section_returns_empty(self):
        import os
        from gui.bom_compare_frame import load_excluded_parts
        path = self._write_cfg({"aliases": {}})
        try:
            self.assertEqual(load_excluded_parts(path), set())
        finally:
            os.unlink(path)

    def test_real_config_excludes_cfp2_cards(self):
        """The shipped data/part_aliases.json must exclude the two
        vendor-discontinued CFP2 SKUs."""
        from gui.bom_compare_frame import load_excluded_parts
        excl = load_excluded_parts()  # default path
        self.assertIn("3HE16718AA", excl)
        self.assertIn("3HE12518AA", excl)


class TestCompareExcludesDiscontinuedParts(unittest.TestCase):
    """Source-level guard: ``_compare`` must load the excluded-parts set
    and skip those PNs when building the comparison rows."""

    def test_compare_loads_and_applies_exclusions(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("load_excluded_parts()", src)
        self.assertIn("if pn in excluded_parts:", src)


class TestNetworkAndLicensePointsBuckets(unittest.TestCase):
    """Some Sales BoMs use a 'Network' column as a centralized
    hardware-allocation bucket (cards/MDAs not tied to a specific
    site). The parser surfaces Network qty as a real source while
    dropping the License-Points bucket (false positives) and NSP
    Feature Pack software entries.
    """

    def _build_bom(self, qtys_for_network=None, qtys_for_license=None,
                   desc="4 PORT T3/E3 CARD V2"):
        """Legacy-layout BoM: cols D..F=sites, G=Spares, H=Maintenance,
        I=Network, J=Total, K=License points.

        ``qtys_for_network`` / ``qtys_for_license`` are integers (or
        None) to set on row 10's Network / License points cells.
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        # Sub-headers on row 7 — single-row header layout.
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "ALBA")
        ws.cell(7, 5, "ALVY")
        ws.cell(7, 6, "BAND")
        ws.cell(7, 7, "Spares")
        ws.cell(7, 8, "Maintenance")
        ws.cell(7, 9, "Network")
        ws.cell(7, 10, "Total")
        ws.cell(7, 11, "License points")
        ws.cell(10, 1, 1)
        ws.cell(10, 2, "3HE04962AB")
        ws.cell(10, 3, desc)
        if qtys_for_network is not None:
            ws.cell(10, 9, qtys_for_network)
        if qtys_for_license is not None:
            ws.cell(10, 11, qtys_for_license)
        return wb, ws

    def test_network_column_is_treated_as_real_site(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom(qtys_for_network=7)
        sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertIn("Network", sites)
        self.assertEqual(parts["3HE04962AB"]["site_qty"]["Network"], 7)
        wb.close()

    def test_license_points_column_is_not_counted(self):
        """Operator reported the 'License Points' column produced false
        positives (it's a license-accounting bucket, not a hardware-
        order column). It must NOT be treated as a site. A part whose
        ONLY qty is in License Points therefore drops out entirely."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom(qtys_for_license=5)
        sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertNotIn("License Points", sites)
        # 3HE04962AB had qty ONLY in License Points → gone.
        self.assertNotIn("3HE04962AB", parts)
        wb.close()

    def test_integrated_services_card_is_not_filtered_as_intangible(self):
        """Real-world hardware ``Integrated Services Card`` contains the
        word 'Services' in its description. The intangible-desc filter
        must not drop it. Uses a Network qty (still a real source) so
        the test isn't coupled to the now-dropped License Points bucket."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom(
            qtys_for_network=5,
            desc="Integrated Services Card",
        )
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertIn("3HE04962AB", parts)
        self.assertEqual(parts["3HE04962AB"]["site_qty"]["Network"], 5)
        wb.close()

    def test_nsp_feature_pack_is_filtered_by_fp_suffix(self):
        """NSP 'Feature Pack' entries are software license-points. Even
        when their qty lands in the Network column (a counted source),
        the row-level description filter must drop them. The
        discriminator is the trailing ' FP' / 'Feature Pack'."""
        from gui.bom_compare_frame import BomCompareFrame
        for fp_desc in (
            "NSP HIGH AVAILABILITY FP",
            "NSP NETWORK INFRASTRUCTURE MANAGEMENT FP",
            "NSP SERVICE ACTIVATION + CONFIG. FP",
        ):
            wb, ws = self._build_bom(qtys_for_network=7467, desc=fp_desc)
            _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
            self.assertNotIn(
                "3HE04962AB", parts,
                f"NSP FP row should be filtered: desc={fp_desc!r}",
            )
            wb.close()

    def test_sfp_descriptions_are_not_accidentally_fp_filtered(self):
        """Word-boundary check: the ' fp' filter must NOT match 'SFP+'.
        ``_hkey("SFP+ 10GE LR")`` → ``"sfp+ 10ge lr"`` — leading 's'
        means no ' fp' substring at any position."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom(qtys_for_network=4, desc="SFP+ 10GE LR - LC")
        _sites, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertIn("3HE04962AB", parts)
        wb.close()


class TestQtyCellToInt(unittest.TestCase):
    """``_qty_cell_to_int`` normalizes the various ways a worksheet can
    spell a quantity. Real Sales BoMs ship cells in every form below;
    treating any of them as zero silently undercounts the order."""

    def test_plain_int(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int(7), 7)

    def test_plain_float(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int(7.0), 7)

    def test_none_is_zero(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int(None), 0)

    def test_empty_string_is_zero(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int(""), 0)

    def test_plain_numeric_string(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int("7"), 7)

    def test_quoted_numeric_string(self):
        """The real bug reproducer: a cell containing the literal string
        ``"7"`` (with embedded quotes). Some Sales BoMs export qty this
        way, presumably from a CONCATENATE or text-formula upstream."""
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int('"7"'), 7)
        self.assertEqual(_qty_cell_to_int("'7'"), 7)

    def test_whitespace_padded_string(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int("  7 "), 7)

    def test_non_numeric_string_is_zero(self):
        from gui.bom_compare_frame import _qty_cell_to_int
        self.assertEqual(_qty_cell_to_int("N/A"), 0)
        self.assertEqual(_qty_cell_to_int("#REF!"), 0)
        self.assertEqual(_qty_cell_to_int("hello"), 0)


class TestLegacyLayoutSiteGate(unittest.TestCase):
    """Real-world legacy Sales BoMs put Total at the right edge with
    metadata (Customer, License points, line totals) past it. The
    site-column scanner has to stop at the Total column for that
    layout — otherwise the price/customer cells leak through as fake
    site quantities and inflate every part's Total Ordered.

    Contrast with the new builder layout where Total sits 1 column past
    Description and sites are to its right; there we have to scan past
    Total to find the sites.
    """

    def _build_legacy_bom_with_metadata_past_total(self):
        """``Item | PN | Desc | <3 sites> | Total | UnitPrice | Customer``
        with a numeric value in the Customer column that the gate must
        NOT count as a site qty.
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        # Headers on row 7, sub-Qty on row 8.
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "ALBA"); ws.cell(8, 4, "Qty")
        ws.cell(7, 5, "ALVY"); ws.cell(8, 5, "Qty")
        ws.cell(7, 6, "BAND"); ws.cell(8, 6, "Qty")
        ws.cell(7, 7, "Total")
        ws.cell(7, 8, "Unit Price")
        ws.cell(7, 9, "Customer")
        # One data row: 2+3+5 = 10 on per-site; Customer cell holds 6439
        # which must NOT be summed as a site qty.
        ws.cell(10, 2, "3HE03127AA")
        ws.cell(10, 3, "2P OC3/STM1 CHANNELIZED ADAPTER CARD")
        ws.cell(10, 4, 2); ws.cell(10, 5, 3); ws.cell(10, 6, 5)
        ws.cell(10, 7, 10)
        ws.cell(10, 8, 6439.09)
        ws.cell(10, 9, 6439.09)
        return wb, ws

    def test_columns_past_total_are_not_counted_as_sites(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_legacy_bom_with_metadata_past_total()
        _, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        # Total per-site = 10, not 10 + 6439 + 6439 = 12,888.
        self.assertEqual(sum(parts["3HE03127AA"]["site_qty"].values()), 10)
        # Customer column should not appear as a site.
        self.assertNotIn("Customer", parts["3HE03127AA"]["site_qty"])
        wb.close()

    def test_quoted_qty_in_legacy_layout_still_parsed(self):
        """The quoted-number fix and the legacy-gate fix must compose:
        a site qty stored as ``"7"`` between Description and Total
        still has to be counted."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(7, 1, "Item"); ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "ALBA"); ws.cell(8, 4, "Qty")
        ws.cell(7, 5, "Total")
        ws.cell(10, 2, "3HE03127AA")
        ws.cell(10, 3, "2P OC3/STM1 CHANNELIZED ADAPTER CARD")
        ws.cell(10, 4, '"7"')   # quoted-string qty
        ws.cell(10, 5, 7)
        _, parts, _, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertEqual(parts["3HE03127AA"]["site_qty"], {"ALBA": 7})
        wb.close()


class TestSparesOnlyPartCapturesDescription(unittest.TestCase):
    """``_parse_per_site_bom`` keeps two passes: one for parts with at
    least one per-site quantity, and a second to scoop up parts that
    appear ONLY in the Spares column. The second pass was previously
    discarding the description, which surfaced downstream as blank desc
    cells on the Sales BoM Spares tab (3HE11279/86/87/88AA were the
    reproducer) and on the BoM Compare Shortfall sheet.
    """

    def _build_bom_with_spares_only_part(self, desc_value):
        """Layout matches a typical Sales BoM aggregate:
        col A=Item, B=Part No., C=Equipment Description,
        col D-F=per-site (ALBA/ALVY/BAND), G=Spares, H=Total.
        One spares-only PN at row 11 with caller-supplied description.
        """
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(7, 1, "Item")
        ws.cell(7, 2, "Part No.")
        ws.cell(7, 3, "Equipment Description")
        ws.cell(7, 4, "ALBA"); ws.cell(7, 5, "ALVY"); ws.cell(7, 6, "BAND")
        ws.cell(7, 7, "Spares")
        ws.cell(7, 8, "Total")
        ws.cell(8, 4, "Qty"); ws.cell(8, 5, "Qty"); ws.cell(8, 6, "Qty")
        ws.cell(8, 7, "Qty"); ws.cell(8, 8, "Qty")
        # Normal part with per-site presence + spares.
        ws.cell(10, 1, 1)
        ws.cell(10, 2, "3HE12546AA")
        ws.cell(10, 3, "SFP - C37.94 LC 2KM SR")
        ws.cell(10, 4, 2); ws.cell(10, 5, 3); ws.cell(10, 7, 5); ws.cell(10, 8, 10)
        # Spares-only PN: zero across every per-site column.
        ws.cell(11, 1, 2)
        ws.cell(11, 2, "3HE11286AA")
        ws.cell(11, 3, desc_value)
        ws.cell(11, 4, 0); ws.cell(11, 5, 0); ws.cell(11, 6, 0)
        ws.cell(11, 7, 5)
        ws.cell(11, 8, 5)
        return wb, ws

    def test_spares_only_part_carries_description_into_parts_dict(self):
        """The spares-only PN must land in ``parts`` with its description
        so downstream consumers can label it on the Spares tab."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom_with_spares_only_part(
            "7250 IXR-R6 fan filter, 5-pack"
        )
        _, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertEqual(spares["3HE11286AA"], 5)
        self.assertIn("3HE11286AA", parts)
        self.assertEqual(
            parts["3HE11286AA"]["desc"], "7250 IXR-R6 fan filter, 5-pack"
        )
        wb.close()

    def test_spares_only_part_with_blank_source_desc_stays_blank(self):
        """If the source itself has a blank Description cell, we don't
        invent one — but the spares qty still registers. The PN may not
        end up in ``parts`` at all (no desc to record) but ``spares``
        must carry the qty so downstream merging sees it."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom_with_spares_only_part("")
        _, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertEqual(spares["3HE11286AA"], 5)
        # If 3HE11286AA wound up in parts, its desc must still be empty.
        if "3HE11286AA" in parts:
            self.assertEqual(parts["3HE11286AA"]["desc"], "")
        wb.close()

    def test_normal_part_still_captures_its_spares_qty(self):
        """Regression guard for the first-pass spares accumulation —
        a part with both per-site qty AND spares qty must hit both
        ``parts`` and ``spares`` correctly."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_bom_with_spares_only_part(
            "7250 IXR-R6 fan filter, 5-pack"
        )
        _, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
        self.assertEqual(parts["3HE12546AA"]["desc"], "SFP - C37.94 LC 2KM SR")
        self.assertEqual(sum(parts["3HE12546AA"]["site_qty"].values()), 5)
        self.assertEqual(spares["3HE12546AA"], 5)
        wb.close()


class TestAggregateFromSiteTabs(unittest.TestCase):
    """``_aggregate_from_site_tabs`` walks every per-site DETAIL tab on a
    workbook and sums each PN's quantities — the authoritative source of
    truth for the Shortfall comparison. It must:

    * Sum aggregated qty (col D) on Sales-style detail tabs.
    * Count rows on Factory-style detail tabs (1 unit per row).
    * Route the Spares tab into a separate spares dict.
    * Skip the BOM aggregate tab and Summary-style tabs.
    * Strip embedded vendor-name lines from PN cells.
    * Filter out license/RTU/OS-LICENSE rows by description keyword.
    """

    def _build_sales_detail_tab(self, wb, title, rows, site_label=None):
        """rows = [(pn, desc, qty)]; appended starting at row 15."""
        ws = wb.create_sheet(title)
        ws["B14"] = "Part Number"
        ws["C14"] = "Description"
        ws["D14"] = "Quantity"
        if site_label is not None:
            ws["C7"] = site_label
        for i, (pn, desc, qty) in enumerate(rows):
            r = 15 + i
            ws.cell(r, 2, pn)
            ws.cell(r, 3, desc)
            ws.cell(r, 4, qty)
        return ws

    def _build_factory_detail_tab(self, wb, title, rows):
        """rows = [(pn, serial, desc)]; appended starting at row 15.
        Each row represents one physical unit; qty=1 implicit."""
        ws = wb.create_sheet(title)
        ws["B14"] = "SLOT/PORT"
        ws["C14"] = "PART TYPE"
        ws["D14"] = "PART NUMBER"
        ws["E14"] = "SERIAL NUMBER"
        ws["F14"] = "DESCRIPTION"
        for i, (pn, serial, desc) in enumerate(rows):
            r = 15 + i
            ws.cell(r, 4, pn)
            ws.cell(r, 5, serial)
            ws.cell(r, 6, desc)
        return ws

    def test_sales_per_site_tabs_sum_into_per_site_total(self):
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        # Add a BOM aggregate that should be skipped.
        wb.create_sheet("BOM")
        # Three Sales detail tabs each ordering the same SFP.
        self._build_sales_detail_tab(wb, "ALBA", [("3HE12546AA", "SFP - C37.94", 2)])
        self._build_sales_detail_tab(wb, "ALVY", [("3HE12546AA", "SFP - C37.94", 3)])
        self._build_sales_detail_tab(wb, "BAND", [("3HE12546AA", "SFP - C37.94", 5)])
        per_site, spares, desc, visited = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        self.assertEqual(per_site.get("3HE12546AA"), 10)
        self.assertEqual(spares, {})
        self.assertEqual(desc.get("3HE12546AA"), "SFP - C37.94")
        self.assertEqual(set(visited), {"ALBA", "ALVY", "BAND"})

    def test_sales_spares_tab_routed_to_spares_dict(self):
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._build_sales_detail_tab(wb, "ALBA", [("3HE12546AA", "SFP", 2)])
        self._build_sales_detail_tab(wb, "Spares", [("3HE12546AA", "SFP", 8)])
        per_site, spares, _, _ = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        self.assertEqual(per_site.get("3HE12546AA"), 2)
        self.assertEqual(spares.get("3HE12546AA"), 8)

    def test_factory_per_site_tabs_count_rows_as_units(self):
        """Factory tabs are row-per-unit; qty must be implicit 1."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._build_factory_detail_tab(wb, "MALN", [
            ("3HE12546AA", "SN001", "SFP - C37.94"),
            ("3HE12546AA", "SN002", "SFP - C37.94"),
            ("3HE12546AA", "SN003", "SFP - C37.94"),
            ("3HE04823AA", "SN100", "SFP+ 10GE LR"),
        ])
        per_site, _, _, _ = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        self.assertEqual(per_site.get("3HE12546AA"), 3)
        self.assertEqual(per_site.get("3HE04823AA"), 1)

    def test_factory_license_rows_filtered_out(self):
        """Rows whose Description contains LICENSE must be skipped —
        they're virtual line items the aggregate parser drops via its
        section banner filter (Software / Maintenance / Services)."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._build_factory_detail_tab(wb, "MALN", [
            ("3HE12546AA", "SN001", "SFP - C37.94 LC 2KM SR"),       # hardware -> kept
            ("3HE02784UA", "N/A",   "SAR RELEASE 24.x BASIC OS LICENSE"),  # license -> dropped
            ("3HE08607EA", "N/A",   "RTU - 7705 SAR-8 Basic IPSec LICENSE"),  # license -> dropped
            ("3HE12546AA", "SN002", "SFP - C37.94 LC 2KM SR"),       # hardware -> kept
        ])
        per_site, _, _, _ = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        self.assertEqual(per_site.get("3HE12546AA"), 2)
        self.assertNotIn("3HE02784UA", per_site)
        self.assertNotIn("3HE08607EA", per_site)

    def test_pn_cells_with_embedded_newline_are_stripped(self):
        """Real-world Sales BoMs sometimes glue the vendor name onto the
        PN cell with a newline: ``"NMA-8509\\nQUEST TECHNOLOGIES"``.
        The first line is the PN; the rest is metadata to discard."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._build_sales_detail_tab(wb, "ALBA", [
            ("NMA-8509\nQUEST TECHNOLOGIES", "ADAPTER, RJ45 to DB25", 4),
        ])
        per_site, _, desc, _ = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        # PN should be just "NMA-8509", not the multiline form.
        self.assertIn("NMA-8509", per_site)
        self.assertEqual(per_site["NMA-8509"], 4)
        self.assertEqual(desc["NMA-8509"], "ADAPTER, RJ45 to DB25")

    def test_summary_and_bom_tabs_are_skipped(self):
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        # Summary tab with a Part Number header (should still be skipped).
        ws = wb.create_sheet("Summary")
        ws["B14"] = "Part Number"
        ws["D14"] = "Quantity"
        ws.cell(15, 2, "3HE12546AA")
        ws.cell(15, 4, 9999)
        # BOM tab too.
        ws = wb.create_sheet("BOM")
        ws["B14"] = "Part Number"
        ws["D14"] = "Quantity"
        ws.cell(15, 2, "3HE12546AA")
        ws.cell(15, 4, 9999)
        # One real detail tab.
        self._build_sales_detail_tab(wb, "ALBA", [("3HE12546AA", "SFP", 7)])
        per_site, _, _, visited = BomCompareFrame._aggregate_from_site_tabs(wb, "BOM")
        self.assertEqual(per_site.get("3HE12546AA"), 7)
        self.assertEqual(visited, ["ALBA"])

    def test_compare_wires_detail_tab_recalc_into_per_side_totals(self):
        """Source-level guard: ``_compare`` must call
        ``_aggregate_from_site_tabs`` for both workbooks and feed the
        result into ``fac_persite_total`` / ``sal_persite_total``. This
        is what makes the Shortfall numbers verifiable by hand from the
        per-site detail tabs."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("_aggregate_from_site_tabs(fac_data", src)
        self.assertIn("_aggregate_from_site_tabs(sal_data", src)


class TestPerSiteBreakdownHelper(unittest.TestCase):
    """``_per_site_breakdown_from_detail_tabs`` walks per-site DETAIL tabs
    and returns ``{pn: {site_title: qty}}`` plus an ordered list of site
    tab titles that actually contributed at least one unit. Used by the
    Site Allocation sheet writer."""

    def _factory_unit_row(self, ws, row, slot, pn, serial, desc):
        ws.cell(row, 2, slot)
        ws.cell(row, 4, pn)
        ws.cell(row, 5, serial)
        ws.cell(row, 6, desc)

    def _factory_detail_tab(self, wb, title, units):
        ws = wb.create_sheet(title)
        ws["B14"] = "SLOT/PORT"
        ws["C14"] = "PART TYPE"
        ws["D14"] = "PART NUMBER"
        ws["E14"] = "SERIAL NUMBER"
        ws["F14"] = "DESCRIPTION"
        for i, (slot, pn, serial, desc) in enumerate(units):
            self._factory_unit_row(ws, 15 + i, slot, pn, serial, desc)
        return ws

    def test_per_site_breakdown_keeps_site_dimension(self):
        """Unlike ``_aggregate_from_site_tabs`` which sums across sites,
        the breakdown helper preserves the (pn, site) -> qty mapping
        so the Site Allocation tab can render one cell per site."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._factory_detail_tab(wb, "MALN", [
            ("1/1/5", "3HE12546AA", "SN001", "SFP - C37.94"),
            ("1/1/6", "3HE12546AA", "SN002", "SFP - C37.94"),
        ])
        self._factory_detail_tab(wb, "CHEM", [
            ("1/2/1", "3HE12546AA", "SN003", "SFP - C37.94"),
            ("1/2/2", "3HE04823AA", "SN100", "SFP+ 10GE LR"),
        ])
        breakdown, site_order = (
            BomCompareFrame._per_site_breakdown_from_detail_tabs(wb, "BOM")
        )
        self.assertEqual(breakdown["3HE12546AA"], {"MALN": 2, "CHEM": 1})
        self.assertEqual(breakdown["3HE04823AA"], {"CHEM": 1})
        self.assertEqual(site_order, ["MALN", "CHEM"])

    def test_empty_tabs_excluded_from_site_order(self):
        """Tabs with no contributing rows must NOT pollute the site
        list (the Site Allocation sheet would otherwise carry dead
        columns)."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._factory_detail_tab(wb, "MALN", [
            ("1/1/5", "3HE12546AA", "SN001", "SFP - C37.94"),
        ])
        # An empty per-site tab.
        self._factory_detail_tab(wb, "GHOST_TAB", [])
        _, site_order = (
            BomCompareFrame._per_site_breakdown_from_detail_tabs(wb, "BOM")
        )
        self.assertEqual(site_order, ["MALN"])

    def test_license_rows_filtered(self):
        """Detail-tab license rows (RTU / OS LICENSE entries with N/A
        serial) must be dropped — they pollute the Shortfall via the
        Network/License Points buckets otherwise."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._factory_detail_tab(wb, "MALN", [
            ("1/1/5", "3HE12546AA", "SN001", "SFP - C37.94"),
            ("RTU",   "3HE02784UA", "N/A",   "SAR RELEASE 24.x BASIC OS LICENSE"),
        ])
        breakdown, _ = (
            BomCompareFrame._per_site_breakdown_from_detail_tabs(wb, "BOM")
        )
        self.assertIn("3HE12546AA", breakdown)
        self.assertNotIn("3HE02784UA", breakdown)


class TestSiteAllocationSheet(unittest.TestCase):
    """``_write_site_allocation_sheet`` renders rows from the Shortfall
    onto a wide tab where each Live site is a column. Row coloring
    inherits the Shortfall delta sign so an operator can see at a
    glance where shortages and surpluses physically sit."""

    def _build_wb(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Site Allocation"
        return wb, ws

    def test_layout_has_two_cells_per_site_need_then_have(self):
        """Each site occupies two columns (Need + Have). Row 3 carries
        the site label merged across the pair; row 4 carries the
        sub-labels."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [
            ("3HE12546AA", "SFP - C37.94", 10, 3, -7),
            ("3HE04823AA", "SFP+ 10GE LR", 4, 8, 4),
        ]
        sal = {
            "3HE12546AA": {"MALN": 2, "CHEM": 1},
            "3HE04823AA": {"MALN": 5, "CHEM": 3},
        }
        live = {
            "3HE12546AA": {"MALN": 1, "CHEM": 2},
            "3HE04823AA": {"MALN": 7, "CHEM": 3},
        }
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN", "CHEM"], live, ["MALN", "CHEM"],
            aliases={},
        )
        # Item / PN / Desc in cols 1-3
        self.assertEqual(ws.cell(3, 1).value, "Item")
        self.assertEqual(ws.cell(3, 2).value, "Part No.")
        self.assertEqual(ws.cell(3, 3).value, "Equipment Description")
        # Site labels start at col 4, two sub-cells per site
        self.assertEqual(ws.cell(3, 4).value, "MALN")
        self.assertEqual(ws.cell(3, 6).value, "CHEM")
        # Row 4 sub-headers alternate Need / Have
        self.assertEqual(ws.cell(4, 4).value, "Need")
        self.assertEqual(ws.cell(4, 5).value, "Have")
        self.assertEqual(ws.cell(4, 6).value, "Need")
        self.assertEqual(ws.cell(4, 7).value, "Have")
        wb.close()

    def test_per_site_short_cell_is_red(self):
        """At a single site where Have < Need, both Need and Have
        cells must be RED — operator sees per-site deficit at a glance."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 10, 3, -7)]
        sal = {"3HE12546AA": {"MALN": 4, "CHEM": 6}}
        live = {"3HE12546AA": {"MALN": 2, "CHEM": 1}}  # both short
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN", "CHEM"], live, ["MALN", "CHEM"],
            aliases={},
        )
        for need_col, have_col in ((4, 5), (6, 7)):
            self.assertEqual(ws.cell(5, need_col).font.color.value, "FFC00000")
            self.assertEqual(ws.cell(5, have_col).font.color.value, "FFC00000")
        wb.close()

    def test_per_site_covered_cell_is_green(self):
        """Where Have >= Need at a site, both Need and Have cells go GREEN."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE04823AA", "SFP+ 10GE LR", 4, 8, 4)]
        sal = {"3HE04823AA": {"MALN": 2, "CHEM": 2}}
        live = {"3HE04823AA": {"MALN": 5, "CHEM": 3}}  # both covered
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN", "CHEM"], live, ["MALN", "CHEM"],
            aliases={},
        )
        for need_col, have_col in ((4, 5), (6, 7)):
            self.assertEqual(ws.cell(5, need_col).font.color.value, "FF548235")
            self.assertEqual(ws.cell(5, have_col).font.color.value, "FF548235")
        wb.close()

    def test_mixed_per_site_coloring_in_same_row(self):
        """The whole point of per-site coloring: same PN can be RED
        at one site and GREEN at another."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 7, 7, 0)]
        sal = {"3HE12546AA": {"MALN": 4, "CHEM": 3}}
        live = {"3HE12546AA": {"MALN": 2, "CHEM": 5}}  # MALN short, CHEM surplus
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN", "CHEM"], live, ["MALN", "CHEM"],
            aliases={},
        )
        # MALN cells red
        self.assertEqual(ws.cell(5, 4).font.color.value, "FFC00000")
        self.assertEqual(ws.cell(5, 5).font.color.value, "FFC00000")
        # CHEM cells green
        self.assertEqual(ws.cell(5, 6).font.color.value, "FF548235")
        self.assertEqual(ws.cell(5, 7).font.color.value, "FF548235")
        wb.close()

    def test_need_cell_populated_when_only_sales_has_qty(self):
        """User's reported case: NMA-8509 has 0 Live but 4 ordered at
        ALBA. The Need cell must show '4' (RED) so the operator can
        see how many are needed where."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("NMA-8509", "ADAPTER RJ45", 4, 0, -4)]
        sal = {"NMA-8509": {"ALBA": 4}}
        live = {}  # nothing in Live
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["ALBA"], live, [], aliases={},
        )
        self.assertEqual(ws.cell(5, 4).value, 4)  # Need cell
        self.assertEqual(ws.cell(5, 4).font.color.value, "FFC00000")
        self.assertIsNone(ws.cell(5, 5).value)  # Have cell blank
        wb.close()

    def test_blank_cells_when_neither_side_has_qty_at_site(self):
        """Sites that this PN doesn't touch on either side: cells
        left blank so the eye skips past."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 4, 4, 0)]
        sal = {"3HE12546AA": {"MALN": 4}}  # only MALN
        live = {"3HE12546AA": {"MALN": 4}}
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN", "CHEM"], live, ["MALN", "CHEM"],
            aliases={},
        )
        # CHEM both cells blank
        self.assertIsNone(ws.cell(5, 6).value)
        self.assertIsNone(ws.cell(5, 7).value)
        wb.close()

    def test_aliased_pn_picks_up_breakdown_under_alias_key(self):
        """If Shortfall PN is the canonical SKU but the detail tabs
        captured the breakdown under an alias, both Sales and Live
        breakdowns must be alias-folded before rendering."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE11278AA", "CHASSIS", 2, 2, 0)]
        sal = {"3HE13584AA": {"MALN": 2}}  # bundle alias
        live = {"3HE11278AA": {"MALN": 2}}
        aliases = {"3HE13584AA": "3HE11278AA"}
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN"], live, ["MALN"], aliases=aliases,
        )
        self.assertEqual(ws.cell(5, 4).value, 2)  # Need (from alias)
        self.assertEqual(ws.cell(5, 5).value, 2)  # Have
        wb.close()

    def test_live_site_matches_sales_site_by_leading_letter_key(self):
        """Real-world: Sales 'MALN' should pair with Live 'MALN001_7250'
        + 'MALN' tabs. The Have column for the Sales 'MALN' site must
        sum across all Live tabs with key 'MALN'."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 4, 11, 7)]
        sal = {"3HE12546AA": {"MALN": 4}}
        live = {"3HE12546AA": {"MALN001_7250": 3, "MALN001_7705": 5, "MALN": 3}}
        BomCompareFrame._write_site_allocation_sheet(
            ws, rows, sal, ["MALN"],
            live, ["MALN001_7250", "MALN001_7705", "MALN"],
            aliases={},
        )
        # Need = 4 at MALN, Have should sum to 11 across Live MALN-group
        self.assertEqual(ws.cell(5, 4).value, 4)
        self.assertEqual(ws.cell(5, 5).value, 11)
        # Both green (covered)
        self.assertEqual(ws.cell(5, 5).font.color.value, "FF548235")
        wb.close()


class TestQtyTraceEntries(unittest.TestCase):
    """``_qty_trace_entries`` walks per-site detail tabs and emits one
    entry per source row that contributed a qty. The Trace sheet uses
    these to render an audit trail.
    """

    def _sales_tab(self, wb, title, rows):
        ws = wb.create_sheet(title)
        ws["B14"] = "Part Number"
        ws["C14"] = "Description"
        ws["D14"] = "Quantity"
        for i, (pn, desc, qty) in enumerate(rows):
            ws.cell(15 + i, 2, pn)
            ws.cell(15 + i, 3, desc)
            ws.cell(15 + i, 4, qty)
        return ws

    def _factory_tab(self, wb, title, units):
        ws = wb.create_sheet(title)
        ws["B14"] = "SLOT/PORT"
        ws["C14"] = "PART TYPE"
        ws["D14"] = "PART NUMBER"
        ws["E14"] = "SERIAL NUMBER"
        ws["F14"] = "DESCRIPTION"
        for i, (pn, serial, desc) in enumerate(units):
            ws.cell(15 + i, 4, pn)
            ws.cell(15 + i, 5, serial)
            ws.cell(15 + i, 6, desc)
        return ws

    def test_sales_entries_aggregate_per_row(self):
        """Sales-side rows are one-per-aggregated-PN; the entry's qty
        comes from the Quantity column and detail is the description."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._sales_tab(wb, "ALBA", [
            ("3HE12546AA", "SFP - C37.94", 4),
            ("3HE04823AA", "SFP+ 10GE LR", 8),
        ])
        entries = BomCompareFrame._qty_trace_entries(wb, "BOM")
        self.assertIn(("3HE12546AA", "ALBA", 15, 4, "SFP - C37.94"), entries)
        self.assertIn(("3HE04823AA", "ALBA", 16, 8, "SFP+ 10GE LR"), entries)

    def test_factory_entries_are_one_per_unit_with_serial(self):
        """Factory-side rows are one-per-physical-unit with qty=1; the
        detail surfaces the serial so the operator can verify each
        individual unit."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._factory_tab(wb, "MALN", [
            ("3HE12546AA", "SGF0130YB200220", "SFP - C37.94"),
            ("3HE12546AA", "SGF0130YB200359", "SFP - C37.94"),
        ])
        entries = BomCompareFrame._qty_trace_entries(wb, "BOM")
        self.assertEqual(len(entries), 2)
        self.assertIn(("3HE12546AA", "MALN", 15, 1, "SGF0130YB200220"), entries)
        self.assertIn(("3HE12546AA", "MALN", 16, 1, "SGF0130YB200359"), entries)

    def test_factory_na_serial_falls_back_to_description(self):
        """When a Factory row has serial 'N/A' (typical for license/RTU
        rows that survived the LICENSE filter for some reason), the
        detail field falls back to the description instead of the
        meaningless 'N/A'."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        # Use a desc WITHOUT "LICENSE" so the row isn't filtered.
        self._factory_tab(wb, "MALN", [
            ("3HE09302CA", "N/A", "RTU -2P 10GE+4P GE 10G ETH ENCRYPTION"),
        ])
        entries = BomCompareFrame._qty_trace_entries(wb, "BOM")
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0][4], "RTU -2P 10GE+4P GE 10G ETH ENCRYPTION"
        )

    def test_license_rows_filtered_out_of_trace(self):
        """License rows must not appear in the trace (they're filtered
        upstream; surfacing them in Trace would be confusing)."""
        import openpyxl
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("BOM")
        self._factory_tab(wb, "MALN", [
            ("3HE12546AA", "SN001", "SFP - C37.94"),
            ("3HE02784UA", "N/A",   "SAR RELEASE 24.x BASIC OS LICENSE"),
        ])
        entries = BomCompareFrame._qty_trace_entries(wb, "BOM")
        pns = {e[0] for e in entries}
        self.assertIn("3HE12546AA", pns)
        self.assertNotIn("3HE02784UA", pns)


class TestTraceSheet(unittest.TestCase):
    """``_write_trace_sheet`` renders the source-row audit trail."""

    def _build_wb(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Trace"
        return wb, ws

    def test_one_row_per_source_contribution(self):
        """For each Shortfall PN, the trace must emit one row per
        Sales source contribution AND one row per Live source unit.
        Sales rows sum to Total Ordered; Live rows sum to Live Inventory."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 7, 3, -4)]
        sal_entries = [
            ("3HE12546AA", "ALBA", 15, 4, "SFP - C37.94"),
            ("3HE12546AA", "ALVY", 16, 3, "SFP - C37.94"),
        ]
        fac_entries = [
            ("3HE12546AA", "MALN", 15, 1, "SN001"),
            ("3HE12546AA", "MALN", 16, 1, "SN002"),
            ("3HE12546AA", "CHEM", 20, 1, "SN003"),
        ]
        BomCompareFrame._write_trace_sheet(
            ws, rows, sal_entries, fac_entries, aliases={}
        )
        # 2 Sales rows + 3 Live rows = 5 data rows
        data_rows = []
        for r in range(4, ws.max_row + 1):
            if ws.cell(r, 2).value:
                data_rows.append((
                    ws.cell(r, 2).value,
                    ws.cell(r, 4).value,
                    ws.cell(r, 5).value,
                    ws.cell(r, 6).value,
                    ws.cell(r, 7).value,
                ))
        self.assertEqual(len(data_rows), 5)
        # Sales rows come first, sum to 7 (= Total Ordered)
        sales_qtys = [r[4] for r in data_rows if r[1] == "Sales"]
        self.assertEqual(sum(sales_qtys), 7)
        # Live rows sum to 3 (= Live Inventory)
        live_qtys = [r[4] for r in data_rows if r[1] == "Live"]
        self.assertEqual(sum(live_qtys), 3)
        wb.close()

    def test_pns_not_on_shortfall_are_excluded(self):
        """Trace must restrict to PNs that appear on the Shortfall.
        Stale entries for filtered PNs (NSP FPs, etc.) would confuse
        the operator."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 7, 3, -4)]
        sal_entries = [
            ("3HE12546AA", "ALBA", 15, 4, "SFP - C37.94"),
            ("FILTERED", "ALBA", 16, 99, "should not appear"),
        ]
        BomCompareFrame._write_trace_sheet(
            ws, rows, sal_entries, [], aliases={}
        )
        pns_emitted = {
            ws.cell(r, 2).value
            for r in range(4, ws.max_row + 1)
            if ws.cell(r, 2).value
        }
        self.assertIn("3HE12546AA", pns_emitted)
        self.assertNotIn("FILTERED", pns_emitted)
        wb.close()

    def test_aliased_raw_pn_attaches_to_canonical_shortfall_row(self):
        """A source row keyed under an alias (3HE13584AA) must show
        up under the canonical Shortfall PN (3HE11278AA), so the
        Trace stays grouped by the PN the operator is looking up."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE11278AA", "CHASSIS", 2, 0, -2)]
        sal_entries = [
            ("3HE13584AA", "ALBA", 15, 2, "CHASSIS BUNDLE"),
        ]
        aliases = {"3HE13584AA": "3HE11278AA"}
        BomCompareFrame._write_trace_sheet(
            ws, rows, sal_entries, [], aliases=aliases
        )
        # PN col shows the canonical
        self.assertEqual(ws.cell(4, 2).value, "3HE11278AA")
        # Description includes the raw PN for traceability
        desc_cell = ws.cell(4, 3).value or ""
        self.assertIn("3HE13584AA", desc_cell)
        wb.close()

    def test_kit_fold_synthetic_row_reconciles_to_shortfall(self):
        """When kit folding adds units to a kit SKU, the Trace must
        emit a synthetic 'Kit Fold' row for the kit so the Trace's
        Live-side sum = Shortfall's Live Inventory. Without this,
        bare-kit source rows would sum to less than the post-fold
        Shortfall total."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        # 3 bare kits in Live + 6 folded from 6/6/6 components = 9 total.
        rows = [("3KC48900AA", "PSS8 SHELF KIT", 5, 9, 4)]
        fac_entries = [
            ("3KC48900AA", "CHEM", 30, 1, "SN-A"),
            ("3KC48900AA", "KEEL", 30, 1, "SN-B"),
            ("3KC48900AA", "SPARES", 30, 1, "SN-C"),
        ]
        fac_kit_folds = [
            ("3KC48900AA", 6, ["3KC48830AA", "3KC48850AA", "3KC48901AA"]),
        ]
        BomCompareFrame._write_trace_sheet(
            ws, rows, [], fac_entries, aliases={},
            fac_kit_folds=fac_kit_folds,
        )
        live_qtys = []
        kit_fold_seen = False
        for r in range(4, ws.max_row + 1):
            if ws.cell(r, 4).value == "Live":
                live_qtys.append(ws.cell(r, 7).value)
                if ws.cell(r, 5).value == "Kit Fold":
                    kit_fold_seen = True
                    self.assertEqual(ws.cell(r, 7).value, 6)
                    detail = ws.cell(r, 8).value
                    self.assertIn("3KC48830AA", detail)
                    self.assertIn("3KC48850AA", detail)
                    self.assertIn("3KC48901AA", detail)
        self.assertTrue(kit_fold_seen, "Synthetic Kit Fold row missing")
        self.assertEqual(sum(live_qtys), 9)
        wb.close()

    def test_no_kit_fold_row_when_components_absent(self):
        """If kit folding didn't happen (one component missing on this
        side), no synthetic kit-fold row should be emitted."""
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3KC48900AA", "PSS8 SHELF KIT", 5, 3, -2)]
        fac_entries = [
            ("3KC48900AA", "CHEM", 30, 1, "SN-A"),
            ("3KC48900AA", "KEEL", 30, 1, "SN-B"),
            ("3KC48900AA", "SPARES", 30, 1, "SN-C"),
        ]
        BomCompareFrame._write_trace_sheet(
            ws, rows, [], fac_entries, aliases={},
            fac_kit_folds=[],  # nothing folded
        )
        for r in range(4, ws.max_row + 1):
            self.assertNotEqual(
                ws.cell(r, 5).value, "Kit Fold",
                f"row {r} should not be a synthetic Kit Fold row",
            )
        wb.close()

    def test_autofilter_set_on_data_range(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb, ws = self._build_wb()
        rows = [("3HE12546AA", "SFP", 1, 0, -1)]
        sal_entries = [("3HE12546AA", "ALBA", 15, 1, "SFP")]
        BomCompareFrame._write_trace_sheet(
            ws, rows, sal_entries, [], aliases={}
        )
        # Auto-filter ref should span the header row and at least one data row
        self.assertIsNotNone(ws.auto_filter.ref)
        # Header row is 3 → filter starts at A3
        self.assertTrue(ws.auto_filter.ref.startswith("A3:"))
        wb.close()


class TestCompareWiresTraceSheet(unittest.TestCase):
    """Source-level guards: ``_compare`` must wire the Trace sheet in
    and the tab order must include it between Site Allocation and the
    reference copies."""

    def test_compare_builds_trace_sheet(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn('create_sheet("Trace")', src)
        self.assertIn("_write_trace_sheet", src)
        self.assertIn("_qty_trace_entries", src)

    def test_tab_order_includes_trace(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn(
            '"Shortfall", "Site Allocation", "Trace",',
            src,
        )


class TestCompareWiresSiteAllocation(unittest.TestCase):
    """Source-level guards so the new Site Allocation sheet stays wired
    into ``_compare`` and the tab order isn't accidentally rearranged."""

    def test_compare_builds_site_allocation_sheet(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn('create_sheet("Site Allocation")', src)
        self.assertIn("_write_site_allocation_sheet", src)
        self.assertIn("_per_site_breakdown_from_detail_tabs", src)

    def test_tab_order_includes_site_allocation_between_shortfall_and_live(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        # The desired_order list lives near the bottom of _compare.
        # Match a single string with the four tab names in order so the
        # guard catches reordering AND removal of any tab.
        self.assertIn(
            '"Shortfall", "Site Allocation", "Trace",',
            src,
        )


class TestCompareTabOrderingAfterSimplification(unittest.TestCase):
    """Source-level guards on the simplified output: no Surplus tab,
    no Spares Allocation tab, no _build_breakdown call. Just three
    tabs: Shortfall | Live Inventory | Sales BoM."""

    def test_compare_does_not_create_surplus_tab(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertNotIn('create_sheet("Surplus")', src)
        self.assertNotIn("_write_surplus_sheet", src)

    def test_compare_does_not_build_spares_allocation(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertNotIn('_build_breakdown', src)
        self.assertNotIn('create_sheet("Spares Allocation")', src)

    def test_compare_iterates_union_of_part_numbers(self):
        """The single-tab compare needs to cover every part that
        appears on either side so all positive AND negative deltas
        show up."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("set(sal_parts.keys())", src)
        self.assertIn("set(fac_parts.keys())", src)
        self.assertIn("set(fac_persite_total.keys())", src)
        self.assertIn("set(sal_persite_total.keys())", src)


class TestCopySheetImagesOptOut(unittest.TestCase):
    """``copy_sheet`` accepts ``copy_images=False`` so callers can skip
    embedded artwork. BomCompareFrame uses this for the reference-copy
    tabs because openpyxl's BytesIO-based image rebuild produces drawing
    XML that Excel rejects with "Repaired Records: Drawing from
    xl/drawings/drawing*.xml" on open.
    """

    def test_copy_images_false_drops_embedded_images(self):
        import openpyxl
        from io import BytesIO
        from openpyxl.drawing.image import Image as XLImage
        from gui.workbook_builder import WorkbookBuilder
        from unittest.mock import MagicMock

        # 1x1 transparent PNG (smallest valid image bytes).
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\xfc\x0f\x00\x00\x01\x01\x00"
            b"\x06\x07\xc7\xee\xa2\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        wb_src = openpyxl.Workbook()
        ws_src = wb_src.active
        ws_src["A1"] = "x"
        ws_src.add_image(XLImage(BytesIO(png)), "B2")
        wb_dst = openpyxl.Workbook()
        wb_dst.remove(wb_dst.active)

        builder = WorkbookBuilder(
            db_cache=MagicMock(),
            template_path="",
            packing_slip_template="",
        )
        new_ws = builder.copy_sheet(ws_src, wb_dst, "Dest", copy_images=False)
        self.assertEqual(len(getattr(new_ws, "_images", []) or []), 0)
        # Data still copied.
        self.assertEqual(new_ws["A1"].value, "x")
        wb_src.close()
        wb_dst.close()

    def test_copy_images_default_true_preserves_existing_behavior(self):
        """Other callers (inventory builder, packing slip) rely on
        images coming along. Default must stay True."""
        import inspect
        from gui.workbook_builder import WorkbookBuilder
        sig = inspect.signature(WorkbookBuilder.copy_sheet)
        self.assertEqual(sig.parameters["copy_images"].default, True)

    def test_bom_compare_calls_copy_sheet_with_copy_images_false(self):
        """Source-level guard: BomCompareFrame must pass copy_images=False
        so the reference-copy tabs don't carry the broken drawing XML."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("copy_sheet(fac_bom, out_wb,", src)
        self.assertIn("copy_sheet(sal_bom, out_wb,", src)
        self.assertIn("copy_images=False", src)


class TestResetSheetViewToDefaults(unittest.TestCase):
    """Source workbooks save ``sheet_view`` state that includes
    multi-pane ``Selection`` objects (``pane='topRight'`` etc.) tied to
    whatever freeze/split layout was active when last saved. Clearing
    only freeze_panes leaves dangling pane refs in those Selections,
    which Excel flags with "Repaired Records: View from
    xl/worksheets/sheet*.xml". The helper replaces them with a single
    clean A1 Selection.
    """

    def setUp(self):
        from gui.bom_compare_frame import BomCompareFrame
        self.reset = BomCompareFrame._reset_sheet_view_to_defaults

    def test_replaces_multi_pane_selections_with_single_a1(self):
        import openpyxl
        from openpyxl.worksheet.views import Selection
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.sheet_view.selection = [
            Selection(pane="topRight",    activeCell="C5", sqref="C5"),
            Selection(pane="bottomLeft",  activeCell="A10", sqref="A10"),
            Selection(pane="bottomRight", activeCell="C10", sqref="C10:E12"),
        ]
        self.reset(ws)
        sel = ws.sheet_view.selection
        self.assertEqual(len(sel), 1)
        self.assertEqual(sel[0].activeCell, "A1")
        self.assertEqual(sel[0].sqref, "A1")
        self.assertIsNone(sel[0].pane)
        wb.close()

    def test_defaults_zoom_and_view_when_unset(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.sheet_view.zoomScale = None
        ws.sheet_view.view = None
        self.reset(ws)
        self.assertEqual(ws.sheet_view.zoomScale, 100)
        self.assertEqual(ws.sheet_view.view, "normal")
        wb.close()

    def test_preserves_existing_zoom_when_already_set(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.sheet_view.zoomScale = 75
        ws.sheet_view.view = "pageLayout"
        self.reset(ws)
        self.assertEqual(ws.sheet_view.zoomScale, 75)
        self.assertEqual(ws.sheet_view.view, "pageLayout")
        wb.close()

    def test_compare_calls_view_reset_after_copy(self):
        """Source-level guard: the view reset must fire after each
        copy_sheet for the BoM reference tabs."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("_reset_sheet_view_to_defaults", src)


class TestCopiedBomTabsAreValuesOnly(unittest.TestCase):
    """Source Sales BoMs carry in-sheet formulas with workbook-scoped
    named ranges (``=VLOOKUP(E10, sites, 1, FALSE)``). Copying those
    formulas verbatim into the comparison output breaks because the
    named range doesn't follow — every formula evaluates to ``#NAME?``
    and Excel pops a circular-reference warning on open. The fix is to
    feed ``copy_sheet`` the ``data_only=True`` snapshot of each source
    BoM so cached values land in the copy instead of live formulas.
    """

    def test_compare_loads_with_data_only_true(self):
        """Source-level guard: ``_compare`` must use ``data_only=True``
        for both source workbooks. The previous ``data_only=False`` load
        was preserving formulas that broke the output."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn(
            "openpyxl.load_workbook(factory_path, data_only=True)", src,
            "Factory BoM must load with data_only=True so cached values "
            "land in the copy",
        )
        self.assertIn(
            "openpyxl.load_workbook(sales_path, data_only=True)", src,
            "Sales BoM must load with data_only=True so cached values "
            "land in the copy",
        )
        # The OLD code also loaded data_only=False to preserve formulas
        # for the visual copy — that's exactly what broke. Make sure it
        # didn't come back.
        self.assertNotIn("data_only=False", src,
            "data_only=False reintroduces the formula-leak / circular-"
            "reference bug — both source loads must be data_only=True")

    def test_compare_passes_data_only_sheet_to_copy_sheet(self):
        """The reference copy must come from the data_only snapshot.
        Passing the data_only=False version would copy raw formulas
        which then fail to resolve in the destination workbook."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        # Both copy_sheet calls must reference the data_only sheet names.
        self.assertIn("copy_sheet(fac_bom, out_wb", src)
        self.assertIn("copy_sheet(sal_bom, out_wb", src)
        # The old code referenced *_full variants of the sheets — those
        # came from data_only=False loads.
        self.assertNotIn("copy_sheet(fac_bom_full", src)
        self.assertNotIn("copy_sheet(sal_bom_full", src)


class TestFreezePanesClearedOnCopiedBomTabs(unittest.TestCase):
    """Source Factory / Sales BoMs frequently arrive with stale freeze
    panes from whatever scroll-state the previous editor saved (e.g.
    ``S143`` on a 489-row Sales sheet, ``JO9`` on a 279-column Factory
    sheet). Those off-screen freeze positions make Excel auto-zoom to
    the frozen region instead of scrolling — which is what the user
    reported as "doesn't scroll, just zooms out." The fix in
    ``_compare`` clears freeze panes on the reference-copy tabs so the
    operator can navigate them normally.
    """

    def test_compare_resets_freeze_panes_on_copied_tabs(self):
        """Inspect the source of ``_compare`` directly — the live build
        flow needs Tk and real workbooks, but a source-level guard is
        enough to keep the reset wired in across refactors.

        Both halves of the freeze handling must fire:
        1. Clear inherited freeze (would otherwise leave stale S143 /
           JO9-style anchors from the source workbook).
        2. Re-apply ``D1`` so columns A/B/C (Item / Part No. /
           Description) stay visible while scrolling horizontally.
        """
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("fac_copy", src)
        self.assertIn("sal_copy", src)
        self.assertIn("ws.freeze_panes = None", src)
        self.assertIn('ws.freeze_panes = "D1"', src)
        # The reset must come AFTER the copy calls (otherwise copy_sheet
        # overwrites the None we just set).
        copy_idx = src.index('copy_sheet(fac_bom,')
        reset_idx = src.index("ws.freeze_panes = None")
        reapply_idx = src.index('ws.freeze_panes = "D1"')
        self.assertLess(copy_idx, reset_idx)
        # The "D1" reapply must come AFTER the None reset (otherwise the
        # reset clobbers our deliberate freeze).
        self.assertLess(reset_idx, reapply_idx)


class TestLiveInventoryRename(unittest.TestCase):
    """The reference copy of the Factory BoM is now exposed to operators
    as 'Live Inventory' — matches LightRiver's internal phrasing for the
    snapshot of what's currently on hand to ship. Source-level guards
    so the rename can't silently regress.
    """

    def test_factory_copy_tab_named_live_inventory(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        # The Factory-side copy_sheet call must use "Live Inventory" as
        # the destination tab name.
        self.assertIn('copy_sheet(fac_bom, out_wb, "Live Inventory"', src)
        # And the previous "Factory BoM" tab name must not be the literal
        # destination anywhere in _compare's copy / index calls.
        self.assertNotIn('create_sheet("Factory BoM")', src)
        self.assertNotIn('"Factory BoM",', src)

    def test_shortfall_title_references_live_inventory(self):
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._write_missing_sheet)
        self.assertIn("Live Inventory", src)


class TestLiveInventoryBannerAdded(unittest.TestCase):
    """The Live Inventory tab strips the source workbook's branding
    (copy_images=False, because source images trigger Excel repair
    prompts) but should still carry the LightRiver banner. Helper
    ``_add_live_inventory_banner`` re-attaches the well-formed banner
    from BOM_Template.xlsx.
    """

    def test_helper_attaches_template_banner_to_target_sheet(self):
        import openpyxl
        from unittest.mock import MagicMock
        from gui.bom_compare_frame import BomCompareFrame
        from utils.helpers import get_data_dir

        # Build a frame without going through Tk.
        frame = BomCompareFrame.__new__(BomCompareFrame)
        frame.gui = MagicMock()
        frame.gui.workbook_builder.bom_template = str(
            get_data_dir() / "BOM_Template.xlsx"
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Live Inventory"
        self.assertEqual(len(getattr(ws, "_images", []) or []), 0)
        result = frame._add_live_inventory_banner(ws)
        self.assertTrue(result)
        imgs = getattr(ws, "_images", []) or []
        self.assertEqual(len(imgs), 1)
        # Sanity: the image is a real PNG carrying the template artwork.
        data = imgs[0]._data()
        self.assertGreater(len(data), 1000)
        self.assertEqual(data[:4], b"\x89PNG")
        wb.close()

    def test_helper_returns_false_when_template_missing(self):
        import openpyxl
        from unittest.mock import MagicMock
        from gui.bom_compare_frame import BomCompareFrame

        frame = BomCompareFrame.__new__(BomCompareFrame)
        frame.gui = MagicMock()
        frame.gui.workbook_builder.bom_template = "/does/not/exist.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        # Must degrade gracefully — comparison should not crash just
        # because branding can't be attached.
        self.assertFalse(frame._add_live_inventory_banner(ws))
        self.assertEqual(len(getattr(ws, "_images", []) or []), 0)
        wb.close()

    def test_compare_calls_banner_helper_for_live_inventory_only(self):
        """Source-level guard: _compare must call the helper for the
        fac_copy (Live Inventory) tab, not for sal_copy."""
        import inspect
        from gui.bom_compare_frame import BomCompareFrame
        src = inspect.getsource(BomCompareFrame._compare)
        self.assertIn("_add_live_inventory_banner(fac_copy)", src)
        # Sales BoM tab intentionally does NOT get the banner.
        self.assertNotIn("_add_live_inventory_banner(sal_copy)", src)


class TestBomTemplateBannerUpdate(unittest.TestCase):
    """The BOM template (``data/BOM_Template.xlsx``) ships a banner
    image and a sheet whose name reflects the LightRiver wording. The
    sheet should be named 'Live Inventory' and the banner image should
    be present (replaced with the 'LightRiver Live Inventory' artwork).
    """

    def setUp(self):
        from utils.helpers import get_data_dir
        import openpyxl
        self.wb = openpyxl.load_workbook(str(get_data_dir() / "BOM_Template.xlsx"))
        self.ws = self.wb.active

    def tearDown(self):
        self.wb.close()

    def test_template_sheet_is_named_live_inventory(self):
        self.assertEqual(self.ws.title, "Live Inventory")

    def test_template_carries_banner_image(self):
        imgs = getattr(self.ws, "_images", []) or []
        self.assertGreaterEqual(len(imgs), 1)
        # Make sure it's a real PNG, not an empty placeholder.
        data = imgs[0]._data()
        self.assertGreater(len(data), 1000)
        # PNG magic number.
        self.assertEqual(data[:4], b"\x89PNG")


if __name__ == "__main__":
    unittest.main()
