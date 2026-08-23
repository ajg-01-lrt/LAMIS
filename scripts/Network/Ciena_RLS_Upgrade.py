"""
scripts/Network/Ciena_RLS_Upgrade.py — Software upgrade flow for Ciena RLS.

Driven by the Software Upgrades GUI tab. The companion in-process HTTP
server stages the .tgz somewhere reachable on the device's internal CTM
network (typically the PC NIC set to 10.0.0.2 for CTM41 or 10.0.0.6 for
CTM42), then this script SSHes in, issues:

    software-install uri "http://<server>:<port>/<file>.tgz"

and polls the device until the shelf either reaches a finished state or
disconnects the SSH session (which is what RLS does when it warm-resets
to boot the new load).

The script intentionally does not depend on the inventory DB / BaseScript
machinery — it's a one-shot, single-IP operation, so the simpler the call
graph the easier it is to read and reason about.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

import paramiko

from utils.helpers import ensure_host_key_known, get_known_hosts_path, safe_load_host_keys

logger = logging.getLogger(__name__)

# How long to wait between status polls. The upgrade typically runs for
# 10–30 minutes; polling more often than ~10s wastes a CLI round-trip
# without buying any observability.
_POLL_INTERVAL_S = 15.0

# Maximum time to wait for the install to finish before bailing out.
# Real upgrades have been observed at ~25 min; 60 min is a generous cap.
_UPGRADE_TIMEOUT_S = 60 * 60

# Treat an SSH session drop as upgrade-complete only if at least this many
# seconds have elapsed since the install command went out. Drops earlier
# than this almost certainly indicate a connectivity issue, not the shelf
# warm-resetting to boot the new load.
_MIN_ELAPSED_FOR_RESET_S = 60.0

_PROMPT_RE = re.compile(r"([A-Za-z0-9._\-]+#)\s*$")


class RLSUpgradeScript:
    """Run a software-install on a Ciena RLS over SSH and report progress.

    Parameters
    ----------
    ip_address
        Management IP of the RLS to upgrade.
    username, password
        SSH credentials. RLS factory default is ``su / admin``.
    server_url
        Full URL the device will use to pull the upgrade artifact, e.g.
        ``http://10.0.0.2:8000/RLS-04.00.01.5093.tgz``. Caller is
        responsible for ensuring the server is up and the path resolves.
    output_callback
        Receives human-readable progress lines (one per call).
    stop_callback
        Returns True when the user wants to abort polling. Note: once
        the install has actually been issued, aborting only stops local
        polling — it does not roll back the install on the device.
    """

    def __init__(
        self,
        *,
        ip_address: str,
        username: str,
        password: str,
        server_url: str,
        output_callback: Optional[Callable[[str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
        timeout: float = _UPGRADE_TIMEOUT_S,
        poll_interval: float = _POLL_INTERVAL_S,
    ) -> None:
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.server_url = server_url
        self.output_callback = output_callback or (lambda _msg: None)
        self.stop_callback = stop_callback or (lambda: False)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._prompt: str = ""

    # ── Public entry point ──────────────────────────────────────────────────

    def run(self) -> bool:
        """Execute the upgrade. Returns True on apparent success."""
        self._log(f"Connecting to RLS at {self.ip_address} as {self.username}…")

        kh_path = str(get_known_hosts_path())
        if not ensure_host_key_known(self.ip_address, port=22):
            self._log(f"Host key verification failed for {self.ip_address}.")
            return False

        client = paramiko.SSHClient()
        safe_load_host_keys(client, kh_path)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            client.connect(
                self.ip_address,
                port=22,
                username=self.username,
                password=self.password,
                timeout=15,
                banner_timeout=15,
                auth_timeout=15,
                look_for_keys=False,
                allow_agent=False,
            )
        except paramiko.AuthenticationException:
            self._log("SSH authentication failed — check credentials.")
            return False
        except Exception as exc:
            self._log(f"SSH connect error: {exc}")
            return False

        try:
            session = client.invoke_shell(width=200, height=10000)
            session.settimeout(30)

            if not self._detect_prompt(session):
                self._log("Could not detect RLS shell prompt.")
                return False
            self._log(f"Logged in; prompt detected ({self._prompt}).")

            install_cmd = f'software-install uri "{self.server_url}"'
            self._log(f">> {install_cmd}")
            out = self._send(session, install_cmd, timeout=60)
            if out is None:
                self._log("Timed out waiting for install command to return.")
                return False
            # Echo whatever the device printed (typically just an empty line
            # for a successful submission; an error message otherwise).
            cleaned = self._strip_echo(out, install_cmd)
            if cleaned.strip():
                self._log(cleaned.strip())

            self._log(
                f"Install submitted — polling every {self.poll_interval:.0f}s. "
                f"The shelf will warm-reset when the load is complete and the "
                f"SSH session will drop; that is the success signal."
            )
            success = self._poll_until_done(session)
            return success
        finally:
            try:
                session.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    # ── Polling loop ────────────────────────────────────────────────────────

    def _poll_until_done(self, session) -> bool:
        start = time.monotonic()
        last_state = ""
        while True:
            if self.stop_callback():
                self._log(
                    "User requested stop. Polling halted; the install on the "
                    "device will continue independently."
                )
                return False

            elapsed = time.monotonic() - start
            if elapsed > self.timeout:
                self._log(
                    f"Upgrade exceeded {self.timeout/60:.0f} min cap without "
                    "completing — giving up local polling."
                )
                return False

            state_out = self._send(
                session, "show software upgrade-operational-state", timeout=20
            )
            if state_out is None:
                # Likely the shelf reset and dropped us — treat as success
                # IFF we've been running long enough for that to be plausible.
                if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                    self._log(
                        f"SSH session went silent after {elapsed:.0f}s — "
                        f"shelf has most likely warm-reset to boot the new "
                        f"load. Treating upgrade as complete."
                    )
                    return True
                self._log(
                    f"SSH went silent only {elapsed:.0f}s in — too early "
                    f"for a warm reset. Aborting."
                )
                return False

            state = self._parse_op_state(state_out)
            previous_state = last_state
            if state and state != last_state:
                self._log(f"  upgrade-operational-state: {state}")
                last_state = state

            # Device-side completion: op-state went back to idle/none after
            # being seen as in-progress. Don't trust idle-from-the-start
            # (could mean the install never started).
            if previous_state and state == "idle" and previous_state != state:
                self._log("Operational state returned to idle — upgrade done.")
                return True

            time.sleep(self.poll_interval)

    @staticmethod
    def _parse_op_state(out: str) -> str:
        """Extract the ``upgrade-operational-state`` value from RLS output.

        The CLI prints YAML-ish:

            software:
                upgrade-operational-state   : load-in-progress
        """
        m = re.search(
            r"upgrade-operational-state\s*:\s*([A-Za-z0-9\-_]+)", out
        )
        return m.group(1).strip().lower() if m else ""

    # ── Low-level helpers ───────────────────────────────────────────────────

    def _send(self, session, cmd: str, timeout: float = 30.0) -> Optional[str]:
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            logger.debug("send failed: %s", exc)
            return None
        return self._read_until_prompt(session, timeout=timeout)

    def _detect_prompt(self, session) -> bool:
        time.sleep(2.0)
        buf = self._drain(session, idle_seconds=1.5, max_wait=8.0)
        m = _PROMPT_RE.search(buf)
        for _ in range(5):
            if m:
                break
            if self.stop_callback():
                return False
            try:
                session.send("\n")
            except Exception:
                return False
            time.sleep(1.0)
            buf += self._drain(session, idle_seconds=1.0, max_wait=4.0)
            m = _PROMPT_RE.search(buf)
        if not m:
            return False
        self._prompt = m.group(1)
        return True

    @staticmethod
    def _drain(session, idle_seconds: float = 1.0, max_wait: float = 5.0) -> str:
        deadline = time.time() + max_wait
        last_read = time.time()
        buf = bytearray()
        while time.time() < deadline:
            if session.recv_ready():
                chunk = session.recv(65535)
                if chunk:
                    buf.extend(chunk)
                    last_read = time.time()
            else:
                if time.time() - last_read >= idle_seconds and buf:
                    break
                time.sleep(0.1)
        return buf.decode("utf-8", errors="replace")

    def _read_until_prompt(self, session, timeout: float) -> Optional[str]:
        """Read from session until our captured prompt is the last thing.

        Returns None on timeout or on session-closed (which is the upgrade
        completion signal — caller distinguishes via elapsed time).
        """
        deadline = time.time() + timeout
        buf = bytearray()
        prompt_b = self._prompt.encode("utf-8")
        while time.time() < deadline:
            if self.stop_callback():
                return None
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if not chunk:
                        # Remote closed the channel
                        return None
                    buf.extend(chunk)
                else:
                    stripped = bytes(buf).replace(b"\r", b"").rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.4)
                        if not session.recv_ready():
                            return buf.decode("utf-8", errors="replace")
                        continue
                    if session.closed or session.exit_status_ready():
                        return None
                    time.sleep(0.1)
            except Exception as exc:
                logger.debug("recv error: %s", exc)
                return None
        return None

    @staticmethod
    def _strip_echo(out: str, cmd: str) -> str:
        """Remove the echoed command line and trailing prompt from output."""
        lines = out.splitlines()
        # Drop the first line if it's the command echo
        if lines and cmd in lines[0]:
            lines = lines[1:]
        # Drop trailing prompt line
        if lines and re.match(r"^[A-Za-z0-9._\-]+#\s*$", lines[-1].strip()):
            lines = lines[:-1]
        return "\n".join(lines)

    def _log(self, msg: str) -> None:
        try:
            self.output_callback(msg)
        except Exception:
            pass
        logger.info("[RLS-Upgrade %s] %s", self.ip_address, msg)
