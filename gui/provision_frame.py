"""
gui/provision_frame.py — "Provision" mode: basic network provisioning.

Supports:
  - Nokia 7705 SAR / Nokia 7250 IXR  (via scripts.Network.Nokia_Provision)
  - Nokia 1830 OLS PSI/PSS           (via scripts.Network.Nokia_OLS)
  - Ciena 39XX/51XX SAOS 6.21.5       (via scripts.Network.Ciena_SAOS)
  - Ciena 39XX/51XX/81XX SAOS 10.x   (via scripts.Network.Ciena_SAOS10)

Workflow:
  1. Provision a single device by typing its IP (+ optional hostname), OR
     upload an Excel file (columns: IP, Hostname; optional: Subnet, Gateway)
     and pick one device from the list. A typed IP overrides the dropdown.
  2. Fills in connection + network parameters (device-type-specific extra fields shown).
  3. Clicks Run; output appears in the log area below.
"""
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from serial.tools import list_ports
except Exception:
    list_ports = None

try:
    import openpyxl
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False


# ── Constants ───────────────────────────────────────────────────────────────

_DEVICE_TYPE_OPTIONS = [
    "Auto (from hostname)",
    "Nokia 7705 SAR",
    "Nokia 7250 IXR",
    "Nokia 1830 OLS",
    "Ciena SAOS 6.21.5",
    "Ciena SAOS 10",
]
_DEVICE_TYPE_MAP = {
    "Auto (from hostname)": None,
    "Nokia 7705 SAR": "7705",
    "Nokia 7250 IXR": "7250",
    "Nokia 1830 OLS": "ols",
    "Ciena SAOS 6.21.5": "saos",
    "Ciena SAOS 10": "saos10",
}
_DEFAULT_BAUD = "115200"
_DEFAULT_PREFIX = "22"
_DEFAULT_STATIC_DEST = "10.0.0.0/8"

# SAOS-specific defaults
_SAOS_DEFAULT_VLAN      = "4000"
_SAOS_DEFAULT_IFACE     = "mgmt"
_SAOS_DEFAULT_VLAN_NAME = "mgmt"
_SAOS_DEFAULT_BAUD      = "9600"
_SAOS_DEFAULT_ROUTE     = "0.0.0.0/0"

# SAOS 10-specific defaults
_SAOS10_DEFAULT_BAUD    = "115200"   # 39xx use 9600; 51xx/81xx use 115200
_SAOS10_DEFAULT_ROUTE   = "0.0.0.0/0"

# Nokia 1830 OLS-specific defaults
_OLS_DEFAULT_BAUD  = "38400"
_OLS_DEFAULT_ROUTE = "0.0.0.0/0"
_OLS_SHELF_OPTIONS = ["Auto-detect", "PSI-4L / PSI-8L (MFC)", "PSS-16II (USRPNL)"]
_OLS_SHELF_MAP     = {
    "Auto-detect":          "auto",
    "PSI-4L / PSI-8L (MFC)": "psi",
    "PSS-16II (USRPNL)":    "pss16",
}

# Protocol display names available in SAOS preferred-source-ip
_SAOS_PROTOCOLS = [
    "SSH", "SNMP", "NTP", "Syslog", "RADIUS", "TACACS",
    "DNS", "Telnet", "FTP/TFTP", "RadSec",
]
_SAOS_DEFAULT_PROTOCOLS = {"SSH", "SNMP", "NTP", "Syslog", "RADIUS", "TACACS"}


# ── Helper ──────────────────────────────────────────────────────────────────

def _read_excel_devices(path: str) -> List[Dict[str, str]]:
    """
    Parse an Excel workbook and return a list of device dicts.

    Accepted column names (case-insensitive, first header row):
      - IP / Management IP / Mgmt IP
      - Hostname / Host Name / Name
      - Subnet / Prefix / Prefix Len
      - Gateway / GW / Next-Hop / Next Hop
      - Static Route / Static Route Dest
    """
    if not _HAS_OPENPYXL:
        raise ImportError("openpyxl is required to read Excel files.")

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("The workbook appears to be empty.")

    # Locate headers in the first non-empty row
    header_row_idx = 0
    headers: List[str] = []
    for i, row in enumerate(rows):
        non_empty = [c for c in row if c is not None]
        if non_empty:
            headers = [str(c).strip() if c is not None else "" for c in row]
            header_row_idx = i
            break

    if not headers:
        raise ValueError("No header row found in the workbook.")

    # Build column index map
    col_map: Dict[str, int] = {}
    for idx, h in enumerate(headers):
        lh = h.lower()
        if lh in ("ip", "management ip", "mgmt ip", "mgmt_ip", "management_ip"):
            col_map.setdefault("ip", idx)
        elif lh in ("hostname", "host name", "host_name", "name"):
            col_map.setdefault("hostname", idx)
        elif lh in ("subnet", "prefix", "prefix len", "prefix_len", "prefix length"):
            col_map.setdefault("prefix", idx)
        elif lh in ("gateway", "gw", "next-hop", "next hop", "nexthop"):
            col_map.setdefault("gateway", idx)
        elif lh in ("static route", "static route dest", "static_route", "static_route_dest"):
            col_map.setdefault("static_route", idx)

    if "ip" not in col_map or "hostname" not in col_map:
        raise ValueError(
            "Could not find required 'IP' and 'Hostname' columns. "
            f"Found headers: {headers}"
        )

    devices: List[Dict[str, str]] = []
    for row in rows[header_row_idx + 1:]:
        ip_val = row[col_map["ip"]] if len(row) > col_map["ip"] else None
        hn_val = row[col_map["hostname"]] if len(row) > col_map["hostname"] else None
        if not ip_val or not hn_val:
            continue  # skip empty / header-continuation rows
        device: Dict[str, str] = {
            "ip": str(ip_val).strip(),
            "hostname": str(hn_val).strip(),
        }
        if "prefix" in col_map and len(row) > col_map["prefix"] and row[col_map["prefix"]]:
            device["prefix"] = str(row[col_map["prefix"]]).strip()
        if "gateway" in col_map and len(row) > col_map["gateway"] and row[col_map["gateway"]]:
            device["gateway"] = str(row[col_map["gateway"]]).strip()
        if "static_route" in col_map and len(row) > col_map["static_route"] and row[col_map["static_route"]]:
            device["static_route"] = str(row[col_map["static_route"]]).strip()
        devices.append(device)

    return devices


# ── Frame ───────────────────────────────────────────────────────────────────

class ProvisionFrame(ttk.Frame):
    """Tkinter frame for the Provision (basic network provisioning) mode."""

    def __init__(self, parent: ttk.Frame, controller: Any) -> None:
        super().__init__(parent)
        self.controller = controller
        self._devices: List[Dict[str, str]] = []
        self._stop_flag = False
        self._run_thread: Optional[threading.Thread] = None
        self._build()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build(self) -> None:
        # ── Row 1: Excel file ──────────────────────────────────────────────
        file_frame = ttk.LabelFrame(self, text="Device List (Excel)")
        file_frame.pack(fill=tk.X, padx=5, pady=4)

        self._file_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self._file_var, width=55).pack(
            side=tk.LEFT, padx=(6, 2), pady=4
        )
        ttk.Button(file_frame, text="Browse…", command=self._browse_excel).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(file_frame, text="Load", command=self._load_excel).pack(
            side=tk.LEFT, padx=2
        )
        self._file_status = ttk.Label(file_frame, text="No file loaded", foreground="gray")
        self._file_status.pack(side=tk.LEFT, padx=8)

        # ── Row 2: Device selector ─────────────────────────────────────────
        dev_frame = ttk.LabelFrame(self, text="Device Selection")
        dev_frame.pack(fill=tk.X, padx=5, pady=4)

        # Row 2a: pick one device from the loaded Excel list + device type
        dev_row = ttk.Frame(dev_frame)
        dev_row.pack(fill=tk.X, padx=2, pady=(4, 2))

        ttk.Label(dev_row, text="Device (from Excel):").pack(side=tk.LEFT, padx=(6, 2))
        self._device_var = tk.StringVar()
        self._device_combo = ttk.Combobox(
            dev_row, textvariable=self._device_var, state="readonly", width=40
        )
        self._device_combo.pack(side=tk.LEFT, padx=2)
        self._device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Label(dev_row, text="  Device Type:").pack(side=tk.LEFT, padx=(12, 2))
        self._dtype_var = tk.StringVar(value=_DEVICE_TYPE_OPTIONS[0])
        self._dtype_combo = ttk.Combobox(
            dev_row,
            textvariable=self._dtype_var,
            values=_DEVICE_TYPE_OPTIONS,
            state="readonly",
            width=22,
        )
        self._dtype_combo.pack(side=tk.LEFT, padx=2)
        self._dtype_combo.bind("<<ComboboxSelected>>", self._on_dtype_change)

        # Row 2b: manual single-device entry — overrides the dropdown when IP set
        manual_row = ttk.Frame(dev_frame)
        manual_row.pack(fill=tk.X, padx=2, pady=(0, 4))

        ttk.Label(manual_row, text="— or single device —  IP:").pack(
            side=tk.LEFT, padx=(6, 2)
        )
        self._manual_ip_var = tk.StringVar()
        ttk.Entry(manual_row, textvariable=self._manual_ip_var, width=18).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(manual_row, text="  Hostname (optional):").pack(
            side=tk.LEFT, padx=(10, 2)
        )
        self._manual_host_var = tk.StringVar()
        ttk.Entry(manual_row, textvariable=self._manual_host_var, width=22).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(
            manual_row,
            text="(when IP is set, it overrides the dropdown above)",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(6, 0))

        # ── Row 3: Connection type ─────────────────────────────────────────
        conn_frame = ttk.LabelFrame(self, text="Connection")
        conn_frame.pack(fill=tk.X, padx=5, pady=4)

        self._conn_var = tk.StringVar(value="serial")
        tk.Radiobutton(
            conn_frame, text="Serial (Console)",
            variable=self._conn_var, value="serial",
            command=self._on_conn_type_change,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            conn_frame, text="LAN (SSH)",
            variable=self._conn_var, value="ssh",
            command=self._on_conn_type_change,
        ).pack(side=tk.LEFT, padx=10)

        # Serial sub-frame
        self._serial_frame = ttk.Frame(conn_frame)
        self._serial_frame.pack(side=tk.LEFT, padx=10)
        ttk.Label(self._serial_frame, text="Port:").pack(side=tk.LEFT)
        self._serial_port_var = tk.StringVar()
        self._serial_combo = ttk.Combobox(
            self._serial_frame, textvariable=self._serial_port_var,
            width=10, state="readonly"
        )
        self._serial_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(self._serial_frame, text="↺", width=2, command=self._refresh_ports).pack(
            side=tk.LEFT, padx=1
        )
        ttk.Label(self._serial_frame, text="  Baud:").pack(side=tk.LEFT)
        self._baud_var = tk.StringVar(value=_DEFAULT_BAUD)
        ttk.Entry(self._serial_frame, textvariable=self._baud_var, width=8).pack(
            side=tk.LEFT, padx=2
        )

        # LAN sub-frame
        self._lan_frame = ttk.Frame(conn_frame)
        ttk.Label(self._lan_frame, text="Connect IP:").pack(side=tk.LEFT)
        self._connect_ip_var = tk.StringVar()
        ttk.Entry(self._lan_frame, textvariable=self._connect_ip_var, width=18).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(self._lan_frame, text="(defaults to IP from Excel)", foreground="gray").pack(
            side=tk.LEFT, padx=(2, 0)
        )

        # Credentials sub-frame
        cred_frame = ttk.Frame(conn_frame)
        cred_frame.pack(side=tk.LEFT, padx=14)
        ttk.Label(cred_frame, text="User:").pack(side=tk.LEFT)
        self._user_var = tk.StringVar(value="admin")
        ttk.Entry(cred_frame, textvariable=self._user_var, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Label(cred_frame, text="Password:").pack(side=tk.LEFT, padx=(6, 0))
        self._pass_var = tk.StringVar(value="admin")
        ttk.Entry(cred_frame, textvariable=self._pass_var, show="*", width=10).pack(
            side=tk.LEFT, padx=2
        )

        # ── Row 4: Network parameters ──────────────────────────────────────
        net_frame = ttk.LabelFrame(self, text="Network Parameters")
        net_frame.pack(fill=tk.X, padx=5, pady=4)

        ttk.Label(net_frame, text="Subnet Prefix Len:").pack(side=tk.LEFT, padx=(6, 2))
        self._prefix_var = tk.StringVar(value=_DEFAULT_PREFIX)
        ttk.Entry(net_frame, textvariable=self._prefix_var, width=5).pack(side=tk.LEFT, padx=2)

        ttk.Label(net_frame, text="  Gateway:").pack(side=tk.LEFT, padx=(10, 2))
        self._gw_var = tk.StringVar()
        ttk.Entry(net_frame, textvariable=self._gw_var, width=18).pack(side=tk.LEFT, padx=2)

        ttk.Label(net_frame, text="  Static Route Dest:").pack(side=tk.LEFT, padx=(10, 2))
        self._route_var = tk.StringVar(value=_DEFAULT_STATIC_DEST)
        ttk.Entry(net_frame, textvariable=self._route_var, width=16).pack(side=tk.LEFT, padx=2)

        # ── Row 5: Nokia-specific options ──────────────────────────────────
        self._nokia_opt_frame = ttk.LabelFrame(self, text="Nokia Options")
        self._nokia_opt_frame.pack(fill=tk.X, padx=5, pady=4)

        self._cfg_card_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self._nokia_opt_frame, text="Configure card type", variable=self._cfg_card_var
        ).pack(side=tk.LEFT, padx=10)
        self._sync_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self._nokia_opt_frame,
            text="Sync redundancy (admin redundancy sync boot-env — up to 10 min)",
            variable=self._sync_var,
        ).pack(side=tk.LEFT, padx=10)

        # ── Row 6: SAOS-specific options (hidden unless SAOS selected) ──────
        self._saos_frame = ttk.LabelFrame(self, text="SAOS 6.21.5 Options")

        # Row A: VLAN ID, interface name, VLAN name, management port
        row_a = ttk.Frame(self._saos_frame)
        row_a.pack(fill=tk.X, padx=4, pady=(4, 2))

        ttk.Label(row_a, text="Mgmt VLAN ID:").pack(side=tk.LEFT, padx=(4, 2))
        self._saos_vlan_var = tk.StringVar(value=_SAOS_DEFAULT_VLAN)
        ttk.Entry(row_a, textvariable=self._saos_vlan_var, width=7).pack(side=tk.LEFT, padx=2)

        ttk.Label(row_a, text="  Interface Name:").pack(side=tk.LEFT, padx=(10, 2))
        self._saos_iface_var = tk.StringVar(value=_SAOS_DEFAULT_IFACE)
        ttk.Entry(row_a, textvariable=self._saos_iface_var, width=14).pack(side=tk.LEFT, padx=2)

        ttk.Label(row_a, text="  VLAN Name:").pack(side=tk.LEFT, padx=(10, 2))
        self._saos_vlan_name_var = tk.StringVar(value=_SAOS_DEFAULT_VLAN_NAME)
        ttk.Entry(row_a, textvariable=self._saos_vlan_name_var, width=10).pack(side=tk.LEFT, padx=2)

        ttk.Label(row_a, text="  Mgmt Port (opt):").pack(side=tk.LEFT, padx=(10, 2))
        self._saos_port_var = tk.StringVar()
        ttk.Entry(row_a, textvariable=self._saos_port_var, width=8).pack(side=tk.LEFT, padx=2)

        # Row B: Update-existing toggle
        row_b = ttk.Frame(self._saos_frame)
        row_b.pack(fill=tk.X, padx=4, pady=(0, 2))

        self._saos_update_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_b,
            text="Update existing interface (deletes & recreates the IP interface)",
            variable=self._saos_update_var,
        ).pack(side=tk.LEFT, padx=6)

        # Row C: Protocol checkboxes
        proto_lf = ttk.LabelFrame(self._saos_frame, text="Bind preferred-source-ip protocols")
        proto_lf.pack(fill=tk.X, padx=4, pady=(0, 4))

        self._saos_proto_vars: Dict[str, tk.BooleanVar] = {}
        for i, proto in enumerate(_SAOS_PROTOCOLS):
            var = tk.BooleanVar(value=(proto in _SAOS_DEFAULT_PROTOCOLS))
            self._saos_proto_vars[proto] = var
            ttk.Checkbutton(proto_lf, text=proto, variable=var).grid(
                row=i // 5, column=i % 5, sticky=tk.W, padx=8, pady=2
            )

        # ── Row 7: SAOS 10-specific options (hidden unless SAOS 10 selected) ──
        self._saos10_frame = ttk.LabelFrame(self, text="SAOS 10 Options")

        # Row A: Update existing toggle
        row10_a = ttk.Frame(self._saos10_frame)
        row10_a.pack(fill=tk.X, padx=4, pady=(4, 2))

        self._saos10_update_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row10_a,
            text="Update existing mgmtbr0 address (removes old IP before adding new one)",
            variable=self._saos10_update_var,
        ).pack(side=tk.LEFT, padx=6)

        # Row B: Optional global source IP interface
        row10_b = ttk.Frame(self._saos10_frame)
        row10_b.pack(fill=tk.X, padx=4, pady=(0, 4))

        ttk.Label(
            row10_b,
            text="Global Source IP Interface (optional — management-plane default-source-ip):",
        ).pack(side=tk.LEFT, padx=(4, 2))
        self._saos10_src_iface_var = tk.StringVar()
        ttk.Entry(row10_b, textvariable=self._saos10_src_iface_var, width=20).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(
            row10_b,
            text="  (leave blank to skip)",
            foreground="gray",
        ).pack(side=tk.LEFT)

        # ── Row 7b: Nokia 1830 OLS-specific options (hidden unless OLS selected) ──
        self._ols_frame = ttk.LabelFrame(self, text="Nokia 1830 OLS Options")

        # Row A: Shelf type selector
        row_ols_a = ttk.Frame(self._ols_frame)
        row_ols_a.pack(fill=tk.X, padx=4, pady=(4, 2))

        ttk.Label(row_ols_a, text="Shelf Type:").pack(side=tk.LEFT, padx=(4, 2))
        self._ols_shelf_var = tk.StringVar(value=_OLS_SHELF_OPTIONS[0])
        ttk.Combobox(
            row_ols_a,
            textvariable=self._ols_shelf_var,
            values=_OLS_SHELF_OPTIONS,
            state="readonly",
            width=24,
        ).pack(side=tk.LEFT, padx=2)
        ttk.Label(
            row_ols_a,
            text="  (Auto-detect reads 'show general detail')",
            foreground="gray",
        ).pack(side=tk.LEFT)

        # Row B: Set loopback toggle
        row_ols_b = ttk.Frame(self._ols_frame)
        row_ols_b.pack(fill=tk.X, padx=4, pady=(0, 4))

        self._ols_loopback_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row_ols_b,
            text=(
                "Also set loopback (SYSTEM IP) to target IP/32  "
                "⚠ Triggers NE warm reset — use for initial commissioning only"
            ),
            variable=self._ols_loopback_var,
        ).pack(side=tk.LEFT, padx=6)

        # ── Row 8: Run controls ────────────────────────────────────────────
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill=tk.X, padx=5, pady=4)

        self._run_btn = ttk.Button(ctrl_frame, text="▶  Run Provisioning", command=self._run)
        self._run_btn.pack(side=tk.LEFT, padx=6)
        self._stop_btn = ttk.Button(
            ctrl_frame, text="■  Stop", command=self._stop, state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=6)
        self._status_lbl = ttk.Label(ctrl_frame, text="Ready", foreground="gray")
        self._status_lbl.pack(side=tk.LEFT, padx=10)

        # ── Output ─────────────────────────────────────────────────────────
        # Progress streams to ATLAS's shared output panel at the bottom of the
        # main window (the same panel the RLS Network Audit writes to), so this
        # frame stays short enough to fit without its own log box clipping.
        ttk.Label(
            self,
            text="▼  Live progress appears in the ATLAS output panel at the bottom of the window.",
            foreground="gray",
        ).pack(anchor=tk.W, padx=8, pady=(2, 4))

        # Keep the SSH Connect IP in sync with the manual single-device IP so
        # LAN provisioning targets the typed host (registered here, after the
        # Connect IP field exists). Clearing the manual IP restores the dropdown.
        self._manual_ip_var.trace_add("write", self._on_manual_ip_change)

        # Initial state
        self._refresh_ports()
        self._on_conn_type_change()
        self._on_dtype_change()

    # ── Event handlers ──────────────────────────────────────────────────────

    def _on_dtype_change(self, _event=None) -> None:
        """Show/hide Nokia vs SAOS vs SAOS10 vs OLS option panels based on device type."""
        dtype = _DEVICE_TYPE_MAP.get(self._dtype_var.get())
        ctrl = self._get_ctrl_frame()

        # Hide all device-specific panels first
        self._nokia_opt_frame.pack_forget()
        self._saos_frame.pack_forget()
        self._saos10_frame.pack_forget()
        self._ols_frame.pack_forget()

        if dtype == "saos":
            self._saos_frame.pack(fill=tk.X, padx=5, pady=4, before=ctrl)
            if self._baud_var.get() == _DEFAULT_BAUD:
                self._baud_var.set(_SAOS_DEFAULT_BAUD)
            if not self._route_var.get() or self._route_var.get() == _DEFAULT_STATIC_DEST:
                self._route_var.set(_SAOS_DEFAULT_ROUTE)
        elif dtype == "saos10":
            self._saos10_frame.pack(fill=tk.X, padx=5, pady=4, before=ctrl)
            if self._baud_var.get() == _DEFAULT_BAUD:
                self._baud_var.set(_SAOS10_DEFAULT_BAUD)
            if not self._route_var.get() or self._route_var.get() == _DEFAULT_STATIC_DEST:
                self._route_var.set(_SAOS10_DEFAULT_ROUTE)
        elif dtype == "ols":
            self._ols_frame.pack(fill=tk.X, padx=5, pady=4, before=ctrl)
            if self._baud_var.get() not in (_OLS_DEFAULT_BAUD,):
                self._baud_var.set(_OLS_DEFAULT_BAUD)
            if not self._route_var.get() or self._route_var.get() == _DEFAULT_STATIC_DEST:
                self._route_var.set(_OLS_DEFAULT_ROUTE)
        else:
            self._nokia_opt_frame.pack(fill=tk.X, padx=5, pady=4, before=ctrl)
            # Restore Nokia defaults when switching away from SAOS/OLS
            if self._baud_var.get() in (_SAOS_DEFAULT_BAUD, _SAOS10_DEFAULT_BAUD, _OLS_DEFAULT_BAUD):
                self._baud_var.set(_DEFAULT_BAUD)
            if self._route_var.get() in (_SAOS_DEFAULT_ROUTE, _SAOS10_DEFAULT_ROUTE, _OLS_DEFAULT_ROUTE):
                self._route_var.set(_DEFAULT_STATIC_DEST)

    def _get_ctrl_frame(self) -> tk.Widget:
        """Return the Run-controls frame (used as 'before' anchor for pack)."""
        return self._run_btn.master

    def _on_conn_type_change(self) -> None:
        if self._conn_var.get() == "serial":
            self._serial_frame.pack(side=tk.LEFT, padx=10)
            self._lan_frame.pack_forget()
        else:
            self._serial_frame.pack_forget()
            self._lan_frame.pack(side=tk.LEFT, padx=10)

    def _on_device_selected(self, _event=None) -> None:
        """Auto-fill Connect IP when a device is selected (LAN mode)."""
        sel = self._device_var.get()
        for d in self._devices:
            label = f"{d['hostname']}  ({d['ip']})"
            if label == sel:
                self._connect_ip_var.set(d["ip"])
                # Auto-fill gateway/prefix if present in Excel
                if "prefix" in d:
                    self._prefix_var.set(d["prefix"])
                if "gateway" in d:
                    self._gw_var.set(d["gateway"])
                if "static_route" in d:
                    self._route_var.set(d["static_route"])
                break

    def _on_manual_ip_change(self, *_args) -> None:
        """Mirror the manual single-device IP into the SSH Connect IP field so
        LAN provisioning targets it; clearing it restores the dropdown device."""
        manual_ip = self._manual_ip_var.get().strip()
        if manual_ip:
            self._connect_ip_var.set(manual_ip)
        else:
            self._on_device_selected()

    @staticmethod
    def _is_valid_ipv4(value: str) -> bool:
        parts = value.split(".")
        if len(parts) != 4:
            return False
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)

    def _resolve_target_device(self) -> Optional[Dict[str, str]]:
        """Return the device to provision.

        A typed manual IP wins over the dropdown; otherwise the device selected
        from the loaded Excel list. Returns None when neither is available. The
        returned dict always carries a 'hostname' key (possibly empty string).
        """
        manual_ip = self._manual_ip_var.get().strip()
        if manual_ip:
            return {
                "ip": manual_ip,
                "hostname": self._manual_host_var.get().strip(),
            }
        if not self._devices:
            return None
        sel = self._device_var.get()
        return next(
            (d for d in self._devices if f"{d['hostname']}  ({d['ip']})" == sel),
            None,
        )

    def _browse_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Select device list Excel file",
            filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
        )
        if path:
            self._file_var.set(path)
            self._load_excel()

    def _load_excel(self) -> None:
        path = self._file_var.get().strip()
        if not path or not os.path.isfile(path):
            messagebox.showerror("Load Error", "Please select a valid Excel file.")
            return
        try:
            self._devices = _read_excel_devices(path)
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))
            return

        labels = [f"{d['hostname']}  ({d['ip']})" for d in self._devices]
        self._device_combo["values"] = labels
        if labels:
            self._device_combo.current(0)
            self._on_device_selected()
        count = len(self._devices)
        self._file_status.config(
            text=f"Loaded {count} device{'s' if count != 1 else ''}",
            foreground="green",
        )
        self._log_append(f"Loaded {count} device(s) from {Path(path).name}\n")

    def _refresh_ports(self) -> None:
        if list_ports is None:
            ports = ["COM1"]
        else:
            try:
                ports = sorted(p.device for p in list_ports.comports()) or ["COM1"]
            except Exception:
                ports = ["COM1"]
        current = self._serial_port_var.get()
        self._serial_combo["values"] = ports
        if current in ports:
            self._serial_port_var.set(current)
        else:
            self._serial_port_var.set(ports[0])

    # ── Log helpers ──────────────────────────────────────────────────────────

    def _log_append(self, msg: str) -> None:
        """Write provisioning activity to the rolling log and shared panel."""
        activity = getattr(self.controller, "log_activity", None)
        if callable(activity):
            try:
                activity(msg)
                return
            except Exception:
                logging.debug(
                    "Provisioning controller log bridge failed", exc_info=True
                )
        logging.info(msg.rstrip())
        out = getattr(self.controller, "output_screen", None)
        root = getattr(self.controller, "root", None)
        if out is None or root is None:
            return
        text = msg if msg.endswith("\n") else msg + "\n"

        def _do():
            try:
                out.insert(tk.END, text)
                out.see(tk.END)
            except tk.TclError:
                pass

        try:
            root.after(0, _do)
        except Exception:
            pass

    def _set_status(self, msg: str, color: str = "gray") -> None:
        def _do():
            self._status_lbl.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _set_controls(self, running: bool) -> None:
        def _do():
            self._run_btn.config(state=tk.DISABLED if running else tk.NORMAL)
            self._stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ── Run / Stop ──────────────────────────────────────────────────────────

    def _stop(self) -> None:
        self._stop_flag = True
        self._set_status("Stopping…", "orange")
        self._log_append("[PROVISIONING] Stop requested by operator.")

    def _run(self) -> None:
        self._log_append("[PROVISIONING] Provisioning run requested.")
        # Resolve the target: a typed single-device IP wins over the dropdown
        device = self._resolve_target_device()
        if device is None:
            messagebox.showerror(
                "Error",
                "Enter a single device IP, or load a device list and "
                "select a device.",
            )
            return

        manual_mode = bool(self._manual_ip_var.get().strip())
        if manual_mode and not self._is_valid_ipv4(device["ip"]):
            messagebox.showerror(
                "Error", f"'{device['ip']}' is not a valid IPv4 address."
            )
            return

        # Validate network params
        gateway = self._gw_var.get().strip()
        if not gateway:
            messagebox.showerror("Error", "Gateway is required.")
            return

        # Resolve device type first so we know whether prefix_len matters
        dtype_label = self._dtype_var.get()
        device_type = _DEVICE_TYPE_MAP.get(dtype_label)  # None = auto-detect

        # Hostname is optional for SAOS / SAOS 10 / OLS, but:
        #   • auto-detect infers the device type from the hostname, and
        #   • Nokia 7705/7250 requires it (used for the saved config filename).
        if not device.get("hostname"):
            if device_type is None:
                messagebox.showerror(
                    "Error",
                    "No hostname given, so the device type can't be "
                    "auto-detected.\nEnter a hostname or choose a Device Type.",
                )
                return
            if device_type in ("7705", "7250"):
                messagebox.showerror(
                    "Error",
                    "Nokia 7705/7250 provisioning requires a hostname "
                    "(used for the saved config filename).",
                )
                return

        prefix_len = 32  # SAOS 6 always uses /32 for the management IP interface
        if device_type not in ("saos",):
            prefix_raw = self._prefix_var.get().strip()
            try:
                prefix_len = int(prefix_raw)
                if not (0 <= prefix_len <= 32):
                    raise ValueError
            except ValueError:
                messagebox.showerror("Error", "Subnet prefix must be an integer 0–32.")
                return

        conn_type = self._conn_var.get()
        if conn_type == "serial" and not self._serial_port_var.get().strip():
            messagebox.showerror("Error", "Select a serial port.")
            return

        # SSH connect IP: user override or fall back to target IP from Excel
        connect_ip = (
            self._connect_ip_var.get().strip() or device["ip"]
            if conn_type == "ssh"
            else None
        )

        self._stop_flag = False
        self._set_controls(running=True)
        self._set_status("Running…", "blue")
        disp_name = device["hostname"] or "(no hostname)"
        self._log_append(f"\n{'─'*60}")
        self._log_append(f"Provisioning: {disp_name}  {device['ip']}/{prefix_len}")

        def _worker():
            try:
                if device_type == "saos":
                    self._run_saos_worker(device, conn_type, connect_ip, gateway)
                elif device_type == "saos10":
                    self._run_saos10_worker(
                        device, conn_type, connect_ip, gateway, prefix_len
                    )
                elif device_type == "ols":
                    self._run_ols_worker(
                        device, conn_type, connect_ip, gateway, prefix_len
                    )
                else:
                    self._run_nokia_worker(
                        device, conn_type, connect_ip, gateway, prefix_len, device_type
                    )
            except Exception as exc:
                logging.exception("ProvisionFrame worker error")
                self._log_append(f"[ERROR] {exc}")
                self._set_status("Error", "red")
            finally:
                self._set_controls(running=False)

        self._run_thread = threading.Thread(target=_worker, daemon=True)
        self._run_thread.start()

    def _run_nokia_worker(
        self, device, conn_type, connect_ip, gateway, prefix_len, device_type
    ) -> None:
        from scripts.Network.Nokia_Provision import Script as ProvisionScript
        script = ProvisionScript(
            connection_type=conn_type,
            serial_port=self._serial_port_var.get().strip() if conn_type == "serial" else None,
            baud_rate=int(self._baud_var.get().strip() or _DEFAULT_BAUD),
            timeout=10,
            ip_address=connect_ip,
            username=self._user_var.get(),
            password=self._pass_var.get(),
            hostname=device["hostname"],
            target_ip=device["ip"],
            prefix_len=prefix_len,
            gateway=gateway,
            static_route_dest=self._route_var.get().strip() or _DEFAULT_STATIC_DEST,
            device_type=device_type,
            configure_card=self._cfg_card_var.get(),
            sync_redundancy=self._sync_var.get(),
            stop_callback=lambda: self._stop_flag,
            # Nokia_Provision already writes every callback message through
            # Python logging; the global GUI handler mirrors those records.
            # A second callback log would duplicate every line.
            output_callback=lambda _msg: None,
        )
        success = script.run()
        if success:
            self._set_status("Done ✔", "green")
            self._log_append(f"✔ Done: {device['hostname'] or device['ip']}")
        else:
            self._set_status("Failed ✘", "red")
            self._log_append(f"✘ Failed: {device['hostname'] or device['ip']} — see output above")

    def _run_saos_worker(self, device, conn_type, connect_ip, gateway) -> None:
        from scripts.Network.Ciena_SAOS import Script as SAOSScript
        selected_protocols = {
            name for name, var in self._saos_proto_vars.items() if var.get()
        }
        script = SAOSScript(
            connection_type=conn_type,
            serial_port=self._serial_port_var.get().strip() if conn_type == "serial" else None,
            baud_rate=int(self._baud_var.get().strip() or _SAOS_DEFAULT_BAUD),
            timeout=15,
            ip_address=connect_ip,
            username=self._user_var.get(),
            password=self._pass_var.get(),
            hostname=device["hostname"],
            target_ip=device["ip"],
            vlan_id=self._saos_vlan_var.get().strip() or _SAOS_DEFAULT_VLAN,
            iface_name=self._saos_iface_var.get().strip() or _SAOS_DEFAULT_IFACE,
            vlan_name=self._saos_vlan_name_var.get().strip() or _SAOS_DEFAULT_VLAN_NAME,
            mgmt_port=self._saos_port_var.get().strip(),
            gateway=gateway,
            static_route_dest=self._route_var.get().strip() or _SAOS_DEFAULT_ROUTE,
            protocols=selected_protocols,
            update_existing=self._saos_update_var.get(),
            stop_callback=lambda: self._stop_flag,
            output_callback=self._log_append,
        )
        success = script.run()
        if success:
            self._set_status("Done ✔", "green")
            self._log_append(f"✔ Done: {device['hostname'] or device['ip']}")
        else:
            self._set_status("Failed ✘", "red")
            self._log_append(f"✘ Failed: {device['hostname'] or device['ip']} — see output above")

    def _run_saos10_worker(
        self, device, conn_type, connect_ip, gateway, prefix_len
    ) -> None:
        from scripts.Network.Ciena_SAOS10 import Script as SAOS10Script
        script = SAOS10Script(
            connection_type=conn_type,
            serial_port=self._serial_port_var.get().strip() if conn_type == "serial" else None,
            baud_rate=int(self._baud_var.get().strip() or _SAOS10_DEFAULT_BAUD),
            timeout=15,
            ip_address=connect_ip,
            username=self._user_var.get(),
            password=self._pass_var.get(),
            hostname=device["hostname"],
            target_ip=device["ip"],
            prefix_len=prefix_len,
            gateway=gateway,
            static_route_dest=self._route_var.get().strip() or _SAOS10_DEFAULT_ROUTE,
            global_src_iface=self._saos10_src_iface_var.get().strip(),
            update_existing=self._saos10_update_var.get(),
            stop_callback=lambda: self._stop_flag,
            output_callback=self._log_append,
        )
        success = script.run()
        if success:
            self._set_status("Done ✔", "green")
            self._log_append(f"✔ Done: {device['hostname'] or device['ip']}")
        else:
            self._set_status("Failed ✘", "red")
            self._log_append(f"✘ Failed: {device['hostname'] or device['ip']} — see output above")

    def _run_ols_worker(
        self, device, conn_type, connect_ip, gateway, prefix_len
    ) -> None:
        from scripts.Network.Nokia_OLS import Script as OLSScript
        shelf_label = self._ols_shelf_var.get()
        shelf_type = _OLS_SHELF_MAP.get(shelf_label, "auto")
        script = OLSScript(
            connection_type=conn_type,
            serial_port=self._serial_port_var.get().strip() if conn_type == "serial" else None,
            baud_rate=int(self._baud_var.get().strip() or _OLS_DEFAULT_BAUD),
            timeout=15,
            ip_address=connect_ip,
            username=self._user_var.get(),
            password=self._pass_var.get(),
            hostname=device["hostname"],
            target_ip=device["ip"],
            prefix_len=prefix_len,
            gateway=gateway,
            shelf_type=shelf_type,
            set_loopback=self._ols_loopback_var.get(),
            stop_callback=lambda: self._stop_flag,
            output_callback=self._log_append,
        )
        success = script.run()
        if success:
            self._set_status("Done ✔", "green")
            self._log_append(f"✔ Done: {device['hostname'] or device['ip']}")
        else:
            self._set_status("Failed ✘", "red")
            self._log_append(f"✘ Failed: {device['hostname'] or device['ip']} — see output above")
