"""Explanation agent: causal account of the break, grounded by retrieval (RAG point two)."""
from __future__ import annotations

import json
import re

from ..rag import dump_hits
from ..sandbox.deps import MANIFEST_FILES
from ..state import Investigation
from ..tools.ast_tools import function_source, nondeterminism_scan
from .base import Ctx
from .prompts import EXPLANATION_SCHEMA, EXPLANATION_SYSTEM, FLAKY_SCHEMA, FLAKY_SYSTEM

INTENT = re.compile(r"\b(intentional(ly)?|intended|by design|behaviou?r change|breaking change|now (uses|rounds|returns)|"
                    r"switch(es|ed)? to|per adr|adr-\d+|decision record|update (the )?tests?|tests? (must|should|need to) "
                    r"be updated|deprecat\w+ the old)\b", re.I)
NEGATED = re.compile(r"\b(no|not|without|never|nor|pure(ly)?|only a)\b[\w\s,-]{0,24}$", re.I)


def intent_hits(text: str) -> list[str]:
    """Phrases declaring a behaviour change intentional, ignoring negated ones ("no behaviour change intended")."""
    out = []
    for m in INTENT.finditer(text or ""):
        before = text[max(0, m.start() - 40): m.start()]
        after = text[m.end(): m.end() + 12]
        if NEGATED.search(before) or re.match(r"\s*(intended|planned)?\s*(is|was)?\s*not\b", after, re.I):
            continue
        out.append(m.group(0))
    return out


def run(ctx: Ctx, inv: Investigation) -> Investigation:
    c = inv.conclusion or {}
    if c.get("culprits"):
        _explain_commits(ctx, inv)
    elif c.get("status") == "flaky":
        _explain_flaky(ctx, inv)
    elif c.get("status") == "dependency":
        _explain_dependency(ctx, inv)
    else:
        _explain_partial(ctx, inv)
    ctx.bus.emit("explanation", **{k: v for k, v in (inv.explanation or {}).items() if k != "context"})
    return inv


# ----------------------------------------------------------------------------- culprit commits
def _explain_commits(ctx: Ctx, inv: Investigation) -> None:
    c = inv.conclusion
    test_path, _, test_name = inv.test_id.partition("::")
    test_name = test_name.split("::")[-1] or test_name
    failure = inv.symptom.get("summary", "")
    ids: list[str] = []
    contexts = []
    for idx in c["culprits"]:
        commit = inv.commits[idx]
        cx = ctx.retriever.context_for_commit(commit.sha, failure, test_path, test_name)
        contexts.append((commit, cx))
        touched_names = [t["qualname"] for t in cx["touched"]]
        d = ctx.evidence.record("EXPLANATION", "git", f"diff of {commit.short}",
                                f"{commit.short} “{commit.subject}” changes {', '.join(cx['changed'][:5])}"
                                + (f"; functions: {', '.join(touched_names[:5])}" if touched_names else ""),
                                {"sha": commit.sha, "changed": cx["changed"], "touched": touched_names,
                                 "message": ctx.repo.message(commit.sha), "author": commit.author,
                                 "prior_commits": cx["prior_commits"]},
                                f"commit {commit.sha}\nAuthor: {commit.author}\nDate: {commit.date}\n\n"
                                f"{ctx.repo.message(commit.sha)}\n\n{cx['diff'][:20000]}", kind="static")
        t = ctx.evidence.record("EXPLANATION", "ast", f"functions touched by {commit.short}",
                                f"{len(cx['touched'])} definition(s) before/after",
                                {"touched": touched_names},
                                "\n\n".join(f"## {x['path']} :: {x['qualname']}\n--- before\n{x['before'] or '(new)'}\n"
                                            f"+++ after\n{x['after'] or '(deleted)'}" for x in cx["touched"]),
                                kind="static")
        ids += [d.id, t.id]
        for name, hits in (("design docs", cx["docs"]), ("PRs / issues", cx["history"] or []),
                           ("tests covering the touched functions", cx["covering_tests"])):
            if hits:
                o = ctx.evidence.record("RAG", "retrieval", f"{name} for {commit.short}",
                                        f"{len(hits)} hit(s): " + "; ".join(h[0].title for h in hits[:3]),
                                        {"hits": [{"id": h[0].id, "title": h[0].title, **h[2]} for h in hits]},
                                        dump_hits(hits), kind="retrieval")
                ids.append(o.id)
        if cx["history_by_files"]:
            o = ctx.evidence.record("RAG", "retrieval", f"PRs touching the same files as {commit.short}",
                                    "; ".join(x.title for x in cx["history_by_files"][:4]),
                                    {"items": [x.id for x in cx["history_by_files"]]},
                                    "\n\n".join(f"{x.title}\n{x.text[:2000]}" for x in cx["history_by_files"][:6]),
                                    kind="retrieval")
            ids.append(o.id)
        prs = sorted({h[0].title.split(":")[0] for h in (cx["history"] or [])[:2]} |
                     {x.title.split(":")[0] for x in cx["history_by_files"][:2]})
        ctx.bus.say("RAG", "Retrieving context for culprit diff",
                    f"{', '.join(cx['changed'][:2])} call graph · {' · '.join(prs) or 'no related PRs'} · "
                    f"{len(cx['prior_commits'])} prior change(s) to these files", ids[-4:])

    test_src = ctx.repo.show_file(inv.head.sha, test_path) or ""
    fn = function_source(test_src, test_name) or test_src[:3000]
    to = ctx.evidence.record("EXPLANATION", "ast", f"source of {test_name}", f"{test_path}::{test_name}",
                             {"path": test_path}, fn, kind="static")
    ids.append(to.id)

    heuristic = _heuristic(ctx, inv, contexts, ids)
    evidence_view = _evidence_view(ctx, inv)
    prompt = {
        "failing_test": inv.test_id, "symptom": inv.symptom,
        "culprits": [{"sha": cm.sha, "short": cm.short, "subject": cm.subject, "message": ctx.repo.message(cm.sha),
                      "diff": cx["diff"][:12000],
                      "touched_functions": cx["touched"][:8],
                      "design_docs": [{"title": h[0].title, "text": h[0].text[:1500]} for h in cx["docs"]],
                      "related_prs_issues": [{"title": h[0].title, "text": h[0].text[:1500]} for h in (cx["history"] or [])]
                      + [{"title": x.title, "text": x.text[:1500]} for x in cx["history_by_files"][:3]],
                      "prior_commits": cx["prior_commits"]} for cm, cx in contexts],
        "test_source": fn[:4000],
        "multi_cause": len(c["culprits"]) > 1,
        "evidence": evidence_view,
    }
    out = ctx.llm.json("explanation", EXPLANATION_SYSTEM, json.dumps(prompt, indent=1, default=str),
                       EXPLANATION_SCHEMA, max_tokens=8192)
    if out:
        known = set(ctx.evidence.items)
        out["claims"] = [{**cl, "evidence": [e for e in cl.get("evidence", []) if e in known], "agent": "EXPLANATION"}
                         for cl in out.get("claims", [])]
        out["source"] = "gemini"
        out["heuristic_classification"] = heuristic["classification"]
        expl = out
    else:
        expl = heuristic
    expl["culprits"] = [{"sha": cm.sha, "short": cm.short, "subject": cm.subject, "author": cm.author,
                         "date": cm.date, "diff": cx["diff"][:30000], "changed": cx["changed"]}
                        for cm, cx in contexts]
    expl["evidence_ids"] = ids
    inv.explanation = expl
    cls = expl.get("classification", "unclear").replace("_", " ")
    ctx.bus.say("EXPLANATION", expl.get("summary", "")[:160],
                f"{cls} → {expl.get('recommended_fix', '').replace('_', ' ')}"
                + (" · (heuristic, no LLM)" if expl.get("source") == "heuristic" else ""), ids[:3])
    ctx.bus.emit("tree.evidence", hypothesis=inv.conclusion["hypothesis"], evidence=ids[:1],
                 label=f"{expl.get('classification', 'unclear').replace('_', ' ')}|{expl.get('recommended_fix', '').replace('_', ' ')}",
                 tone="no", second=True)


def _heuristic(ctx: Ctx, inv: Investigation, contexts, ids) -> dict:
    claims = []
    cls, why, fix = "defect", "no design note or PR in the retrieved context declares the change intentional", "patch_code"
    for commit, cx in contexts:
        manifests_only = cx["changed"] and all(p.rsplit("/", 1)[-1] in MANIFEST_FILES for p in cx["changed"])
        texts = [ctx.repo.message(commit.sha)] + [h[0].text for h in cx["docs"]] + \
                [h[0].text for h in (cx["history"] or [])] + [x.text for x in cx["history_by_files"]]
        doc_changed = any(p.lower().endswith((".md", ".rst")) for p in cx["changed"])
        hits = [h for t in texts for h in intent_hits(t)]
        if manifests_only:
            cls, why, fix = "dependency_change", "the culprit only changes dependency manifests", "pin_dependency"
        elif hits and (doc_changed or len(hits) >= 2):
            cls, why, fix = "stale_test", f"retrieved context declares the change intentional (“{hits[0]}”)", "update_test"
        names = ", ".join(t["qualname"] for t in cx["touched"][:4]) or ", ".join(cx["changed"][:3])
        claims.append({"text": f"{commit.short} (“{commit.subject}”) changed {names}.", "evidence": [ids[0]],
                       "kind": "observed", "agent": "EXPLANATION"})
    sym = inv.symptom
    if sym.get("observed") and sym.get("expected"):
        src = inv.flaky.get("evidence", [None])[0] or inv.extra.get("repro", {}).get("evidence")
        claims.append({"text": f"At HEAD the assertion observes {sym['observed']} where {sym['expected']} is expected.",
                       "evidence": [src] if src else [], "kind": "observed", "agent": "EXPLANATION"})
    rag_ids = [i for i in ids if ctx.evidence.get(i) and ctx.evidence.get(i).kind == "retrieval"]
    claims.append({"text": f"Classified as {cls.replace('_', ' ')}: {why}.", "evidence": rag_ids[:3] or ids[:1],
                   "kind": "inferred", "agent": "EXPLANATION"})
    first = contexts[0][0]
    return {"summary": f"{first.short} “{first.subject}” breaks {inv.test_id.split('::')[-1]}",
            "mechanism": "Bisection isolated the commit and reverting it at HEAD restores the passing test; "
                         "the mechanism below is derived from the diff without an LLM.",
            "why_assertion_breaks": sym.get("summary", ""), "classification": cls,
            "classification_rationale": why, "recommended_fix": fix, "claims": claims, "source": "heuristic"}


def _evidence_view(ctx: Ctx, inv: Investigation) -> list[dict]:
    view = []
    for o in ctx.evidence.all():
        view.append({"id": o.id, "agent": o.agent, "tool": o.tool, "action": o.action, "summary": o.summary,
                     "kind": o.kind, "excerpt": (o.raw_preview or "")[:700]})
    return view[-60:]


# ----------------------------------------------------------------------------- other conclusions
def _explain_flaky(ctx: Ctx, inv: Investigation) -> None:
    test_path, _, test_name = inv.test_id.partition("::")
    sources = {p: ctx.repo.show_file(inv.head.sha, p) or "" for p in inv.import_closure}
    found = nondeterminism_scan(sources)
    o = ctx.evidence.record("EXPLANATION", "ast", "scan for nondeterministic calls",
                            f"{len(found)} nondeterministic call site(s)" +
                            (": " + ", ".join(f"{f['file']}:{f['line']} {f['detail']}" for f in found[:3]) if found else ""),
                            {"findings": found}, "\n".join(f"{f['file']}:{f['line']} {f['detail']} seeded={f['seeded']}"
                                                           for f in found), kind="static")
    fl = inv.flaky
    claims = [{"text": f"Across {fl['runs']} runs the test failed {fl['head']['fails']} times at HEAD and "
                       f"{fl['lg']['fails']} times at last-good.", "evidence": fl.get("evidence", []),
               "kind": "observed", "agent": "EXPLANATION"}]
    if found:
        claims.append({"text": f"The test path calls {found[0]['detail']} at {found[0]['file']}:{found[0]['line']}"
                               + (" without a fixed seed." if not found[0]["seeded"] else "."),
                       "evidence": [o.id], "kind": "observed", "agent": "EXPLANATION"})
    expl = {"summary": f"{inv.test_id.split('::')[-1]} is nondeterministic; no commit in the window caused it",
            "mechanism": (f"Nondeterministic call {found[0]['detail']} in {found[0]['file']}" if found else
                          "Outcome varies between identical runs."),
            "suggested_fix": "seed or inject the random source / clock in the test" if found else
                             "quarantine and investigate the nondeterminism",
            "classification": "flaky", "recommended_fix": "none", "claims": claims, "source": "heuristic"}
    src = function_source(sources.get(test_path, ""), test_name) or ""
    out = ctx.llm.json("explanation-flaky", FLAKY_SYSTEM, json.dumps(
        {"test": inv.test_id, "runs": fl, "test_source": src[:3000], "scan": found,
         "evidence": _evidence_view(ctx, inv)}, indent=1, default=str), FLAKY_SCHEMA)
    if out:
        known = set(ctx.evidence.items)
        out["claims"] = [{**cl, "evidence": [e for e in cl.get("evidence", []) if e in known], "agent": "EXPLANATION"}
                         for cl in out.get("claims", [])]
        expl.update(out)
        expl["source"] = "gemini"
    expl["evidence_ids"] = [o.id]
    inv.explanation = expl
    ctx.bus.say("EXPLANATION", expl["summary"], expl["mechanism"][:160], [o.id])


def _explain_dependency(ctx: Ctx, inv: Investigation) -> None:
    pkgs = inv.conclusion.get("packages", [])
    sus = inv.dep.get("suspects", {})
    parts = [f"{p} {sus[p]['lg_version']} → {sus[p]['head_version']}" for p in pkgs if p in sus]
    ev = [e for cl in inv.claims if cl["agent"] == "DEPENDENCY" for e in cl["evidence"]]
    inv.explanation = {
        "summary": f"Dependency regression: {', '.join(parts)}",
        "mechanism": "Repository code is unchanged in effect: the test passes at HEAD once the package is pinned to "
                     "the version that was current at the last green build, so the newer release changed behaviour.",
        "why_assertion_breaks": inv.symptom.get("summary", ""), "classification": "dependency_change",
        "classification_rationale": "pin-and-rerun experiment at unchanged code", "recommended_fix": "pin_dependency",
        "claims": [{"text": f"The installed version of {p} at HEAD differs from last-good ({sus[p]['lg_version']} → "
                            f"{sus[p]['head_version']}).", "evidence": ev[:1], "kind": "observed",
                    "agent": "EXPLANATION"} for p in pkgs if p in sus],
        "source": "deterministic", "evidence_ids": ev}
    ctx.bus.say("EXPLANATION", inv.explanation["summary"], "classification: dependency change → pin", ev[:2])


def _explain_partial(ctx: Ctx, inv: Investigation) -> None:
    c = inv.conclusion
    top = inv.tree.h[c["hypothesis"]]
    if c["status"] == "window_invalid":
        og = c.get("older_good")
        summary = "The test already fails at the supplied last-good commit" + (
            f"; it passes at {og['short']} ({og['distance']} commits earlier)" if og else "")
        mech = "The search window's good endpoint is wrong, so no commit inside it can be named as the culprit."
        fix = f"re-run with --last-good {og['short']}" if og else "re-run with an earlier --last-good"
    elif top.is_commit:
        cands = c.get("candidates", [])
        summary = f"Narrowed to {len(cands)} candidate commit(s): " + ", ".join(inv.commits[i].short for i in cands[:8])
        mech = c.get("note") or "The evidence budget ran out before a single commit could be isolated and confirmed."
        fix = "raise the probe budget or inspect the remaining candidates"
    else:
        summary = f"Leading hypothesis {top.id} ({top.title}) at p={top.p:.2f}, not confirmed by experiment"
        mech = c.get("note", "")
        fix = "gather the missing evidence listed below"
    inv.explanation = {"summary": summary, "mechanism": mech, "why_assertion_breaks": inv.symptom.get("summary", ""),
                       "classification": "unclear" if c["status"] != "window_invalid" else "window_invalid",
                       "recommended_fix": "none", "next_step": fix, "claims": [], "source": "deterministic"}
    ctx.bus.say("EXPLANATION", summary, fix)
