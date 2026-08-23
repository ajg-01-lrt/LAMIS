"""Smartoptics DCP-M open line system inventory script.

The Smartoptics DCP CLI is delightfully simple compared to most platforms:
authenticate over SSH (default ``admin`` / ``admin``) and the post-login
banner immediately tells us what kind of shelf we're on, e.g.::

    DCP-M32-CSO-ZR+, Coherent 32 channel DWDM Open Line System
    Software version: dcp-release-13.0.0
    ...
    admin@SO-POC-TOP>

A single ``show inventory`` command then yields a fixed-width table whose
columns are derived from the dashed separator line on the device. We parse
the table by column boundaries so descriptions containing commas/spaces
don't break the row split.
"""

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
import paramiko

from script_interface import (
    BaseScript,
    CommandTracker,
    DatabaseCache,
    CredentialPromptRequired,
    NEEDS_CREDENTIALS_SENTINEL,
    get_cache,
    get_tracker,
    ssh_connect_with_credential_fallback,
)
from utils.helpers import get_known_hosts_path, get_host_key_policy, safe_load_host_keys, safe_save_host_keys

# Standard schema all device scripts emit so WorkbookBuilder can combine them.
_SCHEMA = [
    "System Name", "System Type", "Type", "Part Number",
    "Serial Number", "Description", "Name", "Source",
]


class Script(BaseScript):
    def __init__(self,
                 connection_type: str = "ssh",
                 *,
                 ip_address: Optional[str] = None,
                 username: str = "admin",
                 password: str = "admin",
                 stop_callback: Optional[Callable[[], bool]] = None,
                 command_tracker: Optional[CommandTracker] = None,
                 db_cache: Optional[DatabaseCache] = None,
                 timeout: int = 10,
                 **kwargs) -> None:
        self.connection_type = connection_type
        self.ip_address = ip_address
        self.username = username or "admin"
        self.password = password or "admin"
        self.timeout = timeout
        self.stop_callback = stop_callback
        self.command_tracker = command_tracker or get_tracker()
        self.db_cache = db_cache or get_cache()

        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.system_name: Optional[str] = None
        self.system_type: Optional[str] = None
        self.software_version: Optional[str] = None

    # ------------------------------------------------------------------
    # BaseScript hooks
    # ------------------------------------------------------------------
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
                logging.info("SSH connection forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing SSH: {e}")
            finally:
                self.ssh_client = None

    def get_commands(self) -> List[str]:
        return ["show inventory"]

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        if self.connection_type != "ssh":
            return [], f"Unsupported connection type for Smartoptics DCP: {self.connection_type}"
        return self._execute_ssh_commands(commands)

    # ------------------------------------------------------------------
    # SSH transport
    # ------------------------------------------------------------------
    def _execute_ssh_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        shell = None
        outputs: List[str] = []
        _owns_client = False
        try:
            injected = getattr(self, '_injected_ssh_client', None)
            self._injected_ssh_client = None
            _transport = injected.get_transport() if injected is not None else None
            if _transport is not None and _transport.is_active():
                try:
                    self.ssh_client = injected
                    shell = self.ssh_client.invoke_shell()
                    logging.info(f"Reusing existing SSH connection to {self.ip_address}")
                except Exception:
                    logging.info(f"Injected shell failed for {self.ip_address}, opening fresh connection")
                    shell = None
                    try:
                        injected.close()
                    except Exception:
                        pass
                    self.ssh_client = None
                    injected = None
            elif injected is not None:
                logging.info(f"Injected SSH transport is no longer active for {self.ip_address}, opening fresh connection")
                try:
                    injected.close()
                except Exception:
                    pass

            if self.ssh_client is None:
                _owns_client = True
                _kh = str(get_known_hosts_path())
                self.ssh_client = paramiko.SSHClient()
                self.ssh_client.load_system_host_keys()
                safe_load_host_keys(self.ssh_client, _kh)
                self.ssh_client.set_missing_host_key_policy(get_host_key_policy())

                logging.info(f"Connecting to {self.ip_address}")
                try:
                    used_user, used_pass = ssh_connect_with_credential_fallback(
                        self.ssh_client,
                        self.ip_address,
                        self.username,
                        self.password,
                        timeout=self.timeout,
                    )
                except CredentialPromptRequired:
                    logging.info(
                        f"Default credentials exhausted for {self.ip_address}; "
                        f"parking in pause queue for user-credential entry"
                    )
                    return outputs, NEEDS_CREDENTIALS_SENTINEL
                except paramiko.AuthenticationException as ae:
                    logging.error(
                        f"Authentication failed for {self.ip_address}: {ae}"
                    )
                    return outputs, (
                        f"Authentication failed for {self.ip_address}. Skipping this device."
                    )
                self.username, self.password = used_user, used_pass
                safe_save_host_keys(self.ssh_client, _kh)
                logging.info(f"Connected to {self.ip_address}")
                shell = self.ssh_client.invoke_shell()
            if self.sleep_with_abort(1):
                return outputs, "Aborted"

            # Drain the post-login banner — this is where the DCP shelf
            # tells us its model + software version + hostname.
            banner = self._drain_shell(shell, idle_seconds=1.0, max_wait=5.0)
            self._parse_banner(banner)
            if self.system_name or self.system_type:
                logging.info(
                    f"Identified Smartoptics DCP at {self.ip_address}: "
                    f"{self.system_name or 'Unknown'} ({self.system_type or 'Unknown'})"
                )

            total = len(commands)
            logging.info(f"Collecting inventory from {self.ip_address} ({total} commands)")
            for idx, command in enumerate(commands, start=1):
                if self.should_stop():
                    return outputs, "Aborted"
                if self.command_tracker.has_executed(self.ip_address, command, "ssh"):
                    logging.debug(f"Skipping previously executed command: {command}")
                    continue
                logging.info(f"[{self.ip_address}] ({idx}/{total}) Executing: {command}")
                output = self._capture_full_output_ssh(shell, command)
                if output is None:
                    err = "Aborted" if self.should_stop() else f"Failed to execute command: {command}"
                    logging.error(err)
                    return outputs, err
                outputs.append(output)
                self.command_tracker.mark_as_executed(self.ip_address, command, "ssh")

            logging.info(f"Inventory collection complete for {self.ip_address}")
            return outputs, None

        except paramiko.AuthenticationException as ae:
            logging.error(f"SSH authentication failed for {self.ip_address}: {ae}")
            return outputs, f"Authentication failed: {ae}"
        except Exception as e:
            logging.error(f"SSH connection failed: {e}")
            return outputs, str(e)
        finally:
            if shell is not None:
                try:
                    shell.close()
                except Exception as e:
                    logging.debug(f"Error closing shell: {e}")
            if self.ssh_client is not None and _owns_client:
                try:
                    self.ssh_client.close()
                except Exception as e:
                    logging.debug(f"Error closing SSH client: {e}")
            self.ssh_client = None

    def _capture_full_output_ssh(self, shell, command: str) -> Optional[str]:
        try:
            shell.send(command + "\n")
            output = ""
            idle_loops = 0
            while True:
                if self.should_stop():
                    return None
                if shell.recv_ready():
                    chunk = shell.recv(65535).decode("utf-8", errors="ignore")
                    output += chunk
                    if "Press any key" in chunk or "--More--" in chunk:
                        shell.send(" ")
                        if self.sleep_with_abort(1):
                            return None
                    idle_loops = 0
                else:
                    if self.sleep_with_abort(0.5):
                        return None
                    idle_loops += 1
                    # Stop once the prompt has returned and the channel
                    # has been idle for ~1.5 seconds.
                    if idle_loops >= 3 and re.search(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+>\s*$", output):
                        break
                    if idle_loops >= 12:  # 6s hard cap of silence
                        break

            logging.debug(f"Captured {len(output)} bytes for '{command}'")
            return output
        except Exception as e:
            logging.error(f"Exception in executing command: {e}")
            return None

    @staticmethod
    def _drain_shell(shell, idle_seconds: float = 1.0, max_wait: float = 5.0) -> str:
        buf = ""
        deadline = time.time() + max_wait
        last_data = time.time()
        while time.time() < deadline:
            if shell.recv_ready():
                chunk = shell.recv(65535).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    last_data = time.time()
            else:
                if time.time() - last_data >= idle_seconds:
                    break
                time.sleep(0.1)
        return buf

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_banner(self, banner: str) -> None:
        """Extract System Type, software version, and hostname from the
        post-login banner and shell prompt.
        """
        if not banner:
            return

        # First non-empty banner line is something like:
        #   "DCP-M32-CSO-ZR+, Coherent 32 channel DWDM Open Line System"
        for line in banner.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = re.match(r"^(DCP-[A-Z0-9\-+]+)\s*,\s*(.+)$", stripped)
            if m:
                self.system_type = f"{m.group(1)} - {m.group(2).strip()}"
                break

        sw = re.search(r"Software version\s*:\s*(\S+)", banner)
        if sw:
            self.software_version = sw.group(1).strip()

        # Prompt ends with "<user>@<hostname>>". Hostname = system name.
        prompt = re.search(r"([A-Za-z0-9._-]+)@([A-Za-z0-9._-]+)>\s*$", banner)
        if prompt:
            self.system_name = prompt.group(2)

    def _split_table_rows(self, table_text: str) -> List[Dict[str, str]]:
        """Parse a Smartoptics ``show inventory`` table.

        The DCP CLI uses a fixed-width layout that occasionally lets a wide
        Part Number overflow into the Description column with only a single
        space between them. We split each data row on runs of 2+ spaces
        and, if a Part Number cell still contains an embedded space,
        re-split it once to recover the real Part Number + Description.
        """
        lines = [ln.rstrip() for ln in table_text.splitlines() if ln.strip()]
        if not lines:
            return []

        header_idx = None
        for i, ln in enumerate(lines):
            if "Location" in ln and "Part number" in ln and "Serial number" in ln:
                header_idx = i
                break

        if header_idx is None or header_idx + 1 >= len(lines):
            logging.warning("Smartoptics inventory table header not found; skipping parse.")
            return []

        # Skip the dashed separator line if present.
        start = header_idx + 1
        if set(lines[start].strip()) <= {"-", " "}:
            start += 1

        column_labels = ["Location", "Part number", "Description",
                         "HW rev", "FW rev", "Serial number"]
        rows: List[Dict[str, str]] = []
        for ln in lines[start:]:
            if re.search(r"@[A-Za-z0-9._-]+>\s*$", ln):
                break
            cells = re.split(r"\s{2,}", ln.strip())
            if len(cells) < 5:
                logging.debug(f"Skipping unparseable inventory row: {ln!r}")
                continue
            # Recover from Part Number overflowing into Description.
            if len(cells) == 5 and " " in cells[1]:
                pn, _, desc = cells[1].partition(" ")
                cells = [cells[0], pn, desc.strip(), cells[2], cells[3], cells[4]]
            if len(cells) < len(column_labels):
                cells = cells + [""] * (len(column_labels) - len(cells))
            elif len(cells) > len(column_labels):
                # Merge any extra trailing tokens back into Description.
                head = cells[:2]
                tail = cells[-3:]
                middle = " ".join(cells[2:-3])
                cells = head + [middle] + tail

            row = dict(zip(column_labels, cells))
            if any(v for v in row.values()):
                rows.append(row)
        return rows

    def _classify_location(self, location: str) -> Tuple[str, str]:
        """Map a Location value (e.g. ``psu-1/1``) to (Type, Name)."""
        loc = (location or "").strip()
        low = loc.lower()
        if low == "chassis":
            return "Chassis", "Chassis"
        if low.startswith("psu"):
            return "Power Supply", loc
        if low.startswith("fan"):
            return "Fan", loc
        if low.startswith("if-"):
            return "Interface", loc
        if low.startswith("card") or low.startswith("ln"):
            return "Card", loc
        return "Component", loc or "Unknown"

    def extract_inventory(self,
                          output: str,
                          cache_callback: Callable[[pd.DataFrame, str], None],
                          ip: str) -> pd.DataFrame:
        """Parse ``show inventory`` output into the standard schema."""
        data: List[Dict[str, str]] = []
        try:
            for row in self._split_table_rows(output):
                location = row.get("Location", "")
                part_number = row.get("Part number", "") or "Unknown"
                description = row.get("Description", "") or ""
                serial = row.get("Serial number", "") or ""
                if serial.lower() == "n/a":
                    serial = ""

                # Prefer the part DB description when we have a hit;
                # fall back to the device-reported description otherwise.
                db_desc = ""
                if part_number and part_number != "Unknown":
                    try:
                        db_desc = self.db_cache.lookup_part(part_number) or ""
                    except Exception as e:
                        logging.debug(f"DB lookup failed for {part_number}: {e}")
                if db_desc and db_desc.lower() != "unknown":
                    description = db_desc

                comp_type, comp_name = self._classify_location(location)
                data.append({
                    "System Name": self.system_name or "",
                    "System Type": self.system_type or "",
                    "Type": comp_type,
                    "Part Number": part_number,
                    "Serial Number": serial,
                    "Description": description,
                    "Name": comp_name,
                    "Source": ip,
                })

            if not data:
                logging.warning(f"No inventory rows parsed from {ip}.")
        except Exception as e:
            logging.error(f"Error in extract_inventory for {ip}: {e}", exc_info=True)

        df = pd.DataFrame(data, columns=_SCHEMA)
        if df.empty:
            logging.warning(f"DataFrame for inventory is empty for {ip}.")
        else:
            logging.info(f"DataFrame for inventory is populated:\n{df}")

        cache_callback(df, "inventory_data")
        return df

    # ------------------------------------------------------------------
    # Pipeline plumbing (matches Nokia_SAR / Nokia_IXR)
    # ------------------------------------------------------------------
    def process_outputs(self,
                        outputs_from_device: List[str],
                        ip_address: str,
                        outputs: Dict[str, Dict[str, Dict]]) -> None:
        if not outputs_from_device:
            logging.warning(f"No outputs received from device at {ip_address}. Skipping processing.")
            return

        system_info = {
            "System Name": self.system_name or "Unknown",
            "System Type": self.system_type or "Unknown",
            "Software Version": self.software_version or "Unknown",
        }

        processing_functions = [
            lambda output, callback: self.extract_inventory(output, callback, ip_address),
        ]

        for idx, (command_output, fn) in enumerate(zip(outputs_from_device, processing_functions)):
            if not command_output:
                logging.warning(f"Command output {idx} for {ip_address} is empty or None. Skipping this step.")
                continue
            try:
                fn(command_output,
                   lambda df, key: self.cache_data_frame(outputs, ip_address, key, df, system_info))
            except Exception as e:
                logging.error(
                    f"Error processing output {idx} for device {ip_address}: {e}",
                    exc_info=True,
                )

        logging.info(f"All outputs processed successfully for device {ip_address}.")

    def cache_data_frame(self,
                         outputs: Dict[str, Dict[str, Dict]],
                         ip: str,
                         key: str,
                         df: pd.DataFrame,
                         system_info: Dict[str, str]) -> bool:
        try:
            if ip not in outputs:
                outputs[ip] = {}
            outputs[ip][key] = {"DataFrame": df, "System Info": system_info}
            logging.info(f"DataFrame for {ip} under key {key} cached successfully.")
            return True
        except Exception as e:
            logging.error(f"Failed to cache data for {ip} under key {key}. Error: {e}")
            return False

    def is_valid_output(self, output: str, command: str) -> bool:
        if not output or not output.strip():
            return False
        if command.strip().lower() == "show inventory":
            return "Location" in output and "Part number" in output
        return len(output.strip()) > 10

    def combine_and_format_data(self, ip_data: Dict[str, Dict]) -> pd.DataFrame:
        all_data = []
        logging.info(f"Starting combination of {len(ip_data)} data entries.")
        for key, data in ip_data.items():
            df = data["DataFrame"] if isinstance(data, dict) and "DataFrame" in data else data
            if isinstance(df, pd.DataFrame):
                all_data.append(df)
                logging.info(f"Processed DataFrame under key '{key}' with {len(df)} rows.")
        if all_data:
            combined_df = pd.concat(all_data, ignore_index=True)
            logging.info(f"Combined DataFrame created with {len(combined_df)} rows from {len(all_data)} DataFrames.")
            return combined_df
        logging.warning("No data to combine. Returning empty DataFrame.")
        return pd.DataFrame(columns=_SCHEMA)
