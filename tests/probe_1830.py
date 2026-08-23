"""Standalone Nokia 1830 telnet probe — prints raw bytes for diagnosis.

Usage:
    python scripts/probe_1830.py <ip> [username] [password]

Prints every chunk of bytes received from the device, with timestamps and
hex/ascii dumps. No parsing, no policy, no GUI.
"""
import os
import re
import sys
import time
import socket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.telnet import Telnet  # noqa: E402


COMMANDS = [
    "show general detail",   # proven to work in ID probe
    "show general name",
    "show shelf inventory *",
    "show card inventory *",
    "show interface inventory *",
]


def hex_preview(b: bytes, n: int = 80) -> str:
    sample = b[:n]
    hexpart = " ".join(f"{x:02x}" for x in sample)
    asciipart = "".join(chr(x) if 32 <= x < 127 else "." for x in sample)
    more = f" (+{len(b) - n} more bytes)" if len(b) > n else ""
    return f"hex={hexpart} | ascii={asciipart!r}{more}"


def stamp() -> str:
    return time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"


def drain(tn: Telnet, label: str, max_seconds: float = 8.0,
          quiet_after: float = 1.5) -> bytes:
    """Print all bytes received until idle for `quiet_after` seconds."""
    print(f"\n[{stamp()}] -- DRAIN '{label}' (max {max_seconds}s) --")
    buf = bytearray()
    end = time.time() + max_seconds
    last = time.time()
    while time.time() < end:
        try:
            chunk = tn.read_very_eager()
        except Exception as e:
            print(f"[{stamp()}] read raised: {e}")
            break
        if chunk:
            buf.extend(chunk)
            last = time.time()
            print(f"[{stamp()}] +{len(chunk)}B {hex_preview(chunk)}")
        else:
            if time.time() - last >= quiet_after and buf:
                print(f"[{stamp()}] idle {quiet_after}s — done")
                break
            time.sleep(0.1)
    print(f"[{stamp()}] -- DRAIN total {len(buf)} bytes --")
    return bytes(buf)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ip = sys.argv[1]
    user = sys.argv[2] if len(sys.argv) >= 3 else "admin"
    pw = sys.argv[3] if len(sys.argv) >= 4 else "admin"

    print(f"[{stamp()}] Connecting to {ip}:23 as {user!r}/{pw!r}")
    try:
        tn = Telnet(ip, port=23, timeout=10, bypass_policy=True,
                    purpose="probe-1830-raw")
    except Exception as e:
        print(f"FAILED to open telnet: {e}")
        return 1

    try:
        # ---- Three-stage 1830 login (mirrors _identify_via_telnet_1830) ----
        print(f"[{stamp()}] Waiting for 'login: '")
        b = tn.read_until(b"login: ", timeout=5)
        print(f"[{stamp()}] Got {len(b)}B {hex_preview(b)}")
        if b"login:" not in b.lower():
            print(f"[{stamp()}] ABORT: no 'login:' prompt — not a 1830")
            return 1
        print(f"[{stamp()}] Sending 'cli'")
        tn.write(b"cli\n")

        print(f"[{stamp()}] Waiting for 'Username: '")
        b = tn.read_until(b"Username: ", timeout=5)
        print(f"[{stamp()}] Got {len(b)}B {hex_preview(b)}")
        print(f"[{stamp()}] Sending username {user!r}")
        tn.write(user.encode("ascii", errors="ignore") + b"\n")

        print(f"[{stamp()}] Waiting for 'Password: '")
        b = tn.read_until(b"Password: ", timeout=5)
        print(f"[{stamp()}] Got {len(b)}B {hex_preview(b)}")
        print(f"[{stamp()}] Sending password")
        tn.write(pw.encode("ascii", errors="ignore") + b"\n")

        # 1s settle, then ONE eager drain of the banner — exactly what the
        # working ID probe does. Do NOT send SPACE or anything else.
        time.sleep(1.0)
        try:
            login_resp = tn.read_very_eager().decode("ascii", errors="ignore")
        except Exception as e:
            print(f"[{stamp()}] post-login read failed: {e}")
            return 1
        print(f"[{stamp()}] post-login banner: {len(login_resp)} chars")
        print(login_resp)

        if ("Login incorrect" in login_resp
                or "invalid" in login_resp.lower()
                or "denied" in login_resp.lower()):
            print(f"[{stamp()}] ABORT: login rejected")
            return 1

        # Optional Y/n acknowledgement banner some 1830s show
        if re.search(r"(?i)\(\s*y\s*/\s*n\s*\)|acknowledge", login_resp):
            print(f"[{stamp()}] Y/n acknowledgement detected — sending 'y'")
            tn.write(b"y\n")
            time.sleep(0.5)
            try:
                ack = tn.read_very_eager().decode("ascii", errors="ignore")
                print(f"[{stamp()}] post-ack: {ack!r}")
            except Exception:
                pass

        # ---- Run each command using the proven pattern ----
        for cmd in COMMANDS:
            print(f"\n[{stamp()}] >>> SENDING: {cmd!r}")
            try:
                tn.write(cmd.encode("ascii", errors="ignore") + b"\n")
            except Exception as e:
                print(f"[{stamp()}] write failed: {e}")
                break

            # Mirror ID probe exactly: 2s sleep → read_until '#' → eager drain
            time.sleep(2.0)
            try:
                blob = tn.read_until(b"#", timeout=10)
            except Exception as e:
                print(f"[{stamp()}] read_until failed: {e}")
                blob = b""
            try:
                tail = tn.read_very_eager()
            except Exception:
                tail = b""
            data = blob + tail
            print(f"[{stamp()}] read_until got {len(blob)}B + tail {len(tail)}B")

            # Pager handling — only if device actually shows one
            pager_hits = 0
            while (b"Press any key to continue" in data
                   or b"--More--" in data
                   or b"(More)" in data) and pager_hits < 50:
                pager_hits += 1
                print(f"[{stamp()}] PAGER #{pager_hits} — sending SPACE")
                try:
                    tn.write(b" ")
                except Exception as e:
                    print(f"[{stamp()}] pager write failed: {e}")
                    break
                time.sleep(0.5)
                try:
                    more_blob = tn.read_until(b"#", timeout=8)
                    more_tail = tn.read_very_eager()
                except Exception as e:
                    print(f"[{stamp()}] pager read failed: {e}")
                    break
                data += more_blob + more_tail
                if not more_blob and not more_tail:
                    break

            text = data.decode("ascii", errors="replace")
            print(f"\n[{stamp()}] === DECODED OUTPUT for {cmd!r} "
                  f"({len(text)} chars) ===")
            print(text)
            print("=" * 60)

        try:
            tn.write(b"exit\n")
        except Exception:
            pass
    finally:
        try:
            tn.close()
        except Exception:
            pass
    print(f"[{stamp()}] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
