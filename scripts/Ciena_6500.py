import os
import logging
import re
import ipaddress
import sqlite3
import time
import pandas as pd
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from wexpect import spawn, EOF, TIMEOUT  # type: ignore[import-not-found]
except ImportError:
    from pexpect import spawn, EOF, TIMEOUT  # type: ignore[import-not-found]

from script_interface import BaseScript, NEEDS_CREDENTIALS_SENTINEL
from utils.helpers import ensure_host_key_known, get_known_hosts_path, get_database_path


db_path = str(get_database_path())

class Script(BaseScript):
    def __init__(self, connection_type='ssh', **kwargs):
        logging.debug("Initializing Script class")
        self.connection_type = connection_type
        self.stop_callback = kwargs.get('stop_callback')
        self.child = None

        if connection_type == 'ssh':
            self.ip_address = kwargs.get('ip_address')
            self.username = kwargs.get('username', 'ADMIN')
            self.password = kwargs.get('password', 'ADMIN')
            self.port = kwargs.get('port', 20002)
            logging.debug(f"Configured SSH connection: {self.ip_address}:{self.port} as {self.username}")

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    def abort_connection(self):
        """Forcefully close the SSH spawn process to interrupt blocking I/O."""
        if self.child:
            try:
                self.child.close(force=True)
                logging.debug("SSH spawn process forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing spawn: {e}")
            finally:
                self.child = None

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

    def get_commands(self) -> List[str]:
        logging.debug("Fetching device commands to execute")
        return [
            'equipment inventory-fan show',
            'equipment inventory-io show',
            'equipment inventory show']

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        logging.debug(f"Executing commands over {self.connection_type}")
        if self.connection_type == 'ssh':
            return self.execute_ssh_commands(self.ip_address, self.username, self.password, commands)
        raise ValueError("Unsupported connection type")

    def execute_ssh_commands(self, ip_address: str, username: str, password: str, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        try:
            # Validate IP address format to prevent injection attacks
            try:
                ipaddress.ip_address(ip_address)
            except ValueError:
                return [], f"Invalid IP address format: {ip_address}"
            
            # Validate username to prevent injection (alphanumeric, dash, underscore, dot only)
            if not re.match(r'^[a-zA-Z0-9._-]+$', username):
                return [], f"Invalid username format: {username}"
            
            _kh = str(get_known_hosts_path())
            if not ensure_host_key_known(str(ip_address), port=self.port):
                return [], (
                    f"SSH host key verification failed or rejected for "
                    f"{ip_address}:{self.port}"
                )
            ssh_args = [
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={_kh}",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                "-p", str(self.port),
                "-l", username,
                str(ip_address),
            ]
            logging.debug(f"Spawning SSH process to {ip_address}:{self.port}")
            self.child = spawn("ssh", args=ssh_args, encoding='utf-8', timeout=30)

            output_log = []

            idx = self.expect_with_abort(self.child, ["[Ll]ogin:", "Are you sure you want to continue connecting", EOF, TIMEOUT], timeout=30)
            logging.debug(f"Initial expect matched index: {idx}")
            if idx is None:
                self.child.close(force=True)
                self.child = None
                return [], "Aborted"
            if idx == 1:
                self.child.sendline("yes")
                idx = self.expect_with_abort(self.child, ["[Ll]ogin:", EOF, TIMEOUT], timeout=30)
                logging.debug(f"Expect after sending yes: {idx}")
                if idx is None:
                    self.child.close(force=True)
                    self.child = None
                    return [], "Aborted"

            logging.debug("Sending login credentials")
            self.child.sendline(username)
            if self.expect_with_abort(self.child, "[Pp]assword:", timeout=30) is None:
                self.child.close(force=True)
                self.child = None
                return [], "Aborted"
            self.child.sendline(password)

            idx = self.expect_with_abort(self.child, [r"[#]", EOF, TIMEOUT], timeout=30)
            if idx is None:
                self.child.close(force=True)
                self.child = None
                return [], "Aborted"
            if idx in [1, 2]:
                logging.error("Authentication failed")
                return [], NEEDS_CREDENTIALS_SENTINEL

            # Capture prompt
            prompt = self.child.after.strip()
            logging.debug(f"Detected prompt: '{prompt}'")

            for cmd in commands:
                if self.should_stop():
                    self.child.close(force=True)
                    self.child = None
                    return output_log, "Aborted"
                logging.debug(f"Sending command: {cmd}")
                self.child.sendline(cmd)
                if self.expect_with_abort(self.child, cmd, timeout=30) is None:
                    self.child.close(force=True)
                    self.child = None
                    return output_log, "Aborted"
                full_output = ""

                while True:
                    index = self.expect_with_abort(self.child, [prompt, EOF, TIMEOUT], timeout=30)
                    if index is None:
                        self.child.close(force=True)
                        self.child = None
                        return output_log, "Aborted"
                    chunk = self.child.before
                    full_output += chunk

                    if index == 0:
                        full_output += self.child.after
                        break
                    elif index == 1:
                        logging.warning("EOF while waiting for prompt")
                        break
                    elif index == 2:
                        logging.error("Timeout waiting for prompt")
                        break

                output_log.append(full_output.strip())

            self.child.sendline("exit")
            try:
                self.expect_with_abort(self.child, [prompt, EOF, TIMEOUT], timeout=10)
            except TIMEOUT:
                logging.warning("Timeout after 'exit', force closing")

            self.child.close(force=True)
            self.child = None
            return output_log, None

        except Exception as e:
            logging.exception("SSH execution exception")
            self.child = None
            return [], str(e)
            

    def get_part_description(self, part_number: str) -> str:
        try:
            logging.debug(f"Looking up part number: {part_number}")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT description FROM parts WHERE part_number = ?", (part_number,))
            result = cursor.fetchone()
            if result:
                logging.debug(f"Found description: {result[0]}")
            else:
                logging.debug("No description found")
            return result[0] if result else "Unknown"
        except Exception as e:
            logging.error(f"Database error: {e}")
            return "Error"
        finally:
            conn.close()

    def format_inventory_df(self, df: pd.DataFrame, ip: Optional[str] = None, system_info: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
        logging.debug("Starting inventory DataFrame formatting")

        formatted = []
        source_ip = ip or "Unknown"
        system_name = system_info.get("System Name", "") if system_info else ""
        system_type = system_info.get("System Type", "") if system_info else ""

        logging.debug(f"System Info - Name: {system_name}, Type: {system_type}, Source IP: {source_ip}")
        logging.debug(f"Input DataFrame shape: {df.shape}")
        logging.debug(f"Input DataFrame columns: {df.columns.tolist()}")

        skipped = 0

        for idx, row in df.iterrows():
            slot = str(row.get("aid", "")).strip()
            card_type = str(row.get("ctype", "")).strip()
            part_number = str(row.get("pec", "")).strip()
            serial_number = str(row.get("ser", "")).strip()

            if not slot or slot.upper().startswith(("FILLER", "EMPTY", "SPARE")):
                logging.debug(f"Skipping row {idx} - Non-operational slot: '{slot}'")
                skipped += 1
                continue

            description = self.get_part_description(part_number)

            formatted_entry = {
                'System Name': system_name,
                'System Type': system_type,
                'Type': card_type.title(),
                'Part Number': part_number,
                'Serial Number': serial_number,
                'Description': description,
                'Name': slot,
                'Source': source_ip
            }

            logging.debug(f"Row {idx} formatted: {formatted_entry}")
            formatted.append(formatted_entry)

        logging.debug(f"Formatted {len(formatted)} inventory items (Skipped: {skipped}) from IP {source_ip}")
        return formatted

    def process_outputs(self, raw_outputs: List[str], ip_address: str, outputs: Dict[str, Dict[str, Any]]):
        logging.debug(f"Processing outputs from {ip_address}")
        system_info = {'System Name': 'Ciena 6500', 'System Type': 'Optical'}
        combined_output = "\n".join(raw_outputs)

        # SECURITY: Raw output logged only at DEBUG level via the existing logging
        # infrastructure (which applies CredentialFilter redaction). Do NOT write
        # uncontrolled plaintext files to CWD — they expose sensitive device data,
        # are world-readable, and leave indefinite forensic artifacts on disk.
        logging.debug(f"Processing {len(raw_outputs)} raw output block(s) from {ip_address}")

        self.extract_data_to_df(
            combined_output,
            lambda df, key: self.cache_data_frame(outputs, ip_address, key, df, system_info),
            ip=ip_address
        )

    def extract_data_to_df(self, output: str, cache_callback, ip: str) -> None:
        logging.debug("Extracting equipment data from raw output")
        records = self.extract_equipment_data(output)

        if not records:
            logging.warning(f"No equipment records extracted for IP {ip}")
            return

        df = pd.DataFrame(records)

        expected_cols = {"aid", "ctype", "pec", "ser"}
        if not expected_cols.issubset(df.columns):
            logging.warning(f"Missing expected columns in parsed data for {ip}: {df.columns.tolist()}")
            return

        df = df[["aid", "ctype", "pec", "ser"]].dropna(how='all')
        df = df[~df["aid"].str.startswith("EMPTY", na=False)].drop_duplicates()
        cache_callback(df, "equipment_inventory")

    def cache_data_frame(self, outputs: Dict[str, Dict[str, Dict]], ip: str, key: str, df: pd.DataFrame, system_info: Dict[str, str]) -> None:
        logging.debug(f"Caching data for IP: {ip}, Key: {key}, Rows: {len(df)}")
        if ip not in outputs:
            outputs[ip] = {}
        outputs[ip][key] = {'DataFrame': df, 'System Info': system_info}

    @staticmethod
    def extract_equipment_data(output: str) -> List[Dict[str, Optional[str]]]:
        logging.debug("Parsing raw output into structured entries")

        cleaned_output = re.sub(r'--\s*More\s*--', '', output)
        lines = cleaned_output.splitlines()

        logging.debug(f"Cleaned Output ({len(lines)} lines):\n" + "\n".join(lines))

        records = []
        current = {}
        line_count = 0
        matched_count = 0

        for line in lines:
            line_count += 1
            match = re.match(r'(?P<key>aid|ctype|pec|ser)\s*\|\s*"(?P<value>[^"]*)"', line.strip(), re.IGNORECASE)
            if match:
                key = match.group("key").lower()
                value = match.group("value").strip()
                if key == "aid" and current.get("aid"):
                    records.append(current)
                    current = {}
                current[key] = value
                matched_count += 1
            else:
                logging.debug(f"No match on line {line_count}: {line.strip()}")

        if current.get("aid"):
            records.append(current)

        logging.info(f"Parsed {len(records)} equipment entries (matched {matched_count} fields in {line_count} lines)")
        return records

    def print_cached_data(self, outputs: Dict[str, Dict[str, Dict]]) -> None:
        for ip, sections in outputs.items():
            df = sections["equipment_inventory"]["DataFrame"]
            system_info = sections["equipment_inventory"]["System Info"]
            formatted = self.format_inventory_df(df, ip=ip, system_info=system_info)
            print("\n--- FORMATTED INVENTORY ---")
            for row in formatted:
                print(row)


# This script is designed to run as part of a larger system for network inventory management.
# It is not intended to be executed directly, but can be tested by uncommenting the main below.
# If you want to run this script directly, ensure the database path is correct and the database is set up properly.

if __name__ == "__main__":
    def main():
        ip = '172.21.113.10'
        logging.info(f"Running main routine for {ip}")
        script = Script(
            connection_type='ssh',
            ip_address=ip,
            username='ADMIN',
            password='ADMIN',
            port=20002
        )
        commands = script.get_commands()
        raw_outputs, error = script.execute_commands(commands)
        if error:
            logging.error(f"[ERROR] {error}")
            return
        outputs = {}
        script.process_outputs(raw_outputs, ip_address=ip, outputs=outputs)
        script.print_cached_data(outputs)

    main()
