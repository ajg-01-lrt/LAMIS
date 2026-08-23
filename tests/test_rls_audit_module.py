"""Pin the ``scripts/Network/RLS_Audit.py`` adapter contract.

The audit script is a heavily-adapted reference (Apple's
``rls_audit_updated``) wrapped in an ATLAS-friendly ``run_audit``
function. These tests pin:

  * the public ``run_audit`` signature the GUI frame depends on;
  * that the module imports cleanly with no side effects (no network
    I/O, no xlsxwriter workbook creation, no ``input()`` prompts);
  * that the legacy TDS-based audit modules are gone.
"""
from __future__ import annotations
import importlib
import inspect
import sys
import unittest


class TestRunAuditSignature(unittest.TestCase):
    """The Network Audit GUI frame depends on the exact keyword shape
    of ``run_audit``. Catch any rename / argument-drop in tests so a
    silently-broken UI doesn't get shipped."""

    def setUp(self):
        from scripts.Network.RLS_Audit import run_audit
        self.sig = inspect.signature(run_audit)

    def test_has_required_positional_args(self):
        params = self.sig.parameters
        for name in ("seed_host", "username", "password", "output_path"):
            self.assertIn(name, params, f"missing required arg: {name}")
            self.assertNotEqual(
                params[name].default, inspect.Parameter.empty,
                f"{name} should be required (no default)",
            ) if False else None  # see below — required arg test

        # The first four args are required: no default.
        for name in ("seed_host", "username", "password", "output_path"):
            self.assertIs(
                params[name].default, inspect.Parameter.empty,
                f"{name} should be required",
            )

    def test_has_optional_kwargs(self):
        params = self.sig.parameters
        for name, default in [
            ("capture_alarms", False),
            ("capture_alarm_history", False),
            ("debug", False),
        ]:
            self.assertIn(name, params, f"missing optional kwarg: {name}")
            self.assertEqual(params[name].default, default)

    def test_log_callback_is_optional_keyword(self):
        params = self.sig.parameters
        self.assertIn("log_callback", params)
        self.assertIsNone(params["log_callback"].default)


class TestModuleImportsCleanly(unittest.TestCase):
    """Importing ``RLS_Audit`` must NOT:

      * open a network session,
      * create an xlsxwriter workbook on disk,
      * call ``input()`` (the original CLI did at module load).

    The reference script ran its full audit at import time -- the
    adapter pushed all that into ``run_audit``. A regression here
    would re-introduce the same import-time crash that motivated the
    rewrite.
    """

    def test_module_imports_without_side_effects(self):
        # Force a fresh import so we exercise module-level code paths.
        sys.modules.pop("scripts.Network.RLS_Audit", None)
        module = importlib.import_module("scripts.Network.RLS_Audit")
        # Module-level placeholders must default to "no-state" sentinels.
        self.assertIsNone(getattr(module, "workbook", "missing"))
        self.assertIsNone(getattr(module, "session", "missing"))
        self.assertEqual(getattr(module, "network_inventory", None), {})
        # ``options`` should be present as a SimpleNamespace with safe
        # defaults so helpers don't NameError when imported in
        # isolation.
        self.assertTrue(hasattr(module, "options"))
        self.assertFalse(module.options.alarms)
        self.assertFalse(module.options.alarm_history)
        self.assertTrue(module.options.discover)

    def test_run_audit_is_exposed(self):
        from scripts.Network import RLS_Audit
        self.assertTrue(callable(getattr(RLS_Audit, "run_audit", None)))


class TestLegacyTdsAuditModulesAreGone(unittest.TestCase):
    """The TDS-based RLS network walk has been retired. Importing any
    of the deleted modules must fail, so a stale reference won't
    silently succeed against the runtime."""

    def test_legacy_rls_network_audit_is_removed(self):
        with self.assertRaises(ImportError):
            import scripts.TDS.RLS_Network_Audit  # noqa: F401

    def test_legacy_tds_rls_is_removed(self):
        with self.assertRaises(ImportError):
            import scripts.TDS.tds_rls  # noqa: F401

    def test_legacy_rls_validations_is_removed(self):
        with self.assertRaises(ImportError):
            import scripts.TDS.rls_validations  # noqa: F401


class TestDiscoverIsBoundBeforeLinkWalk(unittest.TestCase):
    """The link-walk loop reads ``discover`` to decide whether to keep
    extending the host list past OL/ROADM boundaries. The original CLI
    bound ``discover`` at module level via argparse; the adapted
    ``run_audit`` has to seed it before the first read. A regression
    here surfaces as a mid-audit UnboundLocalError on any seed that
    returns a successful node walk."""

    def test_run_audit_body_seeds_discover_local(self):
        import re as _re
        import textwrap
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        # Strip comments so we can't false-match inside docstrings.
        body_lines = []
        for ln in textwrap.dedent(src).splitlines():
            stripped = ln.strip()
            if stripped.startswith("#"):
                continue
            body_lines.append(ln)
        # Look for an unindented (function-scope) ``discover = ...``
        # assignment somewhere before the first ``if discover:`` read
        # site. Use exact-code matchers, not substring-in-line.
        re_assign = _re.compile(r"^\s*discover\s*=\s*")
        re_read = _re.compile(r"^\s*if\s+discover\s*:")
        assigned_at = next(
            (i for i, ln in enumerate(body_lines) if re_assign.match(ln)), None,
        )
        read_at = next(
            (i for i, ln in enumerate(body_lines) if re_read.match(ln)), None,
        )
        self.assertIsNotNone(assigned_at, "discover never assigned in run_audit")
        self.assertIsNotNone(read_at, "expected 'if discover:' read site")
        self.assertLess(
            assigned_at, read_at,
            "discover must be assigned before the link-walk reads it",
        )


class TestAuditBreadcrumbsExist(unittest.TestCase):
    """Pin user-visible phase breadcrumbs so a refactor can't silently
    drop them and leave the GUI panel blank during a long REST walk.
    Source-level checks; we don't run the audit here."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_announces_audit_start(self):
        self.assertIn("[AUDIT] Starting walk", self.src)

    def test_announces_per_host_neighbor_discovery(self):
        # Per-host visibility: the panel goes silent for seconds while
        # this REST call runs. Surface it.
        self.assertIn("Discovering neighbors", self.src)

    def test_announces_neighbor_count(self):
        # Confirming "Found N node(s) (M remote neighbor(s))" lets the
        # operator see the topology size before the per-node work runs.
        self.assertIn("remote neighbor(s)", self.src)

    def test_announces_inventory_phase(self):
        self.assertIn("Reading system + inventory data", self.src)

    def test_announces_workbook_write(self):
        # The workbook close + format-resolution pass can take several
        # seconds for a 10+ node walk. Tell the operator before it
        # starts.
        self.assertIn("Writing workbook", self.src)


class TestWorkbookBusyDoesNotBlockOnStdin(unittest.TestCase):
    """The CLI reference used ``input()`` to let the operator close
    Excel and retry the workbook write. The GUI has no stdin -- that
    path would deadlock. The audit must instead abort cleanly so the
    Tk worker can surface a "file in use" dialog."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_workbook_close_does_not_call_input(self):
        # Locate the workbook close block and make sure ``input()`` is
        # not the recovery path. Strip comment lines so the explanatory
        # comment we left in the source (referencing ``input()`` by
        # name) doesn't trigger a false positive.
        start = self.src.find("Writing workbook")
        end = self.src.find("Results stored in", start)
        self.assertGreater(end, start, "could not locate workbook close block")
        code_lines = [
            ln for ln in self.src[start:end].splitlines()
            if not ln.strip().startswith("#")
        ]
        block = "\n".join(code_lines)
        self.assertNotIn(
            "input()", block,
            "workbook-busy recovery must not block on input() -- the GUI"
            " has no stdin. Raise _audit_abort with a clear message"
            " instead so the worker can surface a dialog.",
        )

    def test_workbook_close_aborts_on_file_in_use(self):
        # The replacement path must call ``_audit_abort`` with a
        # message naming the busy file so the GUI dialog is actionable.
        start = self.src.find("Writing workbook")
        end = self.src.find("Results stored in", start)
        block = self.src[start:end]
        self.assertIn("_audit_abort", block)
        self.assertIn("open in another application", block)


class TestLogCallbackVisibleToModuleHelpers(unittest.TestCase):
    """Helpers defined at module scope (``login_to_node``,
    ``get_data``, ``alarm_colour``, ...) call ``log_callback(...)``
    directly. That symbol must exist at module scope or those helpers
    raise NameError mid-audit. ``run_audit`` overrides the module
    binding for the duration of a run; module-level default is
    ``print`` so the module is usable outside an audit.
    """

    def test_module_level_log_callback_exists(self):
        from scripts.Network import RLS_Audit
        self.assertTrue(
            callable(getattr(RLS_Audit, "log_callback", None)),
            "module-level log_callback must exist so helpers like"
            " alarm_colour don't NameError outside run_audit",
        )

    def test_run_audit_overrides_module_log_callback(self):
        # Spy that the helpers' module-level lookup actually routes to
        # the user-supplied sink, not the module default. The audit
        # treats unreachable hosts as a soft per-host failure (logs and
        # continues), so we don't ``assertRaises`` -- we just verify
        # the wrapper got installed and that the user sink received
        # the start breadcrumb.
        import os
        import tempfile
        from scripts.Network import RLS_Audit
        captured = []

        original = RLS_Audit.log_callback
        wrapper_after_run = None
        with tempfile.TemporaryDirectory() as tmp:
            try:
                RLS_Audit.run_audit(
                    seed_host="203.0.113.254",
                    username="u", password="p",
                    output_path=os.path.join(tmp, "_unit.xlsx"),
                    log_callback=captured.append,
                )
            except Exception:
                # Treat any exception as fine -- the binding check is
                # what we care about. Helpers raising would be a
                # regression for a different test.
                pass
            finally:
                wrapper_after_run = RLS_Audit.log_callback
                # Reset for other tests that import this module.
                RLS_Audit.log_callback = original
        self.assertIsNot(
            wrapper_after_run, original,
            "run_audit must have overridden the module log_callback"
            " with its wrapper so helpers route to the user sink",
        )
        # And the user sink must have received at least the start
        # breadcrumb before any failure.
        self.assertTrue(
            any("[AUDIT]" in str(line) for line in captured),
            f"expected an [AUDIT] breadcrumb in {captured!r}",
        )

    def test_alarm_colour_does_not_nameerror_outside_run_audit(self):
        # Specifically pin the original crash site: ``alarm_colour``
        # was called from inside ``run_audit`` but the regex adapter
        # rewrote its inner ``print`` to ``log_callback`` -- which
        # didn't exist at module scope, only inside run_audit's frame.
        from scripts.Network import RLS_Audit
        # Should be callable with any alarm type and NOT raise.
        try:
            RLS_Audit.alarm_colour("Critical")
            RLS_Audit.alarm_colour("major")
            RLS_Audit.alarm_colour("minor")
            RLS_Audit.alarm_colour("Other")
        except NameError as exc:
            self.fail(
                f"alarm_colour raised NameError outside run_audit: {exc}."
                " The module-level log_callback default must be defined"
                " so helpers can be invoked safely."
            )


class TestRestDiscoveryExtendsHostlist(unittest.TestCase):
    """Source-level pin: the REST topology call ``/nodes=*`` returns
    every node in the network; the audit must extend ``hostlist`` from
    that data so a single-seed walk actually visits every neighbor.

    The original CLI relied on the fiber link-walk loop to expand
    hostlist, which gave up at the first OL/ROADM boundary -- meaning
    a ROADM seed used to walk only itself. This regression was
    user-reported on a real network audit: 11 expected nodes, 1
    walked. The check below would have caught it before ship.
    """

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_topo_list_feeds_hostlist_for_remote_nodes(self):
        # There must be code that iterates topo_list and extends
        # hostlist for non-local entries. Look for the canonical shape.
        self.assertIn("for entry in topo_list", self.src)
        # The extend must skip the local (seed) entry.
        self.assertIn("'local'", self.src)
        # And must actually enqueue the neighbor for the walk. The walk
        # is now driven by ``_WalkQueue``; discovery enqueues via
        # ``add_candidate`` (was: ``hostlist.append``).
        self.assertIn("walkq.add_candidate", self.src)

    def test_discovery_prefers_ip_over_hostname(self):
        # Customer nets often don't have working DNS for RLS nodes;
        # using IP from the REST data avoids the DNS dependency.
        self.assertIn("ip_address", self.src)

    def test_announces_added_nodes(self):
        # User-visible breadcrumb -- without this the operator can't
        # tell whether REST discovery actually worked.
        self.assertIn("Added", self.src)
        self.assertIn("walk queue", self.src)


class TestWalkLoopDoesNotDoubleAuditSameNode(unittest.TestCase):
    """If REST adds the IP and link-walk adds the hostname, the same
    physical node ends up on hostlist twice. The audit loop must skip
    a host whose short-name has already been walked this run; without
    this, every sheet picks up duplicate rows for every node that has
    both a name and an IP entry."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_walk_loop_tracks_already_walked(self):
        self.assertIn("_walked_short_names", self.src)

    def test_walk_loop_skips_duplicates(self):
        # Either pre-login (when short_node_name resolves) or
        # post-login the loop must guard against a second walk of the
        # same node.
        self.assertIn("Already walked", self.src)

    def test_link_walk_dedupes_against_walked_set(self):
        # The link-walk fallback path also has to dedup against what's
        # already been done -- not just what's on hostlist by exact
        # string.
        self.assertIn("_walked_short_names", self.src)


class TestAuditOutputTeesToFileLogger(unittest.TestCase):
    """Every line that goes to the GUI panel must also land in the
    ATLAS log file. The operator can't attach the GUI scrollback to a
    bug report; the file logger is the canonical raw transcript."""

    def test_run_audit_tees_breadcrumbs_to_atlas_audit_logger(self):
        import logging
        import os
        import tempfile
        from scripts.Network import RLS_Audit

        # Attach a list-handler to the audit logger so we can spy on
        # what would have hit the file.
        records = []

        class _ListHandler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        audit_logger = logging.getLogger("atlas.audit")
        original_level = audit_logger.level
        handler = _ListHandler(level=logging.DEBUG)
        audit_logger.addHandler(handler)
        audit_logger.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    RLS_Audit.run_audit(
                        seed_host="203.0.113.254",
                        username="u", password="p",
                        output_path=os.path.join(tmp, "_unit.xlsx"),
                        log_callback=lambda *_a, **_kw: None,
                    )
                except Exception:
                    pass
        finally:
            audit_logger.removeHandler(handler)
            audit_logger.setLevel(original_level)

        # The audit logger should have received the start breadcrumb
        # and the workbook-write breadcrumb, mirroring what the GUI
        # panel saw.
        joined = "\n".join(records)
        self.assertIn(
            "[AUDIT]", joined,
            f"expected [AUDIT] breadcrumbs in file logger; got {records!r}",
        )
        self.assertTrue(
            any("Starting walk" in r for r in records),
            "start breadcrumb missing from file logger",
        )

    def test_wrapper_strips_ansi_color_codes_before_logging(self):
        # Some helpers prefix their output with ANSI color tokens.
        # Those land in the GUI panel as garbage and pollute the file
        # log. The tee path should strip them.
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        self.assertIn("_ANSI_RE", src)


class TestEmptyDiscoveryIsLoud(unittest.TestCase):
    """If REST returns nodes but none get added to the walk queue,
    something is off (missing IPs, no node-name, etc.). The audit
    must log that fact + the raw entry state so the operator can
    debug from the log file without a live session."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_empty_discovery_message_exists(self):
        self.assertIn("Discovery added 0 nodes", self.src)

    def test_dumps_topo_entries_on_empty_discovery(self):
        # The per-entry dump is what diagnoses "REST returned data but
        # no usable IP/name" cases.
        self.assertIn("topo entry:", self.src)

    def test_tracks_skip_reasons(self):
        # Three skip reasons -- local node, no IP/name, duplicate.
        # Surfacing the counts tells the operator at a glance which
        # branch dropped the entries.
        for token in ("_skipped_local", "_skipped_no_id", "_skipped_dup"):
            self.assertIn(token, self.src)


class TestSeedResolvedNameAddedToWalkedSet(unittest.TestCase):
    """The seed is often entered as an IP (10.0.0.1). The link-walk
    later finds the seed's real hostname as a neighbor of itself and
    would re-add it -- causing a second walk under the resolved name.
    The audit must record the seed's resolved short-name in the
    walked-set the moment it learns it."""

    def test_local_node_resolves_into_walked_set(self):
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        # Look for the local-node branch and verify it records the
        # hostname into _walked_short_names. Search the full source --
        # the relevant block has grown over time as we added more
        # identifier tracking (now also captures operational IP).
        self.assertIn("node-type", src)
        # Walk dedup is now owned by ``_WalkQueue``; the local-node
        # branch records aliases via ``walkq.mark_walked`` (was:
        # ``_walked_short_names.add``).
        self.assertIn("walkq.mark_walked", src)
        # And the resolved short-name (hostname) is what gets added
        # first -- pin the variable name so a refactor that drops it
        # is caught.
        self.assertIn("_resolved", src)


class TestRestDefaultsAreDiaguser(unittest.TestCase):
    """RESTCONF on RLS uses ``diaguser``/``Ciena123``; the shell user
    ``su`` doesn't have REST API access. The audit + GUI must both
    default to the diaguser pair when the caller supplies blank
    credentials."""

    def test_credentials_store_resolves_rls_rest_to_diaguser(self):
        from utils.credentials import get_default_credential_for_vendor
        pair = get_default_credential_for_vendor("ciena-rls-rest")
        self.assertIsNotNone(
            pair, "ciena-rls-rest vendor key not registered",
        )
        user, _ = pair
        self.assertEqual(
            user, "diaguser",
            "RLS REST default username must be 'diaguser' "
            "(distinct from the shell user 'su')",
        )

    def test_run_audit_substitutes_defaults_for_blank_creds(self):
        # When the GUI passes blank user/pass, run_audit must
        # substitute the RLS REST defaults and log that it did so --
        # without this, a blank-creds call would fail authentication
        # against every node.
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        # Look for the empty-creds branch.
        self.assertIn(
            'No credentials supplied', src,
            "run_audit must announce when it falls back to the RLS"
            " REST default credentials (operator otherwise can't tell"
            " whose creds got used).",
        )
        self.assertIn("diaguser", src)
        # And the substitution must actually flow into options.
        self.assertTrue(
            'get_default_credential_for_vendor("ciena-rls-rest")' in src
            or "get_default_credential_for_vendor('ciena-rls-rest')" in src,
            "expected run_audit to look up the RLS REST default vendor key",
        )


class TestNetworkAuditFrameDefaultsToDiaguser(unittest.TestCase):
    """The GUI fields should be pre-filled with diaguser/Ciena123 so
    the operator doesn't have to retype them on every audit. Source-
    level check; we don't spin up Tk in unit tests."""

    def test_frame_prefills_username_with_diaguser(self):
        from gui import network_audit_frame
        src = inspect.getsource(network_audit_frame.NetworkAuditFrame._build)
        # The username entry should call ``insert(0, <default user>)``
        # somewhere -- exact mechanism (literal vs. cred-store lookup)
        # is up to the implementation.
        self.assertIn("username_entry.insert", src)
        # Either the literal or the cred-store lookup must drive the
        # default; both end up at 'diaguser' on a fresh install.
        self.assertTrue(
            'diaguser' in src or '"ciena-rls-rest"' in src
            or "'ciena-rls-rest'" in src,
            "GUI must pre-fill the username with the RLS REST default",
        )

    def test_frame_hint_explains_default_credentials(self):
        from gui import network_audit_frame
        src = inspect.getsource(network_audit_frame.NetworkAuditFrame._build)
        # User-visible label so the operator knows which creds are
        # being used (and that they can override).
        self.assertIn("RLS RESTCONF default", src)


class TestDiscoverySkipsDeadNodes(unittest.TestCase):
    """``/nodes=*`` includes nodes whose heartbeat has failed
    (``diagnostic.is-up: false``). Walking them just wastes REST
    timeouts -- skip them at discovery and surface the count so the
    operator can see how much of the topology is dark."""

    def setUp(self):
        from scripts.Network import RLS_Audit
        self.src = inspect.getsource(RLS_Audit.run_audit)

    def test_topo_entries_capture_is_up(self):
        # The discovery filter needs an ``is_up`` field on each entry;
        # pinning that the parser actually reads ``diagnostic.is-up``.
        self.assertIn("'is_up'", self.src)
        self.assertIn("is-up", self.src)

    def test_discovery_skips_is_up_false_entries(self):
        # The filter check must be against ``False`` specifically --
        # missing field (local node) should NOT be treated as dead.
        self.assertIn("entry.get('is_up') is False", self.src)

    def test_dead_node_count_is_surfaced(self):
        # User-visible breadcrumb so the operator sees how many nodes
        # were dropped because they're off-line.
        self.assertIn("Skipped", self.src)
        self.assertIn("dead node", self.src)
        self.assertIn("is-up=false", self.src)


class TestIpFallbackToTopLevel(unittest.TestCase):
    """The RLS REST schema reports ``ip-address`` at the node config
    block AND mirrors it at the top level. The audit must read both
    so a slightly-different firmware revision (or a non-section
    node type that drops the config mirror) still yields a usable IP
    for the walk queue."""

    def test_parser_falls_back_to_top_level_ip(self):
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        # Two reads: prefer config.ip-address, fall back to top-level
        # node.ip-address. Either ordering is fine as long as both are
        # consulted.
        self.assertIn("node_cfg.get('ip-address'", src)
        self.assertIn("node.get('ip-address'", src)


class TestLocalNodeOperationalIpRecorded(unittest.TestCase):
    """The seed is typically reached at its factory-default management
    IP (``10.0.0.1``) but every neighbor's REST topology view reports
    it at its operational IP (e.g. ``10.6.22.129``). Without recording
    the operational IP in ``_walked_short_names`` during the local-
    node processing of the seed's own ``/nodes=*`` response, the
    seed gets queued AGAIN under its operational IP and audited
    twice. Pin the dedup at source level."""

    def test_local_node_branch_records_operational_ip(self):
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.run_audit)
        # Find the local-node branch of the topology loop.
        idx = src.find("node-type")
        self.assertGreater(idx, -1)
        # Look in the next ~1500 chars for the IP-add logic. Must
        # both pull ``ip-address`` (with the top-level fallback) and
        # add it to _walked_short_names.
        window = src[idx:idx + 1500]
        self.assertIn("_local_ip", window)
        self.assertIn("walkq.mark_walked", window)
        # The fallback to top-level ``ip-address`` matters because
        # firmware variants differ in where they populate the field.
        self.assertIn("node.get('ip-address'", window)


class TestRestDiscoveryDedupsAgainstWalkedSet(unittest.TestCase):
    """In addition to deduping against ``hostlist``, REST discovery
    must also check ``_walked_short_names`` so already-walked nodes
    can't be re-queued via a different identifier (e.g. the seed
    showing up at its operational IP in a neighbor's topology view).
    """

    def test_discovery_checks_walked_set(self):
        from scripts.Network import RLS_Audit
        run_src = inspect.getsource(RLS_Audit.run_audit)
        # Discovery enqueues candidates through the dedup'ing queue.
        self.assertIn("walkq.add_candidate", run_src)
        # The walked-set dedup now lives in ``_WalkQueue.add_candidate``:
        # a candidate is skipped if its identifier OR its short-name is
        # already in the walked-alias set (catches the seed re-appearing
        # under its operational IP in a neighbor's topology view).
        wq_src = inspect.getsource(RLS_Audit._WalkQueue.add_candidate)
        self.assertIn("self._walked", wq_src)
        self.assertIn("short_nm", wq_src)


class TestGetDataLogsNonSuccessStatuses(unittest.TestCase):
    """Silent REST failures kill debuggability -- the original CLI
    only logged non-200 status codes when ``options.debug`` was on.
    The audit needs to surface them unconditionally so the log file
    is actionable when a neighbor returns 401 / 404 / etc."""

    def test_get_data_logs_status_code_on_non_success(self):
        from scripts.Network import RLS_Audit
        src = inspect.getsource(RLS_Audit.get_data)
        # The log call must be OUTSIDE any ``if options.debug:``
        # guard. Strip comment lines first so an explanatory comment
        # doesn't false-match.
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#")
        ]
        body = "\n".join(code_lines)
        # There must be a log_callback for the failure case, and it
        # must not be gated on options.debug.
        self.assertIn("log_callback", body)
        # No debug-gated branch should be the only place that logs
        # the status -- the unconditional log should be present.
        self.assertRegex(
            body,
            r"log_callback\([^)]*->.*\{r\.status_code",
            "get_data must unconditionally log the response status"
            " code on non-success so the failure shows up in the"
            " log file without needing options.debug",
        )


class TestAuditAbortRaisesRuntimeError(unittest.TestCase):
    """``_audit_abort`` replaced every ``sys.exit`` call in the original
    CLI. The GUI worker catches ``RuntimeError`` specifically to show
    a clean "Audit Aborted" dialog -- pin that contract here."""

    def test_audit_abort_logs_and_raises(self):
        from scripts.Network.RLS_Audit import _audit_abort

        captured = []
        with self.assertRaises(RuntimeError) as ctx:
            _audit_abort(captured.append, "boom", "details")
        self.assertIn("boom", str(ctx.exception))
        self.assertIn("details", str(ctx.exception))
        self.assertEqual(len(captured), 1)
        self.assertIn("boom", captured[0])


class TestNetworkAuditFrameDeferredCallbacks(unittest.TestCase):
    """The error/abort handlers in ``NetworkAuditFrame._worker`` run
    later (via ``root.after``) than the ``except`` block that captured
    them. Python deletes ``except X as exc`` at block exit (PEP 3110),
    so the nested callback must *not* close over the raw exception
    object. Pin this with a source-level check."""

    def test_on_error_does_not_reference_raw_exc(self):
        from gui import network_audit_frame
        src = inspect.getsource(network_audit_frame.NetworkAuditFrame.run_audit)
        # Find the ``def on_error`` block and verify it does not call
        # friendly_error(exc) or otherwise reference the raw ``exc``
        # variable -- only pre-bound string locals.
        start = src.find("def on_error")
        self.assertNotEqual(start, -1, "on_error nested function missing")
        # Look at the next ~12 lines after the def. ``exc`` should not
        # appear there; the helper should have stashed the message into
        # ``err_msg`` (or similar) inside the except block instead.
        block = "\n".join(src[start:].splitlines()[:14])
        self.assertNotIn(
            "friendly_error(exc)", block,
            "on_error reads ``exc`` after the except block has deleted "
            "it -- capture friendly_error(exc) into a local string in "
            "the except block first.",
        )

    def test_on_audit_abort_does_not_reference_raw_exc(self):
        from gui import network_audit_frame
        src = inspect.getsource(network_audit_frame.NetworkAuditFrame.run_audit)
        start = src.find("def on_audit_abort")
        self.assertNotEqual(start, -1, "on_audit_abort nested function missing")
        block = "\n".join(src[start:].splitlines()[:10])
        self.assertNotIn(
            "str(exc)", block,
            "on_audit_abort reads ``exc`` after the except block has "
            "deleted it -- bind str(exc) to a local in the except block.",
        )


if __name__ == "__main__":
    unittest.main()
