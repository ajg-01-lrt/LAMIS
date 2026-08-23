"""Grounded question-answering over the ATLAS doc index (RAG Phase 1).

Flow per question:
    1. Embed the question.
    2. Vector-search the index for the top-K most relevant chunks (local, free).
    3. Hand ONLY those chunks to the model with a strict "answer from these or
       say you can't" instruction.
    4. Return the answer plus the citations it drew from.

The guardrail in SYSTEM_PROMPT is the whole point: for a tool that drives live
equipment, "the docs don't cover that" must be a first-class answer, never a
plausible-sounding invention. See the Nokia-1830-BGP case — asking about a
feature a platform may not have should yield a refusal, not a fabricated config.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional

import config
from utils.ai.doc_index import DocIndex, Hit
from utils.ai.provider import AIProvider, default_provider


SYSTEM_PROMPT = (
    "You are the ATLAS documentation assistant. You write step-by-step "
    "procedures that a brand-new field technician can follow exactly. Use ONLY "
    "the numbered documentation excerpts below; each is labelled with its source "
    "document and page. Follow these rules without exception:\n"
    "1. Build the best step-by-step you can from the commands and procedures in "
    "the excerpts, EVEN WHEN no section is titled exactly like the question - "
    "e.g. 'change the management IP' is served by a 'Modify an IP interface' "
    "command. Connect the question to the relevant commands rather than "
    "refusing on a wording mismatch. Refuse ONLY when the excerpts contain NO "
    "command relevant to the task (then say plainly you don't find it in the "
    "indexed documentation). Never invent a command to avoid refusing - the "
    "verbatim rule below still applies.\n"
    "2. FORMAT - PROCEDURE PASSTHROUGH FIRST: if an excerpt contains a numbered "
    "procedure that performs the task (a 'Procedure N ...' heading followed by "
    "'Step Action 1 ... 2 ...'), reproduce THAT procedure as your answer: its "
    "title, each numbered step exactly as written with its command in a fenced "
    "code block copied exactly, plus the doc's 'where <param> is ...' parameter "
    "explanations. Do NOT rephrase, reorder, renumber, or simplify it - the "
    "doc's own procedure is authoritative. ONLY if no matching numbered "
    "procedure exists, build your own numbered step-by-step from the individual "
    "commands in the excerpts (each command still copied exactly, in a fenced "
    "block, one per line).\n"
    "3. COMMAND FIDELITY (critical): every command MUST be copied VERBATIM from "
    "an excerpt - character for character, including 'no'/'config'/punctuation. "
    "Do NOT add, drop, reorder, paraphrase, translate, or merge tokens, and do "
    "NOT combine fragments from different commands. Keep the command's FULL "
    "printed form, including any {[ ]} optional-argument notation and option "
    "lists - do NOT simplify them or strip brackets. Replace only the example "
    "values (IP addresses, names) with <placeholders>. Then, in the step's "
    "prose, tell the tech which argument(s) to fill in (e.g. 'use the ip "
    "option'). If a step REQUIRES a command but it is not present verbatim in "
    "any excerpt, write '(command not in retrieved documentation)' instead of "
    "inventing one. If a step is an inspection, observation, reboot, or 'contact "
    "support' action that needs no command, just state the action in prose - do "
    "NOT put it in a code block and do NOT add that annotation. NEVER turn a "
    "procedure title or alarm name into a command: e.g. a heading like 'Clear "
    "ALLCHANMISS-OUT-L' or 'Procedure 8 Amplifier in Clamped State' is a TITLE, "
    "not a runnable command - never emit it as one.\n"
    "3a. Label each step by what the command ACTUALLY does per the docs: a "
    "'show' command DISPLAYS/reads, 'config'/'set' PROVISIONS, and only "
    "'start-...'/'action' style commands INITIATE an operation. Do NOT relabel "
    "a 'show' or 'config' command as 'start/run/enable' just because the "
    "question asked how to start or run something. If the excerpts contain no "
    "command that performs the requested action (e.g. the operation is "
    "auto-triggered), say so plainly instead of mislabeling a display command.\n"
    "4. PLATFORM AND VERSION ISOLATION (critical): products AND major releases "
    "use different CLIs - e.g. Ciena SAOS 6 vs SAOS 10, 6500 R16.9 vs R17.0, "
    "RLS, 1830/OLS, Nokia 7250/7210 are all distinct. Use ONLY excerpts matching "
    "the platform AND release the question is about. If the only excerpts are a "
    "different platform/version, say so plainly and do not use their commands. "
    "If the question covers MORE THAN ONE platform, answer each in its OWN "
    "clearly-labelled section (e.g. 'For RLS:' / 'For 1830 OLS:'), drawing each "
    "section's commands ONLY from that platform's excerpts. NEVER label one "
    "platform's command as another's or merge them into one list.\n"
    "5. Start with any prerequisites and reproduce any CAUTION/WARNING from the "
    "docs (e.g. risk of losing connectivity).\n"
    "5a. If the question asks to UNDERSTAND, explain, or diagnose (e.g. an alarm "
    "or fault), first give the documented explanation - Probable cause, Impact, "
    "and any relevant Notes from the excerpt - then the resolution steps, so the "
    "tech understands what the condition means.\n"
    "6. Cite the excerpt [n] each step's command came from. Only use numbers "
    "that appear below; never invent one.\n"
    "7. If the excerpts cover only part of the procedure, say so and tell the "
    "technician to read the full cited pages before touching live equipment.\n"
    "8. If the excerpts say a value is set automatically/by default, say that "
    "plainly instead of implying a manual command exists.\n"
    "9. If a PARAMETER REFERENCE block is provided, it is the AUTHORITATIVE "
    "source for measurement parameter names, their direction (RX/TX), and card "
    "restrictions. Use those exact parameters, state the direction, and ALWAYS "
    "state any card restriction it lists (e.g. 'only applicable to OMDWB card'). "
    "Prefer it over guessing from prose. Still copy command syntax verbatim "
    "(rule 3).\n"
    "9a. A measurement parameter may be (i) a command OPTION you append - only "
    "if it appears in that command's input/option list in an excerpt; (ii) an "
    "OUTPUT FIELD you READ from the command's output - typically multi-word "
    "Title-Case names like 'Supvy In Power' or 'OSC Target Output Power'; or "
    "(iii) a PM leaf. For an OUTPUT FIELD, show the display command (use its "
    "'detail' form if present) and tell the tech to read that field in the "
    "output - do NOT append an output-field name to the command as an argument."
)

NO_CONTEXT_ANSWER = (
    "I don't find anything about that in the indexed documentation. "
    "No relevant vendor docs are loaded for this question."
)

# HyDE: draft a throwaway hypothetical answer to steer retrieval toward
# command/config pages. Never shown to the user; correctness doesn't matter.
HYDE_PROMPT = (
    "You help search vendor network-equipment documentation. Given the "
    "question, write a brief (2-5 line) example of the documentation passage "
    "or CLI commands that would answer it, including plausible command syntax. "
    "This text is used ONLY to find relevant docs by similarity and is never "
    "shown to anyone, so approximate syntax is fine. Output only the example."
)


class Answer(NamedTuple):
    text: str               # the model's prose (no command syntax of its own)
    citations: List[Hit]    # numbered sources, [i] -> citations[i-1]
    excerpts: List[str] = ()  # verbatim doc text shown to the tech, parallel to citations


_CITED_RE = re.compile(r"\[(\d+)\]")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\n?(.*?)```", re.DOTALL)

# Page furniture (running headers/footers) that clutters the verbatim view.
# Display-only stripping; never touches the text used for command verification.
_FURNITURE_RES = [
    # Nokia confidentiality footer + copyright.
    re.compile(r"©?\s*\d{4}(?:-\d{4})?\s*Nokia\.?\s*Nokia Confidential Information"
               r"[^.]*?disclosure and use\.?", re.IGNORECASE),
    re.compile(r"Copyright\s*©?\s*\d{4}(?:-\d{4})?\s*Ciena[^.]*?Corporation\s+\w+\s+\d{4}",
               re.IGNORECASE),
    re.compile(r"©\s*\d{4}\s*Nokia\.?", re.IGNORECASE),
    # Doc numbers, release/issue stamps.
    re.compile(r"\b3KC[-\dA-Z]+\b"),
    re.compile(r"\b\d{3}-\d{4}-\d{3}\b"),
    re.compile(r"\bRelease\s+\d+(?:\.\d+)*(?:\s+\w+\s+\d{4})?", re.IGNORECASE),
    re.compile(r"\bStandard\b|\bIssue\s+\d+\b|\bRevision\s+[A-Z]\b", re.IGNORECASE),
    # Repeated product/guide running headers.
    re.compile(r"\bNokia 1830 OLS\b", re.IGNORECASE),
    re.compile(r"39XX/51XX Switches and Platforms", re.IGNORECASE),
    re.compile(r"6500 Reconfigurable Line System", re.IGNORECASE),
    re.compile(r"\bSAOS\s+\d+(?:\.\d+)+\b", re.IGNORECASE),
]

# Structural markers to start a new line on, so the flattened text gets shape.
_BREAK_BEFORE_RE = re.compile(
    r"(?=(?:Procedure\s+\d|Step\s+Action\b|Steps\b|Note\s*\d*\s*:|where\s|—end—|"
    r"Command examples?\b|Examples?\b|Access Level\b|Requirements\b|Overview\b|"
    r"Prerequisites\b|Purpose\b|Input Format\b|Output Parameters\b|Table\s+\d|"
    r"Restrictions\b))"
)


def _format_verbatim(text: str) -> str:
    """Make a flattened PDF excerpt readable: drop repeated page furniture and
    break the run-on text into lines at documentation structure markers. Purely
    cosmetic — the original text is what command verification uses."""
    for rx in _FURNITURE_RES:
        text = rx.sub(" ", text)
    text = _BREAK_BEFORE_RE.sub("\n", text)
    # Put a colon-introduced command on its own line ("...mask: interface ..." ->
    # "...mask:\ninterface ...") so commands stand out from the prose.
    text = re.sub(
        r":\s+(?=(?:interface|show|set|delete|no |config|dhcp|oc-if|rib|batch|"
        r"commit|vlan|activate|deactivate)\b)",
        ":\n", text, flags=re.IGNORECASE,
    )
    text = re.sub(r"[ \t]{2,}", " ", text)      # collapse runs of spaces
    text = re.sub(r" *\n *", "\n", text)         # trim around newlines
    text = re.sub(r"\n{3,}", "\n\n", text)       # cap blank runs
    return text.strip()


def _norm(s: str) -> str:
    """Normalize for command verification: lowercase, fold any <placeholder> to
    a single token, and remove ALL whitespace. Whitespace is dropped because
    PDF extraction mangles spacing ('interface set' -> 'interfaceset'); a real
    verbatim copy must still match despite that, so only token *content*
    differences (a genuinely altered command) should fail verification."""
    return re.sub(r"\s+", "", re.sub(r"<[^>]*>", "<>", s.lower()))


def _looks_like_prose(line: str) -> bool:
    """Tell a CLI command from documentation prose that ended up inside a fenced
    block (e.g. the model over-fenced, or a stray fence desynced pairing).
    Commands are short, aren't markdown/bullets, and carry no sentence
    punctuation. High-precision so real commands are never dropped."""
    if line[:1] in "#-•*>":
        return True
    if len(line) > 100 or line.endswith(":"):
        return True
    if ". " in line or "? " in line or "! " in line:
        return True
    return False


def unverified_commands(answer_text: str, context_blob: str) -> List[str]:
    """Return command lines in the answer's fenced code blocks that do NOT
    appear verbatim (modulo whitespace/placeholders) in the retrieved context.

    This is the safety net for letting the model put commands inline: anything
    it emits that isn't actually in the docs gets flagged rather than trusted.
    """
    ctx = _norm(context_blob)
    flagged = []
    for block in _FENCE_RE.findall(answer_text):
        for line in block.splitlines():
            line = line.strip()
            if len(line) < 6 or " " not in line:
                continue  # skip prompts, short tokens, blank lines
            if _looks_like_prose(line):
                continue  # documentation prose, not a command
            if _norm(line) not in ctx:
                flagged.append(line)
    return flagged


def _excerpts_with_commands(answer_text: str, excerpts) -> set:
    """1-based indices of excerpts that contain a command appearing in the
    answer's fenced blocks. Ensures the verbatim view shows the page a command
    actually came from, even when the model omitted its [n] citation."""
    cmds = []
    for block in _FENCE_RE.findall(answer_text):
        for line in block.splitlines():
            line = line.strip()
            if len(line) >= 6 and " " in line:
                cmds.append(_norm(line))
    if not cmds:
        return set()
    found = set()
    for i, ex in enumerate(excerpts, start=1):
        nex = _norm(ex)
        if any(c in nex for c in cmds):
            found.add(i)
    return found


_SECTION_HDR_RE = re.compile(r"(?im)^\s*#{0,6}\s*\**\s*for\s+([^:\n*]+?)\s*:?\**\s*$")


def _section_platform(label: str) -> str:
    """Map a section header label ('For Ciena RLS') to a glossary platform."""
    l = label.lower()
    if "rls" in l:
        return "RLS"
    if "1830" in l or "ols" in l:
        return "1830/OLS"
    if "saos" in l and "6" in l:
        return "SAOS6"
    if "saos" in l and ("10" in l or "1" in l):
        return "SAOS10"
    if "6500" in l:
        return "6500"
    if "7250" in l or "ixr" in l:
        return "7250"
    if "7210" in l or "sas" in l:
        return "7210"
    return ""


def platform_mismatch_commands(answer_text, citations, excerpts):
    """In a multi-platform answer, flag commands placed under one platform's
    section that appear ONLY in another platform's docs. Catches cross-platform
    contamination (e.g. a Nokia 'aprmode auto' under a Ciena RLS section) that
    plain command verification misses because the command IS in the retrieved
    set - just the wrong platform's part of it."""
    headers = list(_SECTION_HDR_RE.finditer(answer_text))
    if len(headers) < 2:
        return []  # single-platform answer: nothing to cross-check
    from utils.ai.glossary import _doc_platform
    ex_plat = [
        (_doc_platform(citations[i].doc_name) if i < len(citations) else "", _norm(ex))
        for i, ex in enumerate(excerpts)
    ]
    flagged = []
    for idx, hdr in enumerate(headers):
        start = hdr.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(answer_text)
        plat = _section_platform(hdr.group(1))
        if not plat:
            continue
        for block in _FENCE_RE.findall(answer_text[start:end]):
            for line in block.splitlines():
                line = line.strip()
                if len(line) < 6 or " " not in line:
                    continue
                nl = _norm(line)
                in_plat = any(p == plat and nl in ex for p, ex in ex_plat)
                in_other = any(p and p != plat and nl in ex for p, ex in ex_plat)
                if not in_plat and in_other:
                    flagged.append((plat, line))
    return flagged


def suppress_fully_mismatched_sections(answer_text, citations, excerpts):
    """If a platform's section contains commands but NONE are valid for that
    platform (all appear only in other platforms' docs), replace its body with
    an honest 'no platform-specific command found' note. Converts a misleading
    fabricated section into a truthful one. Sections with at least one valid
    command are left intact (the [!] flag still lists any mismatched lines)."""
    headers = list(_SECTION_HDR_RE.finditer(answer_text))
    if len(headers) < 2:
        return answer_text
    from utils.ai.glossary import _doc_platform
    ex_plat = [
        (_doc_platform(citations[i].doc_name) if i < len(citations) else "", _norm(ex))
        for i, ex in enumerate(excerpts)
    ]
    out = [answer_text[:headers[0].start()]]
    for idx, hdr in enumerate(headers):
        body_start = hdr.end()
        seg_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(answer_text)
        header_text = answer_text[hdr.start():body_start]
        body = answer_text[body_start:seg_end]
        plat = _section_platform(hdr.group(1))

        cmds = []
        if plat:
            for block in _FENCE_RE.findall(body):
                for line in block.splitlines():
                    line = line.strip()
                    if len(line) >= 6 and " " in line:
                        cmds.append(_norm(line))

        if plat and cmds:
            any_valid = any(
                any(p == plat and c in ex for p, ex in ex_plat) for c in cmds
            )
            any_other = any(
                any(p and p != plat and c in ex for p, ex in ex_plat) for c in cmds
            )
            if not any_valid and any_other:
                out.append(header_text)
                out.append(
                    f"\nThe indexed {plat} documentation does not contain a "
                    f"{plat}-specific command for this. The commands that match "
                    f"this request belong to a different platform - {plat} may "
                    f"use different terminology. Check the {plat} documentation "
                    f"directly before proceeding.\n\n"
                )
                continue
        out.append(header_text)
        out.append(body)
    return "".join(out)


# "clear ALLCHANMISS-OUT-L" / "Clear ALLCHANMISS-OUT[-L]" is NOT a runnable
# command — it's the alarm's own name (a section heading). You never clear an
# alarm by typing its mnemonic; you fix the underlying condition. Real CLI
# clear-commands take LOWERCASE operands ("clear counters", "clear log"), so a
# "clear" followed by an ALL-CAPS hyphenated mnemonic is a fabrication. The
# operand class is strictly uppercase/digit/hyphen/bracket (NOT case-insensitive)
# precisely so "clear counters" is left alone.
_CLEAR_ALARM_RE = re.compile(r"(?m)^\s*[Cc]lear\s+[A-Z0-9][A-Z0-9\-\[\]]{2,}\s*$")
_PSEUDO_FENCE_RE = re.compile(
    r"```[^\n]*\n(?P<body>.*?)\n?```[ \t]*"
    r"(?:\r?\n[ \t]*(?P<note>\(command not in retrieved documentation\)))?",
    re.DOTALL,
)
_UNFOUNDED_NOTE = "(command not in retrieved documentation)"


def strip_pseudo_command_blocks(answer_text: str) -> str:
    """Remove fenced 'command' blocks that are really an alarm name / doc section
    heading ('clear ALLCHANMISS-OUT-L'), or that the model itself annotated as not
    in the docs. A tech must never be shown a copyable code block for something
    that isn't a real, documented command — a mnemonic-as-command or an admitted
    'not in retrieved documentation' one is exactly the kind of subtly-wrong
    output that's dangerous on live gear."""
    def _replace(m):
        body = (m.group("body") or "").strip()
        annotated = m.group("note") is not None
        pseudo = bool(_CLEAR_ALARM_RE.search(body))
        if annotated or pseudo:
            return _UNFOUNDED_NOTE
        return m.group(0)
    return _PSEUDO_FENCE_RE.sub(_replace, answer_text)


def render_answer(answer: "Answer") -> str:
    """Build the full display: the model's prose, then the VERBATIM documentation.
    In extractive mode the command the tech runs is read from this verbatim block,
    never from the prose. Shows the excerpts the answer cited via [n] PLUS any
    excerpt that actually contains one of the answer's commands (so the command's
    real source page is always present).

    If the answer neither cites an excerpt ([n]) nor contains a command — i.e. it
    is a refusal / "I don't find it" — the verbatim block and source list are
    suppressed entirely. Dumping unrelated pages under a not-found answer reads as
    "here's your documentation" when we just said there wasn't any."""
    parts = [answer.text]
    if answer.citations:
        cited = {int(n) for n in _CITED_RE.findall(answer.text)
                 if 1 <= int(n) <= len(answer.citations)}
        cited |= _excerpts_with_commands(answer.text, answer.excerpts)
        cited = sorted(cited)
        if not cited:
            return answer.text  # refusal / ungrounded — don't dump docs
        parts.append("\n\n=== Documentation (verbatim — copy commands from here) ===")
        for i in cited:
            hit = answer.citations[i - 1]
            page = f" p.{hit.page}" if hit.page is not None else ""
            text = answer.excerpts[i - 1] if i - 1 < len(answer.excerpts) else hit.text
            parts.append(f"\n[{i}] {hit.doc_name}{page}\n{_format_verbatim(text)}")
        parts.append("\n\n--- All sources ---")
        for i, hit in enumerate(answer.citations, start=1):
            page = f" p.{hit.page}" if hit.page is not None else ""
            parts.append(f"[{i}] {hit.doc_name}{page}")
    return "\n".join(parts)


# Common English/question words that aren't useful as exact-match keywords.
_STOPWORDS = {
    "configure", "configuration", "interface", "interfaces", "documentation",
    "default", "change", "setting", "settings", "command", "commands", "what",
    "when", "where", "which", "would", "should", "about", "there", "their",
    "follow", "using", "value",
}


# Map a platform mentioned in the question to doc-name substrings that
# identify that platform's documents. The platform lives in the filename, not
# the chunk text, so retrieval scopes on these. Order matters only for clarity.
_PLATFORM_DOCNAME_HINTS = [
    (("rls",), ["RLS", "2051"]),
    (("saos",), ["SAOS", "saos"]),
    (("6500",), ["6500", "1851"]),
    (("1830", "ols", "open line system"), ["1830", "OLS"]),
    (("waveserver", "ws5"), ["Waveserver"]),
    (("7250", "ixr"), ["7250", "IXR"]),
    (("7210", "sas"), ["7210", "SAS"]),
    (("1finity", "onefinity", "t300", "l100"), ["1finity", "1FINITY", "ONEFINITY"]),
    (("dmxtend", "1665"), ["DMXtend", "1665"]),
    (("smartoptics", "dcp"), ["DCP"]),
    (("oscilloquartz", "osa"), ["OSA", "Oscilloquartz"]),
]


def _detect_platform(question: str) -> List[str]:
    """Return doc-name substrings to scope retrieval to, based on a platform
    named in the question. Empty if no platform is clearly mentioned (then
    retrieval spans the whole corpus). If two platforms are named (e.g. a
    comparison), the hints are unioned."""
    q = question.lower()
    hints: List[str] = []
    for triggers, docname_substrs in _PLATFORM_DOCNAME_HINTS:
        if any(t in q for t in triggers):
            hints.extend(docname_substrs)
    # Version refinement: SAOS 6 and SAOS 10 are different CLIs and their doc
    # filenames carry the release (..._SAOS_6.21.5_... / ..._saos_10-11-02_...).
    # If the question names a SAOS major version, scope to that release only so
    # a SAOS-6 question can't retrieve SAOS-10 syntax (or vice versa).
    saos_ver = re.search(r"saos\s*\.?\s*(\d+)", q)
    if saos_ver:
        major = saos_ver.group(1)
        hints = [h for h in hints if h.lower() != "saos"]  # drop the broad hint
        hints.append(f"saos_{major}")
    # De-dupe, preserve order.
    seen = set()
    return [h for h in hints if not (h in seen or seen.add(h))]


def _platform_groups(question: str) -> List[List[str]]:
    """Like _detect_platform, but keeps each platform's doc-name substrings as
    its OWN group. Used to retrieve per-platform so a topic-dense platform can't
    starve another out of the top-k (e.g. RLS OTDR crowding out 1830 OTDR)."""
    q = question.lower()
    groups: List[List[str]] = []
    for triggers, docname_substrs in _PLATFORM_DOCNAME_HINTS:
        if any(t in q for t in triggers):
            group = list(docname_substrs)
            if triggers == ("saos",):  # version-refine the SAOS group
                m = re.search(r"saos\s*\.?\s*(\d+)", q)
                if m:
                    group = [f"saos_{m.group(1)}"]
            groups.append(group)
    return groups


def _group_label(group: List[str]) -> str:
    """Human-readable platform label for a doc-filter group (for section headers)."""
    g = " ".join(group).lower()
    if "rls" in g or "2051" in g:
        return "Ciena RLS"
    if "1830" in g or "ols" in g:
        return "Nokia 1830 OLS"
    if "saos_6" in g:
        return "Ciena SAOS 6"
    if "saos_10" in g:
        return "Ciena SAOS 10"
    if "6500" in g or "1851" in g:
        return "Ciena 6500"
    if "7250" in g or "ixr" in g:
        return "Nokia 7250 IXR"
    if "7210" in g or "sas" in g:
        return "Nokia 7210 SAS"
    return group[0] if group else "the platform"


# Selectable platforms for the Doc Search dropdown: (label, doc-filter | None).
# None = auto-detect / search all. Doc-filter substrings match the filename.
PLATFORM_CHOICES = [
    ("Any (auto-detect)", None),
    ("Ciena RLS", ["RLS", "2051"]),
    ("Nokia 1830 OLS", ["1830", "OLS"]),
    ("Ciena 6500", ["6500", "1851"]),
    ("Ciena SAOS 6", ["saos_6"]),
    ("Ciena SAOS 10", ["saos_10"]),
    ("Nokia 7250 IXR", ["7250", "IXR"]),
    ("Nokia 7210 SAS", ["7210", "SAS"]),
    ("Ciena Waveserver", ["Waveserver"]),
]

# --- Alarm/error interpretation ------------------------------------------
# An alarm mnemonic: uppercase, >=3 chars, optional hyphenated segments
# (APRNODE, LOS-P, ALLCHANMISS-OUT-L, INTTEMPHIGH, EQPTCARD).
_ALARM_MNEMONIC_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)*\b")
# Words that signal the input is raw alarm/fault output rather than a question.
_ALARM_SIGNAL_RE = re.compile(
    r"\b(alarm|alarms|major|minor|critical|warning|NSA|SA|LOS|LOF|OLF|fail|"
    r"failed|failure|degrade|mismatch|shutoff|clamp|clamped|active|raised)\b",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(
    r"^\s*(how|what|why|where|when|which|can|could|do|does|is|are|should|"
    r"list|show me|tell me)\b",
    re.IGNORECASE,
)
# Uppercase tokens that look like mnemonics but are components/units, not alarms.
_NOT_ALARMS = {
    "CLI", "OTS", "RLS", "OLS", "TID", "AID", "LAN", "SFP", "OSC", "CCMD",
    "RLA", "DLE", "DLA", "LRU", "EDFA", "OMS", "FDI", "NBI", "API", "GBE",
    "OTDR", "ORL", "OMDWB", "OMDCL", "EILA", "AMP", "VOA", "IPV4", "IPV6",
    "CTM", "SPLI", "TL1", "SNMP", "MPO", "WSS", "SRA",
}


def extract_alarm_tokens(text: str) -> list:
    """Regex fallback: distinct alarm-like mnemonics in the text (minus known
    non-alarm acronyms). Used when the LLM extraction is unavailable."""
    seen, out = set(), []
    for m in _ALARM_MNEMONIC_RE.findall(text):
        if m in _NOT_ALARMS or m.upper() in seen:
            continue
        seen.add(m.upper())
        out.append(m)
    return out


_MEASUREMENT_HINTS = (
    "power", " rx", " tx", "receive", "transmit", "measurement", "reading",
    "level", "dbm", "optical", "attenuation", "return loss", "span loss",
    "input power", "output power",
)


def _is_measurement_question(question: str) -> bool:
    q = " " + question.lower() + " "
    return any(h in q for h in _MEASUREMENT_HINTS)


def _target_glossary_platforms(question: str) -> set:
    """Map detected platform doc-name hints to glossary platform labels."""
    label_map = {
        "rls": "RLS", "2051": "RLS", "1830": "1830/OLS", "ols": "1830/OLS",
        "saos_6": "SAOS6", "saos_10": "SAOS10", "6500": "6500", "1851": "6500",
        "7250": "7250", "ixr": "7250", "7210": "7210", "sas": "7210",
    }
    return {label_map[h.lower()] for h in _detect_platform(question)
            if h.lower() in label_map}


def _glossary_reference(index, question: str) -> str:
    """Authoritative parameter facts (name -> direction -> restriction -> cite)
    for measurement/'find X' questions, looked up from the structured glossary
    rather than inferred by the model. Empty if not a measurement question or
    no glossary match."""
    if not _is_measurement_question(question):
        return ""
    from utils.ai import glossary
    keywords = _salient_terms(question)
    targets = _target_glossary_platforms(question)
    hits = []
    if targets:
        for plat in targets:
            hits += glossary.lookup(index, keywords, platform=plat, limit=40)
    else:
        hits = glossary.lookup(index, keywords, limit=40)

    seen, lines = set(), []
    for h in hits:
        if re.search(r"(Min|Max|Avg|Std)$", h.term):
            continue  # drop statistical variants; keep the base parameter
        key = (h.platform, h.term.lower(), h.direction)
        if key in seen:
            continue
        seen.add(key)
        comp = f"{h.component} " if h.component else ""
        restr = f" - {h.restriction}" if h.restriction else ""
        lines.append(f"- [{h.platform}] {comp}{h.term} = {h.direction}{restr} "
                     f"({h.doc_name[:44]} p.{h.page})")
    if not lines:
        return ""
    return (
        "PARAMETER REFERENCE (authoritative - extracted from the vendor PM and "
        "command-parameter tables). For measurement questions, use THESE exact "
        "parameter names, directions (RX=receive, TX=transmit), and card "
        "restrictions; state the direction explicitly and cite the page:\n"
        + "\n".join(lines[:24])
    )


def _salient_terms(question: str) -> List[str]:
    """Pull exact-match terms worth forcing through keyword retrieval:
    hyphen/underscore/dot tokens (network-type, OSPFv2), tokens with digits
    (R4.2), short uppercase acronyms (OSPF, RLS, BGP), and longer content
    words. Generic question words are dropped."""
    terms = []
    seen = set()
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*[A-Za-z0-9]", question):
        low = tok.lower()
        if low in seen or low in _STOPWORDS:
            continue
        technical = ("-" in tok or "_" in tok or "." in tok
                     or any(c.isdigit() for c in tok))
        acronym = tok.isupper() and 2 <= len(tok) <= 6
        longword = len(tok) >= 6
        if technical or acronym or longword:
            terms.append(low)
            seen.add(low)
    return terms


def _format_context(hits: List[Hit], excerpts: List[str], start: int = 1) -> str:
    """Number each excerpt [start], [start+1], ... so the model cites by number
    rather than retyping (and mangling) long vendor filenames. ``start`` lets a
    per-platform section use globally-unique numbers that line up 1:1 with the
    combined citations list.

    ``excerpts[i]`` is the (neighbor-expanded) text shown for ``hits[i]`` — it
    may be wider than the single retrieved chunk so multi-step procedures stay
    intact in context.
    """
    blocks = []
    for i, (hit, text) in enumerate(zip(hits, excerpts), start=start):
        loc = f"{hit.doc_name}"
        if hit.page is not None:
            loc += f", p.{hit.page}"
        blocks.append(f"[{i}] (source: {loc})\n{text}")
    return "\n\n---\n\n".join(blocks)


class DocAssistant:
    """Retrieve-then-answer over the doc index."""

    def __init__(
        self,
        index: Optional[DocIndex] = None,
        provider: Optional[AIProvider] = None,
        top_k: Optional[int] = None,
        context_window: Optional[int] = None,
        use_hyde: Optional[bool] = None,
    ) -> None:
        self._index = index or DocIndex()
        self._provider = provider or default_provider()
        self._top_k = top_k or config.AI_TOP_K
        self._context_window = (
            context_window if context_window is not None
            else config.AI_CONTEXT_WINDOW
        )
        self._use_hyde = (
            use_hyde if use_hyde is not None else config.AI_HYDE_ENABLED
        )

    def _embed_query(self, question: str):
        """Return (query_vec, keyword_terms), applying HyDE: embed the question
        plus a throwaway hypothetical answer so the query vector resembles a
        command/config block, and mine the hypothetical for keyword terms.
        Only steers retrieval; never shown."""
        search_text = question
        terms = _salient_terms(question)
        if self._use_hyde:
            try:
                hypothetical = self._provider.chat(HYDE_PROMPT, question)
                if hypothetical:
                    search_text = f"{question}\n{hypothetical}"
                    seen = set(terms)
                    terms = terms + [t for t in _salient_terms(hypothetical)
                                     if not (t in seen or seen.add(t))]
            except Exception:
                search_text, terms = question, _salient_terms(question)
        return self._provider.embed([search_text])[0], terms

    def _search(self, query_vec, terms, doc_filter):
        return self._index.search(
            query_vec, self._top_k, keyword_terms=terms,
            keyword_k=config.AI_KEYWORD_K, doc_filter=doc_filter,
        )

    def retrieve(self, question: str) -> List[Hit]:
        """Return the chunks most relevant to the question (no answer call).

        Multi-platform questions retrieve per platform (merged, deduped) so a
        topic-dense platform can't starve another out of the top-k.
        """
        query_vec, terms = self._embed_query(question)
        groups = _platform_groups(question)
        if len(groups) > 1:
            hits, seen = [], set()
            for group in groups:
                for h in self._search(query_vec, terms, group):
                    key = (h.doc_name, h.chunk_index)
                    if key not in seen:
                        seen.add(key)
                        hits.append(h)
            return hits
        return self._search(query_vec, terms, groups[0] if groups else [])

    def _expand(self, hit: Hit) -> str:
        """Widen a hit to include its neighboring chunks so a procedure split
        across chunk boundaries stays whole. Falls back to the hit's own text
        if neighbors can't be fetched (e.g. a test double without get_window)."""
        if self._context_window <= 0 or hit.chunk_index < 0:
            return hit.text
        getter = getattr(self._index, "get_window", None)
        if getter is None:
            return hit.text
        window = getter(hit.doc_name, hit.chunk_index, self._context_window)
        if not window:
            return hit.text
        return "\n".join(text for _idx, _page, text in window)

    def _generate_section(self, question, hits, excerpts, extra_context,
                          platform_label, start_num):
        """Run one answer-generation call over a SINGLE platform's excerpts.
        Because the model only sees this platform's docs (numbered from
        ``start_num``), it cannot pull another platform's commands or prose in —
        cross-platform contamination is structurally impossible, not just
        flagged."""
        context = _format_context(hits, excerpts, start=start_num)
        user_parts = []
        if extra_context.strip():
            user_parts.append(f"Current ATLAS context:\n{extra_context.strip()}\n")
        reference = _glossary_reference(self._index, question)
        if reference:
            user_parts.append(reference + "\n")
        if platform_label:
            user_parts.append(
                f"Answer ONLY for the {platform_label} platform, using ONLY the "
                f"excerpts below (all {platform_label} documentation). Do NOT add "
                f"a '## For ...' heading - one is added for you. Do NOT mention, "
                f"compare to, or apologize for any other platform; each platform "
                f"is answered in its own separate section.\n"
            )
        user_parts.append("Documentation excerpts:\n" + context)
        user_parts.append(f"\nQuestion: {question}")
        return self._provider.chat(SYSTEM_PROMPT, "\n".join(user_parts))

    def _apply_flags(self, answer_text, hits, excerpts):
        """Backstop safety passes on the assembled answer: suppress fully-
        mismatched sections, then flag unverified / platform-mismatched
        commands. With per-platform generation these should rarely fire — they
        are a safety net, not the primary defense."""
        answer_text = strip_pseudo_command_blocks(answer_text)
        answer_text = suppress_fully_mismatched_sections(answer_text, hits, excerpts)
        flagged = unverified_commands(answer_text, "\n".join(excerpts))
        if flagged:
            answer_text += (
                "\n\n[i] ADAPTED FROM DOCS - the following commands could not be "
                "matched verbatim to the retrieved text (the model may have "
                "reformatted documentation notation). Confirm them against the "
                "verbatim documentation below before running:\n"
                + "\n".join(f"    {c}" for c in flagged)
            )
        mism = platform_mismatch_commands(answer_text, hits, excerpts)
        if mism:
            answer_text += (
                "\n\n[!] PLATFORM MISMATCH - these commands are under a platform "
                "whose documentation does NOT contain them (they appear only in "
                "another platform's docs). Do NOT run them on the stated "
                "platform - the correct command may differ:\n"
                + "\n".join(f"    [{p}] {c}" for p, c in mism)
            )
        return answer_text

    def ask(self, question: str, extra_context: str = "",
            platform_filter=None) -> Answer:
        """Answer a question, grounded in and cited to retrieved doc chunks.

        For a multi-platform question, each platform's section is generated in a
        SEPARATE call seeing only that platform's excerpts, then concatenated —
        so the RLS section cannot contain Nokia text and vice versa. Single-
        platform questions use one call.

        ``platform_filter`` (doc-name substrings, e.g. from the UI dropdown)
        pins retrieval to one platform, overriding text-based detection.
        ``extra_context`` lets a caller inject ATLAS state (device model,
        operation, recent log lines) — shown to the model as situational
        context, not a citable source.
        """
        query_vec, terms = self._embed_query(question)
        groups = [list(platform_filter)] if platform_filter else _platform_groups(question)

        if len(groups) > 1:
            parts, all_hits, all_excerpts = [], [], []
            for group in groups:
                label = _group_label(group)
                hits = self._search(query_vec, terms, group)
                if not hits:
                    parts.append(f"## For {label}:\nNo {label} documentation was "
                                 f"retrieved for this question.")
                    continue
                excerpts = [self._expand(h) for h in hits]
                prose = self._generate_section(
                    question, hits, excerpts, extra_context, label,
                    start_num=len(all_hits) + 1,
                )
                parts.append(f"## For {label}:\n{prose}")
                all_hits += hits
                all_excerpts += excerpts
            if not all_hits:
                return Answer(text=NO_CONTEXT_ANSWER, citations=[])
            answer_text = self._apply_flags("\n\n".join(parts), all_hits, all_excerpts)
            return Answer(text=answer_text, citations=all_hits, excerpts=all_excerpts)

        # Single platform.
        hits = self._search(query_vec, terms, groups[0] if groups else [])
        if not hits:
            return Answer(text=NO_CONTEXT_ANSWER, citations=[])
        excerpts = [self._expand(h) for h in hits]
        prose = self._generate_section(question, hits, excerpts, extra_context,
                                       "", start_num=1)
        answer_text = self._apply_flags(prose, hits, excerpts)
        return Answer(text=answer_text, citations=hits, excerpts=excerpts)

    # -- alarm / error interpretation -----------------------------------

    def looks_like_alarm_dump(self, text: str) -> bool:
        """Heuristic: does this input read like pasted alarm/error output (to
        interpret) rather than a natural-language question (to answer)?"""
        t = text.strip()
        if not t or t.endswith("?") or _QUESTION_START_RE.match(t):
            return False
        mnemonics = [m for m in _ALARM_MNEMONIC_RE.findall(t) if m not in _NOT_ALARMS]
        has_signal = bool(_ALARM_SIGNAL_RE.search(t))
        return (has_signal and len(mnemonics) >= 1) or len(mnemonics) >= 2

    @staticmethod
    def _parse_alarm_lines(raw: str) -> list:
        """Turn a model's line-per-identifier reply into a token list. Robust to
        a model that:
          * wraps its list in a ``` code block — the fence must NOT become a
            bogus '```' alarm (which also desyncs downstream fence-pairing);
          * adds bullets or '1.' numbering;
          * returns a multi-word alarm name ('Neighbor Mismatch') that must stay
            intact;
          * appends a description after an ALL-CAPS mnemonic ('APRNODE Major
            raised on Line1Out') — then keep just the mnemonic.
        All backticks are stripped so a stray one can never corrupt the '## '
        headings or the fenced-command scan later."""
        alarms = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("```"):
                continue                        # blank or a fence line
            line = line.lstrip("-*•·").replace("`", "").strip()
            line = re.sub(r"^\d+[.)]\s*", "", line).strip()  # "1. " / "1) "
            if not any(ch.isalnum() for ch in line):
                continue                        # pure punctuation
            tokens = line.split()
            if len(tokens) > 1:
                first = tokens[0]
                # ALL-CAPS/hyphen mnemonic followed by a lowercase description ->
                # keep only the mnemonic. Title-case names ('Neighbor Mismatch',
                # 'Power Failure Fault') have no all-caps first token, so they are
                # kept whole.
                if (re.fullmatch(r"[A-Z0-9][A-Z0-9\-\[\]]{2,}", first)
                        and any(c.islower() for c in " ".join(tokens[1:]))):
                    line = first
            alarms.append(line)
        return alarms

    @staticmethod
    def _dedup_alarms(alarms) -> list:
        """Case-insensitive de-dupe, order-preserving, capped so a huge paste
        can't fan out into dozens of per-alarm calls."""
        seen, out = set(), []
        for a in alarms:
            k = a.upper()
            if k and k not in seen:
                seen.add(k)
                out.append(a)
        return out[:8]

    def extract_alarms(self, text: str) -> list:
        """Pull the distinct alarm/error identifiers out of pasted output. Uses
        the model (robust to timestamps/AIDs/severity noise), falling back to a
        regex."""
        prompt = (
            "You extract alarms / error codes from raw network-equipment output. "
            "List each DISTINCT alarm on its own line. Prefer the short "
            "mnemonic/code exactly as printed (e.g. APRNODE, LOS-OUT-C, "
            "ALLCHANMISS-OUT-L). If an alarm is given ONLY by its descriptive "
            "text (e.g. 'Outgoing Loss of signal', 'Neighbor Mismatch'), return "
            "that text VERBATIM - do NOT invent, guess, or normalize it into a "
            "code, and NEVER drop a distinguishing word such as "
            "'Outgoing'/'Incoming' or a band like 'C'/'L' (an outgoing LOS is a "
            "different alarm from an incoming one). Omit timestamps, AIDs, "
            "shelf/slot/port locations, and severities. If there are none, "
            "output nothing."
        )
        alarms = []
        try:
            alarms = self._parse_alarm_lines(self._provider.chat(prompt, text))
        except Exception:
            alarms = []
        if not alarms:
            alarms = extract_alarm_tokens(text)
        return self._dedup_alarms(alarms)

    def extract_alarms_from_image(self, image_bytes: bytes,
                                  image_format: str = "png") -> list:
        """Read alarm/error identifiers off a screenshot via the vision model.
        The image is used ONLY to obtain the identifiers — the interpretation
        itself stays grounded in and cited to the local docs, exactly like the
        pasted-text path. No regex fallback: there is no text to fall back to."""
        fn = getattr(self._provider, "chat_image", None)
        if fn is None:
            raise RuntimeError(
                "The configured AI provider does not support image input.")
        prompt = (
            "This is a screenshot of network-equipment output (an EMS/NMS alarm "
            "list, a CLI session, or an alarm banner). Read it and list each "
            "DISTINCT alarm on its own line. Prefer the short mnemonic/code as "
            "printed (e.g. APRNODE, LOS-OUT-C, ALLCHANMISS-OUT-L). If an alarm is "
            "shown only by its descriptive text (e.g. 'Outgoing Loss of signal', "
            "'Neighbor Mismatch'), return that text VERBATIM - do NOT guess a "
            "code, and NEVER drop a distinguishing word such as "
            "'Outgoing'/'Incoming' or a band 'C'/'L'. Omit timestamps, AIDs, "
            "shelf/slot/port locations, and severities. A name may be WRAPPED "
            "across two lines in a narrow column (e.g. 'section-calibration-in-pr' "
            "+ 'ogress') - reconstruct the full name. Do not wrap your answer in a "
            "code block. If there are none, output nothing."
        )
        raw = fn(prompt, "List the alarm names / error codes in this screenshot.",
                 image_bytes, image_format)
        return self._dedup_alarms(self._parse_alarm_lines(raw))

    def _interpret_alarms(self, alarms, extra_context, doc_filter) -> Answer:
        """Shared per-alarm interpretation: for each identified alarm, retrieve
        and generate a cause/impact/clearing section independently, gated on the
        docs actually mentioning that alarm. Used by both the pasted-text and
        screenshot paths so they share every safety net."""
        label = _group_label(doc_filter) if doc_filter else ""
        parts, all_hits, all_excerpts = [], [], []
        for alarm in alarms:
            q = (f"{alarm} alarm: what it means (probable cause and impact) and "
                 f"the procedure to clear it")
            qvec, terms = self._embed_query(q)
            # Bias the keyword pass toward the 'Clear <alarm>' trouble-clearing
            # section, which the description sections (2.x) otherwise crowd out
            # of the vector top-k. Include a band-suffix-stripped base form
            # because procedures fold C/L band into one bracketed title
            # ("Clear ALLCHANMISS-OUT[-L]" covers ...-OUT and ...-OUT-L).
            base = re.sub(r"-[LC]$", "", alarm)
            proc_terms = [alarm, base, f"Clear {alarm}", f"Clear {base}"]
            seen_t = {t.lower() for t in terms}
            terms = terms + [t for t in proc_terms
                             if t and t.lower() not in seen_t]
            hits = self._search(qvec, terms, doc_filter)
            excerpts = [self._expand(h) for h in hits]
            # Alarm-identity gate: the retrieved docs must actually MENTION this
            # alarm, or we'd mislabel a different alarm's procedure under it
            # (e.g. a Nokia alarm queried under an RLS scope pulling the nearest
            # RLS alarm). Compare with non-alphanumerics stripped so
            # 'ALLCHANMISS-OUT-L' matches 'ALLCHANMISS-OUT[-L]'.
            alarm_key = re.sub(r"[^a-z0-9]", "", alarm.lower())
            present = len(alarm_key) >= 3 and any(
                alarm_key in re.sub(r"[^a-z0-9]", "", ex.lower()) for ex in excerpts)
            if not present:
                where = f" for {label}" if label else ""
                parts.append(
                    f"## {alarm}\nNot found in the indexed documentation{where}. "
                    f"The retrieved pages don't describe this alarm — it may use a "
                    f"different name on this platform, or belong to another platform.")
                continue
            prose = self._generate_section(
                q, hits, excerpts, extra_context, label,
                start_num=len(all_hits) + 1)
            parts.append(f"## {alarm}\n{prose}")
            all_hits += hits
            all_excerpts += excerpts

        intro = "Interpreting: " + ", ".join(alarms) + "\n\n"
        if not all_hits:
            return Answer(text=intro + "\n\n".join(parts), citations=[])
        answer_text = self._apply_flags("\n\n".join(parts), all_hits, all_excerpts)
        return Answer(text=intro + answer_text, citations=all_hits,
                      excerpts=all_excerpts)

    def interpret(self, text: str, extra_context: str = "",
                  platform_filter=None) -> Answer:
        """Interpret pasted alarm/error output: identify each alarm and produce a
        per-alarm section (probable cause + impact + clearing procedure), each
        retrieved and generated independently. Falls back to ``ask`` if no alarm
        identifiers are found."""
        alarms = self.extract_alarms(text)
        if not alarms:
            return self.ask(text, extra_context=extra_context,
                            platform_filter=platform_filter)
        doc_filter = list(platform_filter) if platform_filter else []
        return self._interpret_alarms(alarms, extra_context, doc_filter)

    def interpret_image(self, image_bytes: bytes, extra_context: str = "",
                        platform_filter=None, image_format: str = "png") -> Answer:
        """Interpret a pasted screenshot: read the alarms off it with the vision
        model, then run the same doc-grounded per-alarm interpretation as
        ``interpret``. If nothing readable is found, say so rather than guess."""
        alarms = self.extract_alarms_from_image(image_bytes, image_format)
        if not alarms:
            return Answer(
                text="No alarms or error codes could be read from the "
                     "screenshot. Try a clearer or tighter crop of the alarm "
                     "list, or paste the text instead.",
                citations=[])
        doc_filter = list(platform_filter) if platform_filter else []
        return self._interpret_alarms(alarms, extra_context, doc_filter)

    def answer(self, text: str, extra_context: str = "",
               platform_filter=None) -> Answer:
        """Single entry point for the UI: interpret pasted alarms/errors, or
        answer a natural-language question — auto-routed by looks_like_alarm_dump."""
        if self.looks_like_alarm_dump(text):
            return self.interpret(text, extra_context=extra_context,
                                  platform_filter=platform_filter)
        return self.ask(text, extra_context=extra_context,
                        platform_filter=platform_filter)

    def close(self) -> None:
        self._index.close()
