"""
Nokia 1830 OLS Basic Network Provisioning Script
Supports PSI-4L, PSI-8L, and PSS-16II shelf types (Release 25.x / 26.x)

Provisioning steps:
  1. Login via serial console (38400 baud) or SSH
  2. Show current NE details: show general detail
  3. Set hostname:            config general name <hostname>
  4. Disable DHCP client on management port
  5. Set management IP:
       PSI-4L / PSI-8L  →  config interface mfc 1/10/oamp ip <ip>/<prefix>
       PSS-16II         →  config interface usrpnl oamp ip <ip>/<prefix>
  6. Set default gateway:    config cn routes default add <gateway> force
  7. (Optional) Save DB:     config database
"""
import logging
import re
import time
from typing import Callable, Optional

import paramiko
import serial

from script_interface import (
    BaseScript,
    ssh_connect_with_credential_fallback,
    CredentialPromptRequired,
    NEEDS_CREDENTIALS_SENTINEL,
)
from utils.helpers import get_known_hosts_path, get_host_key_policy, safe_load_host_keys

logger = logging.getLogger(__name__)

# Serial defaults for all 1830 OLS platforms
_OLS_BAUD_RATE = 38400

# Timing constants
_SERIAL_CMD_DELAY = 2.0
_SSH_CMD_DELAY    = 1.0

# Prompt / login patterns
_PROMPT_RE   = re.compile(r"[A-Za-z0-9_\-]+[>#]\s*$", re.MULTILINE)
_LOGIN_RE    = re.compile(r"[Ll]ogin:\s*$")
_USER_RE     = re.compile(r"[Uu]ser[Nn]ame:\s*$|[Ll]ogin as:\s*$")
_PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")
_MORE_RE     = re.compile(r"--More--|Press any key|<SPACE>")

# Shelf-type keywords found in 'show general detail' System Description
_PSI_SHELF_RE    = re.compile(r"PSI-[48]L", re.IGNORECASE)
_PSS16_SHELF_RE  = re.compile(r"PSS-?16", re.IGNORECASE)


# ── Serial helpers ────────────────────────────────────────────────────────────

def _serial_read_until(
    ser: serial.Serial,
    pattern: re.Pattern,
    timeout: float = 30.0,
    chunk_delay: float = 0.1,
) -> str:
    """Read from *ser* until *pattern* matches or *timeout* elapses."""
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
        if chunk:
            buf += chunk
            last_line = buf.splitlines()[-1] if buf.splitlines() else ""
            if pattern.search(last_line) or pattern.search(buf[-200:]):
                break
        else:
            time.sleep(chunk_delay)
    return buf


def _serial_send(ser: serial.Serial, cmd: str, delay: float = _SERIAL_CMD_DELAY) -> str:
    """Send *cmd* + newline over serial, wait *delay* seconds, return response."""
    ser.write((cmd + "\r\n").encode())
    time.sleep(delay)
    raw = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
    while _MORE_RE.search(raw):
        ser.write(b" ")
        time.sleep(0.5)
        raw += ser.read(ser.in_waiting).decode("utf-8", errors="replace")
    return raw


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _ssh_send(channel: paramiko.Channel, cmd: str, delay: float = _SSH_CMD_DELAY) -> str:
    """Send *cmd* over an interactive SSH channel, return accumulated output."""
    channel.send(cmd + "\n")
    time.sleep(delay)
    out = ""
    while channel.recv_ready():
        out += channel.recv(4096).decode("utf-8", errors="replace")
        time.sleep(0.1)
    while _MORE_RE.search(out):
        channel.send(" ")
        time.sleep(0.3)
        while channel.recv_ready():
            out += channel.recv(4096).decode("utf-8", errors="replace")
            time.sleep(0.1)
    return out


# ── Main provisioning script ──────────────────────────────────────────────────

class Script(BaseScript):
    """
    Nokia 1830 OLS provisioning script.

    Parameters
    ----------
    connection_type : "serial" or "ssh"
    serial_port     : COM port string (serial only)
    baud_rate       : baud rate (default 38400 for OLS)
    timeout         : I/O timeout in seconds
    ip_address      : IP/hostname to SSH to (SSH only)
    username / password : login credentials
    hostname        : target NE name (1–20 characters)
    target_ip       : management IP address (without mask)
    prefix_len      : subnet prefix length (integer, e.g. 24)
    gateway         : default gateway IP
    shelf_type      : "auto", "psi" (PSI-4L/8L), or "pss16" (PSS-16II)
    set_loopback    : if True, also set loopback IP = target_ip/32 (causes warm reset)
    stop_callback   : callable returning True to abort
    output_callback : callable(str) for log output
    """

    def __init__(
        self,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        baud_rate: int = _OLS_BAUD_RATE,
        timeout: int = 15,
        ip_address: Optional[str] = None,
        username: str = "admin",
        password: str = "admin",
        hostname: str = "",
        target_ip: str = "",
        prefix_len: int = 24,
        gateway: str = "",
        shelf_type: str = "auto",
        set_loopback: bool = False,
        stop_callback: Optional[Callable[[], bool]] = None,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.connection_type = connection_type
        self.serial_port     = serial_port
        self.baud_rate       = baud_rate
        self.timeout         = timeout
        self.ip_address      = ip_address
        self.username        = username
        self.password        = password
        self.hostname        = hostname
        self.target_ip       = target_ip
        self.prefix_len      = prefix_len
        self.gateway         = gateway
        self.shelf_type      = shelf_type.lower() if shelf_type else "auto"
        self.set_loopback    = set_loopback
        self._stop           = stop_callback or (lambda: False)
        self._out            = output_callback or (lambda m: None)

    # ── BaseScript stubs ──────────────────────────────────────────────────────

    def get_commands(self):
        return []

    def execute_commands(self, *args, **kwargs):
        return None

    def process_outputs(self, *args, **kwargs):
        return None

    def abort_connection(self, *args, **kwargs):
        return None

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> bool:
        """Execute provisioning; return True on success."""
        if self.connection_type == "serial":
            return self._run_serial()
        return self._run_ssh()

    # ── Serial provisioning ───────────────────────────────────────────────────

    def _run_serial(self) -> bool:
        self._log(f"Opening serial port {self.serial_port} @ {self.baud_rate} baud…")
        try:
            ser = serial.Serial(
                port=self.serial_port,
                baudrate=self.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
        except Exception as exc:
            self._log(f"[ERROR] Cannot open serial port: {exc}")
            return False

        try:
            # Wake up the device
            ser.write(b"\r\n")
            time.sleep(1.5)
            banner = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            self._log(banner)

            if not self._serial_login(ser, banner):
                return False

            def send(cmd: str) -> str:
                self._log(f"  >> {cmd}")
                out = _serial_send(ser, cmd)
                self._log(out)
                return out

            return self._provision(send)
        finally:
            ser.close()

    def _serial_login(self, ser: serial.Serial, initial_banner: str = "") -> bool:
        """
        Handle the three-prompt serial login sequence:
          hostname login: cli
          Username: <user>
          Password: <pass>
        """
        banner = initial_banner
        combined = re.compile(
            r"[Ll]ogin:\s*$|[Uu]ser[Nn]ame:\s*$|[>#]\s*$", re.MULTILINE
        )
        if not combined.search(banner):
            banner = _serial_read_until(ser, combined, 20)
            self._log(banner)

        if _PROMPT_RE.search(banner):
            self._log("Already at prompt.")
            return True

        # "hostname login:" prompt — send "cli" to get to Username
        if _LOGIN_RE.search(banner):
            out = _serial_send(ser, "cli", 1.5)
            self._log(out)
            banner = out

        # "Username:" prompt
        if _USER_RE.search(banner) or not _PASSWORD_RE.search(banner):
            out = _serial_read_until(ser, re.compile(r"[Uu]ser[Nn]ame:|[Pp]assword:"), 10)
            self._log(out)
            banner += out

        if _USER_RE.search(banner):
            out = _serial_send(ser, self.username, 1.5)
            self._log(out)
            banner += out

        # "Password:" prompt
        if not _PASSWORD_RE.search(banner):
            out = _serial_read_until(ser, _PASSWORD_RE, 10)
            self._log(out)
            banner += out

        out = _serial_send(ser, self.password, 2.5)
        self._log(out)

        if not _PROMPT_RE.search(out):
            # Give it one more second to show the prompt
            extra = _serial_read_until(ser, _PROMPT_RE, 5)
            self._log(extra)
            if not _PROMPT_RE.search(extra):
                self._log("[WARN] Could not confirm prompt after login.")
        return True

    # ── SSH provisioning ──────────────────────────────────────────────────────

    def _run_ssh(self) -> bool:
        target = self.ip_address or self.target_ip
        self._log(f"Connecting via SSH to {target}…")
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(get_host_key_policy())
            known_hosts = get_known_hosts_path()
            if known_hosts:
                safe_load_host_keys(client, str(known_hosts))
            client.connect(
                target,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
        except Exception as exc:
            self._log(f"[ERROR] SSH connection failed: {exc}")
            return False

        try:
            channel = client.invoke_shell()
            time.sleep(1.5)
            banner = ""
            while channel.recv_ready():
                banner += channel.recv(4096).decode("utf-8", errors="replace")
            self._log(banner)

            def send(cmd: str) -> str:
                self._log(f"  >> {cmd}")
                out = _ssh_send(channel, cmd)
                self._log(out)
                return out

            return self._provision(send)
        finally:
            client.close()

    # ── Shared provisioning logic ─────────────────────────────────────────────

    def _provision(self, send_fn: Callable[[str], str]) -> bool:
        """
        Execute OLS provisioning steps using *send_fn(cmd) -> output*.
        Returns True on success.
        """
        if self._stop():
            self._log("[STOPPED]")
            return False

        # ── Step 1: Show current NE details ──────────────────────────────────
        self._log("Querying current NE details…")
        detail_out = send_fn("show general detail")
        if self._stop():
            return False

        # Auto-detect shelf type from System Description if needed
        resolved_shelf = self._resolve_shelf_type(detail_out)
        self._log(f"Shelf type resolved to: {resolved_shelf}")

        # ── Step 2: Set hostname ──────────────────────────────────────────────
        if self.hostname:
            # OLS allows 1–20 characters for NE name
            ne_name = self.hostname[:20]
            self._log(f"Setting NE name to '{ne_name}'…")
            send_fn(f"config general name {ne_name}")
            if self._stop():
                return False

        # ── Step 3: Disable DHCP client on management port ───────────────────
        ip_prefix = f"{self.target_ip}/{self.prefix_len}"
        if resolved_shelf == "pss16":
            mgmt_port = "usrpnl oamp"
        else:
            # PSI-4L, PSI-8L, and default
            mgmt_port = "mfc 1/10/oamp"

        self._log(f"Disabling DHCP client on {mgmt_port}…")
        send_fn(f"config interface {mgmt_port} dhcp_client disabled")
        if self._stop():
            return False

        send_fn(f"config interface {mgmt_port} dhcp_client_gateway disabled")
        if self._stop():
            return False

        # ── Step 4: Set management IP address ────────────────────────────────
        self._log(f"Setting {mgmt_port} IP to {ip_prefix}…")
        send_fn(f"config interface {mgmt_port} ip {ip_prefix}")
        if self._stop():
            return False

        # ── Step 5: Set default gateway ──────────────────────────────────────
        if self.gateway:
            self._log(f"Setting default gateway to {self.gateway}…")
            # 'force' makes the command re-issue-safe (no error if route exists)
            send_fn(f"config cn routes default add {self.gateway} force")
            if self._stop():
                return False

        # ── Step 6: (Optional) Set loopback IP ───────────────────────────────
        if self.set_loopback:
            self._log(
                f"Setting loopback IP to {self.target_ip}/32 "
                "(NOTE: this will trigger a warm reset)…"
            )
            send_fn(f"config interface loopback ip {self.target_ip}/32")
            # Allow extra time for the warm reset to complete
            self._log("Waiting for NE to reboot after loopback IP change…")
            time.sleep(60)
            if self._stop():
                return False

        self._log("OLS provisioning complete.")
        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_shelf_type(self, show_output: str) -> str:
        """
        Determine shelf type from 'show general detail' output.
        Returns "pss16", "psi", based on System Description field.
        Falls back to self.shelf_type if set, otherwise "psi".
        """
        if self.shelf_type and self.shelf_type != "auto":
            return self.shelf_type

        if _PSS16_SHELF_RE.search(show_output):
            return "pss16"
        if _PSI_SHELF_RE.search(show_output):
            return "psi"

        # Could not auto-detect — default to PSI-4L/8L behavior
        self._log("[WARN] Could not detect shelf type from output; defaulting to PSI-4L/8L.")
        return "psi"

    def _log(self, msg: str) -> None:
        msg = msg.strip()
        if msg:
            logger.debug(msg)
            self._out(msg)
