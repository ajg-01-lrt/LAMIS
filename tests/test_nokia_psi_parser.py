import unittest
from unittest.mock import MagicMock

from scripts.Nokia_PSI import Script


class TestNokiaPSISystemName(unittest.TestCase):
    """The PSI report was missing the System Name because
    'show general system-identification' carries the shelf TYPE but not the
    hostname. The PSI now also runs 'show general name' → extract_system_name.
    """

    def test_extract_system_name_from_show_general_name(self):
        script = Script.__new__(Script)
        script.db_cache = MagicMock()
        # Exact device output the operator pasted.
        output = (
            "show general name\r\n\r\n"
            "System Name: uslgd1-l9i2\r\n\r\n"
            "uslgd1-l9i2#"
        )
        df = Script.extract_system_name(script, output, ip="172.16.0.1")
        self.assertEqual(df.iloc[0]["System Name"], "uslgd1-l9i2")

    def test_command_list_and_pipeline_stay_aligned(self):
        s = Script.__new__(Script)
        self.assertEqual(s.COMMANDS[0], "show general name")
        self.assertEqual(len(s.COMMANDS), len(s._pipeline("1.2.3.4")))


class TestNokiaPSIShelfDetailParser(unittest.TestCase):
    def test_extract_shelf_detail_from_system_identification(self):
        script = Script.__new__(Script)
        output = (
            "ramantest1#  show general system-identification\n\n"
            "Vendor                   : Nokia\n"
            "Product                  : 1830\n"
            "Shelf type               : PSI-4L\n"
            "EC type                  : MEC2L\n"
            "Serial number            : CN2503MAB0D\n"
            "Current OAMP MAC address : 0c:4b:48:2c:52:3d\n\n"
            "ramantest1#\n"
        )

        df = Script.extract_shelf_detail(script, output, ip="10.0.0.1")

        self.assertEqual(df.iloc[0]["System Name"], "ramantest1")
        self.assertEqual(df.iloc[0]["System Type"], "PSI-4L")

    def test_extract_shelf_detail_legacy_show_shelf_1_still_supported(self):
        script = Script.__new__(Script)
        output = (
            "Name : LEGACY-PSI\n"
            "Programmed Type : PSI-8L\n"
        )

        df = Script.extract_shelf_detail(script, output)

        self.assertEqual(df.iloc[0]["System Name"], "LEGACY-PSI")
        self.assertEqual(df.iloc[0]["System Type"], "PSI-8L")


class TestPSIDeviceNameResolution(unittest.TestCase):
    """The workbook read the device name from ``shelf_detail`` and never from
    ``system_name``. When 'show general system-identification' has no hostname
    line, ``extract_shelf_detail`` synthesises "Nokia <product>" — identical on
    every 1830 — so all PSI shelves landed on one "Nokia_1830" tab, each run
    deleting the previous shelf's sheet instead of adding its own.
    """

    def test_generic_product_name_is_not_a_device_identity(self):
        from gui.workbook_builder import _real_name

        # Shared by every 1830 on the network — must not become a tab name.
        self.assertEqual(_real_name("Nokia 1830"), "")
        self.assertEqual(_real_name("nokia 1830"), "")
        # Parser failure sentinels are not identities either.
        self.assertEqual(_real_name("Unknown"), "")
        self.assertEqual(_real_name("Error"), "")
        self.assertEqual(_real_name(""), "")

    def test_real_tids_survive(self):
        from gui.workbook_builder import _real_name

        self.assertEqual(_real_name("uslgd1-l9i2"), "uslgd1-l9i2")
        self.assertEqual(_real_name("  ramantest1  "), "ramantest1")
        # A hostname that merely starts with the vendor name is still a TID.
        self.assertEqual(_real_name("Nokia 1830 East"), "Nokia 1830 East")
        self.assertEqual(_real_name("nokia-1830-a"), "nokia-1830-a")

    def test_builder_reads_the_system_name_dataframe(self):
        # The PSI name-resolution loop must consult 'system_name' (the
        # 'show general name' TID) before shelf_detail's product fallback.
        import inspect

        from gui.workbook_builder import WorkbookBuilder

        src = inspect.getsource(WorkbookBuilder.build_psi_report_workbook)
        keys = src[src.index('for key in ('):]
        keys = keys[:keys.index(')')]
        self.assertIn('"system_name"', keys)
        self.assertLess(
            keys.index('"system_name"'), keys.index('"shelf_detail"'),
            "system_name must be consulted before shelf_detail",
        )


if __name__ == "__main__":
    unittest.main()
