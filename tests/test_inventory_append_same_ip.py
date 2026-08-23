"""Regression: build_report_workbook (default-family) append must NOT drop a
prior shelf when the next shelf shares the same service IP but has a DISTINCT
hostname.

Field bug (2026-07-13, Oracle DFW-SAT job): the operator inventoried many Nokia
1830/PSS shelves in append mode, each cabled to the SAME direct-connect service
IP 172.16.0.1 (HSTQTX02-1-S1, HSTQTX02-2-S1, MTGMTX01-1-S1, ...). Only the LAST
shelf survived in the workbook. Root cause: the builder's IP-fallback treated
"same IP + hostname miss + exactly one prior sheet at that IP" as a rescan and
DELETED the prior tab. That is correct only for a truly anonymous device; a
distinct real hostname at the same service IP is a DIFFERENT shelf. Fix gates the
IP-fallback on the device having no reported hostname AND no chassis serial.
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

_TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "data", "Device_Report_Template.xlsx"
)


class TestInventoryAppendSameIpDistinctHostnames(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inv_append_")
        db = MagicMock()
        db.db_path = ":memory:"
        db.lookup_part.return_value = "cached-desc"   # skip the sqlite fallback path
        self.builder = WorkbookBuilder(
            db_cache=db, template_path=_TEMPLATE, packing_slip_template="")
        self.out = os.path.join(self.tmp, "out.xlsx")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _dev(name, serial, source="172.16.0.1"):
        # The Nokia_1830 script writes the IP into the "Source" column, which the
        # builder stores in F5 and the append-read uses as the device's IP key —
        # so it MUST be populated for the same-IP collision path to be exercised.
        df = pd.DataFrame([
            {"System Name": name, "Type": "Shelf", "Part Number": "PSS-8",
             "Serial Number": serial, "Description": "chassis", "Source": source},
            {"System Name": name, "Type": "Card", "Part Number": "CARD1",
             "Serial Number": "C1-" + serial, "Description": "card", "Source": source},
        ])
        return {"combined": {"DataFrame": df}}

    def _device_tabs(self):
        wb = openpyxl.load_workbook(self.out)
        return sorted(
            n for n in wb.sheetnames if n not in ("Summary", "Inventory by Site")
        )

    def test_distinct_hostnames_same_ip_all_preserved(self):
        self.builder.build_report_workbook(
            {"172.16.0.1": self._dev("HSTQTX02-1-S1", "SER1")}, self.out,
            customer="Oracle", project="DFW", append_mode=False)
        self.assertEqual(self._device_tabs(), ["HSTQTX02_1_S1"])

        for host, ser in [("HSTQTX02-2-S1", "SER2"), ("MTGMTX01-1-S1", "SER3")]:
            self.builder.build_report_workbook(
                {"172.16.0.1": self._dev(host, ser)}, self.out,
                customer="Oracle", project="DFW", append_mode=True)

        tabs = self._device_tabs()
        self.assertEqual(
            tabs, ["HSTQTX02_1_S1", "HSTQTX02_2_S1", "MTGMTX01_1_S1"],
            f"all three distinct shelves at the same IP must survive; got {tabs!r}",
        )

    def test_same_hostname_same_ip_replaces_in_place(self):
        # Genuine rescan (identical hostname) must still de-dupe to one tab —
        # the fix must not turn every re-scan into a duplicate.
        self.builder.build_report_workbook(
            {"172.16.0.1": self._dev("HSTQTX02-1-S1", "SER1")}, self.out,
            customer="Oracle", project="DFW", append_mode=False)
        self.builder.build_report_workbook(
            {"172.16.0.1": self._dev("HSTQTX02-1-S1", "SER1")}, self.out,
            customer="Oracle", project="DFW", append_mode=True)
        tabs = self._device_tabs()
        self.assertEqual(
            len(tabs), 1,
            f"a re-scan of the same hostname must replace in place, not "
            f"duplicate; got {tabs!r}",
        )


if __name__ == "__main__":
    unittest.main()
