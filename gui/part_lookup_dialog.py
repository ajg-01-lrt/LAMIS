"""Help → Part Lookup dialog.

Operators often have a part in hand with no legible markings — the box
ships labeled, but the bare card / SFP / etc. is unlabeled. The factory
parts DB has the descriptions ATLAS uses when building reports, so just
exposing that lookup directly saves a trip into a BoM tool.

The dialog tries to be forgiving about input form:

* **Case-insensitive** — ``xcvr-a10y31`` finds ``XCVR-A10Y31``.
* **Vendor-prefix tolerant** — ``1PXCVR-A10Y31`` and ``PXCVR-A10Y31``
  both strip down to ``XCVR-A10Y31`` for matching. (The DB was deduped
  to canonical-only rows in v2.0.6.x; without the strip the prefixed
  forms would no longer hit.)
* **Prefix matches** — when there's no exact hit we still show up to 10
  rows whose part_number starts with the query, so a partial SKU like
  ``3HE048`` lists every 3HE048* part.

Double-clicking a suggestion copies it back into the search box and
re-queries — useful for drilling from a prefix into the exact match.
"""
from __future__ import annotations

import re
import sqlite3
import tkinter as tk
from tkinter import ttk
from typing import List, Optional, Tuple

# Same vendor-prefix rule the BoM compare layer uses. Keeping the
# regex local to this module means we don't import gui.bom_compare_frame
# (which would pull in a huge dependency graph for a tiny dialog).
_VENDOR_PREFIX_RE = re.compile(r"^(?:1P|P)", re.IGNORECASE)


def _strip_vendor_prefix(pn: str) -> str:
    """Strip a leading ``1P`` / ``P`` and uppercase, matching how the
    rest of ATLAS canonicalizes part numbers."""
    return _VENDOR_PREFIX_RE.sub("", pn.strip()).upper()


def lookup_part(
    db_path: str, query: str, max_suggestions: int = 10,
) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """Look up *query* in the parts DB.

    Returns ``(exact_description, suggestions)``:

    * ``exact_description`` is the description string if *query* (after
      vendor-prefix strip + uppercase) matches a ``part_number`` row
      exactly, else ``None``.
    * ``suggestions`` is up to *max_suggestions* ``(part_number,
      description)`` tuples whose ``part_number`` starts with the
      normalized query, with the exact hit (if any) excluded so the
      operator isn't shown the same row twice.

    Both lookups run against the live runtime DB — same one BoM Compare
    and live inventory write to — so the answer matches what would land
    in a freshly-built report.
    """
    if not query or not query.strip():
        return None, []
    normalized = _strip_vendor_prefix(query)
    if not normalized:
        return None, []

    con = sqlite3.connect(db_path)
    try:
        # Exact match — UPPER() so the lookup is case-insensitive on the
        # DB side, since SQLite TEXT comparisons are case-sensitive by
        # default. UPPER() defeats the UNIQUE index but the table is
        # ~43k rows so a full scan is still sub-100ms.
        row = con.execute(
            "SELECT description FROM parts WHERE UPPER(part_number) = ?",
            (normalized,),
        ).fetchone()
        exact: Optional[str] = row[0] if row else None

        # Prefix matches. Pull one more than we need so we can drop the
        # exact-hit row and still return ``max_suggestions`` candidates.
        like_param = normalized + "%"
        rows = con.execute(
            "SELECT part_number, description FROM parts "
            "WHERE UPPER(part_number) LIKE ? "
            "ORDER BY part_number "
            "LIMIT ?",
            (like_param, max_suggestions + 1),
        ).fetchall()
    finally:
        con.close()

    suggestions: List[Tuple[str, str]] = []
    for pn, desc in rows:
        if exact is not None and pn.upper() == normalized:
            continue  # Don't list the exact hit in the suggestion box.
        suggestions.append((pn, desc))
        if len(suggestions) >= max_suggestions:
            break
    return exact, suggestions


class PartLookupDialog(tk.Toplevel):
    """Non-modal Tk Toplevel for ad-hoc part-number lookups.

    Non-modal on purpose — the operator may want to keep an inventory
    run going in the background while they look up what's in their
    hand. The dialog uses ``transient(parent)`` so it stays grouped
    with the main window in the taskbar but doesn't block input to it.
    """

    def __init__(self, parent: tk.Misc, db_path: str) -> None:
        super().__init__(parent)
        self.title("Part Lookup")
        self.transient(parent)
        self.geometry("560x440")
        self.minsize(420, 320)
        self._db_path = db_path

        # ── Search row ──────────────────────────────────────────────────
        search_frame = ttk.Frame(self)
        search_frame.pack(fill=tk.X, padx=10, pady=(10, 4))
        ttk.Label(search_frame, text="Part Number:").pack(side=tk.LEFT)
        self._query_var = tk.StringVar()
        self._entry = ttk.Entry(
            search_frame, textvariable=self._query_var, width=32
        )
        self._entry.pack(side=tk.LEFT, padx=(6, 4), fill=tk.X, expand=True)
        self._entry.bind("<Return>", self._on_search)
        ttk.Button(search_frame, text="Search", command=self._on_search).pack(
            side=tk.LEFT, padx=(2, 0)
        )

        # ── Result panel ────────────────────────────────────────────────
        result_frame = ttk.LabelFrame(self, text="Description")
        result_frame.pack(fill=tk.X, padx=10, pady=4)
        self._result_text = tk.Text(
            result_frame, height=3, wrap=tk.WORD, font=("Segoe UI", 10),
            relief=tk.FLAT, background=self.cget("background"),
        )
        self._result_text.pack(fill=tk.X, padx=8, pady=8)
        self._result_text.config(state=tk.DISABLED)
        self._result_text.tag_configure("hit", foreground="#1a5e1a")
        self._result_text.tag_configure("miss", foreground="#9a1a1a")
        self._result_text.tag_configure("hint", foreground="gray")

        # ── Suggestions panel ───────────────────────────────────────────
        suggest_frame = ttk.LabelFrame(
            self, text="Other matches (double-click to expand)"
        )
        suggest_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        list_holder = ttk.Frame(suggest_frame)
        list_holder.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._suggest_list = tk.Listbox(
            list_holder, font=("Consolas", 9), activestyle="dotbox",
        )
        scrollbar = ttk.Scrollbar(
            list_holder, orient="vertical", command=self._suggest_list.yview
        )
        self._suggest_list.configure(yscrollcommand=scrollbar.set)
        self._suggest_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._suggest_list.bind("<Double-Button-1>", self._on_suggestion_pick)
        self._suggest_list.bind("<Return>", self._on_suggestion_pick)

        # ── Close button ────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(btn_frame, text="Close", command=self.destroy).pack(
            side=tk.RIGHT
        )

        self._set_result(
            "Type a part number above and press Enter or Search.",
            tag="hint",
        )

        self._entry.focus_set()
        self.bind("<Escape>", lambda _e: self.destroy())

    # ── Event handlers ──────────────────────────────────────────────────

    def _on_search(self, _event: Optional[tk.Event] = None) -> None:
        query = self._query_var.get().strip()
        self._suggest_list.delete(0, tk.END)
        if not query:
            self._set_result(
                "Type a part number above and press Enter or Search.",
                tag="hint",
            )
            return
        try:
            exact, suggestions = lookup_part(self._db_path, query)
        except Exception as exc:
            self._set_result(f"Lookup failed: {exc}", tag="miss")
            return

        if exact:
            self._set_result(exact, tag="hit")
        else:
            self._set_result(
                f"No exact match for {query!r}.", tag="miss",
            )

        if not suggestions:
            if not exact:
                self._suggest_list.insert(
                    tk.END, "(no prefix matches in the parts DB)"
                )
            return
        for pn, desc in suggestions:
            self._suggest_list.insert(tk.END, f"{pn:<20s}  {desc}")

    def _on_suggestion_pick(self, _event: Optional[tk.Event] = None) -> None:
        idx = self._suggest_list.curselection()
        if not idx:
            return
        line = self._suggest_list.get(idx[0])
        if line.startswith("("):
            return  # Placeholder row, not a real part number.
        # Lines are "<part_number><spaces><description>"; grab the first
        # whitespace-delimited token as the SKU.
        pn = line.split(None, 1)[0]
        self._query_var.set(pn)
        self._on_search()

    # ── Helpers ─────────────────────────────────────────────────────────

    def _set_result(self, text: str, *, tag: str = "hit") -> None:
        self._result_text.config(state=tk.NORMAL)
        self._result_text.delete("1.0", tk.END)
        self._result_text.insert("1.0", text, tag)
        self._result_text.config(state=tk.DISABLED)
