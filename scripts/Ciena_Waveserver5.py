"""scripts/Ciena_Waveserver5.py — Inventory script for Ciena Waveserver 5.

Serial-only for v1. The Waveserver 5's only inventory command of
interest is ``chassis inventory show``, which produces a boxed ASCII
table:

::

    +---------+-----------+----------------------------------+--+
    |  Unit   |   State   | Model                            |...
    +---------+-----------+----------------------------------+--+
    | Chassis | Up        | Waveserver 5 Chassis             |...
    | CM-1    | Up        | Waveserver 5 Control Processor M |...
    |         |           | odule                            |...
    | AP-1    | Up        | Waveserver 5 Access Panel        |...
    | PSU-1   | Up        | Waveserver 5 AC/DC Power and Fan |...
    |         |           |  Module                          |...
    ...
    +---------+-----------+----------------------------------+--+

Two quirks the parser has to handle:

* **Wrapped cells.** Long Model values bleed into a continuation row
  with the Unit cell blank — those bytes have to be appended to the
  previous row's Model.
* **``--more--`` paging.** ``utils.serial_helpers._read_until`` now
  auto-sends a space on ``--more--`` (added at the same time as this
  script), so the calling helper sees the full output as a single
  string with no manual key-press logic here.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from script_interface import (
    BaseScript,
    DatabaseCache,
    get_inventory_db_path,
    get_tracker,
)

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

# Waveserver 5 ships at 9600 baud on the console port — no alternative
# rates documented, but we keep a list so future revs that ship a
# different default can be added without touching call sites.
_SERIAL_DEFAULT_BAUDS: List[int] = [9600]

# Factory default is ``su`` with NO password. The empty-password row
# has to come first so we don't fight a "Password:" prompt we'd never
# satisfy. Subsequent ladder entries cover lab boxes that picked up a
# password through unrelated provisioning.
_SERIAL_DEFAULT_CREDS: List[Tuple[str, str]] = [
    ("su", ""),
    ("su", "su"),
    ("su", "admin"),
]

# How a Unit maps to ATLAS's BoM ``Information Type`` classifier (see
# ``gui/workbook_builder.py:_classify_information_type``):
#   * ``Shelf`` / ``Component`` → Chassis/Shelf bucket
#   * ``Card`` → Cards bucket
# Numbered slots (``1``, ``3``, ...) are inferred at parse time; this
# table only covers the fixed Unit prefixes.
_UNIT_PREFIX_TO_INFO_TYPE = {
    "chassis": "Shelf",
    "cm": "Card",          # Control Processor Module
    "ap": "Component",     # Access Panel
    "psu": "Component",
    "fan": "Component",
}


# ── Parser ─────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# A column-boundary separator line: starts with ``+``, only uses
# ``+/-/=/whitespace``, and contains at least 3 ``+`` so the title
# banner (``+--- CHASSIS INVENTORY TABLE ---+`` with just two ``+``)
# isn't mistaken for one.
_BOUNDARY_CHARS = set("+-= \t")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _is_column_boundary(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("+"):
        return False
    if not all(c in _BOUNDARY_CHARS for c in stripped):
        return False
    return stripped.count("+") >= 3


def _classify_unit(unit: str) -> str:
    """Map a Unit cell value to an ``Information Type``.

    Numbered slots (``1``, ``3``, ...) are treated as cards; named
    slots map via :data:`_UNIT_PREFIX_TO_INFO_TYPE`; anything else
    falls back to ``Component`` so the BoM picks it up under Chassis/
    Shelf.
    """
    u = (unit or "").strip().lower()
    if not u:
        return ""
    if u.isdigit():
        return "Card"
    # ``FAN-1/2`` → split at ``-``/``/`` and use the leading word.
    base = re.split(r"[-/]", u, maxsplit=1)[0]
    return _UNIT_PREFIX_TO_INFO_TYPE.get(base, "Component")


def parse_chassis_inventory_show(raw: str) -> List[Dict[str, str]]:
    """Parse the boxed ``chassis inventory show`` table.

    Returns a list of dicts with keys:
    ``unit, oper_state, model, part_number, serial, rev, mfg_date``.
    Multi-line wrapped cells are joined into single fields. Filler-
    module rows with no part number / serial are kept (callers can
    filter on empty fields if they need to).
    """
    text = _strip_ansi(raw)
    lines = text.splitlines()
    boundaries = [i for i, ln in enumerate(lines) if _is_column_boundary(ln)]
    if len(boundaries) < 2:
        return []

    # Column ranges from the FIRST column-boundary line — the one
    # immediately above the data block. ``+`` positions delimit cells.
    sep_line = lines[boundaries[0]]
    plus_positions = [i for i, c in enumerate(sep_line) if c == "+"]
    if len(plus_positions) < 8:
        # Need 8 '+' to bound 7 columns; bail rather than mis-slicing.
        return []
    col_ranges = [
        (plus_positions[i] + 1, plus_positions[i + 1])
        for i in range(7)
    ]

    rows: List[List[str]] = []
    current: Optional[List[str]] = None
    for idx in range(boundaries[0] + 1, boundaries[-1]):
        line = lines[idx]
        if not line.startswith("|"):
            continue
        if _is_column_boundary(line):
            continue
        cells_raw = [
            line[start:end] if end <= len(line) else line[start:]
            for (start, end) in col_ranges
        ]
        cells = [c.strip() for c in cells_raw]
        unit = cells[0]
        if not unit:
            # Continuation row — concatenate non-empty cells onto the
            # previous row. ``current is None`` means we haven't seen a
            # real row yet (e.g. the wrapped HEADER line); ignore.
            #
            # Whether to insert a space between the joined parts comes
            # from the raw cell's leading-whitespace count:
            #   * 1 leading space = just the cell pad → wrap was
            #     mid-word ("Processor M" + "odule" = "Module"), join
            #     with no separator.
            #   * 2+ leading spaces = cell pad + a real word-separator
            #     space the device preserved across the wrap, join
            #     with a single space ("Fan" + " Module" = "Fan
            #     Module").
            if current is not None:
                for i in range(1, len(cells)):
                    text = cells[i]
                    if not text:
                        continue
                    raw = cells_raw[i]
                    leading = len(raw) - len(raw.lstrip(" "))
                    sep = " " if leading >= 2 else ""
                    current[i] = current[i] + sep + text
            continue
        # Skip the canonical header row (``Unit`` as a column label).
        if unit.lower() == "unit":
            continue
        current = cells[:]
        rows.append(current)

    return [
        {
            "unit": r[0],
            "oper_state": r[1],
            "model": r[2],
            "part_number": r[3],
            "serial": r[4],
            "rev": r[5],
            "mfg_date": r[6],
        }
        for r in rows
    ]


def _resolve_hostname(raw: str) -> str:
    """Pull the hostname out of the trailing CLI prompt (``WS5_1#``).

    Falls back to ``"Waveserver-5"`` (the factory default) if the
    output doesn't carry a prompt — happens when the device hasn't
    been provisioned yet and the command was issued from a bare-name
    shell.
    """
    text = _strip_ansi(raw or "")
    # Match the last "<host>*?#" on its own at end of buffer / line.
    matches = re.findall(r"([A-Za-z0-9_\-]+)\*?#\s*$", text, re.MULTILINE)
    return matches[-1] if matches else "Waveserver-5"


def _strip_known_serial_placeholder(value: str) -> str:
    """``"see label"`` is what the FANs print when their physical SN
    is on a sticker rather than burned-in. Treat as empty so the BoM
    doesn't carry the literal string as a serial number."""
    cleaned = (value or "").strip()
    if cleaned.lower() == "see label":
        return ""
    return cleaned


# ── Script ─────────────────────────────────────────────────────────────


class Script(BaseScript):
    """Waveserver 5 inventory script (serial v1).

    Mirrors the shape of ``scripts.Ciena_RLS.Script`` so the inventory
    pipeline doesn't need a special-case loader.
    """

    _SERIAL_DEFAULT_BAUDS = _SERIAL_DEFAULT_BAUDS
    _SERIAL_DEFAULT_CREDS = _SERIAL_DEFAULT_CREDS

    def __init__(
        self,
        *,
        connection_type: str = "serial",
        command_tracker: Any = None,
        ip_address: Optional[str] = None,
        username: str = "su",
        password: str = "",
        timeout: float = 5,
        db_path: Optional[str] = None,
        db_cache: Any = None,
        stop_callback: Optional[Callable[[], bool]] = None,
        serial_port: Optional[str] = None,
        baud_rate: Optional[int] = None,
    ) -> None:
        # DB wiring — mirrors Ciena_RLS so the inventory pipeline can
        # construct us the same way it constructs every other script.
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

        self.connection_type = connection_type
        self.command_tracker = command_tracker or get_tracker()
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.timeout = timeout
        self.stop_callback = stop_callback
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.serial_port_obj = None

        if connection_type == "serial" and not self.serial_port:
            raise ValueError(
                "Missing required 'serial_port' for serial connection."
            )
        if connection_type != "serial":
            # LAN / SSH inventory for Waveserver 5 isn't wired yet —
            # surface the gap up front rather than swallow it later.
            raise NotImplementedError(
                f"Waveserver 5 inventory currently supports serial only "
                f"(got connection_type={connection_type!r})."
            )

    # ── BaseScript contract ─────────────────────────────────────────────

    def get_commands(self) -> List[str]:
        return ["chassis inventory show"]

    def execute_commands(
        self, commands: List[str]
    ) -> Tuple[List[str], Optional[str]]:
        if self.connection_type == "serial":
            return self.execute_serial_commands(commands)
        return [], f"Unsupported connection type: {self.connection_type!r}"

    def abort_connection(self) -> None:
        """Force-close the serial port to interrupt blocking reads."""
        if self.serial_port_obj is not None:
            try:
                self.serial_port_obj.close()
                logging.debug("[WS5-SERIAL] Closed serial port for abort.")
            except Exception as exc:
                logging.debug(f"[WS5-SERIAL] Error closing port: {exc}")
            finally:
                self.serial_port_obj = None

    def should_stop(self) -> bool:
        return bool(self.stop_callback and self.stop_callback())

    # ── Serial driver ───────────────────────────────────────────────────

    def execute_serial_commands(
        self, commands: List[str]
    ) -> Tuple[List[str], Optional[str]]:
        """Drive the Waveserver 5 console over a serial port.

        Mirrors the RLS path: open with baud probe (single rate for
        Waveserver), authenticate with the factory ``su`` / empty
        ladder, then run each command through ``capture_until_prompt``.
        """
        try:
            from utils.serial_helpers import (
                capture_until_prompt,
                open_serial_with_baud_probe,
                serial_login,
            )
        except Exception as exc:
            logging.error(f"[WS5-SERIAL] Helper import failed: {exc}")
            return [], f"Serial helpers unavailable: {exc}"

        bauds = (
            [int(self.baud_rate)] if self.baud_rate
            else list(self._SERIAL_DEFAULT_BAUDS)
        )

        # 10s probe timeout (was 2.0s) -- WS5 console behaves the same
        # as RLS R4: echoes the wake-CR fast but takes a few seconds
        # to print the actual prompt. The upgrade flow's
        # ``_SERIAL_PROMPT_TIMEOUT`` is already 10s; this matches.
        ser = open_serial_with_baud_probe(
            self.serial_port,
            bauds,
            timeout=10.0,
            should_stop=self.should_stop,
        )
        if ser is None:
            return [], (
                f"No console prompt detected on {self.serial_port} at "
                f"{bauds} baud — check the cable, baud rate, and that "
                f"the device is powered."
            )
        self.serial_port_obj = ser

        try:
            creds: List[Tuple[str, str]] = []
            if self.username is not None:
                creds.append((self.username, self.password or ""))
            for pair in self._SERIAL_DEFAULT_CREDS:
                if pair not in creds:
                    creds.append(pair)

            ok, used = serial_login(
                ser, creds, timeout=10.0, should_stop=self.should_stop,
            )
            if not ok:
                return [], (
                    f"Waveserver 5 serial login failed on "
                    f"{self.serial_port}: all credentials exhausted."
                )
            if used:
                logging.info(
                    f"[WS5-SERIAL] Authenticated as {used[0]!r} on "
                    f"{self.serial_port}"
                )

            outputs: List[str] = []
            # ``chassis inventory show`` can be long enough to trigger
            # paging; give it a generous timeout. The shared helper
            # handles ``--more--`` by sending a space, so all we have
            # to wait for is the final shell prompt.
            cmd_timeout = 60.0
            for command in commands:
                if self.should_stop():
                    return outputs, "Aborted"
                logging.info(f"[WS5-SERIAL] Executing: {command}")
                output = capture_until_prompt(
                    ser, command, timeout=cmd_timeout,
                    should_stop=self.should_stop,
                )
                if output is None:
                    error = (
                        "Aborted" if self.should_stop()
                        else f"Failed to execute command: {command}"
                    )
                    return outputs, error
                outputs.append(output)
                self.command_tracker.mark_as_executed(
                    self.serial_port, command, self.connection_type
                )
            return outputs, None
        except Exception as exc:
            logging.error(f"[WS5-SERIAL] Unhandled error: {exc}", exc_info=True)
            return [], str(exc)
        finally:
            self.abort_connection()

    # ── Output → DataFrame ──────────────────────────────────────────────

    def process_outputs(
        self,
        outputs_from_device: List[str],
        ip_address: str,
        outputs: Dict[str, Dict[str, Any]],
    ) -> None:
        if not outputs_from_device or not outputs_from_device[0]:
            logging.warning(f"No outputs received from {ip_address}.")
            return

        raw = outputs_from_device[0]
        units = parse_chassis_inventory_show(raw)
        if not units:
            logging.warning(
                f"Could not locate a CHASSIS INVENTORY TABLE in output "
                f"from {ip_address} (got {len(raw)} bytes)."
            )
            return

        hostname = _resolve_hostname(raw)
        system_type = "Waveserver 5"
        # Surface the chassis Model when present — operator may have a
        # variant board even though every WL5e module reports the same
        # ``Waveserver 5 Chassis`` string today.
        for u in units:
            if u["unit"].lower() == "chassis":
                system_type = u["model"] or system_type
                break

        system_info = {"System Name": hostname, "System Type": system_type}
        shelf_rows: List[Dict[str, str]] = []
        card_rows: List[Dict[str, str]] = []

        for u in units:
            part_number = u["part_number"].strip()
            serial = _strip_known_serial_placeholder(u["serial"])
            description = (
                self.db_cache.lookup_part(part_number[:10])
                if part_number else ""
            )
            if not description or description in ("Not Found", "Invalid part number"):
                description = u["model"]
            info_type = _classify_unit(u["unit"])
            # Numeric Unit values (1, 3, 5, ...) are bare slot indices on
            # the device — operators expect ``Slot 1`` in the workbook so
            # the column reads like every other inventory script's
            # output. Named units (CM-1, FAN-1/1, PSU-1, ...) are
            # already descriptive and pass through unchanged.
            name = (
                f"Slot {u['unit']}" if u["unit"].strip().isdigit()
                else u["unit"]
            )
            row = {
                "System Name": hostname,
                "System Type": system_type,
                "Type": u["model"] or info_type or "Component",
                "Part Number": part_number,
                "Serial Number": serial,
                "Description": description,
                "Information Type": info_type,
                "Name": name,
                "Source": ip_address or self.serial_port or "Unknown",
            }
            if info_type == "Card":
                card_rows.append(row)
            else:
                # Chassis, AP, PSU, FAN and anything we can't classify
                # — all chassis-level inventory in BoM terms.
                shelf_rows.append(row)

        # ATLAS's workbook builder concatenates every DataFrame under
        # this device key, so we only emit each row in ONE bucket.
        # ``shelf_inventory`` already carries the Chassis row; writing
        # a separate ``shelf_detail`` (as the first cut of this script
        # did) caused the Chassis line item to appear twice in the
        # final workbook with the same part number / serial.
        shelf_df = pd.DataFrame(shelf_rows) if shelf_rows else pd.DataFrame()
        card_df = pd.DataFrame(card_rows) if card_rows else pd.DataFrame()
        self._cache(outputs, ip_address, "shelf_inventory", shelf_df, system_info)
        self._cache(outputs, ip_address, "card_inventory", card_df, system_info)

    # ── Cache helper (mirrors RLS/SAR shape) ────────────────────────────

    @staticmethod
    def _cache(
        outputs: Dict[str, Dict[str, Any]],
        ip: str,
        key: str,
        df: pd.DataFrame,
        system_info: Dict[str, str],
    ) -> None:
        if ip not in outputs:
            outputs[ip] = {}
        outputs[ip][key] = {"DataFrame": df, "System Info": system_info}
