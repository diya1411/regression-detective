"""Shared runtime context handed to every agent."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..events import EventBus
from ..evidence import EvidenceLog
from ..llm import LLM
from ..rag import Retriever
from ..sandbox import ProbeResult, Sandbox
from ..tools.forge import Forge
from ..tools.git import GitRepo
from ..tools.pypi import Registry


@dataclass
class Ctx:
    settings: Settings
    repo: GitRepo
    forge: Forge
    sandbox: Sandbox
    registry: Registry
    retriever: Retriever
    llm: LLM
    bus: EventBus
    evidence: EvidenceLog
    inv_dir: Path


def probe_raw(r: ProbeResult) -> str:
    """Raw tool output stored with each sandbox observation."""
    lines = [f"commit: {r.sha}", f"verdict: {r.verdict}" + (f" ({r.skip_kind})" if r.skip_kind else ""),
             f"runs: {len(r.runs)}  passed: {r.passes}  failed: {r.fails}", f"seconds: {r.seconds:.1f}"]
    if r.build:
        lines.append(f"image: {r.build.image}  cached: {r.build.cached}  build_s: {r.build.seconds:.1f}")
    if r.signature:
        lines.append(f"failure signature: {r.signature}")
    for i, run in enumerate(r.runs[:50]):
        lines.append(f"  run {i + 1}: {run.outcome} {run.duration:.2f}s {run.signature}")
    if r.message:
        lines += ["", "message:", r.message[:3000]]
    if r.log:
        lines += ["", "--- sandbox output (tail) ---", r.log[-5000:]]
    return "\n".join(lines)


def short(sha: str) -> str:
    return sha[:7]
