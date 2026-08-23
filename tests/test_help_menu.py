"""Regression tests for the Help menu wired into InventoryGUI.

The menu adds a 'Check for Updates...' item that calls the same Updater
as the boot-time check, plus an 'About ATLAS' entry. Verified by source
inspection so we don't need to spin up a Tk root in the test runner."""
import inspect
import re
import unittest

from gui import gui4_0


class TestHelpMenu(unittest.TestCase):

    def _src(self):
        return inspect.getsource(gui4_0)

    def test_help_button_builder_present(self):
        src = self._src()
        self.assertIn("def _build_help_button", src)
        self.assertIn("def _on_check_for_updates_clicked", src)
        self.assertIn("def _apply_update_with_prompt", src)
        self.assertIn("def _show_about", src)

    def test_help_button_added_to_setup_gui(self):
        src = self._src()
        # The Help button must be built inside setup_gui and packed to
        # the right side of its parent row.
        setup_match = re.search(r"def setup_gui.*?def\s+\w", src, re.DOTALL)
        self.assertIsNotNone(setup_match, "setup_gui method not located")
        setup_body = setup_match.group(0)
        self.assertIn("_build_help_button", setup_body)

    def test_help_button_right_aligned(self):
        """The Help dropdown must be right-aligned via pack(side=tk.RIGHT),
        not built into a flush-left system menubar."""
        src = self._src()
        # Builder uses Menubutton + pack(side=tk.RIGHT).
        self.assertIn("ttk.Menubutton(", src)
        self.assertRegex(
            src,
            r"self\._help_button\.pack\([^)]*side=tk\.RIGHT",
        )
        # And no longer attaches a top-of-window menubar via root.config(menu=...).
        self.assertNotIn("self.root.config(menu=", src)

    def test_menu_includes_check_for_updates_and_about(self):
        src = self._src()
        self.assertIn('"Check for Updates..."', src)
        self.assertIn('"About ATLAS"', src)

    def test_check_runs_off_main_thread(self):
        """The probe should be dispatched to the worker pool (so a slow
        GitHub response doesn't freeze Tk) and marshalled back via root.after."""
        src = self._src()
        self.assertIn("self._worker_pool.submit", src)
        self.assertIn("self.root.after(0", src)

    def test_uses_updater_class(self):
        src = self._src()
        # The Updater class must be imported at module top.
        self.assertIn("from utils.update import Updater", src)
        # And the menu handler must instantiate it (both paths: frozen + dev).
        self.assertIn("Updater()", src)
        self.assertIn("Updater(get_project_root())", src)

    def test_about_dialog_uses_config_version(self):
        src = self._src()
        self.assertIn('getattr(config, "APP_VERSION"', src)

    def test_boot_time_auto_prompt_scheduled(self):
        """If main.py's boot probe set update_available=True, setup_gui must
        schedule the same prompt the Help menu would trigger so the operator
        sees the same dialog without having to find the menu."""
        src = self._src()
        # Must read self.update_available somewhere in setup_gui and call
        # the same handler as the menu via root.after.
        self.assertRegex(
            src,
            r"if\s+self\.update_available:\s*\n\s+self\.root\.after\([^)]*_on_check_for_updates_clicked",
        )

    def test_visual_update_indicator_helpers_present(self):
        """The Help menu must expose a way to toggle a visible 'update
        pending' marker on both the cascade label and the first item."""
        src = self._src()
        self.assertIn("_set_update_indicator", src)
        self.assertIn("_UPDATE_AVAILABLE_CASCADE_LABEL", src)
        self.assertIn("_UPDATE_AVAILABLE_ITEM_LABEL", src)
        # The markers must be distinct from the defaults (i.e., contain
        # something — a bullet, an asterisk, etc. — that the operator can
        # actually see).
        from gui import gui4_0
        cls = gui4_0.InventoryGUI
        self.assertNotEqual(
            cls._UPDATE_AVAILABLE_CASCADE_LABEL, cls._DEFAULT_CASCADE_LABEL,
            "Cascade label must change when an update is pending",
        )
        self.assertNotEqual(
            cls._UPDATE_AVAILABLE_ITEM_LABEL, cls._DEFAULT_ITEM_LABEL,
            "Menu item label must change when an update is pending",
        )

    def test_no_update_clears_indicator(self):
        """A successful 'no updates' result should drop the badge so a
        stale boot probe doesn't leave an indicator hanging around."""
        src = self._src()
        # In the _on_result branch where available is False:
        self.assertRegex(
            src,
            r"if not available:[\s\S]+?self\._set_update_indicator\(False\)",
        )

    def test_about_dialog_includes_manifesto_and_version(self):
        """The About dialog must surface both the engineer's manifesto and
        the running version + update-channel coordinates."""
        src = self._src()
        # Key opening lines of the manifesto — quoted exactly so a typo
        # in the rendered text would fail the test.
        self.assertIn(
            "Is an engineer not entitled to the hours of their own day?",
            src,
        )
        self.assertIn("I chose ATLAS.", src)
        self.assertIn("This is ATLAS.", src)
        # And the running version metadata still appears.
        self.assertIn("Version:", src)
        self.assertIn("Update channel:", src)
        # The dialog is a Toplevel with a scrolled Text widget so the long
        # body stays readable on small screens.
        self.assertIn("_show_about_dialog", src)
        self.assertIn("scrolledtext.ScrolledText(", src)


if __name__ == "__main__":
    unittest.main()
