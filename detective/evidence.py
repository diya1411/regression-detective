"""The evidence log: every tool observation, with its raw output, under a stable id (E1, E2, ...).

Every claim in the final report must cite ids from this log; the Evidence Verifier enforces it.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .redact import redact

RAW_PREVIEW = 1200


@dataclass
class Observation:
    id: str
    agent: str
    tool: str
    action: str
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    raw_preview: str = ""
    raw_path: str | None = None
    kind: str = "experiment"   # experiment | static | retrieval | api | derived
    t: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class EvidenceLog:
    def __init__(self, root: Path | None, bus=None):
        self.root = root
        self.bus = bus
        self.items: dict[str, Observation] = {}
        self._n = 0
        self._lock = threading.Lock()
        self._t0 = time.time()
        if root:
            (root / "raw").mkdir(parents=True, exist_ok=True)

    def record(self, agent: str, tool: str, action: str, summary: str, data: dict | None = None,
               raw: str = "", kind: str = "experiment") -> Observation:
        raw = redact(raw or "")
        with self._lock:
            self._n += 1
            oid = f"E{self._n}"
        raw_path = None
        if raw and self.root:
            p = self.root / "raw" / f"{oid}.txt"
            p.write_text(raw, encoding="utf-8")
            raw_path = str(p)
        obs = Observation(id=oid, agent=agent, tool=tool, action=action, summary=summary,
                          data=data or {}, raw_preview=raw[:RAW_PREVIEW], raw_path=raw_path,
                          kind=kind, t=round(time.time() - self._t0, 3))
        with self._lock:
            self.items[oid] = obs
            if self.root:
                with open(self.root / "evidence.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(obs.to_dict(), default=str) + "\n")
        if self.bus:
            self.bus.emit("evidence.recorded", id=oid, agent=agent, tool=tool, action=action,
                          summary=summary, kind=kind)
        return obs

    def get(self, oid: str) -> Observation | None:
        return self.items.get(oid)

    def raw(self, oid: str) -> str:
        obs = self.items.get(oid)
        if not obs:
            return ""
        if obs.raw_path and Path(obs.raw_path).exists():
            return Path(obs.raw_path).read_text(encoding="utf-8")
        return obs.raw_preview

    def __len__(self) -> int:
        return len(self.items)

    def all(self) -> list[Observation]:
        return list(self.items.values())


def load_evidence(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = root / "evidence.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                out[d["id"]] = d
    return out
