"""Regression tests for IP-collision handling in Summary writers.

Two related bugs surfaced in the Ciena RLS beta artifacts:

* **Inventory build (Bug 3)** — factory-default Ciena RLS gear all report
  ``10.0.0.1`` over LAN until provisioned. When two devices both keyed
  ``summary_index['10.0.0.1']`` the second silently overwrote the first;
  the workbook ended with two device tabs but only one Summary row
  (``F7=1``).

* **Packing slip build (Bug 2)** — the multi-sheet reader disambiguates
  colliding IPs by appending ``_<sheet_name>`` to the dict key. That
  worked for dict hygiene but the SAME mangled string was being written
  into the Summary IP column, producing rows like ``IP =
  '10.0.0.1_usqas4_l8o1_bb_net_apple_com'``.

These tests pin both behaviours so a future refactor can't quietly
re-collapse colliding devices or re-leak the synthetic key into the IP
column.
"""
from __future__ import annotations
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import openpyxl
import pandas as pd

from gui.workbook_builder import WorkbookBuilder


def _empty_template(tmp_dir: str) -> str:
    """Minimal packing-slip template: Summary + one device template sheet."""
    path = os.path.join(tmp_dir, "ps_template.xlsx")
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary["B14"] = "Sales Order"
    summary["C14"] = "Customer PO"
    summary["D14"] = "Part Number"
    summary["E14"] = "Serial Number"
    summary["F14"] = "Description"
    summary["G14"] = "Asset Tag"
    wb.create_sheet("PS_Template")
    wb.save(path)
    return path


def _make_builder(template_path: str) -> WorkbookBuilder:
    db_cache = MagicMock()
    db_cache.db_path = ":memory:"
    db_cache.lookup_part.return_value = ""
    return WorkbookBuilder(
        db_cache=db_cache,
        template_path="",
        packing_slip_template=template_path,
    )


class TestPackingSlipDisplaysBareIp(unittest.TestCase):
    """Bug 2: when ``display_ip_for_key`` maps a mangled key back to its
    bare IP, the Summary C column shows the IP not the synthetic key."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ps_test_")
        self.template = _empty_template(self.tmp_dir)
        self.builder = _make_builder(self.template)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_df(self, system_name: str, part: str) -> pd.DataFrame:
        return pd.DataFrame([{
            "System Name": system_name,
            "Part Number": part,
            "Serial Number": "SN001",
            "Description": "test part",
        }])

    def test_summary_ip_column_shows_bare_ip_when_key_was_mangled(self):
        # First device gets the bare IP; second device's KEY had to be
        # disambiguated to "10.0.0.1_devB" because they shared the IP.
        processed = {
            "10.0.0.1": self._make_df("devA", "P-A"),
            "10.0.0.1_devB": self._make_df("devB", "P-B"),
        }
        display_ip_for_key = {
            "10.0.0.1": "10.0.0.1",
            "10.0.0.1_devB": "10.0.0.1",
        }
        out = self.builder.build_packing_slip_workbook(
            processed, list(processed.keys()),
            "Cust", "Proj", "PO1", "SO1", self.tmp_dir,
            display_ip_for_key=display_ip_for_key,
        )
        try:
            wb = openpyxl.load_workbook(out)
            ws = wb["Summary"]
            ip_col = [ws.cell(r, 3).value for r in range(10, 12)]
            self.assertEqual(
                ip_col, ["10.0.0.1", "10.0.0.1"],
                f"Summary IP column should show bare IPs, got {ip_col!r}"
            )
            # And F7 reflects 2 distinct devices, not 1.
            self.assertEqual(ws["F7"].value, 2)
        finally:
            if os.path.exists(out):
                os.remove(out)

    def test_summary_falls_back_to_key_when_no_map_given(self):
        """Backwards-compat: callers that don't pass display_ip_for_key
        still get the dict-key-as-IP behaviour (legacy single-device-per-IP
        files don't need the new arg)."""
        processed = {"10.0.0.42": self._make_df("solo", "P-S")}
        out = self.builder.build_packing_slip_workbook(
            processed, list(processed.keys()),
            "Cust", "Proj", "PO1", "SO1", self.tmp_dir,
        )
        try:
            wb = openpyxl.load_workbook(out)
            self.assertEqual(wb["Summary"]["C10"].value, "10.0.0.42")
        finally:
            if os.path.exists(out):
                os.remove(out)


class TestPackingSlipFrameRecordsBareIp(unittest.TestCase):
    """Bug 2 (input side): the multi-sheet device-report reader must
    populate _display_ip_for_key with the bare IP for every device, even
    when it had to mangle the key for uniqueness."""

    def test_display_map_records_bare_ip_after_collision(self):
        from gui.packing_slip_frame import PackingSlipFrame
        # Build a 2-sheet device-report file: both sheets report the
        # same metadata IP (10.0.0.1) at F5 of each sheet.
        tmp_dir = tempfile.mkdtemp(prefix="ps_input_")
        try:
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            for sheet_name in ("dev_alpha", "dev_beta"):
                ws = wb.create_sheet(sheet_name)
                ws["F5"] = "10.0.0.1"
                ws["F7"] = "RLS"
                # PART NUMBER header row at row 14, one part row.
                ws["B14"] = "Sales Order"
                ws["C14"] = "Customer PO"
                ws["D14"] = "PART NUMBER"
                ws["E14"] = "SERIAL NUMBER"
                ws["F14"] = "DESCRIPTION"
                ws["D15"] = "P-1"
                ws["E15"] = f"SN-{sheet_name}"
                ws["F15"] = "test"
            src_path = os.path.join(tmp_dir, "devices.xlsx")
            wb.save(src_path)

            frame = PackingSlipFrame.__new__(PackingSlipFrame)  # bypass __init__
            processed = frame._process_multisheet_device_file(src_path)

            self.assertEqual(len(processed), 2)
            keys = list(processed.keys())
            # First device retains the bare IP, second got disambiguated.
            self.assertEqual(keys[0], "10.0.0.1")
            self.assertTrue(
                keys[1].startswith("10.0.0.1_"),
                f"expected disambiguated key, got {keys[1]!r}",
            )
            # Both map back to the bare source IP.
            self.assertEqual(frame._display_ip_for_key[keys[0]], "10.0.0.1")
            self.assertEqual(frame._display_ip_for_key[keys[1]], "10.0.0.1")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestInventoryKeepsSeparateRowsForSharedIp(unittest.TestCase):
    """Bug 3: ``summary_index`` was keyed by IP; two devices reporting
    ``10.0.0.1`` clobbered each other. After the refactor it's keyed by
    sheet_title (guaranteed unique via ``make_unique_sheet_title``) so
    both devices keep a Summary row and F7 reflects the real count."""

    def test_two_devices_one_ip_both_get_summary_rows(self):
        tmp_dir = tempfile.mkdtemp(prefix="inv_test_")
        try:
            # Inventory-report template: active sheet is the "Customer-project"
            # page (gets removed in non-append mode), followed by Summary,
            # then a device template that copy_sheet clones for each device.
            template_path = os.path.join(tmp_dir, "report_template.xlsx")
            tpl = openpyxl.Workbook()
            tpl.active.title = "Customer-project"
            tpl.create_sheet("Summary")
            tpl.create_sheet("DeviceTemplate")
            tpl.save(template_path)

            db = MagicMock()
            db.db_path = ":memory:"
            db.lookup_part.return_value = ""
            builder = WorkbookBuilder(
                db_cache=db,
                template_path=template_path,
                packing_slip_template="",
            )
            wb_path = os.path.join(tmp_dir, "out.xlsx")

            # Real-world shape: outputs[ip] is Dict[category, DataFrame],
            # combine_and_format_data concatenates the DataFrames. Two
            # distinct collector keys here both represent IP 10.0.0.1 —
            # the bug-3 scenario from the Ciena RLS beta artifacts.
            def _bundle(name: str, part: str) -> dict:
                df = pd.DataFrame([{
                    "System Name": name,
                    "System Type": "6500-R2",
                    "Part Number": part,
                    "Description": "shelf",
                    "Information Type": "Shelf",
                    "Source": "LAN",
                }])
                return {"main": df}

            outputs = {
                "10.0.0.1": _bundle("ciena-alpha", "NTK803DA"),
                "10.0.0.1_dup": _bundle("ciena-beta", "NTK803DA"),
            }
            builder.build_report_workbook(
                outputs, wb_path,
                customer="Cust", project="Proj",
                customer_po="PO", sales_order="SO",
                append_mode=False,
            )
            wb = openpyxl.load_workbook(wb_path)
            ws = wb["Summary"]
            d_col = [ws.cell(r, 4).value for r in range(10, 13)]
            populated = [v for v in d_col if v]
            self.assertEqual(
                len(populated), 2,
                f"two devices sharing an IP should yield two Summary rows, "
                f"got D column = {d_col!r}"
            )
            self.assertEqual(ws["F7"].value, 2)
            # Both device tabs survived.
            device_tabs = [
                n for n in wb.sheetnames
                if n.lower().startswith("ciena")
            ]
            self.assertEqual(len(device_tabs), 2, f"got tabs: {wb.sheetnames}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestSummaryIndexRefactorUnit(unittest.TestCase):
    """Bug 3 unit-level check: confirm the refactor by reading the
    summary_index initialization comment / structure. Tightly couples
    to the implementation so a regression that re-keys by IP is caught
    at import time rather than only via full workbook tests."""

    def test_summary_index_doc_string_documents_sheet_title_key(self):
        import inspect
        import gui.workbook_builder as wb_mod
        src = inspect.getsource(wb_mod)
        # The fix comment should still be present — guards against the
        # refactor being silently reverted.
        self.assertIn(
            "sheet_title -> (ip, system_name)", src,
            "summary_index must remain keyed by sheet_title for "
            "shared-IP devices to keep distinct Summary rows.",
        )

    def test_lan_summary_items_unpack_swapped_order(self):
        """summary_items list comprehension should unpack
        ``for sheet_title, (ip, device_name) in summary_index.items()`` —
        not the old ``for ip, (device_name, sheet_title) ...`` form."""
        import inspect
        import gui.workbook_builder as wb_mod
        src = inspect.getsource(wb_mod)
        self.assertIn(
            "for sheet_title, (ip, device_name) in summary_index.items()",
            src,
        )


if __name__ == "__main__":
    unittest.main()
