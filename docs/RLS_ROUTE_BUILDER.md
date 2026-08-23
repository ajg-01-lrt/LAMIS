# Ciena RLS Route Builder

## Purpose

The route builder turns a customer route diagram into an ordered set of Ciena
RLS shelf records, a reopenable project, and a controlled FBN field-deliverable
bundle. It supports routes containing different shelf types under the fixed
RLS R4.0 software contract.

Diagram extraction accelerates data entry; it does not replace engineering
review. Extracted values remain candidates until a person reviews and accepts
them. The Route Builder is the single offline RLS configuration-review entry
under **Provisioning**.

## Diagram-first workflow

Use the Route Builder in this order:

1. Select **Upload Route Diagram…** and confirm that the customer diagram may
   be processed by the configured diagram-AI service. Cancel if the diagram
   contains information that is not approved for that service.
2. ATLAS extracts visible route facts: route identity, OSPF area, sites,
   shelves, order, TIDs, OAM addresses, chassis/release/variant labels,
   RAMAN/power display annotations, and optical-link facts such as endpoints,
   loss, distance, circuit, and fiber. Each candidate retains its extraction
   evidence and confidence. A directly observed terminal-pair header anchors
   A/Z orientation and can correct an otherwise complete reverse transcription.
   Diagram AI never constructs a provider request.
3. The extracted route enters **Pending human review**. Select each shelf in
   **Ordered route shelves** to inspect or correct its populated fields. Use
   **Move Up** and **Move Down** when the customer drawing does not reflect the
   intended installation order. The **Provider** column shows the applied
   exact provider, the single route-compatible review candidate, or a clear
   unresolved/unsupported state for every shelf before its exact editor is
   opened. The adjacent **Direction map** column shows an applied or safely
   derived review-only mapping of the provider's first physical degree/path
   to route side A or Z, or an explicit unresolved state. These columns are
   review context and never authorize CLI.
4. Confirm or correct every value the diagram supports. Leave unresolved values
   visibly blocked; ATLAS does not turn an inferred value into approved
   engineering data merely because its confidence score is high.
5. With the shelf selected and its visible edits applied, select **Review
   Configuration…**:

   - an RLS R4.0 Add/Drop, ILA, or ROADM role opens the exact-provider editor.
     When project-aware constraints leave exactly one compatible audited
     layout, ATLAS prepopulates that non-executable review candidate in the
     provider dropdown. The readonly dropdown remains available and
     automatically includes every compatible provider added to the audited
     catalog. A candidate is hardware-qualified only when direct evidence also
     establishes either its complete inventory or its complete fixed line map
     with matching chassis and represented-degree coverage. A directly
     evidenced C+L route scope may narrow the review list; it never proves the
     shelf by itself. ATLAS may also seed which
     route side the provider's first fixed local line-output faces when direct
     local port evidence uniquely matches the immutable map. Both results are
     non-executable suggestions and require operator confirmation. A role,
     diagram color, catalog uniqueness, or chassis label by itself never
     confirms hardware. Ambiguous, conflicting, unsupported-SRA, and
     no-compatible-provider results remain unselected. Validate the exact
     build, fixed BOM, OAM, physical-side assignment, and both A→Z and Z→A
     propagation paths. ATLAS automatically includes the provider-specific
     deployment-control procedures, then the operator applies the versioned
     payload to that
     shelf. The older workbook field contract and assumption narrative remain
     visible as context only.

6. Select **Preview MOP** to render a temporary draft from the current reviewed
   data. Preview is available for planning while review or
   configuration-readiness blockers remain; it does not publish a final
   deliverable.
7. Complete all required values and accept the human review. Any route,
   shelf, link, or applied-configuration change makes the previous MOP preview
   stale, so preview the current snapshot again.
8. Select **Export Route Bundle…** only after the route passes the complete
   route, provider, input, and validation gates. ATLAS then renders the final MOP
   and the per-shelf CLI candidates together and publishes one atomic bundle.

There is no separate RLS config mode, **Export Reviewed Bundle**, or **Export
Styled FBN MOP** action. Per-shelf review happens inside Route Builder, MOP
preview is a required review step, and the final MOP/configuration set is
created only by **Export Route Bundle…**.

## Diagram extraction and review boundary

Diagram AI requires an explicit privacy confirmation for each upload. The route
diagram and its network-design content may be sent to the configured AI
provider, so the operator must verify customer authorization before continuing.

ATLAS uses the dedicated route-diagram vision profile with reasoning effort
set to `high`. It stores extracted text or visual evidence and a confidence
value with each candidate where the provider returns them. Evidence and
confidence help the reviewer locate the source; neither is an approval
decision. Ambiguous profile classifications or conflicting values remain
review items.

For a large standalone PNG or JPEG, ATLAS sends one route overview followed by
deterministic, overlapping detail views so small labels remain readable without
losing the full-route connections. Detail views are ordered top-to-bottom and
left-to-right, but optical connectors—not tile order—determine shelf order.
Each evidence record cites its exact view, and the saved provenance includes
that view's source-pixel crop. Image count and aggregate normalized bytes are
bounded before the AI provider is called. DOCX images retain their existing
document-relationship order and are not subdivided.

Evidence locations must be positive normalized image rectangles. Structured
output can constrain each component to `0..1`, but it cannot express the
cross-field `x + width <= 1` rule. ATLAS therefore has one narrow,
schema-versioned recovery for model rounding at the right or bottom edge: an
overflow no greater than `0.025` may be clipped to the source boundary only
when at least 60% of every reported dimension remains. The saved evidence
records both provider and normalized rectangles, retained fractions, policy
ID, and `deployable_cli: false`, while
`EVIDENCE_BBOX_EDGE_CLIPPED` remains a nonblocking review advisory.

ATLAS never shifts an origin or expands a rectangle. Zero-area, negative,
individually out-of-range, excessive-overflow, or mostly discarded rectangles
still produce blocking `INVALID_EVIDENCE_BBOX` and are removed. The remaining
route and shelf candidates can open as a review draft only when discarded
evidence is not required for route identity or topology. If critical evidence
was discarded, ATLAS preserves the current route and rejects the incomplete
transcription. Discarded evidence cannot satisfy field validation or authorize
configuration payloads.

### Direct-header A/Z orientation

A directly printed terminal-pair header is authoritative for route orientation
only when it is supported by matching direct header evidence and corroborated
by direct TID evidence on the two returned endpoints. If the returned shelf
chain matches the printed pair, ATLAS retains its order. If the entire chain is
the exact reverse, ATLAS reverses shelf and span order and swaps each
`preceding`/`following` endpoint adjacency before assigning route sides.

If the printed pair matches neither direction, ATLAS raises
`ROUTE_ORIENTATION_MISMATCH` and leaves A/Z unresolved instead of guessing.
The saved orientation record includes the A and Z terminal TIDs/codes, observed
header pair, whether provider order was reversed, and
`deployable_cli: false`.

After that normalization, A is the first distinct active terminal site and Z
is the last distinct active terminal site. For an intermediate shelf,
`preceding` is A-facing and `following` is Z-facing. Multiple shelves at one
terminal retain the same endpoint side. This source-backed orientation drives
route profiles and side-keyed review facts; it does not by itself select a
provider or authorize CLI.

### Bidirectional span and hardware-degree semantics

Every ordered physical adjacency carries two traffic propagations: A→Z and
Z→A. The saved project keeps one physical fiber-pair/span record because the
diagram commonly supplies one shared circuit, distance, fiber pair, and loss
annotation. ATLAS derives two propagation views without duplicating that
evidence:

- the ordered from-shelf endpoint review is A→Z egress; and
- the ordered to-shelf endpoint review is Z→A egress.

Missing endpoint engineering remains missing; ATLAS never substitutes the
facing shelf's loss or link name. A 15-span route consequently reports 30
propagation reviews while retaining 15 physical spans.

For ROADM and Add/Drop RLA hardware, a physical line degree is bidirectional:
its line mux transmits one propagation and its paired line demux receives the
reverse propagation. A terminal's single route-facing degree therefore
already carries both directions. The exact one-degree C+L RLA12/LRU12
terminal-core provider consequently exposes one line record and no second
degree tab. If an audited two-degree provider shows a blank second tab, that
tab is additional hardware outside the modeled route—not the return path—and
requires separate installed inventory and engineering.

The DLE ILA provider has different semantics. Its two records are
unidirectional through-paths. PFG-1-to-2 uses its output-side peer downstream
and the opposite-side peer upstream; PFG-2-to-1 swaps them. Reusing one
neighbor on both ends is blocked.

The vision importer records visible facts and does not ask the AI provider to
invent engineering defaults. After import, the Route Builder supplies the
controlled route-role power standard: ILA is `DC`, while Add/Drop and ROADM
are `AC`. The value carries ATLAS-default provenance, remains editable, and
follows a corrected shelf role only while the operator has not replaced it.
An explicit diagram or operator-entered power value is preserved. Other
provider-specific values remain review items. A blank RAMAN display label is
optional, but it is never interpreted as “No Raman” and never overrides
structured SRA callout evidence. Software release is also controlled by the
tool: the current target is fixed to `RLS R4.0`; an explicitly observed
different release is preserved for rejection and is never rewritten.
The exact-provider review separately prepopulates the editable build/schema
candidate as the vendor-documented R4.0.0 baseline `4.00.00`. This is
unverified planning metadata, not a claim about the running shelf. A customer
shelf may run a later R4.0 build such as `4.00.01`, so successful on-box
`validate` is mandatory before commit.
Missing OAM IP or chassis text also enters pending review instead of causing a
coherent route transcription to be discarded. When the visible TID has a
safe `PREFIX-suffix` form, missing site metadata may also enter pending review:
ATLAS offers the prefix as an editable site-code suggestion with explicit
non-source provenance, while the site name remains blank and unresolved. The
vision prompt separately asks for the exact visible city/location line as
`site_name`; it never derives that value from the TID. A shelf with neither
source site data nor a usable TID prefix remains a structural failure.
An unresolved shelf role enters pending review as **Unresolved — select shelf
role** and cannot authorize MOP preview, provider review, or bundle export
until the operator selects the visible Add/Drop, ILA, or ROADM role. Missing
TID, shelf order, span count, or span continuity remains a structural import
failure and leaves the existing route unchanged. Rejection logs include the
exact structural field paths without recording raw customer values.

### Source-scoped RAMAN/SRA notation

The supplied ELP1–SAT4 diagram and matching reference MOP use a small red
`slot/port` convention for SRA span endpoints. In this source pair, `4/5`
means slot 4, port 5—not slot 4, subslot 5. Port 5 is the SRA line-out and
port 6 is its line-in. The final-span callouts therefore show:

- `4/5` and `4/6` at `USXGN1-L8I2`, one SRA in slot 4; and
- `6/5` and `6/6` at `USSAT4-L8R3`, the bookending SRA in slot 6.

The small red `3/5` and `3/6` samples below the diagram legend explain that
source's notation. They are context, not another shelf or span endpoint, and
must be excluded from the route census. Red color alone is not a portable
Ciena convention: another customer's red text must remain uninterpreted unless
that source supplies the same unambiguous legend or the operator confirms the
meaning.

These callouts are directional path-endpoint facts. The importer retains the
source-bound shelf, slot, line-out port 5, line-in port 6, raw labels, source
rectangles, and operator review state. The two registered exact SRA providers
may influence review only after that structured evidence is accepted and
matches their fixed module/PEC, slot, line endpoint, route side, and paired
bookending contract. A PEC may be recorded only from reviewed inventory or
another explicit source. The two callouts must not be collapsed into a string
such as `4/5,4/6`, treated as subslots, or converted directly into executable
provider input.

The FBN register intentionally uses a different representation. It derives the
shelf-level display values `Slot 4` and `Slot 6` after the paired endpoint
facts are associated with their shelves. That display text remains separate
from the structured SRA evidence and never acts as a RAMAN Boolean or CLI
toggle.

After the facts-only import, the editor may add controlled workflow values that
are not diagram evidence: deliverable revision `1` when no revision was
printed, the fixed product-scope release `RLS R4.0`, the safe alphanumeric
prefix before the first hyphen in a TID as an editable site-code suggestion,
and directly evidenced chassis text as an editable shelf-variant suggestion
when no exact variant/PEC was printed. Each value retains explicit provenance.
Site-code and chassis suggestions remain inside the pending-review boundary
and require operator acceptance. None can authorize CLI or substitute for an
exact provider discriminator.

Route-title prepopulation uses a separate deterministic rule. When a directly
printed header pair is corroborated by the first and last active terminal TIDs,
ATLAS removes their shared `US` prefix and uses the remaining terminal labels.
For example, header pair `USELP1-USSAT4` with terminal TIDs `USELP1-L8R2` and
`USSAT4-L8R3` yields the editable title `ELP1-SAT4`. The original header and
terminal-TID evidence remains preserved. `Ciena RLS` remains a product
descriptor, and the printed `RL-...` value remains `route_code`; neither is
appended to the title. This rule does not change per-shelf site-code
suggestions, which remain review-only, and it cannot authorize CLI. The
reviewed title drives the FBN headline and IRM route name; the same
source-bound terminal display codes drive the IRM A/Z cells only while the
derivation marker still matches the title, source hash, and ordered endpoint
TIDs.

If the operator changes the shelf order, adds a shelf, or removes a shelf,
ATLAS recomputes endpoint A/Z roles and the controlled terminal title from the
new first and last terminal TIDs. It also invalidates the old diagram-relative
line-port direction suggestion, because its original `preceding`/`following`
adjacency no longer proves the edited topology. Exact payloads are cleared and
must be reviewed again.

An explicitly printed route-header optical band is transcribed separately as
`optical_band` and requires matching direct evidence. ATLAS preserves a
supported observation such as `C+L` as read-only exact-review context. It is
not copied into every shelf, treated as a software release, or sufficient to
select an exact provider/BOM. It can narrow advisory selection only after
complete direct module inventory or the complete fixed line-output map with
matching chassis and represented-degree coverage independently passes. When a
direct per-shelf band is absent, it also acts as a negative compatibility
guard: a provider with a conflicting optical band is removed from review,
Apply, stored-payload readiness, and export. A matching high-confidence direct
shelf band may document an intentional band partition. A missing or unverified
band remains blank.

After a structurally successful import, logging reports
`route_integrity=accepted` and aggregates raw source-transcription findings by
controlled issue code and normalized field path. It then reports a separate
post-import accounting of unresolved required values, controlled defaults,
pending suggestions, optional omissions, and lifecycle-excluded omissions.
The raw count remains intact for audit; it is not presented as the number of
operator values still missing. Shelf indexes are collapsed and customer
values/messages are not copied into those aggregates.

For the latest supplied ELP1–SAT4 transcription, the 85 raw missing-field
findings account to zero unresolved required values, 33 controlled defaults
(release, revision, and the 16 role-derived POWER labels), 32 pending
site-code/chassis suggestions, 19 optional display/span values, and one
omission on the planned-removal shelf. All 15 active spans carried direct
`LEAF` labels in that run, so no fiber value was scope-inherited. RLS R4.0
commissioning printed p.199 and the audited legacy workbook both emit the
exact `LEAF` token, so ATLAS may preselect that same token for operator
review. Preselection is not confirmation: the operator must still apply the
route-wide choice before configuration review.
The 16 imported shelves, 15 physical spans, and 30 derived A→Z/Z→A
propagation reviews still require explicit human review, so `0 ready` is
expected immediately after import.

This transcription review is distinct from the later configuration
deployment-readiness assessment, which reports ready/blocked shelf counts and
aggregated readiness reason codes. It also reports reviewed shelves, applied
exact-provider payloads, reviewed propagation paths, reviewed physical spans,
and generated in-memory candidate counts so `0 ready` is actionable rather
than ambiguous.

Role-only classification may use explicit role text attached to a shelf or an
unambiguous visible diagram legend. A legend-based result retains separate
evidence for the individual shelf box and the matching legend entry. It never
selects chassis modules, topology, an exact R4.0 provider, or CLI. TID naming,
chassis labels, ports, and route position are not accepted as substitutes for
role evidence.

Physical labels such as `R2`, `R4`, `R6-300`, `R8-300`, or `R4/R2 600mm`
populate chassis/variant evidence only. If one is returned in the
software-release field, ATLAS retains the original evidence but clears the
editable release value and raises a blocker.
Likewise, Add/Drop, ILA, and ROADM are retained as site roles; they do not
authorize an inferred module/slot/topology configuration.

### Advisory provider and direction-map suggestions

ATLAS can narrow the registered R4.0 catalog without creating an exact request.
The **Provider** column in **Ordered route shelves** exposes this result for
every shelf:

- **Applied** means a current, strictly decoded exact request is attached;
- **Suggested** means direct hardware evidence qualifies the one compatible
  provider for advisory preselection;
- **Candidate** means one route-compatible catalog provider remains but its
  installed hardware identity is incomplete;
- **Select provider (N compatible)** means the reviewed facts still allow more
  than one provider; and
- RAMAN/SRA, conflict, stale-payload, and no-compatible-provider labels remain
  visibly blocked.

These labels are deliberately not readiness states. Only successful validation
and **Apply Reviewed Configuration** attach an exact request.

The selected review starts with the editable, unverified `4.00.00`
build/schema baseline. Physical frame/rack identity is optional and may remain
blank. Deferred optional terminal COLAN is not a missing exact-CLI fact:
**Validate & Preview** generates the remaining factory-staging candidate with
no COLAN commands and a visible warning. If another mandatory fact is still
missing, the action instead displays a visibly watermarked GUI-only planning
review. That planning view carries no CLI, artifact, manifest, hash, or exact
payload. **Copy** and **Apply Reviewed Configuration** remain disabled only
for those actual planning blockers. The planning review cannot invoke the
route apply callback, satisfy readiness, or enter an export bundle. Once the
required values are supplied, the same action resumes the ordinary strict
validation path.

The adjacent **Direction map** column provides the same at-a-glance distinction
for the provider's immutable first line record. For an RLA provider it reports
which route side physical degree 1 faces; for the DLE it reports which route
side amplifier path 1 line-output faces. A current applied payload is shown as
applied. Otherwise ATLAS derives a suggestion only when exactly one
project-compatible provider survives and the mapping is safe:

1. high-confidence direct local line-output evidence is matched to that
   provider's immutable slot/port map; or
2. only when local endpoint observations are genuinely absent, the audited
   provider/route-role convention supplies a controlled fallback.

The compact notation uses `D` for an RLA bidirectional degree and `P` for a DLE
amplifier path. For example, `D1→Z (both flows)` means the one physical degree
faces Z and its mux/demux pair carries both A→Z and Z→A; `D1→Z / D2→A` and
`P1→Z / P2→A` show two fixed records. The label prefix distinguishes an
operator-applied map from a direct-evidence result or a controlled derived
fallback.

Ambiguous, conflicting, low-confidence, invalidated, unsupported-SRA, stale,
multiple-provider, and no-compatible-provider cases remain visibly unresolved.
The direction suggestion does not select installed hardware, satisfy a
required input, create an exact payload, or authorize CLI. Opening the exact
editor retains the A/Z selector so the operator can confirm or correct the
mapping. Reordering shelves recomputes route sides and invalidates any
diagram-relative direction suggestion that no longer proves the edited
topology.

The advisory provider resolver treats every supplied fact as a constraint and
uses only retained, matching direct evidence for hardware discriminators:
release, chassis family/PEC, per-shelf optical band, topology, add/drop
structure, protection type, module PEC/slot/subslot inventory, and SRA state.
The reviewed route role limits the compatible catalog but is never sufficient
to confirm installed hardware. The route-header optical band remains read-only
route context. It may narrow the review list to one catalog candidate, but it
cannot qualify the BOM or authorize CLI.

An `exact_match` requires every fixed discriminator, the complete directly
observed module inventory, and an explicit matching SRA observation. A
`unique_candidate` means only one registered provider remains compatible but
some exact evidence is still missing. Catalog uniqueness is not hardware
identity. ATLAS may populate that sole candidate in the review dropdown so the
operator can inspect it without reselecting the obvious catalog result. It is
marked **Candidate** until direct evidence also establishes the complete
provider module inventory, or the complete fixed line-output map plus matching
chassis and represented-degree coverage. Generic chassis, no-SRA,
no-protection, and a lone terminal endpoint cannot pass that qualification
gate. Ambiguous, conflicting, unsupported-SRA, and route-band-mismatched facts
leave the dropdown blank. Every resolution retains reason codes,
matched/missing fields, project-filtered provider IDs, degree counts, and
`deployable_cli: false`.

ATLAS maps the oriented route adjacency to A/Z, then compares a high-confidence
direct local line-out port—and the direct slot when the port is not unique—to a
compatible candidate's fixed local output map. The direct mapping can remain
visible as advisory context. After the sole compatible provider is populated
for review, a resolved match may seed **physical Degree 1 faces A/Z** for RLA
hardware or **amplifier Path 1 line-out faces A/Z** for a DLE. When endpoint
observations are genuinely absent, the audited provider/role convention
(`*_a`, `*_z`, or DLE ILA) may supply a controlled, non-executable fallback.
Ambiguous, low-confidence, incompatible, or conflicting endpoint evidence
never falls back. Ordered shelves also cross-check a lone terminal's inferred
preceding/following label.
The operator must still confirm the provider, installed BOM, port/side
assignment, and complete request before **Apply Reviewed Configuration** can
attach a versioned payload.

The live vision schema records visible facts only. It never fills product
defaults or synthesizes executable provider input. Only successful operator
validation followed by **Apply Reviewed Configuration** attaches the exact
versioned R4.0 provider payload.

The supplied `Doc1.docx` example is image-only. It can provide enough visual
information for a route draft and MOP preview, but it does not contain all
required per-shelf configuration values. ATLAS correctly blocks final Route
Bundle publication until the missing values are reviewed and completed.

## Route model

Each project stores:

- project ID, route code, title, OSPF area, revision, notes, and passive
  route-header provenance such as an explicitly evidenced optical band;
- a normalized site register;
- an ordered shelf register;
- ordered route links keyed to adjacent shelf IDs, with one canonical shared
  physical optical span containing loss, distance, fiber type/range, circuit
  ID, review state, and evidence, plus endpoint-local egress reviews from
  which ATLAS derives A→Z and Z→A propagation views;
- for every shelf: profile, software release, variant/PEC, site, TID, primary
  OAM IP, RAMAN display label, power label, notes, and an optional versioned
  profile payload;
- diagram-only structured configuration facts: topology, band, add/drop
  structure, protection type, and exact module inventory entries containing
  PEC, role, slot, and subslot.

Fiber type is reviewed once for the active route. Route Builder displays the
diagram label separately, provides one **Native CLI fiber type** selector, and
uses **Apply to all spans** to seed every optical path. The native choice must
be one exact audited RLS R4.0 token and is recorded on every path with
operator-confirmation provenance. A directly evidenced uniform `LEAF` label
may preselect the exact `LEAF` token, but it is never silently converted to
`Enhanced LEAF` or marked confirmed. Changing the route-wide
native token clears exact provider payloads and endpoint-path reviews, returns
the paths to pending, and invalidates the MOP preview and bundle readiness.

When exact R4.0 review opens, ATLAS carries forward every applicable reviewed
project fact: TID and site identity, primary OAM candidate, OSPF area, adjacent
neighbor TID, route circuit context, native fiber token, directional loss,
distance, source fiber range, and original diagram fiber label. Passive span
values that are not exact request fields are shown as read-only context. A
privacy-safe log entry reports counts of carried fields, controlled
derivations/defaults, and remaining manual review groups plus their controlled
field keys, without serializing customer values or CLI.

The selected provider supplies the endpoint-local CLI link object name. The
one-degree C+L core and two-degree RLA providers use `LM1-LINEOUT` and, when
present, `LM2-LINEOUT`; the DLE uses `PFG-1-2-LINEOUT` and
`PFG-2-1-LINEOUT`. These are not the customer route circuit ID. A repeated
circuit value such as `BDJW7353` remains shared route context and is never
copied into multiple provider-local link-name fields.

### Review prepopulation precedence

ATLAS fills the review surface in this order:

1. matching direct diagram evidence;
2. an audited exact-provider or workflow fixed/default value when the diagram
   does not supply that field; and
3. operator entry for anything still unresolved.

This is a prepopulation order, not a rule that prevents correction. A direct
diagram value replaces a conflicting historical or audited default, while an
explicit operator correction becomes the reviewed value and is validated
against route/provider invariants. A conflict with an immutable provider
layout eliminates that provider candidate; ATLAS does not rewrite an audited
PEC, slot, subslot, or port map. Defaults never masquerade as diagram evidence.

The historical workbook contributes only its audited column-B literal
`<...>` field contract. ATLAS maps those cells to named, typed review fields;
it never evaluates the workbook formulas or imports its command/prose column,
cached output, URLs, passwords, or license-registration material. Provider
defaults include `/32` loopback behavior and, where applicable, 0.5-dB
input/output patch loss, 2-dB repair margin, and 3-dB high-loss threshold.
They remain editable review values and cannot establish installed hardware.

The no-SRA one-degree C+L RLA12/LRU12 provider fixes RLA12 `NTK852BC` in
slot 1, `NTK591VQ` OSC at 1/50, and LRU12 `NTK852NA` in slot 3. It generates
only the audited core: LM1/LD1 on 1/53 and 1/54, the C/L SM1/SD1 functions,
and internal links `LRU3-LINEIN` (1/11→3/52), `LRU3-LINEOUT`
(3/51→1/12), and `LRU3-MON` (1/10→3/50).

`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6` is the exact SAT sibling. The
operator-confirmed `USSAT4-L8R3` core inventory adds C+L SRA `NTK830AC` in
slot 6, uses external 6/5 and 6/6, and fixes `RLA1-SRA6` 1/53→6/4 plus
`SRA6-RLA1` 6/3→1/54. This is still a one-degree terminal core. It does not
infer CCMD/client/add-drop packouts, unrelated remote or express WSS peer
half-links, another degree, or protection.

`R40_R2_CL_DLE_S1_SRA4` is the paired exact USXGN provider. It fixes R2
`NTK803DA`, slot-1 C+L DLE `NTK850DC`, `NTK591VQ` OSCs at 1/50 and 1/60, and
C+L SRA `NTK830AC` in slot 4. The represented SRA side uses external 4/5 and
4/6 plus `RLA1-SRA4` 1/63→4/4 and `SRA4-RLA1` 4/3→1/64; the other DLE side
remains on 1/53 and 1/54. A second SRA, dual rail, cascade, DLA substitution,
protection, and COLAN remain excluded.

When accepted structured callouts and reviewed inventory are compatible,
Route Builder may populate these provider choices and their fixed directions
for at-a-glance review. The dropdown remains editable among compatible
providers. Prepopulation is non-executable: only Validate/Preview followed by
Apply Reviewed Configuration creates the exact payload.

### Paired-SRA staged review

The two exact SRA providers form one reciprocal route contract, but the editor
must still let the operator review one shelf at a time. Either endpoint can be
reviewed first. If its local provider, identity, inventory, accepted SRA
slot/port evidence, direction, and represented route path are valid, and its
only unresolved topology issue is the absence of the facing exact SRA payload,
**Apply Reviewed Configuration** saves it as **Staged — paired SRA peer review
pending**. Route Builder then offers the facing shelf as the next review item.
The facing shelf must already have reviewed facts, exact `RLS R4.0`, a
role/band-compatible audited SRA provider candidate, and accepted matching slot
evidence. Missing or conflicting peer eligibility is not stageable.

That first payload is not deployable and is not counted as a completed
reciprocal pair. It cannot make the route CLI-ready, cannot satisfy Route
Bundle readiness, and cannot authorize a partial export. The state remains
visible after save/reopen because ATLAS derives it from the current exact
payload and the still-missing compatible peer; it is never promoted merely by
persistence.

When the operator applies the second endpoint, ATLAS builds one candidate
route snapshot containing both proposed payloads and validates the reciprocal
contract in that snapshot. Both fixed provider layouts, structured SRA
evidence and ports, route-side assignments, neighbor TIDs, provider-local
link/PFG identities, fiber, and endpoint-local loss relationships must agree.
Only then does ATLAS promote the pair together into the normal exact-readiness
evaluation. The review order does not matter.

The staging exception is narrow and fail-closed. It applies only when the
compatible peer payload is absent and every other local and topology check
passes. A wrong SRA slot or port, incompatible/no-SRA peer, nonreciprocal
neighbor or PFG, stale project snapshot, or any other provider,
identity, or path mismatch rejects Apply without attaching the new payload.
If the second endpoint fails, the first remains explicitly pending. Editing
either paired provider, its fixed direction assignment, or a bound
adjacent-path/topology value invalidates the reciprocal result and returns
both endpoints to staged review before any route CLI or bundle can be built.

The route regression contract verifies this at the full 16-shelf
ELP1–SAT4 level. `USXGN1-L8I2` resolves to
`R40_R2_CL_DLE_S1_SRA4`, with fixed amplifier path 1 facing Z, side A
neighbor `USKP21-L8I2`, and side Z neighbor `USSAT4-L8R3`.
`USSAT4-L8R3` resolves to
`R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6`, with its one degree facing A,
`USXGN1-L8I2` on that side, and no synthesized Z-side span. The USELP
terminal and all 13 non-SRA ILAs remain on their no-SRA candidates. If any
source SRA callout is still unassigned, ATLAS may not infer no-SRA merely from
the absence of an attached callout.

The audited RLA12 switch map is SW11 output/input 41/42 and SW12 output/input
43/44. The legacy `ROADM_A!B221` SW12 row duplicated the slot-3 SW11 41→42
formula; ATLAS treats it as a copy defect and uses the corrected fixed map.

COLAN follows deployment policy rather than spreadsheet defaults. Add/Drop and
ROADM terminals open in an explicit **Deferred — omit COLAN for factory
staging** state with every COLAN value blank. This is an optional,
all-or-nothing feature choice: ATLAS generates and may bundle the remaining
exact candidate, emits no COLAN interface or routing commands, shows warning
`TERMINAL_COLAN_DEFERRED`, and records the deferred state in the per-shelf and
route-bundle manifests. The operator changes the state to configured only when
a complete customer-approved design is available. ATLAS does not invent
`colan-x`, reuse the diagram OAM/loopback address, or manufacture a prefix,
gateway, metric, or routing mode. ILA has a fixed **COLAN prohibited** policy
and rejects COLAN payload data. NTP is customer-managed and is omitted from
the editor, payload, and generated CLI.

Legacy `B4 <rack location>` is optional and remains blank unless the source or
operator explicitly supplies a physical rack/location fact. ATLAS does not
reuse a site code or TID as a rack value and does not invent a missing site or
TID. When B4 is blank, the generator omits the complete
`set shelf shelf-location ...` command, including bay and physical-shelf
values; it can be entered on site later if required. The historical COLAN-X
and COLAN-A triplets are represented as alternative terminal interfaces. In
configured mode, exact review selects one complete customer-provided
`colan-x` or `colan-a` record and does not emit both legacy blocks. In deferred
mode, it emits neither block.

If a shelf directly prints exactly `R2 600mm` or `R4 600mm` and vision places
that text only in the shelf-variant field, ATLAS also retains the same source
region as inferred chassis review context. This narrow normalization does not
accept bare R2/R4 labels, PECs, mixed chassis labels, or arbitrary variants,
and it cannot prove a module BOM, topology, or deployable CLI. The resulting
review fact may only constrain the separate non-executable provider candidate
resolver.

The prepopulated, unverified target build/schema candidate, fixed hardware
line/side mapping, remote PFG identities, OSCPFIB when separately engineered,
unrepresented external degrees, and installed BOM still require review when
they are not directly evidenced or fixed by an explicitly selected audited
provider. Add/Drop and ROADM COLAN is an optional customer-provided input and
is never inferred from the diagram OAM address. Deferred means the whole COLAN
block is omitted; configured means every selected COLAN value is strictly
validated. ILA shelves have no COLAN. NTP is customer-managed and is neither
requested nor emitted by the RLS Route Builder. The role-only assumptions
panel receives a filtered facts contract so editor-only context cannot
suppress the report.

### Automatic deployment controls and expected manual inputs

The exact-provider editor no longer presents a **Confirmations** tab. ATLAS
automatically adds the selected provider's deployment-control procedures to
the validation preview, annotated CLI, per-shelf validation report, and bundle
manifest. The route validation report summarizes the same boundary.
**Confirm & Next Pending** remains a review of imported shelf facts; it neither
verifies physical conditions nor grants deployment approval.

| Automatic control | Provider applicability | Behavior and limit |
|---|---|---|
| Expected inventory/topology procedure | All six exact providers | ATLAS validates the request against the provider-fixed chassis, OMC2 requirement, occupied slots, PECs, port map, degree/path count, direction assignment, and reviewed route topology. The installer still compares the actual shelf; ATLAS has not observed it. |
| Runtime-tuning and calibration boundary | All six exact providers | ATLAS omits runtime tuning, licensed-feature activation, and calibration from pre-calibration CLI and points to the separately approved engineering procedure. This may be a customer/Ciena package, including PlannerPlus where applicable; PlannerPlus is not assumed to be LightRiver's tool. |
| Recorded target-build/schema candidate | All six exact providers | ATLAS prepopulates the vendor-documented `4.00.00` R4.0.0 baseline as editable, unverified metadata. It does not assert that value is installed; a later R4.0 build such as `4.00.01` may be present, and successful on-box `validate` remains mandatory before commit. |
| Disconnected-fiber and inactive-control staging procedure | Both C+L DLE ILA providers | The report requires line fibers disconnected and Span Calibration/Passive Terminal Control inactive while the disabled-SCO candidate is staged. ATLAS cannot detect cable or live feature state. |
| Unused-CFIM treatment procedure | C-band CDC RLA32 ROADM only | The report carries the audited loopback/dust-cap procedure. ATLAS cannot inspect the physical CFIM ports. |
| SRA span bookending and colocation | Both exact SRA providers | The reviewed USXGN–SAT span must have compatible accepted SRA endpoints at both ends, and each endpoint must match its provider's fixed PEC, slot, ports, serving amplifier, and internal express-link map. |
| Approved SRA runtime engineering | Both exact SRA providers | A separately approved fiber-specific procedure must supply gain, safety, mixed-fiber, activation, and calibration engineering. ATLAS never derives those values from visible loss or distance. |
| SRA OTDR go/no-go and alarm gate | Both exact SRA providers | The vendor go/no-go result and RAMAN activation-inhibited alarm clearance are required before enablement or accepted calibration. ATLAS does not perform or attest to those field checks. |

The universal controls apply to all six providers. Both DLE providers receive
their two additional staging controls, the CDC RLA32 receives its CFIM control,
and both SRA providers receive the three SRA controls. They are included
automatically and cannot be skipped through the review UI, but they are
advisory procedures—not checked attestations, sensor results, or proof of
work. They emit no claim of physical verification, do not set
`deployment_approved`, and do not replace mandatory successful on-box
`validate`.

The two SRA generators also stage the fixed SRA circuit pack and its
`LINE-IN` RAMAN facility disabled. A successful on-box `validate` is required
before commit, but it does not authorize RAMAN enablement. The later go/no-go,
alarm-clearance, enablement, and calibration steps remain in the separately
approved runtime procedure.

Legacy confirmation values retained in an older schema-1.4 payload are not
treated as observations or deployment authorization. Reopening and applying a
review uses the automatic provider-control model. The payload preserves those
six Boolean fields only for strict compatibility: either value is
non-authoritative, `false` does not block offline candidate generation, and
`true` does not prove a physical condition.

Some fields cannot be populated safely when the customer diagram does not
contain them. ATLAS therefore supplies the vendor-documented `4.00.00`
build/schema candidate and marks it as defaulted/unverified; it does not claim
to have observed the running shelf, which may instead be on a later R4.0 build
such as `4.00.01`. Clearing that candidate or entering only a generic release
label remains invalid. Physical rack/frame identity is optional: a blank value
produces a warning and omits the entire shelf-location command. A terminal
placed in configured numbered COLAN mode without a complete customer-provided
design produces strict validation errors such as
`INVALID_IPV4: management.ip_address`. Selecting deferred instead uses a
canonical blank management record, emits no COLAN commands, and permits the
remaining exact candidate. ATLAS does not synthesize a rack value, site, TID,
or COLAN address to bypass these boundaries. ILA shelves continue to prohibit
COLAN entirely.

Strict required-field errors still block exact payload creation and export,
but they do not hide the useful reviewed data. While required customer or
engineering facts are pending, the exact editor can render a watermarked
planning review in GUI memory only. It emits no command-looking text and
creates no `R40ExactRequest`, `R40ConfigArtifact`, encoded payload, manifest,
or hash. Copy, Apply, per-shelf CLI export, and Route Bundle publication remain
unavailable. Any edit clears the transient planning view. This is separate
from the watermarked MOP preview and does not count as an applied shelf
configuration.

The current `raman_label` remains only a deliverable field. The two registered
SRA-capable providers require the separate reviewed path-endpoint structure
described above; ATLAS never decodes executable SRA topology from
`raman_label`.

The supported planning profiles are:

| Profile | Rack role | IRM bucket | CLI status |
|---|---|---|---|
| Add/Drop — A endpoint | Add/Drop A | Add/Drop A | Advisory CDA candidate only when direct hardware facts uniquely support it; operator review required |
| Add/Drop — Z endpoint | Add/Drop Z | Add/Drop Z | Advisory CDA candidate only when direct hardware facts uniquely support it; operator review required |
| Add/Drop — intermediate/side unresolved | Add/Drop | Total Add/Drop only; A/Z detail unresolved | Same fail-closed advisory resolver; no role-only selection |
| ILA | ILA | ILA | Advisory R2 C+L DLE no-SRA or exact SRA4 candidate and direction may be seeded from compatible direct hardware/port evidence; operator review required |
| ROADM — A endpoint | ROADM A | ROADM | Advisory one-degree C+L core (no-SRA or exact SRA6) or two-degree C-band CDC candidate only when compatible direct hardware evidence supports it; operator review required |
| ROADM — Z endpoint | ROADM Z | ROADM | Advisory one-degree C+L core (no-SRA or exact SRA6) or two-degree C-band CDC candidate only when compatible direct hardware evidence supports it; operator review required |
| ROADM — intermediate/side unresolved | ROADM | ROADM | Same fail-closed advisory resolver; no role-only selection |

R2 and R4 in the reference MOP identify physical chassis families, not software
releases. ATLAS does not infer chassis or hardware variant from a selected
planning role.

The newly supplied RLS 4.0 manuals do support Add/Drop, ILA, ROADM, protected
ROADM, and additional DCI families. A role name alone still cannot select CLI:
RLS 4.0 supports multiple chassis, bands, RLA/DLA/DLE variants, add/drop
structures, degrees, protection arrangements, and local/remote port layouts.
ATLAS therefore requires an exact discriminator and audited provider rather
than routing a generic role into the unsafe workbook adapter. See
[RLS 4.0 vendor audit](RLS_R4_0_VENDOR_AUDIT.md).

For review visibility, ATLAS retains the deterministic non-executable legacy
assumption catalog. Its report shows diagram facts, hidden workbook
assumptions, known unsafe/incomplete assumptions, and missing decisions. It is
context inside the R4.0 Fixed Hardware tab—not a Ciena default, provider
selection, payload, or CLI source. Only the exact R4.0 generator can satisfy
provider readiness.

A/Z assignment is based on the first and last distinct route sites, not merely
the first and last shelf rows. Multiple shelves at one endpoint retain the same
side. Intermediate ROADMs and Add/Drop shelves remain in route order with
neutral profiles; an intermediate Add/Drop contributes to the total count but
is reported as IRM-unmapped until its A/Z-specific material bucket is resolved.

## Validation

ATLAS records validation findings while the project is edited. A draft save or
MOP preview can retain blockers so review can continue. Final Route Bundle
publication requires all of these checks to pass:

- required route, site, shelf, TID, IP, release, variant, and power fields;
- human acceptance of diagram-extracted candidates and resolution of ambiguous
  or missing evidence;
- unique project shelf IDs, TIDs, and primary OAM addresses;
- valid site/profile references and IPv4 addresses;
- case-insensitive site and TID collisions;
- route-order endpoint and distinct-site counts;
- a valid first-class OSPF area and contiguous ordered route-link adjacency;
- both A→Z and Z→A endpoint-egress reviews and provider/path consistency;
- a confirmed first-line-output route-side assignment; a direct-evidence
  suggestion may seed the field, but ATLAS still maps reviewed A/Z span facts
  onto the matching physical degrees or amplifier paths and requires operator
  validation;
- endpoint-local egress link names and asymmetric directional loss without
  collapsing the two propagation views of a shared physical span;
- provider-fixed local CLI link names kept distinct from repeated route circuit
  IDs;
- JSON-safe profile data;
- obvious credential-bearing payload keys such as passwords, secrets,
  community strings, private keys, tokens, and license keys;
- complete, release-authorized provider inputs for every shelf before final
  bundle publication.
- no confirmed SRA endpoint evidence paired with a no-SRA exact provider;
- a vendor-audited SRA-capable provider for every accepted SRA endpoint;
- no incomplete propagation review or pending/manual physical span at bundle
  publication;
- exact `RLS R4.0` on every shelf; any other release raises
  `UNSUPPORTED_SOFTWARE_RELEASE`.

A saved route is controlled network-design data even though credential material
is prohibited. Saving a project or generating a MOP preview does not mean that
the route is configuration-ready. Final export additionally requires a
watermarked MOP preview whose fingerprint matches the current project.

## Dynamic FBN behavior

The embedded template is:

`data/templates/Ciena_RLS_FBN_MOP_Template.xlsx`

Its controlled source SHA-256 is:

`a1ad017b977441d657d7e4e500343ead3fe7ffea5e692d749c6d7ac960e71e11`

This is the sanitized controlled template: the two unused generic sheets and
the old route-specific Diagram pictures were removed, while the requested
static tabs and the 222 IRM formula nodes were retained. The uploaded source is
inserted into the now-neutral Diagram drawing at export time.

The exporter copies the complete OOXML package and patches only the dynamic
FBN/IRM content plus workbook calculation/print metadata when necessary. It
does not round-trip the workbook through openpyxl because that would discard
unsupported connector shapes, drawings, printer settings, and custom XML.

FBN behavior:

- shelves remain in entered route order;
- a rack contains at most eight shelf records;
- rack diagrams repeat to the right until every shelf is represented;
- the route summary and SITE/TID/IP/RAMAN/POWER table move to the right of the
  final rack;
- a machine-readable shelf-type value accompanies each table row so material
  counts are traceable to the FBN shelf register;
- print area, landscape orientation, and fit settings are recalculated from the
  generated bounds.

RAMAN is stored in the FBN table as derived display text, such as `Slot 4` or
`Slot 6`; it is not a Boolean and never enables CLI behavior. In the controlled
ELP1–SAT4 reference workbook, `FBN!W30` is `Slot 4` for `USXGN1-L8I2`,
`FBN!W31` is `Slot 6` for `USSAT4-L8R3`, and `FBN!Z19` labels the affected
span group as `Spans with RAMAN`. The directional `slot/port` evidence remains
outside this display column.

## Dynamic IRM behavior

IRM receives route code, A/Z endpoint codes, distinct-site count, and the ILA,
ROADM, Add/Drop-A, and Add/Drop-Z shelf counts from the same shelf register used
to render FBN. The existing template formula nodes and formula text are
preserved. Formula display is turned off and Excel is instructed to perform a
full recalculation when the workbook opens.

Protected-DCI shelves have no valid bucket in the supplied IRM and are reported
as unmapped instead of being silently counted as another chassis family.

The exact inherited formula behavior is retained, including any source-template
formula anomalies. Engineering approval is required before changing the
controlled IRM formula set.

## Preserved workbook tabs

The following requested tabs and their relationships, drawings, pictures, and
printer-setting parts are copied from the template:

1. Ciena FBN Checklist
2. Ciena FBN Procedure
3. Packouts
4. Fibering
5. FBN Test Setup
6. Debug
7. Test Channels CIENA
8. FBN Script Ciena
9. Label Standards
10. Tear Down

The sanitized template also retains `Diagram` and `Fiber-Label Template`.
The two unused generic `Sheet1` and `Sheet2` tabs from the customer source were
removed during template control. `Diagram` becomes dynamic when the project
came from an uploaded route diagram.

## Embedded Diagram behavior

For a diagram-backed project, ATLAS embeds the locally normalized uploaded
diagram into the MOP workbook's `Diagram` tab. It does not embed the
overlapping detail tiles created only for vision extraction. A standalone PNG
or JPEG contributes its single normalized full source image; a DOCX contributes
its normalized embedded source-image occurrences in document-relationship
order.

The representation is bounded canonical RGB PNG
(`canonical-rgb-png-v2-max2048`). Each source occurrence is deterministically
reduced to at most 2,048 pixels on its longest edge before workbook rendering.
Pictures use internal OOXML image relationships, retain aspect ratio, are
never enlarged beyond native size, are capped at 16 inches wide, and are
stacked in source order. The source route file is not published separately,
and the workbook contains no external image relationship. The bundle manifest
records source and normalized SHA-256 digests, the render bound, image
occurrence/unique-media counts, dimensions, and the `Diagram` sheet name.

Route-project JSON intentionally persists only path-free, hash-only diagram
provenance; it does not retain source paths or pixel bytes. The current ATLAS
session holds the validated normalized pixels in memory. After reopening a
saved project, select **Reattach Diagram…** and choose the original local
source before previewing or exporting. Reattachment is local normalization,
not another vision request, and must match the saved file name, source digest,
image order/count, normalized digests, and dimensions. Missing, stale, or
mismatched content fails closed with a reattachment error instead of rendering
the wrong diagram. Preview and final bundle rendering use the same validated
attachment contract.

## Route bundle

After every shelf passes the route-wide provider and input gates and the
current project has a matching MOP preview, **Export Route Bundle…** publishes
a new directory named:

`<route>_RLS_route_deliverable`

Existing deliveries are never overwritten; later exports use `_2`, `_3`, and
so on. Publication is staged and renamed atomically so a renderer failure does
not leave a partial bundle. Final configuration generation and validation run
again from the current route snapshot inside that atomic operation; an
in-memory preview or earlier payload is not blindly copied into the delivery.

| Artifact | Purpose |
|---|---|
| `<route>_RLS_route_project.json` | Reopenable normalized project snapshot |
| `<route>_RLS_route_FBN_MOP.xlsx` | Final styled FBN/IRM field deliverable |
| Per-shelf pre-calibration CLI candidate | Generated only by one of the six exact audited R4.0 providers |
| Per-shelf validation and annotated review artifacts | Command provenance, warnings, readiness state, selected COLAN state, and automatic provider-specific deployment controls |
| `<route>_RLS_route_validation.txt` | Route counts, extraction-review result, provider status, and route-wide blockers |
| `<route>_RLS_route_manifest.json` | Project/template metadata, provider/readiness state, and SHA-256 hashes for every published artifact |

The CLI outputs are documented pre-calibration candidates, not deployment
approval. Their manifests identify that live schema/on-box validation and the
separate customer-approved runtime-tuning/calibration procedure—including
PlannerPlus when applicable—are still required. Secret material is not
included.

Each per-shelf manifest exposes the applicable records in
`deployment_controls`, with `mode: automatic_background_advisory`,
`status: active_unverified`, and
`physical_verification_status: not_asserted`. It also records
`deployment_control_count` and
`legacy_confirmations_block_offline_generation: false`; none of these fields
changes `deployment_approved: false` or
`on_box_validate_required: true`.

The same manifest records `colan_policy`, `colan_state`, and
`colan_commands_emitted`; the route-bundle candidate record repeats those
fields for at-a-glance audit. A deferred terminal is therefore visibly marked
and contributes a valid factory-staging candidate with zero COLAN commands.
The route validation report lists the same state and emission result for every
ordered shelf.
Configured terminals must carry one complete, strictly validated customer
record. ILA remains `prohibited`. None of these states removes the mandatory
successful on-box `validate` boundary.

Publication fails closed for the whole route. If one shelf is unsupported,
unreviewed, incomplete, or fails provider validation, ATLAS publishes neither a
partial configuration set nor a final bundle. A missing or stale current MOP
preview is also rejected before a destination is chosen. **Export Route
Bundle…** first runs a fresh synchronous deployment-readiness preflight. A
blocked route shows grouped operator actions and starts no folder dialog,
background worker, or artifact staging; the exporter repeats the same gate as
defense in depth. The operator may still save the project and use **Preview
MOP** to continue resolving the draft.

## CLI deployment boundary

The RLS 4.0 manuals support broad equipment families, but the generic
Add/Drop, ILA, and ROADM
records in the MOP/workbook do not specify one exact chassis/module/topology
variant. The workbook-derived output also contains confirmed invalid commands,
fixed slots/ports, missing protection and license state, and unsafe calibration
assumptions. It remains quarantined.

Therefore:

- mixed Add/Drop, ILA, and ROADM shelf-type routes can be planned, saved,
  counted, and documented under RLS R4.0;
- **Confirm & Next Pending** commits one imported shelf review and advances to
  the next pending shelf without bulk-accepting any evidence;
- a reviewed role-only shelf displays **Confirmed - CLI Pending** until an
  exact compatible R4.0 provider payload is applied; this status confirms the
  shelf facts only and does not authorize CLI;
- **Review Configuration…** is the single selected-shelf review entry;
- after shelf-fact review or one exact configuration is applied, ATLAS selects
  and offers the next shelf that has an available exact-provider review and
  reports completed/remaining progress;
- the exact-review queue is passive: it never opens the next modal
  automatically, and closing or canceling without a successful Apply neither
  marks the shelf complete nor advances the queue;
- exact configuration review remains closed until every imported shelf fact
  and every structured RAMAN callout has an explicit operator disposition;
- a generic R4.0 role remains CLI-pending; the sole route-compatible provider
  may be populated for review, while a direct-evidence provider/line map is
  labeled separately as hardware-qualified. The operator must confirm and
  validate the compatible exact provider in either case;
- a fact-confirmed shelf with a compatible exact provider but no applied
  payload reports `EXACT_PROVIDER_REVIEW_REQUIRED`; only a layout with no
  compatible registered provider reports
  `PLANNING_ONLY_PROVIDER_NOT_IMPLEMENTED`;
- exact R4.0 output is limited to the audited two-degree C-band CDA Add/Drop,
  two-degree C-band CDC ROADM, one-degree C+L RLA12/LRU12 terminal core
  (no-SRA or exact slot-6 SRA), and R2 C+L DLE ILA (no-SRA or exact slot-4
  SRA) layouts;
- confirmed SRA evidence is incompatible with the four no-SRA providers and
  must match the fixed slot/port, inventory, bookending, and route-side
  contract of `R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6` or
  `R40_R2_CL_DLE_S1_SRA4`; a RAMAN-label toggle is never sufficient;
- both exact SRA payloads are SRA-disabled pre-calibration candidates. They
  require successful on-box `validate` before commit and a separately approved
  OTDR go/no-go, alarm-clearance, activation, and calibration procedure before
  RAMAN enablement;
- either exact SRA endpoint may be reviewed first. A locally valid first
  endpoint is labeled **Staged — paired SRA peer review pending**, remains
  non-deployable, and keeps route CLI and Route Bundle publication blocked;
- applying the facing endpoint validates both SRA payloads reciprocally in one
  candidate route snapshot and promotes the pair together only when every
  provider, slot/port, direction, neighbor, PFG/link, fiber, and loss
  relationship passes. A failed second review makes no partial mutation;
- provider, fixed-direction, or paired-path/topology edits invalidate the
  reciprocal result and require both SRA endpoints to be reviewed again;
- terminal exact providers may either emit one complete, strictly validated
  customer-provided COLAN design or remain explicitly deferred with blank
  values. Deferred factory-staging candidates emit no COLAN commands, remain
  eligible for Apply and atomic Route Bundle publication, and carry a visible
  warning/manifest state; ILA providers prohibit COLAN;
- NTP is outside the ATLAS RLS deployment scope and no exact payload or
  generated CLI contains NTP settings;
- imported route facts are stored by directly oriented A/Z side. A unique
  direct port-to-provider match may seed which side the first fixed local
  line-output faces. When that mapping is unresolved, review opens the
  represented side as explicitly labeled staging data instead of clearing
  the available line records; the selector remains blank and validation stays
  blocked. Confirming or changing the assignment remaps complete side records
  without discarding edits;
- for a two-degree terminal provider, an endpoint degree outside the uploaded
  route remains blank and
  requires independent neighbor, link, and loss engineering. It is additional
  hardware, not reverse traffic: the represented RLA degree already carries
  both A→Z and Z→A. The one-degree provider has no such second record, and
  ATLAS never copies the route-facing span into another degree;
- the legacy assumption report contains no CLI and never satisfies readiness;
- planning-only shelves never emit invented CLI;
- a route containing Add/Drop, ILA, ROADM, or another profile without a
  registered audited provider fails configuration readiness as a whole;
- another software release or a retired exact payload blocks the whole route;
  schemas 1.2 and 1.3 can be deliberately re-reviewed into current schema 1.4,
  but are never regenerated silently;
- partial route CLI or configuration bundles are never published;
- a current MOP preview is required and any project edit invalidates it;
- a successful Route Bundle contains every eligible shelf's exact candidate,
  validation artifacts, the final MOP, and their hashes together.

Add a new exact RLS discriminator, evidence set, generator, and regression
suite before enabling any additional arrangement.
