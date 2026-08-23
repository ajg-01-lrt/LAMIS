"""Provisioning container with live provisioning and the RLS Route Builder."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from gui.provision_frame import ProvisionFrame
from gui.rls_route_frame import RlsRouteFrame


class ProvisioningFrame(ttk.Frame):
    """Host live provisioning and the unified offline RLS route workflow."""

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self._sub_mode = tk.StringVar(value="live")
        self._build()

    def _build(self) -> None:
        sub_bar = ttk.LabelFrame(self, text="Provisioning Mode")
        sub_bar.pack(fill=tk.X, padx=5, pady=(0, 5))
        tk.Radiobutton(
            sub_bar,
            text="Live Device Provisioning",
            variable=self._sub_mode,
            value="live",
            command=self._switch,
        ).pack(side=tk.LEFT, padx=10, pady=3)
        tk.Radiobutton(
            sub_bar,
            text="Ciena RLS Route Builder",
            variable=self._sub_mode,
            value="rls_route",
            command=self._switch,
        ).pack(side=tk.LEFT, padx=10, pady=3)

        self._content = ttk.Frame(self)
        self._content.pack(fill=tk.BOTH, expand=True)
        self.live_child = ProvisionFrame(self._content, self.controller)
        self.rls_route_child = RlsRouteFrame(self._content, self.controller)
        self._switch()

    def _switch(self) -> None:
        self.live_child.pack_forget()
        self.rls_route_child.pack_forget()
        mode = self._sub_mode.get()
        if mode == "rls_route":
            self.rls_route_child.pack(fill=tk.BOTH, expand=True)
        else:
            self.live_child.pack(fill=tk.BOTH, expand=True)
        labels = {
            "live": "Live Device Provisioning",
            "rls_route": "Ciena RLS Route Builder",
        }
        logging.info(
            "[PROVISIONING] Mode selected: %s", labels.get(mode, mode)
        )
