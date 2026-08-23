"""
Common helper functions for ATLAS.
"""

import base64
import getpass
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple, Union
from pathlib import Path

try:
    import paramiko
except ImportError:  # paramiko optional in stripped-down environments
    paramiko = None  # type: ignore[assignment]


def restrict_path_to_owner(path: Union[str, Path], is_dir: bool = False) -> bool:
    """Restrict filesystem access on *path* to the current OS user only.

    On Windows, breaks ACL inheritance and grants full control only to the
    current user (and SYSTEM, so OS services can still back things up).
    On POSIX, falls back to ``chmod 0o600`` (or ``0o700`` for directories).

    Used to harden credential files (.creds_key, credentials_config.json) and
    the log directory so other local accounts cannot read encrypted secrets,
    decryption keys, or device-output transcripts. Best-effort: returns False
    on failure but never raises — the caller should not abort startup just
    because hardening could not be applied.
    """
    p = Path(path)
    if not p.exists():
        return False

    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or getpass.getuser()
            if not user:
                return False
            perm = "(OI)(CI)(F)" if is_dir else "(F)"
            # /inheritance:r removes inherited ACEs; /grant:r replaces existing
            # entries for the principal. SYSTEM is preserved so backup/AV can
            # still touch the file. No window flash on GUI app.
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            result = subprocess.run(
                [
                    "icacls",
                    str(p),
                    "/inheritance:r",
                    "/grant:r",
                    f"{user}:{perm}",
                    "/grant:r",
                    f"SYSTEM:{perm}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                logging.warning(
                    "[ACL] icacls hardening failed for %s (rc=%s): %s",
                    p, result.returncode, (result.stderr or "").strip()[:200],
                )
                return False
            return True
        else:
            mode = 0o700 if is_dir else 0o600
            os.chmod(p, mode)
            return True
    except Exception as e:
        logging.warning("[ACL] Could not restrict permissions on %s: %s", p, e)
        return False



def get_data_dir() -> Path:
    """Return the path to the bundled data directory (templates, DB seeds, etc.).

    When running as a PyInstaller onedir bundle, ``--add-data "data:data"``
    places all data files inside ``sys._MEIPASS/data/``.  At dev-time the
    data folder lives next to the project root.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / "data"  # type: ignore[attr-defined]
    return get_project_root() / "data"


def get_project_root() -> Path:
    """
    Get the LAMIS project root directory.
    
    The project root is identified by the presence of main.py and config.py.
    Searches upward from the utils directory until found.
    
    Returns:
        Path to project root
        
    Raises:
        FileNotFoundError: If project root cannot be determined
    """
    # PyInstaller frozen bundle: root is the folder containing the executable
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent

    # Start from utils directory and go up
    utils_dir = Path(__file__).parent
    current = utils_dir.parent
    
    # Look for config.py and main.py as markers
    while current != current.parent:  # Stop at filesystem root
        if (current / "config.py").exists() and (current / "main.py").exists():
            return current
        current = current.parent
    
    raise FileNotFoundError(
        "Could not determine ATLAS project root. "
        "Ensure main.py and config.py exist in the project root."
    )


def get_database_path() -> Path:
    """
    Get the path to the LAMIS inventory database file.
    
    Resolves the database path relative to the project root, ensuring
    consistency across all modules.
    
    Returns:
        Path to network_inventory.db (may not exist, but path is resolved)
        
    Example:
        >>> db_path = get_database_path()
        >>> db_path.name
        'network_inventory.db'
    """
    # Store the database in %APPDATA%\ATLAS so it's writable when installed
    # in Program Files (which is read-only for standard users).
    import os, shutil, sys
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    db_dir = Path(app_data) / "ATLAS"
    db_dir.mkdir(parents=True, exist_ok=True)
    dest = (db_dir / "network_inventory.db").resolve()

    # If the destination DB is missing or empty, seed it from the bundled copy.
    if not dest.exists() or dest.stat().st_size == 0:
        # When frozen (PyInstaller), data/ is under sys._MEIPASS; otherwise
        # it's relative to this file's project root.
        if getattr(sys, "frozen", False):
            source = Path(sys._MEIPASS) / "data" / "network_inventory.db"
        else:
            source = Path(__file__).parent.parent / "data" / "network_inventory.db"
        if source.exists():
            shutil.copy2(str(source), str(dest))

    return dest


def get_logs_dir() -> Path:
    """Return the directory ATLAS writes rotating run logs into.

    Lives at ``%APPDATA%\\ATLAS\\logs`` so it's writable when ATLAS is
    installed under ``Program Files`` (which is read-only for standard
    users). The directory is created on demand so callers can rely on
    it existing even before the file logger has written its first line.
    """
    import os
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    log_dir = Path(app_data) / "ATLAS" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir.resolve()


def get_known_hosts_path() -> Path:
    """Return the path to the ATLAS SSH known_hosts file, creating it if needed.

    Stored in ``%APPDATA%\\ATLAS\\known_hosts`` so it persists across sessions and
    is writable without elevated privileges.  Use this with paramiko's
    ``load_host_keys`` / ``save_host_keys`` to get Trust-On-First-Use semantics:
    unknown hosts are accepted and stored the first time, then verified on every
    subsequent connection.
    """
    import os
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    atlas_dir = Path(app_data) / "ATLAS"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    known_hosts = atlas_dir / "known_hosts"
    if not known_hosts.exists():
        known_hosts.touch()
    return known_hosts.resolve()


def get_user_prefs_path() -> Path:
    """Return the path to the JSON file ATLAS uses for per-user UI
    preferences (last-used directories, window layout, etc.).

    Stored alongside the database / logs / known_hosts under
    ``%APPDATA%\\ATLAS`` so it survives upgrades and is writable
    without elevated privileges. The file itself is *not* created here
    -- callers use :func:`load_user_prefs` / :func:`save_user_prefs`.
    """
    import os
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    atlas_dir = Path(app_data) / "ATLAS"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    return (atlas_dir / "prefs.json").resolve()


def load_user_prefs() -> dict:
    """Return ATLAS user preferences as a dict, or an empty dict if the
    file is missing / unreadable. Never raises; callers can rely on a
    dict always coming back so the first-run path is the same as the
    file-corrupt path."""
    import json
    import logging as _logging
    path = get_user_prefs_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:  # JSONDecodeError, OSError, permission, etc.
        _logging.warning(f"[PREFS] Could not read {path}: {exc}")
    return {}


def save_user_prefs(prefs: dict) -> bool:
    """Atomically persist *prefs* to the user-prefs JSON file. Returns
    True on success, False if the write failed (callers should keep
    running -- a failed pref save is never fatal)."""
    import json
    import logging as _logging
    import os
    path = get_user_prefs_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(prefs, handle, indent=2, sort_keys=True)
        # os.replace is atomic on Windows (same volume) and POSIX -- the
        # consumer either sees the old file or the new one, never a
        # half-written one.
        os.replace(tmp, path)
        return True
    except Exception as exc:
        _logging.warning(f"[PREFS] Could not write {path}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def get_desktop_dir() -> Path:
    """Return the OS Desktop directory for the current user.

    Falls back to ``~/Desktop`` if the registry-style ``USERPROFILE``
    layout isn't usable (e.g. test envs with no Desktop). Never raises
    -- callers can rely on a writable Path being returned even if the
    real desktop has been redirected by an enterprise GPO.
    """
    import os
    candidates = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Desktop")
    # OneDrive-redirected Desktop is what most enterprise Windows users
    # actually see; try it second so an explicit USERPROFILE/Desktop
    # wins when it's there, but we still land somewhere usable when
    # the local Desktop was redirected.
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        candidates.append(Path(onedrive) / "Desktop")
    candidates.append(Path.home() / "Desktop")
    for cand in candidates:
        if cand.exists() and cand.is_dir():
            return cand.resolve()
    # Last-resort: return ~/Desktop even if it doesn't exist; the
    # caller can choose to mkdir or fall through to ~.
    return (Path.home() / "Desktop").resolve()


def clear_known_host_entry(
    ip: str,
    path: Optional[Union[str, Path]] = None,
) -> bool:
    """Remove any saved SSH host keys for *ip* from the ATLAS known_hosts file.

    Returns ``True`` when a key was removed, ``False`` when nothing matched
    or the file was missing. Used by LAN-mode inventory pulls — the same
    IP (typically the lab management address ``10.0.0.1``) commonly maps
    to different physical devices between runs, so the previously-stored
    TOFU key blocks the next attempt. Calling this at the end of every
    LAN run lets the next attempt accept whatever's at the IP fresh.

    ``path`` defaults to :func:`get_known_hosts_path`. Pass an explicit
    path for tests.

    paramiko's ``HostKeys`` mapping handles both plaintext and hashed
    ``HashKnownHosts``-style entries transparently, so this helper covers
    either format. Falls back to a no-op (returns ``False``) when paramiko
    isn't importable or the file is malformed — never raises.
    """
    try:
        import paramiko
    except Exception:
        return False
    kh_path = Path(path) if path is not None else get_known_hosts_path()
    if not kh_path.exists():
        return False
    try:
        # Pre-scrub: HostKeys.load() aborts on the first bad line, which
        # would leave us unable to look up the IP. scrub_known_hosts is
        # already idempotent so calling it first is safe.
        scrub_known_hosts(kh_path)
        kh = paramiko.HostKeys(filename=str(kh_path))
        if ip not in kh:
            return False
        del kh[ip]
        kh.save(str(kh_path))
        return True
    except Exception:
        logging.debug(
            "Failed to clear known_hosts entry for %s", ip, exc_info=True
        )
        return False


def scrub_known_hosts(path: Optional[Union[str, Path]] = None) -> Tuple[int, int]:
    """Drop unparseable entries from the known_hosts file at *path*.

    paramiko's ``HostKeys.load`` aborts the entire load on the first
    malformed line (truncated base64, mangled keytype, etc.). Any such
    corruption then poisons every subsequent ``save_host_keys`` call —
    the save path reloads before writing, so a single bad line takes
    out unrelated working connections.

    This helper reads the file line-by-line, keeps lines that paramiko's
    own ``HostKeyEntry.from_line`` accepts, and rewrites the file atomically
    when any lines need dropping. Comments and blank lines are preserved.

    Returns ``(kept, dropped)``. A no-op call returns ``(N, 0)``. Safe to
    call repeatedly (idempotent on clean files). Never raises — on I/O
    or paramiko-availability failure it logs and returns ``(0, 0)``.
    """
    if paramiko is None:
        return (0, 0)
    if path is None:
        path = get_known_hosts_path()
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return (0, 0)

    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logging.warning(f"[HOSTKEY] scrub: cannot read {p}: {exc}")
        return (0, 0)

    kept_lines: list[str] = []
    dropped: list[Tuple[int, str]] = []
    for ln_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            # Preserve blank/comment lines verbatim.
            kept_lines.append(line)
            continue
        try:
            # Strip trailing \r so CRLF-terminated files don't choke
            # paramiko's base64 decoder on the last column.
            entry = paramiko.hostkeys.HostKeyEntry.from_line(
                line.rstrip("\r"), ln_no
            )
        except Exception as exc:
            dropped.append((ln_no, line[:80]))
            logging.warning(
                f"[HOSTKEY] scrub: dropping line {ln_no} -- "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        if entry is None:
            # from_line returns None for entries it silently ignores (e.g.
            # disabled lines starting with @cert-authority on some
            # paramiko versions). Preserve them as-is.
            kept_lines.append(line)
            continue
        kept_lines.append(line)

    if not dropped:
        return (len(kept_lines), 0)

    new_text = "\n".join(kept_lines)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"

    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logging.warning(f"[HOSTKEY] scrub: cannot rewrite {p}: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return (len(kept_lines), 0)

    logging.info(
        f"[HOSTKEY] scrub: kept {len(kept_lines)} line(s), "
        f"dropped {len(dropped)} malformed line(s) from {p}"
    )
    return (len(kept_lines), len(dropped))


def safe_load_host_keys(client: Any, path: Union[str, Path]) -> bool:
    """Robust wrapper around ``client.load_host_keys`` that survives a
    corrupt known_hosts file.

    paramiko's stock load aborts on the first malformed line, which then
    breaks every downstream SSH connection until the file is repaired.
    This wrapper:

      1. Attempts the load.
      2. On ``InvalidHostKey``, scrubs the file (drops the bad line)
         and retries once.
      3. On any other failure (missing file, IO error), logs and
         returns ``False``; the caller usually proceeds with an empty
         in-memory key cache.

    Returns ``True`` if the load succeeded (possibly after a scrub).
    Never raises.
    """
    if paramiko is None:
        return False
    p = str(path)
    try:
        client.load_host_keys(p)
        return True
    except (FileNotFoundError, IOError):
        return False
    except paramiko.hostkeys.InvalidHostKey as ihk:
        logging.warning(
            f"[HOSTKEY] load_host_keys hit InvalidHostKey ({ihk}) -- "
            f"scrubbing {p} and retrying"
        )
    except Exception as exc:
        logging.warning(
            f"[HOSTKEY] load_host_keys failed ({type(exc).__name__}: {exc})"
        )
        return False
    try:
        scrub_known_hosts(p)
        client.load_host_keys(p)
        return True
    except Exception as exc:
        logging.warning(
            f"[HOSTKEY] load_host_keys retry failed ({type(exc).__name__}: {exc})"
        )
        return False


# Process-wide lock serializing all writes to the known_hosts file.
# Parallel inventory workers race to call ``save_host_keys`` after a
# successful SSH handshake; without a lock, two threads' writes can
# interleave and produce concatenated, malformed lines (e.g. a row
# missing its newline ending up smashed into the next host's entry).
_KNOWN_HOSTS_WRITE_LOCK = threading.Lock()


def safe_save_host_keys(client: Any, path: Union[str, Path]) -> bool:
    """Atomically persist a paramiko SSHClient's host-key cache.

    Improves on ``client.save_host_keys`` in three ways:

      1. Serialized across threads via a module-level lock so parallel
         workers can't tear the file with interleaved writes.
      2. Writes to a temp file in the same directory, then ``os.replace``
         (atomic on Windows + POSIX) — partial writes never end up
         visible.
      3. On ``InvalidHostKey`` from the underlying load, scrubs the
         existing file (dropping the malformed line), then retries.

    Never raises; logs and returns ``False`` if both attempts fail.
    The in-memory key on ``client`` is unaffected — the active SSH
    session keeps working regardless of save outcome.
    """
    if paramiko is None:
        return False

    target = Path(path)

    def _atomic_write_using_client() -> None:
        """Build the new file contents from the client's current host-key
        cache (merged with whatever's on disk) and atomically replace
        the on-disk file. Inside the lock."""
        # Snapshot the client's current in-memory keys.
        client_keys = client.get_host_keys()

        # Merge with what's already persisted so concurrent threads
        # whose keys aren't in *this* client's cache don't get blown
        # away. If reading the existing file fails, we still write
        # whatever this client has — strictly more than nothing.
        merged = paramiko.HostKeys()
        try:
            merged.load(str(target))
        except Exception:
            pass
        for host, type_map in client_keys.items():
            for ktype, key in type_map.items():
                merged.add(host, ktype, key)

        tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                for host, type_map in merged.items():
                    for ktype, key in type_map.items():
                        f.write(f"{host} {ktype} {key.get_base64()}\n")
            os.replace(tmp, target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    with _KNOWN_HOSTS_WRITE_LOCK:
        try:
            _atomic_write_using_client()
            return True
        except paramiko.hostkeys.InvalidHostKey as exc:
            logging.warning(
                f"[HOSTKEY] atomic save hit InvalidHostKey ({exc}) -- "
                f"scrubbing {target} and retrying"
            )
        except Exception as exc:
            logging.warning(
                f"[HOSTKEY] atomic save failed ({type(exc).__name__}: {exc}) "
                f"-- scrubbing {target} and retrying"
            )

        # Scrub still uses its own atomic write; that's safe inside the
        # lock because the lock is non-reentrant by name but holds the
        # same thread (we don't re-acquire). The scrub itself does its
        # own atomic temp+rename without touching this lock.
        try:
            scrub_known_hosts(target)
            _atomic_write_using_client()
            return True
        except Exception as exc:
            logging.warning(
                f"[HOSTKEY] atomic save retry failed ({type(exc).__name__}: "
                f"{exc}) -- continuing without persisting the new key"
            )
            return False


class SafeAutoAddPolicy:
    """Drop-in replacement for ``paramiko.AutoAddPolicy`` that routes
    persistence through :func:`safe_save_host_keys` instead of
    ``client.save_host_keys``.

    paramiko's stock policy calls the raw save (non-atomic, unlocked,
    and re-loads the file first — which throws on the first malformed
    line). Under parallel workers this both tears the file and breaks
    unrelated working connections. This policy accepts the key in
    memory and persists via our locked + atomic + scrub-on-fail path.
    """

    def __init__(self, known_hosts_path: Optional[Union[str, Path]] = None) -> None:
        self._kh_path = (
            str(known_hosts_path) if known_hosts_path is not None else None
        )

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        # Add to the client's in-memory cache so the current session
        # passes verification.
        client.get_host_keys().add(hostname, key.get_name(), key)
        # Persist if we have a path. If the caller already attached the
        # filename via ``load_host_keys`` we honour that; otherwise fall
        # back to our standard location.
        path = self._kh_path
        if not path:
            path = getattr(client, "_host_keys_filename", None) or str(get_known_hosts_path())
        # IMPORTANT: clear the client's filename binding so paramiko's
        # own internals don't ALSO try to save (it's currently inside
        # client.connect() which would otherwise call the raw
        # save_host_keys after we return).
        try:
            client._host_keys_filename = None
        except Exception:
            pass
        safe_save_host_keys(client, path)


class CredentialFilter(logging.Filter):
    """Redact credential values before records reach any ATLAS log sink.

    Covers ``key=value``, ``key: value``, common CLI ``key value`` forms, and
    authorization bearer headers. The file, console, and GUI handlers all use
    this filter.
    """

    _AUTH_PATTERN = re.compile(
        r"(?i)\b(authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
    )
    _KEY_VALUE_PATTERN = re.compile(
        r"(?i)\b("
        r"password|passwd|secret|community|token|"
        r"api[_ -]?key|shared[_ -]?key|license[_ -]?key"
        r")\b\s*(?:[:=]|\s)\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    )

    @classmethod
    def _redact(cls, value: str) -> str:
        value = cls._AUTH_PATTERN.sub(
            lambda match: match.group(1) + "=[REDACTED]",
            value,
        )
        return cls._KEY_VALUE_PATTERN.sub(
            lambda match: match.group(1) + "=[REDACTED]",
            value,
        )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = self._redact(rendered)
        # The message has already consumed %-style positional/mapping args.
        # Clearing them prevents Formatter from applying the same args twice.
        record.args = ()
        return True


class AuthLockoutError(Exception):
    """Raised when an IP is currently locked out from authentication attempts."""

    def __init__(self, ip: str, retry_after: float):
        self.ip = ip
        self.retry_after = retry_after
        super().__init__(
            f"{ip} is locked out for {retry_after:.0f}s after too many failed auth attempts"
        )


class AuthLockout:
    """Per-IP failed-authentication tracker with backoff and cooldown.

    In-memory only — state resets on app restart. Designed to keep buggy code
    or fat-fingered prompts from locking out admin accounts on devices that
    enforce their own lockout policies.

    Policy:
      * Up to ``MAX_ATTEMPTS`` failures within ``WINDOW`` seconds.
      * Each attempt sleeps ``BACKOFFS[failure_count]`` seconds before being
        allowed to proceed.
      * Once ``MAX_ATTEMPTS`` is reached, ``check`` raises ``AuthLockoutError``
        until ``COOLDOWN`` seconds have elapsed since the last failure.
      * A successful auth (``register_success``) wipes the IP's history.
    """

    # Must be high enough to cover one full credential rotation without
    # tripping mid-cycle. Today that's 1 primary + 4 seeded defaults
    # (admin/admin, cli/admin, su/Ciena123, ADMIN/ADMIN) = 5 SSH probes
    # before the GUI prompts the user; plus a couple of user-prompted
    # retries on top.
    MAX_ATTEMPTS: int = 8
    WINDOW: float = 300.0          # 5-minute sliding window for attempt counting
    BACKOFFS: Tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 30.0, 60.0)
    COOLDOWN: float = 600.0        # 10-minute lockout once MAX_ATTEMPTS is hit

    _failures: Dict[str, Deque[float]] = {}
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def _prune(cls, ip: str, now: float) -> Deque[float]:
        dq = cls._failures.setdefault(ip, deque())
        cutoff = now - cls.WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    @classmethod
    def check(cls, ip: str, sleep: bool = True) -> float:
        """Verify the IP is allowed to attempt auth; sleep the current backoff if needed.

        Args:
            ip: Device IP address.
            sleep: If True (default), sleep the appropriate backoff before returning.

        Returns:
            The number of seconds slept (0 if no backoff applied).

        Raises:
            AuthLockoutError: If the IP is in cooldown.
        """
        now = time.monotonic()
        with cls._lock:
            dq = cls._prune(ip, now)
            count = len(dq)
            if count >= cls.MAX_ATTEMPTS:
                last = dq[-1]
                retry_after = (last + cls.COOLDOWN) - now
                if retry_after > 0:
                    raise AuthLockoutError(ip, retry_after)
                # Cooldown elapsed — reset and allow the next attempt fresh.
                dq.clear()
                count = 0
            backoff = cls.BACKOFFS[min(count, len(cls.BACKOFFS) - 1)] if count else 0.0
        if sleep and backoff > 0:
            logging.info(f"[AUTH] Backoff {backoff:.0f}s before next attempt to {ip} (failure #{count})")
            time.sleep(backoff)
        return backoff if sleep else 0.0

    @classmethod
    def register_failure(cls, ip: str) -> int:
        """Record a failed auth attempt. Returns the new failure count for this IP."""
        now = time.monotonic()
        with cls._lock:
            dq = cls._prune(ip, now)
            dq.append(now)
            count = len(dq)
        logging.warning(f"[AUTH] Auth failure #{count} recorded for {ip}")
        return count

    @classmethod
    def refund(cls, ip: str, count: int = 1) -> int:
        """Roll back ``count`` recent failures for *ip*.

        Used when a probe (e.g. SSH password auth on a device that only
        supports Telnet) failed in a way that shouldn't count against the
        per-IP lockout budget because we're going to try a different
        protocol next. Returns the new failure count.
        """
        if count <= 0:
            return len(cls._failures.get(ip, ()))
        with cls._lock:
            dq = cls._failures.get(ip)
            if not dq:
                return 0
            for _ in range(min(count, len(dq))):
                dq.pop()
            return len(dq)

    @classmethod
    def register_success(cls, ip: str) -> None:
        """Clear the failure history for an IP after a successful auth."""
        with cls._lock:
            cls._failures.pop(ip, None)

    @classmethod
    def reset(cls, ip: Optional[str] = None) -> None:
        """Manually clear lockout state for one IP or all IPs (test helper)."""
        with cls._lock:
            if ip is None:
                cls._failures.clear()
            else:
                cls._failures.pop(ip, None)


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails validation checks."""


# Magic byte signatures used to verify file contents match the declared format.
# We intentionally don't trust filename extensions alone — a malicious file
# named ``report.xlsx`` could actually be a script, an executable, or a
# tampered archive crafted to exploit a parser bug.
_XLSX_MAGIC = b"PK\x03\x04"            # XLSX is a ZIP archive
_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # CFB/OLE2 (legacy XLS)

# Hard upper bound on uploaded file size. xlsx workbooks rarely exceed this in
# this product; anything larger is much more likely to be a zip-bomb attempt
# or accidental misuse than a real device report.
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def validate_uploaded_file(
    file_path: str,
    allowed_kinds: Tuple[str, ...] = ("xlsx", "xls", "csv"),
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> Path:
    """Validate a user-supplied file path before opening it.

    Performs the following safety checks:
      * Path is non-empty and resolvable.
      * Resolved path exists and is a regular file (not a directory or device).
      * Path is not a symlink (avoids tricking the app into opening a file
        outside an expected directory by following a crafted symlink).
      * File size is within ``max_bytes``.
      * Extension is one of the allowed kinds.
      * Magic bytes match the declared extension. CSV is accepted on
        extension alone since CSV has no magic, but null bytes in the first
        4 KB are treated as a binary file and rejected.

    Args:
        file_path: The path returned by ``filedialog.askopenfilename`` or
            similar.
        allowed_kinds: Allowed file kinds. Each entry is one of
            ``"xlsx"``, ``"xls"``, ``"csv"``.
        max_bytes: Maximum acceptable file size in bytes.

    Returns:
        The fully-resolved absolute :class:`pathlib.Path`.

    Raises:
        UploadValidationError: If any check fails. The error message is
            user-facing and safe to display in a messagebox.
    """
    if not file_path or not str(file_path).strip():
        raise UploadValidationError("No file selected.")

    raw = Path(file_path)

    # Reject symlinks BEFORE resolving — a symlink can point anywhere and we
    # don't want our subsequent reads to silently follow it.
    try:
        if raw.is_symlink():
            raise UploadValidationError(
                "Symbolic links are not permitted for uploaded files."
            )
    except OSError as e:
        raise UploadValidationError(f"Could not inspect file: {e}") from e

    try:
        resolved = raw.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise UploadValidationError(f"File does not exist or is unreadable: {e}") from e

    if not resolved.is_file():
        raise UploadValidationError("Selected path is not a regular file.")

    # Belt-and-suspenders: re-check after resolve in case the resolved target
    # is itself a symlink (shouldn't happen with strict=True, but cheap to verify).
    if resolved.is_symlink():
        raise UploadValidationError(
            "Resolved path is a symbolic link, which is not permitted."
        )

    try:
        size = resolved.stat().st_size
    except OSError as e:
        raise UploadValidationError(f"Could not stat file: {e}") from e

    if size <= 0:
        raise UploadValidationError("Selected file is empty.")
    if size > max_bytes:
        raise UploadValidationError(
            f"File is too large ({size:,} bytes). Maximum allowed is "
            f"{max_bytes:,} bytes."
        )

    suffix = resolved.suffix.lower().lstrip(".")
    if suffix not in allowed_kinds:
        raise UploadValidationError(
            f"Unsupported file type '.{suffix}'. "
            f"Allowed: {', '.join('.' + k for k in allowed_kinds)}."
        )

    # Magic-byte sniff so a renamed binary or tampered file can't slip through.
    try:
        with resolved.open("rb") as fh:
            header = fh.read(4096)
    except OSError as e:
        raise UploadValidationError(f"Could not read file: {e}") from e

    if suffix == "xlsx":
        if not header.startswith(_XLSX_MAGIC):
            raise UploadValidationError(
                "File does not appear to be a valid .xlsx workbook "
                "(missing ZIP signature)."
            )
    elif suffix == "xls":
        if not header.startswith(_XLS_MAGIC):
            raise UploadValidationError(
                "File does not appear to be a valid legacy .xls workbook."
            )
    elif suffix == "csv":
        # CSV has no magic; reject obviously-binary content instead.
        if b"\x00" in header:
            raise UploadValidationError(
                "File looks binary, not a CSV. Refusing to load."
            )

    return resolved


# Patterns we strip from user-facing error messages because they reveal
# filesystem layout, internal IDs, or other implementation detail. The full
# exception is always preserved in the log file via ``logging.exception``.
_FS_PATH_RE = re.compile(
    r"""(?ix)
    (?:[A-Za-z]:[\\/]|\\\\|/)            # absolute path prefix (Windows or POSIX)
    [^\s'"<>|?*\r\n]+                     # path body, stop at quotes/whitespace
    """
)
_MEMORY_ADDR_RE = re.compile(r"0x[0-9A-Fa-f]{6,}")
_DEFAULT_FRIENDLY_FALLBACK = "An unexpected error occurred. See log file for details."


def friendly_error(exc: BaseException, fallback: Optional[str] = None) -> str:
    """Return a short, user-safe description of ``exc`` for display in the GUI.

    The returned string omits filesystem paths, memory addresses, and tracebacks
    so messageboxes and on-screen output don't expose internal implementation
    detail to the operator (or any onlookers / shoulder-surfers). Callers are
    expected to *also* call ``logging.exception(...)`` to preserve the full
    traceback in the log file.

    Behavior:
      * Exception class name is included so similar errors can be grouped.
      * Only the first non-empty line of the message is used.
      * Absolute paths are replaced with ``<path>`` and memory addresses
        with ``<addr>``.
      * Result is capped at 200 characters.
      * If sanitization removes everything, ``fallback`` is returned (or a
        generic default).

    Args:
        exc: The caught exception.
        fallback: Custom fallback message. Defaults to a generic one.

    Returns:
        A sanitized message safe to display in user-facing UI.
    """
    fallback = fallback or _DEFAULT_FRIENDLY_FALLBACK
    try:
        raw = str(exc).strip()
    except Exception:
        raw = ""

    if not raw:
        return f"{type(exc).__name__}: {fallback}"

    # Take only the first line — multi-line exception messages often include
    # paths or repr of internal objects on later lines.
    first_line = raw.splitlines()[0].strip()

    sanitized = _FS_PATH_RE.sub("<path>", first_line)
    sanitized = _MEMORY_ADDR_RE.sub("<addr>", sanitized)

    if len(sanitized) > 200:
        sanitized = sanitized[:197] + "..."

    if not sanitized.strip() or sanitized.strip() in ("<path>", "<addr>"):
        return f"{type(exc).__name__}: {fallback}"

    return f"{type(exc).__name__}: {sanitized}"


# F018: filesystem path-traversal defenses ---------------------------------------
class PathTraversalError(ValueError):
    """Raised when a candidate path would escape its allowed base directory."""


# Windows reserved device names (case-insensitive). Files with these names —
# even with extensions — are inaccessible and confuse downstream tools.
_RESERVED_WIN_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def sanitize_filename_component(name: str, *, max_length: int = 80,
                                fallback: str = "Unnamed") -> str:
    """Return a safe, single-segment filename component.

    Strips path separators (``/``, ``\\``), control characters, leading dots,
    and Windows-reserved tokens. Collapses unsafe characters to ``_`` and caps
    length. The result never contains directory separators, so it can be
    concatenated into a path without traversal risk.

    Used to sanitize user-supplied customer / project / filename fragments
    before they flow into ``filedialog.asksaveasfilename(initialfile=...)``
    or into ``os.path.join(...)`` (F018).
    """
    if name is None:
        return fallback
    s = str(name).strip()
    # Drop control chars, slashes, colons, drive markers, and shell meta.
    # Keep alnum + a small set of safe punctuation.
    cleaned = re.sub(r"[^A-Za-z0-9._\-+ ]", "_", s)
    # Collapse runs of underscores/spaces and trim.
    cleaned = re.sub(r"[ _]+", "_", cleaned).strip("._ ")
    if not cleaned:
        return fallback
    # Reject Windows reserved names (compare against stem, case-insensitive).
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED_WIN_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip("._ ") or fallback
    return cleaned


def safe_resolve_under(base: Union[str, Path], candidate: Union[str, Path]) -> Path:
    """Resolve *candidate* and assert it lives under *base*; return the resolved Path.

    F018 hardening: prevents ``..\\..\\..\\Windows\\System32`` style escapes when
    composing a destination path from a user-supplied filename + a chosen
    directory. ``base`` is resolved with ``strict=False`` (it does not have to
    exist yet). Symlinks are followed via ``Path.resolve``.

    Raises ``PathTraversalError`` if ``candidate`` resolves outside ``base``.
    """
    base_path = Path(base).expanduser().resolve(strict=False)
    cand_path = Path(candidate).expanduser()
    if not cand_path.is_absolute():
        cand_path = base_path / cand_path
    cand_resolved = cand_path.resolve(strict=False)
    try:
        cand_resolved.relative_to(base_path)
    except ValueError:
        raise PathTraversalError(
            f"Path '{cand_resolved}' escapes allowed base '{base_path}'"
        ) from None
    return cand_resolved


# F021: ICMP rate limiting -------------------------------------------------------
class _TokenBucket:
    """Thread-safe token bucket for rate limiting.

    Capacity is the burst size; refill_per_sec controls steady-state rate.
    """

    __slots__ = ("capacity", "refill_per_sec", "_tokens", "_last", "_lock")

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, *, block: bool = True, timeout: Optional[float] = None) -> bool:
        """Take one token. If empty, sleep until refilled (or until *timeout*)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                # Refill based on elapsed time.
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._last) * self.refill_per_sec,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self.refill_per_sec
            if not block:
                return False
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(min(wait, 1.0))


# Tunables: 50/sec global burst, 5/sec per host. Plenty for inventory sweeps;
# small enough to avoid tripping IDS / scanner heuristics in customer networks.
_PING_GLOBAL_BUCKET = _TokenBucket(capacity=50.0, refill_per_sec=50.0)
_PING_PER_HOST_BUCKETS: Dict[str, _TokenBucket] = {}
_PING_BUCKETS_LOCK = threading.Lock()


def _get_per_host_ping_bucket(host: str) -> _TokenBucket:
    with _PING_BUCKETS_LOCK:
        b = _PING_PER_HOST_BUCKETS.get(host)
        if b is None:
            b = _TokenBucket(capacity=5.0, refill_per_sec=5.0)
            _PING_PER_HOST_BUCKETS[host] = b
        return b


def acquire_ping_token(host: str, *, timeout: float = 5.0) -> bool:
    """Block (up to *timeout* seconds) until a ping to *host* is permitted.

    Enforces a 50 ping/sec global cap and a 5 ping/sec per-host cap. Returns
    True when the token was granted, False if *timeout* elapsed first (caller
    should treat as "skip this ping").
    """
    if not _PING_GLOBAL_BUCKET.acquire(block=True, timeout=timeout):
        return False
    if not _get_per_host_ping_bucket(host).acquire(block=True, timeout=timeout):
        # Couldn't satisfy per-host quota — return the global token.
        with _PING_GLOBAL_BUCKET._lock:
            _PING_GLOBAL_BUCKET._tokens = min(
                _PING_GLOBAL_BUCKET.capacity, _PING_GLOBAL_BUCKET._tokens + 1.0
            )
        return False
    return True


def reset_ping_rate_limiter() -> None:
    """Test helper: clear cached per-host buckets and refill the global bucket."""
    with _PING_BUCKETS_LOCK:
        _PING_PER_HOST_BUCKETS.clear()
    with _PING_GLOBAL_BUCKET._lock:
        _PING_GLOBAL_BUCKET._tokens = _PING_GLOBAL_BUCKET.capacity
        _PING_GLOBAL_BUCKET._last = time.monotonic()


# F020: best-effort password scrubbing for Tk widgets ----------------------------
def scrub_password_widget(*widgets) -> None:
    """Best-effort wipe of password Entry widgets and bound StringVars.

    Python strings are immutable, so true zeroing of the secret bytes is not
    possible from pure Python — the original ``str`` may persist in the heap
    until garbage-collected (and even then, the allocator may not zero pages).
    This helper at least clears the visible widget buffer and the StringVar so
    a subsequent ``.get()``/screen-recorder/dialog-reuse cannot leak the
    plaintext. Accepts any mix of Tk ``Entry`` widgets and ``StringVar``
    instances; silently ignores ``None`` and unsupported types.
    """
    for w in widgets:
        if w is None:
            continue
        try:
            # tk.Entry / ttk.Entry expose .delete(start, end)
            if hasattr(w, "delete") and callable(w.delete):
                try:
                    w.delete(0, "end")
                except Exception:
                    pass
            # tk.StringVar exposes .set()
            if hasattr(w, "set") and callable(w.set):
                try:
                    w.set("")
                except Exception:
                    pass
        except Exception:
            # Never let scrubbing raise; missing widget is not a security event.
            continue


# F026: defensive whitespace stripping for tabular data --------------------------
def strip_dataframe_strings(df, *, columns: Optional[List[str]] = None):
    """Strip leading/trailing whitespace from string columns of *df* in place.

    Trailing/leading spaces in IPs, hostnames, and serial numbers ("`10.0.0.1 `")
    silently break equality comparisons against trimmed registry values, leading
    to phantom inventory mismatches. Apply this to every CSV/Excel read whose
    output flows into joins or set membership tests. Pass ``columns`` to limit
    scope; default is all object-dtype columns. Tolerates a missing pandas.
    """
    try:
        import pandas as pd  # local import: avoids hard dep at module load
    except ImportError:
        return df
    if df is None or not hasattr(df, "columns"):
        return df
    targets = columns if columns is not None else [
        c for c in df.columns if df[c].dtype == object
    ]
    for col in targets:
        try:
            df[col] = df[col].astype(str).str.strip()
            # Restore real NaN where the cell was originally empty.
            df[col] = df[col].replace({"nan": pd.NA, "None": pd.NA, "": pd.NA})
        except Exception:
            continue
    return df


# F023: temp-file leak protection ------------------------------------------------
_LAMIS_TEMP_PREFIXES: Tuple[str, ...] = ("ATLAS_", "PackingSlip_", "lamis_")


def cleanup_stale_lamis_tempfiles(max_age_hours: float = 24.0) -> int:
    """Remove leftover LAMIS temp files/dirs older than *max_age_hours*.

    Runtime code uses ``try/finally`` to clean up temp artifacts, but if the
    process is killed (OS shutdown, kill -9, hard crash) the artifacts leak.
    This best-effort sweeper runs at startup and removes anything in
    ``tempfile.gettempdir()`` matching our known prefixes whose mtime is older
    than the threshold. Returns the number of entries removed.
    """
    import shutil
    import tempfile

    cutoff = time.time() - max(0.0, float(max_age_hours)) * 3600.0
    tmp_root = Path(tempfile.gettempdir())
    removed = 0

    try:
        entries = list(tmp_root.iterdir())
    except OSError as exc:
        logging.debug("cleanup_stale_lamis_tempfiles: cannot list %s: %s", tmp_root, exc)
        return 0

    for entry in entries:
        name = entry.name
        if not any(name.startswith(p) for p in _LAMIS_TEMP_PREFIXES):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)  # type: ignore[arg-type]
            removed += 1
        except OSError as exc:
            logging.debug("cleanup_stale_lamis_tempfiles: skip %s: %s", entry, exc)

    if removed:
        logging.info("Removed %d stale LAMIS temp entr%s",
                     removed, "y" if removed == 1 else "ies")
    return removed


def extract_ip_sort_key(value: Union[str, None]) -> Tuple:
    """
    Extract IP address from a value and return a sortable tuple.

    Enables IP-based sorting by parsing the IP octets numerically.
    Non-IP values sort after valid IPs and use a **natural** sort —
    digit runs in the string compare numerically rather than
    lexically, so ``CON PALLET 10`` lands between 9 and 11, not
    between 1 and 2.

    Args:
        value: String that may contain an IP address

    Returns:
        Tuple for sorting:
        - (0, octet1, octet2, octet3, octet4, lowercase_string) for valid IPs
        - (1, natural_key_list) for non-IP values, where natural_key_list
          alternates lowercased text fragments and integer digit runs

    Example:
        >>> extract_ip_sort_key("10.9.100.5")
        (0, 10, 9, 100, 5, "10.9.100.5")
        >>> extract_ip_sort_key("CON PALLET 10")
        (1, ['con pallet ', 10, ''])
    """
    s = str(value or "").strip()
    match = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", s)
    if not match:
        return (1, _natural_sort_key(s))

    ip = match.group(1)
    try:
        octets = [int(x) for x in ip.split(".")]
    except ValueError:
        return (1, _natural_sort_key(s))

    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        return (1, _natural_sort_key(s))

    return (0, octets[0], octets[1], octets[2], octets[3], s.lower())


def _natural_sort_key(s: str) -> list:
    """Split *s* into alternating text/integer chunks for natural sort.

    Digit runs become ``int`` so ``"PALLET 10"`` sorts after
    ``"PALLET 9"`` instead of between ``"PALLET 1"`` and ``"PALLET 2"``.
    Text fragments are lowercased so the sort stays case-insensitive
    — preserves the case behaviour of the previous ``s.lower()`` key.
    """
    return [
        int(token) if token.isdigit() else token.lower()
        for token in re.split(r"(\d+)", s)
    ]


def get_credentials(service: str = "ATLAS") -> Tuple[Optional[str], Optional[str]]:
    """Return the first seeded default credential pair.

    ATLAS no longer persists user-entered credentials — the only stored
    creds are the (Fernet-encrypted) built-in seed list shipped with the
    app (admin/admin, cli/admin, su/Ciena123, ADMIN/ADMIN). This returns
    the first of those so callers have a starting point; the credential
    rotation in ``handle_credential_failure`` cycles through the rest on
    auth failure.

    Args:
        service: Unused — retained for backward signature compatibility.

    Returns:
        Tuple of (username, password) of the first default, or
        ``(None, None)`` when defaults are disabled via
        ``LAMIS_DISABLE_DEFAULT_CREDS``.
    """
    try:
        from utils.credentials import load_credentials_from_config
        return load_credentials_from_config()
    except Exception as e:
        logging.debug(f"[CREDS] Default credentials not available: {e}")
        return (None, None)


# ----------------------------------------------------------------------
# SSH host key verification (F001 — replaces paramiko.AutoAddPolicy)
# ----------------------------------------------------------------------


_HOST_KEY_PROMPT: Optional[Callable[[str, str, str], bool]] = None
_HOST_KEY_REMEMBERED_REJECTS: set = set()


def set_host_key_prompt(callback: Optional[Callable[[str, str, str], bool]]) -> None:
    """Register a prompt callback used by PromptingHostKeyPolicy.

    The callback receives (hostname, key_type, sha256_fingerprint) and must
    return True to accept the key, False to reject. If unset, unknown keys
    are rejected unless LAMIS_AUTO_ACCEPT_HOSTKEYS=1 is set in the env.
    """
    global _HOST_KEY_PROMPT
    _HOST_KEY_PROMPT = callback


def _format_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def default_tk_host_key_prompt(hostname: str, key_type: str, fingerprint: str) -> bool:
    """Show a Tk messagebox asking the user to accept an unknown SSH host key.

    Safe to invoke from worker threads: marshals the dialog onto the Tk
    main loop and waits up to 120 s for the user's response.
    """
    try:
        import tkinter
        from tkinter import messagebox
    except Exception:
        return False

    root = getattr(tkinter, "_default_root", None)

    message = (
        f"First connection to:\n  {hostname}\n\n"
        f"Key type:    {key_type}\n"
        f"Fingerprint: {fingerprint}\n\n"
        "WARNING: If you did not expect a new host key, an attacker may be "
        "intercepting this connection.\n\n"
        "Accept and remember this host key?"
    )

    result: dict = {"value": False}
    done = threading.Event()

    def _ask():
        try:
            result["value"] = bool(messagebox.askyesno("Unknown SSH Host Key", message))
        finally:
            done.set()

    if root is not None and threading.current_thread() is not threading.main_thread():
        root.after(0, _ask)
        if not done.wait(timeout=120):
            logging.warning(f"Host key prompt for {hostname} timed out — rejecting")
            return False
    else:
        _ask()
    return result["value"]


if paramiko is not None:

    class PromptingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
        """Prompts the user before accepting an unknown SSH host key.

        Replaces paramiko.AutoAddPolicy (which silently TOFUs every new host)
        with an interactive policy that surfaces the SHA-256 fingerprint and
        defers to the registered prompt callback. If no callback is set, the
        policy rejects the connection.
        """

        def missing_host_key(self, client, hostname, key):  # noqa: D401
            fingerprint = _format_fingerprint(key)
            key_type = key.get_name()

            if hostname in _HOST_KEY_REMEMBERED_REJECTS:
                logging.warning(
                    f"Refusing previously-rejected host key for {hostname} ({fingerprint})"
                )
                raise paramiko.SSHException(
                    f"Host key for {hostname} previously rejected by user"
                )

            callback = _HOST_KEY_PROMPT
            if callback is None:
                logging.warning(
                    f"No host key prompt registered; refusing unknown key for "
                    f"{hostname} ({key_type} {fingerprint})"
                )
                raise paramiko.SSHException(
                    f"Host key verification failed for {hostname}"
                )

            try:
                accepted = bool(callback(hostname, key_type, fingerprint))
            except Exception as exc:
                logging.error(f"Host key prompt failed for {hostname}: {exc}")
                raise paramiko.SSHException(
                    f"Host key verification failed for {hostname}"
                )

            if not accepted:
                _HOST_KEY_REMEMBERED_REJECTS.add(hostname)
                logging.warning(
                    f"User rejected host key for {hostname} ({fingerprint})"
                )
                raise paramiko.SSHException(
                    f"Host key for {hostname} rejected by user"
                )

            client.get_host_keys().add(hostname, key_type, key)
            logging.info(
                f"User accepted new host key for {hostname} ({key_type} {fingerprint})"
            )

else:  # paramiko unavailable — provide a stub so imports don't break
    class PromptingHostKeyPolicy:  # type: ignore[no-redef]
        def missing_host_key(self, *_args, **_kwargs):
            raise RuntimeError("paramiko is not installed")


def get_host_key_policy():
    """Return the configured paramiko host-key policy.

    Resolution order:
      1. ``LAMIS_PROMPT_HOSTKEYS=1``  → force interactive prompting
         (security override — useful in suspicious environments).
      2. ``LAMIS_AUTO_ACCEPT_HOSTKEYS=1`` → force AutoAdd (legacy env knob).
      3. ``config.SSH_AUTO_ACCEPT_HOST_KEYS`` → project default
         (True for unattended bulk automation).
      4. Otherwise → PromptingHostKeyPolicy (Tk dialog).

    AutoAdd here is Trust-On-First-Use: the new key is persisted to the
    LAMIS known_hosts file, and any *changed* key on a later connection
    still raises (paramiko enforces this independent of the policy).
    """
    if paramiko is None:
        raise RuntimeError("paramiko is not installed")

    if os.environ.get("LAMIS_PROMPT_HOSTKEYS") == "1":
        return PromptingHostKeyPolicy()

    if os.environ.get("LAMIS_AUTO_ACCEPT_HOSTKEYS") == "1":
        logging.warning(
            "LAMIS_AUTO_ACCEPT_HOSTKEYS=1 — auto-accepting unknown SSH host keys (TOFU)"
        )
        return SafeAutoAddPolicy(get_known_hosts_path())

    try:
        from config import SSH_AUTO_ACCEPT_HOST_KEYS  # local import to avoid cycles
    except Exception:
        SSH_AUTO_ACCEPT_HOST_KEYS = False
    if SSH_AUTO_ACCEPT_HOST_KEYS:
        logging.info(
            "[HOSTKEY] Auto-accepting unknown host keys (TOFU). "
            "Set LAMIS_PROMPT_HOSTKEYS=1 to require operator confirmation."
        )
        return SafeAutoAddPolicy(get_known_hosts_path())

    return PromptingHostKeyPolicy()


# Windows socket error codes that indicate a transient routing problem
# rather than a real connection refusal. Common after netsh sets a
# static IP — the interface needs a beat before sockets can route to
# the new subnet.
_TRANSIENT_NETWORK_ERRNOS = {
    10051,  # WSAENETUNREACH — network unreachable (the Nokia G42 case)
    10065,  # WSAEHOSTUNREACH — host unreachable
    10060,  # WSAETIMEDOUT — connection timed out
    10061,  # WSAECONNREFUSED — refused (device booting / SSH not up yet)
    10050,  # WSAENETDOWN — network down
    10064,  # WSAEHOSTDOWN — host down
    # POSIX equivalents for cross-platform safety
    101,    # ENETUNREACH
    113,    # EHOSTUNREACH
    111,    # ECONNREFUSED
    110,    # ETIMEDOUT
    100,    # ENETDOWN
}


def _is_transient_network_error(exc: BaseException) -> bool:
    """True when *exc* is a connection failure that may succeed if retried.

    OSError/socket.error subclasses with a recognized errno are
    transient; SSHException-style refusals are NOT. We use this to
    decide whether ``ensure_host_key_known`` should retry vs. give up
    immediately on a pre-verify connect failure.
    """
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
        if errno in _TRANSIENT_NETWORK_ERRNOS:
            return True
    # Catch ConnectionRefusedError, ConnectionResetError, TimeoutError, etc.
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return False


def nokia_ssh_authenticate(transport, username: str, password: str) -> None:
    """SSH-layer authentication for a Nokia 1830 / PSS / PSI shelf.

    Grounded in a real PSS (HSTQTX02): **modern shelves accept standard password
    auth as admin/admin and drop straight to the CLI shell** (no inner login).
    Some **legacy shelves** instead expose a passwordless ``cli`` account (SSH
    auth method ``none`` or ``keyboard-interactive``) and then present an inner
    ``Username:``/``Password:`` two-stage on the shell. This tries the modern
    password auth (``username``/``password``) first, then the legacy ``cli``
    fallbacks. The caller handles the inner two-stage only if the shell actually
    presents it. Raises ``paramiko`` auth/SSH exceptions on genuine failure.
    """
    if paramiko is None:
        raise RuntimeError("paramiko is required for SSH authentication.")
    # Modern: password auth as the given user (e.g. admin/admin).
    try:
        transport.auth_password(username, password)
        return
    except (paramiko.AuthenticationException, paramiko.BadAuthenticationType):
        pass
    # Legacy: passwordless 'cli' account; the offered method varies by shelf.
    try:
        transport.auth_none("cli")
        return
    except paramiko.BadAuthenticationType as exc:
        allowed = set(getattr(exc, "allowed_types", []) or [])
    if "keyboard-interactive" in allowed:
        transport.auth_interactive("cli", lambda t, i, p: ["" for _ in p])
        return
    if "password" in allowed:
        transport.auth_password("cli", "")       # passwordless cli via password method
        return
    raise paramiko.SSHException(
        f"No usable SSH auth for {username!r} or 'cli' (shelf offered: {sorted(allowed)})"
    )


def ensure_host_key_known(
    host: str,
    port: int = 22,
    timeout: float = 10.0,
    *,
    max_retries: int = 3,
    retry_delay: float = 1.5,
) -> bool:
    """Ensure *host*'s SSH host key is recorded in the LAMIS known_hosts file.

    Used by the spawn-based scripts (Nokia_1830, Nokia_PSI, Ciena_6500,
    Ciena_RLS) and the TDS launcher to perform host-key verification *before*
    handing off to an external ``ssh`` process or Python subprocess. This lets
    the spawned process run with ``StrictHostKeyChecking=yes`` (full
    enforcement) instead of ``accept-new`` (silent TOFU), because the key is
    already in known_hosts by the time it starts.

    Behavior:
      * If the host key is already in known_hosts, returns True without
        prompting.
      * Otherwise performs a one-shot paramiko Transport handshake, runs the
        configured ``MissingHostKeyPolicy`` (Tk prompt by default), and on
        accept persists the key to known_hosts.
      * On transient network errors (``WSAENETUNREACH`` / ``ETIMEDOUT`` /
        connection refused / etc. — see ``_TRANSIENT_NETWORK_ERRNOS``)
        retries up to ``max_retries`` times with ``retry_delay`` seconds
        between attempts. This is the post-netsh-settle case: an
        operator just set a static IP via ``Software Upgrades`` and the
        OS hasn't fully brought up the interface yet.
      * Returns False if the user rejects the key, the host stays
        unreachable across retries, or paramiko isn't available. Never
        raises — callers decide whether a False return aborts the
        connection. A False return is also accompanied by an INFO-level
        log line that distinguishes "host key issue" from "network
        unreachable" so the operator gets an actionable signal.

    Honors ``LAMIS_AUTO_ACCEPT_HOSTKEYS=1`` for headless contexts.
    """
    if paramiko is None:
        logging.warning(
            "[HOSTKEY] paramiko unavailable; cannot pre-verify %s — "
            "spawned ssh will fall back to its own host-key handling", host,
        )
        return False

    kh_path = str(get_known_hosts_path())
    target = host if port == 22 else f"[{host}]:{port}"
    last_transient_err: Optional[BaseException] = None

    for attempt in range(1, max_retries + 1):
        try:
            client = paramiko.SSHClient()
            safe_load_host_keys(client, kh_path)

            # Fast-path: already trusted (re-checked each iteration in
            # case a prior retry persisted the key partway through).
            host_keys = client.get_host_keys()
            if host_keys.lookup(target):
                client.close()
                return True

            client.set_missing_host_key_policy(get_host_key_policy())
            try:
                # Use a non-existent user with no auth methods so the connection
                # terminates right after host-key verification, before any auth
                # round-trip. We only care that missing_host_key() ran.
                client.connect(
                    host,
                    port=port,
                    username="__lamis_hostkey_probe__",
                    password="",
                    timeout=timeout,
                    banner_timeout=timeout,
                    auth_timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
            except paramiko.SSHException as e:
                msg = str(e).lower()
                # "rejected by user" / "verification failed" → real refusal
                if "rejected" in msg or "verification failed" in msg:
                    logging.warning("[HOSTKEY] Verification refused for %s: %s", host, e)
                    client.close()
                    return False
                # Auth failures are EXPECTED — the key was accepted first.
            except paramiko.AuthenticationException:
                pass  # expected — host key already verified
            except Exception as e:
                if _is_transient_network_error(e):
                    last_transient_err = e
                    client.close()
                    if attempt < max_retries:
                        logging.info(
                            "[HOSTKEY] %s unreachable on attempt %d/%d "
                            "(%s); retrying in %.1fs — likely interface "
                            "still coming up after netsh.",
                            host, attempt, max_retries,
                            _short_socket_error(e), retry_delay,
                        )
                        time.sleep(retry_delay)
                        continue
                    # Final attempt failed with a network error — log
                    # the actionable variant so the operator knows to
                    # check cable / interface, not host keys.
                    logging.warning(
                        "[HOSTKEY] %s remained unreachable across "
                        "%d attempt(s) (%s). Check the Ethernet cable, "
                        "confirm the static IP was applied to the right "
                        "adapter, and that the device is powered on.",
                        host, max_retries, _short_socket_error(e),
                    )
                    return False
                # Non-transient: log + bail without retrying.
                logging.warning(
                    "[HOSTKEY] Could not pre-verify host key for %s: %s",
                    host, friendly_error(e) if "friendly_error" in globals() else e,
                )
                client.close()
                return False

            safe_save_host_keys(client, kh_path)

            # Re-check that the key actually landed in known_hosts
            verified = client.get_host_keys().lookup(target) is not None
            client.close()

            if verified:
                try:
                    restrict_path_to_owner(kh_path)
                except Exception:
                    pass
                logging.info("[HOSTKEY] Host key for %s recorded in known_hosts", host)
            else:
                logging.warning(
                    "[HOSTKEY] Host key probe for %s completed but key not found in known_hosts",
                    host,
                )
            return verified
        except Exception as e:
            # Outermost catch — shouldn't fire in practice since we
            # handle the connect-time errors above, but stay safe.
            if _is_transient_network_error(e) and attempt < max_retries:
                last_transient_err = e
                time.sleep(retry_delay)
                continue
            logging.warning("[HOSTKEY] Unexpected error pre-verifying %s: %s", host, e)
            return False

    # Should not reach here — loop returns or continues — but guard
    # anyway so the function always has an explicit return.
    return False


def _short_socket_error(exc: BaseException) -> str:
    """Compact one-line description of a socket error for log lines."""
    errno = getattr(exc, "errno", None) or getattr(exc, "winerror", None)
    if errno is not None:
        return f"errno {errno}: {exc.__class__.__name__}"
    return f"{exc.__class__.__name__}: {exc}"
