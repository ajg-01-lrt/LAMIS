"""Tests for the network-aware retry in ``ensure_host_key_known``.

The Nokia G42 upgrade flow exposed the bug: after the user clicks
``Apply Static IP`` in Software Upgrades, the OS takes a moment to
bring the link-local interface up. The first pre-verify probe to
169.254.0.1 fires too early and hits ``OSError: [WinError 10051]``
(WSAENETUNREACH). The pre-existing handler caught that as a
generic exception and returned False, the upgrade script logged
"Host key verification failed", and the operator was misled
(they assumed key/auth issue, not interface settling).

Two-part fix in ``ensure_host_key_known``:
  1. ``_is_transient_network_error`` classifies transient socket
     errors so the retry path knows which exceptions to ride out.
  2. The probe loop retries up to ``max_retries`` times with a
     short delay, AND on final failure logs an operator-actionable
     "check cable / adapter / power" message instead of "host key
     verification failed".
"""
from __future__ import annotations
import logging
import socket
import unittest
from unittest.mock import patch, MagicMock

from utils.helpers import (
    _TRANSIENT_NETWORK_ERRNOS,
    _is_transient_network_error,
    _short_socket_error,
    ensure_host_key_known,
)


class TestClassifier(unittest.TestCase):
    """``_is_transient_network_error`` is the gate on the retry loop;
    a misclassification here either turns real refusals into infinite
    retries or makes the operator wait through retries on a permanent
    error. Pin every classification we care about."""

    def test_winerror_10051_is_transient(self):
        # The actual G42 case from the operator's log.
        exc = OSError(10051, "A socket operation was attempted to an "
                              "unreachable network")
        exc.winerror = 10051
        self.assertTrue(_is_transient_network_error(exc))

    def test_winerror_10060_timeout_is_transient(self):
        exc = OSError(10060, "Connection timed out")
        exc.winerror = 10060
        self.assertTrue(_is_transient_network_error(exc))

    def test_winerror_10061_refused_is_transient(self):
        # Device booting, SSH not up yet — worth retrying.
        exc = OSError(10061, "Connection refused")
        exc.winerror = 10061
        self.assertTrue(_is_transient_network_error(exc))

    def test_connection_refused_error_is_transient(self):
        self.assertTrue(_is_transient_network_error(ConnectionRefusedError()))

    def test_timeout_error_is_transient(self):
        self.assertTrue(_is_transient_network_error(TimeoutError()))

    def test_value_error_is_not_transient(self):
        # Non-network errors must NOT trip the retry path; they should
        # fail fast.
        self.assertFalse(_is_transient_network_error(ValueError("bad")))

    def test_generic_exception_is_not_transient(self):
        self.assertFalse(_is_transient_network_error(Exception("?")))

    def test_oserror_with_unknown_errno_is_not_transient(self):
        # Don't blindly retry every OSError — only the recognized
        # network-layer codes.
        exc = OSError(2, "No such file or directory")
        self.assertFalse(_is_transient_network_error(exc))

    def test_table_covers_g42_scenario(self):
        # Defensive regression guard: the literal errno from the
        # operator's log must be in the table.
        self.assertIn(10051, _TRANSIENT_NETWORK_ERRNOS)


class TestShortSocketError(unittest.TestCase):

    def test_winerror_renders_with_errno_and_class(self):
        exc = OSError(10051, "Unreachable")
        exc.winerror = 10051
        s = _short_socket_error(exc)
        self.assertIn("10051", s)
        self.assertIn("OSError", s)

    def test_no_errno_falls_back_to_message(self):
        exc = RuntimeError("custom failure")
        s = _short_socket_error(exc)
        self.assertIn("RuntimeError", s)
        self.assertIn("custom failure", s)


class TestEnsureHostKeyRetryBehavior(unittest.TestCase):
    """Drive ``ensure_host_key_known`` with a patched paramiko and a
    fast-failing connect to verify the retry semantics directly."""

    def _patched_module(self):
        """Patch ``paramiko`` inside utils.helpers so the function
        runs end-to-end without touching the network. Returns the
        MagicMock SSHClient instance the test can drive.

        SSHException / AuthenticationException must be DISTINCT
        narrow types — making them ``Exception`` collapses the
        except chain and catches every OSError as SSHException,
        which is exactly what the retry handler was supposed to
        skip past. (Tests originally tripped on this bug.)
        """
        from utils import helpers

        class _FakeSSHException(Exception):
            pass

        class _FakeAuthException(Exception):
            pass

        mock_paramiko = MagicMock()
        mock_paramiko.SSHException = _FakeSSHException
        mock_paramiko.AuthenticationException = _FakeAuthException
        # The SSHClient class returns a fresh mock each instantiation.
        # We need to control connect() per attempt; route all
        # instantiations to the SAME object so the test sees the
        # cumulative call count.
        client_mock = MagicMock()
        client_mock.get_host_keys.return_value.lookup.return_value = None
        mock_paramiko.SSHClient = MagicMock(return_value=client_mock)
        return mock_paramiko, client_mock

    def test_retries_on_transient_network_error_then_succeeds(self):
        from utils import helpers
        mock_paramiko, client = self._patched_module()
        # Two transient failures then a success. Use a stateful
        # closure for lookup so a counter — not a fixed-length
        # side_effect — drives the "key shows up after save" behavior
        # and the test isn't brittle to extra lookup calls.
        unreachable = OSError(10051, "Unreachable")
        unreachable.winerror = 10051
        client.connect.side_effect = [unreachable, unreachable, None]
        state = {"saved": False}

        def fake_lookup(_target):
            return "<key>" if state["saved"] else None
        client.get_host_keys.return_value.lookup.side_effect = fake_lookup

        def fake_save(_client, _path):
            state["saved"] = True
        with patch.object(helpers, "paramiko", mock_paramiko), \
             patch.object(helpers, "safe_load_host_keys"), \
             patch.object(helpers, "safe_save_host_keys",
                          side_effect=fake_save), \
             patch.object(helpers, "get_known_hosts_path",
                          return_value="/tmp/kh"), \
             patch.object(helpers, "get_host_key_policy"), \
             patch.object(helpers, "time") as mock_time, \
             patch.object(helpers, "restrict_path_to_owner"):
            result = ensure_host_key_known(
                "169.254.0.1", retry_delay=0.0,
            )
        self.assertTrue(result)
        self.assertEqual(client.connect.call_count, 3)
        # First two failures triggered sleeps; the third success did
        # not. mock_time.sleep is called exactly twice (once per
        # transient failure before final attempt).
        self.assertEqual(mock_time.sleep.call_count, 2)

    def test_gives_up_after_max_retries_on_persistent_unreachable(self):
        from utils import helpers
        mock_paramiko, client = self._patched_module()
        unreachable = OSError(10051, "Unreachable")
        unreachable.winerror = 10051
        client.connect.side_effect = unreachable
        client.get_host_keys.return_value.lookup.return_value = None
        with patch.object(helpers, "paramiko", mock_paramiko), \
             patch.object(helpers, "safe_load_host_keys"), \
             patch.object(helpers, "get_known_hosts_path",
                          return_value="/tmp/kh"), \
             patch.object(helpers, "get_host_key_policy"), \
             patch.object(helpers, "time"):
            with self.assertLogs(level="WARNING") as cm:
                result = ensure_host_key_known(
                    "169.254.0.1", max_retries=3, retry_delay=0.0,
                )
        self.assertFalse(result)
        self.assertEqual(client.connect.call_count, 3)
        # Operator-facing final warning must mention the actionable
        # cause (cable / adapter / power), not just say "host key
        # verification failed".
        joined = "\n".join(cm.output)
        self.assertIn("Check the Ethernet cable", joined)
        self.assertIn("169.254.0.1", joined)

    def test_does_not_retry_on_non_transient_error(self):
        # ValueError isn't network-shaped — should fail on first try
        # without sleeping.
        from utils import helpers
        mock_paramiko, client = self._patched_module()
        client.connect.side_effect = ValueError("invalid argument")
        client.get_host_keys.return_value.lookup.return_value = None
        with patch.object(helpers, "paramiko", mock_paramiko), \
             patch.object(helpers, "safe_load_host_keys"), \
             patch.object(helpers, "get_known_hosts_path",
                          return_value="/tmp/kh"), \
             patch.object(helpers, "get_host_key_policy"), \
             patch.object(helpers, "time") as mock_time:
            result = ensure_host_key_known(
                "169.254.0.1", max_retries=3, retry_delay=0.0,
            )
        self.assertFalse(result)
        self.assertEqual(client.connect.call_count, 1)
        mock_time.sleep.assert_not_called()

    def test_existing_known_key_short_circuits(self):
        # Sanity: the fast-path that returns True for already-trusted
        # hosts must not be affected by the retry refactor.
        from utils import helpers
        mock_paramiko, client = self._patched_module()
        client.get_host_keys.return_value.lookup.return_value = "<existing key>"
        with patch.object(helpers, "paramiko", mock_paramiko), \
             patch.object(helpers, "safe_load_host_keys"), \
             patch.object(helpers, "get_known_hosts_path",
                          return_value="/tmp/kh"), \
             patch.object(helpers, "time") as mock_time:
            result = ensure_host_key_known("169.254.0.1")
        self.assertTrue(result)
        client.connect.assert_not_called()
        mock_time.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
