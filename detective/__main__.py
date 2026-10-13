"""Command line: investigate | serve | graph."""
from __future__ import annotations

import argparse
import json
import re
import sys

from .config import Budget, Settings

COLORS = {"ORCHESTRATOR": 35, "TRIAGE": 33, "RAG": 90, "HYPOTHESIS": 35, "FLAKINESS": 32, "BISECTION": 31,
          "DEPENDENCY": 33, "ENVIRONMENT": 36, "EXPLANATION": 32, "PATCH": 34, "VERIFIER": 37}
_TAG = re.compile(r"</?(ok|no|hl|dim)>")


def _echo(ev: dict) -> None:
    t = ev["type"]
    tty = sys.stdout.isatty()

    def col(code, s):
        return f"\033[{code}m{s}\033[0m" if tty else s

    if t == "agent.message":
        who = ev["agent"]
        line = f"{ev['t']:7.1f}s {col(COLORS.get(who, 37), f'{who:<12}')} {_TAG.sub('', ev['title'])}"
        if ev.get("detail"):
            line += col(90, f"  — {_TAG.sub('', ev['detail'])}")
        if ev.get("evidence"):
            line += col(90, f"  [{', '.join(ev['evidence'][:4])}]")
        print(line, flush=True)
    elif t == "error":
        print(col(31, f"ERROR in {ev.get('node')}: {ev.get('message')}"), flush=True)
    elif t == "tree.spawn":
        h = ev["hypothesis"]
        print(col(35, f"         + {h['id']} {h['title']} (p={h['p']:.2f})"), flush=True)


def _trace(ident: str) -> int:
    from .events import read_events

    root = Settings().investigations_dir
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime) if root.exists() else []
    pick = dirs[-1:] if ident == "last" else [p for p in dirs if p.name.startswith(ident) or ident in p.name]
    if not pick:
        print(f"no investigation matching {ident!r} in {root}")
        return 1
    d = pick[-1]
    print(f"# {d.name}")
    for ev in read_events(d / "events.jsonl"):
        t = ev["type"]
        if t == "agent.message":
            _echo(ev)
        elif t == "orchestrator.decision":
            cands = "  ".join(f"{c['name']}[{c['hypothesis']}] {c['eig_bits']:.2f}b/{c['cost_s']:.0f}s"
                              for c in ev["candidates"][:4])
            print(f"{ev['t']:7.1f}s {'':12} · decide (H={ev['entropy_bits']:.2f} bits): {cands}")
        elif t == "tree.update":
            ps = " ".join(f"{h['id']}={h['p']:.2f}{'' if h['status'] == 'open' else '/' + h['status'][:4]}"
                          for h in ev["hypotheses"])
            print(f"{ev['t']:7.1f}s {'':12} · tree: {ps}")
        elif t in ("tree.spawn", "error"):
            _echo(ev)
    return 0


def _settings(a) -> Settings:
    s = Settings()
    budget = Budget(max_probes=a.max_probes, max_seconds=a.max_seconds, max_llm_calls=a.max_llm_calls)
    return s.with_(offline=a.offline, use_history_index=not a.no_history, flaky_runs=a.flaky_runs,
                   open_pr=a.open_pr, budget=budget, confidence_threshold=a.threshold)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="regression-detective", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("investigate", help="diagnose one failing test")
    inv.add_argument("--repo", required=True, help="local path or git URL")
    inv.add_argument("--test", required=True, help="pytest node id, e.g. tests/test_cart.py::test_total")
    inv.add_argument("--last-good", help="last commit known to pass (default: from CI history, else HEAD~32)")
    inv.add_argument("--head", default="HEAD")
    inv.add_argument("--meta", help="directory with ci_runs.json / history.json / ci_archive.json")
    inv.add_argument("--mirror", help="local package mirror directory (index.json + wheels)")
    inv.add_argument("--offline", action="store_true", help="never call the LLM (deterministic fallbacks)")
    inv.add_argument("--no-history", action="store_true", help="disable retrieval point one (CI archive priors)")
    inv.add_argument("--max-probes", type=int, default=16)
    inv.add_argument("--max-seconds", type=float, default=1800)
    inv.add_argument("--max-llm-calls", type=int, default=40)
    inv.add_argument("--flaky-runs", type=int, default=10)
    inv.add_argument("--threshold", type=float, default=0.9)
    inv.add_argument("--open-pr", action="store_true", help="open a GitHub draft PR if verification allows it")
    inv.add_argument("--json", action="store_true", help="print the final report as JSON")

    srv = sub.add_parser("serve", help="web UI + API")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    sub.add_parser("graph", help="print the agent graph as Mermaid")
    tr = sub.add_parser("trace", help="print the decision trace of a past investigation")
    tr.add_argument("id", help="investigation id (or a unique prefix / 'last')")
    a = ap.parse_args(argv)

    if a.cmd == "trace":
        return _trace(a.id)
    if a.cmd == "graph":
        from .graph import mermaid

        print(mermaid())
        return 0
    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("detective.server.app:app", host=a.host, port=a.port, log_level="warning")
        return 0

    from .graph import Params, run_investigation

    settings = _settings(a)
    print(f"Regression Detective · LLM {'on (' + settings.gemini_models[0] + ')' if settings.llm_enabled else 'off'}"
          f" · sandbox {settings.sandbox} · home {settings.home}", flush=True)
    result = run_investigation(Params(repo=a.repo, test=a.test, last_good=a.last_good, head=a.head, meta=a.meta,
                                      mirror=a.mirror), settings, echo=None if a.json else _echo)
    r = result.report or {}
    if a.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        v = r.get("verification") or {}
        print(f"\nstatus: {r.get('status')}  confidence: {v.get('confidence')}  "
              f"culprits: {', '.join(c['short'] for c in r.get('culprits', [])) or '-'}")
        print(f"report: {settings.investigations_dir / result.id / 'report.md'}")
    return 0 if result.status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
