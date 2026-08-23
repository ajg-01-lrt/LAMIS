"""Unit tests for utils/telnet.py — _process_iac IAC parsing."""

import unittest

from utils.telnet import _process_iac

_IAC  = 0xFF
_WILL = 0xFB
_WONT = 0xFC
_DO   = 0xFD
_DONT = 0xFE
_SB   = 0xFA  # subnegotiation begin
_SE   = 0xF0  # subnegotiation end


class _RecordSock:
    """Records bytes sent via sendall; never raises."""
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


class _ErrorSock:
    """Always raises OSError on sendall, simulating a broken connection."""
    def sendall(self, data: bytes) -> None:
        raise OSError("simulated send error")


class TestProcessIACBasic(unittest.TestCase):

    def _run(self, data: bytes, sock=None) -> bytes:
        return _process_iac(data, sock or _RecordSock())

    def test_empty_input(self):
        self.assertEqual(self._run(b""), b"")

    def test_plain_ascii_passes_through(self):
        data = b"Login: admin\r\n"
        self.assertEqual(self._run(data), data)

    def test_all_non_iac_bytes_pass_through(self):
        # Bytes 0x00–0xFE should all pass through unchanged.
        data = bytes(range(0, 0xFF))
        self.assertEqual(self._run(data), data)


class TestProcessIACEscape(unittest.TestCase):

    def _run(self, data: bytes, sock=None) -> bytes:
        return _process_iac(data, sock or _RecordSock())

    def test_iac_iac_produces_literal_0xff(self):
        self.assertEqual(self._run(bytes([_IAC, _IAC])), bytes([0xFF]))

    def test_iac_iac_embedded_in_stream(self):
        data = b"AB" + bytes([_IAC, _IAC]) + b"CD"
        self.assertEqual(self._run(data), b"AB\xffCD")

    def test_consecutive_iac_iac_pairs(self):
        data = bytes([_IAC, _IAC, _IAC, _IAC])
        self.assertEqual(self._run(data), bytes([0xFF, 0xFF]))


class TestProcessIACNegotiation(unittest.TestCase):

    def test_will_option_stripped_and_dont_sent(self):
        option = 0x01  # ECHO
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _WILL, option]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(bytes(sock.sent), bytes([_IAC, _DONT, option]))

    def test_do_option_stripped_and_wont_sent(self):
        option = 0x03  # SGA
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _DO, option]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(bytes(sock.sent), bytes([_IAC, _WONT, option]))

    def test_wont_option_stripped_no_reply(self):
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _WONT, 0x01]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)

    def test_dont_option_stripped_no_reply(self):
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _DONT, 0x01]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)

    def test_will_with_surrounding_data(self):
        option = 0x01
        sock = _RecordSock()
        data = b"AB" + bytes([_IAC, _WILL, option]) + b"CD"
        result = _process_iac(data, sock)
        self.assertEqual(result, b"ABCD")
        self.assertEqual(bytes(sock.sent), bytes([_IAC, _DONT, option]))

    def test_all_four_verbs_in_one_packet(self):
        sock = _RecordSock()
        data = bytes([
            _IAC, _WILL, 0x01,   # → DONT reply
            _IAC, _WONT, 0x02,   # no reply
            _IAC, _DO,   0x03,   # → WONT reply
            _IAC, _DONT, 0x04,   # no reply
        ])
        result = _process_iac(data, sock)
        self.assertEqual(result, b"")
        self.assertEqual(bytes(sock.sent), bytes([
            _IAC, _DONT, 0x01,
            _IAC, _WONT, 0x03,
        ]))

    def test_different_option_bytes_round_tripped(self):
        for option in (0x00, 0x01, 0x03, 0x18, 0xFF):
            with self.subTest(option=option):
                sock = _RecordSock()
                _process_iac(bytes([_IAC, _WILL, option]), sock)
                self.assertEqual(bytes(sock.sent), bytes([_IAC, _DONT, option]))


class TestProcessIACSubnegotiation(unittest.TestCase):

    def _run(self, data: bytes) -> bytes:
        return _process_iac(data, _RecordSock())

    def test_subneg_stripped(self):
        data = bytes([_IAC, _SB, 0x01, 0x02, 0x03, _IAC, _SE])
        self.assertEqual(self._run(data), b"")

    def test_subneg_with_surrounding_data(self):
        data = b"XY" + bytes([_IAC, _SB, 0xAA, 0xBB, _IAC, _SE]) + b"ZW"
        self.assertEqual(self._run(data), b"XYZW")

    def test_subneg_empty_payload(self):
        data = bytes([_IAC, _SB, _IAC, _SE])
        self.assertEqual(self._run(data), b"")

    def test_subneg_unterminated_consumes_to_end_of_buffer(self):
        # No IAC SE terminator — all remaining bytes consumed, nothing emitted.
        data = bytes([_IAC, _SB, 0x01, 0x02, 0x03])
        self.assertEqual(self._run(data), b"")

    def test_subneg_then_normal_data(self):
        data = bytes([_IAC, _SB, 0x18, 0x00, _IAC, _SE]) + b"prompt> "
        self.assertEqual(self._run(data), b"prompt> ")


class TestProcessIACTruncated(unittest.TestCase):

    def _run(self, data: bytes, sock=None) -> bytes:
        return _process_iac(data, sock or _RecordSock())

    def test_iac_at_end_of_buffer_dropped(self):
        # IAC with no following byte is silently dropped.
        self.assertEqual(self._run(b"A" + bytes([_IAC])), b"A")

    def test_lone_iac_dropped(self):
        self.assertEqual(self._run(bytes([_IAC])), b"")

    def test_truncated_will_no_option_byte(self):
        # IAC WILL without the option byte — skip 2, no reply sent.
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _WILL]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)

    def test_truncated_do_no_option_byte(self):
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _DO]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)

    def test_truncated_wont_no_option_byte(self):
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _WONT]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)

    def test_truncated_dont_no_option_byte(self):
        sock = _RecordSock()
        result = _process_iac(bytes([_IAC, _DONT]), sock)
        self.assertEqual(result, b"")
        self.assertEqual(len(sock.sent), 0)


class TestProcessIACUnknownCommand(unittest.TestCase):

    def _run(self, data: bytes) -> bytes:
        return _process_iac(data, _RecordSock())

    def test_unknown_two_byte_cmd_skipped(self):
        # 0xF1 is not WILL/WONT/DO/DONT/SB/IAC
        self.assertEqual(self._run(bytes([_IAC, 0xF1])), b"")

    def test_unknown_cmd_with_surrounding_data(self):
        data = b"A" + bytes([_IAC, 0xF1]) + b"B"
        self.assertEqual(self._run(data), b"AB")

    def test_ga_command_skipped(self):
        # IAC GA (0xF9) is a common two-byte sequence
        self.assertEqual(self._run(bytes([_IAC, 0xF9])), b"")


class TestProcessIACSocketErrors(unittest.TestCase):

    def test_will_oserror_swallowed(self):
        data = bytes([_IAC, _WILL, 0x01])
        try:
            result = _process_iac(data, _ErrorSock())
        except OSError:
            self.fail("_process_iac let OSError from sendall escape")
        self.assertEqual(result, b"")

    def test_do_oserror_swallowed(self):
        data = bytes([_IAC, _DO, 0x01])
        try:
            result = _process_iac(data, _ErrorSock())
        except OSError:
            self.fail("_process_iac let OSError from sendall escape")
        self.assertEqual(result, b"")

    def test_processing_continues_after_send_error(self):
        # Data after the failed negotiation should still be returned.
        sock = _ErrorSock()
        data = bytes([_IAC, _WILL, 0x01]) + b"rest"
        result = _process_iac(data, sock)
        self.assertEqual(result, b"rest")


class TestProcessIACMixed(unittest.TestCase):

    def test_login_banner_with_negotiations(self):
        sock = _RecordSock()
        data = (
            b"Login: "
            + bytes([_IAC, _DO, 0x01])   # DO ECHO — stripped, WONT reply sent
            + b"admin"
            + bytes([_IAC, _IAC])        # escaped 0xFF → literal byte
            + b"\n"
        )
        result = _process_iac(data, sock)
        self.assertEqual(result, b"Login: admin\xff\n")
        self.assertEqual(bytes(sock.sent), bytes([_IAC, _WONT, 0x01]))

    def test_subneg_then_data_then_negotiation(self):
        sock = _RecordSock()
        data = (
            bytes([_IAC, _SB, 0x01, _IAC, _SE])   # subneg — stripped
            + b"prompt> "
            + bytes([_IAC, _WILL, 0x03])           # WILL SGA — DONT reply
        )
        result = _process_iac(data, sock)
        self.assertEqual(result, b"prompt> ")
        self.assertEqual(bytes(sock.sent), bytes([_IAC, _DONT, 0x03]))

    def test_multiple_options_interleaved_with_text(self):
        sock = _RecordSock()
        data = (
            bytes([_IAC, _WILL, 0x01])
            + b"hello"
            + bytes([_IAC, _IAC])
            + b"world"
            + bytes([_IAC, _DO, 0x18])
        )
        result = _process_iac(data, sock)
        self.assertEqual(result, b"hello\xffworld")
        self.assertEqual(bytes(sock.sent), bytes([
            _IAC, _DONT, 0x01,
            _IAC, _WONT, 0x18,
        ]))


if __name__ == "__main__":
    unittest.main()
