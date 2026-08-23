"""Bounded, path-free route-diagram assets for workbook rendering.

The route-project JSON intentionally stores only diagram provenance.  Pixel
content is retained in memory for the current ATLAS session and is supplied
explicitly to the workbook renderer.  These types validate that the supplied
content is a bounded PNG whose bytes, dimensions, and recorded digest agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
import re
import struct
from types import MappingProxyType
from typing import Mapping, Sequence

from PIL import Image, UnidentifiedImageError


WORKBOOK_DIAGRAM_MARKER_KEY = "workbook_diagram"
WORKBOOK_DIAGRAM_REPRESENTATION = "normalized_png"
WORKBOOK_DIAGRAM_NORMALIZATION = "canonical-rgb-png-v2-max2048"

MAX_WORKBOOK_DIAGRAM_IMAGES = 32
MAX_WORKBOOK_DIAGRAM_IMAGE_BYTES = 20 * 1024 * 1024
MAX_WORKBOOK_DIAGRAM_TOTAL_BYTES = 80 * 1024 * 1024
MAX_WORKBOOK_DIAGRAM_DIMENSION = 2_048
MAX_WORKBOOK_DIAGRAM_PIXELS = 2_048 * 2_048
MAX_WORKBOOK_DIAGRAM_TEXT = 4096

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class DiagramAssetError(ValueError):
    """Raised when an in-memory diagram asset is unsafe or stale."""


def _clean_text(value: object, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise DiagramAssetError(f"{field} must be a string.")
    clean = value.strip()
    if required and not clean:
        raise DiagramAssetError(f"{field} is required.")
    if len(clean) > MAX_WORKBOOK_DIAGRAM_TEXT:
        raise DiagramAssetError(
            f"{field} exceeds {MAX_WORKBOOK_DIAGRAM_TEXT} characters."
        )
    if _CONTROL_RE.search(clean):
        raise DiagramAssetError(f"{field} contains unsafe control characters.")
    return clean


def _clean_sha256(value: object, field: str) -> str:
    clean = _clean_text(value, field).lower()
    if not _SHA256_RE.fullmatch(clean):
        raise DiagramAssetError(f"{field} must be a 64-character SHA-256 digest.")
    return clean


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DiagramAssetError(f"{field} must be a positive integer.")
    return value


def _png_dimensions(data: bytes, field: str) -> tuple[int, int]:
    if not data.startswith(_PNG_SIGNATURE):
        raise DiagramAssetError(f"{field} is not a PNG image.")
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise DiagramAssetError(f"{field} has no valid PNG IHDR.")
    width, height = struct.unpack(">II", data[16:24])
    if width < 1 or height < 1:
        raise DiagramAssetError(f"{field} has invalid PNG dimensions.")
    if (
        width > MAX_WORKBOOK_DIAGRAM_DIMENSION
        or height > MAX_WORKBOOK_DIAGRAM_DIMENSION
        or width * height > MAX_WORKBOOK_DIAGRAM_PIXELS
    ):
        raise DiagramAssetError(f"{field} exceeds the workbook image limits.")
    try:
        with Image.open(BytesIO(data)) as image:
            if str(image.format or "").upper() != "PNG":
                raise DiagramAssetError(f"{field} is not a PNG image.")
            if image.size != (width, height):
                raise DiagramAssetError(
                    f"{field} PNG dimensions do not match its IHDR."
                )
            if image.mode != "RGB":
                raise DiagramAssetError(
                    f"{field} must use canonical opaque RGB pixels."
                )
            image.verify()
    except DiagramAssetError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise DiagramAssetError(f"{field} is not a valid PNG image: {exc}") from exc
    return width, height


def _workbook_png(
    data: bytes,
    field: str,
) -> tuple[bytes, int, int]:
    """Create one deterministic, opaque, workbook-sized PNG."""

    try:
        with Image.open(BytesIO(data)) as source:
            if str(source.format or "").upper() != "PNG":
                raise DiagramAssetError(f"{field} is not a PNG image.")
            source.load()
            rgb = source.convert("RGB")
            try:
                width, height = rgb.size
                if max(width, height) > MAX_WORKBOOK_DIAGRAM_DIMENSION:
                    scale = MAX_WORKBOOK_DIAGRAM_DIMENSION / max(
                        width,
                        height,
                    )
                    resized = rgb.resize(
                        (
                            max(1, round(width * scale)),
                            max(1, round(height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                else:
                    resized = rgb.copy()
                try:
                    output = BytesIO()
                    resized.save(
                        output,
                        format="PNG",
                        compress_level=6,
                        optimize=False,
                    )
                    return (
                        output.getvalue(),
                        resized.width,
                        resized.height,
                    )
                finally:
                    resized.close()
            finally:
                rgb.close()
    except DiagramAssetError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise DiagramAssetError(
            f"{field} could not be normalized for the workbook: {exc}"
        ) from exc


@dataclass(frozen=True)
class WorkbookDiagramImage:
    """One ordered normalized image occurrence to show on the Diagram tab."""

    source_label: str
    source_part: str
    normalized_sha256: str
    width: int
    height: int
    png_bytes: bytes

    def __post_init__(self) -> None:
        label = _clean_text(self.source_label, "diagram image source_label")
        part = _clean_text(self.source_part, "diagram image source_part")
        digest = _clean_sha256(
            self.normalized_sha256,
            "diagram image normalized_sha256",
        )
        width = _positive_int(self.width, "diagram image width")
        height = _positive_int(self.height, "diagram image height")
        if not isinstance(self.png_bytes, bytes):
            raise DiagramAssetError("diagram image png_bytes must be bytes.")
        data = self.png_bytes
        if not data:
            raise DiagramAssetError("diagram image png_bytes cannot be empty.")
        if len(data) > MAX_WORKBOOK_DIAGRAM_IMAGE_BYTES:
            raise DiagramAssetError(
                "diagram image exceeds the per-image byte limit."
            )
        actual_width, actual_height = _png_dimensions(data, "diagram image")
        if (width, height) != (actual_width, actual_height):
            raise DiagramAssetError(
                "diagram image dimensions do not match the PNG content."
            )
        actual_digest = sha256(data).hexdigest()
        if digest != actual_digest:
            raise DiagramAssetError(
                "diagram image normalized SHA-256 does not match its bytes."
            )
        object.__setattr__(self, "source_label", label)
        object.__setattr__(self, "source_part", part)
        object.__setattr__(self, "normalized_sha256", digest)

    def provenance_dict(self, *, order: int) -> dict[str, object]:
        return {
            "order": order,
            "source_label": self.source_label,
            "source_part": self.source_part,
            "normalized_sha256": self.normalized_sha256,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class WorkbookDiagram:
    """Path-free in-memory diagram content bound to one uploaded source."""

    source_file_name: str
    source_type: str
    source_sha256: str
    images: tuple[WorkbookDiagramImage, ...]

    def __post_init__(self) -> None:
        file_name = _clean_text(
            self.source_file_name,
            "diagram source_file_name",
        )
        if (
            PurePath(file_name).name != file_name
            or "/" in file_name
            or "\\" in file_name
        ):
            raise DiagramAssetError(
                "diagram source_file_name must not contain a path."
            )
        source_type = _clean_text(
            self.source_type,
            "diagram source_type",
        ).casefold()
        if source_type not in {"docx", "png", "jpeg"}:
            raise DiagramAssetError("diagram source_type is unsupported.")
        source_digest = _clean_sha256(
            self.source_sha256,
            "diagram source_sha256",
        )
        images = tuple(self.images)
        if not images:
            raise DiagramAssetError("diagram must contain at least one image.")
        if len(images) > MAX_WORKBOOK_DIAGRAM_IMAGES:
            raise DiagramAssetError("diagram contains too many image occurrences.")
        if not all(isinstance(image, WorkbookDiagramImage) for image in images):
            raise DiagramAssetError(
                "diagram images must be WorkbookDiagramImage values."
            )
        if sum(len(image.png_bytes) for image in images) > (
            MAX_WORKBOOK_DIAGRAM_TOTAL_BYTES
        ):
            raise DiagramAssetError("diagram exceeds the aggregate byte limit.")
        object.__setattr__(self, "source_file_name", file_name)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_sha256", source_digest)
        object.__setattr__(self, "images", images)

    @property
    def unique_media_count(self) -> int:
        return len({image.normalized_sha256 for image in self.images})

    def marker_dict(self, *, required_in_mop: bool = True) -> dict[str, object]:
        return {
            "required_in_mop": bool(required_in_mop),
            "representation": WORKBOOK_DIAGRAM_REPRESENTATION,
            "normalization": WORKBOOK_DIAGRAM_NORMALIZATION,
            "max_render_dimension": MAX_WORKBOOK_DIAGRAM_DIMENSION,
            "source_sha256": self.source_sha256,
            "image_occurrence_count": len(self.images),
            "images": [
                image.provenance_dict(order=order)
                for order, image in enumerate(self.images, start=1)
            ],
        }

    def manifest_dict(self) -> dict[str, object]:
        return {
            "embedded": True,
            "sheet": "Diagram",
            "representation": WORKBOOK_DIAGRAM_REPRESENTATION,
            "normalization": WORKBOOK_DIAGRAM_NORMALIZATION,
            "max_render_dimension": MAX_WORKBOOK_DIAGRAM_DIMENSION,
            "source_file_name": self.source_file_name,
            "source_type": self.source_type,
            "source_sha256": self.source_sha256,
            "image_occurrence_count": len(self.images),
            "unique_media_count": self.unique_media_count,
            "external_relationships": False,
            "images": [
                image.provenance_dict(order=order)
                for order, image in enumerate(self.images, start=1)
            ],
        }


def workbook_diagram_from_source(source: object) -> WorkbookDiagram:
    """Select deliverable source images without retaining a filesystem path.

    DOCX images are retained in their existing document relationship order.
    A standalone raster contributes only its full source/overview image; the
    overlapping detail tiles created solely for vision extraction are omitted.
    """

    source_type = str(getattr(source, "source_type", "") or "").casefold()
    raw_images = tuple(getattr(source, "images", ()) or ())
    if source_type == "docx":
        selected = tuple(
            image
            for image in raw_images
            if str(getattr(image, "view_kind", "") or "") == "source"
        )
    else:
        selected = tuple(
            image
            for image in raw_images
            if str(getattr(image, "view_kind", "") or "")
            in {"source", "overview"}
            and int(getattr(image, "source_image_index", -1)) == 0
        )[:1]
    if not selected:
        raise DiagramAssetError(
            "diagram source has no deliverable source image."
        )
    images: list[WorkbookDiagramImage] = []
    for image in selected:
        if str(getattr(image, "format", "") or "").casefold() != "png":
            raise DiagramAssetError(
                "diagram source images must be normalized PNG values."
            )
        png_bytes, width, height = _workbook_png(
            bytes(getattr(image, "data", b"") or b""),
            "diagram source image",
        )
        images.append(
            WorkbookDiagramImage(
                source_label=str(getattr(image, "source_label", "") or ""),
                source_part=str(getattr(image, "source_part", "") or ""),
                normalized_sha256=sha256(png_bytes).hexdigest(),
                width=width,
                height=height,
                png_bytes=png_bytes,
            )
        )
    return WorkbookDiagram(
        source_file_name=str(getattr(source, "file_name", "") or ""),
        source_type=source_type,
        source_sha256=str(getattr(source, "sha256", "") or ""),
        images=tuple(images),
    )


def _project_diagram_source(project: object) -> Mapping[str, object]:
    if isinstance(project, Mapping):
        value = project.get("diagram_source", {})
    else:
        value = getattr(project, "diagram_source", {})
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise DiagramAssetError("project diagram_source must be a mapping.")
    return value


def _expected_image_records(marker: Mapping[str, object]) -> Sequence[object]:
    value = marker.get("images")
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise DiagramAssetError("workbook diagram marker images must be an array.")
    return value


def validate_workbook_diagram_for_project(
    project: object,
    diagram: WorkbookDiagram | None,
) -> WorkbookDiagram | None:
    """Validate an optional asset against persisted hash-only provenance."""

    source = _project_diagram_source(project)
    marker_value = source.get(WORKBOOK_DIAGRAM_MARKER_KEY)
    if marker_value is None:
        marker: Mapping[str, object] | None = None
        required = False
    else:
        if not isinstance(marker_value, Mapping):
            raise DiagramAssetError(
                "project workbook_diagram marker must be a mapping."
            )
        marker = marker_value
        required_value = marker.get("required_in_mop", False)
        if not isinstance(required_value, bool):
            raise DiagramAssetError(
                "workbook diagram required_in_mop must be Boolean."
            )
        required = required_value
        representation = marker.get(
            "representation",
            WORKBOOK_DIAGRAM_REPRESENTATION,
        )
        if representation != WORKBOOK_DIAGRAM_REPRESENTATION:
            raise DiagramAssetError(
                "workbook diagram representation must be normalized_png."
            )
        normalization = marker.get(
            "normalization",
            WORKBOOK_DIAGRAM_NORMALIZATION,
        )
        if normalization != WORKBOOK_DIAGRAM_NORMALIZATION:
            raise DiagramAssetError(
                "workbook diagram normalization rule is unsupported."
            )
        max_render_dimension = marker.get(
            "max_render_dimension",
            MAX_WORKBOOK_DIAGRAM_DIMENSION,
        )
        if max_render_dimension != MAX_WORKBOOK_DIAGRAM_DIMENSION:
            raise DiagramAssetError(
                "workbook diagram render-dimension rule is unsupported."
            )

    if diagram is None:
        if required:
            raise DiagramAssetError(
                "The source diagram must be reattached before rendering the "
                "Diagram tab."
            )
        return None
    if not isinstance(diagram, WorkbookDiagram):
        raise DiagramAssetError("diagram must be a WorkbookDiagram value.")
    if marker is None:
        raise DiagramAssetError(
            "A supplied diagram requires project workbook-diagram provenance."
        )
    # Revalidate at the export boundary as well as at construction. Frozen
    # dataclasses prevent ordinary mutation, but this makes the renderer
    # independently fail closed if a custom adapter or unsafe low-level
    # mutation presents stale bytes.
    diagram = WorkbookDiagram(
        source_file_name=diagram.source_file_name,
        source_type=diagram.source_type,
        source_sha256=diagram.source_sha256,
        images=tuple(
            WorkbookDiagramImage(
                source_label=image.source_label,
                source_part=image.source_part,
                normalized_sha256=image.normalized_sha256,
                width=image.width,
                height=image.height,
                png_bytes=image.png_bytes,
            )
            for image in diagram.images
        ),
    )

    top_source_hash = str(source.get("source_sha256") or "").strip()
    marker_source_hash = str(
        marker.get("source_sha256") if marker is not None else ""
    ).strip()
    clean_top_hash = (
        _clean_sha256(top_source_hash, "project diagram source_sha256")
        if top_source_hash
        else ""
    )
    clean_marker_hash = (
        _clean_sha256(
            marker_source_hash,
            "workbook diagram source_sha256",
        )
        if marker_source_hash
        else ""
    )
    if (
        clean_top_hash
        and clean_marker_hash
        and clean_top_hash != clean_marker_hash
    ):
        raise DiagramAssetError(
            "Project diagram source hashes are inconsistent."
        )
    expected_source_hash = clean_marker_hash or clean_top_hash
    if required and not expected_source_hash:
        raise DiagramAssetError(
            "Required workbook diagram provenance has no source SHA-256."
        )
    if expected_source_hash:
        if expected_source_hash != diagram.source_sha256:
            raise DiagramAssetError(
                "Attached diagram source SHA-256 does not match the project."
            )
    expected_file_name = source.get("file_name")
    if expected_file_name is not None and str(expected_file_name).strip():
        if str(expected_file_name).strip() != diagram.source_file_name:
            raise DiagramAssetError(
                "Attached diagram file name does not match the project."
            )
    expected_source_type = source.get("source_type")
    if expected_source_type is not None and str(expected_source_type).strip():
        if (
            str(expected_source_type).strip().casefold()
            != diagram.source_type
        ):
            raise DiagramAssetError(
                "Attached diagram source type does not match the project."
            )

    if marker is not None:
        expected_count = marker.get("image_occurrence_count")
        if expected_count is not None:
            if (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count < 0
            ):
                raise DiagramAssetError(
                    "workbook diagram image_occurrence_count is invalid."
                )
            if expected_count != len(diagram.images):
                raise DiagramAssetError(
                    "Attached diagram image count does not match the project."
                )
        records = _expected_image_records(marker)
        if required and not records:
            raise DiagramAssetError(
                "Required workbook diagram provenance has no image records."
            )
        if records and len(records) != len(diagram.images):
            raise DiagramAssetError(
                "Attached diagram image records do not match the project."
            )
        for order, (record_value, image) in enumerate(
            zip(records, diagram.images),
            start=1,
        ):
            if not isinstance(record_value, Mapping):
                raise DiagramAssetError(
                    f"workbook diagram image record {order} must be a mapping."
                )
            record_order = record_value.get("order", order)
            if record_order != order:
                raise DiagramAssetError(
                    "workbook diagram image record order is invalid."
                )
            expected = image.provenance_dict(order=order)
            for key in (
                "source_label",
                "source_part",
                "normalized_sha256",
                "width",
                "height",
            ):
                if record_value.get(key) != expected[key]:
                    raise DiagramAssetError(
                        f"Attached diagram image {order} {key} does not "
                        "match the project."
                    )
    return diagram


__all__ = [
    "DiagramAssetError",
    "MAX_WORKBOOK_DIAGRAM_IMAGES",
    "MAX_WORKBOOK_DIAGRAM_IMAGE_BYTES",
    "MAX_WORKBOOK_DIAGRAM_DIMENSION",
    "MAX_WORKBOOK_DIAGRAM_TOTAL_BYTES",
    "WORKBOOK_DIAGRAM_MARKER_KEY",
    "WORKBOOK_DIAGRAM_NORMALIZATION",
    "WORKBOOK_DIAGRAM_REPRESENTATION",
    "WorkbookDiagram",
    "WorkbookDiagramImage",
    "validate_workbook_diagram_for_project",
    "workbook_diagram_from_source",
]
