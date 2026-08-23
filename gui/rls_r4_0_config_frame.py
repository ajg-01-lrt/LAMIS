"""Integrated editor for the exact, audited Ciena RLS R4.0 providers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Mapping, Optional

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from utils.helpers import friendly_error
from utils.rls_config.common import (
    ConfigValidationError,
    FIBER_TYPES,
    ManagementInterface,
    ROUTING_LABELS,
    ROUTING_OSPF_GNE,
    ROUTING_OSPF_OSC_ONLY,
    ROUTING_STATIC_COLAN_OSPF_OSC,
    ValidationIssue,
)
from utils.rls_config.r4_0_generator import (
    COLAN_PROHIBITED,
    COLAN_TERMINAL_OPTIONAL,
    COLAN_TERMINAL_REQUIRED,
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    GENERATOR_VERSION,
    R40_PROVIDER_CATALOG,
    R40ConfigArtifact,
    R40ExactConfigGenerator,
    R40ExactRequest,
    R40LinePath,
    R40ProviderProfile,
    R40_ROUTE_SIDES,
    SUPPORTED_RELEASE,
    provider_profiles_for_role,
)
from utils.rls_config.r4_0_prepopulation import (
    LEGACY_WORKBOOK_NAME,
    LEGACY_WORKBOOK_SHA256,
    R40DirectionResolution,
    legacy_workbook_contract_text,
    resolve_r40_fixed_direction,
    resolve_r40_fixed_direction_fallback,
)


_PREVIEW_TYPES = ("Paste-safe CLI", "Annotated review", "Validation report")
_PLANNING_PREVIEW_TYPE = "Planning review"
_COLAN_REVIEW_DEFERRED = "Deferred — omit COLAN for factory staging"
_COLAN_REVIEW_CONFIGURED = "Configured — emit reviewed customer COLAN"
_COLAN_REVIEW_NOT_APPLICABLE = "COLAN prohibited — ILA has no COLAN"
_PENDING_DISPLAY = "PENDING — not supplied"
_ROUTING_ORDER = (
    ROUTING_OSPF_GNE,
    ROUTING_STATIC_COLAN_OSPF_OSC,
    ROUTING_OSPF_OSC_ONLY,
)
_TERMINAL_ROUTING_ORDER = (
    ROUTING_OSPF_GNE,
    ROUTING_STATIC_COLAN_OSPF_OSC,
)
_ROUTING_BY_LABEL = {ROUTING_LABELS[item]: item for item in _ROUTING_ORDER}
_ROUTE_SIDE_LABELS = {
    "A": "A-side / preceding route shelf",
    "Z": "Z-side / following route shelf",
}
_ROUTE_SIDE_BY_LABEL = {
    label: side for side, label in _ROUTE_SIDE_LABELS.items()
}


def _is_exact_target_build(value: object) -> bool:
    """Return whether a value is more specific than the release family."""

    text = str(value or "").strip()
    if not text:
        return False
    normalized = " ".join(
        text.casefold().replace("_", " ").replace("-", " ").split()
    )
    return normalized not in {
        "4.0",
        "r4.0",
        "rls 4.0",
        "rls r4.0",
        "release 4.0",
        "release r4.0",
    }


def _planning_preview_blockers(
    profile: R40ProviderProfile | None,
    role_profile: str,
    target_build: object,
    colan_review_state: object,
) -> tuple[ValidationIssue, ...]:
    """Return unresolved facts that permit review but prohibit exact CLI."""

    blockers: list[ValidationIssue] = []
    if profile is None:
        blockers.append(
            ValidationIssue(
                "error",
                "EXACT_PROVIDER_REQUIRED",
                "provider_id",
                "Select and verify an exact audited provider before CLI validation.",
                "Installed chassis and circuit-pack inventory.",
            )
        )
    if not _is_exact_target_build(target_build):
        blockers.append(
            ValidationIssue(
                "error",
                "TARGET_BUILD_REQUIRED",
                "target_software_build",
                "A specific R4.0 build/schema target is pending.",
                "On-box schema validation gate.",
            )
        )
    colan_policy = (
        profile.colan_policy
        if profile is not None
        else _role_colan_policy(role_profile)
    )
    # Optional terminal COLAN is not an exact-provider or CLI gate. Keep the
    # legacy required-policy branch fail-closed in case a future provider
    # explicitly opts into it.
    if (
        colan_policy == COLAN_TERMINAL_REQUIRED
        and colan_review_state != _COLAN_REVIEW_CONFIGURED
    ):
        blockers.append(
            ValidationIssue(
                "error",
                "TERMINAL_COLAN_DEFERRED",
                "management",
                "This provider requires a complete terminal COLAN design.",
                "Provider-scoped terminal installation policy.",
            )
        )
    return tuple(blockers)


def _planning_value(value: object) -> str:
    """Render an unresolved planning value without inventing a placeholder."""

    if value is None:
        return _PENDING_DISPLAY
    text = str(value).strip()
    return text if text else _PENDING_DISPLAY


def _render_planning_preview(
    profile: R40ProviderProfile | None,
    role_profile: str,
    fields: Mapping[str, object],
    blockers: tuple[ValidationIssue, ...],
) -> str:
    """Render review-only facts without constructing executable output."""

    def field(label: str, key: str) -> str:
        return f"{label}: {_planning_value(fields.get(key))}"

    colan_state = str(fields.get("colan_review_state", "") or "").strip()
    lines = [
        "PLANNING PREVIEW — NOT CLI — DO NOT DEPLOY",
        "=" * 58,
        "No CLI, exact request payload, or deployable artifact was generated.",
        "Apply and Copy remain disabled until strict validation succeeds.",
        "",
        "Unresolved CLI gates",
        "--------------------",
    ]
    if blockers:
        lines.extend(
            f"[{issue.code}] {issue.field}: {issue.message}"
            for issue in blockers
        )
    else:
        lines.append("None recorded by the planning-preview gate.")

    lines.extend(
        (
            "",
            "Reviewed identity",
            "-----------------",
            field("Shelf / TID", "shelf_name"),
            field("Shelf label", "shelf_label"),
            field("Site name", "site_name"),
            field("Member name", "member_name"),
            field("Hostname", "hostname"),
            field("Exact target build/schema", "target_software_build"),
            field("Frame/rack location (optional)", "frame_identification_code"),
            field("Bay number", "bay_number"),
            field("Physical shelf", "physical_shelf"),
            "",
            "Provider planning facts",
            "-----------------------",
            f"Route role: {_planning_value(role_profile)}",
            (
                f"Provider ID: {profile.provider_id}"
                if profile is not None
                else f"Provider ID: {_PENDING_DISPLAY}"
            ),
            (
                f"Layout: {profile.display_name}"
                if profile is not None
                else f"Layout: {_PENDING_DISPLAY}"
            ),
            (
                f"Chassis: {profile.chassis_family} / {profile.chassis_pec}"
                if profile is not None
                else f"Chassis: {_PENDING_DISPLAY}"
            ),
            (
                f"Hardware discriminator: {profile.hardware_profile}"
                if profile is not None
                else f"Hardware discriminator: {_PENDING_DISPLAY}"
            ),
        )
    )
    if profile is not None:
        lines.extend(
            (
                "Fixed equipment: "
                + ", ".join(
                    f"slot {slot} {pec}" for slot, pec in profile.equipment
                ),
                "Fixed OSC modules: "
                + ", ".join(
                    f"{slot}/{subslot} {pec}"
                    for slot, subslot, pec in profile.osc_modules
                ),
                "Local line endpoints: "
                + ", ".join(
                    f"{out_slot}/{out_port} out + {in_slot}/{in_port} in"
                    for (out_slot, out_port), (in_slot, in_port) in zip(
                        profile.line_outputs,
                        profile.line_inputs,
                        strict=True,
                    )
                ),
                f"Fixed BOM: {profile.bom_note}",
            )
        )

    lines.extend(
        (
            "",
            "OAM and routing review",
            "----------------------",
            field("Loopback IPv4", "loopback_ip"),
            field("OSPF area", "ospf_area"),
            f"Terminal COLAN state: {_planning_value(colan_state)}",
        )
    )
    if colan_state == _COLAN_REVIEW_DEFERRED:
        lines.extend(
            (
                "COLAN: Omitted from this candidate (optional factory-staging "
                "mode); no COLAN interface or routing commands will be emitted.",
            )
        )
    elif colan_state == _COLAN_REVIEW_NOT_APPLICABLE:
        lines.append("COLAN: Not applicable; this ILA uses loopback/OSC only.")
    else:
        lines.extend(
            (
                field("COLAN routing model", "routing_model"),
                field("COLAN interface", "management_name"),
                field("COLAN IPv4", "management_ip"),
                field("COLAN prefix", "management_prefix"),
                field("COLAN gateway", "management_gateway"),
            )
        )

    for number in (1, 2):
        raw = fields.get(f"line_{number}", {})
        line = raw if isinstance(raw, Mapping) else {}
        if not line and number == 2:
            continue
        lines.extend(
            (
                "",
                f"Physical line record {number}",
                "----------------------",
                f"Route side: {_planning_value(line.get('route_side'))}",
                f"Link name: {_planning_value(line.get('link_name'))}",
                f"Neighbor TID: {_planning_value(line.get('neighbor_node'))}",
                "Neighbor mux/demux PFG: "
                f"{_planning_value(line.get('neighbor_line_mux_pfg'))} / "
                f"{_planning_value(line.get('neighbor_line_demux_pfg'))}",
                f"Native fiber: {_planning_value(line.get('fiber_type'))}",
                "Expected directional loss (dB): "
                f"{_planning_value(line.get('expected_loss_db'))}",
            )
        )

    lines.extend(
        (
            "",
            "PLANNING PREVIEW — NOT CLI — DO NOT DEPLOY",
            "Resolve every listed gate, then run strict Validate & Preview.",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _flow_labels_for_side(side: str) -> tuple[str, str]:
    """Return local transmit and receive propagation for one route side."""

    if side == "A":
        return ("Z→A", "A→Z")
    if side == "Z":
        return ("A→Z", "Z→A")
    return ("", "")


def _role_line_semantics(role_profile: str) -> str:
    """Return the one line-record meaning shared by role providers."""

    values = {
        profile.line_semantics
        for profile in provider_profiles_for_role(role_profile)
    }
    return next(iter(values)) if len(values) == 1 else ""


def _line_record_title(semantics: str, number: int, side: str) -> str:
    """Render one local line record without conflating degree and traffic."""

    transmit, receive = _flow_labels_for_side(side)
    if semantics == "bidirectional_degree":
        if side:
            return (
                f"Degree {number} ({side}-facing; "
                f"{transmit} TX / {receive} RX)"
            )
        return f"Degree {number} (assign physical side)"
    if side and transmit:
        return f"{transmit} amplifier path (line-out {side}-side)"
    return f"Amplifier Path {number} (assign output side)"


def _line_record_label(semantics: str, number: int) -> str:
    """Return a validation label that preserves the record's hardware meaning."""

    if semantics == "bidirectional_degree":
        return f"Physical degree {number}"
    if semantics == "unidirectional_amplifier_path":
        return f"Amplifier path {number}"
    return f"Local line-output {number}"


def _role_colan_policy(role_profile: str) -> str:
    """Return one unambiguous COLAN policy for an exact route role."""

    policies = {
        profile.colan_policy
        for profile in provider_profiles_for_role(role_profile)
    }
    return next(iter(policies)) if len(policies) == 1 else ""


def _route_line_seeds_for_direction(
    lines_by_side: Mapping[str, object],
    line_1_side: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Map immutable A/Z route facts onto fixed local line-output records."""

    if line_1_side not in R40_ROUTE_SIDES:
        raise ValueError("Fixed local line-output 1 must face route side A or Z.")
    line_2_side = "Z" if line_1_side == "A" else "A"

    def seed(side: str) -> dict[str, object]:
        raw = lines_by_side.get(side, {})
        return dict(raw) if isinstance(raw, Mapping) else {}

    return seed(line_1_side), seed(line_2_side)


def _selected_provider_direction_resolution(
    route_seed: Mapping[str, object],
    role_profile: str,
    profile: R40ProviderProfile,
) -> R40DirectionResolution:
    """Resolve a fixed-direction suggestion after manual provider selection.

    Provider preselection and direction prepopulation are intentionally
    independent gates.  A route seed can therefore retain direct local
    endpoint evidence while leaving the provider combobox blank.  Once the
    operator explicitly chooses that exact provider, resolve the retained
    endpoint evidence against its immutable port map so the already reviewed
    A/Z span facts are not left hidden in ``lines_by_side``.

    The audited provider/role convention is used only when endpoint evidence
    is genuinely absent.  Ambiguous, conflicting, or low-confidence endpoint
    observations remain operator-required.
    """

    raw_hardware = route_seed.get("reviewed_hardware", {})
    hardware = raw_hardware if isinstance(raw_hardware, Mapping) else {}
    raw_endpoints = hardware.get("line_endpoints", ())
    endpoints = (
        raw_endpoints
        if isinstance(raw_endpoints, (list, tuple))
        else ()
    )
    resolution = resolve_r40_fixed_direction(profile, endpoints)
    if resolution.status == "missing_evidence" and not endpoints:
        resolution = resolve_r40_fixed_direction_fallback(
            profile,
            role_profile,
        )
    return resolution


def _route_seed_prepopulation_summary(
    route_seed: Mapping[str, object],
) -> str:
    """Summarize carried-forward values without displaying customer data."""

    raw = route_seed.get("prepopulation", {})
    prepopulation = raw if isinstance(raw, Mapping) else {}

    def item_count(key: str) -> int:
        value = prepopulation.get(key, ())
        return len(value) if isinstance(value, (list, tuple)) else 0

    route_count = item_count("route_reviewed_fields")
    derivation_count = item_count("controlled_derivations")
    default_count = item_count("controlled_defaults")
    exclusion_count = item_count("policy_exclusions")
    manual_count = item_count("manual_fields")
    peer_pfg_count = item_count("peer_pfg_suggestions")
    line_1_side = str(route_seed.get("line_1_route_side", "") or "").strip()
    raw_direction = route_seed.get("direction_resolution", {})
    direction = raw_direction if isinstance(raw_direction, Mapping) else {}
    direction_status = str(direction.get("status", "") or "").strip()
    line_semantics = str(
        route_seed.get("line_semantics", "") or ""
    ).strip()
    assignment_name = {
        "bidirectional_degree": "physical degree 1",
        "unidirectional_amplifier_path": "amplifier path 1 line-output",
    }.get(line_semantics, "local line-output 1")
    direction_summary = (
        (
            f"Audited provider/role defaults prepopulated fixed "
            f"{assignment_name} "
            f"to route {line_1_side}-side; confirm that non-executable mapping "
            "against the installed shelf."
        )
        if (
            line_1_side in R40_ROUTE_SIDES
            and direction_status == "controlled_fallback"
        )
        else (
            f"Direct local port evidence mapped fixed {assignment_name} to route "
            f"{line_1_side}-side; verify that mapping against the installed "
            "shelf."
        )
        if line_1_side in R40_ROUTE_SIDES
        else (
            f"Assign {assignment_name} to map stored A/Z span facts into the "
            "fixed local line-output records."
        )
    )
    peer_pfg_summary = (
        (
            f"{peer_pfg_count} adjacent-peer fixed PFG suggestion"
            f"{'' if peer_pfg_count == 1 else 's'} "
            f"{'was' if peer_pfg_count == 1 else 'were'} prepopulated from "
            "a route-compatible exact peer and "
            "reviewed direction provenance; operator review remains required. "
        )
        if peer_pfg_count
        else ""
    )
    return (
        "ATLAS loaded "
        f"{route_count} diagram/reviewed route field"
        f"{'' if route_count == 1 else 's'}, "
        f"{derivation_count} controlled derivation"
        f"{'' if derivation_count == 1 else 's'}, and "
        f"{default_count} editable workflow default"
        f"{'' if default_count == 1 else 's'}. "
        f"{exclusion_count} deployment-policy exclusion"
        f"{'' if exclusion_count == 1 else 's'} are enforced. "
        f"{manual_count} engineering/safety group"
        f"{'' if manual_count == 1 else 's'} remain for review. "
        f"{peer_pfg_summary}"
        f"{direction_summary}"
    )


def _route_span_context_text(raw: object) -> str:
    """Format passive span facts that do not belong in the exact request."""

    seed = raw if isinstance(raw, Mapping) else {}
    if seed.get("represented_by_route_span") is not True:
        return (
            "Uploaded route context: no modeled span represents this local "
            "line-output/degree; "
            "independent engineering is required."
        )
    parts: list[str] = []
    circuit_id = str(seed.get("circuit_id", "") or "").strip()
    if circuit_id:
        parts.append(f"circuit {circuit_id}")
    distance = seed.get("distance_km")
    if isinstance(distance, (int, float)) and not isinstance(distance, bool):
        parts.append(f"distance {float(distance):g} km")
    fiber_start = seed.get("fiber_start")
    fiber_end = seed.get("fiber_end")
    if (
        isinstance(fiber_start, int)
        and not isinstance(fiber_start, bool)
        and isinstance(fiber_end, int)
        and not isinstance(fiber_end, bool)
    ):
        parts.append(f"source fibers {fiber_start}-{fiber_end}")
    source_fiber = str(
        seed.get("source_fiber_label", "") or ""
    ).strip()
    if source_fiber:
        parts.append(f"diagram fiber label {source_fiber}")
    peer_pfg_source = str(
        seed.get("neighbor_pfg_source", "") or ""
    ).strip()
    if peer_pfg_source:
        source_label = {
            "current_exact_peer_payload": "current exact peer payload",
            "direct_peer_direction_evidence": (
                "direct peer port/direction evidence"
            ),
            "audited_peer_role_fallback": "audited peer role convention",
            "ordered_topology_single_degree": (
                "sole ordered adjacency of a one-degree peer"
            ),
        }.get(peer_pfg_source, "reviewed exact-peer evidence")
        parts.append(
            "neighbor PFG suggestion from "
            f"{source_label} (operator review required)"
        )
    outbound_flow = str(seed.get("outbound_flow", "") or "").strip()
    inbound_flow = str(seed.get("inbound_flow", "") or "").strip()
    if outbound_flow and inbound_flow:
        parts.append(
            f"propagation {outbound_flow} transmit / {inbound_flow} receive"
        )
        if seed.get("inbound_peer_reviewed") is True:
            peer_parts: list[str] = []
            peer_loss = seed.get("inbound_peer_loss_db")
            if (
                isinstance(peer_loss, (int, float))
                and not isinstance(peer_loss, bool)
            ):
                peer_parts.append(f"loss {float(peer_loss):g} dB")
            peer_fiber = str(
                seed.get("inbound_peer_fiber_type", "") or ""
            ).strip()
            if peer_fiber:
                peer_parts.append(f"fiber {peer_fiber}")
            peer_link = str(
                seed.get("inbound_peer_link_name", "") or ""
            ).strip()
            if peer_link:
                peer_parts.append(f"peer link {peer_link}")
            parts.append(
                f"{inbound_flow} facing-shelf egress review"
                + (": " + ", ".join(peer_parts) if peer_parts else " complete")
            )
        else:
            parts.append(
                f"{inbound_flow} facing-shelf egress review pending"
            )
    return (
        "Uploaded route context: "
        + (", ".join(parts) if parts else "no additional passive span facts")
        + ". These values remain documentation context unless a matching "
        "editable CLI field is shown above."
    )


def _display_optical_band(value: object) -> str:
    token = str(value or "").strip().casefold()
    return {
        "c": "C",
        "l": "L",
        "c+l": "C+L",
        "integrated_c+l": "Integrated C+L",
    }.get(token, "")


def _reviewed_hardware_context_text(route_seed: Mapping[str, object]) -> str:
    """Render reviewed planning facts beside the exact-provider BOM."""

    raw = route_seed.get("reviewed_hardware", {})
    context = raw if isinstance(raw, Mapping) else {}
    parts: list[str] = []
    labels = (
        ("route_role", "role"),
        ("chassis", "diagram chassis"),
        ("shelf_variant", "reviewed variant"),
        ("shelf_band", "shelf band"),
        ("topology", "topology"),
        ("add_drop_structure", "add/drop structure"),
        ("protection_type", "protection"),
        ("power_label", "power"),
        ("raman_label", "RAMAN"),
    )
    for key, label in labels:
        value = str(context.get(key, "") or "").strip()
        if value:
            parts.append(f"{label}: {value}")
    raw_modules = context.get("module_inventory", ())
    module_labels: list[str] = []
    if isinstance(raw_modules, (list, tuple)):
        for raw_module in raw_modules:
            if not isinstance(raw_module, Mapping):
                continue
            pec = str(raw_module.get("pec", "") or "").strip()
            role = str(raw_module.get("role", "") or "").strip()
            slot = raw_module.get("slot")
            subslot = raw_module.get("subslot")
            module = pec or role
            if not module:
                continue
            if role and role != pec:
                module += f" ({role})"
            if isinstance(slot, int) and not isinstance(slot, bool):
                module += f" @ {slot}"
                if isinstance(subslot, int) and not isinstance(subslot, bool):
                    module += f"/{subslot}"
            module_labels.append(module)
    if module_labels:
        parts.append("visible modules: " + ", ".join(module_labels))
    raw_endpoints = context.get("line_endpoints", ())
    endpoint_labels: list[str] = []
    if isinstance(raw_endpoints, (list, tuple)):
        for raw_endpoint in raw_endpoints:
            if not isinstance(raw_endpoint, Mapping):
                continue
            adjacency = str(
                raw_endpoint.get("adjacency", "") or ""
            ).strip()
            slot = raw_endpoint.get("slot")
            line_out = raw_endpoint.get("line_out_port")
            if (
                adjacency not in {"preceding", "following"}
                or not isinstance(line_out, int)
                or isinstance(line_out, bool)
            ):
                continue
            endpoint = f"{adjacency}: line-out {line_out}"
            if isinstance(slot, int) and not isinstance(slot, bool):
                endpoint += f" @ slot {slot}"
            endpoint_labels.append(endpoint)
    if endpoint_labels:
        parts.append("visible local endpoints: " + ", ".join(endpoint_labels))
    if not parts:
        return (
            "Reviewed diagram hardware context: no exact hardware "
            "discriminators were visible."
        )
    return (
        "Reviewed diagram/route context: "
        + "; ".join(parts)
        + ". Compare these planning facts with the selected provider and "
        "installed inventory; they do not select or authorize the BOM."
    )


def _provider_resolution_context_text(
    route_seed: Mapping[str, object],
) -> str:
    """Render controlled provider/direction resolution without customer data."""

    raw_provider = route_seed.get("provider_resolution", {})
    provider = raw_provider if isinstance(raw_provider, Mapping) else {}
    raw_direction = route_seed.get("direction_resolution", {})
    direction = raw_direction if isinstance(raw_direction, Mapping) else {}
    status = str(provider.get("status", "") or "").strip() or "not evaluated"
    provider_id = str(provider.get("provider_id", "") or "").strip()
    preselected = provider.get("preselect_allowed") is True
    raw_review_ids = provider.get("review_provider_ids", ())
    review_ids = (
        tuple(
            str(item).strip()
            for item in raw_review_ids
            if isinstance(item, str) and item.strip()
        )
        if isinstance(raw_review_ids, (list, tuple))
        else ()
    )
    sole_candidate_populated = bool(
        status in {"exact_match", "unique_candidate"}
        and provider_id
        and review_ids == (provider_id,)
    )
    reason_codes = provider.get("reason_codes", ())
    controlled_reasons = (
        ", ".join(
            str(item)
            for item in reason_codes
            if isinstance(item, str)
        )
        if isinstance(reason_codes, (list, tuple))
        else ""
    )
    lines = [
        "Provider resolution: "
        + status
        + (f"; candidate {provider_id}" if provider_id else "")
        + (
            "; direct-hardware-qualified suggestion populated."
            if preselected
            else "; sole route-compatible review candidate populated; "
            "direct hardware identity remains unproved."
            if sole_candidate_populated
            else "; review selection remains unresolved."
        )
    ]
    if controlled_reasons:
        lines.append("Provider reason codes: " + controlled_reasons + ".")
    band_scope = str(provider.get("band_scope", "") or "").strip()
    band_scope_source = str(
        provider.get("band_scope_source", "") or ""
    ).strip()
    represented_degree_count = provider.get(
        "represented_route_degree_count"
    )
    candidate_degree_count = provider.get(
        "candidate_provider_degree_count"
    )
    if band_scope:
        lines.append(
            "Provider optical-band scope: "
            f"{band_scope} ({band_scope_source or 'source unknown'})."
        )
    if (
        isinstance(represented_degree_count, int)
        and not isinstance(represented_degree_count, bool)
        and isinstance(candidate_degree_count, int)
        and not isinstance(candidate_degree_count, bool)
        and candidate_degree_count
    ):
        lines.append(
            "Physical-degree coverage: uploaded route represents "
            f"{represented_degree_count}; candidate provider defines "
            f"{candidate_degree_count}."
        )
    direction_status = str(
        direction.get("status", "") or ""
    ).strip() or "not evaluated"
    line_1_side = str(
        direction.get("line_1_route_side", "") or ""
    ).strip()
    line_semantics = str(
        route_seed.get("line_semantics", "") or ""
    ).strip()
    assignment_name = {
        "bidirectional_degree": "physical degree 1",
        "unidirectional_amplifier_path": "amplifier path 1 line-output",
    }.get(line_semantics, "local line-output 1")
    lines.append(
        "Fixed line-map resolution: "
        + direction_status
        + (
            f"; {assignment_name.capitalize()} faces route "
            f"{line_1_side}-side."
            if line_1_side in R40_ROUTE_SIDES
            else "; operator mapping is required."
        )
    )
    lines.append(
        "All suggestions are non-executable until every fixed discriminator "
        "is reviewed and validation succeeds."
    )
    return "\n".join(lines)


def _provider_profiles_for_route_seed(
    role_profile: str,
    route_seed: Mapping[str, object],
) -> tuple[R40ProviderProfile, ...]:
    """Return only provider options that survived project-aware constraints."""

    profiles = provider_profiles_for_role(role_profile)
    raw_resolution = route_seed.get("provider_resolution", {})
    resolution = (
        raw_resolution if isinstance(raw_resolution, Mapping) else {}
    )
    if "review_provider_ids" not in resolution:
        # Standalone callers and older non-route seeds retain role-scoped
        # behavior. Integrated Route Builder seeds always carry this key.
        return profiles
    raw_ids = resolution.get("review_provider_ids")
    if not isinstance(raw_ids, (list, tuple)):
        return ()
    allowed_ids = {
        str(provider_id).strip()
        for provider_id in raw_ids
        if isinstance(provider_id, str) and provider_id.strip()
    }
    return tuple(
        profile for profile in profiles if profile.provider_id in allowed_ids
    )


def _initial_route_provider_choice(
    profiles: tuple[R40ProviderProfile, ...],
    route_seed: Mapping[str, object],
) -> tuple[R40ProviderProfile | None, str]:
    """Return one safe initial dropdown choice and its evidence class.

    ``direct_hardware`` is the existing strict advisory preselection: direct
    evidence established the provider identity gate. ``route_candidate`` only
    means exactly one provider survived the reviewed project constraints. It
    improves review visibility but does not identify the installed hardware,
    relax validation, or authorize CLI.

    Malformed, conflicting, ambiguous, unsupported-SRA, empty, and
    catalog-incompatible resolutions deliberately leave the dropdown blank.
    """

    raw_resolution = route_seed.get("provider_resolution", {})
    resolution = (
        raw_resolution if isinstance(raw_resolution, Mapping) else {}
    )
    status = str(resolution.get("status", "") or "").strip()
    if status not in {"exact_match", "unique_candidate"}:
        return None, ""
    raw_ids = resolution.get("review_provider_ids")
    if not isinstance(raw_ids, (list, tuple)):
        return None, ""
    provider_ids = tuple(
        str(provider_id).strip()
        for provider_id in raw_ids
        if isinstance(provider_id, str) and provider_id.strip()
    )
    if len(provider_ids) != 1:
        return None, ""
    provider_id = str(resolution.get("provider_id", "") or "").strip()
    if not provider_id or provider_id != provider_ids[0]:
        return None, ""
    reason_codes = tuple(
        str(code).strip().upper()
        for code in resolution.get("reason_codes", ())
        if isinstance(code, str) and code.strip()
    ) if isinstance(resolution.get("reason_codes", ()), (list, tuple)) else ()
    if (
        "R40_SRA_PROVIDER_UNAVAILABLE" in reason_codes
        or "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE" in reason_codes
        or "R40_EXACT_PROVIDER_SRA_CONFLICT" in reason_codes
    ):
        return None, ""
    profile = next(
        (
            candidate
            for candidate in profiles
            if candidate.provider_id == provider_id
        ),
        None,
    )
    if profile is None:
        return None, ""
    raw_hardware = route_seed.get("reviewed_hardware", {})
    hardware = raw_hardware if isinstance(raw_hardware, Mapping) else {}
    raman_review_status = str(
        resolution.get(
            "raman_callout_review_status",
            hardware.get("raman_callout_review_status", ""),
        )
        or ""
    ).strip().casefold()
    if raman_review_status in {"pending", "invalid"}:
        return None, ""
    if (
        raman_review_status == "accepted"
        and not profile.supports_raman
    ):
        return None, ""
    evidence_class = (
        "direct_hardware"
        if resolution.get("preselect_allowed") is True
        else "route_candidate"
    )
    return profile, evidence_class


def _log_r40_event(
    owner: object,
    message: str,
    level: int = logging.INFO,
) -> None:
    """Persist one safe UI action without logging generated CLI or payloads."""

    controller = getattr(owner, "controller", None)
    activity = getattr(controller, "log_activity", None)
    if callable(activity):
        try:
            activity(message, level)
            return
        except Exception:
            logging.debug("R4.0 config log bridge failed", exc_info=True)
    logging.log(level, message.rstrip())


@dataclass
class _LineWidgets:
    link_name: tk.StringVar
    neighbor_node: tk.StringVar
    neighbor_mux_pfg: tk.StringVar
    neighbor_demux_pfg: tk.StringVar
    fiber_type: tk.StringVar
    expected_loss: tk.StringVar
    input_patch_loss: tk.StringVar
    output_patch_loss: tk.StringVar
    repair_margin: tk.StringVar
    high_loss_threshold: tk.StringVar
    ospcfib: tk.StringVar


class RlsR40ConfigFrame(ttk.Frame):
    """Review one route shelf against an explicitly chosen R4.0 provider."""

    def __init__(
        self,
        parent: tk.Widget,
        controller: Any,
        *,
        role_profile: str,
        route_seed: Mapping[str, object],
        initial_request: R40ExactRequest | None = None,
        assumptions_text: str = "",
        on_apply: Callable[[R40ExactRequest, R40ConfigArtifact], None],
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._role_profile = role_profile
        self._route_seed = dict(route_seed)
        raw_lines_by_side = self._route_seed.get("lines_by_side", {})
        self._route_lines_by_side = (
            {
                side: dict(raw_lines_by_side.get(side, {}))
                if isinstance(raw_lines_by_side.get(side, {}), Mapping)
                else {}
                for side in R40_ROUTE_SIDES
            }
            if isinstance(raw_lines_by_side, Mapping)
            else {side: {} for side in R40_ROUTE_SIDES}
        )
        self._seed_direction_side = ""
        self._assumptions_text = assumptions_text
        self._on_apply = on_apply
        self._generator = R40ExactConfigGenerator()
        self._artifact: Optional[R40ConfigArtifact] = None
        self._planning_preview_text: str | None = None
        self._advisory_preselected_provider_id = ""
        self._route_candidate_populated_provider_id = ""
        self._direction_assignment_provenance = ""
        self._input_variables: list[tk.Variable] = []
        self._loading = True
        self._profiles = _provider_profiles_for_route_seed(
            role_profile,
            self._route_seed,
        )
        self._line_semantics = _role_line_semantics(role_profile)
        self._provider_by_label = {
            profile.display_name: profile for profile in self._profiles
        }
        self._build()
        self._trace_inputs()
        if initial_request is not None:
            self.load_request(initial_request)
        else:
            self._load_route_seed()
            suggested_profile, evidence_class = (
                _initial_route_provider_choice(
                    self._profiles,
                    self._route_seed,
                )
            )
            if suggested_profile is not None:
                self._provider_var.set(suggested_profile.display_name)
                if evidence_class == "direct_hardware":
                    self._advisory_preselected_provider_id = (
                        suggested_profile.provider_id
                    )
                else:
                    self._route_candidate_populated_provider_id = (
                        suggested_profile.provider_id
                    )
            else:
                self._provider_var.set("")
            self._refresh_provider()
            line_1_side = str(
                self._route_seed.get("line_1_route_side", "") or ""
            ).strip()
            if (
                suggested_profile is not None
                and line_1_side in R40_ROUTE_SIDES
            ):
                self._line_1_route_side_var.set(
                    _ROUTE_SIDE_LABELS[line_1_side]
                )
                self._on_route_side_selected(provenance="atlas")
            elif suggested_profile is not None:
                self._apply_selected_provider_direction_suggestion(
                    suggested_profile,
                )
        self._loading = False
        self._invalidate()
        self._dirty = False

    def _new_string(self, value: str = "") -> tk.StringVar:
        variable = tk.StringVar(value=value)
        self._input_variables.append(variable)
        return variable

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=8, pady=(5, 4))
        ttk.Label(
            header,
            text=f"{SUPPORTED_RELEASE} exact provider review",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text=(
                "ATLAS may populate the dropdown with the sole provider that "
                "survives reviewed route constraints so it is visible during "
                "shelf review. ATLAS may preselect one audited provider as "
                "direct-hardware-qualified only when direct diagram facts "
                "also pass the strict identity gate. Both states are "
                "non-executable: verify chassis, circuit-pack inventory, "
                "degree count, and port map. Output remains a documented "
                "pre-calibration candidate and requires successful on-box "
                "validate."
            ),
            foreground="#8a4d00",
            justify=tk.LEFT,
            wraplength=1080,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(
            header,
            text=_route_seed_prepopulation_summary(self._route_seed),
            foreground="#2f5d50",
            justify=tk.LEFT,
            wraplength=1080,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            header,
            text=(
                "Provider-specific deployment controls are included "
                "automatically in the validation report and bundle manifest "
                "as advisory procedures. They are not verified observations "
                "and do not assert the shelf's physical or operational state."
            ),
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=1080,
        ).pack(anchor=tk.W, pady=(4, 0))

        choice = ttk.LabelFrame(self, text="Exact audited provider")
        choice.pack(fill=tk.X, padx=8, pady=(2, 5))
        ttk.Label(choice, text="Provider:").grid(
            row=0, column=0, sticky=tk.E, padx=(8, 4), pady=7
        )
        self._provider_var = self._new_string()
        self._provider_control = ttk.Combobox(
            choice,
            textvariable=self._provider_var,
            values=tuple(self._provider_by_label),
            state="readonly" if self._profiles else "disabled",
            width=72,
        )
        self._provider_control.grid(
            row=0, column=1, sticky=tk.EW, padx=(0, 8), pady=7
        )
        self._provider_control.bind(
            "<<ComboboxSelected>>", lambda _event: self._provider_selected()
        )
        choice.grid_columnconfigure(1, weight=1)
        self._provider_hint = tk.StringVar(
            value=(
                "Choose an exact provider to unlock fixed hardware and "
                "configuration validation."
                if self._profiles
                else "No exact audited provider is registered for this route role."
            )
        )
        ttk.Label(
            choice,
            textvariable=self._provider_hint,
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=1050,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 7))
        assignment_label = (
            "Fixed physical degree 1 faces:"
            if self._line_semantics == "bidirectional_degree"
            else "Fixed amplifier path 1 line-out faces:"
        )
        ttk.Label(choice, text=assignment_label).grid(
            row=2, column=0, sticky=tk.E, padx=(8, 4), pady=(0, 7)
        )
        self._line_1_route_side_var = self._new_string()
        self._route_side_control = ttk.Combobox(
            choice,
            textvariable=self._line_1_route_side_var,
            values=tuple(_ROUTE_SIDE_LABELS[side] for side in R40_ROUTE_SIDES),
            state="readonly" if self._profiles else "disabled",
            width=42,
        )
        self._route_side_control.grid(
            row=2, column=1, sticky=tk.W, padx=(0, 8), pady=(0, 7)
        )
        self._route_side_control.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._on_route_side_selected(
                provenance="operator"
            ),
        )

        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=2)
        self._identity_tab = ttk.Frame(self._notebook)
        self._oam_tab = ttk.Frame(self._notebook)
        self._hardware_tab = ttk.Frame(self._notebook)
        self._line1_tab = ttk.Frame(self._notebook)
        self._line2_tab = ttk.Frame(self._notebook)
        self._legacy_fields_tab = ttk.Frame(self._notebook)
        self._preview_tab = ttk.Frame(self._notebook)
        self._notebook.add(self._identity_tab, text="Identity")
        self._notebook.add(self._oam_tab, text="OAM & Routing")
        self._notebook.add(self._hardware_tab, text="Fixed Hardware")
        initial_line_labels = (
            ("Line Degree 1", "Line Degree 2")
            if self._line_semantics == "bidirectional_degree"
            else ("Amplifier Path 1", "Amplifier Path 2")
        )
        self._notebook.add(self._line1_tab, text=initial_line_labels[0])
        self._notebook.add(self._line2_tab, text=initial_line_labels[1])
        self._notebook.add(self._legacy_fields_tab, text="Workbook Fields")
        self._notebook.add(self._preview_tab, text="Preview")

        self._build_identity()
        self._build_oam()
        self._build_hardware()
        self._line_mapping_hints: dict[int, tk.StringVar] = {}
        self._line_context_hints: dict[int, tk.StringVar] = {}
        self._line_1 = self._build_line(self._line1_tab, 1)
        self._line_2 = self._build_line(self._line2_tab, 2)
        self._swap_direction_button = ttk.Button(
            self._line1_tab,
            text=(
                "Swap Degree 1 / Degree 2 inputs"
                if self._line_semantics == "bidirectional_degree"
                else "Swap Amplifier Path 1 / Path 2 inputs"
            ),
            command=self._swap_direction_inputs,
            state=tk.NORMAL if self._profiles else tk.DISABLED,
        )
        self._swap_direction_button.grid(
            row=9,
            column=0,
            columnspan=4,
            sticky=tk.W,
            padx=10,
            pady=(2, 10),
        )
        self._build_legacy_fields()
        self._build_preview()

        actions = ttk.Frame(self)
        actions.pack(fill=tk.X, padx=7, pady=(4, 6))
        self._preview_button = ttk.Button(
            actions,
            text="Validate && Preview",
            command=self._preview,
            state=tk.NORMAL if self._profiles else tk.DISABLED,
        )
        self._preview_button.pack(side=tk.LEFT, padx=3)
        self._apply_button = ttk.Button(
            actions,
            text="Apply Reviewed Configuration",
            command=self._apply_reviewed,
            state=tk.DISABLED,
        )
        self._apply_button.pack(side=tk.LEFT, padx=3)
        self._status_var = tk.StringVar(
            value=(
                "Select the exact audited provider, complete all fields, and "
                "validate."
                if self._profiles
                else (
                    "Planning review only — no compatible audited provider; "
                    "CLI validation and Apply are blocked."
                )
            )
        )
        ttk.Label(
            actions, textvariable=self._status_var, foreground="#555555"
        ).pack(side=tk.LEFT, padx=10)

    @staticmethod
    def _field(
        parent: tk.Widget,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        column: int = 0,
        width: int = 34,
        readonly: bool = False,
    ) -> ttk.Entry:
        ttk.Label(parent, text=f"{label}:").grid(
            row=row, column=column, sticky=tk.E, padx=(8, 4), pady=4
        )
        control = ttk.Entry(parent, textvariable=variable, width=width)
        control.grid(
            row=row,
            column=column + 1,
            sticky=tk.EW,
            padx=(0, 8),
            pady=4,
        )
        if readonly:
            control.config(state="readonly")
        return control

    def _build_identity(self) -> None:
        tab = self._identity_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        self._target_build_var = self._new_string(
            DEFAULT_R40_TARGET_BUILD_SCHEMA
        )
        self._shelf_name_var = self._new_string()
        self._shelf_label_var = self._new_string()
        self._site_name_var = self._new_string()
        self._site_id_var = self._new_string("0")
        self._site_description_var = self._new_string()
        self._site_address_var = self._new_string()
        self._member_name_var = self._new_string()
        self._hostname_var = self._new_string()
        self._frame_id_var = self._new_string()
        self._bay_var = self._new_string("0")
        self._physical_shelf_var = self._new_string("0")

        self._field(
            tab,
            0,
            "Planned target R4.0 build/schema",
            self._target_build_var,
        )
        self._field(
            tab,
            0,
            "Shelf Name / TID (legacy B3)",
            self._shelf_name_var,
            column=2,
        )
        self._field(tab, 1, "Shelf label", self._shelf_label_var)
        self._field(tab, 1, "Site name", self._site_name_var, column=2)
        self._field(tab, 2, "Numeric site ID", self._site_id_var)
        self._field(tab, 2, "Member name", self._member_name_var, column=2)
        self._field(tab, 3, "Hostname", self._hostname_var)
        self._field(
            tab,
            3,
            "Frame/rack location (optional; legacy B4)",
            self._frame_id_var,
            column=2,
        )
        self._field(tab, 4, "Bay number", self._bay_var)
        self._field(tab, 4, "Physical shelf", self._physical_shelf_var, column=2)
        self._field(tab, 5, "Site description", self._site_description_var)
        self._field(tab, 5, "Site address", self._site_address_var, column=2)
        ttk.Label(
            tab,
            text=(
                f"ATLAS prepopulates the vendor-documented "
                f"{DEFAULT_R40_TARGET_BUILD_SCHEMA} R4.0.0 baseline. It is "
                "editable and not an on-box observation; the release notes "
                "also cover 4.00.01. Verify the running shelf and require "
                "successful validate before commit. "
                "Frame/rack location may remain blank until onsite; when blank, "
                "ATLAS omits the shelf-location command."
            ),
            foreground="#555555",
            justify=tk.LEFT,
        ).grid(row=6, column=0, columnspan=4, sticky=tk.W, padx=10, pady=10)

    def _build_oam(self) -> None:
        tab = self._oam_tab
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        self._loopback_var = self._new_string()
        self._ospf_area_var = self._new_string()
        role_colan_policy = _role_colan_policy(self._role_profile)
        self._colan_state_var = self._new_string(
            (
                _COLAN_REVIEW_NOT_APPLICABLE
                if role_colan_policy == COLAN_PROHIBITED
                else _COLAN_REVIEW_DEFERRED
            )
        )
        self._routing_var = self._new_string(
            (
                ROUTING_LABELS[ROUTING_OSPF_OSC_ONLY]
                if role_colan_policy == COLAN_PROHIBITED
                else ""
            )
        )
        self._management_name_var = self._new_string()
        self._management_ip_var = self._new_string()
        self._prefix_var = self._new_string()
        self._gateway_var = self._new_string()
        self._ospf_metric_var = self._new_string("10")
        self._static_metric_var = self._new_string("1500")

        self._field(
            tab,
            0,
            "Loopback IPv4 (legacy B5; diagram OAM candidate)",
            self._loopback_var,
        )
        self._field(
            tab,
            0,
            "OSPF area (legacy B7 where editable)",
            self._ospf_area_var,
            column=2,
        )
        ttk.Label(tab, text="Terminal COLAN state:").grid(
            row=1, column=0, sticky=tk.E, padx=(8, 4), pady=4
        )
        self._colan_state_control = ttk.Combobox(
            tab,
            textvariable=self._colan_state_var,
            values=(
                (_COLAN_REVIEW_NOT_APPLICABLE,)
                if role_colan_policy == COLAN_PROHIBITED
                else (
                    _COLAN_REVIEW_DEFERRED,
                    _COLAN_REVIEW_CONFIGURED,
                )
            ),
            state=(
                tk.DISABLED
                if role_colan_policy == COLAN_PROHIBITED
                else "readonly"
            ),
            width=40,
        )
        self._colan_state_control.grid(
            row=1, column=1, sticky=tk.EW, padx=(0, 8), pady=4
        )
        self._colan_state_control.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._refresh_management(),
        )
        ttk.Label(tab, text="Routing model:").grid(
            row=1, column=2, sticky=tk.E, padx=(8, 4), pady=4
        )
        self._routing_control = ttk.Combobox(
            tab,
            textvariable=self._routing_var,
            values=tuple(
                ROUTING_LABELS[item]
                for item in (
                    (ROUTING_OSPF_OSC_ONLY,)
                    if role_colan_policy == COLAN_PROHIBITED
                    else _TERMINAL_ROUTING_ORDER
                )
            ),
            state="readonly",
            width=40,
        )
        self._routing_control.grid(
            row=1, column=3, sticky=tk.EW, padx=(0, 8), pady=4
        )
        self._routing_control.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_management()
        )
        self._colan_frame = ttk.LabelFrame(
            tab,
            text=(
                "Optional terminal COLAN for remote staging "
                "(legacy B10:B17; no ILA COLAN)"
            ),
        )
        self._colan_frame.grid(
            row=2,
            column=0,
            columnspan=4,
            sticky=tk.EW,
            padx=8,
            pady=(4, 2),
        )
        self._colan_frame.grid_columnconfigure(1, weight=1)
        self._colan_frame.grid_columnconfigure(3, weight=1)
        self._management_name_control = self._field(
            self._colan_frame,
            0,
            "Customer-provided interface",
            self._management_name_var,
        )
        self._management_ip_control = self._field(
            self._colan_frame,
            0,
            "Customer-provided IPv4",
            self._management_ip_var,
            column=2,
        )
        self._prefix_control = self._field(
            self._colan_frame, 1, "COLAN prefix", self._prefix_var
        )
        self._gateway_control = self._field(
            self._colan_frame,
            1,
            "Default gateway",
            self._gateway_var,
            column=2,
        )
        self._ospf_metric_control = self._field(
            self._colan_frame,
            2,
            "COLAN OSPF metric",
            self._ospf_metric_var,
        )
        self._static_metric_control = self._field(
            self._colan_frame,
            2,
            "Static route metric",
            self._static_metric_var,
            column=2,
        )
        ttk.Label(
            tab,
            text=(
                "NTP is customer-managed. ATLAS does not request, store, "
                "validate, or emit RLS NTP settings."
            ),
            foreground="#2f5d50",
            justify=tk.LEFT,
        ).grid(
            row=3,
            column=0,
            columnspan=4,
            sticky=tk.W,
            padx=10,
            pady=(6, 2),
        )
        self._management_hint = tk.StringVar()
        ttk.Label(
            tab,
            textvariable=self._management_hint,
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=1040,
        ).grid(row=4, column=0, columnspan=4, sticky=tk.W, padx=10, pady=10)

    def _build_hardware(self) -> None:
        tab = self._hardware_tab
        tab.grid_columnconfigure(1, weight=1)
        self._provider_id_display = tk.StringVar()
        self._release_display = tk.StringVar(value=SUPPORTED_RELEASE)
        self._diagram_band_display = tk.StringVar(
            value=_display_optical_band(
                self._route_seed.get("diagram_optical_band", "")
            )
        )
        self._chassis_family_display = tk.StringVar()
        self._chassis_pec_display = tk.StringVar()
        self._hardware_profile_display = tk.StringVar()
        self._equipment_display = tk.StringVar()
        self._osc_display = tk.StringVar()
        for row, (label, variable) in enumerate(
            (
                ("Provider ID", self._provider_id_display),
                ("Software release", self._release_display),
                (
                    "Diagram route band (context only)",
                    self._diagram_band_display,
                ),
                ("Chassis family", self._chassis_family_display),
                ("Chassis PEC", self._chassis_pec_display),
                ("Hardware discriminator", self._hardware_profile_display),
                ("Fixed equipment", self._equipment_display),
                ("Fixed OSC modules", self._osc_display),
            )
        ):
            self._field(tab, row, label, variable, width=82, readonly=True)
        ttk.Label(
            tab,
            text=_reviewed_hardware_context_text(self._route_seed),
            foreground="#2f5d50",
            justify=tk.LEFT,
            wraplength=1030,
        ).grid(row=8, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(5, 2))
        self._bom_text = tk.Text(
            tab, height=7, wrap=tk.WORD, font=("TkDefaultFont", 9)
        )
        self._bom_text.grid(
            row=9, column=0, columnspan=2, sticky=tk.NSEW, padx=8, pady=8
        )
        self._bom_text.config(state=tk.DISABLED)
        tab.grid_rowconfigure(9, weight=1)

    def _build_line(self, tab: ttk.Frame, number: int) -> _LineWidgets:
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_columnconfigure(3, weight=1)
        values = _LineWidgets(
            link_name=self._new_string(),
            neighbor_node=self._new_string(),
            neighbor_mux_pfg=self._new_string(),
            neighbor_demux_pfg=self._new_string(),
            fiber_type=self._new_string(),
            expected_loss=self._new_string(),
            input_patch_loss=self._new_string("0.5"),
            output_patch_loss=self._new_string("0.5"),
            repair_margin=self._new_string("2"),
            high_loss_threshold=self._new_string("3"),
            ospcfib=self._new_string(),
        )
        self._field(tab, 0, "Local CLI link name", values.link_name)
        self._field(tab, 0, "Neighbor node TID", values.neighbor_node, column=2)
        self._field(
            tab,
            1,
            "This side's neighbor line-mux PFG",
            values.neighbor_mux_pfg,
        )
        self._field(
            tab,
            1,
            "This side's neighbor line-demux PFG",
            values.neighbor_demux_pfg,
            column=2,
        )
        ttk.Label(tab, text="Exact native fiber token:").grid(
            row=2, column=0, sticky=tk.E, padx=(8, 4), pady=4
        )
        ttk.Combobox(
            tab,
            textvariable=values.fiber_type,
            values=FIBER_TYPES,
            state="readonly",
            width=31,
        ).grid(row=2, column=1, sticky=tk.EW, padx=(0, 8), pady=4)
        self._field(
            tab,
            2,
            "Expected outbound line loss (dB)",
            values.expected_loss,
            column=2,
        )
        self._field(tab, 3, "Input patch-panel loss (dB)", values.input_patch_loss)
        self._field(tab, 3, "Output patch-panel loss (dB)", values.output_patch_loss, column=2)
        self._field(tab, 4, "Repair margin (dB)", values.repair_margin)
        self._field(tab, 4, "High-loss minor threshold (dB)", values.high_loss_threshold, column=2)
        self._field(tab, 5, "PlannerPlus OSCPFIB (dBm, optional)", values.ospcfib)
        mapping_hint = tk.StringVar(
            value="Select an exact provider to display the fixed local port/PFG map."
        )
        self._line_mapping_hints[number] = mapping_hint
        ttk.Label(
            tab,
            textvariable=mapping_hint,
            foreground="#8a4d00",
            justify=tk.LEFT,
            wraplength=1030,
        ).grid(row=6, column=0, columnspan=4, sticky=tk.W, padx=10, pady=(10, 2))
        context_hint = tk.StringVar(
            value=(
                "Uploaded route context appears after the first fixed local "
                "line-output is assigned to route side A or Z."
            )
        )
        self._line_context_hints[number] = context_hint
        ttk.Label(
            tab,
            textvariable=context_hint,
            foreground="#2f5d50",
            justify=tk.LEFT,
            wraplength=1030,
        ).grid(row=7, column=0, columnspan=4, sticky=tk.W, padx=10, pady=(2, 2))
        semantics_text = (
            (
                f"Line degree {number} is one physical RLA degree. Its local "
                "line-mux transmits and its paired line-demux receives, so the "
                "same represented degree carries both A→Z and Z→A traffic. "
                "A second tab is additional provider hardware—not the return "
                "route."
            )
            if self._line_semantics == "bidirectional_degree"
            else (
                f"Amplifier path {number} is one DLE propagation path and "
                "local line-output. Its downstream peer is on this output "
                "side; its upstream peer is taken from the opposite-side "
                "record. ATLAS does not reuse one neighbor for both ends."
            )
        )
        ttk.Label(
            tab,
            text=semantics_text,
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=1030,
        ).grid(row=8, column=0, columnspan=4, sticky=tk.W, padx=10, pady=(2, 10))
        return values

    def _build_legacy_fields(self) -> None:
        """Show the audited column-B input contract without executing it."""

        text = scrolledtext.ScrolledText(
            self._legacy_fields_tab,
            wrap=tk.WORD,
            font=("TkDefaultFont", 9),
        )
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        text.insert(
            tk.END,
            (
                f"Audited historical workbook: {LEGACY_WORKBOOK_NAME}\n"
                f"SHA-256: {LEGACY_WORKBOOK_SHA256}\n\n"
                f"{legacy_workbook_contract_text(self._role_profile)}\n\n"
                f"{_provider_resolution_context_text(self._route_seed)}\n\n"
                "ATLAS uses the workbook only as an audited inventory of "
                "literal column-B <...> fields. Its formulas, generated "
                "commands, embedded credentials, and license material are "
                "never loaded or executed. Values shown on the other tabs "
                "are the controlled exact-provider request fields; any "
                "historical placeholder that the audited provider cannot "
                "emit is listed here explicitly."
            ),
        )
        text.config(state=tk.DISABLED)

    def _build_preview(self) -> None:
        bar = ttk.Frame(self._preview_tab)
        bar.pack(fill=tk.X, padx=6, pady=(6, 2))
        ttk.Label(bar, text="Show:").pack(side=tk.LEFT)
        self._preview_type_var = tk.StringVar(value=_PREVIEW_TYPES[0])
        self._preview_chooser = ttk.Combobox(
            bar,
            textvariable=self._preview_type_var,
            values=_PREVIEW_TYPES,
            state="readonly",
            width=22,
        )
        self._preview_chooser.pack(side=tk.LEFT, padx=4)
        self._preview_chooser.bind(
            "<<ComboboxSelected>>", self._refresh_preview_text
        )
        self._copy_button = ttk.Button(
            bar, text="Copy", command=self._copy_preview, state=tk.DISABLED
        )
        self._copy_button.pack(side=tk.LEFT, padx=4)
        self._preview_text = scrolledtext.ScrolledText(
            self._preview_tab, wrap=tk.NONE, font=("Consolas", 9)
        )
        self._preview_text.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 6))
        self._preview_text.config(state=tk.DISABLED)

    def _trace_inputs(self) -> None:
        for variable in self._input_variables:
            variable.trace_add("write", self._invalidate)

    def _load_route_seed(self) -> None:
        seed = self._route_seed
        self._target_build_var.set(
            str(
                seed.get("target_software_build")
                or DEFAULT_R40_TARGET_BUILD_SCHEMA
            )
        )
        self._shelf_name_var.set(str(seed.get("shelf_name", "")))
        self._shelf_label_var.set(str(seed.get("shelf_label", "")))
        self._site_name_var.set(str(seed.get("site_name", "")))
        self._site_id_var.set(str(seed.get("site_id", 0)))
        self._site_description_var.set(str(seed.get("site_description", "")))
        self._site_address_var.set(str(seed.get("site_address", "")))
        self._member_name_var.set(str(seed.get("member_name", "")))
        self._hostname_var.set(str(seed.get("hostname", "")))
        self._frame_id_var.set(str(seed.get("frame_identification_code", "")))
        self._bay_var.set(str(seed.get("bay_number", 0)))
        self._physical_shelf_var.set(str(seed.get("physical_shelf", 0)))
        self._loopback_var.set(str(seed.get("loopback_ip", "")))
        self._ospf_area_var.set(str(seed.get("ospf_area", "")))
        self._diagram_band_display.set(
            _display_optical_band(seed.get("diagram_optical_band", ""))
        )
        # Fixed local line-output records are not synonymous with traffic
        # propagation. RLA records are physical bidirectional degrees; DLE
        # records are unidirectional amplifier through-paths.
        # When direct provider-compatible endpoint evidence or the audited
        # provider/role convention has resolved that mapping, hydrate both
        # fixed directions immediately from their independent A/Z route seeds.
        # When it has not, keep the combobox empty but expose the independent
        # A/Z seeds provisionally as route-side data.  This makes imported
        # neighbor/fiber/loss facts reviewable without falsely assigning them
        # to fixed degree/path records. The provisional A/Z layout also lets edits
        # survive the eventual mapping: selecting the staged first side
        # preserves the widgets and selecting the opposite side swaps them.
        line_1_side = str(seed.get("line_1_route_side", "") or "").strip()
        if line_1_side in R40_ROUTE_SIDES:
            line_1_seed, line_2_seed = _route_line_seeds_for_direction(
                self._route_lines_by_side,
                line_1_side,
            )
            self._load_line_seed(self._line_1, line_1_seed)
            self._load_line_seed(self._line_2, line_2_seed)
            self._line_1_route_side_var.set(
                _ROUTE_SIDE_LABELS[line_1_side]
            )
            self._seed_direction_side = line_1_side
            self._direction_assignment_provenance = "atlas"
        else:
            represented_sides = tuple(
                side
                for side in R40_ROUTE_SIDES
                if (
                    isinstance(
                        self._route_lines_by_side.get(side, {}),
                        Mapping,
                    )
                    and self._route_lines_by_side[side].get(
                        "represented_by_route_span"
                    )
                    is True
                )
            )
            provisional_line_1_side = (
                represented_sides[0]
                if len(represented_sides) == 1
                else "A"
            )
            line_1_seed, line_2_seed = _route_line_seeds_for_direction(
                self._route_lines_by_side,
                provisional_line_1_side,
            )
            self._load_line_seed(self._line_1, line_1_seed)
            self._load_line_seed(self._line_2, line_2_seed)
            self._seed_direction_side = provisional_line_1_side
            # A terminal commonly has one represented route degree. Put that
            # evidence on screen immediately instead of leaving the user on
            # the blank external-degree tab.
            RlsR40ConfigFrame._select_represented_direction_tab(self)
            _log_r40_event(
                self,
                (
                    "[RLS R4.0 CONFIG] Fixed local line map remains unassigned; "
                    "displayed independent route-side staging facts without "
                    f"authorizing validation; provisional_first_side="
                    f"{provisional_line_1_side}, represented_sides="
                    f"{','.join(represented_sides) or 'none'}."
                ),
            )

    @staticmethod
    def _load_line_seed(widgets: _LineWidgets, raw: object) -> None:
        seed = raw if isinstance(raw, Mapping) else {}
        widgets.link_name.set(str(seed.get("link_name", "")))
        widgets.neighbor_node.set(str(seed.get("neighbor_node", "")))
        widgets.neighbor_mux_pfg.set(
            str(seed.get("neighbor_line_mux_pfg", ""))
        )
        widgets.neighbor_demux_pfg.set(
            str(seed.get("neighbor_line_demux_pfg", ""))
        )
        widgets.fiber_type.set(str(seed.get("fiber_type", "")))
        loss = seed.get("expected_loss_db")
        widgets.expected_loss.set("" if loss is None else str(loss))

    def load_request(self, request: R40ExactRequest) -> None:
        if not isinstance(request, R40ExactRequest):
            raise TypeError("request must be an R40ExactRequest")
        profile = R40_PROVIDER_CATALOG.get(request.provider_id)
        if profile is None or profile not in self._profiles:
            raise ValueError(
                "The stored exact provider is not compatible with this route role."
            )
        self._loading = True
        try:
            self._provider_var.set(profile.display_name)
            self._line_1_route_side_var.set(
                _ROUTE_SIDE_LABELS.get(request.line_1_route_side, "")
            )
            self._seed_direction_side = request.line_1_route_side
            self._direction_assignment_provenance = "stored"
            self._target_build_var.set(request.target_software_build)
            self._shelf_name_var.set(request.shelf_name)
            self._shelf_label_var.set(request.shelf_label)
            self._site_name_var.set(request.site_name)
            self._site_id_var.set(str(request.site_id))
            self._site_description_var.set(request.site_description)
            self._site_address_var.set(request.site_address)
            self._member_name_var.set(request.member_name)
            self._hostname_var.set(request.hostname)
            self._frame_id_var.set(request.frame_identification_code)
            self._bay_var.set(str(request.bay_number))
            self._physical_shelf_var.set(str(request.physical_shelf))
            self._loopback_var.set(request.loopback_ip)
            self._ospf_area_var.set(request.ospf_area)
            management = request.management
            self._colan_state_var.set(
                (
                    _COLAN_REVIEW_NOT_APPLICABLE
                    if profile.colan_policy == COLAN_PROHIBITED
                    else (
                        _COLAN_REVIEW_CONFIGURED
                        if management.enabled
                        else _COLAN_REVIEW_DEFERRED
                    )
                )
            )
            self._routing_var.set(
                ROUTING_LABELS.get(
                    management.routing_mode,
                    ROUTING_LABELS[ROUTING_OSPF_GNE],
                )
            )
            self._management_name_var.set(management.name)
            self._management_ip_var.set(management.ip_address)
            self._prefix_var.set(
                "" if management.prefix_length is None else str(management.prefix_length)
            )
            self._gateway_var.set(management.gateway)
            self._ospf_metric_var.set(str(management.ospf_metric))
            self._static_metric_var.set(str(management.static_metric))
            self._load_line(self._line_1, request.line_1)
            if request.line_2 is None:
                self._load_line_seed(self._line_2, {})
            else:
                self._load_line(self._line_2, request.line_2)
            # Stored requests from the former checkbox workflow may contain
            # true confirmation flags. They are intentionally ignored:
            # automatic deployment-control advisories are not observations.
            self._refresh_provider()
        finally:
            self._loading = False
        self._invalidate()
        self._status_var.set("Loaded stored exact request — validate before applying.")

    @staticmethod
    def _load_line(widgets: _LineWidgets, line: R40LinePath) -> None:
        widgets.link_name.set(line.link_name)
        widgets.neighbor_node.set(line.neighbor_node)
        widgets.neighbor_mux_pfg.set(line.neighbor_line_mux_pfg)
        widgets.neighbor_demux_pfg.set(line.neighbor_line_demux_pfg)
        widgets.fiber_type.set(line.fiber_type)
        widgets.expected_loss.set(f"{line.expected_loss_db:g}")
        widgets.input_patch_loss.set(f"{line.input_patch_loss_db:g}")
        widgets.output_patch_loss.set(f"{line.output_patch_loss_db:g}")
        widgets.repair_margin.set(f"{line.repair_margin_db:g}")
        widgets.high_loss_threshold.set(f"{line.high_loss_minor_threshold_db:g}")
        widgets.ospcfib.set(
            "" if line.ospcfib_dbm is None else f"{line.ospcfib_dbm:g}"
        )

    def _provider_selected(self) -> None:
        self._refresh_provider()
        profile = self._selected_profile()
        if (
            profile is None
            or profile.provider_id
            != self._advisory_preselected_provider_id
        ):
            self._advisory_preselected_provider_id = ""
        if (
            profile is None
            or profile.provider_id
            != self._route_candidate_populated_provider_id
        ):
            self._route_candidate_populated_provider_id = ""
        direction_applied = (
            self._apply_selected_provider_direction_suggestion(
                profile,
                replace_atlas=(
                    getattr(
                        self,
                        "_direction_assignment_provenance",
                        "",
                    )
                    == "atlas"
                ),
            )
            if profile is not None
            else False
        )
        _log_r40_event(
            self,
            (
                f"[RLS R4.0 CONFIG] Exact provider selected: "
                f"{profile.provider_id}; "
                f"retained_direction_evidence_applied="
                f"{str(direction_applied).lower()}."
                if profile is not None
                else "[RLS R4.0 CONFIG] Exact provider selection cleared."
            ),
        )

    def _apply_selected_provider_direction_suggestion(
        self,
        profile: R40ProviderProfile,
        *,
        replace_atlas: bool = False,
    ) -> bool:
        """Hydrate A/Z route facts once their exact provider is selected."""

        current_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(),
            "",
        )
        current_provenance = getattr(
            self,
            "_direction_assignment_provenance",
            "",
        )
        if (
            current_side in R40_ROUTE_SIDES
            and not (
                replace_atlas
                and current_provenance == "atlas"
            )
        ):
            return False

        resolution = _selected_provider_direction_resolution(
            self._route_seed,
            self._role_profile,
            profile,
        )
        if not resolution.resolved:
            if (
                replace_atlas
                and current_provenance == "atlas"
            ):
                self._line_1_route_side_var.set("")
                self._direction_assignment_provenance = ""
                self._refresh_direction_mapping()
                self._invalidate()
            reason_codes = ",".join(resolution.reason_codes) or "none"
            self._provider_hint.set(
                self._provider_hint.get()
                + (
                    " The first fixed local line-output remains unassigned: "
                    f"{resolution.explanation} "
                    f"(status={resolution.status}; "
                    f"reason codes={reason_codes}). The imported A/Z route "
                    "facts remain visible below; choose the physical-side "
                    "mapping only "
                    "after checking the installed slot/port map."
                )
            )
            _log_r40_event(
                self,
                (
                    "[RLS R4.0 CONFIG] Retained endpoint evidence did not "
                    "resolve the first fixed local line-output for selected "
                    "provider "
                    f"{profile.provider_id}; status={resolution.status}, "
                    "reason_codes="
                    f"{reason_codes}."
                ),
            )
            return False

        side = resolution.line_1_route_side
        if current_side != side:
            self._line_1_route_side_var.set(_ROUTE_SIDE_LABELS[side])
            self._on_route_side_selected(provenance="atlas")
        else:
            self._direction_assignment_provenance = "atlas"
        self._provider_hint.set(
            self._provider_hint.get()
            + (
                f" Retained endpoint evidence mapped the first local "
                f"line-output to route "
                f"{side}-side and populated the represented route degree. "
                "Confirm the mapping against the installed port map."
            )
        )
        return True

    def _selected_profile(self) -> R40ProviderProfile | None:
        return self._provider_by_label.get(self._provider_var.get())

    def _set_line_cardinality(
        self,
        profile: R40ProviderProfile | None,
    ) -> None:
        """Expose only the physical line records defined by the provider.

        With no provider selected both tabs remain visible as provisional
        A/Z route-evidence staging. A one-degree ROADM provider has one
        bidirectional mux/demux degree, so reverse traffic must not create a
        phantom second hardware tab or request record.
        """

        one_degree = profile is not None and len(profile.line_outputs) == 1
        if one_degree:
            self._notebook.hide(self._line2_tab)
        else:
            self._notebook.tab(self._line2_tab, state="normal")
        self._swap_direction_button.config(
            state=(
                tk.DISABLED
                if one_degree or not self._profiles
                else tk.NORMAL
            )
        )

    def _apply_provider_link_name_defaults(
        self,
        profile: R40ProviderProfile,
    ) -> int:
        """Replace span/circuit labels with audited local CLI link names.

        Imported circuit IDs remain visible as read-only route context. They
        are not valid substitutes for provider-local link object names and
        can legitimately repeat across several spans. Existing operator
        corrections are preserved.
        """

        fixed_names = tuple(getattr(profile, "line_link_names", ()))
        widgets = (self._line_1, self._line_2)
        if len(fixed_names) != len(profile.line_outputs):
            return 0
        replaceable = {""}
        for raw in self._route_lines_by_side.values():
            if not isinstance(raw, Mapping):
                continue
            for key in ("link_name", "circuit_id"):
                value = str(raw.get(key, "") or "").strip()
                if value:
                    replaceable.add(value.casefold())
        for candidate in R40_PROVIDER_CATALOG.values():
            replaceable.update(
                value.casefold()
                for value in getattr(candidate, "line_link_names", ())
                if value
            )
        changed = 0
        for widget_set, fixed_name in zip(
            widgets[: len(fixed_names)],
            fixed_names,
            strict=True,
        ):
            current = widget_set.link_name.get().strip()
            if current.casefold() not in replaceable:
                continue
            if current != fixed_name:
                widget_set.link_name.set(fixed_name)
                changed += 1
        if changed:
            _log_r40_event(
                self,
                (
                    "[RLS R4.0 CONFIG] Applied audited provider-local CLI "
                    f"link-name defaults; provider={profile.provider_id}, "
                    f"records={changed}. Route circuit identifiers remain "
                    "documentation context."
                ),
            )
        return changed

    def _refresh_provider(self) -> None:
        profile = self._selected_profile()
        self._set_line_cardinality(profile)
        if profile is None:
            self._provider_id_display.set("")
            self._chassis_family_display.set("")
            self._chassis_pec_display.set("")
            self._hardware_profile_display.set("")
            self._equipment_display.set("")
            self._osc_display.set("")
            self._set_bom("")
            for number in (1, 2):
                self._line_mapping_hints[number].set(
                    "Select an exact provider to display the fixed local "
                    "port/PFG map."
                )
            if not self._profiles:
                raw_resolution = self._route_seed.get(
                    "provider_resolution",
                    {},
                )
                resolution = (
                    raw_resolution
                    if isinstance(raw_resolution, Mapping)
                    else {}
                )
                reason_codes = resolution.get("reason_codes", ())
                reason_text = (
                    ", ".join(
                        str(item)
                        for item in reason_codes
                        if isinstance(item, str)
                    )
                    if isinstance(reason_codes, (list, tuple))
                    else ""
                )
                self._provider_hint.set(
                    "No audited exact provider is compatible with the "
                    "reviewed route/shelf scope. Route facts remain visible "
                    "for planning review, but validation and CLI generation "
                    "are blocked."
                    + (
                        f" Reason codes: {reason_text}."
                        if reason_text
                        else ""
                    )
                )
            elif len(self._profiles) == 1:
                raw_resolution = self._route_seed.get(
                    "provider_resolution",
                    {},
                )
                resolution = (
                    raw_resolution
                    if isinstance(raw_resolution, Mapping)
                    else {}
                )
                status = str(
                    resolution.get("status", "") or ""
                ).strip()
                reason_codes = resolution.get("reason_codes", ())
                reason_text = (
                    ", ".join(
                        str(item)
                        for item in reason_codes
                        if isinstance(item, str)
                    )
                    if isinstance(reason_codes, (list, tuple))
                    else ""
                )
                self._provider_hint.set(
                    "No provider was preselected. The role-scoped provider "
                    f"({self._profiles[0].display_name}) remains advisory "
                    "until direct hardware evidence and installed inventory "
                    "are reconciled."
                    + (f" Resolution: {status}." if status else "")
                    + (
                        f" Reason codes: {reason_text}."
                        if reason_text
                        else ""
                    )
                )
            else:
                self._provider_hint.set(
                    "No provider is selected. Diagram role data alone cannot "
                    "select one."
                )
        else:
            self._provider_id_display.set(profile.provider_id)
            self._chassis_family_display.set(profile.chassis_family)
            self._chassis_pec_display.set(profile.chassis_pec)
            self._hardware_profile_display.set(profile.hardware_profile)
            self._equipment_display.set(
                ", ".join(f"slot {slot}: {pec}" for slot, pec in profile.equipment)
            )
            self._osc_display.set(
                ", ".join(
                    f"{slot}/{subslot}: {pec}"
                    for slot, subslot, pec in profile.osc_modules
                )
            )
            self._set_bom(
                f"{profile.bom_note}\n\nEvidence: {profile.evidence}"
                + (
                    f"\n\nRoute-role assumption review:\n{self._assumptions_text}"
                    if self._assumptions_text
                    else ""
                )
            )
            if (
                profile.provider_id
                == self._advisory_preselected_provider_id
            ):
                self._provider_hint.set(
                    f"ATLAS preselected advisory provider "
                    f"{profile.provider_id} from direct compatible diagram "
                    "facts. Review every fixed discriminator against installed "
                    "inventory; this suggestion does not authorize CLI."
                )
            elif (
                profile.provider_id
                == self._route_candidate_populated_provider_id
            ):
                self._provider_hint.set(
                    "ATLAS populated route-compatible review candidate "
                    f"{profile.provider_id} because it is the only provider "
                    "that survived the current project constraints. Direct "
                    "hardware evidence has not passed the strict provider "
                    "identity gate. Verify every fixed discriminator and the "
                    "installed inventory; this review convenience does not "
                    "identify installed hardware or authorize CLI."
                )
            else:
                self._provider_hint.set(
                    f"Selected provider ID: {profile.provider_id}. "
                    "Review every fixed discriminator against installed "
                    "inventory."
                )
            represented_side_count = sum(
                self._route_side_is_represented(side)
                for side in R40_ROUTE_SIDES
            )
            if (
                profile.line_semantics == "bidirectional_degree"
                and represented_side_count == 1
            ):
                if len(profile.line_outputs) == 1:
                    self._provider_hint.set(
                        self._provider_hint.get()
                        + (
                            " The uploaded terminal and this provider both "
                            "define one physical bidirectional degree. Its "
                            "mux/demux pair carries A→Z and Z→A traffic; no "
                            "second hardware degree or return-route tab is "
                            "created."
                        )
                    )
                else:
                    self._provider_hint.set(
                        self._provider_hint.get()
                        + (
                            " The uploaded terminal route represents one "
                            "physical bidirectional degree: its mux and demux "
                            "carry both A→Z and Z→A traffic. This selected exact "
                            "provider provisions additional physical hardware. "
                            "Keep CLI blocked unless installed inventory proves "
                            "that degree and its independently engineered peer."
                        )
                    )
            self._apply_provider_link_name_defaults(profile)
            if profile.application == "ila_dle_cl":
                self._routing_var.set(ROUTING_LABELS[ROUTING_OSPF_OSC_ONLY])
                self._management_name_var.set("")
                self._management_ip_var.set("")
                self._prefix_var.set("")
                self._gateway_var.set("")
        self._refresh_direction_mapping()
        self._refresh_management()
        self._invalidate()

    def _on_route_side_selected(
        self,
        *,
        provenance: str = "operator",
    ) -> None:
        """Bind side-keyed route evidence to fixed local line outputs."""

        selected_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(), ""
        )
        if selected_side not in R40_ROUTE_SIDES:
            self._refresh_direction_mapping()
            return
        if provenance not in {"atlas", "operator", "stored"}:
            provenance = "operator"

        prior_side = self._seed_direction_side
        was_loading = self._loading
        self._loading = True
        try:
            if not prior_side:
                line_1_seed, line_2_seed = _route_line_seeds_for_direction(
                    self._route_lines_by_side,
                    selected_side,
                )
                self._load_line_seed(self._line_1, line_1_seed)
                self._load_line_seed(self._line_2, line_2_seed)
            elif prior_side != selected_side:
                # There are only two route sides. Swapping every editable line
                # value preserves operator corrections while changing the
                # fixed-direction-to-side assignment.
                self._swap_line_widget_values()
            self._seed_direction_side = selected_side
            self._direction_assignment_provenance = provenance
        finally:
            self._loading = was_loading
        profile_getter = getattr(self, "_selected_profile", None)
        default_applier = getattr(
            self,
            "_apply_provider_link_name_defaults",
            None,
        )
        if callable(profile_getter) and callable(default_applier):
            profile = profile_getter()
            if profile is not None:
                default_applier(profile)
        self._refresh_direction_mapping()
        represented_record = (
            RlsR40ConfigFrame._select_represented_direction_tab(self)
        )
        self._invalidate()
        _log_r40_event(
            self,
            (
                "[RLS R4.0 CONFIG] First fixed local line-output assigned to route "
                f"{selected_side}-side; side-keyed span seeds remapped without "
                "copying an unrepresented degree; represented_record="
                f"{represented_record or 'none'}."
            ),
        )

    def _select_represented_direction_tab(self) -> int | None:
        """Show the sole modeled local line-output instead of a blank tab."""

        notebook = getattr(self, "_notebook", None)
        line1_tab = getattr(self, "_line1_tab", None)
        line2_tab = getattr(self, "_line2_tab", None)
        if notebook is None or line1_tab is None or line2_tab is None:
            return None
        line_1_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(),
            "",
        )
        # With an empty combobox, the two widgets are a provisional side-keyed
        # staging layout rather than fixed hardware directions.
        # `_load_route_seed` puts the sole represented side first when possible
        # (otherwise A first) and records that layout here.
        if (
            line_1_side not in R40_ROUTE_SIDES
            and self._seed_direction_side in R40_ROUTE_SIDES
        ):
            line_1_side = self._seed_direction_side
        line_2_side = (
            "Z"
            if line_1_side == "A"
            else "A"
            if line_1_side == "Z"
            else ""
        )
        represented = tuple(
            number
            for number, side in enumerate(
                (line_1_side, line_2_side),
                start=1,
            )
            if (
                side
                and isinstance(self._route_lines_by_side.get(side, {}), Mapping)
                and self._route_lines_by_side[side].get(
                    "represented_by_route_span"
                )
                is True
            )
        )
        if len(represented) != 1:
            return None
        number = represented[0]
        profile_getter = getattr(self, "_selected_profile", None)
        profile = profile_getter() if callable(profile_getter) else None
        if (
            profile is not None
            and len(profile.line_outputs) == 1
            and number != 1
        ):
            return None
        notebook.select(line1_tab if number == 1 else line2_tab)
        return number

    def _route_side_for_direction(self, number: int) -> str:
        line_1_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(), ""
        )
        if line_1_side not in R40_ROUTE_SIDES:
            return ""
        if number == 1:
            return line_1_side
        return "Z" if line_1_side == "A" else "A"

    def _route_side_is_represented(self, side: str) -> bool:
        raw = self._route_lines_by_side.get(side, {})
        return isinstance(raw, Mapping) and (
            raw.get("represented_by_route_span") is True
        )

    def _refresh_direction_mapping(self) -> None:
        profile = self._selected_profile()
        line_semantics = getattr(
            self,
            "_line_semantics",
            _role_line_semantics(getattr(self, "_role_profile", "")),
        )
        line_1_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(), ""
        )
        provisional_side_layout = (
            line_1_side not in R40_ROUTE_SIDES
            and self._seed_direction_side in R40_ROUTE_SIDES
        )
        if provisional_side_layout:
            provisional_line_1_side = self._seed_direction_side
            provisional_line_2_side = (
                "Z" if provisional_line_1_side == "A" else "A"
            )
            provisional_labels = []
            for number, side in enumerate(
                (provisional_line_1_side, provisional_line_2_side),
                start=1,
            ):
                transmit, receive = _flow_labels_for_side(side)
                provisional_labels.append(
                    (
                        f"Route {side}-facing degree data — "
                        f"{transmit} TX / {receive} RX — degree unassigned"
                    )
                    if line_semantics == "bidirectional_degree"
                    else (
                        f"Route {transmit} egress data — amplifier path "
                        "unassigned"
                    )
                )
            self._notebook.tab(
                self._line1_tab,
                text=provisional_labels[0],
            )
            self._notebook.tab(
                self._line2_tab,
                text=provisional_labels[1],
            )
            for number, side in enumerate(
                (provisional_line_1_side, provisional_line_2_side),
                start=1,
            ):
                self._line_context_hints[number].set(
                    _route_span_context_text(
                        self._route_lines_by_side.get(side, {})
                    )
                )
                transmit, receive = _flow_labels_for_side(side)
                self._line_mapping_hints[number].set(
                    (
                        f"Imported route {side}-facing degree data. This one "
                        f"degree carries both flows: {transmit} transmit and "
                        f"{receive} receive. It is not yet assigned to fixed "
                        "provider Degree 1 or Degree 2. Select the exact "
                        "provider and confirm the physical port map before "
                        "validation."
                    )
                    if line_semantics == "bidirectional_degree"
                    else (
                        f"Imported {transmit} egress data. This tab is not yet "
                        "assigned to fixed amplifier Path 1 or Path 2. Select "
                        "the exact provider and confirm which output side "
                        "fixed Path 1 uses before validation."
                    )
                )
            return
        line_2_side = (
            "Z" if line_1_side == "A" else "A" if line_1_side == "Z" else ""
        )
        self._notebook.tab(
            self._line1_tab,
            text=_line_record_title(
                line_semantics,
                1,
                line_1_side,
            ),
        )
        self._notebook.tab(
            self._line2_tab,
            text=_line_record_title(
                line_semantics,
                2,
                line_2_side,
            ),
        )
        for number, side in enumerate((line_1_side, line_2_side), start=1):
            if side:
                self._line_context_hints[number].set(
                    _route_span_context_text(
                        self._route_lines_by_side.get(side, {})
                    )
                )
            else:
                self._line_context_hints[number].set(
                    "Uploaded route context appears after the first fixed "
                    "local line-output is assigned to route side A or Z."
                )
        if profile is None:
            return
        if len(profile.line_outputs) == 1:
            self._line_mapping_hints[2].set(
                "Not applicable: this exact provider defines one physical "
                "bidirectional degree. The paired mux/demux carries both "
                "traffic directions."
            )
            self._line_context_hints[2].set(
                "No second local hardware degree is generated."
            )
        for number, ((slot, port), (mux_pfg, demux_pfg), side) in enumerate(
            zip(
                profile.line_outputs,
                profile.line_pfg_names,
                (line_1_side, line_2_side),
            ),
            start=1,
        ):
            side_text = (
                f"route {side}-side"
                if side
                else "route side not yet assigned"
            )
            if side and self._route_side_is_represented(side):
                transmit, receive = _flow_labels_for_side(side)
                source_text = (
                    (
                        f"Uploaded {side}-side span facts seed this physical "
                        f"degree. The degree carries {transmit} transmit and "
                        f"{receive} receive."
                    )
                    if profile.line_semantics == "bidirectional_degree"
                    else (
                        f"Uploaded {side}-side span facts seed the "
                        f"{transmit} amplifier egress path."
                    )
                )
            elif side:
                source_text = (
                    f"No uploaded route span represents the {side}-side "
                    + (
                        "additional hardware degree; enter independently "
                        "engineered neighbor, link, and loss values. This is "
                        "not the missing return route: the represented RLA "
                        "degree already carries both traffic flows."
                        if profile.line_semantics == "bidirectional_degree"
                        else "amplifier egress; enter independently engineered "
                        "neighbor, link, and loss values. ATLAS will not copy "
                        "the opposite span."
                    )
                )
            else:
                source_text = (
                    "Assign the first fixed local line-output to route side A "
                    "or Z before reviewing the seeded path facts."
                )
            record_name = (
                f"physical degree {number}"
                if profile.line_semantics == "bidirectional_degree"
                else f"amplifier path {number}"
            )
            self._line_mapping_hints[number].set(
                f"Fixed {record_name}: local line-out {slot}/{port}; "
                f"local PFG map {mux_pfg} / {demux_pfg}; {side_text}. "
                f"{source_text} Confirm this assignment against the installed "
                "packout."
            )

    def _swap_line_widget_values(self) -> None:
        for field_name in _LineWidgets.__dataclass_fields__:
            first = getattr(self._line_1, field_name)
            second = getattr(self._line_2, field_name)
            first_value = first.get()
            first.set(second.get())
            second.set(first_value)

    def _swap_direction_inputs(self) -> None:
        current = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(), ""
        )
        if current not in R40_ROUTE_SIDES:
            first_record = _line_record_label(self._line_semantics, 1).lower()
            messagebox.showwarning(
                "Assign route side first",
                (
                    f"Choose which route side the first fixed {first_record} "
                    "faces before swapping inputs."
                ),
                parent=self,
            )
            _log_r40_event(
                self,
                "[RLS R4.0 CONFIG] Line-output swap refused: route side is unassigned.",
                logging.WARNING,
            )
            return
        self._loading = True
        try:
            self._swap_line_widget_values()
        finally:
            self._loading = False
        swapped = "Z" if current == "A" else "A"
        self._line_1_route_side_var.set(_ROUTE_SIDE_LABELS[swapped])
        self._seed_direction_side = swapped
        self._refresh_direction_mapping()
        self._invalidate()
        _log_r40_event(
            self,
            "[RLS R4.0 CONFIG] Swapped fixed line-output inputs and route-side assignment.",
        )

    def _set_bom(self, text: str) -> None:
        self._bom_text.config(state=tk.NORMAL)
        self._bom_text.delete("1.0", tk.END)
        self._bom_text.insert("1.0", text)
        self._bom_text.config(state=tk.DISABLED)

    def _refresh_management(self) -> None:
        profile = self._selected_profile()
        colan_policy = (
            profile.colan_policy
            if profile is not None
            else _role_colan_policy(self._role_profile)
        )
        is_ila = colan_policy == COLAN_PROHIBITED
        if is_ila:
            self._colan_state_var.set(_COLAN_REVIEW_NOT_APPLICABLE)
            self._colan_state_control.config(
                values=(_COLAN_REVIEW_NOT_APPLICABLE,),
                state=tk.DISABLED,
            )
            self._routing_control.config(
                values=(ROUTING_LABELS[ROUTING_OSPF_OSC_ONLY],)
            )
            self._routing_var.set(ROUTING_LABELS[ROUTING_OSPF_OSC_ONLY])
            self._routing_control.config(state=tk.DISABLED)
            self._management_name_var.set("")
            self._management_ip_var.set("")
            self._prefix_var.set("")
            self._gateway_var.set("")
            self._colan_frame.grid_remove()
            mode = ROUTING_OSPF_OSC_ONLY
        else:
            self._colan_state_control.config(
                values=(
                    _COLAN_REVIEW_DEFERRED,
                    _COLAN_REVIEW_CONFIGURED,
                ),
                state="readonly",
            )
            if self._colan_state_var.get() not in {
                _COLAN_REVIEW_DEFERRED,
                _COLAN_REVIEW_CONFIGURED,
            }:
                self._colan_state_var.set(_COLAN_REVIEW_DEFERRED)
            self._routing_control.config(
                values=tuple(
                    ROUTING_LABELS[item] for item in _TERMINAL_ROUTING_ORDER
                )
            )
            self._colan_frame.grid()
            if self._colan_state_var.get() == _COLAN_REVIEW_DEFERRED:
                self._routing_var.set("")
                self._management_name_var.set("")
                self._management_ip_var.set("")
                self._prefix_var.set("")
                self._gateway_var.set("")
                self._routing_control.config(state=tk.DISABLED)
                mode = ""
            else:
                self._routing_control.config(state="readonly")
                mode = _ROUTING_BY_LABEL.get(self._routing_var.get(), "")
        configured = (
            not is_ila
            and self._colan_state_var.get() == _COLAN_REVIEW_CONFIGURED
        )
        numbered = configured
        for control in (
            self._management_name_control,
            self._management_ip_control,
            self._prefix_control,
        ):
            control.config(state=tk.NORMAL if numbered else tk.DISABLED)
        self._gateway_control.config(
            state=(
                tk.NORMAL
                if configured
                and mode == ROUTING_STATIC_COLAN_OSPF_OSC
                else tk.DISABLED
            )
        )
        self._ospf_metric_control.config(
            state=(
                tk.NORMAL
                if configured and mode == ROUTING_OSPF_GNE
                else tk.DISABLED
            )
        )
        self._static_metric_control.config(
            state=(
                tk.NORMAL
                if configured
                and mode == ROUTING_STATIC_COLAN_OSPF_OSC
                else tk.DISABLED
            )
        )
        if is_ila:
            self._management_hint.set(
                "The uploaded primary OAM IP is seeded as a loopback "
                "candidate; confirm its actual interface binding. This exact "
                "ILA uses IPv4 OSPFv2 RNE over loopback/OSC only. ILAs have "
                "no COLAN, and ATLAS cannot emit one."
            )
        elif not configured:
            self._management_hint.set(
                "Terminal COLAN is explicitly deferred. The uploaded primary "
                "OAM IP remains a loopback candidate and is never copied into "
                "COLAN. Customer interface, IPv4, prefix, gateway, and routing "
                "values stay blank. ATLAS can generate the remaining exact "
                "factory-staging candidate, Apply it to the reviewed route, "
                "and include it in a bundle, but it emits no COLAN commands. "
                "Configure the complete customer design when remote COLAN "
                "access is needed."
            )
        elif mode == ROUTING_OSPF_GNE:
            self._management_hint.set(
                "The uploaded primary OAM IP is seeded as a loopback "
                "candidate and is never copied into COLAN. Enter the "
                "customer-approved terminal COLAN values when available; "
                "COLAN participates in OSPF as the GNE/DCN interface and can "
                "provide remote factory-test access."
            )
        elif mode == ROUTING_STATIC_COLAN_OSPF_OSC:
            self._management_hint.set(
                "The uploaded primary OAM IP is seeded as a loopback "
                "candidate and is never copied into COLAN. Enter the "
                "customer-approved terminal COLAN values when available; "
                "COLAN uses a static default route while OSPF remains on "
                "loopback/OSC and can provide remote factory-test access."
            )
        else:
            self._management_hint.set(
                "Choose the customer-approved terminal COLAN routing model "
                "and enter every required COLAN value, or select Deferred to "
                "generate the remaining candidate without COLAN commands."
            )

    def _invalidate(self, *_args: object) -> None:
        if self._loading:
            return
        self._dirty = True
        self._artifact = None
        self._planning_preview_text = None
        self._apply_button.config(state=tk.DISABLED)
        self._copy_button.config(state=tk.DISABLED)
        self._preview_chooser.config(values=_PREVIEW_TYPES, state="readonly")
        if self._preview_type_var.get() == _PLANNING_PREVIEW_TYPE:
            self._preview_type_var.set(_PREVIEW_TYPES[0])
        self._set_preview_text("")
        if not self._profiles:
            self._status_var.set(
                "Planning review only — no compatible audited provider; "
                "CLI validation and Apply are blocked."
            )
        else:
            self._status_var.set(
                "Inputs changed — validate again before applying."
            )

    @staticmethod
    def _required_int(value: str, label: str) -> int:
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} must be a whole number.") from exc

    @staticmethod
    def _required_float(value: str, label: str) -> float:
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} must be a number.") from exc

    def _management_value(self, profile: R40ProviderProfile) -> ManagementInterface:
        if profile.colan_policy == COLAN_PROHIBITED:
            return ManagementInterface(
                enabled=False,
                name="",
                routing_mode=ROUTING_OSPF_OSC_ONLY,
                ip_address="",
                prefix_length=None,
                gateway="",
                ospf_metric=10,
                static_metric=1500,
            )
        if (
            profile.colan_policy == COLAN_TERMINAL_OPTIONAL
            and self._colan_state_var.get() != _COLAN_REVIEW_CONFIGURED
        ):
            return ManagementInterface(
                enabled=False,
                name="",
                routing_mode=ROUTING_OSPF_OSC_ONLY,
                ip_address="",
                prefix_length=None,
                gateway="",
                ospf_metric=10,
                static_metric=1500,
            )
        if self._colan_state_var.get() != _COLAN_REVIEW_CONFIGURED:
            raise ValueError(
                "This provider requires a complete terminal COLAN design "
                "before exact CLI validation."
            )
        try:
            mode = _ROUTING_BY_LABEL[self._routing_var.get()]
        except KeyError as exc:
            raise ValueError("Choose a supported management routing model.") from exc
        enabled = mode != ROUTING_OSPF_OSC_ONLY
        return ManagementInterface(
            enabled=enabled,
            name=self._management_name_var.get().strip() if enabled else "",
            routing_mode=mode,
            ip_address=self._management_ip_var.get().strip() if enabled else "",
            prefix_length=(
                self._required_int(self._prefix_var.get(), "COLAN prefix")
                if enabled
                else None
            ),
            gateway=(
                self._gateway_var.get().strip()
                if mode == ROUTING_STATIC_COLAN_OSPF_OSC
                else ""
            ),
            ospf_metric=(
                self._required_int(
                    self._ospf_metric_var.get(), "COLAN OSPF metric"
                )
                if mode == ROUTING_OSPF_GNE
                else 10
            ),
            static_metric=(
                self._required_int(
                    self._static_metric_var.get(), "Static route metric"
                )
                if mode == ROUTING_STATIC_COLAN_OSPF_OSC
                else 1500
            ),
        )

    def _line_value(self, widgets: _LineWidgets, number: int) -> R40LinePath:
        ospcfib_text = widgets.ospcfib.get().strip()
        expected_loss_text = widgets.expected_loss.get().strip()
        side = self._route_side_for_direction(number)
        record_label = _line_record_label(
            getattr(self, "_line_semantics", ""),
            number,
        )
        if not expected_loss_text and side:
            if self._route_side_is_represented(side):
                raise ValueError(
                    f"{record_label} ({side}-side) is represented by an "
                    "uploaded route span, but its expected loss is missing. "
                    "Review and enter the engineered directional loss."
                )
            external_record = (
                "external hardware degree"
                if getattr(self, "_line_semantics", "")
                == "bidirectional_degree"
                else "amplifier path"
                if getattr(self, "_line_semantics", "")
                == "unidirectional_amplifier_path"
                else "local line-output"
            )
            raise ValueError(
                f"{record_label} ({side}-side) is not represented by an "
                "uploaded route span. Enter independently engineered neighbor, "
                f"link, and expected-loss values for this {external_record}. "
                "ATLAS will not copy the opposite span."
            )
        return R40LinePath(
            link_name=widgets.link_name.get().strip(),
            neighbor_node=widgets.neighbor_node.get().strip(),
            neighbor_line_mux_pfg=widgets.neighbor_mux_pfg.get().strip(),
            neighbor_line_demux_pfg=widgets.neighbor_demux_pfg.get().strip(),
            fiber_type=widgets.fiber_type.get(),
            expected_loss_db=self._required_float(
                expected_loss_text, f"{record_label} expected loss"
            ),
            input_patch_loss_db=self._required_float(
                widgets.input_patch_loss.get(),
                f"{record_label} input patch loss",
            ),
            output_patch_loss_db=self._required_float(
                widgets.output_patch_loss.get(),
                f"{record_label} output patch loss",
            ),
            repair_margin_db=self._required_float(
                widgets.repair_margin.get(),
                f"{record_label} repair margin",
            ),
            high_loss_minor_threshold_db=self._required_float(
                widgets.high_loss_threshold.get(),
                f"{record_label} high-loss threshold",
            ),
            ospcfib_dbm=(
                self._required_float(
                    ospcfib_text, f"{record_label} OSCPFIB"
                )
                if ospcfib_text
                else None
            ),
        )

    def _build_request(self) -> R40ExactRequest:
        profile = self._selected_profile()
        if profile is None:
            raise ValueError(
                "Explicitly choose a compatible exact audited R4.0 provider."
            )
        line_1_route_side = _ROUTE_SIDE_BY_LABEL.get(
            self._line_1_route_side_var.get(), ""
        )
        if line_1_route_side not in R40_ROUTE_SIDES:
            first_record = _line_record_label(
                getattr(
                    profile,
                    "line_semantics",
                    getattr(self, "_line_semantics", ""),
                ),
                1,
            ).lower()
            raise ValueError(
                f"Assign the first fixed {first_record} to route side A or Z "
                "before validating the exact configuration."
            )
        return R40ExactRequest(
            provider_id=profile.provider_id,
            profile=self._role_profile,
            software_release=SUPPORTED_RELEASE,
            target_software_build=self._target_build_var.get().strip(),
            chassis_family=profile.chassis_family,
            chassis_pec=profile.chassis_pec,
            hardware_profile=profile.hardware_profile,
            shelf_name=self._shelf_name_var.get().strip(),
            shelf_label=self._shelf_label_var.get().strip(),
            site_name=self._site_name_var.get().strip(),
            site_id=self._required_int(self._site_id_var.get(), "Site ID"),
            site_description=self._site_description_var.get().strip(),
            site_address=self._site_address_var.get().strip(),
            member_name=self._member_name_var.get().strip(),
            hostname=self._hostname_var.get().strip(),
            frame_identification_code=self._frame_id_var.get().strip(),
            bay_number=self._required_int(self._bay_var.get(), "Bay number"),
            physical_shelf=self._required_int(
                self._physical_shelf_var.get(), "Physical shelf"
            ),
            loopback_ip=self._loopback_var.get().strip(),
            ospf_area=self._ospf_area_var.get().strip(),
            management=self._management_value(profile),
            osc_profile="ge-fec-1",
            line_1=self._line_value(self._line_1, 1),
            line_2=(
                self._line_value(self._line_2, 2)
                if len(profile.line_outputs) > 1
                else None
            ),
            line_1_route_side=line_1_route_side,
            # These legacy fields remain false because automatic provider
            # controls are advisory procedures, not verified observations.
            installed_inventory_confirmed=False,
            planner_runtime_mop_confirmed=False,
            target_build_confirmed=False,
            greenfield_fibers_disconnected_confirmed=False,
            calibration_feature_inactive_confirmed=False,
            cfim_unused_ports_terminated_confirmed=False,
        )

    def _planning_fields(
        self,
        profile: R40ProviderProfile | None,
    ) -> dict[str, object]:
        """Capture display-only editor facts without parsing an exact request."""

        def line_fields(widgets: _LineWidgets, number: int) -> dict[str, object]:
            return {
                "route_side": self._route_side_for_direction(number),
                "link_name": widgets.link_name.get().strip(),
                "neighbor_node": widgets.neighbor_node.get().strip(),
                "neighbor_line_mux_pfg": (
                    widgets.neighbor_mux_pfg.get().strip()
                ),
                "neighbor_line_demux_pfg": (
                    widgets.neighbor_demux_pfg.get().strip()
                ),
                "fiber_type": widgets.fiber_type.get().strip(),
                "expected_loss_db": widgets.expected_loss.get().strip(),
            }

        fields: dict[str, object] = {
            "shelf_name": self._shelf_name_var.get().strip(),
            "shelf_label": self._shelf_label_var.get().strip(),
            "site_name": self._site_name_var.get().strip(),
            "member_name": self._member_name_var.get().strip(),
            "hostname": self._hostname_var.get().strip(),
            "target_software_build": self._target_build_var.get().strip(),
            "frame_identification_code": self._frame_id_var.get().strip(),
            "bay_number": self._bay_var.get().strip(),
            "physical_shelf": self._physical_shelf_var.get().strip(),
            "loopback_ip": self._loopback_var.get().strip(),
            "ospf_area": self._ospf_area_var.get().strip(),
            "colan_review_state": self._colan_state_var.get(),
            "routing_model": self._routing_var.get().strip(),
            "management_name": self._management_name_var.get().strip(),
            "management_ip": self._management_ip_var.get().strip(),
            "management_prefix": self._prefix_var.get().strip(),
            "management_gateway": self._gateway_var.get().strip(),
            "line_1": line_fields(self._line_1, 1),
        }
        if profile is None or len(profile.line_outputs) > 1:
            fields["line_2"] = line_fields(self._line_2, 2)
        return fields

    def _show_planning_preview(
        self,
        profile: R40ProviderProfile | None,
        blockers: tuple[ValidationIssue, ...],
    ) -> None:
        """Show non-copyable review facts without creating an artifact."""

        self._artifact = None
        self._planning_preview_text = _render_planning_preview(
            profile,
            self._role_profile,
            self._planning_fields(profile),
            blockers,
        )
        self._apply_button.config(state=tk.DISABLED)
        self._copy_button.config(state=tk.DISABLED)
        self._preview_chooser.config(
            values=(_PLANNING_PREVIEW_TYPE,),
            state="readonly",
        )
        self._preview_type_var.set(_PLANNING_PREVIEW_TYPE)
        self._set_preview_text(self._planning_preview_text)
        self._notebook.select(self._preview_tab)
        self._status_var.set(
            "Planning review only — resolve pending gates for exact CLI."
        )
        _log_r40_event(
            self,
            "[RLS R4.0 CONFIG] Watermarked planning preview shown; "
            "no exact request, CLI, payload, or deployable artifact generated; "
            "blocker_codes="
            + ",".join(sorted({issue.code for issue in blockers}))
            + ".",
            logging.WARNING,
        )

    def _restore_exact_preview_choices(self) -> None:
        self._planning_preview_text = None
        self._preview_chooser.config(values=_PREVIEW_TYPES, state="readonly")
        if self._preview_type_var.get() not in _PREVIEW_TYPES:
            self._preview_type_var.set(_PREVIEW_TYPES[0])

    @staticmethod
    def _failure_report(issues: tuple[ValidationIssue, ...]) -> str:
        lines = [
            "ATLAS Ciena RLS R4.0 Exact Provider Validation",
            "=" * 50,
            "RESULT: FAILED — no configuration was generated",
            "",
        ]
        for issue in issues:
            lines.append(
                f"{issue.severity.upper()} [{issue.code}] {issue.field}: "
                f"{issue.message}"
            )
            if issue.source:
                lines.append(f"  Source: {issue.source}")
        return "\n".join(lines).rstrip() + "\n"

    def _show_input_error(self, message: str) -> None:
        self._preview_type_var.set("Validation report")
        self._set_preview_text(
            "ATLAS Ciena RLS R4.0 Exact Provider Validation\n"
            + "=" * 50
            + "\nRESULT: FAILED — no configuration was generated\n\n"
            + f"INPUT ERROR: {message}\n"
        )
        self._notebook.select(self._preview_tab)
        self._status_var.set("Validation failed — correct the input and try again.")
        _log_r40_event(
            self,
            f"[RLS R4.0 CONFIG] Input validation failed: {message}",
            logging.WARNING,
        )

    def _generate_current(self) -> R40ConfigArtifact | None:
        _log_r40_event(
            self, "[RLS R4.0 CONFIG] Validating current exact-provider inputs."
        )
        self._restore_exact_preview_choices()
        try:
            request = self._build_request()
        except ValueError as exc:
            self._show_input_error(str(exc))
            return None
        issues = self._generator.validate(request)
        errors = tuple(issue for issue in issues if issue.severity == "error")
        if errors:
            self._artifact = None
            self._apply_button.config(state=tk.DISABLED)
            self._copy_button.config(state=tk.DISABLED)
            self._preview_type_var.set("Validation report")
            self._set_preview_text(self._failure_report(issues))
            self._notebook.select(self._preview_tab)
            self._status_var.set("Validation blocked — no CLI was generated.")
            _log_r40_event(
                self,
                "[RLS R4.0 CONFIG] Exact provider validation blocked "
                "generation: "
                f"codes={','.join(sorted({item.code for item in errors}))}; "
                "fields="
                + "|".join(
                    f"{item.code}:{item.field}"
                    for item in errors
                )
                + ".",
                logging.WARNING,
            )
            return None
        try:
            artifact = self._generator.generate(request)
        except ConfigValidationError as exc:
            self._show_input_error(str(exc))
            return None
        except Exception as exc:
            logging.exception("R4.0 exact provider generation failed")
            messagebox.showerror(
                "RLS R4.0 Config Generator",
                friendly_error(exc, "Configuration generation failed."),
                parent=self,
            )
            return None
        self._artifact = artifact
        self._apply_button.config(state=tk.NORMAL)
        self._copy_button.config(state=tk.NORMAL)
        self._refresh_preview_text()
        self._notebook.select(self._preview_tab)
        warning_count = sum(
            issue.severity == "warning" for issue in artifact.issues
        )
        self._status_var.set(
            f"Valid candidate: {artifact.command_count} commands, "
            f"{warning_count} warning(s)."
        )
        _log_r40_event(
            self,
            f"[RLS R4.0 CONFIG] Validated {request.shelf_name!r} with "
            f"provider {request.provider_id}: {artifact.command_count} commands; "
            f"warnings={warning_count}; warning_fields="
            + (
                "|".join(
                    f"{issue.code}:{issue.field}"
                    for issue in artifact.issues
                    if issue.severity == "warning"
                )
                or "none"
            )
            + ".",
        )
        return artifact

    def _preview(self) -> None:
        _log_r40_event(self, "[RLS R4.0 CONFIG] Preview requested.")
        profile = self._selected_profile()
        blockers = _planning_preview_blockers(
            profile,
            self._role_profile,
            self._target_build_var.get(),
            self._colan_state_var.get(),
        )
        if blockers:
            self._show_planning_preview(profile, blockers)
            return
        self._generate_current()

    def _apply_reviewed(self) -> None:
        artifact = self._generate_current()
        if artifact is None:
            return
        try:
            self._on_apply(artifact.request, artifact)
        except Exception as exc:
            logging.exception("Could not apply R4.0 exact request to route")
            messagebox.showerror(
                "Could not apply configuration",
                friendly_error(
                    exc,
                    "The exact R4.0 configuration could not be applied to the shelf.",
                ),
                parent=self,
            )
            return
        self._dirty = False
        self._status_var.set("Reviewed exact R4.0 request applied to this shelf.")
        _log_r40_event(
            self,
            f"[RLS R4.0 CONFIG] Applied reviewed provider "
            f"{artifact.request.provider_id} for {artifact.request.shelf_name!r}.",
        )

    def has_unapplied_changes(self) -> bool:
        """Return whether closing now would discard edited review inputs."""

        return bool(self._dirty)

    def _refresh_preview_text(self, _event: object = None) -> None:
        if self._planning_preview_text is not None:
            self._set_preview_text(self._planning_preview_text)
            return
        if self._artifact is None:
            return
        choice = self._preview_type_var.get()
        if choice == "Annotated review":
            text = self._artifact.annotated_text
        elif choice == "Validation report":
            text = self._artifact.validation_report
        else:
            text = self._artifact.cli_text
        self._set_preview_text(text)

    def _set_preview_text(self, text: str) -> None:
        self._preview_text.config(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        self._preview_text.insert("1.0", text)
        self._preview_text.config(state=tk.DISABLED)

    def _copy_preview(self) -> None:
        if self._artifact is None:
            return
        self._refresh_preview_text()
        text = self._preview_text.get("1.0", tk.END).rstrip() + "\n"
        self.clipboard_clear()
        self.clipboard_append(text)
        _log_r40_event(
            self,
            f"[RLS R4.0 CONFIG] Copied {self._preview_type_var.get()} preview.",
        )


__all__ = ["RlsR40ConfigFrame"]
