"""The orchestrator's next-action policy: expected information gain per estimated second.

Every action carries a likelihood model P(outcome | leaf). The policy scores each available
action by EIG / cost and dispatches the best one (plus a second, independent one in parallel).
This is why a cheap flakiness check usually runs before an expensive bisection even when a bad
commit is the more likely explanation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .belief import Belief, Hypothesis, Likelihood

EPS = 0.01  # probability that a single deterministic probe verdict is wrong

# P(flakiness-check outcome | hypothesis kind)
FLAKY_TABLE: dict[str, dict[str, float]] = {
    #               stable head_int both_int lg_fails no_repro lg_unbuildable
    "commit":        dict(stable=.93, head_intermittent=.03, both_intermittent=.005, lg_fails=.02, no_repro=.01, lg_unbuildable=.005),
    "second_defect": dict(stable=.93, head_intermittent=.03, both_intermittent=.005, lg_fails=.02, no_repro=.01, lg_unbuildable=.005),
    "flaky":         dict(stable=.10, head_intermittent=.25, both_intermittent=.55, lg_fails=.02, no_repro=.075, lg_unbuildable=.005),
    "dependency":    dict(stable=.30, head_intermittent=.03, both_intermittent=.01, lg_fails=.55, no_repro=.06, lg_unbuildable=.05),
    "environment":   dict(stable=.10, head_intermittent=.04, both_intermittent=.02, lg_fails=.40, no_repro=.34, lg_unbuildable=.10),
    "fixture":       dict(stable=.10, head_intermittent=.10, both_intermittent=.05, lg_fails=.55, no_repro=.18, lg_unbuildable=.02),
    "window":        dict(stable=.01, head_intermittent=.01, both_intermittent=.01, lg_fails=.95, no_repro=.01, lg_unbuildable=.01),
}

# P(test fails at an arbitrary commit in the window | non-commit hypothesis)
FAIL_ANYWHERE = {"flaky": None, "dependency": .6, "environment": .5, "fixture": .5, "window": .9}
# P(test passes after the confirmation experiment | non-commit hypothesis)
CONFIRM_PASS = {"flaky": None, "dependency": .05, "environment": .05, "fixture": .05, "window": .05}
PIN_PASS = {"commit": .03, "second_defect": .03, "flaky": None, "dependency": .90, "environment": .10,
            "fixture": .05, "window": .03}
# An older commit passing in the *same* sandbox means the code differs, not the environment/registry/data:
# drift hypotheses predict that every commit fails alike.
EXTEND_PASS = {"commit": .9, "second_defect": .9, "flaky": None, "dependency": .04, "environment": .04,
               "fixture": .04, "window": .70}


@dataclass
class CostModel:
    """Seconds. Starts from priors and is recalibrated with measured probe/build times."""

    build_s: float = 45.0
    start_s: float = 1.5
    run_s: float = 2.0
    api_s: float = 3.0
    static_s: float = 2.0
    overhead_s: float = 2.0
    samples: dict = field(default_factory=lambda: {"build": 0, "run": 0})

    def observe_build(self, seconds: float) -> None:
        self.build_s = _ema(self.build_s, seconds, self.samples["build"])
        self.samples["build"] += 1

    def observe_run(self, per_run_seconds: float) -> None:
        self.run_s = _ema(self.run_s, per_run_seconds, self.samples["run"])
        self.samples["run"] += 1


def _ema(old: float, new: float, n: int) -> float:
    return new if n == 0 else 0.6 * old + 0.4 * new


@dataclass
class Action:
    name: str
    hypothesis: str
    agent: str
    label: str
    cost: float
    outcomes: dict[str, Likelihood]
    params: dict = field(default_factory=dict)
    eig: float = 0.0
    score: float = 0.0

    def summary(self) -> dict:
        return {"name": self.name, "hypothesis": self.hypothesis, "agent": self.agent, "label": self.label,
                "eig_bits": round(self.eig, 3), "cost_s": round(self.cost, 1),
                "score": round(self.score, 4), "params": {k: v for k, v in self.params.items()
                                                          if isinstance(v, (int, str, float, list))}}


# --------------------------------------------------------------------------- likelihood builders
def _flake_rate(inv) -> float:
    r = inv.flaky.get("head_fail_rate") if inv.flaky else None
    return 0.5 if r is None or r in (0.0, 1.0) else r


def flaky_outcomes(inv) -> dict[str, Likelihood]:
    keys = FLAKY_TABLE["commit"].keys()
    return {o: (lambda h, i, o=o: FLAKY_TABLE[h.kind][o]) for o in keys}


def probe_outcomes(inv, target: Hypothesis, m: int) -> dict[str, Likelihood]:
    f = _flake_rate(inv)

    def fail(h: Hypothesis, i: Optional[int]) -> float:
        if h.is_commit and i is not None:
            if h.id == target.id:
                return 1 - EPS if i <= m else EPS
            if target.reverts and h.kind == "commit":   # single-defect H1 predicts the revert fixed it
                return EPS
            return 0.5
        base = FAIL_ANYWHERE.get(h.kind)
        return f if base is None else base

    return {"fail": fail, "pass": lambda h, i: 1 - fail(h, i)}


def confirm_outcomes(inv, target: Hypothesis, c: int) -> dict[str, Likelihood]:
    f = _flake_rate(inv)

    def passes(h: Hypothesis, i: Optional[int]) -> float:
        if h.is_commit and i is not None:
            if h.id == target.id:
                return 0.95 if i == c else 0.08
            if h.kind == "second_defect" and target.kind == "commit":
                return 0.05   # a second defect would survive reverting the first culprit
            if h.kind == "commit" and target.kind == "second_defect":
                return 0.9    # single-defect: reverting c1 (included) already fixes it
            return 0.08
        base = CONFIRM_PASS.get(h.kind)
        return (1 - f) if base is None else base

    return {"pass": passes, "fail": lambda h, i: 1 - passes(h, i)}


def table_outcomes(inv, table: dict[str, float]) -> dict[str, Likelihood]:
    f = _flake_rate(inv)

    def passes(h: Hypothesis, i: Optional[int]) -> float:
        v = table.get(h.kind, 0.1)
        return (1 - f) if v is None else v

    return {"pass": passes, "fail": lambda h, i: 1 - passes(h, i)}


def binary_outcomes(target_kind: str, yes: str, no: str, p_yes_target: float, p_yes_other: float):
    def y(h: Hypothesis, i):
        return p_yes_target if h.kind == target_kind else p_yes_other

    return {yes: y, no: lambda h, i: 1 - y(h, i)}


# --------------------------------------------------------------------------- candidate generation
def weighted_midpoint(h: Hypothesis) -> Optional[int]:
    """Probe point splitting the candidate weight in half; skips unbuildable commits."""
    cw = h.cand_weights()
    probeable = [i for i in range(h.good + 1, h.bad) if i not in h.skipped]
    if not probeable:
        return None
    best, best_gap = None, 2.0
    for m in probeable:
        below = sum(w for i, w in cw.items() if i <= m)
        gap = abs(below - 0.5)
        if gap < best_gap - 1e-9:
            best, best_gap = m, gap
    return best


def candidate_actions(inv, settings) -> list[Action]:
    b: Belief = inv.tree
    cm: CostModel = inv.cost
    sb = inv.sandbox_cache
    acts: list[Action] = []
    hyp = {h.kind: h for h in b.h.values()}

    def build_cost(idx: int) -> float:
        return 0.0 if sb(idx) else cm.build_s

    n = settings.flaky_runs
    if not inv.done("flaky_check"):
        cost = build_cost(0) + build_cost(len(inv.commits) - 1) + 2 * (cm.start_s + n * cm.run_s) + cm.overhead_s
        acts.append(Action("flaky_check", hyp["flaky"].id if "flaky" in hyp else "H2", "FLAKINESS",
                           f"run the test {n}x at HEAD and {n}x at last-good", cost, flaky_outcomes(inv),
                           {"runs": n}))

    for h in b.h.values():
        if not h.is_commit or h.status != "open" or h.p < 0.02:
            continue
        cands = h.candidates()
        # Bisection is only valid if its assumptions hold: the good endpoint passes and the test is
        # deterministic. Both are established by the flakiness check, so probes wait for it.
        if len(cands) > 1 and (h.kind != "commit" or inv.flaky.get("outcome") == "stable"):
            m = weighted_midpoint(h)
            if m is not None and inv.probe_count < settings.budget.max_probes:
                c = inv.commits[m]
                acts.append(Action("bisect_probe", h.id, "BISECTION", f"probe {c.short} (c{m:02d})",
                                   build_cost(m) + cm.start_s + cm.run_s + cm.overhead_s,
                                   probe_outcomes(inv, h, m), {"index": m}))
        if not h.confirmed_by_experiment and 0 < len(cands) <= 3 and inv.probe_count < settings.budget.max_probes:
            for c in cands:
                if inv.done("confirm_revert", f"{h.id}:{c}"):
                    continue
                acts.append(Action("confirm_revert", h.id, "BISECTION",
                                   f"revert {inv.commits[c].short} at HEAD (counterfactual)",
                                   build_cost(len(inv.commits) - 1) + cm.start_s + cm.run_s + cm.overhead_s,
                                   confirm_outcomes(inv, h, c), {"index": c}))

    if "dependency" in hyp and hyp["dependency"].status == "open":
        dep = hyp["dependency"]
        if not inv.done("dep_diff"):
            acts.append(Action("dep_diff", dep.id, "DEPENDENCY", "diff manifests + registry releases in window",
                               cm.api_s + cm.overhead_s, binary_outcomes("dependency", "suspects", "none", .95, .35)))
        elif inv.dep.get("suspects") and not inv.done("dep_pin_test") and inv.probe_count < settings.budget.max_probes:
            acts.append(Action("dep_pin_test", dep.id, "DEPENDENCY",
                               f"pin {', '.join(sorted(inv.dep['suspects']))[:60]} back, re-run at HEAD",
                               cm.build_s + cm.start_s + cm.run_s + cm.overhead_s, table_outcomes(inv, PIN_PASS)))

    if "environment" in hyp and hyp["environment"].status == "open" and not inv.done("env_check"):
        acts.append(Action("env_check", hyp["environment"].id, "ENVIRONMENT", "compare CI environment across window",
                           cm.static_s + cm.overhead_s, binary_outcomes("environment", "changed", "unchanged", .85, .15)))
    if "fixture" in hyp and hyp["fixture"].status == "open" and not inv.done("fixture_scan"):
        acts.append(Action("fixture_scan", hyp["fixture"].id, "ENVIRONMENT", "scan test path for external data",
                           cm.static_s + cm.overhead_s, binary_outcomes("fixture", "external", "none", .95, .40)))

    if "window" in hyp and hyp["window"].status == "open" and not hyp["window"].confirmed_by_experiment:
        k = inv.count("window_extend") + 1
        if k <= 6 and inv.probe_count < settings.budget.max_probes:
            dist = 2 ** k
            acts.append(Action("window_extend", hyp["window"].id, "BISECTION", f"probe last-good~{dist}",
                               cm.build_s * 0.5 + cm.start_s + cm.run_s + cm.overhead_s,
                               table_outcomes(inv, EXTEND_PASS), {"distance": dist}))
    return acts


def score_actions(inv, acts: list[Action]) -> list[Action]:
    for a in acts:
        a.eig = inv.tree.expected_info_gain(a.outcomes)
        a.score = a.eig / max(a.cost, 0.5)
    return sorted(acts, key=lambda a: -a.score)


def select(scored: list[Action], max_parallel: int) -> list[Action]:
    chosen: list[Action] = []
    for a in scored:
        if a.eig < 1e-3:
            continue
        if not chosen:
            chosen.append(a)
            continue
        if len(chosen) >= max_parallel:
            break
        # parallel slot: a different hypothesis, and never two sandbox-mutating probes on one hypothesis
        if all(a.hypothesis != c.hypothesis for c in chosen) and a.score >= 0.2 * chosen[0].score:
            chosen.append(a)
    return chosen
