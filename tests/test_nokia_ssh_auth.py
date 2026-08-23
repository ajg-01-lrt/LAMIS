"""Regression tests for utils.helpers.nokia_ssh_authenticate.

Field ground-truth (probed against a real PSS, HSTQTX02 at 172.16.0.1): the
shelf offers only ``['publickey','password']`` and logs in with **admin/admin
password auth, dropping straight to the CLI shell** — there is no passwordless
'cli'/'none' path and no inner Username:/Password: on that shelf. Two earlier
guesses (cli+none, cli+empty-password) both failed against it. The helper must
therefore try the admin/admin PASSWORD auth first, then fall back to the legacy
'cli' variants for older shelves that use them.
"""
from __future__ import annotations
import unittest

import paramiko

from utils.helpers import nokia_ssh_authenticate


class _FakeTransport:
    """Records auth attempts; each test chooses which method 'works'."""
    def __init__(self, *, pw_ok=None, none_ok=False, none_allowed=(),
                 kbd_ok=False):
        self.pw_ok = dict(pw_ok or {})     # (user, pw) -> True means success
        self.none_ok = none_ok
        self.none_allowed = list(none_allowed)
        self.kbd_ok = kbd_ok
        self.calls = []

    def auth_password(self, user, pw):
        self.calls.append(("password", user, pw))
        if self.pw_ok.get((user, pw)):
            return []
        raise paramiko.AuthenticationException("bad password")

    def auth_none(self, user):
        self.calls.append(("none", user))
        if self.none_ok:
            return []
        raise paramiko.BadAuthenticationType("no none", self.none_allowed)

    def auth_interactive(self, user, handler):
        self.calls.append(("kbd", user))
        if self.kbd_ok:
            return []
        raise paramiko.AuthenticationException("bad kbd")


class TestNokiaSshAuthenticate(unittest.TestCase):
    def test_admin_password_first_and_stops(self):
        """The real PSS case: admin/admin password auth succeeds immediately."""
        t = _FakeTransport(pw_ok={("admin", "admin"): True})
        nokia_ssh_authenticate(t, "admin", "admin")
        self.assertEqual(t.calls, [("password", "admin", "admin")])

    def test_custom_username(self):
        t = _FakeTransport(pw_ok={("root", "s3cret"): True})
        nokia_ssh_authenticate(t, "root", "s3cret")
        self.assertEqual(t.calls, [("password", "root", "s3cret")])

    def test_falls_back_to_cli_none_when_admin_password_fails(self):
        t = _FakeTransport(none_ok=True)          # admin pw fails, cli 'none' works
        nokia_ssh_authenticate(t, "admin", "admin")
        self.assertEqual(t.calls[0], ("password", "admin", "admin"))
        self.assertEqual(t.calls[-1], ("none", "cli"))

    def test_falls_back_to_cli_empty_password_when_only_password_offered(self):
        t = _FakeTransport(none_allowed=["publickey", "password"],
                           pw_ok={("cli", ""): True})
        nokia_ssh_authenticate(t, "admin", "admin")
        self.assertIn(("none", "cli"), t.calls)
        self.assertEqual(t.calls[-1], ("password", "cli", ""))

    def test_falls_back_to_keyboard_interactive(self):
        t = _FakeTransport(none_allowed=["keyboard-interactive"], kbd_ok=True)
        nokia_ssh_authenticate(t, "admin", "admin")
        self.assertEqual(t.calls[-1], ("kbd", "cli"))

    def test_raises_when_no_usable_method(self):
        t = _FakeTransport(none_allowed=["publickey"])   # no password/none/kbd usable
        with self.assertRaises(paramiko.SSHException):
            nokia_ssh_authenticate(t, "admin", "admin")


if __name__ == "__main__":
    unittest.main()
