"""Inventory script for Ciena 39XX/51XX devices running SAOS 6.21.5.

Collects transceiver (xcvr) inventory by:
  1. Running ``software show`` to capture the running software version.
  2. Running ``port xcvr show`` to discover which ports have transceivers installed.
  3. Running ``port xcvr show port <N> vendor`` for each discovered port to
     retrieve vendor name, part number, and serial number.
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
    """Inventory script for Ciena 39XX/51XX devices running SAOS 6.21.5."""

    # SAOS prompt ends with "> " at the top level (e.g., "hostname> ")
    _PROMPT_RE = re.compile(r'>\s*$', re.MULTILINE)

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

    # ── Command list (static baseline; vendor cmds added dynamically) ─────────

    def get_commands(self) -> List[str]:
        return ['software show', 'port xcvr show']

    # ── Command execution (dispatches to SSH or serial) ───────────────────────

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        """Run ``software show``, ``port xcvr show``, and per-port vendor commands."""
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
            logging.info(f"SAOS-Inv: connecting to {self.ip_address}")
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
            logging.info(f"SAOS-Inv: connected to {self.ip_address}")

            shell = self.ssh_client.invoke_shell()
            if self.sleep_with_abort(2):
                return [], "Aborted"
            # Consume the login banner before sending commands.
            self._read_until_prompt_ssh(shell, timeout=8)

            outputs: List[str] = []
            for cmd in ['software show', 'port xcvr show']:
                if self.should_stop():
                    return outputs, "Aborted"
                out = self._send_cmd_ssh(shell, cmd)
                if out is None:
                    msg = "Aborted" if self.should_stop() else f"Failed to execute: {cmd}"
                    return outputs, msg
                outputs.append(out)

            # Discover populated ports from xcvr state table.
            ports = self._parse_xcvr_port_list(outputs[1]) if len(outputs) > 1 else []
            for port in ports:
                if self.should_stop():
                    return outputs, "Aborted"
                cmd = f'port xcvr show port {port} vendor'
                out = self._send_cmd_ssh(shell, cmd)
                if out is None:
                    msg = "Aborted" if self.should_stop() else f"Failed to execute: {cmd}"
                    return outputs, msg
                outputs.append(out)

            return outputs, None

        except Exception as exc:
            logging.error(f"SAOS-Inv SSH error: {exc}")
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
            logging.info(f"SAOS-Inv: connected to serial port {self.serial_port}")

            outputs: List[str] = []
            for cmd in ['software show', 'port xcvr show']:
                if self.should_stop():
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted"
                out = self._send_cmd_serial(cmd)
                if out is None:
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted" if self.should_stop() else f"Failed to execute: {cmd}"
                outputs.append(out)

            ports = self._parse_xcvr_port_list(outputs[1]) if len(outputs) > 1 else []
            for port in ports:
                if self.should_stop():
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted"
                cmd = f'port xcvr show port {port} vendor'
                out = self._send_cmd_serial(cmd)
                if out is None:
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted" if self.should_stop() else f"Failed to execute: {cmd}"
                outputs.append(out)

            self.serial_port_obj.close()
            self.serial_port_obj = None
            return outputs, None

        except Exception as exc:
            logging.error(f"SAOS-Inv serial error: {exc}")
            if self.serial_port_obj:
                try:
                    self.serial_port_obj.close()
                except Exception:
                    pass
            self.serial_port_obj = None
            return [], str(exc)

    # ── Low-level I/O helpers ─────────────────────────────────────────────────

    def _read_until_prompt_ssh(self, shell, timeout: float = 12.0) -> str:
        """Read output from an interactive SSH shell until a SAOS prompt or timeout."""
        output = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.should_stop():
                break
            if shell.recv_ready():
                chunk = shell.recv(65535).decode('utf-8', errors='replace')
                output += chunk
                if "Press any key to continue" in chunk:
                    shell.send(' ')
                    if self.sleep_with_abort(1):
                        break
                if self._PROMPT_RE.search(output):
                    break
            else:
                time.sleep(0.1)
        return output

    def _send_cmd_ssh(self, shell, command: str) -> Optional[str]:
        try:
            logging.debug(f"SAOS-Inv send: {command!r}")
            shell.send(command + '\n')
            output = self._read_until_prompt_ssh(shell, timeout=25)
            logging.debug(f"SAOS-Inv recv ({command!r}): {output[:200]!r}")
            return output
        except Exception as exc:
            logging.error(f"SAOS-Inv SSH send error for {command!r}: {exc}")
            return None

    def _send_cmd_serial(self, command: str) -> Optional[str]:
        try:
            logging.info(f"SAOS-Inv serial send: {command!r}")
            self.serial_port_obj.write((command + '\n').encode())
            output = ""
            deadline = time.time() + 20
            while time.time() < deadline:
                if self.sleep_with_abort(0.5):
                    return None
                chunk = self.serial_port_obj.read(
                    self.serial_port_obj.in_waiting or 1
                ).decode('utf-8', errors='replace')
                if chunk:
                    output += chunk
                    if "Press any key to continue" in chunk:
                        self.serial_port_obj.write(b' ')
                        output = output.replace("Press any key to continue (Q to quit)", "")
                if (
                    self._PROMPT_RE.search(output)
                    and self.serial_port_obj.in_waiting == 0
                ):
                    break
            return output
        except Exception as exc:
            logging.error(f"SAOS-Inv serial error for {command!r}: {exc}")
            return None

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_xcvr_port_list(self, output: str) -> List[str]:
        """Return port numbers from ``port xcvr show`` that have an xcvr installed.

        The state table has rows like:
          |12  |Ena  |Ena  |CIENA XCVR-010Y31 Rev10  |1000BASE-LX/LC|    |
        Rows where the vendor/part-number column is empty, "No SFP", or
        "Not Present" indicate an empty port and are skipped.
        """
        ports: List[str] = []
        row_re = re.compile(
            r'^\|\s*(\d[\d.]*)\s*\|[^|]*\|[^|]*\|\s*([^|]*?)\s*\|',
            re.MULTILINE,
        )
        for m in row_re.finditer(output):
            port = m.group(1).strip()
            vendor_col = m.group(2).strip()
            if not vendor_col:
                continue
            if re.search(r'no\s*sfp|not\s*present|empty', vendor_col, re.IGNORECASE):
                continue
            ports.append(port)
        logging.info(f"SAOS-Inv: xcvr ports discovered: {ports}")
        return ports

    def _parse_software_show(self, output: str) -> Dict[str, str]:
        """Parse ``software show`` for software version and optional system name.

        SAOS table rows look like:  ``| Running Package  | saos-06-21-05-0042.pkg |``
        """
        info: Dict[str, str] = {}
        kv_re = re.compile(r'\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|')
        for m in kv_re.finditer(output):
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if not val or val.startswith('-') or val.startswith('+'):
                continue
            if ('running' in key or 'active' in key) and (
                'package' in key or 'version' in key or 'software' in key
            ):
                info['version'] = val
            elif key in ('system name', 'name', 'host-name', 'host name', 'hostname'):
                info['system_name'] = val
        # Fallback: grab a version-like token from the raw output.
        if 'version' not in info:
            vm = re.search(
                r'(saos[-_]\d[\w.\-]+\.pkg|\d+\.\d+[\w.\-]+)',
                output,
                re.IGNORECASE,
            )
            if vm:
                info['version'] = vm.group(1)
        return info

    def _parse_xcvr_vendor(self, output: str, port: str) -> Dict[str, str]:
        """Parse ``port xcvr show port N vendor`` output.

        Extracts: port, vendor_name, part_number (Vendor PN or Ciena PN), serial_number.
        """
        data: Dict[str, str] = {'port': port}
        kv_re = re.compile(r'\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|')
        for m in kv_re.finditer(output):
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if not val or val.startswith('-') or val.startswith('+') or val.startswith('0x'):
                continue
            if key == 'vendor name':
                data['vendor_name'] = val
            elif key in ('vendor pn', 'vendor part number') and 'part_number' not in data:
                data['part_number'] = val
            elif key == 'ciena' and 'part_number' not in data:
                # Ciena's own part number often appears under just "Ciena"
                data['part_number'] = val
            elif key in ('vendor serial number', 'serial number') and 'serial_number' not in data:
                data['serial_number'] = val
        return data

    # ── process_outputs ───────────────────────────────────────────────────────

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict,
    ) -> None:
        """Parse all command outputs and cache a DataFrame for *ip_address*.

        ``outputs_from_device`` layout:
          [0] software show output
          [1] port xcvr show (state table)
          [2..N] port xcvr show port <n> vendor (one per discovered port)
        """
        if not outputs_from_device:
            logging.warning(f"SAOS-Inv: no outputs from {ip_address}")
            return

        sw_info = self._parse_software_show(outputs_from_device[0]) if outputs_from_device else {}
        xcvr_state_out = outputs_from_device[1] if len(outputs_from_device) > 1 else ""
        ports = self._parse_xcvr_port_list(xcvr_state_out)

        # Resolve device identity
        if sw_info.get('system_name'):
            self.device_name = sw_info['system_name']
        if not self.device_name:
            self.device_name = ip_address
        sw_version = sw_info.get('version', 'SAOS 6')
        self.device_type = f"Ciena 39XX/51XX ({sw_version})"

        system_info = {'System Name': self.device_name, 'System Type': self.device_type}

        expected_cols = [
            'System Name', 'System Type', 'Type', 'Part Number',
            'Serial Number', 'Description', 'Information Type', 'Name', 'Source',
        ]
        xcvr_rows = []
        vendor_outputs = outputs_from_device[2:]  # One output per discovered port
        for idx, port in enumerate(ports):
            if idx >= len(vendor_outputs):
                break
            vdata = self._parse_xcvr_vendor(vendor_outputs[idx], port)
            part_number = (vdata.get('part_number') or '')[:10]
            serial_number = vdata.get('serial_number', '')
            vendor_name = vdata.get('vendor_name', '')
            description = self.get_part_description(part_number) if part_number else ''
            xcvr_rows.append({
                'System Name': self.device_name,
                'System Type': self.device_type,
                'Type': 'Transceiver',
                'Part Number': part_number,
                'Serial Number': serial_number,
                'Description': description or vendor_name,
                'Information Type': 'Transceiver',
                'Name': f'Port {port}',
                'Source': ip_address,
            })

        if xcvr_rows:
            df = pd.DataFrame(xcvr_rows, columns=expected_cols)
        else:
            df = pd.DataFrame(columns=expected_cols)
            logging.warning(f"SAOS-Inv: no xcvr data parsed for {ip_address}")

        self.cache_data_frame(outputs, ip_address, 'xcvr_data', df, system_info)
        logging.info(f"SAOS-Inv: processed {len(xcvr_rows)} xcvr(s) for {ip_address}")

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
