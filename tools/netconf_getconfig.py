#!/usr/bin/env python3
"""Capture a Nokia 1830 OLS node's running CONFIG datastore via NETCONF.

Read-only (<get-config source=running>). Used to find the EXACT writable YANG
path for the OAMP OSPF "routestate redistribute" setting before building an
edit-config for it -- so the audit can set/restore OAMP redistribution over
NETCONF instead of the fragile telnet getty.

Usage:
    python tools/netconf_getconfig.py [HOST] [USER] [PASS] [OUTDIR]

Defaults: HOST=172.21.109.22  USER=admin  PASS=admin
          OUTDIR=netconf_config_<host>

IMPORTANT: run this with the OAMP redistribute *enabled* (so the leaf is present
in running-config). If it is currently disabled, enable it once from PuTTY:
    config interface mfc 1/10/oamp routestate redistribute
then run this, and (optionally) set it back with ... routestate disabled.

Writes:
    <OUTDIR>/_full_config.xml       full running <data> (pretty, ns-stripped)
    <OUTDIR>/<container>.xml        one file per top-level container
    <OUTDIR>/_spotlight.txt         every element referencing OAMP / OSPF /
                                    redistribute / routestate -- the candidates
"""
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from lxml import etree                                     # noqa: E402
from utils.netconf import NetconfSession, NetconfError     # noqa: E402

_KEYWORDS = ("oamp", "redistribut", "routestate", "route-state",
             "ospf", "mfc")


def _spotlight(data):
    """Return pretty XML of every element (with a little parent context) whose
    tag or text mentions an OAMP/OSPF/redistribute keyword -- the candidates for
    the writable routestate leaf."""
    hits, seen = [], set()
    for e in data.iter():
        tag = e.tag if isinstance(e.tag, str) else ""
        txt = (e.text or "")
        blob = (tag + " " + txt).lower()
        if any(k in blob for k in _KEYWORDS):
            # climb to a small enclosing container for readable context
            ctx = e
            for _ in range(3):
                if ctx.getparent() is not None:
                    ctx = ctx.getparent()
            key = id(ctx)
            if key in seen:
                continue
            seen.add(key)
            hits.append(etree.tostring(ctx, pretty_print=True).decode())
    return hits


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "172.21.109.22"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    password = sys.argv[3] if len(sys.argv) > 3 else "admin"
    outdir = sys.argv[4] if len(sys.argv) > 4 else f"netconf_config_{host.replace(':', '_')}"

    print(f"NETCONF get-config -> {user}@{host}:830   (read-only, running datastore)")
    print("-" * 64)
    t0 = time.time()
    try:
        with NetconfSession(host, user, password, timeout=180.0) as nc:
            print(f"[ok]   connected; {len(nc.server_capabilities)} capabilities")
            print("[..]   <get-config source=running> ...")
            data = nc.get_config("running")
    except NetconfError as exc:
        print(f"[FAIL] {exc}")
        return 1
    dt = time.time() - t0

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "_full_config.xml"), "wb") as f:
        f.write(etree.tostring(data, pretty_print=True))
    print(f"[ok]   running-config: {len(data)} top-level containers, "
          f"{sum(1 for _ in data.iter())} elements in {dt:.1f}s")

    grouped = {}
    for child in data:
        tag = child.tag if isinstance(child.tag, str) else "unknown"
        grouped.setdefault(tag, []).append(child)
    for tag, elems in sorted(grouped.items()):
        wrapper = etree.Element(tag + "_config")
        for e in elems:
            wrapper.append(e)
        with open(os.path.join(outdir, f"{tag}.xml"), "wb") as f:
            f.write(etree.tostring(wrapper, pretty_print=True))

    hits = _spotlight(data)
    spot = os.path.join(outdir, "_spotlight.txt")
    with open(spot, "w", encoding="utf-8") as f:
        f.write(f"# OAMP/OSPF/redistribute candidates from {host} running-config\n")
        f.write(f"# {len(hits)} context block(s)\n\n")
        f.write("\n".join(hits) if hits else "(no matching elements found)\n")

    print("-" * 64)
    print(f"Spotlight: {len(hits)} OAMP/OSPF/redistribute context block(s) "
          f"-> {os.path.basename(spot)}")
    print(f"Saved to: {os.path.abspath(outdir)}")
    print("Send me _spotlight.txt (and network-instances.xml + interfaces.xml if "
          "present) so I can pin the exact writable routestate path.")
    if not hits:
        print("NOTE: no matches -- OAMP redistribute may be DISABLED (default "
              "values are omitted from running-config). Enable it via PuTTY "
              "(config interface mfc 1/10/oamp routestate redistribute) and re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
