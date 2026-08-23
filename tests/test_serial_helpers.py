"""Tests for utils/serial_helpers.py.

Covers:
* :func:`open_serial_with_baud_probe` — should iterate through candidate
  bauds, return the first that yields a recognisable prompt, and close
  the failed attempts along the way.
* The shared regex set (``_PROMPT_RE`` etc.) — quick sanity tests for
  RLS-style prompts since the RLS serial path depends on them.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from utils.serial_helpers import (
    _LOGIN_RE,
    _PROMPT_RE,
    _SHELL_RE,
    open_serial_with_baud_probe,
)


class _FakeSerial:
    """Just enough of pyserial's ``Serial`` API to drive the helpers.

    Each instance is preloaded with a sequence of bytes that gets fed to
    callers via ``in_waiting`` + ``read``. The fixture sets ``ok=True``
    on bauds whose response matches a prompt; the others stay silent so
    the probe rejects them.
    """

    def __init__(self, port, baud, timeout=2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._buffer = bytearray()
        # The class-level fixture map controls which (port,baud) pairs
        # are "good" — see set_response().
        response = _FakeSerial._RESPONSES.get((port, baud), b"")
        self._buffer.extend(response)
        self.closed = False
        self.written = bytearray()

    # -- pyserial-ish API --
    @property
    def in_waiting(self) -> int:
        return 0 if self.closed else len(self._buffer)

    def read(self, n: int) -> bytes:
        if self.closed:
            return b""
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        # Used by the probe before write — leave _buffer alone so the
        # pre-seeded response can still be consumed afterwards. (Real
        # devices would have nothing in_waiting until after we sent CR.)
        pass

    def close(self) -> None:
        self.closed = True

    # -- test plumbing --
    _RESPONSES: dict = {}

    @classmethod
    def set_response(cls, port: str, baud: int, payload: bytes) -> None:
        cls._RESPONSES[(port, baud)] = payload

    @classmethod
    def reset_responses(cls) -> None:
        cls._RESPONSES = {}


class TestOpenSerialWithBaudProbe(unittest.TestCase):
    """The probe walks candidate bauds and stops on the first that
    responds with anything matching ``_PROMPT_RE``."""

    def setUp(self):
        _FakeSerial.reset_responses()
        # Patch pyserial. The helper imports ``serial`` lazily so we
        # install our fake into sys.modules before each call.
        self._fake_serial_mod = type(sys)("serial")
        self._fake_serial_mod.Serial = _FakeSerial
        self._patcher = patch.dict(sys.modules, {"serial": self._fake_serial_mod})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_first_baud_gives_login_prompt_returns_that_baud(self):
        _FakeSerial.set_response("COM3", 9600, b"\r\nLogin: ")
        ser = open_serial_with_baud_probe("COM3", [9600, 115200])
        self.assertIsNotNone(ser)
        self.assertEqual(ser.baud, 9600)
        self.assertFalse(ser.closed)

    def test_second_baud_wins_when_first_is_silent(self):
        # 9600 yields nothing; 115200 returns a shell prompt.
        _FakeSerial.set_response("COM3", 115200, b"\r\nlrt2.bb#")
        ser = open_serial_with_baud_probe(
            "COM3", [9600, 115200], timeout=0.3
        )
        self.assertIsNotNone(ser)
        self.assertEqual(ser.baud, 115200)

    def test_all_silent_returns_none(self):
        ser = open_serial_with_baud_probe(
            "COM3", [9600, 115200], timeout=0.2
        )
        self.assertIsNone(ser)

    def test_garbage_response_is_rejected(self):
        # High-bit bytes (typical of wrong baud) should NOT match any of
        # the prompt patterns and the probe should keep walking.
        _FakeSerial.set_response("COM3", 9600, b"\xff\xfa\xc3\x80garbage")
        _FakeSerial.set_response("COM3", 115200, b"host> ")
        ser = open_serial_with_baud_probe(
            "COM3", [9600, 115200], timeout=0.3
        )
        self.assertIsNotNone(ser)
        self.assertEqual(ser.baud, 115200)

    def test_should_stop_short_circuits_the_loop(self):
        called = {"n": 0}
        def stop():
            called["n"] += 1
            return True
        ser = open_serial_with_baud_probe(
            "COM3", [9600, 115200, 38400], should_stop=stop
        )
        self.assertIsNone(ser)
        # Stops before opening the first port.
        self.assertEqual(called["n"], 1)


class TestRlsPromptRegex(unittest.TestCase):
    """Ciena RLS prompts include dot-separated FQDN-style hostnames
    (``lrt2.bb.net.apple.com#``). The shared regex must match them."""

    def test_rls_shell_prompt_matches(self):
        self.assertIsNotNone(
            _PROMPT_RE.search(b"lrt2.bb.net.apple.com#")
        )
        self.assertIsNotNone(
            _SHELL_RE.search(b"lrt2.bb.net.apple.com#")
        )

    def test_rls_short_hostname_matches(self):
        self.assertIsNotNone(_PROMPT_RE.search(b"RLS-9100>"))
        self.assertIsNotNone(_SHELL_RE.search(b"RLS-9100>"))

    def test_login_prompt_still_matches(self):
        self.assertIsNotNone(_PROMPT_RE.search(b"Login: "))
        self.assertIsNotNone(_LOGIN_RE.search(b"Login: "))


class TestCienaRlsScriptAcceptsSerial(unittest.TestCase):
    """Source-level guard: Ciena_RLS.Script must accept ``connection_type
    ='serial'`` and route it to ``execute_serial_commands``."""

    def test_init_accepts_serial_kwargs(self):
        import inspect
        from scripts.Ciena_RLS import Script
        sig = inspect.signature(Script.__init__)
        self.assertIn("serial_port", sig.parameters)
        self.assertIn("baud_rate", sig.parameters)

    def test_execute_commands_routes_serial(self):
        import inspect
        from scripts.Ciena_RLS import Script
        src = inspect.getsource(Script.execute_commands)
        self.assertIn("self.connection_type == 'serial'", src)
        self.assertIn("execute_serial_commands", src)

    def test_serial_method_uses_baud_probe_and_login(self):
        import inspect
        from scripts.Ciena_RLS import Script
        src = inspect.getsource(Script.execute_serial_commands)
        self.assertIn("open_serial_with_baud_probe", src)
        self.assertIn("serial_login", src)
        self.assertIn("capture_until_prompt", src)
        # The user-specified factory creds must be tried before falling
        # back to the encrypted seed pool.
        self.assertIn("Ciena123", src)


if __name__ == "__main__":
    unittest.main()
