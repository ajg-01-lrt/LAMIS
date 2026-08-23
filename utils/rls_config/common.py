"""Shared types and constants for the active Ciena RLS software contract.

ATLAS currently supports one executable release: RLS R4.0. Keeping its release
token and the release-neutral validation/OAM value objects here prevents the
exact providers from depending on a retired release generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence


Severity = Literal["error", "warning"]

SUPPORTED_SOFTWARE_RELEASE = "RLS R4.0"
# The supplied R4.0.0 upgrade procedures identify the baseline target as
# ``Rel. 4.00.00``.  This is an editable planning default, not an observation
# of the software actually installed on a customer shelf.
DEFAULT_R40_TARGET_BUILD_SCHEMA = "4.00.00"

ROUTING_OSPF_GNE = "OSPF_GNE"
ROUTING_STATIC_COLAN_OSPF_OSC = "STATIC_COLAN_OSPF_OSC"
ROUTING_OSPF_OSC_ONLY = "OSPF_OSC_ONLY"
ROUTING_MODES = {
    ROUTING_OSPF_GNE,
    ROUTING_STATIC_COLAN_OSPF_OSC,
    ROUTING_OSPF_OSC_ONLY,
}
ROUTING_LABELS = {
    ROUTING_OSPF_GNE: "OSPF GNE (COLAN + loopback + OSC)",
    ROUTING_STATIC_COLAN_OSPF_OSC: "Static COLAN + OSPF loopback/OSC",
    ROUTING_OSPF_OSC_ONLY: "No DCN COLAN (OSPF loopback/OSC only)",
}

# Native RLS fiber-type values accepted by the audited exact providers.
# Plain LEAF is retained because R4.0 commissioning printed p.199 and the
# audited legacy workbook both emit it; the generator reports the conflicting
# formal pp.121-122 enum table as a validation warning.
FIBER_TYPES = (
    "AllWave",
    "DSF",
    "Enhanced LEAF",
    "LEAF",
    "EX2000",
    "EX3000",
    "FREE",
    "Lambda Shifted",
    "MetroCor",
    "NDSF",
    "NDSF LWP",
    "PSC",
    "SMF28ULL",
    "TeraLight",
    "TeraWave ULL",
    "TrueWave Classic",
    "TrueWave Plus",
    "TrueWave Reach",
    "TrueWave RS",
)


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    field: str
    message: str
    source: str = ""


class ConfigValidationError(ValueError):
    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(
            "; ".join(f"{issue.field}: {issue.message}" for issue in self.issues)
        )


@dataclass(frozen=True)
class ManagementInterface:
    """Direct-DCN COLAN and routing settings for an exact RLS provider."""

    enabled: bool = True
    name: str = "colan-x"
    routing_mode: str = ROUTING_OSPF_GNE
    ip_address: str = ""
    prefix_length: int | None = None
    gateway: str = ""
    ospf_metric: int = 10
    static_metric: int = 1500


__all__ = [
    "ConfigValidationError",
    "DEFAULT_R40_TARGET_BUILD_SCHEMA",
    "FIBER_TYPES",
    "ManagementInterface",
    "ROUTING_LABELS",
    "ROUTING_MODES",
    "ROUTING_OSPF_GNE",
    "ROUTING_OSPF_OSC_ONLY",
    "ROUTING_STATIC_COLAN_OSPF_OSC",
    "SUPPORTED_SOFTWARE_RELEASE",
    "ValidationIssue",
]
