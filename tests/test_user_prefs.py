"""Pin the ``utils.helpers`` user-prefs API + Desktop resolver.

ATLAS persists small bits of UI state (last-used output directory for
the Network Audit, etc.) under ``%APPDATA%\\ATLAS\\prefs.json``. The
contract these tests guard:

  * ``load_user_prefs`` returns ``{}`` for missing / corrupt files
    (callers must never have to special-case the first-run path).
  * ``save_user_prefs`` writes atomically (no half-written files
    visible to a concurrent reader).
  * ``get_desktop_dir`` falls back to ``~/Desktop`` (and finally just
    ``~/Desktop`` even if that doesn't exist) -- it must never raise.
"""
from __future__ import annotations
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import helpers


class TestUserPrefsRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Point APPDATA at a clean dir so we don't touch the real one.
        self._appdata_patch = mock.patch.dict(
            os.environ, {"APPDATA": self.tmpdir},
        )
        self._appdata_patch.start()

    def tearDown(self):
        self._appdata_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_returns_empty_dict_when_missing(self):
        # First-run: no prefs file -- load must return {} without
        # raising so callers don't need to special-case the path.
        prefs = helpers.load_user_prefs()
        self.assertEqual(prefs, {})

    def test_save_then_load_roundtrips(self):
        ok = helpers.save_user_prefs({"foo": "bar", "n": 3})
        self.assertTrue(ok)
        loaded = helpers.load_user_prefs()
        self.assertEqual(loaded, {"foo": "bar", "n": 3})

    def test_load_recovers_from_corrupt_file(self):
        # Write garbage to the prefs file -- load must NOT raise.
        path = helpers.get_user_prefs_path()
        path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(helpers.load_user_prefs(), {})

    def test_load_ignores_non_dict_payload(self):
        # The file format must always be a dict at the top level. If a
        # bad payload (list, string) gets in there, fall back to {} so
        # callers can rely on .get(key, default) semantics.
        path = helpers.get_user_prefs_path()
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertEqual(helpers.load_user_prefs(), {})

    def test_save_is_atomic_via_tmp_file(self):
        # The save path renames a .tmp file into place atomically. Make
        # sure the .tmp doesn't get left behind on a successful write.
        helpers.save_user_prefs({"k": "v"})
        path = helpers.get_user_prefs_path()
        tmp = path.with_suffix(".json.tmp")
        self.assertTrue(path.exists())
        self.assertFalse(
            tmp.exists(),
            "save_user_prefs should clean up its .tmp file after rename",
        )

    def test_prefs_path_lives_under_atlas_appdata(self):
        # Pin the storage location -- if it changes, every existing
        # installation loses its remembered settings on the next
        # upgrade.
        path = helpers.get_user_prefs_path()
        self.assertEqual(path.name, "prefs.json")
        self.assertEqual(path.parent.name, "ATLAS")


class TestDesktopResolver(unittest.TestCase):
    """``get_desktop_dir`` must never raise and must prefer the OS
    Desktop directory when it exists."""

    def test_returns_existing_userprofile_desktop(self):
        with tempfile.TemporaryDirectory() as profile:
            desktop = Path(profile) / "Desktop"
            desktop.mkdir()
            with mock.patch.dict(os.environ, {"USERPROFILE": profile}, clear=False):
                result = helpers.get_desktop_dir()
                self.assertEqual(result, desktop.resolve())

    def test_falls_back_to_home_desktop(self):
        # No USERPROFILE / no OneDrive -- should still return a sane
        # path under the user's home.
        env = {k: v for k, v in os.environ.items()
               if k not in ("USERPROFILE", "OneDrive", "OneDriveConsumer")}
        with mock.patch.dict(os.environ, env, clear=True):
            result = helpers.get_desktop_dir()
            self.assertTrue(str(result).endswith("Desktop"))


class TestNetworkAuditFrameOutputDirResolution(unittest.TestCase):
    """``_resolve_default_output_dir`` is what the Network Audit frame
    asks every time it builds the default output path. Walk through
    the three branches without spinning up Tk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._appdata_patch = mock.patch.dict(
            os.environ, {"APPDATA": self.tmpdir},
        )
        self._appdata_patch.start()

    def tearDown(self):
        self._appdata_patch.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prefers_remembered_directory_when_it_exists(self):
        from gui.network_audit_frame import _resolve_default_output_dir, _PREFS_KEY_AUDIT_DIR
        with tempfile.TemporaryDirectory() as remembered:
            helpers.save_user_prefs({_PREFS_KEY_AUDIT_DIR: remembered})
            self.assertEqual(_resolve_default_output_dir(), remembered)

    def test_falls_back_to_desktop_when_remembered_is_gone(self):
        from gui.network_audit_frame import _resolve_default_output_dir, _PREFS_KEY_AUDIT_DIR
        # Reference a directory that doesn't exist.
        helpers.save_user_prefs(
            {_PREFS_KEY_AUDIT_DIR: r"C:\__definitely_does_not_exist__"}
        )
        # Should NOT return the bogus path -- should fall back to a
        # real directory (Desktop or home).
        result = _resolve_default_output_dir()
        self.assertTrue(os.path.isdir(result))
        self.assertNotIn("__definitely_does_not_exist__", result)

    def test_first_run_defaults_to_desktop(self):
        from gui.network_audit_frame import _resolve_default_output_dir
        # No prefs file -- result must be a real directory.
        result = _resolve_default_output_dir()
        self.assertTrue(os.path.isdir(result))


if __name__ == "__main__":
    unittest.main()
