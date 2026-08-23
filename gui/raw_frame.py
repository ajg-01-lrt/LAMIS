"""
gui/raw_frame.py - "Raw" mode: process manually captured CLI output files.

Supported input formats
-----------------------
* **.txt** — single device, raw CLI transcript in plain text.
* **.xlsx / .xls** — one *or* many devices:
  - Single sheet  → treated as one device (sheet name used as Device ID).
  - Multi-sheet   → each sheet is a separate device; all results are merged
                    into a single Device Report workbook.

In each case ATLAS splits the transcript by command boundaries, feeds the
sections into the normal process_outputs() pipeline, and exports a Device
Report using the same templates as a live network scan.
"""
import importlib
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

AUTO_DETECT_NOKIA = "Auto Detect Nokia"
# Sentinel for the Sales BoM Import flow — not a CLI parser, so its
# value is a non-module marker. _worker special-cases this value to
# dispatch the Sales-BoM-to-packing-slip pipeline instead of the
# CLI-transcript parsing path.
SALES_BOM_IMPORT = "Sales BoM"
_SALES_BOM_MARKER = "__sales_bom_import__"

# Display name → module path for the script dropdown
SCRIPT_OPTIONS: Dict[str, str] = {
    AUTO_DETECT_NOKIA: "",
    "Nokia PSI":     "scripts.Nokia_PSI",
    "Nokia PSS":     "scripts.Nokia_1830",
    "Nokia SAR":     "scripts.Nokia_SAR_Raw",
    "Nokia IXR":     "scripts.Nokia_IXR_Raw",
    "Ciena 6500":    "scripts.Ciena_6500",
    "Ciena RLS":     "scripts.Ciena_RLS",
    SALES_BOM_IMPORT: _SALES_BOM_MARKER,
}

# Map module path → workbook-builder family key
_FAMILY_BY_MODULE: Dict[str, str] = {
    "scripts.Nokia_PSI": "psi",
    "scripts.Ciena_RLS": "rls",
}


def _split_raw_output_by_commands(raw_text: str, commands: List[str]) -> List[str]:
    """Split a raw CLI transcript into per-command output sections.

    The transcript may optionally have hostname prompts on command lines,
    e.g.::

        USDEN5-L9O1# show shelf 1
        ...output...
        USDEN5-L9O1# show card inventory *
        ...output...

    or bare lines like ``show shelf 1``.

    Returns a list with one entry per command (parallel to *commands*).
    An empty string is returned for any command not found in the transcript.
    Output lines that appear *before* the first recognised command (e.g. a
    login banner) are discarded.
    """
    lines = raw_text.splitlines()
    prompt_re = re.compile(r"^[^\#]*#\s*")

    # Find the first line index where each command appears.
    cmd_line: Dict[int, int] = {}  # command_index -> line_index

    # Two-pass match: prefer prompt-prefixed command lines (``HOSTNAME# cmd``)
    # over bare echoes (``cmd``). When a user paste-bombs several commands at
    # once, the device echoes the buffered input as bare lines BEFORE the
    # previous command's output, then prints the real prompt+command later.
    # Matching bare echoes first scrambles section boundaries; the
    # prompt-prefixed line is the authoritative start. We still fall through
    # to bare matching in pass 2 so hand-crafted transcripts without
    # prompts continue to work.
    for require_prompt in (True, False):
        for line_idx, line in enumerate(lines):
            if require_prompt and not prompt_re.match(line):
                continue
            candidate = prompt_re.sub("", line).strip()
            for cmd_idx, cmd in enumerate(commands):
                if cmd_idx not in cmd_line and candidate.startswith(cmd):
                    cmd_line[cmd_idx] = line_idx
                    break  # a line can only start one command

    sorted_positions = sorted(cmd_line.items(), key=lambda x: x[1])
    result = [""] * len(commands)

    for i, (cmd_idx, start_line) in enumerate(sorted_positions):
        # Output is everything *after* the command line up to the next command.
        end_line = (
            sorted_positions[i + 1][1] if i + 1 < len(sorted_positions) else len(lines)
        )
        result[cmd_idx] = "\n".join(lines[start_line + 1 : end_line])

    return result


def _read_excel_sheets(file_path: str) -> Dict[str, str]:
    """Read an Excel workbook and return ``{sheet_name: raw_text}`` for every sheet.

    Each sheet is expected to have the CLI transcript in column A, one line
    per row (this is the format produced by Nokia's CPAM / manual capture
    tools).  Blank rows are preserved as empty lines so command-boundary
    detection still works.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl is required to read Excel files.") from exc

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheets: Dict[str, str] = {}
    for name in wb.sheetnames:
        ws = wb[name]
        lines = []
        for row in ws.iter_rows(values_only=True):
            cell_val = row[0] if row else None
            lines.append(str(cell_val) if cell_val is not None else "")
        sheets[name] = "\n".join(lines)
    wb.close()
    return sheets


def _read_text_folder(folder_path: str) -> Dict[str, str]:
    """Read every .txt file in a folder tree and return ``{device_id: raw_text}``."""
    devices: Dict[str, str] = {}

    for txt_path in sorted(Path(folder_path).rglob("*")):
        if not txt_path.is_file() or txt_path.suffix.lower() != ".txt":
            continue
        device_id = _normalize_device_id(txt_path.stem)
        unique_id = device_id
        counter = 2
        while unique_id in devices:
            unique_id = f"{device_id}_{counter:02d}"
            counter += 1
        devices[unique_id] = txt_path.read_text(encoding="utf-8", errors="replace")

    return devices


def _normalize_device_id(value: str) -> str:
    """Normalize site/device names from raw imports.

    Strips common capture timestamp prefixes such as:
      11-27-2023 - 20.33 ET - SITE-NAME
    """
    text = (value or "").strip()

    # Remove one leading timestamp block if present.
    text = re.sub(
        r"^\s*\d{1,2}[-_/]\d{1,2}[-_/]\d{2,4}\s*-\s*"
        r"\d{1,2}[\.:]\d{2}(?:\s*[AP]M)?(?:\s*[A-Z]{2,5})?\s*-\s*",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()

    # Avoid empty IDs after cleanup.
    return text or (value or "").strip() or "Manual"


def _normalize_device_map(devices: Dict[str, str]) -> Dict[str, str]:
    """Normalize and de-duplicate device keys for multi-device inputs."""
    normalized: Dict[str, str] = {}
    for raw_name, raw_text in devices.items():
        base = _normalize_device_id(raw_name)
        key = base
        counter = 2
        while key in normalized:
            key = f"{base}_{counter:02d}"
            counter += 1
        normalized[key] = raw_text
    return normalized


def _detect_nokia_raw_script(raw_text: str, device_id: str = "") -> Optional[str]:
    """Best-effort detect Nokia family for raw transcripts."""
    haystack = f"{device_id}\n{raw_text}"

    if re.search(r"(?<![0-9A-Za-z])(7250|ixr(?:-r6d?)?)(?![0-9A-Za-z])", haystack, re.IGNORECASE):
        return "scripts.Nokia_IXR_Raw"
    if re.search(r"(?<![0-9A-Za-z])(7705|sar(?:-8)?)(?![0-9A-Za-z])", haystack, re.IGNORECASE):
        return "scripts.Nokia_SAR_Raw"
    if re.search(r"(?<![0-9A-Za-z])(1830|nokia\s*1830)(?![0-9A-Za-z])", haystack, re.IGNORECASE):
        return "scripts.Nokia_1830"
    # Matches bare "psi" / "nokia" or the 4L / 8L PSI hardware variants.
    if re.search(r"(?<![0-9A-Za-z])(psi|nokia(?:-[48]l)?)(?![0-9A-Za-z])", haystack, re.IGNORECASE):
        return "scripts.Nokia_PSI"

    has_mda_detail = re.search(r"^MDA\s+\d+/\d+\s+detail", raw_text, re.IGNORECASE | re.MULTILINE)
    has_chassis_detail = re.search(r"^\s*Chassis\s+1\s+Detail", raw_text, re.IGNORECASE | re.MULTILINE)
    if has_mda_detail and has_chassis_detail:
        # Favor IXR when the prompt/file name is absent and only the matched-output
        # capture remains; IXR and SAR raw formats are otherwise very similar.
        return "scripts.Nokia_IXR_Raw"

    return None


def _resolve_script_module(raw_text: str, device_id: str, script_name: str) -> str:
    """Resolve the parser module for a raw transcript."""
    if script_name == AUTO_DETECT_NOKIA:
        detected = _detect_nokia_raw_script(raw_text, device_id)
        if not detected:
            raise RuntimeError(
                f"Could not auto-detect Nokia family for {device_id!r}. "
                "Select Nokia SAR or Nokia IXR explicitly."
            )
        return detected

    return SCRIPT_OPTIONS[script_name]


class RawFrame(ttk.Frame):
    """UI panel for the 'Raw' mode."""

    def __init__(self, parent: tk.Widget, gui: Any) -> None:
        super().__init__(parent)
        self.gui = gui  # reference to InventoryGUI instance
        self._input_path: Optional[str] = None
        self._running = False
        self._setup_ui()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        pad: Dict[str, int] = {"padx": 8, "pady": 4}

        # ── File upload row ──────────────────────────────────────────
        file_frame = ttk.LabelFrame(
            self,
            text="Input Source  (.txt file, .xlsx/.xls workbook, or folder of .txt files)",
        )
        file_frame.pack(fill=tk.X, **pad)

        self._file_label = tk.StringVar(value="No file or folder selected")
        ttk.Label(
            file_frame, textvariable=self._file_label, width=65, anchor="w"
        ).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(
            file_frame, text="Browse File…", command=self._browse_file
        ).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(
            file_frame, text="Browse Folder…", command=self._browse_folder
        ).pack(side=tk.LEFT, padx=6, pady=6)

        # ── Script + device label row ────────────────────────────────
        cfg_frame = ttk.LabelFrame(self, text="Processing Options")
        cfg_frame.pack(fill=tk.X, **pad)

        ttk.Label(cfg_frame, text="Device Type:").grid(
            row=0, column=0, padx=(8, 4), pady=6, sticky="w"
        )
        self._script_var = tk.StringVar(value=AUTO_DETECT_NOKIA)
        script_cb = ttk.Combobox(
            cfg_frame,
            textvariable=self._script_var,
            values=list(SCRIPT_OPTIONS.keys()),
            state="readonly",
            width=18,
        )
        script_cb.grid(row=0, column=1, padx=4, pady=6, sticky="w")

        ttk.Label(
            cfg_frame,
            text="Device ID  (single file only — ignored for multi-device workbook/folder):",
        ).grid(row=0, column=2, padx=(24, 4), pady=6, sticky="w")
        self._device_id_var = tk.StringVar()
        ttk.Entry(cfg_frame, textvariable=self._device_id_var, width=24).grid(
            row=0, column=3, padx=4, pady=6, sticky="w"
        )

        # ── Sheet summary label (populated after Excel load) ─────────
        self._sheet_info_var = tk.StringVar(value="")
        ttk.Label(cfg_frame, textvariable=self._sheet_info_var, foreground="steelblue").grid(
            row=1, column=0, columnspan=4, padx=8, pady=(0, 4), sticky="w"
        )

        ttk.Label(
            cfg_frame,
            text="Optic PART TYPE in report uses: Description > Speed > Optic",
            foreground="gray40",
        ).grid(row=2, column=0, columnspan=4, padx=8, pady=(0, 6), sticky="w")

        # ── Control bar ──────────────────────────────────────────────
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill=tk.X, **pad)

        self._run_btn = ttk.Button(
            ctrl_frame, text="Process Input", command=self._on_run
        )
        self._run_btn.pack(side=tk.LEFT, padx=6)

        self._status_var = tk.StringVar(value="Ready — select a file or folder and click Process Input")
        ttk.Label(ctrl_frame, textvariable=self._status_var, foreground="gray").pack(
            side=tk.LEFT, padx=12
        )

        # ── Log window ───────────────────────────────────────────────
        log_frame = ttk.LabelFrame(self, text="Processing Log")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", wrap="word"
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select device CLI output file",
            filetypes=[
                ("Supported files", "*.txt *.xlsx *.xls"),
                ("Text files", "*.txt"),
                ("Excel workbooks", "*.xlsx *.xls"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._input_path = path
        ext = Path(path).suffix.lower()
        self._log_clear()
        self._sheet_info_var.set("")

        if ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                names = wb.sheetnames
                wb.close()
                count = len(names)
                preview = ", ".join(names[:6]) + ("…" if count > 6 else "")
                self._file_label.set(f"{Path(path).name}  [{count} sheet(s)]")
                self._sheet_info_var.set(
                    f"Sheets ({count}): {preview}  —  each sheet will be processed as a separate device"
                    if count > 1
                    else f"Single sheet: {names[0]}"
                )
                self._status_var.set(
                    f"Excel file loaded — {count} device(s) detected. Click Process Input."
                )
            except Exception as exc:
                self._file_label.set(f"{Path(path).name}  [⚠ could not read sheets]")
                self._status_var.set(f"Warning: {exc}")
        else:
            self._file_label.set(Path(path).name)
            self._status_var.set("Text file loaded — click Process Input to begin")

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(title="Select folder containing device CLI output files")
        if not path:
            return

        txt_files = [p for p in Path(path).rglob("*") if p.is_file() and p.suffix.lower() == ".txt"]
        self._input_path = path
        self._log_clear()
        self._file_label.set(f"{Path(path).name}  [{len(txt_files)} txt file(s)]")
        self._sheet_info_var.set("")

        if txt_files:
            preview = ", ".join(str(p.relative_to(path)) for p in txt_files[:6]) + ("…" if len(txt_files) > 6 else "")
            self._sheet_info_var.set(
                f"Folder contents ({len(txt_files)}, recursive): {preview}"
            )
            self._status_var.set(
                f"Folder loaded — {len(txt_files)} device file(s) detected. Click Process Input."
            )
        else:
            self._status_var.set("Selected folder has no .txt files.")

    def _on_run(self) -> None:
        if self._running:
            return
        if not self._input_path or not os.path.exists(self._input_path):
            messagebox.showerror("No Input", "Please select an input file or folder first.")
            return
        script_name = self._script_var.get()
        if script_name not in SCRIPT_OPTIONS:
            messagebox.showerror("Invalid Script", f"Unknown script: {script_name!r}")
            return

        device_id = self._device_id_var.get().strip() or Path(self._input_path).stem

        self._run_btn.configure(state="disabled")
        self._running = True
        self._status_var.set("Processing…")
        self._log_clear()

        threading.Thread(
            target=self._worker,
            args=(self._input_path, device_id, script_name),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker(self, file_path: str, device_id: str, script_name: str) -> None:
        """Load a file or folder, detect format, and dispatch processing."""
        try:
            input_path = Path(file_path)

            # Sales BoM Import is a completely different flow from the
            # CLI-transcript path — it parses a Sales BoM workbook and
            # produces a packing-slip-style output. Branch early.
            if script_name == SALES_BOM_IMPORT:
                self._process_sales_bom_import(file_path)
                return

            if input_path.is_dir():
                devices = _read_text_folder(file_path)
                if not devices:
                    raise RuntimeError("Selected folder does not contain any .txt files.")
                self._log_write(
                    f"Folder loaded: {input_path.name}\n"
                    f"Text files found ({len(devices)}): {', '.join(devices.keys())}\n\n"
                )
                self._process_multi(devices, script_name)
                return

            ext = input_path.suffix.lower()

            if ext in (".xlsx", ".xls"):
                sheets = _read_excel_sheets(file_path)
                self._log_write(
                    f"Excel workbook loaded: {input_path.name}\n"
                    f"Sheets found ({len(sheets)}): {', '.join(sheets.keys())}\n\n"
                )
                if len(sheets) == 1:
                    name, text = next(iter(sheets.items()))
                    self._process_single(text, _normalize_device_id(name), script_name)
                else:
                    normalized_sheets = _normalize_device_map(sheets)
                    self._process_multi(normalized_sheets, script_name)
            else:
                self._log_write(f"Reading file: {file_path}\n")
                raw_text = input_path.read_text(encoding="utf-8", errors="replace")
                self._log_write(
                    f"File loaded — {len(raw_text):,} characters, "
                    f"{raw_text.count(chr(10)):,} lines\n\n"
                )
                self._process_single(raw_text, _normalize_device_id(device_id), script_name)

        except Exception as exc:
            logging.exception("Raw processing worker error")
            self._log_write(f"\n⚠  ERROR: {exc}\n")
            self.after(0, self._finish, False)

    def _process_single(self, raw_text: str, device_id: str, script_name: str) -> None:
        """Parse one device's transcript and hand off to export."""
        outputs: Dict[str, Any] = {}
        module_path = _resolve_script_module(raw_text, device_id, script_name)
        logging.info(f"[RAW] Parsing single device {device_id!r} with parser {module_path}")
        ok = self._parse_device(raw_text, device_id, module_path, outputs)
        if not ok:
            self.after(0, self._finish, False)
            return
        # Re-key the single device to "Manual" so the summary IP column is clean.
        rekeyed = {"Manual": outputs.pop(device_id)} if device_id in outputs else outputs
        family = _FAMILY_BY_MODULE.get(module_path, "default")
        self.after(0, self._export, rekeyed, family, device_id)

    def _process_multi(self, sheets: Dict[str, str], script_name: str) -> None:
        """Parse every sheet as its own device, merge results, then export."""
        raw_outputs: Dict[str, Any] = {}
        success_count = 0
        # Track per-sheet resolved modules so we can pick the right
        # workbook-builder family below. Using the raw dropdown value
        # would always return "default" for Auto Detect Nokia (its
        # SCRIPT_OPTIONS value is "").
        resolved_modules: List[str] = []

        for sheet_name, raw_text in sheets.items():
            self._log_write(f"{'─'*50}\n")
            self._log_write(f"Processing sheet: {sheet_name!r}\n")
            try:
                module_path = _resolve_script_module(raw_text, sheet_name, script_name)
            except Exception as exc:
                self._log_write(f"  ⚠  Skipping {sheet_name!r} — {exc}\n")
                logging.warning(
                    f"[RAW] Skipping {sheet_name!r}: parser resolution failed: {exc}"
                )
                continue
            ok = self._parse_device(raw_text, sheet_name, module_path, raw_outputs)
            if ok:
                success_count += 1
                resolved_modules.append(module_path)

        self._log_write(f"\n{'═'*50}\n")
        self._log_write(
            f"Multi-sheet complete: {success_count}/{len(sheets)} device(s) parsed successfully.\n"
        )
        logging.info(
            f"[RAW] Multi-sheet parse complete: {success_count}/{len(sheets)} "
            "devices parsed into export payload"
        )

        if success_count == 0:
            self._log_write("⚠  No devices produced data. Aborting export.\n")
            logging.warning("[RAW] Export aborted: no devices produced data")
            self.after(0, self._finish, False)
            return

        # Re-key every device as "Manual" / "Manual_02" / "Manual_03" … so the
        # summary IP column shows "Manual" instead of the node IP address.
        # Zero-pad numbers so lexicographic sort matches numeric order.
        merged_outputs: Dict[str, Any] = {}
        n = len(raw_outputs)
        width = len(str(n)) if n > 1 else 1
        for i, (device_id, data) in enumerate(raw_outputs.items(), start=1):
            key = "Manual" if i == 1 else f"Manual_{str(i).zfill(width)}"
            merged_outputs[key] = data

        # Derive family from the resolved per-sheet modules — picks the
        # first non-default family if there is one, else falls back to
        # "default". Mixed-family inputs (rare) land on "default" too.
        families = {_FAMILY_BY_MODULE.get(m, "default") for m in resolved_modules}
        non_default = families - {"default"}
        family = next(iter(non_default)) if len(non_default) == 1 else "default"
        label = Path(self._input_path).stem if self._input_path else "MultiDevice"
        self.after(0, self._export, merged_outputs, family, label)

    # ------------------------------------------------------------------
    # Sales BoM Import path — completely separate from CLI-transcript
    # processing. Parses a Sales BoM xlsx, prompts the user to pick a
    # worksheet, then generates a packing-slip-style workbook via
    # WorkbookBuilder.build_sales_bom_packing_slip_workbook.
    # ------------------------------------------------------------------

    def _process_sales_bom_import(self, source_path: str) -> None:
        """Worker entrypoint for the Sales BoM Import option.

        Logs every step under the ``[SalesBoM-UI]`` prefix so the
        operator can correlate UI events with the workbook builder's
        ``[SalesBoM]`` log lines when troubleshooting.
        """
        logging.info(
            f"[SalesBoM-UI] _process_sales_bom_import start: source={source_path!r}"
        )
        try:
            import openpyxl
        except Exception as exc:
            logging.exception("[SalesBoM-UI] openpyxl import failed")
            self._log_write(f"openpyxl unavailable: {exc}\n")
            self.after(0, self._finish, False)
            return

        ext = Path(source_path).suffix.lower()
        if ext not in (".xlsx", ".xls"):
            logging.error(
                f"[SalesBoM-UI] Unsupported extension {ext!r} for {source_path!r}"
            )
            self._log_write(
                "Sales BoM Import expects an .xlsx / .xls file; "
                f"got {ext!r}.\n"
            )
            self.after(0, self._finish, False)
            return

        try:
            src_size = os.path.getsize(source_path)
            logging.debug(
                f"[SalesBoM-UI] Source file size: {src_size:,} bytes"
            )
        except OSError as exc:
            logging.error(f"[SalesBoM-UI] Cannot stat source: {exc}")

        self._log_write(f"Sales BoM Import — source: {source_path}\n")
        try:
            wb = openpyxl.load_workbook(source_path, data_only=True, read_only=True)
            sheet_names = list(wb.sheetnames)
            wb.close()
            logging.info(
                f"[SalesBoM-UI] Loaded source with {len(sheet_names)} sheet(s): "
                f"{sheet_names}"
            )
        except Exception as exc:
            logging.exception(
                f"[SalesBoM-UI] Failed to open source workbook {source_path!r}"
            )
            self._log_write(f"Could not open Sales BoM: {exc}\n")
            self.after(0, self._finish, False)
            return

        if not sheet_names:
            logging.warning(
                f"[SalesBoM-UI] Source has no worksheets: {source_path!r}"
            )
            self._log_write("Sales BoM has no worksheets.\n")
            self.after(0, self._finish, False)
            return

        self._log_write(f"Worksheets found: {', '.join(sheet_names)}\n")

        # Prompt for worksheet(s) (modal dialog on the main thread).
        chosen: Dict[str, Optional[List[str]]] = {"sheets": None}

        def _ask():
            logging.debug("[SalesBoM-UI] Showing worksheet picker dialog")
            chosen["sheets"] = self._pick_worksheet_dialog(
                source_path, sheet_names
            )

        # Marshal back to main thread, wait for result before continuing.
        ev = threading.Event()

        def _wrapped():
            try:
                _ask()
            finally:
                ev.set()
        self.after(0, _wrapped)
        if not ev.wait(timeout=300):
            logging.warning(
                "[SalesBoM-UI] Worksheet picker timed out after 300s — "
                "operator left the dialog open"
            )
            self._log_write("Worksheet selection timed out.\n")
            self.after(0, self._finish, False)
            return
        selected = chosen.get("sheets")
        if selected is None or not selected:
            logging.info(
                "[SalesBoM-UI] Worksheet selection cancelled / empty"
            )
            self._log_write("Worksheet selection cancelled.\n")
            self.after(0, self._finish, False)
            return

        logging.info(
            f"[SalesBoM-UI] Worksheet(s) selected ({len(selected)}): "
            f"{selected!r}"
        )
        self._log_write(
            f"Selected worksheet(s): {', '.join(selected)}\n"
        )
        # Hand off to the export path on the main thread — reuses the
        # existing customer/project prompt + save-folder picker.
        self.after(0, self._export_sales_bom, source_path, selected)

    def _pick_worksheet_dialog(
        self,
        source_path: str,
        sheet_names: List[str],
    ) -> Optional[List[str]]:
        """Show a modal multi-select dialog of worksheet names.

        Returns the chosen names (preserving listbox order), or
        ``None`` if the user cancelled. Returns ``[]`` only if the
        operator clicked OK with no selection (treated as cancel by
        the caller).

        Selection mode is ``EXTENDED`` so the operator can shift-click
        a range or ctrl-click individual items. When multiple sheets
        are picked, the builder merges their data into one combined
        output workbook (sites with the same name sum quantities;
        first-seen description wins).
        """
        logging.debug(
            f"[SalesBoM-UI] _pick_worksheet_dialog opened with "
            f"{len(sheet_names)} option(s)"
        )
        top = tk.Toplevel(self)
        top.title("Pick BoM Worksheet(s)")
        top.transient(self.winfo_toplevel())
        top.resizable(True, True)

        ttk.Label(
            top,
            text=(
                f"Source: {os.path.basename(source_path)}\n\n"
                "Pick one or more worksheets to process. Shift-click for\n"
                "a range, Ctrl-click for individual selections. When more\n"
                "than one is picked the data is merged into a single output\n"
                "workbook — sites with the same name sum across sheets."
            ),
            justify=tk.LEFT,
        ).pack(padx=12, pady=(12, 6), anchor="w")

        listbox = tk.Listbox(
            top,
            selectmode=tk.EXTENDED,
            height=min(16, len(sheet_names) + 1),
            width=50,
        )
        for n in sheet_names:
            listbox.insert(tk.END, n)
        listbox.selection_set(0)
        listbox.pack(padx=12, pady=6, fill=tk.BOTH, expand=True)

        result: Dict[str, Optional[List[str]]] = {"v": None}

        def _ok():
            sel = listbox.curselection()
            if sel:
                result["v"] = [sheet_names[i] for i in sel]
                logging.debug(
                    f"[SalesBoM-UI] Worksheet picker: OK -> {result['v']!r} "
                    f"({len(result['v'])} selected)"
                )
            else:
                result["v"] = []
                logging.debug(
                    "[SalesBoM-UI] Worksheet picker: OK with empty selection"
                )
            top.destroy()

        def _cancel():
            result["v"] = None
            logging.debug("[SalesBoM-UI] Worksheet picker: cancelled")
            top.destroy()

        btn = ttk.Frame(top)
        btn.pack(fill=tk.X, padx=12, pady=(6, 12))
        ttk.Button(btn, text="OK", command=_ok, width=10).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn, text="Cancel", command=_cancel, width=10).pack(side=tk.RIGHT, padx=4)

        listbox.bind("<Double-Button-1>", lambda _e: _ok())
        top.bind("<Return>", lambda _e: _ok())
        top.bind("<Escape>", lambda _e: _cancel())
        top.grab_set()
        top.wait_window()
        return result["v"]

    def _export_sales_bom(self, source_path: str, sheet_names) -> None:
        """Prompt for customer/project + save folder, then call the
        WorkbookBuilder to produce the per-site packing-slip output.

        ``sheet_names`` is either a single sheet name (legacy) or a
        list of names. When multiple are passed, the builder merges
        the data into one combined output workbook.

        Every step is logged under ``[SalesBoM-UI]`` so cancellations,
        validation failures, and unexpected exceptions all surface in
        the ATLAS log file.
        """
        # Normalize to list for downstream uniformity.
        if isinstance(sheet_names, str):
            sheet_names = [sheet_names]
        logging.info(
            f"[SalesBoM-UI] _export_sales_bom start: source={source_path!r}  "
            f"sheets={sheet_names!r}"
        )
        try:
            default_name = re.sub(r"[^\w\-]", "_", Path(source_path).stem)
            logging.debug(
                f"[SalesBoM-UI] Default filename derived from source stem: "
                f"{default_name!r}"
            )
            user_in = self.gui.get_user_inputs(default_name)
            if not user_in:
                logging.info(
                    "[SalesBoM-UI] Customer/project prompt cancelled by operator"
                )
                self._log_write("Export cancelled.\n")
                self._finish(False)
                return
            customer = user_in.get("Customer", "")
            project = user_in.get("Project", "")
            filename = user_in.get("Filename", default_name)
            logging.info(
                f"[SalesBoM-UI] User inputs received: customer={customer!r}  "
                f"project={project!r}  filename={filename!r}"
            )

            from utils.helpers import sanitize_filename_component
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            safe_name = "_".join(
                filter(None, [
                    sanitize_filename_component(filename),
                    sanitize_filename_component(customer),
                    sanitize_filename_component(project),
                    "SalesBoM_Site_Packing",
                    timestamp,
                ])
            )
            logging.debug(f"[SalesBoM-UI] Sanitized filename stem: {safe_name!r}")

            save_dir = filedialog.askdirectory(title="Choose folder to save Sales BoM packing slips")
            if not save_dir:
                logging.info(
                    "[SalesBoM-UI] Save folder prompt cancelled by operator"
                )
                self._log_write("Export cancelled (no folder chosen).\n")
                self._finish(False)
                return

            output_file = os.path.join(save_dir, f"{safe_name}.xlsx")
            logging.info(f"[SalesBoM-UI] Save target: {output_file!r}")
            self._log_write(f"Building output: {output_file}\n")
            self._status_var.set("Building…")

            builder = self.gui.workbook_builder
            try:
                builder.build_sales_bom_packing_slip_workbook(
                    source_path=source_path,
                    selected_sheets=sheet_names,
                    output_file=output_file,
                    customer=customer,
                    project=project,
                )
            except Exception:
                # WorkbookBuilder already logged details under [SalesBoM];
                # add the UI-side context and re-raise for the outer
                # handler.
                logging.exception(
                    "[SalesBoM-UI] WorkbookBuilder.build_sales_bom_packing_"
                    "slip_workbook raised — full traceback logged above"
                )
                raise
            logging.info(
                f"[SalesBoM-UI] Build succeeded: {output_file!r}"
            )
            self._log_write("Sales BoM packing slips generated.\n")
            self._finish(True)
        except Exception as exc:
            logging.exception("[SalesBoM-UI] Sales BoM Import export error")
            self._log_write(f"\nExport ERROR: {exc}\n")
            self._finish(False)

    def _parse_device(
        self,
        raw_text: str,
        device_id: str,
        module_path: str,
        outputs: Dict[str, Any],
    ) -> bool:
        """Parse one device transcript into *outputs*.  Returns True on success."""
        try:
            mod = importlib.import_module(module_path)
            script_inst = mod.Script(
                ip_address=device_id,
                connection_type="ssh",
                db_cache=self.gui.db_cache,
                db_path=self.gui.db_file,
            )

            self._log_write(f"  Parser: {module_path}\n")

            commands = script_inst.get_commands()
            outputs_list = _split_raw_output_by_commands(raw_text, commands)

            found = sum(1 for o in outputs_list if o.strip())
            self._log_write(
                f"  {found}/{len(commands)} command sections matched"
                f" for {device_id!r}\n"
            )
            logging.info(
                f"[RAW] Device {device_id!r}: parser={module_path} "
                f"matched {found}/{len(commands)} command sections"
            )
            for cmd, out in zip(commands, outputs_list):
                n = len(out.strip().splitlines()) if out.strip() else 0
                status = f"{n} lines" if n else "not found"
                self._log_write(f"    {cmd!r}: {status}\n")
                logging.debug(
                    f"[RAW] Device {device_id!r}: command {cmd!r} -> {status}"
                )

            if found == 0:
                self._log_write(
                    f"  ⚠  No inventory sections matched for {device_id!r}; "
                    "including device with placeholder row.\n"
                )
                logging.warning(
                    f"[RAW] Device {device_id!r}: no inventory sections matched; "
                    "adding placeholder row"
                )
                outputs[device_id] = {
                    "unmatched_data": {
                        "DataFrame": pd.DataFrame(
                            [
                                {
                                    "System Name": device_id,
                                    "System Type": "Unknown",
                                    "Type": "No inventory sections matched",
                                    "Part Number": "UNPARSED",
                                    "Serial Number": "",
                                    "Description": "Transcript did not contain expected inventory commands",
                                    "Name": "No Inventory Data",
                                    "Source": "Manual",
                                }
                            ]
                        ),
                        "System Info": {
                            "System Name": device_id,
                            "System Type": "Unknown",
                            "Source": "Manual",
                        },
                    }
                }
                return True

            script_inst.process_outputs(outputs_list, device_id, outputs)
            logging.info(f"[RAW] Device {device_id!r}: process_outputs completed")

            # Patch every DataFrame for this device so that:
            #   • System Name → device_id  (drives Device Name column + tab title)
            #   • Source       → "Manual"   (drives the IP/Source field on device sheets)
            if device_id in outputs and isinstance(outputs[device_id], dict):
                for section_data in outputs[device_id].values():
                    if not isinstance(section_data, dict):
                        continue
                    df = section_data.get("DataFrame")
                    if df is not None and hasattr(df, "__setitem__"):
                        try:
                            df["System Name"] = device_id
                            df["Source"] = "Manual"
                        except Exception:
                            pass
                    # Also patch System Info dict so workbook_builder fallbacks agree
                    si = section_data.get("System Info")
                    if isinstance(si, dict):
                        si["System Name"] = device_id
                        si["Source"] = "Manual"

            return True

        except Exception as exc:
            logging.exception(f"Error parsing device {device_id!r}")
            self._log_write(f"  ⚠  Error parsing {device_id!r}: {exc}\n")
            return False

    # ------------------------------------------------------------------
    # Export  (always called on the main thread via self.after())
    # ------------------------------------------------------------------

    def _export(self, outputs: Dict[str, Any], family: str, device_id: str) -> None:
        try:
            self._log_write("\nCollecting project information…\n")
            default_name = re.sub(r"[^\w\-]", "_", device_id)
            user_in = self.gui.get_user_inputs(default_name)
            if not user_in:
                self._log_write("Export cancelled.\n")
                self._finish(False)
                return

            customer = user_in.get("Customer", "")
            project  = user_in.get("Project", "")
            po       = user_in.get("Purchase Order", "")
            so       = user_in.get("Sales Order", "")
            filename = user_in.get("Filename", default_name)

            from utils.helpers import sanitize_filename_component
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            safe_name = "_".join(
                filter(None, [
                    sanitize_filename_component(filename),
                    sanitize_filename_component(customer),
                    sanitize_filename_component(project),
                    "Raw_Report",
                    timestamp,
                ])
            )

            save_dir = filedialog.askdirectory(title="Choose folder to save report")
            if not save_dir:
                self._log_write("Export cancelled (no folder chosen).\n")
                self._finish(False)
                return

            output_file = os.path.join(save_dir, f"{safe_name}.xlsx")
            self._log_write(f"Saving report to:\n  {output_file}\n")
            self._status_var.set("Exporting…")

            if family == "psi":
                self.gui.build_psi_report_workbook(
                    outputs, output_file,
                    customer=customer, project=project,
                    customer_po=po, sales_order=so,
                )
            elif family == "rls":
                self.gui.build_unified_report_workbook(
                    {"rls": outputs}, output_file,
                    customer=customer, project=project,
                    customer_po=po, sales_order=so,
                )
            else:
                self.gui.build_report_workbook(
                    outputs, output_file,
                    customer=customer, project=project,
                    customer_po=po, sales_order=so,
                )

            self._log_write("✓ Export complete!\n")
            self._finish(True)

        except Exception as exc:
            logging.exception("Raw export error")
            self._log_write(f"\n⚠  Export ERROR: {exc}\n")
            self._finish(False)

    def _finish(self, success: bool) -> None:
        self._running = False
        self._run_btn.configure(state="normal")
        self._status_var.set(
            "✓ Done — report saved!" if success else "⚠ Error — see log above"
        )

    # ------------------------------------------------------------------
    # Log helpers  (thread-safe via self.after())
    # ------------------------------------------------------------------

    def _log_write(self, text: str) -> None:
        if text.strip():
            logging.info("[FILE PROCESSING] %s", text.rstrip())

        def _write() -> None:
            self._log.configure(state="normal")
            self._log.insert(tk.END, text)
            self._log.see(tk.END)
            self._log.configure(state="disabled")
        try:
            self.after(0, _write)
        except Exception:
            pass  # widget may already be destroyed

    def _log_clear(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.configure(state="disabled")
