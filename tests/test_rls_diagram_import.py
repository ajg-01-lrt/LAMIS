from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
import warnings
import zipfile

from PIL import Image
import pytest

from utils.rls_config.diagram_import import (
    DIAGRAM_EVIDENCE_SCHEMA_VERSION,
    DIAGRAM_EVIDENCE_SCHEMA_ID,
    RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT,
    DiagramImportConventions,
    DiagramImportError,
    DiagramImportLimits,
    PROVIDER_SCHEMA,
    PROVIDER_RESPONSE_NAME,
    import_route_diagram,
    load_diagram_source,
    parse_provider_result,
)


def _png_bytes(
    color: tuple[int, int, int] = (20, 40, 60),
    size: tuple[int, int] = (32, 16),
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _pattern_png_bytes(size: tuple[int, int]) -> bytes:
    width, height = size
    image = Image.new("RGB", size, (10, 20, 30))
    image.paste((180, 20, 30), (width // 2, 0, width, height // 2))
    image.paste((20, 180, 30), (0, height // 2, width // 2, height))
    image.paste(
        (20, 30, 180),
        (width // 2, height // 2, width, height),
    )
    output = BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 8), (120, 80, 40)).save(output, format="JPEG")
    return output.getvalue()


def _document_xml(relation_ids: list[str], *, linked: bool = False) -> bytes:
    attribute = "link" if linked else "embed"
    blips = "".join(
        f'<w:r><w:drawing><a:blip r:{attribute}="{relation_id}"/>'
        "</w:drawing></w:r>"
        for relation_id in relation_ids
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships"><w:body><w:p>'
        f"{blips}</w:p></w:body></w:document>"
    ).encode()


def _rels_xml(
    relations: list[tuple[str, str]],
    *,
    external: bool = False,
) -> bytes:
    rows = []
    for relation_id, target in relations:
        mode = ' TargetMode="External"' if external else ""
        rows.append(
            '<Relationship '
            f'Id="{relation_id}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            f'relationships/image" Target="{target}"{mode}/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        f'2006/relationships">{"".join(rows)}</Relationships>'
    ).encode()


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
).encode()


def _write_docx(
    path: Path,
    *,
    document: bytes,
    relationships: bytes,
    images: dict[str, bytes],
    extras: dict[str, bytes] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        for name, data in images.items():
            archive.writestr(name, data)
        for name, data in (extras or {}).items():
            archive.writestr(name, data)


def _ev(
    field: str,
    value: object,
    *,
    confidence: float = 0.99,
    image_index: int = 0,
    method: str = "vision",
) -> dict[str, object]:
    return {
        "field": field,
        "raw_text": "" if value is None else str(value),
        "normalized_value": None if value is None else str(value),
        "confidence": confidence,
        "image_index": image_index,
        "bbox": [0.05, 0.05, 0.2, 0.1],
        "method": method,
    }


def _raman_callout(
    slot: int,
    port: int,
    *,
    shelf_tid: str | None,
    context: str,
    confidence: float = 0.99,
    include_association: bool = True,
    image_index: int = 0,
) -> dict[str, object]:
    label = f"{slot}/{port}"
    evidence = [
        _ev(
            "slot_port",
            label,
            confidence=confidence,
            image_index=image_index,
            method="vision",
        ),
        _ev(
            "context",
            context,
            confidence=confidence,
            image_index=image_index,
            method="inferred",
        ),
    ]
    if shelf_tid is not None and include_association:
        evidence.append(
            _ev(
                "shelf_association",
                shelf_tid,
                confidence=confidence,
                image_index=image_index,
                method="inferred",
            )
        )
    return {
        "raw_text": label,
        "slot": slot,
        "port": port,
        "shelf_tid": shelf_tid,
        "context": context,
        "evidence": evidence,
    }


def _shelf(
    order: int,
    tid: str,
    ip: str,
    site: str,
    family: str,
    lifecycle: str = "active",
) -> dict[str, object]:
    values: dict[str, object] = {
        "order": order,
        "tid": tid,
        "primary_oam_ip": ip,
        "site_code": site,
        "site_name": f"{site} site",
        "site_address": None,
        "network_site_id": None,
        "profile_family": family,
        "chassis": "R4 600mm" if family == "roadm" else "R2 600mm",
        "software_release": "RLS R4.0",
        "shelf_variant": "K74-C890-900" if family == "roadm" else "K74-C894-900",
        "topology": None,
        "band": None,
        "add_drop_structure": None,
        "protection_type": None,
        "module_inventory": [],
        "power_label": "A+B",
        "raman_label": "NONE",
        "lifecycle": lifecycle,
        "notes": "remove and bypass" if lifecycle == "planned_remove" else None,
    }
    evidence_fields = (
        "tid",
        "primary_oam_ip",
        "site_code",
        "site_name",
        "profile_family",
        "chassis",
        "software_release",
        "shelf_variant",
        "power_label",
        "raman_label",
    )
    values["evidence"] = [_ev(field, values[field]) for field in evidence_fields]
    return values


def _span(
    order: int,
    left: str,
    right: str,
) -> dict[str, object]:
    values: dict[str, object] = {
        "order": order,
        "from_tid": left,
        "to_tid": right,
        "expected_loss_db": 14.5 + order,
        "distance_km": 60.0 + order,
        "circuit_id": "BDJW7353",
        "fiber_start": 9,
        "fiber_end": 10,
        "fiber_type": "LEAF",
        "lifecycle": "active",
        "notes": None,
    }
    values["evidence"] = [
        _ev(field, values[field])
        for field in (
            "from_tid",
            "to_tid",
            "expected_loss_db",
            "distance_km",
            "circuit_id",
            "fiber_start",
            "fiber_end",
            "fiber_type",
        )
    ]
    return values


def _line_endpoint(
    index: int,
    adjacency: str,
    *,
    label: str,
    direction_number: int,
    slot: int,
    line_in_port: int,
    line_out_port: int,
) -> dict[str, object]:
    values = {
        "adjacency": adjacency,
        "label": label,
        "direction_number": direction_number,
        "slot": slot,
        "line_in_port": line_in_port,
        "line_out_port": line_out_port,
    }
    return {
        **values,
        "evidence": [
            _ev(
                f"line_endpoints.{index}.{field}",
                value,
                method=("inferred" if field == "adjacency" else "vision"),
            )
            for field, value in values.items()
        ],
    }


def _complete_response() -> dict[str, object]:
    route_values = {
        "route_code": "RL-0037805",
        "title": "USELP1-USSAT4",
        "revision": "1",
        "ospf_area": "10.6.8.0",
        "optical_band": "c+l",
    }
    return {
        "route": {
            **route_values,
            "evidence": [
                _ev(field, value) for field, value in route_values.items()
            ],
        },
        "shelves": [
            _shelf(1, "USELP1-L8R2", "10.6.22.129", "USELP1", "roadm"),
            _shelf(2, "USQTN1-L8I2", "10.6.22.132", "USQTN1", "ila"),
            _shelf(
                3,
                "USSAT3-L8I2",
                "10.6.22.146",
                "USSAT3",
                "ila",
                "planned_remove",
            ),
            _shelf(4, "USSAT4-L8R3", "10.6.22.158", "USSAT4", "roadm"),
        ],
        "spans": [
            _span(1, "USELP1-L8R2", "USQTN1-L8I2"),
            _span(2, "USQTN1-L8I2", "USSAT4-L8R3"),
        ],
        "raman_callouts": [],
    }


class _FakeProvider:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[object, ...]] = []

    def chat_images_json(
        self,
        system: str,
        user: str,
        images: object,
        schema: object,
        name: str,
    ) -> dict[str, object]:
        self.calls.append((system, user, images, schema, name))
        return self.response


def test_docx_images_follow_relationship_document_order_not_filename(
    tmp_path: Path,
) -> None:
    image1 = _png_bytes((255, 0, 0))
    image2 = _png_bytes((0, 255, 0))
    source_path = tmp_path / "route.docx"
    _write_docx(
        source_path,
        document=_document_xml(["rFirst", "rSecond"]),
        relationships=_rels_xml(
            [
                ("rFirst", "media/image2.png"),
                ("rSecond", "media/image1.png"),
            ]
        ),
        images={
            "word/media/image1.png": image1,
            "word/media/image2.png": image2,
        },
    )

    source = load_diagram_source(source_path)

    assert [image.source_part for image in source.images] == [
        "word/media/image2.png",
        "word/media/image1.png",
    ]
    assert source.images[0].source_sha256 == hashlib.sha256(image2).hexdigest()
    assert source.images[1].source_sha256 == hashlib.sha256(image1).hexdigest()
    assert all(image.format == "png" for image in source.images)


def test_small_standalone_raster_remains_one_source_view(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "small.png"
    source_path.write_bytes(_pattern_png_bytes((12, 8)))

    source = load_diagram_source(
        source_path,
        limits=DiagramImportLimits(
            detail_tile_max_dimension=16,
            detail_tile_overlap=2,
        ),
    )

    assert len(source.images) == 1
    image = source.images[0]
    assert image.index == 0
    assert image.source_label == "small.png"
    assert image.view_kind == "source"
    assert image.source_image_index == 0
    assert (image.canonical_width, image.canonical_height) == (12, 8)
    assert image.crop_box == (0, 0, 12, 8)


def test_large_standalone_raster_uses_canonical_overview_and_ordered_tiles(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "large.png"
    source_bytes = _pattern_png_bytes((12, 12))
    source_path.write_bytes(source_bytes)

    source = load_diagram_source(
        source_path,
        limits=DiagramImportLimits(
            normalized_max_dimension=6,
            detail_tile_max_dimension=8,
            detail_tile_overlap=2,
        ),
    )

    assert [image.index for image in source.images] == [0, 1, 2, 3, 4]
    assert [image.view_kind for image in source.images] == [
        "overview",
        "detail",
        "detail",
        "detail",
        "detail",
    ]
    assert (source.images[0].width, source.images[0].height) == (6, 6)
    assert [image.crop_box for image in source.images] == [
        (0, 0, 12, 12),
        (0, 0, 7, 7),
        (5, 0, 7, 7),
        (0, 5, 7, 7),
        (5, 5, 7, 7),
    ]
    assert all(
        (image.canonical_width, image.canonical_height) == (12, 12)
        for image in source.images
    )
    assert all(
        (image.original_width, image.original_height) == (12, 12)
        for image in source.images
    )
    assert all(
        image.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
        for image in source.images
    )
    assert len(
        {image.normalized_sha256 for image in source.images[1:]}
    ) == 4
    assert "detail row 1/2, column 1/2" in source.images[1].source_label
    assert "detail row 2/2, column 2/2" in source.images[4].source_label


def test_tiled_manifest_and_evidence_keep_provider_image_identity(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "route.png"
    source_path.write_bytes(_pattern_png_bytes((12, 12)))
    response = _complete_response()
    first_shelf = response["shelves"][0]
    tid_evidence = next(
        item
        for item in first_shelf["evidence"]
        if item["field"] == "tid"
    )
    tid_evidence["image_index"] = 4
    provider = _FakeProvider(response)
    limits = DiagramImportLimits(
        normalized_max_dimension=6,
        detail_tile_max_dimension=8,
        detail_tile_overlap=2,
    )

    result = import_route_diagram(source_path, provider, limits=limits)

    assert len(provider.calls) == 1
    system, user, images, _schema, _name = provider.calls[0]
    manifest = json.loads(
        user.split(
            "Ordered source image manifest (JSON data only):\n",
            maxsplit=1,
        )[1]
    )
    assert len(images) == 5
    assert [item["image_index"] for item in manifest] == [0, 1, 2, 3, 4]
    assert manifest[0]["view_kind"] == "overview"
    assert manifest[4]["view_kind"] == "detail"
    assert manifest[4]["canonical_size"] == {"width": 12, "height": 12}
    assert manifest[4]["crop_box"] == [5, 5, 7, 7]
    parsed_tid_evidence = next(
        item
        for item in result.shelves[0].evidence
        if item.field == "tid"
    )
    assert parsed_tid_evidence.image_index == 4
    assert (
        parsed_tid_evidence.source_label
        == result.source.images[4].source_label
    )
    normalized_system = " ".join(system.split())
    assert "overlapping detail views" in normalized_system
    assert "image or tile order alone is not route order" in normalized_system
    assert "unique, contiguous order" in normalized_system
    normalized_user = " ".join(user.split())
    assert (
        "separately census every active span annotation block"
        in normalized_user
    )
    assert "do not stop after loss and distance" in normalized_user


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (
            DiagramImportLimits(
                normalized_max_dimension=6,
                detail_tile_max_dimension=8,
                detail_tile_overlap=2,
                max_images=4,
            ),
            "too many provider image views",
        ),
        (
            DiagramImportLimits(
                normalized_max_dimension=6,
                detail_tile_max_dimension=8,
                detail_tile_overlap=2,
                max_total_normalized_image_bytes=1,
            ),
            "aggregate byte limit",
        ),
    ],
)
def test_tiling_resource_limits_fail_before_provider_call(
    tmp_path: Path,
    limits: DiagramImportLimits,
    message: str,
) -> None:
    source_path = tmp_path / "large.png"
    source_path.write_bytes(_pattern_png_bytes((12, 12)))
    provider = _FakeProvider(_complete_response())

    with pytest.raises(DiagramImportError, match=message):
        import_route_diagram(source_path, provider, limits=limits)

    assert provider.calls == []


def test_docx_images_are_not_tiled_and_share_the_aggregate_limit(
    tmp_path: Path,
) -> None:
    image1 = _pattern_png_bytes((12, 12))
    image2 = _pattern_png_bytes((14, 12))
    source_path = tmp_path / "route.docx"
    _write_docx(
        source_path,
        document=_document_xml(["rFirst", "rSecond"]),
        relationships=_rels_xml(
            [
                ("rFirst", "media/image2.png"),
                ("rSecond", "media/image1.png"),
            ]
        ),
        images={
            "word/media/image1.png": image1,
            "word/media/image2.png": image2,
        },
    )
    limits = DiagramImportLimits(
        detail_tile_max_dimension=8,
        detail_tile_overlap=2,
    )

    source = load_diagram_source(source_path, limits=limits)

    assert len(source.images) == 2
    assert [image.view_kind for image in source.images] == [
        "source",
        "source",
    ]
    assert [image.source_part for image in source.images] == [
        "word/media/image2.png",
        "word/media/image1.png",
    ]
    assert [image.crop_box for image in source.images] == [
        (0, 0, 14, 12),
        (0, 0, 12, 12),
    ]

    with pytest.raises(DiagramImportError, match="aggregate byte limit"):
        load_diagram_source(
            source_path,
            limits=replace(limits, max_total_normalized_image_bytes=1),
        )


def test_import_calls_injected_provider_and_builds_review_only_gui_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    provider = _FakeProvider(_complete_response())

    result = import_route_diagram(path, provider)

    assert len(provider.calls) == 1
    system, user, images, schema, name = provider.calls[0]
    normalized_system = " ".join(system.split())
    normalized_user = " ".join(user.split())
    assert "untrusted" in system
    assert "R4/R2 600mm" in system
    assert "are not evidence of any software release" in normalized_system
    assert "site roles" in normalized_system
    assert "unambiguous visible legend mapping" in normalized_system
    assert '"profile_family.legend"' in normalized_system
    assert "Never derive a shelf role from a TID prefix/suffix" in (
        normalized_system
    )
    assert "Role classification never establishes a shelf variant" in (
        normalized_system
    )
    assert "module_inventory" in system
    assert "line_endpoints" in system
    assert "at most one consolidated record for each adjacency" in (
        normalized_system
    )
    assert "overview and a detail view" in normalized_system
    assert "add_drop_structure" in system
    assert "never use a point or zero-area box" in normalized_system
    assert "Legends, title blocks, callouts" in normalized_system
    assert "C+L is an optical band label, not a revision" in normalized_system
    assert "Follow the visible optical connectors" in normalized_system
    assert "must never construct a provider configuration request" in (
        normalized_system
    )
    assert "one exact RLS R4.0 provider profile" in normalized_system
    assert "visibly attached location/city line" in normalized_system
    assert "never derive it from the TID" in normalized_system
    assert "one exact standalone terminal-pair header" in normalized_system
    assert "Do not concatenate a neighboring product descriptor" in (
        normalized_system
    )
    assert "do not" in user.lower()
    assert "visibly attached city/location line as site_name" in normalized_user
    assert "do not derive site_code from the TID" in normalized_user
    assert "Populate each role-only profile_family" in normalized_user
    assert "required shelf and legend evidence" in normalized_user
    assert "Keep a standalone terminal-pair header line separate" in (
        normalized_user
    )
    assert "Do not derive or transcribe a generator request" in user
    assert "executable provider payload" in user
    assert '"image_index":0' in user
    assert '"source_label":"route.png"' in user
    assert len(images) == 1
    assert images[0][0].startswith(b"\x89PNG")
    assert images[0][1] == "png"
    assert schema["additionalProperties"] is False
    assert name == PROVIDER_RESPONSE_NAME
    assert name == "ciena_rls_route_diagram_v5"
    assert "small-red-slot-port-v1" not in system
    assert "red color alone" in normalized_system

    assert [shelf.tid for shelf in result.active_shelves] == [
        "USELP1-L8R2",
        "USQTN1-L8I2",
        "USSAT4-L8R3",
    ]
    assert [shelf.profile_id for shelf in result.active_shelves] == [
        "roadm_a",
        "ila",
        "roadm_z",
    ]
    assert [shelf.endpoint_side for shelf in result.active_shelves] == [
        "A",
        "",
        "Z",
    ]
    assert [shelf.site_code for shelf in result.active_shelves] == [
        "USELP1",
        "USQTN1",
        "USSAT4",
    ]
    assert result.deployable_cli is False
    assert result.optical_band == "c+l"
    assert result.title == "ELP1-SAT4"
    assert dict(result.route_title_derivation) == {
        "rule_id": "terminal-site-route-title-v1",
        "status": "controlled_derivation",
        "value": "ELP1-SAT4",
        "provider_title": "USELP1-USSAT4",
        "observed_header_pair": "USELP1-USSAT4",
        "endpoint_tids": ("USELP1-L8R2", "USSAT4-L8R3"),
        "endpoint_codes": ("USELP1", "USSAT4"),
        "display_codes": ("ELP1", "SAT4"),
        "removed_shared_prefix": "US",
        "source_sha256": result.source.sha256,
        "deployable_cli": False,
        "route_orientation": {
            "status": "direct_header_and_endpoint_tids",
            "a_terminal_tid": "USELP1-L8R2",
            "z_terminal_tid": "USSAT4-L8R3",
                "a_terminal_code": "USELP1",
                "z_terminal_code": "USSAT4",
                "provider_title": "USELP1-USSAT4",
                "observed_header_pair": "USELP1-USSAT4",
            "provider_order_reversed": False,
            "deployable_cli": False,
        },
    }
    assert any(
        evidence.field == "title"
        and evidence.raw_text == "USELP1-USSAT4"
        and evidence.normalized_value == "ELP1-SAT4"
        and evidence.method == "inferred"
        for evidence in result.route_evidence
    )
    assert not result.blocking_issues
    assert any(issue.code == "PLANNED_REMOVE_EXCLUDED" for issue in result.issues)
    assert any(
        issue.code == "ROUTE_TITLE_FROM_TERMINAL_SITES"
        and issue.blocking is False
        for issue in result.issues
    )

    rows = result.gui_rows()
    assert len(rows) == 3
    assert all(row["tid"] != "USSAT3-L8I2" for row in rows)
    assert rows[0]["profile_payload"] == {}
    assert rows[0]["source_evidence"]["schema_id"] == DIAGRAM_EVIDENCE_SCHEMA_ID
    assert (
        rows[0]["source_evidence"]["schema_version"]
        == DIAGRAM_EVIDENCE_SCHEMA_VERSION
        == "1.7"
    )
    assert (
        rows[0]["source_evidence"]["status"]
        == "unvalidated_diagram_import"
    )
    assert rows[0]["source_evidence"]["deployable_cli"] is False
    assert rows[0]["source_evidence"]["chassis"] == "R4 600mm"
    assert rows[0]["source_evidence"]["software_release"] == "RLS R4.0"
    assert rows[0]["source_evidence"]["shelf_variant"] == "K74-C890-900"
    assert rows[0]["source_evidence"]["raman_callouts"] == []
    assert rows[0]["source_evidence"]["raman_callout_convention"] == {
        "id": "disabled",
        "source_sha256": result.source.sha256,
        "scope": "source",
        "deployable_cli": False,
    }
    assert rows[0]["source_evidence"]["raman_callout_review"] == "not_applicable"
    assert rows[0]["source_evidence"]["fields"]
    assert rows[0]["review_state"] == "pending"
    with pytest.raises(TypeError):
        rows[0]["tid"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.source.file_name = "changed"  # type: ignore[misc]


def test_direct_line_endpoint_evidence_is_retained_for_direction_review(
    tmp_path: Path,
) -> None:
    path = tmp_path / "line-endpoints.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["shelves"][1]["line_endpoints"] = [
        _line_endpoint(
            0,
            "preceding",
            label="DLE LINE1",
            direction_number=1,
            slot=1,
            line_in_port=54,
            line_out_port=53,
        ),
        _line_endpoint(
            1,
            "following",
            label="DLE LINE2",
            direction_number=2,
            slot=1,
            line_in_port=64,
            line_out_port=63,
        ),
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    endpoints = result.active_shelves[1].line_endpoints
    assert [(item.adjacency, item.slot, item.line_out_port) for item in endpoints] == [
        ("preceding", 1, 53),
        ("following", 1, 63),
    ]
    gui_endpoints = result.gui_rows()[1]["source_evidence"]["line_endpoints"]
    assert [item["line_out_port"] for item in gui_endpoints] == [53, 63]
    assert all(item["deployable_cli"] is False for item in gui_endpoints)


def test_endpoint_local_evidence_paths_are_safely_canonicalized(
    tmp_path: Path,
) -> None:
    path = tmp_path / "endpoint-local-evidence-paths.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    endpoint = _line_endpoint(
        0,
        "following",
        label="DLE LINE1",
        direction_number=1,
        slot=1,
        line_in_port=54,
        line_out_port=53,
    )
    for evidence in endpoint["evidence"]:
        evidence["field"] = str(evidence["field"]).rsplit(".", 1)[-1]
    response["shelves"][0]["line_endpoints"] = [endpoint]

    result = import_route_diagram(path, _FakeProvider(response))

    parsed = result.active_shelves[0].line_endpoints[0]
    assert {
        evidence.field for evidence in parsed.evidence
    } == {
        "line_endpoints.0.adjacency",
        "line_endpoints.0.label",
        "line_endpoints.0.direction_number",
        "line_endpoints.0.slot",
        "line_endpoints.0.line_in_port",
        "line_endpoints.0.line_out_port",
    }
    assert not any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field.startswith("shelves[1].line_endpoints")
        for issue in result.blocking_issues
    )
    assert any(
        issue.code == "LINE_ENDPOINT_EVIDENCE_METADATA_CANONICALIZED"
        for issue in result.issues
    )


def test_direct_endpoint_numeric_metadata_is_canonicalized_from_exact_raw_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "endpoint-numeric-metadata.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    endpoint = _line_endpoint(
        0,
        "following",
        label="DLE LINE1",
        direction_number=1,
        slot=1,
        line_in_port=54,
        line_out_port=53,
    )
    raw_by_leaf = {
        "direction_number": "DLE LINE1",
        "line_in_port": "DLE LINE1IN 54",
        "line_out_port": "DLE LINE1OUT 53",
    }
    for evidence in endpoint["evidence"]:
        leaf = str(evidence["field"]).rsplit(".", 1)[-1]
        raw_text = raw_by_leaf.get(leaf)
        if raw_text is not None:
            evidence["raw_text"] = raw_text
            evidence["normalized_value"] = raw_text
    response["shelves"][0]["line_endpoints"] = [endpoint]

    result = import_route_diagram(path, _FakeProvider(response))

    parsed = result.active_shelves[0].line_endpoints[0]
    normalized = {
        evidence.field: evidence.normalized_value
        for evidence in parsed.evidence
    }
    assert normalized["line_endpoints.0.direction_number"] == "1"
    assert normalized["line_endpoints.0.line_in_port"] == "54"
    assert normalized["line_endpoints.0.line_out_port"] == "53"
    assert not any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field.startswith("shelves[1].line_endpoints")
        for issue in result.blocking_issues
    )
    assert any(
        issue.code == "LINE_ENDPOINT_EVIDENCE_METADATA_CANONICALIZED"
        for issue in result.issues
    )


def test_direct_composite_endpoint_label_is_decomposed_without_new_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composite-endpoint-label.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    composite = "DLE LINE1IN 54 / DLE LINE1OUT 53"
    adjacency_evidence = _ev(
        "line_endpoints.0.adjacency",
        "following",
        method="inferred",
    )
    label_evidence = _ev("line_endpoints.0.label", composite)
    response["shelves"][0]["line_endpoints"] = [
        {
            "adjacency": "following",
            "label": composite,
            "direction_number": 1,
            "slot": None,
            "line_in_port": 54,
            "line_out_port": 53,
            "evidence": [adjacency_evidence, label_evidence],
        }
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    parsed = result.active_shelves[0].line_endpoints[0]
    decomposed = {
        evidence.field: evidence
        for evidence in parsed.evidence
        if evidence.field
        in {
            "line_endpoints.0.direction_number",
            "line_endpoints.0.line_in_port",
            "line_endpoints.0.line_out_port",
        }
    }
    assert set(decomposed) == {
        "line_endpoints.0.direction_number",
        "line_endpoints.0.line_in_port",
        "line_endpoints.0.line_out_port",
    }
    for evidence in decomposed.values():
        assert evidence.raw_text == label_evidence["raw_text"]
        assert list(evidence.bbox) == label_evidence["bbox"]
        assert evidence.method == label_evidence["method"]
        assert evidence.confidence == label_evidence["confidence"]
    assert not any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field.startswith("shelves[1].line_endpoints")
        for issue in result.blocking_issues
    )
    advisory = [
        issue
        for issue in result.issues
        if issue.code == "LINE_ENDPOINT_COMPOSITE_LABEL_DECOMPOSED"
    ]
    assert len(advisory) == 1
    assert advisory[0].blocking is False


def test_ambiguous_composite_endpoint_label_does_not_support_port(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous-composite-endpoint-label.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    composite = "DLE LINE1IN 54 / DLE LINE1IN 64 / DLE LINE1OUT 53"
    response["shelves"][0]["line_endpoints"] = [
        {
            "adjacency": "following",
            "label": composite,
            "direction_number": 1,
            "slot": None,
            "line_in_port": 54,
            "line_out_port": 53,
            "evidence": [
                _ev(
                    "line_endpoints.0.adjacency",
                    "following",
                    method="inferred",
                ),
                _ev("line_endpoints.0.label", composite),
            ],
        }
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    assert any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field
        == "shelves[1].line_endpoints.0.line_in_port"
        for issue in result.blocking_issues
    )
    parsed = result.active_shelves[0].line_endpoints[0]
    assert not any(
        evidence.field == "line_endpoints.0.line_in_port"
        for evidence in parsed.evidence
    )


def test_composite_label_does_not_override_conflicting_direct_port_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conflicting-direct-endpoint-port.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    composite = "DLE LINE1IN 54 / DLE LINE1OUT 53"
    conflicting_output = _ev(
        "line_endpoints.0.line_out_port",
        64,
    )
    conflicting_output["raw_text"] = "DLE LINE1OUT 64"
    response["shelves"][0]["line_endpoints"] = [
        {
            "adjacency": "following",
            "label": composite,
            "direction_number": 1,
            "slot": None,
            "line_in_port": 54,
            "line_out_port": 53,
            "evidence": [
                _ev(
                    "line_endpoints.0.adjacency",
                    "following",
                    method="inferred",
                ),
                _ev("line_endpoints.0.label", composite),
                conflicting_output,
            ],
        }
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    assert any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field
        == "shelves[1].line_endpoints.0.line_out_port"
        for issue in result.blocking_issues
    )
    parsed = result.active_shelves[0].line_endpoints[0]
    output_evidence = [
        evidence
        for evidence in parsed.evidence
        if evidence.field == "line_endpoints.0.line_out_port"
    ]
    assert [evidence.normalized_value for evidence in output_evidence] == ["64"]


def test_semantic_duplicate_line_endpoint_is_collapsed_with_advisory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-line-endpoint.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["shelves"][0]["line_endpoints"] = [
        _line_endpoint(
            0,
            "following",
            label="DLE LINE1",
            direction_number=1,
            slot=1,
            line_in_port=54,
            line_out_port=53,
        ),
        _line_endpoint(
            1,
            "following",
            label="DLE LINE1",
            direction_number=1,
            slot=1,
            line_in_port=54,
            line_out_port=53,
        ),
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    endpoints = result.active_shelves[0].line_endpoints
    assert len(endpoints) == 1
    assert (
        endpoints[0].adjacency,
        endpoints[0].slot,
        endpoints[0].line_out_port,
    ) == ("following", 1, 53)
    duplicate_issues = [
        issue
        for issue in result.issues
        if issue.code == "DUPLICATE_LINE_ENDPOINT_EXCLUDED"
    ]
    assert len(duplicate_issues) == 1
    assert duplicate_issues[0].field == "shelves[0].line_endpoints"
    assert duplicate_issues[0].blocking is False


def test_collapsing_line_endpoint_duplicate_reindexes_later_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-before-distinct-line-endpoint.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["shelves"][1]["line_endpoints"] = [
        _line_endpoint(
            0,
            "following",
            label="DLE LINE2",
            direction_number=2,
            slot=1,
            line_in_port=64,
            line_out_port=63,
        ),
        _line_endpoint(
            1,
            "following",
            label="DLE LINE2",
            direction_number=2,
            slot=1,
            line_in_port=64,
            line_out_port=63,
        ),
        _line_endpoint(
            2,
            "preceding",
            label="DLE LINE1",
            direction_number=1,
            slot=1,
            line_in_port=54,
            line_out_port=53,
        ),
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    endpoints = result.active_shelves[1].line_endpoints
    assert [endpoint.adjacency for endpoint in endpoints] == [
        "following",
        "preceding",
    ]
    assert not any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field.startswith("shelves[1].line_endpoints")
        for issue in result.blocking_issues
    )
    assert {
        evidence.field for evidence in endpoints[1].evidence
    } == {
        "line_endpoints.1.adjacency",
        "line_endpoints.1.label",
        "line_endpoints.1.direction_number",
        "line_endpoints.1.slot",
        "line_endpoints.1.line_in_port",
        "line_endpoints.1.line_out_port",
    }


def test_distinct_same_adjacency_line_endpoints_are_retained_as_ambiguous(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ambiguous-line-endpoints.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["shelves"][0]["line_endpoints"] = [
        _line_endpoint(
            0,
            "following",
            label="DLE LINE1",
            direction_number=1,
            slot=1,
            line_in_port=54,
            line_out_port=53,
        ),
        _line_endpoint(
            1,
            "following",
            label="DLE LINE2",
            direction_number=2,
            slot=2,
            line_in_port=64,
            line_out_port=63,
        ),
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    endpoints = result.active_shelves[0].line_endpoints
    assert [
        (
            endpoint.adjacency,
            endpoint.slot,
            endpoint.line_out_port,
        )
        for endpoint in endpoints
    ] == [
        ("following", 1, 53),
        ("following", 2, 63),
    ]
    ambiguous_issues = [
        issue
        for issue in result.blocking_issues
        if issue.code == "AMBIGUOUS_LINE_ENDPOINT_ADJACENCY"
    ]
    assert len(ambiguous_issues) == 1
    assert ambiguous_issues[0].field == "shelves[0].line_endpoints"
    assert result.deployable_cli is False
    assert all(
        endpoint["deployable_cli"] is False
        for endpoint in result.gui_rows()[0]["source_evidence"]["line_endpoints"]
    )


def test_exact_reverse_provider_chain_is_normalized_to_terminal_header(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reverse-chain.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    reversed_shelves = list(reversed(response["shelves"]))
    for order, shelf in enumerate(reversed_shelves, start=1):
        shelf["order"] = order
    response["shelves"] = reversed_shelves
    reversed_spans = list(reversed(response["spans"]))
    for order, span in enumerate(reversed_spans, start=1):
        span["order"] = order
        span["from_tid"], span["to_tid"] = span["to_tid"], span["from_tid"]
        for evidence in span["evidence"]:
            if evidence["field"] == "from_tid":
                evidence["field"] = "to_tid"
            elif evidence["field"] == "to_tid":
                evidence["field"] = "from_tid"
    response["spans"] = reversed_spans

    result = import_route_diagram(path, _FakeProvider(response))

    assert [shelf.tid for shelf in result.active_shelves] == [
        "USELP1-L8R2",
        "USQTN1-L8I2",
        "USSAT4-L8R3",
    ]
    assert [(span.from_tid, span.to_tid) for span in result.active_spans] == [
        ("USELP1-L8R2", "USQTN1-L8I2"),
        ("USQTN1-L8I2", "USSAT4-L8R3"),
    ]
    assert result.route_title_derivation["route_orientation"][
        "provider_order_reversed"
    ] is True
    assert any(
        issue.code == "ROUTE_ORDER_NORMALIZED_TO_TERMINAL_HEADER"
        and issue.blocking is False
        for issue in result.issues
    )


def test_inferred_header_never_assigns_provider_order_as_route_a_z(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unanchored-order.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    for item in response["route"]["evidence"]:
        if item["field"] == "title":
            item["method"] = "inferred"

    result = import_route_diagram(path, _FakeProvider(response))

    assert [shelf.profile_id for shelf in result.active_shelves] == [
        "roadm",
        "ila",
        "roadm",
    ]
    assert [shelf.endpoint_side for shelf in result.active_shelves] == [
        "",
        "",
        "",
    ]
    assert not result.route_title_derivation.get("route_orientation")
    assert any(
        issue.code == "ROUTE_ORIENTATION_UNRESOLVED"
        and issue.blocking is True
        for issue in result.issues
    )


def test_terminal_title_replaces_unsupported_composite_but_keeps_source_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composite-title.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["route"]["title"] = "USELP1-USSAT4 — Ciena RLS"
    response["route"]["evidence"].append(_ev("title", "Ciena RLS"))

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.route_code == "RL-0037805"
    assert result.title == "ELP1-SAT4"
    assert result.route_title_derivation["provider_title"] == (
        "USELP1-USSAT4 — Ciena RLS"
    )
    assert {
        (item.raw_text, item.normalized_value, item.method)
        for item in result.route_evidence
        if item.field == "title"
    } >= {
        ("USELP1-USSAT4", "USELP1-USSAT4", "vision"),
        ("Ciena RLS", "Ciena RLS", "vision"),
        ("USELP1-USSAT4", "ELP1-SAT4", "inferred"),
    }
    assert not any(
        issue.blocking and issue.field == "route.title"
        for issue in result.issues
    )


def test_terminal_title_requires_header_and_ordered_endpoint_corroboration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mismatched-title.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["route"]["title"] = "USELP1-USSAT4 — Ciena RLS"
    title_evidence = next(
        item
        for item in response["route"]["evidence"]
        if item["field"] == "title"
    )
    title_evidence["raw_text"] = "USELP1-USQTN1"
    title_evidence["normalized_value"] = "USELP1-USQTN1"

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.title == "USELP1-USSAT4 — Ciena RLS"
    assert dict(result.route_title_derivation) == {}
    assert any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field == "route.title"
        for issue in result.blocking_issues
    )


def test_terminal_title_requires_direct_tid_evidence_for_both_endpoints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-endpoint-evidence.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    last = response["shelves"][-1]
    last["evidence"] = [
        item for item in last["evidence"] if item["field"] != "tid"
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.title == "USELP1-USSAT4"
    assert dict(result.route_title_derivation) == {}
    assert any(
        issue.field == "shelves[4].tid" for issue in result.blocking_issues
    )


def test_terminal_title_keeps_non_us_tokens_and_ignores_planned_removal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "non-us-title.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["route"]["title"] = "ELP1-SAT4"
    title_evidence = next(
        item
        for item in response["route"]["evidence"]
        if item["field"] == "title"
    )
    title_evidence["raw_text"] = "ELP1-SAT4"
    title_evidence["normalized_value"] = "ELP1-SAT4"
    response["shelves"][0] = _shelf(
        1,
        "ELP1-L8R2",
        "10.6.22.129",
        "ELP1",
        "roadm",
    )
    response["shelves"][3] = _shelf(
        4,
        "SAT4-L8R3",
        "10.6.22.158",
        "SAT4",
        "roadm",
    )
    response["spans"] = [
        _span(1, "ELP1-L8R2", "USQTN1-L8I2"),
        _span(2, "USQTN1-L8I2", "SAT4-L8R3"),
    ]

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.title == "ELP1-SAT4"
    assert result.route_title_derivation["endpoint_tids"] == (
        "ELP1-L8R2",
        "SAT4-L8R3",
    )
    assert result.route_title_derivation["display_codes"] == (
        "ELP1",
        "SAT4",
    )
    assert result.route_title_derivation["removed_shared_prefix"] is None
    assert "USSAT3-L8I2" not in result.route_title_derivation["endpoint_tids"]


def test_raman_convention_is_source_scoped_and_uses_controlled_prompt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raman-convention.png"
    path.write_bytes(_png_bytes())
    provider = _FakeProvider(_complete_response())
    conventions = DiagramImportConventions(
        RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
    )

    result = import_route_diagram(
        path,
        provider,
        conventions=conventions,
    )

    system = str(provider.calls[0][0])
    assert "TRUSTED SOURCE-SCOPED OPERATOR CONVENTION" in system
    assert "small-red-slot-port-v1" in system
    assert "Port 5 is line-out and port 6 is" in system
    assert "detached 3/5 and 3/6" in system
    assert result.conventions == conventions
    provenance = result.raman_callout_provenance
    assert provenance["raman_callout_convention"] == {
        "id": "small-red-slot-port-v1",
        "source_sha256": result.source.sha256,
        "scope": "source",
        "deployable_cli": False,
    }
    assert provenance["raman_callouts"] == []
    assert provenance["deployable_cli"] is False


def test_route_optical_band_is_optional_but_requires_direct_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route-band.png"
    path.write_bytes(_png_bytes())

    absent = _complete_response()
    absent_route = absent["route"]
    assert isinstance(absent_route, dict)
    absent_route["optical_band"] = None
    absent_route["evidence"] = [
        item
        for item in absent_route["evidence"]
        if item["field"] != "optical_band"
    ]
    absent_result = parse_provider_result(load_diagram_source(path), absent)

    assert absent_result.optical_band is None
    assert not any(
        issue.field == "route.optical_band"
        for issue in absent_result.blocking_issues
    )

    unsupported = _complete_response()
    unsupported_route = unsupported["route"]
    assert isinstance(unsupported_route, dict)
    unsupported_route["evidence"] = [
        item
        for item in unsupported_route["evidence"]
        if item["field"] != "optical_band"
    ]
    unsupported_result = parse_provider_result(
        load_diagram_source(path),
        unsupported,
    )

    assert unsupported_result.optical_band == "c+l"
    assert any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field == "route.optical_band"
        for issue in unsupported_result.blocking_issues
    )


def test_raman_callout_is_discarded_when_convention_is_disabled(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raman-disabled.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["raman_callouts"] = [
        _raman_callout(
            4,
            5,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
        )
    ]

    result = parse_provider_result(load_diagram_source(path), response)

    assert result.raman_callouts == ()
    assert any(
        issue.code == "RAMAN_CALLOUT_CONVENTION_NOT_ENABLED"
        for issue in result.blocking_issues
    )
    assert all(row["profile_payload"] == {} for row in result.gui_rows())


def test_raman_endpoint_pairs_suggest_slots_and_legend_remains_advisory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raman-pairs.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelves = response["shelves"]
    assert isinstance(shelves, list)
    for shelf_index in (1, 3):
        shelf = shelves[shelf_index]
        assert isinstance(shelf, dict)
        shelf["raman_label"] = None
        shelf["evidence"] = [
            item
            for item in shelf["evidence"]
            if item["field"] != "raman_label"
        ]
    response["raman_callouts"] = [
        _raman_callout(
            3,
            5,
            shelf_tid=None,
            context="legend_sample",
        ),
        _raman_callout(
            3,
            6,
            shelf_tid=None,
            context="legend_sample",
        ),
        _raman_callout(
            4,
            5,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
        ),
        _raman_callout(
            4,
            6,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
        ),
        _raman_callout(
            6,
            5,
            shelf_tid="USSAT4-L8R3",
            context="shelf_endpoint",
        ),
        _raman_callout(
            6,
            6,
            shelf_tid="USSAT4-L8R3",
            context="shelf_endpoint",
        ),
        # The same physical box repeated in an overview/detail response.
        _raman_callout(
            4,
            5,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
        ),
    ]
    conventions = DiagramImportConventions(
        RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
    )

    result = parse_provider_result(
        load_diagram_source(path),
        response,
        conventions=conventions,
    )

    assert len(result.raman_callouts) == 6
    rows = {row["tid"]: row for row in result.gui_rows()}
    qtn = rows["USQTN1-L8I2"]
    sat = rows["USSAT4-L8R3"]
    assert qtn["raman_label"] == "Slot 4"
    assert qtn["raman_label_suggestion"] == "Slot 4"
    assert sat["raman_label"] == "Slot 6"
    assert sat["raman_label_suggestion"] == "Slot 6"
    assert qtn["profile_payload"] == {}
    assert sat["profile_payload"] == {}
    assert qtn["source_evidence"]["raman_callout_review"] == "pending"
    assert qtn["source_evidence"]["raman_callout_suggestion"] == {
        "display_text": "Slot 4",
        "raman_present": True,
        "slots": [4],
        "status": "pending_operator_review",
        "deployable_cli": False,
    }
    assert {
        (item["slot"], item["port"])
        for item in qtn["source_evidence"]["raman_callouts"]
    } == {(4, 5), (4, 6)}
    assert qtn["source_evidence"]["module_inventory"] == []
    provenance = result.raman_callout_provenance
    assert len(provenance["raman_callouts"]) == 6
    assert len(provenance["legend_sample_callouts"]) == 2
    assert provenance["unassigned_raman_callouts"] == []
    assert provenance["unknown_callouts"] == []
    codes = {issue.code for issue in result.issues}
    assert "RAMAN_LEGEND_SAMPLE_EXCLUDED" in codes
    assert "DUPLICATE_RAMAN_CALLOUT_EXCLUDED" in codes
    assert "UNASSIGNED_RAMAN_CALLOUT" not in codes
    assert result.deployable_cli is False


def test_unknown_and_incomplete_raman_callouts_do_not_suggest_a_slot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raman-incomplete.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["raman_label"] = None
    shelf["evidence"] = [
        item for item in shelf["evidence"] if item["field"] != "raman_label"
    ]
    response["raman_callouts"] = [
        _raman_callout(
            4,
            5,
            shelf_tid="USELP1-L8R2",
            context="shelf_endpoint",
        ),
        _raman_callout(
            7,
            6,
            shelf_tid=None,
            context="unknown",
        ),
    ]

    result = parse_provider_result(
        load_diagram_source(path),
        response,
        conventions=DiagramImportConventions(
            RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
        ),
    )

    row = result.gui_rows()[0]
    assert row["raman_label"] == ""
    assert row["raman_label_suggestion"] == ""
    assert len(row["source_evidence"]["raman_callouts"]) == 1
    codes = {issue.code for issue in result.blocking_issues}
    assert "INCOMPLETE_RAMAN_ENDPOINT_PAIR" in codes
    assert "UNASSIGNED_RAMAN_CALLOUT" in codes
    assert len(result.raman_callout_provenance["unassigned_raman_callouts"]) == 1
    assert len(result.raman_callout_provenance["unknown_callouts"]) == 1


def test_raman_shelf_association_requires_matching_inferred_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raman-association.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["raman_callouts"] = [
        _raman_callout(
            4,
            5,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
            include_association=False,
        ),
        _raman_callout(
            4,
            6,
            shelf_tid="USQTN1-L8I2",
            context="shelf_endpoint",
            include_association=False,
        ),
    ]

    result = parse_provider_result(
        load_diagram_source(path),
        response,
        conventions=DiagramImportConventions(
            RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
        ),
    )

    assert all(item.context == "unknown" for item in result.raman_callouts)
    assert all(
        row["source_evidence"]["raman_callouts"] == []
        for row in result.gui_rows()
    )
    codes = [issue.code for issue in result.blocking_issues]
    assert codes.count("MISSING_RAMAN_CALLOUT_EVIDENCE") == 2
    assert codes.count("UNASSIGNED_RAMAN_CALLOUT") == 2


def test_raman_raw_slot_port_mismatch_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "raman-mismatch.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    callout = _raman_callout(
        4,
        5,
        shelf_tid="USQTN1-L8I2",
        context="shelf_endpoint",
    )
    callout["raw_text"] = "4/6"
    response["raman_callouts"] = [callout]

    result = parse_provider_result(
        load_diagram_source(path),
        response,
        conventions=DiagramImportConventions(
            RAMAN_CALLOUT_CONVENTION_SMALL_RED_SLOT_PORT
        ),
    )

    assert result.raman_callouts == ()
    assert any(
        issue.code == "INVALID_RAMAN_SLOT_PORT"
        for issue in result.blocking_issues
    )


def test_gui_rows_synthesize_internal_shelf_id_without_fabricating_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-tid.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    shelf["tid"] = None
    tid_evidence = next(
        item for item in shelf["evidence"] if item["field"] == "tid"
    )
    tid_evidence["raw_text"] = ""
    tid_evidence["normalized_value"] = None

    result = parse_provider_result(load_diagram_source(path), response)
    row = result.gui_rows()[0]

    # shelf_id is ATLAS state, not a claim about text observed in the diagram.
    # Source order keeps it unique even when the customer TID is unavailable.
    assert row["shelf_id"] == "diagram-1-unknown"
    assert row["tid"] == ""
    assert "shelf_id" not in row["source_evidence"]
    assert all(
        item["field"] != "shelf_id"
        for item in row["source_evidence"]["fields"]
    )
    assert any(
        issue.code == "MISSING_REQUIRED_FIELD"
        and issue.field == "shelves[1].tid"
        for issue in result.blocking_issues
    )


def test_missing_site_fields_remain_reviewable_with_observed_prefixed_tids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route-without-sites.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    active = [
        shelf
        for shelf in response["shelves"]
        if shelf["lifecycle"] != "planned_remove"
    ]
    for shelf in active:
        shelf["site_code"] = None
        shelf["site_name"] = None
        shelf["evidence"] = [
            item
            for item in shelf["evidence"]
            if item["field"] not in {"site_code", "site_name"}
        ]

    result = import_route_diagram(path, _FakeProvider(response))

    assert all(shelf.site_code is None for shelf in result.active_shelves)
    assert all(shelf.site_name is None for shelf in result.active_shelves)
    assert [shelf.profile_id for shelf in result.active_shelves] == [
        "roadm_a",
        "ila",
        "roadm_z",
    ]
    missing_site_paths = {
        issue.field
        for issue in result.blocking_issues
        if issue.code == "MISSING_REQUIRED_FIELD"
        and issue.field.endswith((".site_code", ".site_name"))
    }
    assert missing_site_paths == {
        "shelves[1].site_code",
        "shelves[1].site_name",
        "shelves[2].site_code",
        "shelves[2].site_name",
        "shelves[4].site_code",
        "shelves[4].site_name",
    }
    rows = result.gui_rows()
    assert all(row["site_code"] == "" for row in rows)
    assert all(row["site_name"] == "" for row in rows)
    assert all(row["profile_payload"] == {} for row in rows)
    assert all(row["review_state"] == "pending" for row in rows)


def test_live_provider_schema_excludes_removed_r42_configuration_contract() -> None:
    route_item = PROVIDER_SCHEMA["properties"]["route"]
    route_schema = route_item["properties"]
    shelf_item = PROVIDER_SCHEMA["properties"]["shelves"]["items"]
    shelf_schema = shelf_item["properties"]
    required = set(shelf_item["required"])
    families = set(shelf_schema["profile_family"]["enum"])
    response_required = set(PROVIDER_SCHEMA["required"])
    callout_item = PROVIDER_SCHEMA["properties"]["raman_callouts"]["items"]
    callout_schema = callout_item["properties"]

    assert "protected_dci_request" not in shelf_schema
    assert "protected_dci_request" not in required
    assert "config_evidence" not in shelf_schema
    assert "config_evidence" not in required
    assert "protected_dci" not in families
    assert "Role-only planning classification" in (
        shelf_schema["profile_family"]["description"]
    )
    assert "never derive it from the TID" in (
        shelf_schema["site_code"]["description"]
    )
    assert "visibly attached city/location line" in (
        shelf_schema["site_name"]["description"]
    )
    assert "raman_callouts" in response_required
    assert "optical_band" in route_item["required"]
    assert set(route_schema["optical_band"]["enum"]) == {
        None,
        "c",
        "l",
        "c+l",
        "integrated_c+l",
    }
    assert set(callout_item["required"]) == {
        "raw_text",
        "slot",
        "port",
        "shelf_tid",
        "context",
        "evidence",
    }
    assert callout_schema["port"]["enum"] == [5, 6]
    assert set(callout_schema["context"]["enum"]) == {
        "shelf_endpoint",
        "legend_sample",
        "unknown",
    }


def test_inferred_profile_family_requires_matching_direct_legend_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legend-role.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    profile_evidence = next(
        item
        for item in shelf["evidence"]
        if item["field"] == "profile_family"
    )
    profile_evidence.update(
        {
            "raw_text": "yellow shelf fill matching legend swatch",
            "method": "inferred",
        }
    )

    missing_legend = parse_provider_result(
        load_diagram_source(path),
        response,
    )

    assert any(
        issue.code == "MISSING_PROFILE_FAMILY_LEGEND_EVIDENCE"
        and issue.field == "shelves[1].profile_family"
        for issue in missing_legend.blocking_issues
    )
    assert missing_legend.shelves[0].profile_family == "unknown"
    assert missing_legend.shelves[0].profile_id == ""

    shelf["evidence"].append(
        _ev("profile_family.legend", "roadm", method="vision")
    )
    supported = parse_provider_result(load_diagram_source(path), response)

    assert not any(
        issue.code
        in {
            "MISSING_PROFILE_FAMILY_LEGEND_EVIDENCE",
            "LOW_CONFIDENCE_PROFILE_FAMILY_LEGEND",
        }
        and issue.field == "shelves[1].profile_family"
        for issue in supported.blocking_issues
    )
    assert supported.active_shelves[0].profile_id == "roadm_a"
    assert supported.gui_rows()[0]["profile_payload"] == {}


def test_profile_family_without_matching_shelf_evidence_is_downgraded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unsupported-role.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    shelf["evidence"] = [
        item
        for item in shelf["evidence"]
        if item["field"] != "profile_family"
    ]

    result = parse_provider_result(load_diagram_source(path), response)

    assert any(
        issue.code == "MISSING_PROFILE_FAMILY_EVIDENCE"
        and issue.field == "shelves[1].profile_family"
        for issue in result.blocking_issues
    )
    assert result.shelves[0].profile_family == "unknown"
    assert result.shelves[0].profile_id == ""


@pytest.mark.parametrize(
    ("evidence_field", "method", "raw_text", "issue_code"),
    (
        (
            "profile_family",
            "vision",
            "yellow shelf fill",
            "PROFILE_FAMILY_DIRECT_TEXT_MISMATCH",
        ),
        (
            "profile_family.legend",
            "vision",
            "yellow legend swatch",
            "PROFILE_FAMILY_LEGEND_TEXT_MISMATCH",
        ),
    ),
)
def test_visual_color_without_matching_visible_role_text_is_downgraded(
    tmp_path: Path,
    evidence_field: str,
    method: str,
    raw_text: str,
    issue_code: str,
) -> None:
    path = tmp_path / f"{evidence_field.replace('.', '-')}.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    primary = next(
        item
        for item in shelf["evidence"]
        if item["field"] == "profile_family"
    )
    if evidence_field == "profile_family":
        primary["raw_text"] = raw_text
    else:
        primary["method"] = "inferred"
        shelf["evidence"].append(
            {
                **_ev(evidence_field, "roadm", method=method),
                "raw_text": raw_text,
            }
        )

    result = parse_provider_result(load_diagram_source(path), response)

    assert any(
        issue.code == issue_code
        and issue.field == "shelves[1].profile_family"
        for issue in result.blocking_issues
    )
    assert result.shelves[0].profile_family == "unknown"
    assert result.shelves[0].profile_id == ""


def test_low_confidence_profile_family_legend_remains_blocking(
    tmp_path: Path,
) -> None:
    path = tmp_path / "low-confidence-legend-role.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    profile_evidence = next(
        item
        for item in shelf["evidence"]
        if item["field"] == "profile_family"
    )
    profile_evidence["method"] = "inferred"
    shelf["evidence"].append(
        _ev(
            "profile_family.legend",
            "roadm",
            confidence=0.70,
            method="vision",
        )
    )

    result = parse_provider_result(load_diagram_source(path), response)

    assert any(
        issue.code == "LOW_CONFIDENCE_PROFILE_FAMILY_LEGEND"
        and issue.field == "shelves[1].profile_family"
        for issue in result.blocking_issues
    )
    assert result.shelves[0].profile_family == "unknown"
    assert result.shelves[0].profile_id == ""


@pytest.mark.parametrize("visible_label", ("ROAD M", "R.O.A.D.M"))
def test_roadm_legend_allows_display_letter_separators(
    tmp_path: Path,
    visible_label: str,
) -> None:
    path = tmp_path / "spaced-roadm-legend.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    profile_evidence = next(
        item
        for item in shelf["evidence"]
        if item["field"] == "profile_family"
    )
    profile_evidence.update(
        {
            "raw_text": "yellow shelf fill matching legend swatch",
            "method": "inferred",
        }
    )
    shelf["evidence"].append(
        {
            **_ev("profile_family.legend", "roadm", method="vision"),
            "raw_text": visible_label,
        }
    )

    result = parse_provider_result(load_diagram_source(path), response)

    assert not any(
        issue.code == "PROFILE_FAMILY_LEGEND_TEXT_MISMATCH"
        and issue.field == "shelves[1].profile_family"
        for issue in result.blocking_issues
    )
    assert result.shelves[0].profile_family == "roadm"
    assert result.shelves[0].profile_id == "roadm_a"


def test_v1_provider_configuration_fields_are_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v1-response.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    shelf["protected_dci_request"] = None
    shelf["config_evidence"] = []

    with pytest.raises(DiagramImportError, match="unknown keys"):
        parse_provider_result(load_diagram_source(path), response)


def test_explicit_non_r40_release_is_preserved_for_downstream_rejection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "explicit-r42-release.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    shelf["software_release"] = "RLS R4.2"
    release_evidence = next(
        item for item in shelf["evidence"] if item["field"] == "software_release"
    )
    release_evidence["raw_text"] = "RLS R4.2"
    release_evidence["normalized_value"] = "RLS R4.2"

    result = parse_provider_result(load_diagram_source(path), response)
    candidate = result.active_shelves[0]
    row = result.gui_rows()[0]

    assert candidate.software_release == "RLS R4.2"
    assert row["software_release"] == "RLS R4.2"
    assert row["source_evidence"]["software_release"] == "RLS R4.2"
    assert any(
        item["field"] == "software_release"
        and item["raw_text"] == "RLS R4.2"
        and item["normalized_value"] == "RLS R4.2"
        for item in row["source_evidence"]["fields"]
    )
    assert row["profile_payload"] == {}


def test_default_route_provider_uses_configured_high_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config
    import gui.rls_route_frame as route_frame
    import utils.ai.provider as provider_module

    captured: dict[str, object] = {}

    class _Provider:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(provider_module, "OpenAIProvider", _Provider)

    provider = route_frame._default_diagram_provider()

    assert isinstance(provider, _Provider)
    assert config.RLS_DIAGRAM_REASONING_EFFORT == "high"
    assert captured["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "bbox",
    (
        [0.0, 0.0, 0.0, 0.0],
        [0.05, 0.05, 0.0, 0.1],
        [0.05, 0.05, 0.2, 0.0],
        [0.5, 0.1, -0.2, 0.1],
        [0.95, 0.05, 0.1, 0.1],
        [0.95, 0.05, 0.076, 0.1],
        [0.2, 0.98, 0.1, 0.04],
        [0.999, 0.05, 0.02, 0.1],
        [0.0, 0.0, 1.01, 0.1],
        [1.05, 0.05, 0.1, 0.1],
        [0.05, 1.05, 0.1, 0.1],
    ),
)
def test_invalid_evidence_bbox_is_discarded_without_losing_route_draft(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    bbox: list[float],
) -> None:
    path = tmp_path / "invalid-bbox.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    bad_evidence = response["route"]["evidence"][0]
    bad_evidence["bbox"] = bbox

    with caplog.at_level(
        logging.WARNING,
        logger="utils.rls_config.diagram_import",
    ):
        result = import_route_diagram(path, _FakeProvider(response))

    assert result.route_code == "RL-0037805"
    assert len(result.gui_rows()) == 3
    assert all(item.field != "route_code" for item in result.route_evidence)
    assert any(
        issue.code == "INVALID_EVIDENCE_BBOX"
        and issue.field == "route.evidence[0].bbox"
        for issue in result.blocking_issues
    )
    assert any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field == "route.route_code"
        for issue in result.blocking_issues
    )
    assert "Discarded evidence at route.evidence[0].bbox" in caplog.text
    assert "bbox=" in caplog.text
    assert "RL-0037805" not in caplog.text
    assert result.deployable_cli is False


@pytest.mark.parametrize(
    ("bbox", "expected_bbox"),
    (
        (
            [0.961, 0.196, 0.06, 0.01],
            [0.961, 0.196, 0.039, 0.01],
        ),
        (
            [0.2, 0.975, 0.1, 0.04],
            [0.2, 0.975, 0.1, 0.025],
        ),
        (
            [0.95, 0.2, 0.075, 0.1],
            [0.95, 0.2, 0.05, 0.1],
        ),
    ),
)
def test_small_bbox_edge_overflow_is_clipped_and_retained_for_review(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    bbox: list[float],
    expected_bbox: list[float],
) -> None:
    path = tmp_path / "edge-bbox.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    response["route"]["evidence"][0]["bbox"] = bbox

    with caplog.at_level(
        logging.WARNING,
        logger="utils.rls_config.diagram_import",
    ):
        result = import_route_diagram(path, _FakeProvider(response))

    route_code_evidence = next(
        item for item in result.route_evidence if item.field == "route_code"
    )
    clipped = [
        issue
        for issue in result.issues
        if issue.code == "EVIDENCE_BBOX_EDGE_CLIPPED"
        and issue.field == "route.evidence[0].bbox"
    ]

    assert route_code_evidence.bbox == pytest.approx(expected_bbox)
    adjustment = route_code_evidence.to_dict()["bbox_adjustment"]
    assert adjustment["policy_id"] == "small-edge-overflow-v1"
    assert adjustment["provider_bbox"] == pytest.approx(bbox)
    assert adjustment["normalized_bbox"] == pytest.approx(expected_bbox)
    assert adjustment["retained_width_fraction"] >= 0.6
    assert adjustment["retained_height_fraction"] >= 0.6
    assert adjustment["status"] == "clipped_pending_review"
    assert adjustment["deployable_cli"] is False
    assert len(clipped) == 1
    assert clipped[0].blocking is False
    assert not any(
        issue.code == "INVALID_EVIDENCE_BBOX"
        and issue.field == "route.evidence[0].bbox"
        for issue in result.issues
    )
    assert not any(
        issue.code == "MISSING_MATCHING_EVIDENCE"
        and issue.field == "route.route_code"
        for issue in result.issues
    )
    assert "Clipped evidence bbox at route.evidence[0].bbox" in caplog.text
    assert "original_bbox=" in caplog.text
    assert "normalized_bbox=" in caplog.text
    assert "policy=small-edge-overflow-v1" in caplog.text
    assert "image_index=0" in caplog.text
    assert "RL-0037805" not in caplog.text
    assert result.deployable_cli is False


def test_logged_elp1_sat4_edge_boxes_retain_route_topology_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "elp1-sat4-edge-bboxes.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()

    shelf_evidence = response["shelves"][1]["evidence"]
    for field_name, bbox in (
        ("tid", [0.961, 0.196, 0.06, 0.01]),
        ("primary_oam_ip", [0.966, 0.207, 0.05, 0.009]),
        ("site_name", [0.967, 0.218, 0.051, 0.01]),
    ):
        next(
            item for item in shelf_evidence if item["field"] == field_name
        )["bbox"] = bbox
    next(
        item
        for item in response["spans"][0]["evidence"]
        if item["field"] == "to_tid"
    )["bbox"] = [0.961, 0.196, 0.06, 0.01]
    next(
        item
        for item in response["spans"][1]["evidence"]
        if item["field"] == "from_tid"
    )["bbox"] = [0.961, 0.196, 0.06, 0.01]

    result = import_route_diagram(path, _FakeProvider(response))

    clipped = [
        issue
        for issue in result.issues
        if issue.code == "EVIDENCE_BBOX_EDGE_CLIPPED"
    ]
    retained_shelf_fields = {
        item.field
        for item in result.shelves[1].evidence
        if item.bbox_adjustment is not None
    }
    retained_span_fields = {
        item.field
        for span in result.spans
        for item in span.evidence
        if item.bbox_adjustment is not None
    }

    assert len(clipped) == 5
    assert all(issue.blocking is False for issue in clipped)
    assert retained_shelf_fields == {"tid", "primary_oam_ip", "site_name"}
    assert {"to_tid", "from_tid"} <= retained_span_fields
    assert not any(
        issue.code in {
            "INVALID_EVIDENCE_BBOX",
            "MISSING_MATCHING_EVIDENCE",
        }
        and (
            issue.field.startswith("shelves[1].")
            or issue.field.startswith("spans[")
        )
        for issue in result.issues
    )
    route_orientation = result.route_title_derivation["route_orientation"]
    assert route_orientation["a_terminal_code"] == "USELP1"
    assert route_orientation["z_terminal_code"] == "USSAT4"


@pytest.mark.parametrize(
    ("bbox", "message"),
    (
        ([0.05, 0.05, 0.2], "must contain four numbers"),
        ([0.05, 0.05, "wide", 0.1], "bbox\\[2\\] must be a number"),
    ),
)
def test_structurally_invalid_evidence_bbox_remains_import_fatal(
    tmp_path: Path,
    bbox: list[object],
    message: str,
) -> None:
    path = tmp_path / "structural-bbox.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    response["route"]["evidence"][0]["bbox"] = bbox

    with pytest.raises(DiagramImportError, match=message):
        parse_provider_result(source, response)


def test_profile_sides_follow_distinct_sites_and_keep_intermediate_roadm(
    tmp_path: Path,
) -> None:
    path = tmp_path / "multi-shelf-sites.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    response["route"]["title"] = "AAA-ZZZ"
    title_evidence = next(
        item
        for item in response["route"]["evidence"]
        if item["field"] == "title"
    )
    title_evidence["raw_text"] = "AAA-ZZZ"
    title_evidence["normalized_value"] = "AAA-ZZZ"
    response["shelves"] = [
        _shelf(1, "AAA-ROADM", "192.0.2.1", "A", "roadm"),
        _shelf(2, "AAA-ADD", "192.0.2.2", "A", "add_drop"),
        _shelf(3, "MID-ROADM", "192.0.2.3", "MID", "roadm"),
        _shelf(4, "ZZZ-ROADM", "192.0.2.4", "Z", "roadm"),
    ]
    response["spans"] = [
        _span(1, "AAA-ROADM", "MID-ROADM"),
        _span(2, "MID-ROADM", "ZZZ-ROADM"),
    ]

    result = parse_provider_result(source, response)

    assert [shelf.profile_id for shelf in result.active_shelves] == [
        "roadm_a",
        "add_drop_a",
        "roadm",
        "roadm_z",
    ]
    assert [shelf.endpoint_side for shelf in result.active_shelves] == [
        "A",
        "A",
        "",
        "Z",
    ]
    assert not {
        "SIDE_PROFILE_NOT_ACTIVE_ENDPOINT",
        "ILA_NOT_INTERMEDIATE",
        "TERMINAL_PROFILE_NOT_ENDPOINT",
    } & {issue.code for issue in result.blocking_issues}


def test_observed_chassis_populates_visible_variant_without_becoming_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["shelf_variant"] = None
    evidence = shelf["evidence"]
    assert isinstance(evidence, list)
    evidence[:] = [
        item for item in evidence if item["field"] != "shelf_variant"
    ]

    result = import_route_diagram(path, _FakeProvider(response))
    row = result.gui_rows()[0]

    assert row["shelf_variant"] == "R4 600mm"
    assert row["software_release"] == "RLS R4.0"
    assert row["source_evidence"]["chassis"] == "R4 600mm"
    assert row["source_evidence"]["shelf_variant"] is None


def test_direct_chassis_shaped_variant_populates_chassis_review_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["chassis"] = None
    shelf["shelf_variant"] = "R4 600mm"
    evidence = shelf["evidence"]
    assert isinstance(evidence, list)
    evidence[:] = [item for item in evidence if item["field"] != "chassis"]
    variant_evidence = next(
        item for item in evidence if item["field"] == "shelf_variant"
    )
    variant_evidence["raw_text"] = "R4 600mm"
    variant_evidence["normalized_value"] = "R4 600mm"

    result = import_route_diagram(path, _FakeProvider(response))
    imported = result.active_shelves[0]
    row = result.gui_rows()[0]

    assert imported.chassis == "R4 600mm"
    assert imported.shelf_variant == "R4 600mm"
    assert row["source_evidence"]["chassis"] == "R4 600mm"
    assert any(
        item.field == "chassis"
        and item.normalized_value == "R4 600mm"
        and item.method == "inferred"
        for item in imported.evidence
    )
    assert not any(
        issue.blocking and issue.field == "shelves[1].chassis"
        for issue in result.issues
    )
    assert any(
        issue.code == "CHASSIS_FROM_DIRECT_VARIANT_EVIDENCE"
        and not issue.blocking
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "variant",
    (
        "K74-C890-900",
        "R2",
        "R4",
        "R4/R2 600mm",
        "R6-300",
        "R8-300",
    ),
)
def test_non_chassis_variant_cannot_fill_missing_chassis(
    tmp_path: Path,
    variant: str,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["chassis"] = None
    shelf["shelf_variant"] = variant
    evidence = shelf["evidence"]
    assert isinstance(evidence, list)
    evidence[:] = [item for item in evidence if item["field"] != "chassis"]
    variant_evidence = next(
        item for item in evidence if item["field"] == "shelf_variant"
    )
    variant_evidence["raw_text"] = variant
    variant_evidence["normalized_value"] = variant

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.active_shelves[0].chassis is None
    assert any(
        issue.code == "MISSING_REQUIRED_FIELD"
        and issue.field == "shelves[1].chassis"
        for issue in result.blocking_issues
    )
    assert not any(
        issue.code == "CHASSIS_FROM_DIRECT_VARIANT_EVIDENCE"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    ("method", "confidence", "raw_text"),
    (
        ("inferred", 0.99, "R4 600mm"),
        ("vision", 0.84, "R4 600mm"),
        ("vision", 0.99, "R4 chassis 600mm"),
    ),
)
def test_chassis_variant_alias_requires_exact_high_confidence_direct_source(
    tmp_path: Path,
    method: str,
    confidence: float,
    raw_text: str,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["chassis"] = None
    shelf["shelf_variant"] = "R4 600mm"
    evidence = shelf["evidence"]
    assert isinstance(evidence, list)
    evidence[:] = [item for item in evidence if item["field"] != "chassis"]
    variant_evidence = next(
        item for item in evidence if item["field"] == "shelf_variant"
    )
    variant_evidence["method"] = method
    variant_evidence["confidence"] = confidence
    variant_evidence["raw_text"] = raw_text
    variant_evidence["normalized_value"] = "R4 600mm"

    result = import_route_diagram(path, _FakeProvider(response))

    assert result.active_shelves[0].chassis is None
    assert any(
        issue.code == "MISSING_REQUIRED_FIELD"
        and issue.field == "shelves[1].chassis"
        for issue in result.blocking_issues
    )


@pytest.mark.parametrize(
    "chassis_text",
    (
        "R2",
        "R4",
        "R6-300",
        "R8-300",
        "R4/R2 600mm",
        "Release R4 chassis",
        "R6.300 shelf",
    ),
)
def test_chassis_label_cannot_satisfy_software_release_evidence(
    tmp_path: Path,
    chassis_text: str,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    response = _complete_response()
    shelf = response["shelves"][0]
    assert isinstance(shelf, dict)
    shelf["software_release"] = chassis_text
    evidence = shelf["evidence"]
    assert isinstance(evidence, list)
    release_evidence = next(
        item for item in evidence if item["field"] == "software_release"
    )
    release_evidence["raw_text"] = chassis_text
    release_evidence["normalized_value"] = chassis_text

    result = import_route_diagram(path, _FakeProvider(response))

    assert "CHASSIS_IS_NOT_SOFTWARE_RELEASE" in {
        issue.code for issue in result.blocking_issues
    }
    assert result.active_shelves[0].software_release is None
    row = result.gui_rows()[0]
    assert row["software_release"] == ""
    assert row["source_evidence"]["software_release"] is None
    assert any(
        item["field"] == "software_release"
        and item["normalized_value"] == chassis_text
        for item in row["source_evidence"]["fields"]
    )


def test_missing_values_low_confidence_and_duplicate_exact_ip_block(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    shelves = response["shelves"]
    assert isinstance(shelves, list)
    first = shelves[0]
    second = shelves[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first["software_release"] = None
    first["power_label"] = None
    first["raman_label"] = None
    first_evidence = first["evidence"]
    assert isinstance(first_evidence, list)
    for evidence in first_evidence:
        if evidence["field"] == "tid":
            evidence["confidence"] = 0.40
    second["primary_oam_ip"] = first["primary_oam_ip"]
    second_evidence = second["evidence"]
    assert isinstance(second_evidence, list)
    for evidence in second_evidence:
        if evidence["field"] == "primary_oam_ip":
            evidence["normalized_value"] = first["primary_oam_ip"]

    result = parse_provider_result(source, response)
    codes = {issue.code for issue in result.blocking_issues}

    assert "MISSING_REQUIRED_FIELD" in codes
    assert "LOW_CONFIDENCE_FIELD" in codes
    assert "DUPLICATE_OAM_IP" in codes
    assert result.active_shelves[0].software_release is None
    assert result.active_shelves[0].power_label is None
    assert result.active_shelves[0].raman_label is None
    assert result.deployable_cli is False


@pytest.mark.parametrize("bad_ip", [".133", "010.006.022.133", "10.6.22.133/24"])
def test_ip_must_be_full_canonical_ipv4(tmp_path: Path, bad_ip: str) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    shelves = response["shelves"]
    assert isinstance(shelves, list)
    first = shelves[0]
    assert isinstance(first, dict)
    first["primary_oam_ip"] = bad_ip
    for evidence in first["evidence"]:
        if evidence["field"] == "primary_oam_ip":
            evidence["normalized_value"] = bad_ip

    result = parse_provider_result(source, response)

    assert {
        issue.code for issue in result.blocking_issues
    } & {"INVALID_OAM_IP", "NONCANONICAL_OAM_IP"}


def test_provider_json_is_strict_and_lifecycle_is_not_inferred(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    unknown = _complete_response()
    unknown["unexpected"] = True
    with pytest.raises(DiagramImportError, match="unknown keys"):
        parse_provider_result(source, unknown)

    lifecycle = _complete_response()
    shelves = lifecycle["shelves"]
    assert isinstance(shelves, list)
    shelves[0]["lifecycle"] = "decommission"
    with pytest.raises(DiagramImportError, match="lifecycle"):
        parse_provider_result(source, lifecycle)


def test_noncontiguous_provider_order_is_blocking_not_silently_sorted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "route.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    shelves = response["shelves"]
    assert isinstance(shelves, list)
    shelves[0], shelves[1] = shelves[1], shelves[0]

    result = parse_provider_result(source, response)

    assert any(
        issue.code == "NONCONTIGUOUS_ORDER" and issue.field == "shelves"
        for issue in result.blocking_issues
    )
    assert result.shelves[0].order == 2


def test_provider_response_string_budget_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "budget.png"
    path.write_bytes(_png_bytes())
    source = load_diagram_source(path)
    response = _complete_response()
    response["route"]["title"] = "x" * 4097

    with pytest.raises(DiagramImportError, match="oversized string"):
        parse_provider_result(source, response)


def test_rejects_docx_traversal_and_duplicate_package_names(
    tmp_path: Path,
) -> None:
    traversal = tmp_path / "traversal.docx"
    _write_docx(
        traversal,
        document=_document_xml(["rId1"]),
        relationships=_rels_xml([("rId1", "media/image1.png")]),
        images={"word/media/image1.png": _png_bytes()},
        extras={"../outside.png": _png_bytes()},
    )
    with pytest.raises(DiagramImportError, match="path traversal|unsafe"):
        load_diagram_source(traversal)

    duplicate = tmp_path / "duplicate.docx"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("word/document.xml", _document_xml(["rId1"]))
            archive.writestr(
                "word/_rels/document.xml.rels",
                _rels_xml([("rId1", "media/image1.png")]),
            )
            archive.writestr("word/media/image1.png", _png_bytes())
    with pytest.raises(DiagramImportError, match="duplicate package name"):
        load_diagram_source(duplicate)


def test_rejects_external_images_macros_and_compression_bombs(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external.docx"
    _write_docx(
        external,
        document=_document_xml(["rId1"], linked=True),
        relationships=_rels_xml(
            [("rId1", "https://customer.example/route.png")], external=True
        ),
        images={},
    )
    with pytest.raises(DiagramImportError, match="external image"):
        load_diagram_source(external)

    macro = tmp_path / "macro.docx"
    _write_docx(
        macro,
        document=_document_xml(["rId1"]),
        relationships=_rels_xml([("rId1", "media/image1.png")]),
        images={"word/media/image1.png": _png_bytes()},
        extras={"word/vbaProject.bin": b"macro"},
    )
    with pytest.raises(DiagramImportError, match="Macro-enabled"):
        load_diagram_source(macro)

    bomb = tmp_path / "bomb.docx"
    _write_docx(
        bomb,
        document=_document_xml(["rId1"]),
        relationships=_rels_xml([("rId1", "media/image1.png")]),
        images={"word/media/image1.png": _png_bytes()},
        extras={"word/bomb.bin": b"\x00" * 100_000},
    )
    limits = DiagramImportLimits(max_compression_ratio=2.0)
    with pytest.raises(DiagramImportError, match="compression ratio"):
        load_diagram_source(bomb, limits=limits)


def test_rejects_oversized_or_extension_mismatched_raster(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.png"
    oversized.write_bytes(_png_bytes(size=(100, 100)))
    with pytest.raises(DiagramImportError, match="pixel count"):
        load_diagram_source(
            oversized,
            limits=DiagramImportLimits(max_image_pixels=9_999),
        )

    mismatch = tmp_path / "mismatch.png"
    mismatch.write_bytes(_jpeg_bytes())
    with pytest.raises(DiagramImportError, match="extension"):
        load_diagram_source(mismatch)


def test_provider_is_never_called_for_unsafe_source(tmp_path: Path) -> None:
    path = tmp_path / "not-a-route.exe"
    path.write_bytes(b"MZ")
    provider = _FakeProvider(_complete_response())

    with pytest.raises(DiagramImportError, match="Unsupported diagram type"):
        import_route_diagram(path, provider)

    assert provider.calls == []
