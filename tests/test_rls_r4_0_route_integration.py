"""Route-level contracts for exact audited RLS R4.0 providers."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from utils.rls_config.common import (
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    ManagementInterface,
    ROUTING_OSPF_OSC_ONLY,
)
from utils.rls_config.r4_0_generator import (
    COLAN_TERMINAL_OPTIONAL,
    R40_CDA_RLA12_C_2DEG_NO_SRA,
    R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
    R40_PROVIDER_CATALOG,
    R40_R2_CL_DLE_S1_SRA4,
    R40ExactRequest,
    R40LinePath,
    decode_r40_exact_payload,
    encode_r40_exact_payload,
)
from utils.rls_config.route_bundle import _write_config_candidates
from utils.rls_config.route_config import (
    RouteConfigBuild,
    ShelfConfigBuild,
    evaluate_route_configs,
)
from utils.rls_config.route_project import (
    DeploymentReadiness,
    OpticalPath,
    PathEndpointReview,
    R40_PENDING_SRA_PEER_REVIEW,
    RouteProject,
    RouteLink,
    ShelfInstance,
    Site,
    _r40_route_topology_issues,
)


def _request(**changes: object) -> R40ExactRequest:
    profile = R40_PROVIDER_CATALOG[R40_CDA_RLA12_C_2DEG_NO_SRA]
    request = R40ExactRequest(
        provider_id=profile.provider_id,
        profile="add_drop",
        software_release="RLS R4.0",
        target_software_build="R4.0.0-audited",
        chassis_family=profile.chassis_family,
        chassis_pec=profile.chassis_pec,
        hardware_profile=profile.hardware_profile,
        shelf_name="RLS-01",
        site_name="S01",
        member_name="rls-01",
        hostname="rls-01.example.net",
        frame_identification_code="Rack 01",
        loopback_ip="198.51.100.10",
        ospf_area="0.0.0.1",
        management=ManagementInterface(
            enabled=True,
            name="colan-x",
            routing_mode="OSPF_GNE",
            ip_address="192.0.2.1",
            prefix_length=30,
            gateway="",
            ospf_metric=10,
            static_metric=1500,
        ),
        line_1=R40LinePath(
            link_name="LINE-1-OUT",
            neighbor_node="peer-1",
            neighbor_line_mux_pfg="LINE-MUX-PFG-1",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-1",
            fiber_type="NDSF",
            expected_loss_db=12.0,
        ),
        line_2=R40LinePath(
            link_name="LINE-2-OUT",
            neighbor_node="peer-2",
            neighbor_line_mux_pfg="LINE-MUX-PFG-2",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-2",
            fiber_type="NDSF",
            expected_loss_db=13.0,
        ),
        line_1_route_side="A",
        installed_inventory_confirmed=True,
        planner_runtime_mop_confirmed=True,
        target_build_confirmed=True,
    )
    return replace(request, **changes)


def _shelf(
    *,
    payload: dict[str, object] | None = None,
    profile_id: str = "add_drop",
    release: str = "RLS R4.0",
) -> ShelfInstance:
    return ShelfInstance(
        shelf_id="shelf-01",
        profile_id=profile_id,
        software_release=release,
        shelf_variant="reviewed-exact-provider",
        site_key="site-1",
        tid="RLS-01",
        primary_oam_ip="192.0.2.1",
        power_label="A/B -48 VDC",
        profile_payload=payload or {},
        review_state="confirmed",
    )


def _project(shelves: tuple[ShelfInstance, ...]) -> RouteProject:
    return RouteProject(
        project_id="r40-route",
        route_code="R40",
        title="Exact R4.0 route",
        ospf_area="0.0.0.1",
        sites=(
            Site("site-1", "S01", "Site 01"),
            Site("site-2", "S02", "Site 02"),
        ),
        shelves=shelves,
    )


def _sra_source_evidence(
    status: str,
    *,
    shelf_tid: str = "RLS-01",
    slot: int = 6,
) -> dict[str, object]:
    source_sha256 = "a" * 64
    callouts = [
        {
            "raw_text": f"{slot}/{port}",
            "slot": slot,
            "port": port,
            "shelf_tid": shelf_tid,
            "context": "shelf_endpoint",
            "evidence": [{"field": "slot_port", "confidence": 0.99}],
            "deployable_cli": False,
        }
        for port in (5, 6)
    ]
    return {
        "schema_id": "atlas.ciena.rls.diagram-import-evidence",
        "schema_version": "1.3",
        "source_sha256": source_sha256,
        "raman_callout_convention": {
            "id": "small-red-slot-port-v1",
            "source_sha256": source_sha256,
            "scope": "source",
            "deployable_cli": False,
        },
        "raman_callouts": callouts,
        "raman_callout_review": status,
    }


def _route_native_fiber_evidence(
    token: str = "NDSF",
) -> dict[str, object]:
    return {
        "route_native_fiber_review": {
            "value": token,
            "scope": "all_active_route_spans",
            "action": "operator_apply_route_native_fiber",
            "status": "confirmed",
            "deployable_cli": False,
        }
    }


def _two_shelf_exact_project(
    *,
    a_loss: float = 12.0,
    z_loss: float = 13.0,
    path_review_state: str = "confirmed",
    z_endpoint_link_name: str = "Z-LOCAL-LINK",
) -> RouteProject:
    a_base = _request(
        profile="add_drop_a",
        shelf_name="RLS-A",
        site_name="S01",
        member_name="RLS-A",
        hostname="rls-a.example.net",
        loopback_ip="198.51.100.10",
        management=replace(
            _request().management,
            ip_address="192.0.2.1",
        ),
        line_1_route_side="Z",
    )
    z_base = _request(
        profile="add_drop_z",
        shelf_name="RLS-Z",
        site_name="S02",
        member_name="RLS-Z",
        hostname="rls-z.example.net",
        loopback_ip="198.51.100.20",
        management=replace(
            _request().management,
            ip_address="192.0.2.5",
        ),
        line_1_route_side="A",
    )
    a_request = replace(
        a_base,
        line_1=replace(
            a_base.line_1,
            link_name="A-LOCAL-LINK",
            neighbor_node="RLS-Z",
            neighbor_line_mux_pfg="LINE-MUX-PFG-1",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-1",
            expected_loss_db=a_loss,
        ),
    )
    z_request = replace(
        z_base,
        line_1=replace(
            z_base.line_1,
            link_name="Z-LOCAL-LINK",
            neighbor_node="RLS-A",
            neighbor_line_mux_pfg="LINE-MUX-PFG-1",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-1",
            expected_loss_db=z_loss,
        ),
    )
    shelves = (
        replace(
            _shelf(
                payload=encode_r40_exact_payload(a_request),
                profile_id="add_drop_a",
            ),
            shelf_id="shelf-a",
            site_key="site-1",
            tid="RLS-A",
            primary_oam_ip="192.0.2.1",
        ),
        replace(
            _shelf(
                payload=encode_r40_exact_payload(z_request),
                profile_id="add_drop_z",
            ),
            shelf_id="shelf-z",
            site_key="site-2",
            tid="RLS-Z",
            primary_oam_ip="192.0.2.5",
        ),
    )
    path = OpticalPath(
        path_id="path-a-z",
        path_role="route",
        link_name="CUSTOMER-SPAN-A-Z",
        expected_loss_db=12.5,
        fiber_type="NDSF",
        review_state=path_review_state,  # type: ignore[arg-type]
        source_evidence=_route_native_fiber_evidence(),
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id="shelf-a",
                link_name="A-LOCAL-LINK",
                expected_loss_db=a_loss,
                fiber_type="NDSF",
            ),
            PathEndpointReview(
                shelf_id="shelf-z",
                link_name=z_endpoint_link_name,
                expected_loss_db=z_loss,
                fiber_type="NDSF",
            ),
        ),
    )
    return replace(
        _project(shelves),
        links=(
            RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id="shelf-a",
                to_shelf_id="shelf-z",
                paths=(path,),
            ),
        ),
    )


def _sra_pair_project(
    *,
    ila_line_1_side: str = "Z",
    sat_evidence_status: str = "accepted",
) -> RouteProject:
    ila_profile = R40_PROVIDER_CATALOG[R40_R2_CL_DLE_S1_SRA4]
    sat_profile = R40_PROVIDER_CATALOG[
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6
    ]
    ila_request = replace(
        _request(),
        provider_id=ila_profile.provider_id,
        profile="ila",
        chassis_family=ila_profile.chassis_family,
        chassis_pec=ila_profile.chassis_pec,
        hardware_profile=ila_profile.hardware_profile,
        shelf_name="USXGN1-L8I2",
        site_name="Site 01",
        member_name="USXGN1-L8I2",
        hostname="USXGN1-L8I2",
        loopback_ip="10.6.22.145",
        management=ManagementInterface(
            enabled=False,
            name="",
            routing_mode=ROUTING_OSPF_OSC_ONLY,
            ip_address="",
            prefix_length=None,
            gateway="",
            ospf_metric=10,
            static_metric=1500,
        ),
        line_1=R40LinePath(
            link_name="PFG-1-2-LINEOUT",
            neighbor_node="USSAT4-L8R3",
            neighbor_line_mux_pfg="LM1",
            neighbor_line_demux_pfg="LD1",
            fiber_type="NDSF",
            expected_loss_db=7.14,
        ),
        line_2=R40LinePath(
            link_name="PFG-2-1-LINEOUT",
            neighbor_node="USKP21-L8I2",
            neighbor_line_mux_pfg="PFG-1-to-2",
            neighbor_line_demux_pfg="PFG-2-to-1",
            fiber_type="NDSF",
            expected_loss_db=15.65,
        ),
        line_1_route_side=ila_line_1_side,
    )
    sat_request = replace(
        _request(),
        provider_id=sat_profile.provider_id,
        profile="roadm_z",
        chassis_family=sat_profile.chassis_family,
        chassis_pec=sat_profile.chassis_pec,
        hardware_profile=sat_profile.hardware_profile,
        shelf_name="USSAT4-L8R3",
        site_name="Site 02",
        member_name="USSAT4-L8R3",
        hostname="USSAT4-L8R3",
        loopback_ip="10.6.22.158",
        management=replace(
            _request().management,
            ip_address="10.50.104.2",
        ),
        line_1=R40LinePath(
            link_name="LM1-LINEOUT",
            neighbor_node="USXGN1-L8I2",
            neighbor_line_mux_pfg="PFG-1-to-2",
            neighbor_line_demux_pfg="PFG-2-to-1",
            fiber_type="NDSF",
            expected_loss_db=7.14,
        ),
        line_2=None,
        line_1_route_side="A",
    )
    shelves = (
        replace(
            _shelf(
                payload=encode_r40_exact_payload(ila_request),
                profile_id="ila",
            ),
            shelf_id="shelf-xgn",
            site_key="site-1",
            tid="USXGN1-L8I2",
            primary_oam_ip="10.6.22.145",
            raman_label="Slot 4",
            source_evidence=_sra_source_evidence(
                "accepted",
                shelf_tid="USXGN1-L8I2",
                slot=4,
            ),
        ),
        replace(
            _shelf(
                payload=encode_r40_exact_payload(sat_request),
                profile_id="roadm_z",
            ),
            shelf_id="shelf-sat",
            site_key="site-2",
            tid="USSAT4-L8R3",
            primary_oam_ip="10.6.22.158",
            raman_label="Slot 6",
            source_evidence=_sra_source_evidence(
                sat_evidence_status,
                shelf_tid="USSAT4-L8R3",
                slot=6,
            ),
        ),
    )
    path = OpticalPath(
        path_id="path-xgn-sat",
        path_role="route",
        link_name="XGN-SAT",
        expected_loss_db=7.14,
        fiber_type="NDSF",
        review_state="confirmed",
        source_evidence=_route_native_fiber_evidence(),
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id="shelf-xgn",
                link_name="PFG-1-2-LINEOUT",
                expected_loss_db=7.14,
                fiber_type="NDSF",
            ),
            PathEndpointReview(
                shelf_id="shelf-sat",
                link_name="LM1-LINEOUT",
                expected_loss_db=7.14,
                fiber_type="NDSF",
            ),
        ),
    )
    return replace(
        _project(shelves),
        links=(
            RouteLink(
                link_id="link-xgn-sat",
                order=1,
                from_shelf_id="shelf-xgn",
                to_shelf_id="shelf-sat",
                paths=(path,),
            ),
        ),
    )


def test_exact_r40_payload_makes_generic_role_ready_and_builds() -> None:
    project = _project(
        (_shelf(payload=encode_r40_exact_payload(_request())),)
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is True
    assert readiness.shelf_statuses[0].provider_available is True
    assert build.ready is True
    assert build.config_count == 1
    assert build.shelf_builds[0].artifact.manifest["release"] == "RLS R4.0"


def test_route_build_accepts_documented_build_default_and_deferred_frame() -> None:
    request = _request(
        target_software_build=DEFAULT_R40_TARGET_BUILD_SCHEMA,
        frame_identification_code="",
    )
    payload = encode_r40_exact_payload(request)
    project = _project(
        (
            replace(
                _shelf(payload=payload),
                primary_oam_ip=request.loopback_ip,
            ),
        )
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)
    artifact = build.shelf_builds[0].artifact
    warning_codes = {
        issue.code
        for issue in artifact.issues
        if issue.severity == "warning"
    }

    assert decode_r40_exact_payload(payload) == request
    assert readiness.ready is True
    assert build.ready is True
    assert build.config_count == 1
    assert "TARGET_BUILD_SCHEMA_DEFAULTED" in warning_codes
    assert "FRAME_LOCATION_DEFERRED" in warning_codes
    assert "set shelf shelf-location" not in artifact.cli_text


def test_route_readiness_and_build_accept_wholly_deferred_terminal_colan() -> None:
    request = replace(
        _request(),
        management=ManagementInterface(
            enabled=False,
            name="",
            routing_mode=ROUTING_OSPF_OSC_ONLY,
            ip_address="",
            prefix_length=None,
            gateway="",
        ),
    )
    payload = encode_r40_exact_payload(request)
    project = _project(
        (
            replace(
                _shelf(payload=payload),
                primary_oam_ip=request.loopback_ip,
            ),
        )
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)
    artifact = build.shelf_builds[0].artifact
    warning_codes = {
        issue.code
        for issue in artifact.issues
        if issue.severity == "warning"
    }

    assert payload["schema_version"] == "1.4"
    assert decode_r40_exact_payload(payload) == request
    assert readiness.ready is True
    assert readiness.shelf_statuses[0].reason_codes == ()
    assert build.ready is True
    assert build.config_count == 1
    assert "TERMINAL_COLAN_DEFERRED" in warning_codes
    assert artifact.manifest["colan_policy"] == COLAN_TERMINAL_OPTIONAL
    assert artifact.manifest["colan_state"] == "deferred"
    assert artifact.manifest["colan_commands_emitted"] is False
    assert "interface colan-" not in artifact.cli_text


def test_automatic_controls_do_not_block_route_candidate_readiness() -> None:
    request = replace(
        _request(),
        installed_inventory_confirmed=False,
        planner_runtime_mop_confirmed=False,
        target_build_confirmed=False,
        greenfield_fibers_disconnected_confirmed=False,
        calibration_feature_inactive_confirmed=False,
        cfim_unused_ports_terminated_confirmed=False,
    )
    project = _project(
        (_shelf(payload=encode_r40_exact_payload(request)),)
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)
    artifact = build.shelf_builds[0].artifact

    assert readiness.ready is True
    assert build.ready is True
    assert artifact.manifest["deployment_approved"] is False
    assert artifact.manifest["on_box_validate_required"] is True
    assert artifact.manifest["deployment_control_count"] == 3
    assert all(
        control["status"] == "active_unverified"
        for control in artifact.manifest["deployment_controls"]
    )


def test_c_plus_l_route_header_blocks_stored_c_band_provider_payload() -> None:
    project = replace(
        _project(
            (_shelf(payload=encode_r40_exact_payload(_request())),)
        ),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            }
        },
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)
    status = readiness.shelf_statuses[0]

    assert readiness.ready is False
    assert status.provider_available is False
    assert status.reason_codes == (
        "R40_EXACT_PROVIDER_ROUTE_BAND_MISMATCH",
    )
    assert "route-header optical-band scope" in status.reasons[0]
    assert build.ready is False
    assert build.config_count == 0


def test_direct_shelf_c_band_evidence_overrides_c_plus_l_route_header() -> None:
    shelf = replace(
        _shelf(payload=encode_r40_exact_payload(_request())),
        source_evidence={
            "band": "C-Band",
            "fields": [
                {
                    "field": "band",
                    "normalized_value": "c",
                    "confidence": 0.99,
                    "method": "vision",
                }
            ],
        },
    )
    project = replace(
        _project((shelf,)),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            }
        },
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is True
    assert readiness.shelf_statuses[0].provider_available is True
    assert build.ready is True
    assert build.config_count == 1


def test_weak_or_inferred_shelf_band_does_not_override_route_header() -> None:
    for method, confidence in (("inferred", 0.99), ("vision", 0.84)):
        shelf = replace(
            _shelf(payload=encode_r40_exact_payload(_request())),
            source_evidence={
                "band": "c",
                "fields": [
                    {
                        "field": "band",
                        "normalized_value": "c",
                        "confidence": confidence,
                        "method": method,
                    }
                ],
            },
        )
        project = replace(
            _project((shelf,)),
            diagram_source={
                "route_header": {
                    "optical_band": "c+l",
                    "optical_band_status": "direct_supported",
                }
            },
        )

        status = project.deployment_readiness().shelf_statuses[0]

        assert status.provider_available is False
        assert status.reason_codes == (
            "R40_EXACT_PROVIDER_ROUTE_BAND_MISMATCH",
        )


def test_no_diagram_route_header_does_not_add_provider_band_guard() -> None:
    project = _project(
        (_shelf(payload=encode_r40_exact_payload(_request())),)
    )

    readiness = project.deployment_readiness()

    assert readiness.ready is True
    assert readiness.shelf_statuses[0].provider_available is True
    assert (
        "R40_EXACT_PROVIDER_ROUTE_BAND_MISMATCH"
        not in readiness.shelf_statuses[0].reason_codes
    )


def test_catalog_contains_only_the_two_audited_sra_variants() -> None:
    assert R40_PROVIDER_CATALOG
    assert {
        provider_id
        for provider_id, profile in R40_PROVIDER_CATALOG.items()
        if profile.supports_raman
    } == {
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
        R40_R2_CL_DLE_S1_SRA4,
    }


def test_exact_usxgn_sat_sra_pair_is_route_ready() -> None:
    project = _sra_pair_project()

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is True
    assert all(status.provider_available for status in readiness.shelf_statuses)
    assert build.ready is True
    assert build.config_count == 2


def test_first_sra_endpoint_review_stages_in_either_order_and_blocks_cli() -> None:
    complete = _sra_pair_project()

    for missing_index, staged_index in ((1, 0), (0, 1)):
        shelves = list(complete.shelves)
        shelves[missing_index] = replace(
            shelves[missing_index],
            profile_payload={},
        )
        project = replace(complete, shelves=tuple(shelves))

        readiness = project.deployment_readiness()
        statuses = {
            status.shelf_id: status
            for status in readiness.shelf_statuses
        }
        staged_status = statuses[
            project.shelves[staged_index].shelf_id
        ]
        missing_status = statuses[
            project.shelves[missing_index].shelf_id
        ]
        build = evaluate_route_configs(project)

        assert readiness.ready is False
        assert staged_status.ready is False
        assert staged_status.provider_available is True
        assert staged_status.reason_codes == (
            "R40_PENDING_SRA_PEER_REVIEW",
        )
        assert any(
            "SRA" in reason
            and "peer" in reason
            and "staged" in reason
            for reason in staged_status.reasons
        )
        assert missing_status.reason_codes == (
            "EXACT_PROVIDER_REVIEW_REQUIRED",
        )
        assert build.ready is False
        assert build.config_count == 0
        assert build.shelf_builds == ()
        staged_request = decode_r40_exact_payload(
            project.shelves[staged_index].profile_payload
        )
        topology_issues = _r40_route_topology_issues(
            staged_request,
            project.shelves[staged_index],
            project,
        )
        assert tuple(issue.code for issue in topology_issues) == (
            R40_PENDING_SRA_PEER_REVIEW,
        )
        assert topology_issues[0].peer_shelf_id == (
            project.shelves[missing_index].shelf_id
        )


def test_staged_sra_endpoint_state_is_derived_after_project_round_trip() -> None:
    complete = _sra_pair_project()
    staged = replace(
        complete,
        shelves=(
            complete.shelves[0],
            replace(complete.shelves[1], profile_payload={}),
        ),
    )

    restored = RouteProject.from_dict(staged.to_dict())
    readiness = restored.deployment_readiness()
    build = evaluate_route_configs(restored)

    assert restored.to_dict() == staged.to_dict()
    assert readiness.ready is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "R40_PENDING_SRA_PEER_REVIEW",
    )
    assert readiness.shelf_statuses[1].reason_codes == (
        "EXACT_PROVIDER_REVIEW_REQUIRED",
    )
    assert build.ready is False
    assert build.config_count == 0
    assert build.shelf_builds == ()


def test_first_sra_endpoint_cannot_stage_without_compatible_peer_evidence() -> None:
    complete = _sra_pair_project(sat_evidence_status="rejected")
    project = replace(
        complete,
        shelves=(
            complete.shelves[0],
            replace(
                complete.shelves[1],
                profile_payload={},
                raman_label="",
            ),
        ),
    )

    status = project.deployment_readiness().shelf_statuses[0]
    build = evaluate_route_configs(project)

    assert status.ready is False
    assert status.reason_codes == ("R40_EXACT_ROUTE_TOPOLOGY_MISMATCH",)
    assert R40_PENDING_SRA_PEER_REVIEW not in status.reason_codes
    assert any(
        "no audited SRA provider candidate compatible" in reason
        for reason in status.reasons
    )
    assert build.ready is False
    assert build.config_count == 0
    assert build.shelf_builds == ()


def test_first_sra_endpoint_cannot_stage_toward_non_r40_peer() -> None:
    complete = _sra_pair_project()
    project = replace(
        complete,
        shelves=(
            complete.shelves[0],
            replace(
                complete.shelves[1],
                profile_payload={},
                software_release="RLS R4.2",
            ),
        ),
    )

    status = project.deployment_readiness().shelf_statuses[0]

    assert status.ready is False
    assert status.reason_codes == ("R40_EXACT_ROUTE_TOPOLOGY_MISMATCH",)
    assert R40_PENDING_SRA_PEER_REVIEW not in status.reason_codes
    assert any(
        "no audited SRA provider candidate compatible" in reason
        for reason in status.reasons
    )


def test_sra_provider_requires_matching_reviewed_local_slot() -> None:
    project = _sra_pair_project()
    xgn = replace(
        project.shelves[0],
        raman_label="Slot 6",
        source_evidence=_sra_source_evidence(
            "accepted",
            shelf_tid="USXGN1-L8I2",
            slot=6,
        ),
    )
    project = replace(project, shelves=(xgn, project.shelves[1]))

    status = project.deployment_readiness().shelf_statuses[0]

    assert status.ready is False
    assert status.provider_available is False
    assert status.reason_codes == (
        "R40_EXACT_PROVIDER_SRA_SLOT_MISMATCH",
    )
    assert "R40_PENDING_SRA_PEER_REVIEW" not in status.reason_codes


def test_represented_sra_span_requires_accepted_evidence_at_both_ends() -> None:
    project = _sra_pair_project(sat_evidence_status="rejected")
    project = replace(
        project,
        shelves=(
            project.shelves[0],
            replace(project.shelves[1], raman_label=""),
        ),
    )

    statuses = project.deployment_readiness().shelf_statuses
    xgn_status = statuses[0]
    sat_status = statuses[1]

    assert "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in xgn_status.reason_codes
    assert "R40_PENDING_SRA_PEER_REVIEW" not in xgn_status.reason_codes
    assert any(
        "Both endpoints of a represented Raman span" in reason
        for reason in xgn_status.reasons
    )
    assert sat_status.reason_codes == (
        "R40_EXACT_PROVIDER_SRA_EVIDENCE_REQUIRED",
    )
    assert "R40_PENDING_SRA_PEER_REVIEW" not in sat_status.reason_codes


def test_present_nonreciprocal_sra_peer_is_hard_failure_not_pending() -> None:
    project = _sra_pair_project()
    sat_request = decode_r40_exact_payload(
        project.shelves[1].profile_payload
    )
    assert sat_request.line_1 is not None
    nonreciprocal_sat = replace(
        sat_request,
        line_1=replace(
            sat_request.line_1,
            neighbor_line_mux_pfg="NOT-THE-FACING-PFG",
        ),
    )
    project = replace(
        project,
        shelves=(
            project.shelves[0],
            replace(
                project.shelves[1],
                profile_payload=encode_r40_exact_payload(
                    nonreciprocal_sat
                ),
            ),
        ),
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is False
    assert all(
        "R40_PENDING_SRA_PEER_REVIEW" not in status.reason_codes
        for status in readiness.shelf_statuses
    )
    assert all(
        "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in status.reason_codes
        for status in readiness.shelf_statuses
    )
    assert any(
        "not reciprocal" in reason
        or "neighbor PFG identities" in reason
        for status in readiness.shelf_statuses
        for reason in status.reasons
    )
    assert build.ready is False
    assert build.config_count == 0
    assert build.shelf_builds == ()


def test_single_shelf_sra_provider_has_unrepresented_degree() -> None:
    pair = _sra_pair_project()
    sat = pair.shelves[1]
    project = replace(
        _project((replace(sat, site_key="site-1"),)),
        sites=(Site("site-1", "S02", "Site 02"),),
    )

    status = project.deployment_readiness().shelf_statuses[0]

    assert status.ready is False
    assert status.reason_codes == ("R40_EXACT_ROUTE_TOPOLOGY_MISMATCH",)
    assert any("unmodeled A-side degree" in reason for reason in status.reasons)


def test_endpoint_ila_sra_output_cannot_face_away_from_only_neighbor() -> None:
    project = _sra_pair_project(ila_line_1_side="A")

    status = project.deployment_readiness().shelf_statuses[0]

    assert status.ready is False
    assert "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in status.reason_codes
    assert "R40_PENDING_SRA_PEER_REVIEW" not in status.reason_codes
    assert any("unmodeled A-side degree" in reason for reason in status.reasons)


def test_pending_structured_sra_endpoint_blocks_exact_provider() -> None:
    shelf = replace(
        _shelf(payload=encode_r40_exact_payload(_request())),
        raman_label="Slot 6",
        source_evidence=_sra_source_evidence("pending"),
    )

    readiness = _project((shelf,)).deployment_readiness()

    assert readiness.ready is False
    assert readiness.shelf_statuses[0].provider_available is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "PENDING_RAMAN_CALLOUT_REVIEW",
    )


def test_accepted_sra_endpoint_conflicts_with_no_sra_provider() -> None:
    shelf = replace(
        _shelf(payload=encode_r40_exact_payload(_request())),
        raman_label="Slot 6",
        source_evidence=_sra_source_evidence("accepted"),
    )
    project = _project((shelf,))

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is False
    assert readiness.shelf_statuses[0].provider_available is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "R40_EXACT_PROVIDER_SRA_CONFLICT",
    )
    assert build.ready is False
    assert build.config_count == 0


def test_accepted_sra_without_payload_reports_missing_provider_capability() -> None:
    shelf = replace(
        _shelf(),
        raman_label="Slot 6",
        source_evidence=_sra_source_evidence("accepted"),
    )

    status = _project((shelf,)).deployment_readiness().shelf_statuses[0]

    assert status.ready is False
    assert status.provider_available is False
    assert status.reason_codes == (
        "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE",
    )
    assert "no vendor-audited SRA-capable" in status.reasons[0]
    assert "no-SRA provider" in status.reasons[0]


def test_rejected_sra_callout_preserves_evidence_and_allows_no_sra_provider() -> None:
    evidence = _sra_source_evidence("rejected")
    shelf = replace(
        _shelf(payload=encode_r40_exact_payload(_request())),
        raman_label="",
        source_evidence=evidence,
    )
    project = _project((shelf,))

    readiness = project.deployment_readiness()

    assert readiness.ready is True
    assert readiness.shelf_statuses[0].provider_available is True
    assert project.shelves[0].source_evidence["raman_callouts"]


def test_generic_r40_role_without_exact_payload_remains_blocked() -> None:
    readiness = _project((_shelf(),)).deployment_readiness()

    assert readiness.ready is False
    assert readiness.shelf_statuses[0].provider_available is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "EXACT_PROVIDER_REVIEW_REQUIRED",
    )


def test_exact_r40_payload_identity_is_cross_checked_against_route() -> None:
    payload = encode_r40_exact_payload(
        _request(
            shelf_name="OTHER-01",
            site_name="S02",
            software_release="RLS R4.0",
            ospf_area="0.0.0.2",
        )
    )
    readiness = _project((_shelf(payload=payload),)).deployment_readiness()

    assert readiness.ready is False
    status = readiness.shelf_statuses[0]
    assert "R40_EXACT_PAYLOAD_IDENTITY_MISMATCH" in status.reason_codes
    assert any("shelf_name" in reason for reason in status.reasons)
    assert any("site_name" in reason for reason in status.reasons)
    assert any("OSPF area" in reason for reason in status.reasons)


def test_exact_r40_direction_facts_are_cross_checked_against_modeled_paths() -> None:
    middle_request = _request(
        shelf_name="RLS-02",
        site_name="S02",
        member_name="RLS-02",
        hostname="rls-02.example.net",
        loopback_ip="198.51.100.20",
        management=replace(
            _request().management,
            ip_address="192.0.2.2",
        ),
        line_1=replace(
            _request().line_1,
            neighbor_node="RLS-01",
            expected_loss_db=14.0,
        ),
        line_2=replace(
            _request().line_2,
            neighbor_node="RLS-03",
            expected_loss_db=13.0,
        ),
    )
    shelves = (
        _shelf(),
        replace(
            _shelf(payload=encode_r40_exact_payload(middle_request)),
            shelf_id="shelf-02",
            site_key="site-2",
            tid="RLS-02",
            primary_oam_ip="192.0.2.2",
        ),
        replace(
            _shelf(),
            shelf_id="shelf-03",
            site_key="site-2",
            tid="RLS-03",
            primary_oam_ip="192.0.2.3",
        ),
    )
    links = (
        RouteLink(
            link_id="link-1",
            order=1,
            from_shelf_id="shelf-01",
            to_shelf_id="shelf-02",
            paths=(
                OpticalPath(
                    path_id="path-1",
                    path_role="route",
                    link_name="A-B",
                    expected_loss_db=12.0,
                    fiber_type="NDSF",
                    review_state="confirmed",
                    source_evidence=_route_native_fiber_evidence(),
                    endpoint_reviews=(
                        PathEndpointReview(
                            shelf_id="shelf-02",
                            link_name="LINE-1-OUT",
                            expected_loss_db=12.0,
                            fiber_type="NDSF",
                        ),
                    ),
                ),
            ),
        ),
        RouteLink(
            link_id="link-2",
            order=2,
            from_shelf_id="shelf-02",
            to_shelf_id="shelf-03",
            paths=(
                OpticalPath(
                    path_id="path-2",
                    path_role="route",
                    link_name="B-C",
                    expected_loss_db=13.0,
                    fiber_type="NDSF",
                    review_state="confirmed",
                    source_evidence=_route_native_fiber_evidence(),
                    endpoint_reviews=(
                        PathEndpointReview(
                            shelf_id="shelf-02",
                            link_name="LINE-2-OUT",
                            expected_loss_db=13.0,
                            fiber_type="NDSF",
                        ),
                    ),
                ),
            ),
        ),
    )
    project = replace(_project(shelves), links=links)

    status = next(
        item
        for item in project.deployment_readiness().shelf_statuses
        if item.shelf_id == "shelf-02"
    )

    assert "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in status.reason_codes
    assert any("expected loss" in reason for reason in status.reasons)


def test_asymmetric_endpoint_losses_and_local_link_names_remain_ready() -> None:
    project = _two_shelf_exact_project(a_loss=12.25, z_loss=13.75)

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is True
    assert build.ready is True
    assert build.config_count == 2
    path = project.links[0].paths[0]
    assert path.link_name == "CUSTOMER-SPAN-A-Z"
    assert {
        (review.shelf_id, review.link_name, review.expected_loss_db)
        for review in path.endpoint_reviews
    } == {
        ("shelf-a", "A-LOCAL-LINK", 12.25),
        ("shelf-z", "Z-LOCAL-LINK", 13.75),
    }


def test_one_provider_direction_cannot_override_route_native_fiber() -> None:
    project = _two_shelf_exact_project()
    a_request = decode_r40_exact_payload(
        project.shelves[0].profile_payload
    )
    mismatched_request = replace(
        a_request,
        line_1=replace(a_request.line_1, fiber_type="DSF"),
    )
    project = replace(
        project,
        shelves=(
            replace(
                project.shelves[0],
                profile_payload=encode_r40_exact_payload(
                    mismatched_request
                ),
            ),
            project.shelves[1],
        ),
    )

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)
    a_status = next(
        status
        for status in readiness.shelf_statuses
        if status.shelf_id == "shelf-a"
    )

    assert readiness.ready is False
    assert build.ready is False
    assert build.shelf_builds == ()
    assert "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in a_status.reason_codes
    assert any("fiber type" in reason for reason in a_status.reasons)


def test_unreviewed_optical_path_blocks_atomic_config_export() -> None:
    project = _two_shelf_exact_project(path_review_state="pending")

    readiness = project.deployment_readiness()
    build = evaluate_route_configs(project)

    assert readiness.ready is False
    assert build.ready is False
    assert build.shelf_builds == ()
    assert all(
        "UNREVIEWED_OPTICAL_PATH" in status.reason_codes
        for status in readiness.shelf_statuses
    )


def test_missing_reverse_egress_review_blocks_only_its_transmitting_shelf() -> None:
    project = _two_shelf_exact_project()
    path = project.links[0].paths[0]
    project = replace(
        project,
        links=(
            replace(
                project.links[0],
                paths=(
                    replace(
                        path,
                        endpoint_reviews=(path.endpoint_reviews[0],),
                    ),
                ),
            ),
        ),
    )

    readiness = project.deployment_readiness()
    statuses = {
        status.shelf_id: status
        for status in readiness.shelf_statuses
    }

    assert statuses["shelf-a"].ready is True
    assert statuses["shelf-z"].ready is False
    assert "PROPAGATION_PATH_INCOMPLETE" in (
        statuses["shelf-z"].reason_codes
    )
    assert any(
        "Z→A egress engineering" in reason
        for reason in statuses["shelf-z"].reasons
    )


def test_endpoint_local_cli_link_name_is_cross_checked() -> None:
    project = _two_shelf_exact_project(
        z_endpoint_link_name="STALE-Z-LINK"
    )

    readiness = project.deployment_readiness()
    z_status = next(
        status
        for status in readiness.shelf_statuses
        if status.shelf_id == "shelf-z"
    )

    assert readiness.ready is False
    assert "R40_EXACT_ROUTE_TOPOLOGY_MISMATCH" in z_status.reason_codes
    assert any("CLI link name" in reason for reason in z_status.reasons)


def test_r4_2_profile_and_release_are_explicitly_rejected() -> None:
    removed_r42 = replace(
        _shelf(
            profile_id="protected_dci",
            release="RLS R4.2",
        ),
        shelf_id="shelf-02",
        site_key="site-2",
        tid="RLS-02",
        primary_oam_ip="192.0.2.2",
    )
    project = _project((removed_r42,))

    error_codes = {
        issue.code for issue in project.validate() if issue.is_error
    }
    assert {"UNKNOWN_PROFILE", "UNSUPPORTED_SOFTWARE_RELEASE"} <= error_codes
    readiness = project.deployment_readiness()
    assert readiness.ready is False
    assert readiness.shelf_statuses[0].reason_codes == ("UNKNOWN_PROFILE",)


def test_bundle_config_stems_use_each_artifact_release(tmp_path) -> None:
    def artifact(release: str) -> SimpleNamespace:
        return SimpleNamespace(
            cli_text="batch\nvalidate\ncommit\nquit\n",
            annotated_text="annotated\n",
            validation_report="valid\n",
            manifest={
                "release": release,
                "generator": "R40ExactConfigGenerator",
                "provider_id": R40_CDA_RLA12_C_2DEG_NO_SRA,
            },
        )

    readiness = DeploymentReadiness(ready=True, shelf_statuses=())
    build = RouteConfigBuild(
        project_fingerprint="a" * 64,
        ready=True,
        readiness=readiness,
        shelf_builds=(
            ShelfConfigBuild(
                1, "shelf-01", "add_drop", "RLS-01", artifact("RLS R4.0")
            ),
            ShelfConfigBuild(
                2,
                "shelf-02",
                "roadm",
                "RLS-02",
                artifact("RLS R4.0"),
            ),
        ),
    )

    records = _write_config_candidates(build, tmp_path / "configs")
    filenames = {
        record["files"]["cli"]["filename"] for record in records
    }

    assert "RLS-01_RLS_R4.0_add_drop.cli" in filenames
    assert "RLS-02_RLS_R4.0_roadm.cli" in filenames
    assert all("R4.2" not in filename for filename in filenames)
