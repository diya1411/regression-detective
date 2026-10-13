"""LangGraph wiring. The hypothesis tree is explicit state and the control loop is a cyclic graph
with conditional exits:

    triage → hypothesize → orchestrate ⇄ dispatch          (evidence re-plans the next action)
                               └→ explain → patch ⟲ → verify → report
                                              (a failed patch loops back into the sandbox)
"""
from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import explanation, hypothesis, orchestrator, patch, triage, verifier
from .agents.base import Ctx
from .config import Settings
from .events import EventBus
from .evidence import EvidenceLog
from .llm import LLM
from .rag import Retriever, make_embedder
from .report import build as build_report
from .sandbox import make_sandbox
from .state import Investigation
from .tools.forge import Forge, GitHubForge, LocalForge
from .tools.git import GitRepo
from .tools.pypi import Registry


class GState(TypedDict):
    inv: Investigation


@dataclass
class Params:
    repo: str
    test: str
    last_good: str | None = None
    head: str = "HEAD"
    meta: str | None = None          # LocalForge directory (ci_runs.json, history.json, ci_archive.json)
    mirror: str | None = None        # local package mirror (index.json + wheels)
    id: str | None = None
    label: str = ""
    extra: dict = field(default_factory=dict)


def _guard(name, fn):
    def node(state: GState) -> GState:
        inv = state["inv"]
        if inv.status == "error":
            return state
        try:
            fn(inv)
        except Exception as e:
            inv.status = "error"
            inv.notes.append(f"{name} failed: {e!r}")
            inv.extra["traceback"] = traceback.format_exc()
            ctx = inv.extra["_ctx"]
            ctx.bus.emit("error", node=name, message=f"{type(e).__name__}: {e}")
        return {"inv": inv}
    return node


def build_graph(ctx: Ctx, params: Params):
    g = StateGraph(GState)
    g.add_node("triage", _guard("triage", lambda inv: triage.run(ctx, inv, params.last_good, params.head)))
    g.add_node("hypothesize", _guard("hypothesize", lambda inv: hypothesis.run(ctx, inv)))

    def orchestrate(inv):
        orchestrator.absorb(ctx, inv)
        orchestrator.decide(ctx, inv)

    g.add_node("orchestrate", _guard("orchestrate", orchestrate))
    g.add_node("dispatch", _guard("dispatch", lambda inv: orchestrator.dispatch(ctx, inv)))
    g.add_node("explain", _guard("explain", lambda inv: explanation.run(ctx, inv)))
    g.add_node("patch", _guard("patch", lambda inv: patch.run(ctx, inv)))
    g.add_node("verify", _guard("verify", lambda inv: verifier.run(ctx, inv)))
    g.add_node("report", lambda s: _report(ctx, s))

    def ok_or_report(nxt):
        return lambda s: "report" if s["inv"].status == "error" else nxt

    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", ok_or_report("hypothesize"), ["hypothesize", "report"])
    g.add_conditional_edges("hypothesize", ok_or_report("orchestrate"), ["orchestrate", "report"])
    g.add_conditional_edges(
        "orchestrate",
        lambda s: "report" if s["inv"].status == "error" else ("dispatch" if s["inv"].pending else "explain"),
        ["dispatch", "explain", "report"])
    g.add_edge("dispatch", "orchestrate")
    g.add_conditional_edges("explain", ok_or_report("patch"), ["patch", "report"])
    g.add_conditional_edges(
        "patch",
        lambda s: "report" if s["inv"].status == "error" else (
            "patch" if (s["inv"].patch or {}).get("status") == "retry" else "verify"),
        ["patch", "verify", "report"])
    g.add_edge("verify", "report")
    g.add_edge("report", END)
    return g.compile()


def _report(ctx: Ctx, state: GState) -> GState:
    inv = state["inv"]
    if inv.status != "error":
        inv.status = "done"
    try:
        inv.report = build_report(ctx, inv)
    except Exception as e:
        inv.notes.append(f"report failed: {e!r}")
        inv.extra["traceback"] = traceback.format_exc()
        inv.report = {"status": "error", "notes": inv.notes}
        (ctx.inv_dir / "report.json").write_text(__import__("json").dumps(inv.report))
    status = inv.report.get("status") if inv.report else inv.status
    ctx.bus.emit("report", report=inv.report)
    ctx.bus.emit("investigation.finished", status=status, seconds=round(inv.elapsed(), 1),
                 summary=(inv.explanation or {}).get("summary") or "; ".join(inv.notes))
    return {"inv": inv}


def mermaid() -> str:
    dummy = Ctx(*([None] * 10))  # graph shape does not depend on runtime objects
    return build_graph(dummy, Params(repo="", test="")).get_graph().draw_mermaid()


# ------------------------------------------------------------------------------------- entry point
def run_investigation(params: Params, settings: Settings | None = None, echo=None) -> Investigation:
    settings = settings or Settings()
    inv_id = params.id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    inv_dir = settings.investigations_dir / inv_id
    inv_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus(inv_dir / "events.jsonl", echo=echo)
    evidence = EvidenceLog(inv_dir, bus)

    repo_url = ""
    if params.repo.startswith(("http://", "https://", "git@")):
        repo_url = params.repo
        name = params.repo.rstrip("/").split("/")[-1].removesuffix(".git")
        repo = GitRepo.clone(params.repo, settings.work_dir / "repos" / name)
    else:
        repo = GitRepo(params.repo)
    if params.meta:
        forge: Forge = LocalForge(Path(params.meta))
    elif repo_url and "github.com" in repo_url:
        forge = GitHubForge(repo_url, settings.github_token)
    else:
        forge = Forge()
    mirror = Path(params.mirror) if params.mirror else None
    llm = LLM(settings, inv_dir / "llm", bus)
    inv = Investigation(id=inv_id, repo_path=repo.path, test_id=params.test, repo_url=repo_url)
    bus.emit("investigation.started", id=inv_id, repo=repo_url or str(repo.path), test=params.test,
             label=params.label, last_good=params.last_good, head=params.head,
             config={"llm": llm.describe(), "sandbox": settings.sandbox, "flaky_runs": settings.flaky_runs,
                     "max_probes": settings.budget.max_probes, "threshold": settings.confidence_threshold,
                     "history_index": settings.use_history_index, "forge": forge.name})
    try:
        sandbox = make_sandbox(settings, repo, mirror)
    except Exception as e:
        bus.emit("error", node="sandbox", message=str(e))
        bus.emit("investigation.finished", status="error", summary=str(e))
        inv.status = "error"
        inv.notes.append(str(e))
        return inv
    registry = Registry(settings.cache_dir, mirror=mirror)
    retriever = Retriever(repo, forge, make_embedder(settings, llm.client), settings.use_history_index)
    ctx = Ctx(settings, repo, forge, sandbox, registry, retriever, llm, bus, evidence, inv_dir)
    inv.extra["_ctx"] = ctx
    inv.sandbox_cache = lambda i: sandbox.is_cached(sandbox.dep_spec(inv.commits[i].sha))
    graph = build_graph(ctx, params)
    try:
        graph.invoke({"inv": inv}, config={"recursion_limit": 500})
    finally:
        inv.extra.pop("_ctx", None)
        bus.close()
    return inv
