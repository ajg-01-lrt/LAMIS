"""Tests for the mixed-shelf Ciena RLS route planning model."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.rls_config.r4_0_generator import (
    R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
    R40_PAYLOAD_SCHEMA_ID,
    R40_PROVIDER_CATALOG,
    R40LinePath,
)
from utils.rls_config.route_project import (
    PROFILE_REGISTRY,
    REVIEW_STATES,
    ROUTE_SCHEMA_NAME,
    ROUTE_SCHEMA_VERSION,
    OpticalPath,
    PathEndpointReview,
    RouteLink,
    RouteProject,
    RouteProjectFormatError,
    RouteProjectValidationError,
    ShelfInstance,
    Site,
    load_route_project,
    load_route_project_draft,
    route_link_propagation_views,
    route_native_fiber_review,
    route_project_fingerprint,
    save_route_project,
    save_route_project_draft,
    _provider_available_for_shelf,
    _r40_route_topology_mismatches,
)


def _site(index: int) -> Site:
    return Site(
        site_key=f"site-{index}",
        code=f"S{index:02}",
        name=f"Route Site {index}",
        address=f"{index} Network Way",
        network_site_id=f"NSI-{index:03}",
    )


def _shelf(
    index: int,
    *,
    profile_id: str = "ila",
    site_key: str | None = None,
    software_release: str | None = None,
    shelf_variant: str | None = None,
    profile_payload: dict[str, object] | None = None,
    review_state: str = "manual",
    source_evidence: dict[str, object] | None = None,
) -> ShelfInstance:
    profile = PROFILE_REGISTRY.get(profile_id)
    if software_release is None:
        software_release = "RLS R4.0"
    if shelf_variant is None:
        shelf_variant = (
            profile.variant_hint
            if profile is not None and profile.variant_hint
            else "PROTECTED-C-L"
        )
    return ShelfInstance(
        shelf_id=f"shelf-{index:02}",
        profile_id=profile_id,
        software_release=software_release,
        shelf_variant=shelf_variant,
        site_key=site_key or f"site-{index}",
        tid=f"RLS-{index:02}",
        primary_oam_ip=f"192.0.2.{index}",
        power_label="A/B -48 VDC",
        raman_label="Slot 4" if index % 2 else "",
        notes=f"Shelf {index}",
        profile_payload=profile_payload or {},
        review_state=review_state,  # type: ignore[arg-type]
        source_evidence=source_evidence or {},
    )


def _project(
    shelves: tuple[ShelfInstance, ...] | None = None,
    sites: tuple[Site, ...] | None = None,
    diagram_source: dict[str, object] | None = None,
    ospf_area: str = "",
    links: tuple[RouteLink, ...] | None = None,
) -> RouteProject:
    if shelves is None:
        shelves = (
            _shelf(1, profile_id="add_drop_a"),
            _shelf(2, profile_id="ila"),
            _shelf(3, profile_id="roadm_z", site_key="site-4"),
            _shelf(4, profile_id="add_drop_z"),
        )
    if sites is None:
        sites = tuple(_site(index) for index in range(1, 5))
    return RouteProject(
        project_id="project-chi-dal",
        route_code="CHI-DAL",
        title="Chicago to Dallas RLS Route",
        ospf_area=ospf_area,
        revision="A",
        notes="Construction deliverable",
        sites=sites,
        shelves=shelves,
        links=links or (),
        diagram_source=diagram_source or {},
    )


def _path(
    index: int,
    *,
    role: str = "route",
    review_state: str = "manual",
    source_evidence: dict[str, object] | None = None,
) -> OpticalPath:
    return OpticalPath(
        path_id=f"path-{index:02}",
        path_role=role,
        link_name=f"LINK-{index:02}",
        expected_loss_db=12.5 + index,
        distance_km=50.0 + index,
        fiber_type="NDSF",
        circuit_id="BDJW7353",
        fiber_start=index * 2 - 1,
        fiber_end=index * 2,
        review_state=review_state,  # type: ignore[arg-type]
        source_evidence=source_evidence or {},
    )


def _links_for(shelves: tuple[ShelfInstance, ...]) -> tuple[RouteLink, ...]:
    return tuple(
        RouteLink(
            link_id=f"link-{index + 1:02}",
            order=index + 1,
            from_shelf_id=shelves[index].shelf_id,
            to_shelf_id=shelves[index + 1].shelf_id,
            paths=(_path(index + 1),),
        )
        for index in range(len(shelves) - 1)
    )


def _route_native_fiber_marker(token: str = "NDSF") -> dict[str, object]:
    return {
        "value": token,
        "scope": "all_active_route_spans",
        "action": "operator_apply_route_native_fiber",
        "status": "confirmed",
        "deployable_cli": False,
    }


def _reviewed_route_native_path(
    path: OpticalPath,
    link: RouteLink,
    token: str = "NDSF",
    *,
    endpoint_tokens: tuple[str, str] | None = None,
) -> OpticalPath:
    endpoint_tokens = endpoint_tokens or (token, token)
    return replace(
        path,
        fiber_type=token,
        review_state="confirmed",
        source_evidence={
            "route_native_fiber_review": _route_native_fiber_marker(token)
        },
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id=link.from_shelf_id,
                link_name=path.link_name,
                expected_loss_db=path.expected_loss_db or 0.0,
                fiber_type=endpoint_tokens[0],
            ),
            PathEndpointReview(
                shelf_id=link.to_shelf_id,
                link_name=path.link_name,
                expected_loss_db=path.expected_loss_db or 0.0,
                fiber_type=endpoint_tokens[1],
            ),
        ),
    )


def _one_degree_line(
    *,
    neighbor_node: str,
    link_name: str = "RLA-LINEOUT",
) -> R40LinePath:
    return R40LinePath(
        link_name=link_name,
        neighbor_node=neighbor_node,
        neighbor_line_mux_pfg="LM1",
        neighbor_line_demux_pfg="LD1",
        fiber_type="NDSF",
        expected_loss_db=14.0,
    )


def _one_degree_request(
    *,
    line_1_route_side: str,
    neighbor_node: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        provider_id=R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
        line_1_route_side=line_1_route_side,
        line_1=_one_degree_line(neighbor_node=neighbor_node),
        line_2=None,
    )


def _one_degree_terminal_project(
    *,
    peer_payload: dict[str, object] | None = None,
) -> tuple[RouteProject, ShelfInstance, ShelfInstance]:
    terminal = replace(
        _shelf(1, profile_id="roadm_a"),
        raman_label="",
    )
    peer = replace(
        _shelf(
            2,
            profile_id="roadm_z",
            profile_payload=peer_payload,
        ),
        raman_label="",
    )
    path = OpticalPath(
        path_id="path-one-degree",
        path_role="route",
        link_name="CUSTOMER-CIRCUIT",
        expected_loss_db=14.0,
        fiber_type="NDSF",
        review_state="confirmed",
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id=terminal.shelf_id,
                link_name="RLA-LINEOUT",
                expected_loss_db=14.0,
                fiber_type="NDSF",
            ),
            PathEndpointReview(
                shelf_id=peer.shelf_id,
                link_name="RLA-LINEOUT",
                expected_loss_db=14.0,
                fiber_type="NDSF",
            ),
        ),
    )
    project = _project(
        shelves=(terminal, peer),
        sites=(_site(1), _site(2)),
        links=(
            RouteLink(
                link_id="link-one-degree",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=peer.shelf_id,
                paths=(path,),
            ),
        ),
    )
    return project, terminal, peer


def test_path_endpoint_reviews_round_trip_without_collapsing_directions() -> None:
    path = replace(
        _path(1, review_state="confirmed"),
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id="shelf-01",
                link_name="A-LOCAL-LINK",
                expected_loss_db=12.25,
                fiber_type="NDSF",
            ),
            PathEndpointReview(
                shelf_id="shelf-02",
                link_name="Z-LOCAL-LINK",
                expected_loss_db=13.75,
                fiber_type="NDSF",
            ),
        ),
    )

    restored = OpticalPath.from_dict(path.to_dict())

    assert restored == path
    assert restored.endpoint_reviews[0].expected_loss_db == 12.25
    assert restored.endpoint_reviews[1].expected_loss_db == 13.75


def test_single_physical_span_exposes_both_propagation_directions() -> None:
    span = replace(
        _path(1, review_state="confirmed"),
        endpoint_reviews=(
            PathEndpointReview(
                shelf_id="shelf-01",
                link_name="A-TO-Z-LINK",
                expected_loss_db=12.25,
                fiber_type="LEAF",
            ),
            PathEndpointReview(
                shelf_id="shelf-02",
                link_name="Z-TO-A-LINK",
                expected_loss_db=13.75,
                fiber_type="LEAF",
            ),
        ),
    )
    link = RouteLink(
        link_id="link-01",
        order=1,
        from_shelf_id="shelf-01",
        to_shelf_id="shelf-02",
        paths=(span,),
    )
    serialized_before = link.to_dict()

    views = route_link_propagation_views(link)

    assert tuple(view.direction for view in views) == ("A_TO_Z", "Z_TO_A")
    assert (
        views[0].egress_shelf_id,
        views[0].ingress_shelf_id,
        views[0].egress_review,
    ) == ("shelf-01", "shelf-02", span.endpoint_reviews[0])
    assert (
        views[1].egress_shelf_id,
        views[1].ingress_shelf_id,
        views[1].egress_review,
    ) == ("shelf-02", "shelf-01", span.endpoint_reviews[1])
    assert views[0].shared_span is span
    assert views[1].shared_span is span
    assert link.to_dict() == serialized_before


def test_one_degree_terminal_uses_only_assigned_line_1_for_physical_span() -> None:
    project, terminal, peer = _one_degree_terminal_project()
    request = _one_degree_request(
        line_1_route_side="Z",
        neighbor_node=peer.tid,
    )

    assert _r40_route_topology_mismatches(
        request,
        terminal,
        project,
    ) == []

    wrong_side = SimpleNamespace(
        **{
            **vars(request),
            "line_1_route_side": "A",
        }
    )
    reasons = _r40_route_topology_mismatches(
        wrong_side,
        terminal,
        project,
    )

    assert reasons == [
        "The selected exact R4.0 provider has no physical line record "
        "assigned to the shelf's Z-side adjacent span."
    ]


def test_one_degree_peer_reciprocity_never_dereferences_line_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_payload = {"schema_id": R40_PAYLOAD_SCHEMA_ID}
    project, terminal, peer = _one_degree_terminal_project(
        peer_payload=peer_payload
    )
    local_request = _one_degree_request(
        line_1_route_side="Z",
        neighbor_node=peer.tid,
    )
    peer_request = _one_degree_request(
        line_1_route_side="A",
        neighbor_node=terminal.tid,
    )
    monkeypatch.setattr(
        "utils.rls_config.r4_0_generator.decode_r40_exact_payload",
        lambda _payload: peer_request,
    )

    assert _r40_route_topology_mismatches(
        local_request,
        terminal,
        project,
    ) == []

    peer_request.line_1_route_side = "Z"
    reasons = _r40_route_topology_mismatches(
        local_request,
        terminal,
        project,
    )

    assert reasons == [
        "R4.0 local line-output 1 facing peer has no provisioned reciprocal "
        "physical line record on the peer's facing route side."
    ]


def test_provider_availability_requires_selected_provider_to_support_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"schema_id": R40_PAYLOAD_SCHEMA_ID}
    shelf = replace(
        _shelf(
            1,
            profile_id="ila",
            profile_payload=payload,
        ),
        raman_label="",
    )
    project = _project(shelves=(shelf,), sites=(_site(1),))
    mismatched_request = _one_degree_request(
        line_1_route_side="Z",
        neighbor_node="RLS-02",
    )
    monkeypatch.setattr(
        "utils.rls_config.r4_0_generator.decode_r40_exact_payload",
        lambda _payload: mismatched_request,
    )

    assert (
        _provider_available_for_shelf(
            PROFILE_REGISTRY[shelf.profile_id],
            shelf,
            project,
        )
        is False
    )


def test_propagation_views_leave_missing_endpoint_review_missing() -> None:
    from_review = PathEndpointReview(
        shelf_id="shelf-01",
        link_name="A-TO-Z-LINK",
        expected_loss_db=12.25,
        fiber_type="NDSF",
    )
    span = replace(_path(1), endpoint_reviews=(from_review,))
    link = RouteLink(
        link_id="link-01",
        order=1,
        from_shelf_id="shelf-01",
        to_shelf_id="shelf-02",
        paths=(span,),
    )

    a_to_z, z_to_a = route_link_propagation_views(link)

    assert a_to_z.egress_review is from_review
    assert z_to_a.egress_review is None
    assert z_to_a.shared_span is span


def test_propagation_views_fail_closed_for_ambiguous_physical_paths() -> None:
    common = {
        "link_id": "link-01",
        "order": 1,
        "from_shelf_id": "shelf-01",
        "to_shelf_id": "shelf-02",
    }

    assert route_link_propagation_views(RouteLink(**common, paths=())) == ()
    assert (
        route_link_propagation_views(
            RouteLink(**common, paths=(_path(1), _path(2)))
        )
        == ()
    )


def _codes(project: RouteProject, severity: str | None = None) -> set[str]:
    return {
        issue.code
        for issue in project.validate()
        if severity is None or issue.severity == severity
    }


def test_profile_registry_has_all_route_types_and_explicit_cli_status():
    assert tuple(PROFILE_REGISTRY) == (
        "add_drop_a",
        "add_drop_z",
        "add_drop",
        "ila",
        "roadm_a",
        "roadm_z",
        "roadm",
    )
    assert PROFILE_REGISTRY["add_drop_a"].planning_only is True
    assert PROFILE_REGISTRY["add_drop_a"].variant_hint is None
    assert PROFILE_REGISTRY["ila"].release_family_hint == "RLS R4.0"
    assert PROFILE_REGISTRY["ila"].variant_hint is None
    assert PROFILE_REGISTRY["roadm_z"].variant_hint is None


def test_valid_planning_project_has_warnings_but_no_errors():
    project = _project()

    assert not [issue for issue in project.validate() if issue.is_error]
    planning = [
        issue
        for issue in project.validate()
        if issue.code == "PLANNING_ONLY_PROFILE"
    ]
    assert len(planning) == len(project.shelves)
    project.assert_valid()


def test_rack_groups_preserve_route_order_and_never_exceed_eight():
    sites = tuple(_site(index) for index in range(1, 18))
    shelves = tuple(_shelf(index) for index in range(1, 18))
    project = _project(shelves=shelves, sites=sites)

    groups = project.rack_groups()

    assert tuple(map(len, groups)) == (8, 8, 1)
    assert project.rack_count == 3
    assert [
        shelf.shelf_id for group in groups for shelf in group
    ] == [shelf.shelf_id for shelf in shelves]
    assert all(len(group) <= 8 for group in groups)
    with pytest.raises(ValueError, match="between 1 and 8"):
        project.rack_groups(9)
    with pytest.raises(TypeError, match="integer"):
        project.rack_groups(True)


def test_irm_counts_are_derived_from_ordered_shelves_not_site_inventory():
    sites = tuple(_site(index) for index in range(1, 8))
    shelves = (
        _shelf(1, profile_id="add_drop_a"),
        _shelf(2, profile_id="roadm_a", site_key="site-1"),
        _shelf(3, profile_id="ila", site_key="site-2"),
        _shelf(4, profile_id="ila", site_key="site-3"),
        _shelf(5, profile_id="unknown_role", site_key="site-4"),
        _shelf(6, profile_id="roadm_z", site_key="site-5"),
        _shelf(7, profile_id="add_drop_z", site_key="site-5"),
    )
    project = _project(shelves=shelves, sites=sites)

    counts = project.irm_counts()

    assert counts.total_distinct_sites == 5
    assert counts.ila_shelves == 2
    assert counts.roadm_shelves == 2
    assert counts.add_drop_a_shelves == 1
    assert counts.add_drop_z_shelves == 1
    assert counts.add_drop_unassigned_shelves == 0
    assert counts.total_add_drop_shelves == 2
    assert counts.unmapped_shelves == 1
    assert counts.total_shelves == 7
    assert counts.as_irm_drivers() == {
        "B6": 5,
        "B7": 2,
        "B9": 2,
        "B10": 2,
        "F18": 2,
        "G18": 2,
        "H18": 1,
        "I18": 1,
    }


def test_neutral_intermediate_profiles_preserve_route_and_count_honestly():
    project = _project(
        shelves=(
            _shelf(1, profile_id="roadm_a", site_key="site-1"),
            _shelf(2, profile_id="add_drop", site_key="site-2"),
            _shelf(3, profile_id="roadm", site_key="site-2"),
            _shelf(4, profile_id="roadm_z", site_key="site-3"),
        ),
        sites=(_site(1), _site(2), _site(3)),
    )

    counts = project.irm_counts()

    assert not [issue for issue in project.validate() if issue.is_error]
    assert counts.roadm_shelves == 3
    assert counts.add_drop_unassigned_shelves == 1
    assert counts.total_add_drop_shelves == 1
    assert counts.unmapped_shelves == 1
    assert counts.as_irm_drivers()["B10"] == 1
    assert counts.as_irm_drivers()["H18"] == 0
    assert counts.as_irm_drivers()["I18"] == 0


def test_validation_catches_duplicate_identity_references_and_bad_ip():
    sites = (
        _site(1),
        replace(_site(2), site_key="SITE-1", code="s01"),
    )
    shelves = (
        _shelf(1),
        replace(
            _shelf(2),
            shelf_id="SHELF-01",
            tid="rls-01",
            primary_oam_ip="not-an-ip",
            site_key="missing",
            power_label="",
        ),
    )
    project = _project(shelves=shelves, sites=sites)

    codes = _codes(project, "error")

    assert {
        "DUPLICATE_SITE_KEY",
        "DUPLICATE_SITE_CODE",
        "DUPLICATE_SHELF_ID",
        "DUPLICATE_TID",
        "UNKNOWN_SITE",
        "INVALID_OAM_IP",
        "REQUIRED_FIELD",
    } <= codes
    with pytest.raises(RouteProjectValidationError) as exc_info:
        project.assert_valid()
    assert all(issue.is_error for issue in exc_info.value.issues)


def test_duplicate_oam_ip_uses_canonical_ip_representation():
    sites = (_site(1), _site(2))
    shelves = (
        replace(_shelf(1), primary_oam_ip="2001:db8::1"),
        replace(
            _shelf(2),
            primary_oam_ip="2001:0db8:0000:0000:0000:0000:0000:0001",
        ),
    )
    project = _project(shelves=shelves, sites=sites)

    assert "DUPLICATE_OAM_IP" in _codes(project, "error")


def test_r4_2_release_is_rejected_by_validation_and_readiness():
    project = _project(
        shelves=(
            _shelf(
                1,
                profile_id="ila",
                software_release="RLS R4.2",
            ),
        ),
        sites=(_site(1),),
    )

    issues = project.validate()

    assert any(
        issue.code == "UNSUPPORTED_SOFTWARE_RELEASE" and issue.is_error
        for issue in issues
    )
    status = project.deployment_readiness().shelf_statuses[0]
    assert status.ready is False
    assert status.reason_codes == ("EXACT_PROVIDER_REVIEW_REQUIRED",)


def test_nested_secret_bearing_payload_keys_are_rejected_without_value_leak(
    tmp_path: Path,
):
    payload = {
        "safe": {
            "private_key": "SUPER-SENSITIVE",
            "nested": [{"snmpCommunity": "also-sensitive"}],
        }
    }
    project = _project(
        shelves=(_shelf(1, profile_payload=payload),),
        sites=(_site(1),),
    )

    secret_issues = [
        issue
        for issue in project.validate()
        if issue.code == "SECRET_FIELD_NOT_ALLOWED"
    ]

    assert len(secret_issues) == 2
    assert all("SUPER-SENSITIVE" not in issue.message for issue in secret_issues)
    assert all("also-sensitive" not in issue.message for issue in secret_issues)
    with pytest.raises(RouteProjectValidationError):
        save_route_project(project, tmp_path / "must-not-save.json")
    assert not (tmp_path / "must-not-save.json").exists()


def test_profile_payload_is_deeply_immutable_and_json_round_trips():
    original = {
        "schema_id": "planning.example",
        "schema_version": "1",
        "nested": {"values": [1, 2]},
    }
    shelf = _shelf(1, profile_payload=original)
    original["nested"]["values"].append(3)  # type: ignore[index, union-attr]

    assert shelf.to_dict()["profile_payload"]["nested"]["values"] == [1, 2]
    with pytest.raises(TypeError):
        shelf.profile_payload["new"] = "value"  # type: ignore[index]


def test_diagram_provenance_and_review_state_round_trip_deeply_immutable():
    diagram_source = {
        "source_name": "customer-route.docx",
        "source_sha256": "abc123",
        "embedded_images": [{"index": 1, "sha256": "image-abc"}],
        "route_title_derivation": {
            "rule_id": "terminal-site-route-title-v1",
            "status": "controlled_derivation",
            "value": "ELP1-SAT4",
            "endpoint_tids": ["USELP1-L8R2", "USSAT4-L8R3"],
            "endpoint_codes": ["USELP1", "USSAT4"],
            "display_codes": ["ELP1", "SAT4"],
            "source_sha256": "abc123",
            "deployable_cli": False,
        },
    }
    source_evidence = {
        "image_index": 1,
        "fields": [
            {
                "name": "tid",
                "raw_text": "RLS-01",
                "confidence": 0.97,
                "bbox": [10, 20, 100, 40],
            }
        ],
    }
    shelf = _shelf(
        1,
        review_state="corrected",
        source_evidence=source_evidence,
    )
    project = _project(
        shelves=(shelf,),
        sites=(_site(1),),
        diagram_source=diagram_source,
    )

    diagram_source["embedded_images"][0]["sha256"] = "mutated"  # type: ignore[index]
    source_evidence["fields"][0]["raw_text"] = "mutated"  # type: ignore[index]

    raw = project.to_dict()
    assert raw["diagram_source"]["embedded_images"][0]["sha256"] == "image-abc"
    assert raw["shelves"][0]["review_state"] == "corrected"
    assert (
        raw["shelves"][0]["source_evidence"]["fields"][0]["raw_text"]
        == "RLS-01"
    )
    assert RouteProject.from_dict(raw) == project
    assert not any(
        issue.code == "SECRET_FIELD_NOT_ALLOWED"
        for issue in project.validate()
    )
    with pytest.raises(TypeError):
        project.diagram_source["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        shelf.source_evidence["new"] = "value"  # type: ignore[index]


def test_ospf_and_parallel_optical_paths_round_trip_deeply_immutable():
    shelves = (_shelf(1), _shelf(2))
    evidence = {
        "image_index": 2,
        "bbox": [0.1, 0.2, 0.3, 0.4],
        "confidence": 0.98,
    }
    first_path = _path(1, role="line_1", source_evidence=evidence)
    second_path = _path(2, role="line_2")
    links = (
        RouteLink(
            link_id="link-01",
            order=1,
            from_shelf_id=shelves[0].shelf_id,
            to_shelf_id=shelves[1].shelf_id,
            paths=(first_path, second_path),
        ),
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
        ospf_area="10.6.8.0",
        links=links,
    )

    evidence["bbox"][0] = 0.9  # type: ignore[index]
    raw = project.to_dict()

    assert not [issue for issue in project.validate() if issue.is_error]
    assert raw["ospf_area"] == "10.6.8.0"
    assert raw["links"][0]["paths"][0]["source_evidence"]["bbox"][0] == 0.1
    assert RouteProject.from_dict(raw) == project
    with pytest.raises(TypeError):
        first_path.source_evidence["new"] = "value"  # type: ignore[index]


def test_link_validation_enforces_count_order_endpoints_and_paths():
    shelves = (_shelf(1), _shelf(2), _shelf(3))
    links = (
        RouteLink(
            link_id="link-01",
            order=2,
            from_shelf_id=shelves[1].shelf_id,
            to_shelf_id=shelves[0].shelf_id,
            paths=(),
        ),
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2), _site(3)),
        links=links,
    )

    assert {
        "LINK_COUNT_MISMATCH",
        "INVALID_LINK_ORDER",
        "LINK_ENDPOINT_ORDER_MISMATCH",
        "NO_LINK_PATHS",
    } <= _codes(project, "error")

    unknown_and_self = replace(
        project,
        links=(
            RouteLink(
                link_id="link-01",
                order=1,
                from_shelf_id="missing-shelf",
                to_shelf_id="missing-shelf",
                paths=(_path(1),),
            ),
            RouteLink(
                link_id="LINK-01",
                order=2,
                from_shelf_id=shelves[1].shelf_id,
                to_shelf_id=shelves[2].shelf_id,
                paths=(replace(_path(1), path_id="PATH-01"),),
            ),
        ),
    )
    assert {
        "DUPLICATE_LINK_ID",
        "DUPLICATE_PATH_ID",
        "UNKNOWN_LINK_ENDPOINT",
        "SELF_LINK",
        "LINK_ENDPOINT_ORDER_MISMATCH",
    } <= _codes(unknown_and_self, "error")


def test_path_validation_rejects_invalid_values_review_and_evidence(
    tmp_path: Path,
):
    shelves = (_shelf(1), _shelf(2))
    invalid_path = OpticalPath(
        path_id="",
        path_role="",
        expected_loss_db=-1.0,
        distance_km=float("nan"),
        fiber_start=True,  # type: ignore[arg-type]
        fiber_end=0,
        review_state="approved",  # type: ignore[arg-type]
        source_evidence={
            "api_token": "DO-NOT-LEAK",
            "confidence": float("inf"),
        },
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
        links=(
            RouteLink(
                link_id="link-01",
                order=1,
                from_shelf_id=shelves[0].shelf_id,
                to_shelf_id=shelves[1].shelf_id,
                paths=(invalid_path,),
            ),
        ),
    )

    issues = project.validate()
    codes = {issue.code for issue in issues if issue.is_error}

    assert {
        "REQUIRED_FIELD",
        "INVALID_PATH_NUMBER",
        "INVALID_FIBER_RANGE",
        "INVALID_REVIEW_STATE",
        "SECRET_FIELD_NOT_ALLOWED",
        "LINKS_NOT_JSON_SAFE",
        "PATH_SOURCE_EVIDENCE_NOT_JSON_SAFE",
    } <= codes
    assert "DO-NOT-LEAK" not in " ".join(issue.message for issue in issues)
    with pytest.raises(RouteProjectValidationError):
        save_route_project_draft(project, tmp_path / "unsafe-links.json")
    assert not (tmp_path / "unsafe-links.json").exists()


def test_current_schema_requires_explicit_ospf_and_links_fields():
    raw = _project().to_dict()
    raw.pop("ospf_area")
    with pytest.raises(RouteProjectFormatError, match="ospf_area"):
        RouteProject.from_dict(raw)

    raw = _project().to_dict()
    raw.pop("links")
    with pytest.raises(RouteProjectFormatError, match="route.links"):
        RouteProject.from_dict(raw)


def test_ospf_area_is_optional_but_must_be_dotted_quad_when_present():
    assert "INVALID_OSPF_AREA" not in _codes(_project(), "error")
    assert "INVALID_OSPF_AREA" not in _codes(
        _project(ospf_area="0.0.0.0"),
        "error",
    )
    assert "INVALID_OSPF_AREA" in _codes(
        _project(ospf_area="area-zero"),
        "error",
    )


@pytest.mark.parametrize("legacy_version", ("1.0", "1.1"))
def test_legacy_schema_load_migrates_with_blank_route_engineering(
    legacy_version: str,
    tmp_path: Path,
):
    original = _project()
    raw = original.to_dict()
    raw["schema_version"] = legacy_version
    raw.pop("ospf_area")
    raw.pop("links")
    if legacy_version == "1.0":
        raw.pop("diagram_source")
        for shelf in raw["shelves"]:
            shelf.pop("review_state")
            shelf.pop("source_evidence")
    legacy_path = tmp_path / "legacy-route.json"
    legacy_path.write_text(json.dumps(raw), encoding="utf-8")

    migrated = load_route_project(legacy_path)

    assert ROUTE_SCHEMA_VERSION == "1.2"
    assert migrated.schema_version == ROUTE_SCHEMA_VERSION
    assert migrated.ospf_area == ""
    assert migrated.links == ()
    assert migrated.diagram_source == {}
    assert all(shelf.review_state == "manual" for shelf in migrated.shelves)
    assert all(shelf.source_evidence == {} for shelf in migrated.shelves)
    migrated_raw = migrated.to_dict()
    assert migrated_raw["schema_version"] == "1.2"
    assert migrated_raw["ospf_area"] == ""
    assert migrated_raw["links"] == []
    assert migrated_raw["diagram_source"] == {}
    assert all(
        shelf["review_state"] == "manual"
        and shelf["source_evidence"] == {}
        for shelf in migrated_raw["shelves"]
    )
    assert route_project_fingerprint(migrated) == route_project_fingerprint(
        original
    )


def test_provenance_rejects_secret_keys_and_non_json_values_without_leaks():
    project = _project(
        shelves=(
            _shelf(
                1,
                source_evidence={
                    "authorization_token": "SENSITIVE-EVIDENCE",
                    "confidence": float("nan"),
                },
            ),
        ),
        sites=(_site(1),),
        diagram_source={
            "private_key": "SENSITIVE-SOURCE",
            "page": float("inf"),
        },
    )

    issues = project.validate()
    secret_issues = [
        issue for issue in issues if issue.code == "SECRET_FIELD_NOT_ALLOWED"
    ]

    assert len(secret_issues) == 2
    assert all("SENSITIVE-EVIDENCE" not in issue.message for issue in secret_issues)
    assert all("SENSITIVE-SOURCE" not in issue.message for issue in secret_issues)
    assert "DIAGRAM_SOURCE_NOT_JSON_SAFE" in _codes(project, "error")
    assert "SOURCE_EVIDENCE_NOT_JSON_SAFE" in _codes(project, "error")


def test_review_state_is_constrained_to_explicit_lifecycle_values():
    assert REVIEW_STATES == ("manual", "pending", "confirmed", "corrected")
    project = _project(
        shelves=(_shelf(1, review_state="approved"),),
        sites=(_site(1),),
    )

    invalid = [
        issue
        for issue in project.validate()
        if issue.code == "INVALID_REVIEW_STATE"
    ]

    assert len(invalid) == 1
    assert invalid[0].field == "shelves[0].review_state"
    assert invalid[0].is_error


def test_route_project_fingerprint_hashes_complete_canonical_document():
    evidence_a = {
        "image": {"sha256": "image-1", "index": 1},
        "confidence": 0.91,
    }
    evidence_b = {
        "confidence": 0.91,
        "image": {"index": 1, "sha256": "image-1"},
    }
    source_a = {"source_sha256": "doc-1", "format": "docx"}
    source_b = {"format": "docx", "source_sha256": "doc-1"}
    shelves_a = (
        replace(_shelf(1), source_evidence=evidence_a),
        _shelf(2),
    )
    shelves_b = (
        replace(_shelf(1), source_evidence=evidence_b),
        _shelf(2),
    )
    project_a = _project(
        shelves=shelves_a,
        sites=(_site(1), _site(2)),
        diagram_source=source_a,
    )
    project_b = _project(
        shelves=shelves_b,
        sites=(_site(1), _site(2)),
        diagram_source=source_b,
    )
    expected = hashlib.sha256(
        json.dumps(
            project_a.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    fingerprint = route_project_fingerprint(project_a)

    assert fingerprint == expected
    assert len(fingerprint) == 64
    assert fingerprint == route_project_fingerprint(project_b)
    assert fingerprint != route_project_fingerprint(
        replace(project_a, notes="Changed project notes")
    )
    assert fingerprint != route_project_fingerprint(
        replace(project_a, diagram_source={"format": "docx", "source_sha256": "doc-2"})
    )
    assert fingerprint != route_project_fingerprint(
        replace(
            project_a,
            shelves=(
                replace(project_a.shelves[0], review_state="corrected"),
                project_a.shelves[1],
            ),
        )
    )
    assert fingerprint != route_project_fingerprint(
        replace(
            project_a,
            shelves=(
                replace(
                    project_a.shelves[0],
                    source_evidence={"confidence": 0.92},
                ),
                project_a.shelves[1],
            ),
        )
    )
    assert fingerprint != route_project_fingerprint(
        replace(project_a, sites=tuple(reversed(project_a.sites)))
    )
    assert fingerprint != route_project_fingerprint(
        replace(project_a, ospf_area="0.0.0.0")
    )
    links = _links_for(project_a.shelves)
    linked = replace(project_a, links=links)
    assert fingerprint != route_project_fingerprint(linked)
    assert route_project_fingerprint(linked) != route_project_fingerprint(
        replace(
            linked,
            links=(
                replace(
                    linked.links[0],
                    paths=(
                        replace(
                            linked.links[0].paths[0],
                            expected_loss_db=99.5,
                        ),
                    ),
                ),
            ),
        )
    )


def test_unassigned_raman_callout_blocks_but_legend_sample_does_not() -> None:
    shelves = (_shelf(1), _shelf(2))
    base = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
    )
    unresolved = replace(
        base,
        diagram_source={
            "unassigned_raman_callouts": [
                {
                    "raw_text": "4/5",
                    "slot": 4,
                    "port": 5,
                    "shelf_tid": None,
                    "context": "unknown",
                }
            ]
        },
    )
    legend_only = replace(
        base,
        diagram_source={
            "unassigned_raman_callouts": [
                {
                    "raw_text": "3/5",
                    "slot": 3,
                    "port": 5,
                    "shelf_tid": None,
                    "context": "legend_sample",
                }
            ]
        },
    )

    assert "UNASSIGNED_RAMAN_CALLOUT" in _codes(unresolved, "error")
    assert "UNASSIGNED_RAMAN_CALLOUT" not in _codes(legend_only, "error")


def test_deterministic_atomic_json_save_and_load(tmp_path: Path):
    shelves = (
        _shelf(1, profile_id="add_drop_a"),
        _shelf(2, profile_id="ila"),
        _shelf(3, profile_id="roadm_z"),
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2), _site(3)),
        ospf_area="0.0.0.0",
        links=_links_for(shelves),
    )
    first = tmp_path / "route.json"
    second = tmp_path / "route-copy.json"

    assert save_route_project(project, first) == first
    assert project.save(second) == second
    first_bytes = first.read_bytes()
    assert first_bytes == second.read_bytes()
    assert first_bytes.endswith(b"\n")
    assert not list(tmp_path.glob(".*.tmp"))

    raw = json.loads(first.read_text(encoding="utf-8"))
    assert raw["schema_name"] == ROUTE_SCHEMA_NAME
    assert raw["schema_version"] == ROUTE_SCHEMA_VERSION
    assert raw["ospf_area"] == "0.0.0.0"
    assert [link["order"] for link in raw["links"]] == [1, 2]
    assert [item["shelf_id"] for item in raw["shelves"]] == [
        item.shelf_id for item in project.shelves
    ]
    loaded = load_route_project(first)
    assert loaded == project
    assert RouteProject.load(second) == project
    assert loaded.to_dict() == project.to_dict()


def test_pending_incomplete_diagram_draft_can_be_saved_and_reopened(
    tmp_path: Path,
) -> None:
    incomplete_shelf = replace(
        _shelf(1, review_state="pending"),
        software_release="",
        shelf_variant="",
        power_label="",
    )
    project = _project(
        shelves=(incomplete_shelf,),
        sites=(_site(1),),
        diagram_source={"file_name": "route.docx", "source_sha256": "abc"},
    )
    destination = tmp_path / "pending-route.json"

    with pytest.raises(RouteProjectValidationError):
        save_route_project(project, destination)

    assert save_route_project_draft(project, destination) == destination
    loaded = load_route_project_draft(destination)
    assert loaded.to_dict() == project.to_dict()
    assert RouteProject.load_draft(destination).to_dict() == project.to_dict()
    assert any(issue.is_error for issue in loaded.validate())
    with pytest.raises(RouteProjectValidationError):
        load_route_project(destination)


def test_draft_persistence_still_rejects_secret_keys_and_control_text(
    tmp_path: Path,
) -> None:
    secret = _project(
        shelves=(
            replace(
                _shelf(1, review_state="pending"),
                source_evidence={"api_token": "do-not-save"},
            ),
        ),
        sites=(_site(1),),
    )
    control_text = replace(_project(), notes="bad\nline")

    with pytest.raises(RouteProjectValidationError):
        secret.save_draft(tmp_path / "secret.json")
    with pytest.raises(RouteProjectValidationError):
        save_route_project_draft(control_text, tmp_path / "control.json")
    assert not list(tmp_path.iterdir())


def test_load_rejects_malformed_or_unknown_schema(tmp_path: Path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(RouteProjectFormatError, match="line 1"):
        load_route_project(malformed)

    wrong_schema = tmp_path / "wrong-schema.json"
    raw = _project().to_dict()
    raw["schema_version"] = "99"
    wrong_schema.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RouteProjectFormatError, match="schema_version"):
        load_route_project(wrong_schema)


def test_planning_profiles_are_never_cli_ready():
    project = _project(
        shelves=(_shelf(1, profile_id="roadm_a"),),
        sites=(_site(1),),
    )

    readiness = project.deployment_readiness()

    assert readiness.ready is False
    assert readiness.is_ready is False
    assert readiness.shelf_statuses[0].provider_available is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "EXACT_PROVIDER_REVIEW_REQUIRED",
    )


def test_pending_diagram_shelf_blocks_deployment_until_human_review():
    project = _project(
        shelves=(
            _shelf(
                1,
                profile_id="ila",
                review_state="pending",
                source_evidence={"confidence": 0.99},
            ),
        ),
        sites=(_site(1),),
        diagram_source={"format": "docx", "source_sha256": "doc-1"},
    )

    issues = project.validate()
    readiness = project.deployment_readiness()
    status = readiness.shelf_statuses[0]

    assert any(
        issue.code == "PENDING_SHELF_REVIEW"
        and issue.severity == "warning"
        for issue in issues
    )
    assert not any(issue.is_error for issue in issues)
    assert readiness.ready is False
    assert status.provider_available is False
    assert status.ready is False
    assert status.reason_codes == ("PENDING_SHELF_REVIEW",)
    assert any("reviewed" in reason for reason in readiness.blocking_reasons)


def test_removed_r4_2_profile_is_explicitly_rejected():
    project = _project(
        shelves=(
            _shelf(
                1,
                profile_id="protected_dci",
                software_release="RLS R4.2",
            ),
        ),
        sites=(_site(1),),
    )

    assert {
        "UNKNOWN_PROFILE",
        "UNSUPPORTED_SOFTWARE_RELEASE",
    } <= _codes(project, "error")
    status = project.deployment_readiness().shelf_statuses[0]
    assert status.ready is False
    assert status.provider_available is False
    assert status.reason_codes == ("UNKNOWN_PROFILE",)


def test_side_specific_profiles_must_be_on_ordered_route_endpoints():
    sites = (_site(1), _site(2), _site(3))
    valid = _project(
        shelves=(
            _shelf(1, profile_id="add_drop_a", site_key="site-1"),
            _shelf(2, profile_id="roadm_a", site_key="site-1"),
            _shelf(3, profile_id="ila", site_key="site-2"),
            _shelf(4, profile_id="roadm_z", site_key="site-3"),
            _shelf(5, profile_id="add_drop_z", site_key="site-3"),
        ),
        sites=sites,
    )
    assert not {
        "INVALID_A_ENDPOINT_PLACEMENT",
        "INVALID_Z_ENDPOINT_PLACEMENT",
    } & _codes(valid, "error")

    invalid = _project(
        shelves=(
            _shelf(1, profile_id="add_drop_z", site_key="site-1"),
            _shelf(2, profile_id="add_drop_a", site_key="site-2"),
            _shelf(3, profile_id="roadm_a", site_key="site-3"),
        ),
        sites=sites,
    )
    assert {
        "INVALID_A_ENDPOINT_PLACEMENT",
        "INVALID_Z_ENDPOINT_PLACEMENT",
    } <= _codes(invalid, "error")


def test_single_site_route_allows_both_sides_with_explicit_ambiguity_warning():
    project = _project(
        shelves=(
            _shelf(1, profile_id="add_drop_a", site_key="site-1"),
            _shelf(2, profile_id="roadm_z", site_key="site-1"),
        ),
        sites=(_site(1),),
    )

    issues = project.validate()

    assert not any(
        issue.code
        in {"INVALID_A_ENDPOINT_PLACEMENT", "INVALID_Z_ENDPOINT_PLACEMENT"}
        for issue in issues
    )
    assert any(
        issue.code == "SINGLE_SITE_ENDPOINT_AMBIGUITY"
        and issue.severity == "warning"
        for issue in issues
    )
    project.assert_valid()


def test_site_helpers_follow_shelf_route_order_not_site_definition_order():
    sites = (_site(3), _site(1), _site(2))
    shelves = (
        _shelf(1, site_key="site-2"),
        _shelf(2, site_key="site-1"),
        _shelf(3, site_key="site-2"),
    )
    project = _project(shelves=shelves, sites=sites)

    assert project.ordered_site_keys() == ("site-2", "site-1")
    endpoint_a, endpoint_z = project.endpoint_sites()
    assert endpoint_a == _site(2)
    assert endpoint_z == _site(1)
    assert project.site_by_key("missing") is None


def test_route_native_fiber_review_requires_one_exact_supported_choice():
    shelves = tuple(_shelf(index) for index in range(1, 4))
    bare_links = _links_for(shelves)
    reviewed_links = tuple(
        replace(
            link,
            paths=(
                _reviewed_route_native_path(link.paths[0], link, "NDSF"),
            ),
        )
        for link in bare_links
    )

    assert route_native_fiber_review(()) == ("not_applicable", "")
    assert route_native_fiber_review(bare_links) == ("missing", "")
    assert route_native_fiber_review(reviewed_links) == (
        "confirmed",
        "NDSF",
    )


@pytest.mark.parametrize(
    "path_transform",
    (
        lambda path, link: replace(
            path,
            source_evidence={
                "route_native_fiber_review": {
                    "value": "NDSF",
                    "status": "confirmed",
                }
            },
        ),
        lambda path, link: replace(
            path,
            source_evidence={
                "route_native_fiber_review": {
                    **_route_native_fiber_marker("NDSF"),
                    "deployable_cli": 0,
                }
            },
        ),
        lambda path, link: _reviewed_route_native_path(
            path, link, "customer-free-text"
        ),
        lambda path, link: replace(
            _reviewed_route_native_path(path, link, "NDSF"),
            fiber_type="DSF",
        ),
        lambda path, link: _reviewed_route_native_path(
            path,
            link,
            "NDSF",
            endpoint_tokens=("NDSF", "DSF"),
        ),
    ),
)
def test_route_native_fiber_review_rejects_invalid_path_contract(
    path_transform,
):
    shelves = (_shelf(1), _shelf(2))
    link = _links_for(shelves)[0]
    invalid_link = replace(
        link,
        paths=(path_transform(link.paths[0], link),),
    )

    assert route_native_fiber_review((invalid_link,)) == ("invalid", "")


def test_route_native_fiber_review_rejects_different_tokens_across_spans():
    shelves = tuple(_shelf(index) for index in range(1, 4))
    links = _links_for(shelves)
    inconsistent = (
        replace(
            links[0],
            paths=(
                _reviewed_route_native_path(
                    links[0].paths[0], links[0], "NDSF"
                ),
            ),
        ),
        replace(
            links[1],
            paths=(
                _reviewed_route_native_path(
                    links[1].paths[0], links[1], "DSF"
                ),
            ),
        ),
    )

    assert route_native_fiber_review(inconsistent) == ("invalid", "")


def test_missing_route_native_fiber_review_is_nonstructural_and_draft_safe(
    tmp_path: Path,
):
    shelves = (_shelf(1), _shelf(2))
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
        links=_links_for(shelves),
    )

    issues = project.validate()

    assert any(
        issue.code == "MISSING_ROUTE_NATIVE_FIBER_REVIEW"
        and issue.severity == "warning"
        for issue in issues
    )
    assert not any(
        issue.code == "MISSING_ROUTE_NATIVE_FIBER_REVIEW"
        and issue.is_error
        for issue in issues
    )
    destination = tmp_path / "missing-route-native-fiber.json"
    assert save_route_project_draft(project, destination) == destination
    assert load_route_project_draft(destination).to_dict() == project.to_dict()


def test_malformed_route_native_fiber_marker_is_a_controlled_validation_error():
    shelves = (_shelf(1), _shelf(2))
    link = _links_for(shelves)[0]
    malformed = replace(
        link.paths[0],
        source_evidence={
            "route_native_fiber_review": {
                "value": "NDSF",
                "scope": "all_active_route_spans",
            }
        },
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
        links=(replace(link, paths=(malformed,)),),
    )

    assert "INVALID_ROUTE_NATIVE_FIBER_REVIEW" in _codes(project, "error")
    readiness = project.deployment_readiness()
    assert readiness.ready is False
    assert all(
        "INVALID_ROUTE_NATIVE_FIBER_REVIEW" in status.reason_codes
        for status in readiness.shelf_statuses
    )


def test_route_native_fiber_readiness_scopes_missing_path_to_its_endpoints():
    shelves = tuple(_shelf(index) for index in range(1, 4))
    links = _links_for(shelves)
    reviewed_first = replace(
        links[0],
        paths=(
            _reviewed_route_native_path(
                links[0].paths[0], links[0], "NDSF"
            ),
        ),
    )
    unmarked_second = replace(
        links[1],
        paths=(replace(links[1].paths[0], review_state="confirmed"),),
    )
    project = _project(
        shelves=shelves,
        sites=tuple(_site(index) for index in range(1, 4)),
        links=(reviewed_first, unmarked_second),
    )

    readiness = project.deployment_readiness()
    by_shelf = {
        status.shelf_id: status for status in readiness.shelf_statuses
    }

    assert readiness.ready is False
    assert "MISSING_ROUTE_NATIVE_FIBER_REVIEW" not in (
        by_shelf["shelf-01"].reason_codes
    )
    assert "MISSING_ROUTE_NATIVE_FIBER_REVIEW" in (
        by_shelf["shelf-02"].reason_codes
    )
    assert "MISSING_ROUTE_NATIVE_FIBER_REVIEW" in (
        by_shelf["shelf-03"].reason_codes
    )


def test_route_native_fiber_mismatch_blocks_every_shelf_on_populated_route():
    shelves = tuple(_shelf(index) for index in range(1, 4))
    links = _links_for(shelves)
    inconsistent = (
        replace(
            links[0],
            paths=(
                _reviewed_route_native_path(
                    links[0].paths[0], links[0], "NDSF"
                ),
            ),
        ),
        replace(
            links[1],
            paths=(
                _reviewed_route_native_path(
                    links[1].paths[0], links[1], "DSF"
                ),
            ),
        ),
    )
    project = _project(
        shelves=shelves,
        sites=tuple(_site(index) for index in range(1, 4)),
        links=inconsistent,
    )

    readiness = project.deployment_readiness()

    assert readiness.ready is False
    assert "ROUTE_NATIVE_FIBER_MISMATCH" in _codes(project, "error")
    assert all(
        "ROUTE_NATIVE_FIBER_MISMATCH" in status.reason_codes
        for status in readiness.shelf_statuses
    )


def test_endpoint_review_must_match_confirmed_route_native_fiber():
    shelves = (_shelf(1), _shelf(2))
    link = _links_for(shelves)[0]
    mismatched = _reviewed_route_native_path(
        link.paths[0],
        link,
        "NDSF",
        endpoint_tokens=("DSF", "NDSF"),
    )
    project = _project(
        shelves=shelves,
        sites=(_site(1), _site(2)),
        links=(replace(link, paths=(mismatched,)),),
    )

    readiness = project.deployment_readiness()
    by_shelf = {
        status.shelf_id: status for status in readiness.shelf_statuses
    }

    assert "ROUTE_NATIVE_FIBER_ENDPOINT_MISMATCH" in _codes(
        project, "error"
    )
    assert "ROUTE_NATIVE_FIBER_ENDPOINT_MISMATCH" in (
        by_shelf["shelf-01"].reason_codes
    )
    assert "ROUTE_NATIVE_FIBER_ENDPOINT_MISMATCH" not in (
        by_shelf["shelf-02"].reason_codes
    )
