# Secure Credential Management

ATLAS now stores only the built-in default credential seed in an encrypted
config file under `%APPDATA%\ATLAS`. It no longer persists user-entered
device credentials.

## What Gets Stored

On first launch, ATLAS creates:

1. `credentials_config.json` in `%APPDATA%\ATLAS`
2. `.creds_key` in `%APPDATA%\ATLAS`

The config file contains an encrypted `defaults` list. Those defaults are the
only credentials ATLAS stores on disk.

## How It Works

1. Passwords are encrypted with Fernet from `cryptography`
2. The Fernet key is stored in `.creds_key`
3. `load_credentials_from_config()` returns the first seeded default pair
4. On auth failure, `handle_credential_failure()` rotates through the rest of
  the seeded defaults
5. When defaults are exhausted, the GUI prompts the operator for credentials
6. Operator-entered credentials are used for that retry path but are not
  written back to disk

## Security Notes

- Never commit `credentials_config.json` or `.creds_key` to version control
- Both files live in `%APPDATA%\ATLAS`, not the repo root
- File permissions / ACLs are restricted to the current user where possible
- The encryption key is unique per installation
- Passwords are decrypted only in memory when needed

## Rotating Defaults

To change the seeded defaults for a fresh install:

1. Stop ATLAS.
2. Delete `%APPDATA%\ATLAS\credentials_config.json`.
3. Optionally delete `%APPDATA%\ATLAS\.creds_key` to rotate the encryption key too.
4. Set `LAMIS_SEED_DEFAULTS=user1:pw1,user2:pw2` before the next launch, or edit `_BUILTIN_DEFAULT_SEED` in `utils/credentials.py` before building.
5. Launch ATLAS again so the encrypted defaults are re-seeded.

## Disabling Defaults

Set `LAMIS_DISABLE_DEFAULT_CREDS=1` to skip default-credential attempts
entirely. In that mode ATLAS will prompt once authentication fails.
