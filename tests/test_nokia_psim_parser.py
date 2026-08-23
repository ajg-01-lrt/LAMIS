"""Unit tests for the Nokia 1830 PSIM inventory parsers."""
import unittest
from unittest.mock import MagicMock

import pandas as pd

from scripts.Nokia_PSIM import Script as PSIMScript


def _make_script():
    s = PSIMScript.__new__(PSIMScript)
    s.db_cache = MagicMock()
    s.db_cache.lookup_part.return_value = "Test description"
    return s


_GENERAL_DETAIL = """\
ChiM1# show general detail
Name                   : ChiM1
System Description     : Nokia 1830 PSIM v23.12.0 SONET ADM
S/W Version            : 1830PSIM-0.0-12
Current Date           : 2026/03/26 12:29:53 (UTC)
System Up Time         : 12 hours, 19 minutes, 35 seconds
Loopback IPV4 Address  : 10.9.1.1/32
Loopback IPV6 Address  : ::/0
"""

_SHELF_INVENTORY = """\
ChiM1# show shelf inventory *

Shelf  Type      Part Number       Serial Number       CLEI
--------------------------------------------------------------------------------
    1  PSI-M   3KC81791AAHC04      RT261300392        WOMSG00ERD
"""

_CARD_INVENTORY = """\
ChiM1# show card inventory *

Location  Card Type    Mnemonic       Part Number        Serial Number      CLEI
---------------------------------------------------------------------------------------
    1/11  MEC2         MEC2           3KC81775AANC05     RT261014919        WOCUBDTUAE
    1/13  MFC          PSIMMFC        3KC81791AAHC04     RT261300392        WOMSG00ERD
    1/21  FAN          PSIMFAN        3KC81709AAAB02     RT260706123        WOCUBDSUAB
    1/22  FAN          PSIMFAN        3KC81709AAAB02     RT260706124        WOCUBDSUAB
    1/23  FAN          PSIMFAN        3KC81709AAAB02     RT260706125        WOCUBDSUAB
    1/24  FAN          PSIMFAN        3KC81709AAAB02     RT260706126        WOCUBDSUAB
    1/25  FAN          PSIMFAN        3KC81709AAAB02     RT260706127        WOCUBDSUAB
    1/26  PF           PSIMPFAC       1AF30787AA         QB254601194        -
    1/27  PF           PSIMPFAC       1AF30787AA         QB254601227        -
"""

_INTERFACE_INVENTORY = """\
ChiM1# show interface inventory *

     Location   Module Type                             Part Number        Serial Number
--------------------------------------------------------------------------------------------------
"""


class TestPSIMGeneralDetail(unittest.TestCase):
    def test_name_type_and_sw_version_parsed(self):
        s = _make_script()
        df = s.extract_general_detail(_GENERAL_DETAIL, None, ip="10.9.1.1")
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row['System Name'], 'ChiM1')
        self.assertIn('PSIM', row['System Type'])
        self.assertEqual(row['Name'], 'ChiM1')
        self.assertEqual(row['Source'], '10.9.1.1')


class TestPSIMShelfInventory(unittest.TestCase):
    def test_psim_shelf_row_parsed(self):
        s = _make_script()
        df = s.extract_shelf_inventory(_SHELF_INVENTORY, None, ip="10.9.1.1")
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row['System Type'], 'PSI-M')
        self.assertEqual(row['Part Number'], '3KC81791AA')
        self.assertEqual(row['Serial Number'], 'RT261300392')
        self.assertEqual(row['Name'], 'Shelf 1')

    def test_header_lines_skipped(self):
        s = _make_script()
        df = s.extract_shelf_inventory(
            "Shelf  Type      Part Number\n-----------\n",
            None,
            ip="10.9.1.1",
        )
        self.assertTrue(df.empty)


class TestPSIMCardInventory(unittest.TestCase):
    def test_parses_all_card_rows(self):
        s = _make_script()
        df = s.extract_card_inventory(_CARD_INVENTORY, None, ip="10.9.1.1")
        self.assertEqual(len(df), 9)
        # MEC2 row
        mec2 = df[df['Name'] == '1/11'].iloc[0]
        self.assertEqual(mec2['Type'], 'MEC2')
        self.assertEqual(mec2['Part Number'], '3KC81775AA')
        self.assertEqual(mec2['Serial Number'], 'RT261014919')
        # MFC row
        mfc = df[df['Name'] == '1/13'].iloc[0]
        self.assertEqual(mfc['Type'], 'PSIMMFC')
        # Fan rows — 5 of them
        fans = df[df['Type'] == 'PSIMFAN']
        self.assertEqual(len(fans), 5)
        # PF rows — 2 with the "-" CLEI placeholder
        pfs = df[df['Type'] == 'PSIMPFAC']
        self.assertEqual(len(pfs), 2)
        self.assertEqual(pfs.iloc[0]['Part Number'], '1AF30787AA')


class TestPSIMInterfaceInventory(unittest.TestCase):
    def test_empty_inventory_returns_empty_df(self):
        s = _make_script()
        df = s.extract_interface_inventory(_INTERFACE_INVENTORY, None, ip="10.9.1.1")
        self.assertTrue(df.empty)


class TestPSIMValidation(unittest.TestCase):
    def test_is_valid_output_for_each_command(self):
        s = _make_script()
        self.assertTrue(s.is_valid_output(_GENERAL_DETAIL, "show general detail"))
        self.assertTrue(s.is_valid_output(_SHELF_INVENTORY, "show shelf inventory *"))
        self.assertTrue(s.is_valid_output(_CARD_INVENTORY, "show card inventory *"))
        self.assertTrue(s.is_valid_output(_INTERFACE_INVENTORY, "show interface inventory *"))
        self.assertFalse(s.is_valid_output("", "show general detail"))


class TestPSIMCommands(unittest.TestCase):
    def test_four_commands_in_order(self):
        s = _make_script()
        cmds = s.get_commands()
        self.assertEqual(len(cmds), 4)
        self.assertEqual(cmds[0], 'show general detail')
        self.assertEqual(cmds[1], 'show shelf inventory *')
        self.assertEqual(cmds[2], 'show card inventory *')
        self.assertEqual(cmds[3], 'show interface inventory *')


if __name__ == '__main__':
    unittest.main()
