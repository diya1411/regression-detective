import math
from types import SimpleNamespace

from detective.belief import Belief, Hypothesis, entropy
from detective.config import Settings
from detective.policy import (CostModel, candidate_actions, confirm_outcomes, probe_outcomes, score_actions,
                              select, weighted_midpoint)
from detective.state import Investigation


def hyps(n_commits=16, weights=None):
    h1 = Hypothesis("H1", "commit", "Bad commit", "", .55, .55, good=0, bad=n_commits, weights=weights or {})
    return [h1,
            Hypothesis("H2", "flaky", "Flaky", "", .15, .15),
            Hypothesis("H3", "dependency", "Dep", "", .20, .20),
            Hypothesis("H4", "environment", "Env", "", .07, .07),
            Hypothesis("H5", "fixture", "Fixture", "", .03, .03)]


def inv_with(belief, n=16, flaky=None):
    inv = Investigation(id="t", repo_path=".", test_id="t::t")
    inv.tree = belief
    inv.commits = [SimpleNamespace(short=f"c{i:02d}", sha=f"{i:040d}") for i in range(n + 1)]
    inv.flaky = flaky or {}
    inv.sandbox_cache = lambda i: True
    inv.cost = CostModel(build_s=30, run_s=0.5)
    return inv


def test_entropy_basics():
    assert entropy([0.5, 0.5]) == 1.0
    assert entropy([1.0]) == 0.0
    b = Belief(hyps(16))
    # leaves = 16 commit leaves + 4 others
    assert len(b.leaves()) == 20
    assert abs(sum(p for _, p in b.leaves()) - 1) < 1e-9


def test_bisection_probe_halves_commit_uncertainty():
    b = Belief(hyps(16))
    inv = inv_with(b)
    h1 = b.h["H1"]
    m = weighted_midpoint(h1)
    assert m == 8
    eig = b.expected_info_gain(probe_outcomes(inv, h1, m))
    # roughly P(H1) bits: one binary split of the commit sub-distribution
    assert 0.45 < eig < 0.7
    b.update(probe_outcomes(inv, h1, m)["pass"])
    assert sum(w for i, w in h1.cand_weights().items() if i <= 8) < 0.05


def test_relevance_weights_move_the_midpoint():
    w = {i: (1.0 if i >= 12 else 0.1) for i in range(1, 17)}
    b = Belief(hyps(16, weights=w))
    assert weighted_midpoint(b.h["H1"]) > 8


def test_confirmation_is_required_and_decisive():
    from detective.policy import flaky_outcomes

    b = Belief(hyps(16))
    inv = inv_with(b)
    b.update(flaky_outcomes(inv)["stable"])        # endpoints verified first, as in a real run
    h1 = b.h["H1"]
    h1.good, h1.bad = 10, 11
    h1.weights = {11: 1.0}
    assert not Belief.confirmable(h1)
    b.update(confirm_outcomes(inv, h1, 11)["pass"])
    assert h1.p > 0.9
    first = b.refresh_status(0.9, "experiment")
    assert all(hid != "H1" for hid, _, _ in first)      # above threshold but untested: not confirmed
    h1.confirmed_by_experiment = True
    changes = b.refresh_status(0.9, "experiment")
    assert ("H1", "open", "confirmed") in changes


def test_flaky_check_runs_before_bisection():
    s = Settings().with_(offline=True)
    b = Belief(hyps(32))
    inv = inv_with(b, 32)
    acts = score_actions(inv, candidate_actions(inv, s))
    names = [a.name for a in acts]
    assert "bisect_probe" not in names, "bisection must wait for verified endpoints"
    assert select(acts, 2)[0].name == "flaky_check"
    inv.flaky = {"outcome": "stable", "head_fail_rate": 1.0}
    inv.mark("flaky_check")
    acts = score_actions(inv, candidate_actions(inv, s))
    assert acts[0].name == "bisect_probe"


def test_stable_outcome_favours_commit_and_kills_flaky():
    from detective.policy import flaky_outcomes

    b = Belief(hyps(16))
    inv = inv_with(b)
    b.update(flaky_outcomes(inv)["stable"])
    assert b.h["H1"].p > 0.8 and b.h["H2"].p < 0.03


def test_spawn_redistributes_mass():
    b = Belief(hyps(16))
    b.spawn(Hypothesis("H6", "window", "Window", "", .15, .15))
    assert abs(sum(h.p for h in b.h.values()) - 1) < 1e-9
    assert math.isclose(b.h["H6"].p, 0.15, rel_tol=1e-6)
