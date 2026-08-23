"""
scripts/Network/Nokia_PSS_Upgrade.py — Software upgrade flow for Nokia PSS.

Identical to the Nokia PSI flow (see ``Nokia_PSI_Upgrade.py``) except the
PSS does not accept ``config software server port 8000`` — on PSS-class
shelves that command hangs until the prompt-wait times out, so it is
omitted from the server-config sequence here. Everything else (the
1830-style two-stage login, FTP/HTTP server setup, audit/load/activate)
is the same.

The PSS fetches its load from the local HTTP server staged by the
Software Upgrades GUI tab (PC at 172.16.0.101, PSS at 172.16.0.1). Login
mechanics mirror the Nokia 1830: SSH as ``cli`` then a second inner
Username/Password dialog on the shell channel. Once we're in, the
upgrade sequence is:

  1. config general ftpserver enable
  2. config software server ip 172.16.0.101
  3. config software server userid UserSWNE
     <password prompt> → "Ftpid#1"
  4. config software server root /CC/
  5. config software server protocol HTTP
  6. config software upgrade manual audit <filename> nobackup
  7. config software upgrade manual load
  8. config software upgrade status      (poll until "complete")
  9. config software upgrade manual activate yes

The PSS fetches files at ``http://172.16.0.101:8000/CC/<filename>``, so
the operator's selected software folder needs a ``CC/`` subdirectory
containing the load files.
"""
from __future__ import annotations

import logging
import re
import socket
import time
from typing import Callable, Optional

import paramiko

from utils.helpers import ensure_host_key_known, nokia_ssh_authenticate

logger = logging.getLogger(__name__)

# Hardcoded FTP/HTTP server credential the PSS uses for its server slot.
# This is the literal password Nokia's procedure specifies — the PSS does
# not actually authenticate against the HTTP server, but the command
# sequence still requires it to be set.
_FTP_PASSWORD = "Ftpid#1"

# How long to wait between `config software upgrade status` polls. Per
# field guidance the load takes ~5 minutes, so polling every 20s gives a
# reasonable cadence without thrashing the CLI.
_STATUS_POLL_INTERVAL_S = 20.0
_STATUS_TIMEOUT_S = 30 * 60  # 30 min cap

# Activate triggers a chassis reboot — same success signal as G42.
_ACTIVATE_DROP_TIMEOUT_S = 5 * 60
_MIN_ELAPSED_FOR_RESET_S = 30.0

_PROMPT_TIMEOUT_S = 30

# 1830/PSS prompt formats. They typically end with "#" but also "$" for
# certain user accounts. Allow either.
_PROMPT_RE = re.compile(r"([A-Za-z0-9._@\-]+[#>\$])\s*$")
_PASSWORD_RE = re.compile(r"(?i)password\s*:\s*$")
_USERNAME_RE = re.compile(r"(?i)(username|login)\s*:\s*$")
_ACK_RE = re.compile(
    r"(?i)\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|continue\?|accept\?|press\s+y"
)
_STATUS_COMPLETE_RE = re.compile(r"(?i)\b(complete|completed|success|loaded)\b")
_STATUS_FAILED_RE = re.compile(r"(?i)\b(fail|failed|error|aborted)\b")


class NokiaPSSUpgradeScript:
    """Drive a Nokia PSS software upgrade end to end over SSH."""

    def __init__(
        self,
        *,
        ip_address: str,
        username: str = "cli",
        password: str = "admin",
        inner_username: str = "admin",
        inner_password: Optional[str] = None,
        software_filename: str,
        output_callback: Optional[Callable[[str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.inner_username = inner_username
        # PSS / 1830 convention: inner password defaults to the same value
        # as the outer SSH password unless the operator overrides it.
        self.inner_password = inner_password if inner_password is not None else password
        self.software_filename = software_filename
        self.output_callback = output_callback or (lambda _msg: None)
        self.stop_callback = stop_callback or (lambda: False)
        self._prompt: str = ""

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self) -> bool:
        self._log(f"Connecting to PSS at {self.ip_address} as {self.username}…")
        if not ensure_host_key_known(self.ip_address, port=22):
            self._log(f"Host key verification failed for {self.ip_address}.")
            return False

        # PSS is 1830-class: the outer SSH user ('cli') has no real SSH
        # password. Plain password auth is rejected outright, so we mirror
        # the working Nokia_1830 flow — auth_none, falling back to an empty
        # keyboard-interactive response. The real admin/admin credentials
        # are entered at the inner Username:/Password: shell prompts by
        # `_two_stage_login`, not here.
        sock = None
        transport = None
        session = None
        try:
            try:
                sock = socket.create_connection((self.ip_address, 22), timeout=15)
                transport = paramiko.Transport(sock)
                transport.start_client(timeout=15)
            except Exception as exc:
                self._log(f"SSH connect error: {exc}")
                return False

            try:
                # SSH-layer auth: modern shelves accept admin/admin password auth
                # and drop straight to the shell; legacy shelves use a passwordless
                # 'cli' account + the inner two-stage in _two_stage_login. Try the
                # admin (inner) creds first.
                nokia_ssh_authenticate(transport, self.inner_username, self.inner_password)
                logging.info("[PSS] SSH auth succeeded for %s@%s",
                             self.inner_username, self.ip_address)
            except (paramiko.AuthenticationException, paramiko.SSHException) as exc:
                self._log(
                    f"SSH authentication was rejected ({exc}). Check the login "
                    "credentials for this shelf (default admin/admin)."
                )
                return False

            session = transport.open_session()
            session.get_pty(width=200, height=10000)
            session.invoke_shell()
            session.settimeout(30)

            if not self._two_stage_login(session):
                self._log("PSS two-stage login did not reach a shell prompt.")
                return False
            self._log(f"Two-stage login successful; prompt detected ({self._prompt}).")

            if not self._configure_server(session):
                return False
            if not self._run_upgrade_phases(session):
                return False
            return self._activate(session)
        finally:
            try:
                if session:
                    session.close()
            except Exception:
                pass
            try:
                if transport:
                    transport.close()
            except Exception:
                pass

    # ── Phase 1 — login ─────────────────────────────────────────────────────

    def _two_stage_login(self, session) -> bool:
        """SSH gave us a banner with the inner ``Username:`` prompt. Answer
        it with ``inner_username``, then the password prompt with
        ``inner_password``, ack any Y/n EULA banners, settle on a shell
        prompt. Mirrors ``DeviceIdentifier._do_1830_two_stage_login``."""
        banner = self._drain(session, idle_seconds=1.5, max_wait=8.0)
        if not _USERNAME_RE.search(banner):
            # Modern shelves (admin/admin SSH auth) drop straight to the shell —
            # no inner Username:/Password:. Accept that if a prompt is present.
            m = _PROMPT_RE.search(banner)
            if m:
                self._prompt = m.group(1)
                self._log(
                    f"At shell prompt after SSH auth ({self._prompt}); "
                    "no inner login needed."
                )
                return True
            self._log(
                "No inner Username: prompt and no shell prompt after SSH auth — "
                "is this really a PSS / 1830-class device?"
            )
            return False

        self._log(f"Sending inner username: {self.inner_username}")
        session.send(f"{self.inner_username}\n")
        pw_banner = self._drain(session, idle_seconds=1.0, max_wait=8.0)
        if not _PASSWORD_RE.search(pw_banner):
            self._log("No inner password prompt after sending username.")
            return False

        self._log("Sending inner password")
        session.send(f"{self.inner_password}\n")
        post = self._drain(session, idle_seconds=1.5, max_wait=10.0)
        if re.search(r"(?i)(incorrect|invalid|fail|denied)", post):
            self._log("Inner login rejected.")
            return False

        # Some builds throw a Y/n EULA banner before dropping to the shell.
        for _ in range(3):
            m = _PROMPT_RE.search(post)
            if m:
                self._prompt = m.group(1)
                return True
            if _ACK_RE.search(post):
                self._log("Acknowledging post-login Y/n banner")
                session.send("y\n")
                post = self._drain(session, idle_seconds=1.0, max_wait=8.0)
                continue
            break

        m = _PROMPT_RE.search(post)
        if not m:
            return False
        self._prompt = m.group(1)
        return True

    # ── Phase 2 — FTP/HTTP server config ────────────────────────────────────

    def _configure_server(self, session) -> bool:
        commands = [
            "config general ftpserver enable",
            "config software server ip 172.16.0.101",
        ]
        for cmd in commands:
            if not self._send(session, cmd):
                return False

        # userid command — afterwards the device prompts for an FTP password.
        self._log(f">> config software server userid UserSWNE")
        try:
            session.send("config software server userid UserSWNE\n")
        except Exception as exc:
            self._log(f"send error: {exc}")
            return False
        # Read until we see either a password prompt or the regular prompt.
        deadline = time.time() + _PROMPT_TIMEOUT_S
        buf = bytearray()
        prompt_b = self._prompt.encode("utf-8")
        sent_password = False
        while time.time() < deadline:
            if self.stop_callback():
                return False
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if not chunk:
                        return False
                    buf.extend(chunk)
                    tail = bytes(buf[-200:]).decode("utf-8", errors="replace")
                    if not sent_password and _PASSWORD_RE.search(tail):
                        self._log(f"  (sending FTP password)")
                        session.send(f"{_FTP_PASSWORD}\n")
                        sent_password = True
                        buf = bytearray()
                        deadline = time.time() + _PROMPT_TIMEOUT_S
                        continue
                else:
                    stripped = bytes(buf).replace(b"\r", b"").rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.3)
                        if not session.recv_ready():
                            self._echo_relevant(
                                bytes(buf).decode("utf-8", errors="replace"),
                                "config software server userid UserSWNE",
                            )
                            break
                    time.sleep(0.1)
            except Exception as exc:
                self._log(f"recv error: {exc}")
                return False
        else:
            self._log("Timed out waiting for FTP password / prompt return.")
            return False

        if not sent_password:
            self._log(
                "No FTP password prompt appeared — proceeding, but the load "
                "may fail if the device expected one."
            )

        # NOTE: unlike the PSI flow, the PSS does NOT accept
        # ``config software server port 8000`` — on PSS-class shelves that
        # command hangs until the prompt-wait times out, so it is omitted.
        for cmd in (
            "config software server root /CC/",
            "config software server protocol HTTP",
        ):
            if not self._send(session, cmd):
                return False
        return True

    # ── Phase 3 — audit + load + status polling ─────────────────────────────

    def _run_upgrade_phases(self, session) -> bool:
        audit = f"config software upgrade manual audit {self.software_filename} nobackup"
        if not self._send(session, audit, timeout=2 * 60):
            return False
        if not self._send(session, "config software upgrade manual load", timeout=2 * 60):
            return False

        self._log(
            f"Polling 'config software upgrade status' every "
            f"{_STATUS_POLL_INTERVAL_S:.0f}s — expect ~5 min for the load."
        )
        start = time.monotonic()
        while True:
            if self.stop_callback():
                self._log("Stop requested during status poll.")
                return False
            if time.monotonic() - start > _STATUS_TIMEOUT_S:
                self._log(
                    f"Load did not complete within {_STATUS_TIMEOUT_S/60:.0f} min."
                )
                return False
            out = self._send_capture(session, "config software upgrade status", timeout=60)
            if out is None:
                self._log("Status command returned no output — bailing.")
                return False
            self._echo_relevant(out, "config software upgrade status")
            if _STATUS_FAILED_RE.search(out):
                self._log("Status reports a failure.")
                return False
            if _STATUS_COMPLETE_RE.search(out):
                self._log("Load reported complete.")
                return True
            time.sleep(_STATUS_POLL_INTERVAL_S)

    # ── Phase 4 — activate ──────────────────────────────────────────────────

    def _activate(self, session) -> bool:
        cmd = "config software upgrade manual activate yes"
        self._log(f">> {cmd}  (device will reboot — SSH will drop)")
        start = time.monotonic()
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            self._log(f"send error on activate: {exc}")
            return False

        deadline = time.time() + _ACTIVATE_DROP_TIMEOUT_S
        while time.time() < deadline:
            if self.stop_callback():
                self._log("Stop requested during activate.")
                return False
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if chunk:
                        for line in chunk.decode("utf-8", errors="replace").splitlines():
                            if line.strip():
                                self._log(f"  {line.rstrip()}")
                elif session.closed or session.exit_status_ready():
                    elapsed = time.monotonic() - start
                    if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                        self._log(
                            f"SSH closed after {elapsed:.0f}s — PSS is "
                            "rebooting into the new load. Treating as success."
                        )
                        return True
                    self._log(
                        f"SSH closed only {elapsed:.0f}s in — too early to "
                        "credit as a reboot. Failing."
                    )
                    return False
                else:
                    time.sleep(0.5)
            except Exception as exc:
                elapsed = time.monotonic() - start
                if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                    self._log(
                        f"SSH channel error after {elapsed:.0f}s ({exc}); "
                        "treating as expected reboot."
                    )
                    return True
                self._log(f"SSH channel error too early: {exc}")
                return False

        self._log("Activate did not result in a session drop within timeout.")
        return False

    # ── Send helpers ────────────────────────────────────────────────────────

    def _send(self, session, cmd: str, timeout: float = _PROMPT_TIMEOUT_S) -> bool:
        """Send a command, wait for prompt, log output. Returns False on
        timeout or send error."""
        self._log(f">> {cmd}")
        out = self._send_capture(session, cmd, timeout=timeout)
        if out is None:
            return False
        self._echo_relevant(out, cmd)
        return True

    def _send_capture(
        self, session, cmd: str, timeout: float
    ) -> Optional[str]:
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            logger.debug("send failed: %s", exc)
            return None
        return self._read_until_prompt(session, timeout=timeout)

    # ── Reading / prompt detection ──────────────────────────────────────────

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
                        return None
                    buf.extend(chunk)
                else:
                    stripped = bytes(buf).replace(b"\r", b"").rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.3)
                        if not session.recv_ready():
                            return buf.decode("utf-8", errors="replace")
                        continue
                    if session.closed or session.exit_status_ready():
                        return None
                    time.sleep(0.15)
            except Exception as exc:
                logger.debug("recv error: %s", exc)
                return None
        return None

    def _echo_relevant(self, out: str, cmd: str) -> None:
        lines = out.splitlines()
        if lines and cmd in lines[0]:
            lines = lines[1:]
        if lines and _PROMPT_RE.match(lines[-1].strip()):
            lines = lines[:-1]
        for line in lines:
            line = line.rstrip()
            if line:
                self._log(f"  {line}")

    def _log(self, msg: str) -> None:
        try:
            self.output_callback(msg)
        except Exception:
            pass
        logger.info("[PSS-Upgrade %s] %s", self.ip_address, msg)
