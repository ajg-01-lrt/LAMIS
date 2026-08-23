"""Planning-role and route-order safety tests for RLS diagram imports."""

from __future__ import annotations

from utils.rls_config.diagram_import import (
    ShelfCandidate,
    _assign_endpoint_profiles,
)


def _candidate(
    order: int,
    tid: str,
    site_code: str,
    profile_family: str,
    *,
    chassis: str,
    lifecycle: str = "active",
) -> ShelfCandidate:
    return ShelfCandidate(
        order=order,
        tid=tid,
        primary_oam_ip=None,
        site_code=site_code,
        site_name=site_code,
        site_address=None,
        network_site_id=None,
        profile_family=profile_family,
        profile_id="",
        endpoint_side="",
        chassis=chassis,
        software_release=None,
        shelf_variant=None,
        topology=None,
        band=None,
        add_drop_structure=None,
        protection_type=None,
        module_inventory=(),
        power_label=None,
        raman_label=None,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        notes=None,
        evidence=(),
    )


def test_sample_route_keeps_removal_order_gap_and_maps_active_endpoints() -> None:
    shelves = [
        _candidate(
            1,
            "USELP1-L8R2",
            "USELP1",
            "roadm",
            chassis="R4 600mm",
        )
    ]
    for order in range(2, 16):
        shelves.append(
            _candidate(
                order,
                f"INTERMEDIATE-{order}-L8I2",
                f"SITE{order}",
                "ila",
                chassis="R2 600mm",
            )
        )
    shelves.extend(
        (
            _candidate(
                16,
                "USSAT3-L8I2",
                "USSAT3",
                "ila",
                chassis="R2 600mm",
                lifecycle="planned_remove",
            ),
            _candidate(
                17,
                "USSAT4-L8R3",
                "USSAT4",
                "roadm",
                chassis="R4 600mm",
            ),
        )
    )
    issues = []

    assigned = _assign_endpoint_profiles(tuple(shelves), issues)
    active = tuple(shelf for shelf in assigned if shelf.active_for_route)

    assert [shelf.order for shelf in assigned] == list(range(1, 18))
    assert [shelf.order for shelf in active] == [*range(1, 16), 17]
    assert assigned[0].profile_id == "roadm_a"
    assert assigned[0].endpoint_side == "A"
    assert all(shelf.profile_id == "ila" for shelf in assigned[1:15])
    assert assigned[15].lifecycle == "planned_remove"
    assert assigned[15].endpoint_side == ""
    assert assigned[16].profile_id == "roadm_z"
    assert assigned[16].endpoint_side == "Z"
    assert not {
        "ILA_NOT_INTERMEDIATE",
        "UNKNOWN_SHELF_PROFILE",
        "ROUTE_REQUIRES_DISTINCT_ENDPOINTS",
    } & {issue.code for issue in issues}


def test_tid_chassis_and_position_never_recover_unknown_planning_roles() -> None:
    shelves = (
        _candidate(
            1,
            "USELP1-L8R2",
            "USELP1",
            "unknown",
            chassis="R4 600mm",
        ),
        _candidate(
            2,
            "USQTN1-L8I2",
            "USQTN1",
            "unknown",
            chassis="R2 600mm",
        ),
        _candidate(
            3,
            "USSAT4-L8R3",
            "USSAT4",
            "unknown",
            chassis="R4 600mm",
        ),
    )
    issues = []

    assigned = _assign_endpoint_profiles(shelves, issues)

    assert [shelf.profile_id for shelf in assigned] == ["", "", ""]
    assert [shelf.endpoint_side for shelf in assigned] == ["A", "", "Z"]
    assert [issue.code for issue in issues].count("UNKNOWN_SHELF_PROFILE") == 3


def test_unknown_planned_removal_does_not_require_an_active_profile() -> None:
    shelves = (
        _candidate(
            1,
            "SITEA-ROADM-1",
            "SITEA",
            "roadm",
            chassis="R4 600mm",
        ),
        _candidate(
            2,
            "REMOVED-ILA-1",
            "REMOVED",
            "unknown",
            chassis="R2 600mm",
            lifecycle="planned_remove",
        ),
        _candidate(
            3,
            "SITEZ-ROADM-1",
            "SITEZ",
            "roadm",
            chassis="R4 600mm",
        ),
    )
    issues = []

    assigned = _assign_endpoint_profiles(shelves, issues)

    assert assigned[1].profile_id == ""
    assert assigned[1].endpoint_side == ""
    assert not any(
        issue.code == "UNKNOWN_SHELF_PROFILE"
        and issue.field == "shelves[2].profile_family"
        for issue in issues
    )


def test_missing_sites_group_by_review_only_tid_prefix_for_endpoint_sides() -> None:
    shelves = (
        _candidate(
            1,
            "SITEA-ROADM-1",
            "",
            "roadm",
            chassis="R4 600mm",
        ),
        _candidate(
            2,
            "SITEA-ROADM-2",
            "",
            "roadm",
            chassis="R4 600mm",
        ),
        _candidate(
            3,
            "MID-ILA-1",
            "",
            "ila",
            chassis="R2 600mm",
        ),
        _candidate(
            4,
            "SITEZ-ROADM-1",
            "",
            "roadm",
            chassis="R4 600mm",
        ),
    )
    issues = []

    assigned = _assign_endpoint_profiles(shelves, issues)

    assert [shelf.profile_id for shelf in assigned] == [
        "roadm_a",
        "roadm_a",
        "ila",
        "roadm_z",
    ]
    assert [shelf.endpoint_side for shelf in assigned] == ["A", "A", "", "Z"]
    assert "ILA_NOT_INTERMEDIATE" not in {issue.code for issue in issues}
