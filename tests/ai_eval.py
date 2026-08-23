"""Eval harness for the ATLAS doc assistant.

Runs a set of known-answer questions (seeded from real field-tech feedback)
against the live index and checks that retrieval surfaces the authoritative
content and that the answer doesn't drift across platforms. This is the quality
gate: when we tune chunking, top_k, keyword_k, or the prompt, re-run this and
confirm we didn't regress the cases coworkers already caught.

NOT a pytest module (it makes real API calls and needs OPENAI_API_KEY). Run it:

    python -m tests.ai_eval                 # run all cases
    python -m tests.ai_eval ospf-create     # run cases whose id contains this

Each case asserts some mix of:
  expect_doc_substr      - at least one citation's doc name contains this
  expect_text_in_context - the (neighbor-expanded) retrieved text contains this
                           verbatim string -> the authoritative chunk surfaced
  forbid_in_answer       - this string must NOT appear in the answer prose
                           (catches fabricated / cross-platform command syntax)
"""

from __future__ import annotations

import re
import sys

from utils.ai.assistant import DocAssistant


EVAL_CASES = [
    {
        "id": "ospf-create-rls",
        "question": "How do I configure OSPF on a Ciena RLS, including creating "
                    "and naming the protocol instance?",
        "expect_doc_substr": ["323-2051"],          # an RLS doc
        "expect_text_in_context": ["config identifier OSPF name"],
        "forbid_in_answer": [r"ospf\s+instance\s+\S*\d"],  # SAOS command form, not prose
        # KNOWN GAP: the create-step command lives in a dense config block
        # (OAM p.120 / ZTP) that is semantically dissimilar to a prose "how do
        # I configure" question, so similarity retrieval lands on the concept
        # pages instead. Tried: neighbor expansion, hybrid keyword, platform
        # scoping, HyDE (+HyDE keyword terms). Not closed. Safe because of
        # extractive mode (no fabrication). Future fix: finer-grained indexing
        # of command/procedure pages, or a procedure-aware retriever.
        "known_limitation": True,
    },
    {
        "id": "ospf-network-type-rls",
        # Reality (per RLS OAM p.38-41 + CLI Ref p.118-122): network-type is
        # NOT a standalone settable command - it is auto-derived from
        # numbered (broadcast) vs unnumbered (point-to-point). A correct answer
        # explains that, rather than pointing to a non-existent setter.
        "question": "On a Ciena RLS OSPFv2 Ethernet interface, what is the "
                    "default network-type and can I change it?",
        "expect_doc_substr": ["323-2051"],
        "expect_text_in_context": ["automatically sets"],  # the authoritative behavior
        "expect_in_answer": ["automat"],                    # must convey it's auto-set
        "forbid_in_answer": [r"ospf\s+instance\s+\S*\d"],  # SAOS command form, not prose              # no SAOS syntax
    },
    {
        "id": "high-temp-alarm",
        "question": "What does a High Temperature alarm indicate and how do I clear it?",
        "expect_doc_substr": [],                       # any platform's alarm guide ok
        "expect_text_in_context": ["temperature"],
        "forbid_in_answer": [],
    },
    {
        "id": "bgp-on-1830",
        # Ground truth: the 1830 OLS DOES document BGP (Product Info & Planning
        # Guide section 8.11). So a correct answer cites 1830 docs and discusses
        # BGP - it must NOT refuse, and must NOT drift to RLS/SAOS BGP docs.
        "question": "How is BGP used on a Nokia 1830 OLS?",
        "expect_doc_substr": ["1830"],
        "expect_in_answer": ["bgp"],
        "forbid_in_answer": ["network-instances network-instance"],  # RLS syntax
    },
    {
        "id": "saos6-mgmt-ip",
        # Version isolation: SAOS 6 uses classic CLI; SAOS 10 uses oc-if:.
        # A SAOS-6 question must cite SAOS-6 docs and must NOT surface SAOS-10
        # 'oc-if:interfaces' syntax.
        "question": "How do I change the management IP address on a Ciena SAOS 6 device?",
        "expect_doc_substr": ["saos_6"],
        "expect_text_in_context": ["interface set ip-interface"],  # the cmd IS retrieved
        "expect_in_answer": ["ip-interface"],         # must deliver it, not refuse
        "forbid_in_answer": [r"oc-if:interfaces"],    # SAOS 10 syntax, wrong version
    },
    {
        "id": "rls-mgmt-ip",
        # RLS management IP = the loopback (shelf) IP; RLS OAM Procedure 14.
        # Verbatim procedure-passthrough; RLS uses openconfig-interfaces:,
        # not SAOS 10's oc-if:.
        "question": "How do I change the management IP address on a Ciena RLS device?",
        "expect_doc_substr": ["323-2051"],
        "expect_text_in_context": ["interface loopback subinterfaces subinterface 0"],
        "expect_in_answer": ["loopback"],
        "forbid_in_answer": [r"oc-if:interfaces"],   # SAOS 10 syntax, wrong platform
    },
    {
        "id": "osc-power-rls-ols",
        # Structured glossary lookup: OSC RX/TX power resolves to exact params
        # per platform (RLS opticalPowerInput/OutputOSC; OLS Supvy In Power /
        # txpower), not a guessed command. Multi-platform -> both cited.
        "question": "how do I find OSC RX and TX power measurements on RLS and OLS?",
        "expect_doc_substr": ["2051"],
        "expect_in_answer": ["opticalpowerinput", "supvy in power"],
    },
    {
        "id": "cisco-ospf-refusal",
        # No Cisco docs in the corpus -> must refuse, not fabricate.
        "question": "How do I configure OSPF on a Cisco Catalyst switch?",
        "expect_refusal": True,
    },
]


def _run_case(assistant: DocAssistant, case: dict) -> dict:
    answer = assistant.ask(case["question"])
    answer_lower = answer.text.lower()
    cite_docs = [h.doc_name for h in answer.citations]
    context_blob = "\n".join(answer.excerpts).lower()

    failures = []

    for substr in case.get("expect_doc_substr", []):
        if not any(substr.lower() in d.lower() for d in cite_docs):
            failures.append(f"no citation doc contains {substr!r}")

    for substr in case.get("expect_text_in_context", []):
        if substr.lower() not in context_blob:
            failures.append(f"retrieved context missing {substr!r}")

    for substr in case.get("expect_in_answer", []):
        if substr.lower() not in answer_lower:
            failures.append(f"answer missing expected {substr!r}")

    # forbid_in_answer entries are regex patterns (case-insensitive) so we can
    # target an actual command form (e.g. the SAOS 'ospf instance <N>') without
    # false-positiving on ordinary prose like 'the OSPF instance'.
    for pat in case.get("forbid_in_answer", []):
        if re.search(pat, answer.text, re.IGNORECASE):
            failures.append(f"answer matches forbidden /{pat}/")

    if case.get("expect_refusal"):
        refused = ("don't find" in answer_lower or "not in the indexed" in answer_lower
                   or "do not find" in answer_lower)
        if not refused:
            failures.append("expected a refusal, got an answer")

    return {"id": case["id"], "failures": failures, "cite_docs": cite_docs}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    flt = argv[0] if argv else ""
    cases = [c for c in EVAL_CASES if flt in c["id"]]
    if not cases:
        print(f"no eval cases match {flt!r}")
        return 2

    assistant = DocAssistant()
    committed = [c for c in cases if not c.get("known_limitation")]
    passed = 0          # committed cases that passed
    real_failures = 0   # committed cases that failed (these fail the gate)
    print(f"Running {len(cases)} eval case(s)\n" + "=" * 60)
    for case in cases:
        result = _run_case(assistant, case)
        ok = not result["failures"]
        if case.get("known_limitation"):
            # Tracked gap (xfail-style): doesn't fail the gate. If it starts
            # passing, flag it loudly so we can promote it to a committed case.
            label = "FIXED! (promote to committed)" if ok else "KNOWN-GAP"
        else:
            passed += ok
            real_failures += (not ok)
            label = "PASS" if ok else "FAIL"
        print(f"\n[{label}] {result['id']}")
        if result["failures"]:
            for f in result["failures"]:
                print(f"    - {f}")
        for d in result["cite_docs"][:4]:
            print(f"      cite: {d[:60]}")

    gaps = len(cases) - len(committed)
    print("\n" + "=" * 60)
    print(f"{passed}/{len(committed)} committed passed"
          + (f"  ({gaps} known gap{'s' if gaps != 1 else ''})" if gaps else ""))
    return 0 if real_failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
