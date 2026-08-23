"""Fidelity and accuracy tests for the immutable-template MOP renderer."""

from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from lxml import etree
from PIL import Image

from utils.rls_config.diagram_assets import (
    DiagramAssetError,
    WorkbookDiagram,
    WorkbookDiagramImage,
    workbook_diagram_from_source,
)
from utils.rls_config.mop_export import (
    CALC_CHAIN_PART,
    DEFAULT_TEMPLATE_PATH,
    DIAGRAM_DRAWING_PART,
    DIAGRAM_DRAWING_RELS_PART,
    FBN_PART,
    IRM_PART,
    MopExportError,
    MopExportWarning,
    TEMPLATE_SHA256,
    WORKBOOK_PART,
    export_mop,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS = {"m": MAIN_NS}
Q = lambda local: f"{{{MAIN_NS}}}{local}"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


def _route_snapshot() -> dict[str, object]:
    sites = [
        {"site_key": f"site-{index:02d}", "code": f"S{index:02d}", "name": f"Site {index:02d}"}
        for index in range(1, 19)
    ]
    shelves: list[dict[str, str]] = []
    for index in range(1, 19):
        if index <= 8:
            profile = "ila"
            release = "RLS R4.0"
            variant = "ILA-A" if index <= 4 else "ILA-B"
            site_index = index
        elif index <= 16:
            profile = "roadm_a" if index % 2 else "roadm_z"
            release = "RLS R4.0"
            variant = "K74-ROADM"
            site_index = 1 if profile == "roadm_a" else 18
        else:
            profile = "add_drop_a" if index == 17 else "add_drop_z"
            release = "RLS R4.0"
            variant = "AD-A" if index == 17 else "AD-Z"
            site_index = 1 if profile == "add_drop_a" else 18
        shelves.append(
            {
                "shelf_id": f"shelf-{index:02d}",
                "profile_id": profile,
                "software_release": release,
                "shelf_variant": variant,
                "site_key": f"site-{site_index:02d}",
                "tid": f"US-TID-{index:02d}",
                "primary_oam_ip": f"10.20.0.{index}",
                "raman_label": "Slot 4" if index == 4 else "",
                "power_label": "DC-A/B",
            }
        )
    return {
        "project_id": "project-1",
        "route_code": "S01-S18",
        "title": "S01 to S18 Long Haul",
        "revision": "B",
        "sites": sites,
        "shelves": shelves,
    }


def _terminal_title_route_snapshot() -> dict[str, object]:
    route = _route_snapshot()
    route["route_code"] = "RL-0037805"
    route["title"] = "ELP1-SAT4"
    route["sites"][0]["code"] = "USELP1"
    route["sites"][-1]["code"] = "USSAT4"
    route["shelves"][0]["tid"] = "USELP1-L8R2"
    route["shelves"][-1]["tid"] = "USSAT4-L8R3"
    source_sha256 = (
        "d797ba84cb942b60d082c31bf44c63e09c508a4d3f7ac28d995f7ef352226c13"
    )
    route["diagram_source"] = {
        "source_sha256": source_sha256,
        "route_title_derivation": {
            "rule_id": "terminal-site-route-title-v1",
            "status": "controlled_derivation",
            "value": "ELP1-SAT4",
            "observed_header_pair": "USELP1-USSAT4",
            "endpoint_codes": ["USELP1", "USSAT4"],
            "display_codes": ["ELP1", "SAT4"],
            "endpoint_tids": ["USELP1-L8R2", "USSAT4-L8R3"],
            "removed_shared_prefix": "US",
            "source_sha256": source_sha256,
            "deployable_cli": False,
        },
    }
    return route


def _png_bytes(
    color: tuple[int, int, int],
    *,
    width: int = 64,
    height: int = 32,
) -> bytes:
    output = BytesIO()
    image = Image.new("RGB", (width, height), color)
    try:
        image.save(output, format="PNG", compress_level=6, optimize=False)
    finally:
        image.close()
    return output.getvalue()


def _workbook_diagram(*, duplicate: bool = False) -> WorkbookDiagram:
    first_data = _png_bytes((10, 20, 30), width=80, height=40)
    first = WorkbookDiagramImage(
        source_label="word/media/route-1.png (document image 1)",
        source_part="word/media/route-1.png",
        normalized_sha256=hashlib.sha256(first_data).hexdigest(),
        width=80,
        height=40,
        png_bytes=first_data,
    )
    if duplicate:
        second = WorkbookDiagramImage(
            source_label="word/media/route-1.png (document image 2)",
            source_part="word/media/route-1.png",
            normalized_sha256=first.normalized_sha256,
            width=80,
            height=40,
            png_bytes=first_data,
        )
    else:
        second_data = _png_bytes((40, 50, 60), width=40, height=80)
        second = WorkbookDiagramImage(
            source_label="word/media/route-2.png (document image 2)",
            source_part="word/media/route-2.png",
            normalized_sha256=hashlib.sha256(second_data).hexdigest(),
            width=40,
            height=80,
            png_bytes=second_data,
        )
    return WorkbookDiagram(
        source_file_name="customer-route.docx",
        source_type="docx",
        source_sha256="a" * 64,
        images=(first, second),
    )


def _route_with_workbook_diagram(
    diagram: WorkbookDiagram,
) -> dict[str, object]:
    route = _route_snapshot()
    route["diagram_source"] = {
        "file_name": diagram.source_file_name,
        "source_type": diagram.source_type,
        "source_sha256": diagram.source_sha256,
        "workbook_diagram": diagram.marker_dict(),
    }
    return route


def _xml(zip_file: zipfile.ZipFile, part: str) -> etree._Element:
    return etree.fromstring(zip_file.read(part))


def _cell(root: etree._Element, reference: str) -> etree._Element:
    cells = root.xpath(".//m:c[@r=$reference]", namespaces=NS, reference=reference)
    if not cells:
        raise AssertionError(f"Missing cell {reference}")
    return cells[0]


def _cell_text(root: etree._Element, reference: str) -> str:
    cell = _cell(root, reference)
    inline = cell.find(Q("is"))
    if inline is not None:
        return "".join(inline.itertext())
    value = cell.find(Q("v"))
    return "" if value is None or value.text is None else value.text


def _formula_signature(root: etree._Element) -> tuple[tuple[object, ...], ...]:
    signature = []
    for cell in root.findall(f".//{Q('c')}"):
        formula = cell.find(Q("f"))
        if formula is not None:
            signature.append(
                (
                    cell.get("r"),
                    formula.text,
                    tuple(sorted(formula.attrib.items())),
                )
            )
    return tuple(signature)


class MopExportFidelityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(
            prefix=".mop-export-tests-", dir=Path.cwd()
        )
        cls.output = Path(cls.temp_dir.name) / "route_mop.xlsx"
        cls.source_hash_before = hashlib.sha256(
            DEFAULT_TEMPLATE_PATH.read_bytes()
        ).hexdigest()
        export_mop(_route_snapshot(), cls.output)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_valid_zip_and_template_source_is_untouched(self) -> None:
        self.assertEqual(TEMPLATE_SHA256, self.source_hash_before)
        self.assertEqual(
            self.source_hash_before,
            hashlib.sha256(DEFAULT_TEMPLATE_PATH.read_bytes()).hexdigest(),
        )
        with zipfile.ZipFile(self.output) as result:
            self.assertIsNone(result.testzip())

    def test_every_non_dynamic_package_part_is_byte_exact(self) -> None:
        changed_parts = {
            FBN_PART,
            IRM_PART,
            WORKBOOK_PART,
            CALC_CHAIN_PART,
        }
        with zipfile.ZipFile(DEFAULT_TEMPLATE_PATH) as source, zipfile.ZipFile(
            self.output
        ) as result:
            self.assertEqual(source.namelist(), result.namelist())
            for part in source.namelist():
                if part not in changed_parts:
                    self.assertEqual(
                        source.read(part),
                        result.read(part),
                        f"Static OOXML part was rewritten: {part}",
                    )

    def test_irm_formula_nodes_are_preserved_exactly_and_recalculate(self) -> None:
        with zipfile.ZipFile(DEFAULT_TEMPLATE_PATH) as source, zipfile.ZipFile(
            self.output
        ) as result:
            original = _xml(source, IRM_PART)
            rendered = _xml(result, IRM_PART)
            original_formulas = _formula_signature(original)
            rendered_formulas = _formula_signature(rendered)
            self.assertEqual(186, len(original_formulas))
            self.assertEqual(original_formulas, rendered_formulas)
            for formula in rendered.findall(f".//{Q('f')}"):
                self.assertIsNone(formula.getparent().find(Q("v")))

            sheet_view = rendered.find(f"{Q('sheetViews')}/{Q('sheetView')}")
            self.assertEqual("0", sheet_view.get("showFormulas"))

            workbook = _xml(result, WORKBOOK_PART)
            calc = workbook.find(Q("calcPr"))
            self.assertEqual("auto", calc.get("calcMode"))
            self.assertEqual("1", calc.get("fullCalcOnLoad"))
            self.assertEqual("1", calc.get("forceFullCalc"))

            chain = _xml(result, CALC_CHAIN_PART)
            chain_cells = chain.findall(Q("c"))
            self.assertEqual(186, len(chain_cells))
            current_sheet = None
            chain_references = []
            for chain_cell in chain_cells:
                current_sheet = chain_cell.get("i", current_sheet)
                self.assertEqual("30", current_sheet)
                chain_references.append(chain_cell.get("r"))
            self.assertCountEqual(
                [
                    cell.get("r")
                    for cell in rendered.findall(f".//{Q('c')}")
                    if cell.find(Q("f")) is not None
                ],
                chain_references,
            )

    def test_ordered_shelves_are_chunked_at_eight_per_rack(self) -> None:
        with zipfile.ZipFile(self.output) as result:
            fbn = _xml(result, FBN_PART)
            self.assertEqual("RACK 1 (FRONT)", _cell_text(fbn, "F2"))
            self.assertEqual("RACK 2 (FRONT)", _cell_text(fbn, "N2"))
            self.assertEqual("RACK 3 (FRONT)", _cell_text(fbn, "V2"))

            all_values = {
                cell.get("r"): "".join(cell.find(Q("is")).itertext())
                for cell in fbn.findall(f".//{Q('c')}")
                if cell.find(Q("is")) is not None
            }
            rack_columns = ("F", "N", "V")
            expected = (
                {f"US-TID-{index:02d}" for index in range(1, 9)},
                {f"US-TID-{index:02d}" for index in range(9, 17)},
                {f"US-TID-{index:02d}" for index in range(17, 19)},
            )
            for column, expected_tids in zip(rack_columns, expected):
                actual_tids = {
                    value
                    for reference, value in all_values.items()
                    if reference.startswith(column)
                    and 10 <= int(reference[len(column) :]) <= 49
                    and value.startswith("US-TID-")
                }
                self.assertEqual(expected_tids, actual_tids)
                self.assertLessEqual(len(actual_tids), 8)

    def test_register_support_column_and_print_area_follow_third_rack(self) -> None:
        with zipfile.ZipFile(self.output) as result:
            fbn = _xml(result, FBN_PART)
            self.assertEqual("SITE", _cell_text(fbn, "AB15"))
            self.assertEqual("TID", _cell_text(fbn, "AC15"))
            self.assertEqual("IP", _cell_text(fbn, "AD15"))
            self.assertEqual("RAMAN", _cell_text(fbn, "AE15"))
            self.assertEqual("POWER", _cell_text(fbn, "AF15"))
            self.assertEqual("PROFILE_ID", _cell_text(fbn, "AH15"))
            self.assertEqual("Site 01", _cell_text(fbn, "AB16"))
            self.assertEqual("US-TID-18", _cell_text(fbn, "AC33"))
            self.assertEqual("add_drop_z", _cell_text(fbn, "AH33"))
            self.assertEqual("422", _cell(fbn, "AE16").get("s"))
            self.assertEqual("292", _cell(fbn, "AE19").get("s"))

            hidden_support = fbn.xpath(
                "./m:cols/m:col[@min='34' and @max='34' and @hidden='1']",
                namespaces=NS,
            )
            self.assertEqual(1, len(hidden_support))

            workbook = _xml(result, WORKBOOK_PART)
            print_areas = workbook.xpath(
                "./m:definedNames/m:definedName"
                "[@name='_xlnm.Print_Area' and @localSheetId='0']",
                namespaces=NS,
            )
            self.assertEqual(1, len(print_areas))
            self.assertEqual("'FBN'!$D$1:$AJ$52", print_areas[0].text)

            page_setup = fbn.find(Q("pageSetup"))
            self.assertEqual("landscape", page_setup.get("orientation"))
            self.assertEqual("0", page_setup.get("fitToWidth"))
            self.assertEqual("1", page_setup.get("fitToHeight"))
            self.assertIsNone(page_setup.get("scale"))
            breaks = fbn.findall(f"{Q('colBreaks')}/{Q('brk')}")
            self.assertEqual(["19"], [item.get("id") for item in breaks])
            child_names = [etree.QName(child).localname for child in fbn]
            self.assertGreater(
                child_names.index("colBreaks"),
                child_names.index("pageSetup"),
            )

    def test_summary_uses_real_per_family_release_and_variant(self) -> None:
        with zipfile.ZipFile(self.output) as result:
            fbn = _xml(result, FBN_PART)
            # Every family uses the fixed R4.0 contract; variants remain honest.
            self.assertEqual("RLS R4.0 ADD/DROP", _cell_text(fbn, "AB8"))
            self.assertEqual("(2) MIXED Shelves", _cell_text(fbn, "AB9"))
            self.assertEqual("RLS R4.0 ROADM", _cell_text(fbn, "AD8"))
            self.assertEqual("(8) K74-ROADM Shelves", _cell_text(fbn, "AD9"))
            self.assertEqual("RLS R4.0 ILAs", _cell_text(fbn, "AF8"))
            self.assertEqual("(8) MIXED Shelves", _cell_text(fbn, "AF9"))

    def test_irm_drivers_match_exact_fbn_profile_register(self) -> None:
        with zipfile.ZipFile(self.output) as result:
            fbn = _xml(result, FBN_PART)
            irm = _xml(result, IRM_PART)
            profiles = [_cell_text(fbn, f"AH{row}") for row in range(16, 34)]
            self.assertEqual("S01 to S18 Long Haul", _cell_text(irm, "B2"))
            self.assertEqual("S01", _cell_text(irm, "B3"))
            self.assertEqual("S18", _cell_text(irm, "B5"))
            self.assertEqual("9", _cell_text(irm, "B6"))
            self.assertEqual(str(profiles.count("ila")), _cell_text(irm, "B7"))
            self.assertEqual(
                str(profiles.count("roadm_a") + profiles.count("roadm_z")),
                _cell_text(irm, "B9"),
            )
            self.assertEqual(
                str(profiles.count("add_drop_a") + profiles.count("add_drop_z")),
                _cell_text(irm, "B10"),
            )
            self.assertEqual("8", _cell_text(irm, "F18"))
            self.assertEqual("8", _cell_text(irm, "G18"))
            self.assertEqual("1", _cell_text(irm, "H18"))
            self.assertEqual("1", _cell_text(irm, "I18"))


class MopDiagramEmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix=".mop-diagram-tests-",
            dir=Path.cwd(),
        )
        self.output = Path(self.temp_dir.name) / "diagram.xlsx"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_diagram_pictures_use_internal_relationships_and_preserve_sheet(self) -> None:
        diagram = _workbook_diagram()
        export_mop(
            _route_with_workbook_diagram(diagram),
            self.output,
            diagram=diagram,
        )

        with zipfile.ZipFile(DEFAULT_TEMPLATE_PATH) as source, zipfile.ZipFile(
            self.output
        ) as result:
            self.assertIsNone(result.testzip())
            self.assertEqual(
                source.read("xl/worksheets/sheet4.xml"),
                result.read("xl/worksheets/sheet4.xml"),
            )
            self.assertEqual(
                source.read("xl/worksheets/_rels/sheet4.xml.rels"),
                result.read("xl/worksheets/_rels/sheet4.xml.rels"),
            )
            self.assertNotEqual(
                source.read(DIAGRAM_DRAWING_PART),
                result.read(DIAGRAM_DRAWING_PART),
            )
            drawing = etree.fromstring(result.read(DIAGRAM_DRAWING_PART))
            pictures = drawing.xpath(
                ".//xdr:pic",
                namespaces={"xdr": XDR_NS},
            )
            self.assertEqual(2, len(pictures))
            descriptions = drawing.xpath(
                ".//xdr:pic/xdr:nvPicPr/xdr:cNvPr/@descr",
                namespaces={"xdr": XDR_NS},
            )
            self.assertIn(diagram.source_sha256, descriptions[0])
            self.assertIn(diagram.images[0].normalized_sha256, descriptions[0])
            self.assertIn(diagram.images[1].normalized_sha256, descriptions[1])

            relationships = etree.fromstring(
                result.read(DIAGRAM_DRAWING_RELS_PART)
            )
            items = relationships.findall(
                f"{{{DRAWING_REL_NS}}}Relationship"
            )
            self.assertEqual(2, len(items))
            for relationship in items:
                self.assertIsNone(relationship.get("TargetMode"))
                target = relationship.get("Target")
                self.assertRegex(
                    target or "",
                    r"^\.\./media/atlas_route_diagram_[0-9]{3}\.png$",
                )
                part_name = "xl/" + (target or "").removeprefix("../")
                self.assertIn(part_name, result.namelist())

            for order, image in enumerate(diagram.images, start=1):
                part = f"xl/media/atlas_route_diagram_{order:03d}.png"
                self.assertEqual(image.png_bytes, result.read(part))
                self.assertEqual(
                    image.normalized_sha256,
                    hashlib.sha256(result.read(part)).hexdigest(),
                )

            allowed_changes = {
                FBN_PART,
                IRM_PART,
                WORKBOOK_PART,
                CALC_CHAIN_PART,
                DIAGRAM_DRAWING_PART,
            }
            for part in source.namelist():
                if part not in allowed_changes:
                    self.assertEqual(
                        source.read(part),
                        result.read(part),
                        f"Unrelated template part changed: {part}",
                    )

    def test_repeated_docx_occurrence_reuses_one_internal_media_part(self) -> None:
        diagram = _workbook_diagram(duplicate=True)
        export_mop(
            _route_with_workbook_diagram(diagram),
            self.output,
            diagram=diagram,
        )

        with zipfile.ZipFile(self.output) as workbook:
            drawing = etree.fromstring(workbook.read(DIAGRAM_DRAWING_PART))
            embeds = drawing.xpath(
                ".//xdr:pic/xdr:blipFill/a:blip/@r:embed",
                namespaces={
                    "xdr": XDR_NS,
                    "a": (
                        "http://schemas.openxmlformats.org/drawingml/2006/main"
                    ),
                    "r": OFFICE_REL_NS,
                },
            )
            self.assertEqual(["rId1", "rId1"], embeds)
            relationships = etree.fromstring(
                workbook.read(DIAGRAM_DRAWING_RELS_PART)
            )
            self.assertEqual(
                1,
                len(
                    relationships.findall(
                        f"{{{DRAWING_REL_NS}}}Relationship"
                    )
                ),
            )
            media = [
                part
                for part in workbook.namelist()
                if part.startswith("xl/media/atlas_route_diagram_")
            ]
            self.assertEqual(
                ["xl/media/atlas_route_diagram_001.png"],
                media,
            )

    def test_source_selection_keeps_docx_order_and_omits_raster_detail_tiles(
        self,
    ) -> None:
        overview_data = _png_bytes((1, 1, 1), width=20, height=10)
        detail_data = _png_bytes((2, 2, 2), width=8, height=8)

        def source_image(
            label: str,
            data: bytes,
            *,
            view_kind: str,
            source_index: int,
            width: int,
            height: int,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                source_label=label,
                source_part=label,
                normalized_sha256=hashlib.sha256(data).hexdigest(),
                width=width,
                height=height,
                format="png",
                data=data,
                view_kind=view_kind,
                source_image_index=source_index,
            )

        raster = workbook_diagram_from_source(
            SimpleNamespace(
                file_name="route.png",
                source_type="png",
                sha256="b" * 64,
                images=(
                    source_image(
                        "route.png",
                        overview_data,
                        view_kind="overview",
                        source_index=0,
                        width=20,
                        height=10,
                    ),
                    source_image(
                        "route.png#detail-1",
                        detail_data,
                        view_kind="detail",
                        source_index=0,
                        width=8,
                        height=8,
                    ),
                ),
            )
        )
        self.assertEqual(["route.png"], [item.source_label for item in raster.images])

        docx = workbook_diagram_from_source(
            SimpleNamespace(
                file_name="route.docx",
                source_type="docx",
                sha256="c" * 64,
                images=(
                    source_image(
                        "document image 2",
                        detail_data,
                        view_kind="source",
                        source_index=0,
                        width=8,
                        height=8,
                    ),
                    source_image(
                        "document image 1",
                        overview_data,
                        view_kind="source",
                        source_index=1,
                        width=20,
                        height=10,
                    ),
                ),
            )
        )
        self.assertEqual(
            ["document image 2", "document image 1"],
            [item.source_label for item in docx.images],
        )

    def test_required_missing_diagram_fails_without_replacing_destination(self) -> None:
        diagram = _workbook_diagram()
        self.output.write_bytes(b"existing-workbook")

        with self.assertRaisesRegex(
            MopExportError,
            "must be reattached",
        ):
            export_mop(
                _route_with_workbook_diagram(diagram),
                self.output,
            )

        self.assertEqual(b"existing-workbook", self.output.read_bytes())

    def test_supplied_diagram_requires_project_hash_provenance(self) -> None:
        diagram = _workbook_diagram()
        self.output.write_bytes(b"existing-workbook")

        with self.assertRaisesRegex(
            MopExportError,
            "requires project workbook-diagram provenance",
        ):
            export_mop(
                _route_snapshot(),
                self.output,
                diagram=diagram,
            )

        self.assertEqual(b"existing-workbook", self.output.read_bytes())

    def test_invalid_png_hash_and_dimensions_are_rejected(self) -> None:
        data = _png_bytes((1, 2, 3), width=10, height=5)
        with self.assertRaisesRegex(DiagramAssetError, "SHA-256"):
            WorkbookDiagramImage(
                source_label="route.png",
                source_part="route.png",
                normalized_sha256="0" * 64,
                width=10,
                height=5,
                png_bytes=data,
            )
        with self.assertRaisesRegex(DiagramAssetError, "dimensions"):
            WorkbookDiagramImage(
                source_label="route.png",
                source_part="route.png",
                normalized_sha256=hashlib.sha256(data).hexdigest(),
                width=11,
                height=5,
                png_bytes=data,
            )
        with self.assertRaisesRegex(DiagramAssetError, "not a PNG"):
            WorkbookDiagramImage(
                source_label="route.png",
                source_part="route.png",
                normalized_sha256=hashlib.sha256(b"not-png").hexdigest(),
                width=1,
                height=1,
                png_bytes=b"not-png",
            )
        rgba_output = BytesIO()
        Image.new("RGBA", (4, 2), (1, 2, 3, 127)).save(
            rgba_output,
            format="PNG",
        )
        rgba_data = rgba_output.getvalue()
        with self.assertRaisesRegex(DiagramAssetError, "opaque RGB"):
            WorkbookDiagramImage(
                source_label="route.png",
                source_part="route.png",
                normalized_sha256=hashlib.sha256(rgba_data).hexdigest(),
                width=4,
                height=2,
                png_bytes=rgba_data,
            )

    def test_export_revalidates_tampered_bytes_before_atomic_replace(self) -> None:
        diagram = _workbook_diagram()
        object.__setattr__(
            diagram.images[0],
            "png_bytes",
            diagram.images[0].png_bytes + b"tampered",
        )
        self.output.write_bytes(b"existing-workbook")

        with self.assertRaisesRegex(MopExportError, "SHA-256"):
            export_mop(
                _route_with_workbook_diagram(diagram),
                self.output,
                diagram=diagram,
            )

        self.assertEqual(b"existing-workbook", self.output.read_bytes())

    def test_preview_and_final_embed_identical_diagram_parts(self) -> None:
        diagram = _workbook_diagram()
        route = _route_with_workbook_diagram(diagram)
        preview = self.output.with_name("preview-diagram.xlsx")
        final = self.output.with_name("final-diagram.xlsx")
        export_mop(route, preview, purpose="preview", diagram=diagram)
        export_mop(route, final, purpose="final", diagram=diagram)

        with zipfile.ZipFile(preview) as preview_zip, zipfile.ZipFile(
            final
        ) as final_zip:
            diagram_parts = [
                DIAGRAM_DRAWING_PART,
                DIAGRAM_DRAWING_RELS_PART,
                *[
                    part
                    for part in preview_zip.namelist()
                    if part.startswith("xl/media/atlas_route_diagram_")
                ],
            ]
            for part in diagram_parts:
                self.assertEqual(
                    preview_zip.read(part),
                    final_zip.read(part),
                    f"Preview changed Diagram part {part}",
                )


class MopExportValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix=".mop-validation-tests-", dir=Path.cwd()
        )
        self.output = Path(self.temp_dir.name) / "result.xlsx"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_preview_has_visible_watermark_while_final_is_construction_clean(
        self,
    ) -> None:
        preview = self.output.with_name("preview.xlsx")
        final = self.output.with_name("final.xlsx")

        export_mop(_route_snapshot(), preview, purpose="preview")
        export_mop(_route_snapshot(), final, purpose="final")

        with zipfile.ZipFile(preview) as preview_zip, zipfile.ZipFile(
            final
        ) as final_zip:
            preview_fbn = _xml(preview_zip, FBN_PART)
            final_fbn = _xml(final_zip, FBN_PART)
            self.assertEqual(
                "PREVIEW — NOT FOR CONSTRUCTION • REV B",
                _cell_text(preview_fbn, "AB11"),
            )
            self.assertEqual(
                "CIENA ROUTE DELIVERABLE • REV B",
                _cell_text(final_fbn, "AB11"),
            )
            self.assertNotEqual(
                preview_zip.read(FBN_PART),
                final_zip.read(FBN_PART),
            )
            for part in preview_zip.namelist():
                if part != FBN_PART:
                    self.assertEqual(
                        preview_zip.read(part),
                        final_zip.read(part),
                        f"Preview unexpectedly changed non-FBN part: {part}",
                    )

    def test_unknown_mop_purpose_is_rejected_before_writing(self) -> None:
        with self.assertRaisesRegex(
            MopExportError, "purpose must be 'final' or 'preview'"
        ):
            export_mop(
                _route_snapshot(),
                self.output,
                purpose="construction",  # type: ignore[arg-type]
            )
        self.assertFalse(self.output.exists())

    def test_preview_renders_review_placeholders_for_incomplete_diagram_draft(
        self,
    ) -> None:
        draft = {
            "route_code": "DRAFT-ROUTE",
            "title": "Customer diagram draft",
            "revision": "0",
            "shelves": [
                {
                    "shelf_id": "diagram-shelf-1",
                    "profile_id": "ila",
                }
            ],
        }

        export_mop(draft, self.output, purpose="preview")

        with zipfile.ZipFile(self.output) as workbook:
            fbn = _xml(workbook, FBN_PART)
            self.assertEqual(
                "PREVIEW — NOT FOR CONSTRUCTION • REV 0",
                _cell_text(fbn, "L11"),
            )
            self.assertEqual("REVIEW REQUIRED", _cell_text(fbn, "L16"))
            self.assertEqual("REVIEW TID 1", _cell_text(fbn, "M16"))
            self.assertEqual("REVIEW OAM 1", _cell_text(fbn, "N16"))
            self.assertEqual("REVIEW REQUIRED", _cell_text(fbn, "P16"))

        final = self.output.with_name("final-draft.xlsx")
        with self.assertRaisesRegex(MopExportError, "has no site"):
            export_mop(draft, final, purpose="final")
        self.assertFalse(final.exists())

    def test_plain_snapshots_cannot_bypass_required_accuracy_fields(self) -> None:
        mutations = (
            ("primary_oam_ip", "", "primary OAM IP"),
            ("power_label", "", "power"),
            ("software_release", "", "software release"),
            ("shelf_variant", "", "shelf variant"),
            ("profile_id", "made_up_profile", "unknown shelf profile"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                route = _route_snapshot()
                route["shelves"][0][field] = value
                with self.assertRaisesRegex(MopExportError, message):
                    export_mop(route, self.output)
                self.assertFalse(self.output.exists())

    def test_plain_snapshot_requires_real_site_code_and_name(self) -> None:
        for field in ("code", "name"):
            with self.subTest(field=field):
                route = _route_snapshot()
                route["sites"][0][field] = ""
                expected = "site code" if field == "code" else "site name"
                with self.assertRaisesRegex(MopExportError, expected):
                    export_mop(route, self.output)

    def test_plain_snapshot_rejects_duplicate_tid_and_canonical_oam_ip(self) -> None:
        route = _route_snapshot()
        route["shelves"][1]["tid"] = route["shelves"][0]["tid"].lower()
        with self.assertRaisesRegex(MopExportError, "Duplicate TID"):
            export_mop(route, self.output)

        route = _route_snapshot()
        route["shelves"][0]["primary_oam_ip"] = "2001:db8::1"
        route["shelves"][1]["primary_oam_ip"] = (
            "2001:0db8:0000:0000:0000:0000:0000:0001"
        )
        with self.assertRaisesRegex(MopExportError, "Duplicate primary OAM IP"):
            export_mop(route, self.output)

        route = _route_snapshot()
        route["sites"][1]["code"] = route["sites"][0]["code"].lower()
        with self.assertRaisesRegex(MopExportError, "Duplicate site code"):
            export_mop(route, self.output)

    def test_plain_snapshot_enforces_side_profiles_at_ordered_endpoints(self) -> None:
        route = _route_snapshot()
        route["shelves"][16]["site_key"] = "site-02"
        with self.assertRaisesRegex(MopExportError, "first ordered route site"):
            export_mop(route, self.output)

        route = _route_snapshot()
        route["shelves"][17]["site_key"] = "site-02"
        with self.assertRaisesRegex(MopExportError, "last ordered route site"):
            export_mop(route, self.output)

    def test_irm_endpoints_use_ordered_distinct_sites_not_last_shelf(self) -> None:
        route = _route_snapshot()
        final = copy.deepcopy(route["shelves"][0])
        final.update(
            {
                "shelf_id": "shelf-19",
                "tid": "US-TID-19",
                "primary_oam_ip": "10.20.0.19",
                "profile_id": "ila",
            }
        )
        route["shelves"].append(final)
        export_mop(route, self.output)

        with zipfile.ZipFile(self.output) as result:
            irm = _xml(result, IRM_PART)
            self.assertEqual("S01", _cell_text(irm, "B3"))
            self.assertEqual("S18", _cell_text(irm, "B5"))

    def test_irm_uses_source_bound_terminal_title_display_codes(self) -> None:
        route = _terminal_title_route_snapshot()

        export_mop(route, self.output)

        with zipfile.ZipFile(self.output) as result:
            irm = _xml(result, IRM_PART)
            self.assertEqual("ELP1-SAT4", _cell_text(irm, "B2"))
            self.assertEqual("ELP1", _cell_text(irm, "B3"))
            self.assertEqual("SAT4", _cell_text(irm, "B5"))

        # Display-only title semantics must not rewrite reviewed site data.
        self.assertEqual("USELP1", route["sites"][0]["code"])
        self.assertEqual("USSAT4", route["sites"][-1]["code"])

    def test_irm_terminal_title_marker_fails_closed_to_reviewed_sites(
        self,
    ) -> None:
        invalid_updates = (
            ("rule", {"rule_id": "unapproved-rule"}),
            ("status", {"status": "candidate"}),
            ("value", {"value": "DIFFERENT-TITLE"}),
            ("code_count", {"display_codes": ["ELP1"]}),
            ("code_shape", {"display_codes": ["ELP 1", "SAT4"]}),
            ("code_title", {"display_codes": ["ELP1", "OTHER"]}),
            ("endpoint", {"endpoint_tids": ["USELP1-L8R2", "OTHER-L8R3"]}),
            ("endpoint_codes", {"endpoint_codes": ["USELP1", "OTHER"]}),
            ("header_pair", {"observed_header_pair": "OTHER-PAIR"}),
            ("removed_prefix", {"removed_shared_prefix": "CA"}),
            ("source", {"source_sha256": "different-source"}),
            ("cli_flag", {"deployable_cli": True}),
        )
        for label, updates in invalid_updates:
            with self.subTest(label=label):
                route = _terminal_title_route_snapshot()
                marker = route["diagram_source"]["route_title_derivation"]
                marker.update(updates)
                output = self.output.with_name(f"fallback-{label}.xlsx")

                export_mop(route, output)

                with zipfile.ZipFile(output) as result:
                    irm = _xml(result, IRM_PART)
                    self.assertEqual("ELP1-SAT4", _cell_text(irm, "B2"))
                    self.assertEqual("USELP1", _cell_text(irm, "B3"))
                    self.assertEqual("USSAT4", _cell_text(irm, "B5"))

    def test_large_route_adds_manual_breaks_every_two_racks(self) -> None:
        route = {
            "route_code": "LARGE-ROUTE",
            "title": "Large Route",
            "revision": "1",
            "sites": [
                {
                    "site_key": f"site-{index:02d}",
                    "code": f"S{index:02d}",
                    "name": f"Site {index:02d}",
                }
                for index in range(1, 81)
            ],
            "shelves": [
                {
                    "shelf_id": f"shelf-{index:02d}",
                    "profile_id": "ila",
                    "software_release": "R2",
                    "shelf_variant": "K74-C894-900",
                    "site_key": f"site-{index:02d}",
                    "tid": f"TID-{index:02d}",
                    "primary_oam_ip": f"10.30.0.{index}",
                    "raman_label": "",
                    "power_label": "DC-A/B",
                }
                for index in range(1, 81)
            ],
        }
        export_mop(route, self.output)

        with zipfile.ZipFile(self.output) as result:
            fbn = _xml(result, FBN_PART)
            breaks = fbn.findall(f"{Q('colBreaks')}/{Q('brk')}")
            self.assertEqual(
                ["19", "35", "51", "67"],
                [item.get("id") for item in breaks],
            )

    def test_assert_valid_is_called_and_wrapped(self) -> None:
        class InvalidRoute:
            def __init__(self) -> None:
                self.called = False

            def assert_valid(self) -> None:
                self.called = True
                raise RuntimeError("bad route data")

        project = InvalidRoute()
        with self.assertRaisesRegex(
            MopExportError, "Route project validation failed: bad route data"
        ):
            export_mop(project, self.output)
        self.assertTrue(project.called)
        self.assertFalse(self.output.exists())

    def test_retired_protected_dci_profile_is_rejected(self) -> None:
        route = _route_snapshot()
        route["sites"] = route["sites"][:1]
        shelf = copy.deepcopy(route["shelves"][0])
        shelf.update(
            {
                "profile_id": "protected_dci",
                "software_release": "R4.2",
                "shelf_variant": "6500 RLS",
            }
        )
        route["shelves"] = [shelf]
        with self.assertRaisesRegex(MopExportError, "unknown shelf profile"):
            export_mop(route, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
