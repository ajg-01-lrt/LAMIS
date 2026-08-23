"""Network Audit frame — Diagnostics → RLS REST-based network audit.

Drives ``scripts/Network/RLS_Audit.py`` (adapted from Apple's
``rls_audit_updated`` reference). One seed host + credentials are
enough — the audit's first REST call
(``restconf/data/ciena-6500r-nodes:nodes=*``) discovers the full
topology and walks every node automatically. Seed file, max-hop, and
seed-TID inputs from the old TDS-based version are gone; they're
unnecessary with REST discovery.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from utils.helpers import (
    friendly_error,
    get_desktop_dir,
    load_user_prefs,
    save_user_prefs,
    scrub_password_widget,
)


_PREFS_KEY_AUDIT_DIR = "network_audit_output_dir"


def _resolve_default_output_dir() -> str:
    """Pick the directory the Browse dialog opens in / Output File
    defaults under.

    Order of preference:
      1. Last directory the operator browsed to, if it still exists.
      2. The OS Desktop directory.
      3. The user's home directory (only if Desktop is missing).
    """
    prefs = load_user_prefs()
    remembered = prefs.get(_PREFS_KEY_AUDIT_DIR)
    if isinstance(remembered, str) and remembered:
        if os.path.isdir(remembered):
            return remembered
    desktop = get_desktop_dir()
    if desktop.is_dir():
        return str(desktop)
    return os.path.expanduser("~")


def _is_valid_host(value: str) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]*$", value) and "&" not in value)


class NetworkAuditFrame(ttk.Frame):
    """Tk frame for the RLS REST Network Audit (Diagnostics sub-mode)."""

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self._running = False
        self._build()

    def _build(self) -> None:
        cfg = ttk.LabelFrame(self, text="Network Audit Configuration")
        cfg.pack(fill=tk.X, pady=5)

        # Network type selector — reshapes credentials + optional collection.
        # Ciena RLS drives the REST audit; Nokia PSI drives the SSH audit
        # (scripts/Network/Nokia_PSI_Audit.py). Defaults to RLS so the existing
        # flow is unchanged until the operator switches.
        tk.Label(cfg, text="Network Type:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.network_type_var = tk.StringVar(value="Ciena RLS")
        self.type_combo = ttk.Combobox(
            cfg, textvariable=self.network_type_var, state="readonly",
            values=["Ciena RLS", "Nokia PSI"], width=18,
        )
        self.type_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_change)

        tk.Label(cfg, text="Seed IP / Hostname:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.seed_entry = tk.Entry(cfg, width=30)
        self.seed_entry.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        # Hint text is set per network type by _on_type_change().
        self.seed_hint_label = tk.Label(cfg, text="", fg="gray")
        self.seed_hint_label.grid(row=1, column=2, columnspan=2, sticky="w", padx=5)

        # Credentials are pre-filled per type from the encrypted credential
        # store by _on_type_change() (RLS RESTCONF diaguser/Ciena123; PSI SSH
        # admin/admin). The operator can override either field.
        tk.Label(cfg, text="Username:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.username_entry = tk.Entry(cfg, width=20)
        self.username_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")

        tk.Label(cfg, text="Password:").grid(row=2, column=2, sticky="w", padx=5, pady=5)
        self.password_entry = tk.Entry(cfg, width=20, show="*")
        self.password_entry.grid(row=2, column=3, padx=5, pady=5, sticky="w")

        self.cred_hint_label = tk.Label(cfg, text="", fg="gray")
        self.cred_hint_label.grid(row=3, column=0, columnspan=4, sticky="w", padx=5)

        tk.Label(cfg, text="Output File:").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        self.output_entry = tk.Entry(cfg, width=50)
        self.output_entry.grid(row=4, column=1, columnspan=2, padx=5, pady=5, sticky="w")
        # Default output path (name + dir) is filled per type by
        # _on_type_change(); the timestamp reflects when the screen was opened.
        tk.Button(cfg, text="Browse…", command=self._browse_output).grid(
            row=4, column=3, padx=5, pady=5, sticky="w",
        )

        # Optional Data Collection — the exact widgets shown depend on the
        # network type (see _on_type_change): RLS shows the alarm toggles; PSI
        # shows an info note (it always captures alarms). Debug shows for both.
        opts = ttk.LabelFrame(self, text="Optional Data Collection")
        opts.pack(fill=tk.X, pady=5)
        self._opts_frame = opts
        self.capture_alarms_var = tk.BooleanVar(value=False)
        self.cb_alarms = tk.Checkbutton(
            opts, text="Capture active alarms (adds 'Alarms' sheet)",
            variable=self.capture_alarms_var,
        )
        self.capture_history_var = tk.BooleanVar(value=False)
        self.cb_history = tk.Checkbutton(
            opts, text="Capture alarm history (adds 'Alarm History' sheet)",
            variable=self.capture_history_var,
        )
        self.psi_opts_note = tk.Label(
            opts,
            text=(
                "PSI always captures alarms; the whole line is auto-discovered"
                " and walked from the seed."
            ),
            fg="gray",
        )
        self.debug_var = tk.BooleanVar(value=False)
        self.cb_debug = tk.Checkbutton(
            opts, text="Debug mode (verbose logging)",
            variable=self.debug_var,
        )

        controls = ttk.Frame(self)
        controls.pack(fill=tk.X, pady=10)
        self.run_button = tk.Button(
            controls, text="Run Network Audit", command=self.run_audit,
        )
        self.run_button.pack(side=tk.RIGHT, padx=5)
        self.status_label = tk.Label(controls, text="Status: Ready", anchor="w")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # Populate credentials / hints / output name / optional toggles for the
        # default network type (Ciena RLS).
        self._on_type_change()

    def _current_type(self) -> str:
        """'psi' or 'rls' from the Network Type dropdown."""
        return "psi" if self.network_type_var.get().startswith("Nokia") else "rls"

    def _defaults_for_type(self, ntype: str):
        """Return (username, password, cred_hint, seed_hint) for the type."""
        try:
            from utils.credentials import get_default_credential_for_vendor
        except Exception:
            get_default_credential_for_vendor = None

        def _lookup(vendor, fallback):
            if get_default_credential_for_vendor:
                try:
                    return get_default_credential_for_vendor(vendor) or fallback
                except Exception:
                    return fallback
            return fallback

        if ntype == "psi":
            user, pw = _lookup("nokia", ("admin", "admin"))
            cred_hint = (
                "(Nokia PSI SSH default: 'admin' / 'admin' --"
                " override only if your network has rotated)"
            )
            seed_hint = (
                "(one node — the audit auto-discovers the line via network-map + OSC)"
            )
        else:
            user, pw = _lookup("ciena-rls-rest", ("diaguser", "Ciena123"))
            cred_hint = (
                "(RLS RESTCONF default: 'diaguser' / 'Ciena123' --"
                " override only if your network has rotated)"
            )
            seed_hint = "(one node — the audit auto-discovers the rest via RESTCONF)"
        return user, pw, cred_hint, seed_hint

    def _default_output_path(self, ntype: str) -> str:
        prefix = "PSI_Audit" if ntype == "psi" else "RLS_Audit"
        return os.path.join(
            _resolve_default_output_dir(),
            f"{prefix}_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.xlsx",
        )

    def _on_type_change(self, *_event) -> None:
        """Reshape credentials, hints, output name and the optional-collection
        section when the network type changes (also called once at build)."""
        ntype = self._current_type()
        user, pw, cred_hint, seed_hint = self._defaults_for_type(ntype)
        self.username_entry.delete(0, tk.END)
        self.username_entry.insert(0, user)
        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, pw)
        self.cred_hint_label.config(text=cred_hint)
        self.seed_hint_label.config(text=seed_hint)

        # Regenerate the default output filename unless the operator has set a
        # custom path (i.e. it no longer matches the auto-generated pattern).
        cur = self.output_entry.get().strip()
        if not cur or re.match(r"^(RLS|PSI)_Audit_.*\.xlsx$", os.path.basename(cur)):
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, self._default_output_path(ntype))

        # Optional Data Collection — repack per type (RLS: alarm toggles; PSI:
        # info note). Debug is always last so it stays at the bottom.
        for w in (self.cb_alarms, self.cb_history, self.psi_opts_note, self.cb_debug):
            w.pack_forget()
        if ntype == "psi":
            self.psi_opts_note.pack(anchor="w", padx=10, pady=2)
        else:
            self.cb_alarms.pack(anchor="w", padx=10, pady=2)
            self.cb_history.pack(anchor="w", padx=10, pady=2)
        self.cb_debug.pack(anchor="w", padx=10, pady=2)

    def _browse_output(self) -> None:
        current = self.output_entry.get().strip()
        # Open the dialog in the same directory the entry currently
        # shows, falling back to the prefs/Desktop resolver -- avoids
        # opening at "(My Documents)" when the user has explicitly
        # picked another folder during this session.
        initial_dir = os.path.dirname(current) if current else ""
        if not initial_dir or not os.path.isdir(initial_dir):
            initial_dir = _resolve_default_output_dir()
        path = filedialog.asksaveasfilename(
            title="Save audit report as",
            defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
            initialdir=initial_dir,
            initialfile=os.path.basename(current or "RLS_Audit.xlsx"),
        )
        if path:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, path)

    def run_audit(self) -> None:
        if self._running:
            return
        seed = self.seed_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        output_path = self.output_entry.get().strip()
        capture_alarms = self.capture_alarms_var.get()
        capture_history = self.capture_history_var.get()
        ntype = self._current_type()
        debug = self.debug_var.get()

        if not seed:
            messagebox.showerror("Input Error", "Seed IP / hostname is required.")
            return
        if not _is_valid_host(seed):
            messagebox.showerror("Input Error", f"Invalid seed: {seed!r}")
            return
        if not username:
            messagebox.showerror("Input Error", "Username is required.")
            return
        if not password:
            messagebox.showerror("Input Error", "Password is required.")
            return
        if not output_path:
            messagebox.showerror("Input Error", "Output file path is required.")
            return
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(
                    "Input Error",
                    f"Could not create output directory:\n{out_dir}\n\n{exc}",
                )
                return

        self._running = True
        self.run_button.config(state=tk.DISABLED)
        self.status_label.config(text="Status: Discovering…")
        label = "Nokia PSI" if ntype == "psi" else "Ciena RLS"
        controller_activity = getattr(self.controller, "log_activity", None)

        def activity(message: str, level: int = logging.INFO) -> None:
            if callable(controller_activity):
                controller_activity(message, level)
            else:
                logging.log(level, message)

        feature_summary = (
            ""
            if ntype == "psi"
            else (
                f", alarms={'on' if capture_alarms else 'off'}, "
                f"history={'on' if capture_history else 'off'}"
            )
        )
        activity(
            f"[NETWORK AUDIT] Starting {label}; seed={seed}, "
            f"output={output_path}, debug={'on' if debug else 'off'}"
            f"{feature_summary}."
        )

        root = self.controller.root

        def _log_to_panel(msg: str) -> None:
            """Persist PSI progress; RLS already tees every line to logging."""

            if ntype == "psi":
                activity(f"[NETWORK AUDIT] {msg}")

        def _worker() -> None:
            try:
                if ntype == "psi":
                    # PSI audit runs in-process (paramiko SSH, no console) and
                    # streams to the panel via log_callback -- same model as RLS.
                    from scripts.Network.Nokia_PSI_Audit import run_audit as _psi_run_audit
                    _psi_run_audit(
                        seed_host=seed,
                        username=username,
                        password=password,
                        output_path=output_path,
                        debug=debug,
                        log_callback=_log_to_panel,
                    )
                else:
                    from scripts.Network.RLS_Audit import run_audit
                    run_audit(
                        seed_host=seed,
                        username=username,
                        password=password,
                        output_path=output_path,
                        capture_alarms=capture_alarms,
                        capture_alarm_history=capture_history,
                        debug=debug,
                        log_callback=_log_to_panel,
                    )
                scrub_password_widget(self.password_entry)
                # Remember the directory the operator just used so the
                # next audit defaults to it. Only persisted on success
                # -- a failed run shouldn't move the default away from
                # somewhere the operator was already using.
                try:
                    chosen_dir = os.path.dirname(output_path)
                    if chosen_dir and os.path.isdir(chosen_dir):
                        prefs = load_user_prefs()
                        prefs[_PREFS_KEY_AUDIT_DIR] = chosen_dir
                        save_user_prefs(prefs)
                except Exception:
                    logging.exception(
                        "Could not persist last network-audit output dir"
                    )

                def on_complete() -> None:
                    self.run_button.config(state=tk.NORMAL)
                    self.status_label.config(text="Status: Ready")
                    self._running = False
                    activity(
                        f"[NETWORK AUDIT] Audit complete; saved to {output_path}."
                    )
                    messagebox.showinfo(
                        "Network Audit Complete",
                        f"Audit finished.\n\nReport: {output_path}",
                    )

                root.after(0, on_complete)
            except RuntimeError as exc:
                # ``_audit_abort`` raises RuntimeError when the audit
                # decides it can't proceed (bad DNS, no nodes reachable,
                # etc.). Surface the message clearly. Capture ``exc`` to
                # a separate name -- Python deletes ``exc`` at the end
                # of the except block (PEP 3110), but the nested
                # callback runs later via ``root.after``.
                abort_msg = str(exc)

                def on_audit_abort() -> None:
                    self.run_button.config(state=tk.NORMAL)
                    self.status_label.config(text="Status: Aborted")
                    self._running = False
                    activity(
                        f"[NETWORK AUDIT] Aborted: {abort_msg}",
                        logging.WARNING,
                    )
                    messagebox.showerror("Network Audit Aborted", abort_msg)

                root.after(0, on_audit_abort)
            except Exception as exc:
                logging.exception("RLS Network Audit unexpected error")
                # Same PEP 3110 hazard as above -- bind the message
                # string in this scope, not the exception object itself,
                # so the deferred callback can still read it.
                err_msg = friendly_error(exc)

                def on_error() -> None:
                    self.run_button.config(state=tk.NORMAL)
                    self.status_label.config(text="Status: Error")
                    self._running = False
                    activity(
                        f"[NETWORK AUDIT] Failed: {err_msg}", logging.ERROR
                    )
                    messagebox.showerror("Network Audit Error", err_msg)

                root.after(0, on_error)

        threading.Thread(target=_worker, daemon=True).start()
