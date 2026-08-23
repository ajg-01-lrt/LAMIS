"""
scripts/Network/Nokia_PSI_Upgrade.py — Software upgrade flow for Nokia PSI.

The PSI fetches its load from the local HTTP server staged by the
Software Upgrades GUI tab (PC at 172.16.0.101, PSI at 172.16.0.1). Login
uses the Nokia 1830-family getty three-stage over **Telnet** — SSH-as-
``admin`` authenticates but then dead-ends at the alarm banner (``admin``
is not the CLI account, and the session ignores input), so the usable CLI
is only reachable via the Telnet getty: ``login:`` -> ``cli``,
``Username:`` -> ``admin``, ``Password:`` -> ``admin``. This mirrors the
inventory PSI path (see ``scripts/_nokia_1830_family`` and
``reference_nokia_1830_class_access``). Once we're in, the upgrade
sequence is:

  1. config general ftpserver enable
  2. config software server ip 172.16.0.101
  3. config software server userid UserSWNE
     <password prompt> → "Ftp-id#1"
  4. config software server root /CC/
  5. config software server protocol HTTP
  6. config software server port 8000
  7. config software upgrade manual audit <filename> nobackup
  8. config software upgrade manual load
  9. config software upgrade status      (poll until "complete")
  10. config software upgrade manual activate yes

The PSI fetches files at ``http://172.16.0.101:8000/CC/<filename>``, so
the operator's selected software folder needs a ``CC/`` subdirectory
containing the load files.
"""
from __future__ import annotations

import logging
import re
import select
import socket
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Hardcoded FTP/HTTP server credential the PSI uses for its server slot.
# This is the literal password Nokia's procedure specifies — the PSI does
# not actually authenticate against the HTTP server, but the command
# sequence still requires it to be set.
_FTP_PASSWORD = "Ftp-id#1"

# How long to wait between `config software upgrade status` polls. Per
# field guidance the load takes ~5 minutes, so polling every 20s gives a
# reasonable cadence without thrashing the CLI.
_STATUS_POLL_INTERVAL_S = 20.0
_STATUS_TIMEOUT_S = 30 * 60  # 30 min cap

# Activate triggers a chassis reboot — same success signal as G42.
_ACTIVATE_DROP_TIMEOUT_S = 5 * 60
_MIN_ELAPSED_FOR_RESET_S = 30.0
# Grace period after sending `activate` to catch a fast rejection (the shelf
# refuses in ~0.5s if the load isn't complete) before announcing success.
_ACTIVATE_GRACE_S = 3.0

_PROMPT_TIMEOUT_S = 30

# Telnet transport: socket-connect + default per-read timeout, and the per
# getty-prompt wait for the login:/Username:/Password: dialog.
_TELNET_TIMEOUT_S = 20
_LOGIN_STEP_TIMEOUT_S = 12

# 1830/PSI prompt formats. They typically end with "#" but also "$" for
# certain user accounts. Allow either.
_PROMPT_RE = re.compile(r"([A-Za-z0-9._@\-]+[#>\$])\s*$")
_PASSWORD_RE = re.compile(r"(?i)password\s*:\s*$")
_ACK_RE = re.compile(
    r"(?i)\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|continue\?|accept\?|press\s+y"
)
# Confirmations that want the full word "yes" (not a bare "y"). The PSI's
# port-change gate is: "Continue? ... Enter 'yes' to confirm, 'no' to cancel:".
# Checked BEFORE _ACK_RE so a prompt containing both "Continue?" and
# "Enter 'yes'" is answered with "yes", not "y".
_CONFIRM_YES_RE = re.compile(
    r"(?i)(?:enter|type)\s+'?yes'?\s+to\s+confirm"
    r"|\[\s*yes\s*/\s*no\s*\]|\byes\s*/\s*no\b"
)
# Cap auto-confirmations per command so echoed prompt text can't loop forever.
_MAX_CONFIRMS = 5
# `config software upgrade status` header fields. Anchored to the labelled
# lines so we read the AGGREGATE operation state — NOT the per-stage
# "...100% complete" lines, which are present from the first poll and caused
# activate to fire before the load had transferred anything.
_OPERATION_RE = re.compile(r"(?im)^\s*Operation\s*:\s*(\S.*?)\s*$")
_OP_STATUS_RE = re.compile(r"(?im)^\s*Operation\s+Status\s*:\s*(\S.*?)\s*$")
_PERCENT_RE = re.compile(r"(?im)^\s*Percent\s+Completion\s*:\s*(\d+)\s*%")
_UPGRADE_PATH_RE = re.compile(r"(?im)^\s*Upgrade\s+Path\s+Available\s*:\s*(\S+)")
_WORKING_RELEASE_RE = re.compile(r"(?im)^\s*Working\s+Release\s*:\s*(\S+)")
# Explicit failure signals: a header status of Failed/Aborted/Error, or any
# download-script stage whose RESULT is a failure (not "Success"/"None").
_STAGE_FAIL_RE = re.compile(r"(?im)^\s*RESULT\s*:\s*(fail\w*|error\w*|abort\w*)")
# The shelf rejects `activate` if the load isn't truly complete.
_ACTIVATE_REJECT_RE = re.compile(
    r"(?i)unable to complete|operation in progress|not allowed|"
    r"rejected|cannot activate"
)


def _strip_telnet_noise(data: bytes) -> bytes:
    """Drop CR and embedded NUL bytes from a Telnet capture.

    The PSI's Telnet stream carries CR-NUL (RFC 854 bare-CR encoding) plus
    stray NULs around the prompt, and their placement varies per command.
    Left in, they break a plain ``endswith(prompt)`` / ``\\s*$`` match — e.g.
    ``b"PROMPT#\\x00"`` does not end with ``b"PROMPT#"`` and ``rstrip()`` does
    NOT strip NUL — which silently hangs a command until its prompt timeout.
    """
    return data.replace(b"\r", b"").replace(b"\x00", b"")


class _TelnetChannel:
    """paramiko-Channel-shaped facade over :class:`utils.telnet.Telnet`.

    The PSI upgrade phases (``_configure_server`` / status polling /
    ``_activate``) were written against a paramiko shell channel
    (``send``/``recv``/``recv_ready``/``closed``). PSI's usable CLI is only
    reachable over the Telnet getty two-step — SSH-as-admin dead-ends at the
    alarm banner — so we drive Telnet but keep that channel-shaped surface,
    letting the command phases run unchanged.
    """

    def __init__(self, telnet) -> None:
        self._t = telnet
        self._buf = bytearray()
        self._closed = False

    def _pull(self) -> None:
        """Move any immediately-available bytes into the local buffer."""
        if self._closed:
            return
        try:
            data = self._t.read_very_eager()
        except OSError:
            self._closed = True
            return
        if data:
            self._buf.extend(data)

    def _peer_closed(self) -> bool:
        """True once the peer has sent FIN/RST — the PSI reboot on activate.

        Uses ``MSG_PEEK`` so we inspect the socket without consuming real data
        or Telnet IAC bytes: select says readable + peek returns empty ==> the
        peer closed cleanly; an OSError ==> reset.
        """
        sock = getattr(self._t, "_sock", None)
        if sock is None:
            return self._closed
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # ── paramiko.Channel-compatible surface ─────────────────────────────────

    def send(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            self._t.write(data)
        except OSError:
            self._closed = True
            raise
        return len(data)

    def recv_ready(self) -> bool:
        if not self._buf:
            self._pull()
        return bool(self._buf)

    def recv(self, nbytes: int = 65535) -> bytes:
        if not self._buf:
            self._pull()
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:nbytes])
        del self._buf[:nbytes]
        return chunk

    def exit_status_ready(self) -> bool:
        if not self._closed and not self._buf and self._peer_closed():
            self._closed = True
        return self._closed

    @property
    def closed(self) -> bool:
        return self.exit_status_ready()

    def settimeout(self, _timeout) -> None:
        # Telnet applies per-read timeouts internally; nothing to set here.
        pass

    def close(self) -> None:
        self._closed = True
        try:
            self._t.close()
        except Exception:
            pass


class NokiaPSIUpgradeScript:
    """Drive a Nokia PSI software upgrade end to end over SSH."""

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
        # PSI / 1830 convention: inner password defaults to the same value
        # as the outer SSH password unless the operator overrides it.
        self.inner_password = inner_password if inner_password is not None else password
        self.software_filename = software_filename
        self.output_callback = output_callback or (lambda _msg: None)
        self.stop_callback = stop_callback or (lambda: False)
        self._prompt: str = ""
        self._working_release: str = ""

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self) -> bool:
        session = None
        try:
            session = self._connect()
            if session is None:
                return False

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

    # ── Phase 1 — connect + login (Telnet getty three-stage) ─────────────────

    def _connect(self) -> Optional["_TelnetChannel"]:
        """Open Telnet to the PSI and drive the getty three-stage login.

        PSI's usable CLI is reachable only over Telnet: over SSH it
        authenticates as ``admin`` but then dead-ends at the alarm banner
        (``admin`` is not the CLI account and the session ignores input). The
        dialog is ``login:`` -> ``cli``, ``Username:`` -> ``admin``,
        ``Password:`` -> ``admin`` (see ``reference_nokia_1830_class_access``).
        Returns a channel adapter ready for the command phases, or None.
        """
        self._log(
            f"Connecting to PSI at {self.ip_address} via Telnet as {self.username}…"
        )

        # Selecting a PSI upgrade against an operator-entered IP is an explicit
        # authorization for Telnet to THAT host (same rationale as the LAN
        # inventory path in gui4_0._build_manual_script_instance). Telnet stays
        # allowlist-gated (Layer B) even with skip_ssh_probe, so add it first.
        try:
            from utils.telnet_policy import add_telnet_allowlist
            add_telnet_allowlist(
                self.ip_address,
                f"auto: Nokia PSI upgrade (operator-selected) {self.ip_address}",
            )
        except Exception as exc:
            logger.warning("[TELNET] Could not auto-allowlist %s: %s",
                           self.ip_address, exc)

        try:
            from utils.telnet import Telnet
            # skip_ssh_probe: the PSI keeps :22 open but SSH-as-admin dead-ends
            # at the alarm banner, so waive the "prefer SSH" refusal (Layer C).
            # The allowlist gate (Layer B) still applies.
            telnet = Telnet(
                self.ip_address, timeout=_TELNET_TIMEOUT_S,
                skip_ssh_probe=True, purpose="nokia-psi-upgrade",
            )
        except Exception as exc:
            self._log(
                f"Telnet connection to {self.ip_address} failed ({exc}). The PSI "
                "CLI is only reachable over Telnet for the upgrade flow."
            )
            return None

        try:
            telnet.read_until(b"login: ", timeout=_LOGIN_STEP_TIMEOUT_S)
            self._log(f"getty login: — sending '{self.username}'")
            telnet.write(self.username.encode("ascii", "replace") + b"\n")

            telnet.read_until(b"Username: ", timeout=_LOGIN_STEP_TIMEOUT_S)
            self._log(f"Sending inner username: {self.inner_username}")
            telnet.write(self.inner_username.encode("ascii", "replace") + b"\n")

            telnet.read_until(b"Password: ", timeout=_LOGIN_STEP_TIMEOUT_S)
            self._log("Sending inner password")
            telnet.write(self.inner_password.encode("ascii", "replace") + b"\n")
        except Exception as exc:
            self._log(f"Telnet login dialog failed: {exc}")
            try:
                telnet.close()
            except Exception:
                pass
            return None

        session = _TelnetChannel(telnet)
        if not self._settle_shell(session):
            self._log("PSI Telnet login did not reach a shell prompt.")
            session.close()
            return None
        self._log(f"Telnet login successful; prompt detected ({self._prompt}).")
        return session

    def _settle_shell(self, session) -> bool:
        """After the getty three-stage, read the alarm/MOTD banner, ack any
        post-login Y/n EULA banner, and latch the shell prompt into
        ``self._prompt``. Returns True once a prompt is seen.

        The Telnet getty CLI *does* respond to input (unlike the dead SSH
        channel), so a newline nudge is safe to elicit a fresh prompt."""
        post = self._drain(session, idle_seconds=1.5, max_wait=12.0)
        for _ in range(5):
            low = post.lower()
            if "login incorrect" in low or "invalid" in low:
                self._log(
                    "Telnet login rejected — check the shelf credentials "
                    "(default cli / admin / admin)."
                )
                return False
            m = _PROMPT_RE.search(post)
            if m:
                self._prompt = m.group(1)
                return True
            answer = self._confirmation_answer(post)
            if answer is not None:
                self._log(f"Acknowledging post-login banner (sending {answer!r})")
                session.send(answer + "\n")
                post = self._drain(session, idle_seconds=1.0, max_wait=8.0)
                continue
            session.send("\n")
            post = self._drain(session, idle_seconds=1.2, max_wait=8.0)
        return False

    @staticmethod
    def _confirmation_answer(text: str) -> Optional[str]:
        """Return the reply for an interactive confirmation gate in *text*, or
        None if there isn't one. PSI config commands can gate on ``Enter 'yes'
        to confirm`` (wants the full word ``yes``); others use a bare
        ``(y/n)``. Check the yes-form first so a combined
        ``Continue? … Enter 'yes'`` prompt isn't answered with just ``y``."""
        if _CONFIRM_YES_RE.search(text):
            return "yes"
        if _ACK_RE.search(text):
            return "y"
        return None

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
                    tail = _strip_telnet_noise(bytes(buf[-200:])).decode(
                        "utf-8", errors="replace"
                    )
                    if not sent_password and _PASSWORD_RE.search(tail):
                        self._log(f"  (sending FTP password)")
                        session.send(f"{_FTP_PASSWORD}\n")
                        sent_password = True
                        buf = bytearray()
                        deadline = time.time() + _PROMPT_TIMEOUT_S
                        continue
                else:
                    stripped = _strip_telnet_noise(bytes(buf)).rstrip()
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

        for cmd in (
            "config software server root /CC/",
            "config software server protocol HTTP",
            "config software server port 8000",
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
            f"{_STATUS_POLL_INTERVAL_S:.0f}s — waiting for the load to transfer "
            "(Operation Status: Completed, 100%). This can take several minutes."
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
            # A status dump never needs a confirmation, and it's large — don't
            # let auto-confirm fire on any incidental text.
            out = self._send_capture(
                session, "config software upgrade status", timeout=60,
                auto_confirm=False,
            )
            if out is None:
                self._log("Status command returned no output — bailing.")
                return False

            info = self._parse_status(out)
            if info["working_release"]:
                self._working_release = info["working_release"]
            self._log(
                f"  [load] Operation={info['operation']} "
                f"Status={info['status']} Percent={info['percent']}% "
                f"UpgradePath={info['path']}"
            )

            # Failure: explicit header status or a failed download stage.
            status_l = (info["status"] or "").lower()
            if any(k in status_l for k in ("fail", "abort", "error")) \
                    or _STAGE_FAIL_RE.search(out):
                self._log(f"Load FAILED — Operation Status: {info['status']!r}.")
                self._echo_relevant(out, "config software upgrade status")
                return False

            # Completion gate (per operator): the aggregate Load operation must
            # report Completed at 100% with an available upgrade path. The
            # per-stage "100% complete" lines are NOT sufficient.
            if self._load_complete(info):
                self._log(
                    "Load complete — Operation Status: Completed, "
                    "Percent Completion: 100%, Upgrade Path Available: True."
                )
                return True

            time.sleep(_STATUS_POLL_INTERVAL_S)

    @staticmethod
    def _load_complete(info: dict) -> bool:
        """The aggregate Load operation is done and safe to activate."""
        return (
            (info.get("status") or "").lower() == "completed"
            and info.get("percent") == 100
            and (info.get("path") or "").lower() == "true"
        )

    @staticmethod
    def _parse_status(out: str) -> dict:
        """Extract the header fields of a `config software upgrade status`
        dump. Returns operation/status/path as trimmed strings (or None) and
        percent as an int (or None)."""
        def field(rx):
            m = rx.search(out)
            return m.group(1).strip() if m else None

        pct_m = _PERCENT_RE.search(out)
        return {
            "operation": field(_OPERATION_RE),
            "status": field(_OP_STATUS_RE),
            "percent": int(pct_m.group(1)) if pct_m else None,
            "path": field(_UPGRADE_PATH_RE),
            "working_release": field(_WORKING_RELEASE_RE),
        }

    # ── Phase 4 — activate ──────────────────────────────────────────────────

    def _activate(self, session) -> bool:
        cmd = "config software upgrade manual activate yes"
        self._log(f">> {cmd}  (device will reboot — the connection will drop)")
        start = time.monotonic()
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            self._log(f"send error on activate: {exc}")
            return False

        captured = ""
        notified = False
        deadline = time.time() + _ACTIVATE_DROP_TIMEOUT_S
        while time.time() < deadline:
            if self.stop_callback():
                self._log("Stop requested during activate.")
                return False
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if chunk:
                        text = _strip_telnet_noise(chunk).decode(
                            "utf-8", errors="replace"
                        )
                        captured += text
                        for line in text.splitlines():
                            if line.strip():
                                self._log(f"  {line.rstrip()}")
                        # If the shelf refuses the activate (e.g. the load
                        # wasn't really complete), it prints an error and stays
                        # up — don't sit here waiting for a reboot that isn't
                        # coming.
                        if _ACTIVATE_REJECT_RE.search(captured):
                            self._log(
                                "Activate was REJECTED by the shelf — it is not "
                                "rebooting. The load may not be fully complete."
                            )
                            return False
                # Accepted: no rejection within the grace window, so the shelf
                # is activating. Notify once, before the session drops on
                # reboot. The grace lets a fast reject (~0.5s in the field) be
                # caught first so we don't announce success then fail.
                if not notified and time.monotonic() - start >= _ACTIVATE_GRACE_S:
                    self._notify_activation_in_progress()
                    notified = True
                if session.closed or session.exit_status_ready():
                    elapsed = time.monotonic() - start
                    if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                        if not notified:
                            self._notify_activation_in_progress()
                        self._log(
                            f"Connection closed after {elapsed:.0f}s — PSI is "
                            "rebooting into the new load."
                        )
                        return True
                    self._log(
                        f"Connection closed only {elapsed:.0f}s in — too early "
                        "to credit as a reboot. Failing."
                    )
                    return False
                else:
                    time.sleep(0.5)
            except Exception as exc:
                elapsed = time.monotonic() - start
                if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                    if not notified:
                        self._notify_activation_in_progress()
                    self._log(
                        f"Connection error after {elapsed:.0f}s ({exc}); "
                        "treating as expected reboot."
                    )
                    return True
                self._log(f"Connection error too early: {exc}")
                return False

        self._log("Activate did not result in a session drop within timeout.")
        return False

    def _notify_activation_in_progress(self) -> None:
        """Log that activation was accepted, before the reboot drops the
        session. This is the log record; the operator-facing popup (with the
        manual-commit / safe-to-disconnect wording) is raised by the GUI after
        the NIC is restored to DHCP."""
        rel = self._working_release or self.software_filename or "the new release"
        self._log(
            f"Activation accepted — the PSI is rebooting into {rel}. "
            "(A manual commit will be required once it is back.)"
        )

    # ── Send helpers ────────────────────────────────────────────────────────

    def _send(self, session, cmd: str, timeout: float = _PROMPT_TIMEOUT_S,
              auto_confirm: bool = True) -> bool:
        """Send a command, wait for prompt, log output. Returns False on
        timeout or send error."""
        self._log(f">> {cmd}")
        out = self._send_capture(session, cmd, timeout=timeout,
                                 auto_confirm=auto_confirm)
        if out is None:
            return False
        self._echo_relevant(out, cmd)
        return True

    def _send_capture(
        self, session, cmd: str, timeout: float, auto_confirm: bool = True
    ) -> Optional[str]:
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            logger.debug("send failed: %s", exc)
            return None
        return self._read_until_prompt(
            session, timeout=timeout, auto_confirm=auto_confirm
        )

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
        return _strip_telnet_noise(bytes(buf)).decode("utf-8", errors="replace")

    def _read_until_prompt(
        self, session, timeout: float, auto_confirm: bool = True
    ) -> Optional[str]:
        deadline = time.time() + timeout
        buf = bytearray()
        prompt_b = self._prompt.encode("utf-8")
        confirmations = 0
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
                    stripped = _strip_telnet_noise(bytes(buf)).rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.3)
                        if not session.recv_ready():
                            return buf.decode("utf-8", errors="replace")
                        continue
                    # The device is idle without the base prompt — it may be
                    # waiting on an interactive confirmation (e.g. the
                    # port-change "Enter 'yes' to confirm" gate). Answer it so
                    # the command completes instead of timing out.
                    if auto_confirm and confirmations < _MAX_CONFIRMS:
                        answer = self._confirmation_answer(
                            _strip_telnet_noise(bytes(buf[-400:])).decode(
                                "utf-8", errors="replace"
                            )
                        )
                        if answer is not None:
                            self._log(f"  (auto-confirming: sending {answer!r})")
                            session.send(answer + "\n")
                            confirmations += 1
                            buf = bytearray()
                            deadline = time.time() + timeout
                            continue
                    if session.closed or session.exit_status_ready():
                        return None
                    time.sleep(0.15)
            except Exception as exc:
                logger.debug("recv error: %s", exc)
                return None
        # Timed out. Surface what the device actually sent (repr keeps control
        # bytes visible) so a stuck command — a sub-prompt we didn't answer, an
        # error banner, dead silence — is diagnosable from the log.
        tail = repr(bytes(buf)[-300:]) if buf else "<nothing received>"
        self._log(
            f"Timed out after {timeout:.0f}s waiting for prompt "
            f"{self._prompt!r}. Last bytes: {tail}"
        )
        return None

    def _echo_relevant(self, out: str, cmd: str) -> None:
        # Drop CR/NUL noise so the command echo and trailing prompt line are
        # recognised (and the log isn't peppered with ^@).
        out = out.replace("\r", "").replace("\x00", "")
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
        logger.info("[PSI-Upgrade %s] %s", self.ip_address, msg)
