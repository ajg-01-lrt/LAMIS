"""Persistent SSH session to an RLS seed for running ``curl`` against
otherwise-unreachable neighbor nodes.

Why this exists: Ciena RLS sshd ships with ``AllowTcpForwarding no``,
so the obvious approach (``ssh -L``-style port forwarding through the
seed via paramiko's ``direct-tcpip`` channel) gets rejected with
``Administratively prohibited``. Workaround: open one SSH session to
the seed, drop to ``shell`` (bash), and run ``curl`` from there --
curl executes in the seed's network namespace and can reach the
neighbors that the workstation can't.

This module hides the prompt-driven mechanics so callers see a clean
``session.http_request(method, host, cmd, ...) -> ResponseLike``
API. Each call sends a curl command bracketed with unique markers so
the response body can be extracted from the shell stream reliably.

Usage::

    with SshSeedSession("10.0.0.1", "su", "Ciena123") as ssh:
        resp = ssh.http_request("POST", "10.6.22.132", "login",
                                data=payload, headers=hdrs)
        cookies = resp.cookies
        resp = ssh.http_request("GET", "10.6.22.132",
                                "restconf/data/...", cookies=cookies)

The class name ``SshJumpTunnel`` is kept as an alias for backwards
compatibility with earlier audit code that did port-forwarding.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import queue
import re
import shlex
import socket
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

import paramiko


_log = logging.getLogger(__name__)


# Bash prompt typical for an RLS seed dropped into shell from CLI:
#   bash(diaguser@uselp1-l8r2)[41p]rls>
# The closing ``> `` is what we anchor on; the rest of the line varies
# with user / hostname / window-size markers, so we match permissively.
_BASH_PROMPT_RE = re.compile(rb"bash\([^\)]+\)[^\n]*>\s*$")
# CLI banner prompt ends in ``#`` on the last line.
_CLI_PROMPT_RE = re.compile(rb"[\w.\-]+#\s*$")


class ResponseLike:
    """Minimal stand-in for ``requests.Response`` that the audit's
    helpers rely on. We carry the status code, response body, and a
    cookie dict parsed from any ``Set-Cookie`` headers."""

    def __init__(self, status_code: int, content: bytes,
                 cookies: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status_code
        self.content = content
        self.cookies: Dict[str, str] = cookies or {}

    def __repr__(self) -> str:
        return f"ResponseLike(status_code={self.status_code}, content={len(self.content)} bytes)"


class SshSeedSession:
    """One persistent SSH+bash session against an RLS seed. Threadsafe:
    every ``http_request`` call serializes on an internal lock so the
    shared shell stream stays coherent across audit phases that might
    fire from worker threads.

    Args:
        jump_host: IP / hostname of the seed.
        username, password: SSH credentials for the seed (typically
            ``su``/``Ciena123`` from the encrypted credential store).
        port: SSH port (defaults to 22).
        connect_timeout: TCP/SSH-handshake budget (seconds).
        prompt_timeout: How long to wait for an interactive prompt
            (CLI banner / bash prompt) when first entering the shell.
        command_timeout: Default time to wait for a curl invocation
            to complete. Per-call ``timeout=`` overrides this.
        host_key_policy: paramiko ``MissingHostKeyPolicy``. Defaults to
            ``AutoAddPolicy`` so fresh field devices don't block the
            audit on a first-run host-key prompt.
    """

    def __init__(
        self,
        jump_host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        connect_timeout: float = 10.0,
        prompt_timeout: float = 15.0,
        command_timeout: float = 30.0,
        host_key_policy: Optional[paramiko.MissingHostKeyPolicy] = None,
    ) -> None:
        self.jump_host = jump_host
        self.username = username
        self.password = password
        self.port = port
        self.connect_timeout = connect_timeout
        self.prompt_timeout = prompt_timeout
        self.command_timeout = command_timeout
        self._host_key_policy = host_key_policy or paramiko.AutoAddPolicy()
        self._client: Optional[paramiko.SSHClient] = None
        self._shell = None  # paramiko Channel from invoke_shell
        self._lock = threading.Lock()
        self._closed = False
        # Cookie-jar file on the seed. Each curl invocation reads from
        # and writes to this file so session cookies persist across
        # consecutive REST calls. Hand-building the ``Cookie:`` header
        # works for ASCII-only values but trips RLS on cookies whose
        # values embed ``::`` (the standard ``2::http.session::<id>``
        # token) -- letting curl manage the jar avoids any escape-
        # rules guessing. ``/tmp`` is wipeable, present on every RLS,
        # and writable by ``diaguser``. Filename combines monotonic_ns
        # with uuid4 so back-to-back constructor calls (e.g. in a
        # test) can't collide -- ``monotonic_ns`` clock resolution on
        # Windows is coarser than a single nanosecond.
        import uuid as _uuid
        self._cookie_jar = (
            f"/tmp/atlas_audit_{int(time.monotonic_ns())}_"
            f"{_uuid.uuid4().hex[:8]}.cookies"
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "SshSeedSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def open(self) -> None:
        """Connect, open an interactive shell, drop from the CLI to
        bash. Raises whatever paramiko raises on auth/connect failure;
        callers should catch and fall back to best-effort behavior."""
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(self._host_key_policy)
        self._client.connect(
            self.jump_host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=self.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError(
                f"paramiko returned no transport for {self.jump_host}"
            )
        try:
            transport.set_keepalive(30)
        except Exception:
            pass

        self._shell = self._client.invoke_shell(width=200, height=200)
        # settimeout governs per-recv waits; without it recv blocks
        # forever on a stalled channel.
        self._shell.settimeout(1.0)

        # 1) Drain the SSH banner + login messages until we see the CLI
        #    prompt ``hostname#`` -- proves the shell is responsive.
        self._drain_until(_CLI_PROMPT_RE, timeout=self.prompt_timeout)

        # 2) Drop from CLI to bash. The ``shell`` keyword on RLS
        #    invokes /bin/bash under the SSH session. On RLS the
        #    ``su`` user is restricted to a locked CLI and the
        #    ``shell`` keyword raises "Unknown keyword" -- only
        #    ``diaguser`` (or other unlocked accounts) can drop to
        #    bash. Surface that as an explicit auth/permission error
        #    instead of a bare timeout so the audit's fallback log
        #    line is actionable.
        self._shell.send(b"shell\n")
        try:
            self._drain_until(_BASH_PROMPT_RE, timeout=self.prompt_timeout)
        except TimeoutError as exc:
            tail = str(exc)[-400:]
            if b"Unknown keyword" in tail.encode("utf-8", "replace") or "Unknown keyword" in tail:
                raise PermissionError(
                    f"seed rejected the ``shell`` keyword for user "
                    f"{self.username!r} -- this user is restricted to "
                    "the CLI. Use ``diaguser`` (Ciena RLS RESTCONF /"
                    " shell account) instead."
                ) from exc
            raise

        # 3) Widen the PTY before any long command lands on the wire.
        #    The RLS bash prompt is ~38 chars; the default PTY width
        #    is 80; so anything above ~42 chars in the command body
        #    gets wrapped, and the terminal driver injects ``\r`` at
        #    the wrap point (field confirmed: long curl commands
        #    came back as ``sttycols`` -- no space -- in the buffer
        #    echo, mangling them before bash could parse).
        #
        #    Keep THIS command short enough to fit in the default 80
        #    cols: ``stty cols 32766 -onlcr`` plus prompt = ~73
        #    chars, safely under the wrap point. PS1/PROMPT_COMMAND
        #    overrides don't survive on this device (the prompt is
        #    regenerated after every command), so we don't try --
        #    subsequent calls use ``_send_and_capture`` with unique
        #    BEGIN/END markers and never need to detect a prompt.
        self._shell.send(b"stty cols 32766 -onlcr 2>/dev/null\n")
        self._drain_until(_BASH_PROMPT_RE, timeout=self.prompt_timeout)

    def close(self) -> None:
        """Idempotent teardown: cleanly exit bash, then the CLI, then
        the SSH connection."""
        if self._closed:
            return
        self._closed = True
        if self._shell is not None:
            try:
                # Best-effort: remove the cookie jar so /tmp doesn't
                # accumulate stale files across audit runs. We don't
                # wait for confirmation -- the exits below close the
                # session whether or not rm succeeded.
                self._shell.send(
                    f"rm -f {self._cookie_jar} 2>/dev/null\n".encode("utf-8")
                )
            except Exception:
                pass
            try:
                self._shell.send(b"exit\n")  # bash -> CLI
                self._shell.send(b"exit\n")  # CLI -> SSH disconnect
            except Exception:
                pass
            try:
                self._shell.close()
            except Exception:
                pass
            self._shell = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def is_open(self) -> bool:
        return (
            not self._closed
            and self._shell is not None
            and self._client is not None
        )

    # ------------------------------------------------------------------
    # Shell I/O
    # ------------------------------------------------------------------

    def _drain_until(self, pattern: re.Pattern, timeout: float) -> bytes:
        """Read from the shell until *pattern* appears anywhere in the
        accumulated buffer. Returns the full buffer including the
        match. Raises ``TimeoutError`` if the pattern never shows up
        within *timeout* seconds.

        We can't rely on ``recv`` returning ``b""`` for end-of-data
        (paramiko's Channel only does that on close), so we drive the
        loop with a deadline."""
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            try:
                chunk = self._shell.recv(8192)
            except socket.timeout:
                chunk = b""
            if chunk:
                buf.extend(chunk)
                if pattern.search(buf):
                    return bytes(buf)
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for prompt; got {len(buf)} bytes,"
                    f" tail={bytes(buf)[-200:]!r}"
                )

    def _send_and_capture(
        self, command: str, timeout: float
    ) -> Tuple[str, int]:
        """Send a single bash command, capture its output between unique
        markers, return ``(stdout, exit_code)``.

        Markers are echo'd before/after the command so we can find the
        edges of the response without prompt-detection heuristics. The
        end marker carries the exit code so callers can detect curl
        failures distinct from REST 4xx/5xx (which are still 200-level
        curl exits with the HTTP error in the body)."""
        token = f"ATLAS_{int(time.monotonic_ns())}"
        begin = f"--BEGIN_{token}--"
        end = f"--END_{token}--"
        # ``set +o history`` and bracketing echos. Use single-quoted
        # outer wrapper so the shell doesn't try to expand $? inside
        # before assigning -- though here we just rely on the standard
        # rule that $? is the LAST command's exit code, so the
        # ``echo "...$?..."`` after our command captures the right
        # status.
        wrapped = f"echo {begin}; {command}; echo {end}_$?\n"
        self._shell.send(wrapped.encode("utf-8"))

        # Wait for the END marker. Slightly larger budget than the
        # caller's timeout to leave room for the shell echo round-trip
        # and our marker decoration.
        end_re = re.compile(rf"{re.escape(end)}_(\d+)".encode("ascii"))
        buf = self._drain_until(end_re, timeout=timeout + 5)

        text = buf.decode("utf-8", errors="replace")
        # Extract between BEGIN and END markers. The PTY echoes our
        # command back BEFORE executing it, so the literal text
        # ``--BEGIN_<token>--`` appears at least twice in the buffer:
        # once inside ``echo --BEGIN_<token>--; ...`` (command echo)
        # and once as the actual stdout from running that echo. We
        # want the LAST occurrence -- everything before it is shell
        # bookkeeping; everything after is the real response body.
        # ``rfind`` is safe even if the response itself happens to
        # contain the token (vanishingly unlikely with a 19-digit
        # monotonic-ns suffix on the marker).
        b_pos = text.rfind(begin)
        if b_pos == -1:
            return ("", -1)
        # Skip past the begin marker and the newline that follows it.
        after = text[b_pos + len(begin):].lstrip("\r\n")
        m = re.search(rf"{re.escape(end)}_(\d+)", after)
        if not m:
            return ("", -1)
        body = after[: m.start()].rstrip("\r\n")
        try:
            rc = int(m.group(1))
        except ValueError:
            rc = -1
        return (body, rc)

    # ------------------------------------------------------------------
    # HTTP via curl
    # ------------------------------------------------------------------

    def http_request(
        self,
        method: str,
        host: str,
        cmd: str,
        *,
        data: Optional[Union[str, Mapping[str, Any]]] = None,
        headers: Optional[Mapping[str, str]] = None,
        cookies: Optional[Mapping[str, str]] = None,
        verify: bool = True,
        timeout: Optional[float] = None,
    ) -> ResponseLike:
        """Run a single HTTP request via ``curl`` on the seed and parse
        the response into a ``ResponseLike``. Threadsafe.

        Args:
            method: ``GET`` / ``POST`` etc.
            host: target host (IP or hostname); URL becomes
                ``https://<host>/<cmd>``.
            cmd: path component, e.g. ``restconf/data/...``.
            data: POST body. ``dict`` is url-form-encoded; ``str`` is
                sent as-is.
            headers: extra HTTP headers.
            cookies: cookie dict; serialized into a single ``Cookie:``
                header.
            verify: when False, passes ``-k`` to curl (Self-signed RLS
                certs are the norm so the audit's existing helpers
                default to ``verify=False``).
            timeout: per-call budget for curl; defaults to the
                session's ``command_timeout``.
        """
        if not self.is_open:
            raise RuntimeError(
                "SshSeedSession is closed -- call open() first."
            )
        eff_timeout = timeout if timeout is not None else self.command_timeout

        # Build the curl command list, then shell-quote each piece.
        # ``-k`` is unconditional: RLS nodes ship self-signed certs,
        # and the audit's first attempt (``verify=True``) would
        # otherwise return curl exit 60 (CURLE_PEER_FAILED_VERIFICATION)
        # without raising, defeating the caller's verify=False
        # fallback path. The audit doesn't gain anything from cert
        # validation on a closed internal management network.
        #
        # ``-c``/``-b`` let curl manage the session cookie jar between
        # invocations. Hand-rolling a ``Cookie:`` header from a parsed
        # dict was rejected by the RLS REST API on neighbors (login
        # returned 200 + Set-Cookie, every subsequent GET returned
        # 401), even though the cookie value reproduced bit-for-bit
        # what the server set. Letting curl read+write the jar
        # delegates any escape-rules guessing to the tool that knows
        # the format. The ``cookies=`` kwarg is still accepted for
        # API compatibility but is added as an explicit
        # ``--cookie name=value`` so curl merges it into the jar
        # natively (this catches any cookies the caller wants to
        # inject that didn't come from a previous Set-Cookie).
        curl_argv: list = [
            "curl",
            "-s",         # silent (no progress bar)
            "-i",         # include response headers in output
            "-k",         # always skip cert verification
            "-c", self._cookie_jar,   # save Set-Cookie responses
            "-b", self._cookie_jar,   # send matching cookies on the request
            "--max-time", str(int(eff_timeout)),
            "-X", method.upper(),
        ]

        if data is not None:
            if isinstance(data, Mapping):
                body = urllib.parse.urlencode(data)
            else:
                body = str(data)
            curl_argv += ["-d", body]

        if headers:
            for k, v in headers.items():
                curl_argv += ["-H", f"{k}: {v}"]

        if cookies:
            # Inject any caller-supplied cookies into the jar via
            # curl's ``--cookie name=value`` style (which curl parses
            # into its cookie engine rather than blindly emitting the
            # header text). Sent in addition to the jar's contents so
            # explicit overrides win when names collide.
            for k, v in cookies.items():
                curl_argv += ["--cookie", f"{k}={v}"]

        url = f"https://{host}/{cmd}"
        curl_argv.append(url)

        # ``NO_COLOR=1`` tells curl (>= 7.67) not to ANSI-bold header
        # names when stdout is a TTY -- which it is, since we're
        # invoking from an interactive shell. Without this, the
        # response would arrive as ``\x1b[1mHTTP/1.1 200 OK\x1b[0m``
        # and the parser's ``startswith("HTTP/")`` check fails, so
        # status_code defaults to 0 and the audit thinks every
        # neighbor returned an error.
        shell_cmd = "NO_COLOR=1 " + " ".join(shlex.quote(a) for a in curl_argv)

        with self._lock:
            try:
                raw_body, rc = self._send_and_capture(
                    shell_cmd, timeout=eff_timeout
                )
            except TimeoutError as exc:
                _log.warning(f"[SSH-CURL] {host} -- shell timeout: {exc}")
                return ResponseLike(0, b"", {})

        if rc != 0:
            # curl reports its own exit code: 6=DNS, 7=connect refused,
            # 28=timeout, etc. Pass that to the caller via status_code
            # 0 (matching the existing audit's behavior for
            # ConnectionError/Timeout) -- the body, if any, is whatever
            # curl printed to stderr before failing.
            _log.warning(
                f"[SSH-CURL] {method} {url} curl exit={rc}"
                f" (no HTTP response received)"
            )
            return ResponseLike(0, raw_body.encode("utf-8", "replace"), {})

        return _parse_curl_response(raw_body)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _parse_curl_response(raw: str) -> ResponseLike:
    """Parse ``curl -i`` output into a :class:`ResponseLike`.

    The shape is::

        HTTP/1.1 200 OK\r\n
        Header-1: value\r\n
        Header-2: value\r\n
        \r\n
        body bytes

    Real-world wrinkles we have to handle:

    * **ANSI bolding**: curl >=7.67 ANSI-bolds header names by default
      when stdout is a TTY (which it is over SSH/invoke_shell). Strip
      the escape codes BEFORE checking ``startswith("HTTP/")`` or the
      status line never matches.
    * **PTY-doubled ``\\n``**: the RLS bash + paramiko interaction
      emits every ``\\r\\n`` as ``\\n\\n`` in our captured buffer
      (field-confirmed: a real ``Header1\\r\\nHeader2\\r\\nHeader3``
      arrives as ``Header1\\n\\nHeader2\\n\\nHeader3\\n\\n``). If we
      naively split on ``\\n\\n`` for the headers/body boundary we'd
      cut between every pair of headers instead. Halve consecutive
      ``\\n`` runs to restore the canonical HTTP shape.
    * **100 Continue**: walk past any interim status lines to reach
      the real response.
    """
    # 1) Strip ANSI escape sequences. Curl colorizes header names with
    #    ``\x1b[1m...\x1b[0m`` when it thinks stdout is interactive,
    #    which breaks the ``startswith("HTTP/")`` check on the status
    #    line. ``NO_COLOR=1`` in the env should prevent this at the
    #    source, but strip here as belt-and-braces in case the env
    #    var doesn't propagate through the PTY.
    norm = _ANSI_ESCAPE_RE.sub("", raw)
    # 2) Normalize CRLF / standalone CR to LF.
    norm = norm.replace("\r\n", "\n").replace("\r", "\n")
    # 3) Collapse PTY-doubled newlines IF AND ONLY IF the stream is
    #    actually doubled. Detection: a canonical curl ``-i`` stream
    #    has AT MOST two consecutive ``\\n`` (the end-of-headers
    #    marker). A doubled stream has at least four (the doubled
    #    end-of-headers ``\\n\\n`` -> ``\\n\\n\\n\\n``). Halving an
    #    already-canonical stream would collapse the end-of-headers
    #    boundary and the parser would put everything into ``head``,
    #    so we have to check first.
    if re.search(r"\n{4}", norm):
        norm = re.sub(
            r"\n+", lambda m: "\n" * max(1, len(m.group(0)) // 2), norm,
        )

    # Split headers from body at the first blank line.
    parts = norm.split("\n\n", 1)
    if len(parts) == 1:
        head, body = parts[0], ""
    else:
        head, body = parts
    while head.startswith("HTTP/") and "100 Continue" in head.split("\n", 1)[0]:
        # Skip the 100-Continue header block and take what's next.
        parts = body.split("\n\n", 1)
        if len(parts) == 1:
            head, body = parts[0], ""
        else:
            head, body = parts

    head_lines = head.split("\n")
    # Find the HTTP status line wherever it appears. ``_send_and_capture``
    # tries to strip the shell command echo, but a half-stripped echo can
    # leave a stray ``echo --BEGIN_...`` or ``; curl ...`` line above
    # the real HTTP response. Pin status-line detection to the actual
    # ``HTTP/`` prefix rather than assuming ``head_lines[0]``.
    status_code = 0
    status_idx = -1
    for i, line in enumerate(head_lines):
        if line.startswith("HTTP/"):
            status_idx = i
            try:
                status_code = int(line.split(" ", 2)[1])
            except (IndexError, ValueError):
                status_code = 0
            break

    # Parse headers from the lines AFTER the status line (skip any
    # pre-HTTP noise that survived the BEGIN/END marker extraction).
    header_lines = head_lines[status_idx + 1:] if status_idx >= 0 else head_lines
    cookies: Dict[str, str] = {}
    for line in header_lines:
        if line.lower().startswith("set-cookie:"):
            # ``Set-Cookie: name=value; Path=/; HttpOnly``
            kv = line.split(":", 1)[1].strip()
            name_eq_value = kv.split(";", 1)[0]
            if "=" in name_eq_value:
                k, v = name_eq_value.split("=", 1)
                cookies[k.strip()] = v.strip()

    return ResponseLike(status_code, body.encode("utf-8", "replace"), cookies)


# Backward-compatible alias for callers that imported the previous
# port-forwarding name. ``SshJumpTunnel`` no longer forwards TCP
# (Ciena RLS sshd ships with AllowTcpForwarding=no, so direct-tcpip
# channels are administratively prohibited) -- it now runs curl over
# a persistent invoke_shell session instead.
SshJumpTunnel = SshSeedSession


class SshSeedSessionPool:
    """A fixed-size pool of :class:`SshSeedSession` shells to a single
    seed, so neighbor ``curl`` calls can run concurrently instead of
    serializing through one shell.

    Each pooled session is an independent SSH connection with its own
    ``invoke_shell`` and its own cookie jar, so leasing a session for the
    full duration of a node's REST sequence (login + GETs) keeps that
    node's cookies isolated from other workers. Ciena RLS sshd allows a
    handful of concurrent sessions; keep ``size`` modest (3-5).

    Lifecycle mirrors :class:`SshSeedSession`: construct, ``open()`` (opens
    all sessions in parallel; survivors are kept), lease sessions via the
    :meth:`lease` context manager, then ``close()``. ``open`` raises only
    if *no* session could be established -- a partial pool is usable and
    logs the shortfall so the caller can proceed best-effort.
    """

    def __init__(
        self,
        jump_host: str,
        username: str,
        password: str,
        *,
        size: int = 3,
        **session_kwargs: Any,
    ) -> None:
        self._params = (jump_host, username, password)
        self._session_kwargs = session_kwargs
        self.size = max(1, int(size))
        self._all: List[SshSeedSession] = []
        self._free: "queue.Queue[SshSeedSession]" = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False

    def open(self) -> "SshSeedSessionPool":
        """Open up to ``size`` sessions in parallel. Keeps whichever
        succeed; raises ``RuntimeError`` only if none do."""
        def _make() -> SshSeedSession:
            sess = SshSeedSession(*self._params, **self._session_kwargs)
            sess.open()
            return sess

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.size
        ) as pool:
            futures = [pool.submit(_make) for _ in range(self.size)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    sess = fut.result()
                except Exception as exc:  # noqa: BLE001 -- best-effort
                    _log.warning(
                        "[SSH-POOL] a session failed to open: %s", exc
                    )
                    continue
                self._all.append(sess)
                self._free.put(sess)

        if not self._all:
            raise RuntimeError(
                "SshSeedSessionPool: no sessions could be opened to "
                f"{self._params[0]}"
            )
        if len(self._all) < self.size:
            _log.warning(
                "[SSH-POOL] opened %d/%d sessions to %s (proceeding with "
                "fewer)", len(self._all), self.size, self._params[0]
            )
        return self

    @property
    def opened(self) -> int:
        """Number of sessions actually established."""
        return len(self._all)

    @property
    def is_open(self) -> bool:
        return not self._closed and bool(self._all)

    @contextlib.contextmanager
    def lease(self, timeout: Optional[float] = None):
        """Check out a session for the caller's exclusive use, returning
        it to the pool on exit. Blocks (up to *timeout*) when every
        session is busy."""
        sess = self._free.get(timeout=timeout)
        try:
            yield sess
        finally:
            self._free.put(sess)

    def close(self) -> None:
        """Idempotent teardown: close every pooled session."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for sess in self._all:
            try:
                sess.close()
            except Exception:  # noqa: BLE001 -- best-effort teardown
                pass
