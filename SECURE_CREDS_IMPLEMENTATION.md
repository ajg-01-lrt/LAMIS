# Secure Credential Configuration - Implementation Summary

## What Was Implemented

✓ **Encrypted Credentials Module** (`utils/credentials.py`)

- Uses Fernet symmetric encryption (cryptography library)
- Automatic encryption key generation and storage
- Secure file permissions (600 - owner read/write only)

✓ **Config File Support** (`credentials_config.json`)

- JSON format with encrypted `defaults` array
- Stores only seeded default credentials, not user-entered credentials

✓ **Unified Credential Loading** (`utils/helpers.py`)

- `get_credentials()` returns the first seeded default pair
- Auth-failure handling rotates through the remaining defaults
- Exhaustion triggers a GUI prompt on the main thread

✓ **Git Security**

- Added `.creds_key` to `.gitignore`
- Added `credentials_config.json` to `.gitignore`
- Credentials never committed to version control

### How Credentials Flow Through the App

```text
Script (identify_device_ssh, identify_device_telnet, etc)
    ↓
get_credentials() [in utils/helpers.py]
  ├→ Load first default from credentials_config.json [encrypted]
    ├→ Decrypt using .creds_key
    └→ Return (username, password)

On auth failure:
  └→ Rotate remaining defaults, then prompt the operator
```

### Usage

#### Re-seed Defaults (One-Time Setup For Fresh Install)

```bash
set LAMIS_SEED_DEFAULTS=admin:admin,cli:admin
python main.py
```

#### The App Uses Them Automatically

No changes needed in code. `get_credentials()` loads the first encrypted
default, and the auth-failure path tries the rest before prompting.

#### Rotate / Reset Defaults

Delete `%APPDATA%\ATLAS\credentials_config.json` and restart ATLAS with a new
`LAMIS_SEED_DEFAULTS` value, or rebuild with an updated `_BUILTIN_DEFAULT_SEED`.

### Security Features

1. **Encryption**: Fernet (authenticated encryption)
2. **Key Isolation**: Encryption key in separate `.creds_key` file
3. **File Permissions**: 600 (owner read/write only)
4. **No Plaintext**: Stored default passwords are never written in plaintext
5. **In-Memory Only**: Decryption happens only when needed
6. **Automatic Protection**: Both files are kept out of version control

### Files Changed/Created

**New Files:**

- `utils/credentials.py` - Encryption/decryption logic
- `CREDENTIALS.md` - User documentation
- `credentials_config.json` - (auto-generated, git-ignored)
- `.creds_key` - (auto-generated, git-ignored)

**Modified Files:**

- `.gitignore` - Added credential file exclusions
- `utils/helpers.py` - Updated `get_credentials()` to check config file first

### Testing

Verified:

- Seeded defaults are encrypted in config file
- Decryption works correctly
- `get_credentials()` returns correct values
- Files are properly git-ignored
- Default rotation and prompt-on-exhaustion flow are aligned

### Next Steps for User

1. Decide whether lab defaults should be enabled.
2. Optionally set `LAMIS_SEED_DEFAULTS` before first launch.
3. Run the app - it will automatically use the encrypted seeded defaults for initial attempts.
4. If the device needs other credentials, use the in-app prompt when ATLAS asks.

### Current Behavior

✓ If no `credentials_config.json` exists, ATLAS seeds one on first launch
✓ User-entered credentials are not persisted automatically
✓ Default attempts can be disabled entirely with `LAMIS_DISABLE_DEFAULT_CREDS=1`
