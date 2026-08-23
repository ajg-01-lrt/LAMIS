# ATLAS AI Documentation Assistant

Cited question-answering over Lightriver's vendor documentation (RAG). A field
tech asks a natural-language question; the assistant retrieves relevant passages
from the indexed manuals and answers **strictly from them**, with citations. It
is a **documentation finder, not a config generator** (see "Extractive commands"
below).

Status: prototype on branch `LAMIS_2.0`. Gated behind `config.AI_ASSISTANT_ENABLED`.
**Not cleared to ship to field laptops** — see "Ship-blockers".

---

## What it is / isn't

- **Is:** a way to ask "what does this alarm mean," "what's the default X,"
  "where is the procedure for Y," and get an answer grounded in the actual
  vendor docs, with page citations and the verbatim source text.
- **Isn't:** a system that writes commands for you. The model never emits CLI
  syntax of its own; it points to the verbatim doc text, which the tech copies.

## Architecture

```
utils/ai/
  provider.py    # LLM seam (OpenAI; key from env, base_url overridable for a proxy)
  chunking.py    # PDF -> page-tagged, overlapping word-window chunks
  doc_index.py   # SQLite vector store + cosine search + neighbor/keyword/platform retrieval
  ingest.py      # CLI: PDF/folder -> chunks -> embeddings -> index  (resumable)
  audit.py       # CLI: find/remove failed extractions, duplicates, multi-release docs
  assistant.py   # retrieve -> grounded, cited, extractive answer
gui/ai_assistant_frame.py   # "Doc Search" tab (worker thread owns the assistant)
tests/test_ai_doc_assistant.py  # offline unit tests (FakeProvider, no key)
tests/ai_eval.py                # live quality gate (needs key) - see "Eval workflow"
```

- **Models:** `gpt-4o-mini` (answers) + `text-embedding-3-small` (embeddings).
- **Index location:** `%APPDATA%\ATLAS\doc_index.db` (writable on a Program Files
  install; same convention as the parts DB). Currently ~130 docs / ~105k chunks.
- **Threading (GUI):** a single long-lived worker thread owns the `DocAssistant`
  (and its SQLite connection — SQLite objects can't cross threads). It builds
  once (the embedding matrix loads a single time, then is cached) and serves
  questions off a queue; UI updates marshal back via `controller.root.after`.

## Retrieval pipeline

Each layer was added to fix a specific, observed failure:

1. **Vector top-k** — cosine similarity over normalized embeddings (brute-force
   numpy; the matrix is cached in memory after first load).
2. **Neighbor-window expansion** (`AI_CONTEXT_WINDOW`) — each hit is widened by
   ±N adjacent chunks so a procedure split across a chunk boundary stays whole.
3. **Hybrid keyword pass** (`AI_KEYWORD_K`) — forces in chunks containing exact
   salient terms from the question (hyphenated tokens, acronyms, version/part
   strings) that pure vector ranking can miss (e.g. `network-type`).
4. **Platform scoping** — the platform lives in the *filename*, not the chunk
   text, so an "RLS" question is restricted to RLS docs. This is what stops an
   RLS answer from retrieving (and quoting) SAOS syntax. Falls back to the full
   corpus if nothing matches.

### Multi-platform questions ("X on RLS and OLS")

Handled in four layers, primary defense first:

1. **Per-platform retrieval** — each platform gets its own top-k (a topic-dense
   platform can't starve another out of the results).
2. **Per-platform generation** (`ask`) — each platform's section is produced in
   a SEPARATE answer call seeing ONLY that platform's excerpts (numbered with a
   global offset so citations stay aligned). The RLS section literally cannot
   contain Nokia text and vice versa — cross-platform contamination (command OR
   prose) is structurally impossible, not merely flagged.
3. **Platform-mismatch flag** (`platform_mismatch_commands`) — backstop that
   flags a command under one platform's section that exists only in another's.
4. **Section suppression** (`suppress_fully_mismatched_sections`) — backstop
   that replaces a section built entirely from another platform's commands with
   an honest "not found for this platform" note.

Layers 3-4 became backstops once layer 2 was added; they catch anything the
generation step might still let through.
5. **HyDE** (`AI_HYDE_ENABLED`) — before retrieval, draft a throwaway
   hypothetical answer, embed it with the question, and also mine it for keyword
   terms. A config block is semantically unlike a prose question; the
   hypothetical makes the query look like a command block. Never shown to anyone.

### Alarm / error interpreter (`answer` / `interpret`)

The Doc Search tab has one input that auto-routes: a natural-language question
goes to `ask`; **pasted alarm/error output** goes to `interpret`
(`looks_like_alarm_dump` decides, on alarm mnemonics + severity signals vs.
question phrasing). `interpret`:

1. **Extracts** the distinct alarm/error identifiers with a cheap LLM pass
   (robust to timestamps/AIDs/severity noise), falling back to a regex.
2. **Per-alarm generation** — each alarm gets its own retrieval + grounded
   section (probable cause + impact + clearing procedure), like per-platform
   generation. Capped at 8 alarms per paste.
3. Scoped by a **platform dropdown** (`PLATFORM_CHOICES`) — alarm mnemonics
   aren't unique across platforms, so the tech pins the platform; `ask` and
   `interpret` both take a `platform_filter` that overrides text detection.

Reuses the whole stack (retrieval, procedure-passthrough, verbatim,
verification). Same safety guarantees apply per alarm section.

### Measurement/parameter glossary (`utils/ai/glossary.py`)

"Find/display X" questions (e.g. "OSC RX and TX power on RLS") were answered
badly by letting the model assemble commands from PM-parameter tables — it
picked the wrong component or dropped a card restriction. The glossary fixes
this with a **structured lookup**: `build_glossary()` parses the PM tables and
command-parameter sections (pure text, no API) into a `glossary` table —
`(platform, component, parameter, direction, restriction, doc, page)`. Direction
is inferred from the parameter name itself (Input/In/rx → RX; Output/Out/tx →
TX), which is reliable for these vendor params.

On a measurement question, `_glossary_reference()` injects an authoritative
**PARAMETER REFERENCE** block (e.g. *"[RLS] opticalPowerInputOSC = RX (…p.102)"*,
*"[1830/OLS] txcalcpower = TX - only applicable to OMDWB card"*), and the prompt
treats it as the authority for parameter names/directions/restrictions. Rebuilt
automatically at the end of `ingest`. This is the right engine for reference
lookups — fact, not guess.

## Commands: step-by-step, verbatim-copied, verified (key decision)

**Command philosophy (decided):** **procedure-passthrough first, verbatim-faithful
fallback.** If a retrieved excerpt contains a numbered vendor procedure
("Procedure N …" + "Step Action 1 … 2 …"), the assistant reproduces *that*
procedure verbatim — title, numbered steps, commands, and the doc's
`where <param> is …` explanations — without rephrasing or simplifying, because
the doc's own procedure is authoritative. Only when no matching numbered
procedure exists does it build its own step-by-step from the individual commands
in the excerpts (still copied exactly, with prose saying which argument to fill).
Rationale: vendor manuals write commands in a meta-notation (`{[ip <IpAddress>], …}`)
that isn't directly runnable, so "clean-runnable" and "verbatim-verified"
are in tension; passing through the doc's own procedure sidesteps that whenever
the doc provides one.

Goal: a **brand-new tech can follow the output as a procedure.** So the answer
is a numbered step-by-step, and each step's command appears inline in a fenced
block. To keep that safe, two hard constraints:

1. **Verbatim only.** The prompt forbids writing, paraphrasing, reordering, or
   merging command syntax — commands must be copied character-for-character from
   an excerpt (only example values become `<placeholders>`).
2. **Automated verification net.** After generation, `unverified_commands()`
   checks every fenced command line against the retrieved context
   (whitespace/placeholder-normalized). Any command not found verbatim is
   appended to the answer under a loud `[i] ADAPTED` warning — so fabrication
   or cross-version drift is caught even though commands are now inline.
3. **Platform-aware check.** `platform_mismatch_commands()` flags a command
   placed under one platform's section that appears ONLY in another platform's
   docs (`[!] PLATFORM MISMATCH`). This catches cross-platform contamination in
   multi-platform answers — e.g. a Nokia `aprmode auto` under a Ciena RLS
   section — which plain verification misses (the command IS in the retrieved
   set, just the wrong platform's part).

`render_answer` also appends the verbatim source excerpts (boilerplate stripped)
as a backup for the tech to verify against.

History / why: this evolved from a stricter "model never writes commands, only
points to excerpts" stance. That was safe but unreadable (a wall of verbatim
text) — field feedback was that techs need a real step-by-step. The
verbatim-copy + verification approach keeps the anti-fabrication guarantee while
producing a usable procedure. The failures that drove this: an incomplete OSPF
config, a fabricated SAOS-as-RLS command, a real command extended with an
invented clause — all now caught by verification and/or platform/version scoping.

The prompt also enforces: platform **and version** isolation, citation
discipline, honoring "automatically set / default" language, prerequisites and
CAUTION notes, and refusing when the docs don't cover the question.

## Index management

```powershell
# Ingest a doc or folder (resumable; re-running skips already-indexed docs)
python -m utils.ai.ingest "C:\path\to\manual.pdf"
python -m utils.ai.ingest "C:\path\to\docs_folder"
python -m utils.ai.ingest --list

# Audit / clean the index (report-only by default)
python -m utils.ai.audit
python -m utils.ai.audit --remove-empty        # drop failed extractions
python -m utils.ai.audit --remove-duplicates    # drop duplicate-filename copies
python -m utils.ai.audit --remove "<exact doc name>"
```

Hygiene matters: failed extractions (scanned/image PDFs -> ~0 chunks), duplicate
files, and **mixed releases** (same doc in R16.9 + R17.0) all degrade retrieval
or risk citing the wrong release's syntax. The current index was cleaned of all
three.

## Eval workflow (quality gate)

`tests/ai_eval.py` runs known-answer questions (seeded from field-tech feedback)
against the **live** index and checks that retrieval surfaces the authoritative
content, the answer stays on-platform, and refusals happen when they should.

```powershell
python -m tests.ai_eval           # all cases
python -m tests.ai_eval ospf      # cases whose id contains "ospf"
```

Each case can assert: `expect_doc_substr`, `expect_text_in_context`,
`expect_in_answer`, `forbid_in_answer` (regex), `expect_refusal`. Cases marked
`known_limitation` are tracked as gaps (xfail-style) — they don't fail the gate,
but if one starts passing the harness flags it to promote. **Add a case here for
every new piece of doc/coworker feedback** — this is where that knowledge lives.

Workflow: change one retrieval lever -> re-run -> confirm green without
regressing the others.

## Known limitation

Open-ended "how do I configure X" questions where the doc separates the
*concept* (prose, easy to retrieve) from the *command block* (dense config,
semantically dissimilar) can land on the concept page and miss the exact command
(tracked case: `ospf-create-rls`). Tried neighbor expansion, hybrid keyword,
platform scoping, and HyDE; not closed. **Safe** because extractive mode means
the worst case is an honest-but-incomplete pointer, never a fabricated command.
Future fix: finer-grained indexing of command/procedure pages, or a
procedure-aware retriever.

## Configuration (`config.py`)

| Setting | Purpose |
|---------|---------|
| `AI_ASSISTANT_ENABLED` | master switch; hides the tab + skips optional deps when off |
| `AI_BASE_URL` | API endpoint override (None = OpenAI; set to a proxy for rollout) |
| `AI_CHAT_MODEL` / `AI_EMBED_MODEL` | `gpt-4o-mini` / `text-embedding-3-small` |
| `AI_CHUNK_WORDS` / `AI_CHUNK_OVERLAP_WORDS` | chunking (re-ingest to apply) |
| `AI_TOP_K` / `AI_KEYWORD_K` / `AI_CONTEXT_WINDOW` | retrieval depth / keyword slots / neighbor window |
| `AI_HYDE_ENABLED` | HyDE retrieval steering (one extra cheap call/query) |

Credential: `OPENAI_API_KEY` is read from the **environment**, never stored in
the repo or the binary.

## Ship-blockers (resolve before any field-laptop build)

1. **Key must move server-side.** A personal/embedded key in a distributed app
   will be extracted. The provider seam (`AI_BASE_URL`) is built so the laptops
   point at a proxy that holds the real key; field laptops carry no secret.
2. **Vendor-doc NDA.** Confirm Nokia/Ciena manual text may be sent to a
   third-party API. The API doesn't train on the data, but the NDA governs.
3. **`AI_ASSISTANT_ENABLED = True` is committed for development.** Decide the
   shipped default deliberately.
4. **Index distribution.** Decide whether the doc index is bundled (seed-from-
   bundle, like the parts DB) or built on first run.
