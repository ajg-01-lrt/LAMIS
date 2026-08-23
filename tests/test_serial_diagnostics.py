"""Tests for the diagnostic logging added to utils/serial_helpers.

Two-tier diagnostic model:

* INFO-level (always on): when a baud probe fails, the captured bytes
  get rendered as a hex+repr line so the operator can tell wrong-baud
  garbage (high-bit chars) from a prompt shape we don't recognize
  WITHOUT raising the global log level.
* DEBUG-level (gated): per-chunk byte transcript through
  ``_read_until`` + state transitions in ``serial_login`` /
  ``capture_until_prompt``. Enabled via
  ``enable_serial_debug(True)`` (or ``config.SERIAL_DEBUG = True``).
"""
from __future__ import annotations
import logging
import re
import sys
import unittest
from unittest.mock import patch

from utils.serial_helpers import (
    _format_byte_dump,
    _read_until,
    enable_serial_debug,
    open_serial_with_baud_probe,
)


class TestFormatByteDump(unittest.TestCase):

    def test_empty_buffer(self):
        self.assertEqual(_format_byte_dump(b""), "len=0 hex=<empty>")

    def test_short_printable(self):
        out = _format_byte_dump(b"Login: ")
        self.assertIn("len=7", out)
        self.assertIn("4C 6F 67 69 6E 3A 20", out)
        self.assertIn("'Login: '", out)

    def test_high_bit_garbage_renders_safely(self):
        # Wrong-baud garbage typically has bytes >= 0x80. The hex side
        # must still show them and the repr must not crash.
        garbage = bytes([0xC3, 0xFF, 0x80, 0xAA])
        out = _format_byte_dump(garbage)
        self.assertIn("len=4", out)
        self.assertIn("C3 FF 80 AA", out)
        # The repr part will have escaped bytes — just confirm we
        # didn't raise.
        self.assertIn("repr=", out)

    def test_long_buffer_capped_with_ellipsis(self):
        long = b"A" * 500
        out = _format_byte_dump(long, max_bytes=50)
        self.assertIn("len=500", out)
        self.assertIn("…", out)
        # Should not embed the full 500 bytes in either side.
        self.assertLess(len(out), 500)


class _FakeSerial:
    """Replays a fixed byte stream through the pyserial API surface
    the helpers touch (in_waiting / read / write / reset_input_buffer
    / close)."""

    def __init__(self, port, baud, timeout=2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._buffer = bytearray(_FakeSerial._RESPONSES.get((port, baud), b""))
        self.closed = False
        self.written = bytearray()

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
        pass

    def close(self) -> None:
        self.closed = True

    _RESPONSES: dict = {}

    @classmethod
    def set_response(cls, port, baud, payload):
        cls._RESPONSES[(port, baud)] = payload

    @classmethod
    def reset_responses(cls):
        cls._RESPONSES = {}


class TestProbeLogsCapturedBytesOnFailure(unittest.TestCase):
    """Today's failing log read ``(got 6 bytes); trying next baud``
    with no clue what those 6 bytes were. The INFO-level hex dump
    must appear on every non-empty failed probe."""

    def setUp(self):
        _FakeSerial.reset_responses()
        fake_serial_mod = type(sys)("serial")
        fake_serial_mod.Serial = _FakeSerial
        self._patcher = patch.dict(sys.modules, {"serial": fake_serial_mod})
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_high_bit_garbage_is_dumped_at_info(self):
        # Six bytes of high-bit junk — looks like the wrong-baud
        # pattern from the operator's 2026-06-08 12:13:58 log line.
        _FakeSerial.set_response("COM11", 9600, b"\xff\xff\xfe\xc3\x80\xaa")
        with self.assertLogs(level="INFO") as cm:
            ser = open_serial_with_baud_probe(
                "COM11", [9600], timeout=0.2,
            )
        self.assertIsNone(ser, "garbage must not be misdetected as a prompt")
        joined = "\n".join(cm.output)
        # The new diagnostic line must appear AND must contain the
        # offending bytes in hex.
        self.assertIn("captured bytes", joined)
        self.assertIn("FF FF FE C3 80 AA", joined)

    def test_empty_buffer_skips_dump(self):
        # Genuine silence (cable not connected / device powered off):
        # log the probe-failure line but DON'T emit an empty dump
        # — len=0 hex=<empty> is just noise.
        _FakeSerial.set_response("COM11", 9600, b"")
        with self.assertLogs(level="INFO") as cm:
            open_serial_with_baud_probe("COM11", [9600], timeout=0.2)
        joined = "\n".join(cm.output)
        self.assertIn("did not yield a prompt", joined)
        self.assertNotIn("captured bytes", joined)

    def test_readable_banner_without_prompt_is_dumped(self):
        # Device emits a banner but never gets to a prompt (e.g.
        # waiting on Enter, or a hardware-level paused state).
        # Dump must be readable so the operator can see the banner.
        _FakeSerial.set_response(
            "COM11", 9600,
            b"\r\n\r\nWaveserver 5 system starting...\r\n",
        )
        with self.assertLogs(level="INFO") as cm:
            open_serial_with_baud_probe("COM11", [9600], timeout=0.2)
        joined = "\n".join(cm.output)
        self.assertIn("captured bytes", joined)
        self.assertIn("Waveserver 5 system starting", joined)


class TestReadUntilDebugTranscript(unittest.TestCase):
    """The per-chunk byte transcript must appear only when the
    ``atlas.serial`` logger is at DEBUG."""

    def setUp(self):
        # Reset the logger to default (NOTSET) between tests so
        # ``enable_serial_debug(True)`` from a prior test doesn't
        # bleed in.
        enable_serial_debug(False)
        self.addCleanup(enable_serial_debug, False)

    def _drive_read(self):
        # Tiny fake-serial-like object since _read_until just polls
        # in_waiting / read.
        class _S:
            def __init__(self):
                self._buf = bytearray(b"hello world\r\nhost# ")
            @property
            def in_waiting(self):
                return len(self._buf)
            def read(self, n):
                d = bytes(self._buf[:n])
                del self._buf[:n]
                return d
            def write(self, _):
                pass
        ser = _S()
        return _read_until(ser, re.compile(rb"host#"), timeout=1.0)

    def test_debug_off_no_transcript(self):
        # Capture at DEBUG so we'd SEE any debug lines if they were
        # emitted — assertion is that the serial logger emits NONE
        # because enable_serial_debug(False) gates them out.
        atlas_serial = logging.getLogger("atlas.serial")
        captured = []

        class _Probe(logging.Handler):
            def emit(self, record):
                if record.name == "atlas.serial":
                    captured.append(record.getMessage())

        h = _Probe(level=logging.DEBUG)
        atlas_serial.addHandler(h)
        try:
            self._drive_read()
        finally:
            atlas_serial.removeHandler(h)
        # With DEBUG off (effective level inherits from root, which
        # tests run at WARNING by default), no atlas.serial DEBUG
        # records should fire.
        debug_lines = [m for m in captured if "[SERIAL][read]" in m]
        self.assertEqual(
            debug_lines, [],
            f"Expected no DEBUG transcript when SERIAL_DEBUG off; "
            f"got {debug_lines!r}",
        )

    def test_debug_on_emits_chunk_lines(self):
        enable_serial_debug(True)
        atlas_serial = logging.getLogger("atlas.serial")
        captured = []

        class _Probe(logging.Handler):
            def emit(self, record):
                if record.name == "atlas.serial":
                    captured.append(record.getMessage())

        h = _Probe(level=logging.DEBUG)
        atlas_serial.addHandler(h)
        try:
            self._drive_read()
        finally:
            atlas_serial.removeHandler(h)
        chunk_lines = [m for m in captured if "[SERIAL][read]" in m]
        self.assertTrue(
            chunk_lines,
            "Expected per-chunk DEBUG lines when SERIAL_DEBUG on",
        )
        # And one of them must include the matched-pattern signal.
        self.assertTrue(
            any("pattern matched" in m for m in chunk_lines),
            f"Expected 'pattern matched' marker; got {chunk_lines!r}",
        )


class TestConfigKnobWired(unittest.TestCase):
    """Source-level guard: main.py must read config.SERIAL_DEBUG on
    startup and call enable_serial_debug. Without this, flipping the
    config flag has no effect on installed clients."""

    def test_config_default_present(self):
        import config
        self.assertTrue(
            hasattr(config, "SERIAL_DEBUG"),
            "config.SERIAL_DEBUG must exist so operators can flip it",
        )
        self.assertFalse(
            config.SERIAL_DEBUG,
            "default must be False — DEBUG-level serial transcript "
            "is opt-in",
        )

    def test_main_applies_serial_debug(self):
        import inspect
        import main
        src = inspect.getsource(main)
        self.assertIn("config.SERIAL_DEBUG", src)
        self.assertIn("enable_serial_debug", src)


if __name__ == "__main__":
    unittest.main()
