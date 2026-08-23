import os
import sqlite3
import logging
import re
import time
import pandas as pd
import paramiko
from typing import Callable, Dict, List, Optional, Tuple
import serial
from script_interface import BaseScript, DatabaseCache, get_inventory_db_path, get_tracker, ssh_connect_with_credential_fallback, CredentialPromptRequired, NEEDS_CREDENTIALS_SENTINEL
from utils.helpers import get_known_hosts_path, get_host_key_policy, safe_load_host_keys, safe_save_host_keys
from utils.serial_helpers import serial_login, capture_until_prompt
from utils.credentials import get_default_credentials_to_try

class Script(BaseScript):
    def __init__(self, *,
                 db_path=None,
                 db_cache=None,
                 connection_type='serial',
                 serial_port=None,
                 baud_rate=None,
                 timeout=5,
                 ip_address=None,
                 username='admin',
                 password='admin',
                 command_tracker=None,
                 stop_callback=None):
        # DB wiring
        if db_cache is not None:
            self.db_cache = db_cache
            self.db_path = db_cache.db_path
            self.command_tracker = command_tracker or get_tracker()
        else:
            if db_path is None:
                db_path = get_inventory_db_path()
            self.db_path = os.path.abspath(db_path)
            self.db_cache = DatabaseCache(self.db_path)

        # Connection params
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.device_name = None
        self.device_type = None
        self.stop_callback = stop_callback
        self.ssh_client = None
        self.serial_port_obj = None

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    def sleep_with_abort(self, seconds: float, interval: float = 0.1) -> bool:
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.should_stop():
                return True
            time.sleep(min(interval, end_time - time.time()))
        return self.should_stop()

    def abort_connection(self):
        """Forcefully close SSH and serial connections to interrupt blocking I/O."""
        if self.ssh_client:
            try:
                self.ssh_client.close()
                logging.info("SSH connection forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing SSH: {e}")
            finally:
                self.ssh_client = None
        
        if self.serial_port_obj:
            try:
                self.serial_port_obj.close()
                logging.info("Serial connection forcefully closed for abort.")
            except Exception as e:
                logging.debug(f"Error force-closing serial: {e}")
            finally:
                self.serial_port_obj = None

    def get_part_description(self, part_number: str) -> str:
        # Use the shared cache
        return self.db_cache.lookup_part((part_number or "")[:10])

    def get_commands(self) -> List[str]:
        return [
            'show chassis detail  | match "(Name.+)|(Type.+)|(Part.+)|(Serial.+)" pre-lines 1 expression',
            'show card a detail | match expression "(Slot)|(^A)|(Part number)|(Serial number)"',
            'show card b detail | match expression "(Slot)|(^B)|(Part number)|(Serial number)"',
            'show mda detail',
            'show port detail | match "(Optical Compliance.+)|(Serial.+)|(Model.+)|(Part.+)|(Interface +: [0-9/]+)" expression'
        ]

    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        if self.connection_type == 'serial':
            return self.execute_serial_commands(commands)
        elif self.connection_type == 'ssh':
            return self.execute_ssh_commands(self.ip_address, self.username, self.password, commands)
        else:
            raise ValueError("Invalid connection type")
    
    def execute_ssh_commands(self, ip_address: str, username: str, password: str, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        shell = None
        try:
            # Nokia 7705/7250 SSH servers don't support exec_command and close the
            # transport when the identification shell channel ends — always open a
            # fresh connection here.
            injected = getattr(self, '_injected_ssh_client', None)
            self._injected_ssh_client = None
            if injected is not None:
                try:
                    injected.close()
                except Exception:
                    pass

            _kh = str(get_known_hosts_path())
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.load_system_host_keys()
            safe_load_host_keys(self.ssh_client, _kh)
            self.ssh_client.set_missing_host_key_policy(get_host_key_policy())
            logging.info(f"Connecting to {ip_address}")
            try:
                used_user, used_pass = ssh_connect_with_credential_fallback(
                    self.ssh_client,
                    ip_address,
                    username,
                    password,
                    timeout=10,
                )
            except CredentialPromptRequired:
                logging.info(
                    f"Default credentials exhausted for {ip_address}; "
                    f"parking in pause queue for user-credential entry"
                )
                return [], NEEDS_CREDENTIALS_SENTINEL
            except paramiko.AuthenticationException as ae:
                logging.error(f"Authentication failed for {ip_address}: {ae}")
                return [], f"Authentication failed for {ip_address}. Skipping this device."
            safe_save_host_keys(self.ssh_client, _kh)
            logging.info(f"Connected to {ip_address}")

            shell = self.ssh_client.invoke_shell()
            if self.sleep_with_abort(1):
                return [], "Aborted"

            outputs = []
            for command in commands:
                if self.should_stop():
                    return outputs, "Aborted"
                output = self.capture_full_output_ssh(shell, command)
                if output is None:
                    error_message = "Aborted" if self.should_stop() else f"Failed to execute command: {command}"
                    logging.error(error_message)
                    return outputs, error_message
                outputs.append(output)

            return outputs, None

        except Exception as e:
            logging.error(f"SSH connection failed: {e}")
            return [], str(e)
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

    def capture_full_output_ssh(self, shell, command: str) -> Optional[str]:
        try:
            logging.debug(f"Executing command: {command}")
            shell.send(command + '\n')

            output = ""
            while True:
                if self.should_stop():
                    return None
                if shell.recv_ready():
                    chunk = shell.recv(65535).decode('utf-8', errors='replace')
                    output += chunk
                    if "Press any key to continue" in chunk:
                        shell.send(' ')
                        output = output.replace("Press any key to continue (Q to quit)", "")
                        if self.sleep_with_abort(2):
                            return None
                else:
                    if self.sleep_with_abort(1):
                        return None
                    if not shell.recv_ready() and not shell.recv_stderr_ready():
                        break

            logging.debug(f"Output: {output}")
            return output

        except Exception as e:
            logging.error(f"Exception executing command: {e}")
            return None

    def execute_serial_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        try:
            self.serial_port_obj = serial.Serial(self.serial_port, self.baud_rate, timeout=self.timeout)
            logging.info(f"Connected to serial port {self.serial_port}")

            # Authenticate against the console using the default credential
            # list (same seed pool as bulk SSH). Caller-supplied user/pass
            # is tried first so a primary set from the GUI still wins.
            defaults = []
            if self.username and self.password:
                defaults.append((self.username, self.password))
            for pair in get_default_credentials_to_try():
                if pair not in defaults:
                    defaults.append(pair)

            ok, used = serial_login(
                self.serial_port_obj,
                defaults,
                timeout=10.0,
                should_stop=self.should_stop,
            )
            if not ok:
                self.serial_port_obj.close()
                self.serial_port_obj = None
                return [], f"Serial login failed on {self.serial_port}: defaults exhausted."
            if used:
                logging.info(f"[SERIAL] Authenticated on {self.serial_port} as {used[0]!r}")

            outputs = []
            for command in commands:
                if self.should_stop():
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, "Aborted"
                output = self.capture_full_output_serial(self.serial_port_obj, command)
                if output is None:
                    error_message = "Aborted" if self.should_stop() else f"Failed to execute command: {command}"
                    logging.error(error_message)
                    self.serial_port_obj.close()
                    self.serial_port_obj = None
                    return outputs, error_message
                outputs.append(output)

            self.serial_port_obj.close()
            self.serial_port_obj = None
            return outputs, None

        except Exception as e:
            logging.error(f"Serial connection failed: {e}")
            self.serial_port_obj = None
            return [], str(e)

    def capture_full_output_serial(self, ser, command: str) -> str:
        logging.info(f"Executing command: {command}")
        return capture_until_prompt(
            ser, command, timeout=20.0, should_stop=self.should_stop
        )


    def extract_hardware_data(self, output: str, cache_callback: Callable[[pd.DataFrame, str], None], ip: str) -> None:
        data = []
        system_info = {}

        try:
            # Extract system info
            info_pattern = re.compile(r"Name\s*:\s*([^\r\n]+).*?Type\s*:\s*([^\r\n]+)", re.DOTALL)
            info_match = info_pattern.search(output)
            if info_match:
                system_info['System Name'] = info_match.group(1).strip()
                system_info['System Type'] = info_match.group(2).strip()
                self.device_name = system_info['System Name']
                self.device_type = system_info['System Type']
            else:
                logging.warning("No system information found in the output.")

            # `[ \t]*` (not `\s*`) around `:` so an empty value line cannot
            # consume the trailing newline and capture the next line as the
            # value — see card_detail_pattern for the same hardening.
            hardware_pattern = re.compile(
                r"Part number[ \t]*:[ \t]*(?P<PartNumber>[^\r\n]+).*?"
                r"Serial number[ \t]*:[ \t]*(?P<SerialNumber>[^\r\n]+).*?"
                r"Type[ \t]*:[ \t]*(?P<Type>[^\r\n]+)",
                re.DOTALL | re.IGNORECASE
            )

            all_matches = list(re.finditer(hardware_pattern, output))

            for match in all_matches:
                try:
                    part_number = match.group("PartNumber").strip()[:10] if match.group("PartNumber") else "Unknown"
                    serial_number = match.group("SerialNumber").strip() if match.group("SerialNumber") else "Unknown"
                    part_type = match.group("Type").strip().lower() if match.group("Type") else "Unknown"

                    # Dynamically classify the component
                    if "dc" in part_type:
                        part_type = "Chassis Fan"
                        name = "Chassis Fan"
                    elif "fan-v1" in part_type:
                        part_type = "Chassis"
                        name = "Chassis"
                    else:
                        part_type = "Unknown Component"
                        name = "Unknown Component"

                    # Get part description
                    description = self.get_part_description(part_number)

                    # Append data
                    data.append({
                        'System Name': system_info.get('System Name', 'Unknown'),
                        'System Type': system_info.get('System Type', 'Unknown'),
                        'Type': part_type.title(),
                        'Part Number': part_number,
                        'Serial Number': serial_number,
                        'Description': description,
                        'Name': name,
                        'Source': ip
                    })
                except Exception as match_error:
                    logging.error(f"Error processing hardware match: {match_error}")
                    continue

            if not data:
                logging.warning("No hardware data found in output.")
            else:
                logging.debug(f"Extracted hardware data: {data}")

        except Exception as e:
            logging.error(f"Error in extract_hardware_data: {e}")
            data = [{
                'System Name': "Error",
                'System Type': "Error",
                'Type': "Error",
                'Part Number': "Error",
                'Serial Number': "Error",
                'Description': "Error",
                'Name': "Error",
                'Source': ip
            }]

        # Convert to DataFrame
        df = pd.DataFrame(data)
        if df.empty:
            logging.warning("No data to cache. DataFrame is empty.")
        else:
            logging.info(f"DataFrame populated successfully:\n{df}")

        # Cache the DataFrame using the callback
        cache_callback(df, 'hardware_data')



    def extract_card_details(self, device_output, slot_names, card_label, cache_callback, ip):
        """
        Extracts card details (Type, Part Number, Serial Number) for both Card A and Card B using a flexible regex.
        """
        card_data = []

        try:
            # Match Slot A/B + Type, then Part Number, then Serial Number.
            # `[ \t]*` (not `\s*`) around `:` so an empty value line cannot
            # consume the trailing newline and slurp the next line (e.g. the
            # shell prompt) into the captured value.
            card_detail_pattern = re.compile(
                r"^[ ]*(?P<Slot>[A-Z])\s+(?P<Type>[^\s]+).*?"
                r"Part number[ \t]*:[ \t]*(?P<PartNumber>[^\r\n]+).*?"
                r"Serial number[ \t]*:[ \t]*(?P<SerialNumber>[^\r\n]+)",
                re.MULTILINE | re.DOTALL
            )

            matches = re.finditer(card_detail_pattern, device_output)

            for match in matches:
                slot = match.group("Slot").strip()
                if slot not in slot_names:
                    continue  # Skip if the slot does not match A or B

                try:
                    part_type = match.group("Type").strip() if match.group("Type") else "Unknown"
                    part_number = match.group("PartNumber").strip()[:10] if match.group("PartNumber") else "Unknown"
                    serial_number = match.group("SerialNumber").strip() if match.group("SerialNumber") else "Unknown"
                    description = self.get_part_description(part_number)
                except Exception as e:
                    logging.error(f"Error retrieving part description for Part Number: {part_number}")
                    description = "Unknown Description"

                # Create structured data entry for the card
                card_info = {
                    'System Name': '',
                    'System Type': '',
                    'Type': part_type,
                    'Part Number': part_number,
                    'Serial Number': serial_number,
                    'Description': description,
                    'Information Type': 'Control Card',
                    'Name': f"Slot {slot}",
                    'Source': ip
                }
                card_data.append(card_info)
                logging.debug(f"Extracted info for {slot}: {card_info}")

            if not card_data:
                logging.warning(f"No {card_label} data found in output.")
            else:
                logging.debug(f"Data successfully extracted for {card_label}: {card_data}")

        except Exception as e:
            logging.error(f"Error in extract_{card_label.lower()}_details: {e}")
            card_data.append({
                'System Name': '',
                'System Type': '',
                'Type': "Error",
                'Part Number': "Error",
                'Serial Number': "Error",
                'Description': "Error",
                'Information Type': '',
                'Name': f"{card_label} Error",
                'Source': ip,
            })

        # Create DataFrame for extracted card data
        expected_columns = ['System Name', 'System Type', 'Type', 'Part Number', 'Serial Number',
                            'Description', 'Information Type', 'Name', 'Source']
        df = pd.DataFrame(card_data, columns=expected_columns)

        if df.empty:
            logging.warning(f"No {card_label} data found or parsing failed. Returning empty DataFrame.")
        else:
            logging.info(f"DataFrame for {card_label} is populated:\n{df}")

        logging.debug(df)

        # Cache the DataFrame using the provided callback
        cache_callback(df, f"{card_label}_data")
        return df


    def extract_mda_details(self, output, cache_callback=None, ip=None):
        """
        Extracts MDA details from 'show mda detail' output.

        Handles two cases:
          - Provisioned MDAs (up or down): captured via their Provisioned Type.
          - Equipped-but-unprovisioned MDAs: Provisioned Type is "(not provisioned)";
            the Equipped Type is used as a fallback so the physical card is still recorded.
        """
        mda_data = []

        try:
            output = output.replace("Press any key to continue (Q to quit)", "").strip()
            logging.debug(f"Raw MDA output:\n{output}")

            # --- Primary: parse structured per-MDA detail blocks ---
            # 'show mda detail' produces blocks separated by ===... / MDA N/M / ===... headers.
            # Split on those separators so each chunk covers exactly one MDA.
            #
            # We capture the slot number directly from the header
            # (``MDA 1/5 detail``) — the second group of N/M is the slot.
            # The previous implementation searched the BODY for a line
            # matching ``MDA   : N`` to extract the slot, but some cards
            # (e.g. the 32-port T1/E1 ASAP ``a32-chds1v2``) format their
            # specific-data section without that exact line, so the
            # whole slot was silently dropped at the
            # ``if not mda_m: continue`` check. Pulling the slot from
            # the header is robust against per-card body variations.
            block_splitter = re.compile(
                r"={5,}[\s\S]*?MDA\s+(\d+)/(\d+)[\s\S]*?={5,}", re.MULTILINE
            )
            block_matches = list(block_splitter.finditer(output))

            detail_entries = {}  # mda_num -> entry dict (so duplicates from summary are avoided)

            if block_matches:
                for i, match in enumerate(block_matches):
                    start = match.start()
                    end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(output)
                    block = output[start:end]

                    # Slot number from the matched header. Group 1 is
                    # the chassis index (typically 1); group 2 is the
                    # slot, which is what we want for the MDA Name.
                    header_slot = match.group(2).strip()

                    prov_m = re.search(r'Provisioned Type\s*:\s*([^\r\n]+)', block, re.IGNORECASE)
                    equip_m = re.search(r'Equipped Type\s*:\s*([^\r\n]+)', block, re.IGNORECASE)
                    part_m = re.search(r'Part number[ \t]*:[ \t]*([^\r\n]+)', block, re.IGNORECASE)
                    serial_m = re.search(r'Serial number[ \t]*:[ \t]*([^\r\n]+)', block, re.IGNORECASE)

                    if not part_m or not serial_m:
                        continue

                    mda_num = header_slot
                    prov_type = prov_m.group(1).strip() if prov_m else ""
                    equip_type = equip_m.group(1).strip() if equip_m else ""

                    # Provisioned → use as-is.  Not provisioned → fall back to equipped type.
                    if "(not provisioned)" in prov_type.lower() or not prov_type:
                        mda_type = equip_type
                    else:
                        mda_type = prov_type

                    if not mda_type or "(empty)" in mda_type.lower():
                        continue  # Empty slot — nothing physically installed

                    part_number = part_m.group(1).strip()[:10]
                    serial_number = serial_m.group(1).strip()
                    description = self.get_part_description(part_number)

                    entry = {
                        'System Name': '',
                        'System Type': '',
                        'Type': mda_type,
                        'Part Number': part_number,
                        'Serial Number': serial_number,
                        'Description': description,
                        'Information Type': "MDA Card",
                        'Name': mda_num,
                        'Source': ip or 'Unknown',
                    }
                    detail_entries[mda_num] = entry
                    logging.debug(f"Detail-block MDA entry: {entry}")

            mda_data.extend(detail_entries.values())

            # --- Fallback: summary-table regex for outputs without detail blocks ---
            # Collapse "(not provisioned)\n  <equipped_type>" lines before matching.
            if not mda_data:
                output_sub = re.sub(r'\(not provisioned\)\s*\n\s+(\S+)', r'\1', output)
                summary_pattern = re.compile(
                    r"^\s*\d*\s+(?P<MDA>\d+)\s+(?P<Type>[\w\(\)\-\+]+).*?"
                    r"Part number[ \t]*:[ \t]*(?P<PartNumber>[^\r\n]+).*?"
                    r"Serial number[ \t]*:[ \t]*(?P<SerialNumber>[^\r\n]+)",
                    re.DOTALL | re.MULTILINE,
                )
                for match in summary_pattern.finditer(output_sub):
                    mda_type = match.group("Type").strip()
                    if "(not provisioned)" in mda_type.lower():
                        continue
                    part_number = match.group("PartNumber").strip()[:10]
                    serial_number = match.group("SerialNumber").strip()
                    description = self.get_part_description(part_number)
                    entry = {
                        'System Name': '',
                        'System Type': '',
                        'Type': mda_type,
                        'Part Number': part_number,
                        'Serial Number': serial_number,
                        'Description': description,
                        'Information Type': "MDA Card",
                        'Name': match.group("MDA"),
                        'Source': ip or 'Unknown',
                    }
                    logging.debug(f"Summary-table MDA entry: {entry}")
                    mda_data.append(entry)

            if not mda_data:
                logging.error("No MDA details found in the provided output.")
                return None

        except Exception as e:
            logging.error(f"Error extracting MDA details: {e}")
            mda_data.append({
                'System Name': 'Error',
                'System Type': 'Error',
                'Type': 'Error',
                'Part Number': 'Error',
                'Serial Number': 'Error',
                'Description': 'Error',
                'Information Type': 'Error',
                'Name': 'Error',
                'Source': 'Error',
            })

        # Convert to DataFrame
        df = pd.DataFrame(mda_data)
        if df.empty:
            logging.warning("No MDA data found or parsing failed. Returning empty DataFrame.")
        else:
            logging.info(f"DataFrame for MDA is populated:\n{df}")

        logging.debug("\n" + df.to_string())

        if cache_callback:
            cache_callback(df, 'mda_data')

        return df

    def extract_port_detail(self, output: str, cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None, ip: Optional[str] = None) -> pd.DataFrame:
        port_data = []

        try:
            logging.debug(f"Raw output:\n{output}")
            output = output.replace("Press any key to continue (Q to quit)", "").strip()

            # ✅ Validate output before processing
            if not self.is_valid_output(output, "port detail"):
                logging.warning("Invalid output detected for port detail command.")
                raise ValueError("Invalid port detail output")

            lines = output.split("\n")

            # ✅ Define regex patterns
            interface_pattern = re.compile(r'Interface\s+:\s+([\d\S]+)', re.MULTILINE)
            serial_pattern = re.compile(r'Serial Number\s+:\s*(.+)', re.MULTILINE)
            model_pattern = re.compile(r'Model Number\s+:\s*([^\s]+)', re.MULTILINE)
            part_pattern = re.compile(r'Part Number\s+:\s*([^\s]+)', re.MULTILINE)
            optical_compliance_pattern = re.compile(r'Optical Compliance\s+:\s*(.+)', re.MULTILINE)

            # ✅ Initialize current port entry
            current_interface = None
            current_serial_number = None
            current_model_number = None
            current_part_number = None
            current_optical_compliance = None

            # ✅ Iterate through each line to extract port details
            for i, line in enumerate(lines):
                logging.debug(f"Processing line: {line.strip()}")

                if "Optical Compliance" in line:
                    # 🔹 **Look back for related data, CLOSEST line first**
                    # (Previous 5 lines max). The `show port detail | match`
                    # filter keeps an `Interface` line for EVERY port —
                    # populated or not — but only emits Serial/Model/
                    # Part/Optical Compliance for ports with an SFP. When
                    # the previous port had no SFP (e.g., a 7705 SAR-8
                    # a6-eth-10G card with empty xcme ports 1-4 followed
                    # by an SFP on port 5), the look-back window contains
                    # TWO Interface lines: the empty previous port and
                    # the current populated one. Iterating forward (the
                    # old behavior) picked the EARLIEST Interface — the
                    # previous, empty port — and the SFP got reported
                    # against the wrong port number (1/1/4 instead of
                    # 1/1/5). Iterating backward picks the closest
                    # Interface above the Optical Compliance line, which
                    # is always the current port.
                    for j in range(i - 1, max(-1, i - 6), -1):
                        if not current_interface:
                            interface_match = interface_pattern.search(lines[j])
                            if interface_match:
                                current_interface = interface_match.group(1).strip()

                        if not current_serial_number:
                            serial_match = serial_pattern.search(lines[j])
                            if serial_match:
                                current_serial_number = serial_match.group(1).strip()

                        if not current_part_number:
                            part_match = part_pattern.search(lines[j])
                            if part_match:
                                current_part_number = part_match.group(1).strip()

                        if not current_model_number:
                            model_match = model_pattern.search(lines[j])
                            if model_match:
                                current_model_number = model_match.group(1).strip()[:10]

                    # ✅ **Ensure missing values are assigned properly**
                    if not current_interface:
                        logging.debug(f"Missing Interface in entry. Assigning placeholder.")
                        current_interface = f"Unknown Interface"

                    if not current_serial_number:
                        logging.debug(f"Missing Serial Number in entry. Assigning placeholder.")
                        current_serial_number = f"Unknown Serial"

                    if not current_model_number or current_model_number.lower() == "none":
                        logging.debug(f"Model Number missing, using Part Number instead: {current_part_number}")
                        current_model_number = current_part_number if current_part_number else "Unknown"

                    # ✅ **Extract Optical Compliance**
                    optical_compliance_match = optical_compliance_pattern.search(line)
                    current_optical_compliance = (
                        optical_compliance_match.group(1).strip() if optical_compliance_match else "N/A"
                    )

                    # ✅ **Special case for known model numbers**
                    if current_model_number == "3HE12546AA":
                        current_optical_compliance = "SFP - C37.94"

                    # ✅ **Final Validation: Entry must have valid values**
                    if (
                        current_interface != "Unknown Interface"
                        and current_serial_number != "Unknown Serial"
                        and current_model_number != "Unknown"
                        and current_optical_compliance != "N/A"
                    ):
                        try:
                            # ✅ **Retrieve part description**
                            description = self.get_part_description(current_model_number)
                        except Exception as desc_error:
                            logging.error(f"Error retrieving part description: {desc_error}")
                            description = "Unknown"

                        # ✅ **Create port info dictionary**
                        port_info = {
                            "System Name": '',
                            "System Type": '',
                            "Type": current_optical_compliance,
                            "Part Number": current_model_number,
                            "Serial Number": current_serial_number,
                            "Description": description,
                            "Information Type": "Plugable Optical Transceiver",
                            "Name": current_interface,
                            "Source": ip or "Unknown"
                        }
                        port_data.append(port_info)
                        logging.debug(f"Extracted port data: {port_info}")
                    else:
                        logging.debug(
                            f"Skipping entry due to missing data:\n"
                            f"Interface: {current_interface}, Serial: {current_serial_number}, "
                            f"Model: {current_model_number}, Optical Compliance: {current_optical_compliance}"
                        )

                    # ✅ **Reset values for next entry**
                    current_interface = None
                    current_serial_number = None
                    current_model_number = None
                    current_part_number = None
                    current_optical_compliance = None

            # ✅ **Final validation**
            if not port_data:
                logging.debug("No port data found in output.")
            else:
                logging.debug(f"Data successfully extracted: {port_data}")

        except Exception as e:
            logging.error(f"Error in extract_port_detail: {e}")
            port_data.append({
                "System Name": '',
                "System Type": '',
                "Type": "Error",
                "Part Number": "Error",
                "Serial Number": "Error",
                "Description": "Error",
                "Information Type": "Error",
                "Name": "Error",
                "Source": ip or "Unknown"
            })

        # ✅ **Create DataFrame and remove empty rows**
        df = pd.DataFrame(port_data).dropna(how='all')
        if df.empty:
            logging.debug("No port data found or parsing failed. Returning empty DataFrame.")
        else:
            logging.info(f"DataFrame for port details is populated:\n{df}")

        logging.debug("\n" + df.to_string())

        if cache_callback:
            cache_callback(df, 'port_data')

        return df


    def process_outputs(self, outputs_from_device: List[str], ip_address: str, outputs: Dict[str, Dict[str, Dict]]) -> None:
        
        # Processes outputs from a device and caches parsed data.
        # param outputs_from_device: List of command outputs from the device.
        # param ip_address: IP address or identifier of the device.
        # param outputs: Shared data structure for storing results.
        
        if not outputs_from_device:
            logging.warning(f"No outputs received from device at {ip_address}. Skipping processing.")
            return
        
        # Set device_name and device_type properly
        self.device_name = self.device_name or "Unknown"
        self.device_type = self.device_type or "Unknown"

        system_info = {'System Name': self.device_name, 'System Type': self.device_type}

        processing_functions = [
            lambda output, callback: self.extract_hardware_data(output, callback, ip_address),
            lambda output, callback: self.extract_card_details(output, ['A'], 'Card A', callback, ip_address),
            lambda output, callback: self.extract_card_details(output, ['B'], 'Card B', callback, ip_address),
            lambda output, callback: self.extract_mda_details(output, callback, ip_address),
            lambda output, callback: self.extract_port_detail(output, callback, ip_address),
        ]

        # Ensure outputs_from_device aligns with processing functions
        min_outputs = min(len(outputs_from_device), len(processing_functions))

        if len(outputs_from_device) != len(processing_functions):
            logging.warning(
                f"Mismatch between outputs and processing functions for device {ip_address}. "
                f"Processing only {min_outputs} outputs."
            )

        system_info = {'System Name': self.device_name or 'Unknown', 'System Type': self.device_type or 'Unknown'}

        for idx, (command_output, processing_function) in enumerate(zip(outputs_from_device[:min_outputs], processing_functions[:min_outputs])):
            if not command_output:
                logging.warning(f"Command output {idx} for {ip_address} is empty or None. Skipping this step.")
                continue

            try:
                processing_function(
                    command_output,
                    lambda df, key: self.cache_data_frame(outputs, ip_address, key, df, system_info)
                )
            except Exception as e:
                logging.error(
                    f"Error processing output {idx} for device {ip_address}: {e}",
                    exc_info=True
                )
                continue  # Ensures other functions still execute

        logging.info(f"All outputs processed successfully for device {ip_address}.")

            
    def is_valid_output(self, output: str, command: str) -> bool:
        """
        Validate the output of a command to ensure it matches the expected structure or patterns.

        Args:
            output (str): The raw output from the device.
            command (str): The command that was executed.

        Returns:
            bool: True if the output is valid, False otherwise.
        """
        try:
            if not output or not output.strip():
                logging.warning(f"Empty or missing output for command: {command}")
                return False

            # Validation for known commands
            if command.startswith("show chassis"):
                # Look for expected fields in chassis details
                required_keywords = ["Name", "Type", "Part", "Serial"]
                if all(keyword in output for keyword in required_keywords):
                    logging.debug(f"Output for 'show chassis' validated successfully.")
                    return True
                else:
                    logging.warning(f"Output for 'show chassis' missing expected keywords: {required_keywords}")
                    return False

            elif command.startswith("show card"):
                # Validate card information, expect slots and serial numbers
                if re.search(r"Slot\s*:\s*\w+", output) and "Serial number" in output:
                    logging.debug(f"Output for 'show card' validated successfully.")
                    return True
                else:
                    logging.warning(f"Output for 'show card' missing required patterns or keywords.")
                    return False

            elif command.startswith("show mda"):
                # Validate MDA details, expect slots and part numbers
                if re.search(r"Slot\s*:\s*\w+", output) and "Part number" in output:
                    logging.debug(f"Output for 'show mda' validated successfully.")
                    return True
                else:
                    logging.warning(f"Output for 'show mda' missing required patterns or keywords.")
                    return False

            elif command.startswith("show port"):
                # Validate port details, expect interfaces and compliance details
                required_keywords = ["Interface", "Optical Compliance", "Serial"]
                if all(keyword in output for keyword in required_keywords):
                    logging.debug(f"Output for 'show port' validated successfully.")
                    return True
                else:
                    logging.warning(f"Output for 'show port' missing expected keywords: {required_keywords}")
                    return False

            # General validation for unknown commands
            if len(output.strip()) > 10:  # Arbitrary threshold for meaningful data
                logging.debug(f"Output for unknown command '{command}' contains sufficient data.")
                return True
            else:
                logging.warning(f"Output for unknown command '{command}' is too short or meaningless.")
                return False

        except Exception as e:
            logging.error(f"Error validating output for command '{command}': {e}")
            return False

    def cache_data_frame(self, outputs: Dict[str, Dict[str, Dict]], ip: str, key: str, df: pd.DataFrame, system_info: Dict[str, str]) -> bool:
        try:
            if ip not in outputs:
                outputs[ip] = {}
            outputs[ip][key] = {'DataFrame': df, 'System Info': system_info}
            logging.info(f"DataFrame for {ip} under key {key} cached successfully.")
            return True
        except Exception as e:
            logging.error(f"Failed to cache data for {ip} under key {key}. Error: {e}")
            return False
        
    def print_cached_data(self, outputs: Dict[str, Dict[str, Dict]]) -> None:
        try:
            if not outputs:
                logging.warning("No data has been cached to display.")
                print("No cached data to display.")
                return

            print("\n--- All Cached DataFrames ---")
            for ip, ip_data in outputs.items():
                print(f"\nIP Address: {ip}")
                for key, data in ip_data.items():
                    print(f"  Key: {key}")
                    print("  DataFrame:")
                    print(data['DataFrame'])
                    print("  System Info:")
                    for info_key, info_value in data['System Info'].items():
                        print(f"    {info_key}: {info_value}")

            logging.info("All cached data has been displayed successfully.")
        except Exception as e:
            logging.error(f"Failed to print cached data. Error: {e}")
            print(f"Error while printing cached data: {e}")

    
    def combine_and_format_data(self, ip_data: Dict[str, Dict]) -> pd.DataFrame:
        all_data = []
        logging.info(f"Starting combination of {len(ip_data)} data entries.")
        for key, data in ip_data.items():
            df = data['DataFrame']
            all_data.append(df)
            logging.info(f"Processed DataFrame under key '{key}' with {len(df)} rows.")

        if all_data:
            combined_df = pd.concat(all_data, ignore_index=True)
            logging.info(f"Combined DataFrame created with {len(combined_df)} rows from {len(all_data)} DataFrames.")
        else:
            combined_df = pd.DataFrame()
            logging.warning("No data to combine. Returning empty DataFrame.")
    
        return combined_df
            