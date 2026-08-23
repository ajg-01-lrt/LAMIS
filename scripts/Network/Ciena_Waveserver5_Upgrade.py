"""scripts/Network/Ciena_Waveserver5_Upgrade.py — Waveserver 5 upgrade flow.

Unique among the upgrade scripts: the Waveserver 5 ships with no usable
management IP, so we can't reach it over LAN until WE provision one. The
flow is two-phase:

**Phase 1 — Serial console** (operator plugs into the console port at
115200 baud and DCN-1 over Cat-5 to the laptop):

  1. Wake the prompt, login as ``su`` (no password).
  2. Send the provisioning batch one command at a time —
     ``system set host-name WS5_1``, ``dhcp client disable``,
     ``interface set interface local ip 10.9.49.36/22``,
     ``interface set gateway 10.9.49.101``, ``ntp client disable``,
     ``system set date <today UTC>``, ``system set time <now UTC>
     timestamp UTC``, ``configuration save``.
  3. After the save the device responds on the management IP we just
     configured.

**Phase 2 — SSH over the DCN-1 port** (laptop is on the same /22 as the
shelf, serving the upgrade tarball over HTTP on :8000):

  1. SSH as ``su`` (no password).
  2. ``software download url http://10.9.49.101:8000/<file> password admin``.
  3. Poll ``software show upgrade-status`` until ``Download Complete``.
  4. Disable grpc/https/netconf/sftp/scp, ``configuration save``.
  5. ``user create user diag password diagdiag access-level diag``,
     ``configuration save``.
  6. ``software activate version <version>`` (version derived by
     stripping the ``.tar.gz`` from the chosen filename).
  7. Final ``software show upgrade-status`` — when the state reads
     ``Activation In Progress`` we're done. Manual commit on the device
     itself is intentionally left to the operator.

The script intentionally does not depend on the inventory DB / BaseScript
machinery — same one-shot single-IP shape as ``Ciena_RLS_Upgrade``.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import paramiko

from utils.helpers import ensure_host_key_known, get_known_hosts_path, safe_load_host_keys
from utils.serial_helpers import (
    capture_until_prompt,
    open_serial_with_baud_probe,
    serial_login,
)

logger = logging.getLogger(__name__)

# Serial defaults — Waveserver 5 console runs at 115200 8N1 (field-
# confirmed on production WS5 hardware; an earlier comment in this
# file claimed 9600 per vendor docs, but the device times out at
# that baud and only locks onto a prompt at 115200). No auto-probing
# fallback is needed.
_SERIAL_BAUD = 115200
_SERIAL_PROMPT_TIMEOUT = 10.0
_SERIAL_CMD_TIMEOUT = 15.0
# `configuration save` writes the running-config and flushes the change
# pending marker (the trailing ``*`` on the prompt). Saves take a beat
# longer than ordinary commands on Waveserver — give them headroom.
_SERIAL_SAVE_TIMEOUT = 30.0

# Provisioning credential ladder used for the serial login. ``su`` with
# no password is the factory default; the empty-password fallback covers
# field cases where the operator has already set one but it didn't take.
_SERIAL_DEFAULTS = [("su", ""), ("su", "su"), ("su", "admin")]

# Software download polling — the tarball is hundreds of MB and the link
# can be slow, so the cap is generous. The state machine breaks out the
# moment the device reports Download Complete; the timeout only fires on
# stuck or aborted downloads.
_DOWNLOAD_POLL_INTERVAL_S = 15.0
_DOWNLOAD_TIMEOUT_S = 45 * 60

# Activation only needs to be observed long enough to confirm it started
# — the spec says we stop at "Activation In Progress" and leave the
# operator to commit after the reboot finishes.
_ACTIVATE_POLL_INTERVAL_S = 5.0
_ACTIVATE_TIMEOUT_S = 5 * 60

# Waveserver SSH prompt: ``<hostname>#``. The Waveserver pads the prompt
# with whitespace before each ``#`` in some firmware revs; accept both.
_SSH_PROMPT_RE = re.compile(r"([A-Za-z0-9._\-]+)\*?#\s*$")


class Waveserver5UpgradeScript:
    """Run the full Waveserver 5 provisioning + software upgrade.

    Parameters
    ----------
    serial_port
        OS-level identifier for the serial console (``COM3`` on Windows,
        ``/dev/ttyUSB0`` on Linux).
    software_filename
        Name of the upgrade artifact in the operator-selected folder —
        e.g. ``waveserver-2.4.52.21-GA.tar.gz``. Used to build both the
        HTTP download URL and the ``software activate version <X>``
        command (which takes the filename minus ``.tar.gz``).
    server_url
        Full HTTP URL the Waveserver will pull the artifact from —
        e.g. ``http://10.9.49.101:8000/waveserver-2.4.52.21-GA.tar.gz``.
        Caller is responsible for ensuring the HTTP server is up.
    device_ip
        Management IP the Waveserver will reach via DCN-1 after phase 1
        completes. Default ``10.9.49.36`` per the operator-supplied spec.
    device_ip_cidr
        Address + prefix written via ``interface set interface local ip``.
        Default ``10.9.49.36/22``.
    gateway_ip
        Gateway written via ``interface set gateway``. The laptop acts
        as the gateway in this setup. Default ``10.9.49.101``.
    hostname
        Hostname written via ``system set host-name`` and used when
        reasoning about the SSH prompt. Default ``WS5_1`` — operator
        spec is one shelf at a time so a fixed name is fine.
    ssh_user, ssh_pass
        SSH credentials. Factory default is ``su`` with no password.
    download_password
        The literal password the Waveserver wants attached to the
        ``software download url ... password <X>`` command. Vendor sets
        this to ``admin``; exposed as a parameter only so tests can
        override it.
    output_callback
        Receives human-readable progress lines (one per call). Wired to
        the GUI log panel.
    stop_callback
        Returns True when the user clicks Stop. Cancellation only stops
        local polling — once a download is in flight on the device it
        continues independently.
    """

    def __init__(
        self,
        *,
        serial_port: str,
        software_filename: str,
        server_url: str,
        device_ip: str = "10.9.49.36",
        device_ip_cidr: str = "10.9.49.36/22",
        gateway_ip: str = "10.9.49.101",
        hostname: str = "WS5_1",
        ssh_user: str = "su",
        ssh_pass: str = "",
        download_password: str = "admin",
        output_callback: Optional[Callable[[str], None]] = None,
        stop_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.serial_port = serial_port
        self.software_filename = software_filename
        self.server_url = server_url
        self.device_ip = device_ip
        self.device_ip_cidr = device_ip_cidr
        self.gateway_ip = gateway_ip
        self.hostname = hostname
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self.download_password = download_password
        self.output_callback = output_callback or (lambda _msg: None)
        self.stop_callback = stop_callback or (lambda: False)
        self._ssh_prompt: str = ""

    # ── Public entry point ──────────────────────────────────────────────────

    def run(self) -> bool:
        """Run both phases. Returns True iff activation was kicked off."""
        if not self._provision_via_serial():
            return False
        if self.stop_callback():
            self._log("Stop requested between phases — skipping SSH phase.")
            return False
        # Give the device a beat to settle on its new IP before SSH'ing in.
        # In field tests the prompt comes back fast after `configuration
        # save`, but the management interface needs a couple of seconds to
        # actually answer SYN packets on the new address.
        self._log("Waiting 5s for the management interface to come up…")
        time.sleep(5.0)
        return self._install_software_via_ssh()

    # ── Phase 1 — serial provisioning ───────────────────────────────────────

    def _provision_via_serial(self) -> bool:
        self._log(f"Opening serial console on {self.serial_port} @ {_SERIAL_BAUD} baud…")
        ser = open_serial_with_baud_probe(
            self.serial_port, [_SERIAL_BAUD],
            timeout=_SERIAL_PROMPT_TIMEOUT, should_stop=self.stop_callback,
        )
        if ser is None:
            self._log(
                f"No usable prompt on {self.serial_port}@{_SERIAL_BAUD}. "
                "Check the serial cable and verify the device is powered."
            )
            return False
        try:
            ok, used = serial_login(
                ser, _SERIAL_DEFAULTS,
                timeout=_SERIAL_PROMPT_TIMEOUT, should_stop=self.stop_callback,
            )
            if not ok:
                self._log("Serial login failed (no shell after credentials).")
                return False
            self._log(f"Serial login OK as {(used or ('?', '?'))[0]!r}.")

            now = datetime.now(tz=timezone.utc)
            commands = [
                f"system set host-name {self.hostname}",
                "dhcp client disable",
                f"interface set interface local ip {self.device_ip_cidr}",
                f"interface set gateway {self.gateway_ip}",
                "ntp client disable",
                f"system set date {now.strftime('%Y-%m-%d')}",
                f"system set time {now.strftime('%H:%M:%S')} timestamp UTC",
                "configuration save",
            ]
            for cmd in commands:
                if self.stop_callback():
                    self._log("Stop requested mid-provisioning.")
                    return False
                # `configuration save` lingers; everything else returns in
                # under a couple of seconds.
                timeout = _SERIAL_SAVE_TIMEOUT if cmd == "configuration save" else _SERIAL_CMD_TIMEOUT
                self._log(f"  serial >> {cmd}")
                out = capture_until_prompt(
                    ser, cmd, timeout=timeout, should_stop=self.stop_callback,
                )
                if out is None:
                    self._log(f"No prompt returned after {cmd!r}; aborting.")
                    return False
                # Surface anything non-empty in the device's reply so a
                # failed `set` doesn't get silently swallowed.
                trimmed = "\n".join(
                    ln.strip() for ln in out.splitlines()
                    if ln.strip() and ln.strip() != cmd
                )
                if trimmed and not trimmed.endswith("#"):
                    self._log(f"     {trimmed}")
            self._log("Phase 1 (serial provisioning) complete.")
            return True
        finally:
            try:
                ser.close()
            except Exception:
                pass

    # ── Phase 2 — SSH download + activate ───────────────────────────────────

    def _install_software_via_ssh(self) -> bool:
        self._log(f"Phase 2: connecting to {self.device_ip} over SSH as {self.ssh_user!r}…")
        kh_path = str(get_known_hosts_path())
        if not ensure_host_key_known(self.device_ip, port=22):
            self._log(f"Host key verification failed for {self.device_ip}.")
            return False

        client = paramiko.SSHClient()
        safe_load_host_keys(client, kh_path)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            try:
                client.connect(
                    self.device_ip, port=22,
                    username=self.ssh_user, password=self.ssh_pass,
                    timeout=20, banner_timeout=20, auth_timeout=20,
                    look_for_keys=False, allow_agent=False,
                )
            except paramiko.AuthenticationException:
                self._log("SSH authentication failed — check the su credential.")
                return False
            except Exception as exc:
                self._log(f"SSH connect error: {exc}")
                return False

            session = client.invoke_shell(width=200, height=10000)
            session.settimeout(30)
            if not self._detect_prompt(session):
                self._log("Could not detect Waveserver SSH prompt.")
                return False
            self._log(f"SSH prompt detected ({self._ssh_prompt}).")

            # ── Download ────────────────────────────────────────────────────
            dl_cmd = (
                f"software download url {self.server_url} "
                f"password {self.download_password}"
            )
            self._log(f"  ssh >> software download url {self.server_url} password ****")
            if self._send(session, dl_cmd, timeout=30) is None:
                self._log("Timed out submitting the download command.")
                return False
            if not self._wait_for_state(
                session, target_state="Download Complete",
                timeout=_DOWNLOAD_TIMEOUT_S,
                poll_interval=_DOWNLOAD_POLL_INTERVAL_S,
                label="download",
            ):
                return False

            # ── Service hardening + diag user ───────────────────────────────
            for cmd in [
                "system server grpc disable",
                "system server https disable",
                "system server netconf disable",
                "system server sftp disable",
                "system server scp disable",
                "configuration save",
                "user create user diag password diagdiag access-level diag",
                "configuration save",
            ]:
                if self.stop_callback():
                    self._log("Stop requested before activate.")
                    return False
                self._log(f"  ssh >> {cmd}")
                if self._send(session, cmd, timeout=30) is None:
                    self._log(f"Timed out after {cmd!r}.")
                    return False

            # ── Activate ────────────────────────────────────────────────────
            version = self._derive_version(self.software_filename)
            activate_cmd = f"software activate version {version}"
            self._log(f"  ssh >> {activate_cmd}")
            if self._send(session, activate_cmd, timeout=60) is None:
                self._log("Timed out submitting the activate command.")
                return False
            # The activate command returns to the prompt immediately; we
            # need one more upgrade-status read to confirm the device
            # picked it up. The Activation In Progress state is the
            # signal we hand back to the GUI.
            if not self._wait_for_state(
                session, target_state="Activation In Progress",
                timeout=_ACTIVATE_TIMEOUT_S,
                poll_interval=_ACTIVATE_POLL_INTERVAL_S,
                label="activate",
            ):
                return False
            self._log("Phase 2 complete — activation in progress on the device.")
            return True
        finally:
            try:
                session.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _derive_version(filename: str) -> str:
        """``waveserver-2.4.52.21-GA.tar.gz`` → ``waveserver-2.4.52.21-GA``.

        Accepts ``.tar.gz`` and ``.tgz`` so an operator-chosen filename
        with either extension produces the right activate target."""
        for ext in (".tar.gz", ".tgz"):
            if filename.lower().endswith(ext):
                return filename[: -len(ext)]
        return filename

    def _wait_for_state(
        self,
        session,
        *,
        target_state: str,
        timeout: float,
        poll_interval: float,
        label: str,
    ) -> bool:
        """Poll ``software show upgrade-status`` until ``Upgrade State``
        equals *target_state* (case-insensitive)."""
        deadline = time.monotonic() + timeout
        last_state = ""
        while time.monotonic() < deadline:
            if self.stop_callback():
                self._log(f"Stop requested while waiting for {target_state!r}.")
                return False
            out = self._send(session, "software show upgrade-status", timeout=20)
            if out is None:
                self._log("SSH read timed out during status poll.")
                return False
            state = self._parse_upgrade_state(out)
            if state and state != last_state:
                self._log(f"  {label} state: {state}")
                last_state = state
            if state.lower() == target_state.lower():
                return True
            time.sleep(poll_interval)
        self._log(
            f"Timed out after {timeout/60:.1f} min waiting for "
            f"{target_state!r}; last seen state was {last_state!r}."
        )
        return False

    @staticmethod
    def _parse_upgrade_state(out: str) -> str:
        """Pull the ``Upgrade State`` value from the boxed table the
        Waveserver prints. The table puts the value column at a fixed
        offset, so we just grab the line and split on the pipe.
        """
        for line in out.splitlines():
            if "Upgrade State" not in line:
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            # Expected layout: ["Upgrade State", "<value>"]
            if len(parts) >= 2:
                return parts[-1]
        return ""

    def _send(self, session, cmd: str, timeout: float = 30.0) -> Optional[str]:
        try:
            session.send(cmd + "\n")
        except Exception as exc:
            logger.debug("ssh send failed: %s", exc)
            return None
        return self._read_until_prompt(session, timeout=timeout)

    def _detect_prompt(self, session) -> bool:
        time.sleep(2.0)
        buf = self._drain(session, idle_seconds=1.5, max_wait=8.0)
        m = _SSH_PROMPT_RE.search(buf)
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
            m = _SSH_PROMPT_RE.search(buf)
        if not m:
            return False
        # Remember the literal prompt text (including the optional ``*``)
        # so _read_until_prompt can wait for the exact same bytes.
        self._ssh_prompt = m.group(0).strip()
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
        """Read until the prompt regex matches at the tail of the buffer.

        Waveserver toggles between ``hostname*#`` (pending changes) and
        ``hostname#`` (saved), so we match on the regex rather than a
        captured literal — the literal can be wrong by one ``*`` and
        leave us blocked.
        """
        deadline = time.time() + timeout
        buf = bytearray()
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
                    tail = bytes(buf).decode("utf-8", errors="replace")
                    if _SSH_PROMPT_RE.search(tail):
                        time.sleep(0.2)
                        if not session.recv_ready():
                            return tail
                        continue
                    if session.closed or session.exit_status_ready():
                        return None
                    time.sleep(0.1)
            except Exception as exc:
                logger.debug("recv error: %s", exc)
                return None
        return None

    # ── Output helpers ──────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        try:
            self.output_callback(msg)
        except Exception:
            logger.exception("Waveserver5 output_callback raised")
        logger.info(msg)
