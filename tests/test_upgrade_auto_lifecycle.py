"""Regression test for the Software Upgrade tab auto-lifecycle.

Field bug this guards: the upgrade tab used to expose four manual
buttons -- Apply Static IP, Restore DHCP, Start Server, Stop Server --
that the operator had to drive in sequence around the Run Upgrade
button. In practice every Run Upgrade press triggered the same
sequence regardless, and operators routinely forgot to click
Restore DHCP at the end, leaving their NIC stuck on the device-side
static IP after the laptop moved to the next bay.

The fix removes the buttons entirely and makes ``_run_<dtype>_upgrade``
do the full lifecycle inside its worker thread:

  1. Apply the static IP for the device family.
  2. Start the HTTP server (all device families — PSI/PSS pull over
     HTTP too, rooted at CC's parent).
  3. Run the upgrade script.
  4. In a ``finally``, stop the server (if we started it) and
     restore DHCP (if we applied the static IP).

The static-IP for each device family is auto-populated into the
form when its name is picked from the device-type dropdown, so the
operator never types it.
"""
from __future__ import annotations
import inspect
import re
import unittest
from pathlib import Path


class TestManualNicAndServerButtonsRemoved(unittest.TestCase):
    """Pin that the four manual buttons are gone from the source.

    Pin via button *label*, not attribute name -- ``self._stop_btn``
    is in active use as the G42 Stop *Upgrade* button (distinct from
    the removed Stop *Server* button), so an attribute-name check
    would either miss the regression or false-fire.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.src = inspect.getsource(
            software_upgrade_frame.SoftwareUpgradeFrame
        )

    def test_apply_static_ip_button_label_removed(self):
        # The button used to read "Apply Static IP" -- match the
        # verbatim label so a refactor that renames the attribute
        # but keeps the user-facing button still trips this.
        self.assertNotIn(
            '"Apply Static IP"', self.src,
            "the manual 'Apply Static IP' button must not be present "
            "-- the worker applies the IP automatically now",
        )

    def test_restore_dhcp_button_label_removed(self):
        self.assertNotIn(
            '"Restore DHCP"', self.src,
            "the manual 'Restore DHCP' button must not be present "
            "-- the worker restores DHCP automatically now",
        )

    def test_start_server_button_label_removed(self):
        self.assertNotIn(
            '"Start Server"', self.src,
            "the manual 'Start Server' button must not be present "
            "-- the worker starts the server automatically now",
        )

    def test_stop_server_button_label_removed(self):
        self.assertNotIn(
            '"Stop Server"', self.src,
            "the manual 'Stop Server' button must not be present "
            "-- the worker stops the server automatically now",
        )


class TestDeviceTypeAutoPopulatesPcIp(unittest.TestCase):
    """``_on_dtype_change`` is the dropdown handler. It must seed the
    PC-IP field with the per-device-family service IP so the operator
    doesn't have to memorise four different /24s. Without this, the
    netsh apply step in the worker would fall back to whatever stale
    value was in the field from a previous run.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.mod = software_upgrade_frame
        self.handler_src = inspect.getsource(
            software_upgrade_frame.SoftwareUpgradeFrame._on_dtype_change
        )

    def test_handler_branches_on_each_device_type(self):
        # The four device-type strings the dropdown emits.
        self.assertIn('"Ciena RLS"', self.handler_src)
        self.assertIn('"Ciena Waveserver 5"', self.handler_src)
        self.assertIn('"Nokia G42"', self.handler_src)
        self.assertIn('"Nokia PSI"', self.handler_src)
        self.assertIn('"Nokia PSS"', self.handler_src)

    def test_g42_branch_writes_169_254_pc_ip(self):
        # G42 PC IP is fixed at 169.254.0.101 -- pin the value as it
        # appears in the per-device network dict.
        self.assertEqual(self.mod._G42_NET["pc_ip"], "169.254.0.101")
        self.assertIn("_G42_NET", self.handler_src)
        self.assertIn(
            "self._pc_ip_var.set(_G42_NET[\"pc_ip\"])", self.handler_src,
            "G42 branch must seed pc_ip_var from _G42_NET so it tracks "
            "the constant rather than a hardcoded duplicate",
        )

    def test_ws5_branch_writes_per_family_pc_ip_and_mask(self):
        # WS5 uses a /22 -- distinct from the others, so the mask
        # must come from _WS5_NET, not _DEFAULT_MASK.
        self.assertIn("_WS5_NET", self.handler_src)
        self.assertIn("self._mask_var.set(_WS5_NET[\"mask\"])", self.handler_src)

    def test_psi_branch_writes_per_family_pc_ip(self):
        self.assertIn("_PSI_NET", self.handler_src)
        self.assertIn(
            "self._pc_ip_var.set(_PSI_NET[\"pc_ip\"])", self.handler_src,
        )

    def test_rls_branch_defers_to_ctm_change(self):
        # RLS has two CTM variants with different /24s, so the handler
        # delegates to the CTM-specific change handler instead of
        # writing a single PC IP.
        self.assertIn("self._on_rls_ctm_change()", self.handler_src)


class TestUpgradeWorkersAutoRestoreDhcp(unittest.TestCase):
    """All four upgrade workers must call ``_restore_dhcp`` in their
    finally block when they applied a static IP. The flag-guard is
    important -- if the netsh apply step itself fails, the NIC was
    never touched and ``_restore_dhcp`` would either no-op or, worse,
    clobber a manually-set address.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.cls = software_upgrade_frame.SoftwareUpgradeFrame
        self.workers = {
            "g42": inspect.getsource(self.cls._run_g42_upgrade),
            "rls": inspect.getsource(self.cls._run_rls_upgrade),
            "psi": inspect.getsource(self.cls._run_psi_upgrade),
            "pss": inspect.getsource(self.cls._run_pss_upgrade),
            "ws5": inspect.getsource(self.cls._run_ws5_upgrade),
        }

    def test_each_worker_tracks_static_ip_ownership_flag(self):
        # The flag pattern -- ``static_ip_owned_by_us = False`` at the
        # top of the try, flipped to True after a successful netsh
        # apply -- is what guards the finally cleanup.
        for name, src in self.workers.items():
            with self.subTest(worker=name):
                self.assertIn(
                    "static_ip_owned_by_us = False", src,
                    f"{name} worker missing the static-IP ownership flag",
                )
                self.assertIn(
                    "static_ip_owned_by_us = True", src,
                    f"{name} worker missing the flag-set after netsh "
                    "apply",
                )

    def test_each_worker_calls_restore_dhcp_in_finally(self):
        for name, src in self.workers.items():
            with self.subTest(worker=name):
                # Scheduled on the Tk thread via ``self.after`` — the bare
                # ``self.after(0, self._restore_dhcp)`` or, for PSI, wrapped in
                # a lambda so the activation popup fires after netsh-OK
                # (``self.after(0, lambda ...: self._restore_dhcp(on_success=...))``).
                self.assertTrue(
                    re.search(r"self\.after\(0,.*self\._restore_dhcp", src),
                    f"{name} worker must schedule _restore_dhcp on the "
                    "Tk thread in its finally block",
                )
                # Pin the guard, not just the call -- without the
                # flag check we'd hit _restore_dhcp on every error
                # path, including ones where netsh never ran.
                self.assertIn(
                    "if static_ip_owned_by_us:", src,
                    f"{name} worker must guard the _restore_dhcp call "
                    "behind the ownership flag",
                )


class TestPsiActivationPopup(unittest.TestCase):
    """The PSI upgrade is only 'complete' once activate has dropped the
    session AND the NIC is back on DHCP — at which point a popup tells the
    operator activation is in progress and a manual commit is required.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.cls = software_upgrade_frame.SoftwareUpgradeFrame
        self.worker_src = inspect.getsource(self.cls._run_psi_upgrade)
        self.popup_src = inspect.getsource(self.cls._show_activation_popup)

    def test_popup_chained_to_dhcp_restore_on_success_only(self):
        # Popup is passed to _restore_dhcp as on_success, and only built when
        # the run succeeded (guarded by ok_run).
        self.assertIn("ok_run = False", self.worker_src)
        self.assertIn("_show_activation_popup", self.worker_src)
        self.assertIn("on_success=", self.worker_src)
        self.assertIn("if ok_run else None", self.worker_src)

    def test_popup_text_has_required_wording(self):
        for needle in (
            "ACTIVATION IN PROGRESS",
            "MANUAL COMMIT",
            "Safe to disconnect",
            "messagebox.showinfo",
        ):
            self.assertIn(needle, self.popup_src,
                          f"activation popup missing {needle!r}")

    def test_restore_dhcp_fires_on_success_after_netsh_ok(self):
        # The on_success hook must be invoked only on the netsh-OK branch.
        src = inspect.getsource(self.cls._restore_dhcp)
        self.assertIn("on_success", src)
        self.assertIn("self.after(0, on_success)", src)


class TestServerUsingWorkersAutoStopServer(unittest.TestCase):
    """G42, RLS, WS5, PSI, and PSS all have the device pull the upgrade
    artefact over HTTP from the laptop (PSI/PSS use ``config software
    server protocol HTTP`` — they are NOT FTP). Each worker must start
    the server if it isn't already running and stop it in the finally
    if *it* started it.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.cls = software_upgrade_frame.SoftwareUpgradeFrame
        self.http_workers = {
            "g42": inspect.getsource(self.cls._run_g42_upgrade),
            "rls": inspect.getsource(self.cls._run_rls_upgrade),
            "ws5": inspect.getsource(self.cls._run_ws5_upgrade),
            "psi": inspect.getsource(self.cls._run_psi_upgrade),
            "pss": inspect.getsource(self.cls._run_pss_upgrade),
        }

    def test_http_workers_track_server_ownership_flag(self):
        for name, src in self.http_workers.items():
            with self.subTest(worker=name):
                self.assertIn(
                    "server_started_by_us = False", src,
                    f"{name} worker missing server-ownership flag init",
                )
                self.assertIn(
                    "server_started_by_us = True", src,
                    f"{name} worker missing the flag-set after server "
                    "start",
                )

    def test_http_workers_auto_start_server_if_not_running(self):
        for name, src in self.http_workers.items():
            with self.subTest(worker=name):
                # The auto-start branch -- ``if self._server is None``
                # followed by an ``_start_server`` call -- replaces the
                # old "HTTP server not running, start now?" askyesno gate.
                # G42/RLS/WS5 serve the browsed folder directly
                # (``self.after(0, self._start_server)``); PSI/PSS serve
                # CC's parent (``self._start_server(str(serve_root))``).
                self.assertIn("if self._server is None:", src)
                self.assertTrue(
                    "self.after(0, self._start_server)" in src
                    or "self._start_server(str(serve_root))" in src,
                    f"{name} worker must auto-start the HTTP server "
                    "rather than prompting the operator",
                )

    def test_http_workers_call_stop_server_in_finally(self):
        for name, src in self.http_workers.items():
            with self.subTest(worker=name):
                self.assertIn(
                    "self.after(0, self._stop_server)", src,
                    f"{name} worker must schedule _stop_server in its "
                    "finally block",
                )
                self.assertIn(
                    "if server_started_by_us:", src,
                    f"{name} worker must guard the _stop_server call "
                    "behind the ownership flag",
                )


class TestRunUpgradePromptsDoNotGateOnHttpServer(unittest.TestCase):
    """The pre-flight ``askokcancel`` / ``askyesno`` dialogs that
    used to ask "HTTP server not running, start now?" are now an
    operator-facing footgun -- the worker handles it. Pin that the
    gate is gone from each Run Upgrade entry point.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.cls = software_upgrade_frame.SoftwareUpgradeFrame
        self.workers = {
            "g42": inspect.getsource(self.cls._run_g42_upgrade),
            "rls": inspect.getsource(self.cls._run_rls_upgrade),
            "ws5": inspect.getsource(self.cls._run_ws5_upgrade),
            "psi": inspect.getsource(self.cls._run_psi_upgrade),
            "pss": inspect.getsource(self.cls._run_pss_upgrade),
        }

    def test_run_upgrade_does_not_block_on_server_not_running_prompt(self):
        for name, src in self.workers.items():
            with self.subTest(worker=name):
                # The old gate -- ``askyesno("HTTP server not
                # running", ...)`` -- would silently abort the run
                # if the operator missed the dialog.
                self.assertNotIn(
                    '"HTTP server not running"', src,
                    f"{name} Run Upgrade must not gate on a 'server "
                    "not running' dialog -- start it automatically",
                )


class TestCcLayoutResolution(unittest.TestCase):
    """The PSI/PSS release dropdown was empty when the operator picked the
    CC/ folder (or a release folder) instead of its parent. _resolve_cc_layout
    must locate CC/ from any of those three levels and always return CC's
    PARENT as the serve root so /CC/<release> resolves over HTTP."""

    def setUp(self):
        import tempfile
        from gui import software_upgrade_frame
        self.resolve = software_upgrade_frame.SoftwareUpgradeFrame._resolve_cc_layout
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "Software Loads"
        self.release = self.base / "CC" / "1830OLS-25.3-3"
        self.release.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_from_parent_of_cc(self):
        root, cc = self.resolve(str(self.base))
        self.assertEqual(root, self.base)
        self.assertEqual(cc, self.base / "CC")

    def test_resolves_from_cc_folder_itself(self):
        root, cc = self.resolve(str(self.base / "CC"))
        self.assertEqual(root, self.base)          # serve root is CC's parent
        self.assertEqual(cc, self.base / "CC")

    def test_resolves_from_release_folder(self):
        root, cc = self.resolve(str(self.release))
        self.assertEqual(root, self.base)
        self.assertEqual(cc, self.base / "CC")

    def test_no_cc_returns_none(self):
        root, cc = self.resolve(str(Path(self._tmp.name)))
        self.assertIsNone(root)
        self.assertIsNone(cc)

    def test_empty_selection_returns_none(self):
        self.assertEqual(self.resolve(""), (None, None))


class TestCcReleaseListingIsUnzippedOnly(unittest.TestCase):
    """PSI/PSS shelves pull individual files from an UNZIPPED release folder,
    so the dropdown must offer only real extracted directories — never a .zip
    archive and never a partial-download artifact (.zip_temp, .crswap, .part)."""

    def setUp(self):
        import tempfile
        from gui import software_upgrade_frame
        self.cls = software_upgrade_frame.SoftwareUpgradeFrame
        self._tmp = tempfile.TemporaryDirectory()
        self.cc = Path(self._tmp.name) / "CC"
        self.cc.mkdir()
        # Two good release folders + a bag of junk that must be filtered.
        (self.cc / "1830OLS-25.3-3").mkdir()
        (self.cc / "3KC72992AAAAPMZZA").mkdir()
        (self.cc / "3KC73353AAAAPMZZA.zip_temp").mkdir()   # partial download dir
        (self.cc / ".hidden").mkdir()
        (self.cc / "1830OLS-25.3-3.zip").write_bytes(b"PK\x03\x04")   # archive
        (self.cc / "3KC73353AAAAPMZZA.zip.1.crswap").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_release_dirs_lists_only_unzipped_folders(self):
        got = self.cls._release_dirs(self.cc)
        self.assertEqual(got, ["1830OLS-25.3-3", "3KC72992AAAAPMZZA"])

    def test_is_release_dir_rejects_artifacts(self):
        self.assertTrue(self.cls._is_release_dir("1830OLS-25.3-3"))
        for junk in ("foo.zip_temp", ".hidden", "x.crswap", "bar.part", "~tmp"):
            self.assertFalse(self.cls._is_release_dir(junk), junk)

    def test_cc_zip_files_detects_unextracted_archives(self):
        # Drives the "unzip the load first" operator hint.
        self.assertIn("1830OLS-25.3-3.zip", self.cls._cc_zip_files(self.cc))


if __name__ == "__main__":
    unittest.main()
