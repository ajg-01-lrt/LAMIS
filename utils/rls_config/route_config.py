"""Fail-closed route configuration preparation.

The route builder evaluates every ordered shelf as one transaction.  A route
with an unsupported profile, an unreviewed diagram candidate, a stale or
invalid provider payload, or a generator failure produces no CLI at all.  This
module deliberately has a small static provider registry; provider identifiers
stored in project JSON are descriptive metadata and are never dynamically
imported or executed.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Mapping, Protocol

from .route_project import (
    DeploymentReadiness,
    RouteProject,
    ShelfInstance,
    route_project_fingerprint,
)

LOGGER = logging.getLogger(__name__)


class _CancelEvent(Protocol):
    def is_set(self) -> bool: ...


class RouteConfigError(ValueError):
    """Raised when a complete, current route configuration cannot be built."""


class RouteConfigCancelled(RouteConfigError):
    """Raised when the caller cancels route configuration preparation."""


@dataclass(frozen=True)
class ShelfConfigBuild:
    """One generated shelf artifact and its stable route identity."""

    order: int
    shelf_id: str
    profile_id: str
    tid: str
    artifact: Any


@dataclass(frozen=True)
class RouteConfigBuild:
    """Result of evaluating and, when possible, generating a complete route."""

    project_fingerprint: str
    ready: bool
    readiness: DeploymentReadiness
    shelf_builds: tuple[ShelfConfigBuild, ...] = ()
    blocking_reasons: tuple[str, ...] = ()

    @property
    def config_count(self) -> int:
        return len(self.shelf_builds)


def _cancelled(cancel_event: _CancelEvent | None) -> bool:
    return cancel_event is not None and bool(cancel_event.is_set())


def _build_exact_r4_0(shelf: ShelfInstance) -> Any:
    """Invoke one exact audited RLS R4.0 provider request."""

    from .r4_0_generator import (
        R40ExactConfigGenerator,
        decode_r40_exact_payload,
    )

    request = decode_r40_exact_payload(shelf.profile_payload)
    return R40ExactConfigGenerator().generate(request)


# Never resolve a provider from route-project text.  Adding a provider requires
# a code change, release-specific evidence, validation, and regression tests.
_PROVIDERS: Mapping[str, Callable[[ShelfInstance], Any]] = {
    "add_drop_a": _build_exact_r4_0,
    "add_drop_z": _build_exact_r4_0,
    "add_drop": _build_exact_r4_0,
    "ila": _build_exact_r4_0,
    "roadm_a": _build_exact_r4_0,
    "roadm_z": _build_exact_r4_0,
    "roadm": _build_exact_r4_0,
}


def evaluate_route_configs(
    project: RouteProject,
    *,
    cancel_event: _CancelEvent | None = None,
) -> RouteConfigBuild:
    """Generate all shelf configs in memory or return one blocked result.

    The returned ``shelf_builds`` tuple is empty whenever any shelf is blocked
    or any provider fails.  This prevents a mixed route from being mistaken
    for a complete deployment package.
    """

    if not isinstance(project, RouteProject):
        raise TypeError("project must be a RouteProject")
    fingerprint = route_project_fingerprint(project)
    if _cancelled(cancel_event):
        raise RouteConfigCancelled("Route configuration preparation cancelled.")

    readiness = project.deployment_readiness()
    if not readiness.ready:
        return RouteConfigBuild(
            project_fingerprint=fingerprint,
            ready=False,
            readiness=readiness,
            blocking_reasons=readiness.blocking_reasons,
        )

    generated: list[ShelfConfigBuild] = []
    try:
        for order, shelf in enumerate(project.shelves, start=1):
            if _cancelled(cancel_event):
                raise RouteConfigCancelled(
                    "Route configuration preparation cancelled."
                )
            provider = _PROVIDERS.get(shelf.profile_id)
            if provider is None:
                raise RouteConfigError(
                    f"No authorized provider is registered for "
                    f"{shelf.profile_id!r}."
                )
            artifact = provider(shelf)
            generated.append(
                ShelfConfigBuild(
                    order=order,
                    shelf_id=shelf.shelf_id,
                    profile_id=shelf.profile_id,
                    tid=shelf.tid,
                    artifact=artifact,
                )
            )
    except RouteConfigCancelled:
        raise
    except Exception as exc:
        LOGGER.exception(
            "[RLS ROUTE] Configuration provider failed; all in-memory "
            "route artifacts were discarded."
        )
        # Discard every in-memory artifact so callers cannot publish a partial
        # route if a later shelf fails.
        return RouteConfigBuild(
            project_fingerprint=fingerprint,
            ready=False,
            readiness=readiness,
            blocking_reasons=(
                "A registered configuration provider failed; no partial route "
                "CLI was retained.",
                f"Provider error type: {type(exc).__name__}",
            ),
        )

    if route_project_fingerprint(project) != fingerprint:
        # Frozen RouteProject instances should make this impossible, but the
        # assertion guards custom subclasses/adapters and documents the stale
        # result policy used by the GUI worker.
        return RouteConfigBuild(
            project_fingerprint=fingerprint,
            ready=False,
            readiness=readiness,
            blocking_reasons=(
                "The route changed while configurations were being prepared; "
                "the stale result was discarded.",
            ),
        )

    return RouteConfigBuild(
        project_fingerprint=fingerprint,
        ready=True,
        readiness=readiness,
        shelf_builds=tuple(generated),
    )


def require_complete_route_configs(
    project: RouteProject,
    *,
    cancel_event: _CancelEvent | None = None,
) -> RouteConfigBuild:
    """Return a complete build or raise with safe, non-payload diagnostics."""

    result = evaluate_route_configs(project, cancel_event=cancel_event)
    if not result.ready:
        detail = "; ".join(result.blocking_reasons) or (
            "Route configuration validation did not pass."
        )
        raise RouteConfigError(detail)
    return result


__all__ = [
    "RouteConfigBuild",
    "RouteConfigCancelled",
    "RouteConfigError",
    "ShelfConfigBuild",
    "evaluate_route_configs",
    "require_complete_route_configs",
]
