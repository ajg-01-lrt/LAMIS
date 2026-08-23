"""Coverage audit: did the newly-built Sales BoM (21:27) capture every
relevant PN from the source?

For each PN with non-zero qty anywhere in the source (TO4 BoM v8 / CBR v1 /
CBR_bpa version), check whether it's present in the output. Per-PN
expected qty = sum of all the source columns we now treat as real
(per-site + Spares + Maintenance + Network + License points).

Categorize misses:
 * Intangible (correctly filtered by description / banner)
 * Aliased (correctly folded under canonical SKU)
 * Real hardware (would be a bug)
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import defaultdict
from openpyxl import load_workbook

SRC = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/source_TO4.xlsx")
OUT = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/sales_21_27.xlsx")

INTANGIBLE_KW = ("subscription", "support", "maintenance", "warranty",
                 "training", "installation", "license", " fp",
                 "feature pack")
# Tracked aliases (alias -> canonical)
ALIASES = {
    "3HE13584AA": "3HE11278AA",
    "3KC72037AA": "3KC72037AAAA",
    "8DG59417AA": "8DG59417AAXX",
    "8DG59427AA": "8DG59427AAXX",
    "8DG60207AA": "8DG60207AAAA",
    "1AD1519300": "1AD151930001",
    "1AB2151200": "1AB215120085",
}

def norm_pn(s):
    if s is None:
        return ""
    pn = str(s).split("\n", 1)[0].strip().upper()
    return re.sub(r"^(?:1P|P)", "", pn)


def canonical(pn):
    return ALIASES.get(pn, pn)


# ---------- Source: every PN with qty in any meaningful column ----------
src_wb = load_workbook(SRC, data_only=True)
source_parts: dict[str, dict] = {}
for sheet_name in ("TO4 BoM v8", "CBR v1", "CBR_bpa version"):
    if sheet_name not in src_wb.sheetnames:
        continue
    ws = src_wb[sheet_name]
    if sheet_name == "TO4 BoM v8":
        pn_col, desc_col, hdr_row = 2, 3, 11
        # per-site = cols 5..64 (TO4 sites only — excludes the bare meta
        # cols 65-78), plus the 3 special-bucket cols
        per_site = list(range(5, 65))
        bucket_cols = {65: "Spares", 66: "Maintenance", 67: "Network", 72: "License Points"}
    elif sheet_name == "CBR v1":
        pn_col, desc_col, hdr_row = 2, 3, 2
        per_site = list(range(5, 30))
        bucket_cols = {30: "Spares?", 31: "Maintenance?", 35: "License Points"}
    else:
        pn_col, desc_col, hdr_row = 1, 2, 2
        per_site = list(range(4, 27))
        bucket_cols = {}

    for r in range(hdr_row + 1, ws.max_row + 1):
        raw = ws.cell(r, pn_col).value
        if not raw:
            continue
        pn = norm_pn(raw)
        if not pn or len(pn) < 6:
            continue
        desc = ws.cell(r, desc_col).value
        desc_s = str(desc).split("\n", 1)[0].strip() if desc else ""

        def grab(c):
            v = ws.cell(r, c).value
            if v is None or v == "":
                return 0
            if isinstance(v, str):
                v = v.strip().strip('"').strip("'").strip()
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        per_site_total = sum(grab(c) for c in per_site)
        bucket_total = sum(grab(c) for c in bucket_cols)
        all_q = per_site_total + bucket_total
        if all_q == 0:
            continue
        entry = source_parts.setdefault(pn, {"desc": desc_s, "per_site": 0,
                                             "buckets": defaultdict(int), "total": 0})
        if not entry["desc"]:
            entry["desc"] = desc_s
        entry["per_site"] += per_site_total
        for c, label in bucket_cols.items():
            q = grab(c)
            if q:
                entry["buckets"][label] += q
        entry["total"] += all_q


# ---------- Output: every PN visible in the rebuilt Sales BoM ----------
out_wb = load_workbook(OUT, data_only=True)
out_pns: set[str] = set()
out_qty_by_pn: dict[str, int] = defaultdict(int)
# Aggregate BoM sheet first
if "BOM" in out_wb.sheetnames:
    ws = out_wb["BOM"]
    for r in range(10, ws.max_row + 1):
        pn = norm_pn(ws.cell(r, 2).value)
        if not pn:
            continue
        out_pns.add(pn)
        tot = ws.cell(r, 4).value  # Total Ordered col D
        if isinstance(tot, (int, float)):
            out_qty_by_pn[pn] = max(out_qty_by_pn[pn], int(tot))

print(f"Source PNs:        {len(source_parts)}")
print(f"Output PNs (BOM):  {len(out_pns)}")
print()

# ---------- Categorize missing ----------
missing_real = []
missing_intangible = []
missing_aliased = []

for pn, info in sorted(source_parts.items()):
    canon = canonical(pn)
    if pn in out_pns or canon in out_pns:
        continue
    desc_low = info["desc"].lower()
    is_intangible = any(kw in desc_low for kw in INTANGIBLE_KW)
    if pn != canon:
        missing_aliased.append((pn, canon, info))
    elif is_intangible:
        missing_intangible.append((pn, info))
    else:
        missing_real.append((pn, info))

print("=" * 72)
print(f"REAL hardware MISSING from output ({len(missing_real)})")
print("=" * 72)
for pn, info in missing_real:
    print(f"  {pn:<22} qty={info['total']:<6}  desc={info['desc']!r}")
    for label, q in info["buckets"].items():
        print(f"      {label}: {q}")
    if info["per_site"]:
        print(f"      per-site: {info['per_site']}")

print()
print("=" * 72)
print(f"Aliased PNs that landed under canonical SKU ({len(missing_aliased)})")
print("=" * 72)
for pn, canon, info in missing_aliased:
    canon_qty = out_qty_by_pn.get(canon, 0)
    in_out = "OK" if canon in out_pns else "ALSO MISSING"
    print(f"  {pn:<18} -> {canon:<18}  in output: {in_out}  output qty={canon_qty}")

print()
print("=" * 72)
print(f"Intangibles correctly filtered ({len(missing_intangible)}) — sample")
print("=" * 72)
for pn, info in missing_intangible[:10]:
    print(f"  {pn:<22} qty={info['total']:<6}  desc={info['desc'][:60]!r}")
if len(missing_intangible) > 10:
    print(f"  ... and {len(missing_intangible) - 10} more")

print()
print("=" * 72)
print("QUANTITY SPOT-CHECK: 9 hardware PNs we fixed today")
print("=" * 72)
TARGETS = ("3HE04962AB", "3HE06151AC", "3HE07942AA", "3HE11280AA",
           "3HE12518AA", "3HE14004AA", "3HE16713AA", "3HE16716AA", "3HE16718AA")
for pn in TARGETS:
    src_info = source_parts.get(pn)
    out_qty = out_qty_by_pn.get(pn, 0)
    if src_info:
        # Source qty is from the buckets only (these PNs have no per-site)
        bucket_str = ", ".join(f"{k}={v}" for k, v in src_info["buckets"].items())
        print(f"  {pn:<18} source={src_info['total']:<5} ({bucket_str}) | output={out_qty}")
