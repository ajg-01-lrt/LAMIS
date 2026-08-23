#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Author : Andrew Bourne
# Contributor : Zackery Simino
# Ciena RLS Audit tool
#

__author__ = 'Andrew Bourne'		# v1.4.1 — improved card type handling
__email__ = 'agbourne@apple.com'
__contributors__ = 'Zackery Simino'   # v1.5.0 — bidir delta feature, code quality fixes
__contributors_email__ = 'zsimino@lightriver.com'
__status__ = 'Production'	
__version__ = '1.6.0'
__program__ = 'rls_audit'

# Version notes
# 1.6.0 - Added "Internal Loss (dB)" column to Links tab: for intra-shelf patchcords
#          (same-node 'fiber' links) the measured loss is flagged red if > internal_fiber_loss_tolerance
#          (1 dB), green within. Inter-node 'line-fiber' spans stay blank.
# 1.5.0 - Added bidirectional span loss delta column to Links tab (red if > 2 dB, green if within tolerance).
#          Fixed 5 code quality issues: bare except clauses now log to Debug.txt; shell injection in
#          adb_asset() fixed (shell=False); local timeout variable shadowing global in get_data() renamed
#          to req_timeout; mixed tabs/spaces indentation in resolve_card_type() and safe_extract()
#          normalized to tabs; check_mark/x_mark moved to module-level globals to prevent NameError.
# 1.4.1 - Added better handling of card types and system types.


# if TLS certification fails (and you know it has been done) run this command
# python3 -m pip install recertifi apple-certifi -i https://pypi.apple.com/simple/

import requests
import argparse
import getpass
import sys
import subprocess
import keyring
import json
import socket
import threading
import xlsxwriter
from xlsxwriter.utility import xl_rowcol_to_cell
import datetime
import os
import re
import traceback
from collections import OrderedDict
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

### Variables ###
timeout_query = 10
extended_timeout_query = 120
# Alarm-history over the seed-curl jump path routinely blows the normal
# ``timeout_query`` budget and returns nothing (curl exit=28 -> no HTTP
# response): the unbounded ``=*`` query asks the node to serialize its
# entire history, which it can't do inside the budget. Give the call a
# short, dedicated timeout so a stall fails fast instead of holding up the
# whole walk. ``ALARM_HISTORY_QUERY`` is isolated here so a verified
# bounded query (recent-N / time-window) can be dropped in as a one-liner.
alarm_history_timeout = 5
ALARM_HISTORY_QUERY = 'restconf/data/ciena-pro-alarm:alarm-history=*'
# Concurrency: number of SSH shells opened to the seed for parallel
# neighbor collection. 1 = serial (historical behavior, single shell).
# Raise (3-5) to fan node collection out across multiple shells so the
# per-node curl sequences overlap; capped in practice by the seed's
# concurrent-session limit. Persistent default lives in
# ``config.RLS_AUDIT_SSH_POOL_SIZE``; the ``RLS_AUDIT_SSH_POOL`` env var
# overrides it per run when set (e.g. ``=1`` to force a one-off serial run).
try:
	import config as _cfg
	_default_pool = int(getattr(_cfg, "RLS_AUDIT_SSH_POOL_SIZE", 1))
except Exception:
	_default_pool = 1
try:
	SSH_POOL_SIZE = max(1, int(os.environ.get("RLS_AUDIT_SSH_POOL", _default_pool)))
except (TypeError, ValueError):
	SSH_POOL_SIZE = 1
gain_difference = 0.5
tilt_difference = 0.5
# Different loss values for LH and Metro systems
loss_table = {'LH':{'assumed_fiber_loss':0.22 , 'assumed_connector_loss':1.0},
			  'Metro':{'assumed_fiber_loss':0.30 , 'assumed_connector_loss':2.0}}
approved_sw_versions = ('RLS-04.00.01.5093','RLS-03.03.00.0393')
previous_approved_sw_versions = ('RLS-04.00.00.6067',)
measured_expected_loss_diff = 1.0
min_ccmd_power_level = -8.0 # dBm

resp_codes = {400:'Request is malformed',
			  401:'Unauthorized access',
			  403:'The request was legal, but the server refuses to process',
			  404:'Requested resource not found',
			  405:'Requested operation not supported',
			  406:'The requested resource cannot generate the required content',
			  409:'Request could not be completed due to conflict with current state of target node',
			  415:'Request media type is not supported'}
orl_state_lookup = {'valid':'The ORL value is valid',
					'outputOORL':'Output power too low to report reliable ORL',
					'reflectOORL':'Reflected power too low to report reliable ORL',
					'reflectOORH':'Reflected power too high to report reliable ORL',
					'hssf':'possible Hardware Subsystem Failure',
					'shutoff':'The amplifier is in a shutoff state',
					'unknown':'No ORL state is reported',
					'notApplicable':'The ORL state is not applicable'}
keychain_item = 'ciena_pass'
timeout = 3
max_alarms_to_display = 5
systemrow = 2
linkrow = 2 # min is 2
amprow = 2
cctrow = 2
clientrow = 2
invrow = 2
alarmrow = 2
afcrow = 2
hxrow = 2
max_orl = 30.0
min_osc_rx_power = -30
now = datetime.datetime.now().replace(microsecond=0)
dt_string = now.strftime("%b %d %Y %H:%M")
fill_type1 = 'FDE9D9'
fill_type2 = 'DAEEF3'
alarm_delimiters = '[' , ']' , ':' , '/ciena-6500r-' , 'name='
regex_pattern = '|'.join(map(re.escape, alarm_delimiters))
alarm_cleanup = {'/config/amps':'amp' , 'slots':'slot' , '/config/':'' , '/config/optmons':'optmon' , '/config/dgff':'dgff' , '/fac/amps':'amp'}
diag_search_keys = ('failed','hardware-subsystem-fail','software-subsystem-fail','hi-temp-warning','hi-temp','secure-erase-in-progress','secure-erase-failed')
LH_cards = ('RLA','DLE')
Metro_cards = ('DLM')
adb_cards = ('DLM','DLE','RLA','CMD','CCM','LRU','SRA')
ctm_slots = ['41' , '42']
osc_sfp_slots = [50 , 60] # slot-1 sub-slots holding the OSC SFPs (slots 1/inventory/slots=50|60/inventory/circuit-pack) -- added to the Inventory tab
span_loss_difference = 1.5
bidir_loss_tolerance = 2.0
internal_fiber_loss_tolerance = 1.0 # dB - intra-shelf 'fiber' (same-node) patchcord loss above this is flagged
add_info_col_width = 15
orl_cell_width = 5
lh_network = False
cct_nwid_pattern = r"network-id='([^']+)'"
cct_name_pattern = r"mc\[name='([^']+)'\]"
check_mark = '✓'
x_mark = '✗'

card_lookup = {'24 Ch Mux/Demux (CMD24) 200 GHz C-Band':'CMD24 C-Band',
			   '24 Ch Mux/Demux (CMD24) 200 GHz L-Band':'CMD24 L-Band',
			   '42 Channel Mux/Demux (CMD42) 112.5 GHz C-Band Module':'CMD42 C-Band Module',
			   '42 Channel Mux/Demux (CMD42) 112.5 GHz L-Band Module':'CMD42 L-Band Module',
			   'RLA 12x1 C&L-Band 1xSFP':'RLA 12x1 C&L-Band',
			   'RLA 12x1 C-Band 1xSFP':'RLA 12x1 C-Band',
			   'DLE C+L-Band 2xSFP':'DLE C+L-Band',
			   'DLE C-Band 2xSFP':'DLE C-Band',
			   'DLM C+L-Band 2xSFP':'DLM C+L-Band',
			   'DLM C-Band 2xSFP':'DLM C-Band'
			   }
system_lookup = {'RLA 12x1 C&L-Band':'LH ROADM C + L' ,
				 'RLA 12x1 C-Band':'LH ROADM C Only' ,
				 'RLA 64x1 iC&L-Band':'LH ROADM C + L' ,
				 'DLE C+L-Band':'LH ILA C + L' ,
				 'DLE C-Band':'LH ILA C Only' ,
				 'DLM C+L-Band':'Metro',
				 'DLM C-Band':'Metro'
				 }
port_mapping = {'RLA':
				{21:'SW1 Out',22:'SW1 In',
				23:'SW2 Out',24:'SW2 In',
				25:'SW3 Out',26:'SW3 In',
				27:'SW4 Out',28:'SW4 In',
				29:'SW5 Out',30:'SW5 In',
				31:'SW6 Out',32:'SW6 In',
				33:'SW7 Out',34:'SW7 In',
				35:'SW8 Out',36:'SW8 In',
				37:'SW9 Out',38:'SW9 In',
				39:'SW10 Out',40:'SW10 In',
				41:'SW11 Out',42:'SW11 In',
				43:'SW12 Out',44:'SW12 In',
				53:'Line Out',54:'Line In',
				51:'OSC Line Rx',55:'OSC Line Tx',
				11:'L Band UPG Out',12:'L Band UPG In',
				10:'L Band Mon Out',
				100:'Line Out Booster C Band Out',101:'Line Out Pre-Amp C Band In',
				105:'Line Out C/L Splitter',
				200:'Line In Pre-Amp C band Out',201:'Line In C/L Filter',
				300:'Booster Lband Out',301:'Booster L band In',
				},
				'DLE':
				{53:'Line Out (E)',54:'Line In (W)',
				63:'Line Out (W)',64:'Line In (E)',
				101:'Line-2-to-line-1-CL_in',105:'Line-2-to-line-1-CL_out',
				201:'Line-1-to-line-2-CL_in',205:'Line-1-to-line-2-CL_out',
				100:'Line-2-to-line-1-C_band_amp_out',200:'Line-1-to-line-2-C_band_amp_out',
				300:'Line-2-to-line-1-L_band_amp_out',400:'Line-1-to-line-2-L_band_amp_out',
				},
				'CCMD16':
				{101:'Common Out',102:'Common In'},
				'DLM':
				{53:'Line 1 Out',54:'Line 1 In',
				63:'Line 2 Out',64:'Line 2 In'},
				'CMD42':
				{85:'Common In',86:'Common Out'},
				'CMD24':
				{49:'Common In',50:'Common Out'},
				'LRU':
				{21:'SW1 Out',22:'SW1 In',
				23:'SW2 Out',24:'SW2 In',
				25:'SW3 Out',26:'SW3 In',
				27:'SW4 Out',28:'SW4 In',
				29:'SW5 Out',30:'SW5 In',
				31:'SW6 Out',32:'SW6 In',
				33:'SW7 Out',34:'SW7 In',
				35:'SW8 Out',36:'SW8 In',
				37:'SW9 Out',38:'SW9 In',
				39:'SW10 Out',40:'SW10 In',
				41:'SW11 Out',42:'SW11 In',
				43:'SW12 Out',44:'SW12 In',
				51:'L Band UPG Out',52:'L Band UPG In',
	 			50:'L Band Mon In',
				400:'L Band Pre Amp Out',},
				'SRA':
				{6:'Line In',5:'Line Out',
	 			3:'Express Out',4:'Express In',}
				}
resetc = '\x1b[0m'  # reset colour back to default
red = '\x1b[0;31m'
amber = '\x1b[0;33m'
green = '\x1b[0;92m'
yellow = '\x1b[0;93m'
blue = '\x1b[0;94m'
colour_for_system_type = {'Metro':blue,'LH ROADM C + L':green,'LH ILA C + L':green,'LH ROADM C Only':green,'LH ILA C Only':green,'Mux only':amber}

debug_log_file = 'Debug.txt'

def debug_log(exc, context=''):
	"""Append exception details to Debug.txt."""
	with open(debug_log_file, 'a') as _f:
		_f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
		if context:
			_f.write(' [' + context + ']')
		_f.write('\n')
		_f.write(traceback.format_exc())
		_f.write('\n')

# --- Run-from-ATLAS entry point --------------------------------------------
# This module was the stand-alone ``rls_audit_updated`` CLI script (Andrew
# Bourne / Apple, contributions Zackery Simino). The CLI/wizard front-end
# has been replaced with a ``run_audit(...)`` entry point so the ATLAS
# Diagnostics > Network Audit screen can drive it directly. Helpers and
# REST flow are verbatim except for:
#   * ANSI color tokens (blue/green/red/...) dropped from print arg lists
#     since the GUI panel can't render them.
#   * ``print(...)`` -> ``log_callback(...)``.
#   * ``sys.exit(...)`` -> ``_audit_abort(...)`` (raises RuntimeError so
#     the caller frame can surface a clean error to the operator).
# ---------------------------------------------------------------------------
from types import SimpleNamespace

# ANSI color tokens kept for parity with the CLI helpers (debug_log /
# login_to_node still reference them in log lines). The GUI log_callback
# path strips them; standalone use gets normal terminal output.
blue = "\033[94m"
green = "\033[92m"
red = "\033[91m"
amber = "\033[93m"
yellow = "\033[93m"
resetc = "\033[0m"


def _audit_abort(log_callback, *args):
	"""Used in place of ``sys.exit`` inside ``run_audit`` -- log + raise."""
	msg = " ".join(str(a) for a in args) if args else "Audit aborted."
	log_callback(msg)
	# Close any open SSH tunnel before bailing so the daemon listener
	# threads + paramiko transport don't outlive the audit run.
	# Idempotent (close() is guarded) so this is safe even if the
	# tunnel was never opened.
	global _jump_tunnel
	if _jump_tunnel is not None:
		try:
			_jump_tunnel.close()
		except Exception:
			pass
		_jump_tunnel = None
	raise RuntimeError(msg)


# Module-level state the helpers read at call time. ``run_audit``
# reassigns every one of these via the ``global`` declaration at
# function entry; safe defaults here so the module imports cleanly
# and any helper that runs outside a real audit (tests, REPL
# exploration) doesn't NameError.
workbook = None
session = None
network_inventory = {}
format_green = None
format_red = None
format_green_left = None
format_red_left = None
format_lookup = {}
# Left-align cell format; created per-workbook in ``run_audit`` and read
# by the module-level ``formatted`` helper (rich-string padding fragment).
left = None

# SSH-via-seed state. RLS field deployments are typically reached via
# the seed's factory-default ``10.0.0.1`` management port; the
# workstation has no route to neighbor IPs (``10.6.22.X`` etc.). The
# obvious fix (``ssh -L`` port-forward via paramiko ``direct-tcpip``)
# is administratively prohibited by Ciena's sshd config -- so instead
# we open a persistent shell on the seed and run ``curl`` from there.
# The seed itself still talks REST directly via ``requests`` -- it's
# reachable from the workstation by definition.
_jump_tunnel = None       # ``SshSeedSession`` (or None when not in use).
_seed_direct_host = None  # host the audit reaches via ``requests``
                          # without going through the SSH session.
_tunnel_open_attempted = False  # so the auto-open path runs once
                                # per audit run, not per REST call.
_tunnel_ssh_creds = None  # (user, pw) used to open the SSH session;
                          # set by run_audit before the first call.


def _try_open_jump_tunnel():
	"""Open the SSH session on first use. Called lazily from
	``_audit_request`` so an audit that touches only the seed
	doesn't pay the SSH-handshake cost. Logs success/failure; on
	failure the audit falls back to direct REST calls (which time
	out for unreachable neighbors -- best-effort, per operator)."""
	global _jump_tunnel, _tunnel_open_attempted
	if _tunnel_open_attempted:
		return
	_tunnel_open_attempted = True
	if not _seed_direct_host or not _tunnel_ssh_creds:
		return
	user, pw = _tunnel_ssh_creds
	log_callback(
		f"[SSH-CURL] Opening shell on {_seed_direct_host} (user={user})..."
	)
	try:
		from utils.ssh_tunnel import SshSeedSession
		session_obj = SshSeedSession(_seed_direct_host, user, pw)
		session_obj.open()
	except Exception as exc:
		log_callback(
			f"[SSH-CURL] Could not open shell: {exc}."
			" Neighbor REST will be tried direct (and likely time out)."
		)
		_jump_tunnel = None
		return
	_jump_tunnel = session_obj
	log_callback(
		f"[SSH-CURL] Shell ready -- neighbor REST will run as ``curl`` "
		f"on {_seed_direct_host}."
	)


# ---- Per-worker request context (concurrency) --------------------------
# When the audit fans node collection out across worker threads, each
# worker needs its OWN ``requests.Session`` and its OWN leased SSH shell:
# the module-level ``session`` clears its cookie jar on every neighbor
# login (see ``_mirror_cookies_to_session``), and one ``curl`` shell is a
# single serial byte stream. A worker stashes its per-thread session/tunnel
# on ``_worker_ctx``; the accessors fall back to the module globals when
# nothing is set, so the serial path stays byte-for-byte unchanged.
_worker_ctx = threading.local()


def _current_session():
	"""The calling thread's ``requests.Session``, or the module global
	when no per-worker session is set (serial path)."""
	return getattr(_worker_ctx, "session", None) or session


def _current_tunnel():
	"""The calling thread's leased SSH shell, or ``None`` -- meaning the
	caller should use the shared, lazily-opened ``_jump_tunnel`` (serial
	path)."""
	return getattr(_worker_ctx, "tunnel", None)


class _WalkQueue:
	"""Dedup'ing BFS work queue for the node walk. Replaces the old
	pattern of iterating a ``hostlist`` grown in place; ``self.order`` IS
	that list -- the same object the historical ``hostlist.index`` /
	``len(hostlist)`` call sites use -- so those keep working unchanged.

	Thread-safe (a lock guards every mutation) so the concurrent collector
	can dequeue and enqueue-on-discovery from multiple workers, but driven
	by a single thread it reproduces the old loop exactly.

	Dedup mirrors the original inline logic: a candidate is skipped if its
	identifier OR short-name already appears among queued nodes, OR among
	the aliases of nodes already walked (the seed is the classic case --
	queued as ``10.0.0.1`` but also walked under its operational IP)."""

	def __init__(self, seed):
		self._lock = threading.Lock()
		self.order = []             # discovery order == the old ``hostlist``
		self._order_keys = set()    # lowercased identifiers already queued
		self._order_shorts = set()  # lowercased short-names already queued
		self._walked = set()        # identifiers/aliases already walked
		self._pos = 0               # next index in ``order`` to hand out
		self._enqueue(seed)

	def _enqueue(self, candidate):
		self.order.append(candidate)
		self._order_keys.add(candidate.lower())
		self._order_shorts.add(format_hostname(candidate)[0].lower())

	def add_candidate(self, candidate, short_nm=''):
		"""Dedup then enqueue ``candidate``. Returns True if newly queued."""
		with self._lock:
			c = candidate.lower()
			if c in self._order_keys:
				return False
			if short_nm and short_nm.lower() in self._order_shorts:
				return False
			if c in self._walked:
				return False
			if short_nm and short_nm.lower() in self._walked:
				return False
			self._enqueue(candidate)
			return True

	def mark_walked(self, *names):
		with self._lock:
			for n in names:
				if n:
					self._walked.add(n.lower())

	def is_walked(self, name):
		with self._lock:
			return bool(name) and name.lower() in self._walked

	def next(self):
		"""Return the next not-yet-handed-out node in discovery order, or
		``None`` when the queue is drained."""
		with self._lock:
			if self._pos >= len(self.order):
				return None
			host = self.order[self._pos]
			self._pos += 1
			return host

	def __len__(self):
		return len(self.order)


# Per-node fields the writer consumes. ``_collect_node`` fetches + parses a
# node and packs these into a plain record; ``_write_record`` unpacks them
# and emits the worksheet rows. Keeping the list in one place guarantees the
# pack and the unpack stay in lock-step.
_RECORD_FIELDS = (
	'host', 'short_node_name', 'login_valid', 'check_tls',
	'reported_node_name', 'domain', 'system_type', 'alarm_free',
	'shelf_type', 'serial_number', 'mac_address', 'swversion',
	'ctm_status', 'license', 'location', 'powerconsumption',
	'site_address', 'site_lat', 'site_lon', 'active_features',
	'sw_active', 'sw_running', 'sw_committed', 'sw_upgrade_state',
	'sw_delivered', 'disconnected_neighbors', 'neighbor_sw_versions',
	'shelf_hw', 'hw_release',
	'alarm_list', 'cct_db', 'afc_list',
	'section_list', 'alarm_history_list', 'amp_list', 'inv_list',
)


def _pack_record(local_vars):
	"""Snapshot the writer-facing fields from a collector's ``locals()``.
	A field a partial fetch never set simply isn't captured -- the writer
	supplies a safe default via ``rec.get(...)``."""
	return {k: local_vars[k] for k in _RECORD_FIELDS if k in local_vars}


def _mirror_cookies_to_session(response):
	"""When a REST call lands through the SSH-curl path, the response
	object is a ``ResponseLike`` whose cookies aren't visible to
	``requests.Session.cookies.get_dict()``. The audit's call sites
	read cookies from ``session.cookies`` after every login, so copy
	them across to keep behavior identical to the direct path.

	Clears the jar first so cookies from a PREVIOUS host's login
	don't pollute ``session.cookies.get_dict()``. Real-world bite:
	RLS uses the cookie name ``-http-session-`` on every node, and
	the seed's cookie (domain=10.0.0.1) and the neighbor's mirrored
	cookie (domain="" from a no-domain ``set()``) coexist in the
	jar with the same name. ``get_dict()`` then returns whichever
	the CookieJar happens to iterate last -- usually the seed's --
	so subsequent GETs to the neighbor present the wrong session
	cookie and get rejected.
	"""
	sess = _current_session()
	if sess is None:
		return
	cookies = getattr(response, "cookies", None)
	if not cookies:
		return
	# A real ``requests.Response.cookies`` is a CookieJar -- skip in
	# that case since the session already has the cookies attached.
	if not isinstance(cookies, dict):
		return
	# Wipe stale cookies from any previous host's session before
	# planting this host's. Safe because the audit's call sites
	# capture cookies into a local dict immediately after this
	# function returns -- clearing the jar later doesn't invalidate
	# the snapshot they're using. Under concurrency ``sess`` is the
	# worker's OWN session, so clearing it can't disturb another node.
	try:
		sess.cookies.clear()
	except Exception:
		pass
	for name, value in cookies.items():
		try:
			sess.cookies.set(name, value)
		except Exception:
			pass


def _audit_request(method, host, cmd, *, data=None, headers=None,
                   cookies=None, verify=True, timeout=10):
	"""Unified REST request dispatcher: ``requests`` for the seed,
	``curl``-via-SSH-shell for everything else. Returns a Response-
	like object with ``.status_code``, ``.content``, ``.cookies``."""
	is_seed = (not host) or (host == _seed_direct_host)
	if not is_seed:
		# Per-worker leased shell when the audit is fanning out; else
		# the shared, lazily-opened tunnel (serial path).
		tunnel = _current_tunnel()
		if tunnel is None:
			if _jump_tunnel is None:
				_try_open_jump_tunnel()
			tunnel = _jump_tunnel
		if tunnel is not None and tunnel.is_open:
			try:
				return tunnel.http_request(
					method, host, cmd,
					data=data, headers=headers, cookies=cookies,
					verify=verify, timeout=timeout,
				)
			except Exception as exc:
				log_callback(
					f"[SSH-CURL] {method} https://{host}/{cmd} failed:"
					f" {exc}; falling back to direct call"
				)
		# Fall through to a direct requests call -- this will likely
		# time out for unreachable neighbors but matches the best-
		# effort behavior the operator chose.
	addr = f"https://{host}/{cmd}"
	if method.upper() == "GET":
		return requests.get(
			addr, cookies=cookies, verify=verify, timeout=timeout,
		)
	# POST (login + logout)
	return _current_session().post(
		addr, data=data, headers=headers, cookies=cookies,
		verify=verify, timeout=timeout,
	)

# Module-level ``log_callback`` so helper functions defined at this
# scope (``login_to_node``, ``get_data``, ``alarm_colour``, etc.) can
# call it without taking it as a parameter. ``run_audit`` overrides
# this with its print-compatible wrapper at function entry. Default
# is ``print`` so the module is usable from a REPL / standalone test.
def log_callback(*args, **kwargs):
	"""Default module-level sink: behave like ``print``. Overwritten
	by ``run_audit`` so calls inside helpers route to the GUI panel."""
	print(*args, **kwargs)

# ``options`` namespace the original CLI populated via argparse.
# Helpers (``get_data``, alarm collectors, etc.) reference fields
# like ``options.debug`` and ``options.alarms``; ``run_audit``
# reassigns it at function entry.
options = SimpleNamespace(
	username="",
	password="",
	use_keychain=False,
	nodes=None,
	seedfile=None,
	outfile=None,
	alarms=False,
	alarm_limit=None,
	alarm_history=False,
	discover=True,
	openexcel=False,
	assumed_fiber_loss=None,
	assumed_connector_loss=None,
	debug=False,
	dump_file=None,
)


def _summarize_afc_container(obj):
	"""Summarize an AFC container dict as 'N channels' (encrypted measurement data)."""
	if not obj or not isinstance(obj, dict):
		return ''
	for value in obj.values():
		if isinstance(value, list):
			n = len(value)
			return f'{n} channel{"s" if n != 1 else ""}'
	return 'Populated'
def login_to_node(host,cmd,payld,hdr):
	try:
		try:
			r = _audit_request(
				"POST", host, cmd,
				data=payld, headers=hdr, verify=True, timeout=timeout,
			)
			tls = True
		except Exception as e:
			debug_log(e, 'login_to_node TLS fallback')
			r = _audit_request(
				"POST", host, cmd,
				data=payld, headers=hdr, verify=False, timeout=timeout,
			)
			tls = False
		# When the call went via the SSH-curl path the cookies live on
		# the response object; mirror them into ``session.cookies`` so
		# the call site's ``session.cookies.get_dict()`` continues to
		# see them (existing audit contract).
		_mirror_cookies_to_session(r)
		if r.status_code >= 200 and r.status_code <= 206:
			return(r.content , True , tls)
		else:
			if r.status_code in resp_codes:
				log_callback(r.status_code , ':' , resp_codes[r.status_code])
				log_callback(cmd)
			else:
				log_callback(r.content , ': Unknown reason')
			return(r.status_code, False , True)
	except requests.Timeout:
		return ('Request timeout' , False , False)
	except requests.ConnectionError:
		return ('ConnectionError' , False , False)
def logout_of_node(host,cookies):
	try:
		_audit_request(
			"POST", host, "",
			headers={'Connection': 'close'},
			cookies=cookies, verify=False, timeout=timeout,
		)
	except Exception:
		log_callback('Caution: did not log out of node correctly')
def get_data(host , cmd , cookies, req_timeout=None):
	# ``req_timeout`` lets a caller override the per-call budget (e.g. the
	# fail-fast alarm-history fetch). When unset, keep the original policy.
	if req_timeout is None:
		if cmd == 'restconf/data': # increase timeout if getting all data
			req_timeout = extended_timeout_query
		else:
			req_timeout = timeout_query
	r = _audit_request(
		"GET", host, cmd,
		cookies=cookies, verify=False, timeout=req_timeout,
	)
	if r.status_code >= 200 and r.status_code <= 206:
		return(r.content , True)
	# Surface non-success status unconditionally (the original CLI
	# gated this behind options.debug, which made silent failures
	# undebuggable from the log file). Use a compact format so a
	# 50-call sequence doesn't drown the panel.
	_label = resp_codes.get(r.status_code, "?") if r.status_code else "no response"
	log_callback(f"  [{host}] GET {cmd} -> {r.status_code} {_label}")
	return(r.status_code, False)
def adb_asset(serial):
	query_args = ['adb', 'assets', 'serialNum=' + serial, '-f', 'assetTag']
	try:
		adb_query = subprocess.run(query_args , shell = False , capture_output = True, text = True)
		asset = adb_query.stdout.splitlines()
		if len(asset) > 1:
			return(asset[1])
		else:
			return('')
	except FileNotFoundError:
		return('')  # adb tool not available on this platform
def format_hostname(device):
	if 'corp.apple.com' in device:
		short = device.split('.',1)[0].lower()
		fqdn = device.lower()
	elif 'bb.net.apple.com' in device:
		short = device.split('.',1)[0].lower()
		fqdn = device.lower()
	else:
		# IP address or non-Apple hostname — use as-is
		short = device.lower()
		fqdn = device.lower()
	return(short , fqdn)
def link_diag(ld):
	diags_list = []
	for cond in ld:
		if ld[cond]:
			diags_list.append(cond)
	result = ('\n'.join(map(str,diags_list)))
	if result == '':
		result = 'Good'
	return(result)
def alarm_colour(alarm_type):
	if alarm_type == 'Critical':
		log_callback(end = '')
	if alarm_type == 'major':
		log_callback(end = '')
	if alarm_type == 'minor':
		log_callback(end = '')
def clean_alarm(noisy_alarm):
	cleaned_alarm = ''
	# Strip first 2 items on alarm
	alarm_item = re.split(regex_pattern,noisy_alarm)[2:]
	for alarm_component in alarm_item:
		if alarm_component in alarm_cleanup:
			d = str(alarm_component).replace(alarm_component , alarm_cleanup[alarm_component])
		else:
			d = str(alarm_component)
		# Add component to alarm string
		if d != "":
			cleaned_alarm = cleaned_alarm + d + ' '
	cleaned_alarm = str(cleaned_alarm).replace("'",'')
	return(cleaned_alarm)
def convert_port(node , card_port):
	card = int(card_port.split('-')[0])
	port = int(card_port.split('-')[1])
	try:
		extracted_port = port_mapping[network_inventory[node][card]][port]
	except Exception as e:
		debug_log(e, 'convert_port extracted_port')
		extracted_port = str(port)
	try:
		extracted_slot = network_inventory[node][card]
	except Exception as e:
		debug_log(e, 'convert_port extracted_slot')
		extracted_slot = str(card)
	return(extracted_slot + '-' + extracted_port)
def lookup_port(node , card , port):
	try:
		extracted_port = port_mapping[network_inventory[node][card]][port]
	except Exception as e:
		debug_log(e, 'lookup_port extracted_port')
		extracted_port = str(port)
	try:
		extracted_slot = network_inventory[node][card]
	except Exception as e:
		debug_log(e, 'lookup_port extracted_slot')
		extracted_slot = str(card)
	return(str(node) + ' ' + extracted_slot + '-' + extracted_port + ' (' + str(card) + '-' + str(port) + ')')
def formatted(cond):
	# split the string between '|' character and apply colour format based on the lookup table
	if cond == '':
		return('')
	items = [item.strip() for item in cond.split('|')]
	# Build segments list
	segments = []
	if len(items) == 1:
		return(format_lookup.get(items[0]) , items[0] , left , ' ')
	else:
		for i, part in enumerate(items):
		# Add format and text
			fmt = format_lookup.get(part)
			if fmt:
				segments.extend([fmt, part])
			# Add separator
			if i < len(items) - 1:
				segments.append(' | ')
		return(segments)
def link_connection(lnk , this_node):
	#
	li = re.findall(r"'(.*?)'", lnk)
	if len(li) == 2: # Node name node not provided in link data so add the local node
		li.insert(0 , this_node)
	remote_node ,x = format_hostname(li[0])
	slot = int(li[1])
	port = int(li[2])
	# log_callback(lnk , remote_node , slot , port)
	return(remote_node,slot,port)
def all_same(items , value):
	return all(x == value for x in items.values())
def extract(data: dict, path):
	try:
		if isinstance(path, list):
			while path:
				result = extract(data, path.pop(0))
				if result:
					return result
		shadow_data = data.copy()
		for key in path.split('.'):
			if str(key).isnumeric():
				key = int(key)
			shadow_data = shadow_data[key]
		try:
			return float(shadow_data)
		except Exception:
			return shadow_data
	except (IndexError, KeyError, AttributeError, TypeError):
		return('')
def extract_network_id(nmc_instance):
	"""Extract network-id from nmc-instance string using regex."""
	match = re.search(r"network-id='([^']+)'\]", nmc_instance)
	if match:
		return match.group(1)
	return None
def extract_slot_port(port_instance):
	"""Extract slot and port from port-instance string, returns 'slot-port' format."""
	slot_match = re.search(r"slots\[name='([^']+)'\]", port_instance)
	port_match = re.search(r"port\[name='([^']+)'\]", port_instance)

	if slot_match and port_match:
		return f"{slot_match.group(1)}-{port_match.group(1)}"
	elif port_match:
		return port_match.group(1)
	return None
def parse_ccs_json(ccs_data, host):
	"""Parse CCS instances JSON and create flat dictionary."""

	# Dictionary to store all extracted data
	network_dict = {}

	# Process all ccs-instances
	ccs_instances = ccs_data.get('ciena-6500r-channel-ctrl:ccs-instance', [])

	# First pass: Process SM (Mux) sections
	for ccs_instance in ccs_instances:
		instance_name = ccs_instance.get('name', '')

		# Check if it's a Mux (starts with SM)
		if instance_name.startswith('SM'):
			# Determine band
			band = 'L' if instance_name.endswith('-L') else 'C'

			# Get mode and additional info from state
			mode = ccs_instance.get('config', {}).get('mode', '')
			controller_state = ccs_instance.get('state', {}).get('controller-state', '')
			state_additional_info = ccs_instance.get('state', {}).get('additional-info', '')

			# Process each channel controller
			channel_controllers = ccs_instance.get('channel-controller', [])

			for channel in channel_controllers:
				# Extract network ID
				nmc_instance = channel.get('nmc-instance', '')
				network_id = extract_network_id(nmc_instance)

				if not network_id:
					continue

				# Extract input port information
				input_data = channel.get('channel-power', {}).get('input', {})
				mip = extract_slot_port(input_data.get('port-instance', ''))
				input_port = convert_port(host , mip) + '(' + mip + ')'
				input_measured = float(input_data.get('measured-power')) if input_data.get('measured-power') else '-'
				input_expected = float(input_data.get('expected-power')) if input_data.get('expected-power') else '-'

				# Extract output port information
				output_data = channel.get('channel-power', {}).get('output', {})
				mop = extract_slot_port(output_data.get('port-instance', ''))
				output_port = convert_port(host , mop) + '(' + mop + ')'
				output_measured = float(output_data.get('measured-power')) if output_data.get('measured-power') else '-'
				output_expected = float(output_data.get('expected-power')) if output_data.get('expected-power') else '-'

				# # Extract diagnostic information
				# diagnostic = channel.get('diagnostic', {})
				# target_unachievable = diagnostic.get('target-unachievable', '')
				# capacity_change_pending = diagnostic.get('capacity-change-pending', '')

				# Create flat dictionary entry for this network ID
				network_dict[network_id] = {
					'network_id': network_id,
					'band': band,
					'mux_instance': instance_name,
					'mux_operational_state': channel.get('operational-state', ''),
					'mux_controller_state': controller_state,
					'center_frequency': float(channel.get('center-frequency')) if channel.get('center-frequency') else 'No Data',
					'spectral_width': float(channel.get('spectral-width')) if channel.get('spectral-width') else 'No Data',
					'mc_spectral_width': '',
					'mc_freq_check': x_mark,
					'nmc_spectral_width': '',
					'additional_info': channel.get('additional-info', ''),
					'control_mode': channel.get('control-mode', ''),
					'ccmd_ports': '',
					'ccmd_rx_pwr': '',
					'ccmd_tx_pwr': '',
					'mux_input_port': input_port,
					'mux_output_port': output_port,
					'mux_input_measured_power': input_measured,
					'mux_input_expected_power': input_expected,
					'mux_output_measured_power': output_measured,
					'mux_output_expected_power': output_expected,
					# 'target_unachievable': target_unachievable,
					# 'capacity_change_pending': capacity_change_pending,
				}

	# Second pass: Process SD (Demux) sections and append to existing entries
	for ccs_instance in ccs_instances:
		instance_name = ccs_instance.get('name', '')

		# Check if it's a Demux (starts with SD)
		if instance_name.startswith('SD'):
			# Determine band
			band = 'L' if instance_name.endswith('-L') else 'C'

			# Get mode and controller state
			mode = ccs_instance.get('config', {}).get('mode', '')
			controller_state = ccs_instance.get('state', {}).get('controller-state', '')

			# Process each channel controller
			channel_controllers = ccs_instance.get('channel-controller', [])

			for channel in channel_controllers:
				# Extract network ID
				nmc_instance = channel.get('nmc-instance', '')
				network_id = extract_network_id(nmc_instance)

				if not network_id or network_id not in network_dict:
					continue

				# Extract input port information
				input_data = channel.get('channel-power', {}).get('input', {})
				dip = extract_slot_port(input_data.get('port-instance', ''))
				input_port = convert_port(host , dip) + '(' + dip + ')'
				input_measured = float(input_data.get('measured-power')) if input_data.get('measured-power') else '-'
				input_expected = float(input_data.get('expected-power')) if	 input_data.get('expected-power') else '-'

				# Extract output port information
				output_data = channel.get('channel-power', {}).get('output', {})
				dop = extract_slot_port(output_data.get('port-instance', ''))
				output_port = convert_port(host , dop) + '(' + dop + ')'
				output_measured = float(output_data.get('measured-power')) if output_data.get('measured-power') else '-'
				output_expected = float(output_data.get('expected-power')) if output_data.get('expected-power') else '-'

				# Extract diagnostic information for demux
				diagnostic = channel.get('diagnostic', {})
				demux_capacity_change_pending = diagnostic.get('capacity-change-pending', '')

				# Get demux operational state
				demux_operational_state = channel.get('operational-state', '')

				# Combine mux and demux operational states
				mux_operational_state = network_dict[network_id]['mux_operational_state']
				combined_operational_state = f"{mux_operational_state} | {demux_operational_state}"

				# Append demux data to the existing entry
				network_dict[network_id].update({
					'mux_instance': network_dict[network_id]['mux_instance'] + ' | ' + instance_name,
					# 'mux_mode': network_dict[network_id]['mux_mode'] + ' "" ' + mode,
					'mux_controller_state': network_dict[network_id]['mux_controller_state'] + ' | ' + controller_state,
					'mux_operational_state': combined_operational_state,
					'mux_input_port': network_dict[network_id]['mux_input_port'] + ' | ' + output_port,
					'demux_input_measured_power': input_measured,
					'demux_input_expected_power': input_expected,
					'mux_output_port': network_dict[network_id]['mux_output_port'] + ' | ' + input_port,
					'demux_output_measured_power': output_measured,
					'demux_output_expected_power': output_expected
					# 'demux_capacity_change_pending': demux_capacity_change_pending
				})

	return network_dict
def resolve_card_type(card_c_type_string: str, lookup_dict: dict) -> str:
	"""
	Resolves a card c-type string by finding the longest matching prefix
	in a lookup dictionary.

	Args:
		card_c_type_string: The c-type string to resolve (e.g., 'DLE C-Band 2xSFP 300mm').
		lookup_dict: The dictionary mapping full names to simplified names.

	Returns:
		The simplified card type if a prefix match is found,
		otherwise the original card_c_type_string.
	"""
	resolved_type = card_c_type_string # Default to the original value
	best_match_key = None
	max_key_len = 0

	for key_in_dict in lookup_dict:
		if card_c_type_string.startswith(key_in_dict):
			if len(key_in_dict) > max_key_len:
				max_key_len = len(key_in_dict)
				best_match_key = key_in_dict

	if best_match_key:
		resolved_type = lookup_dict[best_match_key]

	return resolved_type
def check_center_frequency(center_freq, min_freq, max_freq):
	"""
	Check if center frequency is correct based on min and max frequencies.
	Returns True if valid, False otherwise.
	"""
	if center_freq is None or min_freq is None or max_freq is None:
		return False
	
	try:
		center = float(center_freq)
		min_f = float(min_freq)
		max_f = float(max_freq)
		
		expected_center = (min_f + max_f) / 2
		tolerance = 0.01  # Allow small tolerance for floating point
		
		return abs(center - expected_center) < tolerance

	except (ValueError, TypeError):
		return False
def reverse_link_name(name):
	"""Reverse direction of a link name by swapping two consecutive numeric segments.
	e.g. PFG-1-2-LINEOUT -> PFG-2-1-LINEOUT"""
	parts = name.split('-')
	for i in range(len(parts) - 1):
		if parts[i].isdigit() and parts[i + 1].isdigit():
			parts[i], parts[i + 1] = parts[i + 1], parts[i]
			return '-'.join(parts)
	return None
def combine(item1 , item2):
	if item1 == item2:
		return(item1)
	if item2 == '':
		return(item1)
	else:
		return(str(item1) + ' | ' + str(item2))
def safe_extract(obj, key, default=''):
	try:
		return extract(obj, key)
	except Exception:
		return default
def format_true_False( sheet , x , y  ):
	sheet.conditional_format( x , y , x , y , {'type': 'text' , 'criteria': 'containing', 'value': 'False', 'format': format_red_left})
	sheet.conditional_format( x , y , x , y , {'type': 'text' , 'criteria': 'containing', 'value': 'TRUE', 'format': format_green_left})
def format_enable_disable(sheet , x, y):
	sheet.conditional_format( x , y , x , y , {'type': 'text' , 'criteria': 'containing', 'value': 'enabled', 'format': format_green})
	sheet.conditional_format( x , y , x , y , {'type': 'text' , 'criteria': 'containing', 'value': 'disabled', 'format': format_red})
def draw_border(worksheet , start_row , start_col , end_row , end_col):
	if start_row == end_row: # header only 1 row high
		left_corner = workbook.add_format({'left' : 1 , 'top' : 1 , 'bottom' : 1 })
		right_corner = workbook.add_format({'right' : 1 , 'top' : 1 , 'bottom' : 1 })
		center = workbook.add_format({'top' : 1 , 'bottom' : 1 })
		for c in range(start_col,end_col):
			if c == start_col:
				worksheet.conditional_format(start_row , c , start_row , c , { 'type' : 'no_errors' , 'format' : left_corner})
			elif c == end_col-1:
				worksheet.conditional_format(start_row , c , start_row , c , { 'type' : 'no_errors' , 'format' : right_corner})
			elif start_col < c < end_col-1:
				worksheet.conditional_format(start_row , c , start_row , c , { 'type' : 'no_errors' , 'format' : center})
	else:
		top_left_corner = workbook.add_format({'left' : 1 , 'top' : 1})
		top_right_corner = workbook.add_format({'right' : 1 , 'top' : 1})
		bottom_left_corner = workbook.add_format({'left' : 1 , 'bottom' : 1})
		bottom_right_corner = workbook.add_format({'right' : 1 , 'bottom' : 1})
		top = workbook.add_format({'top' : 1})
		bottom = workbook.add_format({'bottom' : 1})
		left = workbook.add_format({'left' : 1})
		right = workbook.add_format({'right' : 1})
		for c in range(start_col,end_col+1):
			for r in range(start_row,end_row+1):
				if c == start_col and r == start_row:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : top_left_corner})
				elif c == end_col-1 and r == start_row:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : top_right_corner})
				elif r == start_row and start_col < c < end_col-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : top})
				elif r == end_row-1 and start_col < c < end_col-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : bottom})
				elif c == start_col and start_row < r < end_row-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : left})
				elif c == end_col-1 and start_row < r < end_row-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : right})
				if c == start_col and r == end_row-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : bottom_left_corner})
				if c == end_col-1 and r == end_row-1:
					worksheet.conditional_format(r , c , r , c , { 'type' : 'no_errors' , 'format' : bottom_right_corner})
def run_audit(
	seed_host,
	username,
	password,
	output_path,
	*,
	capture_alarms=False,
	capture_alarm_history=False,
	debug=False,
	log_callback=None,
):
	"""Run an RLS REST-based network audit starting from *seed_host*.

	Args:
		seed_host: One IP or hostname to start the walk from. ATLAS calls
			with a single seed; the reference script's seedfile/-n options
			have been dropped -- the REST call
			``restconf/data/ciena-6500r-nodes:nodes=*`` discovers the rest
			of the topology from this entry point.
		username, password: RLS RESTCONF credentials.
		output_path: Absolute path to write the .xlsx audit report to.
		capture_alarms: When True, also collect active alarms per node.
		capture_alarm_history: When True, also collect alarm history.
		debug: When True, the audit logs verbose REST status info.
		log_callback: Function taking a single str argument. Called for
			every line the audit would have ``print()``-ed. Defaults to
			``print`` for standalone use.

	Returns:
		Number of nodes successfully queried.

	Raises:
		RuntimeError on any audit-level abort (DNS, no nodes, bad input).
	"""
	# Wrap the user-supplied callback so the ``print(a, b, c, end='\r')``
	# call sites adapted from the original CLI still work. The original
	# called ``print`` directly with multiple args + ``end='\r'`` for
	# progress carriage-returns; the regex adapter rewrote those as
	# ``log_callback(...)`` but a typical UI sink takes a single str.
	#
	# Also publish the wrapper as the module-level ``log_callback`` so
	# helper functions defined at module scope (``login_to_node``,
	# ``get_data``, ``alarm_colour``, ...) route to the same sink.
	# Without this, those helpers would still call the module-level
	# default (``print``) and the GUI panel would see nothing while
	# REST calls execute. ``globals()`` is used (not ``global ...``)
	# because the parameter name shadows the module binding.
	# Audit-output logger -- every line that goes to the GUI panel also
	# lands in the ATLAS rolling log file (``%APPDATA%\ATLAS\logs``)
	# via the root logger handler, so the operator can attach a raw
	# CLI transcript when reporting an issue. Use a dedicated logger
	# name so output stays grep-able as ``[atlas.audit]`` lines.
	import logging as _audit_logging
	_audit_logger = _audit_logging.getLogger("atlas.audit")
	# Strip ANSI color escapes when teeing to the file; the GUI panel
	# also can't render them but tolerates them. The file log gets the
	# clean form for readability.
	import re as _ansi_re
	_ANSI_RE = _ansi_re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
	_user_log_callback = log_callback or print
	def _wrapped_log_callback(*args, **kwargs):
		end = kwargs.pop("end", "\n")
		sep = kwargs.pop("sep", " ")
		flush = kwargs.pop("flush", False)  # noqa: F841 — accepted, ignored
		msg = sep.join(str(a) for a in args)
		# Strip trailing newline since most GUI panels append their own,
		# but keep ``end='\r'`` (or other custom ends) so progress-style
		# output still renders sensibly on stdout fallbacks.
		if end and end != "\n":
			msg = msg + end
		_user_log_callback(msg)
		# Tee to the file logger. Empty lines and pure whitespace get
		# skipped to keep the log compact; everything else goes through.
		try:
			clean = _ANSI_RE.sub("", msg).rstrip("\r\n")
			if clean.strip():
				_audit_logger.info(clean)
		except Exception:
			# A failed log write must never break the audit -- the GUI
			# sink already got the line, so the operator still sees it.
			pass
	globals()["log_callback"] = _wrapped_log_callback
	log_callback = _wrapped_log_callback
	# Reassign module-level state so helpers picking these up via their
	# lexical scope see this run's settings, and so per-sheet row
	# counters reset between runs. The original CLI used these as
	# module globals; the function body would otherwise treat any name
	# it assigns as local and UnboundLocalError on first read.
	global options
	global systemrow, linkrow, amprow, alarmrow, afcrow, hxrow
	global cctrow, invrow, orl_cell_width, lh_network
	# State that helper functions read by lexical scope.
	global workbook, session, network_inventory
	global format_green, format_red, format_green_left, format_red_left
	global format_lookup, left
	# Reset the per-sheet row counters to their starting values (the
	# reference initialized them once at module top; we re-initialize
	# every call so back-to-back run_audit() invocations don't
	# accumulate row positions).
	systemrow = 2
	linkrow = 2
	amprow = 2
	alarmrow = 2
	afcrow = 2
	hxrow = 2
	cctrow = 2
	invrow = 2
	orl_cell_width = 10
	lh_network = False
	# Credentials policy: REST on RLS uses ``diaguser``/``Ciena123`` --
	# the shell user ``su`` does NOT have REST API access. When the
	# caller passes blank credentials we substitute the well-known
	# defaults from the encrypted credential store. Operators can still
	# override by typing different credentials in the GUI; we just
	# don't force them to retype the factory default on every run.
	if not username or not password:
		try:
			from utils.credentials import get_default_credential_for_vendor
			_default_pair = get_default_credential_for_vendor("ciena-rls-rest")
		except Exception:
			_default_pair = None
		if _default_pair:
			username = username or _default_pair[0]
			password = password or _default_pair[1]
		else:
			# Last-ditch hardcoded fallback so the audit still runs even
			# when the encrypted store is unreachable.
			username = username or "diaguser"
			password = password or "Ciena123"
		log_callback(
			f"[AUDIT] No credentials supplied -- using RLS REST default"
			f" user '{username}'"
		)
	options = SimpleNamespace(
		username=username,
		password=password,
		use_keychain=False,
		nodes=[seed_host],
		seedfile=None,
		outfile=output_path,
		alarms=capture_alarms,
		alarm_limit=None,
		alarm_history=capture_alarm_history,
		discover=True,
		openexcel=False,
		assumed_fiber_loss=None,
		assumed_connector_loss=None,
		debug=debug,
		dump_file=None,
	)
	hostlist = [seed_host]
	# ``discover`` is the running flag the link-walk loop flips off when
	# it reaches an OL/ROADM boundary -- the original CLI kept it at
	# module level (initialized from argparse). Seed it from options so
	# the first ``if discover`` read inside the loop doesn't UnboundLocal.
	discover = options.discover

	# Prime the SSH-curl state: the seed is reachable directly;
	# every other host gets curl'd from the seed's bash (paramiko
	# ``invoke_shell`` -> CLI ``shell`` keyword -> bash). The SSH
	# user must be ``diaguser`` -- the ``su`` user is locked to a
	# restricted CLI on RLS and rejects the ``shell`` keyword (per
	# field testing: CLI returns "Unknown keyword: shell"). The REST
	# vendor key ``ciena-rls-rest`` already resolves to
	# ``diaguser``/``Ciena123``, so we reuse it for SSH too --
	# diaguser is one account with both REST API and shell access.
	# ``_try_open_jump_tunnel`` runs lazily on the first non-seed
	# REST call.
	global _seed_direct_host, _tunnel_ssh_creds, _tunnel_open_attempted, _jump_tunnel
	_seed_direct_host = seed_host
	_tunnel_open_attempted = False
	_jump_tunnel = None
	try:
		from utils.credentials import get_default_credential_for_vendor
		_ssh_pair = get_default_credential_for_vendor("ciena-rls-rest")  # -> diaguser / Ciena123
	except Exception:
		_ssh_pair = None
	_tunnel_ssh_creds = _ssh_pair or ("diaguser", "Ciena123")
	if options.outfile and not options.outfile.endswith(".xlsx"):
		outputfilename = options.outfile + ".xlsx"
	else:
		outputfilename = options.outfile
	payload = {"username": options.username, "password": options.password}
	headers = {"Content-Type": "application/json"}
	node_workbook = None
	dumpfile = ""



		


























	#
	# Start main program
	#
	#Create excel file if filename is provided
	if options.outfile:
		#Create Excel file
		workbook = xlsxwriter.Workbook(outputfilename)
		format_pwr = workbook.add_format({'num_format': '0.0', 'align': 'center'})
		format_pwr_green = workbook.add_format({'num_format': '0.0', 'align': 'center','font_color': '#006100'})
		format_pwr_red = workbook.add_format({'num_format': '0.0', 'align': 'center','font_color': '#9C0006'})
		format_losses = workbook.add_format({'num_format': '0.00', 'align': 'center'})
		format_distance = workbook.add_format({'num_format': '0.000', 'align': 'left'})
		format_centre = workbook.add_format({'align': 'center'})
		format_w = workbook.add_format({'num_format': '0.0' , 'align': 'left'})
		format_freq = workbook.add_format({'num_format': '0.00' , 'align': 'left'})
		format_green = workbook.add_format({'font_color': '#006100'})
		format_green_bold = workbook.add_format({'font_color': '#009933' , 'bold':True})
		format_blue = workbook.add_format({'font_color': '#0066CC'})
		format_blue_bold = workbook.add_format({'font_color': '#0066CC' , 'bold':True})
		format_red_bold = workbook.add_format({'font_color': '#FF0000' , 'bold':True})
		format_green_left = workbook.add_format({'font_color': '#006100','align':'left'})
		format_red = workbook.add_format({'font_color': '#9C0006'})
		format_red_left = workbook.add_format({'font_color': '#9C0006' , 'align':'left'})
		format_amber = workbook.add_format({'font_color': '#CC9900'})
		format_yellow = workbook.add_format({'font_color': '#FFCC00' , 'bold': True})
		format_header = workbook.add_format({'bg_color' : '#99CCFF' , 'bold': True , 'text_wrap' : True})
		format_header_centre = workbook.add_format({'bg_color' : '#99CCFF' , 'bold': True , 'text_wrap' : True ,'align': 'center'})
		center = workbook.add_format({'align': 'center'})
		left = workbook.add_format({'align': 'left'})
		right = workbook.add_format({'align': 'right'})
		green_fmt = workbook.add_format({'font_color': '#00B050', 'align': 'center', 'valign': 'vcenter'})
		red_fmt = workbook.add_format({'font_color': '#C00000', 'align': 'center', 'valign': 'vcenter'})
		system_parameters_format= workbook.add_format({'align': 'left', 'valign':'top' , 'text_wrap' : True})
		
		format_lookup = {'in-service':format_green,
						'out-of-service':format_red,
						'waiting-for-power':format_amber,
						'unknown':format_yellow,
						'loss':format_red,
						'damped-power':format_amber,
						'power':format_green,
						'standby':format_green,
						'Loss of signal detected':format_red,
						'Target attenuations unavailable':format_amber,
						}

		format_ts_link = workbook.add_format({'font_color': '#0563C1', 'underline': 1})
		#Create index sheet (must be first so it appears as first tab)
		indexsheet = workbook.add_worksheet('Index')
		indexsheet.set_zoom(120)
		indexsheet.hide_gridlines(2)
		indexsheet.set_column(0, 0, 22)   # wide enough for 'AFC Orchestrator'
		indexsheet.set_column(1, 1, 80)   # description column
		indexsheet.write('A1', dt_string)
		# Index formats
		format_index_title = workbook.add_format({'bold': True, 'font_size': 14})
		format_index_link  = workbook.add_format({'font_color': '#0563C1', 'underline': 1, 'bold': True, 'border': 1, 'valign': 'top'})
		format_index_desc  = workbook.add_format({'align': 'left', 'text_wrap': True, 'border': 1, 'valign': 'top'})
		format_index_hdr   = workbook.add_format({'bg_color': '#99CCFF', 'bold': True, 'border': 1})
		indexsheet.write(1, 0, 'RLS Node Audit', format_index_title)
		indexsheet.write(2, 0, 'Tab',         format_index_hdr)
		indexsheet.write(2, 1, 'Description', format_index_hdr)
		index_tabs = [
			('Systems',          'One row per queried node. Node identity, software versions, CTM status, license, coordinates, neighbor SW summary.'),
			('Links',            'Physical fiber health per span: loss measurements, OSC power, fiber type, length, and AFC section data (PFG type, cascaded, associated elements).'),
			('Amps',             'Amplifier card inventory and optical power levels (dBm) per amp port across all nodes.'),
			('Alarms',           'Active alarms per node at time of audit: severity, description, and affected resource.'),
			('AFC Orchestrator', 'Automatic Fiber Characterization state per OMS instance: run state, result, compatibility, channel count, and last update time.'),
			('Alarm History',    'Historical alarm log (captured with -H option): timestamped alarm events per node.'),
			('Circuits',         'Optical circuit detail per channel: frequency, spectral width, operational state, power levels at mux/demux ports.'),
			('Inventory',        'Hardware inventory per slot: card type, serial number, part number, firmware, and operational state.'),
		]
		for idx_row, (tab_name, tab_desc) in enumerate(index_tabs, 3):
			indexsheet.write_url(idx_row, 0, f"internal:'{tab_name}'!A1", format_index_link, tab_name)
			indexsheet.write(idx_row, 1, tab_desc, format_index_desc)
			indexsheet.set_row(idx_row, None, None, {'level': 0})  # allow auto-height for wrapped text
		indexsheet.set_row(1, 22)
		indexsheet.set_row(2, 18)

		#Create system sheet
		systemworksheet = workbook.add_worksheet('Systems')
		systemworksheet.set_zoom(120)
		systemworksheet.hide_gridlines(2)
		systemworksheet_header={'Node Name':18,'Domain':20,'Application':16,'Alarm Free':10,'TLS Cert':10,'Shelf Type':10,'Serial':15,'MAC Address':15,'Software Version':20,'CTM41':14,'CTM42':14 ,'License':15,'Location':30,'Power Consumption (W)':14,'Address':30,'Latitude':12,'Longitude':12,'Active Features':40,'SW Active':22,'SW Running':22,'SW Committed':22,'SW Upgrade State':20,'SW Delivered':30,'Disconnected Neighbors':30,'Neighbor SW Versions':40}
		# have removed 'Neighbours':35
		for posn , ( title , width) in enumerate(systemworksheet_header.items()):
			systemworksheet.write( systemrow , posn , title , format_header)
			systemworksheet.set_column( posn , posn , width)
		draw_border(systemworksheet , systemrow , 0 , systemrow , len(systemworksheet_header))
		systemrow += 1
		systemworksheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)
		systemworksheet.write( 'B1' , __program__ + ' : V' + __version__)
		
		#Create link sheet
		linkworksheet = workbook.add_worksheet('Links')
		linkworksheet.set_zoom(120)
		linkworksheet.hide_gridlines(2)
		linkworksheet_header = {'Node':18,'Link Name':20,'Type':14,'From':36,'To':36,'Link Diagnostics':25,'Length':11,'Fiber Type':12,'OSC Tx':8,'OSC Rx':8,'Expected Loss':10,'Measured Loss':10,
							 'Loss Difference':10,'Assumed Fiber Loss (dB/Km)':13,'Assumed Connector Loss':13,'Calculated Expected Loss':13,'PFG Type':14,'Cascaded':12,'Associated Elements':50,'Bidir Loss Delta':14,'Internal Loss (dB)':14}
		# linkworksheet_list = list(linkworksheet_header)
		for posn , ( title , width) in enumerate(linkworksheet_header.items()):
			linkworksheet.write( linkrow , posn , title , format_header)
			linkworksheet.set_column( posn , posn , width )
		draw_border(linkworksheet , linkrow , 0 , linkrow , len(linkworksheet_header))
		linkrow += 1
		linkworksheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)
		linkworksheet.write( linkrow-3 , list(linkworksheet_header).index('Measured Loss') , 'If Red, loss' , center)
		linkworksheet.write( linkrow-2 , list(linkworksheet_header).index('Measured Loss') , '> calculated' , center)
		linkworksheet.merge_range( linkrow-2 , list(linkworksheet_header).index('Assumed Fiber Loss (dB/Km)') , linkrow-2 , list(linkworksheet_header).index('Assumed Connector Loss') , '--- Manually change if rqd ---' , format_blue_bold)

		#Create amp sheet
		ampsheet = workbook.add_worksheet('Amps')
		ampsheet.set_zoom(120)
		ampsheet.hide_gridlines(2)
		cntr_format_places = (9,10,11,12,13,14)
		ampsheet_header = {'Node':18,'Slot':8,'Amp Name':20,'Admin State':13,'Operational State':22,'Forced Shutoff':15,'Diagnostics':25,'Amp Mode':13,'Gain Mode':13,'Reported':10,'Target':10,'Diff':10,'Reported ':10,
						'Target ':10,'Diff  ':10,'ORL':10,'ORL State':orl_cell_width,
						'Target Power (dBm)':18,'In Power (dBm)':16,'In Min (dBm)':14,'In Max (dBm)':14,'Out Power (dBm)':16,'Out Min (dBm)':14,'Out Max (dBm)':14,'Parent Link':22,'Timestamp':22}
		for posn , ( title , width) in enumerate(ampsheet_header.items()):
			if posn in cntr_format_places:
				ampsheet.write( amprow + 1 , posn , title , format_header_centre)
			else:
				ampsheet.write( amprow + 1 , posn , title , format_header)
			ampsheet.set_column( posn , posn , width )
			ampsheet.write(amprow , posn , '' , format_header) # write empty cells to top part of header
		ampsheet.write(amprow , 6 , 	'Amplifier' , format_header)
		ampsheet.merge_range(amprow , 9 , amprow , 11 , 'Gain' , format_header_centre)
		ampsheet.merge_range(amprow , 12 , amprow , 14 , 'Tilt' , format_header_centre)
		draw_border(ampsheet , amprow , 0 , amprow + 2 , 9)
		draw_border(ampsheet , amprow , 9, amprow + 2 , 12)
		draw_border(ampsheet , amprow , 12 , amprow + 2 , 15)
		draw_border(ampsheet , amprow , 15 , amprow + 2 , 17)
		ampsheet.merge_range(amprow , 17 , amprow , 25 , 'Optical Power' , format_header_centre)
		draw_border(ampsheet , amprow , 17 , amprow + 2 , 26)
		amprow += 2
		ampsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)

		#Create alarm sheet if requested via -a option
		if options.alarms:
			alarmsheet = workbook.add_worksheet('Alarms')
			alarmsheet.set_zoom(120)
			alarmsheet.hide_gridlines(2)
			alarmsheet_header = {'Node':18,'Alarm Count':10,'Alarm Severity':18,'Alarm Description':70}
			for posn , ( title , width) in enumerate(alarmsheet_header.items()):
				alarmsheet.write( alarmrow , posn , title , format_header)
				alarmsheet.set_column(posn , posn , width)
			draw_border(alarmsheet , alarmrow , 0 , alarmrow , len(alarmsheet_header))
			alarmrow += 1
			alarmsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)


		#Create AFC orchestrator sheet
		afcsheet = workbook.add_worksheet('AFC Orchestrator')
		afcsheet.set_zoom(120)
		afcsheet.hide_gridlines(2)
		afcsheet_header = {'Node':18,'Instance':22,'Mode':14,'State Mode':16,'State':18,'Result':18,'Compatibility':18,'Container-1':30,'Container-2':30,'Two-Peak Result':30,'Action Wait Time':14,'OMS View':30,'Last Update':22}
		for posn , (title , width) in enumerate(afcsheet_header.items()):
			afcsheet.write( afcrow , posn , title , format_header)
			afcsheet.set_column(posn , posn , width)
		draw_border(afcsheet , afcrow , 0 , afcrow , len(afcsheet_header))
		afcrow += 1
		afcsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)



		#Create alarm history sheet if -H option used
		if options.alarm_history:
			hxsheet = workbook.add_worksheet('Alarm History')
			hxsheet.set_zoom(120)
			hxsheet.hide_gridlines(2)
			hxsheet_header = {'Node':18,'History ID':10,'Alarm ID':10,'Name':30,'Severity':10,'Alarm State':12,'Update Reason':16,'History Time':22,'Raise Time':22,'Clear Time':22,'Cause':30,'Resource':45,'Direction':12,'Location':12,'Service Impact':16,'Condition Type':18,'Additional Info':40,'User Notes':40}
			for posn , (title , width) in enumerate(hxsheet_header.items()):
				hxsheet.write( hxrow , posn , title , format_header)
				hxsheet.set_column(posn , posn , width)
			draw_border(hxsheet , hxrow , 0 , hxrow , len(hxsheet_header))
			hxrow += 1
			hxsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)

		#Create circuit sheet
		cctsheet = workbook.add_worksheet('Circuits')
		cctsheet.set_zoom(120)
		cctsheet.hide_gridlines(2)
		cctsheet_btm_header = {'Node':14,'Circuit Name':12,'Band':6,'Mux Instance':12,'Operational State':25,'Controller State':16,'Freq':12,'width':10,'width ':10,'Check':7,'width  ':10,'Additonal Info':30,
							'Mode':16,'CCMD Ports':12,'Rx':6,'Tx':6,'Mux Ports':35,'Line Ports':45,'Measured':10,'Expected':10,'Measured ':10,'Expected ':10,'Measured  ':10,'Expected  ':10,'Expected ':10,
							'Measured  ':10,'Expected  ':10,'Measured   ':10,'Expected   ':10}
		for posn , (title , width) in enumerate(cctsheet_btm_header.items()):
			if ('Measured' or 'Expected' or 'Rx' or 'Tx') in title:
				cctsheet.write( cctrow + 1 , posn , title , format_header_centre)
			else:
				cctsheet.write( cctrow + 1 , posn , title , format_header)
			cctsheet.write( cctrow , posn , '' , format_header)  # write empty cells to top part of header
			cctsheet.set_column( posn , posn , width )
		cctsheet.write(cctrow , 6 , 'Center' , format_header)
		cctsheet.write(cctrow , 7 , 'Spectral' , format_header)
		cctsheet.write(cctrow , 8 , 'MC Spectral' , format_header)
		cctsheet.write(cctrow , 9 , 'MC Freq' , format_header)
		# cctsheet.write(cctrow , 10 , 'Control' , format_header)
		# cctsheet.write(cctrow , 14 , 'Mux Out' , format_header)
		# cctsheet.write(cctrow , 15 , 'Target' , format_header)
		# cctsheet.write(cctrow , 16 , 'Capacity' , format_header)
		cctsheet.write(cctrow , 10 , 'NMC Spectral' , format_header)
		ccmd_pwr_hdr = 14
		cctsheet.merge_range(cctrow , ccmd_pwr_hdr , cctrow , ccmd_pwr_hdr+1 , 'CCMD Power' , format_header_centre)
		top_hdr_start = 18
		# cctsheet.merge_range(cctrow , top_hdr_start , cctrow , top_hdr_start+1 , 'Add/Drop Pwr' , format_header_centre)
		cctsheet.merge_range(cctrow , top_hdr_start , cctrow , top_hdr_start+1 , 'Mux Port Input Power' , format_header_centre)
		cctsheet.merge_range(cctrow , top_hdr_start+2 , cctrow , top_hdr_start+3 , 'Lineout Output Power' , format_header_centre)
		cctsheet.merge_range(cctrow , top_hdr_start+4 , cctrow , top_hdr_start+5 , 'Linein Post Amp Power' , format_header_centre)
		cctsheet.merge_range(cctrow , top_hdr_start+6 , cctrow , top_hdr_start+7 , 'Mux Port Output Power' , format_header_centre)
		# draw_border(cctsheet , cctrow , 0 , cctrow + 2 , len(cctsheet_btm_header))
		draw_border(cctsheet , cctrow , 0 , cctrow + 2 , list(cctsheet_btm_header).index('Measured'))
		draw_border(cctsheet , cctrow , list(cctsheet_btm_header).index('Measured') , cctrow + 2 , list(cctsheet_btm_header).index('Measured '))
		draw_border(cctsheet , cctrow , list(cctsheet_btm_header).index('Measured ') , cctrow + 2 , list(cctsheet_btm_header).index('Measured  '))
		draw_border(cctsheet , cctrow , list(cctsheet_btm_header).index('Measured  ') , cctrow + 2 , list(cctsheet_btm_header).index('Measured   '))
		draw_border(cctsheet , cctrow , list(cctsheet_btm_header).index('Measured   ') , cctrow + 2 , len(cctsheet_btm_header))
		# draw_border(cctsheet , cctrow , 0 , cctrow + 2 , len(cctsheet_btm_header))

		cctrow += 2
		cctsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)

		#Create inventory sheet
		invsheet = workbook.add_worksheet('Inventory')
		invsheet.set_zoom(120)
		invsheet.hide_gridlines(2)
		invsheet_header = {'Node':22,'Slot':8,'Hardware Version':14,'Type':36,'Status':12,'Serial Number':16,'CLEI':14,'Issue':6,'Uptime Day:Hr:Min:Secs':14,'Card Diagnostics':12,'aDB Asset':15}
		for posn , ( title , width) in enumerate(invsheet_header.items()):
			invsheet.write( invrow , posn , title , format_header)
			invsheet.set_column(posn , posn , width)
		draw_border(invsheet , invrow , 0 , invrow , len(invsheet_header))
		invrow += 1
		invsheet.write_url('A1', "internal:'Index'!A1", format_ts_link, dt_string)

	# Create dump file
	if options.dump_file:
		node_workbook = xlsxwriter.Workbook(dumpfile)
		nw_format_blue_bold = node_workbook.add_format({'font_color': '#0066CC' , 'bold':True})

	# Define the headers
	headers = {
		'Content-Type': 'application/x-www-form-urlencoded',
		'Accept': 'application/json'
	}

	# Prepare the payload with URL-encoded data
	payload = {
		'username': username,
		'password': password
	}

	session = requests.Session()

	# Ensure all hosts are lowercase and domain removed
	def _strip_domain(item):
		# Leave bare IP addresses unchanged; strip domain suffix from hostnames
		return item.lower() if re.match(r'^\d+\.\d+\.\d+\.\d+$', item) else item.lower().split('.')[0]
	# Drive the walk from a dedup'ing BFS queue (was: iterate a ``hostlist``
	# grown in place). ``walkq.order`` IS that list, so ``hostlist.index`` /
	# ``len(hostlist)`` call sites keep working, and the same structure is
	# ready for the concurrent collector.
	_seeds = [_strip_domain(item) for item in hostlist]
	walkq = _WalkQueue(_seeds[0])
	for _seed_extra in _seeds[1:]:
		walkq.add_candidate(_seed_extra, format_hostname(_seed_extra)[0])
	hostlist = walkq.order
	# Reset parameters
	network_inventory = {}
	network_links = {}
	network_sections = {}

	# Phase banner: tell the operator what is about to happen and what
	# data collection is enabled, so the panel doesn't go silent while
	# REST calls execute. ``hostlist`` grows as nodes are discovered, so
	# log the *initial* seed count here -- per-node logs below show the
	# walk expanding.
	_extras = []
	if options.alarms: _extras.append("alarms")
	if options.alarm_history: _extras.append("alarm history")
	_extras_label = (" + " + " + ".join(_extras)) if _extras else ""
	log_callback(f"[AUDIT] Starting walk from seed: {hostlist[0]}{_extras_label}")
	log_callback(
		"[AUDIT] Collection mode: "
		+ (f"concurrent (SSH pool={SSH_POOL_SIZE})" if SSH_POOL_SIZE > 1
		   else "serial (set RLS_AUDIT_SSH_POOL>1 to parallelize)")
	)
	log_callback(f"[AUDIT] Output: {outputfilename}")

	node_count = 0
	# ``walkq`` owns the dedup of nodes reachable under two identifiers
	# (e.g. by IP from REST discovery and by name from the link-walk) so a
	# node isn't audited twice / doesn't add duplicate rows.
	#
	# Stage 2: per-node work is split into ``_collect_node`` (network fetch
	# + parse -> a plain record) and ``_write_record`` (workbook writes). A
	# serial driver runs collect->write in walk order (identical output);
	# Stage 3 fans the collect phase out across the SSH pool.
	# ``assumed_*`` are pre-bound so the nested ``nonlocal`` is legal even
	# when no node reaches the loss-table assignment.
	assumed_fiber_loss = assumed_connector_loss = None
	# Locks guarding shared state once collection fans out across workers
	# (Stage 3). Uncontended -- and thus free -- on the serial path.
	_count_lock = threading.Lock()
	_wb_lock = threading.Lock()
	def _collect_node(host):
		nonlocal node_count, discover, assumed_fiber_loss, assumed_connector_loss
		global lh_network, orl_cell_width
		# Check if host exists in DNS
		short_node_name,fqdn = format_hostname(host)
		# Dedup early: if we've already done this node under a different
		# identifier, skip. ``short_node_name`` is empty for bare IPs
		# until login resolves them, so this only kicks in for genuine
		# duplicates (same hostname, or same IP listed twice).
		if short_node_name and walkq.is_walked(short_node_name):
			log_callback(
				f"[{short_node_name}] Already walked under another identifier"
				f" -- skipping {host}"
			)
			return None
		try:
			hostip = socket.gethostbyname(fqdn)
			dnsvalid = True
		except Exception as e:
			debug_log(e, 'DNS lookup ' + fqdn)
			dnsvalid = False
			log_callback(node_count , ':' , fqdn , ':' , 'Not found in DNS' )
			return None
		with _count_lock:
			node_count += 1
		log_callback(node_count , ':' , short_node_name , ': ...' , end='\r')
		if short_node_name:
			walkq.mark_walked(short_node_name)
		command = 'login'
		login_attempt , login_valid , check_tls = login_to_node(fqdn, command , payload, headers)
		if login_valid:
			log_callback(node_count , ':' , short_node_name , ':' , 'Connected' )
			cookies = _current_session().cookies.get_dict()
		else:
			log_callback(node_count , ':' , short_node_name , ':' , end = '')
			if login_attempt in resp_codes:
				log_callback(login_attempt , resp_codes[login_attempt] )
			else:
				log_callback(login_attempt )
			log_callback('')
		# Process node data
		if login_valid:
			# If rawdump requested, get all data from node and write to sheet with same node name
			if options.dump_file:
				log_callback('Getting all data from node. Please be patient')
				nodesheet = node_workbook.add_worksheet(short_node_name)
				nodesheet.set_zoom(120)
				nodesheet.hide_gridlines(2)
				command = 'restconf/data'
				all_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					nd = json.loads(all_data)
					fdo = json.dumps(nd , indent = 4).splitlines() # Formatted data output
					nodesheet.write( 0 , 0 , short_node_name , nw_format_blue_bold)
					for node_row_num , linedata in enumerate(fdo):
						nodesheet.write(node_row_num + 1 , 0 , linedata)
					nodesheet.set_column(0 , 0 , 255)
			# Clear parameters
			node_name = domain = swversion = line_card = shelf_type = serial_number = powerconsumption = location = mac_address = '--'
			site_address = site_lat = site_lon = active_features = '--'
			sw_active = sw_running = sw_committed = sw_upgrade_state = sw_delivered = '--'
			disconnected_neighbors = neighbor_sw_versions = '--'
			afc_list = []
			alarm_history_list = []
			section_list = []
			topo_list = []
			inv_list = []
			# Get node name and neighbours
			log_callback(f"[{short_node_name}] Discovering neighbors via REST...")
			command = 'restconf/data/ciena-6500r-nodes:nodes=*'
			get_nodes , data_valid = get_data(fqdn , command , cookies)
			# neighbour_list = ()
			if data_valid:
				nodes = json.loads(get_nodes)['ciena-6500r-nodes:nodes']
				if isinstance(nodes, dict):
					nodes = [nodes]
				_remote_nodes = [n for n in nodes if n.get('config', {}).get('node-type') != 'local']
				log_callback(
					f"[{short_node_name}] Found {len(nodes)} node(s)"
					f" ({len(_remote_nodes)} remote neighbor(s))"
				)
				for node in nodes:
					node_details , null = format_hostname(node['node-name'])
					node_cfg = node.get('config', {})
					node_diag = node.get('diagnostic', {})
					if node_cfg.get('node-type') == 'local':
						swversion = node_diag.get('software-version', '--')
						# Track every identifier this host is known by
						# so a later neighbor that reports it (under a
						# different IP) doesn't trigger a redundant
						# walk. The seed is the worst offender: entered
						# as ``10.0.0.1`` (factory mgmt), but every
						# neighbor's topology view reports it as a
						# section neighbor at its OPERATIONAL IP (e.g.
						# ``10.6.22.129``) -- so without recording both,
						# the seed gets audited twice and every sheet
						# picks up duplicate rows for it. Same logic
						# applies to neighbors when discovered indirectly
						# via a different identifier.
						_resolved = node_details.lower() if node_details else ''
						if _resolved:
							walkq.mark_walked(_resolved)
						_local_ip = (node_cfg.get('ip-address') or node.get('ip-address') or '').strip().lower()
						if _local_ip and _local_ip != '0.0.0.0':
							walkq.mark_walked(_local_ip)
					# The RLS REST schema reports ``ip-address`` at the
					# config block AND mirrors it at the top level for
					# section/site neighbors. Prefer the config block
					# (always present for the local node); fall back to
					# the top-level field when config is missing it.
					_ip = node_cfg.get('ip-address', '') or node.get('ip-address', '')
					_ipv6 = node_cfg.get('ipv6-address', '') or node.get('ipv6-address', '')
					topo_list.append({
						'neighbor_name': node.get('node-name', ''),
						'node_type': node_cfg.get('node-type', ''),
						'platform_type': '',
						'ip_address': _ip,
						'ipv6_address': _ipv6,
						'sw_version': node_diag.get('software-version', ''),
						'connection_state': node_diag.get('connection-state', ''),
						# RLS reports node liveness via ``diagnostic.is-up``
						# rather than ``connection-state``. Captured here
						# so discovery can skip dead nodes (heartbeat
						# failed) instead of wasting REST timeouts on
						# them. Default True for the local node which
						# doesn't carry an is-up flag.
						'is_up': node_diag.get('is-up', True),
						'mac_address': '',
					})
			# Enrich topo_list with config-neighbours (IP, platform-type, connection-state, MAC)
			nbr_data , nbr_valid = get_data(fqdn , 'restconf/data/ciena-pro-neighbours-config:config-neighbours' , cookies)
			if nbr_valid:
				try:
					nbrs_raw = json.loads(nbr_data).get('ciena-pro-neighbours-config:config-neighbours', [])
					if isinstance(nbrs_raw, dict):
						nbrs_raw = [nbrs_raw]
					nbr_map = {}
					for nbr in nbrs_raw:
						nbr_name_key = format_hostname(nbr.get('name', ''))[0]
						nbr_map[nbr_name_key] = nbr
					for entry in topo_list:
						key = format_hostname(entry['neighbor_name'])[0]
						if key in nbr_map:
							nbr = nbr_map[key]
							if not entry['ip_address']:
								entry['ip_address'] = nbr.get('ip-address', '')
							if not entry['ipv6_address']:
								entry['ipv6_address'] = nbr.get('ipv6-address', '')
							entry['platform_type'] = nbr.get('platform-type', '')
							entry['mac_address'] = nbr.get('mac-address', '')
							if not entry['connection_state']:
								entry['connection_state'] = nbr.get('diagnostic', {}).get('connection-state', '')
				except (KeyError, TypeError, ValueError):
					pass

			# REST-based topology discovery: extend ``hostlist`` with every
			# remote node we just learned about via ``/nodes=*`` (+ the
			# neighbour-config enrichment above). The legacy CLI relied on
			# the fiber link-walk further below to populate hostlist, but
			# that loop gives up at the first OL/ROADM boundary, so a
			# ROADM seed (like ``uselp1-l8r2``) used to walk only itself.
			# Use the IP when we have one (no DNS dependency on customer
			# nets); fall back to the bare node-name otherwise. Python
			# for-loops over a list re-check ``len()`` each iteration, so
			# appending here is safe -- the outer ``for host in hostlist``
			# will pick up the new entries.
			if options.discover:
				_added = 0
				_skipped_no_id = 0
				_skipped_local = 0
				_skipped_dup = 0
				_skipped_dead = 0
				_dead_names = []
				for entry in topo_list:
					if entry.get('node_type') == 'local':
						_skipped_local += 1
						continue
					# Skip nodes whose heartbeat has failed -- walking
					# them just wastes REST timeouts. The RLS schema
					# reports liveness as ``diagnostic.is-up`` (bool).
					# Only false-y values count as dead; the local node
					# (which never has is-up) was already handled above.
					if entry.get('is_up') is False:
						_skipped_dead += 1
						_dead_names.append(
							format_hostname(entry.get('neighbor_name', ''))[0]
							or entry.get('ip_address') or '?'
						)
						continue
					ip = (entry.get('ip_address') or '').strip()
					nm = (entry.get('neighbor_name') or '').strip()
					short_nm, _ = format_hostname(nm) if nm else ('', '')
					# Prefer IP -- works on any customer network. Skip
					# zero/empty IPs (some configs leave 0.0.0.0 in).
					candidate = ip if (ip and ip != '0.0.0.0') else short_nm
					if not candidate:
						_skipped_no_id += 1
						continue
					# Deduplicate against everything already on hostlist
					# AND against the set of identifiers we've already
					# walked. Two-source dedup catches the seed-as-
					# operational-IP case: the seed is in hostlist as
					# (e.g.) ``10.0.0.1`` but in _walked_short_names as
					# both ``10.0.0.1`` and ``10.6.22.129`` (its
					# operational IP, recorded when its REST identified
					# itself as the local node). Without the
					# _walked_short_names check, a neighbor's topology
					# view that reports the seed at 10.6.22.129 would
					# slip past the hostlist scan and queue a duplicate
					# walk.
					# ``walkq`` reproduces the original four-way dedup:
					# candidate identifier / short-name against both the
					# already-queued nodes and the already-walked aliases.
					if walkq.add_candidate(candidate, short_nm):
						_added += 1
					else:
						_skipped_dup += 1
				if _added:
					log_callback(
						f"[{short_node_name}] Added {_added} node(s)"
						f" to walk queue (total now: {len(hostlist)})"
					)
				if _skipped_dead:
					# Always surface dead-node count so the operator
					# can see at a glance how much of the topology is
					# off-line.
					log_callback(
						f"[{short_node_name}] Skipped {_skipped_dead}"
						f" dead node(s) (is-up=false): "
						f"{', '.join(_dead_names)}"
					)
				if not _added:
					# Loud about an empty discovery -- this used to be
					# silent and is the symptom of "REST found 11 nodes
					# but only the seed was walked". Print the per-entry
					# state so the operator (or a bug report's log file)
					# shows exactly what came back from REST.
					log_callback(
						f"[{short_node_name}] Discovery added 0 nodes "
						f"(skipped: {_skipped_local} local, "
						f"{_skipped_dead} dead, "
						f"{_skipped_no_id} missing IP+name, "
						f"{_skipped_dup} duplicate)"
					)
					for entry in topo_list:
						log_callback(
							f"  topo entry: node_type={entry.get('node_type')!r}"
							f" name={entry.get('neighbor_name')!r}"
							f" ip={entry.get('ip_address')!r}"
							f" is_up={entry.get('is_up')!r}"
						)

			# Compute neighbor summary for Systems sheet
			remote_nbrs = [t for t in topo_list if t.get('node_type','') != 'local']
			disc = [t['neighbor_name'].split('.')[0] for t in remote_nbrs if 'disconnect' in t.get('connection_state','').lower()]
			disconnected_neighbors = ', '.join(disc) if disc else '--'
			nbr_sw = [t['neighbor_name'].split('.')[0] + ':' + t['sw_version'] for t in remote_nbrs if t.get('sw_version','')]
			neighbor_sw_versions = ', '.join(nbr_sw) if nbr_sw else '--'
			# Get node data
			if options.outfile:
				log_callback(f"[{short_node_name}] Reading system + inventory data...")
				# Get system data for node name and domain
				command = 'restconf/data/ciena-6500r-system:system'
				node_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					node = json.loads(node_data)['ciena-6500r-system:system']
					try:
						system_name = extract(node , 'id.member.name')
						reported_node_name = system_name.split('.',1)[0]
						domain = system_name.split('.',1)[1]
					except Exception as e:
						debug_log(e, 'system name/domain parse')
						reported_node_name = node['name']
						domain = 'Not configured'
					# Site ID (latitude, longitude, address)
					try:
						site = node['id']['site']
						site_address = site.get('address', '--')
						site_lat = site.get('latitude', '--')
						site_lon = site.get('longitude', '--')
					except Exception as e:
						debug_log(e, 'site ID parse')
						pass
					# Active system features
					try:
						features_list = node.get('features', [])
						enabled = [f['name'] for f in features_list if f.get('enabled') == True]
						active_features = ', '.join(enabled) if enabled else 'None'
					except Exception as e:
						debug_log(e, 'active features parse')
						pass
				command = 'restconf/data/ciena-6500r-shelves:shelf'
				node_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					node = json.loads(node_data)['ciena-6500r-shelves:shelf']
					shelf_hw = node['pec']
					shelf_type = node['ui-name']
					serial_number = node['serial-number']
					powerconsumption = node['current-power']['value']
					location = node['shelf-location']['frame-identification-code']
					mac_address = node['mac-address'].upper()
					hw_release = node['hardware-release']
					log_callback(' TLS Valid :' , end = '')
					if check_tls:
						log_callback(check_tls )
					else:
						log_callback(check_tls )
					# log_callback('Neighbours:' , neighbours)
				else:
					reported_node_name = domain = 'No response'
				# License
				command = 'restconf/data/ciena-pro-license:license'
				node_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					license_info = json.loads(node_data)['ciena-pro-license:license']['license-client']
					license = license_info['operational-state'] + '/' + license_info['compliance-state']

				# Get inventory
				ccmd_slots = [ 71 , 72]
				ctm_status = {}
				ccmd_powers = {}
				node_inventory = {}
				amp_list = []
				system_type = 'Unknown'

				command = 'restconf/data/ciena-6500r-slots:slots=*/inventory/circuit-pack'
				node_data , data_valid = get_data(fqdn , command , cookies)

				if data_valid:
					slot_data = json.loads(node_data)['ciena-6500r-slots:slots']
					# log_callback(slot_data)
					for s in slot_data:
						if 'inventory' in s:
							slot = s['name']
							card = s['inventory']['circuit-pack']
							# Find the longest matching prefix
							original_c_type = card['c-type']
							card_type = resolve_card_type(original_c_type, card_lookup)
							if options.debug:
								log_callback(f"Original: '{original_c_type}' -> Resolved: '{card_type}'")

							if 'CCMD' in card_type:
								# log_callback('Found CCMD in slot' , slot)
								ccmd_slots.append(int(slot))
							slot_item = [slot , card['pec'] , card_type]
							# Determine system type
							if system_type == 'Unknown':
								try:
									system_type = system_lookup[card_type]
								except Exception as e:
									debug_log(e, 'system_lookup card_type=' + card_type)
									pass
							if 'serial-number' in card :
								try:
									admin_state = card['admin-state']
								except Exception as e:
									debug_log(e, 'card admin-state slot=' + str(slot))
									admin_state = '-'
								slot_item.extend([admin_state , card['serial-number'] , card['common-language-equipment-identifier'] ,card['hardware-release']])
							# log_callback(slot_item)
							node_inventory[int(s['name'])] = card_type.split()[0]
							# Get additional information if the card has it
							# card diagnostics
							try:
								diagnostics = card['diagnostic']
								diag_filter = {k : v for k , v in diagnostics.items() if k in diag_search_keys}
								if all_same(diag_filter , False):
									diag_cond = 'Good'
								else:
									diag_cond = 'Fail'
							except Exception as e:
								debug_log(e, 'card diagnostics slot=' + str(slot))
								diag_cond = ''
							# Card uptime
							try:
								card_uptime = card['uptime']
							except Exception as e:
								debug_log(e, 'card uptime slot=' + str(slot))
								card_uptime = ''
							# if the card is an amplifier get aDB asset
							if card_type[:3] in adb_cards:
								adb_info = adb_asset(card['serial-number'])
							else:
								adb_info = ''
							addnl_slot_data = [card_uptime , diag_cond , adb_info]

							# if the card is an amp get the amp data and diagnostics
							amp_filter = ''
							if card_type[:3] in LH_cards or card_type[:3] in Metro_cards:
								if card_type[:3] == 'DLE': # This is an ILA
									is_ila = True
									amp_filter = 'dgff'
								else:
									amp_filter = 'amps'
									is_ila = False
							if amp_filter:
								command = 'restconf/data/ciena-6500r-slots:slots=' + slot +'/inventory/' + amp_filter
								node_data , data_valid = get_data(fqdn , command , cookies)
								if data_valid:
									filter_string = 'ciena-6500r-slots:' + amp_filter
									amp_data = json.loads(node_data)[filter_string]
									this_amp = []
									for amp_entry in amp_data:
										amp_name = amp_entry.get('name', '')

										# Handle ILA structure
										amp = amp_entry['fac']['amps'][0] if is_ila else amp_entry

										amplifier_parameters = {
											'frcd_shut': 'forced-shutoff',
											'amp_admin_state': 'admin-state',
											'amp_state': 'state',
											'amp_mode': 'amp-mode',
											'gain_mode': 'gain-mode',
											'target_gain': 'target-gain',
											'gain': 'gain',
											'target_tilt': 'target-gain-tilt',
											'gain_tilt': 'gain-tilt',
											'ORL': 'optical-return-loss',
											'orl_state': 'orl-state',
										}

										# Extract parameters
										params = {
											var: safe_extract(amp, path)
											for var, path in amplifier_parameters.items()
										}

										# Diagnostics
										diag_raw = safe_extract(amp, 'diagnostic', default=None)
										amp_diag = link_diag(diag_raw) if diag_raw else '-'

										# Gain / tilt diffs
										try:
											gain_diff = float(params['gain']) - float(params['target_gain'])
										except (TypeError, ValueError):
											gain_diff = ''

										try:
											tilt_diff = float(params['gain_tilt']) - float(params['target_tilt'])
										except (TypeError, ValueError):
											tilt_diff = ''

										# ORL state description
										orl_desc = ''
										if params['ORL']:
											orl_state = params.get('orl_state', 'unknown')
											orl_desc = orl_state_lookup.get(
												orl_state, f'Unrecognised ORL state: {orl_state}')

										this_amp = [
											slot,
											amp_name,
											params['amp_admin_state'],
											params['amp_state'],
											params['frcd_shut'],
											amp_diag,
											params['amp_mode'],
											params['gain_mode'],
											params['gain'],
											params['target_gain'],
											gain_diff,
											params['gain_tilt'],
											params['target_tilt'],
											tilt_diff,
											params['ORL'],
											orl_desc,
											safe_extract(amp, 'target-power'),
											safe_extract(amp, 'in-current-power'),
											safe_extract(amp, 'in-min-power'),
											safe_extract(amp, 'in-max-power'),
											safe_extract(amp, 'out-current-power'),
											safe_extract(amp, 'out-min-power'),
											safe_extract(amp, 'out-max-power'),
											safe_extract(amp, 'parent-link'),
											safe_extract(amp, 'out-current-power-timestamp-str'),
										]
										amp_list.append(this_amp)
										# Extend the ORL State cell width if needed
										with _wb_lock:
											if len(orl_desc) - 5 > orl_cell_width:
												orl_cell_width = len(orl_desc) - 5
												orl_state_pos = list(ampsheet_header.keys()).index('ORL State')
												ampsheet.set_column( orl_state_pos , orl_state_pos , orl_cell_width )

							inv_list.append(slot_item + addnl_slot_data)
							# get port power levels if card is a CMD (RLA cards expose no
							# optmons resource -- their per-port power comes from the
							# ccs-instance channel-power already parsed above)
							if 'CCMD' in card_type:
								command = 'restconf/data/ciena-6500r-slots:slots='+ s['name']+'/inventory/optmons=*'
								ccmd_power_data , data_valid = get_data(fqdn , command , cookies)
								if data_valid:
									power_data = json.loads(ccmd_power_data)['ciena-6500r-slots:optmons']
									for pwr_item in power_data:
										ccmd_powers.update({s['name']+'-'+pwr_item['name'] : pwr_item['current-power']})
								# log_callback(ccmd_powers)
							# Get status of CTMs and add to dictionary
							if s['name'] in ctm_slots:
								ctm_card = str('ctm' + s['name'])
								try:
									ctm_cond = str(card['admin-state'] + ':' + card['operational-state'])
								except Exception as e:
									debug_log(e, 'CTM state slot=' + str(s['name']))
									ctm_cond = str('Missing:Fault')
								ctm_status.update({ctm_card : ctm_cond})
							# ctm_status is read directly via dict lookup below

							if system_type == 'Unknown':
								system_type = 'Mux only' # assume system is mux only if no double width cards exist
					try:
						log_callback(' System type :' , colour_for_system_type[system_type] , system_type )
					except Exception as e:
						debug_log(e, 'system type colour lookup')
						log_callback(' System type :' , system_type )

					if 'Metro' not in system_type:
						lh_network = True
						if not options.assumed_fiber_loss:
							assumed_fiber_loss = loss_table['LH']['assumed_fiber_loss']
						if not options.assumed_connector_loss:
							assumed_connector_loss = loss_table['LH']['assumed_connector_loss']
					else:
						if not options.assumed_fiber_loss:
							assumed_fiber_loss = loss_table['Metro']['assumed_fiber_loss']
						if not options.assumed_connector_loss:
							assumed_connector_loss = loss_table['Metro']['assumed_connector_loss']

					network_inventory[short_node_name] = node_inventory
					if options.debug:
						log_callback('Node inventory:\n' , node_inventory)
						log_callback('ctm status:\n' , ctm_status)
						log_callback('CCMD power levels:\n' , ccmd_powers)

				# Get enabled system features (OTDR, Span Calibration, Alarm Correlation, etc.)
				feat_otdr = feat_spancal = feat_alarmcorr = feat_passive = '--'
				command = 'restconf/data/ciena-6500r-system:system/features'
				feat_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					try:
						features = json.loads(feat_data)['ciena-6500r-system:features']
						feat_map = {f['name']: str(f.get('enabled' , False)) for f in features}
						feat_otdr     = feat_map.get('OTDR' , '--')
						feat_spancal  = feat_map.get('SPAN CALIBRATION' , '--')
						feat_alarmcorr = feat_map.get('ALARM CORRELATION' , '--')
						feat_passive  = feat_map.get('PASSIVE TERMINAL CONTROL' , '--')
					except (KeyError, ValueError, TypeError):
						pass

				# Get SFP optical levels
				osc_data = {}
				if not(system_type == 'Unknown' or system_type == 'Mux only'):
					command = 'restconf/data/ciena-6500r-slots:slots=1/inventory/slots'
					sfp_levels , data_valid = get_data(fqdn , command , cookies)
					if data_valid:
						sfp_data = json.loads(sfp_levels)['ciena-6500r-slots:slots']
						# log_callback(sfp_data)
						for sfp in sfp_data:
							sfp_number = int(extract(sfp , 'name'))
							sfp_cp = sfp['inventory']['circuit-pack']
							sfp_state = sfp_cp['admin-state']
							if sfp_state == 'Enabled': # Only add OSC data if the SFP is enabled
								oscs = sfp['inventory']['oscs'][0]
								tx = extract( oscs , 'tx-power')
								rx = extract( oscs , 'rx-power')
								osc_data[sfp_number] = [tx , rx]
							# Add the OSC SFP modules to the hardware Inventory tab.
							# They live under slots=1/inventory/slots=50|60/inventory/
							# circuit-pack and are skipped by the top-level
							# slots=*/inventory/circuit-pack sweep, so append a row
							# here mirroring slot_item + addnl_slot_data (10 fields:
							# Slot, HW Version, Type, Status, Serial, CLEI, Issue,
							# Uptime, Card Diag, aDB -- last three blank for an SFP).
							if sfp_number in osc_sfp_slots and 'c-type' in sfp_cp:
								osc_ctype = resolve_card_type(sfp_cp.get('c-type' , '') , card_lookup)
								inv_list.append([
									'1-' + str(sfp_number),
									sfp_cp.get('pec' , ''),
									osc_ctype or 'OSC SFP',
									sfp_state,
									sfp_cp.get('serial-number' , ''),
									sfp_cp.get('common-language-equipment-identifier' , ''),
									sfp_cp.get('hardware-release' , ''),
									'' , '' , '',
								])
					# log_callback('OSC data:' , osc_data)

				# Get link data
				node_links = []
				command = 'restconf/data/ciena-6500r-links:link=*'
				node_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					link_data = json.loads(node_data)['ciena-6500r-links:link']
					for link in link_data:
						link_name = extract(link , 'name')
						link_type = extract(link , 'link-type')
						# link_label = extract(link , 'to-label')
						from_node,from_slot_num,from_port_num = '','',''
						to_node,to_slot_num,to_port_num = '','',''
						# Attempt to collect to from info from link
						if 'from' in link.keys():
							from_node,from_slot_num,from_port_num = link_connection(link['from'] , short_node_name)
						# ROADMs with direct links to other roadms may use from-label instead of from
						if 'from-label' in link.keys():
							from_label = link.get('from-label').rsplit('-' , 2)
							(from_node , x) = format_hostname(from_label[0])
							try:
								from_slot_num = int(from_label[1])
							except Exception as e:
								debug_log(e, 'from-label slot parse link=' + str(link_name))
								from_slot_num = from_label[1]
							try:
								from_port_num = int(from_label[2])
							except Exception as e:
								debug_log(e, 'from-label port parse link=' + str(link_name))
								from_port_num = from_label[2]
							# log_callback('port found:' , from_port_num)
						if 'to' in link.keys():
							to_node,to_slot_num,to_port_num = link_connection(link['to'] , short_node_name )
						# Add nodes to the tuple as long as they are not empty 
						link_neighbours = tuple(device for device in (to_node, from_node) if (device and 'txp' not in device.lower() and device != short_node_name))
						# Link-walk fallback discovery: only used when the REST
						# topology endpoint didn't already give us a complete
						# node list. The hostlist was extended by-IP up-front
						# from ``/nodes=*`` (see the discovery block above),
						# so on a healthy network this branch is a no-op. We
						# keep it for the degraded case where the REST
						# topology call failed or returned an empty list.
						for nd in link_neighbours:
							short_nd, _ = format_hostname(nd)
							already_queued = any(
								short_nd.lower() == format_hostname(h)[0].lower()
								for h in hostlist
							)
							if not already_queued and not walkq.is_walked(short_nd):
								if discover:
									index = hostlist.index(host)
									walkq.add_candidate(nd, short_nd)
								# Stop node discovery if come across an OL or ROADM
								if '-l8o' in nd.lower() or '-l8r' in nd.lower():
									discover = False
					
						# Get OSC power levels
						OSC_Tx = ''
						OSC_Rx = ''
						try:
							sfp_slot = int(from_port_num - 3)
						except Exception as e:
							debug_log(e, 'OSC sfp_slot calc link=' + str(link_name))
							sfp_slot = 0
						if sfp_slot in osc_data and 'logical' not in link_type:
							OSC_Tx = osc_data[sfp_slot][0]
							OSC_Rx = osc_data[sfp_slot][1]
						try:
							link_fiber_type = link['fiber-type']
						except Exception as e:
							debug_log(e, 'link fiber-type link=' + str(link_name))
							link_fiber_type = ''
						# Attempt to get all other parameters
						try:
							link_length = (float(link['fiber-length'])) / 1000 # convert to Km
						except Exception as e:
							debug_log(e, 'link fiber-length link=' + str(link_name))
							if link_fiber_type == 'line-fiber':
								link_length = 'Not specified'
							else:
								link_length = ''
						link_diagnostics = link_diag(extract(link , 'diagnostic'))
						expected_link_loss = extract(link , 'expected-physical-loss')
						link_measured_loss = extract(link , 'measured-physical-loss')
						try:
							link_loss_difference = round(abs(expected_link_loss - link_measured_loss),2)
						except Exception as e:
							debug_log(e, 'link loss difference link=' + str(link_name))
							link_loss_difference = '-'
						from_port = (from_node , from_slot_num , from_port_num)
						to_port = (to_node , to_slot_num , to_port_num)
						this_link = {'Link Name':link_name,'Type':link_type,'From':from_port,'To':to_port,'Fiber Type':link_fiber_type,'OSC Tx':OSC_Tx,'OSC Rx':OSC_Rx,'Length':link_length,'Measured Loss':link_measured_loss,
					  				'Expected Loss':expected_link_loss,'Link Diagnostics':link_diagnostics,'Loss Difference':link_loss_difference}
						##### ,'Assumed Fiber Loss (dB/Km)':this_fiber_loss,'Assumed Connector Loss':this_connector_loss,'Calculated Expected Loss':calculated_fiber_loss
						# log_callback(link, this_link)
						node_links.append(this_link)
					network_links[short_node_name] = node_links

				# Get circuits if system is a long haul node
				if 'ROADM' in system_type or 'Mux' in system_type:
					# Reset per-node so stale data from a previous node never bleeds in
					cct_db = {}
					mcpath_data = []
					nmcpath_data = []
					# Get MC path data
					command = 'restconf/data/ciena-6500r-mc-path:mc-path'
					mcpath , data_valid = get_data(fqdn , command , cookies)
					if data_valid:
						mcpath_data = json.loads(mcpath)['ciena-6500r-mc-path:mc-path']
					# Get NMC path data
					command = 'restconf/data/ciena-6500r-nmc-path:nmc-path'
					nmcpath , data_valid = get_data(fqdn , command , cookies)
					if data_valid:
						nmcpath_data = json.loads(nmcpath)['ciena-6500r-nmc-path:nmc-path']
					# Get channel controller data if system is a ROADM
					command = 'restconf/data/ciena-6500r-channel-ctrl:ccs-instance=*'
					ccs , data_valid = get_data(fqdn , command , cookies)
					if data_valid:
						# ccs_data = json.loads(ccs)['ciena-6500r-channel-ctrl:ccs-instance']
						ccs_data = json.loads(ccs)
						cct_db = parse_ccs_json(ccs_data, host)
						number_of_circuits = len(cct_db)
						plural = ''
						if number_of_circuits > 0:
							plural = 's'
							log_callback(' ' + str(int(number_of_circuits)) , 'circuit' + plural , 'found')
							if options.debug:
								log_callback('=== Circuits ===')
								log_callback(json.dumps(cct_db, indent=2))
							# log_callback(cct_db)
							# log_callback(system_type)
					else:
						log_callback(' No circuits found')
					# Get CCMD ports
					for network_id, cct in cct_db.items():
						# The circuit network_id is '<freq>.fc' while the mc-path
						# user-label is 'Channel <freq>', so an exact string compare
						# never matched (leaving MC spectral width / freq check and
						# any CCMD add/drop power blank). Match on the shared
						# frequency instead.
						_cid_freq = re.search(r'\d+\.\d+', str(network_id))
						for pth in mcpath_data:
							_lbl_freq = re.search(r'\d+\.\d+', str(pth.get('user-label', '')))
							if _cid_freq and _lbl_freq and float(_cid_freq.group()) == float(_lbl_freq.group()):
								from_port = extract_slot_port(pth.get('from', ''))
								to_port = extract_slot_port(pth.get('to', ''))
								min_freq = float(pth.get('min-freq'))
								max_freq = float(pth.get('max-freq'))
								mc_freq_check = check_center_frequency(cct_db[network_id]['center_frequency'], min_freq, max_freq)
								cct_db[network_id]['mc_spectral_width'] =  max_freq - min_freq
								if mc_freq_check:
									cct_db[network_id]['mc_freq_check'] = check_mark
								# log_callback(network_id , 'MC freq check:' , mc_freq_check , cct_db[network_id]['center_frequency'] , min_freq , max_freq , 'Widths:' , cct_db[network_id]['spectral_width'])
								if from_port and int(from_port.split('-')[0]) in ccmd_slots:
									if int(from_port.split('-')[1]) < 49: # only capture the add/drop ports and not the mux/demux ports
										# add mux port
										try:
											ccmd_rx_power = float(ccmd_powers[from_port])
										except Exception as e:
											debug_log(e, 'CCMD rx power port=' + str(from_port))
											ccmd_rx_power = '-'
										cct_db[network_id]['ccmd_ports'] = from_port + (' | ' + cct_db[network_id]['ccmd_ports'] if cct_db[network_id]['ccmd_ports'] else '')
										cct_db[network_id]['ccmd_rx_pwr'] = ccmd_rx_power
								if to_port and int(to_port.split('-')[0]) in ccmd_slots:
									if int(to_port.split('-')[1]) < 49: # only capture the add/drop ports and not the mux/demux ports
										# add demux port
										try:
											ccmd_tx_power = float(ccmd_powers[to_port])
										except Exception as e:
											debug_log(e, 'CCMD tx power port=' + str(to_port))
											ccmd_tx_power = '-'
										cct_db[network_id]['ccmd_ports'] = (cct_db[network_id]['ccmd_ports'] + ' | ' if cct_db[network_id]['ccmd_ports'] else '') + to_port
										cct_db[network_id]['ccmd_tx_pwr'] = ccmd_tx_power
					# Get NMC spectral width
					for network_id, cct in cct_db.items():
						for npth in nmcpath_data:
							if npth.get('nmc-network-id') == network_id:
								try:
									cct_db[network_id]['nmc_spectral_width'] = float(npth.get('spectral-width'))
								except (TypeError, ValueError):
									pass
								break


				# Get AFC orchestrator state
				command = 'restconf/data/ciena-6500r-afc-orchestrator:afc-orchestrator'
				afc_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					try:
						afc_root = json.loads(afc_data)['ciena-6500r-afc-orchestrator:afc-orchestrator']
						instances = afc_root.get('instance', [])
						if isinstance(instances, dict):
							instances = [instances]
						for inst in instances:
							state_obj = inst.get('state', {}) if isinstance(inst.get('state'), dict) else {}
							result_obj = inst.get('result', {}) if isinstance(inst.get('result'), dict) else {}
							oms_obj = inst.get('show-oms-view', {}) if isinstance(inst.get('show-oms-view'), dict) else {}
							compat = inst.get('compatibility', '')
							if isinstance(compat, dict):
								compat = compat.get('value', compat.get('status', ''))
							afc_list.append({
								'instance': inst.get('name', '--'),
								'mode': inst.get('mode', '--'),
								'state_mode': inst.get('state-mode', '--'),
								'state': state_obj.get('value', state_obj.get('status', '--')) if state_obj else '--',
								'result': result_obj.get('value', result_obj.get('status', '--')) if result_obj else '--',
								'compatibility': compat or '--',
								'container_1': _summarize_afc_container(inst.get('container-1')),
								'container_2': _summarize_afc_container(inst.get('container-2')),
								'two_peak_result': _summarize_afc_container(inst.get('two-peak-measurements-result')),
								'action_wait_time': inst.get('update-action-wait-time', ''),
								'oms_view': str(oms_obj.get('summary', oms_obj.get('value', '')))[:200] if oms_obj else '',
								'last_update': inst.get('last-update', state_obj.get('last-update', '') if state_obj else ''),
							})
					except (KeyError, TypeError):
						pass

				# Get software inventory
				for sw_module in ('ciena-pro-software:software', 'ciena-pro-software-mgmt:software', 'ciena-software-mgmt:software', 'ciena-6500r-software-mgmt:software', 'ciena-rls-software-mgmt:software'):
					sw_data , data_valid = get_data(fqdn , 'restconf/data/' + sw_module , cookies)
					if data_valid:
						try:
							sw_root = json.loads(sw_data).get(sw_module, {})
							if sw_root:
								sw_active = sw_root.get('active-version', '')
								sw_running = sw_root.get('running-version', '')
								sw_committed = sw_root.get('committed-version', '')
								sw_upgrade_to = sw_root.get('upgrade-to-version', '')
								sw_upgrade_state = sw_root.get('upgrade-operational-state', '')
								delivered = sw_root.get('delivered-versions', [])
								if isinstance(delivered, list):
									delivered_str = ', '.join([str(v) for v in delivered])
								else:
									delivered_str = str(delivered)
								sw_delivered = delivered_str
								break  # found valid module, stop trying
						except (KeyError, TypeError, ValueError):
							pass
					else:
						if options.debug:
							log_callback('  SW endpoint failed:', sw_module, '- HTTP', str(sw_data)[:80])

				# Get section data
				command = 'restconf/data/ciena-6500r-sections:section=*'
				sec_data , data_valid = get_data(fqdn , command , cookies)
				if data_valid:
					try:
						sec_entries = json.loads(sec_data).get('ciena-6500r-sections:section', [])
						if isinstance(sec_entries, dict):
							sec_entries = [sec_entries]
						for s in sec_entries:
							nodes = s.get('node', [])
							if isinstance(nodes, dict):
								nodes = [nodes]
							rev_nodes = s.get('reverse-node', [])
							if isinstance(rev_nodes, dict):
								rev_nodes = [rev_nodes]
							assoc = s.get('associated-elements', [])
							if isinstance(assoc, dict):
								assoc = [assoc]
							assoc_parts = []
							for a in assoc:
								if not isinstance(a, dict): continue
								ap = '{}/{}'.format(a.get('node-name', ''), a.get('pfg-name', ''))
								details = []
								if a.get('direction'): details.append('dir=' + str(a.get('direction')))
								if a.get('line-out-port'): details.append('lo=' + str(a.get('line-out-port')))
								if a.get('line-in-port'): details.append('li=' + str(a.get('line-in-port')))
								if a.get('source'): details.append('src=' + str(a.get('source')))
								if details: ap += ' (' + ', '.join(details) + ')'
								assoc_parts.append(ap)
							assoc_str = '; '.join(assoc_parts)
							node_iter = nodes if nodes else [{}]
							rev_iter = rev_nodes if rev_nodes else [{}]
							for n in node_iter:
								for rn in rev_iter:
									section_list.append({
										'section_name': s.get('section-name', ''),
										'section_type': s.get('section-type', ''),
										'pfg_type': s.get('pfg-type', ''),
										'node_name': n.get('node-name', '') if isinstance(n, dict) else '',
										'pfg_name': n.get('pfg-name', '') if isinstance(n, dict) else '',
										'line_out_port': n.get('line-out-port', '') if isinstance(n, dict) else '',
										'line_in_port': n.get('line-in-port', '') if isinstance(n, dict) else '',
										'reverse_node': rn.get('node-name', '') if isinstance(rn, dict) else '',
										'reverse_pfg': rn.get('pfg-name', '') if isinstance(rn, dict) else '',
										'rev_line_out': rn.get('line-out-port', '') if isinstance(rn, dict) else '',
										'rev_line_in': rn.get('line-in-port', '') if isinstance(rn, dict) else '',
										'cascaded': s.get('cascaded', ''),
										'associated_elements': assoc_str,
									})
					except (KeyError, TypeError, ValueError):
						pass

				# Get alarm history if -H option used
				if options.alarm_history:
					command = ALARM_HISTORY_QUERY
					hx_data , data_valid = get_data(fqdn , command , cookies, req_timeout=alarm_history_timeout)
					if data_valid:
						try:
							hx_entries = json.loads(hx_data).get('ciena-pro-alarm:alarm-history', [])
							if isinstance(hx_entries, dict):
								hx_entries = [hx_entries]
							for h in hx_entries:
								alarm_history_list.append({
									'history_id': h.get('history-id', ''),
									'alarm_id': h.get('id', ''),
									'name': h.get('name', ''),
									'severity': h.get('severity', ''),
									'alarm_state': h.get('alarm-state', ''),
									'update_reason': h.get('update-reason', ''),
									'history_time': h.get('history-time', ''),
									'raise_time': h.get('raise-time', ''),
									'clear_time': h.get('clear-time', ''),
									'cause': h.get('cause', ''),
									'resource': clean_alarm(h.get('resource', '')) if h.get('resource') else '',
									'direction': h.get('direction', ''),
									'location': h.get('location', ''),
									'service_impact': h.get('service-impact', ''),
									'condition_type': h.get('condition-type', ''),
									'additional_info': h.get('additional-info', ''),
									'user_notes': '; '.join([str(un.get('note', '')) for un in (h.get('user-notes', []) if isinstance(h.get('user-notes'), list) else [h.get('user-notes')] if h.get('user-notes') else []) if isinstance(un, dict)]),
								})
						except (KeyError, TypeError, ValueError):
							pass

		# Process alarms
		if login_valid:
			alarm_free = 'Could not get alarms'
			alarm_list = []
			alarm_count = 0
			# Get alarm count
			command = 'restconf/data/ciena-pro-alarm:alarm-counts'
			alarm_data , alarms_valid = get_data(fqdn , command , cookies)
			if alarms_valid:
				alarm_data = json.loads(alarm_data)['ciena-pro-alarm:alarm-counts']
				for a in alarm_data:
					alarm_count = alarm_count + alarm_data[a]
				# If there are alarms on the system, go get them
				if alarm_count > 0:
					alarm_free = False
					command = 'restconf/data/ciena-pro-alarm:active-alarm=*'
					node_data , data_valid = get_data(fqdn , command , cookies)
					if data_valid:
						if options.alarms:
							log_callback(' Alarms:' , end=' ')
							system_alarms = json.loads(node_data)['ciena-pro-alarm:active-alarm']
							initial_alarm_list = []
							for a in system_alarms:
								new_item = a['severity'] , a['cause'] + ' ' + clean_alarm(a['resource'])
								initial_alarm_list.append(new_item)
							alarm_list = sorted(initial_alarm_list)
							if alarm_count  == 1:
								log_callback(alarm_count , 'alarm')
							else:
								log_callback(alarm_count , 'alarms')
							current_alarm = 1
							for a in alarm_list:
								alarm_colour(a[0])
								log_callback(a[0] , ':',a[1])
								if current_alarm == max_alarms_to_display:
									alarms_left = len(system_alarms) - current_alarm
									log_callback(' ... plus' , alarms_left , 'other' , end = '')
									if alarms_left > 1:
										log_callback('s')
									else:
										log_callback('')
									break
								current_alarm += 1
				else:
					if options.alarms:
						log_callback('Alarms:' ,'None' )
					alarm_free = True

		# Fetch/parse complete: close the node session, pack the per-node
		# record from this frame's locals, and hand it to the writer.
		if login_valid:
			logout_of_node(fqdn, cookies)
			return _pack_record(locals())
		# Login failed -> no record, no rows (matches the old inline skip).
		return None

	def _write_record(rec):
		# Row counters are module globals advanced as sheets fill.
		global systemrow, alarmrow, cctrow, afcrow, hxrow, amprow, invrow
		# Unpack the record back into the names the write code expects.
		# Defaults cover a partial fetch that never set a field (the old
		# inline loop leaked the previous node's value; an explicit default
		# is safer and only differs on a failed fetch, which writes no rows).
		host = rec.get('host')
		short_node_name = rec.get('short_node_name', '')
		login_valid = rec.get('login_valid', False)
		check_tls = rec.get('check_tls', '')
		reported_node_name = rec.get('reported_node_name', '--')
		domain = rec.get('domain', '--')
		system_type = rec.get('system_type', 'Unknown')
		alarm_free = rec.get('alarm_free', True)
		shelf_type = rec.get('shelf_type', '--')
		serial_number = rec.get('serial_number', '--')
		mac_address = rec.get('mac_address', '--')
		swversion = rec.get('swversion', '--')
		ctm_status = rec.get('ctm_status', {})
		license = rec.get('license', '--')
		location = rec.get('location', '--')
		powerconsumption = rec.get('powerconsumption', '--')
		site_address = rec.get('site_address', '--')
		site_lat = rec.get('site_lat', '--')
		site_lon = rec.get('site_lon', '--')
		active_features = rec.get('active_features', '--')
		sw_active = rec.get('sw_active', '--')
		sw_running = rec.get('sw_running', '--')
		sw_committed = rec.get('sw_committed', '--')
		sw_upgrade_state = rec.get('sw_upgrade_state', '--')
		sw_delivered = rec.get('sw_delivered', '--')
		disconnected_neighbors = rec.get('disconnected_neighbors', '--')
		neighbor_sw_versions = rec.get('neighbor_sw_versions', '--')
		shelf_hw = rec.get('shelf_hw', '--')
		hw_release = rec.get('hw_release', '--')
		alarm_list = rec.get('alarm_list', [])
		cct_db = rec.get('cct_db', {})
		afc_list = rec.get('afc_list', [])
		section_list = rec.get('section_list', [])
		alarm_history_list = rec.get('alarm_history_list', [])
		amp_list = rec.get('amp_list', [])
		inv_list = rec.get('inv_list', [])
		# Write to system sheet
		if login_valid and options.outfile:
			# Select background colour
			if hostlist.index(host) % 2 == 0:
				fill_format = fill_type1
			else:
				fill_format = fill_type2
			#
			system_write_pattern = ('reported_node_name','domain','system_type','alarm_free','check_tls','shelf_type','serial_number','mac_address','swversion','ctm41','ctm42','license','location','powerconsumption','site_address','site_lat','site_lon','active_features','sw_active','sw_running','sw_committed','sw_upgrade_state','sw_delivered','disconnected_neighbors','neighbor_sw_versions')
			# removed ,'neighbours' from the end
			fmt_FT = ( 'alarm_free' , 'check_tls' )
			fmt_application = ('system_type')
			ctm_condition = ('ctm41' , 'ctm42')
			lic_state = ('license')
			domain_state = ('domain')
			system_write_values = {
				'reported_node_name': reported_node_name,
				'domain': domain,
				'system_type': system_type,
				'alarm_free': alarm_free,
				'check_tls': check_tls,
				'shelf_type': shelf_type,
				'serial_number': serial_number,
				'mac_address': mac_address,
				'swversion': swversion,
				'ctm41': ctm_status.get('ctm41', '--'),
				'ctm42': ctm_status.get('ctm42', '--'),
				'license': license,
				'location': location,
				'powerconsumption': powerconsumption,
				'site_address': site_address,
				'site_lat': site_lat,
				'site_lon': site_lon,
				'active_features': active_features,
				'sw_active': sw_active,
				'sw_running': sw_running,
				'sw_committed': sw_committed,
				'sw_upgrade_state': sw_upgrade_state,
				'sw_delivered': sw_delivered,
				'disconnected_neighbors': disconnected_neighbors,
				'neighbor_sw_versions': neighbor_sw_versions,
			}
			for p , v in enumerate(system_write_pattern):
				systemworksheet.write(systemrow , p , system_write_values.get(v, '--') , system_parameters_format)
				# format False and TRUE results
				if v in fmt_FT:
					format_true_False( systemworksheet , systemrow , p )
				# Format system types
				if v in fmt_application:
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'LH', 'format': format_green})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Metro', 'format': format_blue})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Mux only', 'format': format_amber})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Unknown', 'format': format_red})
				if v in lic_state:
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'up/compliant', 'format': format_green})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'not containing', 'value': 'up/compliant', 'format': format_red})
				if v in ctm_condition:
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Enabled:active', 'format': format_green})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Enabled:idle', 'format': format_green})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Disabled', 'format': format_red})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Fault', 'format': format_red})
				if v in domain_state:
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'bb.net.apple.com', 'format': format_green})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'corp.apple.com', 'format': format_amber})
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'containing', 'value': 'Not configured', 'format': format_red})
				if v == 'swversion':
					if swversion in approved_sw_versions:
						systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type' : 'no_errors' , 'format' : format_green})
					elif swversion in previous_approved_sw_versions:
						systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type' : 'no_errors' , 'format' : format_amber})
					else:
						systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type' : 'no_errors' , 'format' : format_red})
				# SW upgrade state formatting (col 23, index 22 = sw_upgrade_state)
				systemworksheet.conditional_format( systemrow , 22 , systemrow , 22 , {'type': 'text' , 'criteria': 'containing', 'value': 'idle', 'format': format_green})
				systemworksheet.conditional_format( systemrow , 22 , systemrow , 22 , {'type': 'text' , 'criteria': 'containing', 'value': 'in-progress', 'format': format_amber})
				systemworksheet.conditional_format( systemrow , 22 , systemrow , 22 , {'type': 'text' , 'criteria': 'containing', 'value': 'failed', 'format': format_red})
				# Format disconnected neighbors if any are disconnected
				if v == 'disconnected_neighbors':
					systemworksheet.conditional_format( systemrow , p , systemrow , p , {'type': 'text' , 'criteria': 'not containing', 'value': '--', 'format': format_red})
			# format the node output with background colour and a border around the node
			systemworksheet.conditional_format( systemrow , 0 , systemrow , len(systemworksheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
			draw_border(systemworksheet , systemrow , 0 , systemrow , len(systemworksheet_header))
			systemrow += 1

			# Write to alarm sheet if -a option used
			if options.alarms:
				# Write hostname
				alarmsheet.write(alarmrow , 0 , reported_node_name)
				# Write alarms from list
				local_alarms = 1
				if len(alarm_list) == 0:
					alarmsheet.write(alarmrow , 1 , len(alarm_list) , format_green_left)
					alarmsheet.write(alarmrow , 2 , 'No Alarms' , format_green_left)
					alarmsheet.conditional_format( alarmrow , 0 , alarmrow , len(alarmsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
					alarmrow += 1
					local_alarms += 1
				else:
					alarmsheet.write(alarmrow , 1 , len(alarm_list) , format_red_left)
					for a in alarm_list:
						col = 2
						for c in a:
							alarmsheet.write(alarmrow , col , c)
							col += 1
						alarmsheet.conditional_format( alarmrow , 0 , alarmrow , len(alarmsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
						alarmsheet.conditional_format( alarmrow , 2 , alarmrow , 2, {'type': 'text' , 'criteria': 'containing', 'value': 'critical', 'format': format_red})
						alarmsheet.conditional_format( alarmrow , 2 , alarmrow , 2, {'type': 'text' , 'criteria': 'containing', 'value': 'major', 'format': format_amber})
						alarmsheet.conditional_format( alarmrow , 2 , alarmrow , 2, {'type': 'text' , 'criteria': 'containing', 'value': 'minor', 'format': format_yellow})
						alarmrow += 1
						local_alarms += 1
				draw_border(alarmsheet , alarmrow-local_alarms , 0 , alarmrow , len(alarmsheet_header))

			# Write circuit sheet if system is long haul or mux
			if 'ROADM' in system_type or 'Mux' in system_type:
				cctsheet.write(cctrow , 0 , reported_node_name)
				cctrowmarker = cctrow
				# freq_place = (5,6)
				# measurement_place = (12,13,14,15,16,17,18,19)
				# cctrow = cctrowmarker
				for skey , circuit in cct_db.items():
					for cctcol , (key, cct_item) in enumerate(circuit.items()):
						cctcol += 1
						if (key == 'mux_operational_state' or key == 'mux_controller_state' or key == 'additional_info' or key == 'control_mode') and cct_item:
							cctsheet.write_rich_string(cctrow , cctcol , *formatted(cct_item))
						elif 'frequency' in key.lower() or 'width' in key.lower():
							cctsheet.write(cctrow , cctcol , cct_item , format_freq)
						elif 'mc_freq_check' in key.lower():
							if cct_item == check_mark:
								cctsheet.write(cctrow , cctcol , cct_item , green_fmt)
							else:
								cctsheet.write(cctrow , cctcol , cct_item , red_fmt)
						elif any(x in key.lower() for x in ('expected', 'pwr')):
							cctsheet.write(cctrow , cctcol , cct_item , format_pwr)
						elif 'measured' in key.lower() and cct_item != '-':
							prefix = key.lower()[:key.lower().find('_', key.find('_') + 1) + 1]
							exp_value = circuit[prefix + 'expected_power']
							if abs(cct_item - exp_value) >= measured_expected_loss_diff:
								cctsheet.write(cctrow , cctcol , cct_item , format_pwr_red)
							else:
								cctsheet.write(cctrow , cctcol , cct_item , format_pwr_green)
						elif '_pwr' in key and isinstance(cct_item, (int,float)): # ccmd power levels
							if cct_item < min_ccmd_power_level:
								cctsheet.write(cctrow , cctcol , cct_item , format_pwr_red)
							else:
								cctsheet.write(cctrow , cctcol , cct_item , format_pwr_green)
						elif cct_item == '-':
							cctsheet.write(cctrow , cctcol , cct_item , format_centre)
						else:
							cctsheet.write(cctrow , cctcol , cct_item)
						cctsheet.conditional_format( cctrow , 0 , cctrow , len(cctsheet_btm_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
					cctrow += 1
				# If no circuits were found, still advance past the node-name row so the
				# next node doesn't overwrite this one.
				if not cct_db:
					cctrow += 1
				# draw_border(cctsheet , cctrowmarker , 0 , cctrow , len(cctsheet_btm_header))
				draw_border(cctsheet , cctrowmarker , 0 , cctrow , list(cctsheet_btm_header).index('Measured'))
				draw_border(cctsheet , cctrowmarker , list(cctsheet_btm_header).index('Measured') , cctrow , list(cctsheet_btm_header).index('Measured '))
				draw_border(cctsheet , cctrowmarker , list(cctsheet_btm_header).index('Measured ') , cctrow , list(cctsheet_btm_header).index('Measured  '))
				draw_border(cctsheet , cctrowmarker , list(cctsheet_btm_header).index('Measured  ') , cctrow , list(cctsheet_btm_header).index('Measured   '))
				draw_border(cctsheet , cctrowmarker , list(cctsheet_btm_header).index('Measured   ') , cctrow , len(cctsheet_btm_header))


			# Write AFC orchestrator results
			if afc_list:
				afcsheet.write(afcrow , 0 , reported_node_name)
				afc_rowmarker = afcrow
				afc_field_order = ('instance','mode','state_mode','state','result','compatibility','container_1','container_2','two_peak_result','action_wait_time','oms_view','last_update')
				for a in afc_list:
					for afc_idx, key in enumerate(afc_field_order):
						afcsheet.write(afcrow , afc_idx + 1 , a.get(key, ''))
					afcsheet.conditional_format( afcrow , 0 , afcrow , len(afcsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
					# State formatting (col 4)
					afcsheet.conditional_format( afcrow , 4 , afcrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'completed', 'format': format_green})
					afcsheet.conditional_format( afcrow , 4 , afcrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'in-progress', 'format': format_amber})
					afcsheet.conditional_format( afcrow , 4 , afcrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'aborted', 'format': format_red})
					afcsheet.conditional_format( afcrow , 4 , afcrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'failed', 'format': format_red})
					# Result formatting (col 5)
					afcsheet.conditional_format( afcrow , 5 , afcrow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'pass', 'format': format_green})
					afcsheet.conditional_format( afcrow , 5 , afcrow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'success', 'format': format_green})
					afcsheet.conditional_format( afcrow , 5 , afcrow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'fail', 'format': format_red})
					# Compatibility formatting (col 6)
					afcsheet.conditional_format( afcrow , 6 , afcrow , 6 , {'type': 'text' , 'criteria': 'containing', 'value': 'compatible', 'format': format_green})
					afcsheet.conditional_format( afcrow , 6 , afcrow , 6 , {'type': 'text' , 'criteria': 'containing', 'value': 'incompatible', 'format': format_red})
					afcrow += 1
				draw_border(afcsheet , afc_rowmarker , 0 , afcrow , len(afcsheet_header))

			# Store section data keyed by section name for use in Links sheet
			if section_list:
				network_sections[short_node_name] = {s['section_name']: s for s in section_list}


			# Write alarm history
			if options.alarm_history and alarm_history_list:
				hxsheet.write(hxrow , 0 , reported_node_name)
				hx_rowmarker = hxrow
				hx_field_order = ('history_id','alarm_id','name','severity','alarm_state','update_reason','history_time','raise_time','clear_time','cause','resource','direction','location','service_impact','condition_type','additional_info','user_notes')
				for h in alarm_history_list:
					for hx_idx, key in enumerate(hx_field_order):
						hxsheet.write(hxrow , hx_idx + 1 , h.get(key, ''))
					hxsheet.conditional_format( hxrow , 0 , hxrow , len(hxsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
					# Severity formatting (col 3)
					hxsheet.conditional_format( hxrow , 4 , hxrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'critical', 'format': format_red})
					hxsheet.conditional_format( hxrow , 4 , hxrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'major', 'format': format_amber})
					hxsheet.conditional_format( hxrow , 4 , hxrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'minor', 'format': format_yellow})
					# Alarm state formatting (col 5): cleared = 
					hxsheet.conditional_format( hxrow , 5 , hxrow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'cleared', 'format': format_green})
					hxsheet.conditional_format( hxrow , 5 , hxrow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'raised', 'format': format_red})
					hxrow += 1
				draw_border(hxsheet , hx_rowmarker , 0 , hxrow , len(hxsheet_header))

			# Write amp data if list is not empty
			if amp_list:
				ampsheet.write(amprow , 0 , reported_node_name)
				amprowmarker = amprow
				cntr_format_places = (9,10,11,12,13,14,15,17,18,19,20,21,22,23)
				for a in amp_list:
					for ampposn , i in enumerate(a):
						ampposn += 1
						if ampposn in cntr_format_places:
							ampsheet.write(amprow , ampposn , i , format_pwr)
						else:
							ampsheet.write(amprow , ampposn , i)
					ampsheet.conditional_format( amprow , 0 , amprow , len(ampsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
					# Format
					ampsheet.conditional_format( amprow , 3 , amprow , 3 , {'type': 'text' , 'criteria': 'containing', 'value': 'Enabled', 'format': format_green}) # Admin state
					ampsheet.conditional_format( amprow , 3 , amprow , 3 , {'type': 'text' , 'criteria': 'not containing', 'value': 'Enabled', 'format': format_red}) # Admin state
					ampsheet.conditional_format( amprow , 4 , amprow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'normal', 'format': format_green}) #  state
					ampsheet.conditional_format( amprow , 4 , amprow , 4 , {'type': 'text' , 'criteria': 'not containing', 'value': 'normal', 'format': format_red}) #  state
					ampsheet.conditional_format( amprow , 5 , amprow , 5 , {'type': 'text' , 'criteria': 'containing', 'value': 'Disabled', 'format': format_green}) # Forced shutoff
					ampsheet.conditional_format( amprow , 5 , amprow , 5 , {'type': 'text' , 'criteria': 'not containing', 'value': 'Disabled', 'format': format_red}) # Forced shutoff
					ampsheet.conditional_format( amprow , 6 , amprow , 6 , {'type': 'text' , 'criteria': 'containing', 'value': 'Good', 'format': format_green}) # Diagnostics
					ampsheet.conditional_format( amprow , 6 , amprow , 6 , {'type': 'text' , 'criteria': 'not containing', 'value': 'Good', 'format': format_red}) # Diagnostics
					ampsheet.conditional_format( amprow , 11 , amprow , 11 , {'type': 'cell' , 'criteria': '>', 'value': abs(gain_difference), 'format': format_red}) # Gain difference
					ampsheet.conditional_format( amprow , 11 , amprow , 11 , {'type': 'cell' , 'criteria': '<=', 'value': abs(gain_difference), 'format': format_green}) # Gain difference
					ampsheet.conditional_format( amprow , 14 , amprow , 14 , {'type': 'cell' , 'criteria': '>', 'value': abs(tilt_difference), 'format': format_red}) # Tilt difference
					ampsheet.conditional_format( amprow , 14 , amprow , 14 , {'type': 'cell' , 'criteria': '<=', 'value': abs(tilt_difference), 'format': format_green}) # Tilt difference
					ampsheet.conditional_format( amprow , 15 , amprow , 15 , {'type': 'cell' , 'criteria': '<', 'value': max_orl, 'format': format_red}) # ORL value
					ampsheet.conditional_format( amprow , 15 , amprow , 15 , {'type': 'cell' , 'criteria': '>=', 'value': max_orl, 'format': format_green}) # ORL value
					# ampsheet.conditional_format( amprow , 16 , amprow , 16 , {'type': 'text' , 'criteria': 'containing', 'value': 'valid', 'format': format_green}) # ORL State
					# ampsheet.conditional_format( amprow , 16 , amprow , 16 , {'type': 'text' , 'criteria': 'not containing', 'value': 'valid', 'format': format_red}) # ORL State
					amprow += 1
				draw_border(ampsheet , amprowmarker , 0 , amprow , 9)
				draw_border(ampsheet , amprowmarker , 9 , amprow , 12)
				draw_border(ampsheet , amprowmarker , 12 , amprow , 15)
				draw_border(ampsheet , amprowmarker , 15 , amprow , 17)
				draw_border(ampsheet , amprowmarker , 17 , amprow , 26)


			# Write Inventory
			inv_count = 1
			# write first row for system data
			asset_tag = adb_asset(serial_number)
			shelf_write_pattern = (reported_node_name , 'Shelf' , shelf_hw , shelf_type , '' , serial_number , '' , hw_release , '' , '' , asset_tag)
			for p , i in enumerate(shelf_write_pattern):
				invsheet.write( invrow, p , i)
			invsheet.conditional_format( invrow , 0 , invrow , len(invsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
			invrow += 1
			# Write slot data
			for i in inv_list:
				col = 1
				for p in i:
					invsheet.write( invrow , col , p)
					col += 1
				invsheet.conditional_format( invrow , 4 , invrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'Enabled', 'format': format_green})
				invsheet.conditional_format( invrow , 4 , invrow , 4 , {'type': 'text' , 'criteria': 'containing', 'value': 'Disabled', 'format': format_red})
				invsheet.conditional_format( invrow , 9 , invrow , 11 , {'type': 'text' , 'criteria': 'containing', 'value': 'Good', 'format': format_green})
				invsheet.conditional_format( invrow , 9 , invrow , 11 , {'type': 'text' , 'criteria': 'containing', 'value': 'Fail', 'format': format_red})
				invsheet.conditional_format( invrow , 0 , invrow , len(invsheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format}) })
				invrow += 1
				inv_count += 1
			draw_border(invsheet , invrow-inv_count , 0 , invrow , len(invsheet_header))

	def _collect_worker(host, pool):
		# Each worker gets its OWN requests.Session (cookie isolation) and,
		# for a non-seed node, leases one shell from the pool for the whole
		# node so that node's login cookies stay put. The seed is direct.
		_worker_ctx.session = requests.Session()
		_worker_ctx.tunnel = None
		try:
			if pool is not None and host != _seed_direct_host:
				with pool.lease() as shell:
					_worker_ctx.tunnel = shell
					return _collect_node(host)
			return _collect_node(host)
		finally:
			_worker_ctx.session = None
			_worker_ctx.tunnel = None

	def _collect_all_concurrent():
		# Wave-based BFS: drain the queue into a batch, collect the batch
		# concurrently, let discovery grow the queue, repeat until empty.
		# The seed is its own first wave (reached directly), so the pool is
		# opened lazily when the first neighbour wave appears.
		import concurrent.futures
		from utils.ssh_tunnel import SshSeedSessionPool
		records = {}
		pool = None
		creds = _tunnel_ssh_creds
		try:
			while True:
				batch = []
				while True:
					_h = walkq.next()
					if _h is None:
						break
					batch.append(_h)
				if not batch:
					break
				if (pool is None and creds
						and any(h != _seed_direct_host for h in batch)):
					log_callback(
						f"[SSH-POOL] opening {SSH_POOL_SIZE} shell(s) on "
						f"{_seed_direct_host}..."
					)
					try:
						pool = SshSeedSessionPool(
							_seed_direct_host, creds[0], creds[1],
							size=SSH_POOL_SIZE,
						).open()
						log_callback(f"[SSH-POOL] {pool.opened} shell(s) ready.")
					except Exception as exc:
						log_callback(
							f"[SSH-POOL] open failed ({exc}); collecting serially."
						)
						pool = None
				_workers = pool.opened if pool is not None else 1
				with concurrent.futures.ThreadPoolExecutor(
					max_workers=max(1, _workers)
				) as _ex:
					_futs = {_ex.submit(_collect_worker, h, pool): h
					         for h in batch}
					for _fut in concurrent.futures.as_completed(_futs):
						_h = _futs[_fut]
						try:
							records[_h] = _fut.result()
						except Exception as exc:
							log_callback(f"[{_h}] collect failed: {exc}")
							records[_h] = None
		finally:
			if pool is not None:
				pool.close()
				log_callback("[SSH-POOL] closed.")
		return records

	# Driver: serial by default (RLS_AUDIT_SSH_POOL=1). When >1, collect
	# nodes concurrently across the pool, then write the records back in
	# ``walkq.order`` (discovery order) so output stays deterministic.
	if SSH_POOL_SIZE > 1:
		_records = _collect_all_concurrent()
		for host in list(walkq.order):
			_rec = _records.get(host)
			if _rec is not None:
				_write_record(_rec)
			log_callback('')
	else:
		while True:
			host = walkq.next()
			if host is None:
				break
			_rec = _collect_node(host)
			if _rec is not None:
				_write_record(_rec)
			log_callback('')

	# Process links
	# This is done after all nodes have been captured as we need to know the cards on all systems to map the ports

	# Build a map of (from_node, to_node) -> measured_loss for bidirectional delta calculation.
	# Keying by endpoint pair (not link name) avoids collisions where multiple nodes share
	# identical link names (e.g. every ILA on a chain has a "PFG-1-2-LINEOUT").
	link_loss_map = {}
	for _n, _links in network_links.items():
		for _lnk in _links:
			_from_node = _lnk.get('From', (''))[0]
			_to_node   = _lnk.get('To',   (''))[0]
			_loss = _lnk.get('Measured Loss', '')
			if _from_node and _to_node and isinstance(_loss, (int, float)):
				link_loss_map[(_from_node, _to_node)] = _loss

	# Iterate in discovery order (``walkq.order``), not ``network_links``
	# insertion order: under concurrent collection the dict is populated in
	# completion order, which would scramble Links-sheet row order. Walk
	# order is stable across serial and concurrent runs. Nodes with no link
	# data (or that failed login) simply aren't in the dict -- skip them.
	for n in list(walkq.order):
		if n not in network_links:
			continue
		if hostlist.index(n) % 2 == 0:
			fill_format = fill_type1
		else:
			fill_format = fill_type2
		# Write to Link sheet
		link_write_pattern = list(linkworksheet_header.keys())[1:13]
		full_link_write_pattern = list(linkworksheet_header.keys())[1:]
		# log_callback(link_write_pattern)
		# log_callback(full_link_write_pattern)
		linkmarker = linkrow
		linkworksheet.write(linkrow , 0 , n)
		fmt_link = ('Expected Loss' , 'Measured Loss' , 'Loss Difference' , 'OSC Tx' , 'OSC Rx' , 'Calculated Expected Loss')
		fmt_losses = ('Assumed Fiber Loss (dB/Km)','Assumed Connector Loss')
		fmt_distance = ('Length')
		# log_callback(log_callback(json.dumps(network_links, indent=2, sort_keys=True)))
		for link_item in network_links[n]:
			if (link_item['Link Name'] != 'no data') and (link_item['Type'] != 'logical-line-fiber'):
				for p , i in enumerate(link_write_pattern):
					if i == 'To' or i == 'From': # Translate the port numbers to interface names
						item = lookup_port(link_item[i][0],link_item[i][1],link_item[i][2])
						# Remove host name if its on the same node
						if item.split()[0] == n:
							item = ' '.join(item.split()[1:])
					else:
						item = link_item[i]
					linkworksheet.write( linkrow , p + 1 , item)
					if i in fmt_link:
						linkworksheet.write( linkrow , p + 1 , item , format_pwr)
					elif i in fmt_distance:
						linkworksheet.write( linkrow , p + 1 , item , format_distance)
					else:
						linkworksheet.write( linkrow , p + 1 , item)
				if link_item['Length'] != '': # write fiber losses and calculated loss
					linkworksheet.write( linkrow , full_link_write_pattern.index('Assumed Fiber Loss (dB/Km)') + 1 , assumed_fiber_loss , format_losses)
					linkworksheet.write( linkrow , full_link_write_pattern.index('Assumed Connector Loss') + 1 , assumed_connector_loss , format_losses)
					cell_length = xl_rowcol_to_cell(linkrow , full_link_write_pattern.index('Length') + 1)
					fiber_loss_cell = xl_rowcol_to_cell(linkrow , full_link_write_pattern.index('Assumed Fiber Loss (dB/Km)') + 1)
					connector_loss_cell = xl_rowcol_to_cell(linkrow , full_link_write_pattern.index('Assumed Connector Loss') + 1)
					# write forumula to calculate expected loss
					linkworksheet.write( linkrow , full_link_write_pattern.index('Calculated Expected Loss') + 1 , f'={cell_length}*{fiber_loss_cell}+{connector_loss_cell}' , format_pwr)
					meas_loss_cell = xl_rowcol_to_cell(linkrow, full_link_write_pattern.index("Measured Loss") + 1, row_abs=False, col_abs=False)
					exp_loss_cell = xl_rowcol_to_cell(linkrow, full_link_write_pattern.index("Calculated Expected Loss") + 1, row_abs=False, col_abs=False)
					loss_formula = f"={meas_loss_cell}>{exp_loss_cell}"
					linkworksheet.conditional_format( linkrow , full_link_write_pattern.index('Measured Loss') + 1 , linkrow , link_write_pattern.index('Measured Loss') + 1,{'type':'formula','criteria':loss_formula,'format':format_red})
				# Write section fields (PFG Type, Cascaded, Associated Elements)
				sec = network_sections.get(n, {}).get(link_item['Link Name'], {})
				sec_col_start = full_link_write_pattern.index('Calculated Expected Loss') + 2
				linkworksheet.write(linkrow , sec_col_start     , sec.get('pfg_type', ''))
				linkworksheet.write(linkrow , sec_col_start + 1 , sec.get('cascaded', ''))
				linkworksheet.write(linkrow , sec_col_start + 2 , sec.get('associated_elements', ''))
				# Write bidirectional loss delta
				# Look up the reverse span by swapping (from_node, to_node) -> (to_node, from_node).
				# This is exact and works regardless of link naming conventions.
				bidir_col = full_link_write_pattern.index('Bidir Loss Delta') + 1
				from_node = link_item['From'][0]
				to_node   = link_item['To'][0]
				bidir_delta = ''
				if isinstance(link_item['Measured Loss'], (int, float)):
					rev_loss = link_loss_map.get((to_node, from_node))
					if rev_loss is not None:
						bidir_delta = round(abs(link_item['Measured Loss'] - rev_loss), 2)
				if isinstance(bidir_delta, float):
					linkworksheet.write(linkrow , bidir_col , bidir_delta , format_pwr)
					linkworksheet.conditional_format(linkrow , bidir_col , linkrow , bidir_col , {'type': 'cell' , 'criteria': '>' , 'value': bidir_loss_tolerance , 'format': format_pwr_red})
					linkworksheet.conditional_format(linkrow , bidir_col , linkrow , bidir_col , {'type': 'cell' , 'criteria': '<=' , 'value': bidir_loss_tolerance , 'format': format_pwr_green})
				else:
					linkworksheet.write(linkrow , bidir_col , bidir_delta)
				# Internal-fiber loss: intra-shelf patchcords are same-node 'fiber'
				# links (vs inter-node 'line-fiber' spans); their measured loss IS
				# the patch loss and should be ~0 dB. Flag green/red against
				# internal_fiber_loss_tolerance. Blank for inter-node spans.
				int_loss_col = full_link_write_pattern.index('Internal Loss (dB)') + 1
				if from_node and from_node == to_node and isinstance(link_item['Measured Loss'], (int, float)):
					linkworksheet.write(linkrow , int_loss_col , link_item['Measured Loss'] , format_pwr)
					linkworksheet.conditional_format(linkrow , int_loss_col , linkrow , int_loss_col , {'type': 'cell' , 'criteria': '>' , 'value': internal_fiber_loss_tolerance , 'format': format_pwr_red})
					linkworksheet.conditional_format(linkrow , int_loss_col , linkrow , int_loss_col , {'type': 'cell' , 'criteria': '<=' , 'value': internal_fiber_loss_tolerance , 'format': format_pwr_green})
				else:
					linkworksheet.write(linkrow , int_loss_col , '')
				linkworksheet.conditional_format( linkrow , 0 , linkrow , len(linkworksheet_header)-1 , {'type' : 'no_errors' , 'format' : workbook.add_format({'bg_color' : '#' + fill_format})})
				# Format link diagnostics 
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('Link Diagnostics')+1 , linkrow , link_write_pattern.index('Link Diagnostics')+1,{'type':'text','criteria':'containing','value':'Good','format':format_green})
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('Link Diagnostics')+1 , linkrow , link_write_pattern.index('Link Diagnostics')+1,{'type':'text','criteria':'not containing','value':'Good','format':format_red})
				# Format OSC power levels and loss difference
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('OSC Rx')+1 , linkrow , link_write_pattern.index('OSC Rx')+1,{'type':'cell','criteria':'<','value':min_osc_rx_power,'format':format_red})
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('OSC Rx')+1 , linkrow , link_write_pattern.index('OSC Rx')+1,{'type':'cell','criteria':'>=','value':min_osc_rx_power,'format':format_green})
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('Loss Difference')+1 , linkrow , link_write_pattern.index('Loss Difference')+1,{'type':'cell','criteria':'>','value':span_loss_difference,'format':format_red})
				linkworksheet.conditional_format( linkrow , link_write_pattern.index('Loss Difference')+1 , linkrow , link_write_pattern.index('Loss Difference')+1,{'type':'cell','criteria':'<=','value':span_loss_difference,'format':format_green})
				# except:
				# 	pass
				linkrow += 1
		draw_border(linkworksheet , linkmarker , 0 , linkrow , len(linkworksheet_header))

	if options.debug:
		log_callback(network_inventory)
		log_callback(network_links)

	if options.outfile:
		# Hide worksheet if only metro system
		if not lh_network:
			cctsheet.hide()
		log_callback(f"[AUDIT] Writing workbook ({node_count} node(s)) -> {outputfilename}")
		from xlsxwriter.exceptions import FileCreateError
		try:
			workbook.close()
		except (FileCreateError, PermissionError) as exc:
			# The original CLI looped on ``input()`` to let the operator
			# close Excel and retry, but the GUI has no stdin -- block
			# would deadlock. Surface a clear abort instead so the
			# operator can free the file and re-run.
			return _audit_abort(
				log_callback,
				f"Could not write '{outputfilename}'."
				" The file is open in another application (typically Excel)."
				" Close it and re-run the audit.",
			)
		log_callback('')
		log_callback('Results stored in' , outputfilename)
		log_callback('')

	if options.dump_file:
		node_workbook.close()
		log_callback('')
		log_callback('Rawdump file created : ' , dumpfile)
		log_callback('')

	log_callback(node_count , 'nodes queried')
	# Tear down the SSH tunnel (if we opened one). Idempotent + no-op
	# when no tunnel was used. Done before the "complete" banner so
	# any cleanup messages stay next to the related work in the log.
	if _jump_tunnel is not None:
		try:
			_jump_tunnel.close()
			log_callback("[SSH-TUNNEL] Closed.")
		except Exception as exc:
			log_callback(f"[SSH-TUNNEL] Close error: {exc}")
		globals()["_jump_tunnel"] = None
	log_callback('')
	log_callback(' === Audit complete ===')
	log_callback('')
	if options.outfile and options.openexcel:
		log_callback('Opening' , outputfilename , '...')
		# Cross-platform file open
		if sys.platform == 'win32':
			os.startfile(outputfilename)
		else:
			os.system("open -a 'Microsoft Excel.app' " + outputfilename)
