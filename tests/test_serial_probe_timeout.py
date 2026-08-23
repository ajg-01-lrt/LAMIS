"""Pin the serial-probe timeout for RLS and Waveserver 5 inventory.

Field bug this guards: a 2-second probe timeout was tight enough that
healthy RLS R4 / WS5 hardware regularly failed to surface a prompt
inside the window. The diagnostic captured-bytes line showed a clean
``\\r`` echo (so cable + baud were correct) followed by silence -- the
device just takes 3-6 seconds to print the prompt after echoing the
wake-CR. Bumped to 10 seconds, matching the upgrade-flow constant.

The cost of an over-generous timeout on a *truly* dead port is at
most one extra wait per attempt; the cost of an under-generous
timeout on a live one is a failed inventory run and an operator
phone call. So we err deliberately on the high side.
"""
from __future__ import annotations
import inspect
import re
import unittest


class TestRlsSerialProbeTimeoutIsTenSeconds(unittest.TestCase):
    def test_rls_inventory_probe_uses_10s_timeout(self):
        from scripts.Ciena_RLS import Script
        src = inspect.getsource(Script)
        # Find the open_serial_with_baud_probe call and verify the
        # timeout= kwarg it carries. Source-level pin -- can't easily
        # exercise the live probe in a unit test.
        m = re.search(
            r"open_serial_with_baud_probe\([^)]*?timeout\s*=\s*([\d.]+)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "couldn't find open_serial_with_baud_probe(... timeout=...) "
            "in Ciena_RLS.Script",
        )
        self.assertEqual(
            float(m.group(1)), 10.0,
            "RLS serial-probe timeout must be 10.0s -- a 2s timeout"
            " fails on healthy RLS R4 hardware that takes 3-6s to"
            " print the prompt after echoing the wake-CR.",
        )


class TestWaveserver5SerialProbeTimeoutIsTenSeconds(unittest.TestCase):
    def test_ws5_inventory_probe_uses_10s_timeout(self):
        from scripts.Ciena_Waveserver5 import Script
        src = inspect.getsource(Script)
        m = re.search(
            r"open_serial_with_baud_probe\([^)]*?timeout\s*=\s*([\d.]+)",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            float(m.group(1)), 10.0,
            "WS5 serial-probe timeout must be 10.0s, matching the"
            " upgrade-flow constant _SERIAL_PROMPT_TIMEOUT.",
        )


if __name__ == "__main__":
    unittest.main()
