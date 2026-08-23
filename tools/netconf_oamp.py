#!/usr/bin/env python3
"""Prove the OAMP OSPF-redistribute toggle works over NETCONF (no telnet getty).

This is the controlled first live write for the OAMP-over-NETCONF change: it
exercises _psi_oamp_netconf.prepare()/restore() against a real seed and verifies
each transition by read-back, always leaving the box in the state it was found.
Run it once; if it prints ALL PASS, the same code is safe to wire into the audit.

Usage:
    python tools/netconf_oamp.py [HOST] [USER] [PASS] [MODE]

MODE: 'status' (default, READ-ONLY -- just reports current state)
      'test'   (enable+verify then disable+verify, restoring the initial state)

Defaults: HOST=172.21.109.22 USER=admin PASS=admin
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from utils.netconf import NetconfSession, NetconfError          # noqa: E402
import scripts.Network._psi_oamp_netconf as oamp                # noqa: E402


def _state(nc):
    return "REDISTRIBUTE (on)" if oamp.is_redistributing(nc) else "disabled (off)"


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "172.21.109.22"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pw = sys.argv[3] if len(sys.argv) > 3 else "admin"
    mode = (sys.argv[4] if len(sys.argv) > 4 else "status").lower()

    print(f"OAMP-over-NETCONF {mode} -> {user}@{host}:830")
    print("-" * 60)
    try:
        with NetconfSession(host, user, pw, timeout=60.0) as nc:
            initial_on = oamp.is_redistributing(nc)
            print(f"[read] current OAMP OSPF state: {_state(nc)}")
            if mode == "status":
                return 0

            ok = True
            try:
                if initial_on:
                    print("[edit] initial is ON -> disable ...")
                    ok &= _expect(oamp.restore(nc, "disable")
                                  and not oamp.is_redistributing(nc),
                                  "disabled", nc)
                    print("[edit] re-enable ...")
                    ready, _ = oamp.prepare(nc)
                    ok &= _expect(ready and oamp.is_redistributing(nc),
                                  "redistribute", nc)
                else:
                    print("[edit] initial is OFF -> enable ...")
                    ready, restore = oamp.prepare(nc)
                    ok &= _expect(ready and oamp.is_redistributing(nc),
                                  "redistribute", nc)
                    print("[edit] disable (restore) ...")
                    ok &= _expect(oamp.restore(nc, restore or "disable")
                                  and not oamp.is_redistributing(nc),
                                  "disabled", nc)
            finally:
                # Always leave the box as found.
                now_on = oamp.is_redistributing(nc)
                if now_on != initial_on:
                    print(f"[safe] restoring initial state ({_state(nc)} -> "
                          f"{'on' if initial_on else 'off'}) ...")
                    if initial_on:
                        oamp.prepare(nc)
                    else:
                        oamp.restore(nc, "disable")
                print(f"[read] final OAMP OSPF state: {_state(nc)}")

            print("-" * 60)
            print("ALL PASS -- safe to wire into the audit." if ok
                  else "FAILED -- do NOT wire in; send me the output above.")
            return 0 if ok else 2
    except NetconfError as exc:
        print(f"[FAIL] {exc}")
        return 1


def _expect(cond, want, nc):
    print(f"       -> now {_state(nc)}   [{'PASS' if cond else 'FAIL'}: expected {want}]")
    return bool(cond)


if __name__ == "__main__":
    sys.exit(main())
