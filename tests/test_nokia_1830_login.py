"""Regression tests for the Nokia 1830-family login driver.

Field ground truth (operator transcript, PSI at 172.16.0.1, 2026-07-17):

    MEC2-81-2 login: cli
    Username: admin
    Password:
    Last Login: Mon Sep 15 19:21:54 2025 from 127.0.0.1
    Alarm Status:  Critical-4   Major-4   Minor-5 Warning-0
    uslgd1-l9i2#

The login is a getty three-stage: ``login:`` -> ``cli``, ``Username:`` ->
``admin``, ``Password:`` -> ``admin``, then banner + shell prompt. ``cli`` is
typed at the getty prompt, NOT used as an SSH username. On the PSI this runs
over **Telnet** (SSH-auth as admin dead-ends at the banner); the same dialog is
driven on the SSH channel by :meth:`_settle_shell` when a shelf presents it
there. These tests exercise :meth:`_settle_shell`; the ``_ScriptedChannel``
transitions mirror the transcript.
"""
from __future__ import annotations

import unittest

from scripts.Nokia_1830 import Script


BANNER = (
    "\r\nLast Login: Mon Sep 15 19:21:54 2025 from 127.0.0.1\r\n\r\n\r\n\r\n"
    "Alarm Status:  Critical-4   Major-4   Minor-5 Warning-0\r\n"
)
PROMPT = "uslgd1-l9i2# "


def _make():
    s = Script.__new__(Script)
    s.stop_callback = None
    s.username = "admin"   # inner_username / inner_password derive from these
    s.password = "admin"
    return s


class _ScriptedChannel:
    """Fake paramiko invoke_shell channel: ``initial`` bytes are available
    immediately; each ``send()`` releases the next queued ``responses`` entry
    (the shelf answering a prompt)."""

    def __init__(self, initial="", responses=None):
        self._buf = bytearray(initial.encode())
        self._responses = list(responses or [])
        self.sent = []

    def recv_ready(self):
        return len(self._buf) > 0

    def recv(self, n):
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def send(self, data):
        self.sent.append(data)
        if self._responses:
            self._buf.extend(self._responses.pop(0).encode())
        return len(data)


class TestSettleShell(unittest.TestCase):
    def test_getty_three_stage_login(self):
        """login: -> cli, Username: -> admin, Password: -> admin -> shell."""
        ch = _ScriptedChannel(
            initial="MEC2-81-2 login: ",
            responses=["\r\nUsername: ", "\r\nPassword: ", BANNER + PROMPT],
        )
        ok, post = _make()._settle_shell(ch)
        self.assertTrue(ok, f"expected shell prompt; tail={post[-120:]!r}")
        self.assertEqual(ch.sent, ["cli\n", "admin\n", "admin\n"])

    def test_inner_username_password_without_getty(self):
        """Shelf that jumps straight to the inner Username:/Password:."""
        ch = _ScriptedChannel(
            initial="\r\nUsername: ",
            responses=["\r\nPassword: ", BANNER + PROMPT],
        )
        ok, post = _make()._settle_shell(ch)
        self.assertTrue(ok)
        self.assertEqual(ch.sent, ["admin\n", "admin\n"])

    def test_last_login_banner_not_mistaken_for_login_prompt(self):
        """The banner's 'Last Login:' line must not be answered as a getty
        'login:' — a shelf already at the prompt just returns True."""
        ch = _ScriptedChannel(initial=BANNER + PROMPT)
        ok, post = _make()._settle_shell(ch)
        self.assertTrue(ok)
        self.assertEqual(ch.sent, [])

    def test_silent_banner_does_not_falsely_succeed(self):
        """A shelf that shows only the banner and then goes silent (the SSH
        dead-end that pushed PSI onto Telnet) fails cleanly."""
        ch = _ScriptedChannel(initial=BANNER)
        ok, post = _make()._settle_shell(ch)
        self.assertFalse(ok)
        self.assertIn("Alarm Status", post)


class _FakeSerial:
    """Fake pyserial port: each write() releases the next queued response,
    modelling the console answering a prompt."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._pending = bytearray()
        self.written = []

    @property
    def in_waiting(self):
        return len(self._pending)

    def read(self, n):
        chunk = bytes(self._pending[:n])
        del self._pending[:n]
        return chunk

    def write(self, data):
        self.written.append(bytes(data))
        if self._responses:
            self._pending.extend(self._responses.pop(0).encode())
        return len(data)

    def reset_input_buffer(self):
        self._pending.clear()


class TestSerialGettyLogin(unittest.TestCase):
    def test_getty_three_stage_over_serial(self):
        """login: -> cli, Username: -> admin, Password: -> admin -> shell,
        driven over a serial console."""
        from utils.serial_helpers import serial_getty_login
        ser = _FakeSerial([
            "MEC2-81-2 login: ",        # response to wake CRLF
            "\r\nUsername: ",           # response to 'cli'
            "\r\nPassword: ",           # response to 'admin' (username)
            BANNER + PROMPT,            # response to 'admin' (password)
        ])
        ok = serial_getty_login(
            ser, login_name="cli", shell_user="admin", shell_pass="admin",
            timeout=3.0,
        )
        self.assertTrue(ok)
        # Wake, then the three getty answers.
        self.assertEqual(ser.written, [b"\r\n", b"cli\r", b"admin\r", b"admin\r"])


class TestLanRouting(unittest.TestCase):
    def test_psi_lan_uses_telnet(self):
        """LAN PSI must route to Telnet (the getty two-step); SSH-as-admin
        dead-ends on these shelves."""
        from gui.gui4_0 import InventoryGUI
        self.assertEqual(InventoryGUI._lan_connection_types.get("Nokia PSI"), "telnet")

    def test_psi_available_in_serial(self):
        from gui.gui4_0 import InventoryGUI
        self.assertIn("Nokia PSI", InventoryGUI._allowed_serial_scripts)


if __name__ == "__main__":
    unittest.main()
