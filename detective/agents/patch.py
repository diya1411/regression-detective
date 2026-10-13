"""Patch agent: minimal fix, validated in the sandbox (failing test + full suite vs. HEAD baseline).

A failed attempt loops back into this node with the failure output (the graph's second feedback
edge). Output is a draft PR proposal — never a merge.
"""
from __future__ import annotations

import difflib
import json
import shutil
import tempfile
from pathlib import Path

from ..sandbox import ProbeRequest, discover
from ..state import Investigation
from ..tools.git import apply_patch
from .base import Ctx, probe_raw
from .dependency import pin_line
from .prompts import PATCH_SCHEMA, PATCH_SYSTEM

PATCHABLE = ("resolved", "multi_cause", "dependency")


def run(ctx: Ctx, inv: Investigation) -> Investigation:
    c = inv.conclusion or {}
    if c.get("status") not in PATCHABLE:
        inv.patch = {"status": "skipped", "reason": f"no patch for a {c.get('status')} conclusion"}
        return inv
    inv.patch_attempts += 1
    attempt = inv.patch_attempts
    head = inv.head.sha
    expl = inv.explanation or {}
    prev = (inv.patch or {}).get("history", [])

    if "baseline_suite" not in inv.extra:
        ctx.bus.say("PATCH", "Running full suite at HEAD for a baseline", "collateral damage is measured against it")
        rb = ctx.sandbox.probe(ProbeRequest(head, [inv.test_id], full_suite=True,
                                            timeout=ctx.settings.probe_timeout_s * 3, label="baseline suite"))
        ob = ctx.evidence.record("PATCH", "sandbox", "full suite at HEAD (baseline)",
                                 f"{sum(v == 'passed' for v in rb.suite.values())} passed, "
                                 f"{sum(v != 'passed' for v in rb.suite.values())} not passing",
                                 {"suite": rb.suite}, probe_raw(rb))
        inv.extra["baseline_suite"] = {"suite": rb.suite, "evidence": ob.id}

    edits, rationale, target, source = _propose(ctx, inv, expl, prev)
    if not edits:
        inv.patch = {"status": "failed", "reason": "no applicable edit could be proposed", "history": prev,
                     "attempts": attempt}
        return inv

    new_files = edits
    deps = None
    if any(p.rsplit("/", 1)[-1].startswith(("requirements", "pyproject")) for p in new_files):
        deps = discover(lambda p: new_files.get(p, ctx.repo.show_file(head, p)))
    r = ctx.sandbox.probe(ProbeRequest(head, [inv.test_id], file_edits=new_files, deps=deps, label=f"patch {attempt}"))
    o1 = ctx.evidence.record("PATCH", "sandbox", f"patch attempt {attempt}: failing test",
                             f"{inv.test_id.split('::')[-1]} with patch: {r.verdict.upper()}",
                             {"sha": head, "verdict": r.verdict, "files": list(new_files), "attempt": attempt},
                             probe_raw(r))
    diff = _unified(ctx, head, new_files)
    rec = {"attempt": attempt, "source": source, "target": target, "rationale": rationale, "diff": diff,
           "test_verdict": r.verdict, "evidence": [o1.id]}
    if r.verdict != "pass":
        rec["failure"] = (r.message or r.log)[-2500:]
        return _retry(ctx, inv, prev + [rec], attempt, f"patched test {r.verdict}")

    rs = ctx.sandbox.probe(ProbeRequest(head, [inv.test_id], file_edits=new_files, deps=deps, full_suite=True,
                                        timeout=ctx.settings.probe_timeout_s * 3, label=f"patch {attempt} suite"))
    base = inv.extra["baseline_suite"]["suite"]
    collateral = sorted(t for t, o in base.items() if o == "passed" and rs.suite.get(t) != "passed")
    fixed = sorted(t for t, o in rs.suite.items() if o == "passed" and base.get(t) != "passed")
    o2 = ctx.evidence.record("PATCH", "sandbox", f"patch attempt {attempt}: full suite",
                             f"{sum(v == 'passed' for v in rs.suite.values())} passed, "
                             f"{sum(v != 'passed' for v in rs.suite.values())} not passing · "
                             f"{len(collateral)} newly broken · {len(fixed)} fixed",
                             {"suite": rs.suite, "collateral": collateral, "fixed": fixed}, probe_raw(rs))
    rec["evidence"].append(o2.id)
    rec.update({"suite_passed": sum(v == "passed" for v in rs.suite.values()),
                "suite_failed": sum(v != "passed" for v in rs.suite.values()),
                "collateral": collateral, "fixed": fixed})
    if collateral:
        rec["failure"] = "collateral breakage: " + ", ".join(collateral[:10])
        return _retry(ctx, inv, prev + [rec], attempt, f"{len(collateral)} test(s) newly broken")

    inv.patch = {"status": "validated", **rec, "history": prev + [rec], "attempts": attempt,
                 "files": {p: c for p, c in new_files.items()}}
    inv.claim("PATCH", "With the candidate patch the failing test passes at HEAD.", [o1.id],
              check={"type": "probe", "evidence": o1.id, "sha": head, "verdict": "pass"})
    inv.claim("PATCH", f"The full suite with the patch has {rec['suite_passed']} passing tests and no test that "
                       f"passed at HEAD is newly broken.", [o2.id],
              check={"type": "suite", "evidence": o2.id, "collateral": 0})
    ctx.bus.say("PATCH", "Candidate patch applied, full suite run",
                f"{rec['suite_passed']} passed, {rec['suite_failed']} failed · no collateral breakage"
                + (f" · {source}" if source != "gemini" else ""), [o1.id, o2.id], "ok")
    return inv


def _retry(ctx: Ctx, inv: Investigation, history: list, attempt: int, why: str) -> Investigation:
    more = attempt < ctx.settings.budget.max_patch_attempts and inv.extra.get("patch_can_retry", True)
    inv.patch = {"status": "retry" if more else "failed", "reason": why, "history": history, "attempts": attempt}
    ctx.bus.say("PATCH", f"Patch attempt {attempt} rejected: {why}",
                "feeding the failure back for another attempt" if more else "giving up — report without a patch",
                history[-1]["evidence"], "no")
    return inv


def _propose(ctx: Ctx, inv: Investigation, expl: dict, prev: list) -> tuple[dict, str, str, str]:
    head = inv.head.sha
    c = inv.conclusion
    if c["status"] == "dependency":
        inv.extra["patch_can_retry"] = False
        return _pin_edits(ctx, inv)
    culprits = [inv.commits[i] for i in c["culprits"]]
    files = sorted({p for cm in culprits for _, p in ctx.repo.changed_files(cm.sha) if p.endswith(".py")})
    test_path = inv.test_id.split("::")[0]
    if test_path not in files:
        files.append(test_path)
    contents = {p: ctx.repo.show_file(head, p) for p in files}
    contents = {p: t for p, t in contents.items() if t is not None}
    prompt = {"failing_test": inv.test_id, "symptom": inv.symptom,
              "classification": expl.get("classification"), "recommended_fix": expl.get("recommended_fix"),
              "explanation": {k: expl.get(k) for k in ("summary", "mechanism", "why_assertion_breaks")},
              "culprit_diffs": [{"sha": cm.short, "subject": cm.subject, "diff": ctx.repo.commit_diff(cm.sha)[:10000]}
                                for cm in culprits],
              "files_at_head": contents,
              "previous_attempts": [{"diff": h.get("diff", "")[:4000], "failure": h.get("failure", "")[:2000]}
                                    for h in prev]}
    out = ctx.llm.json("patch", PATCH_SYSTEM, json.dumps(prompt, indent=1), PATCH_SCHEMA, temperature=0.1)
    if out and out.get("edits"):
        new, ok = dict(contents), True
        for e in out["edits"]:
            p = e.get("path")
            if p not in new or new[p].count(e.get("search", "")) != 1 or not e.get("search"):
                ok = False
                break
            new[p] = new[p].replace(e["search"], e["replace"], 1)
        if ok:
            changed = {p: t for p, t in new.items() if t != contents.get(p)}
            if changed:
                return changed, out.get("rationale", ""), out.get("target", "code"), "gemini"
    # ---- deterministic fallbacks (no LLM, or the LLM's edit did not apply)
    inv.extra["patch_can_retry"] = False
    if expl.get("classification") == "stale_test":
        sym = inv.symptom
        src = contents.get(test_path, "")
        if sym.get("expected") and sym.get("observed") and src.count(sym["expected"]) == 1:
            return ({test_path: src.replace(sym["expected"], sym["observed"], 1)},
                    "mechanical expectation update to the new intended behaviour — review required", "test",
                    "fallback: update expected value")
    return _revert_edits(ctx, inv, culprits) + ("fallback: revert culprit",)


def _revert_edits(ctx: Ctx, inv: Investigation, culprits) -> tuple[dict, str, str]:
    tmp = Path(tempfile.mkdtemp(prefix="rd-revert-"))
    try:
        ctx.repo.export(inv.head.sha, tmp)
        for cm in sorted(culprits, key=lambda x: x.timestamp, reverse=True):
            ok, _ = apply_patch(tmp, ctx.repo.commit_patch(cm.sha), reverse=True)
            if not ok:
                return {}, "revert does not apply at HEAD", "code"
        out = {}
        for cm in culprits:
            for _, p in ctx.repo.changed_files(cm.sha):
                f = tmp / p
                old = ctx.repo.show_file(inv.head.sha, p)
                new = f.read_text() if f.exists() else ""
                if new != (old or ""):
                    out[p] = new
        return out, "revert the culprit commit(s) at HEAD", "code"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pin_edits(ctx: Ctx, inv: Investigation) -> tuple[dict, str, str, str]:
    head = inv.head.sha
    sus = inv.dep.get("suspects", {})
    pkgs = inv.conclusion.get("packages") or list(sus)
    edits: dict[str, str] = {}
    for f in ("requirements.txt", "requirements/base.txt", "requirements-dev.txt"):
        txt = edits.get(f) or ctx.repo.show_file(head, f)
        if txt is None:
            continue
        for p in pkgs:
            hit = pin_line(p, sus[p]["lg_version"], txt)
            if hit:
                txt = txt.replace(hit[0], hit[1], 1)
                edits[f] = txt
    missing = [p for p in pkgs if not any(p.replace("-", "_").lower() in t.lower().replace("-", "_") for t in edits.values())]
    if missing:
        base = edits.get("requirements.txt") or ctx.repo.show_file(head, "requirements.txt") or ""
        edits["requirements.txt"] = base.rstrip("\n") + "\n" + "\n".join(
            f"{p}=={sus[p]['lg_version']}  # pinned by Regression Detective" for p in missing) + "\n"
    return edits, "pin the regressing package(s) to the last-good version", "manifest", "deterministic pin"


def _unified(ctx: Ctx, head: str, files: dict[str, str]) -> str:
    out = []
    for p, new in sorted(files.items()):
        old = ctx.repo.show_file(head, p) or ""
        out += difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{p}", f"b/{p}")
    return "".join(out)
