"""Root-cause report (JSON + Markdown) and the draft PR proposal."""
from __future__ import annotations

import json

from .agents.base import Ctx
from .state import Investigation
from .tools.forge import PR_BRANCH_PREFIX


def build(ctx: Ctx, inv: Investigation) -> dict:
    c = inv.conclusion or {}
    v = inv.verification or {}
    e = inv.explanation or {}
    p = inv.patch or {}
    culprits = [{"index": i, **inv.commits[i].to_dict()} for i in c.get("culprits", [])]
    report = {
        "id": inv.id,
        "repo": inv.repo_url or str(inv.repo_path),
        "test": inv.test_id,
        "status": inv.status if inv.status == "error" else c.get("status"),
        "head": inv.head.to_dict() if inv.head else None,
        "last_good": inv.last_good.to_dict() if inv.last_good else None,
        "window": {"commits": len(inv.commits) - 1 if inv.commits else 0, "source": inv.window_source},
        "symptom": inv.symptom,
        "conclusion": c,
        "culprits": culprits,
        "explanation": {k: v_ for k, v_ in e.items() if k not in ("culprits",)},
        "culprit_diffs": [{"short": x["short"], "diff": x["diff"]} for x in e.get("culprits", [])],
        "patch": {k: p.get(k) for k in ("status", "source", "target", "rationale", "diff", "suite_passed",
                                         "suite_failed", "collateral", "attempts", "reason")} if p else None,
        "verification": v,
        "hypotheses": [h.to_dict() for h in inv.tree.h.values()] if inv.tree else [],
        "probes": inv.probes,
        "flaky": inv.flaky,
        "dependency": inv.dep,
        "environment": inv.env,
        "decisions": inv.decisions,
        "budget": {"probes": inv.probe_count, "max_probes": ctx.settings.budget.max_probes,
                   "seconds": round(inv.elapsed(), 1), "max_seconds": ctx.settings.budget.max_seconds,
                   "llm": ctx.llm.describe(), "iterations": inv.iteration,
                   "theoretical_min_probes": _log2ceil(len(inv.commits) - 1) if inv.commits else 0,
                   "bisection_probes": sum(1 for x in inv.probes if x["verdict"] in ("pass", "fail"))},
        "safety": {"sandbox": ctx.sandbox.describe(),
                   "repository": "read-only on the investigated repo; probes run on exported copies; the agent "
                                 f"may only push {PR_BRANCH_PREFIX}* branches and only opens draft PRs",
                   "data": "logs and code are redacted for credential-shaped strings before any LLM call"
                           + ("" if ctx.llm.enabled else "; no LLM was used in this run")},
        "evidence_count": len(ctx.evidence),
        "notes": inv.notes,
        "pr": None,
    }
    if p.get("status") == "validated" and p.get("diff"):
        body = pr_body(report)
        pr_dir = ctx.inv_dir / "pr"
        pr_dir.mkdir(exist_ok=True)
        (pr_dir / "patch.diff").write_text(p["diff"])
        (pr_dir / "PR_BODY.md").write_text(body)
        report["pr"] = {"kind": "proposal", "files": [str(pr_dir / "patch.diff"), str(pr_dir / "PR_BODY.md")],
                        "allowed": v.get("patch_allowed", False)}
        if v.get("patch_allowed") and ctx.settings.open_pr:
            try:
                patch_bytes = _as_git_patch(p["diff"])
                report["pr"] = ctx.forge.open_draft_pr(
                    repo_path=inv.repo_path, work_dir=ctx.inv_dir / "pr", base_sha=inv.head.sha,
                    branch=f"{PR_BRANCH_PREFIX}{inv.id}", title=f"Fix {inv.test_id.split('::')[-1]} regression"
                    + (f" from {culprits[0]['short']}" if culprits else ""), body=body, patch=patch_bytes)
            except Exception as ex:
                report["pr"]["error"] = str(ex)[:300]
    (ctx.inv_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (ctx.inv_dir / "report.md").write_text(markdown(report))
    return report


def _as_git_patch(diff: str) -> bytes:
    return diff.encode() if diff.endswith("\n") else (diff + "\n").encode()


def _log2ceil(n: int) -> int:
    k = 0
    while (1 << k) < max(n, 1):
        k += 1
    return k


def pr_body(r: dict) -> str:
    e, v, p = r["explanation"], r["verification"], r["patch"] or {}
    cul = ", ".join(f"`{c['short']}` ({c['subject']})" for c in r["culprits"]) or "n/a"
    return f"""## Regression Detective: {r['test']}

**Status:** {r['status']} · **Confidence:** {v.get('confidence')} · **Culprit:** {cul}

### What broke
{e.get('summary', '')}

{e.get('mechanism', '')}

{e.get('why_assertion_breaks', '')}

**Classification:** {e.get('classification', '')} — {e.get('classification_rationale', '')}

### Patch
{p.get('rationale', '')}  (target: {p.get('target')}, source: {p.get('source')})

Full suite with patch: {p.get('suite_passed')} passed, {p.get('suite_failed')} failed, {len(p.get('collateral') or [])} newly broken.

### Evidence
{v.get('counts', {}).get('supported', 0)} of {len(v.get('claims', []))} claims backed by recorded tool output;
{v.get('counts', {}).get('inferred', 0)} inferred, {v.get('counts', {}).get('unsupported', 0)} unsupported.

> Draft opened for human review. Regression Detective never merges, never pushes to the default branch.
"""


def markdown(r: dict) -> str:
    e, v = r["explanation"], r["verification"] or {}
    lines = [f"# Root-cause report — `{r['test']}`", "",
             f"- **Status:** {r['status']}",
             f"- **Repository:** {r['repo']}",
             f"- **Window:** {r['last_good']['short'] if r['last_good'] else '?'} → "
             f"{r['head']['short'] if r['head'] else '?'} ({r['window']['commits']} commits, {r['window']['source']})",
             f"- **Confidence:** {v.get('confidence')} (threshold {v.get('threshold')})",
             f"- **Budget:** {r['budget']['probes']} probes (bisection {r['budget']['bisection_probes']}, "
             f"log2 minimum {r['budget']['theoretical_min_probes']}), {r['budget']['seconds']}s, "
             f"{r['budget']['llm']['calls']} LLM calls", ""]
    if r["culprits"]:
        lines += ["## Culprit", ""]
        for x in r["culprits"]:
            lines.append(f"- `{x['short']}` “{x['subject']}” — {x['author']}, {x['date']}")
        lines.append("")
    lines += ["## Explanation", "", f"**{e.get('summary', '')}**", "", e.get("mechanism", ""), "",
              e.get("why_assertion_breaks", ""), "",
              f"Classification: **{e.get('classification', '')}** — {e.get('classification_rationale', '')}", ""]
    if e.get("next_step"):
        lines += [f"Next step: {e['next_step']}", ""]
    for d in r.get("culprit_diffs", []):
        lines += [f"### Diff of {d['short']}", "", "```diff", d["diff"][:6000], "```", ""]
    if r["patch"] and r["patch"].get("diff"):
        lines += ["## Proposed patch" + ("" if v.get("patch_allowed") else " (SUPPRESSED by verifier)"), "",
                  r["patch"].get("rationale") or "", "", "```diff", r["patch"]["diff"][:6000], "```", ""]
    lines += ["## Hypotheses", "", "| id | hypothesis | prior | final | status |", "|---|---|---|---|---|"]
    for h in r["hypotheses"]:
        lines.append(f"| {h['id']} | {h['title']} | {h['prior']:.2f} | {h['p']:.3f} | {h['status']}"
                     + (f" ({h['eliminated_by']})" if h.get("eliminated_by") else "") + " |")
    if r["probes"]:
        lines += ["", "## Probes", "", "| # | track | commit | verdict | evidence |", "|---|---|---|---|---|"]
        for i, pr in enumerate(r["probes"], 1):
            lines.append(f"| {i} | {pr['track']} | c{pr['index']:02d} `{pr['short']}` | {pr['verdict']}"
                         + (f" ({pr['skip_kind']})" if pr.get("skip_kind") else "")
                         + (f" repaired:{pr['repaired']}" if pr.get("repaired") else "") + f" | {pr['evidence']} |")
    lines += ["", "## Claims", "", "| verdict | claim | evidence | checked by |", "|---|---|---|---|"]
    for cl in v.get("claims", []):
        lines.append(f"| {cl['verdict']} | {cl['text']} | {', '.join(cl.get('evidence') or [])} | {cl.get('checked_by')} |")
    if v.get("missing_evidence"):
        lines += ["", "## Missing evidence", ""] + [f"- {m}" for m in v["missing_evidence"]]
    lines += ["", "## Safety", "", f"- Sandbox: {r['safety']['sandbox'].get('kind')} — "
                                   f"{r['safety']['sandbox'].get('isolation')}",
              f"- Repository: {r['safety']['repository']}", f"- Data: {r['safety']['data']}"]
    return "\n".join(lines) + "\n"
