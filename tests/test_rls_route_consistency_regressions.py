"""Regressions for route-relative diagram provenance and deliverable binding."""

from __future__ import annotations

from types import SimpleNamespace
import zipfile

import pytest

from tests.test_rls_mop_export import (
    _cell_text,
    _workbook_diagram,
    _xml,
)
from tests.test_rls_route_gui import _route_module, _row
from utils.rls_config.diagram_assets import (
    DiagramAssetError,
    validate_workbook_diagram_for_project,
)
from utils.rls_config.mop_export import IRM_PART, export_mop


_SOURCE_SHA256 = (
    "d797ba84cb942b60d082c31bf44c63e09c508a4d3f7ac28d995f7ef352226c13"
)


class _Variable:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


def _line_endpoint(
    index: int,
    adjacency: str,
    line_out_port: int,
) -> dict[str, object]:
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


def _ila_source_evidence() -> dict[str, object]:
    return {
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
            _line_endpoint(0, "preceding", 53),
            _line_endpoint(1, "following", 63),
        ],
    }


def _terminal_title_derivation() -> dict[str, object]:
    return {
        "rule_id": "terminal-site-route-title-v1",
        "status": "controlled_derivation",
        "value": "ELP1-SAT4",
        "provider_title": "USELP1-USSAT4",
        "observed_header_pair": "USELP1-USSAT4",
        "endpoint_tids": ["USELP1-L8R2", "USSAT4-L8R3"],
        "endpoint_codes": ["USELP1", "USSAT4"],
        "display_codes": ["ELP1", "SAT4"],
        "removed_shared_prefix": "US",
        "source_sha256": _SOURCE_SHA256,
        "deployable_cli": False,
    }


def _diagram_source() -> dict[str, object]:
    return {
        "source_sha256": _SOURCE_SHA256,
        "route_title_derivation": _terminal_title_derivation(),
        "route_header": {
            "optical_band": "c+l",
            "optical_band_status": "direct_supported",
        },
    }


def _route_rows(module):
    return [
        _row(
            module,
            shelf_id="a",
            profile_id="roadm_a",
            site_key="site-a",
            site_code="USELP1",
            site_name="El Paso",
            tid="USELP1-L8R2",
            primary_oam_ip="192.0.2.10",
            source_evidence={},
        ),
        _row(
            module,
            shelf_id="ila",
            profile_id="ila",
            site_key="site-mid",
            site_code="USQTN1",
            site_name="Tornillo",
            tid="USQTN1-L8I2",
            primary_oam_ip="192.0.2.11",
            shelf_variant="R2 600mm",
            source_evidence=_ila_source_evidence(),
        ),
        _row(
            module,
            shelf_id="z",
            profile_id="roadm_z",
            site_key="site-z",
            site_code="USSAT4",
            site_name="San Antonio",
            tid="USSAT4-L8R3",
            primary_oam_ip="192.0.2.12",
            source_evidence={},
        ),
    ]


def _reverse_route_through_editor(module, rows):
    selected_id = {"value": "z"}
    title = _Variable("ELP1-SAT4")
    subject = SimpleNamespace(
        _rows=list(rows),
        _links=[],
        _links_populated=False,
        _title_var=title,
        _route_code_var=_Variable("RL-0037805"),
        _diagram_source=_diagram_source(),
        _require_committed_editor=lambda: True,
        _selected_index=lambda: next(
            index
            for index, row in enumerate(subject._rows)
            if row.shelf_id == selected_id["value"]
        ),
        _committed_project_changed=lambda: None,
        _sync_route_fiber_controls=lambda: None,
        _refresh_tree=lambda **_kwargs: None,
        _refresh_status=lambda _message="": None,
    )

    module.RlsRouteFrame._move_selected(subject, -1)
    module.RlsRouteFrame._move_selected(subject, -1)
    selected_id["value"] = "ila"
    module.RlsRouteFrame._move_selected(subject, -1)

    assert [row.shelf_id for row in subject._rows] == ["z", "ila", "a"]
    return subject


def _build_project(module, rows, *, title: str, diagram_source):
    return module.build_route_project(
        route_code="RL-0037805",
        title=title,
        revision="1",
        ospf_area="10.6.8.0",
        rows=rows,
        diagram_source=diagram_source,
        require_valid=False,
    )


def test_route_reversal_invalidates_imported_fixed_direction_evidence() -> None:
    module = _route_module()
    subject = _reverse_route_through_editor(module, _route_rows(module))
    project = _build_project(
        module,
        subject._rows,
        title=subject._title_var.get(),
        diagram_source=subject._diagram_source,
    )

    seed = module._r4_0_editor_seed(project, "ila")

    assert seed["direction_resolution"]["status"] != "exact_match"
    assert seed["line_1_route_side"] == ""


def test_operator_hardware_correction_disables_diagram_provider_preselection() -> None:
    module = _route_module()
    rows = _route_rows(module)
    rows[1] = _row(
        module,
        shelf_id="ila",
        profile_id="ila",
        site_key="site-mid",
        site_code="USQTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        primary_oam_ip="192.0.2.11",
        shelf_variant="R4 600mm",
        review_state="corrected",
        source_evidence=_ila_source_evidence(),
    )
    project = _build_project(
        module,
        rows,
        title="ELP1-SAT4",
        diagram_source=_diagram_source(),
    )

    seed = module._r4_0_editor_seed(project, "ila")

    assert seed["provider_resolution"]["preselect_allowed"] is False


def test_specific_stale_pec_wins_over_matching_generic_chassis_on_correction() -> None:
    module = _route_module()
    rows = _route_rows(module)
    evidence = _ila_source_evidence()
    evidence["shelf_variant"] = "NTK803DA"
    evidence["fields"].append(
        {
            "field": "shelf_variant",
            "normalized_value": "NTK803DA",
            "confidence": 0.99,
            "method": "vision",
        }
    )
    rows[1] = _row(
        module,
        shelf_id="ila",
        profile_id="ila",
        site_key="site-mid",
        site_code="USQTN1",
        site_name="Tornillo",
        tid="USQTN1-L8I2",
        primary_oam_ip="192.0.2.11",
        shelf_variant="R2 600mm",
        review_state="corrected",
        source_evidence=evidence,
    )
    project = _build_project(
        module,
        rows,
        title="ELP1-SAT4",
        diagram_source=_diagram_source(),
    )

    seed = module._r4_0_editor_seed(project, "ila")

    assert seed["provider_resolution"]["preselect_allowed"] is False
    assert seed["provider_resolution"]["reason_codes"] == [
        "OPERATOR_PROVIDER_FACT_CORRECTION_REQUIRES_REVIEW"
    ]


def test_reversed_terminals_keep_controlled_title_and_mop_a_z_consistent(
    tmp_path,
) -> None:
    module = _route_module()
    subject = _reverse_route_through_editor(module, _route_rows(module))
    project = _build_project(
        module,
        subject._rows,
        title=subject._title_var.get(),
        diagram_source=subject._diagram_source,
    )
    output = tmp_path / "reversed-route-preview.xlsx"

    export_mop(project, output, purpose="preview")

    endpoint_a, endpoint_z = project.endpoint_sites()
    with zipfile.ZipFile(output) as workbook:
        irm = _xml(workbook, IRM_PART)
        mop_values = (
            _cell_text(irm, "B2"),
            _cell_text(irm, "B3"),
            _cell_text(irm, "B5"),
        )

    assert endpoint_a is not None
    assert endpoint_z is not None
    assert (
        project.title,
        endpoint_a.code,
        endpoint_z.code,
        *mop_values,
    ) == (
        "SAT4-ELP1",
        "USSAT4",
        "USELP1",
        "SAT4-ELP1",
        "SAT4",
        "ELP1",
    )


def test_workbook_diagram_requires_matching_project_marker() -> None:
    diagram = _workbook_diagram()
    project_without_marker = {
        "diagram_source": {
            "file_name": diagram.source_file_name,
            "source_type": diagram.source_type,
            "source_sha256": diagram.source_sha256,
        }
    }

    with pytest.raises(DiagramAssetError):
        validate_workbook_diagram_for_project(
            project_without_marker,
            diagram,
        )
