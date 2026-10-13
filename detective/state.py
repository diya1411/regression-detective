"""Investigation state: everything the LangGraph nodes read and write."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .belief import Belief
from .policy import Action, CostModel
from .tools.git import Commit


@dataclass
class ActionResult:
    action: Action
    outcome: Optional[str]          # None = uninformative (skip, conflict, error)
    evidence: list[str] = field(default_factory=list)
    message: str = ""
    tone: str = ""                  # ok | no | dim
    data: dict = field(default_factory=dict)


@dataclass
class Investigation:
    id: str
    repo_path: Path
    test_id: str
    repo_url: str = ""
    head: Optional[Commit] = None
    last_good: Optional[Commit] = None
    commits: list[Commit] = field(default_factory=list)   # [last_good, ..., head]
    window_source: str = "user"
    failure_log: str = ""
    symptom: dict = field(default_factory=dict)
    relevance: dict[int, float] = field(default_factory=dict)
    relevance_reason: dict[int, str] = field(default_factory=dict)
    import_closure: list[str] = field(default_factory=list)
    tree: Optional[Belief] = None
    cost: CostModel = field(default_factory=CostModel)
    actions_done: Counter = field(default_factory=Counter)
    flaky: dict = field(default_factory=dict)
    dep: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    probes: list[dict] = field(default_factory=list)
    verdicts: dict[int, dict] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    pending: list[Action] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    iteration: int = 0
    probe_count: int = 0
    started: float = field(default_factory=time.time)
    conclusion: Optional[dict] = None
    explanation: Optional[dict] = None
    patch: Optional[dict] = None
    patch_attempts: int = 0
    verification: Optional[dict] = None
    report: Optional[dict] = None
    status: str = "running"
    notes: list[str] = field(default_factory=list)
    sandbox_cache: Callable[[int], bool] = field(default=lambda i: False, repr=False)
    extra: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- bookkeeping
    def done(self, name: str, key: str | None = None) -> bool:
        return self.actions_done.get(f"{name}:{key}" if key else name, 0) > 0

    def count(self, name: str) -> int:
        return self.actions_done.get(name, 0)

    def mark(self, name: str, key: str | None = None) -> None:
        self.actions_done[name] += 1
        if key:
            self.actions_done[f"{name}:{key}"] += 1

    def elapsed(self) -> float:
        return time.time() - self.started

    def idx_of(self, sha: str) -> Optional[int]:
        for i, c in enumerate(self.commits):
            if c.sha == sha:
                return i
        return None

    @property
    def head_idx(self) -> int:
        return len(self.commits) - 1

    def claim(self, agent: str, text: str, evidence: list[str], kind: str = "observed",
              check: dict | None = None) -> None:
        self.claims.append({"agent": agent, "text": text, "evidence": evidence, "kind": kind,
                            "check": check or {}})
