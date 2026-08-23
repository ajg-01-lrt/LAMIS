# BoM Compare — End-to-end Logic Flow

This document walks through the BoM Compare feature step by step, matching the
current implementation in [`gui/bom_compare_frame.py`](gui/bom_compare_frame.py).

Entry point: [`BomCompareFrame._compare`](gui/bom_compare_frame.py#L444). Inputs
are two workbook paths (Factory Live Inventory + Sales BoM); output is
`<factory_name>.BOM.COMPARE.xlsx` written next to the Factory file.

---

## 1. Load both workbooks ([L460-462](gui/bom_compare_frame.py#L460))

```python
fac_data = openpyxl.load_workbook(factory_path, data_only=True)
sal_data = openpyxl.load_workbook(sales_path,   data_only=True)
```

The `data_only=True` flag pulls the **cached value** Excel computed for every
formula cell, not the formula string itself. Source Sales BoMs often have
`=VLOOKUP(..., named_range, ...)` and the named ranges don't follow a sheet
copy — values-only avoids `#NAME?` and circular-ref prompts on open.

## 2. Find the BOM aggregate sheet on each side ([L464-465](gui/bom_compare_frame.py#L464))

`_find_bom_sheet` iterates sheets looking for one with both a Part-Number header
and a Total/Qty header within the first 25 rows. Factory side is required;
Sales side falls back to the first sheet if nothing matches.

## 3. Parse the aggregate BOM tab → per-side dicts ([L468-469](gui/bom_compare_frame.py#L468))

`_parse_per_site_bom(ws)` returns `(sites, parts, inline_spares, part_to_group)`.

### Inside the parser ([L840-1178](gui/bom_compare_frame.py#L840))

- **Header detection** scans rows 1-25 for `Part Number` + a Total-style header
  (`Total`, `Total Qty`, `Total Ordered`, or `Qty` as fallback). Anchors
  `hdr_row`, `pn_col`, `desc_col`, `total_col`.
- **Layout decision** ([L944](gui/bom_compare_frame.py#L944)): if
  `total_col - left_anchor ≤ 1`, it's the new builder layout (sites past Total
  → gate is `c == total_col`). Otherwise legacy (sites before Total → gate is
  `c >= total_col`). This guards against legacy-BoM metadata columns
  (Customer / Unit Price / License points / Item Code) leaking as fake sites.
- **Site column discovery**: every column between `left_anchor` and the gate.
  Each candidate is filtered against `_NON_SITE_HEADERS` (price/cost/network/
  category/etc.), `_EXCLUDED_SECTIONS` (maintenance/software/license), and
  qty-header keywords. Spares-named columns route to `spares_col`.
- **Rollup column detection** ([L985+](gui/bom_compare_frame.py#L985)): site
  columns are grouped by leading-letter key (e.g. `MALN001_7250`, `MALN Extra
  Materials`, `MALN` → all key `MALN`). When the group has both a bare-named
  column AND distinguishing siblings, the bare column is treated as a
  sum-of-others rollup and dropped to avoid double-counting.
- **Pass 1** ([L1014+](gui/bom_compare_frame.py#L1014)): iterates data rows.
  Banner detection (`Maintenance`, `Software`, etc.) flips `section_excluded`.
  Description filter rejects intangibles (`SUBSCRIPTION`, `SERVICE`, etc.).
  For each surviving row, sums per-site cells via `_qty_cell_to_int` (tolerates
  `"7"` and `'7'` quoted strings); when `row_total > 0` the part lands in
  `out[pn]` with desc + site_qty. Spares column on the same row accumulates
  into `spares_out`.
- **Pass 2** ([L1151+](gui/bom_compare_frame.py#L1151)): catches PNs with
  zero per-site qty but positive Spares. For each such row, accumulates spares
  qty AND captures the row's description into `out[pn]` so spares-only PNs
  (like `3HE11286AA`) don't surface with blank descriptions downstream.

## 4. Alias folding ([L487-506](gui/bom_compare_frame.py#L487))

`load_part_aliases()` reads `data/part_aliases.json` (alias-PN → canonical-PN).
`fold_aliased_parts` collapses both `fac_parts` and `sal_parts` to canonical
SKUs, summing per-site qtys when two aliases collapse together. The
vendor-prefix strip (`1P`/`P`) is applied first so `1P3HE13584AA` matches
`3HE13584AA` in the alias map.

## 5. Canonicalize Sales spares ([L511-516](gui/bom_compare_frame.py#L511))

`sal_inline_spares` is a flat `{pn: qty}` dict; we re-key it through
`canonical_part`, summing duplicates. Done separately from step 4 because the
parts-dict fold operates on nested site_qty structures.

## 6. Build Factory spares pool ([L521-535](gui/bom_compare_frame.py#L521))

```python
spares = fac_inline_spares or self._parse_spares(fac_data)
```

Two paths:

- Inline Spare column on the Factory BOM aggregate (preferred).
- Separate Spares tab (`_parse_spares` runs `_parse_per_site_bom` on the
  spares sheet).

Then alias-fold to canonical keys. This is the **backfill pool** — units
sitting unallocated, available to satisfy shortfalls.

## 7. Authoritative recalculation from per-site detail tabs ([L543-554](gui/bom_compare_frame.py#L543))

`_aggregate_from_site_tabs(wb, bom_sheet_title)` walks every sheet that *isn't*
the BOM aggregate or Summary. For each:

- `_detect_detail_tab_layout` finds the header row.
- **Sales-style tabs**: `Part Number` col B, `Quantity` col D → one row = one
  aggregated PN; sum col D.
- **Factory-style tabs**: `PART NUMBER` col D, no qty col → one row = one
  physical unit; implicit qty=1.
- Sheets with `"spare"` in the title go to a separate `spares_total` dict.
- PN cells get split on `\n` (handles `"NMA-8509\nQUEST TECHNOLOGIES"` →
  `"NMA-8509"`).
- Rows whose Description contains `LICENSE` are dropped — matches the
  aggregate parser's section-banner filter for Software/Services.

Returns `(per_site_total, spares_total, desc_map, visited_tabs)`.

## 8. Merge detail-tab descriptions into aggregate parts ([L560-571](gui/bom_compare_frame.py#L560))

Spares-only PNs that the aggregate parser captured into `inline_spares` but
not into `parts` get a `parts[pn] = {desc, site_qty: {}}` entry seeded from
`*_dt_desc`. Without this, the Shortfall sheet's desc lookup would still come
up blank for spares-only PNs even with the Pass 2 fix above.

## 9. Choose total source per side ([L582-609](gui/bom_compare_frame.py#L582))

Detail-tab numbers take precedence:

- If `*_dt_persite` or `*_dt_spares` is non-empty → use them (alias-folded via
  `_fold_qty_dict_through_aliases`).
- Otherwise → sum `parts[pn]["site_qty"].values()` from aggregate (fallback
  for legacy workbooks without detail tabs).

`spares` (Factory backfill) and `sal_inline_spares` get the same treatment.

## 9a. Discontinued-part exclusion ([data/part_aliases.json](data/part_aliases.json) `excluded_parts`)

`load_excluded_parts()` reads the `excluded_parts` list from the alias
JSON (each entry `{"pn": ..., "reason": ...}`). These SKUs are dropped
from the comparison entirely — they never appear on Shortfall, Site
Allocation, or Trace, regardless of which side lists them. Applied in
the per-PN compare loop (`if pn in excluded_parts: continue`). Used for
vendor-EOL hardware like the CFP2 MDAs (`3HE16718AA`, `3HE12518AA`).
The exclusion is COMPARE-only — the Sales BoM builder still carries
these parts (so the sanitized BoM is complete); only the comparison
drops them.

**License Points is not a counted source.** Sales BoMs sometimes carry
a "License Points" column; it was found to produce false positives (a
license-accounting bucket, not hardware orders), so any column/tab
whose header contains "license" is filtered (`_EXCLUDED_COLUMN_HEADERS`
+ `_DETAIL_TAB_SKIP_TITLES`). The "Network" column remains a real qty
source.

## 10. Kit folding ([L610-625](gui/bom_compare_frame.py#L610))

`load_part_kits()` reads the `kits` section of `part_aliases.json`. For each
kit definition (e.g. `3KC48900AA` = panel + fan + shelf): if **every**
component has count > 0 on that side, compute `k = min(component_counts)`,
subtract `k` from each component's total, add `k` to the kit SKU's total.
The "all-components-present + min" rule. Spares stay tied to their original
SKU — they're not consumed into kits.

## 11. Build the comparison rows ([L636-673](gui/bom_compare_frame.py#L636))

Union every PN seen anywhere:

```
sal_parts.keys() | fac_parts.keys()
                 | fac_persite_total.keys() | sal_persite_total.keys()
                 | sal_inline_spares.keys() | spares.keys()
```

For each PN:

```python
total_ordered = sal_persite_total[pn] + sal_inline_spares[pn]
                # Sales spares ARE part of the customer's order
live_total    = fac_persite_total[pn] + spares[pn]
                # Factory spares ARE on hand
delta         = live_total - total_ordered
```

Description picks first non-empty: Sales → Factory → kit-definition fallback.
Rows with both totals zero are skipped.

## 12. Sort ([L677](gui/bom_compare_frame.py#L677))

```python
compare_rows.sort(key=lambda r: (r[4], r[0]))
```

Ascending delta puts shortages (negative) at the top, then ties broken by PN.

## 13. Write the Shortfall sheet ([L686-690](gui/bom_compare_frame.py#L686))

`_write_missing_sheet` writes the 6-column layout:

| Item | PN | Description | Total Ordered | Live Inventory | Difference |
|---|---|---|---|---|---|

Cells are colored **red** for negative delta, **green** for positive,
**bold-black** for exact match.

## 13a. Write the Site Allocation sheet ([L717-745](gui/bom_compare_frame.py#L717))

`_per_site_breakdown_from_detail_tabs` walks the Factory per-site detail
tabs and returns `{pn: {site_title: qty}}` plus an ordered list of site
titles that actually contributed. Same license/keyword filtering as
`_aggregate_from_site_tabs`; preserves the site dimension instead of
summing across sites.

`_write_site_allocation_sheet` then renders one row per Shortfall PN
(same order) with one column per Live site. Each populated cell shows
the qty at that site; coloring inherits the row's Shortfall delta:

* **Red** font when `delta < 0` (PN is short overall) — operator scans
  red rows to see where the existing stock physically sits.
* **Green** font when `delta > 0` (PN is surplus) — shows distribution
  of the extras.
* **Bold-black** at zero.

Empty site cells stay blank (no zeros) so the eye skips past sites
that don't carry the part. Frozen panes at D5 keep Item / PN / Desc
columns and the header rows visible while scrolling.

Alias folding is applied to the breakdown keys before writing so a
Shortfall row keyed on the canonical SKU (`3HE11278AA`) finds units
captured under any registered alias (`3HE13584AA`).

## 14. Copy source BoMs into the output ([L692-707](gui/bom_compare_frame.py#L692))

`workbook_builder.copy_sheet(..., copy_images=False)` clones the Factory BoM
as `"Live Inventory"` and the Sales BoM as `"Sales BoM"`. Images are skipped
because openpyxl's BytesIO image rebuild produces drawing XML that triggers
Excel "Repaired Records" prompts.

## 15. Normalize the copied sheets ([L716-719](gui/bom_compare_frame.py#L716))

For both copies:

1. Clear stale freeze panes from source's saved scroll position.
2. `_reset_sheet_view_to_defaults` to clear multi-pane Selection corruption.
3. Re-apply `freeze_panes = "D1"` so Item / PN / Description stay visible
   while scrolling sites.

## 16. Live Inventory branding ([L724-725](gui/bom_compare_frame.py#L724))

`_add_live_inventory_banner` pastes the template's LightRiver banner onto the
Live Inventory tab (replaces the source's stripped banner with a well-formed
one).

## 17. Strip Sales price columns ([L730-737](gui/bom_compare_frame.py#L730))

`_strip_sales_price_columns` scans the first 25 rows of the Sales BoM copy
for headers matching `Unit Price`, `List Price`, `Ext Price`, `Cost`,
`Price`, etc., and **hides** those columns (doesn't delete — preserves
VLOOKUP/SUMIF references and title-row merges).

## 18. Recompute the Total column on copied source BoMs ([L745-754](gui/bom_compare_frame.py#L745))

`_recompute_total_column_on_source_copy` walks the copied tabs and overwrites
the rightmost Total column with plain integer sums of per-site cells. Source
workbooks often store Total as a SUM formula whose cached value is `None`
when not saved through Excel — that would leave Total blank on the copy and
disconnect the Shortfall numbers from what's visible.

## 19. Pin tab order ([L760-763](gui/bom_compare_frame.py#L760))

```
Shortfall | Live Inventory | Sales BoM
```

Shortfall is what the operator opens to first; the two source copies are
reference material for verifying numbers by hand.

## 20. Save ([L766-768](gui/bom_compare_frame.py#L766))

`_derive_out_path` builds `<factory_basename>.BOM.COMPARE.xlsx` next to the
Factory file. `out_wb.save(out_path)` writes it. Returns the absolute path;
the worker thread logs it.

---

## Key invariants the pipeline preserves

1. **Numbers are verifiable by hand.** Detail-tab totals (step 7) are the
   source of truth — open any per-site tab, count rows for a PN, the COMPARE
   will agree.
2. **Aliases collapse, kits roll up.** Bundle/component pairs (step 4) and
   kit groupings (step 10) match real shipping behavior so a chassis-bundle
   order on Sales doesn't show as a phantom shortage against bare-chassis
   Live stock.
3. **Spares mean different things per side.** Sales spares = customer ordered
   extras (counted in Total Ordered). Factory spares = unallocated backfill
   pool (counted in Live Inventory). Both sides feed into the same per-PN
   delta.
4. **Section banners filter intangibles.** Maintenance / Software / Licenses
   are dropped at both the aggregate (banner-driven, step 3) AND detail-tab
   levels (description-keyword filter, step 7) — they're not shippable
   equipment.
5. **Reference copies stay valid.** Steps 14-18 sanitize the source BoMs into
   well-formed copies the operator can correlate the Shortfall against,
   without `#NAME?` cascades or "Repaired Records" prompts.

---

## Quantity-cell normalization

`_qty_cell_to_int` ([L98-127](gui/bom_compare_frame.py#L98)) is centralized so
every conversion site handles the same edge cases consistently:

| Input | Output |
|---|---|
| `None`, `""` | `0` |
| `7` (int) | `7` |
| `7.0` (float) | `7` |
| `"7"` (numeric string) | `7` |
| `'"7"'` (quoted numeric string) | `7` |
| `"  7 "` (whitespace) | `7` |
| `"N/A"`, `"#REF!"`, `"hello"` | `0` |

Applied at:

- Pass 1 per-site qty loop
- Pass 1 spares column
- Pass 2 spares column
- Banner-row qty check (excluded-section detection)
- Detail-tab parser qty column

## Files referenced

| Path | Role |
|---|---|
| [`gui/bom_compare_frame.py`](gui/bom_compare_frame.py) | The whole pipeline |
| [`gui/workbook_builder.py`](gui/workbook_builder.py) | `copy_sheet` used to clone source BoMs into the output |
| [`data/part_aliases.json`](data/part_aliases.json) | Alias map + kit definitions |
| [`data/BOM_Template.xlsx`](data/BOM_Template.xlsx) | LightRiver Live Inventory banner template |
| [`tests/test_bom_compare_aliases.py`](tests/test_bom_compare_aliases.py) | 116 unit + source-level guard tests |
