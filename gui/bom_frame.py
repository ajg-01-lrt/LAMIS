"""
gui/bom_frame.py - "BoM" sub-mode under File Processing.

Adds (or refreshes) a Bill of Materials sheet on an existing inventory
workbook by reading the Summary sheet's display labels, following each
row's hyperlink to the underlying device tab, and aggregating part rows
through ``WorkbookBuilder._build_bom_sheet``.
"""
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

import openpyxl

from gui.workbook_builder import (
    INVENTORY_TAB_NAME,
    _LEGACY_INVENTORY_TAB_NAME,
    find_inventory_sheet_name,
)


_HYPERLINK_TARGET_RE = re.compile(r"^#?'?([^'!]+?)'?!")

# Tabs that should never end up on a synthesized BOM: template/metadata
# leftovers (Customer-project, Customer, Project, Header, Cover) and
# aggregate Task Order roll-up sheets (T##, TO##) whose contents the BOM
# itself is meant to replace.  Matched case-insensitively against the tab
# name.  Only applied when we synthesize the Summary — if the workbook
# already shipped a Summary we trust what it lists.
_TEMPLATE_TAB_NAMES = {"customer-project", "customer", "project", "header", "cover"}
_AGGREGATE_TAB_RE = re.compile(r"^TO?\d{1,3}$", re.IGNORECASE)


def _is_non_inventory_tab(name: str) -> bool:
    s = (name or "").strip().lower()
    if s in _TEMPLATE_TAB_NAMES:
        return True
    if _AGGREGATE_TAB_RE.match(name or ""):
        return True
    return False


def _resolve_hyperlink_target(cell: Any) -> Optional[str]:
    """Return the underlying sheet name a Summary cell links to, if any."""
    link = getattr(cell, "hyperlink", None)
    if link is None:
        return None
    raw = getattr(link, "location", None) or getattr(link, "target", None)
    if not raw:
        return None
    m = _HYPERLINK_TARGET_RE.match(str(raw))
    return m.group(1).strip() if m else None


class BomFrame(ttk.Frame):
    """UI panel for adding / refreshing the BOM sheet on an existing workbook."""

    def __init__(self, parent: tk.Widget, gui: Any) -> None:
        super().__init__(parent)
        self.gui = gui
        self._input_path: Optional[str] = None
        self._running = False
        self._setup_ui()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        pad: Dict[str, int] = {"padx": 8, "pady": 4}

        file_frame = ttk.LabelFrame(self, text="Workbook (.xlsx with a Summary sheet)")
        file_frame.pack(fill=tk.X, **pad)

        self._file_label = tk.StringVar(value="No workbook selected")
        ttk.Label(file_frame, textvariable=self._file_label, width=80, anchor="w").pack(
            side=tk.LEFT, padx=6, pady=6
        )
        ttk.Button(file_frame, text="Browse…", command=self._browse_file).pack(
            side=tk.LEFT, padx=6, pady=6
        )

        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill=tk.X, **pad)

        self._run_btn = ttk.Button(
            ctrl_frame, text="Build / Refresh BOM", command=self._on_run
        )
        self._run_btn.pack(side=tk.LEFT, padx=6)

        self._status_var = tk.StringVar(value="Ready — select a workbook and click Build / Refresh BOM")
        ttk.Label(ctrl_frame, textvariable=self._status_var, foreground="gray").pack(
            side=tk.LEFT, padx=12
        )

        log_frame = ttk.LabelFrame(self, text="Build Log")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", wrap="word")
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        if msg.strip():
            logging.info("[BOM] %s", msg.rstrip())

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

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select inventory workbook",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._input_path = path
        self._file_label.set(path)
        self._set_status("Ready — click Build / Refresh BOM")

    def _on_run(self) -> None:
        if self._running:
            return
        if not self._input_path or not os.path.isfile(self._input_path):
            messagebox.showerror("BoM", "Select a valid .xlsx workbook first.")
            return
        self._running = True
        self._run_btn.configure(state="disabled")
        self._set_status("Building BOM…")
        threading.Thread(target=self._run_worker, args=(self._input_path,), daemon=True).start()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run_worker(self, path: str) -> None:
        try:
            out_path = self._build(path)
            self._append_log(f"Wrote: {out_path}")
            self._set_status(f"Done — {os.path.basename(out_path)}")
        except Exception as exc:
            logging.exception("[BoM] build failed")
            # Snapshot the message before the except block exits — Python
            # deletes the bound `exc` at the end of `except`, so capturing it
            # inside the after() lambda's free scope blows up with NameError.
            err_msg = str(exc)
            self._append_log(f"ERROR: {err_msg}")
            self._set_status("Failed")
            try:
                self.after(0, lambda: messagebox.showerror("BoM", err_msg))
            except Exception:
                pass
        finally:
            self._running = False
            try:
                self.after(0, lambda: self._run_btn.configure(state="normal"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Core build
    # ------------------------------------------------------------------

    def _build(self, src_path: str) -> str:
        builder = self.gui.workbook_builder
        self._append_log(f"Loading workbook: {src_path}")
        wb = openpyxl.load_workbook(src_path)
        synthesized_summary = False
        if "Summary" not in wb.sheetnames:
            self._append_log(
                "No 'Summary' sheet — scanning device tabs to synthesize one."
            )
            synthesized = self._synthesize_summary(wb, builder)
            self._append_log(
                f"Synthesized Summary with {synthesized} site(s) from device tabs."
            )
            if synthesized == 0:
                raise RuntimeError(
                    "Workbook has no 'Summary' sheet and no usable device "
                    "tabs were found (each tab must have part numbers in "
                    "column D from row 15 onward)."
                )
            synthesized_summary = True

        sheetname_set = set(wb.sheetnames)
        summary_items, display_to_tab = self._extract_summary(wb["Summary"], sheetname_set)

        # Heal a truncated Summary: when the workbook has device tabs
        # that the Summary table doesn't list (this happens after a
        # multi-run LAN scan where the inventory builder dropped prior
        # Summary rows — see the matching fix in
        # build_psi_report_workbook), pull those orphan tabs in and
        # rewrite the Summary sheet so the rest of this build sees the
        # full site list. Without this, the BoM only carries a column
        # for the single device the Summary happens to list.
        if not synthesized_summary:
            added = self._augment_summary_from_device_tabs(
                wb, builder, summary_items, display_to_tab,
            )
            if added:
                self._append_log(
                    f"Summary healed: +{added} device tab(s) added from "
                    f"orphaned sheets"
                )
        self._append_log(f"Summary sites: {len(summary_items)}")

        # Expand any "N/A QTY:N" rows in-place on every inventory tab so the
        # tab itself shows N individual rows instead of one bucket. Run
        # BEFORE part-number normalization and BOM aggregation so the
        # downstream scanners see the expanded rows.
        qty_extras_total = 0
        for _ip, _name, display_title in summary_items:
            tab = display_to_tab.get(display_title, display_title)
            if tab not in sheetname_set:
                continue
            try:
                qty_extras_total += builder._expand_qty_buckets_on_sheet(wb[tab])
            except Exception as exc:
                self._append_log(f"  QTY expansion failed on '{tab}': {exc}")
        if qty_extras_total:
            self._append_log(
                f"Expanded QTY buckets: +{qty_extras_total} row(s) added across device tabs"
            )

        # Normalize part numbers on every device tab first so the BOM
        # aggregation reads already-canonical values. Serial numbers are
        # left untouched.
        tab_updates = builder.normalize_device_tab_part_numbers(wb)
        if tab_updates:
            self._append_log(f"Device tabs: updated {tab_updates} cells (part / description)")

        # Make sure every inventory tab carries a Return link in A1,
        # borders around its data rows, and the standard "freeze rows 1-14"
        # so the equipment list scrolls under a static header. Imported
        # spares workbooks come without these conventions; generated
        # device reports already have them but reapplying is idempotent.
        for _ip, _name, display_title in summary_items:
            tab = display_to_tab.get(display_title, display_title)
            if tab not in sheetname_set:
                continue
            ws = wb[tab]
            try:
                builder.ensure_return_link_in_a1(ws)
                builder.apply_device_data_borders(ws)
                ws.freeze_panes = "A15"
            except Exception as exc:
                self._append_log(f"  tab formatting failed on '{tab}': {exc}")

        # Propagate any operator-entered Asset Tag values from Summary E10+
        # onto the matching device tab's Chassis/Shelf row (column G). This
        # is a no-op on the first build (column is empty); subsequent builds
        # pick up tags the operator typed in between runs.
        try:
            tagged = builder.propagate_asset_tags_to_tabs(
                wb, display_to_tab=display_to_tab
            )
            if tagged:
                self._append_log(f"Stamped Asset Tag on {tagged} device tab(s)")
        except Exception as exc:
            self._append_log(f"  asset-tag propagation failed: {exc}")

        bom_data: Dict[str, List[Tuple[str, str, str]]] = {}
        for ip, name, display_title in summary_items:
            tab = display_to_tab.get(display_title, display_title)
            if tab not in sheetname_set:
                self._append_log(f"  skip '{display_title}' — tab '{tab}' not found")
                continue
            try:
                entries = builder._collect_bom_entries_from_sheet(wb[tab])
            except Exception as exc:
                self._append_log(f"  scan failed for '{tab}': {exc}")
                entries = []
            if entries:
                bom_data[display_title] = entries

        # Pre-salvage existing BOM so its entries also get normalized below.
        # _build_bom_sheet runs the same salvage internally but only fills
        # missing sites; doing it here lets normalize_long_part_numbers see
        # the salvaged rows.
        existing_inventory_name = find_inventory_sheet_name(wb)
        if existing_inventory_name is not None:
            try:
                salvaged = builder._scan_existing_bom(
                    wb[existing_inventory_name]
                )
            except Exception as exc:
                self._append_log(
                    f"  existing {existing_inventory_name!r} salvage failed: {exc}"
                )
                salvaged = {}
            for st, entries in salvaged.items():
                if st not in bom_data or not bom_data[st]:
                    bom_data[st] = entries
            self._append_log(f"Salvaged from existing BOM: {len(salvaged)} sites")

        rewrites = builder.normalize_long_part_numbers(bom_data)
        if rewrites:
            self._append_log(f"Part-number normalization: rewrote {rewrites} entries")
        else:
            self._append_log("Part-number normalization: nothing to rewrite")

        builder._build_bom_sheet(wb, summary_items, bom_data, display_to_tab=display_to_tab)

        retargeted = builder.retarget_return_links_to_bom(wb)
        if retargeted:
            self._append_log(f"Retargeted {retargeted} device-tab Return links to BOM")

        builder._append_summary_timestamp(wb["Summary"], "BoM Updated")

        # When the Summary was synthesized, drop template/aggregate tabs
        # from the output — operators have asked that the inventory
        # workbook not carry the Customer-project template or the Task
        # Order roll-up sheet (T##/TO##) the inventory aggregate is
        # meant to replace. Never delete the Summary or inventory tabs
        # themselves; accept both new and legacy inventory names so
        # workbooks built before the rename keep their BOM sheet.
        if synthesized_summary:
            for name in list(wb.sheetnames):
                if name in (
                    "Summary", INVENTORY_TAB_NAME, _LEGACY_INVENTORY_TAB_NAME,
                ):
                    continue
                if _is_non_inventory_tab(name):
                    del wb[name]
                    self._append_log(
                        f"Dropped template/aggregate tab '{name}' from "
                        f"inventory workbook"
                    )

        out_path = self._derive_out_path(src_path)
        wb.save(out_path)
        return out_path

    # ------------------------------------------------------------------
    # Summary synthesis (for workbooks that don't have one)
    # ------------------------------------------------------------------

    def _synthesize_summary(self, wb: Any, builder: Any) -> int:
        """Create a ``Summary`` sheet in *wb* by scanning each existing tab
        for part-number rows.

        Tabs that yield at least one BOM entry via the standard column
        layout (Name=B, Type=C, PartNo=D, Description=F starting at row 15)
        are treated as device/inventory tabs and surface as a row on the
        new Summary. The IP column is left blank because Spares-style
        workbooks don't have one per tab; the Device Name column carries
        the tab name and hyperlinks back to it.

        Customer / Project are pulled from a 'Customer-project' header
        tab if one exists (cells C5 / C6, matching the device-report
        template), else left blank.

        Returns the number of inventory rows added to the Summary table.
        """
        from openpyxl.workbook import Workbook  # type: ignore[import-not-found]

        # Identify inventory tabs by trying the standard parser. Template /
        # aggregate tabs (Customer-project, T03, TO4, ...) are flagged and
        # excluded even if they happen to have part rows, because they're
        # not real inventory destinations — the BOM itself is what they
        # roll up into.
        inventory_tabs: List[str] = []
        for tab_name in wb.sheetnames:
            ws = wb[tab_name]
            try:
                entries = builder._collect_bom_entries_from_sheet(ws)
            except Exception as exc:
                self._append_log(f"  scan failed for '{tab_name}': {exc}")
                entries = []
            if _is_non_inventory_tab(tab_name):
                self._append_log(
                    f"  - '{tab_name}' (template/aggregate tab — excluded)"
                )
                continue
            if entries:
                inventory_tabs.append(tab_name)
                self._append_log(
                    f"  + '{tab_name}' — {len(entries)} part row(s)"
                )
            else:
                self._append_log(f"  - '{tab_name}' (no parts in standard layout)")

        # Best-effort customer / project metadata from a header tab.
        customer, project = self._extract_header_metadata(wb)

        summary_sheet = wb.create_sheet("Summary", 0)
        builder._setup_summary_sheet_header(
            summary_sheet, customer, project, list(inventory_tabs)
        )
        # Spares-style workbooks don't have a real IP per tab — leave the
        # IP column blank and let the tab name (Device Name + hyperlink)
        # carry the row's identity.
        summary_items = [("", tab, tab) for tab in inventory_tabs]
        builder._populate_summary_table(summary_sheet, summary_items, start_row=10)
        return len(inventory_tabs)

    def _augment_summary_from_device_tabs(
        self,
        wb: Any,
        builder: Any,
        summary_items: List[Tuple[str, str, str]],
        display_to_tab: Dict[str, str],
    ) -> int:
        """Heal a truncated Summary by adding any device tabs that
        aren't represented in *summary_items*.

        Mutates ``summary_items`` and ``display_to_tab`` in place; also
        rewrites the workbook's Summary sheet so the on-disk record
        reflects every device tab. Returns the count of rows added —
        0 if Summary was already complete.

        Discovery rule: any sheet that yields BOM entries via the
        standard parser AND isn't tagged as a template/aggregate tab
        counts as a device tab. The new row's IP/Name come from the
        device tab's ``F5``/``F6`` metadata when present, falling
        back to the tab name itself (which is what
        ``_synthesize_summary`` does for Spares-style workbooks).
        """
        # Display titles already covered by the existing Summary —
        # using the canonical tab name (display_to_tab values) as the
        # key so the comparison ignores dotted-vs-underscored
        # discrepancies between Summary's Device Name and the actual
        # sheet name.
        already_covered = {
            display_to_tab.get(disp, disp).strip().lower()
            for (_ip, _name, disp) in summary_items
        }
        orphan_tabs: List[str] = []
        for tab_name in wb.sheetnames:
            if tab_name in (
                "Summary", INVENTORY_TAB_NAME, _LEGACY_INVENTORY_TAB_NAME,
            ):
                continue
            if _is_non_inventory_tab(tab_name):
                continue
            if tab_name.strip().lower() in already_covered:
                continue
            ws = wb[tab_name]
            try:
                entries = builder._collect_bom_entries_from_sheet(ws)
            except Exception as exc:
                self._append_log(f"  scan failed for '{tab_name}': {exc}")
                continue
            if not entries:
                continue
            orphan_tabs.append(tab_name)

        if not orphan_tabs:
            return 0

        # Build the new summary_items entries from F5/F6 when
        # available, falling back to tab-name-as-name with no IP.
        for tab_name in orphan_tabs:
            ws = wb[tab_name]
            ip_val = ws["F5"].value if ws["F5"].value is not None else ""
            name_val = ws["F6"].value if ws["F6"].value is not None else ""
            ip = str(ip_val).strip()
            display = str(name_val).strip() or tab_name
            summary_items.append((ip or display, display, display))
            display_to_tab[display] = tab_name

        # Rewrite the Summary sheet so the healed workbook carries the
        # full site list on disk going forward. Uses the same
        # _populate_summary_table the regular inventory build uses so
        # the layout stays consistent.
        summary_sheet = wb["Summary"]
        # Clear the prior data rows (10..end) before repopulating, so
        # stale rows from a partial run don't bleed through.
        for row in range(10, (summary_sheet.max_row or 10) + 1):
            for col_letter in ("B", "C", "D", "E"):
                cell = summary_sheet[f"{col_letter}{row}"]
                cell.value = None
                cell.hyperlink = None
        builder._populate_summary_table(
            summary_sheet, summary_items, start_row=10,
        )
        return len(orphan_tabs)

    @staticmethod
    def _extract_header_metadata(wb: Any) -> Tuple[str, str]:
        """Pull customer / project from a 'Customer-project' or similar tab.

        Looks for the device-report template's C5/C6 cells. Returns ("", "")
        if nothing usable is found, which lets the synthesized Summary still
        save cleanly.
        """
        candidate_names = [
            n for n in wb.sheetnames
            if re.search(r"customer|project|header|cover", n, re.IGNORECASE)
        ]
        for name in candidate_names:
            ws = wb[name]
            try:
                c5 = ws["C5"].value
                c6 = ws["C6"].value
            except Exception:
                continue
            customer = str(c5).strip() if c5 else ""
            project  = str(c6).strip() if c6 else ""
            if customer or project:
                return customer, project
        return "", ""

    # ------------------------------------------------------------------
    # Summary parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_summary(
        ws: Any, sheetnames: set
    ) -> Tuple[List[Tuple[str, str, str]], Dict[str, str]]:
        """Walk Summary and pull (ip, name, display_title) rows.

        For each row we look at every cell — not just the first non-empty
        one — for either (a) a hyperlink whose target is a worksheet, or
        (b) a value that is itself an existing sheet name. The display
        label is that cell's value; the underlying tab is the resolved
        target (so a label like "STJO Extra Materials" → tab "STJO" still
        gets aggregated).
        """
        items: List[Tuple[str, str, str]] = []
        display_to_tab: Dict[str, str] = {}
        seen: set = set()

        ip_re = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

        for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 0):
            label_cell = None
            label = ""
            tab = ""
            for cell in row:
                v = cell.value
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                resolved = _resolve_hyperlink_target(cell)
                if resolved and resolved in sheetnames:
                    label_cell, label, tab = cell, s, resolved
                    break
                if s in sheetnames:
                    label_cell, label, tab = cell, s, s
                    break
            if not label_cell or label in seen:
                continue
            seen.add(label)

            ip = ""
            for cell in row:
                if cell is label_cell:
                    continue
                v = cell.value
                if v is None:
                    continue
                s = str(v).strip()
                if ip_re.match(s):
                    ip = s
                    break

            items.append((ip or label, label, label))
            display_to_tab[label] = tab

        return items, display_to_tab

    # ------------------------------------------------------------------
    # Output path
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_out_path(src_path: str) -> str:
        base, ext = os.path.splitext(src_path)
        return f"{base}.BOM{ext or '.xlsx'}"
