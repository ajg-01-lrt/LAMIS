from datetime import datetime
import importlib
import ipaddress
import os
import logging
import re
import shutil
import sqlite3
import sys
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from queue import Empty, Queue
from tkinter import filedialog
from typing import Dict, Optional, List, Any, Callable, Tuple
import time
from pathlib import Path
import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, PatternFill
import pandas as pd
import script_interface
import csv
from utils.helpers import (
    CredentialFilter,
    extract_ip_sort_key,
    friendly_error,
    get_data_dir,
    get_database_path,
    get_logs_dir,
    get_project_root,
    sanitize_filename_component,
)
import config
from utils.update import Updater
from gui.workbook_builder import WorkbookBuilder
from gui.inventory_frame import InventoryFrame
from gui.diagnostics_frame import DiagnosticsFrame
from gui.packing_slip_frame import PackingSlipFrame
from gui.file_processing_frame import FileProcessingFrame
from gui.provisioning_frame import ProvisioningFrame
from gui.software_upgrade_frame import SoftwareUpgradeFrame
from gui.ai_assistant_frame import AIAssistantFrame

command_tracker = script_interface.CommandTracker()
db_cache = script_interface.get_cache()
DATA_DIR = get_data_dir()


class _GuiQueueLogHandler(logging.Handler):
    """Move Python log records onto a queue drained by Tk's main thread."""

    def __init__(self, message_queue: Queue) -> None:
        super().__init__()
        self._message_queue = message_queue
        self._atlas_gui_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "atlas_skip_gui", False):
            return
        try:
            self._message_queue.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


class InventoryGUI:
    _manual_script_modules = {
        "Nokia SAR": "scripts.Nokia_SAR",
        "Nokia IXR": "scripts.Nokia_IXR",
        "Nokia PSS": "scripts.Nokia_1830",
        "Nokia PSI": "scripts.Nokia_PSI",
        "Ciena 6500": "scripts.Ciena_6500",
        "Ciena RLS": "scripts.Ciena_RLS",
        "Ciena Waveserver 5": "scripts.Ciena_Waveserver5",
        "Ciena SAOS": "scripts.Ciena_SAOS_Inv",
        "Ciena SAOS 10": "scripts.Ciena_SAOS10_Inv",
    }

    # F025: explicit allowlist of importable script modules. Any value not in
    # this frozen set is rejected before reaching importlib, so even if
    # ``_manual_script_modules`` were ever mutated by attacker-controlled data
    # we still cannot import an arbitrary module.
    _ALLOWED_SCRIPT_MODULES = frozenset(_manual_script_modules.values())

    _lan_connection_types = {
        "Nokia PSS": "ssh",
        # PSI shelves authenticate over SSH as 'admin' but then dead-end at the
        # alarm banner (admin != the CLI account). The working login is the
        # shelf's getty two-step over Telnet: login:cli / Username:admin /
        # Password:admin (see scripts/_nokia_1830_family.telnet_login). LAN PSI
        # therefore uses Telnet; the target IP is auto-allowlisted at build time.
        "Nokia PSI": "telnet",
        "Ciena 6500": "ssh",
        "Ciena RLS": "ssh",
        "Ciena SAOS": "ssh",
        "Ciena SAOS 10": "ssh",
    }

    _allowed_lan_scripts = {"Nokia PSS", "Nokia PSI", "Ciena 6500", "Ciena RLS", "Ciena SAOS", "Ciena SAOS 10"}
    _allowed_serial_scripts = {"Nokia SAR", "Nokia IXR", "Nokia PSI", "Ciena RLS", "Ciena Waveserver 5"}

    def __init__(self, root, update_available, command_tracker, db_cache):
        self.root = root
        self.update_available = update_available
        self.command_tracker = command_tracker
        self.db_cache = db_cache
        self.outputs = {}
        self.task_queue = Queue()
        self._activity_log_queue = Queue()
        self._activity_log_handler: Optional[_GuiQueueLogHandler] = None
        self._activity_log_after_id: Optional[str] = None
        self._last_logged_status = ""
        self.stop_threads = False
        self.is_paused = False
        self.save_location = None
        self.run_queue = Queue()
        self.run_future = None
        self.run_context = None
        self.export_future = None
        self._worker_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="atlas-worker")
        self._stdout_original = sys.stdout
        self.db_file = str(get_database_path())
        self.current_script_instance = None
        self.current_mode = tk.StringVar(value="inventory")
        self.output_screen = None
        self.file_processing_frame = None  # initialized in setup_gui
        self.raw_frame = None  # set when file_processing_frame is built; kept for back-compat
        self.failed_ips: Dict[str, str] = {}
        # Devices whose default credentials all failed are parked here so the
        # main run keeps going. After the regular task queue drains, the
        # worker prompts the user for each parked device on the main thread
        # via the run_queue ("creds_needed" event).
        self.pause_queue: List[Tuple[str, Any]] = []
        self._creds_response_queue: Queue = Queue()
        self._install_activity_log_handler()

        if os.path.isfile(self.db_file):
            if self.db_cache.db_path != self.db_file:
                logging.warning(f"[DB] Fixing db_cache path from {self.db_cache.db_path} to {self.db_file}")
                self.db_cache.db_path = self.db_file
                try:
                    self.db_cache.cache.clear()
                except Exception:
                    pass
        else:
            logging.error(f"[DB] Expected DB file not found: {self.db_file}")
        self.template_path = str(DATA_DIR / "Device_Report_Template.xlsx")
        self.psi_template_path = str(DATA_DIR / "Nokia_PSI_Report_Template.xlsx")
        self.rls_template_path = str(DATA_DIR / "Ciena_RLS_Report_Template.xlsx")
        self.packing_slip_template = str(DATA_DIR / "ATLAS_Packing_Slip.xlsx")
        self.psi_packing_slip_template = str(DATA_DIR / "Nokia_PSI_Packing_Slip.xlsx")
        self.rls_packing_slip_template = str(DATA_DIR / "Ciena_RLS_Packing_Slip.xlsx")
        self.lock = threading.Lock()
        # Per-IP device family for export-time template routing.
        # Values: "rls" (Ciena RLS), "psi" (Nokia PSI), "default" (everything else).
        self.device_family_by_ip: Dict[str, str] = {}

        # Tracks script instances that are actively executing so abort_program
        # can stop all of them immediately, regardless of concurrency.
        self._active_scripts: Dict[str, Any] = {}
        self._active_scripts_lock = threading.Lock()

        self.workbook_builder = WorkbookBuilder(self.db_cache, self.template_path, self.packing_slip_template)

        # Setup GUI Components
        self.setup_gui()

        # Initialize ScrolledText for Output at the bottom
        self.output_screen = scrolledtext.ScrolledText(self.root, height=8, width=120)
        self.output_screen.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._activity_log_after_id = self.root.after(
            50, self._drain_activity_log_queue
        )
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self.log_activity(
            "[ATLAS] Interface ready. Select a mode above to begin."
        )

    def _install_activity_log_handler(self) -> None:
        """Mirror every Python log record into the shared ATLAS output panel.

        Logging can originate on worker threads, so the handler only enqueues
        formatted text. ``_drain_activity_log_queue`` is the sole Tk writer.
        """

        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(config, "LOG_LEVEL", logging.INFO))
        for handler in tuple(root_logger.handlers):
            if getattr(handler, "_atlas_gui_handler", False):
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        handler = _GuiQueueLogHandler(self._activity_log_queue)
        handler.setFormatter(logging.Formatter(config.LOG_FORMAT))
        handler.addFilter(CredentialFilter())
        root_logger.addHandler(handler)
        self._activity_log_handler = handler

    def _drain_activity_log_queue(self) -> None:
        """Render queued log records on Tk's main thread."""

        self._activity_log_after_id = None
        while True:
            try:
                message = self._activity_log_queue.get_nowait()
            except Empty:
                break
            try:
                self.output_screen.insert(tk.END, message.rstrip() + "\n")
                self.output_screen.see(tk.END)
            except (AttributeError, tk.TclError):
                return
        try:
            self._activity_log_after_id = self.root.after(
                100, self._drain_activity_log_queue
            )
        except tk.TclError:
            self._activity_log_after_id = None

    def _on_root_destroy(self, event: tk.Event) -> None:
        """Detach the GUI logging handler when this Tk root is destroyed."""

        if event.widget is not self.root:
            return
        logging.info("[ATLAS] Interface closing.")
        if self._activity_log_after_id is not None:
            try:
                self.root.after_cancel(self._activity_log_after_id)
            except tk.TclError:
                pass
            self._activity_log_after_id = None
        handler = self._activity_log_handler
        if handler is not None:
            root_logger = logging.getLogger()
            root_logger.removeHandler(handler)
            handler.close()
            self._activity_log_handler = None

    @staticmethod
    def log_activity(message: object, level: int = logging.INFO) -> None:
        """Write one operational event to every configured ATLAS log sink."""

        text = str(message).rstrip()
        if text:
            logging.log(level, text)

    def setup_gui(self):
        app_version = getattr(config, "APP_VERSION", "")
        title = "Automatied Toolkit for Lightriver Asset & Systems (ATLAS)"
        if app_version:
            title += f"  v{app_version}"
        self.root.title(title)
        self.root.geometry('1000x750')

        # Top frame: Mode selector with a Help menubutton anchored to the right.
        mode_frame = ttk.LabelFrame(self.root, text="Select Mode")
        mode_frame.pack(fill=tk.X, padx=10, pady=5)

        # Help button (right-aligned). Built first so pack(side=tk.RIGHT)
        # lands flush to the right edge; the mode radio buttons fill in
        # from the left afterward.
        self._build_help_button(mode_frame)

        tk.Radiobutton(mode_frame, text="Inventory", variable=self.current_mode, value="inventory", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Diagnostics", variable=self.current_mode, value="tds", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Packing Slip Generator", variable=self.current_mode, value="packing_slip", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="File Processing", variable=self.current_mode, value="raw", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Provisioning", variable=self.current_mode, value="provision", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(mode_frame, text="Software Upgrades", variable=self.current_mode, value="software_upgrade", command=self.switch_mode).pack(side=tk.LEFT, padx=10)
        # Optional AI doc assistant — only surfaced when enabled in config.
        # Keep a handle so the launch-time key check can gray it out when no
        # valid API key is available.
        self.ai_assistant_radio = None
        if getattr(config, "AI_ASSISTANT_ENABLED", False):
            self.ai_assistant_radio = tk.Radiobutton(
                mode_frame, text="Doc Search", variable=self.current_mode,
                value="ai_assistant", command=self.switch_mode)
            self.ai_assistant_radio.pack(side=tk.LEFT, padx=10)

        # Container frame for all mode frames
        self.content_frame = ttk.Frame(self.root)
        self.content_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Create frames for each mode
        self.inventory_frame = InventoryFrame(self.content_frame, self)
        self.tds_frame = DiagnosticsFrame(self.content_frame, self)
        self.packing_slip_frame = PackingSlipFrame(self.content_frame, self)
        self.file_processing_frame = FileProcessingFrame(self.content_frame, self)
        self.raw_frame = self.file_processing_frame.raw_frame
        self.provision_frame = ProvisioningFrame(self.content_frame, self)
        self.software_upgrade_frame = SoftwareUpgradeFrame(self.content_frame, self)
        self.ai_assistant_frame = (
            AIAssistantFrame(self.content_frame, self)
            if getattr(config, "AI_ASSISTANT_ENABLED", False) else None
        )

        # Show the initial frame
        self.switch_mode()

        # If the boot probe (main.py.check_updates) flagged an update,
        # surface the same confirmation dialog the Help menu uses — but
        # only after Tk has rendered the main window, so the operator
        # sees ATLAS load before being interrupted.
        if self.update_available:
            self.root.after(750, self._on_check_for_updates_clicked)

        # Validate the AI assistant's API key shortly after launch (alongside
        # the update check). Runs in a background thread so startup isn't
        # blocked; disables the Doc Search tab if no valid key is provided.
        if getattr(config, "AI_ASSISTANT_ENABLED", False) and self.ai_assistant_frame is not None:
            self.root.after(1200, self._validate_api_key_async)

    # ------------------------------------------------------------------
    # Menu bar / Help
    # ------------------------------------------------------------------

    # Visual markers used on the Help menubutton when an update is pending.
    _UPDATE_AVAILABLE_CASCADE_LABEL = "● Help"
    _UPDATE_AVAILABLE_ITEM_LABEL = "● Update Available — Install Now..."
    _DEFAULT_CASCADE_LABEL = "Help"
    _DEFAULT_ITEM_LABEL = "Check for Updates..."

    def _build_help_button(self, parent) -> None:
        """Help dropdown anchored to the right of the *parent* (mode-selector
        row). Replaces the older top-of-window menubar so Help can sit on
        the right side instead of forcing flush-left placement.

        If ``self.update_available`` is True (set by main.py's boot probe),
        the button label is prefixed with a bullet and the first dropdown
        item is relabeled so the operator can see at a glance that an
        update is pending without having to open the dropdown first.
        """
        initial_label = (
            self._UPDATE_AVAILABLE_CASCADE_LABEL
            if self.update_available else self._DEFAULT_CASCADE_LABEL
        )
        self._help_button_text = tk.StringVar(value=initial_label)
        self._help_button = ttk.Menubutton(
            parent, textvariable=self._help_button_text, direction="below",
        )

        help_menu = tk.Menu(self._help_button, tearoff=0)
        self._help_menu = help_menu
        self._update_menu_index = 0

        item_label = (
            self._UPDATE_AVAILABLE_ITEM_LABEL
            if self.update_available else self._DEFAULT_ITEM_LABEL
        )
        help_menu.add_command(
            label=item_label,
            command=self._on_check_for_updates_clicked,
        )
        help_menu.add_separator()
        # Ad-hoc part-number → description lookup against the runtime
        # parts DB. Operators have parts in hand with no legible
        # markings; the DB carries the description they'd otherwise
        # have to hunt down in a separate BoM tool.
        help_menu.add_command(
            label="Part Lookup", command=self._open_part_lookup,
        )
        # Direct path to the run-log folder — needed because %APPDATA% is
        # hidden by default in File Explorer, so non-technical operators
        # can't locate the logs to attach to bug reports otherwise.
        help_menu.add_command(
            label="Open Logs Folder", command=self._open_logs_folder,
        )
        # Manual API-key entry for the AI Doc Search. Stored in the Windows
        # Credential Manager (per-user), never in the program files.
        if getattr(config, "AI_ASSISTANT_ENABLED", False):
            help_menu.add_command(
                label="Set OpenAI API Key…", command=self._prompt_set_api_key,
            )
        help_menu.add_separator()
        help_menu.add_command(label="About ATLAS", command=self._show_about)

        self._help_button["menu"] = help_menu
        # Pack to the right edge of the row; mode radio buttons fill in
        # from the left after this returns.
        self._help_button.pack(side=tk.RIGHT, padx=10)

    # ------------------------------------------------------------------
    # AI Doc Search — API key validation and entry
    # ------------------------------------------------------------------
    def _validate_api_key_async(self) -> None:
        """Check the AI assistant's API key in a background thread and update
        the UI (enable / prompt / disable) on the main thread."""
        if getattr(self, "ai_assistant_frame", None) is None:
            return

        def _worker() -> None:
            try:
                from utils.ai.provider import check_api_key
                status = check_api_key()
            except Exception:
                status = "unreachable"
            self.root.after(0, lambda: self._on_api_key_status(status))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_api_key_status(self, status: str) -> None:
        """React to the launch-time key check. 'ok'/'unreachable' leave the tab
        enabled (don't punish a transient offline state); 'no_key'/'invalid'
        prompt for a key and disable the tab if none is provided."""
        if status in ("ok", "unreachable"):
            self._set_ai_enabled(True)
            return
        reason = ("No OpenAI API key is set for Doc Search."
                  if status == "no_key"
                  else "The stored OpenAI API key was rejected (invalid or expired).")
        if not self._prompt_set_api_key(reason=reason):
            self._set_ai_enabled(False)

    def _prompt_set_api_key(self, reason: str = "") -> bool:
        """Prompt for an API key, store it in the Windows Credential Manager,
        and re-validate. Returns True if a usable key is now stored. Also used
        as the Help-menu 'Set OpenAI API Key…' command."""
        from tkinter import simpledialog
        prompt = (reason + "\n\n" if reason else "") + (
            "Paste your OpenAI API key. It is stored in the Windows Credential\n"
            "Manager for this user only — never in the ATLAS program files."
        )
        key = simpledialog.askstring("Set OpenAI API Key", prompt,
                                     show="*", parent=self.root)
        if not key:
            return False
        try:
            from utils.ai import keystore
            from utils.ai.provider import check_api_key
            keystore.store_key(key)
        except Exception as exc:
            messagebox.showerror("Could not save key", str(exc), parent=self.root)
            return False
        status = check_api_key()
        if status == "invalid":
            messagebox.showerror(
                "Invalid Key",
                "OpenAI rejected that key. Doc Search stays disabled until "
                "a valid key is entered.", parent=self.root)
            self._set_ai_enabled(False)
            return False
        if status == "unreachable":
            messagebox.showwarning(
                "Key saved (unverified)",
                "Key saved, but it couldn't be verified right now (offline or API "
                "unreachable). It will be used when you ask a question.",
                parent=self.root)
        else:
            messagebox.showinfo(
                "API Key Saved",
                "Key saved and verified. Doc Search is enabled.",
                parent=self.root)
        self._set_ai_enabled(True)
        return True

    def _set_ai_enabled(self, enabled: bool) -> None:
        """Enable or gray out the Doc Search tab (radio button + frame). When
        disabling while it's the active tab, bounce back to Inventory."""
        radio = getattr(self, "ai_assistant_radio", None)
        if radio is not None:
            try:
                radio.config(state=(tk.NORMAL if enabled else tk.DISABLED))
            except tk.TclError:
                pass
        frame = getattr(self, "ai_assistant_frame", None)
        if frame is not None and hasattr(frame, "set_enabled"):
            frame.set_enabled(enabled)
        if not enabled and self.current_mode.get() == "ai_assistant":
            self.current_mode.set("inventory")
            self.switch_mode()

    def _set_update_indicator(self, available: bool) -> None:
        """Toggle the bullet markers on the Help button label and dropdown
        first item to match whether an update is pending. Safe to call
        from the main thread at any time after ``_build_help_button``."""
        try:
            self._help_button_text.set(
                self._UPDATE_AVAILABLE_CASCADE_LABEL if available
                else self._DEFAULT_CASCADE_LABEL
            )
            item_label = (
                self._UPDATE_AVAILABLE_ITEM_LABEL
                if available else self._DEFAULT_ITEM_LABEL
            )
            self._help_menu.entryconfig(self._update_menu_index, label=item_label)
        except (tk.TclError, AttributeError):
            logging.debug("Could not toggle update indicator", exc_info=True)

    def _set_update_menu_state(self, enabled: bool) -> None:
        try:
            state = tk.NORMAL if enabled else tk.DISABLED
            self._help_menu.entryconfig(self._update_menu_index, state=state)
        except (tk.TclError, AttributeError):
            pass

    def _on_check_for_updates_clicked(self) -> None:
        """Run an out-of-band update check from the Help menu.

        Dispatches the network probe to the worker pool so a slow GitHub
        response doesn't freeze Tk. The result is marshalled back to the
        main thread via ``root.after`` before any dialog is shown.
        """
        self._set_update_menu_state(False)
        self.log_activity("[UPDATE] Checking for updates.")

        def _probe() -> Tuple[Optional[Updater], bool, Optional[str]]:
            try:
                if getattr(sys, "frozen", False):
                    updater = Updater()
                else:
                    updater = Updater(get_project_root())
            except Exception as exc:
                return None, False, friendly_error(exc)
            try:
                available = updater.check_for_updates()
                return updater, available, None
            except Exception as exc:
                return updater, False, friendly_error(exc)

        def _on_result(fut) -> None:
            updater, available, err = fut.result()
            self._set_update_menu_state(True)
            if err:
                self.log_activity(
                    f"[UPDATE] Update check failed: {err}", logging.ERROR
                )
                messagebox.showerror("Update Check Failed", err)
                return
            if not available:
                # Manual re-check found nothing newer — drop the badge if it
                # was up from a stale boot-time probe.
                self.update_available = False
                self._set_update_indicator(False)
                self.log_activity(
                    "[UPDATE] ATLAS is already on the latest version."
                )
                messagebox.showinfo(
                    "No Updates",
                    f"ATLAS v{getattr(config, 'APP_VERSION', '?')} is up to date.",
                )
                return
            # Updates available — surface the badge if the boot probe missed
            # it (e.g., release was published after launch) and run the prompt.
            self.update_available = True
            self._set_update_indicator(True)
            self._apply_update_with_prompt(updater)

        self._worker_pool.submit(_probe).add_done_callback(
            lambda fut: self.root.after(0, _on_result, fut)
        )

    def _apply_update_with_prompt(self, updater: Updater) -> None:
        """Show the maintainer-supplied confirmation message in a Tk dialog,
        then download + launch the installer on the worker pool."""
        # Hook the Updater's confirmation callback into a Tk yes/no dialog.
        def _ask(message: str) -> bool:
            return messagebox.askyesno("Update Available", message)

        updater.set_confirmation_callback(_ask)
        self._set_update_menu_state(False)
        self.log_activity("[UPDATE] Preparing update.")

        def _apply() -> Tuple[bool, str, Updater]:
            ok, msg = updater.apply_update()
            return ok, msg, updater

        def _on_apply(fut) -> None:
            ok, msg, up = fut.result()
            self._set_update_menu_state(True)
            self.log_activity(
                f"[UPDATE] {msg}",
                logging.INFO if ok else logging.WARNING,
            )
            if ok and up.installed_mode:
                # In installed mode a hidden PowerShell watcher is polling
                # for this process to exit; once it sees ATLAS gone it'll
                # launch the NSIS installer. Show one final acknowledgement
                # and then call restart_program (os._exit) so the watcher
                # can take over. Clicking OK is what kicks the sequence:
                # OK -> ATLAS closes -> watcher detects -> installer UI
                # appears. The installer was previously launching BEFORE
                # this messagebox was dismissed, which sometimes layered
                # the NSIS window over a still-visible ATLAS.
                messagebox.showinfo(
                    "Update Ready",
                    "Click OK to close ATLAS. The installer will start "
                    "automatically once ATLAS has fully exited, and the "
                    "new version will re-launch when install completes.",
                )
                up.restart_program()
            elif ok:
                messagebox.showinfo("Update Applied", msg)
            else:
                messagebox.showwarning("Update Not Applied", msg)

        self._worker_pool.submit(_apply).add_done_callback(
            lambda fut: self.root.after(0, _on_apply, fut)
        )

    def _open_part_lookup(self) -> None:
        """Open the Part Lookup dialog against the runtime parts DB.

        Non-blocking — the dialog runs independently so the operator
        can keep working in the main window while it's open.
        """
        self.log_activity("[PART LOOKUP] Open requested.")
        try:
            db_path = str(get_database_path())
        except Exception as exc:
            self.log_activity(
                f"[PART LOOKUP] Database path unavailable: "
                f"{friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Part Lookup",
                f"Could not resolve the parts database.\n\n"
                f"{friendly_error(exc)}",
            )
            return
        try:
            from gui.part_lookup_dialog import PartLookupDialog
            PartLookupDialog(self.root, db_path)
            self.log_activity(
                f"[PART LOOKUP] Opened against database {db_path}."
            )
        except Exception as exc:
            self.log_activity(
                f"[PART LOOKUP] Could not open: {friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Part Lookup",
                f"Could not open the lookup dialog.\n\n{friendly_error(exc)}",
            )

    def _open_logs_folder(self) -> None:
        """Open the ATLAS run-logs directory in the system file browser.

        %APPDATA% is hidden by default in File Explorer, so operators
        couldn't reliably find the logs to send with bug reports —
        this gives them a one-click path.
        """
        self.log_activity("[LOGGING] Open logs folder requested.")
        try:
            log_dir = get_logs_dir()
        except Exception as exc:
            self.log_activity(
                f"[LOGGING] Could not resolve logs directory: "
                f"{friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Open Logs Folder",
                f"Could not resolve the ATLAS logs directory.\n\n{friendly_error(exc)}",
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(log_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(log_dir)])
            else:
                subprocess.Popen(["xdg-open", str(log_dir)])
            self.log_activity(f"[LOGGING] Opened logs folder {log_dir}.")
        except Exception as exc:
            self.log_activity(
                f"[LOGGING] Could not open logs folder {log_dir}: "
                f"{friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Open Logs Folder",
                f"Could not open the logs folder.\n\n"
                f"Path: {log_dir}\n\n{friendly_error(exc)}",
            )

    def _show_about(self) -> None:
        self.log_activity("[UI] About dialog opened.")
        version = getattr(config, "APP_VERSION", "(unknown)")
        owner = getattr(config, "GITHUB_OWNER", "")
        repo  = getattr(config, "GITHUB_REPO", "")
        repo_str = f"{owner}/{repo}" if owner and repo else "(unconfigured)"
        try:
            log_dir_str = str(get_logs_dir())
        except Exception:
            log_dir_str = "(unavailable)"
        manifesto = (
            "Let me ask you a question.\n\n"
            "Is an engineer not entitled to the hours of their own day?\n\n"
            "\"No,\" says the spreadsheet, \"your hours belong to rows and columns.\"\n\n"
            "\"No,\" says the terminal, \"your patience belongs to prompts and passwords.\"\n\n"
            "\"No,\" says the old way, \"your attention belongs to every serial "
            "number buried in the noise.\"\n\n"
            "I rejected those answers.\n\n"
            "I chose something better.\n\n"
            "I chose to build the future.\n\n"
            "I chose ATLAS.\n\n"
            "A tool where the engineer is not chained to command repetition.\n\n"
            "Where the technician is not buried beneath mismatched reports.\n\n"
            "Where discovery, documentation, and deployment are no longer "
            "scattered across windows, notes, and weary hands.\n\n"
            "A system that reaches into the network, finds what is alive, "
            "knows what it is, gathers what it carries, and turns the noise "
            "of the factory into records that can be trusted.\n\n"
            "I am not here to ask whether the work must be done.\n\n"
            "I am here to ask why it must still be done by hand.\n\n"
            "No more endless CLI sessions.\n\n"
            "No more copy, paste, format, repeat.\n\n"
            "No more losing the day to work that machines were born to carry.\n\n"
            "With the click of a button, the tedious becomes automatic.\n\n"
            "The scattered becomes structured.\n\n"
            "The manual becomes memory.\n\n"
            "And with the sweat once spent on repetition, we build something greater.\n\n"
            "A faster factory.\n\n"
            "A cleaner record.\n\n"
            "A network understood.\n\n"
            "This is ATLAS.\n\n"
            "Not merely a program.\n\n"
            "A refusal to waste human effort on work beneath human attention."
        )
        footer = (
            f"\n\n— — —\n"
            f"Automated Toolkit for Lightriver Asset & Systems\n"
            f"Version: {version}    Update channel: {repo_str}\n"
            f"Logs folder: {log_dir_str}\n"
            f"© Chees3loaf/LightRiver Technologies"
        )
        self._show_about_dialog(manifesto + footer)

    def _show_about_dialog(self, body: str) -> None:
        """Render the About dialog in a scrollable Toplevel.

        ``messagebox.showinfo`` doesn't scale gracefully to a multi-paragraph
        manifesto — on shorter screens the OK button can fall off the bottom
        and the text isn't selectable. A small custom Toplevel with a
        scrolled Text widget keeps the whole thing readable on every display.
        """
        try:
            dialog = tk.Toplevel(self.root)
            dialog.title("About ATLAS")
            dialog.transient(self.root)
            dialog.grab_set()
            dialog.resizable(True, True)
            dialog.geometry("560x600")

            text = scrolledtext.ScrolledText(
                dialog, wrap=tk.WORD, padx=14, pady=12,
                font=("Segoe UI", 10),
            )
            text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
            text.insert("1.0", body)
            text.config(state=tk.DISABLED)

            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(fill=tk.X, padx=8, pady=8)
            tk.Button(btn_frame, text="Close", command=dialog.destroy, width=12)\
                .pack(side=tk.RIGHT)

            dialog.bind("<Escape>", lambda _e: dialog.destroy())
            dialog.wait_window()
        except tk.TclError:
            # Fall back to a plain messagebox if the Toplevel couldn't render
            # (e.g., headless / unusual Tk build).
            messagebox.showinfo("About ATLAS", body)

    def switch_mode(self):
        """Hide all frames and show the selected one."""
        mode = self.current_mode.get()
        
        if self.inventory_frame:
            self.inventory_frame.pack_forget()
        if self.tds_frame:
            self.tds_frame.pack_forget()
        if self.packing_slip_frame:
            self.packing_slip_frame.pack_forget()
        if self.file_processing_frame:
            self.file_processing_frame.pack_forget()
        if self.provision_frame:
            self.provision_frame.pack_forget()
        if getattr(self, "software_upgrade_frame", None):
            self.software_upgrade_frame.pack_forget()
        if getattr(self, "ai_assistant_frame", None):
            self.ai_assistant_frame.pack_forget()

        if mode == "inventory":
            self.inventory_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "tds":
            self.tds_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "packing_slip":
            self.packing_slip_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "raw":
            self.file_processing_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "provision":
            self.provision_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "software_upgrade":
            self.software_upgrade_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "ai_assistant" and getattr(self, "ai_assistant_frame", None):
            self.ai_assistant_frame.pack(fill=tk.BOTH, expand=True)
        labels = {
            "inventory": "Inventory",
            "tds": "Diagnostics",
            "packing_slip": "Packing Slip Generator",
            "raw": "File Processing",
            "provision": "Provisioning",
            "software_upgrade": "Software Upgrades",
            "ai_assistant": "Doc Search",
        }
        self.log_activity(f"[UI] Mode selected: {labels.get(mode, mode)}")

    def update_status(self, message: str) -> None:
        self.inventory_frame.update_status(message)
        if message and message != self._last_logged_status:
            self._last_logged_status = message
            self.log_activity(f"[STATUS] {message}")

    def set_run_controls(self, running: bool) -> None:
        self.inventory_frame.set_run_controls(running)

    # Delegate workbook operations to the dedicated WorkbookBuilder.
    def autosize_sheet_columns(self, sheet: Any, min_width: int = 10, max_width: int = 60) -> None:
        self.workbook_builder.autosize_sheet_columns(sheet, min_width, max_width)

    def combine_and_format_data(self, ip_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        return self.workbook_builder.combine_and_format_data(ip_data)

    def build_report_workbook(self, outputs: Dict[str, Any], output_file: str, customer: str = "", project: str = "", customer_po: str = "", sales_order: str = "", append_mode: bool = False) -> Dict[str, Any]:
        return self.workbook_builder.build_report_workbook(outputs, output_file, customer=customer, project=project, customer_po=customer_po, sales_order=sales_order, append_mode=append_mode)

    def build_psi_report_workbook(self, outputs: Dict[str, Any], output_file: str, customer: str = "", project: str = "", customer_po: str = "", sales_order: str = "", append_mode: bool = False, template_override: Optional[str] = None) -> Dict[str, Any]:
        return self.workbook_builder.build_psi_report_workbook(
            outputs,
            output_file,
            customer=customer,
            project=project,
            customer_po=customer_po,
            sales_order=sales_order,
            append_mode=append_mode,
            psi_template_path=template_override or self.psi_template_path,
        )

    def build_unified_report_workbook(self, family_buckets: Dict[str, Dict[str, Any]], output_file: str, customer: str = "", project: str = "", customer_po: str = "", sales_order: str = "", append_mode: bool = False) -> Dict[str, Any]:
        return self.workbook_builder.build_unified_report_workbook(
            family_buckets,
            output_file,
            customer=customer,
            project=project,
            customer_po=customer_po,
            sales_order=sales_order,
            append_mode=append_mode,
            rls_template_path=self.rls_template_path,
            psi_template_path=self.psi_template_path,
        )

    @staticmethod
    def _family_for_script(script_instance: Any) -> str:
        """Return the export-template family for a script instance.

        Used to route per-IP outputs to the correct workbook template at
        export time. Module name is the most reliable signal because it
        is fixed at import time and does not depend on user-visible labels.
        """
        try:
            mod = type(script_instance).__module__
        except Exception:
            return "default"
        if mod.endswith("Ciena_RLS"):
            return "rls"
        if mod.endswith("Nokia_PSI"):
            return "psi"
        return "default"

    def copy_sheet(self, source_sheet: Any, target_wb: Any, new_sheet_name: str) -> Any:
        return self.workbook_builder.copy_sheet(source_sheet, target_wb, new_sheet_name)

    def build_packing_slip_workbook(self, processed_data: Dict[str, Any], ip_list: List[str], customer: str, project: str, customer_po: str, sales_order: str, save_folder: str, display_ip_for_key: Optional[Dict[str, str]] = None) -> str:
        return self.workbook_builder.build_packing_slip_workbook(processed_data, ip_list, customer, project, customer_po, sales_order, save_folder, display_ip_for_key=display_ip_for_key)

    def build_unified_packing_slip_workbook(self, processed_data: Dict[str, Any], ip_list: List[str], customer: str, project: str, customer_po: str, sales_order: str, save_folder: str, family_for_ip: Optional[Dict[str, str]] = None, display_ip_for_key: Optional[Dict[str, str]] = None) -> str:
        """Per-device-family packing slip workbook (RLS / PSI / default templates merged)."""
        return self.workbook_builder.build_unified_packing_slip_workbook(
            processed_data, ip_list, customer, project, customer_po, sales_order, save_folder,
            family_for_ip=family_for_ip,
            rls_packing_slip_template=self.rls_packing_slip_template,
            psi_packing_slip_template=self.psi_packing_slip_template,
            display_ip_for_key=display_ip_for_key,
        )

    def get_user_inputs(self, default_filename, prefill: Optional[Dict[str, str]] = None,
                        append_mode: bool = False):
        """Prompt user for project information in a popup.

        Args:
            default_filename: Default value for the Filename field.
            prefill: Optional dict with keys "customer", "project", "po", "so".
                When provided (e.g. from an uploaded inventory report), the
                corresponding fields are pre-populated so the user only has
                to confirm rather than retype.
            append_mode: When True, omit the Filename field (the existing
                workbook's path is reused) and label the popup as an
                append-mode dialog so the user can see at a glance that
                edits here only affect the NEW device sheet.
        """
        prefill = prefill or {}
        root = tk.Toplevel()  # Create a new popup window
        root.title("Append to Existing Report" if append_mode else "User Inputs")

        if append_mode:
            tk.Label(
                root,
                text=(
                    "Pre-filled from the uploaded report. Changes here apply\n"
                    "only to the new device sheet; existing sheets are not modified."
                ),
                justify="left",
                fg="#444",
            ).grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 4), sticky="w")
            field_row_start = 1
        else:
            field_row_start = 0

        user_inputs = {
            "Customer": tk.StringVar(value=prefill.get("customer", "")),
            "Project": tk.StringVar(value=prefill.get("project", "")),
            "Purchase Order": tk.StringVar(value=prefill.get("po", "")),
            "Sales Order": tk.StringVar(value=prefill.get("so", "")),
        }
        if not append_mode:
            user_inputs["Filename"] = tk.StringVar(value=default_filename)

        # Create input fields
        row = field_row_start
        for label, var in user_inputs.items():
            tk.Label(root, text=label + ":").grid(row=row, column=0, padx=10, pady=5, sticky="w")
            tk.Entry(root, textvariable=var, width=40).grid(row=row, column=1, padx=10, pady=5)
            row += 1

        # Handle window closing event to prevent errors
        def on_close():
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

        # Button to submit inputs
        def submit():
            root.destroy()  # Close the popup safely

        tk.Button(root, text="Submit", command=submit).grid(row=row, column=0, columnspan=2, pady=10)

        root.grab_set()  # Make the popup modal
        root.wait_window(root)  # Ensure the window waits before proceeding

        # Extract values
        return {key: var.get().strip() for key, var in user_inputs.items()}

    def collect_run_context(self) -> Optional[Dict[str, Any]]:
        default_filename = ""
        connection_mode = self.inventory_frame.connection_type.get()
        append_mode = bool(
            self.inventory_frame.inventory_report_path
            and os.path.isfile(self.inventory_frame.inventory_report_path)
        )

        user_inputs = self.get_user_inputs(
            default_filename,
            prefill=getattr(self.inventory_frame, "uploaded_metadata", {}) or {},
            append_mode=append_mode,
        )
        if not user_inputs:
            messagebox.showerror("Input Error", "User input window was closed without entering details.")
            return None

        required_fields = ["Customer", "Project", "Purchase Order", "Sales Order"]
        if not append_mode:
            required_fields.append("Filename")

        for key in required_fields:
            value = user_inputs.get(key, "")
            if not value.strip():
                messagebox.showerror("Input Error", f"{key} is required.")
                return None

        customer = user_inputs["Customer"]
        project = user_inputs["Project"]
        customer_po = user_inputs["Purchase Order"]
        sales_order = user_inputs["Sales Order"]
        if append_mode:
            output_file = os.path.normpath(self.inventory_frame.inventory_report_path)
        else:
            filename = user_inputs["Filename"]
            # Sanitize filename: only alphanumeric, underscore, and hyphen (no spaces for better compatibility)
            filename = "".join(c for c in filename if c.isalnum() or c in ("_", "-")).strip()
            if not filename:
                messagebox.showerror("Input Error", "Invalid filename entered.")
                return None

            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M')
            # F018: sanitize customer/project before composing the on-disk filename;
            # they originate from imported XLSX values and could contain `..\` or other
            # path-traversal sequences.
            safe_customer = sanitize_filename_component(customer, fallback="Customer")
            safe_project = sanitize_filename_component(project, fallback="Project")
            final_filename = f"{filename}_{safe_customer}_{safe_project}_Inventory_{timestamp}"

            save_folder = filedialog.askdirectory(title="Select Save Location")
            if not save_folder:
                messagebox.showinfo("Export Cancelled", "No save folder selected.")
                return None

            # Save folder is the source of truth for where the report is written.
            self.save_location = os.path.realpath(os.path.normpath(save_folder))

            output_file = os.path.normpath(os.path.join(self.save_location, f"{final_filename}.xlsx"))
            counter = 1
            while os.path.exists(output_file):
                output_file = os.path.join(self.save_location, f"{final_filename}_{counter}.xlsx")
                counter += 1

        if connection_mode in ("LAN", "Serial"):
            manual_script = self.inventory_frame.manual_script_var.get().strip()
            if not manual_script or manual_script not in self._manual_script_modules:
                messagebox.showerror("Input Error", "Please select a valid script.")
                return None

            if connection_mode == "LAN" and manual_script not in self._allowed_lan_scripts:
                messagebox.showerror("Input Error", "LAN supports only Nokia PSS, Nokia PSI, Ciena 6500, and Ciena RLS.")
                return None
            if connection_mode == "Serial" and manual_script not in self._allowed_serial_scripts:
                messagebox.showerror("Input Error", "Serial supports only Nokia SAR and Nokia IXR.")
                return None

            context = {
                "customer": customer,
                "project": project,
                "customer_po": customer_po,
                "sales_order": sales_order,
                "output_file": output_file,
                "append_mode": append_mode,
                "connection_mode": connection_mode,
                "manual_script": manual_script,
            }

            from utils.credentials import load_credentials_from_config
            seed_user, seed_pass = load_credentials_from_config()

            if connection_mode == "LAN":
                ip_address = self.inventory_frame.lan_ip.strip()
                if not ip_address:
                    messagebox.showerror("Input Error", "LAN IP is required.")
                    return None
                try:
                    ipaddress.ip_address(ip_address)
                except ValueError:
                    messagebox.showerror("Input Error", "LAN IP must be a valid IPv4 or IPv6 address.")
                    return None
                context.update({
                    "target_id": ip_address,
                    "ip_address": ip_address,
                    "username": seed_user or "",
                    "password": seed_pass or "",
                })
            else:
                serial_port = self.inventory_frame.serial_port_var.get().strip()
                baud_raw = self.inventory_frame.serial_baud_var.get().strip()
                if not serial_port:
                    messagebox.showerror("Input Error", "Serial port is required (example: COM3).")
                    return None
                if not baud_raw:
                    messagebox.showerror("Input Error", "Baud rate is required.")
                    return None
                try:
                    baud_rate = int(baud_raw)
                except ValueError:
                    messagebox.showerror("Input Error", "Baud rate must be a number.")
                    return None

                context.update({
                    "target_id": serial_port,
                    "serial_port": serial_port,
                    "baud_rate": baud_rate,
                    "username": seed_user or "",
                    "password": seed_pass or "",
                })

            return context

        # Each selector resolves to a full start/end address. A pod contributes a fixed
        # /24 and reads only the fourth octet; the Lab reads a third octet per end, so a
        # Lab range may legitimately span more than one /24.
        range_1_start, range_1_end, range_1_error = self.inventory_frame.resolve_range(1)
        if range_1_error:
            messagebox.showerror("Input Error", range_1_error)
            return None
        if not range_1_start:
            messagebox.showwarning("Input Error", "Please enter at least one IP range (IP Selection 1).")
            return None

        range_2_start, range_2_end, range_2_error = self.inventory_frame.resolve_range(2)
        if range_2_error:
            messagebox.showerror("Input Error", range_2_error)
            return None

        def expand(start: str, end: str) -> list[str]:
            first, last = int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end))
            return [str(ipaddress.IPv4Address(value)) for value in range(first, last + 1)]

        range_1_ips = expand(range_1_start, range_1_end)
        range_2_ips = expand(range_2_start, range_2_end) if range_2_start else []

        # A Lab range can cross /24 boundaries, so guard against a sweep so large it
        # would stall the run rather than scanning what the operator meant.
        total_requested = len(range_1_ips) + len(range_2_ips)
        if total_requested > config.MAX_SCAN_ADDRESSES:
            messagebox.showerror(
                "Range Too Large",
                f"That selection covers {total_requested} addresses "
                f"(limit {config.MAX_SCAN_ADDRESSES}). Narrow the range and try again.",
            )
            logging.warning(f"Rejected oversized scan range: {total_requested} addresses")
            return None

        ip_list = set(range_1_ips)
        if range_2_ips:
            overlap_count = len(ip_list) + len(range_2_ips) - len(ip_list | set(range_2_ips))
            ip_list.update(range_2_ips)
            if overlap_count > 0:
                messagebox.showwarning("Duplicate IPs Detected", f"IP ranges overlap by {overlap_count} addresses. Duplicates will be removed.")
                logging.warning(f"Duplicate IPs detected and removed: {overlap_count} addresses")

        return {
            "customer": customer,
            "project": project,
            "customer_po": customer_po,
            "sales_order": sales_order,
            "output_file": output_file,
            "ip_list": list(ip_list),
            "append_mode": append_mode,
            "connection_mode": "Network",
        }

    def _build_manual_script_instance(self, context: Dict[str, Any]):
        script_name = context["manual_script"]
        module_name = self._manual_script_modules.get(script_name)
        if not module_name:
            raise ValueError(f"Unsupported script selection: {script_name}")
        # F025: defense-in-depth — refuse to import anything outside the allowlist.
        if module_name not in self._ALLOWED_SCRIPT_MODULES:
            raise ValueError(
                f"Refusing to import non-allowlisted module: {module_name!r}"
            )

        module = importlib.import_module(module_name)
        script_class = module.Script
        mode = context["connection_mode"]

        kwargs = {
            "db_cache": self.db_cache,
            "command_tracker": self.command_tracker,
            "stop_callback": self.should_stop,
        }

        if mode == "LAN":
            if script_name not in self._allowed_lan_scripts:
                raise ValueError(f"{script_name} is not supported in LAN mode")
            conn_type = self._lan_connection_types.get(script_name, "ssh")
            ip_address = context["ip_address"]
            # Telnet is allowlist-gated. Selecting a Telnet-based LAN script and
            # typing an IP is an explicit operator authorization for Telnet to
            # THAT host (same rationale as the Network path auto-allowlisting
            # identified 1830s), so add it before the connection is attempted.
            if conn_type == "telnet":
                try:
                    from utils.telnet_policy import add_telnet_allowlist
                    add_telnet_allowlist(
                        ip_address,
                        f"auto: {script_name} LAN inventory (operator-selected)",
                    )
                except Exception as exc:
                    logging.warning(
                        f"[TELNET] Could not auto-allowlist {ip_address}: {exc}"
                    )
            kwargs.update({
                "connection_type": conn_type,
                "ip_address": ip_address,
                "username": context["username"],
                "password": context["password"],
            })
        elif mode == "Serial":
            if script_name not in self._allowed_serial_scripts:
                raise ValueError(f"{script_name} is not supported in Serial mode")
            kwargs.update({
                "connection_type": "serial",
                "serial_port": context["serial_port"],
                "baud_rate": context["baud_rate"],
                "username": context["username"],
                "password": context["password"],
            })
        else:
            raise ValueError(f"Unsupported connection mode: {mode}")

        return script_class(**kwargs)

    def start_run_worker(self):
        self.run_future = self._worker_pool.submit(self.run_inventory_worker, self.run_context)
        self.root.after(100, self.poll_run_queue)

    def start_export_worker(self, processed_data):
        self.export_future = self._worker_pool.submit(self.run_export_worker, self.run_context, processed_data)
        self.root.after(100, self.poll_run_queue)

    def run_inventory_worker(self, context: Dict[str, Any]) -> None:
        queue = self.run_queue

        # Reset the command tracker so re-running the same device in the same
        # app session doesn't skip commands marked "already executed".
        script_interface.get_tracker().reset()

        # Stale-state reset for EVERY run (LAN/Serial Direct Connection AND
        # multi-host queue). The reset used to live only on the multi-host
        # path further down, so a Direct Connection run would inherit
        # ``failed_ips`` from the previous run -- the end-of-run popup then
        # listed devices from prior runs (e.g. a Serial COM11 failure
        # showing up after a successful LAN run on a different IP).
        self.failed_ips = {}
        self.pause_queue = []
        # Drain stale credential responses left over from a prior run that
        # was aborted mid-prompt -- otherwise the next prompt resolves
        # immediately with the wrong answer.
        while not self._creds_response_queue.empty():
            try:
                self._creds_response_queue.get_nowait()
            except Empty:
                break

        try:
            if context.get("connection_mode") in ("LAN", "Serial"):
                queue.put(("progress", (0, 1, "Preparing 0/1")))
                manual_script = self._build_manual_script_instance(context)
                self.task_queue = Queue()
                self.device_family_by_ip[context["target_id"]] = self._family_for_script(manual_script)
                self.task_queue.put((context["target_id"], manual_script))
                queue.put((
                    "log",
                    f"Starting {context['connection_mode']} inventory for "
                    f"{context['target_id']} via "
                    f"{context.get('manual_script') or 'manual script'}…",
                ))
                self.process_task_queue(queue)
                # LAN-mode cleanup: the same management IP (e.g. 10.0.0.1)
                # frequently maps to a different physical device between
                # runs in a lab/test workflow, so the TOFU-stored host
                # key would block the next attempt with a mismatch error.
                # Clear our known_hosts entry for this IP after every LAN
                # run so the next pull TOFU-accepts whatever's there now.
                # Serial mode doesn't use SSH so it's skipped.
                if context.get("connection_mode") == "LAN":
                    try:
                        from utils.helpers import clear_known_host_entry
                        ip = context.get("target_id")
                        if ip and clear_known_host_entry(ip):
                            queue.put((
                                "log",
                                f"[{ip}] Cleared stored SSH host key "
                                f"(LAN-mode reset — next run will TOFU-accept fresh)",
                            ))
                    except Exception:
                        logging.exception(
                            "Failed to clear known_hosts after LAN run"
                        )
                # Only kick off the export when we have something to export.
                # If the script bailed before populating self.outputs — most
                # often a serial probe that never saw a console prompt — the
                # downstream export worker would otherwise emit an
                # ``export_complete`` event with the would-be output path
                # and the GUI would print "Report saved successfully as:
                # …" even though no file ever hit disk.
                if self.outputs:
                    queue.put(("inventory_complete", True))
                else:
                    queue.put((
                        "log",
                        "No inventory data collected — no report saved.",
                    ))
                    queue.put(("inventory_complete", False))
                return

            # ``failed_ips`` / ``pause_queue`` / credential-response queue
            # are reset at the top of ``run_inventory_worker`` so both the
            # Direct Connection path and this multi-host LAN-scan path
            # start clean. No reset needed here.
            total_ips = len(context["ip_list"])
            reachable_ips = []
            reachable_lock = threading.Lock()

            queue.put((
                "log",
                f"Starting LAN inventory — {total_ips} device(s) to scan.",
            ))
            queue.put((
                "log",
                f"Phase 1/3 · Pinging {total_ips} device(s)…",
            ))

            def _probe(ip):
                return ip, script_interface.is_reachable(ip)

            with ThreadPoolExecutor(max_workers=min(20, total_ips or 1)) as ping_pool:
                futures = {ping_pool.submit(_probe, ip): ip for ip in context["ip_list"]}
                for idx, future in enumerate(as_completed(futures), start=1):
                    if self.stop_threads:
                        ping_pool.shutdown(wait=False, cancel_futures=True)
                        queue.put(("aborted", None))
                        return
                    ip, alive = future.result()
                    if alive:
                        with reachable_lock:
                            reachable_ips.append(ip)
                    else:
                        self.failed_ips[ip] = "Unreachable"
                    queue.put(("progress", (idx, total_ips, f"Pinging {idx}/{total_ips}")))

            unreachable_count = total_ips - len(reachable_ips)
            queue.put((
                "log",
                f"Ping complete — {len(reachable_ips)} reachable, "
                f"{unreachable_count} unreachable.",
            ))

            if not reachable_ips:
                queue.put(("log", "No reachable IPs found."))
                queue.put(("inventory_complete", False))
                return

            total_reachable = len(reachable_ips)
            queue.put((
                "log",
                f"Phase 2/3 · Scanning {total_reachable} reachable device(s) "
                f"(up to 5 in parallel)…",
            ))
            completed_count = 0
            completed_lock = threading.Lock()

            # Combined identify + execute pipeline: each of up to 5 concurrent
            # workers handles one device end-to-end, reusing the SSH connection
            # when possible so we avoid a second authentication round-trip.
            with ThreadPoolExecutor(
                max_workers=min(5, total_reachable or 1),
                thread_name_prefix="atlas-scan",
            ) as scan_pool:
                scan_futures = {
                    scan_pool.submit(self._process_single_device, ip, queue): ip
                    for ip in reachable_ips
                }
                for future in as_completed(scan_futures):
                    if self.stop_threads:
                        scan_pool.shutdown(wait=False, cancel_futures=True)
                        queue.put(("aborted", None))
                        return

                    while self.is_paused and not self.stop_threads:
                        time.sleep(0.1)

                    ip, status, script_instance, fam = future.result()

                    with completed_lock:
                        completed_count += 1
                        count = completed_count

                    queue.put(("progress", (count, total_reachable, f"Scanning {count}/{total_reachable}")))

                    if status == "ABORTED":
                        scan_pool.shutdown(wait=False, cancel_futures=True)
                        queue.put(("aborted", None))
                        return
                    elif status == "CREDS_REQUIRED":
                        self.pause_queue.append((ip, script_instance))
                    elif status:
                        self.failed_ips[ip] = status
                        queue.put(("log", f"[{ip}] Error: {status}"))
                    else:
                        # Surface per-device success so the operator
                        # sees the panel update at the same rate the
                        # progress bar ticks. Errors are logged above.
                        queue.put((
                            "log",
                            f"[{ip}] Scan complete ({count}/{total_reachable}).",
                        ))

            if self.pause_queue and not self.stop_threads:
                self._drain_pause_queue(queue)

            if self.failed_ips:
                lines = ["\n--- FAILED IPs ---"]
                for ip, reason in self.failed_ips.items():
                    lines.append(f"  {ip}: {reason}")
                queue.put(("log", "\n".join(lines)))

            # Same guard as the LAN/Serial single-device path: only signal
            # success when something landed in self.outputs. Otherwise the
            # export worker's empty-data branch would falsely emit
            # ``export_complete`` with the would-be output_file path.
            if self.outputs:
                queue.put((
                    "log",
                    f"Phase 3/3 · Building report for "
                    f"{len(self.outputs)} device(s)…",
                ))
                queue.put(("inventory_complete", True))
            else:
                queue.put((
                    "log",
                    "No inventory data collected — no report saved.",
                ))
                queue.put(("inventory_complete", False))
        except Exception as exc:
            logging.exception("Background inventory run failed")
            queue.put(("error", friendly_error(exc)))

    def run_export_worker(self, context: Dict[str, Any], _processed_data: Optional[Dict[str, Any]]) -> None:
        try:
            with self.lock:
                outputs_copy = dict(self.outputs)
                family_map = dict(self.device_family_by_ip)

            logging.debug(f"[EXPORT] outputs keys: {list(outputs_copy.keys())}")
            logging.debug(f"[EXPORT] family_map: {family_map}")

            # Partition outputs by family. IPs with no recorded family
            # (e.g., legacy in-memory data from a prior run) fall through
            # to "default" so we never silently drop them.
            buckets: Dict[str, Dict[str, Any]] = {"rls": {}, "psi": {}, "default": {}}
            for ip, data in outputs_copy.items():
                fam = family_map.get(ip, "default")
                logging.debug(f"[EXPORT] IP={ip!r} → family={fam!r}")
                buckets.setdefault(fam, {})[ip] = data

            non_empty = {fam: ips for fam, ips in buckets.items() if ips}
            output_file = context["output_file"]

            if not non_empty:
                self.run_queue.put(("export_complete", {"processed_data": {}, "output_file": output_file}))
                return

            common_kwargs = dict(
                customer=context["customer"],
                project=context["project"],
                customer_po=context["customer_po"],
                sales_order=context["sales_order"],
                append_mode=context.get("append_mode", False),
            )

            # Surface what we're about to do so the operator doesn't
            # stare at a frozen UI during a multi-second Excel build.
            family_summary = ", ".join(
                f"{fam}:{len(ips)}" for fam, ips in non_empty.items()
            )
            self.run_queue.put((
                "log",
                f"Building inventory workbook ({family_summary}) → "
                f"{os.path.basename(output_file)}…",
            ))

            # Single-family scans bypass the unified builder so behavior
            # exactly matches the prior single-template path.
            if len(non_empty) == 1:
                fam = next(iter(non_empty))
                ips = non_empty[fam]
                if fam == "rls":
                    processed_data = self.build_psi_report_workbook(
                        ips, output_file, template_override=self.rls_template_path, **common_kwargs
                    )
                elif fam == "psi":
                    processed_data = self.build_psi_report_workbook(ips, output_file, **common_kwargs)
                else:
                    processed_data = self.build_report_workbook(ips, output_file, **common_kwargs)
            else:
                processed_data = self.build_unified_report_workbook(non_empty, output_file, **common_kwargs)

            self.run_queue.put((
                "export_complete",
                {"processed_data": processed_data, "output_file": output_file},
            ))
        except Exception as exc:
            logging.exception("Background export failed")
            self.run_queue.put(("export_error", friendly_error(exc)))

    def should_stop(self):
        return self.stop_threads

    def poll_run_queue(self):
        should_reschedule = True

        while not self.run_queue.empty():
            try:
                item = self.run_queue.get_nowait()
            except Empty:
                break

            if isinstance(item, tuple) and len(item) == 2:
                event_type, payload = item
            else:
                event_type, payload = "log", item

            if event_type == "log":
                self.log_activity(payload)
            elif event_type == "progress":
                current, total, label = payload
                self.inventory_frame.update_progress(current, total, label)
                self.log_activity(
                    f"[PROGRESS] {label} ({current}/{total})"
                )
            elif event_type == "error":
                self.log_activity(f"[RUN] Failed: {payload}", logging.ERROR)
                self.finish_run()
                messagebox.showerror("Run Error", payload)
                should_reschedule = False
                break
            elif event_type == "aborted":
                self.log_activity("[RUN] Run aborted.", logging.WARNING)
                self.finish_run()
                should_reschedule = False
                break
            elif event_type == "inventory_complete":
                if payload:
                    self.log_activity(
                        "[INVENTORY] Collection complete; starting export."
                    )
                    self.update_status("Exporting...")
                    self.start_export_worker(None)
                else:
                    self.log_activity(
                        "[INVENTORY] Collection completed without an export.",
                        logging.WARNING,
                    )
                    self.finish_run()
                should_reschedule = False
                break
            elif event_type == "export_complete":
                output_file = payload["output_file"]
                self.log_activity(
                    f"[EXPORT] Report saved successfully: {output_file}"
                )
                self.finish_run(success_message=True)
                should_reschedule = False
                break
            elif event_type == "export_error":
                self.log_activity(
                    f"[EXPORT] Failed to save Excel file: {payload}",
                    logging.ERROR,
                )
                self.finish_run()
                messagebox.showerror("Export Error", f"Failed to save Excel file:\n{payload}")
                should_reschedule = False
                break
            elif event_type == "packing_complete":
                self.log_activity(
                    f"[PACKING] Packing slips saved: {payload}"
                )
                self.finish_run(success_message=True)
                messagebox.showinfo("Success", f"Packing slips saved to:\n{payload}")
                should_reschedule = False
                break
            elif event_type == "packing_error":
                self.log_activity(
                    f"[PACKING] Packing slip export failed: {payload}",
                    logging.ERROR,
                )
                self.finish_run()
                messagebox.showerror("Packing Slip Error", f"Error:\n{payload}")
                should_reschedule = False
                break
            elif event_type == "creds_needed":
                # Worker has parked an IP and is blocked waiting for the user
                # to type credentials. Prompt on the main (Tk) thread, then
                # push the answer (or None) back to the worker's response
                # queue so the worker can retry that single device.
                ip = payload
                try:
                    from utils.credentials import prompt_for_credentials_gui
                    self.log_activity(
                        f"[AUTH] {ip}: default credentials failed; "
                        "requesting operator credentials.",
                        logging.WARNING,
                    )
                    answer = prompt_for_credentials_gui(parent_window=self.root)
                except Exception as prompt_err:
                    logging.exception(f"Credential prompt failed for {ip}")
                    self.log_activity(
                        f"[AUTH] {ip}: credential prompt failed: "
                        f"{friendly_error(prompt_err)}",
                        logging.ERROR,
                    )
                    answer = None
                # Always push *something* so the worker doesn't hang.
                self._creds_response_queue.put(answer if answer else (None, None))
            else:
                self.log_activity(
                    f"[RUN] Unrecognized queue event {event_type}: {payload}",
                    logging.WARNING,
                )

        if should_reschedule and ((self.run_future and not self.run_future.done()) or (self.export_future and not self.export_future.done())):
            self.root.after(100, self.poll_run_queue)
        elif should_reschedule and ((self.run_future and self.run_future.done()) or (self.export_future and self.export_future.done())):
            self.finish_run()

    def finish_run(self, success_message: bool = False):
        self.set_run_controls(False)
        self.update_status("Ready")
        sys.stdout = self._stdout_original
        self.inventory_frame.reset_progress()

        self.run_context = None
        self.run_future = None
        self.export_future = None

        # Surface failed devices as a popup BEFORE the "System Ready"
        # success message. Without this, a partial run (e.g. 11 of 12
        # chassis captured because one rejected the credential ladder)
        # only shows the failure as a line in the scrolling output
        # panel -- which the operator can easily miss, then trust the
        # report and ship a packing slip that's missing a chassis.
        # Snapshot the dict before the popup since some callers may
        # mutate it during showwarning's modal loop.
        failed_snapshot = dict(self.failed_ips)
        if failed_snapshot:
            self._show_failed_devices_popup(failed_snapshot)

        if success_message:
            self.stop_threads = True
            messagebox.showinfo("System Ready", "The system is ready.")

    def _show_failed_devices_popup(self, failed: Dict[str, str]) -> None:
        """Pop a warning dialog listing every device that didn't make
        it into the inventory. Called from :py:meth:`finish_run` so
        every run-completion path -- success, error, abort -- gets
        the same end-of-run summary if anything failed."""
        count = len(failed)
        # Cap the displayed list so a 200-device LAN scan with widespread
        # failures doesn't produce a dialog the operator can't see the
        # bottom of. The full list is always in the file log + output
        # panel.
        MAX_SHOWN = 15
        items = list(failed.items())
        shown = items[:MAX_SHOWN]
        rest = len(items) - len(shown)
        lines = [f"  • {ip}: {reason}" for ip, reason in shown]
        if rest > 0:
            lines.append(f"  • ... plus {rest} more (see Output panel)")
        header = (
            f"{count} device{'s' if count != 1 else ''} did not return "
            f"inventory data. Re-run them after fixing the cause "
            f"(credentials, cable, reachability, etc.)."
        )
        body = header + "\n\n" + "\n".join(lines)
        try:
            messagebox.showwarning("Some Devices Failed", body)
        except Exception:
            logging.exception("Failed-devices popup did not display")

    def run_script(self):
        self.log_activity("[RUN] Inventory run requested.")
        if (self.run_future and not self.run_future.done()) or (self.export_future and not self.export_future.done()):
            self.log_activity(
                "[RUN] Request refused because another run is in progress.",
                logging.WARNING,
            )
            messagebox.showwarning("Run In Progress", "Please wait for the current run to finish.")
            return

        with self.lock:
            self.outputs.clear()
            self.device_family_by_ip.clear()
        self.stop_threads = False
        self.is_paused = False

        context = self.collect_run_context()
        if not context:
            self.log_activity(
                "[RUN] Inventory run cancelled or blocked by input validation.",
                logging.WARNING,
            )
            self.update_status("Ready")
            return

        self.run_context = context
        self.run_queue = Queue()
        self.set_run_controls(True)
        self.update_status("Running...")
        target_count = (
            len(context.get("ip_list", ()))
            if context.get("connection_mode") == "Network"
            else 1
        )
        self.log_activity(
            f"[RUN] Starting {context.get('connection_mode', 'unknown')} "
            f"inventory for {target_count} target(s)."
        )
        self.start_run_worker()


    def process_task_queue(self, queue: Queue) -> None:
        total_tasks = self.task_queue.qsize()
        completed = 0
        while not self.task_queue.empty():
            item = self.task_queue.get()
            
            # Unpack original format: (ip, script_instance)
            ip, script_instance = item
            self.current_script_instance = script_instance
            completed += 1

            try:
                if self.stop_threads:
                    queue.put(("aborted", None))
                    return

                while self.is_paused and not self.stop_threads:
                    time.sleep(0.1)

                if self.stop_threads:
                    queue.put(("aborted", None))
                    return

                # 1. Run normal inventory (your existing logic)
                commands = script_instance.get_commands() or []
                if commands:
                    queue.put((
                        "log",
                        f"[{ip}] Connecting and running "
                        f"{len(commands)} command(s)…",
                    ))
                    outputs_list, error = script_instance.execute_commands(commands)
                    if error == "Aborted":
                        queue.put(("aborted", None))
                        return
                    if error == script_interface.NEEDS_CREDENTIALS_SENTINEL:
                        # Credentials failed — park for user prompt after the
                        # rest of the run completes.
                        self.pause_queue.append((ip, script_instance))
                        queue.put((
                            "log",
                            f"[{ip}] Credentials failed — parking for "
                            f"manual credential entry after the rest of the run.",
                        ))
                    elif error:
                        logging.warning(f"[TASK] Script execution error for {ip}: {error}")
                        queue.put(("log", f"Error in normal inventory for {ip}: {error}"))
                        if any(kw in error.lower() for kw in ("auth", "login", "credential", "password", "invalid")):
                            # Defensive fallback: a script returned an auth-related
                            # error string instead of NEEDS_CREDENTIALS_SENTINEL.
                            # Park for retry rather than recording as a hard failure.
                            self.pause_queue.append((ip, script_instance))
                            queue.put((
                                "log",
                                f"[{ip}] Auth error detected — parking for manual credential entry.",
                            ))
                        else:
                            self.failed_ips[ip] = error
                    elif outputs_list and hasattr(script_instance, "process_outputs"):
                        with self.lock:
                            script_instance.process_outputs(outputs_list, ip, self.outputs)
                        queue.put(("log", f"Normal inventory completed for {ip}"))
                    else:
                        queue.put(("log", f"No output from normal inventory for {ip}"))

                queue.put(("log", f"Overall processing finished for {ip}"))
                queue.put(("progress", (completed, total_tasks, f"Collecting {completed}/{total_tasks}")))

            except Exception as e:
                logging.exception(f"[TASK] Unhandled error processing {ip}")
                queue.put(("log", f"Error processing {ip}: {friendly_error(e)}"))

        # Main queue drained — now process anything that needed manual creds.
        if self.pause_queue and not self.stop_threads:
            self._drain_pause_queue(queue)

    def _process_single_device(
        self,
        ip: str,
        queue: Queue,
    ) -> Tuple[str, Optional[str], Any, Optional[str]]:
        """Identify and collect inventory for a single device in one pass.

        Called from the concurrent scan pool in ``run_inventory_worker``.
        Creates its own ``DeviceIdentifier`` and ``ScriptSelector`` instances
        so multiple devices can run completely in parallel without sharing state.

        Returns:
            (ip, status, script_instance, family)
            - status is ``None`` on success, ``"CREDS_REQUIRED"``, ``"ABORTED"``,
              or an error string.
            - script_instance is the Script object (``None`` if identification failed).
            - family is the export-template routing key (``None`` if unavailable).
        """
        device_identifier = script_interface.DeviceIdentifier()
        script_selector = script_interface.ScriptSelector()
        script_instance = None
        fam = None

        queue.put(("log", f"[{ip}] Identifying device type…"))

        # ── Phase A: Identify ─────────────────────────────────────────────
        try:
            device_type, device_name = device_identifier.identify_device(
                ip, queue, None, self.should_stop
            )
        except script_interface.CredentialPromptRequired:
            queue.put((
                "log",
                f"[{ip}] Default credentials exhausted — parked for manual credential entry.",
            ))
            return ip, "CREDS_REQUIRED", None, None
        except Exception as exc:
            logging.exception(f"[PIPELINE] Identification error for {ip}")
            return ip, f"Identification error: {friendly_error(exc)}", None, None

        if self.stop_threads:
            return ip, "ABORTED", None, None

        if not device_type:
            return ip, "Identification failed", None, None

        queue.put(("log", f"[{ip}] Identified as {device_type}."))

        # ── Phase B: Select script and optionally inject kept SSH client ───
        # Nokia 1830 now uses SSH (paramiko auth_none + two-stage shell login).
        # Pass through the credentials that authenticated during identification
        # so the inventory script skips re-rotating through admin/cli/su.
        conn_type = 'ssh'
        ident_creds = device_identifier.take_identified_credentials()
        script_instance = script_selector.select_script(
            device_type, ip, connection_type=conn_type,
            stop_callback=self.should_stop, credentials=ident_creds,
        )
        if not script_instance:
            reason = f"Unknown device type: {device_type}"
            return ip, reason, None, None

        fam = self._family_for_script(script_instance)
        logging.debug(f"[FAMILY] (pipeline) ip={ip} mod={type(script_instance).__module__} → fam={fam!r}")
        with self.lock:
            self.device_family_by_ip[ip] = fam

        # Hand off the SSH client kept alive during identification so the
        # script can skip the second authentication round-trip.
        kept_client = device_identifier.take_identified_client()
        if kept_client is not None and hasattr(script_instance, 'set_existing_ssh_client'):
            script_instance.set_existing_ssh_client(kept_client)

        # ── Phase C: Execute ───────────────────────────────────────────────
        with self._active_scripts_lock:
            self._active_scripts[ip] = script_instance
        try:
            if self.stop_threads:
                return ip, "ABORTED", script_instance, fam

            while self.is_paused and not self.stop_threads:
                time.sleep(0.1)

            if self.stop_threads:
                return ip, "ABORTED", script_instance, fam

            commands = script_instance.get_commands() or []
            if not commands:
                queue.put(("log", f"No commands for {ip}"))
                return ip, None, script_instance, fam

            queue.put((
                "log",
                f"[{ip}] Running {len(commands)} command(s)…",
            ))
            outputs_list, error = script_instance.execute_commands(commands)

            if error == "Aborted":
                return ip, "ABORTED", script_instance, fam

            if error == script_interface.NEEDS_CREDENTIALS_SENTINEL:
                queue.put((
                    "log",
                    f"[{ip}] Credentials failed during execution — parking for manual entry.",
                ))
                return ip, "CREDS_REQUIRED", script_instance, fam

            if error:
                if any(kw in error.lower() for kw in ("auth", "login", "credential", "password", "invalid")):
                    queue.put(("log", f"[{ip}] Auth error — parking for manual credential entry."))
                    return ip, "CREDS_REQUIRED", script_instance, fam
                return ip, error, script_instance, fam

            if outputs_list and hasattr(script_instance, "process_outputs"):
                with self.lock:
                    script_instance.process_outputs(outputs_list, ip, self.outputs)
                queue.put(("log", f"Inventory completed for {ip}"))
            else:
                queue.put(("log", f"No output from {ip}"))

            return ip, None, script_instance, fam

        except Exception as exc:
            logging.exception(f"[PIPELINE] Unhandled error processing {ip}")
            return ip, friendly_error(exc), script_instance, fam
        finally:
            with self._active_scripts_lock:
                self._active_scripts.pop(ip, None)

    def _drain_pause_queue(self, queue: Queue) -> None:
        """Prompt user for credentials per parked IP and retry collection.

        Runs on the worker thread. For each parked (ip, script_instance):
          1. Post ("creds_needed", ip) on the run queue. The main thread sees
             this in poll_run_queue, prompts the user with a Tk dialog, and
             pushes the answer (or None) to ``self._creds_response_queue``.
          2. Block on the response queue (with stop/pause checks).
          3. If user provided creds, swap them onto the script_instance and
             re-run execute_commands. Otherwise mark the IP as skipped.
        """
        parked = list(self.pause_queue)
        self.pause_queue.clear()
        total = len(parked)
        # Need our own identifier/selector since the worker's locals are out
        # of scope here. They're cheap to construct.
        device_identifier = script_interface.DeviceIdentifier()
        script_selector = script_interface.ScriptSelector()
        queue.put((
            "log",
            f"\n--- Processing {total} device(s) that need manual credentials ---",
        ))
        for idx, (ip, script_instance) in enumerate(parked, start=1):
            if self.stop_threads:
                queue.put(("aborted", None))
                return
            while self.is_paused and not self.stop_threads:
                time.sleep(0.1)
            if self.stop_threads:
                queue.put(("aborted", None))
                return

            queue.put((
                "progress",
                (idx, total, f"Manual credentials {idx}/{total}"),
            ))
            queue.put(("log", f"[{ip}] Requesting credentials from user..."))

            # Drain any stale response just in case.
            while not self._creds_response_queue.empty():
                try:
                    self._creds_response_queue.get_nowait()
                except Empty:
                    break

            queue.put(("creds_needed", ip))

            new_creds: Optional[Tuple[Optional[str], Optional[str]]] = None
            while True:
                if self.stop_threads:
                    queue.put(("aborted", None))
                    return
                try:
                    new_creds = self._creds_response_queue.get(timeout=0.25)
                    break
                except Empty:
                    continue

            if not new_creds or not new_creds[0]:
                self.failed_ips[ip] = (
                    "Login failed: user declined to provide credentials"
                )
                queue.put((
                    "log",
                    f"[{ip}] Skipped — no credentials provided by user.",
                ))
                continue

            new_user, new_pass = new_creds

            # Identify-time park: no script_instance was ever built. Re-run
            # identification with the user-provided creds, build the script,
            # then execute it.
            if script_instance is None:
                queue.put(("log", f"[{ip}] Re-identifying with user-provided credentials..."))
                try:
                    device_type, device_name = device_identifier.identify_device(
                        ip,
                        queue,
                        None,
                        self.should_stop,
                        explicit_credentials=(new_user, new_pass),
                    )
                except script_interface.CredentialPromptRequired:
                    self.failed_ips[ip] = "Login failed: user-provided credentials rejected at identification"
                    queue.put(("log", f"[{ip}] User-provided credentials rejected during identification."))
                    continue
                except Exception as e:
                    logging.exception(f"[PAUSE] Re-identify error for {ip}")
                    self.failed_ips[ip] = f"Identification failed: {friendly_error(e)}"
                    queue.put(("log", f"[{ip}] Re-identify error: {friendly_error(e)}"))
                    continue

                if not device_type:
                    self.failed_ips[ip] = "Identification failed even with user-provided credentials"
                    queue.put(("log", f"[{ip}] Could not identify device with user-provided credentials."))
                    continue

                conn_type = 'ssh'
                # Re-identification just stored the working creds (the
                # user-provided pair); thread them into the script so it
                # starts with the right login on the first try.
                ident_creds = device_identifier.take_identified_credentials()
                script_instance = script_selector.select_script(
                    device_type, ip, connection_type=conn_type,
                    stop_callback=self.should_stop, credentials=ident_creds,
                )
                if not script_instance:
                    self.failed_ips[ip] = f"Unknown device type: {device_type}"
                    queue.put(("log", f"[{ip}] No script for device type {device_type!r}"))
                    continue

            # Record the export-template family before running. The
            # bulk-identify path (run_inventory_worker) does this inline,
            # but parked IPs land here without it being set, which would
            # leave them routed to the default workbook builder.
            fam = self._family_for_script(script_instance)
            logging.info(f"[FAMILY] (pause-queue) ip={ip} → fam={fam!r}")
            self.device_family_by_ip[ip] = fam

            # Push the user-provided creds onto whichever attribute the script
            # uses. SAR/IXR take them as method args; Smartoptics_DCP and
            # Nokia_1830 hold them as instance state.
            if hasattr(script_instance, "username"):
                script_instance.username = new_user
            if hasattr(script_instance, "password"):
                script_instance.password = new_pass

            queue.put(("log", f"[{ip}] Retrying with user-provided credentials..."))
            try:
                commands = script_instance.get_commands() or []
                outputs_list, error = script_instance.execute_commands(commands)
                if error == "Aborted":
                    queue.put(("aborted", None))
                    return
                if error == script_interface.NEEDS_CREDENTIALS_SENTINEL:
                    # User-provided creds also failed default-set → record
                    # and move on. We do not re-prompt the same IP twice.
                    self.failed_ips[ip] = "Login failed: user-provided credentials rejected"
                    queue.put((
                        "log",
                        f"[{ip}] User-provided credentials also failed. Skipping.",
                    ))
                elif error:
                    logging.warning(f"[PAUSE] Retry error for {ip}: {error}")
                    queue.put(("log", f"[{ip}] Retry error: {error}"))
                    self.failed_ips[ip] = f"Login failed: {error}"
                elif outputs_list and hasattr(script_instance, "process_outputs"):
                    with self.lock:
                        script_instance.process_outputs(outputs_list, ip, self.outputs)
                    queue.put(("log", f"[{ip}] Inventory completed via manual credentials"))
                    self.failed_ips.pop(ip, None)
                else:
                    queue.put(("log", f"[{ip}] No output after manual credential retry"))
            except Exception as e:
                logging.exception(f"[PAUSE] Unhandled error processing {ip}")
                queue.put(("log", f"[{ip}] Retry error: {friendly_error(e)}"))

    def update_gui_from_queue(self, queue):
        while not queue.empty():
            try:
                message = queue.get_nowait()
                self.log_activity(message)
            except Empty:
                break
        
    def pause_program(self):
        with self.lock:
            self.is_paused = not self.is_paused
            self.update_status("Paused" if self.is_paused else "Resumed")
            self.log_activity(
                "[RUN] Paused by operator."
                if self.is_paused
                else "[RUN] Resumed by operator."
            )
    
    def abort_program(self):
        self.log_activity("[RUN] Abort requested by operator.", logging.WARNING)
        with self.lock:
            self.stop_threads = True
            # Stop any script that is actively executing (concurrent pipeline).
            with self._active_scripts_lock:
                for ip, script_inst in list(self._active_scripts.items()):
                    if hasattr(script_inst, 'abort_connection'):
                        try:
                            script_inst.abort_connection()
                        except Exception as e:
                            logging.debug(f"Error calling abort_connection for {ip}: {e}")
            # Also stop the legacy single-instance reference (LAN/Serial modes).
            if self.current_script_instance and hasattr(self.current_script_instance, 'abort_connection'):
                try:
                    self.current_script_instance.abort_connection()
                except Exception as e:
                    logging.debug(f"Error calling abort_connection: {e}")
            self.update_status("Aborted")
        messagebox.showinfo("Aborted", "The program has been aborted.")
        
# Redirect Console Output to GUI Output Screen
class ConsoleRedirector:
    def __init__(self, widget):
        self.widget = widget

    def write(self, message):
        self.widget.insert(tk.END, message)
        self.widget.see(tk.END)  # Auto-scroll to the latest message

    def flush(self):
        pass  # Required for compatibility with logging
    
def main():
    # Development entry point: use the same rotating log as main.py instead
    # of silently dropping INFO records when this module is run directly.
    from utils.logging_setup import configure_atlas_logging

    log_file = configure_atlas_logging()
    logging.info("Starting ATLAS")
    logging.info("Log file: %s", log_file)
    root = tk.Tk()
    # Reuse the already-created instances
    app = InventoryGUI(
        root,
        update_available=False,
        command_tracker=command_tracker,
        db_cache=db_cache
    )
    root.mainloop()

if __name__ == "__main__":
    main()
