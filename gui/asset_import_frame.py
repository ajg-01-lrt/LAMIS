"""File Processing → Asset Import mode.

Operator picks an inventory workbook + an ASN-style asset doc, clicks
Run; ATLAS matches serials between the two and stamps every device
tab's Asset Tag (column G) + Customer PO (cell C7). Backs up the
inventory workbook to ``<path>.bak_<ts>.xlsx`` before mutating so a
mis-typed asset doc can be undone.
"""
from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import openpyxl

from utils.asset_import import (
    apply_asset_data_to_inventory,
    parse_asset_doc,
)


class AssetImportFrame(ttk.Frame):
    """UI for the Asset Import flow."""

    def __init__(self, parent: tk.Widget, gui: Any) -> None:
        super().__init__(parent)
        self.gui = gui
        self._inventory_path: Optional[str] = None
        self._asset_path: Optional[str] = None
        self._running = False
        self._build()

    # ── UI ─────────────────────────────────────────────────────────────

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        inv_frame = ttk.LabelFrame(
            self, text="Inventory Workbook (.xlsx — gets MUTATED)"
        )
        inv_frame.pack(fill=tk.X, **pad)
        self._inv_label = tk.StringVar(value="No inventory workbook selected")
        ttk.Label(
            inv_frame, textvariable=self._inv_label, width=80, anchor="w",
        ).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(
            inv_frame, text="Browse…", command=self._browse_inventory,
        ).pack(side=tk.LEFT, padx=6, pady=6)

        asn_frame = ttk.LabelFrame(
            self, text="Asset Doc (.xlsx — PO / Serial # / Asset columns)"
        )
        asn_frame.pack(fill=tk.X, **pad)
        self._asn_label = tk.StringVar(value="No asset doc selected")
        ttk.Label(
            asn_frame, textvariable=self._asn_label, width=80, anchor="w",
        ).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(
            asn_frame, text="Browse…", command=self._browse_asset_doc,
        ).pack(side=tk.LEFT, padx=6, pady=6)

        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill=tk.X, **pad)
        self._run_btn = ttk.Button(
            ctrl_frame, text="Apply Asset Tags", command=self._on_run,
        )
        self._run_btn.pack(side=tk.LEFT, padx=6)
        self._status_var = tk.StringVar(
            value="Ready — pick the inventory + asset doc and click Apply Asset Tags"
        )
        ttk.Label(
            ctrl_frame, textvariable=self._status_var, foreground="gray",
        ).pack(side=tk.LEFT, padx=12)

        log_frame = ttk.LabelFrame(self, text="Import Log")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", wrap="word",
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ── Helpers ───────────────────────────────────────────────────────

    def _append_log(self, msg: str) -> None:
        if msg.strip():
            logging.info("[ASSET IMPORT] %s", msg.rstrip())

        def _do():
            self._log.configure(state="normal")
            self._log.insert(tk.END, msg + "\n")
            self._log.see(tk.END)
            self._log.configure(state="disabled")
        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _set_status(self, msg: str) -> None:
        try:
            self.after(0, lambda: self._status_var.set(msg))
        except Exception:
            self._status_var.set(msg)

    # ── File pickers ──────────────────────────────────────────────────

    def _browse_inventory(self) -> None:
        path = filedialog.askopenfilename(
            title="Select inventory workbook to update",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._inventory_path = path
        self._inv_label.set(path)

    def _browse_asset_doc(self) -> None:
        path = filedialog.askopenfilename(
            title="Select asset doc (PO / Serial # / Asset)",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._asset_path = path
        self._asn_label.set(path)

    # ── Run ───────────────────────────────────────────────────────────

    def _on_run(self) -> None:
        if self._running:
            return
        if not self._inventory_path:
            messagebox.showerror("Asset Import", "Pick an inventory workbook first.")
            return
        if not self._asset_path:
            messagebox.showerror("Asset Import", "Pick an asset doc first.")
            return
        self._running = True
        self._run_btn.configure(state="disabled")
        self._set_status("Running…")
        # Run on a worker so the UI stays responsive while openpyxl
        # parses + mutates the workbook.
        threading.Thread(
            target=self._run_blocking, daemon=True,
        ).start()

    def _run_blocking(self) -> None:
        inv_path = Path(self._inventory_path)
        asn_path = Path(self._asset_path)
        self._append_log(f"Inventory: {inv_path}")
        self._append_log(f"Asset doc: {asn_path}")

        try:
            asset_map = parse_asset_doc(str(asn_path))
        except Exception as exc:
            logging.exception("Asset Import: failed to parse asset doc")
            self._append_log(f"[ERROR] Failed to parse asset doc: {exc}")
            self._finish()
            messagebox.showerror("Asset Import", str(exc))
            return
        self._append_log(f"Parsed {len(asset_map)} serial(s) from asset doc")

        # Backup the inventory workbook BEFORE we mutate it. Same
        # naming convention as the one-shot helper tools.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = inv_path.with_name(f"{inv_path.stem}.bak_{ts}{inv_path.suffix}")
        try:
            shutil.copy2(inv_path, bak)
            self._append_log(f"Backup: {bak.name}")
        except Exception as exc:
            self._append_log(f"[ERROR] Could not write backup: {exc}")
            self._finish()
            messagebox.showerror("Asset Import", f"Backup failed: {exc}")
            return

        try:
            wb = openpyxl.load_workbook(str(inv_path))
        except Exception as exc:
            self._append_log(f"[ERROR] Could not open inventory workbook: {exc}")
            self._finish()
            messagebox.showerror("Asset Import", f"Open failed: {exc}")
            return

        try:
            result = apply_asset_data_to_inventory(wb, asset_map)
            wb.save(str(inv_path))
        except Exception as exc:
            logging.exception("Asset Import: merge failed")
            self._append_log(f"[ERROR] Merge failed: {exc}")
            self._finish()
            messagebox.showerror("Asset Import", str(exc))
            return

        # Surface the merge stats.
        self._append_log("")
        self._append_log(f"Tabs touched           : {result.tabs_touched}")
        self._append_log(f"Rows matched (G written): {result.rows_matched}")
        self._append_log(
            f"Inventory serials not in asset doc: "
            f"{result.serials_not_in_asset_doc}"
        )
        if result.tabs_with_no_matches:
            self._append_log("")
            self._append_log(
                f"Tabs with zero matches ({len(result.tabs_with_no_matches)}):"
            )
            for t in result.tabs_with_no_matches[:20]:
                self._append_log(f"  - {t}")
            if len(result.tabs_with_no_matches) > 20:
                self._append_log(
                    f"  ...and {len(result.tabs_with_no_matches) - 20} more"
                )
        if result.po_conflicts:
            self._append_log("")
            self._append_log(
                f"PO conflicts (C7 left untouched on {len(result.po_conflicts)} tab(s)):"
            )
            for tab, pos in result.po_conflicts:
                self._append_log(f"  - {tab}: saw {pos!r}")
        self._append_log("")
        self._append_log(f"Saved: {inv_path}")
        self._set_status(
            f"Done — {result.rows_matched} row(s) updated across "
            f"{result.tabs_touched} tab(s)"
        )
        self._finish()

    def _finish(self) -> None:
        self._running = False
        try:
            self.after(0, lambda: self._run_btn.configure(state="normal"))
        except Exception:
            pass
