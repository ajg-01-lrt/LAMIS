"""
Device identification, script selection, and shared infrastructure for ATLAS.

Provides:
  - ``is_reachable`` — ICMP ping check
  - ``BaseScript`` — ABC that all device scripts must implement
  - ``CommandTracker`` — deduplication of per-device commands
  - ``DatabaseCache`` — write-through cache for part-number lookups
  - ``DeviceIdentifier`` — SSH/Telnet banner probing to determine device type
  - ``ScriptSelector`` — maps device type strings to script classes
"""
import os
import subprocess
import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple, List, Any, Callable
from queue import Queue
import paramiko
import socket
import re
from utils.telnet import Telnet
from utils.telnet_policy import TelnetPolicyError
from paramiko.ssh_exception import AuthenticationException
import config
from utils.helpers import (
    AuthLockout,
    AuthLockoutError,
    acquire_ping_token,
    friendly_error,
    get_database_path,
    get_host_key_policy,
    get_known_hosts_path,
    get_credentials,
    safe_load_host_keys,
    safe_save_host_keys,
)
from utils.credentials import prompt_for_credentials_gui, get_default_credentials_to_try
from utils.telnet_policy import add_telnet_allowlist, is_telnet_allowed

# At the bottom or top-level
def get_inventory_db_path() -> str:
    """
    Get the inventory database path.
    
    Deprecated: Use utils.helpers.get_database_path() instead.
    Kept for backward compatibility.
    """
    return str(get_database_path())

def is_reachable(ip: str, port: int = 22, timeout: float = 2.0) -> bool:
    """Return True if the host at *ip* accepts a TCP connection on *port*.

    Uses a direct socket connect rather than spawning a ping subprocess so
    no CMD windows appear and the check completes in at most *timeout*
    seconds per host (vs. the OS ping default of ~4 s per packet).
    Falls back to port 23 (Telnet) if port 22 is refused immediately.
    """
    try:
        if not acquire_ping_token(ip, timeout=5.0):
            logging.warning(f"[PING] Rate limiter timeout for {ip}; treating as unreachable")
            return False
        logging.debug(f"[PING] TCP-probe {ip}:{port}")
        with socket.create_connection((ip, port), timeout=timeout):
            logging.debug(f"[PING] {ip} reachable on port {port}")
            return True
    except ConnectionRefusedError:
        # Port actively refused means the host is up, just not SSH.
        logging.debug(f"[PING] {ip} port {port} refused — host is up")
        return True
    except (OSError, socket.timeout):
        # Try Telnet port as a secondary probe (for 1830-type devices).
        if port != 23:
            try:
                with socket.create_connection((ip, 23), timeout=timeout):
                    logging.debug(f"[PING] {ip} reachable on port 23")
                    return True
            except (OSError, socket.timeout):
                pass
        logging.debug(f"[PING] {ip} unreachable")
        return False
    except Exception as e:
        logging.warning(f"[PING] Probe failed for {ip}: {e}")
        return False


# Per-IP counter of how many default-credential entries have already been
# returned by handle_credential_failure during the current identification
# attempt. Index 0 is the primary credential (handled by get_credentials),
# so we start dispensing from index 1. Reset by reset_auth_attempt(ip) at
# the top of identify_device.
_AUTH_DEFAULT_INDEX: Dict[str, int] = {}
_AUTH_PROMPTED: Dict[str, bool] = {}


class CredentialPromptRequired(Exception):
    """Raised when default credentials are exhausted and a user prompt is needed.

    Signals the worker thread to defer this device into the GUI's pause
    queue rather than blocking on a Tk dialog from a non-main thread.
    """

    def __init__(self, ip: str, message: Optional[str] = None) -> None:
        self.ip = ip
        super().__init__(message or f"Credential prompt required for {ip}")


# Sentinel error string vendor scripts can return to flag the worker that this
# IP needs to be parked in the pause queue for later user-credential entry.
NEEDS_CREDENTIALS_SENTINEL = "NEEDS_CREDENTIALS"


def reset_auth_attempt(ip: str) -> None:
    """Clear per-IP credential rotation state at the start of a new identification."""
    _AUTH_DEFAULT_INDEX.pop(ip, None)
    _AUTH_PROMPTED.pop(ip, None)


def handle_credential_failure(ip: str, queue: Queue, tried_defaults: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """Handle authentication failure by rotating to the next default credential.

    Cycles through *every* configured default credential set (e.g. admin/admin,
    ADMIN/ADMIN, cli/admin) and returns the next untried pair. Per-IP state is
    tracked in ``_AUTH_DEFAULT_INDEX`` so each call advances by one entry.

    When no defaults remain, this raises :class:`CredentialPromptRequired` so
    the caller can park the device in a pause queue and prompt the user from
    the main thread later. Worker threads must NOT pop a Tk dialog directly —
    Tk is not thread-safe and will deadlock on Windows.

    Args:
        ip: Device IP address
        queue: Output queue for messages
        tried_defaults: If True, skip the default-credential rotation and go
            straight to raising CredentialPromptRequired.

    Returns:
        (username, password) for the next default credential pair to try.

    Raises:
        CredentialPromptRequired: when defaults are exhausted (or
            ``tried_defaults=True``). Caller should park the IP for later
            user-credential entry on the main thread.
    """
    defaults = get_default_credentials_to_try() or []

    if not tried_defaults:
        # Start from index 0 so that "cli" (the Nokia 1830 SSH account) is
        # always tried, even when the configured primary credential is a
        # different account (e.g. admin/admin stored in the Credential Manager).
        # Previously this defaulted to 1, assuming defaults[0] had already been
        # used as the primary — but that assumption is wrong when the primary
        # comes from the Credential Manager rather than from the defaults list.
        next_idx = _AUTH_DEFAULT_INDEX.get(ip, 0)
        if next_idx < len(defaults):
            _AUTH_DEFAULT_INDEX[ip] = next_idx + 1
            queue.put("[AUTH] Trying alternate default credentials...\n")
            logging.info(f"[AUTH] Using default credential set #{next_idx + 1} for {ip}")
            return defaults[next_idx]

    # All defaults tried — but only signal once per IP so a re-entrant caller
    # doesn't double-park the same device.
    if _AUTH_PROMPTED.get(ip):
        queue.put(f"[AUTH] No more credentials to try for {ip}\n")
        return (None, None)
    _AUTH_PROMPTED[ip] = True

    queue.put(
        f"[AUTH] Default credentials exhausted for {ip} — parking for "
        f"manual credential entry after the rest of the run.\n"
    )
    logging.warning(
        f"[AUTH] Default credentials exhausted for {ip}; deferring to user prompt"
    )
    raise CredentialPromptRequired(ip)


def ssh_connect_with_credential_fallback(
    ssh_client: "paramiko.SSHClient",
    ip: str,
    primary_user: str,
    primary_pass: str,
    *,
    timeout: int = 10,
    look_for_keys: bool = False,
    allow_agent: bool = False,
    extra_connect_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """SSH-connect ``ssh_client`` to ``ip`` cycling through credentials.

    Order of attempts:
        1. ``primary_user`` / ``primary_pass`` (whatever the caller was given)
        2. Every entry from ``get_default_credentials_to_try()`` that hasn't
           already been tried this call (admin/admin, ADMIN/ADMIN, cli/admin
           by default)
        3. A single GUI prompt of the user via ``prompt_for_credentials_gui``

    The credential pair that succeeds is returned so callers can reuse it
    for any downstream interactive step. If the user-prompt attempt also
    fails (or the user cancels) ``paramiko.AuthenticationException`` is
    raised and the caller is expected to abandon this IP.

    Non-auth errors (timeouts, connection refused, etc.) are re-raised
    immediately without trying alternate credentials.
    """
    import paramiko  # local import keeps this helper test-friendly

    extra = dict(extra_connect_kwargs or {})

    def _attempt(user: str, pwd: str) -> None:
        ssh_client.connect(
            ip,
            username=user,
            password=pwd,
            look_for_keys=look_for_keys,
            allow_agent=allow_agent,
            timeout=timeout,
            **extra,
        )

    tried: set = set()

    def _norm(u: str, p: str) -> Tuple[str, str]:
        return (u or "", p or "")

    # Build the queue: primary first, then any defaults not equal to primary.
    queue_pairs: List[Tuple[str, str]] = []
    primary_pair = _norm(primary_user, primary_pass)
    queue_pairs.append(primary_pair)
    for u, p in (get_default_credentials_to_try() or []):
        pair = _norm(u, p)
        if pair not in queue_pairs:
            queue_pairs.append(pair)

    last_auth_error: Optional[Exception] = None
    for user, pwd in queue_pairs:
        if (user, pwd) in tried:
            continue
        tried.add((user, pwd))
        try:
            logging.info(f"[AUTH] SSH login attempt to {ip} as {user!r}")
            _attempt(user, pwd)
            logging.info(f"[AUTH] SSH login succeeded for {ip} as {user!r}")
            return (user, pwd)
        except paramiko.AuthenticationException as ae:
            last_auth_error = ae
            logging.warning(f"[AUTH] SSH login failed for {ip} as {user!r}")
            continue

    # All defaults exhausted — defer to the GUI's pause queue. Worker threads
    # cannot safely pop a Tk dialog (no-ops on Windows), so we raise and let
    # the calling vendor script convert this into NEEDS_CREDENTIALS for the
    # main-thread credential prompt.
    logging.warning(
        f"[AUTH] All default credentials failed for {ip}; deferring to user prompt"
    )
    raise CredentialPromptRequired(ip)


class BaseScript(ABC):
    """Abstract base class that all device scripts must implement."""

    @abstractmethod
    def get_commands(self) -> List[str]:
        """Return the list of commands to execute on this device type."""

    @abstractmethod
    def execute_commands(self, commands: List[str]) -> Tuple[List[str], Optional[str]]:
        """Execute commands on the device. Returns (outputs, error_or_None)."""

    @abstractmethod
    def process_outputs(self, outputs_from_device: List[str], ip_address: str, outputs: Dict[str, Any]) -> None:
        """Parse raw device output and populate the outputs dict."""

    @abstractmethod
    def abort_connection(self) -> None:
        """Forcefully close any open connection to interrupt blocking I/O."""

    def should_stop(self) -> bool:
        """Return True if a stop has been requested via the stop_callback."""
        return bool(getattr(self, 'stop_callback', None) and self.stop_callback())

    def set_existing_ssh_client(self, client: "paramiko.SSHClient") -> None:
        """Inject an already-authenticated SSH client to avoid re-login.

        Called by the inventory pipeline when the identification step kept the
        SSH transport alive after probing the device.  Scripts that use
        paramiko directly (Nokia SAR, IXR, Smartoptics DCP) check
        ``self._injected_ssh_client`` in ``execute_ssh_commands`` and skip
        the connect step when it is set.
        """
        self._injected_ssh_client = client

class CommandTracker:
    """Track which CLI commands have already been run on a given device.

    Prevents duplicate command execution when a script is re-entered or
    retried on the same IP/connection pair within a single scan session.
    """
    def __init__(self) -> None:
        self.executed_commands: Dict[Tuple[str, str], set] = {}
        logging.debug("[TRACKER] Initialized command tracker")

    def has_executed(self, ip: str, command: str, connection_type: str) -> bool:
        """Return True if *command* has already been run on *ip* over *connection_type*."""
        key = (ip, connection_type)
        result = command in self.executed_commands.get(key, set())
        logging.debug(f"[TRACKER] Check if executed '{command}' on {ip}/{connection_type}: {result}")
        return result

    def mark_as_executed(self, ip: str, command: str, connection_type: str) -> None:
        """Record that *command* has been executed on *ip* over *connection_type*."""
        key = (ip, connection_type)
        if key not in self.executed_commands:
            self.executed_commands[key] = set()
        self.executed_commands[key].add(command)
        logging.debug(f"[TRACKER] Marked '{command}' as executed on {ip}/{connection_type}")

    def reset(self) -> None:
        """Clear all execution history (call between scan sessions)."""
        self.executed_commands.clear()
        logging.debug("[TRACKER] Reset executed commands")

class DatabaseCache:
    """In-memory write-through cache for part number → description lookups.

    Wraps a SQLite parts database so repeated lookups for the same part
    number don't hit disk on every call.
    """
    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        self.cache: Dict[str, str] = {}
        logging.debug(f"[CACHE] Initialized cache with DB path: {self.db_path}")

    def lookup_part(self, part_number: str) -> str:
        """Return the description for *part_number* (first 10 chars used as key).

        Returns "Invalid part number" if part_number is empty/whitespace,
        "Not Found" when the part is absent from the database,
        or a "DB Error: ..." string if the database cannot be read.
        """
        # Validate part number is not empty or whitespace
        if not part_number or not part_number.strip():
            return "Invalid part number"
        
        key = part_number[:10]
        
        # Check cache first
        if key in self.cache:
            return self.cache[key]
        
        # Lookup in database and cache result. The parts catalog occasionally
        # holds multiple rows that share the same 10-char part number (e.g.
        # an original entry plus a vendor-qualified variant carrying extra
        # text like "IEEE 1613 Class 2 enabled ..."). To make results
        # deterministic and prefer the canonical catalog row over qualified
        # variants, we first try an exact match on the 10-char key and fall
        # back to LIKE prefix only when no exact row exists. On ties we
        # pick the shortest description, which reliably picks the plain
        # catalog row over a qualified one.
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT description FROM parts WHERE part_number = ? "
                    "ORDER BY LENGTH(description) ASC LIMIT 1",
                    (key,),
                )
                result = cursor.fetchone()
                if not result:
                    cursor.execute(
                        "SELECT description FROM parts WHERE part_number LIKE ? "
                        "ORDER BY LENGTH(description) ASC LIMIT 1",
                        (key + "%",),
                    )
                    result = cursor.fetchone()
                description = result[0] if result else "Not Found"
        except sqlite3.Error:
            logging.exception(f"[CACHE] DB error on '{key}'")
            description = "Not Found"
        
        # Always cache the result before returning
        self.cache[key] = description
        return description
        
_CACHE = None
def get_cache():
    """Return the process-wide singleton DatabaseCache, creating it on first call."""
    global _CACHE
    if _CACHE is None:
        _CACHE = DatabaseCache(get_inventory_db_path())
    return _CACHE

_TRACKER = None
def get_tracker():
    """Return the process-wide singleton CommandTracker, creating it on first call."""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = CommandTracker()
    return _TRACKER

class DeviceIdentifier:
    """Identify a network device's type and name by probing it via SSH then Telnet."""

    def __init__(self) -> None:
        # Holds the open paramiko SSHClient kept alive after a successful
        # non-1830 identification so the pipeline can inject it into the script
        # and skip the second login round-trip.  Thread-safe as long as each
        # concurrent device uses its own DeviceIdentifier instance (which the
        # pipeline guarantees).
        self._identified_client: Optional["paramiko.SSHClient"] = None
        # Credentials that actually authenticated during identification.
        # ``select_script`` consumes these (preferring them over the user's
        # primary saved credentials) so the inventory script doesn't have
        # to re-rotate through admin/admin → cli → su to find what works.
        # Cleared by ``take_identified_credentials`` once consumed; reset
        # at the top of each ``identify_device`` call.
        self._identified_credentials: Optional[Tuple[str, str]] = None

    def take_identified_client(self) -> Optional["paramiko.SSHClient"]:
        """Return and clear the SSH client preserved after successful identification.

        Scripts that use paramiko directly (Nokia SAR, IXR, Smartoptics DCP)
        receive this via ``set_existing_ssh_client()`` so they can reuse the
        already-authenticated transport instead of opening a new connection.
        Returns ``None`` for Nokia 1830 devices (which use Telnet/spawn) or
        when identification did not succeed via the SSH path.
        """
        client = self._identified_client
        self._identified_client = None
        return client

    def take_identified_credentials(self) -> Optional[Tuple[str, str]]:
        """Return and clear the (username, password) pair that authenticated
        during identification. ``select_script`` consumes these so the
        inventory script starts with the credential we already know
        works. Returns ``None`` when identification did not succeed via
        SSH (e.g. Telnet-only Nokia 1830 path)."""
        creds = self._identified_credentials
        self._identified_credentials = None
        return creds

    # SSH-level credentials used for the first stage of the Nokia 1830 two-stage
    # login. The 1830 SSH server only accepts the "cli" account; once SSH is
    # authenticated, the device drops into its own CLI login that prompts for
    # the actual operator credentials. Listed in order of likelihood.
    _NOKIA_1830_SSH_PASSWORDS = ("cli", "admin")

    # Identification commands tried (in order) once a shell session is open.
    # The 1830 responds to `show software-zynq version` with a banner that the
    # parser keys on; other Nokia gear uses `show chassis | match ...`.
    _IDENT_COMMANDS = (
        "show chassis | match (Type) pre-lines 1 expression",
        "show general system-identification",
        "show general detail",
        "show shelf product",
    )

    # Vendor-agnostic banner/output fingerprints. Each entry is consulted
    # against the post-login MOTD (and any subsequent CLI output) before
    # we resort to vendor-specific identification commands. This is how a
    # device that self-identifies in its login banner (e.g. Smartoptics
    # DCP, Ciena 6500) gets recognized without us having to send anything.
    #
    # Each entry:
    #   regex          — pattern to search against the banner/output text
    #   device_type    — value returned for ScriptSelector
    #   name_from_*    — optional ways to recover the hostname:
    #       name_from_group: regex group index in `regex`
    #       name_from_prompt: regex with one group capturing the hostname
    #                         in the shell prompt (e.g. "user@host>")
    #       name_default:    fallback string when no hostname is found
    _DEVICE_FINGERPRINTS: Tuple[Dict[str, Any], ...] = (
        {
            "regex": re.compile(
                r"^\s*(DCP-[A-Z0-9\-+]+)\s*,\s*.*?DWDM Open Line System",
                re.MULTILINE,
            ),
            "device_type": "dcp",
            "name_from_prompt": re.compile(r"[A-Za-z0-9._-]+@([A-Za-z0-9._-]+)>\s*$"),
            "name_default": "Smartoptics DCP",
        },
        {
            # Ciena 6500 RLS — must precede the generic 6500 fingerprint
            # below, since the RLS banner ("Ciena 6500 Reconfigurable Line
            # System") would otherwise match the plain 6500 entry first
            # and route to the wrong inventory script.
            "regex": re.compile(
                r"\b6500\s+Reconfigurable\s+Line\s+System\b|\bciena\s+RLS\b",
                re.IGNORECASE,
            ),
            "device_type": "rls",
            "name_default": "Ciena 6500 RLS",
        },
        {
            "regex": re.compile(r"\bciena\s+6500\b", re.IGNORECASE),
            "device_type": "6500",
            "name_default": "Ciena 6500 OPTICAL",
        },
        # Nokia PSIM (1830 PSI-M shelf) — must precede the generic PSI / 1830
        # fingerprints. PSIM identifies via the "Nokia 1830 PSIM" platform
        # string in `show general detail`'s System Description, OR via the
        # "PSI-M" shelf type from `show shelf inventory`.
        {
            "regex": re.compile(
                r"\bNokia\s+1830\s+PSIM\b|Shelf\s+type\s*:\s*PSI-M\b|^\s*\d+\s+PSI-M\b",
                re.IGNORECASE | re.MULTILINE,
            ),
            "device_type": "psim",
            "name_from_prompt": re.compile(r"([A-Za-z0-9._-]+)\s*[#>]\s*$"),
            "name_default": "Nokia 1830 PSIM",
        },
        # Nokia PSI (1830 with PSI shelf) — MUST precede generic 1830 fingerprint
        # so PSI devices (which report "Product: 1830" but have "Shelf type: PSI-4L/PSI-8L")
        # are identified as PSI and use the PSI script, not the 1830 script.
        {
            "regex": re.compile(r"Shelf\s+type\s*:\s*PSI-(4L|8L)", re.IGNORECASE),
            "device_type": "psi",
            "name_from_prompt": re.compile(r"([A-Za-z0-9._-]+)\s*[#>]\s*$"),
            "name_default": "Nokia PSI",
        },
        # Generic 1830 fingerprint — matches platform string anywhere
        # (used when SSH banner sniffing happens before we ever get a
        # chance to log in via the dedicated 1830 telnet/cli paths).
        {
            "regex": re.compile(r"Nokia\s+1830\s+PSS", re.IGNORECASE),
            "device_type": "1830",
            "name_from_prompt": re.compile(r"([A-Za-z0-9._-]+)\s*[#>]\s*$"),
            "name_default": "Nokia 1830",
        },
        {
            # Nokia 7250 IXR — TiMOS banner: "Nokia 7250 IXR-R6" / "7250 IXR-R6d"
            "regex": re.compile(r"\bNokia\s+7250\s+IXR[-\s]?R6D?\b", re.IGNORECASE),
            "device_type": "7250 ixr-r6",
            "name_from_prompt": re.compile(r"([A-Za-z0-9._-]+)\s*[#>]\s*$"),
            "name_default": "Nokia 7250 IXR-R6",
        },
        {
            # Nokia 7705 SAR — TiMOS banner: "Nokia 7705 SAR-8" variants
            "regex": re.compile(r"\bNokia\s+7705\s+SAR\b", re.IGNORECASE),
            "device_type": "7705 sar-8 v2",
            "name_from_prompt": re.compile(r"([A-Za-z0-9._-]+)\s*[#>]\s*$"),
            "name_default": "Nokia 7705 SAR-8",
        },
    )

    # When the pre-auth SSH banner identifies the device, jump straight to
    # the credential most likely to succeed. Values come from the encrypted
    # defaults store via ``utils.credentials.get_default_credential_for_vendor``
    # so the password is never duplicated in code — only the vendor key is.
    # Devices defaulting to admin/admin aren't listed here because
    # admin/admin is already the seed's first entry.
    _BANNER_PREFERRED_VENDOR: Dict[str, str] = {
        "rls":  "ciena",
        "6500": "ciena",
        "1830": "nokia-1830",
    }

    @classmethod
    def _banner_preferred_creds(cls, device_type: str) -> Optional[Tuple[str, str]]:
        """Return the encrypted-store credentials for a banner-identified
        device, or ``None`` if defaults are disabled / the entry is missing."""
        vendor = cls._BANNER_PREFERRED_VENDOR.get(device_type)
        if not vendor:
            return None
        from utils.credentials import get_default_credential_for_vendor
        return get_default_credential_for_vendor(vendor)

    @staticmethod
    def _peek_ssh_banner(ip: str, port: int = 22, timeout: float = 8.0) -> str:
        """Open an SSH transport and read the pre-auth banner without
        supplying real credentials.

        SSH servers may send ``SSH_MSG_USERAUTH_BANNER`` (RFC 4252 §5.4)
        during the auth phase but before any password is accepted. Many
        Ciena / Nokia devices include the platform name in that banner
        (e.g. ``Ciena 6500 Reconfigurable Line System``), which lets us
        pick the right credentials on the first try instead of burning
        rotation attempts on wrong ones.

        Uses ``auth_none`` against a junk username — almost every device
        rejects it with ``BadAuthenticationType``, but the banner is
        delivered before that rejection. Failures are swallowed and
        return an empty string so the regular rotation can proceed.
        """
        sock = None
        transport = None
        try:
            sock = socket.create_connection((ip, port), timeout=timeout)
            transport = paramiko.Transport(sock)
            transport.banner_timeout = timeout
            transport.start_client(timeout=timeout)
            try:
                transport.auth_none("probe")
            except (paramiko.BadAuthenticationType, AuthenticationException):
                # Expected — server rejected the probe but the banner
                # was sent in the same exchange and is now cached on the
                # transport.
                pass
            banner = transport.get_banner() or ""
            if isinstance(banner, bytes):
                banner = banner.decode("utf-8", errors="replace")
            return banner.strip()
        except Exception as e:
            logging.debug(f"[BANNER] Peek failed for {ip}: {e}")
            return ""
        finally:
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    @classmethod
    def _match_fingerprints(cls, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Apply ``_DEVICE_FINGERPRINTS`` to *text* and return the first hit.

        Returns:
            (device_type, device_name) — both None if no fingerprint matches.
        """
        if not text:
            return None, None
        for fp in cls._DEVICE_FINGERPRINTS:
            m = fp["regex"].search(text)
            if not m:
                continue
            device_type = fp["device_type"]

            # Prefer hostname captured directly by the platform regex.
            name_group = fp.get("name_from_group")
            if name_group is not None:
                try:
                    return device_type, m.group(name_group).strip()
                except (IndexError, AttributeError):
                    pass

            # Otherwise try to lift the hostname out of the shell prompt.
            prompt_pat = fp.get("name_from_prompt")
            if prompt_pat is not None:
                pm = prompt_pat.search(text)
                if pm:
                    return device_type, pm.group(1).strip()

            return device_type, fp.get("name_default")
        return None, None

    def _drain_shell(self, session, idle_seconds: float = 2.0,
                     max_wait: float = 12.0) -> str:
        """Read from a paramiko invoke_shell channel until it goes idle."""
        buf = ""
        deadline = time.time() + max_wait
        last_data = time.time()
        while time.time() < deadline:
            if session.recv_ready():
                chunk = session.recv(4096).decode("utf-8", errors="ignore")
                if chunk:
                    buf += chunk
                    last_data = time.time()
                    continue
            if time.time() - last_data >= idle_seconds:
                break
            time.sleep(0.1)
        return buf

    def _do_1830_two_stage_login(self, session, queue: Queue,
                                  shell_user: str, shell_pass: str) -> bool:
        """Drive the post-SSH Nokia 1830 CLI login dialog.

        After SSH auth as "cli" succeeds, the 1830 presents its own
        ``Username:`` / ``Password:`` prompts. Returns True on apparent
        success (a shell prompt was reached), False otherwise.
        """
        banner = self._drain_shell(session, idle_seconds=1.5, max_wait=8.0)
        snippet = banner[-300:].replace("\r", "\\r").replace("\n", "\\n")
        logging.info(f"[1830] Pre-login banner ({len(banner)} bytes): {snippet!r}")
        queue.put(f"[1830] Pre-login banner: {snippet}\n")
        if not re.search(r"(?i)(username|login)\s*:", banner):
            logging.warning(f"[1830] No username prompt seen after SSH auth (banner tail: {snippet!r})")
            queue.put("[1830] No username prompt seen after SSH auth\n")
            return False

        logging.info(f"[1830] Sending shell username: {shell_user}")
        queue.put(f"[1830] Sending username: {shell_user}\n")
        session.send(f"{shell_user}\n")
        pwd_banner = self._drain_shell(session, idle_seconds=1.0, max_wait=8.0)
        snippet = pwd_banner[-300:].replace("\r", "\\r").replace("\n", "\\n")
        logging.info(f"[1830] After username send ({len(pwd_banner)} bytes): {snippet!r}")
        queue.put(f"[1830] After username: {snippet}\n")
        if not re.search(r"(?i)password\s*:", pwd_banner):
            logging.warning(f"[1830] No password prompt after sending username (got: {snippet!r})")
            queue.put("[1830] No password prompt after sending username\n")
            return False

        logging.info(f"[1830] Sending shell password (length={len(shell_pass)})")
        queue.put("[1830] Sending password\n")
        session.send(f"{shell_pass}\n")
        post = self._drain_shell(session, idle_seconds=1.5, max_wait=10.0)
        snippet = post[-300:].replace("\r", "\\r").replace("\n", "\\n")
        logging.info(f"[1830] After password send ({len(post)} bytes): {snippet!r}")
        queue.put(f"[1830] After password: {snippet}\n")
        if re.search(r"(?i)(incorrect|invalid|fail|denied)", post):
            logging.warning(f"[1830] Inner CLI auth rejected (response: {snippet!r})")
            queue.put("[1830] Inner CLI auth rejected\n")
            return False

        # Some 1830 builds present a Y/n acknowledgement banner (license/EULA
        # or session-warning) before dropping to the shell prompt. Auto-answer
        # "y" up to a few times, then re-drain. If we see a prompt char first,
        # skip the ack handling.
        for ack_round in range(3):
            if re.search(r"[#>\$]\s*$", post) or "user@" in post.lower():
                logging.info("[1830] Detected shell prompt — login complete")
                break
            if re.search(r"(?i)\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|continue\?|accept\?|press\s+y", post):
                logging.info(f"[1830] Acknowledging Y/n prompt (round {ack_round + 1})")
                queue.put("[1830] Acknowledging post-login Y/n prompt\n")
                session.send("y\n")
                post = self._drain_shell(session, idle_seconds=1.0, max_wait=8.0)
                snippet = post[-300:].replace("\r", "\\r").replace("\n", "\\n")
                logging.info(f"[1830] After Y/n ack: {snippet!r}")
                queue.put(f"[1830] After Y/n ack: {snippet}\n")
                continue
            logging.info(f"[1830] No prompt char and no Y/n ack pattern — bailing out of ack loop")
            break

        # Reaching a prompt char (#, >, $) indicates we're in.
        logged_in = bool(re.search(r"[#>\$]\s*$", post)) or "user@" in post.lower()
        if logged_in:
            logging.info("[1830] Two-stage login SUCCESS")
            queue.put("[1830] Two-stage login successful\n")
        else:
            logging.warning(f"[1830] Two-stage login did not yield a shell prompt; final tail: {post[-300:]!r}")
            queue.put("[1830] Two-stage login did not yield a shell prompt\n")
        return logged_in

    def _ssh_connect_1830_cli(self, ip: str, username: str, inner_password: str,
                                queue: Queue) -> "paramiko.SSHClient":
        """Open an SSH session to a Nokia 1830's `cli` account.

        The 1830's cli account does not use a real SSH password — PuTTY and
        OpenSSH succeed via ``auth_none`` or ``keyboard-interactive`` with
        empty responses. paramiko's high-level ``SSHClient.connect()`` only
        tries password and key auth, which the device rejects with
        "Authentication failed."

        We open a low-level Transport, run a method cascade, and stash the
        authenticated transport into an SSHClient so the rest of the
        identification flow (invoke_shell / close) works unchanged.
        """
        sock = socket.create_connection((ip, 22), timeout=config.SSH_CONNECT_TIMEOUT)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=config.SSH_CONNECT_TIMEOUT)
        except (paramiko.SSHException, OSError):
            sock.close()
            raise

        # Verify host key against known_hosts using the same policy as the
        # high-level path.
        try:
            remote_key = transport.get_remote_server_key()
            policy = get_host_key_policy()
            # Use a real SSHClient instance solely as the "client" argument
            # to policy.missing_host_key. paramiko's AutoAddPolicy calls a
            # bunch of SSHClient internals (_log, save_host_keys,
            # get_host_keys, etc.) so a homemade stub can't satisfy it
            # without surprises. The real SSHClient is cheap to construct
            # and we never call .connect() on it, so no extra network IO.
            host_key_client = paramiko.SSHClient()
            # paramiko's SSHClient._log delegates to self._transport._log,
            # which is None until .connect() runs. AutoAddPolicy logs the
            # newly-accepted key, so without a Transport attached we'd hit
            # "'NoneType' object has no attribute '_log'". Attach the
            # already-handshaken Transport we built above; we never call
            # .connect() on host_key_client, so this is safe.
            host_key_client._transport = transport
            safe_load_host_keys(host_key_client, str(get_known_hosts_path()))
            try:
                policy.missing_host_key(host_key_client, ip, remote_key)
                # AutoAddPolicy mutates host_key_client.get_host_keys() in
                # memory but only persists if .save_host_keys() is called.
                safe_save_host_keys(host_key_client, str(get_known_hosts_path()))
            except Exception as he:
                transport.close()
                raise paramiko.SSHException(f"Host key check failed for {ip}: {he}")
        except paramiko.SSHException:
            raise
        except (OSError, ValueError, KeyError) as e:
            logging.debug(f"[1830] Host-key verification skipped: {e}")

        last_err: Optional[Exception] = None
        authed = False

        # 1) auth_none — many 1830s drop straight to the inner CLI with no
        #    SSH-layer password at all.
        try:
            allowed = transport.auth_none(username) or []
            if not allowed:
                authed = True
                logging.info(f"[1830] auth_none succeeded for {username}@{ip}")
                queue.put(f"[1830] SSH auth_none succeeded as {username}\n")
            else:
                logging.info(f"[1830] auth_none rejected; server allows: {allowed}")
                queue.put(f"[1830] SSH server allows methods: {','.join(allowed)}\n")
        except paramiko.BadAuthenticationType as bat:
            allowed = list(bat.allowed_types or [])
            logging.info(f"[1830] auth_none not allowed; server allows: {allowed}")
            queue.put(f"[1830] SSH server allows methods: {','.join(allowed)}\n")
        except AuthenticationException as ae:
            last_err = ae
            allowed = []
            logging.info(f"[1830] auth_none failed: {ae}")

        # 2) keyboard-interactive with empty answers — what PuTTY does by
        #    default for accounts whose SSH server prompts but ignores the
        #    response.
        if not authed and (not allowed or "keyboard-interactive" in allowed):
            try:
                logging.info(f"[1830] Trying keyboard-interactive (empty answers) for {username}@{ip}")
                queue.put("[1830] Trying SSH keyboard-interactive auth\n")

                def _kbd_handler(title, instructions, prompt_list):
                    return [""] * len(prompt_list)

                transport.auth_interactive(username, _kbd_handler)
                authed = True
                logging.info("[1830] keyboard-interactive auth succeeded")
            except AuthenticationException as ae:
                last_err = ae
                logging.info(f"[1830] keyboard-interactive failed: {ae}")

        # 3) password auth fallback — empty, then a few common candidates.
        # paramiko's Transport closes after a failed auth_password on most
        # servers, so the second iteration would die with
        # SSHException("No existing session"). Break out on any non-auth
        # error and let the outer credential-rotation loop try a fresh
        # Transport with the next default credential.
        if not authed and (not allowed or "password" in allowed):
            for pw in ("", "cli", "admin", inner_password):
                if pw is None:
                    continue
                try:
                    logging.info(f"[1830] Trying password auth (length={len(pw)}) for {username}@{ip}")
                    transport.auth_password(username, pw)
                    authed = True
                    logging.info("[1830] password auth succeeded")
                    break
                except AuthenticationException as ae:
                    last_err = ae
                    if not transport.is_active():
                        logging.info(
                            "[1830] Transport closed after failed password — "
                            "cascade aborting; outer loop will retry with next credential"
                        )
                        break
                    continue
                except paramiko.SSHException as se:
                    # "No existing session" / transport torn down — convert to
                    # an auth failure so the outer credential loop can rotate.
                    logging.info(f"[1830] Password auth aborted (transport dead): {se}")
                    last_err = AuthenticationException(str(se))
                    break

        if not authed:
            transport.close()
            raise last_err or AuthenticationException("All 1830 SSH auth methods failed")

        # Wrap the authenticated transport in an SSHClient so downstream
        # code can treat it the same as a normally-connected client.
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        safe_load_host_keys(client, str(get_known_hosts_path()))
        client._transport = transport
        return client

    def _ssh_identify_attempt(self, ip: str, username: str, password: str,
                               queue: Queue, should_stop: Callable[[], bool],
                               sleep_with_abort: Callable[[float], bool]
                               ) -> Tuple[Optional[str], Optional[str]]:
        """Open one SSH session and try to identify the device.

        For the special username ``"cli"`` (Nokia 1830), this opens an SSH
        session under the ``cli`` account and then performs the two-stage
        in-shell login dialog using the supplied *password* as the inner
        operator password (paired with operator user ``admin``).

        Raises:
            paramiko.AuthenticationException — SSH-layer auth failed.
            Exception — other connection errors propagate to the caller.
        """
        AuthLockout.check(ip)
        _kh = str(get_known_hosts_path())
        is_1830 = username.lower() == "cli"

        if is_1830:
            # The 1830's `cli` SSH account does NOT use a real password —
            # PuTTY/OpenSSH succeed via auth_none or keyboard-interactive
            # with empty responses. paramiko's high-level connect(password=)
            # cannot negotiate that, so drop to Transport and try a
            # method cascade explicitly.
            ssh_client = self._ssh_connect_1830_cli(ip, username, password, queue)
        else:
            ssh_passwords = [password]
            last_auth_err: Optional[Exception] = None
            ssh_client = None
            for ssh_pw in ssh_passwords:
                try:
                    ssh_client = paramiko.SSHClient()
                    ssh_client.load_system_host_keys()
                    safe_load_host_keys(ssh_client, _kh)
                    ssh_client.set_missing_host_key_policy(get_host_key_policy())
                    ssh_client.connect(
                        ip, username=username, password=ssh_pw,
                        look_for_keys=False, allow_agent=False,
                        timeout=config.SSH_CONNECT_TIMEOUT,
                    )
                    last_auth_err = None
                    break
                except AuthenticationException as ae:
                    last_auth_err = ae
                    try:
                        ssh_client.close()
                    except (OSError, paramiko.SSHException):
                        pass
                    ssh_client = None
                    continue

            if ssh_client is None:
                # Surface the SSH auth failure to the caller's existing handler.
                assert last_auth_err is not None
                raise last_auth_err

        # Track the shell channel so we can close just the channel in `finally`
        # while optionally keeping the SSH transport alive for the script to reuse.
        session = None
        _keep_client = False  # set True on successful non-1830 identification

        try:
            AuthLockout.register_success(ip)
            safe_save_host_keys(ssh_client, _kh)

            queue.put(f"Connected to {ip} via SSH ({username})\n")
            session = ssh_client.invoke_shell()

            if is_1830:
                queue.put(f"[1830] Performing two-stage CLI login on {ip}\n")
                # Use "admin" as the inner operator user; the supplied
                # password is the inner operator password.
                if not self._do_1830_two_stage_login(
                    session, queue, shell_user="admin", shell_pass=password
                ):
                    queue.put(f"[1830] Two-stage login failed on {ip}\n")
                    return None, None
                # We connected as `cli` and the inner CLI login succeeded —
                # this is unambiguously a Nokia 1830 PSS. Run the
                # platform-native identification command to capture the
                # hostname (Name) and the precise platform string
                # (System Description, e.g. "Nokia 1830 PSS v22.12.0
                # SONET ADM"). The generic Junos-style _IDENT_COMMANDS
                # return "command not found" on this platform, so we use
                # the 1830's own `show general detail` here.
                ident_cmd = "show general detail"
                logging.info(f"[1830] Sending identification command: {ident_cmd}")
                queue.put(f"[1830] Sending '{ident_cmd}'\n")
                session.send(ident_cmd + "\n")
                if sleep_with_abort(2):
                    queue.put(f"[ABORT] SSH identification cancelled for {ip}.\n")
                    return None, None
                ident_out = self._drain_shell(session, idle_seconds=1.5, max_wait=10.0)
                logging.info(f"[1830] '{ident_cmd}' returned {len(ident_out)} bytes")
                logging.debug(f"[1830] Raw response from {ip}:\n{ident_out}")
                queue.put(f"[1830] Response from {ip}:\n{ident_out}\n")

                name_m = re.search(r"^\s*Name\s*:\s*(\S.*?)\s*$", ident_out, re.MULTILINE)
                desc_m = re.search(
                    r"^\s*System\s+Description\s*:\s*(\S.*?)\s*$",
                    ident_out, re.MULTILINE,
                )
                hostname = name_m.group(1) if name_m else None
                system_desc = desc_m.group(1) if desc_m else None

                if not hostname:
                    prompt_m = re.search(
                        r"([A-Za-z0-9._\-]+)\s*[#>]\s*$", ident_out
                    )
                    hostname = prompt_m.group(1) if prompt_m else "Nokia 1830"

                if system_desc:
                    logging.info(
                        f"[1830] Identified {ip}: name={hostname!r} "
                        f"system_description={system_desc!r}"
                    )
                    queue.put(f"[1830] {hostname} — {system_desc}\n")
                else:
                    logging.info(f"[1830] Identified {ip} as Nokia 1830 (hostname={hostname!r})")
                    queue.put(f"[1830] Identified as Nokia 1830 ({hostname})\n")

                return "1830", hostname

            # Vendor-agnostic post-login banner check. Many platforms
            # (Smartoptics DCP, etc.) print a self-identifying MOTD as
            # soon as a shell session opens. Drain it and apply
            # fingerprints BEFORE we send any vendor-specific ident
            # commands that might error out on this platform.
            if sleep_with_abort(1):
                queue.put(f"[ABORT] SSH identification cancelled for {ip}.\n")
                return None, None
            login_banner = self._drain_shell(session, idle_seconds=1.0, max_wait=4.0)
            if login_banner:
                logging.debug(f"[IDENTIFY] Post-login banner from {ip}:\n{login_banner}")
                dt_fp, dn_fp = self._match_fingerprints(login_banner)
                if dt_fp:
                    logging.info(
                        f"[IDENTIFY] Post-login banner matched on {ip}: "
                        f"type={dt_fp!r} name={dn_fp!r}"
                    )
                    queue.put(f"[IDENTIFY] {dn_fp} ({dt_fp}) at {ip}\n")
                    # Keep the SSH transport alive for paramiko-based scripts
                    # (SAR, IXR, Smartoptics DCP) so they can skip re-login.
                    # 1830 scripts use Telnet/spawn so they cannot reuse it.
                    self._identified_credentials = (username, password)
                    if dt_fp.lower() != "1830":
                        _keep_client = True
                        self._identified_client = ssh_client
                    return dt_fp, dn_fp

            # No banner fingerprint match — probe with vendor ident commands.
            for command in self._IDENT_COMMANDS:
                if should_stop():
                    queue.put(f"[ABORT] SSH identification cancelled for {ip}.\n")
                    return None, None
                queue.put(f"Executing command: {command}\n")
                session.send(command + "\n")
                if sleep_with_abort(2):
                    queue.put(f"[ABORT] SSH identification cancelled for {ip}.\n")
                    return None, None
                output = self._drain_shell(session, idle_seconds=1.0, max_wait=6.0)
                logging.debug(f"[IDENTIFY] SSH output from {ip}:\n{output}")
                queue.put(f"SSH response from {ip}:\n{output}\n")

                # Vendor-agnostic fingerprint scan against the command
                # response. Catches devices that self-identify in their
                # post-login banner / first command echo even though the
                # Nokia-style ident commands themselves errored out.
                dt_fp, dn_fp = self._match_fingerprints(output)
                if dt_fp:
                    logging.info(
                        f"[IDENTIFY] Output fingerprint matched on {ip}: "
                        f"type={dt_fp!r} name={dn_fp!r}"
                    )
                    queue.put(f"[IDENTIFY] {dn_fp} ({dt_fp}) at {ip}\n")
                    self._identified_credentials = (username, password)
                    if dt_fp.lower() != "1830":
                        _keep_client = True
                        self._identified_client = ssh_client
                    return dt_fp, dn_fp

                device_type, device_name = self.parse_device_info(output, queue)
                if device_type:
                    self._identified_credentials = (username, password)
                    if device_type.lower() != "1830":
                        _keep_client = True
                        self._identified_client = ssh_client
                    return device_type, device_name
                # Generic 1830 fingerprint fallback (matches "1830" anywhere
                # in the output — uses no word boundaries because "_" is a
                # word char in regex and prompts like "LAB_1830-PSS8#"
                # would otherwise be missed).
                if re.search(r"1830", output):
                    return "1830", "Nokia 1830"
            return None, None
        finally:
            # Close the identification shell channel regardless.
            if session is not None:
                try:
                    session.close()
                except (OSError, paramiko.SSHException):
                    pass
            # Close the SSH transport only when identification failed or the device
            # is a Nokia 1830 (whose scripts use Telnet/spawn, not paramiko).
            # When _keep_client is True the transport is stored on
            # self._identified_client and will be closed by the script after use.
            if not _keep_client:
                try:
                    ssh_client.close()
                except (OSError, paramiko.SSHException):
                    pass

    def _identify_via_telnet_1830(self, ip: str, queue: Queue,
                                  should_stop: Callable[[], bool],
                                  sleep_with_abort: Callable[[float], bool]
                                  ) -> Tuple[Optional[str], Optional[str]]:
        """Fast Telnet probe matching the Nokia 1830's three-stage login.

        Sequence (port 23):  ``login: cli`` → ``Username: <u>`` → ``Password: <p>``
        → ``show general detail`` → parse Name + System Description.

        Bypasses the Telnet policy allowlist (this is a short-lived
        identification probe — once we positively identify a 1830 we
        auto-add it to the allowlist so subsequent script runs succeed
        without operator action).

        Returns ``(None, None)`` on any failure so the caller can fall
        through to the SSH-based identification path. Total budget is
        kept under ~5s for non-1830 hosts so we don't slow down
        identification of the rest of the fleet.
        """
        username, password = get_credentials()
        creds_to_try: List[Tuple[str, str]] = [(username, password)]
        for u, p in (get_default_credentials_to_try() or []):
            if (u, p) not in creds_to_try:
                creds_to_try.append((u, p))

        for cred_idx, (u, p) in enumerate(creds_to_try):
            if should_stop():
                return None, None
            tn = None
            try:
                logging.info(
                    f"[1830-TELNET] Probing {ip} (cred {cred_idx + 1}/{len(creds_to_try)}: {u})"
                )
                queue.put(f"[1830-TELNET] Probing {ip} via Telnet (user={u})\n")
                tn = Telnet(ip, port=23, timeout=3,
                            bypass_policy=True,
                            purpose="nokia-1830-identification")

                # Three-stage Nokia 1830 login
                login_banner = tn.read_until(b"login: ", timeout=3)
                if b"login:" not in login_banner.lower():
                    logging.info(
                        f"[1830-TELNET] {ip} did not present 'login:' prompt — not a 1830"
                    )
                    return None, None
                tn.write(b"cli\n")
                tn.read_until(b"Username: ", timeout=3)
                tn.write(u.encode("ascii", errors="ignore") + b"\n")
                tn.read_until(b"Password: ", timeout=3)
                tn.write(p.encode("ascii", errors="ignore") + b"\n")
                if sleep_with_abort(1.0):
                    return None, None

                login_resp = tn.read_very_eager().decode("ascii", errors="ignore")
                if ("Login incorrect" in login_resp or "invalid" in login_resp.lower()
                        or "denied" in login_resp.lower()):
                    logging.warning(
                        f"[1830-TELNET] Telnet login rejected for {ip} as {u}"
                    )
                    queue.put(f"[1830-TELNET] Login rejected for {ip} as {u}\n")
                    try: tn.close()
                    except Exception: pass
                    continue  # try next credential

                # Some 1830s present a Y/n acknowledgement banner; auto-accept.
                if re.search(r"(?i)\(\s*y\s*/\s*n\s*\)|acknowledge", login_resp):
                    tn.write(b"y\n")
                    if sleep_with_abort(0.5):
                        return None, None
                    tn.read_very_eager()

                AuthLockout.register_success(ip)

                # Run the 1830-native identification command
                logging.info(f"[1830-TELNET] Sending 'show general detail' to {ip}")
                queue.put(f"[1830-TELNET] Sending 'show general detail' to {ip}\n")
                tn.write(b"show general detail\n")
                if sleep_with_abort(2.0):
                    return None, None
                ident_out = tn.read_until(b"#", timeout=8).decode("ascii", errors="ignore")
                # Drain any trailing data
                ident_out += tn.read_very_eager().decode("ascii", errors="ignore")
                try:
                    tn.write(b"exit\n")
                except Exception:
                    pass

                logging.debug(f"[1830-TELNET] Response from {ip}:\n{ident_out}")
                queue.put(f"[1830-TELNET] Response from {ip}:\n{ident_out}\n")

                name_m = re.search(r"^\s*Name\s*:\s*(\S.*?)\s*$",
                                   ident_out, re.MULTILINE)
                desc_m = re.search(r"^\s*System\s+Description\s*:\s*(\S.*?)\s*$",
                                   ident_out, re.MULTILINE)
                if not name_m and not desc_m and "1830" not in ident_out:
                    logging.info(
                        f"[1830-TELNET] {ip} login worked but no 1830 fingerprint in "
                        f"response — falling through to SSH identification"
                    )
                    return None, None

                hostname = name_m.group(1) if name_m else "Nokia 1830"
                system_desc = desc_m.group(1) if desc_m else ""

                # Refine the device type based on the platform string in the
                # system description so PSIM and PSI route to their dedicated
                # scripts instead of the generic 1830 path.
                if re.search(r"\bPSIM\b", system_desc, re.IGNORECASE):
                    refined_type = "psim"
                elif re.search(r"\bPSI\b", system_desc, re.IGNORECASE):
                    refined_type = "psi"
                else:
                    refined_type = "1830"

                logging.info(
                    f"[1830-TELNET] Identified {ip}: name={hostname!r} "
                    f"desc={system_desc!r} -> type={refined_type}"
                )
                queue.put(f"[1830-TELNET] {hostname} — {system_desc} ({refined_type})\n")

                # Auto-allowlist for the script run that will follow.
                if not is_telnet_allowed(ip):
                    try:
                        add_telnet_allowlist(
                            ip, reason=f"auto: identified as Nokia 1830 ({hostname})"
                        )
                        queue.put(f"[1830-TELNET] {ip} added to Telnet allowlist\n")
                    except Exception as ae:
                        logging.warning(
                            f"[1830-TELNET] Could not auto-allowlist {ip}: {ae}"
                        )

                return refined_type, hostname

            except TelnetPolicyError as pe:
                # bypass_policy=True should prevent this, but be defensive
                logging.debug(f"[1830-TELNET] Policy refused probe for {ip}: {pe}")
                return None, None
            except (OSError, EOFError, socket.timeout) as e:
                logging.info(
                    f"[1830-TELNET] Probe failed for {ip} ({u}): {friendly_error(e)}"
                )
                # Connection refused / timeout / EOF → not reachable as 1830 Telnet;
                # don't keep trying other credentials, fall through to SSH path.
                return None, None
            finally:
                if tn is not None:
                    try: tn.close()
                    except OSError: pass

        return None, None

    def identify_device(
        self,
        ip: str,
        queue: Queue,
        output_screen: Optional[Any],
        stop_callback: Optional[Callable[[], bool]] = None,
        explicit_credentials: Optional[Tuple[str, str]] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Attempt to identify the device at *ip*.

        Tries SSH banner inspection first, then SSH login, then Telnet.
        Progress messages are placed on *queue* for the GUI to display.

        Args:
            explicit_credentials: Optional (user, pass) pair that overrides
                the saved primary credentials. Used when re-running identify
                after a user-credential prompt drained from the pause queue.

        Returns:
            (device_type, device_name) — both None if identification fails.

        Raises:
            CredentialPromptRequired: If default credentials are exhausted
                and no explicit credentials were supplied. The caller (GUI
                worker) is expected to park this IP for a main-thread
                credential prompt and retry.
        """
        # Clear any client / credentials kept from a previous call.
        self._identified_client = None
        self._identified_credentials = None

        if explicit_credentials and explicit_credentials[0]:
            username, password = explicit_credentials
            # Skip default-rotation entirely on the retry path: the user just
            # told us what to use, so a single failure is final.
            reset_auth_attempt(ip)
            _AUTH_PROMPTED[ip] = True
            # The default-cred cycle that triggered this manual prompt almost
            # certainly tripped AuthLockout (3+ failures in a row → ~10 min
            # cooldown). Blocking the user's explicit retry behind that
            # cooldown is wrong: they've manually intervened with fresh
            # credentials, so clear lockout state for this IP before retrying.
            try:
                AuthLockout.reset(ip)
                logging.info(
                    f"[AUTH] Cleared lockout for {ip} (operator-supplied credentials)"
                )
            except Exception:
                logging.debug(f"[AUTH] AuthLockout.reset failed for {ip}", exc_info=True)
        else:
            username, password = get_credentials()
            reset_auth_attempt(ip)

        def should_stop():
            return bool(stop_callback and stop_callback())

        def sleep_with_abort(seconds: float, interval: float = 0.1) -> bool:
            end_time = time.time() + seconds
            while time.time() < end_time:
                if should_stop():
                    return True
                time.sleep(min(interval, end_time - time.time()))
            return should_stop()

        if should_stop():
            queue.put(f"[ABORT] Identification cancelled for {ip}.\n")
            return None, None

        logging.info(f"[IDENTIFY] Trying SSH identification for {ip}")

        try:
            transport = paramiko.Transport((ip, 22))
            transport.start_client(timeout=config.SSH_CONNECT_TIMEOUT)

            banner_holder = {}
            def handler(title, instructions, prompt_list):
                banner_holder['banner'] = transport.get_banner()
                return [''] * len(prompt_list)

            try:
                transport.auth_interactive('dummyuser', handler)
            except AuthenticationException:
                pass

            banner = banner_holder.get('banner')
            decoded = banner.decode('utf-8', errors='ignore') if banner else ''
            logging.debug(f"[IDENTIFY] Banner from {ip}:\n{decoded}")
            queue.put(f"[DEBUG] SSH Auth Banner from {ip}:\n{decoded}\n")

            # Vendor-agnostic banner fingerprint scan. Any device whose
            # SSH auth banner self-identifies (Ciena 6500, Smartoptics
            # DCP, etc.) gets recognised here without further probing.
            dt_fp, dn_fp = self._match_fingerprints(decoded)
            if dt_fp:
                logging.info(
                    f"[IDENTIFY] Banner fingerprint matched on {ip}: "
                    f"type={dt_fp!r} name={dn_fp!r}"
                )
                queue.put(f"[IDENTIFY] Device identified from banner: {dn_fp} ({dt_fp}) at {ip}\n")
                transport.close()
                return dt_fp, dn_fp

            transport.close()
        except (OSError, paramiko.SSHException) as e:
            logging.warning(f"[IDENTIFY] SSH banner scan failed for {ip}: {e}")
            queue.put(f"[WARNING] SSH banner scan failed for {ip}: {friendly_error(e)}. Proceeding with login...\n")

        # SSH fallback — try primary creds, then rotate through every default
        # credential entry, then prompt the user. Each iteration goes through
        # _ssh_identify_attempt which (for username "cli") performs the
        # Nokia 1830 two-stage shell login.
        try:
            try:
                AuthLockout.check(ip)
            except AuthLockoutError as le:
                logging.warning(f"[AUTH] {le}")
                queue.put(f"[AUTH_LOCKED] {ip} locked for {le.retry_after:.0f}s; skipping.\n")
                return None, None

            current_user, current_pass = username, password

            # Banner-driven credential preference. Peek the SSH pre-auth
            # banner; if it self-identifies the device family, override
            # the first credential we try so we don't waste rotation
            # attempts (and lockout budget) on the wrong account. Falls
            # back silently when no banner is sent or no fingerprint
            # matches — the regular rotation handles that case.
            banner_identified = False
            banner_label = ""
            banner = self._peek_ssh_banner(ip)
            if banner:
                logging.debug(f"[BANNER] {ip}: {banner[:300]!r}")
                bdev_type, bdev_name = self._match_fingerprints(banner)
                if bdev_type:
                    banner_identified = True
                    banner_label = bdev_name or bdev_type
                    preferred = self._banner_preferred_creds(bdev_type)
                    if preferred and preferred != (current_user, current_pass):
                        logging.info(
                            f"[BANNER] {ip} self-identifies as {banner_label} — "
                            f"trying {preferred[0]} first"
                        )
                        queue.put(
                            f"[BANNER] {ip} looks like {banner_label}; "
                            f"trying {preferred[0]} credentials first\n"
                        )
                        current_user, current_pass = preferred

            # When the banner authoritatively identifies the device, skip
            # the generic default-credential rotation on auth failure: the
            # other defaults (admin/admin against an RLS, cli/admin against
            # a 6500, etc.) cannot succeed and only burn lockout budget on
            # the device. Jump straight to the user prompt instead.
            tried_defaults = bool(banner_identified)
            tried_defaults_logged_banner = False
            ssh_failures_logged = 0  # so we can refund them if Telnet is going to be tried
            # Hard cap: primary + N defaults + 1 user prompt. Bound the loop
            # so a misconfigured device can't spin forever.
            max_attempts = 2 + len(get_default_credentials_to_try() or [])
            for attempt in range(max_attempts):
                if should_stop():
                    queue.put(f"[ABORT] SSH identification cancelled for {ip}.\n")
                    return None, None
                # Nokia 1830 quirk: PuTTY/OpenSSH reach this device's `cli`
                # account via auth_none or keyboard-interactive with empty
                # responses (no real SSH password). paramiko's standard
                # connect() with password= fails. We handle the cli flow
                # below in _ssh_identify_attempt with a method cascade,
                # so DON'T skip SSH here anymore.

                try:
                    AuthLockout.check(ip)
                except AuthLockoutError as le:
                    queue.put(f"[AUTH_LOCKED] {ip} locked for {le.retry_after:.0f}s; skipping.\n")
                    return None, None
                try:
                    device_type, device_name = self._ssh_identify_attempt(
                        ip, current_user, current_pass, queue, should_stop, sleep_with_abort
                    )
                    if device_type:
                        return device_type, device_name
                    # Auth succeeded but no useful response. Don't break — continue
                    # rotating credentials. A different account (e.g. "cli" for the
                    # Nokia 1830) may use a completely different SSH code path that
                    # DOES return identification data, even when a standard admin
                    # account connected but produced no output.
                except AuthenticationException as ae:
                    AuthLockout.register_failure(ip)
                    ssh_failures_logged += 1
                    logging.warning(f"[IDENTIFY] SSH auth failed for {ip} (attempt {attempt + 1}): {ae}")
                    queue.put(f"SSH authentication failed for {ip}\n")
                except paramiko.SSHException as se:
                    # Transport-layer error (closed session, EOF, kex failure).
                    # Treat as a soft failure for *this credential* so the loop
                    # rotates to the next default with a fresh Transport rather
                    # than aborting identification entirely.
                    AuthLockout.register_failure(ip)
                    ssh_failures_logged += 1
                    logging.warning(
                        f"[IDENTIFY] SSH transport error for {ip} (attempt {attempt + 1}): {se}"
                    )
                    queue.put(f"SSH error for {ip}: {friendly_error(se)} — trying next credential\n")
                except (OSError, socket.timeout) as conn_err:
                    # Genuine network error (DNS, refused, timeout). No point
                    # cycling credentials — re-raise to the outer handler.
                    logging.warning(f"[IDENTIFY] SSH connect error for {ip}: {conn_err}")
                    queue.put(f"SSH connect error for {ip}: {friendly_error(conn_err)}\n")
                    raise

                if banner_identified and not tried_defaults_logged_banner:
                    queue.put(
                        f"[BANNER] {ip} was identified as {banner_label}; "
                        f"skipping generic default-credential rotation and "
                        f"requesting credentials directly.\n"
                    )
                    logging.info(
                        f"[BANNER] {ip}: banner-identified as {banner_label}; "
                        f"bypassing default-credential rotation"
                    )
                    tried_defaults_logged_banner = True
                next_user, next_pass = handle_credential_failure(
                    ip, queue, tried_defaults=tried_defaults
                )
                if not (next_user and next_pass):
                    # Out of defaults AND prompt was already attempted for
                    # this IP earlier in the run — drop to Telnet fallback.
                    break
                # If we just dispensed the user-prompted credentials, mark
                # tried_defaults so the next failure goes straight to (None, None)
                # rather than recycling the prompt path.
                if _AUTH_DEFAULT_INDEX.get(ip, 0) >= len(get_default_credentials_to_try() or []):
                    tried_defaults = True
                current_user, current_pass = next_user, next_pass
                logging.info(f"[IDENTIFY] Retrying SSH to {ip} with credentials: {current_user}")
                queue.put(f"Retrying SSH connection to {ip}...\n")

            # SSH cycle finished without identifying the device. Try Telnet
            # as a fallback for devices (like the Nokia 1830) that use a
            # three-stage shell login not reachable via standard SSH auth.
            if ssh_failures_logged:
                AuthLockout.refund(ip, ssh_failures_logged)
                logging.info(
                    f"[AUTH] Refunded {ssh_failures_logged} SSH auth failure(s) for {ip}"
                )
        except CredentialPromptRequired:
            raise
        except AuthenticationException:
            queue.put(f"SSH authentication failed for {ip}; identification could not complete.\n")
        except (OSError, socket.timeout, paramiko.SSHException) as e:
            logging.warning(f"[IDENTIFY] SSH error for {ip}: {e}")
            queue.put(f"SSH connection failed for {ip}: {friendly_error(e)}\n")

        if should_stop():
            queue.put(f"[ABORT] Identification cancelled for {ip}.\n")
            return None, None

        # Telnet fallback — targeting the Nokia 1830 three-stage login
        # (login: cli → Username: → Password:).
        logging.info(f"[IDENTIFY] SSH identification failed; trying Nokia 1830 Telnet probe for {ip}")
        try:
            dt, dn = self._identify_via_telnet_1830(
                ip, queue, should_stop, sleep_with_abort
            )
            if dt:
                return dt, dn
        except (OSError, EOFError) as e:
            logging.debug(f"[IDENTIFY] 1830 Telnet probe raised for {ip}: {e}")

        return None, None

    @staticmethod
    def parse_device_info(output: str, queue: Queue) -> Tuple[Optional[str], Optional[str]]:
        """Parse raw CLI output and extract device type and name.

        Searches for Nokia-style ``Type : ...`` / ``Name : ...`` fields and
        Ciena-style ``Product : ...`` / ``Name: ...`` fields.

        Returns:
            (device_type, device_name) — either or both may be None.
        """
        logging.debug(f"[PARSE] Raw output:\n{output}")
        queue.put(f"Parsing device output:\n{output}\n")
        try:
            type_match = re.search(r"Type\s+:\s+(.+)", output)
            name_match = re.search(r"Name\s+:\s+(.+)", output)
            product_match = re.search(r"Product\s+:\s+(\S+)", output)
            product_name_match = re.search(r"Name:\s+(.+)", output)
            shelf_type_match = re.search(r"Shelf\s+type\s*:\s*(.+)", output, re.IGNORECASE)
            prompt_name_match = re.search(
                r"^\s*([A-Za-z0-9._-]+)#\s*show\s+general\s+system-identification",
                output,
                re.IGNORECASE | re.MULTILINE,
            )

            shelf_type = shelf_type_match.group(1).strip() if shelf_type_match else ""
            product = product_match.group(1).strip() if product_match else None
            # PSIM detection: System Description, shelf type "PSI-M", or an
            # inventory row leading with "<n> PSI-M".
            psim_hit = (
                re.search(r"\bNokia\s+1830\s+PSIM\b", output, re.IGNORECASE)
                or re.search(r"\bPSI-M\b", shelf_type, re.IGNORECASE)
                or re.search(r"^\s*\d+\s+PSI-M\b", output, re.MULTILINE | re.IGNORECASE)
            )

            if psim_hit:
                device_type = "psim"
                queue.put(f"[IDENTIFY] PSIM shelf detected\n")
                logging.info("[IDENTIFY] PSIM shelf detected")
            elif re.search(r"\bPSI-(4L|8L)\b", shelf_type, re.IGNORECASE):
                device_type = "psi"
                queue.put(f"[IDENTIFY] PSI shelf type detected: {shelf_type}\n")
                logging.info(f"[IDENTIFY] PSI shelf type detected: {shelf_type}")
            elif type_match:
                device_type = type_match.group(1).strip()
            else:
                device_type = product

            if name_match:
                device_name = name_match.group(1).strip()
            elif product_name_match:
                device_name = product_name_match.group(1).strip()
            elif prompt_name_match:
                device_name = prompt_name_match.group(1).strip()
            elif device_type == "psim":
                device_name = "Nokia 1830 PSIM"
            elif device_type == "psi":
                device_name = "Nokia PSI"
            else:
                device_name = None

            if device_type:
                queue.put(f"Parsed Device Info:\n - Type: {device_type}\n - Name: {device_name}\n")
                logging.debug(f"[PARSE] Parsed Type: {device_type}, Name: {device_name}")
            else:
                logging.warning(f"[PARSE] Could not identify device type from output. Raw output (first 200 chars): {output[:200]!r}")
                queue.put(f"[WARNING] Could not identify device type from response. Device may be unsupported, or CLI output format is unexpected.\n")
            return device_type, device_name

        except (TypeError, re.error) as e:
            logging.exception("[PARSE] Error parsing device info")
            queue.put(f"Error parsing device info: {friendly_error(e)}\n")
            return None, None


class ScriptSelector:
    """Map an identified device type to the appropriate BaseScript subclass and instantiate it."""

    # Map of normalized device types to script classes
    _device_type_to_script = {
        '7705 sar-8 v2': 'scripts.Nokia_SAR',
        '7250 ixr-r6': 'scripts.Nokia_IXR',
        '7250 ixr-r6d': 'scripts.Nokia_IXR',
        '1830': 'scripts.Nokia_1830',
        '1830 psi': 'scripts.Nokia_PSI',
        'psi': 'scripts.Nokia_PSI',
        '1830 psim': 'scripts.Nokia_PSIM',
        'psim': 'scripts.Nokia_PSIM',
        '6500': 'scripts.Ciena_6500',
        'ciena 6500 optical': 'scripts.Ciena_6500',
        'ciena 6500 rls': 'scripts.Ciena_RLS',
        'rls': 'scripts.Ciena_RLS',
        'dcp': 'scripts.Smartoptics_DCP',
        'smartoptics dcp': 'scripts.Smartoptics_DCP',
    }

    # F025: explicit allowlist used as a last-line check before __import__.
    # Pinned at class-definition time so runtime mutation of the dict above
    # cannot smuggle a new module name into the importer.
    _ALLOWED_SCRIPT_MODULES = frozenset(_device_type_to_script.values())

    def _is_device_supported(self, normalized_type: str) -> bool:
        """Return True if the device type is supported by an available script."""
        return normalized_type in self._device_type_to_script

    def select_script(self, device_type: Optional[str], ip_address: str, connection_type: str = 'ssh', stop_callback: Optional[Callable[[], bool]] = None, credentials: Optional[Tuple[str, str]] = None) -> Optional[BaseScript]:
        """Return an instantiated script for *device_type* at *ip_address*, or None if unknown."""
        logging.debug(f"[SCRIPT SELECTOR] Selecting script for IP: {ip_address}, Device Type: {device_type}, Connection: {connection_type}")

        normalized_type = (device_type or '').strip().lower()
        if not normalized_type:
            logging.debug(f"[SCRIPT SELECTOR] No device type detected for IP {ip_address}; skipping script selection.")
            return None

        # Validate device type is supported
        if not self._is_device_supported(normalized_type):
            logging.debug(f"[SCRIPT SELECTOR] Device type '{normalized_type}' not supported for IP {ip_address}")
            return None

        try:
            script_module = self._device_type_to_script[normalized_type]
            # F025: defense-in-depth — never let a value outside the static
            # allowlist reach __import__, even if _device_type_to_script
            # is somehow mutated at runtime.
            if script_module not in self._ALLOWED_SCRIPT_MODULES:
                logging.error(
                    f"[SCRIPT SELECTOR] Refusing non-allowlisted module: {script_module!r}"
                )
                return None
            # Dynamically import the module
            parts = script_module.rsplit('.', 1)
            module = __import__(script_module, fromlist=[parts[-1]])
            script_class = module.Script

            logging.info(f"[SCRIPT SELECTOR] Matched '{normalized_type}' to script: {script_class.__name__}")
            # Prefer credentials passed in by the caller (the ones that
            # actually authenticated during identification). Falls back
            # to the user's saved primary credentials when none were
            # provided (e.g. parked-IP retry after manual entry, or a
            # Telnet identification path that didn't capture creds).
            if credentials and credentials[0] and credentials[1]:
                _username, _password = credentials
                logging.info(
                    f"[SCRIPT SELECTOR] Reusing identification credentials "
                    f"({_username}) for {ip_address}"
                )
            else:
                _username, _password = get_credentials()
            return script_class(
                connection_type=connection_type,
                ip_address=ip_address,
                username=_username,
                password=_password,
                db_cache=get_cache(),
                command_tracker=get_tracker(),
                stop_callback=stop_callback,
            )

        except ImportError as e:
            logging.exception(f"[SCRIPT SELECTOR] Missing optional dependency for device '{device_type}' at IP {ip_address}")
            return None
        except (AttributeError, TypeError, ValueError) as e:
            logging.exception(f"[SCRIPT SELECTOR] Failed to select script for device '{device_type}' at IP {ip_address}")
            return None