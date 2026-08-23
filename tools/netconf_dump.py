#!/usr/bin/env python3
"""Capture a Nokia 1830 OLS node's live NETCONF/YANG tree for audit path-mapping.

Phase 2 of the NETCONF rebase. Read-only: does one full ``<get>`` via
``utils.netconf.NetconfSession`` and saves the result split per top-level
subtree, so the exact YANG paths that feed each audit sheet can be mapped from
*this* node's real data (the shipped reference dump is a terminal; the field
seeds are ILAs, which lay cards out differently).

Usage:
    python tools/netconf_dump.py [HOST] [USER] [PASS] [OUTDIR]

Defaults: HOST=172.21.109.22  USER=admin  PASS=admin
          OUTDIR=netconf_dump_<host>   (created next to where you run it)

Writes:
    <OUTDIR>/_full.xml            full <data> tree (pretty)
    <OUTDIR>/<container>.xml      one file per top-level YANG container
    <OUTDIR>/_summary.txt         per-container element/leaf counts

Read-only and safe. To capture an RNE, bring the OAMP-LAN temp-DCN up first.
"""
import os
import sys
import time

# Allow running from the repo root: make `utils` importable.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from lxml import etree                       # noqa: E402
from utils.netconf import NetconfSession, NetconfError  # noqa: E402


def _leaf_count(elem):
    return sum(1 for e in elem.iter() if len(e) == 0)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "172.21.109.22"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    password = sys.argv[3] if len(sys.argv) > 3 else "admin"
    outdir = sys.argv[4] if len(sys.argv) > 4 else f"netconf_dump_{host.replace(':', '_')}"

    print(f"NETCONF dump -> {user}@{host}:830   (read-only full <get>)")
    print("-" * 64)
    t0 = time.time()
    try:
        with NetconfSession(host, user, password, timeout=180.0) as nc:
            print(f"[ok]   connected; {len(nc.server_capabilities)} capabilities")
            print("[..]   full <get> (this can take 30-90s on a large node) ...")
            data = nc.get()
    except NetconfError as exc:
        print(f"[FAIL] {exc}")
        return 1
    dt = time.time() - t0

    os.makedirs(outdir, exist_ok=True)
    full_xml = etree.tostring(data, pretty_print=True)
    with open(os.path.join(outdir, "_full.xml"), "wb") as f:
        f.write(full_xml)
    total_leaves = _leaf_count(data)
    print(f"[ok]   got {len(data)} top-level containers, "
          f"{total_leaves} leaves, {len(full_xml)//1024} KB in {dt:.1f}s")

    # Split per top-level container (merge duplicates under one file).
    summary = []
    written = {}
    for child in data:
        tag = child.tag if isinstance(child.tag, str) else "unknown"
        written.setdefault(tag, []).append(child)
    for tag, elems in sorted(written.items()):
        wrapper = etree.Element(tag + "_capture")
        for e in elems:
            wrapper.append(e)
        path = os.path.join(outdir, f"{tag}.xml")
        with open(path, "wb") as f:
            f.write(etree.tostring(wrapper, pretty_print=True))
        leaves = _leaf_count(wrapper)
        summary.append((tag, len(elems), leaves))

    summary.sort(key=lambda r: -r[2])
    lines = ["container                              instances   leaves",
             "-" * 60]
    for tag, inst, leaves in summary:
        lines.append(f"{tag:<38} {inst:>6}   {leaves:>8}")
    report = "\n".join(lines)
    with open(os.path.join(outdir, "_summary.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")

    print("-" * 64)
    print(report)
    print("-" * 64)
    print(f"Saved to: {os.path.abspath(outdir)}")
    print("Send me _summary.txt (and the small per-container .xml files, e.g. "
          "system.xml / optical-amplifier.xml / connections.xml) to map the "
          "audit fields to YANG paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
