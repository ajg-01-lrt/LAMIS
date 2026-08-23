# Ciena RLS 4.0 vendor-document and workbook audit

## Outcome

The supplied RLS 4.0 manuals establish the active release boundary:

- RLS 4.0 supports Add/Drop, ILA, ROADM, protected ROADM, and protected and
  unprotected DCI configurations.
- `Add/Drop`, `ILA`, and `ROADM` are site/configuration roles, not physical
  shelf families and not complete CLI-provider identifiers.
- The removed workbook-derived generator could not be promoted by changing its
  release string. Every one of its five profiles had confirmed invalid or
  unsafe output.
- ATLAS now implements four narrow exact RLS 4.0 providers. A generic role
  remains non-ready until an operator explicitly selects one compatible exact
  provider, reviews every input, and stores its versioned request.

This is a fail-closed result. It does not mean other RLS 4.0 arrangements lack
vendor support; it means each arrangement needs its own audited provider.
ATLAS never treats a role name, diagram color, `R2/R4 600mm` label, or legacy
workbook selection as a physical configuration.

## Authoritative release scope

`NTRN10WA`, Planning Guide Issue 3, is the topology catalog. Its printed
pages 9–10 (PDF pages 27–28) identify RLS 4.0 additions including:

- RLA 64x1 iC&L and its TDA configuration;
- RLA 32x1 iC&L and C-Band mixed TDA/CDA ROADMs;
- RLA 32x1 iC&L CDC with C- or L-Band CCMD 8x24;
- CMD24 ROADMs with RLA 12x1 or RLA 32x1;
- protected ROADM with TPS-to-TPS connections; and
- protected DCI using CMD32 and TPS2.

No supplied RLS 4.0 document contains the RLS 4.2 blanket limitation that says
the release must be used only for protected C+L point-to-point DCI.

The Release Notes corroborate active RLS 4.0 ILA, RLA64, mirror-protected
ROADM, and multi-shelf behavior at printed pages 41–47 (PDF pages 49–55).

## Hardware and role terminology

The physical shelf families are:

| Shelf family | Physical description | Shelf PEC |
|---|---|---|
| R2 | Four-slot shelf | `NTK803DA` |
| R4 | Eight-slot shelf | `NTK803FA` |
| R6-300 | Seven-slot shelf | `NTK803PA` |
| R8-300 | Eight-slot shelf | `NTK803QA` |

The Installation Guide identifies the physical R4 eight-slot assembly at
printed page 211 (PDF page 219). The Planning Guide calls R2/R4 the 600-mm
shelves at printed page 410 (PDF page 428). Consequently, diagram text such as
`R4/R2 600mm` is chassis evidence and must not be interpreted as software
Release 4.0 or any other software version.

An RLA is a module, not a shelf. Add/Drop, ILA, and ROADM describe a site's
optical function. A safe discriminator needs:

```text
software release
+ shelf family and shelf PEC
+ site role and topology
+ band
+ every module PEC and slot
+ add/drop structure and degrees
+ protection type and path arrangement
+ exact local/remote ports and neighbor evidence
+ fiber engineering and licensed-feature state
```

## Supported RLS 4.0 configuration families

The Planning Guide documents these families:

| Family | Supported arrangements | Primary Planning Guide evidence |
|---|---|---|
| Fixed add/drop / DCI | CMD24, CMD32, CMD42, CMD48, or CMD64 with the applicable DLM/TLM and TPS/TPS2 design | printed 87–91 / PDF 105–109 |
| CDC | RLA 12x1/32x1 variants with CCMD 8x24 or CCMD 16x24 and applicable FIM/CFIM | printed 92–114 / PDF 110–132 |
| CDA | RLA 12x1/32x1 variants with CCMD16; mixed CDA/CDC and CDA/TDA variants | printed 117–121 / PDF 135–139 |
| Fixed-CMD ROADM | CMD24/42/48/64 Type 2 with the specifically listed RLA combinations | printed 128–134 / PDF 146–152 |
| TDA | RLA 12x1, 32x1, or RLA 64x1 iC&L with the documented transponder/OPS arrangement | printed 134–142 / PDF 152–160 |
| ILA | DLA or DLE, optional SRA, dual-rail, and constrained same-shelf cascaded arrangements | printed 159–163 / PDF 177–181 |
| Protected ROADM | Listed RLA 12x1/32x1, TPS/TPS2, DLA/DLE, optional SRA, and constrained working/protection paths | printed 148–155 / PDF 166–173 |

Two names that look similar are not interchangeable:

- `CCMD 16x24` (`NTK843AA` / `NTK843AE`) is a 16-degree, 24-channel CDC
  module.
- `CCMD16` (`NTK834AA`, `NTK874AA`, or `NTK834AE`) is a 16-channel CDA
  add/drop module.

RLA64 iC&L is explicitly supported for TDA. The reviewed topology tables do
not authorize silently reusing it for every CDC, fixed-CMD, or protected
ROADM role.

## RAMAN/SRA notation and the ELP1–SAT4 source pair

Ciena CLI identifies a physical link endpoint as slot plus port. ZTP/Manual
Commissioning printed page 123 / PDF page 131 defines the identifier as
`<slot #> config port <port #>`. Consequently, the small red `4/5` in the
supplied ELP1–SAT4 route diagram means slot 4, port 5; it does not mean subslot
5 or a pair of module slots.

The same manual establishes the SRA port semantics:

- printed pages 40–42 / PDF pages 48–50 show a RAMAN PFG object on an SRA,
  internal SRA-to-DLE links on SRA ports 3/4, and an external SRA port 5
  facing a remote SRA port 6;
- printed page 133 / PDF page 141 shows an SRA-bookended span using external
  ports 5/6 and documents mixed-fiber handling;
- printed page 435 / PDF page 443 states that an SRA-equipped degree adds the
  RAMAN role and uses SRA line-in port 6 instead of RLA line-in port 54; and
- printed pages 461 and 464–466 / PDF pages 469 and 472–474 require the
  interconnected RLA/DLE/DLA and SRA to share the appropriate shelf and list
  the supported R2/R4 SRA families.

Planning Guide printed page 159 / PDF page 177 adds the topology rule: an SRA
may serve one or both ILA directions, but the span must be bookended with SRAs,
and each SRA must be in the same shelf as the DLE/DLA serving that span.
ZTP/Manual Commissioning printed page 504 / PDF page 512 requires the SRA
go/no-go OTDR result to pass, RAMAN activation-inhibited alarms to be clear,
and RAMAN facilities to be enabled before the relevant calibration state is
accepted.

The customer source uses that grammar consistently on the final span:

- `USXGN1-L8I2` has red `4/5` line-out and `4/6` line-in callouts, identifying
  one SRA in slot 4;
- `USSAT4-L8R3` has red `6/5` line-out and `6/6` line-in callouts, identifying
  the bookending SRA in slot 6; and
- red `3/5` and `3/6` samples below the diagram legend explain the source
  notation and are not another route shelf or installed module.

This interpretation is source-scoped. Red color by itself is not a universal
Ciena data contract and must not be generalized to another customer's
diagram. The matching MOP confirms the association:
`FBN!W30 = "Slot 4"` beside `USXGN1-L8I2`, `FBN!W31 = "Slot 6"` beside
`USSAT4-L8R3`, and `FBN!Z19 = "Spans with RAMAN"`. Its `Fibering!F27:F28`
describes the slot-4 SRA/DLE internal path, while `Fibering!BP32:BP33` and
`Fibering!AL44:AL45` describe slot-6 SRA/RLA paths.

These are three different data layers and must remain separate:

1. the paired red values are directional span-endpoint slot/port evidence;
2. `Slot 4` and `Slot 6` are derived shelf-level FBN display labels; and
3. executable SRA input requires a structured reviewed record containing SRA
   module kind, slot, ports 5/6, route side, raw evidence, and—only when
   explicitly supplied—the module PEC.

`raman_label` can carry layer 2 only. It is not a Boolean or provider switch.
The legacy workbook instead uses per-direction `Raman Needed?` selectors and
unsafe shortcuts: `ILA 1!B16` derives one direction from a 17-dB threshold,
`ILA 1!B99/B121` binds RAMAN roles to slots 3/4, and
`ILA 1!B138:B143` hard-codes local and guessed remote SRA endpoints. Add/Drop
`B111/B124/B174` and ROADM `B121/B134/B159` similarly hard-code slot-6 SRA
ports. These cells document the old workbook's assumptions; they do not
authorize copying them into an exact provider.

## SRA-capable provider determination

The R4.0 material is sufficient to identify the SRA hardware boundary.
Planning Guide printed pages 630 and 634–636 / PDF pages 648 and 652–654
identify `NTK830AA` as the C-band SRA and `NTK830AC` as the C+L SRA, define
ports 3/4 as EXPRESS-OUT/EXPRESS-IN and ports 5/6 as LINE-OUT/LINE-IN, and
allow one logical SRA slot in R2 positions 1–4 or R4 positions 1–8. Slot and
role alone therefore cannot distinguish the two band-specific PECs.

The route's known-good field configuration,
`A13 Ciena RLS Field Configs ELP1-SAT4 v1.xlsx`, and the operator's explicit
SAT inventory confirmation close the previously documented core-layout gap
for this specific pair. They do not prove live shelf state or authorize a
broader ROADM layout:

| Source shelf | Audited determination | Production status |
|---|---|---|
| `USXGN1-L8I2`, ILA, SRA slot 4 | R2 `NTK803DA`; C+L DLE `NTK850DC` anchored in slot 1; `NTK591VQ` OSCs in 1/50 and 1/60; C+L SRA `NTK830AC` in slot 4 on the represented PFG-1-to-2 line-output / PFG-2-to-1 line-input side. The fixed internal map is DLE 1/63 → SRA 4/4 and SRA 4/3 → DLE 1/64, with external 4/5 and 4/6. | Registered as `R40_R2_CL_DLE_S1_SRA4`. It is an exact core-only pre-calibration provider paired to the SAT endpoint; no second SRA, dual rail, cascade, DLA substitution, protection, or COLAN. |
| `USSAT4-L8R3`, ROADM, SRA slot 6 | Operator-confirmed installed core inventory: R4 `NTK803FA`; C+L RLA12 `NTK852BC` anchored in slot 1; `NTK591VQ` OSC in 1/50; LRU12 `NTK852NA` anchored in slot 3; C+L SRA `NTK830AC` in slot 6. The fixed internal map is RLA12 1/53 → SRA 6/4 and SRA 6/3 → RLA12 1/54, with external 6/5 and 6/6. | Registered as `R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6`. This exact core-only provider excludes unrelated add/drop/client/CCMD inventory, remote or express WSS peer half-links, additional degrees, and protection. |

The slot-4 ILA topology is supported by ZTP/Manual Commissioning printed
pages 366–370 / PDF pages 374–378 and the DLE/SRA examples on printed pages
40–42 / PDF pages 48–50. The known-good field configuration supplies the
matching `NTK850DC`, `NTK830AC`, and slot-4 route intent. The operator-confirmed
SAT inventory supplies the corresponding narrow terminal-core boundary.
Neither input is on-box telemetry: the installer must compare both physical
packouts, slots, PECs, and cabling before applying a candidate.

ZTP/Manual Commissioning printed pages 244–248 / PDF pages 252–256 explicitly
show CCMD8x24 `NTK843BA` in logical slot 5 and a C-band SRA `NTK830AA` in
logical slot 6. Therefore a two-slot-high CCMD at logical slot 5 does **not**
consume logical slot 6; the earlier adjacent-logical-slot interpretation was
incorrect. That example cannot be copied as the customer C+L provider because
its SRA is C-band and its surrounding BOM differs. The separate exact C+L CDC
example on printed pages 250–256 / PDF pages 258–264 uses RLA32 C+L, SRA
slots 5/7, and CCMD8x24 in slot 6, which further demonstrates why
`ROADM + slot 6` is not a complete discriminator.

Both exact providers remain a paired-span contract. Planning Guide printed page
159 / PDF page 177 requires SRA bookending and same-shelf colocation with the
serving DLE/DLA; the CDC rules add the corresponding RLA constraint and a
single patch cord no longer than 10 m. Equipment/Facility printed pages
144–149 / PDF pages 152–157 show that runtime RAMAN data includes more than
fiber type, including target gain and safety thresholds. ATLAS must not infer
those values from the route diagram or its visible span loss. ZTP/Manual
Commissioning printed page 504 / PDF page 512 remains the pre-calibration
go/no-go and alarm gate.

ATLAS enforces that paired vendor boundary through an order-independent staged
review transaction. The first locally valid endpoint may be retained as
**Staged — paired SRA peer review pending** only when the absent compatible
peer payload is its sole remaining topology issue. That checkpoint is not a
vendor validation result or deployment authorization: exact route readiness,
CLI publication, and Route Bundle export remain blocked. When the second
endpoint is applied, both payloads are checked reciprocally in one candidate
route snapshot and are promoted together only if every fixed provider,
slot/port, route-side, neighbor, PFG/link, fiber, and endpoint-loss
relationship passes.
An absent peer is considered compatible only after its shelf facts are
reviewed and establish exact `RLS R4.0`, an eligible audited role/band
provider, and matching accepted SRA slot evidence.

This staging rule does not weaken the audited topology gate. Wrong or
conflicting SRA evidence, an incompatible/no-SRA provider, a nonreciprocal
neighbor or PFG map, a stale snapshot, or any other local identity/topology
failure rejects Apply without partially changing the pair. If the second
endpoint fails, the first remains pending. Editing either provider, its fixed
direction assignment, or a bound paired-path/topology value invalidates the
reciprocal result and requires both endpoints to pass same-snapshot review
again. The endpoint review order has no effect on these checks.

## Implemented exact providers

The production registry contains only these exact layouts:

| Provider ID | Fixed audited layout | Readiness |
|---|---|---|
| `r40_cda_rla12_c_2deg_no_sra` | R4 `NTK803FA`; C-band RLA12 `NTK852BA` in anchors 1 and 3; OSC `NTK591VN` in 1/50 and 3/50; local CCMD16-C `NTK834AA` in slot 5; two degrees; no SRA or protection | Documented pre-calibration candidate |
| `r40_cdc_roadm_rla32_c_2deg_ccmd8x24_no_sra` | R4 `NTK803FA`; C-band RLA32 `NTK852AA` in anchors 1 and 3; OSC `NTK591VN`; CCMD8x24-C `NTK843BA` in anchor 5; CFIM1/CFIM2 `NTK504QA/QB` in 71/72 with separately ordered `NTK504NA` OMC2; two degrees; no SRA or protection | Documented pre-calibration candidate |
| `r40_cl_roadm_rla12_lru12_1deg_no_sra` | R4 `NTK803FA`; C+L RLA12 `NTK852BC` in anchor 1; `NTK591VQ` OSC in 1/50; LRU12 `NTK852NA` in anchor 3; one bidirectional degree; core-only; no SRA or protection | Documented pre-calibration candidate |
| `r40_cl_roadm_rla12_lru12_1deg_sra6` (`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6`) | Same R4 C+L RLA12/LRU12 one-degree terminal core plus C+L SRA `NTK830AC` in slot 6; fixed 6/5 and 6/6 external endpoints and fixed RLA/SRA express links; no add/drop, unrelated WSS, or protection | Documented SRA-disabled pre-calibration candidate |
| `r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_single_no_sra_ospfv2_rne_v1` | R2 `NTK803DA`; C+L DLE `NTK850DC` in anchor 1; OSC 50/60 `NTK591VQ`, `ge-fec-1`; single rail; no SRA, protection, cascade, or direct-DCN COLAN; IPv4 OSPFv2 RNE | Documented pre-calibration candidate |
| `r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_single_sra4_ospfv2_rne_v1` (`R40_R2_CL_DLE_S1_SRA4`) | Same R2 C+L DLE core plus C+L SRA `NTK830AC` in slot 4 on the represented USXGN–SAT span; fixed 4/5 and 4/6 external endpoints and DLE/SRA express links; no second SRA, dual rail, protection, cascade, or COLAN | Documented SRA-disabled pre-calibration candidate |

The CDA provider emits the two external line links and the mandatory local
map `1/21 → 5/102`, `5/101 → 1/22`, `1/43 → 3/24`,
`3/23 → 1/44`, plus the single-ended logical `RLA-3-44` stub. The final
RLA-to-RLA connection is rendered as LC fiber; the commissioning example's
`mpo-cable` token conflicts with the documented connectors.

The CDC provider emits all five physical MPO connections, all 20 mandatory
logical subfiber links, and the reciprocal connection-validation port IDs and
Rx/Tx sequences. It corrects the example's stray `SITE1977` endpoint to the
local shelf. Its automatic deployment-control procedure carries the documented
loopback/dust-cap treatment for unused CFIM ports. That procedure is not an
observation of the installed packout; physical verification remains a field
step.

The no-SRA one-degree C+L terminal-core provider emits one bidirectional
external degree on RLA12 ports 1/53 and 1/54, the C/L LM1, LD1, SM1, and SD1
functional groups, and only these audited RLA12/LRU12 core links:

- `LRU3-LINEIN`: RLA12 1/11 → LRU12 3/52;
- `LRU3-LINEOUT`: LRU12 3/51 → RLA12 1/12; and
- `LRU3-MON`: RLA12 1/10 → LRU12 3/50.

The apparent workbook WSS contradiction is a copy defect, not an unresolved
vendor mapping. The audited RLA12 map is SW11 output/input 41/42 and SW12
output/input 43/44. `ROADM_A!B221` is labeled SW12 but repeats the slot-3 SW11
41→42 formula; the corresponding C-band SW12 map is slot 1, 43→44. ATLAS uses
the corrected map and does not reproduce that formula. This provider stops at
the documented terminal core: it does not create CCMD/client/add-drop
packouts, remote or express WSS peer half-links, Raman/SRA, or protection.

The SRA6 terminal sibling retains that exact RLA12/LRU12 core, adds only
`NTK830AC` in slot 6, moves the represented external LM1/LD1 degree to SRA
ports 6/5 and 6/6, adds the LD1 RAMAN object, and emits the fixed express
links `RLA1-SRA6` 1/53→6/4 and `SRA6-RLA1` 6/3→1/54. It does not broaden the
provider into a complete add/drop or multidegree ROADM: client/CCMD modules,
unrelated WSS peer half-links, another degree, and protection remain excluded.

The two ILA providers fix the DLE propagation paths to `54 → 63` and
`64 → 53`, OSC directions to `50 → 60` and `60 → 50`, and emit the
path-specific C/L amp, VOA, and DGFF roles. The R4.0 example on printed pages
40–42 proves these are cross-shelf through-paths, not two independent
same-neighbor degrees:
PFG-1-to-2 takes its downstream peer from the port-63/output-side record and
its upstream peer from the opposite port-53-side record; PFG-2-to-1 swaps
those peers. ATLAS therefore rejects reuse of one neighbor for both physical
sides. Its automatic deployment-control procedure requires the installer to
keep line fibers disconnected and Span Calibration and Passive Terminal
Control inactive during staging. ATLAS does not detect those physical or live
states and does not claim that they were verified. Both SCO instances are
staged `Disabled` before external line links.

The SRA4 ILA sibling changes only the represented USXGN–SAT side: external
line-out becomes 4/5, the reciprocal line-in becomes 4/6, PFG-2-to-1 receives
the slot-4 RAMAN role, and fixed express links are `RLA1-SRA4`
1/63→4/4 and `SRA4-RLA1` 4/3→1/64. The opposite DLE side remains on 1/53 and
1/54. A second SRA, dual rail, cascade, DLA substitution, protected-ROADM use,
and COLAN are excluded.

The two exact SRA providers create the fixed SRA circuit pack and immediately
stage both its circuit-pack administration and `LINE-IN` RAMAN facility
disabled. They do not emit runtime target gain, safety thresholds, mixed-fiber
engineering, enablement, or calibration. Structured SRA evidence conflicts
with any selected no-SRA provider; conversely, an SRA provider is blocked when
its accepted endpoint does not match the fixed slot/ports, its represented
span is not bookended by the compatible exact provider, or reciprocal
direction/PFG mapping fails.

The word *staged* has two separate uses here. Provider output stages the
physical SRA administration and `LINE-IN` facility disabled; paired review
stages the first endpoint payload pending its reciprocal peer. Neither state
authorizes SRA enablement. A first-endpoint review payload remains
non-deployable until its facing payload passes the atomic paired check, and
both disabled pre-calibration candidates still remain subject to all ordinary
route-readiness and on-box validation gates.

The paired pre-calibration candidates still require review of the recorded
build/schema candidate, physical packout verification, reviewed optical paths,
automatic SRA controls, and successful on-box `validate` before commit. Later
SRA/RAMAN enablement and calibration require separately approved
fiber-specific engineering, the vendor OTDR go/no-go result, and clearance of
activation-inhibited alarms. ATLAS documents those requirements but neither
performs nor attests to them.

Provider confirmation is always explicit. The customer diagram can seed TID,
site, loopback, route OSPF area, distinct A/Z adjacency, and separately visible
span facts. A directly evidenced route-header band such as `C+L` is retained
as read-only context and is not propagated to every shelf. It can narrow the
compatible catalog, but cannot by itself prove a BOM or preselect a provider.
Advisory preselection additionally requires either the complete direct module
inventory or the complete fixed line-output map with matching chassis and
represented-degree coverage. In the absence of stronger direct per-shelf band
evidence, route scope is also a negative compatibility guard: conflicting-band
providers are unavailable in review and fail Apply/readiness if retained in an
older payload. RLS R4.0 commissioning printed p.199 and
the audited legacy workbook both emit `fiber-type LEAF`, although the formal
printed pp.121-122 value table names `Enhanced LEAF` and omits plain `LEAF`.
ATLAS therefore accepts the exact `LEAF` token while retaining a validation
warning for this vendor-document inconsistency; it never converts `LEAF` to
`Enhanced LEAF`. One visible span loss is not copied into an additional
unrepresented hardware degree.

For a route whose span fiber is uniform, ATLAS records the observed source
label once as diagram provenance and preselects it only when it exactly
matches an audited RLS R4.0 token. The operator must apply that reviewed
choice before it is recorded on every
active optical path and checked against both endpoint reviews and every exact
provider request. This route-wide scope removes repetitive entry without
turning a customer shorthand into deployable CLI.

Exact-payload schema 1.4 makes provider line cardinality explicit. A profile
contains one or two fixed line records. The one-degree C+L RLA12/LRU12
provider requires `line_1` and requires `line_2` to be `null`; its single
mux/demux pair carries both A→Z and Z→A traffic. The two-degree RLA terminal
providers require both physical bidirectional degree records. The DLE provider
also requires two records, but they are opposing unidirectional amplifier
egress paths. The first record may face the preceding A-side shelf or following
Z-side shelf; a second record, when the provider has one, faces the opposite
side.

Provider-local CLI link object names are fixed topology identifiers:
`LM1-LINEOUT` for the one-degree core, `LM1-LINEOUT`/`LM2-LINEOUT` for the
two-degree RLA layouts, and `PFG-1-2-LINEOUT`/`PFG-2-1-LINEOUT` for the DLE.
They are not route circuit IDs. A circuit identifier such as `BDJW7353` may
legitimately repeat across many physical spans and remains read-only route
context; ATLAS never copies it into the provider-local link-name field.

ATLAS stores fiber type and expected loss independently at both ends of one
shared physical span. The from-shelf review is A→Z and the to-shelf review is
Z→A, so asymmetric engineering is not collapsed. Applying a review
cross-checks shelf/site/OAM/OSPF identity immediately, and final export
requires both propagation reviews on every physical span.

The editor retains imported facts by route side. Direct compatible local-port
evidence has first priority for assigning the fixed line records. If the image
omits all endpoint observations and one provider is uniquely preselected, the
audited workbook/provider convention supplies a controlled fallback:
Add/Drop-A and ROADM-A physical Degree 1 faces Z, the corresponding Z-terminal
roles face A, and DLE amplifier Path 1 exits Z. The fallback is explicitly
non-executable and still requires installed-port verification. Ambiguous or
conflicting evidence never falls back. The selected side maps into fixed
record 1; only a two-record provider maps the opposite side into record 2.
Changing the assignment swaps the complete edited records when two exist; it
cannot leave a Z-side loss under an A-side label. While the fixed mapping is
unresolved, the editor still exposes imported A/Z route-side values under
provisional labels and keeps the first-line selector empty. An RLA tab
explicitly states that one represented degree carries both propagation flows.
For a two-degree provider, a first/last shelf's other unmodeled degree remains
blank and requires independent engineering; it is additional hardware, not the
missing return route. The one-degree provider has no second degree record.
Copying the route-facing span into another record is prohibited.

For `USELP1-L8R2`, the supplied route proves an R4 terminal on a C+L route with
one bidirectional route-facing line degree. It does not prove the two-degree
C-band RLA32/CCMD8x24 provider. The separately audited one-degree C+L
RLA12/LRU12 core is now registered for review, and the corrected WSS map above
removes the prior formula ambiguity. It remains a core-only pre-calibration
candidate—not proof of a complete installed terminal. Review of the
prepopulated build/schema candidate, installed RLA12/LRU12/OSC inventory, side
assignment, remote PFG names, span engineering, the selected optional COLAN
state, automatic deployment controls, and successful on-box `validate` remain
in the workflow. The
controls document procedures but do not verify the installed shelf. If
client/add-drop/CCMD hardware, remote WSS half-links, or protection are
required, a different complete provider is still required. If the represented
degree has exact slot-6 SRA evidence, only the registered SRA6 sibling is
compatible; that sibling still does not authorize any of those excluded
hardware groups.
ATLAS never invents a second physical degree for reverse traffic.

Schema 1.4 also codifies the deployment-management boundary. Add/Drop and
ROADM exact providers support terminal COLAN as one optional, all-or-nothing
block. In configured mode, one complete customer-approved `colan-x` or
`colan-a` record is strictly validated and emitted. In deferred factory-staging
mode, the canonical management record is blank, the remaining exact candidate
and Route Bundle remain available, and no COLAN interface or routing command
is emitted. The validation report warns `TERMINAL_COLAN_DEFERRED`; the
artifact and route-bundle manifests record the optional policy, deferred
state, and `colan_commands_emitted: false`. The diagram OAM address remains a
loopback candidate and is never reused as COLAN. The ILA provider prohibits
all COLAN fields. NTP is customer-managed and is absent from the request schema
and generated CLI. Every generated candidate still requires successful on-box
`validate`.

The supplied R4.0.0 upgrade procedures document `Rel. 4.00.00`, so ATLAS uses
`4.00.00` as the editable build/schema baseline when the route diagram does
not supply one. This default is an unverified candidate, not target-shelf
telemetry or proof that `4.00.00` is installed. A customer shelf may instead
run a later R4.0 build such as `4.00.01`; mandatory successful on-box
`validate` is the execution boundary. Physical frame/rack location is optional.
When it is blank, ATLAS omits the complete shelf-location command rather than
substituting a site code or TID. ATLAS also never invents a missing site or TID
to complete an exact request.

Schemas 1.2 and 1.3 are retired rather than migrated in place. Schema 1.2 DLE
requests would otherwise render materially different neighbor CLI after the
peer correction; schema 1.3 cannot express the one-degree request cardinality
or the current provider-fixed link-name contract. ATLAS opens current schema
1.4 review facts while retaining an old payload until the operator validates
and applies the replacement.

OSC pluggables are created in the complete initial-OAM batch immediately after
the loopback `/32`. This follows the R4.0 OAM requirement to create an OSC
pluggable in the same batch as, or after, the loopback address and allows the
system to auto-create the OSC interface as IPv4 unnumbered. ATLAS therefore
does not redundantly provision the auto-created OSC interface; it retains the
required default-network-instance membership and OSPFv2 interface references.

All six manifests set `deployment_approved: false` and
`on_box_validate_required: true`. They intentionally omit licensed feature
activation, calibration, PTC, OTDR runtime tuning, threshold relaxation, and
channel creation.

Each exact provider also contributes automatic deployment-control advisories
to its annotated CLI, validation report, and manifest. The common controls
cover comparison of the actual inventory/topology to the fixed provider,
separate runtime-tuning and calibration engineering, and review of the
prepopulated unverified build/schema candidate. The DLE adds
disconnected-fiber/inactive-control staging; the CDC RLA32 adds unused-CFIM
physical treatment. Each SRA provider additionally requires compatible
bookending/colocation, approved fiber-specific runtime engineering, and the
OTDR go/no-go plus activation-alarm gate. These controls are included without
a checkbox, but are not telemetry, observed state, or verification. They
cannot change `deployment_approved: false` and cannot replace successful
on-box `validate`. The emitted SRA circuit pack and RAMAN facility remain
disabled. The separate runtime package may be customer- or Ciena-owned,
including PlannerPlus when applicable; the audited generator does not assume
LightRiver uses PlannerPlus.

Schema 1.4 keeps the six former confirmation Booleans only for exact
decode/encode compatibility. `false` does not block offline generation, and
`true` does not establish physical verification. The manifest instead exposes
provider-applicable `deployment_controls` records as
`automatic_background_advisory`, `active_unverified`, and
`physical_verification_status: not_asserted`; it records
`legacy_confirmations_block_offline_generation: false`. This makes the
automatic procedure auditable without turning it into telemetry or deployment
approval.

## Why the legacy workbook generator remains quarantined

The workbook profiles are fixed designs hidden behind generic names:

| Workbook label | What it actually renders |
|---|---|
| Add/Drop 12x1 | One R4 eight-slot C+L terminal layout with fixed RLA12/LRU12/CCMD16 slots |
| ILA | One C+L DLE layout, excluding other DLA/DLE, SRA, dual-rail, cascaded, and protected variants |
| ROADM 12x1 | One RLA12/LRU12 degree with 12 fixed WSS half-link/power-profile assumptions |
| Any 64x1 selection | One blanket RLA64 iC&L TDA-style pre-provisioning pattern regardless of the selected Add/Drop/ROADM label |

Confirmed blockers include:

1. Every profile emits the literal invalid line `commit —> quit`.
2. PFG commands occur after `commit` without re-entering mandatory batch mode.
3. OSC OSPF keys omit the `-1` component in the interface ID.
4. The initial OAM block is incomplete: required network-instance interface
   bindings and some redistribution paths are absent.
5. Blank-shelf link commissioning uses `set link` where the documented create
   workflow is required.
6. Fixed slots are emitted without a shelf-family, installed-inventory, slot
   occupancy, or PEC compatibility model.
7. Remote endpoints are guessed from a generic neighbor type rather than exact
   discovered or engineered ports.
8. Protected paths, TPS/TPS2, complete working/protection topology, and
   route-level calibration order are not represented.
9. Raman defaults use a 17-dB threshold instead of the documented
   fiber/topology-specific SRA requirements and preflight.
10. Legacy fiber aliases and coefficients do not match the RLS 4.0 native
    catalog and include `Unknown`.
11. Licensed features, including Span Calibration, are enabled without
    inventory/license/precondition proof or Passive Terminal Control ordering.
12. TDA power profiles apply a blanket target without the required
    transponder/excess-loss design inputs.
13. Tx-proxy rows can omit the required source, and some profiles reference
    unmodeled circuit packs.

Relevant evidence includes:

- CLI Reference printed pages 17–19 and 32–34: batch/commit/quit behavior;
- OAM Communications printed pages 77 and 222–223: complete batch-mode OAM
  and OSC interface identity;
- ZTP/Manual Commissioning printed pages 101–111 and 118–121: supported slots,
  initial OAM, and link creation;
- Optical/SPLI/OTDR PDF pages 28–33, 74–79, 199–203, and 267: TDA power,
  neighbor discovery, SRA preflight, and native fiber data;
- Licensing/Security/Admin PDF pages 254–256 and 290–291: RLS 4.0 and feature
  licenses plus Passive Terminal Control/calibration ordering; and
- Alarms/Module Replacement PDF pages 92, 100–101, 123, and 244: PEC, CV,
  unknown-fiber, and calibration-failure consequences.

The safe execution baseline for every RLS 4.0 provider is:

```text
batch
<one complete dependency-safe command group>
validate
commit
quit
```

An exported `validate` line is only an instruction. Deployment still requires
the operator to capture a successful validation result from the target shelf
running the matching RLS 4.0 build/schema and to stop before `commit` on any
error. `save-config` may be used as a post-commit backup; it is not a
substitute for candidate validation or commit.

## ATLAS policy

- R4.0 role rows always populate route order, FBN, IRM, rack diagrams, and MOP
  preview.
- A role-only row with a compatible registered exact provider remains
  `EXACT_PROVIDER_REVIEW_REQUIRED`; a role with no registered exact provider
  remains `PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED`. Both block final Route
  Bundle publication until the appropriate review is complete.
- **Review Configuration…** requires the operator to choose a compatible
  provider explicitly. The fixed BOM/discriminators are read-only; the
  unverified build/schema baseline, identity, routing, fixed-direction
  route-side assignment, both directional spans, and neighbors are editable
  and must validate before Apply. Frame/rack location is optional and, when
  blank, suppresses the complete shelf-location command.
  Provider-specific deployment-control advisories are attached automatically;
  they are not physical-state attestations.
- Terminal COLAN is optional for Add/Drop and ROADM exact providers. Deferred
  factory-staging mode emits zero COLAN commands but still permits the
  remaining exact candidate and atomic bundle after every other gate passes.
  Configured mode is strict and all-or-nothing. ILA providers continue to
  prohibit COLAN, and no mode copies the diagram OAM/loopback address into a
  COLAN field.
- Apply refuses identity/OAM/OSPF or adjacent-path mismatches instead of saving
  a knowingly stale payload. Closing an edited R4.0 review requires explicit
  discard confirmation.
- Bundle readiness distinguishes an ordinary missing exact review from
  `R40_SRA_CAPABLE_PROVIDER_UNAVAILABLE`. The latter is an audited-provider
  coverage gap for the reviewed SRA layout; it does not apply when the
  accepted USXGN/SAT evidence matches one of the two registered exact SRA
  providers. It cannot be resolved by choosing a no-SRA provider.
- Each shelf retains its own versioned payload, so a route can contain multiple
  reviewed shelf configurations before one atomic export.
- R2/R4 chassis text never fills the software-release field.
- The legacy workbook adapter is not in the production provider registry.
- Unsupported C+L RLA12 CDA/client or FIM4A packouts, other one-degree
  terminal arrangements, remote-CCMD,
  disaggregated, R8-300, protected ROADM, TDA/RLA64, fixed-CMD, dual-rail,
  other SRA-equipped, and cascaded variants remain blocked.
- A RAMAN display label never toggles CLI. Confirmed structured SRA evidence
  is incompatible with every no-SRA provider and must match the fixed
  slot/port, inventory, bookending, and route-side contract of a registered
  exact SRA provider.
- A route containing any unreviewed/invalid/unsupported shelf fails Route
  Bundle publication as a whole; no partial CLI set is published.
- The active provider boundary is exact RLS R4.0. Any other software release
  is preserved as evidence but rejected with `UNSUPPORTED_SOFTWARE_RELEASE`;
  ATLAS never converts it into an R4.0 project.

## Controlled source set

The audit used the exact local PDF bytes listed below:

| Document | SHA-256 |
|---|---|
| `323-2051-101_(RLS_R4.0_OAM_Communications)_Issue2.pdf` | `39329bfa0dbb1456c5cb34e0df44e9bfa1667e0e7f7e603f4156a2d111414b0d` |
| `323-2051-190_(RLS_R4.0_CLI_Reference)_Issue1.pdf` | `c92ae907d865c29ef4668c6c9145942c814bcbc4156ef09881ccc57b26f4d29a` |
| `323-2051-201_(RLS_R4.0_Installation_Guide)_Issue1.pdf` | `e5bf751e07fd5e4f55f60c9e17c99c66e2317c2fdf64b2e0e108f3ef6d69ac55` |
| `323-2051-220_(RLS_R4.0_ZTP_Manual_Commissioning)_Issue3.pdf` | `1cbc000482fc7ad22351eda72ea7a43e44361644d2c4776fd5a24bcdf32727d9` |
| `323-2051-300_(RLS_R4.0_Licensing_Security_Admin)_Issue2.pdf` | `4904423a7be3671820a3f07f9731cd3773cf70da8c4d16227c0c6e9a793e54bb` |
| `323-2051-310_(RLS_R4.0_Equip_Facility_Protect)_Issue4.pdf` | `2a25450db04617747c7a3f8b4f653707365bbf775db95863ac1c804da00d81a4` |
| `323-2051-318_(RLS_R4.0_Optical_SPLI_OTDR)_Issue2.pdf` | `74c0d03439cbfe447199486d7aa11c2ea7607d96e40a9178329c08d1aef547a7` |
| `323-2051-330_(RLS_R4.0_L0_Control_Plane)_Issue2.pdf` | `2ca3a79ddb3e7a02f4f9f2aa36541003090312e9f1745a3385fbb36bc8c6af65` |
| `323-2051-520_(RLS_R4.0_Logs_and_PMs)_Issue1.pdf` | `b632567046bd91841e66ada3fd21b18facc9c8161f41b6144cbe6891896431fd` |
| `323-2051-530_(RLS_R4.0_Alarms_Module_Replacement)_Issue4.pdf` | `91055b67367bde329f248305454e39a28893cabc45ac67fe2d50fd565f4e33c6` |
| `323-2051-700_(RLS_R4.0_SNMP)_Issue2.pdf` | `8ffef2ee3e707fb686a178961fc92fc7ab14f6221fbb8c186e4ac1ae0cab1081` |
| `6500_RLS_R4.0_RN_Issue3.pdf` | `698498c6930c748932e43af2059b44952bd3ec25a63c86cf2a28740d900be227` |
| `NTRN10WA_(RLS_R4.0_Planning_Guide)_Issue3.pdf` | `6e4b40839efbe7eeb7f320d614ce3187a2fea01306a692fe967f20fa3cd48c25` |
| `NTRN38WA.1_(6500_RLS_R4.0.0_Software_Upgrade_Procedure)_Issue2.pdf` | `a688aa0da487937a60f1b118c64f87016a06a601fa9bfd14dddba08ac7017549` |
| `NTRN38WA.2_(6500_RLS_R4.0.0_Software_Upgrade_Procedure)_Issue2.pdf` | `0f17a2eaaa7218934b01536b673cf29cec8ebeeaf30fef34357c491361904b7b` |
