"""
Ciena SAOS 6.21.5 Basic Network Provisioning Script
Supports Ciena 39XX/51XX switches (3902, 3920, 3922, 3924, 3926, 3928, 5150, etc.)

Provisioning steps:
  1. Login via serial (console) or SSH
  2. Set hostname:       system set host-name <hostname>
  3. Create mgmt VLAN:  vlan create vlan <vlan_id> name <vlan_name>
  4. Add port to VLAN:  vlan add vlan <vlan_id> port <mgmt_port>   (optional)
  5. Create IP iface:   interface create ip-interface <iface_name> ip <ip>/32
                            vlan <vlan_id> role device-mgmt
  6. Add static route:  ip route add destination <dest> gateway <gateway>
  7. Bind protocols:    <protocol> set preferred-source-ip <iface_name>
  8. Save config:       configuration save
"""
import logging
import re
import time
from typing import Callable, Dict, List, Optional

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

# ── Protocol list for preferred-source-ip binding ───────────────────────────
# Maps display name → CLI prefix
PROTOCOL_CMDS: Dict[str, str] = {
    "SSH":        "ssh client",
    "SNMP":       "snmp",
    "NTP":        "ntp client",
    "Syslog":     "syslog",
    "RADIUS":     "radius",
    "TACACS":     "tacacs",
    "DNS":        "dns-client",
    "Telnet":     "telnet client",
    "FTP/TFTP":   "xftp",
    "RadSec":     "radsec",
}

# Default protocols to bind (the most common management protocols)
DEFAULT_PROTOCOLS = {"SSH", "SNMP", "NTP", "Syslog", "RADIUS", "TACACS"}

# Timing constants
_SERIAL_CMD_DELAY = 2.0
_SSH_CMD_DELAY    = 1.0

# Prompt/login regexes
_PROMPT_RE    = re.compile(r"[A-Za-z0-9_\-]+[>#\$]\s*$", re.MULTILINE)
_LOGIN_RE     = re.compile(r"[Ll]ogin:\s*$|[Uu]ser[Nn]ame:\s*$")
_PASSWORD_RE  = re.compile(r"[Pp]assword:\s*$")
_MORE_RE      = re.compile(r"--More--|Press any key")


# ── Serial helpers ───────────────────────────────────────────────────────────

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
            if pattern.search(buf.splitlines()[-1] if buf.splitlines() else ""):
                break
        else:
            time.sleep(chunk_delay)
    return buf


def _serial_send(ser: serial.Serial, cmd: str, delay: float = _SERIAL_CMD_DELAY) -> str:
    """Send *cmd* + newline over serial, wait *delay* seconds, read response."""
    ser.write((cmd + "\r\n").encode())
    time.sleep(delay)
    raw = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
    # Dismiss --More-- prompts
    while _MORE_RE.search(raw):
        ser.write(b" ")
        time.sleep(0.5)
        raw += ser.read(ser.in_waiting).decode("utf-8", errors="replace")
    return raw


# ── SSH helpers ──────────────────────────────────────────────────────────────

def _ssh_send(channel: paramiko.Channel, cmd: str, delay: float = _SSH_CMD_DELAY) -> str:
    """Send *cmd* over an interactive SSH channel, return accumulated output."""
    channel.send(cmd + "\n")
    time.sleep(delay)
    out = ""
    while channel.recv_ready():
        out += channel.recv(4096).decode("utf-8", errors="replace")
        time.sleep(0.1)
    # Dismiss --More-- prompts
    while _MORE_RE.search(out):
        channel.send(" ")
        time.sleep(0.3)
        while channel.recv_ready():
            out += channel.recv(4096).decode("utf-8", errors="replace")
            time.sleep(0.1)
    return out


# ── Main provisioning script ─────────────────────────────────────────────────

class Script(BaseScript):
    """
    Ciena SAOS 6.21.5 provisioning script.

    Parameters
    ----------
    connection_type : "serial" or "ssh"
    serial_port     : COM port string (serial only)
    baud_rate       : baud rate (serial only, default 9600)
    timeout         : I/O timeout in seconds
    ip_address      : IP/hostname to SSH to (SSH only)
    username / password : login credentials
    hostname        : target system hostname
    target_ip       : management IP address (without mask)
    vlan_id         : management VLAN ID (integer string)
    iface_name      : IP interface name (e.g. "mgmt")
    vlan_name       : VLAN name (default "mgmt")
    mgmt_port       : port(s) to add to management VLAN (optional, e.g. "1")
    gateway         : default gateway IP
    static_route_dest : static route destination in CIDR (e.g. "0.0.0.0/0")
    protocols       : set of protocol display names to bind preferred-source-ip
    update_existing : if True, delete existing iface before creating
    stop_callback   : callable returning True to abort
    output_callback : callable(str) for log output
    """

    def __init__(
        self,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        baud_rate: int = 9600,
        timeout: int = 15,
        ip_address: Optional[str] = None,
        username: str = "admin",
        password: str = "admin",
        hostname: str = "",
        target_ip: str = "",
        vlan_id: str = "4000",
        iface_name: str = "mgmt",
        vlan_name: str = "mgmt",
        mgmt_port: str = "",
        gateway: str = "",
        static_route_dest: str = "0.0.0.0/0",
        protocols: Optional[set] = None,
        update_existing: bool = False,
        stop_callback: Optional[Callable[[], bool]] = None,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.connection_type   = connection_type
        self.serial_port       = serial_port
        self.baud_rate         = baud_rate
        self.timeout           = timeout
        self.ip_address        = ip_address
        self.username          = username
        self.password          = password
        self.hostname          = hostname
        self.target_ip         = target_ip
        self.vlan_id           = str(vlan_id)
        self.iface_name        = iface_name
        self.vlan_name         = vlan_name
        self.mgmt_port         = mgmt_port.strip()
        self.gateway           = gateway
        self.static_route_dest = static_route_dest
        self.protocols         = protocols if protocols is not None else DEFAULT_PROTOCOLS
        self.update_existing   = update_existing
        self._stop             = stop_callback or (lambda: False)
        self._out              = output_callback or (lambda m: None)

    # ── BaseScript ABC stubs ─────────────────────────────────────────────────

    def get_commands(self):
        return []

    def execute_commands(self, *args, **kwargs):
        return None

    def process_outputs(self, *args, **kwargs):
        return None

    def abort_connection(self, *args, **kwargs):
        return None

    # ── Entry point ──────────────────────────────────────────────────────────

    def run(self) -> bool:
        """Execute provisioning; return True on success."""
        if self.connection_type == "serial":
            return self._run_serial()
        return self._run_ssh()

    # ── Serial provisioning ──────────────────────────────────────────────────

    def _run_serial(self) -> bool:
        self._log(f"Opening serial port {self.serial_port} @ {self.baud_rate} baud…")
        try:
            ser = serial.Serial(
                port=self.serial_port,
                baudrate=self.baud_rate,
                timeout=self.timeout,
            )
        except Exception as exc:
            self._log(f"[ERROR] Cannot open serial port: {exc}")
            return False

        try:
            ser.write(b"\r\n")
            time.sleep(1)
            banner = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            self._log(banner)

            # Login sequence
            if not self._serial_login(ser):
                return False

            def send(cmd: str) -> str:
                self._log(f"  >> {cmd}")
                out = _serial_send(ser, cmd)
                self._log(out)
                return out

            return self._provision(send)
        finally:
            ser.close()

    def _serial_login(self, ser: serial.Serial) -> bool:
        banner = _serial_read_until(ser, re.compile(r"[Ll]ogin:|[Uu]ser[Nn]ame:|[>#\$]\s*$"), 20)
        self._log(banner)
        if _LOGIN_RE.search(banner):
            out = _serial_send(ser, self.username, 1.5)
            self._log(out)
            out = _serial_read_until(ser, _PASSWORD_RE, 10)
            self._log(out)
            out = _serial_send(ser, self.password, 2.0)
            self._log(out)
        elif _PROMPT_RE.search(banner):
            self._log("Already at prompt.")
        else:
            self._log("[WARN] Unexpected banner; attempting login anyway.")
            out = _serial_send(ser, self.username, 1.5)
            out += _serial_send(ser, self.password, 2.0)
            self._log(out)
        return True

    # ── SSH provisioning ─────────────────────────────────────────────────────

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
        Execute SAOS provisioning steps using *send_fn(cmd) -> output*.
        Returns True on success.
        """
        if self._stop():
            self._log("[STOPPED]")
            return False

        # ── Step 1: Set hostname ─────────────────────────────────────────────
        if self.hostname:
            self._log("Setting hostname…")
            send_fn(f"system set host-name {self.hostname}")
            if self._stop():
                return False

        # ── Step 2: Create management VLAN ──────────────────────────────────
        self._log(f"Creating management VLAN {self.vlan_id}…")
        send_fn(f"vlan create vlan {self.vlan_id} name {self.vlan_name}")
        if self._stop():
            return False

        # ── Step 3: Add port(s) to VLAN ─────────────────────────────────────
        if self.mgmt_port:
            self._log(f"Adding port {self.mgmt_port} to VLAN {self.vlan_id}…")
            send_fn(f"vlan add vlan {self.vlan_id} port {self.mgmt_port}")
            if self._stop():
                return False

        # ── Step 4: Handle existing interface ────────────────────────────────
        if self.update_existing:
            self._log(f"Removing existing interface {self.iface_name} (if present)…")
            send_fn(f"interface delete ip-interface {self.iface_name}")
            time.sleep(0.5)
            if self._stop():
                return False

        # ── Step 5: Create IP interface (/32 loopback-style) ────────────────
        self._log(f"Creating IP interface {self.iface_name} ({self.target_ip}/32)…")
        send_fn(
            f"interface create ip-interface {self.iface_name}"
            f" ip {self.target_ip}/32"
            f" vlan {self.vlan_id}"
            f" role device-mgmt"
        )
        if self._stop():
            return False

        # ── Step 6: Static route ─────────────────────────────────────────────
        if self.gateway and self.static_route_dest:
            self._log(f"Adding static route {self.static_route_dest} via {self.gateway}…")
            send_fn(
                f"ip route add destination {self.static_route_dest}"
                f" gateway {self.gateway}"
            )
            if self._stop():
                return False

        # ── Step 7: Bind preferred-source-ip for each selected protocol ──────
        for proto_name in sorted(self.protocols):
            cli_prefix = PROTOCOL_CMDS.get(proto_name)
            if not cli_prefix:
                continue
            self._log(f"Binding {proto_name} preferred-source-ip → {self.iface_name}…")
            send_fn(f"{cli_prefix} set preferred-source-ip {self.iface_name}")
            if self._stop():
                return False

        # ── Step 8: Save configuration ───────────────────────────────────────
        self._log("Saving configuration…")
        send_fn("configuration save")
        time.sleep(2)

        self._log("Provisioning complete.")
        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        msg = msg.strip()
        if msg:
            logger.debug(msg)
            self._out(msg)
