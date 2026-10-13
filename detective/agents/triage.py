"""Triage agent: symptom signature, search window, and a relevance prior over the window's commits."""
from __future__ import annotations

import re

from ..sandbox import ProbeRequest
from ..sandbox.deps import MANIFEST_FILES
from ..state import Investigation
from ..tools.ast_tools import conftests_for, import_closure
from .base import Ctx, probe_raw
from .prompts import TRIAGE_SCHEMA, TRIAGE_SYSTEM

DEFAULT_WINDOW = 32
DOC_SUFFIX = (".md", ".rst", ".txt", ".adoc", ".png", ".jpg", ".svg")
_TB_LINE = re.compile(r"^([\w./\-]+\.py):(\d+):", re.M)
_ASSERT_CMP = re.compile(r"assert (.+?) (==|!=|<=|>=|<|>|is not|is|in|not in) (.+)$")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def run(ctx: Ctx, inv: Investigation, last_good: str | None, head: str) -> Investigation:
    repo, bus = ctx.repo, ctx.bus
    head_c = repo.commit(repo.rev_parse(head))
    inv.head = head_c

    # ------------------------------------------------------------- failure output
    log = ctx.forge.failure_log(inv.test_id, head_c.sha)
    source = "CI log"
    if not log:
        r = ctx.sandbox.probe(ProbeRequest(head_c.sha, [inv.test_id], label="reproduce"))
        if r.build and not r.build.cached:
            inv.cost.observe_build(r.build.seconds)
        if r.runs:
            inv.cost.observe_run(sum(x.duration for x in r.runs) / len(r.runs))
        obs = ctx.evidence.record("TRIAGE", "sandbox", f"run {inv.test_id} at HEAD {head_c.short}",
                                  f"reproduction at HEAD: {r.verdict}" + (f" ({r.skip_kind})" if r.skip_kind else ""),
                                  {"sha": head_c.sha, "verdict": r.verdict, "signature": r.signature},
                                  probe_raw(r))
        log = f"{r.message}\n\n{r.log}" if r.message else r.log
        source = f"sandbox reproduction {obs.id}"
        inv.extra["repro"] = {"verdict": r.verdict, "evidence": obs.id, "signature": r.signature}
    inv.failure_log = log or ""

    symptom = _parse_symptom(inv.test_id, inv.failure_log)
    llm_sym = ctx.llm.json("triage", TRIAGE_SYSTEM,
                           f"Failing test: {inv.test_id}\n\nFailure output:\n{inv.failure_log[-6000:]}",
                           TRIAGE_SCHEMA)
    if llm_sym:
        for k, v in llm_sym.items():
            if v not in (None, "", []):
                symptom[k] = v
        symptom["parsed_by"] = "gemini+regex"
    inv.symptom = symptom
    sym_obs = ctx.evidence.record("TRIAGE", "parser", "parse failure output", symptom.get("summary", ""),
                                  symptom, inv.failure_log[-6000:], kind="derived")
    mods = ", ".join(symptom.get("suspect_modules", [])[:4]) or "n/a"
    bus.say("TRIAGE", "Parsing failure output",
            f"symptom: {symptom.get('assertion') or symptom.get('summary')} · modules: {mods} · from {source}",
            [sym_obs.id])

    # ------------------------------------------------------------- search window
    lg_sha, inv.window_source = None, "user"
    if last_good:
        lg_sha = repo.rev_parse(last_good)
    else:
        green = ctx.forge.last_green(inv.test_id, head_c.sha, repo.is_ancestor)
        if green:
            lg_sha, inv.window_source = green.sha, f"CI run {green.id}"
        else:
            anc = repo.ancestors(head_c.sha, DEFAULT_WINDOW)
            if not anc:
                raise RuntimeError("HEAD has no ancestors; nothing to bisect")
            lg_sha, inv.window_source = anc[-1], f"default HEAD~{len(anc)} (unverified)"
    inv.last_good = repo.commit(lg_sha)
    inv.commits = repo.first_parent_range(lg_sha, head_c.sha)
    if len(inv.commits) < 2:
        raise RuntimeError("last-good and HEAD are the same commit")

    # ------------------------------------------------------------- relevance prior (AST pre-filter)
    test_path = inv.test_id.split("::")[0]
    files_head = repo.ls_files(head_c.sha)
    closure = import_closure([test_path] + conftests_for(test_path, files_head), files_head,
                             lambda p: repo.show_file(head_c.sha, p))
    inv.import_closure = sorted(closure)
    for i, c in enumerate(inv.commits[1:], 1):
        changed = [p for _, p in repo.changed_files(c.sha)]
        inv.relevance[i], inv.relevance_reason[i] = _relevance(changed, closure)
    rel_obs = ctx.evidence.record(
        "TRIAGE", "ast", "import closure of the failing test",
        f"{len(closure)} files reachable from {test_path}; "
        f"{sum(1 for w in inv.relevance.values() if w >= 1)} of {len(inv.commits) - 1} commits touch them",
        {"closure": inv.import_closure, "relevance": inv.relevance},
        "\n".join(inv.import_closure) + "\n\n" + "\n".join(
            f"c{i:02d} {inv.commits[i].short} w={w:.2f} {inv.relevance_reason[i]}" for i, w in inv.relevance.items()),
        kind="static")

    win_obs = ctx.evidence.record("TRIAGE", "git", "establish search window",
                                  f"{inv.last_good.short} → {head_c.short}: {len(inv.commits) - 1} commits "
                                  f"(last-good from {inv.window_source})",
                                  {"last_good": inv.last_good.sha, "head": head_c.sha, "n": len(inv.commits) - 1,
                                   "source": inv.window_source},
                                  "\n".join(f"c{i:02d} {c.short} {c.date} {c.author}: {c.subject}"
                                            for i, c in enumerate(inv.commits)), kind="api")
    bus.emit("strip.init", commits=[{"index": i, "sha": c.sha, "short": c.short, "subject": c.subject,
                                     "author": c.author, "date": c.date,
                                     "relevance": inv.relevance.get(i, 1.0),
                                     "why": inv.relevance_reason.get(i, "")} for i, c in enumerate(inv.commits)],
             last_good=inv.last_good.short, head=head_c.short, source=inv.window_source)
    bus.say("TRIAGE", "Search window established",
            f"last green {inv.last_good.short} → HEAD {head_c.short} · {len(inv.commits) - 1} commits · "
            f"source: {inv.window_source}", [win_obs.id, rel_obs.id])
    inv.claim("TRIAGE", f"The search window spans {len(inv.commits) - 1} first-parent commits from "
                        f"{inv.last_good.short} to {head_c.short}.", [win_obs.id],
              check={"type": "window", "n": len(inv.commits) - 1})
    return inv


def _relevance(changed: list[str], closure: set[str]) -> tuple[float, str]:
    if not changed:
        return 0.05, "empty commit"
    if any(p in closure for p in changed):
        return 1.0, "touches the test's import closure"
    if any(p.rsplit("/", 1)[-1] in MANIFEST_FILES for p in changed):
        return 1.0, "changes a dependency manifest"
    if any(p.rsplit("/", 1)[-1] == "conftest.py" for p in changed):
        return 1.0, "changes a conftest"
    if all(p.lower().endswith(DOC_SUFFIX) and "requirements" not in p for p in changed):
        return 0.08, "documentation only"
    if any(p.endswith(".py") for p in changed):
        return 0.35, "code outside the test's import closure"
    return 0.6, "data/config files"


def _parse_symptom(test_id: str, log: str) -> dict:
    sym: dict = {"test_id": test_id, "exception_type": "", "assertion": "", "observed": "", "expected": "",
                 "suspect_modules": [], "summary": "", "parsed_by": "regex"}
    lines = [l.strip() for l in (log or "").splitlines() if l.strip()]
    first = next((l for l in lines if "Error" in l or l.startswith(("E ", "assert"))), lines[0] if lines else "")
    first = first.removeprefix("E").strip()
    m = re.match(r"^(\w+(?:Error|Exception|Warning)):?\s*(.*)$", first)
    if m:
        sym["exception_type"], rest = m.group(1), m.group(2)
    else:
        rest = first
        if first.startswith("assert"):
            sym["exception_type"] = "AssertionError"
    sym["assertion"] = rest[:300]
    cm = _ASSERT_CMP.search(rest)
    if cm:
        sym["observed"], sym["expected"] = cm.group(1).strip(), cm.group(3).strip()
        on, en = _NUM.findall(sym["observed"]), _NUM.findall(sym["expected"])
        if len(on) == 1 and len(en) == 1:
            sym["delta"] = round(float(en[0]) - float(on[0]), 6)
    mods = []
    for path, _ in _TB_LINE.findall(log or ""):
        if "site-packages" in path or path.startswith("/usr"):
            continue
        path = path.removeprefix("/work/")
        if path not in mods:
            mods.append(path)
    sym["suspect_modules"] = mods[:6]
    if sym["observed"] and sym["expected"]:
        delta = f" (delta {sym['delta']:+g})" if "delta" in sym else ""
        sym["summary"] = f"{test_id} observed {sym['observed']} where {sym['expected']} was expected{delta}"
    else:
        sym["summary"] = f"{test_id} fails: {sym['assertion'] or first}"[:300]
    return sym
