#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author  : Zackery Simino
# Company : LightRiver Technologies
# Desc    : Nokia 1830 PSI optical audit with network-map discovery.


__author__  = 'Zackery Simino'
__email__   = 'zsimino@lightriver.com'
__status__  = 'Production'
# 1.6.0 - Added "OSC Internal" tab: internal OSC Rx delta per line. Compares the
#         OSC received (OPR) power every card reports for a line (amp OSC SFP vs
#         OMDWB OSC SFP, joined by an internal patch) and flags a spread above
#         OSC_INTERNAL_DELTA_TOLERANCE (1 dB). select_osc_power now retains all
#         per-card readings instead of only the highest-scored one.
# 1.7.0 - Added "Internal Fibers" tab: intra-shelf patch loss. Pairs the amp
#         cards' LINE ports with the line-card SIG (SIGC/SIGL) ports from
#         `show interface topology` Int rows and diffs the launched output power
#         against the received input power (band from SIGC/SIGL), flagging patch
#         loss > INTERNAL_FIBER_LOSS_TOLERANCE (1 dB). Adds OMDWB Line{1,2}In
#         capture + parse_omdwb_line input fields (previously dead code) plus
#         OMDWB SIG-port (SigC/SigL In/Out) capture for the amp<->OMDWB patch.
#         OSC-SFP<->OSC delta is NOT computed: the OMDWB OSC facility reports no
#         received power (add/VOA side only), so there is no 2nd Rx to compare.
# 1.7.1 - Added "Power Health" column to Summary tab: CHECK (red) when a power
#         filter is admin-Up/Oper-Down or a supply-voltage / Power-Filter alarm
#         is present, else OK (green) -- surfaces the PF/SHELFINVOLT condition
#         without cross-referencing the Power Filters + Alarms tabs.
__version__ = '1.7.1'
__program__ = 'psi_audit'

import argparse
import atexit
import concurrent.futures
import getpass
import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime

# ── Windows: enable VT100 ANSI colour codes ───────────────────────────────────
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

# PSI CLI is reached over Telnet (SSH-as-admin dead-ends at the login banner);
# the transport lives in _open_telnet() / ssh_connect() below -- no SSH client.

import keyring
import xlsxwriter

# ── Approved software versions ────────────────────────────────────────────────
APPROVED_SW   = ('1830OLS-25.3-3',)
KEYCHAIN_ITEM = 'nokia_psi_pass'

# Max dB spread allowed between the OSC Received (OPR) power seen by the
# different cards a line's OSC transits internally (e.g. the amp card's OSC
# SFP vs the OMDWB OSC SFP, joined by an internal patch fiber). A spread above
# this flags a suspect internal OSC patch. Matches the 1 dB node-to-node span
# tolerance in _span_delta().
OSC_INTERNAL_DELTA_TOLERANCE = 1.0

# Max dB internal patch loss (LINE-OUT launched power minus the paired
# LINE-IN received power on the intra-shelf fiber) before the Internal Fibers
# tab flags a suspect patch. The 1 dB the coworker wants for internal fibers.
INTERNAL_FIBER_LOSS_TOLERANCE = 1.0

# Optical band each amplifier card carries, so an amp's single-band LINE power
# is compared against the matching band on a combined-band OMDWB port.
AMP_CARD_BAND = {'EILA': 'c', 'IRDM32': 'c', 'EILAL': 'l', 'IRDM32L': 'l'}

# ── Audit workbook colour palette (matches the supplied RLS audit template) ──
HDR_FILL   = '#99CCFF'
HDR_FONT   = '#000000'
FILL_ODD   = '#FDE9D9'
FILL_EVEN  = '#FDE9D9'
SITE_FILL_A = '#FDE9D9'   # per-site band A: peach/orange (first site, matches template)
SITE_FILL_B = '#C5D9F1'   # per-site band B: light blue (alternates with A)
LINK_BLUE  = '#0563C1'
GREEN_FILL = '#008000'
GREEN_FONT = '#FFFFFF'
RED_FILL   = '#C00000'
RED_FONT   = '#FFFFFF'
AMBER_FILL = '#FFC000'
AMBER_FONT = '#000000'

# ── Workbook layout ──────────────────────────────────────────────────────────
TIMESTAMP_ROW  = 0
HEADER_ROW     = 2
DATA_START_ROW = 3

# ── Terminal colours ───────────────────────────────────────────────────────────
green  = '\033[92m'
red    = '\033[91m'
amber  = '\033[93m'
resetc = '\033[0m'
bold   = '\033[1m'

# ── Card type sets ─────────────────────────────────────────────────────────────
ILA_AMP_CARDS      = {'EILA', 'EILAL'}
TERMINAL_AMP_CARDS = {'IRDM32', 'IRDM32L'}
OMDWB_CARD         = 'OMDWB'
CARD_CLI = {
    'EILA':   'eila',
    'EILAL':  'eilal',
    'IRDM32': 'irdm32',
    'IRDM32L':'irdm32l',
}

# ── Per-node caches for post-processing ────────────────────────────────────────
node_osc_data = {}   # short_name -> dict of SFP power values
node_nmap     = {}   # short_name -> list of (ne_name, ip, sw) tuples

# ══════════════════════════════════════════════════════════════════════════════
# Banner
# ══════════════════════════════════════════════════════════════════════════════
options = argparse.Namespace(
    nodes=None, file=None, user='admin', password=None,
    outfile=None, debug=False,
)
hostlist = []
# True for standalone CLI (interactive prompts allowed); run_audit() sets
# it False so the ATLAS worker thread never blocks on input()/getpass().
_INTERACTIVE = True


def _cli_bootstrap():
    """Populate options/hostlist from argv + interactive prompts.
    Standalone-CLI only -- ATLAS drives the module via run_audit(), so this
    (banner/argparse/input()/getpass) never runs on import."""
    global options, hostlist
    print(bold)
    print('╔══════════════════════════════════════════════════════════╗')
    print(f'║    Nokia 1830 PSI Optical Audit Tool  v{__version__}             ║')
    print('║    Transport Network Audit — LightRiver Technologies     ║')
    print(f'╚══════════════════════════════════════════════════════════╝{resetc}')
    print()

    # ══════════════════════════════════════════════════════════════════════════════
    # Argument parser
    # ══════════════════════════════════════════════════════════════════════════════
    parser = argparse.ArgumentParser(
        prog=__program__,
        description='Nokia 1830 PSI optical audit with network-map discovery',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('-n', '--nodes',    help='Comma-separated list of node hostnames/IPs')
    parser.add_argument('-f', '--file',     help='Text file with one node hostname/IP per line')
    parser.add_argument('-u', '--user',     help='SSH username (default: admin)', default='admin')
    parser.add_argument('-p', '--password', help='SSH password (omit to use keychain or prompt)')
    parser.add_argument('-o', '--outfile',  help='Output Excel filename')
    parser.add_argument('-d', '--debug',     help='Enable debug logging', action='store_true')
    options = parser.parse_args()

    # ══════════════════════════════════════════════════════════════════════════════
    # Interactive wizard — runs automatically when no CLI arguments are given.
    # Existing CLI usage (passing -n, -f, -u, etc.) is completely unaffected.
    # ══════════════════════════════════════════════════════════════════════════════
    if len(sys.argv) == 1:
        print(f'{bold}  --- Interactive Setup Wizard ---{resetc}')
        print()

        # ── Step 1: Node ──────────────────────────────────────────────────────────
        print('Step 1 of 4 : Node')
        options.nodes = input('  Enter node IP address: ').strip() or None
        print()

        # ── Step 2: Output file ───────────────────────────────────────────────────
        print('Step 2 of 4 : Output')
        _stamp = datetime.now().strftime('%Y%m%d_%H%M')
        _default_out = f'psi_audit_{_stamp}.xlsx'
        _outfile = input(f'  Excel output filename [{_default_out}]: ').strip()
        options.outfile = _outfile if _outfile else _default_out
        print()

        # ── Step 3: Options ───────────────────────────────────────────────────────
        print('Step 3 of 4 : Options  (press Enter to accept default)')
        options.debug = input('  Debug mode?                     [y/N]: ').strip().lower() == 'y'
        print()

        # ── Step 4: Credentials ───────────────────────────────────────────────────
        print('Step 4 of 4 : Credentials')
        _uname = input(f'  Username [{options.user}]: ').strip()
        if _uname:
            options.user = _uname
        print()

    # ── Resolve node list ─────────────────────────────────────────────────────────
    hostlist = []
    if options.nodes:
        hostlist = [h.strip() for h in options.nodes.replace(',', ' ').split() if h.strip()]
    elif options.file:
        try:
            with open(options.file) as fh:
                hostlist = [ln.strip() for ln in fh if ln.strip() and not ln.startswith('#')]
        except FileNotFoundError:
            print(f'{red}Node file not found: {options.file}{resetc}')
            sys.exit(1)

    if not hostlist:
        print(f'{red}No nodes specified. Run with --help for usage or with no arguments to use the wizard.{resetc}')
        sys.exit(1)

    # ── Output filename ───────────────────────────────────────────────────────────
    if not options.outfile:
        _stamp = datetime.now().strftime('%Y%m%d_%H%M')
        options.outfile = f'psi_audit_{_stamp}.xlsx'

    if not options.outfile.endswith('.xlsx'):
        options.outfile += '.xlsx'

    # ── Credentials ───────────────────────────────────────────────────────────────
    if not options.password:
        stored = None
        try:
            stored = keyring.get_password(KEYCHAIN_ITEM, options.user)
        except Exception:
            pass
        if stored:
            use_stored = input(f'Use stored password for {options.user}? [Y/n]: ').strip().lower()
            options.password = stored if use_stored in ('', 'y', 'yes') else None
        if not options.password:
            options.password = getpass.getpass(f'Password for {options.user}: ')
            save_pw = input('Save password to keychain? [y/N]: ').strip().lower()
            if save_pw in ('y', 'yes'):
                keyring.set_password(KEYCHAIN_ITEM, options.user, options.password)

    print()

# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def debug_log(msg, context=''):
    if options.debug:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f'{amber}[DBG {ts}] {context}: {msg}{resetc}')


def format_hostname(host):
    """Return (short_name, fqdn) resolving Apple DNS domains."""
    host = host.lower().strip()
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
        return host, host
    short = host.split('.')[0]
    if '.apple.com' in host or '.apple.net' in host:
        return short, host
    for domain in ('.corp.apple.com', '.bb.net.apple.com'):
        try:
            socket.gethostbyname(short + domain)
            return short, short + domain
        except Exception:
            pass
    return short, short


def adb_asset(serial):
    """Look up Apple ADB asset tag from serial number."""
    try:
        result = subprocess.run(
            ['adb', 'assets', f'serialNum={serial}', '-f', 'assetTag'],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        tag = result.stdout.strip()
        return tag if tag else '--'
    except Exception:
        return '--'


def alarm_colour(severity):
    """Return (fill_hex, font_hex) for alarm severity, or (None, None)."""
    if severity == 'CR':
        return RED_FILL, RED_FONT
    if severity in ('MJ', 'MN'):
        return AMBER_FILL, AMBER_FONT
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# SSH functions
# ══════════════════════════════════════════════════════════════════════════════
# Nokia PSI login-dialog matchers, applied to the tail of the shell buffer.
# After the SSH-layer auth the node presents a two-stage getty:
#   login: cli -> Username: <user> -> Password: <password> -> <host>#
# Some nodes go straight to the CLI prompt after SSH auth.
_PROMPT_RE = re.compile(r'[#>]\s*$')
_LOGIN_RE  = re.compile(r'(?i)(?:^|\s)login\s*:\s*$')
_USER_RE   = re.compile(r'(?i)user(?:name)?\s*:\s*$')
_PASS_RE   = re.compile(r'(?i)password\s*:\s*$')
_ACK_RE    = re.compile(r'(?i)\(\s*y\s*/\s*n\s*\)|\[\s*y\s*/\s*n\s*\]|continue\?|accept\?|press\s+y')
_FAIL_RE   = re.compile(r'(?i)(login incorrect|access denied|authentication fail|invalid password|permission denied)')
_MORE_RE   = re.compile(r'(?i)--\s*more\s*--|press any key|<space>')


class _Shell:
    """One Telnet session to a PSI node. Mimics just enough of the old pexpect
    ``child`` so run_command/ssh_close keep working unchanged. The PSI CLI is
    reachable over Telnet (SSH-as-admin dead-ends at the banner), via ATLAS's
    policy-aware ``utils.telnet.Telnet``; it runs in-process with no console, so
    output streams to the panel like the RLS REST audit."""
    __slots__ = ('telnet', 'prompt')

    def __init__(self, telnet, prompt=''):
        self.telnet = telnet
        self.prompt = prompt


# ── Session logging ──────────────────────────────────────────────────────────
# Raw SSH session capture (every send + receive, timestamped) to a per-run
# ``.session.log`` next to the report, so a failed/odd login or command exchange
# can be inspected afterwards. Best-effort -- it never raises into the audit.
_session_fh = None


def _session_log_path(output_path):
    try:
        return os.path.splitext(output_path)[0] + '.session.log'
    except Exception:
        return 'psi_audit.session.log'


def _session_open(output_path):
    global _session_fh
    _session_close()
    try:
        _session_fh = open(_session_log_path(output_path), 'w', encoding='utf-8')
        _slog('OPEN', f'PSI session log {datetime.now():%Y-%m-%d %H:%M:%S}')
    except Exception:
        _session_fh = None


def _session_close():
    global _session_fh
    if _session_fh is not None:
        try:
            _slog('CLOSE', f'{datetime.now():%Y-%m-%d %H:%M:%S}')
            _session_fh.close()
        except Exception:
            pass
        _session_fh = None


def _slog(tag, data):
    """Append one raw-session line (best-effort, never raises)."""
    if _session_fh is None:
        return
    try:
        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        _session_fh.write('[%s] %-9s %r\n' % (ts, tag, data))
        _session_fh.flush()
    except Exception:
        pass


def _open_telnet(host, timeout=20):
    """Open a Telnet session to a PSI node. Prefers ATLAS's policy-aware
    ``utils.telnet.Telnet`` and falls back to stdlib telnetlib for bare CLI
    use outside the repo.

    ``bypass_policy=True`` mirrors ATLAS's other Nokia 1830 Telnet path
    (script_interface._identify_via_telnet_1830): the PSI CLI is reachable
    *only* over the Telnet getty (SSH-as-admin dead-ends at the banner), so
    Telnet is mandatory here. The operator explicitly launches a discovery
    audit whose job is to walk neighbor nodes found in the seed's network
    map -- those IPs are learned at runtime and can't be pre-allowlisted, so
    the per-host allowlist gate would otherwise refuse every neighbor. Bypass
    still emits the per-host SECURITY warn-and-log audit line."""
    try:
        from utils.telnet import Telnet as _PolicyTelnet
    except Exception:
        _PolicyTelnet = None
    if _PolicyTelnet is not None:
        return _PolicyTelnet(host, timeout=timeout, bypass_policy=True,
                             skip_ssh_probe=True, purpose='nokia-1830-getty')
    from telnetlib import Telnet as _StdTelnet
    return _StdTelnet(host, timeout=timeout)


def _telnet_write(tn, data, log=None):
    """Write to the telnet session + record it in the session log (``log`` masks
    secrets). Accepts bytes or str."""
    try:
        tn.write(data if isinstance(data, bytes) else data.encode('ascii', 'ignore'))
    finally:
        _slog('SEND', log if log is not None else data)


def _telnet_close(tn):
    if tn is None:
        return
    try:
        _telnet_write(tn, b'exit\n', log='exit\\n')
        time.sleep(0.2)
    except Exception:
        pass
    try:
        tn.close()
    except Exception:
        pass


def _drain(tn, prompt=None, timeout=30.0, idle=2.0):
    """Read from a telnet session until it goes idle (no new data for ``idle``
    seconds), the ``prompt`` reappears at the tail, or ``timeout`` elapses.
    Advances Nokia CLI pagination (``--More--``) automatically. Logs every chunk
    to the session log."""
    buf = ''
    deadline = time.time() + timeout
    last = time.time()
    while time.time() < deadline:
        try:
            chunk = tn.read_very_eager()
        except EOFError:
            break
        except Exception as exc:
            _slog('RECV-ERR', str(exc))
            break
        if chunk:
            text = chunk.decode('ascii', errors='replace') if isinstance(chunk, bytes) else chunk
            buf += text
            last = time.time()
            _slog('RECV', text)
            if _MORE_RE.search(buf[-48:]):
                _telnet_write(tn, b' ', log='<space>')
            if prompt and buf.rstrip().endswith(prompt):
                break
        elif time.time() - last >= idle:
            break
        else:
            time.sleep(0.1)
    return buf


def _prompt_token(text):
    """Return the CLI prompt string (last non-blank line, e.g. 'usyhb1-l9i2#')."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return text.strip()


def ssh_connect(host, username, password):
    """
    Connect to a Nokia PSI node via TELNET (pure-Python, in-process). SSH-as-
    admin dead-ends at the login banner on these shelves (the interactive CLI
    never attaches for a programmatic SSH client), so -- exactly like ATLAS's
    Nokia inventory -- the audit drives the Telnet getty two-step:
        login: -> cli, Username: -> <user>, Password: -> <password>, <host>#
    Telnet reaches both the local craft port (172.16.0.1) and routed/remote
    interfaces, so this one path serves the seed (local or remote) and every
    discovered neighbor. Returns (session, prompt) or (None, None). (Name kept
    as ssh_connect so the rest of the audit is untouched.)
    """
    _slog('CONN', f'telnet {username}@{host}:23')
    try:
        tn = _open_telnet(host, timeout=20)
    except Exception as exc:
        _slog('CONNERR', f'{host}: {exc}')
        debug_log(exc, f'ssh_connect {host} (telnet open)')
        return None, None

    session = _Shell(tn)
    try:
        _slog('AUTH', f'{host} telnet open; driving getty')
        tn.read_until(b'login: ', timeout=10)
        _telnet_write(tn, b'cli\n', log='cli\\n')
        tn.read_until(b'Username: ', timeout=10)
        _telnet_write(tn, username.encode('ascii', 'ignore') + b'\n', log=f'{username}\\n')
        tn.read_until(b'Password: ', timeout=10)
        _telnet_write(tn, password.encode('ascii', 'ignore') + b'\n', log='<password>\\n')
        time.sleep(1.0)

        post = _drain(tn, None, timeout=10.0, idle=1.5)
        prompt = None
        for _ in range(6):
            if _FAIL_RE.search(post):
                _slog('LOGINFAIL', post[-200:])
                debug_log(f'telnet login rejected: {post[-160:]!r}', f'ssh_connect {host}')
                ssh_close(session)
                return None, None
            if _PROMPT_RE.search(post.rstrip()):
                prompt = _prompt_token(post)
                break
            # No prompt captured yet -- nudge with Enter to draw it out.
            _slog('LOGIN', f'no prompt yet; tail={post.rstrip()[-90:]!r}')
            _telnet_write(tn, b'\n', log='\\n')
            post += _drain(tn, None, timeout=8.0, idle=1.2)

        if not prompt:
            _slog('NOPROMPT', post[-300:])
            debug_log(f'No CLI prompt after telnet login: {post[-200:]!r}',
                      f'ssh_connect {host}')
            ssh_close(session)
            return None, None
        session.prompt = prompt
        _slog('PROMPT', prompt)
        debug_log(f'Prompt: {prompt!r}', f'ssh_connect {host}')
        return session, prompt
    except Exception as exc:
        _slog('SHELLERR', f'{host}: {exc}')
        debug_log(exc, f'ssh_connect {host} (telnet)')
        ssh_close(session)
        return None, None


def run_command(child, prompt, command, timeout=30):
    """Send a command over the Telnet session and return its output text with
    the command echo and trailing CLI-prompt line stripped. Drains until the
    prompt reappears or the session goes idle, so it works with the flat/
    space-padded output the parsers already handle."""
    try:
        tn = child.telnet
        _slog('CMD', command)
        _telnet_write(tn, command.encode('ascii', 'ignore') + b'\n', log=command + '\\n')
        raw = _drain(tn, prompt, timeout=timeout, idle=2.0)
        _slog('CMD-OUT', f'{command!r} -> {len(raw)} chars')
        lines = raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        cmd_s = command.strip()
        cleaned = []
        for ln in lines:
            if not ln.strip():
                continue
            if cmd_s and cmd_s in ln:            # command echo
                continue
            if _PROMPT_RE.search(ln.rstrip()):   # trailing CLI prompt line
                continue
            cleaned.append(ln.rstrip())
        return '\n'.join(cleaned)
    except Exception as exc:
        _slog('CMD-ERR', f'{command!r}: {exc}')
        debug_log(exc, f'run_command: {command}')
        return ''


def ssh_close(child):
    """Gracefully close the Telnet session."""
    if child is None:
        return
    _telnet_close(child.telnet)


# Windows: run helper CLIs (route, adb) without flashing a console window.
# ATLAS runs elevated as a windowless process (pythonw / windowed exe), so each
# child console app would otherwise pop a brief black window. CREATE_NO_WINDOW.
_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def _windows_is_admin():
    """Return True when this process has an elevated Windows token."""
    if sys.platform != 'win32':
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _windows_host_route(destination):
    """Return the active Windows /32 route for destination, or None."""
    try:
        result = subprocess.run(
            ['route', 'PRINT', destination],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        debug_log(exc, f'route PRINT {destination}')
        return None

    match = re.search(
        rf'^\s*{re.escape(destination)}\s+'
        rf'255\.255\.255\.255\s+(\S+)\s+(\S+)\s+(\d+)\s*$',
        result.stdout or '',
        re.MULTILINE,
    )
    if not match:
        return None
    return {
        'gateway': match.group(1),
        'interface': match.group(2),
        'metric': int(match.group(3)),
    }


def remove_neighbor_host_routes(destinations, gateway, interface_index=None):
    """Remove only the temporary Windows /32 routes added by this audit."""
    if sys.platform != 'win32' or not destinations:
        return True

    success = True
    for destination in destinations:
        cmd = ['route', 'DELETE', destination, 'MASK', '255.255.255.255', gateway]
        if interface_index is not None:
            cmd += ['IF', str(interface_index)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            debug_log(exc, f'route DELETE {destination}')
            success = False
            continue

        if result.returncode != 0 and _windows_host_route(destination):
            detail = ' '.join(((result.stdout or '') + ' ' + (result.stderr or '')).split())
            debug_log(
                f'Could not remove temporary route: {detail[-240:]}',
                f'route DELETE {destination}',
            )
            success = False
        else:
            debug_log(
                f'Removed temporary /32 route through {gateway}',
                f'route DELETE {destination}',
            )
    return success


def prepare_neighbor_host_routes(destinations, gateway, interface_index=None):
    """
    Ensure exact Windows /32 routes send OSC neighbors through the seed shelf.

    ``interface_index`` pins the route to a specific interface via ``route ADD
    ... IF <idx>``. This is REQUIRED for the OAMP-LAN path: the temp IP is a
    secondary address on a multi-homed NIC, and without the ``IF`` pin Windows
    binds the /32 to the wrong interface (source/route mismatch -> "transmit
    failed. General failure."). The CIT craft path leaves it None.

    Returns ``(ready, added_routes, error_message)``. Existing correct routes
    are preserved, and only routes created here are returned for later cleanup.
    """
    if sys.platform != 'win32':
        return True, [], ''

    destinations = list(dict.fromkeys(destinations))
    missing = []
    for destination in destinations:
        existing = _windows_host_route(destination)
        if not existing:
            missing.append(destination)
            continue
        if existing['gateway'] != gateway:
            return (
                False,
                [],
                f'{destination} already has a /32 route through '
                f'{existing["gateway"]}, not {gateway}',
            )
        debug_log(
            f'Existing /32 route uses gateway {gateway}',
            f'route {destination}',
        )

    if not missing:
        return True, [], ''
    if not _windows_is_admin():
        return (
            False,
            [],
            'Run PowerShell as Administrator so temporary neighbor routes can be added',
        )

    added = []
    for destination in missing:
        cmd = [
            'route', 'ADD', destination,
            'MASK', '255.255.255.255', gateway,
            'METRIC', '1',
        ]
        if interface_index is not None:
            cmd += ['IF', str(interface_index)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:
            remove_neighbor_host_routes(added, gateway, interface_index)
            return False, [], f'Could not add route for {destination}: {exc}'

        if result.returncode == 0:
            added.append(destination)

        verified = _windows_host_route(destination)
        if result.returncode != 0 or not verified or verified['gateway'] != gateway:
            detail = ' '.join(((result.stdout or '') + ' ' + (result.stderr or '')).split())
            remove_neighbor_host_routes(added, gateway, interface_index)
            return (
                False,
                [],
                f'Could not add {destination}/32 through {gateway}: {detail[-180:]}',
            )

        debug_log(
            f'Added temporary /32 route through {gateway} '
            f'on interface {verified["interface"]}',
            f'route ADD {destination}',
        )

    return True, added, ''


# ══════════════════════════════════════════════════════════════════════════════
# Site-type detection
# ══════════════════════════════════════════════════════════════════════════════
def detect_site(card_inv_text):
    """
    Parse card inventory text and return (site_type, amp_cards, omdwb_slot) where:
      site_type   : 'ILA' | 'TERMINAL' | 'UNKNOWN'
      amp_cards   : dict  {card_type: slot_label}  e.g. {'EILA': '1/3', 'EILAL': '1/2'}
      omdwb_slot  : str   slot label for OMDWB (e.g. '1/4' or '1/3') or None
    Tabular line format:  '    1/2  EILAL  EILAL  3KC...  RT...  WO...  NA  126.00  3.23'
    Uses finditer without a line anchor so it also works on Nokia wexpect
    flat-text output where the whole table is flattened onto one line — if the
    card table is flat and this fails, the node's site type, amp cards and
    OMDWB slot are all lost, which nulls out its OSC power, amplifiers and
    OSClan results.
    """
    cards = {}
    omdwb_slot = None
    for m in re.finditer(
        r'(\d+/\d+)\s+(EILA|EILAL|IRDM32|IRDM32L|OMDWB)\b',
        card_inv_text,
    ):
        slot, card_type = m.group(1), m.group(2)
        cards[card_type] = slot
        if card_type == OMDWB_CARD:
            omdwb_slot = slot
    if any(c in cards for c in ILA_AMP_CARDS):
        return 'ILA', {c: s for c, s in cards.items() if c in ILA_AMP_CARDS}, omdwb_slot
    if any(c in cards for c in TERMINAL_AMP_CARDS):
        return 'TERMINAL', {c: s for c, s in cards.items() if c in TERMINAL_AMP_CARDS}, omdwb_slot
    return 'UNKNOWN', {}, omdwb_slot


def _osc_output_label(card_cli, slot, sfp_number, metric):
    """Return a stable output key for one card-native OSC power command."""
    safe_slot = slot.replace('/', '_')
    return f'osc_{card_cli}_{safe_slot}_sfp{sfp_number}_{metric}'


def build_osc_power_commands(site_type, amp_cards, omdwb_slot, has_sfp2=False):
    """Build documented, read-only OPR/OPT commands for each OSC SFP port."""
    commands = []

    def _append_port(card_cli, slot, sfp_number, port_name=None):
        port_name = port_name or f'OscSfp{sfp_number}'
        command_root = f'show interface {card_cli} {slot}/{port_name}'
        for metric in ('opr', 'opt'):
            commands.append((
                _osc_output_label(card_cli, slot, sfp_number, metric),
                f'{command_root} {metric}',
            ))

    if omdwb_slot:
        sfp_numbers = (1, 2) if site_type == 'ILA' or has_sfp2 else (1,)
        for sfp_number in sfp_numbers:
            _append_port('omdwb', omdwb_slot, sfp_number)

    for card_type, slot in amp_cards.items():
        card_cli = CARD_CLI[card_type]
        if card_type in ILA_AMP_CARDS:
            for sfp_number in (1, 2):
                _append_port(card_cli, slot, sfp_number)
        elif card_type in TERMINAL_AMP_CARDS:
            _append_port(card_cli, slot, 1, 'OscSfp')

    return commands


def build_commands(site_type, amp_cards, omdwb_slot, has_sfp2=False):
    """
    Return list of (label, command) tuples based on site type, installed cards
    and OMDWB slot.  Commands match real Nokia 1830 PSI CLI output.
    """
    cmds = [
        ('card_inventory',  'show card inventory *'),
        ('iface_inventory', 'show interface inventory *'),
        ('pf',              'show pf *'),
        ('sw_version',      'show software ne brief'),
        ('ospf',            'show cn ospf area'),
        ('loopback',        'show interface loopback'),
        ('shelf',           'show shelf 1'),
        ('osc_status',      'show cn osc *'),
    ]

    if omdwb_slot:
        cmds.append(('osclan',       f'show interface omdwb {omdwb_slot}/OSClan det'))
        if site_type == 'ILA':
            cmds.append(('omdwb_line1', f'show int omdwb {omdwb_slot}/Line1Out det'))
            cmds.append(('omdwb_line2', f'show int omdwb {omdwb_slot}/Line2Out det'))
            cmds.append(('omdwb_line1_in', f'show int omdwb {omdwb_slot}/Line1In det'))
            cmds.append(('omdwb_line2_in', f'show int omdwb {omdwb_slot}/Line2In det'))
        else:
            cmds.append(('omdwb_line',  f'show int omdwb {omdwb_slot}/Line1Out det'))
            cmds.append(('omdwb_line_in', f'show int omdwb {omdwb_slot}/Line1In det'))
        # OMDWB SIG ports (internal, amp-facing): SigC=C-band, SigL=L-band, one
        # set per installed amp band. These are the far (OMDWB) end of the
        # amp<->OMDWB internal patches. SigNIn reports 'Total Input Power [C/L]
        # band', SigNOut 'Total Output Power [C/L] band' (parse_omdwb_line).
        sig_prefixes = {('SigC' if ct in ('EILA', 'IRDM32') else 'SigL')
                        for ct in amp_cards}
        for pref in sorted(sig_prefixes):
            for n in ((1, 2) if site_type == 'ILA' else (1,)):
                cmds.append((f'omdwb_{pref.lower()}{n}_in',
                             f'show int omdwb {omdwb_slot}/{pref}{n}In det'))
                cmds.append((f'omdwb_{pref.lower()}{n}_out',
                             f'show int omdwb {omdwb_slot}/{pref}{n}Out det'))

    for card_type, slot in amp_cards.items():
        cli_kw = CARD_CLI[card_type]
        if site_type == 'ILA':
            for direction in ('Line1In', 'Line1Out', 'Line2In', 'Line2Out'):
                label = f'amp_{cli_kw}_{direction.lower()}'
                cmds.append((label, f'show int {cli_kw} {slot}/{direction} detail'))
        else:
            for direction in ('LineIn', 'LineOut'):
                label = f'amp_{cli_kw}_{direction.lower()}'
                cmds.append((label, f'show int {cli_kw} {slot}/{direction} detail'))

    cmds.extend(build_osc_power_commands(
        site_type, amp_cards, omdwb_slot, has_sfp2,
    ))
    cmds.append(('networkmap', 'show cn networkmap'))
    cmds.append(('topology',   'show interface topology *'))
    cmds.append(('alarms',     'alm'))

    return cmds


# ══════════════════════════════════════════════════════════════════════════════
# Parsing helpers
# ══════════════════════════════════════════════════════════════════════════════
def _fv(text, field):
    """
    Extract first matching value for 'Field: value' pattern.
    Works with both line-based AND Nokia wexpect flat-text (no-newline) output.
    Stops at 3+ consecutive spaces (Nokia 2-column separator) or line end.
    """
    m = re.search(
        rf'(?<!\w){re.escape(field)}\s*:\s*(.+?)(?=\s{{3,}}|[\r\n]|$)',
        text,
    )
    return m.group(1).strip() if m else '--'


def _ov(text, suffix):
    """
    Match optical fields with optional 'Egress OA ', 'Ingress OA ', or 'OA ' prefix.
    EILA/EILAL fields have no prefix on Total/Signal Output Power but DO have
    'OA ' prefix on Target Gain / Current Gain / Actual Tilt.
    IRDM32(L) fields use 'Egress OA ' or 'Ingress OA ' uniformly.
    Stops at 3+ spaces (Nokia 2-column separator) so a second-column label is not
    included in the captured value.
    """
    pattern = rf'(?:(?:Egress |Ingress )?OA )?{re.escape(suffix)}\s*:\s*(.+?)(?=\s{{3,}}|[\r\n]|$)'
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else '--'


# ──────────────────────────────────────────────────────────────────────────────
def parse_card_inventory(text):
    """
    Parse tabular `show card inventory *` output:
      Location  Card Type  Mnemonic  Part Number  Serial Number  CLEI  Licensed  Pmax  Imax
         1/2    EILAL      EILAL     3KC70525...  RT253411061    WO... NA        ...
    Returns list of dicts keyed: card_type, slot, mnemonic, part_number,
    serial, clei, state.  Ignores MLFSB (shelf extenders).
    Uses finditer (not line-anchored re.match) so it also parses Nokia wexpect
    flat-text output where the whole table is flattened onto one line.
    """
    rows = []
    seen = set()
    for m in re.finditer(
        r'(\d+/\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
        text,
    ):
        slot, card_type, mnem, part, serial, clei = m.groups()
        if card_type in ('Type', 'Card'):
            continue
        if card_type == 'MLFSB':
            continue
        if slot in seen:
            continue
        seen.add(slot)
        rows.append({
            'card_type':   card_type,
            'slot':        slot,
            'mnemonic':    mnem,
            'part_number': part,
            'serial':      serial,
            'clei':        clei if clei != '-' else '--',
            'hw_version':  '--',
            'state':       '--',
        })
    return rows


def parse_interface_inventory(text):
    """
    Parse `show interface inventory *` output, e.g. the OSC SFP rows:
        Location     Module Type     Part Number  Serial Number
        1/4/OSCSFP1  SWE1GOL         3AL82260...  ALLU25--OF50000283
        1/4/OSCSFP2  SWE1GOL         3AL82260...  ALLU25--OF50000582
    Uses finditer (not line-anchored re.match) so the rows are still found when
    Nokia wexpect flattens the whole table onto a single space-padded line —
    otherwise this tab comes back empty (no OSC optics).
    """
    rows = []
    seen = set()
    for m in re.finditer(
        r'(\d+/\d+/\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
        text,
    ):
        loc, mod, part, serial = m.groups()
        if mod == 'Type':
            continue
        if loc in seen:
            continue
        seen.add(loc)
        rows.append({
            'iface_name':   loc,
            'port_type':    mod,
            'serial':       serial,
            'clei':         '--',
            'manufacturer': part,  # store part number where manufacturer would go
        })
    return rows


def parse_software(text):
    """
    Parse `show software ne brief` output.  Picks the SW version reported for
    a MEC2L slot (1/22 or 1/23) when present, otherwise the first slot's
    'current' version.  Returns the version string or '--'.
    Uses \\s+ between Slot and current to handle both newline-based and
    Nokia wexpect flat-text (space-padded, no newlines) output.
    """
    blocks = re.findall(
        r'For:\s*Shelf:\s*\d+\s*Slot:\s*(\d+)\s+current\s*:\s*(\S+)',
        text,
    )
    if not blocks:
        return '--'
    for slot, ver in blocks:
        if slot in ('22', '23'):
            return ver
    return blocks[0][1]


def parse_ospf(text):
    """
    Parse `show cn ospf area` output.
    Finds any OSPF area whose OpaqueLsaCapability contains 'DNS' and records
    that area's ID.  Router ID is derived from the Loopback IP at the call site
    since `show cn ospf` is not reliably available on all nodes.
    Works with both line-based and Nokia wexpect flat-text output.
    """
    result = {'router_id': '--', 'area_id': '--',
              'opaque_lsa_cap': '--', 'dns_capable': 'No'}
    # Find every (AreaID, OpaqueLsaCapability) pair in the area output
    for m_area in re.finditer(
        r'OSPF AreaID\s*:\s*(\S+).*?OpaqueLsaCapability\s*:\s*(\S+)',
        text, re.DOTALL,
    ):
        area_id = m_area.group(1).strip()
        cap     = m_area.group(2).strip()
        if 'DNS' in cap.upper():
            result['area_id']        = area_id
            result['opaque_lsa_cap'] = cap
            result['dns_capable']    = 'Yes'
            return result
        if result['area_id'] == '--':
            result['area_id']        = area_id
            result['opaque_lsa_cap'] = cap
    return result


def parse_osclan(text):
    """
    Parse `show interface omdwb <slot>/OSClan det` output.
    IPv4 Address line is 'IPv4 Address : 172.19.14.68/27'.
    Admin/Oper State are on the same line: 'Admin State : Up   Oper State : Up'.
    """
    ip_cidr = _fv(text, 'IPv4 Address')
    if ip_cidr != '--' and '/' in ip_cidr:
        ip_addr, mask = ip_cidr.split('/', 1)
    else:
        ip_addr, mask = ip_cidr, '--'
    m = re.search(
        r'Admin State\s*:\s*(\S+)\s+Oper State\s*:\s*(\S+)', text
    )
    admin = m.group(1) if m else '--'
    oper  = m.group(2) if m else '--'
    return {
        'ip_addr':    ip_addr.strip(),
        'mask':       mask.strip(),
        'mtu':        _fv(text, 'MTU'),
        'admin_state':admin,
        'oper_state': oper,
    }


def parse_loopback(text):
    """
    Parse `show interface loopback` output.  Format:
        Loopback IP Address
        Current    : 10.6.14.68/32
                     ::/0
    """
    m = re.search(r'Current\s*:\s*(\d+\.\d+\.\d+\.\d+(?:/\d+)?)', text)
    ip = m.group(1) if m else '--'
    return {
        'ip_addr':    ip,
        'admin_state':'--',
        'oper_state': '--',
    }


def parse_shelf(text):
    """
    Parse `show shelf 1` output.  Type field is 'Programmed Type : PSI-8L Shelf'.
    AC vs DC determined by 'PowerTypePF n : DC|AC' lines.
    Total power line: 'Total - 3.4 Amp 178 W'.
    """
    shelf_type = _fv(text, 'Programmed Type')
    pwr_type = '--'
    m_pt = re.search(r'PowerTypePF\s+\d+\s*:\s*(AC|DC)', text)
    if m_pt:
        pwr_type = m_pt.group(1)
    m = re.search(r'Total\s+-\s+([\d.]+)\s*[Aa]mp\s+([\d.]+)\s*W', text)
    if m:
        return {'shelf_type': shelf_type, 'total_current': m.group(1),
                'total_watts': m.group(2), 'pwr_type': pwr_type}
    return {'shelf_type': shelf_type, 'total_current': '--',
            'total_watts': '--', 'pwr_type': pwr_type}


def parse_osc_power(sfp1_opr, sfp1_opt, sfp2_opr='', sfp2_opt=''):
    """
    Parse OSC SFP power outputs. Each input can be a dedicated ``opr``/``opt``
    response, a PM response, or a full OSCSFP detail response.
    """
    def _pwr(text, direction):
        if not text:
            return '--'

        value = _fv(text, f'Supvy {direction} Power')
        if value == '--':
            match = re.search(
                rf'Supvy\s+{direction}\s+Power\s*:\s*'
                rf'(Off|[-+]?\d+(?:\.\d+)?\s*dBm)',
                text,
                re.I,
            )
            value = match.group(1) if match else '--'

        # The direct Nokia ``opr`` and ``opt`` commands can return only the
        # requested dBm value rather than the full labelled detail block.
        if value == '--':
            match = re.search(
                r'(?<![\w.])(Off|[-+]?\d+(?:\.\d+)?\s*dBm)(?!\w)',
                text,
                re.I,
            )
            value = match.group(1) if match else '--'

        if re.search(r'\bOff\b', value, re.I):
            return 'Off'
        number = re.search(r'[-+]?\d+(?:\.\d+)?', value)
        return f'{number.group(0)} dBm' if number else '--'

    return {
        'sfp1_opr': _pwr(sfp1_opr, 'Received'),
        'sfp1_opt': _pwr(sfp1_opt, 'Transmitted'),
        'sfp2_opr': _pwr(sfp2_opr, 'Received'),
        'sfp2_opt': _pwr(sfp2_opt, 'Transmitted'),
    }


def parse_osc_status(text):
    """Return OSC interface Admin/Oper state keyed by upper-case location."""
    status = {}
    for match in re.finditer(
        r'(\d+/\d+/OSCSFP(?:[12])?)\s+([A-Za-z0-9-]+)\s+'
        r'(Up|Down)\s+(Up|Down)\b',
        text,
        re.I,
    ):
        location = match.group(1).upper()
        status[location] = {
            'card': match.group(2).upper(),
            'admin': match.group(3).title(),
            'oper': match.group(4).title(),
        }
    return status


def select_osc_power(output, site_type, amp_cards, omdwb_slot, has_sfp2=False):
    """
    Select LINE1/SFP1 and LINE2/SFP2 powers from all installed OSC-capable
    cards.  An OSC interface reported Up by show cn osc * wins; otherwise the
    first card returning a real power value is used.
    """
    status = parse_osc_status(output.get('osc_status', ''))
    candidates = {1: [], 2: []}

    def _add_candidate(card_cli, slot, sfp_number, port_name=None):
        port_name = port_name or f'OSCSFP{sfp_number}'
        location = f'{slot}/{port_name}'.upper()
        opr_output = output.get(
            _osc_output_label(card_cli, slot, sfp_number, 'opr'),
            '',
        )
        opt_output = output.get(
            _osc_output_label(card_cli, slot, sfp_number, 'opt'),
            '',
        )
        parsed = parse_osc_power(opr_output, opt_output)
        rx = parsed['sfp1_opr']
        tx = parsed['sfp1_opt']
        for metric, raw in (('opr', opr_output), ('opt', opt_output)):
            if raw and _command_failed(raw):
                detail = ' '.join(raw.split())
                debug_log(
                    f'CLI rejected OSC {metric.upper()} command: '
                    f'{detail[-240:]}',
                    f'{card_cli} {location}',
                )
        interface_status = status.get(location, {})
        usable = _dbm_float(rx) is not None or _dbm_float(tx) is not None
        score = (
            (1000 if usable else 0)
            + (200 if interface_status.get('oper') == 'Up' else 0)
            + (100 if interface_status.get('admin') == 'Up' else 0)
            + (20 if _dbm_float(rx) is not None else 0)
            + (10 if _dbm_float(tx) is not None else 0)
        )
        candidates[sfp_number].append({
            'location': location,
            'card_cli': card_cli,
            'rx': rx,
            'tx': tx,
            'usable': usable,
            'score': score,
        })

    if omdwb_slot:
        sfp_numbers = (1, 2) if site_type == 'ILA' or has_sfp2 else (1,)
        for sfp_number in sfp_numbers:
            _add_candidate('omdwb', omdwb_slot, sfp_number)

    for card_type, slot in amp_cards.items():
        card_cli = CARD_CLI[card_type]
        if card_type in ILA_AMP_CARDS:
            _add_candidate(card_cli, slot, 1)
            _add_candidate(card_cli, slot, 2)
        elif card_type in TERMINAL_AMP_CARDS:
            _add_candidate(card_cli, slot, 1, 'OSCSFP')

    selected = {}
    sources = {}
    for sfp_number in (1, 2):
        pool = candidates[sfp_number]
        if pool:
            choice = max(pool, key=lambda item: (item['score'], item['usable']))
            selected[f'sfp{sfp_number}_opr'] = choice['rx']
            selected[f'sfp{sfp_number}_opt'] = choice['tx']
            sources[f'sfp{sfp_number}'] = (
                f'{choice["card_cli"].upper()} {choice["location"]}'
            )
        else:
            selected[f'sfp{sfp_number}_opr'] = '--'
            selected[f'sfp{sfp_number}_opt'] = '--'
            sources[f'sfp{sfp_number}'] = '--'

    # Internal OSC Rx delta per line. The selection above kept only the
    # highest-scored reading; here we keep EVERY card's Rx for the line so the
    # OSC Internal tab can compare the two ends of the internal OSC patch and
    # flag a spread above OSC_INTERNAL_DELTA_TOLERANCE. When only one card
    # reports the line the detail still records it (delta/flag come back '--').
    internal_delta = {}
    for line_sfp in (1, 2):
        line_readings = [
            (f'{c["card_cli"].upper()} {c["location"]}', c['rx'])
            for c in candidates[line_sfp]
        ]
        delta_str, flag_str, detail = _osc_internal_delta(line_readings)
        internal_delta[f'sfp{line_sfp}'] = {
            'delta':  delta_str,
            'flag':   flag_str,
            'detail': detail,
        }
    selected['internal_delta'] = internal_delta

    selected['sources'] = sources
    debug_log(
        f'SFP1={sources["sfp1"]} '
        f'({selected["sfp1_opr"]}/{selected["sfp1_opt"]}), '
        f'SFP2={sources["sfp2"]} '
        f'({selected["sfp2_opr"]}/{selected["sfp2_opt"]})',
        'select_osc_power',
    )
    return selected


def parse_pf(text):
    """
    Parse `show pf *` output.  Format:
        Location  Programmed Type  Admin State  Oper State  State Qualifier
            1/41  PF               Up           Up
    Returns list of dicts: slot, admin_state, oper_state, qualifier.
    Uses finditer so it works with both line-based and flat wexpect output.

    Columns:  Location  <Programmed Type=PF>  Admin  Oper  [State Qualifier].
    The State Qualifier (e.g. 'FLT') is optional -- the node emits it only when
    the filter is faulted/degraded (blank when Oper State is Up). Capturing it
    explains an otherwise-alarming "Oper Down": a single feed faulted on a
    dual-feed shelf, which stays powered/reachable on the healthy feed.
    """
    rows = []
    for m in re.finditer(r'(\d+/\d+)\s+PF\s+(\S+)\s+(\S+)(?:\s+(\S+))?', text):
        rows.append({
            'slot':        m.group(1),
            'admin_state': m.group(2),
            'oper_state':  m.group(3),
            'qualifier':   m.group(4) or '',
        })
    return rows


def parse_omdwb_line(text):
    """
    Parse an OMDWB Line{1,2}{Out,In} detail block. Output-side fields populate
    from a LineOut block, input-side fields from a LineIn block; the other side
    comes back '--' (harmless -- _fv returns '--' when a label is absent). Field
    labels are passed as plain text -- _fv re.escape()s them, so the '+' in
    'C + L bands' must NOT be pre-escaped here.
    """
    return {
        'supvy_out':    _fv(text, 'Supvy Out Power'),
        'span_loss':    _fv(text, 'OTS Span Loss'),
        'total_pwr_c':  _fv(text, 'Total Output Power C band'),
        'total_pwr_l':  _fv(text, 'Total Output Power L band'),
        'total_pwr_cl': _fv(text, 'Total Output Power C + L bands'),
        'total_in_c':   _fv(text, 'Total Input Power C band'),
        'total_in_l':   _fv(text, 'Total Input Power L band'),
        'total_in_cl':  _fv(text, 'Total Input Power C + L bands'),
    }


# Amplifier gain-range spec windows (dB) keyed by the EDFA's reported
# "OA Gain Range". The 1830 amp runs in either Low or High gain range; the
# valid Current Gain window differs per range. Used to flag an amp whose
# Current Gain sits outside the spec for its current range.
AMP_GAIN_SPEC = {'Low': (9.0, 18.0), 'High': (16.0, 27.0)}


def _gain_in_spec(gain_range, current_gain):
    """Return 'PASS'/'FAIL' if *current_gain* falls inside the spec window for
    the reported *gain_range* (Low: 9-18 dB, High: 16-27 dB); '--' when the
    range is unknown/absent or the gain value isn't numeric."""
    bounds = AMP_GAIN_SPEC.get((gain_range or '').strip().title())
    if not bounds:
        return '--'
    m = re.search(r'-?\d+(?:\.\d+)?', str(current_gain))
    if not m:
        return '--'
    lo, hi = bounds
    return 'PASS' if lo <= float(m.group(0)) <= hi else 'FAIL'


def parse_amp_port(text, site_type, direction):
    """
    Parse amplifier port detail for ILA (EILA/EILAL) or Terminal (IRDM32/IRDM32L).
    Field naming differs: EILA uses 'OA Target Gain' and bare 'Total Output Power';
    IRDM32 uses 'Egress OA Target Gain' / 'Ingress OA Target Gain' and prefixes
    'Egress OA Total Output Power'.  _ov() handles the optional prefix.
    """
    result = {
        'target_gain':    '--',
        'current_gain':   '--',
        'gain_range':     '--',
        'actual_tilt':    '--',
        'total_in_pwr':   '--',
        'total_out_pwr':  '--',
        'signal_out_pwr': '--',
        'agc_state':      '--',
    }
    if direction == 'out':
        result['target_gain']    = _ov(text, 'Target Gain')
        result['current_gain']   = _ov(text, 'Current Gain')
        result['gain_range']     = _ov(text, 'Gain Range')
        result['actual_tilt']    = _ov(text, 'Actual Tilt')
        result['total_out_pwr']  = _ov(text, 'Total Output Power')
        result['signal_out_pwr'] = _ov(text, 'Signal Output Power')
        # IRDM32 LineOut also reports input power on the egress side
        result['total_in_pwr']   = _ov(text, 'Total Input Power')
    else:
        # LineIn: EILA has only 'Total Input Power'; IRDM32 uses 'Ingress OA ...'
        result['total_in_pwr']   = _ov(text, 'Total Input Power')
        if site_type == 'TERMINAL':
            result['target_gain']    = _ov(text, 'Target Gain')
            result['current_gain']   = _ov(text, 'Current Gain')
            result['gain_range']     = _ov(text, 'Gain Range')
            result['actual_tilt']    = _ov(text, 'Actual Tilt')
            result['total_out_pwr']  = _ov(text, 'Total Output Power')
            result['signal_out_pwr'] = _ov(text, 'Signal Output Power')
    # 'AGC State' field not present in real output; use Auto Gain Adjust Enabled
    agc = _fv(text, 'Auto Gain Adjust Enabled')
    if agc != '--':
        result['agc_state'] = agc
    return result


# Internal signal-fiber connections in `show interface topology`. Endpoints are
# LINE* ports (on the booster/pre-amp amp cards) joined to SIG* ports
# (SIGC=C-band, SIGL=L-band, on the line/OMDWB card) -- the intra-shelf patches
# are LINE<->SIG, NOT LINE<->LINE. Any alpha-prefixed port ending IN/OUT counts.
# The OSC internal fiber (OSCSFP<->OSC, no IN/OUT suffix) is matched separately.
_INT_FIBER_RE = re.compile(
    r'(\d+/\d+/[A-Za-z]+\d*(?:IN|OUT))\s+(?:-\s+)?Int\s+(\d+/\d+/[A-Za-z]+\d*(?:IN|OUT))',
    re.I,
)


def parse_internal_fibers(topology_text):
    """
    Extract internal (intra-shelf) LINE fibers from `show interface topology *`.
    Returns [{'src': '<LINE*OUT aid>', 'dst': '<LINE*IN aid>'}] (upper-case),
    one entry per physical fiber even though topology lists it from both ends
    (the LINE-OUT row and the paired LINE-IN row). Works on line-based and
    Nokia wexpect flat-text output; only 'Int' (internal) LINE connections are
    matched -- external ('Ext') neighbor rows are ignored.
    """
    seen = set()
    fibers = []
    for a, b in _INT_FIBER_RE.findall(topology_text):
        a, b = a.upper(), b.upper()
        out_port = a if a.endswith('OUT') else b if b.endswith('OUT') else None
        in_port = a if a.endswith('IN') else b if b.endswith('IN') else None
        if not out_port or not in_port or out_port == in_port:
            continue
        key = (out_port, in_port)
        if key in seen:
            continue
        seen.add(key)
        fibers.append({'src': out_port, 'dst': in_port})
    return fibers


def build_port_power(output, site_type, amp_cards, omdwb_slot):
    """
    Map upper-case '<shelf>/<slot>/<PORT>' -> per-port optical power so an
    internal fiber's two LINE endpoints can be looked up by the names
    `show interface topology` reports. Amp LINE ports carry a single-band
    'in_total'/'out_total'; OMDWB LINE ports additionally carry per-band C/L
    values so a single-band amp reading can be matched to the same band.
    """
    ports = {}

    def _ent(slot, port, card):
        aid = f'{slot}/{port}'.upper()
        return ports.setdefault(aid, {
            'card': card,
            'in_total': '--', 'out_total': '--',
            'in_c': '--', 'out_c': '--', 'in_l': '--', 'out_l': '--',
        })

    for card_type, slot in amp_cards.items():
        cli = CARD_CLI[card_type]
        if site_type == 'ILA':
            for n in (1, 2):
                ind = parse_amp_port(
                    output.get(f'amp_{cli}_line{n}in', ''), site_type, 'in')
                outd = parse_amp_port(
                    output.get(f'amp_{cli}_line{n}out', ''), site_type, 'out')
                _ent(slot, f'LINE{n}IN', card_type)['in_total'] = ind['total_in_pwr']
                _ent(slot, f'LINE{n}OUT', card_type)['out_total'] = outd['total_out_pwr']
        else:
            ind = parse_amp_port(
                output.get(f'amp_{cli}_linein', ''), site_type, 'in')
            outd = parse_amp_port(
                output.get(f'amp_{cli}_lineout', ''), site_type, 'out')
            _ent(slot, 'LINEIN', card_type)['in_total'] = ind['total_in_pwr']
            _ent(slot, 'LINEOUT', card_type)['out_total'] = outd['total_out_pwr']

    if omdwb_slot:
        for n in ((1, 2) if site_type == 'ILA' else (1,)):
            out_txt = output.get(f'omdwb_line{n}', '') or output.get('omdwb_line', '')
            in_txt = output.get(f'omdwb_line{n}_in', '') or output.get('omdwb_line_in', '')
            od = parse_omdwb_line(out_txt)
            idp = parse_omdwb_line(in_txt)
            e_out = _ent(omdwb_slot, f'LINE{n}OUT', 'OMDWB')
            e_out['out_total'] = od['total_pwr_cl']
            e_out['out_c'] = od['total_pwr_c']
            e_out['out_l'] = od['total_pwr_l']
            e_in = _ent(omdwb_slot, f'LINE{n}IN', 'OMDWB')
            e_in['in_total'] = idp['total_in_cl']
            e_in['in_c'] = idp['total_in_c']
            e_in['in_l'] = idp['total_in_l']
        # SIG ports (amp-facing): band-specific in/out power feeds the internal
        # amp<->OMDWB patch-loss comparison (SigCNIn -> Total Input Power C band,
        # SigLNOut -> Total Output Power L band, etc.).
        for band in ('c', 'l'):
            pref = 'sigc' if band == 'c' else 'sigl'
            for n in ((1, 2) if site_type == 'ILA' else (1,)):
                idp = parse_omdwb_line(output.get(f'omdwb_{pref}{n}_in', ''))
                od = parse_omdwb_line(output.get(f'omdwb_{pref}{n}_out', ''))
                e_in = _ent(omdwb_slot, f'SIG{band.upper()}{n}IN', 'OMDWB')
                e_in['in_total'] = idp[f'total_in_{band}']
                e_in[f'in_{band}'] = idp[f'total_in_{band}']
                e_out = _ent(omdwb_slot, f'SIG{band.upper()}{n}OUT', 'OMDWB')
                e_out['out_total'] = od[f'total_pwr_{band}']
                e_out[f'out_{band}'] = od[f'total_pwr_{band}']

    return ports


def compute_internal_fibers(output, site_type, amp_cards, omdwb_slot):
    """
    Build the Internal Fibers rows: for each intra-shelf LINE fiber, the
    launched output power at the source LINE-OUT and the received input power
    at the paired LINE-IN, plus the patch loss between them. When one end is a
    single-band amp and the other a combined-band OMDWB, the OMDWB reading is
    taken from the amp's band so the comparison is like-for-like.
    """
    ports = build_port_power(output, site_type, amp_cards, omdwb_slot)
    rows = []
    for fib in parse_internal_fibers(output.get('topology', '')):
        src = ports.get(fib['src'], {})
        dst = ports.get(fib['dst'], {})
        src_card = src.get('card', '--')
        dst_card = dst.get('card', '--')
        # Band from the SIG port name (SIGC=C-band, SIGL=L-band) when present,
        # else fall back to the amp card's band.
        joined = (fib['src'] + ' ' + fib['dst']).upper()
        if 'SIGC' in joined:
            band = 'c'
        elif 'SIGL' in joined:
            band = 'l'
        else:
            band = AMP_CARD_BAND.get(src_card) or AMP_CARD_BAND.get(dst_card)
        if src_card == 'OMDWB' and band:
            src_out = src.get(f'out_{band}', '--')
        else:
            src_out = src.get('out_total', '--')
        if dst_card == 'OMDWB' and band:
            dst_in = dst.get(f'in_{band}', '--')
        else:
            dst_in = dst.get('in_total', '--')
        loss, flag = _patch_loss(src_out, dst_in)
        rows.append({
            'src_aid': fib['src'], 'src_card': src_card, 'src_out': src_out,
            'dst_aid': fib['dst'], 'dst_card': dst_card, 'dst_in': dst_in,
            'band': band.upper() if band else 'C+L',
            'loss': loss, 'flag': flag,
        })
    return rows


def parse_networkmap(text):
    """
    Parse `show cn networkmap` table:
        NE Name                IP Address       SW Release
        ussnu1-l9i1            10.6.14.68       1830OLS-25.3-3
    Works with line-preserving SSH output and flattened wexpect output.
    """
    rows = []
    seen_ips = set()
    for match in re.finditer(
        r'(?<!\S)([A-Za-z0-9_.-]+)\s+'
        r'((?:\d{1,3}\.){3}\d{1,3})\s+'
        r'(1830OLS-[A-Za-z0-9_.-]+)',
        text,
        re.I,
    ):
        name, ip, sw = match.groups()
        # wexpect can flatten the table separator and first data row into one
        # string ("-----uslgd1-l9i2 ...").  Do not turn that separator into a
        # second identity for the seed shelf.
        name = re.sub(r'^-{4,}', '', name)
        if not name:
            continue
        if ip in seen_ips:
            continue
        seen_ips.add(ip)
        rows.append({
            'ne_name':    name,
            'ne_ip':      ip,
            'ne_type':    '--',
            'sw_version': sw,
        })
    return rows


def parse_topology(text):
    """
    Parse `show interface topology *` table.
    Works with both line-based and Nokia wexpect flat-text (no newlines) output.
    Returns rows for all interfaces with external connections.
    """
    rows = []
    for m in re.finditer(
        r'(\d+/\d+/\S+)\s+'
        r'(Int|Ext|-)\s+(\S+(?:\s+\S+)?)\s+'
        r'(Int|Ext|-)\s+(\S+(?:\s+\S+)?)\s{3}',
        text,
    ):
        iface     = m.group(1)
        to_type   = m.group(2)
        conn_to   = m.group(3).strip()
        from_type = m.group(4)
        conn_from = m.group(5).strip()
        remote = conn_to if conn_to != '-' else conn_from
        rows.append({
            'local_ne':     '--',
            'local_iface':  iface,
            'remote_ne':    '--',
            'remote_iface': remote,
            'span_loss':    '--',
            '_to_type':     to_type,
            '_from_type':   from_type,
        })
    return rows


def parse_alarms(text):
    """
    Parse `alm` output.  Works with both line-based and Nokia wexpect flat-text
    (space-padded rows, no actual newlines).

    Alarm entries: severity + header on one terminal row, description + card on
    the next.  In flat-text mode the 'next row' is the space-separated gap
    between consecutive alarm header matches.
    """
    counts = {'CR': 0, 'MJ': 0, 'MN': 0, 'WN': 0}
    crit = re.search(r'Critical-(\d+)', text)
    majr = re.search(r'Major-(\d+)',    text)
    minr = re.search(r'Minor-(\d+)',    text)
    warn = re.search(r'Warning-(\d+)',  text)
    if crit: counts['CR'] = int(crit.group(1))
    if majr: counts['MJ'] = int(majr.group(1))
    if minr: counts['MN'] = int(minr.group(1))
    if warn: counts['WN'] = int(warn.group(1))

    _HDR = re.compile(
        r'(CR|MJ|MN|WN)\s+(SA|NSA)\s+'
        r'(\d{2,4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+'
        r'(\S+)\s+(\S+)\s+(\S+)\s+(\S+)',
    )
    alarms = []
    matches = list(_HDR.finditer(text))
    for idx, m in enumerate(matches):
        alarm = {
            'severity':    m.group(1),
            'sa':          m.group(2),
            'date':        m.group(3),
            'time':        m.group(4),
            'layer':       m.group(5),
            'condition':   m.group(6),
            'interface':   m.group(7),
            'direction':   m.group(8),
            'description': '--',
            'card':        '--',
        }
        # Text between this header's end and the next header's start holds the
        # description + card (separated by 3+ spaces in Nokia 2-column format).
        gap_end   = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        between   = text[m.end():gap_end].strip()
        if between:
            parts = [p.strip() for p in re.split(r'\s{3,}', between) if p.strip()]
            if parts:
                alarm['description'] = parts[0]
                if len(parts) >= 2:
                    alarm['card'] = parts[-1]
        alarms.append(alarm)
    return counts, alarms


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — Remote node helpers
# ══════════════════════════════════════════════════════════════════════════════

# The CIT craft-port routing hack -- temporary Windows /32 routes to the OSC
# neighbors plus toggling the seed's CIT OSPF area/redistribution -- only makes
# sense when the operator is plugged into the local craft interface at this IP,
# where the neighbors are reachable ONLY through the seed. Against a routed
# (remote-over-network) seed the hack is unnecessary and actively disrupts the
# working path (it re-homes the CIT OSPF area and re-advertises the subnet),
# which strands every neighbor. So it is applied ONLY when the seed is this
# address; any other seed connects to the discovered shelves directly.
CIT_CRAFT_SEED_IP = '172.16.0.1'

# Last octet of the temporary secondary IP the audit adds on the OAMP subnet
# when reaching RNEs through a routed GNE that the PC is L2-adjacent to (the
# "temp DCN over OAMP LAN" path -- see prepare_oamp_lan_transport).
OAMP_TEMP_HOST_OCTET = 250


def parse_neighbor_ips(topology_text):
    """
    Extract all external neighbor IPs from `show interface topology *` output.
    Works with both line-based and Nokia wexpect flat-text (no newlines) output.
    External rows look like:
        1/4/LINE2OUT  Ext  10.6.14.65 1/3/1   (has IP after 'Ext')
    Returns a list of (ip, line_num) tuples; duplicates are skipped.
    """
    seen = set()
    result = []
    for m in re.finditer(
        r'\S+/LINE(\d+)OUT\s+Ext\s+(\d+\.\d+\.\d+\.\d+)',
        topology_text,
    ):
        line_num = int(m.group(1))
        ip = m.group(2)
        if ip not in seen:
            seen.add(ip)
            result.append((ip, line_num))
    return result


def _discovery_alias(value):
    """Normalize a node name, address, or CIDR value for duplicate checks."""
    if value is None:
        return ''
    alias = str(value).strip().lower()
    if not alias or alias == '--':
        return ''
    if re.match(r'^\d+\.\d+\.\d+\.\d+/\d+$', alias):
        alias = alias.split('/', 1)[0]
    return alias


def prompt_node_name(prompt):
    """Return the NE name portion of a Nokia CLI prompt."""
    match = re.search(r'([^\s#>]+)[#>]\s*$', prompt or '')
    return match.group(1) if match else '--'


class DiscoveryTracker:
    """Queue network-map/topology discoveries and guarantee one full scan/site."""

    def __init__(self):
        self.queue = deque()
        self.records = []
        self.alias_to_record = {}
        self.scanned_aliases = {}
        self.seed_map_ips = set()
        # Parallel scans mutate the tracker from many worker threads at once.
        # A single reentrant lock guards every public mutator/reader below so
        # enqueue/claim/mark_* stay atomic. _in_progress reserves an identity
        # for the worker actively scanning it, closing the window where two
        # aliases of the same shelf could both be scanned before either is
        # marked done.
        self._lock = threading.RLock()
        self._in_progress = {}

    def _add_alias(self, record, value):
        alias = _discovery_alias(value)
        if not alias:
            return
        record['aliases'].add(alias)
        self.alias_to_record.setdefault(alias, record)

    def enqueue(self, target, ne_name='--', ne_ip='--', map_sw='--',
                method='Topology neighbor', source='--', depth=0,
                is_input=False):
        """Add a site unless any name/IP alias is already queued or scanned."""
        with self._lock:
            aliases = {
                alias for alias in (
                    _discovery_alias(target),
                    _discovery_alias(ne_name),
                    _discovery_alias(ne_ip),
                ) if alias
            }
            existing = next(
                (self.scanned_aliases[a] for a in aliases
                 if a in self.scanned_aliases),
                None,
            )
            if existing is None:
                existing = next(
                    (self.alias_to_record[a] for a in aliases
                     if a in self.alias_to_record),
                    None,
                )
            if existing:
                if existing['ne_name'] == '--' and ne_name != '--':
                    existing['ne_name'] = ne_name
                if existing['ne_ip'] == '--' and ne_ip != '--':
                    existing['ne_ip'] = ne_ip
                if existing['map_sw'] == '--' and map_sw != '--':
                    existing['map_sw'] = map_sw
                existing['is_input'] = existing['is_input'] or is_input
                for alias in aliases:
                    self._add_alias(existing, alias)
                return existing, False

            record = {
                'target': target,
                'ne_name': ne_name,
                'ne_ip': ne_ip,
                'map_sw': map_sw,
                'method': method,
                'source': source,
                'depth': depth,
                'is_input': is_input,
                'status': 'Queued',
                'actual_sw': '--',
                'site_type': '--',
                'actual_ip': '--',
                'neighbors': set(),
                'notes': '',
                'aliases': set(),
                # Buffered render data; each record is touched by exactly one
                # worker, so these need no lock. The whole document is written
                # out (in Terminal→Terminal order) after the parallel scan.
                '_render': None,      # per-tab parsed data for this site
                '_alarm_rows': [],    # (node_label, alarm) for the Alarms tab
                '_remote_rows': [],   # dicts for the Remote OSC tab
            }
            self.records.append(record)
            self.queue.append(record)
            for alias in aliases:
                self._add_alias(record, alias)
            return record, True

    def add_aliases(self, record, *values):
        with self._lock:
            for value in values:
                self._add_alias(record, value)

    def already_scanned(self, record):
        """Return the previously scanned record matching any queued alias."""
        with self._lock:
            for alias in record['aliases']:
                scanned = self.scanned_aliases.get(alias)
                if scanned is not None and scanned is not record:
                    return scanned
            return None

    def claim(self, record, *aliases):
        """Atomically reserve a node identity for scanning.

        Adds the given aliases, then returns a conflicting record if another
        worker has already scanned or is currently scanning the same identity;
        otherwise reserves every alias for this record and returns None. This
        is the parallel-safe replacement for ``already_scanned`` at the point
        a worker has learned the shelf's real CLI identity.
        """
        with self._lock:
            for value in aliases:
                self._add_alias(record, value)
            for alias in record['aliases']:
                other = (self.scanned_aliases.get(alias)
                         or self._in_progress.get(alias))
                if other is not None and other is not record:
                    return other
            for alias in record['aliases']:
                self._in_progress[alias] = record
            return None

    def mark_scanned(self, record, *aliases):
        with self._lock:
            record['status'] = 'Scanned'
            self.add_aliases(record, *aliases)
            for alias in record['aliases']:
                self.scanned_aliases[alias] = record
                self._in_progress.pop(alias, None)

    def mark_failed(self, record, status, note):
        with self._lock:
            record['status'] = status
            record['notes'] = note
            # Release any identity this record had reserved so a legitimate
            # retry of the same shelf is not permanently blocked.
            for alias in record.get('aliases', ()):
                if self._in_progress.get(alias) is record:
                    del self._in_progress[alias]

    def drain_queued(self):
        """Pop and return every currently queued record (thread-safe)."""
        with self._lock:
            items = list(self.queue)
            self.queue.clear()
            return items

    def discard_queued(self, record):
        """Remove one record from the pending queue if still present."""
        with self._lock:
            self.queue = deque(r for r in self.queue if r is not record)

    def find_scanned(self, value):
        with self._lock:
            return self.scanned_aliases.get(_discovery_alias(value))

    def find_known(self, value):
        with self._lock:
            return self.alias_to_record.get(_discovery_alias(value))


def parse_ping(text):
    """
    Parse `tools ping <IP>` output (standard Linux ping format).
    Returns (success: bool, latency_ms: str).
    """
    success = bool(re.search(r'0%\s+packet\s+loss', text))
    m = re.search(r'rtt\s+\S+\s*=\s*[\d.]+/([\d.]+)/', text)
    latency = (m.group(1) + ' ms') if m else '--'
    return success, latency


def _dbm_float(s):
    """Convert a dBm string like '-18.20 dBm' to float, or None if Off/missing."""
    if not s or s == '--' or 'Off' in str(s):
        return None
    m = re.search(r'(-?[\d.]+)', str(s))
    return float(m.group(1)) if m else None


def compute_span(opt, opr):
    """
    Span loss = TX (OPT) − RX (OPR).
    Returns formatted string 'XX.XX' or '--' if either value is Off/missing.
    """
    tx = _dbm_float(opt)
    rx = _dbm_float(opr)
    if tx is None or rx is None:
        return '--'
    return f'{tx - rx:.2f}'


def _best_opr(*values):
    """Return the first non-Off, non-'--' OPR value from the candidates."""
    for v in values:
        if v and v != '--' and 'Off' not in str(v):
            return v
    return '--'


def _span_delta(span_ab, span_ba):
    """
    Compute delta between two span-loss strings.
    Returns (delta_str, flag_str) where flag is 'OK', 'CHECK (X.XX dB)', or '--'.
    """
    try:
        a = float(span_ab)
        b = float(span_ba)
        delta = abs(a - b)
        flag = f'CHECK ({delta:.2f} dB)' if delta > 1.0 else 'OK'
        return f'{delta:.2f}', flag
    except (TypeError, ValueError):
        return '--', '--'


def _osc_internal_delta(readings):
    """
    Worst-case OSC Rx spread for one line across the cards that carry it.

    ``readings`` is an iterable of ``(source_label, rx_string)`` -- one entry
    per card whose OSC SFP reports a Received (OPR) power for the same line
    (e.g. the amp card's OSC SFP and the OMDWB OSC SFP, joined by an internal
    patch fiber). On a good internal patch those readings agree to within
    ``OSC_INTERNAL_DELTA_TOLERANCE`` dB.

    Returns ``(delta_str, flag_str, detail)`` where ``detail`` is the list of
    ``(source_label, rx_float)`` pairs actually compared. ``delta_str`` /
    ``flag_str`` are ``'--'`` when fewer than two cards report a usable Rx
    (nothing to difference -- the detail list still carries the single reading
    so the report can show it).
    """
    detail = []
    for source, rx in readings:
        value = _dbm_float(rx)
        if value is not None:
            detail.append((source, value))
    if len(detail) < 2:
        return '--', '--', detail
    spread = max(v for _, v in detail) - min(v for _, v in detail)
    flag = (f'CHECK ({spread:.2f} dB)'
            if spread > OSC_INTERNAL_DELTA_TOLERANCE else 'OK')
    return f'{spread:.2f}', flag, detail


def _patch_loss(out_str, in_str):
    """
    Internal patch loss for one fiber = launched output power (LINE-OUT) minus
    received input power (paired LINE-IN). Returns ``(loss_str, flag_str)``;
    both ``'--'`` when either power is missing. Flags when |loss| exceeds
    INTERNAL_FIBER_LOSS_TOLERANCE (a healthy intra-shelf patch is ~0 dB).
    """
    out = _dbm_float(out_str)
    inp = _dbm_float(in_str)
    if out is None or inp is None:
        return '--', '--'
    loss = out - inp
    flag = (f'CHECK ({loss:.2f} dB)'
            if abs(loss) > INTERNAL_FIBER_LOSS_TOLERANCE else 'OK')
    return f'{loss:.2f}', flag


def _power_health(pf_list, alarm_list):
    """
    Summary power/voltage health flag. Returns 'OK', or 'CHECK - <reason>' when
    a power filter is administratively Up but Oper-Down, or a supply-voltage /
    Power-Filter alarm is present. Lets the PF/SHELFINVOLT condition surface on
    the Summary tab without cross-referencing the Power Filters + Alarms tabs.
    """
    pf_down = [pf['slot'] for pf in pf_list
               if pf.get('admin_state') == 'Up' and pf.get('oper_state') != 'Up']
    power_alarms = sorted({
        a['condition'] for a in alarm_list
        if 'VOLT' in a.get('condition', '').upper()
        or 'POWER FILTER' in a.get('card', '').upper()
    })
    if not pf_down and not power_alarms:
        return 'OK'
    reasons = []
    if pf_down:
        reasons.append('PF Oper Down ' + ','.join(pf_down))
    if power_alarms:
        reasons.append(', '.join(power_alarms))
    return 'CHECK - ' + '; '.join(reasons)


# ══════════════════════════════════════════════════════════════════════════════
# NETCONF -> _render adapter
# ══════════════════════════════════════════════════════════════════════════════
# When the seed is reachable over the network (any address other than the
# 172.16.0.1 craft port), per-node data is collected via NETCONF
# (scripts.Network._psi_netconf.collect_node_netconf) instead of CLI scraping.
# render_from_netconf() maps one collector record into the SAME `_render` dict
# the workbook writer already consumes, so the discovery walk, the workbook, and
# the OAMP-LAN routing stay unchanged -- only the per-node data source changes.

def _slot_label(raw):
    """NETCONF bare slot ('2', 'PF1') -> the '1/2' shelf/slot label the sheets use."""
    raw = str(raw or '').strip()
    if not raw or raw == '--':
        return '--'
    return raw if '/' in raw else f'1/{raw}'


def _dbm(v):
    """Numeric dBm/dB -> string ('-10.01 dBm'); None -> '--'. Matches the CLI form
    the sheet writer and the _patch_loss/_osc_internal_delta helpers expect."""
    return f'{v} dBm' if v is not None else '--'


def _aid(port_name):
    """'PORT-1-4-SIGL1OUT' -> '1/4/SIGL1OUT' (the topology AID the sheet shows)."""
    return (port_name or '').replace('PORT-', '', 1).replace('-', '/', 2)


def _internal_fibers_from_netconf(rec):
    """Internal Fibers rows from the NETCONF ``connections`` (intra-shelf SIG<->
    LINE signal patches). Launched power = source port output-power, received
    power = dest port input-power. OMDWB SIG ports report calibrated power via
    components/optical-port; amp LINE ports report it only through the amplifier
    state, so those two are merged into one port->power view. Loss between the
    launched/received ends is computed with _patch_loss. External-span
    connections (discovery) and OSC-internal patches (OSCSFP<->OSC, which carry
    no signal power and never end IN/OUT) are skipped -- matching the CLI sheet."""
    pp = rec.get('port_power', {})
    ct = rec.get('card_type_by_slot', {})

    # Amp LINE-port optical power comes from the amplifier state, not the
    # port optical-port leaves; fold it into the port->power lookup so the
    # amp end of each patch has a value. Handle BOTH the ILA form (Line1/Line2 ->
    # LINE1/LINE2) and the terminal form (LineOut/LineIn -> LINEOUT/LINEIN).
    amp_out, amp_in = {}, {}
    for a in rec.get('amplifiers', []):
        slot = a.get('slot')
        pl = a.get('port_label') or ''
        if not slot:
            continue
        m = re.fullmatch(r'Line(\d+)', pl)
        if m:                                   # ILA line direction
            ln = m.group(1)
            amp_out[f'PORT-1-{slot}-LINE{ln}OUT'] = a.get('out_power')
            amp_in[f'PORT-1-{slot}-LINE{ln}IN'] = a.get('in_power')
        elif pl == 'LineOut':                   # terminal booster (line egress)
            amp_out[f'PORT-1-{slot}-LINEOUT'] = a.get('out_power')
        elif pl == 'LineIn':                    # terminal pre-amp (line ingress)
            amp_in[f'PORT-1-{slot}-LINEIN'] = a.get('in_power')

    def _out_pwr(port):
        v = pp.get(port, {}).get('out_power')
        return v if v is not None else amp_out.get(port)

    def _in_pwr(port):
        v = pp.get(port, {}).get('in_power')
        return v if v is not None else amp_in.get(port)

    def _card_of(port_name):
        m = re.match(r'PORT-\d+-([^-]+)-', port_name or '')
        return ct.get(m.group(1)) if m else None

    rows = []
    for c in rec.get('connections', []):
        if c.get('is_external'):
            continue
        src, dst = c.get('source'), c.get('dest')
        if not src or not dst or 'EXTERNAL' in (src, dst):
            continue
        # Signal patches only: both ends are directional LINE/SIG ports ending
        # IN/OUT. OSCSFP<->OSC internal patches (no IN/OUT) are not signal
        # fibers and carry no power -- drop them.
        if not (src.upper().endswith(('IN', 'OUT'))
                and dst.upper().endswith(('IN', 'OUT'))):
            continue
        src_out = _dbm(_out_pwr(src))
        dst_in = _dbm(_in_pwr(dst))
        src_card = _card_of(src) or '--'
        dst_card = _card_of(dst) or '--'
        joined = f'{src} {dst}'.upper()
        if 'SIGC' in joined:
            band = 'c'
        elif 'SIGL' in joined:
            band = 'l'
        else:
            band = AMP_CARD_BAND.get(src_card) or AMP_CARD_BAND.get(dst_card)
        loss, flag = _patch_loss(src_out, dst_in)
        rows.append({
            'src_aid': _aid(src), 'src_card': src_card, 'src_out': src_out,
            'dst_aid': _aid(dst), 'dst_card': dst_card, 'dst_in': dst_in,
            'band': band.upper() if band else 'C+L',
            'loss': loss, 'flag': flag,
        })
    return rows


def render_from_netconf(rec, fqdn, audit_date):
    """Map a ``collect_node_netconf`` record into the audit `_render` dict.

    Returns ``(render_dict, alarm_list)`` -- the caller stashes ``render_dict``
    on ``discovery_record['_render']`` and buffers ``alarm_list`` into
    ``_alarm_rows`` exactly as the CLI path does."""
    inv = rec.get('inventory', [])
    ct_by_slot = rec.get('card_type_by_slot', {})
    pp = rec.get('port_power', {})

    # ── site type + amp / OMDWB slots (from card mnemonics) ──────────────────
    amp_cards, omdwb_slot = {}, None
    for slot, mnem in ct_by_slot.items():
        if mnem in ILA_AMP_CARDS or mnem in TERMINAL_AMP_CARDS:
            amp_cards.setdefault(mnem, slot)
        elif mnem == OMDWB_CARD:
            omdwb_slot = slot
    # ILA vs TERMINAL is decided by AMP PORT SHAPE first -- the reliable signal
    # on networks where both node types use the same amp cards (this line runs
    # EILA/EILAL at ILAs *and* terminals). An ILA has two through-lines
    # (Line1/Line2); a terminal has one add/drop line (LineIn/LineOut). The card
    # mnemonic (IRDM32/IRDM32L => terminal) is only a fallback when no amp ports
    # were labelled.
    labels = {a.get('port_label') for a in rec.get('amplifiers', [])}
    if any(re.fullmatch(r'Line\d+', str(x) or '') for x in labels):
        site_type = 'ILA'
    elif {'LineIn', 'LineOut'} & labels:
        site_type = 'TERMINAL'
    elif any(c in TERMINAL_AMP_CARDS for c in amp_cards):
        site_type = 'TERMINAL'
    elif any(c in ILA_AMP_CARDS for c in amp_cards):
        site_type = 'ILA'
    else:
        site_type = 'UNKNOWN'

    # ── software ─────────────────────────────────────────────────────────────
    sw_ver = rec.get('software_version') or '--'
    sw_stat = 'Approved' if sw_ver in APPROVED_SW else 'NOT Approved'

    # ── inventory / asset ────────────────────────────────────────────────────
    chassis = next((c for c in inv if c.get('name', '').startswith('CHASSIS')), {})
    shelf_type = chassis.get('hw_name') or '--'
    asset_serial = chassis.get('serial') or next(
        (c.get('serial') for c in inv if c.get('serial')), '--')
    asset_tag = (adb_asset(asset_serial)
                 if asset_serial and asset_serial != '--' else '--')

    cards = []
    for c in inv:
        if not c.get('name', '').startswith('CARD-'):
            continue
        slot = c.get('slot')
        cards.append({
            'card_type':   ct_by_slot.get(slot) or c.get('ctype') or '--',
            'slot':        _slot_label(slot),
            'mnemonic':    ct_by_slot.get(slot) or '--',
            'part_number': c.get('part') or '--',
            'serial':      c.get('serial') or '--',
            'clei':        c.get('clei') or '--',
            'hw_version':  c.get('hw') or '--',
            'state':       c.get('oper') or '--',
        })

    # ── OSC optics (ifaces sheet) ────────────────────────────────────────────
    ifaces = [{
        'iface_name':   t.get('loc'),
        'port_type':    t.get('module') or '--',
        'serial':       t.get('serial') or '--',
        'clei':         t.get('clei') or '--',
        'manufacturer': t.get('part') or '--',
    } for t in rec.get('transceivers', [])]

    # ── loopback / OSCLAN / OSPF ─────────────────────────────────────────────
    ifs = rec.get('interfaces', {})

    def _pick_iface(match):
        for name, v in ifs.items():
            if match in name.upper() and v.get('ipv4') not in (None, '0.0.0.0'):
                return v
        return {}

    lo = _pick_iface('LOOPBACK-1')
    loop_ip = lo.get('ipv4')
    loopback = {
        'ip_addr':     f'{loop_ip}/{lo.get("prefix")}' if loop_ip else '--',
        'admin_state': '--',
        'oper_state':  lo.get('oper') or '--',
    }
    oscif = _pick_iface('OSCLAN')
    osclan = {
        'ip_addr':     oscif.get('ipv4') or '--',
        'mask':        str(oscif.get('prefix') or '--'),
        'mtu':         '--',
        'admin_state': '--',
        'oper_state':  oscif.get('oper') or '--',
    }
    nc_ospf = rec.get('ospf', {}) or {}
    router_id = nc_ospf.get('router_id') or loop_ip or '--'
    ospf = {'router_id':      router_id,
            'area_id':        nc_ospf.get('area_id') or '--',
            'opaque_lsa_cap': nc_ospf.get('opaque_lsa_cap') or '--',
            'dns_capable':    nc_ospf.get('dns_capable') or '--'}

    # ── shelf / power ────────────────────────────────────────────────────────
    power = rec.get('power', {}) or {}
    pwr_type = '--'
    for pf in power.get('pfs', []):
        if pf.get('voltage') is not None:
            pwr_type = 'DC' if pf['voltage'] < 70 else 'AC'
            break
    shelf = {
        'shelf_type':    shelf_type,
        'total_current': (str(power['total_current'])
                          if power.get('total_current') is not None else '--'),
        'total_watts':   (str(power['total_power'])
                          if power.get('total_power') is not None else '--'),
        'pwr_type':      pwr_type,
    }

    # ── OSC supervisory power per line ────────────────────────────────────────
    # Rx = best LINE{n}IN input-power-supervisory-channel; Tx = best LINE{n}OUT
    # output-power-supervisory-channel, carried on the OMDWB line ports. Ports
    # that do NOT carry the OSC report a floor (-40 dBm, amp line ports) or a
    # spurious out-of-range value; keep only readings in a plausible OSC window
    # so those neither win the selection nor create a bogus internal-delta CHECK.
    def _osc_ok(v):
        return v is not None and -40.0 < v < 30.0
    osc_pwr, internal_delta, sources = {}, {}, {}
    for n in (1, 2):
        rx_c = [(nm, v['in_supvy']) for nm, v in pp.items()
                if _osc_ok(v.get('in_supvy')) and f'LINE{n}IN' in nm.upper()]
        tx_c = [(nm, v['out_supvy']) for nm, v in pp.items()
                if _osc_ok(v.get('out_supvy')) and f'LINE{n}OUT' in nm.upper()]
        rx = max(rx_c, key=lambda x: x[1], default=(None, None))
        tx = max(tx_c, key=lambda x: x[1], default=(None, None))
        osc_pwr[f'sfp{n}_opr'] = _dbm(rx[1])
        osc_pwr[f'sfp{n}_opt'] = _dbm(tx[1])
        sources[f'sfp{n}'] = _aid(rx[0]) if rx[0] else (_aid(tx[0]) if tx[0] else '--')
        d_str, f_str, detail = _osc_internal_delta(
            [(_aid(nm), _dbm(val)) for nm, val in rx_c])
        internal_delta[f'sfp{n}'] = {'delta': d_str, 'flag': f_str, 'detail': detail}
    osc_pwr['internal_delta'] = internal_delta
    osc_pwr['sources'] = sources

    # ── amplifiers ─────────────────────────────────────────────────────────
    amp_rows = []
    for a in rec.get('amplifiers', []):
        amp_rows.append({
            'card_type':  a.get('card_type') or '--',
            'slot':       _slot_label(a.get('slot')),
            'port_label': a.get('port_label') or '--',
            'data': {
                'target_gain':    a.get('target_gain') if a.get('target_gain') is not None else '--',
                'current_gain':   a.get('current_gain') if a.get('current_gain') is not None else '--',
                'gain_range':     a.get('gain_range') or '--',
                'actual_tilt':    a.get('actual_tilt') if a.get('actual_tilt') is not None else '--',
                'total_in_pwr':   _dbm(a.get('in_power')),
                'total_out_pwr':  _dbm(a.get('out_power')),
                'signal_out_pwr': '--',
                'agc_state':      a.get('agc') or '--',
            },
        })

    # ── alarms (counts + rows in the CLI alarm-dict shape) ────────────────────
    counts = {'CR': 0, 'MJ': 0, 'MN': 0, 'WN': 0}
    alarm_list = []
    for al in rec.get('alarms', []):
        sev = al.get('severity')
        if sev in counts:
            counts[sev] += 1
        tc = (al.get('time_created') or '').split(' ')
        alarm_list.append({
            'severity':    sev or '--',
            'sa':          '--',
            'date':        tc[0] if tc and tc[0] else '--',
            'time':        tc[1] if len(tc) > 1 else '--',
            'layer':       '--',
            'condition':   al.get('condition') or '--',
            'interface':   al.get('resource') or '--',
            'direction':   '--',
            'description': al.get('text') or '--',
            'card':        al.get('resource') if al.get('kind') == 'card' else '--',
        })

    # ── power filters / health ────────────────────────────────────────────────
    pf_list = [{
        'slot':        pf['slot'],
        'admin_state': pf['admin_state'],
        'oper_state':  pf['oper_state'],
        'qualifier':   pf['qualifier'],
    } for pf in rec.get('power_filters', [])]
    power_health = _power_health(pf_list, alarm_list)

    # Local LINE<n> -> adjacent NE IP, from the external-span connections. Drives
    # the node-to-node OSC span (OSC Power tab) after the whole line is collected
    # (see _netconf_osc_spans). Terminal LINE ports have no digit -> line 1.
    line_peers = {}
    for c in rec.get('connections', []):
        if not c.get('is_external'):
            continue
        peer = c.get('external_peer')
        local = c.get('source') if c.get('source') != 'EXTERNAL' else c.get('dest')
        if not peer or not local:
            continue
        mm = re.search(r'LINE(\d+)', local.upper())
        line_peers.setdefault(int(mm.group(1)) if mm else 1, peer)

    host = rec.get('hostname') or fqdn
    render = {
        'host':            host,
        'short':           host,
        'fqdn':            fqdn,
        'site_type':       site_type,
        'sw_ver':          sw_ver,
        'sw_stat':         sw_stat,
        'asset_tag':       asset_tag,
        'loopback':        loopback,
        'osclan':          osclan,
        'ospf':            ospf,
        'shelf':           shelf,
        'osc_pwr':         osc_pwr,
        'line_peers':      line_peers,
        'alarm_counts':    counts,
        'audit_date':      audit_date,
        'cards':           cards,
        'ifaces':          ifaces,
        'pf_list':         pf_list,
        'power_health':    power_health,
        'amp_rows':        amp_rows,
        'internal_fibers': _internal_fibers_from_netconf(rec),
    }
    return render, alarm_list


def _node_identities(rec, d):
    """All IPs/names a record is known by, for matching an external-peer IP to
    the audited neighbour record."""
    ids = set()
    for k in (rec.get('actual_ip'), rec.get('ne_ip'), d.get('host')):
        if k and k != '--':
            ids.add(k)
    lo = (d.get('loopback') or {}).get('ip_addr', '')
    if lo and lo != '--':
        ids.add(lo.split('/')[0])
    return ids


def _netconf_osc_spans(records):
    """Rebuild the OSC Power node-to-node span tab (``_remote_rows``) for the
    NETCONF path from the OSC power every node already reported. For each local
    LINE<n> -> peer span, span A->B loss = local OSC Tx minus the peer's OSC Rx
    on the line facing us (B->A is the reverse); delta/flag via _span_delta.
    No CLI ping exists over NETCONF, so 'reachable' reflects whether the peer
    was actually scanned. Skips records already populated by the telnet PART 2."""
    by_key = {}
    for rec in records:
        d = rec.get('_render')
        if not d:
            continue
        for k in _node_identities(rec, d):
            by_key.setdefault(k, (rec, d))

    for rec in records:
        d = rec.get('_render')
        if not d or rec.get('_remote_rows'):
            continue
        my_ids = _node_identities(rec, d)
        osc = d.get('osc_pwr', {})
        rows = []
        for line, peer_ip in sorted((d.get('line_peers') or {}).items()):
            peer = by_key.get(peer_ip)
            local_tx = osc.get(f'sfp{line}_opt', '--')
            local_rx = osc.get(f'sfp{line}_opr', '--')
            posc, nb_site, reachable = {}, '--', False
            if peer:
                _, pd = peer
                posc = pd.get('osc_pwr', {})
                nb_site = pd.get('site_type', '--')
                reachable = True
                p_line = next((pl for pl, pip in (pd.get('line_peers') or {}).items()
                               if pip in my_ids), None)
                r_rx = posc.get(f'sfp{p_line}_opr', '--') if p_line else '--'
                r_tx = posc.get(f'sfp{p_line}_opt', '--') if p_line else '--'
            else:
                r_rx = r_tx = '--'
            span_ab = compute_span(local_tx, r_rx)      # local Tx - remote Rx
            span_ba = compute_span(r_tx, local_rx)      # remote Tx - local Rx
            delta_str, flag_str = _span_delta(span_ab, span_ba)
            rows.append({
                'host': d['host'], 'site_type': d.get('site_type', '--'),
                'nb_ip': peer_ip, 'nb_site_type': nb_site,
                'ping_ok': reachable, 'ping_latency': '--',
                'local_tx': local_tx, 'local_rx': local_rx,
                'r_sfp1_opr': posc.get('sfp1_opr', '--'),
                'r_sfp1_opt': posc.get('sfp1_opt', '--'),
                'r_sfp2_opr': posc.get('sfp2_opr', '--'),
                'r_sfp2_opt': posc.get('sfp2_opt', '--'),
                'span_ab': span_ab, 'span_ba': span_ba,
                'delta_str': delta_str, 'flag_str': flag_str,
            })
        rec['_remote_rows'] = rows


def _parse_cit_route_state(text):
    """Return ``(redistribute_state, ospf_area_index)`` from CIT output."""
    state = _fv(text, 'Redistribute').lower()
    area_match = re.search(r'OSPF Area Index\s*:\s*(\d+)', text, re.I)
    area = int(area_match.group(1)) if area_match else None
    return state, area


def _command_failed(text):
    """Return True when Nokia CLI output contains a command failure marker."""
    return bool(re.search(r'\b(?:error|invalid|denied|failed)\b', text, re.I))


def _restore_cit_state_on_session(child, prompt, restore_state):
    """Restore CIT redistribution and area using an existing shelf session."""
    original_state = restore_state['redistribute']
    original_area = restore_state['cit_area']

    # Disable redistribution before putting the CIT back into its original
    # area so no transient route is advertised from the wrong OSPF area.
    state_raw = run_command(
        child,
        prompt,
        f'config interface mfc 1/10/CIT redistribute {original_state}',
    )
    if _command_failed(state_raw):
        detail = ' '.join(state_raw.split())
        debug_log(
            f'Could not restore CIT redistribution: {detail[-240:]}',
            'restore_cit_remote_access',
        )
        return False

    area_raw = run_command(
        child,
        prompt,
        f'config interface mfc 1/10/CIT ospf areaindex {original_area}',
    )
    if _command_failed(area_raw):
        detail = ' '.join(area_raw.split())
        debug_log(
            f'Could not restore CIT OSPF area: {detail[-240:]}',
            'restore_cit_remote_access',
        )
        return False

    verify_raw = run_command(child, prompt, 'show interface mfc 1/10/CIT')
    verify_state, verify_area = _parse_cit_route_state(verify_raw)
    restored = verify_state == original_state and verify_area == original_area
    debug_log(
        f'CIT restored to redistribute={verify_state or "unknown"}, area={verify_area}'
        if restored else
        f'CIT restore verification failed: redistribute={verify_state or "unknown"}, '
        f'area={verify_area}',
        'restore_cit_remote_access',
    )
    return restored


def prepare_cit_remote_access(shelf_host, username, password):
    """
    Temporarily advertise the local CIT subnet into the OSC/DCN through OSPF.

    Returns ``(ready, restore_state)``.  ``restore_state`` records the original
    CIT redistribution and area settings when this function changes them; it
    is None when the existing configuration was already usable.
    """
    child, prompt = ssh_connect(shelf_host, username, password)
    if not child:
        debug_log(f'Could not reconnect to {shelf_host}', 'prepare_cit_remote_access')
        return False, None

    try:
        cit_raw = run_command(child, prompt, 'show interface mfc 1/10/CIT')
        loop_area_raw = run_command(
            child, prompt, 'show interface loopback ospfareaindex',
        )

        state, cit_area = _parse_cit_route_state(cit_raw)
        loop_area_match = re.search(
            r'OSPF Area Index\s*:\s*(\d+)', loop_area_raw, re.I,
        )
        loop_area = int(loop_area_match.group(1)) if loop_area_match else None

        debug_log(
            f'CIT redistribute={state or "unknown"}, '
            f'CIT area={cit_area}, loopback area={loop_area}',
            'prepare_cit_remote_access',
        )

        if cit_area is None or loop_area is None:
            debug_log('Could not verify OSPF area indexes', 'prepare_cit_remote_access')
            return False, None
        if state == 'enabled':
            # Preserve a route configuration that was already active when the
            # audit started, even if its area differs from the loopback.
            return True, None
        if state != 'disabled':
            debug_log('Could not determine CIT redistribution state', 'prepare_cit_remote_access')
            return False, None

        restore_state = {
            'redistribute': state,
            'cit_area': cit_area,
        }

        if cit_area != loop_area:
            area_raw = run_command(
                child,
                prompt,
                f'config interface mfc 1/10/CIT ospf areaindex {loop_area}',
            )
            if _command_failed(area_raw):
                detail = ' '.join(area_raw.split())
                debug_log(
                    f'Could not align CIT OSPF area: {detail[-240:]}',
                    'prepare_cit_remote_access',
                )
                return False, None

            area_verify_raw = run_command(
                child, prompt, 'show interface mfc 1/10/CIT',
            )
            _, area_verify = _parse_cit_route_state(area_verify_raw)
            if area_verify != loop_area:
                debug_log(
                    f'CIT OSPF area verification returned {area_verify}',
                    'prepare_cit_remote_access',
                )
                _restore_cit_state_on_session(
                    child, prompt, restore_state,
                )
                return False, None
            debug_log(
                f'CIT OSPF area temporarily changed from {cit_area} to {loop_area}',
                'prepare_cit_remote_access',
            )

        enable_raw = run_command(
            child,
            prompt,
            'config interface mfc 1/10/CIT redistribute enabled',
        )
        if _command_failed(enable_raw):
            detail = ' '.join(enable_raw.split())
            debug_log(
                f'Could not enable CIT redistribution: {detail[-240:]}',
                'prepare_cit_remote_access',
            )
            _restore_cit_state_on_session(child, prompt, restore_state)
            return False, None

        verify_raw = run_command(child, prompt, 'show interface mfc 1/10/CIT')
        verify_state, verify_area = _parse_cit_route_state(verify_raw)
        if verify_state != 'enabled' or verify_area != loop_area:
            debug_log(
                f'CIT route verification returned redistribute={verify_state or "unknown"}, '
                f'area={verify_area}',
                'prepare_cit_remote_access',
            )
            _restore_cit_state_on_session(child, prompt, restore_state)
            return False, None

        debug_log(
            'CIT subnet redistribution enabled; waiting for OSPF propagation',
            'prepare_cit_remote_access',
        )
        time.sleep(5)
        return True, restore_state
    finally:
        ssh_close(child)


def restore_cit_remote_access(shelf_host, username, password, restore_state):
    """Restore the CIT redistribution and OSPF area present before PART 2."""
    child, prompt = ssh_connect(shelf_host, username, password)
    if not child:
        debug_log(
            f'Could not reconnect to {shelf_host}; temporary CIT settings remain active',
            'restore_cit_remote_access',
        )
        return False

    try:
        return _restore_cit_state_on_session(child, prompt, restore_state)
    finally:
        ssh_close(child)


def _powershell(cmd, timeout=15):
    """Run a PowerShell one-liner (no console window). Returns (returncode, stdout)."""
    if sys.platform != 'win32':
        return None, ''
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return r.returncode, (r.stdout or '')
    except Exception as exc:
        debug_log(exc, 'powershell')
        return None, ''


def _iface_index_toward(host):
    """Windows interface index the OS uses to reach *host* -- needed to pin the
    multi-net temp IP + /32 routes to the NIC on the OAMP LAN."""
    if sys.platform != 'win32':
        return None
    _, out = _powershell(
        f'(Find-NetRoute -RemoteIPAddress {host} -ErrorAction SilentlyContinue '
        f'| Select-Object -First 1 -ExpandProperty InterfaceIndex)'
    )
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            return int(s)
    return None


def _local_ipv4s():
    """[(ip, prefix_len, alias, is_wifi)] for every up IPv4 interface (APIPA
    169.254.x excluded). One PowerShell round-trip; the basis for on-link (direct
    subnet-membership) tests and source selection."""
    if sys.platform != 'win32':
        return []
    _, out = _powershell(
        'Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | '
        'ForEach-Object { "$($_.IPAddress) $($_.PrefixLength) $($_.InterfaceAlias)" }')
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or parts[0].startswith('169.254'):
            continue
        alias = parts[2] if len(parts) > 2 else ''
        is_wifi = 'wi-fi' in alias.lower() or 'wireless' in alias.lower()
        rows.append((parts[0], parts[1], alias, is_wifi))
    return rows


def _wired_onlink_ip(seed_ip):
    """A WIRED local IPv4 whose subnet CONTAINS the seed -- i.e. the PC is
    genuinely L2-adjacent to the OAMP subnet via a wired NIC. None otherwise.

    Uses direct subnet membership, NOT Find-NetRoute NextHop: a gateway-less
    on-link NIC (e.g. a static 172.21.109.x on the OAMP LAN with no gateway)
    makes Find-NetRoute return empty, which previously mis-read as 'routed'."""
    try:
        seed = ipaddress.ip_address(seed_ip)
    except ValueError:
        return None
    for ip_s, plen, _alias, is_wifi in _local_ipv4s():
        if is_wifi or ip_s == seed_ip:
            continue
        try:
            if seed in ipaddress.ip_network(f'{ip_s}/{plen}', strict=False):
                return ip_s
        except ValueError:
            continue
    return None


def _seed_is_onlink(seed_ip):
    """True if the PC has a WIRED NIC on the seed's subnet (L2-adjacent) -- the
    only condition under which the OAMP-LAN temp-DCN may safely form. Routed-only
    (WiFi/VPN) or no wired on-subnet NIC -> False -> OAMP-LAN skipped so the
    routed interface is never disrupted; the audit direct-connects instead."""
    if sys.platform != 'win32':
        return False
    return _wired_onlink_ip(seed_ip) is not None


def _tcp_probe(host, port, source_ip, timeout=3.0):
    """True if a TCP connect to host:port succeeds when sourced from *source_ip*
    (i.e. that local NIC can actually reach the target)."""
    try:
        with socket.create_connection((host, port), timeout=timeout,
                                      source_address=(source_ip, 0)):
            return True
    except OSError:
        return False


def _preferred_source_ip(seed_ip, port):
    """Choose the local source IP the audit should bind to when reaching the gear,
    so a WIRED interface is used before Wi-Fi (the operator no longer has to
    disable Wi-Fi). Preference order, each verified reachable before use:
      1. wired NIC on the seed's subnet (on-link / L2-adjacent) -- also enables
         the full walk;
      2. any other wired NIC that can actually reach the seed (probed);
      3. a Wi-Fi NIC on the seed's subnet.
    Returns None (let the OS route normally) when only a routed Wi-Fi path exists
    -- so a Wi-Fi-only setup is never broken. Binding never picks a NIC that
    can't reach the seed, so it can't make a working setup fail."""
    if sys.platform != 'win32':
        return None
    try:
        seed = ipaddress.ip_address(seed_ip)
    except ValueError:
        return None
    rows = _local_ipv4s()

    def on_subnet(ip_s, plen):
        try:
            return seed in ipaddress.ip_network(f'{ip_s}/{plen}', strict=False)
        except ValueError:
            return False

    # 1. wired + on-link -- best (same-subnet is reachable; no probe needed).
    for ip_s, plen, _a, is_wifi in rows:
        if not is_wifi and ip_s != seed_ip and on_subnet(ip_s, plen):
            return ip_s
    # 2. wired + routed -- prefer over Wi-Fi only if it can actually reach the seed.
    for ip_s, _p, _a, is_wifi in rows:
        if not is_wifi and ip_s != seed_ip and _tcp_probe(seed_ip, port, ip_s):
            return ip_s
    # 3. Wi-Fi on-link (rare) -- still better than an ambiguous default.
    for ip_s, plen, _a, is_wifi in rows:
        if is_wifi and ip_s != seed_ip and on_subnet(ip_s, plen):
            return ip_s
    return None                       # routed Wi-Fi only -> let the OS route


def _oamp_temp_ip(seed_ip, prefix):
    """Pick a temporary host IP on the seed's OAMP subnet for the audit PC.
    Prefers .<OAMP_TEMP_HOST_OCTET>; falls back to the top usable host. Never the
    seed. Returns None if it can't be computed."""
    try:
        net = ipaddress.ip_network(f'{seed_ip}/{prefix}', strict=False)
        seed = ipaddress.ip_address(seed_ip)
    except ValueError:
        return None
    cand = ipaddress.ip_address(int(net.network_address) | OAMP_TEMP_HOST_OCTET)
    if (cand in net and cand not in (seed, net.network_address, net.broadcast_address)):
        return str(cand)
    hi = net.broadcast_address - 1
    if hi in net and hi != seed and hi != net.network_address:
        return str(hi)
    return None


def _add_temp_ip(iface_index, ip, prefix):
    """Ensure a temporary secondary IP is present on *iface_index* (multi-net onto
    the OAMP subnet). Returns ``(present, created)``: *present* is True when the
    address is on the interface after this call; *created* is True only when THIS
    call added it.

    Idempotent: an address already on the interface -- e.g. an operator who
    pre-staged the OAMP subnet by hand with the same ``.250`` the tool picks -- is
    reused (present=True) and reported created=False, so the setup does not fail
    on the ``New-NetIPAddress`` "already exists" error and teardown never removes
    an address the audit did not create."""
    if sys.platform != 'win32' or iface_index is None:
        return False, False
    # Reuse a pre-existing address rather than re-adding it (New-NetIPAddress
    # errors on a duplicate) -- and mark it not-owned so teardown leaves it.
    _, existing = _powershell(
        f"if (Get-NetIPAddress -InterfaceIndex {iface_index} -IPAddress {ip} "
        f"-AddressFamily IPv4 -ErrorAction SilentlyContinue) {{ 'PRESENT' }} "
        f"else {{ 'ABSENT' }}"
    )
    if 'PRESENT' in existing:
        debug_log(
            f'temp IP {ip} already on if {iface_index} -- reusing (not owned)',
            'oamp multinet',
        )
        return True, False
    rc, out = _powershell(
        f'New-NetIPAddress -InterfaceIndex {iface_index} -IPAddress {ip} '
        f'-PrefixLength {prefix} -ErrorAction Stop'
    )
    ok = rc == 0
    debug_log(
        f'temp IP {ip}/{prefix} on if {iface_index}: '
        f'{"added" if ok else out[-200:]}',
        'oamp multinet',
    )
    return ok, ok


def _remove_temp_ip(iface_index, ip):
    """Remove the temporary secondary IP added by _add_temp_ip (best-effort)."""
    if sys.platform != 'win32' or iface_index is None or not ip:
        return
    _powershell(
        f'Remove-NetIPAddress -InterfaceIndex {iface_index} -IPAddress {ip} '
        f'-Confirm:$false -ErrorAction SilentlyContinue'
    )
    debug_log(f'removed temp IP {ip} on if {iface_index}', 'oamp multinet')


def prepare_oamp_remote_access(shelf_host, username, password):
    """Advertise the seed's OAMP (DCN) subnet into OSPF so RNEs have a return
    path to the audit PC's temp IP on that subnet -- over NETCONF <edit-config>,
    NOT telnet getty (the getty is exclusive to the 172.16.0.1 craft port and was
    the last fragile dependency in the routed walk). Enabling adds the OAMP as a
    passive OSPFv2 interface (advertise-only, no adjacency); it is verified by
    read-back. Returns ``(ready, restore_state, oamp_prefix)``; restore_state is
    'disable' only when THIS call enabled it, None when already advertising."""
    from scripts.Network import _psi_oamp_netconf as _oamp_nc
    ready, restore_state, prefix = _oamp_nc.prepare_over_netconf(
        shelf_host, username, password)
    if ready and restore_state == 'disable':
        # We just enabled redistribution -- let OSPF propagate the OAMP subnet to
        # the RNEs before the walk connects to them.
        debug_log('OAMP redistribute enabled over NETCONF; waiting for OSPF '
                  'propagation', 'prepare_oamp_remote_access')
        time.sleep(8)
    else:
        debug_log(f'OAMP prepare over NETCONF: ready={ready}, '
                  f'restore={restore_state}, prefix=/{prefix}',
                  'prepare_oamp_remote_access')
    return ready, restore_state, prefix


def restore_oamp_remote_access(shelf_host, username, password, restore_state):
    """Restore the OAMP routestate present before the audit, over NETCONF."""
    from scripts.Network import _psi_oamp_netconf as _oamp_nc
    ok = _oamp_nc.restore_over_netconf(
        shelf_host, username, password, restore_state)
    if not ok:
        debug_log(f'OAMP restore over NETCONF did not verify for {shelf_host}',
                  'restore_oamp_remote_access')
    return ok


def _ping_from(source_ip, dest_ip):
    """Ping *dest_ip* sourced from *source_ip* (Windows). Used to confirm the PC
    is L2-adjacent to the GNE OAMP after multi-netting. True on any reply."""
    if sys.platform != 'win32':
        return False
    try:
        r = subprocess.run(
            ['ping', '-n', '2', '-w', '1000', '-S', source_ip, dest_ip],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
        return 'TTL=' in (r.stdout or '')
    except Exception as exc:
        debug_log(exc, f'ping {dest_ip} from {source_ip}')
        return False


def _oamp_lan_teardown(st, username, password):
    """Reverse _setup_oamp_lan: remove /32 routes, remove the temp OAMP IP, and
    restore the GNE OAMP routestate. Best-effort; safe on partial state."""
    if not st:
        return
    if st.get('routes'):
        remove_neighbor_host_routes(st['routes'], st['gateway'], st.get('iface'))
    if st.get('temp_ip'):
        _remove_temp_ip(st.get('iface'), st['temp_ip'])
    if st.get('oamp_restore'):
        restore_oamp_remote_access(
            st['seed_fqdn'], username, password, st['oamp_restore'])


def _wired_up_ifaces():
    """(ifIndex, description) for connected, physical, non-Wi-Fi adapters -- the
    candidates for a direct OAMP-LAN connection. Wi-Fi is deliberately excluded so
    the OAMP /24 is never multi-net onto the routed wireless NIC."""
    if sys.platform != 'win32':
        return []
    _, out = _powershell(
        "Get-NetAdapter -Physical -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'Up' } | "
        "ForEach-Object { \"$($_.ifIndex) $($_.InterfaceDescription)\" }")
    res = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        idx, desc = int(parts[0]), parts[1]
        low = desc.lower()
        if 'wi-fi' in low or 'wifi' in low or 'wireless' in low:
            continue
        res.append((idx, desc))
    return res


def _ensure_wired_oamp_adjacency(seed_ip, prefix):
    """Find or CREATE a wired NIC that is L2-adjacent to the seed's OAMP subnet,
    so the operator does not have to hand-configure a 172.21.109.x IP.

    Returns ``(source_ip, iface_index, created)``; *created* is True only when
    THIS call added the address (teardown must then remove it). Never touches
    Wi-Fi. Returns ``(None, None, False)`` when no connected wired NIC can reach
    the seed on-link (remote / Wi-Fi-only -> caller direct-connects).

    Probe: on each connected wired NIC add the OAMP temp IP, ping the seed from
    it; the NIC that answers on-link is the OAMP LAN. A NIC that does not answer
    has its temp IP removed immediately, so nothing is left on the wrong NIC."""
    # 1. A wired NIC already on the seed subnet that actually reaches it (operator
    #    pre-configured, or a leftover) -- reuse it, do not own it.
    existing = _wired_onlink_ip(seed_ip)
    if existing:
        idx = _iface_index_toward(seed_ip)
        if idx and _ping_from(existing, seed_ip):
            return existing, idx, False

    # 2. Set the OAMP temp IP on each connected wired NIC in turn until one
    #    reaches the seed on-link.
    temp = _oamp_temp_ip(seed_ip, prefix)
    if not temp:
        return None, None, False
    for idx, desc in _wired_up_ifaces():
        present, created = _add_temp_ip(idx, temp, prefix)
        if not present:
            continue
        if _ping_from(temp, seed_ip):
            debug_log(f'set temp {temp}/{prefix} on if {idx} ({desc}) -- reaches '
                      f'the seed on-link', 'oamp wired-adjacency')
            return temp, idx, created
        if created:                       # not the OAMP NIC -> undo
            _remove_temp_ip(idx, temp)
    return None, None, False


def _setup_oamp_lan(seed_fqdn, seed_ip, username, password, route_targets):
    """Build the temporary "DCN over the OAMP LAN" for a routed GNE the audit PC
    is wired to. ATLAS SETS the required OAMP-subnet IP on the wired NIC itself
    (probing connected wired NICs, never Wi-Fi) and removes it at teardown, so the
    operator no longer has to hand-configure 172.21.109.x. Steps:
      1. Establish wired L2 adjacency (set + ping-verify an OAMP temp IP).
      2. GNE advertises its OAMP subnet into OSPF (RNE return path).
      3. Interface-pinned /32 routes to each neighbour through the GNE OAMP.
    A remote/Wi-Fi-only PC finds no wired OAMP NIC in step 1 and returns None
    (caller direct-connects; the routed interface is never touched). Returns a
    teardown-state dict on success, or None (fully unwound)."""
    from scripts.Network import _psi_oamp_netconf as _oamp_nc
    prefix = _oamp_nc.oamp_prefix_over_netconf(seed_fqdn, username, password)

    # 1. Establish (or reuse) a wired NIC on the OAMP subnet -- ATLAS sets the IP.
    source_ip, iface, created = _ensure_wired_oamp_adjacency(seed_ip, prefix)
    if not source_ip:
        debug_log('OAMP-LAN: no connected wired NIC reaches the seed on-link '
                  '(remote / Wi-Fi only) -- direct-connect instead; nothing set',
                  '_setup_oamp_lan')
        return None

    # 2. Advertise the OAMP subnet into OSPF (RNE return path to source_ip).
    ready, oamp_restore, _pfx = prepare_oamp_remote_access(
        seed_fqdn, username, password)
    if not ready:
        debug_log('OAMP-LAN: OAMP redistribute prepare failed', '_setup_oamp_lan')
        if created:
            _remove_temp_ip(iface, source_ip)
        return None
    st = {
        'gateway': seed_ip, 'iface': iface, 'source_ip': source_ip,
        'temp_ip': source_ip if created else None, 'routes': [],
        'oamp_restore': oamp_restore, 'seed_fqdn': seed_fqdn,
    }

    # 3. Interface-pinned /32 routes to each neighbour through the GNE OAMP.
    routes_ok, added, err = prepare_neighbor_host_routes(
        route_targets, seed_ip, interface_index=iface)
    st['routes'] = added
    if not routes_ok:
        debug_log(f'OAMP-LAN: /32 route add failed: {err}', '_setup_oamp_lan')
        _oamp_lan_teardown(st, username, password)
        return None
    return st


def run_part2_remote(local_ip, neighbor_ip, username, password):
    """
    SSH directly from the audit PC into an OSC neighbor after the local shelf
    has temporarily advertised its CIT subnet, and run PART 2 commands:
      - alm            : remote alarm snapshot
      - tools ping     : ping local IP from remote to verify OSC reachability
      - show interface <OSC-card> <slot>/OscSfp[1|2] opr/opt
    Returns dict of parsed results, or None if connection fails.
    Uses the same two-stage login sequence and user-supplied credentials as
    the current shelf.
    """
    child, prompt = ssh_connect(neighbor_ip, username, password)
    if not child:
        debug_log(
            f'Could not connect directly to neighbor {neighbor_ip} after CIT route setup',
            'run_part2_remote',
        )
        return None

    try:
        card_raw = run_command(child, prompt, 'show card inventory *')
        nb_site, nb_amp_cards, nb_omdwb = detect_site(card_raw)

        alm_raw  = run_command(child, prompt, 'alm')
        ping_raw = run_command(child, prompt, f'tools ping {local_ip}', timeout=30)
        osc_output = {
            'osc_status': run_command(child, prompt, 'show cn osc *'),
        }
        for label, command in build_osc_power_commands(
            nb_site,
            nb_amp_cards,
            nb_omdwb,
            has_sfp2=(nb_site == 'ILA'),
        ):
            osc_output[label] = run_command(child, prompt, command)
            debug_log(
                f'Got {len(osc_output[label])} chars',
                f'remote_{label}',
            )
    finally:
        ssh_close(child)

    ping_ok, ping_lat    = parse_ping(ping_raw)
    osc                  = select_osc_power(
        osc_output,
        nb_site,
        nb_amp_cards,
        nb_omdwb,
        has_sfp2=(nb_site == 'ILA'),
    )
    alarm_counts, alarms = parse_alarms(alm_raw)

    return {
        'neighbor_ip':  neighbor_ip,
        'nb_site_type': nb_site,
        'nb_omdwb':     nb_omdwb,
        'ping_ok':      ping_ok,
        'ping_latency': ping_lat,
        'sfp1_opr':     osc['sfp1_opr'],
        'sfp1_opt':     osc['sfp1_opt'],
        'sfp2_opr':     osc['sfp2_opr'],
        'sfp2_opt':     osc['sfp2_opt'],
        'alarm_counts': alarm_counts,
        'alarm_list':   alarms,
    }



def _xlcol(n):
    """Convert 0-based column index to Excel letter(s)."""
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def draw_border(ws, row, col_start, col_end, fmt_border):
    """Write a thin border across a row range."""
    for c in range(col_start, col_end + 1):
        ws.write(row, c, None, fmt_border)


def setup_workbook(filename):
    """
    Create the audit workbook using the supplied RLS audit visual style.
    Returns (wb, formats_dict).
    """
    wb = xlsxwriter.Workbook(filename, {'strings_to_numbers': False})
    wb.set_properties({
        'title': 'Nokia 1830 PSI Optical Audit',
        'subject': 'Transport network audit',
        'author': 'LightRiver Technologies',
        'company': 'LightRiver Technologies',
        'comments': f'Generated by {__program__} v{__version__}',
    })
    f = {}

    def _fmt(bold=False, bg=None, font_color=None, align='left',
             border=0, italic=False, font_size=11, wrap=False, num_format=None,
             underline=False):
        props = {
            'font_name':  'Calibri',
            'font_size':  font_size,
            'align':      align,
            'valign':     'vcenter',
            'bold':       bold,
            'italic':     italic,
            'border':     border,
            'text_wrap':  wrap,
        }
        if bg:
            props['bg_color'] = bg
        if font_color:
            props['font_color'] = font_color
        if underline:
            props['underline'] = 1
        if num_format:
            props['num_format'] = num_format
        return wb.add_format(props)

    f['hdr']        = _fmt(bold=True, bg=HDR_FILL, font_color=HDR_FONT,
                           align='center', wrap=True)
    f['hdr_left']   = _fmt(bold=True, bg=HDR_FILL, font_color=HDR_FONT,
                           align='left', wrap=True)
    f['odd']        = _fmt(bg=FILL_ODD)
    f['even']       = _fmt(bg=FILL_EVEN)
    f['odd_c']      = _fmt(bg=FILL_ODD, align='center')
    f['even_c']     = _fmt(bg=FILL_EVEN, align='center')
    f['site_a']     = _fmt(bg=SITE_FILL_A)
    f['site_b']     = _fmt(bg=SITE_FILL_B)
    f['good']       = _fmt(bg=GREEN_FILL, font_color=GREEN_FONT,
                           align='center')
    f['warn']       = _fmt(bg=AMBER_FILL, font_color=AMBER_FONT,
                           align='center')
    f['crit']       = _fmt(bg=RED_FILL, font_color=RED_FONT, align='center')
    f['bold_left']  = _fmt(bold=True, border=1)
    f['title']      = _fmt(bold=True, font_size=14)
    f['timestamp']  = _fmt(font_color=LINK_BLUE, underline=True)
    f['section']    = _fmt(bold=True, bg=HDR_FILL, border=1)
    f['alarm_cr']   = _fmt(bold=True, bg=FILL_ODD, font_color=RED_FILL)
    f['alarm_mj']   = _fmt(bold=True, bg=FILL_ODD, font_color='#BF9000')
    f['alarm_mn']   = _fmt(bg=FILL_ODD, font_color='#C65911')
    f['index_hdr']  = _fmt(bold=True, bg=HDR_FILL, border=1)
    f['index_desc'] = _fmt(bg='#FFFFFF', border=1, wrap=True)
    f['index_link'] = wb.add_format({
        'font_name': 'Calibri',
        'font_size': 11,
        'bold': True,
        'font_color': LINK_BLUE,
        'bg_color': '#FFFFFF',
        'border': 1,
        'valign': 'vcenter',
    })
    return wb, f


def _row_fmt(f, row_idx):
    """Return (odd_fmt, even_fmt) based on row index."""
    return (f['odd'], f['even']) if row_idx % 2 == 0 else (f['even'], f['odd'])


def write_header_row(ws, headers, fmt, col_widths=None):
    """Write a single header row with uniform format; optionally set column widths."""
    for c, h in enumerate(headers):
        ws.write(HEADER_ROW, c, h, fmt)
        if col_widths and c < len(col_widths):
            ws.set_column(c, c, col_widths[c])


def prepare_data_sheet(ws, sheet_def, formats, audit_timestamp):
    """Apply the shared timestamp, header, sizing, and view settings."""
    ws.hide_gridlines(2)
    ws.freeze_panes(DATA_START_ROW, 0)
    ws.set_zoom(90)
    ws.set_default_row(18)
    ws.set_row(TIMESTAMP_ROW, 19)
    ws.set_row(1, 8)
    ws.set_row(HEADER_ROW, 32)
    # The timestamp cell doubles as a hyperlink back to the Index tab.
    ws.write_url(
        TIMESTAMP_ROW, 0, "internal:'Index'!A1",
        formats['timestamp'], audit_timestamp,
    )
    write_header_row(
        ws,
        sheet_def['headers'],
        formats['hdr'],
        sheet_def['widths'],
    )


def finalize_data_sheet(ws, sheet_def, formats, next_row, table_name):
    """Finish a sheet as a filterable Excel table without changing its colors."""
    last_col = len(sheet_def['headers']) - 1
    last_row = next_row - 1
    if last_row < DATA_START_ROW:
        last_row = DATA_START_ROW
        for col in range(last_col + 1):
            ws.write_blank(last_row, col, None, formats['odd'])

    columns = [
        {'header': header, 'header_format': formats['hdr']}
        for header in sheet_def['headers']
    ]
    ws.add_table(
        HEADER_ROW,
        0,
        last_row,
        last_col,
        {
            'name': table_name,
            'style': None,
            'autofilter': True,
            'columns': columns,
        },
    )


# Sheet header / column definitions ──────────────────────────────────────────

SHEET_SUMMARY = {
    'name': 'Summary',
    'headers': ['Node', 'Short Name', 'FQDN', 'Site Type', 'SW Version',
                'SW Status', 'Asset Tag', 'Loopback IP', 'OSClan IP',
                'DNS Capable', 'Alarms CR', 'Alarms MJ', 'Alarms MN',
                'Alarms WN', 'Power Health', 'Audit Date'],
    'widths':  [18, 14, 35, 10, 20, 12, 14, 16, 16, 12, 10, 10, 10, 10, 42, 14],
}

SHEET_DISCOVERY = {
    'name': 'Scan Verification',
    'headers': [
        'NE Name', 'Network Map IP', 'Connection Target', 'Map SW Release',
        'Discovery Method', 'Discovered From', 'Queue Depth', 'Scan Status',
        'Actual SW Release', 'Site Type', 'Neighbor IPs', 'Neighbors Found',
        'Neighbors Scanned', 'Neighbor Verification', 'Reciprocal Links',
        'Notes', 'Line', 'Line Position',
    ],
    'widths': [
        20, 17, 20, 20, 20, 20, 12, 18,
        20, 12, 42, 17, 18, 24, 18, 42,
        8, 14,
    ],
}

SHEET_CARDS = {
    'name': 'Card Inventory',
    'headers': ['Node', 'Card Type', 'Slot', 'Serial Number', 'CLEI'],
    'widths':  [18, 12, 8, 18, 16],
}

SHEET_IFACE = {
    'name': 'Interface Inventory',
    'headers': ['Node', 'Interface', 'Port Type', 'Serial Number', 'Manufacturer'],
    'widths':  [18, 24, 16, 18, 18],
}

SHEET_OSPF = {
    'name': 'OSPF',
    'headers': ['Node', 'Router ID', 'Area ID', 'Opaque LSA Capability', 'DNS Capable'],
    'widths':  [18, 16, 16, 30, 12],
}

SHEET_MGMT = {
    'name': 'Management',
    'headers': ['Node', 'OSClan IP', 'OSClan Mask', 'OSClan Admin',
                'OSClan Oper', 'Loopback IP'],
    'widths':  [18, 16, 14, 12, 12, 16],
}

SHEET_POWER = {
    'name': 'Power',
    'headers': ['Node', 'Shelf Type', 'Power Type', 'Total Current (A)', 'Total Power (W)'],
    'widths':  [18, 14, 10, 18, 16],
}

SHEET_PF = {
    'name': 'Power Filters',
    'headers': ['Node', 'PF Slot', 'Admin State', 'Oper State', 'State Qualifier'],
    'widths':  [18, 10, 12, 12, 16],
}

SHEET_AMP = {
    'name': 'Amplifiers',
    'headers': ['Node', 'Card Type', 'Slot', 'Port',
                'Target Gain (dB)', 'Current Gain (dB)',
                'Gain Range', 'Gain In Spec?', 'Actual Tilt (dB)',
                'Total Input Pwr (dBm)', 'Total Output Pwr (dBm)',
                'Signal Output Pwr (dBm)', 'AGC State'],
    'widths':  [18, 12, 8, 12, 16, 16, 11, 13, 16, 20, 20, 22, 12],
}

SHEET_ALARMS = {
    'name': 'Alarms',
    'headers': ['Node', 'Severity', 'SA', 'Date', 'Time', 'Layer',
                'Condition', 'Interface', 'Direction', 'Description', 'Card'],
    'widths':  [28, 8, 5, 10, 10, 8, 28, 24, 12, 45, 14],
}

SHEET_REMOTE = {
    'name': 'OSC Power',
    'headers': [
        'Node', 'Site Type', 'Neighbor IP', 'Neighbor Site',
        'Ping OK', 'Ping Latency (ms)',
        'Local SFP1 OPT (dBm)', 'Local SFP2 OPR (dBm)',
        'Remote SFP1 OPR (dBm)', 'Remote SFP1 OPT (dBm)',
        'Remote SFP2 OPR (dBm)', 'Remote SFP2 OPT (dBm)',
        'Span A\u2192B (dB)', 'Span B\u2192A (dB)', 'Delta (dB)', 'Flag',
    ],
    'widths': [20, 10, 16, 12, 8, 16, 18, 18, 20, 20, 20, 20, 14, 14, 11, 20],
}

SHEET_OSC_INTERNAL = {
    'name': 'OSC Internal',
    'headers': [
        'Node', 'Site Type', 'OSC Line', 'Cards Reporting',
        'Min Rx (dBm)', 'Max Rx (dBm)', 'Rx Delta (dB)', 'Flag', 'Readings',
    ],
    'widths': [20, 10, 10, 15, 14, 14, 14, 22, 60],
}

SHEET_INTERNAL_FIBERS = {
    'name': 'Internal Fibers',
    'headers': [
        'Node', 'Source Port', 'Source Card', 'Out Power (dBm)',
        'Dest Port', 'Dest Card', 'In Power (dBm)', 'Band',
        'Patch Loss (dB)', 'Flag',
    ],
    'widths': [20, 16, 12, 16, 16, 12, 16, 8, 15, 22],
}


SHEET_SPECS = [
    ('sum', SHEET_SUMMARY, 'TblSummary',
     'One row per audited node: identity, software, addressing, and alarm totals.'),
    ('discovery', SHEET_DISCOVERY, 'TblScanVerification',
     'Network-map queue, full-scan status, topology neighbor coverage, and reciprocal-link verification.'),
    ('cards', SHEET_CARDS, 'TblCardInventory',
     'Installed hardware by node and slot, including serial and CLEI identifiers.'),
    ('iface', SHEET_IFACE, 'TblInterfaceInventory',
     'Pluggable interface inventory, port type, serial, CLEI, and manufacturer.'),
    ('ospf', SHEET_OSPF, 'TblOSPF',
     'OSPF router ID, area, opaque-LSA capability, and DNS capability.'),
    ('mgmt', SHEET_MGMT, 'TblManagement',
     'OSCLAN and loopback addressing plus administrative and operational state.'),
    ('power', SHEET_POWER, 'TblPower',
     'Shelf power type, total current, and total power consumption.'),
    ('pf', SHEET_PF, 'TblPowerFilters',
     'Power-filter slot inventory and administrative/operational state.'),
    ('remote', SHEET_REMOTE, 'TblOSCPower',
     'OSC power per span: local and remote SFP TX/RX, neighbor reachability, '
     'bidirectional span loss, and delta flag.'),
    ('osc_internal', SHEET_OSC_INTERNAL, 'TblOSCInternal',
     'Internal OSC Rx delta per line: the OSC received (OPR) power each card '
     'reports for the line, and the dB spread between them. A spread above '
     'tolerance flags a suspect internal OSC patch; a single reading means '
     'only one card sees that OSC.'),
    ('int_fiber', SHEET_INTERNAL_FIBERS, 'TblInternalFibers',
     'Internal (intra-shelf) fibers from show interface topology Int rows: the '
     'LINE-OUT launched power vs the paired LINE-IN received power, and the '
     'patch loss between them (flagged above tolerance).'),
    ('amp', SHEET_AMP, 'TblAmplifiers',
     'Amplifier inventory, gain and tilt, optical power, and AGC state by port.'),
    ('alarms', SHEET_ALARMS, 'TblAlarms',
     'Active local and remote alarms with severity, resource, and description.'),
]


def write_index_sheet(ws, formats, audit_timestamp):
    """Create the front-page worksheet index in the supplied audit style."""
    ws.hide_gridlines(2)
    ws.set_zoom(100)
    ws.set_column(0, 0, 24)
    ws.set_column(1, 1, 95)
    ws.set_row(TIMESTAMP_ROW, 19)
    ws.set_row(1, 24)
    ws.set_row(HEADER_ROW, 22)
    ws.write_url(
        TIMESTAMP_ROW, 0, "internal:'Index'!A1",
        formats['timestamp'], audit_timestamp,
    )
    ws.write(1, 0, 'Nokia 1830 PSI Optical Audit', formats['title'])
    ws.write(HEADER_ROW, 0, 'Tab', formats['index_hdr'])
    ws.write(HEADER_ROW, 1, 'Description', formats['index_hdr'])

    for offset, (_, sheet_def, _, description) in enumerate(SHEET_SPECS):
        row = DATA_START_ROW + offset
        sheet_name = sheet_def['name']
        ws.set_row(row, 31)
        ws.write_url(
            row,
            0,
            f"internal:'{sheet_name}'!A1",
            formats['index_link'],
            sheet_name,
        )
        ws.write(row, 1, description, formats['index_desc'])

    ws.add_table(
        HEADER_ROW,
        0,
        DATA_START_ROW + len(SHEET_SPECS) - 1,
        1,
        {
            'name': 'TblIndex',
            'style': None,
            'autofilter': False,
            'columns': [
                {'header': 'Tab', 'header_format': formats['index_hdr']},
                {'header': 'Description', 'header_format': formats['index_hdr']},
            ],
        },
    )


def write_discovery_verification(ws, next_row, formats, tracker,
                                 ordered_records, line_meta):
    """Write final queue, scan, and topology cross-check results.

    Rows are emitted in physical (Terminal→Terminal) order — the same order as
    the rest of the document — followed by any sites that were not scanned.
    """
    verified_count = 0
    incomplete_count = 0

    ordered_ids = {id(r) for r in ordered_records}
    display = list(ordered_records) + [
        r for r in tracker.records if id(r) not in ordered_ids
    ]
    for record in display:
        neighbor_ips = sorted(record['neighbors'])
        scanned_neighbors = [
            tracker.find_scanned(neighbor_ip)
            for neighbor_ip in neighbor_ips
        ]
        scanned_count = sum(item is not None for item in scanned_neighbors)

        if record['status'] != 'Scanned':
            neighbor_verification = 'Not checked'
            reciprocal_text = '--'
        elif not neighbor_ips:
            neighbor_verification = 'No external neighbors'
            reciprocal_text = 'N/A'
        elif scanned_count == len(neighbor_ips):
            reciprocal = 0
            local_aliases = {
                _discovery_alias(record['actual_ip']),
                _discovery_alias(record['ne_ip']),
            }
            local_aliases.discard('')
            for neighbor_record in scanned_neighbors:
                reverse_aliases = {
                    _discovery_alias(ip)
                    for ip in neighbor_record['neighbors']
                }
                if local_aliases & reverse_aliases:
                    reciprocal += 1
            reciprocal_text = f'{reciprocal}/{len(neighbor_ips)}'
            if reciprocal == len(neighbor_ips):
                neighbor_verification = 'Verified'
                verified_count += 1
            else:
                neighbor_verification = (
                    f'CHECK ({reciprocal}/{len(neighbor_ips)} reciprocal)'
                )
                incomplete_count += 1
        else:
            neighbor_verification = (
                f'INCOMPLETE ({scanned_count}/{len(neighbor_ips)} scanned)'
            )
            reciprocal_text = '--'
            incomplete_count += 1

        row = next_row
        # One row per discovered site here, so alternate the site bands by row.
        body = (formats['site_a'] if (row - DATA_START_ROW) % 2 == 0
                else formats['site_b'])
        status_format = (
            formats['good'] if record['status'] == 'Scanned'
            else formats['warn'] if record['status'].startswith('Skipped')
            else formats['crit']
        )
        verify_format = (
            formats['good'] if neighbor_verification == 'Verified'
            else formats['crit'] if neighbor_verification.startswith('INCOMPLETE')
            else formats['warn'] if neighbor_verification.startswith('CHECK')
            else formats['warn'] if neighbor_verification == 'No external neighbors'
            else body
        )
        values = [
            record['ne_name'],
            record['ne_ip'],
            record['target'],
            record['map_sw'],
            record['method'],
            record['source'],
            record['depth'],
            record['status'],
            record['actual_sw'],
            record['site_type'],
            ', '.join(neighbor_ips) if neighbor_ips else '--',
            len(neighbor_ips),
            scanned_count,
            neighbor_verification,
            reciprocal_text,
            record['notes'] or '--',
            (f'L{line_meta[id(record)][0]}'
             if id(record) in line_meta else '--'),
            (f'{line_meta[id(record)][1]} of {line_meta[id(record)][2]}'
             if id(record) in line_meta else '--'),
        ]
        for col, value in enumerate(values):
            cell_format = status_format if col == 7 else verify_format if col == 13 else body
            ws.write(row, col, value, cell_format)
        next_row += 1

    return next_row, verified_count, incomplete_count


def compute_site_lines(tracker):
    """Order scanned sites into physical lines by walking the topology neighbor
    chain from one Terminal to the other.

    An optical line runs Terminal → ILA → … → ILA → Terminal. Starting at a
    Terminal (a degree-1 endpoint) and always stepping to the neighbor we did
    not just come from reconstructs that physical sequence and doubles as a
    network-map check: a walk that ends anywhere other than a Terminal means a
    link is missing or a neighbor was unreachable.

    Returns ``(lines, line_meta)`` where ``lines`` is a list of record-lists —
    each one Terminal→Terminal walk — and ``line_meta`` maps ``id(record)`` to
    ``(line_number, position, line_length)``.
    """
    scanned = [r for r in tracker.records if r.get('_render')]

    def resolve(ip):
        return tracker.find_scanned(ip) or tracker.find_known(ip)

    idset = {id(r) for r in scanned}
    adj = {}
    for r in scanned:
        neigh, seen = [], set()
        for nbip in sorted(r['neighbors']):
            n = resolve(nbip)
            if (n is not None and id(n) in idset
                    and id(n) != id(r) and id(n) not in seen):
                seen.add(id(n))
                neigh.append(n)
        adj[id(r)] = neigh

    visited = set()

    def walk(start):
        chain, prev, cur = [], None, start
        while cur is not None and id(cur) not in visited:
            visited.add(id(cur))
            chain.append(cur)
            nxt = None
            for n in adj[id(cur)]:
                if id(n) not in visited and (prev is None or id(n) != id(prev)):
                    nxt = n
                    break
            prev, cur = cur, nxt
        return chain

    def is_terminal(r):
        return r['_render']['site_type'] == 'TERMINAL' or len(adj[id(r)]) <= 1

    lines = []
    # Prefer starting each walk at a true Terminal, then by name, so the order
    # is stable and repeatable across runs.
    starts = sorted(
        (r for r in scanned if is_terminal(r)),
        key=lambda r: (r['_render']['site_type'] != 'TERMINAL', r['ne_name']),
    )
    for s in starts:
        if id(s) not in visited:
            chain = walk(s)
            if chain:
                lines.append(chain)
    # Rings / all-ILA segments with no Terminal endpoint: walk from the
    # lowest-named remaining node so every scanned site still appears, grouped.
    for r in sorted(scanned, key=lambda r: r['ne_name']):
        if id(r) not in visited:
            chain = walk(r)
            if chain:
                lines.append(chain)

    line_meta = {}
    for i, chain in enumerate(lines, 1):
        for pos, rec in enumerate(chain, 1):
            line_meta[id(rec)] = (i, pos, len(chain))
    return lines, line_meta


# ══════════════════════════════════════════════════════════════════════════════
# Main processing
# ══════════════════════════════════════════════════════════════════════════════
def main():
    _session_open(options.outfile)
    wb, f = setup_workbook(options.outfile)
    audit_started = datetime.now()
    audit_timestamp = (
        f'{audit_started:%b} {audit_started.day} '
        f'{audit_started:%Y %H:%M}'
    )

    # Create the Index first, then all data sheets in its displayed order.
    ws_index = wb.add_worksheet('Index')
    worksheets = {}
    for key, sheet_def, _, _ in SHEET_SPECS:
        worksheets[key] = wb.add_worksheet(sheet_def['name'])
        prepare_data_sheet(
            worksheets[key],
            sheet_def,
            f,
            audit_timestamp,
        )
    write_index_sheet(ws_index, f, audit_timestamp)

    ws_sum       = worksheets['sum']
    ws_discovery = worksheets['discovery']
    ws_cards     = worksheets['cards']
    ws_iface     = worksheets['iface']
    ws_ospf      = worksheets['ospf']
    ws_mgmt      = worksheets['mgmt']
    ws_power     = worksheets['power']
    ws_pf        = worksheets['pf']
    ws_amp       = worksheets['amp']
    ws_alarms    = worksheets['alarms']
    ws_remote    = worksheets['remote']
    ws_osc_internal = worksheets['osc_internal']
    ws_int_fiber = worksheets['int_fiber']

    # Row counters begin below the timestamp, spacer, and header rows.
    rows = {key: DATA_START_ROW for key, _, _, _ in SHEET_SPECS}

    # ── Seed discovery: build the initial queue from the first site's map ────
    tracker = DiscoveryTracker()
    seed_target = hostlist[0]
    _, discovery_seed_fqdn = format_hostname(seed_target)

    # Decide up front whether the CIT craft-port routing hack applies. It is
    # gated to the local craft seed (CIT_CRAFT_SEED_IP); a routed/remote seed
    # reaches the discovered shelves directly over normal network routing and
    # must NOT touch the seed's CIT OSPF state or add Windows /32 routes (see
    # CIT_CRAFT_SEED_IP). Compare both the raw seed input and its resolved
    # address so a hostname mapping to the craft IP is still recognised. All
    # logins (seed + neighbors) use the Telnet getty -- it reaches both the
    # local craft port and routed/remote interfaces.
    try:
        _resolved_seed_ip = socket.gethostbyname(discovery_seed_fqdn)
    except socket.gaierror:
        _resolved_seed_ip = seed_target
    use_cit_routing = CIT_CRAFT_SEED_IP in (seed_target, _resolved_seed_ip)
    seed_name = '--'
    seed_map = []

    if use_cit_routing:
        # Craft-port seed (172.16.0.1): read the full network map over the
        # telnet getty so every mapped site is queued (and route-pinned) up
        # front. Telnet is exclusive to this address.
        print(
            f'Discovery: reading network map from first site '
            f'{bold}{seed_target}{resetc} ...',
            end=' ',
            flush=True,
        )
        seed_child, seed_prompt = ssh_connect(
            discovery_seed_fqdn,
            options.user,
            options.password,
        )
        if seed_child:
            try:
                seed_name = prompt_node_name(seed_prompt)
                seed_map = parse_networkmap(
                    run_command(seed_child, seed_prompt, 'show cn networkmap')
                )
            finally:
                ssh_close(seed_child)
            print(f'{green}OK — {len(seed_map)} sites found{resetc}')
        else:
            print(f'{amber}FAILED — will retry during the first full scan{resetc}')
    else:
        # Routed seed: NETCONF is the collector and telnet is reserved for the
        # 172.16.0.1 craft port, so there is no CLI network-map preflight. The
        # NETCONF neighbour walk (connections external-peer-address, hop by hop
        # from the seed) discovers the line instead.
        print(
            f'Discovery: routed seed {bold}{seed_target}{resetc} — '
            f'walking neighbours over NETCONF (no CLI network-map preflight).'
        )

    seed_map_by_name = {
        item['ne_name'].lower(): item
        for item in seed_map
    }
    seed_map_by_ip = {
        item['ne_ip']: item
        for item in seed_map
    }
    seed_entry = seed_map_by_name.get(seed_name.lower())
    seed_record, _ = tracker.enqueue(
        seed_target,
        ne_name=seed_name,
        ne_ip=seed_entry['ne_ip'] if seed_entry else '--',
        map_sw=seed_entry['sw_version'] if seed_entry else '--',
        method='Input seed',
        source='User',
        depth=0,
        is_input=True,
    )

    for extra_seed in hostlist[1:]:
        tracker.enqueue(
            extra_seed,
            method='Input seed',
            source='User',
            depth=0,
            is_input=True,
        )

    for item in seed_map:
        tracker.seed_map_ips.add(item['ne_ip'])
        tracker.enqueue(
            item['ne_ip'],
            ne_name=item['ne_name'],
            ne_ip=item['ne_ip'],
            map_sw=item['sw_version'],
            method='Seed network map',
            source=seed_name,
            depth=1,
        )

    discovery_gateway = None
    discovery_routes_added = []
    discovery_restore_state = None
    discovery_transport_ready = False
    discovery_transport_attempted = False
    discovery_transport_error = ''
    discovery_cleanup_registered = False
    discovery_temp_ip = None       # temp secondary IP multi-net'd onto the OAMP subnet
    discovery_temp_iface = None    # its Windows interface index (route pin + cleanup)

    # Prefer a WIRED / on-link NIC to reach the gear over Wi-Fi (so the operator
    # need not disable Wi-Fi). Routed seeds only; bound onto every NETCONF
    # connection. None here just means no wired OAMP IP is preset -- OAMP-LAN
    # discovery will try to SET one on a connected wired NIC (and remove it after).
    discovery_source_ip = None
    if not use_cit_routing:
        discovery_source_ip = _preferred_source_ip(
            _resolved_seed_ip or seed_target, 830)
        if discovery_source_ip:
            print(f'Reaching the gear from {bold}{discovery_source_ip}{resetc} '
                  f'(preferring a wired/on-link NIC over Wi-Fi).')
        else:
            print('Seed reached over the default route (Wi-Fi) for now; if a wired '
                  'NIC is on the OAMP LAN, discovery will set a temporary '
                  '172.21.109.x on it for the full walk and remove it afterward.')

    # ── Concurrency scaffolding (parallel node audit) ────────────────────────
    # The audit of each site is independent network work. Workers touch no
    # workbook state during the scan — every row is buffered on its record and
    # written after the pool joins — so the only shared resources needing a lock
    # are the Windows/CIT routing state and the console. route_lock is an RLock
    # so _drain_lock() can safely release a hold a worker leaked on an error.
    route_lock = threading.RLock()    # serialize Windows route + CIT changes
    print_lock = threading.Lock()     # keep each node's console output intact

    _progress = {'n': 0}
    _progress_lock = threading.Lock()

    def next_index():
        with _progress_lock:
            _progress['n'] += 1
            return _progress['n'], len(tracker.records)

    def _drain_lock(lock):
        # Safety net: if an error interrupted a thread mid-write, release any
        # holds it leaked. release() on a lock this thread does not hold raises
        # RuntimeError, which ends the drain.
        while True:
            try:
                lock.release()
            except RuntimeError:
                break

    def _make_logger(live):
        # Each node's worker buffers its console output and prints it as one
        # block (under print_lock) so parallel nodes don't interleave lines.
        # The sequential seed uses live=True for real-time progress feedback.
        buf = []
        cur = {'s': ''}

        def emit(*args, end='\n', flush=False):
            msg = ' '.join(str(a) for a in args)
            if end == ' ':
                cur['s'] += msg + ' '
                return
            line = cur['s'] + msg
            cur['s'] = ''
            if live:
                with print_lock:
                    print(line)
            else:
                buf.append(line)

        def flush():
            if cur['s']:
                if live:
                    with print_lock:
                        print(cur['s'])
                else:
                    buf.append(cur['s'])
                cur['s'] = ''
            if not live and buf:
                with print_lock:
                    print('\n'.join(buf))
                buf.clear()

        return emit, flush

    _band_state = {}

    def band_fmt(key, label):
        # Per-site banding: every row belonging to one site shares a fill; the
        # next site flips to the other fill (band A = peach, band B = blue), so
        # each site's block of line items reads as one colour. Called only from
        # the single-threaded render pass after the scan, walking sites in line
        # order, so the colour flips exactly at each site boundary.
        st = _band_state.setdefault(key, {'last': None, 'parity': 1})
        if label != st['last']:
            st['last'] = label
            st['parity'] ^= 1
        return f['site_a'] if st['parity'] == 0 else f['site_b']

    def _audit_body(discovery_record, emit):
        nonlocal discovery_transport_ready, discovery_transport_attempted
        nonlocal discovery_transport_error, discovery_gateway
        nonlocal discovery_restore_state, discovery_cleanup_registered
        nonlocal discovery_temp_ip, discovery_temp_iface
        print = emit
        processed_count, total = next_index()

        if discovery_record['status'] == 'Blocked':
            print(
                f'[{processed_count}/{total}] '
                f'{discovery_record["ne_name"]} '
                f'({discovery_record["target"]}) : '
                f'{red}BLOCKED — {discovery_record["notes"]}{resetc}'
            )
            return

        duplicate = tracker.already_scanned(discovery_record)
        if duplicate:
            tracker.mark_failed(
                discovery_record,
                'Skipped duplicate',
                f'Already fully scanned as {duplicate["ne_name"]}',
            )
            print(
                f'[{processed_count}/{total}] '
                f'{discovery_record["target"]}: '
                f'{amber}already scanned — skipping duplicate{resetc}'
            )
            return

        if (
            discovery_transport_error
            and not discovery_record['is_input']
        ):
            tracker.mark_failed(
                discovery_record,
                'Blocked',
                discovery_transport_error,
            )
            print(
                f'[{processed_count}/{total}] '
                f'{discovery_record["ne_name"]} '
                f'({discovery_record["target"]}) : '
                f'{red}BLOCKED — {discovery_transport_error}{resetc}'
            )
            return

        connect_target = discovery_record['target']
        short, fqdn = format_hostname(connect_target)

        # ── DNS pre-flight (skip node cleanly if not resolvable) ─────────────
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', fqdn):
            try:
                resolved_target = socket.gethostbyname(fqdn)
            except socket.gaierror:
                tracker.mark_failed(
                    discovery_record,
                    'Failed',
                    'Not found in DNS',
                )
                if discovery_record is seed_record:
                    discovery_transport_error = (
                        'Seed shelf was not reachable in DNS; '
                        'OSC discovery routing was not prepared'
                    )
                print(f'[{processed_count}/{total}] {bold}{short}{resetc} ({fqdn}) : {red}Not found in DNS — skipping{resetc}')
                return
        else:
            resolved_target = fqdn

        tracker.add_aliases(discovery_record, resolved_target)
        duplicate = tracker.already_scanned(discovery_record)
        if duplicate:
            tracker.mark_failed(
                discovery_record,
                'Skipped duplicate',
                f'Resolved to site already scanned as {duplicate["ne_name"]}',
            )
            print(
                f'[{processed_count}/{total}] {fqdn}: '
                f'{amber}resolved to an already scanned site — skipping{resetc}'
            )
            return

        display_name = (
            discovery_record['ne_name']
            if discovery_record['ne_name'] != '--'
            else short
        )
        print(f'[{processed_count}/{total}] Connecting to {bold}{display_name}{resetc} ({fqdn}) ...', end=' ', flush=True)

        # The telnet getty workflow is exclusive to the 172.16.0.1 craft port.
        # Every other address is collected over NETCONF (the primary collector);
        # hand the node off to the NETCONF path and skip the CLI body entirely.
        if not use_cit_routing:
            _netconf_audit_node(discovery_record, emit, processed_count, total,
                                fqdn, resolved_target, display_name)
            return

        child, prompt = ssh_connect(fqdn, options.user, options.password)
        if not child:
            print(f'{red}FAILED{resetc}')
            tracker.mark_failed(
                discovery_record,
                'Failed',
                'SSH login failed',
            )
            if discovery_record is seed_record:
                discovery_transport_error = (
                    'Seed shelf SSH login failed; '
                    'OSC discovery routing was not prepared'
                )
            return
        print(f'{green}OK{resetc}')

        actual_name = prompt_node_name(prompt)
        if actual_name != '--':
            discovery_record['ne_name'] = actual_name
        # Atomically reserve this shelf's real identity. In parallel, two
        # queue entries reaching the same shelf by different addresses both
        # resolve to the same CLI hostname here; claim() lets only the first
        # proceed and hands the loser the winning record.
        duplicate = tracker.claim(
            discovery_record,
            actual_name,
            fqdn,
            resolved_target,
        )
        if duplicate:
            ssh_close(child)
            tracker.mark_failed(
                discovery_record,
                'Skipped duplicate',
                f'CLI identity matches already scanned site '
                f'{duplicate["ne_name"]}',
            )
            print(
                f'       {amber}CLI identity already scanned as '
                f'{duplicate["ne_name"]} — skipping full audit{resetc}'
            )
            return
        host = actual_name if actual_name != '--' else display_name
        short = host

        try:
            # Step 1: card inventory to detect site type
            card_raw    = run_command(child, prompt, 'show card inventory *')
            site_type, amp_cards, omdwb_slot = detect_site(card_raw)
            print(f'       Site type: {bold}{site_type}{resetc}  Amps: {list(amp_cards.keys())}  OMDWB: {omdwb_slot}')

            # Step 2: interface inventory (to detect SFP2)
            iface_raw   = run_command(child, prompt, 'show interface inventory *')
            has_sfp2    = bool(re.search(r'\bSFP2\b', iface_raw, re.IGNORECASE))

            # Step 3: build full command list
            cmds = build_commands(site_type, amp_cards, omdwb_slot, has_sfp2)

            # Step 4: run all remaining commands (card_inventory + iface_inventory already done)
            output = {
                'card_inventory':  card_raw,
                'iface_inventory': iface_raw,
            }
            done_labels = {'card_inventory', 'iface_inventory'}
            for label, cmd in cmds:
                if label in done_labels:
                    continue
                output[label] = run_command(child, prompt, cmd)
                debug_log(f'Got {len(output[label])} chars', label)

        finally:
            ssh_close(child)

        # ── Parse ───────────────────────────────────────────────────────────
        audit_date = datetime.now().strftime('%Y-%m-%d')

        sw_ver   = parse_software(output.get('sw_version', ''))
        sw_stat  = 'Approved' if sw_ver in APPROVED_SW else 'NOT Approved'

        cards    = parse_card_inventory(output.get('card_inventory', ''))
        ifaces   = parse_interface_inventory(output.get('iface_inventory', ''))
        ospf     = parse_ospf(output.get('ospf', ''))
        osclan   = parse_osclan(output.get('osclan', ''))
        loopback = parse_loopback(output.get('loopback', ''))
        shelf    = parse_shelf(output.get('shelf', ''))
        # Nokia PSI Router ID == Loopback IP (strip /prefix if present)
        if ospf['router_id'] == '--' and loopback.get('ip_addr', '--') != '--':
            ospf['router_id'] = loopback['ip_addr'].split('/')[0]

        osc_pwr = select_osc_power(
            output,
            site_type,
            amp_cards,
            omdwb_slot,
            has_sfp2,
        )

        alarm_counts, alarm_list = parse_alarms(output.get('alarms', ''))
        nmap  = parse_networkmap(output.get('networkmap', ''))
        topo  = parse_topology(output.get('topology', ''))
        asset_serial = next(
            (c['serial'] for c in cards if c.get('serial', '--') != '--'), '--'
        )
        asset_tag = adb_asset(asset_serial) if asset_serial != '--' else '--'

        # ── Discovery bookkeeping and secondary topology verification ───────
        actual_ip = (
            loopback['ip_addr'].split('/')[0]
            if loopback.get('ip_addr', '--') != '--'
            else discovery_record['ne_ip']
            if discovery_record['ne_ip'] != '--'
            else resolved_target
        )
        discovery_record['actual_ip'] = actual_ip
        discovery_record['actual_sw'] = sw_ver
        discovery_record['site_type'] = site_type
        tracker.add_aliases(
            discovery_record,
            host,
            actual_ip,
            loopback.get('ip_addr'),
        )

        if (
            discovery_record['map_sw'] != '--'
            and sw_ver != '--'
            and discovery_record['map_sw'] != sw_ver
        ):
            discovery_record['notes'] = (
                f'Network map SW {discovery_record["map_sw"]} '
                f'differs from scanned SW {sw_ver}'
            )

        # The first fully scanned site is authoritative for the initial queue.
        # Merge its fresh result in case the lightweight preflight was empty or
        # the network map changed between the two reads.
        if discovery_record is seed_record:
            for item in nmap:
                seed_map_by_ip[item['ne_ip']] = item
                seed_map_by_name[item['ne_name'].lower()] = item
                tracker.seed_map_ips.add(item['ne_ip'])
                tracker.enqueue(
                    item['ne_ip'],
                    ne_name=item['ne_name'],
                    ne_ip=item['ne_ip'],
                    map_sw=item['sw_version'],
                    method='Seed network map',
                    source=host,
                    depth=1,
                )

        neighbors = parse_neighbor_ips(output.get('topology', ''))
        discovery_record['neighbors'].update(
            neighbor_ip for neighbor_ip, _ in neighbors
        )
        topology_new = 0
        topology_in_map = 0
        for neighbor_ip, _ in neighbors:
            map_item = seed_map_by_ip.get(neighbor_ip)
            if map_item:
                topology_in_map += 1
            neighbor_record, added = tracker.enqueue(
                neighbor_ip,
                ne_name=map_item['ne_name'] if map_item else '--',
                ne_ip=neighbor_ip,
                map_sw=map_item['sw_version'] if map_item else '--',
                method='Seed network map' if map_item else 'Topology neighbor',
                source=host,
                depth=discovery_record['depth'] + 1,
            )
            if added:
                topology_new += 1
                if discovery_transport_ready:
                    route_lock.acquire()
                    route_ok, new_routes, route_error = (
                        prepare_neighbor_host_routes(
                            [neighbor_ip],
                            discovery_gateway,
                        )
                    )
                    if route_ok:
                        discovery_routes_added.extend(new_routes)
                    else:
                        tracker.mark_failed(
                            neighbor_record,
                            'Blocked',
                            route_error,
                        )
                    route_lock.release()

        print(
            f'       Discovery check: {len(neighbors)} topology neighbor(s), '
            f'{topology_in_map} in seed map, {topology_new} newly queued'
        )

        # After the seed's audit data is safely captured, establish one routing
        # context for the entire OSC walk. It stays active until final cleanup.
        if discovery_record is seed_record and not discovery_transport_attempted:
            discovery_transport_attempted = True
            route_targets = [
                record['ne_ip']
                for record in tracker.records
                if record['ne_ip'] != '--'
                and _discovery_alias(record['ne_ip'])
                != _discovery_alias(resolved_target)
            ]

            if route_targets and not use_cit_routing:
                # Routed seed. If the audit PC is L2-adjacent to the GNE's OAMP
                # (same LAN/VLAN), build a temporary "DCN over OAMP": multi-net a
                # temp IP onto the OAMP subnet, pin /32 routes to the neighbors
                # through the GNE, and have the GNE redistribute its OAMP subnet
                # for the RNE return path -- all auto-reverted. If NOT adjacent
                # (truly routed), _setup_oamp_lan cleans up and returns None, and
                # we fall back to a plain direct-connect (which then needs the
                # customer DCN to route the neighbor subnet to the GNE).
                try:
                    _seed_ip = socket.gethostbyname(discovery_seed_fqdn)
                except socket.gaierror:
                    _seed_ip = discovery_seed_fqdn
                print(
                    f'       DISCOVERY: preparing OAMP-LAN routing to '
                    f'{len(set(route_targets))} mapped site(s) ...',
                    end=' ',
                    flush=True,
                )
                _oamp_state = _setup_oamp_lan(
                    discovery_seed_fqdn, _seed_ip,
                    options.user, options.password, route_targets,
                )
                if _oamp_state:
                    discovery_gateway = _oamp_state['gateway']
                    discovery_temp_iface = _oamp_state['iface']
                    discovery_temp_ip = _oamp_state['temp_ip']
                    discovery_routes_added.extend(_oamp_state['routes'])
                    discovery_restore_state = _oamp_state['oamp_restore']
                    discovery_transport_ready = True
                    print(f'{green}OK{resetc}')
                    atexit.register(
                        remove_neighbor_host_routes,
                        discovery_routes_added, discovery_gateway,
                        discovery_temp_iface,
                    )
                    atexit.register(
                        _remove_temp_ip, discovery_temp_iface, discovery_temp_ip,
                    )
                    if discovery_restore_state:
                        atexit.register(
                            restore_oamp_remote_access,
                            discovery_seed_fqdn, options.user, options.password,
                            discovery_restore_state,
                        )
                    discovery_cleanup_registered = True
                else:
                    print(
                        f'{amber}OAMP-LAN not available — connecting directly '
                        f'(neighbor subnet must be routed to the seed){resetc}'
                    )
            elif route_targets:
                # Local craft seed: on-link CIT route-hack (temporary Windows
                # /32 routes through the seed's CIT port + CIT subnet redistribute).
                try:
                    discovery_gateway = socket.gethostbyname(
                        discovery_seed_fqdn
                    )
                except socket.gaierror:
                    discovery_gateway = None

                print(
                    f'       DISCOVERY: preparing Windows routes for '
                    f'{len(set(route_targets))} mapped site(s) ...',
                    end=' ',
                    flush=True,
                )
                if discovery_gateway:
                    routes_ok, new_routes, route_error = (
                        prepare_neighbor_host_routes(
                            route_targets,
                            discovery_gateway,
                        )
                    )
                else:
                    routes_ok, new_routes, route_error = (
                        False,
                        [],
                        'Could not resolve the seed shelf gateway',
                    )

                if routes_ok:
                    discovery_routes_added.extend(new_routes)
                    print(f'{green}OK{resetc}')
                    print(
                        '       DISCOVERY: preparing seed CIT return route ...',
                        end=' ',
                        flush=True,
                    )
                    route_ready, discovery_restore_state = (
                        prepare_cit_remote_access(
                            discovery_seed_fqdn,
                            options.user,
                            options.password,
                        )
                    )
                    if route_ready:
                        discovery_transport_ready = True
                        print(f'{green}OK{resetc}')
                        # Register the mutable list even when it is currently
                        # empty; topology verification may add routes later.
                        atexit.register(
                            remove_neighbor_host_routes,
                            discovery_routes_added,
                            discovery_gateway,
                        )
                        if discovery_restore_state:
                            atexit.register(
                                restore_cit_remote_access,
                                discovery_seed_fqdn,
                                options.user,
                                options.password,
                                discovery_restore_state,
                            )
                        discovery_cleanup_registered = True
                    else:
                        discovery_transport_error = (
                            'Could not prepare the seed CIT return route'
                        )
                        print(f'{red}FAILED{resetc}')
                        if discovery_routes_added:
                            remove_neighbor_host_routes(
                                discovery_routes_added,
                                discovery_gateway,
                            )
                            discovery_routes_added.clear()
                else:
                    discovery_transport_error = route_error
                    print(f'{red}FAILED — {route_error}{resetc}')
            else:
                discovery_transport_attempted = True

        # ── Buffer this site's rows (rendered later, Terminal→Terminal) ─────
        # Nothing is written to the workbook during the parallel scan; each
        # site's parsed rows are stashed on its record and emitted afterwards
        # in physical line order, which is also why no workbook lock is needed.
        # On an ILA each line direction has an input-monitor port (reports only
        # input power) and an amplifier port (gain/tilt/output); merge the pair
        # into one row per line (Line1/Line2). A Terminal's LineIn and LineOut
        # are two separate amplifiers (ingress + egress), both fully populated,
        # so they stay as their own rows.
        amp_rows = []
        for card_type, slot in amp_cards.items():
            cli_kw = CARD_CLI[card_type]
            if site_type == 'ILA':
                for line_no in (1, 2):
                    in_d = parse_amp_port(
                        output.get(f'amp_{cli_kw}_line{line_no}in', ''),
                        site_type, 'in')
                    out_d = parse_amp_port(
                        output.get(f'amp_{cli_kw}_line{line_no}out', ''),
                        site_type, 'out')
                    amp_rows.append({
                        'card_type':  card_type,
                        'slot':       slot,
                        'port_label': f'Line{line_no}',
                        'data': {
                            'target_gain':    out_d['target_gain'],
                            'current_gain':   out_d['current_gain'],
                            'gain_range':     out_d['gain_range'],
                            'actual_tilt':    out_d['actual_tilt'],
                            'total_in_pwr':   in_d['total_in_pwr'],
                            'total_out_pwr':  out_d['total_out_pwr'],
                            'signal_out_pwr': out_d['signal_out_pwr'],
                            'agc_state':      (in_d['agc_state']
                                               if in_d['agc_state'] != '--'
                                               else out_d['agc_state']),
                        },
                    })
            else:
                for port_label, direction in (('LineIn', 'in'),
                                              ('LineOut', 'out')):
                    amp_rows.append({
                        'card_type':  card_type,
                        'slot':       slot,
                        'port_label': port_label,
                        'data':       parse_amp_port(
                            output.get(f'amp_{cli_kw}_{port_label.lower()}', ''),
                            site_type, direction),
                    })

        # Power/voltage health flag for the Summary tab (see _power_health).
        pf_list = parse_pf(output.get('pf', ''))
        power_health = _power_health(pf_list, alarm_list)

        discovery_record['_render'] = {
            'host':         host,
            'short':        short,
            'fqdn':         fqdn,
            'site_type':    site_type,
            'sw_ver':       sw_ver,
            'sw_stat':      sw_stat,
            'asset_tag':    asset_tag,
            'loopback':     loopback,
            'osclan':       osclan,
            'ospf':         ospf,
            'shelf':        shelf,
            'osc_pwr':      osc_pwr,
            'alarm_counts': alarm_counts,
            'audit_date':   audit_date,
            'cards':        cards,
            'ifaces':       ifaces,
            'pf_list':      pf_list,
            'power_health': power_health,
            'amp_rows':     amp_rows,
            'internal_fibers': compute_internal_fibers(
                output, site_type, amp_cards, omdwb_slot),
        }

        # Buffer local alarms too (emitted grouped by site, in line order).
        for alarm in alarm_list:
            discovery_record['_alarm_rows'].append((host, alarm))

        # The full local audit is complete. All known aliases now point to this
        # record so later queue entries cannot scan the same shelf again.
        tracker.mark_scanned(
            discovery_record,
            host,
            actual_ip,
            loopback.get('ip_addr'),
            discovery_record.get('ne_ip'),
            resolved_target,
        )

        # ── PART 2: REMOTE ────────────────────────────────────────────────────
        local_ip  = loopback['ip_addr'].split('/')[0] if loopback['ip_addr'] != '--' else None

        if not neighbors:
            print(f'       {amber}PART 2: no external neighbor found in topology — skipping{resetc}')
        elif not local_ip:
            print(f'       {amber}PART 2: no loopback IP available — skipping{resetc}')
        else:
            part2_neighbors = list(neighbors)
            route_restore_state = None
            added_neighbor_routes = []

            if not use_cit_routing:
                # Routed/remote seed: connect to the OSC neighbors directly.
                # Reachability is provided by the deployed DCN management routing
                # (GNE OAMP -> OOB/customer DCN: OSPF peering, redistributed
                # default route, or proxy ARP -- see DCN Planning Guide s2.4).
                # The audit does no PC-side route-hack here (there is no on-link
                # equivalent of the CIT craft port for a routed seed).
                cit_gateway = None
                host_routes_ready = True
                print(
                    '       PART 2: connecting to neighbors directly '
                    f'(routed seed) ... {green}OK{resetc}'
                )
            elif discovery_transport_ready:
                # Craft seed: reuse the CIT routing context (Windows /32 routes +
                # CIT return route) established once during seed processing.
                cit_gateway = discovery_gateway
                host_routes_ready = True
                print(
                    '       PART 2: using discovery routing context ... '
                    f'{green}OK{resetc}'
                )
            elif discovery_transport_error:
                host_routes_ready = False
                part2_neighbors = []
                cit_gateway = None
                print(
                    f'       {red}PART 2: skipped — '
                    f'{discovery_transport_error}{resetc}'
                )
            else:
                cit_gateway = socket.gethostbyname(fqdn)
                print(
                    '       PART 2: preparing Windows routes to OSC neighbors ...',
                    end=' ',
                    flush=True,
                )
                route_lock.acquire()
                host_routes_ready, added_neighbor_routes, host_route_error = (
                    prepare_neighbor_host_routes(
                        [neighbor_ip for neighbor_ip, _ in part2_neighbors],
                        cit_gateway,
                    )
                )
                if host_routes_ready:
                    print(f'{green}OK{resetc}')
                    if added_neighbor_routes:
                        atexit.register(
                            remove_neighbor_host_routes,
                            added_neighbor_routes,
                            cit_gateway,
                        )
                else:
                    print(f'{red}FAILED — {host_route_error}{resetc}')
                    part2_neighbors = []

                if host_routes_ready:
                    print('       PART 2: preparing CIT route to OSC neighbors ...', end=' ', flush=True)
                    route_ready, route_restore_state = prepare_cit_remote_access(
                        fqdn, options.user, options.password,
                    )
                    if route_ready:
                        print(f'{green}OK{resetc}')
                        if route_restore_state:
                            # Best-effort safety net if an unexpected exception
                            # stops the audit before normal restoration below.
                            atexit.register(
                                restore_cit_remote_access,
                                fqdn,
                                options.user,
                                options.password,
                                route_restore_state,
                            )
                    else:
                        print(f'{red}FAILED — skipping neighbors{resetc}')
                        part2_neighbors = []
                route_lock.release()

            for nb_ip, line_num in part2_neighbors:
                scanned_note = (
                    ' [link verification; shelf already scanned]'
                    if tracker.find_scanned(nb_ip)
                    else ''
                )
                print(
                    f'       PART 2: neighbor {bold}{nb_ip}{resetc} '
                    f'(LINE{line_num}){scanned_note} ...',
                    end=' ',
                    flush=True,
                )
                p2 = run_part2_remote(local_ip, nb_ip, options.user, options.password)

                if not p2:
                    print(f'{red}FAILED{resetc}')
                    continue
                print(f'{green}OK{resetc}')

                # ── Span loss comparison ──────────────────────────────────
                # LINE number maps to local SFP: LINE1→SFP1, LINE2→SFP2.
                # Terminal only ever has SFP1 (one LINE direction).
                # _best_opr picks the non-Off remote SFP automatically.
                if site_type == 'ILA' and line_num == 2:
                    local_tx = osc_pwr['sfp2_opt']
                    local_rx = osc_pwr['sfp2_opr']
                else:
                    local_tx = osc_pwr['sfp1_opt']
                    local_rx = osc_pwr['sfp1_opr']

                remote_rx = _best_opr(p2['sfp1_opr'], p2['sfp2_opr'])
                remote_tx = _best_opr(p2['sfp1_opt'], p2['sfp2_opt'])

                span_ab = compute_span(local_tx,  remote_rx)
                span_ba = compute_span(remote_tx, local_rx)
                delta_str, flag_str = _span_delta(span_ab, span_ba)

                # ── Buffer Remote OSC row + remote alarms (grouped by site) ──
                discovery_record['_remote_rows'].append({
                    'host':         host,
                    'site_type':    site_type,
                    'nb_ip':        nb_ip,
                    'nb_site_type': p2['nb_site_type'],
                    'ping_ok':      p2['ping_ok'],
                    'ping_latency': p2['ping_latency'],
                    'local_tx':     local_tx,
                    'local_rx':     local_rx,
                    'r_sfp1_opr':   p2['sfp1_opr'],
                    'r_sfp1_opt':   p2['sfp1_opt'],
                    'r_sfp2_opr':   p2['sfp2_opr'],
                    'r_sfp2_opt':   p2['sfp2_opt'],
                    'span_ab':      span_ab,
                    'span_ba':      span_ba,
                    'delta_str':    delta_str,
                    'flag_str':     flag_str,
                })
                remote_label = f'{host} → REMOTE ({nb_ip})'
                for alarm in p2['alarm_list']:
                    discovery_record['_alarm_rows'].append((remote_label, alarm))

                p2_cr = p2['alarm_counts']['CR']
                p2_mj = p2['alarm_counts']['MJ']
                p2_alarm_str = (
                    f'{red}{p2_cr} CR{resetc}, {amber}{p2_mj} MJ{resetc}'
                    if (p2_cr + p2_mj) > 0 else f'{green}No remote critical/major alarms{resetc}'
                )
                ping_str = (
                    f'{green}ping OK ({p2["ping_latency"]}){resetc}'
                    if p2['ping_ok'] else f'{red}ping FAILED{resetc}'
                )
                span_flag = (f'{amber}{flag_str}{resetc}' if 'CHECK' in str(flag_str)
                             else f'{green}{flag_str}{resetc}')
                print(f'         {ping_str}  |  Span delta: {span_flag}  |  Remote alarms: {p2_alarm_str}')

            if route_restore_state:
                route_lock.acquire()
                print('       PART 2: restoring CIT routing state ...', end=' ', flush=True)
                restored = restore_cit_remote_access(
                    fqdn, options.user, options.password, route_restore_state,
                )
                if restored:
                    atexit.unregister(restore_cit_remote_access)
                print(f'{green}OK{resetc}' if restored else f'{red}FAILED{resetc}')
                route_lock.release()

            if added_neighbor_routes:
                route_lock.acquire()
                print('       PART 2: removing temporary Windows routes ...', end=' ', flush=True)
                routes_removed = remove_neighbor_host_routes(
                    added_neighbor_routes, cit_gateway,
                )
                if routes_removed:
                    atexit.unregister(remove_neighbor_host_routes)
                print(f'{green}OK{resetc}' if routes_removed else f'{red}FAILED{resetc}')
                route_lock.release()

        cr = alarm_counts['CR']
        mj = alarm_counts['MJ']
        alarm_str = (
            f'{red}{cr} CR{resetc}, {amber}{mj} MJ{resetc}'
            if (cr + mj) > 0 else f'{green}No critical/major alarms{resetc}'
        )
        print(f'       SW: {sw_ver}  |  Alarms: {alarm_str}')

    def _setup_discovery_routing_oamp(resolved_target, emit):
        """Establish the OSC-walk routing context once, right after the seed's
        NETCONF data is captured, so the RNE neighbours are reachable over
        NETCONF. Multi-nets a temp IP onto the seed's OAMP subnet, pins /32
        routes to every mapped neighbour through the GNE, and toggles the GNE
        OAMP redistribution -- all auto-reverted at teardown. A no-op single-node
        audit (no neighbours) leaves routing unprepared."""
        nonlocal discovery_transport_ready, discovery_gateway
        nonlocal discovery_temp_iface, discovery_temp_ip
        nonlocal discovery_restore_state, discovery_cleanup_registered
        nonlocal discovery_source_ip
        print = emit
        route_targets = [
            r['ne_ip'] for r in tracker.records
            if r['ne_ip'] != '--'
            and _discovery_alias(r['ne_ip']) != _discovery_alias(resolved_target)
        ]
        if not route_targets:
            return
        try:
            _seed_ip = socket.gethostbyname(discovery_seed_fqdn)
        except socket.gaierror:
            _seed_ip = discovery_seed_fqdn
        print(
            f'       DISCOVERY: preparing OAMP-LAN routing to '
            f'{len(set(route_targets))} neighbour(s) ...',
            end=' ', flush=True,
        )
        _oamp = _setup_oamp_lan(
            discovery_seed_fqdn, _seed_ip,
            options.user, options.password, route_targets,
        )
        if _oamp:
            discovery_gateway = _oamp['gateway']
            discovery_temp_iface = _oamp['iface']
            discovery_temp_ip = _oamp['temp_ip']
            discovery_routes_added.extend(_oamp['routes'])
            discovery_restore_state = _oamp['oamp_restore']
            discovery_transport_ready = True
            # Source RNE connections from the wired OAMP-subnet IP ATLAS set, so
            # the RNE return route (redistributed OAMP subnet) applies.
            discovery_source_ip = _oamp.get('source_ip') or discovery_source_ip
            print(f'{green}OK{resetc}')
            atexit.register(
                remove_neighbor_host_routes,
                discovery_routes_added, discovery_gateway, discovery_temp_iface,
            )
            atexit.register(_remove_temp_ip, discovery_temp_iface, discovery_temp_ip)
            if discovery_restore_state:
                atexit.register(
                    restore_oamp_remote_access,
                    discovery_seed_fqdn, options.user, options.password,
                    discovery_restore_state,
                )
            discovery_cleanup_registered = True
        else:
            print(
                f'{amber}OAMP-LAN not available — neighbours may be unreachable '
                f'(needs customer DCN routing to the seed){resetc}'
            )

    def _netconf_audit_node(discovery_record, emit, processed_count, total,
                            fqdn, resolved_target, display_name):
        """Collect + render one node over NETCONF (the primary collector for
        every address other than the 172.16.0.1 craft port). Mirrors the CLI
        _audit_body's discovery bookkeeping but sources per-node data from
        collect_node_netconf() and walks neighbours via the connections tree's
        external-peer-address. Runs from a worker thread; shared state is touched
        only through the tracker's own locks and route_lock."""
        nonlocal discovery_transport_ready, discovery_transport_attempted
        nonlocal discovery_transport_error, discovery_gateway, discovery_temp_iface
        print = emit
        from scripts.Network._psi_netconf import collect_node_netconf

        audit_date = datetime.now().strftime('%Y-%m-%d')
        try:
            rec = collect_node_netconf(fqdn, options.user, options.password,
                                       source_ip=discovery_source_ip)
        except Exception as exc:                     # report + continue the walk
            print(f'{red}FAILED{resetc}')
            tracker.mark_failed(
                discovery_record, 'Failed', f'NETCONF collection failed: {exc}')
            if discovery_record is seed_record:
                discovery_transport_error = (
                    f'Seed NETCONF collection failed ({exc}); '
                    'OSC discovery routing was not prepared')
            debug_log(traceback.format_exc(), 'netconf-collect')
            return
        print(f'{green}OK{resetc}')

        actual_name = rec.get('hostname') or '--'
        if actual_name != '--':
            discovery_record['ne_name'] = actual_name
        duplicate = tracker.claim(
            discovery_record, actual_name, fqdn, resolved_target)
        if duplicate:
            tracker.mark_failed(
                discovery_record, 'Skipped duplicate',
                f'CLI identity matches already scanned site {duplicate["ne_name"]}')
            print(
                f'       {amber}identity already scanned as '
                f'{duplicate["ne_name"]} — skipping{resetc}')
            return
        host = actual_name if actual_name != '--' else display_name

        render, alarm_list = render_from_netconf(rec, fqdn, audit_date)
        site_type    = render['site_type']
        sw_ver       = render['sw_ver']
        loopback     = render['loopback']
        alarm_counts = render['alarm_counts']
        print(
            f'       Site type: {bold}{site_type}{resetc}  SW: {sw_ver}  '
            f'Loopback: {loopback["ip_addr"]}')

        # ── discovery bookkeeping ────────────────────────────────────────────
        actual_ip = (
            loopback['ip_addr'].split('/')[0]
            if loopback.get('ip_addr', '--') != '--'
            else discovery_record['ne_ip']
            if discovery_record['ne_ip'] != '--' else resolved_target
        )
        discovery_record['actual_ip'] = actual_ip
        discovery_record['actual_sw'] = sw_ver
        discovery_record['site_type'] = site_type
        tracker.add_aliases(
            discovery_record, host, actual_ip, loopback.get('ip_addr'))

        neighbors = rec.get('neighbors', [])
        discovery_record['neighbors'].update(neighbors)
        newly = 0
        for neighbor_ip in neighbors:
            neighbor_record, added = tracker.enqueue(
                neighbor_ip, ne_name='--', ne_ip=neighbor_ip,
                method='NETCONF neighbor', source=host,
                depth=discovery_record['depth'] + 1)
            if added:
                newly += 1
                if discovery_transport_ready:
                    route_lock.acquire()
                    route_ok, new_routes, route_error = (
                        prepare_neighbor_host_routes(
                            [neighbor_ip], discovery_gateway,
                            discovery_temp_iface))
                    if route_ok:
                        discovery_routes_added.extend(new_routes)
                    else:
                        tracker.mark_failed(
                            neighbor_record, 'Blocked', route_error)
                    route_lock.release()
        print(
            f'       Discovery: {len(neighbors)} NETCONF neighbour(s), '
            f'{newly} newly queued')

        # After the seed's data is safely captured, establish the OSC-walk
        # routing once. OAMP-LAN auto-detects on-site (L2-adjacent) vs remote:
        # on-site it builds the temp-DCN; remote it no-ops and the RNEs are
        # direct-connected (reachable only if the customer DCN routes to the PC).
        if discovery_record is seed_record and not discovery_transport_attempted:
            discovery_transport_attempted = True
            _setup_discovery_routing_oamp(resolved_target, print)

        discovery_record['_render'] = render
        for alarm in alarm_list:
            discovery_record['_alarm_rows'].append((host, alarm))
        tracker.mark_scanned(
            discovery_record, host, actual_ip, loopback.get('ip_addr'),
            discovery_record.get('ne_ip'), resolved_target)

        cr, mj = alarm_counts['CR'], alarm_counts['MJ']
        alarm_str = (
            f'{red}{cr} CR{resetc}, {amber}{mj} MJ{resetc}'
            if (cr + mj) > 0 else f'{green}No critical/major alarms{resetc}')
        print(f'       SW: {sw_ver}  |  Alarms: {alarm_str}')

    def process_record(discovery_record, live=False):
        """Audit one site end-to-end. Safe to run from a worker thread: all
        shared state is touched only through the locks above, and any lock a
        crash leaves held is drained in the finally block so one failed node
        can never wedge the others."""
        emit, flush_log = _make_logger(live)
        try:
            _audit_body(discovery_record, emit)
        finally:
            flush_log()
            _drain_lock(route_lock)

    # ── Seed first, sequentially ─────────────────────────────────────────────
    # The seed's own audit is what establishes the Windows /32 routes and the
    # temporary CIT redistribution that every other (non-input) site depends on
    # to be reachable, so it must finish before any parallel worker starts.
    process_record(seed_record, live=True)
    tracker.discard_queued(seed_record)

    # ── Fan out the rest: one worker per site in the network map ─────────────
    max_workers = max(1, len(tracker.queue))
    print(
        f'\n{bold}Parallel audit:{resetc} up to {max_workers} concurrent node '
        f'session(s) — one per site in the network map.'
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        in_flight = {}

        def _pump():
            # Submit everything currently queued. Called again each time a
            # worker finishes so topology neighbors discovered mid-scan get
            # picked up (the discovery BFS keeps growing the queue).
            for record in tracker.drain_queued():
                future = executor.submit(process_record, record)
                in_flight[future] = record

        _pump()
        while in_flight:
            done, _ = concurrent.futures.wait(
                list(in_flight),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                record = in_flight.pop(future)
                error = future.exception()
                if error is not None:
                    tracker.mark_failed(
                        record, 'Failed', f'Worker error: {error}',
                    )
                    with print_lock:
                        print(
                            f'{red}Worker error on '
                            f'{record.get("target", "--")}: {error}{resetc}'
                        )
            _pump()

    # ── Restore the one audit-wide discovery routing context ────────────────
    if discovery_transport_ready:
        if discovery_restore_state:
            # Craft seed restores CIT redistribute/area; routed seed (OAMP-LAN)
            # restores the OAMP routestate.
            _is_oamp = not use_cit_routing
            _label = 'OAMP' if _is_oamp else 'CIT'
            _restore_fn = (restore_oamp_remote_access if _is_oamp
                           else restore_cit_remote_access)
            print(
                f'       DISCOVERY: restoring seed {_label} routing state ...',
                end=' ',
                flush=True,
            )
            discovery_restored = _restore_fn(
                discovery_seed_fqdn,
                options.user,
                options.password,
                discovery_restore_state,
            )
            if discovery_restored:
                atexit.unregister(_restore_fn)
            print(
                f'{green}OK{resetc}'
                if discovery_restored
                else f'{red}FAILED{resetc}'
            )

        if discovery_routes_added:
            print(
                '       DISCOVERY: removing temporary Windows routes ...',
                end=' ',
                flush=True,
            )
            discovery_routes_removed = remove_neighbor_host_routes(
                discovery_routes_added,
                discovery_gateway,
                discovery_temp_iface,
            )
            if discovery_routes_removed:
                atexit.unregister(remove_neighbor_host_routes)
            print(
                f'{green}OK{resetc}'
                if discovery_routes_removed
                else f'{red}FAILED{resetc}'
            )
        elif discovery_cleanup_registered:
            atexit.unregister(remove_neighbor_host_routes)

        # OAMP-LAN only: remove the temporary secondary IP from the NIC.
        if discovery_temp_ip:
            print(
                '       DISCOVERY: removing temporary OAMP-subnet IP ...',
                end=' ',
                flush=True,
            )
            _remove_temp_ip(discovery_temp_iface, discovery_temp_ip)
            atexit.unregister(_remove_temp_ip)
            print(f'{green}OK{resetc}')

    # ── Order sites Terminal→Terminal by walking the topology chain ─────────
    lines, line_meta = compute_site_lines(tracker)

    print(f'\n{bold}Network-map walk (Terminal → Terminal):{resetc}')
    if not lines:
        print(f'  {amber}No scanned sites to order.{resetc}')
    for _i, _chain in enumerate(lines, 1):
        _names = []
        for _rec in _chain:
            _tag = ' (T)' if _rec['_render']['site_type'] == 'TERMINAL' else ''
            _names.append(f'{_rec["ne_name"]}{_tag}')
        _head_t = _chain[0]['_render']['site_type'] == 'TERMINAL'
        _tail_t = _chain[-1]['_render']['site_type'] == 'TERMINAL'
        _flag = (f'{green}Terminal–Terminal verified{resetc}'
                 if _head_t and _tail_t
                 else f'{red}endpoint not a Terminal — verify map{resetc}')
        print(f'  Line {_i} [{len(_chain)} sites | {_flag}]')
        print('    ' + ' → '.join(_names))

    ordered_scanned = [rec for chain in lines for rec in chain]

    # NETCONF nodes have no CLI PART 2; rebuild the node-to-node OSC span tab
    # from the OSC power every node already reported (no-op for telnet records,
    # which the craft path already populated).
    _netconf_osc_spans(ordered_scanned)

    rows['discovery'], neighbor_verified, neighbor_incomplete = (
        write_discovery_verification(
            ws_discovery,
            rows['discovery'],
            f,
            tracker,
            ordered_scanned,
            line_meta,
        )
    )

    # ── Emit every data tab in Terminal→Terminal order ──────────────────────
    # All tabs were buffered per record during the parallel scan; rendering
    # them here, in physical line order, lays the whole document out from one
    # Terminal to the other and keeps each site's rows contiguous and banded.
    for record in ordered_scanned:
        d = record['_render']
        host = d['host']
        alarm_counts = d['alarm_counts']
        ospf = d['ospf']
        osclan = d['osclan']
        loopback = d['loopback']
        shelf = d['shelf']

        # Summary
        r = rows['sum']
        ro = band_fmt('sum', host)
        sw_fmt = f['good'] if d['sw_stat'] == 'Approved' else f['crit']
        dns_fmt = f['good'] if ospf['dns_capable'] == 'Yes' else f['warn']
        cr_fmt = f['crit'] if alarm_counts['CR'] > 0 else ro
        mj_fmt = f['warn'] if alarm_counts['MJ'] > 0 else ro
        ph_fmt = f['crit'] if str(d['power_health']).startswith('CHECK') else f['good']
        for c, (val, fmt) in enumerate([
            (host, ro), (d['short'], ro), (d['fqdn'], ro), (d['site_type'], ro),
            (d['sw_ver'], ro), (d['sw_stat'], sw_fmt), (d['asset_tag'], ro),
            (loopback['ip_addr'], ro), (osclan['ip_addr'], ro),
            (ospf['dns_capable'], dns_fmt),
            (alarm_counts['CR'], cr_fmt), (alarm_counts['MJ'], mj_fmt),
            (alarm_counts['MN'], ro), (alarm_counts['WN'], ro),
            (d['power_health'], ph_fmt),
            (d['audit_date'], ro),
        ]):
            ws_sum.write(r, c, val, fmt)
        rows['sum'] += 1

        # Card Inventory
        for card in d['cards']:
            r = rows['cards']
            ro = band_fmt('cards', host)
            ws_cards.write(r, 0, host, ro)
            ws_cards.write(r, 1, card['card_type'], ro)
            ws_cards.write(r, 2, card['slot'], ro)
            ws_cards.write(r, 3, card['serial'], ro)
            ws_cards.write(r, 4, card['clei'], ro)
            rows['cards'] += 1

        # Interface Inventory
        for iface in d['ifaces']:
            r = rows['iface']
            ro = band_fmt('iface', host)
            ws_iface.write(r, 0, host, ro)
            ws_iface.write(r, 1, iface['iface_name'], ro)
            ws_iface.write(r, 2, iface['port_type'], ro)
            ws_iface.write(r, 3, iface['serial'], ro)
            ws_iface.write(r, 4, iface['manufacturer'], ro)
            rows['iface'] += 1

        # OSPF
        r = rows['ospf']
        ro = band_fmt('ospf', host)
        dns_fmt2 = f['good'] if ospf['dns_capable'] == 'Yes' else f['warn']
        ws_ospf.write(r, 0, host, ro)
        ws_ospf.write(r, 1, ospf['router_id'], ro)
        ws_ospf.write(r, 2, ospf['area_id'], ro)
        ws_ospf.write(r, 3, ospf['opaque_lsa_cap'], ro)
        ws_ospf.write(r, 4, ospf['dns_capable'], dns_fmt2)
        rows['ospf'] += 1

        # Management
        r = rows['mgmt']
        ro = band_fmt('mgmt', host)
        ws_mgmt.write(r, 0, host, ro)
        ws_mgmt.write(r, 1, osclan['ip_addr'], ro)
        ws_mgmt.write(r, 2, osclan['mask'], ro)
        ws_mgmt.write(r, 3, osclan['admin_state'], ro)
        ws_mgmt.write(r, 4, osclan['oper_state'], ro)
        ws_mgmt.write(r, 5, loopback['ip_addr'], ro)
        rows['mgmt'] += 1

        # Power
        r = rows['power']
        ro = band_fmt('power', host)
        ws_power.write(r, 0, host, ro)
        ws_power.write(r, 1, shelf['shelf_type'], ro)
        ws_power.write(r, 2, shelf['pwr_type'], ro)
        ws_power.write(r, 3, shelf['total_current'], ro)
        ws_power.write(r, 4, shelf['total_watts'], ro)
        rows['power'] += 1

        # Power Filters
        for pf in d['pf_list']:
            r = rows['pf']
            ro = band_fmt('pf', host)
            oper_fmt = f['good'] if pf['oper_state'] == 'Up' else f['crit']
            ws_pf.write(r, 0, host, ro)
            ws_pf.write(r, 1, pf['slot'], ro)
            ws_pf.write(r, 2, pf['admin_state'], ro)
            ws_pf.write(r, 3, pf['oper_state'], oper_fmt)
            ws_pf.write(r, 4, pf.get('qualifier', ''), ro)
            rows['pf'] += 1

        # Amplifiers
        for a in d['amp_rows']:
            data = a['data']
            r = rows['amp']
            ro = band_fmt('amp', host)
            gain_range = data.get('gain_range', '--')
            in_spec = _gain_in_spec(gain_range, data.get('current_gain', '--'))
            spec_fmt = (f['good'] if in_spec == 'PASS'
                        else f['crit'] if in_spec == 'FAIL' else ro)
            ws_amp.write(r,  0, host, ro)
            ws_amp.write(r,  1, a['card_type'], ro)
            ws_amp.write(r,  2, a['slot'], ro)
            ws_amp.write(r,  3, a['port_label'], ro)
            ws_amp.write(r,  4, data.get('target_gain', '--'), ro)
            ws_amp.write(r,  5, data.get('current_gain', '--'), ro)
            ws_amp.write(r,  6, gain_range, ro)
            ws_amp.write(r,  7, in_spec, spec_fmt)
            ws_amp.write(r,  8, data.get('actual_tilt', '--'), ro)
            ws_amp.write(r,  9, data.get('total_in_pwr', '--'), ro)
            ws_amp.write(r, 10, data.get('total_out_pwr', '--'), ro)
            ws_amp.write(r, 11, data.get('signal_out_pwr', '--'), ro)
            ws_amp.write(r, 12, data.get('agc_state', '--'), ro)
            rows['amp'] += 1

        # Alarms (local + remote for this site, grouped together)
        for node_label, alarm in record['_alarm_rows']:
            r = rows['alarms']
            base_fmt = band_fmt('alarms', host)
            severity_fmt = {
                'CR': f['alarm_cr'],
                'MJ': f['alarm_mj'],
                'MN': f['alarm_mn'],
            }.get(alarm['severity'], base_fmt)
            ws_alarms.write(r,  0, node_label,          base_fmt)
            ws_alarms.write(r,  1, alarm['severity'],   severity_fmt)
            ws_alarms.write(r,  2, alarm['sa'],         base_fmt)
            ws_alarms.write(r,  3, alarm['date'],       base_fmt)
            ws_alarms.write(r,  4, alarm['time'],       base_fmt)
            ws_alarms.write(r,  5, alarm['layer'],      base_fmt)
            ws_alarms.write(r,  6, alarm['condition'],  base_fmt)
            ws_alarms.write(r,  7, alarm['interface'],  base_fmt)
            ws_alarms.write(r,  8, alarm['direction'],  base_fmt)
            ws_alarms.write(r,  9, alarm['description'],base_fmt)
            ws_alarms.write(r, 10, alarm['card'],       base_fmt)
            rows['alarms'] += 1

        # OSC Power (per span: local + remote SFP TX/RX, span loss, delta)
        for rr in record['_remote_rows']:
            r = rows['remote']
            ro = band_fmt('remote', host)
            ping_fmt = f['good'] if rr['ping_ok'] else f['crit']
            flag_str = rr['flag_str']
            flag_fmt = (f['crit'] if 'CHECK' in str(flag_str)
                        else f['good'] if flag_str == 'OK'
                        else ro)
            ws_remote.write(r,  0, rr['host'],          ro)
            ws_remote.write(r,  1, rr['site_type'],     ro)
            ws_remote.write(r,  2, rr['nb_ip'],         ro)
            ws_remote.write(r,  3, rr['nb_site_type'],  ro)
            ws_remote.write(r,  4, 'Yes' if rr['ping_ok'] else 'No', ping_fmt)
            ws_remote.write(r,  5, rr['ping_latency'],  ro)
            ws_remote.write(r,  6, rr['local_tx'],      ro)
            ws_remote.write(r,  7, rr['local_rx'],      ro)
            ws_remote.write(r,  8, rr['r_sfp1_opr'],    ro)
            ws_remote.write(r,  9, rr['r_sfp1_opt'],    ro)
            ws_remote.write(r, 10, rr['r_sfp2_opr'],    ro)
            ws_remote.write(r, 11, rr['r_sfp2_opt'],    ro)
            ws_remote.write(r, 12, rr['span_ab'],       ro)
            ws_remote.write(r, 13, rr['span_ba'],       ro)
            ws_remote.write(r, 14, rr['delta_str'],     ro)
            ws_remote.write(r, 15, rr['flag_str'],      flag_fmt)
            rows['remote'] += 1

        # OSC Internal (per line: the OSC Rx each card reports, and the spread)
        osc_internal = d['osc_pwr'].get('internal_delta', {})
        for line_sfp in (1, 2):
            info = osc_internal.get(f'sfp{line_sfp}')
            if not info or not info['detail']:
                continue
            detail = info['detail']                     # [(source, rx_float)]
            rx_values = [rx for _, rx in detail]
            readings_str = ', '.join(f'{src}={rx:.2f}' for src, rx in detail)
            flag_str = info['flag']
            r = rows['osc_internal']
            ro_oi = band_fmt('osc_internal', host)
            flag_fmt = (f['crit'] if 'CHECK' in str(flag_str)
                        else f['good'] if flag_str == 'OK'
                        else ro_oi)
            ws_osc_internal.write(r, 0, host,             ro_oi)
            ws_osc_internal.write(r, 1, d['site_type'],   ro_oi)
            ws_osc_internal.write(r, 2, f'SFP{line_sfp}', ro_oi)
            ws_osc_internal.write(r, 3, len(detail),      ro_oi)
            ws_osc_internal.write(r, 4, min(rx_values),   ro_oi)
            ws_osc_internal.write(r, 5, max(rx_values),   ro_oi)
            ws_osc_internal.write(r, 6, info['delta'],    ro_oi)
            ws_osc_internal.write(r, 7, flag_str,         flag_fmt)
            ws_osc_internal.write(r, 8, readings_str,     ro_oi)
            rows['osc_internal'] += 1

        # Internal Fibers (intra-shelf LINE patches: out power vs paired in power)
        for fib in d.get('internal_fibers', []):
            r = rows['int_fiber']
            ro_if = band_fmt('int_fiber', host)
            flag_str = fib['flag']
            flag_fmt = (f['crit'] if 'CHECK' in str(flag_str)
                        else f['good'] if flag_str == 'OK'
                        else ro_if)
            ws_int_fiber.write(r, 0, host,            ro_if)
            ws_int_fiber.write(r, 1, fib['src_aid'],  ro_if)
            ws_int_fiber.write(r, 2, fib['src_card'], ro_if)
            ws_int_fiber.write(r, 3, fib['src_out'],  ro_if)
            ws_int_fiber.write(r, 4, fib['dst_aid'],  ro_if)
            ws_int_fiber.write(r, 5, fib['dst_card'], ro_if)
            ws_int_fiber.write(r, 6, fib['dst_in'],   ro_if)
            ws_int_fiber.write(r, 7, fib['band'],     ro_if)
            ws_int_fiber.write(r, 8, fib['loss'],     ro_if)
            ws_int_fiber.write(r, 9, flag_str,        flag_fmt)
            rows['int_fiber'] += 1

    # Add filterable tables after every data row has been written.
    for key, sheet_def, table_name, _ in SHEET_SPECS:
        finalize_data_sheet(
            worksheets[key],
            sheet_def,
            f,
            rows[key],
            table_name,
        )

    # ── Close workbook ───────────────────────────────────────────────────────
    print()
    for attempt in range(3):
        try:
            wb.close()
            break
        except PermissionError:
            if attempt < 2 and _INTERACTIVE:
                print(f'{amber}Output file is open — close it and press Enter ...{resetc}', end='')
                input()
            else:
                raise

    scanned_once = sum(
        record['status'] == 'Scanned'
        for record in tracker.records
    )
    skipped_duplicates = sum(
        record['status'] == 'Skipped duplicate'
        for record in tracker.records
    )
    failed_sites = sum(
        record['status'] in ('Failed', 'Blocked')
        for record in tracker.records
    )

    print(f'\n{bold}Audit complete.{resetc}')
    print(f'  Sites discovered       : {len(tracker.records)}')
    print(f'  Sites scanned once     : {green}{scanned_once}{resetc}')
    print(f'  Duplicate scans skipped: {amber}{skipped_duplicates}{resetc}')
    print(f'  Failed / blocked       : {red}{failed_sites}{resetc}')
    print(f'  Neighbor checks verified: {green}{neighbor_verified}{resetc}')
    print(f'  Neighbor checks incomplete: {red}{neighbor_incomplete}{resetc}')
    print(f'  Output file            : {bold}{options.outfile}{resetc}')

    # Offer to open file on macOS
    if sys.platform == 'darwin' and _INTERACTIVE:
        open_it = input('\nOpen Excel file? [Y/n]: ').strip().lower()
        if open_it in ('', 'y', 'yes'):
            subprocess.run(['open', options.outfile])


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def run_audit(seed_host, username, password, output_path, *,
              debug=False, log_callback=None):
    """ATLAS entry point: run a full PSI network audit from one seed host,
    IN-PROCESS (Telnet -- no console needed, like the RLS REST audit).

    Populates the module ``options``/``hostlist`` from the arguments (no argv
    parsing, no interactive prompts), routes the audit's ``print()`` output
    line-by-line through ``log_callback`` (ANSI colour codes stripped; captures
    the ThreadPoolExecutor workers too), and returns whatever ``main()``
    returns. The audit always auto-discovers and walks the whole line from the
    seed (parity with the standalone tool).
    """
    global options, hostlist, _INTERACTIVE
    options = argparse.Namespace(
        nodes=seed_host, file=None, user=(username or 'admin'),
        password=password, outfile=output_path, debug=debug,
    )
    hostlist = [seed_host]
    _INTERACTIVE = False

    sink = log_callback or print
    buf = {'s': ''}
    lock = threading.Lock()

    def _forward(*args, sep=' ', end='\n', flush=False, **_kwargs):
        text = _ANSI_RE.sub('', sep.join(str(a) for a in args) + end)
        with lock:
            buf['s'] += text
            while '\n' in buf['s']:
                line, buf['s'] = buf['s'].split('\n', 1)
                sink(line)
            # Progress prints use end=' ', flush=True; surface the partial line
            # so the panel updates live during a slow step.
            if flush and buf['s'].strip():
                sink(buf['s'])
                buf['s'] = ''

    # Shadow the module-global ``print`` so every print() in this module (incl.
    # the threaded per-node workers) routes to the GUI panel. Restored after.
    g = globals()
    g['print'] = _forward
    try:
        return main()
    finally:
        with lock:
            if buf['s']:
                sink(buf['s'])
                buf['s'] = ''
        g.pop('print', None)
        _session_close()


if __name__ == '__main__':
    try:
        _cli_bootstrap()
        main()
    except KeyboardInterrupt:
        print(f'\n{amber}Interrupted by user.{resetc}')
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
