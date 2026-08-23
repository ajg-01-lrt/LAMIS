"""Pin the wake-byte sequence used by the serial probe + login.

Field bug this guards: an RLS R4 chassis kept echoing our wake-CR
(``hex=0D``) back through the serial probe but never rendering a
prompt -- even with the timeout bumped to 10 seconds. Diagnosis: some
Ciena CLI engines treat a bare ``\\r`` as "carriage return, no commit
yet" and only render the prompt when they see the LF that follows
(a real ``ENTER`` keypress is ``\\r\\n`` from a terminal emulator).

Both ``open_serial_with_baud_probe`` and ``serial_login`` were
sending bare ``\\r`` -- fixed to send ``\\r\\n``, matching what
SecureCRT / PuTTY send for an ENTER keypress.

Source-level pins so a refactor can't silently revert.
"""
from __future__ import annotations
import inspect
import unittest


class TestProbeSendsCrLfNotBareCr(unittest.TestCase):
    def setUp(self):
        from utils import serial_helpers
        self.probe_src = inspect.getsource(
            serial_helpers.open_serial_with_baud_probe
        )
        self.login_src = inspect.getsource(serial_helpers.serial_login)

    def test_probe_writes_crlf(self):
        # The probe must send ``\r\n`` -- the full ENTER sequence.
        self.assertIn(
            'ser.write(b"\\r\\n")', self.probe_src,
            "probe wake byte must be \\r\\n; bare \\r is echoed but"
            " doesn't render the prompt on some Ciena RLS firmware.",
        )

    def test_probe_does_not_write_bare_cr(self):
        # Catch a refactor that strips the LF -- look for any line in
        # the probe that calls ser.write with bare ``b"\r"`` (not
        # followed by ``\n``).
        for line in self.probe_src.splitlines():
            stripped = line.strip()
            if "ser.write(" not in stripped:
                continue
            # Comment lines explaining the BUG history may legitimately
            # mention ``\r`` alone; skip comments.
            if stripped.startswith("#"):
                continue
            # The actual write line must NOT be ser.write(b"\r")
            self.assertNotIn(
                'ser.write(b"\\r")', stripped,
                f"probe contains bare-CR ser.write -- regresses RLS"
                f" R4 prompt-wake fix:\n  {line!r}",
            )

    def test_login_writes_crlf(self):
        # ``serial_login`` shares the same wake-byte logic and must
        # also send the LF.
        self.assertIn(
            'ser.write(b"\\r\\n")', self.login_src,
            "serial_login wake byte must be \\r\\n; bare \\r blocks"
            " prompt rendering on Ciena RLS.",
        )


if __name__ == "__main__":
    unittest.main()
