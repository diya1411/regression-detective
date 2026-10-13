"""Run the agent over the ground-truth corpus and score it objectively.

    python -m eval.run_eval                 # all cases (LLM on if GEMINI_API_KEY is set)
    python -m eval.run_eval --offline       # deterministic fallbacks only
    python -m eval.run_eval --only c1_single_defect c6_multi_cause

Metrics (report §10): isolation accuracy, probe efficiency (bisection probes vs. the log2 minimum),
false-confidence rate (wrong answer at confidence >= 0.7 — the key safety number), early-termination
correctness for the flaky and dependency cases, and wall-clock time per investigation.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from detective.config import Budget, Settings
from detective.graph import Params, run_investigation

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
RESULTS = HERE / "results"
HIGH_CONFIDENCE = 0.7


def score(truth: dict, rep: dict) -> dict:
    status = rep.get("status")
    culprits = [c["sha"] for c in rep.get("culprits", [])]
    conf = (rep.get("verification") or {}).get("confidence") or 0.0
    exp = truth["expect"]
    hyps = {h["kind"]: h for h in rep.get("hypotheses", [])}
    bis = rep.get("budget", {}).get("bisection_probes", 0)
    n = rep.get("window", {}).get("commits", 0)
    checks: dict[str, bool] = {"status": status == exp}
    if exp in ("resolved", "multi_cause"):
        checks["culprits"] = set(culprits) == set(truth["culprits"])
        if truth.get("classification"):
            checks["classification"] = (rep.get("explanation") or {}).get("classification") == truth["classification"]
        if truth.get("max_probes"):
            checks["probes<=max"] = bis <= truth["max_probes"]
    elif exp == "flaky":
        checks["no_bisection"] = bis == 0
    elif exp == "dependency":
        pk = set((rep.get("conclusion") or {}).get("packages") or [])
        checks["package"] = set(truth["packages"]) <= pk
        checks["H1_eliminated"] = hyps.get("commit", {}).get("status") in ("eliminated", "unreachable")
        checks["no_culprit"] = not culprits
    elif exp == "window_invalid":
        checks["no_culprit"] = not culprits
    elif exp == "budget_exhausted":
        checks["no_culprit"] = not culprits
        cands = (rep.get("conclusion") or {}).get("candidates") or []
        idx = {c["sha"]: i for i, c in enumerate(rep.get("_commits", []))}
        checks["truth_in_range"] = idx.get(truth.get("true_culprit"), -1) in cands if idx else True
    correct = all(checks.values())
    named_wrong = bool(culprits) and set(culprits) != set(truth.get("culprits", []))
    return {"correct": correct, "checks": checks, "confidence": conf,
            "false_confidence": (not correct) and conf >= HIGH_CONFIDENCE, "named_wrong_culprit": named_wrong,
            "bisection_probes": bis, "log2_min": math.ceil(math.log2(n)) if n > 1 else 0,
            "probes_total": rep.get("budget", {}).get("probes"), "seconds": rep.get("budget", {}).get("seconds"),
            "llm_calls": rep.get("budget", {}).get("llm", {}).get("calls"), "status": status,
            "culprits": [c["short"] for c in rep.get("culprits", [])],
            "classification": (rep.get("explanation") or {}).get("classification")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args(argv)
    cases = sorted(p for p in FIXTURES.iterdir() if (p / "truth.json").exists()) if FIXTURES.exists() else []
    if not cases:
        raise SystemExit("no fixtures: run `python -m eval.build_fixtures` first")
    rows = []
    for case in cases:
        if a.only and case.name not in a.only:
            continue
        truth = json.loads((case / "truth.json").read_text())
        cs = truth.get("settings", {})
        settings = Settings().with_(offline=a.offline, use_history_index=not a.no_history,
                                    budget=Budget(max_probes=cs.get("max_probes", 16)))
        t0 = time.time()
        print(f"== {case.name}: {truth['title']}", flush=True)
        inv = run_investigation(Params(repo=str(case / "repo"), test=truth["test"], last_good=truth["last_good"],
                                       head=truth["head"], meta=str(case / "meta"),
                                       mirror=str(case / "registry") if (case / "registry").exists() else None,
                                       id=f"eval-{case.name}-{time.strftime('%H%M%S')}", label=case.name),
                                settings)
        rep = inv.report or {"status": inv.status}
        rep["_commits"] = [c.to_dict() for c in inv.commits]
        s = score(truth, rep)
        s.update(case=case.name, title=truth["title"], expect=truth["expect"], wall=round(time.time() - t0, 1),
                 investigation=inv.id)
        rows.append(s)
        print(f"   {'PASS' if s['correct'] else 'FAIL'} status={s['status']} culprits={s['culprits']} "
              f"conf={s['confidence']} probes={s['bisection_probes']}/{s['log2_min']} {s['wall']}s "
              f"checks={s['checks']}", flush=True)
    RESULTS.mkdir(exist_ok=True)
    mode = "offline" if a.offline else ("gemini" if Settings().llm_enabled else "offline(no key)")
    summary = {
        "mode": mode, "history_index": not a.no_history, "cases": len(rows),
        "accuracy": round(sum(r["correct"] for r in rows) / max(len(rows), 1), 3),
        "false_confidence_rate": round(sum(r["false_confidence"] for r in rows) / max(len(rows), 1), 3),
        "wrong_culprit_named": sum(r["named_wrong_culprit"] for r in rows),
        "mean_seconds": round(sum(r["wall"] for r in rows) / max(len(rows), 1), 1),
        "probe_efficiency": [f"{r['case']}: {r['bisection_probes']}/{r['log2_min']}" for r in rows
                             if r["expect"] in ("resolved", "multi_cause")],
    }
    tag = f"-{a.tag}" if a.tag else ""
    (RESULTS / f"results{tag}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    (RESULTS / f"results{tag}.md").write_text(_md(summary, rows))
    print(json.dumps(summary, indent=1))


def _md(summary: dict, rows: list) -> str:
    out = [f"# Evaluation results ({summary['mode']}, history index {'on' if summary['history_index'] else 'off'})", "",
           f"- Cases: {summary['cases']}  ·  accuracy **{summary['accuracy']:.0%}**",
           f"- False-confidence rate: **{summary['false_confidence_rate']:.0%}**  ·  wrong culprits named: "
           f"{summary['wrong_culprit_named']}", f"- Mean wall-clock per investigation: {summary['mean_seconds']}s", "",
           "| # | case | expected | got | culprit(s) | classification | bisection probes / log2 min | confidence | "
           "time | result |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {r['title']} | {r['expect']} | {r['status']} | {', '.join(r['culprits']) or '–'} | "
                   f"{r.get('classification') or '–'} | {r['bisection_probes']} / {r['log2_min']} | {r['confidence']} | "
                   f"{r['wall']}s | {'✅' if r['correct'] else '❌ ' + ', '.join(k for k, v in r['checks'].items() if not v)} |")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
