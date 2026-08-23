"""One-shot updater for the external ATLAS Development Document.

Reads the .docx, applies a batch of structural edits (version bump,
test-count refresh, new bug-fix entries, new feature entries, refreshed
planned work), then saves once. Idempotent only in the sense that
re-running it will append duplicates; meant to be run by hand alongside
review of the resulting document.
"""
import sys
from copy import deepcopy

import docx
from docx.oxml.ns import qn

DOC_PATH = (
    r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/"
    r"Desktop/ATLAS Build Material/ATLAS_Development_Document.docx"
)


def find_paragraph(doc, predicate):
    """Return the first paragraph matching predicate, or None."""
    for p in doc.paragraphs:
        if predicate(p):
            return p
    return None


def insert_after(anchor_para, text, style):
    """Insert a new paragraph immediately after *anchor_para*."""
    new_para = anchor_para.insert_paragraph_before(text, style=style)
    # insert_paragraph_before places it BEFORE the anchor; swap so it's after.
    anchor_para._p.addnext(new_para._p)
    new_para._p.getparent().remove(new_para._p) if new_para._p in [] else None
    return new_para


def insert_block_after(anchor_para, blocks):
    """Insert a sequence of (text, style) tuples after *anchor_para*.

    Each new paragraph is inserted directly after the previous one so the
    sequence ends up in the order given.
    """
    cursor = anchor_para
    for text, style in blocks:
        new_para = anchor_para.insert_paragraph_before(text, style=style)
        cursor._p.addnext(new_para._p)
        cursor = new_para
    return cursor


def main():
    doc = docx.Document(DOC_PATH)

    # ------------------------------------------------------------------
    # 1. Header banner: v2.0.1 -> v2.0.4; refresh "Updated" date.
    # ------------------------------------------------------------------
    ver_para = find_paragraph(doc, lambda p: "v2.0.1" in p.text)
    if ver_para is not None:
        for run in ver_para.runs:
            if "v2.0.1" in run.text:
                run.text = run.text.replace("v2.0.1", "v2.0.4")

    date_para = find_paragraph(doc, lambda p: "Updated:" in p.text)
    if date_para is not None:
        # Rewrite the whole paragraph cleanly. Preserve the "Prepared:"
        # line on the first run if it lives there.
        for run in date_para.runs:
            if "Updated:" in run.text:
                # Replace from "Updated:" onward with the new date.
                pre, _sep, _rest = run.text.partition("Updated:")
                run.text = f"{pre}Updated: May 22, 2026"

    # ------------------------------------------------------------------
    # 2. Section 6.1 — test count 135 -> 327.
    # ------------------------------------------------------------------
    cov_para = find_paragraph(
        doc, lambda p: p.text.startswith("ATLAS maintains a pytest suite")
    )
    if cov_para is not None:
        for run in cov_para.runs:
            run.text = run.text.replace(
                "All 135 tests pass on the current LAMIS_2.0 branch",
                "All 327 tests pass on the current LAMIS_2.0 branch",
            )
    pytest_out = find_paragraph(doc, lambda p: p.text.strip() == "135 passed")
    if pytest_out is not None:
        for run in pytest_out.runs:
            if "135 passed" in run.text:
                run.text = run.text.replace("135 passed", "327 passed")

    # ------------------------------------------------------------------
    # 3. New Section 4 feature entries — insert right before Section 5.
    # ------------------------------------------------------------------
    sec5_para = find_paragraph(
        doc, lambda p: p.text.startswith("Section 5") and p.style.name == "Heading 1"
    )
    # Anchor is the paragraph BEFORE Section 5 (so we insert "after anchor").
    if sec5_para is not None:
        # Build blocks in document order. We insert them by repeatedly
        # placing each new paragraph just before Section 5.
        new_section_blocks = [
            ("4.6  Asset Tag Tracking", "Heading 2"),
            (
                "Every generated workbook's Summary sheet exposes an operator-editable "
                "Asset Tag column at E9 / E10+. Values typed in alongside each device row "
                "propagate automatically on the next BoM build, Raw report export, or "
                "Packing Slip generation — the tag lands in column G of the matching "
                "device tab's Chassis / Shelf row so the physical asset identifier travels "
                "with the equipment list. The propagation is implemented in "
                "WorkbookBuilder.propagate_asset_tags_to_tabs and is wired into the "
                "inventory, PSI, unified, packing-slip, and Build-a-BOM save paths.",
                "Normal",
            ),
            ("4.7  BoM Comparison (Sales vs Factory)", "Heading 2"),
            (
                "gui/bom_compare_frame.py loads a Factory BoM workbook and a Sales BoM "
                "workbook and emits a four-tab comparison: Shortfall, Spares Allocation, "
                "Factory BoM, and Sales BoM. The Shortfall tab mirrors the Sales site "
                "columns and flags per-site missing quantities; the Spares Allocation tab "
                "distributes available spares across the needy factory locations using a "
                "weighted largest-remainder rule.",
                "Normal",
            ),
            (
                "Key refinements: (1) a part-number alias table at "
                "data/part_aliases.json collapses marketing / bundle SKUs into their "
                "canonical shipping equivalents before the diff so e.g. 3HE13584AA "
                "(7250 IXR-R6 CHASSIS BUNDLE) on the Sales side correlates with "
                "3HE11278AA (7250 IXR-R6 CHASSIS) on the Factory side; (2) Sales site "
                "columns with zero shortfall are hidden from the Shortfall tab so the "
                "operator's eye lands on what's still missing; (3) tab names were "
                "chosen to read clearly even out of context — Shortfall, Spares Allocation.",
                "Normal",
            ),
            ("4.8  Auto-Updater", "Heading 2"),
            (
                "utils/update.py queries the project's GitHub Releases feed at startup, "
                "compares the latest tag against config.APP_VERSION, and surfaces "
                "availability through the Help menu indicator. When the operator confirms "
                "the prompt, ATLAS downloads the signed ATLAS_Setup.exe, verifies its "
                "SHA-256 against the hash published in the release body, and hands off "
                "to the NSIS installer. In dev mode the same code path falls back to a "
                "working-tree probe so engineers can test the flow without cutting a "
                "real release.",
                "Normal",
            ),
            ("4.9  Consolidated TDS Subprocess", "Heading 2"),
            (
                "TDS (Test Diagnostic System) ships as part of the single ATLAS.exe "
                "rather than as a separate binary. main.py inspects sys.argv for "
                "--tds-mode before any GUI imports; when present, it strips the flag "
                "and runpy-executes the bundled scripts/TDS/TDS_v6.2.py source so the "
                "17k-line TDS script runs unmodified. This keeps the installer footprint "
                "down to one visible executable and removes a packaging step from the "
                "build pipeline.",
                "Normal",
            ),
        ]
        # Insert each block immediately before Section 5; iterating in order
        # means the last inserted ends up closest to Section 5 — which is
        # exactly what we want when the blocks are listed in document order.
        for text, style in new_section_blocks:
            sec5_para.insert_paragraph_before(text, style=style)

    # ------------------------------------------------------------------
    # 4. New Bug Fix entries 5.9 and 5.10 — insert right before Section 6.
    # ------------------------------------------------------------------
    sec6_para = find_paragraph(
        doc, lambda p: p.text.startswith("Section 6") and p.style.name == "Heading 1"
    )
    if sec6_para is not None:
        bug_blocks = [
            ("5.9  Raw Frame — Dead PSI Auto-Detect Branch (Nokia-8L)", "Heading 2"),
            ("Problem", "Heading 3"),
            (
                "gui/raw_frame.py:_detect_nokia_raw_script contained two consecutive PSI "
                "detection regexes — one for nokia-4l variants and one for nokia-8l. "
                "Both alternations included a bare \"psi\" and a bare \"nokia\" via an "
                "optional suffix group, so the first pattern always matched any string "
                "the second would have matched, leaving the 8L branch fully unreachable. "
                "Raw transcripts from PSI-8L hardware were detected only because the "
                "first regex happened to also match them, masking the dead code.",
                "Normal",
            ),
            ("Fix", "Heading 3"),
            (
                "Collapsed the two patterns into a single regex "
                "`(psi|nokia(?:-[48]l)?)` that covers both 4L and 8L variants without "
                "redundant checks. Added unit tests in tests/test_raw_processing.py that "
                "feed sample text containing 'NOKIA-4L', 'NOKIA-8L', and bare 'psi' "
                "keywords and assert each resolves to scripts.Nokia_PSI.",
                "Normal",
            ),
            ("Justification", "Heading 3"),
            (
                "Removing the dead branch eliminates a class of silent maintenance "
                "hazard — a future edit to the second regex would have had no effect "
                "but appeared correct in review. Tightening the regex also documents "
                "the intent explicitly (both PSI hardware sizes are supported).",
                "Normal",
            ),
            ("5.10  Raw Frame — Multi-Device Family Detection Bypassed PSI/RLS Builders", "Heading 2"),
            ("Problem", "Heading 3"),
            (
                "When the operator selected \"Auto Detect Nokia\" and uploaded a "
                "multi-sheet workbook (or a folder of transcripts), _process_multi "
                "derived the workbook-builder family from the dropdown's raw value "
                "(SCRIPT_OPTIONS['Auto Detect Nokia'] == '') instead of from the "
                "per-sheet resolved module path. _FAMILY_BY_MODULE.get('') returned "
                "the 'default' family, which meant every multi-device PSI capture was "
                "exported through build_report_workbook rather than the PSI-specific "
                "build_psi_report_workbook (and similarly for RLS). Single-device runs "
                "were unaffected because _process_single used the resolved module path.",
                "Normal",
            ),
            ("Fix", "Heading 3"),
            (
                "Track the resolved module path inside the per-sheet parse loop and "
                "derive the family from that aggregated set after the loop. When the "
                "set contains exactly one non-default family it wins; mixed sets fall "
                "back to 'default'. Added three regression tests covering single-family "
                "multi-PSI, multi-IXR, and mixed PSI+IXR inputs to lock the routing.",
                "Normal",
            ),
            ("Justification", "Heading 3"),
            (
                "PSI and RLS workbooks have specialized layouts (additional metadata "
                "rows, vendor-specific section banners) that the default builder does "
                "not produce. Silently falling through to the default builder meant "
                "multi-device PSI exports were missing template elements that field "
                "engineers relied on. The fix makes the multi-device path behave the "
                "same way the single-device path already did.",
                "Normal",
            ),
        ]
        for text, style in bug_blocks:
            sec6_para.insert_paragraph_before(text, style=style)

    # ------------------------------------------------------------------
    # 5. Refresh Section 6.4 Planned Work — strike completed items, add
    #    new ones. We rewrite individual bullet texts and append fresh
    #    bullets right after the existing block.
    # ------------------------------------------------------------------
    # Find the 6.4 heading, then walk forward until the next heading.
    bullets_to_keep = [
        # Mark in-progress / partially done items with status prefix.
        "Ciena RLS full integration — wire Ciena_RLS.py into the concurrent "
        "scan pipeline; evaluate RESTCONF-based data collection.",
        "Nokia PSI full integration — concurrent-pipeline wiring is live "
        "(LAN/Serial path); next step is site-type auto-detection "
        "(ILA / ROADM-ADD/DROP).",
        "TDS tab completion — execute pipeline now lives inside ATLAS.exe via "
        "--tds-mode dispatch; remaining work is the execute-and-report UI "
        "for Ciena TDS nodes.",
        "Part database UI — admin tab for importing part number / description "
        "CSV files into network_inventory.db (still outstanding).",
        "macOS packaging — evaluate PyInstaller + pexpect substitution for "
        "cross-platform builds.",
        "Crash / log reporter — auto-bundle the rotating session log and ship "
        "to zsimino@lightriver.com on opt-in or per-incident; Sentry or a "
        "self-hosted Worker relay are the two candidate transports.",
        "Part-number alias table maintenance — extend data/part_aliases.json "
        "as additional bundle/component SKU pairs are identified during BoM "
        "Comparison runs.",
    ]

    # Locate the 6.4 heading and the next heading after it.
    paras = list(doc.paragraphs)
    idx_64 = None
    for i, p in enumerate(paras):
        if p.style.name == "Heading 2" and p.text.startswith("6.4"):
            idx_64 = i
            break
    if idx_64 is not None:
        # Find first heading after 6.4 (this is Section 7 or end of doc).
        idx_end = len(paras)
        for j in range(idx_64 + 1, len(paras)):
            if paras[j].style.name.startswith("Heading"):
                idx_end = j
                break
        # Identify existing bullets in [idx_64+1, idx_end).
        old_bullet_paras = [
            paras[k] for k in range(idx_64 + 1, idx_end)
            if paras[k].style.name == "List Bullet"
        ]
        # Delete the old bullets entirely so we re-render a clean list.
        for op in old_bullet_paras:
            op._p.getparent().remove(op._p)
        # The anchor for new bullets is now the 6.4 heading itself; we
        # insert each new bullet directly after it (so they appear in the
        # given order, the last inserted ends up right under the heading).
        anchor = paras[idx_64]._p
        for text in reversed(bullets_to_keep):
            new_para = doc.add_paragraph(text, style="List Bullet")
            anchor.addnext(new_para._p)

    # ------------------------------------------------------------------
    # Save.
    # ------------------------------------------------------------------
    doc.save(DOC_PATH)
    print(f"Saved: {DOC_PATH}")


if __name__ == "__main__":
    main()
