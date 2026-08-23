"""Tests for the non-executable RLS R4.0 workbook-assumption catalog."""

from __future__ import annotations

import inspect
import math

import pytest

import utils.rls_config as public_api
from utils.rls_config import r4_0_review
from utils.rls_config.r4_0_review import (
    R40ReviewError,
    R40_REVIEW_CATALOG,
    R40_REVIEW_RELEASE,
    build_r4_0_review,
    get_r4_0_review_profile,
    render_r4_0_review_report,
)


def _a_endpoint_facts() -> dict[str, object]:
    return {
        "route_id": "RL-0037805",
        "tid": "USELP1-L8R2",
        "site_name": "El Paso, TX",
        "site_code": "USELP1",
        "primary_oam_ip": "10.6.22.129",
        "ospf_area": "10.6.8.0",
        "chassis": "R4 600mm",
        "shelf_variant": "C+L",
        "lifecycle": "active",
        "z_neighbor_tid": "USQTN1-L8I2",
        "z_neighbor_role": "ILA",
        "z_span_fiber_type": "LEAF",
        "z_span_distance_km": 62.47,
        "z_span_loss_db": 14.55,
        "z_span_circuit_id": "BDJW7353",
        "z_span_fiber_start": 3,
        "z_span_fiber_end": 4,
    }


def _ila_facts() -> dict[str, object]:
    facts = {
        "tid": "USQTN1-L8I2",
        "site_name": "Tornillo, TX",
        "primary_oam_ip": "10.6.22.132",
        "ospf_area": "10.6.8.0",
        "chassis": "R2 600mm",
        "a_neighbor_tid": "USELP1-L8R2",
        "a_neighbor_role": "ROADM",
        "a_span_fiber_type": "LEAF",
        "a_span_distance_km": 62.47,
        "a_span_loss_db": 14.55,
        "z_neighbor_tid": "USIEB1-L8I2",
        "z_neighbor_role": "ILA",
        "z_span_fiber_type": "LEAF",
        "z_span_distance_km": 66.15,
        "z_span_loss_db": 16.1,
    }
    return facts


def _fixed(profile_id: str) -> dict[str, str]:
    return {
        item.key: item.value
        for item in R40_REVIEW_CATALOG[profile_id].fixed_assumptions
    }


def test_catalog_is_exactly_the_five_legacy_review_profiles():
    assert tuple(R40_REVIEW_CATALOG) == (
        "add_drop_a",
        "add_drop_z",
        "ila",
        "roadm_a",
        "roadm_z",
    )
    assert {
        profile.source_sheet for profile in R40_REVIEW_CATALOG.values()
    } == {"AddDrop_A", "AddDrop_Z", "ILA 1", "ROADM_A", "ROADM_Z"}

    for profile in R40_REVIEW_CATALOG.values():
        assert profile.provider_available is False
        assert profile.deployment_approved is False
        assert profile.fixed_assumptions
        assert profile.unsafe_assumptions
        assert profile.diagram_required_fields
        assert profile.operator_required_fields
        assert all(item.vendor_default is False for item in profile.fixed_assumptions)
        assert all(item.deployable is False for item in profile.fixed_assumptions)


def test_catalog_records_exact_fixed_workbook_hardware_layouts():
    add_drop = _fixed("add_drop_a")
    assert add_drop["12x1_hardware"] == (
        "slot 1 NTK852BC; OSC slot 1/subslot 50 NTK591VQ; "
        "slot 3 NTK852NA; slots 7 and 8 NTK834AA; slot 5 NTK834AE"
    )
    assert add_drop["64x1_hardware"] == (
        "slot 1 NTK862AH; OSC slot 1/subslot 50 NTK591VQ"
    )
    assert add_drop["raman_hardware"] == (
        "NTK830AC in slot 6 when Raman is selected"
    )

    ila = _fixed("ila")
    assert ila["dle_hardware"] == (
        "slot 1 NTK850DC; OSC slot 1/subslots 50 and 60 NTK591VQ"
    )
    assert ila["raman_hardware"] == (
        "NTK830AC in directional slots 3 and/or 4"
    )

    roadm = _fixed("roadm_z")
    assert roadm["wss_half_links"] == (
        "12 C/L pairs on ports 21/22 through 43/44"
    )
    assert roadm["wss_power_profiles"] == (
        "manual target power -8.50 across the fixed WSS profile pattern"
    )


def test_side_profiles_require_only_the_route_facing_span():
    assert "z_neighbor_tid" in get_r4_0_review_profile(
        "add_drop_a"
    ).diagram_required_fields
    assert "a_neighbor_tid" not in get_r4_0_review_profile(
        "add_drop_a"
    ).diagram_required_fields
    assert "a_neighbor_tid" in get_r4_0_review_profile(
        "roadm_z"
    ).diagram_required_fields
    assert "z_neighbor_tid" not in get_r4_0_review_profile(
        "roadm_z"
    ).diagram_required_fields

    ila_fields = get_r4_0_review_profile("ila").diagram_required_fields
    assert "a_neighbor_tid" in ila_fields
    assert "z_neighbor_tid" in ila_fields


def test_operator_review_matches_optional_colan_and_customer_ntp_policy():
    for profile_id in ("add_drop_a", "add_drop_z", "roadm_a", "roadm_z"):
        fields = get_r4_0_review_profile(profile_id).operator_required_fields
        assert not any("colan" in field.casefold() for field in fields)
        assert not any("ntp" in field.casefold() for field in fields)

    ila_fields = get_r4_0_review_profile("ila").operator_required_fields
    assert not any("colan" in field.casefold() for field in ila_fields)
    assert not any("ntp" in field.casefold() for field in ila_fields)


def test_complete_review_is_deterministic_and_never_claims_cli_readiness():
    first = _a_endpoint_facts()
    second = dict(reversed(tuple(first.items())))

    report_1 = render_r4_0_review_report("add_drop_a", first)
    report_2 = render_r4_0_review_report("add_drop_a", second)

    assert report_1 == report_2
    assert "Software release: RLS R4.0" in report_1
    assert "CLI provider available: false" in report_1
    assert "Deployment approved: false" in report_1
    assert "Configuration generated: false" in report_1
    assert "Vendor default: false; deployable: false." in report_1
    assert "Missing required diagram facts\n------------------------------\nNone." in report_1
    assert "REVIEW ONLY" in report_1
    assert "\nbatch\n" not in report_1
    assert "\nset " not in report_1


def test_review_artifact_copies_and_freezes_diagram_facts():
    source = _ila_facts()
    artifact = build_r4_0_review("ila", source)
    source["tid"] = "MUTATED"

    assert artifact.software_release == R40_REVIEW_RELEASE
    assert artifact.provider_available is False
    assert artifact.deployment_approved is False
    assert artifact.diagram_facts["tid"] == "USQTN1-L8I2"
    assert artifact.missing_diagram_fields == ()
    with pytest.raises(TypeError):
        artifact.diagram_facts["tid"] = "MUTATED"  # type: ignore[index]


def test_incomplete_review_lists_missing_diagram_and_operator_evidence():
    artifact = build_r4_0_review(
        "roadm_a",
        {
            "tid": "USELP1-L8R2",
            "site_name": "El Paso, TX",
        },
    )

    assert artifact.missing_diagram_fields == (
        "primary_oam_ip",
        "ospf_area",
        "chassis",
        "z_neighbor_tid",
        "z_neighbor_role",
        "z_span_fiber_type",
        "z_span_distance_km",
        "z_span_loss_db",
    )
    assert "- roadm_rla_degree_add_drop_and_protection_design" in artifact.report_text
    assert "- twelve_wss_neighbor_labels" in artifact.report_text
    assert "- licensed_feature_inventory" in artifact.report_text


@pytest.mark.parametrize(
    "software_release",
    ["", "RLS R4.2", "RLS R4.0.0", "4.0", "R4.0"],
)
def test_review_rejects_every_nonexact_release(software_release):
    with pytest.raises(R40ReviewError, match="exact release"):
        build_r4_0_review(
            "ila",
            _ila_facts(),
            software_release=software_release,
        )


@pytest.mark.parametrize(
    "profile_id",
    ["", "add_drop", "roadm", "protected_dci", "ILA", "ila "],
)
def test_review_rejects_unsupported_or_ambiguous_profiles(profile_id):
    with pytest.raises(R40ReviewError, match="Unsupported"):
        build_r4_0_review(profile_id, {})


@pytest.mark.parametrize(
    "facts",
    [
        {"password": "not-allowed"},
        {"snmp_community": "not-allowed"},
        {"license_key": "not-allowed"},
        {"notes": {"tacacs_secret": "not-allowed"}},
    ],
)
def test_review_rejects_secret_bearing_fields_without_echoing_values(facts):
    with pytest.raises(
        R40ReviewError,
        match="Credential- or secret-bearing",
    ) as caught:
        build_r4_0_review("ila", facts)

    assert "not-allowed" not in str(caught.value)


@pytest.mark.parametrize(
    ("facts", "message"),
    [
        ({"unexpected": "value"}, "Unsupported diagram fact"),
        ({"notes": "unsafe\nline"}, "control characters"),
        ({"a_span_loss_db": math.nan}, "finite"),
        ({"notes": ["not", "scalar"]}, "scalar JSON value"),
        (
            {"notes": "-----BEGIN PRIVATE KEY-----"},
            "private-key material",
        ),
    ],
)
def test_review_rejects_unknown_or_unsafe_fact_values(facts, message):
    with pytest.raises(R40ReviewError, match=message):
        build_r4_0_review("ila", facts)


def test_review_module_has_no_legacy_generator_dependency_or_cli_api():
    source = inspect.getsource(r4_0_review)

    assert "legacy_generator" not in source
    assert "generate_cli" not in source
    assert not hasattr(r4_0_review, "RLSConfigGenerator")


def test_review_apis_are_available_from_the_rls_package():
    assert public_api.R40_REVIEW_CATALOG is R40_REVIEW_CATALOG
    assert public_api.R40_REVIEW_RELEASE == "RLS R4.0"
    assert public_api.build_r4_0_review is build_r4_0_review
    assert (
        public_api.render_r4_0_review_report
        is render_r4_0_review_report
    )
