"""Web UI + API. Investigations run in background threads; the UI follows them over Server-Sent Events.

Because every investigation is an append-only event log, a live run and a replay of a finished run
are the same stream (replay can be paced with ?speed=N) — a reliable demo even without Docker.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Budget, Settings
from ..evidence import load_evidence
from ..events import read_events
from ..graph import Params, run_investigation

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
FIXTURES = ROOT / "eval" / "fixtures"

app = FastAPI(title="Regression Detective")
app.mount("/static", StaticFiles(directory=str(UI)), name="static")
LIVE: dict[str, threading.Thread] = {}


def settings() -> Settings:
    return Settings()


class StartRequest(BaseModel):
    repo: str = ""
    test: str = ""
    last_good: str | None = None
    head: str = "HEAD"
    meta: str | None = None
    mirror: str | None = None
    fixture: str | None = None
    offline: bool = False
    max_probes: int = 16
    flaky_runs: int = 10
    use_history: bool = True


@app.get("/")
def index():
    return FileResponse(UI / "index.html")


@app.get("/api/status")
def status():
    s = settings()
    return {"llm": {"enabled": s.llm_enabled, "models": list(s.gemini_models)}, "sandbox": "local",
            "home": str(s.home)}


@app.get("/api/fixtures")
def fixtures():
    out = []
    if FIXTURES.exists():
        for d in sorted(FIXTURES.iterdir()):
            t = d / "truth.json"
            if t.exists():
                tr = json.loads(t.read_text())
                out.append({"name": d.name, "title": tr["title"], "expect": tr["expect"], "test": tr["test"],
                            "repo": str(d / "repo"), "last_good": tr["last_good"], "head": tr["head"],
                            "meta": str(d / "meta"), "mirror": str(d / "registry") if (d / "registry").exists() else None,
                            "settings": tr.get("settings", {})})
    return out


@app.post("/api/investigations")
def start(req: StartRequest):
    if req.fixture:
        fx = {f["name"]: f for f in fixtures()}.get(req.fixture)
        if not fx:
            raise HTTPException(404, f"unknown fixture {req.fixture}")
        req.repo, req.test, req.last_good, req.head = fx["repo"], fx["test"], fx["last_good"], fx["head"]
        req.meta, req.mirror = fx["meta"], fx["mirror"]
        req.max_probes = fx["settings"].get("max_probes", req.max_probes)
    if not req.repo or not req.test:
        raise HTTPException(400, "repo and test are required")
    s = settings().with_(offline=req.offline, flaky_runs=req.flaky_runs, use_history_index=req.use_history,
                         budget=Budget(max_probes=req.max_probes))
    inv_id = time.strftime("%Y%m%d-%H%M%S-") + (req.fixture or "run")[:24]
    params = Params(repo=req.repo, test=req.test, last_good=req.last_good or None, head=req.head or "HEAD",
                    meta=req.meta, mirror=req.mirror, id=inv_id, label=req.fixture or "")
    th = threading.Thread(target=_run, args=(params, s), daemon=True, name=f"inv-{inv_id}")
    LIVE[inv_id] = th
    th.start()
    return {"id": inv_id}


def _run(params: Params, s: Settings) -> None:
    try:
        run_investigation(params, s)
    except Exception as e:  # surfaced through the event log
        d = s.investigations_dir / params.id
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"seq": 0, "t": 0, "type": "error", "node": "runner", "message": str(e)}) + "\n")
            fh.write(json.dumps({"seq": 0, "t": 0, "type": "investigation.finished", "status": "error",
                                 "summary": str(e)}) + "\n")


@app.get("/api/investigations")
def list_investigations():
    root = settings().investigations_dir
    out = []
    if root.exists():
        for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
            evs = read_events(d / "events.jsonl")
            if not evs:
                continue
            start_ev = next((e for e in evs if e["type"] == "investigation.started"), {})
            fin = next((e for e in reversed(evs) if e["type"] == "investigation.finished"), None)
            out.append({"id": d.name, "label": start_ev.get("label", ""), "test": start_ev.get("test", ""),
                        "repo": start_ev.get("repo", ""), "status": fin["status"] if fin else "running",
                        "summary": (fin or {}).get("summary", ""), "seconds": (fin or {}).get("seconds"),
                        "live": d.name in LIVE and LIVE[d.name].is_alive()})
    return out


def _dir(inv_id: str) -> Path:
    d = settings().investigations_dir / inv_id
    if not d.exists() or ".." in inv_id:
        raise HTTPException(404, "no such investigation")
    return d


@app.get("/api/investigations/{inv_id}/events")
async def events(inv_id: str, speed: float = 0.0):
    path = _dir(inv_id) / "events.jsonl"

    async def gen():
        pos, last_t, idle = 0, None, 0.0
        while True:
            chunk = ""
            if path.exists():
                with open(path, encoding="utf-8") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
            lines = chunk.split("\n")
            complete, rest = lines[:-1], lines[-1]
            pos += len(chunk.encode("utf-8")) - len(rest.encode("utf-8"))
            for line in complete:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if speed > 0 and last_t is not None:
                    await asyncio.sleep(max(0.0, min((ev["t"] - last_t) / speed, 1.5)))
                last_t = ev["t"]
                yield f"data: {line}\n\n"
                if ev["type"] == "investigation.finished":
                    return
            if not complete:
                th = LIVE.get(inv_id)
                if th is None or not th.is_alive():
                    idle += 0.25
                    if idle > 3:
                        return
                await asyncio.sleep(0.25)
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/investigations/{inv_id}/evidence/{eid}")
def evidence(inv_id: str, eid: str):
    d = _dir(inv_id)
    obs = load_evidence(d).get(eid)
    if not obs:
        raise HTTPException(404, "no such evidence")
    raw_path = d / "raw" / f"{eid}.txt"
    return {**obs, "raw": raw_path.read_text() if raw_path.exists() else obs.get("raw_preview", "")}


@app.get("/api/investigations/{inv_id}/evidence")
def evidence_list(inv_id: str):
    return list(load_evidence(_dir(inv_id)).values())


@app.get("/api/investigations/{inv_id}/report")
def report(inv_id: str, format: str = "json"):
    d = _dir(inv_id)
    if format == "md" and (d / "report.md").exists():
        return FileResponse(d / "report.md", media_type="text/markdown", filename=f"{inv_id}.md")
    if not (d / "report.json").exists():
        raise HTTPException(404, "report not ready")
    return JSONResponse(json.loads((d / "report.json").read_text()))
