"""
gui/bom_compare_frame.py - "BoM Comparison" sub-mode under File Processing.

Loads a Factory BoM workbook and a Sales BoM workbook, parses each BOM
sheet for per-site quantities, and emits a new workbook with three tabs:

* Shortfall      — workbook-level comparison per part with six columns:
                   ``Item | Part No. | Equipment Description | Total
                   Ordered | Live Inventory | Difference``. Total Ordered
                   is the Sales BoM's Total column (per-site + Sales
                   spares); Live Inventory is the Factory per-site total
                   + Factory spares. Difference = Live − Ordered, colored
                   **red** when negative (short) and **green** when
                   positive (surplus). Sort is delta-ascending so the
                   worst shortages appear at the top.
* Live Inventory — values-only copy of the source Factory BOM sheet,
                   renamed to reflect "what's currently on hand".
* Sales BoM      — values-only copy of the Sales BOM sheet (price
                   columns hidden, branding artwork stripped).
"""
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from utils.helpers import get_data_dir


# Header keywords used to recognize the fixed left-side columns.
_PN_HEADERS = ("part no", "part #", "part number", "part")
_DESC_HEADERS = ("description", "equipment description")
_QTY_HEADERS = ("total", "qty", "quantity", "total qty")
_ITEM_HEADERS = ("item", "#")

# Section banners to skip when comparing — these are intangible line items
# on the Sales BoM (and equivalents on the Factory side) and must not feed
# the per-site shortfall.
_EXCLUDED_SECTIONS = (
    "unit price", "price", "maintenance", "software",
    "services", "service", "support", "warranty", "labor",
    "training", "installation", "license",
)
# Column-header version of the exclude list. "License points" columns
# are excluded here just like the banner version: the operator reported
# that counting that column produced false positives (it's a license-
# accounting bucket, not a hardware-order column), so any header
# containing "license" is dropped as a site source.
_EXCLUDED_COLUMN_HEADERS = (
    "unit price", "price", "maintenance", "software",
    "services", "service", "support", "warranty", "labor",
    "training", "installation", "license",
)
# Per-site DETAIL tab titles to skip during the detail-tab walk. These
# are non-site buckets that shouldn't contribute qty: the workbook's
# own Summary plus the License-Points pool (see _EXCLUDED_COLUMN_HEADERS).
_DETAIL_TAB_SKIP_TITLES = ("summary", "license points")
# Section banners that represent real hardware. Anything not matched here
# AND not in the excluded list is included by default so unfamiliar
# section labels don't silently drop parts.
_INCLUDED_SECTIONS = (
    "chassis", "shelf", "card", "cards", "optic", "optics",
    "amplifier", "amplifiers", "amp", "amps", "transponder", "transponders",
    "module", "modules", "passive", "passives",
)
# Description-level keywords that mark a row as intangible regardless of
# any section banner. Catches flat Sales BoMs that don't use banners and
# items like "5 YEAR – Technical Support", "Software Subscription Plan",
# "Software Release Subscription", etc.
_INTANGIBLE_DESC_KEYWORDS = (
    "subscription", "support", "maintenance", "warranty",
    "training", "installation",
    "year - tech", "year tech", "year - software", "year software",
    "release subscription", "license",
    # NSP "Feature Pack" software products — caught by the trailing
    # " fp" (e.g. "NSP NETWORK INFRASTRUCTURE MANAGEMENT FP" or
    # "NSP SERVICE ACTIVATION + CONFIG. FP"). Word-boundary match
    # via leading space avoids false positives on "SFP" / "QSFP".
    " fp", "feature pack",
    # "services" intentionally NOT listed — too broad, matches real
    # hardware like "Integrated Services Card". Software-services
    # rows are already caught by the other keywords (subscription /
    # support / maintenance / year - software).
)
# Column-header keywords that should NEVER be treated as a per-site
# column (they're price/cost/metadata columns, not site quantities).
_NON_SITE_HEADERS = (
    "unit price", "price", "cost", "ext price", "extended price",
    "list price", "msrp", "discount", "amount", "$", "usd", "currency",
    "uom", "unit", "weight", "lead time", "vendor", "manufacturer",
    "category", "notes", "comment",
    # "network" intentionally NOT excluded — Sales BoMs use a "Network"
    # column as a real qty bucket for network-wide hardware allocations
    # (cards/MDAs that aren't tied to a specific site). The row-level
    # description filter (_INTANGIBLE_DESC_KEYWORDS) still drops
    # software/license rows that happen to land in that column.
)


def _hkey(s: Any) -> str:
    """Normalize a header cell for keyword matching: lower, trim, drop trailing dots/colons."""
    return re.sub(r"[.\s:]+$", "", str(s or "").strip().lower())


def _norm_site(name: str) -> str:
    """Case/whitespace-insensitive site key for cross-workbook matching."""
    return re.sub(r"\s+", " ", str(name or "").strip()).lower()


# Vendor-prefix strip used so "1P3HE13584AA" / "P3HE13584AA" / "3HE13584AA"
# all canonicalize to the same alias key.
_VENDOR_PREFIX_RE = re.compile(r"^(?:1P|P)", re.IGNORECASE)


def _strip_vendor_prefix(pn: str) -> str:
    return _VENDOR_PREFIX_RE.sub("", (pn or "").strip(), count=1)


def _qty_cell_to_int(qv: Any) -> int:
    """Parse a worksheet cell value to a non-negative int, tolerating
    forms real-world Sales BoMs use:

    * ``None`` / ``""`` -> 0
    * plain int / float -> int(float(qv))
    * ``"7"`` (plain numeric string) -> 7
    * ``'"7"'`` (quoted numeric string — someone typed ``="7"`` or
      pasted a value with literal quotes) -> 7
    * anything else (text, ``#REF!``, ``N/A``) -> 0

    Centralized so every parser site (per-site, spares, banner check,
    detail tabs) handles the same edge cases consistently.
    """
    if qv is None or qv == "":
        return 0
    if isinstance(qv, str):
        cleaned = qv.strip().strip('"').strip("'").strip()
        if not cleaned:
            return 0
        try:
            return int(float(cleaned))
        except (TypeError, ValueError):
            return 0
    try:
        return int(float(qv))
    except (TypeError, ValueError):
        return 0


def load_part_aliases(path: Optional[str] = None) -> Dict[str, str]:
    """Load the alias → canonical part-number map from ``data/part_aliases.json``.

    Returns an empty dict if the file is missing or malformed — the BoM
    comparison still works without aliases, just without bundle/component
    correlation. All keys and values are uppercased + vendor-prefix
    stripped so lookup is uniform regardless of how the source BoMs typed
    the SKU.
    """
    if path is None:
        path = str(get_data_dir() / "part_aliases.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning(f"[BOM-COMPARE] Could not load part aliases from {path}: {exc}")
        return {}

    raw = payload.get("aliases") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        ak = _strip_vendor_prefix(k).upper()
        av = _strip_vendor_prefix(v).upper()
        if ak and av:
            out[ak] = av
    return out


def load_part_kits(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load kit-grouping definitions from ``data/part_aliases.json``.

    Each kit is ``{"kit": "<kit_sku>", "components": ["<sku>", ...],
    "kit_description": "<desc>"}``. When ALL components are present on
    a given BoM side (per-site count > 0 for each), the comparison
    rolls them into the kit SKU using ``min(component counts)``. If
    any component is missing on that side, the others stay listed as
    individual lines — see :func:`fold_kits` for the folding rule.
    Returns an empty list when the file or section is missing.
    """
    if path is None:
        path = str(get_data_dir() / "part_aliases.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    raw = payload.get("kits") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kit = entry.get("kit")
        comps = entry.get("components")
        if not isinstance(kit, str) or not isinstance(comps, list):
            continue
        kit_canonical = _strip_vendor_prefix(kit).upper()
        comp_canonical = [
            _strip_vendor_prefix(c).upper()
            for c in comps if isinstance(c, str) and c
        ]
        if not kit_canonical or not comp_canonical:
            continue
        out.append({
            "kit": kit_canonical,
            "components": comp_canonical,
            "kit_description": str(entry.get("kit_description", "")),
        })
    return out


def load_excluded_parts(path: Optional[str] = None) -> set:
    """Load the discontinued / excluded part-number set from
    ``data/part_aliases.json`` under the ``"excluded_parts"`` key.

    These SKUs are dropped from the comparison entirely — they never
    appear on Shortfall, Site Allocation, or Trace, regardless of which
    side lists them. Use for vendor-discontinued hardware (e.g. CFP2
    optics) that shouldn't be reconciled.

    Each entry may be a bare PN string or ``{"pn": ..., "reason": ...}``.
    Returns a set of canonical (vendor-prefix-stripped, uppercased)
    part numbers. Empty set when the file or section is missing.
    """
    if path is None:
        path = str(get_data_dir() / "part_aliases.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    raw = payload.get("excluded_parts") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return set()
    out: set = set()
    for entry in raw:
        pn = entry.get("pn") if isinstance(entry, dict) else entry
        if isinstance(pn, str) and pn.strip():
            out.add(_strip_vendor_prefix(pn).upper())
    return out


def compute_kit_fold_ops(
    totals_per_pn: Dict[str, int],
    kits: List[Dict[str, Any]],
) -> List[Tuple[str, int, List[str]]]:
    """Return the list of folds ``fold_kits`` would apply to
    *totals_per_pn*, without mutating anything.

    Each entry is ``(kit_sku, k_folds, [component_pns])``. ``k_folds``
    is the kit qty that gets synthesized; component counts are
    decremented by the same value. Returns an empty list when no
    kit's full component set is present.

    Used by the Trace sheet writer so the kit-folded units have a
    visible audit row under the kit SKU, otherwise the Trace's
    Live-side sum would fall short of the Shortfall's Live Inventory
    by ``k_folds`` per kit.
    """
    ops: List[Tuple[str, int, List[str]]] = []
    if not kits:
        return ops
    for kit_def in kits:
        kit = kit_def["kit"]
        comps = kit_def["components"]
        counts = [int(totals_per_pn.get(c, 0)) for c in comps]
        if not all(n > 0 for n in counts):
            continue
        k = min(counts)
        if k > 0:
            ops.append((kit, k, list(comps)))
    return ops


def fold_kits(
    totals_per_pn: Dict[str, int],
    kits: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Fold component SKU totals into their kit SKU using the
    ``all-components-present + min`` rule.

    Mutates *totals_per_pn* in place and also returns it for chaining.
    For each kit definition:

    - Look up the count of every listed component in *totals_per_pn*.
    - If ANY component has count ``0`` (or is missing), skip the kit —
      the remaining components stay listed as their own SKUs. This is
      the user's "must see all three to count as a kit" rule.
    - Otherwise let ``k = min(component counts)``. Subtract ``k`` from
      every component's total (consuming them into kits) and add ``k``
      to the kit SKU's total.

    Spares are NOT included in kit folding — they're tracked separately
    by the caller and stay associated with the original component SKU.
    """
    if not kits:
        return totals_per_pn
    for kit_def in kits:
        kit = kit_def["kit"]
        comps = kit_def["components"]
        counts = [int(totals_per_pn.get(c, 0)) for c in comps]
        if not all(n > 0 for n in counts):
            # User's rule: any component missing → no folding for this kit.
            continue
        k = min(counts)
        for c in comps:
            totals_per_pn[c] = int(totals_per_pn.get(c, 0)) - k
        totals_per_pn[kit] = int(totals_per_pn.get(kit, 0)) + k
    return totals_per_pn


def canonical_part(pn: str, aliases: Dict[str, str]) -> str:
    """Resolve *pn* to its canonical part number through the alias map.

    Handles 1P/P vendor prefixes and case differences. Falls back to the
    original (uppercased, prefix-stripped) part number when no alias is
    registered.
    """
    if not pn:
        return ""
    key = _strip_vendor_prefix(pn).upper()
    return aliases.get(key, key)


def fold_aliased_parts(
    parts: Dict[str, Dict[str, Any]],
    aliases: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Collapse aliased SKUs in a ``_parse_per_site_bom`` result into the
    canonical part number.

    Quantities are summed per site; descriptions prefer the canonical
    SKU's description when both forms are present (so the Shortfall
    sheet shows the bare-chassis text, not the bundle text). Returns a
    new dict; *parts* is not mutated.
    """
    if not aliases:
        return parts
    folded: Dict[str, Dict[str, Any]] = {}
    for pn, info in parts.items():
        canon = canonical_part(pn, aliases)
        is_canonical_input = (canon == _strip_vendor_prefix(pn).upper() == canon)
        existing = folded.get(canon)
        if existing is None:
            folded[canon] = {
                "desc": info.get("desc", ""),
                "site_qty": dict(info.get("site_qty", {})),
                # Track whether the description came from the canonical SKU
                # so a later alias entry doesn't overwrite the better text.
                "_desc_is_canonical": is_canonical_input,
            }
            continue
        for site, q in info.get("site_qty", {}).items():
            existing["site_qty"][site] = existing["site_qty"].get(site, 0) + int(q)
        # Prefer the canonical description if we have one; otherwise the
        # first non-empty wins.
        if is_canonical_input and info.get("desc"):
            existing["desc"] = info["desc"]
            existing["_desc_is_canonical"] = True
        elif not existing.get("desc") and info.get("desc"):
            existing["desc"] = info["desc"]
    # Drop the bookkeeping flag before returning.
    for v in folded.values():
        v.pop("_desc_is_canonical", None)
    return folded


class BomCompareFrame(ttk.Frame):
    """UI panel for diffing a Sales BoM against the Factory BoM."""

    def __init__(self, parent: tk.Widget, gui: Any) -> None:
        super().__init__(parent)
        self.gui = gui
        self._factory_path: Optional[str] = None
        self._sales_path: Optional[str] = None
        self._running = False
        self._setup_ui()

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        pad: Dict[str, int] = {"padx": 8, "pady": 4}

        f1 = ttk.LabelFrame(self, text="Live Inventory workbook (Factory BoM)")
        f1.pack(fill=tk.X, **pad)
        self._factory_label = tk.StringVar(value="No workbook selected")
        ttk.Label(f1, textvariable=self._factory_label, width=80, anchor="w").pack(
            side=tk.LEFT, padx=6, pady=6
        )
        ttk.Button(f1, text="Browse…", command=self._browse_factory).pack(
            side=tk.LEFT, padx=6, pady=6
        )

        f2 = ttk.LabelFrame(self, text="Sales BoM workbook")
        f2.pack(fill=tk.X, **pad)
        self._sales_label = tk.StringVar(value="No workbook selected")
        ttk.Label(f2, textvariable=self._sales_label, width=80, anchor="w").pack(
            side=tk.LEFT, padx=6, pady=6
        )
        ttk.Button(f2, text="Browse…", command=self._browse_sales).pack(
            side=tk.LEFT, padx=6, pady=6
        )

        ctrl = ttk.Frame(self)
        ctrl.pack(fill=tk.X, **pad)
        self._run_btn = ttk.Button(ctrl, text="Compare", command=self._on_run)
        self._run_btn.pack(side=tk.LEFT, padx=6)
        self._status_var = tk.StringVar(value="Ready — pick both workbooks and click Compare")
        ttk.Label(ctrl, textvariable=self._status_var, foreground="gray").pack(
            side=tk.LEFT, padx=12
        )

        log_frame = ttk.LabelFrame(self, text="Compare Log")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=14, state="disabled", wrap="word"
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        if msg.strip():
            logging.info("[BOM COMPARE] %s", msg.rstrip())

        def _do():
            self._log.configure(state="normal")
            self._log.insert(tk.END, msg + "\n")
            self._log.see(tk.END)
            self._log.configure(state="disabled")
        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _set_status(self, msg: str) -> None:
        try:
            self.after(0, lambda: self._status_var.set(msg))
        except Exception:
            self._status_var.set(msg)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _browse_factory(self) -> None:
        p = filedialog.askopenfilename(
            title="Select Live Inventory workbook (Factory BoM)",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if p:
            self._factory_path = p
            self._factory_label.set(p)

    def _browse_sales(self) -> None:
        p = filedialog.askopenfilename(
            title="Select Sales BoM workbook",
            filetypes=[("Excel workbook", "*.xlsx"), ("All files", "*.*")],
        )
        if p:
            self._sales_path = p
            self._sales_label.set(p)

    def _on_run(self) -> None:
        if self._running:
            return
        if not self._factory_path or not os.path.isfile(self._factory_path):
            messagebox.showerror("BoM Comparison", "Pick a valid Live Inventory (Factory BoM) workbook.")
            return
        if not self._sales_path or not os.path.isfile(self._sales_path):
            messagebox.showerror("BoM Comparison", "Pick a valid Sales BoM workbook.")
            return
        self._running = True
        self._run_btn.configure(state="disabled")
        self._set_status("Comparing…")
        threading.Thread(
            target=self._run_worker,
            args=(self._factory_path, self._sales_path),
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _run_worker(self, factory_path: str, sales_path: str) -> None:
        try:
            out_path = self._compare(factory_path, sales_path)
            self._append_log(f"Wrote: {out_path}")
            self._set_status(f"Done — {os.path.basename(out_path)}")
        except Exception as exc:
            logging.exception("[BoM Compare] failed")
            self._append_log(f"ERROR: {exc}")
            self._set_status("Failed")
            try:
                msg = str(exc)
                self.after(0, lambda m=msg: messagebox.showerror("BoM Comparison", m))
            except Exception:
                pass
        finally:
            self._running = False
            try:
                self.after(0, lambda: self._run_btn.configure(state="normal"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Core compare
    # ------------------------------------------------------------------

    def _compare(self, factory_path: str, sales_path: str) -> str:
        self._append_log(f"Loading Factory: {factory_path}")
        # data_only=True returns the LAST CACHED VALUE Excel wrote for
        # every formula cell, instead of the formula string itself. We
        # use these snapshots for BOTH the per-part diff math AND the
        # full-fidelity copy of each BoM into the output workbook.
        #
        # Why "values-only" copies: source Sales BoMs from the field
        # carry in-sheet formulas like ``=VLOOKUP(E10, sites, 1, FALSE)``
        # that depend on workbook-scoped named ranges (``sites``). When
        # those formulas are copied verbatim into the comparison output
        # the named range doesn't follow, so every formula evaluates to
        # ``#NAME?`` and some of them cross-reference each other,
        # triggering Excel's "circular reference" prompt on open. The
        # ``data_only=True`` snapshot is effectively Paste Special →
        # Values: pure data, no live calculations, no broken references.
        fac_data = openpyxl.load_workbook(factory_path, data_only=True)
        self._append_log(f"Loading Sales:   {sales_path}")
        sal_data = openpyxl.load_workbook(sales_path, data_only=True)

        fac_bom = self._find_bom_sheet(fac_data, required=True)
        sal_bom = self._find_bom_sheet(sal_data, required=False)
        self._append_log(f"Factory sheet: '{fac_bom.title}'  |  Sales sheet: '{sal_bom.title}'")

        fac_sites, fac_parts, fac_inline_spares, fac_part_to_group = self._parse_per_site_bom(fac_bom)
        sal_sites, sal_parts, sal_inline_spares, sal_part_to_group = self._parse_per_site_bom(sal_bom)
        # The Sales BoM's "Spares" column is part of what the customer
        # ORDERED (extras shipped along with the per-site allocation),
        # not a separate inventory pool. The BoM's own "Total" column
        # equals (per-site sum + spares). To match operator expectation
        # ("Total Ordered should match the Sales BoM's Total column")
        # we add this column into the per-part Total Ordered below.
        # Contrast with the Factory BoM's Spares column, which IS a
        # separate pool — units sitting unallocated and available to
        # backfill Shortfalls.
        self._append_log(
            f"Factory: {len(fac_parts)} parts across {len(fac_sites)} site col(s)  |  "
            f"Sales: {len(sal_parts)} parts across {len(sal_sites)} site col(s)"
        )

        # Fold alias SKUs (e.g. CHASSIS BUNDLE -> bare CHASSIS) into their
        # canonical part numbers on BOTH sides before any keyed lookup so
        # bundle/component pairs correlate cleanly during the diff.
        aliases = load_part_aliases()
        # Discontinued / non-comparable SKUs — dropped from every output
        # tab regardless of which side lists them (vendor-EOL hardware
        # like CFP2 MDAs). Canonicalized (prefix-stripped, uppercased).
        excluded_parts = load_excluded_parts()
        if excluded_parts:
            self._append_log(
                f"Excluded parts: {len(excluded_parts)} discontinued SKU(s) "
                f"will be dropped from the comparison"
            )
        if aliases:
            fac_before, sal_before = len(fac_parts), len(sal_parts)
            fac_parts = fold_aliased_parts(fac_parts, aliases)
            sal_parts = fold_aliased_parts(sal_parts, aliases)
            # Re-key the per-part group maps too so the breakdown picks up
            # canonical SKUs (otherwise group="Optics" fallback kicks in).
            fac_part_to_group = {
                canonical_part(pn, aliases): grp
                for pn, grp in fac_part_to_group.items()
            }
            sal_part_to_group = {
                canonical_part(pn, aliases): grp
                for pn, grp in sal_part_to_group.items()
            }
            collapsed = (fac_before - len(fac_parts)) + (sal_before - len(sal_parts))
            self._append_log(
                f"Aliases: {len(aliases)} registered, "
                f"{collapsed} row(s) folded into canonical SKUs"
            )

        # Canonicalize the Sales-side spares column through the alias
        # map so the same bundle/component pairing rule applies when we
        # roll those quantities into total_ordered below.
        if sal_inline_spares and aliases:
            canon_sal_spares: Dict[str, int] = {}
            for pn, qty in sal_inline_spares.items():
                ck = canonical_part(pn, aliases)
                canon_sal_spares[ck] = canon_sal_spares.get(ck, 0) + int(qty)
            sal_inline_spares = canon_sal_spares

        # Prefer a Spare/Spare Materials column on the Factory BoM sheet
        # itself; fall back to a separate Spares tab when the column isn't
        # present.
        spares = fac_inline_spares or self._parse_spares(fac_data)
        if spares and aliases:
            # Spares pool must also be canonicalized so an alias-keyed
            # shortfall finds its backfill regardless of which form the
            # Spares column used.
            canon_spares: Dict[str, int] = {}
            for pn, qty in spares.items():
                ck = canonical_part(pn, aliases)
                canon_spares[ck] = canon_spares.get(ck, 0) + int(qty)
            spares = canon_spares
        if spares:
            src = "BOM column" if fac_inline_spares else "separate tab"
            self._append_log(f"Spares pool ({src}): {len(spares)} unique parts available for backfill")
        else:
            self._append_log("Spares pool: none found (no Spare column on BOM and no Spares tab)")

        # Authoritative recalculation: sum each PN's per-site quantities
        # by walking the detail tabs (ALBA, ALVY, ..., Spares on Sales;
        # STJO001_7250, ..., SPARES on Factory) instead of trusting the
        # BOM aggregate's cached Total formulas or per-site cells. This
        # makes the comparator's numbers verifiable by hand — open any
        # per-site tab, count the rows for a PN, the COMPARE will agree.
        fac_dt_persite, fac_dt_spares, fac_dt_desc, fac_dt_tabs = (
            self._aggregate_from_site_tabs(fac_data, fac_bom.title)
        )
        sal_dt_persite, sal_dt_spares, sal_dt_desc, sal_dt_tabs = (
            self._aggregate_from_site_tabs(sal_data, sal_bom.title)
        )
        self._append_log(
            f"Detail-tab recalc: Factory {len(fac_dt_tabs)} tab(s) "
            f"({len(fac_dt_persite)} per-site PNs, {len(fac_dt_spares)} spares PNs) | "
            f"Sales {len(sal_dt_tabs)} tab(s) "
            f"({len(sal_dt_persite)} per-site PNs, {len(sal_dt_spares)} spares PNs)"
        )

        # Detail-tab descriptions cover PNs that only appear in Spares
        # (3HE11286AA et al.) which the BOM-aggregate parser misses.
        # Merge them into the aggregate parts maps so the Shortfall
        # sheet's desc lookup finds something.
        for pn, desc in fac_dt_desc.items():
            existing = fac_parts.get(pn)
            if existing is None:
                fac_parts[pn] = {"desc": desc, "site_qty": {}}
            elif not existing.get("desc"):
                existing["desc"] = desc
        for pn, desc in sal_dt_desc.items():
            existing = sal_parts.get(pn)
            if existing is None:
                sal_parts[pn] = {"desc": desc, "site_qty": {}}
            elif not existing.get("desc"):
                existing["desc"] = desc

        # Kit grouping: when a workbook lists all components of a known
        # kit (PSS8 SHELF KIT panel + fan + shelf, etc.), fold those
        # per-site totals into the kit SKU using min(component counts).
        # Operates on workbook-aggregate per-side totals only. Spares
        # stay tied to their original SKU (not folded into the kit).
        kits = load_part_kits()
        # Prefer detail-tab totals; fall back to BOM-aggregate per-site
        # sums when a workbook ships without detail tabs (legacy or
        # synthetic test fixtures).
        if fac_dt_persite or fac_dt_spares:
            fac_persite_total: Dict[str, int] = self._fold_qty_dict_through_aliases(
                fac_dt_persite, aliases
            )
        else:
            fac_persite_total = {
                pn: sum(int(q) for q in info.get("site_qty", {}).values())
                for pn, info in fac_parts.items()
            }
        if sal_dt_persite or sal_dt_spares:
            sal_persite_total: Dict[str, int] = self._fold_qty_dict_through_aliases(
                sal_dt_persite, aliases
            )
        else:
            sal_persite_total = {
                pn: sum(int(q) for q in info.get("site_qty", {}).values())
                for pn, info in sal_parts.items()
            }
        # Spares from the detail tabs (when present) override the
        # BOM-aggregate-derived spares dicts populated earlier.
        if sal_dt_spares:
            sal_inline_spares = self._fold_qty_dict_through_aliases(
                sal_dt_spares, aliases
            )
        if fac_dt_spares:
            spares = self._fold_qty_dict_through_aliases(
                fac_dt_spares, aliases
            )
        # Capture the fold operations BEFORE applying them so the Trace
        # sheet can emit synthetic audit rows that reconcile the kit
        # SKU's Live/Sales sums to the Shortfall (which is computed
        # POST-fold).
        fac_kit_folds = compute_kit_fold_ops(fac_persite_total, kits) if kits else []
        sal_kit_folds = compute_kit_fold_ops(sal_persite_total, kits) if kits else []
        if kits:
            kits_folded_fac = sum(1 for op in fac_kit_folds)
            kits_folded_sal = sum(1 for op in sal_kit_folds)
            fold_kits(fac_persite_total, kits)
            fold_kits(sal_persite_total, kits)
            if kits_folded_fac or kits_folded_sal:
                self._append_log(
                    f"Kit grouping: folded {kits_folded_fac} kit type(s) on Live, "
                    f"{kits_folded_sal} on Sales"
                )

        # Build a Factory site-key index so Sales site names can be matched
        # case/whitespace-insensitively.
        fac_site_by_key = { _norm_site(s): s for s in fac_sites }

        # Build a single comparison view: one row per part, three
        # numeric columns (Total Ordered, Live Inventory, Difference).
        # Operators asked for a unified at-a-glance comparison with
        # color cues — red when Live falls short, green when Live
        # exceeds. No separate surplus / per-site breakdown outputs.
        compare_rows: List[Tuple[str, str, int, int, int]] = []
        all_pns = sorted(
            set(sal_parts.keys())
            | set(fac_parts.keys())
            | set(fac_persite_total.keys())
            | set(sal_persite_total.keys())
            | set(sal_inline_spares.keys())
            | set(spares.keys())
        )
        # Kit descriptions for any kit SKU that wasn't already in the
        # parsed data. Pull from the kit definition.
        kit_desc_lookup = {k["kit"]: k.get("kit_description", "") for k in kits}

        for pn in all_pns:
            # Drop discontinued / non-comparable SKUs entirely. ``pn`` is
            # already canonical (alias-folded, prefix-stripped), matching
            # the form ``load_excluded_parts`` returns.
            if pn in excluded_parts:
                continue
            sal_info = sal_parts.get(pn)
            fac_info = fac_parts.get(pn)
            desc = (
                (sal_info or {}).get("desc")
                or (fac_info or {}).get("desc")
                or kit_desc_lookup.get(pn, "")
            )
            # Total Ordered = Sales BoM's TOTAL column = per-site sum
            # + Sales spares (NOT the "net" column). Mirrors the source
            # BoM's BP-style Total.
            total_ordered = (
                int(sal_persite_total.get(pn, 0))
                + int(sal_inline_spares.get(pn, 0))
            )
            # Live Inventory total = Factory per-site + Factory spares.
            # Operator's mental model: spares ARE in the factory.
            live_total = (
                int(fac_persite_total.get(pn, 0))
                + int(spares.get(pn, 0))
            )
            if total_ordered <= 0 and live_total <= 0:
                continue
            delta = live_total - total_ordered
            compare_rows.append((pn, desc, total_ordered, live_total, delta))

        # Sort: shortfalls (negative delta) at the top so the operator
        # sees what's missing first; ties broken by PN.
        compare_rows.sort(key=lambda r: (r[4], r[0]))

        self._append_log(
            f"Compare lines: {len(compare_rows)}  |  "
            f"Shortages: {sum(1 for r in compare_rows if r[4] < 0)}  |  "
            f"Surpluses: {sum(1 for r in compare_rows if r[4] > 0)}  |  "
            f"Exact match: {sum(1 for r in compare_rows if r[4] == 0)}"
        )

        out_wb = openpyxl.Workbook()
        del out_wb[out_wb.sheetnames[0]]

        miss_ws = out_wb.create_sheet("Shortfall")
        self._write_missing_sheet(miss_ws, compare_rows)

        # Site Allocation tab — per-PN per-site Need (Sales ordered)
        # AND Have (Live delivered) broken out so the operator can see
        # at a glance where each part is needed vs where it physically
        # sits. Each site occupies a two-cell column pair; cells are
        # colored per-site (red = site short, green = site covered),
        # NOT row-level — the same PN may be short at one site and
        # surplus at another. Reuses alias-folded canonical SKUs so a
        # Shortfall row picks up the breakdown captured under any
        # registered alias.
        try:
            fac_per_site_breakdown, fac_site_order = (
                self._per_site_breakdown_from_detail_tabs(
                    fac_data, fac_bom.title
                )
            )
            sal_per_site_breakdown, sal_site_order = (
                self._per_site_breakdown_from_detail_tabs(
                    sal_data, sal_bom.title
                )
            )
            alloc_ws = out_wb.create_sheet("Site Allocation")
            self._write_site_allocation_sheet(
                alloc_ws,
                compare_rows,
                sal_per_site_breakdown,
                sal_site_order,
                fac_per_site_breakdown,
                fac_site_order,
                aliases,
            )
            self._append_log(
                f"Site Allocation: {len(sal_site_order)} Sales site(s) "
                f"+ {len(fac_site_order)} Live tab(s) "
                f"x {len(compare_rows)} part row(s)"
            )
        except Exception:
            logging.exception(
                "[BOM-COMPARE] Site Allocation sheet build failed — "
                "Shortfall + reference tabs are unaffected"
            )

        # Trace tab — one row per source contribution so the operator
        # can verify any Shortfall qty by following it back to a
        # specific (source tab, row, qty). Sales-side rows sum to
        # Total Ordered; Live-side rows sum to Live Inventory. Excel
        # auto-filter is set so the operator can drill to a single PN
        # in one click.
        try:
            sal_trace = self._qty_trace_entries(sal_data, sal_bom.title)
            fac_trace = self._qty_trace_entries(fac_data, fac_bom.title)
            trace_ws = out_wb.create_sheet("Trace")
            self._write_trace_sheet(
                trace_ws, compare_rows, sal_trace, fac_trace, aliases,
                sal_kit_folds=sal_kit_folds,
                fac_kit_folds=fac_kit_folds,
            )
            self._append_log(
                f"Trace: {len(sal_trace)} Sales source row(s) + "
                f"{len(fac_trace)} Live source row(s) tracked"
            )
        except Exception:
            logging.exception(
                "[BOM-COMPARE] Trace sheet build failed — Shortfall + "
                "reference tabs are unaffected"
            )

        builder = self.gui.workbook_builder
        # Pass the data_only=True sheets — every formula cell is already
        # resolved to its cached value, so copy_sheet writes plain values
        # (no formulas, no named-range refs, no #NAME? after save).
        #
        # copy_images=False: skip embedded branding (LightRiver banner).
        # openpyxl's BytesIO-based image rebuild produces drawing XML
        # that Excel rejects with "Repaired Records: Drawing from
        # xl/drawings/drawing*.xml" on open. Reference-copy tabs don't
        # need the branding anyway.
        #
        # The Factory-side reference copy is named "Live Inventory" —
        # the operator-facing wording for the snapshot of what's
        # currently on hand to ship.
        fac_copy = builder.copy_sheet(fac_bom, out_wb, "Live Inventory", copy_images=False)
        sal_copy = builder.copy_sheet(sal_bom, out_wb, "Sales BoM",       copy_images=False)
        # Reset view state on the copies. Source workbooks carry
        # (a) stale freeze panes anchored to whatever scroll position
        # the previous editor left behind (e.g. S143 on a 489-row sheet
        # — Excel auto-zooms to the frozen region instead of scrolling)
        # and (b) multi-pane Selection objects keyed to that freeze.
        # Then re-apply a *deliberate* freeze at column D so the Item /
        # Part No. / Equipment Description columns (A/B/C) stay visible
        # while the operator scrolls horizontally across site columns.
        for ws in (fac_copy, sal_copy):
            ws.freeze_panes = None
            self._reset_sheet_view_to_defaults(ws)
            ws.freeze_panes = "D1"
        # Add the LightRiver branding banner from the BOM template to
        # the Live Inventory tab. Source workbooks ship their own banner
        # but we strip it (copy_images=False) because the rebuilt drawing
        # XML triggers Excel "Repaired Records" prompts; the template's
        # banner is well-formed and reuses cleanly.
        if self._add_live_inventory_banner(fac_copy):
            self._append_log("Added LightRiver Live Inventory banner to Live Inventory tab")
        # Sanitize the Sales BoM copy: scan rows 1-25 for any column
        # whose header reads like a price/cost label (Unit Price, List
        # Price, Ext Price, Cost, etc.) and HIDE that column. Hiding
        # rather than deleting preserves VLOOKUP / SUMIF references and
        # title-row merges that Sales BoMs typically carry.
        n_stripped = self._strip_sales_price_columns(sal_copy)
        if n_stripped:
            self._append_log(
                f"Hid {n_stripped} price column(s) on Sales BoM copy"
            )

        # Recompute the Total column on both copied source tabs so the
        # operator can correlate Shortfall numbers against the source.
        # Source workbooks often store Total as a SUM formula whose
        # cached value is None when the file was last saved by something
        # other than Excel — leaving every Total cell blank on the copy
        # and making the Shortfall numbers feel disconnected from what's
        # visible on the BoM. Overwriting with plain integer sums keeps
        # the column populated regardless of source state.
        for tab in (fac_copy, sal_copy):
            try:
                self._recompute_total_column_on_source_copy(tab)
            except Exception:
                logging.exception(
                    f"[BOM-COMPARE] Could not recompute Total column on "
                    f"{tab.title!r}"
                )

        # Final tab order: Shortfall | Site Allocation | Trace |
        # Live Inventory | Sales BoM. Site Allocation drills into the
        # Shortfall per-site; Trace lists every source row that fed
        # those totals so an operator can verify any number against a
        # specific (sheet, row, qty) source cell. Reference copies
        # come last. Pin defensively against future reordering.
        desired_order = [
            "Shortfall", "Site Allocation", "Trace",
            "Live Inventory", "Sales BoM",
        ]
        present = [n for n in desired_order if n in out_wb.sheetnames]
        leftover = [n for n in out_wb.sheetnames if n not in present]
        out_wb._sheets = [out_wb[n] for n in present + leftover]

        out_path = self._derive_out_path(factory_path)
        out_wb.save(out_path)
        return out_path

    # ------------------------------------------------------------------
    # Sheet discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _sheet_has_bom_header(ws: Any) -> bool:
        """Return True when *ws* has both a Part-Number and a Total/Qty header
        within the first 25 rows. Used to identify which tab in a multi-sheet
        Sales workbook actually holds the BOM grid (vs Summary, Notes, etc.)."""
        max_r = min(ws.max_row or 0, 25)
        max_c = ws.max_column or 0
        if max_r == 0 or max_c == 0:
            return False
        for r in range(1, max_r + 1):
            f_pn = f_total = False
            for c in range(1, max_c + 1):
                v = ws.cell(r, c).value
                if v is None:
                    continue
                s = _hkey(v)
                if not s:
                    continue
                if s in _PN_HEADERS:
                    f_pn = True
                elif s in _QTY_HEADERS:
                    f_total = True
                if f_pn and f_total:
                    return True
        return False

    @classmethod
    def _find_bom_sheet(cls, wb: Any, required: bool = True) -> Any:
        # Exact name match wins regardless of position — accepts the
        # current ``Inventory by Site`` canonical name as well as the
        # legacy ``BOM`` form so workbooks built before the rename
        # still compare cleanly.
        for name in wb.sheetnames:
            low = name.strip().lower()
            if low in ("inventory by site", "bom"):
                return wb[name]
        if required:
            raise RuntimeError(
                "Workbook has no 'Inventory by Site' (or legacy 'BOM') "
                "sheet — cannot compare."
            )
        # Sales workbooks frequently lead with Summary/Notes tabs and put the
        # actual BOM grid on a sheet the engineer named themselves (e.g.
        # "CBR v1"). Probe each sheet for a Part-Number + Total/Qty header
        # pair so we land on the real BOM instead of the first non-empty
        # sheet.
        for name in wb.sheetnames:
            ws = wb[name]
            if cls._sheet_has_bom_header(ws):
                return ws
        # Last-ditch fallback — first non-empty sheet, then sheet 0.
        for name in wb.sheetnames:
            ws = wb[name]
            if (ws.max_row or 0) > 1:
                return ws
        return wb[wb.sheetnames[0]]

    @staticmethod
    def _find_spares_sheet(wb: Any) -> Optional[Any]:
        """Return a Spares/Spare Materials sheet if one exists in *wb*."""
        for name in wb.sheetnames:
            low = name.strip().lower()
            if low == "spares" or low == "spare materials" or "spare" in low:
                return wb[name]
        return None

    @staticmethod
    def _section_to_group(section_norm: str) -> Optional[str]:
        """Map a normalized section banner to a Factory BoM group name.

        Returns one of "Chassis/Shelf", "Cards", "Optics", or None when
        the banner doesn't map to a hardware group. Amplifiers and
        transponders fold into the "Optics" group since the Factory BoM
        template only supports those three buckets.
        """
        if not section_norm:
            return None
        if "chassis" in section_norm or "shelf" in section_norm:
            return "Chassis/Shelf"
        if "card" in section_norm:
            return "Cards"
        if (
            "optic" in section_norm
            or "amp" in section_norm
            or "transponder" in section_norm
            or "module" in section_norm
            or "passive" in section_norm
        ):
            return "Optics"
        return None

    # ------------------------------------------------------------------
    # BOM parsing — per-site
    # ------------------------------------------------------------------

    @classmethod
    def _parse_per_site_bom(
        cls, ws: Any
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]], Dict[str, int], Dict[str, str]]:
        """Parse a BOM-shaped sheet into ``(sites, parts, spares, part_to_group)``.

        ``sites``: ordered list of per-site column names.
        ``parts``: ``{part: {desc, site_qty: {site: qty}}}``.
        ``spares``: ``{part: qty}`` extracted from a sibling Spare/Spares
        column on the same sheet (empty dict if no such column exists).
        ``part_to_group``: ``{part: "Chassis/Shelf" | "Cards" | "Optics"}``
        derived from the section banner active when the part was seen.
        Used by the Spares Allocation sheet to slot allocations under the right
        Factory-BoM-template group.

        Auto-detects the header row by scanning the first 25 rows for the
        first row that contains both a Part-Number-style header and a
        Total/Qty-style header. The site columns are every non-empty
        header cell between Description (or Part No. if Description is
        absent) and Total. Aggregates duplicates by summing per-site qty.

        Falls back to a single bucket named after the sheet when no
        per-site columns are present.
        """
        max_r = ws.max_row or 0
        max_c = ws.max_column or 0
        if max_r == 0 or max_c == 0:
            return [], {}, {}, {}

        hdr_row = None
        pn_col = desc_col = total_col = None
        scan_to = min(max_r, 25)
        # Independent matching per category — a single row can supply PN,
        # Desc, AND Total. Prefer an explicit "Total" header over the first
        # "Qty" column: BoMs with two-row headers (site names on row N,
        # repeated "Qty" subheaders on row N+1) anchor on the subheader
        # row, where every site column reads "Qty". If we picked the
        # first "Qty" as total_col, the per-site loop (which stops at
        # total_col) would never iterate. The right answer is the
        # rightmost true "Total" column; "Qty" only acts as a fallback
        # for sheets that don't carry a Total column at all.
        _TOTAL_PRIMARY = ("total", "total qty", "total ordered")
        for r in range(1, scan_to + 1):
            f_pn = f_desc = f_total = f_qty_fallback = None
            for c in range(1, max_c + 1):
                v = ws.cell(r, c).value
                if v is None:
                    continue
                s = _hkey(v)
                if not s:
                    continue
                if f_pn is None and s in _PN_HEADERS:
                    f_pn = c
                if f_desc is None and s in _DESC_HEADERS:
                    f_desc = c
                if s in _TOTAL_PRIMARY:
                    f_total = c  # take the rightmost "Total"
                elif f_qty_fallback is None and s in _QTY_HEADERS:
                    f_qty_fallback = c
            chosen_total = f_total if f_total is not None else f_qty_fallback
            if f_pn is not None and chosen_total is not None:
                hdr_row = r
                pn_col, desc_col, total_col = f_pn, f_desc, chosen_total
                break
        if hdr_row is None:
            raise RuntimeError(
                f"Sheet '{ws.title}' has no recognizable BOM header "
                "(need a Part Number column and a Total/Qty column)."
            )

        # Site columns sit between (desc_col or pn_col) and total_col.
        # Two-row header support: real-world BoMs commonly put site names
        # on row N and a repeated "Qty" subheader on row N+1 (the row that
        # carries Part No./Total and thus anchors hdr_row). When the cell
        # at hdr_row reads "Qty"/"Total", consult hdr_row-1 for the actual
        # site name. Without this fallback every per-site column collapses
        # to the single header keyword and gets filtered out.
        left_anchor = desc_col if desc_col is not None else pn_col
        site_cols: List[Tuple[int, str]] = []
        spares_col: Optional[int] = None
        super_row = hdr_row - 1 if hdr_row > 1 else None
        # Layout detection. Two real-world shapes exist:
        #
        # * NEW: ``Item | PN | Desc | Total Ordered | <sites> | Spares``
        #   total_col sits 1 column past left_anchor; sites + spares are
        #   on the RIGHT. We have to scan past total_col to find them.
        #
        # * LEGACY: ``Item | PN | Desc | <sites> | Spares | Total | ...meta``
        #   total_col is far to the right; columns past total_col are
        #   meta (Customer, License Points, computed subtotals) whose
        #   numeric values can leak through as fake site quantities if
        #   we scan them. Keep the protective ``c >= total_col`` gate.
        total_is_leading = (total_col - left_anchor) <= 1
        scan_end = max_c
        for c in range(left_anchor + 1, scan_end + 1):
            sub_v = ws.cell(hdr_row, c).value
            sup_v = ws.cell(super_row, c).value if super_row else None
            sub_s = str(sub_v).strip() if sub_v is not None else ""
            sup_s = str(sup_v).strip() if sup_v is not None else ""
            sub_low = _hkey(sub_v) if sub_s else ""
            sup_low = _hkey(sup_v) if sup_s else ""
            # Spare detection on EITHER header row.
            if "spare" in sub_low or "spare" in sup_low:
                if spares_col is None:
                    spares_col = c
                continue
            # Pick the most descriptive name: prefer the super-header when
            # the sub-header is a generic Qty/Total subheader.
            if sub_low and sub_low not in _QTY_HEADERS:
                s, low = sub_s, sub_low
            elif sup_s:
                s, low = sup_s, sup_low
            else:
                continue
            if total_is_leading:
                # New layout — skip only the Total column itself.
                if c == total_col:
                    continue
            else:
                # Legacy layout — anything at or past Total is metadata
                # (Spares are handled above; Customer / Line Total /
                # License points etc. would leak as bogus sites).
                if c >= total_col:
                    continue
            if low in _ITEM_HEADERS or low in _PN_HEADERS or low in _DESC_HEADERS or low in _QTY_HEADERS:
                continue
            # Skip price/cost/metadata columns — they're not site quantities.
            if any(bad in low for bad in _NON_SITE_HEADERS):
                continue
            # Drop columns whose header matches an excluded-section keyword
            # (Maintenance, Software, Services, etc.) — these are roll-up
            # totals, not real sites. We use the column-header-specific
            # list (which allows "license points") rather than the banner
            # version (which still excludes "license" sections wholesale).
            if any(ex in low for ex in _EXCLUDED_COLUMN_HEADERS):
                continue
            site_cols.append((c, s))

        # Drop "rollup" columns. Factory BoMs commonly lay each site out
        # as ``SITE001_7250, SITE002_7250, ..., SITE Extra Materials,
        # SITE`` where the last bare-named column repeats the sum of all
        # preceding columns for the same site. Counting it doubles
        # every per-site total — e.g. 3HE12546AA reports 1286 instead
        # of the real 643. Detection: group columns by the leading-
        # letter site key (``MALN001_7250`` -> ``MALN``). When a group
        # has BOTH a column whose name (uppercased) is exactly the
        # group key AND one or more distinguishing siblings, the bare
        # column is the rollup and gets removed. A standalone bare
        # column (e.g. Sales BoMs that list ``ALBA`` as the only ALBA
        # column) is kept — it's the only data source.
        if site_cols:
            groups: Dict[str, List[Tuple[int, str]]] = {}
            for col_pair in site_cols:
                _c, name = col_pair
                m = re.match(r"^([A-Za-z]+)", (name or "").strip())
                key = m.group(1).upper() if m else ""
                if not key:
                    continue
                groups.setdefault(key, []).append(col_pair)
            rollup_cols: set = set()
            for key, cols in groups.items():
                if len(cols) < 2:
                    continue
                bare = [(c, n) for (c, n) in cols if n.strip().upper() == key]
                others = [(c, n) for (c, n) in cols if n.strip().upper() != key]
                if bare and others:
                    for (c, _n) in bare:
                        rollup_cols.add(c)
            if rollup_cols:
                site_cols = [(c, n) for (c, n) in site_cols if c not in rollup_cols]

        # If no per-site columns, treat the whole sheet as one bucket
        # named after the sheet itself so the diff still works.
        single_bucket = not site_cols
        if single_bucket:
            site_cols = [(total_col, ws.title)]

        # Dedupe site names while preserving first-seen order. Some Sales
        # BoMs repeat a site label across multiple columns (e.g. two BENT
        # columns); quantities are summed by name below, and the Missing
        # BOM writer keys on name, so collapsing here prevents a blank
        # column from a dict-key collision.
        _seen: set = set()
        sites: List[str] = []
        for (_, name) in site_cols:
            if name not in _seen:
                _seen.add(name)
                sites.append(name)
        out: Dict[str, Dict[str, Any]] = {}
        spares_out: Dict[str, int] = {}
        part_to_group: Dict[str, str] = {}

        # Track the active section banner. A banner row has text in col A
        # (or the desc col) but no part number in the PN col; if its text
        # matches an excluded section keyword, every following data row
        # is skipped until the next banner.
        section_excluded = False
        current_group: Optional[str] = None

        for r in range(hdr_row + 1, max_r + 1):
            pn = ws.cell(r, pn_col).value
            pn_s = str(pn).strip() if pn is not None else ""

            # Banner detection: PN cell empty but col A (or desc col) has
            # heading-like text. Update section state and move on.
            if not pn_s:
                banner_text = ""
                for cand_col in (1, desc_col, 2):
                    if cand_col is None:
                        continue
                    cv = ws.cell(r, cand_col).value
                    if cv is None:
                        continue
                    cs = str(cv).strip()
                    if cs and not cs.replace(".", "").isdigit():
                        banner_text = cs
                        break
                if banner_text:
                    bn = _hkey(banner_text)
                    if any(ex in bn for ex in _EXCLUDED_SECTIONS):
                        section_excluded = True
                    elif any(inc in bn for inc in _INCLUDED_SECTIONS):
                        section_excluded = False
                        grp = cls._section_to_group(bn)
                        if grp:
                            current_group = grp
                continue

            # Some Sales BoMs put section banners directly in the PN col
            # (e.g., "1830 PSS-8 Maintenance" with no qty). Detect those:
            # if the PN text matches an excluded-section keyword AND the
            # row has zero quantity in every site column, flip section
            # state and skip the row instead of treating the banner as a
            # part number.
            pn_norm = _hkey(pn_s)
            looks_like_excluded_banner = any(ex in pn_norm for ex in _EXCLUDED_SECTIONS)
            looks_like_included_banner = any(inc in pn_norm for inc in _INCLUDED_SECTIONS)
            if looks_like_excluded_banner or looks_like_included_banner:
                row_has_qty = False
                for col, _ in site_cols:
                    if _qty_cell_to_int(ws.cell(r, col).value) > 0:
                        row_has_qty = True
                        break
                if not row_has_qty:
                    section_excluded = looks_like_excluded_banner
                    if looks_like_included_banner:
                        grp = cls._section_to_group(pn_norm)
                        if grp:
                            current_group = grp
                    continue

            if section_excluded:
                continue

            low = _hkey(pn_s)
            if low in _PN_HEADERS or low in _QTY_HEADERS or low in _ITEM_HEADERS:
                continue

            desc = ""
            if desc_col is not None:
                desc = str(ws.cell(r, desc_col).value or "").strip()

            # Description-level intangible filter — catches flat Sales
            # BoMs that don't use section banners (e.g., software/support
            # subscriptions interleaved with hardware).
            desc_norm = _hkey(desc)
            if desc_norm and any(kw in desc_norm for kw in _INTANGIBLE_DESC_KEYWORDS):
                continue

            site_qty: Dict[str, int] = {}
            row_total = 0
            for col, site_name in site_cols:
                q = _qty_cell_to_int(ws.cell(r, col).value)
                if q > 0:
                    site_qty[site_name] = site_qty.get(site_name, 0) + q
                    row_total += q

            if row_total <= 0:
                continue

            existing = out.setdefault(pn_s, {"desc": "", "site_qty": {}})
            if desc and not existing["desc"]:
                existing["desc"] = desc
            for s_name, q in site_qty.items():
                existing["site_qty"][s_name] = existing["site_qty"].get(s_name, 0) + q
            if pn_s not in part_to_group and current_group:
                part_to_group[pn_s] = current_group

            # Spares column on the same row → accumulate into the spares
            # pool keyed by part number. Spares qty doesn't gate row_total
            # so a part with only a spares value still counts.
            if spares_col is not None:
                sq = _qty_cell_to_int(ws.cell(r, spares_col).value)
                if sq > 0:
                    spares_out[pn_s] = spares_out.get(pn_s, 0) + sq

        # Second pass: pick up parts that ONLY appear with a spares qty
        # (zero across all site cols) so the spares pool isn't missing
        # those entries. Walk again only when a spares col exists.
        # We ALSO capture the description on the same row into ``out``
        # so spares-only PNs don't surface with blank descriptions on
        # downstream tabs (Sales BoM Spares tab, BoM Compare Shortfall).
        if spares_col is not None:
            for r in range(hdr_row + 1, max_r + 1):
                pn = ws.cell(r, pn_col).value
                if pn is None:
                    continue
                pn_s = str(pn).strip()
                if not pn_s or pn_s in spares_out:
                    continue
                low = _hkey(pn_s)
                if low in _PN_HEADERS or low in _QTY_HEADERS or low in _ITEM_HEADERS:
                    continue
                sq = _qty_cell_to_int(ws.cell(r, spares_col).value)
                if sq > 0:
                    spares_out[pn_s] = spares_out.get(pn_s, 0) + sq
                    if desc_col is not None:
                        desc_val = str(ws.cell(r, desc_col).value or "").strip()
                        if desc_val:
                            existing = out.setdefault(
                                pn_s, {"desc": "", "site_qty": {}}
                            )
                            if not existing["desc"]:
                                existing["desc"] = desc_val

        return sites, out, spares_out, part_to_group

    # Header keywords used to recognize a per-site DETAIL tab. These
    # tabs sit alongside the BOM aggregate tab and carry a row-per-PN
    # (Sales side) or row-per-physical-unit (Factory side) layout.
    _DETAIL_PN_HEADERS = ("part number", "part no", "part #")
    _DETAIL_DESC_HEADERS = ("description", "equipment description")
    _DETAIL_QTY_HEADERS = ("quantity", "qty", "total qty", "total")

    # License/RTU/OS entries that appear on Factory per-site detail tabs
    # but get filtered from the BOM aggregate by section-banner detection
    # (Software/Services/Maintenance). The aggregate filter doesn't reach
    # the per-site detail tabs, so the detail-tab parser applies a
    # description-keyword filter to match. Matched as a substring on the
    # uppercased description (e.g. ``"OS LICENSE"`` matches
    # ``"OS - 7250 IXR R24.x OS LICENSE LARGE"``).
    _LICENSE_DESC_KEYWORDS = ("LICENSE",)

    @staticmethod
    def _detect_detail_tab_layout(ws: Any) -> Optional[Tuple[int, Optional[int], Optional[int], int]]:
        """Locate ``(pn_col, qty_col, desc_col, hdr_row)`` on a per-site
        detail tab. ``qty_col`` is ``None`` for Factory-side tabs where
        each data row is one physical unit (implicit qty=1).

        Returns ``None`` when the tab has no ``Part Number`` header in
        the first 25 rows (Summary / banner / blank tabs).
        """
        max_r = min(ws.max_row or 0, 25)
        max_c = min(ws.max_column or 0, 12)
        for r in range(1, max_r + 1):
            pn_col = qty_col = desc_col = None
            for c in range(1, max_c + 1):
                v = ws.cell(r, c).value
                if v is None:
                    continue
                s = _hkey(v)
                if not s:
                    continue
                if pn_col is None and s in BomCompareFrame._DETAIL_PN_HEADERS:
                    pn_col = c
                elif desc_col is None and s in BomCompareFrame._DETAIL_DESC_HEADERS:
                    desc_col = c
                elif qty_col is None and s in BomCompareFrame._DETAIL_QTY_HEADERS:
                    qty_col = c
            if pn_col is not None:
                return pn_col, qty_col, desc_col, r
        return None

    @classmethod
    def _aggregate_from_site_tabs(
        cls,
        wb: Any,
        bom_sheet_title: str,
    ) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, str], List[str]]:
        """Sum each PN's quantities across every per-site detail tab —
        the authoritative source of truth — bypassing the BOM aggregate
        tab and any cached SUM formulas it may carry.

        Returns ``(per_site_total, spares_total, desc_map, visited_tabs)``:

        * ``per_site_total``: ``{pn_upper: int}`` summed across every
          non-Spares detail tab.
        * ``spares_total``: ``{pn_upper: int}`` from the Spares tab.
        * ``desc_map``: ``{pn_upper: description}`` — captures
          descriptions from detail tabs so PNs that appear only in
          Spares (e.g. fan-pack accessories) still get a label on the
          Shortfall sheet.
        * ``visited_tabs``: titles of every detail tab actually parsed
          (for logging).

        Tab classification is auto-detected from the row-14 header:

        * Sales detail tab:   ``Part Number`` in col B, ``Quantity`` in
          col D → one row per aggregated PN.
        * Factory detail tab: ``PART NUMBER`` in col D, no qty col →
          one row per physical unit (qty=1).

        PN cells with embedded vendor-name lines
        (``"NMA-8509\\nQUEST TECHNOLOGIES"``) are split on newline and
        only the first non-blank token kept.
        """
        per_site_total: Dict[str, int] = {}
        spares_total: Dict[str, int] = {}
        desc_map: Dict[str, str] = {}
        visited: List[str] = []
        skip_titles = {(bom_sheet_title or "").strip().lower()} | set(_DETAIL_TAB_SKIP_TITLES)
        for ws in wb.worksheets:
            title = (ws.title or "").strip()
            if title.lower() in skip_titles or not title:
                continue
            layout = cls._detect_detail_tab_layout(ws)
            if layout is None:
                continue
            pn_col, qty_col, desc_col, hdr_row = layout
            is_spares = "spare" in title.lower()
            for r in range(hdr_row + 1, (ws.max_row or 0) + 1):
                pn_raw = ws.cell(r, pn_col).value
                if pn_raw is None:
                    continue
                pn = str(pn_raw).split("\n", 1)[0].strip()
                if not pn:
                    continue
                pn_low = _hkey(pn)
                if pn_low in _PN_HEADERS or pn_low in _QTY_HEADERS or pn_low in _ITEM_HEADERS:
                    continue
                if qty_col is None:
                    qty = 1
                else:
                    qty = _qty_cell_to_int(ws.cell(r, qty_col).value)
                if qty <= 0:
                    continue
                # Read description first so we can apply the license
                # keyword filter before booking the qty.
                desc_raw = None
                if desc_col is not None:
                    dv = ws.cell(r, desc_col).value
                    if dv:
                        desc_raw = str(dv).split("\n", 1)[0].strip()
                if desc_raw:
                    desc_upper = desc_raw.upper()
                    if any(kw in desc_upper for kw in cls._LICENSE_DESC_KEYWORDS):
                        # License/RTU/OS LICENSE entries — virtual line
                        # items the aggregate parser also drops via its
                        # section banner filter.
                        continue
                pn_key = pn.upper()
                bucket = spares_total if is_spares else per_site_total
                bucket[pn_key] = bucket.get(pn_key, 0) + qty
                if desc_raw and pn_key not in desc_map:
                    desc_map[pn_key] = desc_raw
            visited.append(title)
        return per_site_total, spares_total, desc_map, visited

    @staticmethod
    def _fold_qty_dict_through_aliases(
        d: Dict[str, int],
        aliases: Dict[str, str],
    ) -> Dict[str, int]:
        """Apply alias resolution to a flat ``{pn: qty}`` map, summing
        duplicates that collapse onto the same canonical SKU."""
        if not aliases:
            return dict(d)
        out: Dict[str, int] = {}
        for pn, qty in d.items():
            ck = canonical_part(pn, aliases)
            out[ck] = out.get(ck, 0) + int(qty)
        return out

    @classmethod
    def _qty_trace_entries(
        cls,
        wb: Any,
        bom_sheet_title: str,
    ) -> List[Tuple[str, str, int, int, str]]:
        """Emit a row-per-source-row trace of where every counted qty
        came from. Used to build the Trace sheet so an operator can
        verify any Shortfall number by following it back to a specific
        ``(sheet, row, qty, detail)`` source coordinate.

        Each entry is ``(raw_pn, source_tab, source_row, qty,
        detail)``:

        * ``raw_pn`` — PN as it appeared in the source (not alias-
          folded), so vendor-extended SKUs that get collapsed under a
          canonical key are still individually traceable.
        * ``source_tab`` — the per-site detail tab title.
        * ``source_row`` — 1-based row number in that tab.
        * ``qty`` — units this row contributes. For Sales-style tabs
          (one row per aggregated PN), this is the Quantity-column
          value. For Factory-style tabs (one row per physical unit),
          this is always 1.
        * ``detail`` — Sales-side row description; Factory-side
          serial number when present, else description.

        Same parser semantics as ``_per_site_breakdown_from_detail_tabs``
        (license rows dropped, PN newlines stripped, quoted qtys
        tolerated) so the totals reconcile cell-for-cell with the
        Shortfall.
        """
        entries: List[Tuple[str, str, int, int, str]] = []
        skip_titles = {(bom_sheet_title or "").strip().lower()} | set(_DETAIL_TAB_SKIP_TITLES)
        for ws in wb.worksheets:
            title = (ws.title or "").strip()
            if title.lower() in skip_titles or not title:
                continue
            layout = cls._detect_detail_tab_layout(ws)
            if layout is None:
                continue
            pn_col, qty_col, desc_col, hdr_row = layout
            for r in range(hdr_row + 1, (ws.max_row or 0) + 1):
                pn_raw = ws.cell(r, pn_col).value
                if pn_raw is None:
                    continue
                pn = str(pn_raw).split("\n", 1)[0].strip()
                if not pn:
                    continue
                pn_low = _hkey(pn)
                if pn_low in _PN_HEADERS or pn_low in _QTY_HEADERS or pn_low in _ITEM_HEADERS:
                    continue
                qty = 1 if qty_col is None else _qty_cell_to_int(
                    ws.cell(r, qty_col).value
                )
                if qty <= 0:
                    continue
                desc_raw = ""
                if desc_col is not None:
                    dv = ws.cell(r, desc_col).value
                    if dv:
                        desc_raw = str(dv).split("\n", 1)[0].strip()
                if desc_raw and any(
                    kw in desc_raw.upper() for kw in cls._LICENSE_DESC_KEYWORDS
                ):
                    continue
                # Detail field: Factory tabs put a SERIAL NUMBER in the
                # cell right after PN — surface that so the operator
                # can verify "this exact unit is here". Sales tabs
                # aggregate qty per row, so we surface the description.
                if qty_col is None:
                    serial = ws.cell(r, pn_col + 1).value
                    serial_s = str(serial).strip() if serial else ""
                    if serial_s and serial_s.upper() != "N/A":
                        detail = serial_s
                    else:
                        detail = desc_raw
                else:
                    detail = desc_raw
                entries.append((pn, title, r, qty, detail))
        return entries

    @classmethod
    def _per_site_breakdown_from_detail_tabs(
        cls,
        wb: Any,
        bom_sheet_title: str,
    ) -> Tuple[Dict[str, Dict[str, int]], List[str]]:
        """Walk per-site DETAIL tabs and return per-PN per-site qty plus
        the ordered list of site tab titles (in workbook order, skipping
        BOM aggregate / Summary).

        Same parsing semantics as ``_aggregate_from_site_tabs`` (license
        rows dropped, PN newlines stripped, quoted-number qty cells
        tolerated) but preserves the SITE dimension — used by the
        ``Site Allocation`` sheet writer to surface where each part
        physically sits, one cell per (PN, site) pair.

        Returns ``(breakdown, site_order)`` where:

        * ``breakdown[pn_upper][site_title] = qty``
        * ``site_order`` is the list of site tabs that actually
          contributed at least one unit (empty tabs are skipped so the
          output sheet doesn't carry dead columns).
        """
        breakdown: Dict[str, Dict[str, int]] = {}
        all_sites: List[str] = []
        skip_titles = {(bom_sheet_title or "").strip().lower()} | set(_DETAIL_TAB_SKIP_TITLES)
        for ws in wb.worksheets:
            title = (ws.title or "").strip()
            if title.lower() in skip_titles or not title:
                continue
            layout = cls._detect_detail_tab_layout(ws)
            if layout is None:
                continue
            pn_col, qty_col, desc_col, hdr_row = layout
            tab_touched = False
            for r in range(hdr_row + 1, (ws.max_row or 0) + 1):
                pn_raw = ws.cell(r, pn_col).value
                if pn_raw is None:
                    continue
                pn = str(pn_raw).split("\n", 1)[0].strip()
                if not pn:
                    continue
                pn_low = _hkey(pn)
                if pn_low in _PN_HEADERS or pn_low in _QTY_HEADERS or pn_low in _ITEM_HEADERS:
                    continue
                qty = 1 if qty_col is None else _qty_cell_to_int(
                    ws.cell(r, qty_col).value
                )
                if qty <= 0:
                    continue
                desc_raw = None
                if desc_col is not None:
                    dv = ws.cell(r, desc_col).value
                    if dv:
                        desc_raw = str(dv).split("\n", 1)[0].strip()
                if desc_raw and any(
                    kw in desc_raw.upper() for kw in cls._LICENSE_DESC_KEYWORDS
                ):
                    continue
                pn_key = pn.upper()
                sites_for_pn = breakdown.setdefault(pn_key, {})
                sites_for_pn[title] = sites_for_pn.get(title, 0) + qty
                tab_touched = True
            if tab_touched and title not in all_sites:
                all_sites.append(title)
        return breakdown, all_sites

    @classmethod
    def _parse_spares(cls, wb: Any) -> Dict[str, int]:
        """Return ``{part: total_qty}`` from a Spares/Spare Materials tab."""
        ws = cls._find_spares_sheet(wb)
        if ws is None:
            return {}
        try:
            _, parts, inline_spares, _ = cls._parse_per_site_bom(ws)
        except Exception as exc:
            logging.debug(f"[BoM Compare] spares parse failed for '{ws.title}': {exc}")
            return {}
        # Prefer an inline Spare column on the spares sheet itself; fall
        # back to summing every per-site qty when the tab is just a flat
        # list of available parts.
        if inline_spares:
            return inline_spares
        return {pn: sum(info["site_qty"].values()) for pn, info in parts.items()}

    # ------------------------------------------------------------------
    # Missing-sheet writer (BOM-style layout)
    # ------------------------------------------------------------------

    # Headers that mark a column as price/cost data on a Sales BoM.
    # Stripped from the reference-copy tab in the comparison output so
    # the operator's view doesn't surface pricing alongside the diff.
    _PRICE_HEADER_KEYWORDS = (
        "unit price", "list price", "ext price", "extended price",
        "cost", "price", "amount",
    )
    # Rows where Sales BoMs are likely to put their column headers.
    # Different templates land them at row 11, 12, or thereabouts; we
    # scan a small range rather than hard-code a single row.
    _PRICE_HEADER_SCAN_ROWS = range(1, 26)

    @classmethod
    def _recompute_total_column_on_source_copy(cls, ws: Any) -> int:
        """Fill in the Total column on a copied source BoM tab with
        rollup-deduplicated per-site sums for each data row.

        Source workbooks often carry a ``Total`` column whose value is
        a SUM(...) formula. When the source was saved by something
        other than Excel (or never opened in Excel after edits), the
        cached value for that formula is ``None`` — and openpyxl's
        ``data_only=True`` load returns ``None`` too. That leaves
        every Total cell blank on the copied tab, which breaks the
        operator's ability to correlate Shortfall numbers against the
        source BoM.

        Why we delegate to ``_parse_per_site_bom`` rather than naively
        summing cells: Factory BoMs commonly lay each site out as
        ``SITE001_7250, SITE002_7250, ..., SITE Extra Materials, SITE``
        where the trailing bare ``SITE`` column is a sum-of-others
        rollup. Naively summing every numeric cell between Description
        and Total double-counts every unit (the rollup column carries
        the same qty as the per-site columns combined) and inflates
        Live Inventory totals by roughly 2x. The parser already
        deduplicates rollups via the leading-letter site-key trick,
        so we reuse its output here.

        Returns the number of rows updated. Idempotent: re-running
        on an already-populated Total column produces the same values.
        """
        if ws is None:
            return 0
        max_c = ws.max_column or 0
        max_r = ws.max_row or 0
        if max_c < 5 or max_r < 10:
            return 0
        # Find the rightmost Total column on the header row (7).
        total_col = None
        for c in range(max_c, 0, -1):
            v = ws.cell(7, c).value
            if v and str(v).strip().lower() == "total":
                total_col = c
                break
        if total_col is None or total_col < 4:
            return 0

        # Delegate site discovery + rollup deduplication to the parser
        # so the Total column matches what the Shortfall sheet sees.
        try:
            _sites, parts, inline_spares, _ = cls._parse_per_site_bom(ws)
        except Exception:
            logging.exception(
                f"[BOM-COMPARE] Parser failed on {ws.title!r} during Total "
                f"recompute; falling back to naive cell sum (may double-count "
                f"rollup columns)."
            )
            parts, inline_spares = {}, {}

        # Build a {pn_strip: rollup_dedup_total} map from the parsed dicts.
        # Match the human-facing Total semantic for each side:
        # * Factory Live Inventory: Total = per-site sum only. The Spares
        #   column on the Factory BoM is a SEPARATE backfill pool, not
        #   units allocated to a site — including it would over-report
        #   what's accounted for on this sheet's per-site cells.
        # * Sales BoM: same here. ``_add_visible_total_column_to_bom``
        #   in the builder writes its own Total Ordered column at col D
        #   (per-site + spares); this recompute targets the legacy
        #   rightmost Total column, which by convention is per-site
        #   only on source BoMs.
        per_pn_total: Dict[str, int] = {
            pn: sum(int(q) for q in info.get("site_qty", {}).values())
            for pn, info in parts.items()
        }

        DATA_FIRST_ROW = 10
        updated = 0
        for r in range(DATA_FIRST_ROW, max_r + 1):
            pn = ws.cell(r, 2).value
            if not pn:
                continue
            pn_key = str(pn).strip()
            if not pn_key:
                continue
            total_value = per_pn_total.get(pn_key, 0)
            ws.cell(r, total_col).value = total_value
            updated += 1
        if updated:
            logging.info(
                f"[BOM-COMPARE] Recomputed Total column on {ws.title!r}: "
                f"{updated} row(s) updated (rollup-deduplicated, "
                f"formula-free)"
            )
        return updated

    def _add_live_inventory_banner(self, ws: Any) -> bool:
        """Attach the 'LightRiver Live Inventory' banner from
        ``data/BOM_Template.xlsx`` to the top of *ws*.

        Source Factory BoMs come with their own branding images, but
        copy_sheet's image-rebuild path produced drawing XML that Excel
        flagged on open (see _compare for the full story); we strip
        those via ``copy_images=False``. This helper re-attaches the
        well-formed template banner so the Live Inventory tab still
        carries LightRiver branding. Returns True on success.
        """
        try:
            from io import BytesIO
            from copy import copy as _copy
            from openpyxl.drawing.image import Image as XLImage
        except Exception:
            return False
        try:
            builder = self.gui.workbook_builder
        except Exception:
            return False
        tpl_path = getattr(builder, "bom_template", None) if builder else None
        if not tpl_path or not os.path.isfile(tpl_path):
            return False
        try:
            tpl_wb = openpyxl.load_workbook(tpl_path)
            try:
                tpl_ws = tpl_wb.active
                imgs = getattr(tpl_ws, "_images", []) or []
                if not imgs:
                    return False
                src_img = imgs[0]
                data_fn = getattr(src_img, "_data", None)
                if not callable(data_fn):
                    return False
                img_bytes = data_fn()
                if not img_bytes:
                    return False
                new_img = XLImage(BytesIO(img_bytes))
                if src_img.anchor is not None:
                    new_img.anchor = _copy(src_img.anchor)
                ws.add_image(new_img)
                return True
            finally:
                tpl_wb.close()
        except Exception as exc:
            logging.warning(
                f"[BOM-COMPARE] could not add banner to '{ws.title}': {exc}"
            )
            return False

    @staticmethod
    def _reset_sheet_view_to_defaults(ws: Any) -> None:
        """Replace the sheet's view state with a single fresh selection.

        Source-workbook ``sheet_view`` objects can carry multi-pane
        ``Selection`` lists (``pane='topRight'`` / ``'bottomLeft'`` /
        ``'bottomRight'``) tied to a freeze/split layout the new sheet
        no longer has. Excel rejects the resulting view XML with
        "Repaired Records: View from xl/worksheets/sheet*.xml". This
        helper installs a single A1 Selection with no pane reference so
        the saved view XML is unambiguous.
        """
        try:
            from openpyxl.worksheet.views import Selection
        except Exception:
            return
        try:
            sv = ws.sheet_view
            sv.selection = [Selection(activeCell="A1", sqref="A1")]
            # Force a sensible zoom even if the source omitted one.
            if sv.zoomScale in (None, 0):
                sv.zoomScale = 100
            if sv.view in (None, ""):
                sv.view = "normal"
        except Exception as exc:
            logging.debug(f"[BOM-COMPARE] sheet view reset skipped: {exc}")

    @classmethod
    def _strip_sales_price_columns(cls, ws: Any) -> int:
        """Hide every column on the Sales BoM whose header looks like a
        price column.

        Walks rows 1-25 looking for any cell whose value (case- and
        whitespace-insensitive) exactly matches one of the price-keyword
        headers in ``_PRICE_HEADER_KEYWORDS``. Each matching column is
        marked ``hidden=True`` so the operator's view of the reference
        copy doesn't surface pricing.

        We *hide* rather than *delete* because Sales BoMs typically carry
        in-sheet ``VLOOKUP``/``SUMIF`` formulas (e.g. row-6 site lookups
        like ``=VLOOKUP(E10, sites, 1, FALSE)``) and wide title merges
        (``A1:BU5``). ``delete_cols`` shifts every cell to the right
        leftward by one but does NOT rewrite formula cell references, so
        those lookups end up pointing at the wrong cells once columns
        move; merges anchored on the deleted columns also collapse.
        Hiding is non-destructive: references stay valid, merges stay
        intact, and the operator can unhide in Excel if they ever need
        to reference the pricing data.

        Returns the number of columns hidden.
        """
        if ws is None:
            return 0
        max_col = ws.max_column or 0
        if max_col == 0:
            return 0

        matched_cols: set = set()
        for row_idx in cls._PRICE_HEADER_SCAN_ROWS:
            for col_idx in range(1, max_col + 1):
                try:
                    val = ws.cell(row=row_idx, column=col_idx).value
                except Exception:
                    continue
                if val is None:
                    continue
                key = str(val).strip().lower()
                if key in cls._PRICE_HEADER_KEYWORDS:
                    matched_cols.add(col_idx)

        hidden = 0
        for col_idx in sorted(matched_cols):
            letter = get_column_letter(col_idx)
            try:
                ws.column_dimensions[letter].hidden = True
                hidden += 1
            except Exception as exc:
                logging.warning(
                    f"[BOM-COMPARE] Could not hide price column "
                    f"{letter}: {exc}"
                )
        return hidden

    # Backwards-compatible alias — older callers / tests may still use the
    # original single-column name.
    @classmethod
    def _strip_sales_unit_price_column(cls, ws: Any) -> bool:
        return cls._strip_sales_price_columns(ws) > 0

    @staticmethod
    def _write_missing_sheet(
        ws: Any,
        rows: List[Tuple[str, str, int, int, int]],
    ) -> None:
        """Write the workbook-level Shortfall comparison sheet.

        ``rows`` is ``[(pn, desc, total_ordered, live_total,
        delta), ...]`` covering every part with a non-zero presence on
        either side. ``delta = live_total - total_ordered`` — negative
        when the Factory is short, positive when it's over.

        Column layout (6 cols total):
          A=Item | B=Part No. | C=Equipment Description |
          D=Total Ordered (Sales BoM's Total column = per-site + spares) |
          E=Live Inventory (Factory per-site + Factory spares) |
          F=Difference (E - D)  — RED if negative, GREEN if positive.

        Sort comes in pre-sorted (delta ascending = worst shortages
        at the top). Operators eyeball the red entries first.
        """
        thin = Side(style="thin", color="FF7F7F7F")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        accent_fill = PatternFill(start_color="FF0087FF", end_color="FF0087FF", fill_type="solid")
        ordered_fill = PatternFill(start_color="FF005EBA", end_color="FF005EBA", fill_type="solid")
        live_fill = PatternFill(start_color="FF386030", end_color="FF386030", fill_type="solid")
        delta_fill = PatternFill(start_color="FF7F7F7F", end_color="FF7F7F7F", fill_type="solid")
        white_bold = Font(color="FFFFFFFF", bold=True)
        bold = Font(bold=True)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        green_font = Font(bold=True, color="FF548235")
        red_font = Font(bold=True, color="FFC00000")

        ITEM_COL, PN_COL, DESC_COL = 1, 2, 3
        ORDERED_COL, LIVE_COL, DELTA_COL = 4, 5, 6
        last_col = DELTA_COL

        # Title row
        ws.cell(1, 1, "Shortfall — Sales BoM vs Live Inventory").font = Font(bold=True, size=13)
        ws.merge_cells(start_row=1, end_row=1, start_column=1, end_column=last_col)

        # Two-row header (mirrors BoM template's site/qty layout)
        hdr_row, sub_row, first_data_row = 3, 4, 5

        for col, label in (
            (ITEM_COL, "Item"),
            (PN_COL, "Part No."),
            (DESC_COL, "Equipment Description"),
        ):
            head = ws.cell(hdr_row, col, label)
            head.fill = accent_fill
            head.font = white_bold
            head.alignment = center
            head.border = border
            sub = ws.cell(sub_row, col)
            sub.fill = accent_fill
            sub.border = border
            ws.merge_cells(
                start_row=hdr_row, end_row=sub_row,
                start_column=col, end_column=col,
            )

        for col, label, fill in (
            (ORDERED_COL, "Total Ordered", ordered_fill),
            (LIVE_COL, "Live Inventory", live_fill),
            (DELTA_COL, "Difference", delta_fill),
        ):
            head = ws.cell(hdr_row, col, label)
            head.fill = fill
            head.font = white_bold
            head.alignment = center
            head.border = border
            sub = ws.cell(sub_row, col, "Qty")
            sub.fill = fill
            sub.font = white_bold
            sub.alignment = center
            sub.border = border

        # Data rows — colored delta tells the story.
        for i, row in enumerate(rows, start=first_data_row):
            pn, desc, total_ordered, live_total, delta = row

            ws.cell(i, ITEM_COL, i - first_data_row + 1).alignment = center
            pn_cell = ws.cell(i, PN_COL, pn)
            pn_cell.font = bold
            pn_cell.alignment = center
            ws.cell(i, DESC_COL, desc).alignment = left

            ocell = ws.cell(i, ORDERED_COL, total_ordered or None)
            ocell.alignment = center
            ocell.font = bold

            lcell = ws.cell(i, LIVE_COL, live_total or None)
            lcell.alignment = center
            lcell.font = bold

            dcell = ws.cell(i, DELTA_COL, delta)
            dcell.alignment = center
            # Red when Live falls short of what was ordered;
            # green when Live exceeds; bold black at zero.
            if delta < 0:
                dcell.font = red_font
            elif delta > 0:
                dcell.font = green_font
            else:
                dcell.font = bold

            for c in range(1, last_col + 1):
                ws.cell(i, c).border = border

        # Column widths + freeze panes
        for col, w in {
            ITEM_COL: 6, PN_COL: 18, DESC_COL: 60,
            ORDERED_COL: 14, LIVE_COL: 16, DELTA_COL: 12,
        }.items():
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.freeze_panes = f"D{first_data_row}"

        if not rows:
            note = ws.cell(first_data_row, 1, "No parts to compare — neither BoM lists any inventory.")
            note.alignment = center
            ws.merge_cells(
                start_row=first_data_row, end_row=first_data_row,
                start_column=1, end_column=last_col,
            )

    # ------------------------------------------------------------------
    # Site Allocation writer
    # ------------------------------------------------------------------

    @staticmethod
    def _site_key(name: str) -> str:
        """Extract a site-grouping key from a column / tab name.

        Matches the rollup heuristic the BOM aggregate parser uses:
        the leading run of letters defines the site bucket.
        ``MALN001_7250`` -> ``MALN``; ``ALBA Extra Materials`` ->
        ``ALBA``; ``MALN CB1`` -> ``MALN``. Used by the Site Allocation
        sheet to align Sales site columns (clean names) with Live
        Inventory tab names (chassis-suffixed).
        """
        m = re.match(r"^([A-Za-z]+)", (name or "").strip())
        return m.group(1).upper() if m else ""

    @staticmethod
    def _write_site_allocation_sheet(
        ws: Any,
        rows: List[Tuple[str, str, int, int, int]],
        sal_breakdown: Dict[str, Dict[str, int]],
        sal_site_order: List[str],
        live_breakdown: Dict[str, Dict[str, int]],
        live_site_order: List[str],
        aliases: Dict[str, str],
    ) -> None:
        """Write the per-site allocation tab.

        Each Shortfall PN gets one row. Each site gets a TWO-cell
        column pair: ``Need`` (Sales ordered at that site) +
        ``Have`` (Live qty in that site's bucket). Cells are
        colored individually by the per-site delta:

        * Need cell red AND Have cell red → site is SHORT (Have < Need).
        * Need cell green AND Have cell green → site is COVERED
          (Have ≥ Need, with at least one side non-zero).
        * Both blank → site has neither ordered nor delivered units of
          this PN; gridlines stay but no number prints.

        Site columns come in two groups:

        1. Sales sites (workbook order) — every distinct site name on
           the Sales BoM detail tabs (ALBA, ALVY, MALN, MALN CB1,
           Spares, Network, License Points, etc.).
        2. Live-only sites — Live Inventory site tabs whose
           leading-letter key doesn't match any Sales site (rare;
           covers pure-surplus locations that never had an order).

        Live qty is matched to Sales sites by the leading-letter key
        (``MALN001_7250`` rolls up to ``MALN``). That means CB sub-sites
        of the same parent (``MALN CB1`` / ``MALN CB2``) share the
        Live ``MALN`` bucket — which is intentional: the Live tabs
        don't carry sub-site granularity, so we surface the parent's
        pool against each sub-site's order.
        """
        thin = Side(style="thin", color="FF7F7F7F")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        accent_fill = PatternFill(
            start_color="FF0087FF", end_color="FF0087FF", fill_type="solid"
        )
        sales_fill = PatternFill(
            start_color="FF005EBA", end_color="FF005EBA", fill_type="solid"
        )
        live_fill = PatternFill(
            start_color="FF386030", end_color="FF386030", fill_type="solid"
        )
        white_bold = Font(color="FFFFFFFF", bold=True)
        bold = Font(bold=True)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        red_font = Font(bold=True, color="FFC00000")
        green_font = Font(bold=True, color="FF548235")

        # Group Live sites by leading-letter key so each Sales site can
        # find its matching Live pool.
        live_sites_by_key: Dict[str, List[str]] = {}
        for site in live_site_order:
            k = BomCompareFrame._site_key(site)
            if not k:
                continue
            live_sites_by_key.setdefault(k, []).append(site)
        sales_keys: set = {
            BomCompareFrame._site_key(s) for s in sal_site_order
        }
        # Live-only sites = Live tabs whose key never appears in Sales.
        live_only_sites = [
            s for s in live_site_order
            if BomCompareFrame._site_key(s) not in sales_keys
        ]

        # Final column list: each entry is
        # ``(header_label, "sales" | "live_only", source_site_name)``.
        # For "sales" entries, source_site_name is the Sales site name.
        # For "live_only" entries, source_site_name is the Live tab name.
        site_cols: List[Tuple[str, str, str]] = []
        for s in sal_site_order:
            site_cols.append((s, "sales", s))
        for s in live_only_sites:
            site_cols.append((s, "live_only", s))

        ITEM_COL, PN_COL, DESC_COL = 1, 2, 3
        FIRST_SITE_COL = 4
        # Each site occupies 2 cols (Need, Have).
        last_col = (
            FIRST_SITE_COL + 2 * len(site_cols) - 1 if site_cols else DESC_COL
        )

        # Title
        title_cell = ws.cell(
            1, 1, "Site Allocation — Need vs Have per Site"
        )
        title_cell.font = Font(bold=True, size=13)
        ws.merge_cells(
            start_row=1, end_row=1, start_column=1, end_column=max(last_col, 6)
        )

        hdr_row, sub_row, first_data_row = 3, 4, 5

        # Left-anchor headers (Item / PN / Description) merged across
        # the two header rows so the column titles stay visible.
        for col, label in (
            (ITEM_COL, "Item"),
            (PN_COL, "Part No."),
            (DESC_COL, "Equipment Description"),
        ):
            head = ws.cell(hdr_row, col, label)
            head.fill = accent_fill
            head.font = white_bold
            head.alignment = center
            head.border = border
            sub = ws.cell(sub_row, col)
            sub.fill = accent_fill
            sub.border = border
            ws.merge_cells(
                start_row=hdr_row, end_row=sub_row,
                start_column=col, end_column=col,
            )

        # Site headers — site name spans both sub-cols on row 3;
        # row 4 reads "Need" / "Have" so the operator can scan
        # diagonally to read either side.
        for i, (label, _kind, _src) in enumerate(site_cols):
            need_c = FIRST_SITE_COL + 2 * i
            have_c = need_c + 1
            head = ws.cell(hdr_row, need_c, label)
            head.fill = sales_fill
            head.font = white_bold
            head.alignment = center
            head.border = border
            # Stretch site name across the two sub-cols.
            ws.merge_cells(
                start_row=hdr_row, end_row=hdr_row,
                start_column=need_c, end_column=have_c,
            )
            # Empty cell still needs the fill so the merged region
            # reads as one styled block on save.
            tail = ws.cell(hdr_row, have_c)
            tail.fill = sales_fill
            tail.border = border
            need_sub = ws.cell(sub_row, need_c, "Need")
            need_sub.fill = sales_fill
            need_sub.font = white_bold
            need_sub.alignment = center
            need_sub.border = border
            have_sub = ws.cell(sub_row, have_c, "Have")
            have_sub.fill = live_fill
            have_sub.font = white_bold
            have_sub.alignment = center
            have_sub.border = border

        # Alias-fold both per-site breakdowns so canonical PN lookups
        # work the same way as the rest of the comparison.
        def _canon_fold(bd: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
            out: Dict[str, Dict[str, int]] = {}
            for pn, sites in bd.items():
                ck = canonical_part(pn, aliases)
                site_map = out.setdefault(ck, {})
                for site, q in sites.items():
                    site_map[site] = site_map.get(site, 0) + int(q)
            return out

        canon_sal = _canon_fold(sal_breakdown)
        canon_live = _canon_fold(live_breakdown)

        # One row per Shortfall PN, in the same order.
        for i, (pn, desc, _ordered, _live, _delta) in enumerate(
            rows, start=first_data_row
        ):
            ws.cell(i, ITEM_COL, i - first_data_row + 1).alignment = center
            pn_cell = ws.cell(i, PN_COL, pn)
            pn_cell.font = bold
            pn_cell.alignment = center
            ws.cell(i, DESC_COL, desc).alignment = left

            sal_site_map = canon_sal.get(pn, {})
            live_site_map = canon_live.get(pn, {})

            for j, (_label, kind, src) in enumerate(site_cols):
                need_c = FIRST_SITE_COL + 2 * j
                have_c = need_c + 1
                if kind == "sales":
                    need_qty = sal_site_map.get(src, 0)
                    # Sum Live qty across all Live tabs that share the
                    # site's leading-letter key.
                    key = BomCompareFrame._site_key(src)
                    have_qty = sum(
                        live_site_map.get(t, 0)
                        for t in live_sites_by_key.get(key, [])
                    )
                else:  # live_only
                    need_qty = 0
                    have_qty = live_site_map.get(src, 0)

                if need_qty <= 0 and have_qty <= 0:
                    # No interaction at this site for this PN — leave
                    # the cells blank so the eye skips past it.
                    continue

                # Pick per-site color: red when this site is SHORT,
                # green when it's covered or surplus.
                site_short = (need_qty > 0 and have_qty < need_qty)
                cell_font = red_font if site_short else green_font
                if need_qty:
                    nc = ws.cell(i, need_c, need_qty)
                    nc.font = cell_font
                    nc.alignment = center
                if have_qty:
                    hc = ws.cell(i, have_c, have_qty)
                    hc.font = cell_font
                    hc.alignment = center

            # Border every cell across the row so the grid stays
            # readable even when site cells are empty.
            for c in range(1, last_col + 1):
                ws.cell(i, c).border = border

        # Column widths: keep Item/PN/Desc generous; site cols narrow
        # since the qty rarely exceeds two digits.
        ws.column_dimensions[get_column_letter(ITEM_COL)].width = 6
        ws.column_dimensions[get_column_letter(PN_COL)].width = 18
        ws.column_dimensions[get_column_letter(DESC_COL)].width = 40
        for j in range(len(site_cols)):
            for sub in (0, 1):
                ws.column_dimensions[
                    get_column_letter(FIRST_SITE_COL + 2 * j + sub)
                ].width = 7
        ws.freeze_panes = f"D{first_data_row}"

        if not rows:
            note = ws.cell(
                first_data_row, 1,
                "No parts to compare — neither BoM lists any inventory.",
            )
            note.alignment = center
            ws.merge_cells(
                start_row=first_data_row, end_row=first_data_row,
                start_column=1, end_column=max(last_col, 6),
            )

    # ------------------------------------------------------------------
    # Trace writer
    # ------------------------------------------------------------------

    @staticmethod
    def _write_trace_sheet(
        ws: Any,
        rows: List[Tuple[str, str, int, int, int]],
        sal_entries: List[Tuple[str, str, int, int, str]],
        fac_entries: List[Tuple[str, str, int, int, str]],
        aliases: Dict[str, str],
        sal_kit_folds: Optional[List[Tuple[str, int, List[str]]]] = None,
        fac_kit_folds: Optional[List[Tuple[str, int, List[str]]]] = None,
    ) -> None:
        """Write the Trace sheet — a row per source contribution so the
        operator can verify any Shortfall number against a specific
        ``(side, source tab, row, qty)`` coordinate.

        Layout (10 cols):
          A=Item | B=PN | C=Description | D=Side | E=Source Tab |
          F=Source Row | G=Qty | H=Detail (serial / desc)

        One row per source contribution. Rows are restricted to PNs
        that appear on the Shortfall sheet (intangibles / NSP FP /
        software-license rows that were filtered upstream are NOT
        traced — by design, since they're not part of the
        comparison).

        Excel auto-filter is enabled on the data range so the operator
        can filter to a single PN and instantly see every source cell
        that contributed to its Total Ordered / Live Inventory totals.
        Sales-side rows sum to the Shortfall's Total Ordered for that
        PN; Live-side rows sum to Live Inventory.

        The ``aliases`` map collapses raw source PNs to canonical
        SKUs so trace rows under aliased forms (e.g. ``3HE13584AA``)
        attach to the canonical Shortfall row (``3HE11278AA``).
        """
        thin = Side(style="thin", color="FF7F7F7F")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        accent_fill = PatternFill(
            start_color="FF0087FF", end_color="FF0087FF", fill_type="solid"
        )
        sales_fill = PatternFill(
            start_color="FF005EBA", end_color="FF005EBA", fill_type="solid"
        )
        live_fill = PatternFill(
            start_color="FF386030", end_color="FF386030", fill_type="solid"
        )
        white_bold = Font(color="FFFFFFFF", bold=True)
        bold = Font(bold=True)
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left = Alignment(horizontal="left", vertical="center", wrap_text=True)

        ITEM_COL, PN_COL, DESC_COL = 1, 2, 3
        SIDE_COL, TAB_COL, ROW_COL, QTY_COL, DETAIL_COL = 4, 5, 6, 7, 8
        last_col = DETAIL_COL

        ws.cell(1, 1, "Trace — every source row that fed the Shortfall").font = Font(
            bold=True, size=13
        )
        ws.merge_cells(
            start_row=1, end_row=1, start_column=1, end_column=last_col
        )

        # Restrict to PNs that survived all filtering and appear on the
        # Shortfall. Anything else would be misleading (the operator
        # wouldn't see it on the Shortfall and wonder why it's in the
        # Trace).
        shortfall_pns = {pn: (desc, idx + 1) for idx, (pn, desc, *_rest) in enumerate(rows)}
        pn_desc_order = list(shortfall_pns.keys())

        # Group entries by canonical PN (alias-fold raw entries so
        # vendor-extended SKUs attach to their canonical Shortfall row).
        def _fold_entries(
            raw_entries: List[Tuple[str, str, int, int, str]],
        ) -> Dict[str, List[Tuple[str, str, int, int, str]]]:
            out: Dict[str, List[Tuple[str, str, int, int, str]]] = {}
            for entry in raw_entries:
                raw_pn = entry[0]
                ck = canonical_part(raw_pn, aliases)
                out.setdefault(ck, []).append(entry)
            return out

        sal_by_pn = _fold_entries(sal_entries)
        fac_by_pn = _fold_entries(fac_entries)

        # Index kit-fold operations by canonical kit SKU so we can emit
        # synthetic audit rows alongside the real source rows. The kit
        # SKU in the alias map is already canonical, but fold defensively
        # via canonical_part to handle alias-targeted kit SKUs.
        def _fold_kit_ops(
            ops: Optional[List[Tuple[str, int, List[str]]]],
        ) -> Dict[str, List[Tuple[int, List[str]]]]:
            out: Dict[str, List[Tuple[int, List[str]]]] = {}
            for kit, k, comps in (ops or []):
                ck = canonical_part(kit, aliases)
                out.setdefault(ck, []).append((k, comps))
            return out

        sal_folds_by_pn = _fold_kit_ops(sal_kit_folds)
        fac_folds_by_pn = _fold_kit_ops(fac_kit_folds)

        # Header row 3
        for col, label, fill in (
            (ITEM_COL, "Item", accent_fill),
            (PN_COL, "Part No.", accent_fill),
            (DESC_COL, "Equipment Description", accent_fill),
            (SIDE_COL, "Side", accent_fill),
            (TAB_COL, "Source Tab", accent_fill),
            (ROW_COL, "Source Row", accent_fill),
            (QTY_COL, "Qty", accent_fill),
            (DETAIL_COL, "Detail (Serial / Desc)", accent_fill),
        ):
            head = ws.cell(3, col, label)
            head.fill = fill
            head.font = white_bold
            head.alignment = center
            head.border = border

        # Data rows — for each PN on the Shortfall, emit Sales-side then
        # Live-side trace rows. Sales-side first because that's the
        # "ordered" side of the comparison.
        item_no = 0
        r = 4
        for pn in pn_desc_order:
            desc = shortfall_pns[pn][0]
            sal_rows = sorted(
                sal_by_pn.get(pn, []), key=lambda e: (e[1], e[2])
            )
            fac_rows = sorted(
                fac_by_pn.get(pn, []), key=lambda e: (e[1], e[2])
            )
            for side, src_rows, fold_ops, fill in (
                ("Sales", sal_rows, sal_folds_by_pn.get(pn, []), sales_fill),
                ("Live",  fac_rows, fac_folds_by_pn.get(pn, []), live_fill),
            ):
                for raw_pn, source_tab, source_row, qty, detail in src_rows:
                    item_no += 1
                    ws.cell(r, ITEM_COL, item_no).alignment = center
                    pn_cell = ws.cell(r, PN_COL, pn)
                    pn_cell.font = bold
                    pn_cell.alignment = center
                    # Include the raw PN in the detail when it differs
                    # from the canonical (aliased SKU shows where the
                    # vendor variant came from).
                    desc_text = desc
                    if raw_pn != pn:
                        desc_text = f"{desc}  [raw: {raw_pn}]"
                    ws.cell(r, DESC_COL, desc_text).alignment = left
                    side_cell = ws.cell(r, SIDE_COL, side)
                    side_cell.fill = fill
                    side_cell.font = white_bold
                    side_cell.alignment = center
                    ws.cell(r, TAB_COL, source_tab).alignment = center
                    ws.cell(r, ROW_COL, source_row).alignment = center
                    qty_cell = ws.cell(r, QTY_COL, qty)
                    qty_cell.alignment = center
                    qty_cell.font = bold
                    ws.cell(r, DETAIL_COL, detail).alignment = left
                    for c in range(1, last_col + 1):
                        ws.cell(r, c).border = border
                    r += 1
                # Synthetic kit-fold audit rows: one per fold of this
                # kit SKU on this side. Without these the Shortfall's
                # post-fold Live/Sales totals wouldn't reconcile with
                # the source-row sum in the Trace.
                for k_qty, components in fold_ops:
                    item_no += 1
                    ws.cell(r, ITEM_COL, item_no).alignment = center
                    pn_cell = ws.cell(r, PN_COL, pn)
                    pn_cell.font = bold
                    pn_cell.alignment = center
                    ws.cell(r, DESC_COL, desc).alignment = left
                    side_cell = ws.cell(r, SIDE_COL, side)
                    side_cell.fill = fill
                    side_cell.font = white_bold
                    side_cell.alignment = center
                    ws.cell(r, TAB_COL, "Kit Fold").alignment = center
                    ws.cell(r, ROW_COL, "—").alignment = center
                    qty_cell = ws.cell(r, QTY_COL, k_qty)
                    qty_cell.alignment = center
                    qty_cell.font = bold
                    ws.cell(
                        r, DETAIL_COL,
                        f"min(components: {', '.join(components)}) = {k_qty}",
                    ).alignment = left
                    for c in range(1, last_col + 1):
                        ws.cell(r, c).border = border
                    r += 1

        # Column widths + freeze panes + autofilter.
        widths = {
            ITEM_COL: 7, PN_COL: 18, DESC_COL: 38, SIDE_COL: 7,
            TAB_COL: 22, ROW_COL: 11, QTY_COL: 7, DETAIL_COL: 28,
        }
        for col, w in widths.items():
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.freeze_panes = "C4"
        last_data_row = max(r - 1, 4)
        ws.auto_filter.ref = (
            f"A3:{get_column_letter(last_col)}{last_data_row}"
        )

        if r == 4:  # no rows emitted
            note = ws.cell(
                4, 1,
                "No source rows to trace — neither BoM lists any "
                "comparable inventory.",
            )
            note.alignment = center
            ws.merge_cells(
                start_row=4, end_row=4,
                start_column=1, end_column=last_col,
            )

    # ------------------------------------------------------------------
    # Output path
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_out_path(factory_path: str) -> str:
        base, ext = os.path.splitext(factory_path)
        return f"{base}.COMPARE{ext or '.xlsx'}"
