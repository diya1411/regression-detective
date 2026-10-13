"""Flakiness agent: N runs at HEAD and N at last-good; also verifies both bisection endpoints."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..policy import Action
from ..sandbox import ProbeRequest
from ..state import ActionResult, Investigation
from .base import Ctx, probe_raw


def run(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    n = a.params.get("runs", ctx.settings.flaky_runs)
    head, lg = inv.head, inv.last_good
    with ThreadPoolExecutor(2) as pool:
        fh = pool.submit(ctx.sandbox.probe, ProbeRequest(head.sha, [inv.test_id], repeat=n, label="flaky@HEAD"))
        fl = pool.submit(ctx.sandbox.probe, ProbeRequest(lg.sha, [inv.test_id], repeat=n, label="flaky@last-good"))
        rh, rl = fh.result(), fl.result()
    for r in (rh, rl):
        if r.build and not r.build.cached:
            inv.cost.observe_build(r.build.seconds)
        if r.runs:
            inv.cost.observe_run(sum(x.duration for x in r.runs) / len(r.runs))

    eh = ctx.evidence.record("FLAKINESS", "sandbox", f"{n} runs at HEAD {head.short}",
                             f"HEAD: {rh.fails} failed / {rh.passes} passed of {n}",
                             {"sha": head.sha, "passes": rh.passes, "fails": rh.fails, "verdict": rh.verdict,
                              "skip_kind": rh.skip_kind, "signature": rh.signature}, probe_raw(rh))
    el = ctx.evidence.record("FLAKINESS", "sandbox", f"{n} runs at last-good {lg.short}",
                             f"last-good: {rl.passes} passed / {rl.fails} failed of {n}",
                             {"sha": lg.sha, "passes": rl.passes, "fails": rl.fails, "verdict": rl.verdict,
                              "skip_kind": rl.skip_kind, "signature": rl.signature}, probe_raw(rl))
    ev = [eh.id, el.id]

    def counted(r):
        return r.passes + r.fails

    if rh.verdict == "skip" or rh.fails == 0:
        outcome = "no_repro"
    elif rl.verdict == "skip" and rl.skip_kind in ("install_error", "collection_error"):
        outcome = "lg_unbuildable"
    elif rh.passes > 0 and rl.passes > 0 and rl.fails > 0:
        outcome = "both_intermittent"
    elif rh.passes > 0:
        outcome = "head_intermittent"
    elif rl.fails > 0 and rl.passes == 0:
        outcome = "lg_fails"
    elif rl.fails > 0:
        outcome = "both_intermittent"
    else:
        outcome = "stable"

    head_rate = rh.fails / counted(rh) if counted(rh) else None
    inv.flaky = {"outcome": outcome, "runs": n, "head_fail_rate": head_rate,
                 "lg_fail_rate": rl.fails / counted(rl) if counted(rl) else None,
                 "head": {"passes": rh.passes, "fails": rh.fails}, "lg": {"passes": rl.passes, "fails": rl.fails},
                 "head_signature": rh.signature, "lg_signature": rl.signature, "evidence": ev}
    inv.extra["head_signature"] = rh.signature

    inv.claim("FLAKINESS", f"At HEAD {head.short} the test failed {rh.fails} of {n} runs.", [eh.id],
              check={"type": "runs", "evidence": eh.id, "fails": rh.fails, "passes": rh.passes})
    inv.claim("FLAKINESS", f"At last-good {lg.short} the test passed {rl.passes} of {n} runs.", [el.id],
              check={"type": "runs", "evidence": el.id, "fails": rl.fails, "passes": rl.passes})

    msgs = {
        "stable": (f"{n} runs at last-good → <ok>{rl.passes} passed</ok> · HEAD {rh.fails}/{n} fail",
                   "deterministic · H2 eliminated by experiment", "ok", f"{rl.passes}/{n} pass|deterministic"),
        "both_intermittent": (f"intermittent at both commits: HEAD {rh.fails}/{n} fail, last-good {rl.fails}/{n} fail",
                              "nondeterministic before and after the window → not a regression", "no",
                              f"{rh.fails}/{n} · {rl.fails}/{n} fail|nondeterministic"),
        "head_intermittent": (f"HEAD intermittent: {rh.fails}/{n} fail; last-good {rl.passes}/{n} pass",
                              "nondeterministic at HEAD", "no", f"HEAD {rh.fails}/{n} fail|intermittent"),
        "lg_fails": (f"last-good also fails: <no>{rl.fails}/{n}</no> in today's sandbox",
                     "the window's good endpoint does not hold → drift or wrong window", "no",
                     f"last-good {rl.fails}/{n} fail|endpoint invalid"),
        "no_repro": (f"could not reproduce at HEAD: {rh.passes}/{n} pass" + (f" ({rh.skip_kind})" if rh.skip_kind else ""),
                     "failure is environment-specific or rare", "no", "no repro|at HEAD"),
        "lg_unbuildable": (f"last-good cannot be built today ({rl.skip_kind})", "dependency or environment drift",
                           "no", "last-good|unbuildable"),
    }
    title, detail, tone, label = msgs[outcome]
    return ActionResult(a, outcome, ev, title, tone, {"detail": detail, "tree_label": label})
