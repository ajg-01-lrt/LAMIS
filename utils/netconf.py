"""Minimal read-only NETCONF-over-SSH client for the Nokia 1830 OLS.

Phase 1 of moving the PSI audit off CLI-scraping onto structured YANG. Uses raw
``paramiko`` + the SSH ``netconf`` subsystem (no ``ncclient`` dependency, which
is not shipped with ATLAS) and ``lxml`` for parsing. Implements RFC 6242 framing
for BOTH base:1.0 (``]]>]]>`` end-of-message) and base:1.1 (chunked) so the large
subtrees (``/components``, ``/optical-amplifier`` ...) transfer reliably.

Design goals:
  * read-first -- ``<get>`` / ``<get-config>`` for all data collection; the only
    write is ``edit_config`` for the OAMP OSPF redistribute toggle (replacing the
    fragile telnet getty), always set-then-verify with a caller-owned revert.
  * same reachability model as everything else (TCP 830 to the NE), so it
    composes with the OAMP-LAN / DCN routing used to reach RNEs.
  * Nokia admin/admin auth via ``utils.helpers.nokia_ssh_authenticate`` when
    available, else a plain password fallback.

Typical use::

    with NetconfSession(host, 'admin', 'admin') as nc:
        data = nc.get(subtree='<system xmlns="http://openconfig.net/yang/system"/>')
        hostname = nc.first_text(data, 'hostname')
"""
from __future__ import annotations

import re
import socket
import time
from typing import List, Optional

import paramiko
from lxml import etree

__all__ = ["NetconfError", "NetconfSession"]

_EOM = b"]]>]]>"                       # base:1.0 end-of-message marker
_BASE_10 = "urn:ietf:params:netconf:base:1.0"
_BASE_11 = "urn:ietf:params:netconf:base:1.1"

_CLIENT_HELLO = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><capabilities>'
    f"<capability>{_BASE_10}</capability>"
    f"<capability>{_BASE_11}</capability>"
    "</capabilities></hello>"
)


class NetconfError(RuntimeError):
    """Raised on connect/auth/subsystem/RPC failure."""


# ─────────────────────────────────────────────────────────────────────────────
# RFC 6242 framing (pure functions -- unit-testable without a socket)
# ─────────────────────────────────────────────────────────────────────────────

def frame_message(payload: bytes, chunked: bool) -> bytes:
    """Frame *payload* for send. base:1.1 -> chunked; base:1.0 -> ]]>]]>."""
    if chunked:
        return b"\n#%d\n%s\n##\n" % (len(payload), payload)
    return payload + _EOM


def deframe_eom(buf: bytes):
    """Return (message, remainder) if a full ]]>]]> message is in *buf*, else
    (None, buf)."""
    idx = buf.find(_EOM)
    if idx < 0:
        return None, buf
    return buf[:idx], buf[idx + len(_EOM):]


def deframe_chunked(buf: bytes):
    """Return (message, remainder) if a complete chunked (base:1.1) message is in
    *buf*, else (None, buf). Message ends at ``\\n##\\n``."""
    out = bytearray()
    i = 0
    n = len(buf)
    while True:
        # Expect "\n#" then either a decimal length or a second '#' (end).
        if buf.startswith(b"\n##\n", i):
            return bytes(out), buf[i + 4:]
        m = re.match(rb"\n#(\d+)\n", buf[i:])
        if not m:
            return None, buf              # need more bytes (partial header)
        size = int(m.group(1))
        start = i + m.end()
        if start + size > n:
            return None, buf              # chunk body not fully arrived yet
        out += buf[start:start + size]
        i = start + size


# ─────────────────────────────────────────────────────────────────────────────
# Namespace-stripping helpers for easy XPath on OpenConfig/Nokia YANG
# ─────────────────────────────────────────────────────────────────────────────

def strip_ns(elem: etree._Element) -> etree._Element:
    """Strip XML namespaces in-place so leaves can be matched by local name
    (OpenConfig trees are namespace-heavy; the audit only needs local names)."""
    for e in elem.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    etree.cleanup_namespaces(elem)
    return elem


# ─────────────────────────────────────────────────────────────────────────────
# Session
# ─────────────────────────────────────────────────────────────────────────────

class NetconfSession:
    """One read-only NETCONF session to a Nokia 1830 OLS NE."""

    def __init__(self, host: str, username: str, password: str,
                 port: int = 830, timeout: float = 30.0,
                 connect_timeout: float = 15.0, jump=None,
                 connect_retries: int = 1, source_ip=None):
        """Open a NETCONF session to *host*:*port*.

        *jump*, when given, is ``(jhost, juser, jpassword[, jport])`` -- reach
        *host* through an SSH direct-tcpip forward on the jump host (ProxyJump).
        This is how DCN-only RNEs (10.6.14.x, no route from a remote PC) are
        collected over NETCONF: tunnel through the GNE/seed, which already
        reaches them over the DCN. No L2 adjacency or routing hacks required.

        *connect_retries* re-attempts a timed-out TCP connect (short backoff) so a
        brief remote/WiFi blip to the NE doesn't fail the node.

        *source_ip* binds the outbound TCP to a specific local address so the OS
        uses that interface -- lets the audit prefer a wired/on-link NIC over
        Wi-Fi when both are up, without touching interface metrics."""
        self.host = host
        self._timeout = timeout
        self._chunked = False
        self._buf = b""
        self._mid = 0
        self._source_ip = source_ip
        self.server_capabilities: List[str] = []
        self._transport = None
        self._chan = None
        self._jump_transport = None
        self._connect(host, username, password, port, connect_timeout, jump,
                      max(1, connect_retries))

    # -- lifecycle ----------------------------------------------------------
    def _connect(self, host, username, password, port, connect_timeout, jump,
                 retries):
        last = None
        for attempt in range(retries):
            try:
                self._connect_once(host, username, password, port,
                                   connect_timeout, jump)
                return
            except NetconfError as exc:
                last = exc
                self.close()
                self._transport = self._chan = self._jump_transport = None
                if attempt + 1 < retries and "timed out" in str(exc).lower():
                    time.sleep(2.0)
                    continue
                raise
        if last is not None:
            raise last

    def _connect_once(self, host, username, password, port, connect_timeout, jump):
        if jump is not None:
            sock = self._open_jump_channel(jump, host, port, connect_timeout)
        else:
            src = (self._source_ip, 0) if self._source_ip else None
            try:
                sock = socket.create_connection(
                    (host, port), timeout=connect_timeout, source_address=src)
            except (OSError, socket.timeout) as exc:
                raise NetconfError(f"TCP connect to {host}:{port} failed: {exc}")
        self._transport = paramiko.Transport(sock)
        try:
            self._transport.start_client(timeout=connect_timeout)
            self._authenticate(self._transport, username, password)
            self._chan = self._transport.open_session(timeout=connect_timeout)
            self._chan.settimeout(self._timeout)
            self._chan.invoke_subsystem("netconf")
        except NetconfError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise NetconfError(f"NETCONF session setup to {host} failed: {exc}")
        self._hello()

    def _open_jump_channel(self, jump, host, port, connect_timeout):
        """SSH to the jump host and open a direct-tcpip channel to host:port.
        Returns the channel (socket-like) for the target Transport. The jump
        transport lives until close(). Raises NetconfError if the jump SSH
        rejects auth or refuses port forwarding."""
        jhost, juser, jpw = jump[0], jump[1], jump[2]
        jport = jump[3] if len(jump) > 3 else 22
        try:
            jsock = socket.create_connection((jhost, jport), timeout=connect_timeout)
        except (OSError, socket.timeout) as exc:
            raise NetconfError(f"jump TCP connect to {jhost}:{jport} failed: {exc}")
        jt = paramiko.Transport(jsock)
        try:
            jt.start_client(timeout=connect_timeout)
            self._authenticate(jt, juser, jpw)
            chan = jt.open_channel(
                "direct-tcpip", (host, port), ("127.0.0.1", 0),
                timeout=connect_timeout)
        except NetconfError:
            jt.close()
            raise
        except paramiko.ChannelException as exc:
            jt.close()
            raise NetconfError(
                f"jump host {jhost} refused forwarding to {host}:{port} "
                f"(SSH port-forwarding may be disabled): {exc}")
        except Exception as exc:
            jt.close()
            raise NetconfError(f"jump {jhost} -> {host}:{port} failed: {exc}")
        if chan is None:
            jt.close()
            raise NetconfError(
                f"jump host {jhost} returned no channel to {host}:{port}")
        self._jump_transport = jt
        return chan

    @staticmethod
    def _authenticate(transport, username, password):
        try:
            from utils.helpers import nokia_ssh_authenticate
            nokia_ssh_authenticate(transport, username, password)
            return
        except ImportError:
            pass
        except paramiko.AuthenticationException:
            raise NetconfError(f"NETCONF auth rejected for {username}")
        except Exception:
            pass
        try:
            transport.auth_password(username, password)
        except paramiko.AuthenticationException:
            raise NetconfError(f"NETCONF auth rejected for {username}")

    def _hello(self):
        # Server sends its <hello> first; we advertise 1.0+1.1 and negotiate.
        server = self._recv(force_eom=True)
        self.server_capabilities = re.findall(
            r"<capability>\s*(.*?)\s*</capability>", server, re.S)
        # Chunked framing only if BOTH ends speak base:1.1.
        self._chunked = any(_BASE_11 in c for c in self.server_capabilities)
        # Our hello MUST be end-of-message framed (pre-negotiation).
        self._chan.sendall(_CLIENT_HELLO.encode() + _EOM)

    def close(self):
        for obj in (self._chan, self._transport, self._jump_transport):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._chan = None
        self._transport = None
        self._jump_transport = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- framing I/O --------------------------------------------------------
    def _recv(self, force_eom: bool = False) -> str:
        """Read one full NETCONF message (str). ``force_eom`` reads a 1.0-framed
        message regardless of negotiation (used for the server <hello>)."""
        chunked = self._chunked and not force_eom
        deadline = time.time() + self._timeout
        while True:
            msg, self._buf = (deframe_chunked(self._buf) if chunked
                              else deframe_eom(self._buf))
            if msg is not None:
                return msg.decode("utf-8", "replace")
            if time.time() > deadline:
                raise NetconfError(f"NETCONF read timeout from {self.host}")
            try:
                chunk = self._chan.recv(65535)
            except socket.timeout:
                raise NetconfError(f"NETCONF read timeout from {self.host}")
            if not chunk:
                # channel closed -- return whatever we have (deframe best-effort)
                msg, self._buf = (deframe_chunked(self._buf) if chunked
                                  else deframe_eom(self._buf))
                if msg is not None:
                    return msg.decode("utf-8", "replace")
                raise NetconfError(f"NETCONF channel closed by {self.host}")
            self._buf += chunk

    def _rpc(self, inner_xml: str, strip: bool = True) -> etree._Element:
        """Send an <rpc> wrapping *inner_xml*, return the parsed reply element.
        Namespaces are stripped unless *strip* is False (raw is needed when the
        reply XML will be cloned back into an edit-config, where the device's own
        namespaces/prefixes must be preserved). Raises NetconfError on
        <rpc-error>."""
        self._mid += 1
        rpc = (
            f'<rpc message-id="{self._mid}" '
            f'xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">{inner_xml}</rpc>'
        )
        self._chan.sendall(frame_message(rpc.encode(), self._chunked))
        reply = self._recv()
        try:
            root = etree.fromstring(reply.encode())
        except etree.XMLSyntaxError as exc:
            raise NetconfError(f"Malformed NETCONF reply from {self.host}: {exc}")
        if strip:
            strip_ns(root)
            err = root.find(".//rpc-error")
        else:
            err = root.find(".//{*}rpc-error")
        if err is not None:
            msg = (err.findtext("error-message") or err.findtext("{*}error-message")
                   or err.findtext("error-tag") or err.findtext("{*}error-tag")
                   or "rpc-error")
            raise NetconfError(f"{self.host} rpc-error: {msg.strip()}")
        return root

    # -- public read API ----------------------------------------------------
    def get(self, subtree: Optional[str] = None) -> etree._Element:
        """<get> operational state. *subtree* is an XML subtree filter (a
        namespaced container, e.g. ``'<system xmlns=\"...\"/>'``); None = full
        tree. Returns the <data> element (namespaces stripped)."""
        filt = f'<filter type="subtree">{subtree}</filter>' if subtree else ""
        root = self._rpc(f"<get>{filt}</get>")
        data = root.find(".//data")
        return data if data is not None else root

    def get_config(self, source: str = "running",
                   subtree: Optional[str] = None,
                   strip: bool = True) -> etree._Element:
        """<get-config> from *source* datastore (default ``running``). Same
        subtree-filter semantics as get(). ``strip=False`` returns the raw
        namespaced <data> (for cloning into an edit-config). Read-only."""
        filt = f'<filter type="subtree">{subtree}</filter>' if subtree else ""
        root = self._rpc(
            f"<get-config><source><{source}/></source>{filt}</get-config>",
            strip=strip)
        tag = "data" if strip else "{*}data"
        data = root.find(f".//{tag}")
        return data if data is not None else root

    def edit_config(self, config_xml: str, target: str = "running",
                    default_operation: Optional[str] = None) -> None:
        """Send an <edit-config> to *target* (default ``running``). *config_xml*
        is the fully namespaced INNER content of <config>. WRITE operation --
        raises NetconfError on <rpc-error>. Callers must read back to verify and
        must own a revert path (this client never reverts on its own)."""
        do = (f"<default-operation>{default_operation}</default-operation>"
              if default_operation else "")
        self._rpc(
            f"<edit-config><target><{target}/></target>{do}"
            f"<config>{config_xml}</config></edit-config>")

    @staticmethod
    def first_text(elem: etree._Element, local_name: str) -> Optional[str]:
        """First text value of a descendant with the given local tag name."""
        if elem is None:
            return None
        found = elem.find(f".//{local_name}")
        return found.text.strip() if found is not None and found.text else None
