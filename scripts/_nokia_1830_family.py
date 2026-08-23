"""scripts/_nokia_1830_family.py — shared engine for the Nokia 1830 family.

The Nokia 1830 PSS, PSI (PSI-4L / PSI-8L) and PSS-class shelves are all the
same CLI platform. Historically ``scripts/Nokia_1830.py`` and
``scripts/Nokia_PSI.py`` were near-duplicate scripts: the SSH/Telnet login,
the drain/abort plumbing and the orchestration were copied byte-for-byte, and
only the command list + a handful of output parsers differed. This module is
the single engine both now subclass — one place to fix login bugs, one place
to maintain the plumbing.

LOGIN (the important part)
--------------------------
SSH-layer auth is admin/admin (``nokia_ssh_authenticate`` — with legacy
fallbacks). The shelf's own login then runs *on the shell channel*, and
:meth:`_settle_shell` answers whichever prompt appears, matching the field
transcript exactly::

    <host> login: cli
    Username: admin
    Password: <admin>
    <alarm/MOTD banner>
    <host>#

i.e. the getty ``login:`` is answered with ``cli`` (it is typed at the prompt,
NOT used as an SSH username), then ``Username:`` / ``Password:`` with
admin/admin.

Transport per variant:
  * **PSI** (``scripts/Nokia_PSI.py``) runs this two-step over **Telnet** — SSH
    auth as ``admin`` dead-ends at the alarm banner (admin is not the CLI
    account), whereas the Telnet getty ``login:`` reliably drives the CLI. LAN
    PSI is routed to Telnet in ``gui4_0._lan_connection_types`` and its target
    IP is auto-allowlisted at build time.
  * **PSS** (``scripts/Nokia_1830.py``) uses SSH; :meth:`_settle_shell` drives
    the same dialog if the shelf presents it on the SSH channel.

Both transports share :meth:`telnet_login` (getty three-stage) and
:meth:`_settle_shell` (the SSH-channel equivalent).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import paramiko

from script_interface import (
    BaseScript,
    DatabaseCache,
    get_inventory_db_path,
    get_tracker,
    NEEDS_CREDENTIALS_SENTINEL,
)
from utils.helpers import ensure_host_key_known, nokia_ssh_authenticate
from utils.serial_helpers import capture_until_prompt, serial_getty_login
from utils.telnet import Telnet

try:  # pragma: no cover - import shim only
    from wexpect import TIMEOUT  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    from pexpect import TIMEOUT  # type: ignore[import-not-found]


class Nokia1830FamilyScript(BaseScript):
    """Shared SSH/Telnet engine for Nokia 1830 / PSI / PSS inventory.

    Subclasses declare:
      * ``LABEL``      — short tag used in log lines (e.g. "1830", "PSI").
      * ``COMMANDS``   — the ordered CLI command list for this variant.
      * ``_pipeline()``— the ordered parser callables matching ``COMMANDS``.
    """

    LABEL: str = "1830-family"
    COMMANDS: List[str] = []

    # ── login prompt matchers (shared) ──────────────────────────────────────
    # All end-anchored (MULTILINE) so we react to the CURRENT prompt at the tail
    # of the buffer, not a stale one earlier in the banner. The getty "login:"
    # is answered with "cli"; the inner "Username:"/"Password:" with admin/admin.
    # end-anchoring keeps the banner's "Last Login:" line from matching login:.
    # NOT multiline: anchor to the END of the accumulated buffer so we react to
    # the CURRENT prompt only. (MULTILINE would keep matching the earlier
    # "login:" line after we'd already advanced to "Username:".)
    _SHELL_PROMPT_RE = re.compile(r"[#>$]\s*$")
    _GETTY_LOGIN_RE = re.compile(r"(?i)(?:^|\s)login\s*:\s*$")
    _USERNAME_RE = re.compile(r"(?i)username\s*:\s*$")
    _PASSWORD_RE = re.compile(r"(?i)password\s*:\s*$")
    _ACK_RE = re.compile(
        r"(?i)\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|continue\?|accept\?|press\s+y"
    )
    _AUTH_FAIL_RE = re.compile(r"(?i)(login incorrect|access denied|authentication fail|invalid password)")

    def __init__(
        self,
        *,
        connection_type: str = "telnet",
        command_tracker=None,
        ip_address=None,
        username: str = "admin",
        password: str = "admin",
        timeout: int = 5,
        db_path: Optional[str] = None,
        db_cache=None,
        stop_callback: Optional[Callable[[], bool]] = None,
        serial_port: Optional[str] = None,
        baud_rate: Optional[int] = None,
    ) -> None:
        # --- DB wiring ---
        if db_cache is not None:
            self.db_cache = db_cache
            self.db_path = db_cache.db_path
        else:
            if db_path is None:
                db_path = get_inventory_db_path()
            db_path = os.path.abspath(db_path)
            if not os.path.exists(db_path):
                raise FileNotFoundError(f"Database file missing at: {db_path}")
            self.db_cache = DatabaseCache(db_path)
            self.db_path = db_path

        # --- misc wiring ---
        self.connection_type = connection_type
        self.command_tracker = command_tracker or get_tracker()
        self.telnet = None
        self.child = None
        self._ssh_transport = None
        self._ssh_channel = None
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.timeout = timeout
        self.port = 22
        self.stop_callback = stop_callback
        # Serial console (getty two-step) support.
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.serial_port_obj = None

        if self.connection_type == "serial":
            if not self.serial_port:
                raise ValueError("Missing required 'serial_port' for serial connection.")
        elif not self.ip_address:
            raise ValueError("Missing required 'ip_address' for network-based connection.")

    # ── Inner shell (two-step) credentials ──────────────────────────────────
    # The outer SSH login uses the passwordless 'cli' bootstrap account; the
    # shelf's own inner Username:/Password: dialog wants the OPERATOR account.
    # These are read dynamically off self.username/self.password so the
    # credential-rotation retry path (which mutates those) is honoured. When
    # the outer SSH user is 'cli', the operator account is 'admin'.

    @property
    def inner_username(self) -> str:
        return "admin" if str(self.username).lower() == "cli" else self.username

    @property
    def inner_password(self) -> str:
        return self.password

    # ------------------------------------------------------------------
    # Abort / stop helpers
    # ------------------------------------------------------------------

    def abort_connection(self):
        """Forcefully tear down any live SSH/Telnet/Serial I/O to interrupt a run."""
        if self.serial_port_obj:
            try:
                self.serial_port_obj.close()
                logging.info("Serial connection forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing serial: {e}")
            finally:
                self.serial_port_obj = None
        if self._ssh_channel:
            try:
                self._ssh_channel.close()
            except Exception as e:
                logging.debug(f"Error force-closing SSH channel: {e}")
            finally:
                self._ssh_channel = None
        if self._ssh_transport:
            try:
                self._ssh_transport.close()
            except Exception as e:
                logging.debug(f"Error force-closing SSH transport: {e}")
            finally:
                self._ssh_transport = None
        if self.child:
            try:
                self.child.close(force=True)
                logging.debug("SSH spawn process forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing spawn: {e}")
            finally:
                self.child = None
        if self.telnet:
            try:
                self.telnet.close()
                logging.info("Telnet connection forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing telnet: {e}")
            finally:
                self.telnet = None

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    def sleep_with_abort(self, seconds: float, interval: float = 0.1) -> bool:
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.should_stop():
                return True
            time.sleep(min(interval, end_time - time.time()))
        return self.should_stop()

    def expect_with_abort(self, child, patterns, timeout=30, step=1):
        elapsed = 0
        while elapsed < timeout:
            if self.should_stop():
                return None
            try:
                return child.expect(patterns, timeout=min(step, timeout - elapsed))
            except TIMEOUT:
                elapsed += min(step, timeout - elapsed)
        raise TIMEOUT("Timeout waiting for device response")

    # ------------------------------------------------------------------
    # Command list (variant-supplied)
    # ------------------------------------------------------------------

    def get_commands(self) -> List[str]:
        return list(self.COMMANDS)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        if self.connection_type == "serial":
            return self.execute_serial_commands(commands)
        if self.connection_type == "ssh":
            outputs, error = self.execute_ssh_commands(commands)
            if error and error not in ("Aborted", NEEDS_CREDENTIALS_SENTINEL):
                logging.warning(
                    f"SSH failed for {self.ip_address}: {error}. Falling back to Telnet."
                )
                return self._execute_telnet_commands(commands)
            return outputs, error
        return self._execute_telnet_commands(commands)

    def _execute_telnet_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        outputs = []
        for command in commands:
            if self.should_stop():
                self.close_telnet()
                return outputs, "Aborted"
            if self.command_tracker.has_executed(self.ip_address, command, "telnet"):
                logging.debug(f"Skipping previously executed command: {command}")
                continue
            output, error = self.execute_telnet_command(command)
            if error:
                logging.error(f"Error executing command '{command}': {error}")
                outputs.append(None)
            else:
                outputs.append(output)
                self.command_tracker.mark_as_executed(self.ip_address, command, "telnet")
        self.close_telnet()
        return outputs, None if all(outputs) else "Some commands failed"

    # ── SSH drain ───────────────────────────────────────────────────────────

    def _drain(self, channel, timeout: float = 8.0, idle: float = 1.5) -> str:
        """Read from an invoke_shell channel until it goes idle (or times out)."""
        buf = ""
        deadline = time.time() + timeout
        last_recv = time.time()
        while time.time() < deadline:
            if self.should_stop():
                return buf
            if channel.recv_ready():
                buf += channel.recv(4096).decode("utf-8", errors="replace")
                last_recv = time.time()
            elif time.time() - last_recv >= idle:
                break
            else:
                time.sleep(0.1)
        return buf

    # ── SSH authentication ───────────────────────────────────────────────────

    def _connect_transport(self) -> paramiko.Transport:
        sock = socket.create_connection((self.ip_address, self.port), timeout=30)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=30)
        return transport

    def _open_shell(self, transport: paramiko.Transport):
        channel = transport.open_session()
        channel.get_pty()
        channel.invoke_shell()
        return channel

    def _settle_shell(self, channel) -> Tuple[bool, str]:
        """Drive the post-auth login dialog to a shell prompt.

        Answers whichever prompt the shelf presents at the tail of the buffer,
        matching the field two-step login exactly::

            <host> login: cli
            Username: admin
            Password: <admin>
            <alarm/MOTD banner>
            <host>#

        i.e. getty ``login:`` -> ``cli``, then ``Username:`` -> ``admin`` and
        ``Password:`` -> ``admin``, answer any Y/n EULA banner, and settle on the
        shell prompt. Returns ``(ok, accumulated_text)``.

        NB: the LAN PSI path runs this two-step over Telnet (``telnet_login``),
        where the getty ``login:`` reliably appears; this SSH variant handles the
        same dialog if the shelf presents it on the SSH channel.
        """
        post = self._drain(channel, timeout=8.0, idle=1.5)
        sent = {"login": 0, "user": 0, "pass": 0, "ack": 0}

        for _ in range(10):
            if self.should_stop():
                return False, post
            if self._SHELL_PROMPT_RE.search(post):
                return True, post
            if self._AUTH_FAIL_RE.search(post):
                logging.warning(f"[{self.LABEL}] Login rejected: {post[-160:]!r}")
                return False, post

            if self._GETTY_LOGIN_RE.search(post) and sent["login"] < 2:
                logging.info(f"[{self.LABEL}] getty login: — sending 'cli'")
                channel.send("cli\n")
                sent["login"] += 1
            elif self._USERNAME_RE.search(post) and sent["user"] < 2:
                logging.info(f"[{self.LABEL}] Username: — sending '{self.inner_username}'")
                channel.send(f"{self.inner_username}\n")
                sent["user"] += 1
            elif self._PASSWORD_RE.search(post) and sent["pass"] < 2:
                logging.info(f"[{self.LABEL}] Password: — sending inner password")
                channel.send(f"{self.inner_password}\n")
                sent["pass"] += 1
            elif self._ACK_RE.search(post) and sent["ack"] < 3:
                logging.info(f"[{self.LABEL}] Acknowledging post-login Y/n banner")
                channel.send("y\n")
                sent["ack"] += 1
            else:
                break

            post += self._drain(channel, timeout=8.0, idle=1.2)

        return bool(self._SHELL_PROMPT_RE.search(post)), post

    def execute_ssh_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        """Connect via SSH, drive the two-step login, run each command."""
        # Validate IP + username (injection guard) before anything else.
        try:
            ipaddress.ip_address(str(self.ip_address))
        except ValueError:
            return [], f"Invalid IP address format: {self.ip_address}"
        if not re.match(r"^[a-zA-Z0-9._-]+$", self.username):
            return [], f"Invalid username format: {self.username}"

        if not ensure_host_key_known(str(self.ip_address), port=self.port):
            return [], (
                f"SSH host key verification failed or rejected for "
                f"{self.ip_address}:{self.port}"
            )

        transport = channel = None
        try:
            transport = self._connect_transport()
            # SSH-layer auth: admin/admin (with the helper's legacy fallbacks).
            # The getty 'cli' / inner Username:/Password: two-step is driven on
            # the shell channel by _settle_shell, not here.
            try:
                nokia_ssh_authenticate(transport, self.username, self.password)
            except paramiko.AuthenticationException:
                try:
                    transport.close()
                except Exception:
                    pass
                return [], NEEDS_CREDENTIALS_SENTINEL
            logging.info(f"[{self.LABEL}] SSH auth succeeded for {self.username}@{self.ip_address}")
            self._ssh_transport = transport
            channel = self._open_shell(transport)
            self._ssh_channel = channel

            ok, post = self._settle_shell(channel)
            if not ok:
                return [], f"[{self.LABEL}] No shell prompt after login (got: {post[-200:]!r})"

            prompt_match = re.search(r"(\S+[#>])\s*$", post)
            logging.debug(
                f"[{self.LABEL}] Shell prompt detected: "
                f"{prompt_match.group(1) if prompt_match else '#'!r}"
            )

            output_log: List[str] = []
            for cmd in commands:
                if self.should_stop():
                    return output_log, "Aborted"
                logging.debug(f"[{self.LABEL}] Sending command: {cmd}")
                channel.send(f"{cmd}\n")
                raw = self._drain(channel, timeout=30.0, idle=2.0)
                lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                # Strip the command echo line and any trailing prompt line.
                cleaned = [
                    l
                    for l in lines
                    if l.strip() and cmd.strip() not in l and not re.search(r"\S+[#>]\s*$", l)
                ]
                output_log.append("\n".join(cleaned).strip())
                self.command_tracker.mark_as_executed(self.ip_address, cmd, "ssh")

            channel.send("exit\n")
            self._drain(channel, timeout=5.0, idle=1.0)
            return output_log, None

        except Exception as e:
            logging.exception(f"[{self.LABEL}] SSH execution exception for {self.ip_address}")
            return [], str(e)
        finally:
            self._ssh_channel = None
            self._ssh_transport = None
            if channel:
                try:
                    channel.close()
                except Exception:
                    pass
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Telnet helpers (three-stage login: login: -> cli -> Username -> Password)
    # ------------------------------------------------------------------

    def telnet_login(self, retries: int = 2) -> bool:
        if self.telnet:
            return True

        for attempt in range(1, retries + 1):
            temp_telnet = None
            try:
                if self.should_stop():
                    return False
                logging.info(f"Connecting to {self.ip_address} via Telnet (Attempt {attempt})...")
                # skip_ssh_probe: these shelves keep SSH open on :22, but SSH-as-
                # admin dead-ends at a banner — the usable CLI is only reachable
                # via this Telnet getty two-step. Keep the allowlist gate; opt out
                # of the "prefer SSH" refusal only.
                temp_telnet = Telnet(
                    self.ip_address, timeout=self.timeout,
                    skip_ssh_probe=True, purpose="nokia-1830-getty",
                )

                temp_telnet.read_until(b"login: ", timeout=5)
                temp_telnet.write(b"cli\n")
                temp_telnet.read_until(b"Username: ", timeout=5)
                temp_telnet.write(self.inner_username.encode("ascii") + b"\n")
                temp_telnet.read_until(b"Password: ", timeout=5)
                temp_telnet.write(self.inner_password.encode("ascii") + b"\n")
                if self.sleep_with_abort(1):
                    try:
                        temp_telnet.close()
                    except Exception:
                        pass
                    return False

                login_response = temp_telnet.read_very_eager().decode("ascii")
                if "Login incorrect" in login_response or "invalid" in login_response.lower():
                    logging.error("Telnet login failed: Invalid credentials.")
                    try:
                        temp_telnet.close()
                    except Exception:
                        pass
                    continue

                logging.info("Telnet login successful.")
                self.telnet = temp_telnet
                return True
            except Exception as e:
                logging.error(f"Telnet login attempt {attempt} failed: {e}")
                if temp_telnet is not None:
                    try:
                        temp_telnet.close()
                    except Exception as close_err:
                        logging.debug(f"Error closing Telnet after failed login: {close_err}")

        return False

    def execute_telnet_command(self, command: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            if not self.telnet_login():
                return None, "Aborted" if self.should_stop() else "Telnet login failed."

            logging.debug(f"Executing command: {command}")
            self.telnet.write(command.encode("ascii") + b"\n")
            if self.sleep_with_abort(3.5):
                return None, "Aborted"
            output = self.capture_full_output_telnet()
            if self.should_stop():
                return None, "Aborted"
            return output, None
        except Exception as e:
            logging.error(f"Telnet command failed: {e}")
            return None, str(e)

    def capture_full_output_telnet(self) -> str:
        try:
            while not self.should_stop():
                output = self.telnet.read_until(b"#", timeout=1).decode("ascii")
                if output:
                    return output.strip()
            return ""
        except Exception as e:
            logging.error(f"Error capturing Telnet output: {e}")
            return ""

    def close_telnet(self):
        if self.telnet:
            try:
                self.telnet.write(b"exit\n")
                self.telnet.close()
                logging.info("Telnet session closed.")
            except Exception as e:
                logging.warning(f"Failed to close Telnet gracefully: {e}")
            finally:
                self.telnet = None

    def close_telnet_force(self):
        if self.telnet:
            try:
                self.telnet.close()
                logging.info("Telnet session force-closed.")
            except Exception as e:
                logging.debug(f"Error force-closing telnet: {e}")
            finally:
                self.telnet = None

    # ------------------------------------------------------------------
    # Serial console (getty two-step login)
    # ------------------------------------------------------------------

    def execute_serial_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        """Run the command set over a serial console.

        The 1830 family presents the same getty two-step on the console as over
        Telnet: ``login:`` -> ``cli``, ``Username:`` -> ``admin``,
        ``Password:`` -> ``admin``, then the shell prompt. Driven by
        :func:`utils.serial_helpers.serial_getty_login`.
        """
        import serial  # local import: pyserial optional at module load

        try:
            self.serial_port_obj = serial.Serial(
                self.serial_port, self.baud_rate, timeout=self.timeout
            )
            logging.info(f"[{self.LABEL}] Connected to serial port {self.serial_port}")
        except Exception as e:
            logging.error(f"[{self.LABEL}] Serial open failed on {self.serial_port}: {e}")
            self.serial_port_obj = None
            return [], str(e)

        try:
            ok = serial_getty_login(
                self.serial_port_obj,
                login_name="cli",
                shell_user=self.inner_username,
                shell_pass=self.inner_password,
                timeout=10.0,
                should_stop=self.should_stop,
            )
            if not ok:
                if self.should_stop():
                    return [], "Aborted"
                return [], (
                    f"[{self.LABEL}] Serial getty login did not reach a shell "
                    f"prompt on {self.serial_port}"
                )
            logging.info(f"[{self.LABEL}] Serial getty login OK on {self.serial_port}")

            outputs: List[str] = []
            for command in commands:
                if self.should_stop():
                    return outputs, "Aborted"
                output = capture_until_prompt(
                    self.serial_port_obj, command, timeout=20.0,
                    should_stop=self.should_stop,
                )
                if output is None:
                    if self.should_stop():
                        return outputs, "Aborted"
                    outputs.append(None)
                else:
                    outputs.append(output)
            return outputs, None if all(outputs) else "Some commands failed"
        except Exception as e:
            logging.exception(f"[{self.LABEL}] Serial execution exception on {self.serial_port}")
            return [], str(e)
        finally:
            if self.serial_port_obj:
                try:
                    self.serial_port_obj.close()
                except Exception:
                    pass
                self.serial_port_obj = None

    # ------------------------------------------------------------------
    # DB helper
    # ------------------------------------------------------------------

    def get_part_description(self, part_number: str) -> str:
        return self.db_cache.lookup_part(part_number[:10])

    # ==================================================================
    # PSI parser set (canonical / richer). Used by the PSI variant and any
    # future 1830 variant that wants the full read-out.
    # ==================================================================

    def extract_shelf_detail(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse shelf identity from ``show general system-identification``
        (Vendor/Product/Shelf type), with fallback to the legacy
        ``show shelf 1`` layout."""
        system_data = []
        try:
            output = output.strip()
            name_match = re.search(r"Name\s*:\s*(.+)", output)
            product_match = re.search(r"Product\s*:\s*(.+)", output)
            shelf_type_match = re.search(r"Shelf\s*type\s*:\s*(.+)", output, re.IGNORECASE)
            type_match = re.search(r"Programmed Type\s*:\s*(.+)", output)

            prompt_name_match = re.search(
                r"^\s*([A-Za-z0-9._-]+)#\s*show\s+general\s+system-identification",
                output,
                re.IGNORECASE | re.MULTILINE,
            )

            if name_match:
                system_name = name_match.group(1).strip()
            elif prompt_name_match:
                system_name = prompt_name_match.group(1).strip()
            elif product_match:
                system_name = f"Nokia {product_match.group(1).strip()}"
            else:
                system_name = "Unknown"

            if shelf_type_match:
                system_type = shelf_type_match.group(1).strip()
            elif type_match:
                system_type = type_match.group(1).strip()
            else:
                system_type = "Unknown"

            system_data.append({
                "System Name": system_name,
                "System Type": system_type,
                "Type": "Shelf",
                "Part Number": "",
                "Serial Number": "",
                "Description": system_type,
                "Name": system_name,
                "Source": ip or "Unknown",
            })
            if re.search(r"^PSI-(4L|8L)$", system_type, re.IGNORECASE):
                logging.info(f"[{self.LABEL}] Shelf type detected: {system_type}")
            logging.info(f"Extracted shelf detail — Name: {system_name}, Type: {system_type}")
        except Exception as e:
            logging.error(f"Error in extract_shelf_detail: {e}")
            system_data.append({
                "System Name": "Error", "System Type": "Error", "Type": "Error",
                "Part Number": "Error", "Serial Number": "Error",
                "Description": "Error", "Name": "Error", "Source": ip or "Unknown",
            })

        df = pd.DataFrame(system_data)
        if cache_callback:
            cache_callback(df, "shelf_detail")
        print(df.to_string(index=False))
        return df

    def extract_shelf_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show shelf inventory *':
        shelf_num  shelf_type  part_number  serial_number  [clei]"""
        shelf_data = []
        try:
            output = output.strip()
            pattern = re.compile(
                r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+))?",
                re.MULTILINE,
            )
            for match in pattern.finditer(output):
                try:
                    shelf_num = match.group(1).strip()
                    shelf_type = match.group(2).strip()
                    part_number = match.group(3).strip()
                    serial_number = match.group(4).strip()

                    description = self.db_cache.lookup_part(part_number[:10])
                    shelf_data.append({
                        "System Name": "",
                        "System Type": shelf_type,
                        "Type": "Shelf",
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": f"Shelf {shelf_num}",
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing shelf inventory row: {me}")

            if not shelf_data:
                logging.warning("No shelf inventory data found.")
        except Exception as e:
            logging.error(f"Error in extract_shelf_inventory: {e}")

        df = pd.DataFrame(shelf_data)
        if cache_callback:
            cache_callback(df, "shelf_inventory")
        print(df.to_string(index=False))
        return df

    def extract_card_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show card inventory *':
        location  card_type  mnemonic  part_number  serial_number  ..."""
        card_data = []
        try:
            output = output.replace("Press any key to continue (Q to quit)", "").strip()
            pattern = re.compile(
                r"^\s*(\d+/\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)",
                re.MULTILINE,
            )
            for match in pattern.finditer(output):
                try:
                    location = match.group(1).strip()
                    mnemonic = match.group(3).strip()
                    part_number = match.group(4).strip()
                    serial_number = match.group(5).strip()

                    if part_number.upper() in ("PART", "NUMBER", "TYPE"):
                        continue

                    description = self.db_cache.lookup_part(part_number[:10])
                    card_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": mnemonic.title(),
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": location,
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing card inventory row: {me}")

            if not card_data:
                logging.warning("No card inventory data found.")
        except Exception as e:
            logging.error(f"Error in extract_card_inventory: {e}")

        df = pd.DataFrame(card_data)
        if cache_callback:
            cache_callback(df, "card_inventory")
        print(df.to_string(index=False))
        return df

    def extract_module_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show interface inventory *' (module / transceiver rows):
        location  module_type  part_number  serial_number"""
        module_data = []
        try:
            output = output.strip()
            pattern = re.compile(
                r"^\s*(\d+/\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
                re.MULTILINE,
            )
            for match in pattern.finditer(output):
                try:
                    location = match.group(1).strip()
                    module_type = match.group(2).strip()
                    part_number = match.group(3).strip()
                    serial_number = match.group(4).strip()

                    description = self.db_cache.lookup_part(part_number[:10])
                    module_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": module_type.title(),
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": f"Module {location}",
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing module inventory row: {me}")

            if not module_data:
                logging.warning("No module inventory data found.")
        except Exception as e:
            logging.error(f"Error in extract_module_inventory: {e}")

        df = pd.DataFrame(module_data)
        if cache_callback:
            cache_callback(df, "module_inventory")
        print(df.to_string(index=False))
        return df

    def extract_software_info(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show software dynamic' — release and RPMS counts."""
        sw_data = []
        try:
            output = output.strip()
            release_match = re.search(r"Release\s*:\s*(\S+)", output)
            total_match = re.search(r"Total RPMS in load\s*[:\s]+(\d+)", output)
            loaded_match = re.search(r"RPMS Loaded\s*[:\s]+(\d+)", output)

            release = release_match.group(1).strip() if release_match else "Unknown"
            total_rpms = total_match.group(1).strip() if total_match else "Unknown"
            rpms_loaded = loaded_match.group(1).strip() if loaded_match else "Unknown"

            sw_data.append({
                "System Name": "",
                "System Type": "",
                "Type": "Software",
                "Part Number": release,
                "Serial Number": "",
                "Description": f"RPMS Loaded: {rpms_loaded} / {total_rpms}",
                "Name": "SW Release",
                "Source": ip or "Unknown",
            })
            logging.info(f"Software release: {release}, RPMS: {rpms_loaded}/{total_rpms}")
        except Exception as e:
            logging.error(f"Error in extract_software_info: {e}")

        df = pd.DataFrame(sw_data)
        if cache_callback:
            cache_callback(df, "software_info")
        print(df.to_string(index=False))
        return df

    def extract_slot_info(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show slot *' — slot programming and operational state."""
        slot_data = []
        try:
            state_values = {"up", "down", "empty"}
            for raw_line in output.strip().splitlines():
                line = raw_line.strip()
                if not line or not re.match(r"^\d+/\d+\s+", line):
                    continue

                tokens = line.split()
                if len(tokens) < 5:
                    continue

                admin_index = None
                for idx in range(3, len(tokens) - 1):
                    if tokens[idx].lower() in state_values and tokens[idx + 1].lower() in state_values:
                        admin_index = idx
                        break

                if admin_index is None or admin_index < 2:
                    continue

                try:
                    slot = tokens[0].strip()
                    prog_type = tokens[1].strip()
                    pres_type = " ".join(tokens[2:admin_index]).strip()
                    admin_state = tokens[admin_index].strip()
                    oper_state = tokens[admin_index + 1].strip()
                    qualifier = " ".join(tokens[admin_index + 2:]).strip()

                    slot_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": "Slot",
                        "Part Number": prog_type,
                        "Present Type": pres_type,
                        "Serial Number": "",
                        "Description": f"Admin: {admin_state} | Oper: {oper_state}"
                        + (f" | {qualifier}" if qualifier else ""),
                        "Name": f"Slot {slot}",
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing slot row: {me}")

            if not slot_data:
                logging.warning("No slot data found.")
        except Exception as e:
            logging.error(f"Error in extract_slot_info: {e}")

        df = pd.DataFrame(slot_data)
        if cache_callback:
            cache_callback(df, "slot_info")
        print(df.to_string(index=False))
        return df

    def extract_redundancy_info(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show redundancy 1 detail' — clock switch and EC selection."""
        redun_data = []
        try:
            output = output.strip()
            clock_match = re.search(r"Clock Switch\s*[:\s]+(\S+)", output)
            ec_match = re.search(r"EC Selection\s*[:\s]+(\S+)", output)

            clock_switch = clock_match.group(1).strip() if clock_match else "Unknown"
            ec_selection = ec_match.group(1).strip() if ec_match else "Unknown"

            redun_data.append({
                "System Name": "",
                "System Type": "",
                "Type": "Redundancy",
                "Part Number": "",
                "Serial Number": "",
                "Description": f"Clock Switch: {clock_switch} | EC Selection: {ec_selection}",
                "Name": "Redundancy",
                "Source": ip or "Unknown",
            })
        except Exception as e:
            logging.error(f"Error in extract_redundancy_info: {e}")

        df = pd.DataFrame(redun_data)
        if cache_callback:
            cache_callback(df, "redundancy_info")
        print(df.to_string(index=False))
        return df

    def extract_power_info(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show pf *' — power feed admin/oper state."""
        pf_data = []
        try:
            output = output.strip()
            pattern = re.compile(
                r"^\s*(\d+/\d+)\s+\S+\s+(Up|Down)\s+(Up|Down)",
                re.MULTILINE | re.IGNORECASE,
            )
            for match in pattern.finditer(output):
                try:
                    slot = match.group(1).strip()
                    admin_state = match.group(2).strip()
                    oper_state = match.group(3).strip()

                    pf_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": "Power Feed",
                        "Part Number": "",
                        "Serial Number": "",
                        "Description": f"Admin: {admin_state} | Oper: {oper_state}",
                        "Name": f"PF {slot}",
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing power feed row: {me}")

            if not pf_data:
                logging.warning("No power feed data found.")
        except Exception as e:
            logging.error(f"Error in extract_power_info: {e}")

        df = pd.DataFrame(pf_data)
        if cache_callback:
            cache_callback(df, "power_info")
        print(df.to_string(index=False))
        return df

    def extract_topology(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show interface topology *' — port connectivity."""
        topo_data = []
        try:
            for raw_line in output.strip().splitlines():
                line = raw_line.strip()
                if not line or not re.match(r"^\d+/\S+", line):
                    continue

                tokens = line.split()
                if len(tokens) < 2:
                    continue

                try:
                    interface = tokens[0].strip()
                    interface_type = tokens[1].strip()

                    if len(tokens) == 2:
                        connected_to = ""
                        type_from = ""
                    elif interface_type == "-" and len(tokens) >= 5 and tokens[2] == "Ext":
                        connected_to = " ".join(tokens[2:4]).strip()
                        type_from = " ".join(tokens[4:]).strip()
                    else:
                        connected_to = tokens[2].strip() if len(tokens) >= 3 else ""
                        type_from = " ".join(tokens[3:]).strip() if len(tokens) >= 4 else ""

                    topo_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": interface_type,
                        "Part Number": "",
                        "Serial Number": "",
                        "Description": f"Connected To: {connected_to} | From: {type_from}",
                        "Name": f"If {interface}",
                        "Source": ip or "Unknown",
                    })
                except Exception as me:
                    logging.error(f"Error processing topology row: {me}")

            if not topo_data:
                logging.warning("No topology data found.")
        except Exception as e:
            logging.error(f"Error in extract_topology: {e}")

        df = pd.DataFrame(topo_data)
        if cache_callback:
            cache_callback(df, "topology")
        print(df.to_string(index=False))
        return df

    # ==================================================================
    # Legacy 1830 parser set. The core inventory commands are identical to the
    # PSI variant, but the 1830 report uses a slightly different system-name
    # command and Name-column conventions, so these are preserved verbatim to
    # keep existing 1830 reports byte-identical.
    # ==================================================================

    def extract_system_name(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show general name' — the system (host) name."""
        system_data = []
        try:
            logging.debug(f"Raw output: {output}")
            output = output.strip()
            system_name_pattern = re.compile(r"Name:\s+([A-Za-z0-9_-]+)", re.MULTILINE | re.DOTALL)
            match = system_name_pattern.search(output)
            if match:
                system_name = match.group(1).strip()
                logging.debug(f"Extracted System Name: {system_name}")
                system_data.append({"System Name": system_name, "Source": ip or "Unknown"})
            else:
                logging.warning("No system name found in output.")
                system_data.append({"System Name": "Unknown", "Source": ip or "Unknown"})
        except Exception as e:
            logging.error(f"Error in extract_system_name: {e}")
            system_data.append({"System Name": "Error", "Source": ip or "Unknown"})

        df = pd.DataFrame(system_data)
        if cache_callback:
            cache_callback(df, "system_name")
        print(df.to_string(index=False))
        return df

    def extract_shelf_inventory_1830(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Legacy 1830 'show shelf inventory *' parser (type-keyed Name)."""
        shelf_data = []
        try:
            logging.debug(f"Raw output: {output}")
            output = output.strip()
            shelf_pattern = re.compile(
                r"^\s*\d+\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)", re.MULTILINE | re.DOTALL
            )
            for match in re.finditer(shelf_pattern, output):
                try:
                    shelf_type = match.group(1).strip()
                    part_number = match.group(2).strip()
                    serial_number = match.group(3).strip()

                    description = self.db_cache.lookup_part(part_number)
                    shelf_data.append({
                        "System Name": "",
                        "System Type": shelf_type,
                        "Type": shelf_type.title(),
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": f"Shelf {shelf_type}",
                        "Source": ip or "Unknown",
                    })
                except Exception as match_error:
                    logging.error(f"Error processing shelf inventory match: {match_error}")
                    continue

            if not shelf_data:
                logging.warning("No shelf inventory data found in output.")
        except Exception as e:
            logging.error(f"Error in extract_shelf_inventory: {e}")
            shelf_data.append({
                "System Name": "", "System Type": "", "Type": "Error",
                "Part Number": "Error", "Serial Number": "Error",
                "Description": "Error", "Name": "Error", "Source": ip or "Unknown",
            })

        df = pd.DataFrame(shelf_data)
        if cache_callback:
            cache_callback(df, "shelf_inventory")
        print(df.to_string(index=False))
        return df

    def extract_card_inventory_1830(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Legacy 1830 'show card inventory *' parser."""
        card_data = []
        try:
            logging.debug(f"Raw output: {output}")
            output = output.replace("Press any key to continue (Q to quit)", "").strip()
            card_pattern = re.compile(
                r"^\s*(\d+\/\d+)\s+[^\s]+\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)", re.MULTILINE
            )
            for match in re.finditer(card_pattern, output):
                try:
                    slot = match.group(1).strip()
                    card_type = match.group(2).strip()
                    part_number = match.group(3).strip()
                    serial_number = match.group(4).strip()

                    description = self.db_cache.lookup_part(part_number)
                    card_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": card_type.title(),
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": slot,
                        "Source": ip or "Unknown",
                    })
                except Exception as match_error:
                    logging.error(f"Error processing card match: {match_error}")
                    continue

            if not card_data:
                logging.warning("No card data found in output.")
        except Exception as e:
            logging.error(f"Error in Card Inventory: {e}")
            card_data.append({
                "System Name": "", "System Type": "", "Type": "Error",
                "Part Number": "Error", "Serial Number": "Error",
                "Description": "Error", "Name": "Error", "Source": ip or "Unknown",
            })

        df = pd.DataFrame(card_data)
        if cache_callback:
            cache_callback(df, "card data")
        print(df.to_string(index=False))
        return df

    def extract_interface_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Legacy 1830 'show interface inventory *' parser (Port-keyed Name)."""
        interface_data = []
        try:
            logging.debug(f"Raw output: {output}")
            output = output.strip()
            interface_pattern = re.compile(
                r"^\s*(\d+\/\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", re.MULTILINE
            )
            for match in re.finditer(interface_pattern, output):
                try:
                    location = match.group(1).strip()
                    module_type = match.group(2).strip()
                    part_number = match.group(3).strip()
                    serial_number = match.group(4).strip()

                    description = self.db_cache.lookup_part(part_number)
                    interface_data.append({
                        "System Name": "",
                        "System Type": "",
                        "Type": module_type.title(),
                        "Part Number": part_number[:10],
                        "Serial Number": serial_number,
                        "Description": description,
                        "Name": f"Port {location}",
                        "Source": ip or "Unknown",
                    })
                except Exception as match_error:
                    logging.error(f"Error processing interface inventory match: {match_error}")
                    continue

            if not interface_data:
                logging.warning("No interface inventory data found in output.")
        except Exception as e:
            logging.error(f"Error in extract_interface_inventory: {e}")
            interface_data.append({
                "System Name": "", "System Type": "", "Type": "Error",
                "Part Number": "Error", "Serial Number": "Error",
                "Description": "Error", "Name": "Error", "Source": ip or "Unknown",
            })

        df = pd.DataFrame(interface_data)
        if cache_callback:
            cache_callback(df, "interface_inventory")
        print(df.to_string(index=False))
        return df

    # ------------------------------------------------------------------
    # Orchestration (variant-supplied pipeline)
    # ------------------------------------------------------------------

    def _pipeline(self, ip_address: str) -> List[Callable[[str, Callable], pd.DataFrame]]:
        """Return the ordered parser callables matching ``get_commands()``.

        Each callable takes ``(command_output, cache_callback)``. Subclasses
        must override.
        """
        raise NotImplementedError

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict[str, Dict[str, Dict]],
    ) -> None:
        if not outputs_from_device:
            logging.warning(f"No outputs received from {ip_address}. Skipping.")
            return

        processing_functions = self._pipeline(ip_address)
        system_info = {"System Name": "", "System Type": ""}

        if len(outputs_from_device) != len(processing_functions):
            logging.warning(
                f"Output/function count mismatch for {ip_address}: "
                f"expected {len(processing_functions)}, got {len(outputs_from_device)}."
            )

        for idx, (command_output, fn) in enumerate(zip(outputs_from_device, processing_functions)):
            if not command_output:
                logging.warning(f"Command output {idx} for {ip_address} is empty. Skipping.")
                continue
            try:
                fn(
                    command_output,
                    lambda df, key: self.cache_data_frame(outputs, ip_address, key, df, system_info),
                )
            except Exception as e:
                logging.error(f"Error processing output {idx} for {ip_address}: {e}", exc_info=True)

        logging.info(f"All outputs processed for {ip_address}.")

    # ------------------------------------------------------------------
    # Validation (union of both variants' command checks)
    # ------------------------------------------------------------------

    def is_valid_output(self, output: str, command: str) -> bool:
        try:
            if not output or not output.strip():
                logging.warning(f"Empty output for command: {command}")
                return False

            if command.startswith("show general system-identification"):
                return bool(re.search(r"Shelf\s*type\s*:", output, re.IGNORECASE))
            if command.startswith("show general name"):
                return bool(re.search(r"Name:\s+([A-Za-z0-9_-]+)", output, re.MULTILINE | re.DOTALL))
            if command.startswith("show shelf 1"):
                return bool(re.search(r"Name\s*:", output))
            if command.startswith("show shelf inventory"):
                return bool(re.search(r"^\s*\d+\s+\S+\s+\S+\s+\S+", output, re.MULTILINE))
            if command.startswith("show card inventory"):
                return bool(re.search(r"^\s*\d+/\d+\s+\S+\s+\S+\s+\S+", output, re.MULTILINE))
            if command.startswith("show interface inventory"):
                return bool(re.search(r"^\s*\d+/\S+\s+\S+\s+\S+\s+\S+", output, re.MULTILINE))
            if command.startswith("show software dynamic"):
                return bool(re.search(r"Release\s+\S+", output))
            if command.startswith("show slot"):
                return bool(re.search(r"^\s*\d+/\d+\s+\S+", output, re.MULTILINE))
            if command.startswith("show redundancy"):
                return bool(re.search(r"Clock Switch", output))
            if command.startswith("show pf"):
                return bool(re.search(r"(Up|Down)", output, re.IGNORECASE))
            if command.startswith("show interface topology"):
                return bool(re.search(r"^\s*\d+/\S+\s+\S+\s+\S+", output, re.MULTILINE))

            return len(output.strip()) > 10
        except Exception as e:
            logging.error(f"Error validating output for '{command}': {e}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Cache / combine helpers
    # ------------------------------------------------------------------

    def cache_data_frame(
        self,
        outputs: Dict[str, Dict[str, Dict]],
        ip: str,
        key: str,
        df: pd.DataFrame,
        system_info: Dict[str, str],
    ) -> bool:
        try:
            if ip not in outputs:
                outputs[ip] = {}
            outputs[ip][key] = {"DataFrame": df, "System Info": system_info}
            logging.info(f"Cached DataFrame for {ip} / {key}.")
            return True
        except Exception as e:
            logging.error(f"Failed to cache data for {ip} / {key}: {e}")
            return False

    def combine_and_format_data(self, ip_data: Dict[str, Dict]) -> pd.DataFrame:
        all_data = []
        for key, data in ip_data.items():
            df = data["DataFrame"]
            if isinstance(df, pd.DataFrame) and not df.empty:
                all_data.append(df)
                logging.info(f"Combining key '{key}' with {len(df)} rows.")

        if all_data:
            combined_df = pd.concat(all_data, ignore_index=True)
            logging.info(f"Combined DataFrame: {len(combined_df)} rows from {len(all_data)} sources.")
        else:
            combined_df = pd.DataFrame()
            logging.warning("No data to combine.")

        return combined_df

    def print_cached_data(self, outputs: Dict[str, Dict[str, Dict]]) -> None:
        try:
            if not outputs:
                print("No cached data to display.")
                return
            print("\n--- All Cached DataFrames ---")
            for ip, ip_data in outputs.items():
                print(f"\nIP Address: {ip}")
                for key, data in ip_data.items():
                    print(f"  Key: {key}")
                    print(data["DataFrame"].to_string())
        except Exception as e:
            logging.error(f"Failed to print cached data: {e}")
