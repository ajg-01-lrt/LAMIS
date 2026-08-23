# ATLAS v2.0.10.0 — Release Notes

## ✨ New: Nokia PSI optical audit — now built into ATLAS
The Nokia 1830 **PSI optical audit** is now a first-class audit inside ATLAS,
selectable from **Diagnostics → Network Audit** via a new **Network Type**
dropdown (**Ciena RLS** / **Nokia PSI**). One screen now runs either audit.
- **One-seed auto-discovery + full multi-node walk** — point it at a single
  seed and it discovers the entire OLS/PSI network and audits every node it can
  reach, walking the OSC/DCN node-to-node.
- **NETCONF-based collection** (port 830) — discovery, transceivers, OSPF
  area/DNS, amplifier and OSC power, and internal-fiber deltas are all pulled
  over NETCONF. Neighbor discovery reads the external-link peers from each
  node's connections, so a walk no longer depends on a telnet getty.
- **Two access paths, auto-detected:**
  - **Wired OAMP-LAN** — ATLAS **sets a temporary OAMP-subnet IP on your wired
    NIC itself, walks the network, then removes it** when the audit finishes.
    No manual IP, and **no IT or routing changes** — safe to run on a customer
    premises.
  - **CIT craft port** (telnet) — the on-site craft workflow is intact for
    direct craft-port access.
  - A **routed / remote** connection audits the **seed only** (the full walk
    needs wired L2 adjacency to the OAMP subnet).
- **Wi-Fi safe** — it prefers a wired Ethernet connection and no longer touches
  Wi-Fi routing while reaching neighbor shelves.
- **Terminal vs ILA aware** — correct site type and amplifier card-type per
  node (IRDM32 / IRDM32L at terminals; EILA / EILAL / RA5PB at ILAs).
- **Credentials** default to `admin` / `admin` for PSI; the output workbook is
  named `PSI_Audit_<timestamp>.xlsx` with **Summary, Card Inventory, Interface
  Inventory, OSPF, Management, Power, Amplifiers, Alarms, OSC Power, OSC
  Internal,** and **Internal Fibers** tabs.

## ⚡ Ciena RLS audit — ~3× faster
- **SSH-pool concurrency** cuts a full RLS network audit from **~6:30 to ~2:06
  (~68% faster)**.
- **Circuits tab fixes** — corrected a header off-by-one, and CCMD power now
  reports correctly on RLA 64×1 nodes (ports 121/122).

## 🖥️ Network Audit UI
- New **Debug mode (verbose logging)** checkbox — available for **both** audit
  types.
- The **Optional Data Collection** section and credential hints adapt to the
  selected network type; the default remains **Ciena RLS**, so existing RLS
  runs are unchanged.

## 🧹 Repo hygiene
- Stopped tracking device NETCONF captures and ad-hoc debug workbooks that had
  slipped into the tree, and added `.gitignore` rules so they can't return.

## 📦 Build
- `ATLAS_Setup.exe` — 56.3 MB (56,271,874 bytes), **unsigned**
- SHA-256: `1ca7c26b4840a9557bc08a09c03d6657d32d3a6c1a29b8af810a2b087fa305fe`
