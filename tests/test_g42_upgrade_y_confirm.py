"""Regression test for Nokia G42 upgrade y/n confirmation handling.

Field bug this guards: the G42 upgrade script ran cleanly through
``prepare-upgrade validate <manifest>`` only when the manifest stayed
within the same release family. Cross-family runs (e.g. installed
ADVANCED -> target BASIC) trigger an interactive y/n prompt from the
device:

    admin@GX> prepare-upgrade validate G40_BASIC-R8.0.2-...manifest
    Upgrading to BASIC release. This may result in loss of config.
    It is advised to unprovision before upgrading.
    Do you still want to continue? [y/n]

The original ``_phase_validate`` / ``_phase_apply`` implementations
used ``_send_raw`` which has no y/n awareness, so the script hung at
the prompt and the upgrade was abandoned. The prep phase already
used ``_send_confirm`` and worked; the validate/apply paths needed
the same treatment.

The fix routes both phases through ``_send_confirm`` (now with a
caller-supplied timeout so the long upgrade windows still apply).
"""
from __future__ import annotations
import inspect
import unittest


class TestG42UpgradeUsesConfirmHelperOnPromptingPhases(unittest.TestCase):

    def setUp(self):
        from scripts.Network import Nokia_G42_Upgrade
        self.mod = Nokia_G42_Upgrade
        self.validate_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._phase_validate
        )
        self.apply_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._phase_apply
        )
        self.confirm_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._send_confirm
        )

    def test_validate_phase_uses_send_confirm(self):
        # The body must call ``_send_confirm`` (not the bare
        # ``_send_raw``) so the cross-family ``[y/n]`` prompt that
        # the device emits before running the validation gets
        # auto-answered.
        self.assertIn("self._send_confirm(", self.validate_src)
        self.assertNotIn(
            "self._send_raw(session, cmd,", self.validate_src,
            "validate phase must no longer call _send_raw with the "
            "raw prepare-upgrade command -- _send_raw hangs at the "
            "y/n prompt the device emits for cross-family upgrades.",
        )

    def test_apply_phase_uses_send_confirm(self):
        self.assertIn("self._send_confirm(", self.apply_src)
        self.assertNotIn(
            "self._send_raw(session, cmd,", self.apply_src,
            "apply phase must no longer call _send_raw with the "
            "raw prepare-upgrade command.",
        )

    def test_validate_phase_uses_long_timeout(self):
        # The y/n round trip is fast, but the actual validate phase
        # is gated by _VALIDATE_TIMEOUT_S (20 min). Pin that the
        # confirm helper gets the right window.
        self.assertIn("_VALIDATE_TIMEOUT_S", self.validate_src)

    def test_apply_phase_uses_long_timeout(self):
        self.assertIn("_APPLY_TIMEOUT_S", self.apply_src)

    def test_send_confirm_accepts_timeout_kwarg(self):
        # The helper must accept a timeout parameter -- previously
        # it was hardcoded to _PROMPT_TIMEOUT_S (30s) which was way
        # too short for the validate / apply phases.
        sig = inspect.signature(
            self.mod.NokiaG42UpgradeScript._send_confirm
        )
        self.assertIn("timeout", sig.parameters)

    def test_send_confirm_resets_deadline_after_confirmation(self):
        # The reset after sending 'y' is what gives the long-running
        # work its full window. Without this, a 20-min validate phase
        # would only get whatever was left of the initial timeout
        # after the y/n round-trip -- effectively unchanged from the
        # old _PROMPT_TIMEOUT_S limitation.
        self.assertIn("deadline = time.time() + timeout", self.confirm_src)


class TestG42UpgradeVerifiesPhaseCompletionViaUpgradeStatus(unittest.TestCase):
    """After the prepare-upgrade command returns to prompt, the device
    runs the validate / apply work asynchronously and reports per-
    card status via ``upgrade-status``. The output is one row per
    software-load target (aggregate + one per card)::

        software-load-installable      ...  apply-complete
        software-load-1-1/installable  ...  apply-complete
        software-load-1-3/installable  ...  apply-in-progress

    Returning success the moment ``apply-complete`` first appears is
    a real-world footgun: card 1/3 frequently lags 1/1, and the
    activate phase racing ahead while 1/3 is still applying triggers
    ``ERROR: precondition failed - Standby is not prepared to
    process activate.`` from the device. The poll helper must
    therefore wait until *every* row that was in-progress has
    reached complete, not just the first.

    The helper also short-circuits on the matching ``-failed`` token
    so a known-bad upgrade aborts immediately instead of waiting out
    the full timeout window.
    """

    def setUp(self):
        from scripts.Network import Nokia_G42_Upgrade
        self.mod = Nokia_G42_Upgrade
        self.validate_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._phase_validate
        )
        self.apply_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._phase_apply
        )
        self.poll_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._poll_upgrade_status
        )

    def test_poll_helper_exists(self):
        # The helper is what factors the poll loop out of each phase.
        # If a refactor removes it, both phases break.
        self.assertTrue(hasattr(
            self.mod.NokiaG42UpgradeScript, "_poll_upgrade_status",
        ))

    def test_poll_helper_sends_upgrade_status_command(self):
        self.assertIn('"upgrade-status"', self.poll_src)

    def test_poll_helper_accepts_per_phase_tokens(self):
        # The helper must take an in_progress / complete / failed
        # token triple so callers can specify their phase's tokens.
        # A single ``success_marker`` argument was the older shape
        # that produced false positives when only the aggregate row
        # had transitioned.
        sig = inspect.signature(
            self.mod.NokiaG42UpgradeScript._poll_upgrade_status
        )
        self.assertIn("in_progress_token", sig.parameters)
        self.assertIn("complete_token", sig.parameters)
        self.assertIn("failed_token", sig.parameters)

    def test_poll_helper_short_circuits_on_failed_token(self):
        # When any row reports failed, the helper must abort the
        # poll loop rather than running out the timeout.
        self.assertIn("failed_token in out", self.poll_src)

    def test_poll_helper_waits_for_zero_in_progress(self):
        # The success condition: no rows still in-progress AND at
        # least one row complete. ``out.count(in_progress_token)``
        # is the load-bearing call -- a substring check ("any row
        # still in-progress?") would race the same way the prior
        # success_marker approach did.
        self.assertIn(
            "out.count(in_progress_token)", self.poll_src,
            "helper must count in-progress rows, not just probe "
            "for the substring -- the success condition is zero",
        )
        self.assertIn("out.count(complete_token)", self.poll_src)

    def test_poll_helper_guards_against_stale_state_with_saw_flag(self):
        # First-poll guard: if we never observed any row in the
        # in-progress state, we can't trust a "complete" reading
        # without ruling out stale state from a prior phase.
        self.assertIn("saw_in_progress", self.poll_src)

    def test_validate_phase_passes_validate_tokens(self):
        self.assertIn('"validate-in-progress"', self.validate_src)
        self.assertIn('"validate-complete"', self.validate_src)
        self.assertIn('"validate-failed"', self.validate_src)
        self.assertIn("_poll_upgrade_status", self.validate_src)

    def test_apply_phase_passes_apply_tokens(self):
        self.assertIn('"apply-in-progress"', self.apply_src)
        self.assertIn('"apply-complete"', self.apply_src)
        self.assertIn('"apply-failed"', self.apply_src)
        self.assertIn("_poll_upgrade_status", self.apply_src)

    def test_validate_phase_checks_initiated_signature(self):
        # If the device didn't print "validate initiated", the prepare
        # command was rejected upstream of any actual validation work
        # -- polling would just time out. The phase must check the
        # immediate reply and bail early.
        self.assertIn("validate initiated", self.validate_src.lower())

    def test_apply_phase_checks_initiated_signature(self):
        self.assertIn("apply initiated", self.apply_src.lower())


class TestG42ActivateHandlesStandbyControllerWarning(unittest.TestCase):
    """``activate swimage`` emits one or two y/n prompts before the
    SSH session drops:

    1. ``Are you sure? [y/n]`` -- always.
    2. ``Standby controller card in NC is not ready synchronized,
       Single controller card will upgrade. Do you want to continue?
       [y/n]`` -- only when the redundant XMM4 isn't in sync.

    The script must auto-answer 'y' to both, AND remember whether
    the standby-sync warning fired so the GUI can show a different
    "repeat the upgrade on the second XMM4" dialog at the end."""

    def setUp(self):
        from scripts.Network import Nokia_G42_Upgrade
        self.mod = Nokia_G42_Upgrade
        self.activate_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript._phase_activate
        )

    def test_script_has_standby_sync_warning_attribute(self):
        # The flag must be a public instance attribute on the script
        # (set False by default) so the GUI worker can read it after
        # script.run() returns. Verify by checking __init__ assigns it.
        init_src = inspect.getsource(
            self.mod.NokiaG42UpgradeScript.__init__
        )
        self.assertIn("self.standby_sync_warning", init_src)

    def test_activate_answers_yn_prompts(self):
        # ``activate swimage`` issues at least one ``Are you sure?
        # [y/n]`` prompt before dropping the SSH session. The phase
        # must send 'y' to it -- previous code just waited for the
        # SSH drop and silently hung on the prompt.
        self.assertIn('session.send("y\\n")', self.activate_src)

    def test_activate_detects_standby_sync_warning_text(self):
        # The signature must be matched case-insensitively against
        # at least one of three landmarks (firmware revisions phrase
        # it slightly differently).
        self.assertIn("standby controller card", self.activate_src.lower())
        # Setting the flag is the whole point.
        self.assertIn(
            "self.standby_sync_warning = True", self.activate_src,
        )

    def test_activate_uses_confirm_regex_for_yn_detection(self):
        # The reused _CONFIRM_RE pattern already handles ``[y/n]``,
        # ``(y/n)``, and "press y to continue" variants. The
        # activate path must use the same regex rather than a
        # hand-rolled string match.
        self.assertIn("_CONFIRM_RE.search", self.activate_src)

    def test_activate_treats_activation_in_progress_line_as_success(self):
        # The device prints "Activation is in progress !" the moment
        # it commits to the reboot, before the SSH session actually
        # drops. Without an early-exit on that line, the worker waits
        # for the SSH drop -- which on some firmware revisions arrives
        # well after the activation point and delays the operator's
        # completion popup unnecessarily.
        self.assertIn(
            "activation is in progress", self.activate_src.lower(),
            "activate phase must detect the device's "
            "'Activation is in progress' announcement and treat it "
            "as the completion signal (case-insensitive match)",
        )


class TestG42SuccessShowsActivationInProgressPopup(unittest.TestCase):
    """End-of-upgrade UX: after the activate-swimage SSH drop, the
    operator gets one of two popups depending on whether the device
    reported the standby XMM4 was synced at activation time:

    * Normal: "Activation in Progress. Safe to Disconnect."
    * Standby not synced: "Control Cards not Synchronized. Preform
      Upgrade Again on Second XMM4."

    Pin both so a future refactor of the worker doesn't silently drop
    either dialog -- without them the operator may keep the cable
    plugged in waiting for "something to happen", or worse, walk
    away believing the upgrade is fully cut over when only the
    primary card got the new load.
    """

    def setUp(self):
        from gui import software_upgrade_frame
        self.src = inspect.getsource(
            software_upgrade_frame.SoftwareUpgradeFrame
        )

    def test_g42_worker_shows_activation_popup_on_success(self):
        self.assertIn(
            '"G42 — Activation in Progress"', self.src,
            "expected the G42 worker to show a messagebox with title "
            "'G42 — Activation in Progress' on the normal success path",
        )
        self.assertIn(
            '"Activation in Progress. Safe to Disconnect."', self.src,
            "expected the G42 popup body to match the verbatim "
            "operator-spec text 'Activation in Progress. Safe to "
            "Disconnect.'",
        )

    def test_g42_worker_shows_standby_not_synced_popup_when_flag_set(self):
        # The conditional popup body for the partial-sync case.
        self.assertIn(
            '"G42 — Standby Controller Not Synced"', self.src,
            "expected the G42 worker to show a different popup when "
            "the standby XMM4 wasn't synced at activate time",
        )
        self.assertIn(
            "Control Cards not Synchronized", self.src,
            "expected the G42 standby-not-synced popup body to start "
            "with the verbatim operator-spec text 'Control Cards not "
            "Synchronized'",
        )
        self.assertIn(
            "Second XMM4", self.src,
            "expected the standby-not-synced popup body to reference "
            "the second XMM4",
        )

    def test_g42_worker_branches_on_standby_sync_warning_flag(self):
        # The worker must consult ``script.standby_sync_warning`` to
        # pick the popup. Pin the attribute name -- a refactor that
        # renames it (e.g. to ``standby_warning``) would silently
        # always fall to the wrong default.
        self.assertIn("standby_sync_warning", self.src)


class TestPollUpgradeStatusWaitsForEveryCard(unittest.TestCase):
    """Behavioural test of the poll helper itself.

    The field bug was a real device transcript where the per-card
    rows reached ``apply-complete`` out of order::

        # Poll N:
        software-load-installable      ...  apply-complete
        software-load-1-1/installable  ...  apply-complete
        software-load-1-3/installable  ...  apply-in-progress

        # Poll N+1:
        software-load-installable      ...  apply-complete
        software-load-1-1/installable  ...  apply-complete
        software-load-1-3/installable  ...  apply-complete

    The helper must NOT return success on poll N (one card still
    in-progress); it must return success on poll N+1. Verify by
    driving the helper with a scripted sequence of upgrade-status
    outputs and asserting on the number of polls before success.
    """

    def setUp(self):
        from scripts.Network import Nokia_G42_Upgrade
        self.mod = Nokia_G42_Upgrade

    def _make_script(self, outputs):
        """Build a NokiaG42UpgradeScript whose ``_send_raw`` returns
        the next scripted output on each call. ``time.sleep`` and
        ``_warn_if_slot_stuck`` are stubbed so the test runs fast."""
        script = self.mod.NokiaG42UpgradeScript(
            ip_address="1.2.3.4",
            username="admin",
            password="pw",
            server_url="http://1.2.3.5:8000/",
            manifest_name="ignored.manifest",
            output_callback=lambda *_: None,
            stop_callback=lambda: False,
        )
        outputs = list(outputs)
        script._send_raw = lambda *a, **kw: (
            outputs.pop(0) if outputs else ""
        )
        script._warn_if_slot_stuck = lambda *_a, **_kw: None
        script._echo_relevant = lambda *_a, **_kw: None
        return script

    def test_returns_true_only_after_every_card_completes(self):
        # Three polls -- two should NOT match (mixed state), the
        # third should match (all complete).
        partial = (
            "software-load-installable      ...  apply-in-progress\n"
            "software-load-1-1/installable  ...  apply-in-progress\n"
            "software-load-1-3/installable  ...  apply-in-progress\n"
        )
        mixed = (
            "software-load-installable      ...  apply-complete\n"
            "software-load-1-1/installable  ...  apply-complete\n"
            "software-load-1-3/installable  ...  apply-in-progress\n"
        )
        all_done = (
            "software-load-installable      ...  apply-complete\n"
            "software-load-1-1/installable  ...  apply-complete\n"
            "software-load-1-3/installable  ...  apply-complete\n"
        )
        script = self._make_script([partial, mixed, all_done])

        # Patch time.sleep so the 5s poll interval doesn't slow the test.
        import scripts.Network.Nokia_G42_Upgrade as mod
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda *_: None
        try:
            ok = script._poll_upgrade_status(
                session=None,
                in_progress_token="apply-in-progress",
                complete_token="apply-complete",
                failed_token="apply-failed",
                timeout=60.0,
                poll_interval=0.0,
            )
        finally:
            mod.time.sleep = original_sleep
        self.assertTrue(ok)

    def test_does_not_falsely_succeed_when_one_card_lags(self):
        # The exact field-failure scenario: an aggregate + 1/1 row
        # report complete, but 1/3 is still in-progress. The helper
        # must KEEP polling, not return True. With only this one
        # output and an exhausted feeder returning "", the poll
        # will time out -- which is what we assert.
        mixed = (
            "software-load-installable      ...  apply-complete\n"
            "software-load-1-1/installable  ...  apply-complete\n"
            "software-load-1-3/installable  ...  apply-in-progress\n"
        )
        script = self._make_script([mixed])
        import scripts.Network.Nokia_G42_Upgrade as mod
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda *_: None
        try:
            ok = script._poll_upgrade_status(
                session=None,
                in_progress_token="apply-in-progress",
                complete_token="apply-complete",
                failed_token="apply-failed",
                timeout=0.05,  # tiny -- we want timeout, not success
                poll_interval=0.0,
            )
        finally:
            mod.time.sleep = original_sleep
        self.assertFalse(
            ok,
            "helper falsely reported success while a card was still "
            "in-progress -- this is the exact race that produced the "
            "'Standby is not prepared to process activate' field bug",
        )

    def test_short_circuits_on_failed_row(self):
        failed_row = (
            "software-load-installable      ...  apply-in-progress\n"
            "software-load-1-1/installable  ...  apply-complete\n"
            "software-load-1-3/installable  ...  apply-failed\n"
        )
        script = self._make_script([failed_row])
        import scripts.Network.Nokia_G42_Upgrade as mod
        original_sleep = mod.time.sleep
        mod.time.sleep = lambda *_: None
        try:
            ok = script._poll_upgrade_status(
                session=None,
                in_progress_token="apply-in-progress",
                complete_token="apply-complete",
                failed_token="apply-failed",
                timeout=60.0,
                poll_interval=0.0,
            )
        finally:
            mod.time.sleep = original_sleep
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
