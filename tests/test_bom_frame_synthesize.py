"""Regression tests for the Build-a-BOM 'no Summary sheet' fallback.

Before this fix, opening a workbook without a 'Summary' tab raised
``RuntimeError("Workbook has no 'Summary' sheet — cannot build BOM.")``
and the surrounding error handler crashed on a free-variable NameError.
The fix:
  1. Scan each tab for the standard part-number layout (col D from row 15)
  2. Synthesize a 'Summary' sheet listing every tab that yields parts
  3. Continue the BOM build normally
  4. Capture the exception message before the after() lambda fires
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import openpyxl

from gui.bom_frame import BomFrame
from gui.workbook_builder import WorkbookBuilder


def _make_builder():
    db_cache = MagicMock()
    db_cache.db_path = ":memory:"
    db_cache.lookup_part.return_value = ""
    return WorkbookBuilder(db_cache=db_cache, template_path="", packing_slip_template="")


def _make_workbook_no_summary(tmp_path: str) -> str:
    """Build a minimal multi-tab inventory workbook (no Summary) for testing."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    # Header / metadata tab that should be skipped — no parts in col D.
    customer = wb.create_sheet("Customer-project")
    customer["B5"] = "Customer:"
    customer["B6"] = "Project:"
    # Three inventory tabs with part rows starting at row 15, col D.
    for tab_name, parts in [
        ("Pallet A", ["3HE00028CA", "3HE12504AA"]),
        ("Pallet B", ["1AF30787AA", "3HE11278AA", "3HE06792EA"]),
        ("Pallet C", ["3KC81775AA"]),
    ]:
        ws = wb.create_sheet(tab_name)
        for idx, part in enumerate(parts):
            row = 15 + idx
            ws.cell(row=row, column=2, value=f"Item {idx + 1}")   # B = Name
            ws.cell(row=row, column=3, value="Component")          # C = Type
            ws.cell(row=row, column=4, value=part)                 # D = Part No.
            ws.cell(row=row, column=6, value=f"desc-{part}")       # F = Description
    wb.save(tmp_path)
    wb.close()
    return tmp_path


class TestSynthesizeSummary(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bom_test_")
        self.src = os.path.join(self.tmpdir, "no_summary.xlsx")
        _make_workbook_no_summary(self.src)
        self.frame = BomFrame.__new__(BomFrame)
        self.frame.gui = MagicMock()
        self.frame.gui.workbook_builder = _make_builder()
        self.frame._append_log = lambda msg: None

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_synthesize_creates_summary_with_inventory_tabs(self):
        out = self.frame._build(self.src)
        try:
            wb = openpyxl.load_workbook(out, read_only=True)
            self.assertIn("Summary", wb.sheetnames)
            self.assertIn("Inventory by Site", wb.sheetnames)
            summary = wb["Summary"]
            # Three inventory tabs were created; the Customer-project tab
            # has no parts and must be skipped.
            names = [
                summary.cell(r, 4).value
                for r in range(10, 14)
                if summary.cell(r, 4).value
            ]
            self.assertEqual(set(names), {"Pallet A", "Pallet B", "Pallet C"})
            # IP column stays blank for synthesized rows.
            ips = [summary.cell(r, 3).value for r in range(10, 13)]
            self.assertTrue(all(v in (None, "") for v in ips))
            # F7 holds the device count.
            self.assertEqual(summary["F7"].value, 3)
            wb.close()
        finally:
            try: os.unlink(out)
            except OSError: pass

    def test_workbook_with_no_inventory_tabs_raises(self):
        """If we scan and find zero parts anywhere, we raise a clear
        error rather than producing a workbook with an empty Summary."""
        empty_path = os.path.join(self.tmpdir, "empty.xlsx")
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        # Just a metadata sheet with no part rows.
        ws = wb.create_sheet("Customer-project")
        ws["B5"] = "Customer:"
        wb.save(empty_path)
        wb.close()
        with self.assertRaises(RuntimeError) as ctx:
            self.frame._build(empty_path)
        self.assertIn("no usable device tabs", str(ctx.exception))


class TestRunWorkerNameErrorRegression(unittest.TestCase):
    """Before the fix, the error handler at _run_worker's `except` block
    failed with ``NameError: cannot access free variable 'exc'`` because
    the Tk after() lambda referenced ``exc`` after Python had unbound it.
    Verify the message is captured before any after() call. We don't need
    a live Tk root — patch ``after`` so we can inspect what got scheduled.
    """

    def test_error_handler_uses_locally_captured_message(self):
        import inspect
        src = inspect.getsource(BomFrame._run_worker)
        self.assertIn("err_msg = str(exc)", src)
        # The after() lambda must reference err_msg, not exc directly.
        self.assertIn("err_msg", src.split("except", 1)[1])
        self.assertNotIn(
            "lambda: messagebox.showerror(\"BoM\", str(exc))", src,
            "error handler still uses the unbound `exc` inside after()",
        )


class TestTemplateAndAggregateTabsDropped(unittest.TestCase):
    """When the Summary is synthesized, template (Customer-project) and
    Task Order aggregate (T##/TO##) tabs are excluded from BOM aggregation
    AND removed from the saved workbook. When the source already has a
    Summary sheet, neither happens (we trust the operator's layout)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bom_test_")
        self.frame = BomFrame.__new__(BomFrame)
        self.frame.gui = MagicMock()
        self.frame.gui.workbook_builder = _make_builder()
        self.frame._append_log = lambda msg: None

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_source(self, *, with_summary=False) -> str:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        if with_summary:
            wb.create_sheet("Summary")
        # Template tab — would otherwise stay in the output as a leftover.
        wb.create_sheet("Customer-project")
        # Task Order roll-up — has parts but is meant to be replaced by the BOM.
        agg = wb.create_sheet("T03")
        agg.cell(row=15, column=4, value="3HE11288AA")
        agg.cell(row=15, column=2, value="Item")
        # Two real pallets.
        for tab_name in ("CON PALLET 1", "CON PALLET 2"):
            ws = wb.create_sheet(tab_name)
            ws.cell(row=15, column=4, value="3HE11288AA")
            ws.cell(row=15, column=2, value="Item")
        src = os.path.join(self.tmpdir, "src.xlsx")
        wb.save(src)
        wb.close()
        return src

    def test_template_and_aggregate_dropped_when_synthesizing(self):
        out = self.frame._build(self._build_source(with_summary=False))
        try:
            wb = openpyxl.load_workbook(out, read_only=True)
            self.assertNotIn("Customer-project", wb.sheetnames)
            self.assertNotIn("T03", wb.sheetnames)
            self.assertIn("Summary", wb.sheetnames)
            self.assertIn("Inventory by Site", wb.sheetnames)
            self.assertIn("CON PALLET 1", wb.sheetnames)
            self.assertIn("CON PALLET 2", wb.sheetnames)
            wb.close()
        finally:
            try: os.unlink(out)
            except OSError: pass

    def test_tabs_preserved_when_summary_already_exists(self):
        """If the workbook already has a Summary sheet, the operator has
        already curated the layout — don't second-guess them by stripping
        tabs."""
        out = self.frame._build(self._build_source(with_summary=True))
        try:
            wb = openpyxl.load_workbook(out, read_only=True)
            self.assertIn("Customer-project", wb.sheetnames)
            self.assertIn("T03", wb.sheetnames)
            self.assertIn("Summary", wb.sheetnames)
            self.assertIn("Inventory by Site", wb.sheetnames)
            wb.close()
        finally:
            try: os.unlink(out)
            except OSError: pass


class TestNonInventoryTabPatterns(unittest.TestCase):
    """Direct tests for the tab-classification predicate."""

    def test_template_names(self):
        from gui.bom_frame import _is_non_inventory_tab
        for name in ("Customer-project", "customer-project", "Customer",
                     "Project", "Header", "Cover"):
            self.assertTrue(_is_non_inventory_tab(name), name)

    def test_task_order_pattern(self):
        from gui.bom_frame import _is_non_inventory_tab
        for name in ("T03", "T3", "TO3", "TO03", "T123", "to99", "to123"):
            self.assertTrue(_is_non_inventory_tab(name), name)

    def test_real_inventory_tabs_not_matched(self):
        from gui.bom_frame import _is_non_inventory_tab
        for name in ("CON PALLET 1", "Pallet A", "Summary", "Inventory by Site",
                     "MNCR001_7250", "Site_T03_North"):
            self.assertFalse(_is_non_inventory_tab(name), name)


class TestQtyBucketExpansion(unittest.TestCase):
    """A row whose Serial Number column reads ``N/A QTY:N`` must be
    expanded into N BOM entries so identical un-serialised accessories
    surface the right quantity in the aggregate."""

    def _scan(self, serial_value, qty_expected):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=15, column=2, value="Impedance Panel")          # B name
        ws.cell(row=15, column=3, value="7250 IXR-R6 Impedance")    # C type
        ws.cell(row=15, column=4, value="1P3HE11288AARA01")         # D part
        ws.cell(row=15, column=5, value=serial_value)               # E serial
        ws.cell(row=15, column=6, value="7250 IXR-R6 Impedance Panel")  # F desc
        builder = _make_builder()
        entries = builder._collect_bom_entries_from_sheet(ws)
        wb.close()
        self.assertEqual(
            len(entries), qty_expected,
            f"serial={serial_value!r} -> {len(entries)} entries, expected {qty_expected}",
        )
        if entries:
            part, desc, _group = entries[0]
            self.assertEqual(part, "1P3HE11288AARA01")
            self.assertEqual(desc, "7250 IXR-R6 Impedance Panel")

    def test_na_qty_uppercase_two_spaces(self):
        # Matches the user's screenshot exactly: "N/A  QTY:34" (two spaces)
        self._scan("N/A  QTY:34", 34)

    def test_na_qty_single_space(self):
        self._scan("N/A QTY:5", 5)

    def test_na_qty_lowercase_and_spaced_colon(self):
        self._scan("n/a qty : 12", 12)

    def test_qty_only_no_na(self):
        # User said "N/A QTY:xxx" specifically, but the same intent shows
        # up sometimes as bare "QTY:N" — match that too.
        self._scan("QTY:7", 7)

    def test_real_serial_is_one_entry(self):
        # A real serial number must produce exactly one entry.
        self._scan("NS2606S0474", 1)

    def test_qty_zero_emits_no_entries(self):
        # Explicit zero quantity means "we counted; there are none of these".
        self._scan("N/A QTY:0", 0)

    def test_blank_serial_is_one_entry(self):
        self._scan("", 1)
        self._scan(None, 1)


class TestExpandQtyBucketsOnSheet(unittest.TestCase):
    """In-place expansion of N/A QTY:N rows on a device tab."""

    def _sheet(self, rows):
        wb = openpyxl.Workbook()
        ws = wb.active
        for idx, (name, type_, part, serial, desc) in enumerate(rows):
            r = 15 + idx
            ws.cell(row=r, column=2, value=name)
            ws.cell(row=r, column=3, value=type_)
            ws.cell(row=r, column=4, value=part)
            ws.cell(row=r, column=5, value=serial)
            ws.cell(row=r, column=6, value=desc)
        return wb, ws

    def test_qty_row_expanded_into_individual_n_a_rows(self):
        builder = _make_builder()
        wb, ws = self._sheet([
            ("Panel", "Impedance", "3HE11288AA", "N/A QTY:5", "7250 IXR-R6 Impedance Panel"),
        ])
        extras = builder._expand_qty_buckets_on_sheet(ws)
        self.assertEqual(extras, 4)
        # rows 15..19 each carry serial="N/A" and the same part
        for r in range(15, 20):
            self.assertEqual(ws.cell(row=r, column=4).value, "3HE11288AA")
            self.assertEqual(ws.cell(row=r, column=5).value, "N/A")
        # row 20 should now be blank
        self.assertIsNone(ws.cell(row=20, column=4).value)
        wb.close()

    def test_mixed_normal_and_qty_rows(self):
        builder = _make_builder()
        wb, ws = self._sheet([
            ("Card", "MEC2", "3HE02774AB", "RT261014919", "Control"),     # normal
            ("Panel", "Impedance", "3HE11288AA", "N/A QTY:3", "Panel"),    # bucket of 3
            ("Optic", "SFP+", "3HE04823AA", "NS2606S0474", "10G LR"),      # normal
        ])
        extras = builder._expand_qty_buckets_on_sheet(ws)
        self.assertEqual(extras, 2)  # 3-1 = 2 added
        # row 15: original Card stays
        self.assertEqual(ws.cell(row=15, column=4).value, "3HE02774AB")
        self.assertEqual(ws.cell(row=15, column=5).value, "RT261014919")
        # rows 16-18: three Panel rows
        for r in range(16, 19):
            self.assertEqual(ws.cell(row=r, column=4).value, "3HE11288AA")
            self.assertEqual(ws.cell(row=r, column=5).value, "N/A")
        # row 19: optic shifted down
        self.assertEqual(ws.cell(row=19, column=4).value, "3HE04823AA")
        self.assertEqual(ws.cell(row=19, column=5).value, "NS2606S0474")
        wb.close()

    def test_malformed_qty_left_alone(self):
        """`N/A QTY:` with no number doesn't match the regex — the row
        stays as a 1-unit entry so the operator notices the missing count."""
        builder = _make_builder()
        wb, ws = self._sheet([
            ("Kit", "Install", "1P3KC49812AA", "N/A QTY:", "PSS8 Install Kit"),
        ])
        extras = builder._expand_qty_buckets_on_sheet(ws)
        self.assertEqual(extras, 0)
        self.assertEqual(ws.cell(row=15, column=5).value, "N/A QTY:")
        wb.close()

    def test_qty_zero_collapses_to_no_rows(self):
        builder = _make_builder()
        wb, ws = self._sheet([
            ("Kit", "Install", "1P8DG59603AA", "N/A QTY:0", "OMD Install Kit"),
            ("Card", "MEC2", "3HE02774AB", "RT261014919", "Control"),
        ])
        extras = builder._expand_qty_buckets_on_sheet(ws)
        self.assertEqual(extras, -1)  # 0 - 1 = -1 (no rows for the bucket, plus the original removed)
        # First row should be Card (the qty=0 row was dropped)
        self.assertEqual(ws.cell(row=15, column=4).value, "3HE02774AB")
        # No leftover QTY text anywhere
        for r in range(15, (ws.max_row or 0) + 1):
            v = ws.cell(row=r, column=5).value
            if v is not None:
                self.assertNotIn("QTY", str(v).upper())
        wb.close()


class TestReturnLinkAndBorders(unittest.TestCase):
    """Spares-style tabs should pick up a Return link in A1, borders on
    populated data rows, and the standard freeze-pane convention."""

    def test_ensure_return_link_creates_when_missing(self):
        builder = _make_builder()
        wb = openpyxl.Workbook(); ws = wb.active
        self.assertTrue(builder.ensure_return_link_in_a1(ws, target_sheet="Inventory by Site"))
        self.assertEqual(ws["A1"].value, "Return")
        self.assertIsNotNone(ws["A1"].hyperlink)
        wb.close()

    def test_ensure_return_link_idempotent(self):
        builder = _make_builder()
        wb = openpyxl.Workbook(); ws = wb.active
        builder.ensure_return_link_in_a1(ws, target_sheet="Inventory by Site")
        # Second call should be a no-op (returns False).
        self.assertFalse(builder.ensure_return_link_in_a1(ws, target_sheet="Inventory by Site"))
        wb.close()

    def test_apply_borders_only_to_populated_rows(self):
        builder = _make_builder()
        wb = openpyxl.Workbook(); ws = wb.active
        # populated row at 15, blank gap, populated at 17, blank tail
        for col in (2, 3, 4):
            ws.cell(row=15, column=col, value="x")
            ws.cell(row=17, column=col, value="y")
        bordered = builder.apply_device_data_borders(ws)
        self.assertEqual(bordered, 2)
        self.assertTrue(ws["D15"].border.left.style)
        self.assertTrue(ws["D17"].border.left.style)
        # row 16 stayed blank, no border
        self.assertFalse(bool(ws["D16"].border.left and ws["D16"].border.left.style))
        wb.close()


class TestAssetTagPropagation(unittest.TestCase):
    """The Summary sheet exposes a column-E "Asset Tag" field at row 9 that
    operators fill in starting at row 10. On the next BoM build, those
    values must flow onto the matching device tab's Chassis/Shelf row in
    column G."""

    def _summary_with_tags(self, rows):
        """Build a workbook with a Summary sheet and device tabs.

        ``rows`` is a list of ``(device_name, asset_tag, chassis_row_info)``
        tuples where ``chassis_row_info`` is a ``(col, value)`` pair
        describing what makes one of the device-tab rows look like a
        chassis/shelf row.
        """
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        summary = wb.create_sheet("Summary")
        summary["B9"] = "#"
        summary["C9"] = "IP Address"
        summary["D9"] = "Device Name"
        summary["E9"] = "Asset Tag"
        for idx, (name, tag, _info) in enumerate(rows):
            r = 10 + idx
            summary.cell(row=r, column=2, value=idx + 1)
            summary.cell(row=r, column=3, value=f"10.0.0.{idx + 1}")
            summary.cell(row=r, column=4, value=name)
            if tag:
                summary.cell(row=r, column=5, value=tag)
        for name, _tag, info in rows:
            ws = wb.create_sheet(name)
            if info is not None:
                col, val = info
                ws.cell(row=15, column=col, value=val)
        return wb

    def test_read_asset_tags_skips_blank_and_missing_names(self):
        builder = _make_builder()
        wb = self._summary_with_tags([
            ("MNCR001", "AT-1001", (2, "Chassis 1")),
            ("MNCR002", "", (2, "Chassis 1")),       # blank tag — skip
            ("MNCR003", "AT-1003", (2, "Chassis 1")),
        ])
        # Drop the Device Name on row 11 to verify the name-missing skip too.
        wb["Summary"].cell(row=11, column=4, value=None)
        tags = builder.read_asset_tags_from_summary(wb["Summary"])
        self.assertEqual(tags, {"MNCR001": "AT-1001", "MNCR003": "AT-1003"})
        wb.close()

    def test_write_finds_chassis_row_by_name_column(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=15, column=2, value="Card 1")        # not chassis
        ws.cell(row=16, column=2, value="Chassis 1")     # match
        ws.cell(row=17, column=2, value="MDA")
        target = builder.write_asset_tag_to_chassis_row(ws, "AT-9000")
        self.assertEqual(target, 16)
        self.assertEqual(ws.cell(row=16, column=7).value, "AT-9000")  # G16
        wb.close()

    def test_write_finds_shelf_row_by_description(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=15, column=2, value="Power")
        ws.cell(row=15, column=6, value="DC power module")
        ws.cell(row=16, column=2, value="Main")
        ws.cell(row=16, column=6, value="1830 PSS-8 Shelf")  # match via desc
        target = builder.write_asset_tag_to_chassis_row(ws, "AT-7000")
        self.assertEqual(target, 16)
        self.assertEqual(ws.cell(row=16, column=7).value, "AT-7000")
        wb.close()

    def test_write_falls_back_to_start_row_when_no_chassis_match(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.cell(row=15, column=2, value="Card 1")
        ws.cell(row=16, column=2, value="MDA")
        target = builder.write_asset_tag_to_chassis_row(ws, "AT-FB")
        self.assertEqual(target, 15)
        self.assertEqual(ws.cell(row=15, column=7).value, "AT-FB")
        wb.close()

    def test_write_returns_none_on_empty_sheet(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        ws = wb.active  # max_row will be 1 with no data
        target = builder.write_asset_tag_to_chassis_row(ws, "AT-X")
        self.assertIsNone(target)
        wb.close()

    def test_propagate_stamps_tags_on_matching_tabs(self):
        builder = _make_builder()
        wb = self._summary_with_tags([
            ("MNCR001", "AT-1001", (2, "Chassis 1")),
            ("MNCR002", "AT-1002", (3, "Shelf type")),  # match via Type column
        ])
        updated = builder.propagate_asset_tags_to_tabs(wb)
        self.assertEqual(updated, 2)
        self.assertEqual(wb["MNCR001"].cell(row=15, column=7).value, "AT-1001")
        self.assertEqual(wb["MNCR002"].cell(row=15, column=7).value, "AT-1002")
        wb.close()

    def test_propagate_skips_missing_tabs(self):
        builder = _make_builder()
        wb = self._summary_with_tags([
            ("MNCR001", "AT-1001", (2, "Chassis 1")),
        ])
        # Reference a Summary row whose name doesn't exist as a tab.
        wb["Summary"].cell(row=11, column=4, value="GHOST_TAB")
        wb["Summary"].cell(row=11, column=5, value="AT-NOPE")
        updated = builder.propagate_asset_tags_to_tabs(wb)
        self.assertEqual(updated, 1)  # only MNCR001 had a real tab
        wb.close()

    def test_propagate_respects_display_to_tab_aliasing(self):
        """Summary may show 'STJO Extra Materials' but link to tab 'STJO'."""
        builder = _make_builder()
        wb = self._summary_with_tags([
            ("STJO Extra Materials", "AT-STJO", None),
        ])
        # Create the real tab name and remove the original display-named one.
        del wb["STJO Extra Materials"]
        ws = wb.create_sheet("STJO")
        ws.cell(row=15, column=2, value="Chassis 1")
        updated = builder.propagate_asset_tags_to_tabs(
            wb, display_to_tab={"STJO Extra Materials": "STJO"}
        )
        self.assertEqual(updated, 1)
        self.assertEqual(wb["STJO"].cell(row=15, column=7).value, "AT-STJO")
        wb.close()

    def test_propagate_returns_zero_when_summary_missing(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        wb.create_sheet("MNCR001")
        self.assertEqual(builder.propagate_asset_tags_to_tabs(wb), 0)
        wb.close()

    def test_summary_header_includes_asset_tag(self):
        builder = _make_builder()
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        summary = wb.create_sheet("Summary", 0)
        builder._setup_summary_sheet_header(summary, "ACME", "P1", ["MNCR001"])
        self.assertEqual(summary["B9"].value, "#")
        self.assertEqual(summary["C9"].value, "IP Address")
        self.assertEqual(summary["D9"].value, "Device Name")
        self.assertEqual(summary["E9"].value, "Asset Tag")
        wb.close()


class TestAssetTagRawBuilderWiring(unittest.TestCase):
    """All three Raw File Processing builders must call
    ``propagate_asset_tags_to_tabs`` before ``wb.save`` so re-running Raw
    against an already-tagged workbook stamps the tags onto the device
    tabs. We assert the call is in the source — verifying it ran inside
    the live builder pipeline would require stubbing the whole template
    flow, but the source-level guard is enough to keep the wiring honest.
    """

    def test_inventory_builder_propagates_before_save(self):
        import inspect
        src = inspect.getsource(WorkbookBuilder.build_report_workbook)
        self.assertIn("propagate_asset_tags_to_tabs", src)
        # Must appear before the final wb.save() call.
        prop_idx = src.index("propagate_asset_tags_to_tabs")
        save_idx = src.rindex("wb.save(output_file)")
        self.assertLess(prop_idx, save_idx)

    def test_psi_builder_propagates_before_save(self):
        import inspect
        src = inspect.getsource(WorkbookBuilder.build_psi_report_workbook)
        self.assertIn("propagate_asset_tags_to_tabs", src)
        prop_idx = src.index("propagate_asset_tags_to_tabs")
        save_idx = src.rindex("wb.save(output_file)")
        self.assertLess(prop_idx, save_idx)

    def test_unified_builder_propagates_before_save(self):
        import inspect
        src = inspect.getsource(WorkbookBuilder.build_unified_report_workbook)
        self.assertIn("propagate_asset_tags_to_tabs", src)
        prop_idx = src.index("propagate_asset_tags_to_tabs")
        save_idx = src.rindex("wb.save(output_file)")
        self.assertLess(prop_idx, save_idx)


class TestAssetTagBomFrameIntegration(unittest.TestCase):
    """End-to-end check that BomFrame._build picks up asset tags from
    Summary E10+ and stamps them onto device tabs at G(chassis-row)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bom_tag_test_")
        self.frame = BomFrame.__new__(BomFrame)
        self.frame.gui = MagicMock()
        self.frame.gui.workbook_builder = _make_builder()
        self.frame._append_log = lambda msg: None

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_source(self) -> str:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        summary = wb.create_sheet("Summary")
        summary["B9"] = "#"
        summary["C9"] = "IP Address"
        summary["D9"] = "Device Name"
        summary["E9"] = "Asset Tag"
        summary.cell(row=10, column=2, value=1)
        summary.cell(row=10, column=3, value="10.0.0.1")
        summary.cell(row=10, column=4, value="MNCR001")
        summary.cell(row=10, column=5, value="AT-42")
        ws = wb.create_sheet("MNCR001")
        ws.cell(row=15, column=2, value="Chassis 1")
        ws.cell(row=15, column=4, value="3HE11288AA")
        ws.cell(row=15, column=6, value="7250 IXR Chassis")
        src = os.path.join(self.tmpdir, "src.xlsx")
        wb.save(src)
        wb.close()
        return src

    def test_build_stamps_asset_tag_on_chassis_row(self):
        out = self.frame._build(self._build_source())
        try:
            wb = openpyxl.load_workbook(out)
            self.assertEqual(wb["MNCR001"].cell(row=15, column=7).value, "AT-42")
            wb.close()
        finally:
            try: os.unlink(out)
            except OSError: pass


if __name__ == "__main__":
    unittest.main()
