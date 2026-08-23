#!/usr/bin/env python3
"""Standalone NETCONF reachability probe for the Nokia 1830 OLS.

Purpose
-------
Confirm — in the real environment, against real gear — whether the 1830 OLS
NETCONF agent (port 830, over SSH) is enabled, authenticates as admin/admin,
and returns YANG data. This is a zero-risk spike: it only reads (<hello> +
one <get>), changes nothing, and is completely separate from the ATLAS audit.

Why it matters
--------------
The ATLAS PSI audit currently scrapes the CLI over the Telnet getty (fragile:
getty two-step, session-exhaustion hangs, ANSI/paging parsing, and the SSH
interactive-shell dead-end). NETCONF would replace all that with structured
YANG. Crucially, NETCONF-over-SSH uses the ``netconf`` *subsystem* — a
different SSH channel than the interactive shell that dead-ends — so it may
work where our interactive SSH stalled. This probe tells us if that's true
here before anyone invests in a NETCONF-based rewrite.

Usage
-----
    python tools/netconf_probe.py [HOST] [USER] [PASS] [PORT]

Defaults: HOST=172.21.109.22  USER=admin  PASS=admin  PORT=830

No third-party deps required (uses paramiko, already shipped with ATLAS).
"""
import re
import socket
import sys
import time

try:
    import paramiko
except ImportError:
    sys.stderr.write("paramiko is required (ships with ATLAS): pip install paramiko\n")
    sys.exit(2)

_EOM = b"]]>]]>"   # NETCONF 1.0 end-of-message framing

# We advertise ONLY base:1.0 so the server uses simple ]]>]]> framing for its
# replies (avoids 1.1 chunked framing in this minimal probe).
CLIENT_HELLO = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    '<capabilities>'
    '<capability>urn:ietf:params:netconf:base:1.0</capability>'
    '</capabilities></hello>'
).encode() + _EOM

# One read-only <get> for the OpenConfig system hostname (what the mature
# Apple ols-audit tool reads first). If the model/namespace differs, the
# server returns an <rpc-error> — still proof that NETCONF works.
GET_HOSTNAME = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rpc message-id="1" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    '<get><filter type="subtree">'
    '<system xmlns="http://openconfig.net/yang/system"><state><hostname/></state></system>'
    '</filter></get></rpc>'
).encode() + _EOM

CLOSE = (
    '<rpc message-id="2" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    '<close-session/></rpc>'
).encode() + _EOM


def _authenticate(transport, user, password):
    """admin/admin first, then the 1830 legacy fallbacks (mirrors ATLAS's
    utils.helpers.nokia_ssh_authenticate without importing the whole stack)."""
    # Prefer ATLAS's shared helper if importable (best compatibility).
    try:
        from utils.helpers import nokia_ssh_authenticate
        nokia_ssh_authenticate(transport, user, password)
        return "utils.helpers.nokia_ssh_authenticate"
    except Exception:
        pass
    transport.auth_password(user, password)   # modern: admin/admin
    return "auth_password"


def _read_until_eom(chan, timeout=20.0):
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            chunk = chan.recv(65535)
            if not chunk:
                break
            buf += chunk
            if _EOM in buf:
                msg, _, rest = buf.partition(_EOM)
                return msg.decode("utf-8", "replace"), rest
        elif chan.closed:
            break
        else:
            time.sleep(0.1)
    return buf.decode("utf-8", "replace"), b""


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "172.21.109.22"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    password = sys.argv[3] if len(sys.argv) > 3 else "admin"
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 830

    print(f"NETCONF probe -> {user}@{host}:{port}\n" + "-" * 60)

    # 1) TCP + SSH transport
    try:
        sock = socket.create_connection((host, port), timeout=15)
    except (OSError, socket.timeout) as exc:
        print(f"[FAIL] TCP connect to {host}:{port} failed: {exc}")
        print("       -> NETCONF port not open/reachable. Is the netconf agent")
        print("          enabled, and is the shelf reachable on 830?")
        return 1

    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=15)
        print(f"[ok]   SSH transport up (server: {transport.remote_version})")
    except Exception as exc:
        print(f"[FAIL] SSH handshake failed: {exc}")
        return 1

    try:
        how = _authenticate(transport, user, password)
        print(f"[ok]   Authenticated as {user!r} (via {how})")
    except paramiko.AuthenticationException:
        print(f"[FAIL] Auth rejected for {user}/<password>. Try real creds:")
        print("       python tools/netconf_probe.py <host> <user> <pass>")
        transport.close()
        return 1
    except Exception as exc:
        print(f"[FAIL] Auth error: {exc}")
        transport.close()
        return 1

    # 2) NETCONF subsystem
    try:
        chan = transport.open_session(timeout=15)
        chan.invoke_subsystem("netconf")
    except Exception as exc:
        print(f"[FAIL] Could not open the 'netconf' SSH subsystem: {exc}")
        print("       -> SSH works but the NETCONF agent may be disabled.")
        transport.close()
        return 1
    print("[ok]   Opened SSH 'netconf' subsystem")

    # 3) <hello> exchange
    chan.sendall(CLIENT_HELLO)
    server_hello, _ = _read_until_eom(chan, timeout=20)
    if "<hello" not in server_hello:
        print("[FAIL] No NETCONF <hello> from server. Got:")
        print("       " + (server_hello[:400] or "<nothing>"))
        transport.close()
        return 1

    caps = re.findall(r"<capability>\s*(.*?)\s*</capability>", server_hello, re.S)
    sess = re.search(r"<session-id>\s*(\d+)", server_hello)
    print(f"[ok]   NETCONF <hello> received  (session-id={sess.group(1) if sess else '?'}, "
          f"{len(caps)} capabilities)")
    base = sorted({c.split(":base:")[-1].split("?")[0] for c in caps if ":netconf:base:" in c})
    print(f"         base versions : {', '.join(base) or '?'}")
    oc = sorted({re.search(r'[?&]module=([^&]+)', c).group(1)
                 for c in caps if "openconfig" in c and "module=" in c})
    nok = sorted({re.search(r'[?&]module=([^&]+)', c).group(1)
                  for c in caps if "module=" in c and "nokia" in c.lower()})
    print(f"         OpenConfig YANG modules: {len(oc)}"
          + (f"  e.g. {', '.join(oc[:4])}" if oc else ""))
    print(f"         Nokia YANG modules     : {len(nok)}"
          + (f"  e.g. {', '.join(nok[:4])}" if nok else ""))
    writable = [c for c in caps if "writable-running" in c or ":candidate" in c]
    print(f"         config datastores      : {'candidate/writable-running present' if writable else 'get-only visible'}")

    # 4) one read-only <get>
    print("-" * 60)
    print("[..]   <get> /system/state/hostname (OpenConfig) ...")
    chan.sendall(GET_HOSTNAME)
    reply, _ = _read_until_eom(chan, timeout=25)
    try:
        chan.sendall(CLOSE)
    except Exception:
        pass
    transport.close()

    hn = re.search(r"<hostname>\s*(.*?)\s*</hostname>", reply, re.S)
    if hn:
        print(f"[ok]   Hostname via NETCONF: {hn.group(1)}")
        verdict = "NETCONF is ENABLED and returned YANG data. A NETCONF-based audit is viable."
    elif "<rpc-error" in reply:
        emsg = re.search(r"<error-message[^>]*>\s*(.*?)\s*</error-message>", reply, re.S)
        print("[warn] Server returned <rpc-error> for that path (model/namespace differs):")
        print("       " + (emsg.group(1)[:300] if emsg else reply[:300]))
        verdict = ("NETCONF is ENABLED (hello + RPC round-trip OK); the sample YANG path "
                   "just differs. Data retrieval works — we'd map the right paths.")
    else:
        print("[warn] Unexpected <get> reply (first 400 chars):")
        print("       " + reply[:400])
        verdict = "NETCONF session worked; <get> reply needs inspection."

    print("-" * 60)
    print("VERDICT: " + verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
