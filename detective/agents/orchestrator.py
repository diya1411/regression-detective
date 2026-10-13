"""Investigation Orchestrator: owns the hypothesis tree and the evidence budget.

It never touches a tool. Each iteration it (1) absorbs returned evidence into the belief with Bayes'
rule, spawning a new hypothesis when an observation is surprising under every existing one,
(2) checks termination, (3) scores every available action by expected information gain per
estimated second, and (4) dispatches the best action(s) to the owning specialists.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..belief import Hypothesis
from ..policy import Action, candidate_actions, score_actions, select
from ..state import ActionResult, Investigation
from . import bisection, dependency, environment, flakiness
from .base import Ctx

SURPRISE = 0.2
HANDLERS = {
    "flaky_check": flakiness.run,
    "bisect_probe": bisection.probe,
    "confirm_revert": bisection.confirm,
    "window_extend": bisection.window_extend,
    "dep_diff": dependency.diff,
    "dep_pin_test": dependency.pin_test,
    "env_check": environment.env_check,
    "fixture_scan": environment.fixture_scan,
}
EVIDENCE_KIND = {"fixture_scan": "static", "env_check": "api", "dep_diff": "api"}


def _ui(h: Hypothesis) -> dict:
    return {"id": h.id, "kind": h.kind, "title": h.title, "short": h.short, "p": round(h.p, 4),
            "status": h.status, "criteria": h.criteria, "rationale": h.rationale, "parent": h.parent}


# ------------------------------------------------------------------------------- absorb
def absorb(ctx: Ctx, inv: Investigation) -> None:
    b, bus = inv.tree, ctx.bus
    for res in inv.results:
        a = res.action
        h = b.h.get(a.hypothesis)
        label = res.data.get("tree_label", res.message[:30])
        if res.outcome is None:
            if h:
                h.evidence.append({"id": res.evidence[0] if res.evidence else None, "label": label, "tone": "dim"})
                bus.emit("tree.evidence", hypothesis=h.id, evidence=res.evidence, label=label, tone="dim")
            continue
        _maybe_spawn(ctx, inv, a, res)
        z = b.update(a.outcomes[res.outcome])
        _structural(inv, a, res)
        if h:
            tone = res.tone or ("ok" if res.outcome in ("pass", "stable", "none", "unchanged") else "no")
            if a.name in ("flaky_check", "dep_diff", "dep_pin_test", "env_check", "fixture_scan", "window_extend"):
                tone = "dim" if b.h[h.id].p < 0.1 else ("no" if h.kind in ("flaky", "dependency", "window") else tone)
            h.evidence.append({"id": res.evidence[0] if res.evidence else None, "label": label, "tone": tone,
                               "action": a.name, "outcome": res.outcome})
            bus.emit("tree.evidence", hypothesis=h.id, evidence=res.evidence, label=label, tone=tone)
        changes = b.refresh_status(ctx.settings.confidence_threshold, EVIDENCE_KIND.get(a.name, "experiment"))
        inv.decisions.append({"absorbed": a.name, "outcome": res.outcome, "p_outcome": round(z, 4),
                              "posterior": b.snapshot()})
        bus.emit("tree.update", hypotheses=b.snapshot(), entropy=round(b.entropy(), 3),
                 because={"action": a.name, "outcome": res.outcome, "p_outcome": round(z, 4)})
        for hid, old, new in changes:
            x = b.h[hid]
            if new in ("eliminated", "unreachable"):
                how = {"static": "by static analysis (unreachable, not tested)", "api": "by registry/CI evidence"}.get(
                    x.eliminated_by or "", "by experiment")
                bus.emit("tree.status", hypothesis=hid, status=new, how=how)
            elif new == "confirmed":
                bus.emit("tree.status", hypothesis=hid, status=new, how="confirmed by experiment"
                         if x.confirmed_by_experiment else "above threshold")
        _emit_range(ctx, inv)
    inv.results = []
    open_n = sum(1 for x in b.h.values() if x.status == "open")
    elim_n = sum(1 for x in b.h.values() if x.status in ("eliminated", "unreachable"))
    conf_n = sum(1 for x in b.h.values() if x.status == "confirmed")
    bus.emit("tree.tag", text=" · ".join(filter(None, [f"{conf_n} confirmed" if conf_n else "",
                                                     f"{open_n} open" if open_n else "",
                                                     f"{elim_n} eliminated" if elim_n else ""])))


def _maybe_spawn(ctx: Ctx, inv: Investigation, a: Action, res: ActionResult) -> None:
    b = inv.tree
    z, _, _ = b.posterior(a.outcomes[res.outcome])
    kinds = {h.kind for h in b.h.values()}
    new = None
    if a.name == "flaky_check" and res.outcome in ("lg_fails", "lg_unbuildable") and "window" not in kinds:
        new = Hypothesis(id=_next_id(inv), kind="window", title="Regression predates the window",
                         short="Window invalid|(predates last-good)", prior=0.15, p=0.15,
                         criteria="confirm: an older ancestor of last-good passes · eliminate: dependency or "
                                  "environment drift explains the last-good failure",
                         rationale=f"last-good fails in the sandbox (P(outcome)={z:.2f} under existing hypotheses)")
    elif a.name == "confirm_revert" and res.outcome == "fail" and "second_defect" not in kinds:
        h = b.h[a.hypothesis]
        c = a.params["index"]
        prev = inv.verdicts.get(c - 1)
        endpoint_ok = (c - 1 == 0 and inv.flaky.get("outcome") == "stable") or (prev and prev["verdict"] == "pass")
        if h.kind == "commit" and h.candidates() == [c] and endpoint_ok and c < inv.head_idx:
            new = Hypothesis(id=_next_id(inv), kind="second_defect", title=f"Second defect after {inv.commits[c].short}",
                             short=f"2nd defect after|{inv.commits[c].short}", prior=0.3, p=0.3, parent=h.id,
                             criteria=f"confirm: reverting {inv.commits[c].short} and a second commit at HEAD passes",
                             rationale="reverting the isolated culprit alone does not fix HEAD",
                             good=c, bad=inv.head_idx, reverts=[c],
                             weights={i: inv.relevance.get(i, 1.0) for i in range(c + 1, inv.head_idx + 1)})
    if new is None:
        return
    # The observation contradicts an assumption the existing tree was built on (last-good passes /
    # a single commit explains the failure), so the tree is expanded rather than forced to fit.
    b.spawn(new)
    new.prior = new.p
    ctx.bus.emit("tree.spawn", hypothesis=_ui(new), because={"action": a.name, "outcome": res.outcome,
                                                             "p_outcome": round(z, 3)})
    why = "surprising under every open hypothesis" if z < SURPRISE else "contradicts an assumption of the tree"
    ctx.bus.say("HYPOTHESIS", f"New hypothesis {new.id}: {new.title}",
                f"observation {why} (P={z:.2f}) → tree expanded", res.evidence)


def _next_id(inv: Investigation) -> str:
    return f"H{len(inv.tree.h) + 1}"


def _structural(inv: Investigation, a: Action, res: ActionResult) -> None:
    b = inv.tree
    h = b.h.get(a.hypothesis)
    if a.name == "bisect_probe" and h:
        m = a.params["index"]
        if res.outcome == "pass":
            h.good = max(h.good, m)
        elif res.outcome == "fail":
            h.bad = min(h.bad, m)
        h.weights = {i: w for i, w in h.weights.items() if h.good < i <= h.bad}
    elif a.name == "confirm_revert" and h and res.outcome == "pass":
        c = a.params["index"]
        h.confirmed_by_experiment = True
        h.good, h.bad = c - 1, c
        h.weights = {c: 1.0}
    elif a.name == "dep_pin_test" and h and res.outcome == "pass":
        h.confirmed_by_experiment = True
    elif a.name == "flaky_check" and res.outcome in ("both_intermittent", "head_intermittent"):
        for x in b.h.values():
            if x.kind == "flaky":
                x.confirmed_by_experiment = True
    elif a.name == "window_extend" and h and res.outcome == "pass":
        h.confirmed_by_experiment = True


def _emit_range(ctx: Ctx, inv: Investigation) -> None:
    for h in inv.tree.h.values():
        if h.is_commit and h.good is not None:
            ctx.bus.emit("strip.range", lo=h.good + 1, hi=h.bad, track=h.id, remaining=len(h.candidates()),
                         skipped=sorted(h.skipped))


# ------------------------------------------------------------------------------- decide
def decide(ctx: Ctx, inv: Investigation) -> None:
    s, b, bus = ctx.settings, inv.tree, ctx.bus
    inv.iteration += 1
    bus.emit("budget", probes=inv.probe_count, max_probes=s.budget.max_probes, elapsed=round(inv.elapsed(), 1),
             max_seconds=s.budget.max_seconds, llm_calls=ctx.llm.calls, max_llm_calls=s.budget.max_llm_calls)
    top = b.top()
    confirmed = [h for h in b.h.values() if h.status == "confirmed"]
    if confirmed:
        return conclude(ctx, inv, max(confirmed, key=lambda h: h.p), "confirmed")
    if inv.elapsed() > s.budget.max_seconds:
        return conclude(ctx, inv, top, "budget_exhausted", "wall-clock budget exhausted")

    scored = score_actions(inv, candidate_actions(inv, s))
    chosen = select(scored, s.max_parallel)
    if not chosen:
        if inv.probe_count >= s.budget.max_probes:
            return conclude(ctx, inv, top, "budget_exhausted", f"probe budget of {s.budget.max_probes} exhausted")
        return conclude(ctx, inv, top, "no_action", "no remaining action can change the conclusion")
    inv.pending = chosen
    dec = {"iteration": inv.iteration, "entropy_bits": round(b.entropy(), 3),
           "chosen": [a.summary() for a in chosen], "candidates": [a.summary() for a in scored[:8]]}
    inv.decisions.append(dec)
    bus.emit("orchestrator.decision", **dec)
    if inv.iteration == 1 or len(chosen) > 1 or chosen[0].name not in ("bisect_probe",):
        best = chosen[0]
        alt = next((x for x in scored if x.name != best.name), None)
        why = (f"{best.label}: {best.eig:.2f} bits / ~{best.cost:.0f}s" +
               (f" vs {alt.label}: {alt.eig:.2f} bits / ~{alt.cost:.0f}s" if alt else ""))
        title = ("Selecting next action by <hl>information gain ÷ cost</hl>" if inv.iteration == 1 else
                 "Dispatching " + " and ".join(dict.fromkeys(x.agent.lower() for x in chosen))
                 + (" checks" if len({x.agent for x in chosen}) < len(chosen) else "")
                 + (" in parallel" if len(chosen) > 1 else ""))
        bus.say("ORCHESTRATOR", title, why + f" → {best.name.replace('_', ' ')}")
    bus.emit("tree.active", ids=sorted({a.hypothesis for a in chosen}))


def dispatch(ctx: Ctx, inv: Investigation) -> None:
    acts, inv.pending = inv.pending, []
    for a in acts:
        key = f"{a.hypothesis}:{a.params['index']}" if "index" in a.params else None
        inv.mark(a.name, key)
    if len(acts) == 1:
        results = [_run(ctx, inv, acts[0])]
    else:
        with ThreadPoolExecutor(len(acts)) as pool:
            results = list(pool.map(lambda a: _run(ctx, inv, a), acts))
    for r in results:
        ctx.bus.say(r.action.agent, r.message, r.data.get("detail", ""), r.evidence, r.tone)
    inv.results.extend(results)
    ctx.bus.emit("tree.active", ids=[])


def _run(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    try:
        return HANDLERS[a.name](ctx, inv, a)
    except Exception as e:  # a failing specialist yields an uninformative result, not a crash
        obs = ctx.evidence.record(a.agent, "error", a.name, f"{a.name} failed: {e!r}"[:300], {"error": repr(e)},
                                  repr(e), kind="derived")
        return ActionResult(a, None, [obs.id], f"{a.name} failed: {type(e).__name__}", "dim",
                            {"detail": str(e)[:200], "tree_label": "error"})


# ------------------------------------------------------------------------------- conclude
def conclude(ctx: Ctx, inv: Investigation, top: Hypothesis, how: str, note: str = "") -> None:
    b = inv.tree
    c: dict = {"hypothesis": top.id, "kind": top.kind, "p": round(top.p, 4), "how": how, "note": note,
               "confirmed": top.status == "confirmed", "culprits": [], "candidates": []}
    if top.kind in ("commit", "second_defect"):
        if top.confirmed_by_experiment:
            c["culprits"] = sorted(set(top.reverts + top.candidates()))
        c["candidates"] = top.candidates()
        c["range"] = [top.good, top.bad]
        c["skipped"] = sorted(top.skipped)
    status = {"commit": "resolved", "second_defect": "multi_cause", "flaky": "flaky", "dependency": "dependency",
              "window": "window_invalid", "environment": "environment", "fixture": "fixture"}[top.kind]
    if not c["confirmed"]:
        if how == "budget_exhausted":
            status = "budget_exhausted"
        elif top.p < ctx.settings.confidence_threshold or not b.confirmable(top):
            status = "inconclusive"
        if top.kind == "window" and top.p >= 0.75:
            status = "window_invalid"
    c["status"] = status
    if top.kind == "dependency":
        c["packages"] = inv.dep.get("culprit_packages") or list(inv.dep.get("suspects", {}))
    if top.kind == "window":
        c["older_good"] = inv.extra.get("older_good")
    if inv.extra.get("confirm_unavailable"):
        c["confirm_unavailable"] = inv.extra["confirm_unavailable"]
    inv.conclusion = c
    ctx.bus.emit("conclusion", **c, culprit_shas=[inv.commits[i].sha for i in c["culprits"]])
    if c["culprits"]:
        ctx.bus.emit("strip.culprit", indices=c["culprits"], track=top.id)
    names = ", ".join(inv.commits[i].short for i in c["culprits"])
    text = {"resolved": f"culprit isolated: <hl>{names}</hl>",
            "multi_cause": f"multiple causes: <hl>{names}</hl>",
            "flaky": "test is flaky — no regression to bisect",
            "dependency": f"dependency regression: {', '.join(c.get('packages', []))}",
            "window_invalid": "search window invalid — regression predates last-good",
            "budget_exhausted": f"budget exhausted · narrowed to {len(c['candidates'])} commit(s)",
            "inconclusive": f"inconclusive · leading hypothesis {top.id} at p={top.p:.2f}",
            "environment": "CI environment change", "fixture": "external fixture change"}[status]
    ctx.bus.say("ORCHESTRATOR", "Search converged" if status not in ("budget_exhausted", "inconclusive")
                else "Search stopped", text + (f" · {note}" if note else ""))
