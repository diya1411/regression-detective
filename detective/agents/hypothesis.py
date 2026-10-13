"""Hypothesis agent: the five root-cause candidates, priors seeded from similar past failures (RAG point one)."""
from __future__ import annotations

import json

from ..belief import Belief, Hypothesis
from ..rag import dump_hits
from ..state import Investigation
from .base import Ctx
from .prompts import HYPOTHESIS_SCHEMA, HYPOTHESIS_SYSTEM

BASE = {"commit": 0.50, "flaky": 0.15, "dependency": 0.20, "environment": 0.10, "fixture": 0.05}
TEMPLATES = {
    "commit": ("H1", "Bad commit in range", "Bad commit in|range",
               "bisection isolates a commit and reverting it at HEAD makes the test pass",
               "the test also fails at last-good, or reverting the isolated commit does not help"),
    "flaky": ("H2", "Test is flaky", "Test is flaky|",
              "repeated runs at one commit give both passes and failures",
              "repeated runs are deterministic at HEAD and at last-good"),
    "dependency": ("H3", "Dependency released in window", "Dependency|released",
                   "pinning suspect packages to their last-good versions makes HEAD pass",
                   "no package version differs, or the pinned environment still fails"),
    "environment": ("H4", "CI environment changed", "CI environment|changed",
                    "runner image / Python / CI config differs between the last green and the failing run",
                    "CI configuration and runner metadata are unchanged across the window"),
    "fixture": ("H5", "Fixture data changed", "Fixture data|changed",
                "the test path reads external data (network, absolute paths)",
                "static analysis finds no external I/O in the test's import closure"),
}
RESOLUTION_KIND = {"commit": "commit", "defect": "commit", "flaky": "flaky", "dependency": "dependency",
                   "environment": "environment", "env": "environment", "fixture": "fixture", "data": "fixture"}
ALPHA = 2.0


def run(ctx: Ctx, inv: Investigation) -> Investigation:
    bus = ctx.bus
    stats = ctx.retriever.build(inv.last_good.sha)
    idx_obs = ctx.evidence.record("RAG", "index", f"index corpora at {inv.last_good.short}",
                                  " · ".join(f"{k}: {v}" for k, v in stats.items()), stats, json.dumps(stats),
                                  kind="retrieval")

    # ------------------------------------------------------------ retrieval point one
    query = f"{inv.test_id}\n{inv.symptom.get('summary', '')}\n{inv.symptom.get('assertion', '')}\n" + \
            " ".join(inv.symptom.get("suspect_modules", []))
    hits = ctx.retriever.similar_failures(query, k=5)
    counts = {k: 0.0 for k in BASE}
    for rank, (doc, _, _) in enumerate(hits):
        kind = RESOLUTION_KIND.get(str(doc.meta.get("resolution", "")).lower())
        if kind:
            counts[kind] += 1.0 / (rank + 1)
    archive = [d for d in ctx.retriever.index.docs if d.corpus == "ci"]
    same_test = [d for d in archive if d.meta.get("test_id") == inv.test_id]
    flake_record = any(RESOLUTION_KIND.get(str(d.meta.get("resolution")).lower()) == "flaky" for d in same_test)

    retr = {k: ALPHA * BASE[k] + counts[k] for k in BASE}
    notes = []
    if ctx.retriever.use_history and archive and not flake_record:
        retr["flaky"] *= 0.6
        notes.append("flakiness prior lowered — this test has no flake record in the CI archive")
    if ctx.retriever.use_history and flake_record:
        retr["flaky"] *= 2.0
        notes.append("flakiness prior raised — this test has been resolved as flaky before")
    if "unverified" in inv.window_source:
        notes.append("last-good is a default guess, not a verified CI green")
    z = sum(retr.values())
    retr = {k: v / z for k, v in retr.items()}

    resolved = [d.meta.get("resolution") for d, _, _ in hits]
    rag_obs = ctx.evidence.record(
        "RAG", "retrieval", "similar past failures (CI archive)",
        f"{len(hits)} hits" + (f" · resolutions: {', '.join(map(str, resolved))}" if hits else "")
        + ("" if ctx.retriever.use_history else " (history index disabled)"),
        {"hits": [{"id": d.id, "resolution": d.meta.get("resolution"), "score": s, **r} for d, s, r in hits],
         "retrieval_prior": retr, "notes": notes}, dump_hits(hits), kind="retrieval")
    bus.say("RAG", "Retrieving similar past failures",
            (f"{len(hits)} hits in CI archive · " + ", ".join(f"{k}: {sum(1 for x in resolved if RESOLUTION_KIND.get(str(x).lower()) == k)}"
                                                           for k in BASE if any(RESOLUTION_KIND.get(str(x).lower()) == k for x in resolved)))
            if hits else ("history index disabled" if not ctx.retriever.use_history else "no similar failures on record"),
            [idx_obs.id, rag_obs.id])

    # ------------------------------------------------------------ LLM: tailored hypotheses
    facts = {
        "symptom": inv.symptom,
        "window": {"commits": len(inv.commits) - 1, "last_good": inv.last_good.short, "head": inv.head.short,
                   "source": inv.window_source,
                   "commits_touching_test_closure": sum(1 for w in inv.relevance.values() if w >= 1)},
        "similar_failures": [{"id": d.id, "resolution": d.meta.get("resolution"), "text": d.text[:600]}
                             for d, _, _ in hits],
        "test_has_flake_record": flake_record,
        "retrieval_prior": {k: round(v, 3) for k, v in retr.items()},
    }
    out = ctx.llm.json("hypothesis", HYPOTHESIS_SYSTEM,
                       "Assess the five hypotheses for this failure.\n" + json.dumps(facts, indent=1, default=str),
                       HYPOTHESIS_SCHEMA)
    llm_h = {}
    if out:
        for h in out.get("hypotheses", []):
            if h.get("kind") in BASE:
                llm_h[h["kind"]] = h
    final = {}
    for k in BASE:
        p_llm = llm_h.get(k, {}).get("prior")
        if isinstance(p_llm, (int, float)) and 0 < p_llm < 1:
            final[k] = 0.5 * retr[k] + 0.5 * float(p_llm)
        else:
            final[k] = retr[k]
    z = sum(final.values())
    final = {k: max(0.01, v / z) for k, v in final.items()}

    hyps = []
    for k, (hid, title, short, confirm, elim) in TEMPLATES.items():
        lh = llm_h.get(k, {})
        h = Hypothesis(id=hid, kind=k, title=lh.get("title") or title, short=short, prior=final[k], p=final[k],
                       criteria=f"confirm: {lh.get('confirm_if') or confirm} · eliminate: {lh.get('eliminate_if') or elim}",
                       rationale=lh.get("rationale", "; ".join(notes)))
        if k == "commit":
            h.good, h.bad = 0, inv.head_idx
            h.weights = dict(inv.relevance)
        hyps.append(h)
    inv.tree = Belief(hyps)
    for h in hyps:
        h.prior = h.p
    pri_obs = ctx.evidence.record("HYPOTHESIS", "policy", "seed priors",
                                  " · ".join(f"{h.id} {h.p:.2f}" for h in hyps),
                                  {"retrieval_prior": retr, "llm_prior": {k: v.get("prior") for k, v in llm_h.items()},
                                   "final": {h.id: h.p for h in hyps}, "notes": notes},
                                  json.dumps({"retrieval": retr, "llm": llm_h, "notes": notes}, indent=1),
                                  kind="derived")
    bus.emit("tree.init", root={"label": inv.test_id.split("::")[-1], "sub": "FAILS on " + inv.head.short},
             hypotheses=[_ui(h) for h in hyps])
    detail = "; ".join(notes) if notes else "priors from base rates" + (" + Gemini" if llm_h else "")
    bus.say("HYPOTHESIS", f"{len(hyps)} candidates generated, priors seeded from history", detail,
            [rag_obs.id, pri_obs.id])
    return inv


def _ui(h: Hypothesis) -> dict:
    return {"id": h.id, "kind": h.kind, "title": h.title, "short": h.short, "p": round(h.p, 4),
            "status": h.status, "criteria": h.criteria, "rationale": h.rationale, "parent": h.parent}
