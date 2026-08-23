"""Exact-provider regression tests for Ciena RLS R4.0."""

from __future__ import annotations

from dataclasses import replace

import pytest

from utils.rls_config.common import (
    ConfigValidationError,
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    ManagementInterface,
    ROUTING_OSPF_OSC_ONLY,
    ROUTING_STATIC_COLAN_OSPF_OSC,
)
from utils.rls_config.r4_0_generator import (
    COLAN_PROHIBITED,
    COLAN_TERMINAL_OPTIONAL,
    R40_CDA_RLA12_C_2DEG_NO_SRA,
    R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
    R40_R2_CL_DLE_S1_NO_SRA,
    R40_R2_CL_DLE_S1_SRA4,
    R40_PAYLOAD_SCHEMA_ID,
    R40_PAYLOAD_SCHEMA_VERSION,
    R40_PROVIDER_CATALOG,
    R40DirectModuleFact,
    R40ExactConfigGenerator,
    R40ExactRequest,
    R40LinePath,
    R40ProviderCandidateFacts,
    decode_r40_exact_payload,
    encode_r40_exact_payload,
    r40_deployment_controls,
    resolve_r40_provider_candidate,
)


_TERMINAL_PROVIDER_IDS = (
    R40_CDA_RLA12_C_2DEG_NO_SRA,
    R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
)
_ILA_PROVIDER_IDS = (
    R40_R2_CL_DLE_S1_NO_SRA,
    R40_R2_CL_DLE_S1_SRA4,
)


def _line(number: int) -> R40LinePath:
    return R40LinePath(
        link_name=f"SPAN-{number}-LINEOUT",
        neighbor_node=f"PEER-{number}-1",
        neighbor_line_mux_pfg=f"LINE-MUX-PFG-{number}",
        neighbor_line_demux_pfg=f"LINE-DEMUX-PFG-{number}",
        fiber_type="NDSF",
        expected_loss_db=14.0 + number,
        input_patch_loss_db=0.6,
        output_patch_loss_db=0.8,
        repair_margin_db=2.0,
        high_loss_minor_threshold_db=3.0,
    )


def _request(provider_id: str) -> R40ExactRequest:
    provider = R40_PROVIDER_CATALOG[provider_id]
    profile = sorted(provider.role_profiles)[0]
    is_ila = provider.application == "ila_dle_cl"
    is_cdc_roadm = (
        provider_id
        == R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA
    )
    management = (
        ManagementInterface(
            enabled=False,
            name="",
            routing_mode=ROUTING_OSPF_OSC_ONLY,
            ip_address="",
            prefix_length=None,
            gateway="",
        )
        if is_ila
        else ManagementInterface(
            enabled=True,
            name="colan-x",
            ip_address="10.50.104.2",
            prefix_length=24,
            gateway="",
        )
    )
    return R40ExactRequest(
        provider_id=provider_id,
        profile=profile,
        software_release="RLS R4.0",
        target_software_build="R4.0.0-reviewed-schema",
        chassis_family=provider.chassis_family,
        chassis_pec=provider.chassis_pec,
        hardware_profile=provider.hardware_profile,
        shelf_name="SITE-104-1",
        shelf_label="SHELF-1",
        site_id=104,
        site_name="SITE-104",
        site_description="El Paso",
        site_address="El Paso, TX",
        member_name="SITE-104-1",
        hostname="site-104-1",
        frame_identification_code="R4-104",
        bay_number=1,
        physical_shelf=1,
        loopback_ip="10.6.22.104",
        ospf_area="1.6.8.0",
        management=management,
        line_1=_line(1),
        line_2=(
            _line(2)
            if len(provider.line_outputs) == 2
            else None
        ),
        line_1_route_side="A",
        installed_inventory_confirmed=True,
        planner_runtime_mop_confirmed=True,
        target_build_confirmed=True,
        greenfield_fibers_disconnected_confirmed=is_ila,
        calibration_feature_inactive_confirmed=is_ila,
        cfim_unused_ports_terminated_confirmed=is_cdc_roadm,
    )


def _deferred_colan() -> ManagementInterface:
    return ManagementInterface(
        enabled=False,
        name="",
        routing_mode=ROUTING_OSPF_OSC_ONLY,
        ip_address="",
        prefix_length=None,
        gateway="",
    )


@pytest.fixture(scope="module")
def generator() -> R40ExactConfigGenerator:
    return R40ExactConfigGenerator()


def _direct_provider_modules(
    provider_id: str,
) -> tuple[R40DirectModuleFact, ...]:
    profile = R40_PROVIDER_CATALOG[provider_id]
    return (
        *(
            R40DirectModuleFact(slot=slot, pec=pec)
            for slot, pec in profile.equipment
        ),
        *(
            R40DirectModuleFact(slot=slot, subslot=subslot, pec=pec)
            for slot, subslot, pec in profile.osc_modules
        ),
    )


def test_provider_profiles_expose_explicit_resolution_metadata() -> None:
    add_drop = R40_PROVIDER_CATALOG[R40_CDA_RLA12_C_2DEG_NO_SRA]
    roadm = R40_PROVIDER_CATALOG[
        R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA
    ]
    cl_core = R40_PROVIDER_CATALOG[
        R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA
    ]
    ila = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    sra_core = R40_PROVIDER_CATALOG[
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6
    ]
    sra_ila = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_SRA4]

    assert (
        add_drop.optical_band,
        add_drop.topology,
        add_drop.add_drop_structure,
        add_drop.protection_type,
    ) == ("c", "roadm_cda", "cda", "none")
    assert (
        roadm.optical_band,
        roadm.topology,
        roadm.add_drop_structure,
        roadm.protection_type,
    ) == ("c", "roadm_cdc", "cdc", "none")
    assert (
        cl_core.optical_band,
        cl_core.topology,
        cl_core.add_drop_structure,
        cl_core.protection_type,
    ) == ("c+l", "roadm_rla12_cl_core", "none", "none")
    assert (
        ila.optical_band,
        ila.topology,
        ila.add_drop_structure,
        ila.protection_type,
    ) == ("c+l", "ila_single", "none", "none")
    assert add_drop.line_semantics == "bidirectional_degree"
    assert roadm.line_semantics == "bidirectional_degree"
    assert cl_core.line_semantics == "bidirectional_degree"
    assert ila.line_semantics == "unidirectional_amplifier_path"
    assert cl_core.line_outputs == ((1, 53),)
    assert cl_core.line_inputs == ((1, 54),)
    assert cl_core.line_pfg_names == (("LM1", "LD1"),)
    assert cl_core.line_link_names == ("LM1-LINEOUT",)
    assert ila.line_pfg_names == (
        ("PFG-1-to-2", "PFG-2-to-1"),
        ("PFG-2-to-1", "PFG-1-to-2"),
    )
    assert ila.line_link_names == (
        "PFG-1-2-LINEOUT",
        "PFG-2-1-LINEOUT",
    )
    assert sra_core.equipment == (
        (1, "NTK852BC"),
        (3, "NTK852NA"),
        (6, "NTK830AC"),
    )
    assert sra_core.line_outputs == ((6, 5),)
    assert sra_core.line_inputs == ((6, 6),)
    assert sra_core.supports_raman is True
    assert sra_ila.equipment == ((1, "NTK850DC"), (4, "NTK830AC"))
    assert sra_ila.line_outputs == ((4, 5), (1, 53))
    assert sra_ila.line_inputs == ((1, 54), (4, 6))
    assert sra_ila.supports_raman is True
    for provider in R40_PROVIDER_CATALOG.values():
        assert len(provider.line_outputs) in {1, 2}
        assert len(provider.line_inputs) == len(provider.line_outputs)
        assert len(provider.line_pfg_names) == len(provider.line_outputs)
        assert len(provider.line_link_names) == len(provider.line_outputs)


def test_role_only_resolution_is_non_executable_and_sra_ambiguous() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(role_profile="ila")
    )

    assert resolution.status == "ambiguous"
    assert resolution.provider_id is None
    assert resolution.compatible_provider_ids == (
        R40_R2_CL_DLE_S1_NO_SRA,
        R40_R2_CL_DLE_S1_SRA4,
    )
    assert resolution.deployable_cli is False
    assert resolution.reason_codes == ("MULTIPLE_COMPATIBLE_PROVIDERS",)
    assert not hasattr(resolution, "cli_text")
    assert not hasattr(resolution, "profile_payload")


def test_c_plus_l_add_drop_still_requires_an_exact_provider() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="add_drop_a",
            chassis_family="R4 600mm",
            optical_band="C+L",
        )
    )

    assert resolution.status == "conflict"
    assert resolution.provider_id is None
    assert resolution.compatible_provider_ids == ()
    assert "OPTICAL_BAND_MISMATCH" in resolution.reason_codes
    assert resolution.deployable_cli is False


def test_c_plus_l_roadm_core_requires_sra_discriminator() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="roadm_z",
            chassis_family="R4 600mm",
            optical_band="C+L",
        )
    )

    assert resolution.status == "ambiguous"
    assert resolution.provider_id is None
    assert resolution.compatible_provider_ids == (
        R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
    )
    assert resolution.deployable_cli is False


def test_add_drop_sra_remains_unsupported() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="add_drop_a",
            sra_state="present",
        )
    )

    assert resolution.status == "unsupported_sra"
    assert resolution.provider_id is None
    assert resolution.reason_codes == ("R40_SRA_PROVIDER_UNAVAILABLE",)
    assert resolution.deployable_cli is False


@pytest.mark.parametrize(
    ("role", "provider_id"),
    (
        ("roadm_z", R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6),
        ("ila", R40_R2_CL_DLE_S1_SRA4),
    ),
)
def test_sra_state_narrows_to_sra_provider(
    role: str,
    provider_id: str,
) -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile=role,
            sra_state="present",
        )
    )

    assert resolution.status == "unique_candidate"
    assert resolution.provider_id == provider_id
    assert resolution.compatible_provider_ids == (provider_id,)
    assert resolution.deployable_cli is False


def test_matching_partial_direct_module_facts_narrow_to_candidate() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            software_release="RLS R4.0",
            chassis_family="R2 600mm",
            optical_band="C+L",
            module_inventory=(
                R40DirectModuleFact(slot=1, pec="ntk850dc"),
            ),
            sra_state="absent",
        )
    )

    assert resolution.status == "unique_candidate"
    assert resolution.provider_id == R40_R2_CL_DLE_S1_NO_SRA
    assert "module_inventory" in resolution.matched_fields
    assert "module_inventory" in resolution.missing_fields
    assert resolution.deployable_cli is False


def test_conflicting_direct_module_fact_rejects_candidate() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            chassis_family="R2",
            module_inventory=(
                R40DirectModuleFact(slot=1, pec="NTK852AA"),
            ),
        )
    )

    assert resolution.status == "conflict"
    assert resolution.provider_id is None
    assert resolution.reason_codes == ("MODULE_INVENTORY_MISMATCH",)


def test_conflicting_direct_chassis_fact_rejects_candidate() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            chassis_family="R4 600mm",
        )
    )

    assert resolution.status == "conflict"
    assert resolution.provider_id is None
    assert resolution.reason_codes == ("CHASSIS_FAMILY_MISMATCH",)


def test_malformed_direct_module_fact_fails_closed() -> None:
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            module_inventory=(
                R40DirectModuleFact(slot=0, pec="NTK850DC"),
            ),
        )
    )

    assert resolution.status == "conflict"
    assert resolution.provider_id is None
    assert resolution.reason_codes == ("INVALID_DIRECT_MODULE_FACTS",)
    assert resolution.deployable_cli is False


def test_complete_direct_discriminators_return_advisory_exact_match() -> None:
    profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            software_release="RLS R4.0",
            chassis_family="R2 600mm",
            chassis_pec=profile.chassis_pec,
            optical_band="C+L",
            topology="ila_single",
            add_drop_structure="none",
            protection_type="none",
            module_inventory=_direct_provider_modules(profile.provider_id),
            sra_state="absent",
        )
    )

    assert resolution.status == "exact_match"
    assert resolution.provider_id == profile.provider_id
    assert resolution.reason_codes == ("EXACT_DIRECT_DISCRIMINATORS_MATCH",)
    assert resolution.missing_fields == ()
    assert resolution.deployable_cli is False
    assert "module_inventory" in resolution.matched_fields


def test_one_degree_cl_core_complete_direct_facts_exactly_match() -> None:
    profile = R40_PROVIDER_CATALOG[
        R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA
    ]
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="roadm_a",
            software_release="RLS R4.0",
            chassis_family="R4 600mm",
            chassis_pec=profile.chassis_pec,
            optical_band="C+L",
            topology="roadm_rla12_cl_core",
            add_drop_structure="none",
            protection_type="none",
            module_inventory=_direct_provider_modules(profile.provider_id),
            sra_state="absent",
        )
    )

    assert resolution.status == "exact_match"
    assert resolution.provider_id == profile.provider_id
    assert resolution.reason_codes == (
        "EXACT_DIRECT_DISCRIMINATORS_MATCH",
    )
    assert resolution.missing_fields == ()
    assert resolution.deployable_cli is False


@pytest.mark.parametrize(
    ("role", "provider_id"),
    (
        ("roadm_z", R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6),
        ("ila", R40_R2_CL_DLE_S1_SRA4),
    ),
)
def test_complete_sra_direct_facts_exactly_match(
    role: str,
    provider_id: str,
) -> None:
    profile = R40_PROVIDER_CATALOG[provider_id]
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile=role,
            software_release="RLS R4.0",
            chassis_family=f"{profile.chassis_family} 600mm",
            chassis_pec=profile.chassis_pec,
            optical_band="C+L",
            topology=profile.topology,
            add_drop_structure=profile.add_drop_structure,
            protection_type=profile.protection_type,
            module_inventory=_direct_provider_modules(provider_id),
            sra_state="present",
        )
    )

    assert resolution.status == "exact_match"
    assert resolution.provider_id == provider_id
    assert resolution.compatible_provider_ids == (provider_id,)
    assert resolution.missing_fields == ()
    assert resolution.deployable_cli is False


def test_multiple_compatible_profiles_are_ambiguous() -> None:
    first = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_NO_SRA]
    second = replace(first, provider_id="test_second_compatible_ila")
    resolution = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="ila",
            chassis_family="R2",
        ),
        catalog={
            first.provider_id: first,
            second.provider_id: second,
        },
    )

    assert resolution.status == "ambiguous"
    assert resolution.provider_id is None
    assert resolution.compatible_provider_ids == (
        first.provider_id,
        second.provider_id,
    )
    assert resolution.reason_codes == ("MULTIPLE_COMPATIBLE_PROVIDERS",)
    assert resolution.deployable_cli is False


def test_provider_resolution_rejects_other_release_and_protection() -> None:
    wrong_release = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="roadm_a",
            software_release="RLS R4.2",
        )
    )
    protected = resolve_r40_provider_candidate(
        R40ProviderCandidateFacts(
            role_profile="roadm_a",
            protection_type="roadm_trunk",
        )
    )

    assert wrong_release.status == "conflict"
    assert wrong_release.reason_codes == ("SOFTWARE_RELEASE_MISMATCH",)
    assert protected.status == "conflict"
    assert protected.reason_codes == ("PROTECTION_TYPE_MISMATCH",)


@pytest.mark.parametrize("provider_id", tuple(R40_PROVIDER_CATALOG))
def test_each_exact_provider_generates_a_staged_candidate(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    artifact = generator.generate(_request(provider_id))
    lines = artifact.cli_text.splitlines()

    assert not [issue for issue in artifact.issues if issue.severity == "error"]
    assert lines.count("batch") == lines.count("validate")
    assert lines.count("batch") == lines.count("commit")
    assert lines.count("batch") == lines.count("quit")
    assert lines[0] == "batch"
    assert "set ztp admin-state disabled" in lines
    assert "commit —> quit" not in artifact.cli_text
    assert "SITE1977" not in artifact.cli_text
    assert artifact.manifest["release"] == "RLS R4.0"
    assert artifact.manifest["deployment_approved"] is False
    assert artifact.manifest["on_box_validate_required"] is True
    assert artifact.manifest["partial_or_legacy_template_output"] is False
    supports_raman = R40_PROVIDER_CATALOG[provider_id].supports_raman
    assert artifact.manifest["supports_raman"] is supports_raman
    assert (
        "RAMAN/SRA supported by provider: "
        f"{'yes' if supports_raman else 'no'}"
    ) in (
        artifact.validation_report
    )
    assert "openconfig-system:system ntp" not in artifact.cli_text
    assert artifact.manifest["ntp_managed_by_customer"] is True
    assert artifact.manifest["ntp_commands_emitted"] is False

    # Runtime and licensed optical features are deliberately outside this
    # pre-calibration provider.
    assert "system features OTDR enabled true" not in artifact.cli_text
    assert 'system features "SPAN CALIBRATION" enabled true' not in artifact.cli_text
    assert "trace-on-fiber-degrade" not in artifact.cli_text


def test_leaf_is_preserved_with_vendor_enum_warning(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_R2_CL_DLE_S1_NO_SRA)
    request = replace(
        request,
        line_1=replace(request.line_1, fiber_type="LEAF"),
        line_2=replace(request.line_2, fiber_type="LEAF"),
    )

    artifact = generator.generate(request)

    assert artifact.cli_text.count('fiber-type "LEAF"') == 2
    warnings = [
        issue
        for issue in artifact.issues
        if issue.code == "R40_LEAF_VENDOR_ENUM_INCONSISTENCY"
    ]
    assert {issue.field for issue in warnings} == {
        "line_1.fiber_type",
        "line_2.fiber_type",
    }
    assert all(issue.severity == "warning" for issue in warnings)
    assert "ON-BOX VALIDATE REQUIRED" in artifact.validation_report
    assert artifact.manifest["deployment_approved"] is False


@pytest.mark.parametrize("provider_id", tuple(R40_PROVIDER_CATALOG))
def test_osc_pluggables_follow_loopback_in_same_initial_oam_batch(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    request = _request(provider_id)
    cli = generator.generate(request).cli_text
    profile = R40_PROVIDER_CATALOG[provider_id]
    loopback_command = (
        "subinterfaces subinterface 0 ipv4 addresses address "
        f"{request.loopback_ip}"
    )

    for slot, subslot, pec in profile.osc_modules:
        create_command = (
            f"create slots {slot} config slots {subslot} "
            f"config circuit-pack pec {pec}"
        )
        network_instance_command = (
            f"interfaces interface osc-{slot}-{subslot}-1.0"
        )
        assert cli.index(loopback_command) < cli.index(create_command)
        assert cli.index(create_command) < cli.index(network_instance_command)
    assert "ipv4 unnumbered config enabled true" not in cli


def test_cda_rla12_uses_corrected_mandatory_links(
    generator: R40ExactConfigGenerator,
) -> None:
    cli = generator.generate(
        _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    ).cli_text

    assert "create slots 1 config circuit-pack pec NTK852BA" in cli
    assert "create slots 3 config circuit-pack pec NTK852BA" in cli
    assert "create slots 5 config circuit-pack pec NTK834AA" in cli
    assert (
        'create link LC-LINK-4 from "slots 3 config port 23" '
        'to "slots 1 config port 44" link-type fiber'
    ) in cli
    assert (
        'create link RLA-3-44 from "slots 3 config port 44" '
        "link-type logical"
    ) in cli
    assert "mpo-cable" not in cli
    assert "NTK830" not in cli
    assert "NTK852NA" not in cli


def test_one_degree_cl_roadm_core_is_exact_and_omits_unproved_peers(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA)
    artifact = generator.generate(request)
    cli = artifact.cli_text

    assert request.line_2 is None
    for command in (
        "create slots 1 config circuit-pack pec NTK852BC",
        "create slots 3 config circuit-pack pec NTK852NA",
        "create slots 1 config slots 50 config circuit-pack pec NTK591VQ",
        "set functional-group LM1 type LINE-MUX",
        "set functional-group LD1 type LINE-DEMUX",
        "set functional-group SM1 type SECTION-MUX",
        "set functional-group SD1 type SECTION-DEMUX",
        'set functional-group LM1 roles LINEOUT objects '
        '"slots 1 config port 53"',
        'set functional-group LD1 roles LINEIN objects '
        '"slots 1 config port 54"',
        'set functional-group LM1 roles BOOSTER objects '
        '"slots 1 config amps booster-c-band" attrs BAND value BAND-C',
        'set functional-group LM1 roles BOOSTER objects '
        '"slots 1 config amps booster-l-band" attrs BAND value BAND-L',
        'set functional-group LD1 roles PREAMP objects '
        '"slots 3 config amps pre-amp-l-band" attrs BAND value BAND-L',
        'set functional-group SM1 roles UPG-ASE-AMP objects '
        '"slots 1 config amps ase-l-band" attrs BAND value BAND-L',
        'create link LRU3-LINEIN from '
        '"slots 1 config port 11" to '
        '"slots 3 config port 52" link-type fiber',
        'create link LRU3-LINEOUT from '
        '"slots 3 config port 51" to '
        '"slots 1 config port 12" link-type fiber',
        'create link LRU3-MON from '
        '"slots 1 config port 10" to '
        '"slots 3 config port 50" link-type fiber',
    ):
        assert command in cli

    assert cli.count('link-type line-fiber') == 1
    assert 'from "slots 1 config port 53" link-type line-fiber' in cli
    assert "NTK834AA" not in cli
    assert "NTK834AE" not in cli
    assert "NTK843BA" not in cli
    assert "NTK830" not in cli
    assert "MPO-LINK" not in cli
    assert "LC-LINK" not in cli
    assert artifact.manifest["line_record_count"] == 1
    assert artifact.manifest["line_outputs"] == [{"slot": 1, "port": 53}]
    assert artifact.manifest["line_inputs"] == [{"slot": 1, "port": 54}]
    assert artifact.manifest["line_pfg_names"] == [
        {"mux": "LM1", "demux": "LD1"}
    ]
    assert artifact.manifest["line_link_names"] == ["LM1-LINEOUT"]
    assert "Provider line records: 1" in artifact.validation_report


def test_sra6_roadm_stages_exact_topology_disabled(
    generator: R40ExactConfigGenerator,
) -> None:
    artifact = generator.generate(
        _request(R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6)
    )
    cli = artifact.cli_text

    for command in (
        "create slots 1 config circuit-pack pec NTK852BC",
        "create slots 3 config circuit-pack pec NTK852NA",
        "create slots 6 config circuit-pack pec NTK830AC",
        "set slots 6 config circuit-pack admin-state disabled",
        "set slots 6 config ramans LINE-IN admin-state disabled",
        'set functional-group LM1 roles LINEOUT objects '
        '"slots 6 config port 5"',
        'set functional-group LD1 roles LINEIN objects '
        '"slots 6 config port 6"',
        'set functional-group LD1 roles RAMAN objects '
        '"slots 6 config ramans LINE-IN"',
        'create link SPAN-1-LINEOUT from "slots 6 config port 5" '
        "link-type line-fiber",
        'create link RLA1-SRA6 from "slots 1 config port 53" '
        'to "slots 6 config port 4" link-type fiber',
        'create link SRA6-RLA1 from "slots 6 config port 3" '
        'to "slots 1 config port 54" link-type fiber',
        'create link LRU3-LINEIN from "slots 1 config port 11" '
        'to "slots 3 config port 52" link-type fiber',
        'create link LRU3-LINEOUT from "slots 3 config port 51" '
        'to "slots 1 config port 12" link-type fiber',
        'create link LRU3-MON from "slots 1 config port 10" '
        'to "slots 3 config port 50" link-type fiber',
    ):
        assert command in cli

    assert "set slots 6 config circuit-pack admin-state enable" not in cli
    assert "set slots 6 config ramans LINE-IN admin-state enable" not in cli
    assert "SPAN CALIBRATION" not in cli
    assert artifact.manifest["supports_raman"] is True
    assert artifact.manifest["line_outputs"] == [{"slot": 6, "port": 5}]
    assert artifact.manifest["line_inputs"] == [{"slot": 6, "port": 6}]


def test_provider_line_cardinality_fails_closed(
    generator: R40ExactConfigGenerator,
) -> None:
    one_degree = _request(R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA)
    with pytest.raises(ConfigValidationError) as extra:
        generator.generate(replace(one_degree, line_2=_line(2)))
    assert any(
        issue.code == "R40_LINE_CARDINALITY_MISMATCH"
        and issue.field == "line_2"
        for issue in extra.value.issues
    )

    two_degree = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    with pytest.raises(ConfigValidationError) as missing:
        generator.generate(replace(two_degree, line_2=None))
    assert any(
        issue.code == "R40_LINE_CARDINALITY_MISMATCH"
        and issue.field == "line_2"
        for issue in missing.value.issues
    )


def test_cdc_roadm_emits_fixed_mpo_logical_and_reciprocal_cv(
    generator: R40ExactConfigGenerator,
) -> None:
    artifact = generator.generate(
        _request(R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA)
    )
    cli = artifact.cli_text

    for command in (
        "create slots 1 config circuit-pack pec NTK852AA",
        "create slots 3 config circuit-pack pec NTK852AA",
        "create slots 5 config circuit-pack pec NTK843BA",
        "create slots 71 config circuit-pack pec NTK504QA",
        "create slots 72 config circuit-pack pec NTK504QB",
        'create link MPO-LINK-5 from "slots 5 config port 101" '
        'to "slots 72 config port 5" link-type mpo-cable',
        'create link QG1-to-QG2-LOGICAL-LINK-1 from '
        '"slots 1 config port 28-3" to "slots 1 config port 28-10"',
        'set slots 5 config cv ports rx 101-10 '
        'expected-port-id "slot.5_port.101-3"',
    ):
        assert command in cli
    assert "NTK504NA OMC2" in artifact.manifest["fixed_bom_note"]
    assert "does not consume logical slot 6" in artifact.manifest["fixed_bom_note"]
    assert "occupies 5-6" not in artifact.manifest["fixed_bom_note"]
    assert cli.count("QG1-to-QG1-LOGICAL-LINK-") == 8
    assert cli.count("QG1-to-QG2-LOGICAL-LINK-") == 12


def test_dle_ila_is_no_colan_and_disables_sco_before_links(
    generator: R40ExactConfigGenerator,
) -> None:
    cli = generator.generate(_request(R40_R2_CL_DLE_S1_NO_SRA)).cli_text

    assert "create slots 1 config circuit-pack pec NTK850DC" in cli
    assert "create slots 1 config slots 50 config circuit-pack pec NTK591VQ" in cli
    assert "create slots 1 config slots 60 config circuit-pack pec NTK591VQ" in cli
    assert "interface colan-x config" not in cli
    assert (
        'set functional-group PFG-1-to-2 roles LINEIN objects '
        '"slots 1 config port 54"'
    ) in cli
    assert (
        'set functional-group PFG-1-to-2 roles LINEOUT objects '
        '"slots 1 config port 63"'
    ) in cli
    assert (
        'set functional-group PFG-2-to-1 roles LINEIN objects '
        '"slots 1 config port 64"'
    ) in cli
    assert (
        'set functional-group PFG-2-to-1 roles LINEOUT objects '
        '"slots 1 config port 53"'
    ) in cli
    assert "roles BAND-C attrs MIN-FREQ value 191.27500" in cli
    assert "roles BAND-L attrs MIN-FREQ value 186.05000" in cli
    assert "set sco PFG-1-to-2 config admin-state Disabled" in cli
    assert "set sco PFG-2-to-1 config admin-state Disabled" in cli
    assert cli.index("set sco PFG-1-to-2") < cli.index("create link SPAN-1-LINEOUT")
    assert "input-patch-panel-loss 0.6" in cli
    assert "output-patch-panel-loss 0.8" in cli
    assert "repair-margin 2" in cli
    assert (
        "set functional-group PFG-1-to-2 attrs DOWNSTR-PFG-NEIGHBOR "
        "value PEER-1-1/LINE-DEMUX-PFG-1"
    ) in cli
    assert (
        "set functional-group PFG-1-to-2 attrs UPSTR-PFG-NEIGHBOR "
        "value PEER-2-1/LINE-MUX-PFG-2"
    ) in cli
    assert (
        "set functional-group PFG-2-to-1 attrs DOWNSTR-PFG-NEIGHBOR "
        "value PEER-2-1/LINE-DEMUX-PFG-2"
    ) in cli
    assert (
        "set functional-group PFG-2-to-1 attrs UPSTR-PFG-NEIGHBOR "
        "value PEER-1-1/LINE-MUX-PFG-1"
    ) in cli
    assert (
        "value PEER-1-1/LINE-MUX-PFG-1"
        not in next(
            line
            for line in cli.splitlines()
            if "PFG-1-to-2 attrs UPSTR-PFG-NEIGHBOR" in line
        )
    )


def test_sra4_dle_stages_one_raman_path_disabled(
    generator: R40ExactConfigGenerator,
) -> None:
    artifact = generator.generate(_request(R40_R2_CL_DLE_S1_SRA4))
    cli = artifact.cli_text

    for command in (
        "create slots 1 config circuit-pack pec NTK850DC",
        "create slots 4 config circuit-pack pec NTK830AC",
        "set slots 4 config circuit-pack admin-state disabled",
        "set slots 4 config ramans LINE-IN admin-state disabled",
        'set functional-group PFG-1-to-2 roles LINEIN objects '
        '"slots 1 config port 54"',
        'set functional-group PFG-1-to-2 roles LINEOUT objects '
        '"slots 4 config port 5"',
        'set functional-group PFG-2-to-1 roles LINEIN objects '
        '"slots 4 config port 6"',
        'set functional-group PFG-2-to-1 roles LINEOUT objects '
        '"slots 1 config port 53"',
        'set functional-group PFG-2-to-1 roles RAMAN objects '
        '"slots 4 config ramans LINE-IN"',
        'create link SPAN-1-LINEOUT from "slots 4 config port 5" '
        "link-type line-fiber",
        'create link SPAN-2-LINEOUT from "slots 1 config port 53" '
        "link-type line-fiber",
        'create link RLA1-SRA4 from "slots 1 config port 63" '
        'to "slots 4 config port 4" link-type fiber',
        'create link SRA4-RLA1 from "slots 4 config port 3" '
        'to "slots 1 config port 64" link-type fiber',
    ):
        assert command in cli

    assert "set slots 4 config circuit-pack admin-state enable" not in cli
    assert "set slots 4 config ramans LINE-IN admin-state enable" not in cli
    assert "SPAN CALIBRATION" not in cli
    assert artifact.manifest["supports_raman"] is True
    assert artifact.manifest["line_outputs"] == [
        {"slot": 4, "port": 5},
        {"slot": 1, "port": 53},
    ]
    assert artifact.manifest["line_inputs"] == [
        {"slot": 1, "port": 54},
        {"slot": 4, "port": 6},
    ]


def test_dle_ila_rejects_reusing_one_neighbor_for_both_physical_sides(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_R2_CL_DLE_S1_NO_SRA)
    request = replace(
        request,
        line_2=replace(
            request.line_2,
            neighbor_node=request.line_1.neighbor_node,
        ),
    )

    issues = generator.validate(request)

    assert "ILA_DISTINCT_SIDE_NEIGHBORS_REQUIRED" in {
        issue.code for issue in issues if issue.severity == "error"
    }


def test_payload_round_trip_is_strict() -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    encoded = encode_r40_exact_payload(request)

    assert encoded["schema_id"] == R40_PAYLOAD_SCHEMA_ID
    assert encoded["schema_version"] == R40_PAYLOAD_SCHEMA_VERSION == "1.4"
    assert decode_r40_exact_payload(encoded) == request
    assert "ntp_servers" not in encoded["request"]

    with pytest.raises(ValueError, match="unknown fields"):
        decode_r40_exact_payload({**encoded, "unexpected": True})

    bad_request = dict(encoded["request"])
    bad_request["target_build_confirmed"] = 1
    with pytest.raises(ValueError, match="must be Boolean"):
        decode_r40_exact_payload({**encoded, "request": bad_request})

    bad_line = dict(bad_request)
    bad_line["target_build_confirmed"] = True
    bad_line["line_1"] = {**bad_line["line_1"], "repair_margin_db": "2"}
    with pytest.raises(ValueError, match="repair_margin_db must be a number"):
        decode_r40_exact_payload({**encoded, "request": bad_line})

    with pytest.raises(ValueError, match="schema_version"):
        decode_r40_exact_payload({**encoded, "schema_version": "1.2"})

    one_degree = _request(R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA)
    one_degree_encoded = encode_r40_exact_payload(one_degree)
    assert one_degree_encoded["request"]["line_2"] is None
    assert decode_r40_exact_payload(one_degree_encoded) == one_degree


def test_exact_discriminators_fail_closed_and_background_controls_do_not(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)

    with pytest.raises(ConfigValidationError) as mismatch:
        generator.generate(replace(request, chassis_pec="NTK803DA"))
    assert any(
        issue.code == "R40_EXACT_DISCRIMINATOR_MISMATCH"
        for issue in mismatch.value.issues
    )

    unconfirmed = generator.generate(
        replace(request, installed_inventory_confirmed=False)
    )
    assert not [
        issue for issue in unconfirmed.issues if issue.severity == "error"
    ]
    assert any(
        issue.code == "BACKGROUND_DEPLOYMENT_CONTROL"
        and issue.field == "installed_inventory_confirmed"
        for issue in unconfirmed.issues
    )

    ila = _request(R40_R2_CL_DLE_S1_NO_SRA)
    unsafe_ila = generator.generate(
        replace(ila, calibration_feature_inactive_confirmed=False)
    )
    assert any(
        issue.field == "calibration_feature_inactive_confirmed"
        and issue.severity == "warning"
        for issue in unsafe_ila.issues
    )

    roadm = _request(
        R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA
    )
    unsafe_cfim = generator.generate(
        replace(roadm, cfim_unused_ports_terminated_confirmed=False)
    )
    assert any(
        issue.field == "cfim_unused_ports_terminated_confirmed"
        and issue.severity == "warning"
        for issue in unsafe_cfim.issues
    )

    with pytest.raises(ConfigValidationError) as unmapped_direction:
        generator.generate(replace(request, line_1_route_side=""))
    assert any(
        issue.code == "R40_DEGREE_ROUTE_SIDE_REQUIRED"
        for issue in unmapped_direction.value.issues
    )


@pytest.mark.parametrize("provider_id", tuple(R40_PROVIDER_CATALOG))
def test_manifest_and_annotated_candidate_carry_provider_controls(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    request = replace(
        _request(provider_id),
        installed_inventory_confirmed=False,
        planner_runtime_mop_confirmed=False,
        target_build_confirmed=False,
        greenfield_fibers_disconnected_confirmed=False,
        calibration_feature_inactive_confirmed=False,
        cfim_unused_ports_terminated_confirmed=False,
    )

    artifact = generator.generate(request)
    expected = r40_deployment_controls(provider_id)
    manifested = artifact.manifest["deployment_controls"]

    assert artifact.manifest["deployment_approved"] is False
    assert artifact.manifest["on_box_validate_required"] is True
    assert (
        artifact.manifest["legacy_confirmations_block_offline_generation"]
        is False
    )
    assert artifact.manifest["deployment_control_count"] == len(expected)
    assert [item["id"] for item in manifested] == [
        control.control_id for control in expected
    ]
    assert all(item["active"] is True for item in manifested)
    assert all(
        item["mode"] == "automatic_background_advisory"
        for item in manifested
    )
    assert all(
        item["physical_verification_status"] == "not_asserted"
        for item in manifested
    )
    assert all(item["status"] == "active_unverified" for item in manifested)
    assert all(item["title"] for item in manifested)
    assert all(item["applicability"] for item in manifested)
    assert all(
        item["legacy_confirmation_value"] is False for item in manifested
    )
    for control in expected:
        assert control.control_id in artifact.annotated_text
    assert "does not claim that ATLAS verified" in artifact.annotated_text


@pytest.mark.parametrize(
    ("provider_id", "specific_control_ids"),
    (
        (R40_CDA_RLA12_C_2DEG_NO_SRA, set()),
        (
            R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
            {"cfim_unused_ports_vendor_treated"},
        ),
        (R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA, set()),
        (
            R40_R2_CL_DLE_S1_NO_SRA,
            {
                "dle_line_fibers_disconnected",
                "dle_calibration_features_inactive",
            },
        ),
        (
            R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
            {
                "sra_span_bookended_and_colocated",
                "sra_runtime_engineering_approved",
                "sra_go_no_go_and_alarms_pass",
            },
        ),
        (
            R40_R2_CL_DLE_S1_SRA4,
            {
                "dle_line_fibers_disconnected",
                "dle_calibration_features_inactive",
                "sra_span_bookended_and_colocated",
                "sra_runtime_engineering_approved",
                "sra_go_no_go_and_alarms_pass",
            },
        ),
    ),
)
def test_background_control_applicability_is_provider_specific(
    provider_id: str,
    specific_control_ids: set[str],
) -> None:
    controls = r40_deployment_controls(provider_id)
    common_ids = {
        "installed_inventory_matches_provider",
        "approved_runtime_engineering_available",
        "target_shelf_matches_recorded_build",
    }

    assert {control.control_id for control in controls} == (
        common_ids | specific_control_ids
    )


def test_background_controls_keep_real_inputs_and_types_fail_closed(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)

    with pytest.raises(ConfigValidationError) as missing_inputs:
        generator.generate(
            replace(
                request,
                target_software_build="",
                frame_identification_code="",
                installed_inventory_confirmed=False,
                planner_runtime_mop_confirmed=False,
                target_build_confirmed=False,
            )
        )
    assert {
        issue.code for issue in missing_inputs.value.issues
    } >= {"TARGET_BUILD_REQUIRED"}
    assert not any(
        issue.code == "REQUIRED"
        and issue.field == "frame_identification_code"
        for issue in missing_inputs.value.issues
    )

    malformed = replace(request, installed_inventory_confirmed=1)
    with pytest.raises(ConfigValidationError) as invalid_boolean:
        generator.generate(malformed)
    assert any(
        issue.code == "INVALID_BOOLEAN"
        and issue.field == "installed_inventory_confirmed"
        for issue in invalid_boolean.value.issues
    )

    for generic_build in (
        "4.0",
        "R4.0",
        "RLS 4.0",
        "RLS R4.0",
        "Release R4.0",
    ):
        with pytest.raises(ConfigValidationError) as generic:
            generator.generate(
                replace(request, target_software_build=generic_build)
            )
        assert any(
            issue.code == "TARGET_BUILD_REQUIRED"
            and issue.field == "target_software_build"
            for issue in generic.value.issues
        )


@pytest.mark.parametrize("provider_id", tuple(R40_PROVIDER_CATALOG))
def test_documented_build_default_and_deferred_frame_generate_safely(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    assert DEFAULT_R40_TARGET_BUILD_SCHEMA == "4.00.00"
    installed_location = generator.generate(
        replace(
            _request(provider_id),
            target_software_build=DEFAULT_R40_TARGET_BUILD_SCHEMA,
        )
    )
    request = replace(
        _request(provider_id),
        target_software_build=DEFAULT_R40_TARGET_BUILD_SCHEMA,
        frame_identification_code="",
    )

    artifact = generator.generate(request)
    warning_codes = {
        issue.code
        for issue in artifact.issues
        if issue.severity == "warning"
    }

    assert "TARGET_BUILD_SCHEMA_DEFAULTED" in warning_codes
    assert "FRAME_LOCATION_DEFERRED" in warning_codes
    assert "set shelf shelf-location" not in artifact.cli_text
    assert "frame-identification-code" not in artifact.cli_text
    assert (
        'set shelf shelf-location frame-identification-code "R4-104"'
        in installed_location.cli_text
    )
    assert artifact.request.target_software_build == "4.00.00"
    assert artifact.request.frame_identification_code == ""


def test_deferred_frame_and_documented_build_default_survive_payload_round_trip(
) -> None:
    request = replace(
        _request(R40_CDA_RLA12_C_2DEG_NO_SRA),
        target_software_build=DEFAULT_R40_TARGET_BUILD_SCHEMA,
        frame_identification_code="",
    )

    encoded = encode_r40_exact_payload(request)
    decoded = decode_r40_exact_payload(encoded)

    assert encoded["request"]["target_software_build"] == "4.00.00"
    assert encoded["request"]["frame_identification_code"] == ""
    assert decoded == request


def test_no_direct_dcn_rejects_hidden_colan_values(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_R2_CL_DLE_S1_NO_SRA)
    invalid_management = replace(
        request.management,
        name="colan-x",
        ip_address="192.0.2.10",
        prefix_length=24,
    )

    with pytest.raises(ConfigValidationError) as exc:
        generator.generate(
            replace(request, management=invalid_management)
        )
    assert any(
        issue.code == "UNUSED_MANAGEMENT_ADDRESS"
        for issue in exc.value.issues
    )
    assert any(
        issue.code == "ILA_COLAN_PROHIBITED"
        for issue in exc.value.issues
    )


def test_colan_policy_classifies_all_exact_providers() -> None:
    assert {
        provider_id
        for provider_id, profile in R40_PROVIDER_CATALOG.items()
        if profile.colan_policy == COLAN_TERMINAL_OPTIONAL
    } == set(_TERMINAL_PROVIDER_IDS)
    assert {
        provider_id
        for provider_id, profile in R40_PROVIDER_CATALOG.items()
        if profile.colan_policy == COLAN_PROHIBITED
    } == set(_ILA_PROVIDER_IDS)


@pytest.mark.parametrize("provider_id", _TERMINAL_PROVIDER_IDS)
def test_terminal_provider_emits_configured_customer_colan(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    request = _request(provider_id)
    artifact = generator.generate(request)
    cli = artifact.cli_text

    assert "interface colan-x config name colan-x" in cli
    assert request.management.ip_address in cli
    assert not any(
        issue.code == "TERMINAL_COLAN_DEFERRED"
        for issue in artifact.issues
    )
    assert artifact.manifest["colan_policy"] == COLAN_TERMINAL_OPTIONAL
    assert artifact.manifest["colan_state"] == "configured"
    assert artifact.manifest["colan_configured"] is True
    assert artifact.manifest["colan_commands_emitted"] is True
    assert artifact.manifest["colan_deferred_for_factory_staging"] is False


@pytest.mark.parametrize("provider_id", _TERMINAL_PROVIDER_IDS)
def test_terminal_provider_accepts_wholly_deferred_colan_with_warning(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    request = replace(_request(provider_id), management=_deferred_colan())

    artifact = generator.generate(request)
    warning_codes = {
        issue.code
        for issue in artifact.issues
        if issue.severity == "warning"
    }

    assert warning_codes >= {"TERMINAL_COLAN_DEFERRED"}
    assert "interface colan-" not in artifact.cli_text
    assert "interface loopback config" in artifact.cli_text
    for slot, subslot, pec in R40_PROVIDER_CATALOG[
        provider_id
    ].osc_modules:
        assert (
            f"create slots {slot} config slots {subslot} "
            f"config circuit-pack pec {pec}"
        ) in artifact.cli_text
    assert artifact.manifest["colan_policy"] == COLAN_TERMINAL_OPTIONAL
    assert artifact.manifest["colan_state"] == "deferred"
    assert artifact.manifest["colan_configured"] is False
    assert artifact.manifest["colan_commands_emitted"] is False
    assert artifact.manifest["colan_deferred_for_factory_staging"] is True
    assert artifact.manifest["warning_count"] >= 1
    assert "[TERMINAL_COLAN_DEFERRED]" in artifact.annotated_text
    assert "[TERMINAL_COLAN_DEFERRED]" in artifact.validation_report


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("name", "colan-x"),
        ("ip_address", "192.0.2.10"),
        ("prefix_length", 24),
        ("gateway", "192.0.2.1"),
    ),
)
def test_optional_terminal_colan_rejects_hidden_values(
    generator: R40ExactConfigGenerator,
    field_name: str,
    value: object,
) -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    hidden = replace(_deferred_colan(), **{field_name: value})

    with pytest.raises(ConfigValidationError) as exc:
        generator.generate(replace(request, management=hidden))

    codes = {issue.code for issue in exc.value.issues}
    assert "UNUSED_MANAGEMENT_ADDRESS" in codes
    assert "TERMINAL_COLAN_DEFERRED" not in codes


def test_optional_terminal_colan_rejects_partial_numbered_design(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    partial = replace(
        request.management,
        ip_address="",
        prefix_length=None,
    )

    with pytest.raises(ConfigValidationError) as exc:
        generator.generate(replace(request, management=partial))

    codes = {issue.code for issue in exc.value.issues}
    assert "INVALID_IPV4" in codes
    assert "INVALID_PREFIX" in codes
    assert "TERMINAL_COLAN_DEFERRED" not in codes


def test_optional_terminal_colan_emits_reviewed_colan_a_static_route(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA)
    management = ManagementInterface(
        enabled=True,
        name="colan-a",
        routing_mode=ROUTING_STATIC_COLAN_OSPF_OSC,
        ip_address="10.10.10.2",
        prefix_length=30,
        gateway="10.10.10.1",
        static_metric=1500,
    )

    artifact = generator.generate(replace(request, management=management))

    assert "interface colan-a config name colan-a" in artifact.cli_text
    assert "interface colan-x" not in artifact.cli_text
    assert "interface colan-a subinterface 0" in artifact.cli_text
    assert "next-hop 10.10.10.1 metric 1500" in artifact.cli_text
    assert artifact.manifest["colan_state"] == "configured"
    assert artifact.manifest["colan_commands_emitted"] is True


@pytest.mark.parametrize("provider_id", _ILA_PROVIDER_IDS)
def test_ila_colan_remains_prohibited_for_every_ila_provider(
    generator: R40ExactConfigGenerator,
    provider_id: str,
) -> None:
    request = replace(_request(provider_id), management=_deferred_colan())
    artifact = generator.generate(request)

    assert not any(
        issue.code == "TERMINAL_COLAN_DEFERRED"
        for issue in artifact.issues
    )
    assert artifact.manifest["colan_policy"] == COLAN_PROHIBITED
    assert artifact.manifest["colan_state"] == "prohibited"
    assert artifact.manifest["colan_configured"] is False
    assert artifact.manifest["colan_commands_emitted"] is False
    assert artifact.manifest["colan_deferred_for_factory_staging"] is False
    assert "interface colan-" not in artifact.cli_text

    hidden = replace(
        request.management,
        name="colan-x",
        ip_address="192.0.2.10",
        prefix_length=24,
    )
    with pytest.raises(ConfigValidationError) as exc:
        generator.generate(replace(request, management=hidden))
    assert "ILA_COLAN_PROHIBITED" in {
        issue.code for issue in exc.value.issues
    }


@pytest.mark.parametrize("provider_id", _TERMINAL_PROVIDER_IDS)
def test_deferred_terminal_colan_payload_round_trip_stays_schema_1_4(
    provider_id: str,
) -> None:
    request = replace(_request(provider_id), management=_deferred_colan())

    encoded = encode_r40_exact_payload(request)

    assert encoded["schema_version"] == R40_PAYLOAD_SCHEMA_VERSION == "1.4"
    assert "colan_review_state" not in encoded["request"]
    assert decode_r40_exact_payload(encoded) == request


def test_schema_1_2_rejects_removed_ntp_field() -> None:
    encoded = encode_r40_exact_payload(
        _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    )
    request = dict(encoded["request"])
    request["ntp_servers"] = ["192.0.2.10"]

    with pytest.raises(ValueError, match="unknown fields"):
        decode_r40_exact_payload({**encoded, "request": request})


def test_ipv4_oam_accepts_backbone_area_and_rejects_unusable_hosts(
    generator: R40ExactConfigGenerator,
) -> None:
    request = _request(R40_CDA_RLA12_C_2DEG_NO_SRA)
    artifact = generator.generate(replace(request, ospf_area="0.0.0.0"))
    assert "areas area 0.0.0.0" in artifact.cli_text

    with pytest.raises(ConfigValidationError) as bad_loopback:
        generator.generate(replace(request, loopback_ip="127.0.0.1"))
    assert any(
        issue.code == "INVALID_IPV4_CLASS"
        for issue in bad_loopback.value.issues
    )

    bad_static = ManagementInterface(
        enabled=True,
        name="colan-x",
        routing_mode=ROUTING_STATIC_COLAN_OSPF_OSC,
        ip_address="10.10.10.4",
        prefix_length=30,
        gateway="10.10.20.1",
    )
    with pytest.raises(ConfigValidationError) as bad_subnet:
        generator.generate(replace(request, management=bad_static))
    codes = {issue.code for issue in bad_subnet.value.issues}
    assert "COLAN_HOST_ADDRESS" in codes
    assert "GATEWAY_OUTSIDE_SUBNET" in codes
