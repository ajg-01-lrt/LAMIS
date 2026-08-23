"""Multi-shelf route planning model for Ciena RLS deliverables.

This module deliberately separates *route/document planning* from exact CLI
provider selection. ATLAS currently accepts only RLS R4.0. Generic Add/Drop,
ILA, and ROADM words are site roles rather than complete configuration
identities, so a role-only record remains planning-only. A versioned reviewed
payload may select one of the explicitly registered R4.0
release/chassis/topology/BOM providers; it is then validated again against the
route identity before CLI is authorized.

The JSON format is intentionally small, deterministic, and free of credential
fields so a saved route can safely travel with a project deliverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence

from .common import (
    ConfigValidationError,
    FIBER_TYPES,
    SUPPORTED_SOFTWARE_RELEASE,
)

LOGGER = logging.getLogger(__name__)


ROUTE_SCHEMA_NAME = "atlas.ciena.rls.route-project"
ROUTE_SCHEMA_VERSION = "1.2"
_LEGACY_ROUTE_SCHEMA_VERSIONS = frozenset({"1.0", "1.1"})
RACK_CAPACITY = 8
_ROUTE_NATIVE_FIBER_REVIEW_KEY = "route_native_fiber_review"
_ROUTE_NATIVE_FIBER_REVIEW_KEYS = frozenset(
    {"value", "scope", "action", "status", "deployable_cli"}
)

Severity = Literal["error", "warning"]
ReviewState = Literal["manual", "pending", "confirmed", "corrected"]
PropagationDirection = Literal["A_TO_Z", "Z_TO_A"]
REVIEW_STATES: tuple[ReviewState, ...] = (
    "manual",
    "pending",
    "confirmed",
    "corrected",
)
PROPAGATION_DIRECTIONS: tuple[PropagationDirection, ...] = (
    "A_TO_Z",
    "Z_TO_A",
)

_R40_EXACT_ROLE_PROFILES = frozenset(
    {
        "add_drop_a",
        "add_drop_z",
        "add_drop",
        "ila",
        "roadm_a",
        "roadm_z",
        "roadm",
    }
)

R40_PENDING_SRA_PEER_REVIEW = "R40_PENDING_SRA_PEER_REVIEW"
R40_ROUTE_TOPOLOGY_MISMATCH = "R40_ROUTE_TOPOLOGY_MISMATCH"


@dataclass(frozen=True)
class ShelfProfile:
    """A supported route-planning profile.

    ``release_family_hint`` and ``variant_hint`` describe the release and
    hardware evidence needed to select an audited provider. They are not CLI
    authorization; every generic R4.0 role requires a separate exact-provider
    payload.
    """

    profile_id: str
    display_name: str
    family: str
    side: str
    rack_label: str
    irm_bucket: str | None
    cli_status: str
    cli_provider: str | None
    release_family_hint: str | None = None
    variant_hint: str | None = None
    evidence_note: str = ""

    @property
    def planning_only(self) -> bool:
        return self.cli_status != "authorized_provider"

    @property
    def label(self) -> str:
        """Short alias useful to UI controls."""

        return self.display_name


_PROFILES = (
    ShelfProfile(
        profile_id="add_drop_a",
        display_name="Add/Drop — A endpoint",
        family="add_drop",
        side="A",
        rack_label="ADD/DROP A",
        irm_bucket="add_drop_a",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports fixed-CMD, CDC, CDA, and TDA add/drop "
            "arrangements. A generic Add/Drop role does not identify the "
            "chassis, band, modules/slots, add/drop structure, or protection "
            "design required for executable CLI."
        ),
    ),
    ShelfProfile(
        profile_id="add_drop_z",
        display_name="Add/Drop — Z endpoint",
        family="add_drop",
        side="Z",
        rack_label="ADD/DROP Z",
        irm_bucket="add_drop_z",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports fixed-CMD, CDC, CDA, and TDA add/drop "
            "arrangements. A generic Add/Drop role does not identify the "
            "chassis, band, modules/slots, add/drop structure, or protection "
            "design required for executable CLI."
        ),
    ),
    ShelfProfile(
        profile_id="add_drop",
        display_name="Add/Drop — intermediate or side unresolved",
        family="add_drop",
        side="",
        rack_label="ADD/DROP",
        irm_bucket="add_drop_unassigned",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports intermediate and multi-node add/drop roles, "
            "but this neutral route role does not establish an A/Z IRM bucket "
            "or one exact executable topology."
        ),
    ),
    ShelfProfile(
        profile_id="ila",
        display_name="In-line amplifier (ILA)",
        family="ila",
        side="",
        rack_label="ILA",
        irm_bucket="ila",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports DLA/DLE, optional-SRA, dual-rail, cascaded, and "
            "protected ILA arrangements across multiple chassis families. A "
            "generic ILA role does not select one executable configuration."
        ),
    ),
    ShelfProfile(
        profile_id="roadm_a",
        display_name="ROADM — A endpoint",
        family="roadm",
        side="A",
        rack_label="ROADM A",
        irm_bucket="roadm",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports several CDC/CDA/TDA, fixed-CMD, and protected "
            "ROADM arrangements. A generic ROADM role does not identify the "
            "RLA family, degrees, add/drop modules, protection, or path layout."
        ),
    ),
    ShelfProfile(
        profile_id="roadm_z",
        display_name="ROADM — Z endpoint",
        family="roadm",
        side="Z",
        rack_label="ROADM Z",
        irm_bucket="roadm",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports several CDC/CDA/TDA, fixed-CMD, and protected "
            "ROADM arrangements. A generic ROADM role does not identify the "
            "RLA family, degrees, add/drop modules, protection, or path layout."
        ),
    ),
    ShelfProfile(
        profile_id="roadm",
        display_name="ROADM — intermediate or side unresolved",
        family="roadm",
        side="",
        rack_label="ROADM",
        irm_bucket="roadm",
        cli_status="planning_only_provider_not_implemented",
        cli_provider=None,
        release_family_hint="RLS R4.0",
        evidence_note=(
            "RLS R4.0 supports intermediate, multidegree, cascaded, and "
            "protected ROADM roles. The neutral route role preserves that "
            "position without selecting an executable topology."
        ),
    ),
)

PROFILE_REGISTRY: Mapping[str, ShelfProfile] = MappingProxyType(
    {profile.profile_id: profile for profile in _PROFILES}
)
PROFILE_IDS = tuple(PROFILE_REGISTRY)
PROFILE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        profile_id: profile.display_name
        for profile_id, profile in PROFILE_REGISTRY.items()
    }
)


@dataclass(frozen=True)
class Site:
    """One physical site referenced by one or more ordered shelves."""

    site_key: str
    code: str
    name: str
    address: str = ""
    network_site_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "site_key": self.site_key,
            "code": self.code,
            "name": self.name,
            "address": self.address,
            "network_site_id": self.network_site_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Site":
        data = _expect_mapping(value, "site")
        return cls(
            site_key=_required_json_string(data, "site_key", "site.site_key"),
            code=_required_json_string(data, "code", "site.code"),
            name=_required_json_string(data, "name", "site.name"),
            address=_optional_json_string(data, "address", "site.address"),
            network_site_id=_optional_json_string(
                data, "network_site_id", "site.network_site_id"
            ),
        )


@dataclass(frozen=True)
class PathEndpointReview:
    """One shelf's reviewed, direction-local view of a shared optical path.

    A physical span can have different engineered loss and local CLI link names
    at its two ends.  Keeping these facts per endpoint prevents one shelf review
    from overwriting the facing shelf's values while preserving the diagram's
    route-level path record.
    """

    shelf_id: str
    link_name: str
    expected_loss_db: float
    fiber_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shelf_id": self.shelf_id,
            "link_name": self.link_name,
            "expected_loss_db": self.expected_loss_db,
            "fiber_type": self.fiber_type,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PathEndpointReview":
        data = _expect_mapping(value, "path endpoint review")
        loss = _optional_json_number(
            data,
            "expected_loss_db",
            "path_endpoint_review.expected_loss_db",
        )
        if loss is None:
            raise RouteProjectFormatError(
                "path_endpoint_review.expected_loss_db is required"
            )
        return cls(
            shelf_id=_required_json_string(
                data, "shelf_id", "path_endpoint_review.shelf_id"
            ),
            link_name=_required_json_string(
                data, "link_name", "path_endpoint_review.link_name"
            ),
            expected_loss_db=loss,
            fiber_type=_required_json_string(
                data, "fiber_type", "path_endpoint_review.fiber_type"
            ),
        )


@dataclass(frozen=True)
class OpticalPath:
    """One reviewed physical span/path carried by a route link.

    A canonical linear adjacency carries one shared physical fiber-pair record.
    Its A→Z and Z→A egress engineering is held in endpoint reviews and exposed
    through :func:`route_link_propagation_views`. A link may contain multiple
    paths only for a separately modeled parallel/protection arrangement; that
    layout remains propagation-ambiguous until explicitly supported. Optional
    engineering values stay absent until the diagram or an operator supplies
    them.
    """

    path_id: str
    path_role: str
    link_name: str = ""
    expected_loss_db: float | None = None
    distance_km: float | None = None
    fiber_type: str = ""
    circuit_id: str = ""
    fiber_start: int | None = None
    fiber_end: int | None = None
    review_state: ReviewState = "manual"
    source_evidence: Mapping[str, object] = field(default_factory=dict)
    endpoint_reviews: tuple[PathEndpointReview, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_evidence, Mapping):
            raise TypeError("source_evidence must be a mapping")
        object.__setattr__(self, "endpoint_reviews", tuple(self.endpoint_reviews))
        object.__setattr__(
            self,
            "source_evidence",
            MappingProxyType(
                {
                    str(key): _freeze_json_value(value)
                    for key, value in self.source_evidence.items()
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "path_role": self.path_role,
            "link_name": self.link_name,
            "expected_loss_db": self.expected_loss_db,
            "distance_km": self.distance_km,
            "fiber_type": self.fiber_type,
            "circuit_id": self.circuit_id,
            "fiber_start": self.fiber_start,
            "fiber_end": self.fiber_end,
            "review_state": self.review_state,
            "source_evidence": _thaw_json_value(self.source_evidence),
            "endpoint_reviews": [
                review.to_dict() for review in self.endpoint_reviews
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpticalPath":
        data = _expect_mapping(value, "optical path")
        review_state = data.get("review_state", "manual")
        if not isinstance(review_state, str):
            raise RouteProjectFormatError(
                "optical_path.review_state must be a JSON string"
            )
        source_evidence = data.get("source_evidence", {})
        if not isinstance(source_evidence, Mapping):
            raise RouteProjectFormatError(
                "optical_path.source_evidence must be a JSON object"
            )
        raw_endpoint_reviews = _expect_json_array(
            data.get("endpoint_reviews", []),
            "optical_path.endpoint_reviews",
        )
        return cls(
            path_id=_required_json_string(
                data, "path_id", "optical_path.path_id"
            ),
            path_role=_required_json_string(
                data, "path_role", "optical_path.path_role"
            ),
            link_name=_optional_json_string(
                data, "link_name", "optical_path.link_name"
            ),
            expected_loss_db=_optional_json_number(
                data,
                "expected_loss_db",
                "optical_path.expected_loss_db",
            ),
            distance_km=_optional_json_number(
                data, "distance_km", "optical_path.distance_km"
            ),
            fiber_type=_optional_json_string(
                data, "fiber_type", "optical_path.fiber_type"
            ),
            circuit_id=_optional_json_string(
                data, "circuit_id", "optical_path.circuit_id"
            ),
            fiber_start=_optional_json_integer(
                data, "fiber_start", "optical_path.fiber_start"
            ),
            fiber_end=_optional_json_integer(
                data, "fiber_end", "optical_path.fiber_end"
            ),
            review_state=review_state,  # type: ignore[arg-type]
            source_evidence=dict(source_evidence),
            endpoint_reviews=tuple(
                PathEndpointReview.from_dict(
                    _expect_mapping(
                        item,
                        f"optical_path.endpoint_reviews[{index}]",
                    )
                )
                for index, item in enumerate(raw_endpoint_reviews)
            ),
        )


@dataclass(frozen=True)
class RouteLink:
    """One ordered adjacency between two shelves, with one or more paths."""

    link_id: str
    order: int
    from_shelf_id: str
    to_shelf_id: str
    paths: tuple[OpticalPath, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", tuple(self.paths))

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "order": self.order,
            "from_shelf_id": self.from_shelf_id,
            "to_shelf_id": self.to_shelf_id,
            "paths": [path.to_dict() for path in self.paths],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteLink":
        data = _expect_mapping(value, "route link")
        raw_paths = _expect_json_array(data.get("paths"), "route_link.paths")
        return cls(
            link_id=_required_json_string(
                data, "link_id", "route_link.link_id"
            ),
            order=_required_json_integer(
                data, "order", "route_link.order"
            ),
            from_shelf_id=_required_json_string(
                data, "from_shelf_id", "route_link.from_shelf_id"
            ),
            to_shelf_id=_required_json_string(
                data, "to_shelf_id", "route_link.to_shelf_id"
            ),
            paths=tuple(
                OpticalPath.from_dict(
                    _expect_mapping(item, f"route_link.paths[{index}]")
                )
                for index, item in enumerate(raw_paths)
            ),
        )


@dataclass(frozen=True)
class RoutePropagationView:
    """One propagation direction derived from a physical route adjacency.

    Route projects retain one :class:`OpticalPath` as the shared physical-span
    context.  This view does not duplicate that span.  It binds the endpoint
    review at the transmitting shelf to one explicit traffic direction:

    * the ordered link's ``from_shelf_id`` review is A-to-Z egress;
    * the ordered link's ``to_shelf_id`` review is Z-to-A egress.

    ``egress_review`` remains ``None`` when that shelf has not been reviewed.
    Consumers must not substitute the facing endpoint's review.
    """

    direction: PropagationDirection
    egress_shelf_id: str
    ingress_shelf_id: str
    shared_span: OpticalPath
    egress_review: PathEndpointReview | None


def route_link_propagation_views(
    link: RouteLink,
) -> tuple[RoutePropagationView, ...]:
    """Return the two traffic directions over one ordered physical span.

    A canonical route adjacency currently carries exactly one physical
    ``OpticalPath``.  Zero-path and multi-path links are ambiguous and return
    no views, so a caller cannot silently choose or collapse parallel paths.
    Missing or duplicate endpoint reviews are represented as a missing
    ``egress_review``; the shared physical span remains available for
    non-directional context such as distance and circuit identity.
    """

    if not isinstance(link, RouteLink):
        raise TypeError("link must be a RouteLink")
    if len(link.paths) != 1:
        return ()
    shared_span = link.paths[0]
    if not isinstance(shared_span, OpticalPath):
        return ()

    def unique_review(shelf_id: str) -> PathEndpointReview | None:
        if not isinstance(shelf_id, str):
            return None
        shelf_key = shelf_id.casefold()
        reviews = tuple(
            review
            for review in shared_span.endpoint_reviews
            if isinstance(review, PathEndpointReview)
            and isinstance(review.shelf_id, str)
            and review.shelf_id.casefold() == shelf_key
        )
        return reviews[0] if len(reviews) == 1 else None

    return (
        RoutePropagationView(
            direction="A_TO_Z",
            egress_shelf_id=link.from_shelf_id,
            ingress_shelf_id=link.to_shelf_id,
            shared_span=shared_span,
            egress_review=unique_review(link.from_shelf_id),
        ),
        RoutePropagationView(
            direction="Z_TO_A",
            egress_shelf_id=link.to_shelf_id,
            ingress_shelf_id=link.from_shelf_id,
            shared_span=shared_span,
            egress_review=unique_review(link.to_shelf_id),
        ),
    )


def _has_exact_route_native_fiber_marker_shape(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and frozenset(value) == _ROUTE_NATIVE_FIBER_REVIEW_KEYS
        and value.get("scope") == "all_active_route_spans"
        and value.get("action") == "operator_apply_route_native_fiber"
        and value.get("status") == "confirmed"
        and value.get("deployable_cli") is False
    )


def _route_native_fiber_marker(
    path: OpticalPath,
) -> tuple[str, str]:
    """Return one path marker's state and exact native fiber token."""

    if _ROUTE_NATIVE_FIBER_REVIEW_KEY not in path.source_evidence:
        return ("missing", "")
    marker = path.source_evidence[_ROUTE_NATIVE_FIBER_REVIEW_KEY]
    if not _has_exact_route_native_fiber_marker_shape(marker):
        return ("invalid", "")
    assert isinstance(marker, Mapping)
    token = marker.get("value")
    if (
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or token not in FIBER_TYPES
        or path.fiber_type != token
        or any(
            not isinstance(review, PathEndpointReview)
            or review.fiber_type != token
            for review in path.endpoint_reviews
        )
    ):
        return ("invalid", "")
    return ("confirmed", token)


def route_native_fiber_review(
    links: Iterable[RouteLink],
) -> tuple[str, str]:
    """Derive the single reviewed native-fiber choice carried by route paths.

    The returned state is one of ``not_applicable``, ``missing``,
    ``confirmed``, or ``invalid``. The token is populated only for a confirmed
    review. A confirmed review requires every populated path to carry the
    exact operator-confirmation marker, use the same supported native fiber
    token on the path itself, and have no endpoint review that disagrees.
    """

    path_count = 0
    missing = False
    invalid = False
    tokens: set[str] = set()
    for link in links:
        if not isinstance(link, RouteLink):
            invalid = True
            continue
        for path in link.paths:
            path_count += 1
            if not isinstance(path, OpticalPath):
                invalid = True
                continue
            state, token = _route_native_fiber_marker(path)
            if state == "missing":
                missing = True
            elif state == "invalid":
                invalid = True
            else:
                tokens.add(token)

    if not path_count and not invalid:
        return ("not_applicable", "")
    if invalid or len(tokens) > 1:
        return ("invalid", "")
    if missing:
        return ("missing", "")
    if len(tokens) == 1:
        return ("confirmed", next(iter(tokens)))
    return ("invalid", "")


@dataclass(frozen=True)
class ShelfInstance:
    """An ordered shelf/device entry in the route.

    ``raman_label`` is display text such as ``"Slot 4"``; it is intentionally
    not treated as a Boolean and never authorizes CLI behavior.
    """

    shelf_id: str
    profile_id: str
    software_release: str
    shelf_variant: str
    site_key: str
    tid: str
    primary_oam_ip: str
    power_label: str
    raman_label: str = ""
    notes: str = ""
    profile_payload: Mapping[str, object] = field(default_factory=dict)
    review_state: ReviewState = "manual"
    source_evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.profile_payload, Mapping):
            raise TypeError("profile_payload must be a mapping")
        if not isinstance(self.source_evidence, Mapping):
            raise TypeError("source_evidence must be a mapping")
        object.__setattr__(
            self,
            "profile_payload",
            MappingProxyType(
                {
                    str(key): _freeze_json_value(value)
                    for key, value in self.profile_payload.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "source_evidence",
            MappingProxyType(
                {
                    str(key): _freeze_json_value(value)
                    for key, value in self.source_evidence.items()
                }
            ),
        )

    @property
    def profile(self) -> ShelfProfile | None:
        return PROFILE_REGISTRY.get(self.profile_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shelf_id": self.shelf_id,
            "profile_id": self.profile_id,
            "software_release": self.software_release,
            "shelf_variant": self.shelf_variant,
            "site_key": self.site_key,
            "tid": self.tid,
            "primary_oam_ip": self.primary_oam_ip,
            "power_label": self.power_label,
            "raman_label": self.raman_label,
            "notes": self.notes,
            "profile_payload": _thaw_json_value(self.profile_payload),
            "review_state": self.review_state,
            "source_evidence": _thaw_json_value(self.source_evidence),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShelfInstance":
        data = _expect_mapping(value, "shelf")
        payload = data.get("profile_payload", {})
        if not isinstance(payload, Mapping):
            raise RouteProjectFormatError(
                "shelf.profile_payload must be a JSON object"
            )
        review_state = data.get("review_state", "manual")
        if not isinstance(review_state, str):
            raise RouteProjectFormatError(
                "shelf.review_state must be a JSON string"
            )
        source_evidence = data.get("source_evidence", {})
        if not isinstance(source_evidence, Mapping):
            raise RouteProjectFormatError(
                "shelf.source_evidence must be a JSON object"
            )
        return cls(
            shelf_id=_required_json_string(data, "shelf_id", "shelf.shelf_id"),
            profile_id=_required_json_string(
                data, "profile_id", "shelf.profile_id"
            ),
            software_release=_required_json_string(
                data, "software_release", "shelf.software_release"
            ),
            shelf_variant=_required_json_string(
                data, "shelf_variant", "shelf.shelf_variant"
            ),
            site_key=_required_json_string(data, "site_key", "shelf.site_key"),
            tid=_required_json_string(data, "tid", "shelf.tid"),
            primary_oam_ip=_required_json_string(
                data, "primary_oam_ip", "shelf.primary_oam_ip"
            ),
            power_label=_required_json_string(
                data, "power_label", "shelf.power_label"
            ),
            raman_label=_optional_json_string(
                data, "raman_label", "shelf.raman_label"
            ),
            notes=_optional_json_string(data, "notes", "shelf.notes"),
            profile_payload=dict(payload),
            review_state=review_state,
            source_evidence=dict(source_evidence),
        )


@dataclass(frozen=True)
class IRMCounts:
    """Counts used to populate the dynamic IRM input cells."""

    total_distinct_sites: int
    ila_shelves: int
    roadm_shelves: int
    add_drop_a_shelves: int
    add_drop_z_shelves: int
    add_drop_unassigned_shelves: int
    unmapped_shelves: int
    total_shelves: int

    @property
    def total_add_drop_shelves(self) -> int:
        return (
            self.add_drop_a_shelves
            + self.add_drop_z_shelves
            + self.add_drop_unassigned_shelves
        )

    def as_irm_drivers(self) -> dict[str, int]:
        """Return the known literal IRM driver-cell values.

        Formula cells are intentionally absent.  The workbook renderer patches
        only these literals and leaves the template's formula text untouched.
        """

        return {
            "B6": self.total_distinct_sites,
            "B7": self.ila_shelves,
            "B9": self.roadm_shelves,
            "B10": self.total_add_drop_shelves,
            "F18": self.ila_shelves,
            "G18": self.roadm_shelves,
            "H18": self.add_drop_a_shelves,
            "I18": self.add_drop_z_shelves,
        }

    def to_dict(self) -> dict[str, int]:
        return {
            "total_distinct_sites": self.total_distinct_sites,
            "ila_shelves": self.ila_shelves,
            "roadm_shelves": self.roadm_shelves,
            "add_drop_a_shelves": self.add_drop_a_shelves,
            "add_drop_z_shelves": self.add_drop_z_shelves,
            "add_drop_unassigned_shelves": self.add_drop_unassigned_shelves,
            "total_add_drop_shelves": self.total_add_drop_shelves,
            "unmapped_shelves": self.unmapped_shelves,
            "total_shelves": self.total_shelves,
        }


@dataclass(frozen=True)
class RouteValidationIssue:
    severity: Severity
    code: str
    field: str
    message: str
    source: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


class RouteProjectValidationError(ValueError):
    def __init__(self, issues: Sequence[RouteValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(
            "; ".join(
                f"{issue.field}: {issue.message}"
                for issue in self.issues
                if issue.severity == "error"
            )
            or "Route project validation failed"
        )


class RouteProjectFormatError(ValueError):
    """Raised when a route JSON document does not match the schema."""


@dataclass(frozen=True)
class ShelfDeploymentStatus:
    shelf_id: str
    profile_id: str
    provider_available: bool
    ready: bool
    reason_codes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentReadiness:
    ready: bool
    shelf_statuses: tuple[ShelfDeploymentStatus, ...]
    blocking_reasons: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.ready

    @property
    def blocked_shelves(self) -> tuple[ShelfDeploymentStatus, ...]:
        return tuple(status for status in self.shelf_statuses if not status.ready)


@dataclass(frozen=True)
class R40RouteTopologyIssue:
    """One structured exact-provider/route topology finding.

    ``peer_shelf_id`` is populated only when a valid local SRA endpoint is
    waiting for the facing endpoint's exact-provider review.  Keeping that
    state structured lets the editor stage the first endpoint without
    mistaking any other topology mismatch for an acceptable deferral.
    """

    code: str
    message: str
    peer_shelf_id: str = ""


@dataclass(frozen=True)
class RouteProject:
    """A complete, ordered route from which a deliverable can be rendered."""

    project_id: str
    route_code: str
    title: str
    ospf_area: str = ""
    sites: tuple[Site, ...] = ()
    shelves: tuple[ShelfInstance, ...] = ()
    links: tuple[RouteLink, ...] = ()
    revision: str = "1"
    notes: str = ""
    schema_version: str = ROUTE_SCHEMA_VERSION
    diagram_source: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.diagram_source, Mapping):
            raise TypeError("diagram_source must be a mapping")
        object.__setattr__(self, "sites", tuple(self.sites))
        object.__setattr__(self, "shelves", tuple(self.shelves))
        object.__setattr__(self, "links", tuple(self.links))
        object.__setattr__(
            self,
            "diagram_source",
            MappingProxyType(
                {
                    str(key): _freeze_json_value(value)
                    for key, value in self.diagram_source.items()
                }
            ),
        )

    def site_by_key(self, site_key: str) -> Site | None:
        for site in self.sites:
            if site.site_key == site_key:
                return site
        return None

    def ordered_site_keys(self) -> tuple[str, ...]:
        seen: set[str] = set()
        result: list[str] = []
        for shelf in self.shelves:
            if shelf.site_key not in seen:
                seen.add(shelf.site_key)
                result.append(shelf.site_key)
        return tuple(result)

    def endpoint_sites(self) -> tuple[Site | None, Site | None]:
        keys = self.ordered_site_keys()
        if not keys:
            return (None, None)
        return (self.site_by_key(keys[0]), self.site_by_key(keys[-1]))

    def rack_groups(
        self, capacity: int = RACK_CAPACITY
    ) -> tuple[tuple[ShelfInstance, ...], ...]:
        """Chunk shelves in route order, never allowing more than eight/rack."""

        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise TypeError("rack capacity must be an integer")
        if capacity < 1 or capacity > RACK_CAPACITY:
            raise ValueError(
                f"rack capacity must be between 1 and {RACK_CAPACITY}"
            )
        return tuple(
            tuple(self.shelves[index : index + capacity])
            for index in range(0, len(self.shelves), capacity)
        )

    @property
    def rack_count(self) -> int:
        return len(self.rack_groups())

    def irm_counts(self) -> IRMCounts:
        profiles = [PROFILE_REGISTRY.get(shelf.profile_id) for shelf in self.shelves]
        ila = sum(profile is not None and profile.irm_bucket == "ila" for profile in profiles)
        roadm = sum(
            profile is not None and profile.irm_bucket == "roadm"
            for profile in profiles
        )
        add_drop_a = sum(
            profile is not None and profile.irm_bucket == "add_drop_a"
            for profile in profiles
        )
        add_drop_z = sum(
            profile is not None and profile.irm_bucket == "add_drop_z"
            for profile in profiles
        )
        add_drop_unassigned = sum(
            profile is not None
            and profile.irm_bucket == "add_drop_unassigned"
            for profile in profiles
        )
        mapped = ila + roadm + add_drop_a + add_drop_z
        return IRMCounts(
            total_distinct_sites=len(
                {shelf.site_key for shelf in self.shelves if shelf.site_key}
            ),
            ila_shelves=ila,
            roadm_shelves=roadm,
            add_drop_a_shelves=add_drop_a,
            add_drop_z_shelves=add_drop_z,
            add_drop_unassigned_shelves=add_drop_unassigned,
            unmapped_shelves=len(self.shelves) - mapped,
            total_shelves=len(self.shelves),
        )

    def validate(self) -> tuple[RouteValidationIssue, ...]:
        return validate_route_project(self)

    def assert_valid(self) -> None:
        errors = tuple(issue for issue in self.validate() if issue.is_error)
        if errors:
            raise RouteProjectValidationError(errors)

    def deployment_readiness(self) -> DeploymentReadiness:
        return route_deployment_readiness(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": ROUTE_SCHEMA_NAME,
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "route_code": self.route_code,
            "title": self.title,
            "ospf_area": self.ospf_area,
            "revision": self.revision,
            "notes": self.notes,
            "diagram_source": _thaw_json_value(self.diagram_source),
            "sites": [site.to_dict() for site in self.sites],
            "shelves": [shelf.to_dict() for shelf in self.shelves],
            "links": [link.to_dict() for link in self.links],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteProject":
        data = _expect_mapping(value, "route project")
        schema_name = _required_json_string(
            data, "schema_name", "route.schema_name"
        )
        if schema_name != ROUTE_SCHEMA_NAME:
            raise RouteProjectFormatError(
                f"Unsupported route schema_name {schema_name!r}"
            )
        schema_version = _required_json_string(
            data, "schema_version", "route.schema_version"
        )
        if schema_version not in {
            *_LEGACY_ROUTE_SCHEMA_VERSIONS,
            ROUTE_SCHEMA_VERSION,
        }:
            raise RouteProjectFormatError(
                f"Unsupported route schema_version {schema_version!r}; "
                f"expected one of "
                f"{', '.join(sorted(_LEGACY_ROUTE_SCHEMA_VERSIONS | {ROUTE_SCHEMA_VERSION}))}"
            )
        raw_sites = _expect_json_array(data.get("sites"), "route.sites")
        raw_shelves = _expect_json_array(data.get("shelves"), "route.shelves")
        if schema_version in _LEGACY_ROUTE_SCHEMA_VERSIONS:
            ospf_area = ""
            raw_links: list[Any] = []
        else:
            ospf_area = _required_json_string(
                data, "ospf_area", "route.ospf_area"
            )
            raw_links = _expect_json_array(data.get("links"), "route.links")
        diagram_source = data.get("diagram_source", {})
        if not isinstance(diagram_source, Mapping):
            raise RouteProjectFormatError(
                "route.diagram_source must be a JSON object"
            )
        return cls(
            project_id=_required_json_string(
                data, "project_id", "route.project_id"
            ),
            route_code=_required_json_string(
                data, "route_code", "route.route_code"
            ),
            title=_required_json_string(data, "title", "route.title"),
            ospf_area=ospf_area,
            revision=_required_json_string(
                data, "revision", "route.revision"
            ),
            notes=_optional_json_string(data, "notes", "route.notes"),
            sites=tuple(
                Site.from_dict(_expect_mapping(item, f"route.sites[{index}]"))
                for index, item in enumerate(raw_sites)
            ),
            shelves=tuple(
                ShelfInstance.from_dict(
                    _expect_mapping(item, f"route.shelves[{index}]")
                )
                for index, item in enumerate(raw_shelves)
            ),
            links=tuple(
                RouteLink.from_dict(
                    _expect_mapping(item, f"route.links[{index}]")
                )
                for index, item in enumerate(raw_links)
            ),
            # Version 1.0 had no diagram provenance/review state and versions
            # 1.0/1.1 had no first-class OSPF area or optical links. Legacy
            # documents are upgraded in memory with blank route engineering
            # data rather than inferring values from provenance or payloads.
            schema_version=ROUTE_SCHEMA_VERSION,
            diagram_source=dict(diagram_source),
        )

    def save(self, path: str | os.PathLike[str]) -> Path:
        return save_route_project(self, path)

    def save_draft(self, path: str | os.PathLike[str]) -> Path:
        """Save a reviewable incomplete project without weakening final gates."""

        return save_route_project_draft(self, path)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "RouteProject":
        return load_route_project(path)

    @classmethod
    def load_draft(cls, path: str | os.PathLike[str]) -> "RouteProject":
        """Load a structurally safe project that may still have review blockers."""

        return load_route_project_draft(path)


_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "community",
    "privatekey",
    "licensekey",
    "token",
    "apikey",
    "credential",
)
_RAMAN_CALLOUT_CONVENTION_ID = "small-red-slot-port-v1"
_RAMAN_CALLOUT_RE = re.compile(
    r"^(?P<slot>[1-9]\d{0,2})/(?P<port>[56])$"
)


def validate_route_project(project: RouteProject) -> tuple[RouteValidationIssue, ...]:
    issues: list[RouteValidationIssue] = []

    if project.schema_version != ROUTE_SCHEMA_VERSION:
        issues.append(
            _error(
                "UNSUPPORTED_SCHEMA_VERSION",
                "schema_version",
                f"Schema version must be {ROUTE_SCHEMA_VERSION}.",
            )
        )

    for field_name, label, value in (
        ("project_id", "Project ID", project.project_id),
        ("route_code", "Route code", project.route_code),
        ("title", "Project title", project.title),
        ("revision", "Revision", project.revision),
    ):
        _validate_required_text(value, field_name, label, issues)
    _validate_optional_text(
        project.ospf_area, "ospf_area", "OSPF area", issues
    )
    if isinstance(project.ospf_area, str) and project.ospf_area.strip():
        try:
            ipaddress.IPv4Address(project.ospf_area.strip())
        except (ipaddress.AddressValueError, ValueError):
            issues.append(
                _error(
                    "INVALID_OSPF_AREA",
                    "ospf_area",
                    "OSPF area must be a dotted-quad value such as 0.0.0.0.",
                )
            )
    _validate_optional_text(project.notes, "notes", "Project notes", issues)
    for key_path in _secret_key_paths(project.diagram_source):
        issues.append(
            _error(
                "SECRET_FIELD_NOT_ALLOWED",
                f"diagram_source{key_path}",
                (
                    "Credential- or secret-bearing fields are not allowed in "
                    "route diagram provenance. Store credentials in the approved "
                    "secret management system, not the deliverable."
                ),
            )
        )
    try:
        json.dumps(
            _thaw_json_value(project.diagram_source),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        issues.append(
            _error(
                "DIAGRAM_SOURCE_NOT_JSON_SAFE",
                "diagram_source",
                "Diagram source provenance must contain only finite JSON-safe values.",
            )
        )
    raw_unassigned_raman = project.diagram_source.get(
        "unassigned_raman_callouts", ()
    )
    if raw_unassigned_raman and not isinstance(
        raw_unassigned_raman, (list, tuple)
    ):
        issues.append(
            _error(
                "INVALID_RAMAN_CALLOUT_PROVENANCE",
                "diagram_source.unassigned_raman_callouts",
                "Unassigned RAMAN callout provenance must be a JSON array.",
            )
        )
    elif isinstance(raw_unassigned_raman, (list, tuple)):
        unresolved_callouts = [
            item
            for item in raw_unassigned_raman
            if not isinstance(item, Mapping)
            or item.get("context") != "legend_sample"
        ]
        if unresolved_callouts:
            issues.append(
                _error(
                    "UNASSIGNED_RAMAN_CALLOUT",
                    "diagram_source.unassigned_raman_callouts",
                    (
                        "At least one source-scoped RAMAN slot/port callout "
                        "could not be associated with a shelf endpoint. Resolve "
                        "or reject it before bundle export."
                    ),
                )
            )

    if not project.sites:
        issues.append(
            _error(
                "NO_SITES",
                "sites",
                "At least one site is required for a route project.",
            )
        )
    if not project.shelves:
        issues.append(
            _error(
                "NO_SHELVES",
                "shelves",
                "At least one ordered shelf is required for a route project.",
            )
        )

    site_keys: dict[str, int] = {}
    site_codes: dict[str, int] = {}
    for index, site in enumerate(project.sites):
        prefix = f"sites[{index}]"
        _validate_required_text(
            site.site_key, f"{prefix}.site_key", "Site key", issues
        )
        _validate_required_text(site.code, f"{prefix}.code", "Site code", issues)
        _validate_required_text(site.name, f"{prefix}.name", "Site name", issues)
        _validate_optional_text(
            site.address, f"{prefix}.address", "Site address", issues
        )
        _validate_optional_text(
            site.network_site_id,
            f"{prefix}.network_site_id",
            "Network site ID",
            issues,
        )
        _record_duplicate(
            site.site_key,
            index,
            site_keys,
            issues,
            "DUPLICATE_SITE_KEY",
            f"{prefix}.site_key",
            "Site keys must be unique (case-insensitive).",
        )
        _record_duplicate(
            site.code,
            index,
            site_codes,
            issues,
            "DUPLICATE_SITE_CODE",
            f"{prefix}.code",
            "Site codes must be unique (case-insensitive).",
        )

    known_site_keys = {site.site_key for site in project.sites}
    ordered_site_keys = project.ordered_site_keys()
    endpoint_a_key = ordered_site_keys[0] if ordered_site_keys else None
    endpoint_z_key = ordered_site_keys[-1] if ordered_site_keys else None
    side_specific_shelves = [
        shelf
        for shelf in project.shelves
        if (PROFILE_REGISTRY.get(shelf.profile_id) is not None)
        and PROFILE_REGISTRY[shelf.profile_id].side in {"A", "Z"}
    ]
    if len(ordered_site_keys) == 1 and side_specific_shelves:
        issues.append(
            _warning(
                "SINGLE_SITE_ENDPOINT_AMBIGUITY",
                "shelves",
                (
                    "This route has one distinct site, so A- and Z-side shelf "
                    "designations resolve to the same endpoint. The placement is "
                    "allowed for documentation, but verify that the side labels "
                    "are intentional."
                ),
            )
        )
    shelf_ids: dict[str, int] = {}
    tids: dict[str, int] = {}
    oam_ips: dict[str, int] = {}
    for index, shelf in enumerate(project.shelves):
        prefix = f"shelves[{index}]"
        _validate_required_text(
            shelf.shelf_id, f"{prefix}.shelf_id", "Shelf ID", issues
        )
        _validate_required_text(
            shelf.site_key, f"{prefix}.site_key", "Shelf site", issues
        )
        _validate_required_text(shelf.tid, f"{prefix}.tid", "TID", issues)
        _validate_required_text(
            shelf.software_release,
            f"{prefix}.software_release",
            "Software release",
            issues,
        )
        if (
            isinstance(shelf.software_release, str)
            and shelf.software_release.strip()
            and not _release_matches_hint(
                shelf.software_release, SUPPORTED_SOFTWARE_RELEASE
            )
        ):
            issues.append(
                _error(
                    "UNSUPPORTED_SOFTWARE_RELEASE",
                    f"{prefix}.software_release",
                    (
                        "The current RLS Route Builder accepts only "
                        f"{SUPPORTED_SOFTWARE_RELEASE}; this project must not be "
                        "silently converted from another customer release."
                    ),
                    "ATLAS active software-release contract.",
                )
            )
        _validate_required_text(
            shelf.shelf_variant,
            f"{prefix}.shelf_variant",
            "Shelf variant",
            issues,
        )
        _validate_required_text(
            shelf.primary_oam_ip,
            f"{prefix}.primary_oam_ip",
            "Primary OAM IP",
            issues,
        )
        _validate_required_text(
            shelf.power_label, f"{prefix}.power_label", "Power", issues
        )
        _validate_optional_text(
            shelf.raman_label, f"{prefix}.raman_label", "RAMAN", issues
        )
        _validate_optional_text(shelf.notes, f"{prefix}.notes", "Shelf notes", issues)
        if (
            not isinstance(shelf.review_state, str)
            or shelf.review_state not in REVIEW_STATES
        ):
            issues.append(
                _error(
                    "INVALID_REVIEW_STATE",
                    f"{prefix}.review_state",
                    (
                        "Shelf review state must be one of: "
                        + ", ".join(REVIEW_STATES)
                        + "."
                    ),
                )
            )
        elif shelf.review_state == "pending":
            issues.append(
                _warning(
                    "PENDING_SHELF_REVIEW",
                    f"{prefix}.review_state",
                    (
                        "This diagram-imported shelf is pending human review and "
                        "cannot be used for deployment configuration generation."
                    ),
                )
            )

        _record_duplicate(
            shelf.shelf_id,
            index,
            shelf_ids,
            issues,
            "DUPLICATE_SHELF_ID",
            f"{prefix}.shelf_id",
            "Shelf IDs must be unique (case-insensitive).",
        )
        _record_duplicate(
            shelf.tid,
            index,
            tids,
            issues,
            "DUPLICATE_TID",
            f"{prefix}.tid",
            "TIDs must be unique (case-insensitive).",
        )

        if shelf.site_key and shelf.site_key not in known_site_keys:
            issues.append(
                _error(
                    "UNKNOWN_SITE",
                    f"{prefix}.site_key",
                    f"Shelf references unknown site key {shelf.site_key!r}.",
                )
            )

        profile = PROFILE_REGISTRY.get(shelf.profile_id)
        if profile is None:
            issues.append(
                _error(
                    "UNKNOWN_PROFILE",
                    f"{prefix}.profile_id",
                    f"Unknown shelf profile {shelf.profile_id!r}.",
                )
            )
        else:
            if (
                len(ordered_site_keys) > 1
                and profile.side == "A"
                and shelf.site_key != endpoint_a_key
            ):
                issues.append(
                    _error(
                        "INVALID_A_ENDPOINT_PLACEMENT",
                        f"{prefix}.site_key",
                        (
                            f"{profile.display_name} must be placed at the first "
                            f"ordered distinct route site ({endpoint_a_key!r})."
                        ),
                    )
                )
            if (
                len(ordered_site_keys) > 1
                and profile.side == "Z"
                and shelf.site_key != endpoint_z_key
            ):
                issues.append(
                    _error(
                        "INVALID_Z_ENDPOINT_PLACEMENT",
                        f"{prefix}.site_key",
                        (
                            f"{profile.display_name} must be placed at the last "
                            f"ordered distinct route site ({endpoint_z_key!r})."
                        ),
                    )
                )
            if profile.planning_only and not _has_r40_exact_payload(
                shelf.profile_payload
            ):
                issues.append(
                    _warning(
                        "PLANNING_ONLY_PROFILE",
                        f"{prefix}.profile_id",
                        (
                            f"{profile.display_name} is available for route/FBN/IRM "
                            "planning only. The role does not contain the exact "
                            "release/chassis/module/topology discriminator required "
                            "to select an audited CLI provider."
                        ),
                        profile.evidence_note,
                    )
                )
        canonical_ip: str | None = None
        if shelf.primary_oam_ip.strip():
            try:
                canonical_ip = str(
                    ipaddress.ip_address(shelf.primary_oam_ip.strip())
                )
            except ValueError:
                issues.append(
                    _error(
                        "INVALID_OAM_IP",
                        f"{prefix}.primary_oam_ip",
                        "Primary OAM IP must be a valid IPv4 or IPv6 address.",
                    )
                )
        if canonical_ip is not None:
            _record_duplicate(
                canonical_ip,
                index,
                oam_ips,
                issues,
                "DUPLICATE_OAM_IP",
                f"{prefix}.primary_oam_ip",
                "Primary OAM IP addresses must be unique.",
            )

        for key_path in _secret_key_paths(shelf.profile_payload):
            issues.append(
                _error(
                    "SECRET_FIELD_NOT_ALLOWED",
                    f"{prefix}.profile_payload{key_path}",
                    (
                        "Credential- or secret-bearing fields are not allowed in "
                        "a route project. Store credentials in the approved secret "
                        "management system, not the deliverable."
                    ),
                )
            )
        try:
            json.dumps(_thaw_json_value(shelf.profile_payload), allow_nan=False)
        except (TypeError, ValueError):
            issues.append(
                _error(
                    "PROFILE_PAYLOAD_NOT_JSON_SAFE",
                    f"{prefix}.profile_payload",
                    "Profile payload must contain only finite JSON-safe values.",
                )
            )
        for key_path in _secret_key_paths(shelf.source_evidence):
            issues.append(
                _error(
                    "SECRET_FIELD_NOT_ALLOWED",
                    f"{prefix}.source_evidence{key_path}",
                    (
                        "Credential- or secret-bearing fields are not allowed in "
                        "diagram source evidence. Store credentials in the approved "
                        "secret management system, not the deliverable."
                    ),
                )
            )
        try:
            json.dumps(
                _thaw_json_value(shelf.source_evidence),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            issues.append(
                _error(
                    "SOURCE_EVIDENCE_NOT_JSON_SAFE",
                    f"{prefix}.source_evidence",
                    "Source evidence must contain only finite JSON-safe values.",
                )
            )
        raman_state, _raman_slots = _structured_sra_state(shelf)
        if raman_state == "invalid":
            issues.append(
                _error(
                    "INVALID_RAMAN_CALLOUT_EVIDENCE",
                    f"{prefix}.source_evidence.raman_callouts",
                    (
                        "Structured RAMAN slot/port evidence is incomplete, "
                        "inconsistent with this shelf, or not bound to the "
                        "source-scoped convention."
                    ),
                )
            )

    try:
        json.dumps(
            [link.to_dict() for link in project.links],
            allow_nan=False,
        )
    except (AttributeError, TypeError, ValueError):
        issues.append(
            _error(
                "LINKS_NOT_JSON_SAFE",
                "links",
                "Optical links must contain only finite JSON-safe values.",
            )
        )

    expected_link_count = max(0, len(project.shelves) - 1)
    if project.links and len(project.links) != expected_link_count:
        issues.append(
            _error(
                "LINK_COUNT_MISMATCH",
                "links",
                (
                    f"A populated ordered route with {len(project.shelves)} "
                    f"shelves requires {expected_link_count} adjacency link(s)."
                ),
            )
        )

    known_shelf_ids = {
        shelf.shelf_id
        for shelf in project.shelves
        if isinstance(shelf.shelf_id, str)
    }
    link_ids: dict[str, int] = {}
    path_ids: dict[str, int] = {}
    route_native_fiber_tokens: list[tuple[str, str]] = []
    for link_index, link in enumerate(project.links):
        prefix = f"links[{link_index}]"
        if not isinstance(link, RouteLink):
            issues.append(
                _error(
                    "INVALID_LINK",
                    prefix,
                    "Every route link must be a RouteLink record.",
                )
            )
            continue
        _validate_required_text(
            link.link_id, f"{prefix}.link_id", "Link ID", issues
        )
        _validate_required_text(
            link.from_shelf_id,
            f"{prefix}.from_shelf_id",
            "Link source shelf ID",
            issues,
        )
        _validate_required_text(
            link.to_shelf_id,
            f"{prefix}.to_shelf_id",
            "Link destination shelf ID",
            issues,
        )
        _record_duplicate(
            link.link_id,
            link_index,
            link_ids,
            issues,
            "DUPLICATE_LINK_ID",
            f"{prefix}.link_id",
            "Link IDs must be unique (case-insensitive).",
        )
        if (
            isinstance(link.order, bool)
            or not isinstance(link.order, int)
            or link.order != link_index + 1
        ):
            issues.append(
                _error(
                    "INVALID_LINK_ORDER",
                    f"{prefix}.order",
                    (
                        "Link order must be an integer matching its contiguous "
                        f"route position {link_index + 1}."
                    ),
                )
            )

        for endpoint_name, endpoint_value in (
            ("from_shelf_id", link.from_shelf_id),
            ("to_shelf_id", link.to_shelf_id),
        ):
            if (
                isinstance(endpoint_value, str)
                and endpoint_value
                and endpoint_value not in known_shelf_ids
            ):
                issues.append(
                    _error(
                        "UNKNOWN_LINK_ENDPOINT",
                        f"{prefix}.{endpoint_name}",
                        "Link endpoint must reference an existing shelf ID.",
                    )
                )
        if (
            isinstance(link.from_shelf_id, str)
            and isinstance(link.to_shelf_id, str)
            and link.from_shelf_id.strip()
            and link.to_shelf_id.strip()
            and link.from_shelf_id.strip().casefold()
            == link.to_shelf_id.strip().casefold()
        ):
            issues.append(
                _error(
                    "SELF_LINK",
                    prefix,
                    "A route link must connect two different shelves.",
                )
            )

        if link_index < expected_link_count:
            expected_from = project.shelves[link_index].shelf_id
            expected_to = project.shelves[link_index + 1].shelf_id
            if (
                link.from_shelf_id != expected_from
                or link.to_shelf_id != expected_to
            ):
                issues.append(
                    _error(
                        "LINK_ENDPOINT_ORDER_MISMATCH",
                        prefix,
                        (
                            "Link endpoints must match the adjacent shelves at "
                            "the same ordered route position."
                        ),
                    )
                )

        if not link.paths:
            issues.append(
                _error(
                    "NO_LINK_PATHS",
                    f"{prefix}.paths",
                    "Every populated route link requires at least one optical path.",
                )
            )
        for path_index, path in enumerate(link.paths):
            path_prefix = f"{prefix}.paths[{path_index}]"
            if not isinstance(path, OpticalPath):
                issues.append(
                    _error(
                        "INVALID_OPTICAL_PATH",
                        path_prefix,
                        "Every link path must be an OpticalPath record.",
                    )
                )
                continue
            _validate_required_text(
                path.path_id,
                f"{path_prefix}.path_id",
                "Optical path ID",
                issues,
            )
            _validate_required_text(
                path.path_role,
                f"{path_prefix}.path_role",
                "Optical path role",
                issues,
            )
            for field_name, label, value in (
                ("link_name", "Path link name", path.link_name),
                ("fiber_type", "Path fiber type", path.fiber_type),
                ("circuit_id", "Path circuit ID", path.circuit_id),
            ):
                _validate_optional_text(
                    value, f"{path_prefix}.{field_name}", label, issues
                )
            _record_duplicate(
                path.path_id,
                len(path_ids),
                path_ids,
                issues,
                "DUPLICATE_PATH_ID",
                f"{path_prefix}.path_id",
                "Optical path IDs must be unique (case-insensitive).",
            )
            for field_name, label, value in (
                (
                    "expected_loss_db",
                    "Expected optical loss",
                    path.expected_loss_db,
                ),
                ("distance_km", "Optical distance", path.distance_km),
            ):
                _validate_optional_nonnegative_number(
                    value,
                    f"{path_prefix}.{field_name}",
                    label,
                    issues,
                )
            for field_name, label, value in (
                ("fiber_start", "Fiber start", path.fiber_start),
                ("fiber_end", "Fiber end", path.fiber_end),
            ):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 1
                ):
                    issues.append(
                        _error(
                            "INVALID_FIBER_RANGE",
                            f"{path_prefix}.{field_name}",
                            f"{label} must be a positive integer when provided.",
                        )
                    )
            if (
                isinstance(path.fiber_start, int)
                and not isinstance(path.fiber_start, bool)
                and isinstance(path.fiber_end, int)
                and not isinstance(path.fiber_end, bool)
                and path.fiber_start > path.fiber_end
            ):
                issues.append(
                    _error(
                        "INVALID_FIBER_RANGE",
                        f"{path_prefix}.fiber_start",
                        "Fiber start cannot be greater than fiber end.",
                    )
                )
            reviewed_endpoint_ids: dict[str, int] = {}
            valid_link_endpoints = {
                endpoint.casefold()
                for endpoint in (link.from_shelf_id, link.to_shelf_id)
                if isinstance(endpoint, str)
            }
            for review_index, review in enumerate(path.endpoint_reviews):
                review_prefix = (
                    f"{path_prefix}.endpoint_reviews[{review_index}]"
                )
                if not isinstance(review, PathEndpointReview):
                    issues.append(
                        _error(
                            "INVALID_PATH_ENDPOINT_REVIEW",
                            review_prefix,
                            "Every endpoint review must be a "
                            "PathEndpointReview record.",
                        )
                    )
                    continue
                _validate_required_text(
                    review.shelf_id,
                    f"{review_prefix}.shelf_id",
                    "Reviewed endpoint shelf ID",
                    issues,
                )
                _validate_required_text(
                    review.link_name,
                    f"{review_prefix}.link_name",
                    "Endpoint-local CLI link name",
                    issues,
                )
                _validate_required_text(
                    review.fiber_type,
                    f"{review_prefix}.fiber_type",
                    "Endpoint fiber type",
                    issues,
                )
                _validate_optional_nonnegative_number(
                    review.expected_loss_db,
                    f"{review_prefix}.expected_loss_db",
                    "Endpoint expected optical loss",
                    issues,
                )
                endpoint_key = (
                    review.shelf_id.casefold()
                    if isinstance(review.shelf_id, str)
                    else ""
                )
                if endpoint_key not in valid_link_endpoints:
                    issues.append(
                        _error(
                            "PATH_REVIEW_ENDPOINT_MISMATCH",
                            f"{review_prefix}.shelf_id",
                            "Endpoint review must reference one of the two "
                            "shelves connected by this route link.",
                        )
                    )
                _record_duplicate(
                    review.shelf_id,
                    review_index,
                    reviewed_endpoint_ids,
                    issues,
                    "DUPLICATE_PATH_ENDPOINT_REVIEW",
                    f"{review_prefix}.shelf_id",
                    "A path can contain only one review per endpoint shelf.",
                )
            marker_field = (
                f"{path_prefix}.source_evidence."
                f"{_ROUTE_NATIVE_FIBER_REVIEW_KEY}"
            )
            if _ROUTE_NATIVE_FIBER_REVIEW_KEY not in path.source_evidence:
                issues.append(
                    _warning(
                        "MISSING_ROUTE_NATIVE_FIBER_REVIEW",
                        marker_field,
                        (
                            "The route-wide native fiber choice has not been "
                            "confirmed for this optical path."
                        ),
                    )
                )
            else:
                marker = path.source_evidence[
                    _ROUTE_NATIVE_FIBER_REVIEW_KEY
                ]
                if not _has_exact_route_native_fiber_marker_shape(marker):
                    issues.append(
                        _error(
                            "INVALID_ROUTE_NATIVE_FIBER_REVIEW",
                            marker_field,
                            (
                                "The route-native fiber review marker must "
                                "contain exactly value, scope, action, status, "
                                "and deployable_cli with the controlled "
                                "operator-confirmation values."
                            ),
                        )
                    )
                else:
                    assert isinstance(marker, Mapping)
                    marker_token = marker.get("value")
                    if (
                        not isinstance(marker_token, str)
                        or not marker_token
                        or marker_token != marker_token.strip()
                    ):
                        issues.append(
                            _error(
                                "INVALID_ROUTE_NATIVE_FIBER_REVIEW",
                                f"{marker_field}.value",
                                (
                                    "The confirmed route-native fiber value "
                                    "must be a non-empty trimmed string."
                                ),
                            )
                        )
                    else:
                        route_native_fiber_tokens.append(
                            (marker_token, marker_field)
                        )
                        if marker_token not in FIBER_TYPES:
                            issues.append(
                                _error(
                                    "UNSUPPORTED_ROUTE_NATIVE_FIBER",
                                    f"{marker_field}.value",
                                    (
                                        "The confirmed route-native fiber "
                                        "value is not supported by the exact "
                                        "RLS R4.0 providers."
                                    ),
                                )
                            )
                        if path.fiber_type != marker_token:
                            issues.append(
                                _error(
                                    "ROUTE_NATIVE_FIBER_PATH_MISMATCH",
                                    f"{path_prefix}.fiber_type",
                                    (
                                        "The path fiber type must match the "
                                        "confirmed route-wide native fiber "
                                        "choice."
                                    ),
                                )
                            )
                        for review_index, review in enumerate(
                            path.endpoint_reviews
                        ):
                            if (
                                isinstance(review, PathEndpointReview)
                                and review.fiber_type != marker_token
                            ):
                                issues.append(
                                    _error(
                                        (
                                            "ROUTE_NATIVE_FIBER_ENDPOINT_"
                                            "MISMATCH"
                                        ),
                                        (
                                            f"{path_prefix}.endpoint_reviews"
                                            f"[{review_index}].fiber_type"
                                        ),
                                        (
                                            "The endpoint fiber type must "
                                            "match the confirmed route-wide "
                                            "native fiber choice."
                                        ),
                                    )
                                )
            if (
                not isinstance(path.review_state, str)
                or path.review_state not in REVIEW_STATES
            ):
                issues.append(
                    _error(
                        "INVALID_REVIEW_STATE",
                        f"{path_prefix}.review_state",
                        (
                            "Optical path review state must be one of: "
                            + ", ".join(REVIEW_STATES)
                            + "."
                        ),
                    )
                )
            elif path.review_state == "pending":
                issues.append(
                    _warning(
                        "PENDING_PATH_REVIEW",
                        f"{path_prefix}.review_state",
                        (
                            "This diagram-imported optical path is pending "
                            "human review."
                        ),
                    )
                )
            for key_path in _secret_key_paths(path.source_evidence):
                issues.append(
                    _error(
                        "SECRET_FIELD_NOT_ALLOWED",
                        f"{path_prefix}.source_evidence{key_path}",
                        (
                            "Credential- or secret-bearing fields are not "
                            "allowed in optical-path source evidence."
                        ),
                    )
                )
            try:
                json.dumps(
                    _thaw_json_value(path.source_evidence),
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                issues.append(
                    _error(
                        "PATH_SOURCE_EVIDENCE_NOT_JSON_SAFE",
                        f"{path_prefix}.source_evidence",
                        (
                            "Optical-path source evidence must contain only "
                            "finite JSON-safe values."
                        ),
                    )
                )

    if len({token for token, _field in route_native_fiber_tokens}) > 1:
        issues.append(
            _error(
                "ROUTE_NATIVE_FIBER_MISMATCH",
                "links",
                (
                    "All populated optical paths must use one identical "
                    "confirmed route-native fiber value."
                ),
            )
        )

    return tuple(issues)


def _route_native_fiber_readiness_findings(
    project: RouteProject,
) -> tuple[tuple[frozenset[str], str, str], ...]:
    """Return shelf-scoped deployment blocks for the route-native review."""

    if len(project.shelves) < 2 or not project.links:
        return ()

    findings: list[tuple[frozenset[str], str, str]] = []
    route_shelf_ids: set[str] = set()
    shaped_tokens: set[str] = set()

    for link in project.links:
        if not isinstance(link, RouteLink):
            continue
        link_shelf_ids = frozenset(
            shelf_id
            for shelf_id in (link.from_shelf_id, link.to_shelf_id)
            if isinstance(shelf_id, str) and shelf_id
        )
        for path in link.paths:
            if not isinstance(path, OpticalPath):
                continue
            route_shelf_ids.update(link_shelf_ids)
            if _ROUTE_NATIVE_FIBER_REVIEW_KEY not in path.source_evidence:
                findings.append(
                    (
                        link_shelf_ids,
                        "MISSING_ROUTE_NATIVE_FIBER_REVIEW",
                        (
                            "The route-wide native fiber choice has not been "
                            "operator-confirmed for every optical path touching "
                            "this shelf."
                        ),
                    )
                )
                continue

            marker = path.source_evidence[
                _ROUTE_NATIVE_FIBER_REVIEW_KEY
            ]
            if not _has_exact_route_native_fiber_marker_shape(marker):
                findings.append(
                    (
                        link_shelf_ids,
                        "INVALID_ROUTE_NATIVE_FIBER_REVIEW",
                        (
                            "The route-native fiber operator-confirmation "
                            "marker is malformed or incomplete."
                        ),
                    )
                )
                continue

            assert isinstance(marker, Mapping)
            token = marker.get("value")
            if (
                not isinstance(token, str)
                or not token
                or token != token.strip()
            ):
                findings.append(
                    (
                        link_shelf_ids,
                        "INVALID_ROUTE_NATIVE_FIBER_REVIEW",
                        (
                            "The confirmed route-native fiber value is "
                            "missing or malformed."
                        ),
                    )
                )
                continue

            shaped_tokens.add(token)
            if token not in FIBER_TYPES:
                findings.append(
                    (
                        link_shelf_ids,
                        "UNSUPPORTED_ROUTE_NATIVE_FIBER",
                        (
                            "The confirmed route-native fiber value is not "
                            "supported by the exact RLS R4.0 providers."
                        ),
                    )
                )
            if path.fiber_type != token:
                findings.append(
                    (
                        link_shelf_ids,
                        "ROUTE_NATIVE_FIBER_PATH_MISMATCH",
                        (
                            "An optical path fiber type does not match the "
                            "confirmed route-wide native fiber choice."
                        ),
                    )
                )
            for review in path.endpoint_reviews:
                if (
                    not isinstance(review, PathEndpointReview)
                    or review.fiber_type == token
                ):
                    continue
                review_shelves = (
                    frozenset({review.shelf_id})
                    if review.shelf_id in link_shelf_ids
                    else link_shelf_ids
                )
                findings.append(
                    (
                        review_shelves,
                        "ROUTE_NATIVE_FIBER_ENDPOINT_MISMATCH",
                        (
                            "An endpoint-local fiber review does not match "
                            "the confirmed route-wide native fiber choice."
                        ),
                    )
                )

    if len(shaped_tokens) > 1:
        findings.append(
            (
                frozenset(route_shelf_ids),
                "ROUTE_NATIVE_FIBER_MISMATCH",
                (
                    "The route contains more than one confirmed native fiber "
                    "value; one choice must apply to all active route spans."
                ),
            )
        )
    return tuple(findings)


def route_deployment_readiness(project: RouteProject) -> DeploymentReadiness:
    statuses: list[ShelfDeploymentStatus] = []
    for shelf in project.shelves:
        profile = PROFILE_REGISTRY.get(shelf.profile_id)
        provider_available = _provider_available_for_shelf(
            profile,
            shelf,
            project,
        )
        if shelf.review_state == "pending":
            statuses.append(
                ShelfDeploymentStatus(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    provider_available=provider_available,
                    ready=False,
                    reason_codes=("PENDING_SHELF_REVIEW",),
                    reasons=(
                        (
                            "The shelf was imported from a route diagram and "
                            "must be reviewed and confirmed or corrected before "
                            "deployment configuration generation."
                        ),
                    ),
                )
            )
            continue
        if shelf.review_state not in REVIEW_STATES:
            statuses.append(
                ShelfDeploymentStatus(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    provider_available=provider_available,
                    ready=False,
                    reason_codes=("INVALID_REVIEW_STATE",),
                    reasons=(
                        "The shelf review state is invalid; deployment is blocked.",
                    ),
                )
            )
            continue
        if profile is None:
            statuses.append(
                ShelfDeploymentStatus(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    provider_available=False,
                    ready=False,
                    reason_codes=("UNKNOWN_PROFILE",),
                    reasons=("No registered CLI provider exists for this profile.",),
                )
            )
            continue
        if profile.planning_only and shelf.profile_id in _R40_EXACT_ROLE_PROFILES:
            if not shelf.profile_payload:
                raman_state, raman_slots = _structured_sra_state(shelf)
                if raman_state == "invalid":
                    statuses.append(
                        ShelfDeploymentStatus(
                            shelf_id=shelf.shelf_id,
                            profile_id=shelf.profile_id,
                            provider_available=False,
                            ready=False,
                            reason_codes=(
                                "INVALID_RAMAN_CALLOUT_EVIDENCE",
                            ),
                            reasons=(
                                "Structured RAMAN slot/port evidence is "
                                "incomplete or inconsistent; no exact provider "
                                "may run.",
                            ),
                        )
                    )
                    continue
                if raman_state == "pending":
                    statuses.append(
                        ShelfDeploymentStatus(
                            shelf_id=shelf.shelf_id,
                            profile_id=shelf.profile_id,
                            provider_available=False,
                            ready=False,
                            reason_codes=(
                                "PENDING_RAMAN_CALLOUT_REVIEW",
                            ),
                            reasons=(
                                "The source contains a complete SRA slot/port "
                                "endpoint that must be accepted or rejected by "
                                "the operator.",
                            ),
                        )
                    )
                    continue
                if raman_state == "accepted":
                    from .r4_0_generator import provider_profiles_for_role

                    compatible_sra = tuple(
                        provider
                        for provider in provider_profiles_for_role(
                            shelf.profile_id
                        )
                        if provider.supports_raman
                    )
                    if not compatible_sra:
                        slot_summary = ", ".join(
                            str(slot) for slot in raman_slots
                        )
                        statuses.append(
                            ShelfDeploymentStatus(
                                shelf_id=shelf.shelf_id,
                                profile_id=shelf.profile_id,
                                provider_available=False,
                                ready=False,
                                reason_codes=(
                                    "R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE",
                                ),
                                reasons=(
                                    "Reviewed diagram evidence establishes an "
                                    f"SRA endpoint in slot(s) {slot_summary}, "
                                    "but no vendor-audited SRA-capable RLS "
                                    "R4.0 provider is registered. ATLAS will "
                                    "not substitute a no-SRA provider or "
                                    "infer Raman commands.",
                                ),
                            )
                        )
                        continue
                from .r4_0_generator import provider_profiles_for_role

                exact_review_available = bool(
                    provider_profiles_for_role(shelf.profile_id)
                )
                statuses.append(
                    ShelfDeploymentStatus(
                        shelf_id=shelf.shelf_id,
                        profile_id=shelf.profile_id,
                        provider_available=provider_available,
                        ready=False,
                        reason_codes=(
                            "EXACT_PROVIDER_REVIEW_REQUIRED"
                            if exact_review_available
                            else "PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED",
                        ),
                        reasons=(
                            (
                                f"{profile.display_name} has an exact "
                                "RLS R4.0 provider review available, but "
                                "no validated provider request is stored "
                                "for this shelf."
                            )
                            if exact_review_available
                            else (
                                f"{profile.display_name} remains "
                                "route-planning only because no exact audited "
                                "RLS R4.0 provider is registered for this "
                                "shelf role."
                            ),
                        ),
                    )
                )
                continue
            codes: list[str] = []
            reasons: list[str] = []
            if not _release_matches_hint(
                shelf.software_release, SUPPORTED_SOFTWARE_RELEASE
            ):
                codes.append("UNSUPPORTED_PROVIDER_RELEASE")
                reasons.append(
                    "Exact providers are authorized only for "
                    f"{SUPPORTED_SOFTWARE_RELEASE}."
                )
            payload_codes, payload_reasons = _r40_payload_readiness(
                shelf.profile_payload,
                shelf,
                project.site_by_key(shelf.site_key),
                project.ospf_area,
                project,
            )
            codes.extend(payload_codes)
            reasons.extend(payload_reasons)
            statuses.append(
                ShelfDeploymentStatus(
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    provider_available=provider_available,
                    ready=not codes,
                    reason_codes=tuple(codes),
                    reasons=tuple(reasons),
                )
            )
            continue
        statuses.append(
            ShelfDeploymentStatus(
                shelf_id=shelf.shelf_id,
                profile_id=shelf.profile_id,
                provider_available=False,
                ready=False,
                reason_codes=("UNSUPPORTED_PROVIDER_PROFILE",),
                reasons=(
                    (
                        f"{profile.display_name} has no provider in the active "
                        f"{SUPPORTED_SOFTWARE_RELEASE} contract."
                    ),
                ),
            )
        )
        continue

    incomplete_propagations: dict[str, set[PropagationDirection]] = {}
    unreviewed_path_shelves: set[str] = set()
    for link in project.links:
        propagation_views = route_link_propagation_views(link)
        if not propagation_views:
            unreviewed_path_shelves.update(
                (link.from_shelf_id, link.to_shelf_id)
            )
            continue
        missing_view = False
        for view in propagation_views:
            if view.egress_review is not None:
                continue
            incomplete_propagations.setdefault(
                view.egress_shelf_id,
                set(),
            ).add(view.direction)
            missing_view = True
        if (
            not missing_view
            and any(
                path.review_state not in {"confirmed", "corrected"}
                for path in link.paths
            )
        ):
            unreviewed_path_shelves.update(
                (link.from_shelf_id, link.to_shelf_id)
            )
    if incomplete_propagations:
        statuses = [
            (
                ShelfDeploymentStatus(
                    shelf_id=status.shelf_id,
                    profile_id=status.profile_id,
                    provider_available=status.provider_available,
                    ready=False,
                    reason_codes=_deduplicate_strings(
                        (
                            *status.reason_codes,
                            "PROPAGATION_PATH_INCOMPLETE",
                        )
                    ),
                    reasons=_deduplicate_strings(
                        (
                            *status.reasons,
                            (
                                "Review this shelf's "
                                + "/".join(
                                    "A→Z"
                                    if direction == "A_TO_Z"
                                    else "Z→A"
                                    for direction in sorted(
                                        incomplete_propagations[
                                            status.shelf_id
                                        ]
                                    )
                                )
                                + " egress engineering. A physical route "
                                "span carries both propagation directions, "
                                "and ATLAS will not copy the facing shelf's "
                                "loss or link review."
                            ),
                        )
                    ),
                )
                if status.shelf_id in incomplete_propagations
                else status
            )
            for status in statuses
        ]
    if unreviewed_path_shelves:
        code = "UNREVIEWED_OPTICAL_PATH"
        reason = (
            "The physical span or parallel-path layout is not in a canonical "
            "reviewed state; route CLI candidates remain blocked."
        )
        statuses = [
            (
                ShelfDeploymentStatus(
                    shelf_id=status.shelf_id,
                    profile_id=status.profile_id,
                    provider_available=status.provider_available,
                    ready=False,
                    reason_codes=_deduplicate_strings(
                        (*status.reason_codes, code)
                    ),
                    reasons=_deduplicate_strings((*status.reasons, reason)),
                )
                if status.shelf_id in unreviewed_path_shelves
                else status
            )
            for status in statuses
        ]

    native_fiber_blocks: dict[str, tuple[list[str], list[str]]] = {}
    for shelf_ids, code, reason in _route_native_fiber_readiness_findings(
        project
    ):
        for shelf_id in shelf_ids:
            codes, reasons = native_fiber_blocks.setdefault(
                shelf_id, ([], [])
            )
            codes.append(code)
            reasons.append(reason)
    if native_fiber_blocks:
        statuses = [
            (
                ShelfDeploymentStatus(
                    shelf_id=status.shelf_id,
                    profile_id=status.profile_id,
                    provider_available=status.provider_available,
                    ready=False,
                    reason_codes=_deduplicate_strings(
                        (
                            *status.reason_codes,
                            *native_fiber_blocks[status.shelf_id][0],
                        )
                    ),
                    reasons=_deduplicate_strings(
                        (
                            *status.reasons,
                            *native_fiber_blocks[status.shelf_id][1],
                        )
                    ),
                )
                if status.shelf_id in native_fiber_blocks
                else status
            )
            for status in statuses
        ]

    validation_errors = tuple(
        issue for issue in project.validate() if issue.severity == "error"
    )
    blocking_reasons = [
        f"{issue.field}: {issue.message}" for issue in validation_errors
    ]
    blocking_reasons.extend(
        reason
        for status in statuses
        if not status.ready
        for reason in status.reasons
    )
    if statuses and any(status.ready for status in statuses) and any(
        not status.ready for status in statuses
    ):
        blocking_reasons.append(
            "The mixed route contains at least one shelf without an authorized "
            "provider; route CLI export fails closed and will not emit a partial "
            "deployment bundle."
        )
    if not statuses:
        blocking_reasons.append(
            "The route has no shelves; there is nothing to generate."
        )
    ready = bool(statuses) and not validation_errors and all(
        status.ready for status in statuses
    )
    return DeploymentReadiness(
        ready=ready,
        shelf_statuses=tuple(statuses),
        blocking_reasons=_deduplicate_strings(blocking_reasons),
    )


def save_route_project(
    project: RouteProject, path: str | os.PathLike[str]
) -> Path:
    """Validate and atomically save a deterministic UTF-8 route JSON file."""

    if not isinstance(project, RouteProject):
        raise TypeError("project must be a RouteProject")
    project.assert_valid()
    destination = Path(path)
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        project.to_dict(),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise
    return destination


_DRAFT_UNSAFE_CODES = frozenset(
    {
        "UNSUPPORTED_SCHEMA_VERSION",
        "SECRET_FIELD_NOT_ALLOWED",
        "DIAGRAM_SOURCE_NOT_JSON_SAFE",
        "LINKS_NOT_JSON_SAFE",
        "PATH_SOURCE_EVIDENCE_NOT_JSON_SAFE",
        "PROFILE_PAYLOAD_NOT_JSON_SAFE",
        "SOURCE_EVIDENCE_NOT_JSON_SAFE",
        "CONTROL_CHARACTER_NOT_ALLOWED",
        "INVALID_TEXT",
        "INVALID_REVIEW_STATE",
    }
)


def save_route_project_draft(
    project: RouteProject,
    path: str | os.PathLike[str],
) -> Path:
    """Atomically save an incomplete but structurally safe review draft.

    Required-field, ordering, identity, provider, and deployment-readiness
    findings remain in the project for the operator to resolve. Credential
    keys, control characters, non-JSON data, invalid review states, and schema
    mismatches are never persisted through the draft path.
    """

    if not isinstance(project, RouteProject):
        raise TypeError("project must be a RouteProject")
    _assert_draft_safe(project)
    destination = Path(path)
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        project.to_dict(),
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise
    return destination


def route_project_fingerprint(project: RouteProject) -> str:
    """Return a deterministic SHA-256 fingerprint of the complete project.

    The hash input is the UTF-8 encoding of ``project.to_dict()`` serialized
    with sorted object keys, no insignificant whitespace, and non-finite
    numbers rejected. No field emitted by ``to_dict`` is excluded: schema
    identity/version, route and site data, ordered shelves, notes, provider
    payloads, review states, and diagram provenance all participate. Array
    order remains meaningful, including the order of sites and shelves.

    This fingerprints a snapshot; it does not assert that the project is
    otherwise valid or deployment-ready.
    """

    if not isinstance(project, RouteProject):
        raise TypeError("project must be a RouteProject")
    canonical_json = json.dumps(
        project.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def load_route_project(path: str | os.PathLike[str]) -> RouteProject:
    """Load, schema-check, and validate a route project."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RouteProjectFormatError(
            f"Invalid route project JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    project = RouteProject.from_dict(_expect_mapping(raw, "route project"))
    project.assert_valid()
    return project


def load_route_project_draft(path: str | os.PathLike[str]) -> RouteProject:
    """Load a review draft while retaining all ordinary validation findings."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RouteProjectFormatError(
            f"Invalid route project JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    project = RouteProject.from_dict(_expect_mapping(raw, "route project"))
    _assert_draft_safe(project)
    return project


def _assert_draft_safe(project: RouteProject) -> None:
    unsafe = tuple(
        issue
        for issue in project.validate()
        if issue.severity == "error" and issue.code in _DRAFT_UNSAFE_CODES
    )
    if unsafe:
        raise RouteProjectValidationError(unsafe)


def _has_r40_exact_payload(payload: Mapping[str, object]) -> bool:
    """Identify the versioned R4.0 envelope without inferring a provider."""

    if not isinstance(payload, Mapping):
        return False
    from .r4_0_generator import R40_PAYLOAD_SCHEMA_ID

    return payload.get("schema_id") == R40_PAYLOAD_SCHEMA_ID


def _structured_sra_state(
    shelf: ShelfInstance,
) -> tuple[str, tuple[int, ...]]:
    """Return the reviewed state of source-bound SRA slot/port evidence.

    ``raman_label`` intentionally is not consulted as a standalone hardware
    selector. Only a complete port-5/port-6 pair produced under the closed
    source convention can establish an SRA endpoint for readiness checks.
    """

    evidence = shelf.source_evidence
    raw_callouts = evidence.get("raman_callouts")
    if raw_callouts in (None, (), []):
        return ("none", ())
    if not isinstance(raw_callouts, (list, tuple)):
        return ("invalid", ())

    convention = evidence.get("raman_callout_convention")
    source_sha256 = evidence.get("source_sha256")
    if (
        not isinstance(convention, Mapping)
        or convention.get("id") != _RAMAN_CALLOUT_CONVENTION_ID
        or not isinstance(source_sha256, str)
        or not source_sha256
        or convention.get("source_sha256") != source_sha256
        or convention.get("scope") != "source"
        or convention.get("deployable_cli") is not False
    ):
        return ("invalid", ())

    raw_review = evidence.get("raman_callout_review", "pending")
    if isinstance(raw_review, Mapping):
        review = raw_review.get("status")
    else:
        review = raw_review
    if review not in {"pending", "accepted", "rejected"}:
        return ("invalid", ())
    # A rejection is a fail-closed disposition: no callout value can select
    # hardware or CLI, but the original provider data remains in provenance.
    if review == "rejected":
        return (
            ("rejected", ())
            if not shelf.raman_label.strip()
            else ("invalid", ())
        )

    ports_by_slot: dict[int, set[int]] = {}
    for raw in raw_callouts:
        if not isinstance(raw, Mapping):
            return ("invalid", ())
        slot = raw.get("slot")
        port = raw.get("port")
        raw_text = raw.get("raw_text")
        shelf_tid = raw.get("shelf_tid")
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 1
            or isinstance(port, bool)
            or port not in {5, 6}
            or not isinstance(raw_text, str)
            or _RAMAN_CALLOUT_RE.fullmatch(raw_text.strip()) is None
            or raw_text.strip() != f"{slot}/{port}"
            or raw.get("context") != "shelf_endpoint"
            or not isinstance(shelf_tid, str)
            or shelf_tid.strip().casefold() != shelf.tid.strip().casefold()
            or not isinstance(raw.get("evidence"), (list, tuple))
            or not raw.get("evidence")
            or raw.get("deployable_cli") is not False
        ):
            return ("invalid", ())
        ports_by_slot.setdefault(slot, set()).add(port)

    if not ports_by_slot or any(
        ports != {5, 6} for ports in ports_by_slot.values()
    ):
        return ("invalid", ())

    if review == "accepted" and not shelf.raman_label.strip():
        return ("invalid", ())
    return (str(review), tuple(sorted(ports_by_slot)))


def _r40_provider_sra_slots(profile: object) -> tuple[int, ...]:
    """Return SRA slots proved by both equipment and fixed line-port maps.

    The two audited Raman providers expose an SRA line-out on port 5 and
    line-in on port 6.  Requiring the slot to appear in the provider equipment
    inventory as well prevents a generic port number from being mistaken for
    an installed SRA.
    """

    if not bool(getattr(profile, "supports_raman", False)):
        return ()
    equipment = getattr(profile, "equipment", ())
    equipment_slots = {
        slot
        for item in equipment
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance((slot := item[0]), int)
            and not isinstance(slot, bool)
        )
    }
    line_outputs = getattr(profile, "line_outputs", ())
    line_inputs = getattr(profile, "line_inputs", ())
    output_slots = {
        slot
        for item in line_outputs
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance((slot := item[0]), int)
            and not isinstance(slot, bool)
            and item[1] == 5
        )
    }
    input_slots = {
        slot
        for item in line_inputs
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance((slot := item[0]), int)
            and not isinstance(slot, bool)
            and item[1] == 6
        )
    }
    return tuple(sorted(equipment_slots.intersection(output_slots, input_slots)))


def _r40_sra_evidence_mismatches(
    profile: object,
    shelf: ShelfInstance,
) -> list[str]:
    """Cross-check one exact provider against reviewed local SRA evidence."""

    state, reviewed_slots = _structured_sra_state(shelf)
    provider_supports_sra = bool(getattr(profile, "supports_raman", False))
    provider_slots = _r40_provider_sra_slots(profile)
    if not provider_supports_sra:
        if state == "accepted":
            return [
                "Reviewed SRA evidence conflicts with the selected no-SRA "
                "exact provider."
            ]
        return []
    if not provider_slots:
        return [
            "The selected SRA-capable exact provider does not contain a "
            "self-consistent SRA equipment and port-5/port-6 map."
        ]
    if state != "accepted":
        return [
            "The selected SRA-capable exact provider requires accepted, "
            "source-bound SRA slot/port evidence for this shelf."
        ]
    if set(reviewed_slots) != set(provider_slots):
        return [
            "The reviewed SRA slot set does not match the selected exact "
            "provider's SRA equipment and line endpoint slot."
        ]
    return []


def _provider_available_for_shelf(
    profile: ShelfProfile | None,
    shelf: ShelfInstance,
    project: RouteProject,
) -> bool:
    if profile is None or shelf.profile_id not in _R40_EXACT_ROLE_PROFILES:
        return False
    if not _has_r40_exact_payload(shelf.profile_payload):
        return False
    from .r4_0_generator import (
        R40_PROVIDER_CATALOG,
        decode_r40_exact_payload,
        provider_profiles_for_role,
    )

    try:
        request = decode_r40_exact_payload(shelf.profile_payload)
    except (TypeError, ValueError):
        return False
    exact_profile = R40_PROVIDER_CATALOG.get(request.provider_id)
    if exact_profile is None:
        return False
    if shelf.profile_id not in exact_profile.role_profiles:
        return False
    if _r40_provider_route_band_mismatches(
        exact_profile,
        shelf,
        project,
    ):
        return False

    raman_state, _slots = _structured_sra_state(shelf)
    if raman_state in {"invalid", "pending"}:
        return False
    if _r40_sra_evidence_mismatches(exact_profile, shelf):
        return False
    return exact_profile in provider_profiles_for_role(shelf.profile_id)


def _normalized_r40_route_band(value: object) -> str:
    """Normalize the closed optical-band vocabulary used by R4.0 providers."""

    token = re.sub(r"\s+", "", str(value or "").strip().casefold())
    return {
        "c": "c",
        "c-band": "c",
        "cband": "c",
        "l": "l",
        "l-band": "l",
        "lband": "l",
        "c+l": "c+l",
        "c+l-band": "c+l",
        "c+lband": "c+l",
        "integrated-c+l": "integrated_c+l",
        "integratedc+l": "integrated_c+l",
        "integrated_c+l": "integrated_c+l",
    }.get(token, token)


def _direct_shelf_band_override(shelf: ShelfInstance) -> str:
    """Return a shelf band only when retained direct evidence supports it."""

    raw_band = shelf.source_evidence.get("band")
    band = _normalized_r40_route_band(raw_band)
    if not band:
        return ""
    raw_fields = shelf.source_evidence.get("fields", ())
    if not isinstance(raw_fields, (list, tuple)):
        return ""

    # Keep this gate tied to the diagram importer's minimum instead of
    # introducing a second, weaker readiness threshold.
    from .diagram_import import MIN_FIELD_CONFIDENCE

    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            continue
        field_name = str(raw.get("field", "") or "").strip()
        if field_name != "band" and not field_name.endswith(".band"):
            continue
        if raw.get("method") not in {"vision", "ocr", "native_text"}:
            continue
        confidence = raw.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or float(confidence) < MIN_FIELD_CONFIDENCE
        ):
            continue
        observed = _normalized_r40_route_band(
            raw.get("normalized_value", raw.get("raw_text", ""))
        )
        if observed == band:
            return band
    return ""


def _r40_provider_route_band_mismatches(
    profile: object,
    shelf: ShelfInstance,
    project: RouteProject,
) -> list[str]:
    """Reject a provider outside a directly observed route band scope.

    A matching high-confidence direct shelf observation is an explicit
    partitioning override. Without that stronger local evidence, the
    direct-supported route header is a compatibility guard. The integrated
    editor may use that guard to narrow review options, but advisory
    preselection still requires a complete direct module inventory or a
    complete local line map plus matching chassis evidence.
    """

    route_header = project.diagram_source.get("route_header")
    if (
        not isinstance(route_header, Mapping)
        or route_header.get("optical_band_status") != "direct_supported"
    ):
        return []
    route_band = _normalized_r40_route_band(
        route_header.get("optical_band")
    )
    if not route_band:
        return []

    shelf_band = _direct_shelf_band_override(shelf)
    required_band = shelf_band or route_band
    provider_band = _normalized_r40_route_band(
        getattr(profile, "optical_band", "")
    )
    if provider_band == required_band:
        return []
    if shelf_band:
        return [
            (
                "The selected exact R4.0 provider optical band does not match "
                "the shelf's high-confidence direct band evidence. The "
                "shelf-level observation overrides the route-header scope "
                "only for this shelf."
            )
        ]
    return [
        (
            "The selected exact R4.0 provider optical band does not match the "
            "directly supported route-header optical-band scope, and no "
            "high-confidence direct shelf band override was retained."
        )
    ]


def _r40_payload_readiness(
    payload: Mapping[str, object],
    shelf: ShelfInstance,
    site: Site | None,
    route_ospf_area: str = "",
    project: RouteProject | None = None,
) -> tuple[list[str], list[str]]:
    from .r4_0_generator import (
        R40ExactConfigGenerator,
        R40_PROVIDER_CATALOG,
        decode_r40_exact_payload,
    )

    try:
        request = decode_r40_exact_payload(payload)
    except (TypeError, ValueError):
        return (
            ["INVALID_R40_EXACT_PROFILE_PAYLOAD"],
            [
                "The exact R4.0 profile payload is missing, malformed, or uses "
                "an unsupported schema; no request values were inferred."
            ],
        )

    raman_state, raman_slots = _structured_sra_state(shelf)
    if raman_state == "invalid":
        return (
            ["INVALID_RAMAN_CALLOUT_EVIDENCE"],
            [
                "Structured RAMAN slot/port evidence is incomplete or "
                "inconsistent; no exact provider may run."
            ],
        )
    if raman_state == "pending":
        return (
            ["PENDING_RAMAN_CALLOUT_REVIEW"],
            [
                "The source contains a complete SRA slot/port endpoint that "
                "must be accepted or rejected by the operator."
            ],
        )
    exact_profile = R40_PROVIDER_CATALOG.get(request.provider_id)
    if project is not None and exact_profile is not None:
        band_reasons = _r40_provider_route_band_mismatches(
            exact_profile,
            shelf,
            project,
        )
        if band_reasons:
            return (
                ["R40_EXACT_PROVIDER_ROUTE_BAND_MISMATCH"],
                band_reasons,
            )
    if (
        raman_state == "accepted"
        and (
            exact_profile is None
            or not exact_profile.supports_raman
        )
    ):
        slot_summary = ", ".join(str(slot) for slot in raman_slots)
        return (
            ["R40_EXACT_PROVIDER_SRA_CONFLICT"],
            [
                (
                    "The reviewed diagram establishes an SRA endpoint in "
                    f"slot(s) {slot_summary}, but the selected exact R4.0 "
                    "provider is explicitly no-SRA. Select a separately "
                    "audited SRA-capable provider; ATLAS will not infer Raman "
                    "commands."
                )
            ],
        )
    if exact_profile is not None and exact_profile.supports_raman:
        sra_reasons = _r40_sra_evidence_mismatches(exact_profile, shelf)
        if sra_reasons:
            provider_slots = _r40_provider_sra_slots(exact_profile)
            if not provider_slots:
                code = "R40_EXACT_PROVIDER_SRA_PORT_MAP_INVALID"
            elif raman_state != "accepted":
                code = "R40_EXACT_PROVIDER_SRA_EVIDENCE_REQUIRED"
            else:
                code = "R40_EXACT_PROVIDER_SRA_SLOT_MISMATCH"
            return ([code], sra_reasons)

    generator = R40ExactConfigGenerator()
    validation_errors = tuple(
        issue
        for issue in generator.validate(request)
        if issue.severity == "error"
    )
    if validation_errors:
        return (
            ["R40_EXACT_GENERATOR_VALIDATION_FAILED"],
            [
                "The exact R4.0 request failed its audited generator: "
                + "; ".join(
                    f"{issue.field}: {issue.message}"
                    for issue in validation_errors
                )
            ],
        )
    try:
        generator.generate(request)
    except ConfigValidationError as exc:
        return (
            ["R40_EXACT_GENERATOR_REJECTED"],
            [
                "The exact R4.0 request was rejected by its audited generator: "
                + "; ".join(
                    f"{issue.field}: {issue.message}" for issue in exc.issues
                )
            ],
        )
    except Exception:
        LOGGER.exception(
            "[RLS ROUTE] Exact R4.0 readiness evaluation failed; CLI export "
            "remains blocked."
        )
        return (
            ["R40_EXACT_GENERATOR_ERROR"],
            [
                "The exact R4.0 generator could not consume the reviewed "
                "request; CLI export remains blocked."
            ],
        )

    codes: list[str] = []
    reasons: list[str] = []
    identity_reasons = _r40_payload_identity_mismatches(
        request,
        shelf,
        site,
        route_ospf_area,
    )
    if identity_reasons:
        codes.append("R40_EXACT_PAYLOAD_IDENTITY_MISMATCH")
        reasons.extend(identity_reasons)
    if project is not None:
        topology_issues = _r40_route_topology_issues(
            request,
            shelf,
            project,
        )
        if topology_issues:
            if all(
                issue.code == R40_PENDING_SRA_PEER_REVIEW
                for issue in topology_issues
            ):
                codes.append(R40_PENDING_SRA_PEER_REVIEW)
            else:
                codes.append("R40_EXACT_ROUTE_TOPOLOGY_MISMATCH")
            reasons.extend(issue.message for issue in topology_issues)
    return (codes, reasons)


def _r40_payload_identity_mismatches(
    request: object,
    shelf: ShelfInstance,
    site: Site | None,
    route_ospf_area: str = "",
) -> list[str]:
    """Cross-check stable route identity without echoing project values."""

    reasons: list[str] = []
    request_profile = getattr(request, "profile", None)
    if request_profile != shelf.profile_id:
        reasons.append(
            "Exact R4.0 payload profile does not match the route shelf role."
        )

    request_shelf_name = getattr(request, "shelf_name", None)
    if (
        not isinstance(request_shelf_name, str)
        or request_shelf_name.strip().casefold()
        != shelf.tid.strip().casefold()
    ):
        reasons.append(
            "Exact R4.0 payload shelf_name does not match the route shelf TID."
        )

    request_release = getattr(request, "software_release", None)
    if (
        not isinstance(request_release, str)
        or _parse_release_version(request_release)
        != _parse_release_version(shelf.software_release)
    ):
        reasons.append(
            "Exact R4.0 payload software release does not match the route shelf "
            "release."
        )

    request_site_name = getattr(request, "site_name", None)
    expected_site_names = (
        {site.code.strip().casefold(), site.name.strip().casefold()}
        if site is not None
        else set()
    )
    if (
        not isinstance(request_site_name, str)
        or not request_site_name.strip()
        or request_site_name.strip().casefold() not in expected_site_names
    ):
        reasons.append(
            "Exact R4.0 payload site_name does not match the referenced route "
            "site code or name."
        )

    shelf_oam_ip = _canonical_ip(shelf.primary_oam_ip)
    request_addresses = {
        _canonical_ip(getattr(request, "loopback_ip", None))
    }
    management = getattr(request, "management", None)
    if bool(getattr(management, "enabled", False)):
        request_addresses.add(
            _canonical_ip(getattr(management, "ip_address", None))
        )
    request_addresses.discard(None)
    if shelf_oam_ip is None or shelf_oam_ip not in request_addresses:
        reasons.append(
            "Exact R4.0 route primary OAM IP does not match the payload "
            "loopback IP or enabled management-interface IP."
        )

    request_ospf_area = getattr(request, "ospf_area", None)
    if (
        route_ospf_area.strip()
        and _canonical_ip(route_ospf_area)
        != _canonical_ip(request_ospf_area)
    ):
        reasons.append(
            "Exact R4.0 payload OSPF area does not match the reviewed route "
            "OSPF area."
        )
    return reasons


def _r40_sra_line_output(
    candidate_profile: object,
    direction: int,
) -> tuple[int, int] | None:
    """Return an audited SRA slot/port for one fixed line record."""

    outputs = getattr(candidate_profile, "line_outputs", ())
    if (
        not isinstance(outputs, (list, tuple))
        or direction < 0
        or direction >= len(outputs)
    ):
        return None
    raw_output = outputs[direction]
    if (
        not isinstance(raw_output, (list, tuple))
        or len(raw_output) != 2
        or raw_output[1] != 5
        or raw_output[0] not in _r40_provider_sra_slots(candidate_profile)
    ):
        return None
    return (raw_output[0], raw_output[1])


def _r40_sra_peer_shelf_ids(
    request: object,
    shelf: ShelfInstance,
    project: RouteProject,
) -> tuple[str, ...]:
    """Return represented adjacent peers for this request's SRA outputs."""

    from .r4_0_generator import R40_PROVIDER_CATALOG

    shelf_index = next(
        (
            index
            for index, item in enumerate(project.shelves)
            if item.shelf_id == shelf.shelf_id
        ),
        None,
    )
    profile = R40_PROVIDER_CATALOG.get(
        getattr(request, "provider_id", "")
    )
    line_1_side = getattr(request, "line_1_route_side", "")
    if (
        shelf_index is None
        or profile is None
        or line_1_side not in {"A", "Z"}
    ):
        return ()

    peers: list[str] = []
    for direction in range(len(getattr(profile, "line_outputs", ()))):
        if _r40_sra_line_output(profile, direction) is None:
            continue
        route_side = (
            line_1_side
            if direction == 0
            else ("Z" if line_1_side == "A" else "A")
        )
        neighbor_index = (
            shelf_index - 1
            if route_side == "A" and shelf_index > 0
            else shelf_index + 1
            if route_side == "Z" and shelf_index < len(project.shelves) - 1
            else None
        )
        if neighbor_index is not None:
            peers.append(project.shelves[neighbor_index].shelf_id)
    return tuple(dict.fromkeys(peers))


def _r40_route_topology_issues(
    request: object,
    shelf: ShelfInstance,
    project: RouteProject,
) -> list[R40RouteTopologyIssue]:
    """Cross-check local line outputs against ordered bidirectional spans."""

    from .r4_0_generator import (
        R40_PROVIDER_CATALOG,
        decode_r40_exact_payload,
        provider_profiles_for_role,
    )

    issues: list[R40RouteTopologyIssue] = []

    def add_issue(
        message: str,
        *,
        code: str = R40_ROUTE_TOPOLOGY_MISMATCH,
        peer_shelf_id: str = "",
    ) -> None:
        issues.append(
            R40RouteTopologyIssue(
                code=code,
                message=message,
                peer_shelf_id=peer_shelf_id,
            )
        )

    shelf_index = next(
        (
            index
            for index, item in enumerate(project.shelves)
            if item.shelf_id == shelf.shelf_id
        ),
        None,
    )
    if shelf_index is None:
        add_issue("Exact R4.0 shelf is absent from the ordered route.")
        return issues
    local_profile = R40_PROVIDER_CATALOG.get(
        getattr(request, "provider_id", "")
    )
    line_1_side = getattr(request, "line_1_route_side", "")
    if line_1_side not in {"A", "Z"}:
        add_issue(
            "Exact R4.0 local line-output 1 is not assigned to route side A "
            "or Z."
        )
        return issues

    def line_record_for_side(
        candidate: object,
        candidate_profile: object,
        side: str,
    ) -> tuple[int, object] | None:
        """Return one actually provisioned line record for a route side.

        A one-degree RLA provider maps only ``line_1`` to its explicitly
        assigned physical side.  The opposing traffic propagation shares that
        same mux/demux degree; it is not a second local hardware record.
        """

        candidate_side = getattr(candidate, "line_1_route_side", "")
        if candidate_side == side:
            direction = 0
        elif candidate_side in {"A", "Z"}:
            direction = 1
        else:
            return None

        outputs = getattr(candidate_profile, "line_outputs", ())
        pfg_names = getattr(candidate_profile, "line_pfg_names", ())
        if (
            not isinstance(outputs, (list, tuple))
            or not isinstance(pfg_names, (list, tuple))
            or direction >= len(outputs)
            or direction >= len(pfg_names)
        ):
            return None
        line = getattr(
            candidate,
            "line_1" if direction == 0 else "line_2",
            None,
        )
        if line is None:
            return None
        return (direction, line)

    def append_sra_bookend_reasons(
        *,
        local_direction: int,
        neighbor: ShelfInstance,
        peer_profile: object | None,
        peer_direction: int | None,
    ) -> None:
        local_endpoint = _r40_sra_line_output(
            local_profile,
            local_direction,
        )
        peer_endpoint = (
            _r40_sra_line_output(peer_profile, peer_direction)
            if peer_profile is not None and peer_direction is not None
            else None
        )
        if local_endpoint is None and peer_endpoint is None:
            return
        if local_endpoint is None or peer_endpoint is None:
            add_issue(
                "A represented Raman span must terminate on an audited SRA "
                "port-5 line output at both facing exact providers."
            )
            return
        if (
            not bool(getattr(local_profile, "supports_raman", False))
            or not bool(getattr(peer_profile, "supports_raman", False))
        ):
            add_issue(
                "Both exact providers bookending a represented Raman span "
                "must explicitly support Raman."
            )
            return
        local_state, local_slots = _structured_sra_state(shelf)
        peer_state, peer_slots = _structured_sra_state(neighbor)
        if (
            local_state != "accepted"
            or local_endpoint[0] not in local_slots
            or peer_state != "accepted"
            or peer_endpoint[0] not in peer_slots
        ):
            add_issue(
                "Both endpoints of a represented Raman span require accepted "
                "structured SRA evidence matching their facing provider "
                "slots."
            )

    def missing_peer_has_compatible_sra_candidate(
        neighbor: ShelfInstance,
    ) -> bool:
        if (
            neighbor.review_state == "pending"
            or neighbor.review_state not in REVIEW_STATES
            or not _release_matches_hint(
                neighbor.software_release,
                SUPPORTED_SOFTWARE_RELEASE,
            )
        ):
            return False
        return any(
            profile.supports_raman
            and not _r40_sra_evidence_mismatches(profile, neighbor)
            and not _r40_provider_route_band_mismatches(
                profile,
                neighbor,
                project,
            )
            for profile in provider_profiles_for_role(neighbor.profile_id)
        )

    for direction in range(len(getattr(local_profile, "line_outputs", ()))):
        if _r40_sra_line_output(local_profile, direction) is None:
            continue
        route_side = (
            line_1_side
            if direction == 0
            else ("Z" if line_1_side == "A" else "A")
        )
        side_is_modeled = (
            route_side == "A" and shelf_index > 0
        ) or (
            route_side == "Z"
            and shelf_index < len(project.shelves) - 1
        )
        if not side_is_modeled:
            add_issue(
                f"R4.0 SRA line-output {direction + 1} is assigned to an "
                f"unmodeled {route_side}-side degree. Every SRA port-5 "
                "endpoint must map to exactly one adjacent physical span and "
                "a compatible peer."
            )

    line_records: list[tuple[ShelfInstance, object, str, int, str]] = []
    if shelf_index > 0:
        local_record = line_record_for_side(request, local_profile, "A")
        if local_record is None:
            add_issue(
                "The selected exact R4.0 provider has no physical line "
                "record assigned to the shelf's A-side adjacent span."
            )
        else:
            local_direction, local_line = local_record
            line_records.append(
                (
                    project.shelves[shelf_index - 1],
                    local_line,
                    "A",
                    local_direction,
                    "Z",
                )
            )
    if shelf_index < len(project.shelves) - 1:
        local_record = line_record_for_side(request, local_profile, "Z")
        if local_record is None:
            add_issue(
                "The selected exact R4.0 provider has no physical line "
                "record assigned to the shelf's Z-side adjacent span."
            )
        else:
            local_direction, local_line = local_record
            line_records.append(
                (
                    project.shelves[shelf_index + 1],
                    local_line,
                    "Z",
                    local_direction,
                    "A",
                )
            )
    for (
        neighbor,
        line,
        route_side,
        local_direction,
        peer_route_side,
    ) in line_records:
        line_record_number = local_direction + 1
        matching_links = [
            link
            for link in project.links
            if {link.from_shelf_id, link.to_shelf_id}
            == {shelf.shelf_id, neighbor.shelf_id}
        ]
        if len(matching_links) != 1:
            add_issue(
                f"R4.0 local line-output {line_record_number} does not map "
                "to exactly one adjacent ordered route link."
            )
            continue
        propagation_views = route_link_propagation_views(matching_links[0])
        expected_propagation = (
            "Z_TO_A" if route_side == "A" else "A_TO_Z"
        )
        expected_propagation_label = (
            "Z→A" if expected_propagation == "Z_TO_A" else "A→Z"
        )
        propagation = next(
            (
                view
                for view in propagation_views
                if view.direction == expected_propagation
                and view.egress_shelf_id == shelf.shelf_id
            ),
            None,
        )
        if propagation is None:
            add_issue(
                f"R4.0 local line-output {line_record_number} has no "
                f"{expected_propagation_label} propagation "
                "view on exactly one physical route span."
            )
            continue
        path = propagation.shared_span
        if (
            getattr(line, "neighbor_node", "").strip().casefold()
            != neighbor.tid.strip().casefold()
        ):
            add_issue(
                f"R4.0 local line-output {line_record_number} neighbor node "
                "does not match the adjacent route shelf TID."
            )
        endpoint_review = propagation.egress_review
        if endpoint_review is None:
            add_issue(
                f"R4.0 local line-output {line_record_number} is missing its "
                f"{expected_propagation_label} egress review."
            )
        else:
            request_link_name = getattr(line, "link_name", None)
            if request_link_name != endpoint_review.link_name:
                add_issue(
                    f"R4.0 local line-output {line_record_number} CLI link "
                    "name does not match its propagation egress review."
                )
            request_fiber = getattr(line, "fiber_type", None)
            if request_fiber != endpoint_review.fiber_type:
                add_issue(
                    f"R4.0 local line-output {line_record_number} fiber type "
                    "does not match its propagation egress review."
                )
            request_loss = getattr(line, "expected_loss_db", None)
            path_loss = endpoint_review.expected_loss_db
            if (
                isinstance(request_loss, bool)
                or not isinstance(request_loss, (int, float))
                or isinstance(path_loss, bool)
                or not isinstance(path_loss, (int, float))
                or not math.isclose(
                    float(request_loss),
                    float(path_loss),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                add_issue(
                    f"R4.0 local line-output {line_record_number} expected "
                    "loss does not match its propagation egress review."
                )

        if (
            local_profile is None
            or not _has_r40_exact_payload(neighbor.profile_payload)
        ):
            if (
                _r40_sra_line_output(local_profile, local_direction)
                is not None
            ):
                if missing_peer_has_compatible_sra_candidate(neighbor):
                    add_issue(
                        "The facing endpoint of a represented Raman span has "
                        "no current exact-provider payload; paired SRA "
                        "compatibility cannot be established. The local "
                        "endpoint may be staged, but deployable CLI and "
                        "route-bundle export remain blocked until the peer is "
                        "reviewed.",
                        code=R40_PENDING_SRA_PEER_REVIEW,
                        peer_shelf_id=neighbor.shelf_id,
                    )
                else:
                    add_issue(
                        "The facing endpoint of a represented Raman span has "
                        "no current exact-provider payload and no audited SRA "
                        "provider candidate compatible with its reviewed role, "
                        "route band, and accepted slot evidence. The local "
                        "endpoint cannot be staged."
                    )
            continue
        try:
            peer_request = decode_r40_exact_payload(
                neighbor.profile_payload
            )
        except (TypeError, ValueError):
            if (
                _r40_sra_line_output(local_profile, local_direction)
                is not None
            ):
                add_issue(
                    "The facing endpoint of a represented Raman span has an "
                    "invalid exact-provider payload; paired SRA compatibility "
                    "cannot be established."
                )
            continue
        peer_profile = R40_PROVIDER_CATALOG.get(peer_request.provider_id)
        if peer_profile is None:
            if (
                _r40_sra_line_output(local_profile, local_direction)
                is not None
            ):
                add_issue(
                    "The facing endpoint of a represented Raman span does not "
                    "select a registered exact provider."
                )
            continue
        peer_record = line_record_for_side(
            peer_request,
            peer_profile,
            peer_route_side,
        )
        if peer_record is None:
            add_issue(
                f"R4.0 local line-output {line_record_number} facing peer "
                "has no provisioned reciprocal physical line record on the "
                "peer's facing route side."
            )
            continue
        peer_direction, peer_line = peer_record
        append_sra_bookend_reasons(
            local_direction=local_direction,
            neighbor=neighbor,
            peer_profile=peer_profile,
            peer_direction=peer_direction,
        )
        expected_peer_mux, expected_peer_demux = (
            peer_profile.line_pfg_names[peer_direction]
        )
        if (
            getattr(line, "neighbor_line_mux_pfg", "")
            != expected_peer_mux
            or getattr(line, "neighbor_line_demux_pfg", "")
            != expected_peer_demux
        ):
            add_issue(
                f"R4.0 local line-output {line_record_number} neighbor PFG "
                "identities do not match the facing exact peer provider."
            )
        expected_local_mux, expected_local_demux = (
            local_profile.line_pfg_names[local_direction]
        )
        if (
            peer_line.neighbor_node.strip().casefold()
            != shelf.tid.strip().casefold()
            or peer_line.neighbor_line_mux_pfg != expected_local_mux
            or peer_line.neighbor_line_demux_pfg != expected_local_demux
        ):
            add_issue(
                f"R4.0 local line-output {line_record_number} facing peer "
                "request is not reciprocal with this exact provider."
            )
    return issues


def _r40_route_topology_mismatches(
    request: object,
    shelf: ShelfInstance,
    project: RouteProject,
) -> list[str]:
    """Compatibility wrapper returning human-readable topology messages."""

    return [
        issue.message
        for issue in _r40_route_topology_issues(request, shelf, project)
    ]


def _canonical_ip(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _release_matches_hint(value: str, hint: str) -> bool:
    if not value or not hint:
        return False
    value_version = _parse_release_version(value)
    hint_version = _parse_release_version(hint)
    if value_version is None or hint_version is None:
        return False
    value_major, value_minor, value_patch = value_version
    hint_major, hint_minor, hint_patch = hint_version
    if value_major != hint_major:
        return False
    # A release hint containing only a major number establishes only that
    # software family. Physical R2/R4 chassis labels must never be passed here
    # as release evidence.
    if hint_minor is None:
        return True
    if value_minor != hint_minor:
        return False
    # Exact provider authorization never extends to a patch-level variant.
    if hint_patch is None:
        return value_patch is None
    return value_patch == hint_patch


def _parse_release_version(
    value: str,
) -> tuple[int, int | None, int | None] | None:
    text = value.strip().casefold()
    text = re.sub(r"^rls[\s_-]*", "", text)
    match = re.fullmatch(
        r"r?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", text
    )
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)) if match.group(2) is not None else None,
        int(match.group(3)) if match.group(3) is not None else None,
    )


def _validate_required_text(
    value: object,
    field_name: str,
    label: str,
    issues: list[RouteValidationIssue],
) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(
            _error("REQUIRED_FIELD", field_name, f"{label} is required.")
        )
        return
    _validate_text_safety(value, field_name, label, issues)


def _validate_optional_text(
    value: object,
    field_name: str,
    label: str,
    issues: list[RouteValidationIssue],
) -> None:
    if not isinstance(value, str):
        issues.append(
            _error("INVALID_TEXT", field_name, f"{label} must be text.")
        )
        return
    if value:
        _validate_text_safety(value, field_name, label, issues)


def _validate_optional_nonnegative_number(
    value: object,
    field_name: str,
    label: str,
    issues: list[RouteValidationIssue],
) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        issues.append(
            _error(
                "INVALID_PATH_NUMBER",
                field_name,
                f"{label} must be a finite non-negative number when provided.",
            )
        )


def _validate_text_safety(
    value: str,
    field_name: str,
    label: str,
    issues: list[RouteValidationIssue],
) -> None:
    if _TEXT_CONTROL_RE.search(value) or "\r" in value or "\n" in value:
        issues.append(
            _error(
                "CONTROL_CHARACTER_NOT_ALLOWED",
                field_name,
                f"{label} cannot contain line breaks or control characters.",
            )
        )


def _record_duplicate(
    value: str,
    index: int,
    seen: dict[str, int],
    issues: list[RouteValidationIssue],
    code: str,
    field_name: str,
    message: str,
) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    key = value.strip().casefold()
    if key in seen:
        issues.append(
            _error(
                code,
                field_name,
                f"{message} It duplicates item {seen[key] + 1}.",
            )
        )
    else:
        seen[key] = index


def _secret_key_paths(
    value: object, path: str = ""
) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", key_text.casefold())
            nested_path = f"{path}.{key_text}"
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                yield nested_path
            yield from _secret_key_paths(nested, nested_path)
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            yield from _secret_key_paths(nested, f"{path}[{index}]")


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json_value(nested) for key, nested in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(nested) for nested in value)
    return value


def _thaw_json_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json_value(nested) for nested in value]
    return value


def _expect_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RouteProjectFormatError(f"{field_name} must be a JSON object")
    return value


def _expect_json_array(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise RouteProjectFormatError(f"{field_name} must be a JSON array")
    return value


def _required_json_string(
    data: Mapping[str, Any], key: str, field_name: str
) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RouteProjectFormatError(f"{field_name} must be a JSON string")
    return value


def _required_json_integer(
    data: Mapping[str, Any], key: str, field_name: str
) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteProjectFormatError(f"{field_name} must be a JSON integer")
    return value


def _optional_json_string(
    data: Mapping[str, Any], key: str, field_name: str
) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise RouteProjectFormatError(f"{field_name} must be a JSON string")
    return value


def _optional_json_integer(
    data: Mapping[str, Any], key: str, field_name: str
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteProjectFormatError(
            f"{field_name} must be a JSON integer or null"
        )
    return value


def _optional_json_number(
    data: Mapping[str, Any], key: str, field_name: str
) -> float | int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RouteProjectFormatError(
            f"{field_name} must be a JSON number or null"
        )
    return value


def _deduplicate_strings(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _error(
    code: str, field_name: str, message: str, source: str = ""
) -> RouteValidationIssue:
    return RouteValidationIssue("error", code, field_name, message, source)


def _warning(
    code: str, field_name: str, message: str, source: str = ""
) -> RouteValidationIssue:
    return RouteValidationIssue("warning", code, field_name, message, source)


__all__ = [
    "DeploymentReadiness",
    "IRMCounts",
    "OpticalPath",
    "PathEndpointReview",
    "PROPAGATION_DIRECTIONS",
    "PROFILE_IDS",
    "PROFILE_LABELS",
    "PROFILE_REGISTRY",
    "RACK_CAPACITY",
    "REVIEW_STATES",
    "R40_PENDING_SRA_PEER_REVIEW",
    "R40_ROUTE_TOPOLOGY_MISMATCH",
    "R40RouteTopologyIssue",
    "ROUTE_SCHEMA_NAME",
    "ROUTE_SCHEMA_VERSION",
    "PropagationDirection",
    "ReviewState",
    "RouteLink",
    "RoutePropagationView",
    "RouteProject",
    "RouteProjectFormatError",
    "RouteProjectValidationError",
    "RouteValidationIssue",
    "ShelfDeploymentStatus",
    "ShelfInstance",
    "ShelfProfile",
    "Site",
    "load_route_project",
    "load_route_project_draft",
    "route_deployment_readiness",
    "route_link_propagation_views",
    "route_native_fiber_review",
    "route_project_fingerprint",
    "save_route_project",
    "save_route_project_draft",
    "validate_route_project",
]
