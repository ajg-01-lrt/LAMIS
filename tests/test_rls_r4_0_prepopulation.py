"""Review-only RLS R4.0 workbook and direction prepopulation contracts."""

from __future__ import annotations

import re

from utils import rls_config as public_api
from utils.rls_config.r4_0_generator import (
    R40_CDA_RLA12_C_2DEG_NO_SRA,
    R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
    R40_R2_CL_DLE_S1_NO_SRA,
    R40_PROVIDER_CATALOG,
)
from utils.rls_config.r4_0_prepopulation import (
    LEGACY_WORKBOOK_CONTRACTS,
    LEGACY_WORKBOOK_NAME,
    LEGACY_WORKBOOK_SHA256,
    legacy_workbook_contract_for_role,
    legacy_workbook_contract_text,
    resolve_r40_fixed_direction,
    resolve_r40_fixed_direction_fallback,
)


def _evidence(
    endpoint: int,
    leaf: str,
    value: object,
    *,
    method: str,
    confidence: float = 0.99,
) -> dict[str, object]:
    return {
        "field": f"line_endpoints.{endpoint}.{leaf}",
        "raw_text": str(value),
        "normalized_value": value,
        "confidence": confidence,
        "method": method,
    }


def _line_endpoint(
    endpoint: int,
    *,
    adjacency: str,
    line_out_port: int,
    slot: int | None = 1,
    output_method: str = "vision",
    confidence: float = 0.99,
) -> dict[str, object]:
    evidence = [
        _evidence(
            endpoint,
            "adjacency",
            adjacency,
            method="inferred",
            confidence=confidence,
        ),
        _evidence(
            endpoint,
            "line_out_port",
            line_out_port,
            method=output_method,
            confidence=confidence,
        ),
    ]
    if slot is not None:
        evidence.append(
            _evidence(
                endpoint,
                "slot",
                slot,
                method="vision",
                confidence=confidence,
            )
        )
    return {
        "adjacency": adjacency,
        "slot": slot,
        "line_out_port": line_out_port,
        "evidence": evidence,
    }


def test_public_api_exports_provider_and_prepopulation_contracts() -> None:
    for name in (
        "R40DirectModuleFact",
        "R40ProviderCandidateFacts",
        "R40ProviderCandidateResolution",
        "R40ProviderCandidateStatus",
        "resolve_r40_provider_candidate",
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
    ):
        assert hasattr(public_api, name), name
        assert name in public_api.__all__


def test_legacy_workbook_contract_preserves_only_angle_bracket_inputs() -> None:
    assert LEGACY_WORKBOOK_NAME == "Ciena RLS C+L CLI Config v3.5LR.xlsx"
    assert re.fullmatch(r"[0-9a-f]{64}", LEGACY_WORKBOOK_SHA256)
    assert set(LEGACY_WORKBOOK_CONTRACTS) == {
        "add_drop_a",
        "add_drop_z",
        "ila",
        "roadm_a",
        "roadm_z",
        "tx_proxy_power_profile",
    }

    for role, contract in LEGACY_WORKBOOK_CONTRACTS.items():
        assert contract.worksheet
        assert contract.inputs
        assert contract.fixed_defaults
        assert all(
            item.placeholder.startswith("<")
            and item.placeholder.endswith(">")
            for item in contract.inputs
        ), role
        assert all(item.cell.startswith("B") for item in contract.inputs), role
        assert all(
            item.handling
            in {
                "editable",
                "terminal_colan",
                "documentation_only",
                "unsupported_provider_feature",
            }
            for item in contract.inputs
        ), role


def test_legacy_workbook_contract_has_the_complete_audited_cell_inventory() -> None:
    expected_cells = {
        "add_drop_a": {
            "B3",
            "B4",
            "B5",
            "B10",
            "B11",
            "B12",
            "B15",
            "B16",
            "B17",
            "B18",
            "B245",
        },
        "add_drop_z": {
            "B3",
            "B4",
            "B5",
            "B7",
            "B10",
            "B11",
            "B12",
            "B15",
            "B16",
            "B17",
            "B18",
            "B245",
        },
        "ila": {"B3", "B4", "B5", "B7", "B8", "B17", "B215"},
        "roadm_a": {
            "B3",
            "B4",
            "B5",
            "B7",
            "B10",
            "B11",
            "B12",
            "B15",
            "B16",
            "B17",
            "B18",
            "B284",
            *(f"B{number}" for number in range(29, 41)),
        },
        "roadm_z": {
            "B3",
            "B4",
            "B5",
            "B7",
            "B10",
            "B11",
            "B12",
            "B15",
            "B16",
            "B17",
            "B18",
            "B284",
            *(f"B{number}" for number in range(29, 41)),
        },
        "tx_proxy_power_profile": {"B3"},
    }

    assert sum(len(values) for values in expected_cells.values()) == 79
    assert {
        role: {item.cell for item in contract.inputs}
        for role, contract in LEGACY_WORKBOOK_CONTRACTS.items()
    } == expected_cells


def test_legacy_contract_enforces_ila_and_terminal_policy_boundaries() -> None:
    ila = legacy_workbook_contract_for_role("ila")
    roadm = legacy_workbook_contract_for_role("roadm_a")

    assert ila is not None
    assert roadm is not None
    assert ila.worksheet == "ILA 1"
    assert {item.cell for item in ila.inputs} == {
        "B3",
        "B4",
        "B5",
        "B7",
        "B8",
        "B17",
        "B215",
    }
    assert not [
        item for item in ila.inputs if item.handling == "terminal_colan"
    ]
    assert "No COLAN" in ila.fixed_defaults
    assert any("NTP omitted" in value for value in ila.fixed_defaults)

    assert [
        item for item in roadm.inputs if item.handling == "terminal_colan"
    ]
    unsupported = [
        item
        for item in roadm.inputs
        if item.handling == "unsupported_provider_feature"
    ]
    assert len(unsupported) == 12
    assert [item.cell for item in unsupported] == [
        f"B{number}" for number in range(29, 41)
    ]
    assert legacy_workbook_contract_for_role("roadm") is roadm

    rendered = legacy_workbook_contract_text("roadm_a")
    assert "Spreadsheet formulas are never executed" in rendered
    assert "Visible historical fields not emitted" in rendered
    assert "B29 SW1 channel neighbor" in rendered
    assert "B284 First ILA ping target" in rendered
    assert "<neighbor name>" in rendered
    assert "Proxy FQDN" in rendered
    assert "legacy_wss_neighbor_1 [unsupported_provider_feature]" in rendered
    assert "Terminal COLAN normalization" in rendered


def test_direct_ila_a_z_outputs_resolve_fixed_direction_one_to_z() -> None:
    profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    result = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="preceding",
                line_out_port=53,
            ),
            _line_endpoint(
                1,
                adjacency="following",
                line_out_port=63,
            ),
        ),
    )

    assert result.status == "exact_match"
    assert result.exact is True
    assert result.line_1_route_side == "Z"
    assert result.reason_codes == ("DIRECT_LINE_OUTPUT_MATCH",)
    assert {
        (
            item["route_side"],
            item["matched_fixed_direction"],
            item["derived_line_1_route_side"],
        )
        for item in result.matched_endpoints
    } == {
        ("A", 2, "Z"),
        ("Z", 1, "Z"),
    }
    serialized = result.to_dict()
    assert serialized["deployable_cli"] is False
    assert "cli" not in serialized
    assert "payload" not in serialized


def test_shared_roadm_output_without_direct_slot_is_ambiguous() -> None:
    profile = R40_PROVIDER_CATALOG[
        R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA
    ]
    result = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="following",
                line_out_port=53,
                slot=None,
            ),
        ),
    )

    assert result.status == "ambiguous"
    assert result.exact is False
    assert result.line_1_route_side == ""
    assert result.reason_codes == ("AMBIGUOUS_LINE_OUTPUT_MATCH",)
    assert result.to_dict()["deployable_cli"] is False


def test_conflicting_direct_ila_endpoints_fail_closed() -> None:
    profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    result = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="preceding",
                line_out_port=63,
            ),
            _line_endpoint(
                1,
                adjacency="following",
                line_out_port=63,
            ),
        ),
    )

    assert result.status == "conflict"
    assert result.exact is False
    assert result.line_1_route_side == ""
    assert result.reason_codes == ("CONFLICTING_LINE_ENDPOINT_EVIDENCE",)
    assert len(result.matched_endpoints) == 2
    assert result.to_dict()["deployable_cli"] is False


def test_one_direct_provider_port_mismatch_overrides_an_otherwise_valid_match() -> None:
    profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    result = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="preceding",
                line_out_port=53,
                slot=3,
            ),
            _line_endpoint(
                1,
                adjacency="following",
                line_out_port=63,
                slot=1,
            ),
        ),
    )

    assert result.status == "conflict"
    assert result.exact is False
    assert result.line_1_route_side == ""
    assert result.reason_codes == ("DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH",)
    assert result.to_dict()["deployable_cli"] is False


def test_inferred_or_low_confidence_line_output_never_resolves_direction() -> None:
    profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    inferred = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="following",
                line_out_port=63,
                output_method="inferred",
            ),
        ),
    )
    low_confidence = resolve_r40_fixed_direction(
        profile,
        (
            _line_endpoint(
                0,
                adjacency="following",
                line_out_port=63,
                confidence=0.84,
            ),
        ),
    )

    assert inferred.status == "missing_evidence"
    assert low_confidence.status == "missing_evidence"
    assert inferred.line_1_route_side == low_confidence.line_1_route_side == ""
    assert inferred.to_dict()["deployable_cli"] is False
    assert low_confidence.to_dict()["deployable_cli"] is False


def test_audited_provider_role_direction_fallbacks_are_non_executable() -> None:
    cases = (
        (R40_CDA_RLA12_C_2DEG_NO_SRA, "add_drop_a", "Z"),
        (R40_CDA_RLA12_C_2DEG_NO_SRA, "add_drop_z", "A"),
        (
            R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
            "roadm_a",
            "Z",
        ),
        (
            R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
            "roadm_z",
            "A",
        ),
        (
            R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
            "roadm_a",
            "Z",
        ),
        (
            R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
            "roadm_z",
            "A",
        ),
        (R40_R2_CL_DLE_S1_NO_SRA, "ila", "Z"),
    )

    for provider_id, role_profile, expected_side in cases:
        result = resolve_r40_fixed_direction_fallback(
            R40_PROVIDER_CATALOG[provider_id],
            role_profile,
        )
        assert result.status == "controlled_fallback"
        assert result.exact is False
        assert result.resolved is True
        assert result.line_1_route_side == expected_side
        assert result.reason_codes == (
            "AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK",
        )
        assert result.to_dict()["deployable_cli"] is False


def test_direction_fallback_does_not_guess_for_generic_or_wrong_role() -> None:
    profile = R40_PROVIDER_CATALOG[
        R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA
    ]

    generic = resolve_r40_fixed_direction_fallback(profile, "roadm")
    incompatible = resolve_r40_fixed_direction_fallback(profile, "ila")

    assert generic.status == "missing_evidence"
    assert generic.line_1_route_side == ""
    assert generic.reason_codes == (
        "AUDITED_PROVIDER_ROLE_DIRECTION_NOT_DEFINED",
    )
    assert incompatible.status == "missing_evidence"
    assert incompatible.line_1_route_side == ""
    assert incompatible.reason_codes == (
        "PROVIDER_ROLE_DIRECTION_FALLBACK_NOT_APPLICABLE",
    )
