"""Local, per-user storage for the AI assistant's API key.

The key is kept in the Windows Credential Manager (via ``keyring``, which ATLAS
already uses for device credentials) - the DPAPI-protected, per-user OS vault.
It is NEVER embedded in the executable, the installer, or the repo: a secret
compiled into a distributed client can always be extracted by whoever holds the
client, so "store it in the program" is only safe in the sense of "store it in
the machine's credential vault, read at runtime."

This is a machine-local convenience for a tech supplying their own key. The
production path remains a server-side proxy (config.AI_BASE_URL) so field
laptops carry no secret at all - see docs/AI_ASSISTANT.md ship-blockers.
"""

from __future__ import annotations

from typing import Optional

# Reuse ATLAS's existing keyring service name so all secrets live together.
_SERVICE = "ATLAS"
_ENTRY = "openai_api_key"


def get_key() -> Optional[str]:
    """Return the stored key from the OS credential vault, or None. Never
    raises - a missing backend / entry just yields None."""
    try:
        import keyring
        return keyring.get_password(_SERVICE, _ENTRY)
    except Exception:
        return None


def store_key(key: str) -> None:
    """Save the key to the per-user OS credential vault."""
    import keyring
    keyring.set_password(_SERVICE, _ENTRY, key.strip())


def clear_key() -> None:
    """Remove the stored key, if present. Never raises."""
    try:
        import keyring
        keyring.delete_password(_SERVICE, _ENTRY)
    except Exception:
        pass
