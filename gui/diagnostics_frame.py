"""Diagnostics container — owns the TDS vs Network Audit sub-mode toggle.

Replaces the bare TDSFrame in gui4_0.py so that selecting "Diagnostics" from
the top mode bar reveals two sub-modes:

    * TDS           — single-host diagnostics (existing TDSFrame)
    * Network Audit — LLDP-driven walk across the RLS network
"""
import tkinter as tk
from tkinter import ttk

from gui.tds_frame import TDSFrame
from gui.network_audit_frame import NetworkAuditFrame


class DiagnosticsFrame(ttk.Frame):
    """Container with a sub-mode radio bar and two child frames."""

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self._sub_mode = tk.StringVar(value="tds")
        self._build()

    def _build(self) -> None:
        sub_bar = ttk.LabelFrame(self, text="Diagnostics Mode")
        sub_bar.pack(fill=tk.X, padx=5, pady=(0, 5))

        tk.Radiobutton(
            sub_bar, text="TDS", variable=self._sub_mode, value="tds",
            command=self._switch,
        ).pack(side=tk.LEFT, padx=10, pady=3)
        tk.Radiobutton(
            sub_bar, text="Network Audit", variable=self._sub_mode, value="network_audit",
            command=self._switch,
        ).pack(side=tk.LEFT, padx=10, pady=3)

        self._sub_container = ttk.Frame(self)
        self._sub_container.pack(fill=tk.BOTH, expand=True)

        self.tds_child = TDSFrame(self._sub_container, controller=self.controller)
        self.network_audit_child = NetworkAuditFrame(self._sub_container, controller=self.controller)

        self._switch()

    def _switch(self) -> None:
        self.tds_child.pack_forget()
        self.network_audit_child.pack_forget()
        if self._sub_mode.get() == "network_audit":
            self.network_audit_child.pack(fill=tk.BOTH, expand=True)
        else:
            self.tds_child.pack(fill=tk.BOTH, expand=True)
