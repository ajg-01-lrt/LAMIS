"""One-click, fail-closed RLS R4.0 route deliverable bundle contracts."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from PIL import Image

import utils.rls_config.route_bundle as bundle_module
from tests.test_rls_r4_0_route_integration import _two_shelf_exact_project
from utils.rls_config.diagram_assets import (
    WorkbookDiagram,
    WorkbookDiagramImage,
)
from utils.rls_config.mop_export import FBN_PART, IRM_PART
from utils.rls_config.route_bundle import (
    RouteBundleError,
    _write_config_candidates,
    export_route_bundle,
)
from utils.rls_config.route_config import RouteConfigBuild, ShelfConfigBuild
from utils.rls_config.route_project import DeploymentReadiness, RouteProject


def _project(*, pending: bool = False) -> RouteProject:
    project = _two_shelf_exact_project()
    if not pending:
        return project
    return replace(
        project,
        shelves=(
            replace(project.shelves[0], review_state="pending"),
            project.shelves[1],
        ),
    )


def _fake_mop(
    _project: RouteProject,
    destination: Path,
    **_kwargs: object,
) -> Path:
    Path(destination).write_bytes(b"PK\x03\x04test-workbook")
    return Path(destination)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workbook_diagram() -> WorkbookDiagram:
    output = BytesIO()
    image = Image.new("RGB", (48, 24), (10, 30, 50))
    try:
        image.save(output, format="PNG", compress_level=6, optimize=False)
    finally:
        image.close()
    data = output.getvalue()
    return WorkbookDiagram(
        source_file_name="customer-route.png",
        source_type="png",
        source_sha256="c" * 64,
        images=(
            WorkbookDiagramImage(
                source_label="customer-route.png",
                source_part="customer-route.png",
                normalized_sha256=hashlib.sha256(data).hexdigest(),
                width=48,
                height=24,
                png_bytes=data,
            ),
        ),
    )


def _project_with_required_diagram(
    diagram: WorkbookDiagram,
) -> RouteProject:
    project = _project()
    return replace(
        project,
        diagram_source={
            "file_name": diagram.source_file_name,
            "source_type": diagram.source_type,
            "source_sha256": diagram.source_sha256,
            "workbook_diagram": diagram.marker_dict(),
        },
    )


def test_complete_route_bundle_contains_every_hashed_r40_candidate_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_module, "export_mop", _fake_mop)
    project = replace(
        _project(),
        shelves=tuple(
            replace(
                shelf,
                source_evidence={
                    "band": "c",
                    "fields": [
                        {
                            "field": "band",
                            "normalized_value": "c",
                            "method": "vision",
                            "confidence": 0.99,
                        }
                    ],
                },
            )
            for shelf in _project().shelves
        ),
        diagram_source={
            "file_name": "customer-route.docx",
            "source_type": "docx",
            "source_sha256": "a" * 64,
            "provider": "configured-vision-provider",
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
            "route_fiber_type_scope_suggestion": {
                "convention_id": "uniform-active-span-fiber-v1",
                "source_sha256": "a" * 64,
                "value": "LEAF",
                "observed_span_orders": [1],
                "inherited_span_orders": [],
                "scope": "active_route_spans",
                "status": "pending_operator_review",
                "deployable_cli": False,
            },
        },
    )

    files = export_route_bundle(project, tmp_path)

    assert set(files) == {
        "project",
        "mop",
        "validation",
        "manifest",
        "configs",
    }
    assert files["configs"].is_dir()
    assert all(files[name].is_file() for name in files if name != "configs")
    assert len({path.parent for path in files.values()}) == 1

    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    assert manifest["schema"] == "atlas.ciena.rls.route-deliverable-bundle"
    assert manifest["schema_version"] == "2.1"
    assert manifest["route_code"] == "R40"
    assert manifest["shelf_count"] == 2
    assert manifest["configuration_candidates_complete"] is True
    assert manifest["configuration_candidate_count"] == 2
    assert manifest["cli_candidate_files_included"] is True
    assert manifest["deployable_cli_included"] is False
    assert manifest["secret_material_included"] is False
    assert manifest["deployment_readiness"]["route_cli_ready"] is True
    assert manifest["route_native_fiber_review"] == {
        "status": "confirmed",
        "token": "NDSF",
        "scope": "all_active_route_spans",
        "deployment_approved": False,
    }
    assert manifest["source_diagram_content_assessed"] is True
    assert manifest["source_diagram_file_included"] is False
    assert manifest["source_diagram_content_embedded_in_mop"] is False
    assert manifest["diagram_embedding"] == {
        "embedded": False,
        "sheet": "Diagram",
        "external_relationships": False,
    }
    assert (
        manifest["diagram_source"]["route_fiber_type_scope_suggestion"][
            "value"
        ]
        == "LEAF"
    )

    for role in ("project", "mop", "validation"):
        record = manifest["artifacts"][role]
        assert record["filename"] == files[role].name
        assert record["sha256"] == _sha256(files[role])

    candidates = manifest["configuration_candidates"]
    assert [record["order"] for record in candidates] == [1, 2]
    assert [record["tid"] for record in candidates] == ["RLS-A", "RLS-Z"]
    for record in candidates:
        assert len(record["deployment_controls"]) == 3
        assert all(
            control["mode"] == "automatic_background_advisory"
            and control["status"] == "active_unverified"
            and control["physical_verification_status"] == "not_asserted"
            for control in record["deployment_controls"]
        )
        candidate_dir = files["configs"] / record["directory"]
        assert candidate_dir.is_dir()
        assert set(record["files"]) == {
            "cli",
            "annotated",
            "validation",
            "manifest",
        }
        for artifact_record in record["files"].values():
            artifact_path = candidate_dir / artifact_record["filename"]
            assert artifact_path.is_file()
            assert artifact_record["sha256"] == _sha256(artifact_path)

    validation = files["validation"].read_text(encoding="utf-8")
    assert "DOCUMENT RESULT: VALID" in validation
    assert "PRE-CALIBRATION CLI CANDIDATES: COMPLETE" in validation
    assert "Route native CLI fiber type: NDSF" in validation
    assert "Route native fiber review: confirmed" in validation
    assert "Diagram fiber label: LEAF" in validation
    assert "Diagram route optical band: C+L (context only)" in validation
    assert "deployment controls are included automatically" in validation
    assert "not facts observed or verified by ATLAS" in validation
    assert "not an on-box deployment approval" in validation
    assert "COLAN candidate states" in validation


def test_complete_bundle_includes_deferred_terminal_colan_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils.rls_config.common import (
        ManagementInterface,
        ROUTING_OSPF_OSC_ONLY,
    )
    from utils.rls_config.r4_0_generator import (
        decode_r40_exact_payload,
        encode_r40_exact_payload,
    )

    monkeypatch.setattr(bundle_module, "export_mop", _fake_mop)
    base = _project()
    shelves = []
    for shelf in base.shelves:
        request = decode_r40_exact_payload(shelf.profile_payload)
        request = replace(
            request,
            management=ManagementInterface(
                enabled=False,
                name="",
                routing_mode=ROUTING_OSPF_OSC_ONLY,
                ip_address="",
                prefix_length=None,
                gateway="",
            ),
        )
        shelves.append(
            replace(
                shelf,
                primary_oam_ip=request.loopback_ip,
                profile_payload=encode_r40_exact_payload(request),
            )
        )
    project = replace(base, shelves=tuple(shelves))

    files = export_route_bundle(project, tmp_path)

    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    validation = files["validation"].read_text(encoding="utf-8")
    assert manifest["configuration_candidates_complete"] is True
    assert manifest["configuration_candidate_count"] == len(project.shelves)
    assert manifest["deployment_readiness"]["route_cli_ready"] is True
    assert "COLAN candidate states" in validation
    assert validation.count("deferred; COLAN commands emitted: no") == len(
        project.shelves
    )

    for record in manifest["configuration_candidates"]:
        directory = files["configs"] / record["directory"]
        assert record["colan_state"] == "deferred"
        assert record["colan_commands_emitted"] is False
        child_manifest = json.loads(
            (
                directory / record["files"]["manifest"]["filename"]
            ).read_text(encoding="utf-8")
        )
        child_validation = (
            directory / record["files"]["validation"]["filename"]
        ).read_text(encoding="utf-8")
        child_cli = (
            directory / record["files"]["cli"]["filename"]
        ).read_text(encoding="utf-8")

        assert child_manifest["colan_state"] == "deferred"
        assert child_manifest["colan_commands_emitted"] is False
        assert child_manifest["warning_count"] >= 1
        assert "[TERMINAL_COLAN_DEFERRED]" in child_validation
        assert "interface colan-" not in child_cli


def test_real_renderer_bundle_hashes_complete_openable_workbook(
    tmp_path: Path,
) -> None:
    files = export_route_bundle(_project(), tmp_path)

    with zipfile.ZipFile(files["mop"]) as workbook:
        assert workbook.testzip() is None
        assert FBN_PART in workbook.namelist()
        assert IRM_PART in workbook.namelist()
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    assert manifest["artifacts"]["mop"]["sha256"] == _sha256(files["mop"])
    assert (
        json.loads(files["project"].read_text(encoding="utf-8"))["route_code"]
        == "R40"
    )


def test_route_bundle_embeds_required_diagram_and_records_hash_only_manifest(
    tmp_path: Path,
) -> None:
    diagram = _workbook_diagram()
    files = export_route_bundle(
        _project_with_required_diagram(diagram),
        tmp_path,
        diagram=diagram,
    )

    with zipfile.ZipFile(files["mop"]) as workbook:
        assert workbook.testzip() is None
        assert "xl/drawings/_rels/drawing1.xml.rels" in workbook.namelist()
        assert "xl/media/atlas_route_diagram_001.png" in workbook.namelist()
        assert (
            hashlib.sha256(
                workbook.read("xl/media/atlas_route_diagram_001.png")
            ).hexdigest()
            == diagram.images[0].normalized_sha256
        )

    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    assert manifest["source_diagram_file_included"] is False
    assert manifest["source_diagram_content_embedded_in_mop"] is True
    embedding = manifest["diagram_embedding"]
    assert embedding["embedded"] is True
    assert embedding["sheet"] == "Diagram"
    assert embedding["representation"] == "normalized_png"
    assert embedding["source_file_name"] == "customer-route.png"
    assert embedding["source_sha256"] == "c" * 64
    assert embedding["image_occurrence_count"] == 1
    assert embedding["unique_media_count"] == 1
    assert embedding["external_relationships"] is False
    assert embedding["images"][0]["normalized_sha256"] == (
        diagram.images[0].normalized_sha256
    )
    serialized_manifest = files["manifest"].read_text(encoding="utf-8")
    assert "png_bytes" not in serialized_manifest
    assert "base64" not in serialized_manifest


def test_required_diagram_missing_publishes_no_bundle_or_partial_artifact(
    tmp_path: Path,
) -> None:
    diagram = _workbook_diagram()

    with pytest.raises(RouteBundleError, match="must be reattached"):
        export_route_bundle(
            _project_with_required_diagram(diagram),
            tmp_path,
        )

    assert list(tmp_path.iterdir()) == []


def test_non_r40_artifact_is_rejected_before_candidate_files_are_written(
    tmp_path: Path,
) -> None:
    artifact = SimpleNamespace(
        manifest={
            "release": "RLS R4.2",
            "generator": "RLSConfigGenerator",
            "provider_id": "protected_dci",
        },
        cli_text="quit\n",
        annotated_text="retired\n",
        validation_report="retired\n",
    )
    build = RouteConfigBuild(
        project_fingerprint="a" * 64,
        ready=True,
        readiness=DeploymentReadiness(ready=True, shelf_statuses=()),
        shelf_builds=(
            ShelfConfigBuild(
                order=1,
                shelf_id="legacy",
                profile_id="protected_dci",
                tid="LEGACY",
                artifact=artifact,
            ),
        ),
    )

    with pytest.raises(RouteBundleError, match="non-R4.0"):
        _write_config_candidates(build, tmp_path / "configs")
    assert not list((tmp_path / "configs").glob("*"))


def test_blocked_route_publishes_no_destination_or_partial_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_mop(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("MOP generation must not start for a blocked route")

    monkeypatch.setattr(bundle_module, "export_mop", forbidden_mop)

    with pytest.raises(RouteBundleError, match="bundle is blocked"):
        export_route_bundle(_project(pending=True), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_staged_sra_peer_publishes_no_bundle_or_partial_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_rls_r4_0_route_integration import _sra_pair_project

    complete = _sra_pair_project()
    staged = replace(
        complete,
        shelves=(
            complete.shelves[0],
            replace(complete.shelves[1], profile_payload={}),
        ),
    )
    readiness = staged.deployment_readiness()

    assert readiness.ready is False
    assert readiness.shelf_statuses[0].reason_codes == (
        "R40_PENDING_SRA_PEER_REVIEW",
    )

    def forbidden_mop(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError(
            "MOP generation must not start for a staged SRA pair"
        )

    monkeypatch.setattr(bundle_module, "export_mop", forbidden_mop)

    with pytest.raises(RouteBundleError, match="bundle is blocked"):
        export_route_bundle(staged, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_reciprocally_validated_sra_pair_publishes_complete_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_rls_r4_0_route_integration import _sra_pair_project

    monkeypatch.setattr(bundle_module, "export_mop", _fake_mop)
    project = _sra_pair_project()

    files = export_route_bundle(project, tmp_path)

    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    assert manifest["deployment_readiness"]["route_cli_ready"] is True
    assert manifest["configuration_candidates_complete"] is True
    assert manifest["configuration_candidate_count"] == 2
    assert [
        record["tid"] for record in manifest["configuration_candidates"]
    ] == [
        "USXGN1-L8I2",
        "USSAT4-L8R3",
    ]
    assert len(tuple(files["configs"].iterdir())) == 2


def test_r42_route_publishes_nothing(tmp_path: Path) -> None:
    project = _project()
    legacy = replace(
        project,
        shelves=tuple(
            replace(
                shelf,
                software_release="RLS R4.2",
                profile_payload={
                    "schema_id": "ciena.rls.protected-dci-request",
                    "schema_version": "2.0",
                    "request": {"profile": "protected_dci"},
                },
            )
            for shelf in project.shelves
        ),
    )

    with pytest.raises(RouteBundleError, match="validation failed"):
        export_route_bundle(legacy, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_repeated_bundle_export_never_overwrites_prior_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_module, "export_mop", _fake_mop)

    first = export_route_bundle(_project(), tmp_path)
    first_manifest = first["manifest"].read_bytes()
    second = export_route_bundle(_project(), tmp_path)

    assert first["manifest"].parent.name == "R40_RLS_route_deliverable"
    assert second["manifest"].parent.name == "R40_RLS_route_deliverable_2"
    assert first["manifest"].read_bytes() == first_manifest
    assert not list(tmp_path.glob(".rls_route_*"))
    assert not list(tmp_path.glob(".*.publish.lock"))


def test_failed_mop_generation_removes_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_mop(*_args: object, **_kwargs: object) -> Path:
        raise OSError("simulated renderer failure")

    monkeypatch.setattr(bundle_module, "export_mop", fail_mop)

    with pytest.raises(OSError, match="simulated renderer failure"):
        export_route_bundle(_project(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_publish_race_fails_without_replacing_new_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bundle_module, "export_mop", _fake_mop)
    raced_destination: Path | None = None

    def race_rename(source: Path, destination: Path) -> None:
        nonlocal raced_destination
        raced_destination = Path(destination)
        raced_destination.mkdir()
        (raced_destination / "other-process.txt").write_text(
            "do not replace",
            encoding="utf-8",
        )
        raise FileExistsError("simulated publication race")

    monkeypatch.setattr(bundle_module.os, "rename", race_rename)
    with pytest.raises(FileExistsError, match="simulated publication race"):
        export_route_bundle(_project(), tmp_path)

    assert raced_destination is not None
    assert (raced_destination / "other-process.txt").read_text(
        encoding="utf-8"
    ) == "do not replace"
    assert not list(tmp_path.glob(".rls_route_*"))
    assert not list(tmp_path.glob(".*.publish.lock"))


def test_invalid_route_is_rejected_before_any_bundle_is_created(
    tmp_path: Path,
) -> None:
    invalid = replace(_project(), shelves=(), links=())

    with pytest.raises(RouteBundleError, match="validation failed"):
        export_route_bundle(invalid, tmp_path)
    assert list(tmp_path.iterdir()) == []
