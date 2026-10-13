"""Append-only event log. Every UI update is an event; the log alone is enough to replay a run."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class EventBus:
    def __init__(self, path: Path | None = None, echo: Callable[[dict], None] | None = None):
        self.path = path
        self.t0 = time.time()
        self.seq = 0
        self._lock = threading.Lock()
        self._subs: list[Callable[[dict], None]] = []
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")
        if echo:
            self._subs.append(echo)

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subs.append(fn)

    def emit(self, type: str, **data: Any) -> dict:
        with self._lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": round(time.time() - self.t0, 3), "type": type, **data}
            if self._fh:
                self._fh.write(json.dumps(ev, default=str) + "\n")
                self._fh.flush()
        for fn in self._subs:
            try:
                fn(ev)
            except Exception:  # a broken subscriber must never kill the investigation
                pass
        return ev

    # convenience wrappers -------------------------------------------------
    def say(self, agent: str, title: str, detail: str = "", evidence: list[str] | None = None,
            tone: str = "") -> dict:
        return self.emit("agent.message", agent=agent, title=title, detail=detail,
                         evidence=evidence or [], tone=tone)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    break  # partially written last line
    return out
