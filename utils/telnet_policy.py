"""F004 — Telnet policy gate (warn + per-host allowlist + SSH-preferred probe).

This module encapsulates the *decision* of whether a Telnet session may be
opened to a given host. It is consulted by ``utils.telnet.Telnet.__init__``
on every connect attempt unless the caller explicitly bypasses the policy
(e.g. the TL1-over-Telnet path against Ciena 6500, where SSH is not an
option because the TL1 prompt is only available on the Telnet listener).

The three layered defenses:

    A. Warn-and-log — every accepted Telnet session writes a SECURITY-tagged
       WARNING to the rotating log and emits a one-shot GUI banner the first
       time per process. Setting ``LAMIS_REQUIRE_ENCRYPTED_TRANSPORT=1``
       converts every Telnet attempt into a hard refusal.

    B. Per-host allowlist — ``data/telnet_allowlist.json`` (created on demand)
       lists the *only* hosts/CIDRs to which Telnet may be opened. Default is
       empty (deny-all). Operators add entries via ``add_telnet_allowlist``,
       which logs the change for audit.

    C. SSH-preferred probe — before allowing Telnet, try a 1 s TCP connect to
       port 22 on the same host. If SSH answers, refuse Telnet (caller is
       expected to fall back / retry on SSH). The result is cached for the
       process lifetime so we never probe the same host twice.

All public APIs are intentionally side-effect-light and never raise unless
asked to enforce. Refusals raise ``TelnetPolicyError`` which callers translate
into a "log + skip" outcome rather than aborting the run.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.helpers import get_project_root, restrict_path_to_owner

__all__ = [
    "TelnetPolicyError",
    "is_telnet_allowed",
    "add_telnet_allowlist",
    "remove_telnet_allowlist",
    "load_telnet_allowlist",
    "ssh_port_open",
    "enforce_telnet_policy",
    "reset_policy_caches",
]

# ---------------------------------------------------------------------------
# Configuration & module state
# ---------------------------------------------------------------------------

ENV_REQUIRE_ENCRYPTED = "LAMIS_REQUIRE_ENCRYPTED_TRANSPORT"
ENV_DISABLE_SSH_PROBE = "LAMIS_DISABLE_SSH_PROBE"

_ALLOWLIST_FILENAME = "telnet_allowlist.json"
_SSH_PROBE_TIMEOUT_S = 1.0
_SSH_PROBE_PORT = 22

_lock = threading.Lock()
_ssh_probe_cache: Dict[str, Tuple[bool, float]] = {}
_SSH_PROBE_CACHE_TTL_S = 600.0  # 10 minutes

_warned_hosts: set = set()  # hosts we've already logged a warning for
_banner_emitted = False


class TelnetPolicyError(RuntimeError):
    """Raised by ``enforce_telnet_policy`` when a Telnet session is refused."""


# ---------------------------------------------------------------------------
# Allowlist persistence
# ---------------------------------------------------------------------------

def _allowlist_path() -> Path:
    # Store the allowlist in %APPDATA%\ATLAS\data so it is writable without
    # admin privileges when ATLAS is installed in Program Files.
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    atlas_data = Path(app_data) / "ATLAS" / "data"
    atlas_data.mkdir(parents=True, exist_ok=True)
    return atlas_data / _ALLOWLIST_FILENAME


def load_telnet_allowlist() -> Dict[str, str]:
    """Return ``{entry: reason}`` from the allowlist file (empty if absent).

    Each *entry* is either a single IP/hostname or a CIDR (e.g. ``10.10/16``).
    Reasons are free-form audit notes shown in the GUI/logs.
    """
    path = _allowlist_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logging.warning("Telnet allowlist %s is not a JSON object; ignoring", path)
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Telnet allowlist read failed (%s); treating as empty", exc)
        return {}


def _save_allowlist(entries: Dict[str, str]) -> None:
    path = _allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    # Best-effort lock-down of the file: same owner-only ACL we use elsewhere.
    restrict_path_to_owner(path, is_dir=False)


def add_telnet_allowlist(entry: str, reason: str = "") -> None:
    """Add *entry* (host or CIDR) to the allowlist and audit-log it."""
    entry = (entry or "").strip()
    if not entry:
        raise ValueError("entry must be a non-empty host or CIDR")
    entries = load_telnet_allowlist()
    entries[entry] = reason or entries.get(entry, "")
    _save_allowlist(entries)
    logging.warning("SECURITY: telnet allowlist add entry=%s reason=%r",
                    entry, reason)


def remove_telnet_allowlist(entry: str) -> bool:
    """Remove *entry* from the allowlist; return True if it was present."""
    entries = load_telnet_allowlist()
    if entry not in entries:
        return False
    del entries[entry]
    _save_allowlist(entries)
    logging.warning("SECURITY: telnet allowlist remove entry=%s", entry)
    return True


# ---------------------------------------------------------------------------
# Allowlist matching
# ---------------------------------------------------------------------------

def _match_entry(host_ip: Optional[ipaddress._BaseAddress], host_str: str,
                 entry: str) -> bool:
    entry = entry.strip()
    if not entry:
        return False
    # Hostname exact match (case-insensitive)
    if entry.lower() == host_str.lower():
        return True
    if host_ip is None:
        return False
    # IP exact match
    try:
        if ipaddress.ip_address(entry) == host_ip:
            return True
    except ValueError:
        pass
    # CIDR match
    try:
        net = ipaddress.ip_network(entry, strict=False)
        if host_ip in net:
            return True
    except ValueError:
        pass
    return False


def is_telnet_allowed(host: str) -> bool:
    """Return True iff *host* matches an entry in the loaded allowlist."""
    entries = load_telnet_allowlist()
    if not entries:
        return False
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        host_ip = None
    return any(_match_entry(host_ip, host, e) for e in entries)


# ---------------------------------------------------------------------------
# SSH-preferred probe
# ---------------------------------------------------------------------------

def ssh_port_open(host: str, *, timeout: float = _SSH_PROBE_TIMEOUT_S,
                  use_cache: bool = True) -> bool:
    """Return True if a TCP connect to ``host:22`` succeeds within *timeout*.

    Result is cached per host for ``_SSH_PROBE_CACHE_TTL_S`` seconds so we don't
    probe the same device repeatedly during one inventory run.
    """
    if os.environ.get(ENV_DISABLE_SSH_PROBE, "").strip() in ("1", "true", "yes"):
        return False

    now = time.monotonic()
    if use_cache:
        with _lock:
            cached = _ssh_probe_cache.get(host)
            if cached is not None and now - cached[1] < _SSH_PROBE_CACHE_TTL_S:
                return cached[0]

    open_ = False
    try:
        with socket.create_connection((host, _SSH_PROBE_PORT), timeout=timeout):
            open_ = True
    except (OSError, socket.timeout):
        open_ = False

    with _lock:
        _ssh_probe_cache[host] = (open_, now)
    return open_


def reset_policy_caches() -> None:
    """Test helper: clear cached SSH probes and warning state."""
    with _lock:
        _ssh_probe_cache.clear()
        _warned_hosts.clear()
    global _banner_emitted
    _banner_emitted = False


# ---------------------------------------------------------------------------
# Top-level enforcement
# ---------------------------------------------------------------------------

def _emit_banner_once() -> None:
    """One-shot stderr/log banner the first time Telnet is used in a process."""
    global _banner_emitted
    if _banner_emitted:
        return
    _banner_emitted = True
    logging.warning(
        "SECURITY BANNER: One or more Telnet sessions are about to be opened. "
        "Telnet sends credentials and traffic in cleartext. Set "
        "%s=1 to forbid Telnet entirely.", ENV_REQUIRE_ENCRYPTED,
    )


def enforce_telnet_policy(host: str, port: int = 23, *,
                          bypass: bool = False,
                          purpose: Optional[str] = None,
                          skip_ssh_probe: bool = False) -> None:
    """Apply the Telnet policy to a pending connect; raise on refusal.

    Parameters
    ----------
    host, port
        The Telnet target. ``port`` is informational; allowlist matching is
        per-host.
    bypass
        When True, skip enforcement entirely. Used by code paths that *must*
        speak Telnet (e.g. TL1 against Ciena 6500, where the TL1 prompt is
        only available on the Telnet listener and SSH cannot be used).
        Even with ``bypass=True`` we still emit the warn-and-log entry.
    purpose
        Optional short tag included in the audit log line (e.g. ``"tl1"`` or
        ``"console"``). Aids incident response.
    skip_ssh_probe
        When True, keep the allowlist gate (Layer B) but skip the
        "prefer SSH" probe (Layer C). Used by devices whose SSH listener is
        up but does NOT expose the usable CLI — e.g. the Nokia 1830-family
        PSI, where SSH-as-admin dead-ends at a banner and the CLI is only
        reachable through the Telnet getty two-step login. The host must
        still be allowlisted.
    """
    tag = f" purpose={purpose}" if purpose else ""

    # Layer A: hard kill-switch via env var.
    if os.environ.get(ENV_REQUIRE_ENCRYPTED, "").strip() in ("1", "true", "yes"):
        logging.error("SECURITY: telnet refused host=%s port=%d (%s=1)%s",
                      host, port, ENV_REQUIRE_ENCRYPTED, tag)
        raise TelnetPolicyError(
            f"Telnet to {host}:{port} refused: encrypted transport is required "
            f"({ENV_REQUIRE_ENCRYPTED}=1)"
        )

    if bypass:
        _emit_banner_once()
        with _lock:
            first = host not in _warned_hosts
            _warned_hosts.add(host)
        if first:
            logging.warning("SECURITY: telnet bypass host=%s port=%d%s "
                            "(plaintext credentials/traffic)", host, port, tag)
        return

    # Layer B: allowlist gate (default deny).
    if not is_telnet_allowed(host):
        logging.warning("SECURITY: telnet refused host=%s port=%d "
                        "(not in allowlist)%s", host, port, tag)
        raise TelnetPolicyError(
            f"Telnet to {host}:{port} refused: host is not in the Telnet "
            f"allowlist (data/{_ALLOWLIST_FILENAME})"
        )

    # Layer C: SSH-preferred probe. Skipped for devices whose SSH listener is
    # up but doesn't expose a usable CLI (see skip_ssh_probe).
    if skip_ssh_probe and ssh_port_open(host):
        logging.warning("SECURITY: telnet SSH-preference probe skipped for "
                        "host=%s port=%d (SSH up but no usable CLI)%s",
                        host, port, tag)
    elif ssh_port_open(host):
        logging.warning("SECURITY: telnet refused host=%s port=%d "
                        "(SSH available on :22)%s", host, port, tag)
        raise TelnetPolicyError(
            f"Telnet to {host}:{port} refused: SSH is available on port 22 "
            f"(prefer SSH; set {ENV_DISABLE_SSH_PROBE}=1 to disable this check)"
        )

    _emit_banner_once()
    with _lock:
        first = host not in _warned_hosts
        _warned_hosts.add(host)
    if first:
        logging.warning("SECURITY: telnet allowed host=%s port=%d%s "
                        "(plaintext credentials/traffic)", host, port, tag)
