"""Packing slip frame — widgets and logic for the Packing Slip (From File) mode."""
import os
import shutil
import tempfile
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, List

import openpyxl
import pandas as pd

from utils.helpers import (
    UploadValidationError,
    friendly_error,
    sanitize_filename_component,
    strip_dataframe_strings,
    validate_uploaded_file,
)


class PackingSlipFrame(ttk.Frame):
    """Tkinter frame for the Packing Slip (From File) mode.

    Owns file-upload widgets, project information fields, and all packing slip
    generation logic for file-based input.  Uses *controller* to access
    ``controller.output_screen`` and ``controller.workbook_builder``.
    """

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self.uploaded_file_path: str | None = None
        self.uploaded_file_data: pd.DataFrame | None = None
        self._multisheet_device_file: bool = False
        self._family_by_ip: Dict[str, str] = {}
        self._last_customer: str = ""
        self._last_project: str = ""
        self._last_customer_po: str = ""
        self._last_sales_order: str = ""
        self._build()

    # ------------------------------------------------------------------
    # Frame construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        file_frame = ttk.LabelFrame(self, text="Upload File")
        file_frame.pack(fill=tk.X, pady=5)

        self.file_path_label = tk.Label(file_frame, text="No file selected", foreground="gray")
        self.file_path_label.pack(side=tk.LEFT, padx=10, pady=5)

        tk.Button(file_frame, text="Browse", command=self.upload_file).pack(side=tk.LEFT, padx=5)

        # Project Information is pulled from the uploaded workbook rather
        # than entered by hand. Customer / Project come from the Summary
        # B7 / D7 (inventory layout) or the first device tab's C5 / C6
        # (packing-slip layout); PO / SO come from device tab C7 / D7 and
        # fall back to "TBD" when absent.
        info_frame = ttk.LabelFrame(self, text="Project Information (read from file)")
        info_frame.pack(fill=tk.X, padx=5, pady=5)

        self._info_value_labels: Dict[str, tk.Label] = {}
        for row, label_text in enumerate((
            "Customer:", "Project:", "Purchase Order:", "Sales Order:",
        )):
            tk.Label(info_frame, text=label_text).grid(
                row=row, column=0, sticky="w", padx=5, pady=3
            )
            val_label = tk.Label(
                info_frame, text="—", foreground="gray", anchor="w", width=40,
            )
            val_label.grid(row=row, column=1, sticky="w", padx=5, pady=3)
            key = label_text.rstrip(":").lower().split()[-1]  # customer / project / order
            self._info_value_labels[label_text] = val_label

        ps_control_frame = ttk.Frame(self)
        ps_control_frame.pack(fill=tk.X, pady=10)

        self.ps_run_button = tk.Button(
            ps_control_frame, text="Generate Packing Slips",
            command=self.generate_packing_slips_from_file,
        )
        self.ps_run_button.pack(side=tk.RIGHT, padx=5)

        self.ps_status_label = tk.Label(ps_control_frame, text="Status: Ready", anchor="w")
        self.ps_status_label.pack(side=tk.RIGHT, padx=10)

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    def upload_file(self) -> None:
        """Prompt for a CSV or Excel file and load it into memory."""
        file_path = filedialog.askopenfilename(
            title="Select File (CSV or Excel)",
            filetypes=[
                ("Excel files", "*.xlsx *.xls"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if not file_path:
            return

        # Validate the path is a real file with an allowed extension and the
        # right magic bytes. Rejects symlinks, oversize files, and binaries
        # masquerading as CSV.
        try:
            resolved = validate_uploaded_file(
                file_path, allowed_kinds=("xlsx", "xls", "csv")
            )
        except UploadValidationError as e:
            messagebox.showerror("File Error", str(e))
            logging.warning(f"Packing slip upload rejected: {e}")
            return
        file_path = str(resolved)

        # Reset any stale project info from a prior upload — generate
        # will only run if the new upload supplies Customer + Project.
        self._last_customer = ""
        self._last_project = ""
        self._last_customer_po = ""
        self._last_sales_order = ""
        self._refresh_info_display()

        try:
            self.uploaded_file_path = file_path
            if file_path.lower().endswith(".csv"):
                self.uploaded_file_data = pd.read_csv(file_path)
                strip_dataframe_strings(self.uploaded_file_data)
                self._multisheet_device_file = False
                count_label = f"{len(self.uploaded_file_data)} rows"
            elif file_path.lower().endswith((".xlsx", ".xls")):
                xl = pd.ExcelFile(file_path)
                sheet_names = xl.sheet_names
                has_summary = any("summary" in str(s).lower() for s in sheet_names)
                device_sheets = [s for s in sheet_names if "summary" not in str(s).lower()]
                if len(sheet_names) > 1 and device_sheets:
                    # Multi-sheet file: each non-summary sheet is one device.
                    # This handles both plain device-report files (no summary)
                    # AND inventory reports (has Summary + device sheets).
                    # _process_multisheet_device_file will auto-skip any summary
                    # sheet because it has no PART NUMBER / SERIAL NUMBER header.
                    self._multisheet_device_file = True
                    self.uploaded_file_data = pd.DataFrame()  # placeholder
                    count_label = f"{len(device_sheets)} device(s)"
                else:
                    self._multisheet_device_file = False
                    self.uploaded_file_data = pd.read_excel(file_path)
                    strip_dataframe_strings(self.uploaded_file_data)
                    count_label = f"{len(self.uploaded_file_data)} rows"
            else:
                messagebox.showerror("File Error", "Unsupported file format. Please use CSV or Excel files.")
                return

            if file_path.lower().endswith((".xlsx", ".xls")):
                self._try_populate_fields_from_file(file_path)

            file_name = os.path.basename(file_path)
            self.file_path_label.config(
                text=f"✓ {file_name} ({count_label})",
                foreground="green",
            )
            activity = getattr(self.controller, "log_activity", logging.info)
            activity(
                f"[PACKING] Loaded {file_path} with {count_label}."
            )

        except Exception as e:
            messagebox.showerror("File Error", f"Failed to load file:\n{friendly_error(e)}")
            logging.exception("File upload error")

    # ------------------------------------------------------------------
    # Metadata auto-populate
    # ------------------------------------------------------------------

    def _try_populate_fields_from_file(self, file_path: str) -> None:
        """Extract Customer, Project, PO, and SO from the uploaded workbook
        and store them on ``self._last_*`` so generation has values to pass
        through. Also refreshes the read-only display labels.

        Two source layouts are supported:

        * **Inventory / BoM workbook** — Summary sheet has Customer at
          B7 and Project at D7. Per-device "Device Report" tabs carry
          Customer at C5, Project at C6, Customer PO at C7, Sales
          Order at D7. PO/SO come from the first Device Report tab.
        * **Existing packing slip workbook** — per-device sheets carry
          Customer at C5 and Project at C6, but B7 is ``Device ID:``
          (not a PO marker), so PO/SO default to TBD.

        A sheet is recognized as a Device Report when its ``B7`` cell
        starts with ``Customer PO`` (the label adjacent to the PO/SO
        values). Without that marker we don't read C7/D7 — that
        prevents the BOM aggregate's column headers ("Equipment
        Description" at C7, first device name at D7) from being
        mis-read as PO/SO, which was the reported bug.
        """
        def _is_device_report(ws) -> bool:
            try:
                b7 = ws["B7"].value
            except Exception:
                return False
            return (
                isinstance(b7, str)
                and b7.strip().lower().startswith("customer po")
            )

        customer, project, po, so = "", "", "", ""
        try:
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            summary_sheets = [n for n in wb.sheetnames if "summary" in n.lower()]

            # Strategy 1a: any sheet that LOOKS like a Device Report.
            # The structural marker on B7 keeps the BOM aggregate (which
            # has "Equipment Description" at C7 and the first device
            # name at D7) from being mistaken for a Device Report.
            device_report_sheets = [
                n for n in wb.sheetnames if _is_device_report(wb[n])
            ]
            if device_report_sheets:
                ws = wb[device_report_sheets[0]]
                for coord, target in (
                    ("C5", "customer"), ("C6", "project"),
                    ("C7", "po"),       ("D7", "so"),
                ):
                    val = ws[coord].value
                    if val and str(val).strip() not in ("", "nan", "None"):
                        if target == "customer":
                            customer = str(val).strip()
                        elif target == "project":
                            project = str(val).strip()
                        elif target == "po":
                            po = str(val).strip()
                        elif target == "so":
                            so = str(val).strip()

            # Strategy 1b: Packing-Slip-style per-device sheet supplies
            # Customer + Project (B5='Customer:' C5=name, B6='Project:'
            # C6=name) but NOT PO/SO. Only consult when 1a missed.
            if not customer or not project:
                for name in wb.sheetnames:
                    if name in device_report_sheets:
                        continue
                    low = name.lower()
                    if (
                        "summary" in low
                        or low == "bom"
                        or low == "inventory by site"
                    ):
                        continue
                    ws = wb[name]
                    b5 = ws["B5"].value
                    b6 = ws["B6"].value
                    if (
                        isinstance(b5, str)
                        and b5.strip().lower().startswith("customer")
                        and isinstance(b6, str)
                        and b6.strip().lower().startswith("project")
                    ):
                        if not customer:
                            v = ws["C5"].value
                            if v and str(v).strip() not in ("", "nan", "None"):
                                customer = str(v).strip()
                        if not project:
                            v = ws["C6"].value
                            if v and str(v).strip() not in ("", "nan", "None"):
                                project = str(v).strip()
                        break

            # Strategy 2: inventory report summary sheet
            # Summary sheet: B7 = Customer value, D7 = Project value
            if (not customer or not project) and summary_sheets:
                ws = wb[summary_sheets[0]]
                b7 = ws["B7"].value
                d7 = ws["D7"].value
                if not customer and b7 and str(b7).strip() not in ("", "nan", "None"):
                    customer = str(b7).strip()
                if not project and d7 and str(d7).strip() not in ("", "nan", "None"):
                    project = str(d7).strip()

            wb.close()
        except Exception as e:
            logging.debug(f"Could not extract metadata from uploaded file: {e}")

        # Stash extracted values for the generate / print steps.
        self._last_customer = customer
        self._last_project = project
        self._last_customer_po = po or "TBD"
        self._last_sales_order = so or "TBD"

        # Refresh the read-only display.
        self._refresh_info_display()

    def _refresh_info_display(self) -> None:
        """Update the read-only project-info labels to reflect what was
        extracted from the upload. Missing values render in red so the
        operator notices before clicking Generate."""
        missing_fg = "#b00020"
        present_fg = "black"
        pairs = (
            ("Customer:",       self._last_customer,    True),
            ("Project:",        self._last_project,     True),
            ("Purchase Order:", self._last_customer_po, False),
            ("Sales Order:",    self._last_sales_order, False),
        )
        for label_text, value, is_required in pairs:
            lbl = self._info_value_labels.get(label_text)
            if lbl is None:
                continue
            if value and value != "TBD":
                lbl.config(text=value, foreground=present_fg)
            elif value == "TBD":
                lbl.config(text="TBD", foreground="gray")
            else:
                lbl.config(
                    text="[not found in file]",
                    foreground=missing_fg if is_required else "gray",
                )

    # ------------------------------------------------------------------
    # Packing slip generation
    # ------------------------------------------------------------------

    def generate_packing_slips_from_file(self) -> None:
        """Validate inputs and generate packing slips from the uploaded file.

        Customer / Project / PO / SO are no longer typed in — they're
        extracted from the uploaded workbook at upload time and stashed on
        ``self._last_*``. If Customer or Project couldn't be found, we
        refuse to generate and tell the operator where to put them.
        """
        if self.uploaded_file_data is None:
            messagebox.showwarning("No File", "Please upload a file first.")
            return

        customer = self._last_customer
        project = self._last_project
        customer_po = self._last_customer_po or "TBD"
        sales_order = self._last_sales_order or "TBD"

        if not all([customer, project]):
            messagebox.showerror(
                "Missing Info",
                "Customer and Project could not be read from the uploaded "
                "file.\n\n"
                "Expected one of:\n"
                "  • Inventory workbook → Summary sheet B7 / D7\n"
                "  • Packing slip workbook → first device sheet C5 / C6\n\n"
                "Fill those cells in and re-upload.",
            )
            return

        self.ps_run_button.config(state=tk.DISABLED)
        self.ps_status_label.config(text="Status: Processing...")
        self.controller.root.update_idletasks()

        # Create temp directory with restricted permissions (owner only, no group/other access)
        # Use secure creation pattern: mkdir first, then restrict via chmod BEFORE any file ops
        # to prevent TOCTOU (Time-Of-Check-Time-Of-Use) race condition attacks
        tmp_dir = tempfile.mkdtemp(prefix="ATLAS_", dir=None)
        os.chmod(tmp_dir, 0o700)

        try:
            if self._multisheet_device_file:
                processed_data = self._process_multisheet_device_file(self.uploaded_file_path)
            else:
                processed_data = self._process_file_for_packing_slip(self.uploaded_file_data)

            if not processed_data:
                self.ps_status_label.config(text="Status: Ready")
                messagebox.showwarning("No Data", "No valid data found in the file.")
                return

            ip_list = list(processed_data.keys())
            family_for_ip = getattr(self, "_family_by_ip", None) or {}
            # Prefer live-run families when present (overrides file-detected for same IPs).
            live_family = getattr(self.controller, "device_family_by_ip", None) or {}
            for ip, fam in live_family.items():
                if ip in processed_data:
                    family_for_ip[ip] = fam

            save_path = self.controller.build_unified_packing_slip_workbook(
                processed_data, ip_list, customer, project, customer_po, sales_order, tmp_dir,
                family_for_ip=family_for_ip,
                display_ip_for_key=getattr(self, "_display_ip_for_key", None),
            )

            self.ps_status_label.config(text="Status: Ready")
            activity = getattr(self.controller, "log_activity", logging.info)
            activity(
                f"[PACKING] Processing complete; "
                f"{len(processed_data)} device(s) ready."
            )
            self._show_print_selection_dialog(save_path)

        except Exception as e:
            self.ps_status_label.config(text="Status: Error")
            messagebox.showerror("Generation Error", f"Failed to generate packing slips:\n{friendly_error(e)}")
            logging.exception("Packing slip generation error")
        finally:
            self.ps_run_button.config(state=tk.NORMAL)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Print selection
    # ------------------------------------------------------------------

    def _show_print_selection_dialog(self, workbook_path: str) -> None:
        """Open a dialog listing device sheets as checkboxes so the user can
        choose which ones to send to the printer."""
        try:
            wb = openpyxl.load_workbook(workbook_path, read_only=True)
            summary_sheets = [n for n in wb.sheetnames if "summary" in n.lower()]
            device_sheets = [n for n in wb.sheetnames if "summary" not in n.lower()]
            wb.close()
        except Exception as e:
            logging.error(f"Could not read workbook for print selection: {e}")
            return

        if not device_sheets:
            return

        dialog = tk.Toplevel(self.controller.root)
        dialog.title("Select Sheets to Print")
        dialog.grab_set()
        dialog.resizable(False, False)

        tk.Label(dialog, text="Select device sheets to print:", font=("TkDefaultFont", 10, "bold")).pack(
            padx=15, pady=(12, 4), anchor="w"
        )

        # Scrollable checkbox list
        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=4)

        canvas_height = min(300, len(device_sheets) * 28 + 10)
        canvas = tk.Canvas(list_frame, height=canvas_height, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if len(device_sheets) > 10:
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        vars_: Dict[str, tk.BooleanVar] = {}
        for name in device_sheets:
            var = tk.BooleanVar(value=True)
            vars_[name] = var
            ttk.Checkbutton(inner, text=name, variable=var).pack(anchor="w", padx=5, pady=2)

        # Select / Deselect All
        sel_frame = ttk.Frame(dialog)
        sel_frame.pack(fill=tk.X, padx=15, pady=(2, 6))
        tk.Button(sel_frame, text="Select All",   command=lambda: [v.set(True)  for v in vars_.values()]).pack(side=tk.LEFT, padx=2)
        tk.Button(sel_frame, text="Deselect All", command=lambda: [v.set(False) for v in vars_.values()]).pack(side=tk.LEFT, padx=2)

        # Consolidated vs Individual toggle
        ttk.Separator(dialog, orient="horizontal").pack(fill=tk.X, padx=15, pady=(4, 0))
        mode_frame = ttk.LabelFrame(dialog, text="Output Mode")
        mode_frame.pack(fill=tk.X, padx=15, pady=(6, 4))
        print_mode = tk.StringVar(value="consolidated")
        tk.Radiobutton(
            mode_frame, text="Consolidated  (save all selected into one workbook)",
            variable=print_mode, value="consolidated",
        ).pack(anchor="w", padx=10, pady=2)
        tk.Radiobutton(
            mode_frame, text="Individual  (one workbook, one tab per device)",
            variable=print_mode, value="individual",
        ).pack(anchor="w", padx=10, pady=2)

        def on_print() -> None:
            selected = [name for name, var in vars_.items() if var.get()]
            if not selected:
                messagebox.showwarning("No Selection", "Please select at least one sheet.", parent=dialog)
                return
            mode = print_mode.get()
            dialog.destroy()
            self._print_selected_sheets(workbook_path, selected, summary_sheets, mode)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=15, pady=(4, 12))
        tk.Button(btn_frame, text="Save / Open", command=on_print, width=16).pack(side=tk.RIGHT, padx=2)
        tk.Button(btn_frame, text="Cancel", command=dialog.destroy, width=10).pack(side=tk.RIGHT, padx=2)

        dialog.wait_window()

    @staticmethod
    def _prune_summary_to_selected(wb, summary_sheet_names: List[str], selected_sheets: set) -> None:
        """Rewrite each Summary sheet so its data rows only reference the
        device sheets the user kept.

        Summary layout (uniform with inventory Summary, set by
        ``_format_summary_sheet`` with ``title='Packing Slip Summary'``):
            row 7   B7 Customer value | D7 Project value | F7 Device Count
            row 9   header — B # | C IP Address | D Device Name | E Asset Tag
            row 10+ data rows — B/C/D/E

        Device Name in column D equals the device sheet's title in the
        current workbook builder; the cell's hyperlink also points at that
        sheet. We use either signal to decide whether to keep the row.
        Asset Tag values in column E are preserved on prune.
        """
        for name in summary_sheet_names:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            max_row = ws.max_row or 10

            kept_rows = []
            for r in range(10, max_row + 1):
                d_cell = ws.cell(r, 4)
                d_value = d_cell.value
                if d_value is None or str(d_value).strip() == "":
                    continue
                link_target = None
                if d_cell.hyperlink is not None:
                    # Internal links live in `.location` (e.g. "'Sheet'!A1");
                    # external links use `.target`. Either is fine for matching.
                    link_target = d_cell.hyperlink.location or d_cell.hyperlink.target
                target_sheet = PackingSlipFrame._extract_sheet_from_link(link_target) if link_target else None
                candidate = target_sheet or str(d_value)
                if candidate in selected_sheets:
                    kept_rows.append((
                        ws.cell(r, 3).value,  # IP
                        d_value,              # Device Name
                        link_target,
                        ws.cell(r, 5).value,  # Asset Tag (preserve)
                    ))

            # Clear old data rows in B/C/D/E.
            for r in range(10, max_row + 1):
                for c in (2, 3, 4, 5):
                    cell = ws.cell(r, c)
                    cell.value = None
                    cell.hyperlink = None

            for offset, (ip, dev_name, link, tag) in enumerate(kept_rows):
                r = 10 + offset
                ws.cell(r, 2).value = offset + 1   # # (sequence)
                ws.cell(r, 3).value = ip
                cell = ws.cell(r, 4)
                cell.value = dev_name
                if link:
                    cell.hyperlink = "#" + link if not link.startswith("#") else link
                    cell.style = "Hyperlink"
                if tag is not None:
                    ws.cell(r, 5).value = tag

            ws["F7"] = len(kept_rows)

    @staticmethod
    def _extract_sheet_from_link(link: str) -> str | None:
        """Parse a sheet name out of an internal hyperlink like ``'Sheet'!A1``."""
        if not link:
            return None
        s = link[1:] if link.startswith("#") else link
        if "!" in s:
            s = s.split("!", 1)[0]
        if s.startswith("'") and s.endswith("'"):
            s = s[1:-1]
        return s or None

    def _print_selected_sheets(self, source_path: str, selected_sheets: List[str], summary_sheets: List[str], mode: str = "consolidated") -> None:
        """Save selected sheets to a workbook and open it.

        mode='consolidated': prompt for a single save path; one workbook with
                             the summary + all selected device sheets.
        mode='individual':   prompt for a save folder; one workbook per device,
                             each containing the summary + that device's sheet.
        """
        try:
            base_name = os.path.splitext(os.path.basename(source_path))[0]

            if mode == "individual":
                safe_customer = sanitize_filename_component(self._last_customer)
                safe_project = sanitize_filename_component(self._last_project)
                save_path = filedialog.asksaveasfilename(
                    title="Save Individual Packing Slips Workbook",
                    initialfile=f"PackingSlip_{safe_customer}_{safe_project}_Individual.xlsx",
                    defaultextension=".xlsx",
                    filetypes=[("Excel Workbook", "*.xlsx")],
                )
                if not save_path:
                    return  # user cancelled

                wb_src = openpyxl.load_workbook(source_path)
                sheets_to_keep = set(summary_sheets + list(selected_sheets))
                for name in list(wb_src.sheetnames):
                    if name not in sheets_to_keep:
                        del wb_src[name]

                # Prune the Summary sheet to only list devices whose sheets
                # survived the deletion above; otherwise it still lists every
                # device from the source workbook.
                PackingSlipFrame._prune_summary_to_selected(wb_src, summary_sheets, set(selected_sheets))

                autosize_wb = self.controller.workbook_builder.autosize_workbook_columns
                autosize_wb(wb_src)
                wb_src.save(save_path)
                os.startfile(save_path)

                activity = getattr(self.controller, "log_activity", logging.info)
                activity(
                    f"[PACKING] Saved {len(selected_sheets)} individual "
                    f"packing-slip tab(s) to {save_path}."
                )

            else:  # consolidated — single sheet, all line items from all selected devices
                safe_customer = sanitize_filename_component(self._last_customer)
                safe_project = sanitize_filename_component(self._last_project)
                save_path = filedialog.asksaveasfilename(
                    title="Save Consolidated Packing Slip",
                    initialfile=f"PackingSlip_{safe_customer}_{safe_project}_Consolidated.xlsx",
                    defaultextension=".xlsx",
                    filetypes=[("Excel files", "*.xlsx")],
                )
                if not save_path:
                    return

                # SECURITY: Validate save path is within an accessible directory (prevent directory traversal)
                try:
                    save_dir = os.path.dirname(save_path)
                    if save_dir:
                        # Ensure directory is accessible before attempting to write
                        os.makedirs(save_dir, exist_ok=True)
                        if not os.access(save_dir, os.W_OK):
                            messagebox.showerror("Permission Error", f"No write permission to directory: {save_dir}")
                            return
                except (OSError, PermissionError) as e:
                    messagebox.showerror("Path Error", f"Cannot write to save location: {friendly_error(e)}")
                    return

                # Build a fresh single-sheet workbook from the consolidated template
                # (data/LAMIS_Consolidated_Packing_Slip.xlsx) which has Device ID in col B
                base_slip_template = self.controller.workbook_builder.packing_slip_template
                consolidated_template = os.path.join(
                    os.path.dirname(base_slip_template),
                    "ATLAS_Consolidated_Packing_Slip.xlsx",
                )
                if not os.path.exists(consolidated_template):
                    consolidated_template = base_slip_template  # fallback

                wb_out = openpyxl.load_workbook(consolidated_template)
                device_sheet = wb_out.active

                # Helper: write to the top-left (master) cell of any merged range,
                # which is the only writable cell in a merge group.
                def write_cell(ws, coord, value):
                    from openpyxl.utils import coordinate_to_tuple
                    r, c = coordinate_to_tuple(coord)
                    for merged in ws.merged_cells.ranges:
                        if r >= merged.min_row and r <= merged.max_row and c >= merged.min_col and c <= merged.max_col:
                            ws.cell(merged.min_row, merged.min_col).value = value
                            return
                    ws[coord] = value

                # Consolidated template layout:
                #   C5 = Customer value, C6 = Project value
                #   Row 14: headers (Device ID | Customer PO | Part Number | Serial Number | Description | Asset Tag)
                #   Row 15+: data rows  — B=Device ID, C=Customer PO, D=Part#, E=Serial#, F=Description
                write_cell(device_sheet, "C5", self._last_customer or "")
                write_cell(device_sheet, "C6", self._last_project or "")

                # Read line items from every selected device sheet and concatenate
                wb_src = openpyxl.load_workbook(source_path, read_only=True)
                row_num = 15
                for sheet_name in selected_sheets:
                    if sheet_name not in wb_src.sheetnames:
                        continue
                    ws = wb_src[sheet_name]
                    for row in ws.iter_rows(min_row=15, values_only=True):
                        if len(row) < 6:
                            continue
                        _so, po, part, serial, desc = row[1], row[2], row[3], row[4], row[5]
                        has_data = any(
                            str(v).strip() not in ("", "None", "nan")
                            for v in (part, serial, desc)
                            if v is not None
                        )
                        if has_data:
                            # SECURITY: Sanitize all cell values to prevent formula injection attacks
                            sanitize = self.controller.workbook_builder._sanitize_cell
                            device_sheet[f"B{row_num}"] = sanitize(sheet_name)   # Device ID
                            device_sheet[f"C{row_num}"] = sanitize(po or "")
                            device_sheet[f"D{row_num}"] = sanitize(part or "")
                            device_sheet[f"E{row_num}"] = sanitize(serial or "")
                            device_sheet[f"F{row_num}"] = sanitize(desc or "")
                            row_num += 1
                wb_src.close()

                self.controller.workbook_builder.autosize_workbook_columns(wb_out)
                wb_out.save(save_path)
                os.startfile(save_path)

                activity = getattr(self.controller, "log_activity", logging.info)
                activity(
                    f"[PACKING] Saved consolidated packing slip with "
                    f"{row_num - 15} line item(s) to {save_path}."
                )

        except Exception as e:
            messagebox.showerror("Print Error", f"Failed to prepare sheets for printing:\n{friendly_error(e)}")
            logging.exception("Print selection error")

    # ------------------------------------------------------------------
    # File processing
    # ------------------------------------------------------------------

    def _process_multisheet_device_file(self, file_path: str) -> Dict[str, pd.DataFrame]:
        """Read a multi-sheet device-report Excel file where each sheet is one device.

        Auto-detects the header row by scanning for 'PART NUMBER' / 'SERIAL NUMBER'.
        Extracts the source IP from sheet metadata and uses it as the dict key.
        Adds a 'System Name' column (= sheet name) so the workbook builder can
        use the sheet name as the device name.

        Side effect: populates ``self._family_by_ip`` with detected device family
        per IP based on the System Type metadata cell (Excel F7).
        """
        processed_data: Dict[str, pd.DataFrame] = {}
        self._family_by_ip = {}
        # Maps disambiguated key (e.g. "10.0.0.1_us..._com") back to the bare
        # source IP ("10.0.0.1") so the Summary column shows the real IP
        # rather than the synthetic dict key. Pre-seeded for every device,
        # even when no dedup was needed, so the workbook builder never
        # silently falls through to the mangled key.
        self._display_ip_for_key: Dict[str, str] = {}
        try:
            xl = pd.ExcelFile(file_path)
            for sheet_name in xl.sheet_names:
                df_raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None)

                # Extract source IP from metadata (typically row 4, column 5)
                ip_address = sheet_name  # fallback to sheet name
                try:
                    val = str(df_raw.iloc[4, 5]).strip()
                    if val and val.lower() != "nan":
                        ip_address = val
                except (IndexError, KeyError):
                    pass

                # Extract System Type from F7 (iloc row 6, col 5) for family detection.
                system_type = ""
                try:
                    st = str(df_raw.iloc[6, 5]).strip()
                    if st and st.lower() != "nan":
                        system_type = st
                except (IndexError, KeyError):
                    pass

                # Remember the bare IP BEFORE we mangle the key. The display
                # map lets the workbook builder show "10.0.0.1" on the Summary
                # row even when the dict key is "10.0.0.1_<sheetname>".
                bare_ip = ip_address

                # Ensure key uniqueness if multiple devices share the same IP
                if ip_address in processed_data:
                    ip_address = f"{ip_address}_{sheet_name}"

                # Find the header row: first row containing 'PART NUMBER' or 'SERIAL NUMBER'
                header_row = None
                for idx, row in df_raw.iterrows():
                    row_vals = [str(v).upper() for v in row if str(v).lower() != "nan"]
                    joined = " ".join(row_vals)
                    if "PART NUMBER" in joined or "SERIAL NUMBER" in joined:
                        header_row = idx
                        break

                if header_row is None:
                    logging.warning(f"Sheet '{sheet_name}': no header row found, skipping")
                    continue

                df = pd.read_excel(file_path, sheet_name=sheet_name, header=header_row)
                strip_dataframe_strings(df)

                # Truncate at the "ADDITIONAL NODE INFORMATION" sentinel row — everything
                # after that line (software, slots, redundancy, power, topology) is not
                # needed in a packing slip.
                sentinel_mask = df.apply(
                    lambda row: row.astype(str).str.upper().str.contains(
                        r"ADDITIONAL\s+NODE\s+INFORMATION", regex=True
                    ).any(),
                    axis=1,
                )
                if sentinel_mask.any():
                    cutoff = sentinel_mask.idxmax()
                    df = df.loc[:cutoff - 1] if cutoff > df.index[0] else pd.DataFrame(columns=df.columns)

                # Drop rows where all key columns are empty
                relevant_cols = [
                    c for c in df.columns
                    if any(k in str(c).upper() for k in ("PART NUMBER", "SERIAL NUMBER", "DESCRIPTION"))
                ]
                if relevant_cols:
                    df = df.dropna(subset=relevant_cols, how="all")
                    df = df[
                        ~df[relevant_cols].apply(
                            lambda r: all(str(v).strip() in ("", "nan") for v in r), axis=1
                        )
                    ]

                # Add System Name column so workbook builder uses sheet name as device name
                df.insert(0, "System Name", sheet_name)

                if not df.empty:
                    processed_data[ip_address] = df.reset_index(drop=True)
                    self._display_ip_for_key[ip_address] = bare_ip
                    # Map system type to family.
                    st_l = system_type.lower()
                    if "rls" in st_l or "ciena" in st_l:
                        self._family_by_ip[ip_address] = "rls"
                    elif "psi" in st_l or "1830" in st_l or "nokia" in st_l:
                        self._family_by_ip[ip_address] = "psi"
                    else:
                        self._family_by_ip[ip_address] = "default"

            return processed_data

        except Exception as e:
            logging.error(f"Error processing multi-sheet device file: {e}")
            raise

    def _process_file_for_packing_slip(self, df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """Convert uploaded DataFrame to per-device dict for the workbook builder."""
        processed_data: Dict[str, pd.DataFrame] = {}
        try:
            # Truncate at "ADDITIONAL NODE INFORMATION" sentinel — not needed in packing slips.
            sentinel_mask = df.apply(
                lambda row: row.astype(str).str.upper().str.contains(
                    r"ADDITIONAL\s+NODE\s+INFORMATION", regex=True
                ).any(),
                axis=1,
            )
            if sentinel_mask.any():
                cutoff = sentinel_mask.idxmax()
                df = df.loc[:cutoff - 1] if cutoff > df.index[0] else pd.DataFrame(columns=df.columns)

            # Use lowercased column names only for device key detection; preserve
            # original column casing so the workbook builder can access "Part Number" etc.
            col_lower_map = {col: str(col).lower() for col in df.columns}

            # Check patterns from most specific to least specific to avoid
            # matching unrelated columns like "Part Name" or "File Name".
            # "ip" is NOT used as a bare substring because it matches "Description".
            device_key = None
            priority_patterns = ["system name", "device", "ip address", " ip"]
            fallback_patterns = ["name"]
            for pattern_list in (priority_patterns, fallback_patterns):
                for pattern in pattern_list:
                    for col, col_lower in col_lower_map.items():
                        if pattern in col_lower or (col_lower.strip() == "ip" and pattern == " ip"):
                            device_key = col
                            break
                    if device_key:
                        break
                if device_key:
                    break

            if device_key:
                for device_id, group in df.groupby(device_key, sort=False):
                    processed_data[str(device_id)] = group.reset_index(drop=True)
            else:
                processed_data["Device_0"] = df.reset_index(drop=True)

            return processed_data

        except Exception as e:
            logging.error(f"Error processing file: {e}")
            raise
