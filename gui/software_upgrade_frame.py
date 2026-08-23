"""
gui/software_upgrade_frame.py — "Software Upgrades" mode.

Stages a local folder of upgrade artifacts so a target device (Ciena
RLS / Waveserver 5, Nokia G42 / PSI) can pull them over HTTP (PSI is
the odd one out — it uses FTP via the on-device client). Workflow:

  1. User browses to the folder holding the software bundle.
  2. User picks a device type from the dropdown — the matching
     PC-side service IP / mask are auto-populated into the form.
  3. User clicks "Run Upgrade". The worker thread then:
       a. Applies the static IP via netsh (auto-elevates via UAC if
          ATLAS is not already running as admin).
       b. Starts an in-process ThreadingHTTPServer on :8000 rooted at
          the selected folder, unless one is already running. The
          custom handler streams files in chunks and pushes byte-
          progress to the frame's progress bar. (PSI skips this step;
          its script uses FTP.)
       c. Runs the device-specific upgrade script.
       d. In a finally block, stops the server (if it started one)
          and restores DHCP on the NIC (if it applied the static IP).

The manual buttons that used to drive steps 3a / 3b / 3d directly
are gone -- the operator only sees "Run Upgrade" and the status
labels.
"""
from __future__ import annotations

import ctypes
import logging
import os
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

try:
    from serial.tools import list_ports as _serial_list_ports
except Exception:  # pragma: no cover — pyserial-extras absent in some envs
    _serial_list_ports = None



# ── Constants ───────────────────────────────────────────────────────────────

_HTTP_PORT = 8000
_DEFAULT_MASK = "255.255.255.0"
_CHUNK_SIZE = 64 * 1024  # 64 KiB per progress tick

# Per-device-family defaults. For Ciena RLS, the shelf reaches the PC via
# the CTM internal /29 network. The PC takes .2 (CTM41) or .6 (CTM42); the
# RLS itself sits at .1 / .5 respectively.
_RLS_CTM_NET = {
    "CTM41": {"pc_ip": "10.0.0.2", "device_ip": "10.0.0.1"},
    "CTM42": {"pc_ip": "10.0.0.6", "device_ip": "10.0.0.5"},
}

# Nokia G42 connects over its link-local service interface. The PC sits at
# 169.254.0.101 and the chassis answers SSH at 169.254.0.1.
_G42_NET = {"pc_ip": "169.254.0.101", "device_ip": "169.254.0.1"}

# Nokia PSI service network — same /24, PC at .101 and PSI management at .1.
_PSI_NET = {"pc_ip": "172.16.0.101", "device_ip": "172.16.0.1"}

# Nokia PSS service network — same service /24 as the PSI (PC at .101,
# PSS management at .1). The PSS flow is identical to the PSI's except the
# upgrade script omits `config software server port 8000`.
_PSS_NET = {"pc_ip": "172.16.0.101", "device_ip": "172.16.0.1"}

# Ciena Waveserver 5 — provisioned via serial first (no factory mgmt IP),
# then SSH'd to at the address WE set. /22 subnet because that's what the
# operator's lab playbook uses; both values flow into the upgrade script.
_WS5_NET = {
    "pc_ip": "10.9.49.101",
    "device_ip": "10.9.49.36",
    "device_ip_cidr": "10.9.49.36/22",
    "mask": "255.255.252.0",  # /22
}
_WS5_HOSTNAME = "WS5_1"

# Dropdown of device families that have a wired-up upgrade flow. Add a
# new entry here (and a matching elif in `_run_upgrade`) when wiring up a
# new device type.
_SUPPORTED_UPGRADES = [
    "Ciena RLS",
    "Ciena Waveserver 5",
    "Nokia G42",
    "Nokia PSI",
    "Nokia PSS",
]

# Windows subprocess flag to suppress the brief console flash from spawning
# netsh.exe under PyInstaller-bundled apps. No-op on POSIX.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ── Admin / elevation helpers ───────────────────────────────────────────────

def _is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_netsh(args: list[str], timeout: float = 30.0) -> Tuple[bool, str]:
    """Run `netsh <args>`. If ATLAS isn't elevated, prompt UAC via
    ShellExecuteEx and wait for the elevated child to exit.
    """
    if os.name != "nt":
        return False, "netsh is only available on Windows."

    logging.info(
        "[NETSH] running: netsh %s",
        subprocess.list2cmdline(args),
    )

    if _is_admin():
        try:
            proc = subprocess.run(
                ["netsh"] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=_CREATE_NO_WINDOW,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, output.strip() or "OK"
        except subprocess.TimeoutExpired:
            return False, "netsh timed out."
        except Exception as exc:
            return False, f"netsh failed: {exc}"

    # Optional pywin32 path for UAC elevation when not already admin
    try:
        import win32event
        import win32process
        import win32con
        from win32com.shell.shell import ShellExecuteEx  # type: ignore[import-not-found]
        from win32com.shell import shellcon  # type: ignore[import-not-found]
    except ImportError:
        return False, (
            "Setting a static IP requires administrator privileges. "
            "Re-launch ATLAS as administrator, or install pywin32 to enable "
            "auto-elevation."
        )

    # Import only when needed (avoids Pylance errors)
    tmp_log_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix="atlas_netsh_",
            delete=False, encoding="utf-8",
        ) as _tmp:
            tmp_log_path = _tmp.name
    except Exception as exc:
        logging.warning(f"[NETSH] could not create capture tempfile: {exc}")

    try:
        if tmp_log_path:
            cmd_line = (
                f'/c netsh {subprocess.list2cmdline(args)} > '
                f'"{tmp_log_path}" 2>&1'
            )
            se = ShellExecuteEx(
                nShow=win32con.SW_HIDE,
                fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
                lpVerb="runas",
                lpFile="cmd.exe",
                lpParameters=cmd_line,
            )
        else:
            se = ShellExecuteEx(
                nShow=win32con.SW_HIDE,
                fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
                lpVerb="runas",
                lpFile="netsh.exe",
                lpParameters=subprocess.list2cmdline(args),
            )
        handle = se["hProcess"]
        wait_rc = win32event.WaitForSingleObject(handle, int(timeout * 1000))
        if wait_rc != 0:  # WAIT_OBJECT_0
            return False, "Elevated netsh timed out or was interrupted."
        rc = win32process.GetExitCodeProcess(handle)

        captured = ""
        if tmp_log_path:
            try:
                with open(tmp_log_path, "r", encoding="utf-8", errors="replace") as f:
                    captured = f.read().strip()
            except Exception as exc:
                captured = f"(could not read netsh output: {exc})"
            finally:
                try:
                    os.unlink(tmp_log_path)
                except Exception:
                    pass

        if rc == 0:
            return True, captured or "OK"
        msg = captured or f"netsh exit code {rc}"
        return False, msg
    except Exception as exc:
        if tmp_log_path:
            try:
                os.unlink(tmp_log_path)
            except Exception:
                pass
        return False, f"Elevation cancelled or failed: {exc}"


def _set_static_ipv4(nic: str, ip: str, mask: str) -> Tuple[bool, str]:
    """Set *nic* to a static IPv4 address, robust against stale
    bindings from a previous run.

    Why this exists: plain ``netsh interface ipv4 set address NAME
    static IP MASK`` fails with ``The object already exists.`` when
    *any* previous run already bound the same IP on this NIC, even
    after a successful ``set address … source=dhcp`` to restore
    DHCP. Windows persists the static binding in the registry and
    DHCP-restore doesn't always clear it before the next ``set``
    runs. The reliable pattern is:

      1. Best-effort ``delete address NAME addr=IP`` to drop the
         stale binding if it's there. Failure (e.g. "the specified
         entry was not found") is fine -- it just means there was
         nothing to delete.
      2. ``set address NAME static IP MASK``.

    Returns ``(success, message)`` matching :func:`_run_netsh`.
    """
    # Step 1: idempotent cleanup of any prior binding of this exact
    # address. Ignore the result -- the only failure mode that
    # matters is the subsequent ``set``.
    del_ok, del_msg = _run_netsh(
        ["interface", "ipv4", "delete", "address",
         f"name={nic}", f"addr={ip}"],
        timeout=15.0,
    )
    if not del_ok:
        # Log at debug-ish level; this is expected when the address
        # wasn't bound.
        logging.info(
            f"[NETSH] pre-clean delete of {ip} on {nic} returned: {del_msg}"
        )

    # Step 2: set the new static address as the primary binding.
    return _run_netsh(
        ["interface", "ipv4", "set", "address",
         f"name={nic}", "static", ip, mask],
    )


# NIC names the operator never wants in the dropdown for a wired
# upgrade session. Pattern-matched case-insensitively against the
# adapter friendly name from psutil:
#   * Wi-Fi / wireless adapters
#   * The MS virtual "Local Area Connection* N" hotspot adapters
#   * Bluetooth Network Connection
#   * Hyper-V / WSL vEthernet, VMware, VirtualBox virtual switches
#   * Loopback
# Substring match (lowercased).
_NIC_EXCLUDE_PATTERNS = (
    "wi-fi",
    "wifi",
    "wireless",
    "local area connection*",
    "bluetooth",
    "vethernet",
    "vmware",
    "virtualbox",
    "loopback",
)


def _list_nics() -> list[str]:
    """Return the names of usable wired Ethernet NICs for the upgrade
    dropdown.

    Filters (all must pass):
      1. Name doesn't substring-match anything in
         :data:`_NIC_EXCLUDE_PATTERNS` (wireless / Bluetooth /
         virtual switches / loopback).
      2. Interface is currently UP -- ``psutil.net_if_stats().isup``
         is the equivalent of ``ipconfig`` reporting an actual
         binding instead of ``Media disconnected``. This naturally
         hides the laptop's onboard Ethernet ports when no cable is
         plugged in (and lets ``Ethernet 4``+ remain visible the
         instant the operator plugs into them, without us having to
         maintain a manual allow/exclude list of numbered ports).
      3. Adapter has at least one IPv4 address (covers APIPA /
         static / DHCP). Pure-IPv6 doesn't work for our setup.
    """
    if not _HAS_PSUTIL:
        return []

    try:
        stats = psutil.net_if_stats()
    except Exception:
        stats = {}

    names: list[str] = []
    for name, addrs in psutil.net_if_addrs().items():
        low = name.lower()
        if any(pat in low for pat in _NIC_EXCLUDE_PATTERNS):
            continue
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        if any(a.family == socket.AF_INET for a in addrs):
            names.append(name)
    # Alphabetical -- every remaining entry is a wired Ethernet the
    # operator knows by number.
    names.sort(key=lambda n: n.lower())
    return names


# ── In-process HTTP server with progress reporting ──────────────────────────

class _ProgressHTTPHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that streams files in 64 KiB chunks and
    invokes ``self.server.on_progress(filename, sent, total)`` after each
    chunk so the GUI can drive its progress bar.
    """

    # Quieter default log_message — route through `self.server.on_log` if set
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        try:
            msg = f"{self.address_string()} - {fmt % args}"
        except Exception:
            msg = fmt
        on_log = getattr(self.server, "on_log", None)
        if callable(on_log):
            try:
                on_log(msg)
            except Exception:
                pass
        else:
            logging.getLogger("software_upgrade.http").info(msg)

    def copyfile(self, source, outputfile):  # type: ignore[override]
        """Chunked copy with progress callback. Mirrors the behaviour of
        ``shutil.copyfileobj`` but reports each chunk."""
        on_progress = getattr(self.server, "on_progress", None)
        # ``source`` is the open file; size lives on the response headers we
        # already sent. Re-derive from path for safety.
        try:
            total = os.fstat(source.fileno()).st_size
        except Exception:
            total = 0
        sent = 0
        fname = os.path.basename(self.path.split("?", 1)[0])

        if callable(on_progress):
            try:
                on_progress(fname, 0, total)
            except Exception:
                pass

        while True:
            buf = source.read(_CHUNK_SIZE)
            if not buf:
                break
            outputfile.write(buf)
            sent += len(buf)
            if callable(on_progress):
                try:
                    on_progress(fname, sent, total)
                except Exception:
                    pass

        if callable(on_progress):
            try:
                on_progress(fname, sent, total, done=True)
            except Exception:
                pass


class _UpgradeHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that pins serving to a specific root directory
    (via ``directory=`` on the handler) and exposes ``on_progress`` /
    ``on_log`` callbacks for the GUI to subscribe to.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        directory: str,
        on_progress: Optional[Callable[..., None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._directory = directory
        self.on_progress = on_progress
        self.on_log = on_log

        def _factory(*args, **kwargs):
            # SimpleHTTPRequestHandler honours `directory=` to chroot the
            # listing/serving to that path.
            return _ProgressHTTPHandler(*args, directory=directory, **kwargs)

        super().__init__(server_address, _factory)


# ── Frame ───────────────────────────────────────────────────────────────────

class SoftwareUpgradeFrame(ttk.Frame):
    """Tkinter frame for the Software Upgrades mode."""

    def __init__(self, parent: ttk.Frame, controller: Any) -> None:
        super().__init__(parent)
        self.controller = controller

        self._server: Optional[_UpgradeHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        # Track current NIC state so the worker's finally-block DHCP
        # restore knows which interface
        # we last touched, and so we can revert on app shutdown.
        self._static_ip_applied_on: Optional[str] = None

        # RLS upgrade thread + stop flag
        self._upgrade_thread: Optional[threading.Thread] = None
        self._upgrade_stop = False

        self._build()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build(self) -> None:
        # Row 1 — software folder browser
        folder_frame = ttk.LabelFrame(self, text="Software Folder")
        folder_frame.pack(fill=tk.X, padx=5, pady=4)

        self._folder_var = tk.StringVar()
        # Re-scan every device's dropdown whenever the path changes — covers a
        # typed/pasted path too, not just Browse… (which is why PSI could show
        # an empty release list). Guarded so it no-ops before the combos exist.
        self._folder_var.trace_add("write", self._on_folder_changed)
        ttk.Entry(folder_frame, textvariable=self._folder_var, width=60).pack(
            side=tk.LEFT, padx=(6, 2), pady=4
        )
        ttk.Button(folder_frame, text="Browse…", command=self._browse_folder).pack(
            side=tk.LEFT, padx=2
        )
        self._folder_status = ttk.Label(
            folder_frame, text="No folder selected", foreground="gray"
        )
        self._folder_status.pack(side=tk.LEFT, padx=8)

        # Row 2 — pick which device family to upgrade
        dev_frame = ttk.LabelFrame(self, text="Target Device")
        dev_frame.pack(fill=tk.X, padx=5, pady=4)

        ttk.Label(dev_frame, text="Device:").pack(side=tk.LEFT, padx=(6, 2))
        self._dtype_var = tk.StringVar(value=_SUPPORTED_UPGRADES[0])
        self._dtype_combo = ttk.Combobox(
            dev_frame,
            textvariable=self._dtype_var,
            values=_SUPPORTED_UPGRADES,
            state="readonly",
            width=20,
        )
        self._dtype_combo.pack(side=tk.LEFT, padx=2, pady=4)
        self._dtype_combo.bind("<<ComboboxSelected>>", self._on_dtype_change)

        # Row 3 — PC NIC static IP controls
        nic_frame = ttk.LabelFrame(self, text="PC Static IP (netsh)")
        nic_frame.pack(fill=tk.X, padx=5, pady=4)

        ttk.Label(nic_frame, text="NIC:").pack(side=tk.LEFT, padx=(6, 2))
        self._nic_var = tk.StringVar()
        self._nic_combo = ttk.Combobox(
            nic_frame, textvariable=self._nic_var, state="readonly", width=28
        )
        self._nic_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(nic_frame, text="↺", width=2, command=self._refresh_nics).pack(
            side=tk.LEFT, padx=1
        )

        ttk.Label(nic_frame, text="  PC IP:").pack(side=tk.LEFT, padx=(10, 2))
        self._pc_ip_var = tk.StringVar()
        ttk.Entry(nic_frame, textvariable=self._pc_ip_var, width=16).pack(
            side=tk.LEFT, padx=2
        )

        ttk.Label(nic_frame, text="  Mask:").pack(side=tk.LEFT, padx=(8, 2))
        self._mask_var = tk.StringVar(value=_DEFAULT_MASK)
        ttk.Entry(nic_frame, textvariable=self._mask_var, width=16).pack(
            side=tk.LEFT, padx=2
        )

        # NIC status row -- pure status, no buttons. The Run-Upgrade
        # workers now own the full lifecycle (apply static IP -> run
        # upgrade -> restore DHCP) so the operator doesn't have to
        # press the right sequence in the right order. The label
        # below shows what the worker is currently doing
        # ("Applying static IP…", "Static 169.254.0.101 on Ethernet 8",
        # "Failed — see log", etc.).
        nic_status_frame = ttk.Frame(self)
        nic_status_frame.pack(fill=tk.X, padx=5, pady=(0, 4))
        ttk.Label(
            nic_status_frame, text="NIC status:", foreground="gray",
        ).pack(side=tk.LEFT, padx=(6, 4))
        self._nic_status = ttk.Label(nic_status_frame, text="Idle", foreground="gray")
        self._nic_status.pack(side=tk.LEFT, padx=2)

        # HTTP server status row -- same pattern. The worker starts /
        # stops the server around the upgrade; the operator just sees
        # the current state.
        srv_status_frame = ttk.Frame(self)
        srv_status_frame.pack(fill=tk.X, padx=5, pady=(0, 4))
        ttk.Label(
            srv_status_frame,
            text=f"HTTP server (port {_HTTP_PORT}):",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=(6, 4))
        self._srv_status = ttk.Label(
            srv_status_frame, text="Stopped", foreground="gray",
        )
        self._srv_status.pack(side=tk.LEFT, padx=2)

        # Row 4b — Ciena RLS-specific upgrade controls (shown only when
        # device type == Ciena RLS).
        self._rls_frame = ttk.LabelFrame(self, text="Ciena RLS Upgrade")

        rls_row1 = ttk.Frame(self._rls_frame)
        rls_row1.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(rls_row1, text="Active CTM:").pack(side=tk.LEFT, padx=(4, 2))
        self._rls_ctm_var = tk.StringVar(value="CTM41")
        for label in _RLS_CTM_NET:
            tk.Radiobutton(
                rls_row1, text=label, variable=self._rls_ctm_var, value=label,
                command=self._on_rls_ctm_change,
            ).pack(side=tk.LEFT, padx=4)

        ttk.Label(rls_row1, text="  Software File:").pack(side=tk.LEFT, padx=(14, 2))
        self._rls_file_var = tk.StringVar()
        self._rls_file_combo = ttk.Combobox(
            rls_row1, textvariable=self._rls_file_var, state="readonly", width=42
        )
        self._rls_file_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(rls_row1, text="↺", width=2, command=self._refresh_rls_files).pack(
            side=tk.LEFT, padx=1
        )

        rls_row2 = ttk.Frame(self._rls_frame)
        rls_row2.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Label(rls_row2, text="SSH User:").pack(side=tk.LEFT, padx=(4, 2))
        self._rls_user_var = tk.StringVar(value="su")
        ttk.Entry(rls_row2, textvariable=self._rls_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(rls_row2, text="  Password:").pack(side=tk.LEFT, padx=(8, 2))
        self._rls_pass_var = tk.StringVar(value="admin")
        ttk.Entry(rls_row2, textvariable=self._rls_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )

        self._rls_run_btn = ttk.Button(
            rls_row2, text="▶  Run Upgrade", command=self._run_rls_upgrade
        )
        self._rls_run_btn.pack(side=tk.LEFT, padx=14)
        self._rls_stop_btn = ttk.Button(
            rls_row2, text="■  Stop", command=self._stop_rls_upgrade, state=tk.DISABLED
        )
        self._rls_stop_btn.pack(side=tk.LEFT, padx=4)
        self._rls_status = ttk.Label(rls_row2, text="", foreground="gray")
        self._rls_status.pack(side=tk.LEFT, padx=10)

        # Row 4c — Nokia G42-specific upgrade controls (shown only when
        # device type == Nokia G42).
        self._g42_frame = ttk.LabelFrame(self, text="Nokia G42 Upgrade")

        g42_row1 = ttk.Frame(self._g42_frame)
        g42_row1.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(g42_row1, text="Manifest File:").pack(side=tk.LEFT, padx=(4, 2))
        self._g42_file_var = tk.StringVar()
        self._g42_file_combo = ttk.Combobox(
            g42_row1, textvariable=self._g42_file_var, state="readonly", width=52
        )
        self._g42_file_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(g42_row1, text="↺", width=2, command=self._refresh_g42_files).pack(
            side=tk.LEFT, padx=1
        )

        g42_row2 = ttk.Frame(self._g42_frame)
        g42_row2.pack(fill=tk.X, padx=4, pady=(0, 4))
        ttk.Label(g42_row2, text="SSH User:").pack(side=tk.LEFT, padx=(4, 2))
        self._g42_user_var = tk.StringVar(value="admin")
        ttk.Entry(g42_row2, textvariable=self._g42_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(g42_row2, text="  Password:").pack(side=tk.LEFT, padx=(8, 2))
        self._g42_pass_var = tk.StringVar(value="admin")
        ttk.Entry(g42_row2, textvariable=self._g42_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )

        self._run_btn = ttk.Button(
            g42_row2, text="▶  Run Upgrade", command=self._run_g42_upgrade
        )
        self._run_btn.pack(side=tk.LEFT, padx=14)
        self._stop_btn = ttk.Button(
            g42_row2, text="■  Stop", command=self._stop_g42_upgrade, state=tk.DISABLED
        )
        self._stop_btn.pack(side=tk.LEFT, padx=4)
        self._g42_status = ttk.Label(g42_row2, text="", foreground="gray")
        self._g42_status.pack(side=tk.LEFT, padx=10)

        # Row 4d — Nokia PSI-specific upgrade controls (shown only when
        # device type == Nokia PSI). Login is 2-phase, mirroring the
        # existing 1830 flow: outer cli/admin, inner admin/admin.
        self._psi_frame = ttk.LabelFrame(self, text="Nokia PSI Upgrade")

        # Folder-structure reminder banner — the PSI fetches from /CC/,
        # so the user's selected folder needs a CC/ subdirectory.
        ttk.Label(
            self._psi_frame,
            text="Note: the PSI fetches http://172.16.0.101:8000/CC/<release>. "
                 "Select the folder that contains CC/ (the CC/ folder itself "
                 "also works). Loads must be UNZIPPED release folders inside CC/ "
                 "— ATLAS serves them automatically during the upgrade.",
            foreground="gray",
        ).pack(anchor=tk.W, padx=6, pady=(4, 0))

        psi_row1 = ttk.Frame(self._psi_frame)
        psi_row1.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(psi_row1, text="Software Release:").pack(side=tk.LEFT, padx=(4, 2))
        self._psi_file_var = tk.StringVar()
        self._psi_file_combo = ttk.Combobox(
            psi_row1, textvariable=self._psi_file_var, state="readonly", width=52
        )
        self._psi_file_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(psi_row1, text="↺", width=2, command=self._refresh_psi_files).pack(
            side=tk.LEFT, padx=1
        )

        psi_row2 = ttk.Frame(self._psi_frame)
        psi_row2.pack(fill=tk.X, padx=4, pady=(0, 2))
        ttk.Label(psi_row2, text="SSH User:").pack(side=tk.LEFT, padx=(4, 2))
        self._psi_user_var = tk.StringVar(value="cli")
        ttk.Entry(psi_row2, textvariable=self._psi_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(psi_row2, text="  SSH Pwd:").pack(side=tk.LEFT, padx=(8, 2))
        self._psi_pass_var = tk.StringVar(value="admin")
        ttk.Entry(psi_row2, textvariable=self._psi_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(psi_row2, text="  Inner User:").pack(side=tk.LEFT, padx=(8, 2))
        self._psi_inner_user_var = tk.StringVar(value="admin")
        ttk.Entry(psi_row2, textvariable=self._psi_inner_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(psi_row2, text="  Inner Pwd:").pack(side=tk.LEFT, padx=(8, 2))
        self._psi_inner_pass_var = tk.StringVar(value="admin")
        ttk.Entry(psi_row2, textvariable=self._psi_inner_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )

        psi_row3 = ttk.Frame(self._psi_frame)
        psi_row3.pack(fill=tk.X, padx=4, pady=(0, 4))
        self._psi_run_btn = ttk.Button(
            psi_row3, text="▶  Run Upgrade", command=self._run_psi_upgrade
        )
        self._psi_run_btn.pack(side=tk.LEFT, padx=4)
        self._psi_stop_btn = ttk.Button(
            psi_row3, text="■  Stop", command=self._stop_psi_upgrade, state=tk.DISABLED
        )
        self._psi_stop_btn.pack(side=tk.LEFT, padx=4)
        self._psi_status = ttk.Label(psi_row3, text="", foreground="gray")
        self._psi_status.pack(side=tk.LEFT, padx=10)

        # Row 4d-2 — Nokia PSS-specific upgrade controls (shown only when
        # device type == Nokia PSS). Identical to the PSI panel; the only
        # difference lives in the upgrade script (no `server port 8000`).
        self._pss_frame = ttk.LabelFrame(self, text="Nokia PSS Upgrade")

        ttk.Label(
            self._pss_frame,
            text="Note: the PSS fetches http://172.16.0.101:8000/CC/<release>. "
                 "Select the folder that contains CC/ (the CC/ folder itself "
                 "also works). Loads must be UNZIPPED release folders inside CC/ "
                 "— ATLAS serves them automatically during the upgrade.",
            foreground="gray",
        ).pack(anchor=tk.W, padx=6, pady=(4, 0))

        pss_row1 = ttk.Frame(self._pss_frame)
        pss_row1.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(pss_row1, text="Software Release:").pack(side=tk.LEFT, padx=(4, 2))
        self._pss_file_var = tk.StringVar()
        self._pss_file_combo = ttk.Combobox(
            pss_row1, textvariable=self._pss_file_var, state="readonly", width=52
        )
        self._pss_file_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(pss_row1, text="↺", width=2, command=self._refresh_pss_files).pack(
            side=tk.LEFT, padx=1
        )

        pss_row2 = ttk.Frame(self._pss_frame)
        pss_row2.pack(fill=tk.X, padx=4, pady=(0, 2))
        ttk.Label(pss_row2, text="SSH User:").pack(side=tk.LEFT, padx=(4, 2))
        self._pss_user_var = tk.StringVar(value="cli")
        ttk.Entry(pss_row2, textvariable=self._pss_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(pss_row2, text="  SSH Pwd:").pack(side=tk.LEFT, padx=(8, 2))
        self._pss_pass_var = tk.StringVar(value="admin")
        ttk.Entry(pss_row2, textvariable=self._pss_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(pss_row2, text="  Inner User:").pack(side=tk.LEFT, padx=(8, 2))
        self._pss_inner_user_var = tk.StringVar(value="admin")
        ttk.Entry(pss_row2, textvariable=self._pss_inner_user_var, width=10).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Label(pss_row2, text="  Inner Pwd:").pack(side=tk.LEFT, padx=(8, 2))
        self._pss_inner_pass_var = tk.StringVar(value="admin")
        ttk.Entry(pss_row2, textvariable=self._pss_inner_pass_var, show="*", width=12).pack(
            side=tk.LEFT, padx=2
        )

        pss_row3 = ttk.Frame(self._pss_frame)
        pss_row3.pack(fill=tk.X, padx=4, pady=(0, 4))
        self._pss_run_btn = ttk.Button(
            pss_row3, text="▶  Run Upgrade", command=self._run_pss_upgrade
        )
        self._pss_run_btn.pack(side=tk.LEFT, padx=4)
        self._pss_stop_btn = ttk.Button(
            pss_row3, text="■  Stop", command=self._stop_pss_upgrade, state=tk.DISABLED
        )
        self._pss_stop_btn.pack(side=tk.LEFT, padx=4)
        self._pss_status = ttk.Label(pss_row3, text="", foreground="gray")
        self._pss_status.pack(side=tk.LEFT, padx=10)

        # Row 4e — Ciena Waveserver 5 controls (shown only when
        # device type == Ciena Waveserver 5). This one is two-phase:
        # serial first (provision the device IP + hostname), then SSH
        # over the DCN-1 port to drive the software download/activate.
        self._ws5_frame = ttk.LabelFrame(self, text="Ciena Waveserver 5 Upgrade")

        ttk.Label(
            self._ws5_frame,
            text=(
                "Phase 1 (serial @ 115200 on console port) provisions "
                f"{_WS5_NET['device_ip_cidr']} and gateway "
                f"{_WS5_NET['pc_ip']}. Phase 2 (SSH over DCN-1) downloads + "
                "activates the load. Stops at 'Activation In Progress' — "
                "manual commit on the device required."
            ),
            foreground="gray", wraplength=620, justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=6, pady=(4, 0))

        ws5_row1 = ttk.Frame(self._ws5_frame)
        ws5_row1.pack(fill=tk.X, padx=4, pady=(4, 2))
        ttk.Label(ws5_row1, text="Software File:").pack(side=tk.LEFT, padx=(4, 2))
        self._ws5_file_var = tk.StringVar()
        self._ws5_file_combo = ttk.Combobox(
            ws5_row1, textvariable=self._ws5_file_var, state="readonly", width=42
        )
        self._ws5_file_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(ws5_row1, text="↺", width=2, command=self._refresh_ws5_files).pack(
            side=tk.LEFT, padx=1
        )

        ttk.Label(ws5_row1, text="  Serial Port:").pack(side=tk.LEFT, padx=(14, 2))
        self._ws5_serial_var = tk.StringVar()
        self._ws5_serial_combo = ttk.Combobox(
            ws5_row1, textvariable=self._ws5_serial_var, state="readonly", width=12
        )
        self._ws5_serial_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(ws5_row1, text="↺", width=2, command=self._refresh_ws5_serial_ports).pack(
            side=tk.LEFT, padx=1
        )

        ws5_row2 = ttk.Frame(self._ws5_frame)
        ws5_row2.pack(fill=tk.X, padx=4, pady=(0, 4))
        self._ws5_run_btn = ttk.Button(
            ws5_row2, text="▶  Run Upgrade", command=self._run_ws5_upgrade
        )
        self._ws5_run_btn.pack(side=tk.LEFT, padx=14)
        self._ws5_stop_btn = ttk.Button(
            ws5_row2, text="■  Stop", command=self._stop_ws5_upgrade, state=tk.DISABLED
        )
        self._ws5_stop_btn.pack(side=tk.LEFT, padx=4)
        self._ws5_status = ttk.Label(ws5_row2, text="", foreground="gray")
        self._ws5_status.pack(side=tk.LEFT, padx=10)

        # Row 5 — progress bar
        prog_frame = ttk.LabelFrame(self, text="Transfer Progress")
        prog_frame.pack(fill=tk.X, padx=5, pady=4)

        self._progress_var = tk.DoubleVar(value=0.0)
        self._progress = ttk.Progressbar(
            prog_frame,
            mode="determinate",
            maximum=100.0,
            variable=self._progress_var,
        )
        self._progress.pack(fill=tk.X, padx=8, pady=(8, 2))
        self._progress_lbl = ttk.Label(prog_frame, text="Idle", foreground="gray")
        self._progress_lbl.pack(anchor=tk.W, padx=8, pady=(0, 6))

        # Row 6 — log
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=4)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED
        )
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        ttk.Button(log_frame, text="Clear", command=self._clear_log).pack(
            anchor=tk.E, padx=4, pady=(0, 4)
        )

        # Initial state
        self._refresh_nics()
        self._refresh_ws5_serial_ports()
        self._on_dtype_change()
        self._on_rls_ctm_change()

    # ── Event handlers ──────────────────────────────────────────────────────

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(title="Select software folder")
        if not path:
            return
        self._folder_var.set(path)
        try:
            count = sum(1 for _ in Path(path).iterdir())
        except OSError as exc:
            self._folder_status.config(text=f"Error: {exc}", foreground="red")
            return
        self._folder_status.config(
            text=f"{count} item(s) in folder", foreground="green"
        )
        self._refresh_rls_files()
        self._refresh_g42_files()
        self._refresh_psi_files()
        self._refresh_pss_files()
        self._refresh_ws5_files()

    def _on_folder_changed(self, *_args) -> None:
        """Folder path changed (Browse… or typed) — re-scan all device
        dropdowns. Guarded because the trace can fire before the later combos
        are built during __init__."""
        if not hasattr(self, "_ws5_file_combo"):
            return
        self._refresh_rls_files()
        self._refresh_g42_files()
        self._refresh_psi_files()
        self._refresh_pss_files()
        self._refresh_ws5_files()

    def _on_dtype_change(self, _event=None) -> None:
        """Show/hide device-specific panels and pre-fill the PC static
        IP based on the selected device type. The Run-Upgrade worker
        reads ``_pc_ip_var`` directly; the entry stays visible so the
        operator can verify (or override) what the worker is about to
        bind."""
        dtype = self._dtype_var.get()
        # Hide all panels first
        self._rls_frame.pack_forget()
        self._g42_frame.pack_forget()
        self._psi_frame.pack_forget()
        self._pss_frame.pack_forget()
        self._ws5_frame.pack_forget()

        # pack_forget+pack would re-add at the end of the parent's geometry
        # list, pushing the progress + log panels around. `before=` anchors
        # device panels just above the progress frame so the layout stays
        # stable regardless of which device is selected.
        before = self._progress.master
        if dtype == "Ciena RLS":
            self._rls_frame.pack(fill=tk.X, padx=5, pady=4, before=before)
            self._mask_var.set(_DEFAULT_MASK)
            # RLS pre-fill happens in ``_on_rls_ctm_change`` based on
            # which CTM card the operator picked (CTM41 vs CTM42).
            self._on_rls_ctm_change()
        elif dtype == "Ciena Waveserver 5":
            self._ws5_frame.pack(fill=tk.X, padx=5, pady=4, before=before)
            self._pc_ip_var.set(_WS5_NET["pc_ip"])
            # WS5 lives on a /22 subnet; the others are /24.
            self._mask_var.set(_WS5_NET["mask"])
        elif dtype == "Nokia G42":
            self._g42_frame.pack(fill=tk.X, padx=5, pady=4, before=before)
            self._pc_ip_var.set(_G42_NET["pc_ip"])
            self._mask_var.set(_DEFAULT_MASK)
        elif dtype == "Nokia PSI":
            self._psi_frame.pack(fill=tk.X, padx=5, pady=4, before=before)
            self._pc_ip_var.set(_PSI_NET["pc_ip"])
            self._mask_var.set(_DEFAULT_MASK)
            self._refresh_psi_files()   # re-scan CC/ for the current folder
        elif dtype == "Nokia PSS":
            self._pss_frame.pack(fill=tk.X, padx=5, pady=4, before=before)
            self._pc_ip_var.set(_PSS_NET["pc_ip"])
            self._mask_var.set(_DEFAULT_MASK)
            self._refresh_pss_files()   # re-scan CC/ for the current folder

    def _on_rls_ctm_change(self) -> None:
        """When the CTM changes, auto-fill the PC IP to the matching internal
        address so the user doesn't have to remember CTM41=.2 / CTM42=.6."""
        net = _RLS_CTM_NET.get(self._rls_ctm_var.get())
        if net:
            self._pc_ip_var.set(net["pc_ip"])

    def _refresh_rls_files(self) -> None:
        """Populate the software-file dropdown from the selected folder.

        For RLS we filter to .tgz; if the user has the folder pointed at a
        bundle directory with no .tgz we show everything so they can pick
        manually.
        """
        self._populate_file_combo(self._rls_file_combo, self._rls_file_var, ".tgz")

    def _refresh_ws5_files(self) -> None:
        """Populate the Waveserver 5 software-file dropdown — filter to
        .tar.gz, fall back to all files if none match. The activate
        command derives the version from the filename so the operator
        needs to pick the actual tarball, not a manifest."""
        self._populate_file_combo(self._ws5_file_combo, self._ws5_file_var, ".tar.gz")

    def _refresh_ws5_serial_ports(self) -> None:
        """Populate the WS5 serial-port combobox from pyserial. Falls back
        to a single ``COM1`` placeholder when pyserial isn't usable so the
        widget never goes blank."""
        if _serial_list_ports is None:
            ports = ["COM1"]
        else:
            try:
                ports = sorted(p.device for p in _serial_list_ports.comports())
            except Exception as exc:
                logging.debug(f"Could not enumerate serial ports: {exc}")
                ports = []
        if not ports:
            ports = ["COM1"]
        current = self._ws5_serial_var.get()
        self._ws5_serial_combo["values"] = ports
        if current in ports:
            self._ws5_serial_var.set(current)
        else:
            self._ws5_serial_var.set(ports[0])

    def _refresh_g42_files(self) -> None:
        """Populate the manifest dropdown — filter to .manifest, fall back
        to all files if none match."""
        self._populate_file_combo(self._g42_file_combo, self._g42_file_var, ".manifest")

    @staticmethod
    def _resolve_cc_layout(selected: str):
        """Locate the CC/ release directory from whatever level the operator
        picked, and return ``(serve_root, cc_dir)`` as ``Path`` objects.

        The device fetches ``http://<pc>:8000/CC/<release>``, so the HTTP
        server must be rooted at CC's PARENT (``serve_root``) for that path to
        resolve. Three selections are tolerated:

          * the folder that CONTAINS CC/  (the intended pick) -> (sel, sel/CC)
          * the CC/ folder itself                             -> (sel.parent, sel)
          * a release folder inside CC/                       -> (sel.parent.parent, sel.parent)

        Returns ``(None, None)`` when no CC/ can be found."""
        if not selected:
            return None, None
        p = Path(selected)
        if not p.is_dir():
            return None, None
        if (p / "CC").is_dir():
            return p, p / "CC"
        if p.name.upper() == "CC":
            return p.parent, p
        if p.parent.name.upper() == "CC":
            return p.parent.parent, p.parent
        return None, None

    # A PSI/PSS shelf pulls individual files from an UNZIPPED release folder, so
    # only real extracted directories are valid — never a .zip archive, and
    # never a partial-download / temp artifact left behind by the browser or
    # OneDrive while a .zip was being fetched.
    _NON_RELEASE_DIR_SUFFIXES = (
        ".zip_temp", ".tmp", ".temp", ".part", ".crdownload", ".download",
    )

    @staticmethod
    def _is_release_dir(name: str) -> bool:
        """True if ``name`` looks like a real unzipped release folder (not a
        hidden dir or an in-progress download artifact)."""
        low = name.lower()
        if name[:1] in (".", "~"):
            return False
        if low.endswith(SoftwareUpgradeFrame._NON_RELEASE_DIR_SUFFIXES):
            return False
        if ".crswap" in low:
            return False
        return True

    @staticmethod
    def _release_dirs(cc_dir: "Path") -> list:
        """Sorted names of the unzipped release *directories* inside ``cc_dir``.
        Archives and partial-download artifacts are filtered out."""
        try:
            names = [p.name for p in cc_dir.iterdir() if p.is_dir()]
        except OSError:
            return []
        return sorted(n for n in names if SoftwareUpgradeFrame._is_release_dir(n))

    @staticmethod
    def _cc_zip_files(cc_dir: "Path") -> list:
        """Sorted names of .zip archives sitting (un-extracted) in ``cc_dir`` —
        used to tell the operator to unzip when no release folder is present."""
        try:
            return sorted(
                p.name for p in cc_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".zip"
            )
        except OSError:
            return []

    def _list_cc_releases(self, combo, var) -> None:
        """Shared PSI/PSS dropdown fill: list only the UNZIPPED release
        directories under the resolved CC/, tolerant of whichever folder level
        the operator selected. Clears the selection when the new list no longer
        contains it so a stale release name can't linger."""
        _serve_root, cc_path = self._resolve_cc_layout(self._folder_var.get().strip())
        if cc_path is None:
            combo["values"] = []
            var.set("")
            return
        entries = self._release_dirs(cc_path)
        combo["values"] = entries
        if entries and var.get() not in entries:
            var.set(entries[0])
        elif not entries:
            var.set("")

    def _refresh_psi_files(self) -> None:
        """Populate the PSI software dropdown from the CC/ release folders of
        the chosen folder. See ``_resolve_cc_layout`` for how the CC/ directory
        is located regardless of which level the operator selected."""
        self._list_cc_releases(self._psi_file_combo, self._psi_file_var)

    def _refresh_pss_files(self) -> None:
        """Populate the PSS software dropdown — identical CC/ layout to PSI."""
        self._list_cc_releases(self._pss_file_combo, self._pss_file_var)

    def _populate_file_combo(
        self, combo: ttk.Combobox, var: tk.StringVar, ext: str
    ) -> None:
        folder = self._folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            combo["values"] = []
            var.set("")
            return
        try:
            entries = sorted(
                p.name for p in Path(folder).iterdir() if p.is_file()
            )
        except OSError:
            entries = []
        matches = [e for e in entries if e.lower().endswith(ext)]
        files = matches or entries
        combo["values"] = files
        if files and not var.get():
            var.set(files[0])

    def _refresh_nics(self) -> None:
        nics = _list_nics()
        current = self._nic_var.get()
        self._nic_combo["values"] = nics
        if current in nics:
            self._nic_var.set(current)
        elif nics:
            self._nic_var.set(nics[0])
        else:
            self._nic_var.set("")

    def _apply_static_ip(self) -> None:
        nic = self._nic_var.get().strip()
        ip = self._pc_ip_var.get().strip()
        mask = self._mask_var.get().strip() or _DEFAULT_MASK
        if not nic:
            messagebox.showerror("Error", "Select a network interface.")
            return
        if not ip:
            messagebox.showerror("Error", "Enter the PC IP to assign.")
            return

        self._set_nic_status("Applying static IP…", "blue")
        self._log(f"netsh: setting {nic} to {ip}/{mask}")

        def _worker() -> None:
            # Pre-clean any stale binding of the same IP before
            # asking netsh to set it, otherwise a previous run's
            # leftover causes "The object already exists." even on
            # a fresh boot.
            ok, msg = _set_static_ipv4(nic, ip, mask)
            if ok:
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
            else:
                self._set_nic_status("Failed — see log", "red")
                self._log(f"netsh FAILED: {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    def _restore_dhcp(
        self, on_success: Optional[Callable[[], None]] = None
    ) -> None:
        nic = self._nic_var.get().strip() or self._static_ip_applied_on
        if not nic:
            messagebox.showerror("Error", "Select a network interface.")
            return

        self._set_nic_status("Restoring DHCP…", "blue")
        self._log(f"netsh: restoring DHCP on {nic}")

        def _worker() -> None:
            ok, msg = _run_netsh(
                ["interface", "ipv4", "set", "address",
                 f"name={nic}", "source=dhcp"]
            )
            # Also restore DNS to DHCP
            _run_netsh(
                ["interface", "ipv4", "set", "dnsservers",
                 f"name={nic}", "source=dhcp"]
            )
            if ok:
                self._static_ip_applied_on = None
                self._set_nic_status(f"DHCP on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                # Only once the NIC is safely back on DHCP do we fire the
                # caller's completion hook (the PSI activation popup).
                if on_success is not None:
                    self.after(0, on_success)
            else:
                self._set_nic_status("Failed — see log", "red")
                self._log(f"netsh FAILED: {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    def _start_server(self, root: Optional[str] = None) -> None:
        if self._server is not None:
            messagebox.showinfo("Already running", "HTTP server is already running.")
            return

        # PSI/PSS pass an explicit root (CC's parent) so /CC/<release> resolves
        # even when the operator selected the CC folder itself. Everyone else
        # defaults to the browsed folder.
        folder = (root if root is not None else self._folder_var.get()).strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Pick a valid software folder first.")
            return

        try:
            self._server = _UpgradeHTTPServer(
                ("0.0.0.0", _HTTP_PORT),
                directory=folder,
                on_progress=self._on_progress,
                on_log=self._log,
            )
        except OSError as exc:
            self._server = None
            messagebox.showerror(
                "Bind failed",
                f"Could not bind to port {_HTTP_PORT}:\n{exc}\n\n"
                "Another process may already be listening on that port.",
            )
            return

        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()

        bind_ip = self._pc_ip_var.get().strip() or "<PC IP>"
        self._set_srv_status(f"Serving {Path(folder).name} on :{_HTTP_PORT}", "green")
        self._log(f"HTTP server up at http://{bind_ip}:{_HTTP_PORT}/ (root: {folder})")

    def _stop_server(self) -> None:
        server = self._server
        if server is None:
            return
        self._set_srv_status("Stopping…", "orange")
        self._log("Stopping HTTP server…")

        def _shutdown() -> None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:
                self._log(f"HTTP shutdown error: {exc}")
            finally:
                self._server = None
                self._server_thread = None
                self._set_srv_status("Stopped", "gray")
                self._log("HTTP server stopped.")

        # server.shutdown() must run off the serving thread.
        threading.Thread(target=_shutdown, daemon=True).start()

    # ── RLS upgrade ─────────────────────────────────────────────────────────

    def _run_rls_upgrade(self) -> None:
        if self._upgrade_thread is not None and self._upgrade_thread.is_alive():
            messagebox.showinfo("Already running", "An upgrade is already in progress.")
            return

        filename = self._rls_file_var.get().strip()
        if not filename:
            messagebox.showerror(
                "Error", "Pick a software file from the dropdown."
            )
            return

        # Derive both the PC IP and the RLS management IP from the CTM
        # selection. CTM41 → PC .2 / RLS .1; CTM42 → PC .6 / RLS .5.
        ctm = self._rls_ctm_var.get()
        net = _RLS_CTM_NET.get(ctm)
        if not net:
            messagebox.showerror("Error", f"Unknown CTM selection: {ctm!r}")
            return
        pc_ip = net["pc_ip"]
        device_ip = net["device_ip"]
        # Reflect the derived PC IP in the netsh frame so the user can see
        # what the worker is about to apply.
        self._pc_ip_var.set(pc_ip)

        nic = self._nic_var.get().strip()
        if not nic:
            messagebox.showerror(
                "Error",
                "No network interface selected. Click ↺ in the PC Static IP "
                "row to refresh, then pick the NIC connected to the shelf.",
            )
            return

        mask = self._mask_var.get().strip() or _DEFAULT_MASK
        server_url = f"http://{pc_ip}:{_HTTP_PORT}/{filename}"
        user = self._rls_user_var.get().strip() or "su"
        pwd = self._rls_pass_var.get()

        self._upgrade_stop = False
        self._set_rls_status("Running…", "blue")
        self._rls_run_btn.config(state=tk.DISABLED)
        self._rls_stop_btn.config(state=tk.NORMAL)
        self._log(f"\n──── RLS upgrade ({ctm}): {device_ip} ← {server_url} ────")

        def _worker() -> None:
            # Same auto-cleanup pattern as the G42 worker -- the finally
            # block undoes the static IP + server start so the operator
            # doesn't have to remember a manual restore step (the
            # buttons that used to do that are gone).
            static_ip_owned_by_us = False
            server_started_by_us = False
            try:
                # Step 1 — set PC NIC to the CTM-internal IP. If a UAC prompt
                # appears, the user has to approve it before SSH can start.
                self._log(f"Setting {nic} to {pc_ip}/{mask} via netsh…")
                self._set_nic_status("Applying static IP…", "blue")
                ok, msg = _set_static_ipv4(nic, pc_ip, mask)
                if not ok:
                    self._set_nic_status("Failed — see log", "red")
                    self._set_rls_status("Failed ✘", "red")
                    self._log(f"netsh FAILED: {msg}")
                    self._log(
                        "Aborting upgrade — the device cannot reach the PC "
                        "until the static IP is in place."
                    )
                    return
                static_ip_owned_by_us = True
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {pc_ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                # Give Windows a beat to bring the interface up with the new
                # address before we initiate the SSH connection.
                time.sleep(2.0)

                # Step 2 — start the HTTP server (auto, no operator gate).
                if self._server is None:
                    self._log(
                        f"Starting HTTP server on {pc_ip}:{_HTTP_PORT}…"
                    )
                    self.after(0, self._start_server)
                    time.sleep(1.0)
                    server_started_by_us = True

                # Step 3 — SSH + software-install + poll.
                from scripts.Network.Ciena_RLS_Upgrade import RLSUpgradeScript

                script = RLSUpgradeScript(
                    ip_address=device_ip,
                    username=user,
                    password=pwd,
                    server_url=server_url,
                    output_callback=self._log_from_script,
                    stop_callback=lambda: self._upgrade_stop,
                )
                ok_run = script.run()
                if ok_run:
                    self._set_rls_status("Done ✔", "green")
                    self._log("✔ RLS upgrade reported complete.")
                else:
                    self._set_rls_status("Failed ✘", "red")
                    self._log("✘ RLS upgrade did not complete — see log.")
            except Exception as exc:
                logging.exception("RLS upgrade worker error")
                self._set_rls_status("Error", "red")
                self._log(f"[ERROR] {exc}")
            finally:
                if server_started_by_us:
                    self._log("Stopping HTTP server…")
                    self.after(0, self._stop_server)
                if static_ip_owned_by_us:
                    self._log("Restoring DHCP on the NIC…")
                    self.after(0, self._restore_dhcp)
                self.after(0, lambda: self._rls_run_btn.config(state=tk.NORMAL))
                self.after(0, lambda: self._rls_stop_btn.config(state=tk.DISABLED))

        self._upgrade_thread = threading.Thread(target=_worker, daemon=True)
        self._upgrade_thread.start()

    def _stop_rls_upgrade(self) -> None:
        if self._upgrade_thread is None or not self._upgrade_thread.is_alive():
            return
        self._upgrade_stop = True
        self._set_rls_status("Stopping…", "orange")
        self._log(
            "Stop requested — local polling will halt. The install on the "
            "device is not cancelled and will continue independently."
        )

    # ── Nokia G42 upgrade ───────────────────────────────────────────────────

    def _run_g42_upgrade(self) -> None:
        if self._upgrade_thread is not None and self._upgrade_thread.is_alive():
            messagebox.showinfo("Already running", "An upgrade is already in progress.")
            return

        manifest = self._g42_file_var.get().strip()
        if not manifest:
            messagebox.showerror(
                "Error", "Pick a .manifest file from the dropdown."
            )
            return

        pc_ip = _G42_NET["pc_ip"]
        device_ip = _G42_NET["device_ip"]
        # Reflect derived PC IP in the netsh frame so the user can see
        # what the worker is about to apply. (The dtype-change handler
        # already pre-fills this when G42 is selected; this assignment
        # is belt-and-braces for the case where the operator selected
        # G42 before the workspace was fully built.)
        self._pc_ip_var.set(pc_ip)

        nic = self._nic_var.get().strip()
        if not nic:
            messagebox.showerror(
                "Error",
                "No network interface selected. Click ↺ in the PC Static IP "
                "row to refresh, then pick the NIC connected to the shelf.",
            )
            return

        mask = self._mask_var.get().strip() or _DEFAULT_MASK
        server_url = f"http://{pc_ip}:{_HTTP_PORT}/{manifest}"
        user = self._g42_user_var.get().strip() or "admin"
        pwd = self._g42_pass_var.get()

        self._upgrade_stop = False
        self._set_g42_status("Running…", "blue")
        self._run_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._log(f"\n──── G42 upgrade: {device_ip} ← {server_url} ────")

        def _worker() -> None:
            # Track whether we actually applied the static IP / started
            # the server, so the finally block knows what to undo. A
            # NIC that we never touched should NOT get flipped back to
            # DHCP (the operator might already have it on something
            # they care about).
            static_ip_owned_by_us = False
            server_started_by_us = False
            try:
                # Step 1 — apply the link-local service IP. Helper
                # pre-deletes any stale binding of the same IP.
                self._log(f"Setting {nic} to {pc_ip}/{mask} via netsh…")
                self._set_nic_status("Applying static IP…", "blue")
                ok, msg = _set_static_ipv4(nic, pc_ip, mask)
                if not ok:
                    self._set_nic_status("Failed — see log", "red")
                    self._set_g42_status("Failed ✘", "red")
                    self._log(f"netsh FAILED: {msg}")
                    self._log(
                        "Aborting upgrade — the device cannot reach the PC "
                        "until the static IP is in place."
                    )
                    return
                static_ip_owned_by_us = True
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {pc_ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                time.sleep(2.0)

                # Step 2 — start the HTTP server. Skipped if the
                # operator already started one manually (legacy
                # workflow); otherwise we own it and we'll stop it
                # in the finally.
                if self._server is None:
                    self._log(
                        f"Starting HTTP server on {pc_ip}:{_HTTP_PORT}…"
                    )
                    # ``_start_server`` schedules its work via
                    # ``self.after`` and returns immediately; give the
                    # listener a beat to actually bind before the
                    # script tries to download from it.
                    self.after(0, self._start_server)
                    time.sleep(1.0)
                    server_started_by_us = True

                # Step 3 — multi-phase SSH upgrade.
                from scripts.Network.Nokia_G42_Upgrade import NokiaG42UpgradeScript

                script = NokiaG42UpgradeScript(
                    ip_address=device_ip,
                    username=user,
                    password=pwd,
                    server_url=server_url,
                    manifest_name=manifest,
                    output_callback=self._log_from_script,
                    stop_callback=lambda: self._upgrade_stop,
                )
                ok_run = script.run()
                if ok_run:
                    self._set_g42_status("Done ✔", "green")
                    self._log("✔ G42 upgrade reported complete.")
                    # Two finish-up dialogs depending on what the
                    # device reported at activate time:
                    #
                    # * standby_sync_warning=True -- the device's
                    #   redundant XMM4 wasn't synced when activate
                    #   ran, so only the primary card got the new
                    #   load. Operator MUST repeat the upgrade
                    #   against the second card before the chassis
                    #   is fully cut over.
                    # * standby_sync_warning=False (normal case) --
                    #   both controllers got the new load; device is
                    #   rebooting and the cable can come out.
                    if getattr(script, "standby_sync_warning", False):
                        self.after(0, lambda: messagebox.showinfo(
                            "G42 — Standby Controller Not Synced",
                            "Control Cards not Synchronized. Preform "
                            "Upgrade Again on Second XMM4.",
                        ))
                    else:
                        self.after(0, lambda: messagebox.showinfo(
                            "G42 — Activation in Progress",
                            "Activation in Progress. Safe to Disconnect.",
                        ))
                else:
                    self._set_g42_status("Failed ✘", "red")
                    self._log("✘ G42 upgrade did not complete — see log.")
            except Exception as exc:
                logging.exception("G42 upgrade worker error")
                self._set_g42_status("Error", "red")
                self._log(f"[ERROR] {exc}")
            finally:
                # Auto-cleanup: stop the server if we started it, and
                # restore DHCP if we applied the static IP. Both run
                # even on failure / abort so the operator doesn't have
                # to manually unwind (the previous flow asked them to
                # "click Restore DHCP when you're done", which was
                # frequently forgotten -- leaving the NIC stuck on the
                # link-local IP for the next session).
                if server_started_by_us:
                    self._log("Stopping HTTP server…")
                    self.after(0, self._stop_server)
                if static_ip_owned_by_us:
                    self._log("Restoring DHCP on the NIC…")
                    self.after(0, self._restore_dhcp)
                self.after(0, lambda: self._run_btn.config(state=tk.NORMAL))
                self.after(0, lambda: self._stop_btn.config(state=tk.DISABLED))

        self._upgrade_thread = threading.Thread(target=_worker, daemon=True)
        self._upgrade_thread.start()

    def _stop_g42_upgrade(self) -> None:
        if self._upgrade_thread is None or not self._upgrade_thread.is_alive():
            return
        self._upgrade_stop = True
        self._set_g42_status("Stopping…", "orange")
        self._log(
            "Stop requested — the upgrade on the device continues "
            "independently and may not be safely interruptible."
        )

    def _set_g42_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._g42_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ── Nokia PSI upgrade ───────────────────────────────────────────────────

    def _run_psi_upgrade(self) -> None:
        if self._upgrade_thread is not None and self._upgrade_thread.is_alive():
            messagebox.showinfo("Already running", "An upgrade is already in progress.")
            return

        # The device pulls the load over HTTP from this PC, so we serve the
        # folder that CONTAINS CC/ (serve_root) — resolved from whatever level
        # the operator selected. No CC/ means nothing to serve.
        serve_root, cc_dir = self._resolve_cc_layout(self._folder_var.get().strip())
        if serve_root is None:
            messagebox.showerror(
                "Error",
                "Could not find a CC/ directory. Select the software folder "
                "that contains CC/ (or the CC/ folder itself).",
            )
            return

        filename = self._psi_file_var.get().strip()
        if not filename:
            zips = self._cc_zip_files(cc_dir)
            if zips:
                messagebox.showerror(
                    "Unzip the load first",
                    f"CC/ contains {len(zips)} .zip archive(s) but no unzipped "
                    "release folder. PSI/PSS shelves pull the load from an "
                    "UNZIPPED folder — extract each .zip into CC/ (e.g. "
                    "CC/1830OLS-25.3-3/), then click ↺ to refresh.",
                )
            else:
                messagebox.showerror(
                    "Error",
                    "Pick a software release from the dropdown. The CC/ folder "
                    "needs an unzipped release subfolder.",
                )
            return

        pc_ip = _PSI_NET["pc_ip"]
        device_ip = _PSI_NET["device_ip"]
        self._pc_ip_var.set(pc_ip)

        nic = self._nic_var.get().strip()
        if not nic:
            messagebox.showerror(
                "Error",
                "No network interface selected. Click ↺ in the PC Static IP "
                "row to refresh, then pick the NIC connected to the shelf.",
            )
            return

        mask = self._mask_var.get().strip() or _DEFAULT_MASK
        user = self._psi_user_var.get().strip() or "cli"
        pwd = self._psi_pass_var.get()
        inner_user = self._psi_inner_user_var.get().strip() or "admin"
        inner_pwd = self._psi_inner_pass_var.get()

        self._upgrade_stop = False
        self._set_psi_status("Running…", "blue")
        self._psi_run_btn.config(state=tk.DISABLED)
        self._psi_stop_btn.config(state=tk.NORMAL)
        self._log(f"\n──── PSI upgrade: {device_ip} ← /CC/{filename} ────")

        def _worker() -> None:
            # Auto-restore DHCP and auto-stop the HTTP server in the finally so
            # the operator doesn't have to remember either manual step. The PSI
            # pulls its load over HTTP from this PC (config software server
            # protocol HTTP, port 8000), so we DO start the local server here —
            # rooted at CC's parent so /CC/<release> resolves.
            static_ip_owned_by_us = False
            server_started_by_us = False
            ok_run = False
            try:
                # Step 1 — set PC NIC to the PSI service IP.
                # Helper handles stale-binding cleanup.
                self._log(f"Setting {nic} to {pc_ip}/{mask} via netsh…")
                self._set_nic_status("Applying static IP…", "blue")
                ok, msg = _set_static_ipv4(nic, pc_ip, mask)
                if not ok:
                    self._set_nic_status("Failed — see log", "red")
                    self._set_psi_status("Failed ✘", "red")
                    self._log(f"netsh FAILED: {msg}")
                    self._log(
                        "Aborting upgrade — the device cannot reach the PC "
                        "until the static IP is in place."
                    )
                    return
                static_ip_owned_by_us = True
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {pc_ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                time.sleep(2.0)

                # Step 2 — start the HTTP server rooted at CC's parent, unless
                # the operator already started one manually. ``_start_server``
                # schedules via ``self.after`` and returns immediately, so give
                # the listener a beat to bind before the device fetches.
                if self._server is None:
                    self._log(
                        f"Starting HTTP server on {pc_ip}:{_HTTP_PORT} "
                        f"(root: {serve_root})…"
                    )
                    self.after(0, lambda: self._start_server(str(serve_root)))
                    time.sleep(1.0)
                    server_started_by_us = True

                # Step 3 — 2-phase SSH login + server config + audit/load/activate.
                from scripts.Network.Nokia_PSI_Upgrade import NokiaPSIUpgradeScript

                script = NokiaPSIUpgradeScript(
                    ip_address=device_ip,
                    username=user,
                    password=pwd,
                    inner_username=inner_user,
                    inner_password=inner_pwd,
                    software_filename=filename,
                    output_callback=self._log_from_script,
                    stop_callback=lambda: self._upgrade_stop,
                )
                ok_run = script.run()
                if ok_run:
                    self._set_psi_status("Done ✔", "green")
                    self._log("✔ PSI upgrade reported complete.")
                else:
                    self._set_psi_status("Failed ✘", "red")
                    self._log("✘ PSI upgrade did not complete — see log.")
            except Exception as exc:
                logging.exception("PSI upgrade worker error")
                self._set_psi_status("Error", "red")
                self._log(f"[ERROR] {exc}")
            finally:
                if server_started_by_us:
                    self._log("Stopping HTTP server…")
                    self.after(0, self._stop_server)
                # On success the operation is only "complete" once the NIC is
                # restored — chain the activation popup to the netsh-OK check so
                # it appears after DHCP is back. On failure, just restore DHCP.
                popup = (lambda: self._show_activation_popup(filename)) if ok_run else None
                if static_ip_owned_by_us:
                    self._log("Restoring DHCP on the NIC…")
                    self.after(0, lambda cb=popup: self._restore_dhcp(on_success=cb))
                elif popup is not None:
                    # No static IP to restore (operator pre-set it) — still
                    # signal completion.
                    self.after(0, popup)
                self.after(0, lambda: self._psi_run_btn.config(state=tk.NORMAL))
                self.after(0, lambda: self._psi_stop_btn.config(state=tk.DISABLED))

        self._upgrade_thread = threading.Thread(target=_worker, daemon=True)
        self._upgrade_thread.start()

    def _stop_psi_upgrade(self) -> None:
        if self._upgrade_thread is None or not self._upgrade_thread.is_alive():
            return
        self._upgrade_stop = True
        self._set_psi_status("Stopping…", "orange")
        self._log(
            "Stop requested — local polling will halt. The load on the "
            "device is not cancelled and will continue independently."
        )

    def _set_psi_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._psi_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _show_activation_popup(self, release: str) -> None:
        """Final completion signal for a PSI upgrade: the activate command has
        dropped the session (shelf rebooting) AND the NIC is back on DHCP.
        Must run on the main thread (scheduled via ``after``)."""
        rel = release or "the new release"
        msg = (
            f"ACTIVATION IN PROGRESS — the PSI is activating {rel} and will "
            "reboot.\n\nIMPORTANT: MANUAL COMMIT is required.\n\n"
            "Safe to disconnect."
        )
        self._log(f"ACTIVATION IN PROGRESS — activating {rel}; MANUAL COMMIT "
                  "required. Safe to disconnect.")
        try:
            messagebox.showinfo("PSI Upgrade — Activation In Progress", msg)
        except Exception:
            logging.exception("Failed to show PSI activation popup")

    # ── Nokia PSS upgrade ───────────────────────────────────────────────────

    def _run_pss_upgrade(self) -> None:
        if self._upgrade_thread is not None and self._upgrade_thread.is_alive():
            messagebox.showinfo("Already running", "An upgrade is already in progress.")
            return

        # The device pulls the load over HTTP from this PC, so we serve the
        # folder that CONTAINS CC/ (serve_root) — resolved from whatever level
        # the operator selected. No CC/ means nothing to serve.
        serve_root, cc_dir = self._resolve_cc_layout(self._folder_var.get().strip())
        if serve_root is None:
            messagebox.showerror(
                "Error",
                "Could not find a CC/ directory. Select the software folder "
                "that contains CC/ (or the CC/ folder itself).",
            )
            return

        filename = self._pss_file_var.get().strip()
        if not filename:
            zips = self._cc_zip_files(cc_dir)
            if zips:
                messagebox.showerror(
                    "Unzip the load first",
                    f"CC/ contains {len(zips)} .zip archive(s) but no unzipped "
                    "release folder. PSI/PSS shelves pull the load from an "
                    "UNZIPPED folder — extract each .zip into CC/ (e.g. "
                    "CC/1830OLS-25.3-3/), then click ↺ to refresh.",
                )
            else:
                messagebox.showerror(
                    "Error",
                    "Pick a software release from the dropdown. The CC/ folder "
                    "needs an unzipped release subfolder.",
                )
            return

        pc_ip = _PSS_NET["pc_ip"]
        device_ip = _PSS_NET["device_ip"]
        self._pc_ip_var.set(pc_ip)

        nic = self._nic_var.get().strip()
        if not nic:
            messagebox.showerror(
                "Error",
                "No network interface selected. Click ↺ in the PC Static IP "
                "row to refresh, then pick the NIC connected to the shelf.",
            )
            return

        mask = self._mask_var.get().strip() or _DEFAULT_MASK
        user = self._pss_user_var.get().strip() or "cli"
        pwd = self._pss_pass_var.get()
        inner_user = self._pss_inner_user_var.get().strip() or "admin"
        inner_pwd = self._pss_inner_pass_var.get()

        self._upgrade_stop = False
        self._set_pss_status("Running…", "blue")
        self._pss_run_btn.config(state=tk.DISABLED)
        self._pss_stop_btn.config(state=tk.NORMAL)
        self._log(f"\n──── PSS upgrade: {device_ip} ← /CC/{filename} ────")

        def _worker() -> None:
            # Auto-restore DHCP and auto-stop the HTTP server in the finally so
            # the operator doesn't have to remember either manual step. The PSS
            # pulls its load over HTTP from this PC (config software server
            # protocol HTTP; it just omits the explicit `port 8000`), so we DO
            # start the local server here — rooted at CC's parent so
            # /CC/<release> resolves.
            static_ip_owned_by_us = False
            server_started_by_us = False
            try:
                # Step 1 — set PC NIC to the PSS service IP.
                # Helper handles stale-binding cleanup.
                self._log(f"Setting {nic} to {pc_ip}/{mask} via netsh…")
                self._set_nic_status("Applying static IP…", "blue")
                ok, msg = _set_static_ipv4(nic, pc_ip, mask)
                if not ok:
                    self._set_nic_status("Failed — see log", "red")
                    self._set_pss_status("Failed ✘", "red")
                    self._log(f"netsh FAILED: {msg}")
                    self._log(
                        "Aborting upgrade — the device cannot reach the PC "
                        "until the static IP is in place."
                    )
                    return
                static_ip_owned_by_us = True
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {pc_ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                time.sleep(2.0)

                # Step 2 — start the HTTP server rooted at CC's parent, unless
                # the operator already started one manually.
                if self._server is None:
                    self._log(
                        f"Starting HTTP server on {pc_ip}:{_HTTP_PORT} "
                        f"(root: {serve_root})…"
                    )
                    self.after(0, lambda: self._start_server(str(serve_root)))
                    time.sleep(1.0)
                    server_started_by_us = True

                # Step 3 — 2-phase SSH login + server config + audit/load/activate.
                from scripts.Network.Nokia_PSS_Upgrade import NokiaPSSUpgradeScript

                script = NokiaPSSUpgradeScript(
                    ip_address=device_ip,
                    username=user,
                    password=pwd,
                    inner_username=inner_user,
                    inner_password=inner_pwd,
                    software_filename=filename,
                    output_callback=self._log_from_script,
                    stop_callback=lambda: self._upgrade_stop,
                )
                ok_run = script.run()
                if ok_run:
                    self._set_pss_status("Done ✔", "green")
                    self._log("✔ PSS upgrade reported complete.")
                else:
                    self._set_pss_status("Failed ✘", "red")
                    self._log("✘ PSS upgrade did not complete — see log.")
            except Exception as exc:
                logging.exception("PSS upgrade worker error")
                self._set_pss_status("Error", "red")
                self._log(f"[ERROR] {exc}")
            finally:
                if server_started_by_us:
                    self._log("Stopping HTTP server…")
                    self.after(0, self._stop_server)
                if static_ip_owned_by_us:
                    self._log("Restoring DHCP on the NIC…")
                    self.after(0, self._restore_dhcp)
                self.after(0, lambda: self._pss_run_btn.config(state=tk.NORMAL))
                self.after(0, lambda: self._pss_stop_btn.config(state=tk.DISABLED))

        self._upgrade_thread = threading.Thread(target=_worker, daemon=True)
        self._upgrade_thread.start()

    def _stop_pss_upgrade(self) -> None:
        if self._upgrade_thread is None or not self._upgrade_thread.is_alive():
            return
        self._upgrade_stop = True
        self._set_pss_status("Stopping…", "orange")
        self._log(
            "Stop requested — local polling will halt. The load on the "
            "device is not cancelled and will continue independently."
        )

    def _set_pss_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._pss_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ── Ciena Waveserver 5 upgrade ──────────────────────────────────────────

    def _run_ws5_upgrade(self) -> None:
        if self._upgrade_thread is not None and self._upgrade_thread.is_alive():
            messagebox.showinfo("Already running", "An upgrade is already in progress.")
            return

        # Pre-flight: WS5 is two-phase and the operator MUST have both
        # cables connected before phase 1 even starts — serial on console
        # for provisioning, Cat-5 on DCN-1 for the subsequent SSH.
        if not messagebox.askokcancel(
            "Waveserver 5 — Pre-flight",
            "Before continuing, confirm:\n\n"
            "  • Serial cable connected from this PC to the Waveserver "
            "console port (115200 baud).\n"
            "  • Cat-5 connected from this PC to the Waveserver DCN-1 "
            "port.\n\n"
            "The program will set your NIC to 10.9.49.101/22 (acting as "
            "the device's gateway) and start an HTTP server on port 8000 "
            "rooted at the selected software folder.\n\n"
            "Click OK to begin.",
        ):
            return

        filename = self._ws5_file_var.get().strip()
        if not filename:
            messagebox.showerror(
                "Error", "Pick a .tar.gz software file from the dropdown."
            )
            return

        serial_port = self._ws5_serial_var.get().strip()
        if not serial_port:
            messagebox.showerror(
                "Error",
                "Select the serial port wired to the Waveserver console. "
                "Click ↺ next to the Serial Port dropdown to refresh.",
            )
            return

        pc_ip = _WS5_NET["pc_ip"]
        device_ip = _WS5_NET["device_ip"]
        device_ip_cidr = _WS5_NET["device_ip_cidr"]
        mask = _WS5_NET["mask"]
        self._pc_ip_var.set(pc_ip)

        nic = self._nic_var.get().strip()
        if not nic:
            messagebox.showerror(
                "Error",
                "No network interface selected. Click ↺ in the PC Static IP "
                "row to refresh, then pick the NIC connected to DCN-1.",
            )
            return

        server_url = f"http://{pc_ip}:{_HTTP_PORT}/{filename}"

        self._upgrade_stop = False
        self._set_ws5_status("Running…", "blue")
        self._ws5_run_btn.config(state=tk.DISABLED)
        self._ws5_stop_btn.config(state=tk.NORMAL)
        self._log(
            f"\n──── Waveserver 5 upgrade: {device_ip_cidr} via {serial_port} "
            f"+ SSH @ {device_ip} ← {server_url} ────"
        )

        def _worker() -> None:
            # Auto-restore DHCP and auto-stop the HTTP server in the
            # finally so the operator doesn't have to remember a manual
            # step after a long two-phase upgrade.
            static_ip_owned_by_us = False
            server_started_by_us = False
            try:
                # Step 1 — point the NIC at 10.9.49.101/22 so the device
                # can reach the HTTP server once phase 1 finishes.
                # Use the helper that pre-deletes any stale binding of
                # the same IP so a previous run's leftover doesn't
                # cause ``The object already exists.``
                self._log(f"Setting {nic} to {pc_ip}/{mask} via netsh…")
                self._set_nic_status("Applying static IP…", "blue")
                ok, msg = _set_static_ipv4(nic, pc_ip, mask)
                if not ok:
                    self._set_nic_status("Failed — see log", "red")
                    self._set_ws5_status("Failed ✘", "red")
                    self._log(f"netsh FAILED: {msg}")
                    self._log(
                        "Aborting upgrade — without the static IP the "
                        "Waveserver cannot reach the HTTP server."
                    )
                    return
                static_ip_owned_by_us = True
                self._static_ip_applied_on = nic
                self._set_nic_status(f"Static {pc_ip} on {nic}", "green")
                self._log(f"netsh OK: {msg}")
                time.sleep(2.0)

                # Step 1b — start the HTTP server if it isn't already
                # running. Phase 2 of the upgrade has the device pull
                # the .tar.gz from us over the static-IP route.
                if self._server is None:
                    self._log("Starting HTTP server…")
                    self.after(0, self._start_server)
                    time.sleep(1.0)
                    server_started_by_us = True

                # Step 2 — run the two-phase upgrade script. The script
                # handles serial provisioning, the SSH download/activate
                # flow, and polls upgrade-status for us.
                from scripts.Network.Ciena_Waveserver5_Upgrade import (
                    Waveserver5UpgradeScript,
                )

                script = Waveserver5UpgradeScript(
                    serial_port=serial_port,
                    software_filename=filename,
                    server_url=server_url,
                    device_ip=device_ip,
                    device_ip_cidr=device_ip_cidr,
                    gateway_ip=pc_ip,
                    hostname=_WS5_HOSTNAME,
                    output_callback=self._log_from_script,
                    stop_callback=lambda: self._upgrade_stop,
                )
                ok_run = script.run()
                if ok_run:
                    self._set_ws5_status("Activating ✔", "green")
                    self._log("✔ Waveserver 5 reached 'Activation In Progress'.")
                    # Final popup matches the operator's verbatim text from
                    # the spec — it's what they expect at end-of-run.
                    self.after(0, lambda: messagebox.showinfo(
                        "Waveserver 5 — Complete",
                        "Software Activation in Progress. Manual Commit "
                        "Required. Safe to Disconnect.",
                    ))
                else:
                    self._set_ws5_status("Failed ✘", "red")
                    self._log("✘ Waveserver 5 upgrade did not complete — see log.")
            except Exception as exc:
                logging.exception("Waveserver 5 upgrade worker error")
                self._set_ws5_status("Error", "red")
                self._log(f"[ERROR] {exc}")
            finally:
                if server_started_by_us:
                    self._log("Stopping HTTP server…")
                    self.after(0, self._stop_server)
                if static_ip_owned_by_us:
                    self._log("Restoring DHCP on the NIC…")
                    self.after(0, self._restore_dhcp)
                self.after(0, lambda: self._ws5_run_btn.config(state=tk.NORMAL))
                self.after(0, lambda: self._ws5_stop_btn.config(state=tk.DISABLED))

        self._upgrade_thread = threading.Thread(target=_worker, daemon=True)
        self._upgrade_thread.start()

    def _stop_ws5_upgrade(self) -> None:
        if self._upgrade_thread is None or not self._upgrade_thread.is_alive():
            return
        self._upgrade_stop = True
        self._set_ws5_status("Stopping…", "orange")
        self._log(
            "Stop requested — local polling will halt. Once a software "
            "download is in flight on the device it continues independently."
        )

    def _set_ws5_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._ws5_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _set_rls_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._rls_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ── Progress + status helpers ───────────────────────────────────────────

    def _on_progress(
        self, filename: str, sent: int, total: int, done: bool = False
    ) -> None:
        pct = (sent / total * 100.0) if total else 0.0

        def _do() -> None:
            self._progress_var.set(pct)
            if done:
                self._progress_lbl.config(
                    text=f"Sent {filename} ({sent:,} bytes)", foreground="green"
                )
            else:
                self._progress_lbl.config(
                    text=f"{filename}: {sent:,}/{total:,} bytes ({pct:.1f}%)",
                    foreground="blue",
                )
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _set_nic_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._nic_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _set_srv_status(self, msg: str, color: str = "gray") -> None:
        def _do() -> None:
            self._srv_status.config(text=msg, foreground=color)
        try:
            self.after(0, _do)
        except Exception:
            pass

    def _log_local(self, msg: str) -> None:
        """Write only to this frame's timestamped log widget."""

        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"

        def _do() -> None:
            try:
                self._log_text.config(state=tk.NORMAL)
                self._log_text.insert(tk.END, line)
                self._log_text.see(tk.END)
                self._log_text.config(state=tk.DISABLED)
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            pass

    def _log_from_script(self, msg: str) -> None:
        """Render script output locally; the script itself logs it globally."""

        self._log_local(msg)

    def _log(self, msg: str) -> None:
        """Tee a single log line to every sink the operator might be
        watching:

          * the per-frame ``Log`` widget at the bottom of the upgrade
            tab (timestamped),
          * the shared bottom output panel ``controller.output_screen``
            (same panel every other ATLAS mode logs to), so the
            operator can leave it docked and still see progress here,
          * Python's root logger, which writes to the rolling ATLAS
            log file -- without this, GUI-side breadcrumbs like
            ``Setting NIC...`` only existed inside the Tk widget and
            disappeared the moment the window closed.

        Each sink is best-effort: a failure in one (Tk widget
        destroyed, controller missing the output_screen attr,
        logging handler error) must not break the others.
        """
        self._log_local(msg)

        # 2 + 3) The controller's activity bridge writes once to Python
        # logging; InventoryGUI's queue-backed handler mirrors that record
        # into controller.output_screen on Tk's thread. Tests and standalone
        # frames without the bridge still write to the root logger.
        try:
            activity = getattr(self.controller, "log_activity", None)
            if callable(activity):
                activity(msg.lstrip("\n"))
            else:
                logging.info(msg.lstrip("\n"))
        except Exception:
            logging.info(msg.lstrip("\n"))

    def _clear_log(self) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    # ── Lifecycle hook ──────────────────────────────────────────────────────

    def on_app_shutdown(self) -> None:
        """Called by the main GUI when the app is closing — make sure the
        listener is torn down so the next launch can re-bind :8000."""
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
