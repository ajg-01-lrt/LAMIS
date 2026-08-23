"""End-of-run feedback: a popup must surface failed devices.

Field report: an operator's audit silently dropped one of 12 chassis
because both ``admin@`` and ``su@`` SSH auth failed for that node. The
failure landed in ``self.failed_ips`` and was written to the on-screen
output panel as a ``--- FAILED IPs ---`` block, but no modal alert
fired -- so the operator didn't notice and shipped a packing slip
built from only 11 captured devices.

Fix: :py:meth:`finish_run` now pops a ``messagebox.showwarning`` when
``self.failed_ips`` has entries, *before* the "System Ready" success
dialog. The popup must:

  * Fire on every completion path -- success, error, abort.
  * Name each failed IP with its reason so the operator knows what to
    re-run / re-credential.
  * Cap the displayed list so a 200-device LAN scan with widespread
    failures doesn't produce an off-screen dialog.

Source-level pins are appropriate here -- we can't reasonably stand
up the full Tk app inside a unit test, but we can guarantee the
finish-run code path won't silently regress.
"""
from __future__ import annotations
import inspect
import unittest


class TestFinishRunSurfacesFailedDevices(unittest.TestCase):
    def setUp(self):
        from gui.gui4_0 import InventoryGUI
        self.finish_src = inspect.getsource(InventoryGUI.finish_run)

    def test_finish_run_inspects_failed_ips(self):
        # The fix must consult self.failed_ips on the completion path,
        # not only when the export worker reports success.
        self.assertIn("self.failed_ips", self.finish_src)

    def test_finish_run_shows_warning_popup_for_failures(self):
        # The popup is the user-visible artifact. Pin the method that
        # produces it so a refactor that replaces showwarning with a
        # silent log line gets caught.
        self.assertIn(
            "_show_failed_devices_popup", self.finish_src,
            "finish_run must delegate to _show_failed_devices_popup "
            "when self.failed_ips has entries -- without the popup, a "
            "partial run silently ships an incomplete inventory.",
        )

    def test_failure_popup_runs_BEFORE_success_dialog(self):
        # The "System Ready" success popup is shown only when the run
        # actually succeeded. The failure popup must come FIRST so the
        # operator sees the failed list before clicking through to
        # "ready". Otherwise the success dialog visually overrides
        # the warning and they dismiss both at once without reading.
        #
        # Match on the actual ``messagebox.showinfo(...)`` call site,
        # not the bare string -- the explanatory comment above the
        # popup invocation contains the literal ``"System Ready"``
        # text and would false-match a substring search.
        fail_idx = self.finish_src.find(
            "self._show_failed_devices_popup("
        )
        ready_idx = self.finish_src.find(
            'messagebox.showinfo("System Ready"'
        )
        self.assertGreater(fail_idx, -1, "popup call missing")
        self.assertGreater(
            ready_idx, -1,
            "'System Ready' showinfo call missing",
        )
        self.assertLess(
            fail_idx, ready_idx,
            "the failed-devices warning must be shown BEFORE the "
            "'System Ready' info dialog -- otherwise the success "
            "popup steals focus and the operator dismisses both at "
            "once without reading the failure list.",
        )


class TestFailedDevicesPopupShape(unittest.TestCase):
    def setUp(self):
        from gui.gui4_0 import InventoryGUI
        self.popup_src = inspect.getsource(
            InventoryGUI._show_failed_devices_popup
        )

    def test_popup_uses_messagebox_showwarning(self):
        # ``showwarning`` is the right level: not an error (run still
        # produced data for the rest) but more visible than
        # ``showinfo``. Pin to catch a refactor that downgrades to
        # ``showinfo`` or removes the popup entirely.
        self.assertIn("messagebox.showwarning", self.popup_src)
        # Title text matters -- it's what the operator sees in the
        # taskbar / Alt-Tab list and is the first signal something
        # went wrong.
        self.assertIn('"Some Devices Failed"', self.popup_src)

    def test_popup_lists_each_failed_ip_with_reason(self):
        # The body must include both the IP and the failure reason so
        # the operator knows what to fix (credentials vs unreachable
        # vs unknown device type).
        self.assertIn("failed.items()", self.popup_src)
        self.assertIn("reason", self.popup_src)

    def test_popup_caps_displayed_list(self):
        # A LAN sweep with hundreds of failures must NOT generate a
        # popup taller than the screen. Pin the truncation logic.
        self.assertIn("MAX_SHOWN", self.popup_src)
        # And it must point to the full list location when truncated
        # so the operator knows where to look.
        self.assertIn("Output panel", self.popup_src)

    def test_popup_protects_against_tk_dialog_failure(self):
        # The popup is a UX nicety; a Tk crash inside it must not
        # prevent finish_run from clearing run controls and resetting
        # state. Pin the exception handler.
        self.assertIn("except Exception", self.popup_src)


class TestFailedIpsResetAtRunStart(unittest.TestCase):
    """``self.failed_ips`` must be cleared at the START of every run --
    BOTH the single-host Direct Connection path (LAN / Serial) and the
    multi-host LAN-scan path.

    Field bug this guards: a Serial Direct Connection run failed
    on COM11, leaving ``failed_ips["COM11"]`` in the dict. The next
    run -- LAN Direct Connection on 10.0.0.1, completed successfully
    via manual credentials -- still triggered the end-of-run failure
    popup, but the popup listed COM11 from the previous run (and
    that run's serial-probe error message), not anything from the
    current LAN attempt.

    Root cause: the reset used to live inside the multi-host branch
    of ``run_inventory_worker``, after the single-host path had
    already ``return``ed. Moving it ABOVE the branch fixes both.
    """

    def setUp(self):
        from gui.gui4_0 import InventoryGUI
        self.src = inspect.getsource(InventoryGUI.run_inventory_worker)

    def test_reset_happens_before_connection_mode_branch(self):
        # The contract: by the time we check ``connection_mode`` to
        # split into Direct vs multi-host, ``failed_ips`` is already
        # empty. Pin this at source level.
        reset_idx = self.src.find("self.failed_ips = {}")
        branch_idx = self.src.find(
            'context.get("connection_mode") in ("LAN", "Serial")'
        )
        self.assertGreater(reset_idx, -1, "failed_ips reset missing")
        self.assertGreater(branch_idx, -1, "connection_mode branch missing")
        self.assertLess(
            reset_idx, branch_idx,
            "failed_ips must be reset BEFORE the Direct-Connection /"
            " multi-host branch -- otherwise a Direct Connection run"
            " inherits the previous run's failed_ips dict and the"
            " end-of-run popup lists stale failures from earlier runs.",
        )

    def test_pause_queue_resets_at_same_point(self):
        # ``pause_queue`` has the same stale-state problem as
        # ``failed_ips`` and must reset alongside it (a parked
        # credential-prompt entry from a prior run would otherwise
        # re-trigger ``_drain_pause_queue`` on the next run).
        reset_idx = self.src.find("self.pause_queue = []")
        branch_idx = self.src.find(
            'context.get("connection_mode") in ("LAN", "Serial")'
        )
        self.assertGreater(reset_idx, -1, "pause_queue reset missing")
        self.assertLess(reset_idx, branch_idx)

    def test_reset_appears_exactly_once_in_run_worker(self):
        # The old code reset failed_ips inside the multi-host branch.
        # After the fix there should be exactly one reset, near the
        # top -- not duplicated. A duplicate reset is harmless but
        # would be a refactor smell.
        self.assertEqual(
            self.src.count("self.failed_ips = {}"), 1,
            "expected exactly one 'self.failed_ips = {}' line --"
            " the previous in-branch reset should have been removed",
        )


if __name__ == "__main__":
    unittest.main()
