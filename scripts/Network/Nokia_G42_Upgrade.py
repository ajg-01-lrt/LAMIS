"""
scripts/Network/Nokia_G42_Upgrade.py — Software upgrade flow for Nokia G42.

The companion HTTP server (Software Upgrades GUI tab) stages the .manifest
+ payload files at http://169.254.0.101:8000/. This script SSHes to
169.254.0.1 (the G42 service interface) and drives the upgrade through
its phases:

  Phase 1 — Pre-upgrade prep
    change-ztp-mode disabled            (interactive confirm)
    clear recover-mode                  (interactive confirm)

  Phase 2 — Download
    set system security security-policies secure-mode false
    download swimage source=<server-url>     (long-running, watches HTTP server)

  Phase 3 — Validate
    set system security security-policies secure-mode true
    prepare-upgrade validate <manifest>
    show software-load
    NB: it is common for validation to stick on slot 1/3 with an XMM4 card
    seated. If that happens the operator has to physically unseat the
    card and re-run the validate. This script logs the stuck state but
    will not perform that physical step.

  Phase 4 — Apply
    prepare-upgrade -i apply <manifest>       (long-running)
    show software-load

  Phase 5 — Activate
    activate swimage                          (device reboots → SSH drops)

A dropped SSH session after `activate` is the success signal, identical
in spirit to the Ciena RLS warm-reset.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, List, Optional, Tuple

import paramiko

from utils.helpers import ensure_host_key_known, get_known_hosts_path, safe_load_host_keys

logger = logging.getLogger(__name__)

# Per-phase command timeouts (seconds). These are intentionally generous —
# the slow steps on a G42 are bound by disk + flash speed, not network.
_PROMPT_TIMEOUT_S = 30
_DOWNLOAD_TIMEOUT_S = 5 * 60   # full image pull from the local HTTP server
_VALIDATE_TIMEOUT_S = 10 * 60  # field observation: validate finished ~20s after the prior 5-min ceiling fired; doubled to leave comfortable headroom
_APPLY_TIMEOUT_S = 60 * 60  # includes time spent polling upgrade-status for apply completion
_ACTIVATE_DROP_TIMEOUT_S = 5 * 60 

# Treat post-activate session drop as success only after at least this
# many seconds, so a connectivity blip doesn't get misread as completion.
_MIN_ELAPSED_FOR_RESET_S = 30.0

_PROMPT_RE = re.compile(r"([A-Za-z0-9._\-]+[#>])\s*$")
# Matches the most common Nokia confirmation prompts: "(y/n)", "[y/n]",
# and "Press y to continue" variants.
_CONFIRM_RE = re.compile(
    r"\(y/n\)|\[y/n\]|press\s+['\"]?y['\"]?|y/n\s*:?\s*$",
    re.IGNORECASE,
)


class NokiaG42UpgradeScript:
    """Drive a Nokia G42 software upgrade end to end over SSH."""

    def __init__(
        self,
        *,
        ip_address: str,
        username: str,
        password: str,
        server_url: str,
        manifest_name: str,
        output_callback: Optional[Callable[[str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.ip_address = ip_address
        self.username = username
        self.password = password
        self.server_url = server_url
        self.manifest_name = manifest_name
        self.output_callback = output_callback or (lambda _msg: None)
        self.stop_callback = stop_callback or (lambda: False)
        self._prompt: str = ""
        # End-of-run signal: True when the device's ``activate
        # swimage`` step reported the standby controller card was
        # NOT synchronized at activation time. The GUI worker reads
        # this after ``run()`` returns to pick which finish-up
        # dialog to show (standby-not-synced => operator needs to
        # repeat the upgrade on the second XMM4 before the chassis
        # is fully cut over).
        self.standby_sync_warning: bool = False

    # ── Public entry ────────────────────────────────────────────────────────

    def run(self) -> bool:
        self._log(f"Connecting to G42 at {self.ip_address} as {self.username}…")
        if not ensure_host_key_known(self.ip_address, port=22):
            self._log(f"Host key verification failed for {self.ip_address}.")
            return False

        client = paramiko.SSHClient()
        safe_load_host_keys(client, str(get_known_hosts_path()))
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

        session = None
        try:
            session = client.invoke_shell(width=200, height=10000)
            session.settimeout(30)

            if not self._detect_prompt(session):
                self._log("Could not detect G42 shell prompt.")
                return False
            self._log(f"Logged in; prompt detected ({self._prompt}).")

            phases: List[Tuple[str, Callable[[paramiko.Channel], bool]]] = [
                ("pre-upgrade prep", self._phase_pre_upgrade),
                ("download swimage", self._phase_download),
                ("validate manifest", self._phase_validate),
                ("apply manifest", self._phase_apply),
                ("activate swimage", self._phase_activate),
            ]
            for name, fn in phases:
                if self.stop_callback():
                    self._log(f"Stop requested before phase '{name}'.")
                    return False
                self._log(f"── Phase: {name} ──")
                if not fn(session):
                    self._log(f"Phase '{name}' did not complete cleanly.")
                    # _phase_activate signals success via session drop, so
                    # treat its "failure" as success when the drop is the
                    # cause. That branch handles its own return value.
                    if name != "activate swimage":
                        return False
                    return False
            return True
        finally:
            try:
                if session:
                    session.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    # ── Phase implementations ───────────────────────────────────────────────

    def _phase_pre_upgrade(self, session) -> bool:
        # _send_confirm now returns Optional[str] -- None on failure,
        # captured output on success. Treat any non-None as ok here;
        # the pre-upgrade commands don't have a verification signature
        # to inspect.
        if self._send_confirm(session, "change-ztp-mode disabled") is None:
            return False
        return (
            self._send_confirm(session, "clear recover-mode") is not None
        )

    def _phase_download(self, session) -> bool:
        if not self._send_simple(
            session,
            "set system security security-policies secure-mode false",
            timeout=_PROMPT_TIMEOUT_S,
        ):
            return False
        cmd = f"download swimage source={self.server_url}"
        self._log(f">> {cmd}  (this may take many minutes)")
        out = self._send_raw(session, cmd, timeout=_DOWNLOAD_TIMEOUT_S)
        if out is None:
            self._log("Download did not return to prompt in time.")
            return False
        self._echo_relevant(out, cmd)
        return True

    def _phase_validate(self, session) -> bool:
        if not self._send_simple(
            session,
            "set system security security-policies secure-mode true",
            timeout=_PROMPT_TIMEOUT_S,
        ):
            return False
        # ``prepare-upgrade validate`` may emit a y/n prompt before
        # running (e.g. "Upgrading to BASIC release. This may result
        # in loss of config ... Do you still want to continue? [y/n]")
        # when the target manifest crosses release families. Route
        # through ``_send_confirm`` so we auto-answer 'y'; the
        # previous ``_send_raw`` path just hung at the y/n.
        cmd = f"prepare-upgrade validate {self.manifest_name}"
        out = self._send_confirm(session, cmd, timeout=_VALIDATE_TIMEOUT_S)
        if out is None:
            self._log("Validate did not return to prompt in time.")
            return False
        # The device prints "Prepare-upgrade validate initiated.
        # Please use upgrade-status command for status" after
        # accepting the command -- absence usually means the device
        # rejected the request (manifest mismatch, etc.) and the
        # script should bail rather than poll for a status that
        # won't arrive.
        if "validate initiated" not in out.lower():
            self._log(
                "Device didn't report 'Prepare-upgrade validate initiated' "
                "-- aborting before we waste the poll budget."
            )
            return False
        # Poll ``upgrade-status`` until every ``validate-in-progress``
        # row has transitioned to ``validate-complete`` -- partial
        # success on the aggregate row alone would let us race ahead
        # of a per-card validation still in flight.
        return self._poll_upgrade_status(
            session,
            in_progress_token="validate-in-progress",
            complete_token="validate-complete",
            failed_token="validate-failed",
            timeout=_VALIDATE_TIMEOUT_S,
        )

    def _phase_apply(self, session) -> bool:
        # Same y/n-on-cross-family case as ``_phase_validate``. Use
        # the confirm helper so we don't hang on the prompt.
        cmd = f"prepare-upgrade -i apply {self.manifest_name}"
        self._log("   (this may take a while)")
        out = self._send_confirm(session, cmd, timeout=_APPLY_TIMEOUT_S)
        if out is None:
            self._log("Apply did not return to prompt in time.")
            return False
        if "apply initiated" not in out.lower():
            self._log(
                "Device didn't report 'Prepare-upgrade apply initiated' "
                "-- aborting before we waste the poll budget."
            )
            return False
        # Wait for every ``apply-in-progress`` row to reach
        # ``apply-complete``. This is the gate that prevents
        # ``activate swimage`` from running while card 1/3 (or any
        # other card) is still applying -- which produces the
        # ``ERROR: precondition failed - Standby is not prepared to
        # process activate`` failure mode.
        return self._poll_upgrade_status(
            session,
            in_progress_token="apply-in-progress",
            complete_token="apply-complete",
            failed_token="apply-failed",
            timeout=_APPLY_TIMEOUT_S,
        )

    def _phase_activate(self, session) -> bool:
        """Send 'activate swimage', auto-answer the y/n prompts the
        device emits, and treat the device's ``Activation is in
        progress !`` line (or a subsequent SSH drop, as a fallback)
        as the success signal.

        Two y/n prompts can appear here:

        1. ``Are you sure? [y/n]`` -- always emitted, gates activation
           on operator intent.
        2. ``Standby controller card in NC is not ready synchronized,
           Single controller card will upgrade. Do you want to
           continue? [y/n]`` -- emitted only when the redundant XMM4
           hasn't synced its state with the primary at activation
           time. The upgrade still proceeds on the primary card,
           but the chassis is operating on a single controller
           until the operator runs the same upgrade against the
           second XMM4. When this case fires we set
           ``self.standby_sync_warning = True`` so the GUI can show
           a different finish-up dialog ("Preform Upgrade Again on
           Second XMM4").
        """
        cmd = "activate swimage"
        self._log(f">> {cmd}  (device will reboot — SSH will drop)")
        start = time.monotonic()
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            self._log(f"send error on activate: {exc}")
            return False

        # Signature substrings (case-insensitive) we look for in the
        # captured chunk between the first ``y`` and the second prompt.
        # Any of these locks in ``standby_sync_warning = True``. The
        # device's exact wording varies slightly across firmware
        # revisions, so we accept any of the three landmarks.
        _STANDBY_HINTS = (
            "standby controller card",
            "not ready synchronized",
            "single controller card will upgrade",
        )

        # The device prints this line the moment it commits to the
        # reboot, several seconds before the SSH session actually
        # drops. Triggering completion off the announcement instead
        # of the SSH drop gets the popup in front of the operator
        # sooner and removes the dependency on the drop arriving at
        # all (some firmware revisions hold the session open longer
        # than _ACTIVATE_DROP_TIMEOUT_S allows).
        _ACTIVATION_DONE_HINT = "activation is in progress"

        deadline = time.time() + _ACTIVATE_DROP_TIMEOUT_S
        buf = bytearray()
        confirmations_sent = 0
        # Track the byte offset we've already scanned for confirm
        # patterns so we don't repeatedly fire on the same y/n
        # already answered.
        scanned_offset = 0
        while time.time() < deadline:
            if self.stop_callback():
                self._log("Stop requested during activate.")
                return False
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if chunk:
                        buf.extend(chunk)
                        # echo any pre-reboot output (commit messages, prompts, etc.)
                        for line in chunk.decode("utf-8", errors="replace").splitlines():
                            if line.strip():
                                self._log(f"  {line.rstrip()}")
                        # Check the newly-arrived tail for a y/n
                        # prompt we haven't answered yet. Look at the
                        # whole buf tail (not just the new chunk)
                        # because the prompt can land split across
                        # recv() boundaries.
                        tail = bytes(buf[scanned_offset:]).decode(
                            "utf-8", errors="replace",
                        )
                        if _CONFIRM_RE.search(tail[-300:]):
                            # Detect the standby-sync warning by
                            # searching the SAME tail (i.e. the
                            # bytes between the previous y/n and this
                            # one). The device emits the warning
                            # line immediately before its y/n.
                            tail_lower = tail.lower()
                            if any(h in tail_lower for h in _STANDBY_HINTS):
                                self.standby_sync_warning = True
                                self._log(
                                    "⚠ Standby XMM4 controller card was "
                                    "NOT synced at activation time -- "
                                    "the second card needs the same "
                                    "upgrade after this one settles."
                                )
                            try:
                                session.send("y\n")
                            except Exception as exc:
                                self._log(f"send 'y' failed: {exc}")
                                return False
                            confirmations_sent += 1
                            # Advance scanned_offset to the current
                            # end of buf so the next y/n detection
                            # doesn't re-trigger on the already-
                            # answered prompt.
                            scanned_offset = len(buf)
                        # Scan the full buffer (not just the post-y/n
                        # tail) for the activation announcement. We
                        # decode the whole buf each time rather than
                        # tracking another offset because the line
                        # appears once and ends the phase -- there's
                        # no per-chunk repeat-trigger risk.
                        if _ACTIVATION_DONE_HINT in bytes(buf).decode(
                            "utf-8", errors="replace",
                        ).lower():
                            elapsed = time.monotonic() - start
                            self._log(
                                f"✔ 'Activation is in progress' seen after "
                                f"{elapsed:.0f}s -- upgrade is committed, "
                                "device is now rebooting. Treating "
                                "upgrade as complete."
                            )
                            return True
                elif session.closed or session.exit_status_ready():
                    elapsed = time.monotonic() - start
                    if elapsed >= _MIN_ELAPSED_FOR_RESET_S:
                        self._log(
                            f"SSH session closed after {elapsed:.0f}s — "
                            "device is rebooting into the new load. "
                            "Treating upgrade as complete."
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
                # paramiko raises on closed channel — same success signal
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

    # ── upgrade-status polling ──────────────────────────────────────────────

    def _poll_upgrade_status(
        self,
        session,
        in_progress_token: str,
        complete_token: str,
        failed_token: str,
        timeout: float,
        poll_interval: float = 5.0,
    ) -> bool:
        """Poll ``upgrade-status`` until *every* row that was
        ``<phase>-in-progress`` has transitioned to
        ``<phase>-complete``.

        The G42 emits one status row per software-load target -- the
        aggregate row plus one per card -- e.g.::

            software-load-installable      ...  apply-complete
            software-load-1-1/installable  ...  apply-complete
            software-load-1-3/installable  ...  apply-in-progress

        Returning success the moment ``apply-complete`` first appears
        would let the activate phase race ahead of a card that's
        still applying (which is exactly how we ended up issuing
        ``activate swimage`` against a half-applied chassis and
        getting back ``ERROR: precondition failed - Standby is not
        prepared to process activate.``). So the success condition is
        the absence of any ``-in-progress`` token *and* the presence
        of at least one ``-complete`` row.

        The ``saw_in_progress`` flag guards against the first poll
        firing before the device has transitioned rows out of the
        prior phase's state -- without it, a quick re-poll after a
        previous apply could wrongly report success against
        leftover ``apply-complete`` rows from the last run.
        """
        deadline = time.time() + timeout
        saw_in_progress = False
        attempt = 0
        while time.time() < deadline:
            if self.stop_callback():
                self._log("Stop requested while polling upgrade-status.")
                return False

            attempt += 1
            out = self._send_raw(
                session, "upgrade-status", timeout=_PROMPT_TIMEOUT_S,
            )
            if out is None:
                self._log(
                    "upgrade-status did not return -- SSH session "
                    "may have dropped."
                )
                return False

            self._warn_if_slot_stuck(out)

            if failed_token in out:
                self._log(f"✘ Detected: {failed_token!r}. Aborting.")
                self._echo_relevant(out, "upgrade-status")
                return False

            in_progress_count = out.count(in_progress_token)
            complete_count = out.count(complete_token)

            if in_progress_count > 0:
                saw_in_progress = True
                if attempt == 1 or attempt % 5 == 0:
                    self._log(
                        f"…{in_progress_count} row(s) still "
                        f"{in_progress_token!r} (poll #{attempt})"
                    )
            elif complete_count > 0 and saw_in_progress:
                # Every row that was in-progress has reported complete.
                self._log(
                    f"✔ All {complete_count} row(s) report "
                    f"{complete_token!r}"
                )
                self._echo_relevant(out, "upgrade-status")
                return True
            elif complete_count > 0 and attempt >= 3:
                # We never saw in-progress, but after ~15s of polling
                # the rows all show complete -- the phase must have
                # finished between command submit and our first poll.
                # (Three polls is enough to rule out the "stale state
                # from the prior phase" race -- the device transitions
                # rows within a couple of seconds of accepting the
                # prepare-upgrade command.)
                self._log(
                    f"✔ All {complete_count} row(s) report "
                    f"{complete_token!r} (no in-progress observed -- "
                    "phase likely completed before first poll)"
                )
                self._echo_relevant(out, "upgrade-status")
                return True

            time.sleep(poll_interval)

        self._log(
            f"Timed out waiting for every {in_progress_token!r} row "
            f"to reach {complete_token!r}."
        )
        return False

    # ── Send helpers ────────────────────────────────────────────────────────

    def _send_simple(self, session, cmd: str, timeout: float) -> bool:
        """Send a command, wait for prompt, echo output."""
        self._log(f">> {cmd}")
        out = self._send_raw(session, cmd, timeout=timeout)
        if out is None:
            return False
        self._echo_relevant(out, cmd)
        return True

    def _send_confirm(
        self, session, cmd: str, timeout: Optional[float] = None,
    ) -> Optional[str]:
        """Send a command that may prompt for y/n confirmation; auto-
        answer 'y' if it does. Returns the captured output on
        success, or ``None`` on failure (timeout / stop / SSH error).

        *timeout* applies both to "time spent waiting for the prompt
        or confirmation" AND, after we answer 'y', to "time spent
        waiting for the device to return to the regular prompt". The
        post-confirm clock resets so a long-running command (e.g.
        ``prepare-upgrade -i apply`` -> 1 hour) gets its full window
        regardless of how quickly the y/n appeared.

        Returning the captured output (rather than a bare bool) lets
        callers inspect the device's reply for phase-completion
        signatures -- the validate / apply phases use this to verify
        the device printed ``Prepare-upgrade <phase> initiated.``
        before they kick off the ``upgrade-status`` poll.
        """
        if timeout is None:
            timeout = _PROMPT_TIMEOUT_S
        self._log(f">> {cmd}  (will answer 'y' to confirmation)")
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            self._log(f"send error: {exc}")
            return None

        # Read until we see either a confirmation prompt or the regular prompt.
        deadline = time.time() + timeout
        buf = bytearray()
        post_confirm_buf = bytearray()
        confirmed = False
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
                    if confirmed:
                        post_confirm_buf.extend(chunk)
                    decoded = bytes(buf).decode("utf-8", errors="replace")
                    if not confirmed and _CONFIRM_RE.search(decoded[-200:]):
                        session.send("y\n")
                        confirmed = True
                        # Reset the deadline with the FULL timeout so
                        # the post-confirm work (long upgrade phases)
                        # gets its full window, not just whatever was
                        # left after the y/n round-trip. Keep the
                        # pre-confirm buf so the caller's logging
                        # still sees the warning line that prompted
                        # the y/n.
                        deadline = time.time() + timeout
                        continue
                else:
                    stripped = bytes(buf).replace(b"\r", b"").rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.3)
                        if not session.recv_ready():
                            decoded = bytes(buf).decode(
                                "utf-8", errors="replace",
                            )
                            self._echo_relevant(decoded, cmd)
                            return decoded
                    time.sleep(0.1)
            except Exception as exc:
                self._log(f"recv error: {exc}")
                return None
        self._log(f"Timed out waiting for confirmation/prompt after '{cmd}'")
        return None

    def _send_raw(
        self, session, cmd: str, timeout: float
    ) -> Optional[str]:
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            logger.debug("send failed: %s", exc)
            return None
        return self._read_until_prompt(session, timeout=timeout)

    # ── Prompt detection / output reading ───────────────────────────────────

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
        deadline = time.time() + timeout
        buf = bytearray()
        prompt_b = self._prompt.encode("utf-8")
        last_progress_log = time.time()
        while time.time() < deadline:
            if self.stop_callback():
                return None
            try:
                if session.recv_ready():
                    chunk = session.recv(65535)
                    if not chunk:
                        return None
                    buf.extend(chunk)
                    # Long-running commands print intermediate output —
                    # surface it to the log every couple of seconds so the
                    # operator can see progress without spamming.
                    if time.time() - last_progress_log >= 2.0:
                        tail = bytes(buf[-512:]).decode("utf-8", errors="replace")
                        for line in tail.splitlines()[-3:]:
                            line = line.rstrip()
                            if line and not line.endswith(self._prompt):
                                self._log(f"  {line}")
                        last_progress_log = time.time()
                else:
                    stripped = bytes(buf).replace(b"\r", b"").rstrip()
                    if stripped.endswith(prompt_b) and b"\n" in stripped:
                        time.sleep(0.4)
                        if not session.recv_ready():
                            return buf.decode("utf-8", errors="replace")
                        continue
                    if session.closed or session.exit_status_ready():
                        return None
                    time.sleep(0.2)
            except Exception as exc:
                logger.debug("recv error: %s", exc)
                return None
        return None

    # ── Output post-processing ──────────────────────────────────────────────

    def _echo_relevant(self, out: str, cmd: str) -> None:
        """Strip the command echo + trailing prompt, log what's left."""
        lines = out.splitlines()
        if lines and cmd in lines[0]:
            lines = lines[1:]
        if lines and re.match(r"^[A-Za-z0-9._\-]+[#>]\s*$", lines[-1].strip()):
            lines = lines[:-1]
        for line in lines:
            line = line.rstrip()
            if line:
                self._log(f"  {line}")

    @staticmethod
    def _warn_if_slot_stuck(load_out: str) -> None:
        """If `upgrade-status` shows slot 1/1 or 1/3 in a non-completed state
        while others have moved on, log the known-issue hint. The exact
        column shape varies by firmware revision so we keep the match loose."""
        lower = load_out.lower()
        
        # Check for either problematic slot
        if ("1/1" in lower or "1/3" in lower) and ("stuck" in lower or "failed" in lower):
            if "1/1" in lower:
                logger.warning(
                    "G42 validate appears stuck on slot 1/1 — physical unseat "
                    "of the card in 1/1 may be required."
                )
            else:  
                logger.warning(
                    "G42 validate appears stuck on slot 1/3 — physical unseat "
                    "of XMM4 may be required."
                )

    def _log(self, msg: str) -> None:
        try:
            self.output_callback(msg)
        except Exception:
            pass
        logger.info("[%s] %s", self.ip_address, msg)
