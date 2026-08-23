# Packing Slip Logic Flow — Complete Walkthrough

## Overview

There are two completely separate entry points for generating packing slips in ATLAS:

1. **"Packing Slip Generator" mode** — standalone mode; user uploads an existing inventory file
2. **Post-inventory path** — triggered automatically after a full device scan completes

Both paths ultimately call the same `WorkbookBuilder.build_packing_slip_workbook()` method to produce the Excel output.

---

## Entry Point 1: "Packing Slip Generator" Mode

The user selects this mode from the radio buttons at the top of the GUI. `PackingSlipFrame` (`gui/packing_slip_frame.py`) owns all widgets and logic for this path.

---

### Step 1 — File Upload (`upload_file`)

The user clicks **Browse** and selects a CSV or Excel file. The frame detects the file type:

| File Type | Handling |
|---|---|
| `.csv` | Read with `pd.read_csv()` into a flat DataFrame |
| `.xlsx` — single sheet | Read with `pd.read_excel()` into a flat DataFrame |
| `.xlsx` — multi-sheet (device report) | Detected as `_multisheet_device_file = True`; non-Summary sheets counted as individual devices |

The file label updates to `✓ filename (N rows)` or `✓ filename (N device(s))` in green. Unsupported formats show an error dialog.

**File validation (security-hardened):**
- Symlinks are rejected
- File size limit enforced
- Magic bytes checked to verify the file matches its claimed extension
- Formula injection characters (`=`, `+`, `-`, `@`) are sanitized in all cell values via `_sanitize_cell()`

**Metadata auto-population:** When a multi-sheet Excel file is loaded, ATLAS automatically reads project metadata from the template cell layout and pre-fills the project information fields:

| Source cells | Field populated |
|---|---|
| Device sheet C5 | Customer |
| Device sheet C6 | Project |
| Device sheet C7 | Purchase Order |
| Device sheet D7 | Sales Order |
| Summary sheet B7 | Customer (fallback) |
| Summary sheet D7 | Project (fallback) |

Purchase Order and Sales Order default to `"TBD"` if not found.

---

### Step 2 — Project Info + Generate

The user fills in (or confirms auto-populated) four fields:
- **Customer**
- **Project**
- **Purchase Order**
- **Sales Order**

Clicking **Generate Packing Slips** calls `generate_packing_slips_from_file()`, which:
1. Validates all four fields are filled
2. Disables the Run button and sets status to "Processing..."
3. Creates a `tempfile.mkdtemp()` working directory (owner-only permissions, `chmod 0o700`)
4. Routes to one of two file parsers based on `_multisheet_device_file` flag

---

### Step 3 — File Parsing (Two Paths)

#### Parser A — `_process_multisheet_device_file(file_path)` (multi-sheet Excel)

Used when the uploaded file has multiple sheets (e.g., a full device inventory report).

```
For each sheet in workbook:
  ├─ Extract source IP from cell [row 4, col 5] → use as dict key
  │   └─ Falls back to sheet name if cell is blank/nan
  │   └─ Appends sheet name suffix if key already exists (deduplication)
  ├─ Scan rows to find header row containing 'PART NUMBER' or 'SERIAL NUMBER'
  │   └─ Sheets with no matching header (e.g., Summary) are silently skipped
  ├─ Read sheet data starting from detected header row
  ├─ Drop rows where Part Number, Serial Number, AND Description are all empty/nan
  ├─ Apply sentinel truncation: discard all rows from 'ADDITIONAL NODE INFORMATION' onward
  │   └─ Removes software, slot, redundancy, power, and topology sections
  │   └─ Only equipment rows (shelf/card/module) are preserved
  ├─ Insert 'System Name' column = sheet name (for workbook builder device naming)
  └─ processed_data[ip_address] = cleaned DataFrame
```

#### Parser B — `_process_file_for_packing_slip(df)` (flat CSV or single-sheet Excel)

Used when the uploaded file is a single flat table.

```
Detect device grouping column by scanning column names (priority order):
  1. 'system name'
  2. 'device'
  3. 'ip address'
  4. ' ip' (bare 'ip' avoided — matches 'Description')
  5. 'name' (fallback)

If grouping column found:
  └─ groupby(device_key) → one DataFrame per unique device ID value

If no grouping column found:
  └─ Entire file treated as one device → {'Device_0': df}
```

Both parsers return `processed_data: Dict[str, DataFrame]` mapping device ID → rows.

---

### Step 4 — Workbook Builder (`WorkbookBuilder.build_packing_slip_workbook`)

Called with `processed_data`, the project info fields, and the temp directory as save folder.

```
build_packing_slip_workbook(processed_data, ip_list, customer, project,
                            customer_po, sales_order, save_folder)
  │
  ├─ Verify data/ATLAS_Packing_Slip.xlsx template exists (raises FileNotFoundError if not)
  ├─ Copy template → PackingSlip_Temp.xlsx (working copy, cleaned up in finally block)
  ├─ Detect summary sheet and device template sheet by name ("summary" substring)
  ├─ Sort devices by IP sort key (numeric octets where possible)
  │
  └─ For each device (sorted order):
      │
      ├─ Detect device name — scans columns in priority order:
      │     1. 'system name'
      │     2. 'device name'
      │     3. 'hostname'
      │     4. any column containing 'name'
      │     Falls back to 'Device_N' if none found
      │
      ├─ Detect row layout by device family:
      │     'rls' / 'psi' / '1830' family → _populate_report_layout_packing_slip_sheet()
      │         └─ Slot/Port identifier written to column B; shifts part#/serial#/desc right
      │     'default' family              → _populate_default_packing_slip_sheet()
      │         └─ Standard B=SO, C=PO, D=Part#, E=Serial#, F=Description layout
      │
      ├─ Copy template sheet → new sheet (title = device_name, max 31 chars, alphanumeric)
      │
      ├─ Write header cells:
      │     A1 = "Return" hyperlink → Summary sheet
      │     C5 = Customer
      │     C6 = Project
      │     C7 = Device Name
      │
      ├─ Write inventory rows starting at row 15 (case-insensitive column lookup):
      │     B = Sales Order
      │     C = Customer PO
      │     D = Part Number  (fallback: Model Number if Part Number empty)
      │     E = Serial Number
      │     F = Description
      │     Rows where all three of Part#/Serial#/Description are empty → skipped
      │
      └─ autosize_sheet_columns(new_sheet)
  │
  ├─ Populate Summary sheet rows (starting row 7):
  │     B = Customer, D = Project, F = IP Address
  │     H = Device Name (hyperlinked to that device's sheet)
  │
  ├─ autosize Summary sheet columns
  ├─ Move Summary sheet to first tab position
  ├─ Delete the original blank device template sheet
  └─ Save → PackingSlip_{Customer}_{Project}_{YYYY-MM-DD}.xlsx in save_folder

Returns: absolute path to the saved file
```

**Note on `build_unified_packing_slip_workbook()`:** This method exists as a wrapper but per-family packing slip template routing has been removed. It delegates directly to `build_packing_slip_workbook()` using the standard `ATLAS_Packing_Slip.xlsx` template for all device families. The per-family templates (`Nokia_PSI_Packing_Slip.xlsx`, `Ciena_RLS_Packing_Slip.xlsx`) exist in `data/` but are not currently used.

---

### Step 5 — Print Selection Dialog (`_show_print_selection_dialog`)

After the workbook builder returns, a `Toplevel` dialog opens with:
- A **scrollable checklist** of all device sheet names (all checked by default)
- **Select All / Deselect All** buttons
- An **Output Mode** toggle (radio buttons):

| Mode | Behavior |
|---|---|
| **Consolidated** | All selected devices merged into a single workbook using `data/ATLAS_Consolidated_Packing_Slip.xlsx` |
| **Individual** | One separate workbook per selected device |

Clicking **Save / Open** calls `_print_selected_sheets()`.

The dialog scrollbar is only shown when more than 10 device sheets are present. Canvas height scales with device count (min 300 px, ~28 px per device).

---

### Step 6 — Output (`_print_selected_sheets`)

#### Consolidated Mode

```
Prompt for save path (initialfile = PackingSlip_{Customer}_{Project}_Consolidated.xlsx)

Load data/ATLAS_Consolidated_Packing_Slip.xlsx
  (falls back to per-device template if consolidated template missing)

Write header cells:
  C5 = Customer, C6 = Project

For each selected device sheet (in selection order):
  Read rows 15+ from source workbook
  For each row: skip if Part#, Serial#, and Description are all empty/None/nan
  Write to consolidated sheet:
    B = Device ID (sheet name)
    C = Customer PO
    D = Part Number
    E = Serial Number
    F = Description

autosize_sheet_columns()
Save → user-chosen path
os.startfile(save_path)  → opens in Excel
```

> **Note:** Consolidated column B is Device ID (not Sales Order). This differs from the per-device sheet layout where B = Sales Order.

#### Individual Mode

```
Prompt for save folder

For each selected device sheet:
  Load source workbook
  Delete all sheets EXCEPT Summary + this device's sheet
  autosize all remaining sheets
  Save → {base_name}_{DeviceName}.xlsx in chosen folder
  os.startfile(save_path)  → opens each file in Excel

Log: "Saved N individual packing slip(s) to: folder"
```

---

### Cleanup

The temp directory (`tempfile.mkdtemp()`) and `PackingSlip_Temp.xlsx` working copy are both deleted in `finally` blocks regardless of success or failure.

---

## Entry Point 2: Post-Inventory Path (After Scan)

After a full device scan and Excel export complete, `poll_run_queue()` can trigger packing slip generation if the user responds **Yes** to the post-export packing slip prompt. This uses `self.outputs` (the live scan data dictionary) rather than an uploaded file.

```
poll_run_queue() receives "export_complete"
  └─ Show dialog: "Do you need packing slips for this inventory?"
      ├─ NO  → run complete
      └─ YES → start_export_worker(processed_data, packing_slip=True)
                └─ run_packing_slip_worker(context, processed_data)
                    └─ WorkbookBuilder.build_packing_slip_workbook(
                         processed_data=self.outputs,
                         ip_list=context["ip_list"],
                         customer=context["customer"],
                         project=context["project"],
                         customer_po=context["customer_po"],
                         sales_order=context["sales_order"],
                         save_folder=<user-chosen folder>
                       )

Result arrives on queue:
  "packing_complete" → messagebox.showinfo("Packing slips saved to: ...")
  "packing_error"    → messagebox.showerror(...)
```

---

## Template Files

| Template | Location | Used By | Status |
|---|---|---|---|
| `ATLAS_Packing_Slip.xlsx` | `data/` | Per-device workbook (Summary sheet + one blank device sheet) | ✅ Active |
| `ATLAS_Consolidated_Packing_Slip.xlsx` | `data/` | Consolidated output (single sheet; Device ID in col B) | ✅ Active |
| `Nokia_PSI_Packing_Slip.xlsx` | `data/` | Nokia PSI per-device layout | ⚠️ Defined, not currently used |
| `Ciena_RLS_Packing_Slip.xlsx` | `data/` | Ciena RLS per-device layout | ⚠️ Defined, not currently used |

---

## Key Data Flow Summary

```
User uploads file (CSV / single-sheet Excel / multi-sheet device report)
    ↓
Metadata auto-populated from template cells (C5/C6/C7/D7)
    ↓
File parser detects structure → processed_data: {device_id: DataFrame}
  Sentinel truncation: rows after "ADDITIONAL NODE INFORMATION" discarded
    ↓
WorkbookBuilder.build_packing_slip_workbook()
  Copy ATLAS_Packing_Slip.xlsx → per-device sheets (row 15+ = inventory rows)
  Summary sheet (row 7+ = device index with hyperlinks)
  Family detection → standard or report-layout row writer
  Save to temp dir
    ↓
Print Selection dialog
  User selects devices + output mode (Consolidated or Individual)
    ↓
  Consolidated: merge all selected rows → ATLAS_Consolidated_Packing_Slip.xlsx → os.startfile
  Individual:   one workbook per device → {base}_{DeviceName}.xlsx → os.startfile each
    ↓
Temp directory cleaned up
```

---

## Key Data Structures

### Column Layout — Per-Device Sheet (rows 15+)

| Column | Content | Notes |
|--------|---------|-------|
| B | Sales Order | Repeated on every data row |
| C | Customer PO | Repeated on every data row |
| D | Part Number | Falls back to Model Number if blank |
| E | Serial Number | |
| F | Description | DB lookup or parser-provided |

### Column Layout — Consolidated Sheet (rows 15+)

| Column | Content | Notes |
|--------|---------|-------|
| B | Device ID | Sheet name of origin device |
| C | Customer PO | |
| D | Part Number | |
| E | Serial Number | |
| F | Description | |

### Summary Sheet Layout

| Cell / Column | Content |
|---|---|
| Row 5 headers | B=Customer, D=Project, F=IP Address, H=Device Name |
| Rows 7+ data | B=index, F=IP, H=device name (hyperlinked to device tab) |
| J6 | Total device count |

### Family Detection (for row layout selection)

The workbook builder scans the `Type` column in each device's DataFrame for family keywords:

| Keyword match | Family | Row writer used |
|---|---|---|
| `"rls"` / `"ciena"` | `"rls"` | `_populate_report_layout_packing_slip_sheet()` |
| `"psi"` / `"1830"` / `"nokia"` | `"psi"` | `_populate_report_layout_packing_slip_sheet()` |
| *(no match)* | `"default"` | `_populate_default_packing_slip_sheet()` |

The report-layout writer places a Slot/Port identifier in column B and shifts all other columns one position right relative to the default layout.

### Sentinel Row Truncation

When parsing multi-sheet Excel files (e.g., Nokia PSI or Ciena RLS inventory reports), rows matching the pattern `ADDITIONAL\s+NODE\s+INFORMATION` mark the boundary between equipment rows and supplemental data sections. All rows from the sentinel onward are discarded. This removes software release info, slot programming states, redundancy/power feed rows, and interface topology — leaving only the shelf/card/module hardware rows in the packing slip.
