# ATLAS Inventory Logic Flow — Complete Walkthrough

## 1. STARTUP & INITIALIZATION (main.py)

- Logging configured → writes to `%APPDATA%\ATLAS\logs\ATLAS_YYYY-MM-DD_HH-MM-SS.log`
  - PIL debug logs suppressed: `logging.getLogger("PIL").setLevel(WARNING)`
  - Paramiko debug logs suppressed: `logging.getLogger("paramiko").setLevel(WARNING)` (eliminates kex handshake noise per connection)
- **cleanup_stale_lamis_tempfiles()** sweeps temp files left by prior crashed runs (max age 24h)
- **LoadingScreen** appears with logo
- **check_updates()** checks for program updates via `utils.update.Updater`
- **start_gui()** creates:
  - `set_host_key_prompt()` — registers a Tk dialog for SSH host-key fingerprint prompts
  - `CommandTracker`: Tracks which commands have executed per IP/connection type (singleton via `get_tracker()`)
  - `DatabaseCache`: Caches part descriptions from SQLite DB (singleton via `get_cache()`)
  - `InventoryGUI`: Main GUI window (`gui/gui4_0.py`)

---

## 2. GUI SETUP (InventoryGUI.__init__)

- Five-mode radio button selector: **Inventory | Diagnostics | Packing Slip Generator | Raw File Processing | Provisioning**
- **Inventory mode frame** builds:
  - Pod selection (100–112) with dual-pod support
  - IP range selection (start/end for each pod)
  - Device Report upload section (optional append mode)
  - Run/Pause/Abort buttons
  - Output terminal (ScrolledText widget)
- **ThreadPoolExecutor** (max_workers=2) created for background inventory/export jobs:
  - Slot 1 — inventory worker
  - Slot 2 — export worker
- **`_active_scripts{}`** + **`_active_scripts_lock`** track all concurrently executing script instances so Abort can stop all of them simultaneously
- **ConsoleRedirector** redirects stdout to the ScrolledText output terminal
- Workbook operations delegated to `WorkbookBuilder`

---

## 3. USER INITIATES INVENTORY RUN

### Step 3a: run_script() — User clicks "Run"
```
run_script()
  ├─ Check no other run is in progress
  ├─ Clear self.outputs{} dictionary
  ├─ Call collect_run_context() → returns user inputs
  └─ start_run_worker() → spawn inventory thread
```

### Step 3b: collect_run_context() — Gather User Inputs
```
collect_run_context()
  ├─ Show popup: Customer, Project, PO, SO, Filename fields
  ├─ Check append_mode: if self.inventory_report_path exists → append mode ON
  ├─ Validate IP ranges:
  │   ├─ start ≤ end for each pod (error if not)
  │   └─ Detect overlapping ranges between Pod 1 and Pod 2 (warn user)
  ├─ Build IP list from:
  │   ├─ Pod Selection 1: 10.9.{pod1}.{start1 to end1}
  │   └─ Pod Selection 2 (optional): 10.9.{pod2}.{start2 to end2}
  ├─ If append mode: use existing .xlsx file path
  └─ If new mode: generate filename with timestamp, ask for save location

  Returns context dict:
  {
    "customer": str,
    "project": str,
    "customer_po": str,
    "sales_order": str,
    "output_file": path,
    "ip_list": [list of IPs],
    "append_mode": bool,
    "connection_mode": "Network" | "LAN" | "Serial"
  }
```

---

## 4. BACKGROUND INVENTORY WORKER THREAD

### Step 4a: run_inventory_worker(context) — Main orchestrator

#### LAN / Serial Mode (manual single-device)
```
connection_mode in ("LAN", "Serial"):
  ├─ _build_manual_script_instance(context)  → script chosen by user from dropdown
  ├─ task_queue.put((target_id, script_instance))
  └─ process_task_queue(queue)               → sequential single-device execution
```
No ping or identification step — goes straight to execute.

#### Network Mode (IP list)

```
run_inventory_worker(context)
  │
  ├─ CommandTracker.reset()   ← per-run reset; clears all prior command dedup state
  │
  ├─ PHASE 1: PING ALL IPs
  │   └─ ThreadPoolExecutor(max_workers=min(20, n))
  │       ├─ is_reachable(ip) → ICMP ping (Windows "ping -n 1")
  │       ├─ alive  → reachable_ips[]
  │       ├─ dead   → failed_ips[ip] = "Unreachable"
  │       └─ Progress: "Pinging X/Y"
  │
  └─ PHASE 2: CONCURRENT SCAN (identify + execute, 5 at a time)
      └─ ThreadPoolExecutor(max_workers=min(5, reachable), thread_name="atlas-scan")
          ├─ Each worker: _process_single_device(ip, queue)
          └─ Progress: "Scanning X/Y"
```

### Step 4b: _process_single_device(ip) — Per-device pipeline

Each of the 5 concurrent workers runs this full sequence independently (no shared state).

#### Phase A — Identify
```
DeviceIdentifier().identify_device(ip)
  │
  ├─ 1830 Telnet probe first (only if Telnet policy allows)
  │   └─ If succeeds → return ("1830", hostname)  [_keep_client=False]
  │
  ├─ SSH banner inspection (no login required)
  │   └─ If fingerprint match → return (device_type, device_name)
  │
  └─ SSH login — try credentials in order:
       1. admin / admin        ← most common (Nokia SAR/IXR, Smartoptics)
       2. cli  / admin         ← Nokia 1830 SSH-level login
       3. su   / Ciena123      ← Ciena 6500 / RLS
       4. user input           ← if all fail → CredentialPromptRequired → parked

     On successful login:
       invoke_shell() → drain post-login banner → fingerprint match
       Run _IDENT_COMMANDS → parse_device_info() → (device_type, device_name)

     On non-1830 success:
       _keep_client = True
       self._identified_client = ssh_client  ← SSH transport stays OPEN
       shell channel (session) closed
       device_identifier.take_identified_client() retrieves the kept transport
```

**Device fingerprint matching precedence** (first match wins):

| Priority | Device Type | Fingerprint Pattern |
|----------|-------------|---------------------|
| 1 | Ciena 6500 RLS | `6500.*Reconfigurable.*Line.*System` \| `ciena.*rls` |
| 2 | Ciena 6500 | `ciena.*6500` |
| 3 | Nokia 1830 | `Nokia.*1830.*PSS` |
| 4 | Nokia 7250 IXR-R6/R6d | `Nokia.*7250.*IXR[-\s]?R6D?` |
| 5 | Nokia 7705 SAR-8 v2 | `Nokia.*7705.*SAR` |
| 6 | Smartoptics DCP | `DCP-[A-Z0-9\-+]+.*DWDM Open Line System` |

> RLS must precede the generic 6500 pattern because an RLS device matches both; the more specific check goes first.

#### Phase B — Script selection + SSH injection
```
ScriptSelector.select_script(device_type, ip, conn_type, stop_callback)
  └─ Normalized device_type maps to script class:
      ├─ "7250 ixr-r6" / "7250 ixr-r6d" → Nokia_IXR
      ├─ "7705 sar-8 v2"                 → Nokia_SAR
      ├─ "smartoptics dcp"               → Smartoptics_DCP
      ├─ "1830"                          → Nokia_1830
      ├─ "nokia psi"                     → Nokia_PSI
      ├─ "6500"                          → Ciena_6500
      ├─ "rls"                           → Ciena_RLS
      └─ No match                        → failed_ips[ip] = "Unknown device type"

kept_client = device_identifier.take_identified_client()
  └─ Non-1830: kept_client is the open SSHClient
  └─ 1830:     kept_client is None (uses fresh paramiko per run, no reuse possible)

script.set_existing_ssh_client(kept_client)
  └─ Stores as self._injected_ssh_client on the script instance
  └─ NOTE: Only Smartoptics_DCP actually uses the injected client (see Phase C)
```

#### Phase C — Execute
```
_active_scripts[ip] = script_instance   ← registered for abort support

script.execute_commands(commands)

  Smartoptics DCP (paramiko-based, SSH reuse):
    ├─ Reads _injected_ssh_client
    ├─ If SET: reuses the open transport — skips connect() + auth entirely
    │          (_owns_client=False, transport not closed in finally block)
    └─ If UNSET: opens new paramiko.SSHClient() + connect + auth

  Nokia SAR / Nokia IXR (paramiko-based, NO SSH reuse):
    ├─ Explicitly discards _injected_ssh_client even if set
    ├─ Always opens a new paramiko.SSHClient() + connect + auth
    ├─ invoke_shell()
    ├─ Run 5 commands, collect output
    └─ finally: close shell channel + close SSHClient

  Nokia 1830 (paramiko, two-stage shell login):
    ├─ No injection possible — opens fresh paramiko Transport per run
    ├─ Auth cascade: auth_none() → auth_interactive() → auth_password()
    │   (Nokia 1830 SSH server requires this non-standard handshake)
    ├─ Opens shell, answers "Username:" and "Password:" prompts interactively
    └─ 4 commands: show general name / show shelf inventory * /
                   show card inventory * / show interface inventory *

  Nokia PSI / Ciena 6500 (wexpect spawn):
    ├─ No injection possible — always spawns a fresh wexpect process
    └─ Vendor-specific interactive command sequences

  Ciena RLS (paramiko, interactive shell):
    ├─ No injection possible — opens fresh paramiko connection per run
    ├─ Interactive shell with prompt detection
    └─ 4 commands: show shelf / show slots * inventory circuit-pack /
                   show slots * inventory slots * / show software

script.process_outputs(outputs_list, ip, self.outputs)
  └─ Parses raw CLI text → structured DataFrames → self.outputs[ip]

_active_scripts.pop(ip)   ← unregistered after execution (success or error)
```

Returns `(ip, status, script_instance, family)` to pool collector:
```
status == None             → success
status == "ABORTED"        → pool shuts down immediately
status == "CREDS_REQUIRED" → parked in pause_queue
status == error string     → failed_ips[ip] = status

family == "rls"            → Ciena RLS (routes to PSI-template workbook builder)
family == "psi"            → Nokia PSI (routes to PSI-template workbook builder)
family == "default"        → all other devices (routes to standard workbook builder)
```

### Step 4c: Credential drain (after pool completes)
```
_drain_pause_queue():
  For each parked (ip, script_instance):
    ├─ Post ("creds_needed", ip) → main thread shows credential dialog
    ├─ Worker blocks on _creds_response_queue
    ├─ User enters username/password
    ├─ If script_instance is None: re-run identify_device() with explicit creds
    ├─ Inject user creds → execute_commands() retry
    └─ Success → self.outputs[ip] populated; failure → failed_ips[ip]
```

### Step 4d: Post-scan summary
```
Print "--- FAILED IPs ---" if any failed
queue.put("inventory_complete", True)
```

---

## 5. TRANSITION TO EXPORT PHASE

### Step 5a: poll_run_queue() — Main thread polling
- Every 100ms, main thread checks `self.run_queue` for events
- Event types handled:
  - `"log"` — append message to output terminal
  - `"progress"` — update progress indicator
  - `"error"` — display error, reset UI state
  - `"aborted"` — display abort message, reset UI state
  - `"creds_needed"` — show credential prompt dialog, push response to `_creds_response_queue`
  - `"inventory_complete"` → update status to "Exporting...", call `start_export_worker(None)`
  - `"export_complete"` → prompt user for packing slips
  - `"export_error"` — display export error, reset UI state
  - `"packing_complete"` — display success, reset UI state
  - `"packing_error"` — display packing slip error, reset UI state

---

## 6. EXCEL WORKBOOK GENERATION

### Step 6a: run_export_worker(context, _processed_data) — Export orchestrator

Devices collected during the scan are partitioned by family and routed to the appropriate builder:

```
run_export_worker(context, _)
  │
  ├─ Partition outputs by device_family_by_ip[ip]:
  │   ├─ "rls"     → rls_bucket     (Ciena RLS devices)
  │   ├─ "psi"     → psi_bucket     (Nokia PSI devices)
  │   └─ "default" → default_bucket (Nokia SAR, IXR, 1830, Ciena 6500, Smartoptics)
  │
  ├─ If only one family present (common case):
  │   ├─ "rls"     → build_psi_report_workbook(..., template=Ciena_RLS_Report_Template.xlsx)
  │   ├─ "psi"     → build_psi_report_workbook(..., template=Nokia_PSI_Report_Template.xlsx)
  │   └─ "default" → build_report_workbook(..., template=Device_Report_Template.xlsx)
  │
  └─ If multiple families present (mixed scan):
      └─ build_unified_report_workbook(family_buckets, ...)
          ├─ Renders each non-empty family to its own workbook using the above builders
          ├─ Copies all device sheets into a single base workbook
          ├─ Rebuilds a unified Summary sheet covering all IPs
          └─ Saves one output file
```

### Step 6b: WorkbookBuilder.build_report_workbook() — Standard devices
```
build_report_workbook(outputs, output_file, customer, project, customer_po,
                      sales_order, append_mode=False)
  │
  ├─ LOAD TEMPLATE OR EXISTING WORKBOOK
  │   ├─ If append_mode=False: Load Device_Report_Template.xlsx
  │   └─ If append_mode=True: Load existing .xlsx file
  │
  ├─ CREATE/GET SUMMARY SHEET
  │   ├─ Set capture timestamp in A1 (black bg, green font)
  │   ├─ Set Customer/Project in rows 5-7
  │   └─ Create device table headers: #, IP Address, Device Name
  │
  ├─ IF APPEND_MODE: Read existing summary to detect duplicate IPs
  │   ├─ Extract IP list from existing Summary sheet rows 10+
  │   └─ Extract device sheet titles from hyperlinks
  │
  ├─ PROCESS EACH IP'S INVENTORY DATA (sorted by IP)
  │   └─ For each IP in sorted order:
  │       ├─ combine_and_format_data(data_dict)
  │       │   └─ Merge all command outputs into unified DataFrame
  │       ├─ Extract System Name, System Type, Source
  │       ├─ IF APPEND_MODE && IP exists in summary_index:
  │       │   └─ Delete old sheet for this IP (replace)
  │       ├─ Create new sheet (copy from template)
  │       ├─ Add "Back to Summary" hyperlink in A1
  │       ├─ Populate headers: Customer, Project, PO, SO, Source, System Name, Type
  │       ├─ FOR EACH inventory row (Part Number, Serial, Type, Name):
  │       │   ├─ Lookup part description from DB cache (first 10 chars of part number used as key)
  │       │   ├─ Write to Excel row 15+: Name, Type, Part #, Serial #, Description
  │       │   └─ db_cache.lookup_part(part_number[:10])
  │       ├─ autosize_sheet_columns(new_sheet)
  │       └─ Add to summary_index[ip] = (system_name, sheet_title)
  │
  ├─ POPULATE SUMMARY SHEET ROWS 10+
  │   └─ For each IP in summary_index (sorted by IP):
  │       ├─ Row #: Sequence number
  │       ├─ Column C: IP address
  │       ├─ Column D: Device name (hyperlinked to device tab)
  │       └─ F7: Comma-separated IP list
  │
  ├─ REORDER WORKBOOK TABS
  │   ├─ Summary first
  │   ├─ Device tabs in IP order
  │   └─ Any remaining tabs at end
  │
  ├─ SAVE WORKBOOK → wb.save(output_file)
  └─ Return processed_data dict
```

### Step 6c: WorkbookBuilder.build_psi_report_workbook() — Nokia PSI and Ciena RLS

Used for Nokia PSI and Ciena RLS devices, which have a richer, multi-section sheet layout. Accepts a `psi_template_path` override so the same method serves both PSI (Nokia_PSI_Report_Template.xlsx) and RLS (Ciena_RLS_Report_Template.xlsx).

Per-device sheet layout:
```
Rows 15–54:   Equipment inventory (shelf / card / module)
Rows 62–71:   Software information
Rows 74–103:  Slot status
Rows 106–115: Redundancy information
Rows 118–127: Power feed status
Rows 130–169: Interface topology
```

Expected DataFrame keys in `outputs[ip]`:
`shelf_detail`, `shelf_inventory`, `card_inventory`, `module_inventory`,
`software_info`, `slot_info`, `redundancy_info`, `power_info`, `topology`

### Step 6d: WorkbookBuilder.build_unified_report_workbook() — Mixed-family scans

When a single network scan returns devices of multiple families (e.g., Nokia SAR + Ciena RLS in the same IP range):

```
build_unified_report_workbook(family_buckets, output_file, ...)
  │
  ├─ For each non-empty bucket, render to a temporary workbook:
  │   ├─ "default" → build_report_workbook()   → tmp_default.xlsx
  │   ├─ "psi"     → build_psi_report_workbook() → tmp_psi.xlsx
  │   └─ "rls"     → build_psi_report_workbook() → tmp_rls.xlsx
  │
  ├─ Select base workbook (priority: default > psi > rls)
  ├─ Copy all device sheets from other family workbooks into base
  ├─ Rebuild unified Summary sheet (all IPs, sorted numerically)
  └─ Save single output_file
```

---

## 7. POST-EXPORT: OPTIONAL PACKING SLIP GENERATION

### Step 7: Prompt user for packing slips
```
poll_run_queue() receives ("export_complete", {...})
  ├─ Show dialog: "Do you need packing slips?"
  ├─ If YES:
  │   ├─ Ask user for save location
  │   └─ start_export_worker(processed_data, packing_slip=True)
  │       └─ run_packing_slip_worker(context, processed_data)
  │           └─ build_packing_slip_workbook(...)
  │               ├─ Load packing slip template
  │               ├─ Create Summary sheet with IP table
  │               ├─ For each device: create sheet with part/serial/description
  │               ├─ Auto-fit columns
  │               └─ Save workbook
  └─ If NO: Finish run
```

---

## 8. ABORT/PAUSE LOGIC

**Pause:**
- Sets `self.is_paused = True`
- All worker threads spin on `while is_paused and not stop_threads: sleep(0.1)`

**Abort:**
- Sets `self.stop_threads = True`
- Iterates `_active_scripts{}` and calls `abort_connection()` on **all** concurrently running scripts simultaneously (not just the last one)
- Also calls `abort_connection()` on `current_script_instance` (legacy LAN/Serial path)
- All workers check `if stop_threads: return` and put `("aborted", None)` signal
- `scan_pool.shutdown(cancel_futures=True)` cancels any queued-but-not-started device futures

---

## Key Data Flow Summary

```
User Input → Ping IPs (20 concurrent)
    ↓
Concurrent scan — 5 devices at a time:
  Identify (SSH probe) → keep transport alive → inject into script → Execute
    ↓
Parse outputs → Populate self.outputs{}
    ↓
Partition by device family (rls / psi / default)
  → Route to matching template builder → Export to Excel
  Summary sheet + per-device tabs (IP-sorted)
    ↓
(Optional) Generate packing slips from processed data
```

The entire process is **threaded** to avoid GUI freezing, with **cooperative abort** via `should_stop()` callbacks and **forced abort** via `abort_connection()`.

---

## Key Components

### Threading Architecture
- **Main Thread**: GUI event loop, polling `self.run_queue` every 100ms
- **ThreadPoolExecutor** (max_workers=2): Top-level pool — one slot for the inventory worker, one for the export worker
- **Ping Pool**: `ThreadPoolExecutor(max_workers=min(20, n))` — ICMP probes, created inside inventory worker
- **Scan Pool**: `ThreadPoolExecutor(max_workers=min(5, n), thread_name="atlas-scan")` — identify+execute pipeline, created inside inventory worker
- **Device Scripts**: Directly imported and instantiated (no subprocess); support `stop_callback()`

### SSH Connection Reuse
- After identification, the `paramiko.SSHClient` transport is **kept alive** and passed to all scripts via `set_existing_ssh_client()`
- **Only Smartoptics DCP** actually uses the injected client to skip the second `connect()` + auth round-trip
- **Nokia SAR and Nokia IXR** receive the injected client but explicitly discard it and open a new paramiko connection every time
- **Nokia 1830** uses paramiko with a non-standard `auth_none → auth_interactive → auth_password` Transport cascade and two-stage shell login (`Username:` / `Password:` prompts); no reuse possible
- **Nokia PSI and Ciena 6500** use wexpect spawn; no reuse possible
- **Ciena RLS** uses a fresh paramiko interactive shell; no reuse possible

### Default Credential Order
```
1. admin / admin     — most common (Nokia SAR/IXR, Smartoptics DCP)
2. cli   / admin     — Nokia 1830 SSH-level login
3. su    / Ciena123  — Ciena 6500 / RLS
4. user input        — prompted after all defaults fail
```
Credentials are encrypted at rest in `credentials_config.json` using Fernet symmetric encryption. The seed order is enforced on upgrade via `_seed_defaults_into_config()`.

### Data Structures
- **self.outputs{}**: Nested dict — `{ip: {data_key: DataFrame}}`
  - Keys (outer): IP address strings
  - Keys (inner): Device-type-specific data keys (see Device Scripts table below)
  - All DataFrames share columns: System Name, System Type, Type, Part Number, Serial Number, Description, Name, Source

- **self.run_queue**: Queue for inter-thread communication
  - Event types: `"log"`, `"progress"`, `"inventory_complete"`, `"export_complete"`, `"export_error"`, `"packing_complete"`, `"packing_error"`, `"error"`, `"aborted"`, `"creds_needed"`

- **self._active_scripts{}**: Dict of `{ip: script_instance}` for all concurrently executing devices, guarded by `_active_scripts_lock`

- **summary_index{}**: Dict mapping IP → (device_name, sheet_title) — tracks existing devices in append mode

- **device_family_by_ip{}**: Dict mapping IP → `"rls"` | `"psi"` | `"default"` — routes export to the correct workbook builder

### Singleton Infrastructure (script_interface.py)
- **`get_tracker()`** → returns process-wide `CommandTracker` singleton
  - Deduplicates CLI commands per (IP, connection_type) pair
  - **Reset at the start of each inventory run** (`run_inventory_worker()`) — state does not persist across runs
  - Methods: `has_executed()`, `mark_as_executed()`, `reset()`
- **`get_cache()`** → returns process-wide `DatabaseCache` singleton
  - Write-through cache for SQLite part lookups (first 10 chars of part number used as key)
  - Returns `"Not Found"` or `"DB Error: ..."` on miss/failure

### External Dependencies
- **script_interface.DeviceIdentifier**: Probes devices via SSH banner → SSH login → Telnet fallback
- **script_interface.ScriptSelector**: Dynamically imports and instantiates device-specific scripts
- **script_interface.is_reachable()**: ICMP ping check (Windows `ping -n 1`)
- **openpyxl**: Excel workbook manipulation
- **pandas**: DataFrame operations for inventory data
- **sqlite3**: Part description lookups (`data/network_inventory.db`)
- **paramiko**: SSH transport (debug logs suppressed to WARNING in main.py)
- **utils/telnet.py**: Custom Telnet client (replaces deprecated telnetlib; full IAC negotiation, policy-gated via telnet_policy.py)

### Device Scripts (scripts/*.py)

All inherit from `BaseScript` (ABC defined in `script_interface.py`).

Required interface:
- `get_commands()` → `List[str]`: SSH/Telnet commands to run on device
- `execute_commands(commands)` → `Tuple[List[str], Optional[str]]`: Runs commands, returns (outputs, error)
- `process_outputs(outputs, ip, outputs_dict)`: Parses outputs and populates `outputs_dict[ip]`
- `abort_connection()`: Forcefully closes SSH/Telnet connection
- `should_stop()`: Checks `stop_callback` — called between commands for cooperative abort
- `set_existing_ssh_client(client)`: Accepts an already-authenticated SSHClient (only Smartoptics DCP uses it)

**Per-script summary:**

| Script | Device Type Key | Connection | SSH Reuse | DataFrame Keys in outputs[ip] |
|--------|-----------------|------------|-----------|-------------------------------|
| Nokia_SAR.py | `7705 sar-8 v2` | paramiko SSH | No — discards injected client | `hardware_data`, `Card A_data`, `Card B_data`, `mda_data`, `port_data` |
| Nokia_IXR.py | `7250 ixr-r6` | paramiko SSH | No — discards injected client | Same as SAR |
| Nokia_1830.py | `1830` | paramiko SSH (two-stage shell login) | No | `system_name`, `shelf_inventory`, `card data`, `interface_inventory` |
| Nokia_PSI.py | `nokia psi` | wexpect spawn | No | `shelf_detail`, `shelf_inventory`, `card_inventory`, `module_inventory`, etc. |
| Ciena_6500.py | `6500` | wexpect spawn | No | `equipment_inventory` |
| Ciena_RLS.py | `rls` | paramiko SSH (interactive shell) | No | `shelf_detail`, `shelf_inventory`, `card_inventory`, `module_inventory` |
| Smartoptics_DCP.py | `dcp` | paramiko SSH | **Yes** | `inventory_data` |

> **Note:** `utils/device_type.py` contains a legacy `DeviceIdentifier` / `ScriptSelector` (3-tuple return, no GUI queue). It is kept for backward compatibility but the active path uses `script_interface.py`.
