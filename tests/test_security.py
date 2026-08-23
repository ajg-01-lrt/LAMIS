"""Tests for OWASP-hardening helpers added during the security audit.

Covers:
  * F006 — validate_uploaded_file (extension/magic-byte/size/symlink checks)
  * F008 — AuthLockout (sliding-window per-IP lockout)
  * F010 — SQL parameterization audit (no string-concat queries in tree)
  * F011/F013/F019 — restrict_path_to_owner (Windows ACL / POSIX chmod)
  * F014 — friendly_error (path/address scrubbing for GUI display)
  * F002/F003/F009 — ensure_host_key_known (pre-flight host-key verification)
"""
import os
import re
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest import mock

import script_interface

# Make sure the project root is importable when pytest is run from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.helpers import (
    AuthLockout,
    AuthLockoutError,
    PathTraversalError,
    UploadValidationError,
    ensure_host_key_known,
    friendly_error,
    restrict_path_to_owner,
    safe_resolve_under,
    sanitize_filename_component,
    validate_uploaded_file,
)


# ---------------------------------------------------------------------------
# F014 — friendly_error
# ---------------------------------------------------------------------------

class TestFriendlyError(unittest.TestCase):

    def test_includes_exception_class_name(self):
        msg = friendly_error(ValueError("boom"))
        self.assertTrue(msg.startswith("ValueError: "))

    def test_strips_windows_path(self):
        e = FileNotFoundError(2, "No such file: 'C:\\Users\\zsimino\\creds_key'")
        out = friendly_error(e)
        self.assertNotIn("zsimino", out)
        self.assertIn("<path>", out)

    def test_strips_posix_path(self):
        e = PermissionError(13, "Denied: '/etc/shadow'")
        out = friendly_error(e)
        self.assertNotIn("/etc/shadow", out)
        self.assertIn("<path>", out)

    def test_strips_memory_address(self):
        out = friendly_error(ValueError("dangling pointer 0x7fffabcd1234"))
        self.assertNotIn("0x7fffabcd1234", out)
        self.assertIn("<addr>", out)

    def test_drops_secondary_lines(self):
        out = friendly_error(Exception("first line\nsecret stack /home/u/.creds"))
        self.assertEqual(out.count("\n"), 0)
        self.assertNotIn(".creds", out)

    def test_caps_length(self):
        out = friendly_error(Exception("x" * 5000))
        self.assertLessEqual(len(out), 220)  # 200 + class prefix

    def test_empty_message_falls_back(self):
        out = friendly_error(Exception(""))
        self.assertIn("unexpected error", out.lower())


# ---------------------------------------------------------------------------
# F011/F013/F019 — restrict_path_to_owner
# ---------------------------------------------------------------------------

class TestRestrictPathToOwner(unittest.TestCase):

    def test_returns_false_for_missing_path(self):
        self.assertFalse(restrict_path_to_owner(r"C:\does\not\exist\zzz"))

    def test_restricts_real_file(self):
        fd, path = tempfile.mkstemp(suffix=".lamis_acl_test")
        os.close(fd)
        try:
            self.assertTrue(restrict_path_to_owner(path))
        finally:
            os.remove(path)

    def test_restricts_directory(self):
        d = tempfile.mkdtemp(prefix="lamis_acl_dir_")
        try:
            self.assertTrue(restrict_path_to_owner(d, is_dir=True))
        finally:
            os.rmdir(d)

    @unittest.skipIf(os.name != "nt", "Windows-only ACL behavior")
    def test_windows_acl_strips_inherited_users(self):
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".lamis_acl_test")
        os.close(fd)
        try:
            restrict_path_to_owner(path)
            r = subprocess.run(["icacls", path], capture_output=True, text=True)
            # No 'BUILTIN\Users' or 'Authenticated Users' should remain
            self.assertNotIn("BUILTIN\\Users", r.stdout)
            self.assertNotIn("Authenticated Users", r.stdout)
        finally:
            os.remove(path)

    def test_never_raises_on_failure(self):
        # Pass a nonsensical path type; helper must swallow and return False
        self.assertFalse(restrict_path_to_owner("\x00invalid\x00"))


# ---------------------------------------------------------------------------
# F006 — validate_uploaded_file
# ---------------------------------------------------------------------------

class TestValidateUploadedFile(unittest.TestCase):

    def _make(self, name: str, content: bytes) -> str:
        p = Path(tempfile.mkdtemp()) / name
        p.write_bytes(content)
        return str(p)

    def test_rejects_unknown_extension(self):
        p = self._make("evil.exe", b"MZ\x90\x00")
        with self.assertRaises(UploadValidationError):
            validate_uploaded_file(p)

    def test_rejects_nonexistent(self):
        with self.assertRaises(UploadValidationError):
            validate_uploaded_file(r"C:\does\not\exist.xlsx")

    def test_rejects_bad_xlsx_magic(self):
        p = self._make("fake.xlsx", b"NOTAZIPFILE")
        with self.assertRaises(UploadValidationError):
            validate_uploaded_file(p)

    def test_accepts_real_xlsx_magic(self):
        # xlsx is a zip — PK\x03\x04 is the local-file-header magic
        p = self._make("good.xlsx", b"PK\x03\x04" + b"\x00" * 100)
        # Should not raise
        validate_uploaded_file(p)

    def test_accepts_csv(self):
        p = self._make("data.csv", b"col1,col2\n1,2\n3,4\n")
        validate_uploaded_file(p)

    def test_rejects_csv_with_nul(self):
        p = self._make("evil.csv", b"col1,col2\n1,\x002\n")
        with self.assertRaises(UploadValidationError):
            validate_uploaded_file(p)

    def test_rejects_oversize(self):
        # Use a tiny cap so we don't write 100 MB
        p = self._make("huge.xlsx", b"PK\x03\x04" + b"\x00" * (1024 * 1024))
        with self.assertRaises(UploadValidationError):
            validate_uploaded_file(p, max_bytes=1024)


# ---------------------------------------------------------------------------
# F008 — AuthLockout
# ---------------------------------------------------------------------------

class TestAuthLockout(unittest.TestCase):

    def setUp(self):
        # AuthLockout is class-state; tighten constants and reset history per test.
        AuthLockout._failures.clear()
        self._orig = (AuthLockout.MAX_ATTEMPTS, AuthLockout.WINDOW,
                      AuthLockout.COOLDOWN, AuthLockout.BACKOFFS)
        AuthLockout.MAX_ATTEMPTS = 3
        AuthLockout.WINDOW = 5.0
        AuthLockout.COOLDOWN = 2.0
        AuthLockout.BACKOFFS = (0.0, 0.0, 0.0)

    def tearDown(self):
        AuthLockout._failures.clear()
        (AuthLockout.MAX_ATTEMPTS, AuthLockout.WINDOW,
         AuthLockout.COOLDOWN, AuthLockout.BACKOFFS) = self._orig

    def test_first_attempt_passes(self):
        AuthLockout.check("10.0.0.1", sleep=False)

    def test_locks_after_max_attempts(self):
        for _ in range(3):
            AuthLockout.check("10.0.0.2", sleep=False)
            AuthLockout.register_failure("10.0.0.2")
        with self.assertRaises(AuthLockoutError):
            AuthLockout.check("10.0.0.2", sleep=False)

    def test_success_clears_state(self):
        for _ in range(2):
            AuthLockout.check("10.0.0.3", sleep=False)
            AuthLockout.register_failure("10.0.0.3")
        AuthLockout.register_success("10.0.0.3")
        AuthLockout.check("10.0.0.3", sleep=False)

    def test_per_ip_isolation(self):
        for _ in range(3):
            AuthLockout.check("10.0.0.4", sleep=False)
            AuthLockout.register_failure("10.0.0.4")
        AuthLockout.check("10.0.0.5", sleep=False)

    def test_cooldown_expires(self):
        for _ in range(3):
            AuthLockout.check("10.0.0.6", sleep=False)
            AuthLockout.register_failure("10.0.0.6")
        with self.assertRaises(AuthLockoutError):
            AuthLockout.check("10.0.0.6", sleep=False)
        time.sleep(2.2)  # cooldown=2s
        AuthLockout.check("10.0.0.6", sleep=False)


# ---------------------------------------------------------------------------
# F010 — SQL parameterization audit (regression guard)
# ---------------------------------------------------------------------------

class TestSqlParameterization(unittest.TestCase):
    """Catch any future code that builds SQL via string concat / f-string."""

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    EXCLUDE_DIRS = {".venv", "build", "dist", "__pycache__", "tests"}
    # Detect dangerous patterns: execute("...{...}".format) or execute(f"...")
    BAD_PATTERNS = [
        re.compile(r'\.execute\s*\(\s*f["\']'),
        re.compile(r'\.execute\s*\(\s*["\'][^"\']*["\']\s*\.\s*format\s*\('),
        re.compile(r'\.execute\s*\(\s*["\'][^"\']*["\']\s*\+'),
    ]

    def test_no_string_concat_or_fstring_sql(self):
        offenders = []
        for py in self.PROJECT_ROOT.rglob("*.py"):
            if any(part in self.EXCLUDE_DIRS for part in py.parts):
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for pat in self.BAD_PATTERNS:
                if pat.search(text):
                    offenders.append(str(py))
                    break
        self.assertEqual(offenders, [], f"Found unsafe SQL in: {offenders}")


# ---------------------------------------------------------------------------
# F002/F003/F009 — ensure_host_key_known
# ---------------------------------------------------------------------------

class TestEnsureHostKeyKnown(unittest.TestCase):

    def test_returns_false_when_paramiko_missing(self):
        with mock.patch("utils.helpers.paramiko", None):
            self.assertFalse(ensure_host_key_known("10.0.0.1"))

    def test_fast_path_when_key_already_known(self):
        # Build a SSHClient mock whose host_keys.lookup() returns truthy
        with mock.patch("utils.helpers.paramiko") as pmock:
            client = mock.MagicMock()
            client.get_host_keys.return_value.lookup.return_value = object()
            pmock.SSHClient.return_value = client
            self.assertTrue(ensure_host_key_known("10.0.0.7"))
            # connect should NOT have been called
            client.connect.assert_not_called()

    def test_returns_false_when_user_rejects(self):
        with mock.patch("utils.helpers.paramiko") as pmock:
            client = mock.MagicMock()
            client.get_host_keys.return_value.lookup.return_value = None
            # Simulate the prompting policy raising "rejected by user"
            pmock.SSHException = Exception
            pmock.AuthenticationException = type("AE", (Exception,), {})
            client.connect.side_effect = pmock.SSHException(
                "Host key for 10.0.0.8 rejected by user"
            )
            pmock.SSHClient.return_value = client
            self.assertFalse(ensure_host_key_known("10.0.0.8"))


# ---------------------------------------------------------------------------
# F016 — Updater.restart_program sanitizes argv (no echo of user-supplied args)
# ---------------------------------------------------------------------------

class TestRestartArgvSanitization(unittest.TestCase):

    def test_safe_restart_argv_drops_user_args(self):
        from utils.update import Updater
        with mock.patch.object(sys, "argv",
                               ["main.py", "--password=secret", "--token", "abc123"]):
            argv = Updater._safe_restart_argv()
        self.assertEqual(argv, ["main.py"])
        self.assertNotIn("--password=secret", argv)
        self.assertNotIn("abc123", argv)

    def test_safe_restart_argv_allows_opt_in_safe_extras(self):
        from utils.update import Updater
        with mock.patch.object(sys, "argv", ["main.py"]), \
             mock.patch.dict(os.environ, {"LAMIS_RESTART_ARGS": "--debug --safe"}):
            argv = Updater._safe_restart_argv()
        self.assertEqual(argv, ["main.py", "--debug", "--safe"])

    def test_safe_restart_argv_filters_dangerous_chars_from_extras(self):
        from utils.update import Updater
        with mock.patch.object(sys, "argv", ["main.py"]), \
             mock.patch.dict(os.environ,
                             {"LAMIS_RESTART_ARGS": "--ok ;rm $(whoami) `id`"}):
            argv = Updater._safe_restart_argv()
        self.assertIn("--ok", argv)
        for bad in (";rm", "$(whoami)", "`id`"):
            self.assertNotIn(bad, argv)

    def test_no_raw_sys_argv_splat_in_update_module(self):
        update_src = (Path(__file__).resolve().parent.parent
                      / "utils" / "update.py").read_text(encoding="utf-8")
        # The dangerous pattern os.execl(..., *sys.argv) must not appear.
        self.assertNotRegex(update_src, r"os\.execl\([^)]*\*sys\.argv")


# ---------------------------------------------------------------------------
# F017 — requirements.lock exists and uses --hash entries
# ---------------------------------------------------------------------------

class TestRequirementsLock(unittest.TestCase):

    def test_lock_file_exists_and_is_hash_pinned(self):
        lock = Path(__file__).resolve().parent.parent / "requirements.lock"
        if not lock.exists():
            self.skipTest("requirements.lock not generated in this environment")
        text = lock.read_text(encoding="utf-8")
        self.assertIn("--hash=sha256:", text)
        # Every non-comment, non-blank line must be a package pin or a hash line.
        bad = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("--hash=sha256:"):
                continue
            # package lines end with a continuation backslash and contain ==
            if "==" in s and s.endswith("\\"):
                continue
            bad.append(ln)
        self.assertEqual(bad, [], f"Unexpected lock-file lines: {bad[:3]}")


# ---------------------------------------------------------------------------
# F022 — Telnet timeout enforcement (no infinite blocks, no busy-spin)
# ---------------------------------------------------------------------------

class TestTelnetTimeout(unittest.TestCase):

    def _fake_telnet(self):
        from utils.telnet import Telnet
        t = Telnet.__new__(Telnet)
        t._buf = b""
        t._default_timeout = None

        class _Sock:
            def settimeout(self, _):
                pass

            def recv(self, _n):
                import socket as _s
                raise _s.timeout()
        t._sock = _Sock()
        return t

    def test_read_until_returns_within_timeout(self):
        t = self._fake_telnet()
        start = time.monotonic()
        out = t.read_until(b"NEVER", timeout=0.2)
        elapsed = time.monotonic() - start
        self.assertEqual(out, b"")
        self.assertLess(elapsed, 1.5)

    def test_zero_timeout_is_floored_not_infinite(self):
        # timeout=0 used to be treated as "no timeout" by the old `or 30` logic;
        # now it must be floored to a small positive value and return promptly.
        t = self._fake_telnet()
        start = time.monotonic()
        t.read_until(b"NEVER", timeout=0)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_expect_returns_within_timeout(self):
        t = self._fake_telnet()
        start = time.monotonic()
        idx, m, data = t.expect([b"never-matches"], timeout=0.2)
        self.assertEqual(idx, -1)
        self.assertIsNone(m)
        self.assertLess(time.monotonic() - start, 1.5)


# ---------------------------------------------------------------------------
# F018 — sanitize_filename_component / safe_resolve_under
# ---------------------------------------------------------------------------

class TestSanitizeFilenameComponent(unittest.TestCase):

    def test_strips_path_separators(self):
        for evil in ("..\\..\\Windows", "../../etc/passwd", "a/b\\c"):
            out = sanitize_filename_component(evil)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)

    def test_keeps_safe_chars(self):
        self.assertEqual(sanitize_filename_component("Acme_Corp-2024"),
                         "Acme_Corp-2024")

    def test_replaces_unsafe_with_underscore(self):
        self.assertNotIn(":", sanitize_filename_component("C:bad"))
        self.assertNotIn("*", sanitize_filename_component("a*b"))

    def test_collapses_runs_and_trims(self):
        self.assertEqual(sanitize_filename_component("  ...weird   name..."),
                         "weird_name")

    def test_empty_yields_fallback(self):
        self.assertEqual(sanitize_filename_component(""), "Unnamed")
        self.assertEqual(sanitize_filename_component(None), "Unnamed")
        self.assertEqual(sanitize_filename_component("///"), "Unnamed")

    def test_caps_length(self):
        out = sanitize_filename_component("a" * 500, max_length=20)
        self.assertLessEqual(len(out), 20)

    def test_windows_reserved_names_prefixed(self):
        self.assertTrue(sanitize_filename_component("CON").startswith("_"))
        self.assertTrue(sanitize_filename_component("nul.txt").startswith("_"))


class TestSafeResolveUnder(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="lamis_resolve_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_relative_child_ok(self):
        out = safe_resolve_under(self.base, "subdir/file.xlsx")
        self.assertTrue(str(out).startswith(str(self.base.resolve())))

    def test_absolute_child_ok(self):
        target = self.base / "abs.xlsx"
        self.assertEqual(safe_resolve_under(self.base, target), target.resolve())

    def test_dotdot_escape_blocked(self):
        with self.assertRaises(PathTraversalError):
            safe_resolve_under(self.base, "../escaped.xlsx")

    def test_unrelated_absolute_blocked(self):
        with self.assertRaises(PathTraversalError):
            safe_resolve_under(self.base, "C:\\Windows\\System32\\evil.xlsx"
                               if os.name == "nt" else "/etc/passwd")


# ---------------------------------------------------------------------------
# F025 — Dynamic-import allowlist
# ---------------------------------------------------------------------------

class TestScriptModuleAllowlist(unittest.TestCase):

    def test_script_interface_allowlist_matches_mapping(self):
        from script_interface import ScriptSelector
        self.assertEqual(
            set(ScriptSelector._ALLOWED_SCRIPT_MODULES),
            set(ScriptSelector._device_type_to_script.values()),
        )
        # Sanity: no wildcards / dotted escapes.
        for m in ScriptSelector._ALLOWED_SCRIPT_MODULES:
            self.assertTrue(m.startswith("scripts."))
            self.assertNotIn("..", m)

    def test_script_interface_rejects_non_allowlisted(self):
        from script_interface import ScriptSelector
        sel = ScriptSelector()
        # Inject a poisoned mapping entry and confirm it is refused.
        with mock.patch.dict(
            ScriptSelector._device_type_to_script,
            {"poisoned": "os"}, clear=False,
        ):
            result = sel.select_script("poisoned", "10.0.0.1")
            self.assertIsNone(result)

    def test_inventory_gui_allowlist_matches_mapping(self):
        from gui.gui4_0 import InventoryGUI
        self.assertEqual(
            set(InventoryGUI._ALLOWED_SCRIPT_MODULES),
            set(InventoryGUI._manual_script_modules.values()),
        )

    def test_inventory_gui_rejects_non_allowlisted(self):
        from gui.gui4_0 import InventoryGUI
        gui = InventoryGUI.__new__(InventoryGUI)  # bypass __init__ (needs Tk)
        with mock.patch.dict(
            InventoryGUI._manual_script_modules,
            {"Evil": "os"}, clear=False,
        ):
            with self.assertRaises(ValueError) as cm:
                gui._build_manual_script_instance({
                    "manual_script": "Evil",
                    "connection_mode": "LAN",
                })
            self.assertIn("non-allowlisted", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# F004 — Telnet policy gate (warn + allowlist + SSH-preferred + bypass)
# ---------------------------------------------------------------------------

class TestTelnetPolicy(unittest.TestCase):

    def setUp(self):
        from utils import telnet_policy
        self.tp = telnet_policy
        self.tp.reset_policy_caches()
        # Redirect the allowlist file to a per-test temp location.
        self.tmp = Path(tempfile.mkdtemp(prefix="lamis_tnp_"))
        self._patch = mock.patch.object(self.tp, "_allowlist_path",
                                        return_value=self.tmp / "telnet_allowlist.json")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tp.reset_policy_caches()
        for var in (self.tp.ENV_REQUIRE_ENCRYPTED, self.tp.ENV_DISABLE_SSH_PROBE):
            os.environ.pop(var, None)

    def test_default_deny_when_allowlist_empty(self):
        with mock.patch.object(self.tp, "ssh_port_open", return_value=False):
            with self.assertRaises(self.tp.TelnetPolicyError) as cm:
                self.tp.enforce_telnet_policy("10.0.0.1")
            self.assertIn("allowlist", str(cm.exception).lower())

    def test_env_kill_switch_blocks_even_allowlisted_hosts(self):
        self.tp.add_telnet_allowlist("10.0.0.1", "test")
        os.environ[self.tp.ENV_REQUIRE_ENCRYPTED] = "1"
        with self.assertRaises(self.tp.TelnetPolicyError):
            self.tp.enforce_telnet_policy("10.0.0.1")

    def test_env_kill_switch_blocks_bypass_too(self):
        os.environ[self.tp.ENV_REQUIRE_ENCRYPTED] = "1"
        with self.assertRaises(self.tp.TelnetPolicyError):
            self.tp.enforce_telnet_policy("10.0.0.1", bypass=True)

    def test_bypass_is_allowed_when_no_kill_switch(self):
        # No allowlist entry, no SSH probe — bypass must still succeed.
        with mock.patch.object(self.tp, "ssh_port_open", return_value=True):
            self.tp.enforce_telnet_policy("10.0.0.1", bypass=True,
                                          purpose="tl1-test")

    def test_allowlist_exact_ip_match(self):
        self.tp.add_telnet_allowlist("10.0.0.42", "test")
        with mock.patch.object(self.tp, "ssh_port_open", return_value=False):
            self.tp.enforce_telnet_policy("10.0.0.42")  # no raise

    def test_allowlist_cidr_match(self):
        self.tp.add_telnet_allowlist("10.0.0.0/24", "lab subnet")
        with mock.patch.object(self.tp, "ssh_port_open", return_value=False):
            self.tp.enforce_telnet_policy("10.0.0.99")  # no raise
        with mock.patch.object(self.tp, "ssh_port_open", return_value=False):
            with self.assertRaises(self.tp.TelnetPolicyError):
                self.tp.enforce_telnet_policy("10.0.1.5")

    def test_ssh_preferred_blocks_even_allowlisted_host(self):
        self.tp.add_telnet_allowlist("10.0.0.5", "test")
        with mock.patch.object(self.tp, "ssh_port_open", return_value=True):
            with self.assertRaises(self.tp.TelnetPolicyError) as cm:
                self.tp.enforce_telnet_policy("10.0.0.5")
            self.assertIn("ssh", str(cm.exception).lower())

    def test_skip_ssh_probe_allows_allowlisted_host_with_ssh_open(self):
        # Nokia 1830-family PSI: SSH is up on :22 but dead-ends, so Telnet is
        # required. skip_ssh_probe keeps the allowlist gate but bypasses the
        # "prefer SSH" refusal.
        self.tp.add_telnet_allowlist("10.0.0.7", "test")
        with mock.patch.object(self.tp, "ssh_port_open", return_value=True):
            self.tp.enforce_telnet_policy("10.0.0.7", skip_ssh_probe=True)  # no raise

    def test_skip_ssh_probe_still_requires_allowlist(self):
        # skip_ssh_probe only waives Layer C, not the allowlist (Layer B).
        with mock.patch.object(self.tp, "ssh_port_open", return_value=True):
            with self.assertRaises(self.tp.TelnetPolicyError) as cm:
                self.tp.enforce_telnet_policy("10.0.0.8", skip_ssh_probe=True)
            self.assertIn("allowlist", str(cm.exception).lower())

    def test_ssh_probe_disabled_via_env_var(self):
        self.tp.add_telnet_allowlist("10.0.0.6", "test")
        os.environ[self.tp.ENV_DISABLE_SSH_PROBE] = "1"
        # ssh_port_open must short-circuit to False without opening a socket.
        with mock.patch("socket.create_connection",
                        side_effect=AssertionError("must not probe")):
            self.assertFalse(self.tp.ssh_port_open("10.0.0.6", use_cache=False))
            self.tp.enforce_telnet_policy("10.0.0.6")  # no raise

    def test_remove_allowlist(self):
        self.tp.add_telnet_allowlist("1.2.3.4", "x")
        self.assertTrue(self.tp.is_telnet_allowed("1.2.3.4"))
        self.assertTrue(self.tp.remove_telnet_allowlist("1.2.3.4"))
        self.assertFalse(self.tp.is_telnet_allowed("1.2.3.4"))
        self.assertFalse(self.tp.remove_telnet_allowlist("1.2.3.4"))

    def test_telnet_class_invokes_policy_before_socket(self):
        from utils import telnet as tn_mod
        with mock.patch.object(tn_mod.socket, "create_connection",
                               side_effect=AssertionError("must not connect")):
            with self.assertRaises(self.tp.TelnetPolicyError):
                tn_mod.Telnet("10.99.99.99", port=23, timeout=1)

    def test_telnet_class_bypass_skips_allowlist_but_opens_socket(self):
        from utils import telnet as tn_mod
        fake_sock = mock.MagicMock()
        with mock.patch.object(tn_mod.socket, "create_connection",
                               return_value=fake_sock) as cc:
            tn_mod.Telnet("10.99.99.99", port=23, timeout=1,
                          bypass_policy=True, purpose="tl1-test")
            cc.assert_called_once()


# ---------------------------------------------------------------------------
# F023 — cleanup_stale_lamis_tempfiles
# ---------------------------------------------------------------------------

class TestStaleTempfileCleanup(unittest.TestCase):

    def setUp(self):
        from utils import helpers
        self.helpers = helpers
        self.tmp_root = Path(tempfile.mkdtemp(prefix="lamis_test_root_"))
        self._patch = mock.patch("tempfile.gettempdir",
                                 return_value=str(self.tmp_root))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        import shutil
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _make(self, name, age_seconds, is_dir=False):
        p = self.tmp_root / name
        if is_dir:
            p.mkdir()
        else:
            p.write_bytes(b"x")
        ts = time.time() - age_seconds
        os.utime(p, (ts, ts))
        return p

    def test_removes_old_atlas_dir(self):
        old = self._make("ATLAS_oldjob", age_seconds=48 * 3600, is_dir=True)
        n = self.helpers.cleanup_stale_lamis_tempfiles(max_age_hours=24)
        self.assertGreaterEqual(n, 1)
        self.assertFalse(old.exists())

    def test_removes_old_packing_slip_file(self):
        old = self._make("PackingSlip_abc.xlsx", age_seconds=48 * 3600)
        self.helpers.cleanup_stale_lamis_tempfiles(max_age_hours=24)
        self.assertFalse(old.exists())

    def test_keeps_fresh_artifacts(self):
        fresh = self._make("ATLAS_running", age_seconds=60, is_dir=True)
        self.helpers.cleanup_stale_lamis_tempfiles(max_age_hours=24)
        self.assertTrue(fresh.exists())

    def test_ignores_unrelated_files(self):
        other = self._make("not_ours.txt", age_seconds=999 * 3600)
        self.helpers.cleanup_stale_lamis_tempfiles(max_age_hours=1)
        self.assertTrue(other.exists())


# ----------------------------------------------------------------------
# F021 — ICMP rate limiting
# ----------------------------------------------------------------------
class TestPingRateLimit(unittest.TestCase):
    def setUp(self):
        from utils import helpers
        self.helpers = helpers
        helpers.reset_ping_rate_limiter()

    def test_acquires_token_under_limit(self):
        # First few pings to a fresh host should succeed instantly.
        for _ in range(5):
            self.assertTrue(self.helpers.acquire_ping_token("10.0.0.1", timeout=0.1))

    def test_per_host_burst_then_throttled(self):
        # Burst capacity is 5/host. The 6th in a tight loop should be denied
        # within a small timeout because tokens haven't refilled yet.
        host = "10.0.0.42"
        for _ in range(5):
            self.assertTrue(self.helpers.acquire_ping_token(host, timeout=0.05))
        denied_at_least_once = not self.helpers.acquire_ping_token(host, timeout=0.01)
        self.assertTrue(denied_at_least_once)

    def test_separate_hosts_have_independent_buckets(self):
        # Drain host A; host B must still have a full bucket.
        for _ in range(5):
            self.helpers.acquire_ping_token("10.0.0.1", timeout=0.05)
        self.assertTrue(self.helpers.acquire_ping_token("10.0.0.2", timeout=0.05))

    def test_thread_safety(self):
        # Hammer the limiter from multiple threads; should not crash or deadlock.
        import threading
        errors = []

        def worker():
            try:
                for _ in range(20):
                    self.helpers.acquire_ping_token("10.0.0.99", timeout=0.5)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])

    def test_reset_refills_buckets(self):
        host = "10.0.0.5"
        for _ in range(5):
            self.helpers.acquire_ping_token(host, timeout=0.05)
        self.helpers.reset_ping_rate_limiter()
        self.assertTrue(self.helpers.acquire_ping_token(host, timeout=0.05))


# ----------------------------------------------------------------------
# F026 — DataFrame whitespace stripping
# ----------------------------------------------------------------------
class TestStripDataFrameStrings(unittest.TestCase):
    def setUp(self):
        try:
            import pandas as pd  # noqa: F401
        except ImportError:
            self.skipTest("pandas not available")
        from utils import helpers
        self.helpers = helpers

    def test_strips_leading_and_trailing(self):
        import pandas as pd
        df = pd.DataFrame({"ip": [" 10.0.0.1 ", "10.0.0.2\t"], "n": [1, 2]})
        self.helpers.strip_dataframe_strings(df)
        self.assertEqual(df["ip"].iloc[0], "10.0.0.1")
        self.assertEqual(df["ip"].iloc[1], "10.0.0.2")

    def test_leaves_numeric_columns_alone(self):
        import pandas as pd
        df = pd.DataFrame({"n": [1, 2, 3]})
        self.helpers.strip_dataframe_strings(df)
        self.assertEqual(list(df["n"]), [1, 2, 3])

    def test_empty_strings_become_na(self):
        import pandas as pd
        df = pd.DataFrame({"x": ["  ", "real"]})
        self.helpers.strip_dataframe_strings(df)
        self.assertTrue(pd.isna(df["x"].iloc[0]))
        self.assertEqual(df["x"].iloc[1], "real")

    def test_handles_none_input(self):
        # Should not raise.
        result = self.helpers.strip_dataframe_strings(None)
        self.assertIsNone(result)

    def test_specific_columns_only(self):
        import pandas as pd
        df = pd.DataFrame({"a": [" x "], "b": [" y "]})
        self.helpers.strip_dataframe_strings(df, columns=["a"])
        self.assertEqual(df["a"].iloc[0], "x")
        self.assertEqual(df["b"].iloc[0], " y ")


# ----------------------------------------------------------------------
# F020 — Best-effort password widget scrubbing
# ----------------------------------------------------------------------
class TestScrubPasswordWidget(unittest.TestCase):
    def setUp(self):
        from utils import helpers
        self.helpers = helpers

    def test_clears_stringvar_like_object(self):
        class FakeVar:
            def __init__(self):
                self._v = "hunter2"

            def get(self):
                return self._v

            def set(self, v):
                self._v = v

        v = FakeVar()
        self.helpers.scrub_password_widget(v)
        self.assertEqual(v.get(), "")

    def test_clears_entry_like_object(self):
        class FakeEntry:
            def __init__(self):
                self.deleted = False

            def delete(self, start, end):
                self.deleted = (start, end)

        e = FakeEntry()
        self.helpers.scrub_password_widget(e)
        self.assertEqual(e.deleted, (0, "end"))

    def test_handles_none_silently(self):
        # Should not raise.
        self.helpers.scrub_password_widget(None, None)

    def test_handles_failing_widget_silently(self):
        class Broken:
            def delete(self, *a, **kw):
                raise RuntimeError("widget destroyed")

            def set(self, *a, **kw):
                raise RuntimeError("widget destroyed")

        # Must not raise — scrubbing is best-effort.
        self.helpers.scrub_password_widget(Broken())

    def test_clears_multiple_widgets_at_once(self):
        cleared = []

        class W:
            def __init__(self, name):
                self.name = name

            def delete(self, s, e):
                cleared.append(("del", self.name))

            def set(self, v):
                cleared.append(("set", self.name, v))

        a, b = W("a"), W("b")
        self.helpers.scrub_password_widget(a, b)
        # Both widgets should have had both delete() and set() called.
        self.assertIn(("del", "a"), cleared)
        self.assertIn(("set", "a", ""), cleared)
        self.assertIn(("del", "b"), cleared)
        self.assertIn(("set", "b", ""), cleared)


# ---------------------------------------------------------------------------
# Default credential seed — order: admin/admin, cli/admin, su/Ciena123,
# diaguser/Ciena123, ADMIN/ADMIN.
# ---------------------------------------------------------------------------
class TestDefaultCredentialSeed(unittest.TestCase):
    """The built-in seed must be admin/admin first, cli/admin (Nokia 1830)
    second, su/Ciena123 (Ciena shell) third, diaguser/Ciena123 (RLS
    RESTCONF) fourth, ADMIN/ADMIN last; the seeding routine must
    migrate pre-existing encrypted config files by appending newly-
    added entries (without disturbing the user's primary credentials
    or the order of existing defaults)."""

    def setUp(self):
        # Use a throwaway config path for each test to avoid touching the
        # developer's real credentials_config.json.
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmppath = Path(self._tmpdir.name) / "credentials_config.json"
        from utils import credentials as creds_mod
        self.creds_mod = creds_mod
        self._orig_path = creds_mod.CREDS_CONFIG_FILE
        creds_mod.CREDS_CONFIG_FILE = self._tmppath

    def tearDown(self):
        self.creds_mod.CREDS_CONFIG_FILE = self._orig_path
        self._tmpdir.cleanup()

    def test_seed_order_includes_admin_first(self):
        """admin/admin, cli/admin, su/Ciena123, diaguser/Ciena123,
        ADMIN/ADMIN — in that order. ``diaguser`` is the RLS RESTCONF
        account (distinct from the shell user ``su``)."""
        self.assertEqual(
            self.creds_mod._BUILTIN_DEFAULT_SEED,
            [
                ("admin", "admin"),
                ("cli", "admin"),
                ("su", "Ciena123"),
                ("diaguser", "Ciena123"),
                ("ADMIN", "ADMIN"),
            ],
        )

    def test_fresh_install_seeds_all_four(self):
        # Test name is historical -- there are now 5 seeded entries.
        self.creds_mod._seed_defaults_into_config()
        pairs = self.creds_mod.get_default_credentials_to_try()
        self.assertEqual(
            pairs,
            [
                ("admin", "admin"),
                ("cli", "admin"),
                ("su", "Ciena123"),
                ("diaguser", "Ciena123"),
                ("ADMIN", "ADMIN"),
            ],
        )

    def test_upgrade_migration_adds_new_entry(self):
        """Legacy install with only admin/admin + ADMIN/ADMIN must end up with
        the full 4-entry seed in seed order (admin, cli, su, ADMIN); the
        pre-existing ADMIN/ADMIN entry is reordered into its seed slot.

        ATLAS no longer persists user-entered credentials, so a legacy
        ``credentials`` block in the config must be stripped during the
        upgrade migration."""
        import json
        legacy = {
            "credentials": {"primary": {"username": "operator",
                                          "password": self.creds_mod._encrypt_password("s3cret")}},
            "defaults": [
                {"username": "admin",
                 "password": self.creds_mod._encrypt_password("admin")},
                {"username": "ADMIN",
                 "password": self.creds_mod._encrypt_password("ADMIN")},
            ],
        }
        self._tmppath.write_text(json.dumps(legacy))

        self.creds_mod._seed_defaults_into_config()

        pairs = self.creds_mod.get_default_credentials_to_try()
        self.assertEqual(
            pairs,
            [("admin", "admin"), ("cli", "admin"), ("su", "Ciena123"),
             ("diaguser", "Ciena123"), ("ADMIN", "ADMIN")],
        )
        # Legacy user-credentials block must be removed by the migration.
        cfg = json.loads(self._tmppath.read_text())
        self.assertNotIn("credentials", cfg)

    def test_upgrade_reorders_existing_entries_in_place(self):
        """Old install where the previous order was cli/admin/ADMIN — seeder
        must rewrite to the new (admin, cli, su, ADMIN) seed order, leaving
        user-added extras (operator/custom) as trailing non-seed entries."""
        import json
        legacy = {
            "credentials": {"primary": {"username": "operator",
                                          "password": self.creds_mod._encrypt_password("s3cret")}},
            "defaults": [
                {"username": "cli",
                 "password": self.creds_mod._encrypt_password("admin")},
                {"username": "admin",
                 "password": self.creds_mod._encrypt_password("admin")},
                {"username": "ADMIN",
                 "password": self.creds_mod._encrypt_password("ADMIN")},
                # User-added extra that's not in the seed.
                {"username": "operator",
                 "password": self.creds_mod._encrypt_password("custom")},
            ],
        }
        self._tmppath.write_text(json.dumps(legacy))

        self.creds_mod._seed_defaults_into_config()

        pairs = self.creds_mod.get_default_credentials_to_try()
        self.assertEqual(
            pairs,
            [("admin", "admin"), ("cli", "admin"), ("su", "Ciena123"),
             ("diaguser", "Ciena123"), ("ADMIN", "ADMIN"),
             ("operator", "custom")],
        )

    def test_re_seed_is_noop_when_all_present(self):
        """Running the seed routine twice must not duplicate entries."""
        self.creds_mod._seed_defaults_into_config()
        self.creds_mod._seed_defaults_into_config()
        pairs = self.creds_mod.get_default_credentials_to_try()
        self.assertEqual(len(pairs), len(self.creds_mod._BUILTIN_DEFAULT_SEED))


# ---------------------------------------------------------------------------
# Nokia 1830 two-stage SSH login (invoke_shell post-auth dialog)
# ---------------------------------------------------------------------------
class TestNokia1830TwoStageLogin(unittest.TestCase):
    """The 1830 SSH server only accepts the "cli" account; once SSH is up,
    the device prompts a second time via its own CLI for the operator
    user/password. The DeviceIdentifier must drive that dialog through
    paramiko's invoke_shell channel."""

    def setUp(self):
        from script_interface import DeviceIdentifier
        self.ident = DeviceIdentifier()

    def _make_fake_session(self, scripted_chunks):
        """Return a fake invoke_shell channel.

        *scripted_chunks* is a list of lists: scripted_chunks[i] is the data
        the device returns AFTER the i-th send() call. scripted_chunks[0]
        is the initial banner before any send. Each list contains the chunks
        recv() will yield on consecutive calls, with recv_ready() returning
        False once that batch is exhausted (until the next send()).
        """
        sent = []
        state = {"phase": 0, "queue": list(scripted_chunks[0])}

        class FakeSession:
            def __init__(self):
                self.sent = sent

            def send(inner, data):
                sent.append(data)
                state["phase"] += 1
                if state["phase"] < len(scripted_chunks):
                    state["queue"].extend(scripted_chunks[state["phase"]])

            def recv_ready(inner):
                return bool(state["queue"])

            def recv(inner, n):
                if not state["queue"]:
                    return b""
                chunk = state["queue"].pop(0)
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                return chunk

            def close(inner):
                # production code closes the shell channel in `finally`
                # blocks; no-op here.
                return None

        return FakeSession()

    def test_two_stage_login_sends_user_then_pass(self):
        """Username -> 'admin', Password -> 'admin', and the function reports
        success when a shell prompt is seen at the end."""
        sess = self._make_fake_session([
            ["Welcome to Nokia 1830\r\nUsername: "],   # initial banner
            ["Password: "],                            # after sending admin
            ["user@nokia# "],                          # after sending password
        ])
        ok = self.ident._do_1830_two_stage_login(
            sess, Queue(), shell_user="admin", shell_pass="admin"
        )
        self.assertTrue(ok)
        self.assertEqual(sess.sent, ["admin\n", "admin\n"])

    def test_two_stage_login_detects_inner_auth_failure(self):
        sess = self._make_fake_session([
            ["Username: "],
            ["Password: "],
            ["Login incorrect\r\nUsername: "],
        ])
        ok = self.ident._do_1830_two_stage_login(
            sess, Queue(), shell_user="admin", shell_pass="wrong"
        )
        self.assertFalse(ok)

    def test_two_stage_login_aborts_when_no_username_prompt(self):
        sess = self._make_fake_session([["just a regular shell$ "]])
        ok = self.ident._do_1830_two_stage_login(
            sess, Queue(), shell_user="admin", shell_pass="admin"
        )
        self.assertFalse(ok)
        self.assertEqual(sess.sent, [])

    def test_ssh_attempt_uses_cli_account_for_1830_username(self):
        """When username == 'cli', _ssh_identify_attempt must call the
        1830-specific connect helper (which performs the auth-method
        cascade), then drive the two-stage shell login, then issue the
        identification commands."""
        captured = {}
        outer = self

        # Fake "ssh client" returned by the 1830 connect helper. Only needs
        # to support invoke_shell() and close().
        class FakeClient:
            def __init__(self):
                self.closed = False

            def save_host_keys(inner, _path):
                # paramiko.SSHClient API surface — production code calls
                # this after a successful connect to persist any newly
                # learnt host keys. No-op in tests.
                return None

            def invoke_shell(inner):
                # Sequence:
                #   [0] pre-login banner with Username: prompt
                #   [1] after sending "admin\n" → Password: prompt
                #   [2] after sending "admin\n" → shell prompt
                #   [3] after sending "show general detail\n" → ident output
                return outer._make_fake_session([
                    ["Username: "],
                    ["Password: "],
                    ["LAB_1830-PSS8# "],
                    [
                        "show general detail\r\n"
                        "Name                   : LAB_1830-PSS8\r\n"
                        "System Description     : Nokia 1830 PSS v22.12.0 SONET ADM\r\n"
                        "S/W Version            : 1830PSSECX-22.12-0\r\n"
                        "LAB_1830-PSS8# "
                    ],
                ])

            def close(inner):
                inner.closed = True

        def fake_connect_1830(self_id, ip, username, password, queue):
            captured["ssh_user"] = username
            captured["ssh_pass"] = password
            captured["ip"] = ip
            return FakeClient()

        with mock.patch.object(
                script_interface.DeviceIdentifier,
                "_ssh_connect_1830_cli",
                new=fake_connect_1830), \
             mock.patch("script_interface.AuthLockout.check"), \
             mock.patch("script_interface.AuthLockout.register_success"), \
             mock.patch("script_interface.get_known_hosts_path", return_value=Path(".")), \
             mock.patch("script_interface.get_host_key_policy", return_value=None):
            dt, dn = self.ident._ssh_identify_attempt(
                "10.0.0.1", "cli", "admin", Queue(),
                should_stop=lambda: False,
                sleep_with_abort=lambda s: False,
            )

        self.assertEqual(captured["ssh_user"], "cli")
        self.assertEqual(captured["ssh_pass"], "admin")
        self.assertEqual(captured["ip"], "10.0.0.1")
        self.assertEqual(dt, "1830")
        # device name should be the hostname extracted from the prompt
        # ("LAB_1830-PSS8" from "LAB_1830-PSS8# ") rather than the
        # placeholder "Nokia 1830".
        self.assertEqual(dn, "LAB_1830-PSS8")


    def test_two_stage_login_handles_post_login_yn_ack(self):
        """Some 1830 builds present a Y/n EULA/warning banner after auth.
        The helper must auto-answer 'y' and then settle to the shell."""
        sess = self._make_fake_session([
            ["Username: "],
            ["Password: "],
            ["WARNING: Authorized use only. Continue? (y/n): "],   # ack #1
            ["Accept license? [y/n]: "],                           # ack #2
            ["user@nokia# "],                                       # final shell
        ])
        ok = self.ident._do_1830_two_stage_login(
            sess, Queue(), shell_user="admin", shell_pass="admin"
        )
        self.assertTrue(ok)
        self.assertEqual(sess.sent, ["admin\n", "admin\n", "y\n", "y\n"])


# ---------------------------------------------------------------------------
# Credential failure rotation — must cycle through ALL defaults
# ---------------------------------------------------------------------------
class TestHandleCredentialFailureRotation(unittest.TestCase):
    """Each successive call to handle_credential_failure on the same IP must
    advance through the default seed list (admin/admin -> ADMIN/ADMIN ->
    cli/admin) instead of being permanently stuck on entry #2. Otherwise
    Nokia 1830s would never be auto-discovered."""

    def setUp(self):
        from script_interface import reset_auth_attempt
        reset_auth_attempt("203.0.113.42")

    def test_rotates_through_every_default_then_prompts(self):
        from script_interface import handle_credential_failure, CredentialPromptRequired
        ip = "203.0.113.42"
        q = Queue()

        # Stub default cred list with 3 entries. Rotation starts at index 0
        # because the primary credential may come from Credential Manager
        # (not necessarily defaults[0]) — so we can't assume it was tried.
        # All three entries are dispensed before CredentialPromptRequired
        # signals exhaustion.
        with mock.patch(
            "script_interface.get_default_credentials_to_try",
            return_value=[("admin", "admin"), ("ADMIN", "ADMIN"), ("cli", "admin")],
        ):
            r1 = handle_credential_failure(ip, q, tried_defaults=False)
            r2 = handle_credential_failure(ip, q, tried_defaults=False)
            r3 = handle_credential_failure(ip, q, tried_defaults=False)
            # Defaults exhausted — next call must raise so the GUI worker
            # can park the IP for a main-thread credential prompt. We
            # deliberately do NOT call prompt_for_credentials_gui from here;
            # that would deadlock Tk on Windows from a worker thread.
            with self.assertRaises(CredentialPromptRequired) as ctx:
                handle_credential_failure(ip, q, tried_defaults=False)
            # And a follow-up call must NOT raise again — it should return
            # (None, None) so the caller stops retrying this IP.
            r5 = handle_credential_failure(ip, q, tried_defaults=False)

        self.assertEqual(r1, ("admin", "admin"))
        self.assertEqual(r2, ("ADMIN", "ADMIN"))
        self.assertEqual(r3, ("cli", "admin"))
        self.assertEqual(ctx.exception.ip, ip)
        self.assertEqual(r5, (None, None))

    def test_reset_clears_state(self):
        from script_interface import handle_credential_failure, reset_auth_attempt
        ip = "203.0.113.43"
        q = Queue()
        with mock.patch(
            "script_interface.get_default_credentials_to_try",
            return_value=[("a", "a"), ("b", "b"), ("c", "c")],
        ):
            self.assertEqual(handle_credential_failure(ip, q), ("a", "a"))
            self.assertEqual(handle_credential_failure(ip, q), ("b", "b"))
            reset_auth_attempt(ip)
            # After reset, rotation rewinds to index 0.
            self.assertEqual(handle_credential_failure(ip, q), ("a", "a"))


# ---------------------------------------------------------------------------
# prompt_for_credentials_gui must not deadlock from worker threads
# ---------------------------------------------------------------------------
class TestCredentialPromptThreadSafety(unittest.TestCase):
    """Tk dialogs from non-main threads on Windows deadlock the entire
    process. The prompt helper must detect this and bail out cleanly."""

    def test_returns_none_when_called_from_worker_thread(self):
        from utils.credentials import prompt_for_credentials_gui
        import threading

        result_holder = {}

        def worker():
            # No parent_window provided -> would normally try to create a
            # new Tk root, which deadlocks. Must return None instead.
            result_holder["r"] = prompt_for_credentials_gui()

        t = threading.Thread(target=worker)
        t.start()
        # Bounded join — if the helper is broken and tries to spin up Tk,
        # the thread will hang forever.
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "Credential prompt deadlocked worker thread")
        self.assertIsNone(result_holder["r"])


if __name__ == "__main__":
    unittest.main()
