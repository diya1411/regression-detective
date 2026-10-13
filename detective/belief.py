"""The hypothesis tree as a probability distribution.

Leaves of the distribution are either a non-commit hypothesis (flaky, dependency, ...) or a
(commit-hypothesis, candidate commit) pair, so the entropy the orchestrator minimises covers both
"which kind of cause" and "which commit". Bisection, flakiness runs, pin tests and static checks
are all just observations with likelihood models over these leaves.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterator, Optional

COMMIT_KINDS = ("commit", "second_defect")
ELIMINATE_BELOW = 0.02

# Kind -> what must be *tested* before the hypothesis may be reported as confirmed.
CONFIRMATION_RULE = {
    "commit": "reverting the culprit at HEAD makes the test pass",
    "second_defect": "reverting both culprits at HEAD makes the test pass",
    "dependency": "pinning the suspect package(s) back makes the test pass at unchanged code",
    "flaky": "repeated runs show nondeterministic outcomes",
    "window": "an older ancestor of last-good passes, so the regression predates the window",
    "environment": None,
    "fixture": None,
}


@dataclass
class Hypothesis:
    id: str
    kind: str
    title: str
    short: str
    prior: float
    p: float
    status: str = "open"          # open | eliminated | unreachable | confirmed
    criteria: str = ""
    rationale: str = ""
    eliminated_by: Optional[str] = None   # experiment | static | api | retrieval
    confirmed_by_experiment: bool = False
    parent: Optional[str] = None
    evidence: list[dict] = field(default_factory=list)
    # commit-kind only: candidates are indices in (good, bad]
    good: Optional[int] = None
    bad: Optional[int] = None
    weights: dict[int, float] = field(default_factory=dict)
    skipped: set[int] = field(default_factory=set)
    reverts: list[int] = field(default_factory=list)   # culprits reverted while probing (second defect)

    @property
    def is_commit(self) -> bool:
        return self.kind in COMMIT_KINDS

    def candidates(self) -> list[int]:
        if not self.is_commit or self.good is None or self.bad is None:
            return []
        return list(range(self.good + 1, self.bad + 1))

    def cand_weights(self) -> dict[int, float]:
        c = self.candidates()
        w = {i: self.weights.get(i, 1.0) for i in c}
        tot = sum(w.values()) or 1.0
        return {i: v / tot for i, v in w.items()}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["skipped"] = sorted(self.skipped)
        d["weights"] = {str(k): v for k, v in self.weights.items() if self.good is not None
                        and self.good < k <= (self.bad or k)}
        d["candidates"] = self.candidates()
        return d


Leaf = tuple[str, Optional[int]]
Likelihood = Callable[[Hypothesis, Optional[int]], float]


def entropy(ps: Iterator[float] | list[float]) -> float:
    return -sum(p * math.log2(p) for p in ps if p > 0)


class Belief:
    def __init__(self, hyps: list[Hypothesis]):
        self.h: dict[str, Hypothesis] = {x.id: x for x in hyps}
        self.normalize()

    # ----------------------------------------------------------------- basics
    def normalize(self) -> None:
        tot = sum(x.p for x in self.h.values()) or 1.0
        for x in self.h.values():
            x.p = x.p / tot

    def leaves(self) -> list[tuple[Leaf, float]]:
        out: list[tuple[Leaf, float]] = []
        for x in self.h.values():
            if x.is_commit and x.candidates():
                for i, w in x.cand_weights().items():
                    out.append(((x.id, i), x.p * w))
            else:
                out.append(((x.id, None), x.p))
        return out

    def entropy(self) -> float:
        return entropy([p for _, p in self.leaves()])

    def top(self) -> Hypothesis:
        return max(self.h.values(), key=lambda x: x.p)

    def snapshot(self) -> list[dict]:
        return [{"id": x.id, "p": round(x.p, 4), "status": x.status} for x in self.h.values()]

    # ----------------------------------------------------------------- Bayes
    def posterior(self, lik: Likelihood) -> tuple[float, dict[str, float], dict[str, dict[int, float]]]:
        """Return (P(o), new hypothesis probs, new candidate weights) without mutating."""
        new_p: dict[str, float] = {}
        new_w: dict[str, dict[int, float]] = {}
        for x in self.h.values():
            if x.is_commit and x.candidates():
                cw = x.cand_weights()
                contrib = {i: w * lik(x, i) for i, w in cw.items()}
                m = sum(contrib.values())
                new_p[x.id] = x.p * m
                new_w[x.id] = {i: (v / m if m else cw[i]) for i, v in contrib.items()}
            else:
                new_p[x.id] = x.p * lik(x, None)
        z = sum(new_p.values())
        if z <= 0:
            return 0.0, {k: v.p for k, v in self.h.items()}, {}
        return z, {k: v / z for k, v in new_p.items()}, new_w

    def update(self, lik: Likelihood) -> float:
        z, new_p, new_w = self.posterior(lik)
        if z <= 0:
            return 0.0
        for k, v in new_p.items():
            self.h[k].p = v
        for k, ws in new_w.items():
            # keep weights as relative values over the remaining candidates
            for i, v in ws.items():
                self.h[k].weights[i] = v
        return z

    def expected_info_gain(self, outcomes: dict[str, Likelihood]) -> float:
        h0 = self.entropy()
        exp_h = 0.0
        total = 0.0
        for lik in outcomes.values():
            z, new_p, new_w = self.posterior(lik)
            if z <= 0:
                continue
            total += z
            ps = []
            for x in self.h.values():
                if x.id in new_w and new_w[x.id]:
                    ps.extend(new_p[x.id] * w for w in new_w[x.id].values())
                else:
                    ps.append(new_p[x.id])
            exp_h += z * entropy(ps)
        if total <= 0:
            return 0.0
        return max(0.0, h0 - exp_h / total)

    # ----------------------------------------------------------------- structure
    def spawn(self, hyp: Hypothesis) -> None:
        for x in self.h.values():
            x.p *= (1 - hyp.p)
        self.h[hyp.id] = hyp
        self.normalize()

    def refresh_status(self, threshold: float, by_kind: str) -> list[tuple[str, str, str]]:
        """Apply elimination/confirmation; return [(id, old, new)] status changes."""
        changes = []
        for x in self.h.values():
            old = x.status
            if x.status in ("open",) and x.p < ELIMINATE_BELOW:
                x.status = "unreachable" if by_kind == "static" else "eliminated"
                x.eliminated_by = by_kind
            elif x.status in ("eliminated", "unreachable") and x.p >= 0.1:
                x.status = "open"          # evidence can revive a hypothesis
                x.eliminated_by = None
            if x.status == "open" and x.p >= threshold and self.confirmable(x):
                x.status = "confirmed"
            if x.status != old:
                changes.append((x.id, old, x.status))
        return changes

    @staticmethod
    def confirmable(x: Hypothesis) -> bool:
        rule = CONFIRMATION_RULE.get(x.kind)
        return rule is None or x.confirmed_by_experiment
