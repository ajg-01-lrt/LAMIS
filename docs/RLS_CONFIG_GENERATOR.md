# Ciena RLS R4.0 Configuration Providers

ATLAS currently implements one executable Ciena RLS software contract:
**RLS R4.0**. The configuration provider is integrated into **Ciena RLS Route
Builder**; there is no standalone configuration-generator mode.

The software release is a controlled product constant, not an operator choice.
If the customer changes release, ATLAS must be updated and re-audited before
that release can generate CLI. R4.2 projects and payloads are rejected without
being converted to R4.0.

Every output is a documented pre-calibration candidate. It is not deployment
approval and must pass `validate` on the matching blank R4.0 shelf before the
operator proceeds.

Because the route diagram normally does not identify the running build/schema,
ATLAS prepopulates **`4.00.00`** from the supplied R4.0.0 vendor upgrade
procedures. This value is editable planning metadata, not an observation of the
target shelf. A customer shelf may instead run a later R4.0 build such as
`4.00.01`; the operator must compare the target when possible and always require
a successful on-box `validate` before commit.

The current exact-request payload schema is **1.4** and generator version is
**1.6.0**. Schemas 1.2 and 1.3 are retired: correcting the DLE
upstream/downstream peer mapping materially changed generated CLI, and the
one-degree provider requires an explicit variable-cardinality request plus
provider-fixed local link names. ATLAS keeps an old payload untouched until the
operator opens the current review, validates it, and deliberately applies a
replacement.

## Exact providers

A diagram role such as Add/Drop, ROADM, or ILA does not prove installed
hardware. **Ordered route shelves** shows a non-executable **Provider** value
for each shelf: an applied exact provider, a hardware-qualified suggestion, a
single route-compatible candidate, a multiple-choice count, or an explicit
blocked state. When exactly one project-compatible provider remains,
**Review Configuration…** prepopulates it for inspection. The readonly
provider dropdown is retained and continues to list every project-compatible
provider in the audited catalog, including providers added later.

The adjacent **Direction map** value provides an at-a-glance view of the
provider's first immutable line record: physical degree 1 faces route A/Z for
an RLA shelf, or amplifier path 1 line-output faces route A/Z for the DLE. A
current payload is shown as applied. Without a payload, ATLAS derives a
review-only suggestion only for a sole project-compatible provider: direct,
high-confidence local output slot/port evidence is matched first; the audited
provider/role convention is used only when endpoint observations are genuinely
absent. Ambiguous, conflicting, low-confidence, invalidated, multiple-provider,
stale, unsupported-SRA, and incompatible cases remain unresolved. The exact
review keeps the direction selector so the operator can confirm or correct the
mapping.

The compact display uses `D` for a bidirectional RLA degree and `P` for a DLE
amplifier path. `D1→Z (both flows)` is one Z-facing mux/demux degree carrying
both A→Z and Z→A; `D1→Z / D2→A` and `P1→Z / P2→A` show two fixed records.
Prefixes distinguish **Applied**, **Direct**, and controlled **Derived**
results.

This convenience does not relax any gate. A provider is hardware-qualified
only when retained direct facts establish either its complete module inventory
or its complete fixed line-output map plus matching chassis evidence. For an
RLA terminal, a provider with more physical degrees than the uploaded route
still requires installed inventory that independently establishes those extra
degrees. A role, generic chassis, no-SRA label, protection default, or catalog
uniqueness never confirms a BOM or authorizes CLI. Ambiguous, conflicting,
unsupported-SRA, and incompatible results remain unselected.

| Provider ID | Route roles | Fixed audited layout |
|---|---|---|
| `r40_cda_rla12_c_2deg_no_sra` | Add/Drop A, Z, or neutral | R4 `NTK803FA`; two C-band RLA12 `NTK852BA`; `NTK591VN` OSCs; local CCMD16-C `NTK834AA`; no SRA/protection |
| `r40_cdc_roadm_rla32_c_2deg_ccmd8x24_no_sra` | ROADM A, Z, or neutral | R4 `NTK803FA`; two C-band RLA32 `NTK852AA`; `NTK591VN` OSCs; CCMD8x24-C `NTK843BA`; CFIM1/2 and OMC2; no SRA/protection |
| `r40_cl_roadm_rla12_lru12_1deg_no_sra` | ROADM A, Z, or neutral | R4 `NTK803FA`; slot-1 C+L RLA12 `NTK852BC`; `NTK591VQ` OSC at 1/50; slot-3 LRU12 `NTK852NA`; one bidirectional core degree; no SRA/protection |
| `r40_cl_roadm_rla12_lru12_1deg_sra6` (`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6`) | ROADM A, Z, or neutral | Same exact one-degree C+L terminal core plus slot-6 C+L SRA `NTK830AC`; external 6/5 and 6/6; no add/drop, unrelated WSS, or protection |
| `r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_single_no_sra_ospfv2_rne_v1` | ILA | R2 `NTK803DA`; slot-1 C+L DLE `NTK850DC`; `NTK591VQ` OSCs; single rail; no SRA/protection/cascade; OSPFv2 RNE without direct COLAN |
| `r40_r2_ntk803da_cl_cdc_dle_ntk850dc_s1_vq_ge_fec1_single_sra4_ospfv2_rne_v1` (`R40_R2_CL_DLE_S1_SRA4`) | ILA | Same exact C+L DLE core plus slot-4 C+L SRA `NTK830AC` on the represented USXGN–SAT span; no second SRA, dual rail, protection, or cascade |

Both C+L RLA12/LRU12 terminal providers are deliberately core-only. The SRA6
sibling adds only the fixed SRA and its audited express/line path; neither
provider provisions CCMD/client/add-drop packouts, unrelated remote or express
WSS peer half-links, or protection. Unsupported layouts remain planning-only.
These include other one-degree or multidegree arrangements, remote CCMD,
protected ROADM, fixed CMD, TDA, RLA64, other SRA placements/topologies, dual
rail, cascade, and unverified chassis/module substitutions.

After diagram facts are confirmed, a role with at least one compatible
registered provider but no applied exact payload reports
`EXACT_PROVIDER_REVIEW_REQUIRED`. `PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED` is
reserved for a role/layout for which the production registry has no compatible
exact provider. Both are non-ready states, but the former directs the operator
to the available exact-provider review instead of incorrectly reporting an
implementation gap.

### Physical degrees and propagation directions

ATLAS keeps three different concepts separate:

- **route side** is the A-facing or Z-facing physical adjacency;
- **RLA line degree** is a physical mux/demux pair; and
- **traffic propagation** is A→Z or Z→A.

One represented ROADM/Add/Drop RLA degree is bidirectional. Its line mux carries
the locally transmitted propagation and its paired line demux receives the
opposite propagation. Therefore a terminal with one visible adjacent span has
both A→Z and Z→A traffic on that one degree. Provider cardinality determines
the request: the one-degree C+L RLA12/LRU12 core has one visible line record and
stores `line_2: null`; the two-degree RLA providers require two records. A blank
second tab on a two-degree terminal provider is additional physical hardware
outside the modeled route, not the missing return route. ATLAS never copies
the represented span into that degree.

The no-SRA one-degree core uses LM1/LD1 on RLA12 ports 1/53 and 1/54 and
SM1/SD1 for the C/L section functions. The SRA6 sibling retains those
functional groups but routes the represented external degree through SRA
line-out 6/5 and line-in 6/6. Its fixed express links are `RLA1-SRA6`
(RLA12 1/53 → SRA 6/4) and `SRA6-RLA1` (SRA 6/3 → RLA12 1/54).
Both terminal providers retain `LRU3-LINEIN` (RLA12 1/11 → LRU12 3/52),
`LRU3-LINEOUT` (LRU12 3/51 → RLA12 1/12), and `LRU3-MON`
(RLA12 1/10 → LRU12 3/50). The corrected RLA12 WSS map is SW11 output/input
41/42 and SW12 output/input 43/44.
`ROADM_A!B221` is a workbook copy defect: it says SW12 while duplicating the
slot-3 SW11 41→42 formula. ATLAS does not reproduce that defect.

The selected one-degree core still requires review of the recorded build/schema
candidate, installed RLA12/LRU12/OSC inventory and SRA6 inventory when
applicable, physical side assignment, remote PFG identities, fiber/loss
engineering, the selected optional terminal-COLAN state, and a successful
on-box `validate`.
ATLAS automatically attaches the provider-specific deployment controls to the
annotated candidate, validation report, and artifact manifest; those controls
are procedures and assumptions, not observations of the shelf. A diagram,
route title, C+L header, or workbook formula cannot prove the physical state.

A DLE ILA is different. It has two unidirectional amplifier through-paths:
PFG-1-to-2 exits one physical side and PFG-2-to-1 exits the other. Each PFG
crosses the shelf, so its downstream neighbor comes from its output-side
record and its upstream neighbor comes from the opposite-side record. The
exact generator implements:

- PFG-1-to-2: line-output-1 neighbor/demux downstream; line-output-2
  neighbor/mux upstream; and
- PFG-2-to-1: line-output-2 neighbor/demux downstream; line-output-1
  neighbor/mux upstream.

Reusing one neighbor for both ends is rejected. This follows
`323-2051-220` R4.0 printed pages 40–42.

The route project retains one physical fiber-pair span per adjacency and
derives two read-only propagation views: the ordered from-shelf endpoint review
is A→Z egress and the to-shelf endpoint review is Z→A egress. A 15-span route
therefore has 30 propagation reviews without duplicating the customer’s shared
distance, circuit, or fiber-pair evidence.

### Evidence-based provider and line-map suggestions

The provider candidate resolver uses the reviewed route role only to limit the
catalog. Direct release, chassis family/PEC, per-shelf band, topology, add/drop
structure, protection type, module PEC/slot/subslot inventory, and SRA state
constrain the remaining providers. A supplied conflict eliminates that
provider. A directly supported route-header band can narrow the compatible
catalog and may populate the sole surviving provider as a review candidate,
but it never proves shelf hardware by itself. Hardware-qualified advisory
preselection still requires either complete direct module inventory or the
complete fixed line-output map with matching chassis and represented-degree
coverage. Thus a direct C+L route scope can make the C+L core immediately
visible without passing that independent hardware gate. When no stronger
direct per-shelf band exists, route scope is also a negative guard: a C-only
provider cannot be reviewed, applied, or exported for a C+L-scoped shelf.
High-confidence direct per-shelf band evidence may establish an intentional
band-partition exception.

An `exact_match` requires all fixed discriminators, the complete directly
observed module inventory, and an explicit matching SRA observation. A
`unique_candidate` identifies the sole compatible provider while reporting
which exact evidence is still absent. Catalog-relative uniqueness does not
identify installed hardware. ATLAS populates that sole route-compatible
candidate for review, while labeling it separately from a direct-hardware-
qualified suggestion. `ambiguous`, `conflict`, `unsupported_sra`, and
route-band mismatch leave selection unresolved. Insufficient identity and
unproved provider-degree coverage remain explicit blockers even when a sole
candidate is visible. Every result is an advisory record with reason codes,
matched/missing fields, project-filtered provider IDs, and
`deployable_cli: false`; it never creates an `R40ExactRequest`, payload, or
command.

The directly printed terminal header establishes route A/Z only when direct
header evidence agrees with direct first/last terminal TID evidence. An exact
reverse provider transcription is normalized before side assignment; any
other mismatch fails closed. After orientation, `preceding` is route side A
and `following` is route side Z for an intermediate shelf.

Line-map suggestion requires an advisory provider plus high-confidence direct
local line-output evidence. ATLAS matches the observed output port—and a direct
slot when the port is shared—to the selected profile's immutable line-output
map. A single consistent result can seed **physical Degree 1 faces A/Z** for
an RLA provider or **amplifier Path 1 line-out faces A/Z** for the DLE.
Missing, inferred-only, low-confidence, ambiguous, or conflicting evidence
leaves the field blank. The operator still reviews the provider, installed
BOM, fixed port/side map, and complete exact request.

The at-a-glance direction suggestion is non-executable. It does not identify
installed hardware, create an `R40ExactRequest`, satisfy an input gate, or
authorize CLI. Reordering a route recomputes A/Z orientation and invalidates
a diagram-relative suggestion whose original adjacency no longer proves the
edited topology.

## RAMAN/SRA boundary

The registry contains four exact no-SRA providers and two exact C+L SRA
providers. A RAMAN display label still cannot turn SRA commands on inside a
no-SRA provider. Executable SRA behavior is available only through
`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6` and
`R40_R2_CL_DLE_S1_SRA4`, after their structured endpoint evidence, fixed
inventory, and route mapping pass review.

For the supplied ELP1–SAT4 diagram and matching reference MOP, the small red
labels use source-scoped `slot/port` notation:

- `4/5` and `4/6` identify the slot-4 SRA line-out and line-in at
  `USXGN1-L8I2`;
- `6/5` and `6/6` identify the slot-6 SRA line-out and line-in at
  `USSAT4-L8R3`; and
- the red `3/5` and `3/6` examples below the legend are notation samples, not
  another route instance.

The `5` and `6` components are ports, not subslots. This convention must not be
generalized from red color alone to unrelated customer diagrams. ATLAS must
retain the raw labels and source rectangles, associate them with the correct
span endpoint, and require review.

The FBN MOP summarizes the same evidence as the shelf-level display text
`Slot 4` and `Slot 6`. Executable SRA topology instead needs a structured,
reviewed path-endpoint record containing module kind, slot, line-out port 5,
line-in port 6, route side, and evidence. A PEC may come only from reviewed
inventory or another explicit source—not from the display label. Neither the
display text nor an unreviewed red callout may select a provider.

An SRA-equipped provider is not a small branch on a no-SRA script. It must
audit the SRA PEC and slot, RAMAN PFG role, internal RLA/DLE/DLA-to-SRA links
through ports 3/4, external line endpoints through ports 5/6, bookending,
native and mixed-fiber engineering, OTDR/go-no-go preflight, activation order,
calibration prerequisites, and alarms.

For `USSAT4-L8R3`, the operator-confirmed installed core inventory establishes
the exact scope of the SRA6 provider: R4 `NTK803FA`; C+L RLA12 `NTK852BC` in
slot 1; `NTK591VQ` OSC at 1/50; LRU12 `NTK852NA` in slot 3; and C+L SRA
`NTK830AC` in slot 6. The external degree uses 6/5 and 6/6; its fixed express
links are `RLA1-SRA6` 1/53→6/4 and `SRA6-RLA1` 6/3→1/54. This confirmation
supports that exact terminal core only. It does not establish or authorize
unrelated add/drop/client/CCMD hardware, remote or express WSS peer
half-links, another degree, or protection.

The paired `USXGN1-L8I2` provider fixes R2 `NTK803DA`, C+L DLE `NTK850DC` in
slot 1, `NTK591VQ` OSCs at 1/50 and 1/60, and C+L SRA `NTK830AC` in slot 4.
The represented SRA side uses external 4/5 and 4/6 and fixed express links
`RLA1-SRA4` 1/63→4/4 and `SRA4-RLA1` 4/3→1/64; the other DLE path remains on
1/53 and 1/54. A second SRA, dual rail, cascade, DLA substitution, protection,
and direct COLAN remain outside this provider.

These two providers are one paired-span contract, not two independent RAMAN
switches. The accepted USXGN–SAT span must be bookended by the slot-4 and
slot-6 structured SRA records, each record must match its provider's fixed
line endpoint and route side, and the reciprocal direction/PFG relationships
must validate. Pending, invalid, wrong-slot, unbookended, or no-SRA pairings
fail closed.

Paired review is deliberately order-independent. The operator may review
either endpoint first. When that endpoint passes its local exact-provider,
identity, fixed-inventory, slot/port, and represented-path checks, and the
only unresolved topology condition is that its compatible SRA peer has no
current exact payload, ATLAS may save it as **Staged — paired SRA peer review
pending**. This is a local review checkpoint, not a readiness result: the
shelf remains non-deployable, the route cannot publish CLI or a bundle, and
the staged payload cannot satisfy the reciprocal bookending gate.
Here, *compatible peer* also requires reviewed shelf facts, exact `RLS R4.0`,
an eligible audited role/band provider, and matching accepted SRA slot
evidence; a shelf that still needs those corrections is a hard blocker, not a
pending-pair shortcut.

Applying the second endpoint constructs one candidate route snapshot
containing both payloads and validates the pair against that same snapshot.
ATLAS checks the two fixed providers, accepted SRA evidence, route sides,
external ports, neighbor TIDs, provider-local link/PFG identities, fiber, and
endpoint-local loss relationships reciprocally. Only a complete successful
check promotes the pair together into ordinary exact-payload readiness
evaluation. If the second endpoint fails, ATLAS attaches no second payload and
the first remains visibly staged. A missing peer is the only paired condition
eligible for this staging exception; wrong slot/port, incompatible/no-SRA
provider, nonreciprocal neighbor or PFG, stale route snapshot, or any other
identity/topology conflict still rejects Apply without mutating the pair.

An edit to either paired provider, its fixed direction/route-side assignment,
or a bound adjacent-path/topology value invalidates the prior reciprocal
result and returns the pair to staged review. Both endpoints must then pass
the same-snapshot check again. Saving and reopening the project preserves the
derived pending state, but never converts it into deployment authorization.

The ELP1–SAT4 regression contract exercises the complete source-scoped
selection result, not only the catalog entries. With the accepted red
slot/port convention, `USXGN1-L8I2` must prepopulate
`R40_R2_CL_DLE_S1_SRA4`, map fixed amplifier path 1 to route side Z, retain
`USKP21-L8I2` on side A, and retain `USSAT4-L8R3` on side Z.
`USSAT4-L8R3` must prepopulate
`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6`, map its sole physical degree to side A,
retain `USXGN1-L8I2` as that neighbor, and create no phantom Z-side degree.
`USELP1-L8R2` and the other 13 ordinary ILAs must remain on their compatible
no-SRA candidates. An unassigned source SRA callout disables no-SRA absence
inference and leaves the affected choice unresolved.

Both exact SRA payloads create the fixed SRA hardware and immediately stage
the SRA circuit pack and `LINE-IN` RAMAN facility disabled. They attach the
fixed RAMAN role and links but do not emit runtime gain, safety, mixed-fiber
engineering, enablement, or calibration. The operator-supplied inventory is a
review input, not live shelf telemetry; the installer still verifies the
packout and recorded build/schema candidate and must obtain a successful on-box
`validate` before commit. Enabling or calibrating RAMAN remains a later,
separately approved procedure gated by the vendor SRA OTDR go/no-go result and
clearance of RAMAN activation-inhibited alarms.

The vendor basis is `323-2051-220` printed pages 40–42 / PDF pages 48–50,
printed page 123 / PDF page 131, printed page 133 / PDF page 141, printed page
435 / PDF page 443, and printed pages 461 and 464–466 / PDF pages 469 and
472–474. `NTRN10WA` printed page 159 / PDF page 177 requires SRAs to bookend a
span and be collocated with the DLE/DLA serving that span.

## Route review workflow

1. Upload the customer route diagram.
2. Review the ordered shelves and diagram evidence.
3. Use **Confirm & Next Pending** to commit each imported shelf fact review.
   Configuration review remains closed until every shelf and structured RAMAN
   callout has an explicit disposition. ATLAS selects and loads the next
   pending shelf automatically. A reviewed shelf without an exact compatible
   provider payload displays **Confirmed - CLI Pending**; this is a fact-review
   state, not deployment authorization.
4. Select one audited **Native CLI fiber type** and use **Apply to all spans**.
   The observed diagram label remains separate source evidence. An exact
   supported match such as `LEAF` may be preselected but is not confirmed
   until the operator applies it; no alias conversion occurs.
5. Select a shelf and choose **Review Configuration…**.
6. Review the compatible exact provider. ATLAS prepopulates the sole
   project-compatible candidate when one exists, but identifies whether it is
   merely catalog-unique or also supported by qualifying direct hardware
   evidence. The operator can change it through the retained dropdown and must
   confirm the installed BOM either way.
7. Review the first fixed local line-output against route side A or Z. For an
   RLA provider this is a physical bidirectional degree; for the DLE it is an
   amplifier egress path. ATLAS first uses a
   unique direct port-to-provider match. If endpoint observations are entirely
   absent, a uniquely preselected provider may use its audited route-role
   convention as a non-executable fallback; conflicting evidence never does.
   Otherwise the operator must assign it.
   ATLAS then maps the stored A/Z route facts into the matching fixed local
   outputs. Changing the assignment swaps the complete line records;
   it does not only relabel the tabs.
8. Review identity, OAM, neighbors, PFG names, provider-fixed local link
   names, fiber, losses, and patch-panel values. ATLAS automatically includes
   the selected provider's deployment-control procedures in the generated
   review artifacts; there are no separate confirmation checkboxes.
9. Select **Validate & Preview**. ATLAS has already supplied the editable,
   unverified `4.00.00` build/schema baseline, and a blank optional frame/rack
   location does not block the candidate. A terminal may remain in the
   explicit deferred factory-staging COLAN state; ATLAS then generates the
   remaining exact candidate with no COLAN commands and records a visible
   warning. If another mandatory fact is still pending, ATLAS shows a GUI-only,
   visibly watermarked planning review of the values already available. This
   is not a CLI preview and cannot be copied or applied.
10. Complete the remaining gates, validate, and apply the reviewed request to
    the shelf. For the first endpoint of an exact SRA pair, a locally valid
    request is saved only as **Staged — paired SRA peer review pending** and
    remains non-deployable.
11. After a successful ordinary Apply, ATLAS passively selects and offers the
    next eligible shelf and reports exact-review progress. After staging the
    first SRA endpoint, it instead offers the facing paired endpoint. It does
    not automatically open another modal review. Closing or canceling a review
    without Apply does not advance the queue.
12. Review the paired endpoint in either order. The second Apply validates both
    endpoints reciprocally in one candidate route snapshot; only a successful
    paired result clears the pending-pair gate.
13. Repeat for every shelf and both ends of every optical adjacency.
14. Preview the MOP.
15. Use **Export Route Bundle…** only when the whole route is ready.

The exact editor prepopulates all applicable reviewed route values before a
provider is confirmed: shelf/site identity, primary OAM candidate, OSPF area,
and the side-keyed neighbor, route circuit context, reviewed native fiber token,
directional loss, distance, fiber range, and original diagram fiber label.
If the first fixed line-output is still ambiguous, those values appear
immediately under explicit route-side staging labels. RLA labels also show
both flows carried by that degree, such as
`Z-facing; A→Z TX / Z→A RX`; the physical-side selector remains empty.
Confirming the side maps those stored A/Z facts into the fixed line records.
Distance, source fiber range, and the raw diagram label remain read-only
context because they are not fields in the exact provider request.

Local CLI link object names come from the exact provider, not from the route
circuit label: the RLA layouts use `LM1-LINEOUT` and, when present,
`LM2-LINEOUT`; the DLE uses `PFG-1-2-LINEOUT` and `PFG-2-1-LINEOUT`. A route
circuit ID can legitimately repeat across many spans and remains read-only
context. ATLAS replaces an untouched diagram-derived circuit placeholder with
the provider-local name but preserves an explicit operator correction for
validation.

An explicitly printed route-header optical band such as `C+L` is also shown as
read-only context when it has matching direct evidence. It never selects a
provider by itself or proves a shelf BOM. It can narrow a candidate only after
the complete inventory or complete line-map/chassis gate passes, and it blocks
a conflicting band-specific provider when no direct shelf-level band overrides
the route scope. The same guard runs when the review opens, when Apply is
attempted, and when a stored payload is evaluated for export. The editor logs
privacy-safe counts for carried fields, controlled derivations/defaults, and
remaining manual review groups.

The route retains endpoint-local provider link names and losses separately from
the shared route circuit identifier. The from-shelf review is A→Z and the
to-shelf review is Z→A, which supports asymmetric engineering without one
shelf overwriting its peer.
For a first or last shelf, the opposite external degree may not exist in the
uploaded ordered route. Its fields remain blank and require independently
engineered neighbor, link, and expected-loss values. ATLAS never duplicates
the visible route-facing span to satisfy a two-degree provider.

## Fixed and editable data

Prepopulation follows one controlled fill order:

1. matching direct diagram evidence;
2. audited provider/workflow fixed or default data for a field the diagram did
   not supply; and
3. operator entry for the unresolved remainder.

Direct evidence therefore replaces a conflicting default. This order does not
lock an incorrect source value: an explicit operator correction becomes the
reviewed value and is checked against the route and exact-provider invariants.
A direct conflict with an immutable provider PEC, slot, subslot, or port map
eliminates that candidate rather than modifying its audited layout. Each source
class remains distinguishable in provenance.

The historical `Ciena RLS C+L CLI Config v3.5LR.xlsx` workbook contributes only
the audited literal column-B `<...>` input contract. Those cells are mapped to
named typed fields such as shelf/TID, optional rack/frame location, loopback,
OSPF, route neighbor, and optional terminal COLAN. ATLAS does not execute or copy
spreadsheet formulas, cached command output, prose, URLs, passwords, or
license-registration material. Historical ROADM switch-neighbor placeholders
that the selected exact provider does not implement remain visible as
unsupported context rather than invented provider inputs.

The following are fixed by the provider:

- software release `RLS R4.0`;
- chassis and module PECs;
- slot, subslot, and port maps;
- PFG applications and local logical/physical connectivity;
- provider-local CLI link object names, distinct from route circuit IDs;
- supported OAM mode for the exact topology;
- unsupported feature omissions;
- pre-calibration safety policy.

The operator reviews customer/engineering facts:

- TID, hostname, and site identity; ATLAS never invents a missing site or TID;
- loopback and OSPF area;
- optional, customer-provided COLAN addressing/routing for Add/Drop and ROADM
  terminals only; the operator may instead omit the complete COLAN block for
  factory staging;
- optional physical rack/location identification; a site code or TID is not
  substituted for it, and a blank value omits the entire shelf-location CLI;
- the editable target build/schema candidate, initially prepopulated as the
  unverified vendor baseline `4.00.00`;
- neighbor TID and facing PFG identities;
- native fiber type and directional expected loss;
- patch-panel loss, repair margin, and high-loss threshold.

### Automatic provider-specific deployment controls

The exact-provider editor has no **Confirmations** tab. Selecting a provider
automatically includes its deployment-control profile in every validation
preview, annotated CLI, per-shelf validation report, and exported manifest.
The route validation report also states the automatic-control boundary. This
reduces repetitive UI work; it does not convert a procedure into an observed
fact.

| Automatic control | Applies to | ATLAS behavior and boundary |
|---|---|---|
| Expected inventory/topology procedure | Every exact provider | Validates the request against the provider-fixed chassis, OMC2 requirement, slots, PECs, port map, degree/path count, direction assignment, and reviewed route topology. It reminds the installer to compare the actual packout; ATLAS has not inspected it. |
| Runtime-tuning and calibration boundary | Every exact provider | Keeps runtime tuning, licensed-feature activation, and calibration out of pre-calibration CLI and calls for the separately approved engineering procedure. That procedure may be a customer or Ciena package, including PlannerPlus when applicable; ATLAS does not require LightRiver to use PlannerPlus. |
| Recorded target-build/schema candidate | Every exact provider | Prepopulates the vendor-documented `4.00.00` R4.0.0 baseline as editable, unverified metadata. It does not assert the running shelf is `4.00.00`; a later R4.0 build such as `4.00.01` may be present, so successful on-box `validate` remains mandatory before commit. |
| Disconnected-fiber and inactive-control staging procedure | Both C+L DLE ILA providers | Documents that line fibers must be disconnected and Span Calibration/Passive Terminal Control inactive while the disabled-SCO pre-calibration candidate is staged. ATLAS does not detect cable state or feature state. |
| Unused-CFIM physical-treatment procedure | C-band CDC RLA32 ROADM only | Documents the required loopback/dust-cap treatment from the audited packout. ATLAS does not visually inspect the installed CFIM ports. |
| SRA span bookending and colocation | Both exact SRA providers | Requires the reviewed span to be bookended by compatible SRAs, with each SRA collocated with its serving RLA/DLE and matching the fixed PEC, slot, endpoint, and express-link map. |
| Approved SRA runtime engineering | Both exact SRA providers | Requires a separately approved fiber-specific procedure before runtime enablement. ATLAS does not infer gain, safety, mixed-fiber, activation, or calibration values from diagram distance/loss. |
| SRA OTDR go/no-go and alarm gate | Both exact SRA providers | Requires the vendor go/no-go result and clearance of RAMAN activation-inhibited alarms before SRA/RAMAN enablement or accepted calibration. ATLAS does not perform or attest to those field checks. |

All six providers receive the three universal controls. Both C+L DLE
providers also receive their DLE staging controls; the C-band CDC RLA32
receives its CFIM procedure; and both SRA providers receive the three SRA
controls. These controls are included whenever the provider is reviewed or
generated, cannot be skipped through the UI, emit no claim of physical
verification, and never change `deployment_approved: false`.

In addition to those background advisories, generated SRA CLI deliberately
stages the SRA circuit pack and `LINE-IN` RAMAN facility disabled. The output
remains a documented pre-calibration candidate and must pass `validate` on the
matching target shelf before commit. On-box validation does not enable RAMAN
or replace the separately approved go/no-go, alarm-clearance, activation, and
calibration procedure.

For exact-payload schema compatibility, legacy confirmation fields are not
treated as observations or deployment authorization. Stored values from the
former checkbox workflow cannot suppress the automatic controls or make an
artifact deployable. Schema 1.4 retains all six Boolean fields for strict
decode/encode round trips: `false` no longer blocks offline candidate
generation, while `true` is never interpreted as physical proof.

Blank values that only the customer or target shelf can supply are expected
workflow states rather than prepopulation defects. A terminal opens with an
explicit **Deferred — omit COLAN for factory staging** state and blank COLAN
name, address, prefix, gateway, and routing values. Deferred is an intentional
all-or-nothing optional feature selection, not a missing exact-CLI gate. ATLAS
generates and may bundle the remaining exact candidate, emits no COLAN
interface or routing command, adds warning `TERMINAL_COLAN_DEFERRED`, and
records `colan_policy: terminal_optional`, `colan_state: deferred`, and
`colan_commands_emitted: false` in the per-shelf manifest. ATLAS does not
create a placeholder `colan-x`, invent an address, or treat the diagram
loopback as a COLAN address.

When direct factory or customer DCN access is wanted and the complete approved
design is available, the operator changes the state to configured and selects
one `colan-x` or `colan-a` record. Interface name, IPv4 address, prefix,
routing model, metrics, and the gateway when static routing is selected are
then validated as one complete set. A partial or internally inconsistent
numbered COLAN design remains CLI-blocking; ATLAS never fills its missing
values. ILA providers instead have a fixed **COLAN prohibited** policy and
cannot store or emit COLAN.

The build and location behavior is:

- ATLAS starts the editable target build/schema at the documented `4.00.00`
  baseline and marks it as defaulted/unverified; clearing it or replacing it
  with only a generic `RLS R4.0` release label remains invalid;
- frame/rack location is optional. When it is blank, ATLAS emits a warning and
  omits the complete `set shelf shelf-location ...` command, including bay and
  physical-shelf values; and
- a configured numbered terminal COLAN selection without a complete
  customer-provided design raises strict field errors such as
  `INVALID_IPV4: management.ip_address`; selecting deferred instead emits no
  COLAN commands and does not block the remaining exact candidate.

ATLAS does not claim the baseline is the installed build, manufacture a
rack/frame identifier from a site code or TID, invent a missing site/TID, or
copy a diagram loopback into COLAN. ILA providers prohibit COLAN; terminal
providers offer it only as an optional, explicit all-or-nothing block.

The diagram OAM address is seeded only as a loopback candidate; it is never
copied into COLAN. ILA shelves use loopback/OSC OSPF RNE and cannot carry or
emit COLAN. NTP is customer-managed: the exact editor has no NTP input, the
payload schema stores no NTP field, and the generator emits no NTP commands.

### GUI-only planning review

Pending customer or on-box facts do not prevent the operator from inspecting
the prepopulated review surface. The editable `4.00.00` baseline supplies the
recorded build/schema candidate, and a blank optional frame/rack value is
handled by omitting shelf-location CLI. Deferred optional terminal COLAN is
not a planning-preview gate: strict generation proceeds with the COLAN block
omitted and a visible warning. If another mandatory fact is unresolved,
**Validate & Preview** renders a conspicuously watermarked planning review in
the GUI. It lists the actual unresolved gates and summarizes the selected
provider, identity, direction, and route-facing engineering already available.

That planning view is deliberately kept outside `R40ExactRequest`,
`R40ConfigArtifact`, and the stored exact-payload schema. It contains no CLI,
manifest, payload, hash, `batch`, `set`, `validate`, `commit`, or `quit`
candidate. **Copy** and **Apply Reviewed Configuration** remain disabled, the
route callback is not invoked, and no stale planning text is reused after an
input changes. Completing the gates returns the same action to strict
generator validation. Final configuration export and **Export Route Bundle…**
use the unchanged strict readiness gates; the planning view can never satisfy
them or be packaged as a deliverable.

## Commissioning sequence

The R4.0 provider emits dependency-sized transactions:

```text
batch
<complete dependent command set>
validate
commit
quit
```

Parent equipment is created first. In the initial OAM transaction, ATLAS
creates the loopback `/32`, then the OSC pluggables. RLS automatically creates
the OSC interfaces as IPv4 unnumbered; ATLAS then adds network-instance
membership and OSPFv2 references. Explicit redundant OSC unnumbered commands
are not emitted.

PFGs and links are generated only from the selected exact topology. Runtime
calibration, PTC, channel creation, threshold relaxation, licensed-feature
guessing, and inferred Raman behavior are omitted.

## Validation and export boundary

ATLAS blocks generation or export when any of the following is true:

- software release is not exactly `RLS R4.0`;
- the profile or provider ID is not registered;
- the versioned exact payload is absent, malformed, or stale;
- shelf identity, OAM, OSPF, chassis, or BOM differs from the request;
- a terminal is marked configured but its COLAN design is partial or invalid,
  or an ILA contains any COLAN value; a wholly deferred terminal COLAN block
  is valid and emits zero COLAN commands;
- a confirmed A/Z-to-fixed-line-output mapping is missing; an advisory seed
  alone does not satisfy this gate;
- the one route-wide native fiber choice is missing, unsupported, or differs
  from any path or endpoint review;
- a neighbor, PFG identity, provider-fixed link name, fiber, or endpoint-local
  loss differs from the selected exact topology or reviewed route path;
- reviewed SRA endpoint evidence conflicts with the selected no-SRA provider;
- an SRA provider lacks its accepted fixed slot/port endpoint, the represented
  span is not bookended by a compatible exact SRA provider, or reciprocal
  route-side/PFG relationships do not match;
- either A→Z or Z→A egress review is missing, or the physical span remains
  pending/manual;
- a provider or bundle artifact claims another release/generator.

Bundle export is atomic. It publishes nothing unless every shelf passes the
same R4.0-only route contract. Route Builder runs this preflight before it asks
for a destination or submits an export worker, and the exporter repeats it as
defense in depth. A successful bundle contains:

- route-project JSON;
- styled FBN/IRM MOP workbook;
- one CLI candidate, annotated review, validation report, and manifest per
  shelf;
- route validation report;
- bundle manifest and hashes.

For each shelf, the validation and manifest records distinguish `configured`,
`deferred`, and ILA-`prohibited` COLAN states and whether COLAN commands were
emitted. The route validation report also lists each ordered shelf's COLAN
state and whether its candidate contains COLAN commands. A successful bundle
can therefore contain a complete set of reviewed
factory-staging candidates while some terminals intentionally omit optional
COLAN; all other route, provider, topology, and on-box validation gates remain
unchanged.

When the project originated from a route diagram, the MOP also contains the
normalized source content on the `Diagram` tab. Project JSON stores only
path-free hashes/provenance, so a reopened project requires local
**Reattach Diagram…** before preview or export. The reattached source must
match the saved file name, source digest, normalized image records, order, and
dimensions. It is normalized locally without another vision request and is
embedded through internal OOXML relationships; a mismatch blocks rendering.

Provider manifests always record:

```text
deployment_approved: false
on_box_validate_required: true
release: RLS R4.0
generator: R40ExactConfigGenerator
supports_raman: <true or false from the exact provider>
```

They also contain `deployment_controls`, `deployment_control_count`, and
`legacy_confirmations_block_offline_generation: false`. Each applicable
control record uses `mode: automatic_background_advisory`,
`status: active_unverified`, and
`physical_verification_status: not_asserted`, alongside its provider
applicability, stage, instruction, source, and legacy-field provenance. These
machine-readable records make the procedure visible without claiming that
ATLAS inspected the shelf.

## Evidence

The command and safety audit is maintained in
[RLS_R4_0_VENDOR_AUDIT.md](RLS_R4_0_VENDOR_AUDIT.md). The principal supplied
R4.0 references are:

- `323-2051-101` — OAM Communications, Issue 2;
- `323-2051-190` — CLI Reference, Issue 1;
- `323-2051-201` — Installation Guide, Issue 1;
- `323-2051-220` — ZTP and Manual Commissioning, Issue 3;
- `323-2051-300` — Licensing, Security, and Administration, Issue 2;
- `323-2051-310` — Equipment, Facility, and Protection, Issue 4;
- `323-2051-318` — Optical Control, SPLI, and OTDR, Issue 2;
- `NTRN10WA` — RLS R4.0 Planning Guide, Issue 3.
