"""Regression tests for the revamped TDS diagnostics frame.

Verifies that the Username/Password entry widgets are gone, that the
Ciena factory default credentials are baked into the module, and that
the auth-failure detector recognises the messages a TDS subprocess
emits when a device rejects the login.
"""
import inspect
import re
import unittest


class TestTDSFrameCredentialRevamp(unittest.TestCase):

    def _get_module_source(self):
        from gui import tds_frame
        return inspect.getsource(tds_frame), tds_frame

    def test_username_password_entries_removed(self):
        src, _ = self._get_module_source()
        # The new frame must not reference the legacy Tk entry widgets.
        self.assertNotIn('tds_username_entry', src)
        self.assertNotIn('tds_password_entry', src)
        # Nor build a "Username:" / "Password:" Label inside _build().
        # (The credential hint label is allowed to mention them in passing,
        # but no `text="Username:"` or `text="Password:"` Label widget.)
        self.assertFalse(
            re.search(r'tk\.Label\([^)]*text=["\']Username:["\']', src),
            "Username: Label widget should not be created anymore",
        )
        self.assertFalse(
            re.search(r'tk\.Label\([^)]*text=["\']Password:["\']', src),
            "Password: Label widget should not be created anymore",
        )

    def test_defaults_sourced_from_fernet_store(self):
        """The TDS frame must read Ciena defaults from the encrypted store
        (via ``get_default_credential_for_vendor``) rather than hardcoding
        the password in the GUI module."""
        src, mod = self._get_module_source()
        # No plaintext "Ciena123" anywhere in the GUI module.
        self.assertNotIn(
            "Ciena123", src,
            "TDS frame should not hardcode the Ciena password; "
            "it must come from credentials_config.json via Fernet.",
        )
        # Vendor lookup helper must be imported.
        self.assertIn("get_default_credential_for_vendor", src)
        # And the resolver returns the same pair the credentials helper
        # would have returned.
        from utils.credentials import get_default_credential_for_vendor
        expected = get_default_credential_for_vendor("ciena")
        self.assertEqual(mod._resolve_ciena_default(), expected or (None, None))

    def test_auth_failure_regex_matches_common_messages(self):
        _, mod = self._get_module_source()
        rx = mod._AUTH_FAILURE_RE
        for msg in (
            "Permission denied (publickey,password).",
            "Authentication failed.",
            "Login failed for user su",
            "Login incorrect",
            "Invalid credentials provided",
            "Invalid username or password",
            "Access denied for user",
            "Bad password",
        ):
            self.assertTrue(rx.search(msg), f"Did not match: {msg!r}")

    def test_auth_failure_regex_ignores_unrelated_text(self):
        _, mod = self._get_module_source()
        rx = mod._AUTH_FAILURE_RE
        for msg in (
            "Diagnostics completed successfully.",
            "Connection timed out",
            "No such file or directory",
            "permission to read the file granted",  # permission != denied
        ):
            self.assertFalse(rx.search(msg), f"False positive on: {msg!r}")

    def test_prompt_helper_present(self):
        src, _ = self._get_module_source()
        # Re-prompt logic must exist for both single-host and network-walk paths.
        self.assertIn('_prompt_user_for_credentials', src)
        # Both worker paths must call the blocking helper.
        self.assertIn('_ask_user_creds_blocking', src)


if __name__ == '__main__':
    unittest.main()
