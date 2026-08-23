"""
Synchronous Telnet client that matches the telnetlib.Telnet API used in LAMIS.

Replaces the standard-library `telnetlib` module, which was deprecated in
Python 3.11 and removed in Python 3.13.  All socket I/O is handled here so
the rest of the codebase can simply do::

    from utils.telnet import Telnet
"""

import re
import socket
import time
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# IAC (Interpret As Command) stripping
# ---------------------------------------------------------------------------
# Many Telnet servers send option-negotiation sequences at connection time.
# We respond minimally (WONT/DONT) and strip the sequences from the data
# returned to callers.

_IAC  = 0xFF
_WILL = 0xFB
_WONT = 0xFC
_DO   = 0xFD
_DONT = 0xFE
_SB   = 0xFA  # subnegotiation begin
_SE   = 0xF0  # subnegotiation end


def _process_iac(data: bytes, sock: socket.socket) -> bytes:
    """Strip IAC sequences from *data*, sending WONT/DONT replies as needed."""
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b != _IAC:
            out.append(b)
            i += 1
            continue

        # Need at least one more byte for the command code.
        if i + 1 >= len(data):
            break

        cmd = data[i + 1]

        if cmd == _IAC:
            # Escaped 0xFF → literal byte
            out.append(_IAC)
            i += 2

        elif cmd == _SB:
            # Subnegotiation: skip until IAC SE.
            # Use i < len(data) so the final byte is consumed even when
            # the block is unterminated (no IAC SE before end of buffer).
            i += 2
            while i < len(data):
                if i + 1 < len(data) and data[i] == _IAC and data[i + 1] == _SE:
                    i += 2
                    break
                i += 1

        elif cmd in (_WILL, _WONT, _DO, _DONT):
            # Three-byte sequence: IAC <verb> <option>
            if i + 2 < len(data):
                option = data[i + 2]
                # Reply: WILL → DONT, DO → WONT (we refuse everything)
                if cmd == _WILL:
                    try:
                        sock.sendall(bytes([_IAC, _DONT, option]))
                    except OSError:
                        pass
                elif cmd == _DO:
                    try:
                        sock.sendall(bytes([_IAC, _WONT, option]))
                    except OSError:
                        pass
                i += 3
            else:
                i += 2  # truncated — skip what we have

        else:
            # Unknown two-byte command; skip
            i += 2

    return bytes(out)


# ---------------------------------------------------------------------------
# Telnet client
# ---------------------------------------------------------------------------

class Telnet:
    """Minimal synchronous Telnet client with the same API as telnetlib.Telnet.

    F004: Every connection is gated by ``utils.telnet_policy.enforce_telnet_policy``
    unless the caller passes ``bypass_policy=True`` (used only by code paths
    that *must* speak Telnet, e.g. TL1 against Ciena 6500). On policy refusal
    a ``TelnetPolicyError`` is raised before the socket is opened — callers
    are expected to catch it and treat the device as unreachable (log + skip)
    rather than aborting the run.
    """

    def __init__(self, host: str, port: int = 23, timeout: Optional[float] = None,
                 *, bypass_policy: bool = False, purpose: Optional[str] = None,
                 skip_ssh_probe: bool = False):
        # Imported lazily to avoid a circular import (telnet_policy → helpers
        # → ... → utils.telnet would otherwise loop during package init).
        from utils.telnet_policy import enforce_telnet_policy
        enforce_telnet_policy(host, port, bypass=bypass_policy, purpose=purpose,
                              skip_ssh_probe=skip_ssh_probe)
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._default_timeout = timeout
        self._buf = b""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recv_chunk(self, timeout: float) -> bytes:
        """Read one chunk from the socket with *timeout* seconds, stripping IAC."""
        self._sock.settimeout(max(0.05, timeout))
        try:
            chunk = self._sock.recv(4096)
            return _process_iac(chunk, self._sock) if chunk else b""
        except (socket.timeout, BlockingIOError, OSError):
            return b""

    # ------------------------------------------------------------------
    # Public API (matches telnetlib.Telnet)
    # ------------------------------------------------------------------

    def _resolve_timeout(self, timeout: Optional[float]) -> float:
        """F022: enforce a sane timeout floor so callers can't accidentally
        spin in a non-blocking loop. ``None`` falls back to the per-instance
        default (or 30 s); any positive value is floored at 0.1 s."""
        if timeout is None:
            timeout = self._default_timeout if self._default_timeout else 30
        try:
            t = float(timeout)
        except (TypeError, ValueError):
            t = 30.0
        if t <= 0:
            t = 0.1
        return t

    def read_until(self, match: bytes, timeout: Optional[float] = None) -> bytes:
        """Read until *match* is found or *timeout* expires; return bytes read."""
        deadline = time.monotonic() + self._resolve_timeout(timeout)
        while True:
            idx = self._buf.find(match)
            if idx >= 0:
                end = idx + len(match)
                result, self._buf = self._buf[:end], self._buf[end:]
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result, self._buf = self._buf, b""
                return result
            chunk = self._recv_chunk(remaining)
            if chunk:
                self._buf += chunk

    def write(self, data: bytes) -> None:
        """Send *data* to the remote end."""
        self._sock.sendall(data)

    def read_very_eager(self) -> bytes:
        """Read all immediately available data without blocking."""
        self._sock.settimeout(0.0)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                self._buf += _process_iac(chunk, self._sock)
        except (socket.timeout, BlockingIOError, OSError):
            pass
        result, self._buf = self._buf, b""
        return result

    def expect(
        self,
        patterns: List[bytes],
        timeout: Optional[float] = None,
    ) -> Tuple[int, Optional[re.Match], bytes]:
        """Wait for one of the regex *patterns* and return (index, match, data)."""
        compiled = [(i, re.compile(p)) for i, p in enumerate(patterns)]
        deadline = time.monotonic() + self._resolve_timeout(timeout)
        while True:
            for i, regex in compiled:
                m = regex.search(self._buf)
                if m:
                    result, self._buf = self._buf[: m.end()], self._buf[m.end():]
                    return i, m, result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result, self._buf = self._buf, b""
                return -1, None, result
            chunk = self._recv_chunk(remaining)
            if chunk:
                self._buf += chunk

    def close(self) -> None:
        """Close the underlying socket."""
        try:
            self._sock.close()
        except OSError:
            pass
