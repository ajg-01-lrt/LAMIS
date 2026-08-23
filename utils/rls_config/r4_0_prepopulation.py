"""Review-only prepopulation contracts for the audited RLS R4.0 workflow.

The historical spreadsheet is useful as an inventory of operator-facing
fields, but its formulas and command output are not a controlled CLI source.
This module records that field contract and provides a fail-closed mapping from
directly observed local line-output ports to an audited provider's one or two
fixed line records. RLA records are physical bidirectional degrees; DLE
records are amplifier egress paths. Neither helper creates an exact request or
authorizes CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from .r4_0_generator import R40ProviderProfile


LEGACY_WORKBOOK_SHA256 = (
    "c07c2efecad3e6bb312e6825a3216845a53ecfc330ee37b1707873efd4eb5905"
)
LEGACY_WORKBOOK_NAME = "Ciena RLS C+L CLI Config v3.5LR.xlsx"
MIN_DIRECTION_EVIDENCE_CONFIDENCE = 0.85

DirectionResolutionStatus = Literal[
    "exact_match",
    "controlled_fallback",
    "missing_evidence",
    "ambiguous",
    "conflict",
]


@dataclass(frozen=True)
class LegacyWorkbookInput:
    """One literal ``<...>`` input cell from the quarantined workbook."""

    cell: str
    label: str
    placeholder: str
    request_field: str
    handling: Literal[
        "editable",
        "terminal_colan",
        "documentation_only",
        "unsupported_provider_feature",
    ] = "editable"


@dataclass(frozen=True)
class LegacyWorkbookContract:
    worksheet: str
    inputs: tuple[LegacyWorkbookInput, ...]
    fixed_defaults: tuple[str, ...]


@dataclass(frozen=True)
class R40DirectionResolution:
    """Non-executable result of matching visible ports to fixed line records."""

    status: DirectionResolutionStatus
    line_1_route_side: str
    reason_codes: tuple[str, ...]
    explanation: str
    matched_endpoints: tuple[Mapping[str, object], ...] = ()

    @property
    def exact(self) -> bool:
        return self.status == "exact_match" and self.line_1_route_side in {
            "A",
            "Z",
        }

    @property
    def resolved(self) -> bool:
        return self.status in {
            "exact_match",
            "controlled_fallback",
        } and self.line_1_route_side in {"A", "Z"}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "line_1_route_side": self.line_1_route_side,
            "reason_codes": list(self.reason_codes),
            "explanation": self.explanation,
            "matched_endpoints": [dict(item) for item in self.matched_endpoints],
            "deployable_cli": False,
        }


_AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACKS = {
    ("cda_rla12_c", "add_drop_a"): "Z",
    ("cda_rla12_c", "add_drop_z"): "A",
    ("cdc_roadm_rla32_c", "roadm_a"): "Z",
    ("cdc_roadm_rla32_c", "roadm_z"): "A",
    ("roadm_rla12_cl_lru12_core", "roadm_a"): "Z",
    ("roadm_rla12_cl_lru12_core", "roadm_z"): "A",
    ("ila_dle_cl", "ila"): "Z",
}


def resolve_r40_fixed_direction_fallback(
    profile: R40ProviderProfile,
    role_profile: str,
) -> R40DirectionResolution:
    """Return the audited provider/route-role line-side convention, if any.

    This fallback is deliberately weaker than direct slot/port resolution. It
    exists so a uniquely preselected exact provider can expose the correct
    route-side seed when vision omitted local endpoint observations. Callers
    must not use it after an ambiguous or conflicting endpoint result.
    """

    if not isinstance(profile, R40ProviderProfile):
        raise TypeError("profile must be an R40ProviderProfile")
    if not isinstance(role_profile, str):
        raise TypeError("role_profile must be a string")
    role = role_profile.strip()
    if role not in profile.role_profiles:
        return R40DirectionResolution(
            "missing_evidence",
            "",
            ("PROVIDER_ROLE_DIRECTION_FALLBACK_NOT_APPLICABLE",),
            (
                "The selected route role is not compatible with this audited "
                "provider, so no fixed line-side convention was applied."
            ),
        )
    side = _AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACKS.get(
        (profile.application, role),
        "",
    )
    if not side:
        return R40DirectionResolution(
            "missing_evidence",
            "",
            ("AUDITED_PROVIDER_ROLE_DIRECTION_NOT_DEFINED",),
            (
                "No audited fixed line-side convention is defined for this "
                "provider and route role."
            ),
        )
    return R40DirectionResolution(
        "controlled_fallback",
        side,
        ("AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK",),
        (
            f"The audited {profile.application} provider convention for "
            f"{role} maps the first fixed local line-output to route "
            f"{side}-side because "
            "direct local endpoint evidence was unavailable. Confirm this "
            "non-executable mapping against the installed port map."
        ),
    )


_COMMON_TERMINAL_AFTER_IDENTITY = (
    LegacyWorkbookInput(
        "B4",
        "Location",
        "<rack location>",
        "frame_identification_code",
    ),
    LegacyWorkbookInput(
        "B5",
        "Loopback IP",
        "<loopback ip address>",
        "loopback_ip",
    ),
    LegacyWorkbookInput(
        "B10",
        "COLAN-X IP",
        "<colan-x ip address>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B11",
        "COLAN-X mask",
        "<colan-x mask>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B12",
        "COLAN-X gateway",
        "<colan-x gateway>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B15",
        "COLAN-A IP",
        "<colan-a ip address>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B16",
        "COLAN-A mask",
        "<colan-a mask>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B17",
        "COLAN-A gateway",
        "<colan-a gateway>",
        "management",
        "terminal_colan",
    ),
    LegacyWorkbookInput(
        "B18",
        "Line 1 Neighbor",
        "<name.bb.net.apple.com>",
        "route_neighbor",
    ),
)

_TERMINAL_FIXED_DEFAULTS = (
    "Loopback prefix /32",
    "OSPF routing model unless the customer specifies static terminal COLAN",
    "Terminal COLAN optional; omit the complete COLAN block when deferred",
    "Input/output patch-panel loss 0.5 dB",
    "Repair margin 2 dB",
    "High-loss minor threshold 3 dB",
    "NTP omitted because it is customer-managed",
)

_ROADM_WSS_INPUTS = tuple(
    LegacyWorkbookInput(
        f"B{28 + number}",
        f"SW{number} channel neighbor",
        "<neighbor name>",
        f"legacy_wss_neighbor_{number}",
        "unsupported_provider_feature",
    )
    for number in range(1, 13)
)

LEGACY_WORKBOOK_CONTRACTS: Mapping[str, LegacyWorkbookContract] = {
    "add_drop_a": LegacyWorkbookContract(
        "AddDrop_A",
        (
            LegacyWorkbookInput(
                "B3",
                "Shelf Name",
                "<add/drop_name>",
                "shelf_name",
            ),
            *_COMMON_TERMINAL_AFTER_IDENTITY,
            LegacyWorkbookInput(
                "B245",
                "First ILA ping target",
                "<insert first ILAs IP>",
                "legacy_first_ila_ping_target",
                "documentation_only",
            ),
        ),
        (
            *_TERMINAL_FIXED_DEFAULTS,
            "Legacy B7 OSPF value 10.6.6.0 is superseded by direct route-header evidence",
        ),
    ),
    "add_drop_z": LegacyWorkbookContract(
        "AddDrop_Z",
        (
            LegacyWorkbookInput(
                "B3",
                "Shelf Name",
                "<roadmz name>",
                "shelf_name",
            ),
            *_COMMON_TERMINAL_AFTER_IDENTITY[:2],
            LegacyWorkbookInput("B7", "OSPF Area ID", "<ospf area>", "ospf_area"),
            *_COMMON_TERMINAL_AFTER_IDENTITY[2:],
            LegacyWorkbookInput(
                "B245",
                "First ILA ping target",
                "<insert first ILAs IP>",
                "legacy_first_ila_ping_target",
                "documentation_only",
            ),
        ),
        _TERMINAL_FIXED_DEFAULTS,
    ),
    "ila": LegacyWorkbookContract(
        "ILA 1",
        (
            LegacyWorkbookInput("B3", "Shelf Name", "<ila1 name>", "shelf_name"),
            LegacyWorkbookInput(
                "B4",
                "Location",
                "<rack location>",
                "frame_identification_code",
            ),
            LegacyWorkbookInput(
                "B5",
                "Loopback IP",
                "<loopback ip address>",
                "loopback_ip",
            ),
            LegacyWorkbookInput("B7", "OSPF Area ID", "<ospf area>", "ospf_area"),
            LegacyWorkbookInput(
                "B8",
                "A-facing Neighbor",
                "<name.bb.net.apple.com>",
                "a_route_neighbor",
            ),
            LegacyWorkbookInput(
                "B17",
                "Z-facing Neighbor",
                "<name.bb.net.apple.com>",
                "z_route_neighbor",
            ),
            LegacyWorkbookInput(
                "B215",
                "First ILA ping target",
                "<insert first ILAs IP>",
                "legacy_first_ila_ping_target",
                "documentation_only",
            ),
        ),
        (
            "Loopback prefix /32",
            "No COLAN",
            "OSPF over loopback/OSC",
            "Repair margin 2 dB",
            "High-loss minor threshold 3 dB",
            "NTP omitted because it is customer-managed",
        ),
    ),
    "roadm_a": LegacyWorkbookContract(
        "ROADM_A",
        (
            LegacyWorkbookInput(
                "B3",
                "Shelf Name",
                "<oxa name>",
                "shelf_name",
            ),
            *_COMMON_TERMINAL_AFTER_IDENTITY[:2],
            LegacyWorkbookInput("B7", "OSPF Area ID", "<ospf area>", "ospf_area"),
            *_COMMON_TERMINAL_AFTER_IDENTITY[2:],
            *_ROADM_WSS_INPUTS,
            LegacyWorkbookInput(
                "B284",
                "First ILA ping target",
                "<insert first ILAs IP>",
                "legacy_first_ila_ping_target",
                "documentation_only",
            ),
        ),
        _TERMINAL_FIXED_DEFAULTS,
    ),
    "roadm_z": LegacyWorkbookContract(
        "ROADM_Z",
        (
            LegacyWorkbookInput(
                "B3",
                "Shelf Name",
                "<oxz name>",
                "shelf_name",
            ),
            *_COMMON_TERMINAL_AFTER_IDENTITY[:2],
            LegacyWorkbookInput("B7", "OSPF Area ID", "<ospf area>", "ospf_area"),
            *_COMMON_TERMINAL_AFTER_IDENTITY[2:],
            *_ROADM_WSS_INPUTS,
            LegacyWorkbookInput(
                "B284",
                "First ILA ping target",
                "<insert first ILAs IP>",
                "legacy_first_ila_ping_target",
                "documentation_only",
            ),
        ),
        _TERMINAL_FIXED_DEFAULTS,
    ),
    "tx_proxy_power_profile": LegacyWorkbookContract(
        "tx-proxy_pwr-profile",
        (
            LegacyWorkbookInput(
                "B3",
                "Proxy FQDN",
                "<fqdn.bb.net.apple.com>",
                "legacy_tx_proxy_fqdn",
                "unsupported_provider_feature",
            ),
        ),
        (
            "Historical proxy/power-profile commands are outside the current "
            "audited exact-provider request",
        ),
    ),
}


def legacy_workbook_contract_for_role(
    role_profile: str,
) -> LegacyWorkbookContract | None:
    """Return the audited historical input contract for one route role."""

    normalized = str(role_profile or "").strip()
    normalized = {
        "add_drop": "add_drop_a",
        "roadm": "roadm_a",
    }.get(normalized, normalized)
    return LEGACY_WORKBOOK_CONTRACTS.get(normalized)


def legacy_workbook_contract_text(role_profile: str) -> str:
    """Render the contract without exposing workbook credentials or formulas."""

    contract = legacy_workbook_contract_for_role(role_profile)
    if contract is None:
        return "No historical workbook field contract applies to this role."

    def render_input(item: LegacyWorkbookInput) -> str:
        return (
            f"{item.cell} {item.label} — {item.placeholder} → "
            f"{item.request_field} [{item.handling}]"
        )

    editable = [
        render_input(item)
        for item in contract.inputs
        if item.handling in {"editable", "terminal_colan"}
    ]
    excluded = [
        render_input(item)
        for item in contract.inputs
        if item.handling not in {"editable", "terminal_colan"}
    ]
    lines = [
        f"Historical input worksheet: {contract.worksheet}",
        "Operator fields: " + ", ".join(editable),
        "Controlled fixed/default fields: " + "; ".join(contract.fixed_defaults),
    ]
    if excluded:
        lines.append(
            "Visible historical fields not emitted by the selected audited "
            "provider: " + ", ".join(excluded)
        )
    terminal_colan = [
        item for item in contract.inputs if item.handling == "terminal_colan"
    ]
    if terminal_colan:
        lines.append(
            "Terminal COLAN normalization: legacy COLAN-X B10:B12 and COLAN-A "
            "B15:B17 are alternative interface records. The exact provider "
            "review may either omit the complete optional COLAN block for "
            "factory staging or expose one selected customer-provided "
            "interface (colan-x or colan-a); it never emits both legacy blocks "
            "and never accepts a partial one."
        )
    global_contract = LEGACY_WORKBOOK_CONTRACTS[
        "tx_proxy_power_profile"
    ]
    if contract is not global_contract:
        lines.append(
            "Additional workbook placeholder outside the shelf request: "
            + ", ".join(
                render_input(item)
                for item in global_contract.inputs
            )
            + " (not emitted by the audited exact providers)."
        )
    lines.append(
        "Precedence: direct diagram evidence, then audited provider/workflow "
        "default, then operator entry. Spreadsheet formulas are never executed."
    )
    return "\n".join(lines)


def _evidence_supports(
    raw_evidence: object,
    *,
    field_suffix: str,
    value: object,
    direct: bool,
) -> bool:
    if not isinstance(raw_evidence, (list, tuple)):
        return False
    for raw in raw_evidence:
        if not isinstance(raw, Mapping):
            continue
        field_name = str(raw.get("field", "") or "")
        if not field_name.endswith(field_suffix):
            continue
        if direct and raw.get("method") not in {"vision", "ocr", "native_text"}:
            continue
        if not direct and raw.get("method") not in {
            "vision",
            "ocr",
            "native_text",
            "inferred",
        }:
            continue
        confidence = raw.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or float(confidence) < MIN_DIRECTION_EVIDENCE_CONFIDENCE
        ):
            continue
        normalized = raw.get("normalized_value")
        if str(normalized).strip().casefold() == str(value).strip().casefold():
            return True
    return False


def resolve_r40_fixed_direction(
    profile: R40ProviderProfile,
    line_endpoints: object,
) -> R40DirectionResolution:
    """Map direct local line-out evidence onto provider line record 1.

    A route-relative adjacency supplies A/Z, while the provider's immutable
    ``line_outputs`` tuple supplies the provider's one or two fixed records.
    A port is used only when its value has high-confidence direct evidence.
    If a slot is absent, the port number must uniquely identify one provider
    record.
    """

    if not isinstance(profile, R40ProviderProfile):
        raise TypeError("profile must be an R40ProviderProfile")
    if not isinstance(line_endpoints, (list, tuple)) or not line_endpoints:
        return R40DirectionResolution(
            "missing_evidence",
            "",
            ("MISSING_LINE_ENDPOINT_EVIDENCE",),
            "No reviewed local line-endpoint observations are available.",
        )

    derived_sides: set[str] = set()
    matched: list[Mapping[str, object]] = []
    provider_mismatches: list[Mapping[str, object]] = []
    ambiguous_count = 0
    for raw in line_endpoints:
        if not isinstance(raw, Mapping):
            continue
        adjacency = str(raw.get("adjacency", "") or "").strip()
        if adjacency not in {"preceding", "following"}:
            continue
        evidence = raw.get("evidence", ())
        if not _evidence_supports(
            evidence,
            field_suffix=".adjacency",
            value=adjacency,
            direct=False,
        ):
            continue
        output = raw.get("line_out_port")
        if (
            not isinstance(output, int)
            or isinstance(output, bool)
            or not _evidence_supports(
                evidence,
                field_suffix=".line_out_port",
                value=output,
                direct=True,
            )
        ):
            continue
        slot = raw.get("slot")
        slot_supported = (
            isinstance(slot, int)
            and not isinstance(slot, bool)
            and _evidence_supports(
                evidence,
                field_suffix=".slot",
                value=slot,
                direct=True,
            )
        )
        candidates = [
            number
            for number, (provider_slot, provider_output) in enumerate(
                profile.line_outputs,
                start=1,
            )
            if provider_output == output
            and (not slot_supported or provider_slot == slot)
        ]
        if not candidates:
            provider_mismatches.append(
                {
                    "adjacency": adjacency,
                    "route_side": (
                        "A" if adjacency == "preceding" else "Z"
                    ),
                    "slot": slot if slot_supported else None,
                    "line_out_port": output,
                }
            )
            continue
        if len(candidates) != 1:
            ambiguous_count += 1
            continue
        fixed_direction = candidates[0]
        route_side = "A" if adjacency == "preceding" else "Z"
        line_1_side = (
            route_side
            if fixed_direction == 1
            else ("Z" if route_side == "A" else "A")
        )
        derived_sides.add(line_1_side)
        matched.append(
            {
                "adjacency": adjacency,
                "route_side": route_side,
                "slot": slot if slot_supported else None,
                "line_out_port": output,
                "matched_fixed_direction": fixed_direction,
                "derived_line_1_route_side": line_1_side,
            }
        )

    if provider_mismatches:
        return R40DirectionResolution(
            "conflict",
            "",
            ("DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH",),
            (
                "At least one directly observed local line-output slot/port "
                "does not exist in this provider's immutable line-output map."
            ),
            tuple((*matched, *provider_mismatches)),
        )
    if len(derived_sides) > 1:
        return R40DirectionResolution(
            "conflict",
            "",
            ("CONFLICTING_LINE_ENDPOINT_EVIDENCE",),
            (
                "Direct endpoint observations map provider line record 1 to "
                "different route sides."
            ),
            tuple(matched),
        )
    if len(derived_sides) == 1:
        side = next(iter(derived_sides))
        return R40DirectionResolution(
            "exact_match",
            side,
            ("DIRECT_LINE_OUTPUT_MATCH",),
            (
                f"Direct local line-output evidence uniquely maps fixed line "
                f"record 1 to route {side}-side."
            ),
            tuple(matched),
        )
    if ambiguous_count:
        return R40DirectionResolution(
            "ambiguous",
            "",
            ("AMBIGUOUS_LINE_OUTPUT_MATCH",),
            (
                "The visible output port matches more than one fixed provider "
                "record; a directly observed slot/port discriminator is "
                "required."
            ),
        )
    return R40DirectionResolution(
        "missing_evidence",
        "",
        ("MISSING_DIRECT_LINE_OUTPUT_MATCH",),
        (
            "No high-confidence direct local line-out observation uniquely "
            "matches this provider's fixed line-output map."
        ),
    )


__all__ = [
    "LEGACY_WORKBOOK_CONTRACTS",
    "LEGACY_WORKBOOK_NAME",
    "LEGACY_WORKBOOK_SHA256",
    "LegacyWorkbookContract",
    "LegacyWorkbookInput",
    "R40DirectionResolution",
    "legacy_workbook_contract_for_role",
    "legacy_workbook_contract_text",
    "resolve_r40_fixed_direction",
    "resolve_r40_fixed_direction_fallback",
]
