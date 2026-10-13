"""Runs *inside* the sandbox. Standard library only. Executes pytest N times and prints one JSON line.

Configuration arrives in the RD_CFG environment variable:
  {"tests": [...], "repeat": n, "timeout": seconds, "workdir": "/work"}
"""
import json
import os
import subprocess
import sys
import time

MARK = "@@RD_RESULT@@"


def trim(report):
    if not report:
        return None
    tests = []
    for t in report.get("tests", []):
        item = {"nodeid": t.get("nodeid"), "outcome": t.get("outcome")}
        for phase in ("setup", "call", "teardown"):
            ph = t.get(phase)
            if ph and ph.get("outcome") == "failed":
                item[phase] = {"outcome": "failed", "crash": ph.get("crash"),
                               "longrepr": (ph.get("longrepr") or "")[-4000:]}
            elif ph:
                item[phase] = {"outcome": ph.get("outcome")}
        tests.append(item)
    collectors = [{"nodeid": c.get("nodeid"), "outcome": c.get("outcome"),
                   "longrepr": (c.get("longrepr") or "")[-4000:]}
                  for c in report.get("collectors", []) if c.get("outcome") == "failed"]
    return {"exitcode": report.get("exitcode"), "summary": report.get("summary"),
            "tests": tests, "collectors": collectors}


def main():
    cfg = json.loads(os.environ.get("RD_CFG", "{}"))
    workdir = cfg.get("workdir", "/work")
    tmp = os.environ.get("TMPDIR", "/tmp")
    results = []
    for i in range(int(cfg.get("repeat", 1))):
        rep = os.path.join(tmp, f"rd-report-{os.getpid()}-{i}.json")
        args = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
                "--json-report", f"--json-report-file={rep}",
                "--json-report-omit", "keywords", "streams", "log", "warnings", "traceback"]
        args += cfg.get("tests") or []
        t0 = time.time()
        timed_out = False
        try:
            p = subprocess.run(args, cwd=workdir, capture_output=True, text=True,
                               timeout=float(cfg.get("timeout", 120)))
            code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            timed_out, code = True, -9
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        report = None
        if os.path.exists(rep):
            try:
                with open(rep) as fh:
                    report = trim(json.load(fh))
            except Exception:
                report = None
            os.remove(rep)
        results.append({"exit": code, "duration": round(time.time() - t0, 3), "timed_out": timed_out,
                        "output": out[-6000:], "report": report})
    sys.stdout.write(MARK + json.dumps(results) + "\n")


if __name__ == "__main__":
    main()
