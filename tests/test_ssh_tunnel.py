"""Pin the ``utils.ssh_tunnel.SshSeedSession`` shape and the RLS
audit's request-dispatch logic.

We don't spin up a real SSH server in unit tests -- paramiko gets
mocked; what we care about is that ``_audit_request`` routes
correctly (requests for the seed, ``http_request`` on the SSH session
for everything else) and that the lifecycle is clean.
"""
from __future__ import annotations
import inspect
import unittest
from unittest import mock


class TestSshSeedSessionShape(unittest.TestCase):
    """The audit relies on a specific small surface of
    ``SshSeedSession``. If any of these names go away, the audit
    breaks silently -- so pin them."""

    def test_class_exists_and_is_context_manager(self):
        from utils.ssh_tunnel import SshSeedSession
        self.assertTrue(hasattr(SshSeedSession, "__enter__"))
        self.assertTrue(hasattr(SshSeedSession, "__exit__"))

    def test_required_methods_exist(self):
        from utils.ssh_tunnel import SshSeedSession
        for name in ("open", "close", "http_request"):
            self.assertTrue(
                hasattr(SshSeedSession, name),
                f"SshSeedSession missing required method: {name}",
            )

    def test_constructor_signature(self):
        from utils.ssh_tunnel import SshSeedSession
        sig = inspect.signature(SshSeedSession.__init__)
        # The audit depends on this exact positional ordering when it
        # instantiates the session.
        params = list(sig.parameters.values())
        self.assertEqual(params[1].name, "jump_host")
        self.assertEqual(params[2].name, "username")
        self.assertEqual(params[3].name, "password")

    def test_is_open_starts_false(self):
        from utils.ssh_tunnel import SshSeedSession
        s = SshSeedSession("10.0.0.1", "su", "Ciena123")
        self.assertFalse(s.is_open)

    def test_close_is_idempotent(self):
        from utils.ssh_tunnel import SshSeedSession
        s = SshSeedSession("10.0.0.1", "su", "Ciena123")
        s.close()
        s.close()  # second call must not raise

    def test_http_request_raises_when_not_open(self):
        from utils.ssh_tunnel import SshSeedSession
        s = SshSeedSession("10.0.0.1", "su", "Ciena123")
        with self.assertRaises(RuntimeError):
            s.http_request("GET", "10.6.22.132", "restconf/data/x")

    def test_backwards_compat_alias_exists(self):
        # Pre-refactor code imported the class as ``SshJumpTunnel``.
        # Keep the alias so any caller that grabbed the old name
        # (other ATLAS scripts, tests, snippets) doesn't break.
        from utils.ssh_tunnel import SshJumpTunnel, SshSeedSession
        self.assertIs(SshJumpTunnel, SshSeedSession)


class TestSshCurlAlwaysSkipsCertVerify(unittest.TestCase):
    """The audit's first ``login_to_node`` attempt asks for
    ``verify=True``; without ``-k``, curl returns exit 60
    (CURLE_PEER_FAILED_VERIFICATION) on every RLS device (self-signed
    certs are universal on the internal management net) and the
    ``ResponseLike`` failure path swallows the failure into
    ``status_code=0``, defeating the audit's verify=False fallback.

    The fix: ``http_request`` always passes ``-k``, regardless of the
    caller's ``verify=`` flag. Pin that here at the source level.
    """

    def test_http_request_source_contains_unconditional_dash_k(self):
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession.http_request)
        # The unconditional ``-k`` must be in the curl arg list, NOT
        # inside an ``if not verify:`` branch.
        self.assertIn('"-k"', src)
        # The old "verify gate" branch must be gone.
        self.assertNotIn('if not verify:', src)

    def test_http_request_prefixes_no_color_env(self):
        # curl auto-colorizes header output when stdout is a TTY,
        # which the SSH invoke_shell PTY is. ``NO_COLOR=1`` prevents
        # the ``\\x1b[1m`` wrapping on every header name (which would
        # break the parser's HTTP/ status-line check).
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession.http_request)
        self.assertIn("NO_COLOR=1", src)

    def test_http_request_uses_curl_cookie_jar(self):
        # Hand-built ``Cookie:`` headers were rejected by RLS REST on
        # neighbors (login 200 -> every following GET 401). curl's
        # native ``-c``/``-b`` cookie jar handles the format
        # correctly. Pin both flags here so a refactor can't silently
        # revert to manual header construction.
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession.http_request)
        self.assertIn('"-c"', src, "expected curl -c <jar> for writing")
        self.assertIn('"-b"', src, "expected curl -b <jar> for reading")
        # The jar path lives on the session, not hardcoded inline.
        self.assertIn("self._cookie_jar", src)
        # The old hand-built ``Cookie:`` header path must be gone --
        # ``--cookie name=value`` is fine (curl parses it natively)
        # but a literal ``f"Cookie: {cookie_header}"`` header is the
        # bug we removed.
        self.assertNotIn('f"Cookie:', src)

    def test_session_has_unique_cookie_jar_path(self):
        # The jar path is per-session so two concurrent audits don't
        # clobber each other's session state. Pin the /tmp prefix
        # (any RLS has /tmp; diaguser has write access).
        from utils import ssh_tunnel
        s = ssh_tunnel.SshSeedSession("10.0.0.1", "u", "p")
        self.assertTrue(s._cookie_jar.startswith("/tmp/atlas_audit_"))
        s2 = ssh_tunnel.SshSeedSession("10.0.0.1", "u", "p")
        self.assertNotEqual(s._cookie_jar, s2._cookie_jar)


class TestSendAndCaptureSkipsCommandEcho(unittest.TestCase):
    """The PTY echoes our send'd command back BEFORE running it, so
    the literal text ``--BEGIN_<token>--`` appears twice in the
    buffer: once in the command echo, once in the actual output.
    ``_send_and_capture`` must use the LAST occurrence (the real one)
    -- otherwise it returns the contaminated tail that starts with
    the command echo, and the parser fails to find the status line.
    """

    def test_send_and_capture_uses_rfind_for_begin_marker(self):
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession._send_and_capture)
        # ``rfind`` returns the LAST occurrence; ``find`` returns the
        # first. The first BEGIN is always the command echo, so we
        # MUST use rfind.
        self.assertIn(
            "text.rfind(begin)", src,
            "_send_and_capture must use rfind to skip the command-echo"
            " BEGIN marker and find the real one from running ``echo``"
            " on the seed.",
        )


class TestSshSessionNormalizesTerminal(unittest.TestCase):
    """Field log showed the seed's prompt ``bash(diaguser@host)[41p]``
    injects ``\\r`` chars at column ~80 into the command echo, which
    mangles long curl invocations (``-H 'Accept:...'`` became
    ``-H\\rH 'Accept:...'``). Mitigation: widen the PTY via
    ``stty cols 32766`` IMMEDIATELY after dropping into bash, before
    any long command runs. The normalization command itself must be
    short enough to fit in the default 80-col PTY -- otherwise it
    wraps in the act of de-wrapping (field-confirmed: a longer
    ``export PS1=...; stty cols ...`` command came back with
    ``sttycols`` (no space) because the PTY wrap deleted the space
    between ``stty`` and ``cols``)."""

    def test_open_runs_short_stty_widen_command(self):
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession.open)
        # Must call stty cols. Specific value isn't pinned (a future
        # bump to 65535 is fine) -- the contract is just "widen".
        self.assertIn("stty cols", src)

    def test_open_does_not_send_long_combined_normalization(self):
        # Regression guard: the previous attempt combined PS1 export +
        # stty + TERM + onlcr into ONE line that itself wrapped at
        # col ~80 and mangled ``stty cols`` into ``sttycols``.
        # PS1 changes don't survive RLS's PROMPT_COMMAND anyway.
        from utils import ssh_tunnel
        src = inspect.getsource(ssh_tunnel.SshSeedSession.open)
        # Find the stty send line and make sure it isn't carrying
        # PS1 or TERM or other long baggage that would push it over
        # the 80-col wrap point.
        lines = [
            ln for ln in src.splitlines()
            if "stty cols" in ln and "self._shell.send" in ln
        ]
        self.assertTrue(lines, "no stty cols send found in open()")
        for ln in lines:
            self.assertNotIn(
                "PS1=", ln,
                f"stty cols command must be sent alone -- combining"
                f" with PS1= would wrap-mangle itself:\n  {ln}",
            )
            self.assertNotIn(
                "TERM=", ln,
                f"stty cols command must be sent alone -- combining"
                f" with TERM= would wrap-mangle itself:\n  {ln}",
            )


class TestCurlResponseParsing(unittest.TestCase):
    """``_parse_curl_response`` turns curl -i output into a
    ResponseLike. Pin the common cases."""

    def setUp(self):
        from utils.ssh_tunnel import _parse_curl_response
        self.parse = _parse_curl_response

    def test_parses_200_ok_with_body(self):
        raw = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
            "{\"hello\": \"world\"}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"{\"hello\": \"world\"}")

    def test_parses_set_cookie_header(self):
        raw = (
            "HTTP/1.1 200 OK\r\n"
            "Set-Cookie: JSESSIONID=abc123; Path=/; HttpOnly\r\n"
            "\r\n"
            ""
        )
        r = self.parse(raw)
        self.assertEqual(r.cookies, {"JSESSIONID": "abc123"})

    def test_parses_404(self):
        raw = "HTTP/1.1 404 Not Found\r\n\r\nnope"
        r = self.parse(raw)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.content, b"nope")

    def test_strips_ansi_bolding_from_status_line(self):
        # curl >=7.67 colorizes headers in interactive shells. The
        # status line arrives wrapped in ANSI bold, which breaks
        # startswith("HTTP/"). Pin the strip.
        raw = (
            "\x1b[1mHTTP/1.1 200 OK\x1b[0m\r\n"
            "\x1b[1mContent-Type\x1b[0m: application/json\r\n"
            "\r\n"
            "{}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"{}")

    def test_handles_pty_doubled_newlines(self):
        # Field-confirmed: the RLS PTY emits every ``\\n`` from curl
        # doubled, so a real ``Header1\\nHeader2\\n\\nbody`` arrives
        # as ``Header1\\n\\nHeader2\\n\\n\\n\\nbody``. The parser
        # must halve consecutive runs of ``\\n`` to recover the
        # canonical HTTP shape before splitting headers from body.
        raw = (
            "HTTP/1.1 200 OK\n\n"
            "Content-Type: application/json\n\n"
            "Set-Cookie: SESSION=abc123\n\n"
            "\n\n"  # end-of-headers (was \r\n\r\n, doubled to \n\n\n\n)
            "{\"ok\": true}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.cookies, {"SESSION": "abc123"})
        self.assertEqual(r.content, b"{\"ok\": true}")

    def test_finds_http_status_line_with_preceding_noise(self):
        # If a stray command-echo line survives marker extraction
        # (e.g. ``; curl -s -i -k ...; echo --END_xxx--_$?`` appears
        # before the real HTTP response), the parser must still find
        # the ``HTTP/`` status line further down rather than failing
        # the ``startswith("HTTP/")`` check on the first head line.
        raw = (
            "; curl -s -i -k --max-time 30 -X POST -d 'username=x' "
            "https://10.6.22.132/login; echo --END_xxx--_$?\n"
            "--BEGIN_xxx--\n"
            "HTTP/1.1 200 OK\r\n"
            "Set-Cookie: SESSION=abc\r\n"
            "\r\n"
            "{}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.cookies, {"SESSION": "abc"})

    def test_handles_combined_ansi_and_doubled_newlines(self):
        # Real-world worst case: both pathologies at once. This is
        # the exact shape we observed in the field log.
        raw = (
            "\x1b[1mHTTP/1.1 200 OK\x1b[0m\n\n"
            "\x1b[1mSet-Cookie\x1b[0m: -http-session-=1::http.session::abc; path=/\n\n"
            "\x1b[1mContent-Type\x1b[0m: application/json\n\n"
            "\n\n"
            "{}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.cookies, {"-http-session-": "1::http.session::abc"})

    def test_tolerates_lf_only_line_endings(self):
        # Interactive shells sometimes normalize \r\n -> \n.
        raw = (
            "HTTP/1.1 200 OK\n"
            "Content-Type: application/json\n"
            "\n"
            "{}"
        )
        r = self.parse(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"{}")


class TestAuditRequestDispatch(unittest.TestCase):
    """``_audit_request`` routes the seed via ``requests`` and every
    other host via the SSH session. Pin both branches."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.mod = RLS_Audit
        self._save = (
            self.mod._jump_tunnel,
            self.mod._seed_direct_host,
            self.mod._tunnel_open_attempted,
            self.mod.session,
        )
        self.mod._jump_tunnel = None
        self.mod._seed_direct_host = "10.0.0.1"
        self.mod._tunnel_open_attempted = True  # block lazy-open
        # Real requests.Session so cookie mirroring doesn't crash.
        import requests
        self.mod.session = requests.Session()

    def tearDown(self):
        (
            self.mod._jump_tunnel,
            self.mod._seed_direct_host,
            self.mod._tunnel_open_attempted,
            self.mod.session,
        ) = self._save

    def test_seed_uses_requests_session(self):
        # Mock requests.get so we can spy without a network call.
        with mock.patch("requests.get") as fake_get:
            fake_get.return_value = mock.Mock(status_code=200, content=b"ok")
            r = self.mod._audit_request(
                "GET", "10.0.0.1", "restconf/data/x",
                verify=False, timeout=5,
            )
            fake_get.assert_called_once()
            (url,) = fake_get.call_args.args
            self.assertEqual(url, "https://10.0.0.1/restconf/data/x")
            self.assertEqual(r.status_code, 200)

    def test_neighbor_uses_ssh_session_when_open(self):
        from utils.ssh_tunnel import ResponseLike
        fake = mock.Mock()
        fake.is_open = True
        fake.http_request.return_value = ResponseLike(200, b'{}', {})
        self.mod._jump_tunnel = fake
        r = self.mod._audit_request(
            "GET", "10.6.22.132", "restconf/data/x",
            verify=False, timeout=5,
        )
        fake.http_request.assert_called_once_with(
            "GET", "10.6.22.132", "restconf/data/x",
            data=None, headers=None, cookies=None,
            verify=False, timeout=5,
        )
        self.assertEqual(r.status_code, 200)

    def test_neighbor_falls_through_to_requests_when_ssh_fails(self):
        fake = mock.Mock()
        fake.is_open = True
        fake.http_request.side_effect = RuntimeError("ssh boom")
        self.mod._jump_tunnel = fake
        with mock.patch("requests.get") as fake_get:
            fake_get.return_value = mock.Mock(status_code=0, content=b"")
            # Must NOT raise; must fall through to direct request.
            r = self.mod._audit_request(
                "GET", "10.6.22.132", "restconf/data/x",
                verify=False, timeout=5,
            )
            fake_get.assert_called_once()


class TestAuditOpensSshLazily(unittest.TestCase):
    """The SSH session should only open when there's a non-seed host
    to reach -- a single-node audit shouldn't pay the SSH-handshake
    cost."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.mod = RLS_Audit
        self._save = (
            self.mod._jump_tunnel,
            self.mod._seed_direct_host,
            self.mod._tunnel_open_attempted,
            self.mod._tunnel_ssh_creds,
        )
        self.mod._jump_tunnel = None
        self.mod._seed_direct_host = "10.0.0.1"
        self.mod._tunnel_open_attempted = False
        self.mod._tunnel_ssh_creds = ("su", "Ciena123")

    def tearDown(self):
        (
            self.mod._jump_tunnel,
            self.mod._seed_direct_host,
            self.mod._tunnel_open_attempted,
            self.mod._tunnel_ssh_creds,
        ) = self._save

    def test_seed_only_does_not_attempt_open(self):
        with mock.patch.object(self.mod, "_try_open_jump_tunnel") as fake:
            with mock.patch("requests.get") as fake_get:
                fake_get.return_value = mock.Mock(status_code=200, content=b"")
                self.mod._audit_request("GET", "10.0.0.1", "x")
                fake.assert_not_called()

    def test_first_neighbor_call_attempts_open(self):
        with mock.patch.object(self.mod, "_try_open_jump_tunnel") as fake:
            with mock.patch("requests.get") as fake_get:
                fake_get.return_value = mock.Mock(status_code=0, content=b"")
                self.mod._audit_request("GET", "10.6.22.132", "x")
                fake.assert_called_once()


class TestAuditRunSetsTunnelSshCreds(unittest.TestCase):
    """``run_audit`` must prime ``_tunnel_ssh_creds`` with the RLS
    ``diaguser``/``Ciena123`` login. The ``su`` user has a restricted
    CLI that rejects the ``shell`` keyword (field-confirmed -- seed
    returns "Unknown keyword: shell"), so diaguser is the only user
    that can drop to bash and run ``curl``."""

    def test_run_audit_initializes_tunnel_ssh_creds(self):
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        self.assertIn("_tunnel_ssh_creds", src)
        # SSH login MUST use ``ciena-rls-rest`` (diaguser) -- ``ciena``
        # would resolve to ``su`` which is locked out of ``shell``.
        self.assertIn(
            'get_default_credential_for_vendor("ciena-rls-rest")', src,
            "tunnel SSH creds must come from the 'ciena-rls-rest'"
            " (diaguser) vendor key -- ``su`` cannot run the ``shell``"
            " keyword on RLS",
        )
        self.assertIn(
            '"diaguser"', src,
            "hardcoded SSH fallback must be diaguser, not su",
        )


class TestCookieMirroring(unittest.TestCase):
    """When a request lands through the SSH-curl path, its
    ResponseLike carries cookies as a dict. The audit's call sites
    read cookies from ``session.cookies.get_dict()`` after every
    login, so we mirror across so the contract still holds."""

    def test_dict_cookies_mirror_into_session(self):
        from scripts.Network import RLS_Audit
        from utils.ssh_tunnel import ResponseLike
        import requests
        orig = RLS_Audit.session
        try:
            RLS_Audit.session = requests.Session()
            resp = ResponseLike(200, b"", {"JSESSIONID": "abc"})
            RLS_Audit._mirror_cookies_to_session(resp)
            self.assertEqual(
                RLS_Audit.session.cookies.get_dict(),
                {"JSESSIONID": "abc"},
            )
        finally:
            RLS_Audit.session = orig

    def test_mirroring_clears_stale_cookies(self):
        # Real-world bite: RLS reuses the cookie name ``-http-session-``
        # on every node. After seed login, session.cookies has the
        # seed's cookie (domain=10.0.0.1). After neighbor login via
        # SSH-curl, mirror_cookies adds the neighbor's cookie (no
        # domain). ``get_dict`` returns whichever the jar iterates
        # last -- often the seed's -- and subsequent GETs to the
        # neighbor present the wrong session cookie and get
        # rejected. Mirror MUST clear the jar before planting the
        # new dict so ``get_dict()`` returns ONLY the current host.
        from scripts.Network import RLS_Audit
        from utils.ssh_tunnel import ResponseLike
        import requests
        orig = RLS_Audit.session
        try:
            RLS_Audit.session = requests.Session()
            # Simulate a prior seed login leaving its cookie in the
            # jar (set via the requests path).
            RLS_Audit.session.cookies.set(
                "-http-session-", "seed_value", domain="10.0.0.1",
            )
            self.assertIn(
                "-http-session-",
                RLS_Audit.session.cookies.get_dict(),
            )
            # Now neighbor login arrives via SSH-curl with its own
            # cookie. Mirror it.
            resp = ResponseLike(
                200, b"", {"-http-session-": "neighbor_value"},
            )
            RLS_Audit._mirror_cookies_to_session(resp)
            # session.cookies should now contain ONLY the neighbor's
            # cookie, not the stale seed one.
            d = RLS_Audit.session.cookies.get_dict()
            self.assertEqual(d, {"-http-session-": "neighbor_value"})
        finally:
            RLS_Audit.session = orig

    def test_real_response_cookies_are_not_overwritten(self):
        # When the seed path returns a real requests.Response, its
        # cookies are a CookieJar (not a dict). The mirror is a no-op
        # because requests already populated session.cookies itself.
        from scripts.Network import RLS_Audit
        import requests
        orig = RLS_Audit.session
        try:
            RLS_Audit.session = requests.Session()
            fake = mock.Mock()
            fake.cookies = requests.cookies.RequestsCookieJar()
            # Should not raise; should not add anything either.
            RLS_Audit._mirror_cookies_to_session(fake)
            self.assertEqual(RLS_Audit.session.cookies.get_dict(), {})
        finally:
            RLS_Audit.session = orig


if __name__ == "__main__":
    unittest.main()
