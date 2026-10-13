"""Bisection agent: the state-changing core. Checkout → install → run, one fresh container per probe.

Handles the awkward cases from the design:
  * a commit whose dependencies fail to install is retried with the nearest buildable ancestor's
    dependency set ("repair probe"); if that also fails it is marked skip and never counted as a fail
  * collection errors (syntax/import errors) are skips, not fails
  * merge commits are traversed on the first-parent chain
It also runs the counterfactual confirmation (revert the culprit at HEAD) and window extension.
"""
from __future__ import annotations

from ..policy import Action
from ..sandbox import ProbeRequest
from ..state import ActionResult, Investigation
from .base import Ctx, probe_raw


def _reverts(ctx: Ctx, inv: Investigation, idxs: list[int]) -> list[tuple[str, bytes, bool]]:
    # newest first so later reverts don't conflict with earlier ones
    return [(f"revert {inv.commits[i].short}", ctx.repo.commit_patch(inv.commits[i].sha), True)
            for i in sorted(idxs, reverse=True)]


def _account(inv: Investigation, r) -> None:
    inv.probe_count += 1
    if r.build and not r.build.cached:
        inv.cost.observe_build(r.build.seconds)
    if r.runs:
        inv.cost.observe_run(sum(x.duration for x in r.runs) / len(r.runs))


def probe(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    h = inv.tree.h[a.hypothesis]
    m = a.params["index"]
    c = inv.commits[m]
    n_probe = sum(1 for p in inv.probes if p["track"] == h.id) + 1
    ctx.bus.emit("strip.probe", index=m, track=h.id, label=f"probe {n_probe} — checkout {c.short}, install, run test"
                 + (f" (with {', '.join(inv.commits[i].short for i in h.reverts)} reverted)" if h.reverts else ""))
    patches = _reverts(ctx, inv, h.reverts)
    r = ctx.sandbox.probe(ProbeRequest(c.sha, [inv.test_id], patches=patches, label=f"bisect c{m:02d}"))
    _account(inv, r)
    repaired = None
    if r.verdict == "skip" and r.skip_kind == "install_error":
        donor = _nearest_buildable(inv, m)
        if donor is not None:
            r2 = ctx.sandbox.probe(ProbeRequest(c.sha, [inv.test_id], patches=patches,
                                                deps_from=inv.commits[donor].sha, label=f"repair c{m:02d}"))
            _account(inv, r2)
            ctx.evidence.record("BISECTION", "sandbox", f"install failed at {c.short}",
                                f"dependency install failed at {c.short}; retrying with c{donor:02d}'s dependency set",
                                {"sha": c.sha, "verdict": "skip", "skip_kind": r.skip_kind}, probe_raw(r))
            if r2.verdict != "skip":
                repaired, r = inv.commits[donor].short, r2

    data = {"sha": c.sha, "index": m, "verdict": r.verdict, "skip_kind": r.skip_kind, "signature": r.signature,
            "repaired_with_deps_of": repaired, "reverted": [inv.commits[i].sha for i in h.reverts],
            "hypothesis": h.id}
    obs = ctx.evidence.record("BISECTION", "sandbox", f"probe {c.short} (c{m:02d})",
                              f"{c.short}: {r.verdict.upper()}" + (f" ({r.skip_kind})" if r.skip_kind else "")
                              + (f" [deps repaired from {repaired}]" if repaired else ""), data, probe_raw(r))
    rec = {"track": h.id, "index": m, "sha": c.sha, "short": c.short, "verdict": r.verdict, "skip_kind": r.skip_kind,
           "signature": r.signature, "evidence": obs.id, "repaired": repaired, "seconds": round(r.seconds, 1)}
    inv.probes.append(rec)
    if not h.reverts:
        inv.verdicts[m] = rec
    ctx.bus.emit("strip.verdict", index=m, verdict=r.verdict, track=h.id, repaired=bool(repaired),
                 skip_kind=r.skip_kind, evidence=obs.id)

    if r.verdict == "skip":
        h.skipped.add(m)
        return ActionResult(a, None, [obs.id], f"probe {c.short} → <dim>SKIP</dim> ({r.skip_kind})", "dim",
                            {"detail": "unbuildable commits are uninformative; an adjacent commit is tried next",
                             "tree_label": f"{c.short} skip|{r.skip_kind}"})

    passed = r.verdict == "pass"
    remaining = (h.bad - m) if passed else (m - h.good)
    what = "with " + ", ".join(inv.commits[i].short for i in h.reverts) + " reverted " if h.reverts else ""
    inv.claim("BISECTION", f"At {c.short} {what}the test {'passed' if passed else 'failed'}.", [obs.id],
              check={"type": "probe", "evidence": obs.id, "sha": c.sha, "verdict": r.verdict})
    detail = ("lower half eliminated" if passed else "upper half eliminated") + \
        (f" · {remaining} commits remain" if remaining != 1 else " · 1 commit remains")
    head_sig = inv.extra.get("head_signature")
    if not passed and head_sig and r.signature and r.signature != head_sig and not h.reverts:
        detail += f" · signature differs from HEAD ({r.signature[:60]})"
        inv.extra.setdefault("signature_mismatch", []).append({"index": m, "signature": r.signature})
    tone = "ok" if passed else "no"
    return ActionResult(a, "pass" if passed else "fail", [obs.id],
                        f"probe {c.short} → <{tone}>{'PASS' if passed else 'FAIL'}</{tone}>"
                        + (f" (deps from {repaired})" if repaired else ""), tone,
                        {"detail": detail, "tree_label": f"{c.short} {'pass' if passed else 'FAIL'}|{remaining} left"})


def _nearest_buildable(inv: Investigation, m: int) -> int | None:
    for i in range(m - 1, -1, -1):
        v = inv.verdicts.get(i)
        if i == 0 or (v and v["verdict"] in ("pass", "fail")):
            return i
    return None


def confirm(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    h = inv.tree.h[a.hypothesis]
    c = a.params["index"]
    culprits = sorted(set(h.reverts + [c]))
    names = ", ".join(inv.commits[i].short for i in culprits)
    ctx.bus.emit("strip.probe", index=inv.head_idx, track=h.id, label=f"counterfactual — revert {names} at HEAD, run test")
    r = ctx.sandbox.probe(ProbeRequest(inv.head.sha, [inv.test_id], patches=_reverts(ctx, inv, culprits),
                                       label="confirm"))
    _account(inv, r)
    data = {"sha": inv.head.sha, "reverted": [inv.commits[i].sha for i in culprits], "verdict": r.verdict,
            "skip_kind": r.skip_kind, "signature": r.signature, "hypothesis": h.id}
    obs = ctx.evidence.record("BISECTION", "sandbox", f"revert {names} at HEAD",
                              f"HEAD with {names} reverted: {r.verdict.upper()}"
                              + (f" ({r.skip_kind})" if r.skip_kind else ""), data, probe_raw(r))
    ctx.bus.emit("strip.confirm", indices=culprits, verdict=r.verdict, track=h.id, evidence=obs.id)
    if r.verdict == "skip":
        inv.extra.setdefault("confirm_unavailable", {})[h.id] = r.skip_kind
        return ActionResult(a, None, [obs.id], f"counterfactual unavailable: revert of {names} → {r.skip_kind}",
                            "dim", {"detail": r.message[:200], "tree_label": "revert|conflicts"})
    passed = r.verdict == "pass"
    inv.claim("BISECTION", f"With {names} reverted, the test {'passes' if passed else 'still fails'} at HEAD "
                           f"{inv.head.short}.", [obs.id],
              check={"type": "probe", "evidence": obs.id, "sha": inv.head.sha, "verdict": r.verdict})
    if passed:
        return ActionResult(a, "pass", [obs.id], f"revert {names} at HEAD → <ok>PASS</ok>", "ok",
                            {"detail": "counterfactual confirmed: the culprit explains the failure",
                             "tree_label": f"revert {names}|test passes"})
    detail = "reverting it does not fix HEAD — one commit does not explain all evidence"
    if r.signature and r.signature != inv.extra.get("head_signature"):
        detail += f" · new failure: {r.signature[:70]}"
    return ActionResult(a, "fail", [obs.id], f"revert {names} at HEAD → <no>still FAILS</no>", "no",
                        {"detail": detail, "tree_label": f"revert {names}|still fails"})


def window_extend(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    dist = a.params["distance"]
    anc = ctx.repo.ancestors(inv.last_good.sha, dist)
    if not anc:
        return ActionResult(a, None, [], "no older history before last-good", "dim", {"tree_label": "no history"})
    sha = anc[min(dist, len(anc)) - 1]
    c = ctx.repo.commit(sha)
    r = ctx.sandbox.probe(ProbeRequest(sha, [inv.test_id], label=f"extend~{dist}"))
    _account(inv, r)
    obs = ctx.evidence.record("BISECTION", "sandbox", f"probe last-good~{len(anc[:dist])} {c.short}",
                              f"{c.short} (last-good~{min(dist, len(anc))}): {r.verdict.upper()}",
                              {"sha": sha, "verdict": r.verdict, "distance": dist, "skip_kind": r.skip_kind},
                              probe_raw(r))
    if r.verdict == "skip":
        return ActionResult(a, None, [obs.id], f"{c.short} unbuildable", "dim", {"tree_label": f"{c.short}|skip"})
    passed = r.verdict == "pass"
    inv.claim("BISECTION", f"At {c.short}, {min(dist, len(anc))} commits before last-good, the test "
                           f"{'passed' if passed else 'failed'}.", [obs.id],
              check={"type": "probe", "evidence": obs.id, "sha": sha, "verdict": r.verdict})
    if passed:
        inv.extra["older_good"] = {"sha": sha, "short": c.short, "distance": min(dist, len(anc))}
    tone = "ok" if passed else "no"
    return ActionResult(a, "pass" if passed else "fail", [obs.id],
                        f"last-good~{min(dist, len(anc))} {c.short} → <{tone}>{r.verdict.upper()}</{tone}>", tone,
                        {"detail": "an older commit passes → the regression predates the window" if passed
                         else "still failing further back",
                         "tree_label": f"~{min(dist, len(anc))} {c.short}|{r.verdict}"})
