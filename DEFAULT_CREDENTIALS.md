# Default Credentials System

> **Security note:** The literal default values used to be listed here. They
> have been removed from documentation and from source code. Defaults are now
> seeded into `credentials_config.json` (encrypted with Fernet) on first run
> and never appear in plain text on disk or in logs.

## Authentication Flow

```text
Device connection attempt
    ↓
Try: default credential set #1 (decrypted from credentials_config.json)
    ↓
    Success → Connect to device
    Fail ↓
Try: default credential set #2 (if configured)
    ↓
    Success → Connect to device
    Fail ↓
Prompt user for credentials
    ↓
User enters username/password
    ↓
Retry connection
```

## Where defaults live

- `credentials_config.json` (in the project root for source installs, or
  `%APPDATA%\ATLAS\` style location for packaged installs) holds a `defaults`
  array of `{username, password}` objects where `password` is a Fernet
  ciphertext. The encryption key lives in `.creds_key` next to it (mode
  `0600`, git-ignored).
- The seed values used to populate that file on first run are defined in
  `utils/credentials.py` as `_BUILTIN_DEFAULT_SEED`. Override them at install
  time by setting `LAMIS_SEED_DEFAULTS=user1:pw1,user2:pw2` in the environment
  before launching the app for the first time.

## Disabling defaults

Set `LAMIS_DISABLE_DEFAULT_CREDS=1` in the environment to skip default
credential attempts entirely — the app will prompt on first failure. Use this
for any deployment outside the lab environment.

## Logging

Audit logs record only the **index** of the default credential set used
(`Using default credential set #2 for <ip>`), never the username or password.
The `CredentialFilter` (`utils/helpers.py`) additionally redacts any
`password=`/`secret=`/`token=` values that might appear in device output.

## Rotating the defaults

1. Stop the app.
2. Delete `credentials_config.json` (and optionally `.creds_key` to also
   rotate the encryption key).
3. Either edit `_BUILTIN_DEFAULT_SEED` in `utils/credentials.py` and rebuild,
   or set `LAMIS_SEED_DEFAULTS` in the environment before the next launch.
4. Restart the app — fresh defaults are encrypted and seeded automatically.

## Recommended for non-lab use

- Set `LAMIS_DISABLE_DEFAULT_CREDS=1`.
- Re-seed lab defaults only when necessary via `LAMIS_SEED_DEFAULTS`, or
    rely on the in-app prompt for operator-entered credentials.
