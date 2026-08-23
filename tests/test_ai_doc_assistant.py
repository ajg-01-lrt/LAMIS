"""Offline tests for the AI doc assistant (RAG Phase 1).

No network/API key required: a FakeProvider supplies deterministic embeddings
so we exercise the real vector store, cosine retrieval, citation wiring, and
the all-important "not in the docs" guardrail.
"""

import logging

import numpy as np
import pytest

import config
from utils.ai.chunking import Chunk, _window
from utils.ai.doc_index import DocIndex
from utils.ai.assistant import (
    DocAssistant, NO_CONTEXT_ANSWER, _salient_terms, _detect_platform,
    _platform_groups, unverified_commands, _glossary_reference,
    _is_measurement_question, platform_mismatch_commands,
    suppress_fully_mismatched_sections, Answer, render_answer,
    strip_pseudo_command_blocks,
)
from utils.ai.doc_index import Hit
from utils.ai.glossary import build_glossary, lookup as glossary_lookup


class FakeProvider:
    """Deterministic stand-in for OpenAIProvider.

    Embeds text by hashing words into a fixed-dim bag-of-words vector, so
    lexically similar strings land near each other — enough to verify that
    retrieval ranks the right chunk first. chat() just echoes what it was given
    so tests can assert on the prompt/citations rather than model prose.
    """

    def __init__(self, dim=config.AI_EMBED_DIM):
        self.dim = dim
        self.last_user = None
        self.chat_calls = 0
        self.users = []

    def embed(self, texts):
        vecs = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for word in t.lower().split():
                v[hash(word) % self.dim] += 1.0
            vecs.append(v.tolist())
        return vecs

    def chat(self, system, user):
        self.chat_calls += 1
        self.last_user = user
        self.users.append(user)
        return "ANSWERED"


@pytest.fixture
def index(tmp_path):
    idx = DocIndex(db_path=tmp_path / "doc_index.db")
    yield idx
    idx.close()


def _seed(index, provider):
    chunks = [
        Chunk(page=4, chunk_index=0, text="G42 firmware upgrade procedure step one connect"),
        Chunk(page=9, chunk_index=1, text="1830 PSS optical amplifier card replacement"),
        Chunk(page=2, chunk_index=2, text="Waveserver 5 commissioning fiber cleaning"),
    ]
    vecs = provider.embed([c.text for c in chunks])
    index.add_chunks("Nokia_manual.pdf", chunks, vecs, config.AI_EMBED_MODEL)


def test_window_overlap_covers_boundary():
    words = [f"w{i}" for i in range(10)]
    windows = list(_window(words, size=4, overlap=2))
    # step = 2 -> windows start at 0,2,4,6 (8 would be the tail inside w6's window)
    assert windows[0] == "w0 w1 w2 w3"
    assert windows[1] == "w2 w3 w4 w5"  # overlap preserves the boundary words
    assert windows[-1].endswith("w9")


def test_add_and_list_docs(index):
    _seed(index, FakeProvider())
    assert index.list_docs() == ["Nokia_manual.pdf"]


def test_search_ranks_relevant_chunk_first(index):
    provider = FakeProvider()
    _seed(index, provider)
    q = provider.embed(["G42 firmware upgrade"])[0]
    hits = index.search(q, top_k=3)
    assert hits, "expected retrieval hits"
    assert hits[0].page == 4  # the G42 chunk, not the optical/Waveserver ones
    assert "G42" in hits[0].text


def test_reingest_replaces_chunks(index):
    provider = FakeProvider()
    _seed(index, provider)
    removed = index.remove_doc("Nokia_manual.pdf")
    assert removed == 3
    assert index.list_docs() == []


def test_ask_includes_citations_and_context(index):
    provider = FakeProvider()
    _seed(index, provider)
    assistant = DocAssistant(index=index, provider=provider, top_k=2, use_hyde=False)
    ans = assistant.ask("How do I upgrade G42 firmware?", extra_context="device=G42")
    assert ans.text == "ANSWERED"
    assert ans.citations, "answer should carry citations"
    # ATLAS context and the doc excerpts both reach the model.
    assert "device=G42" in provider.last_user
    assert "Documentation excerpts" in provider.last_user


def test_neighbor_expansion_pulls_prerequisite_step(index):
    """Regression for the OSPF 'missing arg' miss: a multi-command procedure
    spans chunks, so the chunk that matched the query ('interfaces interface')
    must be widened to include the adjacent prerequisite chunk (the protocol
    creation step), or the answer is a confidently-incomplete config."""
    provider = FakeProvider()
    # chunk_index 2 holds the creation step; 3 holds the interface step.
    chunks = [
        Chunk(page=10, chunk_index=0, text="OSPF overview and concepts"),
        Chunk(page=11, chunk_index=1, text="prerequisites before configuring OSPF"),
        Chunk(page=12, chunk_index=2, text="set protocols protocol OSPF,3 config identifier OSPF name 3 enabled true"),
        Chunk(page=12, chunk_index=3, text="set protocols protocol OSPF,3 ospfv2 areas area interfaces interface loopback.0"),
        Chunk(page=13, chunk_index=4, text="OSPF timers and dead-interval settings"),
    ]
    vecs = provider.embed([c.text for c in chunks])
    index.add_chunks("RLS_OAM.pdf", chunks, vecs, config.AI_EMBED_MODEL)

    # get_window around the interface chunk (index 3) must include the
    # creation step at index 2.
    window = index.get_window("RLS_OAM.pdf", center_index=3, window=2)
    texts = " ".join(t for _i, _p, t in window)
    assert "config identifier OSPF name" in texts

    # And via the assistant's expansion path.
    assistant = DocAssistant(index=index, provider=provider, top_k=1, context_window=2)
    interface_hit = index.search(provider.embed(["interfaces interface loopback.0"])[0], top_k=1)[0]
    expanded = assistant._expand(interface_hit)
    assert "config identifier OSPF name" in expanded


def test_context_window_zero_disables_expansion(index):
    provider = FakeProvider()
    _seed(index, provider)
    assistant = DocAssistant(index=index, provider=provider, top_k=1, context_window=0)
    hit = index.search(provider.embed(["G42 firmware upgrade"])[0], top_k=1)[0]
    # With expansion off, the excerpt is exactly the hit's own chunk text.
    assert assistant._expand(hit) == hit.text


def test_salient_terms_extraction():
    terms = _salient_terms("How do I configure the OSPFv2 network-type on an interface?")
    assert "network-type" in terms   # hyphenated technical token
    assert "ospfv2" in terms          # mixed acronym+digit
    assert "configure" not in terms   # generic stopword
    assert "interface" not in terms   # generic stopword


def test_keyword_pass_surfaces_exact_term(index):
    """Regression for the RLS network-type miss: the chunk holding the exact
    term must be forced in even when pure vector ranking drops it."""
    provider = FakeProvider()
    chunks = [
        Chunk(page=1, chunk_index=0, text="ospf ospf ospf show neighbor status detail"),
        Chunk(page=2, chunk_index=1, text="the network-type parameter is broadcast or point to point"),
    ]
    vecs = provider.embed([c.text for c in chunks])
    index.add_chunks("doc.pdf", chunks, vecs, config.AI_EMBED_MODEL)
    qvec = provider.embed(["show ospf network-type"])[0]

    plain = index.search(qvec, top_k=1)                  # vector only
    assert [h.page for h in plain] == [1]                # network-type chunk missed
    hybrid = index.search(qvec, top_k=1, keyword_terms=["network-type"], keyword_k=2)
    assert 2 in [h.page for h in hybrid]                 # now forced in


def test_render_answer_shows_verbatim_cited_excerpt():
    """Extractive mode: the verbatim doc text for a cited excerpt must appear,
    so the tech copies the real command, not the model's prose."""
    hits = [Hit("RLS_CLI.pdf", 128, "short", 0.9, 5),
            Hit("other.pdf", 10, "x", 0.5, 1)]
    excerpts = ["set ...interfaces interface <id string>  REAL_VERBATIM_CMD", "unrelated"]
    ans = Answer(text="Provision the interface; see the command in [1].",
                 citations=hits, excerpts=excerpts)
    out = render_answer(ans)
    assert "REAL_VERBATIM_CMD" in out                 # actual doc text shown
    assert "Documentation (verbatim" in out
    assert "[1] RLS_CLI.pdf p.128" in out             # cited source labelled
    assert "--- All sources ---" in out               # full list still present


def test_suppress_fully_mismatched_section():
    """RLS section whose only command is a Nokia command (present only in OLS
    docs) gets its body replaced with an honest note; the valid OLS section is
    left intact."""
    hits = [
        Hit("323-2051-318_(RLS_R4.2).pdf", 100, "x", 0.9, 0),
        Hit("3KC91844_1830_OLS_Maint.pdf", 285, "y", 0.5, 0),
    ]
    excerpts = [
        "RLS Automatic Power Reduction overview, no aprmode command",
        "Clear APRFORCED CLI config interface <shelf>/<slot>/<port> aprmode auto",
    ]
    answer = (
        "### For Ciena RLS:\n```\nconfig interface <shelf>/<slot>/<port> aprmode auto\n```\n"
        "### For Nokia OLS:\n```\nconfig interface <shelf>/<slot>/<port> aprmode auto\n```\n"
    )
    out = suppress_fully_mismatched_sections(answer, hits, excerpts)
    # RLS section body replaced with the honest note...
    assert "does not contain a RLS-specific command" in out
    # ...and the bogus RLS command line is gone from the RLS section.
    rls_section = out.split("### For Nokia OLS")[0]
    assert "aprmode auto" not in rls_section
    # OLS section keeps its (valid) command.
    assert "aprmode auto" in out.split("### For Nokia OLS")[1]


def test_suppress_leaves_valid_sections_intact():
    hits = [Hit("323-2051-318_(RLS_R4.2).pdf", 100, "x", 0.9, 0)]
    excerpts = ["start-otdr-trace name <slot> is the RLS command"]
    answer = "### For Ciena RLS:\n```\nstart-otdr-trace name <slot>\n```\n"
    out = suppress_fully_mismatched_sections(answer, hits, excerpts)
    assert "start-otdr-trace" in out  # valid command kept
    assert "does not contain" not in out


def test_platform_mismatch_flags_cross_platform_command():
    """Regression for APR-on-RLS: a Nokia OLS command (aprmode auto) placed
    under a Ciena RLS section, present only in OLS docs, must be flagged."""
    hits = [
        Hit("323-2051-318_(RLS_R4.2).pdf", 100, "x", 0.9, 0),       # RLS excerpt
        Hit("3KC91844_1830_OLS_Maint.pdf", 285, "y", 0.5, 0),       # OLS excerpt
    ]
    excerpts = [
        "RLS Automatic Power Reduction overview, no aprmode command here",
        "Clear APRFORCED CLI config interface <shelf>/<slot>/<port> aprmode auto",
    ]
    answer = (
        "### For Ciena RLS:\n```\nconfig interface <shelf>/<slot>/<port> aprmode auto\n```\n"
        "### For Nokia OLS:\n```\nconfig interface <shelf>/<slot>/<port> aprmode auto\n```\n"
    )
    flagged = platform_mismatch_commands(answer, hits, excerpts)
    # The RLS section's command exists only in the OLS doc -> flagged for RLS.
    assert any(p == "RLS" for p, _ in flagged)
    # The OLS section's identical command IS in an OLS doc -> not flagged for OLS.
    assert not any(p == "1830/OLS" for p, _ in flagged)


def test_render_shows_excerpt_containing_command_even_without_citation():
    """Regression: when the model omits [n] markers, the verbatim block must
    still show the excerpt that actually contains the answer's command - not a
    blind top-3 that lacks it."""
    hits = [Hit("spli.pdf", 220, "x", 0.9, 0), Hit("alarms.pdf", 188, "y", 0.5, 0)]
    excerpts = [
        "unrelated config-neighbours and neighbour-setup text",
        "Steps 1 Verify: show functional-group <PFG-name> | grep -A1 DOWNSTR",
    ]
    ans = Answer(
        text="Run this:\n```\nshow functional-group <PFG-name> | grep -A1 DOWNSTR\n```",
        citations=hits, excerpts=excerpts,
    )
    out = render_answer(ans)
    assert "alarms.pdf p.188" in out          # source of the command is shown
    assert "grep -A1 DOWNSTR" in out


def test_render_suppresses_docs_when_answer_is_ungrounded():
    """A refusal / 'I don't find it' answer cites no [n] and contains no
    command, so the verbatim block and source list must be suppressed — dumping
    unrelated pages under a not-found answer reads as 'here's your docs' when we
    just said there weren't any."""
    hits = [Hit("a.pdf", 1, "t", 0.9, 0)]
    ans = Answer(text="I don't find it in the indexed documentation.",
                 citations=hits, excerpts=["VERBATIM_A"])
    out = render_answer(ans)
    assert out == "I don't find it in the indexed documentation."
    assert "VERBATIM_A" not in out
    assert "=== Documentation" not in out


def test_detect_platform():
    assert "RLS" in _detect_platform("configure OSPF on a Ciena RLS")
    assert "1830" in _detect_platform("BGP on a Nokia 1830 OLS")
    assert "SAOS" in _detect_platform("SAOS interface config")
    assert _detect_platform("what does a high temperature alarm mean") == []


class _ExtractProvider(FakeProvider):
    """FakeProvider that returns a canned alarm list for the extraction prompt
    and 'ANSWERED' for everything else."""
    def chat(self, system, user):
        self.chat_calls += 1
        self.last_user = user
        self.users.append(user)
        if "extract" in system.lower():
            return "APRNODE\nLOS-P\nAPRNODE"  # includes a duplicate
        return "ANSWERED"


class _VisionProvider(_ExtractProvider):
    """Adds a vision seam: chat_image 'reads' a canned alarm list off an image."""
    def __init__(self, dim=config.AI_EMBED_DIM):
        super().__init__(dim)
        self.image_calls = 0
        self.last_image = None

    def chat_image(self, system, user, image_bytes, image_format="png"):
        self.image_calls += 1
        self.last_image = image_bytes
        return "APRNODE\nLOS-P\nAPRNODE"  # duplicate, like a busy alarm banner


class _DescriptiveExtractProvider(FakeProvider):
    """Mimics the FIXED extractor: when the alarm is given only by descriptive
    text, it returns that text VERBATIM (keeping the direction word) instead of
    guessing a mnemonic like the old prompt did (which hallucinated LOS-P from
    'Outgoing Loss of signal')."""
    def chat(self, system, user):
        self.chat_calls += 1
        self.users.append(user)
        if "extract" in system.lower():
            return "Outgoing Loss of signal"
        return "ANSWERED"


def test_looks_like_alarm_dump(index):
    a = DocAssistant(index=index, provider=FakeProvider(), use_hyde=False)
    assert a.looks_like_alarm_dump("APRNODE Major raised on Line1Out shelf 1")
    assert a.looks_like_alarm_dump("LOS-P\nALLCHANMISS-OUT-L")   # 2 mnemonics
    assert not a.looks_like_alarm_dump("How do I clear an alarm on a Ciena RLS?")
    assert not a.looks_like_alarm_dump("what does APRNODE mean?")  # question form


def test_extract_alarms_dedup_and_fallback(index):
    a = DocAssistant(index=index, provider=_ExtractProvider(), use_hyde=False)
    assert a.extract_alarms("raw output") == ["APRNODE", "LOS-P"]

    class _FailChat(FakeProvider):
        def chat(self, system, user):
            raise RuntimeError("chat unavailable")
    b = DocAssistant(index=index, provider=_FailChat(), use_hyde=False)
    got = b.extract_alarms("2026 APRNODE Major; ALLCHANMISS-OUT-L on card X")
    assert "APRNODE" in got and "ALLCHANMISS-OUT-L" in got   # regex fallback


def test_interpret_produces_per_alarm_sections(index):
    provider = _ExtractProvider()
    a_chunk = [Chunk(page=1, chunk_index=0, text="Clear APRNODE procedure show interface topology")]
    index.add_chunks("323-2051-530_(RLS_Alarms).pdf", a_chunk,
                     provider.embed([a_chunk[0].text]), config.AI_EMBED_MODEL)
    b_chunk = [Chunk(page=2, chunk_index=0, text="Clear LOS-P procedure verify fiber")]
    index.add_chunks("323-2051-530b_(RLS_Alarms).pdf", b_chunk,
                     provider.embed([b_chunk[0].text]), config.AI_EMBED_MODEL)

    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    ans = a.interpret("APRNODE and LOS-P both raised", platform_filter=["RLS", "2051"])
    assert "## APRNODE" in ans.text and "## LOS-P" in ans.text
    assert "Interpreting:" in ans.text
    assert ans.citations  # grounded in the alarm docs


def test_interpret_gate_rejects_alarm_absent_from_docs(index):
    """Regression: an alarm queried against a platform that doesn't have it must
    be reported 'not found', NOT written up from the nearest unrelated alarm."""
    provider = _ExtractProvider()  # extracts APRNODE, LOS-P
    # Seed only an UNRELATED RLS alarm (no APRNODE / LOS-P text).
    chunk = [Chunk(page=1, chunk_index=0,
                   text="Procedure 87 OSRP Connection Mismatch osrp not synchronized")]
    index.add_chunks("323-2051-530_(RLS_Alarms).pdf", chunk,
                     provider.embed([chunk[0].text]), config.AI_EMBED_MODEL)
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    ans = a.interpret("APRNODE and LOS-P raised", platform_filter=["RLS", "2051"])
    assert "Not found in the indexed documentation" in ans.text
    assert "OSRP" not in ans.text     # did NOT fabricate a section from the unrelated doc
    assert ans.citations == []        # nothing grounded


def test_answer_routes_question_vs_alarm(index):
    provider = _ExtractProvider()
    _seed(index, provider)  # a G42 doc
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    q = a.answer("How do I upgrade G42 firmware?", platform_filter=None)
    assert "Interpreting:" not in q.text          # routed to Q&A
    al = a.answer("APRNODE Major raised", platform_filter=["RLS", "2051"])
    assert "Interpreting:" in al.text             # routed to interpret


def test_interpret_descriptive_alarm_matches_correct_direction(index):
    """A descriptive-text alarm ('Outgoing Loss of signal') must resolve to the
    OUTGOING alarm, keeping the direction word through extraction and matching
    the outgoing doc rather than being collapsed to a guessed mnemonic."""
    provider = _DescriptiveExtractProvider()   # extracts "Outgoing Loss of signal"
    outgoing = [Chunk(page=209, chunk_index=0, text=(
        "LOS-OUT-C Outgoing Loss of signal C band OTS output channels "
        "monitored power drops below the set threshold MAXDEV"))]
    index.add_chunks("3KC91895_1830_OLS_Maintenance.pdf", outgoing,
                     provider.embed([outgoing[0].text]), config.AI_EMBED_MODEL)
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    ans = a.interpret("1/10/OMD Out OTS Outgoing Loss of signal",
                      platform_filter=["1830", "OLS"])
    assert "Interpreting: Outgoing Loss of signal" in ans.text  # phrase preserved
    assert "## Outgoing Loss of signal" in ans.text
    assert ans.citations                       # grounded in the outgoing alarm doc


def test_interpret_descriptive_alarm_gate_rejects_wrong_direction(index):
    """The dangerous mislabel this guards: your OMD 'Outgoing Loss of signal'
    was written up as the INCOMING sibling (LOS-P) because the old extractor
    guessed a mnemonic. With the direction word preserved, if ONLY the wrong-
    direction alarm is in the docs the gate must report 'not found' — never
    adopt the incoming alarm under an outgoing query."""
    provider = _DescriptiveExtractProvider()   # extracts "Outgoing Loss of signal"
    only_incoming = [Chunk(page=210, chunk_index=0, text=(
        "LOS-P Incoming payload LOS reported when the incoming signal power "
        "level drops below a set threshold at the input port of the amplifier"))]
    index.add_chunks("3KC91895_1830_OLS_Maintenance.pdf", only_incoming,
                     provider.embed([only_incoming[0].text]), config.AI_EMBED_MODEL)
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    ans = a.interpret("1/10/OMD Out OTS Outgoing Loss of signal",
                      platform_filter=["1830", "OLS"])
    assert "Not found in the indexed documentation" in ans.text
    assert "LOS-P" not in ans.text             # did NOT adopt the incoming sibling
    assert "Incoming payload" not in ans.text
    assert ans.citations == []                 # nothing grounded


def test_extract_prompts_preserve_direction_words():
    """Tripwire: both extractors must keep descriptive text verbatim and its
    direction word — a revert to 'just the code' reintroduces the LOS-OUT→LOS-P
    mislabel. Pinned on prompt source so it fails without a live model."""
    import inspect
    for meth in (DocAssistant.extract_alarms, DocAssistant.extract_alarms_from_image):
        src = inspect.getsource(meth)
        self_msg = meth.__name__
        assert "Outgoing" in src and "Incoming" in src, self_msg
        assert "verbatim" in src.lower(), self_msg


def test_platform_groups_keeps_platforms_separate():
    groups = _platform_groups("how do I run OTDR on Ciena RLS and Nokia OLS")
    assert len(groups) >= 2
    assert any("RLS" in g or "2051" in g for g in groups)
    assert any("1830" in g or "OLS" in g for g in groups)


def test_per_platform_generation_isolation(index):
    """Each platform's section is generated in a separate call that sees ONLY
    that platform's docs - so the RLS call can't see Nokia text and vice versa.
    This makes cross-platform contamination structurally impossible."""
    provider = FakeProvider()
    rls = [Chunk(page=1, chunk_index=0, text="OTDR start-otdr-trace rls procedure")]
    index.add_chunks("323-2051-318_(RLS_R4.2_OTDR).pdf", rls,
                     provider.embed([rls[0].text]), config.AI_EMBED_MODEL)
    ols = [Chunk(page=2, chunk_index=0, text="OTDR show otdrscan 1830 ols procedure")]
    index.add_chunks("3KC91935_1830_OLS_CLI.pdf", ols,
                     provider.embed([ols[0].text]), config.AI_EMBED_MODEL)

    a = DocAssistant(index=index, provider=provider, top_k=5, use_hyde=False)
    a.ask("how do I run OTDR on RLS and OLS")

    assert len(provider.users) == 2  # one generation call per platform
    rls_msg = next(u for u in provider.users if "2051" in u)
    ols_msg = next(u for u in provider.users if "3KC" in u)
    # The RLS call never saw OLS docs, and the OLS call never saw RLS docs.
    assert "3KC" not in rls_msg and "1830" not in rls_msg
    assert "2051" not in ols_msg


def test_multiplatform_retrieval_does_not_starve_a_platform(index):
    """Regression for OTDR-on-RLS-and-OLS: a topic-dense platform (many RLS
    chunks) must not crowd the other platform (one OLS chunk) out of retrieval."""
    provider = FakeProvider()
    rls = [Chunk(page=i, chunk_index=i, text=f"OTDR trace start-otdr-trace rls otdr {i}")
           for i in range(6)]
    index.add_chunks("323-2051-318_(RLS_R4.2_Optical_SPLI_OTDR).pdf", rls,
                     provider.embed([c.text for c in rls]), config.AI_EMBED_MODEL)
    ols = [Chunk(page=151, chunk_index=0, text="OTDR optical time domain reflectometer 1830 ols")]
    index.add_chunks("3KC91935_1830_OLS_CLI.pdf", ols,
                     provider.embed([c.text for c in ols]), config.AI_EMBED_MODEL)

    a = DocAssistant(index=index, provider=provider, top_k=5, use_hyde=False)
    docs = {h.doc_name for h in a.retrieve("how do I run OTDR on RLS and OLS")}
    assert any("2051" in d for d in docs)   # RLS represented
    assert any("1830" in d for d in docs)   # OLS NOT starved


def test_provider_key_resolution_falls_back_to_keystore(monkeypatch):
    """Provider resolves the key env-var-first, then the OS credential vault -
    so an installed GUI with no env var still finds a stored key."""
    import os
    from utils.ai import provider as provider_mod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # No env, no arg -> should consult the keystore.
    monkeypatch.setattr(provider_mod, "config", provider_mod.config)
    from utils.ai import keystore
    monkeypatch.setattr(keystore, "get_key", lambda: "sk-from-vault")

    p = provider_mod.OpenAIProvider()
    assert p._api_key is None            # nothing at construction time
    # _ensure_client pulls from the vault (openai import may fail in test env,
    # but key resolution happens before the client is built).
    try:
        p._ensure_client()
    except Exception:
        pass
    assert p._api_key == "sk-from-vault"


def test_chat_passes_seed_and_temperature(monkeypatch):
    """For reproducibility, chat calls must send a fixed seed + temperature 0."""
    from types import SimpleNamespace
    from utils.ai import provider as provider_mod
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="ANSWERED"))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(provider_mod.config, "AI_SEED", 7, raising=False)
    p = provider_mod.OpenAIProvider(api_key="sk-test")
    p._client = fake_client  # bypass real client construction
    p.chat("system", "user")
    assert captured.get("seed") == 7
    assert captured.get("temperature") == 0


def test_chat_images_json_uses_strict_schema_and_preserves_image_order(
    monkeypatch,
):
    """Route-diagram extraction uses one strict, ordered multimodal request."""

    from types import SimpleNamespace
    from utils.ai import provider as provider_mod

    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"route_code":"A-Z","shelves":[]}'
                        )
                    )
                ]
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    monkeypatch.setattr(provider_mod.config, "AI_SEED", 7, raising=False)
    provider = provider_mod.OpenAIProvider(api_key="sk-test")
    provider._client = fake_client
    schema = {
        "type": "object",
        "properties": {
            "route_code": {"type": "string"},
            "shelves": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["route_code", "shelves"],
        "additionalProperties": False,
    }

    result = provider.chat_images_json(
        "system",
        "extract",
        [(b"first", "png"), (b"second", "jpeg")],
        schema,
        "atlas_route_diagram",
    )

    assert result == {"route_code": "A-Z", "shelves": []}
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    content = captured["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "extract"}
    assert content[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert content[1]["image_url"]["detail"] == "high"
    assert content[2]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert content[2]["image_url"]["detail"] == "high"
    assert captured["temperature"] == 0
    assert captured["seed"] == 7


def test_chat_images_json_supports_route_vision_reasoning_profile(
    monkeypatch,
):
    """Dense route extraction can use native detail and a large output budget."""

    from types import SimpleNamespace
    from utils.ai import provider as provider_mod

    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"shelves":[]}')
                    )
                ]
            )

    provider = provider_mod.OpenAIProvider(
        api_key="sk-test",
        chat_model="diagram-vision-model",
        image_detail="auto",
        reasoning_effort="low",
        max_completion_tokens=65_536,
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )

    assert provider.chat_images_json(
        "system",
        "extract",
        [(b"route", "png")],
        {
            "type": "object",
            "properties": {"shelves": {"type": "array"}},
            "required": ["shelves"],
            "additionalProperties": False,
        },
        "route",
    ) == {"shelves": []}

    assert captured["model"] == "diagram-vision-model"
    assert captured["reasoning_effort"] == "low"
    assert captured["max_completion_tokens"] == 65_536
    assert "temperature" not in captured
    assert "seed" not in captured
    content = captured["messages"][1]["content"]
    assert content[1]["image_url"]["detail"] == "auto"


def test_chat_images_json_logs_only_safe_request_and_response_metadata(
    monkeypatch,
    caplog,
):
    """Vision telemetry must never serialize prompts, pixels, or model output."""

    from types import SimpleNamespace
    from utils.ai import provider as provider_mod

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"result":"private-returned-content"}'
                        ),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=123,
                    completion_tokens=45,
                    total_tokens=168,
                ),
            )

    provider = provider_mod.OpenAIProvider(
        api_key="sk-test",
        chat_model="diagram-vision-model",
        image_detail="auto",
        reasoning_effort="low",
    )
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )

    with caplog.at_level(logging.INFO, logger="utils.ai.provider"):
        result = provider.chat_images_json(
            "private-system-prompt",
            "private-user-prompt",
            [(b"private-image-bytes", "png"), (b"more-private-bytes", "jpeg")],
            {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            "private-schema-name",
        )

    assert result == {"result": "private-returned-content"}
    log_text = caplog.text
    assert "model='diagram-vision-model'" in log_text
    assert "images=2" in log_text
    assert "detail='auto'" in log_text
    assert "reasoning_effort='low'" in log_text
    assert "finish_reason='stop'" in log_text
    assert "prompt_tokens=123" in log_text
    assert "completion_tokens=45" in log_text
    assert "total_tokens=168" in log_text
    assert "private-system-prompt" not in log_text
    assert "private-user-prompt" not in log_text
    assert "private-image-bytes" not in log_text
    assert "more-private-bytes" not in log_text
    assert "private-schema-name" not in log_text
    assert "private-returned-content" not in log_text


def test_chat_images_json_logging_tolerates_sparse_fake_response(
    monkeypatch,
    caplog,
):
    """Existing test/proxy responses need not expose finish reason or usage."""

    from types import SimpleNamespace
    from utils.ai import provider as provider_mod

    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"result":"ok"}')
                    )
                ]
            )

    provider = provider_mod.OpenAIProvider(api_key="sk-test")
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )

    with caplog.at_level(logging.INFO, logger="utils.ai.provider"):
        result = provider.chat_images_json(
            "system",
            "user",
            [(b"image", "png")],
            {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            "route",
        )

    assert result == {"result": "ok"}
    assert "finish_reason='unavailable'" in caplog.text
    assert "token_usage=unavailable" in caplog.text


def test_provider_prefers_env_over_keystore(monkeypatch):
    from utils.ai import provider as provider_mod
    from utils.ai import keystore
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    monkeypatch.setattr(keystore, "get_key", lambda: "sk-from-vault")
    p = provider_mod.OpenAIProvider()
    assert p._api_key == "sk-from-env"   # env wins; vault not consulted


def test_detect_platform_saos_version():
    # SAOS 6 must scope to saos_6 docs, NOT the broad/saos_10 form.
    v6 = _detect_platform("change the management IP in SAOS 6")
    assert "saos_6" in v6 and "saos_10" not in v6 and "saos" not in v6
    v10 = _detect_platform("management IP on SAOS 10")
    assert "saos_10" in v10 and "saos_6" not in v10


def test_unverified_commands_flags_fabrication():
    context = "Procedure 40 Steps 1 Delete: no oc-if:interfaces interface mgmtbr0 ipv4 addresses address <ipv4-address>"
    # A verbatim copy (placeholder renamed) should pass; a fabricated one fails.
    answer = (
        "Step 1:\n```\nno oc-if:interfaces interface mgmtbr0 ipv4 addresses address <new-ip>\n```\n"
        "Step 2:\n```\nospf instance 10 interfaces interface intf11 type point-to-point\n```\n"
    )
    flagged = unverified_commands(answer, context)
    assert any("ospf instance 10" in f for f in flagged)        # fabricated -> flagged
    assert not any("oc-if:interfaces" in f for f in flagged)    # verbatim -> not flagged


def test_doc_filter_scopes_retrieval(index):
    """Regression for SAOS-crowds-RLS: an RLS-scoped query must not return SAOS
    chunks even when they're more vector-similar."""
    provider = FakeProvider()
    chunks = [
        Chunk(page=1, chunk_index=0, text="ospf instance interface type point-to-point config"),
        Chunk(page=2, chunk_index=1, text="ospf instance interface type broadcast config detail"),
    ]
    vecs = provider.embed([c.text for c in chunks])
    index.add_chunks("323-1955-331_saos_core.pdf", chunks, vecs, config.AI_EMBED_MODEL)
    rls = [Chunk(page=9, chunk_index=0, text="ospf config identifier OSPF name protocol")]
    index.add_chunks("323-2051-101_(RLS_R4.2_OAM).pdf", rls, provider.embed([rls[0].text]), config.AI_EMBED_MODEL)

    qvec = provider.embed(["ospf instance interface config"])[0]
    # Unscoped: SAOS chunks dominate.
    unscoped = index.search(qvec, top_k=2)
    assert any("saos" in h.doc_name.lower() for h in unscoped)
    # Scoped to RLS: only the RLS doc comes back.
    scoped = index.search(qvec, top_k=3, doc_filter=["RLS", "2051"])
    assert scoped, "filter should not wipe out all results"
    assert all("2051" in h.doc_name or "RLS" in h.doc_name for h in scoped)


def test_doc_filter_falls_back_when_no_match(index):
    provider = FakeProvider()
    _seed(index, provider)  # only Nokia_manual.pdf
    qvec = provider.embed(["G42 firmware upgrade"])[0]
    # Filter matches nothing -> should fall back to full corpus, not empty.
    hits = index.search(qvec, top_k=2, doc_filter=["NONEXISTENT_PLATFORM"])
    assert hits


def test_hyde_adds_a_retrieval_steering_call(index):
    provider = FakeProvider()
    _seed(index, provider)
    DocAssistant(index=index, provider=provider, top_k=1, use_hyde=True).ask("upgrade G42")
    assert provider.chat_calls == 2  # one HyDE hypothetical + one answer

    provider.chat_calls = 0
    DocAssistant(index=index, provider=provider, top_k=1, use_hyde=False).ask("upgrade G42")
    assert provider.chat_calls == 1  # answer only


def _seed_glossary(index):
    provider = FakeProvider()
    rls = [Chunk(page=102, chunk_index=0, text=(
        "RLA 32x1 module PM parameters OSC opticalPowerInputOSC "
        "opticalPowerInputOSCMin Receive opticalPowerOutputOSC "
        "opticalPowerOutputOSCMin Transmit"))]
    ols = [Chunk(page=1109, chunk_index=0, text=(
        "txpower Displays the OSC target output power. Restrictions only "
        "applicable to OMDWB card. Supvy In Power Optical power received by "
        "the OSC receiver."))]
    index.add_chunks("323-2051-520_(RLS_R4.2_Logs_and_PMs).pdf", rls,
                     provider.embed([rls[0].text]), config.AI_EMBED_MODEL)
    index.add_chunks("3KC91935_1830_OLS_CLI.pdf", ols,
                     provider.embed([ols[0].text]), config.AI_EMBED_MODEL)
    build_glossary(index)


def test_glossary_direction_and_restriction(index):
    _seed_glossary(index)
    rls = {h.term: h.direction for h in glossary_lookup(index, ["opticalpower", "osc"], platform="RLS")}
    assert rls.get("opticalPowerInputOSC") == "RX"   # input -> receive
    assert rls.get("opticalPowerOutputOSC") == "TX"  # output -> transmit

    ols = glossary_lookup(index, ["txpower", "supvy"], platform="1830")
    assert any(h.term.lower() == "txpower" and h.direction == "TX" for h in ols)
    assert any(h.direction == "RX" and "supvy" in h.term.lower() for h in ols)
    assert any("omdwb" in (h.restriction or "").lower() for h in ols)


def test_glossary_reference_only_for_measurement_questions(index):
    _seed_glossary(index)
    assert _is_measurement_question("OSC RX and TX power on RLS")
    assert not _is_measurement_question("how do I create an OSPF instance")

    ref = _glossary_reference(index, "OSC RX and TX power on RLS and OLS")
    assert "PARAMETER REFERENCE" in ref
    assert "opticalPowerInputOSC = RX" in ref
    assert "opticalPowerOutputOSC = TX" in ref
    # Non-measurement question -> no reference block.
    assert _glossary_reference(index, "how do I create an OSPF instance") == ""


def test_empty_index_triggers_guardrail(index):
    """With nothing indexed, we must refuse — never fabricate, never call chat."""
    provider = FakeProvider()
    assistant = DocAssistant(index=index, provider=provider, top_k=5, use_hyde=False)
    ans = assistant.ask("How do I set up BGP on a Nokia 1830?")
    assert ans.text == NO_CONTEXT_ANSWER
    assert ans.citations == []
    assert provider.last_user is None  # model was never invoked


def test_strip_pseudo_command_block_from_title():
    """A doc section TITLE wrapped in a code fence ('Clear ALLCHANMISS-OUT[-L]')
    is not a runnable command and must not be shown as one."""
    text = (
        "1. Clear the alarm:\n"
        "```\nClear ALLCHANMISS-OUT[-L]\n```\n"
        "Then check the OCM."
    )
    out = strip_pseudo_command_blocks(text)
    assert "```" not in out                       # fence removed
    assert "(command not in retrieved documentation)" in out
    assert "Then check the OCM." in out           # surrounding prose preserved


def test_strip_pseudo_command_block_lowercase_mnemonic():
    """The dangerous variant: a lowercase 'clear <ALL-CAPS MNEMONIC>' that LOOKS
    like a real CLI command but is just the alarm name. Must be stripped."""
    text = (
        "2. Execute the following command:\n"
        "```\nclear ALLCHANMISS-OUT-L\n```\n"
        "3. Verify."
    )
    out = strip_pseudo_command_blocks(text)
    assert "clear ALLCHANMISS-OUT-L" not in out
    assert "(command not in retrieved documentation)" in out
    assert "3. Verify." in out


def test_extract_alarms_from_image_uses_vision_and_dedups(index):
    provider = _VisionProvider()
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    alarms = a.extract_alarms_from_image(b"\x89PNG fake-bytes")
    assert alarms == ["APRNODE", "LOS-P"]     # deduped, order-preserving
    assert provider.image_calls == 1
    assert provider.last_image == b"\x89PNG fake-bytes"


def test_parse_alarm_lines_strips_fences_and_keeps_multiword():
    """Regression for a real screenshot test: the vision model wrapped its list
    in ``` (which became a bogus '```' alarm and desynced fence-pairing), and
    'Neighbor Mismatch' was truncated to 'Neighbor' by split()[0]."""
    raw = ("```\n"
           "section-calibration-in-progress\n"
           "Neighbor Mismatch\n"
           "APRNODE Major raised on Line1Out\n"
           "```")
    out = DocAssistant._parse_alarm_lines(raw)
    assert "```" not in out                       # fence never becomes an alarm
    assert not any("`" in x for x in out)          # no stray backticks survive
    assert "section-calibration-in-progress" in out
    assert "Neighbor Mismatch" in out              # multi-word name kept intact
    assert "APRNODE" in out                        # mnemonic + description trimmed
    assert not any("raised" in x for x in out)


def test_extract_alarms_from_image_handles_fenced_output(index):
    class _Fenced(_VisionProvider):
        def chat_image(self, system, user, image_bytes, image_format="png"):
            return "```\nNeighbor Mismatch\nclamped\nclamped\n```"
    a = DocAssistant(index=index, provider=_Fenced(), use_hyde=False)
    assert a.extract_alarms_from_image(b"png") == ["Neighbor Mismatch", "clamped"]


def test_unverified_commands_ignores_prose():
    """A fenced block containing documentation prose (from over-fencing or a
    fence-pairing desync) must not be flagged as an unverified command."""
    ans = ("```\n"
           "This alarm is raised when power fails. Check the PIM module.\n"
           "- a disconnected fiber\n"
           "```")
    assert unverified_commands(ans, "unrelated context") == []


def test_extract_alarms_from_image_requires_vision_support(index):
    """A provider without chat_image must fail loudly, not silently guess."""
    a = DocAssistant(index=index, provider=FakeProvider(), use_hyde=False)
    with pytest.raises(RuntimeError):
        a.extract_alarms_from_image(b"bytes")


def test_interpret_image_grounds_and_gates(index):
    """Screenshot path shares the text path's grounding + identity gate: the
    alarm present in the docs gets a section; the absent one is 'not found'."""
    provider = _VisionProvider()  # reads APRNODE, LOS-P off the image
    chunk = [Chunk(page=1, chunk_index=0,
                   text="Clear APRNODE procedure show interface topology alm")]
    index.add_chunks("323-2051-530_(RLS_Alarms).pdf", chunk,
                     provider.embed([chunk[0].text]), config.AI_EMBED_MODEL)
    a = DocAssistant(index=index, provider=provider, use_hyde=False)
    ans = a.interpret_image(b"png", platform_filter=["RLS", "2051"])
    assert "Interpreting:" in ans.text
    assert "## APRNODE" in ans.text
    assert "Not found in the indexed documentation" in ans.text  # LOS-P gated out
    assert ans.citations  # grounded in the APRNODE doc


def test_interpret_image_no_alarms_message(index):
    class _Blank(_VisionProvider):
        def chat_image(self, system, user, image_bytes, image_format="png"):
            return ""  # nothing readable
    a = DocAssistant(index=index, provider=_Blank(), use_hyde=False)
    ans = a.interpret_image(b"png", platform_filter=["RLS"])
    assert "No alarms or error codes could be read" in ans.text
    assert ans.citations == []


def test_strip_pseudo_spares_real_clear_command():
    """A genuine lowercase-operand clear-command ('clear counters') is real and
    must survive — the net keeps uppercase-mnemonic fabrications only."""
    text = "Run:\n```\nclear counters\n```\nDone."
    out = strip_pseudo_command_blocks(text)
    assert "clear counters" in out
    assert "```" in out


def test_strip_pseudo_command_block_when_model_admits_not_in_docs():
    """If the model annotates a block as not in the docs, it must not remain a
    copyable code fence — the admission and the fence are contradictory."""
    text = (
        "```\nclear allchanmiss-out\n```\n"
        "(command not in retrieved documentation)"
    )
    out = strip_pseudo_command_blocks(text)
    assert "```" not in out
    assert out.strip() == "(command not in retrieved documentation)"


def test_strip_pseudo_leaves_real_command_block_intact():
    """A genuine lowercase CLI command block must be left exactly as-is."""
    text = "Run:\n```\nconfig interface 1/1/1 aprmode auto\n```\nDone."
    out = strip_pseudo_command_blocks(text)
    assert "config interface 1/1/1 aprmode auto" in out
    assert "```" in out                           # fence preserved
    assert "(command not in retrieved documentation)" not in out
