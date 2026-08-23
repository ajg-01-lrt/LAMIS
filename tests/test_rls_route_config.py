"""Fail-closed whole-route configuration preparation contracts."""

from __future__ import annotations

import builtins

import pytest

import utils.rls_config.route_config as config_module
from utils.rls_config.common import ManagementInterface
from utils.rls_config.r4_0_generator import (
    R40_CDA_RLA12_C_2DEG_NO_SRA,
    R40_PROVIDER_CATALOG,
    R40ExactRequest,
    R40LinePath,
    encode_r40_exact_payload,
)
from utils.rls_config.route_config import (
    RouteConfigCancelled,
    RouteConfigError,
    evaluate_route_configs,
    require_complete_route_configs,
)
from utils.rls_config.route_project import (
    DeploymentReadiness,
    RouteProject,
    ShelfDeploymentStatus,
    ShelfInstance,
    Site,
    route_project_fingerprint,
)


def _request(index: int) -> R40ExactRequest:
    profile = R40_PROVIDER_CATALOG[R40_CDA_RLA12_C_2DEG_NO_SRA]
    return R40ExactRequest(
        provider_id=profile.provider_id,
        profile="add_drop",
        software_release="RLS R4.0",
        target_software_build="R4.0.0-audited",
        chassis_family=profile.chassis_family,
        chassis_pec=profile.chassis_pec,
        hardware_profile=profile.hardware_profile,
        shelf_name=f"RLS-{index:02d}",
        site_name=f"S{index:02d}",
        member_name=f"rls-{index:02d}",
        hostname=f"rls-{index:02d}.example.net",
        frame_identification_code=f"Rack {index:02d}",
        loopback_ip=f"198.51.100.{index + 9}",
        ospf_area="0.0.0.1",
        management=ManagementInterface(
            enabled=True,
            name="colan-x",
            routing_mode="OSPF_GNE",
            ip_address=f"192.0.2.{index}",
            prefix_length=30,
            gateway="",
            ospf_metric=10,
            static_metric=1500,
        ),
        line_1=R40LinePath(
            link_name="LINE-1-OUT",
            neighbor_node="external-peer-1",
            neighbor_line_mux_pfg="LINE-MUX-PFG-1",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-1",
            fiber_type="NDSF",
            expected_loss_db=12.2,
        ),
        line_2=R40LinePath(
            link_name="LINE-2-OUT",
            neighbor_node="external-peer-2",
            neighbor_line_mux_pfg="LINE-MUX-PFG-2",
            neighbor_line_demux_pfg="LINE-DEMUX-PFG-2",
            fiber_type="NDSF",
            expected_loss_db=13.5,
        ),
        line_1_route_side="A",
        installed_inventory_confirmed=True,
        planner_runtime_mop_confirmed=True,
        target_build_confirmed=True,
    )


def _shelf(
    index: int,
    *,
    profile_id: str = "add_drop",
    software_release: str = "RLS R4.0",
    review_state: str = "confirmed",
) -> ShelfInstance:
    return ShelfInstance(
        shelf_id=f"shelf-{index:02d}",
        profile_id=profile_id,
        software_release=software_release,
        shelf_variant="reviewed-exact-provider",
        site_key=f"site-{index}",
        tid=f"RLS-{index:02d}",
        primary_oam_ip=f"192.0.2.{index}",
        power_label="A/B -48 VDC",
        profile_payload=(
            encode_r40_exact_payload(_request(index))
            if profile_id == "add_drop"
            else {}
        ),
        review_state=review_state,  # type: ignore[arg-type]
        source_evidence=(
            {"method": "vision", "confidence": 0.99}
            if review_state == "pending"
            else {}
        ),
    )


def _project(
    shelves: tuple[ShelfInstance, ...] | None = None,
) -> RouteProject:
    shelves = shelves or (_shelf(1),)
    return RouteProject(
        project_id="route-project-1",
        route_code="S01-S02",
        title="Audited RLS R4.0 route",
        ospf_area="0.0.0.1",
        sites=tuple(
            Site(f"site-{index}", f"S{index:02d}", f"Route Site {index}")
            for index in range(1, len(shelves) + 1)
        ),
        shelves=shelves,
    )


def test_complete_r4_0_route_builds_every_shelf_with_current_fingerprint() -> None:
    project = _project()

    result = evaluate_route_configs(project)

    assert result.ready is True
    assert result.readiness.ready is True
    assert result.project_fingerprint == route_project_fingerprint(project)
    assert result.config_count == len(project.shelves) == 1
    assert [item.order for item in result.shelf_builds] == [1]
    assert [item.shelf_id for item in result.shelf_builds] == [
        "shelf-01",
    ]
    assert all(item.profile_id == "add_drop" for item in result.shelf_builds)
    assert all(
        item.artifact.manifest["release"] == "RLS R4.0"
        for item in result.shelf_builds
    )
    assert all(item.artifact.cli_text.strip() for item in result.shelf_builds)
    assert result.blocking_reasons == ()


@pytest.mark.parametrize(
    "project",
    [
        _project(shelves=(_shelf(1, review_state="pending"),)),
        _project(shelves=(_shelf(1, profile_id="unknown_profile"),)),
        _project(shelves=(_shelf(1, software_release="RLS R4.2"),)),
    ],
    ids=("pending-import", "unsupported-profile", "r4.2-release"),
)
def test_blocked_route_never_invokes_provider_or_retains_partial_builds(
    project: RouteProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_provider(_shelf):
        raise AssertionError("global readiness must be checked before providers")

    monkeypatch.setitem(
        config_module._PROVIDERS,  # noqa: SLF001 - deliberate seam audit
        "add_drop",
        forbidden_provider,
    )

    result = evaluate_route_configs(project)

    assert result.ready is False
    assert result.config_count == 0
    assert result.shelf_builds == ()
    assert result.blocking_reasons
    with pytest.raises(RouteConfigError):
        require_complete_route_configs(project)


def test_removed_r4_2_profile_and_release_are_both_blocked() -> None:
    project = _project(
        shelves=(
            _shelf(
                1,
                profile_id="protected_dci",
                software_release="RLS R4.2",
            ),
        )
    )

    result = evaluate_route_configs(project)

    assert result.ready is False
    assert result.shelf_builds == ()
    assert result.readiness.shelf_statuses[0].reason_codes == (
        "UNKNOWN_PROFILE",
    )
    assert {
        issue.code for issue in project.validate() if issue.is_error
    } >= {"UNKNOWN_PROFILE", "UNSUPPORTED_SOFTWARE_RELEASE"}


def test_later_provider_failure_discards_earlier_in_memory_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_second(shelf):
        calls.append(shelf.shelf_id)
        if len(calls) == 2:
            raise RuntimeError("sensitive provider detail")
        return object()

    monkeypatch.setitem(
        config_module._PROVIDERS,  # noqa: SLF001 - deliberate seam audit
        "add_drop",
        fail_second,
    )
    monkeypatch.setattr(
        RouteProject,
        "deployment_readiness",
        lambda _project: DeploymentReadiness(
            ready=True,
            shelf_statuses=tuple(
                ShelfDeploymentStatus(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    provider_available=True,
                    ready=True,
                )
                for shelf in _project.shelves
            ),
        ),
    )

    result = evaluate_route_configs(
        _project(shelves=(_shelf(1), _shelf(2)))
    )

    assert calls == ["shelf-01", "shelf-02"]
    assert result.ready is False
    assert result.shelf_builds == ()
    assert result.config_count == 0
    assert result.blocking_reasons == (
        "A registered configuration provider failed; no partial route CLI was retained.",
        "Provider error type: RuntimeError",
    )
    assert "sensitive provider detail" not in " ".join(result.blocking_reasons)


def test_cancelled_route_build_raises_without_candidates() -> None:
    class Cancelled:
        @staticmethod
        def is_set() -> bool:
            return True

    with pytest.raises(RouteConfigCancelled, match="cancelled"):
        evaluate_route_configs(_project(), cancel_event=Cancelled())


def test_production_path_never_imports_legacy_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.endswith("legacy_generator"):
            raise AssertionError("legacy generator must never be reached")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    result = evaluate_route_configs(_project(shelves=(_shelf(1),)))

    assert result.ready is True
    assert result.config_count == 1
