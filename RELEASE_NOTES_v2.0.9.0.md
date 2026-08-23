# ATLAS v2.0.9.0 — Release Notes

## ✨ New: AI "Doc Search" assistant
A grounded, RAG-based assistant for querying Nokia/Ciena vendor documentation, added as a new tab (gated behind `AI_ASSISTANT_ENABLED`).
- **Extractive by design** — it describes the steps and shows the **verbatim doc text with citations** for you to copy; it never writes or transcribes CLI syntax itself (added after repeated catches of fabricated/cross-platform config).
- **Alarm / error interpreter** — paste raw alarm or error output and get a per-alarm breakdown (cause, impact, clearing steps), grounded in the docs.
- **Screenshot interpretation** — `Ctrl+V` an alarm-list screenshot; it reads the alarm mnemonics via vision, then runs the same grounded lookup path (image used only to pull identifiers, never as the answer source).
- **Measurement/parameter glossary** — "find/display X" questions (e.g. OSC RX/TX power) resolve against parsed PM/parameter tables rather than the model guessing.
- **Platform scoping** — retrieval is filtered per platform, and multi-platform questions ("X on RLS and OLS") generate a separate section per platform so answers can't cross-contaminate.
- API key is stored in **Windows Credential Manager**; new operator docs at `docs/AI_ASSISTANT.md`.

## 🔧 Software Upgrades
- **New Nokia PSS upgrade flow** (`Nokia_PSS_Upgrade.py`) — mirrors the PSI flow but omits `config software server port 8000`, which hangs PSS-class shelves; load is pulled over HTTP from the local server staged by the Upgrades tab.
- **Nokia G42 upgrade hardening** — the validate / apply / activate phases now **auto-answer the `[y/n]` confirmation prompts** (including cross-family "loss of config" and redundant-XMM4 prompts) instead of hanging at them; handles prompts that arrive split across reads, plus ZTP-mode / recover-mode handling.
- Minor Nokia PSI upgrade refinements.

## 🩹 Inventory & Reporting — data-loss fix
- Fixed a bug where inventorying **multiple distinct shelves that share one direct-connect service IP** (e.g. `172.16.0.1`) in **append mode** silently deleted each prior shelf's tab, leaving only the last one. The same-IP dedup now only replaces a tab when the device has **no real identity** (no hostname *and* no serial); a distinct hostname/serial at the same IP is treated as a different shelf and gets its own tab.
- `workbook_builder` and Nokia BOM-category data updates.

## 🔌 Connectivity / reliability
- **Reworked Nokia SSH login** (`nokia_ssh_authenticate`): authenticates `admin`/`admin` and drops straight to the CLI shell (modern 1830/PSS/PSI), with fallback to the legacy passwordless `cli` + inner two-stage login. Fixes a case where the banner's "Last Login:" line was mistaken for a login prompt. Applies to 1830/PSS/PSI inventory and the upgrade scripts.

## 📦 Build
- `ATLAS_Setup.exe` — 56.0 MB, **unsigned**
- SHA-256: `d93a16a7125b60fac29b8a9e37b52eda6385967f4598ce12e7eff0fca9cdf6ee`
