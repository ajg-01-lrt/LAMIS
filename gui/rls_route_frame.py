"""Route-level Ciena RLS shelf planner and styled MOP exporter.

This frame intentionally separates route documentation from deployable CLI.
Add/Drop, ILA, and ROADM shelf records can be arranged into a route and
exported to the controlled FBN workbook template. A route role remains
planning-only until the operator explicitly selects and validates one of the
narrow exact providers registered for its release and installed hardware.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import ipaddress
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import tempfile
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import uuid4

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from utils.helpers import friendly_error, get_desktop_dir
from utils.rls_config.common import (
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    FIBER_TYPES,
    SUPPORTED_SOFTWARE_RELEASE,
)
from utils.rls_config.route_project import (
    DeploymentReadiness,
    OpticalPath,
    PathEndpointReview,
    PROFILE_REGISTRY,
    R40_PENDING_SRA_PEER_REVIEW,
    RouteLink,
    RouteProject,
    ShelfInstance,
    Site,
    _r40_payload_readiness,
    _r40_sra_evidence_mismatches,
    _r40_sra_line_output,
    _r40_sra_peer_shelf_ids,
    _r40_provider_sra_slots,
    _r40_provider_route_band_mismatches,
    _r40_route_topology_issues,
    _structured_sra_state,
    load_route_project_draft,
    route_link_propagation_views,
    route_project_fingerprint,
    save_route_project_draft,
)
from utils.rls_config.route_config import RouteConfigBuild, evaluate_route_configs

try:
    from utils.rls_config.diagram_import import (
        DIAGRAM_EVIDENCE_SCHEMA_ID,
        DIAGRAM_EVIDENCE_SCHEMA_VERSION,
        MIN_FIELD_CONFIDENCE,
        RAMAN_CALLOUT_CONVENTION_DISABLED,
        RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT,
        DiagramImportConventions,
        DiagramImportError,
        DiagramImportResult,
        import_route_diagram,
        load_diagram_source,
        tid_site_code_review_suggestion,
    )
except ImportError as _diagram_import_error:  # pragma: no cover - packaging guard
    DiagramImportConventions = None  # type: ignore[assignment,misc]
    DiagramImportError = RuntimeError  # type: ignore[assignment,misc]
    DiagramImportResult = Any  # type: ignore[assignment,misc]
    import_route_diagram = None  # type: ignore[assignment]
    load_diagram_source = None  # type: ignore[assignment]
    DIAGRAM_EVIDENCE_SCHEMA_ID = "atlas.ciena.rls.diagram-import-evidence"
    DIAGRAM_EVIDENCE_SCHEMA_VERSION = "1.7"
    MIN_FIELD_CONFIDENCE = 0.85
    RAMAN_CALLOUT_CONVENTION_DISABLED = "disabled"
    RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT = (
        "small-red-slot-port-v1"
    )

    def tid_site_code_review_suggestion(_tid: str | None) -> str:
        return ""

    _DIAGRAM_IMPORT_ERROR: Optional[BaseException] = _diagram_import_error
else:
    _DIAGRAM_IMPORT_ERROR = None

try:
    from utils.rls_config.diagram_assets import (
        WORKBOOK_DIAGRAM_MARKER_KEY,
        DiagramAssetError,
        WorkbookDiagram,
        validate_workbook_diagram_for_project,
        workbook_diagram_from_source,
    )
except ImportError as _diagram_asset_import_error:  # pragma: no cover
    WORKBOOK_DIAGRAM_MARKER_KEY = "workbook_diagram"
    DiagramAssetError = RuntimeError  # type: ignore[assignment,misc]
    WorkbookDiagram = Any  # type: ignore[assignment,misc]
    validate_workbook_diagram_for_project = None  # type: ignore[assignment]
    workbook_diagram_from_source = None  # type: ignore[assignment]
    _DIAGRAM_ASSET_IMPORT_ERROR: Optional[BaseException] = (
        _diagram_asset_import_error
    )
else:
    _DIAGRAM_ASSET_IMPORT_ERROR = None

try:
    from utils.rls_config.mop_export import MopExportError, export_mop
except ImportError as _mop_import_error:  # pragma: no cover - packaging guard
    MopExportError = RuntimeError  # type: ignore[assignment,misc]
    export_mop = None  # type: ignore[assignment]
    _MOP_IMPORT_ERROR: Optional[BaseException] = _mop_import_error
else:
    _MOP_IMPORT_ERROR = None

try:
    from utils.rls_config.route_bundle import RouteBundleError, export_route_bundle
except ImportError as _bundle_import_error:  # pragma: no cover - packaging guard
    RouteBundleError = RuntimeError  # type: ignore[assignment,misc]
    export_route_bundle = None  # type: ignore[assignment]
    _BUNDLE_IMPORT_ERROR: Optional[BaseException] = _bundle_import_error
else:
    _BUNDLE_IMPORT_ERROR = None


LOGGER = logging.getLogger(__name__)

PLANNING_CLI_NOTICE = (
    "This Route Builder supports exact RLS 4.0 projects only. Review each "
    "selected shelf before building the deliverable. RLS 4.0 "
    "supports multiple Add/Drop, ILA, and ROADM arrangements; those site roles "
    "are not complete variants. ATLAS requires the operator to choose a "
    "compatible exact audited R4.0 hardware/topology provider and validate all "
    "directional engineering inputs. Unsupported arrangements remain "
    "planning/documentation-only."
)


def _log_route_event(message: str, level: int = logging.INFO) -> None:
    """Record a safe operator action without serializing CLI or payload data."""

    LOGGER.log(level, "[RLS ROUTE] %s", message)

DIAGRAM_PRIVACY_NOTICE = (
    "The selected customer diagram will be decoded locally, then its embedded "
    "or selected images will be sent to the configured external AI vision "
    "provider for transcription. The provider may process customer site names, "
    "TIDs, IP addresses, circuit identifiers, and other visible diagram data. "
    "Only continue if you are authorized to send this file to that provider."
)

R40_UI_RELEASE = SUPPORTED_SOFTWARE_RELEASE
R40_UI_PROFILE_IDS = (
    "add_drop_a",
    "add_drop_z",
    "add_drop",
    "ila",
    "roadm_a",
    "roadm_z",
    "roadm",
)
_R40_UI_PROFILE_ID_SET = frozenset(R40_UI_PROFILE_IDS)
UNRESOLVED_PROFILE_LABEL = "Unresolved — select shelf role"
_DIRECTION_EVIDENCE_INVALIDATION_KEY = "line_endpoint_direction_invalidation"
_INVALIDATED_LINE_ENDPOINTS_KEY = "invalidated_line_endpoints"
_PROVIDER_PRESELECTION_INVALIDATION_KEY = "provider_preselection_invalidation"
_TERMINAL_ROUTE_TITLE_RULE_ID = "terminal-site-route-title-v1"
_TERMINAL_ROUTE_TOKEN_RE = re.compile(r"^[A-Z0-9]{2,32}$")

_TABLE_COLUMNS = (
    "order",
    "profile",
    "site",
    "tid",
    "ip",
    "release",
    "raman",
    "power",
    "provider",
    "direction",
    "readiness",
)


@dataclass
class _ShelfEditorRow:
    """Display/edit representation that retains route-model-only metadata."""

    shelf_id: str
    profile_id: str
    site_key: str
    site_code: str
    site_name: str
    tid: str
    primary_oam_ip: str
    software_release: str
    shelf_variant: str
    raman_label: str
    power_label: str
    site_address: str = ""
    network_site_id: str = ""
    notes: str = ""
    profile_payload: Mapping[str, Any] = field(default_factory=dict)
    review_state: str = "manual"
    source_evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _WorkerResult:
    """One worker completion packet consumed exclusively by the Tk thread."""

    kind: str
    generation: int
    fingerprint: str
    value: Any = None
    error: Exception | None = None
    context: Any = None


@dataclass(frozen=True)
class _R40CandidatePairReview:
    """Fail-closed paired-SRA outcome for one candidate route snapshot."""

    pending_peer_ids: tuple[str, ...] = ()
    validated_peer_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagramImportMutationBlocker:
    """One transcription-integrity failure that forbids route replacement."""

    code: str
    field: str
    message: str


@dataclass(frozen=True)
class DiagramReviewIssueAggregate:
    """Privacy-safe counts for provider transcription review issues."""

    issue_count: int
    required_review_count: int
    advisory_count: int
    code_counts: tuple[tuple[str, int], ...]
    required_path_counts: tuple[tuple[str, int], ...]
    missing_leaf_counts: tuple[tuple[str, int], ...]
    missing_path_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DiagramReviewAccounting:
    """Post-import accounting that preserves source issues without overstating work."""

    raw: DiagramReviewIssueAggregate
    unresolved: DiagramReviewIssueAggregate
    unresolved_issues: tuple[Any, ...]
    defaulted_count: int
    suggestion_pending_count: int
    scope_inherited_count: int
    optional_count: int
    lifecycle_excluded_count: int

    @property
    def source_absence_count(self) -> int:
        return sum(count for _path, count in self.raw.missing_path_counts)

    @property
    def category_counts(self) -> tuple[tuple[str, int], ...]:
        return (
            ("unresolved", self.unresolved.required_review_count),
            ("defaulted", self.defaulted_count),
            ("suggestion_pending", self.suggestion_pending_count),
            ("scope_inherited", self.scope_inherited_count),
            ("optional", self.optional_count),
            ("lifecycle_excluded", self.lifecycle_excluded_count),
        )


@dataclass(frozen=True)
class DiagramFiberTypeScope:
    """One review-only uniform source-fiber observation across active spans."""

    value: str
    source_sha256: str
    observed_span_orders: tuple[int, ...]
    inherited_span_order: int


_SAFE_DIAGNOSTIC_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$")
_SAFE_DIAGNOSTIC_PATH_RE = re.compile(
    r"^(?:route|shelves|spans|evidence)"
    r"(?:\[\d+\])?"
    r"(?:\.[a-z][a-z0-9_]*(?:\[\d+\])?)*$"
)
_INDEXED_PATH_RE = re.compile(r"\[\d+\]")
_DOTTED_INDEX_RE = re.compile(r"\.\d+(?=\.|$)")
_SHELF_MISSING_PATH_RE = re.compile(
    r"^shelves\[(?P<order>\d+)\]\.(?P<field>[a-z][a-z0-9_]*)$"
)
_SPAN_MISSING_PATH_RE = re.compile(
    r"^spans\[(?P<order>\d+)\]\.(?P<field>[a-z][a-z0-9_]*)$"
)
_SOFTWARE_RELEASE_SCOPE_DEFAULT_REASON = (
    "active Route Builder product contract; not diagram evidence"
)
_SITE_CODE_REVIEW_SUGGESTION_REASON = (
    "review-only TID-prefix suggestion; not direct site-code diagram evidence"
)
_SHELF_VARIANT_CHASSIS_SUGGESTION_REASON = (
    "review-only chassis-family fallback; not an exact shelf variant or PEC"
)
_POWER_LABEL_ROLE_DEFAULT_REASON = (
    "ATLAS route-role power standard; not customer-diagram evidence"
)
_POWER_LABEL_ROLE_DEFAULT_KEY = "power_label_role_default"
_ROUTE_REVISION_SCOPE_DEFAULT_REASON = (
    "new ATLAS deliverable revision; not customer-diagram evidence"
)
_ROUTE_FIBER_SCOPE_CONVENTION_ID = "uniform-active-span-fiber-v1"
_ROUTE_FIBER_SCOPE_SUGGESTION_KEY = "route_fiber_type_scope_suggestion"
_ROUTE_NATIVE_FIBER_REVIEW_KEY = "route_native_fiber_review"


def _safe_diagnostic_code(value: object) -> str:
    code = str(value or "").strip()
    return code if _SAFE_DIAGNOSTIC_CODE_RE.fullmatch(code) else "UNKNOWN"


def _safe_diagnostic_path(value: object) -> str:
    """Normalize controlled field paths without logging customer values."""

    raw = str(value or "").strip()
    candidate = _DOTTED_INDEX_RE.sub("[0]", raw)
    if not _SAFE_DIAGNOSTIC_PATH_RE.fullmatch(candidate):
        return "other"
    return _INDEXED_PATH_RE.sub("[]", candidate)


def _count_labels(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _format_count_pairs(
    values: Iterable[tuple[str, int]],
    *,
    limit: int = 12,
) -> str:
    pairs = tuple(values)
    shown = pairs[:limit]
    text = ",".join(f"{label}:{count}" for label, count in shown)
    if len(pairs) > len(shown):
        text += f",other:{sum(count for _label, count in pairs[len(shown):])}"
    return text or "none"


def _review_site_code_suggestion(tid: str) -> str:
    """Return a conservative, unconfirmed site-code suggestion from a TID."""

    return tid_site_code_review_suggestion(tid)


def power_label_for_profile(profile_id: str) -> str:
    """Return the ATLAS route-role power label, or blank for unknown roles."""

    normalized = str(profile_id or "").strip().casefold()
    if normalized == "ila":
        return "DC"
    if normalized in {
        "add_drop",
        "add_drop_a",
        "add_drop_z",
        "roadm",
        "roadm_a",
        "roadm_z",
    }:
        return "AC"
    return ""


def _power_label_role_default_marker(profile_id: str) -> dict[str, str]:
    """Build exact provenance for a power value supplied by ATLAS."""

    return {
        "value": power_label_for_profile(profile_id),
        "profile_id": str(profile_id or "").strip(),
        "reason": _POWER_LABEL_ROLE_DEFAULT_REASON,
    }


def _has_exact_power_label_role_default(
    source_evidence: Mapping[str, Any],
    profile_id: str,
    power_label: str,
) -> bool:
    """Return whether power is still the exact ATLAS-derived role default."""

    expected = power_label_for_profile(profile_id)
    return (
        bool(expected)
        and str(power_label or "").strip() == expected
        and source_evidence.get(_POWER_LABEL_ROLE_DEFAULT_KEY)
        == _power_label_role_default_marker(profile_id)
    )


def aggregate_diagram_review_issues(
    issues: Iterable[Any],
) -> DiagramReviewIssueAggregate:
    """Aggregate importer issues without messages, values, or shelf identity."""

    issue_list = tuple(issues)
    required = tuple(
        issue for issue in issue_list if bool(getattr(issue, "blocking", False))
    )
    missing = tuple(
        issue
        for issue in required
        if _safe_diagnostic_code(getattr(issue, "code", ""))
        == "MISSING_REQUIRED_FIELD"
    )
    missing_paths = tuple(
        _safe_diagnostic_path(getattr(issue, "field", "")) for issue in missing
    )
    missing_leaves = tuple(
        path.rsplit(".", 1)[-1].replace("[]", "")
        if path != "other"
        else "other"
        for path in missing_paths
    )
    return DiagramReviewIssueAggregate(
        issue_count=len(issue_list),
        required_review_count=len(required),
        advisory_count=len(issue_list) - len(required),
        code_counts=_count_labels(
            _safe_diagnostic_code(getattr(issue, "code", ""))
            for issue in issue_list
        ),
        required_path_counts=_count_labels(
            _safe_diagnostic_path(getattr(issue, "field", ""))
            for issue in required
        ),
        missing_leaf_counts=_count_labels(missing_leaves),
        missing_path_counts=_count_labels(missing_paths),
    )


def _exact_review_marker(
    value: object,
    *,
    expected_value: str,
    source_field: str | None,
    reason: str,
) -> bool:
    expected = {
        "value": expected_value,
        "reason": reason,
    }
    if source_field is not None:
        expected["source_field"] = source_field
    return isinstance(value, Mapping) and dict(value) == expected


def _active_rows_by_source_order(
    result: DiagramImportResult,
    rows: Iterable[_ShelfEditorRow],
) -> dict[int, _ShelfEditorRow]:
    """Bind active source orders to rows only when identity/order still agree."""

    active_shelves = tuple(getattr(result, "active_shelves", ()) or ())
    row_list = tuple(rows)
    if len(active_shelves) != len(row_list):
        return {}
    by_order: dict[int, _ShelfEditorRow] = {}
    for shelf, row in zip(active_shelves, row_list):
        order = getattr(shelf, "order", None)
        shelf_tid = str(getattr(shelf, "tid", "") or "").strip()
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
            or not shelf_tid
            or shelf_tid.casefold() != row.tid.strip().casefold()
            or order in by_order
        ):
            return {}
        by_order[order] = row
    return by_order


def _direct_fiber_evidence_matches(span: object, value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return False
    for evidence in tuple(getattr(span, "evidence", ()) or ()):
        if (
            str(getattr(evidence, "field", "") or "") != "fiber_type"
            or str(getattr(evidence, "method", "") or "")
            not in {"vision", "ocr", "native_text"}
        ):
            continue
        confidence = getattr(evidence, "confidence", None)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < MIN_FIELD_CONFIDENCE
        ):
            continue
        evidence_value = str(
            getattr(evidence, "normalized_value", "") or ""
        ).strip()
        if evidence_value.casefold() == normalized:
            return True
    return False


def diagram_fiber_type_scope(
    result: DiagramImportResult,
) -> DiagramFiberTypeScope | None:
    """Derive one pending route-scope suggestion from unanimous direct facts."""

    active_spans = tuple(getattr(result, "active_spans", ()) or ())
    if len(active_spans) < 3:
        return None
    observed: list[tuple[int, str]] = []
    missing_orders: list[int] = []
    for span in active_spans:
        order = getattr(span, "order", None)
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
        ):
            return None
        value = str(getattr(span, "fiber_type", "") or "").strip()
        if not value:
            missing_orders.append(order)
            continue
        if not _direct_fiber_evidence_matches(span, value):
            return None
        observed.append((order, value))
    if len(missing_orders) != 1 or len(observed) < 2:
        return None
    normalized_values = {value.casefold() for _order, value in observed}
    if len(normalized_values) != 1:
        return None
    source = getattr(result, "source", None)
    source_sha256 = str(getattr(source, "sha256", "") or "").strip()
    if not source_sha256:
        return None
    return DiagramFiberTypeScope(
        value=observed[0][1],
        source_sha256=source_sha256,
        observed_span_orders=tuple(order for order, _value in observed),
        inherited_span_order=missing_orders[0],
    )


def _fiber_type_scope_marker(
    scope: DiagramFiberTypeScope,
) -> dict[str, object]:
    return {
        "convention_id": _ROUTE_FIBER_SCOPE_CONVENTION_ID,
        "source_sha256": scope.source_sha256,
        "value": scope.value,
        "observed_span_orders": list(scope.observed_span_orders),
        "inherited_span_orders": [scope.inherited_span_order],
        "scope": "active_route_spans",
        "status": "pending_operator_review",
        "deployable_cli": False,
    }


def _observed_route_fiber_types(
    diagram_source: Mapping[str, object],
    links: Iterable[RouteLink],
) -> tuple[str, ...]:
    """Return distinct direct source labels without consulting reviewed tokens."""

    scope_marker = diagram_source.get(_ROUTE_FIBER_SCOPE_SUGGESTION_KEY)
    if isinstance(scope_marker, Mapping):
        value = str(scope_marker.get("value", "") or "").strip()
        if value:
            return (value,)
    values: dict[str, str] = {}
    for link in links:
        for path in link.paths:
            raw_fields = path.source_evidence.get("fields", ())
            if not isinstance(raw_fields, (list, tuple)):
                continue
            for evidence in raw_fields:
                if not isinstance(evidence, Mapping):
                    continue
                if (
                    evidence.get("field") != "fiber_type"
                    or evidence.get("method")
                    not in {"vision", "ocr", "native_text"}
                ):
                    continue
                raw_confidence = evidence.get("confidence")
                if (
                    isinstance(raw_confidence, bool)
                    or not isinstance(raw_confidence, (int, float))
                    or raw_confidence < MIN_FIELD_CONFIDENCE
                ):
                    continue
                value = str(
                    evidence.get("normalized_value", "") or ""
                ).strip()
                if value:
                    values.setdefault(value.casefold(), value)
    return tuple(values[key] for key in sorted(values))


def _path_has_exact_fiber_type_scope_marker(
    path: OpticalPath,
    scope: DiagramFiberTypeScope,
) -> bool:
    marker = path.source_evidence.get(_ROUTE_FIBER_SCOPE_SUGGESTION_KEY)
    if not isinstance(marker, Mapping):
        return False
    observed_orders = marker.get("observed_span_orders")
    inherited_orders = marker.get("inherited_span_orders")
    return (
        marker.get("convention_id") == _ROUTE_FIBER_SCOPE_CONVENTION_ID
        and marker.get("source_sha256") == scope.source_sha256
        and marker.get("value") == scope.value
        and isinstance(observed_orders, (list, tuple))
        and tuple(observed_orders) == scope.observed_span_orders
        and isinstance(inherited_orders, (list, tuple))
        and tuple(inherited_orders) == (scope.inherited_span_order,)
        and marker.get("scope") == "active_route_spans"
        and marker.get("status") == "pending_operator_review"
        and marker.get("deployable_cli") is False
    )


def account_diagram_review_issues(
    result: DiagramImportResult,
    rows: Iterable[_ShelfEditorRow],
    *,
    revision_default: object = None,
    links: Iterable[RouteLink] = (),
) -> DiagramReviewAccounting:
    """Classify source blockers against the exact post-import workflow state.

    Only a missing-field issue may be reclassified. Invalid, unsupported,
    low-confidence, or evidence-mismatch issues always remain unresolved.
    Locally generated defaults and suggestions require exact provenance
    markers; a merely nonblank editor value is never enough.
    """

    issues = tuple(getattr(result, "issues", ()) or ())
    raw = aggregate_diagram_review_issues(issues)
    all_shelves = tuple(getattr(result, "shelves", ()) or ())
    active_shelves = tuple(getattr(result, "active_shelves", ()) or ())
    shelves_by_order = {
        getattr(shelf, "order"): shelf
        for shelf in all_shelves
        if isinstance(getattr(shelf, "order", None), int)
        and not isinstance(getattr(shelf, "order", None), bool)
    }
    active_orders = {
        getattr(shelf, "order")
        for shelf in active_shelves
        if isinstance(getattr(shelf, "order", None), int)
        and not isinstance(getattr(shelf, "order", None), bool)
    }
    active_span_orders = {
        getattr(span, "order")
        for span in tuple(getattr(result, "active_spans", ()) or ())
        if isinstance(getattr(span, "order", None), int)
        and not isinstance(getattr(span, "order", None), bool)
    }
    rows_by_order = _active_rows_by_source_order(result, rows)
    paths_by_order: dict[int, OpticalPath] = {}
    duplicate_link_orders: set[int] = set()
    for link in tuple(links):
        order = getattr(link, "order", None)
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
            or len(link.paths) != 1
        ):
            continue
        if order in paths_by_order:
            duplicate_link_orders.add(order)
            paths_by_order.pop(order, None)
            continue
        if order not in duplicate_link_orders:
            paths_by_order[order] = link.paths[0]
    fiber_scope = diagram_fiber_type_scope(result)

    unresolved: list[Any] = []
    category_counts = {
        "defaulted": 0,
        "suggestion_pending": 0,
        "scope_inherited": 0,
        "optional": 0,
        "lifecycle_excluded": 0,
    }

    for issue in issues:
        if not bool(getattr(issue, "blocking", False)):
            continue
        if (
            _safe_diagnostic_code(getattr(issue, "code", ""))
            != "MISSING_REQUIRED_FIELD"
        ):
            unresolved.append(issue)
            continue

        issue_path = str(getattr(issue, "field", "") or "").strip()
        shelf_match = _SHELF_MISSING_PATH_RE.fullmatch(issue_path)
        span_match = _SPAN_MISSING_PATH_RE.fullmatch(issue_path)

        if shelf_match is not None:
            order = int(shelf_match.group("order"))
            field_name = shelf_match.group("field")
            shelf = shelves_by_order.get(order)
            row = rows_by_order.get(order)

            if shelf is not None and order not in active_orders:
                category_counts["lifecycle_excluded"] += 1
                continue
            if shelf is None:
                unresolved.append(issue)
                continue
            if field_name == "raman_label":
                category_counts["optional"] += 1
                continue
            if row is None or row.review_state != "pending":
                unresolved.append(issue)
                continue

            evidence = row.source_evidence
            if not isinstance(evidence, Mapping):
                unresolved.append(issue)
                continue
            if field_name == "software_release" and (
                not str(getattr(shelf, "software_release", "") or "").strip()
                and row.software_release == R40_UI_RELEASE
                and _exact_review_marker(
                    evidence.get("software_release_scope_default"),
                    expected_value=R40_UI_RELEASE,
                    source_field=None,
                    reason=_SOFTWARE_RELEASE_SCOPE_DEFAULT_REASON,
                )
            ):
                category_counts["defaulted"] += 1
                continue
            if field_name == "power_label" and (
                not str(getattr(shelf, "power_label", "") or "").strip()
                and _has_exact_power_label_role_default(
                    evidence,
                    row.profile_id,
                    row.power_label,
                )
            ):
                category_counts["defaulted"] += 1
                continue
            if field_name == "site_code":
                suggested = row.site_code.strip()
                if (
                    not str(getattr(shelf, "site_code", "") or "").strip()
                    and suggested
                    and suggested == _review_site_code_suggestion(row.tid)
                    and _exact_review_marker(
                        evidence.get("site_code_review_suggestion"),
                        expected_value=suggested,
                        source_field="tid",
                        reason=_SITE_CODE_REVIEW_SUGGESTION_REASON,
                    )
                ):
                    category_counts["suggestion_pending"] += 1
                    continue
            if field_name == "shelf_variant":
                suggested = row.shelf_variant.strip()
                chassis = str(evidence.get("chassis", "") or "").strip()
                if (
                    not str(getattr(shelf, "shelf_variant", "") or "").strip()
                    and not str(evidence.get("shelf_variant", "") or "").strip()
                    and suggested
                    and suggested == chassis
                    and suggested
                    == str(getattr(shelf, "chassis", "") or "").strip()
                    and _exact_review_marker(
                        evidence.get("shelf_variant_chassis_suggestion"),
                        expected_value=suggested,
                        source_field="chassis",
                        reason=_SHELF_VARIANT_CHASSIS_SUGGESTION_REASON,
                    )
                ):
                    category_counts["suggestion_pending"] += 1
                    continue

            unresolved.append(issue)
            continue

        if span_match is not None:
            span_order = int(span_match.group("order"))
            if (
                fiber_scope is not None
                and span_order == fiber_scope.inherited_span_order
                and span_match.group("field") == "fiber_type"
            ):
                path = paths_by_order.get(span_order)
                if (
                    path is not None
                    and not path.fiber_type.strip()
                    and _path_has_exact_fiber_type_scope_marker(
                        path,
                        fiber_scope,
                    )
                ):
                    category_counts["scope_inherited"] += 1
                    continue
            if (
                span_order in active_span_orders
                and span_match.group("field")
                in {"circuit_id", "fiber_start", "fiber_end"}
            ):
                category_counts["optional"] += 1
                continue

        if (
            issue_path == "route.revision"
            and not str(getattr(result, "revision", "") or "").strip()
            and _exact_review_marker(
                revision_default,
                expected_value="1",
                source_field=None,
                reason=_ROUTE_REVISION_SCOPE_DEFAULT_REASON,
            )
        ):
            category_counts["defaulted"] += 1
            continue

        unresolved.append(issue)

    unresolved_issues = tuple(unresolved)
    return DiagramReviewAccounting(
        raw=raw,
        unresolved=aggregate_diagram_review_issues(unresolved_issues),
        unresolved_issues=unresolved_issues,
        defaulted_count=category_counts["defaulted"],
        suggestion_pending_count=category_counts["suggestion_pending"],
        scope_inherited_count=category_counts["scope_inherited"],
        optional_count=category_counts["optional"],
        lifecycle_excluded_count=category_counts["lifecycle_excluded"],
    )


def _run_background_job(
    result_queue: Queue[_WorkerResult],
    *,
    kind: str,
    generation: int,
    fingerprint: str,
    work: Callable[[], Any],
    context: Any = None,
) -> None:
    """Run non-Tk work and publish a passive result packet.

    This function is the executor boundary. It intentionally has no widget,
    dialog, variable, or other Tk access.
    """

    try:
        value = work()
    except Exception as exc:
        LOGGER.exception("[RLS ROUTE] Background %s task failed", kind)
        result = _WorkerResult(
            kind=kind,
            generation=generation,
            fingerprint=fingerprint,
            error=exc,
            context=context,
        )
    else:
        result = _WorkerResult(
            kind=kind,
            generation=generation,
            fingerprint=fingerprint,
            value=value,
            context=context,
        )
    result_queue.put(result)


def _default_diagram_provider() -> Any:
    """Resolve the configured provider lazily inside the import worker."""

    import config as atlas_config
    from utils.ai.provider import OpenAIProvider

    return OpenAIProvider(
        chat_model=atlas_config.RLS_DIAGRAM_MODEL,
        image_detail=atlas_config.RLS_DIAGRAM_IMAGE_DETAIL,
        reasoning_effort=atlas_config.RLS_DIAGRAM_REASONING_EFFORT,
        max_completion_tokens=(
            atlas_config.RLS_DIAGRAM_MAX_COMPLETION_TOKENS
        ),
    )


def _import_diagram_worker(
    source: Path,
    provider_factory: Callable[[], Any],
    conventions: Any,
) -> DiagramImportResult:
    if import_route_diagram is None:
        raise DiagramImportError("Route diagram importer is unavailable.")
    return import_route_diagram(
        source,
        provider_factory(),
        conventions=conventions,
    )


def _open_preview_file(path: Path) -> None:
    """Open a generated preview using the platform shell."""

    startfile = getattr(os, "startfile", None)
    if not callable(startfile):
        raise OSError("Opening the preview is supported only on Windows.")
    startfile(str(path))


def _diagram_route_optical_band_status(
    result: DiagramImportResult,
) -> str:
    """Classify the passive route-band observation without authorizing CLI."""

    value = str(getattr(result, "optical_band", "") or "").strip()
    if not value:
        return "not_observed"
    if any(
        bool(getattr(issue, "blocking", False))
        and str(getattr(issue, "field", "") or "") == "route.optical_band"
        for issue in tuple(getattr(result, "issues", ()) or ())
    ):
        return "unverified"
    return "direct_supported"


def _display_optical_band_for_review(value: object) -> str:
    """Render one closed route-band token for operator-facing context."""

    return {
        "c": "C",
        "l": "L",
        "c+l": "C+L",
        "integrated_c+l": "Integrated C+L",
    }.get(str(value or "").strip().casefold(), "unknown")


def _diagram_source_record(result: DiagramImportResult) -> dict[str, Any]:
    """Build JSON-safe project provenance without retaining paths or pixels."""

    source = result.source
    record = {
        "schema_id": DIAGRAM_EVIDENCE_SCHEMA_ID,
        "schema_version": DIAGRAM_EVIDENCE_SCHEMA_VERSION,
        "file_name": source.file_name,
        "source_type": source.source_type,
        "source_sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "external_vision_processing_confirmed": True,
        "images": [
            {
                "index": image.index,
                "source_label": image.source_label,
                "source_part": image.source_part,
                "source_sha256": image.source_sha256,
                "normalized_sha256": image.normalized_sha256,
                "original_width": image.original_width,
                "original_height": image.original_height,
                "width": image.width,
                "height": image.height,
                "format": image.format,
                "view_kind": image.view_kind,
                "source_image_index": image.source_image_index,
                "canonical_width": image.canonical_width,
                "canonical_height": image.canonical_height,
                "crop_box": (
                    list(image.crop_box)
                    if image.crop_box is not None
                    else None
                ),
            }
            for image in source.images
        ],
        "route_evidence": [
            evidence.to_dict() for evidence in result.route_evidence
        ],
        "route_header": {
            "route_code": result.route_code,
            "title": result.title,
            "revision": result.revision,
            "ospf_area": result.ospf_area,
            "optical_band": getattr(result, "optical_band", None),
            "optical_band_status": _diagram_route_optical_band_status(result),
        },
        "active_span_count": len(result.active_spans),
        "active_spans": [
            {
                "order": span.order,
                "from_tid": span.from_tid,
                "to_tid": span.to_tid,
                "expected_loss_db": getattr(
                    span, "expected_loss_db", None
                ),
                "distance_km": getattr(span, "distance_km", None),
                "circuit_id": getattr(span, "circuit_id", None),
                "fiber_start": getattr(span, "fiber_start", None),
                "fiber_end": getattr(span, "fiber_end", None),
                "fiber_type": getattr(span, "fiber_type", None),
                "evidence": [
                    evidence.to_dict()
                    for evidence in getattr(span, "evidence", ())
                ],
            }
            for span in result.active_spans
        ],
        "planned_removals": [
            {
                "order": shelf.order,
                "tid": shelf.tid,
                "site_code": shelf.site_code,
                "profile_id": getattr(shelf, "profile_id", ""),
                "notes": getattr(shelf, "notes", None),
            }
            for shelf in result.shelves
            if not getattr(
                shelf,
                "active_for_route",
                shelf in result.active_shelves,
            )
        ],
        "issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "field": issue.field,
                "message": issue.message,
                "blocking": issue.blocking,
            }
            for issue in result.issues
        ],
    }
    route_title_derivation = getattr(
        result,
        "route_title_derivation",
        {},
    )
    if isinstance(route_title_derivation, Mapping) and route_title_derivation:
        record["route_title_derivation"] = dict(route_title_derivation)
    raman_provenance = getattr(result, "raman_callout_provenance", {})
    if isinstance(raman_provenance, Mapping):
        record.update(dict(raman_provenance))
    fiber_scope = diagram_fiber_type_scope(result)
    if fiber_scope is not None:
        record[_ROUTE_FIBER_SCOPE_SUGGESTION_KEY] = (
            _fiber_type_scope_marker(fiber_scope)
        )
    return record


def _validated_diagram_attachment(
    project: RouteProject,
    diagram: WorkbookDiagram | None,
) -> WorkbookDiagram | None:
    """Validate the session-only diagram against saved route provenance."""

    if validate_workbook_diagram_for_project is None:
        marker = project.diagram_source.get(WORKBOOK_DIAGRAM_MARKER_KEY)
        if marker is not None or diagram is not None:
            raise DiagramAssetError(
                "Diagram attachment validation support is unavailable."
            )
        return None
    return validate_workbook_diagram_for_project(project, diagram)


def _diagram_route_links(
    result: DiagramImportResult,
    rows: Iterable[_ShelfEditorRow],
) -> list[RouteLink]:
    """Convert reviewed route adjacency transcription into first-class links."""

    row_list = list(rows)
    if len(row_list) < 2:
        return []
    by_tid = {row.tid.casefold(): row for row in row_list if row.tid}
    if len(by_tid) != len(row_list):
        raise DiagramImportError(
            "Imported active shelves require unique non-empty TIDs before "
            "their optical links can be retained."
        )
    fiber_scope = diagram_fiber_type_scope(result)
    links: list[RouteLink] = []
    for order, span in enumerate(result.active_spans, start=1):
        from_tid = str(span.from_tid or "").strip()
        to_tid = str(span.to_tid or "").strip()
        from_row = by_tid.get(from_tid.casefold())
        to_row = by_tid.get(to_tid.casefold())
        if from_row is None or to_row is None:
            raise DiagramImportError(
                "Imported span endpoints do not resolve to active shelf IDs."
            )
        source_evidence: dict[str, object] = {
            "schema_id": DIAGRAM_EVIDENCE_SCHEMA_ID,
            "schema_version": DIAGRAM_EVIDENCE_SCHEMA_VERSION,
            "source_sha256": str(
                getattr(getattr(result, "source", None), "sha256", "") or ""
            ),
            "fields": [
                evidence.to_dict()
                for evidence in getattr(span, "evidence", ())
            ],
        }
        if (
            fiber_scope is not None
            and span.order == fiber_scope.inherited_span_order
        ):
            source_evidence[_ROUTE_FIBER_SCOPE_SUGGESTION_KEY] = (
                _fiber_type_scope_marker(fiber_scope)
            )
        links.append(
            RouteLink(
                link_id=f"diagram-link-{order:03}",
                order=order,
                from_shelf_id=from_row.shelf_id,
                to_shelf_id=to_row.shelf_id,
                paths=(
                    OpticalPath(
                        path_id=f"diagram-path-{order:03}-1",
                        path_role="route",
                        link_name=str(
                            getattr(span, "circuit_id", None) or ""
                        ),
                        expected_loss_db=getattr(
                            span, "expected_loss_db", None
                        ),
                        distance_km=getattr(span, "distance_km", None),
                        fiber_type=str(
                            getattr(span, "fiber_type", None) or ""
                        ),
                        circuit_id=str(
                            getattr(span, "circuit_id", None) or ""
                        ),
                        fiber_start=getattr(span, "fiber_start", None),
                        fiber_end=getattr(span, "fiber_end", None),
                        review_state="pending",
                        source_evidence=source_evidence,
                    ),
                ),
            )
        )
    return links


def _diagram_editor_rows(
    result: DiagramImportResult,
) -> list[_ShelfEditorRow]:
    """Convert importer mappings while keeping provider data and evidence apart."""

    rows: list[_ShelfEditorRow] = []
    for raw in result.gui_rows():
        profile_payload = raw.get("profile_payload", {})
        source_evidence = raw.get("source_evidence", {})
        observed_release = str(raw.get("software_release", "")).strip()
        observed_raman_label = str(raw.get("raman_label", "")).strip()
        observed_power_label = str(raw.get("power_label", "")).strip()
        suggested_raman_label = str(
            raw.get("raman_label_suggestion", "")
        ).strip()
        displayed_shelf_variant = str(raw.get("shelf_variant", "")).strip()
        tid = str(raw.get("tid", "")).strip()
        observed_site_code = str(raw.get("site_code", "")).strip()
        suggested_site_code = (
            _review_site_code_suggestion(tid)
            if not observed_site_code
            else ""
        )
        site_code = observed_site_code or suggested_site_code
        if not isinstance(profile_payload, Mapping):
            raise DiagramImportError(
                "Imported shelf profile_payload must be a JSON object."
            )
        if not isinstance(source_evidence, Mapping):
            raise DiagramImportError(
                "Imported shelf source_evidence must be a JSON object."
            )
        # Compatibility with importer drafts created before the dedicated
        # source_evidence field. Exact provider envelopes are never moved.
        if (
            not source_evidence
            and profile_payload.get("schema_id") == DIAGRAM_EVIDENCE_SCHEMA_ID
        ):
            source_evidence = profile_payload
            profile_payload = {}
        if not source_evidence:
            source_evidence = {
                "schema_id": DIAGRAM_EVIDENCE_SCHEMA_ID,
                "schema_version": DIAGRAM_EVIDENCE_SCHEMA_VERSION,
                "source_sha256": result.source.sha256,
                "fields": [],
            }
        source_evidence = dict(source_evidence)
        if not observed_release:
            # The vision importer remains facts-only. A missing release is
            # supplied here by the Route Builder's fixed product scope and is
            # recorded separately from customer-diagram evidence.
            source_evidence["software_release_scope_default"] = {
                "value": R40_UI_RELEASE,
                "reason": _SOFTWARE_RELEASE_SCOPE_DEFAULT_REASON,
            }
        if suggested_site_code:
            source_evidence["site_code_review_suggestion"] = {
                "value": suggested_site_code,
                "source_field": "tid",
                "reason": _SITE_CODE_REVIEW_SUGGESTION_REASON,
            }
        default_power_label = power_label_for_profile(
            str(raw.get("profile_id", ""))
        )
        if not observed_power_label and default_power_label:
            source_evidence[_POWER_LABEL_ROLE_DEFAULT_KEY] = (
                _power_label_role_default_marker(
                    str(raw.get("profile_id", ""))
                )
            )
        elif observed_power_label:
            source_evidence.pop(_POWER_LABEL_ROLE_DEFAULT_KEY, None)
        source_variant = str(
            source_evidence.get("shelf_variant", "") or ""
        ).strip()
        source_chassis = str(
            source_evidence.get("chassis", "") or ""
        ).strip()
        if (
            not source_variant
            and displayed_shelf_variant
            and displayed_shelf_variant == source_chassis
        ):
            source_evidence["shelf_variant_chassis_suggestion"] = {
                "value": displayed_shelf_variant,
                "source_field": "chassis",
                "reason": _SHELF_VARIANT_CHASSIS_SUGGESTION_REASON,
            }
        rows.append(
            _ShelfEditorRow(
                shelf_id=str(raw.get("shelf_id", "")),
                profile_id=str(raw.get("profile_id", "")),
                site_key=(
                    f"site-{site_code.casefold()}"
                    if suggested_site_code
                    else str(raw.get("site_key", ""))
                ),
                site_code=site_code,
                site_name=str(raw.get("site_name", "")),
                tid=tid,
                primary_oam_ip=str(raw.get("primary_oam_ip", "")),
                software_release=observed_release or R40_UI_RELEASE,
                shelf_variant=displayed_shelf_variant,
                raman_label=observed_raman_label or suggested_raman_label,
                power_label=observed_power_label or default_power_label,
                site_address=str(raw.get("site_address", "")),
                network_site_id=str(raw.get("network_site_id", "")),
                notes=str(raw.get("notes", "")),
                profile_payload=dict(profile_payload),
                review_state="pending",
                source_evidence=source_evidence,
            )
        )
    return rows


def diagram_issue_summary(
    issues: Iterable[Any],
    *,
    limit: int = 8,
) -> str:
    """Format privacy-safe aggregated transcription diagnostics for review."""

    if limit < 1:
        raise ValueError("Diagram issue summary limit must be positive.")
    aggregate = aggregate_diagram_review_issues(issues)
    if not aggregate.required_review_count:
        return "No field-level blockers were reported."
    lines = [
        "- Transcription issue types: "
        + _format_count_pairs(aggregate.code_counts, limit=limit),
        "- Affected field paths: "
        + _format_count_pairs(aggregate.required_path_counts, limit=limit),
    ]
    if aggregate.missing_leaf_counts:
        lines.extend(
            (
                "- Absent diagram fields: "
                + _format_count_pairs(
                    aggregate.missing_leaf_counts,
                    limit=limit,
                ),
                "- Absent field paths: "
                + _format_count_pairs(
                    aggregate.missing_path_counts,
                    limit=limit,
                ),
            )
        )
    return "\n".join(lines)


_CRITICAL_BBOX_EVIDENCE_FIELDS = {
    "route": {"route_code", "title", "ospf_area"},
    "shelves": {
        "tid",
        "primary_oam_ip",
        "site_code",
        "site_name",
        "profile_family",
        "chassis",
        "lifecycle",
    },
    "spans": {"from_tid", "to_tid"},
}
_DISCARDED_EVIDENCE_FIELD_RE = re.compile(
    r"\bEvidence for ['\"](?P<field>[a-z][a-z0-9_.]*)['\"] was discarded\b"
)


def _nonblank_import_value(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _discarded_critical_bbox(issue: object) -> bool:
    """Return whether an invalid bbox removed route-identity evidence.

    ``INVALID_EVIDENCE_BBOX`` predates the GUI mutation gate and stores the
    evidence-list location in ``field``.  Its controlled message includes the
    logical evidence field. Unknown future forms fail closed unless they are
    explicitly configuration-only evidence.
    """

    if getattr(issue, "code", "") != "INVALID_EVIDENCE_BBOX":
        return False
    issue_path = str(getattr(issue, "field", ""))
    if ".config_evidence[" in issue_path:
        return False
    if issue_path.startswith("route.evidence["):
        scope = "route"
    elif issue_path.startswith("shelves[") and ".evidence[" in issue_path:
        scope = "shelves"
    elif issue_path.startswith("spans[") and ".evidence[" in issue_path:
        scope = "spans"
    else:
        return True

    match = _DISCARDED_EVIDENCE_FIELD_RE.search(
        str(getattr(issue, "message", ""))
    )
    if match is None:
        return True
    return match.group("field") in _CRITICAL_BBOX_EVIDENCE_FIELDS[scope]


def diagram_import_mutation_blockers(
    result: DiagramImportResult,
) -> tuple[DiagramImportMutationBlocker, ...]:
    """Return structural transcription failures that forbid GUI replacement.

    This deliberately does not use the importer's general ``blocking_issues``
    collection. Missing shelf role, OAM IP, chassis, release, variant, power,
    RAMAN, or generator-specific values are review/readiness problems; an
    otherwise coherent route must still be allowed into pending human review.
    Missing source site fields may also enter review when the observed TID has
    a conservative prefix that the editor can mark explicitly as a suggestion.
    """

    blockers: list[DiagramImportMutationBlocker] = []

    for field_name, label in (
        ("route_code", "Route code"),
        ("title", "Route title"),
        ("ospf_area", "OSPF area"),
    ):
        if not _nonblank_import_value(getattr(result, field_name, None)):
            blockers.append(
                DiagramImportMutationBlocker(
                    "MISSING_ROUTE_IDENTITY",
                    f"route.{field_name}",
                    f"{label} is absent from the transcription.",
                )
            )

    if _nonblank_import_value(getattr(result, "title", None)) and any(
        bool(getattr(issue, "blocking", False))
        and str(getattr(issue, "field", "") or "") == "route.title"
        for issue in tuple(getattr(result, "issues", ()) or ())
    ):
        blockers.append(
            DiagramImportMutationBlocker(
                "UNSUPPORTED_ROUTE_TITLE",
                "route.title",
                (
                    "The transcribed route title is not backed by an exact "
                    "source observation or the controlled terminal-site "
                    "derivation."
                ),
            )
        )

    orientation_issues = tuple(
        issue
        for issue in tuple(getattr(result, "issues", ()) or ())
        if bool(getattr(issue, "blocking", False))
        and str(getattr(issue, "code", "") or "")
        in {
            "ROUTE_ORIENTATION_AMBIGUOUS",
            "ROUTE_ORIENTATION_MISMATCH",
            "ROUTE_ORIENTATION_UNRESOLVED",
        }
    )
    if orientation_issues:
        blockers.append(
            DiagramImportMutationBlocker(
                "MISSING_ROUTE_ORIENTATION",
                "shelves",
                (
                    "The diagram does not establish one directly corroborated "
                    "terminal A/Z order."
                ),
            )
        )

    shelves = tuple(getattr(result, "shelves", ()) or ())
    active_shelves = tuple(getattr(result, "active_shelves", ()) or ())
    if len(active_shelves) < 2:
        blockers.append(
            DiagramImportMutationBlocker(
                "INSUFFICIENT_ACTIVE_SHELVES",
                "shelves",
                "A route transcription requires at least two active shelves.",
            )
        )

    shelf_orders = [getattr(shelf, "order", None) for shelf in shelves]
    if shelf_orders != list(range(1, len(shelves) + 1)):
        blockers.append(
            DiagramImportMutationBlocker(
                "NONCONTIGUOUS_SHELF_ORDER",
                "shelves",
                "Shelf records are not in exact contiguous source order.",
            )
        )
    active_orders = [getattr(shelf, "order", None) for shelf in active_shelves]
    if active_orders != sorted(
        order for order in active_orders if isinstance(order, int)
    ) or len(set(active_orders)) != len(active_orders):
        blockers.append(
            DiagramImportMutationBlocker(
                "NONCONTIGUOUS_ACTIVE_SHELF_ORDER",
                "shelves",
                "Active shelves do not preserve one unambiguous route order.",
            )
        )

    for index, shelf in enumerate(active_shelves, start=1):
        order = getattr(shelf, "order", index)
        prefix = f"shelves[{order}]"
        # TID is the customer-observed endpoint key used to prove that spans
        # follow shelf order. OAM IP and chassis are required before an exact
        # configuration can validate, but neither establishes route topology;
        # keep those fields blank for explicit operator review instead of
        # rejecting an otherwise coherent transcription.
        if not _nonblank_import_value(getattr(shelf, "tid", None)):
            blockers.append(
                DiagramImportMutationBlocker(
                    "MISSING_SHELF_IDENTITY",
                    f"{prefix}.tid",
                    f"Active shelf {order} has no TID.",
                )
            )
        has_source_site = (
            _nonblank_import_value(getattr(shelf, "site_code", None))
            or _nonblank_import_value(getattr(shelf, "site_name", None))
        )
        suggested_site_code = _review_site_code_suggestion(
            str(getattr(shelf, "tid", "") or "")
        )
        if not has_source_site and not suggested_site_code:
            blockers.append(
                DiagramImportMutationBlocker(
                    "MISSING_SHELF_IDENTITY",
                    f"{prefix}.site",
                    (
                        f"Active shelf {order} has no site identifier or name, "
                        "and its TID cannot support a conservative site-code "
                        "review suggestion."
                    ),
                )
            )
    spans = tuple(getattr(result, "spans", ()) or ())
    active_spans = tuple(getattr(result, "active_spans", ()) or ())
    span_orders = [getattr(span, "order", None) for span in spans]
    if span_orders != list(range(1, len(spans) + 1)):
        blockers.append(
            DiagramImportMutationBlocker(
                "NONCONTIGUOUS_SPAN_ORDER",
                "spans",
                "Span records are not in exact contiguous source order.",
            )
        )

    expected_span_count = max(0, len(active_shelves) - 1)
    if len(active_spans) != expected_span_count:
        blockers.append(
            DiagramImportMutationBlocker(
                "ACTIVE_SPAN_COUNT",
                "spans",
                (
                    f"Expected {expected_span_count} active spans for "
                    f"{len(active_shelves)} active shelves; found "
                    f"{len(active_spans)}."
                ),
            )
        )

    expected_pairs = [
        (getattr(left, "tid", None), getattr(right, "tid", None))
        for left, right in zip(active_shelves, active_shelves[1:])
    ]
    actual_pairs = [
        (getattr(span, "from_tid", None), getattr(span, "to_tid", None))
        for span in active_spans
    ]
    if actual_pairs != expected_pairs:
        blockers.append(
            DiagramImportMutationBlocker(
                "SPAN_ROUTE_DISCONTINUITY",
                "spans",
                "Active span endpoints do not exactly follow active shelf order.",
            )
        )

    if any(
        _discarded_critical_bbox(issue)
        for issue in tuple(getattr(result, "issues", ()) or ())
    ):
        blockers.append(
            DiagramImportMutationBlocker(
                "CRITICAL_EVIDENCE_BBOX_DISCARDED",
                "evidence",
                (
                    "A malformed evidence rectangle discarded route identity "
                    "or topology evidence."
                ),
            )
        )

    return tuple(blockers)


def _descriptor_value(profile_id: str, attribute: str, default: str = "") -> str:
    descriptor = PROFILE_REGISTRY.get(profile_id)
    value = getattr(descriptor, attribute, default) if descriptor is not None else default
    return str(value or default)


def profile_display_name(profile_id: str) -> str:
    """Return the operator-facing name for a registered route shelf profile."""

    if not str(profile_id or "").strip():
        return UNRESOLVED_PROFILE_LABEL
    return _descriptor_value(profile_id, "display_name", profile_id)


def profile_is_planning_only(profile_id: str) -> bool:
    descriptor = PROFILE_REGISTRY.get(profile_id)
    return bool(
        True if descriptor is None else getattr(descriptor, "planning_only", True)
    )


def _r40_payload_version_state(
    profile_payload: Optional[Mapping[str, Any]],
) -> str:
    """Return ``current``, ``retired``, or ``absent`` for an exact payload."""

    if (
        not isinstance(profile_payload, Mapping)
        or profile_payload.get("schema_id")
        != "ciena.rls.r4-0-exact-request"
    ):
        return "absent"
    from utils.rls_config.r4_0_generator import R40_PAYLOAD_SCHEMA_VERSION

    return (
        "current"
        if profile_payload.get("schema_version")
        == R40_PAYLOAD_SCHEMA_VERSION
        else "retired"
    )


def profile_readiness_label(
    profile_id: str,
    *,
    review_state: str = "manual",
    advisory_label: str = "",
    profile_payload: Optional[Mapping[str, Any]] = None,
) -> str:
    """Short, unambiguous readiness label used in the shelf table."""

    if review_state == "pending":
        return "Pending review — CLI blocked"
    payload_state = _r40_payload_version_state(profile_payload)
    has_exact_payload = payload_state == "current"
    if payload_state == "retired":
        return "Exact review outdated — re-review required"
    if review_state in {"confirmed", "corrected"} and not has_exact_payload:
        return "Confirmed - CLI Pending"
    if has_exact_payload:
        if advisory_label and advisory_label != "R4.0 review only — CLI gated":
            return advisory_label
        return "Exact R4.0 provider — validation pending"
    if profile_is_planning_only(profile_id):
        return "R4.0 review only — CLI gated"
    return advisory_label or "Provider available — validation pending"


_R40_SRA_PROVIDER_REASON_CODES = frozenset(
    {
        "R40_SRA_PROVIDER_UNAVAILABLE",
        "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE",
        "R40_EXACT_PROVIDER_SRA_CONFLICT",
    }
)


def _r40_sole_candidate_provider_id(
    provider_resolution: Mapping[str, object],
) -> str:
    """Return the sole non-conflicting review provider, otherwise blank.

    This is a review-visibility predicate only.  It does not establish the
    installed packout, construct a request, or authorize generated CLI.
    """

    if (
        str(provider_resolution.get("status", "") or "").strip()
        not in {"exact_match", "unique_candidate"}
    ):
        return ""
    if (
        str(
            provider_resolution.get(
                "raman_callout_review_status",
                "not_applicable",
            )
            or ""
        )
        .strip()
        .casefold()
        in {"pending", "invalid"}
    ):
        return ""
    provider_id = str(
        provider_resolution.get("provider_id", "") or ""
    ).strip()
    raw_ids = provider_resolution.get("review_provider_ids", ())
    if not provider_id or not isinstance(raw_ids, (list, tuple)):
        return ""
    review_ids = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in raw_ids
            if isinstance(item, str) and item.strip()
        )
    )
    if review_ids != (provider_id,):
        return ""
    raw_codes = provider_resolution.get("reason_codes", ())
    reason_codes = (
        {
            str(item).strip().upper()
            for item in raw_codes
            if isinstance(item, str) and item.strip()
        }
        if isinstance(raw_codes, (list, tuple))
        else set()
    )
    if reason_codes.intersection(_R40_SRA_PROVIDER_REASON_CODES):
        return ""
    return provider_id


def _r40_direction_mapping_text(
    profile: Any,
    line_1_route_side: object,
) -> str:
    """Render fixed hardware records without inventing a return degree."""

    side = str(line_1_route_side or "").strip().upper()
    if side not in {"A", "Z"}:
        return ""
    line_outputs = getattr(profile, "line_outputs", ())
    if not isinstance(line_outputs, (list, tuple)) or len(line_outputs) not in {
        1,
        2,
    }:
        return ""
    semantics = str(getattr(profile, "line_semantics", "") or "")
    record = (
        "D"
        if semantics == "bidirectional_degree"
        else "P"
        if semantics == "unidirectional_amplifier_path"
        else ""
    )
    if not record:
        return ""
    if len(line_outputs) == 1:
        suffix = " (both flows)" if record == "D" else ""
        return f"{record}1→{side}{suffix}"
    opposite = "Z" if side == "A" else "A"
    return f"{record}1→{side} / {record}2→{opposite}"


def _r40_shelf_glance_labels(
    project: RouteProject,
    shelf: ShelfInstance,
) -> tuple[str, str]:
    """Return synchronized provider and fixed-direction table summaries."""

    from utils.rls_config.r4_0_generator import (
        R40ExactConfigGenerator,
        R40_PROVIDER_CATALOG,
        decode_r40_exact_payload,
    )

    raman_status = _raman_callout_review_status(shelf.source_evidence)
    if raman_status == "pending":
        return ("SRA review pending", "Blocked — SRA review pending")
    if raman_status == "invalid":
        return ("Invalid SRA evidence", "Invalid direction evidence")

    payload = shelf.profile_payload
    if payload:
        payload_state = _r40_payload_version_state(payload)
        if payload_state == "retired":
            return (
                "Stale provider review — re-review",
                "Stale direction review — re-review",
            )
        if payload_state != "current":
            return (
                "Invalid provider review — re-review",
                "Invalid direction review — re-review",
            )
        try:
            request = decode_r40_exact_payload(payload)
        except (TypeError, ValueError):
            return (
                "Invalid provider review — re-review",
                "Invalid direction review — re-review",
            )
        profile = R40_PROVIDER_CATALOG.get(request.provider_id)
        if (
            profile is None
            or request.profile != shelf.profile_id
            or shelf.profile_id not in profile.role_profiles
            or request.software_release != shelf.software_release
            or request.shelf_name != shelf.tid
            or request.loopback_ip != shelf.primary_oam_ip
            or _r40_provider_route_band_mismatches(profile, shelf, project)
        ):
            return (
                "Invalid provider review — re-review",
                "Invalid direction review — re-review",
            )
        if raman_status == "accepted" and not profile.supports_raman:
            return ("SRA provider required", "Blocked — SRA provider required")
        if _r40_sra_evidence_mismatches(profile, shelf):
            return (
                "SRA evidence mismatch — re-review",
                "Blocked — SRA evidence mismatch",
            )
        try:
            validation_issues = R40ExactConfigGenerator().validate(request)
        except (TypeError, ValueError):
            return (
                "Invalid provider review — re-review",
                "Invalid direction review — re-review",
            )
        if any(issue.severity == "error" for issue in validation_issues):
            return (
                "Invalid provider review — re-review",
                "Invalid direction review — re-review",
            )
        mapping = _r40_direction_mapping_text(
            profile,
            request.line_1_route_side,
        )
        if isinstance(project, RouteProject):
            topology_issues = _r40_route_topology_issues(
                request,
                shelf,
                project,
            )
            if topology_issues and all(
                issue.code == R40_PENDING_SRA_PEER_REVIEW
                for issue in topology_issues
            ):
                return (
                    f"Staged — {profile.display_name} (SRA peer pending)",
                    (
                        f"Staged — {mapping} (SRA peer pending)"
                        if mapping
                        else "Invalid direction review — re-review"
                    ),
                )
            if topology_issues:
                return (
                    "Topology mismatch — re-review",
                    "Blocked — topology mismatch",
                )
        return (
            f"Applied — {profile.display_name}",
            (
                f"Applied — {mapping}"
                if mapping
                else "Invalid direction review — re-review"
            ),
        )

    try:
        provider_resolution, direction_resolution = (
            _r4_0_provider_prepopulation(project, shelf)
        )
    except (TypeError, ValueError):
        return ("Invalid provider evidence", "Invalid direction evidence")
    provider_id = str(
        provider_resolution.get("provider_id", "") or ""
    ).strip()
    raw_review_ids = provider_resolution.get("review_provider_ids", ())
    review_ids = (
        tuple(
            dict.fromkeys(
                candidate_id
                for candidate_id in raw_review_ids
                if (
                    isinstance(candidate_id, str)
                    and candidate_id in R40_PROVIDER_CATALOG
                    and shelf.profile_id
                    in R40_PROVIDER_CATALOG[candidate_id].role_profiles
                    and not _r40_provider_route_band_mismatches(
                        R40_PROVIDER_CATALOG[candidate_id],
                        shelf,
                        project,
                    )
                    and not (
                        raman_status == "accepted"
                        and not R40_PROVIDER_CATALOG[
                            candidate_id
                        ].supports_raman
                    )
                )
            )
        )
        if isinstance(raw_review_ids, (list, tuple))
        else ()
    )
    checked_provider_resolution = {
        **provider_resolution,
        "review_provider_ids": list(review_ids),
    }
    sole_provider_id = _r40_sole_candidate_provider_id(
        checked_provider_resolution
    )
    profile = R40_PROVIDER_CATALOG.get(sole_provider_id)
    resolution_status = str(
        provider_resolution.get("status", "") or ""
    ).strip()
    if profile is not None:
        provider_prefix = (
            "Suggested"
            if provider_resolution.get("preselect_allowed") is True
            else "Candidate"
        )
        provider_label = f"{provider_prefix} — {profile.display_name}"
    elif resolution_status == "conflict":
        provider_label = "Provider conflict — review required"
    elif review_ids:
        provider_label = f"Select provider ({len(review_ids)} compatible)"
    elif raman_status == "accepted":
        provider_label = "SRA provider required"
    else:
        provider_label = "No compatible provider"

    if raman_status == "accepted" and profile is None:
        return (provider_label, "Blocked — SRA provider required")
    if profile is None:
        if resolution_status == "conflict":
            return (provider_label, "Blocked — provider conflict")
        if review_ids:
            return (provider_label, "Blocked — select provider")
        return (provider_label, "Blocked — no compatible provider")

    direction_status = str(
        direction_resolution.get("status", "") or ""
    ).strip()
    if direction_status in {"exact_match", "controlled_fallback"}:
        mapping = _r40_direction_mapping_text(
            profile,
            direction_resolution.get("line_1_route_side", ""),
        )
        if not mapping:
            return (provider_label, "Invalid direction evidence")
        prefix = "Direct" if direction_status == "exact_match" else "Derived"
        return (provider_label, f"{prefix} — {mapping}")
    if direction_status == "conflict":
        return (provider_label, "Blocked — direction conflict")
    if direction_status == "ambiguous":
        return (provider_label, "Blocked — direction ambiguous")
    return (provider_label, "Blocked — direction review required")


def r40_provider_glance_label(
    project: RouteProject,
    shelf: ShelfInstance,
) -> str:
    """Return the fail-closed exact-provider summary for the shelf table."""

    return _r40_shelf_glance_labels(project, shelf)[0]


def r40_direction_glance_label(
    project: RouteProject,
    shelf: ShelfInstance,
) -> str:
    """Return the fail-closed fixed-record-to-route-side summary."""

    return _r40_shelf_glance_labels(project, shelf)[1]


def provider_identity_changed(
    current: _ShelfEditorRow,
    replacement: _ShelfEditorRow,
) -> bool:
    """Return whether an edit invalidates a provider-specific request payload."""

    provider_fields = (
        "profile_id",
        "site_key",
        "site_code",
        "site_name",
        "tid",
        "primary_oam_ip",
        "software_release",
        "shelf_variant",
    )
    return any(
        str(getattr(current, field_name)).strip()
        != str(getattr(replacement, field_name)).strip()
        for field_name in provider_fields
    )


def _review_raman_callout_evidence(
    source_evidence: Mapping[str, Any],
    raman_label: str,
) -> tuple[dict[str, Any], bool]:
    """Record explicit operator disposition of imported SRA callouts.

    A display label is never interpreted as RAMAN hardware on its own. This
    transition applies only when the source evidence already contains
    structured, source-bound shelf-endpoint callouts.
    """

    reviewed = dict(source_evidence)
    raw_callouts = reviewed.get("raman_callouts")
    if not isinstance(raw_callouts, (list, tuple)) or not raw_callouts:
        return (reviewed, False)
    old_review = reviewed.get("raman_callout_review", "pending")
    if isinstance(old_review, Mapping):
        old_status = str(old_review.get("status", "pending"))
    else:
        old_status = str(old_review)
    new_status = "accepted" if raman_label.strip() else "rejected"
    reviewed["raman_callout_review"] = new_status
    reviewed["raman_callout_review_record"] = {
        "status": new_status,
        "display_text": raman_label.strip(),
        "action": "operator_update_selected",
        "deployable_cli": False,
    }
    compatibility_changed = (
        old_status == "accepted"
    ) != (new_status == "accepted")
    return (reviewed, compatibility_changed)


def _has_accepted_structured_sra(
    source_evidence: Mapping[str, Any],
) -> bool:
    raw_callouts = source_evidence.get("raman_callouts")
    if not isinstance(raw_callouts, (list, tuple)) or not raw_callouts:
        return False
    raw_review = source_evidence.get("raman_callout_review", "pending")
    if isinstance(raw_review, Mapping):
        status = raw_review.get("status")
    else:
        status = raw_review
    return status == "accepted"


def _raman_callout_review_status(
    source_evidence: Mapping[str, Any],
) -> str:
    """Return the fail-closed operator-review state for structured callouts."""

    raw_callouts = source_evidence.get("raman_callouts")
    if raw_callouts is None:
        return "not_applicable"
    if not isinstance(raw_callouts, (list, tuple)):
        return "invalid"
    if not raw_callouts:
        return "not_applicable"
    raw_review = source_evidence.get("raman_callout_review", "pending")
    if isinstance(raw_review, Mapping):
        raw_status = raw_review.get("status", "pending")
    else:
        raw_status = raw_review
    status = str(raw_status or "").strip().casefold()
    return status if status in {"accepted", "rejected", "pending"} else "invalid"


def _source_scoped_no_sra_observation(
    project: RouteProject,
    shelf: ShelfInstance,
) -> bool:
    """Return a review-only SRA absence fact from the enabled source convention.

    This narrows the provider dropdown only. It does not prove installed
    inventory or authorize CLI. Any unassigned source callout keeps the
    absence unknown for every shelf because its intended endpoint is unresolved.
    """

    evidence = shelf.source_evidence
    convention = evidence.get("raman_callout_convention")
    source_sha256 = evidence.get("source_sha256")
    if (
        not isinstance(convention, Mapping)
        or convention.get("id")
        != RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
        or convention.get("scope") != "source"
        or convention.get("deployable_cli") is not False
        or not isinstance(source_sha256, str)
        or not source_sha256
        or convention.get("source_sha256") != source_sha256
    ):
        return False
    raw_callouts = evidence.get("raman_callouts")
    if not isinstance(raw_callouts, (list, tuple)) or raw_callouts:
        return False
    raw_review = evidence.get("raman_callout_review", "not_applicable")
    if isinstance(raw_review, Mapping):
        raw_review = raw_review.get("status", "not_applicable")
    if str(raw_review or "").strip().casefold() not in {
        "not_applicable",
        "rejected",
    }:
        return False
    suggestion = evidence.get("raman_callout_suggestion")
    if isinstance(suggestion, Mapping) and suggestion.get("raman_present") is True:
        return False
    if shelf.raman_label.strip().casefold() not in {
        "",
        "no",
        "none",
        "not applicable",
    }:
        return False
    diagram_source = getattr(project, "diagram_source", {})
    if not isinstance(diagram_source, Mapping):
        return False
    for key in ("unassigned_raman_callouts", "unknown_callouts"):
        unresolved = diagram_source.get(key, ())
        if not isinstance(unresolved, (list, tuple)) or unresolved:
            return False
    return True


def _r4_0_candidate_sra_state(
    project: RouteProject,
    shelf: ShelfInstance,
) -> str:
    """Return present/absent/unknown for non-executable provider filtering."""

    evidence = shelf.source_evidence
    if _has_accepted_structured_sra(evidence):
        return "present"
    if _source_scoped_no_sra_observation(project, shelf):
        return "absent"
    raman_label = shelf.raman_label.strip()
    if (
        raman_label.casefold() in {"no", "none", "not applicable"}
        and _direct_diagram_fact(evidence, "raman_label", raman_label)
    ):
        return "absent"
    return "unknown"


def _next_pending_shelf_id(
    rows: Sequence[_ShelfEditorRow],
    current_index: int,
) -> str:
    """Return the next pending shelf after the current row, with one wrap."""

    if not rows or current_index < 0 or current_index >= len(rows):
        return ""
    for offset in range(1, len(rows) + 1):
        row = rows[(current_index + offset) % len(rows)]
        if row.review_state == "pending":
            return row.shelf_id
    return ""


def _next_exact_config_review_shelf_id(
    rows: Sequence[_ShelfEditorRow],
    current_index: int,
) -> str:
    """Return the next shelf that can enter an available exact-provider review."""

    if not rows or current_index < 0 or current_index >= len(rows):
        return ""
    from utils.rls_config.r4_0_generator import provider_profiles_for_role

    for offset in range(1, len(rows) + 1):
        row = rows[(current_index + offset) % len(rows)]
        if (
            _r40_payload_version_state(row.profile_payload) == "current"
        ):
            continue
        providers = provider_profiles_for_role(row.profile_id)
        if _has_accepted_structured_sra(row.source_evidence):
            providers = tuple(
                provider for provider in providers if provider.supports_raman
            )
        if providers:
            return row.shelf_id
    return ""


def _exact_config_review_progress(
    rows: Sequence[_ShelfEditorRow],
) -> tuple[int, int]:
    """Return completed and total exact-provider review counts."""

    return (
        sum(
            _r40_payload_version_state(row.profile_payload) == "current"
            for row in rows
        ),
        len(rows),
    )


def imported_values_changed(
    current: _ShelfEditorRow,
    replacement: _ShelfEditorRow,
) -> bool:
    """Return whether an operator corrected any visible imported field."""

    visible_fields = (
        "profile_id",
        "site_code",
        "site_name",
        "tid",
        "primary_oam_ip",
        "software_release",
        "shelf_variant",
        "raman_label",
        "power_label",
    )
    return any(
        str(getattr(current, field_name)).strip()
        != str(getattr(replacement, field_name)).strip()
        for field_name in visible_fields
    )


def profile_choices() -> tuple[tuple[str, str], ...]:
    """Return only RLS R4.0 route roles exposed to operators."""

    return tuple(
        (profile_id, profile_display_name(profile_id))
        for profile_id in R40_UI_PROFILE_IDS
        if profile_id in PROFILE_REGISTRY
    )


def _site_key_for(code: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", code.strip().casefold()).strip("-")
    return f"site-{token or uuid4().hex[:8]}"


def _filename_stem(value: str, fallback: str = "Ciena_RLS_Route") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._")
    return stem or fallback


def _release_is_exact(value: str, major: int, minor: int) -> bool:
    return value.strip() == f"RLS R{major}.{minor}"


def _r40_only_rows_error(rows: Iterable[object]) -> str:
    """Explain why rows cannot enter the RLS R4.0-only operator workflow."""

    row_list = tuple(rows)

    def pending_unresolved_diagram_role(row: object) -> bool:
        evidence = getattr(row, "source_evidence", {})
        return (
            not str(getattr(row, "profile_id", "") or "").strip()
            and getattr(row, "review_state", "") == "pending"
            and isinstance(evidence, Mapping)
            and evidence.get("schema_id") == DIAGRAM_EVIDENCE_SCHEMA_ID
        )

    unsupported_profiles = sorted(
        {
            str(getattr(row, "profile_id", "") or "")
            for row in row_list
            if getattr(row, "profile_id", None) not in _R40_UI_PROFILE_ID_SET
            and not pending_unresolved_diagram_role(row)
        }
    )
    unsupported_releases = sorted(
        {
            str(getattr(row, "software_release", "") or "")
            for row in row_list
            if not _release_is_exact(
                str(getattr(row, "software_release", "") or ""),
                4,
                0,
            )
        }
    )
    details: list[str] = []
    if unsupported_profiles:
        details.append(
            "unsupported shelf role(s): "
            + ", ".join(value or "<blank>" for value in unsupported_profiles)
        )
    if unsupported_releases:
        details.append(
            "non-R4.0 release value(s): "
            + ", ".join(value or "<blank>" for value in unsupported_releases)
        )
    if not details:
        return ""
    return (
        "The Ciena RLS Route Builder accepts exact RLS R4.0 Add/Drop, ILA, "
        "and ROADM projects only; " + "; ".join(details) + "."
    )


def _reconcile_route_links(
    rows: Iterable[_ShelfEditorRow],
    existing_links: Iterable[RouteLink],
    *,
    populate_missing: bool,
) -> list[RouteLink]:
    """Retain physical span facts only when their shelf pair still exists."""

    row_list = list(rows)
    link_list = list(existing_links)
    if not populate_missing or len(row_list) < 2:
        return []
    available: dict[frozenset[str], list[RouteLink]] = {}
    for link in link_list:
        key = frozenset((link.from_shelf_id, link.to_shelf_id))
        available.setdefault(key, []).append(link)

    reconciled: list[RouteLink] = []
    for order, (left, right) in enumerate(
        zip(row_list, row_list[1:]),
        start=1,
    ):
        key = frozenset((left.shelf_id, right.shelf_id))
        candidates = available.get(key, [])
        prior = candidates.pop(0) if candidates else None
        if prior is None:
            paths = (
                OpticalPath(
                    path_id=f"manual-path-{uuid4().hex}",
                    path_role="route",
                    review_state="manual",
                ),
            )
            link_id = f"manual-link-{uuid4().hex}"
        else:
            paths = prior.paths
            link_id = prior.link_id
        reconciled.append(
            RouteLink(
                link_id=link_id,
                order=order,
                from_shelf_id=left.shelf_id,
                to_shelf_id=right.shelf_id,
                paths=paths,
            )
        )
    return reconciled


def _route_profile_family(profile_id: str) -> str:
    """Return the role family whose A/Z suffix is route-order-derived."""

    normalized = str(profile_id or "").strip().casefold()
    if normalized in {"add_drop", "add_drop_a", "add_drop_z"}:
        return "add_drop"
    if normalized in {"roadm", "roadm_a", "roadm_z"}:
        return "roadm"
    return normalized


def _route_row_site_identity(row: _ShelfEditorRow) -> str:
    """Return one stable site identity for endpoint-side derivation."""

    return (
        str(row.site_key or "").strip().casefold()
        or str(row.site_code or "").strip().casefold()
        or str(row.site_name or "").strip().casefold()
        or f"shelf:{row.shelf_id}"
    )


def _reconcile_route_endpoint_profiles(
    rows: Iterable[_ShelfEditorRow],
) -> tuple[list[_ShelfEditorRow], int]:
    """Recompute A/Z role suffixes from the current ordered terminal sites.

    Add/Drop and ROADM side suffixes are derived route facts, not immutable
    hardware attributes. Reordering therefore updates every affected endpoint
    role. ILA roles and unresolved roles remain unchanged.
    """

    row_list = list(rows)
    ordered_sites: list[str] = []
    for row in row_list:
        identity = _route_row_site_identity(row)
        if identity not in ordered_sites:
            ordered_sites.append(identity)
    a_site = ordered_sites[0] if len(ordered_sites) >= 2 else ""
    z_site = ordered_sites[-1] if len(ordered_sites) >= 2 else ""

    reconciled: list[_ShelfEditorRow] = []
    changed = 0
    for row in row_list:
        family = _route_profile_family(row.profile_id)
        if family not in {"add_drop", "roadm"}:
            reconciled.append(row)
            continue
        identity = _route_row_site_identity(row)
        if a_site and identity == a_site:
            side = "A"
        elif z_site and identity == z_site:
            side = "Z"
        else:
            side = ""
        expected_profile = (
            f"{family}_{side.casefold()}" if side else family
        )
        if expected_profile == row.profile_id:
            reconciled.append(row)
            continue
        evidence = dict(row.source_evidence)
        if (
            _has_exact_power_label_role_default(
                evidence,
                row.profile_id,
                row.power_label,
            )
            and row.power_label == power_label_for_profile(expected_profile)
        ):
            # A/Z suffixes are route-order derivations, while Add/Drop and
            # ROADM use the same ATLAS power standard on either side. Keep
            # that controlled-default provenance bound to the newly derived
            # profile instead of turning an untouched AC value into an
            # apparent operator override.
            evidence[_POWER_LABEL_ROLE_DEFAULT_KEY] = (
                _power_label_role_default_marker(expected_profile)
            )
        evidence["route_endpoint_derivation"] = {
            "endpoint_side": side,
            "profile_family": family,
            "reason": "current ordered first/last distinct route site",
            "deployable_cli": False,
        }
        reconciled.append(
            replace(
                row,
                profile_id=expected_profile,
                profile_payload={},
                source_evidence=evidence,
            )
        )
        changed += 1
    return reconciled, changed


def _invalidate_route_direction_evidence(
    rows: Iterable[_ShelfEditorRow],
    *,
    reason: str,
) -> tuple[list[_ShelfEditorRow], int]:
    """Retain but disable source-relative endpoint adjacency after reordering."""

    result: list[_ShelfEditorRow] = []
    invalidated = 0
    for row in rows:
        evidence = dict(row.source_evidence)
        line_endpoints = evidence.pop("line_endpoints", None)
        if isinstance(line_endpoints, (list, tuple)) and line_endpoints:
            evidence[_INVALIDATED_LINE_ENDPOINTS_KEY] = list(line_endpoints)
            evidence[_DIRECTION_EVIDENCE_INVALIDATION_KEY] = {
                "reason": reason,
                "endpoint_count": len(line_endpoints),
                "deployable_cli": False,
            }
            row = replace(row, source_evidence=evidence)
            invalidated += 1
        result.append(row)
    return result, invalidated


def _current_terminal_route_title(
    rows: Sequence[_ShelfEditorRow],
    *,
    prior_marker: Mapping[str, object],
    source_sha256: str,
) -> tuple[str, Mapping[str, object]] | None:
    """Derive the display title from the current ordered terminal TIDs."""

    if len(rows) < 2:
        return None
    endpoint_rows = (rows[0], rows[-1])
    endpoint_tids = tuple(row.tid.strip() for row in endpoint_rows)
    endpoint_codes = tuple(
        (
            _review_site_code_suggestion(row.tid)
            or row.site_code.strip()
        ).upper()
        for row in endpoint_rows
    )
    if (
        not all(endpoint_tids)
        or not all(endpoint_codes)
        or endpoint_codes[0] == endpoint_codes[1]
        or any(
            _TERMINAL_ROUTE_TOKEN_RE.fullmatch(code) is None
            for code in endpoint_codes
        )
    ):
        return None

    display_codes = endpoint_codes
    removed_prefix: str | None = None
    if all(code.startswith("US") for code in endpoint_codes):
        stripped = tuple(code[2:] for code in endpoint_codes)
        if all(
            _TERMINAL_ROUTE_TOKEN_RE.fullmatch(code) is not None
            for code in stripped
        ):
            display_codes = stripped
            removed_prefix = "US"
    title = "-".join(display_codes)
    marker: Mapping[str, object] = {
        "rule_id": _TERMINAL_ROUTE_TITLE_RULE_ID,
        "status": "operator_order_derivation",
        "value": title,
        "provider_title": prior_marker.get("provider_title"),
        "observed_header_pair": prior_marker.get("observed_header_pair"),
        "current_endpoint_pair": "-".join(endpoint_codes),
        "endpoint_tids": endpoint_tids,
        "endpoint_codes": endpoint_codes,
        "display_codes": display_codes,
        "removed_shared_prefix": removed_prefix,
        "source_sha256": source_sha256,
        "deployable_cli": False,
    }
    return title, marker


def _clear_provider_payloads(
    rows: Iterable[_ShelfEditorRow],
) -> tuple[list[_ShelfEditorRow], int]:
    """Clear every route-bound provider request after a topology change."""

    cleared = 0
    result: list[_ShelfEditorRow] = []
    for row in rows:
        if row.profile_payload:
            cleared += 1
            row = replace(row, profile_payload={})
        result.append(row)
    return result, cleared


def _r40_sra_pair_binding_signature(request: object) -> tuple[object, ...]:
    """Return only the request fields that bind a reviewed SRA peer."""

    from utils.rls_config.r4_0_generator import R40_PROVIDER_CATALOG

    provider_id = str(getattr(request, "provider_id", "") or "")
    profile = R40_PROVIDER_CATALOG.get(provider_id)
    line_1_side = str(
        getattr(request, "line_1_route_side", "") or ""
    ).strip().upper()
    line_signatures: list[tuple[object, ...]] = []
    for direction in range(len(getattr(profile, "line_outputs", ()))):
        if _r40_sra_line_output(profile, direction) is None:
            continue
        line = getattr(
            request,
            "line_1" if direction == 0 else "line_2",
            None,
        )
        line_signatures.append(
            (
                direction,
                getattr(line, "link_name", None),
                getattr(line, "neighbor_node", None),
                getattr(line, "neighbor_line_mux_pfg", None),
                getattr(line, "neighbor_line_demux_pfg", None),
                getattr(line, "fiber_type", None),
                getattr(line, "expected_loss_db", None),
            )
        )
    return (provider_id, line_1_side, tuple(line_signatures))


def _restage_changed_r40_sra_peers(
    rows: Sequence[_ShelfEditorRow],
    project: RouteProject,
    shelf_id: str,
    previous_request: object | None,
    replacement_request: object,
) -> tuple[list[_ShelfEditorRow], tuple[str, ...]]:
    """Clear paired endpoint payloads after an SRA-bound review change.

    This is intentionally narrow: build, rack, COLAN, and other non-topology
    edits do not discard a reciprocal peer review. Provider, direction, or
    SRA-facing endpoint/path changes do, because the old pair can no longer be
    treated as one validated snapshot.
    """

    result = list(rows)
    if (
        previous_request is None
        or _r40_sra_pair_binding_signature(previous_request)
        == _r40_sra_pair_binding_signature(replacement_request)
    ):
        return (result, ())
    shelf = next(
        (item for item in project.shelves if item.shelf_id == shelf_id),
        None,
    )
    if shelf is None:
        return (result, ())
    peer_ids = tuple(
        dict.fromkeys(
            (
                *_r40_sra_peer_shelf_ids(
                    previous_request,
                    shelf,
                    project,
                ),
                *_r40_sra_peer_shelf_ids(
                    replacement_request,
                    shelf,
                    project,
                ),
            )
        )
    )
    cleared: list[str] = []
    for index, row in enumerate(result):
        if (
            row.shelf_id in peer_ids
            and row.shelf_id != shelf_id
            and row.profile_payload
        ):
            result[index] = replace(row, profile_payload={})
            cleared.append(row.shelf_id)
    return (result, tuple(cleared))


def _validate_r40_candidate_pair_review(
    project: RouteProject,
    shelf_id: str,
) -> _R40CandidatePairReview:
    """Validate a proposed exact payload and any facing SRA peer in full.

    The first locally valid endpoint may return a pending peer only when the
    named pending condition is its sole readiness finding. A completed pair is
    accepted only after both payloads pass the complete generator, identity,
    band, SRA-evidence, and reciprocal-topology contract against this same
    immutable candidate project.
    """

    from utils.rls_config.r4_0_generator import decode_r40_exact_payload

    shelf = next(
        (item for item in project.shelves if item.shelf_id == shelf_id),
        None,
    )
    if shelf is None:
        raise ValueError(
            "The reviewed shelf is absent from the candidate route snapshot."
        )
    current_codes, current_reasons = _r40_payload_readiness(
        shelf.profile_payload,
        shelf,
        project.site_by_key(shelf.site_key),
        project.ospf_area,
        project,
    )
    if current_codes:
        if tuple(current_codes) == (R40_PENDING_SRA_PEER_REVIEW,):
            request = decode_r40_exact_payload(shelf.profile_payload)
            topology_issues = _r40_route_topology_issues(
                request,
                shelf,
                project,
            )
            pending_peer_ids = tuple(
                dict.fromkeys(
                    issue.peer_shelf_id
                    for issue in topology_issues
                    if (
                        issue.code == R40_PENDING_SRA_PEER_REVIEW
                        and issue.peer_shelf_id
                    )
                )
            )
            if (
                pending_peer_ids
                and all(
                    issue.code == R40_PENDING_SRA_PEER_REVIEW
                    for issue in topology_issues
                )
            ):
                return _R40CandidatePairReview(
                    pending_peer_ids=pending_peer_ids
                )
        raise ValueError(
            "The reviewed configuration failed the complete current-snapshot "
            "readiness contract "
            f"({', '.join(current_codes)}):\n• "
            + "\n• ".join(current_reasons)
        )

    request = decode_r40_exact_payload(shelf.profile_payload)
    peer_ids = _r40_sra_peer_shelf_ids(request, shelf, project)
    peer_failures: list[str] = []
    for peer_id in peer_ids:
        peer_shelf = next(
            (
                item
                for item in project.shelves
                if item.shelf_id == peer_id
            ),
            None,
        )
        if peer_shelf is None:
            peer_failures.append(
                f"Paired SRA peer {peer_id!r} is absent from the candidate "
                "route snapshot."
            )
            continue
        peer_codes, peer_reasons = _r40_payload_readiness(
            peer_shelf.profile_payload,
            peer_shelf,
            project.site_by_key(peer_shelf.site_key),
            project.ospf_area,
            project,
        )
        if peer_codes:
            peer_failures.append(
                f"{peer_shelf.tid} ({', '.join(peer_codes)}): "
                + "; ".join(peer_reasons)
            )
    if peer_failures:
        raise ValueError(
            "The paired SRA endpoints did not pass their complete reciprocal "
            "readiness contracts against the same route snapshot:\n• "
            + "\n• ".join(peer_failures)
        )
    return _R40CandidatePairReview(validated_peer_ids=peer_ids)


_BUNDLE_EXACT_REVIEW_CODES = frozenset(
    {
        "EXACT_PROVIDER_REVIEW_REQUIRED",
        "PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED",
        "INVALID_R40_EXACT_PROFILE_PAYLOAD",
        "UNSUPPORTED_PROVIDER_RELEASE",
        "R40_EXACT_GENERATOR_VALIDATION_FAILED",
        "R40_EXACT_GENERATOR_REJECTED",
        "R40_EXACT_GENERATOR_ERROR",
        "R40_EXACT_PAYLOAD_IDENTITY_MISMATCH",
        "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH",
    }
)
_BUNDLE_PAIRED_SRA_REVIEW_CODES = frozenset(
    {R40_PENDING_SRA_PEER_REVIEW}
)
_BUNDLE_RAMAN_REVIEW_CODES = frozenset(
    {
        "PENDING_RAMAN_CALLOUT_REVIEW",
        "INVALID_RAMAN_CALLOUT_EVIDENCE",
    }
)
_BUNDLE_SRA_PROVIDER_CODES = frozenset(
    {
        "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE",
        "R40_EXACT_PROVIDER_SRA_CONFLICT",
    }
)
_BUNDLE_NATIVE_FIBER_CODES = frozenset(
    {
        "MISSING_ROUTE_NATIVE_FIBER_REVIEW",
        "INVALID_ROUTE_NATIVE_FIBER_REVIEW",
        "UNSUPPORTED_ROUTE_NATIVE_FIBER",
        "ROUTE_NATIVE_FIBER_PATH_MISMATCH",
        "ROUTE_NATIVE_FIBER_ENDPOINT_MISMATCH",
        "ROUTE_NATIVE_FIBER_MISMATCH",
    }
)
_BUNDLE_PROPAGATION_CODES = frozenset(
    {
        "PROPAGATION_PATH_INCOMPLETE",
        "UNREVIEWED_OPTICAL_PATH",
    }
)


def _bundle_preflight_actions(
    project: RouteProject,
    readiness: DeploymentReadiness,
) -> tuple[tuple[str, str, int], ...]:
    """Return concise, deduplicated operator actions for a blocked bundle."""

    statuses = tuple(readiness.shelf_statuses)

    def shelf_count(codes: frozenset[str]) -> int:
        return sum(
            bool(set(status.reason_codes) & codes)
            for status in statuses
        )

    pending_shelves = shelf_count(frozenset({"PENDING_SHELF_REVIEW"}))
    raman_shelves = shelf_count(_BUNDLE_RAMAN_REVIEW_CODES)
    sra_provider_shelves = shelf_count(_BUNDLE_SRA_PROVIDER_CODES)
    paired_sra_shelves = shelf_count(_BUNDLE_PAIRED_SRA_REVIEW_CODES)
    exact_review_shelves = shelf_count(_BUNDLE_EXACT_REVIEW_CODES)
    unreviewed_paths = 0
    for link in project.links:
        propagation_views = route_link_propagation_views(link)
        if not propagation_views:
            unreviewed_paths += 1
            continue
        missing = sum(
            view.egress_review is None for view in propagation_views
        )
        unreviewed_paths += missing
        if (
            not missing
            and any(
                path.review_state not in {"confirmed", "corrected"}
                for path in link.paths
            )
        ):
            unreviewed_paths += 1
    native_fiber_blocked = any(
        set(status.reason_codes) & _BUNDLE_NATIVE_FIBER_CODES
        for status in statuses
    )
    known_codes = {
        "PENDING_SHELF_REVIEW",
        *_BUNDLE_PROPAGATION_CODES,
        *_BUNDLE_RAMAN_REVIEW_CODES,
        *_BUNDLE_SRA_PROVIDER_CODES,
        *_BUNDLE_PAIRED_SRA_REVIEW_CODES,
        *_BUNDLE_EXACT_REVIEW_CODES,
        *_BUNDLE_NATIVE_FIBER_CODES,
    }
    other_shelves = sum(
        not status.ready
        and bool(set(status.reason_codes) - known_codes)
        for status in statuses
    )
    candidates = (
        (
            "REVIEW_SHELF_FACTS",
            "Review imported shelf facts",
            pending_shelves,
        ),
        (
            "REVIEW_RAMAN_EVIDENCE",
            "Resolve RAMAN/SRA evidence",
            raman_shelves,
        ),
        (
            "SRA_PROVIDER_COVERAGE",
            "Add a vendor-audited SRA-capable R4.0 provider",
            sra_provider_shelves,
        ),
        (
            "REVIEW_PAIRED_SRA_PEER",
            "Review the facing paired SRA endpoint",
            paired_sra_shelves,
        ),
        (
            "REVIEW_EXACT_CONFIGS",
            "Complete or correct exact R4.0 configuration reviews",
            exact_review_shelves,
        ),
        (
            "REVIEW_OPTICAL_PATHS",
            (
                "Complete A→Z and Z→A egress reviews through the adjacent "
                "shelf configuration reviews"
            ),
            unreviewed_paths,
        ),
        (
            "CONFIRM_NATIVE_FIBER",
            "Confirm one route-native CLI fiber token",
            1 if native_fiber_blocked else 0,
        ),
        (
            "RESOLVE_OTHER_BLOCKERS",
            "Resolve other route validation blockers",
            other_shelves,
        ),
    )
    return tuple(item for item in candidates if item[2])


def _bundle_preflight_message(
    actions: Iterable[tuple[str, str, int]],
) -> str:
    action_list = tuple(actions)
    lines = [
        "The final route bundle is not ready. Resolve these items first:",
        "",
    ]
    if action_list:
        lines.extend(
            f"• {label}: {count}" for _code, label, count in action_list
        )
    else:
        lines.append("• Resolve the remaining deployment-readiness blockers.")
    lines.extend(
        [
            "",
            "No destination folder was selected, no background export was "
            "started, and no bundle artifacts were created.",
        ]
    )
    return "\n".join(lines)


def _r4_0_review_facts(
    project: RouteProject,
    shelf_id: str,
) -> dict[str, object]:
    """Map route identity and adjacent span evidence into the R4.0 catalog."""

    shelf_index = next(
        (
            index
            for index, shelf in enumerate(project.shelves)
            if shelf.shelf_id == shelf_id
        ),
        None,
    )
    if shelf_index is None:
        raise ValueError("The selected shelf is no longer in the route.")
    shelf = project.shelves[shelf_index]
    site = project.site_by_key(shelf.site_key)
    descriptor = PROFILE_REGISTRY.get(shelf.profile_id)
    evidence_chassis = shelf.source_evidence.get("chassis", "")
    facts: dict[str, object] = {
        "route_id": project.route_code,
        "tid": shelf.tid,
        "site_name": site.name if site is not None else "",
        "primary_oam_ip": shelf.primary_oam_ip,
        "ospf_area": project.ospf_area,
        "chassis": (
            evidence_chassis
            if isinstance(evidence_chassis, str) and evidence_chassis.strip()
            else shelf.shelf_variant
        ),
        "site_code": site.code if site is not None else "",
        "shelf_variant": shelf.shelf_variant,
        "lifecycle": "active",
        "power_label": shelf.power_label,
        "raman_label": shelf.raman_label,
        "notes": shelf.notes,
    }
    for side, neighbor_index in (
        ("a", shelf_index - 1),
        ("z", shelf_index + 1),
    ):
        neighbor = (
            project.shelves[neighbor_index]
            if 0 <= neighbor_index < len(project.shelves)
            else None
        )
        neighbor_id = neighbor.shelf_id if neighbor is not None else ""
        matching_links = (
            tuple(
                link
                for link in project.links
                if {
                    link.from_shelf_id,
                    link.to_shelf_id,
                }
                == {shelf_id, neighbor_id}
            )
            if neighbor_id
            else ()
        )
        # Ordered shelf topology is the authority for A/Z adjacency. A path is
        # used only when exactly one first-class link represents that adjacent
        # pair; a missing or ambiguous link must not borrow another degree's
        # optical facts.
        link = matching_links[0] if len(matching_links) == 1 else None
        propagation_views = (
            route_link_propagation_views(link)
            if link is not None
            else ()
        )
        outbound_direction = "Z_TO_A" if side == "a" else "A_TO_Z"
        outbound_view = next(
            (
                view
                for view in propagation_views
                if view.direction == outbound_direction
                and view.egress_shelf_id == shelf_id
            ),
            None,
        )
        inbound_direction = (
            "A_TO_Z" if outbound_direction == "Z_TO_A" else "Z_TO_A"
        )
        inbound_view = next(
            (
                view
                for view in propagation_views
                if view.direction == inbound_direction
                and view.ingress_shelf_id == shelf_id
            ),
            None,
        )
        path = (
            outbound_view.shared_span
            if outbound_view is not None
            else None
        )
        endpoint_review = (
            outbound_view.egress_review
            if outbound_view is not None
            else None
        )
        inbound_peer_review = (
            inbound_view.egress_review
            if inbound_view is not None
            else None
        )
        neighbor_profile = (
            PROFILE_REGISTRY.get(neighbor.profile_id)
            if neighbor is not None
            else None
        )
        source_fiber_labels: dict[str, str] = {}
        if path is not None:
            raw_fields = path.source_evidence.get("fields", ())
            if isinstance(raw_fields, (list, tuple)):
                for raw_evidence in raw_fields:
                    if (
                        not isinstance(raw_evidence, Mapping)
                        or raw_evidence.get("field") != "fiber_type"
                        or raw_evidence.get("method")
                        not in {"vision", "ocr", "native_text"}
                    ):
                        continue
                    raw_confidence = raw_evidence.get("confidence")
                    if (
                        isinstance(raw_confidence, bool)
                        or not isinstance(raw_confidence, (int, float))
                        or raw_confidence < MIN_FIELD_CONFIDENCE
                    ):
                        continue
                    label = str(
                        raw_evidence.get("normalized_value", "") or ""
                    ).strip()
                    if label:
                        source_fiber_labels.setdefault(label.casefold(), label)
        source_fiber_label = (
            next(iter(source_fiber_labels.values()))
            if len(source_fiber_labels) == 1
            else ""
        )
        facts.update(
            {
                f"{side}_neighbor_tid": neighbor.tid if neighbor else "",
                f"{side}_neighbor_role": (
                    neighbor_profile.family if neighbor_profile else ""
                ),
                f"{side}_span_fiber_type": (
                    endpoint_review.fiber_type
                    if endpoint_review is not None
                    else path.fiber_type
                    if path is not None
                    else ""
                ),
                f"{side}_span_distance_km": (
                    path.distance_km if path is not None else None
                ),
                f"{side}_span_loss_db": (
                    endpoint_review.expected_loss_db
                    if endpoint_review is not None
                    else path.expected_loss_db
                    if path is not None
                    else None
                ),
                f"{side}_span_circuit_id": (
                    path.circuit_id if path is not None else ""
                ),
                f"{side}_span_link_name": (
                    endpoint_review.link_name
                    if endpoint_review is not None
                    else path.link_name
                    if path is not None
                    else ""
                ),
                f"{side}_span_fiber_start": (
                    path.fiber_start if path is not None else None
                ),
                f"{side}_span_fiber_end": (
                    path.fiber_end if path is not None else None
                ),
                f"{side}_span_source_fiber_label": source_fiber_label,
                f"{side}_span_present": path is not None,
                f"{side}_outbound_flow": (
                    "Z→A" if side == "a" else "A→Z"
                ),
                f"{side}_inbound_flow": (
                    "A→Z" if side == "a" else "Z→A"
                ),
                f"{side}_propagation_reviewed": (
                    endpoint_review is not None
                ),
                f"{side}_inbound_peer_reviewed": (
                    inbound_peer_review is not None
                ),
                f"{side}_inbound_peer_link_name": (
                    inbound_peer_review.link_name
                    if inbound_peer_review is not None
                    else ""
                ),
                f"{side}_inbound_peer_loss_db": (
                    inbound_peer_review.expected_loss_db
                    if inbound_peer_review is not None
                    else None
                ),
                f"{side}_inbound_peer_fiber_type": (
                    inbound_peer_review.fiber_type
                    if inbound_peer_review is not None
                    else ""
                ),
            }
        )
    # The descriptor lookup above is also a deliberate profile-registration
    # assertion for headless review callers.
    if descriptor is None:
        raise ValueError("The selected shelf profile is not registered.")
    return facts


def _r4_0_assumption_facts(
    project: RouteProject,
    shelf_id: str,
) -> dict[str, object]:
    """Return only fields accepted by the deterministic assumptions catalog."""

    from utils.rls_config.r4_0_review import R40_DIAGRAM_FACT_FIELDS

    return {
        field_name: value
        for field_name, value in _r4_0_review_facts(
            project,
            shelf_id,
        ).items()
        if field_name in R40_DIAGRAM_FACT_FIELDS
    }


def _route_header_optical_band(project: RouteProject) -> str:
    """Return the passive route-header band observation, when retained."""

    route_header = project.diagram_source.get("route_header", {})
    if not isinstance(route_header, Mapping):
        return ""
    if route_header.get("optical_band_status") != "direct_supported":
        return ""
    value = route_header.get("optical_band")
    return str(value or "").strip() if isinstance(value, str) else ""


def _represented_route_degree_count(
    project: RouteProject,
    shelf: ShelfInstance,
) -> int:
    """Count modeled adjacent physical spans, not traffic directions."""

    shelf_index = next(
        (
            index
            for index, candidate in enumerate(project.shelves)
            if candidate.shelf_id == shelf.shelf_id
        ),
        None,
    )
    if shelf_index is None:
        return 0
    count = 0
    for neighbor_index in (shelf_index - 1, shelf_index + 1):
        if not 0 <= neighbor_index < len(project.shelves):
            continue
        neighbor_id = project.shelves[neighbor_index].shelf_id
        matching = tuple(
            link
            for link in project.links
            if {link.from_shelf_id, link.to_shelf_id}
            == {shelf.shelf_id, neighbor_id}
            and bool(link.paths)
        )
        if len(matching) == 1:
            count += 1
    return count


def _direct_diagram_fact(
    source_evidence: Mapping[str, object],
    field_name: str,
    value: object,
    *,
    allow_inferred: bool = False,
) -> bool:
    """Return whether one retained shelf fact has matching strong evidence."""

    raw_fields = source_evidence.get("fields", ())
    if not isinstance(raw_fields, (list, tuple)):
        return False
    allowed_methods = {"vision", "ocr", "native_text"}
    if allow_inferred:
        allowed_methods.add("inferred")
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return False
    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            continue
        observed_field = str(raw.get("field", "") or "").strip()
        if (
            observed_field != field_name
            and not observed_field.endswith(f".{field_name}")
        ):
            continue
        if raw.get("method") not in allowed_methods:
            continue
        confidence = raw.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or float(confidence) < MIN_FIELD_CONFIDENCE
        ):
            continue
        observed = str(
            raw.get("normalized_value", raw.get("raw_text", "")) or ""
        ).strip().casefold()
        if observed == normalized:
            return True
    return False


def _topology_checked_line_endpoints(
    project: RouteProject,
    shelf: ShelfInstance,
) -> tuple[object, tuple[dict[str, object], ...]]:
    """Correct a lone terminal endpoint's impossible route-side assertion.

    Endpoint slot/port values remain direct diagram observations, but their
    ``preceding``/``following`` labels are provider-inferred topology. When
    exactly one imported endpoint exists and exactly one adjacent modeled span
    proves that a terminal can face only the opposite route side, normalize
    that inferred label before resolving the provider's fixed direction.
    """

    raw_endpoints = shelf.source_evidence.get("line_endpoints", ())
    if not isinstance(raw_endpoints, (list, tuple)) or len(raw_endpoints) != 1:
        return raw_endpoints, ()
    shelf_index = next(
        (
            index
            for index, candidate in enumerate(project.shelves)
            if candidate.shelf_id == shelf.shelf_id
        ),
        None,
    )
    if shelf_index is None or len(project.shelves) < 2:
        return raw_endpoints, ()

    possible: list[str] = []
    for adjacency, neighbor_index in (
        ("preceding", shelf_index - 1),
        ("following", shelf_index + 1),
    ):
        if not 0 <= neighbor_index < len(project.shelves):
            continue
        neighbor_id = project.shelves[neighbor_index].shelf_id
        matching = tuple(
            link
            for link in project.links
            if {
                link.from_shelf_id,
                link.to_shelf_id,
            }
            == {shelf.shelf_id, neighbor_id}
            and bool(link.paths)
        )
        if len(matching) == 1:
            possible.append(adjacency)
    if len(possible) != 1:
        return raw_endpoints, ()

    raw_endpoint = raw_endpoints[0]
    if not isinstance(raw_endpoint, Mapping):
        return raw_endpoints, ()
    observed = str(raw_endpoint.get("adjacency", "") or "").strip()
    normalized = possible[0]
    if observed not in {"preceding", "following"} or observed == normalized:
        return raw_endpoints, ()

    raw_evidence = raw_endpoint.get("evidence", ())
    if not isinstance(raw_evidence, (list, tuple)):
        return raw_endpoints, ()
    corrected_evidence: list[object] = []
    adjacency_supported = False
    for item in raw_evidence:
        if not isinstance(item, Mapping) or not str(
            item.get("field", "") or ""
        ).endswith(".adjacency"):
            corrected_evidence.append(item)
            continue
        confidence = item.get("confidence")
        if (
            item.get("method") != "inferred"
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or float(confidence) < MIN_FIELD_CONFIDENCE
            or str(item.get("normalized_value", "") or "").strip()
            != observed
        ):
            corrected_evidence.append(item)
            continue
        corrected_evidence.append(
            {
                **dict(item),
                "normalized_value": normalized,
                "method": "inferred",
            }
        )
        adjacency_supported = True
    if not adjacency_supported:
        return raw_endpoints, ()

    corrected_endpoint = {
        **dict(raw_endpoint),
        "adjacency": normalized,
    }
    corrected_endpoint["evidence"] = corrected_evidence
    adjustment = {
        "rule_id": "terminal-endpoint-adjacency-from-ordered-span-v1",
        "original_adjacency": observed,
        "normalized_adjacency": normalized,
        "status": "derived_pending_review",
        "deployable_cli": False,
    }
    return (corrected_endpoint,), (adjustment,)


def _r4_0_provider_prepopulation(
    project: RouteProject,
    shelf: ShelfInstance,
) -> tuple[dict[str, object], dict[str, object]]:
    """Resolve one advisory provider and its fixed route-direction mapping."""

    from utils.rls_config.r4_0_generator import (
        R40DirectModuleFact,
        R40ProviderCandidateFacts,
        R40_PROVIDER_CATALOG,
        resolve_r40_provider_candidate,
    )
    from utils.rls_config.r4_0_prepopulation import (
        resolve_r40_fixed_direction,
        resolve_r40_fixed_direction_fallback,
    )

    evidence = shelf.source_evidence
    raman_callout_review_status = _raman_callout_review_status(evidence)
    sra_state = _r4_0_candidate_sra_state(project, shelf)
    structured_sra_state, structured_sra_slots = _structured_sra_state(shelf)

    def provider_matches_sra_scope(profile: object) -> bool:
        if sra_state == "present":
            return bool(getattr(profile, "supports_raman", False)) and (
                structured_sra_state == "accepted"
                and set(structured_sra_slots)
                == set(_r40_provider_sra_slots(profile))
            )
        if sra_state == "absent":
            return not bool(getattr(profile, "supports_raman", False))
        return True

    invalidation = evidence.get(_PROVIDER_PRESELECTION_INVALIDATION_KEY)
    if not isinstance(invalidation, Mapping):
        # Projects saved by an earlier Route Builder may contain a corrected
        # visible shelf variant without the newer explicit invalidation
        # marker.  Do not let directly transcribed stale hardware silently
        # reselect a provider in that migration case.
        reviewed_variant = shelf.shelf_variant.strip().casefold()
        direct_shelf_variants = tuple(
            value.strip().casefold()
            for value in (
                str(evidence.get("shelf_variant", "") or ""),
            )
            if value.strip()
            and _direct_diagram_fact(evidence, "shelf_variant", value)
        )
        direct_chassis_variants = tuple(
            value.strip().casefold()
            for value in (
                str(evidence.get("chassis", "") or ""),
            )
            if value.strip()
            and _direct_diagram_fact(evidence, "chassis", value)
        )
        # A specific directly evidenced shelf variant/PEC is stronger than a
        # generic chassis-family label.  Falling back to the latter while a
        # stale PEC remains present could silently preserve the wrong exact
        # provider in an older saved project.
        expected_source_variants = (
            direct_shelf_variants or direct_chassis_variants
        )
        if (
            shelf.review_state == "corrected"
            and reviewed_variant
            and expected_source_variants
            and reviewed_variant not in expected_source_variants
        ):
            invalidation = {
                "reason": (
                    "corrected visible shelf variant differs from retained "
                    "direct diagram hardware"
                ),
                "fields": ("shelf_variant",),
                "deployable_cli": False,
            }
    if isinstance(invalidation, Mapping):
        role_provider_ids = tuple(
            provider_id
            for provider_id, profile in R40_PROVIDER_CATALOG.items()
            if (
                shelf.profile_id in profile.role_profiles
                and provider_matches_sra_scope(profile)
            )
        )
        review_provider_ids = tuple(
            provider_id
            for provider_id in role_provider_ids
            if not _r40_provider_route_band_mismatches(
                R40_PROVIDER_CATALOG[provider_id],
                shelf,
                project,
            )
        )
        route_band = _route_header_optical_band(project)
        raw_shelf_band = str(evidence.get("band", "") or "").strip()
        direct_shelf_band = (
            raw_shelf_band
            if raw_shelf_band
            and _direct_diagram_fact(evidence, "band", raw_shelf_band)
            else ""
        )
        band_scope = direct_shelf_band or route_band
        band_scope_rejected = bool(role_provider_ids) and not review_provider_ids
        return (
            {
                "status": "conflict",
                "provider_id": "",
                "compatible_provider_ids": list(review_provider_ids),
                "review_provider_ids": list(review_provider_ids),
                "reason_codes": [
                    "OPERATOR_PROVIDER_FACT_CORRECTION_REQUIRES_REVIEW",
                    *(
                        (
                            "ROUTE_SCOPE_OPTICAL_BAND_MISMATCH",
                            "NO_AUDITED_ROUTE_SCOPE_PROVIDER",
                        )
                        if band_scope_rejected
                        else ()
                    ),
                ],
                "reasons": [
                    "An operator corrected provider-relevant diagram facts; "
                    "choose and confirm the compatible audited provider "
                    "manually.",
                    *(
                        (
                            "No role-compatible audited provider matches the "
                            "current direct optical-band scope.",
                        )
                        if band_scope_rejected
                        else ()
                    ),
                ],
                "matched_fields": [],
                "missing_fields": [],
                "preselect_allowed": False,
                "band_scope": band_scope,
                "band_scope_source": (
                    "direct_shelf"
                    if direct_shelf_band
                    else "direct_route_header"
                    if route_band
                    else "none"
                ),
                "direct_shelf_band": direct_shelf_band,
                "route_header_band": route_band,
                "represented_route_degree_count": (
                    _represented_route_degree_count(project, shelf)
                ),
                "candidate_provider_degree_count": 0,
                "complete_direct_line_map": False,
                "complete_direct_module_inventory": False,
                "raman_callout_review_status": (
                    raman_callout_review_status
                ),
                "deployable_cli": False,
            },
            {
                "status": "missing_evidence",
                "line_1_route_side": "",
                "reason_codes": ["PROVIDER_NOT_PRESELECTED"],
                "explanation": (
                    "Provider/direction preselection was invalidated by an "
                    "operator hardware correction."
                ),
                "deployable_cli": False,
            },
        )
    chassis = str(evidence.get("chassis", "") or "").strip()
    if chassis and not (
        _direct_diagram_fact(evidence, "chassis", chassis)
        or _direct_diagram_fact(evidence, "shelf_variant", chassis)
    ):
        chassis = ""
    chassis_pec = ""
    for candidate in (
        str(evidence.get("shelf_variant", "") or "").strip(),
        chassis,
    ):
        if (
            re.fullmatch(r"NTK[A-Z0-9]{3,29}", candidate, re.IGNORECASE)
            and (
                _direct_diagram_fact(evidence, "shelf_variant", candidate)
                or _direct_diagram_fact(evidence, "chassis", candidate)
            )
        ):
            chassis_pec = candidate
            break

    def direct_structured(name: str) -> str:
        value = str(evidence.get(name, "") or "").strip()
        return (
            value
            if value and _direct_diagram_fact(evidence, name, value)
            else ""
        )

    direct_modules: list[R40DirectModuleFact] = []
    raw_modules = evidence.get("module_inventory", ())
    if isinstance(raw_modules, (list, tuple)):
        for index, raw_module in enumerate(raw_modules):
            if not isinstance(raw_module, Mapping):
                continue
            pec = str(raw_module.get("pec", "") or "").strip()
            slot = raw_module.get("slot")
            subslot = raw_module.get("subslot")
            if (
                not pec
                or not isinstance(slot, int)
                or isinstance(slot, bool)
                or not _direct_diagram_fact(
                    evidence,
                    f"module_inventory.{index}.pec",
                    pec,
                )
                or not _direct_diagram_fact(
                    evidence,
                    f"module_inventory.{index}.slot",
                    slot,
                )
            ):
                continue
            if subslot is not None and not (
                isinstance(subslot, int)
                and not isinstance(subslot, bool)
                and _direct_diagram_fact(
                    evidence,
                    f"module_inventory.{index}.subslot",
                    subslot,
                )
            ):
                continue
            direct_modules.append(
                R40DirectModuleFact(
                    slot=slot,
                    pec=pec,
                    subslot=subslot,
                )
            )

    facts = R40ProviderCandidateFacts(
        role_profile=shelf.profile_id,
        software_release=R40_UI_RELEASE,
        chassis_family=chassis,
        chassis_pec=chassis_pec,
        # A direct per-shelf band is the strongest provider discriminator. The
        # direct-supported route header is a project-scope compatibility guard;
        # it may narrow the review catalog, but it cannot authorize advisory
        # preselection without a complete direct module inventory or complete
        # local line map plus chassis evidence.
        optical_band=direct_structured("band"),
        topology=direct_structured("topology"),
        add_drop_structure=direct_structured("add_drop_structure"),
        protection_type=direct_structured("protection_type"),
        module_inventory=tuple(direct_modules),
        sra_state=sra_state,
    )
    resolution = resolve_r40_provider_candidate(facts)
    checked_endpoints, topology_adjustments = (
        _topology_checked_line_endpoints(project, shelf)
    )
    route_band = _route_header_optical_band(project)
    shelf_band = facts.optical_band
    band_scope = shelf_band or route_band
    band_scope_source = (
        "direct_shelf"
        if shelf_band
        else "direct_route_header"
        if route_band
        else "none"
    )
    compatible_provider_ids = tuple(resolution.compatible_provider_ids)
    sra_provider_ids = tuple(
        provider_id
        for provider_id in compatible_provider_ids
        if (
            provider_id in R40_PROVIDER_CATALOG
            and provider_matches_sra_scope(
                R40_PROVIDER_CATALOG[provider_id]
            )
        )
    )
    endpoint_provider_ids = tuple(
        provider_id
        for provider_id in sra_provider_ids
        if (
            provider_id in R40_PROVIDER_CATALOG
            and (
                not checked_endpoints
                or "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH"
                not in resolve_r40_fixed_direction(
                    R40_PROVIDER_CATALOG[provider_id],
                    checked_endpoints,
                ).reason_codes
            )
        )
    )
    review_provider_ids = tuple(
        provider_id
        for provider_id in endpoint_provider_ids
        if (
            provider_id in R40_PROVIDER_CATALOG
            and not _r40_provider_route_band_mismatches(
                R40_PROVIDER_CATALOG[provider_id],
                shelf,
                project,
            )
        )
    )
    sra_scope_rejected = bool(compatible_provider_ids) and not sra_provider_ids
    endpoint_scope_rejected = bool(sra_provider_ids) and not endpoint_provider_ids
    band_scope_rejected = bool(endpoint_provider_ids) and not review_provider_ids
    endpoint_scope_unique = bool(
        checked_endpoints
        and len(review_provider_ids) == 1
        and len(compatible_provider_ids) > 1
    )
    route_scope_unique = bool(
        len(review_provider_ids) == 1
        and resolution.provider_id not in review_provider_ids
        and (band_scope or checked_endpoints)
    )
    provider_id = (
        resolution.provider_id
        if resolution.provider_id in review_provider_ids
        else review_provider_ids[0]
        if route_scope_unique
        else None
    )
    provider_profile = (
        R40_PROVIDER_CATALOG.get(provider_id) if provider_id else None
    )
    direction = (
        resolve_r40_fixed_direction(provider_profile, checked_endpoints)
        if provider_profile is not None
        else None
    )
    direct_line_outputs = {
        (
            endpoint.get("slot"),
            endpoint.get("line_out_port"),
        )
        for endpoint in (
            direction.matched_endpoints if direction is not None else ()
        )
        if isinstance(endpoint.get("slot"), int)
        and not isinstance(endpoint.get("slot"), bool)
        and isinstance(endpoint.get("line_out_port"), int)
        and not isinstance(endpoint.get("line_out_port"), bool)
    }
    complete_line_map = bool(
        provider_profile is not None
        and direct_line_outputs == set(provider_profile.line_outputs)
    )
    direct_module_set = {
        (module.slot, module.subslot, module.pec.strip().upper())
        for module in direct_modules
    }
    expected_module_set = (
        {
            *(
                (slot, None, pec.upper())
                for slot, pec in provider_profile.equipment
            ),
            *(
                (slot, subslot, pec.upper())
                for slot, subslot, pec in provider_profile.osc_modules
            ),
        }
        if provider_profile is not None
        else set()
    )
    complete_module_inventory = bool(
        expected_module_set and direct_module_set == expected_module_set
    )
    represented_degree_count = _represented_route_degree_count(project, shelf)
    provider_degree_count = (
        len(provider_profile.line_outputs)
        if provider_profile is not None
        else 0
    )
    unrepresented_terminal_degree = bool(
        provider_profile is not None
        and provider_profile.line_semantics == "bidirectional_degree"
        and represented_degree_count < provider_degree_count
    )
    matched_fields = set(resolution.matched_fields)
    sufficient_provider_identity = bool(
        complete_module_inventory
        or (
            complete_line_map
            and bool(
                {"chassis_family", "chassis_pec"}.intersection(
                    matched_fields
                )
            )
            and not unrepresented_terminal_degree
        )
    )
    provider_status = (
        "conflict"
        if (
            band_scope_rejected
            or sra_scope_rejected
            or endpoint_scope_rejected
        )
        else "unique_candidate"
        if route_scope_unique
        else resolution.status
    )
    preselect_allowed = (
        provider_status in {"exact_match", "unique_candidate"}
        and provider_profile is not None
        and sufficient_provider_identity
        and raman_callout_review_status not in {"pending", "invalid"}
    )
    provider_reason_codes = [
        code
        for code in resolution.reason_codes
        if not (
            route_scope_unique
            and code == "MULTIPLE_COMPATIBLE_PROVIDERS"
        )
    ]
    provider_reasons = (
        [
            (
                "Exactly one role-compatible provider remains after applying "
                "the reviewed fixed line-output map and direct optical-band "
                "scope."
                if endpoint_scope_unique and band_scope
                else "Exactly one role-compatible provider remains after "
                "applying the reviewed fixed line-output map."
                if endpoint_scope_unique
                else "Exactly one role-compatible provider remains after "
                f"applying the direct {band_scope} optical-band scope."
            )
            + " These review constraints do not replace installed hardware "
            "confirmation."
        ]
        if route_scope_unique
        else list(resolution.reasons)
    )
    if route_scope_unique:
        provider_reason_codes.append(
            (
                "UNIQUE_ENDPOINT_SCOPE_COMPATIBLE_PROVIDER"
                if endpoint_scope_unique
                else "UNIQUE_ROUTE_SCOPE_COMPATIBLE_PROVIDER"
            )
        )
    if band_scope_rejected:
        provider_reason_codes.extend(
            [
                "ROUTE_SCOPE_OPTICAL_BAND_MISMATCH",
                "NO_AUDITED_ROUTE_SCOPE_PROVIDER",
            ]
        )
        provider_reasons.append(
            "No role-compatible audited provider matches the directly "
            f"observed {band_scope or 'unknown'} optical-band scope. A direct "
            "per-shelf band may establish an intentional band-partition "
            "exception; route role and catalog uniqueness cannot."
        )
    if sra_scope_rejected:
        provider_reason_codes.extend(
            [
                "R40_SRA_SLOT_PROVIDER_MISMATCH",
                "NO_AUDITED_SRA_SLOT_PROVIDER",
            ]
        )
        provider_reasons.append(
            "No role-compatible audited provider matches the reviewed SRA "
            "slot and its fixed port-5/port-6 endpoint map."
        )
    if endpoint_scope_rejected:
        provider_reason_codes.extend(
            [
                "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH",
                "NO_AUDITED_LINE_ENDPOINT_PROVIDER",
            ]
        )
        provider_reasons.append(
            "No role-compatible audited provider matches the reviewed fixed "
            "line-output endpoint map."
        )
    elif provider_profile is not None and not sufficient_provider_identity:
        provider_reason_codes.append(
            "INSUFFICIENT_PROVIDER_IDENTITY_EVIDENCE"
        )
        provider_reasons.append(
            "The compatible catalog candidate is not identified by a complete "
            "direct module inventory or a complete directly observed fixed "
            "line-output map plus chassis evidence."
        )
        if unrepresented_terminal_degree:
            provider_reason_codes.append(
                "PROVIDER_DEGREE_COUNT_NOT_ESTABLISHED"
            )
            provider_reasons.append(
                "The provider contains more physical RLA degrees than the "
                "uploaded route represents. Installed inventory must establish "
                "the additional degree and its independently engineered peer."
            )
    provider_record: dict[str, object] = {
        "status": provider_status,
        "provider_id": provider_id or "",
        "compatible_provider_ids": list(review_provider_ids),
        "review_provider_ids": list(review_provider_ids),
        "reason_codes": provider_reason_codes,
        "reasons": provider_reasons,
        "matched_fields": list(resolution.matched_fields),
        "missing_fields": list(resolution.missing_fields),
        "preselect_allowed": preselect_allowed,
        "band_scope": band_scope,
        "band_scope_source": band_scope_source,
        "direct_shelf_band": shelf_band,
        "route_header_band": route_band,
        "represented_route_degree_count": represented_degree_count,
        "candidate_provider_degree_count": provider_degree_count,
        "complete_direct_line_map": complete_line_map,
        "complete_direct_module_inventory": complete_module_inventory,
        "raman_callout_review_status": raman_callout_review_status,
        "deployable_cli": False,
    }
    direction_record: dict[str, object] = {
        "status": "missing_evidence",
        "line_1_route_side": "",
        "reason_codes": ["PROVIDER_NOT_PRESELECTED"],
        "explanation": (
            "A compatible exact provider must be established before local "
            "ports can be mapped to its fixed directions."
        ),
        "deployable_cli": False,
    }
    if endpoint_scope_rejected:
        direction_record = {
            "status": "conflict",
            "line_1_route_side": "",
            "reason_codes": [
                "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH",
            ],
            "explanation": (
                "Direct local line-output evidence conflicts with every "
                "route-compatible exact provider's immutable port map."
            ),
            "matched_endpoints": [],
            "deployable_cli": False,
        }
    if provider_id and direction is not None:
        raw_line_endpoints = evidence.get("line_endpoints", ())
        endpoint_evidence_absent = (
            isinstance(raw_line_endpoints, (list, tuple))
            and not raw_line_endpoints
            and not evidence.get(_INVALIDATED_LINE_ENDPOINTS_KEY)
        )
        if (
            _r40_sole_candidate_provider_id(provider_record) == provider_id
            and direction.status == "missing_evidence"
            and endpoint_evidence_absent
        ):
            direction = resolve_r40_fixed_direction_fallback(
                R40_PROVIDER_CATALOG[provider_id],
                shelf.profile_id,
            )
        direction_record = direction.to_dict()
        if topology_adjustments:
            direction_record["reason_codes"] = [
                *list(direction_record.get("reason_codes", ())),
                "TERMINAL_ADJACENCY_NORMALIZED_FROM_ORDERED_SPAN",
            ]
            direction_record["explanation"] = (
                str(direction_record.get("explanation", "") or "").rstrip(
                    "."
                )
                + ". The lone terminal endpoint's inferred route side was "
                "normalized against its only modeled adjacent span; confirm "
                "the fixed direction against the installed port map."
            )
            direction_record["topology_adjustments"] = list(
                topology_adjustments
            )
            direction_record["deployable_cli"] = False
        if (
            direction.status == "conflict"
            and "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH"
            in direction.reason_codes
        ):
            provider_record["status"] = "conflict"
            provider_record["provider_id"] = ""
            provider_record["compatible_provider_ids"] = []
            provider_record["review_provider_ids"] = []
            provider_record["reason_codes"] = [
                *list(provider_record["reason_codes"]),
                "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH",
            ]
            provider_record["reasons"] = [
                *list(provider_record["reasons"]),
                (
                    "Direct local line-output evidence conflicts with the "
                    "candidate provider's immutable port map."
                ),
            ]
            provider_record["preselect_allowed"] = False
    return provider_record, direction_record


_PEER_PFG_SOURCE_EXACT_PAYLOAD = "current_exact_peer_payload"
_PEER_PFG_SOURCE_DIRECT_DIRECTION = "direct_peer_direction_evidence"
_PEER_PFG_SOURCE_ROLE_FALLBACK = "audited_peer_role_fallback"
_PEER_PFG_SOURCE_ORDERED_TOPOLOGY = "ordered_topology_single_degree"


def _r4_0_peer_pfg_prepopulation(
    project: RouteProject,
    shelf: ShelfInstance,
    route_side: str,
) -> dict[str, str]:
    """Return fail-closed facing-peer PFG defaults for one represented side.

    Neighbor PFG names are immutable properties of an exact provider, but the
    correct record still depends on the peer's physical route-side mapping.
    A current exact payload is authoritative for that reviewed mapping.
    Otherwise ATLAS requires exactly one route-compatible provider and either
    direct endpoint resolution, the existing audited provider/role fallback,
    or a single-degree endpoint whose sole ordered adjacency proves its only
    possible side. Conflicting, ambiguous, SRA-equipped, and multi-adjacency
    peers intentionally return no suggestion. Reviewed SRA peers are eligible
    only when the surviving exact provider has the same audited SRA slot.
    """

    blank = {
        "neighbor_line_mux_pfg": "",
        "neighbor_line_demux_pfg": "",
        "neighbor_pfg_source": "",
    }
    if route_side not in {"A", "Z"}:
        return blank
    shelf_index = next(
        (
            index
            for index, candidate in enumerate(project.shelves)
            if candidate.shelf_id == shelf.shelf_id
        ),
        None,
    )
    if shelf_index is None:
        return blank
    peer_index = shelf_index - 1 if route_side == "A" else shelf_index + 1
    if not 0 <= peer_index < len(project.shelves):
        return blank
    peer = project.shelves[peer_index]
    matching_links = tuple(
        link
        for link in project.links
        if {link.from_shelf_id, link.to_shelf_id}
        == {shelf.shelf_id, peer.shelf_id}
    )
    if len(matching_links) != 1 or len(matching_links[0].paths) != 1:
        return blank
    peer_raman_status = _raman_callout_review_status(peer.source_evidence)
    if peer_raman_status in {"pending", "invalid"}:
        # Pending or malformed structured callouts are unresolved hardware
        # identity, so they must fail closed.
        return blank

    from utils.rls_config.r4_0_generator import (
        R40_PAYLOAD_SCHEMA_ID,
        R40_PROVIDER_CATALOG,
        decode_r40_exact_payload,
    )
    from utils.rls_config.r4_0_prepopulation import (
        resolve_r40_fixed_direction_fallback,
    )

    peer_facing_side = "Z" if route_side == "A" else "A"
    ordered_neighbor_indices = tuple(
        index
        for index in (peer_index - 1, peer_index + 1)
        if 0 <= index < len(project.shelves)
    )
    sole_neighbor_id = (
        project.shelves[ordered_neighbor_indices[0]].shelf_id
        if len(ordered_neighbor_indices) == 1
        else ""
    )
    sole_links = (
        tuple(
            link
            for link in project.links
            if {
                link.from_shelf_id,
                link.to_shelf_id,
            }
            == {peer.shelf_id, sole_neighbor_id}
        )
        if sole_neighbor_id
        else ()
    )
    sole_side = (
        "A"
        if ordered_neighbor_indices == (peer_index - 1,)
        else "Z"
        if ordered_neighbor_indices == (peer_index + 1,)
        else ""
    )
    one_degree_topology_resolved = bool(
        sole_side == peer_facing_side
        and len(sole_links) == 1
        and len(sole_links[0].paths) == 1
    )

    def provider_matches_peer_sra(profile: object) -> bool:
        state, reviewed_slots = _structured_sra_state(peer)
        supports_sra = bool(getattr(profile, "supports_raman", False))
        if supports_sra:
            return (
                state == "accepted"
                and set(reviewed_slots)
                == set(_r40_provider_sra_slots(profile))
            )
        return state != "accepted"

    def pfg_values(
        profile: object,
        line_1_side: str,
        source: str,
        *,
        line_2_present: bool | None = None,
    ) -> dict[str, str]:
        if line_1_side not in {"A", "Z"}:
            return blank
        record_index = 0 if line_1_side == peer_facing_side else 1
        line_outputs = getattr(profile, "line_outputs", ())
        line_pfg_names = getattr(profile, "line_pfg_names", ())
        if (
            not isinstance(line_outputs, (list, tuple))
            or not isinstance(line_pfg_names, (list, tuple))
            or record_index >= len(line_outputs)
            or record_index >= len(line_pfg_names)
            or (record_index == 1 and line_2_present is False)
            or (
                len(line_outputs) == 1
                and not one_degree_topology_resolved
            )
        ):
            # A one-degree provider has no opposite-side record. Its paired
            # mux/demux carries both traffic directions on the assigned side;
            # reverse traffic must never synthesize a second degree.
            return blank
        raw_names = line_pfg_names[record_index]
        if (
            not isinstance(raw_names, (list, tuple))
            or len(raw_names) != 2
        ):
            return blank
        mux_name = str(raw_names[0] or "").strip()
        demux_name = str(raw_names[1] or "").strip()
        if not mux_name or not demux_name:
            return blank
        return {
            "neighbor_line_mux_pfg": mux_name,
            "neighbor_line_demux_pfg": demux_name,
            "neighbor_pfg_source": source,
        }

    raw_payload = peer.profile_payload
    if (
        isinstance(raw_payload, Mapping)
        and raw_payload.get("schema_id") == R40_PAYLOAD_SCHEMA_ID
    ):
        try:
            peer_request = decode_r40_exact_payload(raw_payload)
        except (TypeError, ValueError):
            return blank
        peer_profile = R40_PROVIDER_CATALOG.get(peer_request.provider_id)
        line_count = (
            len(peer_profile.line_outputs)
            if peer_profile is not None
            else 0
        )
        if (
            peer_profile is None
            or peer.profile_id not in peer_profile.role_profiles
            or peer_request.profile not in peer_profile.role_profiles
            or peer_request.software_release != R40_UI_RELEASE
            or peer_request.chassis_family != peer_profile.chassis_family
            or peer_request.chassis_pec != peer_profile.chassis_pec
            or peer_request.hardware_profile != peer_profile.hardware_profile
            or (line_count == 1 and peer_request.line_2 is not None)
            or (line_count == 2 and peer_request.line_2 is None)
            or not provider_matches_peer_sra(peer_profile)
            or _r40_provider_route_band_mismatches(
                peer_profile,
                peer,
                project,
            )
        ):
            return blank
        return pfg_values(
            peer_profile,
            peer_request.line_1_route_side,
            _PEER_PFG_SOURCE_EXACT_PAYLOAD,
            line_2_present=peer_request.line_2 is not None,
        )

    provider_resolution, direction_resolution = (
        _r4_0_provider_prepopulation(project, peer)
    )
    if provider_resolution.get("status") == "conflict":
        return blank
    raw_provider_ids = provider_resolution.get("review_provider_ids", ())
    provider_ids = (
        tuple(
            str(provider_id).strip()
            for provider_id in raw_provider_ids
            if isinstance(provider_id, str) and provider_id.strip()
        )
        if isinstance(raw_provider_ids, (list, tuple))
        else ()
    )
    if len(provider_ids) != 1:
        return blank
    peer_profile = R40_PROVIDER_CATALOG.get(provider_ids[0])
    if (
        peer_profile is None
        or peer.profile_id not in peer_profile.role_profiles
        or not provider_matches_peer_sra(peer_profile)
        or _r40_provider_route_band_mismatches(
            peer_profile,
            peer,
            project,
        )
    ):
        return blank

    direction_status = str(
        direction_resolution.get("status", "") or ""
    ).strip()
    if direction_status in {"conflict", "ambiguous"}:
        return blank
    line_1_side = str(
        direction_resolution.get("line_1_route_side", "") or ""
    ).strip()
    if direction_status == "exact_match" and line_1_side in {"A", "Z"}:
        source = _PEER_PFG_SOURCE_DIRECT_DIRECTION
    elif (
        direction_status == "controlled_fallback"
        and line_1_side in {"A", "Z"}
    ):
        source = _PEER_PFG_SOURCE_ROLE_FALLBACK
    else:
        source = ""

    if not source and len(peer_profile.line_outputs) == 1:
        # A one-degree endpoint can face only its sole ordered neighbor. This
        # fallback is not valid for an interior shelf, a missing/duplicate
        # modeled link, or any ambiguous/conflicting endpoint observation.
        if (
            direction_status == "missing_evidence"
            and one_degree_topology_resolved
        ):
            line_1_side = sole_side
            source = _PEER_PFG_SOURCE_ORDERED_TOPOLOGY

    if not source and direction_status == "missing_evidence":
        raw_endpoints = peer.source_evidence.get("line_endpoints", ())
        invalidated = peer.source_evidence.get(
            _INVALIDATED_LINE_ENDPOINTS_KEY
        )
        if (
            isinstance(raw_endpoints, (list, tuple))
            and not raw_endpoints
            and not invalidated
        ):
            fallback = resolve_r40_fixed_direction_fallback(
                peer_profile,
                peer.profile_id,
            )
            if fallback.resolved:
                line_1_side = fallback.line_1_route_side
                source = _PEER_PFG_SOURCE_ROLE_FALLBACK

    if not source:
        return blank
    return pfg_values(peer_profile, line_1_side, source)


def _r4_0_editor_seed(
    project: RouteProject,
    shelf_id: str,
) -> dict[str, object]:
    """Seed editable facts without selecting a provider or inventing optics."""

    from utils.rls_config.common import FIBER_TYPES

    shelf = next(
        (item for item in project.shelves if item.shelf_id == shelf_id),
        None,
    )
    if shelf is None:
        raise ValueError("The selected shelf is no longer in the route.")
    site = project.site_by_key(shelf.site_key)
    facts = _r4_0_review_facts(project, shelf_id)

    def line(side: str) -> dict[str, object]:
        raw_fiber = facts.get(f"{side}_span_fiber_type", "")
        fiber = (
            str(raw_fiber)
            if isinstance(raw_fiber, str) and raw_fiber in FIBER_TYPES
            else ""
        )
        loss = facts.get(f"{side}_span_loss_db")
        return {
            "link_name": str(facts.get(f"{side}_span_link_name", "") or ""),
            "neighbor_node": str(
                facts.get(f"{side}_neighbor_tid", "") or ""
            ),
            "fiber_type": fiber,
            "expected_loss_db": (
                float(loss)
                if isinstance(loss, (int, float)) and not isinstance(loss, bool)
                else None
            ),
            "represented_by_route_span": bool(
                facts.get(f"{side}_span_present", False)
            ),
            "outbound_flow": str(
                facts.get(f"{side}_outbound_flow", "") or ""
            ),
            "inbound_flow": str(
                facts.get(f"{side}_inbound_flow", "") or ""
            ),
            "propagation_reviewed": bool(
                facts.get(f"{side}_propagation_reviewed", False)
            ),
            "inbound_peer_reviewed": bool(
                facts.get(f"{side}_inbound_peer_reviewed", False)
            ),
            "inbound_peer_link_name": str(
                facts.get(f"{side}_inbound_peer_link_name", "") or ""
            ),
            "inbound_peer_loss_db": facts.get(
                f"{side}_inbound_peer_loss_db"
            ),
            "inbound_peer_fiber_type": str(
                facts.get(f"{side}_inbound_peer_fiber_type", "") or ""
            ),
            # Read-only route context carried into the exact editor. These
            # values support review but are not inserted into an R40 request.
            "distance_km": facts.get(f"{side}_span_distance_km"),
            "circuit_id": str(
                facts.get(f"{side}_span_circuit_id", "") or ""
            ),
            "fiber_start": facts.get(f"{side}_span_fiber_start"),
            "fiber_end": facts.get(f"{side}_span_fiber_end"),
            "source_fiber_label": str(
                facts.get(f"{side}_span_source_fiber_label", "") or ""
            ),
        }

    raw_site_id = site.network_site_id.strip() if site is not None else ""
    source_evidence = shelf.source_evidence
    reviewed_hardware = {
        "route_role": shelf.profile_id,
        "chassis": str(source_evidence.get("chassis", "") or "").strip(),
        "shelf_variant": shelf.shelf_variant,
        "shelf_band": str(source_evidence.get("band", "") or "").strip(),
        "topology": str(source_evidence.get("topology", "") or "").strip(),
        "add_drop_structure": str(
            source_evidence.get("add_drop_structure", "") or ""
        ).strip(),
        "protection_type": str(
            source_evidence.get("protection_type", "") or ""
        ).strip(),
        "module_inventory": source_evidence.get("module_inventory", ()),
        "line_endpoints": source_evidence.get("line_endpoints", ()),
        "power_label": shelf.power_label,
        "raman_label": shelf.raman_label,
    }
    provider_resolution, direction_resolution = (
        _r4_0_provider_prepopulation(project, shelf)
    )
    direction_side = str(
        direction_resolution.get("line_1_route_side", "") or ""
    ).strip()
    direction_status = str(
        direction_resolution.get("status", "") or ""
    ).strip()
    direction_prepopulated = bool(
        _r40_sole_candidate_provider_id(provider_resolution)
        and direction_side in {"A", "Z"}
        and direction_status in {"exact_match", "controlled_fallback"}
    )
    lines_by_side = {
        "A": line("a"),
        "Z": line("z"),
    }
    for side, side_seed in lines_by_side.items():
        if side_seed["represented_by_route_span"] is True:
            side_seed.update(
                _r4_0_peer_pfg_prepopulation(
                    project,
                    shelf,
                    side,
                )
            )
        else:
            side_seed.update(
                {
                    "neighbor_line_mux_pfg": "",
                    "neighbor_line_demux_pfg": "",
                    "neighbor_pfg_source": "",
                }
            )
    reviewed_fields = [
        field_name
        for field_name, value in (
            ("shelf_name", shelf.tid),
            ("site_name", site.name if site is not None else ""),
            ("site_id", raw_site_id if raw_site_id.isdigit() else ""),
            ("site_description", project.title),
            ("site_address", site.address if site is not None else ""),
            ("loopback_ip", shelf.primary_oam_ip),
            ("ospf_area", project.ospf_area),
            ("diagram_optical_band", _route_header_optical_band(project)),
        )
        if str(value or "").strip()
    ]
    for field_name, value in reviewed_hardware.items():
        if (
            isinstance(value, (list, tuple)) and bool(value)
        ) or (
            not isinstance(value, (list, tuple))
            and bool(str(value or "").strip())
        ):
            reviewed_fields.append(f"reviewed_hardware.{field_name}")
    for side, side_seed in lines_by_side.items():
        if side_seed["represented_by_route_span"] is not True:
            continue
        for field_name in (
            "link_name",
            "neighbor_node",
            "fiber_type",
            "expected_loss_db",
            "distance_km",
            "circuit_id",
            "fiber_start",
            "fiber_end",
            "source_fiber_label",
            "outbound_flow",
            "inbound_flow",
            "inbound_peer_link_name",
            "inbound_peer_loss_db",
            "inbound_peer_fiber_type",
        ):
            value = side_seed.get(field_name)
            if value is not None and str(value).strip():
                reviewed_fields.append(f"{side}.{field_name}")
    peer_pfg_suggestions = tuple(
        f"{side}:{side_seed['neighbor_pfg_source']}"
        for side, side_seed in lines_by_side.items()
        if (
            side_seed["represented_by_route_span"] is True
            and side_seed.get("neighbor_line_mux_pfg")
            and side_seed.get("neighbor_line_demux_pfg")
            and side_seed.get("neighbor_pfg_source")
        )
    )
    controlled_derivations = [
        "shelf_label_from_site_name",
        "member_name_from_tid",
        "hostname_from_tid",
    ]
    controlled_derivations.extend(
        item.replace(":", "_neighbor_pfg_from_", 1)
        for item in peer_pfg_suggestions
    )
    if direction_prepopulated:
        controlled_derivations.append(
            (
                "fixed_direction_from_direct_endpoint"
                if direction_status == "exact_match"
                else "fixed_direction_from_audited_provider_role_fallback"
            )
        )
    controlled_defaults = [
        "target_build_schema_4.00.00_vendor_baseline_unverified",
        "bay_number_zero",
        "physical_shelf_zero",
        "input_patch_loss_0.5_db",
        "output_patch_loss_0.5_db",
        "repair_margin_2_db",
        "high_loss_threshold_3_db",
    ]
    if not raw_site_id.isdigit():
        controlled_defaults.insert(0, "site_id_zero_when_absent")
    represented_side_count = sum(
        1
        for side_seed in lines_by_side.values()
        if side_seed["represented_by_route_span"] is True
    )
    remote_pfg_manual_field = (
        "prepopulated_remote_pfg_review"
        if represented_side_count
        and len(peer_pfg_suggestions) == represented_side_count
        else "remote_pfg_identities"
    )
    controlled_derivations.append(
        "provider_deployment_controls_included_automatically"
    )
    manual_fields = [
        (
            "prepopulated_provider_and_installed_bom_review"
            if provider_resolution.get("preselect_allowed") is True
            else "exact_provider_and_installed_bom"
        ),
        (
            "prepopulated_fixed_direction_review"
            if direction_prepopulated
            else "fixed_direction_to_route_side"
        ),
        remote_pfg_manual_field,
        "planner_ospcfib",
        "unrepresented_external_degree",
    ]
    policy_exclusions = [
        "customer_managed_ntp_omitted",
        "optional_frame_location_omits_shelf_location_cli_when_blank",
    ]
    if shelf.profile_id == "ila":
        policy_exclusions.append("ila_colan_prohibited")
    else:
        controlled_defaults.append(
            "terminal_colan_deferred_no_commands_by_default"
        )
        policy_exclusions.append(
            "terminal_colan_optional_for_factory_staging"
        )
    return {
        "shelf_name": shelf.tid,
        "shelf_label": site.name if site is not None else "",
        "site_name": site.name if site is not None else "",
        "site_id": int(raw_site_id) if raw_site_id.isdigit() else 0,
        "site_description": project.title,
        "site_address": site.address if site is not None else "",
        "member_name": shelf.tid,
        "hostname": shelf.tid,
        # The supplied R4.0.0 upgrade procedures identify Rel. 4.00.00 as the
        # documented baseline. This is editable planning metadata, never an
        # assertion that ATLAS observed the running target shelf.
        "target_software_build": DEFAULT_R40_TARGET_BUILD_SCHEMA,
        # Legacy B4 is a physical rack/location input. A site code or TID is
        # not rack evidence, so leave it blank unless a future diagram schema
        # supplies that fact. Blank means the shelf-location CLI is omitted.
        "frame_identification_code": "",
        "bay_number": 0,
        "physical_shelf": 0,
        "loopback_ip": shelf.primary_oam_ip,
        "ospf_area": project.ospf_area,
        "diagram_optical_band": _route_header_optical_band(project),
        "reviewed_hardware": reviewed_hardware,
        "provider_resolution": provider_resolution,
        "direction_resolution": direction_resolution,
        "line_semantics": (
            "unidirectional_amplifier_path"
            if shelf.profile_id == "ila"
            else "bidirectional_degree"
        ),
        "line_1_route_side": (
            direction_side if direction_prepopulated else ""
        ),
        "prepopulation": {
            "route_reviewed_fields": tuple(reviewed_fields),
            "controlled_derivations": tuple(controlled_derivations),
            "controlled_defaults": tuple(controlled_defaults),
            "policy_exclusions": tuple(policy_exclusions),
            "manual_fields": tuple(manual_fields),
            "peer_pfg_suggestions": peer_pfg_suggestions,
        },
        # Route sides remain independent until the operator maps the fixed
        # local outputs. An RLA side is one bidirectional mux/demux degree;
        # missing additional hardware remains blank and is never synthesized
        # from the return propagation on the represented degree.
        "lines_by_side": lines_by_side,
    }


def _apply_r40_reviewed_lines_to_links(
    rows: Iterable[_ShelfEditorRow],
    links: Iterable[RouteLink],
    shelf_id: str,
    request: Any,
) -> list[RouteLink]:
    """Apply local R4.0 line-output facts to bidirectional route spans.

    The request assigns its first fixed local line-output to route side A or
    Z. A two-record provider assigns its second output to the opposite side;
    a one-degree ROADM request has ``line_2=None`` because the first degree's
    paired mux/demux already carries both traffic directions. For the DLE
    provider the two records are opposing amplifier egress paths. The endpoint
    review on the ordered ``from`` shelf is the A→Z propagation; the ``to``
    review is Z→A. An absent provider degree remains unrepresented and is
    never populated by cloning reverse traffic from the same physical span.
    """

    row_list = list(rows)
    shelf_index = next(
        (
            index
            for index, row in enumerate(row_list)
            if row.shelf_id == shelf_id
        ),
        None,
    )
    if shelf_index is None:
        raise ValueError("The reviewed R4.0 shelf is no longer in the route.")
    link_list = _reconcile_route_links(
        row_list,
        links,
        populate_missing=True,
    )
    line_1_side = getattr(request, "line_1_route_side", "")
    if line_1_side not in {"A", "Z"}:
        raise ValueError(
            "R4.0 local line-output 1 must be assigned to route side A or Z."
        )
    line_by_side: dict[str, tuple[Any, int]] = {
        line_1_side: (request.line_1, 1),
    }
    if request.line_2 is not None:
        line_by_side[
            "Z" if line_1_side == "A" else "A"
        ] = (request.line_2, 2)
    line_records: list[tuple[str, Any, int]] = []
    if shelf_index > 0:
        if "A" not in line_by_side:
            raise ValueError(
                "The selected exact R4.0 provider has no reviewed physical "
                "degree/path for this shelf's A-side adjacent span."
            )
        line, record_number = line_by_side["A"]
        line_records.append(
            (
                row_list[shelf_index - 1].shelf_id,
                line,
                record_number,
            )
        )
    if shelf_index < len(row_list) - 1:
        if "Z" not in line_by_side:
            raise ValueError(
                "The selected exact R4.0 provider has no reviewed physical "
                "degree/path for this shelf's Z-side adjacent span."
            )
        line, record_number = line_by_side["Z"]
        line_records.append(
            (
                row_list[shelf_index + 1].shelf_id,
                line,
                record_number,
            )
        )
    for neighbor_id, line, record_number in line_records:
        candidates = [
            index
            for index, link in enumerate(link_list)
            if {link.from_shelf_id, link.to_shelf_id}
            == {shelf_id, neighbor_id}
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"R4.0 local line-output {record_number} must map to exactly "
                "one adjacent route link."
            )
        link_index = candidates[0]
        link = link_list[link_index]
        if len(link.paths) != 1:
            raise ValueError(
                f"R4.0 local line-output {record_number} requires exactly "
                "one reviewed physical optical span on its adjacent route "
                "link."
            )
        prior = link.paths[0]
        shelf_key = shelf_id.casefold()
        prior_endpoint_review = next(
            (
                review
                for review in prior.endpoint_reviews
                if review.shelf_id.casefold() == shelf_key
            ),
            None,
        )
        changed = (
            (
                prior_endpoint_review.expected_loss_db
                if prior_endpoint_review is not None
                else prior.expected_loss_db
            )
            != line.expected_loss_db
            or (
                prior_endpoint_review.fiber_type
                if prior_endpoint_review is not None
                else prior.fiber_type
            )
            != line.fiber_type
            or (
                prior_endpoint_review.link_name
                if prior_endpoint_review is not None
                else prior.link_name
            )
            != line.link_name
        )
        endpoint_reviews = tuple(
            review
            for review in prior.endpoint_reviews
            if review.shelf_id.casefold() != shelf_key
        ) + (
            PathEndpointReview(
                shelf_id=shelf_id,
                link_name=line.link_name,
                expected_loss_db=line.expected_loss_db,
                fiber_type=line.fiber_type,
            ),
        )
        changed = changed or any(
            review.link_name != prior.link_name
            or review.expected_loss_db != prior.expected_loss_db
            or review.fiber_type != prior.fiber_type
            for review in endpoint_reviews
        )
        all_endpoints_reviewed = {
            review.shelf_id.casefold() for review in endpoint_reviews
        } >= {
            link.from_shelf_id.casefold(),
            link.to_shelf_id.casefold(),
        }
        reviewed_path = OpticalPath(
            path_id=prior.path_id,
            path_role=prior.path_role or "route",
            link_name=prior.link_name,
            expected_loss_db=prior.expected_loss_db,
            distance_km=prior.distance_km,
            fiber_type=prior.fiber_type,
            circuit_id=prior.circuit_id,
            fiber_start=prior.fiber_start,
            fiber_end=prior.fiber_end,
            review_state=(
                "pending"
                if not all_endpoints_reviewed
                else "corrected"
                if changed or prior.review_state == "corrected"
                else "confirmed"
            ),
            source_evidence=dict(prior.source_evidence),
            endpoint_reviews=endpoint_reviews,
        )
        link_list[link_index] = replace(link, paths=(reviewed_path,))
    return link_list


def build_route_project(
    *,
    route_code: str,
    title: str,
    revision: str,
    rows: Iterable[_ShelfEditorRow],
    project_id: str = "",
    notes: str = "",
    ospf_area: str = "",
    links: Iterable[RouteLink] = (),
    diagram_source: Optional[Mapping[str, Any]] = None,
    require_valid: bool = True,
) -> RouteProject:
    """Build a canonical route project while preserving shelf order.

    Site rows are deduplicated case-insensitively by their visible site code.
    Conflicting names for the same code are rejected because they would make
    the FBN table and IRM counts ambiguous.
    """

    row_list = list(rows)
    if require_valid and not route_code.strip():
        raise ValueError("Route code is required.")
    if require_valid and not title.strip():
        raise ValueError("Project title is required.")
    if require_valid and not revision.strip():
        raise ValueError("Revision is required.")
    if not row_list:
        raise ValueError("Add at least one shelf before saving or exporting.")

    sites: list[Site] = []
    canonical_sites: dict[str, Site] = {}
    used_site_keys: set[str] = set()
    shelves: list[ShelfInstance] = []

    for row_index, row in enumerate(row_list, start=1):
        code = row.site_code.strip()
        name = row.site_name.strip()
        code_key = code.casefold() if code else f"__missing_site_{row_index}"
        if require_valid and (not code or not name):
            raise ValueError(f"Shelf {row.tid or row.shelf_id} needs a site code and name.")

        existing = canonical_sites.get(code_key)
        if (
            existing is not None
            and existing.name.casefold() != name.casefold()
            and not require_valid
        ):
            code_key = f"{code_key}__conflict_{row_index}"
            existing = None
        if existing is None:
            base_site_key = row.site_key.strip() or _site_key_for(
                code or f"review-{row_index}"
            )
            site_key = base_site_key
            suffix = 2
            while site_key.casefold() in used_site_keys:
                site_key = f"{base_site_key}-{suffix}"
                suffix += 1
            existing = Site(
                site_key=site_key,
                code=code,
                name=name,
                address=row.site_address,
                network_site_id=row.network_site_id,
            )
            canonical_sites[code_key] = existing
            used_site_keys.add(site_key.casefold())
            sites.append(existing)
        elif existing.name.casefold() != name.casefold():
            raise ValueError(
                f"Site code {code!r} has conflicting names "
                f"({existing.name!r} and {name!r})."
            )

        shelves.append(
            ShelfInstance(
                shelf_id=row.shelf_id.strip() or uuid4().hex,
                profile_id=row.profile_id,
                software_release=row.software_release.strip(),
                shelf_variant=row.shelf_variant.strip(),
                site_key=existing.site_key,
                tid=row.tid.strip(),
                primary_oam_ip=row.primary_oam_ip.strip(),
                power_label=row.power_label.strip(),
                raman_label=row.raman_label.strip(),
                notes=row.notes,
                profile_payload=dict(row.profile_payload),
                review_state=row.review_state,  # type: ignore[arg-type]
                source_evidence=dict(row.source_evidence),
            )
        )

    project = RouteProject(
        project_id=project_id.strip() or uuid4().hex,
        route_code=route_code.strip(),
        title=title.strip(),
        ospf_area=ospf_area.strip(),
        revision=revision.strip(),
        sites=tuple(sites),
        shelves=tuple(shelves),
        links=tuple(links),
        notes=notes,
        diagram_source=dict(diagram_source or {}),
    )
    if require_valid:
        issues = project.validate()
        errors = [issue for issue in issues if getattr(issue, "is_error", True)]
        if errors:
            formatted = "\n".join(
                f"• {getattr(issue, 'message', str(issue))}" for issue in errors
            )
            raise ValueError(f"Route project validation failed:\n{formatted}")
    return project


class RlsRouteFrame(ttk.Frame):
    """Master/detail route editor for mixed Ciena RLS shelf projects."""

    def __init__(self, parent: tk.Widget, controller: Any = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._rows: list[_ShelfEditorRow] = []
        self._links: list[RouteLink] = []
        self._links_populated = False
        self._project_id = uuid4().hex
        self._project_notes = ""
        self._diagram_source: Mapping[str, Any] = {}
        self._attached_workbook_diagram: WorkbookDiagram | None = None
        self._current_path: Optional[Path] = None
        self._dirty = False
        self._editor_dirty = False
        self._editor_shelf_id = ""
        self._loading_project = False
        self._loading_editor = False
        self._editor_power_is_atlas_default = False
        self._restoring_tree_selection = False
        self._destroyed = False
        self._job_generation = 0
        self._foreground_job: tuple[str, int] | None = None
        self._config_labels: dict[str, str] = {}
        self._config_after_id: Optional[str] = None
        self._poll_after_id: Optional[str] = None
        self._preview_fingerprint = ""
        self._preview_path: Optional[Path] = None
        self._preview_tempdirs: list[tempfile.TemporaryDirectory[str]] = []
        self._config_review_window: Optional[tk.Toplevel] = None
        self._worker_results: Queue[_WorkerResult] = Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="atlas-rls-route",
        )
        self._diagram_provider_factory: Callable[[], Any] = (
            _default_diagram_provider
        )
        self._profile_pairs = profile_choices()
        self._profile_id_by_label = {
            display_name: profile_id
            for profile_id, display_name in self._profile_pairs
        }
        self._profile_label_by_id = dict(self._profile_pairs)
        self._build()
        self._refresh_tree()
        self._refresh_status()
        self._poll_after_id = self.after(100, self._drain_worker_results)
        self.bind("<Destroy>", self._on_destroy, add="+")
        _log_route_event("Route Builder initialized.")

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky=tk.EW, padx=8, pady=(5, 3))
        ttk.Label(
            header,
            text="Ciena RLS R4.0 Route Project & FBN Deliverable",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text=PLANNING_CLI_NOTICE,
            foreground="#8a4d00",
            justify=tk.LEFT,
            wraplength=1120,
        ).pack(anchor=tk.W, pady=(2, 0))

        project_group = ttk.LabelFrame(self, text="Project")
        project_group.grid(row=1, column=0, sticky=tk.EW, padx=8, pady=4)
        project_group.grid_columnconfigure(3, weight=1)

        self._route_code_var = tk.StringVar()
        self._title_var = tk.StringVar()
        self._revision_var = tk.StringVar(value="1")
        self._ospf_area_var = tk.StringVar()
        self._route_fiber_type_var = tk.StringVar()
        self._route_fiber_observation_var = tk.StringVar(
            value="Diagram fiber: not available."
        )
        self._project_entry(
            project_group, row=0, column=0, label="Route", variable=self._route_code_var
        )
        self._project_entry(
            project_group,
            row=0,
            column=2,
            label="Deliverable title",
            variable=self._title_var,
            width=48,
        )
        self._project_entry(
            project_group,
            row=0,
            column=4,
            label="Revision",
            variable=self._revision_var,
            width=9,
        )
        self._project_entry(
            project_group,
            row=1,
            column=0,
            label="OSPF area",
            variable=self._ospf_area_var,
        )
        ttk.Label(project_group, text="Native CLI fiber type:").grid(
            row=1, column=2, sticky=tk.E, padx=(7, 3), pady=5
        )
        ttk.Combobox(
            project_group,
            textvariable=self._route_fiber_type_var,
            values=FIBER_TYPES,
            state="readonly",
            width=24,
        ).grid(row=1, column=3, sticky=tk.W, padx=(0, 5), pady=5)
        self._apply_route_fiber_button = ttk.Button(
            project_group,
            text="Apply to all spans",
            command=self._apply_route_native_fiber_type,
        )
        self._apply_route_fiber_button.grid(
            row=1, column=4, columnspan=2, sticky=tk.W, padx=(0, 8), pady=5
        )
        ttk.Label(
            project_group,
            textvariable=self._route_fiber_observation_var,
            foreground="#666666",
        ).grid(
            row=2,
            column=0,
            columnspan=4,
            sticky=tk.W,
            padx=(7, 8),
            pady=5,
        )
        ttk.Label(
            project_group,
            text=(
                "Choose one audited native CLI token for the route. The "
                "diagram label remains separate and is never alias-converted."
            ),
            foreground="#666666",
        ).grid(
            row=2,
            column=4,
            columnspan=2,
            sticky=tk.W,
            padx=(7, 8),
            pady=5,
        )
        for variable in (
            self._route_code_var,
            self._title_var,
            self._revision_var,
        ):
            variable.trace_add("write", self._on_project_edited)
        self._ospf_area_var.trace_add("write", self._on_ospf_area_edited)

        workspace = ttk.Panedwindow(self, orient=tk.VERTICAL)
        workspace.grid(row=2, column=0, sticky=tk.NSEW, padx=8, pady=4)

        list_group = ttk.LabelFrame(workspace, text="Ordered route shelves")
        editor_group = ttk.LabelFrame(workspace, text="Shelf details")
        workspace.add(list_group, weight=3)
        workspace.add(editor_group, weight=2)
        list_group.grid_columnconfigure(0, weight=1)
        list_group.grid_rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(
            list_group,
            columns=_TABLE_COLUMNS,
            show="headings",
            selectmode="browse",
            height=10,
        )
        column_setup = {
            "order": ("#", 42, tk.CENTER),
            "profile": ("Shelf type", 145, tk.W),
            "site": ("Site", 92, tk.W),
            "tid": ("TID", 126, tk.W),
            "ip": ("Primary OAM IP", 122, tk.W),
            "release": ("Release", 86, tk.W),
            "raman": ("RAMAN", 80, tk.W),
            "power": ("Power", 78, tk.W),
            "provider": ("Provider", 285, tk.W),
            "direction": ("Direction map", 225, tk.W),
            "readiness": ("CLI readiness", 165, tk.W),
        }
        for key, (heading, width, anchor) in column_setup.items():
            self._tree.heading(key, text=heading)
            self._tree.column(key, width=width, minwidth=36, anchor=anchor)
        self._tree.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll = ttk.Scrollbar(
            list_group, orient=tk.VERTICAL, command=self._tree.yview
        )
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        xscroll = ttk.Scrollbar(
            list_group, orient=tk.HORIZONTAL, command=self._tree.xview
        )
        xscroll.grid(row=1, column=0, sticky=tk.EW)
        self._tree.configure(
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        order_controls = ttk.Frame(list_group)
        order_controls.grid(row=2, column=0, columnspan=2, sticky=tk.EW, pady=4)
        ttk.Button(
            order_controls, text="Move Up", command=lambda: self._move_selected(-1)
        ).pack(side=tk.LEFT, padx=(2, 3))
        ttk.Button(
            order_controls, text="Move Down", command=lambda: self._move_selected(1)
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(
            order_controls, text="Remove", command=self._remove_selected
        ).pack(side=tk.LEFT, padx=3)
        ttk.Label(
            order_controls,
            text="Rack diagrams are populated in this order, eight shelves per rack.",
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=12)

        self._build_editor(editor_group)

        footer = ttk.Frame(self)
        footer.grid(row=3, column=0, sticky=tk.EW, padx=8, pady=(3, 7))
        self._new_button = ttk.Button(
            footer, text="New", command=self._new_project
        )
        self._new_button.pack(side=tk.LEFT, padx=(0, 3))
        self._open_button = ttk.Button(
            footer, text="Open Project…", command=self._open_project
        )
        self._open_button.pack(side=tk.LEFT, padx=3)
        self._save_button = ttk.Button(
            footer, text="Save Project…", command=self._save_project
        )
        self._save_button.pack(side=tk.LEFT, padx=3)
        self._upload_button = ttk.Button(
            footer,
            text="Upload Route Diagram…",
            command=self._upload_route_diagram,
        )
        self._upload_button.pack(side=tk.LEFT, padx=(8, 3))
        self._reattach_diagram_button = ttk.Button(
            footer,
            text="Reattach Diagram…",
            command=self._reattach_route_diagram,
        )
        self._reattach_diagram_button.pack(side=tk.LEFT, padx=3)
        self._preview_button = ttk.Button(
            footer,
            text="Preview MOP",
            command=self._preview_mop,
        )
        self._preview_button.pack(side=tk.LEFT, padx=3)
        self._bundle_button = ttk.Button(
            footer, text="Export Route Bundle…", command=self._export_bundle
        )
        self._bundle_button.pack(side=tk.LEFT, padx=3)
        self._status_var = tk.StringVar()
        ttk.Label(
            footer,
            textvariable=self._status_var,
            foreground="#555555",
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=9)

    @staticmethod
    def _project_entry(
        parent: tk.Widget,
        *,
        row: int,
        column: int,
        label: str,
        variable: tk.StringVar,
        width: int = 22,
    ) -> None:
        ttk.Label(parent, text=f"{label}:").grid(
            row=row, column=column, sticky=tk.E, padx=(7, 3), pady=5
        )
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row, column=column + 1, sticky=tk.EW, padx=(0, 8), pady=5
        )

    def _build_editor(self, parent: ttk.LabelFrame) -> None:
        for column in (1, 3, 5):
            parent.grid_columnconfigure(column, weight=1)

        first_profile = self._profile_pairs[0][1] if self._profile_pairs else ""
        first_profile_id = (
            self._profile_pairs[0][0] if self._profile_pairs else ""
        )
        initial_power = power_label_for_profile(first_profile_id)
        self._profile_var = tk.StringVar(value=first_profile)
        self._site_code_var = tk.StringVar()
        self._site_name_var = tk.StringVar()
        self._tid_var = tk.StringVar()
        self._ip_var = tk.StringVar()
        self._release_var = tk.StringVar(value=R40_UI_RELEASE)
        self._variant_var = tk.StringVar()
        self._raman_var = tk.StringVar()
        self._power_var = tk.StringVar(value=initial_power)
        self._editor_power_is_atlas_default = bool(initial_power)
        for variable in (
            self._profile_var,
            self._site_code_var,
            self._site_name_var,
            self._tid_var,
            self._ip_var,
            self._release_var,
            self._variant_var,
            self._raman_var,
            self._power_var,
        ):
            variable.trace_add("write", self._on_editor_edited)
        self._power_var.trace_add("write", self._on_power_edited)

        profile = ttk.Combobox(
            parent,
            textvariable=self._profile_var,
            values=tuple(label for _profile_id, label in self._profile_pairs),
            state="readonly",
            width=24,
        )
        profile.bind("<<ComboboxSelected>>", self._on_profile_selected)
        self._editor_field(parent, 0, 0, "Shelf type", profile)
        self._editor_field(
            parent, 0, 2, "Site code", ttk.Entry(parent, textvariable=self._site_code_var)
        )
        self._editor_field(
            parent, 0, 4, "Site name", ttk.Entry(parent, textvariable=self._site_name_var)
        )
        self._editor_field(
            parent, 1, 0, "TID", ttk.Entry(parent, textvariable=self._tid_var)
        )
        self._editor_field(
            parent,
            1,
            2,
            "Primary OAM IP",
            ttk.Entry(parent, textvariable=self._ip_var),
        )
        self._editor_field(
            parent,
            1,
            4,
            "Software release",
            ttk.Entry(
                parent,
                textvariable=self._release_var,
                state="readonly",
            ),
        )
        self._editor_field(
            parent,
            2,
            0,
            "Shelf variant / PEC",
            ttk.Entry(parent, textvariable=self._variant_var),
        )
        self._editor_field(
            parent,
            2,
            2,
            "RAMAN display",
            ttk.Entry(parent, textvariable=self._raman_var),
        )
        self._editor_field(
            parent,
            2,
            4,
            "Power",
            ttk.Entry(parent, textvariable=self._power_var),
        )

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, columnspan=6, sticky=tk.EW, pady=(6, 5))
        ttk.Button(actions, text="Add Shelf", command=self._add_shelf).pack(
            side=tk.LEFT, padx=(7, 3)
        )
        ttk.Button(actions, text="Update Selected", command=self._update_selected).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Button(
            actions,
            text="Confirm & Next Pending",
            command=self._confirm_and_next_pending,
        ).pack(side=tk.LEFT, padx=3)
        self._review_config_button = ttk.Button(
            actions,
            text="Review Configuration…",
            command=self._review_selected_configuration,
        )
        self._review_config_button.pack(side=tk.LEFT, padx=3)
        ttk.Button(actions, text="Clear Editor", command=self._clear_editor).pack(
            side=tk.LEFT, padx=3
        )
        ttk.Label(
            actions,
            text=(
                "RAMAN and power text are deliverable labels. Reviewed "
                "structured SRA evidence separately gates provider "
                "compatibility; it never enables CLI."
            ),
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=12)

    @staticmethod
    def _editor_field(
        parent: tk.Widget,
        row: int,
        column: int,
        label: str,
        control: tk.Widget,
    ) -> None:
        ttk.Label(parent, text=f"{label}:").grid(
            row=row, column=column, sticky=tk.E, padx=(7, 3), pady=4
        )
        control.grid(
            row=row, column=column + 1, sticky=tk.EW, padx=(0, 8), pady=4
        )

    def _on_project_edited(self, *_args: object) -> None:
        if self._loading_project:
            return
        self._dirty = True
        self._invalidate_project_results()
        self._schedule_config_evaluation()
        self._refresh_status()

    def _on_ospf_area_edited(self, *_args: object) -> None:
        if self._loading_project:
            return
        self._rows, cleared = _clear_provider_payloads(self._rows)
        if cleared:
            _log_route_event(
                f"OSPF area changed; cleared {cleared} route-bound "
                "configuration payload(s).",
                logging.WARNING,
            )
        self._on_project_edited()
        if cleared:
            self._refresh_tree()
            self._refresh_status(
                "OSPF area changed. Review every affected configuration again."
            )

    def _sync_route_fiber_controls(self) -> None:
        """Refresh the one route-wide native token and source-label summary."""

        from utils.rls_config.route_project import route_native_fiber_review

        status, token = route_native_fiber_review(self._links)
        observed = _observed_route_fiber_types(
            self._diagram_source,
            self._links,
        )
        if status == "confirmed":
            self._route_fiber_type_var.set(token)
        elif len(observed) == 1 and observed[0] in FIBER_TYPES:
            self._route_fiber_type_var.set(observed[0])
        else:
            self._route_fiber_type_var.set("")
        if not observed:
            observed_text = "Diagram fiber: not available."
        elif len(observed) == 1:
            observed_text = f"Diagram fiber: {observed[0]}."
        else:
            observed_text = (
                "Diagram fibers conflict: " + ", ".join(observed) + "."
            )
        if status == "confirmed":
            observed_text += f" Confirmed native route token: {token}."
        elif status == "invalid":
            observed_text += (
                " Stored route-wide native review is invalid and blocks CLI."
            )
        elif status == "missing" and self._links:
            observed_text += " Select and apply one native token for all spans."
        self._route_fiber_observation_var.set(observed_text)

    def _apply_route_native_fiber_type(self) -> bool:
        """Apply one operator-confirmed native token to every route path."""

        _log_route_event("Route-wide native fiber-type apply requested.")
        if not self._require_committed_editor():
            return False
        token = self._route_fiber_type_var.get().strip()
        if token not in FIBER_TYPES:
            messagebox.showwarning(
                "Native fiber type required",
                (
                    "Select one exact native RLS fiber token before applying "
                    "it to the route. Diagram labels are never alias-converted."
                ),
                parent=self,
            )
            _log_route_event(
                "Route-wide fiber apply refused: no supported native fiber type "
                "was selected.",
                logging.WARNING,
            )
            return False
        path_count = sum(len(link.paths) for link in self._links)
        if not path_count:
            messagebox.showwarning(
                "No optical spans",
                "Add or import the ordered route spans before applying a fiber type.",
                parent=self,
            )
            _log_route_event(
                "Route-wide fiber apply refused: route has no optical paths.",
                logging.WARNING,
            )
            return False

        from utils.rls_config.route_project import route_native_fiber_review

        status, prior_token = route_native_fiber_review(self._links)
        if status == "confirmed" and prior_token == token:
            self._refresh_status(
                f"Native fiber type {token} is already applied to every span."
            )
            _log_route_event(
                "Route-wide native fiber apply made no changes; the selected "
                "fiber type is already confirmed on every optical path."
            )
            return True
        payload_count = sum(bool(row.profile_payload) for row in self._rows)
        endpoint_review_count = sum(
            len(path.endpoint_reviews)
            for link in self._links
            for path in link.paths
        )
        if (
            payload_count or endpoint_review_count
        ) and not messagebox.askyesno(
            "Replace route fiber type",
            (
                "Changing the route-wide native fiber type clears every exact "
                "configuration payload and optical-path endpoint review. "
                "Continue?"
            ),
            parent=self,
        ):
            _log_route_event(
                "Route-wide native fiber change cancelled; reviewed "
                "configurations and endpoint paths were retained."
            )
            return False

        marker = {
            "value": token,
            "scope": "all_active_route_spans",
            "action": "operator_apply_route_native_fiber",
            "status": "confirmed",
            "deployable_cli": False,
        }
        reviewed_links: list[RouteLink] = []
        for link in self._links:
            reviewed_paths: list[OpticalPath] = []
            for path in link.paths:
                source_evidence = dict(path.source_evidence)
                source_evidence[_ROUTE_NATIVE_FIBER_REVIEW_KEY] = marker
                reviewed_paths.append(
                    replace(
                        path,
                        fiber_type=token,
                        review_state="pending",
                        source_evidence=source_evidence,
                        endpoint_reviews=(),
                    )
                )
            reviewed_links.append(
                replace(link, paths=tuple(reviewed_paths))
            )
        self._links = reviewed_links
        self._rows, cleared_payloads = _clear_provider_payloads(self._rows)
        self._committed_project_changed()
        self._sync_route_fiber_controls()
        self._refresh_tree()
        self._refresh_status(
            f"Applied native fiber type {token} to {path_count} route span"
            f"{'' if path_count == 1 else 's'}. Exact configuration and "
            "endpoint-path review remain required."
        )
        _log_route_event(
            "Applied one operator-confirmed native fiber type across all route "
            f"paths; paths={path_count}, cleared_payloads={cleared_payloads}, "
            f"cleared_endpoint_reviews={endpoint_review_count}. "
            "deployable_cli=false until exact reviews pass."
        )
        return True

    def _on_editor_edited(self, *_args: object) -> None:
        if self._loading_editor:
            return
        self._editor_dirty = True
        self._dirty = True
        self._invalidate_project_results()
        self._refresh_status("Shelf editor has uncommitted changes.")

    def _invalidate_project_results(self) -> None:
        """Invalidate every snapshot-derived result on the Tk thread."""

        self._job_generation += 1
        self._preview_fingerprint = ""
        self._preview_path = None
        self._config_labels.clear()
        tree = getattr(self, "_tree", None)
        if tree is not None:
            self._update_tree_readiness_cells()

    def _refresh_terminal_title_after_topology_change(self) -> bool:
        """Keep an imported terminal title aligned with the current A/Z order."""

        prior_marker = self._diagram_source.get("route_title_derivation")
        if (
            not isinstance(prior_marker, Mapping)
            or prior_marker.get("rule_id") != _TERMINAL_ROUTE_TITLE_RULE_ID
        ):
            return False
        source_sha256 = str(
            self._diagram_source.get("source_sha256", "") or ""
        ).strip()
        derived = _current_terminal_route_title(
            self._rows,
            prior_marker=prior_marker,
            source_sha256=source_sha256,
        )
        self._loading_project = True
        try:
            if derived is None:
                self._title_var.set("")
                self._diagram_source["route_title_derivation"] = {
                    "rule_id": _TERMINAL_ROUTE_TITLE_RULE_ID,
                    "status": "operator_review_required",
                    "value": "",
                    "source_sha256": source_sha256,
                    "deployable_cli": False,
                }
                return True
            title, marker = derived
            self._title_var.set(title)
            self._diagram_source["route_title_derivation"] = dict(marker)
            return True
        finally:
            self._loading_project = False

    def _committed_project_changed(self) -> None:
        self._dirty = True
        self._editor_dirty = False
        self._invalidate_project_results()
        self._schedule_config_evaluation()

    def _schedule_config_evaluation(self) -> None:
        if self._destroyed:
            return
        if self._config_after_id is not None:
            try:
                self.after_cancel(self._config_after_id)
            except tk.TclError:
                pass
        self._config_after_id = self.after(350, self._start_config_evaluation)

    def _start_config_evaluation(self) -> None:
        self._config_after_id = None
        if self._destroyed or self._foreground_job is not None:
            return
        try:
            project = self._build_project(require_valid=False)
            fingerprint = route_project_fingerprint(project)
        except (TypeError, ValueError):
            self._config_labels.clear()
            self._update_tree_readiness_cells()
            self._refresh_status(
                "Configuration assessment is waiting for complete route fields."
            )
            _log_route_event(
                "Configuration assessment deferred until route fields are complete.",
                logging.DEBUG,
            )
            return
        self._submit_background(
            "config",
            lambda: evaluate_route_configs(project),
            fingerprint=fingerprint,
        )

    def _submit_background(
        self,
        kind: str,
        work: Callable[[], Any],
        *,
        fingerprint: str = "",
        context: Any = None,
        foreground: bool = False,
    ) -> None:
        """Submit work without allowing a worker to touch Tk state."""

        if self._destroyed:
            return
        generation = self._job_generation
        if foreground:
            if self._foreground_job is not None:
                _log_route_event(
                    f"{kind} task refused because "
                    f"{self._foreground_job[0]} is still running.",
                    logging.WARNING,
                )
                messagebox.showwarning(
                    "Route task in progress",
                    "Wait for the current route task to finish.",
                    parent=self,
                )
                return
            self._foreground_job = (kind, generation)
            self._set_foreground_busy(True)
        _log_route_event(
            f"Submitted background {kind} task at route generation {generation}.",
            logging.INFO if foreground else logging.DEBUG,
        )
        try:
            self._executor.submit(
                _run_background_job,
                self._worker_results,
                kind=kind,
                generation=generation,
                fingerprint=fingerprint,
                work=work,
                context=context,
            )
        except RuntimeError as exc:
            if foreground:
                self._foreground_job = None
                self._set_foreground_busy(False)
            _log_route_event(
                f"Could not submit background {kind} task: "
                f"{friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Route task unavailable",
                friendly_error(exc, "The background task could not be started."),
                parent=self,
            )

    def _drain_worker_results(self) -> None:
        """Apply queued completions on the Tk event thread."""

        self._poll_after_id = None
        if self._destroyed:
            return
        while True:
            try:
                result = self._worker_results.get_nowait()
            except Empty:
                break
            self._handle_worker_result(result)
        if not self._destroyed:
            self._poll_after_id = self.after(100, self._drain_worker_results)

    def _handle_worker_result(self, result: _WorkerResult) -> None:
        foreground_match = self._foreground_job == (
            result.kind,
            result.generation,
        )
        if foreground_match:
            self._foreground_job = None
            self._set_foreground_busy(False)

        if not self._worker_result_is_current(result):
            self._discard_worker_context(result)
            self._refresh_status(
                f"Discarded stale {result.kind} result after route edits."
            )
            _log_route_event(
                f"Discarded stale {result.kind} result from generation "
                f"{result.generation}.",
                logging.WARNING,
            )
            return
        if result.error is not None:
            self._discard_worker_context(result)
            self._handle_worker_error(result.kind, result.error)
            return
        if result.kind == "diagram_import":
            self._apply_diagram_import(result.value)
        elif result.kind == "config":
            self._apply_config_evaluation(result.value)
        elif result.kind == "preview":
            self._complete_preview(result)
        elif result.kind == "bundle":
            self._complete_bundle(result.value)

    def _worker_result_is_current(self, result: _WorkerResult) -> bool:
        if result.generation != self._job_generation:
            return False
        if not result.fingerprint:
            return True
        try:
            current = route_project_fingerprint(
                self._build_project(require_valid=False)
            )
        except (TypeError, ValueError):
            return False
        return current == result.fingerprint

    @staticmethod
    def _discard_worker_context(result: _WorkerResult) -> None:
        if result.kind == "preview" and result.context is not None:
            try:
                result.context.cleanup()
            except Exception:
                LOGGER.debug("Could not remove stale MOP preview", exc_info=True)

    def _handle_worker_error(self, kind: str, error: Exception) -> None:
        _log_route_event(
            f"{kind} task failed: {friendly_error(error)}",
            logging.ERROR,
        )
        if kind == "config":
            self._config_labels.clear()
            self._update_tree_readiness_cells()
            self._refresh_status(
                "Configuration assessment failed; final bundle validation "
                "remains authoritative."
            )
            return
        title, fallback = {
            "diagram_import": (
                "Route diagram import failed",
                "The customer diagram could not be transcribed.",
            ),
            "preview": (
                "MOP preview failed",
                "The preview workbook could not be created.",
            ),
            "bundle": (
                "Route bundle export failed",
                "The route bundle could not be exported.",
            ),
        }.get(kind, ("Route task failed", "The route task could not be completed."))
        messagebox.showerror(
            title,
            friendly_error(error, fallback),
            parent=self,
        )

    def _set_foreground_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]

        def visit(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(
                    child,
                    (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Treeview),
                ):
                    try:
                        child.state(state)
                    except tk.TclError:
                        pass
                visit(child)

        visit(self)
        self._refresh_status("Working…" if busy else "")

    def _on_destroy(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self or self._destroyed:
            return
        self._destroyed = True
        self._job_generation += 1
        for callback_id in (self._config_after_id, self._poll_after_id):
            if callback_id is not None:
                try:
                    self.after_cancel(callback_id)
                except tk.TclError:
                    pass
        self._executor.shutdown(wait=False, cancel_futures=True)
        review_window = self._config_review_window
        if review_window is not None:
            try:
                self._close_config_review(
                    review_window,
                    reason="route_destroy",
                )
            except tk.TclError:
                pass
        for preview_dir in self._preview_tempdirs:
            try:
                preview_dir.cleanup()
            except Exception:
                LOGGER.debug("Could not remove MOP preview directory", exc_info=True)
        self._preview_tempdirs.clear()

    def _on_profile_selected(self, _event: object = None) -> None:
        # Release is a fixed product boundary; provider/hardware selection
        # remains an explicit action in the per-shelf review.
        self._release_var.set(R40_UI_RELEASE)
        default_power = power_label_for_profile(self._selected_profile_id())
        if not default_power:
            return
        if (
            self._power_var.get().strip()
            and not getattr(
                self,
                "_editor_power_is_atlas_default",
                False,
            )
        ):
            return
        was_loading = self._loading_editor
        self._loading_editor = True
        try:
            self._power_var.set(default_power)
        finally:
            self._loading_editor = was_loading
        self._editor_power_is_atlas_default = True

    def _on_power_edited(self, *_args: object) -> None:
        """Mark a typed power value as an operator override."""

        if not self._loading_editor:
            self._editor_power_is_atlas_default = False

    def _selected_profile_id(self) -> str:
        label = self._profile_var.get().strip()
        return self._profile_id_by_label.get(label, label)

    def _read_editor_row(
        self,
        *,
        shelf_id: str = "",
        site_key: str = "",
        notes: str = "",
        site_address: str = "",
        network_site_id: str = "",
        profile_payload: Optional[Mapping[str, Any]] = None,
        review_state: str = "manual",
        source_evidence: Optional[Mapping[str, Any]] = None,
    ) -> _ShelfEditorRow:
        values = {
            "shelf type": self._selected_profile_id().strip(),
            "site code": self._site_code_var.get().strip(),
            "site name": self._site_name_var.get().strip(),
            "TID": self._tid_var.get().strip(),
            "primary OAM IP": self._ip_var.get().strip(),
            "software release": self._release_var.get().strip(),
            "shelf variant / PEC": self._variant_var.get().strip(),
            "power": self._power_var.get().strip(),
        }
        missing = [label for label, value in values.items() if not value]
        if missing:
            raise ValueError("Complete these shelf fields: " + ", ".join(missing) + ".")
        if values["shelf type"] not in _R40_UI_PROFILE_ID_SET:
            raise ValueError("Select an RLS R4.0 Add/Drop, ILA, or ROADM role.")
        if not _release_is_exact(values["software release"], 4, 0):
            raise ValueError(
                "Software release is fixed to exact 'RLS R4.0' in this "
                "Route Builder."
            )
        try:
            ipaddress.ip_address(values["primary OAM IP"])
        except ValueError as exc:
            raise ValueError("Primary OAM IP must be a valid IP address.") from exc

        reviewed_source_evidence = dict(source_evidence or {})
        default_power = power_label_for_profile(values["shelf type"])
        if (
            getattr(self, "_editor_power_is_atlas_default", False)
            and default_power
            and values["power"] == default_power
        ):
            reviewed_source_evidence[_POWER_LABEL_ROLE_DEFAULT_KEY] = (
                _power_label_role_default_marker(values["shelf type"])
            )
        else:
            reviewed_source_evidence.pop(
                _POWER_LABEL_ROLE_DEFAULT_KEY,
                None,
            )

        return _ShelfEditorRow(
            shelf_id=shelf_id or uuid4().hex,
            profile_id=values["shelf type"],
            site_key=site_key or _site_key_for(values["site code"]),
            site_code=values["site code"],
            site_name=values["site name"],
            tid=values["TID"],
            primary_oam_ip=values["primary OAM IP"],
            software_release=values["software release"],
            shelf_variant=values["shelf variant / PEC"],
            raman_label=self._raman_var.get().strip(),
            power_label=values["power"],
            site_address=site_address,
            network_site_id=network_site_id,
            notes=notes,
            profile_payload=dict(profile_payload or {}),
            review_state=review_state,
            source_evidence=reviewed_source_evidence,
        )

    def _add_shelf(self) -> None:
        try:
            row = self._read_editor_row()
        except ValueError as exc:
            _log_route_event(
                f"Add shelf refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning("Shelf details incomplete", str(exc), parent=self)
            return
        self._rows.append(row)
        self._rows, cleared_payloads = _clear_provider_payloads(self._rows)
        self._rows, invalidated_directions = (
            _invalidate_route_direction_evidence(
                self._rows,
                reason="shelf added to ordered route",
            )
        )
        self._rows, endpoint_role_changes = (
            _reconcile_route_endpoint_profiles(self._rows)
        )
        links_populated = bool(getattr(self, "_links_populated", False))
        self._links = _reconcile_route_links(
            self._rows,
            getattr(self, "_links", ()),
            populate_missing=links_populated,
        )
        title_refresh = getattr(
            self,
            "_refresh_terminal_title_after_topology_change",
            None,
        )
        if callable(title_refresh):
            title_rederived = bool(title_refresh())
        elif hasattr(self, "_diagram_source") and hasattr(self, "_title_var"):
            title_rederived = bool(
                RlsRouteFrame._refresh_terminal_title_after_topology_change(
                    self
                )
            )
        else:
            title_rederived = False
        self._committed_project_changed()
        self._sync_route_fiber_controls()
        self._refresh_tree(select_id=row.shelf_id)
        self._refresh_status(
            "Shelf added. Route order updated."
            + (
                f" Cleared {cleared_payloads} route-bound configuration(s); "
                "review affected shelves again."
                if cleared_payloads
                else ""
            )
            + (
                f" Recomputed {endpoint_role_changes} A/Z endpoint role(s)."
                if endpoint_role_changes
                else ""
            )
            + (
                f" Invalidated {invalidated_directions} source direction "
                "suggestion(s)."
                if invalidated_directions
                else ""
            )
        )
        _log_route_event(
            f"Added shelf {row.tid!r} at site {row.site_code!r} "
            f"with role {row.profile_id!r}; route now has {len(self._rows)} "
            f"shelf(s); cleared_payloads={cleared_payloads}; "
            f"endpoint_role_changes={endpoint_role_changes}; "
            f"invalidated_direction_suggestions={invalidated_directions}; "
            f"title_rederived={str(title_rederived).casefold()}."
        )

    def _selected_index(self) -> Optional[int]:
        selected = self._tree.selection()
        if not selected:
            return None
        selected_id = selected[0]
        return next(
            (
                index
                for index, row in enumerate(self._rows)
                if row.shelf_id == selected_id
            ),
            None,
        )

    def _update_selected(self) -> bool:
        index = self._selected_index()
        if index is None:
            _log_route_event(
                "Update shelf refused: no shelf selected.", logging.WARNING
            )
            messagebox.showwarning(
                "No shelf selected",
                "Select a shelf in the route table before updating it.",
                parent=self,
            )
            return False
        current = self._rows[index]
        edited_site_code = self._site_code_var.get().strip()
        site_key = (
            current.site_key
            if edited_site_code.casefold() == current.site_code.casefold()
            else ""
        )
        try:
            replacement = self._read_editor_row(
                shelf_id=current.shelf_id,
                site_key=site_key,
                site_address=current.site_address,
                network_site_id=current.network_site_id,
                notes=current.notes,
                profile_payload=current.profile_payload,
                review_state=current.review_state,
                source_evidence=current.source_evidence,
            )
        except ValueError as exc:
            _log_route_event(
                f"Update shelf refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning("Shelf details incomplete", str(exc), parent=self)
            return False
        reviewed_evidence, sra_compatibility_changed = (
            _review_raman_callout_evidence(
                replacement.source_evidence,
                replacement.raman_label,
            )
        )
        replacement = replace(
            replacement,
            source_evidence=reviewed_evidence,
        )
        provider_fact_corrections = tuple(
            field_name
            for field_name in ("profile_id", "shelf_variant")
            if str(getattr(current, field_name)).strip()
            != str(getattr(replacement, field_name)).strip()
        )
        if provider_fact_corrections:
            corrected_evidence = dict(replacement.source_evidence)
            corrected_evidence[_PROVIDER_PRESELECTION_INVALIDATION_KEY] = {
                "reason": "operator corrected provider-relevant shelf facts",
                "fields": provider_fact_corrections,
                "deployable_cli": False,
            }
            replacement = replace(
                replacement,
                source_evidence=corrected_evidence,
            )
        identity_changed = (
            provider_identity_changed(current, replacement)
            or sra_compatibility_changed
        )
        payload_cleared = identity_changed and bool(current.profile_payload)
        if identity_changed:
            replacement = replace(replacement, profile_payload={})
        nondefault_source_evidence = set(current.source_evidence) - {
            _POWER_LABEL_ROLE_DEFAULT_KEY
        }
        if nondefault_source_evidence or current.review_state in {
            "pending",
            "confirmed",
            "corrected",
        }:
            review_state = (
                "corrected"
                if current.review_state == "corrected"
                or imported_values_changed(current, replacement)
                else "confirmed"
            )
            replacement = replace(replacement, review_state=review_state)
        self._rows[index] = replacement
        cleared_payloads = 0
        endpoint_role_changes = 0
        if identity_changed:
            self._rows, cleared_payloads = _clear_provider_payloads(self._rows)
            self._rows, endpoint_role_changes = (
                _reconcile_route_endpoint_profiles(self._rows)
            )
            replacement = self._rows[index]
        title_refresh = getattr(
            self,
            "_refresh_terminal_title_after_topology_change",
            None,
        )
        if identity_changed and callable(title_refresh):
            title_rederived = bool(title_refresh())
        elif (
            identity_changed
            and hasattr(self, "_diagram_source")
            and hasattr(self, "_title_var")
        ):
            title_rederived = bool(
                RlsRouteFrame._refresh_terminal_title_after_topology_change(
                    self
                )
            )
        else:
            title_rederived = False
        total_cleared_payloads = cleared_payloads + int(payload_cleared)
        self._committed_project_changed()
        self._refresh_tree(select_id=replacement.shelf_id)
        if reviewed_evidence.get("raman_callouts"):
            _log_route_event(
                "Structured RAMAN slot/port review "
                f"{reviewed_evidence.get('raman_callout_review', 'pending')} "
                f"for shelf {replacement.tid!r}; deployable_cli=false."
            )
        if payload_cleared or cleared_payloads:
            self._refresh_status(
                "Selected shelf updated. A deployment payload was cleared "
                "because provider-relevant identity changed; all affected "
                "route shelves must revalidate their configuration review "
                "before bundle export."
                + (
                    f" Recomputed {endpoint_role_changes} A/Z endpoint role(s)."
                    if endpoint_role_changes
                    else ""
                )
            )
            _log_route_event(
                f"Updated shelf {replacement.tid!r}; provider identity or "
                "structured SRA compatibility changed and "
                f"{total_cleared_payloads} route-bound deployment payload(s) "
                "were cleared; "
                f"endpoint_role_changes={endpoint_role_changes}; "
                f"title_rederived={str(title_rederived).casefold()}.",
                logging.WARNING,
            )
        elif replacement.review_state == "corrected":
            self._refresh_status(
                "Imported shelf corrected and marked reviewed."
            )
            _log_route_event(
                f"Corrected imported shelf {replacement.tid!r} and marked it reviewed."
            )
        elif replacement.review_state == "confirmed":
            self._refresh_status(
                "Imported shelf confirmed and marked reviewed."
            )
            _log_route_event(
                f"Confirmed imported shelf {replacement.tid!r}."
            )
        else:
            self._refresh_status("Selected shelf updated.")
            _log_route_event(f"Updated shelf {replacement.tid!r}.")
        return True

    def _confirm_and_next_pending(self) -> None:
        """Commit one shelf review and advance without bulk-accepting facts."""

        current_index = self._selected_index()
        if current_index is None:
            self._update_selected()
            return
        payload_count_before = sum(
            bool(row.profile_payload) for row in self._rows
        )
        if not self._update_selected():
            return
        payloads_cleared = max(
            0,
            payload_count_before
            - sum(bool(row.profile_payload) for row in self._rows),
        )
        next_id = _next_pending_shelf_id(self._rows, current_index)
        pending_count = sum(
            row.review_state == "pending" for row in self._rows
        )
        if next_id:
            self._refresh_tree(select_id=next_id)
            self._on_tree_select()
            message = (
                f"Shelf review saved; {pending_count} imported shelf"
                f"{'' if pending_count == 1 else 's'} "
                f"{'remains' if pending_count == 1 else 'remain'} pending."
            )
            if payloads_cleared:
                message += (
                    f" Cleared {payloads_cleared} route-bound configuration"
                    f"{'' if payloads_cleared == 1 else 's'}."
                )
            self._refresh_status(message)
            _log_route_event(
                "Guided shelf review advanced to the next pending shelf; "
                f"pending_shelves={pending_count}, "
                f"cleared_payloads={payloads_cleared}."
            )
            return

        message = (
            "All imported shelf facts are reviewed. Continue with exact "
            "configuration and optical-path review."
        )
        if payloads_cleared:
            message += (
                f" Cleared {payloads_cleared} route-bound configuration"
                f"{'' if payloads_cleared == 1 else 's'}."
            )
        next_config_id = RlsRouteFrame._offer_next_exact_config_review(
            self,
            current_index,
            trigger="shelf_fact_review_complete",
            status_prefix=message,
        )
        _log_route_event(
            "Guided shelf fact review completed; pending_shelves=0, "
            f"cleared_payloads={payloads_cleared}; "
            f"next_review_shelf_id={next_config_id or 'none'}. Exact "
            "configuration and optical-path review remain required."
        )

    def _offer_next_exact_config_review(
        self,
        current_index: int,
        *,
        trigger: str,
        status_prefix: str = "",
        preferred_shelf_id: str = "",
    ) -> str:
        """Select and advertise the next review without opening a modal."""

        next_id = ""
        if preferred_shelf_id:
            from utils.rls_config.r4_0_generator import (
                provider_profiles_for_role,
            )

            preferred_row = next(
                (
                    row
                    for row in self._rows
                    if row.shelf_id == preferred_shelf_id
                ),
                None,
            )
            if (
                preferred_row is not None
                and _r40_payload_version_state(
                    preferred_row.profile_payload
                )
                != "current"
            ):
                preferred_providers = provider_profiles_for_role(
                    preferred_row.profile_id
                )
                if _has_accepted_structured_sra(
                    preferred_row.source_evidence
                ):
                    preferred_providers = tuple(
                        provider
                        for provider in preferred_providers
                        if provider.supports_raman
                    )
                if preferred_providers:
                    next_id = preferred_row.shelf_id
        if not next_id:
            next_id = _next_exact_config_review_shelf_id(
                self._rows,
                current_index,
            )
        completed, total = _exact_config_review_progress(self._rows)
        remaining = max(0, total - completed)
        next_row = next(
            (row for row in self._rows if row.shelf_id == next_id),
            None,
        )
        button = getattr(self, "_review_config_button", None)
        if next_row is not None:
            self._refresh_tree(select_id=next_row.shelf_id)
            self._on_tree_select()
            if button is not None:
                button.configure(text=f"Review Next: {next_row.tid}…")
            queue_message = (
                f"Next exact configuration review: {next_row.tid} "
                f"({completed}/{total} complete; {remaining} remaining). "
                "Use Review Next when ready."
            )
        elif remaining:
            if button is not None:
                button.configure(text="Review Configuration…")
            queue_message = (
                f"Exact configuration reviews: {completed}/{total} complete; "
                f"{remaining} remain, but no additional audited-provider "
                "review is currently available. Select a blocked shelf to "
                "inspect or correct its route facts."
            )
        else:
            if button is not None:
                button.configure(text="Review Configuration…")
            queue_message = (
                f"Exact configuration reviews complete ({completed}/{total})."
            )
        self._refresh_status(
            " ".join(
                part.strip()
                for part in (status_prefix, queue_message)
                if part.strip()
            )
        )
        _log_route_event(
            "Guided exact-configuration review offer updated; "
            f"trigger={trigger}, completed={completed}, total={total}, "
            f"remaining={remaining}, "
            f"next_shelf_id={next_id or 'none'}, "
            f"next_tid={next_row.tid if next_row is not None else 'none'}, "
            f"preferred_shelf_id={preferred_shelf_id or 'none'}, "
            "modal_opened=false."
        )
        return next_id

    def _finish_applied_config_review(
        self,
        window: tk.Toplevel,
        current_index: int,
        *,
        provider_id: str,
        reviewed_tid: str,
        pending_peer_ids: Sequence[str] = (),
        validated_peer_ids: Sequence[str] = (),
        restaged_peer_ids: Sequence[str] = (),
    ) -> None:
        """Close an applied review, then offer the next shelf."""

        self._close_config_review(window, reason="applied")
        preferred_peer_id = (
            str(pending_peer_ids[0]) if pending_peer_ids else ""
        )
        if pending_peer_ids:
            status_prefix = (
                "Staged the locally validated SRA endpoint; paired SRA peer "
                "review is pending. Deployable CLI and route-bundle export "
                "remain blocked."
            )
            trigger = "configuration_staged_pending_sra_peer"
            sra_pair_state = "pending_peer"
            log_action = "Staged non-deployable exact R4.0 provider"
        elif validated_peer_ids:
            status_prefix = (
                "Applied and reciprocally validated the paired SRA endpoints "
                "against one route snapshot."
            )
            trigger = "paired_sra_configuration_validated"
            sra_pair_state = "validated_complete"
            log_action = "Applied reciprocally validated exact R4.0 provider"
        else:
            status_prefix = (
                "Applied validated exact R4.0 configuration and "
                "endpoint-path review."
            )
            trigger = "configuration_applied"
            sra_pair_state = "not_applicable"
            log_action = "Applied exact R4.0 provider"
        next_id = RlsRouteFrame._offer_next_exact_config_review(
            self,
            current_index,
            trigger=trigger,
            status_prefix=status_prefix,
            preferred_shelf_id=preferred_peer_id,
        )
        next_row = next(
            (row for row in self._rows if row.shelf_id == next_id),
            None,
        )
        _log_route_event(
            f"{log_action} {provider_id!r} for "
            f"{reviewed_tid!r}; preserved other reviewed shelf payloads; "
            f"review_window_closed=true, "
            f"next_review_shelf_id={next_id or 'none'}, "
            f"next_review_tid="
            f"{next_row.tid if next_row is not None else 'none'}, "
            f"sra_pair_state={sra_pair_state}, "
            "pending_peer_ids="
            f"{','.join(str(item) for item in pending_peer_ids) or 'none'}, "
            "validated_peer_ids="
            f"{','.join(str(item) for item in validated_peer_ids) or 'none'}, "
            "restaged_peer_ids="
            f"{','.join(str(item) for item in restaged_peer_ids) or 'none'}, "
            "next_review_modal_opened=false."
        )

    def _close_config_review(
        self,
        window: tk.Toplevel,
        *,
        reason: str = "operator",
    ) -> None:
        was_open = (
            getattr(self, "_config_review_window", None) is window
        )
        if window.winfo_exists():
            try:
                window.grab_release()
            except tk.TclError:
                pass
            window.destroy()
        if getattr(self, "_config_review_window", None) is window:
            self._config_review_window = None
        if was_open:
            _log_route_event(
                "Closed selected-shelf configuration review; "
                f"reason={reason}, queue_advanced=false."
            )

    def _review_selected_configuration(self) -> None:
        """Open the release-scoped review for the selected route shelf."""

        _log_route_event("Selected-shelf configuration review requested.")
        if not self._require_committed_editor():
            return
        index = self._selected_index()
        if index is None:
            messagebox.showwarning(
                "No shelf selected",
                "Select a shelf in the ordered route before reviewing it.",
                parent=self,
            )
            _log_route_event(
                "Configuration review refused: no shelf selected.",
                logging.WARNING,
            )
            return
        pending_rows = [
            row for row in self._rows if row.review_state == "pending"
        ]
        if pending_rows:
            first_pending = pending_rows[0]
            self._refresh_tree(select_id=first_pending.shelf_id)
            self._on_tree_select()
            messagebox.showwarning(
                "Shelf fact review required",
                (
                    "Review and confirm every imported shelf before opening "
                    "an exact configuration review. "
                    f"{len(pending_rows)} shelf"
                    f"{'' if len(pending_rows) == 1 else 's'} "
                    f"{'remains' if len(pending_rows) == 1 else 'remain'} "
                    "pending. "
                    "Use Confirm & Next Pending to continue."
                ),
                parent=self,
            )
            _log_route_event(
                "Configuration review refused until imported shelf facts are "
                f"reviewed; pending_shelves={len(pending_rows)}.",
                logging.WARNING,
            )
            return
        unresolved_raman_rows = [
            row
            for row in self._rows
            if _raman_callout_review_status(row.source_evidence)
            in {"pending", "invalid"}
        ]
        if unresolved_raman_rows:
            first_unresolved = unresolved_raman_rows[0]
            self._refresh_tree(select_id=first_unresolved.shelf_id)
            self._on_tree_select()
            messagebox.showwarning(
                "RAMAN callout review required",
                (
                    "Every structured RAMAN/SRA callout must be explicitly "
                    "accepted or rejected before exact configuration review. "
                    f"{len(unresolved_raman_rows)} shelf"
                    f"{'' if len(unresolved_raman_rows) == 1 else 's'} "
                    f"{'still requires' if len(unresolved_raman_rows) == 1 else 'still require'} "
                    "a valid callout disposition."
                ),
                parent=self,
            )
            _log_route_event(
                "Configuration review refused until structured RAMAN/SRA "
                "callouts are resolved; "
                f"unresolved_shelves={len(unresolved_raman_rows)}.",
                logging.WARNING,
            )
            return
        from utils.rls_config.route_project import route_native_fiber_review

        fiber_review_status, _fiber_token = route_native_fiber_review(
            self._links
        )
        if fiber_review_status in {"missing", "invalid"}:
            messagebox.showwarning(
                "Route fiber type review required",
                (
                    "Select one exact native CLI fiber type and use Apply to "
                    "all spans before opening configuration review. The "
                    "diagram fiber label remains separate and is never "
                    "automatically converted to a native token."
                ),
                parent=self,
            )
            _log_route_event(
                "Configuration review refused until one route-wide native "
                f"fiber choice is confirmed; fiber_review={fiber_review_status}.",
                logging.WARNING,
            )
            return
        try:
            project = self._build_project(require_valid=False)
            fingerprint = route_project_fingerprint(project)
        except (TypeError, ValueError) as exc:
            messagebox.showwarning(
                "Route not ready for review",
                str(exc),
                parent=self,
            )
            _log_route_event(
                f"Configuration review refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            return
        row = self._rows[index]
        unsupported = _r40_only_rows_error((row,))
        if unsupported:
            messagebox.showwarning(
                "RLS R4.0 configuration review unavailable",
                unsupported,
                parent=self,
            )
            _log_route_event(
                f"Configuration review refused for {row.tid!r}: {unsupported}",
                logging.WARNING,
            )
            return
        self._open_r4_0_review(row, project, fingerprint)

    def _new_review_window(self, title: str) -> tk.Toplevel:
        prior = self._config_review_window
        if prior is not None:
            try:
                self._close_config_review(prior, reason="replaced")
            except tk.TclError:
                self._config_review_window = None
        window = tk.Toplevel(self)
        self._config_review_window = window
        window.title(title)
        window.geometry("1180x780")
        window.minsize(900, 620)
        window.transient(self.winfo_toplevel())
        window.protocol(
            "WM_DELETE_WINDOW",
            lambda current=window: self._close_config_review(current),
        )
        return window

    def _review_snapshot_is_current(
        self,
        fingerprint: str,
        shelf_id: str,
    ) -> int:
        current = self._build_project(require_valid=False)
        if route_project_fingerprint(current) != fingerprint:
            raise ValueError(
                "The route changed while this review was open. Close it and "
                "review the selected shelf again from the current route."
            )
        index = next(
            (
                item_index
                for item_index, item in enumerate(self._rows)
                if item.shelf_id == shelf_id
            ),
            None,
        )
        if index is None:
            raise ValueError("The reviewed shelf is no longer in the route.")
        return index

    def _open_r4_0_review(
        self,
        row: _ShelfEditorRow,
        project: RouteProject,
        fingerprint: str,
    ) -> None:
        if not _release_is_exact(row.software_release, 4, 0):
            messagebox.showwarning(
                "R4.0 configuration review unavailable",
                (
                    "This review requires exact software release 'RLS R4.0'. "
                    "Update and save the selected shelf first."
                ),
                parent=self,
            )
            _log_route_event(
                f"R4.0 review refused for {row.tid!r}: exact release missing.",
                logging.WARNING,
            )
            return

        from gui.rls_r4_0_config_frame import RlsR40ConfigFrame
        from utils.rls_config.r4_0_generator import (
            R40_PAYLOAD_SCHEMA_VERSION,
            decode_r40_exact_payload,
            encode_r40_exact_payload,
            provider_profiles_for_role,
        )

        providers = provider_profiles_for_role(row.profile_id)
        if _has_accepted_structured_sra(row.source_evidence):
            providers = tuple(
                provider
                for provider in providers
                if provider.supports_raman
            )
            if not providers:
                messagebox.showwarning(
                    "SRA-capable R4.0 provider required",
                    (
                        "The selected shelf has reviewed RAMAN/SRA slot-port "
                        "evidence, but no registered role-compatible exact "
                        "R4.0 provider matches that SRA slot and fixed port "
                        "map. ATLAS will not open a no-SRA configuration "
                        "review or infer RAMAN commands."
                    ),
                    parent=self,
                )
                _log_route_event(
                    f"R4.0 exact review refused for {row.tid!r}: reviewed "
                    "structured SRA evidence has no compatible audited "
                    "provider.",
                    logging.WARNING,
                )
                return
        if not providers:
            messagebox.showwarning(
                "R4.0 configuration review unavailable",
                (
                    "No exact audited R4.0 provider is registered for this "
                    "route role. Choose only a role supported by an exact "
                    "hardware/topology provider."
                ),
                parent=self,
            )
            _log_route_event(
                f"R4.0 exact review refused for {row.tid!r}: no compatible "
                f"provider for role {row.profile_id!r}.",
                logging.WARNING,
            )
            return

        request = None
        if row.profile_payload:
            try:
                request = decode_r40_exact_payload(row.profile_payload)
            except (TypeError, ValueError) as exc:
                if (
                    row.profile_payload.get("schema_id")
                    == "ciena.rls.r4-0-exact-request"
                    and row.profile_payload.get("schema_version")
                    != R40_PAYLOAD_SCHEMA_VERSION
                ):
                    messagebox.showwarning(
                        "Stored R4.0 review requires re-review",
                        (
                            "This shelf's exact configuration was saved with "
                            "a retired payload schema. ATLAS corrected the "
                            "bidirectional RLA terminology and DLE ILA "
                            "upstream/downstream neighbor mapping, so the old "
                            "request cannot be regenerated silently.\n\n"
                            "The review will open from current diagram and "
                            "route facts. Validate and apply it to replace the "
                            "old payload with the current audited schema "
                            f"{R40_PAYLOAD_SCHEMA_VERSION}."
                        ),
                        parent=self,
                    )
                    _log_route_event(
                        f"Stored exact R4.0 payload for {row.tid!r} uses "
                        "retired schema "
                        f"{row.profile_payload.get('schema_version')!r}; "
                        f"opened current schema {R40_PAYLOAD_SCHEMA_VERSION} "
                        "for deliberate re-review; old payload retained until "
                        "apply.",
                        logging.WARNING,
                    )
                    request = None
                else:
                    messagebox.showwarning(
                        "Stored R4.0 configuration is invalid",
                        (
                            f"{friendly_error(exc)}\n\n"
                            "ATLAS will not discard or infer replacements for "
                            "an invalid exact-provider payload. Correct the "
                            "saved route project data or remove and "
                            "deliberately review this shelf again."
                        ),
                        parent=self,
                    )
                    _log_route_event(
                        f"R4.0 exact review refused for {row.tid!r}: stored "
                        "payload failed strict decoding: "
                        f"{friendly_error(exc)}",
                        logging.ERROR,
                    )
                    return

        try:
            route_seed = _r4_0_editor_seed(project, row.shelf_id)
        except (TypeError, ValueError) as exc:
            messagebox.showwarning(
                "R4.0 configuration review unavailable",
                friendly_error(exc),
                parent=self,
            )
            _log_route_event(
                f"R4.0 exact review refused for {row.tid!r}: "
                f"{friendly_error(exc)}",
                logging.WARNING,
            )
            return

        raw_provider_resolution = route_seed.get("provider_resolution", {})
        provider_resolution = (
            raw_provider_resolution
            if isinstance(raw_provider_resolution, Mapping)
            else {}
        )
        raw_review_provider_ids = provider_resolution.get(
            "review_provider_ids"
        )
        review_provider_ids = (
            {
                provider_id
                for provider_id in raw_review_provider_ids
                if isinstance(provider_id, str) and provider_id
            }
            if isinstance(raw_review_provider_ids, (list, tuple))
            else {provider.provider_id for provider in providers}
        )
        if request is not None and request.provider_id not in review_provider_ids:
            messagebox.showwarning(
                "Stored R4.0 provider no longer matches route scope",
                (
                    "The stored exact-provider request conflicts with the "
                    "current reviewed diagram/route constraints and will not "
                    "be loaded into this review. ATLAS is preserving the saved "
                    "payload until a compatible review is deliberately "
                    "applied or the route data is corrected.\n\n"
                    "Provider validation and CLI export remain blocked."
                ),
                parent=self,
            )
            _log_route_event(
                f"Stored exact R4.0 payload for {row.tid!r} was not loaded; "
                f"provider_id={request.provider_id!r}, "
                "reason_code=R40_STORED_PROVIDER_ROUTE_SCOPE_MISMATCH.",
                logging.WARNING,
            )
            request = None
        if not review_provider_ids:
            reason_codes = provider_resolution.get("reason_codes", ())
            reason_text = (
                ", ".join(
                    item
                    for item in reason_codes
                    if isinstance(item, str)
                )
                if isinstance(reason_codes, (list, tuple))
                else ""
            )
            messagebox.showwarning(
                "No compatible audited R4.0 provider",
                (
                    "No registered exact provider matches the reviewed shelf "
                    "and route scope. ATLAS will open the planning review so "
                    "you can inspect the diagram-derived identity and optical "
                    "path facts, but provider validation, Apply, and CLI "
                    "generation remain blocked.\n\n"
                    + (
                        "For this terminal, one physical bidirectional route "
                        "degree is not a missing return direction; A→Z and Z→A "
                        "share its mux/demux pair. ATLAS will not substitute "
                        "an exact provider whose audited band, SRA state, "
                        "degree cardinality, or fixed hardware conflicts with "
                        "the imported shelf facts."
                        if row.profile_id != "ila"
                        else (
                            "ATLAS will not substitute a role-compatible "
                            "provider whose audited band or installed hardware "
                            "scope conflicts with the imported ILA facts."
                        )
                    )
                    + (
                        f"\n\nReason codes: {reason_text}"
                        if reason_text
                        else ""
                    )
                ),
                parent=self,
            )
            _log_route_event(
                f"R4.0 planning review for {row.tid!r} has no route-compatible "
                "audited provider; reason_codes="
                f"{reason_text or 'none'}; exact CLI remains blocked.",
                logging.WARNING,
            )

        assumptions_text = ""
        try:
            from utils.rls_config.r4_0_review import build_r4_0_review

            assumptions_text = build_r4_0_review(
                row.profile_id,
                _r4_0_assumption_facts(project, row.shelf_id),
            ).report_text
        except (TypeError, ValueError) as exc:
            # Exact-provider review remains available even when the older
            # role-only narrative cannot resolve an endpoint side.
            _log_route_event(
                f"Role-only R4.0 assumptions summary unavailable for "
                f"{row.tid!r}; exact editor opened without it; "
                f"reason_code={type(exc).__name__}.",
                logging.WARNING,
            )

        window = self._new_review_window(
            f"Review Exact RLS R4.0 Configuration — {row.tid}"
        )

        def apply_reviewed(request_value: Any, _artifact: Any) -> None:
            current_index = self._review_snapshot_is_current(
                fingerprint,
                row.shelf_id,
            )
            payload = encode_r40_exact_payload(request_value)
            current = self._rows[current_index]
            current_rows = list(self._rows)
            current_rows[current_index] = replace(
                current,
                profile_payload=payload,
                review_state=(
                    "corrected"
                    if current.review_state == "corrected"
                    else "confirmed"
                ),
            )
            previous_request = None
            if current.profile_payload:
                try:
                    previous_request = decode_r40_exact_payload(
                        current.profile_payload
                    )
                except (TypeError, ValueError):
                    previous_request = None
            current_rows, restaged_peer_ids = (
                _restage_changed_r40_sra_peers(
                    current_rows,
                    project,
                    row.shelf_id,
                    previous_request,
                    request_value,
                )
            )
            reviewed_links = _apply_r40_reviewed_lines_to_links(
                current_rows,
                self._links,
                row.shelf_id,
                request_value,
            )
            candidate_project = build_route_project(
                route_code=self._route_code_var.get(),
                title=self._title_var.get(),
                revision=self._revision_var.get(),
                rows=current_rows,
                project_id=self._project_id,
                notes=self._project_notes,
                ospf_area=self._ospf_area_var.get(),
                links=reviewed_links,
                diagram_source=self._diagram_source,
                require_valid=False,
            )
            candidate_shelf = next(
                shelf
                for shelf in candidate_project.shelves
                if shelf.shelf_id == row.shelf_id
            )
            current_provider_resolution, _direction_resolution = (
                _r4_0_provider_prepopulation(
                    candidate_project,
                    candidate_shelf,
                )
            )
            current_raw_review_ids = current_provider_resolution.get(
                "review_provider_ids"
            )
            current_review_ids = (
                {
                    provider_id
                    for provider_id in current_raw_review_ids
                    if isinstance(provider_id, str) and provider_id
                }
                if isinstance(current_raw_review_ids, (list, tuple))
                else {
                    provider.provider_id
                    for provider in provider_profiles_for_role(
                        candidate_shelf.profile_id
                    )
                }
            )
            if request_value.provider_id not in current_review_ids:
                reason_codes = current_provider_resolution.get(
                    "reason_codes",
                    (),
                )
                reason_text = (
                    ", ".join(
                        item
                        for item in reason_codes
                        if isinstance(item, str)
                    )
                    if isinstance(reason_codes, (list, tuple))
                    else ""
                )
                _log_route_event(
                    f"Rejected exact R4.0 apply for {row.tid!r}; provider_id="
                    f"{request_value.provider_id!r} is outside current "
                    "project-aware provider options; reason_codes="
                    f"{reason_text or 'none'}.",
                    logging.WARNING,
                )
                raise ValueError(
                    "The selected exact provider is incompatible with the "
                    "current reviewed shelf/route scope. Reopen the review "
                    "after correcting the diagram facts or importing verified "
                    "installed inventory."
                )
            try:
                pair_review = _validate_r40_candidate_pair_review(
                    candidate_project,
                    candidate_shelf.shelf_id,
                )
            except ValueError as exc:
                _log_route_event(
                    f"Rejected exact R4.0 apply for {row.tid!r}; "
                    "reason_code=R40_COMPLETE_CANDIDATE_READINESS_FAILED, "
                    f"error_type={type(exc).__name__}, "
                    "route_mutated=false.",
                    logging.WARNING,
                )
                raise
            pending_peer_ids = pair_review.pending_peer_ids
            validated_peer_ids = pair_review.validated_peer_ids
            self._rows = current_rows
            self._links = reviewed_links
            self._links_populated = True
            self._committed_project_changed()
            window.after_idle(
                lambda current_window=window: (
                    RlsRouteFrame._finish_applied_config_review(
                        self,
                        current_window,
                        current_index,
                        provider_id=request_value.provider_id,
                        reviewed_tid=row.tid,
                        pending_peer_ids=pending_peer_ids,
                        validated_peer_ids=validated_peer_ids,
                        restaged_peer_ids=restaged_peer_ids,
                    )
                )
            )

        editor = RlsR40ConfigFrame(
            window,
            self.controller,
            role_profile=row.profile_id,
            route_seed=route_seed,
            initial_request=request,
            assumptions_text=assumptions_text,
            on_apply=apply_reviewed,
        )
        editor.pack(fill=tk.BOTH, expand=True)

        def request_close() -> None:
            if editor.has_unapplied_changes() and not messagebox.askyesno(
                "Discard R4.0 review edits?",
                "This shelf has unapplied configuration-review changes. "
                "Discard them and close the review?",
                parent=window,
            ):
                _log_route_event(
                    f"Kept exact R4.0 review open for {row.tid!r}; "
                    "discard was cancelled."
                )
                return
            self._close_config_review(window, reason="operator")

        window.protocol("WM_DELETE_WINDOW", request_close)
        window.grab_set()
        window.focus_set()
        prepopulation = route_seed.get("prepopulation", {})
        route_seed_count = 0
        derivation_count = 0
        default_count = 0
        exclusion_count = 0
        manual_count = 0
        prepopulated_keys: tuple[str, ...] = ()
        derivation_keys: tuple[str, ...] = ()
        default_keys: tuple[str, ...] = ()
        exclusion_keys: tuple[str, ...] = ()
        manual_keys: tuple[str, ...] = ()
        if isinstance(prepopulation, Mapping):
            for key, target in (
                ("route_reviewed_fields", "route"),
                ("controlled_derivations", "derivation"),
                ("controlled_defaults", "default"),
                ("policy_exclusions", "exclusion"),
                ("manual_fields", "manual"),
            ):
                raw_items = prepopulation.get(key, ())
                count = (
                    len(raw_items)
                    if isinstance(raw_items, (list, tuple))
                    else 0
                )
                if target == "route":
                    route_seed_count = count
                    prepopulated_keys = (
                        tuple(
                            item
                            for item in raw_items
                            if isinstance(item, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,96}",
                                item,
                            )
                        )
                        if isinstance(raw_items, (list, tuple))
                        else ()
                    )
                elif target == "derivation":
                    derivation_count = count
                    derivation_keys = (
                        tuple(
                            item
                            for item in raw_items
                            if isinstance(item, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,96}",
                                item,
                            )
                        )
                        if isinstance(raw_items, (list, tuple))
                        else ()
                    )
                elif target == "default":
                    default_count = count
                    default_keys = (
                        tuple(
                            item
                            for item in raw_items
                            if isinstance(item, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,96}",
                                item,
                            )
                        )
                        if isinstance(raw_items, (list, tuple))
                        else ()
                    )
                elif target == "exclusion":
                    exclusion_count = count
                    exclusion_keys = (
                        tuple(
                            item
                            for item in raw_items
                            if isinstance(item, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,96}",
                                item,
                            )
                        )
                        if isinstance(raw_items, (list, tuple))
                        else ()
                    )
                else:
                    manual_count = count
                    manual_keys = (
                        tuple(
                            item
                            for item in raw_items
                            if isinstance(item, str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]{1,96}",
                                item,
                            )
                        )
                        if isinstance(raw_items, (list, tuple))
                        else ()
                    )
        provider_preselected = (
            isinstance(provider_resolution, Mapping)
            and provider_resolution.get("preselect_allowed") is True
        )
        resolved_provider_id = str(
            provider_resolution.get("provider_id", "") or ""
        ).strip()
        resolution_status = str(
            provider_resolution.get("status", "") or ""
        ).strip()
        sole_route_candidate_populated = bool(
            resolution_status in {"exact_match", "unique_candidate"}
            and resolved_provider_id
            and review_provider_ids == {resolved_provider_id}
        )
        if request is not None:
            provider_selection_state = "loaded from stored payload"
        elif provider_preselected:
            provider_selection_state = "advisory diagram candidate preselected"
        elif sole_route_candidate_populated:
            provider_selection_state = (
                "sole route-compatible candidate populated"
            )
        else:
            provider_selection_state = "required"
        raw_direction_resolution = route_seed.get("direction_resolution", {})
        direction_resolution = (
            raw_direction_resolution
            if isinstance(raw_direction_resolution, Mapping)
            else {}
        )
        direction_source = {
            "exact_match": "direct_port",
            "controlled_fallback": "audited_provider_role_fallback",
        }.get(
            str(direction_resolution.get("status", "") or ""),
            "operator_required",
        )

        _log_route_event(
            f"Opened integrated exact R4.0 configuration review for "
            f"{row.tid!r}; provider selection "
            f"{provider_selection_state}; "
            "provider_resolution_status="
            f"{str(provider_resolution.get('status', '') or '') or 'unknown'}; "
            "provider_resolution_reason_codes="
            f"{','.join(str(item) for item in provider_resolution.get('reason_codes', ()) if isinstance(item, str)) or 'none'}; "
            "review_provider_ids="
            f"{','.join(sorted(review_provider_ids)) or 'none'}; "
            "provider_band_scope="
            f"{str(provider_resolution.get('band_scope', '') or '') or 'none'}; "
            "provider_band_scope_source="
            f"{str(provider_resolution.get('band_scope_source', '') or '') or 'none'}; "
            "represented_route_degrees="
            f"{provider_resolution.get('represented_route_degree_count', 'unknown')}; "
            "candidate_provider_degrees="
            f"{provider_resolution.get('candidate_provider_degree_count', 'unknown')}; "
            "fixed_direction="
            f"{str(route_seed.get('line_1_route_side', '') or '') or 'operator_required'}; "
            f"fixed_direction_source={direction_source}; "
            f"prepopulated_route_fields={route_seed_count}, "
            f"controlled_derivations={derivation_count}, "
            f"controlled_defaults={default_count}, "
            f"policy_exclusions={exclusion_count}, "
            f"manual_review_groups={manual_count}, "
            "prepopulated_field_keys="
            f"{','.join(prepopulated_keys) or 'none'}, "
            f"controlled_derivation_keys={','.join(derivation_keys) or 'none'}, "
            f"controlled_default_keys={','.join(default_keys) or 'none'}, "
            f"policy_exclusion_keys={','.join(exclusion_keys) or 'none'}, "
            f"manual_review_keys={','.join(manual_keys) or 'none'}, "
            f"assumptions_summary={'available' if assumptions_text else 'unavailable'}."
        )

    def _remove_selected(self) -> None:
        if not self._require_committed_editor():
            return
        index = self._selected_index()
        if index is None:
            _log_route_event(
                "Remove shelf refused: no shelf selected.", logging.WARNING
            )
            messagebox.showwarning(
                "No shelf selected",
                "Select a shelf in the route table before removing it.",
                parent=self,
            )
            return
        row = self._rows[index]
        if not messagebox.askyesno(
            "Remove shelf",
            f"Remove {row.tid} from this route project?",
            parent=self,
        ):
            _log_route_event(
                f"Remove shelf cancelled for {row.tid!r}."
            )
            return
        del self._rows[index]
        self._rows, cleared_payloads = _clear_provider_payloads(self._rows)
        self._rows, invalidated_directions = (
            _invalidate_route_direction_evidence(
                self._rows,
                reason="shelf removed from ordered route",
            )
        )
        self._rows, endpoint_role_changes = (
            _reconcile_route_endpoint_profiles(self._rows)
        )
        self._links = _reconcile_route_links(
            self._rows,
            getattr(self, "_links", ()),
            populate_missing=bool(
                getattr(self, "_links_populated", False)
            ),
        )
        title_refresh = getattr(
            self,
            "_refresh_terminal_title_after_topology_change",
            None,
        )
        if callable(title_refresh):
            title_rederived = bool(title_refresh())
        elif hasattr(self, "_diagram_source") and hasattr(self, "_title_var"):
            title_rederived = bool(
                RlsRouteFrame._refresh_terminal_title_after_topology_change(
                    self
                )
            )
        else:
            title_rederived = False
        self._committed_project_changed()
        self._sync_route_fiber_controls()
        self._clear_editor()
        self._refresh_tree()
        self._refresh_status(
            "Shelf removed. Route order updated."
            + (
                f" Cleared {cleared_payloads} route-bound configuration(s)."
                if cleared_payloads
                else ""
            )
            + (
                f" Recomputed {endpoint_role_changes} A/Z endpoint role(s)."
                if endpoint_role_changes
                else ""
            )
            + (
                f" Invalidated {invalidated_directions} source direction "
                "suggestion(s)."
                if invalidated_directions
                else ""
            )
        )
        _log_route_event(
            f"Removed shelf {row.tid!r}; route now has {len(self._rows)} "
            f"shelf(s); cleared_payloads={cleared_payloads}; "
            f"endpoint_role_changes={endpoint_role_changes}; "
            f"invalidated_direction_suggestions={invalidated_directions}; "
            f"title_rederived={str(title_rederived).casefold()}."
        )

    def _move_selected(self, offset: int) -> None:
        if not self._require_committed_editor():
            return
        index = self._selected_index()
        if index is None:
            _log_route_event(
                "Reorder refused: no shelf selected.", logging.WARNING
            )
            messagebox.showwarning(
                "No shelf selected",
                "Select a shelf before changing route order.",
                parent=self,
            )
            return
        destination = index + offset
        if destination < 0 or destination >= len(self._rows):
            _log_route_event(
                "Reorder ignored: selected shelf is already at the route boundary.",
                logging.DEBUG,
            )
            return
        row = self._rows.pop(index)
        self._rows.insert(destination, row)
        self._rows, cleared_payloads = _clear_provider_payloads(self._rows)
        self._rows, invalidated_directions = (
            _invalidate_route_direction_evidence(
                self._rows,
                reason="shelf order changed",
            )
        )
        self._rows, endpoint_role_changes = (
            _reconcile_route_endpoint_profiles(self._rows)
        )
        self._links = _reconcile_route_links(
            self._rows,
            getattr(self, "_links", ()),
            populate_missing=bool(
                getattr(self, "_links_populated", False)
            ),
        )
        title_refresh = getattr(
            self,
            "_refresh_terminal_title_after_topology_change",
            None,
        )
        if callable(title_refresh):
            title_rederived = bool(title_refresh())
        elif hasattr(self, "_diagram_source") and hasattr(self, "_title_var"):
            title_rederived = bool(
                RlsRouteFrame._refresh_terminal_title_after_topology_change(
                    self
                )
            )
        else:
            title_rederived = False
        self._committed_project_changed()
        self._sync_route_fiber_controls()
        self._refresh_tree(select_id=row.shelf_id)
        self._refresh_status(
            "Route order updated."
            + (
                f" Cleared {cleared_payloads} route-bound configuration(s); "
                "review affected shelves again."
                if cleared_payloads
                else ""
            )
            + (
                f" Recomputed {endpoint_role_changes} A/Z endpoint role(s)."
                if endpoint_role_changes
                else ""
            )
            + (
                f" Invalidated {invalidated_directions} source direction "
                "suggestion(s)."
                if invalidated_directions
                else ""
            )
        )
        _log_route_event(
            f"Moved shelf {row.tid!r} from position {index + 1} "
            f"to {destination + 1}; cleared_payloads={cleared_payloads}; "
            f"endpoint_role_changes={endpoint_role_changes}; "
            f"invalidated_direction_suggestions={invalidated_directions}; "
            f"title_rederived={str(title_rederived).casefold()}."
        )

    def _on_tree_select(self, _event: object = None) -> None:
        if getattr(self, "_restoring_tree_selection", False):
            return
        index = self._selected_index()
        if index is None:
            return
        row = self._rows[index]
        loaded_shelf_id = getattr(self, "_editor_shelf_id", "")
        if self._editor_dirty:
            if row.shelf_id == loaded_shelf_id:
                return
            self._restoring_tree_selection = True
            try:
                if loaded_shelf_id and self._tree.exists(loaded_shelf_id):
                    self._tree.selection_set(loaded_shelf_id)
                    self._tree.focus(loaded_shelf_id)
                    self._tree.see(loaded_shelf_id)
                else:
                    for item in self._tree.selection():
                        self._tree.selection_remove(item)
            finally:
                self._restoring_tree_selection = False
            action = (
                "Update Selected or Confirm & Next Pending"
                if loaded_shelf_id
                else "Add Shelf"
            )
            _log_route_event(
                "Shelf selection change refused because the editor has "
                f"uncommitted changes; operator must use {action}.",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Shelf edits not applied",
                (
                    f"Use {action} or Clear Editor before selecting another "
                    "shelf. The visible editor values were preserved."
                ),
                parent=self,
            )
            return
        self._load_editor_row(row)
        button = getattr(self, "_review_config_button", None)
        if button is not None:
            button.configure(text=f"Review Configuration: {row.tid}…")

    def _load_editor_row(self, row: _ShelfEditorRow) -> None:
        """Load one committed row without triggering editor-dirty traces."""

        self._loading_editor = True
        try:
            self._profile_var.set(
                self._profile_label_by_id.get(
                    row.profile_id, profile_display_name(row.profile_id)
                )
            )
            self._site_code_var.set(row.site_code)
            self._site_name_var.set(row.site_name)
            self._tid_var.set(row.tid)
            self._ip_var.set(row.primary_oam_ip)
            self._release_var.set(R40_UI_RELEASE)
            self._variant_var.set(row.shelf_variant)
            self._raman_var.set(row.raman_label)
            self._power_var.set(row.power_label)
            self._editor_power_is_atlas_default = (
                _has_exact_power_label_role_default(
                    row.source_evidence,
                    row.profile_id,
                    row.power_label,
                )
            )
        finally:
            self._loading_editor = False
        self._editor_shelf_id = row.shelf_id
        self._editor_dirty = False

    def _clear_editor(self) -> None:
        self._loading_editor = True
        try:
            for variable in (
                self._site_code_var,
                self._site_name_var,
                self._tid_var,
                self._ip_var,
                self._variant_var,
                self._raman_var,
                self._power_var,
            ):
                variable.set("")
            self._release_var.set(R40_UI_RELEASE)
            if self._profile_pairs:
                self._profile_var.set(self._profile_pairs[0][1])
            selected_profile = self._selected_profile_id()
            default_power = power_label_for_profile(selected_profile)
            self._power_var.set(default_power)
            self._editor_power_is_atlas_default = bool(default_power)
        finally:
            self._loading_editor = False
        self._editor_shelf_id = ""
        self._editor_dirty = False
        button = getattr(self, "_review_config_button", None)
        if button is not None:
            button.configure(text="Review Configuration…")
        for item in self._tree.selection():
            self._tree.selection_remove(item)

    def _refresh_tree(self, *, select_id: str = "") -> None:
        current = select_id
        if not current:
            selection = self._tree.selection()
            current = selection[0] if selection else ""
        project: RouteProject | None = None
        project_shelves: dict[str, ShelfInstance] = {}
        try:
            project = self._build_project(require_valid=False)
        except (TypeError, ValueError):
            pass
        else:
            project_shelves = {
                shelf.shelf_id: shelf for shelf in project.shelves
            }
        self._tree.delete(*self._tree.get_children())
        for index, row in enumerate(self._rows, start=1):
            project_shelf = project_shelves.get(row.shelf_id)
            provider_label, direction_label = (
                _r40_shelf_glance_labels(project, project_shelf)
                if project is not None and project_shelf is not None
                else (
                    "Provider pending route data",
                    "Direction pending route data",
                )
            )
            self._tree.insert(
                "",
                tk.END,
                iid=row.shelf_id,
                values=(
                    index,
                    profile_display_name(row.profile_id),
                    row.site_code,
                    row.tid,
                    row.primary_oam_ip,
                    row.software_release,
                    row.raman_label,
                    row.power_label,
                    provider_label,
                    direction_label,
                    profile_readiness_label(
                        row.profile_id,
                        review_state=row.review_state,
                        advisory_label=self._config_labels.get(row.shelf_id, ""),
                        profile_payload=row.profile_payload,
                    ),
                ),
            )
        if current and self._tree.exists(current):
            self._tree.selection_set(current)
            self._tree.focus(current)
            self._tree.see(current)

    def _update_tree_readiness_cells(self) -> None:
        """Patch only readiness cells so the selected editor is untouched."""

        for row in self._rows:
            if not self._tree.exists(row.shelf_id):
                continue
            values = list(self._tree.item(row.shelf_id, "values"))
            if len(values) != len(_TABLE_COLUMNS):
                continue
            values[_TABLE_COLUMNS.index("readiness")] = profile_readiness_label(
                row.profile_id,
                review_state=row.review_state,
                advisory_label=self._config_labels.get(row.shelf_id, ""),
                profile_payload=row.profile_payload,
            )
            self._tree.item(row.shelf_id, values=values)

    def _build_project(self, *, require_valid: bool = True) -> RouteProject:
        return build_route_project(
            route_code=self._route_code_var.get(),
            title=self._title_var.get(),
            revision=self._revision_var.get(),
            rows=self._rows,
            project_id=self._project_id,
            notes=self._project_notes,
            ospf_area=self._ospf_area_var.get(),
            links=self._links,
            diagram_source=self._diagram_source,
            require_valid=require_valid,
        )

    def _require_committed_editor(self) -> bool:
        if not self._editor_dirty:
            return True
        action = (
            "Update Selected or Confirm & Next Pending"
            if getattr(self, "_editor_shelf_id", "")
            else "Add Shelf"
        )
        _log_route_event(
            f"Action refused: shelf editor has uncommitted changes; "
            f"operator must use {action}.",
            logging.WARNING,
        )
        messagebox.showwarning(
            "Shelf edits not applied",
            (
                f"Use {action} or Clear Editor before saving, previewing, "
                "or exporting. This prevents visible shelf edits from being "
                "silently omitted."
            ),
            parent=self,
        )
        return False

    def _new_project(self) -> None:
        if self._dirty and not messagebox.askyesno(
            "Start a new project",
            "Discard the current unsaved route edits?",
            parent=self,
        ):
            _log_route_event("New project cancelled; unsaved route retained.")
            return
        self._rows.clear()
        self._links = []
        self._links_populated = False
        self._project_id = uuid4().hex
        self._project_notes = ""
        self._diagram_source = {}
        self._attached_workbook_diagram = None
        self._current_path = None
        self._loading_project = True
        try:
            self._route_code_var.set("")
            self._title_var.set("")
            self._revision_var.set("1")
            self._ospf_area_var.set("")
        finally:
            self._loading_project = False
        self._dirty = False
        self._sync_route_fiber_controls()
        self._clear_editor()
        self._invalidate_project_results()
        self._refresh_tree()
        self._refresh_status("New route project.")
        _log_route_event("Created a new empty route project.")

    def _save_project(self) -> None:
        _log_route_event("Save project requested.")
        if not self._require_committed_editor():
            return
        try:
            project = self._build_project(require_valid=False)
        except (TypeError, ValueError) as exc:
            _log_route_event(
                f"Save project refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning("Route not ready", str(exc), parent=self)
            return
        initial = (
            self._current_path.name
            if self._current_path is not None
            else f"{_filename_stem(project.route_code)}_route_project.json"
        )
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="Save Ciena RLS route project",
            initialdir=str(get_desktop_dir()),
            initialfile=initial,
            defaultextension=".json",
            filetypes=(("Route project", "*.json"), ("All files", "*.*")),
        )
        if not destination:
            _log_route_event("Save project cancelled by operator.")
            return
        try:
            output = save_route_project_draft(project, Path(destination))
        except (OSError, TypeError, ValueError) as exc:
            LOGGER.exception("Could not save Ciena RLS route project")
            messagebox.showerror(
                "Project save failed",
                friendly_error(exc, "The route project could not be saved."),
                parent=self,
            )
            return
        self._project_id = project.project_id
        self._project_notes = project.notes
        self._current_path = Path(output) if output is not None else Path(destination)
        self._dirty = False
        self._refresh_status(f"Saved project: {self._current_path.name}")
        _log_route_event(
            f"Saved project {project.project_id} with {len(project.shelves)} "
            f"shelf(s) to {self._current_path}."
        )

    def _open_project(self) -> None:
        _log_route_event("Open project requested.")
        if self._dirty and not messagebox.askyesno(
            "Open another project",
            "Discard the current unsaved route edits?",
            parent=self,
        ):
            _log_route_event("Open project cancelled; unsaved route retained.")
            return
        source = filedialog.askopenfilename(
            parent=self,
            title="Open Ciena RLS route project",
            initialdir=str(get_desktop_dir()),
            filetypes=(("Route project", "*.json"), ("All files", "*.*")),
        )
        if not source:
            _log_route_event("Open project cancelled by operator.")
            return
        try:
            project = load_route_project_draft(Path(source))
            self._load_project(project)
        except (OSError, TypeError, ValueError) as exc:
            LOGGER.exception("Could not open Ciena RLS route project")
            messagebox.showerror(
                "Project open failed",
                friendly_error(exc, "The selected route project is not valid."),
                parent=self,
            )
            return
        self._current_path = Path(source)
        self._dirty = False
        diagram_reattachment_required = isinstance(
            self._diagram_source.get(WORKBOOK_DIAGRAM_MARKER_KEY),
            Mapping,
        )
        self._refresh_status(
            f"Opened project: {self._current_path.name}"
            + (
                ". Reattach the original diagram before preview/export."
                if diagram_reattachment_required
                else ""
            )
        )
        _log_route_event(
            f"Opened project {project.project_id} with {len(project.shelves)} "
            f"shelf(s) from {self._current_path}; "
            "diagram_reattachment_required="
            f"{str(diagram_reattachment_required).casefold()}."
        )

    def _load_project(self, project: RouteProject) -> None:
        unsupported = _r40_only_rows_error(project.shelves)
        if unsupported:
            raise ValueError(
                unsupported
                + " The current route was not replaced. Use an RLS R4.0 "
                "project or convert it through an explicitly reviewed process."
            )
        sites = {site.site_key: site for site in project.sites}
        rows: list[_ShelfEditorRow] = []
        for shelf in project.shelves:
            site = sites.get(
                shelf.site_key,
                Site(
                    site_key=shelf.site_key,
                    code="",
                    name="",
                ),
            )
            rows.append(
                _ShelfEditorRow(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    site_key=shelf.site_key,
                    site_code=site.code,
                    site_name=site.name,
                    tid=shelf.tid,
                    primary_oam_ip=shelf.primary_oam_ip,
                    software_release=shelf.software_release,
                    shelf_variant=shelf.shelf_variant,
                    raman_label=shelf.raman_label,
                    power_label=shelf.power_label,
                    site_address=site.address,
                    network_site_id=site.network_site_id,
                    notes=shelf.notes,
                    profile_payload=dict(shelf.profile_payload),
                    review_state=shelf.review_state,
                    source_evidence=dict(shelf.source_evidence),
                )
            )
        self._project_id = project.project_id
        self._project_notes = project.notes
        self._diagram_source = dict(project.diagram_source)
        # Pixel content is deliberately session-only and never serialized
        # into the route-project JSON. A project that requires a Diagram-tab
        # image must be rebound to the same local source before rendering.
        self._attached_workbook_diagram = None
        self._rows = rows
        self._links = list(project.links)
        self._links_populated = bool(project.links)
        self._loading_project = True
        try:
            self._route_code_var.set(project.route_code)
            self._title_var.set(project.title)
            self._revision_var.set(project.revision)
            self._ospf_area_var.set(project.ospf_area)
        finally:
            self._loading_project = False
        self._sync_route_fiber_controls()
        self._clear_editor()
        self._invalidate_project_results()
        self._refresh_tree()
        self._schedule_config_evaluation()

    def _upload_route_diagram(self) -> None:
        _log_route_event("Route diagram upload requested.")
        if import_route_diagram is None:
            detail = (
                friendly_error(_DIAGRAM_IMPORT_ERROR)
                if _DIAGRAM_IMPORT_ERROR is not None
                else "Route diagram importer unavailable."
            )
            messagebox.showerror(
                "Route diagram importer unavailable",
                detail,
                parent=self,
            )
            _log_route_event(
                f"Diagram upload unavailable: {detail}", logging.ERROR
            )
            return
        if not self._require_committed_editor():
            _log_route_event(
                "Diagram upload stopped before file selection because shelf "
                "editor changes are uncommitted.",
                logging.WARNING,
            )
            return
        source = filedialog.askopenfilename(
            parent=self,
            title="Upload customer Ciena RLS route diagram",
            initialdir=str(get_desktop_dir()),
            filetypes=(
                ("Supported route diagrams", "*.docx *.png *.jpg *.jpeg"),
                ("Word document", "*.docx"),
                ("Image", "*.png *.jpg *.jpeg"),
                ("All files", "*.*"),
            ),
        )
        if not source:
            _log_route_event("Route diagram selection cancelled.")
            return
        _log_route_event(f"Selected route diagram {Path(source).name!r}.")
        if self._rows and not messagebox.askyesno(
            "Replace current route shelves",
            (
                "A diagram import represents a complete route and will replace "
                "the current shelf list after successful transcription. Continue?"
            ),
            parent=self,
        ):
            _log_route_event(
                "Diagram import cancelled; operator retained current shelf list."
            )
            return
        raman_callout_enabled = messagebox.askyesno(
            "RAMAN slot/port convention",
            (
                "For this diagram only, should ATLAS treat small red N/5 and "
                "N/6 boxes as RAMAN slot/port annotations?\n\n"
                "Choose Yes only when the customer/source convention is "
                "confirmed. A detached 3/5–3/6 legend sample and large red "
                "equipment boxes will not be assigned to a shelf. This choice "
                "does not authorize SRA hardware or CLI."
            ),
            default="no",
            parent=self,
        )
        raman_convention_id = (
            RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
            if raman_callout_enabled
            else RAMAN_CALLOUT_CONVENTION_DISABLED
        )
        if DiagramImportConventions is None:
            raise DiagramImportError("Route diagram importer is unavailable.")
        conventions = DiagramImportConventions(
            raman_callout_convention=raman_convention_id
        )
        _log_route_event(
            "Source-scoped RAMAN slot/port convention "
            f"{'enabled' if raman_callout_enabled else 'disabled'} for "
            f"{Path(source).name!r}."
        )
        if not messagebox.askyesno(
            "External AI privacy confirmation",
            (
                f"{DIAGRAM_PRIVACY_NOTICE}\n\n"
                f"Selected file: {Path(source).name}\n\nContinue?"
            ),
            parent=self,
        ):
            _log_route_event(
                "Diagram import cancelled at external-AI privacy confirmation."
            )
            return
        source_path = Path(source)
        _log_route_event(
            f"External-AI privacy confirmation accepted for "
            f"{source_path.name!r}; transcription starting."
        )
        provider_factory = self._diagram_provider_factory
        self._refresh_status(
            f"Transcribing {source_path.name} in the background…"
        )
        self._submit_background(
            "diagram_import",
            lambda: _import_diagram_worker(
                source_path,
                provider_factory,
                conventions,
            ),
            foreground=True,
        )

    def _reattach_route_diagram(self) -> None:
        """Rebind saved hash-only provenance to local pixels without AI."""

        _log_route_event("Local route diagram reattachment requested.")
        if (
            load_diagram_source is None
            or workbook_diagram_from_source is None
            or validate_workbook_diagram_for_project is None
        ):
            detail = (
                friendly_error(_DIAGRAM_ASSET_IMPORT_ERROR)
                if _DIAGRAM_ASSET_IMPORT_ERROR is not None
                else "Diagram attachment support is unavailable."
            )
            messagebox.showerror(
                "Diagram attachment unavailable",
                detail,
                parent=self,
            )
            _log_route_event(
                f"Diagram reattachment unavailable: {detail}",
                logging.ERROR,
            )
            return
        if not self._require_committed_editor():
            return
        marker = self._diagram_source.get(WORKBOOK_DIAGRAM_MARKER_KEY)
        if not isinstance(marker, Mapping):
            messagebox.showwarning(
                "No uploaded diagram provenance",
                (
                    "This route project has no controlled Diagram-tab source "
                    "marker. Use Upload Route Diagram to transcribe and bind a "
                    "customer diagram to the route."
                ),
                parent=self,
            )
            _log_route_event(
                "Diagram reattachment refused: project has no workbook "
                "diagram provenance marker.",
                logging.WARNING,
            )
            return
        source = filedialog.askopenfilename(
            parent=self,
            title="Reattach the original customer route diagram",
            initialdir=str(get_desktop_dir()),
            filetypes=(
                ("Supported route diagrams", "*.docx *.png *.jpg *.jpeg"),
                ("Word document", "*.docx"),
                ("Image", "*.png *.jpg *.jpeg"),
                ("All files", "*.*"),
            ),
        )
        if not source:
            _log_route_event("Diagram reattachment cancelled by operator.")
            return
        try:
            normalized_source = load_diagram_source(Path(source))
            diagram = workbook_diagram_from_source(normalized_source)
            project = self._build_project(require_valid=False)
            validate_workbook_diagram_for_project(project, diagram)
        except (DiagramAssetError, DiagramImportError, OSError, TypeError, ValueError) as exc:
            _log_route_event(
                "Diagram reattachment refused because local content did not "
                f"match saved provenance: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Diagram does not match this project",
                friendly_error(
                    exc,
                    "The selected diagram does not match the uploaded source "
                    "record saved with this route.",
                ),
                parent=self,
            )
            return
        self._attached_workbook_diagram = diagram
        self._refresh_status(
            f"Reattached Diagram-tab source: {diagram.source_file_name}"
        )
        _log_route_event(
            f"Reattached local Diagram-tab source "
            f"{diagram.source_file_name!r}; sha256={diagram.source_sha256}; "
            f"image_occurrences={len(diagram.images)}; "
            "external_ai_processing=false."
        )

    def _apply_diagram_import(self, result: DiagramImportResult) -> None:
        try:
            integrity_blockers = diagram_import_mutation_blockers(result)
            if integrity_blockers:
                codes = sorted({blocker.code for blocker in integrity_blockers})
                fields = sorted({blocker.field for blocker in integrity_blockers})
                summary = "\n".join(
                    (
                        f"- [{blocker.code}] {blocker.field}: "
                        f"{blocker.message}"
                    )
                    for blocker in integrity_blockers[:8]
                )
                remaining = len(integrity_blockers) - min(
                    len(integrity_blockers), 8
                )
                if remaining:
                    summary += f"\n- …and {remaining} more integrity failure(s)."
                detail_log = " | ".join(
                    f"{blocker.field}: {blocker.message}"
                    for blocker in integrity_blockers[:8]
                )
                if remaining:
                    detail_log += f" | ...and {remaining} more"
                importer_issues = tuple(getattr(result, "issues", ()) or ())
                importer_issue_codes = sorted(
                    {
                        str(getattr(issue, "code", "UNKNOWN"))
                        for issue in importer_issues
                    }
                )
                importer_blocker_count = sum(
                    bool(getattr(issue, "blocking", False))
                    for issue in importer_issues
                )
                _log_route_event(
                    "Diagram transcription rejected before route replacement; "
                    f"shelves={len(result.active_shelves)}, "
                    f"spans={len(result.active_spans)}, "
                    f"integrity_codes={','.join(codes)}, "
                    f"integrity_fields={','.join(fields)}, "
                    f"integrity_details={detail_log}, "
                    f"importer_blockers={importer_blocker_count}, "
                    "importer_issue_codes="
                    f"{','.join(importer_issue_codes) or 'none'}. "
                    "Current route unchanged.",
                    logging.WARNING,
                )
                self._refresh_status(
                    "Diagram transcription incomplete; current route unchanged."
                )
                messagebox.showwarning(
                    "Diagram transcription incomplete — route unchanged",
                    (
                        "ATLAS could not establish a complete, contiguous route "
                        "from this transcription. The existing route was not "
                        "replaced.\n\n"
                        f"{summary}\n\n"
                        "Configuration-readiness fields such as software "
                        "release are evaluated separately and did not cause "
                        "this rejection."
                    ),
                    parent=self,
                )
                return
            rows = _diagram_editor_rows(result)
            unsupported = _r40_only_rows_error(rows)
            if unsupported:
                _log_route_event(
                    "Diagram transcription rejected by the RLS R4.0 product "
                    f"boundary before route replacement: {unsupported}",
                    logging.WARNING,
                )
                self._refresh_status(
                    "Diagram contains unsupported release or shelf roles; "
                    "current route unchanged."
                )
                messagebox.showwarning(
                    "RLS R4.0 route required — current route unchanged",
                    (
                        f"{unsupported}\n\n"
                        "ATLAS did not replace the current route. Correct the "
                        "customer source or use an explicitly reviewed RLS "
                        "R4.0-only diagram."
                    ),
                    parent=self,
                )
                return
            links = _diagram_route_links(result, rows)
            diagram_source = _diagram_source_record(result)
            workbook_diagram: WorkbookDiagram | None = None
            source_images = tuple(
                getattr(getattr(result, "source", None), "images", ()) or ()
            )
            if source_images:
                if workbook_diagram_from_source is None:
                    raise DiagramAssetError(
                        "Diagram attachment support is unavailable."
                    )
                workbook_diagram = workbook_diagram_from_source(result.source)
                diagram_source[WORKBOOK_DIAGRAM_MARKER_KEY] = (
                    workbook_diagram.marker_dict(required_in_mop=True)
                )
            elif str(
                getattr(getattr(result, "source", None), "path", "") or ""
            ).strip():
                # A production import always retains normalized source images.
                # Only older headless fixtures omit them.
                raise DiagramAssetError(
                    "The imported diagram has no renderable source image."
                )
            revision_scope_defaulted = not str(result.revision or "").strip()
            if revision_scope_defaulted:
                diagram_source["route_revision_scope_default"] = {
                    "value": "1",
                    "reason": _ROUTE_REVISION_SCOPE_DEFAULT_REASON,
                }
        except (DiagramAssetError, DiagramImportError, TypeError, ValueError) as exc:
            self._handle_worker_error("diagram_import", exc)
            return
        if not rows:
            _log_route_event(
                "Diagram transcription produced no active shelves; route unchanged.",
                logging.WARNING,
            )
            messagebox.showwarning(
                "No active shelves found",
                (
                    "The diagram transcription did not contain an active shelf. "
                    "The current route was not changed."
                ),
                parent=self,
            )
            return

        self._rows = rows
        self._links = links
        self._links_populated = True
        self._project_id = uuid4().hex
        self._project_notes = ""
        self._diagram_source = diagram_source
        self._attached_workbook_diagram = workbook_diagram
        self._current_path = None
        self._loading_project = True
        try:
            self._route_code_var.set(result.route_code or "")
            self._title_var.set(result.title or "")
            self._revision_var.set(result.revision or "1")
            ospf_variable = getattr(self, "_ospf_area_var", None)
            if ospf_variable is not None:
                ospf_variable.set(result.ospf_area or "")
        finally:
            self._loading_project = False
        self._clear_editor()
        self._committed_project_changed()
        self._sync_route_fiber_controls()
        self._refresh_tree(select_id=rows[0].shelf_id)

        issue_accounting = account_diagram_review_issues(
            result,
            rows,
            revision_default=diagram_source.get(
                "route_revision_scope_default"
            ),
            links=links,
        )
        source_issue_aggregate = issue_accounting.raw
        issue_aggregate = issue_accounting.unresolved
        required_review_count = issue_aggregate.required_review_count
        blocker_summary = diagram_issue_summary(
            issue_accounting.unresolved_issues
        )
        unresolved_role_orders = [
            str(order)
            for order, row in enumerate(rows, start=1)
            if not row.profile_id.strip()
        ]
        unresolved_role_count = len(unresolved_role_orders)
        suggested_site_code_count = sum(
            "site_code_review_suggestion" in row.source_evidence for row in rows
        )
        suggested_shelf_variant_count = sum(
            "shelf_variant_chassis_suggestion" in row.source_evidence
            for row in rows
        )
        raman_callouts = tuple(
            getattr(result, "raman_callouts", ()) or ()
        )
        raman_endpoint_callout_count = sum(
            getattr(callout, "context", "") == "shelf_endpoint"
            and bool(getattr(callout, "shelf_tid", None))
            for callout in raman_callouts
        )
        raman_legend_callout_count = sum(
            getattr(callout, "context", "") == "legend_sample"
            for callout in raman_callouts
        )
        raman_unresolved_callout_count = sum(
            getattr(callout, "context", "") == "unknown"
            or (
                getattr(callout, "context", "") == "shelf_endpoint"
                and not getattr(callout, "shelf_tid", None)
            )
            for callout in raman_callouts
        )
        raman_suggested_shelf_count = sum(
            "raman_callout_suggestion" in row.source_evidence for row in rows
        )
        fiber_scope = diagram_fiber_type_scope(result)
        route_band_status = _diagram_route_optical_band_status(result)
        route_band = (
            str(getattr(result, "optical_band", "") or "").strip()
            if route_band_status == "direct_supported"
            else ""
        )
        _log_route_event(
            f"Imported diagram {result.source.file_name!r} "
            f"sha256={result.source.sha256}; images={len(result.source.images)}, "
            "mop_diagram_images="
            f"{len(workbook_diagram.images) if workbook_diagram is not None else 0}, "
            "mop_diagram_required="
            f"{str(workbook_diagram is not None).casefold()}, "
            f"shelves={len(rows)}, spans={len(result.active_spans)}, "
            "route_integrity=accepted, "
            "transcription_required_review="
            f"{required_review_count}, "
            "transcription_source_required_review="
            f"{source_issue_aggregate.required_review_count}, "
            "transcription_source_absences="
            f"{issue_accounting.source_absence_count}, "
            "transcription_review_accounting="
            f"{_format_count_pairs(issue_accounting.category_counts)}, "
            f"transcription_advisories={source_issue_aggregate.advisory_count}, "
            "transcription_issue_codes="
            f"{_format_count_pairs(source_issue_aggregate.code_counts)}, "
            "transcription_required_paths="
            f"{_format_count_pairs(issue_aggregate.required_path_counts)}, "
            "transcription_missing_fields="
            f"{_format_count_pairs(issue_aggregate.missing_leaf_counts)}, "
            "transcription_missing_paths="
            f"{_format_count_pairs(issue_aggregate.missing_path_counts)}, "
            "transcription_source_missing_fields="
            f"{_format_count_pairs(source_issue_aggregate.missing_leaf_counts)}, "
            "transcription_source_missing_paths="
            f"{_format_count_pairs(source_issue_aggregate.missing_path_counts)}, "
            "revision_scope_defaulted="
            f"{str(revision_scope_defaulted).casefold()}, "
            f"route_optical_band={route_band or 'not_prepopulated'}, "
            f"route_optical_band_status={route_band_status}, "
            f"site_code_review_suggestions={suggested_site_code_count}, "
            "shelf_variant_chassis_suggestions="
            f"{suggested_shelf_variant_count}, "
            f"raman_endpoint_callouts={raman_endpoint_callout_count}, "
            f"raman_legend_callouts={raman_legend_callout_count}, "
            f"raman_unresolved_callouts={raman_unresolved_callout_count}, "
            f"raman_shelf_suggestions={raman_suggested_shelf_count}, "
            "fiber_scope_inherited_spans="
            f"{issue_accounting.scope_inherited_count}, "
            "route_native_fiber_review=pending, "
            f"unresolved_roles={unresolved_role_count}, "
            "unresolved_role_orders="
            f"{','.join(unresolved_role_orders) or 'none'}, "
            "deployment_readiness=not_authorized_pending_review.",
            logging.WARNING if required_review_count else logging.INFO,
        )
        self._refresh_status(
            f"Imported {len(rows)} shelf draft(s); route topology accepted; "
            f"{required_review_count} unresolved required value(s); "
            "raw source findings retained in the log; "
            "deployment readiness not authorized."
            + (
                f" Resolve {unresolved_role_count} shelf role(s)."
                if unresolved_role_count
                else ""
            )
        )
        unresolved_note = (
            (
                f"\n\n{unresolved_role_count} shelf role(s) could not be "
                "established from sufficient diagram evidence. They are shown "
                f"as {UNRESOLVED_PROFILE_LABEL!r}; select each visible role "
                "and use Confirm & Next Pending before configuration review."
            )
            if unresolved_role_count
            else ""
        )
        suggestion_note = (
            (
                f"\n\nATLAS supplied {suggested_site_code_count} editable "
                "site-code suggestion(s) from TID prefixes"
                + (
                    f" and {suggested_shelf_variant_count} chassis-family "
                    "shelf-variant suggestion(s)"
                    if suggested_shelf_variant_count
                    else ""
                )
                + (
                    " and started deliverable revision 1"
                    if revision_scope_defaulted
                    else ""
                )
                + ". These are workflow suggestions, not diagram evidence; "
                "verify them before using Confirm & Next Pending."
            )
            if (
                suggested_site_code_count
                or suggested_shelf_variant_count
                or revision_scope_defaulted
            )
            else ""
        )
        raman_note = (
            (
                f"\n\nATLAS found {raman_endpoint_callout_count} attached "
                "RAMAN slot/port callout(s) and prepared "
                f"{raman_suggested_shelf_count} pending shelf display "
                "suggestion(s). Confirming a suggestion records SRA presence "
                "for compatibility review; it never enables RAMAN CLI."
                + (
                    f" {raman_legend_callout_count} detached legend sample "
                    "callout(s) were retained as context only."
                    if raman_legend_callout_count
                    else ""
                )
                + (
                    f" {raman_unresolved_callout_count} callout(s) remain "
                    "unassigned and will block bundle export."
                    if raman_unresolved_callout_count
                    else ""
                )
            )
            if raman_callouts
            else ""
        )
        fiber_scope_note = (
            (
                "\n\nATLAS found one directly evidenced fiber label shared by "
                f"{len(fiber_scope.observed_span_orders)} active spans and "
                "scoped it to the one missing active-span record as a pending "
                "route-level suggestion. Select one audited Native CLI fiber "
                "type and use Apply to all spans. The source label is retained "
                "separately and is never converted to a CLI token."
            )
            if fiber_scope is not None
            else ""
        )
        route_band_note = (
            (
                "\n\nATLAS preserved the directly evidenced route-header "
                f"optical band {_display_optical_band_for_review(route_band)} "
                "as read-only exact-review context. It is not copied into "
                "each shelf and does not select a provider or BOM."
            )
            if route_band
            else (
                "\n\nA route-header optical-band candidate was not "
                "prepopulated because its direct evidence did not pass "
                "review."
                if route_band_status == "unverified"
                else ""
            )
        )
        messagebox.showwarning(
            "Diagram imported — human review required",
            (
                f"Imported {len(rows)} active shelf draft(s). Every row remains "
                "pending until you select it, verify each visible field, and use "
                "Confirm & Next Pending.\n\n"
                "Route topology integrity passed and the draft was imported. "
                f"After workflow accounting, {required_review_count} required "
                "field value(s) remain unresolved. The source transcription "
                f"reported {issue_accounting.source_absence_count} absent "
                "field value(s); "
                f"{issue_accounting.defaulted_count} received controlled "
                "workflow defaults, "
                f"{issue_accounting.suggestion_pending_count} received "
                "pending review suggestions, "
                f"{issue_accounting.scope_inherited_count} fiber omission(s) "
                "received a unanimous route-scope suggestion pending operator "
                "review, "
                f"{issue_accounting.optional_count} are optional, and "
                f"{issue_accounting.lifecycle_excluded_count} belong to "
                "excluded lifecycle records"
                + (
                    f". The source also reported "
                    f"{source_issue_aggregate.advisory_count} advisory issue(s)"
                    if source_issue_aggregate.advisory_count
                    else ""
                )
                + ".\n\n"
                f"{blocker_summary}{unresolved_note}{suggestion_note}"
                f"{raman_note}{fiber_scope_note}{route_band_note}"
                "\n\nConfiguration deployment readiness is a separate "
                "assessment and is not authorized by diagram transcription."
            ),
            parent=self,
        )

    def _preview_mop(self) -> None:
        _log_route_event("MOP preview requested.")
        if export_mop is None:
            detail = (
                friendly_error(_MOP_IMPORT_ERROR)
                if _MOP_IMPORT_ERROR is not None
                else "MOP exporter unavailable."
            )
            messagebox.showerror("MOP exporter unavailable", detail, parent=self)
            _log_route_event(
                f"MOP preview unavailable: {detail}", logging.ERROR
            )
            return
        if not self._require_committed_editor():
            return
        try:
            project = self._build_project(require_valid=False)
            fingerprint = route_project_fingerprint(project)
        except (TypeError, ValueError) as exc:
            _log_route_event(
                f"MOP preview refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning("Route not ready", str(exc), parent=self)
            return
        try:
            workbook_diagram = _validated_diagram_attachment(
                project,
                getattr(self, "_attached_workbook_diagram", None),
            )
        except (DiagramAssetError, TypeError, ValueError) as exc:
            _log_route_event(
                "MOP preview refused: Diagram-tab attachment is missing or "
                f"stale; {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Diagram reattachment required",
                (
                    f"{friendly_error(exc)}\n\n"
                    "Use Reattach Diagram to select the original local source. "
                    "This local-only step does not send the diagram to AI or "
                    "replace the reviewed route."
                ),
                parent=self,
            )
            return
        preview_dir = tempfile.TemporaryDirectory(
            prefix="atlas_rls_mop_preview_",
            ignore_cleanup_errors=True,
        )
        output_path = (
            Path(preview_dir.name)
            / f"{_filename_stem(project.route_code)}_MOP_PREVIEW.xlsx"
        )
        self._refresh_status("Creating a watermarked MOP preview…")
        _log_route_event(
            f"Creating watermarked MOP preview for route "
            f"{project.route_code!r} with {len(project.shelves)} shelf(s)."
        )
        self._submit_background(
            "preview",
            lambda: export_mop(
                project,
                output_path,
                purpose="preview",
                diagram=workbook_diagram,
            ),
            fingerprint=fingerprint,
            context=preview_dir,
            foreground=True,
        )

    def _complete_preview(self, result: _WorkerResult) -> None:
        output = Path(result.value)
        self._preview_fingerprint = result.fingerprint
        self._preview_path = output
        if result.context is not None:
            self._preview_tempdirs.append(result.context)
        self._refresh_status(
            f"Current watermarked MOP preview: {output.name}"
        )
        _log_route_event(f"Created watermarked MOP preview at {output}.")
        try:
            _open_preview_file(output)
            _log_route_event(f"Opened MOP preview {output.name!r}.")
        except OSError as exc:
            _log_route_event(
                f"Could not open MOP preview {output}: {friendly_error(exc)}",
                logging.ERROR,
            )
            messagebox.showerror(
                "Could not open MOP preview",
                (
                    f"{friendly_error(exc)}\n\nThe current preview was created at "
                    f"{output}."
                ),
                parent=self,
            )

    def _apply_config_evaluation(self, result: RouteConfigBuild) -> None:
        labels: dict[str, str] = {}
        for status in result.readiness.shelf_statuses:
            if status.ready:
                labels[status.shelf_id] = "Config validated — bundle only"
            elif "PENDING_SHELF_REVIEW" in status.reason_codes:
                labels[status.shelf_id] = "Pending review — CLI blocked"
            elif "PENDING_RAMAN_CALLOUT_REVIEW" in status.reason_codes:
                labels[status.shelf_id] = "Review RAMAN — CLI blocked"
            elif "R40_EXACT_PROVIDER_SRA_CONFLICT" in status.reason_codes:
                labels[status.shelf_id] = (
                    "SRA provider required — CLI blocked"
                )
            elif R40_PENDING_SRA_PEER_REVIEW in status.reason_codes:
                labels[status.shelf_id] = (
                    "Paired SRA peer review pending — CLI blocked"
                )
            elif (
                "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE"
                in status.reason_codes
            ):
                labels[status.shelf_id] = (
                    "SRA provider required — CLI blocked"
                )
            elif "PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED" in status.reason_codes:
                labels[status.shelf_id] = "R4.0 review only — CLI gated"
            elif "EXACT_PROVIDER_REVIEW_REQUIRED" in status.reason_codes:
                labels[status.shelf_id] = (
                    "Select exact provider — CLI pending"
                )
            elif "PROPAGATION_PATH_INCOMPLETE" in status.reason_codes:
                labels[status.shelf_id] = (
                    "Review A→Z/Z→A propagation — CLI blocked"
                )
            elif "UNREVIEWED_OPTICAL_PATH" in status.reason_codes:
                labels[status.shelf_id] = (
                    "Review physical span layout — CLI blocked"
                )
            else:
                labels[status.shelf_id] = "Config validation blocked"
        self._config_labels = labels
        self._update_tree_readiness_cells()
        ready_count = sum(
            1 for status in result.readiness.shelf_statuses if status.ready
        )
        blocked_count = len(result.readiness.shelf_statuses) - ready_count
        route_rows = tuple(getattr(self, "_rows", ()) or ())
        reviewed_shelf_count = sum(
            getattr(row, "review_state", "") != "pending"
            for row in route_rows
        )
        exact_payload_count = sum(
            _r40_payload_version_state(
                getattr(row, "profile_payload", None)
            )
            == "current"
            for row in route_rows
        )
        optical_paths = tuple(
            path
            for link in tuple(getattr(self, "_links", ()) or ())
            for path in tuple(getattr(link, "paths", ()) or ())
        )
        reviewed_path_count = sum(
            getattr(path, "review_state", "") in {"confirmed", "corrected"}
            for path in optical_paths
        )
        propagation_views = tuple(
            view
            for link in tuple(getattr(self, "_links", ()) or ())
            for view in route_link_propagation_views(link)
        )
        reviewed_propagation_count = sum(
            view.egress_review is not None for view in propagation_views
        )
        reason_counts = _count_labels(
            _safe_diagnostic_code(code)
            for status in result.readiness.shelf_statuses
            if not status.ready
            for code in tuple(status.reason_codes)
        )
        if result.ready:
            message = (
                "Configuration deployment readiness passed for "
                f"{ready_count} shelf(s); final bundle export revalidates the "
                "current snapshot."
            )
        else:
            message = (
                "Configuration deployment readiness: "
                f"{ready_count} ready, {blocked_count} blocked. "
                f"Shelves reviewed {reviewed_shelf_count}/{len(route_rows)}; "
                f"exact configuration reviews {exact_payload_count}/"
                f"{len(route_rows)}; A→Z/Z→A propagation reviews "
                f"{reviewed_propagation_count}/{len(propagation_views)}; "
                f"physical spans reviewed "
                f"{reviewed_path_count}/{len(optical_paths)}."
            )
        self._refresh_status(message)
        _log_route_event(
            "Configuration deployment-readiness assessment completed: "
            f"ready_shelves={ready_count}, blocked_shelves={blocked_count}, "
            f"reviewed_shelves={reviewed_shelf_count}/{len(route_rows)}, "
            f"exact_payloads={exact_payload_count}/{len(route_rows)}, "
            "reviewed_propagation_paths="
            f"{reviewed_propagation_count}/{len(propagation_views)}, "
            "reviewed_physical_spans="
            f"{reviewed_path_count}/{len(optical_paths)}, "
            f"candidate_configs={getattr(result, 'config_count', 0)}, "
            f"reason_codes={_format_count_pairs(reason_counts)}.",
            logging.INFO if result.ready else logging.WARNING,
        )

    def _export_bundle(self) -> None:
        """Export the complete route documentation bundle, never partial CLI."""

        _log_route_event("Final route bundle export requested.")
        if export_route_bundle is None:
            detail = (
                friendly_error(_BUNDLE_IMPORT_ERROR)
                if _BUNDLE_IMPORT_ERROR is not None
                else "Route bundle exporter unavailable."
            )
            messagebox.showerror("Bundle exporter unavailable", detail, parent=self)
            _log_route_event(
                f"Bundle export unavailable: {detail}", logging.ERROR
            )
            return
        if not self._require_committed_editor():
            return
        try:
            project = self._build_project(require_valid=True)
            fingerprint = route_project_fingerprint(project)
        except (TypeError, ValueError) as exc:
            _log_route_event(
                f"Bundle export refused: {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning("Route not ready", str(exc), parent=self)
            return
        try:
            workbook_diagram = _validated_diagram_attachment(
                project,
                getattr(self, "_attached_workbook_diagram", None),
            )
        except (DiagramAssetError, TypeError, ValueError) as exc:
            _log_route_event(
                "Bundle export refused: Diagram-tab attachment is missing or "
                f"stale; {friendly_error(exc)}",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Diagram reattachment required",
                (
                    f"{friendly_error(exc)}\n\n"
                    "Use Reattach Diagram before previewing and exporting the "
                    "current deliverable."
                ),
                parent=self,
            )
            return
        readiness = project.deployment_readiness()
        if not readiness.ready:
            actions = _bundle_preflight_actions(project, readiness)
            action_counts = tuple(
                (code, count) for code, _label, count in actions
            )
            reason_counts = _count_labels(
                _safe_diagnostic_code(code)
                for status in readiness.shelf_statuses
                for code in status.reason_codes
            )
            _log_route_event(
                "Bundle export refused by deployment-readiness preflight; "
                f"actions={_format_count_pairs(action_counts)}, "
                f"reason_codes={_format_count_pairs(reason_counts)}, "
                "background_submitted=false, artifacts_created=false.",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Route bundle not ready",
                _bundle_preflight_message(actions),
                parent=self,
            )
            return
        if (
            not self._preview_fingerprint
            or self._preview_fingerprint != fingerprint
            or self._preview_path is None
            or not self._preview_path.is_file()
        ):
            _log_route_event(
                "Bundle export refused: current MOP preview is missing or stale.",
                logging.WARNING,
            )
            messagebox.showwarning(
                "Current MOP preview required",
                (
                    "Preview the current route before exporting its final bundle. "
                    "Any route or shelf edit invalidates the prior preview."
                ),
                parent=self,
            )
            return
        destination = filedialog.askdirectory(
            parent=self,
            title="Choose a folder for the Ciena RLS route bundle",
            initialdir=str(get_desktop_dir()),
            mustexist=True,
        )
        if not destination:
            _log_route_event("Bundle export cancelled by operator.")
            return
        self._refresh_status(
            "Building the final fail-closed route bundle in the background…"
        )
        _log_route_event(
            f"Building final route bundle for {project.route_code!r} with "
            f"{len(project.shelves)} shelf(s) in {destination}."
        )
        self._submit_background(
            "bundle",
            lambda: export_route_bundle(
                project,
                Path(destination),
                diagram=workbook_diagram,
            ),
            fingerprint=fingerprint,
            foreground=True,
        )

    def _complete_bundle(self, artifacts: Mapping[str, Path]) -> None:
        paths = tuple(Path(path) for path in artifacts.values())
        bundle_dir = paths[0].parent if paths else Path(".")
        self._refresh_status(f"Exported route bundle: {bundle_dir.name}")
        _log_route_event(
            f"Exported route bundle {bundle_dir} with {len(paths)} artifact(s)."
        )
        messagebox.showinfo(
            "Route bundle exported",
            (
                f"Created {bundle_dir.name} with the route project, final FBN "
                "MOP, complete per-shelf configuration candidates, validation "
                "report, and manifest.\n\n"
                "The final exporter revalidated and regenerated every candidate "
                "from this route snapshot. Candidate CLI is not declared "
                "deployable without the required engineering review."
            ),
            parent=self,
        )

    def _refresh_status(self, message: str = "") -> None:
        count = len(self._rows)
        rack_count = max(1, (count + 7) // 8) if count else 0
        planning_count = sum(
            1 for row in self._rows if profile_is_planning_only(row.profile_id)
        )
        pending_count = sum(
            1 for row in self._rows if row.review_state == "pending"
        )
        state = (
            f"{count} shelf{'ves' if count != 1 else ''}; "
            f"{rack_count} rack diagram{'s' if rack_count != 1 else ''}; "
            f"{pending_count} pending review; {planning_count} CLI-gated."
        )
        if self._dirty:
            state += " Unsaved changes."
        self._status_var.set(f"{message}  {state}".strip())


__all__ = [
    "DIAGRAM_PRIVACY_NOTICE",
    "PLANNING_CLI_NOTICE",
    "R40_UI_PROFILE_IDS",
    "R40_UI_RELEASE",
    "UNRESOLVED_PROFILE_LABEL",
    "DiagramImportMutationBlocker",
    "DiagramReviewAccounting",
    "RlsRouteFrame",
    "account_diagram_review_issues",
    "build_route_project",
    "diagram_import_mutation_blockers",
    "diagram_issue_summary",
    "imported_values_changed",
    "profile_choices",
    "profile_display_name",
    "profile_is_planning_only",
    "profile_readiness_label",
    "provider_identity_changed",
]
