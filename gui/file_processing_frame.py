"""
gui/file_processing_frame.py - "File Processing" mode container.

Hosts the file-processing sub-modes toggled by radio buttons:

* Raw Inventory  — process manually captured CLI output files (RawFrame).
* BoM            — add or refresh a Bill of Materials sheet on an inventory
                   workbook (BomFrame).
* BoM Comparison — diff a Live BoM against a Sales BoM (BomCompareFrame).
* Asset Import   — match an ASN-style asset doc against an inventory
                   workbook, stamping Asset Tag (G) and Customer PO (C7)
                   onto every device tab whose serials match
                   (AssetImportFrame).
"""
import logging
from typing import Any

import tkinter as tk
from tkinter import ttk

from gui.raw_frame import RawFrame
from gui.bom_frame import BomFrame
from gui.bom_compare_frame import BomCompareFrame
from gui.asset_import_frame import AssetImportFrame


class FileProcessingFrame(ttk.Frame):
    """Container for file-processing sub-modes (radio-button toggle)."""

    def __init__(self, parent: tk.Widget, gui: Any) -> None:
        super().__init__(parent)
        self.gui = gui

        self._sub_mode = tk.StringVar(value="raw_inventory")

        sub_frame = ttk.LabelFrame(self, text="File Processing Mode")
        sub_frame.pack(fill=tk.X, padx=5, pady=5)
        tk.Radiobutton(
            sub_frame, text="Raw Inventory", variable=self._sub_mode,
            value="raw_inventory", command=self._switch,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            sub_frame, text="BoM", variable=self._sub_mode,
            value="bom", command=self._switch,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            sub_frame, text="BoM Comparison", variable=self._sub_mode,
            value="bom_compare", command=self._switch,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            sub_frame, text="Asset Import", variable=self._sub_mode,
            value="asset_import", command=self._switch,
        ).pack(side=tk.LEFT, padx=10)

        self._content = ttk.Frame(self)
        self._content.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.raw_frame = RawFrame(self._content, gui)
        self.bom_frame = BomFrame(self._content, gui)
        self.bom_compare_frame = BomCompareFrame(self._content, gui)
        self.asset_import_frame = AssetImportFrame(self._content, gui)

        self._switch()

    def _switch(self) -> None:
        self.raw_frame.pack_forget()
        self.bom_frame.pack_forget()
        self.bom_compare_frame.pack_forget()
        self.asset_import_frame.pack_forget()
        mode = self._sub_mode.get()
        if mode == "raw_inventory":
            self.raw_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "bom":
            self.bom_frame.pack(fill=tk.BOTH, expand=True)
        elif mode == "asset_import":
            self.asset_import_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.bom_compare_frame.pack(fill=tk.BOTH, expand=True)
        labels = {
            "raw_inventory": "Raw Inventory",
            "bom": "BoM",
            "bom_compare": "BoM Comparison",
            "asset_import": "Asset Import",
        }
        logging.info(
            "[FILE PROCESSING] Mode selected: %s", labels.get(mode, mode)
        )
