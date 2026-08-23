import unittest
from queue import Queue

from script_interface import DeviceIdentifier


class TestDeviceIdentifierParse(unittest.TestCase):
    def test_parse_psi_from_system_identification(self):
        q = Queue()
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

        device_type, device_name = DeviceIdentifier.parse_device_info(output, q)

        messages = []
        while not q.empty():
            messages.append(q.get())

        self.assertEqual(device_type, "psi")
        self.assertEqual(device_name, "ramantest1")
        self.assertTrue(any("PSI shelf type detected: PSI-4L" in msg for msg in messages))

    def test_parse_1830_when_no_psi_shelf_type(self):
        output = (
            "Vendor                   : Nokia\n"
            "Product                  : 1830\n"
            "Shelf type               : PSS-16II\n"
        )

        device_type, _device_name = DeviceIdentifier.parse_device_info(output, Queue())

        self.assertEqual(device_type, "1830")

    def test_parse_psim_from_general_detail(self):
        q = Queue()
        output = (
            "ChiM1# show general detail\n"
            "Name                   : ChiM1\n"
            "System Description     : Nokia 1830 PSIM v23.12.0 SONET ADM\n"
            "S/W Version            : 1830PSIM-0.0-12\n"
            "Current Date           : 2026/03/26 12:29:53 (UTC)\n"
            "Loopback IPV4 Address  : 10.9.1.1/32\n"
            "ChiM1#\n"
        )
        device_type, device_name = DeviceIdentifier.parse_device_info(output, q)
        self.assertEqual(device_type, "psim")
        self.assertEqual(device_name, "ChiM1")

    def test_parse_psim_from_shelf_inventory(self):
        output = (
            "ChiM1# show shelf inventory *\n"
            "Shelf  Type      Part Number       Serial Number       CLEI\n"
            "----------------------------------------------------------\n"
            "    1  PSI-M   3KC81791AAHC04      RT261300392        WOMSG00ERD\n"
        )
        device_type, _ = DeviceIdentifier.parse_device_info(output, Queue())
        self.assertEqual(device_type, "psim")


if __name__ == "__main__":
    unittest.main()
