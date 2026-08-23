# Credential Prompt System - Implementation Summary

## What Was Added

✅ **Default Credentials in Config File**

- Built-in default seed is encrypted into `%APPDATA%\ATLAS\credentials_config.json`
- Multiple defaults can be rotated in order
- The defaults list is the only credential data persisted on disk

✅ **Automatic Credential Prompt on Auth Failure**

- When SSH authentication fails → prompts user for new credentials
- When Telnet authentication fails → prompts user for new credentials
- User can enter new credentials via GUI dialog
- New credentials are used for the parked/retry path
- User-entered credentials are not automatically saved back to disk

✅ **GUI Credential Dialog**

- Clean dialog box for entering username and password
- Shows when authentication fails
- User can "Save & Continue" or "Cancel"
- Works in multi-threaded environment

## How It Works

### Flow Diagram

```text
App tries to connect with loaded credentials
    ↓
SSH/Telnet Auth Fails
    ↓
handle_credential_failure() called
    ↓
Rotate through remaining seeded defaults
    ↓
Defaults exhausted
    ↓
Show GUI Prompt Dialog
    ↓
User enters new credentials
    ↓
Connection retried with new credentials
    ↓
Success → Continue with device identification
    Or Fail → Return to user
```

## Usage - First Run

**Step 1:** Start the app

```bash
python main.py
```

**Step 2:** The app loads default credentials (admin/admin123) from `credentials_config.json`

**Step 2:** The app loads the first encrypted seeded default from `%APPDATA%\ATLAS\credentials_config.json`

**Step 3:** If the seeded defaults don't work:

- Authentication fails → a dialog prompt appears automatically
- Enter your actual device username and password
- Click "Save & Continue"
- App retries with your credentials for that run / retry path

## Usage - Updating Credentials

**Option 1:** Via automatic prompt (when auth fails)

- Just follow the steps above

**Option 2:** Re-seed defaults before launch

```bash
set LAMIS_SEED_DEFAULTS=username:password
python main.py
```

## Files Changed/Created

**New Files:**

- `utils/credentials.py` - Enhanced with `prompt_for_credentials_gui()` function
- Sample template added to documentation

**Modified Files:**

- `script_interface.py`:
  - Added import for credential retry functions
  - Added `handle_credential_failure()` function
  - Enhanced SSH auth failure handling with retry logic
  - Enhanced Telnet auth failure handling with retry logic
- `utils/helpers.py`:
  - Updated `get_credentials()` to check config file first
- `.gitignore`:
  - Added credential file exclusions

## Key Features

1. **Seamless Experience**: Default credentials work out of the box
2. **Automatic Recovery**: Failed auth triggers immediate credential prompt
3. **Smart Retry**: Retries connection with new credentials automatically
4. **Secure Storage**: Seeded defaults are encrypted at rest
5. **Non-Interactive**: Works in GUI environment without stopping execution flow
6. **Predictable Rotation**: Defaults are tried in a fixed order before prompting

## Security

✓ Seeded defaults are encrypted with a unique key per installation
✓ Operator-entered credentials are not persisted automatically
✓ Both key and config files git-ignored (never committed)
✓ File permissions set to 600 (owner read/write only)

## Error Handling

- If user cancels dialog → connection fails gracefully
- If new credentials also fail → appropriate error message shown
- Telnet and SSH both have independent retry logic
- All errors logged for debugging

## Testing

To test the credential prompt system:

1. Run the app with invalid credentials in config file
2. Try to connect to a device
3. Observe the credential prompt dialog
4. Enter new credentials
5. Watch the connection retry
6. Verify the device retry succeeds without expecting the entered credentials to be stored
