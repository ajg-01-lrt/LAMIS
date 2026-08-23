"""Review-only catalog for the quarantined RLS R4.0 workbook assumptions.

This module does not generate CLI and deliberately has no dependency on the
legacy workbook adapter.  It records the exact fixed layouts hidden behind the
five legacy worksheet labels so Route Builder can show those assumptions to a
reviewer without presenting them as vendor defaults or deployable profiles.

Only passive, non-secret diagram facts are accepted.  Every returned artifact
states that no provider is available and that deployment is not approved.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from types import MappingProxyType
from typing import Mapping


R40_REVIEW_RELEASE = "RLS R4.0"

_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "community",
    "privatekey",
    "licensekey",
    "token",
    "apikey",
    "credential",
)
_SECRET_VALUE_MARKERS = (
    "-----begin private key-----",
    "-----begin encrypted private key-----",
    "-----begin openssh private key-----",
)
_MAX_FACT_TEXT_LENGTH = 512

_IDENTITY_FIELDS = (
    "tid",
    "site_name",
    "primary_oam_ip",
    "ospf_area",
    "chassis",
)
_A_SPAN_FIELDS = (
    "a_neighbor_tid",
    "a_neighbor_role",
    "a_span_fiber_type",
    "a_span_distance_km",
    "a_span_loss_db",
)
_Z_SPAN_FIELDS = (
    "z_neighbor_tid",
    "z_neighbor_role",
    "z_span_fiber_type",
    "z_span_distance_km",
    "z_span_loss_db",
)
_DIAGRAM_FIELD_ORDER = (
    "route_id",
    *_IDENTITY_FIELDS,
    "site_code",
    "shelf_variant",
    "lifecycle",
    "power_label",
    "raman_label",
    *_A_SPAN_FIELDS,
    "a_span_circuit_id",
    "a_span_fiber_start",
    "a_span_fiber_end",
    *_Z_SPAN_FIELDS,
    "z_span_circuit_id",
    "z_span_fiber_start",
    "z_span_fiber_end",
    "notes",
)
_ALLOWED_DIAGRAM_FIELDS = frozenset(_DIAGRAM_FIELD_ORDER)
# Public, immutable contract used by route adapters to keep editor-only
# context out of the deterministic assumptions artifact.
R40_DIAGRAM_FACT_FIELDS = _ALLOWED_DIAGRAM_FIELDS

_COMMON_OPERATOR_FIELDS = (
    "software_release_confirmation",
    "exact_build_standard",
    "chassis_pec_and_installed_inventory",
    "module_pec_slot_and_port_inventory",
    "hostname_member_and_domain_standard",
    "loopback_ospf_and_osc_routing_design",
    "fiber_cli_token",
    "span_loss_semantics_patch_losses_and_repair_margin",
    "raman_sra_design_and_preflight",
    "licensed_feature_inventory",
    "plannerplus_runtime_tuning_mop",
    "ptc_and_span_calibration_mop",
)


class R40ReviewError(ValueError):
    """Raised when an R4.0 review request is unsupported or unsafe."""


@dataclass(frozen=True)
class R40FixedAssumption:
    """One value or layout fixed by the legacy workbook.

    ``vendor_default`` and ``deployable`` are intentionally always false for
    catalog entries.  They are explicit fields so a review UI cannot confuse a
    workbook constant with an authorized Ciena default.
    """

    key: str
    value: str
    note: str
    vendor_default: bool = False
    deployable: bool = False


@dataclass(frozen=True)
class R40ReviewProfile:
    """One exact legacy worksheet label and its review-only assumptions."""

    profile_id: str
    display_name: str
    source_sheet: str
    role: str
    side: str
    fixed_assumptions: tuple[R40FixedAssumption, ...]
    unsafe_assumptions: tuple[str, ...]
    diagram_required_fields: tuple[str, ...]
    operator_required_fields: tuple[str, ...]
    provider_available: bool = False
    deployment_approved: bool = False


@dataclass(frozen=True)
class R40ReviewArtifact:
    """Deterministic review result containing no executable configuration."""

    software_release: str
    profile: R40ReviewProfile
    diagram_facts: Mapping[str, object]
    missing_diagram_fields: tuple[str, ...]
    report_text: str
    provider_available: bool = False
    deployment_approved: bool = False


def _assumption(key: str, value: str, note: str) -> R40FixedAssumption:
    return R40FixedAssumption(key=key, value=value, note=note)


_COMMON_FIXED_ASSUMPTIONS = (
    _assumption(
        "fqdn_rule",
        "lowercase TID plus the workbook's customer-specific DNS suffix",
        "The embedded suffix is project-specific and is not a portable default.",
    ),
    _assumption(
        "loopback_prefix",
        "/32",
        "The workbook fixes the IPv4 loopback prefix.",
    ),
    _assumption(
        "ospf_defaults",
        "protocol instance 2; metric 10; priority 1; timers 40/10/10",
        "These values are workbook constants and still require DCN review.",
    ),
    _assumption(
        "oscpfib",
        "-5.0",
        "The workbook applies one blanket value without PlannerPlus evidence.",
    ),
    _assumption(
        "repair_margin_db",
        "2",
        "The workbook does not expose repair margin as an input.",
    ),
    _assumption(
        "line_band_ranges",
        "C 191.27500-196.15000 THz; L 186.05000-190.87500 THz",
        "The same C+L boundaries are reused by all five sheets.",
    ),
    _assumption(
        "licensed_features",
        (
            "OTDR, Span Calibration, NBI, Connection Validation, "
            "Performance Monitoring, and Alarm Correlation enabled"
        ),
        "The workbook does not prove license inventory or commissioning order.",
    ),
)

_COMMON_UNSAFE_ASSUMPTIONS = (
    "The source emits an invalid commit-to-quit line and has broken batch boundaries.",
    "Initial OAM is incomplete and omits required network-instance bindings.",
    "OSC OSPF interface identifiers omit the required -1 component.",
    "Blank-shelf links use set-link behavior instead of the documented create workflow.",
    "No chassis PEC, installed inventory, slot occupancy, or compatibility model exists.",
    "Remote endpoints are guessed from worksheet role and Raman branches.",
    "Raman may be inferred by the unsafe legacy 17 dB threshold.",
    "Legacy fiber aliases, misspellings, and Unknown do not establish a native CLI token.",
    "Licensed features are enabled without license or prerequisite evidence.",
    "Passive Terminal Control and safe staged Span Calibration sequencing are absent.",
    "OSCPFIB and power targets are blanket constants without PlannerPlus evidence.",
)

_ADD_DROP_FIXED = (
    _assumption(
        "12x1_hardware",
        (
            "slot 1 NTK852BC; OSC slot 1/subslot 50 NTK591VQ; "
            "slot 3 NTK852NA; slots 7 and 8 NTK834AA; slot 5 NTK834AE"
        ),
        "One fixed R4 C+L terminal layout is hidden behind the 12x1 selection.",
    ),
    _assumption(
        "64x1_hardware",
        "slot 1 NTK862AH; OSC slot 1/subslot 50 NTK591VQ",
        "The workbook reuses a blanket RLA64 TDA-style pattern.",
    ),
    _assumption(
        "raman_hardware",
        "NTK830AC in slot 6 when Raman is selected",
        "The slot is fixed rather than checked against installed inventory.",
    ),
    _assumption(
        "external_endpoint_pattern",
        (
            "no Raman: local 1/53 to remote 1/54; "
            "Raman: local 6/5 to remote 3/6"
        ),
        "The remote endpoint is guessed and does not vary by exact neighbor BOM.",
    ),
)

_ILA_FIXED = (
    _assumption(
        "dle_hardware",
        (
            "slot 1 NTK850DC; OSC slot 1/subslots 50 and 60 NTK591VQ"
        ),
        "This is one bidirectional C+L DLE layout, not a generic ILA.",
    ),
    _assumption(
        "dle_line_ports",
        "line pair 1 uses ports 54/63; line pair 2 uses ports 64/53",
        "The port layout is fixed by the single workbook design.",
    ),
    _assumption(
        "raman_hardware",
        "NTK830AC in directional slots 3 and/or 4",
        "The SRA slots are fixed by direction rather than inventory evidence.",
    ),
    _assumption(
        "raman_remote_endpoint_pattern",
        "remote slot 6/port 6 for ROADM; otherwise remote slot 3/port 6",
        "Neighbor role is used as a proxy for exact remote hardware.",
    ),
)

_ROADM_FIXED = (
    _assumption(
        "12x1_hardware",
        (
            "slot 1 NTK852BC; OSC slot 1/subslot 50 NTK591VQ; "
            "slot 3 NTK852NA"
        ),
        "One fixed RLA12/LRU12 degree is hidden behind the 12x1 selection.",
    ),
    _assumption(
        "64x1_hardware",
        "slot 1 NTK862AH; OSC slot 1/subslot 50 NTK591VQ",
        "The workbook reuses a blanket RLA64 TDA-style pattern.",
    ),
    _assumption(
        "raman_hardware",
        "NTK830AC in slot 6 when Raman is selected",
        "The slot is fixed rather than checked against installed inventory.",
    ),
    _assumption(
        "external_endpoint_pattern",
        (
            "no Raman: local 1/53 to remote 1/64; "
            "Raman: local 6/5 to remote 4/6"
        ),
        "The remote endpoint is guessed and does not vary by exact neighbor BOM.",
    ),
    _assumption(
        "wss_half_links",
        "12 C/L pairs on ports 21/22 through 43/44",
        "The 12x1 sheets require twelve separate WSS neighbor labels.",
    ),
    _assumption(
        "wss_power_profiles",
        "manual target power -8.50 across the fixed WSS profile pattern",
        "The target lacks transponder and excess-loss design evidence.",
    ),
)

_ADD_DROP_UNSAFE = (
    "The Z-side pre-amplifier and channel-demux formulas can bind the wrong object.",
    "COLAN-A static commands can target colan-x, and dual static paths reuse keys.",
    "RLA64 selection is treated as generic Add/Drop even though its documented use is topology-specific.",
)
_ILA_UNSAFE = (
    "Two formulas can emit the Boolean FALSE into copied output.",
    "Diagnostic commands contain malformed smart-quote characters.",
    "The selectable fiber catalog contains a value with no attenuation lookup row.",
)
_ROADM_UNSAFE = (
    "Raman and RLA branches reference loss cells instead of their selectors.",
    "Raman internal links can use the wrong patch-loss cells.",
    "Several WSS link names, source ports, and targets disagree or are duplicated.",
    "RLA64 is reused under a generic ROADM label without exact TDA/CDC/CDA evidence.",
)


def _profile(
    *,
    profile_id: str,
    display_name: str,
    source_sheet: str,
    role: str,
    side: str,
    profile_fixed: tuple[R40FixedAssumption, ...],
    profile_unsafe: tuple[str, ...],
    required_spans: tuple[str, ...],
    operator_fields: tuple[str, ...],
) -> R40ReviewProfile:
    return R40ReviewProfile(
        profile_id=profile_id,
        display_name=display_name,
        source_sheet=source_sheet,
        role=role,
        side=side,
        fixed_assumptions=(*_COMMON_FIXED_ASSUMPTIONS, *profile_fixed),
        unsafe_assumptions=(*_COMMON_UNSAFE_ASSUMPTIONS, *profile_unsafe),
        diagram_required_fields=(*_IDENTITY_FIELDS, *required_spans),
        operator_required_fields=(*_COMMON_OPERATOR_FIELDS, *operator_fields),
    )


_CATALOG_ITEMS = (
    _profile(
        profile_id="add_drop_a",
        display_name="Legacy Add/Drop A endpoint assumption review",
        source_sheet="AddDrop_A",
        role="add_drop",
        side="A",
        profile_fixed=_ADD_DROP_FIXED,
        profile_unsafe=_ADD_DROP_UNSAFE,
        required_spans=_Z_SPAN_FIELDS,
        operator_fields=(
            "rla_and_add_drop_structure",
        ),
    ),
    _profile(
        profile_id="add_drop_z",
        display_name="Legacy Add/Drop Z endpoint assumption review",
        source_sheet="AddDrop_Z",
        role="add_drop",
        side="Z",
        profile_fixed=_ADD_DROP_FIXED,
        profile_unsafe=_ADD_DROP_UNSAFE,
        required_spans=_A_SPAN_FIELDS,
        operator_fields=(
            "rla_and_add_drop_structure",
        ),
    ),
    _profile(
        profile_id="ila",
        display_name="Legacy bidirectional C+L DLE ILA assumption review",
        source_sheet="ILA 1",
        role="ila",
        side="",
        profile_fixed=_ILA_FIXED,
        profile_unsafe=_ILA_UNSAFE,
        required_spans=(*_A_SPAN_FIELDS, *_Z_SPAN_FIELDS),
        operator_fields=(
            "dla_dle_sra_rail_and_cascade_variant",
            "bidirectional_port_mapping",
        ),
    ),
    _profile(
        profile_id="roadm_a",
        display_name="Legacy ROADM A endpoint assumption review",
        source_sheet="ROADM_A",
        role="roadm",
        side="A",
        profile_fixed=_ROADM_FIXED,
        profile_unsafe=_ROADM_UNSAFE,
        required_spans=_Z_SPAN_FIELDS,
        operator_fields=(
            "roadm_rla_degree_add_drop_and_protection_design",
            "twelve_wss_neighbor_labels",
            "power_profile_design",
        ),
    ),
    _profile(
        profile_id="roadm_z",
        display_name="Legacy ROADM Z endpoint assumption review",
        source_sheet="ROADM_Z",
        role="roadm",
        side="Z",
        profile_fixed=_ROADM_FIXED,
        profile_unsafe=_ROADM_UNSAFE,
        required_spans=_A_SPAN_FIELDS,
        operator_fields=(
            "roadm_rla_degree_add_drop_and_protection_design",
            "twelve_wss_neighbor_labels",
            "power_profile_design",
        ),
    ),
)

R40_REVIEW_CATALOG: Mapping[str, R40ReviewProfile] = MappingProxyType(
    {profile.profile_id: profile for profile in _CATALOG_ITEMS}
)


def get_r4_0_review_profile(profile_id: str) -> R40ReviewProfile:
    """Return one supported review profile or fail closed."""

    if not isinstance(profile_id, str) or profile_id not in R40_REVIEW_CATALOG:
        raise R40ReviewError(
            "Unsupported RLS R4.0 review profile. Supported profiles are: "
            + ", ".join(R40_REVIEW_CATALOG)
            + "."
        )
    return R40_REVIEW_CATALOG[profile_id]


def build_r4_0_review(
    profile_id: str,
    diagram_facts: Mapping[str, object],
    *,
    software_release: str = R40_REVIEW_RELEASE,
) -> R40ReviewArtifact:
    """Build a deterministic, non-executable R4.0 assumption review."""

    if software_release != R40_REVIEW_RELEASE:
        raise R40ReviewError(
            f"R4.0 review requires exact release {R40_REVIEW_RELEASE!r}; "
            f"received {software_release!r}."
        )
    profile = get_r4_0_review_profile(profile_id)
    normalized = _normalize_diagram_facts(diagram_facts)
    missing = tuple(
        field_name
        for field_name in profile.diagram_required_fields
        if _is_missing(normalized.get(field_name))
    )
    frozen_facts: Mapping[str, object] = MappingProxyType(dict(normalized))
    report = _render_report(profile, frozen_facts, missing)
    return R40ReviewArtifact(
        software_release=software_release,
        profile=profile,
        diagram_facts=frozen_facts,
        missing_diagram_fields=missing,
        report_text=report,
    )


def render_r4_0_review_report(
    profile_id: str,
    diagram_facts: Mapping[str, object],
    *,
    software_release: str = R40_REVIEW_RELEASE,
) -> str:
    """Return only the deterministic human-readable review report."""

    return build_r4_0_review(
        profile_id,
        diagram_facts,
        software_release=software_release,
    ).report_text


def _normalize_diagram_facts(
    diagram_facts: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(diagram_facts, Mapping):
        raise R40ReviewError("diagram_facts must be a mapping.")
    secret_paths = tuple(_secret_key_paths(diagram_facts))
    if secret_paths:
        raise R40ReviewError(
            "Credential- or secret-bearing diagram facts are prohibited."
        )
    if any(not isinstance(key, str) for key in diagram_facts):
        raise R40ReviewError("Diagram fact names must be strings.")
    unknown = sorted(set(diagram_facts) - _ALLOWED_DIAGRAM_FIELDS)
    if unknown:
        raise R40ReviewError(
            "Unsupported diagram fact fields: " + ", ".join(unknown) + "."
        )

    normalized: dict[str, object] = {}
    for field_name in _DIAGRAM_FIELD_ORDER:
        if field_name not in diagram_facts:
            continue
        value = diagram_facts[field_name]
        if value is None:
            normalized[field_name] = None
        elif isinstance(value, str):
            if (
                "\r" in value
                or "\n" in value
                or _TEXT_CONTROL_RE.search(value)
            ):
                raise R40ReviewError(
                    f"Diagram fact {field_name!r} contains unsafe control characters."
                )
            stripped = value.strip()
            if len(stripped) > _MAX_FACT_TEXT_LENGTH:
                raise R40ReviewError(
                    f"Diagram fact {field_name!r} exceeds "
                    f"{_MAX_FACT_TEXT_LENGTH} characters."
                )
            if any(
                marker in stripped.casefold()
                for marker in _SECRET_VALUE_MARKERS
            ):
                raise R40ReviewError(
                    f"Diagram fact {field_name!r} contains private-key material."
                )
            normalized[field_name] = stripped
        elif isinstance(value, bool):
            normalized[field_name] = value
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise R40ReviewError(
                    f"Diagram fact {field_name!r} must be finite."
                )
            normalized[field_name] = value
        else:
            raise R40ReviewError(
                f"Diagram fact {field_name!r} must be a scalar JSON value."
            )
    return normalized


def _secret_key_paths(value: object, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", key_text.casefold())
            nested_path = f"{path}.{key_text}" if path else key_text
            if any(
                fragment in normalized
                for fragment in _SECRET_KEY_FRAGMENTS
            ):
                found.append(nested_path)
            found.extend(_secret_key_paths(nested, nested_path))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found.extend(_secret_key_paths(nested, f"{path}[{index}]"))
    return tuple(found)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value)


def _display_value(value: object) -> str:
    if value is None:
        return "<not provided>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".15g")
    return str(value)


def _render_report(
    profile: R40ReviewProfile,
    diagram_facts: Mapping[str, object],
    missing: tuple[str, ...],
) -> str:
    lines = [
        "ATLAS Ciena RLS R4.0 Workbook-Assumption Review",
        "=" * 51,
        f"Software release: {R40_REVIEW_RELEASE}",
        f"Profile: {profile.profile_id} - {profile.display_name}",
        f"Legacy source sheet: {profile.source_sheet}",
        f"Role: {profile.role}",
        f"Side: {profile.side or 'not applicable'}",
        "CLI provider available: false",
        "Deployment approved: false",
        "Configuration generated: false",
        "",
        "Diagram facts",
        "-" * 13,
    ]
    if diagram_facts:
        for field_name in _DIAGRAM_FIELD_ORDER:
            if field_name in diagram_facts:
                lines.append(
                    f"{field_name}: {_display_value(diagram_facts[field_name])}"
                )
    else:
        lines.append("None supplied.")

    lines.extend(["", "Missing required diagram facts", "-" * 30])
    if missing:
        lines.extend(f"- {field_name}" for field_name in missing)
    else:
        lines.append("None.")

    lines.extend(
        [
            "",
            "Legacy fixed assumptions - not vendor defaults",
            "-" * 46,
        ]
    )
    for item in profile.fixed_assumptions:
        lines.extend(
            [
                f"- {item.key}: {item.value}",
                f"  Vendor default: false; deployable: false. {item.note}",
            ]
        )

    lines.extend(["", "Unsafe or incomplete workbook assumptions", "-" * 40])
    lines.extend(f"- {message}" for message in profile.unsafe_assumptions)

    lines.extend(["", "Operator review inputs", "-" * 22])
    lines.extend(
        f"- {field_name}" for field_name in profile.operator_required_fields
    )
    lines.extend(
        [
            "",
            "Decision",
            "-" * 8,
            (
                "REVIEW ONLY - These role-level legacy assumptions are not "
                "an exact provider selection or executable CLI."
            ),
            (
                "Choose and validate one separately audited exact provider "
                "against installed inventory. This narrative cannot authorize "
                "configuration generation or deployment."
            ),
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "R40FixedAssumption",
    "R40ReviewArtifact",
    "R40ReviewError",
    "R40ReviewProfile",
    "R40_DIAGRAM_FACT_FIELDS",
    "R40_REVIEW_CATALOG",
    "R40_REVIEW_RELEASE",
    "build_r4_0_review",
    "get_r4_0_review_profile",
    "render_r4_0_review_report",
]
