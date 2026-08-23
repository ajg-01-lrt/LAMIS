"""High-fidelity FBN/IRM workbook export for Ciena RLS route projects.

The supplied MOP workbook contains a large amount of DrawingML, embedded
media, printer metadata, and custom XML.  Loading and saving that workbook
through a general spreadsheet library rewrites or drops some of those parts.
This module therefore treats the workbook as an immutable OPC/OOXML package:
all package parts are copied byte-for-byte except the intentionally dynamic
FBN/IRM content, optional Diagram drawing, and calculation/print-area metadata.

No CLI configuration is generated here.  Profiles without an approved CLI
provider (including planning-only Add/Drop, ILA, and ROADM profiles) remain
useful in the FBN deliverable but are never promoted to deployable config by
this exporter.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import math
import os
import re
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lxml import etree

from utils.helpers import get_data_dir
from .diagram_assets import (
    DiagramAssetError,
    WorkbookDiagram,
    validate_workbook_diagram_for_project,
)


TEMPLATE_FILENAME = "Ciena_RLS_FBN_MOP_Template.xlsx"
TEMPLATE_SHA256 = (
    "a1ad017b977441d657d7e4e500343ead3fe7ffea5e692d749c6d7ac960e71e11"
)
DEFAULT_TEMPLATE_PATH = get_data_dir() / "templates" / TEMPLATE_FILENAME

FBN_PART = "xl/worksheets/sheet1.xml"
IRM_PART = "xl/worksheets/sheet6.xml"
WORKBOOK_PART = "xl/workbook.xml"
CALC_CHAIN_PART = "xl/calcChain.xml"
DIAGRAM_DRAWING_PART = "xl/drawings/drawing1.xml"
DIAGRAM_DRAWING_RELS_PART = "xl/drawings/_rels/drawing1.xml.rels"

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"m": MAIN_NS}
Q = lambda local: f"{{{MAIN_NS}}}{local}"
XDR_Q = lambda local: f"{{{DRAWING_NS}}}{local}"
A_Q = lambda local: f"{{{DRAWING_MAIN_NS}}}{local}"
PACKAGE_REL_Q = lambda local: f"{{{PACKAGE_REL_NS}}}{local}"

_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_INVALID_XML_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)
_TERMINAL_ROUTE_DISPLAY_TOKEN_RE = re.compile(r"^[A-Z0-9]{2,32}$")
_TERMINAL_ROUTE_TITLE_RULE_ID = "terminal-site-route-title-v1"

_ILA_PROFILES = frozenset({"ila"})
_ROADM_PROFILES = frozenset({"roadm_a", "roadm_z", "roadm"})
_ADD_DROP_A_PROFILES = frozenset({"add_drop_a"})
_ADD_DROP_Z_PROFILES = frozenset({"add_drop_z"})
_ADD_DROP_UNASSIGNED_PROFILES = frozenset({"add_drop"})
_IRM_MAPPED_PROFILES = (
    _ILA_PROFILES
    | _ROADM_PROFILES
    | _ADD_DROP_A_PROFILES
    | _ADD_DROP_Z_PROFILES
)
_KNOWN_PROFILES = (
    _IRM_MAPPED_PROFILES
    | _ADD_DROP_UNASSIGNED_PROFILES
)


class MopExportError(ValueError):
    """Raised when a route cannot be rendered into a valid MOP workbook."""


class MopExportWarning(UserWarning):
    """Warning emitted when a route contains shelves not represented by IRM."""


@dataclass(frozen=True)
class _ShelfSnapshot:
    shelf_id: str
    profile_id: str
    software_release: str
    shelf_variant: str
    site_key: str
    site_code: str
    site_name: str
    tid: str
    primary_oam_ip: str
    raman_label: str
    power_label: str


@dataclass(frozen=True)
class _RouteSnapshot:
    route_code: str
    title: str
    revision: str
    shelves: tuple[_ShelfSnapshot, ...]
    irm_endpoint_display_codes: tuple[str, str] | None


@dataclass(frozen=True)
class _ProfileCounts:
    total_sites: int
    ila: int
    roadm: int
    add_drop_a: int
    add_drop_z: int
    add_drop_unassigned: int
    unmapped: int

    @property
    def add_drop(self) -> int:
        return (
            self.add_drop_a
            + self.add_drop_z
            + self.add_drop_unassigned
        )


def export_mop(
    project: Any,
    output_path: str | os.PathLike[str],
    *,
    template_path: str | os.PathLike[str] | None = None,
    purpose: str = "final",
    diagram: WorkbookDiagram | None = None,
) -> Path:
    """Export *project* as a styled FBN/IRM MOP workbook.

    ``project`` may be a ``RouteProject`` instance or a mapping with equivalent
    fields.  Shelves are kept in input order; every consecutive group of eight
    becomes one rack diagram.  The returned path is absolute.

    The write is atomic.  The known template hash is verified before any
    output is replaced, and the template itself may not be used as the output.
    """

    if purpose not in {"final", "preview"}:
        raise MopExportError("MOP purpose must be 'final' or 'preview'.")

    validator = getattr(project, "assert_valid", None)
    if purpose == "final" and callable(validator):
        try:
            validator()
        except Exception as exc:
            raise MopExportError(f"Route project validation failed: {exc}") from exc

    snapshot = _snapshot_project(project, preview=purpose == "preview")
    counts = _profile_counts(snapshot)

    source = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
    source = source.expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()

    if source == destination:
        raise MopExportError("The immutable MOP template cannot be overwritten.")
    if not source.is_file():
        raise MopExportError(f"MOP template not found: {source}")
    if source.suffix.lower() != ".xlsx":
        raise MopExportError("The MOP template must be an .xlsx workbook.")
    if destination.suffix.lower() != ".xlsx":
        raise MopExportError("The MOP output path must end in .xlsx.")

    actual_hash = _sha256_file(source)
    if actual_hash != TEMPLATE_SHA256:
        raise MopExportError(
            "The MOP template does not match the validated immutable source "
            f"(expected SHA-256 {TEMPLATE_SHA256}, got {actual_hash})."
        )
    try:
        diagram = validate_workbook_diagram_for_project(project, diagram)
    except DiagramAssetError as exc:
        raise MopExportError(f"Diagram tab rendering failed: {exc}") from exc

    if counts.unmapped:
        warnings.warn(
            f"{counts.unmapped} shelf(s) are present in FBN but have no IRM "
            "family mapping; they are intentionally excluded from IRM counts.",
            MopExportWarning,
            stacklevel=2,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with zipfile.ZipFile(source, "r") as source_zip:
            required = {
                FBN_PART,
                IRM_PART,
                WORKBOOK_PART,
                CALC_CHAIN_PART,
                DIAGRAM_DRAWING_PART,
            }
            missing = required.difference(source_zip.namelist())
            if missing:
                raise MopExportError(
                    "MOP template is missing required OOXML parts: "
                    + ", ".join(sorted(missing))
                )
            if diagram is not None:
                if DIAGRAM_DRAWING_RELS_PART in source_zip.namelist():
                    raise MopExportError(
                        "MOP template Diagram drawing unexpectedly has "
                        "relationships."
                    )
                content_types = source_zip.read("[Content_Types].xml")
                if not re.search(
                    rb"<Default\b[^>]*\bExtension=[\"']png[\"']",
                    content_types,
                    flags=re.IGNORECASE,
                ):
                    raise MopExportError(
                        "MOP template has no PNG content-type declaration."
                    )

            fbn_xml = _render_fbn(
                source_zip.read(FBN_PART),
                snapshot,
                counts,
                preview=purpose == "preview",
            )
            irm_xml = _render_irm(source_zip.read(IRM_PART), snapshot, counts)
            print_area = _fbn_print_area(snapshot)
            workbook_xml = _render_workbook(
                source_zip.read(WORKBOOK_PART), print_area
            )
            calc_chain_xml = _render_calc_chain(source_zip.read(CALC_CHAIN_PART))
            diagram_drawing_xml: bytes | None = None
            diagram_relationships_xml: bytes | None = None
            diagram_media: Mapping[str, bytes] = {}
            if diagram is not None:
                (
                    diagram_drawing_xml,
                    diagram_relationships_xml,
                    diagram_media,
                ) = _render_diagram_drawing(
                    source_zip.read(DIAGRAM_DRAWING_PART),
                    diagram,
                )
                collisions = set(diagram_media).intersection(
                    source_zip.namelist()
                )
                if collisions:
                    raise MopExportError(
                        "MOP template already contains renderer-owned Diagram "
                        "media: "
                        + ", ".join(sorted(collisions))
                    )

            fd, raw_temp = tempfile.mkstemp(
                prefix=f".{destination.stem}.",
                suffix=".tmp.xlsx",
                dir=str(destination.parent),
            )
            os.close(fd)
            temp_path = Path(raw_temp)
            replacements = {
                FBN_PART: fbn_xml,
                IRM_PART: irm_xml,
                WORKBOOK_PART: workbook_xml,
                CALC_CHAIN_PART: calc_chain_xml,
            }
            if diagram_drawing_xml is not None:
                replacements[DIAGRAM_DRAWING_PART] = diagram_drawing_xml
            with zipfile.ZipFile(
                temp_path, "w", allowZip64=True, strict_timestamps=False
            ) as output_zip:
                for info in source_zip.infolist():
                    data = replacements.get(info.filename)
                    if data is None:
                        data = source_zip.read(info.filename)
                    # ZipFile.writestr mutates the supplied ZipInfo (CRC and
                    # sizes).  A copy keeps the immutable source package's
                    # in-memory directory records untouched as well.
                    output_zip.writestr(copy.copy(info), data)
                if diagram_relationships_xml is not None:
                    _write_new_zip_part(
                        output_zip,
                        DIAGRAM_DRAWING_RELS_PART,
                        diagram_relationships_xml,
                    )
                for part_name in sorted(diagram_media):
                    _write_new_zip_part(
                        output_zip,
                        part_name,
                        diagram_media[part_name],
                    )

        with zipfile.ZipFile(temp_path, "r") as check_zip:
            broken_part = check_zip.testzip()
            if broken_part is not None:
                raise MopExportError(
                    f"Generated workbook failed ZIP integrity at {broken_part}."
                )
        os.replace(temp_path, destination)
        temp_path = None
    except MopExportError:
        raise
    except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise MopExportError(f"Unable to export MOP workbook: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    return destination


def _snapshot_project(project: Any, *, preview: bool = False) -> _RouteSnapshot:
    route_code = _clean_text(
        _read_value(project, "route_code", "project_id", default="")
    )
    title = _clean_text(_read_value(project, "title", default=route_code))
    revision = _clean_text(_read_value(project, "revision", default="1")) or "1"
    if not route_code:
        raise MopExportError("Route code is required for MOP export.")

    sites_raw = _read_value(project, "sites", default=())
    sites: dict[str, Any] = {}
    seen_site_keys: set[str] = set()
    seen_site_codes: set[str] = set()
    if isinstance(sites_raw, Mapping):
        site_items: Iterable[tuple[Any, Any]] = sites_raw.items()
    else:
        site_items = ((None, value) for value in _as_sequence(sites_raw, "sites"))
    for supplied_key, site in site_items:
        key = _clean_text(
            _read_value(site, "site_key", "key", "id", default=supplied_key or "")
        )
        if key:
            canonical_key = key.casefold()
            if canonical_key in seen_site_keys:
                raise MopExportError(f"Duplicate site key in route: {key}")
            seen_site_keys.add(canonical_key)
            site_code = _clean_text(
                _read_value(site, "code", "site_code", default="")
            )
            if site_code:
                canonical_code = site_code.casefold()
                if canonical_code in seen_site_codes:
                    raise MopExportError(
                        f"Duplicate site code in route: {site_code}"
                    )
                seen_site_codes.add(canonical_code)
            sites[key] = site

    shelves_raw = _read_value(project, "shelves", default=())
    if isinstance(shelves_raw, Mapping):
        raw_shelves = list(shelves_raw.values())
    else:
        raw_shelves = list(_as_sequence(shelves_raw, "shelves"))
    if not raw_shelves:
        raise MopExportError("At least one shelf is required for MOP export.")

    shelves: list[_ShelfSnapshot] = []
    seen_ids: set[str] = set()
    seen_tids: set[str] = set()
    seen_oam_ips: set[str] = set()
    for index, raw in enumerate(raw_shelves, start=1):
        shelf_id = _clean_text(
            _read_value(raw, "shelf_id", "id", default=f"S{index:03d}")
        ) or f"S{index:03d}"
        canonical_shelf_id = shelf_id.casefold()
        if canonical_shelf_id in seen_ids:
            raise MopExportError(f"Duplicate shelf ID in route: {shelf_id}")
        seen_ids.add(canonical_shelf_id)

        profile_id = _normalise_profile_id(
            _read_value(raw, "profile_id", "shelf_type", "type", default="")
        )
        if not profile_id:
            raise MopExportError(f"Shelf {shelf_id} has no shelf/profile type.")
        if profile_id not in _KNOWN_PROFILES:
            raise MopExportError(
                f"Shelf {shelf_id} has unknown shelf profile {profile_id!r}."
            )

        site_key = _clean_text(
            _read_value(raw, "site_key", "site_id", default="")
        )
        direct_site_code = _clean_text(
            _read_value(raw, "site_code", default="")
        )
        if not site_key:
            site_key = direct_site_code
        if not site_key:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no site.")
            site_key = f"review-site-{index}"

        site = sites.get(site_key)
        site_code = direct_site_code or _clean_text(
            _read_value(site, "code", "site_code", default="")
        )
        site_name = _clean_text(
            _read_value(raw, "site_name", default="")
        ) or _clean_text(
            _read_value(site, "name", "site_name", default="")
        )
        if not site_code:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no site code.")
            site_code = f"REVIEW SITE {index}"
        if not site_name:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no site name.")
            site_name = "REVIEW REQUIRED"
        tid = _clean_text(_read_value(raw, "tid", "target_id", default=""))
        if not tid:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no TID.")
            tid = f"REVIEW TID {index}"
        canonical_tid = tid.casefold()
        if canonical_tid in seen_tids:
            raise MopExportError(f"Duplicate TID in route: {tid}")
        seen_tids.add(canonical_tid)

        software_release = _clean_text(
            _read_value(raw, "software_release", "release", default="")
        )
        if not software_release:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no software release.")
            software_release = "REVIEW REQUIRED"
        shelf_variant = _clean_text(
            _read_value(raw, "shelf_variant", "variant", default="")
        )
        if not shelf_variant:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no shelf variant.")
            shelf_variant = "REVIEW REQUIRED"
        primary_oam_ip = _clean_text(
            _read_value(
                raw,
                "primary_oam_ip",
                "oam_ip",
                "ip",
                default="",
            )
        )
        if not primary_oam_ip:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no primary OAM IP.")
            primary_oam_ip = f"REVIEW OAM {index}"
        else:
            try:
                canonical_oam_ip = str(ipaddress.ip_address(primary_oam_ip))
            except ValueError as exc:
                if not preview:
                    raise MopExportError(
                        f"Shelf {shelf_id} has invalid primary OAM IP "
                        f"{primary_oam_ip!r}."
                    ) from exc
                primary_oam_ip = f"REVIEW OAM {index}: {primary_oam_ip}"
            else:
                if canonical_oam_ip in seen_oam_ips:
                    if not preview:
                        raise MopExportError(
                            f"Duplicate primary OAM IP in route: {primary_oam_ip}"
                        )
                    primary_oam_ip = (
                        f"REVIEW DUPLICATE OAM {index}: {primary_oam_ip}"
                    )
                else:
                    seen_oam_ips.add(canonical_oam_ip)
        power_label = _clean_text(
            _read_value(raw, "power_label", "power", default="")
        )
        if not power_label:
            if not preview:
                raise MopExportError(f"Shelf {shelf_id} has no power value.")
            power_label = "REVIEW REQUIRED"

        shelves.append(
            _ShelfSnapshot(
                shelf_id=shelf_id,
                profile_id=profile_id,
                software_release=software_release,
                shelf_variant=shelf_variant,
                site_key=site_key,
                site_code=site_code,
                site_name=site_name,
                tid=tid,
                primary_oam_ip=primary_oam_ip,
                raman_label=_clean_text(
                    _read_value(raw, "raman_label", "raman", default="")
                ),
                power_label=power_label,
            )
        )

    ordered_sites: list[str] = []
    for shelf in shelves:
        if shelf.site_key not in ordered_sites:
            ordered_sites.append(shelf.site_key)
    endpoint_a = ordered_sites[0]
    endpoint_z = ordered_sites[-1]
    for shelf in shelves:
        if (
            shelf.profile_id in _ADD_DROP_A_PROFILES | frozenset({"roadm_a"})
            and shelf.site_key != endpoint_a
        ):
            raise MopExportError(
                f"Shelf {shelf.shelf_id} profile {shelf.profile_id!r} must be "
                f"at the first ordered route site {endpoint_a!r}."
            )
        if (
            shelf.profile_id in _ADD_DROP_Z_PROFILES | frozenset({"roadm_z"})
            and shelf.site_key != endpoint_z
        ):
            raise MopExportError(
                f"Shelf {shelf.shelf_id} profile {shelf.profile_id!r} must be "
                f"at the last ordered route site {endpoint_z!r}."
            )

    snapshot_title = title or route_code
    return _RouteSnapshot(
        route_code=route_code,
        title=snapshot_title,
        revision=revision,
        shelves=tuple(shelves),
        irm_endpoint_display_codes=_trusted_irm_endpoint_display_codes(
            project,
            title=snapshot_title,
            shelves=shelves,
        ),
    )


def _trusted_irm_endpoint_display_codes(
    project: Any,
    *,
    title: str,
    shelves: Sequence[_ShelfSnapshot],
) -> tuple[str, str] | None:
    """Return source-bound terminal display codes or fail closed.

    Reviewed shelf site codes remain the default IRM endpoints.  Diagram
    display codes are accepted only when the complete controlled-derivation or
    operator-order marker still agrees with the current route title, source,
    and ordered endpoint TIDs.
    """

    diagram_source = _read_value(project, "diagram_source", default=None)
    if not isinstance(diagram_source, Mapping):
        return None
    marker = diagram_source.get("route_title_derivation")
    if not isinstance(marker, Mapping):
        return None
    if marker.get("rule_id") != _TERMINAL_ROUTE_TITLE_RULE_ID:
        return None
    status = marker.get("status")
    if status not in {"controlled_derivation", "operator_order_derivation"}:
        return None
    if marker.get("value") != title:
        return None
    if marker.get("deployable_cli") is not False:
        return None

    source_sha256 = diagram_source.get("source_sha256")
    if (
        not isinstance(source_sha256, str)
        or not source_sha256
        or marker.get("source_sha256") != source_sha256
    ):
        return None

    endpoint_tids = marker.get("endpoint_tids")
    if (
        not isinstance(endpoint_tids, (list, tuple))
        or len(endpoint_tids) != 2
        or any(not isinstance(tid, str) for tid in endpoint_tids)
        or tuple(endpoint_tids) != (shelves[0].tid, shelves[-1].tid)
    ):
        return None

    endpoint_codes = marker.get("endpoint_codes")
    if (
        not isinstance(endpoint_codes, (list, tuple))
        or len(endpoint_codes) != 2
        or any(
            not isinstance(code, str)
            or _TERMINAL_ROUTE_DISPLAY_TOKEN_RE.fullmatch(code) is None
            for code in endpoint_codes
        )
        or any(
            tid.split("-", maxsplit=1)[0] != code
            for tid, code in zip(endpoint_tids, endpoint_codes)
        )
    ):
        return None
    endpoint_pair = "-".join(endpoint_codes)
    if status == "controlled_derivation":
        if marker.get("observed_header_pair") != endpoint_pair:
            return None
    elif marker.get("current_endpoint_pair") != endpoint_pair:
        return None

    display_codes = marker.get("display_codes")
    if (
        not isinstance(display_codes, (list, tuple))
        or len(display_codes) != 2
        or any(
            not isinstance(code, str)
            or _TERMINAL_ROUTE_DISPLAY_TOKEN_RE.fullmatch(code) is None
            for code in display_codes
        )
    ):
        return None
    trusted_codes = (display_codes[0], display_codes[1])
    if "-".join(trusted_codes) != title:
        return None

    removed_prefix = marker.get("removed_shared_prefix")
    if removed_prefix == "US":
        expected_display_codes = tuple(
            code[2:] if code.startswith("US") else ""
            for code in endpoint_codes
        )
    elif removed_prefix is None:
        expected_display_codes = tuple(endpoint_codes)
    else:
        return None
    if not all(expected_display_codes) or trusted_codes != expected_display_codes:
        return None
    return trusted_codes


def _read_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_sequence(value: Any, label: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)):
        raise MopExportError(f"Route {label} must be a collection, not text.")
    try:
        return tuple(value)
    except TypeError as exc:
        raise MopExportError(f"Route {label} must be a collection.") from exc


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    text = _INVALID_XML_RE.sub(" ", text)
    if len(text) > 32767:
        raise MopExportError("A workbook field exceeds Excel's 32,767-character limit.")
    return text


def _normalise_profile_id(value: Any) -> str:
    text = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", text).strip("_")


def _profile_counts(snapshot: _RouteSnapshot) -> _ProfileCounts:
    profiles = [shelf.profile_id for shelf in snapshot.shelves]
    distinct_sites = {shelf.site_key for shelf in snapshot.shelves}
    ila = sum(profile in _ILA_PROFILES for profile in profiles)
    roadm = sum(profile in _ROADM_PROFILES for profile in profiles)
    add_a = sum(profile in _ADD_DROP_A_PROFILES for profile in profiles)
    add_z = sum(profile in _ADD_DROP_Z_PROFILES for profile in profiles)
    add_unassigned = sum(
        profile in _ADD_DROP_UNASSIGNED_PROFILES for profile in profiles
    )
    mapped = ila + roadm + add_a + add_z
    return _ProfileCounts(
        total_sites=len(distinct_sites),
        ila=ila,
        roadm=roadm,
        add_drop_a=add_a,
        add_drop_z=add_z,
        add_drop_unassigned=add_unassigned,
        unmapped=len(profiles) - mapped,
    )


def _render_fbn(
    source_xml: bytes,
    snapshot: _RouteSnapshot,
    counts: _ProfileCounts,
    *,
    preview: bool = False,
) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(source_xml, parser)
    sheet_data = root.find(Q("sheetData"))
    if sheet_data is None:
        raise MopExportError("FBN worksheet has no sheetData element.")

    source_rows = {
        int(row.get("r")): row for row in sheet_data.findall(Q("row"))
    }
    source_cells: dict[tuple[int, int], etree._Element] = {}
    for cell in sheet_data.iterfind(f".//{Q('c')}"):
        col, row = _split_cell_ref(cell.get("r", ""))
        source_cells[(row, col)] = cell

    rack_count = max(1, math.ceil(len(snapshot.shelves) / 8))
    table_start = 4 + 8 * rack_count
    end_col = table_start + 8
    end_row = max(52, 15 + len(snapshot.shelves))

    cells: dict[tuple[int, int], etree._Element] = {}

    # Clone a clean, styled rack shell from D:I for every eight shelves.
    for rack_index in range(rack_count):
        rack_start = 4 + rack_index * 8
        for row in range(1, 53):
            for source_col in range(4, 10):
                source_cell = source_cells.get((row, source_col))
                if source_cell is None:
                    continue
                cell = _clone_blank_cell(
                    source_cell, rack_start + source_col - 4, row
                )
                cells[(row, rack_start + source_col - 4)] = cell

        center_col = rack_start + 2
        _put_inline(cells, 2, center_col, f"RACK {rack_index + 1} (FRONT)", 270)
        for row in range(5, 50):
            rack_unit = 50 - row
            _put_number(cells, row, rack_start, rack_unit, _source_style(source_cells, row, 4, 288))
            _put_number(cells, row, rack_start + 4, rack_unit, _source_style(source_cells, row, 8, 288))
            _put_inline(cells, row, center_col, "Empty", 269)
        _put_inline(cells, 5, center_col, "DC PDU", 278)
        _put_inline(cells, 50, rack_start, f"RACK {rack_index + 1}", 508)

        cursor = 10
        rack_shelves = snapshot.shelves[rack_index * 8 : (rack_index + 1) * 8]
        for shelf in rack_shelves:
            if shelf.profile_id in _ILA_PROFILES:
                rows_used = 3
                _put_inline(cells, cursor, center_col, shelf.site_name, 286)
                _put_inline(cells, cursor + 1, center_col, shelf.tid, 289)
                _put_inline(
                    cells,
                    cursor + 2,
                    center_col,
                    shelf.raman_label or "Empty",
                    281 if shelf.raman_label else 269,
                )
            else:
                rows_used = 5
                _put_inline(cells, cursor, center_col, shelf.site_name, 295)
                _put_inline(cells, cursor + 1, center_col, shelf.tid, 296)
                release = " / ".join(
                    value
                    for value in (shelf.software_release, shelf.shelf_variant)
                    if value
                )
                _put_inline(cells, cursor + 2, center_col, release or "PLANNED", 297)
                _put_inline(
                    cells,
                    cursor + 3,
                    center_col,
                    _rack_profile_label(shelf.profile_id),
                    297,
                )
                _put_inline(
                    cells,
                    cursor + 4,
                    center_col,
                    shelf.raman_label or "Empty",
                    281 if shelf.raman_label else 269,
                )
            cursor += rows_used

    # Move the original styled summary/register block so it follows all racks.
    summary_shift = table_start - 20
    for row in range(1, min(end_row, 55) + 1):
        for source_col in range(19, 29):
            source_cell = source_cells.get((row, source_col))
            if source_cell is None:
                continue
            destination_col = source_col + summary_shift
            cells[(row, destination_col)] = _clone_blank_cell(
                source_cell, destination_col, row
            )

    # Summary and route-family counts.
    title = snapshot.title
    if "FBN" not in title.upper():
        title = f"{title}\nFBN"
    _put_inline(cells, 2, table_start, title, 543)
    _put_inline(cells, 5, table_start, snapshot.route_code, 545)
    _put_inline(cells, 5, table_start + 2, "Long Haul Next Gen\n(CIENA)", 551)
    _put_inline(
        cells,
        5,
        table_start + 4,
        f"{counts.total_sites} sites\n{max(counts.total_sites - 1, 0)} Spans",
        557,
    )
    add_release, add_variant = _family_release_variant(
        snapshot.shelves,
        (
            _ADD_DROP_A_PROFILES
            | _ADD_DROP_Z_PROFILES
            | _ADD_DROP_UNASSIGNED_PROFILES
        ),
        default_release="UNSPECIFIED",
        default_variant="UNSPECIFIED",
    )
    roadm_release, roadm_variant = _family_release_variant(
        snapshot.shelves,
        _ROADM_PROFILES,
        default_release="UNSPECIFIED",
        default_variant="UNSPECIFIED",
    )
    ila_release, ila_variant = _family_release_variant(
        snapshot.shelves,
        _ILA_PROFILES,
        default_release="UNSPECIFIED",
        default_variant="UNSPECIFIED",
    )
    _put_inline(cells, 8, table_start, f"{add_release} ADD/DROP", 536)
    _put_inline(cells, 8, table_start + 2, f"{roadm_release} ROADM", 538)
    _put_inline(cells, 8, table_start + 4, f"{ila_release} ILAs", 540)
    _put_inline(
        cells,
        9,
        table_start,
        f"({counts.add_drop}) {add_variant} Shelves",
        524,
    )
    _put_inline(
        cells,
        9,
        table_start + 2,
        f"({counts.roadm}) {roadm_variant} Shelves",
        526,
    )
    _put_inline(
        cells,
        9,
        table_start + 4,
        f"({counts.ila}) {ila_variant} Shelves",
        528,
    )
    if preview:
        route_note = (
            f"PREVIEW — NOT FOR CONSTRUCTION • REV {snapshot.revision}"
        )
    elif counts.unmapped:
        route_note = (
            f"REV {snapshot.revision} • IRM EXCLUDES {counts.unmapped} "
            "UNMAPPED SHELF/SHELVES"
        )
    else:
        route_note = f"CIENA ROUTE DELIVERABLE • REV {snapshot.revision}"
    _put_inline(cells, 11, table_start, route_note, 522)

    # Visible FBN register plus a hidden, auditable profile_id support column.
    header_styles = (282, 283, 283, 284, 285)
    for offset, (label, style) in enumerate(
        zip(("SITE", "TID", "IP", "RAMAN", "POWER"), header_styles)
    ):
        _put_inline(cells, 15, table_start + offset, label, style)
    _put_inline(cells, 15, table_start + 6, "PROFILE_ID", 285)

    for index, shelf in enumerate(snapshot.shelves, start=16):
        endpoint = index == 16 or index == 15 + len(snapshot.shelves)
        styles = (
            (290, 291, 291, 422, 421)
            if endpoint
            else (336, 357, 357, 422, 358)
        )
        if shelf.raman_label:
            styles = (*styles[:3], 292, styles[4])
        values = (
            shelf.site_name,
            shelf.tid,
            shelf.primary_oam_ip,
            shelf.raman_label,
            shelf.power_label,
        )
        for offset, (value, style) in enumerate(zip(values, styles)):
            _put_inline(cells, index, table_start + offset, value, style)
        _put_inline(cells, index, table_start + 6, shelf.profile_id, 293)

    # Rebuild column widths for the dynamic horizontal layout.
    source_cols = root.find(Q("cols"))
    if source_cols is not None:
        column_templates = list(source_cols.findall(Q("col")))
        for child in list(source_cols):
            source_cols.remove(child)
        output_columns: dict[int, etree._Element] = {}
        for col in range(1, 4):
            template = _column_template(column_templates, col)
            if template is not None:
                output_columns[col] = _clone_column(template, col)
        for rack_index in range(rack_count):
            rack_start = 4 + rack_index * 8
            for offset, source_col in enumerate(range(4, 12)):
                template = _column_template(column_templates, source_col)
                if template is not None:
                    output_columns[rack_start + offset] = _clone_column(
                        template, rack_start + offset
                    )
        for offset, source_col in enumerate(range(19, 29)):
            template = _column_template(column_templates, source_col)
            if template is not None:
                output_columns[table_start - 1 + offset] = _clone_column(
                    template, table_start - 1 + offset
                )
        # profile_id lives six columns after SITE and is intentionally hidden.
        support_col = table_start + 6
        if support_col in output_columns:
            output_columns[support_col].set("hidden", "1")
        for col in sorted(output_columns):
            source_cols.append(output_columns[col])

    # Replace the stale used range with only the deliberate dynamic cells.
    for child in list(sheet_data):
        sheet_data.remove(child)
    for row_number in range(1, end_row + 1):
        row_cells = [
            (col, cell)
            for (row, col), cell in cells.items()
            if row == row_number
        ]
        if not row_cells:
            continue
        source_row = source_rows.get(row_number)
        if source_row is None:
            source_row = source_rows.get(17)
        attrs = dict(source_row.attrib) if source_row is not None else {}
        attrs["r"] = str(row_number)
        attrs["spans"] = f"4:{end_col}"
        row_element = etree.Element(Q("row"), attrs)
        for _, cell in sorted(row_cells):
            row_element.append(cell)
        sheet_data.append(row_element)

    dimension = root.find(Q("dimension"))
    if dimension is not None:
        dimension.set("ref", f"D1:{_column_name(end_col)}{end_row}")

    merge_cells = root.find(Q("mergeCells"))
    if merge_cells is None:
        merge_cells = etree.Element(Q("mergeCells"))
        sheet_data.addnext(merge_cells)
    original_merges = [
        element.get("ref", "") for element in merge_cells.findall(Q("mergeCell"))
    ]
    for child in list(merge_cells):
        merge_cells.remove(child)
    merge_refs: list[str] = []
    for rack_index in range(rack_count):
        start = 4 + rack_index * 8
        merge_refs.append(
            f"{_column_name(start)}50:{_column_name(start + 4)}52"
        )
    for reference in original_merges:
        bounds = _range_bounds(reference)
        if bounds is None:
            continue
        min_col, min_row, max_col, max_row = bounds
        if 19 <= min_col <= max_col <= 28 and max_row <= end_row:
            merge_refs.append(
                _shift_range(reference, col_offset=summary_shift, row_offset=0)
            )
    for reference in merge_refs:
        etree.SubElement(merge_cells, Q("mergeCell"), ref=reference)
    merge_cells.set("count", str(len(merge_refs)))

    existing_col_breaks = root.find(Q("colBreaks"))
    if existing_col_breaks is not None:
        root.remove(existing_col_breaks)
    break_ids = [
        4 + 8 * rack_index - 1 for rack_index in range(2, rack_count, 2)
    ]
    if break_ids:
        col_breaks = etree.Element(
            Q("colBreaks"),
            count=str(len(break_ids)),
            manualBreakCount=str(len(break_ids)),
        )
        for break_id in break_ids:
            etree.SubElement(
                col_breaks,
                Q("brk"),
                id=str(break_id),
                min="0",
                max="16383",
                man="1",
            )
        # colBreaks belongs after pageSetup in worksheet schema order. The
        # controlled template has pageSetup as its final child, so appending
        # keeps the XML valid for native desktop Excel.
        root.append(col_breaks)

    sheet_pr = root.find(Q("sheetPr"))
    if sheet_pr is not None:
        page_setup_pr = sheet_pr.find(Q("pageSetUpPr"))
        if page_setup_pr is None:
            page_setup_pr = etree.SubElement(sheet_pr, Q("pageSetUpPr"))
        page_setup_pr.set("fitToPage", "1")
        page_setup_pr.set("autoPageBreaks", "0")

    sheet_view = root.find(f"{Q('sheetViews')}/{Q('sheetView')}")
    if sheet_view is not None:
        sheet_view.set("tabSelected", "1")
        sheet_view.set("topLeftCell", "D1")
        sheet_view.set("zoomScale", "70")
        selection = sheet_view.find(Q("selection"))
        if selection is not None:
            selection.set("activeCell", "D1")
            selection.set("sqref", "D1")

    page_margins = root.find(Q("pageMargins"))
    if page_margins is not None:
        page_margins.set("left", "0.25")
        page_margins.set("right", "0.25")
        page_margins.set("top", "0.35")
        page_margins.set("bottom", "0.35")

    page_setup = root.find(Q("pageSetup"))
    if page_setup is not None:
        page_setup.set("orientation", "landscape")
        page_setup.set("fitToWidth", "0")
        page_setup.set("fitToHeight", "1")
        page_setup.set("pageOrder", "overThenDown")
        page_setup.attrib.pop("scale", None)

    return _xml_bytes(root)


def _render_irm(
    source_xml: bytes, snapshot: _RouteSnapshot, counts: _ProfileCounts
) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(source_xml, parser)
    shelves = snapshot.shelves
    ordered_sites: list[str] = []
    site_codes: dict[str, str] = {}
    for shelf in shelves:
        site_codes.setdefault(shelf.site_key, shelf.site_code)
        if shelf.site_key not in ordered_sites:
            ordered_sites.append(shelf.site_key)
    first_site = site_codes[ordered_sites[0]]
    last_site = site_codes[ordered_sites[-1]]

    first_display = first_site
    last_display = last_site
    if snapshot.irm_endpoint_display_codes is not None:
        first_display, last_display = snapshot.irm_endpoint_display_codes

    string_values = {
        "B2": snapshot.title,
        "B3": first_display,
        "B5": last_display,
    }
    numeric_values = {
        "B6": counts.total_sites,
        "B7": counts.ila,
        "B8": 0,  # DGE is not a route shelf profile in the current model.
        "B9": counts.roadm,
        "B10": counts.add_drop,
        "B11": 0,
        "B12": 0,
        "B13": 0,
        "B14": 0,
        "F18": counts.ila,
        "G18": counts.roadm,
        "H18": counts.add_drop_a,
        "I18": counts.add_drop_z,
    }
    for reference, value in string_values.items():
        cell = _required_cell(root, reference, "IRM")
        _set_cell_inline(cell, value)
    for reference, value in numeric_values.items():
        cell = _required_cell(root, reference, "IRM")
        _set_cell_number(cell, value)

    # Cached results refer to the source route.  Removing only cached <v>
    # values avoids displaying incorrect material quantities before Excel's
    # forced full recalculation, while all formula nodes remain byte-logically
    # identical (same text and attributes).
    for formula in root.findall(f".//{Q('f')}"):
        cell = formula.getparent()
        cached = cell.find(Q("v"))
        if cached is not None:
            cell.remove(cached)

    sheet_view = root.find(f"{Q('sheetViews')}/{Q('sheetView')}")
    if sheet_view is not None:
        sheet_view.set("showFormulas", "0")
        sheet_view.set("tabSelected", "0")
        sheet_view.set("topLeftCell", "A1")

    return _xml_bytes(root)


_EMU_PER_INCH = 914_400
_EMU_PER_PIXEL = 9_525
_DEFAULT_ROW_EMU = 190_500
_DIAGRAM_MAX_WIDTH_EMU = 16 * _EMU_PER_INCH


def _render_diagram_drawing(
    source_xml: bytes,
    diagram: WorkbookDiagram,
) -> tuple[bytes, bytes, Mapping[str, bytes]]:
    """Append internally related PNG pictures to the template Diagram drawing."""

    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(source_xml, parser)
    if root.tag != XDR_Q("wsDr"):
        raise MopExportError("MOP template Diagram drawing has an invalid root.")

    existing_ids: list[int] = []
    for value in root.xpath(
        ".//xdr:cNvPr/@id",
        namespaces={"xdr": DRAWING_NS},
    ):
        try:
            existing_ids.append(int(value))
        except (TypeError, ValueError) as exc:
            raise MopExportError(
                "MOP template Diagram drawing has an invalid object ID."
            ) from exc
    next_object_id = max(existing_ids, default=0) + 1

    relationships = etree.Element(
        PACKAGE_REL_Q("Relationships"),
        nsmap={None: PACKAGE_REL_NS},
    )
    media_by_hash: dict[str, tuple[str, str]] = {}
    media: dict[str, bytes] = {}
    row = 0
    for order, image in enumerate(diagram.images, start=1):
        media_record = media_by_hash.get(image.normalized_sha256)
        if media_record is None:
            media_number = len(media_by_hash) + 1
            relationship_id = f"rId{media_number}"
            part_name = (
                f"xl/media/atlas_route_diagram_{media_number:03d}.png"
            )
            relationship = etree.SubElement(
                relationships,
                PACKAGE_REL_Q("Relationship"),
            )
            relationship.set("Id", relationship_id)
            relationship.set(
                "Type",
                (
                    "http://schemas.openxmlformats.org/officeDocument/"
                    "2006/relationships/image"
                ),
            )
            relationship.set(
                "Target",
                f"../media/{Path(part_name).name}",
            )
            media[part_name] = image.png_bytes
            media_record = (relationship_id, part_name)
            media_by_hash[image.normalized_sha256] = media_record
        relationship_id, _part_name = media_record

        native_width = image.width * _EMU_PER_PIXEL
        native_height = image.height * _EMU_PER_PIXEL
        scale = min(1.0, _DIAGRAM_MAX_WIDTH_EMU / native_width)
        extent_width = max(1, round(native_width * scale))
        extent_height = max(1, round(native_height * scale))

        anchor = etree.SubElement(root, XDR_Q("oneCellAnchor"))
        origin = etree.SubElement(anchor, XDR_Q("from"))
        for name, value in (
            ("col", 0),
            ("colOff", 0),
            ("row", row),
            ("rowOff", 0),
        ):
            element = etree.SubElement(origin, XDR_Q(name))
            element.text = str(value)
        extent = etree.SubElement(anchor, XDR_Q("ext"))
        extent.set("cx", str(extent_width))
        extent.set("cy", str(extent_height))

        picture = etree.SubElement(anchor, XDR_Q("pic"))
        nonvisual = etree.SubElement(picture, XDR_Q("nvPicPr"))
        properties = etree.SubElement(nonvisual, XDR_Q("cNvPr"))
        properties.set("id", str(next_object_id))
        next_object_id += 1
        properties.set("name", f"ATLAS Route Diagram {order}")
        properties.set(
            "descr",
            (
                f"Embedded normalized route diagram {order}; "
                f"source file {diagram.source_file_name}; "
                f"source part {image.source_part}; "
                f"source SHA-256 {diagram.source_sha256}; "
                f"normalized SHA-256 {image.normalized_sha256}"
            ),
        )
        picture_properties = etree.SubElement(
            nonvisual,
            XDR_Q("cNvPicPr"),
        )
        locks = etree.SubElement(
            picture_properties,
            A_Q("picLocks"),
        )
        locks.set("noChangeAspect", "1")

        fill = etree.SubElement(picture, XDR_Q("blipFill"))
        blip = etree.SubElement(fill, A_Q("blip"))
        blip.set(f"{{{REL_NS}}}embed", relationship_id)
        stretch = etree.SubElement(fill, A_Q("stretch"))
        etree.SubElement(stretch, A_Q("fillRect"))

        shape = etree.SubElement(picture, XDR_Q("spPr"))
        transform = etree.SubElement(shape, A_Q("xfrm"))
        offset = etree.SubElement(transform, A_Q("off"))
        offset.set("x", "0")
        offset.set("y", "0")
        shape_extent = etree.SubElement(transform, A_Q("ext"))
        shape_extent.set("cx", str(extent_width))
        shape_extent.set("cy", str(extent_height))
        geometry = etree.SubElement(shape, A_Q("prstGeom"))
        geometry.set("prst", "rect")
        etree.SubElement(geometry, A_Q("avLst"))
        etree.SubElement(anchor, XDR_Q("clientData"))

        row += math.ceil(extent_height / _DEFAULT_ROW_EMU) + 2

    return _xml_bytes(root), _xml_bytes(relationships), media


def _write_new_zip_part(
    archive: zipfile.ZipFile,
    part_name: str,
    data: bytes,
) -> None:
    """Write one deterministic renderer-owned OPC part."""

    info = zipfile.ZipInfo(part_name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0
    archive.writestr(info, data)


def _render_workbook(source_xml: bytes, print_area: str) -> bytes:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(source_xml, parser)

    workbook_view = root.find(f"{Q('bookViews')}/{Q('workbookView')}")
    if workbook_view is not None:
        workbook_view.set("activeTab", "0")

    defined_names = root.find(Q("definedNames"))
    if defined_names is None:
        defined_names = etree.Element(Q("definedNames"))
        calc_pr = root.find(Q("calcPr"))
        if calc_pr is not None:
            calc_pr.addprevious(defined_names)
        else:
            root.append(defined_names)
    for element in list(defined_names.findall(Q("definedName"))):
        if (
            element.get("name") == "_xlnm.Print_Area"
            and element.get("localSheetId") == "0"
        ):
            defined_names.remove(element)
    name = etree.SubElement(
        defined_names,
        Q("definedName"),
        name="_xlnm.Print_Area",
        localSheetId="0",
    )
    name.text = print_area

    calc_pr = root.find(Q("calcPr"))
    if calc_pr is None:
        calc_pr = etree.SubElement(root, Q("calcPr"))
    calc_pr.set("calcMode", "auto")
    calc_pr.set("fullCalcOnLoad", "1")
    calc_pr.set("forceFullCalc", "1")

    return _xml_bytes(root)


def _render_calc_chain(source_xml: bytes) -> bytes:
    """Remove only stale FBN formula records while retaining all IRM records.

    The template uses worksheet ``sheetId`` 35 for FBN and 30 for IRM.  The
    rebuilt FBN rack diagram intentionally contains no formulas, so leaving
    its 32 old chain records makes desktop Excel invoke file recovery.  The
    186 IRM records still correspond one-for-one with the preserved formulas.
    """

    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    root = etree.fromstring(source_xml, parser)
    current_sheet_id: str | None = None
    for cell in list(root.findall(Q("c"))):
        explicit_sheet_id = cell.get("i")
        if explicit_sheet_id is not None:
            current_sheet_id = explicit_sheet_id
        if current_sheet_id == "35":
            root.remove(cell)
    remaining = root.findall(Q("c"))
    if len(remaining) != 186:
        raise MopExportError(
            "The validated template calculation chain did not retain exactly "
            "the 186 IRM formula records."
        )
    return _xml_bytes(root)


def _fbn_print_area(snapshot: _RouteSnapshot) -> str:
    rack_count = max(1, math.ceil(len(snapshot.shelves) / 8))
    table_start = 4 + 8 * rack_count
    end_col = table_start + 8
    end_row = max(52, 15 + len(snapshot.shelves))
    return f"'FBN'!$D$1:${_column_name(end_col)}${end_row}"


def _rack_profile_label(profile_id: str) -> str:
    if profile_id in _ROADM_PROFILES:
        return "ROADM"
    if profile_id in (
        _ADD_DROP_A_PROFILES
        | _ADD_DROP_Z_PROFILES
        | _ADD_DROP_UNASSIGNED_PROFILES
    ):
        return "ADD/DROP"
    return profile_id.upper().replace("_", " ")


def _family_release_variant(
    shelves: Sequence[_ShelfSnapshot],
    profiles: frozenset[str],
    *,
    default_release: str,
    default_variant: str,
) -> tuple[str, str]:
    """Return honest, concise summary labels for one shelf family.

    Explicit ``UNSPECIFIED`` labels are used when the route has no shelf in
    the family. A heterogeneous route is labeled ``MIXED`` rather than being
    silently described as one release or part number.
    """

    family_shelves = [shelf for shelf in shelves if shelf.profile_id in profiles]
    if not family_shelves:
        return default_release, default_variant
    releases = {shelf.software_release for shelf in family_shelves}
    variants = {shelf.shelf_variant for shelf in family_shelves}
    release = next(iter(releases)) if len(releases) == 1 else "MIXED"
    variant = next(iter(variants)) if len(variants) == 1 else "MIXED"
    return release, variant


def _put_inline(
    cells: dict[tuple[int, int], etree._Element],
    row: int,
    col: int,
    value: Any,
    style: int | str | None = None,
) -> None:
    cell = cells.get((row, col))
    if cell is None:
        cell = etree.Element(Q("c"), r=f"{_column_name(col)}{row}")
        cells[(row, col)] = cell
    if style is not None:
        cell.set("s", str(style))
    _set_cell_inline(cell, _clean_text(value))


def _put_number(
    cells: dict[tuple[int, int], etree._Element],
    row: int,
    col: int,
    value: int,
    style: int | str | None = None,
) -> None:
    cell = cells.get((row, col))
    if cell is None:
        cell = etree.Element(Q("c"), r=f"{_column_name(col)}{row}")
        cells[(row, col)] = cell
    if style is not None:
        cell.set("s", str(style))
    _set_cell_number(cell, value)


def _set_cell_inline(cell: etree._Element, value: str) -> None:
    _clear_cell_value(cell)
    cell.set("t", "inlineStr")
    inline = etree.SubElement(cell, Q("is"))
    text = etree.SubElement(inline, Q("t"))
    if value != value.strip() or "\n" in value:
        text.set(f"{{{XML_NS}}}space", "preserve")
    text.text = value


def _set_cell_number(cell: etree._Element, value: int) -> None:
    _clear_cell_value(cell)
    cell.attrib.pop("t", None)
    etree.SubElement(cell, Q("v")).text = str(int(value))


def _clear_cell_value(cell: etree._Element) -> None:
    for child in list(cell):
        if child.tag in {Q("f"), Q("v"), Q("is")}:
            cell.remove(child)


def _clone_blank_cell(
    source: etree._Element, col: int, row: int
) -> etree._Element:
    cell = copy.deepcopy(source)
    cell.set("r", f"{_column_name(col)}{row}")
    _clear_cell_value(cell)
    cell.attrib.pop("t", None)
    return cell


def _source_style(
    source_cells: Mapping[tuple[int, int], etree._Element],
    row: int,
    col: int,
    fallback: int,
) -> str:
    cell = source_cells.get((row, col))
    return cell.get("s", str(fallback)) if cell is not None else str(fallback)


def _required_cell(root: etree._Element, reference: str, sheet: str) -> etree._Element:
    cells = root.xpath(".//m:c[@r=$reference]", namespaces=NS, reference=reference)
    if not cells:
        raise MopExportError(f"{sheet} template is missing required cell {reference}.")
    return cells[0]


def _column_template(
    templates: Sequence[etree._Element], col: int
) -> etree._Element | None:
    for element in templates:
        if int(element.get("min", "0")) <= col <= int(element.get("max", "0")):
            return element
    return None


def _clone_column(source: etree._Element, col: int) -> etree._Element:
    element = copy.deepcopy(source)
    element.set("min", str(col))
    element.set("max", str(col))
    return element


def _split_cell_ref(reference: str) -> tuple[int, int]:
    match = _CELL_REF_RE.match(reference)
    if not match:
        raise MopExportError(f"Invalid cell reference in MOP template: {reference!r}")
    return _column_index(match.group(1)), int(match.group(2))


def _column_index(name: str) -> int:
    result = 0
    for character in name:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _column_name(index: int) -> str:
    if index < 1:
        raise MopExportError(f"Invalid Excel column index: {index}")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _range_bounds(reference: str) -> tuple[int, int, int, int] | None:
    try:
        start, end = reference.split(":", 1)
        start_col, start_row = _split_cell_ref(start)
        end_col, end_row = _split_cell_ref(end)
    except (ValueError, MopExportError):
        return None
    return start_col, start_row, end_col, end_row


def _shift_range(reference: str, col_offset: int, row_offset: int) -> str:
    bounds = _range_bounds(reference)
    if bounds is None:
        raise MopExportError(f"Invalid merged-cell range in template: {reference}")
    min_col, min_row, max_col, max_row = bounds
    return (
        f"{_column_name(min_col + col_offset)}{min_row + row_offset}:"
        f"{_column_name(max_col + col_offset)}{max_row + row_offset}"
    )


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "DIAGRAM_DRAWING_PART",
    "DIAGRAM_DRAWING_RELS_PART",
    "DEFAULT_TEMPLATE_PATH",
    "MopExportError",
    "MopExportWarning",
    "TEMPLATE_FILENAME",
    "TEMPLATE_SHA256",
    "export_mop",
]
