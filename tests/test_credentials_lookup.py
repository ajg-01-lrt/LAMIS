"""Tests for the username-keyed / vendor-keyed default credential lookups.

These confirm that every consumer in ATLAS can pull from the same Fernet
store via ``get_default_credential`` / ``get_default_credential_for_vendor``
instead of maintaining a private copy of the password.
"""
import unittest
from unittest.mock import patch


from utils import credentials as creds


class TestGetDefaultCredential(unittest.TestCase):

    def test_known_username_returns_pair(self):
        # The built-in seed always contains su / Ciena123 for Ciena gear.
        # Patch the lookup so we don't depend on the encrypted file on disk.
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('su', 'Ciena123'), ('cli', 'admin')],
        ):
            self.assertEqual(creds.get_default_credential('su'), ('su', 'Ciena123'))
            self.assertEqual(creds.get_default_credential('cli'), ('cli', 'admin'))
            self.assertEqual(creds.get_default_credential('admin'), ('admin', 'admin'))

    def test_unknown_username_returns_none(self):
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('su', 'Ciena123')],
        ):
            self.assertIsNone(creds.get_default_credential('does-not-exist'))

    def test_empty_username_returns_none(self):
        self.assertIsNone(creds.get_default_credential(''))
        self.assertIsNone(creds.get_default_credential(None))

    def test_defaults_disabled_returns_none(self):
        with patch.object(creds, '_defaults_disabled', return_value=True):
            self.assertIsNone(creds.get_default_credential('su'))


class TestGetDefaultCredentialForVendor(unittest.TestCase):

    def test_ciena_aliases_resolve_to_su(self):
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('su', 'Ciena123')],
        ):
            for alias in ('ciena', 'CIENA', 'ciena-rls', 'rls', '6500', 'ciena-6500'):
                self.assertEqual(
                    creds.get_default_credential_for_vendor(alias),
                    ('su', 'Ciena123'),
                    f"alias {alias!r} should resolve to su/Ciena123",
                )

    def test_nokia_1830_resolves_to_cli(self):
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('cli', 'admin')],
        ):
            self.assertEqual(
                creds.get_default_credential_for_vendor('nokia-1830'),
                ('cli', 'admin'),
            )
            self.assertEqual(
                creds.get_default_credential_for_vendor('1830'),
                ('cli', 'admin'),
            )

    def test_generic_nokia_resolves_to_admin(self):
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('cli', 'admin')],
        ):
            self.assertEqual(
                creds.get_default_credential_for_vendor('nokia'),
                ('admin', 'admin'),
            )

    def test_unknown_vendor_returns_none(self):
        self.assertIsNone(creds.get_default_credential_for_vendor('martian'))
        self.assertIsNone(creds.get_default_credential_for_vendor(''))


class TestBannerPreferredCredsRoutesThroughFernet(unittest.TestCase):

    def test_banner_preferred_creds_uses_vendor_lookup(self):
        from script_interface import DeviceIdentifier
        # Map rls/6500 → ciena, 1830 → nokia-1830
        with patch.object(
            creds, 'get_default_credentials_to_try',
            return_value=[('admin', 'admin'), ('cli', 'admin'), ('su', 'Ciena123')],
        ):
            self.assertEqual(
                DeviceIdentifier._banner_preferred_creds('rls'),
                ('su', 'Ciena123'),
            )
            self.assertEqual(
                DeviceIdentifier._banner_preferred_creds('6500'),
                ('su', 'Ciena123'),
            )
            self.assertEqual(
                DeviceIdentifier._banner_preferred_creds('1830'),
                ('cli', 'admin'),
            )
            self.assertIsNone(DeviceIdentifier._banner_preferred_creds('unknown'))


if __name__ == '__main__':
    unittest.main()
