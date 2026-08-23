import logging
import re
from typing import Callable, Dict, List, Optional

import pandas as pd


class RawNokiaTranscriptMixin:
    RAW_DEVICE_TYPE = "Nokia"

    @staticmethod
    def _normalize_model_part_number(raw_model: str) -> str:
        """Extract a stable part key from a raw model string.

        Handles vendor-prefixed values like:
          - "ALCATEL 3FE62600AA03 ..."
          - "COUIA7FPAAXCVR-S10V31 ..."
        by selecting the first token that looks like a part identifier
        (contains both letters and digits), then truncating to the DB key
        length used across ATLAS.
        """
        tokens = re.findall(r"[A-Za-z0-9-]+", raw_model)
        for token in tokens:
            if re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
                return token[:10]
        return (tokens[0] if tokens else raw_model)[:10]

    @staticmethod
    def _is_usable_description(description: str) -> bool:
        if not description:
            return False
        desc = description.strip().lower()
        if not desc:
            return False
        if desc in {"not found", "unknown", "invalid part number"}:
            return False
        if desc.startswith("db error"):
            return False
        return True

    @staticmethod
    def _find_port_speed(output: str, interface_name: str, current_match: re.Match[str]) -> str:
        speed_match = re.search(r"(?:Oper Speed|Speed)\s*:\s*([^\r\n]+)", current_match.group(0), re.IGNORECASE)
        if speed_match:
            return speed_match.group(1).strip()

        child_speed_match = re.search(
            rf"^Interface\s*:\s*{re.escape(interface_name)}/\d+.*?(?:Oper Speed|Speed)\s*:\s*([^\r\n]+)",
            output,
            re.IGNORECASE | re.MULTILINE,
        )
        if child_speed_match:
            return child_speed_match.group(1).strip()

        return ""

    def get_commands(self) -> List[str]:
        return [
            'show chassis detail | match expression',
            'show mda detail | match expression',
            'show port detail | match expression',
        ]

    def extract_hardware_data(
        self,
        output: str,
        cache_callback: Callable[[pd.DataFrame, str], None],
        ip: str,
    ) -> pd.DataFrame:
        hardware_data = []
        self.device_name = ip
        self.device_type = self.RAW_DEVICE_TYPE

        chassis_match = re.search(
            r"Chassis\s+\d+\s+Detail.*?Part number[ \t]*:[ \t]*([^\r\n]+).*?Serial number[ \t]*:[ \t]*([^\r\n]+)",
            output,
            re.IGNORECASE | re.DOTALL,
        )
        if chassis_match:
            part_number = chassis_match.group(1).strip()[:10]
            serial_number = chassis_match.group(2).strip()
            hardware_data.append(
                {
                    "System Name": ip,
                    "System Type": self.RAW_DEVICE_TYPE,
                    "Type": "Chassis",
                    "Part Number": part_number,
                    "Serial Number": serial_number,
                    "Description": self.get_part_description(part_number),
                    "Name": "Chassis",
                    "Source": ip,
                }
            )

        fan_blocks = re.finditer(
            r"(?P<header>Fan tray number\s*:\s*(?P<tray>[^\r\n]+)|Fan Information)(?P<body>.*?)(?=Power\s+(?:Supply|Feed)\s+Information|$)",
            output,
            re.IGNORECASE | re.DOTALL,
        )
        for match in fan_blocks:
            body = match.group("body")
            part_match = re.search(r"Part number[ \t]*:[ \t]*([^\r\n]+)", body, re.IGNORECASE)
            serial_match = re.search(r"Serial number[ \t]*:[ \t]*([^\r\n]+)", body, re.IGNORECASE)
            if not part_match or not serial_match:
                continue
            tray_name = match.group("tray")
            part_number = part_match.group(1).strip()[:10]
            serial_number = serial_match.group(1).strip()
            hardware_data.append(
                {
                    "System Name": ip,
                    "System Type": self.RAW_DEVICE_TYPE,
                    "Type": "Chassis Fan",
                    "Part Number": part_number,
                    "Serial Number": serial_number,
                    "Description": self.get_part_description(part_number),
                    "Name": f"Fan Tray {tray_name.strip()}" if tray_name else "Fan",
                    "Source": ip,
                }
            )

        df = pd.DataFrame(hardware_data)
        if df.empty:
            logging.warning("No raw hardware data found in output.")
        if cache_callback:
            cache_callback(df, "hardware_data")
        return df

    def extract_mda_details(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        mda_data = []
        source = ip or "Unknown"

        for match in re.finditer(
            r"^MDA\s+(?P<slot>\d+/\d+)\s+detail(?P<body>.*?)(?=^MDA\s+\d+/\d+\s+detail|\Z)",
            output,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        ):
            body = match.group("body")
            part_match = re.search(r"^\s*Part number\s*:[ \t]*([^\r\n]*)", body, re.IGNORECASE | re.MULTILINE)
            serial_match = re.search(r"^\s*Serial number\s*:[ \t]*([^\r\n]*)", body, re.IGNORECASE | re.MULTILINE)
            state_match = re.search(r"Operational state\s*:\s*([^\r\n]+)", body, re.IGNORECASE)
            if not part_match or not serial_match:
                continue

            part_number = part_match.group(1).strip()[:10]
            serial_number = serial_match.group(1).strip()
            if not part_number or not serial_number:
                continue
            description = self.get_part_description(part_number)
            state = state_match.group(1).strip().lower() if state_match else ""
            type_label = description if self._is_usable_description(description) else "MDA"
            if state:
                type_label = f"{type_label} ({state})"

            mda_data.append(
                {
                    "System Name": ip or "",
                    "System Type": self.RAW_DEVICE_TYPE,
                    "Type": type_label,
                    "Part Number": part_number,
                    "Serial Number": serial_number,
                    "Description": description,
                    "Information Type": "MDA Card",
                    "Name": match.group("slot"),
                    "Source": source,
                }
            )

        df = pd.DataFrame(mda_data)
        if df.empty:
            logging.warning("No raw MDA data found in output.")
        if cache_callback:
            cache_callback(df, "mda_data")
        return df

    def extract_port_detail(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        port_data = []
        source = ip or "Unknown"

        for match in re.finditer(
            r"^Interface\s*:\s*(?P<interface>[^\s]+)(?P<body>.*?)(?=^Interface\s*:|\Z)",
            output,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        ):
            block = match.group("body")
            interface_name = match.group("interface")
            model_match = re.search(r"Model Number\s*:\s*([^\r\n]+)", block, re.IGNORECASE)
            serial_match = re.search(r"Serial Number\s*:\s*([^\r\n]+)", block, re.IGNORECASE)
            if not model_match or not serial_match:
                continue

            raw_model = model_match.group(1).strip()
            if raw_model.lower() in {"none", "n/a", "na"}:
                continue
            part_number = self._normalize_model_part_number(raw_model)
            serial_number = serial_match.group(1).strip()
            speed = self._find_port_speed(output, interface_name, match)
            description = self.get_part_description(part_number)
            type_label = description if self._is_usable_description(description) else (speed or "Optic")

            port_data.append(
                {
                    "System Name": ip or "",
                    "System Type": self.RAW_DEVICE_TYPE,
                    "Type": type_label,
                    "Part Number": part_number,
                    "Serial Number": serial_number,
                    "Description": description,
                    "Information Type": "Plugable Optical Transceiver",
                    "Name": interface_name,
                    "Source": source,
                }
            )

        df = pd.DataFrame(port_data)
        if df.empty:
            logging.warning("No raw port data found in output.")
        if cache_callback:
            cache_callback(df, "port_data")
        return df

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict[str, Dict[str, Dict]],
    ) -> None:
        if not outputs_from_device:
            logging.warning(f"No outputs received from raw device at {ip_address}.")
            return

        self.device_name = ip_address
        self.device_type = self.RAW_DEVICE_TYPE
        system_info = {"System Name": ip_address, "System Type": self.RAW_DEVICE_TYPE}

        processing_functions = [
            lambda output, callback: self.extract_hardware_data(output, callback, ip_address),
            lambda output, callback: self.extract_mda_details(output, callback, ip_address),
            lambda output, callback: self.extract_port_detail(output, callback, ip_address),
        ]

        for idx, (command_output, processing_function) in enumerate(
            zip(outputs_from_device[: len(processing_functions)], processing_functions),
        ):
            if not command_output or not command_output.strip():
                logging.warning(f"Raw command output {idx} for {ip_address} is empty. Skipping.")
                continue
            processing_function(
                command_output,
                lambda df, key: self.cache_data_frame(outputs, ip_address, key, df, system_info),
            )