"""Structured measurement/parameter glossary for the ATLAS doc assistant.

"Find/display" questions (e.g. "OSC RX and TX power on RLS") are answered badly
by letting the model assemble commands from PM-parameter tables - it picks the
wrong component or drops a card restriction. This module extracts those tables
and command-parameter sections into a queryable glossary so such questions
resolve to a *fact* (parameter -> direction -> component -> restriction -> cite)
instead of a synthesized command.

Extraction is pure text parsing over the already-indexed chunks (no API/key),
so it can be built and validated offline. Direction is inferred from the
parameter name itself, which is reliable for these vendor params:
  *Input* / *In* / rx / received  -> RX (Receive)
  *Output* / *Out* / tx / transmit -> TX (Transmit)

Glossary rows live in a `glossary` table in the same doc_index DB.
"""

from __future__ import annotations

import re
import sqlite3
from typing import List, NamedTuple, Optional

from utils.ai.doc_index import DocIndex, get_index_db_path


# Candidate measurement parameter tokens worth indexing.
_TERM_RE = re.compile(
    r"\b("
    r"optical[A-Za-z]*"            # opticalPowerInput, opticalPowerOutput, opticalReturnLoss...
    r"|tx(?:calc|max)?power(?:thr)?"  # txpower, txcalcpower, txmaxpower, txmaxpowerthr
    r"|rx[a-z]*power[a-z]*"        # rxpower variants
    r"|supvy\s+in\s+power"         # 'Supvy In Power' (OSC receiver power, OLS)
    r")\b",
    re.IGNORECASE,
)

# Component tokens (UPPERCASE) used in the RLS PM tables.
_COMPONENTS = (
    "OSC", "PWRMON", "OPTMON", "AMP", "VOA", "SDMON", "NMCMON",
    "BOOSTER", "PREAMP", "OA", "EILA",
)
_COMPONENT_RE = re.compile(r"\b(" + "|".join(_COMPONENTS) + r")\b")

_RESTRICTION_RE = re.compile(
    r"(?:only applicable to|not applicable to|applicable to)\s+([^.\n]{1,60})",
    re.IGNORECASE,
)


def _direction(term: str) -> str:
    low = term.lower()
    if "output" in low or low.startswith("tx") or "transmit" in low or "out power" in low:
        return "TX"
    if ("input" in low or low.startswith("rx") or "received" in low
            or "in power" in low or low.endswith("in")):
        return "RX"
    return ""


def _doc_platform(doc_name: str) -> str:
    d = doc_name.lower()
    if "rls" in d or "2051" in d:
        return "RLS"
    if "1830" in d or "ols" in d:
        return "1830/OLS"
    if "saos_10" in d or "saos_10-11" in d:
        return "SAOS10"
    if "saos_6" in d:
        return "SAOS6"
    if "6500" in d or "1851" in d:
        return "6500"
    if "7250" in d or "ixr" in d:
        return "7250"
    if "7210" in d or "sas" in d:
        return "7210"
    return ""


class GlossaryHit(NamedTuple):
    platform: str
    component: str
    term: str
    direction: str       # RX | TX | ''
    restriction: str
    doc_name: str
    page: Optional[int]


def _nearest_component(text: str, pos: int) -> str:
    """Closest component token at or before pos (within ~200 chars)."""
    window = text[max(0, pos - 200):pos]
    matches = list(_COMPONENT_RE.finditer(window))
    return matches[-1].group(1) if matches else ""


def build_glossary(index: Optional[DocIndex] = None) -> int:
    """(Re)build the glossary table from the indexed chunks. Idempotent.

    Returns the number of glossary rows written.
    """
    own = index is None
    index = index or DocIndex()
    conn = index._conn  # reuse the same connection/DB
    conn.execute("DROP TABLE IF EXISTS glossary")
    conn.execute(
        """
        CREATE TABLE glossary (
            id          INTEGER PRIMARY KEY,
            platform    TEXT,
            component   TEXT,
            term        TEXT NOT NULL,
            direction   TEXT,
            restriction TEXT,
            doc_name    TEXT,
            page        INTEGER
        )
        """
    )
    rows = conn.execute("SELECT doc_name, page, text FROM chunks").fetchall()
    seen = set()
    out = []
    for r in rows:
        doc_name, page, text = r["doc_name"], r["page"], r["text"]
        platform = _doc_platform(doc_name)
        for m in _TERM_RE.finditer(text):
            term = m.group(1).strip()
            direction = _direction(term)
            if not direction:
                continue  # only keep params whose direction we can determine
            component = _nearest_component(text, m.start())
            # Restriction within a short window after the term.
            tail = text[m.end():m.end() + 200]
            rmatch = _RESTRICTION_RE.search(tail)
            restriction = ""
            if rmatch and ("only applicable" in rmatch.group(0).lower()
                           or "not applicable" in rmatch.group(0).lower()):
                restriction = rmatch.group(0).strip()
            key = (platform, component, term.lower(), direction, doc_name, page)
            if key in seen:
                continue
            seen.add(key)
            out.append((platform, component, term, direction, restriction, doc_name, page))
    conn.executemany(
        "INSERT INTO glossary (platform, component, term, direction, restriction, doc_name, page)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        out,
    )
    conn.commit()
    if own:
        index.close()
    return len(out)


def lookup(
    index: DocIndex,
    keywords: List[str],
    platform: str = "",
    direction: str = "",
    limit: int = 12,
) -> List[GlossaryHit]:
    """Return glossary rows whose term/component matches any keyword, optionally
    scoped by platform substring and/or direction (RX/TX)."""
    try:
        clauses, params = [], []
        if keywords:
            kw = " OR ".join(["lower(term) LIKE ? OR lower(component) LIKE ?"] * len(keywords))
            clauses.append(f"({kw})")
            for k in keywords:
                params += [f"%{k.lower()}%", f"%{k.lower()}%"]
        if platform:
            clauses.append("lower(platform) LIKE ?")
            params.append(f"%{platform.lower()}%")
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT platform, component, term, direction, restriction, doc_name, page"
            f" FROM glossary{where} ORDER BY platform, component, direction LIMIT ?"
        )
        rows = index._conn.execute(sql, params + [limit]).fetchall()
    except sqlite3.OperationalError:
        return []  # glossary not built yet
    return [
        GlossaryHit(r["platform"], r["component"], r["term"], r["direction"],
                    r["restriction"], r["doc_name"], r["page"])
        for r in rows
    ]
