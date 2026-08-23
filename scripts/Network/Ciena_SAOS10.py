"""
Ciena SAOS 10 Basic Network Provisioning Script
Supports Ciena Service Aggregation Platforms running SAOS 10.x
(3924, 3926, 3928, 5162, 5170, 5171, 8180, etc.)

Provisioning steps:
  1. Login via serial (console) or SSH
  2. Enter config mode:      config
  3. Set hostname:            system config hostname <hostname>
  4. Disable DHCP on mgmt:   dhcp-client client mgmtbr0 admin-enable false
  5. Set static mgmt IP:     oc-if:interfaces interface mgmtbr0 ipv4 addresses address
                                 <ip> config ip <ip> prefix-length <prefix_len>
  6. Add default route:      rib vrf default ipv4 0.0.0.0/0 next-hop <gateway>
  7. (Optional) Global src:  management-plane default-source-ip interface <iface>
  8. Exit config:             exit

Note: SAOS 10 auto-saves configuration changes — no explicit 'save' command required.

Baud rates by platform:
  3924, 3926, 3928           → 9600 bps
  5162, 5169, 5170, 5171,
  8140, 8180                 → 115200 bps
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

# Timing constants
_SERIAL_CMD_DELAY = 2.0
_SSH_CMD_DELAY    = 1.5

# SAOS 10 prompts:
#   Operational mode: "hostname> "
#   Config mode:      "username@hostname# "
_PROMPT_RE   = re.compile(r"[A-Za-z0-9_\-@]+[>#]\s*$", re.MULTILINE)
_LOGIN_RE    = re.compile(r"[Ll]ogin:\s*$|[Uu]ser[Nn]ame:\s*$")
_PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")
_MORE_RE     = re.compile(r"--More--|Press any key")


# ── Serial helpers ───────────────────────────────────────────────────────────

def _serial_read_until(
    ser: serial.Serial,
    pattern: re.Pattern,
    timeout: float = 30.0,
    chunk_delay: float = 0.1,
) -> str:
    """Read from *ser* until *pattern* matches on the last line or *timeout* elapses."""
    buf = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
        if chunk:
            buf += chunk
            last_line = buf.splitlines()[-1] if buf.splitlines() else ""
            if pattern.search(last_line):
                break
        else:
            time.sleep(chunk_delay)
    return buf


def _serial_send(ser: serial.Serial, cmd: str, delay: float = _SERIAL_CMD_DELAY) -> str:
    """Send *cmd* over serial, wait *delay* seconds, read and return all output."""
    ser.write((cmd + "\r\n").encode())
    time.sleep(delay)
    raw = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
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
    Ciena SAOS 10 provisioning script.

    Parameters
    ----------
    connection_type  : "serial" or "ssh"
    serial_port      : COM port string (serial only)
    baud_rate        : baud rate (serial only; 9600 for 3924/3926/3928,
                       115200 for 5162/5170/5171/8180)
    timeout          : I/O timeout in seconds
    ip_address       : IP/hostname for SSH (SSH only)
    username / password : login credentials
    hostname         : target system hostname
    target_ip        : management IP address (without mask)
    prefix_len       : subnet prefix length (e.g. "24")
    gateway          : default-route next-hop IP
    static_route_dest: route destination CIDR (default "0.0.0.0/0")
    global_src_iface : if set, configure management-plane default-source-ip
                       to point at this L3 interface name
    update_existing  : if True, delete existing mgmtbr0 address before adding new one
    stop_callback    : callable returning True to abort
    output_callback  : callable(str) for UI log output
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
        prefix_len: str = "24",
        gateway: str = "",
        static_route_dest: str = "0.0.0.0/0",
        global_src_iface: str = "",
        update_existing: bool = False,
        stop_callback: Optional[Callable[[], bool]] = None,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.connection_type    = connection_type
        self.serial_port        = serial_port
        self.baud_rate          = baud_rate
        self.timeout            = timeout
        self.ip_address         = ip_address
        self.username           = username
        self.password           = password
        self.hostname           = hostname
        self.target_ip          = target_ip
        self.prefix_len         = str(prefix_len)
        self.gateway            = gateway
        self.static_route_dest  = static_route_dest
        self.global_src_iface   = global_src_iface.strip()
        self.update_existing    = update_existing
        self._stop              = stop_callback or (lambda: False)
        self._out               = output_callback or (lambda m: None)

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
        banner = _serial_read_until(
            ser,
            re.compile(r"[Ll]ogin:|[Uu]ser[Nn]ame:|[>#]\s*$"),
            timeout=20,
        )
        self._log(banner)
        if _LOGIN_RE.search(banner):
            self._log(f"  >> {self.username}")
            _serial_send(ser, self.username, 1.5)
            _serial_read_until(ser, _PASSWORD_RE, timeout=10)
            self._log("  >> ****")
            _serial_send(ser, self.password, 2.0)
        elif _PROMPT_RE.search(banner):
            self._log("Already at prompt.")
        else:
            self._log("[WARN] Unexpected banner; attempting login anyway.")
            _serial_send(ser, self.username, 1.5)
            _serial_send(ser, self.password, 2.0)
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
        Execute SAOS 10 provisioning using *send_fn(cmd) -> output*.
        Returns True on success.
        """
        if self._stop():
            self._log("[STOPPED]")
            return False

        # ── Step 1: Enter config mode ────────────────────────────────────────
        self._log("Entering configuration mode…")
        send_fn("config")
        if self._stop():
            return False

        # ── Step 2: Set hostname ─────────────────────────────────────────────
        if self.hostname:
            self._log(f"Setting hostname to '{self.hostname}'…")
            send_fn(f"system config hostname {self.hostname}")
            if self._stop():
                return False

        # ── Step 3: Disable DHCP client on mgmtbr0 ──────────────────────────
        self._log("Disabling DHCP client on mgmtbr0…")
        send_fn("dhcp-client client mgmtbr0 admin-enable false")
        if self._stop():
            return False

        # ── Step 4: Remove existing address (if update mode) ────────────────
        if self.update_existing:
            self._log("Removing existing mgmtbr0 IPv4 addresses…")
            # SAOS 10 uses 'no' prefix to delete configuration
            send_fn("no oc-if:interfaces interface mgmtbr0 ipv4 addresses")
            time.sleep(0.5)
            if self._stop():
                return False

        # ── Step 5: Configure static IP on mgmtbr0 ──────────────────────────
        self._log(f"Setting management IP {self.target_ip}/{self.prefix_len} on mgmtbr0…")
        send_fn(
            f"oc-if:interfaces interface mgmtbr0 ipv4 addresses address"
            f" {self.target_ip} config ip {self.target_ip}"
            f" prefix-length {self.prefix_len}"
        )
        if self._stop():
            return False

        # ── Step 6: Add static default route ────────────────────────────────
        if self.gateway and self.static_route_dest:
            self._log(f"Adding route {self.static_route_dest} via {self.gateway}…")
            send_fn(
                f"rib vrf default ipv4 {self.static_route_dest}"
                f" next-hop {self.gateway}"
            )
            if self._stop():
                return False

        # ── Step 7: Set global source IP interface (optional) ────────────────
        if self.global_src_iface:
            self._log(
                f"Setting management-plane default-source-ip → {self.global_src_iface}…"
            )
            send_fn(
                f"management-plane default-source-ip interface {self.global_src_iface}"
            )
            if self._stop():
                return False

        # ── Step 8: Exit config mode ─────────────────────────────────────────
        # Changes are auto-saved in SAOS 10; exit returns to operational mode.
        self._log("Exiting configuration mode…")
        send_fn("exit")

        self._log("Provisioning complete. (SAOS 10 auto-saves configuration changes.)")
        return True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        msg = msg.strip()
        if msg:
            logger.debug(msg)
            self._out(msg)
