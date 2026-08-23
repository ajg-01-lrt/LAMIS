"""Atomic route-level MOP and configuration-candidate bundle export.

The route is one transaction: every ordered shelf must have a reviewed,
release-authorized provider payload before any CLI file is published.  A
planning-only, unreviewed, invalid, or failed shelf aborts the export, so ATLAS
can never emit a deceptively incomplete mixed-route bundle.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping

from .common import SUPPORTED_SOFTWARE_RELEASE
from .diagram_assets import (
    DiagramAssetError,
    WorkbookDiagram,
    validate_workbook_diagram_for_project,
)
from .mop_export import TEMPLATE_SHA256, export_mop
from .r4_0_generator import R40_PROVIDER_CATALOG
from .route_config import (
    RouteConfigBuild,
    RouteConfigError,
    require_complete_route_configs,
)
from .route_project import (
    DeploymentReadiness,
    RouteProject,
    RouteValidationIssue,
    route_native_fiber_review,
)


ROUTE_BUNDLE_SCHEMA = "atlas.ciena.rls.route-deliverable-bundle"
ROUTE_BUNDLE_SCHEMA_VERSION = "2.1"


class RouteBundleError(ValueError):
    """Raised when a complete route deliverable cannot be published."""


def _safe_component(value: str, fallback: str = "Ciena_RLS_Route") -> str:
    clean = "".join(
        character
        if character.isalnum() or character in {"-", "_", "."}
        else "_"
        for character in value.strip()
    )
    return clean.strip("._") or fallback


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _claim_destination(root: Path, base_name: str) -> tuple[Path, Path]:
    """Reserve one collision-free bundle name across concurrent ATLAS exports.

    The hidden lock is created with ``O_EXCL``. Other ATLAS processes therefore
    skip a name that is in-flight instead of racing between an existence check
    and publication.
    """

    counter = 1
    while True:
        suffix = "" if counter == 1 else f"_{counter}"
        destination = root / f"{base_name}{suffix}"
        lock_path = root / f".{destination.name}.publish.lock"
        counter += 1
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            continue
        try:
            os.close(descriptor)
            if destination.exists():
                lock_path.unlink(missing_ok=True)
                continue
        except BaseException:
            lock_path.unlink(missing_ok=True)
            raise
        return destination, lock_path


def _issue_dict(issue: RouteValidationIssue) -> dict[str, str]:
    return {
        "severity": issue.severity,
        "code": issue.code,
        "field": issue.field,
        "message": issue.message,
        "source": issue.source,
    }


def _readiness_dict(readiness: DeploymentReadiness) -> dict[str, Any]:
    return {
        "route_cli_ready": readiness.ready,
        "partial_cli_export_allowed": False,
        "shelves": [
            {
                "shelf_id": status.shelf_id,
                "profile_id": status.profile_id,
                "provider_available": status.provider_available,
                "ready": status.ready,
                "reason_codes": list(status.reason_codes),
                "reasons": list(status.reasons),
            }
            for status in readiness.shelf_statuses
        ],
        "blocking_reasons": list(readiness.blocking_reasons),
    }


def _validation_text(
    project: RouteProject,
    issues: tuple[RouteValidationIssue, ...],
    readiness: DeploymentReadiness,
    config_build: RouteConfigBuild | None = None,
) -> str:
    counts = project.irm_counts()
    errors = tuple(issue for issue in issues if issue.severity == "error")
    warnings = tuple(issue for issue in issues if issue.severity == "warning")
    endpoint_a, endpoint_z = project.endpoint_sites()
    native_fiber_status, native_fiber_token = route_native_fiber_review(
        project.links
    )
    source_fiber_label = ""
    source_fiber_scope = project.diagram_source.get(
        "route_fiber_type_scope_suggestion"
    )
    if isinstance(source_fiber_scope, Mapping):
        source_fiber_label = str(
            source_fiber_scope.get("value", "") or ""
        ).strip()
    route_optical_band = ""
    route_header = project.diagram_source.get("route_header")
    if (
        isinstance(route_header, Mapping)
        and route_header.get("optical_band_status") == "direct_supported"
    ):
        route_optical_band = {
            "c": "C",
            "l": "L",
            "c+l": "C+L",
            "integrated_c+l": "Integrated C+L",
        }.get(
            str(route_header.get("optical_band", "") or "").strip().casefold(),
            "",
        )
    lines = [
        "ATLAS Ciena RLS Route Deliverable Validation",
        "=" * 47,
        "DOCUMENT RESULT: VALID" if not errors else "DOCUMENT RESULT: FAILED",
        (
            "DOCUMENTED PRE-CALIBRATION CLI CANDIDATES: COMPLETE"
            if config_build is not None and config_build.ready
            else "DOCUMENTED PRE-CALIBRATION CLI CANDIDATES: BLOCKED"
        ),
        "",
        f"Route: {project.route_code}",
        f"Title: {project.title}",
        f"Revision: {project.revision}",
        f"OSPF area: {project.ospf_area or 'not provided'}",
        f"Ordered shelves: {len(project.shelves)}",
        f"Reviewed optical adjacencies: {len(project.links)}",
        (
            "Route native CLI fiber type: "
            f"{native_fiber_token or 'not confirmed'}"
        ),
        f"Route native fiber review: {native_fiber_status}",
        f"Diagram fiber label: {source_fiber_label or 'not available'}",
        (
            "Diagram route optical band: "
            f"{route_optical_band or 'not available'} (context only)"
        ),
        f"Rack diagrams: {project.rack_count}",
        f"Distinct sites: {counts.total_distinct_sites}",
        f"Site A: {endpoint_a.code if endpoint_a is not None else 'N/A'}",
        f"Site Z: {endpoint_z.code if endpoint_z is not None else 'N/A'}",
        "",
        "IRM shelf counts",
        "----------------",
        f"ILA: {counts.ila_shelves}",
        f"ROADM: {counts.roadm_shelves}",
        f"Add/Drop A: {counts.add_drop_a_shelves}",
        f"Add/Drop Z: {counts.add_drop_z_shelves}",
        f"Add/Drop side unresolved: {counts.add_drop_unassigned_shelves}",
        f"Add/Drop total: {counts.total_add_drop_shelves}",
        "",
        f"Validation errors: {len(errors)}",
        f"Validation warnings: {len(warnings)}",
    ]
    for issue in issues:
        lines.append(
            f"{issue.severity.upper()} [{issue.code}] "
            f"{issue.field}: {issue.message}"
        )
        if issue.source:
            lines.append(f"  Source: {issue.source}")

    if config_build is not None and config_build.ready:
        lines.extend(["", "COLAN candidate states", "----------------------"])
        for shelf_build in config_build.shelf_builds:
            artifact_manifest = shelf_build.artifact.manifest
            state = str(
                artifact_manifest.get("colan_state", "unknown") or "unknown"
            )
            emitted = bool(
                artifact_manifest.get("colan_commands_emitted", False)
            )
            lines.append(
                f"{shelf_build.order:03d} {shelf_build.tid}: {state}; "
                f"COLAN commands emitted: {'yes' if emitted else 'no'}"
            )

    lines.extend(["", "CLI deployment gate", "-------------------"])
    if config_build is not None and config_build.ready:
        lines.extend(
            [
                f"Generated {config_build.config_count} complete, validated "
                "pre-calibration CLI candidate artifact(s), one per ordered "
                "shelf.",
                "Provider-specific deployment controls are included "
                "automatically in every candidate validation report and "
                "manifest. They are requirements, not facts observed or "
                "verified by ATLAS.",
                "These files are not an on-box deployment approval. Before "
                "use, verify the exact target release/schema, installed PECs "
                "and licenses, the approved EDP/IDP, PlannerPlus output, or "
                "equivalent customer engineering package, discovered far-end "
                "endpoints, batch validation, and the calibration MOP.",
            ]
        )
    else:
        lines.extend(
            f"- {reason}" for reason in readiness.blocking_reasons
        )
        lines.append(
            "No partial route CLI was emitted. Every shelf must be reviewed "
            "and fall within the release-authorized topology of an audited "
            "provider before configuration-candidate export."
        )
    lines.extend(
        [
            "",
            "Artifact scope",
            "--------------",
            "The workbook, JSON snapshot, and generated configuration "
            "candidates contain controlled network-design data but no password, "
            "community string, private key, license key, or other credential "
            "material.",
            "The FBN and IRM sheets are dynamic. The procedure, packout, "
            "fibering, test, script, label, and teardown sheets are preserved "
            "from the controlled embedded template.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _write_config_candidates(
    build: RouteConfigBuild,
    configs_root: Path,
) -> list[dict[str, Any]]:
    """Write complete in-memory provider artifacts into the staging tree."""

    configs_root.mkdir(parents=False, exist_ok=False)
    records: list[dict[str, Any]] = []
    for shelf_build in build.shelf_builds:
        safe_tid = _safe_component(shelf_build.tid, "shelf")
        artifact = shelf_build.artifact
        artifact_manifest = dict(artifact.manifest)
        if (
            artifact_manifest.get("release") != SUPPORTED_SOFTWARE_RELEASE
            or artifact_manifest.get("generator") != "R40ExactConfigGenerator"
            or artifact_manifest.get("provider_id") not in R40_PROVIDER_CATALOG
        ):
            raise RouteBundleError(
                "Configuration staging rejected a non-R4.0 or unregistered "
                "provider artifact; no route bundle was published."
            )
        directory = configs_root / f"{shelf_build.order:03d}_{safe_tid}"
        directory.mkdir()
        artifact_release = _safe_component(
            str(artifact_manifest.get("release", "")),
            "RLS",
        )
        stem = (
            f"{safe_tid}_{artifact_release}_"
            f"{_safe_component(shelf_build.profile_id)}"
        )
        paths = {
            "cli": directory / f"{stem}.cli",
            "annotated": directory / f"{stem}_annotated.txt",
            "validation": directory / f"{stem}_validation.txt",
            "manifest": directory / f"{stem}_manifest.json",
        }
        paths["cli"].write_text(
            str(artifact.cli_text).rstrip() + "\n",
            encoding="utf-8",
            newline="\n",
        )
        paths["annotated"].write_text(
            str(artifact.annotated_text).rstrip() + "\n",
            encoding="utf-8",
            newline="\n",
        )
        paths["validation"].write_text(
            str(artifact.validation_report).rstrip() + "\n",
            encoding="utf-8",
            newline="\n",
        )
        paths["manifest"].write_text(
            json.dumps(
                artifact_manifest,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        records.append(
            {
                "order": shelf_build.order,
                "shelf_id": shelf_build.shelf_id,
                "profile_id": shelf_build.profile_id,
                "tid": shelf_build.tid,
                "directory": directory.name,
                "deployment_controls": list(
                    artifact_manifest.get("deployment_controls", ())
                ),
                "colan_policy": artifact_manifest.get("colan_policy", ""),
                "colan_state": artifact_manifest.get("colan_state", ""),
                "colan_commands_emitted": bool(
                    artifact_manifest.get("colan_commands_emitted", False)
                ),
                "files": {
                    role: {
                        "filename": path.name,
                        "sha256": _sha256(path),
                    }
                    for role, path in paths.items()
                },
            }
        )
    return records


def export_route_bundle(
    project: RouteProject,
    output_dir: str | os.PathLike[str],
    *,
    diagram: WorkbookDiagram | None = None,
) -> Mapping[str, Path]:
    """Publish one complete MOP/configuration bundle atomically.

    Configuration generation happens before publication.  If any ordered shelf
    is blocked, no destination directory and no partial CLI set are produced.
    """

    if not isinstance(project, RouteProject):
        raise TypeError("project must be a RouteProject")
    issues = project.validate()
    errors = tuple(issue for issue in issues if issue.severity == "error")
    if errors:
        raise RouteBundleError(
            "Route project validation failed: "
            + "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
        )
    try:
        diagram = validate_workbook_diagram_for_project(project, diagram)
    except DiagramAssetError as exc:
        raise RouteBundleError(
            "Route diagram attachment is invalid: " + str(exc)
        ) from exc
    try:
        config_build = require_complete_route_configs(project)
    except RouteConfigError as exc:
        raise RouteBundleError(
            "Route configuration bundle is blocked: " + str(exc)
        ) from exc

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    route = _safe_component(project.route_code)
    base_name = f"{route}_RLS_route_deliverable"
    destination, publication_lock = _claim_destination(root, base_name)

    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix=".rls_route_", dir=str(root)))
        stem = f"{route}_RLS_route"
        files = {
            "project": staging / f"{stem}_project.json",
            "mop": staging / f"{stem}_FBN_MOP.xlsx",
            "validation": staging / f"{stem}_validation.txt",
            "manifest": staging / f"{stem}_manifest.json",
            "configs": staging / "configs",
        }
        files["project"].write_text(
            json.dumps(
                project.to_dict(),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        export_mop(project, files["mop"], diagram=diagram)

        readiness = project.deployment_readiness()
        validation = _validation_text(
            project,
            issues,
            readiness,
            config_build,
        )
        files["validation"].write_text(
            validation,
            encoding="utf-8",
            newline="\n",
        )
        config_records = _write_config_candidates(
            config_build,
            files["configs"],
        )
        counts = project.irm_counts()
        native_fiber_status, native_fiber_token = route_native_fiber_review(
            project.links
        )
        manifest: dict[str, Any] = {
            "schema": ROUTE_BUNDLE_SCHEMA,
            "schema_version": ROUTE_BUNDLE_SCHEMA_VERSION,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "route_project_schema_version": project.schema_version,
            "project_id": project.project_id,
            "route_code": project.route_code,
            "title": project.title,
            "revision": project.revision,
            "ospf_area": project.ospf_area,
            "shelf_count": len(project.shelves),
            "link_count": len(project.links),
            "route_native_fiber_review": {
                "status": native_fiber_status,
                "token": native_fiber_token,
                "scope": "all_active_route_spans",
                "deployment_approved": False,
            },
            "rack_count": project.rack_count,
            "irm_counts": counts.to_dict(),
            "validation_issues": [_issue_dict(issue) for issue in issues],
            "deployment_readiness": _readiness_dict(readiness),
            "configuration_candidates_complete": True,
            "configuration_candidate_count": config_build.config_count,
            "configuration_candidates": config_records,
            "cli_candidate_files_included": True,
            "deployable_cli_included": False,
            "secret_material_included": False,
            "diagram_source": project.to_dict().get("diagram_source", {}),
            "source_diagram_file_included": False,
            "source_diagram_content_assessed": bool(project.diagram_source),
            "source_diagram_content_embedded_in_mop": diagram is not None,
            "diagram_embedding": (
                diagram.manifest_dict()
                if diagram is not None
                else {
                    "embedded": False,
                    "sheet": "Diagram",
                    "external_relationships": False,
                }
            ),
            "template": {
                "filename": "Ciena_RLS_FBN_MOP_Template.xlsx",
                "sha256": TEMPLATE_SHA256,
                "dynamic_sheets": ["FBN", "IRM", "Diagram"],
                "static_sheets_preserved": [
                    "Ciena FBN Checklist",
                    "Ciena FBN Procedure",
                    "Packouts",
                    "Fibering",
                    "FBN Test Setup",
                    "Debug",
                    "Test Channels CIENA",
                    "FBN Script Ciena",
                    "Label Standards",
                    "Tear Down",
                ],
            },
            "artifacts": {
                "project": {
                    "filename": files["project"].name,
                    "sha256": _sha256(files["project"]),
                },
                "mop": {
                    "filename": files["mop"].name,
                    "sha256": _sha256(files["mop"]),
                },
                "validation": {
                    "filename": files["validation"].name,
                    "sha256": _sha256(files["validation"]),
                },
                "configs": {
                    "directory": files["configs"].name,
                    "shelf_artifact_count": len(config_records),
                },
            },
        }
        files["manifest"].write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # ``rename`` is non-replacing on the supported Windows platform. The
        # exclusive publication lock prevents another ATLAS process from
        # selecting the same name; this final check also fails safely if an
        # unrelated process creates the destination after reservation.
        if destination.exists():
            raise FileExistsError(
                f"Route bundle destination appeared during publication: "
                f"{destination}"
            )
        os.rename(staging, destination)
        return {
            name: destination / path.name
            for name, path in files.items()
        }
    except BaseException:
        if (
            staging is not None
            and staging.exists()
            and staging.parent == root
        ):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        publication_lock.unlink(missing_ok=True)


__all__ = [
    "ROUTE_BUNDLE_SCHEMA",
    "ROUTE_BUNDLE_SCHEMA_VERSION",
    "RouteBundleError",
    "export_route_bundle",
]
