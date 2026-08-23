# ATLAS — User Guide

> **Automated Toolkit for LightRiver Asset & Systems**
> Version 2.0.1 · Windows Desktop Application · LightRiver Technologies

---

## Table of Contents

1. [Getting Started](#1-getting-started)
2. [Application Layout](#2-application-layout)
3. [Mode: Inventory](#3-mode-inventory)
   - 3.1 [Connection Type — Network Mode](#31-connection-type--network-mode)
   - 3.2 [Connection Type — LAN Mode](#32-connection-type--lan-mode)
   - 3.3 [Connection Type — Serial Mode](#33-connection-type--serial-mode)
   - 3.4 [Optional: Append to an Existing Report](#34-optional-append-to-an-existing-report)
   - 3.5 [Pod & IP Range Selection](#35-pod--ip-range-selection)
   - 3.6 [Running the Inventory (Network)](#36-running-the-inventory-network)
   - 3.7 [Running the Inventory (LAN / Serial)](#37-running-the-inventory-lan--serial)
   - 3.8 [During a Run — Progress, Pause, and Abort](#38-during-a-run--progress-pause-and-abort)
   - 3.9 [Credential Prompts](#39-credential-prompts)
   - 3.10 [After the Inventory — Packing Slip Prompt](#310-after-the-inventory--packing-slip-prompt)
   - 3.11 [Output File](#311-output-file)
4. [Mode: Packing Slip Generator](#4-mode-packing-slip-generator)
   - 4.1 [Uploading a File](#41-uploading-a-file)
   - 4.2 [Filling in Project Information](#42-filling-in-project-information)
   - 4.3 [Generating Packing Slips](#43-generating-packing-slips)
5. [Mode: Diagnostics](#5-mode-diagnostics)
   - 5.1 [Sub-mode: TDS Diagnostics](#51-sub-mode-tds-diagnostics)
   - 5.2 [Sub-mode: Network Audit](#52-sub-mode-network-audit)
6. [Mode: Raw File Processing](#6-mode-raw-file-processing)
   - 6.1 [Supported Input Formats](#61-supported-input-formats)
   - 6.2 [Device Type Selection](#62-device-type-selection)
   - 6.3 [Running Raw Processing](#63-running-raw-processing)
7. [Mode: Provisioning](#7-mode-provisioning)
   - 7.1 [Loading a Device List](#71-loading-a-device-list)
   - 7.2 [Connection Type](#72-connection-type)
   - 7.3 [Device-Specific Options](#73-device-specific-options)
   - 7.4 [Running Provisioning](#74-running-provisioning)
8. [Credential Management](#8-credential-management)
   - 8.1 [Default Credential Order](#81-default-credential-order)
   - 8.2 [Saving New Credentials](#82-saving-new-credentials)
9. [Output Files Reference](#9-output-files-reference)
10. [Logs & Troubleshooting](#10-logs--troubleshooting)
11. [Supported Devices Quick-Reference](#11-supported-devices-quick-reference)

---

## 1. Getting Started

### Installation

ATLAS ships as a self-contained Windows installer (`ATLAS_Setup.exe`). No Python installation is required.

1. Run `ATLAS_Setup.exe` and follow the installer prompts.
2. A shortcut is placed on your Desktop and in the Start Menu.
3. Launch **ATLAS** from either location.

### First Launch

On the very first launch ATLAS will:

- Create an application data folder at `%APPDATA%\ATLAS\`
- Write an encrypted credential file (`credentials_config.json`) with three default device credentials already seeded
- Check for any available updates (requires internet access — the check is skipped silently if offline)
- Display a brief loading screen, then open the main window

> **Tip:** If you see a Windows Defender SmartScreen warning on first run, click **More info → Run anyway**. This can occur if the installer's code-signing certificate is not yet trusted by your machine's policy.

---

## 2. Application Layout

When ATLAS opens you will see a row of **radio buttons** across the top of the window that switch between five operating modes:

| Mode | Purpose |
|------|---------|
| **Inventory** | Scan a range of network devices, collect hardware inventory, and export an Excel report. |
| **Diagnostics** | Run TDS diagnostics or a BFS Network Audit against Ciena TDS/RLS nodes. |
| **Packing Slip Generator** | Generate packing slips from an uploaded Excel file without scanning any devices. |
| **Raw File Processing** | Parse saved CLI transcript files and export an inventory workbook without connecting to any device. |
| **Provisioning** | Push initial configuration to Nokia or Ciena devices via serial console or LAN SSH. |

Selecting a mode switches the center panel. Below the mode panel is a shared **output terminal** — a scrollable text area that displays all log messages, scan progress, and error details during any operation.

---

## 3. Mode: Inventory

The Inventory mode is where most day-to-day work happens. It has three connection modes — **Network**, **LAN**, and **Serial** — selected via radio buttons at the top of the panel.

---

### 3.1 Connection Type — Network Mode

> **Use this when:** You want to scan a range of IPs on the 10.9.x.x network.

Network mode is the default and most common mode. It:

1. Pings every IP in the range you specify.
2. Identifies each reachable device automatically.
3. Pulls full hardware inventory from all identified devices in parallel (up to 5 at a time).
4. Exports the results to an Excel workbook.

When **Network** is selected, the lower portion of the panel shows the **Pod Selection** and **IP Range** controls (see [Section 3.5](#35-pod--ip-range-selection)).

---

### 3.2 Connection Type — LAN Mode

> **Use this when:** You have a single device connected directly to your laptop via a crossover cable or a small local switch, and you know its IP address.

Selecting **LAN** reveals the **Direct Connection** panel with the following fields:

| Field | Description |
|-------|-------------|
| **Script** | Select the device type from the dropdown. Options: Nokia 1830, Nokia PSI, Ciena 6500, Ciena RLS. |
| **IP** | Enter the four octets of the device's IP address. Press `.` or `Tab` to jump between octets automatically. |
| **Username** | The login username for this device. |
| **Password** | The login password (masked). |
| **Save Creds** | Click to save the username/password to the encrypted credential store for future use. |

> **Note:** In LAN mode there is no ping sweep or auto-identification step. ATLAS goes straight to running the selected script against the IP you entered.

**How to run:**
1. Select **LAN**.
2. Choose the correct script from the dropdown.
3. Enter the device IP (four octets).
4. Enter credentials (or leave blank if already saved — ATLAS will use stored creds).
5. Click **Run** (see [Section 3.7](#37-running-the-inventory-lan--serial)).

---

### 3.3 Connection Type — Serial Mode

> **Use this when:** You are connected to a device via a console/serial cable (RS-232 to USB adapter).

Selecting **Serial** reveals the **Direct Connection** panel with:

| Field | Description |
|-------|-------------|
| **Script** | Select the device type. Options: Nokia SAR, Nokia IXR. |
| **Serial Port** | Select the COM port the cable is on (e.g., COM3). Click **Refresh** to re-scan available ports. |
| **Baud Rate** | Select from 9600, 19200, 38400, 57600, or 115200. Default is 9600. |
| **Username** | Login username. |
| **Password** | Login password (masked). |
| **Save Creds** | Saves credentials to the encrypted store. |

> **Note:** Like LAN mode, Serial mode skips ping and identification and runs the selected script directly over the serial connection.

**How to run:**
1. Select **Serial**.
2. Plug in your console cable and click **Refresh** to confirm the COM port.
3. Set the correct baud rate (check device documentation if unsure — most Nokia devices default to 9600).
4. Choose the correct script.
5. Enter credentials.
6. Click **Run** (see [Section 3.7](#37-running-the-inventory-lan--serial)).

---

### 3.4 Optional: Append to an Existing Report

The **Device Report (Optional)** section allows you to add new devices to a report file that already exists from a previous run instead of creating a brand-new workbook.

**To use append mode:**

1. Click **Browse** in the Device Report section.
2. Select an existing ATLAS `.xlsx` report file.
3. The filename will appear in green with a checkmark. ATLAS reads the existing report's customer/project metadata automatically.
4. Run the inventory normally. New devices will be added as new tabs; any IP that already exists in the file will have its sheet replaced with fresh data.

**To go back to creating a new file:**

- Click **Clear** next to the file label. The label returns to "No report selected (new workbook will be created)".

> **Tip:** Append mode is useful for multi-day projects where you scan different pods on different days. Each scan adds to the same master report without losing previous data.

---

### 3.5 Pod & IP Range Selection

The Inventory panel has two side-by-side pod/IP selection columns — **Pod Selection 1** (left) and **Pod Selection 2** (right). You must fill in Pod 1 at minimum; Pod 2 is optional and used when you need to scan two different subnets in a single run.

**Each column has:**

| Control | Description |
|---------|-------------|
| **Pod** | A dropdown (100–112). This is the third octet of the IP. Selecting pod 105 gives IPs in the 10.9.105.x range. |
| **Start IP** | The last octet of the first IP to include (e.g., `1`). The full IP prefix is shown as a label to the left. |
| **End IP** | The last octet of the last IP to include (e.g., `20`). |

**Example — Single pod:**
```
Pod 1: 105    Start IP: 1    End IP: 20
→ Scans 10.9.105.1 through 10.9.105.20
```

**Example — Dual pod:**
```
Pod 1: 105    Start IP: 1    End IP: 10
Pod 2: 106    Start IP: 1    End IP: 5
→ Scans 10.9.105.1–10 and 10.9.106.1–5 in one run
```

> **Warning:** ATLAS will alert you if the two pod ranges overlap. Overlapping ranges are allowed but produce a warning because they will cause duplicate device entries.

> **Tip:** Leave Pod 2 blank (or leave the IP fields empty) if you only need one subnet.

---

### 3.6 Running the Inventory (Network)

Once your pod/IP range is set (and optionally an existing report is loaded), click **Run**.

#### Step 1 — Project Information Popup

A dialog box appears asking for:

| Field | Description |
|-------|-------------|
| **Customer** | Customer name printed on all report sheets. |
| **Project** | Project name or number. |
| **Purchase Order** | PO number (can be left as TBD). |
| **Sales Order** | SO number (can be left as TBD). |
| **Filename** | Output filename (auto-generated with timestamp, but editable). |

If you loaded an existing report in append mode, these fields are pre-filled from that file.

Click **OK** to continue. A save-location dialog then opens — choose where to save the output Excel file and click **Save**.

> **Note:** In append mode the save dialog is skipped — the output is written back to the file you already selected.

#### Step 2 — Ping Sweep

ATLAS pings every IP in the range simultaneously (up to 20 at a time). The output terminal shows:

```
Pinging 1/20...
Pinging 5/20...
...
Pinging complete. 14 reachable, 6 unreachable.
```

Unreachable IPs are listed and skipped. Only reachable IPs move to the next step.

#### Step 3 — Concurrent Device Identification & Inventory

ATLAS processes up to **5 devices at a time**. For each device it:

1. Probes the SSH banner to get a preliminary device type hint.
2. Logs in via SSH, trying credentials in order: `admin/admin` → `cli/admin` → `su/Ciena123` → any saved user credentials.
3. Runs a short set of identification commands to confirm the device type and name.
4. Selects the correct device script automatically.
5. Keeps the SSH connection open and immediately begins running inventory commands on the same connection (no second login required for Nokia SAR, IXR, and Smartoptics DCP).
6. Parses the command output and stores the inventory data.

The output terminal updates as each device completes:

```
Scanning 1/14 — 10.9.105.3 identified as Nokia 7705 SAR-8 v2
Scanning 2/14 — 10.9.105.7 identified as Nokia 7250 IXR-R6
...
Scanning 14/14 — Complete
```

> **If a device cannot be identified:** It is logged as "Unknown" and skipped. Its IP appears in the terminal with an explanation. This does not stop the other devices from being scanned.

#### Step 4 — Export

After all devices complete, ATLAS automatically builds the Excel workbook:

- A **Summary** sheet listing all devices with IP addresses and hyperlinks to their individual tabs.
- One **device tab per IP** containing the full hardware inventory (chassis, cards, MDAs, transceivers, part numbers, serial numbers, descriptions).

The terminal shows:

```
Exporting workbook...
Report saved: C:\Users\...\CustomerName_ProjectName_2026-05-02.xlsx
```

#### Step 5 — Packing Slip Prompt

After the export, a dialog asks:

> **Do you need packing slips for this inventory?**

- **Yes** — See [Section 3.10](#310-after-the-inventory--packing-slip-prompt).
- **No** — The run is complete.

---

### 3.7 Running the Inventory (LAN / Serial)

For LAN and Serial modes the flow is simpler — there is no ping sweep or auto-identification.

1. Fill in all Direct Connection fields (script, IP or COM port, credentials).
2. Click **Run**.
3. The Project Information popup appears — fill it in and choose a save location.
4. ATLAS connects directly to the device and runs the selected script.
5. Results are exported to the Excel workbook.
6. The packing slip prompt appears.

> **Tip:** If you saved credentials with **Save Creds**, you can leave the Username and Password fields blank when running — ATLAS will use the stored values automatically.

---

### 3.8 During a Run — Progress, Pause, and Abort

While a scan is in progress, three buttons are active:

| Button | What It Does |
|--------|-------------|
| **Run** | Greyed out during a run — prevents double-starts. |
| **Pause** | Pauses processing between devices. In-progress device connections are allowed to finish before the pause takes effect. Click **Resume** (same button, label changes) to continue. |
| **Abort** | Immediately stops all active device connections and cancels any queued devices that have not yet started. A partial result set is **not** exported — the run ends with an "Aborted" message. |

> **Note:** Abort stops **all** concurrent connections simultaneously — not just the most recent one.

---

### 3.9 Credential Prompts

If all stored credentials fail for a particular device, ATLAS shows a **Credential Required** dialog:

```
Could not authenticate to 10.9.105.12.
All stored credentials failed.

Username: [          ]
Password: [          ]
[ ] Save these credentials for future runs

   [Retry]   [Skip Device]
```

| Option | Result |
|--------|--------|
| Enter credentials and click **Retry** | ATLAS tries the new credentials and continues if successful. |
| Check **Save for future runs** | The new credentials are added to the encrypted store and tried automatically on future scans. |
| Click **Skip Device** | This device is skipped. The scan continues with remaining devices. |

---

### 3.10 After the Inventory — Packing Slip Prompt

When the export finishes, ATLAS asks:

> **Do you need packing slips for this inventory?**

Clicking **Yes** opens a mode selection:

| Mode | Description |
|------|-------------|
| **Individual** | Creates one Excel workbook per device. Each file contains that device's inventory using the packing slip template. Good for labeling individual boxes for shipment. |
| **Consolidated** | Creates a single Excel workbook with all devices on separate sheets. Good for a receiving department or a project manager who needs one file. |

After selecting a mode you are prompted to choose a save location (folder for Individual, file path for Consolidated). ATLAS then generates the packing slips and reports completion in the terminal.

---

### 3.11 Output File

The inventory Excel report contains:

- **Summary sheet** — Timestamp, customer/project metadata, device list table with clickable hyperlinks to each device tab. Cell F7 contains a comma-separated list of all IPs scanned.
- **Per-device sheets** (one per IP) — Named by device name or IP. Contains:
  - Customer, Project, PO, SO, Source (device name), System Type
  - Hardware inventory rows starting at row 15: Name, Type, Part Number, Serial Number, Description
  - A "Back to Summary" hyperlink in cell A1
  - Auto-sized columns

---

## 4. Mode: Packing Slip Generator

> **Use this when:** You already have an Excel file with device inventory data and want to generate packing slips without scanning any devices.

This mode is completely independent of the Inventory mode. It generates packing slips directly from data you upload.

---

### 4.1 Uploading a File

Click **Browse** under the **Upload File** header. Supported formats:

| Format | How It Is Treated |
|--------|-------------------|
| `.csv` | Loaded as a single flat table. |
| `.xlsx` / `.xls` (single sheet) | Loaded as a single flat table. |
| `.xlsx` / `.xls` (multiple sheets) | Treated as a **multi-device file**: each non-Summary sheet is one device. |

**Multi-sheet Excel files** are the most common input. ATLAS will:
- Detect all sheets in the file.
- Show `✓ filename.xlsx (X device(s))` in green when loaded.
- Auto-populate Customer, Project, PO, and SO fields if those values are found in the template cells (C5, C6, C7, D7) on the first device sheet, or in the Summary sheet (B7, D7).
- Display a **scrollable checkbox list** of all device sheets so you can select which to include. Use **Select All** / **Deselect All** as needed.

**File validation:** ATLAS checks that the file has an allowed extension, is not too large, and matches its claimed format. Files that fail validation are rejected with an error message.

---

### 4.2 Filling in Project Information

The **Project Information** section has four fields:

| Field | Description |
|-------|-------------|
| **Customer** | Customer name printed on the packing slip header. |
| **Project** | Project name or number. |
| **Purchase Order** | PO number. |
| **Sales Order** | SO number. |

These fields are auto-populated when you upload an Excel file that contains them. You can edit any field before generating.

---

### 4.3 Generating Packing Slips

Select the output mode using the radio buttons in the panel:

| Mode | Description |
|------|-------------|
| **Consolidated** | One workbook containing all selected devices. |
| **Individual** | One workbook per device. |

Then click **Generate Packing Slips** and choose a save location — a folder (Individual) or file path (Consolidated).

**What ATLAS does for each device sheet:**

1. Reads the inventory rows (looks for Part Number and Serial Number columns starting at row 15).
2. Opens the packing slip template.
3. Writes customer/project metadata into the template header cells.
4. Writes each inventory row into the template body.
5. Optionally auto-sizes columns.
6. Saves the file.

The output terminal confirms each file as it is saved. A completion message appears when done.

> **Note:** Any sheet named "Summary" (case-insensitive) in the uploaded file is automatically skipped — it is treated as metadata, not device inventory.

---

## 5. Mode: Diagnostics

The Diagnostics mode contains two sub-modes selected via radio buttons: **TDS** (single-host diagnostics) and **Network Audit** (multi-hop BFS walk).

---

### 5.1 Sub-mode: TDS Diagnostics

> **Use this when:** You need to run a targeted Ciena TDS diagnostic script against a single device.

TDS (Test Diagnostic System) runs a specialized external script (`TDS_v6.2.py`) against a Ciena 6500 or RLS node. It is designed for in-lab or in-field diagnostics separate from full inventory collection.

#### Configuring and Running TDS

Fill in the **TDS Configuration** fields:

| Field | Description |
|-------|-------------|
| **IP Address** | IP or resolvable hostname of the target device. |
| **Platform** | `rls` (Ciena RLS) or `6500` (Ciena 6500). |
| **Username** | Device login username. |
| **Password** | Device login password (masked). |
| **File Name** | Base name for the output file(s) that TDS will generate. |

Click **Run Diagnostics**.

**What happens before the script runs:**

1. ATLAS validates all fields (IP format, platform value, non-empty username/password/filename).
2. ATLAS verifies the device's SSH host key — a dialog may appear asking you to confirm an unknown host key fingerprint. You must accept it for TDS to proceed.
3. If the TDS script file is not found, an error is shown and the run is cancelled.

**What happens during the run:**

- The TDS script runs as a subprocess in the background.
- Output is streamed to the ATLAS terminal in real time.
- The **Run Diagnostics** button is disabled until the script completes.
- The status label shows **Running...** and returns to **Ready** when done.
- The password field is cleared immediately after the credentials are passed to the subprocess.

> **Timeout:** If TDS takes longer than 30 minutes, the subprocess is terminated and an error is shown.

---

### 5.2 Sub-mode: Network Audit

> **Use this when:** You need to map an entire RLS network by automatically walking LLDP neighbors hop by hop, starting from one or more seed devices.

The Network Audit performs a bounded breadth-first search (BFS) across an RLS network. It runs a TDS session against each discovered host, reads the LLDP neighbor data from the results, and queues newly discovered neighbors for the next hop — repeating until the hop limit is reached or no new neighbors are found.

#### Configuring and Running a Network Audit

| Field | Description |
|-------|-------------|
| **Seed IP / Hostname** | The first device to start the walk from. Enter an IPv4 address or resolvable hostname. |
| **Seed File** (optional) | Path to a text file containing multiple seed hosts — one per line. Lines starting with `#` are ignored. A TID may follow the hostname on the same line, separated by a space. |
| **Username** | Device login username (used for all hosts in the walk). |
| **Password** | Device login password (masked). |
| **Seed TID** (optional) | File name identifier for the seed host's TDS output files. |
| **Max Hops** | Maximum BFS depth (spinbox, 0–10, default **3**). A value of 0 runs TDS on the seed host only, with no neighbor walk. |

Click **Run Network Audit**.

**What happens during the run:**

1. ATLAS verifies the seed host's SSH key before starting.
2. `RLS_Network_Audit.py` is launched as a subprocess.
3. For each hop, one TDS session runs per discovered host.
4. Newly discovered LLDP neighbors are deduplicated and added to the queue.
5. Progress is streamed to the ATLAS output terminal.

**Output files** (written to the working directory):

| File | Contents |
|------|----------|
| `Walk_Summary.csv` | One row per discovered host: hop number, hostname, run status, PASS/WARN/FAIL/INFO validation counts, neighbors discovered. |
| `{HOST}_RLS_Validation.csv` | Per-host engineering validation verdicts. |
| `{HOST}_RLS_Walk_Neighbors.csv` | LLDP neighbor table for that host. |

> **Timeout:** The Network Audit timeout scales with Max Hops. Allow up to several hours for large networks.

> **Tip:** Use a Seed File when you need to start the walk from multiple independent nodes simultaneously.

---

## 6. Mode: Raw File Processing

> **Use this when:** You have saved CLI transcript files from a previous manual session and want to parse them into an inventory workbook without connecting to any device.

This mode is entirely offline. It reads CLI output that was captured to text files or a multi-sheet Excel workbook and produces the same formatted Excel report that a live scan would generate.

---

### 6.1 Supported Input Formats

| Input | Description |
|-------|-------------|
| **Single `.txt` file** | A single device's CLI output captured to a text file. |
| **Multi-sheet `.xlsx` / `.xls`** | Each sheet is treated as one device's CLI output. |
| **Folder of `.txt` files** | Each `.txt` file in the folder is treated as one device. |

Click **Browse** to select a file, or **Browse Folder** to select a folder. The detected input type and device count are confirmed in the panel.

---

### 6.2 Device Type Selection

Use the **Device Type** dropdown to tell ATLAS how to parse the transcript:

| Option | Use When |
|--------|----------|
| **Auto Detect Nokia** | The file contains Nokia SAR or IXR output and you want ATLAS to determine which. |
| **Nokia PSI** | Nokia 1830 PSI transcript. |
| **Nokia 1830** | Nokia 1830 transcript. |
| **Nokia SAR** | Nokia 7705 SAR transcript (explicit). |
| **Nokia IXR** | Nokia 7250 IXR transcript (explicit). |
| **Ciena 6500** | Ciena 6500 transcript. |
| **Ciena RLS** | Ciena RLS transcript. |

> **Tip:** Use **Auto Detect Nokia** when the transcript file names contain the device name (e.g., `USDEN5-SAR1-2026-05-01.txt`). ATLAS reads the filename to determine SAR vs IXR before parsing.

---

### 6.3 Running Raw Processing

1. Select your input (file or folder).
2. Choose the device type.
3. Click **Process**.

ATLAS parses the CLI output, matches command sections, and builds the inventory DataFrame. The output terminal shows which command sections matched and which were skipped. When complete, the standard Project Information popup appears — fill it in and choose a save location.

The output Excel file has the same structure as a live scan report: Summary sheet + one tab per device.

> **Note:** Devices with no matching CLI output produce a placeholder row in the report rather than being silently omitted.

---

## 7. Mode: Provisioning

> **Use this when:** You need to push initial configuration (management IP, hostname, routing, protocols) to a factory-default Nokia or Ciena device.

The Provisioning mode connects to a single device via serial console or LAN SSH and applies a templated configuration script. It supports Nokia SAR, Nokia IXR, Nokia 1830 OLS, Ciena SAOS 6, and Ciena SAOS 10.

---

### 7.1 Loading a Device List

Click **Browse** to load an Excel file containing your device list. Required columns:

| Column | Description |
|--------|-------------|
| **IP** | Management IP to assign to the device. |
| **Hostname** | Hostname to set on the device. |

Optional columns (auto-populated into the network parameter fields when a device is selected):

| Column | Description |
|--------|-------------|
| **Subnet / Prefix / Prefix Len** | Subnet prefix length (e.g., `22`). |
| **Gateway / GW / Next-Hop** | Default gateway IP. |
| **Static Route / Static Route Dest** | Static route destination (e.g., `10.0.0.0/8`). |

Once loaded, select the target device from the dropdown. Its values populate the parameter fields automatically.

---

### 7.2 Connection Type

Select **Serial (Console)** or **LAN (SSH)**:

**Serial:**

| Field | Description |
|-------|-------------|
| **Port** | COM port (auto-detected; click **Refresh** to rescan). |
| **Baud** | Baud rate. Defaults: Nokia = 115200, Ciena SAOS = 9600, Nokia OLS = 38400. |

**LAN (SSH):**

| Field | Description |
|-------|-------------|
| **Connect IP** | Override IP for the SSH connection (leave blank to use the IP from the device list). |
| **Username** | SSH username (default `admin`). |
| **Password** | SSH password (default `admin`). |

---

### 7.3 Device-Specific Options

#### Nokia SAR / IXR

| Option | Default | Description |
|--------|---------|-------------|
| **Configure card type** | ✅ Enabled | Programs the card type on the chassis. |
| **Sync redundancy** | ☐ Disabled | Configures redundancy synchronization. |

#### Nokia 1830 OLS

| Option | Default | Description |
|--------|---------|-------------|
| **Shelf Type** | Auto-detect | Override shelf type: PSI-4L / PSI-8L (MFC) or PSS-16II (USRPNL). |
| **Set loopback** | ☐ Disabled | Configures a loopback interface. ⚠️ Triggers a NE warm reset — only enable if expected. |

#### Ciena SAOS 6

| Option | Default | Description |
|--------|---------|-------------|
| **Mgmt VLAN ID** | `4000` | VLAN ID for the management interface. |
| **Interface Name** | `mgmt` | Name of the management interface to create or update. |
| **VLAN Name** | `mgmt` | VLAN name. |
| **Mgmt Port** | *(blank)* | Optional: physical port to assign to the management VLAN. |
| **Update existing interface** | ☐ Disabled | If enabled, modifies an existing interface instead of creating a new one. |
| **Protocol checkboxes** | SSH, SNMP, NTP, Syslog, RADIUS, TACACS | Enable or disable management protocols. |

> **Note:** Ciena SAOS 6 always assigns a `/32` prefix to the management IP regardless of the Prefix Len field.

#### Ciena SAOS 10

| Option | Default | Description |
|--------|---------|-------------|
| **Update existing mgmtbr0** | ☐ Disabled | Modifies the existing `mgmtbr0` bridge address instead of creating it. |
| **Global Source IP Interface** | *(blank)* | Optional: sets the management-plane default source IP interface. |

---

### 7.4 Running Provisioning

1. Load a device list and select the target device.
2. Choose your connection type and fill in connection parameters.
3. Set any device-specific options.
4. Click **▶ Run Provisioning**.

Progress is streamed to the output terminal in the panel. Click **■ Stop** to abort the run at any time.

> **Warning:** Provisioning sends configuration commands to the device. Verify all parameters before clicking Run — some operations (e.g., Nokia 1830 loopback) trigger a device reset.

---

## 8. Credential Management

ATLAS stores device credentials in an encrypted file at `%APPDATA%\ATLAS\credentials_config.json`. Credentials are encrypted with Fernet symmetric encryption. The key is unique to your machine and user account.

---

### 8.1 Default Credential Order

When ATLAS attempts to log into a device it tries credentials in this order:

| # | Username | Password | Primary Devices |
|---|----------|----------|-----------------|
| 1 | `admin` | `admin` | Nokia SAR, Nokia IXR, Smartoptics DCP |
| 2 | `cli` | `admin` | Nokia 1830 |
| 3 | `su` | `Ciena123` | Ciena 6500, Ciena RLS |
| 4 | *(user input)* | *(user input)* | Any device — prompted when all above fail |

This order is automatically set on first launch and re-applied on upgrade.

---

### 8.2 Saving New Credentials

There are three ways to add or update credentials:

**Method 1 — During a scan (Credential Prompt)**
When a device rejects all stored credentials, the prompt dialog appears. Enter the correct credentials, check **Save for future runs**, and click **Retry**. The new credential is appended to the store and tried automatically on all future scans.

**Method 2 — LAN / Serial mode Save Creds button**
Enter a username and password in the LAN or Serial Direct Connection panel and click **Save Creds**. The credentials are saved and pre-populated the next time you switch to that mode.

**Method 3 — Edit credentials_config.json**
For advanced users: the file at `%APPDATA%\ATLAS\credentials_config.json` is Fernet-encrypted and cannot be edited by hand in a useful way. Use Method 1 or 2 for all credential management.

> **Security note:** Credentials are never written to log files in plain text. Passwords visible in the UI are masked with `*`. Password entry fields are scrubbed (cleared) from memory immediately after use.

---

## 9. Output Files Reference

| File | Location | Created By |
|------|----------|------------|
| Inventory report | Location you chose at run-time | Inventory mode → Run (Network/LAN/Serial) |
| Individual packing slips | Folder you chose | Packing slip prompt after inventory, or Packing Slip Generator mode |
| Consolidated packing slip | File path you chose | Same as above |
| TDS output files | Working directory or path set in TDS script | Diagnostics → TDS → Run Diagnostics |
| Network Audit summary | Working directory | Diagnostics → Network Audit → Run Network Audit |
| Raw processing report | Location you chose at run-time | Raw File Processing mode → Process |
| Run log | `%APPDATA%\ATLAS\logs\ATLAS_YYYY-MM-DD_HH-MM-SS.log` | Automatically on every launch |

### Inventory Report Structure

```
Summary (sheet 1)
  A1: Capture timestamp
  B5/D5: Customer / Project
  Row 10+: # | IP Address | Device Name (hyperlinked to device tab)
  F7: Comma-separated IP list

Device Sheet (one per IP)
  A1: "Back to Summary" hyperlink
  C5: Customer
  C6: Project
  C7: Purchase Order  / D7: Sales Order
  C8: Source (device system name)
  C9: System Type
  Row 15+: Name | Type | Part Number | Serial Number | Description
```

---

## 10. Logs & Troubleshooting

### Log Files

Every ATLAS run writes a timestamped log file to `%APPDATA%\ATLAS\logs\`. Log level detail:

| Level | Content |
|-------|---------|
| INFO | Run start/end, devices found, files saved |
| DEBUG | Every CLI command sent, every response received, all SSH connection events |
| WARNING | SSH key exchange issues, file validation rejections |
| ERROR | Unexpected exceptions |

To open the logs folder: press `Win + R`, type `%APPDATA%\ATLAS\logs`, press Enter.

### Common Issues

**Device shows as "Unreachable" but I can ping it manually**

- Confirm the pod and IP range are set correctly (the prefix is `10.9.<pod>.<last octet>`).
- Ensure the device is on the same network segment as your laptop.
- Check if a firewall or VPN is blocking ICMP.

**Device is reachable but not identified**

- ATLAS attempts SSH banner → SSH login → Telnet fallback. If all three fail the device is skipped.
- Check the terminal output for the specific error (e.g., "Authentication failed", "Connection refused").
- Try LAN mode with the exact IP and credentials to isolate the issue.

**"Authentication failed" for every device**

- All three default credentials have been tried and failed.
- The Credential Prompt will appear. Enter the correct credentials and check **Save for future runs**.

**Output Excel file will not open**

- Check the logs for an export error.
- Ensure the file is not already open in Excel (Excel locks the file for writing).
- Try a different save location (avoid network shares for first runs).

**Packing slips show "TBD" for PO/SO**

- Normal when no PO/SO was entered at the project information popup.
- Re-run and fill in the fields, or edit the cells in the generated file directly.

**TDS button is disabled / grayed out**

- TDS will not run if a required field is blank or if the SSH host key verification failed.
- Check that all four fields (IP, platform, username, password) are filled.
- If the host-key dialog appeared and you clicked Cancel, re-run and accept the key.

**Network Audit stops after the seed host**

- Verify that Max Hops is set to at least 1.
- Check `Walk_Summary.csv` for per-host error details.
- Ensure the seed host can reach its LLDP neighbors over the management network.

**Raw processing produces empty or incomplete output**

- Confirm the correct Device Type is selected — a mismatch causes all command sections to be skipped.
- Check the output terminal for "[RAW] No match" messages which identify unrecognized command sections.

---

## 11. Supported Devices Quick-Reference

| Device | Auto-ID? | Connection | Script Used |
|--------|----------|------------|-------------|
| Nokia 7705 SAR-8 v2 | ✅ Yes | SSH | Nokia_SAR.py |
| Nokia 7250 IXR-R6 | ✅ Yes | SSH | Nokia_IXR.py |
| Nokia 7250 IXR-R6d | ✅ Yes | SSH | Nokia_IXR.py |
| Nokia 1830 | ✅ Yes | SSH → Telnet | Nokia_1830.py |
| Nokia 1830 PSI | ⚠️ Partial | SSH → Telnet | Nokia_PSI.py |
| Nokia 1830 OLS | ➡️ Provision only | Serial / SSH | scripts/Network/Nokia_OLS.py |
| Ciena 6500 | ✅ Yes | SSH → Telnet | Ciena_6500.py |
| Ciena RLS | ⚠️ Partial | SSH → Telnet | Ciena_RLS.py |
| Ciena SAOS 6 | ➡️ Provision only | Serial / SSH | scripts/Network/Ciena_SAOS.py |
| Ciena SAOS 10 | ➡️ Provision only | Serial / SSH | scripts/Network/Ciena_SAOS10.py |
| Smartoptics DCP-R | ✅ Yes | SSH | Smartoptics_DCP.py |
| Smartoptics DCP-2 | ✅ Yes | SSH | Smartoptics_DCP.py |

**⚠️ Partial** = The device can be connected to and identified, but some data fields may be incomplete pending full pipeline integration in a future release.

**➡️ Provision only** = This device is supported in the Provisioning mode only; it does not participate in the Inventory auto-scan pipeline.

---

*For developer documentation, architecture details, and bug fix history see [ATLAS_Development_Document.docx](ATLAS_Development_Document.docx) and [INVENTORY_LOGIC_FLOW.md](INVENTORY_LOGIC_FLOW.md).*
