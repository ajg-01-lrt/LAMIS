"""Tests for the ``BOM`` → ``Inventory by Site`` sheet rename.

The aggregate inventory sheet on every ATLAS-built workbook is now
called ``Inventory by Site`` (was ``BOM``). This file pins:

* Fresh builds emit the new name canonically.
* Legacy workbooks with the old ``BOM`` sheet still load — the
  ``find_inventory_sheet_name`` helper falls back gracefully.
* Device-tab ``Return`` hyperlinks point at the new name.

If a future refactor accidentally reintroduces the literal ``"BOM"``
in a write site, the first two tests catch it; the hyperlink test
catches the navigation regression.
"""
from __future__ import annotations
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import openpyxl
import pandas as pd

from gui.workbook_builder import (
    INVENTORY_TAB_NAME,
    WorkbookBuilder,
    _LEGACY_INVENTORY_TAB_NAME,
    find_inventory_sheet_name,
)


class TestFindInventorySheetName(unittest.TestCase):
    """Direct guards on the lookup helper used everywhere a read site
    needs to be backwards-compatible."""

    def test_prefers_new_name_when_both_present(self):
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        wb.create_sheet("Inventory by Site")
        wb.create_sheet("BOM")  # ancient leftover; new name should win
        self.assertEqual(
            find_inventory_sheet_name(wb), "Inventory by Site",
        )

    def test_falls_back_to_legacy_bom(self):
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        wb.create_sheet("BOM")
        self.assertEqual(find_inventory_sheet_name(wb), "BOM")

    def test_returns_none_when_neither_present(self):
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        wb.create_sheet("DeviceA")
        self.assertIsNone(find_inventory_sheet_name(wb))


class TestFreshBuildUsesNewName(unittest.TestCase):
    """End-to-end: ``build_report_workbook`` produces an aggregate
    sheet titled ``Inventory by Site``, NOT ``BOM``."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rename_test_")
        template_path = os.path.join(self.tmp_dir, "tpl.xlsx")
        tpl = openpyxl.Workbook()
        tpl.active.title = "Customer-project"
        tpl.create_sheet("Summary")
        tpl.create_sheet("DeviceTemplate")
        tpl.save(template_path)
        db = MagicMock()
        db.db_path = ":memory:"
        db.lookup_part.return_value = ""
        self.builder = WorkbookBuilder(
            db_cache=db, template_path=template_path,
            packing_slip_template="",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _data(self, name: str) -> dict:
        return {"main": pd.DataFrame([{
            "System Name": name, "System Type": "T",
            "Type": "Shelf", "Information Type": "Shelf",
            "Part Number": "P", "Serial Number": "S",
            "Description": "d", "Name": "Main Shelf", "Source": "LAN",
        }])}

    def test_built_workbook_has_inventory_by_site_not_bom(self):
        out_path = os.path.join(self.tmp_dir, "out.xlsx")
        self.builder.build_report_workbook(
            {"10.0.0.1": self._data("dev-one")},
            out_path,
            customer="C", project="P",
            customer_po="PO", sales_order="SO",
            append_mode=False,
        )
        wb = openpyxl.load_workbook(out_path)
        self.assertIn(INVENTORY_TAB_NAME, wb.sheetnames)
        self.assertNotIn(
            _LEGACY_INVENTORY_TAB_NAME, wb.sheetnames,
            "Fresh builds must use the new sheet name only — no "
            "duplicate legacy 'BOM' tab.",
        )

    def test_device_tab_return_link_points_at_new_name(self):
        # Navigation regression guard — operators click Return from
        # any device tab expecting to land on the aggregate. The
        # hyperlink target was hard-coded to ``BOM`` in five places
        # and all of them now use the constant.
        out_path = os.path.join(self.tmp_dir, "links.xlsx")
        self.builder.build_report_workbook(
            {"10.0.0.1": self._data("dev-one")},
            out_path,
            customer="C", project="P",
            customer_po="PO", sales_order="SO",
            append_mode=False,
        )
        wb = openpyxl.load_workbook(out_path)
        device_tab = next(
            n for n in wb.sheetnames
            if n not in ("Summary", INVENTORY_TAB_NAME, "DeviceTemplate")
            and not n.startswith("Customer")
        )
        a1 = wb[device_tab]["A1"]
        link = a1.hyperlink
        self.assertIsNotNone(link, "A1 must carry a Return hyperlink")
        target = (link.location or link.target or "")
        self.assertIn(INVENTORY_TAB_NAME, target)
        self.assertNotIn(
            f"'{_LEGACY_INVENTORY_TAB_NAME}'", target,
            f"Stale 'BOM' target in {target!r}",
        )


class TestLegacyWorkbookStillLoads(unittest.TestCase):
    """The Asset Import / BoM-rebuild / BoM-compare paths must still
    work on workbooks built before the rename (which have a ``BOM``
    sheet, not ``Inventory by Site``)."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="legacy_compat_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_bom_compare_finds_legacy_bom_sheet(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        wb.create_sheet("BOM")
        sheet = BomCompareFrame._find_bom_sheet(wb, required=True)
        self.assertEqual(sheet.title, "BOM")

    def test_bom_compare_finds_new_name_sheet(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        wb.create_sheet("Inventory by Site")
        sheet = BomCompareFrame._find_bom_sheet(wb, required=True)
        self.assertEqual(sheet.title, "Inventory by Site")

    def test_bom_compare_error_message_mentions_both_names(self):
        from gui.bom_compare_frame import BomCompareFrame
        wb = openpyxl.Workbook()
        wb.active.title = "Summary"
        with self.assertRaises(RuntimeError) as ctx:
            BomCompareFrame._find_bom_sheet(wb, required=True)
        msg = str(ctx.exception)
        self.assertIn("Inventory by Site", msg)
        self.assertIn("BOM", msg)


if __name__ == "__main__":
    unittest.main()
