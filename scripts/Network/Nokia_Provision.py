"""
Nokia Basic Network Provisioning Script
Supports Nokia 7705 SAR and Nokia 7250 IXR devices.

Provisioning steps (per MOP):
  1. Login via serial (console) or SSH
  2. Set hostname:            configure system name <hostname>
  3. Set BOF address:         bof primary-address <ip>/<prefix>
  4. Set static route:        bof static-route-entry <dest> next-hop <gateway>
  5. Configure card type      (device-type specific)
  6. show mda → configure card 1 mda <slot> mda-type <equipped-type>  (per MDA)
  7. Save config              (device-type specific procedure)
"""
import logging
import re
import time
from typing import Callable, List, Optional, Tuple

import paramiko
import serial

from script_interface import (
    BaseScript,
    ssh_connect_with_credential_fallback,
    CredentialPromptRequired,
    NEEDS_CREDENTIALS_SENTINEL,
)
from utils.credentials import get_default_credentials_to_try, prompt_for_credentials_gui
from utils.helpers import get_known_hosts_path, get_host_key_policy, safe_load_host_keys, safe_save_host_keys

# ── Device type constants ───────────────────────────────────────────────────
DEVICE_7705 = "7705"
DEVICE_7250 = "7250"

# Card types mandated by the MOP
CARD_TYPE = {
    DEVICE_7705: "iom-sar",
    DEVICE_7250: "iom-ixr-r6",
}

# Seconds to pause after sending each provisioning command (serial is slower)
_SERIAL_CMD_DELAY = 3.0
_SSH_CMD_DELAY = 1.5

# Prompt patterns used to detect command completion
# Handles both classic CLI (A:hostname#, *A:hostname#) and
# MD-CLI / groff mode (A:admin@hostname#, [/]\nA:admin@hostname#)
_PROMPT_RE = re.compile(r"[A-Za-z0-9_\-@:]+[#>$]\s*$")
_LOGIN_RE = re.compile(r"[Ll]ogin:\s*$|[Uu]ser[Nn]ame:\s*$")
_PASSWORD_RE = re.compile(r"[Pp]assword:\s*$")

# Auto-Discovery warning that can appear before the CLI prompt after login.
# The device asks whether to terminate auto-discovery; we always answer "y".
_AUTO_DISC_RE = re.compile(r"[Dd]o you wish to terminate Auto-Discovery\s*\(y/n\)\?", re.IGNORECASE)


def _detect_device_type(hostname: str) -> Optional[str]:
    """Infer device type from hostname suffix (_7705 or _7250)."""
    h = (hostname or "").upper()
    if "_7705" in h:
        return DEVICE_7705
    if "_7250" in h:
        return DEVICE_7250
    return None


# Values that appear in the Equipped Type column but are NOT real MDA types
_MDA_NON_TYPES = frozenset(
    {"(empty)", "(not", "provisioned)", "equipped", "type", "state", ""}
)


def _parse_show_mda(output: str) -> List[Tuple[str, str]]:
    """Parse *show mda* (summary) output.

    Nokia SR OS ``show mda`` format (one entry spans two lines)::

        Slot  Mda   Provisioned Type                    Admin     Oper
                    Equipped Type                       State     State
        ─────────────────────────────────────────────────────────────────
        1     1     (not provisioned)                   up        up
                    me6-100gb-qsfp28
        1     2     m20-1gb-tx                          up        up
                    m20-1gb-tx
        1     3     (empty)                              -         -
                    (empty)

    Returns a list of ``(mda_num, equipped_type)`` tuples for all slots that
    have a physical MDA installed.  Empty slots and unrecognised lines are
    silently skipped.
    """
    output = output.replace("Press any key to continue (Q to quit)", "")

    results: List[Tuple[str, str]] = []
    lines = output.splitlines()

    # Nokia SR OS 'show mda' has two entry formats:
    #   Full:  "1     1     (not provisioned) ..."  — slot + MDA, slot at col 0-3
    #   Short: "      2     (not provisioned) ..."  — MDA only, slot omitted after row 1
    # The equipped-type continuation line is indented 12+ spaces.
    full_entry_re = re.compile(r"^\s{0,3}(\d+)\s+(\d+)\s+")   # slot  mda  ...
    mda_only_re   = re.compile(r"^\s{4,10}(\d+)\s+")           # [spaces]mda  ...
    equipped_re   = re.compile(r"^\s{12,}(\S+)")               # [deep indent]type

    i = 0
    while i < len(lines):
        line = lines[i]

        # Determine MDA number from whichever format matches
        mda_num: Optional[str] = None
        em = full_entry_re.match(line)
        if em:
            mda_num = em.group(2)
        else:
            mo = mda_only_re.match(line)
            if mo:
                mda_num = mo.group(1)

        if mda_num is not None:
            equipped_type: Optional[str] = None

            # Equipped type follows on the next non-blank line (serial adds blank
            # lines between every device line due to \r\n decoding)
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                eqm = equipped_re.match(lines[j])
                if eqm:
                    val = eqm.group(1).strip().lower()
                    if val not in _MDA_NON_TYPES:
                        equipped_type = eqm.group(1).strip()
                    i = j  # consume the equipped-type line

            # Fallback: provisioned type on the main line itself (single-line format)
            if not equipped_type:
                parts = line.split()
                # Full entry: slot mda type ...; Short entry: mda type ...
                type_idx = 2 if em else 1
                if len(parts) > type_idx:
                    candidate = parts[type_idx].strip().lower()
                    if candidate not in _MDA_NON_TYPES:
                        equipped_type = parts[type_idx].strip()

            if equipped_type:
                results.append((mda_num, equipped_type))

        i += 1

    return results


class Script(BaseScript):
    """Provision a single Nokia 7705 or 7250 with hostname, management IP, and static route."""

    def __init__(
        self,
        *,
        connection_type: str = "serial",
        serial_port: Optional[str] = None,
        baud_rate: int = 115200,
        timeout: int = 10,
        ip_address: Optional[str] = None,
        username: str = "admin",
        password: str = "admin",
        hostname: Optional[str] = None,
        target_ip: Optional[str] = None,
        prefix_len: int = 22,
        gateway: Optional[str] = None,
        static_route_dest: str = "10.0.0.0/8",
        device_type: Optional[str] = None,
        configure_card: bool = True,
        sync_redundancy: bool = False,
        stop_callback: Optional[Callable[[], bool]] = None,
        output_callback: Optional[Callable[[str], None]] = None,
        # BaseScript compatibility — not used for provisioning
        db_path=None,
        db_cache=None,
        command_tracker=None,
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.ip_address = ip_address  # current IP to SSH into
        self.username = username
        self.password = password

        self.hostname = hostname
        self.target_ip = target_ip
        self.prefix_len = prefix_len
        self.gateway = gateway
        self.static_route_dest = static_route_dest

        # Resolve device type: explicit override → hostname detection → None
        self.device_type = device_type or _detect_device_type(hostname or "")
        self.configure_card = configure_card
        self.sync_redundancy = sync_redundancy

        self.stop_callback = stop_callback
        self.output_callback = output_callback or (lambda msg: None)

        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.serial_port_obj: Optional[serial.Serial] = None

        # Populated during run
        self.device_name: Optional[str] = None

    # ── BaseScript interface ────────────────────────────────────────────────

    def get_commands(self) -> List[str]:
        """Not used for provisioning (steps are conditional/sequential)."""
        return []

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        """Not used for provisioning."""
        return [], None

    def process_outputs(self, outputs_from_device, ip_address, outputs) -> None:
        """Not used for provisioning."""

    # ── Stop / sleep helpers ────────────────────────────────────────────────

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    def sleep_with_abort(self, seconds: float, interval: float = 0.1) -> bool:
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.should_stop():
                return True
            time.sleep(min(interval, end_time - time.time()))
        return self.should_stop()

    def abort_connection(self) -> None:
        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass
            finally:
                self.ssh_client = None
        if self.serial_port_obj:
            try:
                self.serial_port_obj.close()
            except Exception:
                pass
            finally:
                self.serial_port_obj = None

    # ── Output helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        logging.info(msg)
        self.output_callback(msg)

    def _warn(self, msg: str) -> None:
        logging.warning(msg)
        self.output_callback(f"[WARN] {msg}")

    def _err(self, msg: str) -> None:
        logging.error(msg)
        self.output_callback(f"[ERROR] {msg}")

    # ── Serial helpers ──────────────────────────────────────────────────────

    def _serial_read_until(
        self, ser: serial.Serial, patterns: List[re.Pattern], timeout: float = 30.0
    ) -> Tuple[str, Optional[re.Pattern]]:
        """
        Read from *ser* until one of *patterns* matches the tail of accumulated
        output or *timeout* seconds elapse.

        Returns (accumulated_output, matched_pattern or None).
        """
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.should_stop():
                return buf, None
            waiting = ser.in_waiting
            if waiting:
                chunk = ser.read(waiting).decode("utf-8", errors="replace")
                buf += chunk
                # Auto-dismiss the Auto-Discovery termination prompt
                if _AUTO_DISC_RE.search(buf):
                    self._log("[AUTO-DISC] Auto-Discovery prompt detected — answering 'y'.")
                    ser.write(b"y\r\n")
                    buf += "\ny\n"
                for pat in patterns:
                    if pat.search(buf):
                        return buf, pat
            else:
                time.sleep(0.1)
        return buf, None

    def _serial_login(self, ser: serial.Serial) -> bool:
        """Send Enter then handle login:/Password: prompts, cycling through credentials.

        Tries: primary creds → defaults → GUI prompt.  Returns True on success.
        """
        self._log("Sending Enter to activate console...")
        ser.write(b"\r")

        output, matched = self._serial_read_until(
            ser, [_LOGIN_RE, _PROMPT_RE], timeout=15
        )
        self._log(f"Console response: {output.strip()[-200:]}")

        if matched is _PROMPT_RE:
            self._log("Already at command prompt (no login required).")
            return True

        if matched is not _LOGIN_RE:
            self._err("No login prompt or command prompt detected on console.")
            return False

        # Build credential queue: primary first, then any defaults not already queued
        queue: List[Tuple[str, str]] = []
        primary = (self.username or "", self.password or "")
        queue.append(primary)
        for u, p in (get_default_credentials_to_try() or []):
            pair = (u or "", p or "")
            if pair not in queue:
                queue.append(pair)

        for user, pwd in queue:
            self._log(f"Sending username: {user}")
            ser.write((user + "\r").encode())
            out2, m2 = self._serial_read_until(ser, [_PASSWORD_RE], timeout=10)
            self._log(f"After username: {out2.strip()[-200:]}")
            if not m2:
                self._err("Did not receive password prompt.")
                return False

            self._log("Sending password...")
            ser.write((pwd + "\r").encode())
            out3, m3 = self._serial_read_until(ser, [_PROMPT_RE, _LOGIN_RE], timeout=30)
            self._log(f"After password: {out3.strip()[-200:]}")

            if m3 is _PROMPT_RE:
                self._log(f"Login succeeded as '{user}'.")
                self.username = user
                self.password = pwd
                return True

            # Auth failed — device re-showed Login: prompt
            logging.warning(f"[AUTH] Serial login failed for user '{user}'")

        # All defaults exhausted — try GUI prompt once
        self._log("All default credentials failed; requesting credentials from user...")
        result = prompt_for_credentials_gui()
        if result:
            user, pwd = result
            self._log(f"Sending username: {user}")
            ser.write((user + "\r").encode())
            out2, m2 = self._serial_read_until(ser, [_PASSWORD_RE], timeout=10)
            if not m2:
                self._err("Did not receive password prompt.")
                return False
            self._log("Sending password...")
            ser.write((pwd + "\r").encode())
            out3, m3 = self._serial_read_until(ser, [_PROMPT_RE, _LOGIN_RE], timeout=30)
            if m3 is _PROMPT_RE:
                self._log(f"Login succeeded as '{user}'.")
                self.username = user
                self.password = pwd
                return True

        self._err(
            "All credential attempts failed. Check the username and password in the Provision tab."
        )
        return False

    def _serial_send(
        self,
        ser: serial.Serial,
        command: str,
        wait_prompt: bool = True,
        delay: float = _SERIAL_CMD_DELAY,
        timeout: float = 60.0,
    ) -> str:
        """Send *command* over serial; optionally wait for prompt.  Returns output."""
        self._log(f"[CMD] {command}")
        ser.write((command + "\r\n").encode())
        if not wait_prompt:
            time.sleep(delay)
            raw = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
            return raw
        output, _ = self._serial_read_until(ser, [_PROMPT_RE], timeout=timeout)
        # Handle --More-- / Press any key
        while "Press any key" in output or "More" in output:
            ser.write(b" ")
            more, _ = self._serial_read_until(ser, [_PROMPT_RE], timeout=30)
            output += more
        return output

    # ── SSH helpers ─────────────────────────────────────────────────────────

    def _ssh_connect(self) -> Optional[paramiko.Channel]:
        """Open SSH connection, return an interactive shell channel."""
        _kh = str(get_known_hosts_path())
        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.load_system_host_keys()
        safe_load_host_keys(self.ssh_client, _kh)
        self.ssh_client.set_missing_host_key_policy(get_host_key_policy())
        self._log(f"SSH connecting to {self.ip_address}...")
        try:
            ssh_connect_with_credential_fallback(
                self.ssh_client,
                self.ip_address,
                self.username,
                self.password,
                timeout=15,
            )
        except CredentialPromptRequired:
            self._err("Credentials exhausted; cannot connect.")
            return None
        except paramiko.AuthenticationException as exc:
            self._err(f"Authentication failed: {exc}")
            return None
        safe_save_host_keys(self.ssh_client, _kh)
        self._log(f"SSH connected to {self.ip_address}")
        shell = self.ssh_client.invoke_shell()
        # Drain the login banner, handling the Auto-Discovery prompt if present.
        # Give the device up to 15 s to show either the prompt or the warning.
        banner = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            if shell.recv_ready():
                chunk = shell.recv(65535).decode("utf-8", errors="replace")
                banner += chunk
                if _AUTO_DISC_RE.search(banner):
                    self._log("[AUTO-DISC] Auto-Discovery prompt detected — answering 'y'.")
                    shell.send("y\n")
                    banner += "\ny\n"
                if _PROMPT_RE.search(banner):
                    break
            else:
                time.sleep(0.1)
        self._log(f"Banner: {banner.strip()[-300:]}")
        return shell

    def _ssh_send(
        self,
        shell: paramiko.Channel,
        command: str,
        delay: float = _SSH_CMD_DELAY,
        timeout: float = 60.0,
    ) -> str:
        """Send *command* over SSH shell; return accumulated output until next prompt."""
        self._log(f"[CMD] {command}")
        shell.send(command + "\n")
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.should_stop():
                return buf
            if shell.recv_ready():
                chunk = shell.recv(65535).decode("utf-8", errors="replace")
                buf += chunk
                # Auto-dismiss the Auto-Discovery termination prompt
                if _AUTO_DISC_RE.search(buf):
                    self._log("[AUTO-DISC] Auto-Discovery prompt detected — answering 'y'.")
                    shell.send("y\n")
                    buf += "\ny\n"
                    continue
                if "Press any key" in chunk:
                    shell.send(" ")
                    continue
                if _PROMPT_RE.search(buf):
                    break
            else:
                time.sleep(0.1)
        return buf

    # ── Re-connect helper (used for 7250 CLI mode switch) ───────────────────

    def _ssh_reconnect(self) -> Optional[paramiko.Channel]:
        """Close current SSH session and open a fresh one."""
        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass
            self.ssh_client = None
        time.sleep(3)
        return self._ssh_connect()

    # ── Provisioning steps ──────────────────────────────────────────────────

    def _provision_mdas(self, send_fn: Callable[[str], str]) -> bool:
        """
        Run ``show mda``, parse equipped types, then configure each MDA.

        Issues: ``configure card 1 mda <num> mda-type <equipped-type>``
        for every slot that has a physical MDA installed.

        Returns True if at least one MDA was configured (or if none were found
        but the show command succeeded), False on hard error.
        """
        self._log("Running 'show mda' to discover equipped MDAs...")
        raw = send_fn("show mda")
        self._log(f"show mda output:\n{raw}")

        mdas = _parse_show_mda(raw)
        self._log(f"[DEBUG] _parse_show_mda found {len(mdas)} MDA(s): {mdas}")

        if not mdas:
            self._warn(
                "No equipped MDAs detected in 'show mda' output. "
                "Skipping MDA provisioning — verify manually if needed."
            )
            return True  # non-fatal: device may have no MDAs yet

        for mda_num, equipped_type in mdas:
            cmd = f"configure card 1 mda {mda_num} mda-type {equipped_type}"
            self._log(f"Provisioning MDA {mda_num}: {equipped_type}")
            send_fn(cmd)

        self._log(f"MDA provisioning complete ({len(mdas)} MDA(s) configured).")
        return True

    def _provision_7705(self, send_fn: Callable[[str], str]) -> bool:
        """7705 SAR provisioning sequence."""
        self._log("── 7705 SAR Provisioning ──")

        self._log("Setting hostname...")
        send_fn(f'configure system name "{self.hostname}"')

        self._log("Setting BOF management IP...")
        send_fn(f"bof address {self.target_ip}/{self.prefix_len}")

        self._log("Setting static route...")
        send_fn(
            f"bof static-route {self.static_route_dest} next-hop {self.gateway}"
        )

        if self.configure_card:
            self._log("Configuring card type (iom-sar)...")
            send_fn(f'configure card 1 card-type "{CARD_TYPE[DEVICE_7705]}"')
            time.sleep(2)  # Allow card to initialize before querying MDAs

        self._provision_mdas(send_fn)

        self._log("Saving configuration...")
        send_fn(f"admin save cf3:/{self.hostname}.cfg")
        send_fn(f"bof primary-config cf3:/{self.hostname}.cfg")
        send_fn("bof save")

        if self.sync_redundancy:
            self._log("Syncing CPMs (this may take several minutes)...")
            send_fn("admin redundancy sync boot-env", timeout=600)

        return True

    def _provision_7250_serial(self, ser: serial.Serial) -> bool:
        """7250 IXR provisioning via serial.

        Phase 1 (MD-CLI / groff mode after factory login):
          Switch to classic CLI, then logout.
        Phase 2 (classic CLI after re-login):
          Set hostname, system BOF, initial save, BOF file entries,
          card/MDA config, final save + bof primary-config + bof save.
        """
        self._log("── 7250 IXR Provisioning (serial) ──")

        def send(cmd, timeout=60):
            return self._serial_send(ser, cmd, timeout=timeout)

        # ── Phase 1: Switch CLI mode ──────────────────────────────────────
        self._log("Switching to classic CLI mode (groff → classic)...")
        send("configure global")
        send("system management-interface configuration-save incremental-saves false")
        send("system management-interface configuration-mode classic")
        send("system management-interface cli cli-engine classic-cli")
        send("commit")
        send("exit")

        self._log("Logging out for CLI mode switch to take effect...")
        ser.write(b"logout\r")
        output, matched = self._serial_read_until(ser, [_LOGIN_RE], timeout=30)
        if not matched:
            self._warn("Login prompt not seen after logout; attempting to continue anyway.")

        # ── Phase 2: Re-login in classic CLI mode ─────────────────────────
        if not self._serial_login(ser):
            self._err("Re-login after CLI mode switch failed.")
            return False

        self._log("Locking classic CLI engine...")
        send("configure system management-interface cli cli-engine classic-cli")

        self._log("Setting hostname...")
        send(f"configure system name {self.hostname}")

        self._log("Setting system management IP...")
        send(f"bof address {self.target_ip}/{self.prefix_len}")

        self._log("Setting system static route...")
        send(f"bof static-route {self.static_route_dest} next-hop {self.gateway}")

        self._log("Saving initial configuration...")
        send(f"admin save cf3://{self.hostname}.cfg")

        self._log("Setting BOF management IP...")
        send(f"bof address {self.target_ip}/{self.prefix_len}")

        self._log("Setting BOF static route...")
        send(f"bof static-route {self.static_route_dest} next-hop {self.gateway}")

        if self.configure_card:
            self._log("Configuring card type (iom-ixr-r6)...")
            send(f'configure card 1 card-type "{CARD_TYPE[DEVICE_7250]}"')
            time.sleep(2)  # Allow card to initialize before querying MDAs

        self._provision_mdas(send)

        self._log("Saving final configuration...")
        send(f"admin save cf3:/{self.hostname}.cfg")
        send(f"bof primary-config cf3:/{self.hostname}.cfg")
        send("bof save")

        if self.sync_redundancy:
            self._log("Syncing CPMs (this may take several minutes)...")
            send("admin redundancy sync boot-env", timeout=600)

        return True

    def _provision_7250_ssh(self, shell: paramiko.Channel) -> Optional[paramiko.Channel]:
        """
        7250 IXR provisioning via SSH.

        Phase 1 (MD-CLI / groff mode after factory login):
          Switch to classic CLI, disconnect, then reconnect.
        Phase 2 (classic CLI after re-connection):
          Set hostname, system BOF, initial save, BOF file entries,
          card/MDA config, final save + bof primary-config + bof save.

        Returns the (possibly new) shell after re-connection, or None on failure.
        """
        self._log("── 7250 IXR Provisioning (SSH) ──")

        def send(cmd, timeout=60):
            return self._ssh_send(shell, cmd, timeout=timeout)

        # ── Phase 1: Switch CLI mode ──────────────────────────────────────
        self._log("Switching to classic CLI mode (groff → classic)...")
        send("configure global")
        send("system management-interface configuration-save incremental-saves false")
        send("system management-interface configuration-mode classic")
        send("system management-interface cli cli-engine classic-cli")
        send("commit")
        send("exit")

        self._log("Logging out and reconnecting for CLI mode switch...")
        shell.send("logout\n")
        shell.close()

        new_shell = self._ssh_reconnect()
        if new_shell is None:
            self._err("Re-connection after CLI mode switch failed.")
            return None

        def send2(cmd, timeout=60):
            return self._ssh_send(new_shell, cmd, timeout=timeout)

        # ── Phase 2: Classic CLI provisioning ─────────────────────────────
        self._log("Locking classic CLI engine...")
        send2("configure system management-interface cli cli-engine classic-cli")

        self._log("Setting hostname...")
        send2(f"configure system name {self.hostname}")

        self._log("Setting system management IP...")
        send2(f"bof address {self.target_ip}/{self.prefix_len}")

        self._log("Setting system static route...")
        send2(f"bof static-route {self.static_route_dest} next-hop {self.gateway}")

        self._log("Saving initial configuration...")
        send2(f"admin save cf3://{self.hostname}.cfg")

        self._log("Setting BOF management IP...")
        send2(f"bof address {self.target_ip}/{self.prefix_len}")

        self._log("Setting BOF static route...")
        send2(f"bof static-route {self.static_route_dest} next-hop {self.gateway}")

        if self.configure_card:
            self._log("Configuring card type (iom-ixr-r6)...")
            send2(f'configure card 1 card-type "{CARD_TYPE[DEVICE_7250]}"')
            time.sleep(2)  # Allow card to initialize before querying MDAs

        self._provision_mdas(send2)

        self._log("Saving final configuration...")
        send2(f"admin save cf3:/{self.hostname}.cfg")
        send2(f"bof primary-config cf3:/{self.hostname}.cfg")
        send2("bof save")

        if self.sync_redundancy:
            self._log("Syncing CPMs (this may take several minutes)...")
            send2("admin redundancy sync boot-env", timeout=600)

        return new_shell

    # ── Main entry point ────────────────────────────────────────────────────

    def run(self) -> bool:
        """
        Execute the full provisioning sequence.
        Returns True on success, False on failure.
        """
        # Validate required fields
        missing = [
            f for f, v in [
                ("hostname", self.hostname),
                ("target_ip", self.target_ip),
                ("gateway", self.gateway),
            ]
            if not v
        ]
        if missing:
            self._err(f"Missing required parameters: {', '.join(missing)}")
            return False

        if self.device_type not in (DEVICE_7705, DEVICE_7250):
            self._err(
                f"Unknown device type '{self.device_type}'. "
                "Expected '7705' or '7250'. "
                "Set Device Type Override in the GUI or rename the hostname to include _7705 or _7250."
            )
            return False

        self._log(
            f"Starting provisioning: {self.hostname} | "
            f"{self.target_ip}/{self.prefix_len} | gw {self.gateway} | "
            f"type {self.device_type} | via {self.connection_type}"
        )

        try:
            if self.connection_type == "serial":
                return self._run_serial()
            elif self.connection_type == "ssh":
                return self._run_ssh()
            else:
                self._err(f"Unknown connection type: {self.connection_type}")
                return False
        except Exception as exc:
            self._err(f"Provisioning failed with exception: {exc}")
            logging.exception("Provisioning exception")
            return False
        finally:
            self.abort_connection()

    def _run_serial(self) -> bool:
        if not self.serial_port:
            self._err("No serial port specified.")
            return False
        self._log(f"Opening serial port {self.serial_port} @ {self.baud_rate} baud...")
        try:
            self.serial_port_obj = serial.Serial(
                self.serial_port, self.baud_rate, timeout=self.timeout
            )
        except serial.SerialException as exc:
            self._err(f"Could not open {self.serial_port}: {exc}")
            return False

        ser = self.serial_port_obj
        if not self._serial_login(ser):
            return False

        if self.should_stop():
            return False

        if self.device_type == DEVICE_7705:
            success = self._provision_7705(
                lambda cmd, timeout=60: self._serial_send(ser, cmd, timeout=timeout)
            )
        else:
            success = self._provision_7250_serial(ser)

        if success:
            self._log(f"✔ Provisioning complete for {self.hostname}")
            self._log("You may now safely disconnect the console cable.")
        return success

    def _run_ssh(self) -> bool:
        if not self.ip_address:
            self._err("No IP address specified for SSH connection.")
            return False

        shell = self._ssh_connect()
        if shell is None:
            return False

        if self.should_stop():
            return False

        if self.device_type == DEVICE_7705:
            success = self._provision_7705(
                lambda cmd, timeout=60: self._ssh_send(shell, cmd, timeout=timeout)
            )
        else:
            result_shell = self._provision_7250_ssh(shell)
            success = result_shell is not None
            if result_shell is not None:
                try:
                    result_shell.close()
                except Exception:
                    pass

        if success:
            self._log(f"✔ Provisioning complete for {self.hostname}")
            self._log("You may now safely disconnect the console cable.")
        return success
