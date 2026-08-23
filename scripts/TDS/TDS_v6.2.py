# Embedded file name: TDS_v6.2.py
import time
from time import strftime, gmtime
import sys
import os
import socket
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..'))
from utils.telnet import Telnet as _Telnet
from utils.helpers import (
    get_known_hosts_path as _get_known_hosts_path,
    safe_load_host_keys as _safe_load_host_keys,
    safe_save_host_keys as _safe_save_host_keys,
)
import re
import glob
import ipaddress
import logging
import csv
import argparse
from datetime import datetime
logging.basicConfig()
SCRIPT_VERSION = '6.2'
paramiko = None
tk = None
tkMessageBox = None


def _ensure_paramiko():
    global paramiko
    if paramiko is not None:
        return True
    t0 = time.time()
    try:
        import paramiko as _paramiko
        paramiko = _paramiko
        dt = time.time() - t0
        if dt > 5:
            print('[INIT] Paramiko import took %.1fs (environment may be slow).' % dt)
        return True
    except Exception as err:
        print('[INIT] Paramiko import failed: %s' % err)
        print('[INIT] Suggestion: reinstall paramiko in the active .venv and retry.')
        return False


def _ensure_tkinter():
    global tk
    global tkMessageBox
    if tk is not None and tkMessageBox is not None:
        return True
    try:
        import tkinter as _tk
        from tkinter import messagebox as _tkMessageBox
        tk = _tk
        tkMessageBox = _tkMessageBox
        return True
    except Exception:
        return False

dMSFT__SHELFID_PARAM = {}
CienaPC = 'YES'
PartnerPC = 'YES'
if CienaPC == 'NO':
    if PartnerPC == 'NO':
        print ('Incorrect password\n')
        print ('Multiple TID data collection with arbitrary username/password enabled \n')
    elif PartnerPC == 'YES':
        print ('XLSX file generation from a single/multiple TID with arbitrary username/password enabled')
# Lazy-init Tk only when needed (e.g., warning popups). Some environments
# can block on tk.Tk() during startup, which should not prevent CLI execution.
root = None

TIMEOUT = 120
PROMPT = '\r\n;\r\n'
SSH_SHELL_PROMPT = ''  # Set at login from the actual shell prompt (e.g. 'CN651174-INLAF# ')
RLS_SHELL_PROMPT = ''
REPORT = 'YES'
ISSUES = 'YES'
COMMENT = ''
inFile = 'NONE'
missingTID = 'NO'
print ('Script version:', SCRIPT_VERSION)
print ('\n*************************************************************************')
print ("*             Ciena 6500 / 6500 RLS Tester's Diagnostic Script           *")
print ('*      Authorized for internal use only. Distribution is prohibited      *')
print ('*    This tool is intended for use on Ciena 6500 and 6500 RLS devices    *')
print ('*************************************************************************\n')
def _parse_startup_args():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--host', dest='host', default='')
    parser.add_argument('--platform', dest='platform', default='')
    parser.add_argument('--username', dest='username', default='')
    parser.add_argument('--password', dest='password', default='')
    parser.add_argument('--file-name', dest='file_name', default='')
    parser.add_argument('--non-interactive', dest='non_interactive', action='store_true')
    parser.add_argument('--read-password-stdin', dest='read_password_stdin', action='store_true')
    parser.add_argument('--validate', dest='validate', action='store_true',
                        help='Run engineering validations after RLS collection')
    parser.add_argument('--walk-mode', dest='walk_mode', action='store_true',
                        help='One hop of a Network Audit walk; relaxes --file-name requirement')
    parser.add_argument('--hop', dest='hop', type=int, default=0,
                        help='Hop depth in walk (informational)')
    args, _ = parser.parse_known_args()
    return args


def _resolve_startup_inputs():
    args = _parse_startup_args()

    host = (args.host or '').strip()
    platform = (args.platform or '').strip().upper()
    user = (args.username or '').strip()
    
    # Password priority: stdin (most secure) > --password arg > TDS_PASSWORD env var
    password = ''
    if args.read_password_stdin:
        try:
            password = sys.stdin.readline().rstrip('\n')
        except Exception:
            password = ''
    elif args.password:
        password = args.password
    else:
        password = os.getenv('TDS_PASSWORD', '')
    expected_tid = (args.file_name or '').strip().upper()

    def _validate_host(value):
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return bool(re.match('^[A-Za-z0-9][A-Za-z0-9.-]*$', value) and '&' not in value)

    if host:
        if not _validate_host(host):
            print('Invalid --host value. Please enter a valid IP address or hostname.')
            sys.exit()
    else:
        print('Missing required --host.')
        sys.exit()

    if not platform:
        platform = 'RLS'
    if platform not in ('6500', 'RLS'):
        platform = 'RLS'

    if not user:
        print('Missing required --username.')
        sys.exit()

    if not password:
        print('Missing required password. Use --password or TDS_PASSWORD env var.')
        sys.exit()

    if not expected_tid:
        if args.walk_mode:
            expected_tid = ''  # discovered hosts may not have a known TID up front
        else:
            print('Missing required --file-name.')
            sys.exit()

    return (host, platform, user, password, expected_tid,
            bool(args.validate), bool(args.walk_mode), int(args.hop or 0))


HOST, PLATFORM_MODE, USER, PASS, EXPECTED_TID, RUN_VALIDATIONS, WALK_MODE, WALK_HOP = _resolve_startup_inputs()

if PLATFORM_MODE == '6500':
    METHOD = 'TELNET'
    PORT = '23'
else:
    METHOD = 'SSH'
    PORT = '22'

print('Login mode set from platform: %s on port %s' % (METHOD, PORT))
COLLECT = 'YES'
abs_path = os.getcwd()

def _recv_text(channel, nbytes):
    try:
        data = channel.recv(nbytes)
    except socket.timeout:
        return ''
    if isinstance(data, (bytes, bytearray)):
        return data.decode('utf-8', errors='ignore')
    return data


def _has_ssh_banner_prompt(text):
    if not text:
        return False
    trimmed = text.rstrip()
    if '< ' in text or text.endswith(PROMPT):
        return True
    if trimmed.endswith('#') or '\n#' in text or '\r#' in text:
        return True
    if trimmed.endswith('<') or '\n<' in text or '\r<' in text:
        return True
    return False


def _telnet_write(conn, data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    conn.write(data)

def _telnet_read_until(conn, expected, timeout):
    if isinstance(expected, str):
        expected = expected.encode('utf-8')
    data = conn.read_until(expected, timeout)
    if isinstance(data, (bytes, bytearray)):
        return data.decode('utf-8', errors='ignore')
    return data

def _telnet_expect(conn, patterns, timeout):
    byte_patterns = [p.encode('utf-8') if isinstance(p, str) else p for p in patterns]
    idx, match, data = conn.expect(byte_patterns, timeout)
    if isinstance(data, (bytes, bytearray)):
        data = data.decode('utf-8', errors='ignore')
    return idx, match, data
if PORT == '20002' or PORT == '22':
    PROMPT = ';\r\n< '
elif PORT == '23':
    PROMPT = '\r\n;\r\n<'
else:
    PROMPT = '\r\n;'

F_DBG = open(str(_get_known_hosts_path().parent / 'TDS_Debug.txt'), 'w')
F_DBG.write('\nScript Version = ' + SCRIPT_VERSION + '\n####################\n\n')
class _LazyWriter:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def write(self, text):
        if self.handle is None:
            self.handle = open(self.path, 'w')
        self.handle.write(text)
        self.handle.flush()

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


F_MISS = _LazyWriter(str(_get_known_hosts_path().parent / 'TDS_MissingTID.txt'))
WindowsHostName = HOST.replace(':', '^')
def LOGIN_SSH():
    global chan_6500
    if not _ensure_paramiko():
        return 'NO'
    try:
        ssh = paramiko.SSHClient()
        def LOGIN_SSH():
            global chan_6500
            if not _ensure_paramiko():
                return 'NO'
            try:
                _kh = str(_get_known_hosts_path())
                ssh = paramiko.SSHClient()
                _safe_load_host_keys(ssh, _kh)
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
                ssh.connect(HOST, port=int(PORT), username=USER, password=PASS, timeout=TIMEOUT)
                _safe_save_host_keys(ssh, _kh)
                chan_6500 = ssh.invoke_shell()
                return 'YES'
            except Exception as err:
                F_DBG.write('\nSSH Connection Error: %s' % str(err))
                F_MISS.write('\nSSH Connection Error: %s' % str(err))
                return 'NO'
        ssh.connect(HOST, port=int(PORT), username=USER, password=PASS, timeout=TIMEOUT)
        chan_6500 = ssh.invoke_shell()
        return 'YES'
    except Exception as err:
        F_DBG.write('\nSSH Connection Error: %s' % str(err))
        F_MISS.write('\nSSH Connection Error: %s' % str(err))
        return 'NO'

def LOGIN_TELNET():
    global telnet_6500
    try:
        # F004: Ciena 6500 TL1 prompt is only available on the Telnet listener;
        # SSH cannot speak TL1. Bypass policy here so this required path is not
        # blocked by the deny-all default.
        telnet_6500 = _Telnet(HOST, PORT, TIMEOUT,
                              bypass_policy=True, purpose="tl1-6500")
        return 'YES'
    except Exception as err:
        F_DBG.write('\nTELNET Connection Error: %s' % str(err))
        F_MISS.write('\nTELNET Connection Error: %s' % str(err))
        return 'NO'

def MCEMON_STATUS(mcemonPort):
    mcemon = 'OFF'
    mTimeout = 10
    wasConnected = 'NO'
    if METHOD == 'SSH':
        pass
    elif METHOD == 'TELNET':
        pass
    wasConnected = 'YES'


def RLS_LOGIN_SSH():
    global rls_ssh_client
    global rls_chan
    global RLS_SHELL_PROMPT
    if not _ensure_paramiko():
        return 'NO'
    try:
        _kh = str(_get_known_hosts_path())
        rls_ssh_client = paramiko.SSHClient()
        _safe_load_host_keys(rls_ssh_client, _kh)
        rls_ssh_client.set_missing_host_key_policy(paramiko.RejectPolicy())
        rls_ssh_client.connect(HOST, port=int(PORT), username=USER, password=PASS, timeout=TIMEOUT, look_for_keys=False, allow_agent=False)
        _safe_save_host_keys(rls_ssh_client, _kh)
        rls_chan = rls_ssh_client.invoke_shell()
        time.sleep(2)
        banner = ''
        while rls_chan.recv_ready():
            banner += _recv_text(rls_chan, 32768)
            time.sleep(0.1)
        banner = banner.replace('\r', '')
        lines = [line.strip() for line in banner.splitlines() if line.strip()]
        if lines:
            last_line = lines[-1]
            if last_line.endswith('#') or last_line.endswith('>') or last_line.endswith('$'):
                RLS_SHELL_PROMPT = last_line
        F_DBG.write('\nRLS SSH banner:\n' + banner + '\n')
        return 'YES'
    except Exception as err:
        F_DBG.write('\nRLS SSH Connection Error: %s' % str(err))
        F_MISS.write('\nRLS SSH Connection Error: %s' % str(err))
        return 'NO'


def RLS_LOGOUT_SSH():
    global rls_ssh_client
    global rls_chan
    logout_attempted = False
    try:
        if 'rls_chan' in globals() and rls_chan is not None:
            logout_attempted = True
            for cmd in ('logout', 'exit'):
                try:
                    rls_chan.send(cmd + '\n')
                    time.sleep(0.2)
                except Exception:
                    pass
            try:
                if rls_chan.recv_ready():
                    _recv_text(rls_chan, 32768)
            except Exception:
                pass
            try:
                rls_chan.close()
            except Exception:
                pass
            rls_chan = None
    finally:
        try:
            if 'rls_ssh_client' in globals() and rls_ssh_client is not None:
                rls_ssh_client.close()
                rls_ssh_client = None
        except Exception:
            pass
    if logout_attempted:
        print('RLS session logged out.')
        try:
            F_DBG.write('\nRLS session logged out.\n')
        except Exception:
            pass


def _rls_prompt_seen(text):
    global RLS_SHELL_PROMPT
    if not text:
        return False
    trimmed = text.rstrip()
    if not trimmed:
        return False
    last_line = trimmed.splitlines()[-1].strip()
    if RLS_SHELL_PROMPT and last_line.endswith(RLS_SHELL_PROMPT):
        return True
    return last_line.endswith('#') or last_line.endswith('>') or last_line.endswith('$')


def _sanitize_rls_output(output):
    if not output:
        return ''
    output = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', output)
    output = output.replace('\r', '')
    output = output.replace('\x08 \x08', '')
    output = output.replace('--More--', '')
    return output


def _rls_meaningful_lines(output, command=''):
    lines = []
    for line in (output or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if command and stripped == command.strip():
            continue
        if _rls_prompt_seen(stripped):
            continue
        lines.append(stripped)
    return lines


def _classify_rls_output(output, command=''):
    text = _sanitize_rls_output(output)
    upper_text = text.upper()
    if 'CLI SYNTAX ERROR' in upper_text or 'UNKNOWN KEYWORD' in upper_text:
        return 'WARN', 'syntax error'
    if 'COMMAND ERROR' in upper_text or 'TRACEBACK' in upper_text or 'PERMISSION DENIED' in upper_text or 'INCOMPLETE COMMAND' in upper_text:
        return 'WARN', 'command failure'
    meaningful = _rls_meaningful_lines(text, command)
    if not meaningful:
        return 'WARN', 'echo only / incomplete capture'
    return 'OK', 'response captured'


def _drain_rls_channel(max_wait=1.0):
    drained = ''
    end_time = time.time() + max_wait
    while time.time() < end_time:
        if rls_chan.recv_ready():
            drained += _recv_text(rls_chan, 32768)
            end_time = time.time() + 0.2
        else:
            time.sleep(0.05)
    return _sanitize_rls_output(drained)


def _sync_rls_prompt(timeout=3.0):
    output = _drain_rls_channel(0.5)
    if _rls_prompt_seen(output):
        return output
    rls_chan.send('\n')
    end_time = time.time() + timeout
    while time.time() < end_time:
        if rls_chan.recv_ready():
            output += _recv_text(rls_chan, 32768)
            output = _sanitize_rls_output(output)
            if _rls_prompt_seen(output):
                break
        else:
            time.sleep(0.1)
    return _sanitize_rls_output(output)


def RLS_CMD(command, settle_time=0.6, timeout=20):
    _sync_rls_prompt(2.0)
    output = ''
    rls_chan.send(command + '\n')
    end_time = time.time() + timeout
    idle_loops = 0
    saw_meaningful = False
    while time.time() < end_time:
        if rls_chan.recv_ready():
            chunk = _recv_text(rls_chan, 32768)
            if chunk:
                output += chunk
                if '--More--' in chunk:
                    rls_chan.send(' ')
                if _rls_meaningful_lines(_sanitize_rls_output(output), command):
                    saw_meaningful = True
                idle_loops = 0
            time.sleep(0.1)
        else:
            idle_loops += 1
            time.sleep(max(settle_time / 3, 0.2))
            cleaned = _sanitize_rls_output(output)
            if saw_meaningful and _rls_prompt_seen(cleaned) and idle_loops >= 3:
                break

    cleaned = _sanitize_rls_output(output)
    if not _rls_meaningful_lines(cleaned, command):
        grace_end = time.time() + min(6.0, timeout)
        while time.time() < grace_end:
            if rls_chan.recv_ready():
                chunk = _recv_text(rls_chan, 32768)
                if chunk:
                    output += chunk
                    cleaned = _sanitize_rls_output(output)
                    if _rls_meaningful_lines(cleaned, command) and _rls_prompt_seen(cleaned):
                        break
            else:
                time.sleep(0.2)
    return _sanitize_rls_output(output)


def _extract_rls_slot_details(show_slots_output):
    slot_details = []
    seen_slots = set()
    current_slot = None
    current_form = ''

    def _append_current():
        if current_slot and current_form in ('access-panel', 'ctm', 'fan', 'power') and current_slot not in seen_slots:
            slot_details.append((current_slot, current_form))
            seen_slots.add(current_slot)

    for raw_line in (show_slots_output or '').splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(' '))
        if indent == 2 and stripped.startswith('- name'):
            _append_current()
            match = re.search(r':\s*([0-9]+)\s*$', stripped)
            current_slot = match.group(1) if match else None
            current_form = ''
        elif current_slot and indent == 4 and stripped.startswith('form-factor'):
            current_form = stripped.split(':', 1)[1].strip().lower()
    _append_current()
    return sorted(slot_details, key=lambda item: int(item[0]))


def _extract_rls_lldp_interfaces(lldp_output):
    interfaces = []
    neighbor_map = {}
    current_iface = None
    for raw_line in (lldp_output or '').splitlines():
        stripped = raw_line.strip()
        if raw_line.startswith('      - name'):
            match = re.search(r':\s*([A-Za-z0-9_.-]+)\s*$', stripped)
            if match:
                current_iface = match.group(1)
                if current_iface not in interfaces:
                    interfaces.append(current_iface)
                if current_iface not in neighbor_map:
                    neighbor_map[current_iface] = False
        elif stripped.startswith('neighbors:') and current_iface:
            neighbor_map[current_iface] = True
    return interfaces, neighbor_map


def _record_rls_command(summary_writer, WindowsHost, group_name, label, cmd, cmd_timeout, total_ok, total_warn):
    safe_name = re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_')
    out_path = WindowsHost + '_RLS_' + safe_name + '.csv'
    try:
        output = RLS_CMD(cmd, timeout=cmd_timeout)
    except Exception as err:
        output = 'COMMAND ERROR: %s' % str(err)
    with open(out_path, 'w', newline='') as f_out:
        writer = csv.writer(f_out)
        writer.writerow(['command', 'line_number', 'output'])
        output_lines = output.splitlines()
        if output_lines:
            for idx, line in enumerate(output_lines, 1):
                writer.writerow([cmd, idx, line])
        else:
            writer.writerow([cmd, 1, ''])
    print('Created CSV: ' + os.path.basename(out_path))
    status, note = _classify_rls_output(output, cmd)
    if status == 'OK':
        total_ok += 1
    else:
        total_warn += 1
    first_line = ''
    for line in output.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    summary_writer.writerow([group_name, status, cmd, out_path, note, first_line])
    return output, total_ok, total_warn


def RLS_SMOKE_TEST(WindowsHost):
    command_groups = [
        ('software', [
            ('software_summary', 'show software', 20),
            ('active_version', 'show software active-version', 15),
            ('running_version', 'show software running-version', 15),
            ('committed_version', 'show software committed-version', 15),
            ('upgrade_state', 'show software upgrade-operational-state', 15),
            ('upgrade_target', 'show software upgrade-to-version', 15),
            ('operation_info', 'show software operation-info', 20),
            ('ztp', 'show ztp', 15),
            ('ztp_admin_state', 'show ztp admin-state', 15),
        ]),
        ('platform', [
            ('shelf', 'show shelf', 15),
            ('system', 'show system', 15),
            ('lldp', 'show lldp', 20),
            ('alarm_history', 'show alarm-history', 20),
            ('alarm_counts', 'show alarm-counts', 15),
        ]),
        ('logging_pm', [
            ('logs_remote_config', 'show logs remote-config', 20),
            ('logs_retrieve_status', 'show logs retrieve-log-status', 20),
            ('command_log', 'show command-log', 20),
            ('syslog_history', 'syslog-history level 0', 20),
            ('pm_current', 'show pm current', 20),
            ('pm_history', 'show pm historical', 20),
            ('pm_tca', 'show-pm-tca', 20),
            ('osrp_snc_diagnostics', 'action osrp ALL object snc ALL show-snc-diagnostics', 25),
            ('osrp_sncg_diagnostics', 'action osrp ALL object snc-group ALL show-snc-group-diagnostics', 25),
        ]),
        ('hardware', [
            ('all_slots', 'show slots', 30),
        ]),
    ]

    summary_path = WindowsHost + '_RLS_Smoke_Summary.csv'
    total_ok = 0
    total_warn = 0
    slot_details = []
    lldp_interfaces = []
    lldp_neighbor_map = {}
    with open(summary_path, 'w', newline='') as f_sum:
        summary_writer = csv.writer(f_sum)
        summary_writer.writerow(['Group', 'Status', 'Command', 'Output File', 'Note', 'First Non-Empty Line'])
        print('Created CSV: ' + os.path.basename(summary_path))
        for group_name, commands in command_groups:
            for label, cmd, cmd_timeout in commands:
                output, total_ok, total_warn = _record_rls_command(summary_writer, WindowsHost, group_name.upper(), label, cmd, cmd_timeout, total_ok, total_warn)
                if label == 'all_slots':
                    slot_details = _extract_rls_slot_details(output)
                elif label == 'lldp':
                    lldp_interfaces, lldp_neighbor_map = _extract_rls_lldp_interfaces(output)

        if not lldp_interfaces:
            lldp_interfaces = ['colan-x', 'colan-a', 'ilan-in1', 'ilan-out1', 'ilan-in2', 'ilan-out2', 'osc-1-50-1']
            lldp_neighbor_map = {'colan-x': True, 'osc-1-50-1': True}

        summary_writer.writerow(['DISCOVERED_INTERFACES', 'INFO', 'selected_interfaces', '', '', ', '.join(lldp_interfaces[:8])])

        for iface in lldp_interfaces[:8]:
            detail_commands = [
                ('lldp_' + iface + '_state', 'show lldp interfaces interface ' + iface + ' state', 15),
            ]
            if lldp_neighbor_map.get(iface, False):
                detail_commands.append(('lldp_' + iface + '_neighbors', 'show lldp interfaces interface ' + iface + ' neighbors', 15))

            for label, cmd, cmd_timeout in detail_commands:
                _, total_ok, total_warn = _record_rls_command(summary_writer, WindowsHost, 'NETWORK_DETAILS', label, cmd, cmd_timeout, total_ok, total_warn)

        if not slot_details:
            slot_details = [
                ('40', 'access-panel'),
                ('41', 'ctm'),
                ('42', 'ctm'),
                ('51', 'fan'),
                ('52', 'fan'),
                ('61', 'power'),
                ('62', 'power'),
            ]

        summary_writer.writerow(['DISCOVERED_HARDWARE', 'INFO', 'selected_slots', '', '', ', '.join([slot + ' (' + form + ')' for slot, form in slot_details])])

        for slot, form_factor in slot_details:
            detail_commands = [
                ('slot_' + slot + '_inventory', 'show slots ' + slot + ' inventory', 20),
            ]
            if form_factor in ('access-panel', 'ctm'):
                detail_commands.append(('slot_' + slot + '_config_circuit_pack', 'show slots ' + slot + ' config circuit-pack', 20))
                detail_commands.append(('slot_' + slot + '_oper_state', 'show slots ' + slot + ' inventory circuit-pack operational-state', 20))
                detail_commands.append(('slot_' + slot + '_software_component', 'show software component ' + slot, 20))

            for label, cmd, cmd_timeout in detail_commands:
                _, total_ok, total_warn = _record_rls_command(summary_writer, WindowsHost, 'HARDWARE_DETAILS', label, cmd, cmd_timeout, total_ok, total_warn)

        summary_writer.writerow(['TOTALS', 'OK', 'OK_COUNT', '', '', str(total_ok)])
        summary_writer.writerow(['TOTALS', 'WARN', 'WARN_COUNT', '', '', str(total_warn)])
    return ''


def DETECT_6500_VARIANT(WindowsHost):
    rls_artifacts = [
        WindowsHost + '_RLS_Smoke_Summary.csv',
        WindowsHost + '_RLS_Smoke_Summary.txt',
        WindowsHost + '_RLS_Report.csv',
    ]
    for path in rls_artifacts:
        if os.path.exists(path):
            return 'RLS'

    try:
        with open(WindowsHost + '.txt', 'r', errors='ignore') as f_in:
            data = f_in.read().upper()
    except Exception:
        return 'UNKNOWN'

    rls_markers = [
        'RECONFIGURABLE LINE SYSTEM',
        '6500 RLS',
        'RLS RELEASE',
        'RLS-OS',
    ]
    for marker in rls_markers:
        if marker in data:
            return 'RLS'
    return '6500'


def _read_rls_file(path):
    try:
        with open(path, 'r', errors='ignore') as f_in:
            return f_in.read()
    except Exception:
        return ''


def _rls_artifact_path(base_path):
    csv_path = base_path + '.csv'
    txt_path = base_path + '.txt'
    if os.path.exists(csv_path):
        return csv_path
    if os.path.exists(txt_path):
        return txt_path
    return csv_path


def _read_rls_artifact(base_path):
    path = _rls_artifact_path(base_path)
    if path.lower().endswith('.csv'):
        try:
            with open(path, 'r', newline='', errors='ignore') as f_csv:
                rows = list(csv.reader(f_csv))
            if rows and rows[0][:3] == ['command', 'line_number', 'output']:
                return '\n'.join([row[2] if len(row) > 2 else '' for row in rows[1:]])
        except Exception:
            pass
    return _read_rls_file(path)


def _read_rls_summary_counts(base_path):
    path = _rls_artifact_path(base_path)
    if path.lower().endswith('.csv'):
        total_ok = 0
        total_warn = 0
        try:
            with open(path, 'r', newline='', errors='ignore') as f_csv:
                reader = csv.reader(f_csv)
                next(reader, None)
                for row in reader:
                    if len(row) >= 6 and row[0] == 'TOTALS' and row[2] == 'OK_COUNT':
                        total_ok = _safe_int(row[5])
                    elif len(row) >= 6 and row[0] == 'TOTALS' and row[2] == 'WARN_COUNT':
                        total_warn = _safe_int(row[5])
        except Exception:
            pass
        return total_ok, total_warn
    text = _read_rls_file(path)
    ok_match = re.search(r'(?m)^OK =\s*(\d+)\s*$', text)
    warn_match = re.search(r'(?m)^WARN =\s*(\d+)\s*$', text)
    return _safe_int(ok_match.group(1) if ok_match else 0), _safe_int(warn_match.group(1) if warn_match else 0)


def _extract_rls_summary_commands(base_path):
    commands = []
    path = _rls_artifact_path(base_path)
    if not path.lower().endswith('.csv'):
        return commands
    try:
        with open(path, 'r', newline='', errors='ignore') as f_csv:
            reader = csv.reader(f_csv)
            next(reader, None)
            for row in reader:
                if len(row) < 6:
                    continue
                group_name = (row[0] or '').strip()
                status = (row[1] or '').strip()
                command = (row[2] or '').strip()
                note = (row[4] or '').strip()
                if not command:
                    continue
                if group_name in ('TOTALS', 'DISCOVERED_INTERFACES', 'DISCOVERED_HARDWARE'):
                    continue
                commands.append((group_name, status, command, note))
    except Exception:
        pass
    return commands


def _extract_rls_device_command_log_entries(command_log_text, target_user='', max_entries=5000, session_gap_minutes=45):
    entries = []
    target_user_norm = (target_user or '').strip().lower()

    def _parse_timestamp(value):
        if not value:
            return None
        cleaned = value.strip().replace(' ', 'T').replace('Z', '+00:00').replace(',', '.')
        if re.search(r'[+-]\d{4}$', cleaned):
            cleaned = cleaned[:-5] + cleaned[-5:-2] + ':' + cleaned[-2:]
        try:
            return datetime.fromisoformat(cleaned)
        except Exception:
            return None

    def _looks_like_session_start(text):
        text = (text or '').lower()
        return bool(re.search(r'\b(login|logged\s+in|authentication\s+ok|session\s+(started|opened)|connected)\b', text))

    def _looks_like_session_end(text):
        text = (text or '').lower()
        return bool(re.search(r'\b(logout|logged\s+out|session\s+(closed|ended)|disconnect(ed)?|exit)\b', text))

    def _matches_target_user(entry):
        if not target_user_norm:
            return True
        entry_user_norm = (entry[1] or '').strip().lower()
        if entry_user_norm and entry_user_norm == target_user_norm:
            return True
        raw = (entry[3] or '').lower()
        return bool(re.search(r'(?i)\b(?:user|username|login-user|principal)\s*[:=]\s*' + re.escape(target_user_norm) + r'\b', raw))

    for raw_line in (command_log_text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == 'show command-log':
            continue
        if set(line) <= set('-=_.'):
            continue

        timestamp = ''
        user = ''
        command = ''

        ts_match = re.search(r'\b\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:z|[+-]\d{2}:?\d{2})?\b', line, re.IGNORECASE)
        if ts_match:
            timestamp = ts_match.group(0)

        user_match = re.search(r'(?i)\b(?:user|username|login-user|principal)\s*[:=]\s*([^,;\s]+)', line)
        if user_match:
            user = user_match.group(1).strip()

        cmd_match = re.search(r'(?i)\b(?:command|cmd|cli|input)\s*[:=]\s*(.+)$', line)
        if cmd_match:
            command = cmd_match.group(1).strip()
        else:
            segments = re.split(r'\s{2,}|\s+-\s+', line)
            if segments:
                candidate = segments[-1].strip()
                if candidate and len(candidate) <= len(line):
                    command = candidate

        if not command:
            command = line

        # Skip command-log query metadata rows; they are not operator commands.
        if re.match(r'(?i)^\s*(start-date-time|end-date-time)\s*[:=]', command):
            continue
        if re.search(r'(?i)\b(start-date-time|end-date-time)\b\s*[:=]', line):
            continue

        entries.append((timestamp, user, command, line))
        if len(entries) >= max_entries:
            break

    if not entries:
        return entries

    first_dt = _parse_timestamp(entries[0][0])
    last_dt = _parse_timestamp(entries[-1][0])
    is_descending = bool(first_dt and last_dt and first_dt >= last_dt)

    newest_to_oldest = entries if is_descending else list(reversed(entries))
    anchor_index = 0
    for idx, item in enumerate(newest_to_oldest):
        if item[0] and _parse_timestamp(item[0]) is not None:
            anchor_index = idx
            break

    selected_newest_to_oldest = []
    last_ts = None
    found_session_start = False

    for idx in range(anchor_index, len(newest_to_oldest)):
        item = newest_to_oldest[idx]
        explicit_user = (item[1] or '').strip().lower()
        item_ts = _parse_timestamp(item[0])

        if target_user_norm and explicit_user and explicit_user != target_user_norm:
            break

        if last_ts is not None and item_ts is not None:
            gap = last_ts - item_ts
            if gap.total_seconds() > session_gap_minutes * 60:
                break

        selected_newest_to_oldest.append(item)

        if idx > anchor_index and (_looks_like_session_start(item[2]) or _looks_like_session_start(item[3])) and _matches_target_user(item):
            found_session_start = True
            break

        if item_ts is not None:
            last_ts = item_ts

    # If user filtering removed everything, fall back to the newest contiguous timestamp block.
    if not selected_newest_to_oldest:
        selected_newest_to_oldest = []
        last_ts = None
        for idx in range(anchor_index, len(newest_to_oldest)):
            item = newest_to_oldest[idx]
            item_ts = _parse_timestamp(item[0])
            if last_ts is not None and item_ts is not None:
                gap = last_ts - item_ts
                if gap.total_seconds() > session_gap_minutes * 60:
                    break
            selected_newest_to_oldest.append(item)
            if item_ts is not None:
                last_ts = item_ts

    if is_descending:
        return selected_newest_to_oldest
    return list(reversed(selected_newest_to_oldest))


def _extract_rls_field(text, field_name):
    if not text:
        return ''
    match = re.search(r'(?im)^\s*' + re.escape(field_name) + r'\s*:\s*(.*?)\s*$', text)
    if match:
        return match.group(1).strip()
    return ''


def _normalize_rls_tid_label(value):
    value = str(value or '').strip()
    value = re.sub(r'(_RLS)+$', '', value, flags=re.IGNORECASE)
    value = re.sub(r'[>#;$]+\s*$', '', value).strip()
    if not value:
        return ''
    if re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$', value):
        return value.upper()
    return value


def _extract_tid_from_prompt(prompt_text):
    prompt_text = _normalize_rls_tid_label(prompt_text)
    if not prompt_text:
        return ''
    candidate = prompt_text.split()[-1]
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', candidate):
        return ''
    if re.match(r'^[A-Z0-9][A-Z0-9_.-]{2,}$', candidate):
        return candidate
    return ''


def _looks_like_component_label(value):
    value = _normalize_rls_tid_label(value)
    if not value:
        return False
    blocked_prefixes = (
        'SLOT-', 'SHELF-', 'OSC-', 'OTS-', 'OSID-', 'TX-', 'RX-', 'FE-',
        'ETTP-', 'OTUTTP-', 'ODUTTP-', 'CHMON-', 'NMCMON-', 'OPTMON-', 'SDMON-'
    )
    if value.startswith(blocked_prefixes):
        return True
    if re.match(r'^(SLOT|SHELF|OSC|OTS|OSID|TX|RX|FE|ETTP|OTUTTP|ODUTTP|CHMON|NMCMON|OPTMON|SDMON)[-_]?[A-Z0-9]+$', value):
        return True
    return False


def _is_probable_rls_tid(value):
    value = _normalize_rls_tid_label(value)
    if not value:
        return False
    if value in ('UNKNOWN', 'NONE', 'N/A', 'AUTO'):
        return False
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', value):
        return False
    if _looks_like_component_label(value):
        return False
    return bool(re.match(r'^[A-Z0-9][A-Z0-9_.-]{2,}$', value))


def _derive_rls_tid_label(host, expected_tid='', system_text='', shelf_text=''):
    expected_tid_clean = _normalize_rls_tid_label(expected_tid)
    if expected_tid_clean:
        return expected_tid_clean
    return ''


def _safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def _safe_float(value, default=0.0):
    try:
        return float(str(value).strip())
    except Exception:
        return default


def _extract_rls_alarm_highlights(text, max_items=5):
    if not text:
        return []
    highlights = []
    seen = set()
    blocks = re.split(r'(?m)^\s*-\s*history-id\s*:\s*\d+\s*$', text)
    for block in blocks[1:]:
        severity = _extract_rls_field(block, 'severity')
        cause = _extract_rls_field(block, 'cause') or _extract_rls_field(block, 'name')
        resource = _extract_rls_field(block, 'resource')
        info = _extract_rls_field(block, 'additional-info')
        if not severity and not cause:
            continue
        if (cause or '').lower() in ('slot empty', 'circuit pack unknown'):
            continue
        signature = (severity, cause, resource, info)
        if signature in seen:
            continue
        seen.add(signature)
        parts = [severity.upper() if severity else 'INFO', cause or 'unspecified']
        if resource:
            parts.append(resource)
        if info:
            parts.append(info)
        highlights.append(' | '.join(parts))
        if len(highlights) >= max_items:
            break
    return highlights


def _extract_rls_neighbor_details(WindowsHost, interfaces):
    details = []
    for iface in interfaces:
        safe_iface = re.sub(r'[^A-Za-z0-9]+', '_', iface).strip('_')
        text = _read_rls_artifact(WindowsHost + '_RLS_lldp_' + safe_iface + '_neighbors')
        if not text:
            continue
        system_name = _extract_rls_field(text, 'system-name') or 'unknown'
        mgmt_addr = _extract_rls_field(text, 'management-address') or 'n/a'
        port_id = _extract_rls_field(text, 'port-description') or _extract_rls_field(text, 'port-id') or 'n/a'
        details.append((iface, system_name, mgmt_addr, port_id))
    return details


def _extract_rls_interface_context(text, iface, radius=20):
    if not text:
        return ''
    iface = str(iface or '').strip().lower()
    if not iface:
        return text
    lines = text.splitlines()
    windows = []
    seen = set()
    tokens = [iface, iface.replace('-', '/'), iface.replace('-', ' ')]
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if any(token and token in lowered for token in tokens):
            start = max(0, idx - radius)
            end = min(len(lines), idx + radius + 1)
            key = (start, end)
            if key not in seen:
                seen.add(key)
                windows.append('\n'.join(lines[start:end]))
    return '\n\n'.join(windows) if windows else text


def _extract_rls_named_metric_value(text, metric_patterns, min_value=-80.0, max_value=40.0):
    if not text:
        return ''
    for metric in metric_patterns:
        patterns = [
            r'(?im)^\s*' + metric + r'\s*[:=]\s*(-?\d+(?:\.\d+)?)\s*(?:dBm|dbm|dB|db)?\s*$',
            r'(?ims)' + metric + r'.{0,120}?\b(?:current|value|measured|actual|untimed|average)?\b.{0,40}?(-?\d+(?:\.\d+)?)\s*(?:dBm|dbm|dB|db)?',
            r'(?ims)' + metric + r'.{0,80}?(-?\d+(?:\.\d+)?)\s*(?:dBm|dbm|dB|db)?',
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).strip()
                numeric = _safe_float(value, 9999.0)
                if min_value <= numeric <= max_value:
                    return value
    return ''


def _format_rls_metric_value(value, unit):
    value = str(value or '').strip()
    if not value:
        return ''
    numeric_match = re.search(r'-?\d+(?:\.\d+)?', value)
    if not numeric_match:
        return ''
    numeric = numeric_match.group(0)
    number = _safe_float(numeric, 9999.0)
    if unit.lower() == 'dbm':
        if not (-80.0 <= number <= 40.0):
            return ''
        formatted = ('%.2f' % number).rstrip('0').rstrip('.')
        return formatted + ' dBm'
    if unit.lower() == 'db':
        if not (0.0 <= abs(number) <= 80.0):
            return ''
        formatted = ('%.2f' % abs(number)).rstrip('0').rstrip('.')
        return formatted + ' dB'
    formatted = ('%.2f' % number).rstrip('0').rstrip('.')
    return formatted + ' ' + unit


def _extract_rls_osc_power_metrics(pm_current_text, pm_history_text, iface=''):
    search_text = '\n'.join([pm_current_text or '', pm_history_text or ''])
    if not search_text.strip():
        return '', '', ''

    candidate_texts = []
    scoped_text = _extract_rls_interface_context(search_text, iface)
    if scoped_text:
        candidate_texts.append(scoped_text)
    if 'osc' in str(iface or '').lower():
        osc_text = _extract_rls_interface_context(search_text, 'osc')
        if osc_text and osc_text not in candidate_texts:
            candidate_texts.append(osc_text)
    if search_text not in candidate_texts:
        candidate_texts.append(search_text)

    tx_labels = [
        r'untimed[\s-]*tx[\s-]*power(?:[\s-]*value)?',
        r'current(?:[\s-]*15[\s-]*minutes)?[\s-]*tx[\s-]*power(?:[\s-]*value)?',
        r'tx[\s-]*actual[\s-]*power',
        r'actual[\s-]*tx[\s-]*power',
        r'current[\s-]*tx[\s-]*power',
        r'\btx[\s-]*power\b',
    ]
    rx_labels = [
        r'untimed[\s-]*rx[\s-]*power(?:[\s-]*level)?',
        r'current(?:[\s-]*15[\s-]*minutes)?[\s-]*rx[\s-]*power(?:[\s-]*level)?',
        r'rx[\s-]*actual[\s-]*power',
        r'actual[\s-]*rx[\s-]*power',
        r'nominal[\s-]*rx[\s-]*power',
        r'\brx[\s-]*power\b',
    ]
    loss_labels = [
        r'rx[\s-]*cord[\s-]*loss',
        r'span[\s-]*loss',
        r'total[\s-]*fiber[\s-]*loss',
    ]

    tx_value = ''
    rx_value = ''
    loss_value = ''
    for candidate_text in candidate_texts:
        if not tx_value:
            tx_value = _extract_rls_named_metric_value(candidate_text, tx_labels, -80.0, 40.0)
        if not rx_value:
            rx_value = _extract_rls_named_metric_value(candidate_text, rx_labels, -80.0, 40.0)
        if not loss_value:
            loss_value = _extract_rls_named_metric_value(candidate_text, loss_labels, 0.0, 80.0)
        if tx_value and rx_value and loss_value:
            break

    tx_power = _format_rls_metric_value(tx_value, 'dBm')
    rx_power = _format_rls_metric_value(rx_value, 'dBm')
    rx_cord_loss = _format_rls_metric_value(loss_value, 'dB')

    if not rx_cord_loss and tx_power and rx_power:
        tx_match = re.search(r'-?\d+(?:\.\d+)?', tx_power)
        rx_match = re.search(r'-?\d+(?:\.\d+)?', rx_power)
        if tx_match and rx_match:
            tx_number = _safe_float(tx_match.group(0), 0.0)
            rx_number = _safe_float(rx_match.group(0), 0.0)
            if tx_number >= rx_number:
                rx_cord_loss = _format_rls_metric_value(str(tx_number - rx_number), 'dB')

    return tx_power, rx_power, rx_cord_loss


def _write_rls_csv(path, headers, rows):
    with open(path, 'w', newline='') as f_csv:
        writer = csv.writer(f_csv)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    print('Created CSV: ' + os.path.basename(path))


def _autosize_worksheet_columns(worksheet, rows, min_width=8, max_width=80, padding=2):
    col_widths = []
    for row in rows:
        for col_idx, value in enumerate(row):
            text = str(value or '')
            line_width = max([len(line) for line in text.splitlines()] or [0])
            if col_idx >= len(col_widths):
                col_widths.extend([0] * (col_idx - len(col_widths) + 1))
            if line_width > col_widths[col_idx]:
                col_widths[col_idx] = line_width
    for col_idx, width in enumerate(col_widths):
        worksheet.set_column(col_idx, col_idx, min(max_width, max(min_width, width + padding)))


def _reserve_workbook_path(preferred_path):
    if not os.path.exists(preferred_path):
        return preferred_path
    try:
        os.rename(preferred_path, preferred_path)
        return preferred_path
    except OSError as err:
        root, ext = os.path.splitext(preferred_path)
        stamp = strftime('%Y%m%d_%H%M%S')
        candidate = root + '_' + stamp + ext
        counter = 1
        while os.path.exists(candidate):
            candidate = root + '_' + stamp + '_' + str(counter) + ext
            counter += 1
        try:
            F_DBG.write('\nWorkbook in use, saving to alternate file: %s (%s)\n' % (candidate, str(err)))
        except Exception:
            pass
        print('Workbook in use; saving to alternate file: ' + os.path.basename(candidate))
        return candidate


def _consolidate_rls_csv_to_debug_xlsx(WindowsHost, tid_label=''):
    try:
        from xlsxwriter.workbook import Workbook
    except Exception as err:
        F_DBG.write('\nRLS Debug XLSX generation unavailable: %s\n' % str(err))
        return ''

    workbook_label = _normalize_rls_tid_label(tid_label or WindowsHost) or _normalize_rls_tid_label(WindowsHost)
    workbook_label = re.sub(r'[\\/:*?"<>|]+', '_', workbook_label)
    output_file = _reserve_workbook_path(WindowsHost + '_Debug.xlsx')

    try:
        workbook = Workbook(output_file)
    except Exception as err:
        output_file = _reserve_workbook_path(WindowsHost + '_Debug_' + strftime('%Y%m%d_%H%M%S') + '.xlsx')
        try:
            workbook = Workbook(output_file)
        except Exception as err:
            try:
                F_DBG.write('\nRLS Debug workbook creation failed for %s: %s\n' % (output_file, str(err)))
            except Exception:
                pass
            return ''

    header_format = workbook.add_format({'bold': True, 'font_color': 'white'})
    header_format.set_bg_color('black')
    link_format = workbook.add_format({'font_color': 'blue', 'underline': 1})
    header_link_format = workbook.add_format({'bold': True, 'font_color': 'blue', 'underline': 1})
    header_link_format.set_bg_color('black')

    display_tid = workbook_label
    capture_stamp = strftime('%Y-%m-%d @ %H:%M:%S')

    index_sheet = workbook.add_worksheet('Index')
    index_sheet.set_column(0, 0, 24)
    index_sheet.set_column(1, 1, 72)
    index_sheet.merge_range(0, 0, 0, 1, 'Debug Workbook - Capture Time = ' + capture_stamp, header_format)
    index_sheet.merge_range(2, 0, 2, 1, 'TID IP = ' + WindowsHost, header_format)
    index_sheet.merge_range(3, 0, 3, 1, 'TID Name = ' + display_tid, header_format)

    debug_sheets = {
        WindowsHost + '_RLS_System_Health.csv': ('System_Health', 'CPU, memory, logging, and notifications'),
        WindowsHost + '_RLS_Report.csv': ('Report', 'Consolidated RLS diagnostic report'),
        WindowsHost + '_RLS_Detected.csv': ('Detected', 'Detected platform family and artifact list'),
        WindowsHost + '_RLS_Smoke_Summary.csv': ('Smoke_Summary', 'Command capture completion summary'),
    }
    
    raw_tabs_map = {
        'alarm_history': ('raw_alarm_history', 'Raw alarm history evidence'),
        'all_slots': ('raw_all_slots', 'Raw slot inventory evidence'),
        'lldp': ('raw_lldp', 'Raw LLDP evidence'),
        'operation_info': ('raw_operation_info', 'Raw software operation evidence'),
    }

    all_csv_paths = glob.glob(WindowsHost + '_RLS_*.csv')
    used_sheet_names = {'Index'}
    row_index = 5

    for csv_path in sorted(all_csv_paths):
        if not os.path.exists(csv_path):
            continue
        
        if csv_path in debug_sheets:
            sheet_name, description = debug_sheets[csv_path]
        else:
            base_name = os.path.splitext(os.path.basename(csv_path))[0]
            prefix = WindowsHost + '_RLS_'
            if base_name.startswith(prefix):
                base_name = base_name[len(prefix):]
            if base_name in raw_tabs_map:
                sheet_name, description = raw_tabs_map[base_name]
            else:
                continue

        sheet_name = sheet_name[:31]
        candidate = sheet_name
        suffix = 1
        while candidate in used_sheet_names:
            tag = '_' + str(suffix)
            candidate = sheet_name[:31 - len(tag)] + tag
            suffix += 1
        sheet_name = candidate
        used_sheet_names.add(sheet_name)

        index_sheet.write_url(row_index, 0, 'internal:' + sheet_name + '!A1', link_format, sheet_name)
        index_sheet.write(row_index, 1, description)
        
        with open(csv_path, 'r', newline='', errors='ignore') as f_csv:
            reader = csv.reader(f_csv)
            sheet = workbook.add_worksheet(sheet_name)
            rows = list(reader)
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    if r == 0 and c == 0:
                        sheet.write_url(0, 0, 'internal:Index!A' + str(row_index + 1), header_link_format, value)
                        try:
                            sheet.write_comment(0, 0, 'Bookmark to Index')
                        except Exception:
                            pass
                    elif r == 0:
                        sheet.write(r, c, value, header_format)
                    else:
                        sheet.write(r, c, value)
            _autosize_worksheet_columns(sheet, rows)
        row_index += 1

    try:
        workbook.close()
    except Exception as err:
        try:
            F_DBG.write('\nRLS Debug workbook close failed for %s: %s\n' % (output_file, str(err)))
        except Exception:
            pass
        raise

    return output_file


def _cleanup_rls_csv_artifacts(csv_paths):
    for csv_path in csv_paths:
        try:
            if os.path.exists(csv_path):
                os.remove(csv_path)
        except Exception as err:
            try:
                F_DBG.write('\nCould not remove RLS CSV %s: %s\n' % (csv_path, str(err)))
            except Exception:
                pass


def _consolidate_rls_csv_to_xlsx(WindowsHost, tid_label='', cleanup_csvs=True):
    try:
        from xlsxwriter.workbook import Workbook
    except Exception as err:
        F_DBG.write('\nRLS XLSX generation unavailable: %s\n' % str(err))
        return ''

    workbook_label = _normalize_rls_tid_label(tid_label or WindowsHost) or _normalize_rls_tid_label(WindowsHost)
    workbook_label = re.sub(r'[\\/:*?"<>|]+', '_', workbook_label)
    output_file = os.path.join(os.getcwd(), workbook_label + '_RLS.xlsx')
    output_file = _reserve_workbook_path(output_file)
    workbook = Workbook(output_file)
    header_format = workbook.add_format({'bold': True, 'font_color': 'white'})
    header_format.set_bg_color('black')
    link_format = workbook.add_format({'font_color': 'blue', 'underline': 1})
    header_link_format = workbook.add_format({'bold': True, 'font_color': 'blue', 'underline': 1})
    header_link_format.set_bg_color('black')

    display_tid = workbook_label
    capture_stamp = strftime('%Y-%m-%d @ %H:%M:%S')

    index_sheet = workbook.add_worksheet('Index')
    index_sheet.set_column(0, 0, 24)
    index_sheet.set_column(1, 1, 72)
    index_sheet.merge_range(0, 0, 0, 1, 'Capture Time = ' + capture_stamp, header_format)
    index_sheet.merge_range(2, 0, 2, 1, 'TID IP = ' + WindowsHost, header_format)
    index_sheet.merge_range(3, 0, 3, 1, 'TID Name = ' + display_tid, header_format)

    all_csv_paths = glob.glob(WindowsHost + '_RLS_*.csv')
    priority = {
        WindowsHost + '_RLS_Issues.csv': 0,
        # Engineering-level verdicts produced by the validation step (when
        # the run was invoked with --validate). Slotted right after Issues
        # so the operator sees PASS/WARN/FAIL/INFO judgements before
        # diving into raw command output.
        WindowsHost + '_RLS_Validation.csv': 1,
        WindowsHost + '_RLS_Adjacencies.csv': 2,
        WindowsHost + '_RLS_Alarms.csv': 3,
        WindowsHost + '_RLS_Amplifiers.csv': 4,
        WindowsHost + '_RLS_CHMON.csv': 5,
        WindowsHost + '_RLS_DCN.csv': 6,
        WindowsHost + '_RLS_Logging.csv': 7,
        WindowsHost + '_RLS_PM_Audit.csv': 8,
        WindowsHost + '_RLS_OSRP_Diagnostics.csv': 9,
        WindowsHost + '_RLS_DOC.csv': 10,
        WindowsHost + '_RLS_Equipment.csv': 8,
        WindowsHost + '_RLS_ETTP.csv': 9,
        WindowsHost + '_RLS_Inventory.csv': 10,
        WindowsHost + '_RLS_Licenses.csv': 11,
        WindowsHost + '_RLS_LOC.csv': 12,
        WindowsHost + '_RLS_NMCMON.csv': 13,
        WindowsHost + '_RLS_ODUTTP.csv': 14,
        WindowsHost + '_RLS_OPTMON.csv': 15,
        WindowsHost + '_RLS_OSC.csv': 16,
        WindowsHost + '_RLS_OSPF_Nodes.csv': 17,
        WindowsHost + '_RLS_OTM4.csv': 18,
        WindowsHost + '_RLS_OTS.csv': 19,
        WindowsHost + '_RLS_OTUTTP.csv': 20,
        WindowsHost + '_RLS_PTP.csv': 22,
        WindowsHost + '_RLS_Routing_Table.csv': 24,
        WindowsHost + '_RLS_Rx_Adjacency.csv': 25,
        WindowsHost + '_RLS_Shelves.csv': 28,
        WindowsHost + '_RLS_SlotSequence.csv': 29,
        WindowsHost + '_RLS_SPLI.csv': 30,
        WindowsHost + '_RLS_Tx_Adjacency.csv': 32,
        WindowsHost + '_RLS_Software.csv': 33,
        WindowsHost + '_RLS_LLDP.csv': 35,
        # LLDP neighbor topology snapshot produced by the audit's walk
        # mode (--walk-mode). Placed at the end so the workbook reads
        # device-detail first, network-level info last.
        WindowsHost + '_RLS_Walk_Neighbors.csv': 36,
    }
    display_names = {
        WindowsHost + '_RLS_Issues.csv': 'Issues',
        WindowsHost + '_RLS_Validation.csv': 'Validation',
        WindowsHost + '_RLS_Adjacencies.csv': 'Adjacencies',
        WindowsHost + '_RLS_Alarms.csv': 'Alarms',
        WindowsHost + '_RLS_Amplifiers.csv': 'Amplifiers',
        WindowsHost + '_RLS_CHMON.csv': 'CHMON',
        WindowsHost + '_RLS_DCN.csv': 'DCN',
        WindowsHost + '_RLS_Logging.csv': 'Command Log',
        WindowsHost + '_RLS_PM_Audit.csv': 'PM_Audit',
        WindowsHost + '_RLS_OSRP_Diagnostics.csv': 'OSRP_Diagnostics',
        WindowsHost + '_RLS_DOC.csv': 'DOC',
        WindowsHost + '_RLS_Equipment.csv': 'Equipment',
        WindowsHost + '_RLS_ETTP.csv': 'ETTP',
        WindowsHost + '_RLS_Inventory.csv': 'Inventory',
        WindowsHost + '_RLS_Licenses.csv': 'Licenses',
        WindowsHost + '_RLS_LOC.csv': 'LOC',
        WindowsHost + '_RLS_NMCMON.csv': 'NMCMON',
        WindowsHost + '_RLS_ODUTTP.csv': 'ODUTTP',
        WindowsHost + '_RLS_OPTMON.csv': 'OPTMON',
        WindowsHost + '_RLS_OSC.csv': 'OSC',
        WindowsHost + '_RLS_OSPF_Nodes.csv': 'OSPF_Nodes',
        WindowsHost + '_RLS_OTM4.csv': 'OTM4',
        WindowsHost + '_RLS_OTS.csv': 'OTS',
        WindowsHost + '_RLS_OTUTTP.csv': 'OTUTTP',
        WindowsHost + '_RLS_PTP.csv': 'PTP',
        WindowsHost + '_RLS_Routing_Table.csv': 'Routing_Table',
        WindowsHost + '_RLS_Rx_Adjacency.csv': 'Rx_Adjacency',
        WindowsHost + '_RLS_Shelves.csv': 'Shelves',
        WindowsHost + '_RLS_SlotSequence.csv': 'SlotSequence',
        WindowsHost + '_RLS_SPLI.csv': 'SPLI',
        WindowsHost + '_RLS_Tx_Adjacency.csv': 'Tx_Adjacency',
        WindowsHost + '_RLS_Software.csv': 'Software',
        WindowsHost + '_RLS_LLDP.csv': 'LLDP',
        WindowsHost + '_RLS_Walk_Neighbors.csv': 'Walk_Neighbors',
    }
    index_descriptions = {
        'Issues': 'Photonic Issues',
        'Validation': 'Engineering validation verdicts (PASS / WARN / FAIL / INFO)',
        'Adjacencies': 'Adjacency and discovered neighbor summary',
        'Alarms': 'Active and disabled alarm conditions',
        'Amplifiers': 'Amplifier and line-card power summary',
        'CHMON': 'Channel monitoring summary',
        'DCN': 'Management connectivity and discovered neighbors',
        'Command Log': 'Log collection and command context summary',
        'PM_Audit': 'Performance monitoring, baseline, and TCA visibility',
        'OSRP_Diagnostics': 'OSRP SNC and SNCG diagnostic visibility',
        'DOC': 'Shelf and software document summary',
        'Equipment': 'Equipment and equipment mode summary',
        'ETTP': 'Ethernet trail termination point view',
        'Inventory': 'Inventory, fan, and power module summary',
        'Licenses': 'License-related signals and software state',
        'LOC': 'Optical line characteristics and reference power',
        'NMCMON': 'Optical channel performance monitoring',
        'ODUTTP': 'ODU trail termination point summary',
        'OPTMON': 'Optical monitor summary',
        'OSC': 'Optical Service Channel neighbor summary',
        'OSPF_Nodes': 'Visible neighboring nodes',
        'OTM4': 'OTM4 transport card summary',
        'OTS': 'Optical transport section summary',
        'OTUTTP': 'OTN trail termination summary',
        'PTP': 'PTP and timing summary',
        'Routing_Table': 'Discovered routing and adjacency paths',
        'Rx_Adjacency': 'Receive adjacencies',
        'Shelves': 'Shelf identity, release, and alignment summary',
        'SlotSequence': 'Slot and hardware ordering',
        'SPLI': 'SPLI-like provisioning summary',
        'Tx_Adjacency': 'Transmit adjacencies',
        'Software': 'Software versions and upgrade state',
        'LLDP': 'LLDP neighbors and management addresses',
        'Walk_Neighbors': 'Network-walk neighbor topology (interface / system / mgmt-address / port)',
    }
    csv_paths = sorted(all_csv_paths, key=lambda p: (priority.get(p, 100), os.path.basename(p).lower()))
    keep_raw_tabs = set()

    used_sheet_names = {'Index'}
    row_index = 5
    for csv_path in csv_paths:
        if not os.path.exists(csv_path):
            continue
        base_name = os.path.splitext(os.path.basename(csv_path))[0]
        prefix = WindowsHost + '_RLS_'
        if base_name.startswith(prefix):
            base_name = base_name[len(prefix):]
        if csv_path in display_names:
            sheet_name = display_names[csv_path]
        elif base_name in keep_raw_tabs:
            sheet_name = 'raw_' + re.sub(r'[^A-Za-z0-9_]+', '_', base_name).strip('_')
        else:
            continue
        sheet_name = sheet_name[:31]
        candidate = sheet_name
        suffix = 1
        while candidate in used_sheet_names:
            tag = '_' + str(suffix)
            candidate = sheet_name[:31 - len(tag)] + tag
            suffix += 1
        sheet_name = candidate
        used_sheet_names.add(sheet_name)

        index_sheet.write_url(row_index, 0, 'internal:' + sheet_name + '!A1', link_format, sheet_name)
        index_sheet.write(row_index, 1, index_descriptions.get(sheet_name, 'RLS derived data'))
        with open(csv_path, 'r', newline='', errors='ignore') as f_csv:
            reader = csv.reader(f_csv)
            sheet = workbook.add_worksheet(sheet_name)
            rows = list(reader)
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    if r == 0 and c == 0:
                        sheet.write_url(0, 0, 'internal:Index!A' + str(row_index + 1), header_link_format, value)
                        try:
                            sheet.write_comment(0, 0, 'Bookmark to Index')
                        except Exception:
                            pass
                    elif r == 0:
                        sheet.write(r, c, value, header_format)
                    else:
                        sheet.write(r, c, value)
            _autosize_worksheet_columns(sheet, rows)
        row_index += 1

    try:
        workbook.close()
    except Exception as err:
        try:
            F_DBG.write('\nRLS workbook close failed for %s: %s\n' % (output_file, str(err)))
        except Exception:
            pass
        raise

    if cleanup_csvs:
        _cleanup_rls_csv_artifacts(csv_paths)
    return output_file


def PARSE_COLLECTED_DATA_RLS(WindowsHost):
    ErrorMessage = ''
    summary_path = _rls_artifact_path(WindowsHost + '_RLS_Smoke_Summary')
    summary_text = _read_rls_artifact(WindowsHost + '_RLS_Smoke_Summary')
    if not summary_text:
        print(summary_path + ' not found')
        F_DBG.write('\n\n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% %s not found %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% \n' % summary_path)
        return 'RLS smoke summary not found'

    variant_note = WindowsHost + '_RLS_Detected.csv'
    report_path = WindowsHost + '_RLS_Report.csv'

    active_version = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_active_version'), 'active-version')
    running_version = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_running_version'), 'running-version')
    committed_version = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_committed_version'), 'committed-version')
    upgrade_state = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_upgrade_state'), 'upgrade-operational-state')
    upgrade_target = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_upgrade_target'), 'upgrade-to-version')
    ztp_state = _extract_rls_field(_read_rls_artifact(WindowsHost + '_RLS_ztp_admin_state'), 'admin-state')

    operation_text = _read_rls_artifact(WindowsHost + '_RLS_operation_info')
    operation_in_progress = _extract_rls_field(operation_text, 'operation')
    operation_result = _extract_rls_field(operation_text, 'result')
    op_start = _extract_rls_field(operation_text, 'start-timestamp')
    op_end = _extract_rls_field(operation_text, 'end-timestamp')

    alarm_text = _read_rls_artifact(WindowsHost + '_RLS_alarm_counts')
    alarm_history_text = _read_rls_artifact(WindowsHost + '_RLS_alarm_history')
    critical = _safe_int(_extract_rls_field(alarm_text, 'critical'))
    major = _safe_int(_extract_rls_field(alarm_text, 'major'))
    minor = _safe_int(_extract_rls_field(alarm_text, 'minor'))
    warning = _safe_int(_extract_rls_field(alarm_text, 'warning'))
    alarm_highlights = _extract_rls_alarm_highlights(alarm_history_text)

    shelf_text = _read_rls_artifact(WindowsHost + '_RLS_shelf')
    shelf_product = _extract_rls_field(shelf_text, 'product')
    shelf_type = _extract_rls_field(shelf_text, 'shelf-type')
    serial_number = _extract_rls_field(shelf_text, 'serial-number')
    hardware_release = _extract_rls_field(shelf_text, 'hardware-release')
    current_power = _extract_rls_field(shelf_text, 'value')

    system_text = _read_rls_artifact(WindowsHost + '_RLS_system')
    system_admin = _extract_rls_field(system_text, 'admin-state')
    debug_logging = _extract_rls_field(system_text, 'debug-logging')
    publish_notifications = _extract_rls_field(system_text, 'publish-notifications')
    latest_bin_match = re.search(r'latest-bin-number:\s*\n\s*bin-number\s*:\s*(\d+)', system_text, re.IGNORECASE)
    latest_bin = latest_bin_match.group(1) if latest_bin_match else ''
    cpu_idle_match = re.search(r'idle:\s*\n\s*current\s*:\s*([0-9.]+)', system_text, re.IGNORECASE)
    cpu_idle = cpu_idle_match.group(1) if cpu_idle_match else ''
    mem_used_match = re.search(r'percent-of-used-mem:\s*\n\s*current\s*:\s*([0-9.]+)', system_text, re.IGNORECASE)
    mem_used = mem_used_match.group(1) if mem_used_match else ''

    logs_remote_config_text = _read_rls_artifact(WindowsHost + '_RLS_logs_remote_config')
    logs_retrieve_status_text = _read_rls_artifact(WindowsHost + '_RLS_logs_retrieve_status')
    command_log_text = _read_rls_artifact(WindowsHost + '_RLS_command_log')
    syslog_history_text = _read_rls_artifact(WindowsHost + '_RLS_syslog_history')
    pm_current_text = _read_rls_artifact(WindowsHost + '_RLS_pm_current')
    pm_history_text = _read_rls_artifact(WindowsHost + '_RLS_pm_history')
    pm_tca_text = _read_rls_artifact(WindowsHost + '_RLS_pm_tca')
    osrp_snc_diag_text = _read_rls_artifact(WindowsHost + '_RLS_osrp_snc_diagnostics')
    osrp_sncg_diag_text = _read_rls_artifact(WindowsHost + '_RLS_osrp_sncg_diagnostics')

    syslog_server_hosts = []
    for line in logs_remote_config_text.splitlines():
        if re.match(r'(?i)^\s*host\s*:', line):
            host_value = line.split(':', 1)[1].strip()
            if host_value and host_value not in ('""', "''"):
                syslog_server_hosts.append(host_value)
    provisioned_syslog_servers = len(syslog_server_hosts)
    log_collection_status = _extract_rls_field(logs_retrieve_status_text, 'status') or ('visible' if logs_retrieve_status_text.strip() else 'unknown')
    command_log_visible = 'YES' if _rls_meaningful_lines(command_log_text, 'show command-log') else 'NO'
    pm_current_available = 'YES' if _rls_meaningful_lines(pm_current_text, 'show pm current') else 'NO'
    pm_history_available = 'YES' if _rls_meaningful_lines(pm_history_text, 'show pm historical') else 'NO'
    pm_baseline_state = 'VISIBLE' if 'BASELINE' in pm_current_text.upper() else 'NOT-VISIBLE'
    pm_tca_state = 'VISIBLE' if 'TCA' in pm_tca_text.upper() or 'THRESHOLD' in pm_tca_text.upper() else 'NOT-VISIBLE'
    osrp_snc_status = 'VISIBLE' if _rls_meaningful_lines(osrp_snc_diag_text, 'action osrp ALL object snc ALL show-snc-diagnostics') else 'NOT-VISIBLE'
    osrp_sncg_status = 'VISIBLE' if _rls_meaningful_lines(osrp_sncg_diag_text, 'action osrp ALL object snc-group ALL show-snc-group-diagnostics') else 'NOT-VISIBLE'
    auth_event_count = sum(syslog_history_text.upper().count(evt) for evt in ('LOGINACCEPTED', 'LOGINDENIED', 'LOGOUT'))
    alarm_event_count = sum(syslog_history_text.upper().count(evt) for evt in ('ALARMCREATED', 'ALARMDELETED', 'ALARMMODIFIED'))
    login_user = globals().get('USER', '') or 'unknown'
    session_commands = _extract_rls_summary_commands(WindowsHost + '_RLS_Smoke_Summary')
    device_command_entries = _extract_rls_device_command_log_entries(command_log_text, target_user=login_user)
    session_command_count = len(session_commands)
    device_command_count = len(device_command_entries)
    cpu_health = 'PASS' if _safe_float(cpu_idle, -1.0) >= 80.0 else ('WARN' if 0.0 <= _safe_float(cpu_idle, -1.0) < 40.0 else 'INFO')
    mem_health = 'PASS' if 0.0 <= _safe_float(mem_used, -1.0) <= 70.0 else ('WARN' if _safe_float(mem_used, -1.0) > 85.0 else 'INFO')

    all_slots_text = _read_rls_artifact(WindowsHost + '_RLS_all_slots')
    slot_details = _extract_rls_slot_details(all_slots_text)
    lldp_text = _read_rls_artifact(WindowsHost + '_RLS_lldp')
    lldp_interfaces, lldp_neighbor_map = _extract_rls_lldp_interfaces(lldp_text)
    neighbor_ifaces = [iface for iface in lldp_interfaces if lldp_neighbor_map.get(iface, False)]
    neighbor_details = _extract_rls_neighbor_details(WindowsHost, neighbor_ifaces)
    osc_power_map = {}
    osc_power_details = []
    for iface in [item for item in lldp_interfaces if 'osc' in (item or '').lower()]:
        tx_power, rx_power, rx_cord_loss = _extract_rls_osc_power_metrics(pm_current_text, pm_history_text, iface)
        osc_power_map[iface] = (tx_power, rx_power, rx_cord_loss)
        parts = []
        if tx_power:
            parts.append('Tx=' + tx_power)
        if rx_power:
            parts.append('Rx=' + rx_power)
        if rx_cord_loss:
            parts.append('Loss=' + rx_cord_loss)
        if parts:
            osc_power_details.append(iface + ' [' + ', '.join(parts) + ']')

    total_ok, total_warn = _read_rls_summary_counts(WindowsHost + '_RLS_Smoke_Summary')
    expected_tid_clean = _normalize_rls_tid_label(EXPECTED_TID)
    display_tid = expected_tid_clean or ''
    detected_tid = _normalize_rls_tid_label(_extract_tid_from_prompt(RLS_SHELL_PROMPT) or _extract_rls_field(system_text, 'system-name') or _extract_rls_field(system_text, 'host-name') or _extract_rls_field(shelf_text, 'system-name') or _extract_rls_field(shelf_text, 'host-name') or _extract_rls_field(system_text, 'name') or _extract_rls_field(shelf_text, 'name'))
    tid_match = 'YES' if not expected_tid_clean or expected_tid_clean == detected_tid else 'NO'
    primary_shelf = 'SHELF-1'

    report_lines = []
    report_lines.append('6500 RLS Diagnostic Report')
    report_lines.append('Host = ' + HOST)
    report_lines.append('Expected TID = ' + EXPECTED_TID)
    report_lines.append('Detected TID = ' + (detected_tid or 'unknown'))
    if RLS_SHELL_PROMPT:
        report_lines.append('CLI Prompt = ' + RLS_SHELL_PROMPT.strip())
    report_lines.append('')

    report_lines.append('[COLLECTION_STATUS]')
    if tid_match == 'YES':
        report_lines.append('\tPASS:\tExpected TID matches detected shelf name')
    else:
        report_lines.append('\tWARN:\tExpected TID does not match detected shelf name (' + EXPECTED_TID + ' vs ' + (detected_tid or 'unknown') + ')')
    if total_warn == 0:
        report_lines.append('\tPASS:\tSmoke collection completed cleanly (OK = %d, WARN = %d)' % (total_ok, total_warn))
    else:
        report_lines.append('\tWARN:\tSmoke collection completed with warnings (OK = %d, WARN = %d)' % (total_ok, total_warn))
    report_lines.append('')

    report_lines.append('[SOFTWARE]')
    versions = [v for v in (active_version, running_version, committed_version) if v]
    if versions and len(set(versions)) == 1:
        report_lines.append('\tPASS:\tSoftware versions aligned at ' + versions[0])
    else:
        report_lines.append('\tWARN:\tSoftware versions not fully aligned')
    report_lines.append('\tINFO:\tactive-version = ' + (active_version or 'unknown'))
    report_lines.append('\tINFO:\trunning-version = ' + (running_version or 'unknown'))
    report_lines.append('\tINFO:\tcommitted-version = ' + (committed_version or 'unknown'))
    report_lines.append('\tINFO:\tupgrade-operational-state = ' + (upgrade_state or 'unknown'))
    report_lines.append('\tINFO:\tupgrade-to-version = ' + (upgrade_target or 'unknown'))
    report_lines.append('\tINFO:\toperation-in-progress = ' + (operation_in_progress or 'unknown'))
    if operation_result:
        report_lines.append('\tINFO:\tlast-operation result = ' + operation_result)
    if op_start or op_end:
        report_lines.append('\tINFO:\tlast-operation window = ' + (op_start or 'unknown') + ' -> ' + (op_end or 'unknown'))
    if ztp_state:
        if ztp_state.upper() == 'DISABLED':
            report_lines.append('\tPASS:\tZTP admin-state = ' + ztp_state)
        else:
            report_lines.append('\tWARN:\tZTP admin-state = ' + ztp_state)
    report_lines.append('')

    report_lines.append('[PLATFORM]')
    report_lines.append('\tINFO:\tproduct = ' + (shelf_product or 'unknown'))
    report_lines.append('\tINFO:\tshelf-type = ' + (shelf_type or 'unknown'))
    report_lines.append('\tINFO:\tserial-number = ' + (serial_number or 'unknown'))
    report_lines.append('\tINFO:\thardware-release = ' + (hardware_release or 'unknown'))
    if current_power:
        report_lines.append('\tINFO:\tcurrent-power = ' + current_power + ' W')
    report_lines.append('')

    report_lines.append('[SYSTEM_HEALTH]')
    report_lines.append('\tINFO:\tadmin-state = ' + (system_admin or 'unknown'))
    report_lines.append('\tINFO:\tdebug-logging = ' + (debug_logging or 'unknown'))
    report_lines.append('\tINFO:\tpublish-notifications = ' + (publish_notifications or 'unknown'))
    if latest_bin:
        report_lines.append('\tINFO:\tlatest-bin-number = ' + latest_bin)
    if cpu_idle:
        report_lines.append('\tINFO:\tcurrent sampled CPU idle = ' + cpu_idle + '% (' + cpu_health + ')')
    if mem_used:
        report_lines.append('\tINFO:\tcurrent sampled used memory = ' + mem_used + '% (' + mem_health + ')')
    report_lines.append('')

    report_lines.append('[LOGGING_AUDIT]')
    report_lines.append('\tINFO:\tprovisioned remote syslog servers = ' + str(provisioned_syslog_servers))
    if syslog_server_hosts:
        report_lines.append('\tINFO:\tsyslog hosts = ' + ', '.join(syslog_server_hosts))
    report_lines.append('\tINFO:\tretrieve-log-status = ' + (log_collection_status or 'unknown'))
    report_lines.append('\tINFO:\tcommand-log visible = ' + command_log_visible)
    report_lines.append('\tINFO:\tTDS session command count = ' + str(session_command_count))
    report_lines.append('\tINFO:\tdevice command-log entry count = ' + str(device_command_count))
    if auth_event_count or alarm_event_count:
        report_lines.append('\tINFO:\tsyslog event markers auth=' + str(auth_event_count) + ' alarm=' + str(alarm_event_count))
    else:
        report_lines.append('\tINFO:\tsyslog event markers not visible in current capture')
    report_lines.append('')

    report_lines.append('[PERFORMANCE_MONITORING]')
    report_lines.append('\tINFO:\tPM current output visible = ' + pm_current_available)
    report_lines.append('\tINFO:\tPM historical output visible = ' + pm_history_available)
    report_lines.append('\tINFO:\tPM baseline visibility = ' + pm_baseline_state)
    report_lines.append('\tINFO:\tPM TCA visibility = ' + pm_tca_state)
    report_lines.append('')

    report_lines.append('[OSRP_DIAGNOSTICS]')
    report_lines.append('\tINFO:\tSNC diagnostics = ' + osrp_snc_status)
    report_lines.append('\tINFO:\tSNCG diagnostics = ' + osrp_sncg_status)
    report_lines.append('')

    report_lines.append('[ALARMS]')
    alarm_summary = 'critical=%d, major=%d, minor=%d, warning=%d' % (critical, major, minor, warning)
    if critical > 0:
        report_lines.append('\tWARN:\tCritical alarms present: ' + alarm_summary)
    elif major == 0 and minor == 0 and warning == 0:
        report_lines.append('\tPASS:\tNo active alarms: ' + alarm_summary)
    else:
        report_lines.append('\tWARN:\tActive alarms present: ' + alarm_summary)
    for item in alarm_highlights:
        report_lines.append('\tINFO:\t' + item)
    report_lines.append('')

    report_lines.append('[HARDWARE]')
    if slot_details:
        report_lines.append('\tINFO:\tDiscovered slots = ' + ', '.join([slot + ' (' + form + ')' for slot, form in slot_details]))
        for slot, form_factor in slot_details:
            inv_text = _read_rls_artifact(WindowsHost + '_RLS_slot_' + slot + '_inventory')
            sw_text = _read_rls_artifact(WindowsHost + '_RLS_slot_' + slot + '_software_component')
            c_type = _extract_rls_field(inv_text, 'c-type') or form_factor
            oper_state = _extract_rls_field(inv_text, 'operational-state') or _extract_rls_field(inv_text, 'state')
            slot_serial = _extract_rls_field(inv_text, 'serial-number')
            slot_hw = _extract_rls_field(inv_text, 'hardware-release')
            slot_power = _extract_rls_field(inv_text, 'value')
            slot_sw = _extract_rls_field(sw_text, 'active-version')
            line = '\tINFO:\tSlot %s %s: %s' % (slot, form_factor, c_type)
            if oper_state:
                line += ', state=' + oper_state
            if slot_serial:
                line += ', serial=' + slot_serial
            if slot_hw:
                line += ', hw=' + slot_hw
            if slot_power:
                line += ', power=' + slot_power + ' W'
            if slot_sw:
                line += ', sw=' + slot_sw
            report_lines.append(line)
    else:
        report_lines.append('\tWARN:\tNo slot details were discovered from show slots')
    report_lines.append('')

    report_lines.append('[LLDP]')
    if neighbor_ifaces:
        report_lines.append('\tINFO:\tNeighbors detected on interfaces: ' + ', '.join(neighbor_ifaces))
        for iface, system_name, mgmt_addr, port_id in neighbor_details:
            report_lines.append('\tINFO:\t' + iface + ' -> ' + system_name + ' (' + mgmt_addr + ', ' + port_id + ')')
    elif lldp_interfaces:
        report_lines.append('\tINFO:\tInterfaces checked with no live neighbors detected')
    else:
        report_lines.append('\tWARN:\tNo LLDP interfaces were discovered')
    report_lines.append('')

    osc_neighbor_ifaces = [iface for iface in neighbor_ifaces if 'osc' in (iface or '').lower()]

    report_lines.append('[OPTICAL_CONTROL]')
    if osc_neighbor_ifaces:
        report_lines.append('\tPASS:\tOSC neighbor verified on interfaces: ' + ', '.join(osc_neighbor_ifaces))
    else:
        report_lines.append('\tWARN:\tNo live OSC neighbor was verified from current LLDP evidence')
    if osc_power_details:
        for item in osc_power_details:
            report_lines.append('\tINFO:\tMeasured optical levels ' + item)
    else:
        report_lines.append('\tINFO:\tNo OSC Tx/Rx power values were visible in the current PM capture')
    report_lines.append('')

    report_lines.append('[SPLI]')
    if slot_details and osc_neighbor_ifaces:
        report_lines.append('\tPASS:\tSPLI-style shelf sequencing has OSC control visibility')
    elif slot_details:
        report_lines.append('\tWARN:\tShelf sequencing was derived, but OSC control visibility was not verified')
    else:
        report_lines.append('\tWARN:\tNo slot details were available for SPLI-style validation')
    report_lines.append('')

    report_lines.append('[RECOMMENDED_FOLLOW_UP]')
    if 'LICENSE-VIOLATION' in alarm_history_text.upper() or 'LICENSES ARE IN ARREARS' in alarm_history_text.upper():
        report_lines.append('\tWARN:\tReview shelf licensing status')
    if 'CERTIFICATE EXPIRED' in alarm_history_text.upper():
        report_lines.append('\tWARN:\tReview shelf certificate state')
    if 'POWER FAILURE FAULT' in alarm_history_text.upper():
        report_lines.append('\tWARN:\tReview power module alarm history for slot 62')
    if total_warn == 0 and critical == 0:
        report_lines.append('\tPASS:\tCollection path is stable for this RLS shelf')
    report_lines.append('')

    software_csv = WindowsHost + '_RLS_Software.csv'
    system_health_csv = WindowsHost + '_RLS_System_Health.csv'
    alarms_csv = WindowsHost + '_RLS_Alarms.csv'
    shelves_csv = WindowsHost + '_RLS_Shelves.csv'
    doc_csv = WindowsHost + '_RLS_DOC.csv'
    equipment_csv = WindowsHost + '_RLS_Equipment.csv'
    inventory_csv = WindowsHost + '_RLS_Inventory.csv'
    amplifiers_csv = WindowsHost + '_RLS_Amplifiers.csv'
    chmon_csv = WindowsHost + '_RLS_CHMON.csv'
    licenses_csv = WindowsHost + '_RLS_Licenses.csv'
    osc_csv = WindowsHost + '_RLS_OSC.csv'
    ots_csv = WindowsHost + '_RLS_OTS.csv'
    otuttp_csv = WindowsHost + '_RLS_OTUTTP.csv'
    optmon_csv = WindowsHost + '_RLS_OPTMON.csv'
    ptp_csv = WindowsHost + '_RLS_PTP.csv'
    logging_csv = WindowsHost + '_RLS_Logging.csv'
    pm_audit_csv = WindowsHost + '_RLS_PM_Audit.csv'
    osrp_diag_csv = WindowsHost + '_RLS_OSRP_Diagnostics.csv'
    ettp_csv = WindowsHost + '_RLS_ETTP.csv'
    loc_csv = WindowsHost + '_RLS_LOC.csv'
    nmcmon_csv = WindowsHost + '_RLS_NMCMON.csv'
    oduttp_csv = WindowsHost + '_RLS_ODUTTP.csv'
    otm4_csv = WindowsHost + '_RLS_OTM4.csv'
    slot_sequence_csv = WindowsHost + '_RLS_SlotSequence.csv'
    spli_csv = WindowsHost + '_RLS_SPLI.csv'
    adjacencies_csv = WindowsHost + '_RLS_Adjacencies.csv'
    rx_adjacency_csv = WindowsHost + '_RLS_Rx_Adjacency.csv'
    tx_adjacency_csv = WindowsHost + '_RLS_Tx_Adjacency.csv'
    lldp_csv = WindowsHost + '_RLS_LLDP.csv'
    dcn_csv = WindowsHost + '_RLS_DCN.csv'
    ospf_nodes_csv = WindowsHost + '_RLS_OSPF_Nodes.csv'
    routing_csv = WindowsHost + '_RLS_Routing_Table.csv'
    issues_csv = WindowsHost + '_RLS_Issues.csv'

    inventory_rows = []
    for slot, form_factor in slot_details:
        inv_text = _read_rls_artifact(WindowsHost + '_RLS_slot_' + slot + '_inventory')
        sw_text = _read_rls_artifact(WindowsHost + '_RLS_slot_' + slot + '_software_component')
        inventory_rows.append([
            slot,
            form_factor,
            _extract_rls_field(inv_text, 'c-type') or form_factor,
            _extract_rls_field(inv_text, 'operational-state') or _extract_rls_field(inv_text, 'state'),
            _extract_rls_field(inv_text, 'serial-number'),
            _extract_rls_field(inv_text, 'hardware-release'),
            _extract_rls_field(inv_text, 'value'),
            _extract_rls_field(sw_text, 'active-version'),
        ])

    neighbor_detail_map = {iface: (system_name, mgmt_addr, port_id) for iface, system_name, mgmt_addr, port_id in neighbor_details}
    lldp_rows = []
    adjacency_rows = []
    ospf_rows = []
    routing_rows = []
    for iface in lldp_interfaces:
        system_name, mgmt_addr, port_id = neighbor_detail_map.get(iface, ('', '', ''))
        neighbor_present = 'YES' if iface in neighbor_ifaces else 'NO'
        lldp_rows.append([display_tid, primary_shelf, iface, neighbor_present, system_name, mgmt_addr, port_id])
        adjacency_rows.append([display_tid, primary_shelf, iface, neighbor_present, system_name or 'none', mgmt_addr or 'n/a', port_id or 'n/a'])
        if system_name or mgmt_addr:
            ospf_rows.append([display_tid, system_name or 'unknown', mgmt_addr or 'n/a', iface, port_id or 'n/a'])
            routing_rows.append([display_tid, system_name or 'unknown', mgmt_addr or 'n/a', iface, 'LLDP-discovered'])

    uptime_value = _extract_rls_field(all_slots_text, 'uptime') or ''
    time_of_day = _extract_rls_field(all_slots_text, 'time-of-day') or ''
    equipment_rows = [['SHELF-1', 'shelf', shelf_product or shelf_type or 'Shelf', system_admin or 'unknown', serial_number or '', hardware_release or '', current_power or '', active_version or '']]
    equipment_rows.extend(inventory_rows)

    dcn_rows = [
        ['Management', 'Host', HOST],
        ['Management', 'Expected TID', EXPECTED_TID],
        ['Management', 'Detected TID', detected_tid or 'unknown'],
        ['Management', 'TID Match', tid_match],
        ['Management', 'CLI Prompt', RLS_SHELL_PROMPT.strip()],
        ['Management', 'Publish Notifications', publish_notifications or 'unknown'],
        ['Management', 'Debug Logging', debug_logging or 'unknown'],
        ['Management', 'ZTP Admin State', ztp_state or 'unknown'],
        ['Neighbors', 'Neighbor Count', str(len(neighbor_ifaces))],
    ]
    for iface, system_name, mgmt_addr, port_id in neighbor_details:
        dcn_rows.append(['Neighbors', iface, (system_name or 'unknown') + ' | ' + (mgmt_addr or 'n/a') + ' | ' + (port_id or 'n/a')])

    doc_rows = [
        [display_tid, primary_shelf, shelf_product or 'unknown', shelf_type or 'unknown', serial_number or 'unknown', hardware_release or 'unknown', active_version or 'unknown', running_version or 'unknown', committed_version or 'unknown', upgrade_state or 'unknown', upgrade_target or 'unknown', ztp_state or 'unknown'],
    ]

    license_hits = [item for item in alarm_highlights if 'LICENSE' in item.upper()]
    license_rows = []
    if license_hits:
        for item in license_hits:
            parts = item.split(' | ')
            severity = parts[0] if len(parts) > 0 else 'INFO'
            cause = parts[1] if len(parts) > 1 else item
            resource = ' | '.join(parts[2:]) if len(parts) > 2 else primary_shelf
            license_rows.append([display_tid, severity, cause, resource, 'WARN'])
    else:
        license_rows.append([display_tid, 'INFO', 'No explicit license alarm found in current smoke capture', primary_shelf, 'INFO'])
    license_rows.append([display_tid, 'INFO', 'software-version', active_version or running_version or 'unknown', 'INFO'])

    logging_rows = []
    sequence_id = 1
    for timestamp, user_name, command, raw_line in device_command_entries:
        logging_rows.append([
            display_tid,
            sequence_id,
            'DEVICE_COMMAND_LOG',
            timestamp,
            user_name or 'unknown',
            command,
            'show command-log',
            'INFO',
            raw_line,
        ])
        sequence_id += 1

    for group_name, status, command, note in session_commands:
        logging_rows.append([
            display_tid,
            sequence_id,
            'TDS_SESSION',
            '',
            login_user,
            command,
            group_name,
            status,
            note,
        ])
        sequence_id += 1

    if not logging_rows:
        logging_rows.append([
            display_tid,
            1,
            'INFO',
            '',
            login_user,
            'No command entries captured',
            'show command-log',
            'WARN' if command_log_visible != 'YES' else 'INFO',
            'Run show command-log manually if the platform restricts command history visibility',
        ])

    pm_audit_rows = [
        [display_tid, 'PM Current', pm_current_available, 'show pm current', pm_baseline_state, 'INFO'],
        [display_tid, 'PM Historical', pm_history_available, 'show pm historical', '', 'INFO'],
        [display_tid, 'PM TCA', pm_tca_state, 'show-pm-tca', '', 'INFO'],
    ]

    osrp_diag_rows = [
        [display_tid, 'SNC Diagnostics', osrp_snc_status, 'action osrp ALL object snc ALL show-snc-diagnostics', 'read-only audit evidence'],
        [display_tid, 'SNCG Diagnostics', osrp_sncg_status, 'action osrp ALL object snc-group ALL show-snc-group-diagnostics', 'read-only audit evidence'],
    ]

    def _legacy_optical_tuple(identifier):
        token = re.sub(r'[^0-9A-Za-z]+', '-', str(identifier)).strip('-') or '1'
        ots_aid = 'OTS-1-' + token
        osid = 'OSID-' + token
        tx_path = 'TX-' + token
        rx_path = 'RX-' + token
        fe_aid = 'FE-' + token
        return ots_aid, osid, tx_path, rx_path, fe_aid

    slot_sequence_rows = []
    amplifier_rows = []
    chmon_rows = []
    ots_rows = []
    otuttp_detail_rows = []
    optmon_rows = []
    nmcmon_rows = []
    loc_rows = []
    spli_rows = []
    otm4_candidates = []

    for idx, row in enumerate(inventory_rows, 1):
        slot, form_factor, card_type, oper_state, slot_serial, slot_hw, slot_power, slot_sw = row
        source = 'show slots inventory'
        ots_aid, osid, tx_path, rx_path, fe_aid = _legacy_optical_tuple(slot)
        slot_label = card_type or form_factor or ('slot-' + str(slot))
        oper_state_clean = (oper_state or '').lower()
        if osc_neighbor_ifaces:
            osc_control_state = 'UP' if oper_state_clean in ('active', 'enabled', 'up') else 'STANDBY'
        else:
            osc_control_state = 'ATTENTION' if oper_state_clean in ('active', 'enabled', 'up') else 'IDLE'

        slot_sequence_rows.append([display_tid, primary_shelf, ots_aid, osid, tx_path, rx_path, fe_aid, idx, 'YES' if idx == 1 else 'NO', slot_label, idx, idx])
        spli_rows.append([display_tid, primary_shelf, idx, shelf_product or '6500 RLS', fe_aid, display_tid, primary_shelf, HOST, 'SSH+LLDP' if osc_neighbor_ifaces else 'SSH', oper_state or 'unknown', len(neighbor_ifaces), osc_control_state])

        if form_factor in ('ctm', 'access-panel') or 'otm' in (card_type or '').lower() or 'amp' in (card_type or '').lower():
            amplifier_rows.append([display_tid, primary_shelf, slot, ots_aid, osid, tx_path, rx_path, fe_aid, slot_power or '', 'automatic', card_type or form_factor or 'unknown', slot])
            chmon_rows.append([display_tid, primary_shelf, ots_aid, osid, tx_path, rx_path, fe_aid, 'CHMON-' + str(slot), slot_label, '', str(idx), oper_state or 'unknown', slot_power or '', '', ''])
            ots_rows.append([display_tid, primary_shelf, form_factor or 'RLS', card_type or 'RLS', ots_aid, osid, tx_path, rx_path, slot, display_tid, 'MONITORED', fe_aid])
            otuttp_detail_rows.append([display_tid, primary_shelf, slot, 'OTUTTP-' + str(slot), card_type or 'RLS', oper_state or 'unknown', slot_sw or 'unknown', slot_power or '', source])
            optmon_rows.append([display_tid, primary_shelf, ots_aid, osid, tx_path, rx_path, fe_aid, 'OPTMON-' + str(slot), slot_label, 'inventory-monitor', primary_shelf])
            nmcmon_rows.append([display_tid, primary_shelf, ots_aid, osid, tx_path, rx_path, fe_aid, 'NMCMON-' + str(slot), slot_label, '', '', '', slot_power or '', '', ''])
            loc_rows.append([display_tid, slot, ots_aid, osid, tx_path, rx_path, fe_aid, oper_state or 'unknown', oper_state or 'unknown', form_factor or 'RLS', '', '', slot_power or '', '', '', card_type or form_factor or 'RLS', 'OSC-VERIFIED' if osc_neighbor_ifaces else ('IDLE' if oper_state_clean in ('idle', 'down') else 'MONITORED')])
            otm4_candidates.append((slot, slot_label, oper_state or 'unknown', slot_serial or 'unknown', slot_hw or 'unknown', slot_sw or 'unknown'))

    if not amplifier_rows:
        fallback = _legacy_optical_tuple('1')
        amplifier_rows.append([display_tid, primary_shelf, '1', fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], '', 'automatic', 'unknown', '1'])
    if not chmon_rows:
        fallback = _legacy_optical_tuple('1')
        chmon_rows.append([display_tid, primary_shelf, fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], 'CHMON-1', 'unknown', '', '1', 'unknown', '', '', ''])
    if not ots_rows:
        fallback = _legacy_optical_tuple('1')
        ots_rows.append([display_tid, primary_shelf, 'RLS', 'RLS', fallback[0], fallback[1], fallback[2], fallback[3], '', display_tid, 'MONITORED', fallback[4]])
    if not otuttp_detail_rows:
        otuttp_detail_rows.append([display_tid, primary_shelf, '1', 'OTUTTP-1', 'RLS', 'IDLE', 'unknown', '', 'inventory-derived'])
    if not optmon_rows:
        fallback = _legacy_optical_tuple('1')
        optmon_rows.append([display_tid, primary_shelf, fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], 'OPTMON-1', 'unknown', 'inventory-monitor', primary_shelf])
    if not nmcmon_rows:
        fallback = _legacy_optical_tuple('1')
        nmcmon_rows.append([display_tid, primary_shelf, fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], 'NMCMON-1', 'unknown', '', '', '', '', '', ''])
    if not loc_rows:
        fallback = _legacy_optical_tuple('1')
        loc_rows.append([display_tid, '1', fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], 'IDLE', 'IDLE', 'RLS', '', '', '', '', '', 'RLS', 'MONITORED'])

    osc_rows = []
    for row in lldp_rows:
        iface = row[2]
        neighbor_present = row[3]
        system_name = row[4]
        mgmt_addr = row[5]
        port_id = row[6]
        if 'osc' in (iface or '').lower():
            ots_aid, osid, tx_path, rx_path, fe_aid = _legacy_optical_tuple(iface)
            tx_power, rx_power, rx_cord_loss = osc_power_map.get(iface, ('', '', ''))
            osc_rows.append([display_tid, primary_shelf, ots_aid, osid, tx_path, rx_path, mgmt_addr or fe_aid, len(slot_sequence_rows) or 1, iface, tx_power, rx_power, rx_cord_loss, system_name or 'none', port_id or 'n/a', mgmt_addr or 'n/a', 'VERIFIED' if neighbor_present == 'YES' else 'NO_NEIGHBOR'])
    if not osc_rows:
        fallback = _legacy_optical_tuple('OSC-1')
        osc_rows.append([display_tid, primary_shelf, fallback[0], fallback[1], fallback[2], fallback[3], fallback[4], 1, 'none-detected', '', '', '', 'none', 'n/a', 'n/a', 'NO_NEIGHBOR'])

    client_ifaces = [iface for iface in lldp_interfaces if 'osc' not in (iface or '').lower()]
    if not client_ifaces:
        client_ifaces = ['ETTP-1-1-1']

    ettp_header = ['TL1 Parameter', 'TID = ' + display_tid] + [('ETTP-1-' + re.sub(r'[^0-9A-Za-z]+', '-', iface).strip('-').upper()) for iface in client_ifaces]
    ettp_rows = [
        ['AID', 'Derived From'] + client_ifaces,
        ['State', 'LLDP/Inventory'] + [('UP' if iface in neighbor_ifaces else 'DISCOVERED') for iface in client_ifaces],
        ['Neighbor', 'LLDP'] + [neighbor_detail_map.get(iface, ('', '', ''))[0] or 'none' for iface in client_ifaces],
        ['Management Address', 'LLDP'] + [neighbor_detail_map.get(iface, ('', '', ''))[1] or 'n/a' for iface in client_ifaces],
        ['Port ID', 'LLDP'] + [neighbor_detail_map.get(iface, ('', '', ''))[2] or 'n/a' for iface in client_ifaces],
    ]

    oduttp_candidates = [row[0] for row in inventory_rows[:2]] or ['1']
    oduttp_header = ['TL1 Parameter', 'TID = ' + display_tid] + [('ODUTTP-1-' + str(slot)) for slot in oduttp_candidates]
    oduttp_rows = [
        ['Circuit Pack', 'RLS derived'] + [(next((r[2] for r in inventory_rows if r[0] == slot), 'unknown')) for slot in oduttp_candidates],
        ['Operational State', 'RLS derived'] + [(next((r[3] for r in inventory_rows if r[0] == slot), 'unknown')) for slot in oduttp_candidates],
        ['Software Version', 'RLS derived'] + [(next((r[7] for r in inventory_rows if r[0] == slot), 'unknown')) for slot in oduttp_candidates],
        ['Serial Number', 'RLS derived'] + [(next((r[4] for r in inventory_rows if r[0] == slot), 'unknown')) for slot in oduttp_candidates],
    ]

    otm4_display = otm4_candidates[:1] or [('1', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown')]
    otm4_header = ['TL1 Parameter', 'TID = ' + display_tid] + [('OTM4-1-' + str(item[0]) + '-1') for item in otm4_display]
    otm4_rows = [
        ['Card Type', 'RLS derived'] + [item[1] for item in otm4_display],
        ['Operational State', 'RLS derived'] + [item[2] for item in otm4_display],
        ['Serial Number', 'RLS derived'] + [item[3] for item in otm4_display],
        ['Hardware Release', 'RLS derived'] + [item[4] for item in otm4_display],
        ['Software Version', 'RLS derived'] + [item[5] for item in otm4_display],
    ]

    ptp_rows = [
        [display_tid, primary_shelf, time_of_day or 'unknown', uptime_value or 'unknown', latest_bin or 'unknown', operation_result or 'unknown'],
    ]

    rx_rows = []
    tx_rows = []
    for tid, shelf, iface, neighbor_present, system_name, mgmt_addr, port_id in adjacency_rows:
        ots_aid, osid, tx_path, rx_path, fe_aid = _legacy_optical_tuple(iface)
        iface_lower = str(iface or '').lower()
        if 'osc' in iface_lower:
            local_service = 'OSC-CONTROL'
            discovered_service = 'OSC-CONTROL'
            frequency_label = '198.54'
        elif 'colan' in iface_lower:
            local_service = 'CLIENT-LAN'
            discovered_service = 'CLIENT-LAN'
            frequency_label = ''
        elif 'ilan' in iface_lower:
            local_service = 'INTRA-LAN'
            discovered_service = 'INTRA-LAN'
            frequency_label = ''
        else:
            local_service = (iface or 'LINK').upper()
            discovered_service = local_service
            frequency_label = ''
        link_state = 'UP' if neighbor_present == 'YES' else 'NO-LLDP-NEIGHBOR'
        fe_address = mgmt_addr or system_name or ('LOCAL-' + (iface or 'LINK').upper())
        circuit_id = port_id or system_name or ('LOCAL-' + (iface or 'LINK').upper())
        rx_rows.append([tid, shelf, ots_aid, osid, tx_path, rx_path, fe_aid, iface, local_service, discovered_service, link_state, fe_address])
        tx_rows.append([tid, shelf, ots_aid, osid, tx_path, rx_path, fe_aid, iface, circuit_id, local_service, discovered_service, frequency_label])

    otuttp_aids = [row[2] for row in otuttp_detail_rows]
    otuttp_header = ['TL1 Parameter', 'TID = ' + display_tid] + [('OTUTTP-1-' + str(slot)) for slot in otuttp_aids]
    otuttp_rows = [
        ['Circuit Pack', 'RLS derived'] + [row[4] for row in otuttp_detail_rows],
        ['Operational State', 'RLS derived'] + [row[5] for row in otuttp_detail_rows],
        ['Software Version', 'RLS derived'] + [row[6] for row in otuttp_detail_rows],
        ['Power W', 'RLS derived'] + [row[7] for row in otuttp_detail_rows],
    ]

    issue_rows = []
    if total_warn == 0:
        issue_rows.append(['INFO', 'Collection', 'Smoke collection completed with WARN = 0'])
    else:
        issue_rows.append(['WARN', 'Collection', 'Smoke collection reported WARN = %d' % total_warn])
    if not versions or len(set(versions)) != 1:
        issue_rows.append(['WARN', 'Software', 'Software versions are not fully aligned'])
    if ztp_state and ztp_state.upper() != 'DISABLED':
        issue_rows.append(['WARN', 'Provisioning', 'ZTP admin-state is ' + ztp_state])
    if tid_match != 'YES':
        issue_rows.append(['WARN', 'TID', 'Expected TID does not match detected shelf name: ' + EXPECTED_TID + ' vs ' + (detected_tid or 'unknown')])
    if critical or major or minor or warning:
        issue_rows.append(['WARN', 'Alarms', 'Active alarms present: ' + alarm_summary])
    if not osc_neighbor_ifaces:
        issue_rows.append(['WARN', 'Optical Control', 'No live OSC neighbor was verified; check optical control continuity'])
    if slot_details and not osc_neighbor_ifaces:
        issue_rows.append(['WARN', 'SPLI', 'SPLI-style sequencing is present but OSC control evidence was not verified'])
    for item in alarm_highlights:
        issue_rows.append(['INFO', 'Alarm Highlight', item])

    _write_rls_csv(software_csv,
                   ['TID', 'Property', 'Value'],
                   [
                       [display_tid, 'active-version', active_version],
                       [display_tid, 'running-version', running_version],
                       [display_tid, 'committed-version', committed_version],
                       [display_tid, 'upgrade-operational-state', upgrade_state],
                       [display_tid, 'upgrade-to-version', upgrade_target],
                       [display_tid, 'operation-in-progress', operation_in_progress],
                       [display_tid, 'last-operation-result', operation_result],
                       [display_tid, 'last-operation-start', op_start],
                       [display_tid, 'last-operation-end', op_end],
                       [display_tid, 'ztp-admin-state', ztp_state],
                   ])
    _write_rls_csv(doc_csv,
                   ['TID', 'Shelf', 'Product', 'Shelf Type', 'Serial Number', 'Hardware Release', 'Active Version', 'Running Version', 'Committed Version', 'Upgrade State', 'Upgrade Target', 'ZTP State'],
                   doc_rows)
    _write_rls_csv(system_health_csv,
                   ['TID', 'Metric', 'Value'],
                   [
                       [display_tid, 'admin-state', system_admin],
                       [display_tid, 'debug-logging', debug_logging],
                       [display_tid, 'publish-notifications', publish_notifications],
                       [display_tid, 'latest-bin-number', latest_bin],
                       [display_tid, 'current-cpu-idle-percent', cpu_idle],
                       [display_tid, 'current-used-memory-percent', mem_used],
                       [display_tid, 'cpu-idle-assessment', cpu_health],
                       [display_tid, 'memory-usage-assessment', mem_health],
                       [display_tid, 'remote-syslog-servers', str(provisioned_syslog_servers)],
                       [display_tid, 'retrieve-log-status', log_collection_status],
                       [display_tid, 'pm-current-visible', pm_current_available],
                       [display_tid, 'pm-tca-visible', pm_tca_state],
                   ])
    _write_rls_csv(alarms_csv,
                   ['TID', 'Severity', 'Cause or Metric', 'Resource / Value'],
                   [
                       [display_tid, 'COUNT', 'critical', critical],
                       [display_tid, 'COUNT', 'major', major],
                       [display_tid, 'COUNT', 'minor', minor],
                       [display_tid, 'COUNT', 'warning', warning],
                   ] + [[display_tid, item.split(' | ')[0], item.split(' | ')[1] if ' | ' in item else item, ' | '.join(item.split(' | ')[2:]) if item.count(' | ') >= 2 else ''] for item in alarm_highlights])
    _write_rls_csv(shelves_csv,
                   ['Host', 'Expected TID', 'Detected TID', 'TID Match', 'Product', 'Shelf Type', 'Serial Number', 'Hardware Release', 'Current Power W', 'Active Version', 'Running Version', 'Committed Version', 'Upgrade State', 'Upgrade Target', 'ZTP State', 'Critical', 'Major', 'Minor', 'Warning'],
                   [[HOST, EXPECTED_TID, detected_tid or 'unknown', tid_match, shelf_product, shelf_type, serial_number, hardware_release, current_power, active_version, running_version, committed_version, upgrade_state, upgrade_target, ztp_state, critical, major, minor, warning]])
    _write_rls_csv(equipment_csv,
                   ['Component', 'Form Factor', 'Card Type', 'Operational State', 'Serial Number', 'Hardware Release', 'Power W', 'Software Version'],
                   equipment_rows)
    _write_rls_csv(inventory_csv,
                   ['Slot', 'Form Factor', 'Card Type', 'Operational State', 'Serial Number', 'Hardware Release', 'Power W', 'Software Version'],
                   inventory_rows)
    _write_rls_csv(amplifiers_csv,
                   ['TID', 'SHELF', 'SLOT', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'Amplifier Gain Range', 'Amplifier Gain Regime', 'Amplifier Type', 'AID'],
                   amplifier_rows)
    _write_rls_csv(chmon_csv,
                   ['TID', 'SHELF ID', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'AID', 'Circuit Pack', 'Wavelength', 'Channel ID', 'OCH Status', 'Untimed OPT-OCH (dBm)', 'Baseline OPT-OCH (dBm)', 'Beaseline Reset (M-D:H-M)'],
                   chmon_rows)
    _write_rls_csv(licenses_csv,
                   ['TID', 'Severity', 'Cause', 'Resource', 'Assessment'],
                   license_rows)
    _write_rls_csv(loc_csv,
                   ['TID', 'Circuit Pack', 'OTS AID', 'OSID', 'Tx Path ID', 'Rx path ID', 'FEAID', 'PState', 'SState', 'Reference Tx/Rx Type', 'Reference Signal Bandwidth 3dB (GHz)', 'Reference Signal Bandwidth 10dB (GHz)', 'Reference Signal Power (dBm)', 'Auto Maximum Control Power Output (dBm)', 'Reference Bandwidth', 'Type', 'Tx Power Reduction Control'],
                   loc_rows)
    _write_rls_csv(nmcmon_csv,
                   ['TID', 'SHELF ID', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'AID', 'Circuit Pack', 'Frequency (THz)', 'Channel Width (GHz)', 'Wavelength (nm)', 'Untimed OPT-OCH (dBm)', 'Baseline OPT-OCH (dBm)', 'Beaseline Reset (M-D:H-M)'],
                   nmcmon_rows)
    _write_rls_csv(osc_csv,
                   ['TID', 'SHELF', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'Slot Sequencing', 'AID', 'Tx Power', 'Rx Power', 'Rx Cord Loss', 'Neighbor System', 'Neighbor Port', 'Neighbor Mgmt', 'OSC Link State'],
                   osc_rows)
    _write_rls_csv(ots_csv,
                   ['TID', 'Shelf', 'Configuration', 'Subtype', 'AID', 'OSID', 'TX Path ID', 'RX Path ID', 'OTS Members', 'DOC Site', 'Slot Sequence Mode', 'AMP Mate OTS'],
                   ots_rows)
    _write_rls_csv(otuttp_csv, otuttp_header, otuttp_rows)
    _write_rls_csv(oduttp_csv, oduttp_header, oduttp_rows)
    _write_rls_csv(otm4_csv, otm4_header, otm4_rows)
    _write_rls_csv(ettp_csv, ettp_header, ettp_rows)
    _write_rls_csv(optmon_csv,
                   ['TID', 'SHELF', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'AID', 'Circuit Pack', 'Port Label', 'Monitor Type', 'Location'],
                   optmon_rows)
    _write_rls_csv(ptp_csv,
                   ['TID', 'Shelf', 'Time Of Day', 'Uptime', 'Latest Bin Number', 'Last Operation Result'],
                   ptp_rows)
    _write_rls_csv(slot_sequence_csv,
                   ['TID', 'Shelf', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'Sequence ID', 'Anchor', 'Label', 'Add Sequence', 'Drop Sequence'],
                   slot_sequence_rows)
    _write_rls_csv(adjacencies_csv,
                   ['TID', 'SHELF', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'AID', 'Type', 'Provisioned FE AID', 'Discovered FE AID', 'Provisioned FE Form', 'Discovered FE Form'],
                   [[tid, shelf, _legacy_optical_tuple(iface)[0], _legacy_optical_tuple(iface)[1], _legacy_optical_tuple(iface)[2], _legacy_optical_tuple(iface)[3], iface, 'LLDP', port_id or 'n/a', system_name or 'none', iface.split('-')[0].upper() if iface else 'unknown', system_name or 'unknown'] for tid, shelf, iface, neighbor_present, system_name, mgmt_addr, port_id in adjacency_rows])
    _write_rls_csv(rx_adjacency_csv,
                   ['TID', 'Shelf ID', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'AID', 'Wavelength', 'Discovered Wavelength', 'PState', 'Discovered FE Address'],
                   rx_rows)
    _write_rls_csv(tx_adjacency_csv,
                   ['TID', 'Shelf ID', 'OTS', 'OSID', 'TX Path ID', 'RX Path ID', 'Reliable Far End AID', 'AID', 'Circuit ID', 'Wavelength', 'Discovered Wavelength', 'Frequency (THz)'],
                   tx_rows)
    _write_rls_csv(lldp_csv,
                   ['Local TID', 'Shelf', 'Interface', 'Neighbor Present', 'Neighbor System Name', 'Management Address', 'Neighbor Port'],
                   lldp_rows)
    _write_rls_csv(dcn_csv,
                   ['Section', 'Parameter', 'Value'],
                   dcn_rows)
    _write_rls_csv(logging_csv,
                   ['TID', 'Sequence', 'Source', 'Timestamp', 'User', 'Command', 'Collected Via', 'Status', 'Detail'],
                   logging_rows)
    _write_rls_csv(pm_audit_csv,
                   ['TID', 'PM Area', 'Status', 'Source', 'Additional Detail', 'Assessment'],
                   pm_audit_rows)
    _write_rls_csv(osrp_diag_csv,
                   ['TID', 'Diagnostic Area', 'Visibility', 'Source', 'Notes'],
                   osrp_diag_rows)
    _write_rls_csv(ospf_nodes_csv,
                   ['Local TID', 'Remote TID', 'Remote IP', 'Discovered Via Interface', 'Remote Port'],
                   ospf_rows or [[display_tid, 'none', 'n/a', 'n/a', 'n/a']])
    _write_rls_csv(routing_csv,
                   ['TID', 'Destination System', 'Management Address', 'Outgoing Interface', 'Source'],
                   routing_rows or [[display_tid, 'none', 'n/a', 'n/a', 'n/a']])
    _write_rls_csv(spli_csv,
                   ['TID', 'Shelf', 'TIDIndex', 'Platform', 'FEAID Prefix', 'Node/TID', 'Shelf/Bay', 'IP Address', 'SPLI Comms Type', 'Status', 'Matches', 'SPLI Comms State'],
                   spli_rows)
    _write_rls_csv(issues_csv,
                   ['Severity', 'Category', 'Summary'],
                   issue_rows)

    report_lines.append('[CSV_ARTIFACTS]')
    report_lines.append('\tINFO:\tAdjacencies CSV = ' + adjacencies_csv)
    report_lines.append('\tINFO:\tAlarms CSV = ' + alarms_csv)
    report_lines.append('\tINFO:\tDCN CSV = ' + dcn_csv)
    report_lines.append('\tINFO:\tLogging CSV = ' + logging_csv)
    report_lines.append('\tINFO:\tPM Audit CSV = ' + pm_audit_csv)
    report_lines.append('\tINFO:\tOSRP Diagnostics CSV = ' + osrp_diag_csv)
    report_lines.append('\tINFO:\tDOC CSV = ' + doc_csv)
    report_lines.append('\tINFO:\tEquipment CSV = ' + equipment_csv)
    report_lines.append('\tINFO:\tETTP CSV = ' + ettp_csv)
    report_lines.append('\tINFO:\tInventory CSV = ' + inventory_csv)
    report_lines.append('\tINFO:\tLicenses CSV = ' + licenses_csv)
    report_lines.append('\tINFO:\tLOC CSV = ' + loc_csv)
    report_lines.append('\tINFO:\tNMCMON CSV = ' + nmcmon_csv)
    report_lines.append('\tINFO:\tODUTTP CSV = ' + oduttp_csv)
    report_lines.append('\tINFO:\tOSC CSV = ' + osc_csv)
    report_lines.append('\tINFO:\tOSPF Nodes CSV = ' + ospf_nodes_csv)
    report_lines.append('\tINFO:\tPTP CSV = ' + ptp_csv)
    report_lines.append('\tINFO:\tRouting Table CSV = ' + routing_csv)
    report_lines.append('\tINFO:\tRx Adjacency CSV = ' + rx_adjacency_csv)
    report_lines.append('\tINFO:\tShelves CSV = ' + shelves_csv)
    report_lines.append('\tINFO:\tSlotSequence CSV = ' + slot_sequence_csv)
    report_lines.append('\tINFO:\tTx Adjacency CSV = ' + tx_adjacency_csv)
    report_lines.append('\tINFO:\tSoftware CSV = ' + software_csv)
    report_lines.append('\tINFO:\tSystem Health CSV = ' + system_health_csv)
    report_lines.append('\tINFO:\tLLDP CSV = ' + lldp_csv)
    report_lines.append('\tINFO:\tIssues CSV = ' + issues_csv)
    report_lines.append('')

    report_lines.append('[FILES]')
    report_lines.append('\tINFO:\tSmoke summary file = ' + summary_path)
    report_lines.append('\tINFO:\tGenerated report file = ' + report_path)
    report_lines.append('\tINFO:\tDetected metadata file = ' + variant_note)
    report_lines.append('')

    report_rows = [['Section', 'Status', 'Detail']]
    current_section = 'GENERAL'
    for line in report_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            current_section = stripped[1:-1]
            continue
        match = re.match(r'^(PASS|WARN|INFO):\s*(.*)$', stripped)
        if match:
            report_rows.append([current_section, match.group(1), match.group(2)])
        else:
            report_rows.append([current_section, 'INFO', stripped])

    with open(report_path, 'w', newline='') as f_out:
        writer = csv.writer(f_out)
        writer.writerows(report_rows)
    print('Created CSV: ' + os.path.basename(report_path))

    with open(variant_note, 'w', newline='') as f_out:
        writer = csv.writer(f_out)
        writer.writerow(['Key', 'Value'])
        writer.writerow(['Detected platform family', '6500 RLS'])
        writer.writerow(['Expected TID', EXPECTED_TID])
        writer.writerow(['Detected TID', detected_tid or 'unknown'])
        writer.writerow(['TID Match', tid_match])
        writer.writerow(['Generated report', report_path])
        writer.writerow(['Smoke summary', summary_path])
        writer.writerow(['Adjacencies CSV', adjacencies_csv])
        writer.writerow(['Alarms CSV', alarms_csv])
        writer.writerow(['DCN CSV', dcn_csv])
        writer.writerow(['Logging CSV', logging_csv])
        writer.writerow(['PM Audit CSV', pm_audit_csv])
        writer.writerow(['OSRP Diagnostics CSV', osrp_diag_csv])
        writer.writerow(['DOC CSV', doc_csv])
        writer.writerow(['Equipment CSV', equipment_csv])
        writer.writerow(['ETTP CSV', ettp_csv])
        writer.writerow(['Inventory CSV', inventory_csv])
        writer.writerow(['Licenses CSV', licenses_csv])
        writer.writerow(['LOC CSV', loc_csv])
        writer.writerow(['NMCMON CSV', nmcmon_csv])
        writer.writerow(['ODUTTP CSV', oduttp_csv])
        writer.writerow(['OSC CSV', osc_csv])
        writer.writerow(['OSPF Nodes CSV', ospf_nodes_csv])
        writer.writerow(['PTP CSV', ptp_csv])
        writer.writerow(['Routing Table CSV', routing_csv])
        writer.writerow(['Rx Adjacency CSV', rx_adjacency_csv])
        writer.writerow(['Shelves CSV', shelves_csv])
        writer.writerow(['SlotSequence CSV', slot_sequence_csv])
        writer.writerow(['Tx Adjacency CSV', tx_adjacency_csv])
        writer.writerow(['Software CSV', software_csv])
        writer.writerow(['System Health CSV', system_health_csv])
        writer.writerow(['LLDP CSV', lldp_csv])
        writer.writerow(['Issues CSV', issues_csv])
    print('Created CSV: ' + os.path.basename(variant_note))

    xlsx_path = _consolidate_rls_csv_to_xlsx(WindowsHost, display_tid, cleanup_csvs=False)
    debug_xlsx_path = _consolidate_rls_csv_to_debug_xlsx(WindowsHost, display_tid)
    # In walk mode the per-host CSVs are consumed by RLS_Network_Audit.py
    # to build a span-wide workbook, so suppress the cleanup here. The
    # audit orchestrator removes them after the network workbook is
    # written.
    if not WALK_MODE:
        _cleanup_rls_csv_artifacts(glob.glob(WindowsHost + '_RLS_*.csv'))

    if xlsx_path:
        print('6500 RLS workbook generated: ' + xlsx_path)
        print('RLS CSV artifacts consolidated and removed after workbook creation.')
    else:
        print('6500 RLS CSV artifacts created; workbook generation unavailable in this environment.')
    
    if debug_xlsx_path:
        print('6500 RLS Debug workbook generated: ' + debug_xlsx_path)

    if RUN_VALIDATIONS:
        try:
            import rls_validations
            host_data = {
                'expected_tid': expected_tid_clean,
                'detected_tid': detected_tid,
                'active_version': active_version,
                'running_version': running_version,
                'committed_version': committed_version,
                'cpu_idle': cpu_idle,
                'mem_used': mem_used,
                'critical': critical,
                'major': major,
                'minor': minor,
                'warning': warning,
                'osc_neighbor_ifaces': [i for i in neighbor_ifaces if 'osc' in (i or '').lower()],
                'osc_power_map': osc_power_map,
                'total_ok': total_ok,
                'total_warn': total_warn,
            }
            verdicts = rls_validations.run_validations(host_data)
            validation_csv = WindowsHost + '_RLS_Validation.csv'
            rls_validations.write_csv(verdicts, validation_csv)
            counts = rls_validations.summarize(verdicts)
            print('Validations: PASS=%d WARN=%d FAIL=%d INFO=%d -> %s' % (
                counts.get('PASS', 0), counts.get('WARN', 0),
                counts.get('FAIL', 0), counts.get('INFO', 0), validation_csv))
        except Exception as _verr:
            print('Validation step failed: %s' % str(_verr))
            F_DBG.write('\nValidation step failed: %s\n' % str(_verr))

    if WALK_MODE:
        try:
            walk_neighbors_csv = WindowsHost + '_RLS_Walk_Neighbors.csv'
            with open(walk_neighbors_csv, 'w', newline='') as _f:
                _w = csv.writer(_f)
                _w.writerow(['Interface', 'Neighbor System Name', 'Management Address', 'Neighbor Port'])
                for iface, system_name, mgmt_addr, port_id in neighbor_details:
                    _w.writerow([iface, system_name or '', mgmt_addr or '', port_id or ''])
            print('Walk neighbors written: ' + walk_neighbors_csv)
        except Exception as _werr:
            print('Walk neighbor export failed: %s' % str(_werr))

    return ErrorMessage



def MICROSOFT(TID, dCPACK):
    global dMSFT__SHELFID_PARAM
    PRINT_DICTIONARY(dMSFT__SHELFID_PARAM, 'Microsoft', 'dMSFT__SHELFID_PARAM')
    PRI_SHELF = dMSFT__SHELFID_PARAM['PRIMARY']
    try:
        list1 = dMSFT__SHELFID_PARAM[PRI_SHELF + '+CP']
        if any(('FIM_5' in s for s in list1)):
            SITE_TYPE = 'OLR'
        elif any(('WSS' in s for s in list1)):
            SITE_TYPE = 'OLT'
        else:
            SITE_TYPE = 'OLA'
    except:
        SITE_TYPE = 'UNKNOWN'

    ReportOut = 'TID = ' + TID + '\t\tSite Type = ' + SITE_TYPE + '\n\n'
    if TID.upper() != TID:
        ReportOut += '\tFAIL:\t TID name has lower case characters\n'
    else:
        ReportOut += '\tPASS:\t TID has upper case characters only\n\n'
    lSHELVES = dMSFT__SHELFID_PARAM['SHELVES']
    for shelf in lSHELVES:
        ReportOut += 'SHELF ID = ' + shelf + '\n'
        if dMSFT__SHELFID_PARAM[shelf + '+TIDC'] == 'ENABLED':
            ReportOut += '\tPASS:\t TIDc ENABLED\n'
        else:
            ReportOut += '\tFAIL:\t TIDc DISABLED\n'
        if dMSFT__SHELFID_PARAM[shelf + '+TELNET-SERVER'] == 'DISABLED':
            ReportOut += '\tPASS:\t TELNET DISABLED\n'
        else:
            ReportOut += '\tFAIL:\t TELNET ENABLED\n'
        if dMSFT__SHELFID_PARAM[shelf + '+HTTP'] == 'DISABLED':
            ReportOut += '\tPASS:\t HTTP DISABLED\n'
        else:
            ReportOut += '\tFAIL:\t HTTP ENABLED\n'
        if dMSFT__SHELFID_PARAM[shelf + '+HTTPS'] == 'DISABLED':
            ReportOut += '\tPASS:\t HTTPS DISABLED\n'
        else:
            ReportOut += '\tFAIL:\t HTTPS ENABLED\n'
        if dMSFT__SHELFID_PARAM[shelf + '+REST'] == 'ENABLED':
            ReportOut += '\tPASS:\t REST ENABLED\n'
        else:
            ReportOut += '\tFAIL:\t REST DISABLED\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SSH-MAXSESSIONS']
        if int(f1) > 18:
            ReportOut += '\tPASS:\t SSH MAXSESSIONS = ' + f1 + '\n'
        else:
            ReportOut += '\tFAIL:\t SSH MAXSESSIONS = ' + f1 + '\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SSH-KEYSIZE']
        if f1 == '2048':
            ReportOut += '\tPASS:\t SSH KEYSIZE = 2048\n'
        else:
            ReportOut += '\tFAIL:\t SSH KEYSIZE = ' + f1 + '\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SSH-KEYTYPE']
        if f1 == 'RSA':
            ReportOut += '\tPASS:\t SSH KEYTYPE = RSA\n'
        else:
            ReportOut += '\tFAIL:\t SSH KEYTYPE = ' + f1 + '\n'
            f1 = dMSFT__SHELFID_PARAM[shelf + '+SSH-KEYTYPE']
            if f1 == 'RSA':
                ReportOut += '\tPASS:\t SSH KEYTYPE = RSA\n'
            else:
                ReportOut += '\tFAIL:\t SSH KEYTYPE = ' + f1 + '\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SSL-MINVER']
        if f1 == 'TLS11':
            ReportOut += '\tPASS:\t Minumum TLS Level = TLS11\n'
        else:
            ReportOut += '\tFAIL:\t Minumum TLS Level = ' + f1 + '\n'
        if dMSFT__SHELFID_PARAM[shelf + '+SNMPAGENT'] == 'ENABLED':
            ReportOut += '\tPASS:\t SNMP Agent ENABLED\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Agent DISABLED\n'
        if dMSFT__SHELFID_PARAM[shelf + '+SNMP-ALMMASKING'] == 'ON':
            ReportOut += '\tPASS:\t SNMP Alarm Masking ON\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Alarm Masking OFF\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SNMP-PROXY']
        if SITE_TYPE == 'OLA':
            if f1 == 'OFF':
                ReportOut += '\tPASS:\t SNMP Proxy OFF\n'
            else:
                ReportOut += '\tFAIL:\t SNMP Proxy ON\n'
        elif f1 == 'ON':
            ReportOut += '\tPASS:\t SNMP Proxy ON\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Proxy OFF\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SNMP-PROXYREQTIMEOUT']
        if SITE_TYPE == 'OLA':
            if f1 == '50':
                ReportOut += '\tPASS:\t SNMP Proxy Timeout = 50\n'
            else:
                ReportOut += '\tFAIL:\t SNMP Proxy Timeout = ' + f1 + '\n'
        elif f1 == '20':
            ReportOut += '\tPASS:\t SNMP Proxy Timeout = 20\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Proxy Timeout = ' + f1 + '\n'
        if dMSFT__SHELFID_PARAM[shelf + '+SNMP-ENHANCEDPROXY'] == 'ON':
            ReportOut += '\tPASS:\t SNMP Enhanced Proxy ON\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Enhanced Proxy OFF\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SNMP-TRAPIF']
        if SITE_TYPE == 'OLA':
            if f1 == 'AUTO':
                ReportOut += '\tPASS:\t SNMP Trap Interface = AUTO\n'
            else:
                ReportOut += '\tFAIL:\t SNMP Trap Interface  = ' + f1 + '\n'
        elif f1 == 'SHELF-IP':
            ReportOut += '\tPASS:\t SNMP Trap Interface = SHELF-IP\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Trap Interface  = ' + f1 + '\n'
        if dMSFT__SHELFID_PARAM[shelf + '+SNMP-TCAREPORTING'] == 'ON':
            ReportOut += '\tPASS:\t SNMP TCA Reporting ON\n'
        else:
            ReportOut += '\tFAIL:\t SNMP TCA Reporting OFF\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SNMP-TRAPMIB']
        if f1 == 'NORTEL':
            ReportOut += '\tPASS:\t SNMP MIB = NORTEL\n'
        else:
            ReportOut += '\tFAIL:\t SNMP MIB = ' + f1 + '\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+SNMP-VERSION']
        if f1 == 'V1V2CV3':
            ReportOut += '\tPASS:\t SNMP Version = V1V2CV3\n'
        else:
            ReportOut += '\tFAIL:\t SNMP Version = ' + f1 + '\n'
        f1 = dMSFT__SHELFID_PARAM[shelf + '+TRAP-DEST']
        for i in f1:
            if i.find('ID = 1') > -1:
                if i.find('ID = 1 & IP Address = 10.20.6.16 & UDP port = 162 & Version = V2C & UAP/UID = SYSADMIN & Trap Config = ENABLE_ALL') > -1:
                    ReportOut += '\tPASS:\t SNMP First Trap Destination: ' + i + '\n'
                else:
                    ReportOut += '\tFAIL:\t SNMP First Trap Destination: ' + i + '\n'
            elif i.find('0.0.0.0') > -1:
                continue
            else:
                ReportOut += '\tFAIL:\t Redundant SNMP Trap Destination: ' + i + '\n'

        f1 = dMSFT__SHELFID_PARAM[shelf + '+BITSMODE']
        if SITE_TYPE != 'OLA':
            if f1 == 'SONET':
                ReportOut += '\tPASS:\t Node BITSMODE is SONET\n'
            else:
                ReportOut += '\tFAIL:\t Node BITSMODE is ' + f1 + '\n'
        try:
            f1 = dMSFT__SHELFID_PARAM[shelf + '+TOD-SERADDRESS1']
            if f1 == '10.3.148.170':
                ReportOut += '\tPASS:\t TOD Server Address = 10.3.148.170\n'
            else:
                ReportOut += '\tFAIL:\t TOD Server Address = ' + f1 + '\n'
        except:
            pass

        try:
            f1 = dMSFT__SHELFID_PARAM[shelf + '+SPANLOSSMARGIN']
            if len(f1) == 0:
                ReportOut += '\tPASS:\t All Span Loss Margins were 2.00 dB\n'
            else:
                for i in f1:
                    f2 = i.split('+')
                    ReportOut += '\tFAIL:\t Line adjacency assigned to slot = ' + f2[0] + ' had Span Loss Margin = ' + f2[1] + '\n'

        except:
            pass

    print (ReportOut)
    return None


def PARSE_RTRV_EQUIPMENT(linesIn, TID, SITE_NAME, FileOut, F_ERROR):
    f1 = 'TID,Site Name,SHELF,AID,Pstate,Sstate,PEC,CP Description,Provisioned PEC,Baseline,Serial#,CLEI,Auto Equip,'
    f1 += 'Equipment Mode,Mate Equipment1,Mate Equipment2,Mate Equipment3,'
    f1 += 'Provisioning Mode,Timing Group ID,Equipment Profile,Equipment Profile 2,On since (YY-DDD-HH-MM),Carrier 1,Carrier 2,\n'
    F_OUT = open(FileOut, 'w')
    F_OUT.write(f1)
    d_AUTOEQ__ShSl = {}
    dMODE__ShSl = {}
    SH0 = -1
    lSHELF_XC = []
    dCTYPE = {}
    dCPACK = {}
    dCPPEC = {}
    d_EQUIPMENT_STATE__AID = {}
    needOPTMON = []
    dSP = {}
    lSHELVES = []
    reportWSS = 0
    reportDISP = 0
    for line in linesIn:
        if line.find('RTRV-') > -1:
            continue
        if line.find('::') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('%HLINK-OC3') > -1:
                s1 = AID.split('-')
                Sh = s1[4]
                SHELF = 'SHELF-' + s1[4]
                ShSl = '-' + s1[1] + '-' + s1[2]
            else:
                s1 = AID.split('-')
                if AID.count('-') > 1:
                    Sh = s1[1]
                    SHELF = 'SHELF-' + Sh
                    ShSl = '-' + Sh + '-' + s1[2]
                else:
                    Sh = '0'
                    ShSl = '-' + Sh + '-' + s1[1]
            if SHELF not in lSHELVES:
                lSHELVES.append(SHELF)
            EQPT_3 = s1[0]
            if line.find('::MODE=') > -1:
                l1 = line.find('=') + 1
                l2 = len(line) - 2
                d_AUTOEQ__ShSl[ShSl] = line[l1:l2]
                continue
            if line.find('EQPTMODE=') > -1:
                f1 = FISH(line, 'EQPTMODE=', ',') + ','
                f2 = line.replace('"', ',')
                if f2.find('MATEEQPT1') > -1:
                    f1 += FISH(f2, 'MATEEQPT1=', ',') + ','
                else:
                    f1 += ','
                if f2.find('MATEEQPT2') > -1:
                    f1 += FISH(f2, 'MATEEQPT2=', ',') + ','
                else:
                    f1 += ','
                if f2.find('MATEEQPT3') > -1:
                    f1 += FISH(f2, 'MATEEQPT3=', ',') + ','
                else:
                    f1 += ','
                dMODE__ShSl[ShSl] = f1
                continue
            SH1 = int(Sh)
            if SH0 != SH1:
                if SH0 == -1:
                    SH0 = SH1
                else:
                    SH0 = SH1
                    F_OUT.write('\n')
            if line.find('CTYPE') < 0:
                continue
            else:
                l1 = line.rfind(':') + 1
                l2 = len(line) - 1
                States = line[l1:l2]
                line = line.replace(':', ',')
            line = line.replace(':', ',')
            PROVPEC = FISH(line, 'PROVPEC=', ',')
            CTYPE = FISH(line, 'CTYPE=\\"', '\\"')
            CTYPE = CTYPE.replace(',', ';')
            PEC = FISH(line, ',PEC=', ',')
            REL = FISH(line, ',REL=', ',')
            SER = FISH(line, ',SER=', ',')
            CLEI = FISH(line, ',CLEI=', ',')
            PROVMODE = FISH(line, 'PROVMODE=', ',')
            TMGID = FISH(line, ',TMGID=', ',')
            AGE = FISH(line, ',AGE=', ',')
            ONSC = FISH(line, ',ONSC=', ',')
            EQPTPROFILE = FISH(line, ',EQPTPROFILE=', ',')
            EQPTPROFILE2 = FISH(line, ',EQPTPROFILE2=', ',')
            CARRIER1 = FISH(line, ',CARRIER1=', ',')
            CARRIER2 = FISH(line, ',CARRIER2=', ',')
            if AID.count('-') == 2:
                try:
                    f2 = dMODE__ShSl[ShSl]
                except:
                    f2 = ',,,,'

            else:
                f2 = ',,,,'
            try:
                f1 = d_AUTOEQ__ShSl[ShSl]
            except:
                try:
                    f1 = SHELF.replace('SHELF-', '')
                    l1 = ShSl.replace(f1, '0')
                    f1 = d_AUTOEQ__ShSl[l1]
                except:
                    f1 = ''

            s1 = SHELF + ',' + AID + ',' + States + ',' + PEC + ',' + CTYPE + ',' + PROVPEC + ',' + REL + ',' + SER + ',' + CLEI + ',' + f1 + ',' + f2 + PROVMODE + ',' + TMGID + ',' + EQPTPROFILE + ',' + EQPTPROFILE2 + ',' + ONSC + ',' + CARRIER1 + ',' + CARRIER2
            F_OUT.write(TID + ',' + SITE_NAME + ',' + s1 + '\n')
            d_EQUIPMENT_STATE__AID[ShSl] = States.replace(',', ' & ')
            f2 = States.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1]
            except:
                SEC = ''

            l1 = AID.find('-') + 1
            l2 = AID.rfind('-')
            SHELF = 'SHELF-' + AID[l1:l2]
            if SHELF.count('-') == 2:
                l2 = SHELF.rfind('-')
                SHELF = SHELF[0:l2]
            if line.find(' "SP-') > -1:
                try:
                    dSP[SHELF] += '+' + SEC
                except:
                    dSP[SHELF] = SEC

            l1 = line
            l1 = AID.count('-')
            if AID.count('-') == 2:
                l1 = AID.find('-')
                _Sh_Sl_ = AID[l1:] + '-'
                dCTYPE[_Sh_Sl_] = CTYPE
                if CTYPE.find('LIM') > -1:
                    dCPACK[_Sh_Sl_] = 'LIM'
                    l1 = AID.replace('LIM', 'OPTMON') + '-6'
                    needOPTMON.append(l1)
                elif CTYPE.find('SLA') > -1:
                    dCPACK[_Sh_Sl_] = 'SLA'
                    l1 = AID.replace('LIM', 'OPTMON') + '-6'
                    needOPTMON.append(l1)
                elif CTYPE.find('MLA C') > -1 or CTYPE.find('MLA)') > -1:
                    dCPACK[_Sh_Sl_] = 'MLA'
                elif CTYPE.find('MLA2') > -1:
                    dCPACK[_Sh_Sl_] = 'MLA2'
                elif CTYPE.find('MLA3') > -1:
                    dCPACK[_Sh_Sl_] = 'MLA3'
                elif CTYPE.find('XLA C-Band') > -1:
                    dCPACK[_Sh_Sl_] = 'XLA'
                elif CTYPE.find('SRA C-Band') > -1:
                    dCPACK[_Sh_Sl_] = 'SRA'
                elif AID.find('OPM-') > -1 and AID.find('WSSOPM-') < 0:
                    dCPACK[_Sh_Sl_] = 'OPM'
                elif AID.find('100G') > -1 or CTYPE.find('OTU4') > -1:
                    dCPACK[_Sh_Sl_] = 'OTM4'
                elif AID.find('40G') > -1 or CTYPE.find('OTU3') > -1:
                    dCPACK[_Sh_Sl_] = 'OTM3'
                elif AID.find('10G') > -1 or CTYPE.find('OTU2') > -1 or CTYPE.find('NGM') > -1:
                    dCPACK[_Sh_Sl_] = 'OTM2'
                elif CTYPE.find('CCMD12') > -1:
                    dCPACK[_Sh_Sl_] = 'CCMD12'
                elif CTYPE.find('SMD ') > -1:
                    dCPACK[_Sh_Sl_] = 'SMD'
                elif AID.find('WSS-') > -1 or AID.find('WSSOPM-') > -1:
                    dCPACK[_Sh_Sl_] = 'WSS'
                    reportWSS = 1
                elif AID.find('FIM-') > -1:
                    if line.find('Type 1') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_1'
                        EQPT_3 += '_1'
                    if line.find('Type 2') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_2'
                        EQPT_3 += '_2'
                    if line.find('Type 3') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_3'
                        EQPT_3 += '_3'
                    if line.find('Type 4') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_4'
                        EQPT_3 += '_4'
                    if line.find('Type 5') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_5'
                        EQPT_3 += '_5'
                    if line.find('Type 6') > -1:
                        dCPACK[_Sh_Sl_] = 'FIM_6'
                        EQPT_3 += '_6'
                elif AID.find('XC-') > -1:
                    dCPACK[_Sh_Sl_] = 'XC'
                    lSHELF_XC.append(SHELF)
                else:
                    f1 = AID.split('-')
                    dCPACK[_Sh_Sl_] = AID[:l1]
                if CTYPE.find('DSCM ') > -1:
                    dCPACK[_Sh_Sl_] = 'DSCM'
                    reportDISP = 1
                if CTYPE.find('Optical Attenuator') > -1:
                    f1 = CTYPE.split(' ')
                    dCPACK[_Sh_Sl_] = f1[1] + ' dB Pad'
                    reportDISP = 1
                i = dCPACK[_Sh_Sl_] + _Sh_Sl_
                dCPPEC[i] = PEC
            elif AID.count('-') == 3:
                l1 = AID.find('-') + 1
                _Sh_Sl_Pt = '-' + AID[l1:]
                if CTYPE.find('OC-3') and CTYPE.find('CWDM') > -1 or CTYPE.find('OSC') > -1 and CTYPE.find('w/WSC') > -1:
                    dCPACK[_Sh_Sl_Pt] = 'OSC-SFP'
                    dCTYPE[_Sh_Sl_Pt] = CTYPE
                if CTYPE.find('OC-3 12-44dB DWDM 1516.9 nm') > -1 or CTYPE.find('OC-3 12-42dB CWDM 1511 nm') > -1 or CTYPE.find('OC-3 0-34dB CWDM 1511 nm') > -1:
                    dCPACK[_Sh_Sl_Pt] = 'SRA-OSC-SFP'
                    dCTYPE[_Sh_Sl_Pt] = CTYPE
            if EQPT_3.find('WSS') > -1 or EQPT_3.find('XLA') > -1 or EQPT_3.find('SAM') > -1 or EQPT_3.find('ESAM') > -1 or EQPT_3.find('BMD2') > -1 or EQPT_3.find('FIM_4') > -1 or EQPT_3.find('FIM_5') > -1:
                f1 = EQPT_3 + ShSl
                try:
                    dMSFT__SHELFID_PARAM[SHELF + '+CP'].append(f1)
                except:
                    dMSFT__SHELFID_PARAM[SHELF + '+CP'] = [f1]

    dMSFT__SHELFID_PARAM['SHELVES'] = lSHELVES
    for i in dSP:
        f1 = dSP[i]
        if f1.count('+') > 0 and f1.find('STBYH') < 0 and f1.find('STBY') < 0:
            F_ERROR.write('\nEquipment Provisioning Issues (see tab Equipment)\n,' + i + ',The two SP are not mated or one SP is OOS\n')

    del dSP
    F_OUT.close()
    return (lSHELF_XC,
    dCTYPE,
    dCPACK,
    dCPPEC,
    needOPTMON,
    d_EQUIPMENT_STATE__AID,
    reportWSS,
    reportDISP)


def PARSE_RTRV_SNMP(linesIn, TID, F_NOW):
    s_snmp = 'TID,Shelf,Agent State,Version,Alarm Masking,TCA Reporting,Proxy,Enhanced Proxy,Proxy Timeout,Trap Interface,Trap MIB,\n'
    s_dest = 'TID,AID,Index,IP Address,UDP Port,Version,UAP/UID,TrapConfig,\n'
    for line in linesIn:
        if line.find('SNMPAGENT') > -1:
            line = line.replace('"', ',')
            l1 = line.find(':')
            AID = line[4:l1]
            f1 = FISH(line, 'SNMPAGENT=', ',')
            f1 += ',' + FISH(line, ',VERSION=', ',')
            f1 += ',' + FISH(line, ',ALMMASKING=', ',')
            f1 += ',' + FISH(line, ',TCAREPORTING=', ',')
            f1 += ',' + FISH(line, ',PROXY=', ',')
            f1 += ',' + FISH(line, ',ENHANCEDPROXY=', ',')
            f1 += ',' + FISH(line, ',PROXYREQTIMEOUT', ',')
            f1 += ',' + FISH(line, ',TRAPIF=', ',')
            f1 += ',' + FISH(line, ',TRAPMIB=', ',')
            s_snmp += TID + ',' + AID + ',' + f1 + '\n'
        elif line.find('TRAPCONFIG') > -1:
            line = line.replace('"', ',')
            l1 = line.find(':')
            AID = line[4:l1]
            l1 = AID.rfind('-') + 1
            f1 = AID[l1:]
            f1 += ',' + FISH(line, 'IPADDR=', ',')
            f1 += ',' + FISH(line, ',UDPPORT=', ',')
            f1 += ',' + FISH(line, ',VERSION=', ',')
            if line.find(',UID=') > -1:
                f1 += ',' + FISH(line, ',UID=', ',')
            else:
                f1 += ',' + FISH(line, ',UAP=', ',')
            f1 += ',' + FISH(line, ',TRAPCONFIG=', ',')
            s_dest += TID + ',' + AID + ',' + f1 + '\n'

    F_NOW.write(s_snmp + '\n\n\n\n\n\n' + s_dest)
    return None


def PARSE_RTRV_SPLI(linesIn, TID, F_NOW):
    F_NOW.write('TID,Shelf,TIDIndex,Platform,FEAID Prefix,Node/TID,Shelf/Bay,IP Address,SPLI Comms Type,Status,Matches,SPLI Comms State\n')
    for line in linesIn:
        if line.find('PLATFORMTYPE') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            f1 = FISH(line, 'INDEX=', ',')
            f1 += ',' + FISH(line, ',PLATFORMTYPE=', ',')
            f1 += ',' + FISH(line, ',KEYFORMAT=', ',')
            f2 = FISH(line, 'SPLI_ID=\\"', '\\"')
            if line.find('KEYFORMAT=NODENAME') > -1:
                f1 += ',' + f2 + ','
            else:
                l1 = f2.rfind('-')
                f1 += ',' + f2[:l1]
                l1 += 1
                f1 += ',' + f2[l1:]
            line = line.replace('"', ',')
            f1 += ',' + FISH(line, ',FENDIPADDR=', ',')
            f1 += ',' + FISH(line, ',COMMSTYPE=', ',')
            f1 += ',' + FISH(line, ',SPLISTATUS=', ',')
            f1 += ',' + FISH(line, ',NUMSPLIMATCHES=', ',')
            f1 += ',' + FISH(line, ',COMMSSTATE=', ',')
            F_NOW.write(TID + ',' + AID + ',' + f1 + '\n')

    return None


def PARSE_IISIS(linesIn, TID, F_NOW):
    F_NOW.write('TID,Unit,Carrier,Circuit Default Metric,Level 2 Only,3 Way Handshake,Neighbour Protocol Supported Overrite,Route Level,L1 Priority,L2 Priority,Route Summarization,\n')
    F_NOW.write(',CIRCUIT,CIRCUIT,CIRCUIT,CIRCUIT,CIRCUIT,CIRCUIT,ROUTER,ROUTER,ROUTER,ROUTER,\n')
    dRouter__AID = {}
    for line in linesIn:
        if line.find('ROUTESUMMARISATION') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            line = line.replace('"', ',')
            f1 = FISH(line, 'ROUTERLEVEL=', ',')
            f1 += ',' + FISH(line, ',L1PRIORITY=', ',')
            f1 += ',' + FISH(line, ',L2PRIORITY=', ',')
            f1 += ',' + FISH(line, ',ROUTESUMMARISATION=', ',')
            dRouter__AID[AID] = f1
        elif line.find('CARRIER') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            f1 = FISH(line, ',CARRIER=', ',')
            f1 += ',' + FISH(line, 'CKTDEFMETRIC=', ',')
            f1 += ',' + FISH(line, ',L2ONLY=', ',')
            f1 += ',' + FISH(line, ',THREEWAYHS=', ',')
            f1 += ',' + FISH(line, ',NPSOVERRIDE=', ',')
            try:
                f2 = dRouter__AID[AID]
            except:
                f2 = ''

            F_NOW.write(TID + ',' + AID + ',' + f1 + ',' + f2 + '\n')

    return None


def PARSE_OTN_PROTECTION(linesIn, TID, WindowsHost):
    F_NOW = open(WindowsHost + '_OTN_Protection.csv', 'w')
    F_NOW.write('TID,Shelf ID,Unit,Group ID,Group Name,Working Member,Working SNC,Protect Member,Protect SNC,Scheme,PriState,SecState,Status,Switch End,Reason,Reversion failures,Revertive ?,Wait to Restore,Switch Direction,Detection Guard Time,Detection Recovery Time,Signaling Type,Reversion Time,Time of day (TOD) for home path reversion (TODR),Acceptable duration for TODR,OD reversion holdback enabled,TOD reversion holdback period,Holdback signal degrade threshold,List of TODR profiles,\n')
    dSTATUS__ShId = {}
    for line in linesIn:
        if line.find(' "PROTGRP') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            UNIT = line[4:l1]
            f2 = UNIT.split('-')
            SHELF = f2[1]
            ProtID = f2[2]
            if line.find(',ACTIVE=') > -1:
                MBR = FISH(line, 'MBR=', ',')
                SWSTATUS = FISH(line, 'SWSTATUS=', ',')
                SWEND = FISH(line, ',SWEND=', ',')
                f2 = FISH(line, 'SWREASON=', ',')
                if f2 == 'SIGOK':
                    SWREASON = 'Signal OK'
                elif f2 == 'SF':
                    SWREASON = 'Auto switch due to Signal Fail'
                elif f2 == 'SD':
                    SWREASON = 'Auto switch due to Signal Degrade'
                elif f2 == 'WTR':
                    SWREASON = 'Auto switch active; Wait to retore not expired'
                elif f2 == 'AIS':
                    SWREASON = 'Auto switch due to path Alarm Indicaton Signal (AIS)'
                elif f2 == 'OCI':
                    SWREASON = 'Auto switch due to Open Connection Indicator (OCI)'
                elif f2 == 'LCK':
                    SWREASON = 'Auto switch due to path LCK'
                elif f2 == 'PLM':
                    SWREASON = 'Auto switch due to Payload Mismatch (PLM)'
                elif f2 == 'TIM':
                    SWREASON = 'Auto switch due to Trace Indentifier Mismatch (TIM)'
                elif f2 == 'LOF':
                    SWREASON = 'Auto switch due to Loss of Frame (LOF)'
                elif f2 == 'NHP':
                    SWREASON = 'Auto switch : Working leg is not on home route.'
                elif f2 == 'TODR':
                    SWREASON = 'Waiting for Time of Day Reversion window to open.'
                elif f2 == 'TODRHB':
                    SWREASON = 'Outside of TODR window and/or holdback requirement not met.'
                elif f2 == 'TRFLT':
                    SWREASON = 'Autonomous switch triggered by transponder'
                else:
                    SWREASON = f2
                REVFAILCNT = FISH(line, 'REVFAILCNT=', ',')
                TODRHBSDCLNTIME = FISH(line, 'TODRHBSDCLNTIME=\\"', '\\"')
                ACTIVE = FISH(line, ',ACTIVE=', ',')
                f2 = '-' + SHELF + '-' + ProtID
                dSTATUS__ShId[f2] = SWSTATUS + ',' + SWEND + ',' + SWREASON + ',' + REVFAILCNT
            elif line.find('WRKMBR') > -1:
                l1 = line.rfind(':') + 1
                STATES = line[l1:-1]
                line = line.replace(':', ',')
                LABEL = '"' + FISH(line, 'LABEL=\\"', '\\"') + '"'
                WRKMBR = FISH(line, 'WRKMBR=', ',')
                WRKSNC = FISH(line, 'WRKSNC=\\"', '\\"')
                PROTMBR = FISH(line, 'PROTMBR=', ',')
                PROTSNC = FISH(line, 'PROTSNC=\\"', '\\"')
                PS = FISH(line, ',PS=', ',')
                f2 = '-' + SHELF + '-' + ProtID
                try:
                    STATUS = dSTATUS__ShId[f2]
                except:
                    STATUS = ',,,'

                RVRTV = FISH(line, 'RVRTV=', ',')
                WR = FISH(line, ',WR=', ',')
                if line.find('PSDIRN=UNI') > -1:
                    PSDIRN = 'Unidirectional'
                else:
                    PSDIRN = 'Bidirectional'
                TDG = FISH(line, ',TDG=', ',')
                TRG = FISH(line, ',TRG=', ',')
                SIGTYPE = FISH(line, ',SIGTYPE=', ',')
                RVRTTYPE = FISH(line, ',RVRTTYPE=', ',')
                TODRTIME = FISH(line, ',TODRTIME=', ',')
                TODRPRD = FISH(line, ',TODRPRD=', ',')
                TODRHBEN = FISH(line, ',TODRHBEN=', ',')
                TODRHBPRD = FISH(line, ',TODRHBPRD=', ',')
                f2 = FISH(line, ',HBSDTHRESHOLD=', ',')
                if f2 != '':
                    HBSDTHRESHOLD = '10^' + f2
                else:
                    HBSDTHRESHOLD = ''
                TODRPROFLST = FISH(line, ',TODRPROFLST=', ',')
                f2 = '-' + SHELF + '-' + ProtID
                try:
                    STATUS = dSTATUS__ShId[f2]
                except:
                    STATUS = ',,,'

                F_NOW.write(TID + ',' + SHELF + ',' + UNIT + ',' + ProtID + ',' + LABEL + ',' + WRKMBR + ',' + WRKSNC + ',' + PROTMBR + ',' + PROTSNC + ',' + PS + ',' + STATES + ',' + STATUS + ',' + RVRTV + ',' + WR + ',' + PSDIRN + ',' + TDG + ',' + TRG + ',' + SIGTYPE + ',' + RVRTTYPE + ',' + TODRTIME + ',' + TODRPRD + ',' + TODRHBEN + ',' + TODRHBPRD + ',' + HBSDTHRESHOLD + ',' + TODRPROFLST + ',\n')

    F_NOW.close()
    return None


def LOGIN_CHALLENGE_RESPONSE(LoginCommand):
    errMessage = 'OK'
    capturedText = ''
    beginTime = time.time()
    if METHOD == 'SSH':
        nbytes = 32768
        t = 0.0
        chan_6500.send(LoginCommand)
        while t < TIMEOUT:
            try:
                if chan_6500.recv_ready():
                    capturedText += _recv_text(chan_6500, nbytes)
                else:
                    time.sleep(0.2)
                if capturedText.endswith(PROMPT):
                    break
                else:
                    t = t + 0.25
            except Exception as err:
                errMessage = str(err)
                F_DBG.write('\n%s SSH Channel Error: %s' % (LoginCommand, errMessage))
                F_MISS.write('\n%s SSH Channel Error: %s' % (LoginCommand, errMessage))
                break

        fOut = capturedText.replace('\x08 \x08', '')
        print (capturedText)
        if capturedText.find(' PRTL') > -1:
            capturedText = ''
            startT = time.time()
            TL1Command = 'RTRV-CHALLENGE:::CTAG;'
            chan_6500.send(TL1Command)
            t = 0
            while not capturedText.endswith(PROMPT) and t <= TIMEOUT:
                try:
                    if chan_6500.recv_ready():
                        capturedText += _recv_text(chan_6500, nbytes)
                    t = time.time() - startT
                except Exception as err:
                    errMessage = 'While receiving data:\n' + str(err)
                    F_DBG.write('\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                    F_MISS.write('\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                    break

        else:
            errMessage = '\n ACT-USER command did not report PRTL state'
            F_DBG.write('\n%s Error: %s' % (capturedText, errMessage))
            F_MISS.write('\n%s Error: %s' % (capturedText, errMessage))
            return (fOut, 1)
        fOut += capturedText.replace(':\x08 \x08', '')
        print (capturedText)
        if capturedText.count('\\"') == 2:
            f1 = capturedText.split('\\"')
            capturedText = ''
            challenge = f1[1]
            print ('Challenge = ' + challenge)
            f1 = input('Enter response (do not enclose it in ") >>> ')
            TL1Command = 'ENT-CHALLENGE-RESPONSE:::CTAG::"' + f1 + '";'
            chan_6500.send(TL1Command)
            startT = time.time()
            t = 0
            while not capturedText.endswith(PROMPT) and t <= TIMEOUT:
                try:
                    if chan_6500.recv_ready():
                        capturedText += _recv_text(chan_6500, nbytes)
                    t = time.time() - startT
                except Exception as err:
                    errMessage = 'While receiving data:\n' + str(err)
                    F_DBG.write('\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                    F_MISS.write('\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                    break

        else:
            errMessage = '\n the shelf did not provide challenge string'
            F_DBG.write('\n%s Error: %s' % (capturedText, errMessage))
            F_MISS.write('\n%s Error: %s' % (capturedText, errMessage))
            return (fOut, 1)
        fOut += capturedText.replace(':\x08 \x08', '')
        print (capturedText)
    else:
        try:
            startT = time.time()
            _telnet_write(telnet_6500, LoginCommand)
            capturedText = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
            t = time.time() - startT
        except Exception as err:
            t = time.time() - startT
            errMessage = str(err)
            F_DBG.write('\n%s TELNET Channel Error: %s' % (LoginCommand, errMessage))
            F_MISS.write('\n%s TELNET Channel Error: %s' % (LoginCommand, errMessage))
            return ('', 1)

        fOut = capturedText.replace('\x08 \x08', '')
        print (capturedText)
        if capturedText.find(' PRTL') > -1:
            capturedText = ''
            TL1Command = 'RTRV-CHALLENGE:::CTAG;'
            try:
                _telnet_write(telnet_6500, TL1Command)
                capturedText = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
            except Exception as err:
                errMessage = str(err)
                F_DBG.write('\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))
                F_MISS.write('\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))
                return ('', 1)

        else:
            errMessage = fOut + '\n ACT-USER command did not report PRTL state'
            F_DBG.write('\n%s Error: %s' % (TL1Command, errMessage))
            F_MISS.write('\n%s Error: %s' % (TL1Command, errMessage))
            return ('', 1)
        fOut += capturedText.replace(':\x08 \x08', '')
        print (capturedText)
        if capturedText.count('\\"') == 2:
            f1 = capturedText.split('\\"')
            capturedText = ''
            challenge = f1[1]
            print ('Challenge = ' + challenge)
            f1 = input('Enter response (do not enclose it in quotes) >>> ')
            TL1Command = 'ENT-CHALLENGE-RESPONSE:::CTAG::"' + f1 + '";'
            try:
                _telnet_write(telnet_6500, TL1Command)
                capturedText = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
            except Exception as err:
                errMessage = str(err)
                F_DBG.write('\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))
                F_MISS.write('\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))
                return ('', 1)

        else:
            errMessage = capturedText + '\n without challenge string'
            F_DBG.write('\n%s Error: %s' % (TL1Command, errMessage))
            F_MISS.write('\n%s Error: %s' % (TL1Command, errMessage))
            return (fOut, 1)
        fOut += capturedText.replace(':\x08 \x08', '')
        print (capturedText)
    fOut = fOut.replace('\r\n<\r\n\n', '\r\n')
    fOut = fOut.replace('\r\n>\r\n\n', '\r\n')
    return (fOut, 0)


def PARSE_RTRV_LICENSE_and_SERVER(linesIn, TID, WindowsHost):
    dSERVER__SHELF = {}
    fOut = ''
    for line in linesIn:
        if line.find('PRIMARYIP=') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            SHELF = line[4:l1]
            PRIIP = FISH(line, 'PRIMARYIP=\\"', '\\"')
            PORT = FISH(line, 'PORT=', ',')
            PROTOCOL = FISH(line, 'PROTOCOL=', ',')
            SECIP = FISH(line, 'SECONDARYIP=\\"', '\\"')
            PROXY = FISH(line, 'PROXY=', ',')
            PROXYIP = FISH(line, 'PROXYIP=\\"', '\\"')
            PROXYPORT = FISH(line, 'PROXYPORT=', ',')
            AUDITTIME = FISH(line, 'AUDITTIME=', ',')
            PRISTATUS = FISH(line, 'PRISTATUS=', ',')
            SECSTATUS = FISH(line, 'SECSTATUS=', ',')
            LAST = FISH(line, 'LASTEXCHANGE=\\"', '\\"')
            dSERVER__SHELF[SHELF] = PRIIP + ',' + PORT + ',' + PROTOCOL + ',' + SECIP + ',' + PROXY + ',' + PROXYIP + ',' + PROXYPORT + ',' + AUDITTIME + ',' + PRISTATUS + ',' + SECSTATUS + ',' + LAST + ',\n'
        elif line.find('ARREARS=') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            l1 = AID.split('-')
            SHELF = 'SHELF-' + l1[1]
            FEATURE = FISH(line, 'FEATURE=', ',')
            LICENSED = FISH(line, 'LICENSED=', ',')
            ARREARS = FISH(line, 'ARREARS=', ',')
            DESC = FISH(line, 'DESC=\\"', '\\"')
            POLICY = FISH(line, 'POLICY=', ',')
            ORDERCODE = FISH(line, 'ORDERCODE=', ',')
            try:
                f1 = dSERVER__SHELF[SHELF]
            except:
                f1 = '\n'

            fOut += TID + ',' + SHELF + ',' + FEATURE + ',' + LICENSED + ',' + ARREARS + ',' + DESC + ',' + POLICY + ',' + ORDERCODE + ',' + f1

    if fOut != '':
        F_OUT = open(WindowsHost + '_Licenses.csv', 'w')
        F_OUT.write('TID,Shelf,Feature,License,Arrears,Description,Policy,Order Code,Primary IP,Primary Port,Protocol,Secondary IP,HTTP(s) Protocol,Proxy IP,Proxy Port,Daily Audit time,Primary Status,Secondary Status,Last Exchange\n')
        F_OUT.write(fOut)
        F_OUT.close()
    return None


def PARSE_RTRV_DTL(linesIn, TID, WindowsHost, dOSRP_RMTLINKS__NameId):
    dDTLSET__DTL = {}
    s_Dtl = 'Local TID,ID,Label,Service Type,Number of Hops,Source,Usage Type,Destination,Lower Frequency (THz),Upper Frequency (THz),Capacity Change Mode,DTLSET,DTL Role,Route(LinkID = 100 * ShelfID + Slot)\n'
    s_DtlSet = 'ID,Service Type,Label,Working DTL,Protect DTL\n'
    for line in linesIn:
        if line.find('WRKDTL=') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if AID[0:7] == 'DTLSET0-':
                SERVICE = 'PHOTONIC'
            else:
                SERVICE = 'OTN'
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            WRKDTL = FISH(line, 'WRKDTL=', ',')
            dDTLSET__DTL[WRKDTL] = AID + ',WORKING'
            PROTDTLS = FISH(line, 'PROTDTLS=', ',')
            if PROTDTLS.find('NONE') < 0:
                if PROTDTLS.find('&') > -1:
                    f1 = PROTDTLS.split('&')
                    for i in f1:
                        dDTLSET__DTL[i] = AID + ',PROTECT'

                else:
                    dDTLSET__DTL[PROTDTLS] = AID + ',PROTECT'
            s_DtlSet += AID + ',' + SERVICE + ',' + LABEL + ',' + WRKDTL + ',' + PROTDTLS + ',\n'
        elif line.find('TERMNODENAME=') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if AID.find('DTL0-') > -1:
                SERVICE = 'PHOTONIC'
            else:
                SERVICE = 'OTN'
            DTLTYPE = FISH(line, 'DTLTYPE=', ',')
            CCMODE = FISH(line, 'CCMODE=', ',')
            TERMNODENAME = FISH(line, 'TERMNODENAME=\\"', '\\"')
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            MINFREQ = FISH(line, 'MINFREQ=', ',')
            MAXFREQ = FISH(line, 'MAXFREQ=', ',')
            DTLDATA = FISH(line, 'DTLDATA=\\"', '\\"')
            l1 = DTLDATA.find(',')
            SOURCE = DTLDATA[:l1]
            HOPS = str(DTLDATA.count('&') + 1)
            DTLDATA = DTLDATA.replace(',', ':')
            toks = DTLDATA.split('&')
            n = len(toks)
            try:
                DtlSet = dDTLSET__DTL[AID]
            except:
                DtlSet = '-,-'

            f1 = ''
            for i in range(n):
                l1 = toks[i]
                try:
                    f1 += l1 + ' >> ' + dOSRP_RMTLINKS__NameId[l1] + ' & '
                except:
                    f1 != '-'
                    break

            DtlRoute = f1[:-3]
            s_Dtl += TID + ',' + AID + ',' + LABEL + ',' + SERVICE + ',' + HOPS + ',' + SOURCE + ',' + DTLTYPE + ',' + TERMNODENAME + ',' + MINFREQ + ',' + MAXFREQ + ',' + CCMODE + ',' + DtlSet + ',' + DtlRoute + ',\n'

    F_OUT = open(WindowsHost + '_DTL.csv', 'w')
    F_OUT.write(s_Dtl + '\n\n\n\n' + s_DtlSet)
    F_OUT.close()
    return None


def PARSE_RTRV_OSRP_L0_ALL(linesIn, TID, WindowsHost):
    dLINK_INFO__NameId = {}
    stringNodes = ''
    stringLinks = ''
    stringLines = ''
    dMETRICS = {}
    for line in linesIn:
        if line.find('UDPPORT=') > -1:
            line = line[:-2] + ','
            LOCALNODENAME = FISH(line, 'NODENAME=\\"', '\\"')
            LOCALNODEID = FISH(line, 'NODEID=', ',')
            LOCALTL1IP = FISH(line, 'TL1IPADDR=', ',')
            SERVICETYPE = FISH(line, 'TYPE=', ',')
        elif line.find(' "OSRPNODE0-') > -1 or line.find(' "OSRPRMTNODES0-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            TL1IPADDR = FISH(line, 'TL1IPADDR=', ',')
            OSRPNODENAME = FISH(line, 'OSRPNODENAME=\\"', '\\"')
            OSRPNODETYPE = FISH(line, 'OSRPNODETYPE=', ',')
            OSRPBLOCKEDNODEOPERST = FISH(line, 'OSRPBLOCKEDNODEOPERST=', ',')
            OSRPNODEID = FISH(line, ',OSRPNODEID=', ',')
            if line.find('HOTIMER=') > -1:
                LOCALNODEIDHEX = OSRPNODEID
                HOTIMER = FISH(line, 'HOTIMER=', ',')
                LOWPRIOHOFEATURE = FISH(line, 'LOWPRIOHOFEATURE=', ',')
                OOBIPADDR = FISH(line, 'OOBIPADDR=', ',')
                OOBLOCALPORT = FISH(line, 'OOBLOCALPORT=', ',')
                OSRPBLOCKEDNODEADMINST = FISH(line, 'OSRPBLOCKEDNODEADMINST=', ',')
                OSRPBLOCKEDNODEFEATURE = FISH(line, 'OSRPBLOCKEDNODEFEATURE=', ',')
                NBRPS = ''
                LOCAL = ''
            else:
                HOTIMER = ''
                LOWPRIOHOFEATURE = ''
                OOBIPADDR = ''
                OOBLOCALPORT = ''
                OSRPBLOCKEDNODEADMINST = ''
                OSRPBLOCKEDNODEFEATURE = ''
                LOCAL = FISH(line, 'LOCAL=', ',')
                NBRPS = FISH(line, 'NBRPS=', ',')
            stringNodes += TID + ',' + AID + ',' + OSRPNODEID + ',' + OSRPNODENAME + ',' + OSRPNODETYPE + ',' + SERVICETYPE + ',' + TL1IPADDR + ',' + OOBIPADDR + ',' + OOBLOCALPORT + ',' + HOTIMER + ',' + OSRPBLOCKEDNODEFEATURE + ',' + OSRPBLOCKEDNODEADMINST + ',' + OSRPBLOCKEDNODEOPERST + ',' + NBRPS + ',' + LOCAL + ',\n'
        elif line.find(' "OSRPLINK0-') > -1 or line.find(' "OSRPRMTLINKS0-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if line.find(',ADVBW=') > -1:
                SUPPROTTYPE = FISH(line, 'SUPPROTTYPE=', ',')
                GRIDTYPE = FISH(line, 'GRIDTYPE=', ',')
                ADVBW = FISH(line, 'ADVBW=\\"', '\\"')
                toks = ADVBW.split(':')
                try:
                    Available = toks[0] + ':' + toks[2]
                except:
                    Available = ''

                try:
                    Maximum = toks[1] + ':' + toks[2]
                except:
                    Maximum = ''

                if line.find('ORIGLINKID') < 0:
                    l1 = AID.rfind('-') + 1
                    ORIGLINKID = AID[l1:]
                    try:
                        ORIGNODENAME = LOCALNODENAME
                    except:
                        ORIGNODENAME = ''

                    dMETRICS[AID] = SUPPROTTYPE + ',' + GRIDTYPE + ',' + ADVBW + ',' + Available + ',' + Maximum
                else:
                    ORIGNODENAME = FISH(line, 'ORIGNODENAME=\\"', '\\"')
                    ORIGLINKID = FISH(line, 'ORIGLINKID=', ',')
                    dMETRICS[ORIGLINKID] = SUPPROTTYPE + ',' + GRIDTYPE + ',' + ADVBW + ',' + Available + ',' + Maximum
            elif line.find(',HSTATE=') > -1:
                LABEL = FISH(line, 'LABEL=\\"', '\\"')
                PBIDS = FISH(line, 'PBIDS=', ',')
                ADMW = FISH(line, 'ADMW=', ',')
                HSTATE = FISH(line, 'HSTATE=', ',')
                CONSTRAINTFLOODENABLED = FISH(line, 'CONSTRAINTFLOODENABLED=', ',')
                RMTNODENAME = FISH(line, 'RMTNODENAME=\\"', '\\"')
                RMTLINKID = FISH(line, 'RMTLINKID=', ',')
                RMTLABEL = FISH(line, 'RMTLABEL=\\"', '\\"')
                RMTPBIDS = FISH(line, 'RMTPBIDS=', ',')
                RMTADMW = FISH(line, 'RMTADMW=', ',')
                if line.find('ORIGLINKID') < 0:
                    l1 = AID.rfind('-') + 1
                    ORIGNODEID = OSRPNODEID
                    try:
                        ORIGNODENAME = LOCALNODENAME
                    except:
                        ORIGNODENAME = ''

                    ORIGLINKID = AID[l1:]
                    f1 = dMETRICS[AID]
                else:
                    ORIGNODEID = FISH(line, 'ORIGNODEID=', ',')
                    ORIGNODENAME = FISH(line, 'ORIGNODENAME=\\"', '\\"')
                    ORIGLINKID = FISH(line, 'ORIGLINKID=', ',')
                    f1 = dMETRICS[ORIGLINKID]
                stringLinks += TID + ',' + AID + ',' + ORIGNODEID + ',' + ORIGNODENAME + ',' + ORIGLINKID + ',' + LABEL + ',' + RMTNODENAME + ',' + RMTLINKID + ',' + RMTLABEL + ',' + PBIDS + ',' + RMTPBIDS + ',' + ADMW + ',' + RMTADMW + ',' + HSTATE + ',' + CONSTRAINTFLOODENABLED + ',' + f1 + '\n'
        elif line.find(' "OSRPLINE0-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            f1 = FISH(line, 'OSRPLINK=', ',')
            l1 = f1.rfind('-') + 1
            OSRPLINK = f1[l1:]
            LCLSUPTP = FISH(line, 'LCLSUPTP=', ',')
            CMNID = FISH(line, 'CMNID=', ',')
            l1 = line.rfind(':') + 1
            PSTATE = line[l1:-1]
            line = line.replace(':', ',')
            RMTNODENAME = FISH(line, 'RMTNODENAME=\\"', '\\"')
            RMTOSRPLINKID = FISH(line, 'RMTOSRPLINKID=', ',')
            RMTSUPTP = FISH(line, 'RMTSUPTP=', ',')
            RMTCMNID = FISH(line, 'RMTCMNID=', ',')
            RMTLABEL = FISH(line, 'RMTLABEL=\\"', '\\"')
            RMTPST = FISH(line, 'RMTPST=', ',')
            HOLDOFF = FISH(line, 'HOLDOFF=', ',')
            MJHFLACTION = FISH(line, 'MJHFLACTION=', ',')
            APRACTION = FISH(line, 'APRACTION=', ',')
            OOBCMNID = FISH(line, 'OOBCMNID=', ',')
            LINETYPE = FISH(line, 'LINETYPE=', ',')
            STATE = FISH(line, 'STATE=', ',')
            SPANLOSSEXCACTION = FISH(line, 'SPANLOSSEXCACTION=', ',')
            BWLCKOEN = FISH(line, 'BWLCKOEN=', ',')
            IGNOREFAULTS = FISH(line, 'IGNOREFAULTS=', ',')
            RMTSTATE = FISH(line, 'RMTSTATE=', ',')
            RMTBWLCKOEN = FISH(line, 'RMTBWLCKOEN=', ',')
            UNBLOCK = FISH(line, 'UNBLOCK=', ',')
            stringLines += LOCALNODENAME + ',' + AID + ',' + LABEL + ',' + SERVICETYPE + ',' + OSRPLINK + ',' + LCLSUPTP + ',' + CMNID + ',' + PSTATE + ',' + RMTNODENAME + ',' + RMTOSRPLINKID + ',' + RMTSUPTP + ',' + RMTCMNID + ',' + RMTLABEL + ',' + RMTPST + ',' + HOLDOFF + ',' + MJHFLACTION + ',' + APRACTION + ',' + OOBCMNID + ',' + LINETYPE + ',' + STATE + ',' + SPANLOSSEXCACTION + ',' + BWLCKOEN + ',' + IGNOREFAULTS + ',' + RMTSTATE + ',' + RMTBWLCKOEN + ',' + UNBLOCK + '\n'

    if stringNodes != '':
        F_OUT = open(WindowsHost + '_L0_OSRP_Nodes.csv', 'w')
        F_OUT.write('Local TID,AID,OSRP Node ID,Node Name,Node Type,Service Type,TL1 IP Address,OOB IP Address,OOB Local Port,Low Priority Timer (ms),Blocked Node Feature,Admin State,Operational State,Neighbor State,Locality,\n' + stringNodes)
        F_OUT.close()
    if stringLinks != '':
        F_OUT = open(WindowsHost + '_L0_OSRP_Links.csv', 'w')
        F_OUT.write('Local TID,AID,Node ID,Node Name,Link ID,Label,Remote Node name,Remote Link ID,Remote Label,Bundle ID,Remore Bundle ID,Admin Weight,Remote Admin Weight,Hello State,Constraint Flood,Protection,Grid,Advertized BW,Available BW,Maximum BW,\n' + stringLinks)
        F_OUT.close()
    if stringLines != '':
        F_OUT = open(WindowsHost + '_L0_OSRP_Lines.csv', 'w')
        F_OUT.write('Local Node Name,AID,Label,Service Type,Containing Link ID,Local TP,Common ID,Primary State,Neighbor Node,Neighbor Containing Link ID,Neighbor TP,Neighbor Common ID,Neighbor Label,Neighbor Primary State,Line Down Hold-off Timer,Major High Fiber Loss Action,ARP Action,OOB Common ID,Line Type,State,Span Loss Exceed Action,Bandwidth Lockout,Maintenance Mode,Remote State,Remote Bandwidth Lockout,Unblock,\n' + stringLines)
        F_OUT.close()
    return (LOCALNODENAME,
    LOCALNODEID,
    LOCALNODEIDHEX,
    LOCALTL1IP)


def PARSE_ODU_BW_L1(dataIn):
    f2 = dataIn.split('&')
    dataOut = ''
    for i in f2:
        l1 = i.find(':')
        n = i[:l1]
        l1 += 1
        odu = i[l1:]
        if odu == 'ODUFLEXNRSZ':
            dataOut += 'NonResizable [' + n + '] + '
        elif odu == 'ODUFLEXRSZ':
            dataOut += 'Resizable [' + n + '] + '
        else:
            dataOut += odu + '[' + n + '] + '

    return dataOut[:-2]


def PARSE_ODU_BW_METRICS_L1(dataIn):
    AvailableBW = ''
    AdvertizedBW = ''
    MaximumBW = ''
    tokens = dataIn.split('&')
    for i in tokens:
        toks = i.split(':')
        TypeBW = toks[-1]
        if TypeBW == 'ODUFLEXNRSZ':
            f1 = 'NonResizable'
        elif TypeBW == 'ODUFLEXRSZ':
            f1 = 'Resizable'
        else:
            f1 = TypeBW
        if len(toks) == 4:
            AvailableBW += f1 + '[' + toks[0] + '] + '
            AdvertizedBW += f1 + '[' + toks[1] + '] + '
            MaximumBW += f1 + '[' + toks[2] + '] + '
        elif len(toks) == 3:
            AvailableBW += ''
            AdvertizedBW += f1 + '[' + toks[0] + '] + '
            MaximumBW += f1 + '[' + toks[1] + '] + '
        elif len(toks) == 2:
            AvailableBW += f1 + '[' + toks[0] + '] + '
            AdvertizedBW += ''
            MaximumBW += ''

    return (AvailableBW[:-2], AdvertizedBW[:-2], MaximumBW[:-2])


def PARSE_RTRV_OSRP_L1_ALL(linesIn, TID, WindowsHost):
    dLINK_INFO__NameId = {}
    dMETRICS = {}
    dOSRPLINE_SNC = {}
    dOSRPLINK_SNC = {}
    LocalNode = ''
    RemoteNodes = ''
    LocalLinks = ''
    RemoteLinks = ''
    LocalLines = ''
    for line in linesIn:
        if line.find(',NODEID=') > -1:
            line = line[:-2] + ','
            TID_LOCALNODENAME = FISH(line, 'NODENAME=\\"', '\\"')
            TID_NODEID = FISH(line, 'NODEID=', ',')
            TID_TL1IP = FISH(line, 'TL1IPADDR=', ',')
            SERVICETYPE = FISH(line, ',TYPE=', ',')
        elif line.find(' "OSRPNODE-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            OSRPNODENAME = FISH(line, 'OSRPNODENAME=\\"', '\\"')
            OSRPNODEID = FISH(line, ',OSRPNODEID=\\"', '\\"')
            TID_OSRPNODEID = OSRPNODEID
            TL1IPADDR = FISH(line, 'TL1IPADDR=', ',')
            OSRPNODETYPE = FISH(line, 'OSRPNODETYPE=', ',')
            HOTIMER = FISH(line, 'HOTIMER=', ',')
            LOWPRIOHOFEATURE = FISH(line, 'LOWPRIOHOFEATURE=', ',')
            LPHOCTRLINESTATE = FISH(line, 'LPHOCTRLINESTATE=', ',')
            OSRPLNTCMLVL = FISH(line, 'OSRPLNTCMLVL=', ',')
            OSRPSNCTCMLVL = FISH(line, 'OSRPSNCTCMLVL=', ',')
            MBB = FISH(line, 'MBB=', ',')
            RHPCAPABILITY = FISH(line, 'RHPCAPABILITY=', ',')
            OVPNCAPABILITY = FISH(line, 'OVPNCAPABILITY=', ',')
            SNIC = FISH(line, 'SNIC=', ',')
            BWTHRMODE = FISH(line, 'BWTHRMODE=', ',')
            RVRTT = FISH(line, 'RVRTT=', ',')
            if RVRTT.find('SNC_DELAY') > -1:
                RVRTT += ' (Delayed)'
            elif RVRTT.find('SNC_NO_REVERT') > -1:
                RVRTT += ' (No Revert)'
            elif RVRTT.find('SNC_TIMEOFDAY') > -1:
                RVRTT += ' (TODR)'
            TRVRT = FISH(line, 'TRVRT=', ',')
            TODRTIME = FISH(line, 'TODRTIME=', ',')
            TODRPERIOD = FISH(line, 'TODRPERIOD=', ',')
            TODRHBPRD = FISH(line, 'TODRHBPRD=', ',')
            OSRPBLOCKEDNODEFEATURE = FISH(line, 'OSRPBLOCKEDNODEFEATURE=', ',')
            OSRPBLOCKEDNODEOPERST = FISH(line, 'OSRPBLOCKEDNODEOPERST=', ',')
            OSRPBLOCKEDNODEADMINST = FISH(line, 'OSRPBLOCKEDNODEADMINST=', ',')
            f1 = FISH(line, 'OTU2BWT=\\"', '\\"')
            OTU2BWT = PARSE_ODU_BW_L1(f1)
            f1 = FISH(line, 'OTU2EBWT=\\"', '\\"')
            OTU2EBWT = PARSE_ODU_BW_L1(f1)
            f1 = FISH(line, 'OTU3BWT=\\"', '\\"')
            OTU3BWT = PARSE_ODU_BW_L1(f1)
            f1 = FISH(line, 'OTU3E2BWT=\\"', '\\"')
            OTU3E2BWT = PARSE_ODU_BW_L1(f1)
            f1 = FISH(line, 'OTU4BWT=\\"', '\\"')
            OTU4BWT = PARSE_ODU_BW_L1(f1)
            LocalNode = TID + ',' + AID + ',' + OSRPNODEID + ',' + OSRPNODENAME + ',' + OSRPNODETYPE + ',' + SERVICETYPE + ',' + TL1IPADDR + ',' + OSRPBLOCKEDNODEOPERST + ',' + MBB + ',' + RHPCAPABILITY + ',' + OVPNCAPABILITY + ',' + HOTIMER + ',' + LOWPRIOHOFEATURE + ',' + LPHOCTRLINESTATE + ',' + OSRPLNTCMLVL + ',' + OSRPSNCTCMLVL + ',' + SNIC + ',' + BWTHRMODE + ',' + RVRTT + ',' + TRVRT + ',' + TODRTIME + ',' + TODRPERIOD + ',' + TODRHBPRD + ',' + OSRPBLOCKEDNODEFEATURE + ',' + OSRPBLOCKEDNODEADMINST + ',' + OTU2BWT + ',' + OTU2EBWT + ',' + OTU3BWT + ',' + OTU3E2BWT + ',' + OTU4BWT + '\n'
        elif line.find(' "OSRPRMTNODES-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            RMTOSRPNODENAME = FISH(line, 'OSRPNODENAME=\\"', '\\"')
            RMTOSRPNODEID = FISH(line, ',OSRPNODEID=\\"', '\\"')
            RMTTL1IPADDR = FISH(line, 'TL1IPADDR=', ',')
            RMTOSRPNODETYPE = FISH(line, 'OSRPNODETYPE=', ',')
            OSRPBLOCKEDNODEOPERST = FISH(line, 'OSRPBLOCKEDNODEOPERST=', ',')
            MBB = FISH(line, 'MBB=', ',')
            RHPCAPABILITY = FISH(line, 'RHPCAPABILITY=', ',')
            OVPNCAPABILITY = FISH(line, 'OVPNCAPABILITY=', ',')
            LOCAL = FISH(line, 'LOCAL=', ',')
            NBRPS = FISH(line, 'NBRPS=', ',')
            RemoteNodes += TID + ',' + AID + ',' + RMTOSRPNODEID + ',' + RMTOSRPNODENAME + ',' + RMTOSRPNODETYPE + ',' + SERVICETYPE + ',' + RMTTL1IPADDR + ',' + OSRPBLOCKEDNODEOPERST + ',' + MBB + ',' + RHPCAPABILITY + ',' + OVPNCAPABILITY + ',' + ',,,,,,,' + ',,,,,,,' + ',,,,,' + NBRPS + ',' + LOCAL + ',\n'
        elif line.find('  "OSRPLINK-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if line.find('SNCNAME=') > -1:
                SNCNAME = FISH(line, 'SNCNAME=\\"', '\\"')
                ORIGNODENAME = FISH(line, 'ORIGNODENAME=\\"', '\\"')
                PRIORITY = FISH(line, 'PRIORITY=', ',')
                STATE = FISH(line, 'STATE=', ',')
                INGRESSLINKID = FISH(line, 'INGRESSLINKID=', ',')
                EGRESSLINKID = FISH(line, 'EGRESSLINKID=', ',')
                ENDPTSIZE = FISH(line, 'ENDPTSIZE=', ',')
                OVPNID = FISH(line, 'OVPNID=', ',')
                RHPCAPABILITY = FISH(line, 'RHPCAPABILITY=', ',')
                ISHOMEPATH = FISH(line, 'ISHOMEPATH=', ',')
                f1 = 'SNC Name=' + SNCNAME + '  &  Orig. Node Name=' + ORIGNODENAME + '  &  Priority=' + PRIORITY + '  &  Call State=' + STATE + '  &  Ingress Link ID=' + INGRESSLINKID + '  &  Egress Link ID=' + EGRESSLINKID + '  &  Size=' + ENDPTSIZE + ' &  OVPN ID=' + OVPNID + '  & SNC Utilizing RHP=' + RHPCAPABILITY + '  & Home Path Active=' + ISHOMEPATH
                try:
                    f2 = dOSRPLINK_SNC[AID]
                    dOSRPLINK_SNC[AID] = f2 + '\n' + f1
                except:
                    dOSRPLINK_SNC[AID] = f1

            elif line.find('XFERD') > -1:
                f1 = FISH(line, ',ADVBW=\\"', '\\"')
                NAvailableBW, NAdvertizedBW, NMaximumBW = PARSE_ODU_BW_METRICS_L1(f1)
                f1 = FISH(line, ',RHPADVBW=\\"', '\\"')
                RHPAvailableBW, RHPAdvertizedBW, RHPMaximumBW = PARSE_ODU_BW_METRICS_L1(f1)
                if NAvailableBW == RHPAvailableBW:
                    RHPAvl = 'Equal to Normal Priority'
                else:
                    RHPAvl = RHPAvailableBW
                if NAdvertizedBW == RHPAdvertizedBW:
                    RHPAdv = 'Equal to Normal Priority'
                else:
                    RHPAdv = RHPAdvertizedBW
                if NMaximumBW == RHPMaximumBW:
                    RHPMax = 'Equal to Normal Priority'
                else:
                    RHPMax = RHPMaximumBW
                dMETRICS[AID] = NAvailableBW + ',' + RHPAvl + ',' + NAdvertizedBW + ',' + RHPAdv + ',' + NMaximumBW + ',' + RHPMax
            elif line.find(' "OSRPLINK-') > -1 and line.find('ADMW') > -1:
                try:
                    ORIGNODENAME = TID_LOCALNODENAME
                except:
                    ORIGNODENAME = ''

                l1 = AID.rfind('-') + 1
                ORIGLINKID = AID[l1:]
                OSRPNODETYPE = FISH(line, 'OSRPNODETYPE=', ',')
                ORIGNODEID = TID_OSRPNODEID
                RMTNODENAME = FISH(line, 'RMTNODENAME=\\"', '\\"')
                RMTNODEID = FISH(line, 'RMTNODEID=\\"', '\\"')
                RMTLINKID = FISH(line, 'RMTLINKID=', ',')
                RMTNODETYPE = FISH(line, 'RMTNODETYPE=\\"', '\\"')
                LABEL = FISH(line, 'LABEL=\\"', '\\"')
                RMTLABEL = FISH(line, 'RMTLABEL=\\"', '\\"')
                HSTATE = FISH(line, 'HSTATE=', ',')
                PBIDS = FISH(line, 'PBIDS=', ',')
                RMTPBIDS = FISH(line, 'RMTPBIDS=', ',')
                ADMW = FISH(line, 'ADMW=', ',')
                RMTADMW = FISH(line, 'RMTADMW=', ',')
                OSRPLINES = FISH(line, 'OSRPLINES=', ',')
                nLines = str(OSRPLINES.count('OSRPLINE'))
                CONSTRAINTFLOODENABLED = FISH(line, 'CONSTRAINTFLOODENABLED=', ',')
                LATENCYDISCOVERYENABLED = FISH(line, 'LATENCYDISCOVERYENABLED=', ',')
                LINKMAXDELAY = FISH(line, 'LINKMAXDELAY=', ',')
                RMTLINKMAXDELAY = FISH(line, 'RMTLINKMAXDELAY=', ',')
                MANUALDELAY = FISH(line, 'MANUALDELAY=', ',')
                ISMASTER = FISH(line, 'ISMASTER=', ',')
                OVPNID = FISH(line, 'OVPNID=', ',')
                RMTOVPNID = FISH(line, 'RMTOVPNID=', ',')
                HBSDTHRESHOLD = '10^' + FISH(line, 'HBSDTHRESHOLD=', ',')
                BWTHRMODE = FISH(line, 'BWTHRMODE=', ',')
                try:
                    f1 = ',"' + dOSRPLINK_SNC[AID] + '",\n'
                except:
                    f1 = ',\n'

                LocalLinks += AID + ',' + ORIGNODENAME + ',' + ORIGNODEID + ',' + nLines + ',' + SERVICETYPE + ',' + ORIGLINKID + ',' + OSRPNODETYPE + ',' + RMTNODENAME + ',' + RMTNODEID + ',' + RMTLINKID + ',' + RMTNODETYPE + ',' + LABEL + ',' + RMTLABEL + ',' + HSTATE + ',' + PBIDS + ',' + RMTPBIDS + ',' + ADMW + ',' + RMTADMW + ',' + OSRPLINES + ',' + CONSTRAINTFLOODENABLED + ',' + LATENCYDISCOVERYENABLED + ',' + LINKMAXDELAY + ',' + RMTLINKMAXDELAY + ',' + MANUALDELAY + ',' + ISMASTER + ',' + OVPNID + ',' + RMTOVPNID + ',' + HBSDTHRESHOLD + ',' + BWTHRMODE + ',' + dMETRICS[AID] + f1
        elif line.find('  "OSRPRMTLINKS-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if line.find('XFERD') > -1:
                f1 = FISH(line, ',ADVBW=\\"', '\\"')
                NAvailableBW, NAdvertizedBW, NMaximumBW = PARSE_ODU_BW_METRICS_L1(f1)
                f1 = FISH(line, ',RHPADVBW=\\"', '\\"')
                RHPAvailableBW, RHPAdvertizedBW, RHPMaximumBW = PARSE_ODU_BW_METRICS_L1(f1)
                if NAvailableBW == RHPAvailableBW:
                    RHPAvl = 'Equal to Normal Priority'
                else:
                    RHPAvl = RHPAvailableBW
                if NAdvertizedBW == RHPAdvertizedBW:
                    RHPAdv = 'Equal to Normal Priority'
                else:
                    RHPAdv = RHPAdvertizedBW
                if NMaximumBW == RHPMaximumBW:
                    RHPMax = 'Equal to Normal Priority'
                else:
                    RHPMax = RHPMaximumBW
                ORIGLINKID = FISH(line, 'ORIGLINKID=', ',')
                dMETRICS[ORIGLINKID] = NAvailableBW + ',' + RHPAvl + ',' + NAdvertizedBW + ',' + RHPAdv + ',' + NMaximumBW + ',' + RHPMax
            elif line.find('RMTNODEID') > -1:
                l1 = AID.rfind('-') + 1
                ORIGLINKID = AID[l1:]
                ORIGNODENAME = FISH(line, 'ORIGNODENAME=\\"', '\\"')
                ORIGNODEID = FISH(line, 'ORIGNODEID=\\"', '\\"')
                ORIGLINKID = FISH(line, 'ORIGLINKID=', ',')
                OSRPNODETYPE = '-'
                LABEL = FISH(line, 'LABEL=\\"', '\\"')
                RMTNODENAME = FISH(line, 'RMTNODENAME=\\"', '\\"')
                RMTNODEID = FISH(line, 'RMTNODEID=\\"', '\\"')
                RMTLINKID = FISH(line, 'RMTLINKID=', ',')
                RMTNODETYPE = '-'
                RMTLABEL = FISH(line, 'RMTLABEL=\\"', '\\"')
                HSTATE = FISH(line, 'HSTATE=', ',')
                PBIDS = FISH(line, 'PBIDS=', ',')
                RMTPBIDS = FISH(line, 'RMTPBIDS=', ',')
                ADMW = FISH(line, 'ADMW=', ',')
                RMTADMW = FISH(line, 'RMTADMW=', ',')
                OSRPLINES = '-'
                nLines = '-'
                CONSTRAINTFLOODENABLED = '-'
                LATENCYDISCOVERYENABLED = '-'
                LINKMAXDELAY = FISH(line, 'LINKMAXDELAY=', ',')
                RMTLINKMAXDELAY = FISH(line, 'RMTLINKMAXDELAY=', ',')
                MANUALDELAY = FISH(line, 'MANUALDELAY=', ',')
                ISMASTER = FISH(line, 'ISMASTER=', ',')
                OVPNID = FISH(line, 'OVPNID=', ',')
                RMTOVPNID = FISH(line, 'RMTOVPNID=', ',')
                HBSDTHRESHOLD = '-'
                BWTHRMODE = '-'
                RemoteLinks += AID + ',' + ORIGNODENAME + ',' + ORIGNODEID + ',' + nLines + ',' + SERVICETYPE + ',' + ORIGLINKID + ',' + OSRPNODETYPE + ',' + RMTNODENAME + ',' + RMTNODEID + ',' + RMTLINKID + ',' + RMTNODETYPE + ',' + LABEL + ',' + RMTLABEL + ',' + HSTATE + ',' + PBIDS + ',' + RMTPBIDS + ',' + ADMW + ',' + RMTADMW + ',' + OSRPLINES + ',' + CONSTRAINTFLOODENABLED + ',' + LATENCYDISCOVERYENABLED + ',' + LINKMAXDELAY + ',' + RMTLINKMAXDELAY + ',' + MANUALDELAY + ',' + ISMASTER + ',' + OVPNID + ',' + RMTOVPNID + ',' + HBSDTHRESHOLD + ',' + BWTHRMODE + ',' + dMETRICS[ORIGLINKID] + ',\n'
        elif line.find('  "OSRPLINE-') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            AID = line[4:l1]
            if line.find('SNCNAME=') > -1:
                SNCNAME = FISH(line, 'SNCNAME=\\"', '\\"')
                ORIGNODENAME = FISH(line, 'ORIGNODENAME=\\"', '\\"')
                PRIORITY = FISH(line, 'PRIORITY=', ',')
                STATE = FISH(line, 'STATE=', ',')
                INGRESSLINKID = FISH(line, 'INGRESSLINKID=', ',')
                EGRESSLINKID = FISH(line, 'EGRESSLINKID=', ',')
                ENDPTSIZE = FISH(line, 'ENDPTSIZE=', ',')
                OVPNID = FISH(line, 'OVPNID=', ',')
                RHPCAPABILITY = FISH(line, 'RHPCAPABILITY=', ',')
                ISHOMEPATH = FISH(line, 'ISHOMEPATH=', ',')
                f1 = 'SNC Name=' + SNCNAME + '  &  Orig. Node Name=' + ORIGNODENAME + '  &  Priority=' + PRIORITY + '  &  Call State=' + STATE + '  &  Ingress Link ID=' + INGRESSLINKID + '  &  Egress Link ID=' + EGRESSLINKID + '  &  Size=' + ENDPTSIZE + ' &  OVPN ID=' + OVPNID + '  & SNC Utilizing RHP=' + RHPCAPABILITY + '  & Home Path Active=' + ISHOMEPATH
                try:
                    f2 = dOSRPLINE_SNC[AID]
                    dOSRPLINE_SNC[AID] = f2 + '\n' + f1
                except:
                    dOSRPLINE_SNC[AID] = f1

            else:
                LABEL = FISH(line, 'LABEL=\\"', '\\"')
                f1 = FISH(line, 'OSRPLINK=', ',')
                l1 = f1.rfind('-') + 1
                OSRPLINK = f1[l1:]
                LCLSUPTP = FISH(line, 'LCLSUPTP=\\"', '\\"')
                CMNID = FISH(line, 'CMNID=', ',')
                l1 = line.rfind(':') + 1
                PSTATE = line[l1:-1]
                line = line.replace(':', ',')
                RMTNODENAME = FISH(line, 'RMTNODENAME=\\"', '\\"')
                RMTOSRPLINKID = FISH(line, 'RMTOSRPLINKID=', ',')
                RMTSUPTP = FISH(line, 'RMTSUPTP=\\"', '\\"')
                RMTCMNID = FISH(line, 'RMTCMNID=', ',')
                RMTLABEL = FISH(line, 'RMTLABEL=\\"', '\\"')
                RMTPST = FISH(line, 'RMTPST=', ',')
                HOLDOFF = FISH(line, 'HOLDOFF=', ',')
                TODRHBSDCLNTIME = FISH(line, 'TODRHBSDCLNTIME=', ',')
                LINETYPE = FISH(line, 'LINETYPE=', ',')
                DELAY = FISH(line, ',DELAY=', ',')
                RMTDELAY = FISH(line, 'RMTDELAY=', ',')
                BASEOVPNID = FISH(line, 'BASEOVPNID=', ',')
                BWLCKOEN = FISH(line, 'BWLCKOEN=', ',')
                RMTBWLCKOEN = FISH(line, 'RMTBWLCKOEN=', ',')
                try:
                    f1 = ',"' + dOSRPLINE_SNC[AID] + '",\n'
                except:
                    f1 = ',\n'

                LocalLines += TID_LOCALNODENAME + ',' + AID + ',' + LABEL + ',' + SERVICETYPE + ',' + OSRPLINK + ',' + LCLSUPTP + ',' + CMNID + ',' + PSTATE + ',' + RMTNODENAME + ',' + RMTOSRPLINKID + ',' + RMTSUPTP + ',' + RMTCMNID + ',' + RMTLABEL + ',' + RMTPST + ',' + TODRHBSDCLNTIME + ',' + HOLDOFF + ',' + LINETYPE + ',' + DELAY + ',' + RMTDELAY + ',' + BASEOVPNID + ',' + BWLCKOEN + ',' + RMTBWLCKOEN + f1

    if LocalNode != '':
        F_OUT = open(WindowsHost + '_L1_OSRP_Nodes.csv', 'w')
        F_OUT.write('Local TID,AID,OSRP Node ID,Node Name,Node Type,Service Type,TL1 IP Address,Blocked Node State,Make Before Break,Retain Home Path,OVPN Capability,Low Priority HO Timer (ms),Low Priority HO Feature,Low Priority HO State,Lne default TCM,SNC default TCM,SNC Path Events,BW Threshold mode,Reversion,Time to Revert (s),TODR time (hh-mm),TODR Reverse interval (mins),TODR Reverse period (hours),Blocked Node Feature,Blocked Node State,OTU2 BW Thres,OTU2E BW Thres,OTU3 BW Thres,OTU3E2 BW Thres,OTU4B BW Thres,Remote Node Locality,Remote Node Neighbor,\n')
        F_OUT.write(LocalNode + RemoteNodes)
        F_OUT.close()
    if LocalLinks != '':
        F_OUT = open(WindowsHost + '_L1_OSRP_Links.csv', 'w')
        F_OUT.write('AID,Originating Node,Originating Node ID,# Lines,Service Type,Originating Link,Originating Node Type,Terminating Node,Terminating Node ID,Terminating Link, Terminating Node Type,Originating Label,Terminating Label,Hello State,Originating Bundle ID,Terminating Bundle ID,Originating Admin Weight,Terminating Admin Weight,OSRP Lines,Constraint Flood,Latency Discovery,Applied Delay,Remote Applied Delay,Manually Override Delay,Holdback SD,Threshold\tBandwidth,Threshold Mode,# Holdback Thershold,BW Threshold mode,Normal Priority Advertized BW,RHP Priority Advertized BW,Normal Priority Available BW,RHP Priority Available BW,Normal Priority Maximum BW,RHP Priority Maximum BW,OSRP LINK SNC,\n' + LocalLinks + RemoteLinks)
        F_OUT.close()
    if LocalLines != '':
        F_OUT = open(WindowsHost + '_L1_OSRP_Lines.csv', 'w')
        F_OUT.write('Local Node name,AID,Label,Service Type,Containing Link ID,Local TP,Common ID,Primary State,Neighbor Node,Neighbor Containing Link ID,Neighbor TP,Neighbor Common ID,Neighbor Label,Neighbor Primary State,Holdback Line Clean Time,Line Down Hold-off Timer,Line Type,Delay,Remote Delay,OVPN ID,Bandwidth Lockout,Remote Bandwidth Lockout,OSRP LINE SNC\n' + LocalLines)
        F_OUT.close()
    return (TID_LOCALNODENAME,
    TID_NODEID,
    TID_OSRPNODEID,
    TID_TL1IP)


def PARSE_RTRV_PRF_and_LOC(linesIn, lOTSinfo, TID, dCPACK, dOTSinfo_CpShSl, WindowsHost):
    s_prf = ''
    s_loc = ''
    for line in linesIn:
        if line.find('ADJTXRXTYPE=') > -1:
            line = line[:-2] + ','
            l1 = line.find(':')
            LABEL = FISH(line, 'ADJTXRXTYPE=\\"', '\\"')
            AID = line[4:l1]
            ADJTXBIAS = FISH(line, 'ADJTXBIAS=', ',')
            ADJTXCURPOW = FISH(line, 'ADJTXCURPOW=', ',')
            ADJTXMAXPOW = FISH(line, 'ADJTXMAXPOW=', ',')
            ADJTXMINPOW = FISH(line, 'ADJTXMINPOW=', ',')
            ADJTXMODCLASS = FISH(line, 'ADJTXMODCLASS=', ',')
            ADJTXRATE = FISH(line, 'ADJTXRATE=', ',')
            TRANSMODE = FISH(line, 'TRANSMODE=\\"', '\\"')
            CTRLFREQOFFSET = FISH(line, 'CTRLFREQOFFSET=\\"', '\\"')
            TXMINSPECTRALWIDTH = FISH(line, 'TXMINSPECTRALWIDTH=', ',')
            TXSIGBW3DB = FISH(line, 'TXSIGBW3DB=', ',')
            TXSIGBW10DB = FISH(line, 'TXSIGBW10DB=', ',')
            TXFREQRES = FISH(line, 'TXFREQRES=', ',')
            ADJRXNOMINPUT = FISH(line, 'ADJRXNOMINPUT=', ',')
            ADJRXOVERTHRESH = FISH(line, 'ADJRXOVERTHRESH=', ',')
            ADJRXSENSTHRESH = FISH(line, 'ADJRXSENSTHRESH=', ',')
            MINFREQGUARDBAND = FISH(line, 'MINFREQGUARDBAND=', ',')
            MAXFREQGUARDBAND = FISH(line, 'MAXFREQGUARDBAND=', ',')
            s_prf += TID + ',' + LABEL + ',' + AID + ',' + ADJTXBIAS + ',' + ADJTXCURPOW + ',' + ADJTXMAXPOW + ',' + ADJTXMINPOW + ',' + ADJTXMODCLASS + ',' + ADJTXRATE + ',' + TRANSMODE + ',' + CTRLFREQOFFSET + ',' + TXMINSPECTRALWIDTH + ',' + TXSIGBW3DB + ',' + TXSIGBW10DB + ',' + TXFREQRES + ',' + ADJRXNOMINPUT + ',' + ADJRXOVERTHRESH + ',' + ADJRXSENSTHRESH + ',' + MINFREQGUARDBAND + ',' + MAXFREQGUARDBAND + ',' + ',\n'
        elif line.find('AUTOMCPO=') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            l1 = line.rfind(':') + 1
            STATE = line[l1:-2]
            l1 = AID.find('-')
            l2 = AID.rfind('-') + 1
            ShSl = AID[l1:l2]
            CP = ''
            for j in dCPACK.items():
                if ShSl.find(j[0]) > -1:
                    CP = j[1]
                    continue

            l1 = AID.find('-') + 1
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            try:
                label = dOTSinfo_CpShSl[ShSl]
            except:
                label = ',,,'

            line = line.replace(':', ',')
            REFTXRXTYPE = FISH(line, 'REFTXRXTYPE=\\"', '\\"')
            REFBW3DB = FISH(line, 'REFBW3DB=', ',')
            REFBW10DB = FISH(line, 'REFBW10DB=', ',')
            REFSIGPOW = FISH(line, 'REFSIGPOW=', ',')
            AUTOMCPO = FISH(line, 'AUTOMCPO=', ',')
            REFBW = FISH(line, 'REFBW=', ',')
            TYPE = FISH(line, ',TYPE=', ',')
            OCHTXBCTRL = FISH(line, 'OCHTXBCTRL=', ',')
            s_loc += TID + ',' + CP + ',' + label + ',' + AID + ',' + STATE + ',' + REFTXRXTYPE + ',' + REFBW3DB + ',' + REFBW10DB + ',' + REFSIGPOW + ',' + AUTOMCPO + ',' + REFBW + ',' + TYPE + ',' + OCHTXBCTRL + ',\n'
        if s_loc != '':
            F_OUT = open(WindowsHost + '_LOC.csv', 'w')
            F_OUT.write('TID,Circuit Pack,OTS AID,OSID,Tx Path ID,Rx path ID,FEAID,PState,SState,Reference Tx/Rx Type,Reference Signal Bandwidth 3dB (GHz),Reference Signal Bandwidth 10dB (GHz),Reference Signal Power (dBm),Auto Maximum Control Power Output (dBm),Reference Bandwidth,Type,Tx Power Reduction Control,\n' + s_loc)
            F_OUT.close()
        if s_prf != '':
            F_OUT = open(WindowsHost + '_Photonic_Profiles.csv', 'w')
            F_OUT.write('TID,Label,AID\tType,Tx SNR Bias (dB),Actual Tx Power (dBm),Max Launch Power (dBm),Min launch Power (dBm),Modulation Class,Rate (Gbps),Transmission Mode,Control Frequency Offset (GHz),Tx Minimum Spectral Width (GHz),Tx Signal Bandwidth 3dB (GHz),Tx Signal Bandwidth 10dB (GHz),Frequency Resolution(GHz),Rx Nominal Level (dBm),Rx Overload Level (dBm),Rx Sensitivity Level (dBm),Lower Frequency Guard Band (GHz),Upper Frequency Guard Band (GHz)\n' + s_prf)
            F_OUT.close()

    return None


def TRANSLATE_CP_PORTTRAIL(Trail, dCPACK):
    lTrail = Trail.split(',')
    CP_TRAIL = ''
    oldCP = ''
    for i in lTrail:
        l1 = i.rfind('-')
        aid = '-' + i[:l1] + '-'
        try:
            f1 = dCPACK[aid]
            if f1 == oldCP:
                pass
            else:
                oldCP = f1
                CP_TRAIL += f1 + '-' + i[:l1] + ' > '
        except:
            CP_TRAIL += '? > '

    return CP_TRAIL[:-3]


def PARSE_RTRV_SNCG(linesIn, lOTSinfo, TID, WindowsHost, dINFO_4_SNCG__SncId, dOSRP_RMTLINKS__NameId):
    F_ROUTE = open(WindowsHost + '_SNCG_Routes.csv', 'w')
    F_ROUTE.write('TID,SNCG-Shelf-ID,Route Type,DTL Name,DTL Cost,DTL Path,\n')
    F_OUT = open(WindowsHost + '_SNCG.csv', 'w')
    F_OUT.write('TID,Shelf ID,AID,Label,End Point Type,End Point Status,P State,S State,Associated Routing List,Non-Viable Routing List,Exclusive,Lower Frequency Range,Upper Frequency Range,Upper Frequency Filter-Edge Spacing,Lower Frequency Filter-Edge Spacing,Permanent,Protection Class,Reversion,Time to Revert,Backoff Period,Absolute Route Diversity,Using Home,Home Available,Mesh Restorable,Max Home,Circuit ID,SNC List[ AID / Mesh Restorable / State / Local Point / Remote Node / Remote Point / Spectral Assignment / Center Frequency (THz) / Width (GHz) / Wavelength (nm) ]\n')
    SNCG_ROUTE0 = ''
    for line in linesIn:
        if line.find('ROUTETYPE=') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            if SNCG_ROUTE0 != AID:
                if SNCG_ROUTE0 == '':
                    pass
                else:
                    F_ROUTE.write('\n')
                SNCG_ROUTE0 = AID
            ROUTETYPE = FISH(line, 'ROUTETYPE=', ',')
            DTLCOST = FISH(line, 'DTLCOST=', ',')
            DTLNAME = FISH(line, 'DTLNAME=\\"', '\\"')
            DTL = FISH(line, 'DTL=\\"', '\\"')
            DTL = DTL.replace(',', ':')
            toks = DTL.split('&')
            n = len(toks)
            f1 = ''
            for i in range(n):
                l1 = toks[i]
                try:
                    f1 += l1 + ' >> ' + dOSRP_RMTLINKS__NameId[l1] + ' & '
                except:
                    f1 += l1 + ' >> ' + ' ? '

            SncRoute = f1[:-3]
            F_ROUTE.write(TID + ',' + AID + ',' + ROUTETYPE + ',' + DTLNAME + ',' + DTLCOST + ',' + SncRoute + ',\n')
        elif line.find('SNCGEPSTATE=') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            l1 = line.rfind(':') + 1
            STATE = line[l1:-2]
            if STATE.find(',') < 0:
                STATE += ','
            line = line.replace(':', ',')
            l1 = AID.split('-')
            SHELF = 'SHELF-' + l1[1]
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            LABEL = LABEL.replace(',', ';')
            f1 = FISH(line, 'SNCGEPSTATE=', ',')
            l1 = f1.rfind('_')
            SNCGEPSTATE = f1[:l1]
            l1 += 1
            SNCGEPSTATE += ',' + f1[l1:]
            DTLSN = FISH(line, ',DTLSN=', ',')
            if DTLSN.find('NONE') > -1:
                DTLSN = 'None'
            NVDTLSN = FISH(line, 'NVDTLSN=', ',')
            DTLEXCL = FISH(line, 'DTLEXCL=', ',')
            f1 = FISH(line, 'MINFREQ=', ',')
            MINFREQ = f1.rstrip('0')
            f1 = FISH(line, 'MAXFREQ=', ',')
            MAXFREQ = f1.rstrip('0')
            f1 = FISH(line, 'MAXFREQDB=', ',')
            MAXFREQDB = f1.rstrip('0')
            f1 = FISH(line, 'MINFREQDB=', ',')
            MINFREQDB = f1.rstrip('0')
            f1 = FISH(line, ',TYPE=', ',')
            if f1 == 'DYNAMIC':
                TYPE = 'No'
            else:
                TYPE = 'Yes'
            PRTT = FISH(line, 'PRTT=', ',')
            RVRTT = FISH(line, 'RVRTT=', ',')
            TRVRT = FISH(line, 'TRVRT=', ',')
            BCKOP = FISH(line, 'BCKOP=', ',')
            f1 = FISH(line, 'ARD=', ',')
            if f1 == 'ON':
                ARD = 'Yes'
            else:
                ARD = 'No'
            HOMEDTLACT = FISH(line, 'HOMEDTLACT=', ',')
            HOMEDTLAVAIL = FISH(line, 'HOMEDTLAVAIL=', ',')
            f1 = FISH(line, 'MESHRST=', ',')
            if f1 == 'ON':
                MESHRST = 'Yes'
            else:
                MESHRST = 'No'
            MAXADMWEIGHT = FISH(line, 'MAXADMWEIGHT=', ',')
            CKTID = FISH(line, ',CKTID=\\"', '\\"')
            SNCLIST = FISH(line, 'SNCLIST=', ',')
            if SNCLIST.find('&') > 0:
                toks = SNCLIST.split('&')
                f1 = ''
                for i in toks:
                    f1 += dINFO_4_SNCG__SncId[i] + '\n'

                SNCLIST = '"' + f1[:-1] + '"'
            f1 = TID + ',' + SHELF + ',' + AID + ',' + LABEL + ',' + SNCGEPSTATE + ',' + STATE + ',' + DTLSN + ',' + NVDTLSN + ',' + DTLEXCL + ',' + MINFREQ + ',' + MAXFREQ + ',' + MAXFREQDB + ',' + MINFREQDB + ',' + TYPE + ',' + PRTT + ',' + RVRTT + ',' + TRVRT + ',' + BCKOP + ',' + ARD + ',' + HOMEDTLACT + ',' + HOMEDTLAVAIL + ',' + MESHRST + ',' + MAXADMWEIGHT + ',' + CKTID + ',' + SNCLIST
            F_OUT.write(f1 + '\n')

    F_OUT.close()
    F_ROUTE.close()
    return None


def PARSE_RTRV_SNC(linesIn, lOTSinfo, TID, WindowsHost, dINFO_4_SNC__SourceADJ, OSRP_TYPE):
    dOSRP_RMTLINKS__NameId = {}
    dINFO_4_SNCG__SncId = {}
    lSNC_ROUTES__SncId = []
    dSNC_EEDIAG__SncId = {}
    F_ROUTE = open(WindowsHost + '_SNC_Routes.csv', 'w')
    F_ROUTE.write('TID,SNC-Shelf-ID,Route Type,DTL Name,DTL Cost,DTL Path (Node Name : Link ID),\n')
    F_OUT = open(WindowsHost + '_SNC.csv', 'w')
    f1 = 'TID,Shelf ID,SNC-PriShelf-ID,Label,Grouped,Incarnation #,Associated SNCG,End Point Type_Status,Primary State,Secondary State,Center Frequency (THz),Width (GHz),Wavelength(nm),Home,Home Available,Permanent,Routing Profile,Exclusive,Local End Point,Remote Node,Remote End Point,Mesh Restorable,Protection Class,Maximum Admin Weight,Absolute Route Diversity,Regroom Allowed,Priority,Spectral Assignment,Lower Frequency(THz),Upper Frequency(THz),Revertive,Time To Revert(sec),Backoff Period(sec),Circuit ID,In-Service Takeover,Non-Viable DTL,Datapath Fault Alarm Timer(min),End-to-End Diagnostics,'
    if OSRP_TYPE != 'OTN':
        F_OUT.write(f1 + '\n')
    else:
        f1 += 'SNC Name,SNCP Line Type,Peer Name,Peer Node Name,Remote TS,Remote Path  Protection,Originating  Drop Side PS,Terminating Drop Side PS,Originating Network Side PS,Terminating Network Side PSCo-Routed  SNC,Primary  OVPN ID,RHP  Capability,HP Preempt  Capability,Cost Criteria,Max Delay(ms),TCM,Remote TTP MUX,Tributary Port,Day Restoration  Time(hh-mm),Day Restoration  Interval(min),GEP,Max. Protect  Delay(ms),TODR Holdback  Enable,TODR DOW  Profiles,TODR Holdback  Period(hh-mm),Reversion Fail Count,Reversion State,Diversity Type,SNC Mode,ROCF,ROCF Hold Off,ROCF Hold On'
        F_OUT.write(f1 + '\n')
    SNC_ROUTE0 = ''
    for line in linesIn:
        if line.find(' "OSRPRMTLINKS') > -1:
            f1 = FISH(line, 'ORIGNODENAME=\\"', '\\"') + ':' + FISH(line, 'ORIGLINKID=', ',')
            l1 = FISH(line, 'RMTNODENAME=\\"', '\\"') + ':' + FISH(line, 'RMTLINKID=', ',')
            dOSRP_RMTLINKS__NameId[f1] = l1
            dOSRP_RMTLINKS__NameId[l1] = f1
        elif line.find(':ROUTETYPE=') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            lSNC_ROUTES__SncId.append(AID)
            if SNC_ROUTE0 != AID:
                if SNC_ROUTE0 == '':
                    pass
                else:
                    F_ROUTE.write('\n')
                SNC_ROUTE0 = AID
            ROUTETYPE = FISH(line, 'ROUTETYPE=', ',')
            DTLCOST = FISH(line, 'DTLCOST=', ',')
            DTLNAME = FISH(line, 'DTLNAME=\\"', '\\"')
            DTL = FISH(line, 'DTL=\\"', '\\"')
            DTL = DTL.replace(',', ':')
            toks = DTL.split('&')
            n = len(toks)
            f1 = ''
            for i in range(n):
                l1 = toks[i]
                try:
                    f1 += l1 + ' >> ' + dOSRP_RMTLINKS__NameId[l1] + ' & '
                    SncRoute = f1[:-3]
                except:
                    SncRoute = 'na'

            F_ROUTE.write(TID + ',' + AID + ',' + ROUTETYPE + ',' + DTLNAME + ',' + DTLCOST + ',' + SncRoute + ',\n')
        elif line.find('::LINKID') > -1 and line.find(',NODENAME') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            LINKID = FISH(line, 'LINKID=', ',')
            NODENAME = FISH(line, 'NODENAME=\\"', '\\"')
            l1 = NODENAME + ':' + LINKID
            if LINKID != '0':
                try:
                    f1 = dOSRP_RMTLINKS__NameId[l1]
                    l1 = f1.find(':')
                    FE_NODENAME = f1[:l1]
                    l1 += 1
                    FE_LINK = f1[l1:]
                except:
                    FE_LINK = 'na'

            STATUS = FISH(line, 'STATUS=\\"', '\\"')
            if STATUS.find('/') > -1:
                l1 = STATUS.split('/')
                s1 = re.findall('OTS-\\d+-\\d+', l1[0])
                OTS_In = ''.join(s1)
                s1 = l1[0]
                f1 = s1.rfind('[') + 1
                CHS_In = s1[f1:-1]
                s1 = re.findall('OTS-\\d+-\\d+', l1[1])
                OTS_Out = ''.join(s1)
                s1 = l1[1]
                f1 = s1.rfind('[') + 1
                CHS_Out = s1[f1:-1]
                try:
                    dSNC_EEDIAG__SncId[AID] += ' / ' + OTS_In + ' > ' + NODENAME + ' > ' + OTS_Out + ' / ' + LINKID + ' ] >> ' + CHS_Out + ' >> [ ' + FE_LINK
                except:
                    dSNC_EEDIAG__SncId[AID] = ' / ' + OTS_In + ' > ' + NODENAME + ' > ' + OTS_Out + ' / ' + LINKID + ' ] >> ' + CHS_Out + ' >> [ ' + FE_LINK

            elif LINKID == '0':
                s1 = re.findall('OTS-\\d+-\\d+', STATUS)
                OTS_In = ''.join(s1)
                dSNC_EEDIAG__SncId[AID] += ' / ' + OTS_In + ' > ' + NODENAME + ' ] > '
            else:
                s1 = re.findall('OTS-\\d+-\\d+', STATUS)
                OTS_Out = ''.join(s1)
                f1 = STATUS.rfind('[') + 1
                CHS_Out = STATUS[f1:-1]
                dSNC_EEDIAG__SncId[AID] = ' > [ ' + NODENAME + ' > ' + OTS_Out + ' / ' + LINKID + ' ] >> ' + CHS_Out + ' >> [ ' + FE_LINK
        elif line.find('WVLGRID') > -1 or line.find('SNCEPSTATE') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            l1 = line.rfind(':') + 1
            STATE = line[l1:-2]
            line = line.replace(':', ',')
            l1 = AID.split('-')
            SHELF = 'SHELF-' + l1[1]
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            LABEL = LABEL.replace(',', ';')
            GROUPED = FISH(line, 'GROUPED=', ',')
            INCARNATION = FISH(line, 'INCARNATION=', ',')
            ASSOCSNCG = FISH(line, 'ASSOCSNCG=', ',')
            SNCEPSTATE = FISH(line, 'SNCEPSTATE=', ',')
            SNCEPSTATE = SNCEPSTATE.replace('ORIG_', 'ORIGINATING & ')
            SNCEPSTATE = SNCEPSTATE.replace('TERM_', 'TERMINATING & ')
            HOMEDTLACT = FISH(line, 'HOMEDTLACT=', ',')
            HOMEDTLAVAIL = FISH(line, 'HOMEDTLAVAIL=', ',')
            f1 = FISH(line, 'TYPE=', ',')
            if f1 == 'DYNAMIC':
                TYPE = 'No'
            else:
                TYPE = 'Yes'
            DTLSN = FISH(line, 'DTLSN=', ',')
            if DTLSN.find('NONE') > -1:
                DTLSN = 'None'
            DTLEXCL = FISH(line, 'DTLEXCL=', ',')
            LEP = FISH(line, 'LEP=\\"', '\\"')
            RMTNODE = FISH(line, 'RMTNODE=\\"', '\\"')
            RMTEP = FISH(line, 'RMTEP=\\"', '\\"')
            f1 = FISH(line, 'MESHRST=', ',')
            if f1 == 'ON':
                MESHRST = 'Yes'
            else:
                MESHRST = 'No'
            PRTT = FISH(line, 'PRTT=', ',')
            MAXADMWEIGHT = FISH(line, 'MAXADMWEIGHT=', ',')
            f1 = FISH(line, 'ARD=', ',')
            if f1 == 'ON':
                ARD = 'Yes'
            else:
                ARD = 'No'
            f1 = FISH(line, 'REGROOM=', ',')
            if f1 == 'ON':
                REGROOM = 'Yes'
            else:
                REGROOM = 'No'
            PRIORITY = FISH(line, 'PRIORITY=', ',')
            f1 = FISH(line, 'MINFREQ=', ',')
            MINFREQ = f1.rstrip('0')
            f1 = FISH(line, 'MAXFREQ=', ',')
            MAXFREQ = f1.rstrip('0')
            f1 = FISH(line, 'RVRTT=', ',')
            if f1.find('_NO_') > -1:
                RVRTT = 'NO'
            else:
                RVRTT = 'YES'
            TRVRT = FISH(line, 'TRVRT=', ',')
            BCKOP = FISH(line, 'BCKOP=', ',')
            CKTID = FISH(line, ',CKTID=\\"', '\\"')
            MP2CP = FISH(line, 'MP2CP=', ',')
            NVDTLSN = FISH(line, 'NVDTLSN=', ',')
            if NVDTLSN.find('NONE') > -1:
                NVDTLSN = 'None'
            DPFLTALMTIME = FISH(line, 'DPFLTALMTIME=', ',')
            if STATE.find('OOS') < 0:
                if DTLSN == '':
                    SPECTRAL_ASSIGNMENT = 'Explicit'
                else:
                    SPECTRAL_ASSIGNMENT = 'Implicit'
                try:
                    s1 = dINFO_4_SNC__SourceADJ[LEP]
                except:
                    f1 = FISH(line, 'FREQUENCY=', ',')
                    if f1 == '0.000000':
                        s1 = ',,'
                    else:
                        s1 = f1.rstrip('0') + ',,'

            else:
                s1 = ',,'
                SPECTRAL_ASSIGNMENT = ''
            if STATE[-1] == ',':
                f1 = STATE[:-2]
            else:
                f1 = STATE.replace(',', '&')
            dINFO_4_SNCG__SncId[AID] = AID + ' / ' + LABEL + ' / ' + MESHRST + ' / ' + f1 + ' / ' + LEP + ' / ' + RMTNODE + ' / ' + RMTEP + ' / ' + SPECTRAL_ASSIGNMENT + ' / ' + s1.replace(',', ' / ')
            if line.find(',ROCFHOLDON=') > -1:
                NAME = FISH(line, 'NAME=\\"', '\\"')
                SNCLINETYPE = FISH(line, 'SNCLINETYPE=', ',')
                PEERSNC = FISH(line, 'PEERSNC=\\"', '\\"')
                PEERORIGIN = FISH(line, 'PEERORIGIN=', ',')
                RMTTIMESLOT = FISH(line, ',RMTTIMESLOT=', ',')
                RMTPATHPROTECTION = FISH(line, ',RMTPATHPROTECTION=', ',')
                ORIGINDSPS = FISH(line, ',ORIGINDSPS=', ',')
                TERMINDSPS = FISH(line, ',TERMINDSPS=', ',')
                ORIGINNSPS = FISH(line, ',ORIGINNSPS=', ',')
                TERMINNSPS = FISH(line, ',TERMINNSPS=', ',')
                CRSNC = FISH(line, 'CRSNC=', ',')
                PRIMARYOVPNIDS = FISH(line, 'PRIMARYOVPNIDS=', ',')
                RHPCAPABILITY = FISH(line, 'RHPCAPABILITY=', ',')
                HPPREEMPT = FISH(line, 'HPPREEMPT=', ',')
                COSTCRITERIA = FISH(line, ',COSTCRITERIA=', ',')
                MAXDELAY = FISH(line, ',MAXDELAY=', ',')
                TCM = FISH(line, 'TCM=', ',')
                RMTTTPMUX = FISH(line, ',RMTTTPMUX=', ',')
                TRIBPORT = FISH(line, ',TRIBPORT=', ',')
                TODRTIME = FISH(line, ',TODRTIME=', ',')
                TODRPERIOD = FISH(line, ',TODRPERIOD=', ',')
                GEP = FISH(line, ',GEP=', ',')
                PROTMAXDELAY = FISH(line, ',PROTMAXDELAY=', ',')
                TODRHBEN = FISH(line, ',TODRHBEN=', ',')
                TODRPROFLST = FISH(line, ',TODRPROFLST=', ',')
                TODRHBPRD = FISH(line, ',TODRHBPRD=', ',')
                REVFAILCNT = FISH(line, ',REVFAILCNT=', ',')
                RVRTSTATE = FISH(line, ',RVRTSTATE=', ',')
                PROTDIVTYPE = FISH(line, ',PROTDIVTYPE=', ',')
                MODE = FISH(line, ',MODE=', ',')
                ROCF = FISH(line, ',ROCF=', ',')
                ROCFHOLDOFF = FISH(line, ',ROCFHOLDOFF=', ',')
                ROCFHOLDON = FISH(line, ',ROCFHOLDON=', ',')
                f2 = NAME + ',' + SNCLINETYPE + ',' + PEERSNC + ',' + PEERORIGIN + ',' + RMTTIMESLOT + ',' + RMTPATHPROTECTION + ',' + ORIGINDSPS + ',' + TERMINDSPS + ',' + ORIGINNSPS + ',' + TERMINNSPS
                f2 += CRSNC + ',' + PRIMARYOVPNIDS + ',' + RHPCAPABILITY + ',' + HPPREEMPT + ',' + COSTCRITERIA + ',' + MAXDELAY + ',' + TCM + ',' + RMTTTPMUX + ',' + TRIBPORT + ',' + TODRTIME + ',' + TODRPERIOD + ',' + GEP + ',' + PROTMAXDELAY + ',' + TODRHBEN + ',' + TODRPROFLST + ',' + TODRHBPRD + ',' + REVFAILCNT + ',' + RVRTSTATE + ',' + PROTDIVTYPE + ',' + MODE + ',' + ROCF + ',' + ROCFHOLDOFF + ',' + ROCFHOLDON + ',\n'
            else:
                f2 = ',\n'
            try:
                l1 = LEP + dSNC_EEDIAG__SncId[AID] + RMTEP
                l1 = l1.replace('ADJ-', 'CMD-')
            except:
                l1 = ''

            f1 = TID + ',' + SHELF + ',' + AID + ',' + LABEL + ',' + GROUPED + ',' + INCARNATION + ',' + ASSOCSNCG + ',' + SNCEPSTATE + ',' + STATE + ',' + s1 + ',' + HOMEDTLACT + ',' + HOMEDTLAVAIL + ',' + TYPE + ',' + DTLSN + ',' + DTLEXCL + ',' + LEP + ',' + RMTNODE + ',' + RMTEP + ',' + MESHRST + ',' + PRTT + ',' + MAXADMWEIGHT + ',' + ARD + ',' + REGROOM + ',' + PRIORITY + ',' + SPECTRAL_ASSIGNMENT + ',' + MINFREQ + ',' + MAXFREQ + ',' + RVRTT + ',' + TRVRT + ',' + BCKOP + ',' + CKTID + ',' + MP2CP + ',' + NVDTLSN + ',' + DPFLTALMTIME + ',' + l1 + ',' + f2
            F_OUT.write(f1)
        elif line.find('  "OSRPRMTLINK') > -1:
            pass
        elif line.find('ROUTETYPE=') > -1:
            pass
        elif line.find('LINKID=') > -1:
            pass

    F_OUT.close()
    F_ROUTE.close()
    return (dINFO_4_SNCG__SncId, dOSRP_RMTLINKS__NameId)


def PARSE_RTRV_PM_SDMON(linesIn, dMEMBERS, dCPACK, TID, F_NOW):
    f1 = 'TID,SHELF ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit Pack,Frequency (THz),Bin Width (GHz),Untimed OPT-OTS (dBm),\n'
    F_NOW.write(f1)
    for line in linesIn:
        if line.find('SDMON:OPT-OTS') > -1:
            tokens = line.split(',')
            f1 = tokens[0]
            AID = f1[4:]
            Power = tokens[2]
            WIDTH = '12.5'
            tokens = AID.split('-')
            f1 = tokens[4]
            FREQ = str(float(f1) / 1000000)
            SHELF = 'SHELF-' + tokens[1]
            _ShSl_ = '-' + tokens[1] + '-' + tokens[2] + '-'
            CP = ''
            try:
                CP = dCPACK[_ShSl_]
            except:
                CP = ''

            s1 = TID + ',' + SHELF + ',-,-,-,-,-,' + AID + ',' + CP
            for j in dMEMBERS.items():
                if j[1].find(_ShSl_) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID + ',' + CP
                    continue

            F_NOW.write(s1 + ',' + FREQ + ',' + WIDTH + ',' + Power + ',\n')

    return None


def PARSE_RTRV_PM_NMCMON(linesIn, dMEMBERS, dCPACK, TID, F_NOW):
    f1 = 'TID,SHELF ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit Pack,Frequency (THz),Channel Width (GHz),Wavelength (nm),Untimed OPT-OCH (dBm),Baseline OPT-OCH (dBm),Beaseline Reset (M-D:H-M),\n'
    F_NOW.write(f1)
    dUNTIMED__AID = {}
    for line in linesIn:
        if line.find(',1-UNT,') > -1:
            tokens = line.split(',')
            f1 = tokens[0]
            AID = f1[4:]
            Power = tokens[2]
            WIDTH = tokens[14]
            f1 = tokens[15].replace('"', '')
            if f1[:2] == '15':
                WAVE = str(float(f1) / 100.0)
            else:
                WAVE = ''
            tokens = AID.split('-')
            f1 = tokens[4]
            FREQ = str(float(f1) / 1000000)
            SHELF = 'SHELF-' + tokens[1]
            _ShSl_ = '-' + tokens[1] + '-' + tokens[2] + '-'
            CP = ''
            try:
                CP = dCPACK[_ShSl_]
            except:
                CP = ''

            s1 = TID + ',' + SHELF + ',-,-,-,-,-,' + AID + ',' + CP
            for j in dMEMBERS.items():
                if j[1].find(_ShSl_) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID + ',' + CP
                    continue

            dUNTIMED__AID[AID] = s1 + ',' + FREQ + ',' + WIDTH + ',' + WAVE + ',' + Power
        elif line.find(',BASLN,') > -1:
            tokens = line.split(',')
            f1 = tokens[0]
            AID = f1[4:]
            Power = tokens[2]
            M_D = tokens[7]
            H_M = tokens[8]
            f1 = dUNTIMED__AID[AID]
            F_NOW.write(f1 + ',' + Power + ',' + M_D + ':' + H_M + ',\n')

    return None


def PARSE_RTRV_PM_CHMON(linesIn, dMEMBERS, dCPACK, TID, F_NOW):
    f1 = 'TID,SHELF ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit Pack,Wavelength,Channel ID,OCH Status,Untimed OPT-OCH (dBm),Baseline OPT-OCH (dBm),Beaseline Reset (M-D:H-M),\n'
    F_NOW.write(f1)
    dUNTIMED__AID = {}
    for line in linesIn:
        if line.find(',1-UNT,') > -1:
            tokens = line.split(',')
            f1 = tokens[0]
            AID = f1[4:]
            Power = tokens[2]
            WaveId = tokens[11]
            Status = tokens[12]
            tokens = AID.split('-')
            WAVE = tokens[4]
            SHELF = 'SHELF-' + tokens[1]
            _ShSl_ = '-' + tokens[1] + '-' + tokens[2] + '-'
            CP = ''
            try:
                CP = dCPACK[_ShSl_]
            except:
                CP = ''

            s1 = TID + ',' + SHELF + ',-,-,-,-,-,' + AID + ',' + CP
            for j in dMEMBERS.items():
                if j[1].find(_ShSl_) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID + ',' + CP
                    continue

            dUNTIMED__AID[AID] = s1 + ',' + WAVE + ',' + WaveId + ',' + Status + ',' + Power
        elif line.find(',BASLN,') > -1:
            tokens = line.split(',')
            f1 = tokens[0]
            AID = f1[4:]
            Power = tokens[2]
            M_D = tokens[7]
            H_M = tokens[8]
            f1 = dUNTIMED__AID[AID]
            F_NOW.write(f1 + ',' + Power + ',' + M_D + ':' + H_M + ',\n')

    return None


def PARSE_ENCRYPT(linesIn, TID, F_Out):
    dSHELF_ENCRYPT__ID = {}
    f1 = 'TID,SHELF,MODE,Port,AID,IP Address,PreFix,Gateway,\n'
    F_Out.write(f1)
    for line in linesIn:
        if line.find(' "SHELF-') > -1:
            l1 = line.find('::')
            SHELF = line[10:l1]
            line = line[:-2] + ','
            LANPORT = FISH(line, 'LANPORT=', ',')
            MODE = FISH(line, 'MODE=', ',')
            if MODE == 'SEG':
                MODE += ' (L2 Solution access)'
            else:
                MODE += ' (L3 Solution access)'
            dSHELF_ENCRYPT__ID[SHELF] = MODE + ',' + LANPORT
        elif line.find('GATEWAY') > -1:
            l1 = line.find('::')
            AID = line[4:l1]
            l1 = AID.find('-') + 1
            l2 = AID.rfind('-')
            SHELF = AID[l1:l2]
            line = line[:-2] + ','
            IPADDR = FISH(line, 'IPADDR=', ',')
            GATEWAY = FISH(line, 'GATEWAY=', ',')
            PREFIX = FISH(line, 'PREFIX=', ',')
            F_Out.write(TID + ',SHELF-' + SHELF + ',' + dSHELF_ENCRYPT__ID[SHELF] + ',' + AID + ',' + IPADDR + ',' + PREFIX + ',' + GATEWAY + ',\n')

    return None


def PARSE_RTRV_IPxRTG_TBL(linesIn, TID, F_Out):
    f1 = 'TID,SHELF,IP Subnet,Subnet Mask,Next Hop,Cost,Forward Metric,Circuit ID,Carrier,Owner,Tunnel Termination,Prefix,\n'
    F_Out.write(f1)
    for line in linesIn:
        if line.find('Begin: RTRV-IP6RTG-TBL') > -1:
            F_Out.write('\n')
        if len(line) < 70:
            continue
        l1 = line.find('::')
        SHELF = line[4:l1]
        if line.find('IPADDR') > -1:
            IPADDR = FISH(line, 'IPADDR=', ',')
            NETMASK = FISH(line, 'NETMASK=', ',')
            NEXTHOP = FISH(line, 'NEXTHOP=', ',')
            CARRIER = FISH(line, 'CARRIER=', '"')
            TUNNEL = FISH(line, 'TUNNEL=', ',')
            OWNER = FISH(line, 'OWNER=', ',')
            PREFIX = ''
        elif line.find('IP6ADDR') > -1:
            IPADDR = FISH(line, 'IP6ADDR=\\"', '\\"')
            NEXTHOP = FISH(line, 'NEXTHOP=\\"', '\\"')
            PREFIX = FISH(line, 'PREFIX=', ',')
            OWNER = FISH(line, 'OWNER=', '"')
            NETMASK = ''
            TUNNEL = ''
            CARRIER = ''
        COST = FISH(line, 'COST=', ',')
        EXTCOST = FISH(line, 'EXTCOST=', ',')
        CIRCUIT = FISH(line, 'CIRCUIT=', ',')
        F_Out.write(TID + ',' + SHELF + ',' + IPADDR + ',' + NETMASK + ',' + NEXTHOP + ',' + COST + ',' + EXTCOST + ',' + CIRCUIT + ',' + CARRIER + ',' + OWNER + ',' + TUNNEL + ',' + PREFIX + '\n')

    return None


def PARSE_RTRV_ODUCTP(linesIn, TID, F_NOW):
    dRATE__AID = {}
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('Label')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point ')
    lTL1.append(' RATE')
    lPoint.append('Basic rate')
    lTL1.append(' NUMTS')
    lPoint.append('Number of trib slots')
    lTL1.append(' CONDTYPE')
    lPoint.append('OSRP Line TCM level')
    lTL1.append(' OWNER')
    lPoint.append('Owner')
    lTL1.append(' DMCOUNT')
    lPoint.append('One Way Latency (micro-sec)')
    lTL1.append(' DMENABLE')
    lPoint.append('Delay Measurement')
    lTL1.append(' EXPT')
    lPoint.append('Expected Payload Type')
    lTL1.append(' RXPT')
    lPoint.append('Received Payload Type')
    lTL1.append(' CTPMODE')
    lPoint.append('CTP Mode')
    lTL1.append(' GEP')
    lPoint.append('Generic End Point')
    lTL1.append(' GEPNAME')
    lPoint.append('Generic End Point Name')
    lTL1.append(' SDTH')
    lPoint.append('Signal degrade threshold')
    lTL1.append(' SFTH')
    lPoint.append('Signal failed threshold')
    lTL1.append(' BASEHO')
    lPoint.append('Base HO')
    lTL1.append(' BITRATE')
    lPoint.append('Bit Rate (bps)')
    lTL1.append(' CLIENTTYPE')
    lPoint.append('Client Type')
    lTL1.append(' FLEXTYPE')
    lPoint.append('ODUFlex Type')
    lTL1.append(' RESIZEABLE')
    lPoint.append('Resizeable')
    lTL1.append(' TOLERANCE')
    lPoint.append('Tolerance (ppm)')
    lTL1.append(' TXOPER')
    lPoint.append('Trail Trace Transmitted')
    lTL1.append(' EXOPER')
    lPoint.append('Trail Trace Expected')
    lTL1.append(' RXOPER')
    lPoint.append('Trail Trace Received')
    lTL1.append(' TFMODE')
    lPoint.append('Trace Fail Mode')
    i_15MIN = 29
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN CV-ODU')
    lPoint.append('15 Minute Code Violations ')
    lTL1.append(' 15-MIN ES-ODU')
    lPoint.append('15 Minute Errored Seconds ')
    lTL1.append(' 15-MIN SES-ODU')
    lPoint.append('15 Minute Severely Errored Seconds ')
    lTL1.append(' 15-MIN UAS-ODU')
    lPoint.append('15 Minute Unavailable Seconds')
    lTL1.append(' 15-MIN FC-ODU')
    lPoint.append('15 Minute Fault Corrections')
    lTL1.append(' 15-MIN DMAVG-ODU')
    lPoint.append('15 Minute Average Delay (microseconds)')
    i_UNTIMED = 35
    lTL1.append(' 1_UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT CV-ODU')
    lPoint.append('Untimed Code Violations  ')
    lTL1.append(' 1-UNT ES-ODU')
    lPoint.append('Untimed Errored Seconds ')
    lTL1.append(' 1-UNT SES-ODU')
    lPoint.append('Untimed Severely Errored Seconds ')
    lTL1.append(' 1-UNT UAS-ODU')
    lPoint.append('Untimed Unavailable Seconds')
    lTL1.append(' 1-UNT FC-ODU')
    lPoint.append('Untimed Fault Corrections')
    lTL1.append(' 1-UNT DMAVG-ODU')
    lPoint.append('Untimed Average Delay (microseconds)')
    ARRAY_ALL = {}
    lAID = []
    AID = '?'
    for line in linesIn:
        if line.find(' "ODUCTP-') > -1 and line.find('LABEL=') > -1:
            f1 = line.split(':')
            l1 = f1[0]
            AID = l1.replace('   "', '')
            l2 = f1[-1].strip('\r\n"')
            states = l2.replace(',', ' & ')
            rest = f1[2]
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'SUPPTP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(rest, 'RATE=', ',')
            dRATE__AID[AID] = f1
            ARRAY_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'NUMTS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'CONDTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'OWNER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'DMCOUNT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'DMENABLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'EXPT=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'RXPT=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'CTPMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'GEP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'GEPNAME=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = '1E-0' + FISH(rest, 'SDTH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'SFTH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'BASEHO=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'BITRATE=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'CLIENTTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'FLEXTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'RESIZEABLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'TOLERANCE=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'TXOPER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'EXOPER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(rest, 'RXOPER=\\"', '\\"')
            try:
                f3 = f1.encode('ascii', 'replace')
            except:
                f1 = 'String has unknown Encoding'

            ARRAY_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(rest, 'TFMODE=', ',')
        elif line.find('15-MIN') > -1:
            if line.find('ODUCTP:CV-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:ES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:SES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:UAS-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:FC-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:DMAVG-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        elif line.find('1-UNT') > -1:
            if line.find('ODUCTP:CV-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[7] + ' : ' + f1[8]
                idx = i_UNTIMED + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:ES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:SES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:UAS-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:FC-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUCTP:DMAVG-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 6
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return dRATE__AID


def PARSE_RTRV_TCMTTP(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('Label')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point ')
    lTL1.append(' DMENABLE')
    lPoint.append('Delay Measurement')
    lTL1.append(' DMCOUNT')
    lPoint.append('One Way Latency (microsec)')
    lTL1.append(' OWNER')
    lPoint.append('Owner')
    lTL1.append(' TCMMODE')
    lPoint.append('TCM Mode')
    lTL1.append(' SDTH')
    lPoint.append('Signal degrade threshold')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    i_15MIN = 11
    lTL1.append(' 15-MIN CV-TCM')
    lPoint.append('Current 15 Minute code violations ')
    lTL1.append(' 15-MIN ES-TCM')
    lPoint.append('Current 15 Minute Errored Seconds ')
    lTL1.append(' 15-MIN SES-TCM')
    lPoint.append('Current 15 Minute Severely Errored Seconds ')
    lTL1.append(' 15-MIN FC-TCM')
    lPoint.append('Current 15 Minute Failure Counts')
    lTL1.append(' 15-MIN UAS-TCM')
    lPoint.append('Current 15 Minute Unavailablr Seconds')
    lTL1.append(' 15-MIN IAE-TCM')
    lPoint.append('Current 15 Minute Imcoming Alignment Errors')
    lTL1.append(' 15-MIN DMAVG-TCM')
    lPoint.append('Current 15 Minute Average Delay Measurement (micro-sec)')
    i_UNTIMED = i_15MIN + 7
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT CV-TCM')
    lPoint.append('Untimed code violations ')
    lTL1.append(' 1-UNT ES-TCM')
    lPoint.append('Untimed Errored Seconds ')
    lTL1.append(' 1-UNT SES-TCM')
    lPoint.append('Untimed Severely Errored Seconds ')
    lTL1.append(' 1-UNT FC-TCM')
    lPoint.append('Untimed Failure Counts')
    lTL1.append(' 1-UNT UAS-TCM')
    lPoint.append('Untimed Unavailablr Seconds')
    lTL1.append(' 1-UNT IAE-TCM')
    lPoint.append('Untimed Imcoming Alignment Errors')
    lTL1.append(' 1-UNT DMAVG-TCM')
    lPoint.append('Untimed Average Delay Measurement (micro-sec)')
    ARRAY_ALL = {}
    lAID = []
    AID = '?'
    for line in linesIn:
        if line.find(' "TCMTTP-') > -1 and line.find('LABEL=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SUPPTP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'DMENABLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'DMCOUNT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OWNER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'TCMMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SDTH=', ':')
        elif line.find('15-MIN') > -1:
            if line.find('TCM:CV-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:ES-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:SES-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:FC-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:UAS-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:IAE-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:DMAVG-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15MIN + 6
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        elif line.find('1-UNT') > -1:
            if line.find('TCM:CV-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:ES-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
                idx = i_UNTIMED
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[7] + ' : ' + f1[8]
            if line.find('TCM:SES-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:FC-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:UAS-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:IAE-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 6
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('TCM:DMAVG-TCM') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 7
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_ODUTTP(linesIn, TID, dCLFI_SSP, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('Label')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point')
    lTL1.append(' RATE')
    lPoint.append('Basic rate')
    lTL1.append(' OWNER')
    lPoint.append('Owner')
    lTL1.append(' ODU1INTEROP')
    lPoint.append('ODU1 Interop')
    lTL1.append(' EXPT')
    lPoint.append('Expected Payload Type')
    lTL1.append(' RXPT')
    lPoint.append('Received Payload Type')
    lTL1.append(' NUMTS')
    lPoint.append('Number of trib slots')
    lTL1.append(' LPROTGRP')
    lPoint.append('Protection Group')
    lTL1.append(' LPROTTYPE')
    lPoint.append('Protection Type')
    lTL1.append(' LPROTROLE')
    lPoint.append('Protection Role')
    lTL1.append(' TSSIZE')
    lPoint.append('Tributary Slot Size')
    lTL1.append(' BASEHO')
    lPoint.append('Base HO')
    lTL1.append(' FLEXTYPE')
    lPoint.append('ODUFlex Type')
    lTL1.append(' RESIZEABLE')
    lPoint.append('Resizeable')
    lTL1.append(' CLIENTTYPE')
    lPoint.append('Client Type')
    lTL1.append(' BITRATE ')
    lPoint.append('Bit Rate (bps)')
    lTL1.append(' ANCHORTS')
    lPoint.append('Anchor of tx  tributary slot ')
    lTL1.append(' LCLSUPCACID')
    lPoint.append('Local Supporting CAC Line ID')
    lTL1.append(' RSRVD')
    lPoint.append('Packet SNC Reserved')
    lTL1.append(' RSRVDOPER')
    lPoint.append('Packet SNC Reserved Operational')
    lTL1.append(' SDTH')
    lPoint.append('Signal degrade threshold')
    lTL1.append(' SFTH')
    lPoint.append('Signal failed threshold')
    i_CLFI = 25
    lTL1.append(' CLFI')
    lPoint.append('TXRX ADJ CLFI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    i_15_MIN = 28
    lTL1.append(' 15-MIN CV-ODU')
    lPoint.append('15 Minute code violations')
    lTL1.append(' 15-MIN ES-ODU')
    lPoint.append('15 Minute Errored Seconds')
    lTL1.append(' 15-MIN SES-ODU')
    lPoint.append('15 Minute Severely Errored Seconds ')
    lTL1.append(' 15-MIN UAS-ODU')
    lPoint.append('15 Minute Unavailable Seconds ')
    lTL1.append(' 15-MIN FC-ODU')
    lPoint.append('15 Minute FEC Corrections')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    i_UNTIMED = i_15_MIN + 5
    lTL1.append(' 1-UNT CV-ODU')
    lPoint.append('Untimed code violations')
    lTL1.append(' 1-UNT ES-ODU')
    lPoint.append('Untimed Errored Seconds')
    lTL1.append(' 1-UNT SES-ODU')
    lPoint.append('Untimed Severely Errored Seconds ')
    lTL1.append(' 1-UNT UAS-ODU')
    lPoint.append('Untimed Unavailable Seconds ')
    lTL1.append(' 1-UNT FC-ODU')
    lPoint.append('Untimed FEC Corrections')
    ARRAY_ALL = {}
    lAID = []
    AID = '?'
    for line in linesIn:
        if line.find(' "ODUTTP-') > -1 and line.find('LABEL=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2].strip('\r\n"')
            states = states.replace(',', ' & ')
            for i in dCLFI_SSP:
                f1 = dCLFI_SSP[i]
                clfiAID = i.replace('ADJ', 'ODUTTP')
                if AID.find(clfiAID) > -1:
                    location = AID + '@' + str(i_CLFI)
                    ARRAY_ALL[location] = dCLFI_SSP[i]

            line = line.replace(':', ',')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SUPPTP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OWNER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'ODU1INTEROP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'EXPT=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'RXPT=\\"', '\\"')
            ARRAY_ALL[location] = FISH(line, 'RXPT=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'NUMTS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LPROTGRP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LPROTTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LPROTROLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'TSSIZE=', ',')
            ARRAY_ALL[location] = FISH(line, 'TSSIZE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'BASEHO=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'FLEXTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RESIZEABLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'CLIENTTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'BITRATE=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'ANCHORTS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LCLSUPCACID=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RSRVD=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RSRVDOPER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SDTH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SFTH=', ',')
        elif line.find('15-MIN') > -1:
            if line.find('ODUTTP:CV-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:ES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:SES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:UAS-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:FC-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        elif line.find('1-UNT') > -1:
            if line.find('ODUTTP:CV-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:ES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
                idx = i_UNTIMED
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[7] + ' : ' + f1[8]
            if line.find('ODUTTP:SES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:SES-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ODUTTP:FC-ODU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_ETTP(linesIn, TID, dCLFI_SSP, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('Label')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point ')
    lTL1.append(' RATE')
    lPoint.append('Basic rate')
    lTL1.append(' CONDTYPE')
    lPoint.append('Conditioning type')
    lTL1.append(' MODE')
    lPoint.append('Port Conditioning')
    lTL1.append(' FLOWCTRL')
    lPoint.append('Advertised flow control')
    lTL1.append(' MAPPING')
    lPoint.append('Packet mapping')
    lTL1.append(' HOLDOFF')
    lPoint.append('Holdoff timer (ms)')
    lTL1.append(' MTU')
    lPoint.append('Maximum ethernet frame size')
    lTL1.append(' LCLSUPCACID')
    lPoint.append('Local Supporting CAC Line ID')
    lTL1.append(' SERVICERATE')
    lPoint.append('Service Rate (Mbps)')
    lTL1.append(' TRANSPORTRATE')
    lPoint.append('Transport Rate(Mbps)')
    lTL1.append(' RSRVD')
    lPoint.append('Packet SNC Reserved')
    lTL1.append(' RSRVDOPER')
    lPoint.append('Packet SNC Reserved Operational')
    lTL1.append(' L2INUSEACT')
    lPoint.append('L2 Active Config')
    lTL1.append(' RXOPER')
    lPoint.append('L2 Saved Config')
    lTL1.append(' L2INUSESAV')
    lPoint.append('L2 Saved Config')
    lTL1.append(' IFTYPE')
    lPoint.append('Interface Type')
    i_CLFI = 20
    lTL1.append(' CLFI')
    lPoint.append('TXRX ADJ CLFI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    i_15_MIN = 23
    lTL1.append(' 15-MIN CV-PCS')
    lPoint.append('15 Minute code violations ')
    lTL1.append(' 15-MIN ES-PCS')
    lPoint.append('15 Minute Errored Seconds ')
    lTL1.append(' 15-MIN SES-PCS')
    lPoint.append('15 Minute Severely Errored Seconds ')
    lTL1.append(' 15-MIN UAS-PCS')
    lPoint.append('15 Minute Unavailable Seconds ')
    lTL1.append(' 15-MIN ES-E')
    lPoint.append('15 Minute ETH Errored Seconds')
    lTL1.append(' 15-MIN SES-E')
    lPoint.append('15 Minute ETH Severely Errored Seconds ')
    lTL1.append(' 15-MIN UAS-E')
    lPoint.append('15 Minute ETH Unavailable Seconds ')
    lTL1.append(' 15-MIN INFRAMES-E')
    lPoint.append('15 Minute Total ETH frames received')
    lTL1.append(' 15-MIN INFRAMESERR-E')
    lPoint.append('15 Minute Total ETH frames received with errors')
    lTL1.append(' 15-MIN INFRAMESDISCDS-E')
    lPoint.append('15 Minute Ingress frames discarded due to congestion or policing')
    lTL1.append(' 15-MIN DFR-E')
    lPoint.append('15 Minute Total frames discarded for any reason other than FCS errors (Rx and Tx)')
    lTL1.append(' 15-MIN OUTFRAMES-E')
    lPoint.append('15 Minute Total ETH frames transmitted')
    lTL1.append(' 15-MIN OUTFRAMESERR-E')
    lPoint.append('15 Minute Total egress direction ETH frames transmitted with FCS errors')
    lTL1.append(' 15-MIN OUTFRAMESDISCDS-E')
    lPoint.append('15 Minute Egress frames discarded due to congestion or policing')
    lTL1.append(' 15-MIN FCSERR-E')
    lPoint.append('15 Minute Frame Check Sequence Errors')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    i_UNTIMED = i_15_MIN + 15
    lTL1.append(' 1-UNT CV-PCS')
    lPoint.append('Untimed code violations ')
    lTL1.append(' 1-UNT ES-PCS')
    lPoint.append('Untimed Errored Seconds ')
    lTL1.append(' 1-UNT SES-PCS')
    lPoint.append('Untimed Severely Errored Seconds ')
    lTL1.append(' 1-UNT UAS-OTU')
    lPoint.append('Untimed Unavailable Seconds ')
    lTL1.append(' 1-UNT ES-E')
    lPoint.append('Untimed ETH Errored Seconds')
    lTL1.append(' 1-UNT SES-E')
    lPoint.append('Untimed ETH Severely Errored Seconds ')
    lTL1.append(' 1-UNT UAS-E')
    lPoint.append('Untimed ETH Unavailable Seconds ')
    lTL1.append(' 1-UNT INFRAMES-E')
    lPoint.append('UntimedTotal ETH frames received')
    lTL1.append(' 1-UNT INFRAMESERR-E')
    lPoint.append('Untimed Total ETH frames received with errors')
    lTL1.append(' 1-UNT INFRAMESDISCDS-E')
    lPoint.append('Untimed Ingress frames discarded due to congestion or policing')
    lTL1.append(' 1-UNT DFR-E')
    lPoint.append('Untimed Total frames discarded for any reason other than FCS errors (Rx and Tx)')
    lTL1.append(' 1-UNT OUTFRAMES-E')
    lPoint.append('Untimed Total ETH frames transmitted')
    lTL1.append(' 1-UNT OUTFRAMESERR-E')
    lPoint.append('Untimed Total egress direction ETH frames transmitted with FCS errors')
    lTL1.append(' 1-UNT OUTFRAMESDISCDS-E')
    lPoint.append('Untimed Egress frames discarded due to congestion or policing')
    lTL1.append(' 1-UNT FCSERR-E')
    lPoint.append('Untimed Frame Check Sequence Errors')
    ARRAY_ALL = {}
    for i in dCLFI_SSP:
        f1 = dCLFI_SSP[i]
        AID = i.replace('ADJ', 'ETTP')
        location = AID + '@' + str(i_CLFI)
        ARRAY_ALL[location] = dCLFI_SSP[i]

    lAID = []
    AID = '?'
    for line in linesIn:
        if line.find(' "ETTP-') > -1 and line.find('LABEL=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            line = line.replace(':', ',')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SUPPTPL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'CONDTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'MODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'FLOWCTRL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'MAPPING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'HOLDOFF=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'MTU=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LCLSUPCACID=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SERVICERATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'TRANSPORTRATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RSRVD=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RSRVDOPER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'L2INUSEACT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'L2INUSESAV=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'IFTYPE=', ',')
        elif line.find('15-MIN') > -1:
            if line.find('ETTP:CV-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:ES-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:SES-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:UAS-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:ES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:SES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:UAS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 6
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 7
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMESERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 8
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMESDISCDS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 9
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:DFR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 10
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 11
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMESERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 12
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMESDISCDS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 13
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:FCSERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_15_MIN + 14
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        elif line.find('1-UNT') > -1:
            if line.find('ETTP:CV-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 1
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:ES-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 2
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
                idx = i_UNTIMED
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[7] + ' : ' + f1[8]
            if line.find('ETTP:SES-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 3
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:UAS-PCS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 4
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:ES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 5
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:SES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 6
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:UAS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 7
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 8
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMESERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 9
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:INFRAMESDISCDS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 10
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:DFR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 11
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMES-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 12
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMESERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 13
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:OUTFRAMESDISCDS-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 14
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('ETTP:FCSERR-E') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = i_UNTIMED + 15
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_OTUTTP(linesIn, TID, dGCC0, dGCC1, dCLFI_SSP, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('Label')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point ')
    lTL1.append(' RATE')
    lPoint.append('Basic rate')
    lTL1.append(' NUMTS')
    lPoint.append('Number of trib slots')
    lTL1.append(' OSRPCHANNEL')
    lPoint.append('OSRP Comms channel')
    lTL1.append(' OSRPTCMLEVEL')
    lPoint.append('OSRP Line TCM level')
    lTL1.append(' PREFECSDTHBER')
    lPoint.append('Pre-FEC signal degrade threshold (BER)')
    lTL1.append(' PREFECSDTHLEV')
    lPoint.append('Pre-FEC signal degrade threshold (dBQ)')
    lTL1.append(' PREFECSFTHBER')
    lPoint.append('Pre-FEC signal fail threshold (BER)')
    lTL1.append(' PREFECSFTHLEV')
    lPoint.append('Pre-FEC signal fail threshold (dBQ)')
    lTL1.append(' RXFECFRMT')
    lPoint.append('Rx FEC Format')
    lTL1.append(' TXFECFRMT')
    lPoint.append('Tx FEC Format')
    lTL1.append(' OSRP')
    lPoint.append('OSRP')
    lTL1.append(' LCLSUPCACID')
    lPoint.append('Local Supporting CAC Line ID')
    lTL1.append(' TXOPER')
    lPoint.append('Trail Trace actual Received')
    lTL1.append(' RXOPER')
    lPoint.append('Trail Trace Transmitted')
    lTL1.append(' TFMODE')
    lPoint.append('Trace Fail Mode')
    i_CLFI = 19
    lTL1.append(' CLFI')
    lPoint.append('TXRX ADJ CLFI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN CV-OTU')
    lPoint.append('15 Minute code violations ')
    lTL1.append(' 15-MIN ES-OTU')
    lPoint.append('15 Minute Errored Seconds ')
    lTL1.append(' 15-MIN SES-OTU')
    lPoint.append('15 Minute Severely Errored Seconds ')
    lTL1.append(' 15-MIN SEFS-OTU')
    lPoint.append('15 Minute Severely Errored Frame Seconds ')
    lTL1.append(' 15-MIN FEC-OTU')
    lPoint.append('15 Minute Forward Error Corrections')
    lTL1.append(' 15-MIN HCCS-OTU')
    lPoint.append('15 Minute High Correction Count Seconds')
    lTL1.append(' 15-MIN PFBERE-OTU')
    lPoint.append('15 Minute Post-FEC Bit Error Rate Estimate')
    lTL1.append(' 15-MIN PRFBER-OUT')
    lPoint.append('15 Minute Pre-FEC Bit Error Rate')
    lTL1.append(' 15-MIN PRFBERMAX-OTU')
    lPoint.append('15 Minute Pre-FEC Bit Max Error Rate')
    lTL1.append(' 15-MIN IAE-OTU')
    lPoint.append('15 Minute Incoming Alignment Error ')
    lTL1.append(' 15-MIN QMIN-OTU')
    lPoint.append('15 Minute QMIN (dBQ)')
    lTL1.append(' 15-MIN QMAX-OTU')
    lPoint.append('15 Minute QMAX (dBQ)')
    lTL1.append(' 15-MIN QAVG-OTU')
    lPoint.append('15 Minute QAVG (dBQ)')
    lTL1.append(' 15-MIN QSTDEV-OTU')
    lPoint.append('15 Minute QSTDEV')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT ES-OTU')
    lPoint.append('Untimed Errored Seconds ')
    lTL1.append(' 1-UNT SES-OTU')
    lPoint.append('Untimed Severely Errored Seconds ')
    lTL1.append(' 1-UNT SEFS-OTU')
    lPoint.append('Untimed Severely Errored Frame Seconds ')
    lTL1.append(' 1-UNT HCCS-OTU')
    lPoint.append('Untimed High Correction Count Seconds')
    lTL1.append(' 1-UNT PFBERE-OTU')
    lPoint.append('Untimed Post-FEC Bit Error Rate Estimate')
    lTL1.append(' 1-UNT PRFBER-OUT')
    lPoint.append('1Untimed Pre-FEC Bit Error Rate')
    lTL1.append(' 1-UNT PRFBERMAX-OTU')
    lPoint.append('Untimed Pre-FEC Bit Max Error Rate')
    lTL1.append(' 1-UNT IAE-OTU')
    lPoint.append('Untimed Incoming Alignment Error ')
    lTL1.append(' 1-UNT QMIN-OTU')
    lPoint.append('Untimed QMIN (dBQ)')
    lTL1.append(' 1-UNT QMAX-OTU')
    lPoint.append('Untimed QMAX (dBQ)')
    lTL1.append(' 1-UNT QAVG-OTU')
    lPoint.append('Untimed QAVG (dBQ)')
    lTL1.append(' 1-UNT QSTDEV-OTU')
    lPoint.append('1 UNT QSTDEV')
    lTL1.append(' HCCSREF')
    lPoint.append('HCCSREF (dBQ) relative to post FEC BER=1E-15')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('GCC0 CIRCUITS')
    lTL1.append('')
    lPoint.append('Network Domain')
    lTL1.append('')
    lPoint.append('Carrier')
    lTL1.append('')
    lPoint.append('Operation Carrier')
    lTL1.append('')
    lPoint.append('Protocol')
    lTL1.append('')
    lPoint.append('FCS_Mode')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('GCC1 CIRCUITS')
    lTL1.append('')
    lPoint.append('Network Domain')
    lTL1.append('')
    lPoint.append('Carrier')
    lTL1.append('')
    lPoint.append('Operation Carrier')
    lTL1.append('')
    lPoint.append('Protocol')
    lTL1.append('')
    lPoint.append('FCS_Mode')
    ARRAY_ALL = {}
    for i in dCLFI_SSP:
        f1 = dCLFI_SSP[i]
        AID = i.replace('ADJ', 'OTUTTP')
        location = AID + '@' + str(i_CLFI)
        ARRAY_ALL[location] = dCLFI_SSP[i]

    lAID = []
    AID = '?'
    IS_OSRP_ENABLED = 'NO'
    for line in linesIn:
        if line.find(' "OSRP-') > -1 and line.find('TYPE') > -1:
            IS_OSRP_ENABLED = FISH(line, 'TYPE=', '"')
        elif line.find(' "OTUTTP-') > -1 and line.find('LABEL=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LCLSUPCACID=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'NUMTS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OSRPCHANNEL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OSRPTCMLEVEL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'PREFECSDTHBER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'PREFECSDTHLEV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'PREFECSFTHBER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'PREFECSFTHLEV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'TXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = IS_OSRP_ENABLED
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LCLSUPCACID=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'TXOPER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'RXOPER=\\"', '\\"')
        elif line.find('15-MIN') > -1:
            if line.find('OTUTTP:CV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 22
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:ES-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 23
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:SES-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 24
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:SEFS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 25
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:FEC-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 26
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 27
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:PFBERE-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 28
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 29
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:PRFBERMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 30
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:IAE-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 31
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 32
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 33
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 34
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 35
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        elif line.find('1-UNT') > -1:
            if line.find('OTUTTP:CV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 36
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[7] + ' : ' + f1[8]
                idx = 37
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:ES-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 38
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:SES-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 39
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:SFES-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 40
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 41
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:PFBERE-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 42
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 43
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:IAE-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 44
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 45
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 46
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 47
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
            if line.find('OTUTTP:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 48
                location = AID + '@' + str(idx)
                ARRAY_ALL[location] = f1[2]
        if line.find('OTUTTP::HCCSREF') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f1 = line.find('=') + 1
            f2 = line[f1:-3]
            idx = 49
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = f2
        if AID in dGCC0:
            f1 = dGCC0[AID]
            s1 = f1.split(',')
            idx = 52
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[4]
        if AID in dGCC1:
            f1 = dGCC1[AID]
            s1 = f1.split(',')
            idx = 59
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = s1[4]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_PTP(linesIn, TID, dCPPEC, dCLFI_SSP, F_NOW):
    lTL1 = []
    lPoint = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' LABEL')
    lPoint.append('LABEL')
    lTL1.append(' SERVICETYPE')
    lPoint.append('Service Type')
    lTL1.append(' SPLIMGMT')
    lPoint.append('SPLI  Management')
    lTL1.append(' SPLIMANAGED')
    lPoint.append('SPLI Managed')
    lTL1.append(' OCHTXPWR')
    lPoint.append('Provisioned Tx Power (dBm)')
    lTL1.append(' OCHTXACTPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lTL1.append(' OCHTXMODE ')
    lPoint.append('Tx Compensation Mode')
    lTL1.append(' OCHTXDISPPROV ')
    lPoint.append('Tx Dispersion Provisioned (ps/nm)')
    lTL1.append(' OCHTXPREDISP ')
    lPoint.append('Tx Actual Dispersion (ps/nm)')
    lTL1.append(' OCHRXPOSTDISP ')
    lPoint.append('Rx Dispersion Post-compensation (ps/nm)')
    lTL1.append(' OCHTXWVLNGTHSPACING')
    lPoint.append('Tx Channel Spacing (THz)')
    lTL1.append(' OCHTXWVLNGTHPROV ')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append(' OCHTXFREQMAX ')
    lPoint.append('Tx Maximum Frequency (THz)')
    lTL1.append(' OCHTXFREQPROV ')
    lPoint.append('Tx Frequency (THz)')
    lTL1.append(' OCHTXFREQMIN ')
    lPoint.append('Tx Minimum Frequency (THz)')
    lTL1.append(' OCHRXACTDISP ')
    lPoint.append('Total Rx Link Dispersion (ps/nm)')
    lTL1.append(' OCHTXACTDISP ')
    lPoint.append('Total Tx Link Dispersion (ps/nm)')
    lTL1.append(' OCHRXACTPMD ')
    lPoint.append('Estimated Instance Of DGD (ps)')
    lTL1.append(' OCHMAXPMD')
    lPoint.append('Supported Max DGD (ps)')
    lTL1.append(' OCHUNILATENCY')
    lPoint.append('Estimated Unidirectional Latency (microSec)')
    lTL1.append(' OCHESTLENGTH')
    lPoint.append('Estimated Fiber Length (km)')
    lTL1.append(' OCHREACHSPEC')
    lPoint.append('Reach Specification (km)')
    lTL1.append(' OCHTXTRACE')
    lPoint.append('Transmitted TX Identifier')
    lTL1.append(' OCHRXECHOTRACE')
    lPoint.append('Echoed Trace Rx')
    lTL1.append(' OCHTXASSOCFARENDRX')
    lPoint.append('Associated Far End Rx AID')
    lTL1.append(' OCHTXB')
    lPoint.append('Tx Power Reduced State')
    lTL1.append(' OCHOPTIMIZEMODE')
    lPoint.append('Performance Optimization Mode')
    lTL1.append(' OCHFRR')
    lPoint.append('Fast Receiver Recovery')
    lTL1.append(' OCHFRRCONFIG')
    lPoint.append('Network Configuration')
    lTL1.append(' OCHFRRPATH1DISP')
    lPoint.append('Link  Dispersion  Path 1 (ps/nm)')
    lTL1.append(' OCHFRRPATH2DISP')
    lPoint.append('Link  Dispersion  Path 2 (ps/nm)')
    lTL1.append(' OCHTXDISPMIN')
    lPoint.append('Min Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHTXDISPMAX')
    lPoint.append('Max Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHROTATION')
    lPoint.append('OCH Jones Rotation feature')
    lTL1.append(' OCHSPECTRALOCCUPANCY')
    lPoint.append('OCH Spectral Occupancy setting')
    lTL1.append(' OCHDIFFERENTIALENCODING')
    lPoint.append('OCH Differential Encoding')
    lTL1.append(' OCHTXCHIRP')
    lPoint.append('Tx Chirp')
    lTL1.append(' OCHPWRBALOFFSET')
    lPoint.append('OCH recovery mode')
    lTL1.append(' OCHENMPROV')
    lPoint.append('Provisioned Enhanced Non-linear Mitigation (ENM) mode')
    lTL1.append(' TUNINGMODE')
    lPoint.append('Tuning Mode')
    lTL1.append(' OCHCCDA')
    lPoint.append('TX Channel Contention Detection and Avoidance')
    lTL1.append(' CONDTYPE')
    lPoint.append('Condition Type')
    lTL1.append(' SPLIMANAGED')
    lPoint.append('SPLI Managed')
    lTL1.append(' CLFI')
    lPoint.append('TXRX ADJ CLFI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN OPR-OCH')
    lPoint.append('Current 15 Minutes Rx power level dBm')
    lTL1.append(' 15-MIN OPT-OCH')
    lPoint.append('15 Minutes Tx power value (dBm)')
    lTL1.append(' 15-MIN PRTL DGDAVG-OCH')
    lPoint.append('15 Minutes average Group Delay')
    lTL1.append(' 15-MIN OPR-OTS')
    lPoint.append(' OPR-OTS')
    lTL1.append(' 15-MIN OPR-OTSI')
    lPoint.append(' OPR-OTSI')
    lTL1.append(' 15-MIN OPT-OTSI')
    lPoint.append(' OPT-OTSI')
    lTL1.append(' 15-MIN HCCS-OTSI')
    lPoint.append(' HCCS-OTSI')
    lTL1.append(' 15-MIN PRFBERMAX-OTSI')
    lPoint.append(' PRFBERMAX-OTSI')
    lTL1.append(' 15-MIN QAVE-OTSI')
    lPoint.append(' QAVE-OTSI')
    lTL1.append(' 15-MIN QMIN-OTSI')
    lPoint.append(' QMIN-OTSI')
    lTL1.append(' 15-MIN QMAX-OTSI')
    lPoint.append(' QM-OTSI')
    lTL1.append(' 15-MIN QSTDEV-OTSI')
    lPoint.append(' QSTDEV-OTSI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append(' 1-DAY PRTL OPR-OCH')
    lPoint.append('1 Day Rx power level dBm')
    lTL1.append(' 1-DAY PRTL OPT-OCH')
    lPoint.append('1 Day Tx power value (dBm)')
    lTL1.append(' 1-DAY PRTL DGDAVG-OCH')
    lPoint.append('1 Day average Group Delay')
    lTL1.append(' 1-DAY OPR-OTS')
    lPoint.append(' OPR-OTS')
    lTL1.append(' 1-DAY OPR-OTSI')
    lPoint.append(' OPR-OTSI')
    lTL1.append(' 1-DAY OPT-OTSI')
    lPoint.append(' OPT-OTSI')
    lTL1.append(' 1-DAY HCCS-OTSI')
    lPoint.append(' HCCS-OTSI')
    lTL1.append(' 1-DAY PRFBERMAX-OTSI')
    lPoint.append(' PRFBERMAX-OTSI')
    lTL1.append(' 1-DAY QAVE-OTSI')
    lPoint.append(' QAVE-OTSI')
    lTL1.append(' 1-DAY QMIN-OTSI')
    lPoint.append(' QMIN-OTSI')
    lTL1.append(' 1-DAY QMAX-OTSI')
    lPoint.append(' QM-OTSI')
    lTL1.append(' 1-DAY QSTDEV-OTSI')
    lPoint.append(' QSTDEV-OTSI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT OPR-OCH')
    lPoint.append('Untimed Rx power level dBm')
    lTL1.append(' 1-UNT OPT-OCH')
    lPoint.append('Untimed Tx power value (dBm)')
    lTL1.append(' 1-UNT DGDAVG-OCH')
    lPoint.append('Untimed Group Delay')
    lTL1.append(' 1-UNT OPR-OTS')
    lPoint.append(' OPR-OTS')
    lTL1.append(' 1-UNT OPR-OTSI')
    lPoint.append(' OPR-OTSI')
    lTL1.append(' 1-UNT OPT-OTSI')
    lPoint.append(' OPT-OTSI')
    lTL1.append(' 1-UNT HCCS-OTSI')
    lPoint.append(' HCCS-OTSI')
    lTL1.append(' 1-UNT PRFBERMAX-OTSI')
    lPoint.append(' PRFBERMAX-OTSI')
    lTL1.append(' 1-UNT QAVE-OTSI')
    lPoint.append(' QAVE-OTSI')
    lTL1.append(' 1-UNT QMIN-OTSI')
    lPoint.append(' QMIN-OTSI')
    lTL1.append(' 1-UNT QMAX-OTSI')
    lPoint.append(' QM-OTSI')
    lTL1.append(' 1-UNT QSTDEV-OTSI')
    lPoint.append(' QSTDEV-OTSI')
    i_CLFI = 48
    i_15_OCH = i_CLFI + 3
    i_24_OCH = i_15_OCH + 3 + 10
    i_UNT_OCH = i_24_OCH + 3 + 10
    fErr = ''
    ARRAY_ALL = {}
    for i in dCLFI_SSP:
        f1 = dCLFI_SSP[i]
        AID = i.replace('ADJ', 'PTP')
        location = AID + '@' + str(i_CLFI)
        ARRAY_ALL[location] = dCLFI_SSP[i]

    lAID = []
    AID = '?'
    for line in linesIn:
        if line.find(' "PTP-') > -1 and line.find('OCHTXWVLNGTHPROV') > -1:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            line = line.replace(':', ',')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SERVICETYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SPLIMGMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'SPLIMANAGED=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXDISPPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXPREDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXPOSTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXWVLNGTHSPACING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXFREQMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXFREQPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXFREQMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXACTPMD=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHMAXPMD=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHUNILATENCY=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHESTLENGTH=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHREACHSPEC=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHRXECHOTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXASSOCFARENDRX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXB=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHOPTIMIZEMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHFRR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHFRRCONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHFRRPATH1DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHFRRPATH2DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXDISPMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHTXDISPMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHROTATION=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHSPECTRALOCCUPANCY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHDIFFERENTIALENCODING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHTXCHIRP=', ',')
            ARRAY_ALL[location] = FISH(line, 'OCHTXCHIRP=', ',')
            s1 = AID[:-1]
            try:
                PEC = dCPPEC[s1]
            except:
                PEC = ''

            if PEC.find('NTK539U') > -1 or PEC.find('NTK539B') > -1:
                if f1 != 'POSITIVE':
                    fErr += ',' + AID + ',' + PEC + ' with chirp set to ' + f1 + '\n'
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHPWRBALOFFSET=\\"', '\\",')
            if f1 == '0':
                f2 = 'FW'
            elif f1 == '1':
                f2 = 'SW Override'
            elif f1 == '2':
                f2 = 'QPSK Nonlinear1'
            elif f1 == '3':
                f2 = 'QPSK Nonlinear2'
            elif f1 == '4':
                f2 = 'BPSK Nonlinear1'
            elif f1 == 5:
                f2 = 'BPSK Nonlinear2'
            else:
                f2 = ''
            ARRAY_ALL[location] = f2
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'OCHENMPROV=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'TUNINGMODE=', ',')
            ARRAY_ALL[location] = f1
            if f1 != 'NORMAL' and f1 != 'ACCELERATED' and f1 != '':
                fErr += ',' + AID + ',Does not have Tuning Mode=Performance Optimized or Accelerated\n'
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHCCDA=', ',')
            ARRAY_ALL[location] = f1
            if f1 != 'ON' and f1 != '':
                fErr += ',' + AID + ',Does not have Channel Contention Detection and Avoidance = ON\n'
            idx = idx + 1
            location = AID + '@' + str(idx)
            ARRAY_ALL[location] = FISH(line, 'CONDTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'SPLIMANAGED=', ',')
            ARRAY_ALL[location] = f1
            if f1 != 'YES' and f1 != '':
                fErr += ',' + AID + ',Is not SPLI managed\n'
        if line.find('15-MIN') > -1:
            if line.find('PTP:OPR-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPT-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 1)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:DGDAVG-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 2)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 3)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 4)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPT-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 5)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:HCCS-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 6)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:PRFBERMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 7)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QAVG-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 8)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMIN-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 9)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 10)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QSTDEV-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_15_OCH + 11)
                ARRAY_ALL[location] = f1[2]
        if line.find('1-DAY') > -1:
            if line.find('PTP:OPR-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPT-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 1)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:DGDAVG-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 2)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 3)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 4)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPT-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 5)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:HCCS-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 6)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:PRFBERMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 7)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QAVG-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 8)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMIN-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 9)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 10)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QSTDEV-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_24_OCH + 11)
                ARRAY_ALL[location] = f1[2]
        if line.find('1-UNT') > -1:
            if line.find('PTP:OPR-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 1)
                ARRAY_ALL[location] = f1[2]
                location = AID + '@' + str(i_UNT_OCH)
                ARRAY_ALL[location] = f1[7] + ':' + f1[8]
            if line.find(',PTP:OPT-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 2)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:DGDAVG-OCH') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 3)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTS') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 4)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPR-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 5)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:OPT-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 6)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:HCCS-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 7)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:PRFBERMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 8)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QAVG-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 9)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMIN-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 10)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QMAX-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 11)
                ARRAY_ALL[location] = f1[2]
            if line.find('PTP:QSTDEV-OTSI') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(i_UNT_OCH + 12)
                ARRAY_ALL[location] = f1[2]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ARRAY_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return fErr


def MCEMON_STATUS(mcemonPort):
    mcemon = 'OFF'
    mTimeout = 10
    wasConnected = 'NO'
    if METHOD == 'SSH':
        if not _ensure_paramiko():
            return mcemon
        nbytes = 16384
        try:
            mon_6500 = paramiko.Transport((HOST, mcemonPort))
            mon_6500.connect()
        except Exception as err:
            wasConnected = str(err) + '\n Verify HOST IP, username/password, and that the primary shelf supports SSH'
            return mcemon

        try:
            mon_6500.auth_none(USER)
        except Exception as err:
            mon_6500.close()
            wasConnected = str(err) + '\n Verify Username and/or password'
            return mcemon

        try:
            mon_chan_6500 = mon_6500.open_channel('session')
        except Exception as err:
            mon_6500.close()
            wasConnected = str(err) + '\n SSH session established but no response from the shelf'
            return mcemon

        try:
            mon_chan_6500.invoke_shell()
        except Exception as err:
            mon_6500.close()
            wasConnected = str(err) + '\n SSH session closed by the remote host'
            return mcemon

        capturedText = ''
        t = 0.0
        try:
            mon_chan_6500.settimeout(mTimeout)
            mon_chan_6500.send(';')
            capturedText = _recv_text(mon_chan_6500, nbytes)
        except Exception as err:
            mon_6500.close()
            wasConnected = str(err) + '\n SSH session closed by the remote host'
            return mcemon

        while capturedText.find('-->') < 0 and capturedText.find('hallenge') < 0 and t < mTimeout:
            try:
                mon_chan_6500.send(';')
                capturedText += _recv_text(mon_chan_6500, nbytes)
                time.sleep(0.1)
                t = t + 0.15
            except Exception as err:
                mon_6500.close()
                wasConnected = str(err) + '\n SSH session closed by the remote host'
                return mcemon

        mon_6500.close()
    elif METHOD == 'TELNET':
        try:
            # F004: TL1 mcemon channel uses a Telnet-only TL1 prompt.
            tel_6500 = _Telnet(HOST, mcemonPort, mTimeout,
                               bypass_policy=True, purpose="tl1-mcemon")
        except:
            wasConnected = 'No telnet for this IP'
            return mcemon

        _telnet_write(tel_6500, ';')
        t = _telnet_expect(tel_6500, ['-->', 'hallenge'], mTimeout)
        tel_6500.close()
        capturedText = t[2]
    wasConnected = 'YES'
    if capturedText.find('-->') > -1:
        mcemon = 'ON'
    print ('From DEBUG:\n' + capturedText)
    print ('\nwasConnected = ' + wasConnected)
    return mcemon


def PARSE_Q_GROUPS(linesIn, TID, F_Out):
    f1 = 'AID,SHELF,Index,Default,In Use,Scheduler Profile,Critical Drop Profile,Critical Multiplier,Network Drop Profile,Network Multiplier,Premium Drop Profile,Premium Multiplier,Platinum Drop Profile,Platinum Multiplier,Gold Drop Profile,Gold Multiplier,Silver Drop Profile,Silver Multiplier,Bronze Drop Profile,Bronze Multiplier,Standard Drop Profile,Standard Multiplier,\n'
    F_Out.write(f1)
    dDEFAULT = {}
    dGROUP__S = {}
    lAID = []
    for line in linesIn:
        if line.find('"QGRP-') > -1 and line.find(':TYPE=') > -1:
            f1 = line.split(':')
            AID = f1[0].replace('   "', '')
            f2 = f1[2].replace('TYPE=', '')
            f1 = f2[:-2]
            try:
                dDEFAULT[AID] += ' & ' + f1
            except:
                dDEFAULT[AID] = f1

        if line.find(',CRSC=') > -1:
            f1 = line.split(':')
            AID = f1[0].replace('   "', '')
            rest = f1[2]
            ShelfIndex = AID.replace('QGRP-', '')
            f1 = ShelfIndex.index('-')
            SHELF = ShelfIndex[:f1]
            INDEX = ShelfIndex[f1 + 1:]
            ShelfIndex = SHELF + '+' + INDEX
            lAID.append(ShelfIndex)
            fOut = AID + ',' + SHELF + ',' + INDEX
            try:
                fOut += ',' + dDEFAULT[AID] + ','
            except:
                fOut += ', ,'

            DashShelfDash = '-' + SHELF + '-'
            f2 = rest.split(',')
            f1 = f2[17]
            INUSECOUNT = f1[:-2].replace('INUSECOUNT=', '') + ','
            SCHPRF = f2[0].replace('SCHPRF=SCHPRF' + DashShelfDash, '') + ','
            CRDPRF = f2[1].replace('CRDPRF=DROPPRF' + DashShelfDash, '') + ','
            CRSC = f2[2].replace('CRSC=', '') + ','
            NTDPRF = f2[3].replace('NTDPRF=DROPPRF' + DashShelfDash, '') + ','
            NTSC = f2[4].replace('NTSC=', '') + ','
            PRDPRF = f2[5].replace('PRDPRF=DROPPRF' + DashShelfDash, '') + ','
            PRSC = f2[6].replace('PRSC=', '') + ','
            PLDPRF = f2[7].replace('PLDPRF=DROPPRF' + DashShelfDash, '') + ','
            PLSC = f2[8].replace('PLSC=', '') + ','
            GDDPRF = f2[9].replace('GDDPRF=DROPPRF' + DashShelfDash, '') + ','
            GDSC = f2[10].replace('GDSC=', '') + ','
            SLDPRF = f2[11].replace('SLDPRF=DROPPRF' + DashShelfDash, '') + ','
            SLSC = f2[12].replace('SLSC=', '') + ','
            BRDPRF = f2[13].replace('BRDPRF=DROPPRF' + DashShelfDash, '') + ','
            BRSC = f2[14].replace('BRSC=', '') + ','
            STDPRF = f2[15].replace('STDPRF=DROPPRF' + DashShelfDash, '') + ','
            STSC = f2[16].replace('STSC=', '') + '\n'
            dGROUP__S[ShelfIndex] = fOut + INUSECOUNT + SCHPRF + CRDPRF + CRSC + NTDPRF + NTSC + PRDPRF + PRSC + PLDPRF + PLSC + GDDPRF + GDSC + SLDPRF + SLSC + BRDPRF + BRSC + STDPRF + STSC

    for i in lAID:
        F_Out.write(dGROUP__S[i])

    return None


def BER2Q(f):
    import math
    a = {}
    a[0] = -39.69683028665376
    a[1] = 220.9460984245205
    a[2] = -275.9285104469687
    a[3] = 138.357751867269
    a[4] = -30.66479806614716
    a[5] = 2.506628277459239
    b = {}
    b[0] = -54.47609879822406
    b[1] = 161.5858368580409
    b[2] = -155.6989798598866
    b[3] = 66.80131188771972
    b[4] = -13.28068155288572
    c = {}
    c[0] = -0.007784894002430293
    c[1] = -0.3223964580411365
    c[2] = -2.400758277161838
    c[3] = -2.549732539343734
    c[4] = 4.374664141464968
    c[5] = 2.938163982698783
    d = {}
    d[0] = 0.007784695709041462
    d[1] = 0.3224671290700398
    d[2] = 2.445134137142996
    d[3] = 3.754408661907416
    p = float(f)
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        f1 = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        f2 = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        fout = -f1 / f2
    elif phigh < p:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        f1 = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        f2 = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        fout = f1 / f2
    else:
        q = p - 0.5
        r = q * q
        f1 = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
        f2 = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        fout = -f1 / f2
    f1 = 20.0 * math.log10(fout)
    Q = '%2.3f' % f1
    return str(Q)


def PARSE_RTRV_DS3(linesIn, TID, F_NOW):
    f1 = 'TID,SHELF,AID,Line Build Out,Frame Format,Local RAI,Channel Type,Pstate,Sstate,15Min Reset m-d@h-m,15Min CV Line NEND RCV,15Min ES Line NEND RCV,15Min SES Line NEND RCV,15Min CV Path NEND RCV,15Min ES Path NEND RCV,15Min SES Path NEND RCV,15Min CV Path NEND TRMT,15Min ES Path NEND TRMT,15Min SES Path NEND TRMT,Untimed Reset m-d@h-m,Untimed CV Line NEND RCV,Untimed ES Line NEND RCV,Untimed SES Line NEND RCV,Untimed CV Path NEND RCV,Untimed ES Path NEND RCV,Untimed SES Path NEND RCV,Untimed CV Path NEND TRMT,Untimed ES Path NEND TRMT,Untimed SES path NEND TRMT,\n'
    F_NOW.write(f1)
    d_PM_15MIN__AID = {}
    d_PM_UNTIMED__AID = {}
    d_DS3__AID = {}
    for line in linesIn:
        if line.find('DS3-') > -1 and (line.find('LOCALRAI') > -1 or line.find('CHNLTYPE') > -1 or line.find('LBO=') > -1):
            s1 = line.split(':')
            AID = s1[0].replace('   "', '')
            f1 = AID.split('-')
            SHELF = f1[2]
            STATE = s1[3].replace('"\r', '')
            f1 = FISH(line, 'LBO=', ',')
            if f1 == '1':
                LBO = 'short'
            else:
                LBO = 'long'
            FMT = FISH(s1[2], 'FMT=', ',')
            CHNLTYPE = FISH(s1[2], 'CHNLTYPE=', ',')
            f1 = FISH(s1[2], 'LOCALRAI=', ',')
            if f1 == '0':
                LOCALRAI = 'Allow'
            elif f1 == '1':
                LOCALRAI = 'Inhibit'
            else:
                LOCALRAI = '-'
            d_DS3__AID[AID] = TID + ',' + SHELF + ',' + AID + ',' + LBO + ',' + FMT + ',' + LOCALRAI + ',' + CHNLTYPE + ',' + STATE + ','
            d_PM_15MIN__AID[AID] = ['-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-']
            d_PM_UNTIMED__AID[AID] = ['-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-']
            continue
        if line.find('NEND,RCV') > -1 and line.find('15-MIN') > -1:
            if line.find(',T3:CV-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                f1[1] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:ES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[2] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:SES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[3] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[4] = s1[2]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[5] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[6] = s1[2]
                d_PM_15MIN__AID[AID] = f1
        elif line.find('NEND,TRMT') > -1 and line.find('15-MIN') > -1:
            if line.find(',T3:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[7] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[8] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T3:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[9] = s1[2]
                d_PM_15MIN__AID[AID] = f1
        elif line.find('NEND,RCV') > -1 and line.find('1-UNT,') > -1:
            if line.find(',T3:CV-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                f1[1] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:ES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[2] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:SES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[3] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[4] = s1[2]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[5] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[6] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
        elif line.find('NEND,TRMT') > -1 and line.find('1-UNT,') > -1:
            if line.find(',T3:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[7] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[8] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T3:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[9] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1

    for i in d_DS3__AID:
        s1 = d_DS3__AID[i]
        f1 = d_PM_15MIN__AID[i]
        f2 = d_PM_UNTIMED__AID[i]
        F_NOW.write(''.join(s1) + ','.join(f1) + ',' + ','.join(f2) + ',\n')

    return None


def PARSE_RTRV_DS1(linesIn, TID, F_NOW):
    f1 = 'TID,SHELF,AID,Equalization,Far End Equipment,Fault Locate Mode,Frame Format,Line Code,Mapping,DS1 Mode,Re-Time,Pstate,Sstate,15Min Reset m-d@h-m,15Min CV Line NEND RCV,15Min ES Line NEND RCV,15Min SES Line NEND RCV,15Min CV Path NEND RCV,15Min ES Path NEND RCV,15Min SES Path NEND RCV,15Min CV Path NEND TRMT,15Min ES Path NEND TRMT,15Min SES Path NEND TRMT,Untimed Reset m-d@h-m,Untimed CV Line NEND RCV,Untimed ES Line NEND RCV,Untimed SES Line NEND RCV,Untimed CV Path NEND RCV,Untimed ES Path NEND RCV,Untimed SES Path NEND RCV,Untimed CV Path NEND TRMT,Untimed ES Path NEND TRMT,Untimed SES path NEND TRMT,\n'
    F_NOW.write(f1)
    d_PM_15MIN__AID = {}
    d_PM_UNTIMED__AID = {}
    d_DS1__AID = {}
    for line in linesIn:
        if line.find('"DS1') > -1 and line.find('EQLZ') > -1:
            s1 = line.split(':')
            AID = s1[0].replace('   "', '')
            f1 = AID.split('-')
            SHELF = f1[2]
            STATE = s1[3].replace('"\r', '')
            f1 = FISH(line, 'EQLZ=', ',')
            EQLZ = f1
            if f1 == '1':
                EQLZ = '1  (less than 220 ft)'
            elif f1 == '2':
                EQLZ = '2  (220-430 ft)'
            elif f1 == '3':
                EQLZ = '3  (430-655 ft)'
            FENDNTE = FISH(s1[2], 'FENDNTE=', ',')
            FLMDE = FISH(s1[2], 'FLMDE=', ',')
            FMT = FISH(s1[2], 'FMT=', ',')
            LINECDE = FISH(s1[2], 'LINECDE=', ',')
            MAP = FISH(s1[2], 'MAP=', ',')
            OMODE = FISH(s1[2], 'OMODE=', ',')
            RETIME = FISH(s1[2], 'RETIME=', ',')
            d_DS1__AID[AID] = TID + ',' + SHELF + ',' + AID + ',' + EQLZ + ',' + FENDNTE + ',' + FLMDE + ',' + FMT + ',' + LINECDE + ',' + MAP + ',' + OMODE + ',' + RETIME + ',' + STATE + ','
            d_PM_15MIN__AID[AID] = ['-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-']
            d_PM_UNTIMED__AID[AID] = ['-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-']
            continue
        if line.find('NEND,RCV') > -1 and line.find('15-MIN') > -1:
            if line.find(',T1:CV-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                f1[1] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:ES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[2] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:SES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[3] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[4] = s1[2]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[5] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[6] = s1[2]
                d_PM_15MIN__AID[AID] = f1
        elif line.find('NEND,TRMT') > -1 and line.find('15-MIN') > -1:
            if line.find(',T1:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[7] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[8] = s1[2]
                d_PM_15MIN__AID[AID] = f1
            elif line.find(',T1:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_15MIN__AID[AID]
                f1[9] = s1[2]
                d_PM_15MIN__AID[AID] = f1
        elif line.find('NEND,RCV') > -1 and line.find('1-UNT,') > -1:
            if line.find(',T1:CV-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                f1[1] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:ES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[2] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:SES-L,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[3] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[4] = s1[2]
                f1[0] = "'" + s1[7] + '@' + s1[8]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[5] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[6] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
        elif line.find('NEND,TRMT') > -1 and line.find('1-UNT,') > -1:
            if line.find(',T1:CV-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[7] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:ES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[8] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1
            elif line.find(',T1:SES-P,') > -1:
                s1 = line.split(',')
                AID = s1[0].replace('   "', '')
                f1 = d_PM_UNTIMED__AID[AID]
                f1[9] = s1[2]
                d_PM_UNTIMED__AID[AID] = f1

    for i in d_DS1__AID:
        s1 = d_DS1__AID[i]
        f1 = d_PM_15MIN__AID[i]
        f2 = d_PM_UNTIMED__AID[i]
        F_NOW.write(''.join(s1) + ','.join(f1) + ',' + ','.join(f2) + ',\n')

    return None


def PARSE_RTRV_CRS_ALL(linesIn, TID, dCTYPE, F_NOW):
    f1 = 'TID,SHELF,Circuit Pack_A,Circuit Pack_Z,FROM,TO,Type,Rate,Mate,Circuit ID,Prime,\n'
    F_NOW.write(f1)
    for line in linesIn:
        if line.find('PRIMARYXC') > -1:
            line = line.replace(',FFP:PRI', ',FFP-PRI')
            s1 = line.split(':')
            f2 = s1[0].split(',')
            f1 = f2[0]
            FROM = f1.replace('   "', '')
            TO = f2[1]
            CCT = s1[1]
            RATE = s1[-2]
            PRIME = FISH(line, 'PRIME=', ',')
            SWMATE = FISH(line, 'SWMATE=', ',')
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            f1 = FROM.split('-')
            SHELF = f1[1]
            _Sh_Sl_ = '-' + SHELF + '-' + f1[2] + '-'
            try:
                FROM_CP = dCTYPE[_Sh_Sl_]
            except:
                FROM_CP = '-'

            f1 = TO.split('-')
            if TO.find('HLINK') > -1:
                l1 = len(f1) - 2
                l2 = l1 - 1
                _Sh_Sl_ = '-' + f1[l2] + '-' + f1[l1] + '-'
                SHELF = f1[l2]
            else:
                SHELF = f1[1]
                _Sh_Sl_ = '-' + SHELF + '-' + f1[2] + '-'
            try:
                TO_CP = dCTYPE[_Sh_Sl_]
            except:
                TO_CP = '-'

            f1 = TID + ',' + SHELF + ',' + FROM_CP + ',' + TO_CP + ',' + FROM + ',' + TO + ',' + CCT + ',' + RATE + ',' + SWMATE + ',' + CKTID + ',' + PRIME + ',\n'
            F_NOW.write(f1)

    return None


def PARSE_RTRV_CRS_ODUx(linesIn, TID, dCTYPE, dRATE__AID, F_Out):
    f1 = 'TID,SHELF,Circuit Pack_A,Circuit Pack_Z,FROM,TO,Protection Switch Mate,Protection Destination Mate,Type,Circuit ID,Rate,Owner,Flex CRS AID\n'
    F_Out.write(f1)
    for line in linesIn:
        if line.find(' "ODU') > -1 and line.find('ODUCTP') < 0:
            s1 = line.split(':')
            f2 = s1[0].split(',')
            f1 = f2[0]
            FROM = f1.replace('   "', '')
            TO = f2[1]
            l1 = FROM.find('-')
            RATE = FROM[:l1]
            CCT = s1[1]
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            SWMATE = FISH(line, 'SWMATE=', ',')
            f1 = FROM.split('-')
            SHELF = f1[1]
            _Sh_Sl_ = '-' + SHELF + '-' + f1[2] + '-'
            try:
                FROM_CP = dCTYPE[_Sh_Sl_]
            except:
                FROM_CP = '-'

            f1 = TO.split('-')
            _Sh_Sl_ = '-' + f1[1] + '-' + f1[2] + '-'
            try:
                TO_CP = dCTYPE[_Sh_Sl_]
            except:
                TO_CP = '-'

            f1 = TID + ',' + SHELF + ',' + FROM_CP + ',' + TO_CP + ',' + FROM + ',' + TO + ',' + SWMATE + ',-,' + CCT + ',"' + CKTID + '",' + RATE + ',\n'
            F_Out.write(f1)
        elif line.find(' "ODUCTP') > -1 and line.find('CKTID=') > -1:
            s1 = line.split(':')
            f2 = s1[0].split(',')
            f1 = f2[0]
            FROM = f1.replace('   "', '')
            TO = f2[1]
            try:
                RATE = dRATE__AID[FROM]
            except:
                try:
                    RATE = dRATE__AID[TO]
                except:
                    RATE = ''

            CCT = s1[1]
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            SWMATE = FISH(line, 'SWMATE=', ',')
            DSWMATE = FISH(line, 'DSWMATE=', ',')
            f1 = FROM.split('-')
            SHELF = f1[1]
            _Sh_Sl_ = '-' + SHELF + '-' + f1[2] + '-'
            try:
                FROM_CP = dCTYPE[_Sh_Sl_]
            except:
                FROM_CP = '-'

            f1 = TO.split('-')
            _Sh_Sl_ = '-' + f1[1] + '-' + f1[2] + '-'
            try:
                TO_CP = dCTYPE[_Sh_Sl_]
            except:
                TO_CP = '-'

            line = line.replace('"', ',')
            OWNER = FISH(line, 'OWNER=', ',')
            FCCID = FISH(line, 'FCCID=', ',')
            f1 = TID + ',' + SHELF + ',' + FROM_CP + ',' + TO_CP + ',' + FROM + ',' + TO + ',' + SWMATE + ',' + DSWMATE + ',' + CCT + ',"' + CKTID + '",' + RATE + ',' + OWNER + ',' + FCCID + ',\n'
            F_Out.write(f1)

    return None


def PARSE_SYNCHRONIZATION(linesIn, TID, lSHELF_ID, lSHELF_XC, sysNEMODE, F_NOW):
    lPoint = []
    lTL1 = []
    lPoint.append('TID = ' + TID)
    lPoint.append('Provisioned Timing Mode')
    lPoint.append('Input Timing Reference')
    lPoint.append('First')
    lPoint.append('Second')
    lPoint.append('Third')
    lPoint.append('Fourth')
    lPoint.append('External Synchronization Mode')
    lPoint.append('BITSIN / ESI A')
    lPoint.append('BITSIN / ESI B')
    lPoint.append('BITSOUT / ESO A Distribution Reference')
    lPoint.append('First')
    lPoint.append('Second')
    lPoint.append('Third')
    lPoint.append('Fourth')
    lPoint.append('Signal Provisioning')
    lPoint.append('BITSOUT / ESO B Distribution Reference')
    lPoint.append('First')
    lPoint.append('Second')
    lPoint.append('Third')
    lPoint.append('Fourth')
    lPoint.append('Signal Provisioning')
    lPoint.append('Time Generation Switch Hierarchy')
    lPoint.append('First')
    lPoint.append('Second')
    lPoint.append('Third')
    lPoint.append('Fourth')
    lPoint.append('Time Distribution Switch Hierarchy')
    lPoint.append('First')
    lPoint.append('Second')
    lPoint.append('Third')
    lPoint.append('Fourth')
    lAID = []
    SYNC_ALL = {}
    for i in lSHELF_ID:
        lAID.append(i)
        SYNC_ALL[i + '@0'] = i
        if i in lSHELF_XC:
            f1 = ' Shelf has XC'
        else:
            f1 = 'No XC & no Timing was provisioned'
        SYNC_ALL[i + '@1'] = f1

    BITSOUTSW = False
    for line in linesIn:
        if line.find('Begin: RTRV-BITSOUTSW') > -1:
            BITSOUTSW = True
            continue
        elif line.find('End: RTRV-BITSOUTSW') > -1:
            BITSOUTSW = False
            continue
        f1 = line.split(':')
        l1 = len(f1)
        if line.find(':MODE=') > -1 and l1 == 2:
            line = line.replace('"', '')
            l1 = line.find(':')
            f1 = line[0:l1]
            aid = f1.replace('   ', '')
            SYNC_ALL[aid + '@0'] = aid
            lAID.append(aid)
            l1 = line.find('=') + 1
            l2 = len(line)
            f2 = line[l1:l2]
            if f2.find('EXT') > -1:
                f2 = f2 + ' : Externally-Timed (locked to BITS)'
            elif f2.find('INT') > -1:
                f2 = f2 + ' : Self-Timed (freerun) '
            elif f2.find('LINE') > -1:
                f2 = f2 + ' : Line-Timed (locked to traffic facility)'
            elif f2.find('MIXED') > -1:
                f2 = f2 + ' : Mixed (Lock to BITS and Traffic Facility)'
            elif f2.find('NOTINUSE') > -1:
                f2 = f2 + ' : Timing mode not in use (retrieve only)'
            SYNC_ALL[aid + '@1'] = f2
        if line.find(':FIRST=') > -1 and line.find(':SECOND=') > -1 and line.find('BITSOUT') < 0 and line.find(',MANSTATUS') < 0:
            f1 = line.split(':')
            aid = f1[0].replace('   "', '')
            lAID.append(aid)
            list1 = [1,
            2,
            3,
            4]
            for j in list1:
                if f1[j].find('FIRST=') > -1:
                    idx = 3
                    f2 = f1[j].split(',')
                elif f1[j].find('SECOND=') > -1:
                    idx = 4
                    f2 = f1[j].split(',')
                elif f1[j].find('THIRD=') > -1:
                    idx = 5
                    f2 = f1[j].split(',')
                elif f1[j].find('FOURTH=') > -1:
                    idx = 6
                    f1[j] = f1[j].replace('"', '')
                    f2 = f1[j].split(',')
                if f2[0].find('NONE') > -1:
                    s1 = f2[0].split('=')
                    SYNC_ALL[aid + '@' + str(idx)] = s1[1].strip()
                else:
                    s1 = "'State = " + f2[1]
                    s1 = s1 + '  \nIncoming Quality = ' + f2[2]
                    s1 = s1 + '  \nIncoming Provisioned Quality = ' + f2[3]
                    s1 = s1 + '  \nIncoming Detected Quality = ' + f2[4]
                    SYNC_ALL[aid + '@' + str(idx)] = s1

            SYNC_ALL[aid + '@7'] = sysNEMODE
        if line.find('"BITSIN-') > -1 and line.find(',IMP=') > -1:
            l1 = line.find(':')
            f1 = line[0:l1]
            f2 = f1.replace('   "', '')
            f1 = f2.split('-')
            aid = 'SHELF-' + str(f1[1])
            lAID.append(aid)
            bitsin_A = 'BITSIN-' + str(f1[1]) + '-A'
            bitsin_B = 'BITSIN-' + str(f1[1]) + '-B'
            if line.find(bitsin_A) > -1:
                s1 = "'" + aid + '  \nSignal Format = ' + FISH(line, 'MT=', ',')
                s1 = s1 + '  \nLine code = ' + FISH(line, 'LINECDE=', ',')
                s1 = s1 + '  \nImpedance = ' + FISH(line, 'IMP=', '"')
                s1 = s1 + '  \nFrame format = ' + FISH(line, ',FMT=', ',')
                s1 = s1 + '  \nSAN = ' + FISH(line, 'SAN=', ',')
                s1 = s1 + '  \nDUS overrite = ' + FISH(line, 'DUSOVERRIDE=', ',')
                SYNC_ALL[aid + '@8'] = s1
            if line.find(bitsin_B) > -1:
                s1 = "'" + aid + '  \nSignal Format = ' + FISH(line, 'SIGFMT=', ',')
                s1 = s1 + '  \nLine code = ' + FISH(line, 'LINECDE=', ',')
                s1 = s1 + '  \nImpedance = ' + FISH(line, 'IMP=', '"')
                s1 = s1 + '  \nFrame format = ' + FISH(line, ',FMT=', ',')
                s1 = s1 + '  \nSAN = ' + FISH(line, 'SAN=', ',')
                s1 = s1 + '  \nDUS overrite = ' + FISH(line, 'DUSOVERRIDE=', ',')
                SYNC_ALL[aid + '@9'] = s1
        if line.find('"BITSOUT-') > -1 and line.find(':FIRST=') > -1:
            f1 = line.split(':')
            aid = f1[0].replace('   "', '')
            f2 = aid.split('-')
            shelf = 'SHELF-' + str(f2[1])
            lAID.append(shelf)
            bitsout_A = 'BITSOUT-' + str(f2[1]) + '-A'
            bitsout_B = 'BITSOUT-' + str(f2[1]) + '-B'
            bitsin_A = 'BITSIN-' + str(f2[1]) + '-A'
            bitsin_B = 'BITSIN-' + str(f2[1]) + '-B'
            if aid == bitsout_A:
                idx = 10
            else:
                idx = 16
            list1 = [1,
            2,
            3,
            4,
            5]
            for j in list1:
                f2 = f1[j]
                if f2.find('FIRST=') > -1:
                    idx = idx + 1
                elif f2.find('SECOND=') > -1:
                    idx = idx + 1
                elif f2.find('THIRD=') > -1:
                    idx = idx + 1
                elif f2.find('FOURTH=') > -1:
                    idx = idx + 1
                elif f2.find('SIGFMT=') > -1:
                    idx = idx + 1
                if f2.find('NONE-') > -1:
                    SYNC_ALL[shelf + '@' + str(idx)] = 'NONE'
                elif f2.find('SIGFMT=') > -1:
                    f2 = f2.replace('SIGFMT', 'SIGFMT1')
                    s1 = "'" + aid + '  \nSignal Format = ' + FISH(line, 'SIGFMT1=', ',')
                    s1 = s1 + '  \nThreshold = ' + FISH(line, 'THRESHOLD=', ',')
                    s1 = s1 + '  \nImpedance = ' + FISH(line, 'IMP=', '"')
                    s1 = s1 + '  \nFrame format = ' + FISH(line, ',FMT=', ',')
                    s1 = s1 + '  \nSAN = ' + FISH(line, 'SAN=', ',')
                    SYNC_ALL[shelf + '@' + str(idx)] = s1
                else:
                    f2 = f2.split(',')
                    s1 = "'" + aid + '  ' + f2[0] + ' \nState = ' + f2[1]
                    s1 = s1 + ' Incoming Quality = ' + f2[2]
                    s1 = s1 + ' Incoming Provisioned Quality = ' + f2[3]
                    s1 = s1 + ' Incoming Detected Quality = ' + f2[4]
                    SYNC_ALL[shelf + '@' + str(idx)] = '-'

        if not BITSOUTSW:
            if line.find('LCKSTATUS=') > -1 and line.find('MANSTATUS') > -1 or line.find('=NONE:,,,') > -1 and line.find('-') > -1:
                f1 = line.split(':')
                aid = f1[0].replace('   "', '')
                lAID.append(aid)
                protFac = f1[1]
                f1 = protFac.split('=')
                s1 = f1[1].strip()
                if protFac.find('FIRST') > -1:
                    idx = 23
                elif protFac.find('SECOND') > -1:
                    idx = 24
                if protFac.find('THIRD') > -1:
                    idx = 25
                if protFac.find('FOURTH') > -1:
                    idx = 26
                if s1 != 'NONE':
                    s1 = "'" + s1 + '  \nLocked Status = ' + FISH(line, 'LCKSTATUS=', ',')
                    s1 = s1 + '  \nForced Status = ' + FISH(line, 'FRCDSTATUS=', ',')
                    s1 = s1 + '  \nAuto Status = ' + FISH(line, 'AUTOSTATUS=', ',')
                    s1 = s1 + '  \nManual Status = ' + FISH(line, ',MANSTATUS=', ',')
                SYNC_ALL[aid + '@' + str(idx)] = s1
        elif BITSOUTSW:
            pass

    CCC = []
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = SYNC_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_SONET(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' OCHTXPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lTL1.append(' OCHTXWVLNGTHPROV')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append(' NLS')
    lPoint.append('Non Linear Supression compensation')
    lTL1.append(' FEC')
    lPoint.append('FEC Type')
    lTL1.append(' STFORMAT')
    lPoint.append('Section Trace Format')
    lTL1.append(' STRC')
    lPoint.append('Transmitted Section Trace')
    lTL1.append(' EXPSTRC')
    lPoint.append('Expected Section Trace')
    lTL1.append(' PORTMODE')
    lPoint.append('Protocol')
    lTL1.append(' TMGREF')
    lPoint.append('Facility is a Timing Reference')
    lTL1.append(' DCC')
    lPoint.append('Facility has DCC Enabled')
    lTL1.append(' DSMINFO')
    lPoint.append('DSMINFO')
    lTL1.append(' LASEROFFFARENDFAIL')
    lPoint.append('Laser Off Far End Fail')
    lTL1.append('')
    lPoint.append('CLFI')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append('15-MIN')
    lPoint.append('15 Minutes')
    lTL1.append(' CV-S,NEND,RCV')
    lPoint.append('Code Violations - Section Near Rx')
    lTL1.append(' CV-S,NEND,TRMT')
    lPoint.append('Code Violations - Section Near Tx')
    lTL1.append(' ES-S,NEND,RCV')
    lPoint.append('Errored Seconds - Section Near Rx')
    lTL1.append(' ES-S,NEND,TRMT')
    lPoint.append('Errored Seconds - Section Near Tx')
    lTL1.append(' SES-S,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Section Near Rx')
    lTL1.append(' SES-S,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Section Near Tx')
    lTL1.append(' SEFS-S,NEND,RCV')
    lPoint.append('Severely Errored Frame Seconds - Section Near Rx')
    lTL1.append(' SEFS-S,NEND,TRMT')
    lPoint.append('Severely Errored Frame Seconds - Section Near Tx')
    lTL1.append(' CV-L,NEND,RCV')
    lPoint.append('Code Violations - Line Near Rx')
    lTL1.append(' CV-L,FEND,RCV')
    lPoint.append('Code Violations - Line Far Rx')
    lTL1.append(' CV-L,NEND,TRMT')
    lPoint.append('Code Violations - Line Near Tx')
    lTL1.append(' ES-L,NEND,RCV')
    lPoint.append('Errored Seconds - Line Near Rx')
    lTL1.append(' ES-L,FEND,RCV')
    lPoint.append('Errored Seconds - Line Far Rx')
    lTL1.append(' ES-L,NEND,TRMT')
    lPoint.append('Errored Seconds - Line Near Tx')
    lTL1.append(' SES-L,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Near Rx')
    lTL1.append(' SES-L,FEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Far Rx')
    lTL1.append(' SES-L,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Line Near Tx')
    lTL1.append(' UAS-L,NEND,RCV')
    lPoint.append('Unavailable Seconds - Line Near Rx')
    lTL1.append(' UAS-L,FEND,RCV')
    lPoint.append('Unavailable Seconds - Line Far Rx')
    lTL1.append(' UAS-L,NEND,TRMT')
    lPoint.append('navailable Seconds - Line End Tx')
    lTL1.append(' FC-L,NEND,RCV')
    lPoint.append('Failure Count - Line Near Rx')
    lTL1.append(' FC-L,FEND,RCV')
    lPoint.append('Failure Count - Line Far Rx')
    lTL1.append(' FC-L,NEND,TRMT')
    lPoint.append('Failure Count - Line End Tx')
    lTL1.append(' PSCW-L,NEND,RCV')
    lPoint.append('Protection Switch Count Working - Line End Tx')
    lTL1.append(' PSCP-L,NEND,RCV')
    lPoint.append('Protection Switch Count Protection - Line End Tx')
    lTL1.append(' PSD-L,NEND,TRMT')
    lPoint.append('Protection Switch Duration - Line End Tx')
    lTL1.append('')
    lPoint.append('')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT CV-S,NEND,RCV')
    lPoint.append('Code Violations - Section Near Rx')
    lTL1.append(' 1-UNT CV-S,NEND,TRMT')
    lPoint.append('Code Violations - Section Near Tx')
    lTL1.append(' 1-UNT ES-S,NEND,RCV')
    lPoint.append('Errored Seconds - Section Near Rx')
    lTL1.append(' 1-UNT ES-S,NEND,TRMT')
    lPoint.append('Errored Seconds - Section Near Tx')
    lTL1.append(' 1-UNT SES-S,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Section Near Rx')
    lTL1.append(' 1-UNT SES-S,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Section Near Tx')
    lTL1.append(' 1-UNT SEFS-S,NEND,RCV')
    lPoint.append('Severely Errored Framed Seconds - Section Near Rx')
    lTL1.append(' 1-UNT SEFS-S,NEND,TRMT')
    lPoint.append('Severely Errored Framed Seconds - Section Near Tx')
    lTL1.append(' 1-UNT CV-L,NEND,RCV')
    lPoint.append('Code Violations - Line Near Rx')
    lTL1.append(' 1-UNT CV-L,FEND,RCV')
    lPoint.append('Code Violations - Line Far Rx')
    lTL1.append(' 1-UNT CV-L,NEND,TRMT')
    lPoint.append('Code Violations - Line Near Tx')
    lTL1.append(' 1-UNT ES-L,NEND,RCV')
    lPoint.append('Errored Seconds - Line Near Rx')
    lTL1.append(' 1-UNT ES-L,FEND,RCV')
    lPoint.append('Errored Seconds - Line Far Rx')
    lTL1.append(' 1-UNT ES-L,NEND,TRMT')
    lPoint.append('Errored Seconds - Line Near Tx')
    lTL1.append(' 1-UNT SES-L,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Near Rx')
    lTL1.append(' 1-UNT SES-L,FEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Far Rx')
    lTL1.append(' 1-UNT SES-L,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Line Near Tx')
    lTL1.append(' 1-UNT UAS-L,NEND,RCV')
    lPoint.append('Unavailable Seconds - Line Near Rx')
    lTL1.append(' 1-UNT UAS-L,FEND,RCV')
    lPoint.append('Unavailable Seconds - Line Far Rx')
    lTL1.append(' 1-UNT UAS-L,NEND,TRMT')
    lPoint.append('Unavailable Seconds - Line End Tx')
    lTL1.append(' 1-UNT FC-L,NEND,RCV')
    lPoint.append('Failure Count - Line Near Rx')
    lTL1.append(' 1-UNT FC-L,FEND,RCV')
    lPoint.append('Failure Count - Line Far Rx')
    lTL1.append(' 1-UNT FC-L,NEND,TRMT')
    lPoint.append('Failure Count - Line End Tx')
    lTL1.append(' 1-UNT PSCW-L,NEND,RCV')
    lPoint.append('Protection Switch Count Working - Line End Tx')
    lTL1.append(' 1-UNT PSCP-L,NEND,RCV')
    lPoint.append('Protection Switch Count Protection - Line End Tx')
    lTL1.append(' 1-UNT PSD-L,NEND,TRMT')
    lPoint.append('Protection Switch Duration - Line End Tx')
    lTL1.append(' ')
    lPoint.append('')
    lTL1.append(' ')
    lPoint.append('SONET DCC')
    lTL1.append(' CARRIER')
    lPoint.append('Carrier')
    lTL1.append(' OPER_CARRIER')
    lPoint.append('Byte Carrying DCC')
    lTL1.append(' L2INFO')
    lPoint.append('LAPD Frame Size')
    lTL1.append(' L2SIDE')
    lPoint.append('Local Shelf Role')
    lTL1.append(' OPER_L2SIDE')
    lPoint.append('Local Node Role')
    lTL1.append(' NETDOMAIN')
    lPoint.append('Management Communications Network\t')
    lTL1.append(' PROTOCOL')
    lPoint.append('Protocol')
    lTL1.append(' FCS_MODE')
    lPoint.append('FCS Mode')
    lTL1.append(' ')
    lPoint.append('')
    lTL1.append(' ')
    lPoint.append('SONET IISIS Circuits')
    lTL1.append(' CARRIER')
    lPoint.append('Carrier')
    lTL1.append(' CKTDEFMETRIC')
    lPoint.append('Circuit default metric')
    lTL1.append(' L2ONLY')
    lPoint.append('Level 2 Only')
    lTL1.append(' NPSOVERRIDE')
    lPoint.append('Neighbour Protocols Supported Override')
    lTL1.append(' THREEWAYHS')
    lPoint.append('Three-Way Handshake')
    lAID = []
    SONET_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('CLFI=') > -1 and line.find('PORTMODE=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            SONET_ALL[location] = AID
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, ',NLS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, ',FEC=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'STFORMAT=', ',')
            SONET_ALL[location] = f1.strip('\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'STRC=', ',')
            SONET_ALL[location] = f1.strip('\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'EXPSTRC=', ',')
            SONET_ALL[location] = f1.strip('\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'PORTMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'TMGREF=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, ',DCC=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            if line.find('DSM=') > -1:
                SONET_ALL[location] = FISH(line, ',DSM=', ',')
            else:
                SONET_ALL[location] = FISH(line, ',DSMINFO=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'LASEROFFFARENDFAIL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'CLFI=\\"', '\\"')
        if line.find('15-MIN') > -1 and line.find('OSC') < 0:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':CV-S,') > -1:
                s1 = FISH(line, ':CV-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@21'
                else:
                    location = AID + '@22'
                SONET_ALL[location] = s1
                continue
            if line.find(':ES-S,') > -1:
                s1 = FISH(line, ':ES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@23'
                else:
                    location = AID + '@24'
                SONET_ALL[location] = s1
                continue
            if line.find(':SES-S,') > -1:
                s1 = FISH(line, ':SES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@25'
                else:
                    location = AID + '@26'
                SONET_ALL[location] = s1
                continue
            if line.find(':SEFS-S,') > -1:
                s1 = FISH(line, ':SEFS-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@27'
                else:
                    location = AID + '@28'
                SONET_ALL[location] = s1
                continue
            if line.find(':CV-L,') > -1:
                s1 = FISH(line, ':CV-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@29'
                    else:
                        location = AID + '@30'
                else:
                    location = AID + '@31'
                SONET_ALL[location] = s1
                continue
            if line.find(':ES-L,') > -1:
                s1 = FISH(line, ':ES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@32'
                    else:
                        location = AID + '@33'
                else:
                    location = AID + '@34'
                SONET_ALL[location] = s1
                continue
            if line.find(':SES-L,') > -1:
                s1 = FISH(line, ':SES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@35'
                    else:
                        location = AID + '@36'
                else:
                    location = AID + '@37'
                SONET_ALL[location] = s1
                continue
            if line.find(':UAS-L,') > -1:
                s1 = FISH(line, ':UAS-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@38'
                    else:
                        location = AID + '@39'
                else:
                    location = AID + '@40'
                SONET_ALL[location] = s1
                continue
            if line.find(':FC-L,') > -1:
                s1 = FISH(line, ':FC-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@41'
                    else:
                        location = AID + '@42'
                else:
                    location = AID + '@43'
                SONET_ALL[location] = s1
                continue
            if line.find(':PSCW-L,') > -1:
                location = AID + '@44'
                SONET_ALL[location] = FISH(line, ':PSCW-L,', ',')
                continue
            if line.find(':PSCP-L,') > -1:
                location = AID + '@45'
                SONET_ALL[location] = FISH(line, ':PSCP-L,', ',')
                continue
            if line.find(':PSD-L,') > -1:
                location = AID + '@46'
                SONET_ALL[location] = FISH(line, ':PSD-L,', ',')
                continue
        if line.find('1-UNT') > -1 and line.find('OSC') < 0:
            f1 = line.split(',')
            AID = f1[0].replace('   "', '')
            if line.find(':OPR-OCH,') > -1:
                SONET_ALL[AID + '@4'] = FISH(line, ':OPR-OCH,', ',')
            if line.find(':OPT-OCH,') > -1:
                SONET_ALL[AID + '@2'] = FISH(line, ':OPT-OCH,', ',')
            if line.find(':CV-S,') > -1:
                f1 = line.split(',')
                SONET_ALL[AID + '@48'] = f1[7] + ' : ' + f1[8]
                s1 = FISH(line, ':CV-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@49'
                else:
                    location = AID + '@50'
                SONET_ALL[location] = s1
                continue
            if line.find(':ES-S,') > -1:
                s1 = FISH(line, ':ES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@51'
                else:
                    location = AID + '@52'
                SONET_ALL[location] = s1
                continue
            if line.find(':SES-S,') > -1:
                s1 = FISH(line, ':SES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@53'
                else:
                    location = AID + '@54'
                SONET_ALL[location] = s1
                continue
            if line.find(':SEFS-S,') > -1:
                s1 = FISH(line, ':SEFS-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@55'
                else:
                    location = AID + '@56'
                SONET_ALL[location] = s1
                continue
            if line.find(':CV-L,') > -1:
                s1 = FISH(line, ':CV-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@57'
                    else:
                        location = AID + '@58'
                else:
                    location = AID + '@59'
                SONET_ALL[location] = s1
                continue
            if line.find(':ES-L,') > -1:
                s1 = FISH(line, ':ES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@60'
                    else:
                        location = AID + '@61'
                else:
                    location = AID + '@62'
                SONET_ALL[location] = s1
                continue
            if line.find(':SES-L,') > -1:
                s1 = FISH(line, ':SES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@63'
                    else:
                        location = AID + '@64'
                else:
                    location = AID + '@65'
                SONET_ALL[location] = s1
                continue
            if line.find(':UAS-L,') > -1:
                s1 = FISH(line, ':UAS-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@66'
                    else:
                        location = AID + '@67'
                else:
                    location = AID + '@68'
                SONET_ALL[location] = s1
                continue
            if line.find(':FC-L,') > -1:
                s1 = FISH(line, ':FC-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@69'
                    else:
                        location = AID + '@70'
                else:
                    location = AID + '@71'
                SONET_ALL[location] = s1
                continue
            if line.find(':PSCW-L,') > -1:
                location = AID + '@72'
                SONET_ALL[location] = FISH(line, ':PSCW-L,', ',')
                continue
            if line.find(':PSCP-L,') > -1:
                location = AID + '@73'
                SONET_ALL[location] = FISH(line, ':PSCP-L,', ',')
                continue
            if line.find(':PSD-L,') > -1:
                location = AID + '@74'
                SONET_ALL[location] = FISH(line, ':PSD-L,', ',')
                continue
        idx = 77
        if line.find('NETDOMAIN=') > -1:
            f1 = line.split('::')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OPER_CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'L2INFO=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'L2SIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'OPER_L2SIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'NETDOMAIN=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'PROTOCOL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'FCS_MODE=', '"')
        idx = 87
        if line.find('::CKTDEFMETRIC=') > -1:
            f1 = line.split('::')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'CKTDEFMETRIC=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'L2ONLY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'NPSOVERRIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            SONET_ALL[location] = FISH(line, 'THREEWAYHS=', ',')

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = SONET_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_STTP(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point')
    lTL1.append(' LABEL')
    lPoint.append('Customer Defined Label')
    lTL1.append(' RATE')
    lPoint.append('Rate')
    lTL1.append(' SDTH')
    lPoint.append('Signal Degrade Threshold')
    lTL1.append(' EBERTH')
    lPoint.append('Excessive BER Threshold')
    lTL1.append(' MAPPING')
    lPoint.append('Packet Mapping')
    lTL1.append(' LCLSUPCACID')
    lPoint.append('Local CAC Line ID')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append('15-MIN')
    lPoint.append('15 Minutes')
    lTL1.append(' CV-S,NEND,RCV')
    lPoint.append('Code Violations - Section Near Rx')
    lTL1.append(' CV-S,NEND,TRMT')
    lPoint.append('Code Violations - Section Near Tx')
    lTL1.append(' ES-S,NEND,RCV')
    lPoint.append('Errored Seconds - Section Near Rx')
    lTL1.append(' ES-S,NEND,TRMT')
    lPoint.append('Errored Seconds - Section Near Tx')
    lTL1.append(' SES-S,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Section Near Rx')
    lTL1.append(' SES-S,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Section Near Tx')
    lTL1.append(' SEFS-S,NEND,RCV')
    lPoint.append('Severely Errored Frame Seconds - Section Near Rx')
    lTL1.append(' SEFS-S,NEND,TRMT')
    lPoint.append('Severely Errored Frame Seconds - Section Near Tx')
    lTL1.append(' CV-L,NEND,RCV')
    lPoint.append('Code Violations - Line Near Rx')
    lTL1.append(' CV-L,FEND,RCV')
    lPoint.append('Code Violations - Line Far Rx')
    lTL1.append(' CV-L,NEND,TRMT')
    lPoint.append('Code Violations - Line Near Tx')
    lTL1.append(' ES-L,NEND,RCV')
    lPoint.append('Errored Seconds - Line Near Rx')
    lTL1.append(' ES-L,FEND,RCV')
    lPoint.append('Errored Seconds - Line Far Rx')
    lTL1.append(' ES-L,NEND,TRMT')
    lPoint.append('Errored Seconds - Line Near Tx')
    lTL1.append(' SES-L,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Near Rx')
    lTL1.append(' SES-L,FEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Far Rx')
    lTL1.append(' SES-L,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Line Near Tx')
    lTL1.append(' UAS-L,NEND,RCV')
    lPoint.append('Unavailable Seconds - Line Near Rx')
    lTL1.append(' UAS-L,FEND,RCV')
    lPoint.append('Unavailable Seconds - Line Far Rx')
    lTL1.append(' UAS-L,NEND,TRMT')
    lPoint.append('navailable Seconds - Line End Tx')
    lTL1.append(' FC-L,NEND,RCV')
    lPoint.append('Failure Count - Line Near Rx')
    lTL1.append(' FC-L,FEND,RCV')
    lPoint.append('Failure Count - Line Far Rx')
    lTL1.append(' FC-L,NEND,TRMT')
    lPoint.append('Failure Count - Line End Tx')
    lTL1.append('')
    lPoint.append('')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT CV-S,NEND,RCV')
    lPoint.append('Code Violations - Section Near Rx')
    lTL1.append(' 1-UNT CV-S,NEND,TRMT')
    lPoint.append('Code Violations - Section Near Tx')
    lTL1.append(' 1-UNT ES-S,NEND,RCV')
    lPoint.append('Errored Seconds - Section Near Rx')
    lTL1.append(' 1-UNT ES-S,NEND,TRMT')
    lPoint.append('Errored Seconds - Section Near Tx')
    lTL1.append(' 1-UNT SES-S,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Section Near Rx')
    lTL1.append(' 1-UNT SES-S,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Section Near Tx')
    lTL1.append(' 1-UNT SEFS-S,NEND,RCV')
    lPoint.append('Severely Errored Framed Seconds - Section Near Rx')
    lTL1.append(' 1-UNT SEFS-S,NEND,TRMT')
    lPoint.append('Severely Errored Framed Seconds - Section Near Tx')
    lTL1.append(' 1-UNT CV-L,NEND,RCV')
    lPoint.append('Code Violations - Line Near Rx')
    lTL1.append(' 1-UNT CV-L,FEND,RCV')
    lPoint.append('Code Violations - Line Far Rx')
    lTL1.append(' 1-UNT CV-L,NEND,TRMT')
    lPoint.append('Code Violations - Line Near Tx')
    lTL1.append(' 1-UNT ES-L,NEND,RCV')
    lPoint.append('Errored Seconds - Line Near Rx')
    lTL1.append(' 1-UNT ES-L,FEND,RCV')
    lPoint.append('Errored Seconds - Line Far Rx')
    lTL1.append(' 1-UNT ES-L,NEND,TRMT')
    lPoint.append('Errored Seconds - Line Near Tx')
    lTL1.append(' 1-UNT SES-L,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Near Rx')
    lTL1.append(' 1-UNT SES-L,FEND,RCV')
    lPoint.append('Severely Errored Seconds - Line Far Rx')
    lTL1.append(' 1-UNT SES-L,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Line Near Tx')
    lTL1.append(' 1-UNT UAS-L,NEND,RCV')
    lPoint.append('Unavailable Seconds - Line Near Rx')
    lTL1.append(' 1-UNT UAS-L,FEND,RCV')
    lPoint.append('Unavailable Seconds - Line Far Rx')
    lTL1.append(' 1-UNT UAS-L,NEND,TRMT')
    lPoint.append('Unavailable Seconds - Line End Tx')
    lTL1.append(' 1-UNT FC-L,NEND,RCV')
    lPoint.append('Failure Count - Line Near Rx')
    lTL1.append(' 1-UNT FC-L,FEND,RCV')
    lPoint.append('Failure Count - Line Far Rx')
    lTL1.append(' 1-UNT FC-L,NEND,TRMT')
    lPoint.append('Failure Count - Line End Tx')
    lAID = []
    STTP_ALL = {}
    AID = '?'
    i15 = 12
    iUNT = i15 + 24
    for line in linesIn:
        if line.find('LABEL=') > -1 and line.find('RATE=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            STTP_ALL[location] = AID
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = FISH(line, 'SUPPTP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = FISH(line, 'LABEL=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = FISH(line, ',RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = '1.0E-' + FISH(line, ',SDTH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = '1.0E-' + FISH(line, ',EBERTH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = FISH(line, ',MAPPING=', ':')
            idx = idx + 1
            location = AID + '@' + str(idx)
            STTP_ALL[location] = FISH(line, 'LCLSUPCACID=\\"', '\\"')
        if line.find('15-MIN') > -1 and line.find('OSC') < 0:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':CV-S,') > -1:
                s1 = FISH(line, ':CV-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15)
                else:
                    location = AID + '@' + str(i15 + 1)
                STTP_ALL[location] = s1
                continue
            if line.find(':ES-S,') > -1:
                s1 = FISH(line, ':ES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15 + 2)
                else:
                    location = AID + '@' + str(i15 + 3)
                STTP_ALL[location] = s1
                continue
            if line.find(':SES-S,') > -1:
                s1 = FISH(line, ':SES-S,', ',')
                if line.find('RCV') > -1:
                    location = location = AID + '@' + str(i15 + 4)
                else:
                    location = AID + '@' + str(i15 + 5)
                STTP_ALL[location] = s1
                continue
            if line.find(':SEFS-S,') > -1:
                s1 = FISH(line, ':SEFS-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15 + 6)
                else:
                    location = AID + '@' + str(i15 + 7)
                STTP_ALL[location] = s1
                continue
            if line.find(':CV-L,') > -1:
                s1 = FISH(line, ':CV-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(i15 + 8)
                    else:
                        location = AID + '@' + str(i15 + 9)
                else:
                    location = AID + '@' + str(i15 + 10)
                STTP_ALL[location] = s1
                continue
            if line.find(':ES-L,') > -1:
                s1 = FISH(line, ':ES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(i15 + 11)
                    else:
                        location = AID + '@' + str(i15 + 12)
                else:
                    location = AID + '@' + str(i15 + 13)
                STTP_ALL[location] = s1
                continue
            if line.find(':SES-L,') > -1:
                s1 = FISH(line, ':SES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(i15 + 14)
                    else:
                        location = AID + '@' + str(i15 + 15)
                else:
                    location = AID + '@' + str(i15 + 16)
                STTP_ALL[location] = s1
                continue
            if line.find(':UAS-L,') > -1:
                s1 = FISH(line, ':UAS-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(i15 + 17)
                    else:
                        location = AID + '@' + str(i15 + 18)
                else:
                    location = AID + '@' + str(i15 + 19)
                STTP_ALL[location] = s1
                continue
            if line.find(':FC-L,') > -1:
                s1 = FISH(line, ':FC-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(i15 + 20)
                    else:
                        location = AID + '@' + str(i15 + 21)
                else:
                    location = AID + '@' + str(i15 + 22)
                STTP_ALL[location] = s1
                continue
        if line.find('1-UNT') > -1 and line.find('OSC') < 0:
            f1 = line.split(',')
            AID = f1[0].replace('   "', '')
            if line.find(':CV-S,') > -1:
                f1 = line.split(',')
                location = AID + '@' + str(iUNT)
                STTP_ALL[location] = f1[7] + ' : ' + f1[8]
                s1 = FISH(line, ':CV-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(iUNT + 1)
                else:
                    location = AID + '@' + str(iUNT + 2)
                STTP_ALL[location] = s1
                continue
            if line.find(':ES-S,') > -1:
                s1 = FISH(line, ':ES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(iUNT + 3)
                else:
                    location = AID + '@' + str(iUNT + 4)
                STTP_ALL[location] = s1
                continue
            if line.find(':SES-S,') > -1:
                s1 = FISH(line, ':SES-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(iUNT + 5)
                else:
                    location = AID + '@' + str(iUNT + 6)
                STTP_ALL[location] = s1
                continue
            if line.find(':SEFS-S,') > -1:
                s1 = FISH(line, ':SEFS-S,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(iUNT + 7)
                else:
                    location = AID + '@' + str(iUNT + 8)
                STTP_ALL[location] = s1
                continue
            if line.find(':CV-L,') > -1:
                s1 = FISH(line, ':CV-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(iUNT + 9)
                    else:
                        location = AID + '@' + str(iUNT + 10)
                else:
                    location = AID + '@' + str(iUNT + 11)
                STTP_ALL[location] = s1
                continue
            if line.find(':ES-L,') > -1:
                s1 = FISH(line, ':ES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(iUNT + 12)
                    else:
                        location = AID + '@' + str(iUNT + 13)
                else:
                    location = AID + '@' + str(iUNT + 14)
                STTP_ALL[location] = s1
                continue
            if line.find(':SES-L,') > -1:
                s1 = FISH(line, ':SES-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(iUNT + 15)
                    else:
                        location = AID + '@' + str(iUNT + 16)
                else:
                    location = AID + '@' + str(iUNT + 17)
                STTP_ALL[location] = s1
                continue
            if line.find(':UAS-L,') > -1:
                s1 = FISH(line, ':UAS-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(iUNT + 18)
                    else:
                        location = AID + '@' + str(iUNT + 19)
                else:
                    location = AID + '@' + str(iUNT + 20)
                STTP_ALL[location] = s1
                continue
            if line.find(':FC-L,') > -1:
                s1 = FISH(line, ':FC-L,', ',')
                if line.find('RCV') > -1:
                    if line.find('NEND') > -1:
                        location = AID + '@' + str(iUNT + 21)
                    else:
                        location = AID + '@' + str(iUNT + 22)
                else:
                    location = AID + '@' + str(iUNT + 23)
                STTP_ALL[location] = s1
                continue

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = STTP_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_ETH(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' OCHTXPWR')
    lPoint.append('TX Actual Power (dBm)')
    lPoint.append('TX Actual High Power (dBm)')
    lTL1.append(' OCHTXACTHIGHPWR')
    lPoint.append('TX Actual Low Power (dBm)')
    lTL1.append(' OCHTXACTLOWPWR')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('RX Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lPoint.append('RX Actual High Power (dBm)')
    lTL1.append(' OCHRXACTHIGHPWR')
    lPoint.append('RX Actual Low Power (dBm)')
    lTL1.append(' OCHRXACTLOWPWR')
    lTL1.append(' OCHTXWVLNGTHPROV')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append('LASEROFFFARENDFAIL')
    lPoint.append('Laser Off Far End Fail')
    lTL1.append(' MTU')
    lPoint.append('Maximum Ethernet frame size')
    lTL1.append(' MAPPING')
    lPoint.append('Packet Mapping')
    lTL1.append(' FLOWCTRL')
    lPoint.append('Advertized Flow Control')
    lTL1.append(' ETHDPX')
    lPoint.append('Advertised Duplex Operation')
    lTL1.append(' SPEED')
    lPoint.append('Advertised Link Speed')
    lTL1.append(' AN')
    lPoint.append('Auto Negotiation')
    lTL1.append(' ANSTATUS')
    lPoint.append('Auto-negotiation Status')
    lTL1.append(' IFTYPE')
    lPoint.append('Interface Type')
    lTL1.append(' PAUSETX')
    lPoint.append('Pause Transmission')
    lTL1.append(' PAUSERX')
    lPoint.append('Pause Reception')
    lTL1.append(' PAUSETXOVERRIDE')
    lPoint.append('Pause Transmission Override')
    lTL1.append(' PAUSERXOVERRIDE')
    lPoint.append('Pause Reception Override')
    lTL1.append(' PASSCTRL')
    lPoint.append('Pass Control')
    lTL1.append(' PHYSADDR')
    lPoint.append('MAC Address')
    lTL1.append(' POLICING')
    lPoint.append('Policing')
    lTL1.append(' SNMPINDEX')
    lPoint.append('SNMP Index')
    lTL1.append(' ETYPE')
    lPoint.append('Ether Type (in Hex)')
    lTL1.append(' CFPRF')
    lPoint.append('Control Frame Profile   (CFPRF-shelf-profile#)')
    lTL1.append(' RXCOSPRF')
    lPoint.append('Receive Class Of Service Profile')
    lTL1.append(' TXCOSPRF')
    lPoint.append('Transmit Class Of Service Profile')
    lTL1.append(' URXCOS')
    lPoint.append('Untagged Receive Class Of Service')
    lTL1.append(' QGRP1')
    lPoint.append('Queue Group1')
    lTL1.append(' PORTBW')
    lPoint.append('Port Bandwidth')
    lTL1.append(' ADVETHDPX')
    lPoint.append('Link Partner Advertised Duplex Operation')
    lTL1.append(' ADVSPEED')
    lPoint.append('Link Partner Advertised Speed')
    lTL1.append(' ADVFLOWCTRL')
    lPoint.append('Link Partner Advertised Flow Control')
    lTL1.append(' ANETHDPX')
    lPoint.append('Negotiated Duplex Operation')
    lTL1.append(' ANSPEED')
    lPoint.append('Negotiated Speed')
    lTL1.append(' ANPAUSETX')
    lPoint.append('Negotiated Pause Transmission')
    lTL1.append(' ANPAUSERX')
    lPoint.append('Negotiated Pause Reception')
    lTL1.append(' TXIPG')
    lPoint.append('Inter-Packet Gap')
    lTL1.append(' TXCON')
    lPoint.append('Port Conditioning')
    lTL1.append(' TXCONHB')
    lPoint.append('Conditioning Heartbeat')
    lTL1.append(' TXCONHBINTERVAL')
    lPoint.append('Conditioning HB Interval')
    lTL1.append(' TXCONMDLEVEL')
    lPoint.append('Conditioning MD Level')
    lTL1.append(' TXCONNWFLTSIG')
    lPoint.append('Conditioning  Network Signal')
    lTL1.append(' RATE')
    lPoint.append('Rate')
    lTL1.append(' MODE')
    lPoint.append('Mode of Service')
    lTL1.append(' TMGREF')
    lPoint.append('Used As Timing Reference')
    lTL1.append(' CBRBWREMAIN')
    lPoint.append('CBRBWREMAIN')
    lTL1.append(' LATENCYOPT')
    lPoint.append('Latency Optimization')
    lTL1.append(' DUSOVERRIDE')
    lPoint.append('DUS Override')
    lTL1.append(' SSMTRANSMIT')
    lPoint.append('SSMTRANSMIT')
    lTL1.append('')
    lPoint.append('CLFI')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN')
    lPoint.append(' 15 Minutes')
    lTL1.append(' 15-MIN ES-E,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 15-MIN ES-E,TRMT')
    lPoint.append('Errored Seconds Transmitted')
    lTL1.append(' 15-MIN SES-E,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 15-MIN SES-E,TRMT')
    lPoint.append('Severe Errored Seconds Transmitted')
    lTL1.append(' 15-MIN UAS-E,RCV')
    lPoint.append('Unavailable Seconds Received')
    lTL1.append(' 15-MIN UAS-E,TRMT')
    lPoint.append('Unavailable Seconds Transmitted')
    lTL1.append(' 15-MIN INFRAMES-E,RCV')
    lPoint.append('Number of frames Received')
    lTL1.append(' 15-MIN OUTFRAMES-E,TRMT')
    lPoint.append('Number of frames Transmitted')
    lTL1.append(' 15-MIN INFRAMESERR-E,RCV')
    lPoint.append('Number of errored frames received')
    lTL1.append(' 15-MIN OUTFRAMESERR-E,TRMT')
    lPoint.append('Total egress direction frames transmitted with FCS errors')
    lTL1.append(' 15-MIN INFRAMESDISCDS-E,RCV')
    lPoint.append('Ingress frames discarded due to congestion or policing')
    lTL1.append(' 15-MIN OUTFRAMESDISCDS-E,TRMT')
    lPoint.append('Egress frames discarded due to congestion or policing')
    lTL1.append(' 15-MIN FCSERR-E,RCV')
    lPoint.append('Frame Check Sequence Errors')
    lTL1.append(' 15-MIN FCSERR-E,TRMT')
    lPoint.append('Frame Check Sequence Errors')
    lTL1.append(' 15-MIN DFR-E,RCV')
    lPoint.append('Total ingress frames discarded for any reason other than FCS errors')
    lTL1.append(' 15-MIN DFR-E,TRMT')
    lPoint.append('Total egress frames discarded for any reason other than FCS errors')
    lTL1.append(' 15-MIN UAS-PCS,RCV')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer')
    lTL1.append(' ')
    lPoint.append(' ')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT ES-E,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 1-UNT ES-E,TRMT')
    lPoint.append('Errored Seconds Transmitted')
    lTL1.append(' 1-UNT SES-E,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 1-UNT SES-E,TRMT')
    lPoint.append('Severe Errored Seconds Transmitted')
    lTL1.append(' 1-UNT UAS-E,RCV')
    lPoint.append('Unavailable Seconds Received')
    lTL1.append(' 1-UNT UAS-E,TRMT')
    lPoint.append('Unavailable Seconds Transmitted')
    lTL1.append(' 1-UNT INFRAMES-E,RCV')
    lPoint.append('Number of frames Received')
    lTL1.append(' 1-UNT OUTFRAMES-E,TRMT')
    lPoint.append('Number of frames Transmitted')
    lTL1.append(' 1-UNT INFRAMESERR-E,RCV')
    lPoint.append('Number of errored frames received')
    lTL1.append(' 1-UNT OUTFRAMESERR-E,TRMT')
    lPoint.append('Total egress direction frames transmitted with FCS errors')
    lTL1.append(' 1-UNT INFRAMESDISCDS-E,RCV')
    lPoint.append('Ingress frames discarded due to congestion or policing')
    lTL1.append(' 1-UNT OUTFRAMESDISCDS-E,TRMT')
    lPoint.append('Egress frames discarded due to congestion or policing')
    lTL1.append(' 1-UNT FCSERR-E,RCV')
    lPoint.append('Frame Check Sequence Errors')
    lTL1.append(' 1-UNT FCSERR-E,TRMT')
    lPoint.append('Frame Check Sequence Errors')
    lTL1.append(' 1-UNT DFR-E,RCV')
    lPoint.append('Total ingress frames discarded for any reason other than FCS errors')
    lTL1.append(' 1-UNT DFR-E,TRMT')
    lPoint.append('Total egress frames discarded for any reason other than FCS errors')
    lTL1.append(' 1-UNT UAS-PCS,RCV')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer')
    lTL1.append(' 1-UNT OPR-OCH,RCV')
    lPoint.append('Rx Power')
    lTL1.append(' 1-UNT OPT-OCH,TRMT')
    lPoint.append('Tx Power')
    i15MIN = 58
    i1UNT = i15MIN + 19
    lAID = []
    ETH_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('LASEROFFFARENDFAIL') > -1 or line.find('MAPPING=') > -1 or line.find('MTU=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            ETH_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = states
            l1 = l1 - 1
            f1 = line[0:l1]
            line = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHTXACTHIGHPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHTXACTLOWPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHRXACTHIGHPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHRXACTLOWPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'LASEROFFFARENDFAIL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'MTU=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'MAPPING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'FLOWCTRL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ETHDPX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'SPEED=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'AN=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ANSTATUS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'IFTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PAUSETX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PAUSERX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PAUSETXOVERRIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PAUSERXOVERRIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PASSCTRL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PHYSADDR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'POLICING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'SNMPINDEX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ETYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'CFPRF=', ',')
            if f1 == 4:
                f1 = 'P2P Tunnel'
            ETH_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'RXCOSPRF=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCOSPRF=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'URXCOS=', ',')
            if f1 == '10':
                f1 = 'Premium-Green'
            ETH_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'QGRP1=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'PORTBW=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ADVETHDPX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ADVSPEED=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ADVFLOWCTRL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ANETHDPX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ANSPEED=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ANPAUSETX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'ANPAUSERX=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXIPG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCON=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCONHB=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCONHBINTERVAL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCONMDLEVEL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TXCONNWFLTSIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'MODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'TMGREF=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'CBRBWREMAIN=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'LATENCYOPT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'DUSOVERRIDE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'SSMTRANSMIT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            ETH_ALL[location] = FISH(line, 'CLFI=\\"', '\\"')
        if line.find('15-MIN') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-E,') > -1:
                f1 = FISH(line, ':ES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15MIN + 1)
                else:
                    location = AID + '@' + str(i15MIN + 2)
                ETH_ALL[location] = f1
            if line.find(':SES-E,') > -1:
                f1 = FISH(line, ':SES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15MIN + 3)
                else:
                    location = AID + '@' + str(i15MIN + 4)
                ETH_ALL[location] = f1
            if line.find(':UAS-E,') > -1:
                f1 = FISH(line, ':UAS-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15MIN + 5)
                else:
                    location = AID + '@' + str(i15MIN + 6)
                ETH_ALL[location] = f1
            if line.find(':INFRAMES-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i15MIN + 7)
                ETH_ALL[location] = FISH(line, ':INFRAMES-E,', ',')
            if line.find(':OUTFRAMES-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i15MIN + 8)
                ETH_ALL[location] = FISH(line, ':OUTFRAMES-E,', ',')
            if line.find(':INFRAMESERR-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i15MIN + 9)
                ETH_ALL[location] = FISH(line, ':INFRAMESERR-E,', ',')
            if line.find(':OUTFRAMESERR-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i15MIN + 10)
                ETH_ALL[location] = FISH(line, ':OUTFRAMESERR-E,', ',')
            if line.find(':INFRAMESDISCDS-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i15MIN + 11)
                ETH_ALL[location] = FISH(line, ':INFRAMESDISCDS-E,', ',')
            if line.find(':OUTFRAMESDISCDS-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i15MIN + 12)
                ETH_ALL[location] = FISH(line, ':OUTFRAMESDISCDS-E,', ',')
            if line.find(':FCSERR-E') > -1:
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15MIN + 13)
                else:
                    location = AID + '@' + str(i15MIN + 14)
                ETH_ALL[location] = FISH(line, ':FCSERR-E,', ',')
            if line.find(':DFR-E,') > -1:
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i15MIN + 15)
                else:
                    location = AID + '@' + str(i15MIN + 16)
                ETH_ALL[location] = FISH(line, ':DFR-E,', ',')
            if line.find(':UAS-PCS,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i15MIN + 17)
                ETH_ALL[location] = FISH(line, ':UAS-PCS,', ',')
        if line.find('1-UNT') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-E,') > -1:
                location = AID + '@' + str(i1UNT)
                ETH_ALL[location] = f1[7] + ' : ' + f1[8]
                f1 = FISH(line, ':ES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i1UNT + 1)
                else:
                    location = AID + '@' + str(i1UNT + 2)
                ETH_ALL[location] = f1
            if line.find(':SES-E,') > -1:
                f1 = FISH(line, ':SES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i1UNT + 3)
                else:
                    location = AID + '@' + str(i1UNT + 4)
                ETH_ALL[location] = f1
            if line.find(':UAS-E,') > -1:
                f1 = FISH(line, ':UAS-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@' + str(i1UNT + 5)
                else:
                    location = AID + '@' + str(i1UNT + 6)
                ETH_ALL[location] = f1
            if line.find(':INFRAMES-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i1UNT + 7)
                ETH_ALL[location] = FISH(line, ':INFRAMES-E,', ',')
            if line.find(':OUTFRAMES-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i1UNT + 8)
                ETH_ALL[location] = FISH(line, ':OUTFRAMES-E,', ',')
            if line.find(':INFRAMESERR-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i1UNT + 9)
                ETH_ALL[location] = FISH(line, ':INFRAMESERR-E,', ',')
            if line.find(':OUTFRAMESERR-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i1UNT + 10)
                ETH_ALL[location] = FISH(line, ':OUTFRAMESERR-E,', ',')
            if line.find(':INFRAMESDISCDS-E,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i1UNT + 11)
                ETH_ALL[location] = FISH(line, ':INFRAMESDISCDS-E,', ',')
            if line.find(':OUTFRAMESDISCDS-E,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i1UNT + 12)
                ETH_ALL[location] = FISH(line, ':OUTFRAMESDISCDS-E,', ',')
            if line.find(':FCSERR-E,') > -1:
                if line.find('TRMT') > -1:
                    location = AID + '@' + str(i1UNT + 13)
                else:
                    location = AID + '@' + str(i1UNT + 14)
                ETH_ALL[location] = FISH(line, ':FCSERR-E,', ',')
            if line.find(':DFR-E,') > -1:
                if line.find('TRMT') > -1:
                    location = AID + '@' + str(i1UNT + 15)
                else:
                    location = AID + '@' + str(i1UNT + 16)
                ETH_ALL[location] = FISH(line, ':DFR-E,', ',')
            if line.find(':UAS-PCS,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i1UNT + 17)
                ETH_ALL[location] = FISH(line, ':UAS-PCS,', ',')
            if line.find(':OPR-OCH,') > -1 and line.find('RCV') > -1:
                location = AID + '@' + str(i1UNT + 18)
                ETH_ALL[location] = FISH(line, ':OPR-OCH,', ',')
            if line.find(':OPT-OCH,') > -1 and line.find('TRMT') > -1:
                location = AID + '@' + str(i1UNT + 19)
                ETH_ALL[location] = FISH(line, ':OPT-OCH,', ',')

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = ETH_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_FLEX(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' PROTOCOL')
    lPoint.append('Protocol')
    lTL1.append(' PRATE')
    lPoint.append('Protocol Rate (Mbps)')
    lTL1.append(' MAPPEDRATE')
    lPoint.append('Mapped Rate')
    lTL1.append(' MAPPING')
    lPoint.append('Packet Mapping')
    lTL1.append(' TXCON')
    lPoint.append('Conditioning Type')
    lTL1.append(' OCHTXACTPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Rx Maximum Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Rx Minimum Power (dBm)')
    lTL1.append(' INGPOLICE')
    lPoint.append('Ingress Policing')
    lTL1.append(' CIR')
    lPoint.append('Committed Information Rate(Mbits/sec)')
    lTL1.append('CBSUNITS')
    lPoint.append('Committed Burst Size Unit')
    lTL1.append('CBS')
    lPoint.append('Committed Burst Size')
    lTL1.append('')
    lPoint.append('CLFI')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN')
    lPoint.append('15 Minutes (Near End)')
    lTL1.append(' 15-MIN ES-E,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 15-MIN ES-E,TRMT')
    lPoint.append('Errored Seconds Transmitted')
    lTL1.append(' 15-MIN SES-E,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 15-MIN SES-E,TRMT')
    lPoint.append('Severe Errored Seconds Transmitted')
    lTL1.append(' 15-MIN UAS-E,RCV')
    lPoint.append('Unavailable Seconds Received')
    lTL1.append(' 15-MIN UAS-E,TRMT')
    lPoint.append('Unavailable Seconds Transmitted')
    lTL1.append(' 15-MIN INFRAMES-E,RCV')
    lPoint.append('Number of frames Received')
    lTL1.append(' 15-MIN OUTFRAMES-E,TRMT')
    lPoint.append('Number of frames Transmitted')
    lTL1.append(' 15-MIN INFRAMESERR-E,RCV')
    lPoint.append('Number of errored frames received')
    lTL1.append(' 15-MIN OUTFRAMESERR-E,TRMT')
    lPoint.append('Total egress direction frames transmitted with FCS errors')
    lTL1.append(' 15-MIN INFRAMESDISCDS-E,RCV')
    lPoint.append('Discarded ingress frames due to congestion or policing')
    lTL1.append(' 15-MIN DFR-E,RCV')
    lPoint.append('Discarded ingress frames for any reason other than FCS errors')
    lTL1.append(' 15-MIN CV-PCS,NEND,RCV')
    lPoint.append('Code Violations - Physical Coding Sublayer Near Rx')
    lTL1.append(' 15-MIN CV-PCS,NEND,TRMT')
    lPoint.append('Code Violations - Physical Coding Sublayer Near Tx')
    lTL1.append(' 15-MIN ES-PCS,NEND,RCV')
    lPoint.append('Errored Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 15-MIN ES-PCS,NEND,TRMT')
    lPoint.append('Errored Seconds - Physical Coding Sublayer Near Tx')
    lTL1.append(' 15-MIN SES-PCS,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 15-MIN SES-PCS,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Physical Coding Sublayer Near Tx')
    lTL1.append(' 15-MIN UAS-PCS,NEND,RCV')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 15-MIN UAS-PCS,NEND,TRMT')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer Near Tx')
    lTL1.append(' ')
    lPoint.append(' ')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT ES-E,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 1-UNT ES-E,TRMT')
    lPoint.append('Errored Seconds Transmitted')
    lTL1.append(' 1-UNT SES-E,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 1-UNT SES-E,TRMT')
    lPoint.append('Severe Errored Seconds Transmitted')
    lTL1.append(' 1-UNT UAS-E,RCV')
    lPoint.append('Unavailable Seconds Received')
    lTL1.append(' 1-UNT UAS-E,TRMT')
    lPoint.append('Unavailable Seconds Transmitted')
    lTL1.append(' 1-UNT INFRAMES-E,RCV')
    lPoint.append('Number of frames Received')
    lTL1.append(' 1-UNT OUTFRAMES-E,TRMT')
    lPoint.append('Number of frames Transmitted')
    lTL1.append(' 1-UNT INFRAMESERR-E,RCV')
    lPoint.append('Number of errored frames received')
    lTL1.append(' 1-UNT OUTFRAMESERR-E,TRMT')
    lPoint.append('Total egress direction frames transmitted with FCS errors')
    lTL1.append(' 1-UNT INFRAMESDISCDS-E,RCV')
    lPoint.append('Discarded ingress frames due to congestion or policing')
    lTL1.append(' 1-UNT DFR-E,RCV')
    lPoint.append('Discarded ingress frames for any reason other than FCS errors')
    lTL1.append(' 1-UNT CV-PCS,NEND,RCV')
    lPoint.append('Code Violations - Physical Coding Sublayer Near Rx')
    lTL1.append(' 1-UNT CV-PCS,NEND,TRMT')
    lPoint.append('Code Violations - Physical Coding Sublayer Near Tx')
    lTL1.append(' 1-UNT ES-PCS,NEND,RCV')
    lPoint.append('Errored Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 1-UNT ES-PCS,NEND,TRMT')
    lPoint.append('Errored Seconds - Physical Coding Sublayer Near Tx')
    lTL1.append(' 1-UNT SES-PCS,NEND,RCV')
    lPoint.append('Severely Errored Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 1-UNT SES-PCS,NEND,TRMT')
    lPoint.append('Severely Errored Seconds - Physical Coding Sublayer Near Tx')
    lTL1.append(' 1-UNT UAS-PCS,NEND,RCV')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer Near Rx')
    lTL1.append(' 1-UNT UAS-PCS,NEND,TRMT')
    lPoint.append('Unavailable Seconds - Physical Coding Sublayer Near Tx')
    lAID = []
    FLEX_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('PROTOCOL=') > -1 and line.find('MAPPING=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = AID
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'PROTOCOL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'PRATE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'MAPPEDRATE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'MAPPING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'TXCON=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'INGPOLICE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'CIR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'CBSUNITS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'CBS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            FLEX_ALL[location] = FISH(line, 'CLFI=\\"', '\\"')
        if line.find('15-MIN') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-E,') > -1:
                f1 = FISH(line, ':ES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@19'
                else:
                    location = AID + '@20'
                FLEX_ALL[location] = f1
            if line.find(':SES-E,') > -1:
                f1 = FISH(line, ':SES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@21'
                else:
                    location = AID + '@22'
                FLEX_ALL[location] = f1
            if line.find(':UAS-E,') > -1:
                f1 = FISH(line, ':UAS-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@23'
                else:
                    location = AID + '@24'
                FLEX_ALL[location] = f1
            if line.find(':INFRAMES-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@25'] = FISH(line, ':INFRAMES-E,', ',')
            if line.find(':OUTFRAMES-E,') > -1 and line.find('TRMT') > -1:
                FLEX_ALL[AID + '@26'] = FISH(line, ':OUTFRAMES-E,', ',')
            if line.find(':INFRAMESERR-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@27'] = FISH(line, ':INFRAMESERR-E,', ',')
            if line.find(':OUTFRAMESERR-E,') > -1 and line.find('TRMT') > -1:
                FLEX_ALL[AID + '@28'] = FISH(line, ':OUTFRAMESERR-E,', ',')
            if line.find(':INFRAMESDISCDS-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@29'] = FISH(line, ':INFRAMESDISCDS-E,', ',')
            if line.find(':DFR-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@30'] = FISH(line, ':DFR-E,', ',')
            if line.find(':CV-PCS,') > -1:
                f1 = FISH(line, ':CV-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@31'
                else:
                    location = AID + '@32'
                FLEX_ALL[location] = f1
            if line.find(':ES-PCS,') > -1:
                f1 = FISH(line, ':ES-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@33'
                else:
                    location = AID + '@34'
                FLEX_ALL[location] = f1
            if line.find(':SES-PCS,') > -1:
                f1 = FISH(line, ':SES-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@35'
                else:
                    location = AID + '@36'
                FLEX_ALL[location] = f1
            if line.find(':UAS-PCS,') > -1:
                f1 = FISH(line, ':UAS-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@37'
                else:
                    location = AID + '@38'
                FLEX_ALL[location] = f1
        if line.find('1-UNT') > -1 and line.find('FLEX-') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-E,') > -1:
                f1 = line.split(',')
                FLEX_ALL[AID + '@40'] = f1[7] + ' : ' + f1[8]
                f1 = FISH(line, ':ES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@41'
                else:
                    location = AID + '@42'
                FLEX_ALL[location] = f1
            if line.find(':SES-E,') > -1:
                f1 = FISH(line, ':SES-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@43'
                else:
                    location = AID + '@44'
                FLEX_ALL[location] = f1
            if line.find(':UAS-E,') > -1:
                f1 = FISH(line, ':UAS-E,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@45'
                else:
                    location = AID + '@46'
                FLEX_ALL[location] = f1
            if line.find(':INFRAMES-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@47'] = FISH(line, ':INFRAMES-E,', ',')
            if line.find(':OUTFRAMES-E,') > -1 and line.find('TRMT') > -1:
                FLEX_ALL[AID + '@48'] = FISH(line, ':OUTFRAMES-E,', ',')
            if line.find(':INFRAMESERR-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@49'] = FISH(line, ':INFRAMESERR-E,', ',')
            if line.find(':OUTFRAMESERR-E,') > -1 and line.find('TRMT') > -1:
                FLEX_ALL[AID + '@50'] = FISH(line, ':OUTFRAMESERR-E,', ',')
            if line.find(':INFRAMESDISCDS-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@51'] = FISH(line, ':INFRAMESDISCDS-E,', ',')
            if line.find(':DFR-E,') > -1 and line.find('RCV') > -1:
                FLEX_ALL[AID + '@52'] = FISH(line, ':DFR-E,', ',')
            if line.find(':CV-PCS,') > -1:
                f1 = line.split(',')
                FLEX_ALL[AID + '@40'] = f1[7] + ' : ' + f1[8]
                f1 = FISH(line, ':CV-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@53'
                else:
                    location = AID + '@54'
                FLEX_ALL[location] = f1
            if line.find(':ES-PCS,') > -1:
                f1 = FISH(line, ':ES-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@55'
                else:
                    location = AID + '@56'
                FLEX_ALL[location] = f1
            if line.find(':SES-PCS,') > -1:
                f1 = FISH(line, ':SES-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@57'
                else:
                    location = AID + '@58'
                FLEX_ALL[location] = f1
            if line.find(':UAS-PCS,') > -1:
                f1 = FISH(line, ':UAS-PCS,', ',')
                if line.find('RCV') > -1:
                    location = AID + '@59'
                else:
                    location = AID + '@60'
                FLEX_ALL[location] = f1

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in list1:
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = FLEX_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_WAN(linesIn, TID, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' SUPPTP')
    lPoint.append('Supporting Termination Point')
    lTL1.append(' DIRECTION')
    lPoint.append('Direction')
    lTL1.append(' GFPRFI')
    lPoint.append('GFP RFI')
    lTL1.append(' FCS')
    lPoint.append('Frame Checksum')
    lTL1.append(' MAPPING')
    lPoint.append('Packet Mapping')
    lTL1.append(' SCRAMBLE')
    lPoint.append('Payload Scrambling')
    lTL1.append(' RATE')
    lPoint.append('SONET/SDH Basic rate')
    lTL1.append(' VCAT')
    lPoint.append('SONET/SDH concatenation')
    lTL1.append(' PREAMBLE')
    lPoint.append('Ethernet Preamble')
    lTL1.append(' FCSERRFRAMES')
    lPoint.append('FCS Errored Frames')
    lTL1.append(' GFPRTDELAY')
    lPoint.append('Round Trip Delay Status')
    lTL1.append(' OSTRAN')
    lPoint.append('Transparent Ordered Sets')
    lTL1.append(' GFPCMFUPI')
    lPoint.append('GFP CMF User Payload Indicator')
    lTL1.append(' CONDTYPE')
    lPoint.append('Conditioning Type')
    lTL1.append(' TRANSMITTEDUPI')
    lPoint.append('User Payload Indicator Transmitted (In HEX)')
    lTL1.append(' EXPECTEDUPI')
    lPoint.append('User Payload Indicator Expected (In HEX)')
    lTL1.append(' RECEIVEDUPI')
    lPoint.append('User Payload Indicator Received (In HEX)')
    lTL1.append(' LANFCS')
    lPoint.append('LAN Frame Checksum')
    lTL1.append('')
    lPoint.append('CLFI')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN')
    lPoint.append('15 Minutes (Near End)')
    lTL1.append(' 15-MIN ES-W,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 15-MIN SES-W,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 15-MIN UAS-W,RCV')
    lPoint.append('Unavailable Seconds Transmitted')
    lTL1.append(' 15-MIN INFRAMES-W,RCV')
    lPoint.append('WAN frames Received')
    lTL1.append(' 15-MIN INFRAMESERR-W,RCV')
    lPoint.append('WAN errored frames received')
    lTL1.append(' 15-MIN DFR-E,RCV')
    lPoint.append('Discarded ingress frames for any reason other than FCS errors')
    lTL1.append(' ')
    lPoint.append(' ')
    lTL1.append(' 1-UNT')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT ES-W,RCV')
    lPoint.append('Errored Seconds Received')
    lTL1.append(' 1-UNT SES-W,RCV')
    lPoint.append('Severe Errored Seconds Received')
    lTL1.append(' 1-UNT UAS-W,RCV')
    lPoint.append('Unavailable Seconds Received')
    lTL1.append(' 1-UNT INFRAMES-W,RCV')
    lPoint.append('WAN frames Received')
    lTL1.append(' 1-UNT INFRAMESERR-W,RCV')
    lPoint.append('WAN errored frames received')
    lTL1.append(' 1-UNT DFR-W,RCV')
    lPoint.append('Discarded ingress frames for any reason other than FCS errors')
    lAID = []
    WAN_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('WAN-') > -1 and line.find('MAPPING=') > -1:
            f1 = line.split(':')
            AID = f1[0].replace('   "', '')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            WAN_ALL[location] = AID
            states = f1[3].replace(',', ' & ')
            states = states.replace('"', '')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = states
            rest = f1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'SUPPTP=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'DIRECTION=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'GFPRFI=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'FCS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'MAPPING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'SCRAMBLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, ',RATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'VCAT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'PREAMBLE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'FCSERRFRAMES=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'GFPRTDELAY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'OSTRAN=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'GFPCMFUPI=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'CONDTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'TRANSMITTEDUPI=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'EXPECTEDUPI=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'RECEIVEDUPI=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'LANFCS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            WAN_ALL[location] = FISH(rest, 'CLFI=\\"', '\\"')
        if line.find('15-MIN') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-W,') > -1:
                WAN_ALL[AID + '@24'] = FISH(line, ':ES-W,', ',')
            if line.find(':SES-W,') > -1:
                WAN_ALL[AID + '@25'] = FISH(line, ':SES-W,', ',')
            if line.find(':UAS-W,') > -1:
                WAN_ALL[AID + '@26'] = FISH(line, ':UAS-W,', ',')
            if line.find(':INFRAMES-W,') > -1:
                WAN_ALL[AID + '@27'] = FISH(line, ':INFRAMES-W,', ',')
            if line.find(':INFRAMESERR-W,') > -1:
                WAN_ALL[AID + '@28'] = FISH(line, ':INFRAMESERR-W,', ',')
            if line.find(':DFR-W,') > -1:
                WAN_ALL[AID + '@29'] = FISH(line, ':DFR-W,', ',')
        if line.find('1-UNT') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find(':ES-W,') > -1:
                f1 = line.split(',')
                WAN_ALL[AID + '@31'] = f1[7] + ' : ' + f1[8]
                WAN_ALL[AID + '@32'] = FISH(line, ':ES-W,', ',')
            if line.find(':SES-W,') > -1:
                WAN_ALL[AID + '@33'] = FISH(line, ':SES-W,', ',')
            if line.find(':UAS-W,') > -1:
                WAN_ALL[AID + '@34'] = FISH(line, ':UAS-W,', ',')
            if line.find(':INFRAMES-W,') > -1:
                WAN_ALL[AID + '@35'] = FISH(line, ':INFRAMES-W,', ',')
            if line.find(':INFRAMESERR-W,') > -1:
                WAN_ALL[AID + '@36'] = FISH(line, ':INFRAMESERR-W,', ',')
            if line.find(':DFR-W,') > -1:
                WAN_ALL[AID + '@37'] = FISH(line, ':DFR-W,', ',')

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = WAN_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_OTM2(linesIn, TID, dGCC0, dGCC1, d_FACILITY_STATE__AID, F_ERROR, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' OCHTXPWR')
    lPoint.append('Provisioned Tx Power (dBm)')
    lTL1.append(' OCHTXACTPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lTL1.append(' OCHTXWVLNGTHPROV')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append(' PREFECSFTHLEV')
    lPoint.append('Pre-FEC Signal Fail Threshold (dBQ)')
    lTL1.append(' PREFECSFTHBER')
    lPoint.append('Pre-FEC Signal Fail Threshold (BER)')
    lTL1.append(' PREFECSDTHLEV')
    lPoint.append('Pre-FEC Signal Degrade Threshold (dBQ)')
    lTL1.append(' PREFECSDTHBER')
    lPoint.append('Pre-FEC Signal Degrade Threshold (BER)')
    lTL1.append(' OCHTXSBS')
    lPoint.append('Tx SBS Dither')
    lTL1.append(' OCHTXTRCONTSTATE')
    lPoint.append('Tx Compensation Mode')
    lTL1.append(' OTUTXFECFRMT')
    lPoint.append('Tx FEC Format')
    lTL1.append(' OCHTXAMFRMT')
    lPoint.append('Tx AM Format')
    lTL1.append(' OTURXFECFRMT')
    lPoint.append('Rx FEC Format')
    lTL1.append(' OTURATE')
    lPoint.append('Line Rate')
    lTL1.append(' PORTMODE')
    lPoint.append('Port Mode')
    lTL1.append('')
    lPoint.append('OSID')
    lTL1.append('')
    lPoint.append('CLFI')
    lTL1.append(' OCHTXMODE')
    lPoint.append('Tx Compensation Mode')
    lTL1.append(' OCHTXACTDISP')
    lPoint.append('Tx Actual Dispersion (ps/nm)')
    lTL1.append(' OCHRXACTDISP')
    lPoint.append('Rx Actual Dispersion (ps/nm)')
    lTL1.append('  OCHTXDISPMIN')
    lPoint.append('Min Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHTXDISPMAX')
    lPoint.append('Max Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHTXTRACE')
    lPoint.append('Trace Tx')
    lTL1.append(' OCHRXECHOTRACE')
    lPoint.append('Echoed Trace Rx')
    lTL1.append(' OCHTXASSOCFARENDRX')
    lPoint.append('Associated Far End Rx ID')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('Encryption')
    lTL1.append('ENCRTCM')
    lPoint.append('TCM level for encryption')
    lTL1.append('ENCRODU1')
    lPoint.append('Encryption Byte1')
    lTL1.append('ENCRODU2')
    lPoint.append('Encryption Byte2')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('Operator Trail Trace Indentifiers')
    lTL1.append(' OTUTXTTI')
    lPoint.append('Transmitted OTU TTI')
    lTL1.append(' OTURXEXPTTI')
    lPoint.append('Expected OTU TTI')
    lTL1.append(' RXINCTTI')
    lPoint.append('Incoming OTU TTI')
    lTL1.append('')
    lPoint.append('')
    iMon = 40
    lTL1.append('')
    lPoint.append('OTU PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN CV-OTU')
    lPoint.append('15 Minutes Code Violations')
    lTL1.append(' 15-MIN ES-OTU')
    lPoint.append('15 Minutes Error Seconds')
    lTL1.append(' 15-MIN SES-OTU')
    lPoint.append('15 Minutes Severe Error Seconds')
    lTL1.append(' 15-MIN FEC-OTU')
    lPoint.append('15 Minutes FEC Corrections')
    lTL1.append(' 15-MIN PRFBER-OTU')
    lPoint.append('15 Minutes Pre FEC BER & ( Q )')
    lTL1.append(' 15-MIN HCCS-OTU')
    lPoint.append('15 Minutes HCCS')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT CV-OTU')
    lPoint.append('Untimed Code Violations')
    lTL1.append(' 1-UNT ES-OTU')
    lPoint.append('Untimed Error Seconds')
    lTL1.append(' 1-UNT SES-OTU')
    lPoint.append('Untimed Severe Error Seconds')
    lTL1.append(' 1-UNT FEC-OTU')
    lPoint.append('Untimed FEC Corrections')
    lTL1.append(' 1-UNT PRFBER-OTU')
    lPoint.append('Untimed Pre FEC BER & ( Q )')
    lTL1.append(' 1-UNT HCCS-OTU')
    lPoint.append('Untimed HCCS')
    lTL1.append(' HCCSREF')
    lPoint.append('HCCSREF (dBQ) relative to post FEC BER=1E-15')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('ODU PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN CV-ODU')
    lPoint.append('15 Minutes Code Violations')
    lTL1.append(' 15-MIN ES-ODU')
    lPoint.append('15 Minutes Error Seconds')
    lTL1.append(' 15-MIN SES-ODU')
    lPoint.append('15 Minutes Severe Error Seconds')
    lTL1.append(' 15-MIN UAS-ODU')
    lPoint.append('15 Minutes Unavailable Seconds')
    lTL1.append(' 15-MIN FC-ODU')
    lPoint.append('15 Minutes FEC Corrections')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' 1-UNT OPT-OCH')
    lPoint.append('Untimed Tx Power')
    lTL1.append(' 1-UNT OPR-OCH')
    lPoint.append('Untimed Rx Power')
    lTL1.append(' 1-UNT CV-OTU')
    lPoint.append('Untimed Code Violations')
    lTL1.append(' 1-UNT ES-OTU')
    lPoint.append('Untimed Error Seconds')
    lTL1.append(' 1-UNT SES-OTU')
    lPoint.append('Untimed Severe Error Seconds')
    lTL1.append(' 1-UNT UAS-OTU')
    lPoint.append('Untimed Unavailable Seconds')
    lTL1.append(' 1-UNT FC-ODU')
    lPoint.append('Untimed FEC Corrections')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('GCC0 CIRCUITS')
    lPoint.append('Network Domain')
    lPoint.append('Carrier')
    lPoint.append('Operation Carrier')
    lPoint.append('Protocol')
    lPoint.append('FCS_Mode')
    lTL1.append('')
    lPoint.append('')
    lTL1.append('')
    lPoint.append('GCC1 CIRCUITS')
    lPoint.append('Network Domain')
    lPoint.append('Carrier')
    lPoint.append('Operation Carrier')
    lPoint.append('Protocol')
    lPoint.append('FCS_Mode')
    lAID = []
    OTU2_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('OTM2-') > -1 and line.find(',OTURATE=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            d_FACILITY_STATE__AID[AID] = states
            line = line.replace(':', ',')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'PREFECSFTHLEV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'PREFECSFTHBER=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'PREFECSDTHLEV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'PREFECSDTHBER=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXSBS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXTRCONTSTATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OTUTXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXAMFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OTURXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OTURATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'PORTMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OSID=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'CLFI=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHTXMODE=', ',')
            if f1 == 'ED':
                OTU2_ALL[location] = 'Extended Dispersion'
            else:
                OTU2_ALL[location] = 'Extended Power'
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHRXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXDISPMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXDISPMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHRXECHOTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OCHTXASSOCFARENDRX=\\"', '\\",')
            idx = idx + 3
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'ENCRTCM=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'ENCRODU1=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'ENCRODU2=', ',')
            idx = idx + 3
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = FISH(line, 'OTUTXTTI=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OTURXEXPTTI=\\"', '\\",')
            try:
                OTU2_ALL[location] = f1.decode('utf-8')
            except:
                OTU2_ALL[location] = 'string with invalid characters'

            continue
        if line.count(':') == 1 and line.find('\\""') > -1:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            RXINCTTI = FISH(line, ':\\"', '\\"')
            try:
                f1 = RXINCTTI.decode('utf-8')
            except:
                RXINCTTI = 'Received TTI had invalid characters'

            OTU2_ALL[AID + '@38'] = RXINCTTI
            try:
                OTURXEXPTTI = OTU2_ALL[AID + '@37']
                if RXINCTTI != OTURXEXPTTI:
                    F_ERROR.write(',' + AID + ',The provisioned Expected OTU Operator TTI (= ' + OTURXEXPTTI + ' ) is not equal to the Received OTU Operator TTI (= ' + RXINCTTI + ' )\n')
            except:
                pass

            continue
        if line.find('15-MIN') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            idx = iMon
            if line.find('OTM2:CV-OTU') > -1:
                OTU2_ALL[AID + '@41'] = f1[2]
            if line.find('OTM2:ES-OTU') > -1:
                OTU2_ALL[AID + '@42'] = f1[2]
            if line.find('OTM2:SES-OTU') > -1:
                OTU2_ALL[AID + '@43'] = f1[2]
            if line.find('OTM2:FEC-OTU') > -1:
                OTU2_ALL[AID + '@44'] = f1[2]
            if line.find('OTM2:PRFBER-OTU') > -1:
                s1 = f1[2]
                try:
                    l1 = float(s1)
                    if l1 > 0.5:
                        f2 = s1 + '       ( -infinity )'
                    elif l1 < 1e-30:
                        f2 = s1 + '       ( +infinity )'
                    else:
                        f2 = s1 + '       ( ' + BER2Q(s1) + ' )'
                except:
                    f2 = s1

                OTU2_ALL[AID + '@45'] = f2
            if line.find('OTM2:HCCS-OTU') > -1:
                OTU2_ALL[AID + '@46'] = f1[2]
            if line.find('OTM2:CV-ODU') > -1:
                OTU2_ALL[AID + '@57'] = f1[2]
            if line.find('OTM2:ES-ODU') > -1:
                OTU2_ALL[AID + '@58'] = f1[2]
            if line.find('OTM2:SES-ODU') > -1:
                OTU2_ALL[AID + '@59'] = f1[2]
            if line.find('OTM2:UAS-ODU') > -1:
                OTU2_ALL[AID + '@60'] = f1[2]
            if line.find('OTM2:FC-ODU') > -1:
                OTU2_ALL[AID + '@61'] = f1[2]
        if line.find('1-UNT') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find('OTM2:CV-OTU') > -1:
                OTU2_ALL[AID + '@47'] = f1[7] + ' : ' + f1[8]
                OTU2_ALL[AID + '@48'] = FISH(line, 'CV-OTU,', ',')
            if line.find('OTM2:ES-OTU') > -1:
                OTU2_ALL[AID + '@49'] = f1[2]
            if line.find('OTM2:SES-OTU') > -1:
                OTU2_ALL[AID + '@50'] = f1[2]
            if line.find('OTM2:FEC-OTU') > -1:
                OTU2_ALL[AID + '@51'] = f1[2]
            if line.find('OTM2:PRFBER-OTU') > -1:
                s1 = f1[2]
                try:
                    l1 = float(s1)
                    if l1 > 0.5:
                        f2 = s1 + '       ( -infinity )'
                    elif l1 < 1e-30:
                        f2 = s1 + '       ( +infinity )'
                    else:
                        f2 = s1 + '       ( ' + BER2Q(s1) + ' )'
                except:
                    f2 = s1

                OTU2_ALL[AID + '@52'] = f2
            if line.find('OTM2:HCCS-OTU') > -1:
                OTU2_ALL[AID + '@53'] = f1[2]
            if line.find('OTM2:OPT-OCH') > -1:
                OTU2_ALL[AID + '@62'] = f1[7] + ' : ' + f1[8]
                OTU2_ALL[AID + '@63'] = FISH(line, 'OTM2:OPT-OCH,', ',')
            if line.find('OTM2:OPR-OCH') > -1:
                OTU2_ALL[AID + '@64'] = FISH(line, 'OTM2:OPR-OCH,', ',')
            if line.find('OTM2:CV-ODU') > -1:
                OTU2_ALL[AID + '@65'] = FISH(line, 'CV-ODU,', ',')
            if line.find('OTM2:ES-ODU') > -1:
                OTU2_ALL[AID + '@66'] = f1[2]
            if line.find('OTM2:SES-ODU') > -1:
                OTU2_ALL[AID + '@67'] = f1[2]
            if line.find('OTM2:UAS-ODU') > -1:
                OTU2_ALL[AID + '@68'] = f1[2]
            if line.find('OTM2:FC-ODU') > -1:
                OTU2_ALL[AID + '@69'] = f1[2]
        if line.find('HCCSREF') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f1 = line.find('=') + 1
            f2 = line[f1:-3]
            OTU2_ALL[AID + '@54'] = f2
        if AID in dGCC0:
            f1 = dGCC0[AID]
            s1 = f1.split(',')
            idx = 72
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[4]
        if AID in dGCC1:
            f1 = dGCC1[AID]
            s1 = f1.split(',')
            idx = 79
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU2_ALL[location] = s1[4]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in list1:
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = OTU2_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_OTM3(linesIn, TID, dGCC, F_ERROR, F_NOW):
    lPoint = []
    lTL1 = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' OCHTXPWR')
    lPoint.append('Provisioned Tx Power (dBm)')
    lTL1.append(' OCHTXACTPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lTL1.append(' OCHTXMODE')
    lPoint.append('Tx Compensation Mode')
    lTL1.append(' OCHTXDISPPROV')
    lPoint.append('Tx Dispersion Provisioned (ps/nm)')
    lTL1.append(' OCHTXPREDISP')
    lPoint.append('Tx Actual Dispersion (ps/nm)')
    lTL1.append(' OCHRXPOSTDISP')
    lPoint.append('Rx Dispersion Post-compensation (ps/nm)')
    lTL1.append(' OCHTXWVLNGTHPROV')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append(' OCHRXACTDISP')
    lPoint.append('Total Rx Link Dispersion (ps/nm)')
    lTL1.append(' OCHTXACTDISP')
    lPoint.append('Total Tx Link Dispersion (ps/nm)')
    lTL1.append(' OCHRXACTPMD')
    lPoint.append('Estimated Instance of DGD (ps)')
    lTL1.append(' OCHMAXPMD')
    lPoint.append('Supported Mean DGD (ps)')
    lTL1.append(' OCHUNILATENCY')
    lPoint.append('Estimated Unidirectional Latency (microSec)')
    lTL1.append(' OCHESTLENGTH')
    lPoint.append('Estimated Fiber Length (km)')
    lTL1.append(' OCHREACHSPEC')
    lPoint.append('Reach Specification (km)')
    lTL1.append(' PREFECSFTHLEV')
    lPoint.append('Pre-FEC Signal Fail Threshold (dBQ)')
    lTL1.append(' PREFECSFTHBER')
    lPoint.append('Pre-FEC Signal Fail Threshold (BER)')
    lTL1.append(' PREFECSDTHLEV')
    lPoint.append('Pre-FEC Signal Degrade Threshold (dBQ)')
    lTL1.append(' PREFECSDTHBER')
    lPoint.append('Pre-FEC Signal Degrade Threshold (BER)')
    lTL1.append(' OCHTXTRACE')
    lPoint.append('Trace Tx')
    lTL1.append(' OCHRXECHOTRACE')
    lPoint.append('Echoed Trace Rx')
    lTL1.append(' OCHTXASSOCFARENDRX')
    lPoint.append('Associated Far End Rx ID')
    lTL1.append(' OTUTXFECFRMT')
    lPoint.append('Tx FEC Format')
    lTL1.append(' OTURXFECFRMT')
    lPoint.append('Rx FEC Format')
    lTL1.append(' TUNINGMODE')
    lPoint.append('Tuning Mode')
    lTL1.append(' OCHOPTIMIZEMODE')
    lPoint.append('Performance Optimization Mode')
    lTL1.append(' OTURATE')
    lPoint.append('Line Rate')
    lTL1.append(' PORTMODE')
    lPoint.append('Port Mode')
    lTL1.append(' OCHFRR')
    lPoint.append('Fast Receiver Recovery')
    lTL1.append(' OCHFRRCONFIG')
    lPoint.append('Network Configuration')
    lTL1.append(' OCHFRRPATH1DISP')
    lPoint.append('Link  Dispersion  Path 1 (ps/nm)')
    lTL1.append(' OCHFRRPATH2DISP')
    lPoint.append('Link  Dispersion  Path 2 (ps/nm)')
    lTL1.append('OSID')
    lPoint.append('Domain')
    lTL1.append('CLFI')
    lPoint.append('CLFI')
    lTL1.append(' OCHTXDISPMIN')
    lPoint.append('Min Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHTXDISPMAX')
    lPoint.append('Max Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHROTATION')
    lPoint.append('OCH Jones Rotation feature')
    lTL1.append(' OCHSPECTRALOCCUPANCY')
    lPoint.append('OCH Spectral Occupancy setting')
    lTL1.append(' OCHDIFFERENTIALENCODING')
    lPoint.append('OCH Differential Encoding')
    lTL1.append(' OCHTXCHIRP')
    lPoint.append('Tx Chirp')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('Operator Trail Trace Indentifiers')
    lTL1.append(' OTUTXTTI')
    lPoint.append('Transmitted OTU TTI')
    lTL1.append(' OTURXEXPTTI')
    lPoint.append('Expected OTU TTI')
    lTL1.append(' RXINCTTI')
    lPoint.append('Incoming OTU TTI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN PRFBER-OTU')
    lPoint.append('15 Minutes Pre FEC BER')
    lTL1.append(' 15-MIN HCCS-OTU')
    lPoint.append('15 Minutes HCCS')
    lTL1.append(' 15-MIN QMIN-OTU')
    lPoint.append('15 Minutes QMIN (dBQ)')
    lTL1.append(' 15-MIN QMAX-OTU')
    lPoint.append('15 Minutes QMAX (dBQ)')
    lTL1.append(' 15-MIN QAVG-OTU')
    lPoint.append('15 Minutes QAVG (dBQ)')
    lTL1.append(' 15-MIN QSTDEV-OTU')
    lPoint.append('15 Minutes QSTDEV')
    lTL1.append(' 1-UNT PRFBER-OTU')
    lPoint.append('Untimed Pre FEC BER')
    lTL1.append(' 1-UNT HCCS-OTU')
    lPoint.append('Untimed HCCS')
    lTL1.append(' 1-UNT QMIN-OTU')
    lPoint.append('Untimed QMIN-OTU (dBQ)')
    lTL1.append(' 1-UNT QMAX-OTU')
    lPoint.append('Untimed QMAX (dBQ)')
    lTL1.append(' 1-UNT QAVG-OTU')
    lPoint.append('Untimed QAVG (dBQ)')
    lTL1.append(' 1-UNT QSTDEV-OTU')
    lPoint.append('Untimed QSTDEV')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' HCCSREF')
    lPoint.append('HCCSREF (dBQ) relative to post FEC BER=1E-15')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('GCC CIRCUITS')
    lTL1.append('')
    lPoint.append('Network Domain')
    lTL1.append('')
    lPoint.append('Carrier')
    lTL1.append('')
    lPoint.append('Operation Carrier')
    lTL1.append('')
    lPoint.append('Protocol')
    lTL1.append('')
    lPoint.append('FCS_Mode')
    lAID = []
    OTU3_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find('"OTM3-') > -1 and (line.find(',OCHTXTRACE=') > -1 or line.find(',OTURATE=') > -1):
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXDISPPROV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXPREDISP=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXPOSTDISP=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXACTDISP=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXACTDISP=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXACTPMD=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHMAXPMD=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHUNILATENCY=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHESTLENGTH=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHREACHSPEC=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'PREFECSFTHLEV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'PREFECSFTHBER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'PREFECSDTHLEV=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'PREFECSDTHBER=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXTRACE=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHRXECHOTRACE=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXASSOCFARENDRX=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OTUTXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OTURXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'TUNINGMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHOPTIMIZEMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OTURATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'PORTMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHFRR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHFRRCONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHFRRPATH1DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHFRRPATH2DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OSID=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'CLFI=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXDISPMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXDISPMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHROTATION=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHSPECTRALOCCUPANCY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHDIFFERENTIALENCODING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OCHTXCHIRP=', ',')
            idx = idx + 3
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OTUTXTTI=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = FISH(line, 'OTURXEXPTTI=\\"', '\\",')
        if line.count(':') == 1 and line.find('\\""') > -1:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            RXINCTTI = FISH(line, ':\\"', '\\"')
            try:
                f1 = RXINCTTI.decode('utf-8')
            except:
                RXINCTTI = 'Received TTI had invalid characters'

            OTU3_ALL[AID + '@48'] = RXINCTTI
            OTURXEXPTTI = OTU3_ALL[AID + '@47']
            if RXINCTTI != OTURXEXPTTI:
                F_ERROR.write(',' + AID + ',The provisioned Expected OTU Operator TTI (= ' + OTURXEXPTTI + ' ) is not equal to the Received OTU Operator TTI (= ' + RXINCTTI + ' )\n')
            continue
        if line.find('15-MIN') > -1:
            if line.find('OTM3:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 51
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find('OTM3:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 52
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 53
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 54
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 55
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 56
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
        if line.find('1-UNT') > -1:
            if line.find('OTM3:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 57
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find('OTM3:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 58
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
                idx = 63
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[7] + ' : ' + f1[8]
            if line.find(',OTM3:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 60
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 61
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 62
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
            if line.find(',OTM3:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = 64
                location = AID + '@' + str(idx)
                OTU3_ALL[location] = f1[2]
        if line.find(AID) > -1 and line.find('HCCSREF') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f1 = line.find('=') + 1
            f2 = line[f1:-3]
            idx = 64
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = f2
        if AID in dGCC:
            f1 = dGCC[AID]
            s1 = f1.split(',')
            idx = 67
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU3_ALL[location] = s1[4]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = OTU3_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_RTRV_OTM4(FAC, linesIn, TID, dCPPEC, dGCC0, dGCC1, F_ERROR, F_NOW):
    lTL1 = []
    lPoint = []
    lTL1.append('TL1 Parameter')
    lPoint.append('TID = ' + TID)
    lTL1.append('')
    lPoint.append('Primary & Secondary State')
    lTL1.append(' OCHTXPWR')
    lPoint.append('Provisioned Tx Power (dBm)')
    lTL1.append(' OCHTXACTPWR')
    lPoint.append('Tx Actual Power (dBm)')
    lTL1.append(' OCHRXMAXPWR')
    lPoint.append('Max Rx Power (dBm)')
    lTL1.append(' OCHRXACTPWR')
    lPoint.append('Rx Actual Power (dBm)')
    lTL1.append(' OCHRXMINPWR')
    lPoint.append('Min Rx Power (dBm)')
    lTL1.append(' OCHTXMODE ')
    lPoint.append('Tx Dispersion Compensation Mode')
    lTL1.append(' OCHTXDISPPROV ')
    lPoint.append('Tx Dispersion Provisioned (ps/nm)')
    lTL1.append(' OCHTXPREDISP ')
    lPoint.append('Tx Actual Dispersion (ps/nm)')
    lTL1.append(' OCHRXPOSTDISP ')
    lPoint.append('Rx Dispersion Post-compensation (ps/nm)')
    lTL1.append(' OCHTXWVLNGTHSPACING')
    lPoint.append('Tx Channel Spacing (THz)')
    lTL1.append(' OCHTXWVLNGTHPROV ')
    lPoint.append('Tx Wavelength (nm)')
    lTL1.append(' OCHTXFREQMAX ')
    lPoint.append('Tx Maximum Frequency (THz)')
    lTL1.append(' OCHTXFREQPROV ')
    lPoint.append('Tx Frequency (THz)')
    lTL1.append(' OCHTXFREQMIN ')
    lPoint.append('Tx Minimum Frequency (THz)')
    lTL1.append(' OCHRXACTDISP ')
    lPoint.append('Total Rx Link Dispersion (ps/nm)')
    lTL1.append(' OCHTXACTDISP ')
    lPoint.append('Total Tx Link Dispersion (ps/nm)')
    lTL1.append(' OCHRXACTPMD ')
    lPoint.append('Estimated Instance Of DGD (ps)')
    lTL1.append(' OCHMAXPMD')
    lPoint.append('Supported Max DGD (ps)')
    lTL1.append(' OCHUNILATENCY')
    lPoint.append('Estimated Unidirectional Latency (microSec)')
    lTL1.append(' OCHESTLENGTH')
    lPoint.append('Estimated Fiber Length (km)')
    lTL1.append(' OCHREACHSPEC')
    lPoint.append('Reach Specification (km)')
    lTL1.append(' PREFECSFTHLEV')
    lPoint.append('Pre-FEC Signal Fail Threshold (dBQ)')
    lTL1.append(' PREFECSFTHBER')
    lPoint.append('Pre-FEC Signal Fail Threshold (BER)')
    lTL1.append(' PREFECSDTHLEV')
    lPoint.append('Pre-FEC Signal Degrade Threshold (dBQ)')
    lTL1.append(' PREFECSDTHBER')
    lPoint.append('Pre-FEC Signal Degrade Threshold (BER)')
    lTL1.append(' OCHTXTRACE')
    lPoint.append('Transmitted TX Identifier')
    lTL1.append(' OCHRXECHOTRACE')
    lPoint.append('Echoed Trace Rx')
    lTL1.append(' OCHTXASSOCFARENDRX')
    lPoint.append('Associated Far End Rx AID')
    lTL1.append(' OTUTXFECFRMT')
    lPoint.append('Tx FEC Format')
    lTL1.append(' OTURXFECFRMT')
    lPoint.append('Rx FEC Format')
    lTL1.append(' OCHOPTIMIZEMODE')
    lPoint.append('Performance Optimization Mode')
    lTL1.append(' OTURATE')
    lPoint.append('Line Rate')
    lTL1.append(' PORTMODE')
    lPoint.append('Port Mode')
    lTL1.append(' OCHFRR')
    lPoint.append('Fast Receiver Recovery')
    lTL1.append(' OCHFRRCONFIG')
    lPoint.append('Network Configuration')
    lTL1.append(' OCHFRRPATH1DISP')
    lPoint.append('Link  Dispersion  Path 1 (ps/nm)')
    lTL1.append(' OCHFRRPATH2DISP')
    lPoint.append('Link  Dispersion  Path 2 (ps/nm)')
    lTL1.append(' OSID')
    lPoint.append('Domain')
    lTL1.append(' CLFI')
    lPoint.append('CLFI')
    lTL1.append(' OCHTXDISPMIN')
    lPoint.append('Min Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHTXDISPMAX')
    lPoint.append('Max Tx Dispersion Value (ps/nm)')
    lTL1.append(' OCHROTATION')
    lPoint.append('OCH Jones Rotation feature')
    lTL1.append(' OCHSPECTRALOCCUPANCY')
    lPoint.append('OCH Spectral Occupancy setting')
    lTL1.append(' OCHDIFFERENTIALENCODING')
    lPoint.append('OCH Differential Encoding')
    lTL1.append(' OCHTXCHIRP')
    lPoint.append('Tx Chirp')
    lTL1.append(' OCHPWRBALOFFSET')
    lPoint.append('OCH recovery mode')
    lTL1.append(' OCHENMPROV')
    lPoint.append('Provisioned Enhanced Non-linear Mitigation mode')
    lTL1.append(' TUNINGMODE')
    lPoint.append('Tuning Mode')
    lTL1.append(' OCHCCDA')
    lPoint.append('TX Channel Contention Detection and Avoidance')
    lTL1.append(' SPLIMANAGED')
    lPoint.append('SPLI Managed')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('Operator Trail Trace Indentifiers')
    lTL1.append(' OTUTXTTI')
    lPoint.append('Transmitted OTU TTI')
    lTL1.append(' OTURXEXPTTI')
    lPoint.append('Expected OTU TTI')
    lTL1.append(' RXINCTTI')
    lPoint.append('Incoming OTU TTI')
    lTL1.append('')
    lPoint.append(' ')
    lTL1.append('')
    lPoint.append('PERFORMANCE MONITORING')
    lTL1.append(' 15-MIN PRFBER-OTU')
    lPoint.append('15 Minutes Pre FEC BER & ( Q )')
    lTL1.append(' 15-MIN HCCS-OTU')
    lPoint.append('15 Minutes HCCS')
    lTL1.append(' 15-MIN QMIN-OTU')
    lPoint.append('15 Minutes QMIN (dBQ)')
    lTL1.append(' 15-MIN QMAX-OTU')
    lPoint.append('15 Minutes QMAX (dBQ)')
    lTL1.append(' 15-MIN QAVG-OTU')
    lPoint.append('15 Minutes QAVG (dBQ)')
    lTL1.append(' 15-MIN QSTDEV-OTU')
    lPoint.append('15 Minutes QSTDEV')
    lTL1.append(' 1-UNT PRFBER-OTU')
    lPoint.append('Untimed Pre FEC BER & ( Q )')
    lTL1.append(' 1-UNT HCCS-OTU')
    lPoint.append('Untimed HCCS')
    lTL1.append(' 1-UNT QMIN-OTU')
    lPoint.append('Untimed QMIN-OTU (dBQ)')
    lTL1.append(' 1-UNT QMAX-OTU')
    lPoint.append('Untimed QMAX (dBQ)')
    lTL1.append(' 1-UNT QAVG-OTU')
    lPoint.append('Untimed QAVG (dBQ)')
    lTL1.append(' 1-UNT QSTDEV-OTU')
    lPoint.append('Untimed QSTDEV')
    lTL1.append(' ')
    lPoint.append('Untimed counter reset [M-D : H-M]')
    lTL1.append(' HCCSREF')
    lPoint.append('HCCSREF (dBQ) relative to post FEC BER=1E-15')
    lTL1.append(' ')
    lPoint.append(' ')
    lTL1.append(' ')
    lPoint.append('GCC0 CIRCUITS')
    lTL1.append(' ')
    lPoint.append('Network Domain')
    lTL1.append(' ')
    lPoint.append('Carrier')
    lTL1.append(' ')
    lPoint.append('Operation Carrier')
    lTL1.append(' ')
    lPoint.append('Protocol')
    lTL1.append(' ')
    lPoint.append('FCS_Mode')
    lTL1.append(' ')
    lPoint.append(' ')
    lTL1.append(' ')
    lPoint.append('GCC1 CIRCUITS')
    lTL1.append(' ')
    lPoint.append('Network Domain')
    lTL1.append(' ')
    lPoint.append('Carrier')
    lTL1.append(' ')
    lPoint.append('Operation Carrier')
    lTL1.append(' ')
    lPoint.append('Protocol')
    lTL1.append(' ')
    lPoint.append('FCS_Mode')
    iHCCS = 72
    iTTI = 54
    iMON15 = iTTI + 5
    iMON24 = iMON15 + 6
    iGCC0 = iHCCS + 3
    iGCC1 = iGCC0 + 7
    lAID = []
    OTU4_ALL = {}
    AID = '?'
    for line in linesIn:
        if line.find(' "' + FAC + '-') > -1 and (line.find(',OCHTXTRACE=') > -1 or line.find(',OTURATE=') > -1):
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            states = line[l1:-2]
            states = states.replace(',', ' & ')
            lAID.append(AID)
            idx = 0
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = states
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXMAXPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXACTPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXMINPWR=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXDISPPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXPREDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXPOSTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXWVLNGTHSPACING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXWVLNGTHPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXFREQMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXFREQPROV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXFREQMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXACTDISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXACTPMD=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHMAXPMD=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHUNILATENCY=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHESTLENGTH=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHREACHSPEC=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'PREFECSFTHLEV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'PREFECSFTHBER=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'PREFECSDTHLEV=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'PREFECSDTHBER=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHRXECHOTRACE=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXASSOCFARENDRX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OTUTXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OTURXFECFRMT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHOPTIMIZEMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OTURATE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'PORTMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHFRR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHFRRCONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHFRRPATH1DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHFRRPATH2DISP=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OSID=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'CLFI=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXDISPMIN=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHTXDISPMAX=\\"', '\\",')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHROTATION=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHSPECTRALOCCUPANCY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = FISH(line, 'OCHDIFFERENTIALENCODING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHTXCHIRP=', ',')
            OTU4_ALL[location] = FISH(line, 'OCHTXCHIRP=', ',')
            s1 = AID[:-1]
            try:
                PEC = dCPPEC[s1]
            except:
                PEC = ''

            if PEC.find('NTK539U') > -1 or PEC.find('NTK539B') > -1:
                if f1 != 'POSITIVE':
                    F_ERROR.write(',' + AID + ',' + PEC + ' with chirp set to ' + f1 + '\n')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHPWRBALOFFSET=\\"', '\\",')
            if f1 == '0':
                f2 = 'FW'
            elif f1 == '1':
                f2 = 'SW Override'
            elif f1 == '2':
                f2 = 'QPSK Nonlinear1'
            elif f1 == '3':
                f2 = 'QPSK Nonlinear2'
            elif f1 == '4':
                f2 = 'BPSK Nonlinear1'
            elif f1 == 5:
                f2 = 'BPSK Nonlinear2'
            else:
                f2 = 'NA'
            OTU4_ALL[location] = f2
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHENMPROV=', ',')
            if f1 == 'MODE1':
                OTU4_ALL[location] = 'STANDARD'
            elif f1 == 'MODE2':
                OTU4_ALL[location] = 'ENHANCED'
            else:
                OTU4_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'TUNINGMODE=', ',')
            OTU4_ALL[location] = f1
            if f1 != 'NORMAL' and f1 != 'ACCELERATED' and f1 != '':
                F_ERROR.write(',' + AID + ',Does not have Tuning Mode = Performance Optimized or Accelerated\n')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'OCHCCDA=', ',')
            OTU4_ALL[location] = f1
            if f1 != 'ON' and f1 != '':
                F_ERROR.write(',' + AID + ',Does not have Channel Contention Detection and Avoidance = ON\n')
            idx = idx + 1
            location = AID + '@' + str(idx)
            s1 = line.replace(':', ',')
            f1 = FISH(s1, 'SPLIMANAGED=', ',')
            OTU4_ALL[location] = f1
            if f1 == 'YES':
                F_ERROR.write(',' + AID + ',Is not SPLI managed\n')
            location = AID + '@' + str(iTTI)
            OTU4_ALL[location] = FISH(line, 'OTUTXTTI=\\"', '\\",')
            location = AID + '@' + str(iTTI + 1)
            OTURXEXPTTI = FISH(line, 'OTURXEXPTTI=\\"', '\\",')
            OTU4_ALL[location] = OTURXEXPTTI
        if line.count(':') == 1 and line.find(FAC) > -1 and line.find('\\""') > -1 and line.find(';') < 0:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            RXINCTTI = FISH(line, ':\\"', '\\"')
            try:
                f1 = RXINCTTI.decode('utf-8')
            except:
                RXINCTTI = 'Received TTI had invalid characters'

            idx = idx + 1
            OTU4_ALL[AID + '@' + str(iTTI + 2)] = RXINCTTI
            if RXINCTTI != OTURXEXPTTI:
                F_ERROR.write(',' + AID + ',The provisioned Expected OTU Operator TTI (= ' + OTURXEXPTTI + ' ) is not equal to the Received OTU Operator TTI (= ' + RXINCTTI + ' )\n')
            continue
        if line.find('15-MIN') > -1:
            if line.find('OTM:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = iMON15
                location = AID + '@' + str(iMON15)
                s1 = f1[2]
                try:
                    l1 = float(s1)
                    if l1 > 0.5:
                        f2 = s1 + '       ( -infinity )'
                    elif l1 < 1e-30:
                        f2 = s1 + '       ( +infinity )'
                    else:
                        f2 = s1 + '       ( ' + BER2Q(s1) + ' )'
                except:
                    f2 = s1

                OTU4_ALL[location] = f2
            if line.find('OTM:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON15 + 1)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON15 + 2)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON15 + 3)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON15 + 4)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON15 + 5)
                OTU4_ALL[location] = f1[2]
        if line.find('1-UNT') > -1:
            if line.find('OTM:PRFBER-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON24)
                s1 = f1[2]
                try:
                    l1 = float(s1)
                    if l1 > 0.5:
                        f2 = s1 + '       ( -infinity )'
                    elif l1 < 1e-30:
                        f2 = s1 + '       ( +infinity )'
                    else:
                        f2 = s1 + '       ( ' + BER2Q(s1) + ' )'
                except:
                    f2 = s1

                OTU4_ALL[location] = f2
            if line.find('OTM:HCCS-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON24 + 1)
                OTU4_ALL[location] = f1[2]
                location = AID + '@' + str(iMON24 + 6)
                OTU4_ALL[location] = f1[7] + ' : ' + f1[8]
            if line.find(',OTM:QMIN-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON24 + 2)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QMAX-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON24 + 3)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QAVG-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                location = AID + '@' + str(iMON24 + 4)
                OTU4_ALL[location] = f1[2]
            if line.find(',OTM:QSTDEV-OTU') > -1:
                f1 = line.split(',')
                f2 = f1[0]
                AID = f2.replace('   "', '')
                idx = idx + 1
                location = AID + '@' + str(iMON24 + 5)
                OTU4_ALL[location] = f1[2]
        if line.find('HCCSREF') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f1 = line.find('=') + 1
            f2 = line[f1:-3]
            location = AID + '@' + str(iHCCS)
            OTU4_ALL[location] = f2
        if AID in dGCC0:
            f1 = dGCC0[AID]
            s1 = f1.split(',')
            idx = iGCC0
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[4]
        if AID in dGCC1:
            f1 = dGCC1[AID]
            s1 = f1.split(',')
            idx = iGCC1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[0]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[1]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[2]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[3]
            idx = idx + 1
            location = AID + '@' + str(idx)
            OTU4_ALL[location] = s1[4]

    CCC = []
    CCC.append(lTL1)
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(0, nPoint))
    list1 = list(set(lAID))
    for aid in sorted(list1):
        ccc = []
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = OTU4_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PARSE_PROTECTION_OTM(linesIn, TID, d_EQUIPMENT_STATE__AID, d_FACILITY_STATE__AID, F_NOW):
    F_NOW.write('TID,Shelf,Protection Pair,AID,Scheme,Switch Mode,Revertive,Route Diversity,Remote Standard,Wait-to-Restore Time,Member,Switch,Reason for auto switch,Equipment State,Facility State,\n')
    d_PROTECT = {}
    for line in linesIn:
        if line.find('OTM') > -1 and line.find('PS=') > -1:
            line = line.replace(' "', '')
            f1 = line.split(':')
            f2 = f1[0]
            f3 = f2.split(',')
            WORK = f3[0].strip()
            PROTECT = f3[1]
            f3 = WORK.split('-')
            tid_shelf = TID + ',' + f3[1] + ',' + WORK + ' & ' + PROTECT
            line = line.replace('"\r', ',')
            s1 = FISH(line, ',PS=', ',')
            f1 = FISH(line, 'PSDIRN=', ',')
            if f1.find('BI') > -1:
                f1 = 'Bidirectional'
            else:
                f1 = 'Unidirectional'
            f2 = FISH(line, 'RVRTV=', ',')
            f3 = FISH(line, ',RD=', ',')
            f4 = FISH(line, 'REMSTANDARD=', ',')
            f5 = FISH(line, 'WR=', ',')
            s1 = s1 + ',' + f1 + ',' + f2 + ',' + f3 + ',' + f4 + ',' + f5
            d_PROTECT[WORK] = tid_shelf + ',' + WORK + ',' + s1 + ',' + 'Working'
            d_PROTECT[PROTECT] = tid_shelf + ',' + PROTECT + ',' + s1 + ',' + 'Protect'
            continue
        if line.find('OTM') > -1 and line.find('SWSTATUS') > -1:
            line = line.replace(' "', '')
            f1 = line.split(':')
            AID = f1[0].strip()
            line = line.replace('"\r', ',')
            f1 = FISH(line, 'SWSTATUS=', ',')
            f2 = FISH(line, 'SWEND=', ',')
            s1 = f1 + ' & ' + f2
            f2 = FISH(line, 'SWREASON=', ',')
            if f2 == 'SIGOK':
                f3 = 'Signal OK'
            elif f2 == 'SF':
                f3 = 'Autonomous switch due to Signal Fail'
            elif f2 == 'SD':
                f3 = 'Autonomous switch due to Signal Degrade'
            elif f2 == 'EBER':
                f3 = 'Autonomous switch due to Excessive BER error'
            elif f2 == 'EQPFL':
                f3 = 'Autonomous switch due to Equipment Fail'
            elif f2 == 'FACOOS':
                f3 = 'Autonomous switch due to Facility OOS'
            elif f2 == 'EQPOOS':
                f3 = 'Autonomous switch due to Equipment OOS'
            elif f2 == 'WTR':
                f3 = 'Autonomous switch active; Wait to retore not expired'
            else:
                f3 = ''
            s1 = s1 + ',' + f2 + ' ( ' + f3 + ' )'
            try:
                f1 = d_PROTECT[AID]
            except:
                f1 = ',,,,,'

            d_PROTECT[AID] = f1 + ',' + s1

    for key in sorted(d_PROTECT):
        f1 = key.find('-') + 1
        f2 = len(key)
        f3 = key[f1:f2]
        f3 = key[f1:f2]
        try:
            f1 = d_EQUIPMENT_STATE__AID[f3]
        except:
            f1 = '-'

        try:
            f2 = d_FACILITY_STATE__AID[key]
        except:
            f2 = '-'

        s1 = f1 + ',' + f2
        F_NOW.write(d_PROTECT[key] + ',' + s1 + '\n')

    return None


def LOGIN_SSH():
    global chan_6500
    global ssh_6500
    if not _ensure_paramiko():
        return 'NO'
    nbytes = 16384
    logging.getLogger('paramiko.transport').addHandler(logging.NullHandler())
    wasConnected = 'YES'
    s1 = str(PORT)
    if s1.find('localhost') > -1:
        sshHOST = 'localhost'
        f1 = PORT.split(' ')
        sshPORT = f1[1]
    else:
        sshHOST = HOST
        sshPORT = PORT
    sshTunnel = sshHOST + ' @ port ' + str(sshPORT)
    F_DBG.write('\nHOST = %s \nUsername = %s \nMethod = %s \nPort = %s \nComment = %s\n' % (HOST,
    USER,
    METHOD,
    PORT,
    COMMENT))
    print ('Trying to login via SSH to %s...' % sshTunnel)
    try:
        f1 = int(sshPORT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect((sshHOST, f1))
        ssh_6500 = paramiko.Transport(sock)
        auth_mode = 'none'
        print('[SSH] Establishing transport (auth_none flow)...')
        F_DBG.write('[SSH] Establishing transport (auth_none flow)...\n')
        ssh_6500.connect()
    except Exception as err:
        err = str(err)
        wasConnected = 'Verify HOST IP, username/password, and that the primary shelf supports SSH'
        if err.find('target machine actively refused it') > -1:
            wasConnected = 'Wrong username/password, or member shelf, or SSH protocol disabled'
        elif err.find('because connected host has failed to respond') > -1:
            wasConnected = 'IP did not respond'
        WARNING('Login Issue', wasConnected)
        F_DBG.write('%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (err, HOST))
        return wasConnected

    if auth_mode == 'none':
        try:
            print(f'[SSH] Authenticating with auth_none...')
            F_DBG.write(f'[SSH] Authenticating with auth_none...\n')
            ssh_6500.auth_none(USER)
            print(f'[SSH] Auth successful (auth_none).')
            F_DBG.write(f'[SSH] Auth successful (auth_none).\n')
        except Exception as err:
            err = str(err)
            print(f'[SSH] Auth failed: {err}')
            F_DBG.write(f'[SSH] Auth failed: {err}\n')
            ssh_6500.close()
            wasConnected = '\tVerify Username and/or password'
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (wasConnected, HOST))
            return wasConnected
    else:
        print(f'[SSH] Auth successful (password).')
        F_DBG.write(f'[SSH] Auth successful (password).\n')

    try:
        print(f'[SSH] Opening session channel...')
        F_DBG.write(f'[SSH] Opening session channel...\n')
        chan_6500 = ssh_6500.open_channel('session')
        print(f'[SSH] Channel opened.')
        F_DBG.write(f'[SSH] Channel opened.\n')
    except Exception as err:
        err = str(err)
        print(f'[SSH] Channel open failed: {err}')
        F_DBG.write(f'[SSH] Channel open failed: {err}\n')
        ssh_6500.close()
        wasConnected = '\tSHH session established but no response from the shelf'
        WARNING('Login Issue', wasConnected)
        F_DBG.write('%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (wasConnected, HOST))
        return wasConnected

    f1 = ''
    print(f'[SSH] Invoking shell...')
    F_DBG.write(f'[SSH] Invoking shell...\n')
    chan_6500.invoke_shell()
    chan_6500.settimeout(1.0)
    print(f'[SSH] Shell invoked. Sleeping 2s...')
    F_DBG.write(f'[SSH] Shell invoked. Sleeping 2s...\n')
    time.sleep(2)
    print(f'[SSH] Reading banner (waiting for TL1 prompt variants: "< ", "<", or PROMPT)...')
    F_DBG.write(f'[SSH] Reading banner (waiting for TL1 prompt variants)...\n')
    t = 0.0
    chunk_count = 0
    sent_shell_user = False
    sent_shell_pass = False
    login_retry_count = 0
    last_probe = -5.0
    while not _has_ssh_banner_prompt(f1):
        time.sleep(0.2)
        if chan_6500.recv_ready():
            chunk = _recv_text(chan_6500, nbytes)
            if chunk:
                chunk_count += 1
                f1 += chunk
                print(f'[SSH] Banner chunk {chunk_count} received ({len(chunk)} bytes, total {len(f1)} bytes)')
                print(f'[SSH] Banner chunk {chunk_count} preview: {repr(chunk[:120])}')
                F_DBG.write(f'[SSH] Banner chunk {chunk_count} ({len(chunk)} bytes) preview={repr(chunk[:120])}\n')
                chunk_lower = chunk.lower()
                if sent_shell_user and not sent_shell_pass and 'password' in chunk_lower:
                    print('[SSH] Interactive password prompt detected; sending password...')
                    F_DBG.write('[SSH] Interactive password prompt detected; sending password\n')
                    chan_6500.send(PASS + '\r')
                    sent_shell_pass = True
                elif not sent_shell_user and 'login:' in chunk_lower:
                    print('[SSH] Interactive login prompt detected; sending username...')
                    F_DBG.write('[SSH] Interactive login prompt detected; sending username\n')
                    chan_6500.send(USER + '\r')
                    sent_shell_user = True
                if sent_shell_pass and 'login incorrect' in chunk_lower:
                    if login_retry_count < 1:
                        login_retry_count += 1
                        sent_shell_user = False
                        sent_shell_pass = False
                        print('[SSH] Login incorrect received; retrying interactive login once...')
                        F_DBG.write('[SSH] Login incorrect received; retrying interactive login once\n')
                    else:
                        wasConnected = 'Interactive login rejected credentials (Login incorrect); verify shell credentials or use TL1 service endpoint/method'
                        WARNING('Login Issue', wasConnected)
                        F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (f1, wasConnected, HOST))
                        return wasConnected
        elif t - last_probe >= 5.0:
            if not sent_shell_user:
                chan_6500.send('\r\n')
                last_probe = t
                print(f'[SSH] No banner data yet at {t:.1f}s; sent newline probe')
                F_DBG.write(f'[SSH] No banner data at {t:.1f}s; sent newline probe\n')
        if t > 60:
            print(f'[SSH] Banner timeout after {t}s. Received {len(f1)} bytes, {chunk_count} chunks')
            wasConnected = 'Did not receive a valid banner'
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s\n%s \n[SSH] Timeout after %fs, %d chunks\n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (f1, wasConnected, t, chunk_count, HOST))
            return wasConnected
        t = t + 0.2

    f1 += _recv_text(chan_6500, nbytes)
    global SSH_SHELL_PROMPT
    SSH_SHELL_PROMPT = ''

    _banner_tail = f1[-120:] if len(f1) >= 120 else f1
    _in_cli_shell = '#' in _banner_tail and '< ' not in _banner_tail
    if _in_cli_shell:
        _banner_last_line = f1.split('\n')[-1].replace('\r', '').lstrip()
        print(f'[SSH] CLI shell detected (prompt: {repr(_banner_last_line)}); switching to TL1 mode...')
        F_DBG.write(f'[SSH] CLI shell detected; sending "tl1" to enter TL1 mode\n')
        chan_6500.send('tl1\r')
        _tl1_switch_buf = ''
        _tl1_switch_t = 0.0
        while _tl1_switch_t < 30.0:
            time.sleep(0.2)
            if chan_6500.recv_ready():
                _chunk = _recv_text(chan_6500, nbytes)
                if _chunk:
                    _tl1_switch_buf += _chunk
                    print(f'[SSH] TL1 switchover chunk: {repr(_chunk[:80])}')
                    F_DBG.write(f'[SSH] TL1 switchover chunk: {repr(_chunk[:80])}\n')
                    if '< ' in _tl1_switch_buf[-40:] or _tl1_switch_buf.rstrip().endswith('<'):
                        print('[SSH] TL1 prompt received; now in TL1 mode.')
                        F_DBG.write('[SSH] TL1 prompt received.\n')
                        f1 += _tl1_switch_buf
                        break
            _tl1_switch_t += 0.2
        else:
            wasConnected = 'Could not switch to TL1 mode after CLI login (no TL1 prompt within 30s)'
            print(f'[SSH] {wasConnected}')
            WARNING('Login Issue', wasConnected)
            F_DBG.write(f'{wasConnected}\n')
            chan_6500.close()
            ssh_6500.close()
            return wasConnected
    print(f1)
    if PASS == '?':
        LoginCommand = 'ACT-USER::"' + USER + '":CHRES:::DOMAIN=CHALLENGE;'
        captured_text, f1 = LOGIN_CHALLENGE_RESPONSE(LoginCommand)
        if captured_text.find('LOG DENY') > -1 or f1 == 1:
            wasConnected = '\nChallenge/response login failed'
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
            time.sleep(0.3)
            chan_6500.close()
            ssh_6500.close()
            return 1
    else:
        LoginCommand = 'ACT-USER::"' + USER + '":LOG::"' + PASS + '":;'
        print(f'[SSH] Sending ACT-USER command...')
        F_DBG.write(f'[SSH] Sending ACT-USER command...\\n')
        f1 = TL1_In_Out(LoginCommand)
        print(f'[SSH] ACT-USER response received ({len(f1)} bytes)')
        F_DBG.write(f'[SSH] ACT-USER response ({len(f1)} bytes)\\n')
        print (f1)
        if f1.find('M  LOG DENY') > -1:
            if f1.find('Login from Primary Shelf') > -1:
                wasConnected = 'This IP does not correspond to a primary shelf'
            else:
                wasConnected = 'Incorrect login command, verify username & password'
                F_DBG.write('\n\t Failed ACT-USER = ' + LoginCommand + '\n')
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (f1, wasConnected, HOST))
            time.sleep(0.3)
            chan_6500.close()
            ssh_6500.close()
            return wasConnected
    print(f'[SSH] Sending INH-MSG-ALL command...')
    F_DBG.write(f'[SSH] Sending INH-MSG-ALL command...\\n')
    f1 = TL1_In_Out('INH-MSG-ALL::ALL:Q100;')
    print(f'[SSH] INH-MSG-ALL response received ({len(f1)} bytes)')
    F_DBG.write(f'[SSH] INH-MSG-ALL response ({len(f1)} bytes)\\n')
    print (f1)
    if f1.find('Privilege, Login Not Active') > -1:
        wasConnected = 'Shelf responded: Privilege, Login Not Active'
        WARNING('Login Issue', wasConnected)
        F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (f1, wasConnected, HOST))
        time.sleep(2)
        chan_6500.close()
        ssh_6500.close()
        return wasConnected
    return wasConnected


def PARSE_RTRV_NODES(linesIn, TID, F_Out):
    f1 = 'Local TID,Local Shelf ID,Remote TID,Remote Site ID,Remote Shelf,Remote Member,Remote Shelf Type,Remote IP,Remote Shelf COLAN-X IP,Remote MAC,Netmask,Next Hop,Circuit,Carrier,Cost,Extra Cost,Owner,\n'
    F_Out.write(f1)
    f1 = ',,NODES,NODES,NODES,NODES,NODES,NODES,NODES,NODES,IPRTG-TBL,IPRTG-TBL,IPRTG-TBL,IPRTG-TBL,IPRTG-TBL,IPRTG-TBL,IPRTG-TBL,\n'
    F_Out.write(f1)
    d_TID_ID_SHELF__RSHELF_RIP = {}
    for line in linesIn:
        if line.find('REMOTESHELF') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            rtid = FISH(line, 'TID=\\"', '\\",')
            rshelf = FISH(line, 'REMOTESHELF=', ',')
            ip1 = FISH(line, 'IPADDR=', ',')
            ip2 = FISH(line, 'IPADDR2=', ',')
            rmember = FISH(line, 'MEMBER=', ',')
            rsite = FISH(line, 'SITEID=', '"')
            macaddress = FISH(line, 'MACADDR=', ',')
            ne = FISH(line, 'NETYPE=', ',')
            i = ne[4:6]
            if i == '16':
                ne = '6500'
            elif i == '17':
                ne = 'CPL'
            elif i == '18':
                ne == '5410'
            elif i == '1C':
                ne = 5430
            else:
                ne = '-'
            d_TID_ID_SHELF__RSHELF_RIP[shelf + '+' + ip1] = rtid + ',' + rsite + ',' + rshelf + ',' + rmember + ',' + ne + ',' + ip1 + ',' + ip2 + ',' + macaddress
        elif line.find('NETMASK') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            ip1 = FISH(line, 'IPADDR=', ',')
            mask = FISH(line, 'NETMASK=', ',')
            hop = FISH(line, 'NEXTHOP=', ',')
            owner = FISH(line, 'OWNER=', ',')
            cost = FISH(line, ',COST=', ',')
            extracost = FISH(line, 'EXTCOST=', ',')
            circ = FISH(line, 'CIRCUIT=', ',')
            carrier = FISH(line, 'CARRIER=', ',')
            try:
                f2 = d_TID_ID_SHELF__RSHELF_RIP[shelf + '+' + ip1]
                F_Out.write(TID + ',' + shelf + ',' + f2 + ',' + mask + ',' + hop + ',' + circ + ',' + carrier + ',' + cost + ',' + extracost + ',' + owner + ',\n')
            except:
                f2 = ',-,-,-,-,' + ip1 + ',-'

    return None


def PARSE_RTRV_RTG_TBL(linesIn, TID, F_Out):
    f1 = 'Local TID,Local Shelf ID,Remote TID,Remote Shelf Type,Remote IP,Cost,\n'
    F_Out.write(f1)
    d_TID_ID_SHELF__RIP = {}
    for line in linesIn:
        if line.find('|') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            rtid = FISH(line, '::,\\"', '\\",')
            ip1 = FISH(line, '|', ',')
            f1 = line.split(',')
            ne = f1[3]
            i = ne[4:6]
            if i == '16':
                ne = '6500'
            elif i == '17':
                ne = 'CPL'
            elif i == '18':
                ne == '5410'
            elif i == '1C':
                ne = '5430'
            else:
                ne = '-'
            d_TID_ID_SHELF__RIP[shelf + '+' + rtid] = rtid + ',' + ne + ',' + ip1
            continue
        elif line.find('ADJACENCY=') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            rtid = FISH(line, 'TID=', ',')
            cost = FISH(line, ',COST=', ',')
            try:
                f2 = d_TID_ID_SHELF__RIP[shelf + '+' + rtid]
            except:
                f2 = '-,-,-'

            F_Out.write(TID + ',' + shelf + ',' + f2 + ',' + cost + ',\n')

    return None


def PARSE_RTRV_OPTMON(linesIn, dMEMBERS, dCPACK, TID, F_Out, F_ERROR):
    fErr = ''
    f1 = 'TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit Pack,Port Label,Monitor Type,Location,Untimed,LOS Threshold,Auto IS Time left (hh-mm),Pstate,Sstate,\n'
    F_Out.write(f1)
    dOPTMON = {}
    for line in linesIn:
        if line.find(',OPTMON:OP') > -1 and line.find('1-UNT') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[1]
            s1 = f2.replace('OPTMON:', '') + ',' + f1[4] + ',' + f1[2]
            dOPTMON[AID] = s1
        if line.find('::') > -1 and line.find('LOSTHRES') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[3].strip(' "\n\r')
            f1 = f2.split(',')
            PRI = f1[0]
            try:
                SEC = f1[1]
            except IndexError:
                SEC = ''

            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            l1 = AID.find('-') + 1
            l2 = AID.rfind('-') - 1
            f1 = AID[0:l2]
            l2 = f1.rfind('-')
            shelf = AID[l1:l2]
            SHELF = 'SHELF-' + shelf
            ALSODISABLED = FISH(line, 'ALSODISABLED=', ':')
            LOSTHRES = FISH(line, 'LOSTHRES=', ',')
            AINSTIMELEFT = FISH(line, 'AINSTIMELEFT=', ',')
            if line.find('PORTLABEL') > -1:
                PORTLABEL = FISH(line, 'PORTLABEL=\\"', '\\"')
            else:
                PORTLABEL = ''
            s1 = ShSl + '-'
            CP = ''
            for j in dCPACK.items():
                if s1.find(j[0]) > -1:
                    CP = j[1]
                    continue

            if PORTLABEL == 'OSC A Out' and PRI == 'IS':
                if CP.find('SRA') > -1:
                    fErr += ',' + AID + ',OPTMON for SRA OSC A Out had primary state IS instead of OOS\n'
                elif CP.find('ESAM') > -1:
                    fErr += ',' + AID + ',OPTMON for ESAM OSC A Out had primary state IS instead of OOS\n'
            s1 = TID + ',' + SHELF + ',-,-,-,-,-,' + AID + ',' + CP
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID + ',' + CP
                    continue

            try:
                f2 = dOPTMON[AID]
            except KeyError:
                f2 = ',,'

            f1 = s1 + ',' + PORTLABEL + ',' + f2 + ',' + LOSTHRES + ',' + AINSTIMELEFT + ',' + PRI + ',' + SEC + ','
            F_Out.write(f1 + '\n')

    if fErr != '':
        F_ERROR.write('\nOPTMON Issues (see tab OPTMON)\n' + fErr)
    return None


def DETERMINE_CienaPC():
    import platform
    import datetime
    import os
    import math
    s1 = sys.getwindowsversion()
    major = s1[0]
    s2 = platform.platform()
    f1 = s2.split('-')
    platform = f1[1]
    if major == 6 and platform == 7:
        print ('OS = Win-7')
    elif major == 6 and platform == 8:
        print ('OS = Win-10')
    CienaPC = 'NO'
    sList = glob.glob('c:/programdata/sccm/*.*')
    for f1 in sList:
        if f1.find('Win7_Base_Ver_') > -1:
            CienaPC = 'YES'
            PartnerPC = 'YES'
            break

    sList = glob.glob('c:/programdata/sccm/sms_files/*.*')
    for f1 in sList:
        if f1.find('Win7_Base_Ver_') > -1 or f1.find('Win10_Model_Ver_2.1.sms') > -1:
            CienaPC = 'YES'
            PartnerPC = 'YES'
            break

    if CienaPC == 'NO':
        GlowPC1 = False
        sList = glob.glob('c:/programdata/sccm/appslogs/*.*')
        for f1 in sList:
            if f1.find('CiscoJabber_') > -1:
                GlowPC1 = True
                break

        GlowPC2 = False
        GlowPC3 = False
        GlowPC4 = False
        GlowPC5 = False
        sList = glob.glob('c:/programdata/*')
        for f1 in sList:
            if f1.find('Oracle') > -1:
                GlowPC2 = True
            if f1.find('Hewlett-Packard') > -1:
                GlowPC3 = True
            if f1.find('dbg') > -1:
                GlowPC4 = True
            if f1.find('Avecto') > -1:
                GlowPC5 = True
            if GlowPC1 and GlowPC2 and GlowPC3 and GlowPC4 and GlowPC5:
                print ('GLOW PC  ')
                CienaPC = 'YES'
                PartnerPC = 'YES'

    if CienaPC != 'YES':
        PartnerPC = 'NO'
        version = float(SCRIPT_VERSION)
        print ('Script Version: ' + str(version))
        now = datetime.datetime.now()
        currentYear = int(now.year)
        currentMonth = int(now.month)
        ALPHA = 3000
        BETA = 3
        GAMMA = currentYear
        M2 = currentMonth * currentMonth
        ZZ = int(ALPHA * version) + BETA * M2 + GAMMA
        IN = input('Enter numeric password >>> ')
        try:
            base = 8
            oct_dec = int(IN, base)
        except:
            oct_dec = 0

        ii = ZZ - oct_dec
        if math.fabs(ii) < 0.01:
            PartnerPC = 'YES'
        else:
            PartnerPC = 'NO'
    out = CienaPC + '+' + PartnerPC
    return out


def CONSOLIDATE_CSV(TimeStamp, WindowsHost, TID, OSRP_NODENAME, OSRP_NODEID, OSRP_NODEID_HEX, OSRP_NODEIP, OSRP_TYPE):
    from xlsxwriter.workbook import Workbook
    import codecs
    COMMENTS = {}
    dCSV_COMMENTS__FILE = {'Issues': ['Photonic Issues'],
    'Active_Users': ['Provisioned Security Profiles & Invalid Passwords'],
    'Adjacencies': ['Adjacencies \nFiber Adjacencies \nLine Adjacencies'],
    'Alarms': ['Active & Disabled Conditions'],
    'Amplifiers': ['Amplifier Facility & PM \nOTS & OSID \nLine Adjacency FEAID \nPower Levels & ORL'],
    'CHMON': ['CHMON Power Levels & OTS & OSID'],
    'CRS_MC': ['Media Channel Photonic Cross-connects'],
    'CRS_NMC': ['Network Media Channel Photonic Cross-connects'],
    'CRS_ODUx': ['ODU (Transponder) & ODUCTP (OTN) CRS'],
    'DCN': ['GNE \nOSPF Routers \nOSPF Circuits \nDBRS \nIPV4 IP \nIPV6 IP \nLAN \nNDP \nGCC0/GCC1 \nTelnet \nSSH \nHTTP/HTTPS/REST \nFTP'],
    'DOC': ['DOC & Differential Provisioning'],
    'DOC_OCH': ['Fixed Grid Optical Channel Controller (DOC)'],
    'DSCM_Pads': ['DISP Provisioning & OTS & OSID'],
    'DTL': ['DTL & DTLSET'],
    'Equipment': ['Equipment & Equipment Mode'],
    'ETH10G': ['10G ETH Facilities & PM'],
    'ETTP': ['ETH Trail Termination Point Facilities & PM'],
    'FLEX': ['FLEX Facilities & PM'],
    'Inventory': ['Inventory & Fans'],
    'L0_OSRP_Lines': ['L0 CP OSRP Lines'],
    'L0_OSRP_Links': ['L0 CP OSRP Links'],
    'L0_OSRP_Nodes': ['L0 CP OSRP Nodes'],
    'L1_OSRP_Lines': ['L1 CP OSRP Lines'],
    'L1_OSRP_Links': ['L1 CP OSRP Links'],
    'L1_OSRP_Nodes': ['L1 CP OSRP Nodes'],
    'Licenses': ['Licenses & License Manager'],
    'LOC': ['CCMD Local Optical Controller'],
    'NMCMON': ['Network Media Channel Power Levels'],
    'OCH_Management': ['Flex Optical Channel Management'],
    'ODUCTP': ['ODUCTP Facilities & PM'],
    'ODUTTP': ['ODUTTP Facilities & PM'],
    'OPTMON': ['OPTMON Facilities & PM'],
    'OSC': ['Optical Service Channel Facility, PM, and Measured Distance'],
    'OSPF_Nodes': ['Visible TID via OSPF'],
    'OTDR': ['OTDR Traces and Events'],
    'OTM2': ['OTM2 Facilities & PM & GCC0 / GCC1 OTM2 Circuits'],
    'OTM3': ['OTM3 Facilities & PM & GCC0 / GCC1 OTM3 Circuits'],
    'OTM4': ['OTM4 Facilities & PM & GCC0 / GCC1 OTM4 Circuits'],
    'OTMC2': ['OTMC2 Facilities & PM & GCC0 / GCC1 OTM4 Circuits'],
    'OTM_Protection': ['OTN Protection Provisioning & OTN Protection Status'],
    'OTN_Protection': ['OTN Protection Provisioning & OTN Protection Status'],
    'OTS': ['OTS Group & Line Adjacency FEAID'],
    'OTUTTP': ['OTUTTP Facilities & PM & GCC0 / GCC1 OTM4 Circuits'],
    'Photonic_Profiles': ['Photonic Profiles'],
    'PTP': ['PTP Facilities & PM'],
    'Q_Groups': ['Q-Groups'],
    'RADIUS': ['RADIUS Accounting & Server'],
    'Routing_Table': ['DCN Routing Table'],
    'Rx_Adjacency': ['Rx Adjacencies'],
    'SDMON': ['Spectral Data Monitor & OTS & OSID'],
    'Security_Rules': ['Default Security Attributes, Password Rules, and SYSLOG'],
    'Shelves': ['MCEMON State \nCLLI \nTime Zone \nTOD Provisioning & Status \nSystem \nBackup Status \nTL1 Gateway \nSSL \nSNMP & Trap Destination Provisioning'],
    'SlotSequence': ['Slot & TID Slot Sequence'],
    'SNC': ['Sub Network Connections \nEnd-to-end Diagnostics'],
    'SNC_Routes': ['SNC Routes'],
    'SNCG': ['Grouped Sub Network Connections'],
    'SNCG_Routes': ['SNCG Routes'],
    'SONET': ['SONET Facilities & PM'],
    'SPLI': ['SPLI Provisioning'],
    'SSC': ['Spectral Shape Controller', 'OTS & OSID'],
    'STTP': ['SONET Trail Termination Point Facilities & PM'],
    'Static_Routes': ['Static Routes'],
    'Synch': ['Synchronization provisioning'],
    'Telemetry': ['Telemetry Facility'],
    'Tx_Adjacency': ['Tx Adjacencies'],
    'TCMTTP': ['TCM Trail Termination Point Facilities & PM'],
    'WAN': ['WAN Point Facilities & PM'],
    'WSS_CHC': ['WSS Channel Control'],
    'WSS_NMCC': ['Network Media Channel Controller']}
    if TID.find('/') > -1:
        f1 = TID.replace('/', '_')
    else:
        f1 = TID
    OutputFileName = os.getcwd() + '\\' + f1 + '.xlsx'
    OutputFileName = _reserve_workbook_path(OutputFileName)

    workbook = Workbook(OutputFileName)
    HeaderFormat = workbook.add_format({'bold': True,
    'font_color': 'white'})
    HeaderFormat.set_bg_color('black')
    HeaderFormat.set_font_size(10)
    url_format = workbook.add_format({'font_color': 'blue',
    'underline': 1})
    root0 = 'Index'
    worksheet1 = workbook.add_worksheet(root0)
    irow = 0
    worksheet1.write(irow, 0, 'Capture Time = ' + TimeStamp, HeaderFormat)
    irow += 2
    worksheet1.write(irow, 0, 'TID IP = ' + WindowsHost, HeaderFormat)
    irow += 1
    worksheet1.write(irow, 0, 'TID Name = ' + TID, HeaderFormat)
    if OSRP_NODENAME != '':
        irow += 1
        worksheet1.write(irow, 0, 'OSRP Name = ' + OSRP_NODENAME, HeaderFormat)
        irow += 1
        worksheet1.write(irow, 0, 'OSRP Node ID = ' + OSRP_NODEID, HeaderFormat)
        irow += 1
        worksheet1.write(irow, 0, 'OSRP Node ID = ' + OSRP_NODEID_HEX, HeaderFormat)
        irow += 1
        worksheet1.write(irow, 0, 'OSRP Node IP = ' + OSRP_NODEIP, HeaderFormat)
        irow += 1
        worksheet1.write(irow, 0, 'OSRP Type = ' + OSRP_TYPE, HeaderFormat)
    irow += 1
    for csvName in glob.glob(os.path.join('.', '*.csv')):
        if ISSUES == 'NO' and csvName.find('Issues.csv') > -1:
            continue
        f_path, f_name = os.path.split(csvName)
        f_short_name, f_extension = os.path.splitext(f_name)
        l1 = f_short_name.find('_') + 1
        newTab = f_short_name[l1:].strip()
        print (newTab)
        with codecs.open(csvName, encoding='utf-8', errors='ignore') as f:
            lines = f.read()
            if lines.count('\n') < 2:
                f.close()
                os.remove(csvName)
                continue
            else:
                f.seek(0)
            try:
                COMMENTS = dCSV_COMMENTS__FILE[newTab]
            except:
                COMMENTS[0] = ''

            worksheet = workbook.add_worksheet(newTab)
            link = 'internal:' + newTab + '!A1'
            irow += 1
            irow0 = irow
            worksheet1.write_url(irow, 0, link, url_format, newTab)
            worksheet1.write(irow, 1, COMMENTS[0])
            l1 = len(COMMENTS)
            if l1 > 1:
                i = 1
                while i < l1:
                    irow += 1
                    worksheet1.write(irow, 1, COMMENTS[i])
                    i += 1

            reader = csv.reader(f, dialect='excel')
            rows = list(reader)
            for r, row in enumerate(rows):
                for c, col in enumerate(row):
                    if r == 0 and c == 0:
                        link = 'internal:' + root0 + '!A' + str(irow0 + 1)
                        worksheet.write_url(0, 0, link, url_format, col)
                        worksheet.write_comment(0, 0, 'Bookmark to ' + root0)
                    else:
                        worksheet.write(r, c, col)
            _autosize_worksheet_columns(worksheet, rows)

            f.close()
            os.remove(csvName)

    try:
        workbook.close()
    except Exception as err:
        try:
            F_DBG.write('\nLegacy workbook close failed for %s: %s\n' % (OutputFileName, str(err)))
        except Exception:
            pass
        raise
    return


def PARSE_RTRV_DOC_CH_FLEX(linesIn, lOTSinfo, TID, dCPACK, WindowsHost):
    dNMC_INFO_1__LnAid = {}
    dNMC_INFO_2__LnAid = {}
    dMCTTP_INFO__ShSlPrtMc = {}
    dINFO_4_SNC__SourceADJ = {}
    dCRS__FREQUENCY = {}
    F_OCH = open(WindowsHost + '_OCH_Management.csv', 'w')
    F_OCH.write('TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,DOC Instance,Section Ingress,SNCG Circuit Id,SNCG Incarnation #,SNCG Label,SNCG Prime,SNCG Min Frequency,SNCG Max Frequency,SNCG Bias,SNC Circuit Id,SNC Incarnation #,SNC Label,SNC Prime,Center Frequency(THz),Bandwidth (GHz),Wavelength (nm),SNC Type,SNC Subtype,CMD Tx Port,CMD Rx Port,DOC Care,SNC CRS Mismatch,SNC CRS From Port,AID ShelfID-Sl-Prt-MCId-NMCId,ADJ Tx CKTID,Channel Optimization State,Channel Fault Status,Routing,Modulation Class,Active,SNR Bias(dB),MCId-NMCId,Source TID-Shelf-TxPathId,Destination(s) TID-Shelf-RxPathId,Local Domain TID trail,TID:DOC Trail,Estimated OSNR (dB)\n')
    F_OCH.write('TID,SHELF,RTRV-OTS,RTRV-OTS,RTRV-OTS,RTRV-OTS,RTRV-ADJ-LINE,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-MCTTP,RTRV-MCTTP,RTRV-MCTTP,RTRV-MCTTP,RTRV-MCTTP,RTRV-MCTTP,RTRV-MCTTP,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-CRS-NMC,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH,RTRV-DOC-CH\n')
    F_MC = open(WindowsHost + '_CRS_MC.csv', 'w')
    F_MC.write('AID,Connection Type,Min Frequency (THz),Max Frequency (THz),Lower Freq Filter-Edge Spacing (GHz),Upper Freq Filter-Edge Spacing (GHz),MC Bias (dB),Paired MCTTP,SNCG Circuit ID,SNCG Label,Incarnation #,Derived,Number oF NMC,Prime,Auto Delete,Target Lower Freq (THz),Target Upper  Freq (THz),\n')
    for line in linesIn:
        if line.find('PAIREDCRS') > -1 and line.find('DOCCARE') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            l1 += 1
            l2 = line.rfind(':')
            WAY = line[l1:l2]
            l1 = AID.split(',')
            AID1 = l1[0]
            AID2 = l1[1]
            if AID1.find('NMCLNCTP') > -1:
                LN_AID = AID1
                AD_AID = AID2
            else:
                LN_AID = AID2
                AD_AID = AID1
            l1 = FISH(line, 'WIDTH=', ',')
            WIDTH = l1.rstrip('0')
            if len(WIDTH) > 1 and WIDTH[-1] == '.':
                WIDTH = WIDTH + '0'
            FREQUENCY = FISH(line, 'FREQUENCY=', ',')
            FREQUENCY = FREQUENCY.rstrip('0')
            if WIDTH == '37.5':
                l1 = 299792458.0 / float(FREQUENCY) / 1000.0
                WAVE = '%.2f' % l1
            else:
                WAVE = ''
            CST = FISH(line, 'CST=', ',')
            SOURCEPORT = FISH(line, ',SOURCEPORT=\\"', '\\"')
            DESTPORT = FISH(line, ',DESTPORT=\\"', '\\"')
            DOCCARE = FISH(line, 'DOCCARE=', ',')
            MISMATCH = FISH(line, 'MISMATCH=', ',')
            PRIME = FISH(line, 'PRIME=', ',')
            if PRIME == 'CPS':
                PRIME += ' (Control Plane System Owned)'
            else:
                PRIME += ' (Operation Support System Owned)'
            dNMC_INFO_2__LnAid[LN_AID] = WAY + ',' + CST + ',' + SOURCEPORT + ',' + DESTPORT + ',' + DOCCARE + ',' + MISMATCH + ',' + AD_AID
            if AD_AID.find('NMCLNCTP') > -1:
                dNMC_INFO_2__LnAid[AD_AID] = WAY + ',' + CST + ',' + SOURCEPORT + ',' + DESTPORT + ',' + DOCCARE + ',' + MISMATCH + ',' + AD_AID
            SNCCKTID = FISH(line, ',SNCCKTID=\\"', '\\"')
            SNCLABEL = FISH(line, ',SNCLABEL=\\"', '\\"')
            SNCINCARN = FISH(line, 'SNCINCARN=', ',')
            if line.find('CST=DROP,') < 0:
                dNMC_INFO_1__LnAid[LN_AID] = SNCCKTID + ',' + SNCINCARN + ',' + SNCLABEL + ',' + PRIME + ',' + FREQUENCY + ',' + WIDTH + ',' + WAVE
            if AD_AID.find('NMCLNCTP') > -1:
                dNMC_INFO_1__LnAid[AD_AID] = SNCCKTID + ',' + SNCINCARN + ',' + SNCLABEL + ',' + PRIME + ',' + FREQUENCY + ',' + WIDTH + ',' + WAVE
                l1 = AD_AID.split('-')
            ADJ_ShSlPrt = 'ADJ' + SOURCEPORT.replace(TID, '')
            dINFO_4_SNC__SourceADJ[ADJ_ShSlPrt] = FREQUENCY + ',' + WIDTH + ',' + WAVE
            CKTID = FISH(line, ',CKTID=\\"', '\\"')
            ACTIVE = FISH(line, 'ACTIVE=', ',')
            CRSAUTODELETE = FISH(line, 'CRSAUTODELETE=', ',')
            CRSNMCAID = FISH(line, ',CRSNMCAID=\\"', '\\"')
            FROMOTS = FISH(line, 'FROMOTS=\\"', '\\"')
            TOOTS = FISH(line, ',TOOTS=\\"', '\\"')
            FROMCHSTATUS = FISH(line, 'FROMCHSTATUS=', ',')
            TOCHSTATUS = FISH(line, 'TOCHSTATUS=', ',')
            f1 = FISH(line, ',PAIREDCRS=\\"', '\\"')
            PAIREDCRS = f1.replace(',', ' & ')
            PORTTRAIL = FISH(line, ',PORTTRAIL=\\"', '\\"')
            CP_TRAIL = TRANSLATE_CP_PORTTRAIL(PORTTRAIL, dCPACK)
            PORTTRAIL = PORTTRAIL.replace(',', ' >> ')
            if line.find('EXPRESSDELETE') > -1:
                EXPRESSDELETE = FISH(line, 'EXPRESSDELETE=', ',')
                f1 = FISH(line, ',CRSNMCAID=\\"', '\\"')
                CRSNMCAID = f1.replace(',', ' & ')
                DERIVED = FISH(line, 'DERIVED=', ',')
                l1 = AID1.rfind('-') + 1
                WAVELENGTH = AID1[:l1]
            else:
                EXPRESSDELETE = 'Not Applicable'
                CRSNMCAID = 'Not Applicable'
                DERIVED = 'Not Applicable'
                WAVELENGTH = 'Not Applicable'
            f1 = CKTID + ',' + WAVELENGTH + ',' + FREQUENCY + ',' + WIDTH + ',' + AID1 + ',' + AID2 + ',' + WAY + ',' + DOCCARE + ',' + MISMATCH + ',' + DERIVED + ',' + CST + ',' + ACTIVE + ',' + EXPRESSDELETE + ',' + CRSAUTODELETE + ',' + PRIME + ',' + SOURCEPORT + ',' + DESTPORT + ',' + CRSNMCAID + ',' + SNCCKTID + ',' + SNCLABEL + ',' + FROMOTS + ',' + TOOTS + ',' + FROMCHSTATUS + ',' + TOCHSTATUS + ',' + PAIREDCRS + ',' + PORTTRAIL + ',' + CP_TRAIL + '\n'
            try:
                dCRS__FREQUENCY[FREQUENCY] += f1
            except:
                dCRS__FREQUENCY[FREQUENCY] = f1

        elif line.find('SNCGINCARN') > -1 and line.find(' "MCTTP-') > -1:
            l1 = line.find(':')
            AID = line[4:l1]
            AID1 = line[10:l1]
            l2 = line.rfind(':')
            WAY = line[l1:l2]
            MINFREQ = FISH(line, 'MINFREQ=', ',')
            MAXFREQ = FISH(line, 'MAXFREQ=', ',')
            CKTID = FISH(line, ',CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            MINFREQDEADBAND = FISH(line, 'MINFREQDEADBAND=', ',')
            MAXFREQDEADBAND = FISH(line, 'MAXFREQDEADBAND=', ',')
            MCBIAS = FISH(line, 'MCBIAS=', ',')
            PAIREDMCTTP = FISH(line, ',PAIREDMCTTP=\\"', '\\"')
            SNCGCKTID = FISH(line, ',SNCGCKTID=\\"', '\\"')
            SNCGLABEL = FISH(line, ',SNCGLABEL=\\"', '\\"')
            SNCGINCARN = FISH(line, 'SNCGINCARN=', ',')
            DERIVED = FISH(line, 'DERIVED=', ',')
            NUMOFNMC = FISH(line, 'NUMOFNMC=', ',')
            PRIME = FISH(line, 'PRIME=', ',')
            if PRIME == 'CPS':
                PRIME += ' (Control Plane System Owned)'
            else:
                PRIME = +' (Operation Support System Owned)'
            AUTODELETE = FISH(line, 'AUTODELETE=', ',')
            TARGETMINFREQ = FISH(line, 'TARGETMINFREQ=', ',')
            TARGETMAXFREQ = FISH(line, 'TARGETMAXFREQ=', ',')
            dMCTTP_INFO__ShSlPrtMc[AID1] = SNCGCKTID + ',' + SNCGINCARN + ',' + SNCGLABEL + ',' + PRIME + ',' + MINFREQ + ',' + MAXFREQ + ',' + MCBIAS
            F_MC.write(AID + ',' + WAY + ',' + MINFREQ + ',' + MAXFREQ + ',' + MINFREQDEADBAND + ',' + MAXFREQDEADBAND + ',' + MCBIAS + ',' + PAIREDMCTTP + ',' + SNCGCKTID + ',' + SNCGLABEL + ',' + SNCGINCARN + ',' + DERIVED + ',' + NUMOFNMC + ',' + PRIME + ',' + AUTODELETE + ',' + TARGETMINFREQ + ',' + TARGETMAXFREQ + ',\n')
        elif line.find('SNRBIAS') > -1:
            l1 = line.find('::')
            AID = line[4:l1]
            tokens = AID.split('-')
            SHELF = tokens[1]
            ShSlPrt = SHELF + '-' + tokens[2] + '-' + tokens[3]
            McID = tokens[4]
            NmcID = tokens[5]
            SectionIngress = TID + '-' + ShSlPrt
            McIDNmcID = tokens[4] + '-' + tokens[5]
            line = line[:-2] + ','
            COS = FISH(line, 'COS=\\"', '\\"')
            CFS = FISH(line, ',CFS=\\"', '\\"')
            EEC = FISH(line, ',EEC=\\"', '\\"')
            FLAG = FISH(line, ',INGRESSACTIVEFLAG=\\"', '\\"')
            INGRESS = FISH(line, ',INGRESS=\\"', '\\"')
            EGRESS = FISH(line, ',EGRESS=\\"', '\\"')
            f1 = FISH(line, ',NETRAIL=\\"', '\\"')
            NETRAIL = f1.replace(',', ' > ')
            if line.find('NWCTDOCTRAIL') > -1:
                f1 = FISH(line, ',NWCTDOCTRAIL=\\"', '\\"')
                NWCTDOCTRAIL = f1.replace(',', ' > ')
            else:
                NWCTDOCTRAIL = ''
            CKTID = FISH(line, ',CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            DOMROU = FISH(line, ',Domain Routing=\\"', '\\"')
            SNRBIAS = FISH(line, 'SNRBIAS=', ',')
            MODCLASS = FISH(line, 'MODCLASS=', ',')
            ESTINCROSNR = FISH(line, ',ESTINCROSNR=\\"', '\\"')
            DOCINST = FISH(line, 'DOCINST=', ',')
            l1 = ShSlPrt + '-' + McID
            try:
                MNC_1 = dMCTTP_INFO__ShSlPrtMc[l1]
            except:
                MNC_1 = ',,,,,,,'

            try:
                SNC_1 = dNMC_INFO_1__LnAid[AID]
            except:
                SNC_1 = ',,,,,,,'

            try:
                SNC_2 = dNMC_INFO_2__LnAid[AID]
            except:
                SNC_2 = ',,,,'

            DOC_CH_1 = AID + ',' + CKTID + ',' + COS + ',' + CFS + ',' + DOMROU + ',' + MODCLASS + ',' + FLAG + ',' + SNRBIAS + ',' + McIDNmcID + ',' + INGRESS + ',' + EGRESS + ',' + NETRAIL + ',' + NWCTDOCTRAIL + ',' + ESTINCROSNR
            f1 = DOCINST.replace('DOC', 'OTS')
            OTS_info = ',' + f1 + ',,,'
            for i in lOTSinfo:
                if i.find(f1) > -1:
                    OTS_info = i
                    continue

            f1 = TID + ',' + SHELF + ',' + OTS_info + ',' + DOCINST + ',' + SectionIngress + ',' + MNC_1 + ',' + SNC_1 + ',' + SNC_2 + ',' + DOC_CH_1 + ',\n'
            F_OCH.write(f1)

    F_OCH.close()
    F_MC.close()
    F_NMC = open(WindowsHost + '_CRS_NMC.csv', 'w')
    F_NMC.write('Circuit ID,Wavelength (nm),Center Frequency (THz),BW Width (GHz),From AID,To AID,Type,DOC Controlled,Mismatch,Derived,Subtype,Active,Express Delete,Auto Delete,Prime,Source Port,Destination Port,SNC AID,SNC Circuit ID,SNC Label,From OTS,To OTS,FromChStatus,ToChStatus,Paired Connection,Port Trail,CP Trail\n')
    for FREQUENCY in sorted(dCRS__FREQUENCY):
        F_NMC.write(dCRS__FREQUENCY[FREQUENCY] + '\n')

    F_NMC.close()
    return dINFO_4_SNC__SourceADJ


def PARSE_RTRV_DOC_CH_FIXED(linesIn, lOTSinfo, TID, F_OUT):
    F_OUT.write('TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,DOC,Circuit Identifier,Wavelength,AID,DOC Trail Status,Domain Routing,Channel Condition,End-to-End Condition,Source,Destination,Ingress Flag,Channel Fault State,NE Trail,DOC Trail,Estimated OSNR (dB),\n')
    for line in linesIn:
        if line.find('NWCTDOCTRAILSTATUS') > -1:
            s1 = line.split('::')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            l1 = AID.rfind('-') + 1
            l2 = len(AID)
            f2 = AID[l1:l2]
            WAVE = f2[:4] + '.' + f2[4:]
            f1 = s1[1]
            COS = FISH(f1, 'COS=\\"', '\\"')
            CFS = FISH(f1, ',CFS=\\"', '\\"')
            EEC = FISH(f1, ',EEC=\\"', '\\"')
            INGRESSACTIVEFLAG = FISH(f1, ',INGRESSACTIVEFLAG=\\"', '\\"')
            INGRESS = FISH(f1, ',INGRESS=\\"', '\\"')
            EGRESS = FISH(f1, ',EGRESS=\\"', '\\"')
            NETRAIL = FISH(f1, ',NETRAIL=\\"', '\\"')
            NETRAIL = NETRAIL.replace(',', ' > ')
            NWCTDOCTRAIL = FISH(f1, ',NWCTDOCTRAIL=\\"', '\\"')
            NWCTDOCTRAIL = NWCTDOCTRAIL.replace(',', ' >>> ')
            NWCTDOCTRAILSTATUS = FISH(f1, ',NWCTDOCTRAILSTATUS=', ',')
            CKTID = FISH(f1, ',CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            DOMROU = FISH(f1, ',Domain Routing=\\"', '\\"')
            ESTINCROSNR = FISH(line, ',ESTINCROSNR=\\"', '\\"')
            out1 = CKTID + ',' + WAVE + ',' + AID + ',' + NWCTDOCTRAILSTATUS + ',' + DOMROU + ',' + COS + ',' + EEC + ',' + INGRESS + ',' + EGRESS + ',' + INGRESSACTIVEFLAG + ',' + CFS + ',' + NETRAIL + ',' + NWCTDOCTRAIL + ',' + ESTINCROSNR
            if len(NWCTDOCTRAIL) > 3:
                f1 = NWCTDOCTRAIL.replace(' = ', ' >>> ')
                f2 = f1.split(' >>>')
                for i in f2:
                    if i.find(TID) > -1:
                        f1 = i.split(':')
                        doc = f1[1]
                        f1 = doc.split('-')
                        shelf = 'SHELF-' + f1[1]
                        continue

                f1 = doc.replace('DOC', 'OTS')
                ots = ''
                for i in lOTSinfo:
                    if i.find(f1) > -1:
                        ots = i
                        continue

            else:
                doc = ''
                ots = ',,,,'
                shelf = ''
            f1 = TID + ',' + shelf + ',' + ots + ',' + doc + ',' + out1 + ',\n'
            F_OUT.write(f1)

    return None


def PARSE_ALL_DOC(linesIn, TID, lOTSinfo, F_ERROR, F_OUT):
    lPoint = []
    lPoint.append('TID = ' + TID)
    lPoint.append('OTS INFORMATION')
    i_Ots = 2
    lPoint.append('OTS Instance')
    lPoint.append('OSID')
    lPoint.append('Tx Path ID')
    lPoint.append('Rx Path ID')
    lPoint.append('Reliable Far End AID')
    lPoint.append(' ')
    lPoint.append('DOC PROVISIONING')
    i_Doc = i_Ots + 7
    lPoint.append('DOC Identifier')
    lPoint.append('Automation Mode')
    lPoint.append('Auto add channels')
    lPoint.append('Auto delete channels')
    lPoint.append('Auto delete on fault')
    lPoint.append('DOC Clamp Mode')
    lPoint.append('DOC Command Status')
    lPoint.append('Progress')
    lPoint.append('Overall Status')
    lPoint.append('State')
    lPoint.append(' ')
    lPoint.append('DIFFERENTIAL PROVISIONING')
    i_Dp = i_Doc + 12
    lPoint.append('2G5 Class Bias')
    lPoint.append('10G Class Bias')
    lPoint.append('10G NGM Class Bias')
    lPoint.append('40G Class Bias')
    lPoint.append('100G Class Bias')
    lPoint.append('100G WL3 Class Bias')
    lPoint.append('100G WL3 BPSK Class Bias')
    lPoint.append('40G ULH Class Bias')
    lPoint.append('100G WL3 8QAM Class Bias')
    lPoint.append('100G WL3 16QAM Class Bias')
    lPoint.append('100G WL3 4ASK Class Bias')
    lPoint.append('100G WLAi 35GBAUD Class Bias')
    lPoint.append('150G WLAi 35GBAUD Class Bias')
    lPoint.append('200G WLAi 35GBAUD Class Bias')
    lPoint.append('250G WLAi 35GBAUD Class Bias')
    lPoint.append('100G WLAi 56GBAUD Class Bias')
    lPoint.append('150G WLAi 56GBAUD Class Bias')
    lPoint.append('200G WLAi 56GBAUD Class Bias')
    lPoint.append('250G WLAi 56GBAUD Class Bias')
    lPoint.append('300G WLAi 56GBAUD Class Bias')
    lPoint.append('350G WLAi 56GBAUD Class Bias')
    lPoint.append('400G WLAi 56GBAUD Class Bias')
    lPoint.append('Custom 1 Class Bias')
    lPoint.append('Custom 2 Class Bias')
    lPoint.append('Custom 3 Class Bias')
    lPoint.append('Custom 4 Class Bias')
    lPoint.append('Custom 5 Class Bias')
    lPoint.append('Custom 6 Class Bias')
    lAID = []
    DOC_ALL = {}
    for line in linesIn:
        if line.find(':DOCMODE') > -1 and line.find('DOCPROGRESSSTATUS') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            l1 = line.rfind(':') + 1
            STATE = line[l1:-2]
            f1 = AID.replace('DOC', 'OTS')
            for i in lOTSinfo:
                if i.find(f1) > -1:
                    f2 = i.split(',')
                    idx = i_Ots
                    location = AID + '@' + str(idx)
                    DOC_ALL[location] = f1
                    idx = idx + 1
                    location = AID + '@' + str(idx)
                    DOC_ALL[location] = f2[1]
                    idx = idx + 1
                    location = AID + '@' + str(idx)
                    DOC_ALL[location] = f2[2]
                    idx = idx + 1
                    location = AID + '@' + str(idx)
                    DOC_ALL[location] = f2[3]
                    idx = idx + 1
                    location = AID + '@' + str(idx)
                    DOC_ALL[location] = f2[4]
                    continue

            lAID.append(AID)
            idx = i_Doc
            location = AID + '@' + str(idx)
            DOC_ALL[location] = AID
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ':DOCMODE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCAUTOADD=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCAUTODEL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCAUTODELLOS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'DOCGAINCLAMP=', ':')
            if f1 != 'ENABLE':
                F_ERROR.write(',' + AID + ', does not have its Clamp Mode enabled\n')
            DOC_ALL[location] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCCMDSTAT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCPROGRESSSTATUS=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'DOCSTATUS=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = STATE
            continue
        if line.find(':MODCLASSBIAS2G5') > -1 and line.find('MODCLASSBIASCUSTOM') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            line = line.replace('"', ',')
            idx = i_Dp
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, 'MODCLASSBIAS2G5=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS10G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS10GNGM=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS40G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100GWL3=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100GWL3BPSK=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS40GULH=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100GWL38QAM=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100GWL316QAM=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS100GWL34ASK=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS35GBAUD_100G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS35GBAUD_150G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS35GBAUD_200G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS35GBAUD_250G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_100G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_150G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_200G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_250G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_300G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_350G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIAS56GBAUD_400G=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM1=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM2=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM3=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM4=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM5=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DOC_ALL[location] = FISH(line, ',MODCLASSBIASCUSTOM6=', ',')
            continue

    CCC = []
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(1, nPoint))
    list1 = list(set(lAID))
    for aid in list1:
        ccc = []
        ccc.append('')
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = DOC_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    with open(F_OUT + '_DOC.csv', 'w', newline='') as f:
        writer = csv.writer(f)
        for i in range(len(max(CCC, key=len))):
            writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return


def GET_CP_DESCRIPTION(code):
    d_TX_TRANSLATE = {'WLAI': 'WLAi',
    'UNKNOWN': 'UNKNOWN',
    'EDC100GWL3OCLDLHCLD': '100G WaveLogic 3 OCLD LH 1xOTU4 C-Band (NTK539UB - Colored)',
    'FLEX3WL3MDMQPSKCLD3A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QL - Colored)',
    'WLAI56GBAUD200GCS': 'WL Ai Modem 56Gbaud 200GBit/s (Coherent Select)',
    'WL3EMDMQPSKCLS': 'WL3e Modem QPSK 1xOTU4 C-Band (Colorless)',
    'OMEEDC100GOCLD': '6500 eDC100G OCLD 1xOTU4+ DWDM (NTK539TB)',
    'EDC100GWL3OCLDLHCLS': '100G WaveLogic 3 OCLD LH 1xOTU4 C-Band (NTK539UB - Colorless)',
    'HDX10GDWDMTUNABLE': 'HDX (_C) 1 x 10G DWDM tunable service module (NTUC39JA)',
    'FLEX2WL3OCLDLHQPSKCO': 'Flex2 WL3 OCLD Long Haul Colorless-Optimized QPSK 1xOTU4 C-Band (NTK539AB - Colorless)',
    'OMENGMWT10GSSW10G7': '6500 NGM (eDCO) WT 1xOC192/STM64 1x10.7G (NTK530AA)',
    'WL3MDMQPSKCLS4B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UM - Colorless)',
    'OMENGMWT10GBE11G1EXT': '6500 NGM (eDCO) WT 1x10GE LAN 1x11.1G EXT PWR (NTK530AB)',
    'WL3EMDMUNAMP8QAMCLS': 'WL3e Modem Un-Amplified 8QAM NxOTU4 C-Band (Colorless)',
    'FLEX3WL3MDMQPSKCNTLS4A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QM - Contentionless)',
    'WL3EMDMBPSKCS1': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QJ - Coherent Select)',
    'WL3EMDMBPSKCS2': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QK - Coherent Select)',
    'WL3EMDMBPSKCS5': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QN - Coherent Select)',
    'WL3EMDMBPSKCS6': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QG - Coherent Select)',
    'WL3NMDMQPSKCLD3': 'WL3n Modem QPSK 1xOTU4 C-Band (NTK538BK - Colored)',
    'WL3EMDM16QAMCLD3': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QL; 134-5550-900 - Colored)',
    'FLEX2WL3MDMQPSKCLS4': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BN - Colorless)',
    'WL3EMDM8QAMCLS': 'WL3e Modem 8QAM NxOTU4 C-Band (Colorless)',
    'WL3EMDMQPSKCNTLS4B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UM - Contentionless)',
    'OMEEDC100GOCLDEPMD': '6500 eDC100G OCLD 1xOTU4+ DWDM Enhanced PMD Comp (NTK539TA)',
    'WL3EMDMQPSKCNTLS4A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QM; 134-5550-904 - Contentionless)',
    'XFPDWDM_NTK589_LBAND': 'Multirate Narrow L-Band Tunable DWDM XFP (NTK589PA_PQ)',
    'OM5KUOTR10GBE11G1': '565/5100/5200 OTR 10G Ultra 10GbE 11.1G (NT0H85xx)',
    'WL3EMDMQPSKCS2B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UK - Coherent Select)',
    'WL3EMDM8QAMCLD': 'WL3e Modem 8QAM NxOTU4 C-Band (Colored)',
    'OMENGMWT10GSSW10G7REXT': '6500 NGM (eDCO) WT 1xOC-192/STM-64 1x10.7G Regional EXT PWR (NTK530BA; NTK530BX)',
    'WL3EMDMQPSKCNTLS10A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UK - Contentionless)',
    'WL3EMDMQPSKCLD7A': 'WL3e Modem 1xOTU4 C-Band (NTK539UN - Colored)',
    'WL3EMDMUNAMP16QAMCLD': 'WL3e Modem Un-Amplified 16QAM 2xOTU4 C-Band (Colored)',
    'WL3NMDMAMP4ASKCLS': 'WL3n Modem Amplified 4ASK 1xOTU4 C-Band (Colorless)',
    'OM5K25GOTR': '565/5100/5200 OTR 2.5G Flex 850nm/1310nm (850nm Client: NT0H82xx; 1310nm Client: NT0H81xx)',
    'WL3EMDM16QAMCS2': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QK - Coherent Select)',
    'WL3EMDM16QAMCS3': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QL - Coherent Select)',
    'WL3EMDM16QAMCS1': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QJ - Coherent Select)',
    'WL3EMDM16QAMCS6': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QG - Coherent Select)',
    'WL3EMDM16QAMCS4': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QM - Coherent Select)',
    'WL3EMDM16QAMCS5': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QN - Coherent Select)',
    'WL3NMDMAMP4ASKCLD': 'WL3n Modem Amplified 4ASK 1xOTU4 C-Band (Colored)',
    'WL3EMDMUNAMP16QAMCLS': 'WL3e Modem Un-Amplified 16QAM 2xOTU4 C-Band (Colorless)',
    'FOREIGNCOHERENT': 'Foreign Coherent',
    'WLAI56GBAUD400GCS': 'WL Ai Modem 56Gbaud 400GBit/s (Coherent Select)',
    'FLEX2WL3MDMQPSKCNTLS4': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BN - Contentionless)',
    'FLEX2WL3MDMQPSKCNTLS3': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BE - Contentionless)',
    'FLEX2WL3MDMQPSKCNTLS2': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BH - Contentionless)',
    'FLEX2WL3MDMQPSKCNTLS1': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BB - Contentionless)',
    'XFPDWDM10G7RS8': 'DWDM XFP 10.7G RS8 (NTK587XX; NTK588XX; NTK589XX; NTK583AA)',
    'WLAI35GBAUD150GCNTLS': 'WL Ai Modem 35Gbaud 150GBit/s (Contentionless)',
    'WL3MDMQPSKCLD3B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UL - Colored)',
    'WL3EMDMQPSKCLS1B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UJ - Colorless)',
    'WL3EMDMUNAMP16QAMCNTLS': 'WL3e Modem Un-Amplified 16QAM 2xOTU4 C-Band (Contentionless)',
    'OM5KMOTRERGBE10G7URS8': '565/5100/5200 MOTR 10G GbE Extended Reach 10.7G RS8 (NT0H86xx)',
    'EDC40GWVSELOCLDSUBCLD': 'eDC40G Wave-Sel OCLD Submarine 1xOTU3+ C-Band (NTK539RE - Colored)',
    'WL3EMDMQPSKCLS6A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QG - Colorless)',
    'OM5KTMOTROTN10G7RS8': '565/5100/5200 MOTR OTN 4xOC48/OTU1 Tunable 10.7G RS8 (NT0H87AZ)',
    'EDC40GWVSELOCLDCLD': 'eDC40G Wave-Sel OCLD 1xOTU3+ C-Band (NTK539RB - Colored)',
    'OMETOTR10G7RS8': '6500 DWDM Tunable OTR 1xOC192/STM64  1x10.7G RS8 FEC (NTK530MA)',
    'WL3EMDMUNAMPQPSKCNTLS': 'WL3e Modem Un-Amplified QPSK 1xOTU4 C-Band (Contentionless)',
    'EDC40GWVSELOCLDCLS': 'eDC40G Wave-Sel OCLD 1xOTU3+ C-Band (NTK539RB - Colorless)',
    'OMENGMWTOTU210G7R': '6500 NGM (eDCO) WT 1xOTU2 1x10.7G Regional (NTK530BC)',
    'EDC40GWVSELOCLDSUBCLS': 'eDC40G Wave-Sel OCLD Submarine 1xOTU3+ C-Band (NTK539RE - Colorless)',
    'FLEX2WL3OCLDLHBPSKCO': 'Flex2 WL3 OCLD Long Haul Colorless-Optimized BPSK 1xOTU4 C-Band (NTK539AB - Colorless)',
    'LBAND10G': 'DWDM XFP 10G LBAND (NTK589PA-PQ)',
    'WL3NMDM4ASKCLS': 'WL3n Modem 4ASK 1xOTU4 C-Band (Colorless)',
    'WLAI56GBAUD150GCS': 'WL Ai Modem 56Gbaud 150GBit/s (Coherent Select)',
    'OM5KTMOTRGBE10G7RS8': '565/5100/5200 MOTR 10G Tunable GbE 10.7G RS8 (NT0H84AZ)',
    'WL3EMDMQPSKCLS8A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539BN - Colorless)',
    'WL3IMDM4ASKCLS2': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BM - Colorless)',
    'WL3IMDM4ASKCLS1': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BL - Colorless)',
    'WL3NMDMQPSKCS': 'WL3n Modem QPSK 1xOTU4 C-Band (Coherent Select)',
    'WL3NMDM4ASKCLD': 'WL3n Modem 4ASK 1xOTU4 C-Band (Colored)',
    'FLEX3WL3MDMQPSKCLD4A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QM - Colored)',
    'OMENGMWT10GBE11G1R': '6500 NGM (eDCO) WT 1x10GE LAN 1x11.1G Regional (NTK530BB; NTK530BY)',
    'WL3EMDMBPSKCLS8': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539BN - Colorless)',
    'FLEX2WL3OCLDPRMBPSKCLD': 'Flex2 WL3 OCLD Premium Long Haul BPSK 1xOTU4 C-Band (NTK539BH - Colored)',
    'WL3EMDMBPSKCLS1': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QJ - Colorless)',
    'WL3EMDMBPSKCLS2': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QK - Colorless)',
    'WL3NMDMQPSKCNTLS3': 'WL3n Modem QPSK 1xOTU4 C-Band (NTK538BK - Contentionless)',
    'WL3EMDMBPSKCLS5': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QN - Colorless)',
    'WL3EMDMBPSKCLS6': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QG - Colorless)',
    'FLEX2WL3OCLDPRMBPSKCLS': 'Flex2 WL3 OCLD Premium Long Haul BPSK 1xOTU4 C-Band (NTK539BH - Colorless)',
    'WL3EMDMQPSKCNTLS11A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UL - Contentionless)',
    'FLEX3WL3MDM16QAMCLS4': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QM - Colorless)',
    'FLEX3WL3MDM16QAMCLS5': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QN - Colorless)',
    'FLEX3WL3MDM16QAMCLS6': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QG - Colorless)',
    'FLEX3WL3MDM16QAMCLS1': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QJ - Colorless)',
    'FLEX3WL3MDM16QAMCLS2': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QK - Colorless)',
    'FLEX3WL3MDM16QAMCLS3': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QL - Colorless)',
    'WL3EMDMQPSKCNTLS1A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QJ; 134-5550-901 - Contentionless)',
    'WL3EMDMQPSKCNTLS1B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UJ - Contentionless)',
    'WL3EMDMQPSKCLD': 'WL3e Modem QPSK 1xOTU4 C-Band (Colored)',
    'EDC40GWVSELOCLDMHCLS': 'eDC40G Wave-Sel OCLD MetroHSRx 1xOTU3+ C-Band (NTK539RF - Colorless)',
    'SCMD4 SCMD4RXTXTYPE': 'Connection to/from a CMD4 or SCMD4 (NTT810AA-AJ; NTT810BA-BJ; NTT810CA-CJ; NTK508AA-AJ)',
    'XFPT8DWDM10G7RS8': '8-Wavelength Tunable DWDM XFP 10.7G RS8',
    'WL3EMDMQPSKCLD3A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QL; 134-5550-900 - Colored)',
    'WL3EMDMQPSKCLD3B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UL - Colored)',
    'WLCFP2ACOA4ASKCLD': 'WL CFP2-ACO A 4ASK 1xOTU4 C-Band (Colored)',
    'EDC100GWL3OCLDRNGCLD': '100G WaveLogic 3 OCLD Regional 1xOTU4 C-Band noGCC (NTK539UX - Colored)',
    'WL3EMDMQPSKCS9': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UJ - Coherent Select)',
    'XFPDWDM_NTK583AA': 'Multirate 1528.38 to 1568.77 50GHz DWDM XFP (NTK583AA; NTK583AC; 160-9002-900)',
    'XFPDWDM_NTK583AB': 'Multirate 1528.38 to 1568.77 50GHz Type 2 DWDM XFP (NTK583AB; 160-9004-900)',
    'FLEX3WL3MDMQPSKCLS2A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QK - Colorless)',
    'OM5KTMOTROTN10G7SCFEC': '565/5100/5200 MOTR OTN 4xOC48/OTU1 Tunable 10.7G SCFEC (NT0H87AZ)',
    'XFPDWDM10G7EFEC': 'DWDM XFP 10.7G EFEC',
    'OSICSUPERVISORYIDLER': 'OSIC Sup/Idler C-Band 1x10G DWDM (NTK528XA)',
    'WL3EMDMQPSKCNTLS8A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539BN - Contentionless)',
    'EDC100GWL3OCLDSUBCLS': '100G WaveLogic 3 OCLD Submarine 1xOTU4 C-Band (NTK539UE - Colorless)',
    'EDC100GWL3OCLDSUBCLD': '100G WaveLogic 3 OCLD Submarine 1xOTU4 C-Band (NTK539UE - Colored)',
    'WLAI56GBAUD100GCLS': 'WL Ai Modem 56Gbaud 100GBit/s (Colorless)',
    'OM5KTOTR10G10G7RS8': '565/5100/5200 OTR 10G Enhanced Tunable 10.7G (NT0H83AZ)',
    'LH10GWTSFEC': 'LH 1600T 10 G WT (w/SFEC) (NTCF07xx)',
    'DT10GCMB': 'DT Single 10G combiner (NTU540xx)',
    'WL3NMDM4ASKCLD2': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BM - Colored)',
    'WL3NMDM4ASKCLD1': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BL - Colored)',
    'WL3EMDMQPSKCLS2A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QK - Colorless)',
    'WL3EMDMQPSKCLS2B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UK - Colorless)',
    'FLEX2WL3MDMQPSKCLD': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (Colored)',
    'OTHER': 'Foreign',
    'FLEX2WL3MDMQPSKCLS': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (Colorless)',
    'OMETOTSCOTU211G1SCFEC': '6500 OTSC Tunable 1xOTU2 1x11.1G SCFEC (NTK528AA)',
    'OMENGMWT10GBE11G1': '6500 NGM (eDCO) WT 1x10GE LAN 1x11.1G (NTK530AB)',
    'OM5K10GMOTR': '565/5100/5200 MOTR 10G GbE/FC (NT0H84[A-J][A-D]; w/VCAT: NT0H84[A-J][E-H])',
    'OMETOTR10GBE11G1SCFEC': '6500 DWDM Tunable OTR 1x10GE LAN 11.1G SCFEC (NTK530MA)',
    'OMESMUX10G7RS8': '6500 SuperMux 10G DWDM Tunable 10.7G RS8 FEC (NTK535EA; NTK535EB)',
    'EDC100GWL2MDM': 'eDC100G Modem 1xOTU4+ C-Band',
    'WL3MDMQPSKCNTLS5B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UN - Contentionless)',
    'EDC40GWVSELOCLDMCLD': 'eDC40G Wave-Sel OCLD Metro 1xOTU3+ C-Band (NTK539RD - Colored)',
    'FLEX3WL3MDMQPSKCLS1A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QJ - Colorless)',
    'WLCFP2ACOAQPSKCNTLS': 'WL CFP2-ACO A QPSK 1xOTU4 C-Band (Contentionless)',
    'WL3EMDMQPSKCNTLS5A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QN; 134-5550-905 - Contentionless)',
    'WL3EMDMQPSKCNTLS5B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UN - Contentionless)',
    'WL3EMDMQPSKCS3B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UL - Coherent Select)',
    'EDC40GWVSELOCLDMCLS': 'eDC40G Wave-Sel OCLD Metro 1xOTU3+ C-Band (NTK539RD - Colorless)',
    'XFPDWDM11G1RS8': 'DWDM XFP 11.1G RS8 (NTK587XX; NTK588XX; NTK589XX; NTK583AA)',
    'WLCFP2ACOCQPSKCLS': 'WL CFP2-ACO C QPSK 1xOTU4 C-Band (Colorless)',
    'WL3IMDMQPSKCLSPC3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Colorless Pre-Comp)',
    'WLAI56GBAUD400GCLS': 'WL Ai Modem 56Gbaud 400GBit/s (Colorless)',
    'WL3EMDM16QAMCS': 'WL3e Modem 16QAM 2xOTU4 C-Band (Coherent Select)',
    'WL3EMDMQPSKCLD6A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QG - Colored)',
    'XFPDWDM_NTK588AA_DV': 'Multirate DWDM XFP (NTK588AA_DV)',
    'FLEX3WL3MDMQPSKCLS5A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QN - Colorless)',
    'OMEEDC40GOCLDR': '6500 eDC40G OCLD 1xOTU3+ DWDM Regional (NTK539PC)',
    'WLCFP2ACOAQPSKCLS': 'WL CFP2-ACO A QPSK 1xOTU4 C-Band (Colorless)',
    'WL3EMDMQPSKCLS10A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UK - Colorless)',
    'OM5KOTR10GBE11G1': '565/5100/5200 OTR 10G Enhanced (CPL) 10GbE 11.1G (NT0H83[A-J][A-D])',
    'WLAI56GBAUD350GCLS': 'WL Ai Modem 56Gbaud 350GBit/s (Colorless)',
    'OMEEDC40GOCLDM': '6500 eDC40G OCLD 1xOTU3+ DWDM Metro (NTK539PD)',
    'OMETOTSCOTU210G7RS8': '6500 OTSC Tunable 1xOTU2 1x10.7G RS8 FEC (NTK528AA)',
    'SFPDWDM_NTK585AA_DU': 'Multirate DWDM SFP (NTK585AA_DU)',
    'FLEX2WL3OCLDPRMBPSKCO': 'Flex2 WL3 OCLD Premium Long Haul Colorless-Optimized BPSK 1xOTU4 C-Band (NTK539AH - Colorless)',
    'EDC100GWL3MDMCLS': 'WL3 Modem 1xOTU4 C-Band (Colorless)',
    'FLEX3WL3MDMQPSKCLS6A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QG - Colorless)',
    'OMENGMWTOTU210G7REXT': '6500 NGM (eDCO) WT 1xOTU2 1x10.7G Regional EXT PWR (NTK530BC)',
    'OMETOTSC10GBE10G7SCFEC': '6500 DWDM Tunable OTSC 1x10GE LAN 10.7G SCFEC (NTK528AA)',
    'FLEX2WL3MDMQPSKCLD4': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (NTK539BN - Colored)',
    'EDC100GWL3MDMCLD': 'WL3 Modem 1xOTU4 C-Band (Colored)',
    'WL3EMDMQPSKCLS9A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UJ - Colorless)',
    'OM5KUOTR10G10G7RS8': '565/5100/5200 OTR 10G Ultra 10.7G RS8 (NT0H85xx)',
    'FLEX3WL3MDMQPSKCLD5A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QN - Colored)',
    'EDC100GWL2MDMER': 'eDC100G ER Modem 1xOTU4 C-band',
    'FLEX3WL3MDMBPSKCLS6': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QG - Colorless)',
    'FLEX3WL3MDMBPSKCLS5': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QN - Colorless)',
    'WLCFP2ACOCQPSKCNTLS': 'WL CFP2-ACO C QPSK 1xOTU4 C-Band (Contentionless)',
    'FLEX3WL3MDMBPSKCLS2': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QK - Colorless)',
    'FLEX3WL3MDMBPSKCLS1': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QJ - Colorless)',
    'LBAND40G': 'eDC40G OCLD OCLD MetroHSRx 1xOTU3+ DWDM L-Band (NTK539P[P-U])',
    'FLEX2WL3OCLDSUBBPSKCLD': 'Flex2 WL3 OCLD Submarine BPSK 1xOTU4 C-Band (NTK539BE - Colored)',
    'EDC100GWL3OCLDRCLSLBAND': '100G WaveLogic 3 OCLD Regional 1xOTU4 L-Band (NTK539UR - Colorless)',
    'XFPDWDM10G7SCFEC': 'DWDM XFP 10.7G SCFEC',
    'EDC100GOCLDSUBER': '6500 eDC100G OCLD Subm ER 1xOTU4 C-band (NTK539TN)',
    'WL3NMDM4ASKCS2': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BM - Coherent Select)',
    'WL3NMDM4ASKCS1': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BL - Coherent Select)',
    'FLEX2WL3OCLDSUBQPSKCO': 'Flex2 WL3 OCLD Submarine Colorless-Optimized QPSK 1xOTU4 C-Band (NTK539AE - Colorless)',
    'FLEX2WL3OCLDSUBBPSKCLS': 'Flex2 WL3 OCLD Submarine BPSK 1xOTU4 C-Band (NTK539BE - Colorless)',
    'OM5KTOTR10GBE10G7': '565/5100/5200 OTR 10GBE Tunable 10.7G (NT0H83AZ)',
    'WL3NMDMQPSKCLS3': 'WL3n Modem QPSK 1xOTU4 C-Band (NTK538BK - Colorless)',
    'WL3EMDMQPSKCS4B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UM - Coherent Select)',
    'WL3EMDM16QAMCLS': 'WL3e Modem 16QAM 2xOTU4 C-Band (Colorless)',
    'EDC40GWVSELMDMCNTLS': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (Contentionless)',
    'WL3EMDM16QAMCLD': 'WL3e Modem 16QAM 2xOTU4 C-Band (Colored)',
    'EDC100GWL3MDMCS': 'WL3 Modem 1xOTU4 C-Band (Coherent Select)',
    'XFPDWDM_NTK587AA_DS': 'Multirate EML DWDM XFP (NTK587AA_DS)',
    'FLEX2WL3MDMBPSKCLD': 'Flex2 WL3 Modem BPSK 1xOTU4 C-Band (Colored)',
    'FLEX2WL3MDMBPSKCLS': 'Flex2 WL3 Modem BPSK 1xOTU4 C-Band (Colorless)',
    'WL3NMDM4ASKCNTLS': 'WL3n Modem 4ASK 1xOTU4 C-Band (Contentionless)',
    'FLEX3WL3MDMQPSKCNTLS3A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QL - Contentionless)',
    'OMETOTSCOTU211G05RS8': '6500 OTSC Tunable 1xOTU2 1x11.05G RS8 FEC  (NTK528AA)',
    'UNKNOWN': 'UNKNOWN',
    'OMETOTSCFC120011G3RS8': '6500 OTSC Tunable FC1200 11.3G RS8 FEC (NTK528AA)',
    'FLEX2WL3MDMQPSKCNTLS': 'Flex2 WL3 Modem QPSK 1xOTU4 C-Band (Contentionless)',
    'WL3NMDM4ASKCLS2': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BM - Colorless)',
    'WL3NMDM4ASKCLS1': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BL - Colorless)',
    'EDC100GWL3OCLDMCLS': '100G WaveLogic 3 OCLD Metro 1xOTU4 C-Band (NTK539UD - Colorless)',
    'EDC100GWL3OCLDMCLD': '100G WaveLogic 3 OCLD Metro 1xOTU4 C-Band (NTK539UD - Colored)',
    'WLAI35GBAUD100GCS': 'WL Ai Modem 35Gbaud 100GBit/s (Coherent Select)',
    'WL3EMDMQPSKCNTLS9A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UJ - Contentionless)',
    'OMETOTSCOTU210G7SCFEC': '6500 OTSC Tunable 1xOTU2 1x10.7G SCFEC (NTK528AA)',
    'WLAI56GBAUD350GCS': 'WL Ai Modem 56Gbaud 350GBit/s (Coherent Select)',
    'WLAI35GBAUD250GCLD': 'WL Ai Modem 35Gbaud 250GBit/s (Colored)',
    'WL3MDMQPSKCLD4B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UM - Colored)',
    'XFPDWDM_NTK587EA_HU': 'Multirate EML DWDM 800ps/nm XFP (NTK587EA_HU)',
    'OMETOTSCOTU211G1RS8': '6500 OTSC Tunable 1xOTU2 1x11.1G RS8 FEC  (NTK528AA)',
    'WLAI35GBAUD250GCLS': 'WL Ai Modem 35Gbaud 250GBit/s (Colorless)',
    'HDX10GSR': 'HDX (_C) 4 X 10G SR TR (Legacy)',
    'LH10GWTSR': 'LH 1600T 10G SR WT with TriFEC (Legacy)',
    'WL3EMDMQPSKCLS7A': 'WL3e Modem 1xOTU4 C-Band (NTK539UN - Colorless)',
    'WLAI35GBAUD150GCLS': 'WL Ai Modem 35Gbaud 150GBit/s (Colorless)',
    'WLAI35GBAUD200GCS': 'WL Ai Modem 35Gbaud 200GBit/s (Coherent Select)',
    'WL3EMDMQPSKCS11': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UL - Coherent Select)',
    'WL3EMDMQPSKCLS1A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QJ; 134-5550-901 - Colorless)',
    'WLAI35GBAUD150GCLD': 'WL Ai Modem 35Gbaud 150GBit/s (Colored)',
    'EDC100GWL3OCLDRCLDLBAND': '100G WaveLogic 3 OCLD Regional 1xOTU4 L-Band (NTK539UR - Colored)',
    'WL3EMDM16QAMCNTLS5': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QN; 134-5550-905 - Contentionless)',
    'WL3EMDM16QAMCNTLS4': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QM; 134-5550-904 - Contentionless)',
    'WL3EMDM16QAMCNTLS6': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QG - Contentionless)',
    'WL3EMDM16QAMCNTLS1': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QJ; 134-5550-901 - Contentionless)',
    'WL3EMDM16QAMCNTLS3': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QL; 134-5550-900 - Contentionless)',
    'WL3EMDM16QAMCNTLS2': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QK - Contentionless)',
    'HDX10GDWDM': 'HDX (_C) 4 X 10G DWDM TR (Optical Modules: NTUC32[B-Z][P-Q]; Quad Carrier: NTUC32AA)',
    'SFPP_1609201900_RS8': 'Multirate; 1528.38-1568.77nm Tunable; 50GHz; Type 1 DWDM; SFP+ RS8 (160-9201-900)',
    'WLAI56GBAUD100GCS': 'WL Ai Modem 56Gbaud 100GBit/s (Coherent Select)',
    'FLEX3WL3MDMQPSKCLD1A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QJ - Colored)',
    'OM350010G': 'OM3500 10G (NT445xx)',
    'XFPT8DWDM11G1EFEC': '8-Wavelength Tunable DWDM XFP 11.1G EFEC',
    'XFPT8DWDM10G7SCFEC': '8-Wavelength Tunable DWDM XFP 10.7G SCFEC',
    'OMETOTSC10GBE11G1SCFEC': '6500 DWDM Tunable OTSC 1x10GE LAN 11.1G SCFEC (NTK528AA)',
    'FLEX3WL3MDMQPSKCNTLS6A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QG - Contentionless)',
    'OM5K25GOCLD': '565/5100/5200 OCLD 2.5G Flex (NT0H80xx)',
    'WL3EMDMQPSKCNTLS2A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QK - Contentionless)',
    'WL3EMDMQPSKCNTLS2B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UK - Contentionless)',
    'WL3NMDM4ASKCNTLS2': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BM - Contentionless)',
    'WL3NMDM4ASKCNTLS1': 'WL3n Modem 4ASK 1xOTU4 C-Band (NTK538BL - Contentionless)',
    'WL3EMDMQPSKCLD2B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UK - Colored)',
    'WL3EMDMQPSKCLD2A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QK - Colored)',
    'HDX25GIR': 'HDX (_C) 16 X 2.5G IR TR (Legacy)',
    'WL3MDMQPSKCNTLS2B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UK - Contentionless)',
    'OMETOTSC10G7RS8': '6500 DWDM Tunable OTSC 1xOC192/STM64 1x10.7G RS8 FEC (NTK528AA)',
    'WL3IMDM4ASKCNTLS2': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BM - Contentionless)',
    'WL3IMDM4ASKCNTLS1': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BL - Contentionless)',
    'WL3NMDMQPSKCS3': 'WL3n Modem QPSK 1xOTU4 C-Band (NTK538BK - Coherent Select)',
    'WL3MDMQPSKCLS3B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UL - Colorless)',
    'EDC40GMDM': 'eDC40G Modem 1xOTU3+ C-Band',
    'EDC100GOCLDRER': '6500 eDC100G OCLD Regional ER 1xOTU4 C-band (NTK539TL)',
    'OMETOTSC10GBE11G1RS8': '6500 DWDM Tunable OTSC 1x10GE LAN 11.1G RS8 FEC (NTK528AA)',
    'OM5KUOTRFC120011G3': '565/5100/5200 OTR 10G Ultra FC1200 11.3G (NT0H85xx)',
    'XFPT8DWDM10G7EFEC': '8-Wavelength Tunable DWDM XFP 10.7G EFEC',
    'WL3EMDM8QAMCNTLS': 'WL3e Modem 8QAM NxOTU4 C-Band (Contentionless)',
    'WL3EMDMUNAMP8QAMCLD': 'WL3e Modem Un-Amplified 8QAM NxOTU4 C-Band (Colored)',
    'WLCFP2ACOBQPSKCNTLS': 'WL CFP2-ACO B QPSK 1xOTU4 C-Band (Contentionless)',
    'WLCFP2ACOCQPSKCLD': 'WL CFP2-ACO C QPSK 1xOTU4 C-Band (Coloured)',
    'WLAI56GBAUD200GCLS': 'WL Ai Modem 56Gbaud 200GBit/s (Colorless)',
    'WL3MDMQPSKCLD1B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UJ - Colored)',
    'OMETOTSC10GBE10G7RS8': '6500 DWDM Tunable OTSC 1x10GE LAN 10.7G RS8 FEC (NTK528AA)',
    'WLCFP2ACOBQPSKCLD': 'WL CFP2-ACO B QPSK 1xOTU4 C-Band (Coloured)',
    'SCMD8 SCMD8RXTXTYPE': 'Connection to/from a SCMD8 (NTT861AA-AJ; NTT861BA-BJ)',
    'OMETOTR10GSSWSCFEC': '6500 DWDM Tunable OTR 1xOC192/STM64 1x10.7G SCFEC (NTK530MA)',
    'WLCFP2ACOBQPSKCLS': 'WL CFP2-ACO B QPSK 1xOTU4 C-Band (Colorless)',
    'WLCFP2ACOA4ASKCLS': 'WL CFP2-ACO A 4ASK 1xOTU4 C-Band (Colorless)',
    'EDC100GWL3OCLDPCLDLBAND': '100G WaveLogic 3 OCLD Premium 1xOTU4 L-Band (NTK539UP - Colored)',
    'WL3EMDMQPSKCLD10A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UK - Colored)',
    'WL3EMDMBPSKCS': 'WL3e Modem BPSK 1xOTU4 C-Band (Coherent Select)',
    'OMETOTSC10GBE11G05RS8': '6500 DWDM Tunable OTSC 1x10GE LAN 11.05G RS8 FEC (NTK528AA)',
    'EDC100GWL3MDMCS1': '100G WL3 Modem 1xOTU4 C-Band (NTK539UA - Coherent Select)',
    'EDC100GWL3MDMCS2': '100G WL3 Modem 1xOTU4 C-Band (NTK539UH - Coherent Select)',
    'EDC100GWL3MDMCS3': '100G WL3 Modem 1xOTU4 C-Band (NTK539UB - Coherent Select)',
    'EDC100GWL3MDMCS4': '100G WL3 Modem 1xOTU4 C-Band (NTK539UC - Coherent Select)',
    'EDC100GWL3MDMCS5': '100G WL3 Modem 1xOTU4 C-Band (NTK539UD - Coherent Select)',
    'EDC100GWL3MDMCS6': '100G WL3 Modem 1xOTU4 C-Band (NTK539UE - Coherent Select)',
    'EDC100GWL3MDMCS7': '100G WL3 Modem 1xOTU4 C-Band (NTK539UX - Coherent Select)',
    'FLEX3WL3MDMBPSKCLD2': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QK - Colored)',
    'FLEX3WL3MDMBPSKCLD1': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QJ - Colored)',
    'FLEX3WL3MDMBPSKCLD6': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QG - Colored)',
    'WL3EMDMQPSKCLS3B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UL - Colorless)',
    'WL3EMDMQPSKCLS3A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QL; 134-5550-900 - Colorless)',
    'FLEX3WL3MDMBPSKCLD5': 'Flex3 WL3 Modem BPSK 1xOTU4 C-Band (NTK539QN - Colored)',
    'OMETDWDM10G7RS8AM': '6500 DWDM 1xOC-192/STM64 G.709 HO/LO TUNABLE AM1 AM2 RS8 FEC (HO: NTK526[A-N]x; LO: NTK527[A-N]x)',
    'SFPDWDM_NTK586AA_HW_2G5': 'Multirate DWDM SFP 2.5G (NTK586AA_HW)',
    'SFPDWDM_NTK586AA_HW_2G7': 'Multirate DWDM SFP 2.7G (NTK586AA_HW)',
    'EDC100GOCLDER': '6500 eDC100G OCLD LH ER 1xOTU4 C-band (NTK539TK)',
    'WLAI56GBAUD300GCLS': 'WL Ai Modem 56Gbaud 300GBit/s (Colorless)',
    'WL3EMDMQPSKCS5B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UN - Coherent Select)',
    'OMETOTR10GBE11G1RS8': '6500 DWDM Tunable OTR 1x10GE LAN 11.1G RS8 FEC (NTK530MA)',
    'WL3MDMQPSKCNTLS3B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UL - Contentionless)',
    'FLEX3WL3MDMQPSKCNTLS2A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QK - Contentionless)',
    'WL3MDMQPSKCNTLS4B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UM - Contentionless)',
    'WL3IMDM4ASKCLD2': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BM - Colored)',
    'WL3EMDMQPSKCS': 'WL3e Modem QPSK 1xOTU4 C-Band (Coherent Select)',
    'OMEEDC40GOCLDHSRX': '6500 eDC40G OCLD 1xOTU3+ DWDM Metro HSRx (NTK539PF)',
    'WL3IMDM4ASKCLD1': 'WL3i Modem 4ASK 1xOTU4 C-Band (NTK538BL - Colored)',
    'WLAI35GBAUD200GCLS': 'WL Ai Modem 35Gbaud 200GBit/s (Colorless)',
    'WL3IMDMQPSKCNTLSPC3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Contentionless Pre-Comp)',
    'XFPDWDM_NTK589_CBAND': 'Multirate Narrow C-Band Tunable DWDM XFP (NTK589AA_MA; NTK589PR_PX; NTK589NA_NQ)',
    'WLAI35GBAUD200GCLD': 'WL Ai Modem 35Gbaud 200GBit/s (Colored)',
    'OMEEDC11G1SUB': '6500 NGM (eDCO) WT 1x10GE LAN 1x11.1G Submarine (NTK530CB)',
    'OMETOTSCFC120011G3SCFEC': '6500 OTSC Tunable FC1200 11.3G SCFEC (NTK528AA)',
    'EDC40GWVSELMDMCLD': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (Colored)',
    'EDC100GOCLDMER': '6500 eDC100G OCLD Metro ER 1xOTU4 C-band (NTK539TM)',
    'EDC100GWL3OCLDECLS': '100G WL3 OCLD Enhanced PMD 1xOTU4 C-Band (NTK539UA - Colorless)',
    'OM5K10GOTRE': '565/5100/5200 OTR Enhanced',
    'LH10GWTTFEC': 'LH 1600T 10G WT with TriFEC (NTCF07xx)',
    'EDC100GWL3OCLDECLD': '100G WL3 OCLD Enhanced PMD 1xOTU4 C-Band (NTK539UA - Colored)',
    'SFPP_1609201900': 'Multirate; 1528.38-1565.50nm Tunable; 50GHz; Type 1 DWDM; SFP+ (160-9201-900)',
    'WL3EMDM16QAMCNTLS': 'WL3e Modem 16QAM 2xOTU4 C-Band (Contentionless)',
    'OMETOTSCOTU211G05SCFEC': '6500 OTSC Tunable 1xOTU2 1x11.05G SCFEC (NTK528AA)',
    'EDC40GWVSELMDMCLS': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (Colorless)',
    'EDC100GWL3MDMCNTLS7': '100G WL3 Modem 1xOTU4 C-Band (NTK539UX - Contentionless)',
    'EDC100GWL3MDMCNTLS6': '100G WL3 Modem 1xOTU4 C-Band (NTK539UE - Contentionless)',
    'EDC100GWL3MDMCNTLS5': '100G WL3 Modem 1xOTU4 C-Band (NTK539UD - Contentionless)',
    'EDC100GWL3MDMCNTLS4': '100G WL3 Modem 1xOTU4 C-Band (NTK539UC - Contentionless)',
    'EDC100GWL3MDMCNTLS3': '100G WL3 Modem 1xOTU4 C-Band (NTK539UB - Contentionless)',
    'EDC100GWL3MDMCNTLS2': '100G WL3 Modem 1xOTU4 C-Band (NTK539UH - Contentionless)',
    'EDC100GWL3MDMCNTLS1': '100G WL3 Modem 1xOTU4 C-Band (NTK539UA - Contentionless)',
    'WL3EMDMQPSKCLD5B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UN - Colored)',
    'WL3EMDMQPSKCLD5A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QN; 134-5550-905 - Colored)',
    'WL3EMDMQPSKCNTLS6A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QG - Contentionless)',
    'FLEX3WL3MDM16QAMCLD1': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QJ - Colored)',
    'FLEX3WL3MDM16QAMCLD3': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QL - Colored)',
    'FLEX3WL3MDM16QAMCLD2': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QK - Colored)',
    'FLEX3WL3MDM16QAMCLD5': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QN - Colored)',
    'FLEX3WL3MDM16QAMCLD4': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QM - Colored)',
    'OMENGMWT10GSSW10G7EXT': '6500 NGM (eDCO) WT 1xOC-192/STM-64 1x10.7G EXT PWR (NTK530AA)',
    'FLEX3WL3MDM16QAMCLD6': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QG - Colored)',
    'FLEX2WL3OCLDSUBQPSKCLS': 'Flex2 WL3 OCLD Submarine QPSK 1xOTU4 C-Band (NTK539BE - Colorless)',
    'WL3MDMQPSKCLD5B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UN - Colored)',
    'FLEX2WL3OCLDSUBQPSKCLD': 'Flex2 WL3 OCLD Submarine QPSK 1xOTU4 C-Band (NTK539BE - Colored)',
    'XFPT8DWDM11G1SCFEC': '8-Wavelength Tunable DWDM XFP 11.1G SCFEC',
    'OMEEDC40GOCLDULHSUB': '6500 eDC40G OCLD ULH 1xOTU3+ DWDM Submarine (NTK539XE)',
    'UNKNOWN RXTX_TYPE_UNKNOWN': 'UNKNOWN',
    'FLEX2WL3OCLDLHQPSKCLS': 'Flex2 WL3 OCLD Long Haul QPSK 1xOTU4 C-Band (NTK539BB - Colorless)',
    'WL3EMDMQPSKCLS4B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UM - Colorless)',
    'WL3EMDMQPSKCLS4A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QM; 134-5550-904 - Colorless)',
    'DTREGEN': 'DT Dual Regeni NTU530xx',
    'OMENGMWTOTU210G7': '6500 NGM (eDCO) WT 1xOTU2 1x10.7G (NTK530AC)',
    'FLEX2WL3OCLDLHQPSKCLD': 'Flex2 WL3 OCLD Long Haul QPSK 1xOTU4 C-Band (NTK539BB - Colored)',
    'WLAI35GBAUD250GCNTLS': 'WL Ai Modem 35Gbaud 250GBit/s (Contentionless)',
    'OMEEDC100GOCLDR': '6500 eDC100G OCLD 1xOTU4+ DWDM Regional (NTK539TC)',
    'WL3EMDMUNAMP8QAMCS': 'WL3e Modem Un-Amplified 8QAM NxOTU4 C-Band (Coherent Select)',
    'OMEEDC100GOCLDM': '6500 eDC100G OCLD 1xOTU4+ DWDM Metro (NTK539TD)',
    'WL3EMDM16QAMCLD4': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QM; 134-5550-904 - Colored)',
    'WL3EMDM16QAMCLD5': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QN; 134-5550-905 - Colored)',
    'WL3EMDM16QAMCLD6': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QG - Colored)',
    'WL3EMDMUNAMPQPSKCLD': 'WL3e Modem Un-Amplified QPSK 1xOTU4 C-Band (Colored)',
    'WL3EMDM16QAMCLD1': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QJ; 134-5550-901 - Colored)',
    'WL3EMDM16QAMCLD2': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QK - Colored)',
    'OMEEDC40GOCLDULH': '6500 eDC40G OCLD ULH 1xOTU3+ DWDM (NTK539XA)',
    'OME2X2G5DWDMDPO': '6500 DWDM  2xOC-48/STM-16 STS/HO VT/LO DPO (DPO Modules: NTK580xx; Carriers: 2xSTS1/HO: NTK519BA; 2xVT1.5/LO: NTK520BA)',
    'OMEEDC40GOCLDEPMD': '6500 eDC40G OCLD 1xOTU3+ DWDM Enhanced PMD Comp (NTK539PA)',
    'DX10GSFEC': 'DX 10G (w/SFEC) (NTCF06[B-Z][P-Q])',
    'OMEEDC40GOCLDSUB': '6500 eDC40G OCLD 1xOTU3+ DWDM Submarine (NTK539PE)',
    'WL3EMDMQPSKCNTLS3B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UL - Contentionless)',
    'WL3EMDMQPSKCNTLS3A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QL; 134-5550-900 - Contentionless)',
    'WL3EMDMQPSKCLD1B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UJ - Colored)',
    'WL3EMDMQPSKCS1B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UJ - Coherent Select)',
    'WL3EMDMQPSKCLD1A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QJ; 134-5550-901 - Colored)',
    'WL3EMDMQPSKCLD8A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539BN - Colored)',
    'WL3MDMQPSKCLS2B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UK - Colorless)',
    'OM350025G': 'OM3500 2.5G (NT442xx)',
    'WLAI35GBAUD200GCNTLS': 'WL Ai Modem 35Gbaud 200GBit/s (Contentionless)',
    'WLAI56GBAUD250GCLS': 'WL Ai Modem 56Gbaud 250GBit/s (Colorless)',
    'FLEX2WL3OCLDSUBBPSKCO': 'Flex2 WL3 OCLD Submarine Colorless-Optimized BPSK 1xOTU4 C-Band (NTK539AE - Colorless)',
    'WL3EMDMQPSKCLD9A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UJ - Colored)',
    'FLEX3WL3MDMQPSKCLS4A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QM - Colorless)',
    'EDC100GWL3OCLDPCLD': '100G WaveLogic 3 OCLD Premium 1xOTU4 C-Band (NTK539UH - Colored)',
    'FLEX2WL3OCLDPRMQPSKCO': 'Flex2 WL3 OCLD Premium Long Haul Colorless-Optimized QPSK 1xOTU4 C-Band (NTK539AH - Colorless)',
    'EDC100GOCLDEPMDER': '6500 eDC100G OCLD Enh PMD ER 1xOTU4 C-band (NTK539TJ)',
    'EDC100GWL3OCLDPCLS': '100G WaveLogic 3 OCLD Premium 1xOTU4 C-Band (NTK539UH - Colorless)',
    'WL3IMDMQPSKCNTLS3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Contentionless)',
    'WL3EMDMBPSKCLD1': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QJ - Colored)',
    'WL3EMDMBPSKCLD2': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QK - Colored)',
    'WL3EMDMBPSKCLD5': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QN - Colored)',
    'WL3EMDMBPSKCLD6': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539QG - Colored)',
    'WL3EMDMQPSKCS10': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UK - Coherent Select)',
    'WL3EMDMBPSKCLD8': 'WL3e Modem BPSK 1xOTU4 C-Band (NTK539BN - Colored)',
    'WL3EMDMQPSKCS12': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UM - Coherent Select)',
    'DT10GWT': 'DT Dual 10G WT (Line Side) (NTU520xx)',
    'FLEX2WL3MDMBPSKCLD4': 'Flex2 WL3 Modem BPSK 1xOTU4 C-Band (NTK539BN - Colored)',
    'EDC100GWL3MDMCNTLS': 'WL3 Modem 1xOTU4 C-Band (Contentionless)',
    'XFPT8DWDM11G1RS8': '8-Wavelength Tunable DWDM XFP 11.1G RS8',
    'OMENGMWTOTU210G7EXT': '6500 NGM (eDCO) WT 1xOTU2 1x10.7G EXT PWR (NTK530AC)',
    'WL3NMDMQPSKCNTLS': 'WL3n Modem QPSK 1xOTU4 C-Band (Contentionless)',
    'WLAI35GBAUD100GCNTLS': 'WL Ai Modem 35Gbaud 100GBit/s (Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS1': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QJ - Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS2': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QK - Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS3': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QL - Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS4': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QM - Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS5': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QN - Contentionless)',
    'FLEX3WL3MDM16QAMCNTLS6': 'Flex3 WL3 Modem 16QAM 1xOTU4 C-Band (NTK539QG - Contentionless)',
    'UNDEFINED': 'RxTx Type Not Defined',
    'WLAI35GBAUD100GCLD': 'WL Ai Modem 35Gbaud 100GBit/s (Colored)',
    'OMETDWDM10G7SCFECAM': '6500 DWDM 1xOC-192/STM64 G.709 HO/LO TUNABLE AM1 AM2 wSCFEC (HO: NTK526J[A-B]; LO: NTK527J[A-B])',
    'WLCFP2ACOAQPSKCLD': 'WL CFP2-ACO A QPSK 1xOTU4 C-Band (Coloured)',
    'WLAI35GBAUD100GCLS': 'WL Ai Modem 35Gbaud 100GBit/s (Colorless)',
    'WL3EMDMQPSKCNTLS12A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UM - Contentionless)',
    'XFPDWDM10G': 'DWDM XFP 10G (NTK587XX; NTK588XX; NTK589XX; NTK583AA)',
    'SCMD4': 'Connection to/from a CMD4 or SCMD4 (NTT810AA-AJ; NTT810BA-BJ; NTT810CA-CJ; NTK508AA-AJ)',
    'EDC100GWL3OCLDRNGCLS': '100G WaveLogic 3 OCLD Regional 1xOTU4 C-Band noGCC (NTK539UX - Colorless)',
    'SCMD8': 'Connection to/from a SCMD8 (NTT861AA-AJ; NTT861BA-BJ)',
    'EDC40GULHMDM': 'eDC40G ULH Modem 1xOTU3+ C-Band',
    'FLEX3WL3MDMQPSKCLD2A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QK - Colored)',
    'FLEX2WL3OCLDLHBPSKCLD': 'Flex2 WL3 OCLD Long Haul BPSK 1xOTU4 C-Band (NTK539BB - Colored)',
    'XFPDWDM11G1SCFEC': 'DWDM XFP 11.1G SCFEC',
    'WL3MDMQPSKCLS5B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UN - Colorless)',
    'FLEX2WL3OCLDLHBPSKCLS': 'Flex2 WL3 OCLD Long Haul BPSK 1xOTU4 C-Band (NTK539BB - Colorless)',
    'WL3NMDMAMP4ASKCNTLS': 'WL3n Modem Amplified 4ASK 1xOTU4 C-Band (Contentionless)',
    'SFPDWDM_NTK586AA_HW_4G': 'Multirate DWDM SFP 4G FC400 (NTK586AA_HW)',
    'FLEX3WL3MDMQPSKCNTLS5A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QN - Contentionless)',
    'WL3MDMQPSKCNTLS1B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UJ - Contentionless)',
    'WLAI35GBAUD150GCS': 'WL Ai Modem 35Gbaud 150GBit/s (Coherent Select)',
    'FLEX3WL3MDMQPSKCLS3A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QL - Colorless)',
    'OMEEDC40GOCLD': '6500 eDC40G OCLD 1xOTU3+ DWDM (NTK539PB)',
    'LHOC48': 'LH 1600T DWDM OC-48 trib (NTCA30xx)',
    'WL3NMDMQPSKCLD': 'WL3n Modem QPSK 1xOTU4 C-Band (Colored)',
    'WL3EMDMQPSKCNTLS': 'WL3e Modem QPSK 1xOTU4 C-Band (Contentionless)',
    'WL3NMDMQPSKCLS': 'WL3n Modem QPSK 1xOTU4 C-Band (Colorless)',
    'OMETOTSC10GSSWSCFEC': '6500 DWDM Tunable OTSC 1xOC192/STM64 1x10.7G SCFEC (NTK528AA)',
    'WL3EMDMUNAMP16QAMCS': 'WL3e Modem Un-Amplified 16QAM 2xOTU4 C-Band (Coherent Select)',
    'OMEEDC100GOCLDSUB': '6500 eDC100G OCLD 1xOTU4+ DWDM Submarine (NTK539TE)',
    'EDC40GWVSELOCLDMHCLD': 'eDC40G Wave-Sel OCLD MetroHSRx 1xOTU3+ C-Band (NTK539RF - Colored)',
    'WL3EMDMQPSKCNTLS7A': 'WL3e Modem 1xOTU4 C-Band (NTK539UN - Contentionless)',
    'XFPT8DWDM10G': '8-Wavelength Tunable DWDM XFP 10G',
    'WL3EMDMQPSKCS2': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QK - Coherent Select)',
    'WL3EMDMQPSKCS3': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QL - Coherent Select)',
    'WL3EMDMQPSKCS1': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QJ - Coherent Select)',
    'WL3EMDMQPSKCS6': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QG - Coherent Select)',
    'WL3EMDMQPSKCS7': 'WL3e Modem 1xOTU4 C-Band (NTK539UN - Coherent Select)',
    'WL3EMDMQPSKCS4': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QM - Coherent Select)',
    'WL3EMDMQPSKCS5': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QN - Coherent Select)',
    'WLAI56GBAUD150GCLS': 'WL Ai Modem 56Gbaud 150GBit/s (Colorless)',
    'WL3MDMQPSKCLD2B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UK - Colored)',
    'WL3EMDMQPSKCLD11A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UL - Colored)',
    'OME10G7DWDM10G7RS8': '6500 DWDM  1xOC-192/STM64 G.709 STS/HO VT/LO (STS-1/HO: NTK526[K-N]x; VT1.5/LO: NTK527[K-N]x)',
    'WL3EMDMQPSKCLS12A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UM - Colorless)',
    'WLAI56GBAUD300GCS': 'WL Ai Modem 56Gbaud 300GBit/s (Coherent Select)',
    'OMETOTSC10GBE11G05SCFEC': '6500 DWDM Tunable OTSC 1x10GE LAN 11.05G SCFEC (NTK528AA)',
    'WLAI35GBAUD250GCS': 'WL Ai Modem 35Gbaud 250GBit/s (Coherent Select)',
    'OMENGMWT10GBE11G1REXT': '6500 NGM (eDCO) WT 1x10GE LAN 1x11.1G Regional EXT PWR (NTK530BB; NTK530BY)',
    'WL3EMDM16QAMCLS1': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QJ; 134-5550-901 - Colorless)',
    'WL3EMDM16QAMCLS3': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QL; 134-5550-900 - Colorless)',
    'WL3EMDM16QAMCLS2': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QK - Colorless)',
    'WL3EMDM16QAMCLS5': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QN; 134-5550-905 - Colorless)',
    'WL3EMDM16QAMCLS4': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QM; 134-5550-904 - Colorless)',
    'WL3EMDM16QAMCLS6': 'WL3e Modem 16QAM 2xOTU4 C-Band (NTK539QG - Colorless)',
    'WL3NMDMAMP4ASKCS': 'WL3n Modem Amplified 4ASK 1xOTU4 C-Band (Coherent Select)',
    'OMEEDC10G7SUB': '6500 NGM (eDCO) WT 1xOC192/STM64 and OTU2 1x10.7G Submarine (NTK530CA;NTK530CC)',
    'EDC100GWL3OCLDRCLD': '100G WaveLogic 3 OCLD Regional 1xOTU4 C-Band (NTK539UC - Colored)',
    'WLCFP2ACOA4ASKCNTLS': 'WL CFP2-ACO A 4ASK 1xOTU4 C-Band (Contentionless)',
    'WL3EMDMUNAMPQPSKCS': 'WL3e Modem Un-Amplified QPSK 1xOTU4 C-Band (Coherent Select)',
    'OM5KUOTR10G10G7SCFEC': '565/5100/5200 OTR 10G Ultra 10.7G SCFEC (NT0H85xx)',
    'FLEX2WL3MDMBPSKCLS4': 'Flex2 WL3 Modem BPSK 1xOTU4 C-Band (NTK539BN - Colorless)',
    'WL3EMDMQPSKCLD4A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QM; 134-5550-904 - Colored)',
    'WL3MDMQPSKCLS1B': 'WL3 Modem QPSK 1xOTU4 C-Band (NTK538UJ - Colorless)',
    'WL3EMDMQPSKCLD4B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UM - Colored)',
    'EDC100GWL3OCLDRCLS': '100G WaveLogic 3 OCLD Regional 1xOTU4 C-Band (NTK539UC - Colorless)',
    'WL3IMDMQPSKCLS3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Colorless)',
    'OMESMUX10G7SCFEC': '6500 SuperMux 10G DWDM Tunable 10.7G SCFEC (NTK535EA; NTK535EB)',
    'WL3IMDMQPSKCLDPC3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Colored Pre-Comp)',
    'FLEX3WL3MDMQPSKCNTLS1A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QJ - Contentionless)',
    'DX10GTFEC': 'DX 10G (w/TriFEC) (NTCF06[B-Z][P-Q])',
    'OMENGMWT10GSSW10G7R': '6500 NGM (eDCO) WT 1xOC-192/STM-64 1x10.7G Regional (NTK530BA; NTK530BX)',
    'WL3EMDMQPSKCLS11A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UL - Colorless)',
    'WL3EMDM8QAMCS': 'WL3e Modem 8QAM NxOTU4 C-Band (Coherent Select)',
    'XFPDWDM11G1EFEC': 'DWDM XFP 11.1G EFEC',
    'OM5K10GOTR': '565/5100/5200 OTR 10G Enhanced (CPL) (NT0H83[A-J][A-D])',
    'WL3EMDMUNAMP8QAMCNTLS': 'WL3e Modem Un-Amplified 8QAM NxOTU4 C-Band (Contentionless)',
    'WL3EMDMUNAMPQPSKCLS': 'WL3e Modem Un-Amplified QPSK 1xOTU4 C-Band (Colorless)',
    'WL3NMDM4ASKCS': 'WL3n Modem 4ASK 1xOTU4 C-Band (Coherent Select)',
    'EDC40GWVSELOCLDEPMDCLD': 'eDC40G Wave-Sel OCLD Enh PMD Comp 1xOTU3+ C-Band (NTK539RA - Colored)',
    'EDC100GWL3OCLDPCLSLBAND': '100G WaveLogic 3 OCLD Premium 1xOTU4 L-Band (NTK539UP - Colorless)',
    'EDC40GWVSELMDMCNTLS1': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (NTK539RA - Contentionless)',
    'EDC40GWVSELMDMCNTLS2': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (NTK539RB - Contentionless)',
    'EDC40GWVSELMDMCNTLS3': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (NTK539RC - Contentionless)',
    'EDC40GWVSELMDMCNTLS4': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (NTK539RD - Contentionless)',
    'EDC40GWVSELMDMCNTLS5': 'eDC40G Wave-Sel Modem 1xOTU3+ C-Band (NTK539RE - Contentionless)',
    'WL3EMDMQPSKCLD12A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539UM - Colored)',
    'EDC40GWVSELOCLDEPMDCLS': 'eDC40G Wave-Sel OCLD Enh PMD Comp 1xOTU3+ C-Band (NTK539RA - Colorless)',
    'WL3EMDMBPSKCLD': 'WL3e Modem BPSK 1xOTU4 C-Band (Colored)',
    'FLEX2WL3OCLDPRMQPSKCLS': 'Flex2 WL3 OCLD Premium Long Haul QPSK 1xOTU4 C-Band (NTK539BH - Colorless)',
    'WLAI56GBAUD250GCS': 'WL Ai Modem 56Gbaud 250GBit/s (Coherent Select)',
    'WL3EMDMBPSKCLS': 'WL3e Modem BPSK 1xOTU4 C-Band (Colorless)',
    'FLEX2WL3OCLDPRMQPSKCLD': 'Flex2 WL3 OCLD Premium Long Haul 1xOTU4 C-Band (NTK539BH - Colored)',
    'WL3IMDMQPSKCLD3': 'WL3i Modem QPSK 1xOTU4 C-Band (NTK538BK - Colored)',
    'EDC40GWVSELOCLDRCLS': 'eDC40G Wave-Sel OCLD Regional 1xOTU3+ C-Band (NTK539RC - Colorless)',
    'WL3EMDMQPSKCLS5A': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK539QN; 134-5550-905 - Colorless)',
    'WL3EMDMQPSKCLS5B': 'WL3e Modem QPSK 1xOTU4 C-Band (NTK538UN - Colorless)',
    'EDC40GWVSELOCLDRCLD': 'eDC40G Wave-Sel OCLD Regional 1xOTU3+ C-Band (NTK539RC - Colored)',
    'FLEX3WL3MDMQPSKCLD6A': 'Flex3 WL3 Modem QPSK 1xOTU4 C-Band (NTK539QG - Colored)',
    'EDC40GULHMDMSUB': 'eDC40G ULH Modem 1xOTU3+ C-Band Submarine'}
    try:
        f1 = d_TX_TRANSLATE[code]
    except KeyError:
        f1 = 'Not in R12.1'

    return f1


def PARSE_RTRV_ADJ_TX(linesIn, dTxADJACENCY, dMEMBERS, TID, F_Out, F_ERROR):
    d_ADJTXTYPE_PR_DSC__AID = {}
    F_Out.write('TID,Shelf ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit ID,Wavelength,Discovered Wavelength,Frequency (THz),Discovered Frequency (THz),PState,Discovered FE Address,Discovered FE Address Format,Provisioned FE Address,Provisioned FE Address Format,CLFI,Port Label,Mate Info,Provisioned PEC,Discovered PEC,Provisioned Tx Code, Discovered Tx Code,Provisioned Tx type,Discovered Tx Type,Modulation Class,Rate,Min Tx Power,Current Tx Power,Max Tx Power,Tx FEC Gain,Transmission Mode,Discovered Transmission Mode,Line Type,Tx Power Reduced State,Discovered Tx Reduced State,Low Frequency Guard band (THz),High Frequency Guard Band (GHz),Tx SNR Bias,Tx Tuning Resolution,Tx 3dB BW (GHz),Tx 10dB BW (GHz),TX Min Spectral Width (GHz),Allocated NMC Spectral Width (GHz),Laser Centering Mode,Dicovered Laser Centering Mode,Laser Centering Range (GHz),Discovered Laser Centering Range (GHz),Controlled Frequency Offset (GHz),Target power into CCMD,Transponder Discovered Tx Power,Channel Status,DOC Care,Active Flag,Paired Rx,Express Delete,Autodiscovery,SPLI Auto-Tuning,Transponder Tuned,Transponder SPLI,Notes,\n')
    fErr = ''
    for line in linesIn:
        if line.find('EXPRESSDELETE=') > -1:
            NOTES = ''
            l1 = line.find('::')
            f1 = line[:l1]
            AID = f1.replace('   "', '')
            f1 = AID.replace('ADJ-', '')
            l1 = f1.find('-')
            f2 = f1[0:l1]
            SHELF = 'SHELF-' + f2
            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            fOTS = ',,,,'
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    fOTS = j[0]
                    break

            line = line[:-2] + ','
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            WAVE = FISH(line, 'WAVELENGTH=', ',')
            if line.find('MATE') > -1:
                DWAVE = FISH(line, 'DISCWAVELENGTH=', ',')
            else:
                DWAVE = FISH(line, 'DISCWAVELENGTH=', '"')
            try:
                REST = dTxADJACENCY[AID]
            except KeyError:
                REST = ',,,,,'

            lREST = REST.split(',')
            TXTYPE = FISH(line, 'ADJTXTYPE=', ',')
            TXTYPE = TXTYPE.replace('\\"', '')
            TX_Translated = GET_CP_DESCRIPTION(TXTYPE)
            DTXTYPE = FISH(line, 'DISCTYPE=', ',')
            DTXTYPE = DTXTYPE.replace('\\"', '')
            DTX_Translated = GET_CP_DESCRIPTION(DTXTYPE)
            FREQUENCY = FISH(line, 'FREQUENCY=', ',')
            DISCFREQUENCY = FISH(line, 'DISCFREQUENCY=', ',')
            PORTLABEL = FISH(line, 'PORTLABEL=\\"', '\\"')
            MATEINFO = FISH(line, 'MATEINFO=\\"', '\\"')
            PROVFEPEC = FISH(line, 'PROVFEPEC=\\"', '\\"')
            DISCFEPEC = FISH(line, 'DISCFEPEC=\\"', '\\"')
            TRANSMODE = FISH(line, ',TRANSMODE=\\"', '\\",')
            DISCTXTRANSMODE = FISH(line, ',DISCTXTRANSMODE=\\"', '\\"')
            OCHLINETYPE = FISH(line, 'OCHLINETYPE=', ',')
            OCHTXB = FISH(line, 'OCHTXB=', ',')
            DISCOCHTXB = FISH(line, 'DISCOCHTXB=', ',')
            MINFREQGUARDBAND = FISH(line, 'MINFREQGUARDBAND=', ',')
            MAXFREQGUARDBAND = FISH(line, 'MAXFREQGUARDBAND=', ',')
            ADJTXBIAS = FISH(line, 'ADJTXBIAS=', ',')
            FREQRESOLUTION = FISH(line, 'FREQRESOLUTION=', ',')
            TXSIGBW3DB = FISH(line, 'TXSIGBW3DB=', ',')
            TXSIGBW10DB = FISH(line, 'TXSIGBW10DB=', ',')
            TXMINSPECTRALWIDTH = FISH(line, 'TXMINSPECTRALWIDTH=', ',')
            ALLOCSPECWIDTH = FISH(line, 'ALLOCSPECWIDTH=', ',')
            LASERCENTERING = FISH(line, 'LASERCENTERING=', ',')
            DISCLASERCENTERING = FISH(line, 'DISCLASERCENTERING=', ',')
            LASERCENTRANGE = FISH(line, 'LASERCENTRANGE=', ',')
            DISCLASERCENTRANGE = FISH(line, 'DISCLASERCENTRANGE=', ',')
            CTRLFREQOFFSET = FISH(line, 'CTRLFREQOFFSET=', ',')
            TARGINPWR = FISH(line, 'TARGINPWR=', ',')
            DISCTXPROVPWR = FISH(line, 'DISCTXPROVPWR=', ',')
            TXTUNED = FISH(line, 'TXTUNED=', ',')
            DISCSPLIMGMT = FISH(line, 'DISCSPLIMGMT=', ',')
            MODCLASS = FISH(line, 'ADJTXMODCLASS=', ',')
            RATE = FISH(line, 'RATE=', ',')
            ADJTXMINPOW = FISH(line, 'ADJTXMINPOW=', ',')
            ADJTXCURPOW = FISH(line, 'ADJTXCURPOW=', ',')
            ADJTXMAXPOW = FISH(line, 'ADJTXMAXPOW=', ',')
            ADJTXFEC = FISH(line, 'ADJTXFEC=', ',')
            CHSTATUS = FISH(line, 'CHSTATUS=', ',')
            DOCCARE = FISH(line, 'DOCCARE=', ',')
            ACTIVE = FISH(line, 'ACTIVE=', ',')
            PAIREDRX = FISH(line, 'PAIREDRX=', ',')
            EXPRESSDELETE = FISH(line, 'EXPRESSDELETE=', ',')
            AUTODISC = FISH(line, 'AUTODISC=', ',')
            SYNCPROV = FISH(line, 'SYNCPROV=', ',')
            PRI = lREST[0]
            DISCFEADDR = lREST[1]
            DADDRFORM = lREST[2]
            PROVFEADDR = lREST[3]
            PADDRFORM = lREST[4]
            if PRI == 'IS':
                if PADDRFORM != 'TID-SH-SL-PRT' and PADDRFORM != 'TID-BAY-SH-SL-PRT' and PADDRFORM != 'NODENAME-SL-PRT':
                    NOTES = NOTES + ' Tx FE format +'
                    s1 = 'IS Tx adjacency with provisioned far end format (' + PADDRFORM + ') not equal to TID-SH-SL-PRT or TID-BAY-SH-SL-PRT or NODENAME-SL-PRT'
                    fErr += ',' + AID + ',' + s1 + '\n'
                else:
                    if DADDRFORM != PADDRFORM:
                        NOTES = NOTES + ' Tx FE format +'
                        s1 = 'IS Tx adjacency having mismatched discovered (' + DADDRFORM + ') and provisioned (' + PADDRFORM + ') far end AID format'
                        fErr += ',' + AID + ',' + s1 + '\n'
                    if DISCFEADDR != PROVFEADDR:
                        NOTES = NOTES + ' Tx FE AID +'
                        s1 = 'IS Tx adjacency having mismatched  discovered (' + DISCFEADDR + ') and provisioned (' + PROVFEADDR + ') far end AID'
                        fErr += ',' + AID + ',' + s1 + '\n'
            elif DOCCARE == 'TRUE':
                if CHSTATUS == 'NOT APPLICABLE' and TXTYPE == 'UNKNOWN' and ACTIVE == 'FALSE' and CKTID == '' and DISCFEADDR == '' and DADDRFORM == 'NULL':
                    pass
                else:
                    NOTES = ' Verify this pass-through channel  '
            if CHSTATUS == 'MANAGED':
                d_ADJTXTYPE_PR_DSC__AID[AID] = TXTYPE + '+' + DTXTYPE
                if TXTYPE != DTXTYPE:
                    fErr += ',' + AID + ',Managed channel: provisioned (' + TXTYPE + ') and discovered (' + DTXTYPE + ') Tx type mismatch \n'
                    NOTES = NOTES + ' Tx Type discrepancy +'
                if WAVE != DWAVE:
                    fErr += ',' + AID + ',Managed channel: provisioned (' + WAVE + ') and discovered (' + DWAVE + ') wavelength mismatch \n'
                    NOTES = NOTES + ' Wavelength mismatch +'
                if DOCCARE == 'FALSE':
                    fErr += ',' + AID + ',Managed channel but not under DOC control \n'
                    NOTES = NOTES + ' Not under DOC care +'
                if ACTIVE == 'FALSE':
                    fErr += ',' + AID + ',Managed channel with Ingress Flag = FALSE \n'
                    NOTES = NOTES + ' Flag is FALSE +'
                if AUTODISC != 'AUTO':
                    fErr += ',' + AID + ',Managed channel: Autodiscovery is disabled \n'
                    NOTES = NOTES + ' Autodiscovery=OFF +'
                if CKTID == '':
                    fErr += ',' + AID + ',Managed channel: missing Circuit ID \n'
                    NOTES = NOTES + ' No Circuit ID +'
                if PAIREDRX != 'YES':
                    NOTES = NOTES + ' No Rx pair +'
                    fErr += ',' + AID + ',Managed channel note: Tx/Rx are not paired \n'
            if ISSUES == 'YES':
                NOTES = NOTES[:-1]
            else:
                NOTES = ''
            f2 = TID + ',' + SHELF + ',' + fOTS + ',' + AID + ',' + CKTID + ',' + WAVE + ',' + DWAVE + ',' + FREQUENCY + ',' + DISCFREQUENCY + ',' + REST + ',' + PORTLABEL + ',' + MATEINFO + ',' + PROVFEPEC + ',' + DISCFEPEC + ',' + TXTYPE + ',' + DTXTYPE + ',' + TX_Translated + ',' + DTX_Translated + ',' + MODCLASS + ',' + RATE + ',' + ADJTXMINPOW + ',' + ADJTXCURPOW + ',' + ADJTXMAXPOW + ',' + ADJTXFEC + ',' + TRANSMODE + ',' + DISCTXTRANSMODE + ',' + OCHLINETYPE + ',' + OCHTXB + ',' + DISCOCHTXB + ',' + MINFREQGUARDBAND + ',' + MAXFREQGUARDBAND + ',' + ADJTXBIAS + ',' + FREQRESOLUTION + ',' + TXSIGBW3DB + ',' + TXSIGBW10DB + ',' + TXMINSPECTRALWIDTH + ',' + ALLOCSPECWIDTH + ',' + LASERCENTERING + ',' + DISCLASERCENTERING + ',' + LASERCENTRANGE + ',' + DISCLASERCENTRANGE + ',' + CTRLFREQOFFSET + ',' + TARGINPWR + ',' + DISCTXPROVPWR + ',' + CHSTATUS + ',' + DOCCARE + ',' + ACTIVE + ',' + PAIREDRX + ',' + EXPRESSDELETE + ',' + AUTODISC + ',' + SYNCPROV + ',' + TXTUNED + ',' + DISCSPLIMGMT + ',' + NOTES + ','
            F_Out.write(f2 + '\n')

    if fErr != '':
        F_ERROR.write('\nShelf Active Channel Issues (see tab Tx_Adjacency)\n' + fErr)
    return d_ADJTXTYPE_PR_DSC__AID


def PARSE_RTRV_ADJ_RX(linesIn, dRxADJACENCY, dMEMBERS, TID, dTxADJACENCY, d_ADJTXTYPE_PR_DSC__AID, F_Out, F_ERROR):
    F_Out.write('TID,Shelf ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Wavelength,Discovered Wavelength,PState,Discovered FE Address,Discovered FE Address Format,Provisioned FE Address,Provisioned FE Address Format,CLFI,Provisioned PEC,Discovered PEC,Provisioned Frequency,Discovered Frequency,Provisioned Rx Code,Discovered Rx Code,Provisioned Rx Type,Discovered Rx Type,Rate,Min Rx Power,Max Rx power,Nominal Rx Power,Positive Rx Transient,Negative Rx Transient,Linetype,Port Label,Status,DOC Care,Paired Rx,Autodiscovery,SPLI Auto-Tuning,Notes,\n')
    fErr = ''
    for line in linesIn:
        if line.find('ADJRXDTRANSNEG=') > -1:
            NOTES = ''
            s1 = line.split('::')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            f1 = AID.replace('ADJ-', '')
            l1 = f1.find('-')
            f2 = f1[0:l1]
            SHELF = 'SHELF-' + f2
            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            fOTS = ',,,,'
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    fOTS = j[0]
                    continue

            f1 = s1[1]
            f1 = f1[:-2] + ','
            CKTID = FISH(f1, 'CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            WAVE = FISH(f1, 'WAVELENGTH=', ',')
            if line.find('MATE') > -1 or line.find('OCHLINETYPE') > -1:
                DWAVE = FISH(f1, 'DISCWAVELENGTH=', ',')
            else:
                DWAVE = FISH(f1, 'DISCWAVELENGTH=', '"')
            RXMIN = FISH(f1, 'ADJRXSENSTHRESH=', ',')
            RXMAX = FISH(f1, 'ADJRXOVERTHRESH=', ',')
            RXNOM = FISH(f1, 'ADJRXNOMINPUT=', ',')
            TRPLUS = FISH(f1, 'ADJRXDTRANSPOS=', ',')
            TRNEG = FISH(f1, 'ADJRXDTRANSNEG=', ',')
            try:
                REST = dRxADJACENCY[AID]
            except KeyError:
                REST = ',,,,,'

            lREST = REST.split(',')
            RXTYPE = FISH(f1, 'ADJRXTYPE=', ',')
            RXTYPE = RXTYPE.replace('\\"', '')
            RX_Translated = GET_CP_DESCRIPTION(RXTYPE)
            DRXTYPE = FISH(f1, 'DISCTYPE=', ',')
            DRXTYPE = DRXTYPE.replace('\\"', '')
            DRX_Translated = GET_CP_DESCRIPTION(DRXTYPE)
            RATE = FISH(f1, 'RATE=', ',')
            CHSTATUS = FISH(f1, 'CHSTATUS=', ',')
            DOCCARE = FISH(f1, 'DOCCARE=', ',')
            PAIREDTX = FISH(f1, 'PAIREDTX=', ',')
            AUTODISC = FISH(f1, 'AUTODISC=', ',')
            SYNCPROV = FISH(f1, 'SYNCPROV=', ',')
            PROVFEPEC = FISH(f1, 'PROVFEPEC=\\"', '\\"')
            DISCFEPEC = FISH(f1, 'DISCFEPEC=\\"', '\\"')
            FREQUENCY = FISH(f1, 'FREQUENCY=', ',')
            DISCFREQUENCY = FISH(f1, 'DISCFREQUENCY=', ',')
            OCHLINETYPE = FISH(f1, 'OCHLINETYPE=', ',')
            PORTLABEL = FISH(f1, 'PORTLABEL=\\"', '\\"')
            PRI = lREST[0]
            DISCFEADDR = lREST[1]
            DADDRFORM = lREST[2]
            PROVFEADDR = lREST[3]
            PADDRFORM = lREST[4]
            if PRI == 'IS':
                if PADDRFORM != 'TID-SH-SL-PRT' and PADDRFORM != 'TID-BAY-SH-SL-PRT' and PADDRFORM != 'NODENAME-SL-PRT':
                    NOTES = NOTES + ' Rx FE format +'
                    s1 = 'IS Rx adjacency with provisioned far end format (' + PADDRFORM + ') not equal to TID-SH-SL-PRT or TID-BAY-SH-SL-PRT or NODENAME-SL-PRT'
                    fErr += ',' + AID + ',' + s1 + '\n'
                else:
                    if DADDRFORM != PADDRFORM:
                        NOTES = NOTES + ' Rx FE format +'
                        s1 = 'IS Rx adjacency having mismatched discovered (' + DADDRFORM + ') and provisioned (' + PADDRFORM + ') far end AID format'
                        fErr += ',' + AID + ',' + s1 + '\n'
                    if DISCFEADDR != PROVFEADDR:
                        NOTES = NOTES + ' Rx FE AID +'
                        s1 = 'IS Rx adjacency having mismatched discovered (' + DISCFEADDR + ') and provisioned (' + PROVFEADDR + ') far end AID'
                        fErr += ',' + AID + ',' + s1 + '\n'
            elif DOCCARE == 'TRUE':
                if CHSTATUS == 'NOT APPLICABLE' and RXTYPE == 'UNKNOWN' and DISCFEADDR == '' and DADDRFORM == 'NULL':
                    pass
                else:
                    NOTES = ' Verify this pass-through channel  '
            if CHSTATUS == 'MANAGED':
                if RXTYPE != DRXTYPE:
                    fErr += ',' + AID + ',Managed channel: provisioned (' + RXTYPE + ') and discovered (' + DRXTYPE + ') Tx type mismatch \n'
                    NOTES = NOTES + ' Rx Type discrepancy +'
                if WAVE != DWAVE:
                    fErr += ',' + AID + ',Managed channel: provisioned (' + WAVE + ') and discovered (' + DWAVE + ') wavelength mismatch \n'
                    NOTES = NOTES + ' Wavelength mismatch +'
                if DOCCARE == 'FALSE':
                    fErr += ',' + AID + ',Managed channel but not under DOC control \n'
                    NOTES = NOTES + ' Not under DOC care +'
                if AUTODISC != 'AUTO':
                    fErr += ',' + AID + ',Managed channel: Autodiscovery is disabled \n'
                    NOTES = NOTES + ' Autodiscovery=OFF +'
                if PAIREDTX != 'YES':
                    NOTES = NOTES + ' No Tx pair +'
                    fErr += ',' + AID + ',Managed channel note: Tx/Rx are not paired \n'
                f1 = AID.split('-')
                i = int(f1[3]) - 1
                aidTx = f1[0] + '-' + f1[1] + '-' + f1[2] + '-' + str(i)
                try:
                    f1 = d_ADJTXTYPE_PR_DSC__AID[aidTx]
                    i = f1.split('+')
                    pTx = i[0]
                    dTx = i[1]
                except:
                    fErr += ',' + AID + ',The corresponding managed channel Tx AID (' + aidTx + ') was not found\n'
                    pTx = ''
                    dTx = ''

                if RXTYPE != pTx:
                    NOTES = NOTES + ' Provisioned Tx CP Type +'
                    fErr += ',' + AID + ',Managed channel: provisioned Tx (' + pTx + ') and Rx (' + RXTYPE + ') CP type mismatch \n'
                if DRXTYPE != dTx:
                    NOTES = NOTES + ' Discovered Tx CP Type +'
                    fErr += ',' + AID + ',Managed channel: discovered Tx (' + dTx + ') and Rx (' + DRXTYPE + ') CP type mismatch \n'
                    NOTES = NOTES + ' Rx vs Tx Discovered Type discrepancy +'
                f1 = ''
                try:
                    f1 = dTxADJACENCY[aidTx]
                except:
                    fErr += ',' + AID + ',The corresponding managed channel Tx AID (' + aidTx + ') was not found\n'

                if f1 != '':
                    f2 = f1.split(',')
                    f1 = f2[4]
                    if f1 != PADDRFORM:
                        NOTES = NOTES + ' Provisioned Rx FE AID format +'
                        s1 = 'Managed channel Tx AID (' + aidTx + ') & Rx AID (' + AID + ') have different provisioned Far-End AID format ( Tx = ' + f1 + ') vs (Rx = ' + PADDRFORM + ')'
                        fErr += ',' + AID + ',' + s1 + '\n'
                    f1 = f2[3]
                    if f1 != PROVFEADDR:
                        NOTES = NOTES + ' Provisioned Tx FE AID +'
                        s1 = 'Managed channel Tx AID (' + aidTx + ') & Rx AID (' + AID + ') have different provisioned Far-End AID address( Tx = ' + f1 + ') vs (Rx = ' + PROVFEADDR + ')'
                        fErr += ',' + AID + ',' + s1 + '\n'
            if ISSUES == 'YES':
                NOTES = NOTES[:-1]
            else:
                NOTES = ''
            f2 = TID + ',' + SHELF + ',' + fOTS + ',' + AID + ',' + WAVE + ',' + DWAVE + ',' + REST + ',' + PROVFEPEC + ',' + DISCFEPEC + ',' + FREQUENCY + ',' + DISCFREQUENCY + ',' + RXTYPE + ',' + DRXTYPE + ',' + RX_Translated + ',' + DRX_Translated + ',' + RATE + ',' + RXMIN + ',' + RXMAX + ',' + RXNOM + ',' + TRPLUS + ',' + TRPLUS + ',' + OCHLINETYPE + ',' + PORTLABEL + ',' + CHSTATUS + ',' + DOCCARE + ',' + PAIREDTX + ',' + AUTODISC + ',' + SYNCPROV + ',' + NOTES + ','
            F_Out.write(f2 + '\n')

    if fErr != '':
        F_ERROR.write('\nShelf Active Channel Issues (see tab Rx_Adjacency)\n' + fErr)
    return None


def PARSE_RTRV_CHC(linesIn, dMEMBERS, TID, F_Out, F_ERROR):
    F_Out.write('TID,Shelf ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Wavelength,Minimum Frequency (THz),Maximum Frequency (THz),Channel Width (GHz),Minimum Frequency Limit,Maximum Frequency Limit,Circuit ID,Opacity,Port,Selected Port,CHC Mode,Controller Output Power,Controller Target Power,Base Target Power,Power Differene,Target Loss,Calculated Loss,Loss Difference,Derived Input Power,Reference Input Power,Input Source,Derived Output Power,Drive,Pstate,Sstate,Notes,\n')
    fErr = ''
    wrongState = False
    for line in linesIn:
        NOTES = ''
        if line.find(':ISOPQ=') > -1:
            s1 = line.split(':')
            AID = s1[0].replace('   "', '')
            l1 = AID.rfind('-') + 1
            l2 = l1 + 6
            f1 = AID[l1:l2]
            if len(f1) > 3:
                WAVE = f1
            else:
                WAVE = FISH(line, 'WAVELENGTH=\\"', '\\"')
            f1 = AID.split('-')
            SHELF = 'SHELF-' + f1[1]
            ShSl = '-' + f1[1] + '-' + f1[2]
            fOTS = ',,,,'
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    fOTS = j[0]
                    continue

            f1 = s1[3].strip(' "\n\r')
            f2 = f1.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1]
            except:
                SEC = ''

            line = line.replace(':', ',')
            f1 = s1[2]
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            CKTID = CKTID.replace(',', ';')
            ISOPQ = FISH(line, 'ISOPQ=', ',')
            SWSEL = FISH(line, 'SWSEL=', ',')
            MINFREQ = FISH(line, 'MINFREQ=\\"', '\\"').rstrip('0')
            MAXFREQ = FISH(line, 'MAXFREQ=\\"', '\\"').rstrip('0')
            BW = '-'
            MINFREQLIMIT = FISH(line, 'MINFREQLIMIT=\\"', '\\"').rstrip('0')
            MAXFREQLIMIT = FISH(line, 'MAXFREQLIMIT=\\"', '\\"').rstrip('0')
            TARGSWSEL = FISH(line, 'TARGSWSEL=', ',')
            CHCMODE = FISH(line, 'CHCMODE=', ',')
            CTRLOUTPOW = FISH(line, 'CTRLOUTPOW=', ',')
            CTRLTARGPOW = FISH(line, 'CTRLTARGPOW=', ',')
            BASETRGTPW = FISH(line, 'BASETRGTPW=', ',')
            try:
                DIFF1 = abs(float(CTRLTARGPOW) - float(BASETRGTPW))
                if DIFF1 > 0.5:
                    NOTES = NOTES + 'Controller Output Power delta was greater than 0.5 dB &'
                    fErr += ',' + AID + ',Controller Power delta (=' + str(DIFF1) + ') was greater than 0.5 dB \n'
            except:
                DIFF1 = ''

            LOSS = FISH(line, ',LOSS=', ',')
            TARGLOSS = FISH(line, 'TARGLOSS=', ',')
            try:
                DIFF2 = abs(float(LOSS) - float(TARGLOSS))
                if DIFF2 > 0.5:
                    NOTES = NOTES + ' Insertion loss delta greater than 0.5 dB '
                    fErr += ',' + AID + ',Pixel insertion loss delta (=' + str(DIFF2) + ') was greater than 0.5 dB \n'
            except:
                DIFF2 = ''

            INPOW = FISH(line, 'INPOW=', ',')
            INPWEREF = FISH(line, 'INPWEREF=', ',')
            INPOWSRC = FISH(line, 'INPOWSRC=\\"', '\\"')
            OUTPOW = FISH(line, 'OUTPOW=', ',')
            INITDRIVE = FISH(line, 'INITDRIVE=', ',')
            if PRI.find('OOS') > -1 or SEC.find('SGEO') > -1:
                wrongState = True
            if ISSUES == 'YES':
                NOTES = NOTES[:-1]
            else:
                NOTES = ''
            f2 = TID + ',' + SHELF + ',' + fOTS + ',' + AID + ',' + WAVE + ',' + MINFREQ + ',' + MAXFREQ + ',' + BW + ',' + MINFREQLIMIT + ',' + MAXFREQLIMIT + ',' + CKTID + ',' + ISOPQ + ',' + SWSEL + ',' + TARGSWSEL + ',' + CHCMODE + ',' + CTRLOUTPOW + ',' + CTRLTARGPOW + ',' + BASETRGTPW + ',' + str(DIFF1) + ',' + TARGLOSS + ',' + LOSS + ',' + str(DIFF2) + ',' + INPOW + ',' + INPWEREF + ',' + INPOWSRC + ',' + OUTPOW + ',' + INITDRIVE + ',' + PRI + ',' + SEC + ',' + NOTES + ',\n'
            F_Out.write(f2)

    if wrongState == True:
        fErr += ',,There are Pixels with PRI-State OOS and/or SEC-State SGEO \n'
    if fErr != '':
        F_ERROR.write('\nShelf WSS Pixel Issues (see tab WSS)\n' + fErr)
    return None


def PARSE_RTRV_SSC(linesIn, dMEMBERS, TID, F_Out, F_ERROR):
    F_Out.write('TID,Shelf ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Unit,Bias (dB),Minimum Frequency (THz),Maximum Frequency (THz),Parent NMCC,Index Within NMCC,Attenuation (dB),Base Target Power (dBm),Reference Bandwidth\n')
    for line in linesIn:
        if len(line) < 100:
            continue
        s1 = line.find(':')
        f1 = line[0:s1]
        AID = f1.replace('   "', '')
        f1 = AID.split('-')
        SHELF = 'SHELF-' + f1[1]
        ShSl = '-' + f1[1] + '-' + f1[2]
        fOTS = ',,,,'
        for j in dMEMBERS.items():
            if j[1].find(ShSl) > -1:
                fOTS = j[0]
                continue

        line = line[:-2] + ','
        BIAS = FISH(line, 'BIAS=', ',')
        MINFREQ = FISH(line, 'MINFREQ=\\"', '\\"').rstrip('0')
        MAXFREQ = FISH(line, 'MAXFREQ=\\"', '\\"').rstrip('0')
        PARENTNMCC = FISH(line, 'PARENTNMCC=\\"', '\\"')
        CHCRELATIVEINDEX = FISH(line, 'NMCCRELATIVEINDEX=\\"', '\\"')
        ATTEN = FISH(line, 'ATTEN=', ',')
        BASETARGPOW = FISH(line, 'BASETARGPOW=', ',')
        REFBW = FISH(line, 'REFBW=', ',')
        f2 = TID + ',' + SHELF + ',' + fOTS + ',' + AID + ',' + BIAS + ',' + MINFREQ + ',' + MAXFREQ + ',' + PARENTNMCC + ',' + CHCRELATIVEINDEX + ',' + ATTEN + ',' + BASETARGPOW + ',' + REFBW + ',\n'
        F_Out.write(f2)

    return None


def PARSE_RTRV_NMCC(linesIn, dMEMBERS, TID, F_Out, F_ERROR):
    F_Out.write('TID,Shelf ID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Unit,PState,SState,Center Frequency (THz),Spectral Width (THz),Minimum Frequency (THz),Maximum Frequency (THz),Target Loss (dBm),Reference Bandwidth,Controller Target Power (dBm),Base Target Power (dBm),Initial Attenuation (dB),Controller State,Control SSC,Control SSC Attenuation (dB),WSS Output Power (dBm),Controller Output Power (dBm),Opacity,CHC Mode,Switch Selector,Target Switch Selector,Channel Power (dBm),Input Power Source,Derived Input Power,Reference Input Power,CKTID,Channel Input Power,Channel Output Power,Wavelength,\n')
    for line in linesIn:
        if line.find(',ISOPQ=') > -1:
            s1 = line.split(':')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            STATE = s1[3]
            STATE = STATE.replace('"\r', '')
            f1 = AID.split('-')
            SHELF = 'SHELF-' + f1[1]
            ShSl = '-' + f1[1] + '-' + f1[2]
            fOTS = ',,,,'
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    fOTS = j[0]
                    continue

            l1 = line.rfind(':')
            line = line[:l1] + ','
            BIAS = FISH(line, 'BIAS=', ',')
            CENTERFREQ = FISH(line, 'CENTERFREQ=\\"', '\\"').rstrip('0')
            SPECTRALWIDTH = FISH(line, 'SPECTRALWIDTH=\\"', '\\"').rstrip('0')
            MINFREQ = FISH(line, 'MINFREQ=\\"', '\\"').rstrip('0')
            MAXFREQ = FISH(line, 'MAXFREQ=\\"', '\\"').rstrip('0')
            TARGLOSS = FISH(line, 'TARGLOSS=', ',')
            CTRLTARGPOW = FISH(line, 'CTRLTARGPOW=', ',')
            INITATTEN = FISH(line, 'INITATTEN=', ',')
            BASETARGPOW = FISH(line, 'BASETARGPOW=', ',')
            REFBW = FISH(line, 'REFBW=', ',')
            CTRLSS = FISH(line, 'CTRLSS=\\"', '\\"')
            CTRLSS = CTRLSS.replace(',', ' &')
            CTRLSSCATTEN = FISH(line, 'CTRLSSCATTEN=', ',')
            CTRLSTATE = FISH(line, 'CTRLSTATE=\\"', '\\"')
            CTRLOUTPOW = FISH(line, 'CTRLOUTPOW=', ',')
            WSSOUTPOW = FISH(line, 'WSSOUTPOW=', ',')
            CHNLPOW = FISH(line, 'CHNLPOW=', ',')
            ISOPQ = FISH(line, 'ISOPQ=', ',')
            CHCMODE = FISH(line, 'CHCMODE=', ',')
            SWSEL = FISH(line, 'SWSEL=', ',')
            TARGSWSEL = FISH(line, 'TARGSWSEL=', ',')
            INPOW = FISH(line, 'INPOW=', ',')
            INPWEREF = FISH(line, 'INPWEREF=', ',')
            INPOWSRC = FISH(line, 'INPOWSRC=\\"', '\\"')
            CKTID = FISH(line, 'CKTID=\\"', '\\"')
            WSSCHOUTPOW = FISH(line, 'WSSCHOUTPOW=', ',')
            WSSCHINPOW = FISH(line, 'WSSCHINPOW=', ',')
            WAVELENGTH = FISH(line, 'WAVELENGTH=\\"', '\\"')
            f2 = TID + ',' + SHELF + ',' + fOTS + ',' + AID + ',' + STATE + ',' + CENTERFREQ + ',' + SPECTRALWIDTH + ',' + MINFREQ + ',' + MAXFREQ + ',' + TARGLOSS + ',' + REFBW + ',' + CTRLTARGPOW + ',' + BASETARGPOW + ',' + INITATTEN + ',' + CTRLSTATE + ',' + CTRLSS + ',' + CTRLSSCATTEN + ',' + WSSOUTPOW + ',' + CTRLOUTPOW + ',' + ISOPQ + ',' + CHCMODE + ',' + SWSEL + ',' + TARGSWSEL + ',' + CHNLPOW + ',' + INPOW + ',' + INPWEREF + ',' + INPOWSRC + ',' + CKTID + ',' + WSSCHINPOW + ',' + WSSCHOUTPOW + ',' + WAVELENGTH + ',' + '\n'
            F_Out.write(f2)

    return None


def PARSE_STATIC_ROUTES(linesIn, TID, FileName):
    sOut = ''
    for line in linesIn:
        if line.find('>>>Begin: RTRV-STATICROUTE') > -1:
            sOut += '\n'
        if len(line) < 50:
            continue
        if line.find('CARRIER') > -1:
            l1 = line.find('::')
            AID = line[4:l1]
            IPADDR = FISH(line, 'IPADDR=', ',')
            NETMASK = FISH(line, 'NETMASK=', ',')
            PREFIX = ''
            NEXTHOP = FISH(line, 'NEXTHOP=', ',')
            COST = FISH(line, ',COST=', ',')
            CIRCUIT = FISH(line, 'CIRCUIT=', ',')
            CARRIER = FISH(line, 'CARRIER=', ',')
            STATUS = FISH(line, ',STATUS=', ',')
            DESCRIPTION = FISH(line, 'DESCRIPTION=\\"', '\\"')
            RDTYPE = ''
            REDISTRIBUT = ''
            sOut += TID + ',' + AID + ',' + IPADDR + ',' + NETMASK + ',' + PREFIX + ',' + NEXTHOP + ',' + COST + ',' + CIRCUIT + ',' + CARRIER + ',' + STATUS + ',' + DESCRIPTION + ',' + RDTYPE + ',' + REDISTRIBUT + '\n'
        elif line.find(' "STATICRT-') > -1:
            l1 = line.find('::')
            AID = line[4:l1]
            IPADDR = FISH(line, 'IPADDR=\\"', '\\"')
            NETMASK = ''
            PREFIX = FISH(line, 'PREFIX=', ',')
            NEXTHOP = FISH(line, 'NEXTHOP=\\"', '\\"')
            COST = FISH(line, 'COST=', ',')
            CIRCUIT = FISH(line, 'CIRCUIT=', ',')
            CARRIER = ''
            STATUS = FISH(line, 'GWSTATUS=', ',')
            DESCRIPTION = FISH(line, 'DESCRIPTION=', '"')
            RDTYPE = FISH(line, 'RDTYPE=', ',')
            REDISTRIBUT = FISH(line, 'REDISTRIBUT=', ',')
            sOut += TID + ',' + AID + ',' + IPADDR + ',' + NETMASK + ',' + PREFIX + ',' + NEXTHOP + ',' + COST + ',' + CIRCUIT + ',' + CARRIER + ',' + STATUS + ',' + DESCRIPTION + ',' + RDTYPE + ',' + REDISTRIBUT + '\n'

    if sOut.find('-') > -1:
        f1 = open(FileName, 'w')
        f1.write('TID,Instance,IP Subnet,Subnet Mask,Prefix,Next Hop,Cost,Circuit ID,Carrier,Status,Description,RD Type,Redistribute,\n' + sOut)
        f1.close()
    return None


def PARSE_RTRV_IPFILTER(linesIn, F_NOW):
    F_NOW.write('Unit,Action,Filtering Location,Protocol,Destination Start,Destination End,\n')
    for line in linesIn:
        if line.find('ACTION') < 0:
            continue
        line = line[:-2] + ','
        l1 = line.find('::')
        AID = line[4:l1]
        ACTION = FISH(line, 'ACTION=', ',')
        LOCATION = FISH(line, 'LOCATION=', ',')
        PROTO = FISH(line, 'PROTO=', ',')
        DESTSTARTPORT = FISH(line, 'DESTSTARTPORT=', ',')
        DESTENDPORT = FISH(line, 'DESTENDPORT=', ',')
        F_NOW.write(AID + ',' + ACTION + ',' + LOCATION + ',' + PROTO + ',' + DESTSTARTPORT + ',' + DESTENDPORT + '\n')

    return None


def PARSE_RTRV_SECU_USER(linesIn, F_NOW):
    F_NOW.write('User ID,User Type,Password Status,Priviledge Code,In Use,Automatic Timeout,Timeout Interval,Use Defaults,Last Login, Expiration Data,\n')
    BadPass = ''
    for line in linesIn:
        if line.find('PWDSTATUS') > -1:
            line = line[:-2] + ','
            toks = line.split(':')
            User = toks[0].strip(' \\"')
            UserType = FISH(line, 'USERTYPE=', ',')
            PassStatus = FISH(line, 'PWDSTATUS=', ',')
            PrivCode = toks[1].strip(',')
            InUse = FISH(line, 'ACTIVE=', ',')
            AutoTout = FISH(line, 'TMOUTA=', ',')
            Tout = FISH(line, 'TMOUT=', ',')
            UseDefaults = FISH(line, 'USEDFLT=', ',')
            Last = FISH(line, 'LASTLGTIME=\\"', '\\"')
            Expir = FISH(line, 'PWDEXP=\\"', '\\"')
            F_NOW.write(User + ',' + UserType + ',' + PassStatus + ',' + PrivCode + ',' + InUse + ',' + AutoTout + ',' + Tout + ',' + UseDefaults + ',' + Last + ',' + Expir + ',\n')
        elif line.find(' "\\"') > -1 and line.find('\\""') > -1:
            User = FISH(line, ' "\\"', '\\""')
            BadPass += User + '\n'

    if BadPass == '':
        BadPass = 'N/A'
    F_NOW.write('\n\nBad Passwords\n' + BadPass)
    return None


def PARSE_RTRV_SECU_RULES(linesIn, F_NOW):
    iServer = 0
    for line in linesIn:
        if line.find('USRLCKOUTMDE') > -1:
            line = line[:-2] + ','
            l1 = line.find('::')
            fOut = line[4:l1]
            fOut += ',' + FISH(line, 'ACCR=', ',')
            fOut += ',' + FISH(line, 'ACCRSTAT=', ',')
            fOut += ',' + FISH(line, ',DSTATE=', ',')
            fOut += ',' + FISH(line, 'DURAL=', ',')
            fOut += ',' + FISH(line, ',IDSTATE=', ',')
            fOut += ',' + FISH(line, 'MINW=', ',')
            fOut += ',' + FISH(line, 'MXINV=', ',')
            fOut += ',' + FISH(line, 'MXLOGIN=', ',')
            fOut += ',' + FISH(line, 'PAGE=', ',')
            fOut += ',' + FISH(line, 'PAGESTAT=', ',')
            fOut += ',' + FISH(line, 'PCND=', ',')
            if line.find('PWDRLS=STD') > -1:
                fOut += ',Standard'
            elif line.find('PWDRLS=CMPLX') > -1:
                fOut += ',Complex'
            elif line.find('PWDRLS=CUSTOM') > -1:
                fOut += ',Custom'
            else:
                fOut += ',' + FISH(line, 'PWDRLS=', ',')
            fOut += ',' + FISH(line, 'UOUT=', ',')
            fOut += ',' + FISH(line, 'USRLCKOUTMDE=', ',')
            F_NOW.write('Security Defaults:,\nAID,Password accreditation time (Days),Password accreditation Status,Account Dormancy state,Duration of lockout (sec),Intrusion detection state,Minimum waiting time (Days),Maximum # of invalid login attempts,Simultaneous login limit,Password aging time (Days),Password aging status,Early warning time (Days),Password Rules State,User aging interval,User lock out mode,\n' + fOut + ',\n\n\n')
        elif line.find('REPEAT_CHAR_MAX') > -1:
            line = line[:-2] + ','
            l1 = line.find('::')
            fOut = line[4:l1]
            fOut += ',' + FISH(line, 'ALPHA_MIN=', ',')
            fOut += ',' + FISH(line, 'LOWERC_MIN=', ',')
            fOut += ',' + FISH(line, 'NUM_MIN=', ',')
            fOut += ',' + FISH(line, 'PDIF=', ',')
            fOut += ',' + FISH(line, 'PLEN_MIN=', ',')
            fOut += ',' + FISH(line, 'POLD=', ',')
            fOut += ',' + FISH(line, 'REPEAT_CHAR_MAX=', ',')
            fOut += ',' + FISH(line, 'SPEC_MIN=', ',')
            fOut += ',' + FISH(line, 'UPPERC_MIN=', ',')
            F_NOW.write('Password Rules:\nAID,Min # of alphabetic characters,Min # of lowercase characters,MIN # of numeric characters,Min # of characters differ between the old and new password,Min # of characters,Min # of prior password that cannot be used in new password,Max # of repeating characters,Min # of special characters,Min # of uppercase characters,\n' + fOut + '\n\n\n')
        elif line.find('SYSLOGTYPES') > -1:
            line = line[:-2] + ','
            l1 = line.find('::')
            fOut = line[4:l1]
            fOut += ',' + FISH(line, 'HOSTIPFMT=', ',')
            fOut += ',RFC-' + FISH(line, 'PRTCL=', ',')
            fOut += ',' + FISH(line, 'SYSLOGFAC=', ',')
            fOut += ',' + FISH(line, 'SYSLOGSEV=', ',')
            fOut += ',' + FISH(line, 'SYSLOGTYPES=', ',')
            F_NOW.write('SysLog Settings:\nAID,IP Format,Protocol,Facility,Severity,Type,\n' + fOut + '\n\n\n')
            F_NOW.write('SysLog Server:\nAID,IP,Port,State,Server #\n')
            iServer = 1
        elif line.find(':STATE=') > -1 and line.find(',IP=') > -1:
            line = line[:-2] + ','
            l1 = line.find('::')
            fOut = line[4:l1]
            fOut += ',' + FISH(line, 'STATE=', ',')
            fOut += ',' + FISH(line, 'IP=\\"', '\\"')
            fOut += ',' + FISH(line, 'PORT=', ',')
            F_NOW.write(fOut + ',' + str(iServer) + ',\n')
            iServer += 1

    return None


def PARSE_RADIUS(linesIn, F_NOW):
    Server = ''
    for line in linesIn:
        if line.find('  "SHELF-') < 0:
            continue
        if line.find('::DFLT=') > -1:
            l1 = line.find('=') + 1
            F_NOW.write('Default Authentication =,' + line[l1:-2] + '\n')
            continue
        if line.find(',') < 0:
            if line.find(':LOCAL"') > -1:
                F_NOW.write('Alternate Authentication =,LOCAL' + '\n')
            elif line.find(':CHALLENGE"') > -1:
                F_NOW.write('Alternate Authentication =,CHALLENGE' + '\n')
            continue
        if line.find(',QUERYMODE=') > -1:
            QueryMode = FISH(line, 'QUERYMODE=', ',')
            RADIUSStatus = FISH(line, 'AUTHSTATE=', ',')
            F_NOW.write('RADIUS Status =,' + RADIUSStatus + '\nQuery Mode =,' + QueryMode + '\n')
            continue
        if line.find(':STATE=') > -1:
            if line.find(',DUP=') > -1:
                f1 = FISH(line, ':STATE=', ',')
                F_NOW.write('Accounting Status =,' + f1 + '\n\n')
            elif line.find(',RADIUS=') > -1:
                if Server == '':
                    F_NOW.write('Server,Type,IP Address,Port,Timeout,Status,Auto Generate Secret,\n')
                    Server = 'Accounting,Primary,'
                elif Server == 'Accounting,Primary,':
                    Server = 'Accounting,Secondary,'
                elif Server == 'Accounting,Secondary,':
                    Server = 'Authentication,Primary,'
                elif Server == 'Authentication,Primary,':
                    Server = 'Authentication,Secondary,'
                IP = FISH(line, 'RADIUS=\\"', '\\"')
                Port = FISH(line, ',PORT=', ',')
                Tout = FISH(line, ',TO=', ',')
                Status = FISH(line, 'STATE=', ',')
                if line.find('GENSECRET=Y') > -1:
                    AutoGen = 'Yes'
                elif line.find('GENSECRET=N') > -1:
                    AutoGen = 'No'
                else:
                    AutoGen = '-'
                F_NOW.write(Server + IP + ',' + Port + ',' + Tout + ',' + Status + ',' + AutoGen + ',\n')
            elif line.find(',BADSIZE=') > -1:
                if line.find('AUTHENTICATION') > -1:
                    Server = 'Authentication RADIUS Proxy'
                    F_NOW.write('\n\n\nServer,Status,Auto Generate Secret,\n')
                else:
                    Server = 'Accounting RADIUS Proxy'
                Status = FISH(line, 'STATE=', ',')
                if line.find('GENSECRET=Y') > -1:
                    AutoGen = 'Yes'
                elif line.find('GENSECRET=N') > -1:
                    AutoGen = 'No'
                else:
                    AutoGen = '-'
                F_NOW.write(Server + ',' + Status + ',' + AutoGen + ',' + '\n')

    return None


def PARSE_ALL_DCN(linesIn, TID, F_NOW):
    lPoint = []
    lPoint.append('TID = ' + TID)
    i_Gne = 1
    lPoint.append('INTERFACES >>> GNE')
    lPoint.append('GNE Configuration')
    lPoint.append('GNE Access')
    lPoint.append('Subnet Name')
    lPoint.append('')
    lPoint.append('ROUTERS >>> OSPF ROUTERS')
    i_OspfRouter = i_Gne + 6
    lPoint.append('OSPF Router ID')
    lPoint.append('Type of Link State Announcement')
    lPoint.append('Route Summarization')
    lPoint.append('Autonomous System Border Router')
    lPoint.append('Opaque Filter')
    lPoint.append('Shelf IP Redistribution')
    lPoint.append('ABR')
    lPoint.append('RFC1583')
    lPoint.append('')
    lPoint.append('ROUTERS >>> OSPF CIRCUITS')
    i_OspfCircuit = i_OspfRouter + 10
    lPoint.append('Network Area')
    lPoint.append('Cost')
    lPoint.append('Dead Interval')
    lPoint.append('Retransmit Interval')
    lPoint.append('Transmit Delay')
    lPoint.append('Priority')
    lPoint.append('Area Default Cost')
    lPoint.append('Area Virtual Link')
    lPoint.append('Area')
    lPoint.append('Carrier')
    lPoint.append('Authentication Type')
    lPoint.append('Password')
    lPoint.append('Password Status')
    lPoint.append('ID1')
    lPoint.append('Key1')
    lPoint.append('First MD5 Authentication Status')
    lPoint.append('ID2')
    lPoint.append('Key2')
    lPoint.append('Second MD5 Authentication Status')
    lPoint.append('OSPF Opaque Link State Advertisement')
    lPoint.append('Passive OSPF Circuit')
    lPoint.append('')
    lPoint.append('SERVICES >>> DATABASE REPLICATION')
    i_Dbrs = i_OspfCircuit + 23
    lPoint.append('Address Resolution')
    lPoint.append('Topology Resolution')
    lPoint.append('')
    lPoint.append('INTERFACES >>> IP (IPV4)')
    i_Ip = i_Dbrs + 4
    lPoint.append('IP Address')
    lPoint.append('Subnet Mask')
    lPoint.append('Non-routing mode')
    lPoint.append('Host Only Mode')
    lPoint.append('Proxy ARP Mode')
    lPoint.append('')
    lPoint.append('INTERFACES >>> IP (IPV6)')
    i_Ipv6 = i_Ip + 7
    lPoint.append('AID')
    lPoint.append('IP Address')
    lPoint.append('Prefix')
    lPoint.append('')
    lPoint.append('INTERFACES >>> LAN')
    i_Lan = i_Ipv6 + 5
    lPoint.append('Port Configuration')
    lPoint.append('Port Negotiation')
    lPoint.append('Port Status')
    lPoint.append('')
    lPoint.append('INTERFACES >>> NDP')
    i_Ndp = i_Lan + 5
    lPoint.append('Admin State')
    lPoint.append('')
    lPoint.append('INTERFACES >>> Lower Layer DCC/GCC')
    i_Gcc = i_Ndp + 3
    lPoint.append('Network Domain')
    lPoint.append('Carrier')
    lPoint.append('Operation Carrier')
    lPoint.append('Protocol')
    lPoint.append('FCS_Mode')
    lPoint.append('')
    lPoint.append('SERVICES >>> TELNET')
    i_Tel = i_Gcc + 7
    lPoint.append('Server')
    lPoint.append('Maximum Sessions')
    lPoint.append('Idle Timeout')
    lPoint.append('')
    lPoint.append('SERVICES >>> SSH')
    i_Ssh = i_Tel + 5
    lPoint.append('Server')
    lPoint.append('Maximum Sessions')
    lPoint.append('Idle Timeout')
    lPoint.append('CIPHER')
    lPoint.append('HMAC')
    i_Ssh_PubKey = i_Ssh + 5
    lPoint.append('Key Type')
    lPoint.append('Key Size')
    lPoint.append('')
    lPoint.append('SERVICES >>> HTTP')
    i_Http = i_Ssh_PubKey + 4
    lPoint.append('HTTP')
    lPoint.append('HTTPS')
    lPoint.append('REST')
    lPoint.append('')
    lPoint.append('SERVICES >>> FTP')
    i_Ftp = i_Http + 5
    lPoint.append('Server')
    lPoint.append('Maximum Sessions')
    lPoint.append('Idle Timeout')
    lAID = []
    lSHELF = []
    DCN_ALL = {}
    getTelnet = 0
    getSsh = 1
    for line in linesIn:
        if line.find(':CONFIG=') > -1 and line.find('IPTIDMESSAGING') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Gne
            location = AID + '@' + str(idx)
            DCN_ALL[location] = 'YES'
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':CONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'ACCESS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'SUBNETNAME=\\"', '\\"')
            continue
        if line.find(':ROUTERID=') > -1 and line.find('ROUTESUMMARISATION') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            line = line[:-2] + ','
            idx = i_OspfRouter
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':ROUTERID=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',LSATYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'ROUTESUMMARISATION=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',ASBR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',OPAQUEFILTER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',SHELFRD=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',ABR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',RFC1583=', ',')
            continue
        if ',HELLOINVL=' in line and ',OPAQUE=' in line and ',COST=' in line:
            line = line.replace('"\r', ',')
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_OspfCircuit
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':NETAREA=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',COST=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',DEADINVL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',RETRANSINVL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',TRANSDELAY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PRIORITY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',AREADEFCOST=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',AREAVLINK=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',AREA=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',AUTHTYPE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PASSWORD=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',STATUS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',ID1=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, ',KEY1=\\"', '\\"')
            try:
                f3 = u''.join(f1).encode('utf-8')
                DCN_ALL[location] = f1
            except:
                DCN_ALL[location] = 'String had unknown Encoding'

            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',STATUS1=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',ID2=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, ',KEY2=\\"', '\\"')
            try:
                f3 = u''.join(f1).encode('utf-8')
                DCN_ALL[location] = f1
            except:
                DCN_ALL[location] = 'String had unknown Encoding'

            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',STATUS2=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',OPAQUE=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PASSIVE=', ',')
            continue
        if line.find(':DBRSAR=') > -1 and line.find('DBRSTR=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            lAID.append(AID)
            idx = i_Dbrs
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':DBRSAR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',DBRSTR=', '"')
            continue
        if line.find('NONROUTING') > -1 and line.find('BCASTADDR=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Ip
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':IPADDR=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',NETMASK=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',NONROUTING=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',HOSTONLY=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PROXYARP=', ',')
            continue
        if line.find('  "IPADDR-') > -1 and line.find(',PREFIX=') > -1:
            AID = FISH(line, ',CIRCUIT=', '"')
            l1 = line.find('::')
            f2 = line[0:l1]
            idx = i_Ipv6
            location = AID + '@' + str(idx)
            DCN_ALL[location] = f2.replace('   "', '')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'IPADDR=\\"', '\\"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PREFIX=', ',')
            continue
        if line.find(':CONFIG=') > -1 and line.find(',OPER_CONFIG=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Lan
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':CONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',OPER_CONFIG=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',PORT=', ',')
            continue
        if line.find('ADMINSTATE=') > -1 and line.find(',NDPVERSION') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            idx = i_Ndp
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'ADMINSTATE=', ',')
            continue
        if line.find(' "OT') > -1 and line.find('CARRIER=') > -1 and line.find('::NETDOMAIN=') > -1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Gcc
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'NETDOMAIN=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'OPER_CARRIER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'PROTOCOL=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'FCS_MODE=', '"')
            continue
        if 'RTRV-TELNET' in line:
            getTelnet = 1
            getSsh = 0
            getFtp = 0
        if 'SHELF-' in line and '::MAXSESSIONS=' in line and getTelnet == 1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Tel
            location = AID + '@' + str(idx)
            f1 = FISH(line, ',SERVER=', '"')
            DCN_ALL[location] = f1
            dMSFT__SHELFID_PARAM[AID + '+TELNET-SERVER'] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':MAXSESSIONS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',IDLETIMEOUT=', ',')
            continue
        if 'RTRV-SSH' in line:
            getTelnet = 0
            getSsh = 1
            getFtp = 0
        if 'SHELF-' in line and '::MAXSESSIONS=' in line and getSsh == 1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            line = line[:-2] + ','
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Ssh
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'SERVER=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'MAXSESSIONS=', ',')
            DCN_ALL[location] = f1
            dMSFT__SHELFID_PARAM[AID + '+SSH-MAXSESSIONS'] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, 'IDLETIMEOUT=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'CIPHER=', ',')
            DCN_ALL[location] = '"\n' + f1.replace('&', '  \n') + '\n"'
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'HMAC=', ',')
            DCN_ALL[location] = '"\n' + f1.replace('&', '  \n') + '\n"'
            continue
        if line.find('KEYTYPE') > -1 and line.find('KEYSIZE') > -1:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            line = line[:-2] + ','
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Ssh_PubKey
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'KEYTYPE=', ',')
            DCN_ALL[location] = f1
            dMSFT__SHELFID_PARAM[AID + '+SSH-KEYTYPE'] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            f1 = FISH(line, 'KEYSIZE=', ',')
            DCN_ALL[location] = f1
            dMSFT__SHELFID_PARAM[AID + '+SSH-KEYSIZE'] = f1
            continue
        if 'SHELF-' in line and '::HTTP' in line:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Http
            location = AID + '@' + str(idx)
            f1 = FISH(line, ':HTTP=', ',')
            DCN_ALL[location] = f1
            dMSFT__SHELFID_PARAM[AID + '+HTTP'] = f1
            idx = idx + 1
            location = AID + '@' + str(idx)
            if line.find('HTTPS=ON') > -1:
                DCN_ALL[location] = 'ON'
                dMSFT__SHELFID_PARAM[AID + '+HTTPS'] = 'ON'
            else:
                DCN_ALL[location] = 'OFF'
                dMSFT__SHELFID_PARAM[AID + '+HTTPS'] = 'OFF'
            idx = idx + 1
            location = AID + '@' + str(idx)
            if line.find('REST=ON') > -1:
                DCN_ALL[location] = 'ON'
                dMSFT__SHELFID_PARAM[AID + '+REST'] = 'ON'
            else:
                DCN_ALL[location] = 'OFF'
                dMSFT__SHELFID_PARAM[AID + '+REST'] = 'OFF'
            continue
        if 'RTRV-FTP' in line:
            getTelnet = 0
            getSsh = 0
            getFtp = 1
        if 'SHELF-' in line and '::MAXSESSIONS=' in line and getFtp == 1:
            l1 = line.find('::')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('SHELF') > -1:
                lSHELF.append(AID)
            lAID.append(AID)
            idx = i_Ftp
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',SERVER=', '"')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ':MAXSESSIONS=', ',')
            idx = idx + 1
            location = AID + '@' + str(idx)
            DCN_ALL[location] = FISH(line, ',IDLETIMEOUT=', ',')
            continue

    unique_lSHELF = set(lSHELF)
    unique_lSHELF = sorted(unique_lSHELF)
    unique_lAID = set(lAID)
    unique_lAID = sorted(unique_lAID)
    arranged_AID = []
    for SHELF in unique_lSHELF:
        Sh0 = SHELF.replace('SHELF-', '')
        arranged_AID.append(SHELF)
        for aid in unique_lAID:
            if aid.find('SHELF-') > -1:
                continue
            l1 = aid.find('-') + 1
            f1 = aid[l1:]
            l1 = f1.find('-')
            Sh = f1[0:l1]
            if Sh0 == Sh:
                arranged_AID.append(aid)

    CCC = []
    CCC.append(lPoint)
    nPoint = len(lPoint)
    jj = list(range(1, nPoint))
    for aid in arranged_AID:
        ccc = []
        ccc.append(aid)
        for idx in jj:
            location = aid + '@' + str(idx)
            try:
                f2 = DCN_ALL[location]
                f2.replace('\n', '')
            except KeyError:
                f2 = ''

            ccc.append(f2)

        CCC.append(ccc)

    writer = csv.writer(F_NOW)
    for i in range(len(max(CCC, key=len))):
        writer.writerow([ (c[i] if i < len(c) else '') for c in CCC ])

    return None


def PRINT_DICTIONARY(dDICT, Title, DictName):
    F_Out = open('!__' + DictName + '.txt', 'w')
    F_Out.write('\n\n\t %s \n' % Title)
    for i in sorted(dDICT.keys()):
        F_Out.write('\n %s[%s] = %s' % (DictName, i, dDICT[i]))

    F_Out.write('\n ########################### \n\n')
    F_Out.close()
    return None


def PRINT_LIST(lLIST, lName, F_Out):
    F_Out.write('\n\nReport List: %s' % lName)
    for i in range(len(lLIST)):
        F_Out.write('\n %s[%d] = %s' % (lName, i, lLIST[i]))

    F_Out.write('\nEnd of List Report\n\n')
    return None


def PARSE_RTRV_COND(TID, FileName, F_IN):
    fOut = ''
    ALRMSTAT = 'Enabled'
    for line in F_IN:
        if line.find('====>') > -1:
            l1 = line.find('me=') + 3
            TimeStamp = line[l1:-1]
        if line.find('>>>Begin: RTRV-ALM-ENV') > -1:
            break
        if line.find('ALRMSTAT=DISABLED;') > -1:
            ALRMSTAT = 'Disabled'
        if line.find('\\"') > -1:
            line = line.strip('\n')
            if len(line) < 10:
                continue
            l1 = line.find(',')
            AID = line[4:l1]
            l1 += 1
            l2 = line.find(':')
            AIDTYPE = line[l1:l2]
            l1 = l2 + 1
            l2 = line.rfind(':')
            f1 = line[l1:l2]
            f2 = f1.split(',')
            f1 = f2[0]
            if f1 == 'CR':
                NTFCNCDE = 'CRITICAL'
            elif f1 == 'MJ':
                NTFCNCDE = 'MAJOR'
            elif f1 == 'MN':
                NTFCNCDE = 'MINOR'
            else:
                NTFCNCDE = 'Not Alarmed'
            CONDTYPE = f2[1]
            f1 = f2[2]
            if f1 == 'SA':
                SRVEFF = 'Service Affecting'
            elif f1 == 'NSA':
                SRVEFF = 'Not Service Affecting'
            else:
                SRVEFF = ' '
            OCRDAT = f2[3]
            YEAR = FISH(line, 'YEAR=', ',')
            OCRTM = f2[4]
            Time = YEAR + ' : ' + OCRDAT + ' : ' + OCRTM
            CONDDESCR = FISH(line, ',\\"', '\\"')
            CARDTYPE = FISH(line, 'CARDTYPE=\\"', '\\"')
            ADDITIONALINFO = FISH(line, 'ADDITIONALINFO=\\"', '\\"')
            fOut += TimeStamp + ',' + TID + ',' + AID + ',' + AIDTYPE + ',' + ALRMSTAT + ',' + NTFCNCDE + ',' + SRVEFF + ',' + Time + ',' + CONDDESCR + ',' + ADDITIONALINFO + ',' + CARDTYPE + ',' + '\n'

    if fOut != '':
        f1 = open(str(FileName), 'w')
        f1.write('Capture Time,TID,AID,Class,Alarm Status,Severity,Service,Raised at Year : mm-dd : hh-mm-ss,Description,Additional Info,Card Type,\n' + fOut)
        f1.close()
    return fOut


def PARSE_RTRV_VOA(linesIn, TID, dMEMBERS, dCPACK, F_Out):
    f1 = 'TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Circuit Pack,VOA Mode,Target Loss,LOS Threshold,Auto IS Time left (hh-mm),OPTMON OPOUT,Pstate,Sstate,\n'
    F_Out.write(f1)
    dVOA_OPOUT = {}
    for line in linesIn:
        if line.find('VOA:OPOUT-OTS') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            dVOA_OPOUT[AID] = f1[2]
        if line.find('::') > -1 and line.find('VOAMODE') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[3].strip(' "\n\r')
            f1 = f2.split(',')
            PRI = f1[0]
            try:
                SEC = f1[1]
            except IndexError:
                SEC = ''

            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            l1 = AID.find('-') + 1
            l2 = AID.rfind('-') - 1
            f1 = AID[0:l2]
            l2 = f1.rfind('-')
            shelf = AID[l1:l2]
            SHELF = 'SHELF-' + shelf
            VOAMODE = FISH(line, 'VOAMODE=', ',')
            TARGLOSS = FISH(line, 'TARGLOSS=', ',')
            LOSTHRES = FISH(line, 'LOSTHRES=', ':')
            AINSTIMELEFT = FISH(line, 'AINSTIMELEFT=', ',')
            s1 = ShSl + '-'
            CP = ''
            for j in dCPACK.items():
                if s1.find(j[0]) > -1:
                    CP = j[1]
                    continue

            s1 = TID + ',' + SHELF + ',-,-,-,-,' + AID + ',' + CP
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID + ',' + CP
                    break

            try:
                f1 = dVOA_OPOUT[AID]
            except KeyError:
                f1 = ''

            f1 = s1 + ',' + VOAMODE + ',' + TARGLOSS + ',' + LOSTHRES + ',' + AINSTIMELEFT + ',' + f1 + ',' + PRI + ',' + SEC + ','
            F_Out.write(f1 + '\n')

    return None


def PARSE_RTRV_TELEMETRY(linesIn, dCPACK, dMEMBERS, TID, F_Out, F_ERROR):
    f1 = 'TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,LOS Thershold,CW Optical Power,Max Acquisition Time (s), Short Pulse (ns),Short Distance (m), Long Pulse (ns),Long Distance (m),Office Pulse (ms),Office Distance (m),OTDR Power,OTDR Loss Per Event,OTDR Loss All Events,OTDR Reflection Per Event,OTDR Reflection All Events, Total Fiber Loss,Tx TG Power,Received TG  Power,Span Loss (dB),Fiber Type,Disc Fiber Type,Total Loss,Total Reflection,Pstate,Sstate\n'
    F_Out.write(f1)
    fError = ''
    for line in linesIn:
        if line.find('::') > -1 and line.find(',DISTANCE') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[3].strip(' "\n\r')
            f1 = f2.split(',')
            PRI = f1[0]
            try:
                SEC = f1[1]
            except IndexError:
                SEC = ''

            l1 = AID.split('-')
            SHELF = 'SHELF-' + l1[1]
            ShSl = '-' + l1[1] + '-' + l1[2]
            s1 = TID + ',' + SHELF + ',-,-,-,-,-,' + AID
            l1 = 'LIM=SRA' + ShSl
            l2 = 'LIM=ESAM' + ShSl
            for j in dMEMBERS.items():
                if j[1].find(l1) > -1 or j[1].find(l2) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID
                    break

            LOSTHRES = FISH(line, 'LOSTHRES=', ',')
            TELEPWR = FISH(line, 'TELEPWR=', ',')
            MEASTIME = FISH(line, 'MEASTIME=', ',')
            PULSESHORT = FISH(line, 'PULSESHORT=', ',')
            DISTANCESHORT = FISH(line, 'DISTANCESHORT=', ',')
            PULSELONG = FISH(line, 'PULSELONG=', ',')
            DISTANCELONG = FISH(line, 'DISTANCELONG=', ',')
            PULSEOFFICE = FISH(line, 'PULSEOFFICE=', ',')
            DISTANCEOFFICE = FISH(line, 'DISTANCEOFFICE=', ':')
            LOSSSINGLE = FISH(line, 'LOSSSINGLE=', ',')
            LOSSALL = FISH(line, 'LOSSALL=', ',')
            REFLSINGLE = FISH(line, 'REFLSINGLE=', ',')
            REFLALL = FISH(line, 'REFLALL=', ',')
            TOTFBRLOSS = FISH(line, 'TOTFBRLOSS=', ',')
            SIGNALPWR = FISH(line, 'SIGNALPWR=', ',')
            RXPWR = FISH(line, 'RXPWR=', ',')
            SPANLOSS = FISH(line, 'SPANLOSS=', ':')
            FIBERTYPE = FISH(line, 'FIBERTYPE=', ',')
            DISCFIBERTYPE = FISH(line, 'DISCFIBERTYPE=', ',')
            TOTLLOSS = FISH(line, 'TOTLLOSS=', ',')
            TOTLREFL = FISH(line, 'TOTLREFL=', ',')
            OTDRSIGPWR = FISH(line, 'OTDRSIGPWR=', ',')
            if dCPACK[ShSl + '-'] == 'SRA':
                if float(REFLSINGLE) > -27.0:
                    fError += ',' + AID + ',SRA Telemetry with modified Single event reflection threshold ( ' + REFLSINGLE + ' vs -27 dB in Rel 12.x)\n'
                if float(REFLALL) > -24.0:
                    fError += ',' + AID + ',SRA Telemetry with modified total event reflection threshold ( ' + REFLALL + ' vs -24 dB in Rel 12.x)\n'
                if float(LOSSSINGLE) > 1.5:
                    fError += ',' + AID + ',SRA Telemetry with modified single event loss threshold ( ' + REFLALL + ' vs 1.5 dB in Rel 12.x)\n'
                if float(LOSSALL) > 3.0:
                    fError += ',' + AID + ',SRA Telemetry with modified total event loss threshold ( ' + REFLALL + ' vs 3.0 dB in Rel 12.x)\n'
            f1 = s1 + ',' + LOSTHRES + ',' + TELEPWR + ',' + MEASTIME + ',' + PULSESHORT + ',' + DISTANCESHORT + ',' + PULSELONG + ',' + DISTANCELONG + ',' + PULSEOFFICE + ',' + DISTANCEOFFICE + ',' + OTDRSIGPWR + ',' + LOSSSINGLE + ',' + LOSSALL + ',' + REFLSINGLE + ',' + REFLALL + ',' + TOTFBRLOSS + ',' + SIGNALPWR + ',' + RXPWR + ',' + SPANLOSS + ',' + FIBERTYPE + ',' + DISCFIBERTYPE + ',' + TOTLLOSS + ',' + TOTLREFL + ',' + PRI + ',' + SEC + ','
            F_Out.write(f1 + '\n')

    if fError != '':
        F_ERROR.write('\nTelemetry Threshold Issues (see tab Telemetry)\n' + fError)
    return None


def PARSE_RTRV_OTDR_EVENTS(linesIn, dMEMBERS, TID, F_Out):
    f1 = 'TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,AID,Trace Date,Trace Time,Trace Type,Trace Tag,Event Type,Event Distance,Event Value,\n'
    F_Out.write(f1)
    for line in linesIn:
        if line.find('::') > -1 and line.find(',TRACETIME=') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            l1 = AID.find('-') + 1
            l2 = AID.rfind('-') - 1
            f1 = AID[0:l2]
            l2 = f1.rfind('-')
            shelf = AID[l1:l2]
            SHELF = 'SHELF-' + shelf
            s1 = TID + ',' + SHELF + ',-,-,-,-,' + AID
            for j in dMEMBERS.items():
                if j[1].find(ShSl) > -1:
                    s1 = TID + ',' + SHELF + ',' + j[0] + ',' + AID
                    break

            TRACEDATE = FISH(line, 'TRACEDATE=', ',')
            TRACETIME = FISH(line, 'TRACETIME=', ',')
            TYPE = FISH(line, 'TYPE=', ',')
            DISTANCE = FISH(line, 'DISTANCE=', ',')
            if TYPE == 'LOSS':
                VALUE = FISH(line, ',LOSSVALUE=', ',')
            else:
                VALUE = FISH(line, ',REFLVALUE=', ',')
            TRACETYPE = FISH(line, 'TRACETYPE=', ',')
            l1 = line.replace('"', ',')
            TRACETAG = FISH(l1, 'TRACETAG=', ',')
            f1 = s1 + ',' + TRACEDATE + ',' + TRACETIME + ',' + TRACETYPE + ',' + TRACETAG + ',' + TYPE + ',' + DISTANCE + ',' + VALUE + ','
            F_Out.write(f1 + '\n')

    return None


def PARSE_RTRV_TOPO_SLOTSEQ(linesIn, TID, dMEMBERS, F_Out):
    f1 = 'TID,Shelf,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Sequence ID,Anchor,Label,Add Sequence,Drop Sequence,Intersecting Status,Intersecting Slot Sequence,OTS Members,\n'
    F_Out.write(f1)
    for line in linesIn:
        if 'SLOTSEQ-' in line and 'LABEL=' in line:
            SLOTSEQ = FISH(line, ' "', ':')
            f1 = SLOTSEQ.split('-')
            SHELF = f1[1]
            l1 = f1[2]
            ots = 'OTS-' + SHELF + '-' + l1
            ANCHOR = FISH(line, 'ANCHOR=', ',')
            ADDSEQ = FISH(line, 'ADDSEQ=', ',')
            DROPSEQ = FISH(line, 'DROPSEQ=', ',')
            LABEL = FISH(line, 'LABEL=\\"', '\\"')
            INTERSECTINGSTATUS = FISH(line, 'INTERSECTINGSTATUS=', ',')
            INTERSECTINGSLOTSEQ = FISH(line, 'INTERSECTINGSLOTSEQ=', '"')
            s1 = ',,,,'
            f1 = ''
            for j, k in dMEMBERS.items():
                if ots in j:
                    s1 = j
                    if 'Main' in LABEL:
                        f1 = k
                    break

            f1 = TID + ',' + SHELF + ',' + s1 + ',' + SLOTSEQ + ',' + ANCHOR + ',' + LABEL + ',' + ADDSEQ + ',' + DROPSEQ + ',' + INTERSECTINGSTATUS + ',' + INTERSECTINGSLOTSEQ + ',' + f1 + '\n'
            F_Out.write(f1)

    return None


def PARSE_RTRV_DISP(linesIn, dDISPots, TID, F_Out, F_ERROR):
    f1 = 'TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Slot Sequencing,AID,Location,DISP Type,DISP Module,Input Loss,Output Loss,DISP Loss,Pstate,Sstate,\n'
    F_Out.write(f1)
    dDISP = {}
    for line in linesIn:
        if line.find('::') > -1 and line.find('RTRV-') < 0:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[3].strip(' "\n\r')
            f1 = f2.split(',')
            PRI = f1[0]
            try:
                SEC = f1[1]
            except IndexError:
                SEC = ''

            l1 = AID.find('-') + 1
            l2 = AID.rfind('-') - 1
            f1 = AID[0:l2]
            l2 = f1.rfind('-')
            shelf = AID[l1:l2]
            SHELF = 'SHELF-' + shelf
            INST = FISH(line, 'INST=', ',')
            LIMSLOT = FISH(line, 'LIMSLOT=', ',')
            LIMPORT = FISH(line, 'LIMPORT=', ',')
            location = 'LIM-' + shelf + '-' + LIMSLOT + '-' + LIMPORT
            DSCMTYPE = FISH(line, 'DSCMTYPE=', ',')
            DSCMMOD = FISH(line, 'DSCMMOD=', ',')
            INPUTLOSS = FISH(line, 'INPUTLOSS=', ',')
            OUTPUTLOSS = FISH(line, 'OUTPUTLOSS=', ',')
            if DSCMTYPE != 'TYPE6':
                DSCMAVGLOSS = FISH(line, 'DSCMAVGLOSS=', ',')
                DSCMLENGTH = FISH(line, 'DSCMLENGTH=', ',')
                f1 = float(INPUTLOSS) + float(DSCMLENGTH) * float(DSCMAVGLOSS) + float(OUTPUTLOSS)
                dscmLoss = str(f1) + ' dB'
            else:
                f1 = float(INPUTLOSS) + float(OUTPUTLOSS)
                dscmLoss = DSCMMOD.replace('DB', ' dB')
            f1 = 'ADJ-' + shelf + '-' + LIMSLOT + '-' + LIMPORT
            try:
                label = dDISPots[AID]
            except KeyError:
                F_ERROR.write(',' + AID + ',Not provisioned in any OTS group \n')
                label = ',,,'

            if label.find('PROVISIONED') > -1:
                if LIMSLOT == '0' and LIMPORT == '0':
                    F_ERROR.write(',' + AID + ',LIMSLOT and LIMPORT were not provisioned in a PROVISIONED OTS group\n')
                elif LIMSLOT == '0':
                    F_ERROR.write(',' + AID + ',LIMSLOT was not provisioned in a PROVISIONED OTS group\n')
                elif LIMPORT == '0':
                    F_ERROR.write(',' + AID + ',LIMPORT was not provisioned in a PROVISIONED OTS group\n')
            if dscmLoss[0] == '0':
                l1 = len(dscmLoss)
                f2 = dscmLoss[1:l1]
                dDISP[f1] = f2.replace('dB', '')
            else:
                dDISP[f1] = dscmLoss.replace('dB', '')
            f1 = SHELF + ',' + label + ',' + AID + ',' + location + ',' + DSCMTYPE + ',' + DSCMMOD + ',' + INPUTLOSS + ',' + OUTPUTLOSS + ',' + dscmLoss + ',' + PRI + ',' + SEC + ','
            F_Out.write(TID + ',' + f1 + '\n')

    return dDISP


def PARSE_RTRV_OSC(linesIn, dOSCots, TID, F_Out, F_ERROR):
    dOSC = {}
    dOSC_RX = {}
    dRTD_DIST = {}
    stringOut = ''
    fErr = ''
    for line in linesIn:
        if line.find('  "OSC-') > -1:
            f1 = line.split(':')
            f2 = f1[0].replace(',OSC', '')
            AID = f2.replace('   "', '')
            if line.find('STATUS') > -1:
                if line.find('STATUS=INPROGRESS') > -1:
                    STATUS = 'INPROGRESS'
                    fErr += ',' + AID + ',OSC RTD for release higher than 11 should have status = RELEASED \n'
                else:
                    STATUS = FISH(line, 'STATUS=', ',')
                DIST = FISH(line, 'ESTIMATEDDISTANCE=', ',')
                if line.find('BASELINE_') > -1:
                    LATENCY = FISH(line, 'UNILATENCY=', ',')
                else:
                    LATENCY = FISH(line, 'UNILATENCY=', '"')
                dOSC[AID] = STATUS + ',' + DIST + ',' + LATENCY
                dRTD_DIST[AID] = DIST
            elif line.find('OPR-OCH') > -1:
                dOSC_RX[AID] = FISH(line, 'OPR-OCH,', ',')
        if line.find('RXPATHLOSS') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f2 = f1[3].strip(' "\r')
            f1 = f2.split(',')
            PRI = f1[0]
            try:
                SEC = f1[1]
            except IndexError:
                SEC = ''

            l1 = AID.find('-') + 1
            l2 = AID.rfind('-') - 1
            f1 = AID[0:l2]
            l2 = f1.rfind('-')
            shelf = AID[l1:l2]
            SHELF = 'SHELF-' + shelf
            line = line.replace(':', ',')
            OSCTX = FISH(line, 'OSCTXPOW=', ',')
            if line.find('OSCRECEIVEPOW') > -1:
                OSCRX = FISH(line, 'OSCRECEIVEPOW=', ',')
            else:
                try:
                    OSCRX = dOSC_RX[AID]
                except:
                    OSCRX = ''

            try:
                f1 = float(OSCRX)
            except:
                fErr += ',' + AID + ',OSC RX reported ' + OSCRX + ' \n'

            RXPATH = FISH(line, 'RXPATHLOSS=', ',')
            WAVE = FISH(line, 'OSCTXWAVELENGTH=', ',')
            LOSS = FISH(line, 'OSCSPANLOSS=', ',')
            BER = FISH(line, 'OSCESTBER=', ',')
            DMENABLE = FISH(line, 'DMENABLE=', ',')
            DMCOUNT = FISH(line, 'DMCOUNT=', ',')
            DMDISTANCE = FISH(line, 'DMDISTANCE=', ',')
            try:
                f1 = float(DMDISTANCE)
            except:
                try:
                    f1 = float(dRTD_DIST[AID])
                except:
                    fErr += ',' + AID + ',No OSC distance captured\n'

            try:
                s1 = dOSC[AID]
            except:
                s1 = '-,-,-'

            try:
                s2 = dOSCots[AID]
            except KeyError:
                fErr += ',' + AID + ',Not provisioned in any OTS group \n'
                s2 = ',,,,,'

            stringOut += TID + ',' + SHELF + ',' + s2 + ',' + AID + ',' + OSCTX + ',' + OSCRX + ',' + RXPATH + ',' + WAVE + ',' + BER + ',' + LOSS + ',' + DMENABLE + ',' + DMCOUNT + ',' + DMDISTANCE + ',' + s1 + ',' + PRI + ',' + SEC + '\n'

    if fErr != '':
        F_ERROR.write('\nOSC Distance Issues (see tab OSC)\n' + fErr)
    if stringOut != '':
        F_Out.write('TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Slot Sequencing,AID,Tx Power,Rx Power,Rx Cord Loss,Wavelength,BER,Span Loss,Delay Measurement,One Way Latency,Distance,RTD Status,RTD Span Distance,RTD One Way Latency,Pstate,Sstate\n' + stringOut)
    return None


def PARSE_RTRV_ADJACENCY(linesIn, dCPG, dLOSS, dMEMBERS, TID, F_Out, F_ERROR):
    import collections
    F_Out.write('TID,SHELF,OTS,OSID,TX Path ID,RX Path ID,AID,Type,Provisoned FE AID,Discovered FE AID,Provisioned FE Form,Discovered FE Form,CLFI,Status,Point-A Label,Point-A,Excess Loss,Pad Loss,System Configuration Loss,Patch-cord Loss,Point-Z,Pstate,Sstate,Fiber Type,Span Loss,Target Span Loss,Margin,Minimum Loss,Span Loss Source,Notes,\n')
    LineADJ = {}
    FiberADJ = {}
    FiberADJ_NOTES = {}
    dTxADJACENCY = {}
    dRxADJACENCY = {}
    d_TxAid_OTMx = {}
    txList = []
    d_RxAid_OTMx = {}
    rxList = []
    for line in linesIn:
        if line.find('FIBERTYPE=') > -1:
            NOTES = ''
            f1 = line.split('::')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            FIBERTYPE = FISH(line, 'FIBERTYPE=', ',')
            OSCSPANLOSS = FISH(line, 'OSCSPANLOSS=', ',')
            TARGSPANLOSS = FISH(line, 'TARGSPANLOSS=', ',')
            MINSPANLOSS = FISH(line, 'MINSPANLOSS=', ',')
            CLFI = FISH(line, 'CLFI=\\"', '\\"')
            if line.find('SPANLOSSSOURCE') > -1:
                SPANLOSSSOURCE = FISH(line, 'SPANLOSSSOURCE=', ',')
                SPANLOSSMARGIN = FISH(line, 'SPANLOSSMARGIN=', ',')
            else:
                SPANLOSSSOURCE = 'OSC'
                SPANLOSSMARGIN = FISH(line, 'SPANLOSSMARGIN=', '"')
            if FIBERTYPE == '' or FIBERTYPE == 'OTHER' or FIBERTYPE == 'UNKNOWN':
                F_ERROR.write(',' + AID + ',Fiber Type (=' + FIBERTYPE + ') is not provisioned \n')
                NOTES = 'Fiber type not provisioned'
            if TARGSPANLOSS == '0.00' or SPANLOSSMARGIN == '0.00':
                F_ERROR.write(',' + AID + ',Target and/or Margin loss are not provisioned \n')
                if NOTES == '':
                    NOTES = 'TARGSPANLOSS and/or SPANLOSSMARGIN was set to 0'
                else:
                    NOTES = NOTES + ' & TARGSPANLOSS and/or SPANLOSSMARGIN was set to 0'
            if TARGSPANLOSS != '0.00' and OSCSPANLOSS != '':
                f1 = float(OSCSPANLOSS) - float(TARGSPANLOSS)
                if abs(f1) > 0.5:
                    F_ERROR.write(',' + AID + ',The difference between Target (' + str(TARGSPANLOSS) + ') and OSC (' + str(OSCSPANLOSS) + ') span loss is greater than 0.5 dB \n')
                    if NOTES == '':
                        NOTES = 'OSC loss changed since the TARGSPANLOSS provisioning'
                    else:
                        NOTES = NOTES + ' & OSC loss changed since the TARGSPANLOSS provisioning'
            if SPANLOSSMARGIN != '2.00' and line.find(',LINEOUT=\\"\\"') < 0:
                f1 = AID.split('-')
                SHELF = 'SHELF-' + f1[1]
                try:
                    dMSFT__SHELFID_PARAM[SHELF + '+SPANLOSSMARGIN'].append(f1[2] + '+' + SPANLOSSMARGIN)
                except:
                    dMSFT__SHELFID_PARAM[SHELF + '+SPANLOSSMARGIN'] = [f1[2] + '+' + SPANLOSSMARGIN]

            if ISSUES == 'NO':
                NOTES = ''
            LineADJ[AID] = FIBERTYPE + ',' + OSCSPANLOSS + ',' + TARGSPANLOSS + ',' + SPANLOSSMARGIN + ',' + MINSPANLOSS + ',' + SPANLOSSSOURCE + ',' + NOTES
            continue
        if line.find('PADLOSS') > -1:
            f1 = line.split('::')
            AID = f1[0].replace('   "', '')
            EXCESSLOSS = FISH(line, 'EXCESSLOSS=', ',')
            PADLOSS = FISH(line, 'PADLOSS=', ',')
            line = line[:-2] + ','
            SCL = FISH(line, ',SCL=', ',')
            FIBERLOSS = FISH(line, 'FIBERLOSS=', ',')
            FiberADJ[AID] = EXCESSLOSS + ',' + PADLOSS + ',' + SCL + ',' + FIBERLOSS
            NOTES = ''
            if FIBERLOSS != '' and FIBERLOSS != 'N/A' and FIBERLOSS != 'LOS' and FIBERLOSS != 'VARYING':
                try:
                    pad = dLOSS[AID]
                    if abs(float(pad) - float(FIBERLOSS)) > 0.5:
                        NOTES = 'More than 0.5 dB difference between measured and provisioned DSCM/Pad value'
                        F_ERROR.write(',' + AID + ', ' + NOTES + '\n')
                    else:
                        NOTES = 'A provisioned ' + pad + ' dB DSCM/Pad is present here'
                except:
                    pass

                try:
                    if SCL != '':
                        try:
                            if float(FIBERLOSS + 0.1) > float(SCL):
                                NOTES = 'Patch-cord has insertion loss (=' + FIBERLOSS + ') higher than SCL'
                                F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                        except:
                            pass

                    elif float(FIBERLOSS) > 0.5:
                        NOTES = 'Patch-cord has insertion loss (=' + FIBERLOSS + ') higher than 0.5 dB (or a not provisioned pad is present)'
                        F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                except:
                    pass

            else:
                if FIBERLOSS == 'LOS':
                    NOTES = 'FIBERLOSS = LOS'
                    F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                elif FIBERLOSS == 'VARYING':
                    NOTES = 'FIBERLOSS = VARYING (is not constant)'
                    F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                if float(EXCESSLOSS) > 0.0:
                    NOTES = 'A ' + EXCESSLOSS + ' dB pad is present here'
            if ISSUES == 'NO':
                NOTES = ''
            FiberADJ_NOTES[AID] = NOTES
            continue
        if line.find(':ADJTYPE=') > -1 and line.find(':IS') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            l1 = f1[3].strip(' "\n\r')
            f2 = l1.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1].strip(' "\n\r')
            except:
                SEC = ''

            ADJTYPE = FISH(line, 'ADJTYPE=', ',')
            PROVFEADDR = FISH(line, 'PROVFEADDR=\\"', '\\",')
            DISCFEADDR = FISH(line, 'DISCFEADDR=\\"', '\\",')
            PADDRFORM = FISH(line, 'PADDRFORM=', ',')
            DADDRFORM = FISH(line, 'DADDRFORM=', ',')
            ADJSTAT = FISH(line, 'ADJSTAT=', ',')
            PORTLABEL = FISH(line, 'PORTLABEL=\\"', '\\"')
            CLFI = FISH(line, 'CLFI=\\"', '\\"')
            l1 = AID.split('-')
            SHELF = 'SHELF-' + l1[1]
            if line.find('ADJTYPE=RX,') > -1:
                dRxADJACENCY[AID] = PRI + ',' + DISCFEADDR + ',' + DADDRFORM + ',' + PROVFEADDR + ',' + PADDRFORM + ',' + CLFI
                try:
                    d_RxAid_OTMx[PROVFEADDR].append(AID)
                except:
                    d_RxAid_OTMx[PROVFEADDR] = [AID]

                rxList.append(PROVFEADDR)
            if line.find('ADJTYPE=TX,') > -1:
                dTxADJACENCY[AID] = PRI + ',' + DISCFEADDR + ',' + DADDRFORM + ',' + PROVFEADDR + ',' + PADDRFORM + ',' + CLFI
                try:
                    d_TxAid_OTMx[PROVFEADDR].append(AID)
                except:
                    d_TxAid_OTMx[PROVFEADDR] = [AID]

                txList.append(PROVFEADDR)
            NOTES = ''
            if PROVFEADDR.count('-') > 2:
                f2 = PROVFEADDR.split('-')
                if PADDRFORM == 'TID-SH-SL-PRT-SBPRT':
                    l1 = len(f2) - 4
                else:
                    l1 = len(f2) - 3
                l2 = l1 + 1
                f1 = l2 + 1
                ShSlPrt = f2[l1] + '-' + f2[l2] + '-' + f2[f1]
                f1 = '-' + f2[l1] + '-' + f2[l2] + '-'
                if ADJTYPE == 'LINE':
                    f2 = 'AMP'
                    NOTES = 'Point-Z is the Far End Amplifier'
                    TO = f2 + ' @ ' + PROVFEADDR
                else:
                    try:
                        f2 = dCPG[f1]
                        TO = f2 + ' @ ' + PROVFEADDR
                    except:
                        NOTES = 'Circuit Pack at SH-SL=' + f1 + ' is not present'
                        TO = ''

            else:
                TO = ''
                ShSl = ''
            if AID.count('-') == 3:
                l1 = AID.find('-')
                l2 = AID.rfind('-') + 1
                f1 = AID[l1:l2]
                ShSl = f1[1:-1]
                try:
                    f3 = dCPG[f1]
                    l2 = len(AID)
                    FROM = f3 + AID[l1:l2]
                except KeyError:
                    FROM = ''

            else:
                ShSl = ''
                FROM = ''
            if PORTLABEL.find('Switch ') > -1 and FROM.find('WSS') > -1 and TO == '':
                NOTES = 'This WSS to WSS adjacency needs provisioning (if required)'
            if FROM.find('BMD-') > -1 or TO.find('BMD-') > -1:
                NOTES = 'Patch-cord loss measurement not available for BMD'
            if ADJTYPE != 'UNKNOWN':
                if FROM.find('XLA-') > -1 and ADJTYPE != 'OPM':
                    l1 = FROM + '>' + f2 + '-' + ShSlPrt
                    try:
                        dMSFT__SHELFID_PARAM[SHELF + '+XLA_ADJ'].append(l1)
                    except:
                        dMSFT__SHELFID_PARAM[SHELF + '+XLA_ADJ'] = [l1]

                elif FROM.find('FIM_') > -1:
                    l1 = FROM + '>' + f2 + '-' + ShSlPrt
                    try:
                        dMSFT__SHELFID_PARAM[SHELF + '+FIM_ADJ'].append(l1)
                    except:
                        dMSFT__SHELFID_PARAM[SHELF + '+FIM_ADJ'] = [l1]

                elif FROM.find('BMD2-') > -1:
                    l1 = FROM + '>' + f2 + '-' + ShSlPrt
                    try:
                        dMSFT__SHELFID_PARAM[SHELF + '+BMD2_ADJ'].append(l1)
                    except:
                        dMSFT__SHELFID_PARAM[SHELF + '+BMD2_ADJ'] = [l1]

            if ADJTYPE != 'LINE':
                try:
                    s2 = LineADJ[AID]
                except KeyError:
                    s2 = ',,,,,'

            else:
                try:
                    s2 = LineADJ[AID]
                    if PROVFEADDR != DISCFEADDR:
                        NOTES = 'Provisoned and discovered far end TID do not match'
                        F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                    if ADJSTAT != 'RELIABLE':
                        NOTES = 'Line Adjacency status is not RELIABLE; verify far end TID address and format provisioning'
                        F_ERROR.write(',' + AID + ',' + NOTES + '\n')
                except:
                    s2 = ',,,,,'

            try:
                s1 = FiberADJ[AID]
                NOTES = FiberADJ_NOTES[AID]
            except:
                s1 = ',,,'
                NOTES = ''

            s3 = ',,,'
            if ShSl != '':
                for j, k in dMEMBERS.items():
                    if ShSl in k:
                        l1 = j.rfind(',')
                        s3 = j[:l1]
                        break

            if ISSUES == 'NO':
                NOTES = ''
            f1 = TID + ',' + SHELF + ',' + s3 + ',' + AID + ',' + ADJTYPE + ',' + PROVFEADDR + ',' + DISCFEADDR + ',' + PADDRFORM + ',' + DADDRFORM + ',' + CLFI + ',' + ADJSTAT + ',' + PORTLABEL + ',' + FROM + ',' + s1 + ',' + TO + ',' + PRI + ',' + SEC + ',' + s2 + ',' + NOTES + ',' + '\n'
            F_Out.write(f1)

    txList = filter(None, txList)
    f1 = [ x for x, y in collections.Counter(txList).items() if y > 1 ]
    if len(f1) > 0:
        for i in f1:
            s1 = d_TxAid_OTMx[i]
            s2 = ' & '.join(s1)
            F_ERROR.write(',' + i + ',' + 'DWDM CPG was the FE AID for more than one ( ' + s2 + ' ) Tx adjacencies\n')

    rxList = filter(None, rxList)
    f1 = [ x for x, y in collections.Counter(rxList).items() if y > 1 ]
    if len(f1) > 0:
        for i in f1:
            s1 = d_RxAid_OTMx[i]
            s2 = ' & '.join(s1)
            F_ERROR.write(',' + i + ',' + 'DWDM CPG was the FE AID for more than one ( ' + s2 + ' ) Rx adjacencies\n')

    return (dTxADJACENCY, dRxADJACENCY)


def EXTRACT_DSM(linesIn):
    l_DSM_PM = []
    for line in linesIn:
        if line.find('-%HLINK-OC3-') > -1:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('DS1TM-1-') > -1:
                AID = AID.replace('DS1TM-1-', 'DS1-1-ALL-')
                l_DSM_PM.append(AID)
            elif AID.find('DS1TM-2-') > -1:
                AID = AID.replace('DS1TM-2-', 'DS1-2-ALL-')
                l_DSM_PM.append(AID)

    return l_DSM_PM


def PARSE_RTRV_EQUIPMENT___________(SITE_NAME, linesIn, F_Out, TID):
    f1 = 'TID,Site Name,SHELF,AID,Pstate,Sstate,PEC,CP Description,Provisioned PEC,Baseline,Serial#,CLEI,Auto Equip,'
    f1 += 'Equipment Mode,Mate Equipment1,Mate Equipment2,Mate Equipment3,'
    f1 += 'Provisioning Mode,Timing Group ID,Equipment Profile,Equipment Profile 2,On since (YY-DDD-HH-MM),Carrier 1,Carrier 2,\n'
    F_Out.write(f1)
    d_AUTOEQ__ShSl = {}
    dMODE__ShSl = {}
    SH0 = -1
    for line in linesIn:
        if line.find('::') > -1 and line.find('RTRV-') < 0:
            line = line[:-2] + ','
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('%HLINK-OC3') > -1:
                s1 = AID.split('-')
                Sh = s1[4]
                SHELF = 'SHELF-' + s1[4]
                ShSl = '-' + s1[1] + '-' + s1[2]
            else:
                s1 = AID.split('-')
                if AID.count('-') > 1:
                    Sh = s1[1]
                    SHELF = 'SHELF-' + Sh
                    ShSl = '-' + Sh + '-' + s1[2]
                else:
                    Sh = '0'
                    ShSl = '-' + Sh + '-' + s1[1]
            if line.find('::MODE=') > -1:
                l1 = line.find('=') + 1
                l2 = len(line) - 2
                d_AUTOEQ__ShSl[ShSl] = line[l1:l2]
                continue
            if line.find('EQPTMODE=') > -1:
                f1 = FISH(line, 'EQPTMODE=', ',') + ','
                f2 = line.replace('"', ',')
                if f2.find('MATEEQPT1') > -1:
                    f1 += FISH(f2, 'MATEEQPT1=', ',') + ','
                else:
                    f1 += ','
                if f2.find('MATEEQPT2') > -1:
                    f1 += FISH(f2, 'MATEEQPT2=', ',') + ','
                else:
                    f1 += ','
                if f2.find('MATEEQPT3') > -1:
                    f1 += FISH(f2, 'MATEEQPT3=', ',') + ','
                else:
                    f1 += ','
                dMODE__ShSl[ShSl] = f1
                continue
            SH1 = int(Sh)
            if SH0 != SH1:
                if SH0 == -1:
                    SH0 = SH1
                else:
                    SH0 = SH1
                    F_Out.write('\n')
            if line.find('CTYPE') < 0:
                continue
            else:
                l1 = line.rfind(':') + 1
                l2 = len(line) - 1
                States = line[l1:l2]
                line = line.replace(':', ',')
            line = line.replace(':', ',')
            PROVPEC = FISH(line, 'PROVPEC=', ',')
            CTYPE = FISH(line, 'CTYPE=\\"', '\\"')
            CTYPE = CTYPE.replace(',', ';')
            PEC = FISH(line, ',PEC=', ',')
            REL = FISH(line, ',REL=', ',')
            SER = FISH(line, ',SER=', ',')
            CLEI = FISH(line, ',CLEI=', ',')
            PROVMODE = FISH(line, 'PROVMODE=', ',')
            TMGID = FISH(line, ',TMGID=', ',')
            AGE = FISH(line, ',AGE=', ',')
            ONSC = FISH(line, ',ONSC=', ',')
            EQPTPROFILE = FISH(line, ',EQPTPROFILE=', ',')
            EQPTPROFILE2 = FISH(line, ',EQPTPROFILE2=', ',')
            CARRIER1 = FISH(line, ',CARRIER1=', ',')
            CARRIER2 = FISH(line, ',CARRIER2=', ',')
            if AID.count('-') == 2:
                try:
                    f2 = dMODE__ShSl[ShSl]
                except:
                    f2 = ',,,,'

            else:
                f2 = ',,,,'
            try:
                f1 = d_AUTOEQ__ShSl[ShSl]
            except:
                try:
                    f1 = SHELF.replace('SHELF-', '')
                    l1 = ShSl.replace(f1, '0')
                    f1 = d_AUTOEQ__ShSl[l1]
                except:
                    f1 = ''

            s1 = SHELF + ',' + AID + ',' + States + ',' + PEC + ',' + CTYPE + ',' + PROVPEC + ',' + REL + ',' + SER + ',' + CLEI + ',' + f1 + ',' + f2 + PROVMODE + ',' + TMGID + ',' + EQPTPROFILE + ',' + EQPTPROFILE2 + ',' + ONSC + ',' + CARRIER1 + ',' + CARRIER2
            F_Out.write(TID + ',' + SITE_NAME + ',' + s1 + '\n')

    return None


def PARSE_RTRV_CPL_RAMAN(linesIn, TID, F_Out):
    F_Out.write('TID,AID,Shutoff Mode,Target Power,Input LOS Threshold,SO Threshold,Turn On Threshold,ARP Threshold,ARP Target Power,Input Loss,Pump1 %,Pump2 %,Pump3 %,Pump4 %,Pump1 Power,Pump2 Power,Pump3 Power,Pump4 Power,Auto SO Disabled Time,Actual SO Disabled Time,OPIN,OPOUT,ORL,Pstate,Sstate,\n')
    ORL = {}
    OPIN = {}
    OPOUT = {}
    for line in linesIn:
        if line.find(',RAMAN:ORL-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            ORL[AID] = f1[2]
        if line.find(',RAMAN:OPIN-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPIN[AID] = f1[2]
        if line.find(',RAMAN:OPOUT-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPOUT[AID] = f1[2]
        if line.find('TARGPOW=') > -1:
            s1 = line.split(':')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            f1 = s1[3].strip(' "\n\r')
            f2 = f1.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1]
            except:
                SEC = ''

            f1 = s1[2]
            SHUTOFFMODE = FISH(f1, 'SHUTOFFMODE=', ',')
            TARGPOW = FISH(f1, 'TARGPOW=', ',')
            INPUTLOSTHRES = FISH(f1, 'INPUTLOSTHRES=', ',')
            SHUTTHRES = FISH(f1, 'SHUTTHRES=', ',')
            TURNONTHRES = FISH(f1, 'TURNONTHRES=', ',')
            APRTHRES = FISH(f1, 'APRTHRES=', ',')
            APRTARGPOW = FISH(f1, 'APRTARGPOW=', ',')
            INPUTLOSS = FISH(f1, 'INPUTLOSS=', ',')
            PUMP1POWRATIO = FISH(f1, 'PUMP1POWRATIO=', ',')
            PUMP2POWRATIO = FISH(f1, 'PUMP2POWRATIO=', ',')
            PUMP3POWRATIO = FISH(f1, 'PUMP3POWRATIO=', ',')
            PUMP4POWRATIO = FISH(f1, 'PUMP4POWRATIO=', ',')
            ACTUALPUMP1POWER = FISH(f1, 'ACTUALPUMP1POWER=', ',')
            ACTUALPUMP2POWER = FISH(f1, 'ACTUALPUMP2POWER=', ',')
            ACTUALPUMP3POWER = FISH(f1, 'ACTUALPUMP3POWER=', ',')
            ACTUALPUMP4POWER = FISH(f1, 'ACTUALPUMP4POWER=', ',')
            AUTOSHUTOFFDISABLEDTIME = FISH(f1, 'AUTOSHUTOFFDISABLEDTIME=', ',')
            ACTUALAUTOSHUTOFFDISABLEDTIME = FISH(f1, 'ACTUALAUTOSHUTOFFDISABLEDTIME=', ',')
            s1 = TID + ',' + AID + ',' + SHUTOFFMODE + ',' + TARGPOW + ',' + INPUTLOSTHRES + ',' + SHUTTHRES + ',' + TURNONTHRES + ',' + APRTHRES + ',' + APRTARGPOW + ',' + INPUTLOSS + ',' + PUMP1POWRATIO + ',' + PUMP2POWRATIO + ',' + PUMP3POWRATIO + ',' + PUMP4POWRATIO + ',' + ACTUALPUMP1POWER + ',' + ACTUALPUMP2POWER + ',' + ACTUALPUMP3POWER + ',' + ACTUALPUMP4POWER + ',' + AUTOSHUTOFFDISABLEDTIME + ',' + ACTUALAUTOSHUTOFFDISABLEDTIME + ',' + OPIN[AID] + ',' + OPOUT[AID] + ',' + ORL[AID] + ',' + PRI + ',' + SEC
            F_Out.write(s1 + '\n')

    return None


def PARSE_RTRV_INVENTORY(linesIn, F_Out, TID):
    f1 = 'TID,SHELF,AID,Type,Actual PEC,REL,CLEI,SER,SNMP Index,'
    f1 += 'Mfg. Date,Age,On Since (YY-DDD-HH-MM),Current Temp,Average Temp\n'
    F_Out.write(f1)
    SH0 = -1
    for line in linesIn:
        if line.find('::') > -1 and line.find('RTRV-') < 0:
            l1 = line.find(':')
            f2 = line[0:l1]
            AID = f2.replace('   "', '')
            if AID.find('%HLINK-OC3') > -1:
                s1 = AID.split('-')
                l2 = s1[4]
            else:
                s1 = AID.split('-')
                l2 = s1[1]
            SHELF = 'SHELF-' + l2
            SH1 = int(l2)
            if SH0 != SH1:
                if SH0 == -1:
                    SH0 = SH1
                else:
                    SH0 = SH1
                    F_Out.write('\n')
            if AID.find('FILLED-') > -1 or AID.find('EMPTY-') > -1 or AID.find('FILLER-') > -1:
                CTYPE = ''
                PEC = ''
                REL = ''
                CLEI = ''
                SER = ''
                SNMPINDEX = ''
                MDAT = ''
                AGE = ''
                ONSC = ''
                TCUR = ''
                TAVG = ''
            else:
                CTYPE = FISH(line, 'CTYPE=\\"', '\\"')
                CTYPE = CTYPE.replace(',', ';')
                line = line.replace('"\r', ',')
                PEC = FISH(line, ',PEC=', ',')
                REL = FISH(line, ',REL=', ',')
                CLEI = FISH(line, ',CLEI=', ',')
                SER = FISH(line, ',SER=', ',')
                SNMPINDEX = FISH(line, ',SNMPINDEX=', ',')
                MDAT = FISH(line, ',MDAT=', ',')
                AGE = FISH(line, ',AGE=', ',')
                ONSC = FISH(line, ',ONSC=', ',')
                TCUR = FISH(line, ',TCUR=', ',')
                TAVG = FISH(line, ',TAVG=', ',')
            f1 = SHELF + ',' + AID + ',' + CTYPE + ',' + PEC + ',' + REL + ',' + CLEI + ',' + SER + ',' + SNMPINDEX + ','
            f1 += MDAT + ',' + AGE + ',' + ONSC + ',' + TCUR + ',' + TAVG
            F_Out.write(TID + ',' + f1 + '\n')

    return None


def PARSE_RTRV_6500_RAMAN(linesIn, dMEMBERS, TID, F_Out, F_ERROR):
    F_Out.write('TID,AID,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Raman Pump Mode,Raman State,Target Power,Pump1 Power,Pump2 Power,Pump3 Power,Pump4 Power,TurnOn Threshold,APR Threshold,Fiber Pinch Threshold,Residual Pump Power Threshold,Pump Mode,Target Gain,Target Gain Tilt,Calibration Flag,Forced Shutoff,Target Unachiecable Minor,Target Unachiecable Major,Gain Mode,Recommended Gain,Calculated Gain,OPIN,OPOUT,ORLIN,ORLIN Baseline,ORLOUT,ORLOUT Baseline,MM-DD-HH-MM,Pstate,Sstate,\n')
    ORLIN = {}
    ORLOUT = {}
    OPIN = {}
    OPOUT = {}
    for line in linesIn:
        if line.find('-6,OPTMON:OPR-OTS') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPIN[AID] = f1[2]
            try:
                OPOUT[AID] = str(float(f1[2]) - 1.7)
            except:
                OPOUT[AID] = ''

        if line.find(',RAMAN:ORLIN-OTS') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find('BASLN') < 0:
                ORLIN[AID] = f1[2]
                try:
                    if float(f1[2]) < 30.0:
                        F_ERROR.write(',' + AID + ',Measured ORLIN (' + f1[2] + ') is below 30 dB \n')
                except ValueError:
                    continue

            else:
                ORLIN[AID] += ',' + f1[2]
        if line.find(',RAMAN:ORLOUT-OTS') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find('BASLN') < 0:
                ORLOUT[AID] = f1[2]
                try:
                    if float(f1[2]) < 30.0:
                        F_ERROR.write(',' + AID + ',Measured ORLOUT (' + f1[2] + ') is below 30 dB \n')
                except ValueError:
                    continue

            else:
                ORLOUT[AID] += ',' + f1[2] + ',' + f1[7] + '-' + f1[8]
        if line.find(',RAMAN:OPIN-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPIN[AID] = f1[2]
        if line.find(',RAMAN:OPOUT-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPOUT[AID] = f1[2]
        if line.find('TARGPOW=') > -1:
            s1 = line.split(':')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            f1 = s1[3].strip(' "\n\r')
            f2 = f1.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1]
            except IndexError:
                SEC = ''

            f1 = s1[2]
            RAMANMODE = FISH(f1, 'RAMANMODE=', ',')
            OPERMODE = FISH(f1, 'OPERMODE=', ',')
            TARGPOW = FISH(f1, 'TARGPOW=', ',')
            PUMP1POWER = FISH(f1, 'PUMP1POWER=', ',')
            PUMP2POWER = FISH(f1, 'PUMP2POWER=', ',')
            PUMP3POWER = FISH(f1, 'PUMP3POWER=', ',')
            PUMP4POWER = FISH(f1, 'PUMP4POWER=', ',')
            TURNONTHRES = FISH(f1, 'TURNONTHRES=', ',')
            APRTHRES = FISH(f1, 'APRTHRES=', ',')
            FBRPNCHTHRES = FISH(f1, 'FBRPNCHTHRES=', ',')
            RESPUMPPWRTHRES = FISH(f1, 'RESPUMPPWRTHRES=', ',')
            PUMPMODE = FISH(f1, 'PUMPMODE=', ',')
            TARGGAIN = FISH(f1, 'TARGGAIN=', ',')
            TARGGAINTILT = FISH(f1, 'TARGGAINTILT=', ',')
            CALIBRATED = FISH(f1, 'CALIBRATED=', ',')
            FORCEDSHUTOFF = FISH(f1, 'FORCEDSHUTOFF=', ',')
            TGTUNACHMIN = FISH(f1, 'TGTUNACHMIN=', ',')
            TGTUNACHMAJ = FISH(f1, 'TGTUNACHMAJ=', ',')
            GAINMODE = FISH(f1, 'GAINMODE=', ',')
            RECGAIN = FISH(f1, 'RECGAIN=', ',')
            CALCGAIN = FISH(f1, 'CALCGAIN=', ',')
            l1 = AID.find('-')
            l2 = AID.rfind('-')
            ShSl = AID[l1:l2]
            l2 = ',,,,'
            for j, k in dMEMBERS.items():
                if ShSl in k:
                    l2 = j
                    break

            s1 = TID + ',' + AID + ',' + l2 + ',' + RAMANMODE + ',' + OPERMODE + ',' + TARGPOW + ',' + PUMP1POWER + ',' + PUMP2POWER + ',' + PUMP3POWER + ',' + PUMP4POWER + ',' + TURNONTHRES + ',' + APRTHRES + ',' + FBRPNCHTHRES + ',' + RESPUMPPWRTHRES + ',' + PUMPMODE + ',' + TARGGAIN + ',' + TARGGAINTILT + ',' + CALIBRATED + ',' + FORCEDSHUTOFF + ',' + TGTUNACHMIN + ',' + TGTUNACHMAJ + ',' + GAINMODE + ',' + RECGAIN + ',' + CALCGAIN + ',' + OPIN[AID] + ',' + OPOUT[AID] + ',' + ORLIN[AID] + ',' + ORLOUT[AID] + ',' + PRI + ',' + SEC
            F_Out.write(s1 + '\n')
            f1 = AID.replace('RAMAN', 'OPTMON')
            f2 = f1[:-1] + '6'
            try:
                s1 = TID + ',' + f2 + ',' + l2 + ',,,,,,,,,,,,,,,,,,,,,,' + OPIN[f2] + ',' + OPOUT[f2]
            except:
                s1 = TID + ',' + f2 + ',' + l2 + '\n'

            F_Out.write(s1 + '\n')
            if PRI == 'IS':
                if CALIBRATED != 'CALIBRATED':
                    F_ERROR.write(',' + AID + ',Needs Calibration\n')
                s1 = abs(float(RECGAIN) - float(CALCGAIN))
                if s1 > 2.5:
                    F_ERROR.write(',' + AID + ',Gain difference between Calculated and Reccomended Gain is greater than 2.5 dB\n')
                if FORCEDSHUTOFF == 'TRUE':
                    F_ERROR.write(',' + AID + ',Raman laser pumps are OFF, ORLIN and ORLOUT levels will change when pumps are ON\n')
                if PUMP1POWER == 'N/A' or PUMP2POWER == 'N/A' or PUMP3POWER == 'N/A' or PUMP4POWER == 'N/A':
                    F_ERROR.write(',' + AID + ',At least one of the Raman pump-lasers had no power\n')
            else:
                F_ERROR.write(',' + AID + ',Raman amplifier not IS: No power & ORL checks were performed\n')

    return None


def PARSE_RTRV_AMPLIFIERS(linesIn, dMEMBERS, dCPACK, lOPTMON, TID, F_Out, F_ERROR):
    f1 = 'TID,SHELF,SLOT,OTS,OSID,TX Path ID,RX Path ID,Reliable Far End AID,Amplifier Gain Range,Amplifier Gain Regime,Amplifier Type,AID,AMP Mode,Reference Bandwith,Peak Power Control State,Target Gain,Target Gain Tilt,Target Power,Target Peak Power,Input Loss,Output Loss,Gain Mode,True Gain,Forced Shut Off,Input LOS Threshold,Output LOS Threshold,Shut Off Threshold,Top Offset, ALSO Disabled,OPIN,OPOUT,ORL,ORL Baseline,MM-DD-HH-MM,PRI,SEC\n'
    F_Out.write(f1)
    ORL = {}
    OPIN = {}
    OPOUT = {}
    AMP = {}
    for line in linesIn:
        AID = ''
        if line.find(',AMP:ORL-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            if line.find('BASLN') < 0:
                ORL[AID] = f1[2]
                try:
                    if float(f1[2]) < 30.0:
                        F_ERROR.write(',' + AID + ',Measured reflection (' + f1[2] + ') is below 30 dB \n')
                except ValueError:
                    continue

            else:
                ORL[AID] += ',' + f1[2] + ',' + f1[7] + '-' + f1[8]
        if line.find(',AMP:OPIN-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPIN[AID] = f1[2]
        if line.find(',AMP:OPOUT-OTS,') > -1:
            f1 = line.split(',')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            OPOUT[AID] = f1[2]
        if line.find(',TARGPOW=') > -1:
            s1 = line.split(':')
            f1 = s1[0]
            AID = f1.replace('   "', '')
            f1 = AID.split('-')
            SHELF = f1[1]
            SLOT = f1[2]
            PORT = f1[3]
            f1 = s1[-1].strip(' "\n\r')
            f2 = f1.split(',')
            PRI = f2[0]
            try:
                SEC = f2[1]
            except IndexError:
                SEC = ''

            f1 = s1[2] + ','
            AMPMODE = FISH(f1, 'AMPMODE=', ',')
            REFBW = FISH(f1, 'REFBW=', ',')
            AMPPKMODE = FISH(f1, 'AMPPKMODE=', ',')
            TARGGAIN = FISH(f1, ',TARGGAIN=', ',')
            TARGGAINTILT = FISH(f1, 'TARGGAINTILT=', ',')
            TARGPOW = FISH(f1, 'TARGPOW=', ',')
            TARGPKPOW = FISH(f1, 'TARGPKPOW=', ',')
            INPUTLOSS = FISH(f1, 'INPUTLOSS=', ',')
            OUTPUTLOSS = FISH(f1, 'OUTPUTLOSS=', ',')
            GAINMODE = FISH(f1, 'GAINMODE=', ',')
            GAIN = FISH(f1, ',GAIN=', ',')
            FORCEDSO = FISH(f1, 'FORCEDSHUTOFF=', ',')
            INPUTLOSTHRES = FISH(f1, 'INPUTLOSTHRES=', ',')
            OUTPUTLOSTHRES = FISH(f1, 'OUTPUTLOSTHRES=', ',')
            SHUTTHRES = FISH(f1, 'SHUTTHRES=', ',')
            TOPOFFSET = FISH(f1, 'TOPOFFSET=', ',')
            if f1.find('MAXTARGPOW') > -1:
                ALSODISABLED = FISH(f1, 'ALSODISABLED=', ',')
            else:
                ALSODISABLED = FISH(f1, 'ALSODISABLED=', ':')
            try:
                orl = ORL[AID]
            except KeyError:
                orl = '-,-,-'

            try:
                f1 = OPIN[AID]
            except:
                f1 = '-'

            try:
                f2 = OPOUT[AID]
            except:
                f2 = '-'

            AMP[AID] = AID + ',' + AMPMODE + ',' + REFBW + ',' + AMPPKMODE + ',' + TARGGAIN + ',' + TARGGAINTILT + ',' + TARGPOW + ',' + TARGPKPOW + ',' + INPUTLOSS + ',' + OUTPUTLOSS + ',' + GAINMODE + ',' + GAIN + ',' + FORCEDSO + ',' + INPUTLOSTHRES + ',' + OUTPUTLOSTHRES + ',' + SHUTTHRES + ',' + TOPOFFSET + ',' + ALSODISABLED + ',' + f1 + ',' + f2 + ',' + orl + ',' + PRI + ',' + SEC
            ShSl = '-' + SHELF + '-' + SLOT
            f1 = AID.replace('LIM', '')
            s1 = ShSl + '-'
            for j, k in dCPACK.items():
                if s1.find(j) > -1:
                    ampType = k
                    break

            if ampType.find('RA') > -1:
                ampShSl = 'RA' + ShSl
            elif ampType == 'XLA':
                ampShSl = 'XLA' + ShSl
            elif ampType.find('SAM') > -1:
                ampShSl = 'SAM' + ShSl
            else:
                ampShSl = 'LIM' + ShSl
            if PRI == 'IS':
                if GAIN != '!' and ampType != 'LIM' and ampType != 'SRA' and ampType != 'SAM' and ampType != 'ESAM':
                    gainRanges = GAIN_RANGE(ampType, PORT, GAINMODE, GAIN)
                    if 'UNKNOWN' in gainRanges:
                        F_ERROR.write(',' + AID + ',The amplifier true gain (=' + str(GAIN) + ') is outside the extendend regime\n')
                else:
                    gainRanges = ','
            else:
                gainRanges = ','
                F_ERROR.write(',' + AID + ',Amplifier not IS:   No power; gain; and ORL checks were performed\n')
            s1 = ',,,,'
            for j, k in dMEMBERS.items():
                if ampShSl in k:
                    s1 = j
                    break

            F_Out.write(TID + ',' + SHELF + ',' + SLOT + ',' + s1 + ',' + gainRanges + ',' + ampType + ',' + AMP[AID] + ',' + '\n')
        if line.find('OPTMON:OPR-OTS,') > -1:
            f1 = line.split(',')
            AID = f1[0].replace('   "', '')
            f2 = AID.split('-')
            SHELF = f2[1]
            SLOT = f2[2]
            PORT = f2[3]
            ShSl = '-' + SHELF + '-' + SLOT
            s1 = ShSl + '-'
            for j, k in dCPACK.items():
                if s1.find(j) > -1:
                    ampType = k
                    break

            s1 = ',,,,'
            for j, k in dMEMBERS.items():
                if ShSl in k:
                    s1 = j
                    break

            if AID in lOPTMON and f1[2] != 'OOR':
                d1 = float(f1[2]) - 1.1
                f2 = str(d1)
                AMP[AID] = AID + ',' + ',,,,,,,,,,,,,,,,,' + f1[2] + ',' + f2
                F_Out.write(TID + ',' + SHELF + ',' + SLOT + ',' + s1 + ',,,' + ampType + ',' + AMP[AID] + ',' + '\n')

    return None


def GAIN_RANGE(ampType, port, xlamode, gain):
    if gain == '':
        out = ','
        return out
    gainRange = ''
    gainRegime = ''
    gain = float(gain)
    if ampType == 'MLA2' or ampType == 'MLA3':
        if gain <= 23.5 and gain >= 15.0:
            gainRange = '15 < gain (dB) < 23.5'
            gainRegime = 'Typical'
        elif gain <= 28.0 and gain >= 11.0:
            gainRange = '11 < gain (dB) < 28'
            gainRegime = 'Extended'
        else:
            gainRegime = 'UNKNOWN'
            if gain < 11.0:
                gainRange = 'gain < 11 dB'
            else:
                gainRange = 'gain > 28 dB'
    elif ampType == 'SLA' or ampType == 'MLA' or ampType == 'CASLIM':
        if port == '6':
            if gain < 17.0 and gain > 9.0:
                gainRange = '9 < gain (dB) < 17'
                gainRegime = 'Typical'
            elif gain <= 22.0 and gain >= 6.0:
                gainRange = '6 < gain (dB) < 22'
                gainRegime = 'Extended'
            else:
                gainRegime = 'UNKNOWN'
                if gain < 6.0:
                    gainRange = 'gain < 6 dB'
                else:
                    gainRange = 'gain > 22 dB'
        elif gain < 20.0 and gain > 12.0:
            gainRange = '12 < gain (dB) < 20'
            gainRegime = 'Typical'
        elif gain <= 25.0 and gain >= 7.0:
            gainRange = '7 < gain (dB) < 25'
            gainRegime = 'Extended'
        else:
            gainRegime = 'UNKNOWN'
            if gain < 7.0:
                gainRange = 'gain < 7 dB'
            else:
                gainRange = 'gain > 25 dB'
    elif ampType == 'XLA' and xlamode == 'LOW':
        if gain > 5.0 and gain <= 15.0:
            gainRange = '5 < gain (dB) < 15'
            gainRegime = 'Typical'
        elif gain >= 15.0 and gain <= 19.0:
            gainRange = '15 < gain (dB) < 19'
            gainRegime = 'Extended'
        else:
            gainRegime = 'UNKNOWN'
            if gain < 5.0:
                gainRange = 'gain < 5 dB'
            else:
                gainRange = 'gain > 19 dB'
    elif ampType == 'XLA' and xlamode == 'HIGH':
        if gain > 15.0 and gain < 25.0:
            gainRange = '15 < gain (dB) < 25'
            gainRegime = 'Typical'
        elif gain >= 11.0 and gain <= 29.0:
            gainRange = '11 < gain (dB) < 29'
            gainRegime = 'Extended'
        else:
            gainRegime = 'UNKNOWN'
            if gain < 11.0:
                gainRange = 'gain < 11 dB'
            else:
                gainRange = 'gain > 29 dB'
    out = gainRange + ',' + gainRegime
    return out


def FISH(line, Parameter, delimiter):
    l1 = line.find(Parameter)
    if l1 > -1:
        lPend = l1 + len(Parameter)
        end = len(line)
        f1 = line[lPend:end]
        lDstart = f1.find(delimiter)
        if lDstart > -1:
            out = f1[0:lDstart]
        elif f1.find('"\r') > -1:
            s1 = len(f1) - 2
            out = f1[0:s1]
        else:
            out = f1[0:end]
    else:
        out = ''
    return out


def WARNING(title, message):
    if REPORT == 'YES':
        global root
        if root is None:
            try:
                if not _ensure_tkinter():
                    root = False
                    raise RuntimeError('tkinter unavailable')
                root = tk.Tk()
                root.withdraw()
            except Exception:
                root = False
        if root:
            try:
                tkMessageBox.showwarning(title, message)
            except Exception:
                print('[WARNING] %s: %s' % (title, message))
        else:
            print('[WARNING] %s: %s' % (title, message))
    return None


def TL1_2_File(TL1Command, fOut):
    f1 = TL1_In_Out(TL1Command)
    s1 = '\n>>>Begin: ' + TL1Command + '\n' + f1 + '\n>>>End: ' + TL1Command + '\n'
    fOut.write(s1)
    return s1


def TL1_In_Out(TL1Command):
    errMessage = 'OK'
    capturedText = ''
    beginTime = time.time()
    if METHOD == 'SSH':
        nbytes = 32768
        t = 0.0
        f1 = TL1Command.split(':')
        print(f'[TL1-SSH] Sending: {f1[0]} (full: {TL1Command[:80]}...)')
        F_DBG.write(f'[TL1-SSH] Sending: {f1[0]}\\n')
        chan_6500.send(TL1Command + '\r\n')
        poll_count = 0
        while t < TIMEOUT:
            try:
                if chan_6500.recv_ready():
                    chunk = _recv_text(chan_6500, nbytes)
                    if chunk:
                        poll_count += 1
                        capturedText += chunk
                        print(f'[TL1-SSH] Data chunk {poll_count} received ({len(chunk)} bytes, total {len(capturedText)} bytes)')
                        F_DBG.write(f'[TL1-SSH] Chunk {poll_count}: {len(chunk)} bytes (total {len(capturedText)}), tail={repr(capturedText[-60:])}\\n')
                        if capturedText.endswith(PROMPT) or (SSH_SHELL_PROMPT and SSH_SHELL_PROMPT in capturedText[-60:]):
                            print(f'[TL1-SSH] Prompt found! Response complete after {t:.1f}s')
                            F_DBG.write(f'[TL1-SSH] Prompt found after {t:.1f}s\\n')
                            break
                else:
                    time.sleep(0.2)
                    t = t + 0.25
            except Exception as err:
                errMessage = str(err)
                print(f'[TL1-SSH] Exception: {err}')
                F_DBG.write(f'[TL1-SSH] Exception: {err}\\n')
                F_DBG.write('\\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                F_MISS.write('\\n%s SSH Channel Error: %s' % (TL1Command, errMessage))
                break

        if not (capturedText.endswith(PROMPT) or (SSH_SHELL_PROMPT and SSH_SHELL_PROMPT in capturedText[-60:])):
            print(f'[TL1-SSH] Timeout/incomplete after {t:.1f}s. Got {len(capturedText)} bytes, {poll_count} chunks')
            F_DBG.write(f'[TL1-SSH] Timeout after {t:.1f}s, {len(capturedText)} bytes, {poll_count} chunks\\n')

        fOut = capturedText.replace('\\x08 \\x08', '')
        f1 = TL1Command.split(':')
        print ('SSH(%s:%s): %s collected' % (HOST, PORT, f1[0]))
    else:
        t = TIMEOUT
        f1 = TL1Command.split(':')
        print(f'[TL1-TELNET] Sending: {f1[0]} (full: {TL1Command[:80]}...)')
        F_DBG.write(f'[TL1-TELNET] Sending: {f1[0]}\\n')
        try:
            _telnet_write(telnet_6500, TL1Command)
            print(f'[TL1-TELNET] Waiting for response (up to 120s for prompt)...')
            F_DBG.write(f'[TL1-TELNET] Waiting for response...\\n')
            capturedText = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
            print(f'[TL1-TELNET] Response received ({len(capturedText)} bytes)')
            F_DBG.write(f'[TL1-TELNET] Response received ({len(capturedText)} bytes)\\n')
        except Exception as err:
            errMessage = str(err)
            print(f'[TL1-TELNET] Exception: {err}')
            F_DBG.write(f'[TL1-TELNET] Exception: {err}\\n')
            F_DBG.write('\\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))
            F_MISS.write('\\n%s TELNET Channel Error: %s' % (TL1Command, errMessage))

        fOut = capturedText.replace('\\x08 \\x08', '')
    f1 = time.time() - beginTime
    if not (fOut.endswith(PROMPT) or (SSH_SHELL_PROMPT and SSH_SHELL_PROMPT in fOut[-60:])) or f1 >= TIMEOUT:
        F_MISS.write('\nHost: %s \t TL1 Command: %s timed out after %d seconds' % (HOST, TL1Command, t))
        F_DBG.write('\nHost: %s \t TL1 Command: %s timed out after %d seconds' % (HOST, TL1Command, t))
    if capturedText.find('DENY') > -1:
        if capturedText.find('Not in Valid State') > -1:
            F_DBG.write('%s returned DENY & Status not in Valid State \n' % TL1Command)
        elif capturedText.find('Entity does Not Exist') > -1:
            F_DBG.write('%s returned DENY & Input Entity does Not Exist \n' % TL1Command)
        elif capturedText.find('Equipage, Not EQuipped') > -1:
            F_DBG.write('%s returned DENY & Equipage, Not EQuipped \n' % TL1Command)
        elif capturedText.find('Input, Invalid ACcess identifier') > -1:
            F_DBG.write('%s returned DENY & Input, Invalid ACcess identifier \n' % TL1Command)
        elif capturedText.find('Input, Command Not Valid') > -1:
            F_DBG.write('%s returned DENY & Input, Command Not Valid \n' % TL1Command)
        else:
            F_DBG.write('%s returned DENY \n' % TL1Command)
    return fOut


def TL1_Strip(TL1Command, F_in):
    start = '>>>Begin: ' + TL1Command
    stop = '>>>End: ' + TL1Command
    F_in.seek(0)
    Out = ''
    okIn = 'NO'
    for line in F_in:
        if okIn == 'NO':
            if line.find(start) > -1:
                okIn = 'YES'
        else:
            if line.find(stop) > -1:
                break
            if len(line) > 4:
                Out = Out + line

    f1 = Out.split('\n')
    return f1


def PARSE_RTRV_SYS(linesIn, CPL, F_Out, F_ERROR):
    dPEC = {}
    dTYPE = {}
    dSERIAL = {}
    dCLEI = {}
    IDb = 'Sheld ID'
    TYPEb = 'Shelf Type'
    PECb = 'Shelf PEC'
    SERIALb = 'Backplane Serial #'
    CLEIb = 'CLEI'
    ID = 'Shelf ID'
    NEMODE = 'Shelf mode'
    BITSMODE = 'External synchronization mode'
    SHELFSYNCH = 'Synch across the node'
    ALMHO = 'Alarm hold-off (sec)'
    VOARESETREQD = 'VOA reset required'
    DOCAUTODELLOS = 'Auto delete on LOS'
    AINSTIMEOUT = 'AINS timeout (HH-MM)'
    AUTOPROVFAC = 'Auto facility provisioning'
    FILLERMGMT = 'Filler missing Alarm'
    ADVEQPTMGMT = 'Advanced equipment mode'
    GCC0MODE = 'GCC0 autoprovisioning'
    GCC1MODE = 'GCC1 autoprovisioning'
    OSCMODE = 'OSC Mode'
    LASEROFFFARENDFAIL = 'Laser off far end fail'
    ALMCORR = 'Alarm correlation'
    FIBERLOSSDETECTION = 'High fiber loss alarm'
    CSCTRL = 'Coherent Select Control'
    NDPMODE = 'Auto Neighbor Discovery Protocol (NDP)'
    GRIDMODE = 'WSS Grid Mode'
    AUTOCONNVAL = 'Automatic Connection Validation'
    AUTOROUTEDEF = 'Automatic intra-domain Photonic CRS provisioning'
    CTRLMODEDFLT = 'Default Control Mode'
    DBDFLT = 'Default Filter-edge Spacing (GHz)'
    FBRLOSSMJTHDFLT = 'High Fiber Loss Major Threshold'
    FBRLOSSMNTHDFLT = 'High Fiber Loss Manor Threshold'
    STATE = 'Shelf Reconfig State'
    EQPTCURRENT = 'Shelf current capacity (A)'
    PROVCURRENT = 'Provisioned shelf current limit (A)'
    ESTIMATEDPOWER = 'Calculated shelf power (W)'
    MAXPOWER = 'Recommended total shelf power (W)'
    MONITORFAN = 'Shelf fan monitoring'
    ACTUALCOOLING = 'Shelf current cooling'
    for line in linesIn:
        if line.find('::CTYPE') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            IDb = IDb + ',' + shelf
            f1 = FISH(line, 'PEC=', ',')
            PECb = PECb + ',' + f1
            dPEC[shelf] = f1
            if line.find(',MDAT=') > -1:
                SERIALb = SERIALb + ',' + FISH(line, 'SER=', ',')
                dSERIAL[shelf] = FISH(line, 'SER=', ',')
            else:
                SERIALb = SERIALb + ',' + FISH(line, 'SER=', '"')
                dSERIAL[shelf] = FISH(line, 'SER=', '"')
            CLEIb = CLEIb + ',' + FISH(line, 'CLEI=', ',')
            dCLEI[shelf] = FISH(line, 'CLEI=', ',')
            TYPEb = TYPEb + ',' + FISH(line, 'CTYPE=\\"', '\\"')
            dTYPE[shelf] = FISH(line, 'CTYPE=\"', '\"')
            continue
        if line.find('ACTUALCOOLING=') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            ID = ID + ',' + shelf
            NEMODE = NEMODE + ',' + FISH(line, 'NEMODE=', ',')
            f1 = FISH(line, 'BITSMODE=', ',')
            BITSMODE = BITSMODE + ',' + f1
            dMSFT__SHELFID_PARAM[shelf + '+BITSMODE'] = f1
            SHELFSYNCH = SHELFSYNCH + ',' + FISH(line, 'SHELFSYNCH=', ',')
            ALMHO = ALMHO + ',' + FISH(line, 'ALMHO=', ',')
            VOARESETREQD = VOARESETREQD + ',' + FISH(line, 'VOARESETREQD=', ',')
            DOCAUTODELLOS = DOCAUTODELLOS + ',' + FISH(line, 'DOCAUTODELLOS=', ',')
            AINSTIMEOUT = AINSTIMEOUT + ',' + FISH(line, 'AINSTIMEOUT=', ',')
            ADVEQPTMGMT = ADVEQPTMGMT + ',' + FISH(line, 'ADVEQPTMGMT=', ',')
            LASEROFFFARENDFAIL = LASEROFFFARENDFAIL + ',' + FISH(line, 'LASEROFFFARENDFAIL=', ',')
            NDPMODE = NDPMODE + ',' + FISH(line, 'NDPMODE=', ',')
            GRIDMODE += ',' + FISH(line, 'GRIDMODE=', ',')
            AUTOCONNVAL += ',' + FISH(line, 'AUTOCONNVAL=', ',')
            AUTOROUTEDEF += ',' + FISH(line, 'AUTOROUTEDEF=', ',')
            f1 = FISH(line, 'STATE=', ',')
            STATE += ',' + f1
            if f1 != 'NORMAL':
                F_ERROR.write(',' + shelf + ',Shelf state is not Normal \n')
            f1 = FISH(line, 'CTRLMODEDFLT=', ',')
            if f1 == '50':
                CTRLMODEDFLT += ',Fixed ITU'
            else:
                CTRLMODEDFLT += ',Flex Grid Capable'
            if line.find('DBDFLT=') > -1:
                f1 = FISH(line, 'DBDFLT=', ',')
                DBDFLT += ',' + f1
                if f1 != '6.250':
                    F_ERROR.write(',' + shelf + ',Filter dead band is not 6.250 GHz \n')
            else:
                DBDFLT += ',' + 'not supported in this release'
            FBRLOSSMJTHDFLT += ',' + FISH(line, 'FBRLOSSMJTHDFLT=', ',')
            FBRLOSSMNTHDFLT += ',' + FISH(line, 'FBRLOSSMNTHDFLT=', ',')
            ACTUALCOOLING = ACTUALCOOLING + ',' + FISH(line, 'ACTUALCOOLING=', ',')
            f1 = FISH(line, 'AUTOPROVFAC=', ',')
            AUTOPROVFAC = AUTOPROVFAC + ',' + f1
            if f1 == 'OFF':
                F_ERROR.write(',' + shelf + ',The circuit pack autoprovisioning is disabled \n')
            f1 = FISH(line, 'FILLERMGMT=', ',')
            FILLERMGMT = FILLERMGMT + ',' + f1
            if f1 == 'DISABLED':
                F_ERROR.write(',' + shelf + ',The filler card missing detection is disabled \n')
            f1 = FISH(line, 'GCC0MODE=', ',')
            GCC0MODE = GCC0MODE + ',' + f1
            if f1 != 'DISABLED':
                F_ERROR.write(',' + shelf + ',The GCC0 auto circuit provisioning is not disabled \n')
            f1 = FISH(line, 'GCC1MODE=', ',')
            GCC1MODE = GCC1MODE + ',' + f1
            if f1 != 'DISABLED':
                F_ERROR.write(',' + shelf + ',The GCC1 circuit auto provisioning is not disabled \n')
            f1 = FISH(line, 'OSCMODE=', ',')
            OSCMODE = OSCMODE + ',' + f1
            if f1 != 'DISABLED':
                F_ERROR.write(',' + shelf + ',The OSC OSPF circuit auto provisioning is not disabled \n')
            f1 = FISH(line, 'ALMCORR=', ',')
            ALMCORR = ALMCORR + ',' + f1
            if f1 == 'OFF':
                F_ERROR.write(',' + shelf + ',The alarm correlation is disabled \n')
            f1 = FISH(line, 'FIBERLOSSDETECTION=', ',')
            FIBERLOSSDETECTION = FIBERLOSSDETECTION + ',' + f1
            if f1 == 'DISABLED':
                F_ERROR.write(',' + shelf + ',The High fiber loss detection is disabled \n')
            f1 = FISH(line, ',CSCTRL=', ',')
            CSCTRL = CSCTRL + ',' + f1
            if f1 == 'ON':
                F_ERROR.write(',' + shelf + ',Coherent Select Control is ON \n')
            f1 = FISH(line, 'MONITORFAN=', ',')
            MONITORFAN = MONITORFAN + ',' + f1
            if f1 == 'DISABLED':
                F_ERROR.write(',' + shelf + ',The fan monitoring is disabled \n')
            f1 = FISH(line, ',STATE=', ',')
            STATE = STATE + ',' + f1
            if f1 != 'NORMAL':
                F_ERROR.write(',' + shelf + ',The shelf state is not NORMAL \n')
            f1 = FISH(line, 'EQPTCURRENT=', ',')
            EQPTCURRENT = EQPTCURRENT + ',' + f1
            f2 = FISH(line, 'PROVCURRENT=', ',')
            PROVCURRENT = PROVCURRENT + ',' + f2
            FEEDS = f2
            try:
                PEC = dPEC[shelf]
                CPL = 'NO'
            except:
                CPL = 'YES'
                PEC = ''

            if CPL == 'NO':
                REC_MAXPOWER = ''
                DUAL_FEED = False
                if f2.find('_') > -1:
                    DUAL_FEED = True
                    s1 = f2.split('_')
                    pCard = s1[1]
                    if pCard.find('X') > -1:
                        s2 = pCard.split('X')
                        f4 = int(s2[0]) * int(s2[1])
                        s1 = 'Provisioned current (' + pCard + ' = '
                elif f2.find('X') > -1:
                    pCard = f2
                    f3 = f2.split('X')
                    f4 = int(f3[0]) * int(f3[1])
                    s1 = 'Provisioned current ('
                elif f2 == '':
                    F_ERROR.write(',' + shelf + ',No shelf current was provisioned\n')
                    f4 = 0
                    s1 = 'Provisioned current ('
                else:
                    pCard = f2
                    f4 = int(f2)
                    s1 = 'Provisioned current ('
                if int(f4) > int(f1):
                    F_ERROR.write(',' + shelf + ',' + s1 + str(f4) + ') is greater than the breaker card rating (' + f1 + ') \n')
                if line.find('ESTIMATEDPOWER=') > -1:
                    f1 = FISH(line, 'ESTIMATEDPOWER=', ',')
                    ESTIMATEDPOWER = ESTIMATEDPOWER + ',' + f1
                    REC_MAXPOWER = 'NA'
                    if DUAL_FEED == True:
                        if PEC == 'NTK503KA':
                            if FEEDS == '1X5_1X5':
                                REC_MAXPOWER = '500'
                            elif FEEDS == '1X5_2X5':
                                REC_MAXPOWER = '950'
                            elif FEEDS == '2X5_2X5':
                                REC_MAXPOWER = '1000'
                            elif FEEDS == '2X5_3X5':
                                REC_MAXPOWER = '1400'
                    elif line.find('ZONE2POWER=') > -1:
                        if pCard == '1X60':
                            REC_MAXPOWER = '2250 '
                        elif pCard == '1X80':
                            REC_MAXPOWER = '3000'
                        elif pCard == '1X100':
                            REC_MAXPOWER = '3750 '
                        elif pCard == '3X40':
                            REC_MAXPOWER = '4500'
                        elif pCard == '3X50':
                            REC_MAXPOWER = '6525'
                        elif pCard == '3X60':
                            REC_MAXPOWER = '6750'
                    elif pCard.find('X') < 0:
                        if PEC == 'NTK530MA' or PEC == 'NTK503LA':
                            if pCard == '5':
                                REC_MAXPOWER = '187'
                            elif pCard == '7':
                                REC_MAXPOWER = '262'
                            elif pCard == '10':
                                REC_MAXPOWER = '375'
                        elif PEC == 'NTK503NA':
                            REC_MAXPOWER = '334'
                        elif PEC == 'NTK503RA' or PEC == 'NTK503KA':
                            if pCard == '5':
                                REC_MAXPOWER = '187'
                            elif pCard == '10':
                                REC_MAXPOWER = '375'
                            elif pCard == '15':
                                REC_MAXPOWER = '562'
                            elif pCard == '20':
                                REC_MAXPOWER = '750'
                            elif pCard == '30':
                                REC_MAXPOWER = '1125'
                            elif pCard == '40':
                                REC_MAXPOWER = '1500'
                            elif pCard == '50':
                                REC_MAXPOWER = '1875'
                            elif pCard == '60':
                                REC_MAXPOWER = '2250'
                        elif PEC == 'NTK503PA':
                            if pCard == '5':
                                REC_MAXPOWER = '199'
                            elif pCard == '10':
                                REC_MAXPOWER = '375'
                            elif pCard == '15':
                                REC_MAXPOWER = '562'
                            elif pCard == '20':
                                REC_MAXPOWER = '750'
                            elif pCard == '30':
                                REC_MAXPOWER = '1125'
                    elif pCard == '2X40':
                        REC_MAXPOWER = '3000 '
                    elif pCard == '2X50':
                        REC_MAXPOWER = '3750'
                    if REC_MAXPOWER != 'NA':
                        if int(f1) > int(REC_MAXPOWER):
                            F_ERROR.write(',' + shelf + ',Captured shelf power (' + f1 + ' W) is greater than the maximum recommended shelf power ( ' + REC_MAXPOWER + ' W)\n')
            else:
                ESTIMATEDPOWER = ESTIMATEDPOWER + ','
                REC_MAXPOWER = ''
            MAXPOWER += ',' + REC_MAXPOWER + ','

    try:
        f1 = 'Shelf ID,' + shelf + '\nShelf Type,' + dTYPE[shelf] + '\nShelf PEC,' + dPEC[shelf] + '\nBackplane Serial #,' + dSERIAL[shelf] + '\nCLEI,' + dCLEI[shelf] + '\n\n'
    except:
        f1 = ''

    F_Out.write(IDb + '\n')
    F_Out.write(TYPEb + '\n')
    F_Out.write(PECb + '\n')
    F_Out.write(SERIALb + '\n')
    F_Out.write(CLEIb + '\n\n')
    F_Out.write(f1 + ID + '\n')
    F_Out.write(NEMODE + '\n')
    F_Out.write(BITSMODE + '\n')
    F_Out.write(SHELFSYNCH + '\n')
    F_Out.write(ALMHO + '\n')
    F_Out.write(VOARESETREQD + '\n')
    F_Out.write(DOCAUTODELLOS + '\n')
    F_Out.write(AINSTIMEOUT + '\n')
    F_Out.write(AUTOPROVFAC + '\n')
    F_Out.write(FILLERMGMT + '\n')
    F_Out.write(ADVEQPTMGMT + '\n')
    F_Out.write(GCC0MODE + '\n')
    F_Out.write(GCC1MODE + '\n')
    F_Out.write(OSCMODE + '\n')
    F_Out.write(ALMCORR + '\n')
    F_Out.write(CSCTRL + '\n')
    F_Out.write(NDPMODE + '\n')
    F_Out.write(GRIDMODE + '\n')
    F_Out.write(AUTOCONNVAL + '\n')
    F_Out.write(AUTOROUTEDEF + '\n')
    F_Out.write(CTRLMODEDFLT + '\n')
    F_Out.write(DBDFLT + '\n')
    F_Out.write(FIBERLOSSDETECTION + '\n')
    F_Out.write(FBRLOSSMJTHDFLT + '\n')
    F_Out.write(FBRLOSSMNTHDFLT + '\n')
    F_Out.write(STATE + '\n')
    F_Out.write(LASEROFFFARENDFAIL + '\n')
    F_Out.write(MONITORFAN + '\n')
    F_Out.write(ACTUALCOOLING + '\n')
    F_Out.write(EQPTCURRENT + '\n')
    F_Out.write(PROVCURRENT + '\n')
    F_Out.write(ESTIMATEDPOWER + '\n')
    F_Out.write(MAXPOWER + '\n\n')
    return None


def PARSE_RTRV_SHELF(linesIn, neType, F_Out, F_ERROR):
    SHELFID = 'Shelf ID'
    SITENAME = 'Site Name'
    SITEID = 'Site ID'
    PRIMARY = 'Primary Shelf'
    TIDC = 'TIDc'
    LOCATION = 'Shelf Location'
    ACTFLTRTIMER = 'Activate air filter replacement timer'
    FLTRTIMER = 'Air filter replacement interval (days)'
    DBSYNCSTATE = 'Database Synch State'
    SHELFSYNC = 'Primary/Member Shelf Synch'
    SUBNETNAME = 'Shelf Subnet Name'
    lSHELF_ID = []
    l_Snumber = []
    l_Sname = []
    PRI_NAME = ''
    for line in linesIn:
        if line.find(',PRIMARY=') > -1:
            f1 = line.split(':')
            f2 = f1[0]
            shelf = f2.replace('   "', '')
            lSHELF_ID.append(shelf)
            SHELFID = SHELFID + ',' + shelf
            f1 = FISH(line, 'SITEID=', ',')
            SITEID = SITEID + ',' + f1
            l_Snumber.append(f1)
            f1 = FISH(line, ',SITENAME=\\"', '\\"')
            SITENAME = SITENAME + ',' + f1
            if f1 == '':
                F_ERROR.write(',' + shelf + ',site name was not provisioned \n')
            l_Sname.append(f1)
            dMSFT__SHELFID_PARAM[shelf + '+SITENAME'] = f1
            PRIMARY = PRIMARY + ',' + FISH(line, 'PRIMARY=', ',')
            if line.find('PRIMARY=ENABLE') > -1:
                PRI_NAME = f1.replace(',', ';')
                dMSFT__SHELFID_PARAM['PRIMARY'] = shelf
            f1 = FISH(line, 'TIDC=', ',')
            dMSFT__SHELFID_PARAM[shelf + '+TIDC'] = f1
            TIDC = TIDC + ',' + f1
            if neType != 'AMP' and f1 != 'ENABLE':
                F_ERROR.write(',' + shelf + ',NE type = ' + neType + ' has TIDc disabled \n')
            f1 = FISH(line, ',LOCATION=\\"', '\\"')
            LOCATION = LOCATION + ',' + f1.replace(',', ';')
            if f1 == '':
                F_ERROR.write(',' + shelf + ',location was not provisioned \n')
            f1 = FISH(line, 'ACTFLTRTIMER=', ',')
            ACTFLTRTIMER = ACTFLTRTIMER + ',' + f1
            if f1 == 'DISABLE':
                F_ERROR.write(',' + shelf + ',The air filter replacement alarm is deactivated \n')
            FLTRTIMER = FLTRTIMER + ',' + FISH(line, ',FLTRTIMER=', ',')
            SUBNETNAME = SUBNETNAME + ',' + FISH(line, ',SUBNETNAME=\\"', '\\"')
            if line.find(',SHELFSYNC=') > -1:
                f1 = FISH(line, 'DBSYNCSTATE=', ',')
                DBSYNCSTATE = DBSYNCSTATE + ',' + f1
                if f1 == 'OOSYNC':
                    F_ERROR.write(',' + shelf + ',The database is not synchronized \n')
                f1 = FISH(line, 'SHELFSYNC=', '"')
                SHELFSYNC = SHELFSYNC + ',' + f1
            else:
                f1 = FISH(line, 'DBSYNCSTATE=', '"')
                DBSYNCSTATE = DBSYNCSTATE + ',' + f1
                if f1 == 'OOSYNC':
                    F_ERROR.write(',' + shelf + ',The database is not synchronized \n')
                SHELFSYNC = SHELFSYNC + ',' + '-'

    F_Out.write(SHELFID + '\n')
    F_Out.write(SITENAME + '\n')
    F_Out.write(SITEID + '\n')
    F_Out.write(PRIMARY + '\n')
    F_Out.write(TIDC + '\n')
    F_Out.write(LOCATION + '\n')
    F_Out.write(ACTFLTRTIMER + '\n')
    F_Out.write(FLTRTIMER + '\n')
    F_Out.write(DBSYNCSTATE + '\n')
    F_Out.write(SHELFSYNC + '\n')
    F_Out.write(SUBNETNAME + '\n\n')
    f1 = set(l_Snumber)
    if len(f1) > 1:
        F_ERROR.write(',TID,The TID has unequal Site ID \n')
    f1 = set(l_Sname)
    l1 = len(f1)
    if l1 > 1:
        F_ERROR.write(',TID,The TID has unequal Site Names \n')
        if PRI_NAME == '':
            PRI_NAME = l_Sname[0]
    elif l1 == 0:
        PRI_NAME = ''
    elif l1 == 1:
        PRI_NAME = l_Sname[0]
    dMSFT__SHELFID_PARAM['PRIMARY'] = PRI_NAME
    return (PRI_NAME, lSHELF_ID)


def PARSE_SHELF_ALL(linesIn, F_Out, F_ERROR):
    SHELFID = 'Shelf ID'
    COMMITTED = 'Shelf Committed Release'
    REST = 'Release details'
    LOCAL = 'Last Local Backup'
    REMOTE = 'Last Remote Backup'
    USB = 'Last USB Backup'
    GNE = 'Gateway NE'
    RNE = 'Remote NE'
    MINVER = 'Minimum TLS version'
    MAXVER = 'Maximum TLS version'
    SNMP = 'SNMP Agent'
    SNMP_VER = 'SNMP Version'
    SNMP_ALM = 'SNMP Alarm Masking'
    SNMP_TCA = 'SNMP TCA Reporting'
    SNMP_PROXY = 'SNMP Proxy'
    SNMP_PROXYTIME = 'SNMP Proxy Timeout'
    SNMP_PROXYENH = 'SNMP Enhanced Proxy'
    SNMP_TRAPIF = 'SNMP Trap Interface'
    SNMP_MIB = 'SNMP MIB'
    dShelfAll = {}
    dLOCAL = {}
    dREMOTE = {}
    dUSB = {}
    dREST = {}
    dMemberSW = {}
    dGNE = {}
    dRNE = {}
    dSSL_MAXVER = {}
    dSSL_MINVER = {}
    dSNMP = {}
    dSNMP_VER = {}
    dSNMP_ALM = {}
    dSNMP_TCA = {}
    dSNMP_PROXY = {}
    dSNMP_PROXYTIME = {}
    dSNMP_PROXYENH = {}
    dSNMP_TRAPIF = {}
    dSNMP_MIB = {}
    dTRAP__SI = {}
    rtrvRelease = 0
    memberN = 0
    MainRel = ''
    for line in linesIn:
        if line.find('RTRV-') > -1:
            continue
        line = line.strip()
        if line.find('CIENA,') > -1:
            f2 = line.split(',')
            MainRel = f2[3].replace('\\"', '')
            MainRel = MainRel.replace('"', '')
            continue
        if line.find('MEMBERFUNC') > -1:
            memberN = memberN + 1
            l1 = line.find(':')
            shelf = line[1:l1]
            f1 = FISH(line, 'SWVER=\\"', '\\"')
            dMemberSW[shelf] = f1
            if f1 != MainRel:
                F_ERROR.write(',' + shelf + ',Member shelf has different release than the primary\n')
            continue
        if line.find('ALLSW COMPLD') > -1:
            rtrvRelease = 1
            continue
        if line.find('SHELF-') > -1 and line.find('REL') > -1:
            l1 = line.find(':')
            shelf = line[1:l1]
            l1 += 1
            rest = line[l1:-1]
            rest = rest.replace(',', ';')
            if rtrvRelease == 0:
                SHELFID += ',' + shelf
                COMMITTED += ',' + rest
                if rest != MainRel:
                    F_ERROR.write(',' + shelf + ',is not at release ' + MainRel + '\n')
            elif rtrvRelease == 1:
                if rest.find('INCOMPLETE') > -1:
                    F_ERROR.write(',' + shelf + ',Incomplete SW release was detected\n')
                if rest.find('PARTIAL') > -1:
                    F_ERROR.write(',' + shelf + ',Partial SW release was detected\n')
                rest = rest.replace(',', '; ')
                try:
                    dShelfAll[shelf] = dShelfAll[shelf] + ' & ' + rest
                except KeyError:
                    dShelfAll[shelf] = rest

            continue
        if line.find('LASTBACKUP') > -1:
            l1 = line.find(':')
            shelf = line[1:l1]
            if line.find(':LOCAL') > -1:
                f2 = FISH(line, 'LASTBACKUP=', ',')
                if f2 == '':
                    f2 = 'Never'
                if line.find('BACKUPREQUIRED=T') > -1:
                    dLOCAL[shelf] = '"' + f2 + ':\r\n' + ' Backup Required' + '"'
                    F_ERROR.write(',' + shelf + ',Requires local (SP) backup \n')
                else:
                    dLOCAL[shelf] = f2
                continue
            if line.find(':REMOTE') > -1:
                f2 = FISH(line, 'LASTBACKUP=', ',')
                if f2 == '':
                    f2 = 'Never'
                if line.find('BACKUPREQUIRED=T') > -1:
                    dREMOTE[shelf] = '"' + f2 + ':\r\n' + ' Backup Required' + '"'
                    F_ERROR.write(',' + shelf + ',Requires remote (TOD server) backup \n')
                else:
                    dREMOTE[shelf] = f2
                continue
            if line.find('USB') > -1:
                f2 = FISH(line, 'LASTBACKUP=', ',')
                if f2 == '':
                    f2 = 'Never'
                if line.find('BACKUPREQUIRED=T') > -1:
                    dUSB[shelf] = '"' + f2 + ':\r\n' + 'Backup Required' + '"'
                else:
                    dUSB[shelf] = f2
                continue
        if line.find('GNE=') > -1 and line.find('RNE='):
            line = line.replace('"', '')
            line = line.replace('"\r', '')
            f1 = line.split(':')
            shelf = f1[0]
            if '::' in line:
                rest = f1[2].split(',')
            else:
                rest = f1[1].split(',')
            s1 = rest[0].replace('GNE=', '')
            s2 = rest[1].replace('RNE=', '')
            dGNE[shelf] = s1
            dRNE[shelf] = s2
            continue
        if line.find('MINVER=') > -1 and line.find(',MAXVER=') > -1:
            l1 = line.find(':')
            shelf = line[1:l1]
            line = line.replace('"', ',')
            f1 = FISH(line, 'MINVER=', ',')
            dMSFT__SHELFID_PARAM[shelf + '+SSL-MINVER'] = f1
            dSSL_MINVER[shelf] = f1
            dSSL_MAXVER[shelf] = FISH(line, 'MAXVER=', ',')
            continue
        if line.find(':SNMPAGENT=') > -1:
            line = line.replace('"', '')
            f1 = line.split(':')
            shelf = f1[0]
            f1 = FISH(line, 'SNMPAGENT=', ',')
            dSNMP[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMPAGENT'] = f1
            f1 = FISH(line, 'VERSION=', ',')
            dSNMP_VER[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-VERSION'] = f1
            f1 = FISH(line, 'ALMMASKING=', ',')
            dSNMP_ALM[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-ALMMASKING'] = f1
            f1 = FISH(line, ',PROXY=', ',')
            dSNMP_PROXY[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-PROXY'] = f1
            f1 = FISH(line, 'PROXYREQTIMEOUT=', ',')
            dSNMP_PROXYTIME[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-PROXYREQTIMEOUT'] = f1
            f1 = FISH(line, 'ENHANCEDPROXY=', ',')
            dSNMP_PROXYENH[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-ENHANCEDPROXY'] = f1
            f1 = FISH(line, 'TRAPIF=', ',')
            dSNMP_TRAPIF[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-TRAPIF'] = f1
            f1 = FISH(line, 'TCAREPORTING=', ',')
            dSNMP_TCA[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-TCAREPORTING'] = f1
            f1 = FISH(line, 'TRAPMIB=', ',')
            dSNMP_MIB[shelf] = f1
            dMSFT__SHELFID_PARAM[shelf + '+SNMP-TRAPMIB'] = f1
        if line.find('"DEST-') > -1 and line.find(',TRAPCONFIG=') > -1:
            l1 = line.find(':')
            f2 = line[4:l1]
            l1 = f2.split('-')
            line = line.replace('"', ',')
            f1 = 'IP Address = ' + FISH(line, 'IPADDR=', ',') + '\nUDP port = ' + FISH(line, 'UDPPORT=', ',') + '\nVersion = ' + FISH(line, 'VERSION=', ',')
            if line.find(',UID=') > -1:
                l2 = FISH(line, 'UID=', ',')
            else:
                l2 = FISH(line, 'UAP=', ',')
            f1 += '\nUAP/UID = ' + l2 + '\nTrap Config = ' + FISH(line, 'TRAPCONFIG=', ',')
            dTRAP__SI['SHELF-' + l1[1] + '+' + l1[2]] = '"' + f1 + '"'
            f1 = 'ID = ' + l1[2] + ' & ' + f1.replace('\n', ' & ')
            try:
                dMSFT__SHELFID_PARAM['SHELF-' + l1[1] + '+TRAP-DEST'].append(f1)
            except:
                dMSFT__SHELFID_PARAM['SHELF-' + l1[1] + '+TRAP-DEST'] = [f1]

    if memberN > 0:
        COMMITTED = 'Member Committed Release'
        s1 = dMemberSW.values()
        f1 = set(s1)
        releaseN = len(f1)
        if releaseN != 1:
            F_ERROR.write(',,' + 'Multiple SW releases were detected within the TIDc \n')
    shelves = ','
    for i in dShelfAll:
        shelves += i + ','
        f1 = dShelfAll[i]
        REST = REST + ',' + f1
        if f1.find(MainRel) < 0:
            F_ERROR.write(',' + i + ',The main release (' + MainRel + ') is missing \n')
        if f1.find('&') > -1:
            F_ERROR.write(',' + i + ',Has more than one SW releases loaded on the SP \n')
        if f1.find(MainRel + '; PARTIAL') > -1:
            F_ERROR.write(',' + i + ',The committed release ( ' + MainRel + ' ) is not fully loaded (i.e. it is PARTIAL) \n')
        if memberN > 0:
            COMMITTED += ',' + dMemberSW[i]
            try:
                LOCAL = LOCAL + ',' + dLOCAL[i]
                REMOTE = REMOTE + ',' + dREMOTE[i]
            except:
                F_ERROR.write(',' + i + ',Did not report Local/Remote backup status \n')
                LOCAL += ',Not Reported'
                REMOTE += ',Not Reported'

        try:
            SNMP = SNMP + ',' + dSNMP[i]
        except:
            SNMP = SNMP + ','

        try:
            SNMP_VER = SNMP_VER + ',' + dSNMP_VER[i]
        except:
            SNMP_VER = SNMP_VER + ','

        try:
            SNMP_ALM = SNMP_ALM + ',' + dSNMP_ALM[i]
        except:
            SNMP_ALM = SNMP_ALM + ','

        try:
            SNMP_TCA = SNMP_TCA + ',' + dSNMP_TCA[i]
        except:
            SNMP_TCA = SNMP_TCA + ','

        try:
            SNMP_PROXY = SNMP_PROXY + ',' + dSNMP_PROXY[i]
        except:
            SNMP_PROXY = SNMP_PROXY + ','

        try:
            SNMP_PROXYTIME = SNMP_PROXYTIME + ',' + dSNMP_PROXYTIME[i]
        except:
            SNMP_PROXYTIME = SNMP_PROXYTIME + ','

        try:
            SNMP_PROXYENH = SNMP_PROXYENH + ',' + dSNMP_PROXYENH[i]
        except:
            SNMP_PROXYENH = SNMP_PROXYENH + ','

        try:
            SNMP_TRAPIF = SNMP_TRAPIF + ',' + dSNMP_TRAPIF[i]
        except:
            SNMP_TRAPIF = SNMP_TRAPIF + ','

        try:
            SNMP_MIB = SNMP_MIB + ',' + dSNMP_MIB[i]
        except:
            SNMP_MIB = SNMP_MIB + ','

        try:
            USB = USB + ',' + dUSB[i]
        except KeyError:
            USB = USB + ','

        try:
            MAXVER += ',' + dSSL_MAXVER[i]
        except:
            MAXVER = MAXVER + ',-'

        try:
            MINVER += ',' + dSSL_MINVER[i]
        except:
            MINVER = MINVER + ',-'

        try:
            GNE = GNE + ',' + dGNE[i]
            RNE = RNE + ',' + dRNE[i]
        except KeyError:
            GNE = GNE + ',-'
            RNE = RNE + ',-'

    F_Out.write('\nNE SW RELEASE = ' + MainRel + '\n')
    F_Out.write(SHELFID + '\n')
    F_Out.write(COMMITTED + '\n')
    F_Out.write('\nShelf ID' + shelves + '\n')
    F_Out.write(REST + '\n')
    F_Out.write('\nShelf ID' + shelves + '\n')
    F_Out.write(LOCAL + '\n')
    F_Out.write(REMOTE + '\n')
    F_Out.write(USB + '\n')
    F_Out.write('\nTL1 Gateway \n')
    F_Out.write(GNE + '\n')
    F_Out.write(RNE + '\n')
    F_Out.write('\nSecure Socket Layer (SSL) \n')
    F_Out.write(MINVER + '\n')
    F_Out.write(MAXVER + '\n')
    F_Out.write('\nSNMP Agent Provisioning\n')
    F_Out.write('\nShelf ID' + shelves + '\n')
    F_Out.write(SNMP + '\n')
    F_Out.write(SNMP_VER + '\n')
    F_Out.write(SNMP_ALM + '\n')
    F_Out.write(SNMP_PROXY + '\n')
    F_Out.write(SNMP_PROXYTIME + '\n')
    F_Out.write(SNMP_PROXYENH + '\n')
    F_Out.write(SNMP_TRAPIF + '\n')
    F_Out.write(SNMP_TCA + '\n')
    F_Out.write(SNMP_MIB + '\n')
    i = 0
    while i <= 7:
        i += 1
        line = 'SNMP_TRAP_' + str(i) + ','
        for j in dShelfAll:
            line += dTRAP__SI[str(j) + '+' + str(i)] + ','

        F_Out.write(line + '\n')

    return None


def PARSE_TOD(linesIn, F_Out, F_ERROR):
    SERADDRESS = ''
    TZONE = ''
    TOD = ''
    CLLI = ''
    SHELFTYPE = ''
    NETYPE = ''
    REL = ''
    NEXTSYNC = ''
    LASTSYNC = ''
    DETECTEDOFFSET = ''
    blankAddress = 1
    nextCLLI = 'NO'
    for line in linesIn:
        line = line.replace('"\r', '')
        if line.find('M  QCLLI COMPLD') > -1:
            nextCLLI = 'YES'
            continue
        if nextCLLI == 'YES' and len(line) > 5:
            f1 = line.strip()
            CLLI = 'CLLI = ' + f1.strip('"')
            nextCLLI = 'NO'
            continue
        if line.find('CIENA,\\"') > -1:
            f1 = line.split(',')
            s1 = f1[1]
            SHELFTYPE = 'SHELF TYPE = ' + s1.replace('\\"', '')
            NETYPE = 'NE TYPE = ' + f1[2]
            s1 = f1[3]
            REL = 'NE SW RELEASE = ' + s1.replace('\\"', '')
            continue
        if line.find('"TMZONE=') > -1:
            l1 = line.find('=')
            l2 = len(line)
            TZONE = 'Time Zone = ' + line[l1 + 1:l2]
            continue
        if line.find(' "SHELF-') > -1:
            SHELF = line[4:]
            continue
        if line.find(' "TOD=') > -1:
            l1 = line.find('\\"') + 2
            l2 = line.rfind('\\"')
            TOD = 'TOD = ' + line[l1:l2]
            continue
        if line.find('"SERADDRESS') > -1:
            line = line.replace(',', '; ')
            f1 = line.split('=')
            s1 = f1[0].replace('\\"', '')
            s2 = f1[1].replace('\\"', '')
            SERADDRESS = SERADDRESS + s1[4:] + ' = ' + s2 + '\n'
            if line.find('SERADDRESS1=') > -1:
                l1 = s2.find(';')
                dMSFT__SHELFID_PARAM[SHELF + '+TOD-SERADDRESS1'] = s2[:l1]
            continue
        if line.find('"LASTSYNC=') > -1:
            l1 = line.find('\\"') + 2
            l2 = line.rfind('\\"')
            s1 = line[l1:l2]
            if s1 != '':
                blankAddress = 0
            LASTSYNC = 'LASTSYNC = ' + s1
            continue
        if line.find('"NEXTSYNC=') > -1:
            l1 = line.find('\\"') + 2
            l2 = line.rfind('\\"')
            NEXTSYNC = 'NEXTSYNC = ' + line[l1:l2]
            continue
        if line.find('DETECTEDOFFSET=\\') > -1:
            l1 = line.find('\\"') + 2
            l2 = line.rfind('\\"')
            DETECTEDOFFSET = 'DETECTEDOFFSET = ' + line[l1:l2]
            continue

    if NEXTSYNC == 'NO ACTIVE SERVER':
        F_ERROR.write(',The TOD server is inactive \n')
    if blankAddress == 1:
        F_ERROR.write(',No TOD Server IP Address was provisioned \n')
    F_Out.write(CLLI + '\n')
    F_Out.write(SHELFTYPE + '\n')
    F_Out.write(NETYPE + '\n')
    F_Out.write(REL + '\n\n')
    F_Out.write(TZONE + '\n')
    F_Out.write(TOD + '\n')
    F_Out.write(LASTSYNC + '\n')
    F_Out.write(NEXTSYNC + '\n')
    F_Out.write(DETECTEDOFFSET + '\n')
    F_Out.write(SERADDRESS + '\n')
    return NETYPE


def PARSE_RTRV_OTS___(line, F_ERROR):
    f1 = line.split(':')
    f2 = f1[0]
    AID = f2.replace('   "', '')
    f1 = AID.find('-') + 1
    f2 = AID.rfind('-')
    SHELF = 'SHELF-' + AID[f1:f2]
    firstMember = -1
    if line.find('OSID') > -1:
        OSID = FISH(line, 'OSID=\\"', '\\"')
        if line.find('OSC=') > -1:
            firstMember = line.find('OSC=')
        elif line.find('LINEOUT=') > -1:
            firstMember = line.find('LINEOUT=')
        elif line.find('LIM=') > -1:
            firstMember = line.find('LIM=')
    else:
        OSID = ''
        if line.find('LIM=') > -1:
            firstMember = line.find('LIM')
        elif line.find('SMD=') > -1:
            firstMember = line.find('SMD')
        else:
            if line.find('SUBTYPE=PASSIVE') == -1:
                l1 = line.find(',UNEXPTLOSSTHRES=') + 17
                l2 = len(line)
                f1 = line[l1:l2]
            else:
                l1 = line.find(',BWCAL=') + 7
                l2 = len(line)
                f1 = line[l1:l2]
            l1 = f1.find(',')
            l2 = len(f1)
            f2 = f1[l1:l2]
            l1 = f2.find('=')
            FirstMember = f2[0:l1]
    if line.find('AMPMATE') > -1:
        l1 = line.find(',AMPMATE=')
    elif line.find('COMBINEDOPMPOWER') > -1:
        l1 = line.find(',COMBINEDOPMPOWER=')
    else:
        l1 = line.find(',ASSOCIATEDOTS=')
    if firstMember > 0:
        f1 = line[firstMember:l1]
        OTSMEMBERS = f1.replace(',', '+')
    else:
        OTSMEMBERS = 'NONE'
    if line.find('ISS=ISS'):
        ISS = FISH(line, 'ISS=', ',')
        if ISS != '':
            OTSMEMBERS = OTSMEMBERS + ' (+ISS=' + ISS + ')'
    l1 = line.rfind('"')
    line = line[:l1] + ','
    CFGTYPE = FISH(line, 'CFGTYPE=', ',')
    SUBTYPE = FISH(line, 'SUBTYPE=', ',')
    TXPATH = FISH(line, 'TXPATH=', ',')
    RXPATH = FISH(line, 'RXPATH=', ',')
    DOCIND = FISH(line, 'DOCIND=', ',')
    DOCIND = FISH(line, 'DOCIND=', ',')
    PEPCCLAMP = FISH(line, 'PEPCCLAMP=', ',')
    SLOTCFGMODE = FISH(line, 'SLOTCFGMODE=', ',')
    AMPMATE = FISH(line, 'AMPMATE=', ',')
    ASSOCIATEDOTS = FISH(line, 'ASSOCIATEDOTS=\\"', '\\"')
    CPS = FISH(line, 'CPS=', ',')
    AUTOROUTE = FISH(line, 'AUTOROUTE=', ',')
    SEQCHCUPDATES = FISH(line, 'SEQCHCUPDATES=', ',')
    GBWIDTH = FISH(line, 'GBWIDTH=', ',')
    OSCREQUIRED = FISH(line, 'OSCREQUIRED=', ',')
    ENHANCEDTOPOLOGY = FISH(line, 'ENHANCEDTOPOLOGY=', ',')
    BUNFACTOR = FISH(line, 'BUNFACTOR=', ',')
    DRADSCMTOPOLOGY = FISH(line, 'DRADSCMTOPOLOGY=', ',')
    AUTOGB = FISH(line, 'AUTOGB=', ',')
    CSIND = FISH(line, 'CSIND=', ',')
    PROVCTRLMODE = FISH(line, 'PROVCTRLMODE=', ',')
    if PROVCTRLMODE == '50':
        PROVCTRLMODE = 'Fixed ITU 50 GHz'
    elif PROVCTRLMODE == '12.5':
        PROVCTRLMODE = 'Flex Grid capable'
    else:
        PROVCTRLMODE = 'Undefined'
    ACTCTRLMODE = FISH(line, 'ACTCTRLMODE=', ',')
    if ACTCTRLMODE == '50':
        ACTCTRLMODE = 'Fixed ITU 50 GHz'
    elif ACTCTRLMODE == '12.5':
        ACTCTRLMODE = 'Flex Grid capable'
    else:
        ACTCTRLMODE = 'Undefined'
    OTSOut = SHELF + ',' + CFGTYPE + ',' + SUBTYPE + ',' + AID + ',' + OSID + ',' + TXPATH + ',' + RXPATH + ',' + OTSMEMBERS + ',' + DOCIND + ',' + SLOTCFGMODE + ',' + AMPMATE + ',' + ASSOCIATEDOTS + ',' + PROVCTRLMODE + ',' + ACTCTRLMODE + ',' + CPS + ',' + AUTOROUTE + ',' + SEQCHCUPDATES + ',' + GBWIDTH + ',' + AUTOGB + ',' + OSCREQUIRED + ',' + CSIND + ',' + PEPCCLAMP + ',' + ENHANCEDTOPOLOGY + ',' + BUNFACTOR + ',' + DRADSCMTOPOLOGY
    if CFGTYPE == 'AMP' and AMPMATE == '':
        F_ERROR.write(',' + AID + ',Associated OTS must be provisioned for line amplifiers\n')
    if ENHANCEDTOPOLOGY != 'ENABLE':
        F_ERROR.write(',' + AID + ',Enchanced Topology not enabled\n')
    if CSIND == 'Y':
        F_ERROR.write(',' + AID + ',has Coherent Select enabled\n')
    return OTSOut


def LOGIN_TELNET():
    global CPL
    global telnet_6500
    wasConnected = 'YES'
    LoginCommand = 'ACT-USER::"' + USER + '":LOG::"' + PASS + '":;'
    F_DBG.write('\nHOST = %s \nUsername = %s \nMethod = %s \nPort = %s \nComment = %s\n' % (HOST,
    USER,
    METHOD,
    PORT,
    COMMENT))
    f1 = 'Trying to login to ' + HOST + ':' + PORT + '...'
    try:
        print (f1)
        # F004: TL1 prompt only on Telnet — bypass policy.
        telnet_6500 = _Telnet(HOST, PORT, TIMEOUT,
                              bypass_policy=True, purpose="tl1-6500")
    except:
        wasConnected = 'No telnet for this IP'
        F_DBG.write('%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (wasConnected, HOST))
        return wasConnected

    telnet_6500.set_debuglevel(10)
    captured_text = _telnet_read_until(telnet_6500, 'parameter/keyword', TIMEOUT)
    print (captured_text)
    if captured_text.find('6500') > -1:
        CPL = 'NO'
    elif captured_text.find('ommon Photonic') > -1:
        CPL = 'YES'
    else:
        if captured_text.find('ould not open connection to the host, on port') > -1:
            wasConnected = 'Host responded with \n' + captured_text
            F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
            return wasConnected
        wasConnected = 'Host responded with \n' + captured_text
        F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
        telnet_6500.close()
        return wasConnected
    if PASS == '?':
        LoginCommand = 'ACT-USER::"' + USER + '":CHRES:::DOMAIN=CHALLENGE;'
        captured_text, f1 = LOGIN_CHALLENGE_RESPONSE(LoginCommand)
        if captured_text.find('LOG DENY') > -1 or f1 == 1:
            wasConnected = '\nChallenge/response login failed'
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
            telnet_6500.close()
            return 1
    else:
        _telnet_write(telnet_6500, LoginCommand)
        captured_text = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
        print (captured_text)
        if captured_text.find('M  LOG DENY') > -1:
            telnet_6500.close()
            if captured_text.find('Privilege, Login Not Active') > 0:
                wasConnected = 'Privilege, Login Not Active (possible incorrect username/password)'
                WARNING('Login Issue', wasConnected)
                F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
                return wasConnected
            elif captured_text.find('Login from Primary Shelf') > -1:
                wasConnected = 'This is secondary shelf'
                WARNING('Login Issue', wasConnected)
                F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
                return wasConnected
            else:
                wasConnected = 'LOG DENY (possible incorrect username/password)'
                WARNING('Login Issue', wasConnected)
                F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
                telnet_6500.close()
                return wasConnected
        elif len(captured_text) < 4:
            wasConnected = 'Shelf did not not respond'
            WARNING('Login Issue', wasConnected)
            F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
            return wasConnected
    f1 = 'INH-MSG-ALL::ALL:Q0;'
    _telnet_write(telnet_6500, f1)
    captured_text = _telnet_read_until(telnet_6500, PROMPT, TIMEOUT)
    if captured_text.find('Privilege, Login Not Active') > -1:
        print (captured_text)
        wasConnected = 'Shelf responded: Privilege, Login Not Active'
        WARNING('Login Issue', wasConnected)
        F_DBG.write('%s\n%s \n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% Premature Ending of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%\n\n' % (captured_text, wasConnected, HOST))
        telnet_6500.close()
        return wasConnected
    return wasConnected


def COLLECT_DATA(mcemon, WindowsHost):
    global CPL
    sErr = ''
    rSHELF_ID = []
    rID = []
    NumberOfShelves = 0
    TID = '?'
    CPL = '?'
    f1 = TL1_In_Out('RTRV-SHELF::ALL:QQQ;')
    lines = f1.split('\n')
    for i in lines:
        if len(i) < 4:
            continue
        F_DBG.write(i + '\n')
        f1 = re.search('\\d\\d-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d', i)
        if f1:
            f1 = i.lstrip(' ')
            tokens = f1.split(' ')
            TID = tokens[0]
        if i.find('PRIMARY=') > -1:
            NumberOfShelves = NumberOfShelves + 1
            tokens = i.split('::')
            f1 = tokens[0].lstrip('   "')
            rSHELF_ID.append(f1)
            f2 = f1.split('-')
            rID.append(f2[1])
            if i.find('PRIMARY=ENABLE') > -1:
                PRIMARY_SHELF_ID = f2[1]

    if len(rID) == 1:
        PRIMARY_SHELF_ID = rID[0]
    f1 = set(rSHELF_ID)
    rSHELF_ID = list(f1)
    nShelves = str(len(rSHELF_ID))
    f1 = set(rID)
    rID = list(f1)
    if TID == '?':
        sErr = 'Could not extract TID'
        WARNING('TL1 Capturing Issue', sErr)
        return sErr
    if NumberOfShelves == 0:
        sErr = 'Number of shelves = 0'
        WARNING('TL1 Capturing Issue', sErr)
        return sErr
    f1 = TL1_In_Out('RTRV-NETYPE:::QTYPE;')
    if f1.find('6500') > -1:
        CPL = 'NO'
    elif f1.find('ommon Photonic Layer') > -1:
        CPL = 'YES'
    else:
        title = 'TL1 Capturing Issue'
        WARNING(title, 'Could not identify NE type (CPL/6500)')
    F_HOST = open(WindowsHost + '.txt', 'w')
    TimeStamp = strftime('%Y-%m-%d @ %H:%M:%S')
    F_DBG.write('\nNumber of shelves = %s\n\n' % NumberOfShelves)
    F_HOST.write('%s\nScript version = %s \n \n' % (TimeStamp, SCRIPT_VERSION))
    if mcemon == 'ON':
        F_HOST.write('\n\n ***MCEMON is ON***\n\n')
    else:
        F_HOST.write('\n\n ***MCEMON is OFF***\n\n')
    TL1_2_File('RTRV-TMZONE:::TOD0;', F_HOST)
    TL1_2_File('RTRV-TOD-MODE:::TOD1;', F_HOST)
    TL1_2_File('RTRV-TOD-SER:::TOD2;', F_HOST)
    TL1_2_File('RTRV-CLLI:::QCLLI;', F_HOST)
    TL1_2_File('RTRV-NETYPE:::QTYPE;', F_HOST)
    TL1_2_File('RTRV-SHELF::ALL:QSHELF;', F_HOST)
    TL1_2_File('RTRV-SYS::ALL:QSYS;', F_HOST)
    TL1_2_File('RTRV-MEMBER::ALL:QMEMBE;', F_HOST)
    TL1_2_File('RTRV-TL1GW:::QGW;', F_HOST)
    TL1_2_File('RTRV-SSL-SRVR::ALL:QSSL;', F_HOST)
    TL1_2_File('RTRV-SW-VER::ALL:SWVER;', F_HOST)
    TL1_2_File('RTRV-RELEASE:::ALLSW;', F_HOST)
    F_HOST.write('>>>Begin: RTRV-PROV-STATE++\n')
    for i in rSHELF_ID:
        s1 = 'RTRV-PROV-STATE::' + i + ':QBACK:::;'
        TL1_2_File(s1, F_HOST)

    F_HOST.write('>>>End: RTRV-PROV-STATE++\n')
    F_HOST.write('>>>Begin: RTRV-DCN00++\n')
    TL1_2_File('RTRV-GNE:::QGNE;', F_HOST)
    TL1_2_File('RTRV-OSPF-ROUTER:::QROUTE;', F_HOST)
    TL1_2_File('RTRV-IP:::QIP;', F_HOST)
    for i in rID:
        s1 = 'RTRV-IPADDRESS::IPADDR-' + i + '-ALL:QIPV6;'
        TL1_2_File(s1, F_HOST)

    TL1_2_File('RTRV-LAN:::QLAN;', F_HOST)
    TL1_2_File('RTRV-WSC:::QWSC;', F_HOST)
    TL1_2_File('RTRV-OSPF-CIRCUIT:::QCIRC;', F_HOST)
    TL1_2_File('RTRV-DBRS:::QDBRS;', F_HOST)
    TL1_2_File('RTRV-LLSDCC:::QDCC;', F_HOST)
    TL1_2_File('RTRV-NDP:::QNDP;', F_HOST)
    TL1_2_File('RTRV-TELNET:::QTEL;', F_HOST)
    TL1_2_File('RTRV-SSH-PUBKEY::ALL:QKEY;', F_HOST)
    TL1_2_File('RTRV-SSH::ALL:QSSH;', F_HOST)
    TL1_2_File('RTRV-HTTP:::QHTTP;', F_HOST)
    TL1_2_File('RTRV-FTP:::QFTP;', F_HOST)
    F_HOST.write('>>>End: RTRV-DCN00++\n')
    F_HOST.write('>>>Begin: RTRV-DCN01++\n')
    TL1_2_File('RTRV-IPSTATICRT:::QSTAT;', F_HOST)
    TL1_2_File('RTRV-OSPF-RDENTRY:::QENTRY;', F_HOST)
    TL1_2_File('RTRV-STATICROUTE:::STAT6;', F_HOST)
    F_HOST.write('>>>End: RTRV-DCN01++\n')
    F_HOST.write('>>>Begin: RTRV-IPxRTG-TBL++\n')
    TL1_2_File('RTRV-IPRTG-TBL:::RIPV4L;', F_HOST)
    for i in rID:
        TL1_2_File('RTRV-IP6RTG-TBL::SHELF-' + i + ':RIPV6;', F_HOST)

    F_HOST.write('>>>End: RTRV-IPxRTG-TBL++\n')
    TL1_2_File('RTRV-IISIS-ROUTER:::QTL;', F_HOST)
    TL1_2_File('RTRV-IISIS-CIRCUIT:::QTL;', F_HOST)
    TL1_2_File('RTRV-IISIS-RDENTRY:::QTL;', F_HOST)
    TL1_2_File('RTRV-RTG-TBL:::QIS;', F_HOST)
    TL1_2_File('RTRV-RTG-INFO:::QIS;', F_HOST)
    TL1_2_File('RTRV-TIDMAP:::QTL;', F_HOST)
    TL1_2_File('RTRV-IPNAT:::QTL;', F_HOST)
    TL1_2_File('RTRV-NAT:::QTL;', F_HOST)
    TL1_2_File('RTRV-RP-IPNAT:::QTL;', F_HOST)
    TL1_2_File('RTRV-ARP-TBL:::QTL;', F_HOST)
    TL1_2_File('RTRV-ARP-PROXY:::QTL;', F_HOST)
    TL1_2_File('RTRV-NODES:::QTL;', F_HOST)
    TL1_2_File('RTRV-NE-LIST:::QTL;', F_HOST)
    TL1_2_File('RTRV-TL1GW:::QTL;', F_HOST)
    TL1_2_File('RTRV-OTS::ALL:QOTS;', F_HOST)
    F_HOST.write('>>>Begin: RTRV-EQPT++\n')
    TL1_2_File('RTRV-AUTOEQUIP::ALL:QAUTO;', F_HOST)
    TL1_2_File('RTRV-EQPTMODE::ALL:QMODE;', F_HOST)
    s1 = TL1_2_File('RTRV-EQPT::ALL:QEQUIP;', F_HOST)
    F_HOST.write('>>>End: RTRV-EQPT++\n')
    f1 = s1.split('\n')
    l_DSM_PM = EXTRACT_DSM(f1)
    F_HOST.write('>>>Begin: RTRV-INVENTORY++\n')
    list1 = ''
    for i in rSHELF_ID:
        f1 = TL1_2_File('RTRV-BACKPLANE::' + i + ':QBAC;', F_HOST)
        list1 += f1
        TL1_2_File('RTRV-INVENTORY::' + i + '-ALL:QINV;', F_HOST)
        l1 = 1 + i.find('-')
        l2 = len(i)
        j = i[l1:l2]
        TL1_2_File('RTRV-INVENTORY-FAN::FAN-' + j + '-ALL:QFAN;', F_HOST)
        TL1_2_File('RTRV-INVENTORY-IO::SLOT-' + j + '-ALL:QIO;', F_HOST)

    F_HOST.write('>>>End: RTRV-INVENTORY++\n')
    F_HOST.write('>>>Begin: RTRV-BACKPLANE++\n')
    F_HOST.write(list1)
    F_HOST.write('>>>End: RTRV-BACKPLANE++\n')
    F_HOST.write('\n>>>Begin: RTRV-AMP++\n')
    TL1_2_File('RTRV-PM-AMP::ALL:QPMAMP::,,,,1-UNT,ALL,ALL;', F_HOST)
    TL1_2_File('RTRV-PM-AMP::ALL:QORLB::ORL-OTS,,,,BASLN,ALL,ALL;', F_HOST)
    TL1_2_File('RTRV-PM-OPTMON::ALL:QOPRPM::OPR-OTS,,ALL,ALL,1-UNT,,,;', F_HOST)
    TL1_2_File('RTRV-AMP::ALL:AMPALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-AMP++\n')
    F_HOST.write('\n>>>Begin: RTRV-RAMAN++\n')
    TL1_2_File('RTRV-PM-RAMAN::ALL:QRAMAN::,,,,1-UNT,ALL,ALL;', F_HOST)
    TL1_2_File('RTRV-PM-RAMAN::ALL:BASE::ALL,,,,BASLN,ALL,ALL,;', F_HOST)
    TL1_2_File('RTRV-RAMAN::ALL:QRAMAN;', F_HOST)
    F_HOST.write('\n>>>End: RTRV-RAMAN++\n')
    F_HOST.write('>>>Begin: RTRV-TELEMETRY&OTDRCFG++\n')
    f1 = TL1_2_File('RTRV-TELEMETRY::ALL:QTELE;', F_HOST)
    f1 += TL1_2_File('RTRV-OTDRCFG::ALL:QOCFG;', F_HOST)
    F_HOST.write('>>>End: RTRV-TELEMETRY&OTDRCFG++\n')
    if f1.find('FIBERTYPE') > -1:
        if f1.find('DISTANCEOFFICE') > -1:
            l1 = 'DISTANCEOFFICE'
        else:
            l1 = 'SPANLOSS'
        F_HOST.write('>>>Begin: RTRV-OTDR-EVENTS++\n')
        lines = f1.split('\n')
        for i in lines:
            if i.find(l1) > -1:
                f1 = i.split(':')
                AID = f1[0].replace('   "', '')
                TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=BSLN,TRACETYPE=LONG;', F_HOST)
                TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=CURRENT,TRACETYPE=LONG;', F_HOST)
                TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=BSLN,TRACETYPE=SHORT;', F_HOST)
                TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=CURRENT,TRACETYPE=SHORT;', F_HOST)
                if l1 == 'DISTANCEOFFICE':
                    TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=BSLN,TRACETYPE=OFFICE;', F_HOST)
                    TL1_2_File('RTRV-OTDR-EVENTS::' + AID + ':QTEVE:::TRACETAG=CURRENT,TRACETYPE=OFFICE;', F_HOST)

        F_HOST.write('>>>End: RTRV-OTDR-EVENTS++\n')
    TL1_2_File('RTRV-DISP::ALL:DISPQ;', F_HOST)
    F_HOST.write('\n>>>Begin: RTRV-OSC++\n')
    f2 = TL1_In_Out('RTRV-OSC::ALL:QOSC;')
    s1 = f2.split('\n')
    for i in s1:
        if i.find('RXPATHLOSS') > -1:
            f1 = i.split(':')
            AID = f1[0].replace('   "', '')
            f1 = TL1_In_Out('RTRV-RTD::' + AID + ':QRTD;')

    TL1_2_File('RTRV-PM-OSC::ALL:OSCPM::DMAVG-L&OPR-OCH,0-UP,,,1-UNT,,,0:,;', F_HOST)
    F_HOST.write('\n' + f2 + '\n')
    F_HOST.write('\n>>>End: RTRV-OSC++\n')
    F_HOST.write('>>>Begin: RTRV-VOA++\n')
    TL1_2_File('RTRV-PM-VOA::ALL:QPMVOA::OPOUT-OTS,,ALL,ALL,1-UNT,,,;', F_HOST)
    TL1_2_File('RTRV-VOA::ALL:QVOA;', F_HOST)
    F_HOST.write('>>>End: RTRV-VOA++\n')
    for i in rID:
        TL1_2_File('RTRV-CASC-GRP::SHELF-' + i + '-ALL:QCASC;', F_HOST)

    TL1_2_File('RTRV-ADJ-LINE::ALL:ADJLIN;', F_HOST)
    TL1_2_File('RTRV-ADJ-FIBER::ALL:ADJFIB;', F_HOST)
    TL1_2_File('RTRV-ADJ::ALL:ADJALL;', F_HOST)
    F_HOST.write('>>>Begin: RTRV-ADJ-Tx&Rx++\n')
    TL1_2_File('RTRV-ADJ-TX::ALL:QTX;', F_HOST)
    TL1_2_File('RTRV-ADJ-RX::ALL:QRX;', F_HOST)
    F_HOST.write('>>>End: RTRV-ADJ-Tx&Rx++\n')
    F_HOST.write('>>>Begin: RTRV-DOC++\n')
    TL1_2_File('RTRV-DOC::ALL:QDOC;', F_HOST)
    TL1_2_File('RTRV-DOC-CLASS::ALL:QDP;', F_HOST)
    F_HOST.write('>>>End: RTRV-DOC++\n')
    F_HOST.write('>>>Begin: RTRV-DOC-CH++\n')
    TL1_2_File('RTRV-CRS-OCH::ALL,ALL:QOCH;', F_HOST)
    TL1_2_File('RTRV-CRS-NMC::ALL,ALL:QNMC;', F_HOST)
    TL1_2_File('RTRV-MCTTP::ALL:QMCTTP;', F_HOST)
    TL1_2_File('RTRV-DOC-CH::ALL:QCHDOC:::;', F_HOST)
    F_HOST.write('>>>End: RTRV-DOC-CH++\n')
    F_HOST.write('>>>Begin: RTRV-OSRP++\n')
    TL1_2_File('RTRV-OSRP::ALL:QOSRP;', F_HOST)
    TL1_2_File('RTRV-OSRP-NODE::ALL:QNODE;', F_HOST)
    f1 = TL1_In_Out('RTRV-OSRP-RMTNODES::ALL:QNDES;')
    if f1.find('*Input, Invalid ACcess identifier*') > -1:
        f1 = TL1_In_Out('RTRV-OSRP-RMTNODES::OSRPRMTNODES-' + PRIMARY_SHELF_ID + '-1:QNDES;')
    F_HOST.write('\n>>>Begin: RTRV-OSRP-RMTNODES \n' + f1 + '\n>>>End: RTRV-OSRP-RMTNODES \n')
    f1 = TL1_In_Out('RTRV-OSRP-LINE::ALL:QLINE;')
    s1 = f1.split('\n')
    for i in s1:
        if i.find(' "OSRPLINE-') > -1:
            l1 = i.find(':')
            TL1_2_File('RTRV-OSRPLINE-SNC::' + i[4:l1] + ':QOSNC;', F_HOST)

    F_HOST.write('\n>>>Begin: RTRV-OSRP-LINE \n' + f1 + '\n>>>End: RTRV-OSRP-LINE \n')
    TL1_2_File('RTRV-OSRPLINK-METRICS::ALL:QMTR;', F_HOST)
    f1 = TL1_In_Out('RTRV-OSRP-LINK::ALL:QLINK;')
    s1 = f1.split('\n')
    f2 = []
    for i in s1:
        if i.find(' "OSRPLINK-') > -1:
            l1 = i.find(':')
            l2 = i[4:l1]
            s1.append(l2)
            TL1_2_File('RTRV-OSRPLINK-SNC::' + l2 + ':QOSNC;', F_HOST)

    F_HOST.write('\n>>>Begin: RTRV-OSRP-LINK \n' + f1 + '\n>>>End: RTRV-OSRP-LINK \n')
    f1 = TL1_In_Out('RTRV-OSRPRMTLINKS-METRICS::ALL:QMTRS;')
    if f1.find('*Input, Invalid ACcess identifier*') > -1:
        f1 = TL1_In_Out('RTRV-OSRPRMTLINKS-METRICS::OSRPRMTLINKS-' + PRIMARY_SHELF_ID + '-1:QMTRS;')
    F_HOST.write('\n>>>Begin: RTRV-OSRPRMTLINKS-METRICS \n' + f1 + '\n>>>End: RTRV-OSRPRMTLINKS-METRICS \n')
    f1 = TL1_In_Out('RTRV-OSRP-RMTLINKS::ALL:QLNKS;')
    if f1.find('*Input, Invalid ACcess identifier*') > -1:
        f1 = TL1_In_Out('RTRV-OSRP-RMTLINKS::OSRPRMTLINKS-' + PRIMARY_SHELF_ID + '-1:QLNKS;')
    F_HOST.write('\n>>>Begin: RTRV-OSRP-RMTLINKS \n' + f1 + '\n>>>End: RTRV-OSRP-RMTLINKS \n')
    F_HOST.write('>>>End: RTRV-OSRP++\n')
    F_HOST.write('>>>Begin: RTRV-SNC++\n')
    F_HOST.write('\n>>>Begin: RTRV-OSRP-RMTLINKS \n' + f1 + '\n>>>End: RTRV-OSRP-RMTLINKS \n')
    TL1_2_File('RTRV-SNC-ROUTE::ALL:QSNCR;', F_HOST)
    f2 = TL1_In_Out('RTRV-SNC::ALL:SNCEE;')
    F_HOST.write('>>>Begin: RTRV-SNC-EEDIAG++\n')
    s1 = f2.split('\n')
    for i in s1:
        if i.find('LABEL=') > -1:
            l1 = i.find(':')
            AID = i[4:l1]
            l1 = AID.rfind('-') + 1
            if int(AID[l1:]) > 1024:
                continue
            else:
                TL1_2_File('RTRV-SNC-EEDIAG::' + AID + ':QSNCE;', F_HOST)

    F_HOST.write('>>>End: RTRV-SNC-EEDIAG++\n')
    F_HOST.write('\n' + f2 + '\n')
    F_HOST.write('>>>End: RTRV-SNC++\n')
    F_HOST.write('>>>Begin: RTRV-SNCG++\n')
    TL1_2_File('RTRV-SNCG-ROUTE::ALL:QSGRT;', F_HOST)
    TL1_2_File('RTRV-SNCG::ALL:QSNCG;', F_HOST)
    F_HOST.write('>>>End: RTRV-SNCG++\n')
    F_HOST.write('>>>Begin: RTRV-DTL++\n')
    TL1_2_File('RTRV-DTL-SET::ALL:QDSET;', F_HOST)
    TL1_2_File('RTRV-DTL::ALL:QDTLA;', F_HOST)
    F_HOST.write('>>>End: RTRV-DTL++\n')
    F_HOST.write('>>>Begin: RTRV-PRF++\n')
    TL1_2_File('RTRV-LOC::ALL:QLOC;', F_HOST)
    TL1_2_File('RTRV-PRF-TXRX::ALL:QPRF;', F_HOST)
    F_HOST.write('>>>End: RTRV-PRF++\n')
    F_HOST.write('>>>Begin: RTRV-LICENSE++\n')
    TL1_2_File('RTRV-LICENSE-SERVER::ALL:QLIC1;', F_HOST)
    TL1_2_File('RTRV-LICENSE::ALL:QLIC2;', F_HOST)
    F_HOST.write('>>>End: RTRV-LICENSE++\n')
    TL1_2_File('RTRV-OPTMON::ALL:QOPT;', F_HOST)
    TL1_2_File('RTRV-PM-OPTMON::ALL:QOPTPM::OPT-OTS,,ALL,ALL,1-UNT,,,;', F_HOST)
    TL1_2_File('RTRV-SSC::ALL:QSSC;', F_HOST)
    TL1_2_File('RTRV-NMCC::ALL:QNMCC;', F_HOST)
    TL1_2_File('RTRV-CHC::ALL:QCHAN;', F_HOST)
    TL1_2_File('RTRV-PM-NMCMON::ALL:QNMCM::OPT-OCH,0-UP,ALL,ALL,BASLN&1-UNT,,,:,;', F_HOST)
    TL1_2_File('RTRV-PM-CHMON::ALL:QCHM::OPT-OCH,0-UP,ALL,ALL,BASLN&1-UNT,,,:,;', F_HOST)
    TL1_2_File('RTRV-PM-SDMON::ALL:QSDMO::OPT-OTS,0-UP,ALL,ALL,1-UNT,,,:,;', F_HOST)
    F_HOST.write('>>>Begin: RTRV-PTP++\n')
    s1 = TL1_2_File('RTRV-PTP::ALL:QPTP:::STATSINFO=YES;', F_HOST)
    if s1.find(' "PTP-') > -1:
        TL1_2_File('RTRV-PM-PTP::ALL:CTAG::ALL,0-UP,NEND,ALL,ALL,,,0&1;', F_HOST)
    F_HOST.write('>>>End: RTRV-PTP++\n')
    F_HOST.write('>>>Begin: RTRV-OTUTTP++\n')
    s1 = TL1_In_Out('RTRV-PMCONFIG-OTUTTP::ALL:QPTPH;')
    if s1.find(' "OTUTTP-') > -1:
        F_HOST.write(s1)
        TL1_2_File('RTRV-OSRP::ALL:QOSRP;', F_HOST)
        TL1_2_File('RTRV-OTUTTP::ALL:QTTP;', F_HOST)
        TL1_2_File('RTRV-PM-OTUTTP::ALL:PMTTP::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-OTUTTP++\n')
    F_HOST.write('>>>Begin: RTRV-ODUTTP++\n')
    s1 = TL1_2_File('RTRV-ODUTTP::ALL:QDTTP:::STATSINFO=YES;', F_HOST)
    if s1.find(' "ODUTTP-') > -1:
        TL1_2_File('RTRV-PM-ODUTTP::ALL:PDTTP::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-ODUTTP++\n')
    F_HOST.write('>>>Begin: RTRV-ETTP++\n')
    s1 = TL1_2_File('RTRV-ETTP::ALL:QETTP;', F_HOST)
    if s1.find(' "ETTP-') > -1:
        TL1_2_File('RTRV-PM-ETTP::ALL:PETTP::ALL,0-UP,NEND,ALL,ALL,,,0;', F_HOST)
    F_HOST.write('>>>End: RTRV-ETTP++\n')
    F_HOST.write('>>>Begin: RTRV-STTP++\n')
    s1 = TL1_2_File('RTRV-STTP::ALL:QSTTP:::STATSINFO=YES;', F_HOST)
    if s1.find(' "STTP-') > -1:
        TL1_2_File('RTRV-PM-STTP::ALL:PETTP::ALL,0-UP,ALL,ALL,ALL,,,0;', F_HOST)
    F_HOST.write('>>>End: RTRV-STTP++\n')
    F_HOST.write('>>>Begin: RTRV-TCMTTP++\n')
    s1 = TL1_2_File('RTRV-TCM::ALL:QTCM:::STATSINFO=YES;', F_HOST)
    if s1.find(' "TCMTTP-') > -1:
        TL1_2_File('RTRV-PM-TCM::ALL:PTCM::ALL,0-UP,NEND,ALL,ALL,,,0;', F_HOST)
    F_HOST.write('>>>End: RTRV-TCMTTP++\n')
    F_HOST.write('>>>Begin: RTRV-ODUCTP++\n')
    s1 = TL1_2_File('RTRV-ODUCTP::ALL:QDCTP:::STATSINFO=YES;', F_HOST)
    if s1.find(' "ODUCTP-') > -1:
        TL1_2_File('RTRV-PM-ODUCTP::ALL:PDCTP::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-ODUCTP++\n')
    F_HOST.write('>>>Begin: RTRV-ENCRYPT++\n')
    s1 = TL1_2_File('RTRV-ACCESS-ENCRYP::ALL:ENCR1;', F_HOST)
    s1 = TL1_2_File('RTRV-IP-ENCRYP::ALL:ENCR2;', F_HOST)
    F_HOST.write('>>>End: RTRV-ENCRYPT++\n')
    F_HOST.write('>>>Begin: RTRV-OTM4++\n')
    s1 = TL1_In_Out('RTRV-PMCONFIG-OTM::ALL:QOTM4;')
    F_HOST.write(s1)
    f1 = s1.split('\n')
    l_OTM_CPG = []
    for line in f1:
        if line.find('HCCSREF') > -1:
            f2 = line.split(',')
            AID = f2[0].replace('   "', '')
            l_OTM_CPG.append(AID)

    f1 = len(l_OTM_CPG)
    F_HOST.write('\n### >>>> OTM4 HCCSREF = ' + str(f1) + '\n')
    TL1_2_File('RTRV-OTM::ALL:QOTM4:::TTIINFO=YES,STATSINFO=YES;', F_HOST)
    for i in l_OTM_CPG:
        if i.find('OTM4-') > -1 or i.find('OTMC2-') > -1:
            TL1_2_File('RTRV-TTI-OTM::' + i + ':QST::RXINCTTI;', F_HOST)

    F_HOST.write('\n### >>>> OTM HCCSREF = ' + str(i) + '\n')
    TL1_2_File('RTRV-PM-OTM::ALL:QPM4::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-OTM4++\n')
    F_HOST.write('>>>Begin: RTRV-OTM3++\n')
    s1 = TL1_In_Out('RTRV-PMCONFIG-OTM3::ALL:QOTM3;')
    F_HOST.write(s1)
    f1 = s1.split('\n')
    l_OTM_CPG = []
    for line in f1:
        if line.find('HCCSREF') > -1:
            f2 = line.split(',')
            AID = f2[0].replace('   "', '')
            l_OTM_CPG.append(AID)

    f1 = len(l_OTM_CPG)
    F_HOST.write('\n### >>>> OTM3 HCCSREF = ' + str(f1) + '\n')
    TL1_2_File('RTRV-OTM3::ALL:QOTM3:::TTIINFO=YES,STATSINFO=YES;', F_HOST)
    for i in l_OTM_CPG:
        TL1_2_File('RTRV-TTI-OTM3::' + i + ':QST::RXINCTTI;', F_HOST)

    TL1_2_File('RTRV-PM-OTM3::ALL:QPM3::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-OTM3++\n')
    F_HOST.write('>>>Begin: RTRV-OTM2++\n')
    s1 = TL1_In_Out('RTRV-PMCONFIG-OTM2::ALL:QOTM2;')
    F_HOST.write(s1)
    l_OTM_CPG = []
    f1 = s1.split('\n')
    for line in f1:
        if line.find('HCCSREF') > -1:
            f2 = line.split(',')
            AID = f2[0].replace('   "', '')
            l_OTM_CPG.append(AID)

    f1 = len(l_OTM_CPG)
    F_HOST.write('\n### >>>> OTM2 HCCSREF = ' + str(f1) + '\n')
    for i in rID:
        TL1_2_File('RTRV-OTM2::OTM2-' + i + '-ALL:QOTM21:::TTIINFO=YES,STATSINFO=YES;', F_HOST)

    for i in l_OTM_CPG:
        TL1_2_File('RTRV-TTI-OTM2::' + i + ':QST::RXINCTTI;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PM-OTM2::OTM2-' + i + '-ALL:QOTM22::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)

    F_HOST.write('>>>End: RTRV-OTM2++\n')
    del l_OTM_CPG
    F_HOST.write('>>>Begin: RTRV-SLOTSEQ++\n')
    TL1_2_File('RTRV-TOPO-SLOTSEQ::ALL:QSLOT;', F_HOST)
    TL1_2_File('RTRV-TOPO-TIDSLOTSEQ::ALL:QTID;', F_HOST)
    F_HOST.write('>>>End: RTRV-SLOTSEQ++\n')
    F_HOST.write('>>>Begin: RTRV-ETH100++\n')
    for i in rID:
        TL1_2_File('RTRV-ETH100::ETH100-' + i + '-ALL:QET11;', F_HOST)

    TL1_2_File('RTRV-PM-ETH100::ALL:QET12::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-ETH100++\n')
    F_HOST.write('>>>Begin: RTRV-ETH++\n')
    TL1_2_File('RTRV-ETH::ALL:QET31;', F_HOST)
    TL1_2_File('RTRV-PM-ETH::ALL:QET32::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-ETH++\n')
    F_HOST.write('>>>Begin: RTRV-ETH10G++\n')
    s1 = ''
    for i in rID:
        s1 += TL1_In_Out('RTRV-ETH10G::ETH10G-' + i + '-ALL:QETH1;')

    F_HOST.write(s1)
    l_ETH_CPG = []
    f1 = s1.split('\n')
    for line in f1:
        if line.find('CLFI') > -1:
            f2 = line.split(':')
            AID = f2[0].replace('   "', '')
            l_ETH_CPG.append(AID)

    f1 = len(l_ETH_CPG)
    F_HOST.write('\n### >>>> ETH10G CLFI = ' + str(f1) + '\n')
    for i in rID:
        TL1_2_File('RTRV-PM-ETH10G::ETH10G-' + i + '-ALL:QETH2::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)

    F_HOST.write('>>>End: RTRV-ETH10G++\n')
    del l_ETH_CPG
    F_HOST.write('>>>Begin: RTRV-ETHN++\n')
    TL1_2_File('RTRV-ETHN::ALL:QET41:::STATSINFO=YES;', F_HOST)
    TL1_2_File('RTRV-PM-ETHN::ALL:QET42::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-ETHN++\n')
    F_HOST.write('>>>Begin: RTRV-WAN++\n')
    TL1_2_File('RTRV-WAN::ALL:QWAN1:::STATSINFO=YES;', F_HOST)
    TL1_2_File('RTRV-PM-WAN::ALL:QWAN2::ALL,0-UP,NEND,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-WAN++\n')
    F_HOST.write('>>>Begin: RTRV-EC1++\n')
    TL1_2_File('RTRV-EC1::ALL:QEC1;', F_HOST)
    TL1_2_File('RTRV-PM-EC1::ALL:QEC2::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-EC1++\n')
    F_HOST.write('>>>Begin: RTRV-VCE++\n')
    for i in rID:
        TL1_2_File('RTRV-VCE-COUNT::VCE-' + i + '-ALL:QVCE;', F_HOST)
        TL1_2_File('RTRV-VCE::VCE-' + i + '-ALL:QVCE;', F_HOST)
        TL1_2_File('RTRV-VCEMAP::VCEMAP-' + i + '-ALL:QVCE;', F_HOST)

    F_HOST.write('>>>End: RTRV-VCE++\n')
    F_HOST.write('>>>Begin: RTRV-VCS++\n')
    for i in rID:
        TL1_2_File('RTRV-VCS-COUNT::VCS-' + i + '-ALL:QVCS;', F_HOST)
        TL1_2_File('RTRV-VCS::VCS-' + i + '-ALL:QVCS;', F_HOST)

    F_HOST.write('>>>End: RTRV-VCS++\n')
    F_HOST.write('>>>Begin: RTRV-QGRP++\n')
    TL1_2_File('RTRV-QGRP-DFLT:::QDEF;', F_HOST)
    for i in rID:
        TL1_2_File('RTRV-QGRP::QGRP-' + i + '-ALL:QGRPS;', F_HOST)

    F_HOST.write('>>>End: RTRV-QGRP++\n')
    F_HOST.write('>>>Begin: RTRV-VT1++\n')
    TL1_2_File('RTRV-CRS-VT1::,:QVT11:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    TL1_2_File('RTRV-PM-VT1::ALL:QVT12::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-VT1++\n')
    F_HOST.write('>>>Begin: RTRV-VT2++\n')
    TL1_2_File('RTRV-CRS-VT2::,:QVT21:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    TL1_2_File('RTRV-PM-VT2::ALL:QVT22::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-VT2++\n')
    F_HOST.write('>>>Begin: RTRV-FLEX++\n')
    TL1_2_File('RTRV-FLEX::ALL:QFLX1:::STATSINFO=YES,TRCINFO=YES;', F_HOST)
    TL1_2_File('RTRV-PM-FLEX::ALL:QFLX3::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-FLEX++\n')
    F_HOST.write('>>>Begin: RTRV-FC++\n')
    for i in rID:
        TL1_2_File('RTRV-FC::FC100-' + i + '-ALL:QFC1;', F_HOST)
        TL1_2_File('RTRV-FC::FC200-' + i + '-ALL:QFC2;', F_HOST)
        TL1_2_File('RTRV-FC::FC400-' + i + '-ALL:QFC4;', F_HOST)
        TL1_2_File('RTRV-FC::FC1200-' + i + '-ALL:QFC12;', F_HOST)

    TL1_2_File('RTRV-PM-FC::ALL:QFPM2::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-FC++\n')
    F_HOST.write('>>>Begin: RTRV-DS1++\n')
    for i in rID:
        TL1_2_File('RTRV-T1::DS1-' + i + '-ALL:QDS1;', F_HOST)

    for i in l_DSM_PM:
        TL1_2_File('RTRV-PM-T1::' + i + ':QDSM1::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)

    TL1_2_File('RTRV-PM-T1::ALL:QDS1::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-DS1++\n')
    F_HOST.write('>>>Begin: RTRV-DS3++\n')
    for i in rID:
        TL1_2_File('RTRV-T3::DS3-' + i + '-ALL:QDS1;', F_HOST)

    TL1_2_File('RTRV-PM-T3::ALL:QDS3::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)
    F_HOST.write('>>>End: RTRV-DS3++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-OTM2++\n')
    for i in rID:
        TL1_2_File('RTRV-CRS-OTM2::OTM2-' + i + '-ALL,:QOTM2;', F_HOST)

    F_HOST.write('>>>End: RTRV-CRS-OTM2++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-ALL++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-ALL\n')
    TL1_2_File('RTRV-CRS-COUNT:::QNUM;', F_HOST)
    TL1_2_File('RTRV-CRS-ALL::ALL,ALL:QCRS:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-ALL\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS192C++\n')
    TL1_2_File('RTRV-CRS-STS192C::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS192C++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS48C++\n')
    TL1_2_File('RTRV-CRS-STS48C::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS48C++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS24C++\n')
    TL1_2_File('RTRV-CRS-STS24C::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS24C++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS12C++\n')
    TL1_2_File('RTRV-CRS-STS12C::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS12C++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS3C++\n')
    TL1_2_File('RTRV-CRS-STS3C::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS3C++\n')
    F_HOST.write('>>>Begin: RTRV-CRS-STS1++\n')
    TL1_2_File('RTRV-CRS-STS1::,:CTAG:::DISPLAY=PROV,CKTID=ALL;', F_HOST)
    F_HOST.write('>>>End: RTRV-CRS-STS1++\n')
    F_HOST.write('>>>End: RTRV-CRS-ALL++\n')
    TL1_2_File('RTRV-CRS-ODU::ALL:QODU;', F_HOST)
    TL1_2_File('RTRV-CRS-ODUCTP::ALL:QODUCT;', F_HOST)
    F_HOST.write('>>>Begin: RTRV-SYNCH++\n')
    TL1_2_File('RTRV-BITS-IN::ALL:QBIN;', F_HOST)
    TL1_2_File('RTRV-BITS-OUT::ALL:QBOUT;', F_HOST)
    TL1_2_File('RTRV-BITSOUTSW::ALL:QBSW;', F_HOST)
    for i in rID:
        TL1_2_File('RTRV-SYNCSTIN::BITSIN-' + i + '-A:QSIN;', F_HOST)
        TL1_2_File('RTRV-SYNCSTIN::BITSIN-' + i + '-B:QSIN;', F_HOST)
        TL1_2_File('RTRV-SYNCSTOUT::BITSOUT-' + i + '-A:QSIN;', F_HOST)
        TL1_2_File('RTRV-SYNCSTOUT::BITSOUT-' + i + '-B:QSIN;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-TMREFIN::SHELF-' + i + '-ALL:QREF;', F_HOST)
        TL1_2_File('RTRV-TMG-MODE::SHELF-' + i + '-ALL:QTMG;', F_HOST)

    TL1_2_File('RTRV-SYNCSW::ALL:QSW;', F_HOST)
    F_HOST.write('>>>End: RTRV-SYNCH++\n')
    F_HOST.write('>>>Begin: RTRV-SNMP++\n')
    TL1_2_File('RTRV-SNMP:::QSNMP;', F_HOST)
    TL1_2_File('RTRV-TRAP-DEST:::QDEST;', F_HOST)
    TL1_2_File('RTRV-USER-USM:::QUSM;', F_HOST)
    TL1_2_File('RTRV-USERGROUP-VACM:::QVACM;', F_HOST)
    F_HOST.write('>>>End: RTRV-SNMP++\n')
    OC_XYZ = ['OC768',
    'OC192',
    'OC48',
    'OC12',
    'OC3']
    F_HOST.write('>>>Begin: RTRV-SONET++\n')
    for i in OC_XYZ:
        if i == 'OC768':
            TL1_2_File('RTRV-' + i + '::ALL:QOC:::STATSINFO=YES,STINFO=YES;', F_HOST)
        if i == 'OC192':
            TL1_2_File('RTRV-' + i + '::ALL:QOC:::SSBITINFO=YES,STATSINFO=YES,STINFO=YES;', F_HOST)
        elif i == 'OC48' or i == 'OC12':
            TL1_2_File('RTRV-' + i + '::ALL:QOC:::SSBITINFO=YES,STINFO=YES;', F_HOST)
        elif i == 'OC3':
            TL1_2_File('RTRV-OC3::ALL:QOC:::DSMINFO=YES,SSBITINFO=YES,STINFO=YES;', F_HOST)
        TL1_2_File('RTRV-PM-' + i + '::ALL:QFPM2::ALL,0-UP,ALL,ALL,ALL,,,;', F_HOST)

    F_HOST.write('>>>End: RTRV-SONET++\n')
    F_HOST.write('>>>Begin: RTRV-PROT-SONET++\n')
    for i in OC_XYZ:
        for j in rID:
            TL1_2_File('RTRV-FFP-' + i + '::' + i + '-' + j + '-ALL,:QSON;', F_HOST)
            TL1_2_File('RTRV-PROTNSW-' + i + '::' + i + '-' + j + '-ALL,:QSON;', F_HOST)

    del OC_XYZ
    F_HOST.write('>>>End: RTRV-PROT-SONET++\n')
    F_HOST.write('>>>Begin: RTRV-PROT-EQPT++\n')
    TL1_2_File('RTRV-FFP-PROTGRP::ALL:QPROT:::;', F_HOST)
    TL1_2_File('RTRV-PROTNSW-EQPT::ALL:QPEQ;', F_HOST)
    TL1_2_File('RTRV-PROTNSW-PROTGRP::ALL:QPRGR;', F_HOST)
    F_HOST.write('>>>End: RTRV-PROT-EQPT++\n')
    F_HOST.write('>>>Begin: PROTECTION-OTM++\n')
    for i in rID:
        TL1_2_File('RTRV-FFP-OTM::OTM0-' + i + '-ALL,:QFP0;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-FFP-OTM2::OTM2-' + i + '-ALL,:QFP2;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-OTM2::OTM2-' + i + '-ALL,:QFP2;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-FFP-OTM3::OTM3-' + i + '-ALL,:QFP3;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-OTM3::OTM3-' + i + '-ALL,:QFP3;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-FFP-OTM::OTM4-' + i + '-ALL,:QFP4;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-OTM::OTM4-' + i + '-ALL,:QFP4;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-FFP-OTM::OTMFLEX-' + i + '-ALL,:QFLX;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-OTM::OTMFLEX-' + i + '-ALL,:QFLX;', F_HOST)

    F_HOST.write('>>>End: PROTECTION-OTM++\n')
    F_HOST.write('>>>Begin: RTRV-PROT-ETH++\n')
    for i in rID:
        TL1_2_File('RTRV-FFP-ETH::ETH-' + i + '-ALL,:QETH;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-ETH::ETH-' + i + '-ALL,:QETH;', F_HOST)

    F_HOST.write('>>>End: RTRV-PROT-ETH++\n')
    F_HOST.write('>>>Begin: RTRV-PROT-FLEX++\n')
    for i in rID:
        TL1_2_File('RTRV-FFP-FLEX::FLEX-' + i + '-ALL,:QFLX;', F_HOST)

    for i in rID:
        TL1_2_File('RTRV-PROTNSW-FLEX::FLEX-' + i + '-ALL,:QFLX;', F_HOST)

    F_HOST.write('>>>End: RTRV-PROT-ETH++\n')
    F_HOST.write('>>>Begin: RTRV-IPFILTER++\n')
    for i in rID:
        TL1_2_File('RTRV-IPFILTER::SHELF-' + i + '-ALL:QIPF;', F_HOST)

    F_HOST.write('>>>End: RTRV-IPFILTER++\n')
    F_HOST.write('>>>Begin: SECU-USER++\n')
    TL1_2_File('RTRV-SECU-USER::ALL:QSECU;', F_HOST)
    TL1_2_File('RTRV-SECU-BADPID:::QBAD;', F_HOST)
    F_HOST.write('>>>End: SECU-USER++\n')
    F_HOST.write('>>>Begin: SECU-RULES++\n')
    TL1_2_File('RTRV-SECU-DFLT:::QDFLT;', F_HOST)
    TL1_2_File('RTRV-SECU-PWDRLS:::QPWD;', F_HOST)
    TL1_2_File('RTRV-SYSLOG-SETTINGS:::QSSET;', F_HOST)
    TL1_2_File('RTRV-SYSLOG-SERVER:::QSERV1::SERVER1;', F_HOST)
    TL1_2_File('RTRV-SYSLOG-SERVER:::QSERV2::SERVER2;', F_HOST)
    TL1_2_File('RTRV-SYSLOG-SERVER:::QSERV3::SERVER3;', F_HOST)
    F_HOST.write('>>>End: SECU-RULES++\n')
    F_HOST.write('>>>Begin: RADIUS++\n')
    TL1_2_File('RTRV-AUTH-DFLT:::QSCA1;', F_HOST)
    TL1_2_File('RTRV-REMAUTH-ALTERNATE:::QSCA3;', F_HOST)
    TL1_2_File('RTRV-ATTR-CSA:::QSCA4:::EXINFO=Y;', F_HOST)
    TL1_2_File('RTRV-RADIUS-ACCOUNTING:::QSCA5:::EXINFO=Y;', F_HOST)
    TL1_2_File('RTRV-ATTR-REMACCT:::QSCA6::PRIMARY;', F_HOST)
    TL1_2_File('RTRV-ATTR-REMACCT:::QSCA7::SECONDARY;', F_HOST)
    TL1_2_File('RTRV-ATTR-REMAUTH:::QSCA8::PRIMARY;', F_HOST)
    TL1_2_File('RTRV-ATTR-REMAUTH:::QSCA9::SECONDARY;', F_HOST)
    TL1_2_File('RTRV-RADIUS-PROXY:::QSC10::AUTHENTICATION:EXINFO=Y;', F_HOST)
    TL1_2_File('RTRV-RADIUS-PROXY:::QSC11::ACCOUNTING:EXINFO=Y;', F_HOST)
    F_HOST.write('>>>End: RADIUS++\n')
    TL1_2_File('RTRV-SPLI:::QSPLI;', F_HOST)
    F_HOST.write('>>>BEGIN: RTRV-ALMPROFILE++\n')
    TL1_2_File('RTRV-ALMPROFILE:::QALL::AMP,:;', F_HOST)
    for i in rID:
        TL1_2_File('RTRV-ALMPROFILE-ACTIVE::SHELF-' + i + ':QDFL::AMP,AMP-' + i + '-ALL;', F_HOST)

    list1 = ['PROFILE1',
    'PROFILE2',
    'PROFILE3',
    'PROFILE4',
    'PROFILE5']
    for f1 in list1:
        for i in rID:
            TL1_2_File('RTRV-ALMPROFILE::SHELF-' + i + ':QPROF::AMP,' + f1 + ':PRFLINFO=Y;', F_HOST)

    F_HOST.write('>>>END: RTRV-ALMPROFILE++\n')
    TimeStamp = strftime('%Y-%m-%d @ %H:%M:%S')
    F_HOST.write('%s\n\n %s \n' % (TimeStamp, SCRIPT_VERSION))
    F_HOST.close()
    F_ALARM = open(WindowsHost + '_Alarm.txt', 'w')
    F_ALARM.write('\n ====> IP=%s \tTime=%s \n\n' % (HOST, strftime('%Y-%m-%d %H:%M:%S')))
    TL1_2_File('RTRV-ALM-ALL:::QALM1;', F_ALARM)
    TL1_2_File('RTRV-COND-ALL:::QALM2:::ALRMSTAT=DISABLED;', F_ALARM)
    TL1_2_File('RTRV-ALM-ENV::ALL:QALM3;', F_ALARM)
    TL1_2_File('RTRV-ATTR-ENV::ALL:QALM4;', F_ALARM)
    F_ALARM.close()
    f1 = TL1_In_Out('ALW-MSG-ALL::ALL:Q101;')
    if METHOD == 'TELNET':
        telnet_6500.close()
    else:
        ssh_6500.close()
    return sErr


def PARSE_RTRV_OTS(LinesIn, TID, dFEaid_SS, fName, F_ERROR):
    dMEMBERS = {}
    lOTSinfo = []
    dOTSinfo_CpShSl = {}
    dDISPots = {}
    dOSCots = {}
    lTxId = []
    lEnchanced = []
    instanceDOC = 0
    sOut = ''
    for line in LinesIn:
        if line.find(',DOCIND=') > -1 and line.find(',CFGTYPE=') > -1:
            if ',DOCIND=Y,' in line:
                instanceDOC = 1
            f1 = line.split(':')
            f2 = f1[0]
            AID = f2.replace('   "', '')
            f1 = AID.find('-') + 1
            f2 = AID.rfind('-')
            SHELF = 'SHELF-' + AID[f1:f2]
            firstMember = -1
            if line.find('OSID') > -1:
                OSID = FISH(line, 'OSID=\\"', '\\"')
                if line.find('OSC=') > -1:
                    firstMember = line.find('OSC=')
                elif line.find('LINEOUT=') > -1:
                    firstMember = line.find('LINEOUT=')
                elif line.find('LIM=') > -1:
                    firstMember = line.find('LIM=')
            else:
                OSID = ''
                if line.find('LIM=') > -1:
                    firstMember = line.find('LIM')
                elif line.find('SMD=') > -1:
                    firstMember = line.find('SMD')
                else:
                    if line.find('SUBTYPE=PASSIVE') == -1:
                        l1 = line.find(',UNEXPTLOSSTHRES=') + 17
                        l2 = len(line)
                        f1 = line[l1:l2]
                    else:
                        l1 = line.find(',BWCAL=') + 7
                        l2 = len(line)
                        f1 = line[l1:l2]
                    l1 = f1.find(',')
                    l2 = len(f1)
                    f2 = f1[l1:l2]
                    l1 = f2.find('=')
                    firstMember = f2[0:l1]
            if line.find('AMPMATE') > -1:
                l1 = line.find(',AMPMATE=')
            elif line.find('COMBINEDOPMPOWER') > -1:
                l1 = line.find(',COMBINEDOPMPOWER=')
            else:
                l1 = line.find(',ASSOCIATEDOTS=')
            if firstMember > 0:
                f1 = line[firstMember:l1]
                OTSMEMBERS = f1.replace(',', '+')
            else:
                OTSMEMBERS = 'NONE'
            if line.find('ISS=ISS'):
                ISS = FISH(line, 'ISS=', ',')
                if ISS != '':
                    OTSMEMBERS = OTSMEMBERS + ' (+ISS=' + ISS + ')'
            l1 = line.rfind('"')
            line = line[:l1] + ','
            CFGTYPE = FISH(line, 'CFGTYPE=', ',')
            SUBTYPE = FISH(line, 'SUBTYPE=', ',')
            TXPATH = FISH(line, 'TXPATH=', ',')
            RXPATH = FISH(line, 'RXPATH=', ',')
            DOCIND = FISH(line, 'DOCIND=', ',')
            PEPCCLAMP = FISH(line, 'PEPCCLAMP=', ',')
            SLOTCFGMODE = FISH(line, 'SLOTCFGMODE=', ',')
            AMPMATE = FISH(line, 'AMPMATE=', ',')
            ASSOCIATEDOTS = FISH(line, 'ASSOCIATEDOTS=\\"', '\\"')
            CPS = FISH(line, 'CPS=', ',')
            AUTOROUTE = FISH(line, 'AUTOROUTE=', ',')
            SEQCHCUPDATES = FISH(line, 'SEQCHCUPDATES=', ',')
            GBWIDTH = FISH(line, 'GBWIDTH=', ',')
            OSCREQUIRED = FISH(line, 'OSCREQUIRED=', ',')
            ENHANCEDTOPOLOGY = FISH(line, 'ENHANCEDTOPOLOGY=', ',')
            BUNFACTOR = FISH(line, 'BUNFACTOR=', ',')
            DRADSCMTOPOLOGY = FISH(line, 'DRADSCMTOPOLOGY=', ',')
            AUTOGB = FISH(line, 'AUTOGB=', ',')
            CSIND = FISH(line, 'CSIND=', ',')
            PROVCTRLMODE = FISH(line, 'PROVCTRLMODE=', ',')
            if PROVCTRLMODE == '50':
                PROVCTRLMODE = 'Fixed ITU 50 GHz'
            elif PROVCTRLMODE == '12.5':
                PROVCTRLMODE = 'Flex Grid capable'
            else:
                PROVCTRLMODE = 'Undefined'
            ACTCTRLMODE = FISH(line, 'ACTCTRLMODE=', ',')
            if ACTCTRLMODE == '50':
                ACTCTRLMODE = 'Fixed ITU 50 GHz'
            elif ACTCTRLMODE == '12.5':
                ACTCTRLMODE = 'Flex Grid capable'
            else:
                ACTCTRLMODE = 'Undefined'
            OTSOut = SHELF + ',' + CFGTYPE + ',' + SUBTYPE + ',' + AID + ',' + OSID + ',' + TXPATH + ',' + RXPATH + ',' + OTSMEMBERS + ',' + DOCIND + ',' + SLOTCFGMODE + ',' + AMPMATE + ',' + ASSOCIATEDOTS + ',' + PROVCTRLMODE + ',' + ACTCTRLMODE + ',' + CPS + ',' + AUTOROUTE + ',' + SEQCHCUPDATES + ',' + GBWIDTH + ',' + AUTOGB + ',' + OSCREQUIRED + ',' + CSIND + ',' + PEPCCLAMP + ',' + ENHANCEDTOPOLOGY + ',' + BUNFACTOR + ',' + DRADSCMTOPOLOGY
            if CFGTYPE == 'AMP' and AMPMATE == '':
                F_ERROR.write(',' + AID + ',Associated OTS must be provisioned for line amplifiers\n')
            if ENHANCEDTOPOLOGY != 'ENABLE':
                F_ERROR.write(',' + AID + ',Enchanced Topology not enabled\n')
            if CSIND == 'Y':
                F_ERROR.write(',' + AID + ',has Coherent Select enabled\n')
            fe = ''
            for j, k in dFEaid_SS.items():
                if j in OTSOut:
                    fe = k
                    break

            sOut += TID + ',' + OTSOut + ',' + fe + '\n'
            label = AID + ',' + OSID + ',' + TXPATH + ',' + RXPATH + ',' + fe
            lTxId.append(TXPATH)
            lEnchanced.append(ENHANCEDTOPOLOGY)
            lOTSinfo.append(label)
            dMEMBERS[label] = OTSMEMBERS
            if OTSMEMBERS.find('OSC-') > -1:
                f1 = OTSMEMBERS.split('+')
                for i in range(len(f1)):
                    SEC = f1[i]
                    l1 = SEC.find('=') + 1
                    s1 = SEC[l1:]
                    if s1.count('-') == 2:
                        l1 = s1.find('-') + 1
                        ShSl = s1[l1:]
                    elif s1.count('-') == 3:
                        l1 = s1.find('-') + 1
                        l2 = s1.rfind('-')
                        ShSl = s1[l1:l2]
                    dOTSinfo_CpShSl[ShSl] = label
                    if f1[i].find('OSC=OSC-') > -1:
                        s1 = f1[i].split('=')
                        s2 = s1[1]
                        if line.find('SLOTCFGMODE=DERIVED') > -1:
                            dOSCots[s2] = label + ',DERIVED'
                        else:
                            dOSCots[s2] = label + ',PROVISIONED'

            if OTSMEMBERS.find('DSCM') > -1:
                for i in range(len(f1)):
                    if f1[i].find('=DSCM-') > -1:
                        if line.find('SLOTCFGMODE=DERIVED') > -1:
                            s2 = 'DERIVED'
                        else:
                            s2 = 'PROVISIONED'
                        s1 = f1[i].split('=')
                        f2 = s1[1].replace('DSCM', 'DISP')
                        dDISPots[f2 + '-1'] = label + ',' + s2

    l1 = len(set(lTxId))
    l2 = len(lOTSinfo)
    if l2 != l1:
        F_ERROR.write(',' + TID + ',The TID must not have two equal Tx ID\n')
    l1 = len(set(lEnchanced))
    if l1 != 1 and l1 != 0:
        F_ERROR.write(',' + TID + ',All OTS should have identical Enhanced Topology settings\n')
    del lTxId
    del lEnchanced
    if sOut != '':
        F_NOW = open(fName, 'w')
        f1 = 'TID,Shelf,Configuration,Subtype,AID,OSID,TX Path ID,RX Path ID,OTS Members,DOC Site,Slot Sequence Mode,AMP Mate OTS,Accosiated OTS,Provisioned Control Mode,Actual Control Mode,Control Plane (CPS),Automatic Routing,Sequence CHC Updates,Guardband BW,Auto-Guardbanding,OSC Required,Coherent Select OTS,Clamp Mode,Enhanced Topology,Bun Factor,Raman Topology,Far End Reliable AID\n'
        F_NOW.write(f1 + sOut)
        F_NOW.close()
    return (dMEMBERS,
    lOTSinfo,
    dOTSinfo_CpShSl,
    dDISPots,
    dOSCots,
    instanceDOC)


def PARSE_COLLECTED_DATA(WindowsHost):
    TID_pattern = '\\d\\d-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d'
    ErrorMessage = ''
    TID = '?'
    try:
        F_HOST = open(WindowsHost + '.txt', 'r')
    except:
        print (WindowsHost + ' not found')
        F_DBG.write('\n\n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% %s.txt not found %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% \n' % HOST)
        return

    TimeStamp = F_HOST.readline()
    mcemon = '?'
    for i in F_HOST:
        if len(i) < 4:
            continue
        if i.find('*MCEMON is ON*') > -1:
            mcemon = 'MCEMON = ENABLED'
        if i.find('*MCEMON is OFF*') > -1:
            mcemon = 'MCEMON = DISABLED'
        f1 = re.search(TID_pattern, i)
        if f1:
            f1 = i.lstrip(' ')
            tokens = f1.split(' ')
            TID = tokens[0]
            break

    F_HOST.seek(0)
    CPL = '?'
    for i in F_HOST:
        if 'CPL =' in i:
            if 'CPL = YES' in i:
                CPL = 'YES'
                break
            else:
                CPL = 'NO'
                break

    if TID == '?':
        return 'Undetermined TID'
    F_DBG.write('\nTime Stamp = %sScript Version = %s\nTID = %s' % (TimeStamp, SCRIPT_VERSION, TID))
    F_ERROR = open(WindowsHost + '_ Issues.csv', 'w')
    F_ERROR.write('Script Version = ' + SCRIPT_VERSION + '\n' + TimeStamp + '\n\nIP = ' + HOST + '\nTID = ' + TID + '\n\n')
    if TID.find('"') > -1:
        TID = TID.replace('"', '')
    if EXPECTED_TID and TID.strip().upper() != EXPECTED_TID:
        return 'TID mismatch: expected %s, got %s' % (EXPECTED_TID, TID.strip())
    F_ERROR.write('\nShelf Provisioning Issues (see tab Shelf)\n')
    F_NOW = open(WindowsHost + '_Shelves.csv', 'w')
    F_NOW.write(TimeStamp + '\nIP = ' + HOST + '\nTID = ' + TID + '\n' + mcemon + '\n\n')
    list1 = TL1_Strip('RTRV-CLLI:::QCLLI;', F_HOST)
    list1 += TL1_Strip('RTRV-NETYPE:::QTYPE;', F_HOST)
    list1 += TL1_Strip('RTRV-TMZONE:::TOD0;', F_HOST)
    list1 += TL1_Strip('RTRV-TOD-MODE:::TOD1;', F_HOST)
    list1 += TL1_Strip('RTRV-TOD-SER:::TOD2;', F_HOST)
    neType = PARSE_TOD(list1, F_NOW, F_ERROR)
    list1 = TL1_Strip('RTRV-SHELF::ALL:QSHELF;', F_HOST)
    PRI_SHELF_NAME, lSHELF_ID = PARSE_RTRV_SHELF(list1, neType, F_NOW, F_ERROR)
    s1 = TL1_Strip('RTRV-BACKPLANE++', F_HOST) + TL1_Strip('RTRV-SYS::ALL:QSYS;', F_HOST)
    if any(('"NEMODE=SDH"' in s for s in s1)):
        sysNEMODE = 'SDH'
    else:
        sysNEMODE = 'SONET'
    PARSE_RTRV_SYS(s1, CPL, F_NOW, F_ERROR)
    list1 = TL1_Strip('RTRV-NETYPE:::QTYPE;', F_HOST)
    list1 += TL1_Strip('RTRV-MEMBER::ALL:QMEMBE;', F_HOST)
    list1 += TL1_Strip('RTRV-SW-VER::ALL:SWVER;', F_HOST)
    list1 += TL1_Strip('RTRV-RELEASE:::ALLSW;', F_HOST)
    list1 += TL1_Strip('RTRV-PROV-STATE++', F_HOST)
    list1 += TL1_Strip('RTRV-TL1GW:::QGW;', F_HOST)
    list1 += TL1_Strip('RTRV-SSL-SRVR::ALL:QSSL;', F_HOST)
    list1 += TL1_Strip('RTRV-SNMP++', F_HOST)
    PARSE_SHELF_ALL(list1, F_NOW, F_ERROR)
    F_NOW.close()
    list0 = TL1_Strip('RTRV-DCN00++', F_HOST)
    if any((' "SHELF-' in s for s in list0)):
        F_NOW = open(WindowsHost + '_DCN.csv', 'w', newline='')
        PARSE_ALL_DCN(list0, TID, F_NOW)
        F_NOW.close()
    del list0
    s1 = TL1_Strip('RTRV-DCN01++', F_HOST)
    if any(('::IPADDR=' in s for s in s1)):
        f1 = WindowsHost + '_Static_Routes.csv'
        PARSE_STATIC_ROUTES(s1, TID, f1)
    s1 = TL1_Strip('RTRV-IPxRTG-TBL++', F_HOST)
    if any((',NEXTHOP=' in s for s in s1)):
        F_NOW = open(WindowsHost + '_Routing_Table.csv', 'w', newline='')
        PARSE_RTRV_IPxRTG_TBL(s1, TID, F_NOW)
        F_NOW.close()
    dFEaid_SS = {}
    dCLFI_SSP = {}
    list1 = TL1_Strip('RTRV-ADJ::ALL:ADJALL;', F_HOST)
    for line in list1:
        if '::ADJTYPE=LINE,' in line and ',ADJSTAT=RELIABLE,' in line:
            f1 = line.split('::')
            s1 = f1[0].replace('   "', '')
            l1 = s1.find('-')
            l2 = s1.rfind('-') + 1
            j = s1[l1:l2]
            dFEaid_SS[j] = FISH(line, 'DISCFEADDR=\\"', '\\"')
        elif 'ADJTYPE=TXRX,' in line:
            f1 = line.split('::')
            s1 = f1[0].replace('   "ADJ', 'ADJ')
            dCLFI_SSP[s1] = FISH(line, 'CLFI=\\"', '\\"')

    list1 = TL1_Strip('RTRV-OTS::ALL:QOTS;', F_HOST)
    F_ERROR.write('\nOTS Provisioning Issues (see tab OTS)\n')
    f1 = WindowsHost + '_OTS.csv'
    dMEMBERS, lOTSinfo, dOTSinfo_CpShSl, dDISPots, dOSCots, instanceDOC = PARSE_RTRV_OTS(list1, TID, dFEaid_SS, f1, F_ERROR)
    list1 = TL1_Strip('RTRV-INVENTORY++', F_HOST)
    F_NOW = open(WindowsHost + '_Inventory.csv', 'w')
    PARSE_RTRV_INVENTORY(list1, F_NOW, TID)
    F_NOW.close()
    list1 = TL1_Strip('RTRV-EQPT++', F_HOST)
    if len(list1) < 5:
        list1 = TL1_Strip('RTRV-AUTOEQUIP::ALL:QAUTO;', F_HOST)
        list1 += TL1_Strip('RTRV-EQPTMODE::ALL:QMODE;', F_HOST)
        list1 += TL1_Strip('RTRV-EQPT::ALL:QEQUIP;', F_HOST)
    f1 = WindowsHost + '_Equipment.csv'
    lSHELF_XC, dCTYPE, dCPACK, dCPPEC, needOPTMON, d_EQUIPMENT_STATE__AID, reportWSS, reportDISP = PARSE_RTRV_EQUIPMENT(list1, TID, PRI_SHELF_NAME, f1, F_ERROR)
    list1 = TL1_Strip('RTRV-OSRP++', F_HOST)
    f1 = str(list1)
    if ',TYPE=PHOTONIC' in f1:
        OSRP_NODENAME, OSRP_NODEID, OSRP_NODEID_HEX, OSRP_NODEIP = PARSE_RTRV_OSRP_L0_ALL(list1, TID, WindowsHost)
        OSRP_TYPE = 'Photonic'
    elif ',TYPE=OTN' in f1:
        OSRP_NODENAME, OSRP_NODEID, OSRP_NODEID_HEX, OSRP_NODEIP = PARSE_RTRV_OSRP_L1_ALL(list1, TID, WindowsHost)
        OSRP_TYPE = 'OTN'
    else:
        OSRP_NODENAME = ''
        OSRP_NODEID = ''
        OSRP_NODEID_HEX = ''
        OSRP_NODEIP = ''
        OSRP_TYPE = ''
    list1 = TL1_Strip('RTRV-DOC-CH++', F_HOST)
    if len(list1) > 5:
        if any((',SNRBIAS=' in s for s in list1)):
            dINFO_4_SNC__SourceADJ = PARSE_RTRV_DOC_CH_FLEX(list1, lOTSinfo, TID, dCPACK, WindowsHost)
        else:
            dINFO_4_SNC__SourceADJ = {}
            F_NOW = open(WindowsHost + '_DOC_OCH.csv', 'w')
            PARSE_RTRV_DOC_CH_FIXED(list1, lOTSinfo, TID, F_NOW)
            F_NOW.close()
    else:
        list1 = TL1_Strip('RTRV-DOC-CH', F_HOST)
        F_NOW = open(WindowsHost + '_DOC_OCH.csv', 'w')
        PARSE_RTRV_DOC_CH_FIXED(list1, lOTSinfo, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-SNC++', F_HOST)
    if any(('INCARNATION' in s for s in list1)):
        dINFO_4_SNCG__SncId, dOSRP_RMTLINKS__NameId = PARSE_RTRV_SNC(list1, lOTSinfo, TID, WindowsHost, dINFO_4_SNC__SourceADJ, OSRP_TYPE)
        del dINFO_4_SNC__SourceADJ
    else:
        dOSRP_RMTLINKS__NameId = {}
        dINFO_4_SNCG__SncId = {}
    list1 = TL1_Strip('RTRV-SNCG++', F_HOST)
    if any(('MAXSNCSPACING' in s for s in list1)):
        PARSE_RTRV_SNCG(list1, lOTSinfo, TID, WindowsHost, dINFO_4_SNCG__SncId, dOSRP_RMTLINKS__NameId)
    del dINFO_4_SNCG__SncId
    list1 = TL1_Strip('RTRV-LICENSE++', F_HOST)
    if any((' "LICENSE-' in s for s in list1)):
        PARSE_RTRV_LICENSE_and_SERVER(list1, TID, WindowsHost)
    list1 = TL1_Strip('RTRV-PRF++', F_HOST)
    if any(('TXSIGBW3DB' in s for s in list1)):
        PARSE_RTRV_PRF_and_LOC(list1, lOTSinfo, TID, dCPACK, dOTSinfo_CpShSl, WindowsHost)
    list1 = TL1_Strip('RTRV-DTL++', F_HOST)
    if any(('TERMNODENAME' in s for s in list1)):
        PARSE_RTRV_DTL(list1, TID, WindowsHost, dOSRP_RMTLINKS__NameId)
    del dOSRP_RMTLINKS__NameId
    s1 = TL1_Strip('RTRV-AMP++', F_HOST)
    if any(('AMP:OPIN-OTS' in s for s in s1)):
        F_ERROR.write('\nAmplifier Provisioning Issues (see tab Amplifiers)\n')
        F_NOW = open(WindowsHost + '_Amplifiers.csv', 'w')
        PARSE_RTRV_AMPLIFIERS(s1, dMEMBERS, dCPACK, needOPTMON, TID, F_NOW, F_ERROR)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-SLOTSEQ++', F_HOST)
    if any(('"SLOTSEQ-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_SlotSequence.csv', 'w')
        PARSE_RTRV_TOPO_SLOTSEQ(list1, TID, dMEMBERS, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-OSC++', F_HOST)
    if any(('RXPATHLOSS' in s for s in list1)):
        F_NOW = open(WindowsHost + '_OSC.csv', 'w')
        PARSE_RTRV_OSC(list1, dOSCots, TID, F_NOW, F_ERROR)
        F_NOW.close()
        del dOSCots
    if reportDISP == 1:
        F_ERROR.write('\nOTS DSCM/Pad Provisioning Issues (see tab DSCM&Pads)\n')
        s1 = TL1_Strip('RTRV-DISP::ALL:DISPQ;', F_HOST)
        F_NOW = open(WindowsHost + '_DSCM_Pads.csv', 'w')
        dDISP = PARSE_RTRV_DISP(s1, dDISPots, TID, F_NOW, F_ERROR)
        F_NOW.close()
    else:
        dDISP = {}
        F_ERROR.write('\nNo Pad/DSCM were found in the Equipment list\n')
    del dDISPots
    list1 = TL1_Strip('RTRV-VOA++', F_HOST)
    if any(('VOA:OPOUT' in s for s in list1)):
        F_ERROR.write('\nVOA Issues (see tab VOA)\n')
        F_NOW = open(WindowsHost + '_VOA.csv', 'w')
        PARSE_RTRV_VOA(list1, TID, dMEMBERS, dCPACK, F_NOW)
        F_NOW.close()
    s1 = TL1_Strip('RTRV-RAMAN++', F_HOST)
    if any(('PUMP3POWER' in s for s in s1)):
        F_ERROR.write('\nRaman Amplifier Issues (see tab Raman)\n')
        F_NOW = open(WindowsHost + '_Raman.csv', 'w')
        if any((',TARGPOWPASSWORD=' in s for s in s1)):
            list1 = TL1_Strip('RTRV-PM-OPTMON:', F_HOST) + s1
            PARSE_RTRV_6500_RAMAN(list1, dMEMBERS, TID, F_NOW, F_ERROR)
            F_NOW.close()
        else:
            PARSE_RTRV_CPL_RAMAN(s1, TID, F_NOW)
            F_NOW.close()
    s1 = TL1_Strip('RTRV-TELEMETRY&OTDRCFG++', F_HOST)
    if any(('FIBERTYPE=' in s for s in s1)):
        F_NOW = open(WindowsHost + '_Telemetry.csv', 'w')
        PARSE_RTRV_TELEMETRY(s1, dCPACK, dMEMBERS, TID, F_NOW, F_ERROR)
        F_NOW.close()
        s1 = TL1_Strip('RTRV-OTDR-EVENTS++', F_HOST)
        F_NOW = open(WindowsHost + '_OTDR.csv', 'w')
        PARSE_RTRV_OTDR_EVENTS(s1, dMEMBERS, TID, F_NOW)
        F_NOW.close()
    if instanceDOC == 1:
        s1 = TL1_Strip('RTRV-DOC::ALL:QDOC;', F_HOST)
        s1 = s1 + TL1_Strip('RTRV-DOC-CLASS::ALL:QDP;', F_HOST)
        F_ERROR.write('\nDOC Provisioning Issues (see tab DOC)\n')
        PARSE_ALL_DOC(s1, TID, lOTSinfo, F_ERROR, WindowsHost)
    F_ERROR.write('\nAdjacency Issues (see tab Adjacencies)\n')
    list1 = TL1_Strip('RTRV-ADJ-LINE::ALL:ADJLIN;', F_HOST)
    list2 = TL1_Strip('RTRV-ADJ-FIBER::ALL:ADJFIB;', F_HOST)
    list3 = TL1_Strip('RTRV-ADJ::ALL:ADJALL;', F_HOST)
    s1 = list1 + list2 + list3
    F_NOW = open(WindowsHost + '_Adjacencies.csv', 'w')
    dTxADJACENCY, dRxADJACENCY = PARSE_RTRV_ADJACENCY(s1, dCPACK, dDISP, dMEMBERS, TID, F_NOW, F_ERROR)
    F_NOW.close()
    if reportWSS == 1:
        list1 = TL1_Strip('RTRV-SSC::ALL:QSSC;', F_HOST)
        if any(('CRELATIVEINDEX=' in s for s in list1)):
            F_NOW = open(WindowsHost + '_SSC.csv', 'w')
            PARSE_RTRV_SSC(list1, dMEMBERS, TID, F_NOW, F_ERROR)
            F_NOW.close()
        list1 = TL1_Strip('RTRV-NMCC::ALL:QNMCC;', F_HOST)
        if any(('CENTERFREQ=' in s for s in list1)):
            F_NOW = open(WindowsHost + '_WSS_NMCC.csv', 'w')
            PARSE_RTRV_NMCC(list1, dMEMBERS, TID, F_NOW, F_ERROR)
            F_NOW.close()
        list1 = TL1_Strip('RTRV-CHC::ALL:QCHAN;', F_HOST)
        F_NOW = open(WindowsHost + '_WSS_CHC.csv', 'w')
        PARSE_RTRV_CHC(list1, dMEMBERS, TID, F_NOW, F_ERROR)
        F_NOW.close()
    else:
        F_ERROR.write('\nNo WSS found in the TID\n')
    list1 = TL1_Strip('RTRV-ADJ-TX::ALL:QTX;', F_HOST)
    if any(('ADJTXTYPE=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_Tx_Adjacency.csv', 'w')
        d_ADJTXTYPE_PR_DSC__AID = PARSE_RTRV_ADJ_TX(list1, dTxADJACENCY, dMEMBERS, TID, F_NOW, F_ERROR)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ADJ-RX::ALL:QRX;', F_HOST)
    if any(('ADJRXTYPE=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_Rx_Adjacency.csv', 'w')
        PARSE_RTRV_ADJ_RX(list1, dRxADJACENCY, dMEMBERS, TID, dTxADJACENCY, d_ADJTXTYPE_PR_DSC__AID, F_NOW, F_ERROR)
        F_NOW.close()
        del d_ADJTXTYPE_PR_DSC__AID
        del dRxADJACENCY
        del dTxADJACENCY
    list1 = TL1_Strip('RTRV-PM-OPTMON::ALL:QOPTPM::OPT-OTS,,ALL,ALL,1-UNT,,,;', F_HOST)
    list2 = TL1_Strip('RTRV-PM-OPTMON::ALL:QOPRPM::OPR-OTS,,ALL,ALL,1-UNT,,,;', F_HOST)
    list3 = TL1_Strip('RTRV-OPTMON::ALL:QOPT;', F_HOST)
    s1 = list1 + list2 + list3
    if any((',OPTMON:O' in s for s in s1)):
        F_NOW = open(WindowsHost + '_OPTMON.csv', 'w')
        PARSE_RTRV_OPTMON(s1, dMEMBERS, dCPACK, TID, F_NOW, F_ERROR)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-PM-CHMON', F_HOST)
    if any((',CHMON:OPT-OCH,' in s for s in list1)):
        F_NOW = open(WindowsHost + '_CHMON.csv', 'w')
        PARSE_RTRV_PM_CHMON(list1, dMEMBERS, dCPACK, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-PM-NMCMON', F_HOST)
    if any((',NMCMON:OPT-OCH,' in s for s in list1)):
        F_NOW = open(WindowsHost + '_NMCMON.csv', 'w')
        PARSE_RTRV_PM_NMCMON(list1, dMEMBERS, dCPACK, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-PM-SDMON', F_HOST)
    if any((',SDMON:OPT-OTS,' in s for s in list1)):
        F_NOW = open(WindowsHost + '_SDMON.csv', 'w')
        PARSE_RTRV_PM_SDMON(list1, dMEMBERS, dCPACK, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-LLSDCC:::QDCC;', F_HOST)
    dLDCC = {}
    dLDCC_0 = {}
    dLDCC_1 = {}
    for line in list1:
        if line.find('  "') > -1 and line.find(',CARRIER=') > -1 and line.find('::NETDOMAIN=') > -1:
            f1 = line.split('::')
            s1 = f1[0]
            AID = s1.replace('   "', '')
            l1 = FISH(line, 'NETDOMAIN=', ',')
            l2 = FISH(line, 'CARRIER=', ',')
            f1 = FISH(line, 'OPER_CARRIER=', ',')
            f2 = FISH(line, 'PROTOCOL=', ',')
            s1 = FISH(line, 'FCS_MODE=', '"')
            dLDCC[AID] = l1 + ',' + l2 + ',' + f1 + ',' + f2 + ',' + s1
            if l2 == 'GCC0':
                dLDCC_0[AID] = l1 + ',' + l2 + ',' + f1 + ',' + f2 + ',' + s1
            else:
                dLDCC_1[AID] = l1 + ',' + l2 + ',' + f1 + ',' + f2 + ',' + s1

    list1 = TL1_Strip('RTRV-OTM4++', F_HOST)
    if any(('"OTM4-' in s for s in list1)):
        F_ERROR.write('\nOTM4 Issues (see tab OTM4)\n')
        F_NOW = open(WindowsHost + '_OTM4.csv', 'w', newline='')
        PARSE_RTRV_OTM4('OTM4', list1, TID, dCPPEC, dLDCC_0, dLDCC_1, F_ERROR, F_NOW)
        F_NOW.close()
    if any(('"OTMC2-' in s for s in list1)):
        F_ERROR.write('\nOTM4 Issues (see tab OTM4)\n')
        F_NOW = open(WindowsHost + '_OTMC2.csv', 'w', newline='')
        PARSE_RTRV_OTM4('OTMC2', list1, TID, dCPPEC, dLDCC_0, dLDCC_1, F_ERROR, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-OTM3++', F_HOST)
    if any(('"OTM3-' in s for s in list1)):
        F_ERROR.write('\nOTM3 Issues (see tab OTM3)\n')
        F_NOW = open(WindowsHost + '_OTM3.csv', 'w', newline='')
        PARSE_RTRV_OTM3(list1, TID, dLDCC, F_ERROR, F_NOW)
        F_NOW.close()
    d_FACILITY_STATE__AID = {}
    list0 = TL1_Strip('RTRV-OTM2++', F_HOST)
    if any((' "OTM2-' in s for s in list0)):
        F_ERROR.write('\nOTM2 Issues (see tab OTM2)\n')
        F_NOW = open(WindowsHost + '_OTM2.csv', 'w', newline='')
        PARSE_RTRV_OTM2(list0, TID, dLDCC_0, dLDCC_1, d_FACILITY_STATE__AID, F_ERROR, F_NOW)
        F_NOW.close()
    del list0
    list1 = TL1_Strip('RTRV-PTP++', F_HOST)
    if any((' "PTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_PTP.csv', 'w', newline='')
        fErr = PARSE_RTRV_PTP(list1, TID, dCPPEC, dCLFI_SSP, F_NOW)
        F_NOW.close()
        if fErr != '':
            F_ERROR.write('\nPTP Issues (see tab PTP)\n' + fErr)
    list1 = TL1_Strip('RTRV-OTUTTP++', F_HOST)
    if any((' "OTUTTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_OTUTTP.csv', 'w', newline='')
        PARSE_RTRV_OTUTTP(list1, TID, dLDCC_0, dLDCC_1, dCLFI_SSP, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ODUTTP++', F_HOST)
    if any((' "ODUTTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_ODUTTP.csv', 'w', newline='')
        PARSE_RTRV_ODUTTP(list1, TID, dCLFI_SSP, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ETTP++', F_HOST)
    if any((' "ETTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_ETTP.csv', 'w', newline='')
        PARSE_RTRV_ETTP(list1, TID, dCLFI_SSP, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-STTP++', F_HOST)
    if any((' "STTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_STTP.csv', 'w', newline='')
        PARSE_RTRV_STTP(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-TCMTTP++', F_HOST)
    if any((' "TCMTTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_TCMTTP.csv', 'w', newline='')
        PARSE_RTRV_TCMTTP(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ODUCTP++', F_HOST)
    if any((' "ODUCTP-' in s for s in list1)):
        F_NOW = open(WindowsHost + '_ODUCTP.csv', 'w', newline='')
        dRATE__AID = PARSE_RTRV_ODUCTP(list1, TID, F_NOW)
        F_NOW.close()
    else:
        dRATE__AID = []
    list1 = TL1_Strip('RTRV-PROTNSW-PROTGRP::ALL:QPRGR;', F_HOST)
    list1 += TL1_Strip('RTRV-FFP-PROTGRP::ALL:QPROT:::;', F_HOST)
    if any((' "PROTGRP' in s for s in list1)):
        PARSE_OTN_PROTECTION(list1, TID, WindowsHost)
    list1 = TL1_Strip('RTRV-ENCRYPT++', F_HOST)
    if any((',LANPORT=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_Encryption.csv', 'w', newline='')
        PARSE_ENCRYPT(list1, TID, F_NOW)
        F_NOW.close()
    del list1
    list1 = TL1_Strip('RTRV-QGRP++', F_HOST)
    if any((',CRDPRF=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_Q_Groups.csv', 'w', newline='')
        PARSE_Q_GROUPS(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ETH++', F_HOST)
    if any((',MTU=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_ETH1G.csv', 'w', newline='')
        PARSE_RTRV_ETH(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ETH100++', F_HOST)
    if any((',MTU=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_ETH100M.csv', 'w', newline='')
        PARSE_RTRV_ETH(list1, TID, F_NOW)
        F_NOW.close()
    list0 = TL1_Strip('RTRV-ETHN++', F_HOST)
    if any((' "ETH100G-' in s for s in list0)):
        F_NOW = open(WindowsHost + '_ETH100G.csv', 'w', newline='')
        PARSE_RTRV_ETH(list0, TID, F_NOW)
        F_NOW.close()
    del list0
    list0 = TL1_Strip('RTRV-ETH10G++', F_HOST)
    if any((' "ETH10G-' in s for s in list0)):
        F_NOW = open(WindowsHost + '_ETH10G.csv', 'w', newline='')
        PARSE_RTRV_ETH(list0, TID, F_NOW)
        F_NOW.close()
    del list0
    list0 = TL1_Strip('RTRV-WAN++', F_HOST)
    if any(('MAPPING' in s for s in list0)):
        F_NOW = open(WindowsHost + '_WAN.csv', 'w', newline='')
        PARSE_RTRV_WAN(list0, TID, F_NOW)
        F_NOW.close()
    del list0
    list1 = TL1_Strip('RTRV-FLEX++', F_HOST)
    if any(('PROTOCOL' in s for s in list1)):
        F_NOW = open(WindowsHost + '_FLEX.csv', 'w', newline='')
        PARSE_RTRV_FLEX(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-CRS-ALL', F_HOST)
    if any(('PRIME' in s for s in list1)):
        F_NOW = open(WindowsHost + '_CRS.csv', 'w')
        PARSE_RTRV_CRS_ALL(list1, TID, dCTYPE, F_NOW)
        F_NOW.close()
    del list1
    list1 = TL1_Strip('RTRV-CRS-ODU::ALL:QODU;', F_HOST)
    list1 += TL1_Strip('RTRV-CRS-ODUCTP::ALL:QODUCT', F_HOST)
    if any(('CKTID=' in s for s in list1)):
        F_NOW = open(WindowsHost + '_CRS_ODUx.csv', 'w', newline='')
        PARSE_RTRV_CRS_ODUx(list1, TID, dCTYPE, dRATE__AID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-SONET++', F_HOST)
    list2 = TL1_Strip('RTRV-LLSDCC:::QDCC;', F_HOST)
    list3 = TL1_Strip('RTRV-IISIS-CIRCUIT:::QTL;', F_HOST)
    list4 = list1 + list2 + list3
    for i in list1:
        if i.find(':CV-') > -1 and i.find('OSC:') < 0:
            F_NOW = open(WindowsHost + '_SONET.csv', 'w', newline='')
            PARSE_RTRV_SONET(list4, TID, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('RTRV-DS3++', F_HOST)
    for i in list1:
        if i.find('"DS3-') > -1 and i.find(',FMT=') < 0:
            F_NOW = open(WindowsHost + '_DS3.csv', 'w')
            PARSE_RTRV_DS3(list1, TID, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('RTRV-DS1++', F_HOST)
    for i in list1:
        if i.find('EQLZ') > -1:
            F_NOW = open(WindowsHost + '_DS1.csv', 'w')
            PARSE_RTRV_DS1(list1, TID, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('PROTECTION-OTM++', F_HOST)
    for i in list1:
        if i.find('REMSTANDARD') > -1 or i.find('SWSTATUS') > -1:
            F_NOW = open(WindowsHost + '_OTM_Protection.csv', 'w')
            PARSE_PROTECTION_OTM(list1, TID, d_EQUIPMENT_STATE__AID, d_FACILITY_STATE__AID, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('RTRV-SYNCH++', F_HOST)
    F_NOW = open(WindowsHost + '_Synch.csv', 'w', newline='')
    PARSE_SYNCHRONIZATION(list1, TID, lSHELF_ID, lSHELF_XC, sysNEMODE, F_NOW)
    F_NOW.close()
    list1 = TL1_Strip('RTRV-NODES:::QTL;', F_HOST)
    for i in list1:
        if i.find('REMOTESHELF=') > -1:
            F_NOW = open(WindowsHost + '_OSPF_Nodes.csv', 'w')
            list1 += TL1_Strip('RTRV-IPxRTG-TBL++', F_HOST)
            PARSE_RTRV_NODES(list1, TID, F_NOW)
            F_NOW.close()
            break

    list2 = TL1_Strip('RTRV-IISIS-ROUTER:::QTL;', F_HOST)
    list2 += TL1_Strip('RTRV-IISIS-RDENTRY:::QTL;', F_HOST)
    list2 += TL1_Strip('RTRV-IISIS-CIRCUIT:::QTL;', F_HOST)
    if any(('CKTDEFMETRIC' in s for s in list2)):
        F_NOW = open(WindowsHost + '_IISIS.csv', 'w', newline='')
        PARSE_IISIS(list2, TID, F_NOW)
        F_NOW.close()
    del list2
    list2 = TL1_Strip('RTRV-IPFILTER++', F_HOST)
    for i in list2:
        if i.find('ACTION') > -1:
            F_NOW = open(WindowsHost + '_IP_FILTER.csv', 'w')
            PARSE_RTRV_IPFILTER(list2, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('SECU-USER++', F_HOST)
    F_NOW = open(WindowsHost + '_Active_Users.csv', 'w')
    PARSE_RTRV_SECU_USER(list1, F_NOW)
    F_NOW.close()
    list1 = TL1_Strip('SECU-RULES++', F_HOST)
    F_NOW = open(WindowsHost + '_Security_Rules.csv', 'w')
    PARSE_RTRV_SECU_RULES(list1, F_NOW)
    F_NOW.close()
    list2 = TL1_Strip('RADIUS++', F_HOST)
    for i in list2:
        if i.find(',PORT=') > -1:
            F_NOW = open(WindowsHost + '_RADIUS.csv', 'w')
            PARSE_RADIUS(list2, F_NOW)
            F_NOW.close()
            break

    list1 = TL1_Strip('RTRV-SPLI:::QSPLI;', F_HOST)
    if any(('PLATFORMTYPE' in s for s in list1)):
        F_NOW = open(WindowsHost + '_SPLI.csv', 'w')
        PARSE_RTRV_SPLI(list1, TID, F_NOW)
        F_NOW.close()
    list1 = TL1_Strip('RTRV-ALMPROFILE-DFLT:::ALPR::AMP:;', F_HOST)
    if any(('PLATFORMTYPE' in s for s in list1)):
        F_NOW = open(WindowsHost + '_SPLI.csv', 'w')
        PARSE_RTRV_SPLI(list1, TID, F_NOW)
        F_NOW.close()
    f1 = WindowsHost + '_Alarms.csv'
    F_IN = open(WindowsHost + '_Alarm.txt', 'r')
    PARSE_RTRV_COND(TID, f1, F_IN)
    F_IN.close()
    F_HOST.close()
    F_ERROR.close()
    F_DBG.write('\n\nMerging csv...\n')
    CONSOLIDATE_CSV(TimeStamp, WindowsHost, TID, OSRP_NODENAME, OSRP_NODEID, OSRP_NODEID_HEX, OSRP_NODEIP, OSRP_TYPE)
    F_DBG.write('\n\n%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% End of %s %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% \n' % HOST)
    F_HOST.close()
    print (HOST + ' done\n')
    return ErrorMessage


def main():
    global missingTID
    global REPORT
    if COLLECT == 'YES':
        if PLATFORM_MODE == 'RLS':
            wasConnected = RLS_LOGIN_SSH()
            if wasConnected == 'YES':
                print('Running 6500 RLS smoke test command bundle...')
                try:
                    sErr = RLS_SMOKE_TEST(WindowsHostName)
                    if sErr == '':
                        sErr = PARSE_COLLECTED_DATA_RLS(WindowsHostName)
                    if sErr != '':
                        missingTID = 'YES'
                        print('RLS smoke test encountered an error')
                finally:
                    RLS_LOGOUT_SSH()
            else:
                title = 'Login issue, script aborted'
                WARNING(HOST, title)
                missingTID = 'YES'
                print('Could not connect to the RLS device')
        else:
            mcemonPort = '8888'
            wasConnected = LOGIN_TELNET()
            if wasConnected == 'YES':
                mcemon = MCEMON_STATUS(mcemonPort)
                sErr = COLLECT_DATA(mcemon, WindowsHostName)
                if sErr != '':
                    missingTID = 'YES'
                    print ('Could extract TID name')
                elif PartnerPC == 'YES':
                    platform_family = DETECT_6500_VARIANT(WindowsHostName)
                    print('Detected platform family: ' + platform_family)
                    sErr = PARSE_COLLECTED_DATA(WindowsHostName)
                    if sErr != '':
                        title = 'Login issue, script aborted'
                        WARNING(HOST, title)
                        missingTID = 'YES'
                        print ('Could extract TID name')
            else:
                title = 'Login issue, script aborted'
                WARNING(HOST, title)
                missingTID = 'YES'
                print ('Could not connect to the TID')
    F_DBG.close()
    REPORT = 'YES'
    f1 = 'Testers Diagnostic Script ' + SCRIPT_VERSION
    if missingTID == 'NO':
        WARNING(f1, '   Finished succesfully          ')
    else:
        WARNING(f1, 'See MissingTID.txt for more information')

if __name__ == '__main__':
    main()
