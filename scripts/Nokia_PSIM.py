"""Inventory script for the Nokia 1830 PSIM (PSI-M shelf).

Compared to the full PSI (PSI-4L / PSI-8L) script, PSIM speaks a leaner CLI
surface that maps cleanly to four commands:

    show general detail           — name + system description (TID)
    show shelf inventory *        — chassis P/N + serial
    show card inventory *         — MEC2 / MFC / FAN / PF cards
    show interface inventory *    — module / transceiver inventory (often empty)

The login flow and Telnet/SSH transport handling are inherited from the
existing Nokia_PSI script — only ``get_commands``, the parsers, and
``process_outputs`` change here.
"""
import logging
import re
from typing import Callable, Dict, List, Optional

import pandas as pd

from scripts.Nokia_PSI import Script as PSIScript


class Script(PSIScript):
    """Nokia 1830 PSIM inventory scraper."""

    # ------------------------------------------------------------------
    # Command list — PSIM-specific
    # ------------------------------------------------------------------

    def get_commands(self) -> List[str]:
        return [
            'show general detail',         # name + system description (TID)
            'show shelf inventory *',      # chassis P/N + serial (PSI-M shelf)
            'show card inventory *',       # MEC2 / MFC / FAN / PF cards
            'show interface inventory *',  # module/transceiver inventory
        ]

    # ------------------------------------------------------------------
    # Output parsers
    # ------------------------------------------------------------------

    def extract_general_detail(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show general detail' for the device name (TID) and system type."""
        system_data = []
        try:
            output = output.strip()
            name_match = re.search(r"^\s*Name\s*:\s*(\S.*?)\s*$", output, re.MULTILINE)
            desc_match = re.search(
                r"^\s*System\s+Description\s*:\s*(\S.*?)\s*$", output, re.MULTILINE
            )
            sw_match = re.search(r"^\s*S/W\s+Version\s*:\s*(\S.*?)\s*$", output, re.MULTILINE)
            prompt_match = re.search(
                r"^\s*([A-Za-z0-9._-]+)#\s*show\s+general\s+detail",
                output, re.MULTILINE,
            )

            system_name = (
                name_match.group(1).strip() if name_match
                else (prompt_match.group(1).strip() if prompt_match else "Unknown")
            )
            system_desc = desc_match.group(1).strip() if desc_match else ""
            # Pull the PSIM platform string out of the system description.
            # Typical: "Nokia 1830 PSIM v23.12.0 SONET ADM"
            system_type_match = re.search(
                r"\bNokia\s+1830\s+PSIM\b[^\r\n]*", system_desc, re.IGNORECASE
            )
            system_type = system_type_match.group(0).strip() if system_type_match else (
                system_desc or "Nokia 1830 PSIM"
            )
            sw_version = sw_match.group(1).strip() if sw_match else ""

            system_data.append({
                'System Name': system_name,
                'System Type': system_type,
                'Type': 'Shelf',
                'Part Number': '',
                'Serial Number': '',
                'Description': f"{system_type}{(' | SW ' + sw_version) if sw_version else ''}",
                'Name': system_name,
                'Source': ip or 'Unknown',
            })
            logging.info(
                f"[PSIM] Identified shelf: name={system_name!r} type={system_type!r}"
            )
        except Exception as e:
            logging.error(f"Error in extract_general_detail: {e}")
            system_data.append({
                'System Name': 'Error', 'System Type': 'Error', 'Type': 'Error',
                'Part Number': 'Error', 'Serial Number': 'Error',
                'Description': 'Error', 'Name': 'Error', 'Source': ip or 'Unknown',
            })

        df = pd.DataFrame(system_data)
        if cache_callback:
            cache_callback(df, 'shelf_detail')
        return df

    def extract_shelf_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show shelf inventory *' for the PSI-M shelf row.

        Layout:
          Shelf  Type      Part Number       Serial Number       CLEI
          --------------------------------------------------------------
              1  PSI-M     3KC81791AAHC04    RT261300392         WOMSG00ERD
        """
        shelf_data = []
        try:
            for raw in output.strip().splitlines():
                line = raw.strip()
                # Skip headers / separators
                if not line or line.startswith('-') or line.lower().startswith('shelf'):
                    continue
                tokens = line.split()
                # Require: shelf_num + type + part_number + serial_number (CLEI optional)
                if len(tokens) < 4 or not tokens[0].isdigit():
                    continue
                shelf_num, shelf_type, part_number, serial_number = tokens[:4]
                clei = tokens[4] if len(tokens) >= 5 else ''
                description = self.db_cache.lookup_part(part_number[:10])
                shelf_data.append({
                    'System Name': '',
                    'System Type': shelf_type,
                    'Type': 'Shelf',
                    'Part Number': part_number[:10],
                    'Serial Number': serial_number,
                    'Description': description or shelf_type,
                    'Name': f"Shelf {shelf_num}",
                    'Source': ip or 'Unknown',
                })

            if not shelf_data:
                logging.warning("[PSIM] No shelf inventory rows parsed.")
        except Exception as e:
            logging.error(f"Error in extract_shelf_inventory (PSIM): {e}")

        df = pd.DataFrame(shelf_data)
        if cache_callback:
            cache_callback(df, 'shelf_inventory')
        return df

    def extract_card_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show card inventory *'.

        Layout:
          Location  Card Type   Mnemonic   Part Number       Serial Number    CLEI
          --------------------------------------------------------------------
              1/11  MEC2        MEC2       3KC81775AANC05    RT261014919      WOCUBDTUAE
              1/13  MFC         PSIMMFC    3KC81791AAHC04    RT261300392      WOMSG00ERD
              1/21  FAN         PSIMFAN    3KC81709AAAB02    RT260706123      WOCUBDSUAB
              1/26  PF          PSIMPFAC   1AF30787AA        QB254601194      -
        """
        card_data = []
        try:
            for raw in output.strip().splitlines():
                line = raw.strip()
                if not line or line.startswith('-') or line.lower().startswith('location'):
                    continue
                # Must lead with a slot like "1/11"
                if not re.match(r'^\d+/\d+\b', line):
                    continue
                tokens = line.split()
                if len(tokens) < 5:
                    continue
                location, card_type, mnemonic, part_number, serial_number = tokens[:5]
                clei = tokens[5] if len(tokens) >= 6 else ''
                description = self.db_cache.lookup_part(part_number[:10])
                card_data.append({
                    'System Name': '',
                    'System Type': '',
                    'Type': mnemonic.upper() if mnemonic else card_type.upper(),
                    'Part Number': part_number[:10],
                    'Serial Number': serial_number,
                    'Description': description or card_type,
                    'Name': location,
                    'Source': ip or 'Unknown',
                })
            if not card_data:
                logging.warning("[PSIM] No card inventory rows parsed.")
        except Exception as e:
            logging.error(f"Error in extract_card_inventory (PSIM): {e}")

        df = pd.DataFrame(card_data)
        if cache_callback:
            cache_callback(df, 'card_inventory')
        return df

    def extract_interface_inventory(
        self,
        output: str,
        cache_callback: Optional[Callable[[pd.DataFrame, str], None]] = None,
        ip: Optional[str] = None,
    ) -> pd.DataFrame:
        """Parse 'show interface inventory *'.

        Layout (typically empty on the M-shelf):
          Location   Module Type   Part Number   Serial Number
        """
        iface_data = []
        try:
            for raw in output.strip().splitlines():
                line = raw.strip()
                if not line or line.startswith('-') or line.lower().startswith('location'):
                    continue
                if not re.match(r'^\d+/\S+', line):
                    continue
                tokens = line.split()
                if len(tokens) < 4:
                    continue
                location, module_type, part_number, serial_number = tokens[:4]
                description = self.db_cache.lookup_part(part_number[:10])
                iface_data.append({
                    'System Name': '',
                    'System Type': '',
                    'Type': module_type,
                    'Part Number': part_number[:10],
                    'Serial Number': serial_number,
                    'Description': description or module_type,
                    'Name': f"Module {location}",
                    'Source': ip or 'Unknown',
                })
            if not iface_data:
                logging.info("[PSIM] 'show interface inventory *' returned no transceiver rows.")
        except Exception as e:
            logging.error(f"Error in extract_interface_inventory (PSIM): {e}")

        df = pd.DataFrame(iface_data)
        if cache_callback:
            cache_callback(df, 'interface_inventory')
        return df

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict[str, Dict[str, Dict]],
    ) -> None:
        if not outputs_from_device:
            logging.warning(f"[PSIM] No outputs received from {ip_address}. Skipping.")
            return

        processing_functions = [
            lambda out, cb: self.extract_general_detail(out, cb, ip_address),
            lambda out, cb: self.extract_shelf_inventory(out, cb, ip_address),
            lambda out, cb: self.extract_card_inventory(out, cb, ip_address),
            lambda out, cb: self.extract_interface_inventory(out, cb, ip_address),
        ]

        system_info = {'System Name': '', 'System Type': ''}

        if len(outputs_from_device) != len(processing_functions):
            logging.warning(
                f"[PSIM] Output/function count mismatch for {ip_address}: "
                f"expected {len(processing_functions)}, got {len(outputs_from_device)}."
            )

        for idx, (command_output, fn) in enumerate(
            zip(outputs_from_device, processing_functions)
        ):
            if not command_output:
                logging.warning(
                    f"[PSIM] Output {idx} for {ip_address} is empty. Skipping."
                )
                continue
            try:
                fn(
                    command_output,
                    lambda df, key: self.cache_data_frame(
                        outputs, ip_address, key, df, system_info
                    ),
                )
            except Exception as e:
                logging.error(
                    f"[PSIM] Error processing output {idx} for {ip_address}: {e}",
                    exc_info=True,
                )

        logging.info(f"[PSIM] All outputs processed for {ip_address}.")

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def is_valid_output(self, output: str, command: str) -> bool:
        try:
            if not output or not output.strip():
                logging.warning(f"[PSIM] Empty output for command: {command}")
                return False

            if command.startswith("show general detail"):
                return bool(re.search(r"^\s*Name\s*:", output, re.MULTILINE))
            if command.startswith("show shelf inventory"):
                return bool(re.search(r"^\s*\d+\s+\S+\s+\S+\s+\S+", output, re.MULTILINE))
            if command.startswith("show card inventory"):
                return bool(re.search(r"^\s*\d+/\d+\s+\S+\s+\S+\s+\S+\s+\S+", output, re.MULTILINE))
            if command.startswith("show interface inventory"):
                # Empty output is acceptable here; just verify the header presence.
                return bool(re.search(r"^\s*Location", output, re.MULTILINE | re.IGNORECASE))
            return len(output.strip()) > 10
        except Exception as e:
            logging.error(f"[PSIM] Error validating output for '{command}': {e}")
            return False
