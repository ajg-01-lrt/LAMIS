# ATLAS Build & Deployment Guide

This document covers building ATLAS (formerly LAMIS) into a Windows installer.

The pipeline is:

1. **PyInstaller** packages the GUI (`ATLAS.exe`) and the TDS subprocess
   (`TDS.exe`) into `dist\ATLAS\` from a single spec file (`ATLAS.spec`).
2. **NSIS** wraps that folder into a per-user installer at
   `dist\ATLAS_Setup.exe`.
3. **`build.bat`** drives both steps and (optionally) signs the artifacts.

The spec file is the source of truth for hidden imports, bundled data files,
and the two-executable layout. Do **not** rebuild by passing CLI flags to
`pyinstaller`; that regenerates the spec from scratch and drops the
hidden-import list, which breaks the keyring backend, dynamic device-script
imports, and the TDS subprocess.

---

## Prerequisites

### 1. Python 3.12 environment

Use the project's virtual env if you have it; otherwise create one:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For supply-chain-verified installs, use the hash-pinned lock file:

```bat
pip install --require-hashes -r requirements.lock
```

Regenerate the lock after upgrading anything in `requirements.txt`:

```bat
python scripts\generate_requirements_lock.py
```

### 2. NSIS (Nullsoft Scriptable Install System)

```bat
choco install nsis -y
```

Or download manually from <https://nsis.sourceforge.io/Download>. After
install, verify with `makensis /VERSION`.

### 3. icon.ico

A 256×256 `icon.ico` must exist in the project root (the spec references it
and the build aborts without it).

### 4. (Optional) Windows SDK for code signing

```bat
choco install windows-sdk -y
```

---

## Build process

### Quick path

```bat
build.bat
```

Outputs:

- `dist\ATLAS\ATLAS.exe`   — windowed GUI (smoke-test this before shipping)
- `dist\ATLAS\TDS.exe`     — console TDS subprocess, spawned by the GUI
- `dist\ATLAS\_internal\`  — shared dependencies (numpy, pandas, paramiko, …)
- `dist\ATLAS_Setup.exe`   — final per-user installer

### Common options

```bat
build.bat                      :: build + sign (signs by default)
build.bat --clean              :: wipe build\ and dist\ first, then build + sign
build.bat --release            :: also delete dist\ATLAS\ at the end
                                  (keeps only the Setup.exe for distribution)
build.bat --no-sign            :: build without signing (debug / CI)
build.bat --sign other.pfx     :: sign with a non-default cert
build.bat --clean --release    :: full release build (signed)
```

**Signing is on by default.** The build looks for the cert at
`certs\LightRiver_codesign.pfx` (gitignored) and prompts for its
password each run. If the cert is missing the build aborts with a
message — either drop the `.pfx` at that path or pass `--no-sign`. To
use a different cert path on the fly, pass `--sign path\to\other.pfx`.

By default the unpacked `dist\ATLAS\` folder is preserved so you can run
`ATLAS.exe` directly to smoke-test before installing. Add `--release` when
you're confident and ready to ship only the installer.

### What the spec actually bundles

Hidden imports baked into `ATLAS.spec` (because static analysis can't see
them):

- **Dynamic device scripts** — `scripts.Nokia_SAR`, `Nokia_IXR`,
  `Nokia_1830`, `Nokia_PSI`, `Ciena_6500`, `Ciena_RLS`, `Ciena_SAOS_Inv`,
  `Ciena_SAOS10_Inv`, `Smartoptics_DCP`, plus the `scripts.Network.*`
  provisioning variants. The GUI loads these via `importlib.import_module()`.
- **Keyring's Windows backend** — `keyring.backends.Windows` and
  `keyring.backends.fail`. Without these, the Windows Credential Manager
  integration silently falls back to "no backend available" at runtime.
- **pywin32** — `win32api`, `win32cred`, `win32event`, `pywintypes`,
  `pythoncom` (used by keyring and by `utils.helpers.restrict_path_to_owner`).
- **`ping3`** and **`pexpect`** fallbacks.

`collect_all()` is also called for `paramiko`, `openpyxl`, `pandas`, `PIL`,
`serial`, `cryptography`, `keyring`, `wexpect`, and the project's own
`scripts` package, so data files and submodules come along.

---

## Test the unpacked build before installing

```bat
dist\ATLAS\ATLAS.exe
```

Quick checklist:

- [x] Loading screen displays the ATLAS logo (proves `ATLAS Logo.png` is bundled)
- [x] Main window opens with tabs: Inventory, Packing Slip, TDS, Provision, Raw
- [x] **Inventory** — can save credentials (proves `keyring` + `cryptography`
      backends bundled)
- [x] **Inventory** — can identify a device (proves dynamic `scripts.*` imports work)
- [x] **TDS** — clicking Run on a configured host actually launches
      `TDS.exe` as a subprocess (check Task Manager). Before this fix, the
      TDS tab silently re-launched ATLAS.exe.
- [x] Packing Slip generation produces an `.xlsx` from the templates

Logs are written to `%APPDATA%\ATLAS\logs\ATLAS_*.log` — check there for
any startup errors.

---

## Build the installer separately (if you skipped build.bat)

```bat
makensis ATLAS.nsi
```

`ATLAS.nsi` is a **per-user installer**:

- No UAC elevation prompt.
- Installs to `%LOCALAPPDATA%\Programs\ATLAS\` (the modern convention used
  by VS Code, Chrome, etc.).
- Registers itself under `HKCU` so the Apps & Features entry belongs to the
  user who installed it. The legacy `HKCU`-while-installed-by-admin layout
  meant uninstall never showed up for the actual user.

If you need a system-wide install instead, edit `ATLAS.nsi` and change:

- `RequestExecutionLevel user` → `admin`
- `InstallDir "$LOCALAPPDATA\Programs\ATLAS"` → `"$PROGRAMFILES\ATLAS"`
- All `HKCU` → `HKLM`

---

## Test the installer

```bat
dist\ATLAS_Setup.exe
```

Expected behaviour:

- No UAC prompt
- Installs to `%LOCALAPPDATA%\Programs\ATLAS\`
- Desktop and Start Menu shortcuts are created
- "ATLAS" shows up in **Settings → Apps & installed apps**, uninstallable
  by the same user

After install, runtime data lives separately under `%APPDATA%\ATLAS\`:

```text
%APPDATA%\ATLAS\
├── logs\                       ← rotating ATLAS_*.log files
├── credentials_config.json     ← encrypted defaults (Fernet)
├── .creds_key                  ← Fernet key, ACL-restricted to the user
├── known_hosts                 ← SSH TOFU host keys
├── network_inventory.db        ← seeded from the bundled copy on first run
└── telnet_allowlist.json
```

`%APPDATA%\ATLAS\` is preserved across uninstall/reinstall so saved
credentials, host keys, and the inventory DB survive upgrades.

---

## Troubleshooting

### Build issues

**`PyInstaller not found`**

```bat
pip install pyinstaller
```

**`ATLAS.spec not found`**

You're running the build from the wrong directory. `cd` into the project
root (the folder that contains `main.py`, `ATLAS.spec`, and `build.bat`).

**`dist\ATLAS\TDS.exe was not produced`**

Almost certainly because `scripts\TDS\TDS_v6.2.py` was moved or renamed. The
spec hardcodes that path. Restore the file or update `ATLAS.spec`.

**NSIS `LAMIS.nsi has been retired`**

You ran `makensis LAMIS.nsi` out of muscle memory. Use `makensis ATLAS.nsi`
or just `build.bat`.

### Runtime issues (in the installed app)

#### Logo missing on the splash screen

The bundled file must be at the bundle root with the exact name
`ATLAS Logo.png`. Verify with:

```bat
dir "%LOCALAPPDATA%\Programs\ATLAS\_internal\ATLAS Logo.png"
```

#### "No recommended backend was available" when saving credentials

`keyring.backends.Windows` wasn't bundled. Either the spec was regenerated
by a stale `build.bat` (overwriting the hidden imports) or pywin32 isn't
installed in the build environment. Reinstall deps and rebuild from the
committed `ATLAS.spec`.

#### TDS tab does nothing / re-launches the GUI

The build dropped `TDS.exe`. Verify:

```bat
dir "%LOCALAPPDATA%\Programs\ATLAS\TDS.exe"
```

If missing, rebuild via `build.bat` (which fails fast when `TDS.exe` is
missing post-build).

#### Inventory scan fails with "Unsupported script selection"

A dynamic device-script import is missing from the bundle. Confirm via:

```bat
dir "%LOCALAPPDATA%\Programs\ATLAS\_internal\scripts"
```

You should see `Nokia_SAR.pyc`, `Ciena_6500.pyc`, etc. If any are missing,
the spec was tampered with — restore the committed `ATLAS.spec`.

### Installer issues

#### Installer succeeds but the app is missing from Apps & Features

You're looking under the wrong user. The per-user installer registers under
`HKCU` for the currently-logged-in user. If you ran the installer elevated
(e.g. right-click → Run as administrator), it registered under the admin's
HKCU. Re-install without elevating.

---

## Code signing (optional)

Code signing prevents Defender SmartScreen warnings and satisfies IT
policies that require signed binaries. Both `ATLAS.exe` and `TDS.exe`
should be signed in addition to the installer.

```bat
:: All-in-one: clean build, sign every artifact (default), produce release installer
build.bat --clean --release
```

Or sign after the fact:

```bat
sign.bat "certs\LightRiver_codesign.pfx"                  :: all three
sign.bat "certs\LightRiver_codesign.pfx" --exe-only       :: ATLAS.exe + TDS.exe
sign.bat "certs\LightRiver_codesign.pfx" --installer-only :: just Setup.exe
```

Store `*.pfx` files outside the repo (the project's `.gitignore` excludes
`certs/`).

---

## Version updates

1. Bump the version in `ATLAS.nsi` — both `VIProductVersion` and the
   `DisplayVersion` reg writes.
2. Bump the version in `config.py` if applicable.
3. `build.bat --clean` to force a full rebuild.
4. Smoke-test `dist\ATLAS\ATLAS.exe`.
5. `build.bat --release` (or just rename `dist\ATLAS_Setup.exe` to include
   the version, e.g. `ATLAS_Setup_v2.1.0.exe`).

---

## File checklist before distribution

```text
LAMIS/                             ← (folder still named LAMIS, app is ATLAS)
├── dist\
│   ├── ATLAS_Setup.exe            ← final deliverable
│   └── ATLAS\                     ← (deleted by build.bat --release)
│       ├── ATLAS.exe
│       ├── TDS.exe
│       ├── ATLAS Logo.png
│       ├── icon.ico
│       ├── data\
│       │   ├── network_inventory.db
│       │   ├── ATLAS_Packing_Slip.xlsx
│       │   ├── ATLAS_Consolidated_Packing_Slip.xlsx
│       │   ├── Device_Report_Template.xlsx
│       │   ├── Ciena_RLS_*.xlsx
│       │   └── Nokia_PSI_*.xlsx
│       └── _internal\             ← all bundled dependencies
├── ATLAS.spec                     ← source of truth for the build
├── ATLAS.nsi                      ← per-user installer
├── build.bat                      ← orchestrator
├── sign.bat                       ← code signing
└── icon.ico
```

---

## Support resources

- PyInstaller — <https://pyinstaller.org/en/stable/>
- NSIS — <https://nsis.sourceforge.io/Docs/>
- Signtool — <https://learn.microsoft.com/windows/win32/seccrypto/signtool>
