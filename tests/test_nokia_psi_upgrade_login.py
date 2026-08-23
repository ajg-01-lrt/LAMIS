"""Regression tests for the Nokia PSI *upgrade* login transport.

The PSI upgrade used to log in over SSH, but PSI's usable CLI is only
reachable via the Telnet getty three-stage (``login:``->``cli``,
``Username:``->``admin``, ``Password:``->``admin``) — SSH-as-admin
authenticates but then dead-ends at the alarm banner. These tests pin the
Telnet transport, the getty dialog order, the auto-allowlisting, and the
``_TelnetChannel`` adapter (recv/send + peer-close detection used by the
activate/reboot phase).
"""
from __future__ import annotations

import socket
import unittest
from unittest import mock

from scripts.Network.Nokia_PSI_Upgrade import (
    NokiaPSIUpgradeScript,
    _strip_telnet_noise,
    _TelnetChannel,
)

_PROMPT = b"PSI-NODE#"


class FakeTelnet:
    """Scripted stand-in for utils.telnet.Telnet.

    Login prompts are served by ``read_until``; everything after the password
    (banner, command output) is queued for ``read_very_eager``. A real
    ``socketpair`` backs ``_sock`` so the adapter's select()/MSG_PEEK
    peer-close detection exercises real socket behaviour.
    """

    def __init__(self, host, timeout=None, *, skip_ssh_probe=False, purpose=None,
                 login_prompts=None, post_password=None):
        self.host = host
        self.skip_ssh_probe = skip_ssh_probe
        self.purpose = purpose
        self._peer, self._sock = socket.socketpair()
        self._login = list(
            login_prompts
            if login_prompts is not None
            else [b"\r\nHOST login: ", b"Username: ", b"Password: "]
        )
        self._post_password = (
            post_password
            if post_password is not None
            else b"\r\nLast Login: Mon\r\nAlarm Status: OK\r\n" + _PROMPT
        )
        self.out = bytearray()
        self.written = []
        self.closed = False

    def read_until(self, match, timeout=None):
        return self._login.pop(0) if self._login else b""

    def write(self, data):
        self.written.append(data)
        # The third write is the inner password; queue the post-login banner.
        if len(self.written) == 3:
            self.out.extend(self._post_password)

    def read_very_eager(self):
        data = bytes(self.out)
        self.out.clear()
        return data

    def close(self):
        self.closed = True
        for s in (self._peer, self._sock):
            try:
                s.close()
            except OSError:
                pass


class ConnectLoginTests(unittest.TestCase):
    def _make(self, **kw):
        return NokiaPSIUpgradeScript(
            ip_address="172.16.0.1",
            username="cli",
            password="admin",
            inner_username="admin",
            inner_password="admin",
            software_filename="1830OLS-25.3-3",
            **kw,
        )

    def test_connect_drives_getty_three_stage_over_telnet(self):
        fake = FakeTelnet("172.16.0.1")
        allow = mock.Mock()
        with mock.patch("utils.telnet.Telnet", return_value=fake) as telnet_ctor, \
             mock.patch("utils.telnet_policy.add_telnet_allowlist", allow):
            script = self._make()
            session = script._connect()

        self.assertIsNotNone(session, "login should reach the shell prompt")
        # getty dialog order: login name, inner user, inner password.
        self.assertEqual(fake.written, [b"cli\n", b"admin\n", b"admin\n"])
        self.assertEqual(script._prompt, _PROMPT.decode())
        # Telnet, not SSH: allowlist-gated + SSH-probe waived.
        _, kwargs = telnet_ctor.call_args
        self.assertTrue(kwargs.get("skip_ssh_probe"))
        # The operator-entered IP is auto-allowlisted.
        allow.assert_called_once()
        self.assertEqual(allow.call_args.args[0], "172.16.0.1")

    def test_connect_acks_post_login_yn_banner(self):
        # A EULA Y/n banner precedes the prompt; _settle_shell must ack it.
        fake = FakeTelnet(
            "172.16.0.1",
            post_password=b"\r\nAccept license? (y/n) ",
        )
        # After the operator's "y", deliver the prompt.
        orig_write = fake.write

        def write(data):
            orig_write(data)
            if data == b"y\n":
                fake.out.extend(b"\r\n" + _PROMPT)

        fake.write = write
        with mock.patch("utils.telnet.Telnet", return_value=fake), \
             mock.patch("utils.telnet_policy.add_telnet_allowlist", mock.Mock()):
            script = self._make()
            session = script._connect()

        self.assertIsNotNone(session)
        self.assertIn(b"y\n", fake.written)
        self.assertEqual(script._prompt, _PROMPT.decode())

    def test_connect_returns_none_when_login_rejected(self):
        fake = FakeTelnet(
            "172.16.0.1",
            post_password=b"\r\nLogin incorrect\r\nHOST login: ",
        )
        with mock.patch("utils.telnet.Telnet", return_value=fake), \
             mock.patch("utils.telnet_policy.add_telnet_allowlist", mock.Mock()):
            script = self._make()
            session = script._connect()

        self.assertIsNone(session)


class TelnetChannelAdapterTests(unittest.TestCase):
    def _adapter(self):
        fake = FakeTelnet("172.16.0.1")
        return _TelnetChannel(fake), fake

    def test_send_encodes_str(self):
        chan, fake = self._adapter()
        n = chan.send("config foo\n")
        self.assertEqual(fake.written, [b"config foo\n"])
        self.assertEqual(n, len(b"config foo\n"))
        chan.close()

    def test_recv_ready_and_chunking(self):
        chan, fake = self._adapter()
        fake.out.extend(b"hello world")
        self.assertTrue(chan.recv_ready())
        self.assertEqual(chan.recv(5), b"hello")
        self.assertEqual(chan.recv(65535), b" world")
        # Nothing left, socket still open -> not ready, not closed.
        self.assertFalse(chan.recv_ready())
        self.assertFalse(chan.closed)
        chan.close()

    def test_peer_close_detected_for_reboot(self):
        chan, fake = self._adapter()
        self.assertFalse(chan.exit_status_ready())
        # Simulate the PSI dropping the connection on activate/reboot.
        fake._peer.close()
        self.assertTrue(chan.exit_status_ready())
        self.assertTrue(chan.closed)
        chan.close()


class PromptDetectionTests(unittest.TestCase):
    """The live port-8000 hang: the PSI's Telnet stream carries stray NUL
    bytes whose placement varies per command; a NUL landing after the prompt
    broke ``endswith(prompt)`` and hung the command for the full 30s timeout.
    """

    def _script(self, **kw):
        return NokiaPSIUpgradeScript(
            ip_address="172.16.0.1", software_filename="X", **kw
        )

    def test_strip_telnet_noise_removes_cr_and_nul(self):
        self.assertEqual(_strip_telnet_noise(b"a\r\x00b#\x00"), b"ab#")

    def test_read_until_prompt_matches_prompt_with_trailing_nul(self):
        fake = FakeTelnet("172.16.0.1")
        chan = _TelnetChannel(fake)
        script = self._script()
        script._prompt = "usgng1-l9i2#"
        # Command echo + NUL before AND after the prompt (as seen on the wire).
        fake.out.extend(
            b"config software server port 8000\r\n\x00usgng1-l9i2#\x00"
        )
        out = script._read_until_prompt(chan, timeout=5)
        self.assertIsNotNone(out, "prompt with trailing NUL must still match")
        chan.close()

    def test_confirmation_answer_prefers_full_yes(self):
        # The real port-change gate wants the full word "yes".
        gate = "Continue?\r\nEnter 'yes' to confirm, 'no' to cancel:  "
        self.assertEqual(
            NokiaPSIUpgradeScript._confirmation_answer(gate), "yes"
        )
        # Bare y/n gates still answer "y".
        self.assertEqual(
            NokiaPSIUpgradeScript._confirmation_answer("Proceed? (y/n)"), "y"
        )
        # No gate -> no answer.
        self.assertIsNone(
            NokiaPSIUpgradeScript._confirmation_answer("done\nusgng1-l9i2#")
        )

    def test_read_until_prompt_auto_confirms_port_change_gate(self):
        # Reproduces the live 09:09 hang: config software server port 8000
        # opens a "Enter 'yes' to confirm" gate that must be answered.
        fake = FakeTelnet("172.16.0.1")
        chan = _TelnetChannel(fake)
        script = self._script()
        script._prompt = "usgng1-l9i2#"
        fake.out.extend(
            b"config software server port 8000\r\n\r\x00\r\n\r\x00WARNING:  "
            b"Changing the port number may block the service.\r\n\r\x00"
            b"Continue?\r\n\r\x00Enter 'yes' to confirm, 'no' to cancel:  "
        )
        orig_write = fake.write

        def write(data):
            orig_write(data)
            if data == b"yes\n":
                fake.out.extend(b"\r\n\x00usgng1-l9i2#")

        fake.write = write
        out = script._read_until_prompt(chan, timeout=5)
        self.assertIsNotNone(out, "gate should be auto-confirmed, not timed out")
        self.assertIn(b"yes\n", fake.written)
        self.assertNotIn(b"y\n", fake.written, "must send full 'yes', not 'y'")
        chan.close()

    def test_read_until_prompt_dumps_bytes_on_timeout(self):
        fake = FakeTelnet("172.16.0.1")
        chan = _TelnetChannel(fake)
        logs = []
        script = self._script(output_callback=logs.append)
        script._prompt = "usgng1-l9i2#"
        fake.out.extend(b"partial output, no prompt\r\n")
        out = script._read_until_prompt(chan, timeout=0.5)
        self.assertIsNone(out)
        self.assertTrue(
            any("Last bytes" in m for m in logs),
            f"timeout should dump received bytes; got {logs!r}",
        )
        chan.close()


class StatusParsingTests(unittest.TestCase):
    """The load/activate timing bug: the per-stage lines all say
    "Completed ... 100% complete" from the first poll, so a loose
    "complete" match fired activate before the load transferred. Completion
    must come from the aggregate header fields only.
    """

    # Real header text from the shelf, with the per-stage download-script
    # lines that used to trip the loose matcher.
    _STAGES = (
        "\r\nSoftware Download Script (Timezone: UTC):\r\n"
        "Stage=0 Step=1  CARDTYPE: MEC2L\r\n"
        "ACTION: Audit node          Completed       100% complete\r\n"
        "RESULT: Success\r\n"
        "Stage=5 Step=0  CARDTYPE: MEC2L\r\n"
        "ACTION: Activate            Planned           0% complete\r\n"
        "RESULT: None\r\n"
    )

    def _status(self, *, status, percent, path="True", operation="Load"):
        return (
            "Software Upgrade Information:\r\n"
            "Software Server IP             : 172.16.0.101\r\n"
            "Working Release                : 1830OLS-25.3-2\r\n"
            f"Operation                      : {operation}\r\n"
            f"Operation Status               : {status}\r\n"
            f"Percent Completion             : {percent}%\r\n"
            f"Upgrade Path Available         : {path}\r\n"
            "URL                            : http://172.16.0.101:8000/CC/x\r\n"
            + self._STAGES
        )

    def test_parse_status_reads_header_not_stage_lines(self):
        info = NokiaPSIUpgradeScript._parse_status(
            self._status(status="In Progress", percent=0)
        )
        self.assertEqual(info["operation"], "Load")
        self.assertEqual(info["status"], "In Progress")
        self.assertEqual(info["percent"], 0)
        self.assertEqual(info["path"], "True")
        self.assertEqual(info["working_release"], "1830OLS-25.3-2")

    def test_in_progress_is_not_complete(self):
        # The exact false-positive state from the 09:17 log.
        info = NokiaPSIUpgradeScript._parse_status(
            self._status(status="In Progress", percent=0)
        )
        self.assertFalse(NokiaPSIUpgradeScript._load_complete(info))

    def test_completed_100_true_is_complete(self):
        info = NokiaPSIUpgradeScript._parse_status(
            self._status(status="Completed", percent=100, path="True")
        )
        self.assertTrue(NokiaPSIUpgradeScript._load_complete(info))

    def test_completed_but_path_false_is_not_complete(self):
        info = NokiaPSIUpgradeScript._parse_status(
            self._status(status="Completed", percent=100, path="False")
        )
        self.assertFalse(NokiaPSIUpgradeScript._load_complete(info))

    def test_operation_status_line_not_confused_with_operation(self):
        # "Operation Status : ..." must not be captured by the Operation field.
        info = NokiaPSIUpgradeScript._parse_status(
            self._status(status="In Progress", percent=42)
        )
        self.assertEqual(info["operation"], "Load")
        self.assertEqual(info["percent"], 42)


class ActivateTests(unittest.TestCase):
    def test_activate_rejection_fails_fast(self):
        # The 09:17 symptom: activate sent too early -> shelf rejects with
        # "Unable to complete request / Operation in progress". Must fail, not
        # hang waiting for a reboot that never comes.
        fake = FakeTelnet("172.16.0.1")
        chan = _TelnetChannel(fake)
        logs = []
        script = NokiaPSIUpgradeScript(
            ip_address="172.16.0.1", software_filename="X",
            output_callback=logs.append,
        )
        script._prompt = "usgng1-l9i2#"
        orig_write = fake.write

        def write(data):
            orig_write(data)
            if b"activate" in data:
                fake.out.extend(
                    b"\r\n\x00Unable to complete request.\r\n"
                    b"\x00Error: Operation in progress\r\n\x00usgng1-l9i2#"
                )

        fake.write = write
        ok = script._activate(chan)
        self.assertFalse(ok)
        self.assertTrue(any("REJECTED" in m for m in logs))
        # No manual-commit notice on a rejected activate.
        self.assertFalse(any("MANUAL COMMIT" in m for m in logs))
        chan.close()

    def test_activation_notice_logs_commit_and_release(self):
        # The script logs a toned-down record (the emphatic MANUAL COMMIT /
        # safe-to-disconnect popup is raised by the GUI after DHCP restore).
        logs = []
        script = NokiaPSIUpgradeScript(
            ip_address="172.16.0.1", software_filename="1830OLS-25.3-2",
            output_callback=logs.append,
        )
        script._working_release = "1830OLS-25.3-2"
        script._notify_activation_in_progress()
        joined = "\n".join(logs).lower()
        self.assertIn("manual commit", joined)
        self.assertIn("1830ols-25.3-2", joined)


if __name__ == "__main__":
    unittest.main()
