"""One-shot adapter — convert the standalone reference RLS audit CLI
into a function-callable ATLAS module. Run once, then delete; kept here
in case the upstream reference gets updated and the adaptation needs
to be re-applied.

Reference structure:
  [1..banner_start)           imports + constants + debug_log    KEEP
  [banner_start..helpers)     banner + argparse + wizard         STRIP
  [helpers..flow)             helper FUNCTIONS                   KEEP
  [flow..EOF)                 main flow                          WRAP
"""
from __future__ import annotations
import re
from pathlib import Path

p = Path("scripts/Network/RLS_Audit.py")
src = p.read_text(encoding="utf-8")
lines = src.splitlines(keepends=False)


def find_line(needle, start=0):
    for i, ln in enumerate(lines[start:], start=start):
        if needle in ln:
            return i
    raise SystemExit(f"Marker not found: {needle!r}")


banner_start = find_line("title = 'Ciena RLS Node Audit", 0)
helpers_start = find_line("def _summarize_afc_container", 0)
flow_start = find_line("session = requests.Session()", 0)
assert banner_start < helpers_start < flow_start

# The reference interleaves helper FUNCTION DEFS with module-level
# main-flow setup (workbook creation, sheet headers, format defs).
# We want only the function defs to stay at module level. Use AST to
# partition the [helpers_start..flow_start) range into:
#   * function defs            -> keep at module level
#   * everything else          -> push into run_audit before the main flow
import ast

pre = lines[:banner_start]
mid_block_lines = lines[helpers_start:flow_start]
mid_block_src = "\n".join(mid_block_lines)
mid_tree = ast.parse(mid_block_src)

# Build a set of line-ranges that belong to function definitions in the
# mid block (line numbers are 1-based, relative to the start of
# mid_block_src).
func_ranges = []
for node in mid_tree.body:
    if isinstance(node, ast.FunctionDef):
        # ast doesn't track trailing blank lines after the last
        # statement; use end_lineno (Py3.8+).
        func_ranges.append((node.lineno - 1, node.end_lineno))

helper_funcs = []   # function defs to keep at module level
mid_setup = []      # everything else, will be appended to run_audit body
in_func_until = -1
for idx, ln in enumerate(mid_block_lines):
    in_func = any(start <= idx < end for start, end in func_ranges)
    if in_func:
        helper_funcs.append(ln)
    else:
        mid_setup.append(ln)

helpers = helper_funcs
flow = mid_setup + lines[flow_start:]


def adapt_line(line: str) -> str:
    """Scrub ANSI tokens used as args; swap print/sys.exit."""
    line = re.sub(r"\b(?:blue|green|red|amber|yellow|resetc)\s*,\s*", "", line)
    line = re.sub(r",\s*\b(?:blue|green|red|amber|yellow|resetc)\b\s*", "", line)
    line = re.sub(r"\b(?:blue|green|red|amber|yellow|resetc)\b\s*", "", line)
    line = re.sub(r",\s*,", ",", line)
    line = re.sub(r"\(\s*,\s*", "(", line)
    line = re.sub(r",\s*\)", ")", line)
    line = re.sub(r"\bprint\s*\(", "log_callback(", line)
    line = re.sub(r"\blog_callback\s*\(\s*\)", "log_callback('')", line)
    line = re.sub(r"\bsys\.exit\s*\(", "return _audit_abort(log_callback,", line)
    return line


indented_flow = ["" if adapt_line(ln) == "" else "\t" + adapt_line(ln)
                 for ln in flow]
adapted_helpers = [adapt_line(ln) for ln in helpers]

new_header = '''
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
blue = "\\033[94m"
green = "\\033[92m"
red = "\\033[91m"
amber = "\\033[93m"
yellow = "\\033[93m"
resetc = "\\033[0m"


def _audit_abort(log_callback, *args):
\t"""Used in place of ``sys.exit`` inside ``run_audit`` -- log + raise."""
\tmsg = " ".join(str(a) for a in args) if args else "Audit aborted."
\tlog_callback(msg)
\traise RuntimeError(msg)


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

# ``options`` namespace the original CLI populated via argparse.
# Helpers (``get_data``, alarm collectors, etc.) reference fields
# like ``options.debug`` and ``options.alarms``; ``run_audit``
# reassigns it at function entry.
options = SimpleNamespace(
\tusername="",
\tpassword="",
\tuse_keychain=False,
\tnodes=None,
\tseedfile=None,
\toutfile=None,
\talarms=False,
\talarm_limit=None,
\talarm_history=False,
\tdiscover=True,
\topenexcel=False,
\tassumed_fiber_loss=None,
\tassumed_connector_loss=None,
\tdebug=False,
\tdump_file=None,
)

'''

run_audit_def = '''
def run_audit(
\tseed_host,
\tusername,
\tpassword,
\toutput_path,
\t*,
\tcapture_alarms=False,
\tcapture_alarm_history=False,
\tdebug=False,
\tlog_callback=None,
):
\t"""Run an RLS REST-based network audit starting from *seed_host*.

\tArgs:
\t\tseed_host: One IP or hostname to start the walk from. ATLAS calls
\t\t\twith a single seed; the reference script's seedfile/-n options
\t\t\thave been dropped -- the REST call
\t\t\t``restconf/data/ciena-6500r-nodes:nodes=*`` discovers the rest
\t\t\tof the topology from this entry point.
\t\tusername, password: RLS RESTCONF credentials.
\t\toutput_path: Absolute path to write the .xlsx audit report to.
\t\tcapture_alarms: When True, also collect active alarms per node.
\t\tcapture_alarm_history: When True, also collect alarm history.
\t\tdebug: When True, the audit logs verbose REST status info.
\t\tlog_callback: Function taking a single str argument. Called for
\t\t\tevery line the audit would have ``print()``-ed. Defaults to
\t\t\t``print`` for standalone use.

\tReturns:
\t\tNumber of nodes successfully queried.

\tRaises:
\t\tRuntimeError on any audit-level abort (DNS, no nodes, bad input).
\t"""
\t# Wrap the user-supplied callback so the ``print(a, b, c, end='\\r')``
\t# call sites adapted from the original CLI still work. The original
\t# called ``print`` directly with multiple args + ``end='\\r'`` for
\t# progress carriage-returns; the regex adapter rewrote those as
\t# ``log_callback(...)`` but a typical UI sink takes a single str.
\t_user_log_callback = log_callback or print
\tdef log_callback(*args, **kwargs):
\t\tend = kwargs.pop("end", "\\n")
\t\tsep = kwargs.pop("sep", " ")
\t\tflush = kwargs.pop("flush", False)  # noqa: F841 — accepted, ignored
\t\tmsg = sep.join(str(a) for a in args)
\t\t# Strip trailing newline since most GUI panels append their own,
\t\t# but keep ``end='\\r'`` (or other custom ends) so progress-style
\t\t# output still renders sensibly on stdout fallbacks.
\t\tif end and end != "\\n":
\t\t\tmsg = msg + end
\t\t_user_log_callback(msg)
\t# Reassign module-level state so helpers picking these up via their
\t# lexical scope see this run's settings, and so per-sheet row
\t# counters reset between runs. The original CLI used these as
\t# module globals; the function body would otherwise treat any name
\t# it assigns as local and UnboundLocalError on first read.
\tglobal options
\tglobal systemrow, linkrow, amprow, alarmrow, cvrow, afcrow, hxrow
\tglobal cctrow, invrow, orl_cell_width, lh_network
\t# State that helper functions read by lexical scope.
\tglobal workbook, session, network_inventory
\tglobal format_green, format_red, format_green_left, format_red_left
\tglobal format_lookup
\t# Reset the per-sheet row counters to their starting values (the
\t# reference initialized them once at module top; we re-initialize
\t# every call so back-to-back run_audit() invocations don't
\t# accumulate row positions).
\tsystemrow = 2
\tlinkrow = 2
\tamprow = 2
\talarmrow = 2
\tcvrow = 2
\tafcrow = 2
\thxrow = 2
\tcctrow = 2
\tinvrow = 2
\torl_cell_width = 10
\tlh_network = False
\toptions = SimpleNamespace(
\t\tusername=username,
\t\tpassword=password,
\t\tuse_keychain=False,
\t\tnodes=[seed_host],
\t\tseedfile=None,
\t\toutfile=output_path,
\t\talarms=capture_alarms,
\t\talarm_limit=None,
\t\talarm_history=capture_alarm_history,
\t\tdiscover=True,
\t\topenexcel=False,
\t\tassumed_fiber_loss=None,
\t\tassumed_connector_loss=None,
\t\tdebug=debug,
\t\tdump_file=None,
\t)
\thostlist = [seed_host]
\tif options.outfile and not options.outfile.endswith(".xlsx"):
\t\toutputfilename = options.outfile + ".xlsx"
\telse:
\t\toutputfilename = options.outfile
\tpayload = {"username": options.username, "password": options.password}
\theaders = {"Content-Type": "application/json"}
\tnode_workbook = None
\tdumpfile = ""

'''

out_lines = []
out_lines.extend(pre)
out_lines.append(new_header.lstrip("\n"))
out_lines.extend(adapted_helpers)
out_lines.append(run_audit_def.lstrip("\n"))
out_lines.extend(indented_flow)
p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
print(f"Wrote {p}: {len(out_lines)} lines")
