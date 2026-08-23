#!/usr/bin/env python3
"""PSI audit pre-flight: is this connection good for a FULL walk or SEED-ONLY?

Reports, for a given seed, whether its management port answers and whether the PC
is L2-ADJACENT (on-link) or ROUTED (via a gateway) -- which is exactly what
decides whether the OAMP-LAN / CIT workflow engages (full multi-node walk) or the
audit is limited to the seed. Read-only; no config, no routes, no temp IPs.

Usage:
    python tools/psi_reach_check.py [SEED] [RNE]

Examples:
    python tools/psi_reach_check.py 172.21.109.22 10.6.14.133   # OAMP/switch path
    python tools/psi_reach_check.py 172.16.0.1                  # CIT craft port
"""
import socket
import subprocess
import sys


def tcp_ok(host, port, timeout=5.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def onlink(seed):
    """L2-adjacency by DIRECT SUBNET MEMBERSHIP: is a local IPv4 on the seed's
    subnet, via a WIRED NIC? Reliable -- Find-NetRoute NextHop comes back empty on
    a gateway-less on-link NIC (e.g. a static 172.21.109.x with no gateway), which
    previously mis-read as 'routed'. Returns (description, is_wired_onlink)."""
    import ipaddress
    if sys.platform != "win32":
        return "(non-Windows)", False
    try:
        seedip = ipaddress.ip_address(seed)
    except ValueError:
        return "(bad seed IP)", False
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
             "ForEach-Object { \"$($_.IPAddress) $($_.PrefixLength) $($_.InterfaceAlias)\" }"],
            capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return f"(error: {exc})", False
    wifi_hit = None
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or parts[0].startswith("169.254"):
            continue
        try:
            net = ipaddress.ip_network(f"{parts[0]}/{parts[1]}", strict=False)
        except ValueError:
            continue
        if seedip in net and parts[0] != seed:
            alias = parts[2] if len(parts) > 2 else ""
            if "wi-fi" in alias.lower() or "wireless" in alias.lower():
                wifi_hit = wifi_hit or f"local {parts[0]} on Wi-Fi '{alias}'"
            else:
                return f"local {parts[0]} on wired '{alias}'", True
    if wifi_hit:
        return wifi_hit + " (Wi-Fi, not wired)", False
    return "(no local IP on the seed's subnet -> routed)", False


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else "172.21.109.22"
    rne = sys.argv[2] if len(sys.argv) > 2 else None
    is_cit = seed == "172.16.0.1"
    port = 23 if is_cit else 830
    proto = "telnet/CIT" if is_cit else "NETCONF"

    print(f"PSI reach check: seed={seed}" + (f"  rne={rne}" if rne else ""))
    print("-" * 62)
    seed_ok = tcp_ok(seed, port)
    print(f"seed {seed}:{port} ({proto}) reachable : {seed_ok}")
    desc, is_onlink = onlink(seed)
    print(f"L2 adjacency                     : {desc}  "
          f"-> {'ON-LINK (wired, L2-adjacent)' if is_onlink else 'ROUTED / not wired-adjacent'}")
    if rne:
        print(f"RNE {rne}:830 direct reachable : {tcp_ok(rne, 830)}")
    print("-" * 62)

    if is_cit:
        if seed_ok and is_onlink:
            print("=> CIT CRAFT path READY: telnet getty + CIT redistribute + host "
                  "routes -> FULL multi-node walk.")
        elif seed_ok:
            print("=> Seed answers but is ROUTED; the CIT hack needs an on-link "
                  "connection to the craft port (set a 172.16.0.x IP on the wired NIC).")
        else:
            print("=> CIT port not reachable -- check the cable and that your NIC "
                  "has a 172.16.0.x IP (seed 172.16.0.1).")
    else:
        if not seed_ok:
            print("=> Seed NOT reachable on 830 -- audit can't run. Check the "
                  "connection / that your NIC has an IP on the OAMP subnet.")
        elif is_onlink:
            print("=> L2-ADJACENT (wired): OAMP-LAN will engage -> FULL multi-node walk.")
        else:
            print("=> ROUTED / not wired-adjacent: SEED-ONLY audit. OAMP-LAN is "
                  "skipped (routed link left untouched); RNEs need a wired OAMP-LAN "
                  "connection (static 172.21.109.x on the wired NIC) for the full walk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
