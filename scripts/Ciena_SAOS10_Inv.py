"""Inventory script for Ciena 39XX/51XX/81XX devices running SAOS 10.x.

Collects inventory by:
  1. Running ``show software``        – captures running package version.
  2. Running ``show system components`` – captures hardware component table
                                         (chassis, cards, PSUs, fans).
  3. Running ``show logical-ports``   – captures logical port list.

SAOS 10 CLI notes:
  • Operational prompt:  ``hostname> ``
  • Config prompt:       ``username@hostname# ``
  • Tables use the format:  ``| Name       | Value       |``
  • Baud rates: 3924/3926/3928 = 9600 bps; 5162/5170/5171/8140/8180 = 115200 bps
"""
import os
import logging
import re
import time
import pandas as pd
import paramiko
from typing import Callable, Dict, List, Optional, Tuple
import serial

from script_interface import (
    BaseScript,
    DatabaseCache,
    get_inventory_db_path,
    get_tracker,
    ssh_connect_with_credential_fallback,
    CredentialPromptRequired,
    NEEDS_CREDENTIALS_SENTINEL,
)
from utils.helpers import get_known_hosts_path, get_host_key_policy, safe_load_host_keys, safe_save_host_keys


class Script(BaseScript):
    """Inventory script for Ciena 39XX/51XX/81XX devices running SAOS 10.x."""

    # SAOS 10 operational prompt: "hostname> "
    _PROMPT_RE = re.compile(r'>\s*$', re.MULTILINE)

    _COMMANDS = [
        'show software',
        'show system components',
        'show logical-ports',
    ]

    def __init__(
        self,
        *,
        db_path=None,
        db_cache=None,
        connection_type='ssh',
        serial_port=None,
        baud_rate=None,
        timeout=10,
        ip_address=None,
        username='admin',
        password='admin',
        command_tracker=None,
        stop_callback=None,
    ):
        if db_cache is not None:
            self.db_cache = db_cache
            self.db_path = db_cache.db_path
        else:
            if db_path is None:
                db_path = get_inventory_db_path()
            self.db_path = os.path.abspath(db_path)
            self.db_cache = DatabaseCache(self.db_path)

        self.connection_type = connection_type
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.device_name: Optional[str] = None
        self.device_type: Optional[str] = None
        self.stop_callback = stop_callback
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.serial_port_obj = None
        self.command_tracker = command_tracker or get_tracker()

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

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
            except Exception as exc:
                logging.debug(f"Error closing SSH: {exc}")
            finally:
                self.ssh_client = None
        if self.serial_port_obj:
            try:
                self.serial_port_obj.close()
                logging.info("Serial connection forcefully closed for abort.")
            except Exception as exc:
                logging.debug(f"Error closing serial: {exc}")
            finally:
                self.serial_port_obj = None

    def get_part_description(self, part_number: str) -> str:
        return self.db_cache.lookup_part((part_number or "")[:10])

    # ── Command list ──────────────────────────────────────────────────────────

    def get_commands(self) -> List[str]:
        return list(self._COMMANDS)

    # ── Command execution ─────────────────────────────────────────────────────

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        if self.connection_type == 'ssh':
            return self._execute_commands_ssh()
        elif self.connection_type == 'serial':
            return self._execute_commands_serial()
        else:
            raise ValueError(f"Invalid connection type: {self.connection_type!r}")

    def _execute_commands_ssh(self) -> Tuple[List[str], Optional[str]]:
        shell = None
        try:
            _kh = str(get_known_hosts_path())
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.load_system_host_keys()
            safe_load_host_keys(self.ssh_client, _kh)
            self.ssh_client.set_missing_host_key_policy(get_host_key_policy())
            logging.info(f"SAOS10-Inv: connecting to {self.ip_address}")
            try:
                ssh_connect_with_credential_fallback(
                    self.ssh_client,
                    self.ip_address,
                    self.username,
                    self.password,
                    timeout=10,
                )
            except CredentialPromptRequired:
                logging.info(f"Credentials exhausted for {self.ip_address}; parking for manual entry.")
                return [], NEEDS_CREDENTIALS_SENTINEL
            except paramiko.AuthenticationException as exc:
                return [], f"Authentication failed for {self.ip_address}: {exc}"
            safe_save_host_keys(self.ssh_client, _kh)
            logging.info(f"SAOS10-Inv: connected to {self.ip_address}")

            shell = self.ssh_client.invoke_shell()
            if self.sleep_with_abort(2):
                return [], "Aborted"
            self._read_until_prompt_ssh(shell, timeout=10)

            outputs: List[str] = []
            for cmd in self._COMMANDS:
                if self.should_stop():
                    return outputs, "Aborted"
                out = self._send_cmd_ssh(shell, cmd)
                if out is None:
                    msg = "Aborted" if self.should_stop() else f"Failed to execute: {cmd}"
                    return outputs, msg
                outputs.append(out)

            return outputs, None

        except Exception as exc:
            logging.error(f"SAOS10-Inv SSH error: {exc}")
            return [], str(exc)
        finally:
            if shell is not None:
                try:
                    shell.close()
                except Exception:
                    pass
            if self.ssh_client is not None:
                try:
                    self.ssh_client.close()
                except Exception:
                    pass
            self.ssh_client = None

    def _execute_commands_serial(self) -> Tuple[List[str], Optional[str]]:
        try:
            self.serial_port_obj = serial.Serial(
                self.serial_port, self.baud_rate or 9600, timeout=self.timeout
            )
            logging.info(f"SAOS10-Inv: connected to serial port {self.serial_port}")

            outputs: List[str] = []
            for cmd in self._COMMANDS:
                if self.should_stop():
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted"
                out = self._send_cmd_serial(cmd)
                if out is None:
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted" if self.should_stop() else f"Failed: {cmd}"
                outputs.append(out)

            self.serial_port_obj.close()
            self.serial_port_obj = None
            return outputs, None

        except Exception as exc:
            logging.error(f"SAOS10-Inv serial error: {exc}")
            if self.serial_port_obj:
                try:
                    self.serial_port_obj.close()
                except Exception:
                    pass
            self.serial_port_obj = None
            return [], str(exc)

    # ── Low-level I/O helpers ─────────────────────────────────────────────────

    def _read_until_prompt_ssh(self, shell, timeout: float = 15.0) -> str:
        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.should_stop():
                break
            if shell.recv_ready():
                chunk = shell.recv(65535).decode('utf-8', errors='replace')
                output += chunk
                if "--More--" in chunk or "Press any key" in chunk:
                    shell.send(' ')
                    if self.sleep_with_abort(0.5):
                        break
                if self._PROMPT_RE.search(output):
                    break
            else:
                time.sleep(0.1)
        return output

    def _send_cmd_ssh(self, shell, command: str) -> Optional[str]:
        try:
            logging.debug(f"SAOS10-Inv send: {command!r}")
            shell.send(command + '\n')
            output = self._read_until_prompt_ssh(shell, timeout=30)
            logging.debug(f"SAOS10-Inv recv ({command!r}): {output[:200]!r}")
            return output
        except Exception as exc:
            logging.error(f"SAOS10-Inv SSH send error for {command!r}: {exc}")
            return None

    def _send_cmd_serial(self, command: str) -> Optional[str]:
        try:
            logging.info(f"SAOS10-Inv serial send: {command!r}")
            self.serial_port_obj.write((command + '\n').encode())
            output = ""
            deadline = time.time() + 25
            while time.time() < deadline:
                if self.sleep_with_abort(0.5):
                    return None
                chunk = self.serial_port_obj.read(
                    self.serial_port_obj.in_waiting or 1
                ).decode('utf-8', errors='replace')
                if chunk:
                    output += chunk
                    if "--More--" in chunk or "Press any key" in chunk:
                        self.serial_port_obj.write(b' ')
                if (
                    self._PROMPT_RE.search(output)
                    and self.serial_port_obj.in_waiting == 0
                ):
                    break
            return output
        except Exception as exc:
            logging.error(f"SAOS10-Inv serial error for {command!r}: {exc}")
            return None

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_kv_table(self, output: str) -> Dict[str, str]:
        """Parse a two-column ``| Key | Value |`` SAOS table into a dict."""
        result: Dict[str, str] = {}
        kv_re = re.compile(r'\|\s*([^|+\-]+?)\s*\|\s*([^|+\-]+?)\s*\|')
        for m in kv_re.finditer(output):
            key = m.group(1).strip()
            val = m.group(2).strip()
            if key and val and not key.startswith('-') and not val.startswith('-'):
                result[key.lower()] = val
        return result

    def _parse_software_show(self, output: str) -> Dict[str, str]:
        """Parse ``show software`` → version string.

        Expected table row:
          | Running package version | saos-10-03-00-0172-GA |
        """
        info: Dict[str, str] = {}
        kv = self._parse_kv_table(output)
        for key, val in kv.items():
            if 'running' in key and ('package' in key or 'version' in key):
                info['version'] = val
                break
        if 'version' not in info:
            vm = re.search(
                r'(saos[-_]10[-_\w.]+|saos-\d[\d.\-]+)',
                output,
                re.IGNORECASE,
            )
            if vm:
                info['version'] = vm.group(1)
        # Try to extract hostname from the prompt at the start of output
        hn = re.search(r'^([A-Za-z0-9_\-]+)>\s', output, re.MULTILINE)
        if hn:
            info['system_name'] = hn.group(1)
        return info

    def _parse_system_components(self, output: str) -> List[Dict[str, str]]:
        """Parse ``show system components`` for hardware component rows.

        SAOS 10 system component tables look like:
          +------+------+----------+----------+--------+--------+--------+
          | Slot | Name | Part No  | Serial No| State  | ...    |        |
          +------+------+----------+----------+--------+--------+--------+
          | 1    | ...  | XXXXXXXX | YYYYYYYY | ...    |        |        |

        Falls back to a generic key-value scan if the column table is absent.
        """
        components: List[Dict[str, str]] = []

        # Try columnar table: look for header row with Part/Serial keywords
        header_re = re.compile(
            r'\|\s*(Slot|Name|Component)\s*\|[^\n]+Part[^\n]+Serial[^\n]+\|',
            re.IGNORECASE,
        )
        hm = header_re.search(output)
        if hm:
            header_line = hm.group(0)
            # Determine column positions from header
            cols = [c.strip().lower() for c in header_line.split('|') if c.strip()]
            col_idx = {name: i for i, name in enumerate(cols)}

            # Parse all data rows following the header
            data_re = re.compile(r'\|([^+\n]+)\|')
            in_data = False
            for line in output.splitlines():
                if not in_data:
                    if hm.group(0).rstrip('\n') in line or line == hm.group(0).rstrip('\n'):
                        in_data = True
                    continue
                dm = data_re.match(line.strip())
                if not dm:
                    continue
                cells = [c.strip() for c in line.split('|') if c.strip() or '|' in line]
                cells = [c.strip() for c in line.split('|')]
                if len(cells) < max(col_idx.values(), default=0) + 1:
                    continue

                def gcell(key: str) -> str:
                    idx = col_idx.get(key)
                    if idx is not None and idx < len(cells):
                        return cells[idx].strip()
                    return ''

                part = gcell('part no') or gcell('part number') or gcell('part#')
                serial = gcell('serial no') or gcell('serial number') or gcell('serial#')
                name = gcell('name') or gcell('component') or gcell('slot')
                if part or serial:
                    components.append({'name': name, 'part_number': part, 'serial_number': serial})
            return components

        # Fallback: scan for lines that contain both a part-number-like and serial-like token
        pn_re = re.compile(r'\b([A-Z0-9]{4,}-[A-Z0-9\-]+|[A-Z]{2,}\d{4,})\b')
        sn_re = re.compile(r'\b([A-Z]{1,3}\d{6,}|[0-9]{8,})\b')
        for line in output.splitlines():
            if '|' not in line:
                continue
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if not cells:
                continue
            text = ' '.join(cells)
            pn_m = pn_re.search(text)
            sn_m = sn_re.search(text)
            if pn_m and sn_m and pn_m.start() != sn_m.start():
                components.append({
                    'name': cells[0] if cells else '',
                    'part_number': pn_m.group(1),
                    'serial_number': sn_m.group(1),
                })
        return components

    def _parse_logical_ports(self, output: str) -> List[Dict[str, str]]:
        """Parse ``show logical-ports`` for port names and admin state.

        Typical row format:
          | 1          | eth-1-1    | enabled  | ... |
        Returns list of {name, admin_state} dicts.
        """
        ports: List[Dict[str, str]] = []
        row_re = re.compile(
            r'^\|\s*(\d[\d.]*)\s*\|\s*([A-Za-z0-9_/\-]+)\s*\|\s*([a-z]+)\s*\|',
            re.MULTILINE,
        )
        for m in row_re.finditer(output):
            ports.append({
                'index': m.group(1).strip(),
                'name': m.group(2).strip(),
                'admin_state': m.group(3).strip(),
            })
        if not ports:
            # Alternative: rows may only have name + state without numeric index
            alt_re = re.compile(
                r'^\|\s*([A-Za-z0-9_/\-]+)\s*\|\s*(enabled|disabled)\s*\|',
                re.MULTILINE | re.IGNORECASE,
            )
            for m in alt_re.finditer(output):
                ports.append({'name': m.group(1).strip(), 'admin_state': m.group(2).strip()})
        logging.info(f"SAOS10-Inv: logical ports found: {[p['name'] for p in ports]}")
        return ports

    # ── process_outputs ───────────────────────────────────────────────────────

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict,
    ) -> None:
        """Parse all command outputs and cache DataFrames for *ip_address*.

        ``outputs_from_device`` layout:
          [0] show software output
          [1] show system components output
          [2] show logical-ports output
        """
        if not outputs_from_device:
            logging.warning(f"SAOS10-Inv: no outputs from {ip_address}")
            return

        sw_out   = outputs_from_device[0] if len(outputs_from_device) > 0 else ""
        comp_out = outputs_from_device[1] if len(outputs_from_device) > 1 else ""
        port_out = outputs_from_device[2] if len(outputs_from_device) > 2 else ""

        sw_info = self._parse_software_show(sw_out)

        # Resolve device identity
        self.device_name = sw_info.get('system_name') or ip_address
        sw_version = sw_info.get('version', 'SAOS 10')
        self.device_type = f"Ciena SAOS 10 ({sw_version})"

        system_info = {'System Name': self.device_name, 'System Type': self.device_type}

        expected_cols = [
            'System Name', 'System Type', 'Type', 'Part Number',
            'Serial Number', 'Description', 'Information Type', 'Name', 'Source',
        ]

        rows: List[Dict] = []

        # ── Hardware components ───────────────────────────────────────────────
        for comp in self._parse_system_components(comp_out):
            part_number = (comp.get('part_number') or '')[:10]
            serial_number = comp.get('serial_number', '')
            comp_name = comp.get('name', '')
            description = self.get_part_description(part_number) if part_number else ''
            rows.append({
                'System Name': self.device_name,
                'System Type': self.device_type,
                'Type': 'Component',
                'Part Number': part_number,
                'Serial Number': serial_number,
                'Description': description or comp_name,
                'Information Type': 'Component',
                'Name': comp_name,
                'Source': ip_address,
            })

        # ── Logical ports (as informational entries) ──────────────────────────
        for port in self._parse_logical_ports(port_out):
            rows.append({
                'System Name': self.device_name,
                'System Type': self.device_type,
                'Type': 'Port',
                'Part Number': '',
                'Serial Number': '',
                'Description': port.get('admin_state', ''),
                'Information Type': 'Logical Port',
                'Name': port.get('name', ''),
                'Source': ip_address,
            })

        if rows:
            df = pd.DataFrame(rows, columns=expected_cols)
        else:
            df = pd.DataFrame(columns=expected_cols)
            logging.warning(f"SAOS10-Inv: no inventory data parsed for {ip_address}")

        self.cache_data_frame(outputs, ip_address, 'inventory_data', df, system_info)
        logging.info(f"SAOS10-Inv: processed {len(rows)} entries for {ip_address}")

    # ── Cache helpers (mirrors Nokia_SAR pattern) ─────────────────────────────

    def cache_data_frame(
        self,
        outputs: Dict,
        ip: str,
        key: str,
        df: pd.DataFrame,
        system_info: Dict[str, str],
    ) -> bool:
        try:
            if ip not in outputs:
                outputs[ip] = {}
            outputs[ip][key] = {'DataFrame': df, 'System Info': system_info}
            logging.info(f"DataFrame for {ip} under key {key!r} cached.")
            return True
        except Exception as exc:
            logging.error(f"Failed to cache data for {ip}[{key!r}]: {exc}")
            return False

    def combine_and_format_data(self, ip_data: Dict) -> pd.DataFrame:
        all_data = [entry['DataFrame'] for entry in ip_data.values()]
        if all_data:
            return pd.concat(all_data, ignore_index=True)
        return pd.DataFrame()
