"""Headless contracts for the mixed-shelf Ciena RLS route editor."""

from __future__ import annotations

import ast
from dataclasses import replace
import importlib
import inspect
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from utils.rls_config.r4_0_generator import R40_PAYLOAD_SCHEMA_VERSION


_ROOT = Path(__file__).resolve().parents[1]
_GUI_PATH = _ROOT / "gui" / "rls_route_frame.py"


def _source() -> str:
    return _GUI_PATH.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=str(_GUI_PATH))


def _class(name: str) -> ast.ClassDef:
    return next(
        node
        for node in _tree().body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method(class_name: str, method_name: str) -> ast.FunctionDef:
    return next(
        node
        for node in _class(class_name).body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def _route_module():
    return importlib.import_module("gui.rls_route_frame")


def _row(module, **overrides):
    values = {
        "shelf_id": "shelf-1",
        "profile_id": "ila",
        "site_key": "site-chi",
        "site_code": "CHI",
        "site_name": "Chicago",
        "tid": "CHI-ILA-01",
        "primary_oam_ip": "192.0.2.10",
        "software_release": "RLS R4.0",
        "shelf_variant": "K74-C894-900",
        "raman_label": "Slot 4",
        "power_label": "A/B",
    }
    values.update(overrides)
    return module._ShelfEditorRow(**values)


def _no_sra_source_evidence(**overrides: object) -> dict[str, object]:
    source_sha256 = "b" * 64
    evidence: dict[str, object] = {
        "source_sha256": source_sha256,
        "raman_callout_convention": {
            "id": "small-red-slot-port-v1",
            "source_sha256": source_sha256,
            "scope": "source",
            "deployable_cli": False,
        },
        "raman_callouts": [],
        "raman_callout_review": "not_applicable",
    }
    evidence.update(overrides)
    return evidence


def _sra_source_evidence(
    tid: str,
    slot: int,
    endpoints: list[dict[str, object]],
) -> dict[str, object]:
    source_sha256 = "b" * 64
    return {
        "source_sha256": source_sha256,
        "raman_callout_convention": {
            "id": "small-red-slot-port-v1",
            "source_sha256": source_sha256,
            "scope": "source",
            "deployable_cli": False,
        },
        "raman_callouts": [
            {
                "raw_text": f"{slot}/{port}",
                "slot": slot,
                "port": port,
                "shelf_tid": tid,
                "context": "shelf_endpoint",
                "evidence": [
                    {
                        "field": "slot_port",
                        "normalized_value": f"{slot}/{port}",
                        "confidence": 0.99,
                        "method": "vision",
                    }
                ],
                "deployable_cli": False,
            }
            for port in (5, 6)
        ],
        "raman_callout_review": "accepted",
        "line_endpoints": endpoints,
    }


def _line_endpoint(
    index: int,
    *,
    adjacency: str,
    slot: int,
    line_out_port: int,
) -> dict[str, object]:
    return {
        "adjacency": adjacency,
        "slot": slot,
        "line_out_port": line_out_port,
        "evidence": [
            {
                "field": f"line_endpoints.{index}.adjacency",
                "normalized_value": adjacency,
                "confidence": 0.99,
                "method": "inferred",
            },
            {
                "field": f"line_endpoints.{index}.slot",
                "normalized_value": slot,
                "confidence": 0.99,
                "method": "vision",
            },
            {
                "field": f"line_endpoints.{index}.line_out_port",
                "normalized_value": line_out_port,
                "confidence": 0.99,
                "method": "vision",
            },
        ],
    }


def test_module_import_is_headless_and_frame_accepts_parent_only() -> None:
    module = _route_module()
    signature = inspect.signature(module.RlsRouteFrame)

    assert "parent" in signature.parameters
    assert signature.parameters["controller"].default is None
    assert module.RlsRouteFrame.__name__ == "RlsRouteFrame"


def test_editor_exposes_full_master_detail_route_workflow() -> None:
    source = _source()
    frame = _class("RlsRouteFrame")
    method_names = {
        node.name for node in frame.body if isinstance(node, ast.FunctionDef)
    }

    assert {
        "_add_shelf",
        "_update_selected",
        "_remove_selected",
        "_move_selected",
        "_save_project",
        "_open_project",
        "_export_bundle",
        "_upload_route_diagram",
        "_reattach_route_diagram",
        "_preview_mop",
        "_start_config_evaluation",
        "_review_selected_configuration",
        "_confirm_and_next_pending",
        "_load_editor_row",
    } <= method_names
    assert "_export_mop" not in method_names
    for label in (
        "Ordered route shelves",
        "Shelf type",
        "Site code",
        "Site name",
        "TID",
        "Primary OAM IP",
        "Software release",
        "Native CLI fiber type",
        "Apply to all spans",
        "Shelf variant / PEC",
        "RAMAN display",
        "Power",
        "Provider",
        "Move Up",
        "Move Down",
        "Confirm & Next Pending",
        "Upload Route Diagram",
        "Reattach Diagram",
        "Preview MOP",
        "Review Configuration",
        "Export Route Bundle",
    ):
        assert label in source
    assert "Export Styled FBN MOP" not in source
    assert "orient=tk.HORIZONTAL" in source
    assert "xscrollcommand=xscroll.set" in source
    assert source.index('text="New"') < source.index('text="Open Project')
    assert source.index('text="Open Project') < source.index('text="Save Project')
    assert source.index('text="Save Project') < source.index(
        'text="Upload Route Diagram'
    )
    assert source.index('text="Upload Route Diagram') < source.index(
        'text="Reattach Diagram'
    )
    assert source.index('text="Reattach Diagram') < source.index(
        'text="Preview MOP"'
    )
    assert source.index('text="Preview MOP"') < source.index(
        'text="Export Route Bundle'
    )
    build_editor = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_build_editor")
    )
    assert build_editor is not None
    assert "value=R40_UI_RELEASE" in build_editor
    assert '"Software release"' in build_editor
    assert 'state="readonly"' in build_editor
    select_handler = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_on_tree_select")
    )
    assert select_handler is not None
    assert "self._load_editor_row(row)" in select_handler
    load_handler = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_load_editor_row")
    )
    assert load_handler is not None
    assert "_release_var.set(R40_UI_RELEASE)" in load_handler
    reattach_handler = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_reattach_route_diagram")
    )
    assert reattach_handler is not None
    assert "load_diagram_source" in reattach_handler
    assert "validate_workbook_diagram_for_project" in reattach_handler
    assert "_diagram_provider_factory" not in reattach_handler
    assert "_submit_background" not in reattach_handler


def test_planning_only_cli_boundary_is_prominent_and_no_cli_export_exists() -> None:
    module = _route_module()
    source = _source()

    assert "planning/documentation-only" in module.PLANNING_CLI_NOTICE
    assert "exact RLS 4.0 projects only" in module.PLANNING_CLI_NOTICE
    assert "RLS 4.0 supports multiple" in module.PLANNING_CLI_NOTICE
    assert "site roles" in module.PLANNING_CLI_NOTICE
    assert "RLS 4.2" not in module.PLANNING_CLI_NOTICE
    assert module.profile_readiness_label("ila") == (
        "R4.0 review only — CLI gated"
    )
    assert module.profile_readiness_label("add_drop_a") == (
        "R4.0 review only — CLI gated"
    )
    assert (
        module.profile_readiness_label("roadm_z")
        == "R4.0 review only — CLI gated"
    )
    assert "RLSConfigGenerator" not in source
    assert "legacy_generator" not in source
    assert "export_artifact" not in source
    assert "Export CLI" not in source


@pytest.mark.parametrize("review_state", ("confirmed", "corrected"))
def test_reviewed_shelf_without_exact_payload_is_cli_pending(
    review_state: str,
) -> None:
    module = _route_module()

    assert (
        module.profile_readiness_label(
            "ila",
            review_state=review_state,
            advisory_label="SRA provider required — CLI blocked",
            profile_payload={},
        )
        == "Confirmed - CLI Pending"
    )


def test_reviewed_shelf_with_exact_payload_keeps_validation_status() -> None:
    module = _route_module()
    payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }

    assert (
        module.profile_readiness_label(
            "ila",
            review_state="confirmed",
            profile_payload=payload,
        )
        == "Exact R4.0 provider — validation pending"
    )
    assert (
        module.profile_readiness_label(
            "ila",
            review_state="confirmed",
            advisory_label="Config validation blocked",
            profile_payload=payload,
        )
        == "Config validation blocked"
    )


def test_retired_exact_payload_requires_deliberate_re_review() -> None:
    module = _route_module()

    assert (
        module.profile_readiness_label(
            "ila",
            review_state="confirmed",
            profile_payload={
                "schema_id": "ciena.rls.r4-0-exact-request",
                "schema_version": "1.2",
            },
        )
        == "Exact review outdated — re-review required"
    )


def test_diagram_issue_summary_surfaces_blockers_and_truncates() -> None:
    module = _route_module()
    issues = [
        SimpleNamespace(
            blocking=True,
            code=f"BLOCK_{index}",
            field=f"shelves[{index}]",
            message=f"Review {index}",
        )
        for index in range(3)
    ]
    issues.append(
        SimpleNamespace(
            blocking=False,
            code="INFO",
            field="route",
            message="Advisory",
        )
    )

    summary = module.diagram_issue_summary(issues, limit=2)

    assert "Transcription issue types:" in summary
    assert "BLOCK_0:1" in summary
    assert "BLOCK_1:1" in summary
    assert "other:2" in summary
    assert "shelves[]:3" in summary
    assert "Review 0" not in summary
    assert "Advisory" not in summary


def test_diagram_issue_aggregation_normalizes_paths_without_customer_values() -> None:
    module = _route_module()
    issues = (
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="shelves[1].primary_oam_ip",
            message="Customer value 192.0.2.10 was absent.",
        ),
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="shelves[17].primary_oam_ip",
            message="Customer value 192.0.2.99 was absent.",
        ),
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="shelves[2].module_inventory.0.pec",
            message="Module customer-pec was absent.",
        ),
        SimpleNamespace(
            blocking=True,
            code="BAD\nINJECT",
            field="shelves[3].tid\ncustomer=SECRET",
            message="SECRET",
        ),
        SimpleNamespace(
            blocking=False,
            code="PLANNED_REMOVE_EXCLUDED",
            field="shelves[16].lifecycle",
            message="Customer TID removed.",
        ),
    )

    aggregate = module.aggregate_diagram_review_issues(issues)
    summary = module.diagram_issue_summary(issues)

    assert aggregate.issue_count == 5
    assert aggregate.required_review_count == 4
    assert aggregate.advisory_count == 1
    assert aggregate.code_counts == (
        ("MISSING_REQUIRED_FIELD", 3),
        ("PLANNED_REMOVE_EXCLUDED", 1),
        ("UNKNOWN", 1),
    )
    assert aggregate.missing_leaf_counts == (
        ("primary_oam_ip", 2),
        ("pec", 1),
    )
    assert aggregate.missing_path_counts == (
        ("shelves[].primary_oam_ip", 2),
        ("shelves[].module_inventory[].pec", 1),
    )
    assert "192.0.2.10" not in summary
    assert "customer-pec" not in summary
    assert "SECRET" not in summary
    assert "\nINJECT" not in summary


def test_diagram_issue_aggregation_explains_sample_route_eighty_two() -> None:
    module = _route_module()
    issues = [
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="route.revision",
            message="Required value is absent.",
        )
    ]
    issues.extend(
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field=f"shelves[{order}].site_code",
            message="Required value is absent.",
        )
        for order in range(1, 18)
    )
    for order in (*range(1, 16), 17):
        issues.extend(
            SimpleNamespace(
                blocking=True,
                code="MISSING_REQUIRED_FIELD",
                field=f"shelves[{order}].{field_name}",
                message="Required value is absent.",
            )
            for field_name in (
                "software_release",
                "shelf_variant",
                "power_label",
                "raman_label",
            )
        )
    issues.append(
        SimpleNamespace(
            blocking=False,
            code="PLANNED_REMOVE_EXCLUDED",
            field="shelves[16].lifecycle",
            message="Planned removal excluded.",
        )
    )

    aggregate = module.aggregate_diagram_review_issues(issues)

    assert aggregate.issue_count == 83
    assert aggregate.required_review_count == 82
    assert aggregate.advisory_count == 1
    assert aggregate.missing_leaf_counts == (
        ("site_code", 17),
        ("power_label", 16),
        ("raman_label", 16),
        ("shelf_variant", 16),
        ("software_release", 16),
        ("revision", 1),
    )
    assert aggregate.missing_path_counts == (
        ("shelves[].site_code", 17),
        ("shelves[].power_label", 16),
        ("shelves[].raman_label", 16),
        ("shelves[].shelf_variant", 16),
        ("shelves[].software_release", 16),
        ("route.revision", 1),
    )


def _fiber_evidence(
    value: str,
    *,
    confidence: float = 0.99,
    method: str = "vision",
):
    raw = {
        "field": "fiber_type",
        "raw_text": value,
        "normalized_value": value,
        "confidence": confidence,
        "image_index": 0,
        "bbox": [0.1, 0.1, 0.2, 0.2],
        "method": method,
    }
    return SimpleNamespace(
        **raw,
        to_dict=lambda current=dict(raw): dict(current),
    )


def test_contextual_review_accounting_reclassifies_route_86_to_defaults() -> None:
    module = _route_module()
    physical_shelves = tuple(
        SimpleNamespace(
            order=order,
            tid=f"US{order:03}-L8I2",
            site_code=None,
            software_release=None,
            shelf_variant=None,
            chassis="R2 600mm",
        )
        for order in range(1, 18)
    )
    active_shelves = tuple(
        shelf for shelf in physical_shelves if shelf.order != 16
    )
    active_spans = tuple(
        SimpleNamespace(
            order=order,
            from_tid=active_shelves[order - 1].tid,
            to_tid=active_shelves[order].tid,
            fiber_type=None if order == 15 else "LEAF",
            evidence=(
                () if order == 15 else (_fiber_evidence("LEAF"),)
            ),
        )
        for order in range(1, 16)
    )
    issues = [
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="route.revision",
            message="Required value is absent.",
        )
    ]
    issues.extend(
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field=f"shelves[{order}].site_code",
            message="Required value is absent.",
        )
        for order in range(1, 18)
    )
    for order in (*range(1, 16), 17):
        issues.extend(
            SimpleNamespace(
                blocking=True,
                code="MISSING_REQUIRED_FIELD",
                field=f"shelves[{order}].{field_name}",
                message="Required value is absent.",
            )
            for field_name in (
                "software_release",
                "shelf_variant",
                "power_label",
                "raman_label",
            )
        )
    issues.extend(
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field=f"spans[15].{field_name}",
            message="Required value is absent.",
        )
        for field_name in (
            "circuit_id",
            "fiber_start",
            "fiber_end",
            "fiber_type",
        )
    )
    raw_rows = tuple(
        {
            **_row(
                module,
                shelf_id=f"shelf-{shelf.order}",
                site_key=f"site-{shelf.order}",
                site_code="",
                tid=shelf.tid,
                software_release="",
                shelf_variant=shelf.chassis,
                raman_label="",
                power_label="",
            ).__dict__,
            "source_evidence": {
                "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
                "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
                "source_sha256": "diagram-sha",
                "software_release": None,
                "shelf_variant": None,
                "chassis": shelf.chassis,
                "fields": [],
            },
        }
        for shelf in active_shelves
    )
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        revision=None,
        shelves=physical_shelves,
        active_shelves=active_shelves,
        active_spans=active_spans,
        issues=tuple(issues),
        gui_rows=lambda: raw_rows,
    )
    rows = module._diagram_editor_rows(result)
    links = module._diagram_route_links(result, rows)

    accounting = module.account_diagram_review_issues(
        result,
        rows,
        revision_default={
            "value": "1",
            "reason": (
                "new ATLAS deliverable revision; not customer-diagram evidence"
            ),
        },
        links=links,
    )

    assert accounting.raw.required_review_count == 86
    assert accounting.source_absence_count == 86
    assert accounting.unresolved.required_review_count == 0
    assert accounting.category_counts == (
        ("unresolved", 0),
        ("defaulted", 33),
        ("suggestion_pending", 32),
        ("scope_inherited", 1),
        ("optional", 19),
        ("lifecycle_excluded", 1),
    )
    assert accounting.unresolved.missing_leaf_counts == ()
    assert accounting.unresolved.missing_path_counts == ()
    inherited_path = links[14].paths[0]
    assert inherited_path.fiber_type == ""
    marker = inherited_path.source_evidence[
        "route_fiber_type_scope_suggestion"
    ]
    assert marker["value"] == "LEAF"
    assert marker["observed_span_orders"] == tuple(range(1, 15))
    assert marker["inherited_span_orders"] == (15,)
    assert marker["deployable_cli"] is False
    assert all(row.review_state == "pending" for row in rows)
    assert all(row.profile_payload == {} for row in rows)


def _fiber_scope_result(
    values: tuple[str | None, ...],
    *,
    confidence: float = 0.99,
    method: str = "vision",
):
    return SimpleNamespace(
        source=SimpleNamespace(sha256="fiber-source-sha"),
        active_spans=tuple(
            SimpleNamespace(
                order=order,
                fiber_type=value,
                evidence=(
                    ()
                    if value is None
                    else (
                        _fiber_evidence(
                            value,
                            confidence=confidence,
                            method=method,
                        ),
                    )
                ),
            )
            for order, value in enumerate(values, start=1)
        ),
    )


def test_route_optical_band_is_prepopulated_only_with_supported_evidence() -> None:
    module = _route_module()
    supported = SimpleNamespace(
        optical_band="c+l",
        issues=(),
    )
    unverified = SimpleNamespace(
        optical_band="c+l",
        issues=(
            SimpleNamespace(
                blocking=True,
                field="route.optical_band",
            ),
        ),
    )

    assert module._diagram_route_optical_band_status(supported) == (
        "direct_supported"
    )
    assert module._diagram_route_optical_band_status(unverified) == "unverified"
    assert module._diagram_route_optical_band_status(
        SimpleNamespace(optical_band=None, issues=())
    ) == "not_observed"
    assert module._display_optical_band_for_review("c+l") == "C+L"


def test_diagram_source_record_persists_verified_route_band_context() -> None:
    module = _route_module()
    result = SimpleNamespace(
        source=SimpleNamespace(
            file_name="route.png",
            source_type="png",
            sha256="a" * 64,
            size_bytes=123,
            images=(),
        ),
        route_code="RL-1",
        title="ELP1-SAT4",
        revision=None,
        ospf_area="10.6.8.0",
        optical_band="c+l",
        route_evidence=(),
        active_spans=(),
        shelves=(),
        active_shelves=(),
        issues=(),
        route_title_derivation={
            "rule_id": "terminal-site-route-title-v1",
            "status": "controlled_derivation",
            "value": "ELP1-SAT4",
            "provider_title": "USELP1-USSAT4 — Ciena RLS",
            "observed_header_pair": "USELP1-USSAT4",
            "endpoint_tids": ("USELP1-L8R2", "USSAT4-L8R3"),
            "endpoint_codes": ("USELP1", "USSAT4"),
            "display_codes": ("ELP1", "SAT4"),
            "removed_shared_prefix": "US",
            "source_sha256": "a" * 64,
            "deployable_cli": False,
        },
        raman_callout_provenance={},
    )

    record = module._diagram_source_record(result)

    assert record["schema_version"] == module.DIAGRAM_EVIDENCE_SCHEMA_VERSION
    assert record["route_header"]["optical_band"] == "c+l"
    assert record["route_header"]["optical_band_status"] == (
        "direct_supported"
    )
    assert record["route_title_derivation"]["value"] == "ELP1-SAT4"
    assert record["route_title_derivation"]["display_codes"] == (
        "ELP1",
        "SAT4",
    )


def test_route_fiber_scope_requires_one_missing_unanimous_direct_value() -> None:
    module = _route_module()

    scope = module.diagram_fiber_type_scope(
        _fiber_scope_result(("LEAF", "LEAF", None))
    )

    assert scope is not None
    assert scope.value == "LEAF"
    assert scope.observed_span_orders == (1, 2)
    assert scope.inherited_span_order == 3
    assert (
        module.diagram_fiber_type_scope(
            _fiber_scope_result(("LEAF", "NDSF", None))
        )
        is None
    )
    assert (
        module.diagram_fiber_type_scope(
            _fiber_scope_result(("LEAF", None, None))
        )
        is None
    )
    assert (
        module.diagram_fiber_type_scope(
            _fiber_scope_result(
                ("LEAF", "LEAF", None),
                confidence=0.5,
            )
        )
        is None
    )
    assert (
        module.diagram_fiber_type_scope(
            _fiber_scope_result(
                ("LEAF", "LEAF", None),
                method="inferred",
            )
        )
        is None
    )


def test_fiber_scope_accounting_requires_exact_path_provenance() -> None:
    module = _route_module()
    result = _fiber_scope_result(("LEAF", "LEAF", None))
    result.revision = "1"
    result.shelves = ()
    result.active_shelves = ()
    result.issues = (
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field="spans[3].fiber_type",
            message="Required value is absent.",
        ),
    )
    scope = module.diagram_fiber_type_scope(result)
    assert scope is not None
    marker = module._fiber_type_scope_marker(scope)
    path = module.OpticalPath(
        path_id="path-3",
        path_role="route",
        review_state="pending",
        source_evidence={
            "route_fiber_type_scope_suggestion": marker,
        },
    )
    link = module.RouteLink(
        link_id="link-3",
        order=3,
        from_shelf_id="a",
        to_shelf_id="b",
        paths=(path,),
    )

    accepted = module.account_diagram_review_issues(
        result,
        (),
        links=(link,),
    )
    forged = dict(marker)
    forged["source_sha256"] = "forged"
    forged_path = replace(
        path,
        source_evidence={
            "route_fiber_type_scope_suggestion": forged,
        },
    )
    rejected = module.account_diagram_review_issues(
        result,
        (),
        links=(replace(link, paths=(forged_path,)),),
    )

    assert accepted.unresolved.required_review_count == 0
    assert accepted.scope_inherited_count == 1
    assert rejected.unresolved.required_review_count == 1
    assert rejected.scope_inherited_count == 0


def test_contextual_review_accounting_requires_exact_local_provenance() -> None:
    module = _route_module()
    shelf = SimpleNamespace(
        order=1,
        tid="USELP1-L8R2",
        site_code=None,
        software_release=None,
        shelf_variant=None,
        chassis="R2 600mm",
    )
    base_evidence = {
        "software_release": None,
        "shelf_variant": None,
        "chassis": "R2 600mm",
        "software_release_scope_default": {
            "value": module.R40_UI_RELEASE,
            "reason": "forged reason",
        },
        "site_code_review_suggestion": {
            "value": "USELP1",
            "source_field": "tid",
            "reason": "forged reason",
        },
        "shelf_variant_chassis_suggestion": {
            "value": "R2 600mm",
            "source_field": "chassis",
            "reason": "forged reason",
        },
    }
    row = _row(
        module,
        site_code="USELP1",
        tid=shelf.tid,
        software_release=module.R40_UI_RELEASE,
        shelf_variant="R2 600mm",
        review_state="pending",
        source_evidence=base_evidence,
    )
    issues = tuple(
        SimpleNamespace(
            blocking=True,
            code="MISSING_REQUIRED_FIELD",
            field=path,
            message="Required value is absent.",
        )
        for path in (
            "route.revision",
            "shelves[1].software_release",
            "shelves[1].site_code",
            "shelves[1].shelf_variant",
        )
    ) + (
        SimpleNamespace(
            blocking=True,
            code="LOW_CONFIDENCE_FIELD",
            field="shelves[1].software_release",
            message="Evidence confidence is too low.",
        ),
    )
    result = SimpleNamespace(
        revision=None,
        shelves=(shelf,),
        active_shelves=(shelf,),
        issues=issues,
    )

    accounting = module.account_diagram_review_issues(
        result,
        (row,),
        revision_default={"value": "1", "reason": "forged reason"},
    )

    assert accounting.unresolved.required_review_count == 5
    assert accounting.defaulted_count == 0
    assert accounting.suggestion_pending_count == 0
    assert accounting.scope_inherited_count == 0
    assert accounting.optional_count == 0
    assert accounting.lifecycle_excluded_count == 0


def _diagram_shelf(
    order: int,
    tid: str,
    *,
    profile_family: str = "ila",
    profile_id: str = "ila",
    ip: str = "192.0.2.10",
    site_code: str = "SITE",
    site_name: str = "Site",
    chassis: str = "R2 600mm",
):
    return SimpleNamespace(
        order=order,
        tid=tid,
        primary_oam_ip=ip,
        site_code=site_code,
        site_name=site_name,
        profile_family=profile_family,
        profile_id=profile_id,
        chassis=chassis,
    )


def _diagram_span(order: int, from_tid: str, to_tid: str):
    return SimpleNamespace(
        order=order,
        from_tid=from_tid,
        to_tid=to_tid,
    )


def _diagram_gate_result(
    *,
    shelves,
    active_shelves,
    spans,
    active_spans,
    issues=(),
    route_code: str = "RL-0037805",
    title: str = "USELP1-USSAT4",
    ospf_area: str = "10.6.8.0",
):
    return SimpleNamespace(
        route_code=route_code,
        title=title,
        revision=None,
        ospf_area=ospf_area,
        shelves=tuple(shelves),
        active_shelves=tuple(active_shelves),
        spans=tuple(spans),
        active_spans=tuple(active_spans),
        issues=tuple(issues),
    )


def test_diagram_mutation_gate_allows_coherent_release_incomplete_route() -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="USELP1",
    )
    removed = _diagram_shelf(2, "USSAT3-L8I2")
    last = _diagram_shelf(
        3,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    config_issues = (
        SimpleNamespace(
            code="MISSING_REQUIRED_FIELD",
            field="shelves[1].software_release",
            message="Software release is absent.",
            blocking=True,
        ),
        SimpleNamespace(
            code="INVALID_EVIDENCE_BBOX",
            field="shelves[0].config_evidence[0].bbox",
            message=(
                "Evidence for 'shelves[0].tid' was discarded "
                "because its bounding box is invalid."
            ),
            blocking=True,
        ),
    )
    result = _diagram_gate_result(
        shelves=(first, removed, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=config_issues,
    )

    assert module.diagram_import_mutation_blockers(result) == ()


def test_diagram_mutation_gate_allows_retained_edge_clipped_evidence() -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="USELP1",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    clipped_issue = SimpleNamespace(
        code="EVIDENCE_BBOX_EDGE_CLIPPED",
        field="shelves[1].evidence[0].bbox",
        message="Evidence rectangle was clipped to the source-image edge.",
        blocking=False,
    )
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=(clipped_issue,),
    )

    assert module.diagram_import_mutation_blockers(result) == ()


def test_diagram_mutation_gate_rejects_unsupported_nonblank_route_title() -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="USELP1",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    result = _diagram_gate_result(
        title="USELP1-USSAT4 — Ciena RLS",
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=(
            SimpleNamespace(
                code="MISSING_MATCHING_EVIDENCE",
                field="route.title",
                message="Value has no matching evidence.",
                blocking=True,
            ),
        ),
    )

    blockers = module.diagram_import_mutation_blockers(result)

    assert [(item.code, item.field) for item in blockers] == [
        ("UNSUPPORTED_ROUTE_TITLE", "route.title")
    ]


def test_diagram_mutation_gate_allows_missing_config_readiness_identity_fields() -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        ip="",
        site_code="USELP1",
        chassis="",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        ip="",
        site_code="USSAT4",
        chassis="",
    )
    span = _diagram_span(1, first.tid, last.tid)
    readiness_issues = (
        SimpleNamespace(
            code="MISSING_REQUIRED_FIELD",
            field="shelves[1].primary_oam_ip",
            message="Required value is absent from the diagram.",
            blocking=True,
        ),
        SimpleNamespace(
            code="MISSING_REQUIRED_FIELD",
            field="shelves[2].chassis",
            message="Required value is absent from the diagram.",
            blocking=True,
        ),
    )
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=readiness_issues,
    )

    assert module.diagram_import_mutation_blockers(result) == ()


def test_diagram_mutation_gate_keeps_missing_tid_fail_closed() -> None:
    module = _route_module()
    first_values = {
        "tid": "",
        "profile_family": "roadm",
        "profile_id": "roadm_a",
        "site_code": "USELP1",
        "site_name": "El Paso",
    }
    first = _diagram_shelf(1, **first_values)
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
    )

    blockers = module.diagram_import_mutation_blockers(result)

    assert any(
        blocker.code == "MISSING_SHELF_IDENTITY"
        and blocker.field == "shelves[1].tid"
        for blocker in blockers
    )


def test_diagram_mutation_gate_allows_missing_sites_with_tid_prefix_suggestions():
    module = _route_module()
    shelves = (
        _diagram_shelf(
            1,
            "USELP1-L8R2",
            profile_family="roadm",
            profile_id="roadm_a",
            site_code="",
            site_name="",
        ),
        _diagram_shelf(
            2,
            "USQTN1-L8I2",
            site_code="",
            site_name="",
        ),
        _diagram_shelf(
            3,
            "USSAT4-L8R3",
            profile_family="roadm",
            profile_id="roadm_z",
            site_code="",
            site_name="",
        ),
    )
    spans = (
        _diagram_span(1, shelves[0].tid, shelves[1].tid),
        _diagram_span(2, shelves[1].tid, shelves[2].tid),
    )
    result = _diagram_gate_result(
        shelves=shelves,
        active_shelves=shelves,
        spans=spans,
        active_spans=spans,
    )

    assert module.diagram_import_mutation_blockers(result) == ()


def test_diagram_mutation_gate_rejects_missing_site_without_usable_tid_prefix():
    module = _route_module()
    first = _diagram_shelf(
        1,
        "LAB3",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="",
        site_name="",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
    )

    blockers = module.diagram_import_mutation_blockers(result)

    assert any(
        blocker.code == "MISSING_SHELF_IDENTITY"
        and blocker.field == "shelves[1].site"
        for blocker in blockers
    )


def test_unresolved_diagram_roles_enter_pending_review_but_manual_blanks_do_not():
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="unknown",
        profile_id="",
        site_code="USELP1",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="unknown",
        profile_id="",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    gate_result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
    )

    assert module.diagram_import_mutation_blockers(gate_result) == ()

    evidence = {
        "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
        "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
        "fields": [],
    }
    raw = {
        **_row(module, profile_id="").__dict__,
        "profile_id": "",
        "source_evidence": evidence,
    }
    editor_result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (raw,),
    )
    pending_rows = module._diagram_editor_rows(editor_result)

    assert pending_rows[0].review_state == "pending"
    assert module.profile_display_name("") == module.UNRESOLVED_PROFILE_LABEL
    assert module._r40_only_rows_error(pending_rows) == ""

    manual_blank = replace(
        pending_rows[0],
        review_state="manual",
        source_evidence={},
    )
    assert "<blank>" in module._r40_only_rows_error((manual_blank,))


def test_diagram_mutation_gate_rejects_incomplete_route_before_gui_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USBLP1-LAB2",
        profile_family="add_drop",
        profile_id="add_drop_a",
        ip="",
        site_code="",
        site_name="",
        chassis="",
    )
    second = _diagram_shelf(2, "LAB3")
    third = _diagram_shelf(
        3,
        "LAB4",
        profile_family="roadm",
        profile_id="roadm_z",
    )
    spans = (
        _diagram_span(1, first.tid, second.tid),
        _diagram_span(2, second.tid, third.tid),
        _diagram_span(3, third.tid, "LAB5"),
    )
    invalid_bbox = SimpleNamespace(
        code="INVALID_EVIDENCE_BBOX",
        field="shelves[0].evidence[0].bbox",
        message=(
            "Evidence for 'primary_oam_ip' was discarded because its "
            "bounding box is invalid."
        ),
        blocking=True,
    )
    result = _diagram_gate_result(
        shelves=(first, second, third),
        active_shelves=(first, second, third),
        spans=spans,
        active_spans=spans,
        issues=(invalid_bbox,),
    )
    old_rows = [object()]
    status: list[str] = []
    warnings: list[tuple[object, ...]] = []
    log_events: list[str] = []
    subject = SimpleNamespace(
        _rows=old_rows,
        _refresh_status=lambda message="": status.append(message),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **_kwargs: warnings.append(args),
    )
    monkeypatch.setattr(
        module,
        "_log_route_event",
        lambda message, *_args, **_kwargs: log_events.append(message),
    )

    blockers = module.diagram_import_mutation_blockers(result)
    module.RlsRouteFrame._apply_diagram_import(subject, result)

    codes = {blocker.code for blocker in blockers}
    assert {
        "ACTIVE_SPAN_COUNT",
        "SPAN_ROUTE_DISCONTINUITY",
        "CRITICAL_EVIDENCE_BBOX_DISCARDED",
    } <= codes
    assert subject._rows is old_rows
    assert status == [
        "Diagram transcription incomplete; current route unchanged."
    ]
    assert warnings
    assert "route unchanged" in str(warnings[0][0]).casefold()
    assert log_events
    assert "integrity_fields=" in log_events[0]
    assert "spans" in log_events[0]
    assert "shelves[1].site" not in log_events[0]
    assert "importer_blockers=1" in log_events[0]
    assert "importer_issue_codes=INVALID_EVIDENCE_BBOX" in log_events[0]


def test_apply_diagram_import_rejects_non_r40_rows_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="USELP1",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    config_issue = SimpleNamespace(
        severity="error",
        code="UNSUPPORTED_SOFTWARE_RELEASE",
        field="shelves[1].software_release",
        message="Software release RLS R4.2 is outside the R4.0 product scope.",
        blocking=True,
    )
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=(config_issue,),
    )
    result.source = SimpleNamespace(
        file_name="route.png",
        source_type="png",
        sha256="a" * 64,
        size_bytes=123,
        images=(),
    )
    result.route_evidence = ()
    result.blocking_issues = (config_issue,)
    result.gui_rows = lambda: (
        {
            **_row(
                module,
                shelf_id="a",
                profile_id="roadm_a",
                site_key="uselp1",
                site_code="USELP1",
                site_name="El Paso",
                tid=first.tid,
                primary_oam_ip=first.primary_oam_ip,
                software_release="RLS R4.2",
                shelf_variant=first.chassis,
            ).__dict__,
            "source_evidence": {"fields": []},
        },
        {
            **_row(
                module,
                shelf_id="z",
                profile_id="roadm_z",
                site_key="ussat4",
                site_code="USSAT4",
                site_name="San Antonio",
                tid=last.tid,
                primary_oam_ip=last.primary_oam_ip,
                software_release="RLS R4.2",
                shelf_variant=last.chassis,
            ).__dict__,
            "source_evidence": {"fields": []},
        },
    )

    class _Variable:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    route_code = _Variable()
    title = _Variable()
    revision = _Variable()
    status: list[str] = []
    selected: list[str] = []
    sentinel = object()
    subject = SimpleNamespace(
        _rows=[sentinel],
        _route_code_var=route_code,
        _title_var=title,
        _revision_var=revision,
        _clear_editor=lambda: None,
        _committed_project_changed=lambda: None,
        _sync_route_fiber_controls=lambda: None,
        _refresh_tree=lambda *, select_id: selected.append(select_id),
        _refresh_status=lambda message="": status.append(message),
    )
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **_kwargs: warnings.append(args),
    )

    module.RlsRouteFrame._apply_diagram_import(subject, result)

    assert subject._rows == [sentinel]
    assert route_code.value is None
    assert title.value is None
    assert revision.value is None
    assert selected == []
    assert status == [
        "Diagram contains unsupported release or shelf roles; "
        "current route unchanged."
    ]
    assert warnings
    assert "r4.0 route required" in str(warnings[0][0]).casefold()


def test_successful_diagram_import_logs_aggregated_transcription_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="USELP1",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="USSAT4",
    )
    span = _diagram_span(1, first.tid, last.tid)
    issues = (
        SimpleNamespace(
            severity="error",
            code="MISSING_REQUIRED_FIELD",
            field="shelves[1].primary_oam_ip",
            message="Customer address value was absent.",
            blocking=True,
        ),
        SimpleNamespace(
            severity="error",
            code="MISSING_REQUIRED_FIELD",
            field="shelves[2].primary_oam_ip",
            message="Another customer address value was absent.",
            blocking=True,
        ),
        SimpleNamespace(
            severity="error",
            code="MISSING_REQUIRED_FIELD",
            field="shelves[1].chassis",
            message="Customer chassis value was absent.",
            blocking=True,
        ),
        SimpleNamespace(
            severity="warning",
            code="PLANNED_REMOVE_EXCLUDED",
            field="shelves[3].lifecycle",
            message="Customer removal note.",
            blocking=False,
        ),
    )
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=issues,
    )
    result.source = SimpleNamespace(
        file_name="route.png",
        source_type="png",
        sha256="a" * 64,
        size_bytes=123,
        images=(),
    )
    result.route_evidence = ()
    result.blocking_issues = issues[:3]
    result.gui_rows = lambda: tuple(
        {
            **_row(
                module,
                shelf_id=f"shelf-{index}",
                profile_id=profile_id,
                site_key=site_code.casefold(),
                site_code=site_code,
                site_name=site_name,
                tid=tid,
                primary_oam_ip="",
                shelf_variant="",
            ).__dict__,
            "profile_payload": {},
            "source_evidence": {
                "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
                "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
                "fields": [],
            },
        }
        for index, profile_id, site_code, site_name, tid in (
            (1, "roadm_a", "USELP1", "El Paso", first.tid),
            (2, "roadm_z", "USSAT4", "San Antonio", last.tid),
        )
    )

    class _Variable:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    statuses: list[str] = []
    warnings: list[tuple[object, ...]] = []
    logs: list[str] = []
    selected: list[str] = []
    subject = SimpleNamespace(
        _rows=[],
        _route_code_var=_Variable(),
        _title_var=_Variable(),
        _revision_var=_Variable(),
        _ospf_area_var=_Variable(),
        _clear_editor=lambda: None,
        _committed_project_changed=lambda: None,
        _sync_route_fiber_controls=lambda: None,
        _refresh_tree=lambda *, select_id: selected.append(select_id),
        _refresh_status=lambda message="": statuses.append(message),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **_kwargs: warnings.append(args),
    )
    monkeypatch.setattr(
        module,
        "_log_route_event",
        lambda message, *_args, **_kwargs: logs.append(message),
    )

    module.RlsRouteFrame._apply_diagram_import(subject, result)

    assert len(subject._rows) == 2
    assert selected == ["shelf-1"]
    assert logs
    assert "route_integrity=accepted" in logs[0]
    assert "transcription_required_review=3" in logs[0]
    assert "transcription_source_required_review=3" in logs[0]
    assert "transcription_source_absences=3" in logs[0]
    assert (
        "transcription_review_accounting="
        "unresolved:3,defaulted:0,suggestion_pending:0,scope_inherited:0,"
        "optional:0,"
        "lifecycle_excluded:0"
    ) in logs[0]
    assert (
        "transcription_issue_codes="
        "MISSING_REQUIRED_FIELD:3,PLANNED_REMOVE_EXCLUDED:1"
    ) in logs[0]
    assert "transcription_missing_fields=primary_oam_ip:2,chassis:1" in logs[0]
    assert (
        "transcription_missing_paths="
        "shelves[].primary_oam_ip:2,shelves[].chassis:1"
    ) in logs[0]
    assert (
        "transcription_source_missing_paths="
        "shelves[].primary_oam_ip:2,shelves[].chassis:1"
    ) in logs[0]
    assert "deployment_readiness=not_authorized_pending_review" in logs[0]
    assert "Customer address" not in logs[0]
    assert "topology accepted" in statuses[-1]
    assert "deployment readiness not authorized" in statuses[-1]
    assert warnings
    dialog_text = str(warnings[0][1])
    assert "Route topology integrity passed" in dialog_text
    assert "After workflow accounting, 3 required field value(s) remain" in (
        dialog_text
    )
    assert "source transcription reported 3 absent field value(s)" in (
        dialog_text
    )
    assert "Configuration deployment readiness is a separate assessment" in (
        dialog_text
    )


def test_site_omissions_import_with_tid_suggestions_and_pending_site_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    first = _diagram_shelf(
        1,
        "USELP1-L8R2",
        profile_family="roadm",
        profile_id="roadm_a",
        site_code="",
        site_name="",
    )
    last = _diagram_shelf(
        2,
        "USSAT4-L8R3",
        profile_family="roadm",
        profile_id="roadm_z",
        site_code="",
        site_name="",
    )
    span = _diagram_span(1, first.tid, last.tid)
    issues = tuple(
        SimpleNamespace(
            severity="error",
            code="MISSING_REQUIRED_FIELD",
            field=f"shelves[{order}].{field_name}",
            message="Required value was absent from the customer diagram.",
            blocking=True,
        )
        for order in (1, 2)
        for field_name in ("site_code", "site_name")
    )
    result = _diagram_gate_result(
        shelves=(first, last),
        active_shelves=(first, last),
        spans=(span,),
        active_spans=(span,),
        issues=issues,
    )
    result.source = SimpleNamespace(
        file_name="route.png",
        source_type="png",
        sha256="b" * 64,
        size_bytes=123,
        images=(),
    )
    result.route_evidence = ()
    result.blocking_issues = issues
    result.gui_rows = lambda: tuple(
        {
            **_row(
                module,
                shelf_id=f"shelf-{index}",
                profile_id=profile_id,
                site_key=f"site-{index}",
                site_code="",
                site_name="",
                tid=tid,
                raman_label="",
            ).__dict__,
            "profile_payload": {},
            "source_evidence": {
                "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
                "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
                "fields": [],
            },
        }
        for index, profile_id, tid in (
            (1, "roadm_a", first.tid),
            (2, "roadm_z", last.tid),
        )
    )

    class _Variable:
        def __init__(self):
            self.value = None

        def set(self, value):
            self.value = value

    statuses: list[str] = []
    warnings: list[tuple[object, ...]] = []
    logs: list[str] = []
    selected: list[str] = []
    subject = SimpleNamespace(
        _rows=[],
        _route_code_var=_Variable(),
        _title_var=_Variable(),
        _revision_var=_Variable(),
        _ospf_area_var=_Variable(),
        _clear_editor=lambda: None,
        _committed_project_changed=lambda: None,
        _sync_route_fiber_controls=lambda: None,
        _refresh_tree=lambda *, select_id: selected.append(select_id),
        _refresh_status=lambda message="": statuses.append(message),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **_kwargs: warnings.append(args),
    )
    monkeypatch.setattr(
        module,
        "_log_route_event",
        lambda message, *_args, **_kwargs: logs.append(message),
    )

    module.RlsRouteFrame._apply_diagram_import(subject, result)

    assert [row.site_code for row in subject._rows] == ["USELP1", "USSAT4"]
    assert [row.site_name for row in subject._rows] == ["", ""]
    assert all(row.review_state == "pending" for row in subject._rows)
    assert all(
        "site_code_review_suggestion" in row.source_evidence
        for row in subject._rows
    )
    assert selected == ["shelf-1"]
    assert "route_integrity=accepted" in logs[0]
    assert "transcription_required_review=2" in logs[0]
    assert "transcription_source_required_review=4" in logs[0]
    assert "transcription_source_absences=4" in logs[0]
    assert (
        "transcription_review_accounting="
        "unresolved:2,defaulted:0,suggestion_pending:2,scope_inherited:0,"
        "optional:0,lifecycle_excluded:0"
    ) in logs[0]
    assert "transcription_missing_paths=shelves[].site_name:2" in logs[0]
    assert (
        "transcription_source_missing_paths="
        "shelves[].site_code:2,shelves[].site_name:2"
    ) in logs[0]
    assert "site_code_review_suggestions=2" in logs[0]
    assert "deployment_readiness=not_authorized_pending_review" in logs[0]
    assert "2 unresolved required value(s)" in statuses[-1]
    assert warnings
    assert "After workflow accounting, 2 required field value(s) remain" in (
        str(warnings[0][1])
    )


def test_diagram_mutation_gate_rejects_missing_header_and_bad_orders() -> None:
    module = _route_module()
    first = _diagram_shelf(2, "A")
    second = _diagram_shelf(1, "Z")
    span = _diagram_span(2, first.tid, second.tid)
    result = _diagram_gate_result(
        shelves=(first, second),
        active_shelves=(first, second),
        spans=(span,),
        active_spans=(span,),
        route_code=" ",
        title="",
        ospf_area="",
    )

    codes = {
        blocker.code
        for blocker in module.diagram_import_mutation_blockers(result)
    }

    assert "MISSING_ROUTE_IDENTITY" in codes
    assert "NONCONTIGUOUS_SHELF_ORDER" in codes
    assert "NONCONTIGUOUS_ACTIVE_SHELF_ORDER" in codes
    assert "NONCONTIGUOUS_SPAN_ORDER" in codes


def test_profile_choices_expose_only_r40_operator_roles() -> None:
    module = _route_module()
    choices = module.profile_choices()
    profile_ids = [profile_id for profile_id, _label in choices]

    assert tuple(profile_ids) == module.R40_UI_PROFILE_IDS
    assert len(profile_ids) == len(set(profile_ids))
    assert set(profile_ids) == {
        "add_drop_a",
        "add_drop_z",
        "add_drop",
        "ila",
        "roadm_a",
        "roadm_z",
        "roadm",
    }
    assert "protected_dci" not in profile_ids
    assert all(label.strip() for _profile_id, label in choices)


def test_ordered_terminal_sites_recompute_a_z_role_suffixes() -> None:
    module = _route_module()
    rows = (
        _row(
            module,
            shelf_id="a",
            profile_id="roadm_z",
            site_key="site-a",
            site_code="ELP1",
            tid="USELP1-L8R2",
            profile_payload={
                "schema_id": "ciena.rls.r4-0-exact-request",
                "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
            },
        ),
        _row(
            module,
            shelf_id="mid",
            profile_id="ila",
            site_key="site-mid",
            site_code="QTN1",
            tid="USQTN1-L8I2",
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="add_drop_a",
            site_key="site-z",
            site_code="SAT4",
            tid="USSAT4-L8R3",
        ),
    )

    reconciled, changed = module._reconcile_route_endpoint_profiles(rows)

    assert [row.profile_id for row in reconciled] == [
        "roadm_a",
        "ila",
        "add_drop_z",
    ]
    assert changed == 2
    assert reconciled[0].profile_payload == {}
    assert reconciled[0].source_evidence[
        "route_endpoint_derivation"
    ]["endpoint_side"] == "A"
    assert reconciled[2].source_evidence[
        "route_endpoint_derivation"
    ]["endpoint_side"] == "Z"
    assert reconciled[0].source_evidence[
        "route_endpoint_derivation"
    ]["deployable_cli"] is False


def test_endpoint_role_derivation_rebinds_only_exact_power_default() -> None:
    module = _route_module()
    exact_default = module._power_label_role_default_marker("roadm")
    rows = (
        _row(
            module,
            shelf_id="a",
            profile_id="roadm",
            site_key="site-a",
            site_code="ELP1",
            tid="USELP1-L8R2",
            power_label="AC",
            source_evidence={
                "power_label_role_default": exact_default,
            },
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="roadm",
            site_key="site-z",
            site_code="SAT4",
            tid="USSAT4-L8R3",
            power_label="Customer AC Feed A/B",
            source_evidence={},
        ),
    )

    reconciled, changed = module._reconcile_route_endpoint_profiles(rows)

    assert changed == 2
    assert reconciled[0].profile_id == "roadm_a"
    assert module._has_exact_power_label_role_default(
        reconciled[0].source_evidence,
        "roadm_a",
        "AC",
    )
    assert reconciled[1].profile_id == "roadm_z"
    assert reconciled[1].power_label == "Customer AC Feed A/B"
    assert (
        "power_label_role_default"
        not in reconciled[1].source_evidence
    )


def test_multiple_shelves_at_terminal_site_share_derived_side() -> None:
    module = _route_module()
    rows = (
        _row(
            module,
            shelf_id="a-1",
            profile_id="roadm",
            site_key="site-a",
            site_code="ELP1",
            tid="USELP1-L8R2",
        ),
        _row(
            module,
            shelf_id="a-2",
            profile_id="add_drop_z",
            site_key="site-a",
            site_code="ELP1",
            tid="USELP1-L8R4",
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="roadm_a",
            site_key="site-z",
            site_code="SAT4",
            tid="USSAT4-L8R3",
        ),
    )

    reconciled, changed = module._reconcile_route_endpoint_profiles(rows)

    assert [row.profile_id for row in reconciled] == [
        "roadm_a",
        "add_drop_a",
        "roadm_z",
    ]
    assert changed == 3


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    (
        ("ila", "DC"),
        ("add_drop", "AC"),
        ("add_drop_a", "AC"),
        ("add_drop_z", "AC"),
        ("roadm", "AC"),
        ("roadm_a", "AC"),
        ("roadm_z", "AC"),
        ("", ""),
        ("unknown", ""),
    ),
)
def test_power_label_for_profile_uses_route_role_standard(
    profile_id: str,
    expected: str,
) -> None:
    module = _route_module()

    assert module.power_label_for_profile(profile_id) == expected


def test_editor_row_fails_closed_for_non_r40_release_or_profile() -> None:
    module = _route_module()

    def variable(value: str) -> SimpleNamespace:
        return SimpleNamespace(get=lambda: value)

    subject = SimpleNamespace(
        _selected_profile_id=lambda: "ila",
        _site_code_var=variable("CHI"),
        _site_name_var=variable("Chicago"),
        _tid_var=variable("CHI-ILA-01"),
        _ip_var=variable("192.0.2.10"),
        _release_var=variable(module.R40_UI_RELEASE),
        _variant_var=variable("K74-C894-900"),
        _raman_var=variable(""),
        _power_var=variable("A/B"),
    )

    row = module.RlsRouteFrame._read_editor_row(subject)
    assert row.profile_id == "ila"
    assert row.software_release == module.R40_UI_RELEASE

    for unsupported_release in ("R4.0", "RLS 4.0", "RLS R4.2"):
        subject._release_var = variable(unsupported_release)
        with pytest.raises(ValueError, match="fixed to exact 'RLS R4.0'"):
            module.RlsRouteFrame._read_editor_row(subject)

    subject._release_var = variable(module.R40_UI_RELEASE)
    subject._selected_profile_id = lambda: "protected_dci"
    with pytest.raises(ValueError, match="RLS R4.0 Add/Drop, ILA, or ROADM"):
        module.RlsRouteFrame._read_editor_row(subject)


def test_editor_row_persists_default_provenance_and_removes_it_on_override():
    module = _route_module()

    def variable(value: str) -> SimpleNamespace:
        return SimpleNamespace(get=lambda: value)

    subject = SimpleNamespace(
        _selected_profile_id=lambda: "ila",
        _site_code_var=variable("CHI"),
        _site_name_var=variable("Chicago"),
        _tid_var=variable("CHI-ILA-01"),
        _ip_var=variable("192.0.2.10"),
        _release_var=variable(module.R40_UI_RELEASE),
        _variant_var=variable("R2 600mm"),
        _raman_var=variable(""),
        _power_var=variable("DC"),
        _editor_power_is_atlas_default=True,
    )

    defaulted = module.RlsRouteFrame._read_editor_row(subject)
    assert module._has_exact_power_label_role_default(
        defaulted.source_evidence,
        defaulted.profile_id,
        defaulted.power_label,
    )

    subject._power_var = variable("Operator DC A/B")
    subject._editor_power_is_atlas_default = False
    overridden = module.RlsRouteFrame._read_editor_row(
        subject,
        source_evidence=defaulted.source_evidence,
    )
    assert overridden.power_label == "Operator DC A/B"
    assert "power_label_role_default" not in overridden.source_evidence


def test_build_route_project_preserves_shelf_order_and_deduplicates_sites() -> None:
    module = _route_module()
    first = _row(module)
    second = _row(
        module,
        shelf_id="shelf-2",
        profile_id="roadm_z",
        site_key="a-different-input-key",
        site_name="CHICAGO",
        tid="CHI-ROADM-02",
        primary_oam_ip="192.0.2.11",
        software_release="RLS R4.0",
        shelf_variant="K74-C890-900",
        raman_label="",
    )

    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1 to SAT4 FBN",
        revision="2",
        rows=(first, second),
        project_id="route-1",
        notes="Preserve this route note.",
    )

    assert project.project_id == "route-1"
    assert project.route_code == "ELP1-SAT4"
    assert project.notes == "Preserve this route note."
    assert len(project.sites) == 1
    assert [shelf.shelf_id for shelf in project.shelves] == ["shelf-1", "shelf-2"]
    assert project.shelves[0].site_key == project.shelves[1].site_key
    assert project.shelves[0].raman_label == "Slot 4"
    assert project.shelves[1].profile_id == "roadm_z"
    assert all(shelf.review_state == "manual" for shelf in project.shelves)
    assert all(shelf.source_evidence == {} for shelf in project.shelves)
    assert project.diagram_source == {}


def test_build_route_project_preserves_review_and_diagram_provenance() -> None:
    module = _route_module()
    evidence = {
        "schema_id": "atlas.ciena.rls.diagram-import-evidence",
        "fields": [{"field": "tid", "confidence": 0.98}],
    }
    row = _row(
        module,
        review_state="corrected",
        source_evidence=evidence,
    )
    diagram_source = {
        "file_name": "customer-route.docx",
        "source_sha256": "abc123",
    }

    project = module.build_route_project(
        route_code="CHI",
        title="Imported Chicago route",
        revision="1",
        rows=(row,),
        diagram_source=diagram_source,
    )

    assert project.shelves[0].review_state == "corrected"
    serialized = project.to_dict()
    assert serialized["shelves"][0]["source_evidence"] == evidence
    assert serialized["diagram_source"] == diagram_source


def test_build_route_project_preserves_ospf_and_first_class_links() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        primary_oam_ip="10.6.22.129",
        software_release="RLS R4.0",
        shelf_variant="R4",
    )
    second = _row(
        module,
        shelf_id="roadm-z",
        profile_id="roadm_z",
        site_key="site-sat4",
        site_code="SAT4",
        site_name="San Antonio",
        tid="USSAT4-L8R3",
        primary_oam_ip="10.6.22.158",
        software_release="RLS R4.0",
        shelf_variant="R4",
    )
    links = (
        module.RouteLink(
            link_id="link-1",
            order=1,
            from_shelf_id=first.shelf_id,
            to_shelf_id=second.shelf_id,
            paths=(
                module.OpticalPath(
                    path_id="path-1",
                    path_role="route",
                    link_name="ELP1-SAT4-L1",
                    expected_loss_db=14.5,
                    distance_km=62.47,
                    fiber_type="NDSF",
                ),
            ),
        ),
    )

    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="RLS R4.0 ROADM route",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(first, second),
        links=links,
    )

    assert project.ospf_area == "10.6.8.0"
    assert project.links == links
    assert project.to_dict()["links"][0]["paths"][0]["distance_km"] == 62.47


def test_load_project_rejects_r42_before_mutating_route_rows() -> None:
    module = _route_module()
    sentinel = object()
    subject = SimpleNamespace(_rows=[sentinel])
    project = SimpleNamespace(
        shelves=(
            SimpleNamespace(
                profile_id="protected_dci",
                software_release="RLS R4.2",
            ),
        )
    )

    with pytest.raises(ValueError, match="RLS R4.0"):
        module.RlsRouteFrame._load_project(subject, project)

    assert subject._rows == [sentinel]


def test_r40_endpoint_reviews_preserve_asymmetric_losses_and_side_mapping() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="r40-a",
        profile_id="add_drop_a",
        site_code="A",
        site_name="A Site",
        tid="R40-A",
        software_release="RLS R4.0",
    )
    second = _row(
        module,
        shelf_id="r40-z",
        profile_id="add_drop_z",
        site_key="site-z",
        site_code="Z",
        site_name="Z Site",
        tid="R40-Z",
        primary_oam_ip="192.0.2.11",
        software_release="RLS R4.0",
    )
    original = module.RouteLink(
        link_id="link-a-z",
        order=1,
        from_shelf_id=first.shelf_id,
        to_shelf_id=second.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-z",
                path_role="route",
                link_name="CUSTOMER-SPAN",
                expected_loss_db=12.5,
                distance_km=50.0,
                fiber_type="NDSF",
                review_state="confirmed",
            ),
        ),
    )
    first_request = SimpleNamespace(
        line_1_route_side="A",
        line_1=SimpleNamespace(
            link_name="UNMODELED-A-SIDE",
            expected_loss_db=20.0,
            fiber_type="NDSF",
        ),
        line_2=SimpleNamespace(
            link_name="A-LOCAL-LINK",
            expected_loss_db=12.25,
            fiber_type="NDSF",
        ),
    )
    after_first = module._apply_r40_reviewed_lines_to_links(
        (first, second),
        (original,),
        first.shelf_id,
        first_request,
    )

    assert after_first[0].paths[0].review_state == "pending"
    assert after_first[0].paths[0].endpoint_reviews == (
        module.PathEndpointReview(
            shelf_id=first.shelf_id,
            link_name="A-LOCAL-LINK",
            expected_loss_db=12.25,
            fiber_type="NDSF",
        ),
    )

    second_request = SimpleNamespace(
        line_1_route_side="A",
        line_1=SimpleNamespace(
            link_name="Z-LOCAL-LINK",
            expected_loss_db=13.75,
            fiber_type="NDSF",
        ),
        line_2=SimpleNamespace(
            link_name="UNMODELED-Z-SIDE",
            expected_loss_db=21.0,
            fiber_type="NDSF",
        ),
    )
    completed = module._apply_r40_reviewed_lines_to_links(
        (first, second),
        after_first,
        second.shelf_id,
        second_request,
    )
    path = completed[0].paths[0]

    assert path.review_state == "corrected"
    assert path.link_name == "CUSTOMER-SPAN"
    assert path.expected_loss_db == 12.5
    assert {
        (review.shelf_id, review.link_name, review.expected_loss_db)
        for review in path.endpoint_reviews
    } == {
        ("r40-a", "A-LOCAL-LINK", 12.25),
        ("r40-z", "Z-LOCAL-LINK", 13.75),
    }


def test_one_degree_terminal_applies_only_its_modeled_bidirectional_degree() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
    )
    neighbor = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    link = module.RouteLink(
        link_id="link-a-z",
        order=1,
        from_shelf_id=terminal.shelf_id,
        to_shelf_id=neighbor.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-z",
                path_role="route",
                link_name="BDJW7353",
                expected_loss_db=14.55,
                fiber_type="LEAF",
            ),
        ),
    )
    request = SimpleNamespace(
        line_1_route_side="Z",
        line_1=SimpleNamespace(
            link_name="LM1-LINEOUT",
            expected_loss_db=14.55,
            fiber_type="LEAF",
        ),
        line_2=None,
    )

    updated = module._apply_r40_reviewed_lines_to_links(
        (terminal, neighbor),
        (link,),
        terminal.shelf_id,
        request,
    )

    assert updated[0].paths[0].endpoint_reviews == (
        module.PathEndpointReview(
            shelf_id=terminal.shelf_id,
            link_name="LM1-LINEOUT",
            expected_loss_db=14.55,
            fiber_type="LEAF",
        ),
    )
    assert updated[0].paths[0].link_name == "BDJW7353"

    with pytest.raises(ValueError, match="no reviewed physical degree"):
        module._apply_r40_reviewed_lines_to_links(
            (terminal, neighbor),
            (link,),
            terminal.shelf_id,
            SimpleNamespace(
                line_1_route_side="A",
                line_1=request.line_1,
                line_2=None,
            ),
        )


def test_r40_endpoint_review_reapply_replaces_case_insensitive_shelf_id() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="r40-a",
        profile_id="add_drop_a",
        site_code="A",
        site_name="A Site",
        tid="R40-A",
        software_release="RLS R4.0",
    )
    second = _row(
        module,
        shelf_id="r40-z",
        profile_id="add_drop_z",
        site_key="site-z",
        site_code="Z",
        site_name="Z Site",
        tid="R40-Z",
        primary_oam_ip="192.0.2.11",
        software_release="RLS R4.0",
    )
    link = module.RouteLink(
        link_id="link-a-z",
        order=1,
        from_shelf_id=first.shelf_id,
        to_shelf_id=second.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-z",
                path_role="route",
                link_name="CUSTOMER-SPAN",
                expected_loss_db=12.5,
                distance_km=50.0,
                fiber_type="NDSF",
                review_state="confirmed",
                endpoint_reviews=(
                    module.PathEndpointReview(
                        shelf_id="R40-A",
                        link_name="A-LOCAL-LINK",
                        expected_loss_db=12.25,
                        fiber_type="NDSF",
                    ),
                    module.PathEndpointReview(
                        shelf_id="R40-Z",
                        link_name="Z-LOCAL-LINK",
                        expected_loss_db=13.75,
                        fiber_type="NDSF",
                    ),
                ),
            ),
        ),
    )
    request = SimpleNamespace(
        line_1_route_side="Z",
        line_1=SimpleNamespace(
            link_name="A-LOCAL-LINK",
            expected_loss_db=12.25,
            fiber_type="NDSF",
        ),
        line_2=SimpleNamespace(
            link_name="UNMODELED-A-SIDE",
            expected_loss_db=20.0,
            fiber_type="NDSF",
        ),
    )

    updated = module._apply_r40_reviewed_lines_to_links(
        (first, second),
        (link,),
        first.shelf_id,
        request,
    )
    endpoint_reviews = updated[0].paths[0].endpoint_reviews

    assert len(endpoint_reviews) == 2
    assert len({review.shelf_id.casefold() for review in endpoint_reviews}) == 2
    assert any(review.shelf_id == "r40-a" for review in endpoint_reviews)
    assert updated[0].paths[0].review_state == "corrected"


def test_r40_editor_seed_preserves_route_sides_and_missing_external_degree() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        software_release="RLS R4.0",
        source_evidence={
            "chassis": "R4 600mm",
            "band": "c+l",
            "topology": "cdc",
            "module_inventory": [
                {
                    "pec": "NTK852AA",
                    "role": "RLA32",
                    "slot": 1,
                    "subslot": None,
                }
            ],
        },
    )
    second = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        primary_oam_ip="192.0.2.11",
        software_release="RLS R4.0",
    )
    link = module.RouteLink(
        link_id="link-a-z",
        order=1,
        from_shelf_id=first.shelf_id,
        to_shelf_id=second.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-z",
                path_role="route",
                link_name="CUSTOMER-SPAN",
                expected_loss_db=14.55,
                distance_km=62.47,
                fiber_type="NDSF",
                circuit_id="BDJW7353",
                fiber_start=14,
                fiber_end=15,
                source_evidence={
                    "fields": [
                        {
                            "field": "fiber_type",
                            "normalized_value": "LEAF",
                            "confidence": 0.99,
                            "method": "vision",
                        }
                    ]
                },
            ),
        ),
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="Endpoint seed audit",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(first, second),
        links=(link,),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            }
        },
    )

    first_seed = module._r4_0_editor_seed(project, first.shelf_id)
    second_seed = module._r4_0_editor_seed(project, second.shelf_id)

    assert set(first_seed["lines_by_side"]) == {"A", "Z"}
    assert first_seed["lines_by_side"]["A"][
        "represented_by_route_span"
    ] is False
    assert first_seed["lines_by_side"]["A"]["expected_loss_db"] is None
    assert first_seed["lines_by_side"]["Z"][
        "represented_by_route_span"
    ] is True
    assert first_seed["lines_by_side"]["Z"]["expected_loss_db"] == 14.55
    assert first_seed["lines_by_side"]["Z"]["distance_km"] == 62.47
    assert first_seed["lines_by_side"]["Z"]["circuit_id"] == "BDJW7353"
    assert first_seed["lines_by_side"]["Z"]["fiber_start"] == 14
    assert first_seed["lines_by_side"]["Z"]["fiber_end"] == 15
    assert first_seed["lines_by_side"]["Z"]["source_fiber_label"] == "LEAF"
    assert second_seed["lines_by_side"]["A"]["expected_loss_db"] == 14.55
    assert second_seed["lines_by_side"]["Z"]["expected_loss_db"] is None
    assert first_seed["diagram_optical_band"] == "c+l"
    assert (
        first_seed["target_software_build"]
        == module.DEFAULT_R40_TARGET_BUILD_SCHEMA
        == "4.00.00"
    )
    assert first_seed["frame_identification_code"] == ""
    assert first_seed["reviewed_hardware"]["chassis"] == "R4 600mm"
    assert first_seed["reviewed_hardware"]["shelf_band"] == "c+l"
    assert first_seed["reviewed_hardware"]["module_inventory"][0]["pec"] == (
        "NTK852AA"
    )
    assert "diagram_optical_band" in first_seed["prepopulation"][
        "route_reviewed_fields"
    ]
    assert "onsite_terminal_colan" not in first_seed["prepopulation"][
        "manual_fields"
    ]
    assert (
        "terminal_colan_optional_for_factory_staging"
        in first_seed["prepopulation"]["policy_exclusions"]
    )
    assert "rack_location" not in first_seed["prepopulation"]["manual_fields"]
    assert (
        "target_build_schema_4.00.00_vendor_baseline_unverified"
        in first_seed["prepopulation"]["controlled_defaults"]
    )
    assert (
        "optional_frame_location_omits_shelf_location_cli_when_blank"
        in first_seed["prepopulation"]["policy_exclusions"]
    )
    assert "customer_managed_ntp_omitted" in first_seed["prepopulation"][
        "policy_exclusions"
    ]
    assert "ntp_servers" not in first_seed["prepopulation"]["manual_fields"]
    assert "onsite_terminal_colan" not in second_seed["prepopulation"][
        "manual_fields"
    ]
    assert "ila_colan_prohibited" in second_seed["prepopulation"][
        "policy_exclusions"
    ]


def test_r40_editor_seed_prepopulates_terminal_dle_peer_pfgs_fail_closed() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        raman_label="",
        source_evidence=_no_sra_source_evidence(line_endpoints=[]),
    )
    ila = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        raman_label="",
        source_evidence=_no_sra_source_evidence(line_endpoints=[]),
    )
    link = module.RouteLink(
        link_id="link-a-z",
        order=1,
        from_shelf_id=terminal.shelf_id,
        to_shelf_id=ila.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-z",
                path_role="route",
                link_name="BDJW7353",
                expected_loss_db=14.55,
                fiber_type="LEAF",
            ),
        ),
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(terminal, ila),
        links=(link,),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    terminal_seed = module._r4_0_editor_seed(project, terminal.shelf_id)
    ila_seed = module._r4_0_editor_seed(project, ila.shelf_id)

    # The terminal sees the DLE peer's A-facing fixed path (DLE record 2).
    assert terminal_seed["lines_by_side"]["Z"][
        "neighbor_line_mux_pfg"
    ] == "PFG-2-to-1"
    assert terminal_seed["lines_by_side"]["Z"][
        "neighbor_line_demux_pfg"
    ] == "PFG-1-to-2"
    assert terminal_seed["lines_by_side"]["Z"][
        "neighbor_pfg_source"
    ] == module._PEER_PFG_SOURCE_ROLE_FALLBACK

    # The DLE sees the one-degree terminal's audited Z-facing LM1/LD1
    # provider/role mapping.
    assert ila_seed["lines_by_side"]["A"][
        "neighbor_line_mux_pfg"
    ] == "LM1"
    assert ila_seed["lines_by_side"]["A"][
        "neighbor_line_demux_pfg"
    ] == "LD1"
    assert ila_seed["lines_by_side"]["A"][
        "neighbor_pfg_source"
    ] == module._PEER_PFG_SOURCE_ROLE_FALLBACK
    assert "prepopulated_remote_pfg_review" in terminal_seed[
        "prepopulation"
    ]["manual_fields"]
    assert "prepopulated_remote_pfg_review" in ila_seed[
        "prepopulation"
    ]["manual_fields"]
    assert terminal_seed["prepopulation"]["peer_pfg_suggestions"] == (
        "Z:audited_peer_role_fallback",
    )
    assert ila_seed["prepopulation"]["peer_pfg_suggestions"] == (
        "A:audited_peer_role_fallback",
    )


def test_r40_peer_pfg_prepopulation_leaves_ambiguous_peer_blank() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={"line_endpoints": []},
    )
    ila = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        source_evidence={"line_endpoints": []},
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        rows=(terminal, ila),
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=ila.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        # Without a direct route/shelf band, the terminal has multiple
        # role-compatible exact providers. Role alone cannot choose its PFGs.
        diagram_source={},
        require_valid=False,
    )

    ila_seed = module._r4_0_editor_seed(project, ila.shelf_id)
    a_side = ila_seed["lines_by_side"]["A"]

    assert a_side["neighbor_line_mux_pfg"] == ""
    assert a_side["neighbor_line_demux_pfg"] == ""
    assert a_side["neighbor_pfg_source"] == ""
    assert "remote_pfg_identities" in ila_seed["prepopulation"][
        "manual_fields"
    ]
    assert ila_seed["prepopulation"]["peer_pfg_suggestions"] == ()


def test_r40_peer_pfg_prepopulation_uses_current_exact_peer_payload() -> None:
    module = _route_module()
    from utils.rls_config.r4_0_generator import (
        R40ExactRequest,
        R40LinePath,
        R40_PROVIDER_CATALOG,
        encode_r40_exact_payload,
    )

    peer_profile = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "ila_dle_cl"
    )
    peer_request = R40ExactRequest(
        provider_id=peer_profile.provider_id,
        profile="ila",
        software_release="RLS R4.0",
        target_software_build="R4.0-test",
        chassis_family=peer_profile.chassis_family,
        chassis_pec=peer_profile.chassis_pec,
        hardware_profile=peer_profile.hardware_profile,
        shelf_name="USQTN1-L8I2",
        site_name="Tornillo",
        member_name="USQTN1-L8I2",
        hostname="USQTN1-L8I2",
        frame_identification_code="RACK-1",
        loopback_ip="192.0.2.11",
        ospf_area="10.6.8.0",
        line_1=R40LinePath(
            link_name="PFG-1-2-LINEOUT",
            neighbor_node="USSAT4-L8R3",
            neighbor_line_mux_pfg="LM1",
            neighbor_line_demux_pfg="LD1",
            fiber_type="LEAF",
            expected_loss_db=14.55,
        ),
        line_2=R40LinePath(
            link_name="PFG-2-1-LINEOUT",
            neighbor_node="USELP1-L8R2",
            neighbor_line_mux_pfg="LM1",
            neighbor_line_demux_pfg="LD1",
            fiber_type="LEAF",
            expected_loss_db=14.55,
        ),
        line_1_route_side="Z",
    )
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
    )
    ila = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        profile_payload=encode_r40_exact_payload(peer_request),
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        rows=(terminal, ila),
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=ila.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    z_side = module._r4_0_editor_seed(
        project,
        terminal.shelf_id,
    )["lines_by_side"]["Z"]

    assert z_side["neighbor_line_mux_pfg"] == "PFG-2-to-1"
    assert z_side["neighbor_line_demux_pfg"] == "PFG-1-to-2"
    assert z_side["neighbor_pfg_source"] == (
        module._PEER_PFG_SOURCE_EXACT_PAYLOAD
    )


def test_r40_peer_pfg_prepopulation_uses_direct_peer_direction() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={
            "line_endpoints": [
                {
                    "adjacency": "following",
                    "slot": 1,
                    "line_out_port": 53,
                    "evidence": [
                        {
                            "field": "line_endpoints.0.adjacency",
                            "normalized_value": "following",
                            "confidence": 0.99,
                            "method": "inferred",
                        },
                        {
                            "field": "line_endpoints.0.slot",
                            "normalized_value": 1,
                            "confidence": 0.99,
                            "method": "vision",
                        },
                        {
                            "field": "line_endpoints.0.line_out_port",
                            "normalized_value": 53,
                            "confidence": 0.99,
                            "method": "vision",
                        },
                    ],
                },
            ],
        },
    )
    ila = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        rows=(terminal, ila),
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=ila.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    a_side = module._r4_0_editor_seed(
        project,
        ila.shelf_id,
    )["lines_by_side"]["A"]

    assert a_side["neighbor_line_mux_pfg"] == "LM1"
    assert a_side["neighbor_line_demux_pfg"] == "LD1"
    assert a_side["neighbor_pfg_source"] == (
        module._PEER_PFG_SOURCE_DIRECT_DIRECTION
    )


@pytest.mark.parametrize(
    "raman_review_status",
    ("accepted", "pending", "invalid"),
)
def test_r40_peer_pfg_prepopulation_blocks_unresolved_structured_raman(
    raman_review_status: str,
) -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={
            "line_endpoints": [],
            "raman_callouts": [{"slot": 3, "port": 6}],
            "raman_callout_review": raman_review_status,
        },
    )
    ila = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        rows=(terminal, ila),
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=ila.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    a_side = module._r4_0_editor_seed(
        project,
        ila.shelf_id,
    )["lines_by_side"]["A"]

    assert a_side["neighbor_line_mux_pfg"] == ""
    assert a_side["neighbor_line_demux_pfg"] == ""
    assert a_side["neighbor_pfg_source"] == ""


def test_diagram_hardware_preselects_ila_provider_and_fixed_direction_only() -> None:
    module = _route_module()

    def endpoint(index: int, adjacency: str, line_out_port: int):
        return {
            "adjacency": adjacency,
            "slot": 1,
            "line_out_port": line_out_port,
            "evidence": [
                {
                    "field": f"line_endpoints.{index}.adjacency",
                    "normalized_value": adjacency,
                    "confidence": 0.99,
                    "method": "inferred",
                },
                {
                    "field": f"line_endpoints.{index}.slot",
                    "normalized_value": 1,
                    "confidence": 0.99,
                    "method": "vision",
                },
                {
                    "field": f"line_endpoints.{index}.line_out_port",
                    "normalized_value": line_out_port,
                    "confidence": 0.99,
                    "method": "vision",
                },
            ],
        }

    first = _row(
        module,
        shelf_id="a",
        profile_id="roadm_a",
        site_key="site-a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        raman_label="",
        source_evidence=_no_sra_source_evidence(),
    )
    ila = _row(
        module,
        shelf_id="ila",
        profile_id="ila",
        site_key="site-mid",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        primary_oam_ip="192.0.2.11",
        source_evidence={
            "chassis": "R2 600mm",
            "fields": [
                {
                    "field": "chassis",
                    "normalized_value": "R2 600mm",
                    "confidence": 0.99,
                    "method": "vision",
                },
            ],
            "line_endpoints": [
                endpoint(0, "preceding", 53),
                endpoint(1, "following", 63),
            ],
        },
    )
    last = _row(
        module,
        shelf_id="z",
        profile_id="roadm_z",
        site_key="site-z",
        site_code="SAT4",
        site_name="San Antonio",
        tid="USSAT4-L8R3",
        primary_oam_ip="192.0.2.12",
        source_evidence={},
    )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(first, ila, last),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, ila.shelf_id)
    terminal_seed = module._r4_0_editor_seed(project, first.shelf_id)

    assert seed["provider_resolution"]["status"] == "unique_candidate"
    assert seed["provider_resolution"]["preselect_allowed"] is True
    assert seed["provider_resolution"]["deployable_cli"] is False
    assert seed["direction_resolution"]["status"] == "exact_match"
    assert seed["direction_resolution"]["deployable_cli"] is False
    assert seed["line_1_route_side"] == "Z"
    assert (
        "fixed_direction_from_direct_endpoint"
        in seed["prepopulation"]["controlled_derivations"]
    )
    assert seed["lines_by_side"]["A"]["neighbor_node"] == "USELP1-L8R2"
    assert seed["lines_by_side"]["Z"]["neighbor_node"] == "USSAT4-L8R3"
    assert seed["lines_by_side"]["A"]["represented_by_route_span"] is False
    assert seed["lines_by_side"]["Z"]["represented_by_route_span"] is False
    assert terminal_seed["provider_resolution"]["status"] == "unique_candidate"
    assert terminal_seed["provider_resolution"]["preselect_allowed"] is False
    assert len(
        terminal_seed["provider_resolution"]["review_provider_ids"]
    ) == 1
    assert "UNIQUE_ROUTE_SCOPE_COMPATIBLE_PROVIDER" in terminal_seed[
        "provider_resolution"
    ]["reason_codes"]
    assert terminal_seed["lines_by_side"]["A"]["neighbor_node"] == ""
    assert terminal_seed["lines_by_side"]["Z"]["neighbor_node"] == (
        "USQTN1-L8I2"
    )


def test_elp_sat_source_convention_selects_no_sra_and_exact_sra_candidates() -> None:
    module = _route_module()
    from utils.rls_config.r4_0_generator import (
        R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
        R40_R2_CL_DLE_S1_NO_SRA,
        R40_R2_CL_DLE_S1_SRA4,
    )

    tids = (
        "USELP1-L8R2",
        "USQTN1-L8I2",
        "USIEB1-L8I2",
        "USVHN1-L8I2",
        "USVTE1-L8I2",
        "USMRF1-L8I2",
        "USALE11-L8I2",
        "USALE21-L8I2",
        "USDSA1-L8I2",
        "USDYN1-L8I2",
        "USDRT2-L8I2",
        "USDRT1-L8I2",
        "USUVA1-L8I2",
        "USKP21-L8I2",
        "USXGN1-L8I2",
        "USSAT4-L8R3",
    )
    rows = []
    for index, tid in enumerate(tids):
        if index == 0:
            profile_id = "roadm_a"
            raman_label = ""
            source_evidence = _no_sra_source_evidence(line_endpoints=[])
        elif index == len(tids) - 2:
            profile_id = "ila"
            raman_label = "Slot 4"
            source_evidence = _sra_source_evidence(
                tid,
                4,
                [
                    _line_endpoint(
                        0,
                        adjacency="following",
                        slot=4,
                        line_out_port=5,
                    ),
                    _line_endpoint(
                        1,
                        adjacency="preceding",
                        slot=1,
                        line_out_port=53,
                    ),
                ],
            )
        elif index == len(tids) - 1:
            profile_id = "roadm_z"
            raman_label = "Slot 6"
            source_evidence = _sra_source_evidence(
                tid,
                6,
                [
                    _line_endpoint(
                        0,
                        adjacency="preceding",
                        slot=6,
                        line_out_port=5,
                    )
                ],
            )
        else:
            profile_id = "ila"
            raman_label = ""
            source_evidence = _no_sra_source_evidence(line_endpoints=[])
        rows.append(
            _row(
                module,
                shelf_id=f"shelf-{index + 1}",
                profile_id=profile_id,
                site_key=f"site-{index + 1}",
                site_code=f"S{index + 1:02d}",
                site_name=f"Site {index + 1}",
                tid=tid,
                primary_oam_ip=f"192.0.2.{index + 10}",
                raman_label=raman_label,
                source_evidence=source_evidence,
            )
        )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        ospf_area="10.6.8.0",
        rows=rows,
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
            "unassigned_raman_callouts": [],
            "unknown_callouts": [],
        },
        require_valid=False,
    )

    seeds = [
        module._r4_0_editor_seed(project, row.shelf_id)
        for row in rows
    ]
    assert seeds[0]["provider_resolution"]["provider_id"] == (
        R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA
    )
    assert all(
        seed["provider_resolution"]["provider_id"]
        == R40_R2_CL_DLE_S1_NO_SRA
        for seed in seeds[1:14]
    )
    assert seeds[14]["provider_resolution"]["provider_id"] == (
        R40_R2_CL_DLE_S1_SRA4
    )
    assert seeds[14]["direction_resolution"]["status"] == "exact_match"
    assert seeds[14]["line_1_route_side"] == "Z"
    assert seeds[14]["lines_by_side"]["A"]["neighbor_node"] == (
        "USKP21-L8I2"
    )
    assert seeds[14]["lines_by_side"]["Z"]["neighbor_node"] == (
        "USSAT4-L8R3"
    )
    assert seeds[14]["direction_resolution"]["matched_endpoints"][0][
        "line_out_port"
    ] == 5
    assert seeds[15]["provider_resolution"]["provider_id"] == (
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6
    )
    assert seeds[15]["direction_resolution"]["status"] == "exact_match"
    assert seeds[15]["line_1_route_side"] == "A"
    assert seeds[15]["lines_by_side"]["A"]["neighbor_node"] == (
        "USXGN1-L8I2"
    )
    assert seeds[15]["lines_by_side"]["Z"]["represented_by_route_span"] is (
        False
    )
    assert seeds[15]["direction_resolution"]["matched_endpoints"][0] == {
        "adjacency": "preceding",
        "route_side": "A",
        "slot": 6,
        "line_out_port": 5,
        "matched_fixed_direction": 1,
        "derived_line_1_route_side": "A",
    }


def test_unassigned_source_sra_callout_prevents_no_sra_absence_inference() -> None:
    module = _route_module()
    row = _row(
        module,
        profile_id="ila",
        raman_label="",
        source_evidence=_no_sra_source_evidence(line_endpoints=[]),
    )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        rows=(row,),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
            "unassigned_raman_callouts": [{"slot": 4, "port": 5}],
            "unknown_callouts": [],
        },
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, row.shelf_id)

    assert seed["provider_resolution"]["status"] == "ambiguous"
    assert seed["provider_resolution"]["provider_id"] == ""
    assert len(seed["provider_resolution"]["review_provider_ids"]) == 2


def test_terminal_fixed_direction_uses_only_modeled_ordered_span_side() -> None:
    module = _route_module()
    endpoint = {
        # The vision provider mislabeled this first-terminal connector as
        # preceding even though the only modeled link is to the next shelf.
        "adjacency": "preceding",
        "slot": 1,
        "line_out_port": 53,
        "evidence": [
            {
                "field": "line_endpoints.0.adjacency",
                "normalized_value": "preceding",
                "confidence": 0.99,
                "method": "inferred",
            },
            {
                "field": "line_endpoints.0.slot",
                "normalized_value": 1,
                "confidence": 0.99,
                "method": "vision",
            },
            {
                "field": "line_endpoints.0.line_out_port",
                "normalized_value": 53,
                "confidence": 0.99,
                "method": "vision",
            },
        ],
    }
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={
            "chassis": "R4 600mm",
            "fields": [
                {
                    "field": "chassis",
                    "normalized_value": "R4 600mm",
                    "confidence": 0.99,
                    "method": "vision",
                },
            ],
            "line_endpoints": [endpoint],
        },
    )
    neighbor = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(terminal, neighbor),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=neighbor.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, terminal.shelf_id)

    assert seed["direction_resolution"]["status"] == "exact_match"
    assert seed["provider_resolution"]["preselect_allowed"] is True
    assert "PROVIDER_DEGREE_COUNT_NOT_ESTABLISHED" not in seed[
        "provider_resolution"
    ]["reason_codes"]
    assert seed["line_1_route_side"] == "Z"
    assert (
        "TERMINAL_ADJACENCY_NORMALIZED_FROM_ORDERED_SPAN"
        in seed["direction_resolution"]["reason_codes"]
    )
    assert seed["direction_resolution"]["deployable_cli"] is False
    assert seed["direction_resolution"]["topology_adjustments"] == [
        {
            "rule_id": "terminal-endpoint-adjacency-from-ordered-span-v1",
            "original_adjacency": "preceding",
            "normalized_adjacency": "following",
            "status": "derived_pending_review",
            "deployable_cli": False,
        }
    ]
    assert seed["lines_by_side"]["A"]["represented_by_route_span"] is False
    assert seed["lines_by_side"]["Z"]["neighbor_node"] == "USQTN1-L8I2"
    assert seed["lines_by_side"]["Z"]["represented_by_route_span"] is True

    conflicting_endpoint = {
        **endpoint,
        "line_out_port": 99,
        "evidence": [
            (
                {
                    **item,
                    "normalized_value": 99,
                }
                if item["field"].endswith(".line_out_port")
                else item
            )
            for item in endpoint["evidence"]
        ],
    }
    conflicting_terminal = replace(
        terminal,
        source_evidence={
            **terminal.source_evidence,
            "line_endpoints": [conflicting_endpoint],
        },
    )
    conflicting_project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(conflicting_terminal, neighbor),
        diagram_source=project.diagram_source,
        links=project.links,
        require_valid=False,
    )
    conflicting_seed = module._r4_0_editor_seed(
        conflicting_project,
        conflicting_terminal.shelf_id,
    )

    assert conflicting_seed["direction_resolution"]["status"] == "conflict"
    assert conflicting_seed["line_1_route_side"] == ""
    assert conflicting_seed["provider_resolution"]["preselect_allowed"] is False
    assert (
        "DIRECT_LINE_OUTPUT_PROVIDER_MISMATCH"
        in conflicting_seed["direction_resolution"]["reason_codes"]
    )
    assert (
        "AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK"
        not in conflicting_seed["direction_resolution"]["reason_codes"]
    )


def test_sole_terminal_candidate_uses_audited_direction_fallback() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        raman_label="",
        source_evidence={
            **_no_sra_source_evidence(),
            "chassis": "R4 600mm",
            "fields": [
                {
                    "field": "chassis",
                    "normalized_value": "R4 600mm",
                    "confidence": 0.99,
                    "method": "vision",
                },
            ],
            # The diagram provider omitted the visible local line endpoint.
            "line_endpoints": [],
        },
    )
    neighbor = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(terminal, neighbor),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=terminal.shelf_id,
                to_shelf_id=neighbor.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="BDJW7353",
                        expected_loss_db=14.55,
                        fiber_type="LEAF",
                    ),
                ),
            ),
        ),
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, terminal.shelf_id)

    assert seed["provider_resolution"]["preselect_allowed"] is False
    assert "INSUFFICIENT_PROVIDER_IDENTITY_EVIDENCE" in seed[
        "provider_resolution"
    ]["reason_codes"]
    assert seed["direction_resolution"]["status"] == "controlled_fallback"
    assert seed["direction_resolution"]["reason_codes"] == [
        "AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK"
    ]
    assert seed["direction_resolution"]["deployable_cli"] is False
    assert seed["line_1_route_side"] == "Z"
    assert seed["lines_by_side"]["Z"]["neighbor_node"] == "USQTN1-L8I2"
    assert seed["lines_by_side"]["Z"]["link_name"] == "BDJW7353"
    assert seed["lines_by_side"]["Z"]["fiber_type"] == "LEAF"
    assert seed["lines_by_side"]["Z"]["expected_loss_db"] == 14.55
    assert "prepopulated_fixed_direction_review" in seed[
        "prepopulation"
    ]["manual_fields"]
    assert (
        "fixed_direction_from_audited_provider_role_fallback"
        in seed["prepopulation"]["controlled_derivations"]
    )


def test_direct_shelf_c_band_is_an_explicit_route_band_partition_override() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={
            "chassis": "R4 600mm",
            "band": "c",
            "fields": [
                {
                    "field": "chassis",
                    "normalized_value": "R4 600mm",
                    "confidence": 0.99,
                    "method": "vision",
                },
                {
                    "field": "band",
                    "normalized_value": "c",
                    "confidence": 0.99,
                    "method": "native_text",
                },
            ],
            "line_endpoints": [],
        },
    )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        rows=(terminal,),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            }
        },
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, terminal.shelf_id)
    resolution = seed["provider_resolution"]

    assert resolution["status"] == "unique_candidate"
    assert resolution["band_scope"] == "c"
    assert resolution["band_scope_source"] == "direct_shelf"
    assert resolution["review_provider_ids"]
    assert resolution["preselect_allowed"] is False
    assert "ROUTE_SCOPE_OPTICAL_BAND_MISMATCH" not in resolution["reason_codes"]
    assert "INSUFFICIENT_PROVIDER_IDENTITY_EVIDENCE" in resolution[
        "reason_codes"
    ]


def test_low_confidence_endpoint_does_not_use_direction_fallback() -> None:
    module = _route_module()
    terminal = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        source_evidence={
            "chassis": "R4 600mm",
            "fields": [
                {
                    "field": "chassis",
                    "normalized_value": "R4 600mm",
                    "confidence": 0.99,
                    "method": "vision",
                },
            ],
            "line_endpoints": [
                {
                    "adjacency": "following",
                    "slot": 1,
                    "line_out_port": 53,
                    "evidence": [
                        {
                            "field": "line_endpoints.0.adjacency",
                            "normalized_value": "following",
                            "confidence": 0.99,
                            "method": "inferred",
                        },
                        {
                            "field": "line_endpoints.0.slot",
                            "normalized_value": 1,
                            "confidence": 0.5,
                            "method": "vision",
                        },
                        {
                            "field": "line_endpoints.0.line_out_port",
                            "normalized_value": 53,
                            "confidence": 0.5,
                            "method": "vision",
                        },
                    ],
                },
            ],
        },
    )
    neighbor = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="ELP1-QTN1",
        revision="1",
        rows=(terminal, neighbor),
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, terminal.shelf_id)

    assert seed["provider_resolution"]["preselect_allowed"] is False
    assert seed["direction_resolution"]["status"] == "missing_evidence"
    assert seed["line_1_route_side"] == ""
    assert "AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK" not in seed[
        "direction_resolution"
    ]["reason_codes"]


def test_r40_direction_facts_follow_ordered_neighbors_not_link_tuple_order() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
    )
    middle = _row(
        module,
        shelf_id="ila-mid",
        profile_id="ila",
        site_key="site-mid",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
    )
    last = _row(
        module,
        shelf_id="roadm-z",
        profile_id="roadm_z",
        site_key="site-z",
        site_code="SAT4",
        site_name="San Antonio",
        tid="USSAT4-L8R3",
    )
    a_link = module.RouteLink(
        link_id="link-a-mid",
        order=1,
        from_shelf_id=first.shelf_id,
        to_shelf_id=middle.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-a-mid",
                path_role="route",
                link_name="A-SPAN",
                expected_loss_db=14.55,
                fiber_type="NDSF",
            ),
        ),
    )
    z_link = module.RouteLink(
        link_id="link-mid-z",
        order=2,
        from_shelf_id=middle.shelf_id,
        to_shelf_id=last.shelf_id,
        paths=(
            module.OpticalPath(
                path_id="path-mid-z",
                path_role="route",
                link_name="Z-SPAN",
                expected_loss_db=15.25,
                fiber_type="NDSF",
            ),
        ),
    )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(first, middle, last),
        # Saved projects preserve serialized tuple order, so adjacency lookup
        # must use endpoint identity rather than positional link indexing.
        links=(z_link, a_link),
        require_valid=False,
    )

    facts = module._r4_0_review_facts(project, middle.shelf_id)

    assert facts["a_neighbor_tid"] == "USELP1-L8R2"
    assert facts["a_span_link_name"] == "A-SPAN"
    assert facts["a_span_loss_db"] == 14.55
    assert facts["a_outbound_flow"] == "Z→A"
    assert facts["a_inbound_flow"] == "A→Z"
    assert facts["z_neighbor_tid"] == "USSAT4-L8R3"
    assert facts["z_span_link_name"] == "Z-SPAN"
    assert facts["z_span_loss_db"] == 15.25
    assert facts["z_outbound_flow"] == "A→Z"
    assert facts["z_inbound_flow"] == "Z→A"


def test_r40_assumption_adapter_excludes_editor_only_span_context() -> None:
    module = _route_module()
    first = _row(
        module,
        shelf_id="roadm-a",
        profile_id="roadm_a",
        site_code="ELP1",
        site_name="El Paso",
        tid="USELP1-L8R2",
        software_release="RLS R4.0",
    )
    second = _row(
        module,
        shelf_id="ila-z",
        profile_id="ila",
        site_key="site-z",
        site_code="QTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        primary_oam_ip="192.0.2.11",
        software_release="RLS R4.0",
    )
    project = module.build_route_project(
        route_code="ELP1-QTN1",
        title="Assumption adapter audit",
        revision="1",
        ospf_area="10.6.8.0",
        rows=(first, second),
        links=(
            module.RouteLink(
                link_id="link-a-z",
                order=1,
                from_shelf_id=first.shelf_id,
                to_shelf_id=second.shelf_id,
                paths=(
                    module.OpticalPath(
                        path_id="path-a-z",
                        path_role="route",
                        link_name="CUSTOMER-SPAN",
                        expected_loss_db=14.55,
                        distance_km=62.47,
                        fiber_type="NDSF",
                    ),
                ),
            ),
        ),
    )

    raw = module._r4_0_review_facts(project, first.shelf_id)
    filtered = module._r4_0_assumption_facts(project, first.shelf_id)

    assert raw["z_span_link_name"] == "CUSTOMER-SPAN"
    assert raw["z_span_present"] is True
    assert "z_span_link_name" not in filtered
    assert "z_span_present" not in filtered
    from utils.rls_config.r4_0_review import build_r4_0_review

    artifact = build_r4_0_review(first.profile_id, filtered)
    assert artifact.profile.profile_id == "roadm_a"
    assert "z_neighbor_tid" in artifact.diagram_facts


def test_build_route_project_rejects_conflicting_site_names() -> None:
    module = _route_module()
    first = _row(module)
    second = _row(
        module,
        shelf_id="shelf-2",
        site_name="Cicero",
        tid="CHI-ILA-02",
        primary_oam_ip="192.0.2.11",
    )

    with pytest.raises(ValueError, match="conflicting names"):
        module.build_route_project(
            route_code="CHI",
            title="Chicago route",
            revision="1",
            rows=(first, second),
        )


def test_build_route_project_disambiguates_normalized_site_key_collisions() -> None:
    module = _route_module()
    first = _row(module, site_key="", site_code="A B", site_name="Site One")
    second = _row(
        module,
        shelf_id="shelf-2",
        site_key="",
        site_code="A-B",
        site_name="Site Two",
        tid="AB-ILA-02",
        primary_oam_ip="192.0.2.11",
    )

    project = module.build_route_project(
        route_code="AB",
        title="Normalized key collision",
        revision="1",
        rows=(first, second),
    )

    assert [site.site_key for site in project.sites] == ["site-a-b", "site-a-b-2"]


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    (
        ("profile_id", "roadm_a"),
        ("site_key", "site-den"),
        ("site_code", "DEN"),
        ("site_name", "Denver"),
        ("tid", "CHI-ILA-99"),
        ("primary_oam_ip", "192.0.2.99"),
        ("software_release", "R2.1"),
        ("shelf_variant", "K74-CHANGED"),
    ),
)
def test_provider_identity_change_detection_covers_every_payload_binding(
    field_name: str,
    changed_value: str,
) -> None:
    module = _route_module()
    current = _row(module, profile_payload={"schema_id": "provider.request"})

    assert module.provider_identity_changed(
        current,
        replace(current, **{field_name: changed_value}),
    )


def test_document_only_labels_do_not_invalidate_provider_payload() -> None:
    module = _route_module()
    current = _row(module, profile_payload={"schema_id": "provider.request"})
    replacement = replace(
        current,
        raman_label="Slot 6",
        power_label="A/B -48 VDC",
    )

    assert not module.provider_identity_changed(current, replacement)


def test_structured_sra_callout_review_is_explicit_and_preserves_evidence() -> None:
    module = _route_module()
    evidence = {
        "source_sha256": "a" * 64,
        "raman_callouts": [
            {
                "raw_text": "6/5",
                "slot": 6,
                "port": 5,
                "shelf_tid": "CHI-ILA-01",
                "context": "shelf_endpoint",
            },
            {
                "raw_text": "6/6",
                "slot": 6,
                "port": 6,
                "shelf_tid": "CHI-ILA-01",
                "context": "shelf_endpoint",
            },
        ],
        "raman_callout_review": "pending",
    }

    accepted, changed = module._review_raman_callout_evidence(
        evidence,
        "Slot 6",
    )
    rejected, changed_again = module._review_raman_callout_evidence(
        accepted,
        "",
    )

    assert accepted["raman_callout_review"] == "accepted"
    assert accepted["raman_callouts"] == evidence["raman_callouts"]
    assert accepted["raman_callout_review_record"]["deployable_cli"] is False
    assert changed is True
    assert module._has_accepted_structured_sra(accepted) is True
    assert rejected["raman_callout_review"] == "rejected"
    assert rejected["raman_callouts"] == evidence["raman_callouts"]
    assert changed_again is True
    assert module._has_accepted_structured_sra(rejected) is False


def test_pending_shelf_navigation_wraps_and_skips_reviewed_rows() -> None:
    module = _route_module()
    rows = (
        _row(module, shelf_id="a", review_state="confirmed"),
        _row(module, shelf_id="b", review_state="pending"),
        _row(module, shelf_id="c", review_state="corrected"),
        _row(module, shelf_id="d", review_state="pending"),
    )

    assert module._next_pending_shelf_id(rows, 0) == "b"
    assert module._next_pending_shelf_id(rows, 1) == "d"
    assert module._next_pending_shelf_id(rows, 3) == "b"
    assert module._next_pending_shelf_id(rows, -1) == ""
    assert (
        module._next_pending_shelf_id(
            tuple(replace(row, review_state="confirmed") for row in rows),
            0,
        )
        == ""
    )


def test_exact_config_navigation_skips_completed_and_sra_blocked_shelves() -> None:
    module = _route_module()
    rows = (
        _row(
            module,
            shelf_id="a",
            profile_id="roadm_a",
            profile_payload={},
        ),
        _row(
            module,
            shelf_id="done",
            profile_id="ila",
            profile_payload={
                "schema_id": "ciena.rls.r4-0-exact-request",
                "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
            },
        ),
        _row(
            module,
            shelf_id="sra",
            profile_id="ila",
            profile_payload={},
            source_evidence={
                "raman_callouts": [{"slot": 4, "port": 5}],
                "raman_callout_review": "accepted",
            },
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="roadm_z",
            profile_payload={},
        ),
    )

    assert module._next_exact_config_review_shelf_id(rows, 3) == "a"
    assert module._next_exact_config_review_shelf_id(rows, 0) == "sra"


def test_exact_config_review_progress_counts_only_current_payloads() -> None:
    module = _route_module()
    rows = (
        _row(module, shelf_id="pending", profile_payload={}),
        _row(
            module,
            shelf_id="current",
            profile_payload={
                "schema_id": "ciena.rls.r4-0-exact-request",
                "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
            },
        ),
        _row(
            module,
            shelf_id="retired",
                profile_payload={
                    "schema_id": "ciena.rls.r4-0-exact-request",
                    "schema_version": "0.9",
                },
            ),
        )

    assert module._exact_config_review_progress(rows) == (1, 3)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    (
        ({}, "not_applicable"),
        ({"raman_callouts": []}, "not_applicable"),
        ({"raman_callouts": [{"slot": 4}]}, "pending"),
        (
            {
                "raman_callouts": [{"slot": 4}],
                "raman_callout_review": {"status": "accepted"},
            },
            "accepted",
        ),
        (
            {
                "raman_callouts": [{"slot": 4}],
                "raman_callout_review": "rejected",
            },
            "rejected",
        ),
        ({"raman_callouts": {"slot": 4}}, "invalid"),
        (
            {
                "raman_callouts": [{"slot": 4}],
                "raman_callout_review": "invented",
            },
            "invalid",
        ),
    ),
)
def test_structured_raman_review_status_fails_closed(
    evidence: dict[str, object],
    expected: str,
) -> None:
    module = _route_module()

    assert module._raman_callout_review_status(evidence) == expected


@pytest.mark.parametrize(
    ("source_evidence", "expected_status"),
    (
        (
            {
                "raman_callouts": [{"slot": 4, "port": 5}],
                "raman_callout_review": "pending",
            },
            "pending",
        ),
        (
            {
                "raman_callouts": {"slot": 4, "port": 5},
                "raman_callout_review": "accepted",
            },
            "invalid",
        ),
    ),
)
def test_pending_or_invalid_raman_blocks_provider_and_direction_autofill(
    source_evidence: dict[str, object],
    expected_status: str,
) -> None:
    module = _route_module()
    row = _row(
        module,
        profile_id="ila",
        source_evidence=source_evidence,
    )
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1-SAT4",
        revision="1",
        rows=(row,),
        diagram_source={
            "route_header": {
                "optical_band": "c+l",
                "optical_band_status": "direct_supported",
            },
        },
        require_valid=False,
    )

    seed = module._r4_0_editor_seed(project, row.shelf_id)

    assert seed["provider_resolution"][
        "raman_callout_review_status"
    ] == expected_status
    assert seed["provider_resolution"]["preselect_allowed"] is False
    assert module._r40_sole_candidate_provider_id(
        seed["provider_resolution"]
    ) == ""
    assert seed["line_1_route_side"] == ""
    assert seed["direction_resolution"]["status"] == "missing_evidence"
    assert "fixed_direction_to_route_side" in seed[
        "prepopulation"
    ]["manual_fields"]


def test_exact_review_refuses_role_without_sra_provider_for_accepted_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    row = _row(
        module,
        profile_id="add_drop_a",
        software_release="RLS R4.0",
        source_evidence={
            "raman_callouts": [
                {"raw_text": "4/5"},
                {"raw_text": "4/6"},
            ],
            "raman_callout_review": "accepted",
        },
    )
    warnings: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    module.RlsRouteFrame._open_r4_0_review(
        SimpleNamespace(),
        row,
        SimpleNamespace(),
        "route-fingerprint",
    )

    assert warnings
    assert warnings[0][0][0] == "SRA-capable R4.0 provider required"
    assert "no registered role-compatible exact" in warnings[0][0][1]


def test_ospf_edit_clears_every_route_bound_provider_payload() -> None:
    module = _route_module()
    payload = {"schema_id": "provider.request"}
    rows = [
        _row(
            module,
            shelf_id="a",
            profile_id="add_drop_a",
            software_release="RLS R4.0",
            profile_payload=payload,
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="roadm_z",
            tid="Z-ROADM-01",
            primary_oam_ip="192.0.2.11",
            software_release="RLS R4.0",
            profile_payload=payload,
        ),
    ]
    calls: list[str] = []
    subject = SimpleNamespace(
        _loading_project=False,
        _rows=rows,
        _on_project_edited=lambda: calls.append("project"),
        _refresh_tree=lambda: calls.append("tree"),
        _refresh_status=lambda message="": calls.append(message),
    )

    module.RlsRouteFrame._on_ospf_area_edited(subject)

    assert all(not row.profile_payload for row in subject._rows)
    assert calls[:2] == ["project", "tree"]
    assert "Review every affected configuration" in calls[2]


def test_sra_topology_edits_restage_only_the_facing_peer_in_either_order() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project
    from utils.rls_config.r4_0_generator import (
        decode_r40_exact_payload,
        encode_r40_exact_payload,
    )

    project = _sra_pair_project()
    rows = [
        module._ShelfEditorRow(
            shelf_id=shelf.shelf_id,
            profile_id=shelf.profile_id,
            site_key=shelf.site_key,
            site_code=project.site_by_key(shelf.site_key).code,
            site_name=project.site_by_key(shelf.site_key).name,
            tid=shelf.tid,
            primary_oam_ip=shelf.primary_oam_ip,
            software_release=shelf.software_release,
            shelf_variant=shelf.shelf_variant,
            raman_label=shelf.raman_label,
            power_label=shelf.power_label,
            profile_payload=dict(shelf.profile_payload),
            review_state=shelf.review_state,
            source_evidence=dict(shelf.source_evidence),
        )
        for shelf in project.shelves
    ]

    for active_index, peer_index in ((0, 1), (1, 0)):
        previous = decode_r40_exact_payload(
            rows[active_index].profile_payload
        )
        assert previous.line_1 is not None
        replacement = replace(
            previous,
            line_1=replace(
                previous.line_1,
                expected_loss_db=previous.line_1.expected_loss_db + 0.25,
            ),
        )
        candidate_rows = list(rows)
        candidate_rows[active_index] = replace(
            candidate_rows[active_index],
            profile_payload=encode_r40_exact_payload(replacement),
        )
        original_peer_payload = dict(
            candidate_rows[peer_index].profile_payload
        )

        restaged, cleared = module._restage_changed_r40_sra_peers(
            candidate_rows,
            project,
            rows[active_index].shelf_id,
            previous,
            replacement,
        )

        assert cleared == (rows[peer_index].shelf_id,)
        assert dict(restaged[peer_index].profile_payload) == {}
        assert dict(restaged[active_index].profile_payload) == dict(
            candidate_rows[active_index].profile_payload
        )
        assert dict(candidate_rows[peer_index].profile_payload) == (
            original_peer_payload
        )


def test_non_topology_sra_review_edits_preserve_reciprocal_peer_payload() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project
    from utils.rls_config.r4_0_generator import decode_r40_exact_payload

    project = _sra_pair_project()
    rows = [
        module._ShelfEditorRow(
            shelf_id=shelf.shelf_id,
            profile_id=shelf.profile_id,
            site_key=shelf.site_key,
            site_code=project.site_by_key(shelf.site_key).code,
            site_name=project.site_by_key(shelf.site_key).name,
            tid=shelf.tid,
            primary_oam_ip=shelf.primary_oam_ip,
            software_release=shelf.software_release,
            shelf_variant=shelf.shelf_variant,
            raman_label=shelf.raman_label,
            power_label=shelf.power_label,
            profile_payload=dict(shelf.profile_payload),
            review_state=shelf.review_state,
            source_evidence=dict(shelf.source_evidence),
        )
        for shelf in project.shelves
    ]
    previous = decode_r40_exact_payload(rows[0].profile_payload)
    replacement = replace(
        previous,
        target_software_build="4.00.01",
        frame_identification_code="Rack assigned on site",
        management=replace(
            previous.management,
            ip_address="192.0.2.222",
        ),
    )

    preserved, cleared = module._restage_changed_r40_sra_peers(
        rows,
        project,
        rows[0].shelf_id,
        previous,
        replacement,
    )

    assert cleared == ()
    assert tuple(
        dict(row.profile_payload) for row in preserved
    ) == tuple(
        dict(row.profile_payload) for row in rows
    )


def test_candidate_pair_review_is_order_independent_and_full_contract() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project

    complete = _sra_pair_project()

    for current_index, peer_index in ((0, 1), (1, 0)):
        complete_result = module._validate_r40_candidate_pair_review(
            complete,
            complete.shelves[current_index].shelf_id,
        )
        assert complete_result.pending_peer_ids == ()
        assert complete_result.validated_peer_ids == (
            complete.shelves[peer_index].shelf_id,
        )

        shelves = list(complete.shelves)
        shelves[peer_index] = replace(
            shelves[peer_index],
            profile_payload={},
        )
        staged = replace(complete, shelves=tuple(shelves))
        staged_result = module._validate_r40_candidate_pair_review(
            staged,
            staged.shelves[current_index].shelf_id,
        )
        assert staged_result.pending_peer_ids == (
            staged.shelves[peer_index].shelf_id,
        )
        assert staged_result.validated_peer_ids == ()


def test_candidate_pair_review_rechecks_peer_generator_before_promotion() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project
    from utils.rls_config.r4_0_generator import (
        decode_r40_exact_payload,
        encode_r40_exact_payload,
    )

    complete = _sra_pair_project()
    peer_request = decode_r40_exact_payload(
        complete.shelves[1].profile_payload
    )
    invalid_peer = replace(peer_request, target_software_build="")
    candidate = replace(
        complete,
        shelves=(
            complete.shelves[0],
            replace(
                complete.shelves[1],
                profile_payload=encode_r40_exact_payload(invalid_peer),
            ),
        ),
    )
    serialized_before = candidate.to_dict()

    with pytest.raises(
        ValueError,
        match="R40_EXACT_GENERATOR_VALIDATION_FAILED",
    ):
        module._validate_r40_candidate_pair_review(
            candidate,
            candidate.shelves[0].shelf_id,
        )

    assert candidate.to_dict() == serialized_before


def test_stale_exact_review_snapshot_is_rejected_before_mutation() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project

    original = _sra_pair_project()
    changed = replace(original, title="Changed while review was open")
    rows_before = tuple(changed.shelves)
    subject = SimpleNamespace(
        _build_project=lambda **_kwargs: changed,
        _rows=list(changed.shelves),
    )

    with pytest.raises(ValueError, match="route changed"):
        module.RlsRouteFrame._review_snapshot_is_current(
            subject,
            module.route_project_fingerprint(original),
            original.shelves[0].shelf_id,
        )

    assert tuple(subject._rows) == rows_before


def test_update_clears_stale_payload_and_surfaces_revalidation_status() -> None:
    module = _route_module()
    payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": "1.0",
        "request": {"profile": "ila"},
    }
    current = _row(
        module,
        profile_id="ila",
        software_release="RLS R4.0",
        profile_payload=payload,
    )
    replacement = replace(current, tid="CHI-ILA-02")
    calls: dict[str, object] = {}
    subject = SimpleNamespace(
        _rows=[current],
        _site_code_var=SimpleNamespace(get=lambda: current.site_code),
        _selected_index=lambda: 0,
        _read_editor_row=lambda **_kwargs: replacement,
        _committed_project_changed=lambda: calls.setdefault("committed", True),
        _refresh_tree=lambda **kwargs: calls.setdefault("tree", kwargs),
        _refresh_status=lambda message="": calls.setdefault("status", message),
    )

    updated = module.RlsRouteFrame._update_selected(subject)

    assert updated is True
    assert subject._rows[0].tid == "CHI-ILA-02"
    assert dict(subject._rows[0].profile_payload) == {}
    assert calls["committed"] is True
    assert "deployment payload was cleared" in str(calls["status"])
    assert "revalidate" in str(calls["status"])


def test_update_preserves_payload_for_raman_and_power_only_changes() -> None:
    module = _route_module()
    payload = {"schema_id": "provider.request", "request": {"profile": "ila"}}
    current = _row(module, profile_payload=payload)
    replacement = replace(
        current,
        raman_label="Slot 6",
        power_label="A/B -48 VDC",
    )
    calls: dict[str, object] = {}
    subject = SimpleNamespace(
        _rows=[current],
        _site_code_var=SimpleNamespace(get=lambda: current.site_code),
        _selected_index=lambda: 0,
        _read_editor_row=lambda **_kwargs: replacement,
        _committed_project_changed=lambda: calls.setdefault("committed", True),
        _refresh_tree=lambda **kwargs: calls.setdefault("tree", kwargs),
        _refresh_status=lambda message="": calls.setdefault("status", message),
    )

    updated = module.RlsRouteFrame._update_selected(subject)

    assert updated is True
    assert dict(subject._rows[0].profile_payload) == payload
    assert calls["status"] == "Selected shelf updated."


@pytest.mark.parametrize(
    ("replacement_overrides", "expected_state"),
    (
        ({}, "confirmed"),
        ({"tid": "CHI-ILA-CORRECTED"}, "corrected"),
        ({"raman_label": "Slot 6"}, "corrected"),
    ),
)
def test_update_selected_marks_imported_rows_confirmed_or_corrected(
    replacement_overrides: dict[str, str],
    expected_state: str,
) -> None:
    module = _route_module()
    evidence = {"source_sha256": "diagram-1", "fields": []}
    current = _row(
        module,
        review_state="pending",
        source_evidence=evidence,
    )
    replacement = replace(current, **replacement_overrides)
    calls: dict[str, object] = {}
    subject = SimpleNamespace(
        _rows=[current],
        _site_code_var=SimpleNamespace(get=lambda: current.site_code),
        _selected_index=lambda: 0,
        _read_editor_row=lambda **_kwargs: replacement,
        _committed_project_changed=lambda: calls.setdefault("committed", True),
        _refresh_tree=lambda **kwargs: calls.setdefault("tree", kwargs),
        _refresh_status=lambda message="": calls.setdefault("status", message),
    )

    did_update = module.RlsRouteFrame._update_selected(subject)

    assert did_update is True
    updated = subject._rows[0]
    assert updated.review_state == expected_state
    assert dict(updated.source_evidence) == evidence
    assert calls["committed"] is True


def test_update_selected_reports_failure_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    warnings: list[str] = []
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda title, *_args, **_kwargs: warnings.append(title),
    )
    no_selection = SimpleNamespace(_selected_index=lambda: None)

    assert module.RlsRouteFrame._update_selected(no_selection) is False

    row = _row(module)

    def incomplete(**_kwargs):
        raise ValueError("POWER is required.")

    incomplete_editor = SimpleNamespace(
        _rows=[row],
        _selected_index=lambda: 0,
        _site_code_var=SimpleNamespace(get=lambda: row.site_code),
        _read_editor_row=incomplete,
    )

    assert module.RlsRouteFrame._update_selected(incomplete_editor) is False
    assert warnings == ["No shelf selected", "Shelf details incomplete"]


def test_confirm_and_next_pending_advances_only_after_success() -> None:
    module = _route_module()
    rows = [
        _row(module, shelf_id="a", review_state="pending"),
        _row(module, shelf_id="b", review_state="pending"),
        _row(module, shelf_id="c", review_state="confirmed"),
    ]
    calls: list[object] = []
    selected: list[str] = []
    loaded: list[str] = []

    def select_next(*, select_id: str) -> None:
        selected[:] = [select_id]
        calls.append(("tree", {"select_id": select_id}))

    def load_selected() -> None:
        loaded[:] = selected
        calls.append(("load", selected[0]))

    subject = SimpleNamespace(
        _rows=rows,
        _selected_index=lambda: 0,
        _update_selected=lambda: (
            subject._rows.__setitem__(
                0,
                replace(subject._rows[0], review_state="confirmed"),
            )
            or True
        ),
        _refresh_tree=select_next,
        _on_tree_select=load_selected,
        _refresh_status=lambda message="": calls.append(("status", message)),
    )

    module.RlsRouteFrame._confirm_and_next_pending(subject)

    assert calls[0] == ("tree", {"select_id": "b"})
    assert calls[1] == ("load", "b")
    assert selected == ["b"]
    assert loaded == ["b"]
    assert "1 imported shelf remains pending" in calls[2][1]

    refused_calls: list[object] = []
    refused = SimpleNamespace(
        _rows=rows,
        _selected_index=lambda: 0,
        _update_selected=lambda: False,
        _refresh_tree=lambda **kwargs: refused_calls.append(kwargs),
        _on_tree_select=lambda: refused_calls.append("load"),
        _refresh_status=lambda message="": refused_calls.append(message),
    )

    module.RlsRouteFrame._confirm_and_next_pending(refused)

    assert refused_calls == []


def test_confirm_and_next_pending_reports_shelf_fact_review_complete() -> None:
    module = _route_module()
    rows = [_row(module, review_state="pending")]
    statuses: list[str] = []
    calls: list[object] = []
    button_labels: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _selected_index=lambda: 0,
        _update_selected=lambda: (
            subject._rows.__setitem__(
                0,
                replace(subject._rows[0], review_state="confirmed"),
            )
            or True
        ),
        _refresh_tree=lambda **kwargs: calls.append(("tree", kwargs)),
        _on_tree_select=lambda: calls.append("load"),
        _review_config_button=SimpleNamespace(
            configure=lambda **kwargs: button_labels.append(kwargs["text"])
        ),
        _refresh_status=lambda message="": statuses.append(message),
    )

    module.RlsRouteFrame._confirm_and_next_pending(subject)

    assert calls == [
        ("tree", {"select_id": "shelf-1"}),
        "load",
    ]
    assert button_labels == ["Review Next: CHI-ILA-01…"]
    assert len(statuses) == 1
    assert statuses[0].startswith(
        "All imported shelf facts are reviewed. Continue with exact "
        "configuration and optical-path review."
    )
    assert (
        "Next exact configuration review: CHI-ILA-01 "
        "(0/1 complete; 1 remaining)."
        in statuses[0]
    )


def test_exact_review_offer_is_passive_and_reports_completion() -> None:
    module = _route_module()
    current_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }
    rows = [
        _row(module, shelf_id="a", tid="A", profile_payload=current_payload),
        _row(module, shelf_id="b", tid="B", profile_payload={}),
    ]
    events: list[object] = []
    labels: list[str] = []
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _refresh_tree=lambda **kwargs: events.append(("select", kwargs)),
        _on_tree_select=lambda: events.append("load"),
        _review_config_button=SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        ),
        _refresh_status=lambda message="": statuses.append(message),
    )

    next_id = module.RlsRouteFrame._offer_next_exact_config_review(
        subject,
        0,
        trigger="test",
    )

    assert next_id == "b"
    assert events == [("select", {"select_id": "b"}), "load"]
    assert labels == ["Review Next: B…"]
    assert "1/2 complete; 1 remaining" in statuses[-1]
    assert not hasattr(subject, "_open_r4_0_review")

    subject._rows[1] = replace(
        subject._rows[1],
        profile_payload=current_payload,
    )
    next_id = module.RlsRouteFrame._offer_next_exact_config_review(
        subject,
        1,
        trigger="test_complete",
    )

    assert next_id == ""
    assert labels[-1] == "Review Configuration…"
    assert statuses[-1] == "Exact configuration reviews complete (2/2)."


def test_pending_sra_peer_is_preferred_over_normal_review_queue_order() -> None:
    module = _route_module()
    current_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }
    rows = [
        _row(
            module,
            shelf_id="staged",
            tid="STAGED-SRA",
            profile_payload=current_payload,
        ),
        _row(
            module,
            shelf_id="normal-next",
            tid="NORMAL-NEXT",
            profile_id="roadm_a",
            profile_payload={},
        ),
        _row(
            module,
            shelf_id="paired-peer",
            tid="PAIRED-PEER",
            profile_id="roadm_z",
            raman_label="Slot 6",
            profile_payload={},
            source_evidence=_sra_source_evidence(
                "PAIRED-PEER",
                6,
                [],
            ),
        ),
    ]
    events: list[object] = []
    labels: list[str] = []
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _refresh_tree=lambda **kwargs: events.append(("select", kwargs)),
        _on_tree_select=lambda: events.append("load"),
        _review_config_button=SimpleNamespace(
            configure=lambda **kwargs: labels.append(kwargs["text"])
        ),
        _refresh_status=lambda message="": statuses.append(message),
    )

    next_id = module.RlsRouteFrame._offer_next_exact_config_review(
        subject,
        0,
        trigger="pending_sra_peer_test",
        preferred_shelf_id="paired-peer",
    )

    assert next_id == "paired-peer"
    assert events == [
        ("select", {"select_id": "paired-peer"}),
        "load",
    ]
    assert labels == ["Review Next: PAIRED-PEER…"]
    assert "Next exact configuration review: PAIRED-PEER" in statuses[-1]
    assert "1/3 complete; 2 remaining" in statuses[-1]


def test_successful_exact_review_closes_before_offering_next() -> None:
    module = _route_module()
    current_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }
    rows = [
        _row(module, shelf_id="a", tid="A", profile_payload=current_payload),
        _row(module, shelf_id="b", tid="B", profile_payload={}),
    ]
    events: list[object] = []
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _close_config_review=lambda window, **kwargs: events.append(
            ("close", window, kwargs)
        ),
        _refresh_tree=lambda **kwargs: events.append(("select", kwargs)),
        _on_tree_select=lambda: events.append("load"),
        _review_config_button=SimpleNamespace(
            configure=lambda **kwargs: events.append(("button", kwargs["text"]))
        ),
        _refresh_status=lambda message="": statuses.append(message),
    )
    window = object()

    module.RlsRouteFrame._finish_applied_config_review(
        subject,
        window,
        0,
        provider_id="provider-a",
        reviewed_tid="A",
    )

    assert events[0] == ("close", window, {"reason": "applied"})
    assert events[1:] == [
        ("select", {"select_id": "b"}),
        "load",
        ("button", "Review Next: B…"),
    ]
    assert statuses[-1].startswith(
        "Applied validated exact R4.0 configuration and endpoint-path review."
    )
    assert "Next exact configuration review: B" in statuses[-1]


def test_staged_sra_review_closes_and_offers_its_paired_peer_next() -> None:
    module = _route_module()
    current_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }
    rows = [
        _row(
            module,
            shelf_id="staged",
            tid="STAGED-SRA",
            profile_payload=current_payload,
        ),
        _row(
            module,
            shelf_id="other",
            tid="OTHER",
            profile_id="roadm_a",
            profile_payload={},
        ),
        _row(
            module,
            shelf_id="paired-peer",
            tid="PAIRED-PEER",
            profile_id="roadm_z",
            raman_label="Slot 6",
            profile_payload={},
            source_evidence=_sra_source_evidence(
                "PAIRED-PEER",
                6,
                [],
            ),
        ),
    ]
    events: list[object] = []
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _close_config_review=lambda window, **kwargs: events.append(
            ("close", window, kwargs)
        ),
        _refresh_tree=lambda **kwargs: events.append(("select", kwargs)),
        _on_tree_select=lambda: events.append("load"),
        _review_config_button=SimpleNamespace(
            configure=lambda **kwargs: events.append(
                ("button", kwargs["text"])
            )
        ),
        _refresh_status=lambda message="": statuses.append(message),
    )
    window = object()

    module.RlsRouteFrame._finish_applied_config_review(
        subject,
        window,
        0,
        provider_id="sra-provider",
        reviewed_tid="STAGED-SRA",
        pending_peer_ids=("paired-peer",),
    )

    assert events == [
        ("close", window, {"reason": "applied"}),
        ("select", {"select_id": "paired-peer"}),
        "load",
        ("button", "Review Next: PAIRED-PEER…"),
    ]
    assert statuses[-1].startswith(
        "Staged the locally validated SRA endpoint; paired SRA peer review "
        "is pending. Deployable CLI and route-bundle export remain blocked."
    )
    assert "Next exact configuration review: PAIRED-PEER" in statuses[-1]


def test_operator_close_does_not_advance_exact_review_queue() -> None:
    module = _route_module()
    events: list[str] = []

    class _Window:
        def winfo_exists(self) -> bool:
            return True

        def grab_release(self) -> None:
            events.append("release")

        def destroy(self) -> None:
            events.append("destroy")

    window = _Window()
    subject = SimpleNamespace(_config_review_window=window)

    module.RlsRouteFrame._close_config_review(
        subject,
        window,
        reason="operator",
    )

    assert events == ["release", "destroy"]
    assert subject._config_review_window is None


def test_exact_review_queue_tolerates_optional_gui_attrs_absent() -> None:
    module = _route_module()
    row = _row(module, shelf_id="a", tid="A", profile_payload={})
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=[row],
        _refresh_tree=lambda **_kwargs: None,
        _on_tree_select=lambda: None,
        _refresh_status=lambda message="": statuses.append(message),
    )

    assert (
        module.RlsRouteFrame._offer_next_exact_config_review(
            subject,
            0,
            trigger="optional_attrs_test",
        )
        == "a"
    )
    assert "Next exact configuration review: A" in statuses[-1]

    class _Window:
        def winfo_exists(self) -> bool:
            return False

    module.RlsRouteFrame._close_config_review(
        subject,
        _Window(),
        reason="route_destroy",
    )


class _SelectionTree:
    def __init__(self, selected: str) -> None:
        self.selected = selected
        self.focused = ""
        self.seen = ""

    def selection(self):
        return (self.selected,) if self.selected else ()

    def exists(self, shelf_id: str) -> bool:
        return shelf_id in {"a", "b"}

    def selection_set(self, shelf_id: str) -> None:
        self.selected = shelf_id

    def selection_remove(self, shelf_id: str) -> None:
        if self.selected == shelf_id:
            self.selected = ""

    def focus(self, shelf_id: str) -> None:
        self.focused = shelf_id

    def see(self, shelf_id: str) -> None:
        self.seen = shelf_id


def test_tree_selection_preserves_uncommitted_editor_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    warnings: list[str] = []
    tree = _SelectionTree("b")
    subject = SimpleNamespace(
        _tree=tree,
        _rows=[
            _row(module, shelf_id="a"),
            _row(module, shelf_id="b"),
        ],
        _selected_index=lambda: 1,
        _editor_dirty=True,
        _editor_shelf_id="a",
        _restoring_tree_selection=False,
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda title, *_args, **_kwargs: warnings.append(title),
    )

    module.RlsRouteFrame._on_tree_select(subject)

    assert tree.selection() == ("a",)
    assert tree.focused == "a"
    assert tree.seen == "a"
    assert subject._editor_dirty is True
    assert warnings == ["Shelf edits not applied"]


def test_same_shelf_selection_event_does_not_overwrite_dirty_editor() -> None:
    module = _route_module()
    subject = SimpleNamespace(
        _tree=_SelectionTree("a"),
        _rows=[_row(module, shelf_id="a")],
        _selected_index=lambda: 0,
        _editor_dirty=True,
        _editor_shelf_id="a",
        _restoring_tree_selection=False,
        _load_editor_row=lambda _row: pytest.fail(
            "dirty editor must not be reloaded"
        ),
    )

    module.RlsRouteFrame._on_tree_select(subject)

    assert subject._editor_dirty is True


def test_unsaved_add_form_refuses_shelf_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    tree = _SelectionTree("b")
    subject = SimpleNamespace(
        _tree=tree,
        _rows=[_row(module, shelf_id="b")],
        _selected_index=lambda: 0,
        _editor_dirty=True,
        _editor_shelf_id="",
        _restoring_tree_selection=False,
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *_args, **_kwargs: None,
    )

    module.RlsRouteFrame._on_tree_select(subject)

    assert tree.selection() == ()
    assert subject._editor_dirty is True


def test_reorder_and_remove_refuse_uncommitted_editor_changes() -> None:
    module = _route_module()
    subject = SimpleNamespace(
        _require_committed_editor=lambda: False,
        _selected_index=lambda: pytest.fail(
            "selection must not be read after the edit guard refuses"
        ),
    )

    module.RlsRouteFrame._move_selected(subject, 1)
    module.RlsRouteFrame._remove_selected(subject)


def test_configuration_review_requires_every_imported_shelf_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    rows = [
        _row(module, shelf_id="a", review_state="confirmed"),
        _row(module, shelf_id="b", review_state="pending"),
    ]
    calls: list[object] = []
    warnings: list[tuple[str, str]] = []
    subject = SimpleNamespace(
        _rows=rows,
        _require_committed_editor=lambda: True,
        _selected_index=lambda: 0,
        _refresh_tree=lambda **kwargs: calls.append(("tree", kwargs)),
        _on_tree_select=lambda: calls.append("load"),
        _build_project=lambda **_kwargs: pytest.fail(
            "project build must remain gated"
        ),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda title, message, **_kwargs: warnings.append((title, message)),
    )

    module.RlsRouteFrame._review_selected_configuration(subject)

    assert calls == [("tree", {"select_id": "b"}), "load"]
    assert warnings[0][0] == "Shelf fact review required"
    assert "1 shelf remains pending" in warnings[0][1]


def test_configuration_review_requires_structured_raman_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    rows = [
        _row(module, shelf_id="a", review_state="confirmed"),
        _row(
            module,
            shelf_id="b",
            review_state="confirmed",
            source_evidence={
                "raman_callouts": [{"slot": 4, "port": 5}],
                "raman_callout_review": "pending",
            },
        ),
    ]
    calls: list[object] = []
    warnings: list[str] = []
    subject = SimpleNamespace(
        _rows=rows,
        _require_committed_editor=lambda: True,
        _selected_index=lambda: 0,
        _refresh_tree=lambda **kwargs: calls.append(("tree", kwargs)),
        _on_tree_select=lambda: calls.append("load"),
        _build_project=lambda **_kwargs: pytest.fail(
            "project build must remain gated"
        ),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda title, *_args, **_kwargs: warnings.append(title),
    )

    module.RlsRouteFrame._review_selected_configuration(subject)

    assert calls == [("tree", {"select_id": "b"}), "load"]
    assert warnings == ["RAMAN callout review required"]


def test_manual_shelf_can_enter_configuration_review() -> None:
    module = _route_module()
    row = _row(module, review_state="manual", source_evidence={})
    project = module.build_route_project(
        route_code="CHI",
        title="Manual route",
        revision="1",
        rows=(row,),
        require_valid=False,
    )
    opened: list[object] = []
    subject = SimpleNamespace(
        _rows=[row],
        _links=[],
        _require_committed_editor=lambda: True,
        _selected_index=lambda: 0,
        _build_project=lambda **_kwargs: project,
        _open_r4_0_review=lambda *args: opened.append(args),
    )

    module.RlsRouteFrame._review_selected_configuration(subject)

    assert len(opened) == 1
    assert opened[0][0] is row
    assert opened[0][1] is project


def test_configuration_review_requires_route_native_fiber_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    row = _row(module, review_state="confirmed")
    path = module.OpticalPath(
        path_id="path-1",
        path_role="route",
        fiber_type="LEAF",
        review_state="pending",
    )
    link = module.RouteLink(
        link_id="link-1",
        order=1,
        from_shelf_id="a",
        to_shelf_id="b",
        paths=(path,),
    )
    warnings: list[str] = []
    subject = SimpleNamespace(
        _rows=[row],
        _links=[link],
        _require_committed_editor=lambda: True,
        _selected_index=lambda: 0,
        _build_project=lambda **_kwargs: pytest.fail(
            "project build must remain fiber-gated"
        ),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda title, *_args, **_kwargs: warnings.append(title),
    )

    module.RlsRouteFrame._review_selected_configuration(subject)

    assert warnings == ["Route fiber type review required"]


def test_route_native_fiber_apply_updates_all_paths_and_invalidates_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    old_review = module.PathEndpointReview(
        shelf_id="a",
        link_name="old-link",
        expected_loss_db=12.5,
        fiber_type="NDSF",
    )
    links = [
        module.RouteLink(
            link_id=f"link-{order}",
            order=order,
            from_shelf_id=f"shelf-{order}",
            to_shelf_id=f"shelf-{order + 1}",
            paths=(
                module.OpticalPath(
                    path_id=f"path-{order}",
                    path_role="route",
                    fiber_type="LEAF",
                    review_state="corrected",
                    endpoint_reviews=(old_review,),
                    source_evidence={
                        "fields": [
                            {
                                "field": "fiber_type",
                                "normalized_value": "LEAF",
                                "confidence": 0.99,
                                "method": "vision",
                            }
                        ]
                    },
                ),
            ),
        )
        for order in (1, 2)
    ]
    rows = [
        _row(
            module,
            shelf_id="a",
            profile_payload={"schema_id": "provider.request"},
        ),
        _row(
            module,
            shelf_id="b",
            profile_payload={"schema_id": "provider.request"},
        ),
    ]
    calls: list[object] = []
    subject = SimpleNamespace(
        _rows=rows,
        _links=links,
        _route_fiber_type_var=SimpleNamespace(
            get=lambda: "Enhanced LEAF"
        ),
        _require_committed_editor=lambda: True,
        _committed_project_changed=lambda: calls.append("committed"),
        _sync_route_fiber_controls=lambda: calls.append("sync"),
        _refresh_tree=lambda: calls.append("tree"),
        _refresh_status=lambda message="": calls.append(message),
    )
    monkeypatch.setattr(
        module.messagebox,
        "askyesno",
        lambda *_args, **_kwargs: True,
    )

    applied = module.RlsRouteFrame._apply_route_native_fiber_type(subject)

    assert applied is True
    assert calls[:3] == ["committed", "sync", "tree"]
    assert all(not row.profile_payload for row in subject._rows)
    for link in subject._links:
        path = link.paths[0]
        assert path.fiber_type == "Enhanced LEAF"
        assert path.review_state == "pending"
        assert path.endpoint_reviews == ()
        assert path.source_evidence["fields"][0]["normalized_value"] == "LEAF"
        assert path.source_evidence["route_native_fiber_review"] == {
            "value": "Enhanced LEAF",
            "scope": "all_active_route_spans",
            "action": "operator_apply_route_native_fiber",
            "status": "confirmed",
            "deployable_cli": False,
        }


def test_exact_source_leaf_preselects_without_confirmation() -> None:
    module = _route_module()

    class Variable:
        def __init__(self) -> None:
            self.value = ""

        def set(self, value: str) -> None:
            self.value = value

    fiber_variable = Variable()
    observation_variable = Variable()
    subject = SimpleNamespace(
        _links=[
            module.RouteLink(
                link_id="link-1",
                order=1,
                from_shelf_id="a",
                to_shelf_id="b",
                paths=(
                    module.OpticalPath(
                        path_id="path-1",
                        path_role="route",
                        fiber_type="LEAF",
                    ),
                ),
            )
        ],
        _diagram_source={
            "route_fiber_type_scope_suggestion": {
                "value": "LEAF",
            }
        },
        _route_fiber_type_var=fiber_variable,
        _route_fiber_observation_var=observation_variable,
    )

    module.RlsRouteFrame._sync_route_fiber_controls(subject)

    assert fiber_variable.value == "LEAF"
    assert "Diagram fiber: LEAF" in observation_variable.value
    assert "Select and apply one native token" in observation_variable.value


def test_exact_supported_diagram_fiber_preselects_without_confirmation() -> None:
    module = _route_module()

    class Variable:
        def __init__(self) -> None:
            self.value = ""

        def set(self, value: str) -> None:
            self.value = value

    path = module.OpticalPath(
        path_id="path-1",
        path_role="route",
        fiber_type="NDSF",
        source_evidence={
            "fields": [
                {
                    "field": "fiber_type",
                    "normalized_value": "NDSF",
                    "confidence": 0.99,
                    "method": "vision",
                }
            ]
        },
    )
    links = [
        module.RouteLink(
            link_id="link-1",
            order=1,
            from_shelf_id="a",
            to_shelf_id="b",
            paths=(path,),
        )
    ]
    fiber_variable = Variable()
    observation_variable = Variable()
    subject = SimpleNamespace(
        _links=links,
        _diagram_source={},
        _route_fiber_type_var=fiber_variable,
        _route_fiber_observation_var=observation_variable,
    )

    module.RlsRouteFrame._sync_route_fiber_controls(subject)

    from utils.rls_config.route_project import route_native_fiber_review

    assert fiber_variable.value == "NDSF"
    assert route_native_fiber_review(links) == ("missing", "")
    assert "route_native_fiber_review" not in path.source_evidence
    assert "Select and apply one native token" in observation_variable.value


def test_preview_builds_current_snapshot_in_background_with_preview_purpose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _route_module()
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1 to SAT4",
        revision="1",
        rows=(_row(module),),
    )
    calls: dict[str, object] = {}

    def _submit(kind, work, **kwargs):
        calls["kind"] = kind
        calls["work"] = work
        calls["submit_kwargs"] = kwargs

    subject = SimpleNamespace(
        _require_committed_editor=lambda: True,
        _build_project=lambda **_kwargs: project,
        _submit_background=_submit,
        _refresh_status=lambda value="": calls.setdefault("status", value),
    )

    def _fake_export(current_project, output_path, *, purpose, diagram):
        calls["project"] = current_project
        calls["output_path"] = Path(output_path)
        calls["purpose"] = purpose
        calls["diagram"] = diagram
        return Path(output_path)

    monkeypatch.setattr(module, "export_mop", _fake_export)

    module.RlsRouteFrame._preview_mop(subject)
    output = calls["work"]()

    assert calls["kind"] == "preview"
    assert calls["project"] is project
    assert calls["purpose"] == "preview"
    assert calls["diagram"] is None
    assert Path(output).name == "ELP1-SAT4_MOP_PREVIEW.xlsx"
    assert calls["submit_kwargs"]["foreground"] is True
    assert calls["submit_kwargs"]["fingerprint"] == (
        module.route_project_fingerprint(project)
    )
    calls["submit_kwargs"]["context"].cleanup()


def test_bundle_export_requires_current_preview_and_runs_fail_closed_in_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _route_module()
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1 to SAT4",
        revision="1",
        rows=(_row(module),),
    )
    calls: dict[str, object] = {}
    preview = tmp_path / "current-preview.xlsx"
    preview.write_bytes(b"preview")
    fingerprint = module.route_project_fingerprint(project)

    def _submit(kind, work, **kwargs):
        calls["kind"] = kind
        calls["work"] = work
        calls["submit_kwargs"] = kwargs

    subject = SimpleNamespace(
        _require_committed_editor=lambda: True,
        _build_project=lambda **_kwargs: project,
        _preview_fingerprint=fingerprint,
        _preview_path=preview,
        _submit_background=_submit,
        _refresh_status=lambda value="": calls.setdefault("status", value),
    )
    bundle_dir = tmp_path / "ELP1-SAT4_RLS_route_deliverable"
    artifacts = {
        "project": bundle_dir / "route_project.json",
        "mop": bundle_dir / "route_FBN_MOP.xlsx",
        "validation": bundle_dir / "route_validation.txt",
        "manifest": bundle_dir / "route_manifest.json",
    }
    monkeypatch.setattr(
        module.filedialog,
        "askdirectory",
        lambda **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(module, "get_desktop_dir", lambda: tmp_path)
    monkeypatch.setattr(
        module.RouteProject,
        "deployment_readiness",
        lambda _project: module.DeploymentReadiness(
            ready=True,
            shelf_statuses=(),
        ),
    )

    def _fake_export(current_project, output_dir, *, diagram):
        calls["project"] = current_project
        calls["output_dir"] = Path(output_dir)
        calls["diagram"] = diagram
        return artifacts

    monkeypatch.setattr(module, "export_route_bundle", _fake_export)

    module.RlsRouteFrame._export_bundle(subject)
    result = calls["work"]()

    assert result is artifacts
    assert calls["kind"] == "bundle"
    assert calls["project"] is project
    assert calls["output_dir"] == tmp_path
    assert calls["diagram"] is None
    assert calls["submit_kwargs"]["foreground"] is True
    assert calls["submit_kwargs"]["fingerprint"] == fingerprint


def test_bundle_export_rejects_missing_or_stale_preview_before_directory_dialog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _route_module()
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="ELP1 to SAT4",
        revision="1",
        rows=(_row(module),),
    )
    warnings: list[tuple[tuple, dict]] = []
    subject = SimpleNamespace(
        _require_committed_editor=lambda: True,
        _build_project=lambda **_kwargs: project,
        _preview_fingerprint="stale-fingerprint",
        _preview_path=tmp_path / "missing-preview.xlsx",
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module.RouteProject,
        "deployment_readiness",
        lambda _project: module.DeploymentReadiness(
            ready=True,
            shelf_statuses=(),
        ),
    )
    monkeypatch.setattr(
        module.filedialog,
        "askdirectory",
        lambda **_kwargs: pytest.fail("directory dialog must remain gated"),
    )

    module.RlsRouteFrame._export_bundle(subject)

    assert warnings
    assert warnings[0][0][0] == "Current MOP preview required"


def test_bundle_export_preflight_blocks_before_preview_folder_or_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _route_module()
    project = module.build_route_project(
        route_code="ELP1-SAT4",
        title="Blocked route",
        revision="1",
        rows=(_row(module),),
    )
    warnings: list[tuple[tuple, dict]] = []
    subject = SimpleNamespace(
        _require_committed_editor=lambda: True,
        _build_project=lambda **_kwargs: project,
        _preview_fingerprint="",
        _preview_path=None,
        _submit_background=lambda *_args, **_kwargs: pytest.fail(
            "blocked preflight must not submit a worker"
        ),
    )
    monkeypatch.setattr(
        module.messagebox,
        "showwarning",
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )
    monkeypatch.setattr(
        module.filedialog,
        "askdirectory",
        lambda **_kwargs: pytest.fail(
            "blocked preflight must not open a folder dialog"
        ),
    )

    with caplog.at_level("WARNING"):
        module.RlsRouteFrame._export_bundle(subject)

    assert warnings
    assert warnings[0][0][0] == "Route bundle not ready"
    assert "exact R4.0 configuration reviews: 1" in warnings[0][0][1]
    assert "no background export was started" in warnings[0][0][1]
    assert "background_submitted=false" in caplog.text
    assert "artifacts_created=false" in caplog.text


def test_bundle_preflight_distinguishes_sra_provider_gap() -> None:
    module = _route_module()
    actions = module._bundle_preflight_actions(
        SimpleNamespace(links=()),
        SimpleNamespace(
            shelf_statuses=(
                SimpleNamespace(
                    ready=False,
                    reason_codes=(
                        "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE",
                    ),
                ),
            ),
        ),
    )

    assert actions == (
        (
            "SRA_PROVIDER_COVERAGE",
            "Add a vendor-audited SRA-capable R4.0 provider",
            1,
        ),
    )


def test_upload_requires_explicit_external_ai_privacy_confirmation_before_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _route_module()
    source = tmp_path / "customer-route.docx"
    source.write_bytes(b"test fixture")
    provider = object()
    sentinel = object()
    events: list[str] = []
    calls: dict[str, object] = {}

    def _confirm(*args, **_kwargs):
        title, text = args[:2]
        if title == "RAMAN slot/port convention":
            events.append("raman-confirmation")
            calls["raman_text"] = text
        else:
            events.append("privacy-confirmation")
            calls["privacy_text"] = text
        return True

    def _submit(kind, work, **kwargs):
        events.append("worker-submitted")
        calls.update(kind=kind, work=work, submit_kwargs=kwargs)

    def _fake_import(path, current_provider, *, conventions):
        events.append("provider-called")
        assert Path(path) == source
        assert current_provider is provider
        assert conventions.raman_callout_convention == (
            module.RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
        )
        return sentinel

    subject = SimpleNamespace(
        _rows=[],
        _require_committed_editor=lambda: True,
        _diagram_provider_factory=lambda: provider,
        _refresh_status=lambda *_args, **_kwargs: None,
        _submit_background=_submit,
    )
    monkeypatch.setattr(
        module.filedialog,
        "askopenfilename",
        lambda **_kwargs: str(source),
    )
    monkeypatch.setattr(module.messagebox, "askyesno", _confirm)
    monkeypatch.setattr(module, "import_route_diagram", _fake_import)

    module.RlsRouteFrame._upload_route_diagram(subject)

    assert events == [
        "raman-confirmation",
        "privacy-confirmation",
        "worker-submitted",
    ]
    assert "small red N/5 and N/6" in str(calls["raman_text"])
    assert "external AI vision provider" in str(calls["privacy_text"])
    assert source.name in str(calls["privacy_text"])
    assert calls["kind"] == "diagram_import"
    assert calls["submit_kwargs"]["foreground"] is True
    assert calls["work"]() is sentinel
    assert events[-1] == "provider-called"


def test_diagram_upload_preserves_uncommitted_empty_route_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    subject = SimpleNamespace(
        _rows=[],
        _require_committed_editor=lambda: False,
    )
    monkeypatch.setattr(
        module.filedialog,
        "askopenfilename",
        lambda **_kwargs: pytest.fail(
            "file selection must remain gated by uncommitted editor changes"
        ),
    )

    module.RlsRouteFrame._upload_route_diagram(subject)


def test_imported_gui_rows_keep_profile_payload_evidence_and_pending_separate():
    module = _route_module()
    provider_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": "1.0",
        "request": {"profile": "ila"},
    }
    evidence = {
        "schema_id": "atlas.ciena.rls.diagram-import-evidence",
        "source_sha256": "diagram-sha",
        "fields": [{"field": "tid", "confidence": 0.99}],
    }
    raw = {
        **_row(module, profile_id="ila").__dict__,
        "profile_payload": provider_payload,
        "source_evidence": evidence,
        "review_state": "confirmed",
    }
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (raw,),
    )

    rows = module._diagram_editor_rows(result)

    assert len(rows) == 1
    assert rows[0].review_state == "pending"
    assert dict(rows[0].profile_payload) == provider_payload
    assert dict(rows[0].source_evidence) == evidence


def test_imported_gui_rows_apply_r40_scope_only_when_release_is_unstated():
    module = _route_module()
    evidence = {
        "schema_id": "atlas.ciena.rls.diagram-import-evidence",
        "source_sha256": "diagram-sha",
        "fields": [],
    }
    blank_release = {
        **_row(module, profile_id="ila").__dict__,
        "software_release": "",
        "source_evidence": evidence,
    }
    explicit_r42 = {
        **_row(module, shelf_id="SHELF-002", profile_id="ila").__dict__,
        "software_release": "RLS R4.2",
        "source_evidence": evidence,
    }
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (blank_release, explicit_r42),
    )

    rows = module._diagram_editor_rows(result)

    assert rows[0].software_release == module.R40_UI_RELEASE
    assert rows[0].source_evidence["software_release_scope_default"] == {
        "value": module.R40_UI_RELEASE,
        "reason": "active Route Builder product contract; not diagram evidence",
    }
    assert rows[1].software_release == "RLS R4.2"
    assert "software_release_scope_default" not in rows[1].source_evidence
    assert "RLS R4.2" in module._r40_only_rows_error(rows)


def test_imported_gui_rows_default_power_without_overwriting_source_values():
    module = _route_module()
    evidence = {
        "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
        "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
        "source_sha256": "diagram-sha",
        "fields": [],
    }
    blank_ila = {
        **_row(
            module,
            shelf_id="SHELF-001",
            profile_id="ila",
            power_label="",
        ).__dict__,
        "source_evidence": evidence,
    }
    blank_roadm = {
        **_row(
            module,
            shelf_id="SHELF-002",
            profile_id="roadm_z",
            power_label="",
        ).__dict__,
        "source_evidence": evidence,
    }
    explicit_add_drop = {
        **_row(
            module,
            shelf_id="SHELF-003",
            profile_id="add_drop_a",
            power_label="Customer AC Feed A/B",
        ).__dict__,
        "source_evidence": {
            **evidence,
            "power_label_role_default": {
                "value": "DC",
                "profile_id": "ila",
                "reason": (
                    "ATLAS route-role power standard; not customer-diagram "
                    "evidence"
                ),
            },
        },
    }
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (blank_ila, blank_roadm, explicit_add_drop),
    )

    rows = module._diagram_editor_rows(result)

    assert rows[0].power_label == "DC"
    assert module._has_exact_power_label_role_default(
        rows[0].source_evidence,
        rows[0].profile_id,
        rows[0].power_label,
    )
    assert rows[1].power_label == "AC"
    assert module._has_exact_power_label_role_default(
        rows[1].source_evidence,
        rows[1].profile_id,
        rows[1].power_label,
    )
    assert rows[2].power_label == "Customer AC Feed A/B"
    assert "power_label_role_default" not in rows[2].source_evidence


def test_profile_change_updates_only_blank_or_atlas_default_power():
    module = _route_module()

    class Variable:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    atlas_power = SimpleNamespace(
        _selected_profile_id=lambda: "roadm",
        _release_var=Variable(""),
        _power_var=Variable("DC"),
        _editor_power_is_atlas_default=True,
        _loading_editor=False,
    )
    module.RlsRouteFrame._on_profile_selected(atlas_power)
    assert atlas_power._power_var.get() == "AC"
    assert atlas_power._editor_power_is_atlas_default is True

    explicit_power = SimpleNamespace(
        _selected_profile_id=lambda: "roadm",
        _release_var=Variable(""),
        _power_var=Variable("Operator feed"),
        _editor_power_is_atlas_default=False,
        _loading_editor=False,
    )
    module.RlsRouteFrame._on_profile_selected(explicit_power)
    assert explicit_power._power_var.get() == "Operator feed"
    assert explicit_power._editor_power_is_atlas_default is False

    blank_power = SimpleNamespace(
        _selected_profile_id=lambda: "ila",
        _release_var=Variable(""),
        _power_var=Variable(""),
        _editor_power_is_atlas_default=False,
        _loading_editor=False,
    )
    module.RlsRouteFrame._on_profile_selected(blank_power)
    assert blank_power._power_var.get() == "DC"
    assert blank_power._editor_power_is_atlas_default is True


def test_imported_gui_rows_offer_provenance_marked_site_code_suggestion():
    module = _route_module()
    evidence = {
        "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
        "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
        "fields": [],
    }
    suggested = {
        **_row(
            module,
            shelf_id="SHELF-001",
            site_key="site-1",
            site_code="",
            site_name="",
            tid="USELP1-L8R2",
        ).__dict__,
        "source_evidence": evidence,
    }
    explicit = {
        **_row(
            module,
            shelf_id="SHELF-002",
            site_key="site-explicit",
            site_code="USQTN1",
            tid="USQTN1-L8I2",
        ).__dict__,
        "source_evidence": evidence,
    }
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (suggested, explicit),
    )

    rows = module._diagram_editor_rows(result)

    assert rows[0].site_code == "USELP1"
    assert rows[0].site_key == "site-uselp1"
    assert rows[0].site_name == ""
    assert rows[0].source_evidence["site_code_review_suggestion"] == {
        "value": "USELP1",
        "source_field": "tid",
        "reason": (
            "review-only TID-prefix suggestion; not direct site-code diagram "
            "evidence"
        ),
    }
    assert rows[0].review_state == "pending"
    assert rows[1].site_code == "USQTN1"
    assert rows[1].site_key == "site-explicit"
    assert "site_code_review_suggestion" not in rows[1].source_evidence


def test_imported_gui_rows_mark_chassis_variant_fallback_as_review_only():
    module = _route_module()
    fallback = {
        **_row(
            module,
            shelf_id="SHELF-001",
            shelf_variant="R2 600mm",
        ).__dict__,
        "source_evidence": {
            "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
            "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
            "shelf_variant": None,
            "chassis": "R2 600mm",
            "fields": [],
        },
    }
    explicit = {
        **_row(
            module,
            shelf_id="SHELF-002",
            shelf_variant="K74-C894-900",
        ).__dict__,
        "source_evidence": {
            "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
            "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
            "shelf_variant": "K74-C894-900",
            "chassis": "R2 600mm",
            "fields": [],
        },
    }
    mismatched = {
        **_row(
            module,
            shelf_id="SHELF-003",
            shelf_variant="R4 600mm",
        ).__dict__,
        "source_evidence": {
            "schema_id": module.DIAGRAM_EVIDENCE_SCHEMA_ID,
            "schema_version": module.DIAGRAM_EVIDENCE_SCHEMA_VERSION,
            "shelf_variant": None,
            "chassis": "R2 600mm",
            "fields": [],
        },
    }
    result = SimpleNamespace(
        source=SimpleNamespace(sha256="diagram-sha"),
        gui_rows=lambda: (fallback, explicit, mismatched),
    )

    rows = module._diagram_editor_rows(result)

    assert rows[0].source_evidence["shelf_variant_chassis_suggestion"] == {
        "value": "R2 600mm",
        "source_field": "chassis",
        "reason": (
            "review-only chassis-family fallback; not an exact shelf variant "
            "or PEC"
        ),
    }
    assert (
        "shelf_variant_chassis_suggestion"
        not in rows[1].source_evidence
    )
    assert (
        "shelf_variant_chassis_suggestion"
        not in rows[2].source_evidence
    )


def test_background_executor_boundary_never_calls_tk() -> None:
    module = _route_module()
    result_queue = Queue()
    source = inspect.getsource(module._run_background_job)

    module._run_background_job(
        result_queue,
        kind="test",
        generation=7,
        fingerprint="abc",
        work=lambda: "done",
    )
    result = result_queue.get_nowait()

    assert result.value == "done"
    assert result.generation == 7
    assert result.fingerprint == "abc"
    assert "messagebox" not in source
    assert "filedialog" not in source
    assert ".after(" not in source
    assert "_status_var" not in source


def test_worker_fingerprint_and_generation_reject_stale_results() -> None:
    module = _route_module()
    project = module.build_route_project(
        route_code="CHI",
        title="Chicago route",
        revision="1",
        rows=(_row(module),),
    )
    fingerprint = module.route_project_fingerprint(project)
    subject = SimpleNamespace(
        _job_generation=4,
        _build_project=lambda **_kwargs: project,
    )

    current = module._WorkerResult("config", 4, fingerprint)
    old_generation = module._WorkerResult("config", 3, fingerprint)
    old_snapshot = module._WorkerResult("config", 4, "0" * 64)

    assert module.RlsRouteFrame._worker_result_is_current(subject, current)
    assert not module.RlsRouteFrame._worker_result_is_current(
        subject, old_generation
    )
    assert not module.RlsRouteFrame._worker_result_is_current(
        subject, old_snapshot
    )


def test_config_evaluation_labels_pending_sra_pair_without_cli_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    row = _row(
        module,
        profile_payload={
            "schema_id": "ciena.rls.r4-0-exact-request",
            "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        },
    )
    status = SimpleNamespace(
        shelf_id=row.shelf_id,
        ready=False,
        reason_codes=("R40_PENDING_SRA_PEER_REVIEW",),
    )
    result = SimpleNamespace(
        ready=False,
        config_count=0,
        readiness=SimpleNamespace(shelf_statuses=(status,)),
    )
    statuses: list[str] = []
    subject = SimpleNamespace(
        _rows=[row],
        _links=[],
        _config_labels={},
        _update_tree_readiness_cells=lambda: None,
        _refresh_status=lambda message="": statuses.append(message),
    )
    monkeypatch.setattr(
        module,
        "_log_route_event",
        lambda *_args, **_kwargs: None,
    )

    module.RlsRouteFrame._apply_config_evaluation(subject, result)

    assert subject._config_labels == {
        row.shelf_id: "Paired SRA peer review pending — CLI blocked"
    }
    assert statuses == [
        (
            "Configuration deployment readiness: 0 ready, 1 blocked. "
            "Shelves reviewed 1/1; exact configuration reviews 1/1; "
            "A→Z/Z→A propagation reviews 0/0; physical spans reviewed "
            "0/0."
        )
    ]


def test_config_advisory_logs_deployment_readiness_reasons_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    row = _row(
        module,
        profile_id="ila",
        profile_payload={
            "schema_id": "ciena.rls.r4-0-exact-request",
            "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        },
    )

    class FakeTree:
        def __init__(self):
            self.values = {
                row.shelf_id: (
                    1,
                    "ILA",
                    row.site_code,
                    row.tid,
                    row.primary_oam_ip,
                row.software_release,
                    row.raman_label,
                    row.power_label,
                    "old provider",
                    "old direction",
                    "old readiness",
                )
            }
            self.selected = (row.shelf_id,)

        def exists(self, item):
            return item in self.values

        def item(self, item, option=None, **kwargs):
            if "values" in kwargs:
                self.values[item] = tuple(kwargs["values"])
            if option == "values":
                return self.values[item]
            return {"values": self.values[item]}

        def selection(self):
            return self.selected

    tree = FakeTree()
    editor_tid = SimpleNamespace(value=row.tid)
    status = SimpleNamespace(
        shelf_id=row.shelf_id,
        ready=False,
        reason_codes=("R40_EXACT_GENERATOR_VALIDATION_FAILED",),
    )
    result = SimpleNamespace(
        ready=False,
        readiness=SimpleNamespace(shelf_statuses=(status,)),
    )
    status_messages: list[str] = []
    log_messages: list[str] = []
    subject = SimpleNamespace(
        _rows=[row],
        _tree=tree,
        _config_labels={},
        _tid_var=editor_tid,
        _update_tree_readiness_cells=lambda: (
            module.RlsRouteFrame._update_tree_readiness_cells(subject)
        ),
        _refresh_status=lambda message="": status_messages.append(message),
    )
    monkeypatch.setattr(
        module,
        "_log_route_event",
        lambda message, *_args, **_kwargs: log_messages.append(message),
    )

    module.RlsRouteFrame._apply_config_evaluation(subject, result)

    assert tree.selection() == (row.shelf_id,)
    assert editor_tid.value == row.tid
    assert tree.values[row.shelf_id][-1] == "Config validation blocked"
    assert status_messages == [
        (
            "Configuration deployment readiness: 0 ready, 1 blocked. "
            "Shelves reviewed 1/1; exact configuration reviews 1/1; "
            "A→Z/Z→A propagation reviews 0/0; physical spans reviewed "
            "0/0."
        )
    ]
    assert log_messages == [
        (
            "Configuration deployment-readiness assessment completed: "
            "ready_shelves=0, blocked_shelves=1, "
            "reviewed_shelves=1/1, exact_payloads=1/1, "
            "reviewed_propagation_paths=0/0, "
            "reviewed_physical_spans=0/0, candidate_configs=0, "
            "reason_codes=R40_EXACT_GENERATOR_VALIDATION_FAILED:1."
        )
    ]


def test_project_save_and_open_use_draft_safe_model_paths() -> None:
    source = _source()

    assert "save_route_project_draft" in source
    assert "load_route_project_draft" in source
    save_method = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_save_project")
    )
    open_method = ast.get_source_segment(
        source, _method("RlsRouteFrame", "_open_project")
    )
    assert save_method is not None and "save_route_project_draft" in save_method
    assert open_method is not None and "load_route_project_draft" in open_method


def test_route_table_includes_order_and_readiness_columns() -> None:
    tree = _tree()
    table_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_TABLE_COLUMNS"
            for target in node.targets
        )
    )
    assert isinstance(table_assignment.value, ast.Tuple)
    columns = {
        item.value
        for item in table_assignment.value.elts
        if isinstance(item, ast.Constant)
    }
    assert {
        "order",
        "profile",
        "site",
        "tid",
        "ip",
        "release",
        "raman",
        "power",
        "provider",
        "direction",
        "readiness",
    } == columns


def test_provider_glance_distinguishes_suggestion_candidate_and_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    from utils.rls_config.r4_0_generator import (
        R40_PROVIDER_CATALOG,
        provider_profiles_for_role,
    )

    ila_profile = provider_profiles_for_role("ila")[0]
    roadm_profiles = provider_profiles_for_role("roadm")
    project = SimpleNamespace()
    shelf = SimpleNamespace(
        profile_id="ila",
        profile_payload={},
        source_evidence={},
        software_release="RLS R4.0",
        tid="CHI-ILA-01",
        primary_oam_ip="192.0.2.10",
    )
    resolution = {
        "status": "unique_candidate",
        "provider_id": ila_profile.provider_id,
        "review_provider_ids": [ila_profile.provider_id],
        "preselect_allowed": True,
    }
    monkeypatch.setattr(
        module,
        "_r40_provider_route_band_mismatches",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        module,
        "_r4_0_provider_prepopulation",
        lambda *_args: (dict(resolution), {}),
    )

    assert module.r40_provider_glance_label(project, shelf) == (
        f"Suggested — {ila_profile.display_name}"
    )

    resolution["preselect_allowed"] = False
    assert module.r40_provider_glance_label(project, shelf) == (
        f"Candidate — {ila_profile.display_name}"
    )

    resolution["status"] = "conflict"
    assert (
        module.r40_provider_glance_label(project, shelf)
        == "Provider conflict — review required"
    )
    assert ila_profile.display_name not in (
        module.r40_provider_glance_label(project, shelf)
    )

    shelf.profile_id = "roadm"
    resolution.update(
        status="ambiguous",
        provider_id="",
        review_provider_ids=[
            profile.provider_id for profile in roadm_profiles
        ],
    )
    assert (
        module.r40_provider_glance_label(project, shelf)
        == f"Select provider ({len(roadm_profiles)} compatible)"
    )

    resolution["review_provider_ids"] = ["not-in-the-audited-catalog"]
    assert (
        module.r40_provider_glance_label(project, shelf)
        == "No compatible provider"
    )
    assert "not-in-the-audited-catalog" not in R40_PROVIDER_CATALOG


def test_provider_glance_gates_sra_and_stale_or_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    monkeypatch.setattr(
        module,
        "_r4_0_provider_prepopulation",
        lambda *_args: (
            {
                "provider_id": "",
                "review_provider_ids": [],
                "preselect_allowed": False,
            },
            {},
        ),
    )
    project = SimpleNamespace()
    shelf = SimpleNamespace(
        profile_id="ila",
        profile_payload={},
        source_evidence={
            "raman_callouts": [{"slot": 4, "port": 5}],
            "raman_callout_review": "accepted",
        },
        software_release="RLS R4.0",
        tid="CHI-ILA-01",
        primary_oam_ip="192.0.2.10",
    )

    assert (
        module.r40_provider_glance_label(project, shelf)
        == "SRA provider required"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Blocked — SRA provider required"
    )

    shelf.source_evidence["raman_callout_review"] = "pending"
    assert (
        module.r40_provider_glance_label(project, shelf)
        == "SRA review pending"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Blocked — SRA review pending"
    )

    shelf.source_evidence = {}
    shelf.profile_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": "retired",
    }
    assert module.r40_provider_glance_label(project, shelf) == (
        "Stale provider review — re-review"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Stale direction review — re-review"
    )

    shelf.profile_payload = {
        "schema_id": "ciena.rls.r4-0-exact-request",
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
    }
    assert module.r40_provider_glance_label(project, shelf) == (
        "Invalid provider review — re-review"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Invalid direction review — re-review"
    )


def test_provider_and_direction_glance_mark_either_staged_sra_endpoint() -> None:
    module = _route_module()
    from tests.test_rls_r4_0_route_integration import _sra_pair_project
    from utils.rls_config.r4_0_generator import R40_PROVIDER_CATALOG

    complete = _sra_pair_project()
    expected_mappings = (
        "P1→Z / P2→A",
        "D1→A (both flows)",
    )

    for staged_index, missing_index in ((0, 1), (1, 0)):
        shelves = list(complete.shelves)
        shelves[missing_index] = replace(
            shelves[missing_index],
            profile_payload={},
        )
        project = replace(complete, shelves=tuple(shelves))
        staged = project.shelves[staged_index]
        provider_id = staged.profile_payload["request"]["provider_id"]
        profile = R40_PROVIDER_CATALOG[provider_id]

        assert module._r40_shelf_glance_labels(
            project,
            staged,
        ) == (
            f"Staged — {profile.display_name} (SRA peer pending)",
            (
                f"Staged — {expected_mappings[staged_index]} "
                "(SRA peer pending)"
            ),
        )


def test_provider_glance_marks_only_valid_current_request_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    generator = importlib.import_module(
        "utils.rls_config.r4_0_generator"
    )
    profile = generator.provider_profiles_for_role("ila")[0]
    project = SimpleNamespace()
    shelf = SimpleNamespace(
        profile_id="ila",
        profile_payload={
            "schema_id": "ciena.rls.r4-0-exact-request",
            "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        },
        source_evidence={},
        software_release="RLS R4.0",
        tid="CHI-ILA-01",
        primary_oam_ip="198.51.100.10",
    )
    request = SimpleNamespace(
        provider_id=profile.provider_id,
        profile="ila",
        software_release=shelf.software_release,
        shelf_name=shelf.tid,
        loopback_ip=shelf.primary_oam_ip,
        line_1_route_side="Z",
    )

    class _AcceptingGenerator:
        @staticmethod
        def validate(_request):
            return ()

    monkeypatch.setattr(
        generator,
        "decode_r40_exact_payload",
        lambda _payload: request,
    )
    monkeypatch.setattr(
        generator,
        "R40ExactConfigGenerator",
        _AcceptingGenerator,
    )
    monkeypatch.setattr(
        module,
        "_r40_provider_route_band_mismatches",
        lambda *_args: [],
    )

    assert module.r40_provider_glance_label(project, shelf) == (
        f"Applied — {profile.display_name}"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Applied — P1→Z / P2→A"
    )


def test_provider_glance_treats_deferred_terminal_colan_warning_as_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    generator = importlib.import_module(
        "utils.rls_config.r4_0_generator"
    )
    profile = next(
        candidate
        for candidate in generator.provider_profiles_for_role("roadm_a")
        if len(candidate.line_outputs) == 1
    )
    project = SimpleNamespace()
    shelf = SimpleNamespace(
        profile_id="roadm_a",
        profile_payload={
            "schema_id": "ciena.rls.r4-0-exact-request",
            "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        },
        source_evidence={},
        software_release="RLS R4.0",
        tid="TERM-A",
        primary_oam_ip="198.51.100.10",
    )
    request = SimpleNamespace(
        provider_id=profile.provider_id,
        profile=shelf.profile_id,
        software_release=shelf.software_release,
        shelf_name=shelf.tid,
        loopback_ip=shelf.primary_oam_ip,
        line_1_route_side="Z",
    )

    class _WarningGenerator:
        @staticmethod
        def validate(_request):
            return (
                SimpleNamespace(
                    severity="warning",
                    code="TERMINAL_COLAN_DEFERRED",
                ),
            )

    monkeypatch.setattr(
        generator,
        "decode_r40_exact_payload",
        lambda _payload: request,
    )
    monkeypatch.setattr(
        generator,
        "R40ExactConfigGenerator",
        _WarningGenerator,
    )
    monkeypatch.setattr(
        module,
        "_r40_provider_route_band_mismatches",
        lambda *_args: [],
    )

    assert module.r40_provider_glance_label(project, shelf) == (
        f"Applied — {profile.display_name}"
    )
    assert module.r40_direction_glance_label(project, shelf) == (
        "Applied — D1→Z (both flows)"
    )


def test_provider_glance_rejects_sra_payload_with_wrong_reviewed_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    generator = importlib.import_module(
        "utils.rls_config.r4_0_generator"
    )
    profile = next(
        candidate
        for candidate in generator.provider_profiles_for_role("ila")
        if candidate.supports_raman
    )
    shelf = SimpleNamespace(
        profile_id="ila",
        profile_payload={
            "schema_id": "ciena.rls.r4-0-exact-request",
            "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        },
        source_evidence=_sra_source_evidence(
            "CHI-ILA-01",
            6,
            [],
        ),
        raman_label="Slot 6",
        software_release="RLS R4.0",
        tid="CHI-ILA-01",
        primary_oam_ip="198.51.100.10",
    )
    request = SimpleNamespace(
        provider_id=profile.provider_id,
        profile="ila",
        software_release=shelf.software_release,
        shelf_name=shelf.tid,
        loopback_ip=shelf.primary_oam_ip,
        line_1_route_side="Z",
    )

    monkeypatch.setattr(
        generator,
        "decode_r40_exact_payload",
        lambda _payload: request,
    )
    monkeypatch.setattr(
        module,
        "_r40_provider_route_band_mismatches",
        lambda *_args: [],
    )

    assert module.r40_provider_glance_label(
        SimpleNamespace(),
        shelf,
    ) == "SRA evidence mismatch — re-review"
    assert module.r40_direction_glance_label(
        SimpleNamespace(),
        shelf,
    ) == "Blocked — SRA evidence mismatch"


def test_direction_glance_distinguishes_direct_derived_and_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    from utils.rls_config.r4_0_generator import provider_profiles_for_role

    profile = provider_profiles_for_role("ila")[0]
    project = SimpleNamespace()
    shelf = SimpleNamespace(
        profile_id="ila",
        profile_payload={},
        source_evidence={},
        software_release="RLS R4.0",
        tid="CHI-ILA-01",
        primary_oam_ip="192.0.2.10",
    )
    provider_resolution = {
        "status": "unique_candidate",
        "provider_id": profile.provider_id,
        "review_provider_ids": [profile.provider_id],
        "preselect_allowed": False,
        "reason_codes": ["UNIQUE_COMPATIBLE_PROVIDER_CANDIDATE"],
        "raman_callout_review_status": "not_applicable",
    }
    direction_resolution = {
        "status": "exact_match",
        "line_1_route_side": "Z",
    }
    monkeypatch.setattr(
        module,
        "_r40_provider_route_band_mismatches",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        module,
        "_r4_0_provider_prepopulation",
        lambda *_args: (
            dict(provider_resolution),
            dict(direction_resolution),
        ),
    )

    assert module.r40_direction_glance_label(project, shelf) == (
        "Direct — P1→Z / P2→A"
    )

    direction_resolution["status"] = "controlled_fallback"
    assert module.r40_direction_glance_label(project, shelf) == (
        "Derived — P1→Z / P2→A"
    )

    direction_resolution["status"] = "ambiguous"
    direction_resolution["line_1_route_side"] = ""
    assert module.r40_direction_glance_label(project, shelf) == (
        "Blocked — direction ambiguous"
    )

    provider_resolution["status"] = "conflict"
    provider_resolution["provider_id"] = ""
    assert module.r40_direction_glance_label(project, shelf) == (
        "Blocked — provider conflict"
    )


def test_direction_mapping_respects_provider_record_cardinality() -> None:
    module = _route_module()
    from utils.rls_config.r4_0_generator import R40_PROVIDER_CATALOG

    one_degree = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if (
            profile.line_semantics == "bidirectional_degree"
            and len(profile.line_outputs) == 1
        )
    )
    two_degree = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if (
            profile.line_semantics == "bidirectional_degree"
            and len(profile.line_outputs) == 2
        )
    )
    dle = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.line_semantics == "unidirectional_amplifier_path"
    )

    assert module._r40_direction_mapping_text(one_degree, "Z") == (
        "D1→Z (both flows)"
    )
    assert "D2" not in module._r40_direction_mapping_text(one_degree, "Z")
    assert module._r40_direction_mapping_text(two_degree, "A") == (
        "D1→A / D2→Z"
    )
    assert module._r40_direction_mapping_text(dle, "Z") == (
        "P1→Z / P2→A"
    )


def test_refresh_tree_builds_project_once_for_provider_and_direction_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _route_module()
    rows = [
        _row(module, shelf_id="shelf-1"),
        _row(module, shelf_id="shelf-2", tid="CHI-ILA-02"),
    ]
    project_shelves = tuple(
        SimpleNamespace(shelf_id=row.shelf_id) for row in rows
    )
    project = SimpleNamespace(shelves=project_shelves)
    build_calls: list[bool] = []
    glance_calls: list[str] = []

    class _Tree:
        def __init__(self) -> None:
            self.values: dict[str, tuple[object, ...]] = {}

        @staticmethod
        def selection():
            return ()

        def get_children(self):
            return tuple(self.values)

        def delete(self, *items):
            for item in items:
                self.values.pop(item, None)

        def insert(self, _parent, _position, *, iid, values):
            self.values[iid] = tuple(values)

        def exists(self, iid):
            return iid in self.values

        @staticmethod
        def selection_set(_iid):
            return None

        @staticmethod
        def focus(_iid):
            return None

        @staticmethod
        def see(_iid):
            return None

    tree = _Tree()
    subject = SimpleNamespace(
        _rows=rows,
        _tree=tree,
        _config_labels={},
        _build_project=lambda *, require_valid: (
            build_calls.append(require_valid) or project
        ),
    )
    monkeypatch.setattr(
        module,
        "_r40_shelf_glance_labels",
        lambda _project, shelf: (
            glance_calls.append(shelf.shelf_id)
            or (
                f"Candidate — {shelf.shelf_id}",
                f"Derived — {shelf.shelf_id}",
            )
        ),
    )

    module.RlsRouteFrame._refresh_tree(subject)

    assert build_calls == [False]
    assert glance_calls == ["shelf-1", "shelf-2"]
    provider_index = module._TABLE_COLUMNS.index("provider")
    direction_index = module._TABLE_COLUMNS.index("direction")
    assert tree.values["shelf-1"][provider_index] == (
        "Candidate — shelf-1"
    )
    assert tree.values["shelf-1"][direction_index] == (
        "Derived — shelf-1"
    )
    assert tree.values["shelf-2"][provider_index] == (
        "Candidate — shelf-2"
    )
    assert tree.values["shelf-2"][direction_index] == (
        "Derived — shelf-2"
    )
    assert module._TABLE_COLUMNS[-1] == "readiness"
