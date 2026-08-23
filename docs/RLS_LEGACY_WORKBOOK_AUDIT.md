# Ciena RLS C+L CLI Config v3.5LR workbook audit

## Outcome

The workbook is useful as historical engineering context, but it is not safe
to use as an executable configuration generator. Its output mixes
commands, prose, URLs, interactive prompts, and stored secret material; several
formula defects select the wrong hardware, port, or loss input.

The R4.2 generator, editor, export path, adapter, extraction tool, and sanitized
fixture have been removed from ATLAS. The active implementation does not port
these workbook profiles into production. It retains only the separately
audited literal column-B `<...>` field contract as non-executable review
metadata.

The original `.xlsx` was inspected read-only and was not modified.

## RLS 4.0 follow-up

The later-supplied RLS 4.0 manuals do support Add/Drop, ILA, ROADM,
protected-ROADM, and broader DCI families. That does not validate the workbook
profiles.

The RLS 4.0 audit found additional blockers in every workbook-derived profile:
an invalid `commit —> quit` line, broken batch boundaries, incomplete OAM,
wrong OSC OSPF IDs, fixed/unverified slots and remote ports, missing protection
and license state, unsafe Raman/fiber rules, and blanket TDA power assumptions.
The labels also hide single hard-coded designs rather than generic shelf types.
The adapter therefore remains quarantined for RLS 4.0 as well. See
[Ciena RLS 4.0 vendor audit](RLS_R4_0_VENDOR_AUDIT.md).

## Workbook inventory

- Nine visible worksheets
- 677 formulas, including 398 single-cell array formulas
- 16 `_xlfn.XLOOKUP` formulas
- No macros, external workbook links, defined names, hidden sheets, or sheet
  protection
- No `Diagram` worksheet, drawing, chart, or embedded image
- Filename identifies `v3.5LR`, while the visible revision history ends at
  `v2.9`
- Audited source SHA-256:
  `c07c2efecad3e6bb312e6825a3216845a53ecfc330ee37b1707873efd4eb5905`

## Audited column-B field contract

The replacement does not treat all non-formula cells as inputs. The historical
operator fields are the literal angle-bracket placeholders in column B before
the command/procedure region. ATLAS records their worksheet/cell provenance
and maps only supported entries to named, typed review fields:

| Route role / worksheet | Supported historical input cells | Handling |
|---|---|---|
| Add/Drop A / `AddDrop_A` | `B3` shelf name, `B4` rack location, `B5` loopback, `B10:B12` COLAN-X, `B15:B17` COLAN-A, `B18` route neighbor | Identity/OAM/neighbor fields plus optional terminal COLAN |
| Add/Drop Z / `AddDrop_Z` | The same cells plus `B7` OSPF area | Same; `B7` is sourced from the reviewed route when directly printed |
| ILA / `ILA 1` | `B3` shelf name, `B4` rack location, `B5` loopback, `B7` OSPF area, `B8` A-facing neighbor, `B17` Z-facing neighbor | No COLAN; two route-facing neighbor fields |
| ROADM A / `ROADM_A` | `B3` shelf name, `B4` rack location, `B5` loopback, `B7` OSPF, `B10:B12` COLAN-X, `B15:B17` COLAN-A, `B18` route neighbor, and `B29:B40` WSS neighbor placeholders | Supported terminal fields are editable; WSS entries remain unsupported-provider context |
| ROADM Z / `ROADM_Z` | The same cells as `ROADM_A` | Same handling |

The legacy Add/Drop-A constant `B7 = 10.6.6.0` is not a trusted customer value;
matching direct route-header OSPF evidence supersedes it. The helper sheet
`tx-proxy_pwr-profile!B3`, downstream `ping <...>` instructions, and the 96
ROADM formula occurrences that compare cells to `<neighbor name>`—48 on each
ROADM worksheet—are outside the route input contract. They are command/helper
content or sentinels, not additional operator fields.

The legacy COLAN-X (`B10:B12`) and COLAN-A (`B15:B17`) rows are alternative
interface records in the integrated exact-provider review. ATLAS exposes one
customer-selected `colan-x` or `colan-a` interface with its address, prefix,
and applicable gateway/routing fields when COLAN is configured; it does not
emit both defective legacy blocks in one request. COLAN may instead remain
explicitly deferred for factory staging. In that state ATLAS emits neither
legacy block, generates and may bundle the remaining exact candidate, and
records a visible warning and deferred manifest state.

The field-contract renderer exposes only cell address, safe label, literal
placeholder, mapped request field, handling class, and audited fixed/default
descriptions. It
never returns workbook formulas, cached CLI, plaintext credentials, or secret
values.

## Fixed/default precedence

Review prepopulation uses this fill order:

1. matching direct diagram evidence;
2. an audited provider/workflow fixed or default value when the source did not
   show that field; and
3. operator entry for the unresolved remainder.

Thus a directly observed OSPF area, neighbor, fiber, distance/loss, or hardware
fact replaces a conflicting historical/default value. The order is only for
initial population: an explicit operator correction becomes the reviewed value
and must pass the same route/provider validation. A conflict with an immutable
exact-provider layout removes that candidate instead of changing its audited
PEC or port map. Defaults retain their own provenance and never become diagram
evidence.

Audited workflow defaults represented by the replacement include:

- loopback prefix `/32`;
- loopback/OSC OSPF in every exact candidate, with optional configured
  terminal COLAN using either the audited OSPF-GNE or static-routing model;
- 0.5-dB input and output patch-panel loss where the exact provider supports
  those fields;
- 2-dB repair margin and 3-dB high-loss minor threshold;
- no ILA COLAN; and
- no NTP fields or commands because NTP is customer-managed.

Add/Drop and ROADM COLAN remains optional customer-provided terminal data.
Configured mode is all-or-nothing: ATLAS strictly validates one selected
interface, address, prefix, routing model, metrics, and applicable gateway
before emitting it. Deferred factory-staging mode keeps every COLAN value
blank, emits no COLAN commands, and does not block the remaining exact
candidate or atomic Route Bundle. Warning `TERMINAL_COLAN_DEFERRED` and
manifest values `colan_state: deferred` and
`colan_commands_emitted: false` make the omission explicit. ATLAS never copies
the uploaded OAM/loopback
candidate into COLAN. ILA providers prohibit COLAN values. Rack location is
also operator-entered unless the source explicitly supplies that physical
fact; ATLAS never substitutes a site code or TID for legacy
`B4 <rack location>`. A successful on-box `validate` remains mandatory for
either terminal COLAN state.

## A/Z and provider suggestions

The old `_A`/`_Z` worksheet names are not evidence of a shelf's direction or
installed hardware. ATLAS establishes A/Z from a directly observed terminal
header corroborated by direct first/last terminal TID evidence. It retains a
matching chain, reverses an exact reverse transcription before assigning
sides, and rejects any other header/endpoint mismatch. After normalization,
the preceding route adjacency is A-facing and the following adjacency is
Z-facing. Consequently `ILA 1!B8` maps to the A-facing neighbor and
`ILA 1!B17` to the Z-facing neighbor.

The R4.0 provider resolver may offer a review-only candidate from direct
chassis/PEC, band, topology, add/drop structure, protection, module
slot/subslot inventory, and SRA evidence. Role-only matching is insufficient;
at least one hardware discriminator must match. A unique compatible candidate
may still report missing exact discriminators, while conflicting, ambiguous,
or unsupported-SRA facts leave the provider unresolved.

Only after that advisory provider is established can ATLAS compare a
high-confidence directly observed local line-out port—and a direct slot when
needed—with the provider's fixed direction map. A unique consistent match may
seed fixed Direction 1 as route side A or Z. These provider and direction
records always carry `deployable_cli: false`; the operator must confirm the
installed BOM, direction, and complete exact request.

## Diagram deliverable boundary

The audited CLI workbook has no `Diagram` tab. The controlled FBN MOP template
does, and ATLAS now treats it as dynamic. It embeds the locally normalized full
standalone source image, or normalized DOCX source-image occurrences in
document relationship order, using internal OOXML image relationships. Vision
detail tiles are not included.

Saved route-project JSON contains hash-only, path-free diagram provenance
rather than source pixels. After reopening, the operator uses
**Reattach Diagram…** to select the original local source. ATLAS normalizes it
locally without another vision request and verifies source filename/hash,
image order/count, normalized hashes, and dimensions before MOP preview or
export. Missing or mismatched content blocks rendering. The bundle embeds the
normalized content in `Diagram` but does not publish the original source file
or an external image link.

## Confirmed high-severity defects

| Area | Confirmed defect | Impact |
|---|---|---|
| `ROADM_A`, `ROADM_Z` | Raman and RLA branches reference numeric loss cells instead of the Raman and RLA selectors. | Raman paths and 64x1 hardware can select the wrong ports and roles. |
| `ROADM_A`, `ROADM_Z` | Raman internal links reuse the wrong patch-loss cell; the Z sheet also substitutes fiber type/distance fields for patch loss. | Generated loss values are invalid. |
| Add/Drop and ROADM | OTDR commissioning conditions test an unrelated cell instead of Raman status. | Required or inapplicable tests can be selected. |
| Add/Drop and ROADM | Static COLAN-A commands target `colan-x`. | Management addressing is applied to the wrong interface. |
| Add/Drop and ROADM | Enabling both static COLAN paths reuses the same static protocol, route, and next-hop keys. | The later block overwrites the earlier route. |
| ROADM | Several switch-link names disagree with their source ports; two WSS links are duplicated or target the wrong slot. | Link inventory and physical endpoints diverge. |
| Add/Drop | Z-side preamp slot and multiple channel-demux role names reference the wrong objects. | PFG roles bind to an incorrect or nonexistent object. |
| Tx proxy / power profile | The sheet contains 64 proxies, 65 profiles, and only 63 links; a duplicate port-150 profile replaces the missing link. | One required link is absent and the block is not transactionally complete. |
| ILA | Two formulas omit their false branch and emit Boolean `FALSE`; diagnostic commands contain mismatched smart quotes. | Copy/paste output contains non-CLI values and malformed quoting. |
| ILA | Validation offers a fiber type with no lookup row. | Loss calculation returns `#N/A`. |

The specific `ROADM_A!B221` WSS contradiction is now resolved as a workbook
copy defect. The audited RLA12 map is SW11 output/input 41/42 and SW12
output/input 43/44; B221 is labeled SW12 but repeats the slot-3 SW11 41→42
formula. The C-band SW12 half-link belongs on slot 1, 43→44, while the L-band
row uses slot 3, 43→44. This correction does not make the historical 12-peer
WSS block deployable: the customer-specific peers and complete installed
CCMD/client/add-drop packout remain unproved and are excluded from the exact
terminal-core provider.

The workbook's fixed RLA12/LRU12 core link object names are retained after
vendor endpoint verification: `LRU3-LINEIN` for 1/11→3/52,
`LRU3-LINEOUT` for 3/51→1/12, and `LRU3-MON` for 1/10→3/50. ATLAS emits
those three core links with the documented create workflow; it does not copy
the workbook's generic `set link` commissioning shortcut.

## Systemic safety gaps

- Required values, IP addresses, prefixes, FQDNs, loss ranges, slot overlap,
  duplicate links, and incompatible topology combinations are not blocked.
- Formula cells and reference values remain editable because the workbook is
  unprotected.
- Some Office `x14` validation rules are removed by common Python workbook
  libraries when the file is saved.
- Cached output can retain placeholders without blocking export.
- A generated column is not an executable script: it contains commands mixed
  with prose, URLs, interactive responses, and diagnostics.
- Shared passwords and license-registration material are stored in plaintext
  in the source workbook. Those values are deliberately absent from the
  replacement, its tests, and its export artifacts.
- Commands have no per-line traceability to a release manual or page.

## Replacement controls

The ATLAS replacement uses named, typed fields instead of cell coordinates and
adds:

- A hard exact-RLS-R4.0 release gate
- Six exact audited chassis/module/topology providers, including the
  core-only slot-6 terminal SRA and paired slot-4 DLE SRA layouts
- Exact PEC, slot, subslot, port, PFG, and link rules
- Explicit routing modes and complete initial IPv4 OAM transactions
- Planned-loss and endpoint-local topology validation
- CLI-safe identity, endpoint, link-name, and native fiber-type validation
- Explicit `batch` / `commit` / `quit` boundaries
- Paste-safe CLI separated from annotations and validation
- Secret-free, checksummed export manifests
- Automatically included pre-calibration and approved runtime-engineering
  controls (EDP, IDP, PlannerPlus output, or customer-approved equivalent);
  these are advisories, not facts ATLAS claims to have observed
- Regression coverage for the exact R4.0 providers and removed-release
  boundaries

## Deployment boundary

Passing generator validation means the request is internally consistent with
the supplied RLS R4.0 documentation. It is not a substitute for Ciena planning
tools, a peer-reviewed MOP, license verification, plant records, or validation
on the target shelf/software build.
