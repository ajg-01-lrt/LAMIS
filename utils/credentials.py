"""
Secure credential management with encryption support.

Handles encrypted storage and retrieval of device login credentials from a config file.
Uses Fernet (symmetric encryption) from the cryptography library for secure storage.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict
from cryptography.fernet import Fernet
from utils.helpers import get_project_root, restrict_path_to_owner


def _get_user_data_dir() -> Path:
    """Return the per-user ATLAS data directory (%APPDATA%\\ATLAS), creating it if needed."""
    app_data = os.environ.get("APPDATA", os.path.expanduser("~"))
    d = Path(app_data) / "ATLAS"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Credential files live in %APPDATA%\ATLAS so they are writable without
# admin privileges when ATLAS is installed in Program Files.
CREDS_KEY_FILE = _get_user_data_dir() / ".creds_key"
CREDS_CONFIG_FILE = _get_user_data_dir() / "credentials_config.json"

# Built-in seed defaults — used ONLY to populate the encrypted config on first
# run. After seeding, defaults are read from the encrypted config file and these
# constants are never consulted again. Override the seed values in a fresh
# install by setting LAMIS_SEED_DEFAULTS=user1:pw1,user2:pw2 in the environment
# before first launch, or by editing this list before building.
#
# The defaults can be disabled entirely (e.g. for non-lab deployments) by
# setting LAMIS_DISABLE_DEFAULT_CREDS=1; in that case the credential failure
# handler will jump straight to prompting the user.
_BUILTIN_DEFAULT_SEED: List[Tuple[str, str]] = [
    # Most devices default to admin/admin (Smartoptics DCP, Nokia SAR/IXR).
    # Tried first since it is the most common single credential across the
    # supported device population.
    ("admin", "admin"),
    # Nokia 1830 SSH-level login: user is "cli" with password "admin".
    # Once authenticated, the Nokia_1830 script performs the secondary
    # interactive cli -> admin -> admin sequence (see scripts/Nokia_1830.py).
    ("cli", "admin"),
    # Ciena 6500 / RLS shell login: su / Ciena123!.
    ("su", "Ciena123!"),
    # Ciena RLS RESTCONF login -- distinct from the shell user. The REST
    # API requires the diaguser account; ``su`` works for SSH only.
    ("diaguser", "Ciena123"),
    # Some older / case-sensitive devices use ADMIN/ADMIN — tried last so
    # we don't pre-empt the more common admin/admin lowercase variant.
    ("ADMIN", "ADMIN"),
]


def _load_seed_from_env() -> Optional[List[Tuple[str, str]]]:
    raw = os.environ.get("LAMIS_SEED_DEFAULTS", "").strip()
    if not raw:
        return None
    out: List[Tuple[str, str]] = []
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        u, p = pair.split(":", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            out.append((u, p))
    return out or None


def _defaults_disabled() -> bool:
    return os.environ.get("LAMIS_DISABLE_DEFAULT_CREDS", "").strip() in ("1", "true", "yes")


def _get_or_create_key() -> bytes:
    """Get encryption key from file, or create one if it doesn't exist.

    The key is stored locally in .creds_key (should be git-ignored).
    This key decrypts the credentials in credentials_config.json.
    """
    if CREDS_KEY_FILE.exists():
        return CREDS_KEY_FILE.read_bytes()

    # Generate new key
    key = Fernet.generate_key()
    CREDS_KEY_FILE.write_bytes(key)
    try:
        CREDS_KEY_FILE.chmod(0o600)  # POSIX read/write for owner only
    except Exception:
        pass
    restrict_path_to_owner(CREDS_KEY_FILE)  # Windows ACL hardening
    logging.info(f"[CREDS] Generated new encryption key at {CREDS_KEY_FILE}")
    return key


def _encrypt_password(password: str) -> str:
    """Encrypt a password string."""
    key = _get_or_create_key()
    cipher = Fernet(key)
    encrypted = cipher.encrypt(password.encode('utf-8'))
    return encrypted.decode('utf-8')


def _decrypt_password(encrypted_password: str) -> str:
    """Decrypt a password string."""
    key = _get_or_create_key()
    cipher = Fernet(key)
    try:
        decrypted = cipher.decrypt(encrypted_password.encode('utf-8'))
        return decrypted.decode('utf-8')
    except Exception as e:
        logging.error(f"[CREDS] Failed to decrypt password: {e}")
        raise


def _read_config_file() -> dict:
    if not CREDS_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CREDS_CONFIG_FILE.read_text())
    except Exception as e:
        logging.error(f"[CREDS] Failed to parse credentials config: {e}")
        return {}


def _write_config_file(config: dict) -> bool:
    try:
        CREDS_CONFIG_FILE.write_text(json.dumps(config, indent=2))
        try:
            CREDS_CONFIG_FILE.chmod(0o600)
        except Exception:
            pass
        restrict_path_to_owner(CREDS_CONFIG_FILE)  # Windows ACL hardening
        return True
    except Exception as e:
        logging.error(f"[CREDS] Failed to write credentials config: {e}")
        return False


def _seed_defaults_into_config() -> None:
    """On first run, encrypt the built-in seed defaults into credentials_config.json.

    Once seeded, the seed constants are no longer consulted. Subsequent reads
    decrypt the values from the config file. Users wanting to rotate the
    defaults can edit credentials_config.json (the values are encrypted at
    rest) or delete the file to re-seed from the current build.

    Migration: if a stored config exists but the build has added *new* default
    entries (e.g. an upgrade adds "cli/admin" for Nokia 1830), the new entries
    are appended to the encrypted store without disturbing existing entries
    or user-saved primary credentials.
    """
    config = _read_config_file()
    seed = _load_seed_from_env() or _BUILTIN_DEFAULT_SEED
    if not seed:
        return

    existing_raw = config.get("defaults") or []
    # Build a username -> entry map (preserves the encrypted password from the
    # existing config so we don't have to re-encrypt unchanged entries).
    existing_by_user: Dict[str, dict] = {}
    for entry in existing_raw:
        if isinstance(entry, dict):
            u = (entry.get("username") or "").strip()
            if u:
                existing_by_user[u] = entry

    seed_users = [u for u, _ in seed]
    needs_reorder = False
    if existing_raw:
        # Find the seed entries that are present in the existing config and
        # check whether they appear in the seed's preferred order. If any are
        # out of order, we rewrite the file so users get the new ordering on
        # upgrade without having to delete credentials_config.json.
        existing_seed_users_in_existing_order = [
            (entry.get("username") or "").strip()
            for entry in existing_raw
            if isinstance(entry, dict)
            and (entry.get("username") or "").strip() in seed_users
        ]
        existing_seed_users_in_seed_order = [
            u for u in seed_users if u in existing_by_user
        ]
        if existing_seed_users_in_existing_order != existing_seed_users_in_seed_order:
            needs_reorder = True

    new_pairs = [(u, p) for u, p in seed if u not in existing_by_user]

    # One-time cleanup: ATLAS no longer persists user-entered credentials,
    # so any "credentials" block from an older install is now dead data.
    # Drop it if present (idempotent — no-op on already-clean files).
    legacy_creds_present = bool(config.get("credentials"))
    if legacy_creds_present:
        config.pop("credentials", None)
        _write_config_file(config)
        logging.info(
            "[CREDS] Removed legacy user-credential block from encrypted "
            "config (ATLAS no longer persists user-entered credentials)"
        )

    if existing_raw and not new_pairs and not needs_reorder:
        return  # Already seeded, in the right order, no new defaults to add.

    try:
        encrypted_new = [
            {"username": u, "password": _encrypt_password(p)} for u, p in new_pairs
        ]
    except Exception as e:
        logging.error(f"[CREDS] Failed to seed default credentials: {e}")
        return

    if existing_raw:
        # Rebuild defaults: emit seed entries in seed order first (re-using
        # the existing encrypted password if we already had it, else using
        # the freshly-encrypted one for this user), then append any extra
        # user-added entries that aren't in the seed (in their original
        # relative order).
        encrypted_new_by_user = {e["username"]: e for e in encrypted_new}
        rebuilt: List[dict] = []
        for u in seed_users:
            if u in existing_by_user:
                rebuilt.append(existing_by_user[u])
            elif u in encrypted_new_by_user:
                rebuilt.append(encrypted_new_by_user[u])
        for entry in existing_raw:
            if not isinstance(entry, dict):
                continue
            u = (entry.get("username") or "").strip()
            if u and u not in seed_users:
                rebuilt.append(entry)
        config["defaults"] = rebuilt
    else:
        config["defaults"] = encrypted_new
    config.setdefault(
        "notes",
        "All credential values are encrypted with Fernet. "
        "Set LAMIS_DISABLE_DEFAULT_CREDS=1 to skip default-credential attempts.",
    )
    if _write_config_file(config):
        if existing_raw and needs_reorder and not encrypted_new:
            logging.info(
                "[CREDS] Reordered default credential sets in encrypted config "
                "to match new seed priority"
            )
        elif existing_raw:
            logging.info(
                f"[CREDS] Migrated {len(encrypted_new)} new default credential set(s) "
                f"into encrypted config (reorder={'yes' if needs_reorder else 'no'})"
            )
        else:
            logging.info(
                f"[CREDS] Seeded {len(encrypted_new)} default credential set(s) into encrypted config"
            )


def _load_defaults_from_config() -> List[Tuple[str, str]]:
    """Decrypt and return the stored default credentials list."""
    config = _read_config_file()
    raw = config.get("defaults") or []
    out: List[Tuple[str, str]] = []
    for entry in raw:
        try:
            u = entry.get("username")
            enc = entry.get("password")
            if not u or not enc:
                continue
            out.append((u, _decrypt_password(enc)))
        except Exception as e:
            logging.warning(f"[CREDS] Skipping unreadable default entry: {e}")
    return out


def load_credentials_from_config() -> Tuple[Optional[str], Optional[str]]:
    """Return the first default credential pair from the encrypted config.

    ATLAS no longer persists user-entered credentials — the seeded
    defaults are the only thing this function returns. The credential
    rotation in ``handle_credential_failure`` cycles through every
    default in order on auth failure; this just provides the starting
    point.

    Returns:
        (username, password) of ``_BUILTIN_DEFAULT_SEED[0]`` after Fernet
        decrypt, else ``(None, None)`` if defaults are disabled via
        ``LAMIS_DISABLE_DEFAULT_CREDS`` or the seed list is empty.
    """
    # Make sure defaults are seeded on first run, and migrate away any
    # legacy user-credential block from older installs.
    _seed_defaults_into_config()

    if _defaults_disabled():
        logging.debug("[CREDS] Default credentials disabled by LAMIS_DISABLE_DEFAULT_CREDS")
        return (None, None)
    defaults = _load_defaults_from_config()
    if defaults:
        return defaults[0]
    return (None, None)


def get_default_credentials_to_try() -> List[Tuple[str, str]]:
    """Get list of default credentials to try (in order).

    Returns an empty list if defaults have been disabled via the
    ``LAMIS_DISABLE_DEFAULT_CREDS`` environment variable.
    """
    if _defaults_disabled():
        return []
    _seed_defaults_into_config()
    return _load_defaults_from_config()


def get_default_credential(username: str) -> Optional[Tuple[str, str]]:
    """Return the (username, password) pair for *username* from the encrypted
    defaults store, or ``None`` if not present / defaults are disabled.

    This is the single point of access for callers that need a specific
    vendor default — e.g. the TDS frame wants ``su`` for Ciena and the
    SSH banner shortcut wants ``cli`` for the Nokia 1830. Looking the
    entry up here keeps every consumer pulling from the same Fernet
    source rather than maintaining its own hardcoded copy of the
    password.
    """
    if not username:
        return None
    if _defaults_disabled():
        return None
    for u, p in get_default_credentials_to_try():
        if u == username:
            return (u, p)
    return None


# Conventional username for each vendor's factory default in the seed list.
# Callers that know they need "the Ciena default" can ask for vendor="ciena"
# instead of hardcoding the username — keeps the username convention in one
# place if the seed list is ever reordered or renamed.
_VENDOR_DEFAULT_USERNAME: Dict[str, str] = {
    "ciena": "su",        # Ciena 6500 / RLS shell — su / Ciena123
    "ciena-rls": "su",
    "ciena-6500": "su",
    "rls": "su",
    "6500": "su",
    # RESTCONF on RLS uses a separate diagnostic account; shell user
    # ``su`` doesn't have REST API access.
    "ciena-rls-rest": "diaguser",
    "rls-rest": "diaguser",
    "nokia": "admin",     # Nokia SAR / IXR / Smartoptics — admin / admin
    "nokia-1830": "cli",  # Nokia 1830 SSH bootstrap — cli / admin
    "1830": "cli",
    "smartoptics": "admin",
    "dcp": "admin",
}


def get_default_credential_for_vendor(vendor: str) -> Optional[Tuple[str, str]]:
    """Return the (user, pw) pair for *vendor*'s factory default, from the
    encrypted store. Returns ``None`` if the vendor is unknown, defaults
    are disabled, or the matching entry has been removed."""
    if not vendor:
        return None
    user = _VENDOR_DEFAULT_USERNAME.get(vendor.strip().lower())
    if not user:
        return None
    return get_default_credential(user)


def prompt_for_credentials_gui(parent_window=None) -> Optional[Tuple[str, str]]:
    """Show a GUI dialog to prompt user for credentials.

    Args:
        parent_window: Parent tkinter window (optional)

    Returns:
        (username, password) if user confirms, else None if cancelled

    Notes:
        Tkinter is NOT thread-safe. Calling tk.Tk() from a worker thread on
        Windows deadlocks the entire process — the dialog's mainloop blocks
        the worker forever, and Tk's atexit handlers prevent the parent
        process from exiting even after the user clicks Abort or closes the
        window. To prevent this, when called from a non-main thread we
        refuse to create a new Tk root and return None immediately. Callers
        should marshal credential prompts back to the main thread (or
        configure default credentials in advance) for unattended scans.
    """
    import threading
    if threading.current_thread() is not threading.main_thread() and parent_window is None:
        logging.warning(
            "[CREDS] Skipping credential prompt: called from worker thread "
            "without a parent_window. Tk dialogs from non-main threads "
            "deadlock on Windows. Configure credentials in the main GUI "
            "instead, or pass parent_window from a main-thread caller."
        )
        return None

    try:
        import tkinter as tk
        from tkinter import simpledialog, messagebox

        owns_root = False
        if parent_window is None:
            # Create a hidden root window
            parent_window = tk.Tk()
            parent_window.withdraw()
            owns_root = True

        # Create a top-level dialog
        dialog = tk.Toplevel(parent_window)
        dialog.title("Update Device Credentials")
        dialog.transient(parent_window)
        dialog.grab_set()
        dialog.resizable(False, False)

        # Labels and entries
        tk.Label(dialog, text="Username:", font=("Arial", 10)).pack(pady=10)
        username_var = tk.StringVar()
        username_entry = tk.Entry(dialog, textvariable=username_var, width=30)
        username_entry.pack(pady=5)
        username_entry.focus()

        tk.Label(dialog, text="Password:", font=("Arial", 10)).pack(pady=10)
        password_var = tk.StringVar()
        password_entry = tk.Entry(dialog, textvariable=password_var, show="*", width=30)
        password_entry.pack(pady=5)

        result = {"ok": False}

        def on_ok():
            result["ok"] = True
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        # Buttons
        button_frame = tk.Frame(dialog)
        button_frame.pack(pady=20)
        tk.Button(button_frame, text="Save & Continue", command=on_ok, width=15).pack(side=tk.LEFT, padx=5)
        tk.Button(button_frame, text="Cancel", command=on_cancel, width=15).pack(side=tk.LEFT, padx=5)

        # Size to contents and center on screen
        dialog.update_idletasks()
        w = dialog.winfo_reqwidth()
        h = dialog.winfo_reqheight()
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        dialog.geometry(f"{w}x{h}+{x}+{y}")

        dialog.wait_window()

        try:
            if result["ok"]:
                username = username_var.get().strip()
                password = password_var.get()
                try:
                    from utils.helpers import scrub_password_widget
                    scrub_password_widget(password_entry, password_var)
                except Exception:
                    pass
                if username and password:
                    logging.debug(f"[CREDS] User entered credentials for: {username}")
                    return (username, password)
                else:
                    messagebox.showerror("Error", "Username and password cannot be empty")
                    return None

            return None
        finally:
            # Always tear down a root we created so the process can exit.
            if owns_root:
                try:
                    parent_window.destroy()
                except Exception:
                    pass

    except Exception as e:
        logging.error(f"[CREDS] Failed to show credential prompt: {e}")
        return None

