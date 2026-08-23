#!/usr/bin/env python3
"""Probe how to reach a DCN-only RNE's NETCONF from a REMOTE PC (over WiFi/corp),
where the audit PC is NOT on the 10.6.14.x DCN and NOT L2-adjacent to the GNE
OAMP (so the on-site OAMP-LAN temp-DCN can't form).

Tries two paths to the RNE and reports which works:
  1. DIRECT   PC -> RNE:830          (works only if the customer DCN routes the
                                      RNE loopback range to you)
  2. JUMP     PC -> GNE:22 -> RNE:830 (SSH direct-tcpip forward through the seed;
                                      works if the seed's SSH allows forwarding)

Read-only: just opens NETCONF and reads the RNE hostname on each path.

Usage:
    python tools/netconf_jump_probe.py SEED RNE [USER] [PASS]
Example:
    python tools/netconf_jump_probe.py 172.21.109.22 10.6.14.133 admin admin
"""
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from utils.netconf import NetconfSession, NetconfError          # noqa: E402


def _try(label, **kw):
    t0 = time.time()
    try:
        with NetconfSession(timeout=60.0, connect_timeout=20.0, **kw) as nc:
            data = nc.get(subtree='<system xmlns="http://openconfig.net/yang/system"/>')
            hn = NetconfSession.first_text(data, "hostname")
        print(f"[ OK ] {label}: reached RNE, hostname={hn}  ({time.time()-t0:.1f}s)")
        return True
    except NetconfError as exc:
        print(f"[FAIL] {label}: {exc}  ({time.time()-t0:.1f}s)")
        return False


def main():
    if len(sys.argv) < 3:
        print("usage: netconf_jump_probe.py SEED RNE [USER] [PASS]")
        return 2
    seed = sys.argv[1]
    rne = sys.argv[2]
    user = sys.argv[3] if len(sys.argv) > 3 else "admin"
    pw = sys.argv[4] if len(sys.argv) > 4 else "admin"

    print(f"Remote RNE reachability probe: seed(GNE)={seed}  RNE={rne}")
    print("-" * 64)
    direct = _try(f"DIRECT   PC->{rne}:830", host=rne, username=user, password=pw)
    # The seed's NETCONF SSH (830) is reachable remotely; the CLI SSH (22) often
    # is not. Try forwarding through 830 first, then 22.
    jump830 = _try(f"JUMP:830 PC->{seed}:830->{rne}:830",
                   host=rne, username=user, password=pw,
                   jump=(seed, user, pw, 830))
    jump22 = _try(f"JUMP:22  PC->{seed}:22->{rne}:830",
                  host=rne, username=user, password=pw,
                  jump=(seed, user, pw, 22))
    print("-" * 64)
    if direct:
        print("=> DIRECT works: the customer DCN routes the RNE range to you. "
              "The audit can direct-connect remotely (no jump needed).")
    elif jump830 or jump22:
        jp = 830 if jump830 else 22
        print(f"=> JUMP via seed:{jp} works: tunnel NETCONF to RNEs through the "
              f"seed. I'll wire SSH-jump (port {jp}) into the audit.")
    else:
        print("=> Neither path reached the RNE. If the seed's 830 refused "
              "forwarding, its SSH server has port-forwarding disabled -> remote "
              "multi-node then needs customer DCN provisioning (route "
              "10.6.14.0/24 -> GNE OAMP + GNE forwarding) or on-site craft.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
