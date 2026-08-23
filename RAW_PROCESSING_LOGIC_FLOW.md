# Raw File Processing Logic Flow — Complete Walkthrough

## Overview

Raw File Processing is the fourth mode in ATLAS (selectable via radio button). It allows a user to feed saved CLI transcripts — captured offline from a device — into the same parsing and export pipeline used by a live inventory scan. No network connection is required.

**Key design principle:** The raw pipeline reuses every device-family script (`Nokia_PSI`, `Nokia_SAR_Raw`, `Nokia_IXR_Raw`, `Ciena_6500`, `Ciena_RLS`, etc.) without modification. The mode handles transcript splitting, device ID normalization, and DataFrame patching, then hands off to the standard `WorkbookBuilder` methods for Excel output.

`RawFrame` (`gui/raw_frame.py`) owns all UI and processing logic for this mode.

---

## Supported Input Formats

| Input | Handling |
|---|---|
| `.txt` file | Single device; entire file is raw CLI transcript |
| `.xlsx` / `.xls` — single sheet | Single device; column A = transcript lines, one per row |
| `.xlsx` / `.xls` — multi-sheet | Multi-device; each sheet is a separate device |
| Folder of `.txt` files | Multi-device; all `.txt` files discovered recursively |

---

## Step 1 — File / Folder Selection

The user clicks **Browse File…** or **Browse Folder…**.

### File (`_browse_file`)

- Non-Excel files: label set to filename; status set to "Text file loaded".
- Excel files: `openpyxl` opens the workbook in read-only mode to count sheets.
  - Sheet names preview (up to 6) shown in the `_sheet_info_var` label (steel blue).
  - If `count > 1`: status indicates multi-device mode and each sheet will be a separate device.
  - If `count == 1`: status shows single-sheet mode.

### Folder (`_browse_folder`)

- All `.txt` files found recursively via `Path.rglob("*")`.
- File count shown in the label; up to 6 filenames previewed in `_sheet_info_var`.
- If no `.txt` files found, status warns the user.

---

## Step 2 — Processing Options

| Widget | Description |
|---|---|
| **Device Type** dropdown | Selects the parser module; defaults to "Auto Detect Nokia" |
| **Device ID** field | Override device name for single-file inputs only; ignored for multi-device inputs |

### Device Type Options

| Display Name | Module Path | Notes |
|---|---|---|
| Auto Detect Nokia | `""` (empty) | Triggers `_detect_nokia_raw_script()` at parse time |
| Nokia PSI | `scripts.Nokia_PSI` | Nokia 7705/7250 PSI chassis format |
| Nokia 1830 | `scripts.Nokia_1830` | Nokia 1830 OLS format |
| Nokia SAR | `scripts.Nokia_SAR_Raw` | Nokia 7705 SAR raw format |
| Nokia IXR | `scripts.Nokia_IXR_Raw` | Nokia 7250 IXR raw format |
| Ciena 6500 | `scripts.Ciena_6500` | Ciena 6500 format |
| Ciena RLS | `scripts.Ciena_RLS` | Ciena RLS format |

---

## Step 3 — Run (`_on_run`)

Clicking **Process Input**:

1. Validates `_input_path` exists on disk.
2. Validates selected script name is in `SCRIPT_OPTIONS`.
3. Resolves the effective Device ID: `_device_id_var` entry if filled, else `Path(input_path).stem`.
4. Disables the **Process Input** button; sets status to "Processing…"; clears the log.
5. Spawns a **daemon thread** (`threading.Thread(daemon=True)`) running `_worker()`.

All subsequent progress updates are posted to the main thread via `self.after(0, ...)`.

---

## Step 4 — Worker Thread (`_worker`)

Determines input type and dispatches accordingly:

```
_worker(file_path, device_id, script_name)
  │
  ├─ Path is a directory?
  │     → _read_text_folder(file_path) → devices: {normalized_id: raw_text}
  │     → _process_multi(devices, script_name)
  │
  ├─ Extension is .xlsx / .xls?
  │     → _read_excel_sheets(file_path) → sheets: {sheet_name: raw_text}
  │     ├─ Single sheet → _process_single(text, normalized_name, script_name)
  │     └─ Multi sheet  → _normalize_device_map(sheets)
  │                        → _process_multi(normalized_sheets, script_name)
  │
  └─ Plain text (.txt or other)?
        → Path.read_text(encoding="utf-8", errors="replace")
        → _process_single(raw_text, normalized_device_id, script_name)
```

### Excel Sheet Reading (`_read_excel_sheets`)

`openpyxl` opens the workbook in read-only / data-only mode. For each sheet, column A values are read row by row (blank rows preserved as empty strings) and joined into a single newline-delimited string. Returns `{sheet_name: raw_text}`.

### Folder Reading (`_read_text_folder`)

Iterates `Path.rglob("*")`, collecting `.txt` files sorted by path. Each file stem is passed through `_normalize_device_id()` and de-duplicated by appending `_02`, `_03`, etc. Returns `{device_id: raw_text}`.

---

## Step 5 — Device ID Normalization (`_normalize_device_id`)

Strips leading timestamp prefixes that appear on Nokia CPAM capture exports, e.g.:

```
"11-27-2023 - 20.33 ET - USDEN5-L9O1"  →  "USDEN5-L9O1"
```

The regex matches formats like `MM-DD-YYYY - HH.MM TZ - ` (with date separators `-`, `_`, `/`; time separators `.` or `:`; optional AM/PM and timezone). Falls back to the original string if the pattern does not match, or `"Manual"` if the result is empty.

---

## Step 6 — Auto-Detection (`_detect_nokia_raw_script`)

Called only when Device Type is "Auto Detect Nokia". Builds a `haystack = device_id + "\n" + raw_text` and applies regex patterns in priority order:

| Pattern checked | Returned module |
|---|---|
| `7250` or `ixr` (word-bounded, case-insensitive) | `scripts.Nokia_IXR_Raw` |
| `7705` or `sar` (word-bounded, case-insensitive) | `scripts.Nokia_SAR_Raw` |
| Both `MDA N/N detail` AND `Chassis 1 Detail` present | `scripts.Nokia_IXR_Raw` (IXR fallback when prompt is absent) |
| No match | `RuntimeError` — user must select device type explicitly |

IXR is checked before SAR because when only output content is available (no hostname in device ID), the SAR and IXR raw formats are very similar; the IXR fallback is safer as it avoids missing IXR-specific MDA sections.

---

## Step 7 — Parsing a Single Device (`_process_single`)

```
_process_single(raw_text, device_id, script_name)
  │
  ├─ _resolve_script_module() → module_path
  ├─ _parse_device(raw_text, device_id, module_path, outputs={})
  │     → outputs: {device_id: {section_key: {"DataFrame": df, "System Info": {...}}}}
  │
  ├─ Re-key: outputs["Manual"] = outputs.pop(device_id)
  │     (Single device always appears as "Manual" in the summary IP column)
  │
  └─ family = _FAMILY_BY_MODULE.get(module_path, "default")
     self.after(0, self._export, rekeyed, family, device_id)
```

---

## Step 8 — Parsing Multiple Devices (`_process_multi`)

```
_process_multi(sheets: {device_id: raw_text}, script_name)
  │
  ├─ For each sheet:
  │     ├─ _resolve_script_module(raw_text, sheet_name, script_name) → module_path
  │     │     └─ Failures: log warning with [RAW] prefix; skip device; continue
  │     └─ _parse_device(raw_text, sheet_name, module_path, raw_outputs)
  │           → success_count++
  │
  ├─ If success_count == 0: log error, abort export
  │
  ├─ Re-key all devices as "Manual", "Manual_02", "Manual_03"…
  │     (zero-padded to match total device count width)
  │
  └─ family = _FAMILY_BY_MODULE.get(SCRIPT_OPTIONS.get(script_name, ""), "default")
     label = Path(input_path).stem  (used for output filename base)
     self.after(0, self._export, merged_outputs, family, label)
```

---

## Step 9 — Core Device Parse (`_parse_device`)

This is the central step that bridges the raw transcript to the normal script pipeline.

```
_parse_device(raw_text, device_id, module_path, outputs)
  │
  ├─ importlib.import_module(module_path) → mod
  ├─ mod.Script(ip_address=device_id, connection_type="ssh",
  │             db_cache=gui.db_cache, db_path=gui.db_file)
  │
  ├─ script_inst.get_commands() → commands: List[str]
  │
  ├─ _split_raw_output_by_commands(raw_text, commands) → outputs_list: List[str]
  │     (see Step 10)
  │
  ├─ Log: "N/M command sections matched for 'device_id'"
  │   For each command: log "command_name: N lines" or "not found"
  │
  ├─ If found == 0:
  │     → Insert placeholder row (see Placeholder Row section)
  │     → Return True  (device included in export with UNPARSED marker)
  │
  ├─ script_inst.process_outputs(outputs_list, device_id, outputs)
  │     → Populates outputs[device_id] with section DataFrames + System Info
  │
  └─ DataFrame patching (for all sections in outputs[device_id]):
        df["System Name"] = device_id   (drives Device Name + sheet tab title)
        df["Source"]      = "Manual"    (drives the IP/Source field in report)
        system_info["System Name"] = device_id
        system_info["Source"]      = "Manual"
```

Returns `True` on success or zero-match-but-placeholder; `False` only on exception.

---

## Step 10 — Command Section Splitting (`_split_raw_output_by_commands`)

Separates a raw CLI transcript into per-command output blocks.

```
Input:  raw_text (full transcript), commands: List[str]

For each line in transcript:
  ├─ Strip leading hostname prompt: re.sub(r"^[^\#]*#\s*", "", line).strip()
  │     e.g. "USDEN5-L9O1# show shelf 1"  →  "show shelf 1"
  └─ Check if cleaned line startswith any unmatched command
        → Record first occurrence of each command by line index

Build output slices:
  For each command found (sorted by line position):
    output = lines[cmd_line + 1 : next_cmd_line]

Commands not found in transcript → empty string "" in result list
Lines before the first matched command (e.g. login banner) → discarded

Returns: List[str] parallel to commands; index N = output for commands[N]
```

---

## Step 11 — Placeholder Row

When zero command sections are matched (transcript format unrecognized), the device is still included in the export with a diagnostic placeholder row:

| Column | Value |
|---|---|
| System Name | device_id |
| System Type | "Unknown" |
| Type | "No inventory sections matched" |
| Part Number | "UNPARSED" |
| Serial Number | `""` |
| Description | "Transcript did not contain expected inventory commands" |
| Name | "No Inventory Data" |
| Source | "Manual" |

---

## Step 12 — Family Detection

After all devices are parsed, the family key determines which workbook builder to call:

| Module path | Family key | Workbook builder |
|---|---|---|
| `scripts.Nokia_PSI` | `"psi"` | `build_psi_report_workbook()` |
| `scripts.Ciena_RLS` | `"rls"` | `build_unified_report_workbook()` |
| All others | `"default"` | `build_report_workbook()` |

For multi-device inputs, the family key is resolved from the **selected device type** (not per-device), since all devices in a multi-device batch must use the same script type.

---

## Step 13 — Export (`_export`, runs on main thread)

```
_export(outputs, family, device_id)
  │
  ├─ gui.get_user_inputs(default_name) → dialog for project info
  │     Fields: Customer, Project, Purchase Order, Sales Order, Filename
  │     └─ If cancelled: abort; _finish(False)
  │
  ├─ Build safe output filename:
  │     sanitize(filename) + sanitize(customer) + sanitize(project) + "Raw_Report" + timestamp
  │     e.g. "USDEN5_AcmeCo_Q2_Audit_Raw_Report_2026-05-11_14-30.xlsx"
  │
  ├─ filedialog.askdirectory() → save folder
  │     └─ If cancelled: abort; _finish(False)
  │
  ├─ output_file = os.path.join(save_dir, f"{safe_name}.xlsx")
  │
  ├─ Dispatch to workbook builder by family:
  │     "psi"     → gui.build_psi_report_workbook(outputs, output_file, ...)
  │     "rls"     → gui.build_unified_report_workbook({"rls": outputs}, output_file, ...)
  │     "default" → gui.build_report_workbook(outputs, output_file, ...)
  │
  └─ _finish(True) → re-enable button; set status "✓ Done — report saved!"
```

---

## Key Data Structure: `outputs`

The `outputs` dict populated by `_parse_device` matches the structure produced by live inventory scans:

```python
outputs = {
    device_id: {
        section_key: {
            "DataFrame": pd.DataFrame([...]),
            "System Info": {
                "System Name": device_id,
                "System Type": "...",
                "Source": "Manual",
            }
        },
        ...  # one entry per data section (hardware_data, mda_data, port_data, ...)
    }
}
```

After patching, every DataFrame has `System Name = device_id` and `Source = "Manual"` to ensure the report workbook shows the device name and flags the data as offline.

---

## Supporting Module: `RawNokiaTranscriptMixin` (`scripts/nokia_raw_transcript.py`)

Nokia raw-transcript scripts (`Nokia_SAR_Raw`, `Nokia_IXR_Raw`) inherit from this mixin. It defines a standard three-command interface for Nokia CLI transcripts.

### Commands (from `get_commands()`)

```python
[
    "show chassis detail | match expression",
    "show mda detail | match expression",
    "show port detail | match expression",
]
```

### Extraction Methods

#### `extract_hardware_data(output, cache_callback, ip)`

Parses `show chassis detail` output. Extracts:
- **Chassis**: `Chassis N Detail` block → Part Number, Serial Number
- **Fan trays**: `Fan tray number: N` blocks → Part Number, Serial Number per tray

Returns a DataFrame with `Type = "Chassis"` or `"Chassis Fan"`.

#### `extract_mda_details(output, cache_callback, ip)`

Parses `show mda detail` output. Iterates `MDA N/N detail` blocks. For each:
- Extracts Part Number and Serial Number
- Sets `Type` to DB description if available, else `"MDA"`, appended with operational state
- Sets `Information Type = "MDA Card"` and `Name = "slot_id"` (e.g., `"1/1"`)

Part numbers truncated to 10 characters to match DB key length.

#### `extract_port_detail(output, cache_callback, ip)`

Parses `show port detail` output. Iterates `Interface : <name>` blocks. For each:
- Requires both `Model Number` and `Serial Number` fields
- Skips entries where Model Number is `"none"`, `"n/a"`, or `"na"`
- Calls `_normalize_model_part_number()` to extract stable part key from vendor-prefixed model strings (e.g., `"ALCATEL 3FE62600AA03"` → `"3FE62600AA"`)
- Sets `Type` to DB description if usable, else port speed, else `"Optic"`
- Port speed detected from `Oper Speed` or `Speed` fields within the block
- Sets `Information Type = "Plugable Optical Transceiver"` and `Name = interface_name`

### Helper Methods

| Method | Description |
|---|---|
| `_normalize_model_part_number(raw)` | Extracts first alphanumeric token containing both letters and digits; truncated to 10 chars |
| `_is_usable_description(desc)` | Returns False for `"not found"`, `"unknown"`, `"invalid part number"`, `"db error"...` |
| `_find_port_speed(output, iface, match)` | Searches `Oper Speed` / `Speed` within current block, then in child interface blocks |

---

## Logging

All raw-specific log messages use the `[RAW]` prefix in the Python `logging` module:

| Event | Log level | Message format |
|---|---|---|
| Parsing starts | `INFO` | `[RAW] Parsing single device 'ID' with parser module.path` |
| Command match summary | `INFO` | `[RAW] Device 'ID': parser=... matched N/M command sections` |
| Per-command detail | `DEBUG` | `[RAW] Device 'ID': command '...' -> N lines` |
| No sections matched | `WARNING` | `[RAW] Device 'ID': no inventory sections matched; adding placeholder row` |
| `process_outputs` done | `INFO` | `[RAW] Device 'ID': process_outputs completed` |
| Multi-sheet complete | `INFO` | `[RAW] Multi-sheet parse complete: N/M devices parsed into export payload` |
| Export abort | `WARNING` | `[RAW] Export aborted: no devices produced data` |
| Parser resolution failure | `WARNING` | `[RAW] Skipping 'sheet': parser resolution failed: ...` |
| Worker exception | `EXCEPTION` | Full traceback via `logging.exception()` |

GUI log window (`ScrolledText`) receives human-readable equivalents of these events.

---

## Complete Call Chain

```
User clicks "Process Input"
    ↓
_on_run()
  ├─ Validate input path exists
  ├─ Resolve Device ID (entry field or filename stem)
  ├─ Disable button; set status "Processing…"
  └─ daemon thread → _worker(file_path, device_id, script_name)

_worker()
  ├─ Folder  → _read_text_folder() → dict  → _process_multi()
  ├─ .xlsx   → _read_excel_sheets() → dict
  │             └─ 1 sheet  → _process_single()
  │             └─ N sheets → _normalize_device_map() → _process_multi()
  └─ .txt    → Path.read_text() → _process_single()

_process_single() / _process_multi()
  └─ for each device:
       _resolve_script_module()          ← auto-detect or explicit selection
       _parse_device()
         ├─ importlib.import_module()    ← load device family script
         ├─ Script(ip_address=device_id, ...)
         ├─ get_commands()               ← script defines expected CLI commands
         ├─ _split_raw_output_by_commands()  ← align transcript to command list
         ├─ process_outputs()            ← populate DataFrames via script parsers
         └─ patch: df["System Name"], df["Source"] = device_id, "Manual"
  └─ re-key outputs → "Manual" / "Manual_02" / ...
     self.after(0, _export, outputs, family, label)

_export()  (main thread)
  ├─ gui.get_user_inputs()              ← project info dialog
  ├─ filedialog.askdirectory()          ← choose save location
  ├─ Build sanitized filename
  └─ dispatch by family:
       "psi"     → build_psi_report_workbook()
       "rls"     → build_unified_report_workbook({"rls": outputs})
       "default" → build_report_workbook()
       → saved as {Filename}_{Customer}_{Project}_Raw_Report_{YYYY-MM-DD_HH-MM}.xlsx

_finish(success)
  └─ Re-enable button; update status label
```

---

## Key Differences from Live Inventory Mode

| Aspect | Live Inventory | Raw File Processing |
|---|---|---|
| Connection | SSH / Telnet to device | None — transcript read from disk |
| Command execution | Real-time via `run_script()` | `_split_raw_output_by_commands()` aligns saved transcript |
| `Source` field | Device IP address | `"Manual"` |
| Device ID | IP address | Filename stem or user-entered value |
| Summary IP column | Real IP | `"Manual"` / `"Manual_02"` |
| Script selection | Auto-fingerprinted by IP scan | User-selected or auto-detected from transcript content |
| Export builders | All three (fingerprint-based) | Same three, selected by family key from `_FAMILY_BY_MODULE` |
| Error handling | Per-device failures skip to next | Per-device failures logged; placeholder row inserted on zero matches |
