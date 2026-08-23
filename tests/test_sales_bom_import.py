"""End-to-end tests for the Sales BoM Import flow.

The feature adds a ``Sales BoM`` option to the Raw File Processing
dropdown. Selecting it and uploading a Sales BoM xlsx prompts the
operator to pick a worksheet, then generates a packing-slip-style
output workbook:

* Summary tab with one row per site found in the chosen worksheet
* BOM aggregate tab using the older "Factory Bill of Materials"
  banner via ``data/BoM_Factory_Aggregate_Template.xlsx``
* One tab per site, layout:
    - C5 = customer, C6 = project, C7 = site name
    - Row 14 headers: B14 "Part Number" | C14 "Description" | D14 "Quantity"
    - Row 15+ data with plain numeric quantities (no formulas)
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import openpyxl
from openpyxl.styles import Alignment, Font

from gui.workbook_builder import WorkbookBuilder
from utils.helpers import get_data_dir


def _make_builder():
    db_cache = MagicMock()
    db_cache.db_path = ":memory:"
    db_cache.lookup_part.return_value = ""
    # Use the real BOM template path so _build_bom_sheet's template
    # lookup resolves to the bundled BoM_Factory_Aggregate_Template
    # in the same directory.
    bom_template = str(get_data_dir() / "BOM_Template.xlsx")
    return WorkbookBuilder(
        db_cache=db_cache,
        template_path=bom_template,
        packing_slip_template="",
    )


def _make_sales_bom_source(tmp_dir: str) -> str:
    """Build a minimal multi-sheet Sales BoM xlsx for testing.

    Three sites (ALBA, MNCR, STJO) plus a Spares column. Headers at
    row 11; data from row 12. Includes one extra "draft" sheet so the
    worksheet picker has multiple options.
    """
    path = os.path.join(tmp_dir, "sales_bom.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # Active BoM
    ws = wb.create_sheet("BoM v2 (Final)")
    ws.cell(11, 2, "Part No.")
    ws.cell(11, 3, "Equipment Description")
    ws.cell(11, 4, "ALBA")
    ws.cell(11, 5, "MNCR")
    ws.cell(11, 6, "STJO")
    ws.cell(11, 7, "Spares")
    ws.cell(11, 8, "Total")
    # Row 12+: parts data — Total = ALBA + MNCR + STJO + Spares
    rows = [
        # (pn, desc, alba, mncr, stjo, spares)
        ("3HE11278AA", "7250 IXR-R6 CHASSIS",  2, 1, 1, 0),
        ("3HE11279AA", "7250 IXR-R6 FAN TRAY", 2, 1, 1, 1),
        ("3HE04823AA", "SFP+ 10GE LR",         8, 4, 4, 2),
    ]
    for i, (pn, desc, q_alba, q_mncr, q_stjo, q_sp) in enumerate(rows, start=12):
        ws.cell(i, 2, pn)
        ws.cell(i, 3, desc)
        ws.cell(i, 4, q_alba)
        ws.cell(i, 5, q_mncr)
        ws.cell(i, 6, q_stjo)
        ws.cell(i, 7, q_sp)
        ws.cell(i, 8, q_alba + q_mncr + q_stjo + q_sp)

    # A second draft sheet with the same shape but different numbers
    # so the worksheet picker scenario has multiple options.
    draft = wb.create_sheet("BoM v1 (Draft)")
    draft.cell(11, 2, "Part No.")
    draft.cell(11, 3, "Equipment Description")
    draft.cell(11, 4, "ALBA")
    draft.cell(11, 5, "Total")
    draft.cell(12, 2, "3HE99999XX")
    draft.cell(12, 3, "Old draft part")
    draft.cell(12, 4, 99)
    draft.cell(12, 5, 99)

    wb.save(path)
    wb.close()
    return path


class TestSalesBomImportEndToEnd(unittest.TestCase):
    """Drive the full build pipeline end-to-end and validate the
    resulting workbook structure + cell contents.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sbi_test_")
        self.src = _make_sales_bom_source(self.tmpdir)
        self.out = os.path.join(self.tmpdir, "out.xlsx")
        self.builder = _make_builder()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build(self, sheet="BoM v2 (Final)"):
        return self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets=sheet,
            output_file=self.out,
            customer="ACME",
            project="Phase 4",
        )

    def test_returns_saved_path_and_creates_file(self):
        path = self._build()
        self.assertEqual(path, self.out)
        self.assertTrue(os.path.exists(self.out))

    def test_output_has_summary_bom_and_three_site_tabs(self):
        self._build()
        wb = openpyxl.load_workbook(self.out)
        self.assertIn("Summary", wb.sheetnames)
        self.assertIn("Inventory by Site", wb.sheetnames)
        for site in ("ALBA", "MNCR", "STJO", "Spares"):
            self.assertIn(site, wb.sheetnames, f"{site} tab missing")
        wb.close()

    def test_site_tab_layout(self):
        """Per-site tabs follow the spec:
        - C5=customer, C6=project, C7=site
        - Row 14: B Part Number / C Description / D Quantity
        - Row 15+ data, sorted by Part No.
        """
        self._build()
        wb = openpyxl.load_workbook(self.out)
        ws = wb["ALBA"]
        self.assertEqual(ws["C5"].value, "ACME")
        self.assertEqual(ws["C6"].value, "Phase 4")
        self.assertEqual(ws["C7"].value, "ALBA")
        self.assertEqual(ws["B14"].value, "Part Number")
        self.assertEqual(ws["C14"].value, "Description")
        self.assertEqual(ws["D14"].value, "Quantity")
        # Data rows from 15: three parts at ALBA, sorted by PN.
        # Our source data: 3HE04823AA, 3HE11278AA, 3HE11279AA
        self.assertEqual(ws["B15"].value, "3HE04823AA")
        self.assertEqual(ws["D15"].value, 8)
        self.assertEqual(ws["B16"].value, "3HE11278AA")
        self.assertEqual(ws["D16"].value, 2)
        self.assertEqual(ws["B17"].value, "3HE11279AA")
        self.assertEqual(ws["D17"].value, 2)
        wb.close()

    def test_per_site_quantity_cells_are_plain_numbers_not_formulas(self):
        """Per user spec: Quantity cells are plain numeric values, not
        Excel formulas linking back to the source workbook."""
        self._build()
        wb = openpyxl.load_workbook(self.out)
        for site in ("ALBA", "MNCR", "STJO"):
            ws = wb[site]
            for r in range(15, 20):
                v = ws.cell(r, 4).value
                if v is None:
                    continue
                self.assertIsInstance(v, (int, float),
                    f"{site}!D{r} is {v!r} (expected number)")
                self.assertNotIsInstance(v, str,
                    f"{site}!D{r} is a string ({v!r}) — looks like a formula")
        wb.close()

    def test_per_site_tab_has_return_link_at_a1(self):
        self._build()
        wb = openpyxl.load_workbook(self.out)
        ws = wb["MNCR"]
        self.assertEqual(ws["A1"].value, "Return")
        self.assertIsNotNone(ws["A1"].hyperlink)
        wb.close()

    def test_per_site_tab_freezes_at_row_15(self):
        self._build()
        wb = openpyxl.load_workbook(self.out)
        ws = wb["STJO"]
        self.assertEqual(ws.freeze_panes, "A15")
        wb.close()

    def test_summary_lists_every_site_including_spares(self):
        self._build()
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Summary"]
        # Summary's Device Name column (D) carries site names starting row 10
        names = [
            str(ws.cell(r, 4).value or "").strip()
            for r in range(10, 15)
            if ws.cell(r, 4).value
        ]
        self.assertEqual(set(names), {"ALBA", "MNCR", "STJO", "Spares"})
        wb.close()

    def test_bom_tab_carries_factory_banner_not_live_inventory(self):
        """The BOM aggregate sheet must use the older 'Factory Bill of
        Materials' banner image via BoM_Factory_Aggregate_Template.xlsx,
        not the current BOM_Template's 'LightRiver Live Inventory' one."""
        self._build()
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        self.assertGreaterEqual(len(ws._images), 1,
            "BOM tab should carry a banner image")
        # Compare image bytes against both banner files.
        src_bytes = ws._images[0]._data()
        with open(get_data_dir() / "factory_bom_banner.png", "rb") as f:
            factory_bytes = f.read()
        self.assertEqual(
            src_bytes, factory_bytes,
            "BOM tab's banner doesn't match the older 'Factory Bill of "
            "Materials' artwork — check the template wiring",
        )
        wb.close()

    def test_user_can_select_a_different_worksheet(self):
        """The picker lets the user choose any worksheet — verify
        building against the draft sheet produces a different output."""
        self._build(sheet="BoM v1 (Draft)")
        wb = openpyxl.load_workbook(self.out)
        # Draft only had ALBA as a site
        self.assertIn("ALBA", wb.sheetnames)
        self.assertNotIn("MNCR", wb.sheetnames)
        # Draft had 3HE99999XX with qty 99
        ws = wb["ALBA"]
        self.assertEqual(ws["B15"].value, "3HE99999XX")
        self.assertEqual(ws["D15"].value, 99)
        wb.close()

    def test_invalid_sheet_name_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            self.builder.build_sales_bom_packing_slip_workbook(
                source_path=self.src,
                selected_sheets="DoesNotExist",
                output_file=self.out,
                customer="ACME",
                project="Phase 4",
            )
        self.assertIn("DoesNotExist", str(ctx.exception))


class TestRawFrameWiring(unittest.TestCase):
    """Source-level guards that the Raw frame exposes the Sales BoM
    option and routes to the dedicated worker path."""

    def test_dropdown_contains_sales_bom_entry(self):
        from gui.raw_frame import SCRIPT_OPTIONS, SALES_BOM_IMPORT
        self.assertIn(SALES_BOM_IMPORT, SCRIPT_OPTIONS)
        # Value is a sentinel, not a module path
        self.assertFalse(SCRIPT_OPTIONS[SALES_BOM_IMPORT].startswith("scripts."))

    def test_worker_dispatches_sales_bom_to_dedicated_handler(self):
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._worker)
        # Must branch on SALES_BOM_IMPORT script name and call the
        # dedicated processor.
        self.assertIn("SALES_BOM_IMPORT", src)
        self.assertIn("_process_sales_bom_import", src)


class TestBomAggregateVisibleTotalColumn(unittest.TestCase):
    """The BoM aggregate sheet must surface a 'Total Ordered' column
    at position D (right after Equipment Description) so the operator
    doesn't have to scroll across all the per-site columns to see the
    per-part total. Values are plain numbers (the template's SUM
    formula at the rightmost column is removed because insert_cols
    invalidates its cell references).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sbi_total_test_")
        self.src = _make_sales_bom_source(self.tmpdir)
        self.out = os.path.join(self.tmpdir, "out.xlsx")
        self.builder = _make_builder()
        self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets="BoM v2 (Final)",
            output_file=self.out,
            customer="ACME",
            project="Phase 4",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_bom_tab_has_total_ordered_header_at_d7(self):
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        self.assertEqual(ws.cell(7, 4).value, "Total Ordered")
        self.assertEqual(ws.cell(8, 4).value, "Qty")
        wb.close()

    def test_total_ordered_value_matches_per_site_plus_spares_sum(self):
        """For each data row, D = sum of every site column + Spares
        column. Source rows for our fixture:
          3HE11278AA: 2+1+1 (sites) + 0 (spares) = 4
          3HE11279AA: 2+1+1 + 1 = 5
          3HE04823AA: 8+4+4 + 2 = 18
        """
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        # Locate the row for each part (find by PN in col B).
        wanted = {"3HE11278AA": 4, "3HE11279AA": 5, "3HE04823AA": 18}
        for r in range(10, ws.max_row + 1):
            pn = str(ws.cell(r, 2).value or "").strip()
            if pn in wanted:
                expected = wanted.pop(pn)
                actual = ws.cell(r, 4).value
                self.assertEqual(actual, expected,
                    f"{pn} Total Ordered: got {actual!r}, expected {expected}")
        self.assertEqual(wanted, {}, f"Parts not found on BOM tab: {wanted}")
        wb.close()

    def test_total_column_is_plain_number_not_formula(self):
        """The template's SUM formula is replaced with a plain int so
        Excel doesn't need to re-evaluate the workbook on open."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        for r in range(10, ws.max_row + 1):
            pn = ws.cell(r, 2).value
            v = ws.cell(r, 4).value
            if v is None or pn is None:
                continue
            self.assertIsInstance(v, int,
                f"Row {r} Total Ordered is {v!r} ({type(v).__name__}); "
                f"expected int")
            # Defensive: a plain number cannot start with '='.
            self.assertFalse(
                isinstance(v, str) and v.startswith("="),
                f"Row {r} Total Ordered carries a formula: {v!r}"
            )
        wb.close()

    def test_no_residual_total_formula_at_far_right(self):
        """After the insert, the stale Total formula at the rightmost
        column gets deleted — verify by scanning row 7 for any column
        with header 'Total' OTHER than D7."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        total_cols = [
            c for c in range(1, ws.max_column + 1)
            if str(ws.cell(7, c).value or "").strip().lower() in ("total", "total ordered")
        ]
        self.assertEqual(total_cols, [4],
            f"Expected single Total column at col 4; got {total_cols}")
        wb.close()

    def test_freeze_pane_extended_to_keep_total_visible(self):
        """Original template freezes at D9 (A-C static). With Total
        inserted at D, freeze must extend to E9 so A-D all stay visible
        while scrolling through site columns."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Inventory by Site"]
        self.assertEqual(ws.freeze_panes, "E9")
        wb.close()


class TestSpareColumnSurfacesAsSparesSite(unittest.TestCase):
    """The Sales BoM's 'Spares' column lists extras the customer
    ordered (shipped alongside the per-site allocation). Earlier the
    importer discarded this data, which made the BoM aggregate's
    Total column disagree with the source's Total column. Fix:
    promote the Spares column to a virtual 'Spares' site so it gets
    its own tab, its own column on the BoM aggregate, and rolls
    naturally into the Total column.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sbi_spares_test_")
        self.src = _make_sales_bom_source(self.tmpdir)
        self.out = os.path.join(self.tmpdir, "out.xlsx")
        self.builder = _make_builder()
        self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets="BoM v2 (Final)",
            output_file=self.out,
            customer="ACME",
            project="Phase 4",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_spares_tab_exists(self):
        wb = openpyxl.load_workbook(self.out)
        self.assertIn("Spares", wb.sheetnames)
        wb.close()

    def test_spares_tab_lists_only_parts_with_positive_spares(self):
        """Source data:
        3HE11278AA spares=0 → not listed on Spares tab
        3HE11279AA spares=1 → listed with qty 1
        3HE04823AA spares=2 → listed with qty 2
        """
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Spares"]
        # Data rows start at 15, sorted by Part No.
        # 3HE04823AA (qty 2) sorts before 3HE11279AA (qty 1)
        self.assertEqual(ws["B15"].value, "3HE04823AA")
        self.assertEqual(ws["D15"].value, 2)
        self.assertEqual(ws["B16"].value, "3HE11279AA")
        self.assertEqual(ws["D16"].value, 1)
        # 3HE11278AA had 0 spares — must NOT appear
        for r in range(15, 20):
            self.assertNotEqual(ws.cell(r, 2).value, "3HE11278AA")
        wb.close()

    def test_spares_tab_uses_same_layout_as_site_tabs(self):
        """Spares tab is built by the same path as ALBA / MNCR / STJO
        tabs so the layout, freeze panes, and return link all match."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Spares"]
        self.assertEqual(ws["B14"].value, "Part Number")
        self.assertEqual(ws["C14"].value, "Description")
        self.assertEqual(ws["D14"].value, "Quantity")
        self.assertEqual(ws["A1"].value, "Return")
        self.assertEqual(ws.freeze_panes, "A15")
        # Site Name cell at C7 should read 'Spares'.
        self.assertEqual(ws["C7"].value, "Spares")
        wb.close()

    def test_summary_has_spares_after_real_sites(self):
        """Operator-readable Summary lists Spares LAST (after every
        per-site row) so the page reads ALBA, MNCR, STJO, Spares."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Summary"]
        names = [
            str(ws.cell(r, 4).value or "").strip()
            for r in range(10, 15)
            if ws.cell(r, 4).value
        ]
        # 'Spares' should be at the end; not in the middle of real sites.
        self.assertEqual(names[-1], "Spares")
        wb.close()

    def test_per_site_tabs_dont_include_spares_rows(self):
        """ALBA / MNCR / STJO tabs continue to show only per-site
        quantities; the Spares column doesn't leak into them."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["ALBA"]
        # ALBA had 2 chassis + 2 fan trays + 8 SFPs = 3 rows total
        rows_with_data = 0
        for r in range(15, 25):
            if ws.cell(r, 2).value:
                rows_with_data += 1
        self.assertEqual(rows_with_data, 3)
        wb.close()

    def test_no_spares_column_on_source_yields_no_spares_tab(self):
        """The draft sheet ('BoM v1 (Draft)') has no Spares column.
        Building against it should produce no Spares tab."""
        out2 = os.path.join(self.tmpdir, "out_draft.xlsx")
        self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets="BoM v1 (Draft)",
            output_file=out2,
            customer="ACME",
            project="Phase 4",
        )
        wb = openpyxl.load_workbook(out2)
        self.assertNotIn("Spares", wb.sheetnames)
        wb.close()


class TestSalesBomImportLogging(unittest.TestCase):
    """The Sales BoM Import flow must emit grep-able log lines under
    the ``[SalesBoM]`` prefix at INFO (high-level milestones) and
    DEBUG (data shape) levels so the operator can troubleshoot a
    failed run from the ATLAS log file alone.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sbi_log_test_")
        self.src = _make_sales_bom_source(self.tmpdir)
        self.out = os.path.join(self.tmpdir, "out.xlsx")
        self.builder = _make_builder()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _capture_logs(self, *, level=10):  # logging.DEBUG = 10
        """Capture log records emitted during a build."""
        import logging
        records = []
        handler = logging.Handler()
        handler.setLevel(level)
        handler.emit = records.append
        root = logging.getLogger()
        prev_level = root.level
        root.addHandler(handler)
        root.setLevel(level)
        try:
            self.builder.build_sales_bom_packing_slip_workbook(
                source_path=self.src,
                selected_sheets="BoM v2 (Final)",
                output_file=self.out,
                customer="ACME",
                project="Phase 4",
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
        return records

    def test_logs_have_salesbom_prefix(self):
        """Every log line emitted by the builder is grep-able via
        ``[SalesBoM]`` so the operator can isolate one run's output."""
        records = self._capture_logs()
        sales_bom_records = [
            r for r in records
            if "[SalesBoM]" in r.getMessage()
        ]
        self.assertGreater(len(sales_bom_records), 5,
            f"Expected several [SalesBoM] log lines; got {len(sales_bom_records)}")

    def test_logs_include_begin_and_end_markers(self):
        """The build emits explicit BEGIN/END markers so the operator
        can find where one run starts and stops in a long log file."""
        records = self._capture_logs()
        msgs = [r.getMessage() for r in records]
        self.assertTrue(
            any("=== BEGIN ===" in m and "[SalesBoM]" in m for m in msgs),
            "Missing [SalesBoM] === BEGIN === log line",
        )
        self.assertTrue(
            any("=== END ===" in m and "[SalesBoM]" in m for m in msgs),
            "Missing [SalesBoM] === END === log line",
        )

    def test_logs_record_parse_results(self):
        """The parser step logs site / part counts so the operator
        can verify the input was understood correctly."""
        records = self._capture_logs()
        msgs = [r.getMessage() for r in records]
        # Source data has 3 parts, 3 sites
        self.assertTrue(
            any("3 part(s)" in m and "3 site(s)" in m for m in msgs),
            "Parse-result log line missing or unexpected count",
        )

    def test_logs_record_end_timing_and_output_size(self):
        """The END marker carries elapsed time + final output file
        size so the operator can sanity-check the build."""
        records = self._capture_logs()
        end_lines = [
            r.getMessage() for r in records
            if "[SalesBoM]" in r.getMessage() and "=== END ===" in r.getMessage()
        ]
        self.assertEqual(len(end_lines), 1)
        end = end_lines[0]
        self.assertIn("elapsed=", end)
        self.assertIn("size=", end)
        self.assertIn("sheets=", end)

    def test_invalid_sheet_logs_error_before_raising(self):
        """A bad sheet name surfaces a clear ERROR log line in
        addition to the ValueError."""
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with self.assertRaises(ValueError):
                self.builder.build_sales_bom_packing_slip_workbook(
                    source_path=self.src,
                    selected_sheets="DoesNotExist",
                    output_file=self.out,
                    customer="ACME",
                    project="Phase 4",
                )
        finally:
            root.removeHandler(handler)
        err_lines = [
            r for r in records
            if r.levelno >= logging.ERROR
            and "[SalesBoM]" in r.getMessage()
            and "DoesNotExist" in r.getMessage()
        ]
        self.assertGreaterEqual(len(err_lines), 1,
            "Expected at least one ERROR-level [SalesBoM] log naming "
            "the bad sheet")


class TestSalesBomImportUiLoggingSourceLevel(unittest.TestCase):
    """Source-level guards that the raw_frame.py Sales BoM handlers
    carry the ``[SalesBoM-UI]`` prefix and call ``logging.exception``
    at error sites — checked statically so we don't need a Tk root."""

    def test_process_handler_uses_salesbom_ui_prefix(self):
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._process_sales_bom_import)
        # Multiple log emit points
        self.assertGreaterEqual(src.count("[SalesBoM-UI]"), 5)
        # Errors are logged with full traceback
        self.assertIn("logging.exception", src)

    def test_export_handler_uses_salesbom_ui_prefix_and_exception_logging(self):
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._export_sales_bom)
        self.assertGreaterEqual(src.count("[SalesBoM-UI]"), 5)
        self.assertIn("logging.exception", src)

    def test_pick_dialog_logs_each_outcome(self):
        """OK-with-selection, OK-without-selection, and Cancel all
        emit DEBUG log lines so the dialog's interaction trail is
        visible."""
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._pick_worksheet_dialog)
        self.assertIn("OK ->", src)        # successful pick
        self.assertIn("cancelled", src)    # cancel path


def _make_multi_sheet_source(tmp_dir: str) -> str:
    """Build a Sales BoM with two sheets that share some sites and
    have non-overlapping ones, so multi-sheet merge behavior can be
    asserted end-to-end."""
    path = os.path.join(tmp_dir, "sales_bom_multi.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    def _make_sheet(ws, sites, rows):
        # Headers: B=Part No., C=Description, then site columns, then Spares, then Total
        ws.cell(11, 2, "Part No.")
        ws.cell(11, 3, "Equipment Description")
        for i, site in enumerate(sites):
            ws.cell(11, 4 + i, site)
        sp_col = 4 + len(sites)
        ws.cell(11, sp_col, "Spares")
        ws.cell(11, sp_col + 1, "Total")
        for r_idx, (pn, desc, qtys, sp) in enumerate(rows, start=12):
            ws.cell(r_idx, 2, pn)
            ws.cell(r_idx, 3, desc)
            for i, q in enumerate(qtys):
                ws.cell(r_idx, 4 + i, q)
            ws.cell(r_idx, sp_col, sp)
            ws.cell(r_idx, sp_col + 1, sum(qtys) + sp)

    # Phase 1: ALBA + MNCR
    phase1 = wb.create_sheet("Phase 1")
    _make_sheet(phase1, ["ALBA", "MNCR"], [
        # (pn, desc, [qty_alba, qty_mncr], spares)
        ("3HE11278AA", "Chassis", [2, 1], 1),  # 4 total
        ("3HE04823AA", "SFP+ 10G", [4, 2], 1),  # 7 total
    ])
    # Phase 2: ALBA (overlap) + STJO (new)
    phase2 = wb.create_sheet("Phase 2")
    _make_sheet(phase2, ["ALBA", "STJO"], [
        ("3HE11278AA", "Chassis", [1, 2], 0),       # adds 1 ALBA, 2 STJO
        ("3HE12546AA", "SFP C37.94", [5, 5], 2),    # NEW part, only in Phase 2
    ])

    wb.save(path)
    wb.close()
    return path


class TestMultiSheetMerge(unittest.TestCase):
    """Picking multiple sheets in the worksheet dialog merges their
    data into a single output workbook:

    - Sites shared across sheets sum their quantities
    - Sites unique to one sheet get their own tab
    - Spares column sums across sheets too
    - Parts unique to one sheet appear naturally
    - First-seen non-empty description wins
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="sbi_multi_test_")
        self.src = _make_multi_sheet_source(self.tmpdir)
        self.out = os.path.join(self.tmpdir, "out.xlsx")
        self.builder = _make_builder()
        self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets=["Phase 1", "Phase 2"],
            output_file=self.out,
            customer="ACME",
            project="Multi-Phase",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_output_has_union_of_sites(self):
        """ALBA appears in both sheets (merged); MNCR only in Phase 1;
        STJO only in Phase 2. All three should be tabs, plus Spares."""
        wb = openpyxl.load_workbook(self.out)
        for site in ("ALBA", "MNCR", "STJO", "Spares"):
            self.assertIn(site, wb.sheetnames, f"{site} tab missing")
        wb.close()

    def test_shared_site_sums_quantities_across_sheets(self):
        """ALBA's chassis qty: Phase 1 = 2, Phase 2 = 1 -> merged = 3"""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["ALBA"]
        # 3HE11278AA at ALBA: 2 from Phase 1 + 1 from Phase 2 = 3
        # 3HE04823AA at ALBA: 4 from Phase 1 (only)
        # 3HE12546AA at ALBA: 5 from Phase 2 (only)
        # Total of 3 unique parts
        rows = [(ws.cell(r, 2).value, ws.cell(r, 4).value)
                for r in range(15, 25) if ws.cell(r, 2).value]
        d = dict(rows)
        self.assertEqual(d["3HE11278AA"], 3)
        self.assertEqual(d["3HE04823AA"], 4)
        self.assertEqual(d["3HE12546AA"], 5)
        wb.close()

    def test_site_unique_to_one_sheet_carries_its_quantities(self):
        """MNCR only existed on Phase 1 → tab carries Phase 1's values."""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["MNCR"]
        rows = {ws.cell(r, 2).value: ws.cell(r, 4).value
                for r in range(15, 25) if ws.cell(r, 2).value}
        self.assertEqual(rows.get("3HE11278AA"), 1)
        self.assertEqual(rows.get("3HE04823AA"), 2)
        # Phase 2's chassis 12546 isn't at MNCR — must not appear
        self.assertNotIn("3HE12546AA", rows)
        wb.close()

    def test_spares_column_sums_across_sheets(self):
        """3HE11278AA spares: Phase 1 = 1, Phase 2 = 0 -> total 1
        3HE04823AA spares: Phase 1 = 1, Phase 2 = 0 -> total 1
        3HE12546AA spares: Phase 2 only = 2"""
        wb = openpyxl.load_workbook(self.out)
        ws = wb["Spares"]
        rows = {ws.cell(r, 2).value: ws.cell(r, 4).value
                for r in range(15, 25) if ws.cell(r, 2).value}
        self.assertEqual(rows.get("3HE11278AA"), 1)
        self.assertEqual(rows.get("3HE04823AA"), 1)
        self.assertEqual(rows.get("3HE12546AA"), 2)
        wb.close()

    def test_part_only_in_second_sheet_appears(self):
        """3HE12546AA only exists in Phase 2 — must appear in the
        merged output's ALBA + STJO + Spares tabs."""
        wb = openpyxl.load_workbook(self.out)
        for site in ("ALBA", "STJO", "Spares"):
            ws = wb[site]
            pns = {ws.cell(r, 2).value for r in range(15, 25)}
            self.assertIn("3HE12546AA", pns,
                f"3HE12546AA missing on {site} tab")
        wb.close()

    def test_single_sheet_still_works_as_string(self):
        """Backward compat: passing a single sheet name (str, not
        list) must still work — legacy callers continue to function."""
        out2 = os.path.join(self.tmpdir, "single.xlsx")
        self.builder.build_sales_bom_packing_slip_workbook(
            source_path=self.src,
            selected_sheets="Phase 1",  # str, not list
            output_file=out2,
            customer="ACME",
            project="Phase 1 only",
        )
        wb = openpyxl.load_workbook(out2)
        for site in ("ALBA", "MNCR", "Spares"):
            self.assertIn(site, wb.sheetnames)
        # STJO was only on Phase 2 — must NOT appear
        self.assertNotIn("STJO", wb.sheetnames)
        wb.close()

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.builder.build_sales_bom_packing_slip_workbook(
                source_path=self.src,
                selected_sheets=[],
                output_file=self.out,
                customer="ACME",
                project="Empty",
            )
        self.assertIn("must not be empty", str(ctx.exception))

    def test_any_invalid_sheet_in_list_raises(self):
        """If one selected sheet is missing the build aborts with a
        clear ValueError listing every missing sheet."""
        with self.assertRaises(ValueError) as ctx:
            self.builder.build_sales_bom_packing_slip_workbook(
                source_path=self.src,
                selected_sheets=["Phase 1", "DoesNotExist"],
                output_file=self.out,
                customer="ACME",
                project="Bad list",
            )
        self.assertIn("DoesNotExist", str(ctx.exception))

    def test_multi_sheet_logs_include_merge_summary(self):
        """The merge step emits an INFO log line summarizing the
        rollup so operators can verify the merge worked."""
        import logging
        records = []
        handler = logging.Handler()
        handler.setLevel(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        prev_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            self.builder.build_sales_bom_packing_slip_workbook(
                source_path=self.src,
                selected_sheets=["Phase 1", "Phase 2"],
                output_file=os.path.join(self.tmpdir, "logged.xlsx"),
                customer="ACME",
                project="Multi-Phase",
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(prev_level)
        merge_lines = [
            r.getMessage() for r in records
            if "Multi-sheet merge complete" in r.getMessage()
        ]
        self.assertEqual(len(merge_lines), 1)
        msg = merge_lines[0]
        self.assertIn("2 sheets", msg)


class TestPickerDialogMultiSelect(unittest.TestCase):
    """Source-level guards that the worksheet picker now supports
    multi-select via tk.EXTENDED selectmode."""

    def test_dialog_uses_extended_selectmode(self):
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._pick_worksheet_dialog)
        self.assertIn("tk.EXTENDED", src,
            "Worksheet picker should use EXTENDED selectmode for "
            "shift/ctrl-click multi-select")

    def test_dialog_returns_list_of_names(self):
        """The OK handler builds a list comprehension over curselection,
        not just sheet_names[sel[0]] like the old single-select code."""
        import inspect
        from gui.raw_frame import RawFrame
        src = inspect.getsource(RawFrame._pick_worksheet_dialog)
        # The new code: [sheet_names[i] for i in sel]
        self.assertIn("for i in sel", src)


if __name__ == "__main__":
    unittest.main()
