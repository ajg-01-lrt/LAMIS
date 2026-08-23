"""Regression: ``build_psi_report_workbook`` must accumulate the Summary
table across append-mode runs.

Bug from the 2026-06-08 12:24 workbook the operator sent:
``A13_UQO4-CLT4-CLT2-XQM1_Inventory_2026-06-08_12-24.xlsx`` had three
device tabs (usctl2, usclt4, usuqo4 — three distinct shelves scanned
back-to-back over LAN) but the Summary sheet listed only the first
device and ``F7=1``. Each append run was silently dropping the prior
Summary rows.

Root cause: the PSI/RLS builder initialized ``summary_index = {}``
and never restored entries from existing device sheets. The standard
``build_report_workbook`` already did this; ``build_psi_report_workbook``
— which Ciena RLS / Ciena 6500 / Nokia PSI scans route through —
didn't.
"""
from __future__ import annotations
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

import openpyxl
import pandas as pd

from gui.workbook_builder import WorkbookBuilder


def _make_psi_template(tmp_dir: str) -> str:
    """Minimal Nokia_PSI_Report_Template.xlsx: a device-template
    page (which the builder clones for each device) plus a
    pre-existing Summary sheet."""
    path = os.path.join(tmp_dir, "Nokia_PSI_Report_Template.xlsx")
    tpl = openpyxl.Workbook()
    tpl.active.title = "DeviceTemplate"
    tpl.create_sheet("Summary")
    tpl.save(path)
    return path


class TestPsiReportAccumulatesSummaryAcrossAppendRuns(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="psi_append_")
        # Both the report template and the PSI-specific template live
        # alongside each other in the real install — point the builder
        # at the same file for both so the test doesn't need a
        # separate scaffold for each.
        template = _make_psi_template(self.tmp_dir)
        db = MagicMock()
        db.db_path = ":memory:"
        db.lookup_part.return_value = ""
        self.builder = WorkbookBuilder(
            db_cache=db,
            template_path=template,
            packing_slip_template="",
        )
        self.template = template
        self.out_path = os.path.join(self.tmp_dir, "out.xlsx")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _shelf_data(system_name: str, chassis_serial: str) -> dict:
        """Per-IP data_dict shape build_psi_report_workbook expects:
        category → ``{"DataFrame": df}`` wrappers."""
        rows = [{
            "System Name": system_name,
            "System Type": "6500-R4",
            "Type": "Shelf",
            "Information Type": "Shelf",
            "Part Number": "NTK-CHASSIS-XX",
            "Serial Number": chassis_serial,
            "Description": "6500-R4 Chassis",
            "Name": "Shelf 1",
            "Source": "LAN",
        }]
        df = pd.DataFrame(rows)
        return {"shelf_inventory": {"DataFrame": df}}

    def test_three_lan_runs_keep_all_three_summary_rows(self):
        # Run 1: fresh build with the first shelf.
        self.builder.build_psi_report_workbook(
            {"10.0.0.1": self._shelf_data(
                "usctl2-l8r5.bb.net.apple.com", "M9A03562"
            )},
            self.out_path,
            customer="A13", project="UQO4-CLT4",
            customer_po="PO", sales_order="SO",
            append_mode=False,
        )

        # Run 2: append a second, distinct shelf on a different IP.
        self.builder.build_psi_report_workbook(
            {"10.0.0.5": self._shelf_data(
                "usclt4-l8r3.bb.net.apple.com", "M9A12345"
            )},
            self.out_path,
            customer="A13", project="UQO4-CLT4",
            customer_po="PO", sales_order="SO",
            append_mode=True,
        )

        # Run 3: append a third shelf. Real-world IP collision the
        # operator hit — same 10.0.0.5 the second device used. The
        # chassis-serial disambiguation isn't on the hook here; we
        # just need each tab + Summary row preserved across appends.
        self.builder.build_psi_report_workbook(
            {"10.0.0.6": self._shelf_data(
                "usuqo4-l8o1.bb.net.apple.com", "M9A99999"
            )},
            self.out_path,
            customer="A13", project="UQO4-CLT4",
            customer_po="PO", sales_order="SO",
            append_mode=True,
        )

        wb = openpyxl.load_workbook(self.out_path)
        # Three device tabs survived (this part was already working).
        device_tabs = sorted(
            n for n in wb.sheetnames
            if n not in ("Summary", "Inventory by Site")
            and not n.startswith("DeviceTemplate")
        )
        self.assertEqual(
            len(device_tabs), 3,
            f"All three device tabs should survive; got {device_tabs!r}",
        )

        # Summary must list ALL three devices — the bug was that this
        # collapsed to just the last run's device, with F7=1.
        summary = wb["Summary"]
        d_col = [summary.cell(r, 4).value for r in range(10, 14)]
        non_empty = [v for v in d_col if v]
        self.assertEqual(
            len(non_empty), 3,
            f"Summary D column should list all three devices; got "
            f"{d_col!r}",
        )
        joined = " ".join(str(v) for v in non_empty)
        self.assertIn("usctl2", joined)
        self.assertIn("usclt4", joined)
        self.assertIn("usuqo4", joined)
        # F7 reflects the cumulative count, not the last-run count.
        self.assertEqual(summary["F7"].value, 3)

    def test_rescan_same_device_still_replaces_not_duplicates(self):
        """Sanity: the append-mode rebuild must not turn rescans of
        the SAME device into duplicate Summary rows. The pre-fix
        ``summary_index`` was being built from sheets and keyed by
        sheet title, so re-scanning a device whose tab already exists
        re-uses the same key and replaces in-place."""
        # Run 1
        self.builder.build_psi_report_workbook(
            {"10.0.0.1": self._shelf_data(
                "usctl2-l8r5.bb.net.apple.com", "M9A03562"
            )},
            self.out_path,
            customer="A13", project="UQO4-CLT4",
            customer_po="PO", sales_order="SO",
            append_mode=False,
        )
        # Run 2 — same device, fresh data.
        self.builder.build_psi_report_workbook(
            {"10.0.0.1": self._shelf_data(
                "usctl2-l8r5.bb.net.apple.com", "M9A03562"
            )},
            self.out_path,
            customer="A13", project="UQO4-CLT4",
            customer_po="PO", sales_order="SO",
            append_mode=True,
        )
        wb = openpyxl.load_workbook(self.out_path)
        summary = wb["Summary"]
        d_col_values = [
            summary.cell(r, 4).value for r in range(10, 14)
        ]
        non_empty = [v for v in d_col_values if v]
        self.assertEqual(
            len(non_empty), 1,
            f"Rescanning the same device shouldn't create a duplicate "
            f"Summary row; got {d_col_values!r}",
        )
        self.assertEqual(summary["F7"].value, 1)


if __name__ == "__main__":
    unittest.main()
