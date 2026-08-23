"""Tests for the Waveserver 5 upgrade flow (serial + SSH).

The hardware integration isn't testable in unit tests, but we can pin
down the parts that ARE pure logic — version derivation from a filename,
the status-table parser, and the prompt regex extension. We also add a
source-level shape check so a future refactor that removes a required
constructor kwarg gets caught at import time.
"""
from __future__ import annotations
import inspect
import socket
import unittest

from utils.serial_helpers import _PROMPT_RE, _SHELL_RE


class TestListNicsFiltersWirelessAndDisconnected(unittest.TestCase):
    """The NIC dropdown is for wired Ethernet adapters operators use
    for serial-console upgrade sessions. Wi-Fi, Bluetooth, the MS
    virtual ``Local Area Connection*`` adapters Windows creates for
    hotspot/Wi-Fi Direct, and disconnected Ethernet ports must NOT
    appear -- picking the wrong one wastes the operator's time
    chasing netsh failures that have nothing to do with the actual
    upgrade. Use psutil's net_if_stats + a name-pattern allowlist.
    """

    def setUp(self):
        from unittest import mock
        from gui import software_upgrade_frame
        self.mod = software_upgrade_frame
        self.mock = mock

    def _make_addr(self, family, ip):
        # Lightweight stand-in for psutil snicaddr; psutil only reads
        # ``.family`` and ``.address`` here.
        class _A:
            pass
        a = _A()
        a.family = family
        a.address = ip
        a.netmask = None
        a.broadcast = None
        a.ptp = None
        return a

    def _make_stat(self, isup):
        class _S:
            pass
        s = _S()
        s.isup = isup
        s.duplex = 0
        s.speed = 0
        s.mtu = 0
        return s

    def test_excludes_wifi(self):
        addrs = {
            "Wi-Fi": [self._make_addr(socket.AF_INET, "10.9.103.217")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {
            "Wi-Fi": self._make_stat(True),
            "Ethernet 8": self._make_stat(True),
        }
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])

    def test_excludes_local_area_connection_star(self):
        # MS virtual hotspot / Wi-Fi Direct adapters -- always present,
        # never useful for our wired-upgrade flow.
        addrs = {
            "Local Area Connection* 1": [self._make_addr(socket.AF_INET, "169.254.1.2")],
            "Local Area Connection* 2": [self._make_addr(socket.AF_INET, "169.254.3.4")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {n: self._make_stat(True) for n in addrs}
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])

    def test_excludes_disconnected_ethernet(self):
        # ``Media disconnected`` Ethernet ports must be hidden so the
        # operator can't pick one only to have netsh refuse to bind.
        addrs = {
            "Ethernet 2": [self._make_addr(socket.AF_INET, "169.254.0.1")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {
            "Ethernet 2": self._make_stat(False),  # media disconnected
            "Ethernet 8": self._make_stat(True),
        }
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])

    def test_excludes_bluetooth_and_loopback(self):
        addrs = {
            "Bluetooth Network Connection": [self._make_addr(socket.AF_INET, "169.254.99.99")],
            "Loopback Pseudo-Interface 1": [self._make_addr(socket.AF_INET, "127.0.0.1")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {n: self._make_stat(True) for n in addrs}
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])

    def test_excludes_hyper_v_and_vmware_virtual_adapters(self):
        # These are Hyper-V / VMware host-only switches -- netsh
        # commands against them don't reach physical hardware.
        addrs = {
            "vEthernet (WSL)": [self._make_addr(socket.AF_INET, "172.30.16.1")],
            "VMware Network Adapter VMnet1": [self._make_addr(socket.AF_INET, "192.168.43.1")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {n: self._make_stat(True) for n in addrs}
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])

    def test_keeps_multiple_connected_ethernets(self):
        # If the operator has two physical Ethernet adapters in use
        # (e.g. Cat-5 + USB-Ethernet), both should remain selectable.
        addrs = {
            "Ethernet 1": [self._make_addr(socket.AF_INET, "10.0.0.5")],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
        }
        stats = {n: self._make_stat(True) for n in addrs}
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        # Alphabetical order; both kept.
        self.assertEqual(result, ["Ethernet 1", "Ethernet 8"])

    def test_real_log_scenario_only_ethernet_8_remains(self):
        # Reproduces the exact ipconfig output the operator shared
        # in the field log: Ethernet 2/3 disconnected, Ethernet 8
        # connected on APIPA, Wi-Fi on DHCP, two Local Area
        # Connection* virtual adapters, Bluetooth. Should yield
        # just Ethernet 8.
        addrs = {
            "Ethernet 2": [],  # no IPv4 because disconnected
            "Ethernet 3": [],
            "Ethernet 8": [self._make_addr(socket.AF_INET, "169.254.107.225")],
            "Local Area Connection* 1": [],
            "Local Area Connection* 2": [],
            "Wi-Fi": [self._make_addr(socket.AF_INET, "10.9.103.217")],
            "Bluetooth Network Connection": [],
        }
        stats = {
            "Ethernet 2": self._make_stat(False),
            "Ethernet 3": self._make_stat(False),
            "Ethernet 8": self._make_stat(True),
            "Local Area Connection* 1": self._make_stat(False),
            "Local Area Connection* 2": self._make_stat(False),
            "Wi-Fi": self._make_stat(True),
            "Bluetooth Network Connection": self._make_stat(False),
        }
        with self.mock.patch.object(self.mod.psutil, "net_if_addrs", return_value=addrs), \
             self.mock.patch.object(self.mod.psutil, "net_if_stats", return_value=stats):
            result = self.mod._list_nics()
        self.assertEqual(result, ["Ethernet 8"])


class TestSetStaticIpv4PreCleansStaleBinding(unittest.TestCase):
    """A previous run that didn't restore DHCP cleanly leaves the
    same static IP bound on the NIC. The next ``netsh interface
    ipv4 set address … static`` then fails with ``The object already
    exists.`` even after the operator clicks Restore DHCP first,
    because Windows persists the binding in the registry.

    The fix: ``_set_static_ipv4`` always runs an idempotent
    ``delete address … addr=<ip>`` first, then the ``set``. The
    delete may legitimately fail (nothing to remove) -- that's fine.
    """

    def test_helper_exists_and_takes_nic_ip_mask(self):
        from gui import software_upgrade_frame
        self.assertTrue(hasattr(software_upgrade_frame, "_set_static_ipv4"))
        sig = inspect.signature(software_upgrade_frame._set_static_ipv4)
        names = list(sig.parameters)
        # Three positional args: nic, ip, mask.
        self.assertEqual(names[:3], ["nic", "ip", "mask"])

    def test_helper_deletes_addr_before_set(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._set_static_ipv4)
        # Two netsh calls: delete then set. Pin both.
        self.assertIn('"delete", "address"', src)
        self.assertIn('"set", "address"', src)
        # The delete must come BEFORE the set in source order (the
        # whole point is to clear stale bindings first).
        del_idx = src.find('"delete", "address"')
        set_idx = src.find('"set", "address"')
        self.assertLess(del_idx, set_idx)
        # The delete targets the SAME IP we're about to set
        # (``addr=ip`` arg), not the catch-all ``addr=*`` which
        # would nuke unrelated bindings.
        self.assertIn('f"addr={ip}"', src)

    def test_helper_swallows_delete_failure(self):
        # Delete fails when the address wasn't bound -- that's the
        # happy first-run case. Helper must not propagate the
        # delete's success/failure back to the caller; it must fall
        # through to the set call regardless.
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._set_static_ipv4)
        # The only ``return`` in the function must be the one that
        # returns the SET result. No ``if not del_ok: return ...``
        # bail-out. Search for the bail-out pattern explicitly.
        self.assertNotIn(
            "if not del_ok:\n\t\treturn", src.replace("    ", "\t"),
            "helper must NOT bail when the pre-clean delete fails --"
            " 'address not found' is the happy first-run case",
        )


class TestStaticSetCallSitesUseHelper(unittest.TestCase):
    """Every static-IP set path in the upgrade frame must route
    through ``_set_static_ipv4`` so the stale-binding fix is
    applied consistently (RLS, G42, PSI, Waveserver 5, and the
    standalone "Set Static IP" button)."""

    def test_no_inline_set_address_static_outside_helper(self):
        from gui import software_upgrade_frame
        import pathlib
        # Source-level scan of the whole module: only the helper
        # itself should contain the literal ``"set", "address",
        # ... "static"`` pattern. Every other call site should use
        # the helper. (We don't have to parse Python AST -- a tight
        # textual regex over the file is enough.)
        path = pathlib.Path(software_upgrade_frame.__file__)
        text = path.read_text(encoding="utf-8")
        # Count occurrences of the literal pattern. Should be EXACTLY
        # one (inside _set_static_ipv4).
        n = text.count('"set", "address"')
        # DHCP-restore call uses ``"set", "address",
        # f"name={nic}", "source=dhcp"`` -- that's a DIFFERENT
        # pattern (source=dhcp, not static), so it shows up too.
        # Be permissive: just guarantee that ALL inline static-set
        # patterns are gone.
        # Find every set address line and check what follows.
        import re
        lines_with_static = []
        for m in re.finditer(r'"set", "address",[\s\S]{0,120}?"static"', text):
            # Find which function/method contains this match.
            preceding = text[:m.start()].splitlines()
            # Walk back to the nearest def or class.
            ctx = ""
            for ln in reversed(preceding):
                stripped = ln.strip()
                if stripped.startswith("def ") or stripped.startswith("class "):
                    ctx = stripped
                    break
            lines_with_static.append(ctx)
        # The ONLY legitimate location is inside _set_static_ipv4.
        unexpected = [
            ctx for ctx in lines_with_static
            if "_set_static_ipv4" not in ctx
        ]
        self.assertEqual(
            unexpected, [],
            "found inline ``set address ... static`` calls outside the"
            f" helper -- they bypass the stale-binding fix: {unexpected}",
        )


class TestRunNetshCapturesElevatedOutput(unittest.TestCase):
    """When ATLAS isn't already elevated, ``_run_netsh`` uses
    ShellExecuteEx to launch UAC-elevated netsh. ShellExecuteEx-
    launched processes don't inherit our stdout/stderr pipes, so the
    previous implementation returned a bare ``netsh exit code 1`` with
    no clue what netsh actually rejected. The fix: launch via
    ``cmd.exe /c netsh <args> > <tmp> 2>&1`` and read the tempfile
    after the child exits, so the operator sees the real error string
    (e.g. "The system cannot find the file specified" for a bad NIC
    name)."""

    def test_elevated_path_uses_cmd_exe_for_output_redirect(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._run_netsh)
        # Pin both the temp-file allocation and the cmd.exe wrapping.
        # The exact filename suffix isn't load-bearing; the contract
        # is "tempfile created" + "cmd.exe launched with redirection".
        self.assertIn("tempfile.NamedTemporaryFile", src)
        self.assertIn('lpFile="cmd.exe"', src)
        # The redirect itself: ``> "<path>" 2>&1`` is what gives us
        # both streams in one file.
        self.assertIn("2>&1", src)

    def test_elevated_path_reads_tempfile_after_exit(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._run_netsh)
        # Must actually OPEN the tempfile, not just write to it.
        self.assertIn('open(tmp_log_path', src)

    def test_elevated_path_cleans_up_tempfile(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._run_netsh)
        # We must remove the temp file when done -- don't leak files
        # in %TEMP% across runs.
        self.assertIn("os.unlink(tmp_log_path)", src)

    def test_logs_exact_command_before_invocation(self):
        # If netsh refuses an invocation, the operator's first
        # question is "what command did ATLAS construct?". The log
        # must answer that without needing a debugger.
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame._run_netsh)
        self.assertIn("[NETSH]", src)
        self.assertIn("running:", src)


class TestSoftwareUpgradeLogTeesToAllSinks(unittest.TestCase):
    """``SoftwareUpgradeFrame._log`` previously wrote only to the
    frame's local Log widget. That meant: (a) the shared bottom output
    panel showed nothing for WS5 / RLS / G42 upgrades, even though
    every other ATLAS mode logs there; (b) GUI-side messages never
    reached the ATLAS log file, so the operator couldn't attach the
    file to a bug report and have the full transcript -- only the
    upgrade SCRIPT's logger.info lines made it through.

    Source-level pins are enough here -- we don't spin up Tk in unit
    tests. The contract: _log must use the controller's unified
    ``log_activity`` bridge, with ``logging.info`` as its fallback.
    """

    def test_log_method_writes_to_controller_output_screen(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame.SoftwareUpgradeFrame._log)
        self.assertIn("log_activity", src)

    def test_log_method_writes_to_python_logger(self):
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame.SoftwareUpgradeFrame._log)
        # Must reach the rolling ATLAS log file. logging.info is the
        # canonical call site (the root logger handler picks it up).
        self.assertIn("logging.info", src)

    def test_log_method_still_writes_to_per_frame_widget(self):
        # Don't accidentally REMOVE the per-frame widget -- the
        # bottom shared panel may not always be visible (Diagnostics
        # mode hides it, for instance), so we want belt-and-braces.
        from gui import software_upgrade_frame
        src = inspect.getsource(
            software_upgrade_frame.SoftwareUpgradeFrame._log_local
        )
        self.assertIn("_log_text", src)


class TestSerialBaudIs115200(unittest.TestCase):
    """The Waveserver 5 console runs at 115200 8N1 (field-confirmed on
    production hardware). An earlier comment in the module claimed
    9600 per vendor docs, but the device times out at that baud and
    only yields a prompt at 115200. Pin the value so a future
    refactor / docs sync can't quietly downgrade it back to 9600.
    """

    def test_serial_baud_constant_is_115200(self):
        from scripts.Network import Ciena_Waveserver5_Upgrade as ws5
        self.assertEqual(
            ws5._SERIAL_BAUD, 115200,
            "Waveserver 5 console baud must be 115200; 9600 will hang"
            " every upgrade run on field hardware.",
        )

    def test_user_facing_hints_advertise_115200(self):
        # The pre-flight dialog + the labeled-frame description both
        # tell the operator which baud to cable for. If we ever
        # change _SERIAL_BAUD again, those strings need to match or
        # the operator gets contradictory guidance.
        from gui import software_upgrade_frame
        src = inspect.getsource(software_upgrade_frame)
        self.assertIn("115200", src)
        # Make sure the OLD 9600 references in WS5 contexts are gone.
        # Other devices (SAOS 39xx etc.) legitimately use 9600 in the
        # provisioning frame, so we only scan the WS5 surface here.
        ws5_block_start = src.find("Waveserver 5")
        self.assertGreater(ws5_block_start, -1)
        ws5_section = src[ws5_block_start:ws5_block_start + 4000]
        self.assertNotIn(
            "9600", ws5_section,
            "found a stale '9600' reference inside the WS5 UI block --"
            " operator will be told to cable at the wrong baud",
        )


class TestPromptRegexAcceptsWaveserverAsterisk(unittest.TestCase):
    """Waveserver-5 prepends ``*`` to the prompt while config changes are
    pending (``WS5_1*#``). Before this fix the shared regex stalled on
    those prompts because it required ``#`` immediately after the
    hostname — the asterisk broke the match."""

    def test_pending_changes_prompt_matches_shell_re(self):
        self.assertIsNotNone(_SHELL_RE.search(b"Waveserver-5*#"))
        self.assertIsNotNone(_SHELL_RE.search(b"WS5_1*#"))

    def test_saved_prompt_still_matches_shell_re(self):
        # No asterisk = config committed. Must still match.
        self.assertIsNotNone(_SHELL_RE.search(b"WS5_1#"))
        self.assertIsNotNone(_SHELL_RE.search(b"Waveserver-5#"))

    def test_pending_changes_prompt_matches_prompt_re(self):
        self.assertIsNotNone(_PROMPT_RE.search(b"WS5_1*#\n"[:-1]))
        self.assertIsNotNone(_PROMPT_RE.search(b"Waveserver-5*#"))

    def test_sros_a_prompt_still_matches(self):
        # Backwards-compat: the existing Nokia SROS prompt forms must
        # keep matching after the asterisk relaxation.
        self.assertIsNotNone(_SHELL_RE.search(b"A:lrt2#"))
        self.assertIsNotNone(_SHELL_RE.search(b"*A:lrt2#"))
        self.assertIsNotNone(_SHELL_RE.search(b"B:lrt2>"))


class TestWaveserver5VersionDerivation(unittest.TestCase):
    """``software activate version <X>`` takes the tarball name minus
    the ``.tar.gz`` — which is also what the device echoes back as
    Upgrade-To-Version in the status table."""

    def setUp(self):
        from scripts.Network.Ciena_Waveserver5_Upgrade import (
            Waveserver5UpgradeScript,
        )
        self.derive = Waveserver5UpgradeScript._derive_version

    def test_tar_gz_extension_stripped(self):
        self.assertEqual(
            self.derive("waveserver-2.4.52.21-GA.tar.gz"),
            "waveserver-2.4.52.21-GA",
        )

    def test_tgz_extension_also_supported(self):
        self.assertEqual(
            self.derive("waveserver-2.4.52.21-GA.tgz"),
            "waveserver-2.4.52.21-GA",
        )

    def test_unknown_extension_returns_filename_as_is(self):
        # Avoids accidentally chopping something useful off if the
        # operator picked a non-tarball by mistake — the device will
        # then reject the activate command and surface the error.
        self.assertEqual(self.derive("weird-name"), "weird-name")

    def test_case_insensitive_extension_match(self):
        self.assertEqual(
            self.derive("Waveserver-2.4.52.21-GA.TAR.GZ"),
            "Waveserver-2.4.52.21-GA",
        )


class TestWaveserver5StatusTableParser(unittest.TestCase):
    """The device prints ``software show upgrade-status`` as a boxed
    table. We only need the ``Upgrade State`` row's value cell — the
    parser splits on the pipe and takes the rightmost non-empty token."""

    def setUp(self):
        from scripts.Network.Ciena_Waveserver5_Upgrade import (
            Waveserver5UpgradeScript,
        )
        self.parse = Waveserver5UpgradeScript._parse_upgrade_state

    _SAMPLE_DOWNLOADING = (
        "WS5_1# software show upgrade-status\n"
        "+----------------------------------- UPGRADE STATUS INFORMATION ----+\n"
        "|         Parameter             |                Value              |\n"
        "+-------------------------------+-----------------------------------+\n"
        "| Committed Release Version     | waveserver-2.3.11.180-GA          |\n"
        "| Active Release Version        | waveserver-2.3.11.180-GA          |\n"
        "| Upgrade To Version            |                                   |\n"
        "| Upgrade State                 | Download In Progress              |\n"
        "| Last Upgrade Operation        | Downloading release file          |\n"
        "+-------------------------------+-----------------------------------+\n"
        "WS5_1#"
    )
    _SAMPLE_DOWNLOAD_DONE = _SAMPLE_DOWNLOADING.replace(
        "Download In Progress", "Download Complete"
    )
    _SAMPLE_ACTIVATING = _SAMPLE_DOWNLOADING.replace(
        "Download In Progress", "Activation In Progress"
    )

    def test_download_in_progress(self):
        self.assertEqual(
            self.parse(self._SAMPLE_DOWNLOADING), "Download In Progress"
        )

    def test_download_complete(self):
        self.assertEqual(
            self.parse(self._SAMPLE_DOWNLOAD_DONE), "Download Complete"
        )

    def test_activation_in_progress(self):
        self.assertEqual(
            self.parse(self._SAMPLE_ACTIVATING), "Activation In Progress"
        )

    def test_returns_empty_when_table_missing(self):
        self.assertEqual(self.parse("garbage output"), "")


class TestWaveserver5ScriptShape(unittest.TestCase):
    """Source-level guard against silent constructor / method-signature
    changes that would break the GUI worker. The frame wires up a
    specific set of kwargs and a single ``run()`` entry point — pin
    them down here."""

    def setUp(self):
        from scripts.Network.Ciena_Waveserver5_Upgrade import (
            Waveserver5UpgradeScript,
        )
        self.cls = Waveserver5UpgradeScript

    def test_constructor_accepts_documented_kwargs(self):
        sig = inspect.signature(self.cls.__init__)
        params = sig.parameters
        for required in (
            "serial_port", "software_filename", "server_url",
            "device_ip", "device_ip_cidr", "gateway_ip",
            "hostname", "ssh_user", "ssh_pass",
            "output_callback", "stop_callback",
        ):
            self.assertIn(
                required, params,
                f"Waveserver5UpgradeScript.__init__ must accept {required!r} "
                f"(GUI worker passes it in)",
            )

    def test_run_method_orchestrates_both_phases(self):
        src = inspect.getsource(self.cls.run)
        # The serial → SSH ordering is load-bearing: if SSH ran first
        # the device wouldn't have a usable IP yet. Confirm via call
        # ordering in the source.
        serial_pos = src.find("_provision_via_serial")
        ssh_pos = src.find("_install_software_via_ssh")
        self.assertGreater(serial_pos, 0)
        self.assertGreater(ssh_pos, serial_pos)

    def test_serial_phase_sends_required_commands(self):
        src = inspect.getsource(self.cls._provision_via_serial)
        for needle in (
            "system set host-name",
            "dhcp client disable",
            "interface set interface local ip",
            "interface set gateway",
            "ntp client disable",
            "system set date",
            "system set time",
            "configuration save",
        ):
            self.assertIn(
                needle, src,
                f"Phase 1 serial command {needle!r} is missing from "
                f"_provision_via_serial",
            )

    def test_ssh_phase_sends_required_commands(self):
        # Check the whole class — ``software show upgrade-status`` is
        # only literal in ``_wait_for_state`` (the polling helper),
        # while the rest live in ``_install_software_via_ssh``.
        src = inspect.getsource(self.cls)
        for needle in (
            "software download url",
            "software show upgrade-status",
            "system server grpc disable",
            "system server https disable",
            "system server netconf disable",
            "system server sftp disable",
            "system server scp disable",
            "user create user diag password diagdiag access-level diag",
            "software activate version",
        ):
            self.assertIn(
                needle, src,
                f"Phase 2 SSH command {needle!r} is missing from the "
                f"Waveserver5UpgradeScript class",
            )


class TestSoftwareUpgradeFrameRegistersWaveserver5(unittest.TestCase):
    """The dropdown must list ``Ciena Waveserver 5`` or the user can't
    pick it. Source-level check avoids needing a Tk root."""

    def test_supported_upgrades_includes_waveserver5(self):
        from gui import software_upgrade_frame as suf
        self.assertIn("Ciena Waveserver 5", suf._SUPPORTED_UPGRADES)

    def test_ws5_network_constants_present(self):
        from gui import software_upgrade_frame as suf
        self.assertEqual(suf._WS5_NET["pc_ip"], "10.9.49.101")
        self.assertEqual(suf._WS5_NET["device_ip"], "10.9.49.36")
        self.assertEqual(suf._WS5_NET["device_ip_cidr"], "10.9.49.36/22")
        # /22 = 255.255.252.0; getting this wrong would silently put the
        # PC and the shelf on different subnets.
        self.assertEqual(suf._WS5_NET["mask"], "255.255.252.0")
        self.assertEqual(suf._WS5_HOSTNAME, "WS5_1")


if __name__ == "__main__":
    unittest.main()
