# Automated Toolkit for Lightriver Asset & Systems (ATLAS)

A comprehensive network device management platform supporting Nokia (7705 SAR-8, 7250 IXR, 1830), Ciena 6500, and Smartoptics DCP devices.

**Key Features:**

- Automated inventory collection via SSH/Telnet
- Multi-device batch processing with ThreadPoolExecutor
- Excel workbook generation with detailed device hardware reports
- Packing slip creation (individual or consolidated modes)
- Multi-sheet device file import and processing
- SQLite part number description caching
- Real-time GUI progress tracking with pause/abort controls

## Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [Usage](#usage)
- [Building and Deployment](#building--deployment)
- [Project Structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

## Overview

ATLAS is a Python-based network device management platform designed to automate hardware discovery, asset documentation, and device lifecycle operations. The application:

- **Discovers devices** via SSH banner probing + full login authentication (with Telnet fallback)
- **Collects hardware inventory** including chassis, cards, MDAs, and optical transceivers
- **Generates Excel reports** with system name, type, part numbers, serial numbers, and descriptions
- **Creates packing slips** for shipment/deployment documentation (individual per device or consolidated into single workbook)
- **Imports multi-sheet Excel files** to create batch packing slips without device queries
- **Logs all operations** with timestamped files for audit trails and debugging

Supported devices:

- **Nokia 7705 SAR-8 v2** — Supports chassis, control cards, MDAs (Ethernet, serial), ports
- **Nokia 7250 IXR-R6 / IXR-R6d** — High-speed routing platform with 10G/100G interfaces
- **Nokia 1830** — Compact transport platform
- **Ciena 6500** — Optical transport platform
- **Smartoptics DCP-R / DCP-2** — Modular optical platforms

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/Chees3loaf/Network-Inventory-Update.git
   cd Network-Inventory-Update
   ```

2. **Create a virtual environment (optional but recommended):**

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use `venv\Scripts\activate`
   ```

3. **Install the required packages:**

   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. **Run the application:**

   ```bash
   python main.py
   ```

2. **Main Workflow — Inventory Tab:**
   - Enter customer name, project name, PO/SO numbers
   - Specify device pod and IP range (e.g., `10.0.0.1-10.0.0.5` or individual IPs)
   - Click **Run Inventory** — devices are discovered, queried, and results saved to timestamped Excel workbook
   - Optional: append new scans to existing inventory file
   - Pause/Abort buttons available during collection

3. **Packing Slip Generation — Packing Slip Tab:**
   - **Mode A (From Inventory):** Uses live-queried device data
     - Select devices from the generated inventory workbook
     - Choose **Individual** (one Excel per device) or **Consolidated** (single multi-sheet workbook)
     - Optionally auto-size columns
   - **Mode B (From File):** Imports pre-built multi-sheet Excel files
     - Upload file with one device per sheet (optional Summary sheet for metadata)
     - Select sheets and output mode
     - Generates packing slips without querying devices

4. **TDS Tab:**
   - Test/Diagnostic/Showcase mode (not currently active)

5. **Logging:**
   - All operations logged to `logs/LAMIS_YYYY-MM-DD_HH-MM-SS.log`
   - Paramiko (SSH library) log level suppressed to reduce verbosity

## Key Features & Recent Improvements

### Inventory Collection

- **Auto-detection** — Identifies device type via SSH banner + login response parsing
- **Multi-device batch processing** — Concurrent collection using ThreadPoolExecutor (max 2 workers)
- **Fallback authentication** — SSH → Telnet fallback if SSH unavailable
- **Error resilience** — Missing devices are logged but don't block others

### Excel Output

- **Hardware inventory** — System name, type, part numbers, serial numbers, descriptions
- **Per-device sheets** — Each device gets dedicated sheet in report workbook
- **Auto-sizing** — Column widths automatically optimized for readability
- **Summary integration** — Automatic Summary sheet with device list and metadata

### Packing Slips

- **Individual mode** — One Excel workbook per device (ships with device)
- **Consolidated mode** — All devices in single multi-row workbook (for receiving/shipping)
- **From File mode** — Import pre-populated multi-sheet Excel files without device queries
- **Merged cell handling** — Proper customer/project info in C5:C6 (merged cell support)
- **Data validation** — Automatic row offset (row 15+) for device data rows

### Logging & Debugging

- **Timestamped logs** — Each run creates `logs/LAMIS_YYYY-MM-DD_HH-MM-SS.log`
- **Paramiko suppression** — SSH key exchange debug noise filtered; kept at WARNING level
- **Query logging** — All commands and responses logged at DEBUG level
- **Part lookup caching** — SQLite database avoids repeated lookups

### Testing

- **44 unit tests** covering inventory collection, Excel generation, packing slip logic, multi-sheet handling, and upgrade/audit regressions
- **Test command:** `python -m pytest tests/ -q`
- **CI:** GitHub Actions runs `python -m pytest tests/ -q` on every push and pull request via `.github/workflows/tests.yml`

## Building & Deployment

LAMIS uses a **two-stage build process** for professional deployment:

1. **Build onedir executable** — Fast startup, separate dependencies
2. **Package installer** — Professional Windows installer (NSIS)

### Quick Start

```bash
# Step 1: Build executable (onedir)
build.bat

# Step 2: Install NSIS (one-time)
choco install nsis -y
# or download from https://nsis.sourceforge.io/Download

# Step 3: Build installer
makensis LAMIS.nsi
```

**Output:** `dist/LAMIS_Setup.exe` (~120-150MB) — ready for distribution

### Detailed Guide

See [BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md) for:

- Complete build process walkthrough
- Testing and verification steps
- Troubleshooting common issues
- Enterprise deployment options
- Version update procedures

## Troubleshooting

**Device not identified:**

- Check SSH/Telnet connectivity: `ssh user@device_ip` or `telnet device_ip`
- Verify credentials are correct (same user/password for all devices)
- Review `logs/LAMIS_*.log` for SSH error details

**No port data in inventory:**

- Expected behavior if device has no SFP transceivers installed (logged at DEBUG level)
- Part number lookups for ports require entries in `data/network_inventory.db`

**MDA/Card data missing:**

- Ensure device supports the queried commands for your firmware version
- Review device script for supported device types (SAR.py, IXR.py, etc.)

## Project Structure

```text
LAMIS/
├── main.py                              # Entry point; logging configuration
├── script_interface.py                  # Device identification, script selection, command caching
├── config.py                            # Configuration (log levels, paths, etc.)
├── requirements.txt                     # Python dependencies
├── README.md                            # This file
│
├── data/
│   ├── network_inventory.db             # SQLite cache: part number → description mappings
│   ├── LAMIS_Packing_Slip.xlsx          # Template for individual packing slips (per device)
│   ├── LAMIS_Consolidated_Packing_Slip.xlsx  # Template for consolidated packing slips
│   └── Device_Report_Template.xlsx      # Template for inventory reports
│
├── gui/
│   ├── __init__.py
│   ├── gui4_0.py                        # Main GUI class (InventoryGUI) with tab switching
│   ├── inventory_frame.py               # InventoryFrame — IP/pod input, progress, run controls
│   ├── packing_slip_frame.py            # PackingSlipFrame — upload, mode selection, output generation
│   ├── tds_frame.py                     # TDSFrame (test/diagnostic)
│   └── workbook_builder.py              # WorkbookBuilder — Excel generation logic
│
├── scripts/
│   ├── __init__.py
│   ├── script_interface.py              # Script base class and command execution framework
│   ├── Nokia_SAR.py                     # Nokia 7705 SAR-8 v2 script
│   ├── Nokia_IXR.py                     # Nokia 7250 IXR script
│   ├── Nokia_1830.py                    # Nokia 1830 script
│   ├── Ciena_6500.py                    # Ciena 6500 script
│   ├── Smartoptics_DCP.py               # Smartoptics DCP script
│   └── TDS/
│       └── Ciena_TDS.py                 # TDS (test diagnostic system) extension
│
├── utils/
│   ├── __init__.py
│   ├── device_type.py                   # Device type normalization and mapping
│   ├── packing_slip.py                  # Packing slip Excel template helpers
│   ├── update.py                        # Update management
│   └── helpers.py                       # Utility functions
│
├── logs/
│   └── LAMIS_YYYY-MM-DD_HH-MM-SS.log   # Timestamped log files (auto-created)
│
└── tests/
│   └── test_*.py                        # Unit tests (44 tests covering all major components)
```

## Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature/your-feature`).
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature/your-feature`).
5. Create a new Pull Request.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
