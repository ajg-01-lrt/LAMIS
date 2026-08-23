"""Serial-console helpers for Nokia SAR/IXR (and similar) devices.

These exist because the per-script `capture_full_output_serial` loops
were polling `in_waiting` and breaking on the first momentary gap,
which truncated multi-screen `show` output, and because no script was
authenticating against the serial console — commands were blasted at a
`Login:` prompt and silently ignored.

Diagnostics
-----------
Every public helper logs through the ``atlas.serial`` logger so its
level can be raised independently of the rest of ATLAS. Set
``config.SERIAL_DEBUG = True`` (or call :func:`enable_serial_debug`) to
turn on a per-chunk byte transcript without touching the global log
level. Probe-failure byte dumps are always logged at INFO so the
operator can tell wrong-baud garbage from a prompt-shape we don't
recognize without flipping anything.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger("atlas.serial")


def enable_serial_debug(enabled: bool = True) -> None:
    """Raise the serial helpers' logger to DEBUG (or back to default).

    DEBUG turns on a per-chunk byte transcript inside :func:`_read_until`
    plus state-transition logs in :func:`serial_login` and
    :func:`capture_until_prompt`. Without this the helpers stay at the
    root logger's level so they don't drown out other lines.
    """
    logger.setLevel(logging.DEBUG if enabled else logging.NOTSET)


def _format_byte_dump(data: bytes, max_bytes: int = 200) -> str:
    """Render *data* as a single human-readable line.

    Format: ``len=N hex=AA BB CC… repr='...'``. Hex is the literal
    bytes (always safe to log); ``repr`` is the Python repr of the
    decoded UTF-8 (errors=replace) so printable banner text is
    instantly readable. Both sides are capped at *max_bytes* so we
    don't blow up the log on a captured multi-screen dump.
    """
    if not data:
        return "len=0 hex=<empty>"
    head = bytes(data[:max_bytes])
    hex_part = " ".join(f"{b:02X}" for b in head)
    repr_part = head.decode("utf-8", errors="replace")
    ellipsis = "…" if len(data) > max_bytes else ""
    return (
        f"len={len(data)} hex={hex_part}{ellipsis} "
        f"repr={repr_part!r}{ellipsis}"
    )

# Match the common operational prompts at end-of-buffer:
#   Nokia SROS:    "A:hostname#"   "*A:hostname#"  "B:hostname>"
#   Generic:       "hostname#"     "hostname>"     "hostname$"
#   Waveserver-5:  "Waveserver-5*#" / "WS5_1*#" — the trailing ``*`` flags
#                  pending-but-unsaved config changes; we tolerate it so
#                  the prompt detector doesn't stall mid-provisioning.
# Also matches Login:/Password: prompts so callers can drive the login.
_PROMPT_RE = re.compile(
    rb"(?:[\*]?[ABab]:[A-Za-z0-9_\-.]+[#>]\s*$)"
    rb"|(?:[A-Za-z0-9_\-.]+\*?\s*[#>$]\s*$)"
    rb"|(?:[Ll]ogin:\s*$)"
    rb"|(?:[Uu]sername:\s*$)"
    rb"|(?:[Pp]assword:\s*$)"
)

_LOGIN_RE = re.compile(rb"(?:[Ll]ogin|[Uu]sername):\s*$")
# Getty "login:" vs the inner "Username:" must be answered differently for the
# Nokia 1830 family (login: -> cli, Username: -> admin), so match them
# separately. Both end-anchored so the banner's "Last Login:" line never matches.
_GETTY_LOGIN_RE = re.compile(rb"[Ll]ogin:\s*$")
_USERNAME_ONLY_RE = re.compile(rb"[Uu]sername:\s*$")
_PASSWORD_RE = re.compile(rb"[Pp]assword:\s*$")
_SHELL_RE = re.compile(rb"(?:[\*]?[ABab]:[A-Za-z0-9_\-.]+[#>]|[A-Za-z0-9_\-.]+\*?[#>$])\s*$")
_FAIL_RE = re.compile(rb"(?:[Ll]ogin\s+(?:incorrect|failed)|[Aa]uthentication\s+fail)")


def _drain_quiet(ser, quiet_ms: int = 400, max_wait: float = 2.0) -> None:
    """Read and discard bytes until the line has been quiet for *quiet_ms*.

    The device's banner can arrive in chunks over several hundred ms after a
    wake-up CR; if we send the first credential before the banner finishes,
    `reset_input_buffer` won't help — the bytes still in flight will land in
    the next read and trick us into thinking the device already responded.
    Sit and absorb those bytes before sending anything new.
    """
    deadline = time.time() + max_wait
    last_byte_time = time.time()
    while time.time() < deadline:
        n = ser.in_waiting
        if n:
            ser.read(n)
            last_byte_time = time.time()
        else:
            if (time.time() - last_byte_time) * 1000 >= quiet_ms:
                return
            time.sleep(0.05)


def _read_until(
    ser,
    pattern: re.Pattern,
    timeout: float,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[bytes, bool]:
    """Read bytes from *ser* until *pattern* matches the tail or *timeout*.

    Returns (buffer, matched). Buffer is the raw bytes read so far.

    Per-chunk byte transcript is emitted at DEBUG so an operator
    chasing "got 6 bytes but no prompt" can see exactly which bytes
    showed up and when. Enable via :func:`enable_serial_debug` or
    ``config.SERIAL_DEBUG = True``.
    """
    deadline = time.time() + timeout
    buf = bytearray()
    start = time.time()
    debug_on = logger.isEnabledFor(logging.DEBUG)
    chunk_idx = 0
    while time.time() < deadline:
        if should_stop and should_stop():
            return bytes(buf), False
        n = ser.in_waiting
        if n:
            chunk = ser.read(n)
            buf.extend(chunk)
            chunk_idx += 1
            if debug_on:
                logger.debug(
                    "[SERIAL][read] +%.0fms chunk#%d %s",
                    (time.time() - start) * 1000.0,
                    chunk_idx,
                    _format_byte_dump(chunk, max_bytes=120),
                )
            tail = bytes(buf[-512:])
            if pattern.search(tail):
                if debug_on:
                    logger.debug(
                        "[SERIAL][read] pattern matched after %.0fms / %d bytes",
                        (time.time() - start) * 1000.0, len(buf),
                    )
                return bytes(buf), True
            # Page-pause handling. Different vendors emit different
            # paging banners — handle the common ones inline so callers
            # don't have to re-implement the same loop per device.
            #   * Nokia SROS:        "Press any key to continue"
            #   * Ciena Waveserver:  "--more--"  (also "--More--")
            # A single space advances all of them by one screen.
            if b"Press any key to continue" in tail:
                ser.write(b" ")
                if debug_on:
                    logger.debug("[SERIAL][read] sent SPACE for 'Press any key' pager")
            elif b"--more--" in tail or b"--More--" in tail:
                ser.write(b" ")
                if debug_on:
                    logger.debug("[SERIAL][read] sent SPACE for --more-- pager")
        else:
            time.sleep(0.05)
    if debug_on:
        logger.debug(
            "[SERIAL][read] TIMEOUT after %.1fs / %d bytes (tail=%s)",
            timeout, len(buf), _format_byte_dump(bytes(buf[-120:]), max_bytes=120),
        )
    return bytes(buf), False


def open_serial_with_baud_probe(
    port: str,
    baud_rates: List[int],
    *,
    timeout: float = 2.0,
    should_stop: Optional[Callable[[], bool]] = None,
):
    """Open *port* trying each baud in *baud_rates*, returning the first
    that produces a recognisable login/shell/password prompt within
    *timeout* seconds.

    Returns the opened ``serial.Serial`` instance on success, or ``None``
    when every candidate baud rate is silent or garbled. The caller is
    responsible for closing the returned object.

    The probe sends a CRLF (a real ``ENTER`` keypress) to wake the
    console, reads briefly, and looks for any of:
    Login:/Username:/Password:/<host>#/<host>>. A wrong baud rate
    typically returns high-bit garbage or nothing at all -- neither
    matches the prompt patterns so we move on. Used by devices where
    the operator may not know the console speed in advance (RLS lab
    gear ships at 9600 OR 115200 depending on the flash image).

    Field note: an RLS R4 chassis kept echoing our bare ``\\r`` back
    but never rendering a prompt, even with a 10s timeout. Switching
    the wake byte to ``\\r\\n`` (matches what SecureCRT/PuTTY send
    when the operator hits Enter) made the prompt appear within a
    second. CR alone is interpreted by some CLI engines as "no commit
    yet" -- the LF is what commits the line and triggers the prompt.
    """
    import serial  # local import keeps the module importable on systems
                   # without pyserial when only the regexes are needed
    for baud in baud_rates:
        if should_stop and should_stop():
            return None
        logging.info(f"[SERIAL] Probing {port} at {baud} baud...")
        try:
            ser = serial.Serial(port, baud, timeout=timeout)
        except Exception as exc:
            logging.warning(f"[SERIAL] Could not open {port}@{baud}: {exc}")
            continue
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        try:
            # ``\r\n`` matches the byte sequence SecureCRT/PuTTY send
            # when the operator hits Enter. Some Ciena RLS firmwares
            # treat a bare ``\r`` as "carriage return, no commit" and
            # only render the prompt when they see the LF.
            ser.write(b"\r\n")
        except Exception:
            try:
                ser.close()
            except Exception:
                pass
            continue
        buf, matched = _read_until(
            ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop
        )
        if matched:
            logging.info(f"[SERIAL] {port} locked onto {baud} baud")
            return ser
        # Empty buffer = no response (cable issue or really wrong speed);
        # non-empty = bytes arrived but didn't match a prompt (likely the
        # wrong baud emitting garbage). Either way, close and try next.
        # Dump the captured bytes at INFO so the operator can tell which
        # case they're in WITHOUT flipping DEBUG: wrong-baud garbage
        # shows as high-bit non-printables, a missing-CR/LF device shows
        # as readable banner text without a trailing prompt, etc.
        logging.info(
            f"[SERIAL] {port}@{baud} did not yield a prompt "
            f"(got {len(buf)} bytes); trying next baud"
        )
        if buf:
            logging.info(
                f"[SERIAL] {port}@{baud} captured bytes: "
                f"{_format_byte_dump(buf, max_bytes=200)}"
            )
        try:
            ser.close()
        except Exception:
            pass
    logging.warning(
        f"[SERIAL] No baud rate in {baud_rates} produced a prompt on {port}"
    )
    return None


def serial_login(
    ser,
    defaults: List[Tuple[str, str]],
    timeout: float = 10.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, Optional[Tuple[str, str]]]:
    """Drive the device to a shell prompt, rotating through *defaults*.

    Sends a CR to wake the prompt, then:
      - if a shell prompt appears, succeed with no auth needed
      - if Login:/Username: appears, walk *defaults* trying each pair
        until a shell prompt appears or all pairs fail
    """
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    # Send a full ENTER (CRLF) -- some Ciena CLI engines need the LF
    # to commit; bare CR gets echoed but doesn't render the prompt.
    # Matches the wake sequence used by ``open_serial_with_baud_probe``.
    ser.write(b"\r\n")
    logger.debug("[SERIAL][login] wake-up CRLF sent; waiting for prompt")
    buf, matched = _read_until(ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop)
    tail = buf[-512:]

    if _SHELL_RE.search(tail):
        logging.info("[SERIAL] Already at shell prompt; no auth needed.")
        logger.debug(
            "[SERIAL][login] shell prompt at tail: %s",
            _format_byte_dump(tail, max_bytes=80),
        )
        return True, None

    if not (_LOGIN_RE.search(tail) or _PASSWORD_RE.search(tail)):
        logging.warning(
            f"[SERIAL] No prompt seen within {timeout}s. Last bytes: {tail[-200:]!r}"
        )
        return False, None

    for user, pw in defaults:
        logging.info(f"[SERIAL] Trying credentials user={user!r}")
        logger.debug("[SERIAL][login] sending username %r", user)
        # Wait for the line to go quiet so the device's banner finishes
        # before we send the username. Without this the first iteration
        # consumes residual banner bytes and never actually waits for the
        # device's Password: prompt.
        _drain_quiet(ser, quiet_ms=400, max_wait=2.0)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write((user + "\r").encode("utf-8", errors="replace"))
        buf, matched = _read_until(ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop)
        tail = buf[-512:]
        if _PASSWORD_RE.search(tail):
            logger.debug("[SERIAL][login] saw Password: prompt; sending password")
            ser.write((pw + "\r").encode("utf-8", errors="replace"))
            buf, matched = _read_until(ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop)
            tail = buf[-512:]
        if _SHELL_RE.search(tail):
            logging.info(f"[SERIAL] Login OK as {user!r}.")
            return True, (user, pw)
        # Some devices (Nokia SROS variants) don't print "Login incorrect" on
        # bad creds — they silently replay the banner and re-prompt with
        # Login:. Treat any post-credential return-to-Login: as auth failure.
        if _FAIL_RE.search(buf) or _LOGIN_RE.search(tail):
            logging.info(f"[SERIAL] Auth failed for {user!r}; trying next default.")
            continue
        # Whatever happened, we don't have a shell. Try next.
        logging.info(
            f"[SERIAL] No shell after {user!r}; trying next default. "
            f"Last bytes: {tail[-200:]!r}"
        )

    logging.warning("[SERIAL] All default credentials exhausted on serial console.")
    return False, None


def serial_getty_login(
    ser,
    *,
    login_name: str = "cli",
    shell_user: str = "admin",
    shell_pass: str = "admin",
    timeout: float = 10.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """Drive the Nokia 1830-family getty *three-stage* console login.

    Unlike :func:`serial_login` (single ``Login:``/``Password:``), the 1830
    family answers three distinct prompts::

        <host> login: cli
        Username: admin
        Password: <admin>
        <banner>
        <host>#

    i.e. the getty ``login:`` gets ``login_name`` (``cli`` — a login account,
    NOT the operator), then the inner ``Username:``/``Password:`` get
    ``shell_user``/``shell_pass`` (admin/admin). Answers whichever prompt is at
    the tail of the buffer until a shell prompt appears. Returns True on
    success.
    """
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    ser.write(b"\r\n")
    logger.debug("[SERIAL][getty] wake-up CRLF sent; waiting for prompt")
    buf, _ = _read_until(ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop)
    tail = buf[-512:]
    sent = {"login": 0, "user": 0, "pass": 0}

    for _ in range(8):
        if should_stop and should_stop():
            return False
        if _SHELL_RE.search(tail):
            logging.info("[SERIAL] Getty two-step login reached shell prompt.")
            return True
        if _FAIL_RE.search(buf):
            logging.warning("[SERIAL] Getty login rejected.")
            return False

        if _GETTY_LOGIN_RE.search(tail) and sent["login"] < 2:
            logger.debug("[SERIAL][getty] login: -> %r", login_name)
            _drain_quiet(ser, quiet_ms=400, max_wait=2.0)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write((login_name + "\r").encode("utf-8", errors="replace"))
            sent["login"] += 1
        elif _USERNAME_ONLY_RE.search(tail) and sent["user"] < 2:
            logger.debug("[SERIAL][getty] Username: -> %r", shell_user)
            ser.write((shell_user + "\r").encode("utf-8", errors="replace"))
            sent["user"] += 1
        elif _PASSWORD_RE.search(tail) and sent["pass"] < 2:
            logger.debug("[SERIAL][getty] Password: -> (inner)")
            ser.write((shell_pass + "\r").encode("utf-8", errors="replace"))
            sent["pass"] += 1
        else:
            break

        buf, _ = _read_until(ser, _PROMPT_RE, timeout=timeout, should_stop=should_stop)
        tail = buf[-512:]

    ok = bool(_SHELL_RE.search(tail))
    if not ok:
        logging.warning(
            f"[SERIAL] Getty two-step login did not reach a shell prompt. "
            f"Last bytes: {tail[-200:]!r}"
        )
    return ok


def capture_until_prompt(
    ser,
    command: str,
    timeout: float = 20.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Send *command* and read until the shell prompt re-appears.

    Returns the decoded output (excluding the prompt line) or None on
    timeout / abort.
    """
    try:
        ser.reset_input_buffer()
    except Exception:
        pass
    logger.debug("[SERIAL][cmd] sending %r (timeout=%.1fs)", command, timeout)
    started = time.time()
    ser.write((command + "\r").encode("utf-8", errors="replace"))
    buf, matched = _read_until(ser, _SHELL_RE, timeout=timeout, should_stop=should_stop)
    if should_stop and should_stop():
        return None
    elapsed = time.time() - started
    if not matched:
        logging.warning(
            f"[SERIAL] No prompt within {timeout}s for command {command!r}; "
            f"returning {len(buf)} bytes captured so far."
        )
    else:
        logger.debug(
            "[SERIAL][cmd] %r completed in %.0fms / %d bytes",
            command, elapsed * 1000.0, len(buf),
        )
    return buf.decode("utf-8", errors="replace")
