"""Exact, fail-closed Ciena RLS R4.0 configuration providers.

The broad words ``Add/Drop``, ``ROADM``, and ``ILA`` do not select a physical
configuration.  This module exposes only layouts for which the supplied R4.0
manuals establish the chassis, circuit packs, slots, ports, PFG roles, and
local connectivity.  It never imports the quarantined legacy workbook
adapter.

The generated CLI is a documented *pre-calibration candidate*.  Ciena's own
commissioning manual warns that example scripts can contain errors.  ATLAS
therefore corrects only defects that are independently resolved by the
equipment/topology tables, emits dependency-sized batch transactions, and
requires a successful ``validate`` result on the matching blank R4.0 shelf
before any operator proceeds to ``commit``.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import ipaddress
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence

from .common import (
    ConfigValidationError,
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    FIBER_TYPES,
    ManagementInterface,
    ROUTING_OSPF_GNE,
    ROUTING_OSPF_OSC_ONLY,
    ROUTING_STATIC_COLAN_OSPF_OSC,
    ROUTING_MODES,
    SUPPORTED_SOFTWARE_RELEASE,
    ValidationIssue,
)


Severity = Literal["error", "warning"]
R40ProviderCandidateStatus = Literal[
    "exact_match",
    "unique_candidate",
    "ambiguous",
    "conflict",
    "unsupported_sra",
]

SUPPORTED_RELEASE = SUPPORTED_SOFTWARE_RELEASE
GENERATOR_VERSION = "1.6.0"
READINESS_STATE = "documented_pre_calibration_candidate"
READINESS_LABEL = "Documented R4.0 pre-calibration candidate"

R40_PAYLOAD_SCHEMA_ID = "ciena.rls.r4-0-exact-request"
R40_PAYLOAD_SCHEMA_VERSION = "1.4"
R40_ROUTE_SIDES = ("A", "Z")
COLAN_TERMINAL_REQUIRED = "terminal_required"
COLAN_TERMINAL_OPTIONAL = "terminal_optional"
COLAN_PROHIBITED = "prohibited"

R40_CDA_RLA12_C_2DEG_NO_SRA = "r40_cda_rla12_c_2deg_no_sra"
R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA = (
    "r40_cdc_roadm_rla32_c_2deg_ccmd8x24_no_sra"
)
R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA = (
    "r40_cl_roadm_rla12_lru12_1deg_no_sra"
)
R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6 = (
    "r40_cl_roadm_rla12_lru12_1deg_sra6"
)
R40_R2_CL_DLE_S1_NO_SRA = (
    "r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_"
    "single_no_sra_ospfv2_rne_v1"
)
R40_R2_CL_DLE_S1_SRA4 = (
    "r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_"
    "single_sra4_ospfv2_rne_v1"
)

R4_CHASSIS_PEC = "NTK803FA"
R2_CHASSIS_PEC = "NTK803DA"
OSC_C_PEC = "NTK591VN"
OSC_CL_PEC = "NTK591VQ"

SOURCE_REFERENCES = (
    "323-2051-190 RLS R4.0 CLI Reference, printed pp.11-13,17-19,27-34",
    "323-2051-101 RLS R4.0 OAM Communications, printed "
    "pp.40,116,168-169,224-232",
    "323-2051-201 RLS R4.0 Installation Guide, printed p.211",
    "323-2051-220 RLS R4.0 ZTP/Manual Commissioning, printed pp.40-42, "
    "101-121,129-130,171-179,189-212,279-289,442-443,447-448",
    "323-2051-300 RLS R4.0 Licensing/Security/Admin, printed pp.244-246,280-281",
    "323-2051-310 RLS R4.0 Equipment/Facility/Protection, printed pp.56,71-72",
    "323-2051-318 RLS R4.0 Optical/SPLI/OTDR, printed pp.185-187,222-235",
    "NTRN10WA RLS R4.0 Planning Guide, printed pp.182,558,576,582, "
    "620-623,630-638,644,658,662,665-666",
)

CONTROLLED_SOURCE_HASHES = MappingProxyType(
    {
        "323-2051-101_(RLS_R4.0_OAM_Communications)_Issue2.pdf": (
            "39329bfa0dbb1456c5cb34e0df44e9bfa1667e0e7f7e603f4156a2d111414b0d"
        ),
        "323-2051-190_(RLS_R4.0_CLI_Reference)_Issue1.pdf": (
            "c92ae907d865c29ef4668c6c9145942c814bcbc4156ef09881ccc57b26f4d29a"
        ),
        "323-2051-201_(RLS_R4.0_Installation_Guide)_Issue1.pdf": (
            "e5bf751e07fd5e4f55f60c9e17c99c66e2317c2fdf64b2e0e108f3ef6d69ac55"
        ),
        "323-2051-220_(RLS_R4.0_ZTP_Manual_Commissioning)_Issue3.pdf": (
            "1cbc000482fc7ad22351eda72ea7a43e44361644d2c4776fd5a24bcdf32727d9"
        ),
        "323-2051-310_(RLS_R4.0_Equip_Facility_Protect)_Issue4.pdf": (
            "2a25450db04617747c7a3f8b4f653707365bbf775db95863ac1c804da00d81a4"
        ),
        "323-2051-300_(RLS_R4.0_Licensing_Security_Admin)_Issue2.pdf": (
            "4904423a7be3671820a3f07f9731cd3773cf70da8c4d16227c0c6e9a793e54bb"
        ),
        "323-2051-318_(RLS_R4.0_Optical_SPLI_OTDR)_Issue2.pdf": (
            "74c0d03439cbfe447199486d7aa11c2ea7607d96e40a9178329c08d1aef547a7"
        ),
        "NTRN10WA_(RLS_R4.0_Planning_Guide)_Issue3.pdf": (
            "6e4b40839efbe7eeb7f320d614ce3187a2fea01306a692fe967f20fa3cd48c25"
        ),
    }
)

_SHELF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
_MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DNS_LABEL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_RLS_INVALID_IPV4_NETWORKS = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("192.0.0.0/24"),
)


@dataclass(frozen=True)
class R40ProviderProfile:
    provider_id: str
    display_name: str
    role_profiles: frozenset[str]
    chassis_family: str
    chassis_pec: str
    hardware_profile: str
    application: str
    colan_policy: Literal[
        "terminal_required",
        "terminal_optional",
        "prohibited",
    ]
    equipment: tuple[tuple[int, str], ...]
    osc_modules: tuple[tuple[int, int, str], ...]
    line_outputs: tuple[tuple[int, int], ...]
    line_inputs: tuple[tuple[int, int], ...]
    line_pfg_names: tuple[tuple[str, str], ...]
    line_link_names: tuple[str, ...]
    line_semantics: Literal[
        "bidirectional_degree",
        "unidirectional_amplifier_path",
    ]
    supports_raman: bool
    bom_note: str
    evidence: str
    optical_band: str = ""
    topology: str = ""
    add_drop_structure: str = ""
    protection_type: str = ""


@dataclass(frozen=True)
class R40DeploymentControl:
    """One provider-scoped control carried with every offline candidate.

    These controls make required field/on-box actions visible without claiming
    that ATLAS can observe or verify a physical shelf from an offline request.
    The legacy Boolean field is retained only so schema 1.4 payloads continue
    to round-trip.
    """

    control_id: str
    legacy_field: str
    label: str
    applicability: str
    stage: str
    instruction: str
    source: str


_PROVIDER_PROFILES = (
    R40ProviderProfile(
        provider_id=R40_CDA_RLA12_C_2DEG_NO_SRA,
        display_name="C-band CDA Add/Drop — R4, RLA12 2-degree, no SRA",
        role_profiles=frozenset({"add_drop_a", "add_drop_z", "add_drop"}),
        chassis_family="R4",
        chassis_pec=R4_CHASSIS_PEC,
        hardware_profile="R4_RLA12_C_2DEG_CCMD16_C_NO_SRA",
        application="cda_rla12_c",
        colan_policy=COLAN_TERMINAL_OPTIONAL,
        equipment=((1, "NTK852BA"), (3, "NTK852BA"), (5, "NTK834AA")),
        osc_modules=((1, 50, OSC_C_PEC), (3, 50, OSC_C_PEC)),
        line_outputs=((1, 53), (3, 53)),
        line_inputs=((1, 54), (3, 54)),
        line_pfg_names=(
            ("LINE-MUX-PFG-1", "LINE-DEMUX-PFG-1"),
            ("LINE-MUX-PFG-2", "LINE-DEMUX-PFG-2"),
        ),
        line_link_names=("LM1-LINEOUT", "LM2-LINEOUT"),
        line_semantics="bidirectional_degree",
        supports_raman=False,
        bom_note=(
            "RLA12-C anchors in slots 1 and 3 are double-wide; CCMD16-C is "
            "fixed in slot 5. No SRA, remote CCMD, R8 substitution, or "
            "protection is permitted."
        ),
        evidence="323-2051-220 printed pp.279-284; NTRN10WA printed pp.582,658",
        optical_band="c",
        topology="roadm_cda",
        add_drop_structure="cda",
        protection_type="none",
    ),
    R40ProviderProfile(
        provider_id=R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA,
        display_name="C-band CDC ROADM — R4, RLA32 2-degree, no SRA",
        role_profiles=frozenset({"roadm_a", "roadm_z", "roadm"}),
        chassis_family="R4",
        chassis_pec=R4_CHASSIS_PEC,
        hardware_profile="R4_RLA32_C_2DEG_CCMD8X24_C_CFIM1_CFIM2_NO_SRA",
        application="cdc_roadm_rla32_c",
        colan_policy=COLAN_TERMINAL_OPTIONAL,
        equipment=(
            (1, "NTK852AA"),
            (3, "NTK852AA"),
            (5, "NTK843BA"),
            (71, "NTK504QA"),
            (72, "NTK504QB"),
        ),
        osc_modules=((1, 50, OSC_C_PEC), (3, 50, OSC_C_PEC)),
        line_outputs=((1, 53), (3, 53)),
        line_inputs=((1, 54), (3, 54)),
        line_pfg_names=(
            ("LINE-MUX-PFG-1", "LINE-DEMUX-PFG-1"),
            ("LINE-MUX-PFG-2", "LINE-DEMUX-PFG-2"),
        ),
        line_link_names=("LM1-LINEOUT", "LM2-LINEOUT"),
        line_semantics="bidirectional_degree",
        supports_raman=False,
        bom_note=(
            "RLA32-C anchors 1/3 are double-wide; CCMD8x24-C is provisioned "
            "at logical slot 5 and is one slot wide by two slots high. Its "
            "height does not consume logical slot 6. CFIM1/CFIM2 in external "
            "slots 71/72 require the separately ordered NTK504NA OMC2 "
            "carrier. No SRA or protection."
        ),
        evidence=(
            "323-2051-220 printed pp.129-130,171-179,204-212,244,250; "
            "NTRN10WA printed pp.558,644,662,665-666"
        ),
        optical_band="c",
        topology="roadm_cdc",
        add_drop_structure="cdc",
        protection_type="none",
    ),
    R40ProviderProfile(
        provider_id=R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA,
        display_name=(
            "C+L ROADM terminal core — R4, RLA12/LRU12 1-degree, no SRA"
        ),
        role_profiles=frozenset({"roadm_a", "roadm_z", "roadm"}),
        chassis_family="R4",
        chassis_pec=R4_CHASSIS_PEC,
        hardware_profile="R4_RLA12_CL_LRU12_1DEG_CORE_NO_SRA",
        application="roadm_rla12_cl_lru12_core",
        colan_policy=COLAN_TERMINAL_OPTIONAL,
        equipment=((1, "NTK852BC"), (3, "NTK852NA")),
        osc_modules=((1, 50, OSC_CL_PEC),),
        line_outputs=((1, 53),),
        line_inputs=((1, 54),),
        line_pfg_names=(("LM1", "LD1"),),
        line_link_names=("LM1-LINEOUT",),
        line_semantics="bidirectional_degree",
        supports_raman=False,
        bom_note=(
            "RLA12 C+L NTK852BC is anchored in slot 1 (occupying 1-2) with "
            "NTK591VQ OSC at 1/50; LRU12 NTK852NA is anchored in slot 3 "
            "(occupying 3-4). This provider covers "
            "one bidirectional line degree and the documented RLA/LRU core "
            "links only. CCMD/client modules, express WSS peer links, "
            "Raman/SRA, and protection are outside this provider and must "
            "not be inferred."
        ),
        evidence=(
            "323-2051-220 printed pp.285-289,442-443; "
            "NTRN10WA printed pp.182,576,620-623; "
            "Ciena RLS C+L CLI Config v3.5LR.xlsx "
            "ROADM_A/ROADM_Z B159,B162:B164"
        ),
        optical_band="c+l",
        topology="roadm_rla12_cl_core",
        add_drop_structure="none",
        protection_type="none",
    ),
    R40ProviderProfile(
        provider_id=R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
        display_name=(
            "C+L ROADM terminal core — R4, RLA12/LRU12 1-degree, "
            "slot-6 SRA"
        ),
        role_profiles=frozenset({"roadm_a", "roadm_z", "roadm"}),
        chassis_family="R4",
        chassis_pec=R4_CHASSIS_PEC,
        hardware_profile="R4_RLA12_CL_LRU12_1DEG_CORE_SRA6",
        application="roadm_rla12_cl_lru12_core",
        colan_policy=COLAN_TERMINAL_OPTIONAL,
        equipment=(
            (1, "NTK852BC"),
            (3, "NTK852NA"),
            (6, "NTK830AC"),
        ),
        osc_modules=((1, 50, OSC_CL_PEC),),
        line_outputs=((6, 5),),
        line_inputs=((6, 6),),
        line_pfg_names=(("LM1", "LD1"),),
        line_link_names=("LM1-LINEOUT",),
        line_semantics="bidirectional_degree",
        supports_raman=True,
        bom_note=(
            "RLA12 C+L NTK852BC is anchored in slot 1 (occupying 1-2), "
            "NTK591VQ OSC is at 1/50, LRU12 NTK852NA is anchored in slot 3 "
            "(occupying 3-4), and C+L SRA NTK830AC is fixed in slot 6. "
            "The one bidirectional line degree exits and enters through SRA "
            "ports 6/5 and 6/6. The RLA/SRA express links and existing "
            "RLA/LRU core links are fixed. Protection and unobserved "
            "CCMD/client/add-drop hardware are outside this provider."
        ),
        evidence=(
            "323-2051-220 printed pp.40-42,285-289,435,442-443,"
            "461,464-466,504; NTRN10WA printed pp.159,576,620-623,"
            "630,634-636; confirmed USSAT4-L8R3 installed inventory; "
            "A13 Ciena RLS Field Configs ELP1-SAT4 v1.xlsx "
            "USSAT4-L8R3 B102:B104,B121,B134,B139,B159:B161"
        ),
        optical_band="c+l",
        topology="roadm_rla12_cl_core",
        add_drop_structure="none",
        protection_type="none",
    ),
    R40ProviderProfile(
        provider_id=R40_R2_CL_DLE_S1_NO_SRA,
        display_name="C+L DLE ILA — R2 600 mm, slot 1, no SRA",
        role_profiles=frozenset({"ila"}),
        chassis_family="R2",
        chassis_pec=R2_CHASSIS_PEC,
        hardware_profile="R2_DLE_CL_NTK850DC_SLOT1_NO_SRA",
        application="ila_dle_cl",
        colan_policy=COLAN_PROHIBITED,
        equipment=((1, "NTK850DC"),),
        osc_modules=((1, 50, OSC_CL_PEC), (1, 60, OSC_CL_PEC)),
        line_outputs=((1, 63), (1, 53)),
        line_inputs=((1, 54), (1, 64)),
        line_pfg_names=(
            ("PFG-1-to-2", "PFG-2-to-1"),
            ("PFG-2-to-1", "PFG-1-to-2"),
        ),
        line_link_names=(
            "PFG-1-2-LINEOUT",
            "PFG-2-1-LINEOUT",
        ),
        line_semantics="unidirectional_amplifier_path",
        supports_raman=False,
        bom_note=(
            "NTK850DC starts in slot 1 and occupies slots 1-2. Both OSC "
            "subslots are fixed; no SRA, dual rail, cascade, DLA substitution, "
            "or protected-ROADM use is permitted."
        ),
        evidence=(
            "323-2051-220 printed pp.101-103,447-448; "
            "323-2051-318 printed pp.222-223"
        ),
        optical_band="c+l",
        topology="ila_single",
        add_drop_structure="none",
        protection_type="none",
    ),
    R40ProviderProfile(
        provider_id=R40_R2_CL_DLE_S1_SRA4,
        display_name=(
            "C+L DLE ILA — R2 600 mm, slot 1, slot-4 SRA"
        ),
        role_profiles=frozenset({"ila"}),
        chassis_family="R2",
        chassis_pec=R2_CHASSIS_PEC,
        hardware_profile="R2_DLE_CL_NTK850DC_SLOT1_SRA4",
        application="ila_dle_cl",
        colan_policy=COLAN_PROHIBITED,
        equipment=((1, "NTK850DC"), (4, "NTK830AC")),
        osc_modules=((1, 50, OSC_CL_PEC), (1, 60, OSC_CL_PEC)),
        line_outputs=((4, 5), (1, 53)),
        line_inputs=((1, 54), (4, 6)),
        line_pfg_names=(
            ("PFG-1-to-2", "PFG-2-to-1"),
            ("PFG-2-to-1", "PFG-1-to-2"),
        ),
        line_link_names=(
            "PFG-1-2-LINEOUT",
            "PFG-2-1-LINEOUT",
        ),
        line_semantics="unidirectional_amplifier_path",
        supports_raman=True,
        bom_note=(
            "C+L DLE NTK850DC is anchored in slot 1 (occupying 1-2), "
            "NTK591VQ OSCs are fixed at 1/50 and 1/60, and C+L SRA "
            "NTK830AC is fixed in slot 4 on the PFG-1-to-2 line-output / "
            "PFG-2-to-1 line-input side. The SRA external endpoints are "
            "4/5 and 4/6 and the DLE/SRA express links are fixed. Dual rail, "
            "cascade, DLA substitution, protection, and a second SRA are "
            "outside this provider."
        ),
        evidence=(
            "323-2051-220 printed pp.40-42,101-103,366-370,447-448,"
            "461,464-466,504; 323-2051-318 printed pp.222-223; "
            "NTRN10WA printed pp.159,630,634-636; "
            "A13 Ciena RLS Field Configs ELP1-SAT4 v1.xlsx "
            "USXGN1-L8I2 B73:B75,B101,B121:B122,B138:B143"
        ),
        optical_band="c+l",
        topology="ila_single",
        add_drop_structure="none",
        protection_type="none",
    ),
)

R40_PROVIDER_CATALOG: Mapping[str, R40ProviderProfile] = MappingProxyType(
    {profile.provider_id: profile for profile in _PROVIDER_PROFILES}
)

_COMMON_DEPLOYMENT_CONTROLS = (
    R40DeploymentControl(
        control_id="installed_inventory_matches_provider",
        legacy_field="installed_inventory_confirmed",
        label="Installed inventory and topology match the exact provider",
        applicability="all_exact_r40_providers",
        stage="before_apply",
        instruction=(
            "Verify the chassis, required OMC2 carrier, occupied slots, module "
            "PECs, provider degree/path count, fixed port map, and physical "
            "direction assignment against the installed shelf."
        ),
        source=(
            "323-2051-220 RLS R4.0 printed pp.101-121,171-179,189-212,"
            "279-289,442-443,447-448"
        ),
    ),
    R40DeploymentControl(
        control_id="approved_runtime_engineering_available",
        legacy_field="planner_runtime_mop_confirmed",
        label="Approved runtime-tuning engineering is available when needed",
        applicability="all_exact_r40_providers",
        stage="before_runtime_tuning",
        instruction=(
            "Use an approved EDP, IDP, PlannerPlus output, or customer-approved "
            "equivalent for runtime tuning and calibration. The pre-calibration "
            "candidate intentionally omits those runtime and licensed-feature "
            "actions."
        ),
        source=(
            "323-2051-220 RLS R4.0 printed pp.96,119,498-500; "
            "NTRN10WA RLS R4.0 Planning Guide"
        ),
    ),
    R40DeploymentControl(
        control_id="target_shelf_matches_recorded_build",
        legacy_field="target_build_confirmed",
        label="Target shelf accepts the recorded R4.0 build/schema candidate",
        applicability="all_exact_r40_providers",
        stage="before_apply",
        instruction=(
            f"ATLAS defaults target_software_build to the documented "
            f"{DEFAULT_R40_TARGET_BUILD_SCHEMA} R4.0.0 baseline when the "
            "installed build is unavailable. Compare it with the running "
            "shelf when possible, always run on-box validate, and stop if the "
            "target differs or rejects any command."
        ),
        source=(
            "NTRN38WA.1/.2 RLS R4.0.0 Software Upgrade Procedure; "
            "on-box schema validation gate"
        ),
    ),
)

_SRA_DEPLOYMENT_CONTROLS = (
    R40DeploymentControl(
        control_id="sra_span_bookended_and_colocated",
        legacy_field="installed_inventory_confirmed",
        label="SRA span is bookended and each SRA is collocated with its amplifier",
        applicability="all_exact_r40_sra_providers",
        stage="before_apply",
        instruction=(
            "Verify the reviewed optical span is bookended by compatible SRAs "
            "and each SRA is in the same shelf as the RLA, DLE, or DLA serving "
            "that span. Stop if either endpoint, PEC, slot, or internal "
            "express-link map differs."
        ),
        source=(
            "NTRN10WA RLS R4.0 printed p.159; "
            "323-2051-220 RLS R4.0 printed pp.461,464-466"
        ),
    ),
    R40DeploymentControl(
        control_id="sra_runtime_engineering_approved",
        legacy_field="planner_runtime_mop_confirmed",
        label="Approved SRA runtime engineering is available",
        applicability="all_exact_r40_sra_providers",
        stage="before_raman_enable",
        instruction=(
            "Use approved fiber-specific RAMAN engineering for runtime gain, "
            "safety, mixed-fiber, activation, and calibration steps. ATLAS "
            "does not infer those values from route distance or span loss and "
            "does not emit runtime enable or calibration commands."
        ),
        source=(
            "323-2051-310 RLS R4.0 printed pp.144-149; "
            "323-2051-220 RLS R4.0 printed pp.133,498-504"
        ),
    ),
    R40DeploymentControl(
        control_id="sra_go_no_go_and_alarms_pass",
        legacy_field="planner_runtime_mop_confirmed",
        label="SRA OTDR go/no-go and activation-alarm gate passes",
        applicability="all_exact_r40_sra_providers",
        stage="before_raman_enable",
        instruction=(
            "Run the vendor SRA go/no-go OTDR procedure and confirm RAMAN "
            "activation-inhibited alarms are clear before enabling the SRA "
            "circuit pack or RAMAN facility or accepting calibration."
        ),
        source="323-2051-220 RLS R4.0 printed p.504",
    ),
)

_APPLICATION_DEPLOYMENT_CONTROLS: Mapping[
    str, tuple[R40DeploymentControl, ...]
] = MappingProxyType(
    {
        "ila_dle_cl": (
            R40DeploymentControl(
                control_id="dle_line_fibers_disconnected",
                legacy_field="greenfield_fibers_disconnected_confirmed",
                label="DLE line fibers remain disconnected while staging",
                applicability="ila_dle_cl",
                stage="before_apply",
                instruction=(
                    "Stage the greenfield DLE PFGs and disabled SCO state with "
                    "the external line fibers disconnected."
                ),
                source=(
                    "323-2051-220 RLS R4.0 printed pp.447-448; "
                    "323-2051-318 RLS R4.0 printed p.123"
                ),
            ),
            R40DeploymentControl(
                control_id="dle_calibration_features_inactive",
                legacy_field="calibration_feature_inactive_confirmed",
                label="DLE automatic calibration features remain inactive",
                applicability="ila_dle_cl",
                stage="before_apply",
                instruction=(
                    "Keep Span Calibration and Passive Terminal Control "
                    "inactive while creating and validating the DLE PFGs."
                ),
                source=(
                    "323-2051-220 RLS R4.0 printed p.357; "
                    "323-2051-318 RLS R4.0 printed p.123"
                ),
            ),
        ),
        "cdc_roadm_rla32_c": (
            R40DeploymentControl(
                control_id="cfim_unused_ports_vendor_treated",
                legacy_field="cfim_unused_ports_terminated_confirmed",
                label="Unused CFIM ports have the vendor-required treatment",
                applicability="cdc_roadm_rla32_c",
                stage="before_apply",
                instruction=(
                    "Verify unused CFIM ports have the required loopback or "
                    "dust-cap treatment and match the installed packout."
                ),
                source=(
                    "323-2051-201 RLS R4.0 printed p.211; "
                    "323-2051-220 RLS R4.0 printed pp.171-179"
                ),
            ),
        ),
    }
)


def deployment_controls_for_profile(
    profile: R40ProviderProfile,
) -> tuple[R40DeploymentControl, ...]:
    """Return automatic advisory controls applicable to an exact profile."""

    if not isinstance(profile, R40ProviderProfile):
        raise TypeError("profile must be an R40ProviderProfile")
    return (
        *_COMMON_DEPLOYMENT_CONTROLS,
        *_APPLICATION_DEPLOYMENT_CONTROLS.get(profile.application, ()),
        *(_SRA_DEPLOYMENT_CONTROLS if profile.supports_raman else ()),
    )


def r40_deployment_controls(
    provider_id: str,
) -> tuple[R40DeploymentControl, ...]:
    """Return automatic advisory controls applicable to one provider ID."""

    profile = R40_PROVIDER_CATALOG.get(provider_id)
    return () if profile is None else deployment_controls_for_profile(profile)


def provider_profiles_for_role(role_profile: str) -> tuple[R40ProviderProfile, ...]:
    """Return exact providers compatible with one route-planning role."""

    return tuple(
        profile
        for profile in _PROVIDER_PROFILES
        if role_profile in profile.role_profiles
    )


@dataclass(frozen=True)
class R40DirectModuleFact:
    """One directly observed module fact used only for provider resolution.

    Callers must not construct this value from a role, chassis label, workbook
    default, or provider profile. A missing ``subslot`` denotes a top-level
    circuit pack; a populated ``subslot`` denotes a nested OSC circuit pack.
    """

    slot: int
    pec: str
    subslot: int | None = None


@dataclass(frozen=True)
class R40ProviderCandidateFacts:
    """Non-executable facts that may narrow the exact R4.0 provider catalog."""

    role_profile: str
    software_release: str = ""
    chassis_family: str = ""
    chassis_pec: str = ""
    optical_band: str = ""
    topology: str = ""
    add_drop_structure: str = ""
    protection_type: str = ""
    module_inventory: tuple[R40DirectModuleFact, ...] = ()
    sra_state: Literal["unknown", "absent", "present"] = "unknown"


@dataclass(frozen=True)
class R40ProviderCandidateResolution:
    """Fail-closed advisory result that can never authorize configuration."""

    status: R40ProviderCandidateStatus
    provider_id: str | None
    compatible_provider_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    matched_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    deployable_cli: bool = False


_R40_CANDIDATE_REQUIRED_EXACT_FIELDS = (
    "software_release",
    "chassis_family",
    "chassis_pec",
    "optical_band",
    "topology",
    "add_drop_structure",
    "protection_type",
    "module_inventory",
    "sra_state",
)


def _candidate_resolution(
    status: R40ProviderCandidateStatus,
    *,
    provider_id: str | None = None,
    compatible_provider_ids: Iterable[str] = (),
    reason_codes: Iterable[str],
    reasons: Iterable[str],
    matched_fields: Iterable[str] = (),
    missing_fields: Iterable[str] = (),
) -> R40ProviderCandidateResolution:
    """Construct one consistently immutable, non-executable result."""

    return R40ProviderCandidateResolution(
        status=status,
        provider_id=provider_id,
        compatible_provider_ids=tuple(compatible_provider_ids),
        reason_codes=tuple(reason_codes),
        reasons=tuple(reasons),
        matched_fields=tuple(matched_fields),
        missing_fields=tuple(missing_fields),
        deployable_cli=False,
    )


def _candidate_chassis_family(value: str) -> str:
    token = re.sub(r"\s+", " ", value.strip().upper())
    match = re.fullmatch(r"(R2|R4)(?:\s+600\s*MM)?", token)
    return match.group(1) if match is not None else token


def _candidate_enum(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().casefold())


def _candidate_band(value: str) -> str:
    token = re.sub(r"\s+", "", value.strip().casefold())
    return {
        "c": "c",
        "c-band": "c",
        "cband": "c",
        "c+l": "c+l",
        "c+l-band": "c+l",
        "c+lband": "c+l",
    }.get(token, token)


def _provider_module_inventory(
    profile: R40ProviderProfile,
) -> frozenset[tuple[int, int | None, str]]:
    return frozenset(
        (
            *(
                (slot, None, pec.upper())
                for slot, pec in profile.equipment
            ),
            *(
                (slot, subslot, pec.upper())
                for slot, subslot, pec in profile.osc_modules
            ),
        )
    )


def _direct_module_inventory(
    values: Sequence[R40DirectModuleFact],
) -> tuple[
    frozenset[tuple[int, int | None, str]],
    tuple[str, ...],
]:
    normalized: list[tuple[int, int | None, str]] = []
    problems: list[str] = []
    for index, fact in enumerate(values):
        if not isinstance(fact, R40DirectModuleFact):
            problems.append(
                f"module_inventory[{index}] is not an R40DirectModuleFact."
            )
            continue
        fact_problems: list[str] = []
        if isinstance(fact.slot, bool) or not isinstance(fact.slot, int) or (
            fact.slot <= 0
        ):
            fact_problems.append(
                f"module_inventory[{index}].slot must be a positive integer."
            )
        if (
            fact.subslot is not None
            and (
                isinstance(fact.subslot, bool)
                or not isinstance(fact.subslot, int)
                or fact.subslot <= 0
            )
        ):
            fact_problems.append(
                f"module_inventory[{index}].subslot must be a positive "
                "integer or null."
            )
        pec = fact.pec.strip().upper() if isinstance(fact.pec, str) else ""
        if re.fullmatch(r"[A-Z0-9]{3,32}", pec) is None:
            fact_problems.append(
                f"module_inventory[{index}].pec is not a valid PEC token."
            )
        problems.extend(fact_problems)
        if not fact_problems:
            normalized.append((fact.slot, fact.subslot, pec))
    if len(set(normalized)) != len(normalized):
        problems.append("module_inventory contains duplicate direct facts.")
    return (frozenset(normalized), tuple(problems))


def resolve_r40_provider_candidate(
    facts: R40ProviderCandidateFacts,
    *,
    catalog: Mapping[str, R40ProviderProfile] = R40_PROVIDER_CATALOG,
) -> R40ProviderCandidateResolution:
    """Resolve a non-executable provider candidate from reviewed direct facts.

    The resolver never constructs an :class:`R40ExactRequest`, payload, CLI
    command, or deployment authorization. Partial compatible facts can identify
    a unique review candidate, while ``exact_match`` requires every fixed
    discriminator plus the complete directly observed module inventory and an
    explicit no-SRA observation. Any supplied fact is a constraint: conflicting
    chassis, band, topology, protection, or module data eliminates a provider.
    """

    if not isinstance(facts, R40ProviderCandidateFacts):
        raise TypeError("facts must be an R40ProviderCandidateFacts value")
    if not isinstance(catalog, Mapping):
        raise TypeError("catalog must be a provider-profile mapping")
    text_fields = (
        "role_profile",
        "software_release",
        "chassis_family",
        "chassis_pec",
        "optical_band",
        "topology",
        "add_drop_structure",
        "protection_type",
        "sra_state",
    )
    invalid_text_fields = tuple(
        field_name
        for field_name in text_fields
        if not isinstance(getattr(facts, field_name), str)
    )
    if invalid_text_fields:
        return _candidate_resolution(
            "conflict",
            reason_codes=("INVALID_CANDIDATE_FACT_TYPES",),
            reasons=tuple(
                f"{field_name} must be text."
                for field_name in invalid_text_fields
            ),
        )
    if (
        isinstance(facts.module_inventory, (str, bytes))
        or not isinstance(facts.module_inventory, Sequence)
    ):
        return _candidate_resolution(
            "conflict",
            reason_codes=("INVALID_DIRECT_MODULE_FACTS",),
            reasons=("module_inventory must be a sequence of direct facts.",),
        )

    module_inventory, module_problems = _direct_module_inventory(
        facts.module_inventory
    )
    if module_problems:
        return _candidate_resolution(
            "conflict",
            reason_codes=("INVALID_DIRECT_MODULE_FACTS",),
            reasons=module_problems,
        )
    if facts.sra_state not in {"unknown", "absent", "present"}:
        return _candidate_resolution(
            "conflict",
            reason_codes=("INVALID_SRA_STATE",),
            reasons=(
                "SRA state must be exactly unknown, absent, or present.",
            ),
        )

    role = facts.role_profile.strip()
    role_profiles = tuple(
        profile
        for profile in catalog.values()
        if role in profile.role_profiles
    )
    if not role_profiles:
        return _candidate_resolution(
            "conflict",
            reason_codes=("UNSUPPORTED_ROLE",),
            reasons=(
                "No registered exact R4.0 provider supports the reviewed "
                "route role.",
            ),
        )

    if facts.sra_state == "present":
        sra_profiles = tuple(
            profile for profile in role_profiles if profile.supports_raman
        )
        if not sra_profiles:
            return _candidate_resolution(
                "unsupported_sra",
                compatible_provider_ids=(),
                reason_codes=("R40_SRA_PROVIDER_UNAVAILABLE",),
                reasons=(
                    "The reviewed facts establish SRA/RAMAN hardware, but "
                    "every role-compatible registered R4.0 provider is "
                    "explicitly no-SRA.",
                ),
            )
        role_profiles = sra_profiles

    supplied_release = facts.software_release.strip()
    if supplied_release and supplied_release != SUPPORTED_RELEASE:
        return _candidate_resolution(
            "conflict",
            reason_codes=("SOFTWARE_RELEASE_MISMATCH",),
            reasons=(
                f"Exact provider resolution requires {SUPPORTED_RELEASE}.",
            ),
        )

    supplied_values = {
        "chassis_family": (
            _candidate_chassis_family(facts.chassis_family)
            if facts.chassis_family.strip()
            else ""
        ),
        "chassis_pec": facts.chassis_pec.strip().upper(),
        "optical_band": (
            _candidate_band(facts.optical_band)
            if facts.optical_band.strip()
            else ""
        ),
        "topology": (
            _candidate_enum(facts.topology)
            if facts.topology.strip()
            else ""
        ),
        "add_drop_structure": (
            _candidate_enum(facts.add_drop_structure)
            if facts.add_drop_structure.strip()
            else ""
        ),
        "protection_type": (
            _candidate_enum(facts.protection_type)
            if facts.protection_type.strip()
            else ""
        ),
    }
    compatible: list[R40ProviderProfile] = []
    rejected_fields: set[str] = set()
    for profile in role_profiles:
        expected_values = {
            "chassis_family": profile.chassis_family,
            "chassis_pec": profile.chassis_pec.upper(),
            "optical_band": profile.optical_band,
            "topology": profile.topology,
            "add_drop_structure": profile.add_drop_structure,
            "protection_type": profile.protection_type,
        }
        conflicts = {
            field_name
            for field_name, supplied in supplied_values.items()
            if supplied
            and supplied != expected_values[field_name]
        }
        expected_modules = _provider_module_inventory(profile)
        if module_inventory and not module_inventory.issubset(expected_modules):
            conflicts.add("module_inventory")
        if facts.sra_state == "absent" and profile.supports_raman:
            conflicts.add("sra_state")
        if conflicts:
            rejected_fields.update(conflicts)
            continue
        compatible.append(profile)

    if not compatible:
        labels = {
            "chassis_family": "chassis family",
            "chassis_pec": "chassis PEC",
            "optical_band": "optical band",
            "topology": "topology",
            "add_drop_structure": "add/drop structure",
            "protection_type": "protection type",
            "module_inventory": "direct module inventory",
            "sra_state": "SRA state",
        }
        ordered = tuple(
            field_name
            for field_name in (
                "chassis_family",
                "chassis_pec",
                "optical_band",
                "topology",
                "add_drop_structure",
                "protection_type",
                "module_inventory",
                "sra_state",
            )
            if field_name in rejected_fields
        )
        return _candidate_resolution(
            "conflict",
            reason_codes=tuple(
                f"{field_name.upper()}_MISMATCH" for field_name in ordered
            )
            or ("NO_COMPATIBLE_PROVIDER",),
            reasons=tuple(
                f"No role-compatible provider matches the supplied "
                f"{labels[field_name]}."
                for field_name in ordered
            )
            or (
                "No exact R4.0 provider is compatible with the supplied facts.",
            ),
        )

    compatible_ids = tuple(profile.provider_id for profile in compatible)
    matched_fields = ["role_profile"]
    if supplied_release:
        matched_fields.append("software_release")
    matched_fields.extend(
        field_name
        for field_name, value in supplied_values.items()
        if value
    )
    if module_inventory:
        matched_fields.append("module_inventory")
    if facts.sra_state != "unknown":
        matched_fields.append("sra_state")

    if len(compatible) > 1:
        return _candidate_resolution(
            "ambiguous",
            compatible_provider_ids=compatible_ids,
            reason_codes=("MULTIPLE_COMPATIBLE_PROVIDERS",),
            reasons=(
                "More than one exact R4.0 provider remains compatible with "
                "the supplied direct facts.",
            ),
            matched_fields=matched_fields,
        )

    profile = compatible[0]
    expected_modules = _provider_module_inventory(profile)
    present = set(matched_fields)
    exact_fields = {
        "software_release",
        "chassis_family",
        "chassis_pec",
        "optical_band",
        "topology",
        "add_drop_structure",
        "protection_type",
        "sra_state",
    }
    complete_modules = module_inventory == expected_modules
    if complete_modules:
        present.add("module_inventory")
    expected_sra_state = "present" if profile.supports_raman else "absent"
    missing_fields = tuple(
        field_name
        for field_name in _R40_CANDIDATE_REQUIRED_EXACT_FIELDS
        if field_name not in present
        or (
            field_name == "sra_state"
            and facts.sra_state != expected_sra_state
        )
        or (
            field_name == "module_inventory"
            and not complete_modules
        )
    )
    if not missing_fields and exact_fields.issubset(present):
        return _candidate_resolution(
            "exact_match",
            provider_id=profile.provider_id,
            compatible_provider_ids=(profile.provider_id,),
            reason_codes=("EXACT_DIRECT_DISCRIMINATORS_MATCH",),
            reasons=(
                "Every exact fixed discriminator and the complete direct "
                "module inventory match one registered R4.0 provider. This "
                "advisory match is not a payload or CLI authorization.",
            ),
            matched_fields=matched_fields,
        )

    return _candidate_resolution(
        "unique_candidate",
        provider_id=profile.provider_id,
        compatible_provider_ids=(profile.provider_id,),
        reason_codes=("UNIQUE_COMPATIBLE_PROVIDER_CANDIDATE",),
        reasons=(
            "Exactly one registered R4.0 provider is compatible with the "
            "supplied facts, but exact discriminator evidence remains "
            "incomplete. Operator review is required.",
        ),
        matched_fields=matched_fields,
        missing_fields=missing_fields,
    )


@dataclass(frozen=True)
class R40LinePath:
    """One local line-output record and its reviewed peer identity.

    The selected provider defines what the record represents.  RLA terminal
    providers use one record per physical bidirectional degree (the paired
    local mux and demux carry opposing traffic flows).  The DLE ILA provider
    uses one record per unidirectional amplifier through-path and local
    line-output.  These meanings must not be interchanged.
    """

    link_name: str
    neighbor_node: str
    neighbor_line_mux_pfg: str
    neighbor_line_demux_pfg: str
    fiber_type: str
    expected_loss_db: float
    input_patch_loss_db: float = 0.5
    output_patch_loss_db: float = 0.5
    repair_margin_db: float = 2.0
    high_loss_minor_threshold_db: float = 3.0
    ospcfib_dbm: float | None = None


@dataclass(frozen=True)
class R40ExactRequest:
    provider_id: str
    profile: str
    software_release: str
    target_software_build: str
    chassis_family: str
    chassis_pec: str
    hardware_profile: str
    shelf_name: str
    site_name: str
    member_name: str
    hostname: str
    frame_identification_code: str
    loopback_ip: str
    ospf_area: str
    line_1: R40LinePath
    line_2: R40LinePath | None
    line_1_route_side: str
    shelf_label: str = ""
    site_id: int = 0
    site_description: str = ""
    site_address: str = ""
    bay_number: int = 0
    physical_shelf: int = 0
    management: ManagementInterface = field(default_factory=ManagementInterface)
    osc_profile: str = "ge-fec-1"
    installed_inventory_confirmed: bool = False
    planner_runtime_mop_confirmed: bool = False
    target_build_confirmed: bool = False
    greenfield_fibers_disconnected_confirmed: bool = False
    calibration_feature_inactive_confirmed: bool = False
    cfim_unused_ports_terminated_confirmed: bool = False


@dataclass(frozen=True)
class R40ConfigArtifact:
    request: R40ExactRequest
    cli_text: str
    annotated_text: str
    validation_report: str
    manifest: Mapping[str, Any]
    issues: tuple[ValidationIssue, ...]
    command_count: int


@dataclass(frozen=True)
class _Command:
    section: str
    text: str
    source: str


class R40ExactConfigGenerator:
    """Generate one of the explicitly registered RLS R4.0 layouts."""

    def validate(
        self, request: R40ExactRequest
    ) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        profile = R40_PROVIDER_CATALOG.get(request.provider_id)
        if profile is None:
            issues.append(
                self._error(
                    "UNKNOWN_R40_PROVIDER",
                    "provider_id",
                    "Select one exact audited R4.0 provider ID.",
                )
            )
        else:
            if request.profile not in profile.role_profiles:
                issues.append(
                    self._error(
                        "R40_PROVIDER_ROLE_MISMATCH",
                        "profile",
                        "The exact provider does not match this shelf's route role.",
                        profile.evidence,
                    )
                )
            for field_name, actual, expected in (
                ("software_release", request.software_release, SUPPORTED_RELEASE),
                ("chassis_family", request.chassis_family, profile.chassis_family),
                ("chassis_pec", request.chassis_pec, profile.chassis_pec),
                (
                    "hardware_profile",
                    request.hardware_profile,
                    profile.hardware_profile,
                ),
            ):
                if actual != expected:
                    issues.append(
                        self._error(
                            "R40_EXACT_DISCRIMINATOR_MISMATCH",
                            field_name,
                            f"Exact provider requires {expected!r}.",
                            profile.evidence,
                        )
                    )

        target_build = (
            request.target_software_build.strip()
            if isinstance(request.target_software_build, str)
            else ""
        )
        generic_target_build = (
            re.sub(r"[\s_-]+", " ", target_build).strip().casefold()
            in {
                "4.0",
                "r4.0",
                "rls 4.0",
                "rls r4.0",
                "release 4.0",
                "release r4.0",
            }
        )
        if not target_build or generic_target_build:
            issues.append(
                self._error(
                    "TARGET_BUILD_REQUIRED",
                    "target_software_build",
                    "Enter a specific R4.0 software build/schema target; a "
                    "blank or generic RLS R4.0 release label is insufficient. "
                    f"ATLAS normally prepopulates the documented "
                    f"{DEFAULT_R40_TARGET_BUILD_SCHEMA} baseline.",
                    "On-box schema validation gate.",
                )
            )
        else:
            self._safe_text(
                request.target_software_build,
                "target_software_build",
                "Target software build",
                64,
                issues,
            )
            if target_build == DEFAULT_R40_TARGET_BUILD_SCHEMA:
                issues.append(
                    self._warning(
                        "TARGET_BUILD_SCHEMA_DEFAULTED",
                        "target_software_build",
                        (
                            f"ATLAS prepopulated the documented "
                            f"{DEFAULT_R40_TARGET_BUILD_SCHEMA} R4.0.0 baseline. "
                            "This is not an on-box observation, and the R4.0 "
                            "release notes also cover 4.00.01. Verify the "
                            "running shelf and require successful validate "
                            "before commit."
                        ),
                        (
                            "NTRN38WA.1/.2 RLS R4.0.0 Software Upgrade "
                            "Procedure; 6500 RLS R4.0 Release Notes"
                        ),
                    )
                )

        if not _SHELF_RE.fullmatch(request.shelf_name or ""):
            issues.append(
                self._error(
                    "INVALID_SHELF_NAME",
                    "shelf_name",
                    "Shelf name must be 1-32 letters, digits, or hyphens and "
                    "cannot contain an underscore.",
                    "323-2051-220 printed pp.98-99",
                )
            )
        for field_name, label, value, maximum, required in (
            ("shelf_label", "Shelf label", request.shelf_label, 64, False),
            ("site_name", "Site name", request.site_name, 32, True),
            (
                "site_description",
                "Site description",
                request.site_description,
                64,
                False,
            ),
            ("site_address", "Site address", request.site_address, 128, False),
            (
                "frame_identification_code",
                "Frame identification code",
                request.frame_identification_code,
                128,
                False,
            ),
        ):
            self._safe_text(
                value,
                field_name,
                label,
                maximum,
                issues,
                required=required,
            )
        if (
            isinstance(request.frame_identification_code, str)
            and not request.frame_identification_code.strip()
        ):
            issues.append(
                self._warning(
                    "FRAME_LOCATION_DEFERRED",
                    "frame_identification_code",
                    (
                        "Frame/rack location was not available. ATLAS will "
                        "omit the shelf-location command; enter it onsite "
                        "later if required."
                    ),
                    "LightRiver onsite installation workflow.",
                )
            )
        self._member(
            request.member_name, "member_name", "Member name", issues
        )
        self._hostname(request.hostname, issues)
        self._integer(request.site_id, "site_id", 0, 65_536, issues)
        self._integer(request.bay_number, "bay_number", 0, 99, issues)
        self._integer(
            request.physical_shelf, "physical_shelf", 0, 9, issues
        )
        loopback = self._ipv4(request.loopback_ip, "loopback_ip", issues)
        self._ospf_area(request.ospf_area, issues)
        self._management(request.management, loopback, issues)

        if request.osc_profile != "ge-fec-1":
            issues.append(
                self._error(
                    "UNSUPPORTED_OSC_PROFILE",
                    "osc_profile",
                    "These exact providers are audited only with ge-fec-1. "
                    "A different OSC profile requires a separate exact provider.",
                    "323-2051-220 printed pp.103,204-212,279-284",
                )
            )
        if profile is not None:
            if profile.colan_policy == COLAN_PROHIBITED and (
                request.management.enabled
                or request.management.routing_mode != ROUTING_OSPF_OSC_ONLY
                or request.management.name
                or request.management.ip_address
                or request.management.prefix_length is not None
                or request.management.gateway
            ):
                issues.append(
                    self._error(
                        "ILA_COLAN_PROHIBITED",
                        "management",
                        "ILA shelves do not use COLAN. Use the no-direct-DCN "
                        "RNE mode with blank COLAN interface, address, prefix, "
                        "and gateway values.",
                        "LightRiver deployment policy; 323-2051-101 RLS R4.0 "
                        "OSPF RNE examples",
                    )
                )
            elif profile.colan_policy == COLAN_TERMINAL_REQUIRED and (
                not request.management.enabled
                or request.management.routing_mode == ROUTING_OSPF_OSC_ONLY
            ):
                issues.append(
                    self._error(
                        "TERMINAL_COLAN_REQUIRED",
                        "management",
                        "Add/Drop and ROADM terminal shelves require the "
                        "customer-provided on-site COLAN design before CLI "
                        "can be generated.",
                        "LightRiver terminal installation policy",
                    )
                )
            elif (
                profile.colan_policy == COLAN_TERMINAL_OPTIONAL
                and not request.management.enabled
                and request.management.routing_mode == ROUTING_OSPF_OSC_ONLY
                and not request.management.name
                and not request.management.ip_address
                and request.management.prefix_length is None
                and not request.management.gateway
            ):
                issues.append(
                    self._warning(
                        "TERMINAL_COLAN_DEFERRED",
                        "management",
                        "Terminal COLAN is deferred for factory staging. "
                        "ATLAS will emit the remaining exact shelf candidate "
                        "without COLAN interface or COLAN routing commands. "
                        "Direct COLAN remote access is unavailable until a "
                        "complete customer-approved COLAN design is added.",
                        "LightRiver factory-staging workflow",
                    )
                )

        if profile is not None:
            for control in r40_deployment_controls(profile.provider_id):
                self._background_deployment_control(
                    getattr(request, control.legacy_field),
                    control,
                    issues,
                )

        self._line("line_1", request.line_1, issues)
        if profile is not None:
            expected_line_count = len(profile.line_outputs)
            cardinalities = {
                len(profile.line_inputs),
                len(profile.line_pfg_names),
                len(profile.line_link_names),
            }
            if cardinalities != {expected_line_count} or expected_line_count not in {
                1,
                2,
            }:
                issues.append(
                    self._error(
                        "INVALID_R40_PROVIDER_LINE_CARDINALITY",
                        "provider_id",
                        "The selected exact provider has inconsistent internal "
                        "line metadata and cannot be used.",
                        profile.evidence,
                    )
                )
            if expected_line_count == 1 and request.line_2 is not None:
                issues.append(
                    self._error(
                        "R40_LINE_CARDINALITY_MISMATCH",
                        "line_2",
                        "This provider has one physical bidirectional degree; "
                        "line_2 must be null. Reverse traffic uses the same "
                        "degree's paired mux/demux path.",
                        profile.evidence,
                    )
                )
            elif expected_line_count == 2 and request.line_2 is None:
                issues.append(
                    self._error(
                        "R40_LINE_CARDINALITY_MISMATCH",
                        "line_2",
                        "This provider has two independently reviewed physical "
                        "degrees or amplifier paths; line_2 is required.",
                        profile.evidence,
                    )
                )
        if request.line_2 is not None:
            self._line("line_2", request.line_2, issues)
        if request.line_1_route_side not in R40_ROUTE_SIDES:
            is_degree = (
                profile is not None
                and profile.line_semantics == "bidirectional_degree"
            )
            assignment = (
                "fixed physical degree 1"
                if is_degree
                else "fixed amplifier path 1 line-output"
            )
            opposite = (
                " Its paired mux/demux carries both traffic directions."
                if profile is not None and len(profile.line_outputs) == 1
                else " The second local line-output uses the opposite side."
            )
            issues.append(
                self._error(
                    "R40_DEGREE_ROUTE_SIDE_REQUIRED",
                    "line_1_route_side",
                    f"Explicitly assign {assignment} to route side A "
                    f"(preceding shelf) or Z (following shelf).{opposite}",
                    "Installed packout and direction-to-port assignment.",
                )
            )
        if (
            request.line_2 is not None
            and isinstance(request.line_1.link_name, str)
            and isinstance(request.line_2.link_name, str)
            and request.line_1.link_name.casefold()
            == request.line_2.link_name.casefold()
        ):
            issues.append(
                self._error(
                    "DUPLICATE_LINK_NAME",
                    "line_1.link_name,line_2.link_name",
                    "The two line-fiber links require unique names.",
                )
            )
        if (
            profile is not None
            and profile.line_semantics
            == "unidirectional_amplifier_path"
            and request.line_2 is not None
            and isinstance(request.line_1.neighbor_node, str)
            and isinstance(request.line_2.neighbor_node, str)
            and request.line_1.neighbor_node.strip().casefold()
            == request.line_2.neighbor_node.strip().casefold()
        ):
            issues.append(
                self._error(
                    "ILA_DISTINCT_SIDE_NEIGHBORS_REQUIRED",
                    "line_1.neighbor_node,line_2.neighbor_node",
                    "A DLE through-path must cross the two physical sides: "
                    "PFG-1-to-2 uses the first output-side neighbor "
                    "downstream and the opposite-side neighbor upstream; "
                    "PFG-2-to-1 swaps them. Enter the distinct adjacent "
                    "A-side and Z-side node TIDs.",
                    "323-2051-220 RLS R4.0 printed pp.40-42",
                )
            )

        issues.extend(
            (
                self._warning(
                    "ON_BOX_VALIDATE_REQUIRED",
                    "deployment",
                    "Run each emitted batch on an empty matching shelf, capture "
                    "a successful validate result, and stop before commit on any "
                    "error. Offline validation is not on-box authorization.",
                    "323-2051-220 printed p.189; 323-2051-190 printed pp.11-13",
                ),
                self._warning(
                    "LICENSED_FEATURES_DEFERRED",
                    "licensed_features",
                    "OTDR, Span Calibration, Passive Terminal Control, NBI, and "
                    "other licensed feature activation are intentionally omitted. "
                    "Activate them only in the separate staged MOP after inventory, "
                    "license, SCO, and calibration prerequisites are verified.",
                    "323-2051-300 RLS R4.0 printed pp.254-256,290-291",
                ),
                self._warning(
                    "REMOTE_ENDPOINT_DISCOVERY_REQUIRED",
                    (
                        "line_1,line_2"
                        if request.line_2 is not None
                        else "line_1"
                    ),
                    "External line links use a local from endpoint only. Verify "
                    "the discovered far-end node/slot/port and PFG neighbors before "
                    "calibration.",
                    "323-2051-318 printed pp.185-187,234-235",
                ),
            )
        )
        return tuple(issues)

    def generate(self, request: R40ExactRequest) -> R40ConfigArtifact:
        issues = self.validate(request)
        errors = tuple(issue for issue in issues if issue.severity == "error")
        if errors:
            raise ConfigValidationError(errors)
        profile = R40_PROVIDER_CATALOG[request.provider_id]
        commands: list[_Command] = []
        commands.extend(self._identity_commands(request))
        commands.extend(self._equipment_commands(profile))
        commands.extend(self._oam_commands(request, profile))
        commands.extend(self._osc_profile_commands(request, profile))
        commands.extend(self._pfg_commands(request, profile))
        if profile.application == "ila_dle_cl":
            commands.extend(self._ila_sco_disable_commands())
        commands.extend(self._link_commands(request, profile))
        if profile.application == "cdc_roadm_rla32_c":
            commands.extend(self._roadm_cv_commands())

        cli_text = self._render_cli(commands)
        annotated = self._render_annotated(request, profile, commands, issues)
        report = self._render_report(request, profile, commands, issues)
        manifest = self._manifest(
            request, profile, commands, issues, cli_text
        )
        return R40ConfigArtifact(
            request=request,
            cli_text=cli_text,
            annotated_text=annotated,
            validation_report=report,
            manifest=manifest,
            issues=issues,
            command_count=len(commands),
        )

    @staticmethod
    def _error(
        code: str, field_name: str, message: str, source: str = ""
    ) -> ValidationIssue:
        return ValidationIssue("error", code, field_name, message, source)

    @staticmethod
    def _warning(
        code: str, field_name: str, message: str, source: str = ""
    ) -> ValidationIssue:
        return ValidationIssue("warning", code, field_name, message, source)

    @classmethod
    def _safe_text(
        cls,
        value: object,
        field_name: str,
        label: str,
        maximum: int,
        issues: list[ValidationIssue],
        *,
        required: bool = True,
    ) -> None:
        if not isinstance(value, str):
            issues.append(
                cls._error("INVALID_TEXT_TYPE", field_name, f"{label} must be text.")
            )
            return
        if required and not value.strip():
            issues.append(
                cls._error("REQUIRED", field_name, f"{label} is required.")
            )
        if len(value) > maximum:
            issues.append(
                cls._error(
                    "TEXT_TOO_LONG",
                    field_name,
                    f"{label} is limited to {maximum} characters.",
                )
            )
        if '"' in value or any(ord(char) < 32 for char in value):
            issues.append(
                cls._error(
                    "CLI_UNSAFE_TEXT",
                    field_name,
                    f"{label} cannot contain quotes, newlines, or control characters.",
                )
            )

    @classmethod
    def _identifier(
        cls,
        value: object,
        field_name: str,
        label: str,
        issues: list[ValidationIssue],
    ) -> None:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            issues.append(
                cls._error(
                    "INVALID_IDENTIFIER",
                    field_name,
                    f"{label} must be 1-64 CLI-safe letters, digits, dots, "
                    "hyphens, or underscores.",
                )
            )

    @classmethod
    def _member(
        cls,
        value: object,
        field_name: str,
        label: str,
        issues: list[ValidationIssue],
    ) -> None:
        if not isinstance(value, str) or not _MEMBER_RE.fullmatch(value):
            issues.append(
                cls._error(
                    "INVALID_MEMBER",
                    field_name,
                    f"{label} must be 1-32 CLI-safe letters, digits, dots, "
                    "hyphens, or underscores.",
                )
            )

    @classmethod
    def _hostname(
        cls, value: object, issues: list[ValidationIssue]
    ) -> None:
        if not isinstance(value, str):
            issues.append(
                cls._error(
                    "INVALID_HOSTNAME", "hostname", "Hostname must be text."
                )
            )
            return
        clean = value.rstrip(".")
        if (
            not clean
            or len(clean) > 253
            or any(
                _DNS_LABEL_RE.fullmatch(label) is None
                for label in clean.split(".")
            )
        ):
            issues.append(
                cls._error(
                    "INVALID_HOSTNAME",
                    "hostname",
                    "Hostname must use valid DNS labels.",
                )
            )

    @classmethod
    def _integer(
        cls,
        value: object,
        field_name: str,
        minimum: int,
        maximum: int,
        issues: list[ValidationIssue],
    ) -> None:
        if type(value) is not int or not minimum <= value <= maximum:
            issues.append(
                cls._error(
                    "INVALID_INTEGER",
                    field_name,
                    f"Value must be an integer from {minimum} through {maximum}.",
                )
            )

    @classmethod
    def _ipv4(
        cls,
        value: object,
        field_name: str,
        issues: list[ValidationIssue],
    ) -> ipaddress.IPv4Address | None:
        try:
            address = ipaddress.IPv4Address(value)
        except (ipaddress.AddressValueError, TypeError, ValueError):
            issues.append(
                cls._error(
                    "INVALID_IPV4", field_name, "Enter a valid IPv4 address."
                )
            )
            return None
        if (
            address.is_unspecified
            or address.is_multicast
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address == ipaddress.IPv4Address("255.255.255.255")
            or any(
                address in network for network in _RLS_INVALID_IPV4_NETWORKS
            )
        ):
            issues.append(
                cls._error(
                    "INVALID_IPV4_CLASS",
                    field_name,
                    "Address must be a usable unicast IPv4 address.",
                )
            )
        return address

    @classmethod
    def _ospf_area(
        cls,
        value: object,
        issues: list[ValidationIssue],
    ) -> None:
        if not isinstance(value, str):
            issues.append(
                cls._error(
                    "INVALID_OSPF_AREA",
                    "ospf_area",
                    "OSPF area must be a dotted-quad value such as 0.0.0.0.",
                )
            )
            return
        try:
            ipaddress.IPv4Address(value)
        except (ipaddress.AddressValueError, ValueError):
            issues.append(
                cls._error(
                    "INVALID_OSPF_AREA",
                    "ospf_area",
                    "OSPF area must be a dotted-quad value such as 0.0.0.0.",
                )
            )

    @classmethod
    def _background_deployment_control(
        cls,
        value: object,
        control: R40DeploymentControl,
        issues: list[ValidationIssue],
    ) -> None:
        """Validate a legacy Boolean and carry its control as an advisory.

        A true legacy checkbox cannot prove a physical condition, and a false
        checkbox must not prevent creation of an offline pre-calibration
        candidate. The control therefore remains active and visible in either
        case; only a malformed schema value is an error.
        """

        if type(value) is not bool:
            issues.append(
                cls._error(
                    "INVALID_BOOLEAN",
                    control.legacy_field,
                    "Legacy confirmation compatibility value must be Boolean.",
                )
            )
            return
        issues.append(
            cls._warning(
                "BACKGROUND_DEPLOYMENT_CONTROL",
                control.legacy_field,
                (
                    f"Automatic deployment procedure included "
                    f"({control.stage}): "
                    f"{control.instruction} ATLAS does not assert that this "
                    "physical condition has been verified; successful staged "
                    "on-box validation remains required."
                ),
                control.source,
            )
        )

    @classmethod
    def _number(
        cls,
        value: object,
        field_name: str,
        minimum: float,
        maximum: float,
        issues: list[ValidationIssue],
    ) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(
                cls._error(
                    "INVALID_NUMBER", field_name, "Enter a finite number."
                )
            )
            return None
        result = float(value)
        if not math.isfinite(result) or not minimum <= result <= maximum:
            issues.append(
                cls._error(
                    "NUMBER_OUT_OF_RANGE",
                    field_name,
                    f"Value must be from {minimum:g} through {maximum:g}.",
                )
            )
            return None
        return result

    def _management(
        self,
        management: object,
        loopback: ipaddress.IPv4Address | None,
        issues: list[ValidationIssue],
    ) -> None:
        if not isinstance(management, ManagementInterface):
            issues.append(
                self._error(
                    "INVALID_MANAGEMENT",
                    "management",
                    "Management settings are invalid.",
                )
            )
            return
        if management.routing_mode not in ROUTING_MODES:
            issues.append(
                self._error(
                    "INVALID_ROUTING_MODE",
                    "management.routing_mode",
                    "Choose an audited COLAN/OSPF routing mode.",
                )
            )
            return
        numbered = management.routing_mode != ROUTING_OSPF_OSC_ONLY
        if management.enabled != numbered:
            issues.append(
                self._error(
                    "MANAGEMENT_MODE_MISMATCH",
                    "management.enabled",
                    "Management enabled state must match the selected routing mode.",
                )
            )
        if not numbered and (
            management.name
            or management.ip_address
            or management.prefix_length is not None
            or management.gateway
        ):
            issues.append(
                self._error(
                    "UNUSED_MANAGEMENT_ADDRESS",
                    "management",
                    "No-direct-DCN mode requires blank COLAN interface, "
                    "address, prefix, and gateway fields.",
                )
            )
        if numbered:
            if management.name not in {"colan-x", "colan-a"}:
                issues.append(
                    self._error(
                        "INVALID_COLAN_NAME",
                        "management.name",
                        "COLAN interface must be colan-x or colan-a.",
                    )
                )
            address = self._ipv4(
                management.ip_address,
                "management.ip_address",
                issues,
            )
            if (
                type(management.prefix_length) is not int
                or not 1 <= management.prefix_length <= 31
            ):
                issues.append(
                    self._error(
                        "INVALID_PREFIX",
                        "management.prefix_length",
                        "COLAN prefix must be 1 through 31.",
                    )
                )
            else:
                network = (
                    ipaddress.IPv4Network(
                        (address, management.prefix_length),
                        strict=False,
                    )
                    if address is not None
                    else None
                )
                if (
                    network is not None
                    and management.prefix_length < 31
                    and address
                    in {network.network_address, network.broadcast_address}
                ):
                    issues.append(
                        self._error(
                            "COLAN_HOST_ADDRESS",
                            "management.ip_address",
                            "COLAN address cannot be the subnet network or "
                            "broadcast address.",
                        )
                    )
                if (
                    network is not None
                    and loopback is not None
                    and loopback in network
                ):
                    issues.append(
                        self._error(
                            "OVERLAPPING_PREFIX",
                            "management.ip_address",
                            "COLAN prefix must not overlap the loopback address.",
                            "323-2051-101 printed pp.76-79",
                        )
                    )
            if loopback is not None and address == loopback:
                issues.append(
                    self._error(
                        "DUPLICATE_INTERFACE_ADDRESS",
                        "management.ip_address",
                        "COLAN and loopback addresses must differ.",
                    )
                )
        if management.routing_mode == ROUTING_STATIC_COLAN_OSPF_OSC:
            gateway = self._ipv4(
                management.gateway, "management.gateway", issues
            )
            if (
                numbered
                and address is not None
                and gateway is not None
                and type(management.prefix_length) is int
                and 1 <= management.prefix_length <= 31
            ):
                network = ipaddress.IPv4Network(
                    (address, management.prefix_length),
                    strict=False,
                )
                if gateway not in network:
                    issues.append(
                        self._error(
                            "GATEWAY_OUTSIDE_SUBNET",
                            "management.gateway",
                            "Gateway must be in the numbered COLAN subnet.",
                        )
                    )
                elif gateway == address or (
                    management.prefix_length < 31
                    and gateway
                    in {network.network_address, network.broadcast_address}
                ):
                    issues.append(
                        self._error(
                            "INVALID_GATEWAY_HOST",
                            "management.gateway",
                            "Gateway must be a different usable host in the "
                            "COLAN subnet.",
                        )
                    )
        elif management.gateway:
            issues.append(
                self._error(
                    "UNUSED_GATEWAY",
                    "management.gateway",
                    "Gateway must be blank unless static COLAN routing is selected.",
                )
            )
        self._integer(
            management.ospf_metric,
            "management.ospf_metric",
            1,
            65_535,
            issues,
        )
        self._integer(
            management.static_metric,
            "management.static_metric",
            1,
            4_294_967_295,
            issues,
        )

    def _line(
        self,
        field_name: str,
        line: object,
        issues: list[ValidationIssue],
    ) -> None:
        if not isinstance(line, R40LinePath):
            issues.append(
                self._error(
                    "INVALID_LINE_PATH",
                    field_name,
                    "Line path is invalid.",
                )
            )
            return
        self._member(
            line.neighbor_node,
            f"{field_name}.neighbor_node",
            "Neighbor node",
            issues,
        )
        for suffix, label, value in (
            ("link_name", "Link name", line.link_name),
            (
                "neighbor_line_mux_pfg",
                "Neighbor line-mux PFG",
                line.neighbor_line_mux_pfg,
            ),
            (
                "neighbor_line_demux_pfg",
                "Neighbor line-demux PFG",
                line.neighbor_line_demux_pfg,
            ),
        ):
            self._identifier(
                value, f"{field_name}.{suffix}", label, issues
            )
        if line.fiber_type not in FIBER_TYPES:
            issues.append(
                self._error(
                    "UNSUPPORTED_FIBER_TYPE",
                    f"{field_name}.fiber_type",
                    "Choose an exact native RLS R4.0 fiber token; Unknown and "
                    "legacy aliases are not accepted.",
                    "323-2051-220 printed pp.121-123",
                )
            )
        elif line.fiber_type == "LEAF":
            issues.append(
                self._warning(
                    "R40_LEAF_VENDOR_ENUM_INCONSISTENCY",
                    f"{field_name}.fiber_type",
                    (
                        "RLS R4.0 commissioning printed p.199 and the audited "
                        "legacy workbook emit the exact LEAF token, while the "
                        "formal printed pp.121-122 value table omits plain "
                        "LEAF. Preserve the diagram value, validate this batch "
                        "on the target shelf, and stop before commit on any "
                        "error."
                    ),
                    (
                        "323-2051-220 printed pp.121-122,199; "
                        "Ciena RLS C+L CLI Config v3.5LR RefValue A31"
                    ),
                )
            )
        loss = self._number(
            line.expected_loss_db,
            f"{field_name}.expected_loss_db",
            0.1,
            70.0,
            issues,
        )
        self._number(
            line.input_patch_loss_db,
            f"{field_name}.input_patch_loss_db",
            0.0,
            10.0,
            issues,
        )
        self._number(
            line.output_patch_loss_db,
            f"{field_name}.output_patch_loss_db",
            0.0,
            10.0,
            issues,
        )
        self._number(
            line.repair_margin_db,
            f"{field_name}.repair_margin_db",
            0.0,
            20.0,
            issues,
        )
        self._number(
            line.high_loss_minor_threshold_db,
            f"{field_name}.high_loss_minor_threshold_db",
            0.1,
            30.0,
            issues,
        )
        if line.ospcfib_dbm is not None:
            self._number(
                line.ospcfib_dbm,
                f"{field_name}.ospcfib_dbm",
                -20.0,
                10.0,
                issues,
            )
        elif loss is not None and loss > 30.0:
            issues.append(
                self._error(
                    "OSCPFIB_REQUIRED_FOR_HIGH_LOSS_SPAN",
                    f"{field_name}.ospcfib_dbm",
                    "PlannerPlus OSCPFIB is required when the engineered span "
                    "loss exceeds 30 dB.",
                    "323-2051-220 printed pp.119,499",
                )
            )

    @staticmethod
    def _transaction(
        section: str, body: Iterable[str], source: str
    ) -> list[_Command]:
        commands = [_Command(section, "batch", source)]
        commands.extend(_Command(section, item, source) for item in body)
        commands.extend(
            (
                _Command(section, "validate", source),
                _Command(section, "commit", source),
                _Command(section, "quit", source),
            )
        )
        return commands

    def _identity_commands(
        self, request: R40ExactRequest
    ) -> list[_Command]:
        body = [
            "set ztp admin-state disabled",
            f'set shelf name "{request.shelf_name}"',
        ]
        if request.shelf_label:
            body.append(f'set shelf label "{request.shelf_label}"')
        site = (
            "set ciena-6500r-system:system id site "
            f'id {request.site_id} name "{request.site_name}"'
        )
        if request.site_description:
            site += f' description "{request.site_description}"'
        if request.site_address:
            site += f' address "{request.site_address}"'
        body.append(site)
        if request.frame_identification_code.strip():
            body.append(
                "set shelf shelf-location "
                f'frame-identification-code "{request.frame_identification_code}" '
                f"bay-num {request.bay_number} phys-shelf {request.physical_shelf}"
            )
        body.extend(
            (
                "set ciena-6500r-system:system id member "
                f'name "{request.member_name}"',
                f"set openconfig-system:system config hostname {request.hostname}",
            )
        )
        return self._transaction(
            "Shelf, site, and member identity",
            body,
            "323-2051-220 printed pp.98-100,190",
        )

    def _equipment_commands(
        self, profile: R40ProviderProfile
    ) -> list[_Command]:
        body = [
            f"create slots {slot} config circuit-pack pec {pec}"
            for slot, pec in profile.equipment
        ]
        for slot, pec in profile.equipment:
            if pec == "NTK830AC":
                body.extend(
                    (
                        f"set slots {slot} config circuit-pack admin-state disabled",
                        f"set slots {slot} config ramans LINE-IN admin-state disabled",
                    )
                )
        return self._transaction(
            "Exact fixed parent equipment",
            body,
            profile.evidence,
        )

    def _oam_commands(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        loopback = request.loopback_ip
        area = request.ospf_area
        mgmt = request.management
        ni = "network-instances network-instance default"
        ospf = f"{ni} protocols protocol OSPF,2"
        osc_interfaces = tuple(
            f"osc-{slot}-{subslot}-1"
            for slot, subslot, _pec in profile.osc_modules
        )
        body = [
            "set openconfig-interfaces:interfaces interface loopback config "
            "name loopback type softwareLoopback mtu 1500 enabled true",
            "set openconfig-interfaces:interfaces interface loopback "
            "subinterfaces subinterface 0 config index 0 enabled true",
            "set openconfig-interfaces:interfaces interface loopback "
            f"subinterfaces subinterface 0 ipv4 addresses address {loopback} "
            f"config ip {loopback} prefix-length 32",
            *[
                "create slots "
                f"{slot} config slots {subslot} config circuit-pack pec {pec}"
                for slot, subslot, pec in profile.osc_modules
            ],
            f"set {ni} config name default type DEFAULT_INSTANCE enabled true "
            f'description "network instance" router-id {loopback}',
            f"set {ni} interfaces interface loopback.0 config id loopback.0 "
            "interface loopback subinterface 0 associated-address-families IPV4",
            f"set {ni} tables table OSPF,IPV4 config address-family IPV4 protocol OSPF",
            f"set {ospf} config identifier OSPF name 2 enabled true default-metric 10",
            f'set {ospf} ospfv2 global config router-id "{loopback}"',
            f"set {ospf} ospfv2 areas area {area} config identifier {area}",
            f"set {ospf} ospfv2 areas area {area} interfaces interface loopback.0 "
            "config id loopback.0 network-type BROADCAST_NETWORK priority 1 "
            "multi-area-adjacency-primary true metric 1 passive true",
            f"set {ospf} ospfv2 areas area {area} interfaces interface loopback.0 "
            "interface-ref config interface loopback subinterface 0",
            f"set {ospf} ospfv2 areas area {area} interfaces interface loopback.0 "
            "timers config dead-interval 40 hello-interval 10 "
            "retransmission-interval 10",
        ]
        if mgmt.enabled:
            name = mgmt.name
            body.extend(
                (
                    "set openconfig-interfaces:interfaces interface "
                    f"{name} config name {name} type ethernetCsmacd mtu 1500 "
                    "enabled true",
                    "set openconfig-interfaces:interfaces interface "
                    f"{name} subinterfaces subinterface 0 config index 0 enabled true",
                    "set openconfig-interfaces:interfaces interface "
                    f"{name} subinterfaces subinterface 0 ipv4 addresses address "
                    f"{mgmt.ip_address} config ip {mgmt.ip_address} "
                    f"prefix-length {mgmt.prefix_length}",
                    f"set {ni} interfaces interface {name}.0 config id {name}.0 "
                    f"interface {name} subinterface 0 associated-address-families IPV4",
                )
            )
            if mgmt.routing_mode == ROUTING_OSPF_GNE:
                body.extend(
                    self._ospf_interface(
                        ospf,
                        area,
                        f"{name}.0",
                        name,
                        "BROADCAST_NETWORK",
                        mgmt.ospf_metric,
                    )
                )
            else:
                static = f"{ni} protocols protocol STATIC,3"
                body.extend(
                    (
                        f"set {ni} tables table STATIC,IPV4 config address-family "
                        "IPV4 protocol STATIC",
                        f"set {static} config identifier STATIC name 3 enabled true "
                        "default-metric 10",
                        f'set {static} static-routes static "0.0.0.0/0" config '
                        'prefix "0.0.0.0/0"',
                        f'set {static} static-routes static "0.0.0.0/0" next-hops '
                        f"next-hop 1 config index 1 next-hop {mgmt.gateway} "
                        f"metric {mgmt.static_metric}",
                        f'set {static} static-routes static "0.0.0.0/0" next-hops '
                        f"next-hop 1 interface-ref config interface {name} "
                        "subinterface 0",
                        f"set {ni} table-connections table-connection "
                        "STATIC,OSPF,IPV4 config src-protocol STATIC "
                        "dst-protocol OSPF address-family IPV4",
                    )
                )
        for interface in osc_interfaces:
            body.extend(
                (
                    f"set {ni} interfaces interface {interface}.0 config "
                    f"id {interface}.0 interface {interface} subinterface 0 "
                    "associated-address-families IPV4",
                    *self._ospf_interface(
                        ospf,
                        area,
                        f"{interface}.0",
                        interface,
                        "POINT_TO_POINT_NETWORK",
                        10,
                    ),
                )
            )
        return self._transaction(
            "Complete initial IPv4 OAM",
            body,
            "323-2051-101 printed pp.40,116,168-169,224-232; "
            "323-2051-220 printed pp.104-108,263",
        )

    @staticmethod
    def _ospf_interface(
        ospf: str,
        area: str,
        interface_id: str,
        interface_name: str,
        network_type: str,
        metric: int,
    ) -> tuple[str, str, str]:
        base = (
            f"set {ospf} ospfv2 areas area {area} interfaces "
            f"interface {interface_id}"
        )
        return (
            f"{base} config id {interface_id} network-type {network_type} "
            f"priority 1 multi-area-adjacency-primary true metric {metric} "
            "passive false",
            f"{base} interface-ref config interface {interface_name} subinterface 0",
            f"{base} timers config dead-interval 40 hello-interval 10 "
            "retransmission-interval 10",
        )

    def _osc_profile_commands(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        body = [
            f"set slots {slot} config slots {subslot} config oscs 1 "
            f"prov-profile {request.osc_profile}"
            for slot, subslot, _pec in profile.osc_modules
        ]
        return self._transaction(
            "OSC provisioning profiles",
            body,
            "323-2051-220 printed pp.103,204-212,279-284",
        )

    def _pfg_commands(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        if profile.application == "ila_dle_cl":
            return self._ila_pfg_commands(request, profile)
        if profile.application == "roadm_rla12_cl_lru12_core":
            return self._rla12_cl_lru12_pfg_commands(
                request.line_1,
                profile,
            )
        commands: list[_Command] = []
        assert request.line_2 is not None
        for index, slot in enumerate((1, 3), start=1):
            line = request.line_1 if index == 1 else request.line_2
            commands.extend(
                self._rla_c_pfg_commands(
                    slot=slot,
                    ordinal=index,
                    line=line,
                )
            )
        if profile.application == "cda_rla12_c":
            commands.extend(self._ccmd16_c_pfg_commands(slot=5))
        else:
            commands.extend(self._ccmd8x24_c_pfg_commands(slot=5))
        return commands

    def _rla12_cl_lru12_pfg_commands(
        self,
        line: R40LinePath,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        """Emit the audited one-degree RLA12/LRU12 C+L core PFGs."""

        source = (
            "323-2051-220 printed pp.285-289,442-443; "
            "NTRN10WA printed pp.576,620-623"
        )
        c_band = (
            "roles BAND-C attrs MIN-FREQ value 191.27500",
            "roles BAND-C attrs MAX-FREQ value 196.15000",
        )
        l_band = (
            "roles BAND-L attrs MIN-FREQ value 186.05000",
            "roles BAND-L attrs MAX-FREQ value 190.87500",
        )
        line_output_slot, line_output_port = profile.line_outputs[0]
        line_input_slot, line_input_port = profile.line_inputs[0]
        lm_body = [
            "set functional-group LM1 type LINE-MUX",
            *(f"set functional-group LM1 {item}" for item in c_band),
            *(f"set functional-group LM1 {item}" for item in l_band),
            'set functional-group LM1 roles LINEOUTOSC objects '
            '"slots 1 config slots 50 config oscs 1"',
            'set functional-group LM1 roles LINEOUT objects '
            f'"slots {line_output_slot} config port {line_output_port}"',
            'set functional-group LM1 roles BOOSTER objects '
            '"slots 1 config amps booster-c-band" attrs BAND value BAND-C',
            'set functional-group LM1 roles BOOSTER objects '
            '"slots 1 config amps booster-l-band" attrs BAND value BAND-L',
            'set functional-group LM1 roles LINE-OUT-VOA objects '
            '"slots 1 config voa booster-c-band" attrs BAND value BAND-C',
            'set functional-group LM1 roles LINE-OUT-VOA objects '
            '"slots 1 config voa booster-l-band" attrs BAND value BAND-L',
            "set functional-group LM1 attrs DOWNSTR-PFG-NEIGHBOR "
            f"value {line.neighbor_node}/{line.neighbor_line_demux_pfg}",
        ]
        if line.ospcfib_dbm is not None:
            lm_body.append(
                "set functional-group LM1 attrs OSCPFIB value "
                f"{self._num(line.ospcfib_dbm)}"
            )
        ld_body = [
            "set functional-group LD1 type LINE-DEMUX",
            *(f"set functional-group LD1 {item}" for item in c_band),
            *(f"set functional-group LD1 {item}" for item in l_band),
            'set functional-group LD1 roles LINEINOSC objects '
            '"slots 1 config slots 50 config oscs 1"',
            'set functional-group LD1 roles LINEIN objects '
            f'"slots {line_input_slot} config port {line_input_port}"',
            'set functional-group LD1 roles PREAMP objects '
            '"slots 1 config amps pre-amp-c-band" attrs BAND value BAND-C',
            'set functional-group LD1 roles PREAMP objects '
            '"slots 3 config amps pre-amp-l-band" attrs BAND value BAND-L',
            "set functional-group LD1 attrs UPSTR-PFG-NEIGHBOR "
            f"value {line.neighbor_node}/{line.neighbor_line_mux_pfg}",
        ]
        if profile.supports_raman:
            ld_body.insert(
                -1,
                'set functional-group LD1 roles RAMAN objects '
                '"slots 6 config ramans LINE-IN"',
            )
        sm_body = (
            "set functional-group SM1 type SECTION-MUX",
            *(f"set functional-group SM1 {item}" for item in c_band),
            *(f"set functional-group SM1 {item}" for item in l_band),
            'set functional-group SM1 roles ASE-AMP objects '
            '"slots 1 config amps ase-c-band" attrs BAND value BAND-C',
            'set functional-group SM1 roles ASE-AMP objects '
            '"slots 3 config amps ase-l-band" attrs BAND value BAND-L',
            'set functional-group SM1 roles UPG-ASE-AMP objects '
            '"slots 1 config amps ase-l-band" attrs BAND value BAND-L',
            'set functional-group SM1 roles WSS objects '
            '"slots 1 config wss mux-c-band" attrs BAND value BAND-C',
            'set functional-group SM1 roles WSS objects '
            '"slots 3 config wss mux-l-band" attrs BAND value BAND-L',
        )
        sd_body = (
            "set functional-group SD1 type SECTION-DEMUX",
            *(f"set functional-group SD1 {item}" for item in c_band),
            *(f"set functional-group SD1 {item}" for item in l_band),
            'set functional-group SD1 roles WSS objects '
            '"slots 1 config wss demux-c-band" attrs BAND value BAND-C',
            'set functional-group SD1 roles WSS objects '
            '"slots 3 config wss demux-l-band" attrs BAND value BAND-L',
        )
        return [
            *self._transaction("LM1 exact C+L PFG", lm_body, source),
            *self._transaction("LD1 exact C+L PFG", ld_body, source),
            *self._transaction("SM1 exact C+L PFG", sm_body, source),
            *self._transaction("SD1 exact C+L PFG", sd_body, source),
        ]

    def _rla_c_pfg_commands(
        self,
        *,
        slot: int,
        ordinal: int,
        line: R40LinePath,
    ) -> list[_Command]:
        lm = f"LINE-MUX-PFG-{ordinal}"
        ld = f"LINE-DEMUX-PFG-{ordinal}"
        sm = f"SECTION-MUX-PFG-{ordinal}"
        sd = f"SECTION-DEMUX-PFG-{ordinal}"
        source = "323-2051-220 printed pp.204-212,279-284"
        commands: list[_Command] = []
        lm_body = [
            f"set functional-group {lm} type LINE-MUX",
            f"set functional-group {lm} roles BAND-C attrs MIN-FREQ value 191.32500",
            f"set functional-group {lm} roles BAND-C attrs MAX-FREQ value 196.15000",
            f'set functional-group {lm} roles LINEOUTOSC objects "slots {slot} config slots 50 config oscs 1"',
            f'set functional-group {lm} roles LINEOUT objects "slots {slot} config port 53"',
            f'set functional-group {lm} roles BOOSTER objects "slots {slot} config amps Booster"',
            f'set functional-group {lm} roles LINE-OUT-VOA objects "slots {slot} config voa Booster"',
            f"set functional-group {lm} attrs DOWNSTR-PFG-NEIGHBOR "
            f"value {line.neighbor_node}/{line.neighbor_line_demux_pfg}",
        ]
        if line.ospcfib_dbm is not None:
            lm_body.append(
                f"set functional-group {lm} attrs OSCPFIB value "
                f"{self._num(line.ospcfib_dbm)}"
            )
        commands.extend(
            self._transaction(f"{lm} exact PFG", lm_body, source)
        )
        commands.extend(
            self._transaction(
                f"{ld} exact PFG",
                (
                    f"set functional-group {ld} type LINE-DEMUX",
                    f"set functional-group {ld} roles BAND-C attrs MIN-FREQ value 191.32500",
                    f"set functional-group {ld} roles BAND-C attrs MAX-FREQ value 196.15000",
                    f'set functional-group {ld} roles LINEINOSC objects "slots {slot} config slots 50 config oscs 1"',
                    f'set functional-group {ld} roles LINEIN objects "slots {slot} config port 54"',
                    f'set functional-group {ld} roles PREAMP objects "slots {slot} config amps Pre-Amp"',
                    f"set functional-group {ld} attrs UPSTR-PFG-NEIGHBOR "
                    f"value {line.neighbor_node}/{line.neighbor_line_mux_pfg}",
                ),
                source,
            )
        )
        commands.extend(
            self._transaction(
                f"{sm} exact PFG",
                (
                    f"set functional-group {sm} type SECTION-MUX",
                    f"set functional-group {sm} roles BAND-C attrs MIN-FREQ value 191.32500",
                    f"set functional-group {sm} roles BAND-C attrs MAX-FREQ value 196.15000",
                    f'set functional-group {sm} roles ASE-AMP objects "slots {slot} config amps ASE"',
                    f'set functional-group {sm} roles WSS objects "slots {slot} config wss MUX"',
                ),
                source,
            )
        )
        commands.extend(
            self._transaction(
                f"{sd} exact PFG",
                (
                    f"set functional-group {sd} type SECTION-DEMUX",
                    f"set functional-group {sd} roles BAND-C attrs MIN-FREQ value 191.32500",
                    f"set functional-group {sd} roles BAND-C attrs MAX-FREQ value 196.15000",
                    f'set functional-group {sd} roles WSS objects "slots {slot} config wss DEMUX"',
                ),
                source,
            )
        )
        return commands

    def _ccmd16_c_pfg_commands(self, *, slot: int) -> list[_Command]:
        source = "323-2051-220 printed pp.281-284"
        commands: list[_Command] = []
        for direction, amp in (("MUX", "mux"), ("DEMUX", "demux")):
            name = f"CHANNEL-{direction}-PFG-1"
            commands.extend(
                self._transaction(
                    f"{name} exact PFG",
                    (
                        f"set functional-group {name} type CHANNEL-{direction}",
                        f"set functional-group {name} roles BAND-C attrs MIN-FREQ value 191.27500",
                        f"set functional-group {name} roles BAND-C attrs MAX-FREQ value 196.15000",
                        f'set functional-group {name} roles CHANNEL-AMP objects "slots {slot} config amps {amp}"',
                    ),
                    source,
                )
            )
        return commands

    def _ccmd8x24_c_pfg_commands(self, *, slot: int) -> list[_Command]:
        source = "323-2051-220 printed pp.208-209"
        mux = [
            "set functional-group CHANNEL-MUX-PFG-1 type CHANNEL-MUX",
            "set functional-group CHANNEL-MUX-PFG-1 roles BAND-C attrs MIN-FREQ value 191.27500",
            "set functional-group CHANNEL-MUX-PFG-1 roles BAND-C attrs MAX-FREQ value 196.15000",
        ]
        demux = [
            "set functional-group CHANNEL-DEMUX-PFG-1 type CHANNEL-DEMUX",
            "set functional-group CHANNEL-DEMUX-PFG-1 roles BAND-C attrs MIN-FREQ value 191.27500",
            "set functional-group CHANNEL-DEMUX-PFG-1 roles BAND-C attrs MAX-FREQ value 196.15000",
        ]
        amp_names = (
            "113 101-6",
            "112 101-5",
            "111 101-4",
            "110 101-3",
            "117 102-6",
            "116 102-5",
            "115 102-4",
            "114 102-3",
        )
        for degree in range(1, 9):
            mux.append(
                "set functional-group CHANNEL-MUX-PFG-1 roles WSS objects "
                f'"slots {slot} config wss MUX{degree}" attrs DEGREE value {degree}'
            )
            mux.append(
                "set functional-group CHANNEL-MUX-PFG-1 roles CHANNEL-AMP "
                f'objects "slots {slot} config amps \'{amp_names[degree - 1]}\'" '
                f"attrs DEGREE value {degree}"
            )
            demux.append(
                "set functional-group CHANNEL-DEMUX-PFG-1 roles WSS objects "
                f'"slots {slot} config wss DEMUX{degree}" attrs DEGREE value {degree}'
            )
        return [
            *self._transaction("CHANNEL-MUX-PFG-1 exact PFG", mux, source),
            *self._transaction("CHANNEL-DEMUX-PFG-1 exact PFG", demux, source),
        ]

    def _ila_pfg_commands(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        assert request.line_2 is not None
        source = (
            "323-2051-220 printed pp.40-42,447-448; "
            "323-2051-318 printed pp.222-223"
        )
        commands: list[_Command] = []
        first_output_slot, first_output_port = profile.line_outputs[0]
        second_output_slot, second_output_port = profile.line_outputs[1]
        first_input_slot, first_input_port = profile.line_inputs[0]
        second_input_slot, second_input_port = profile.line_inputs[1]
        directions = (
            (
                "PFG-1-to-2",
                "Line-1-to-Line-2",
                request.line_1,
                request.line_2,
                first_input_slot,
                first_input_port,
                first_output_slot,
                first_output_port,
                50,
                60,
                False,
            ),
            (
                "PFG-2-to-1",
                "Line-2-to-Line-1",
                request.line_2,
                request.line_1,
                second_input_slot,
                second_input_port,
                second_output_slot,
                second_output_port,
                60,
                50,
                profile.supports_raman,
            ),
        )
        for (
            name,
            object_name,
            downstream_line,
            upstream_line,
            linein_slot,
            linein_port,
            lineout_slot,
            lineout_port,
            oscin,
            oscout,
            has_raman,
        ) in directions:
            body = [
                f"set functional-group {name} type LINE-AMP",
                f"set functional-group {name} roles BAND-C attrs MIN-FREQ value 191.27500",
                f"set functional-group {name} roles BAND-C attrs MAX-FREQ value 196.15000",
                f"set functional-group {name} roles BAND-L attrs MIN-FREQ value 186.05000",
                f"set functional-group {name} roles BAND-L attrs MAX-FREQ value 190.87500",
                f'set functional-group {name} roles LINEIN objects "slots {linein_slot} config port {linein_port}"',
                f'set functional-group {name} roles LINEOUT objects "slots {lineout_slot} config port {lineout_port}"',
                f'set functional-group {name} roles LINEINOSC objects "slots 1 config slots {oscin} config oscs 1"',
                f'set functional-group {name} roles LINEOUTOSC objects "slots 1 config slots {oscout} config oscs 1"',
                f'set functional-group {name} roles BOOSTER objects "slots 1 config dgff {object_name}-Cband fac amps {object_name}-Cband" attrs BAND value BAND-C',
                f'set functional-group {name} roles BOOSTER objects "slots 1 config dgff {object_name}-Lband fac amps {object_name}-Lband" attrs BAND value BAND-L',
                f'set functional-group {name} roles PREAMP objects "slots 1 config dgff {object_name}-Cband fac amps {object_name}-Cband" attrs BAND value BAND-C',
                f'set functional-group {name} roles PREAMP objects "slots 1 config dgff {object_name}-Lband fac amps {object_name}-Lband" attrs BAND value BAND-L',
                f'set functional-group {name} roles LINE-OUT-VOA objects "slots 1 config voa {object_name}-Cband" attrs BAND value BAND-C',
                f'set functional-group {name} roles LINE-OUT-VOA objects "slots 1 config voa {object_name}-Lband" attrs BAND value BAND-L',
                f'set functional-group {name} roles DGFF objects "slots 1 config dgff {object_name}-Cband" attrs BAND value BAND-C',
                f'set functional-group {name} roles DGFF objects "slots 1 config dgff {object_name}-Lband" attrs BAND value BAND-L',
            ]
            if has_raman:
                body.append(
                    f'set functional-group {name} roles RAMAN objects '
                    f'"slots 4 config ramans LINE-IN"'
                )
            body.extend(
                (
                    f"set functional-group {name} attrs DOWNSTR-PFG-NEIGHBOR "
                    f"value {downstream_line.neighbor_node}/"
                    f"{downstream_line.neighbor_line_demux_pfg}",
                    f"set functional-group {name} attrs UPSTR-PFG-NEIGHBOR "
                    f"value {upstream_line.neighbor_node}/"
                    f"{upstream_line.neighbor_line_mux_pfg}",
                )
            )
            if downstream_line.ospcfib_dbm is not None:
                body.append(
                    f"set functional-group {name} attrs OSCPFIB value "
                    f"{self._num(downstream_line.ospcfib_dbm)}"
                )
            commands.extend(
                self._transaction(f"{name} exact PFG", body, source)
            )
        return commands

    def _ila_sco_disable_commands(self) -> list[_Command]:
        return self._transaction(
            "DLE SCO safety hold before external links",
            (
                "set sco PFG-1-to-2 config admin-state Disabled",
                "set sco PFG-2-to-1 config admin-state Disabled",
            ),
            "323-2051-318 printed p.123; "
            "323-2051-220 printed p.357",
        )

    def _link_commands(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
    ) -> list[_Command]:
        body: list[str] = []
        request_lines = (
            (request.line_1,)
            if request.line_2 is None
            else (request.line_1, request.line_2)
        )
        for line, (slot, port) in zip(
            request_lines,
            profile.line_outputs,
            strict=True,
        ):
            body.append(
                f"create link {line.link_name} "
                f'from "slots {slot} config port {port}" '
                "link-type line-fiber "
                f'fiber-type "{line.fiber_type}" '
                f"expected-line-fiber-loss {self._num(line.expected_loss_db)} "
                f"repair-margin {self._num(line.repair_margin_db)} "
                "input-patch-panel-loss "
                f"{self._num(line.input_patch_loss_db)} "
                "output-patch-panel-loss "
                f"{self._num(line.output_patch_loss_db)} "
                "high-fiber-loss-minor-threshold "
                f"{self._num(line.high_loss_minor_threshold_db)}"
            )
        if profile.application == "cda_rla12_c":
            body.extend(
                (
                    'create link LC-LINK-1 from "slots 1 config port 21" '
                    'to "slots 5 config port 102" link-type fiber',
                    'create link LC-LINK-2 from "slots 5 config port 101" '
                    'to "slots 1 config port 22" link-type fiber',
                    'create link LC-LINK-3 from "slots 1 config port 43" '
                    'to "slots 3 config port 24" link-type fiber',
                    'create link LC-LINK-4 from "slots 3 config port 23" '
                    'to "slots 1 config port 44" link-type fiber',
                    'create link RLA-3-44 from "slots 3 config port 44" '
                    "link-type logical",
                )
            )
        elif profile.application == "cdc_roadm_rla32_c":
            body.extend(self._roadm_local_links())
        elif profile.application == "roadm_rla12_cl_lru12_core":
            body.extend(
                (
                    'create link LRU3-LINEIN from '
                    '"slots 1 config port 11" to '
                    '"slots 3 config port 52" link-type fiber',
                    'create link LRU3-LINEOUT from '
                    '"slots 3 config port 51" to '
                    '"slots 1 config port 12" link-type fiber',
                    'create link LRU3-MON from '
                    '"slots 1 config port 10" to '
                    '"slots 3 config port 50" link-type fiber',
                )
            )
            if profile.supports_raman:
                body.extend(
                    (
                        'create link RLA1-SRA6 from '
                        '"slots 1 config port 53" to '
                        '"slots 6 config port 4" link-type fiber',
                        'create link SRA6-RLA1 from '
                        '"slots 6 config port 3" to '
                        '"slots 1 config port 54" link-type fiber',
                    )
                )
        elif profile.application == "ila_dle_cl" and profile.supports_raman:
            body.extend(
                (
                    'create link RLA1-SRA4 from '
                    '"slots 1 config port 63" to '
                    '"slots 4 config port 4" link-type fiber',
                    'create link SRA4-RLA1 from '
                    '"slots 4 config port 3" to '
                    '"slots 1 config port 64" link-type fiber',
                )
            )
        source = (
            "323-2051-220 printed pp.129-130,171-179,209-212,284-289,"
            "442-443; 323-2051-318 printed pp.185-187,234-235; "
            "NTRN10WA printed pp.576,620-623"
        )
        if profile.application == "roadm_rla12_cl_lru12_core":
            source += (
                "; Ciena RLS C+L CLI Config v3.5LR.xlsx "
                "ROADM_A/ROADM_Z B162:B164 (local link object names)"
            )
        return self._transaction(
            "External and exact local links",
            body,
            source,
        )

    @staticmethod
    def _roadm_local_links() -> tuple[str, ...]:
        commands = [
            'create link MPO-LINK-1 from "slots 1 config port 21" '
            'to "slots 71 config port 1" link-type mpo-cable',
            'create link MPO-LINK-2 from "slots 3 config port 21" '
            'to "slots 71 config port 2" link-type mpo-cable',
            'create link MPO-LINK-3 from "slots 1 config port 28" '
            'to "slots 72 config port 1" link-type mpo-cable',
            'create link MPO-LINK-4 from "slots 3 config port 28" '
            'to "slots 72 config port 2" link-type mpo-cable',
            'create link MPO-LINK-5 from "slots 5 config port 101" '
            'to "slots 72 config port 5" link-type mpo-cable',
        ]
        same_qg = (
            (1, "1/21-6", "1/21-7", True),
            (2, "1/21-5", "3/21-7", False),
            (3, "1/21-4", "1/21-9", True),
            (4, "1/21-3", "1/21-10", True),
            (5, "3/21-6", "1/21-8", False),
            (6, "3/21-5", "3/21-8", True),
            (7, "3/21-4", "3/21-9", True),
            (8, "3/21-3", "3/21-10", True),
        )
        cross_qg = (
            (1, "1/28-3", "1/28-10", True),
            (2, "1/28-4", "1/28-9", True),
            (3, "1/28-5", "1/28-8", True),
            (4, "1/28-6", "5/101-7", False),
            (5, "3/28-3", "3/28-10", True),
            (6, "3/28-4", "3/28-9", True),
            (7, "3/28-5", "3/28-8", True),
            (8, "3/28-6", "5/101-8", False),
            (9, "5/101-6", "1/28-7", False),
            (10, "5/101-5", "3/28-7", False),
            (11, "5/101-4", "5/101-9", True),
            (12, "5/101-3", "5/101-10", True),
        )
        for prefix, rows in (
            ("QG1-to-QG1-LOGICAL-LINK", same_qg),
            ("QG1-to-QG2-LOGICAL-LINK", cross_qg),
        ):
            for number, source, destination, insertion_loss in rows:
                source_slot, source_port = source.split("/")
                dest_slot, dest_port = destination.split("/")
                command = (
                    f"create link {prefix}-{number} "
                    f'from "slots {source_slot} config port {source_port}" '
                    f'to "slots {dest_slot} config port {dest_port}" '
                    "link-type logical high-fiber-loss-minor-threshold 3"
                )
                if insertion_loss:
                    command += " insertion-loss 1.6"
                commands.append(command)
        return tuple(commands)

    def _roadm_cv_commands(self) -> list[_Command]:
        body: list[str] = []
        same_pairs = (
            (1, "21-7", 1, "21-6"),
            (3, "21-7", 1, "21-5"),
            (1, "21-9", 1, "21-4"),
            (1, "21-10", 1, "21-3"),
            (1, "21-8", 3, "21-6"),
            (3, "21-8", 3, "21-5"),
            (3, "21-9", 3, "21-4"),
            (3, "21-10", 3, "21-3"),
        )
        cross_pairs = (
            (1, "28-10", 1, "28-3"),
            (1, "28-9", 1, "28-4"),
            (1, "28-8", 1, "28-5"),
            (5, "101-7", 1, "28-6"),
            (3, "28-10", 3, "28-3"),
            (3, "28-9", 3, "28-4"),
            (3, "28-8", 3, "28-5"),
            (5, "101-8", 3, "28-6"),
            (1, "28-7", 5, "101-6"),
            (3, "28-7", 5, "101-5"),
            (5, "101-9", 5, "101-4"),
            (5, "101-10", 5, "101-3"),
        )
        for rx_slot, rx_port, tx_slot, tx_port in (*same_pairs, *cross_pairs):
            body.append(
                f"set slots {rx_slot} config cv ports rx {rx_port} "
                f'expected-port-id "slot.{tx_slot}_port.{tx_port}"'
            )
        for slot, base in ((1, 21), (3, 21)):
            for tx, rx in ((3, 10), (4, 9), (5, 8), (6, 7)):
                body.extend(
                    (
                        f'set slots {slot} config cv config tx port-sequence "port {base}-{tx}"',
                        f'set slots {slot} config cv config rx port-sequence "port {base}-{rx}"',
                    )
                )
        for slot, base in ((1, 28), (3, 28), (5, 101)):
            for rx, tx in ((7, 6), (8, 5), (9, 4), (10, 3)):
                body.extend(
                    (
                        f'set slots {slot} config cv config rx port-sequence "port {base}-{rx}"',
                        f'set slots {slot} config cv config tx port-sequence "port {base}-{tx}"',
                    )
                )
        return self._transaction(
            "Mandatory fixed logical-link connection validation",
            body,
            "323-2051-220 printed pp.171-179,210-212",
        )

    @staticmethod
    def _num(value: int | float | Decimal) -> str:
        number = Decimal(str(value))
        if number == 0:
            return "0"
        text = format(number, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def _render_cli(commands: Sequence[_Command]) -> str:
        return "\n".join(command.text for command in commands).rstrip() + "\n"

    def _render_annotated(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
        commands: Sequence[_Command],
        issues: Sequence[ValidationIssue],
    ) -> str:
        controls = r40_deployment_controls(profile.provider_id)
        lines = [
            f"# {READINESS_LABEL}",
            f"# Provider: {profile.provider_id}",
            f"# Layout: {profile.display_name}",
            f"# Target: {request.software_release} / {request.target_software_build}",
            f"# Provider line records: {len(profile.line_outputs)}",
            (
                "# Fixed physical degree 1 faces route side "
                f"{request.line_1_route_side}; its mux/demux pair carries "
                "opposing traffic flows"
                if profile.line_semantics == "bidirectional_degree"
                else "# Fixed amplifier path 1 line-output faces route side "
                f"{request.line_1_route_side}"
            ),
            "# RAMAN/SRA supported by this provider: "
            f"{'yes' if profile.supports_raman else 'no'}",
            (
                "# COLAN state: prohibited for this ILA; no COLAN commands emitted."
                if profile.colan_policy == COLAN_PROHIBITED
                else (
                    "# COLAN state: configured; reviewed COLAN commands emitted."
                    if request.management.enabled
                    else "# COLAN state: deferred for factory staging; no COLAN "
                    "commands emitted."
                )
            ),
            "# Never paste an entire file blindly. Run one transaction at a "
            "time and stop before commit if validate reports any error.",
            f"# Fixed BOM: {profile.bom_note}",
            "# Licensed features and runtime calibration are intentionally deferred.",
            "# NTP is customer-managed and intentionally not requested or emitted.",
            "",
            "# Automatic provider deployment controls",
            "# These controls are always carried with this provider candidate.",
            "# ACTIVE does not claim that ATLAS verified a physical condition.",
            *[
                (
                    f"# - {control.control_id} [ACTIVE/{control.stage}]: "
                    f"{control.label}"
                )
                for control in controls
            ],
            "",
        ]
        prior: tuple[str, str] | None = None
        for command in commands:
            marker = (command.section, command.source)
            if marker != prior:
                if prior is not None:
                    lines.append("")
                lines.extend(
                    (
                        f"# --- {command.section} ---",
                        f"# Source: {command.source}",
                    )
                )
                prior = marker
            lines.append(command.text)
        warnings = tuple(
            issue for issue in issues if issue.severity == "warning"
        )
        if warnings:
            lines.extend(("", "# Validation warnings"))
            lines.extend(
                f"# [{issue.code}] {issue.message}" for issue in warnings
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_report(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
        commands: Sequence[_Command],
        issues: Sequence[ValidationIssue],
    ) -> str:
        controls = r40_deployment_controls(profile.provider_id)
        warnings = tuple(
            issue for issue in issues if issue.severity == "warning"
        )
        lines = [
            "ATLAS Ciena RLS R4.0 Exact Provider Validation",
            "=" * 50,
            "OFFLINE RESULT: VALID",
            "DEPLOYMENT RESULT: ON-BOX VALIDATE REQUIRED",
            "",
            f"Provider ID: {profile.provider_id}",
            f"Layout: {profile.display_name}",
            f"Route role: {request.profile}",
            f"Shelf: {request.shelf_name}",
            f"Target release: {request.software_release}",
            f"Target build/schema: {request.target_software_build}",
            f"Chassis: {profile.chassis_family} / {profile.chassis_pec}",
            f"Hardware profile: {profile.hardware_profile}",
            f"Provider line records: {len(profile.line_outputs)}",
            "Local line endpoints: "
            + ", ".join(
                f"{out_slot}/{out_port} out + {in_slot}/{in_port} in"
                for (out_slot, out_port), (in_slot, in_port) in zip(
                    profile.line_outputs,
                    profile.line_inputs,
                    strict=True,
                )
            ),
            "RAMAN/SRA supported by provider: "
            f"{'yes' if profile.supports_raman else 'no'}",
            f"COLAN policy: {profile.colan_policy}",
            (
                "COLAN state: prohibited (ILA; no COLAN commands emitted)"
                if profile.colan_policy == COLAN_PROHIBITED
                else (
                    "COLAN state: configured (reviewed COLAN commands emitted)"
                    if request.management.enabled
                    else "COLAN state: deferred for factory staging "
                    "(no COLAN commands emitted)"
                )
            ),
            "NTP policy: customer-managed; no ATLAS NTP commands",
            (
                "Fixed physical degree 1 route side: "
                f"{request.line_1_route_side} (bidirectional mux/demux degree)"
                if profile.line_semantics == "bidirectional_degree"
                else "Fixed amplifier path 1 line-output route side: "
                f"{request.line_1_route_side}"
            ),
            f"Command lines: {len(commands)}",
            f"Warnings: {len(warnings)}",
            "",
            "Fixed scope",
            "-----------",
            profile.bom_note,
            "",
            "Warnings",
            "--------",
        ]
        for issue in warnings:
            lines.append(f"[{issue.code}] {issue.field}: {issue.message}")
            if issue.source:
                lines.append(f"  Source: {issue.source}")
        lines.extend(
            (
                "",
                "Mandatory deployment review",
                "---------------------------",
                *[
                    (
                        f"{index}. [{control.control_id}] {control.label} "
                        f"({control.stage})."
                    )
                    for index, control in enumerate(controls, start=1)
                ],
                (
                    f"{len(controls) + 1}. Verify every local fiber and fixed "
                    "CV mapping against the packout."
                ),
                (
                    f"{len(controls) + 2}. Verify all PFG neighbor identities "
                    "and discovered far-end endpoints."
                ),
                (
                    f"{len(controls) + 3}. Apply one batch transaction at a "
                    "time on the matching R4.0 build."
                ),
                (
                    f"{len(controls) + 4}. Capture a successful validate "
                    "result before commit; stop on any error."
                ),
                "ATLAS carries the listed controls automatically but does not "
                "assert that physical verification has occurred.",
                "",
                "Controlled sources",
                "------------------",
                *[f"- {source}" for source in SOURCE_REFERENCES],
            )
        )
        return "\n".join(lines).rstrip() + "\n"

    def _manifest(
        self,
        request: R40ExactRequest,
        profile: R40ProviderProfile,
        commands: Sequence[_Command],
        issues: Sequence[ValidationIssue],
        cli_text: str,
    ) -> Mapping[str, Any]:
        controls = r40_deployment_controls(profile.provider_id)
        colan_state = (
            "prohibited"
            if profile.colan_policy == COLAN_PROHIBITED
            else ("configured" if request.management.enabled else "deferred")
        )
        colan_commands_emitted = bool(request.management.enabled)
        return MappingProxyType(
            {
                "schema": "atlas.ciena.rls.config-artifact",
                "schema_version": "2.0",
                "generator": "R40ExactConfigGenerator",
                "generator_version": GENERATOR_VERSION,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "release": SUPPORTED_RELEASE,
                "target_software_build": request.target_software_build,
                "provider_id": profile.provider_id,
                "route_profile": request.profile,
                "readiness_state": READINESS_STATE,
                "readiness_label": READINESS_LABEL,
                "deployment_approved": False,
                "on_box_validate_required": True,
                "partial_or_legacy_template_output": False,
                "licensed_features_emitted": [],
                "supports_raman": profile.supports_raman,
                "colan_policy": profile.colan_policy,
                "colan_state": colan_state,
                "colan_configured": colan_state == "configured",
                "colan_commands_emitted": colan_commands_emitted,
                "colan_deferred_for_factory_staging": (
                    colan_state == "deferred"
                ),
                "ntp_managed_by_customer": True,
                "ntp_commands_emitted": False,
                "deployment_controls": [
                    {
                        "id": control.control_id,
                        "legacy_field": control.legacy_field,
                        "title": control.label,
                        "applicability": control.applicability,
                        "stage": control.stage,
                        "instruction": control.instruction,
                        "source": control.source,
                        "mode": "automatic_background_advisory",
                        "active": True,
                        "status": "active_unverified",
                        "physical_verification_status": "not_asserted",
                        "legacy_confirmation_value": getattr(
                            request, control.legacy_field
                        ),
                    }
                    for control in controls
                ],
                "deployment_control_count": len(controls),
                "legacy_confirmations_block_offline_generation": False,
                "fixed_bom_note": profile.bom_note,
                "line_record_count": len(profile.line_outputs),
                "line_outputs": [
                    {"slot": slot, "port": port}
                    for slot, port in profile.line_outputs
                ],
                "line_inputs": [
                    {"slot": slot, "port": port}
                    for slot, port in profile.line_inputs
                ],
                "line_pfg_names": [
                    {"mux": mux, "demux": demux}
                    for mux, demux in profile.line_pfg_names
                ],
                "line_link_names": list(profile.line_link_names),
                "request": asdict(request),
                "command_count": len(commands),
                "warning_count": sum(
                    issue.severity == "warning" for issue in issues
                ),
                "cli_sha256": hashlib.sha256(
                    cli_text.encode("utf-8")
                ).hexdigest(),
                "source_references": list(SOURCE_REFERENCES),
                "controlled_source_sha256": dict(CONTROLLED_SOURCE_HASHES),
                "contains_credentials": False,
                "contains_license_secrets": False,
            }
        )


def encode_r40_exact_payload(
    request: R40ExactRequest,
) -> dict[str, object]:
    """Encode a reviewed R4.0 request in a strict versioned envelope."""

    if not isinstance(request, R40ExactRequest):
        raise TypeError("request must be an R40ExactRequest")
    encoded: dict[str, object] = {
        "schema_id": R40_PAYLOAD_SCHEMA_ID,
        "schema_version": R40_PAYLOAD_SCHEMA_VERSION,
        "request": asdict(request),
    }
    json.dumps(encoded, allow_nan=False)
    decode_r40_exact_payload(encoded)
    return encoded


def decode_r40_exact_payload(
    payload: Mapping[str, object],
) -> R40ExactRequest:
    """Strictly decode an exact R4.0 request without inferring defaults."""

    if not isinstance(payload, Mapping) or not payload:
        raise ValueError(
            "R4.0 exact provider payload is missing or is not a JSON object."
        )
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("R4.0 payload field names must be strings.")
    envelope = {"schema_id", "schema_version", "request"}
    unknown = sorted(set(payload) - envelope)
    missing = sorted(envelope - set(payload))
    if unknown:
        raise ValueError(
            "R4.0 payload contains unknown fields: " + ", ".join(unknown) + "."
        )
    if missing:
        raise ValueError(
            "R4.0 payload is missing fields: " + ", ".join(missing) + "."
        )
    if payload.get("schema_id") != R40_PAYLOAD_SCHEMA_ID:
        raise ValueError("Unsupported R4.0 exact payload schema_id.")
    if payload.get("schema_version") != R40_PAYLOAD_SCHEMA_VERSION:
        raise ValueError("Unsupported R4.0 exact payload schema_version.")
    raw = payload.get("request")
    if not isinstance(raw, Mapping):
        raise ValueError("R4.0 payload request must be a JSON object.")
    if any(not isinstance(key, str) for key in raw):
        raise ValueError("R4.0 request field names must be strings.")
    allowed = {item.name for item in fields(R40ExactRequest)}
    required = {
        item.name
        for item in fields(R40ExactRequest)
        if item.default is MISSING and item.default_factory is MISSING
    }
    unknown_request = sorted(set(raw) - allowed)
    missing_request = sorted(required - set(raw))
    if unknown_request:
        raise ValueError(
            "R4.0 request contains unknown fields: "
            + ", ".join(unknown_request)
            + "."
        )
    if missing_request:
        raise ValueError(
            "R4.0 request is missing fields: "
            + ", ".join(missing_request)
            + "."
        )
    values = dict(raw)
    string_fields = {
        "provider_id",
        "profile",
        "software_release",
        "target_software_build",
        "chassis_family",
        "chassis_pec",
        "hardware_profile",
        "shelf_name",
        "site_name",
        "member_name",
        "hostname",
        "frame_identification_code",
        "loopback_ip",
        "ospf_area",
        "line_1_route_side",
        "shelf_label",
        "site_description",
        "site_address",
        "osc_profile",
    }
    integer_fields = {"site_id", "bay_number", "physical_shelf"}
    boolean_fields = {
        "installed_inventory_confirmed",
        "planner_runtime_mop_confirmed",
        "target_build_confirmed",
        "greenfield_fibers_disconnected_confirmed",
        "calibration_feature_inactive_confirmed",
        "cfim_unused_ports_terminated_confirmed",
    }
    problems: list[str] = []
    for name in string_fields & set(values):
        if not isinstance(values[name], str):
            problems.append(f"request.{name} must be text")
    for name in integer_fields & set(values):
        if type(values[name]) is not int:
            problems.append(f"request.{name} must be an integer")
    for name in boolean_fields & set(values):
        if type(values[name]) is not bool:
            problems.append(f"request.{name} must be Boolean")
    values["management"] = _decode_management(
        values.get("management"), problems
    )
    values["line_1"] = _decode_line(
        values.get("line_1"), "line_1", problems
    )
    if values.get("line_2") is None:
        values["line_2"] = None
    else:
        values["line_2"] = _decode_line(
            values.get("line_2"), "line_2", problems
        )
    if problems:
        raise ValueError("; ".join(problems) + ".")
    try:
        return R40ExactRequest(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "R4.0 exact request could not be constructed."
        ) from exc


def _decode_management(
    raw: object, problems: list[str]
) -> ManagementInterface | None:
    initial_problem_count = len(problems)
    if not isinstance(raw, Mapping):
        problems.append("request.management must be a JSON object")
        return None
    allowed = {item.name for item in fields(ManagementInterface)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        problems.append(
            "request.management contains unknown fields: " + ", ".join(unknown)
        )
        return None
    values = dict(raw)
    for name in {"name", "routing_mode", "ip_address", "gateway"} & set(values):
        if not isinstance(values[name], str):
            problems.append(f"request.management.{name} must be text")
    for name in {"ospf_metric", "static_metric"} & set(values):
        if type(values[name]) is not int:
            problems.append(f"request.management.{name} must be an integer")
    if "enabled" in values and type(values["enabled"]) is not bool:
        problems.append("request.management.enabled must be Boolean")
    if "prefix_length" in values and (
        values["prefix_length"] is not None
        and type(values["prefix_length"]) is not int
    ):
        problems.append(
            "request.management.prefix_length must be an integer or null"
        )
    if len(problems) != initial_problem_count:
        return None
    try:
        return ManagementInterface(**values)
    except (TypeError, ValueError):
        problems.append("request.management is invalid")
        return None


def _decode_line(
    raw: object, name: str, problems: list[str]
) -> R40LinePath | None:
    initial_problem_count = len(problems)
    if not isinstance(raw, Mapping):
        problems.append(f"request.{name} must be a JSON object")
        return None
    allowed = {item.name for item in fields(R40LinePath)}
    required = {
        item.name
        for item in fields(R40LinePath)
        if item.default is MISSING and item.default_factory is MISSING
    }
    unknown = sorted(set(raw) - allowed)
    missing = sorted(required - set(raw))
    if unknown:
        problems.append(
            f"request.{name} contains unknown fields: " + ", ".join(unknown)
        )
    if missing:
        problems.append(
            f"request.{name} is missing fields: " + ", ".join(missing)
        )
    values = dict(raw)
    for field_name in {
        "link_name",
        "neighbor_node",
        "neighbor_line_mux_pfg",
        "neighbor_line_demux_pfg",
        "fiber_type",
    } & set(values):
        if not isinstance(values[field_name], str):
            problems.append(f"request.{name}.{field_name} must be text")
    for field_name in {
        "expected_loss_db",
        "input_patch_loss_db",
        "output_patch_loss_db",
        "repair_margin_db",
        "high_loss_minor_threshold_db",
        "ospcfib_dbm",
    } & set(values):
        value = values[field_name]
        if field_name == "ospcfib_dbm" and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                f"request.{name}.{field_name} must be a number"
            )
    if unknown or missing or len(problems) != initial_problem_count:
        return None
    try:
        return R40LinePath(**values)
    except (TypeError, ValueError):
        problems.append(f"request.{name} is invalid")
        return None


__all__ = [
    "COLAN_PROHIBITED",
    "COLAN_TERMINAL_OPTIONAL",
    "COLAN_TERMINAL_REQUIRED",
    "DEFAULT_R40_TARGET_BUILD_SCHEMA",
    "GENERATOR_VERSION",
    "R40_CDA_RLA12_C_2DEG_NO_SRA",
    "R40_CDC_ROADM_RLA32_C_2DEG_CCMD8X24_NO_SRA",
    "R40_CL_ROADM_RLA12_LRU12_1DEG_NO_SRA",
    "R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6",
    "R40_R2_CL_DLE_S1_NO_SRA",
    "R40_R2_CL_DLE_S1_SRA4",
    "R40_PAYLOAD_SCHEMA_ID",
    "R40_PAYLOAD_SCHEMA_VERSION",
    "R40_PROVIDER_CATALOG",
    "R40_ROUTE_SIDES",
    "R40ConfigArtifact",
    "R40DirectModuleFact",
    "R40DeploymentControl",
    "R40ExactConfigGenerator",
    "R40ExactRequest",
    "R40LinePath",
    "R40ProviderCandidateFacts",
    "R40ProviderCandidateResolution",
    "R40ProviderCandidateStatus",
    "R40ProviderProfile",
    "READINESS_LABEL",
    "READINESS_STATE",
    "SUPPORTED_RELEASE",
    "decode_r40_exact_payload",
    "deployment_controls_for_profile",
    "encode_r40_exact_payload",
    "provider_profiles_for_role",
    "r40_deployment_controls",
    "resolve_r40_provider_candidate",
]
