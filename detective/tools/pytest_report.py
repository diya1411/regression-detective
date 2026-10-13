"""Turn pytest's JSON report (pytest-json-report) into a verdict and a failure signature."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# pytest exit codes
OK, TESTS_FAILED, INTERRUPTED, INTERNAL, USAGE, NO_TESTS = 0, 1, 2, 3, 4, 5


@dataclass
class RunOutcome:
    outcome: str        # passed | failed | error | collection_error | missing | timeout
    duration: float
    message: str = ""
    signature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_WS = re.compile(r"\s+")
_TMP = re.compile(r"/(?:tmp|work|private)/[^\s:'\"]*")


def signature(message: str) -> str:
    """A normalised, comparable fingerprint of a failure (first line of the crash message)."""
    if not message:
        return ""
    first = message.strip().splitlines()[0]
    first = _ADDR.sub("0x?", first)
    first = _TMP.sub("<path>", first)
    return _WS.sub(" ", first).strip()[:240]


def parse_run(report: dict | None, exit_code: int, test_id: str, duration: float,
              output: str = "", timed_out: bool = False) -> RunOutcome:
    if timed_out:
        return RunOutcome("timeout", duration, "test exceeded the probe timeout", "TIMEOUT")
    if report is None:
        msg = _tail(output)
        if exit_code == USAGE or "ERROR: file or directory not found" in output or "not found:" in output:
            return RunOutcome("missing", duration, msg, "")
        return RunOutcome("error", duration, msg, signature(msg))

    for c in report.get("collectors", []) or []:
        if c.get("outcome") == "failed":
            msg = c.get("longrepr", "") or ""
            return RunOutcome("collection_error", duration, _tail(msg), signature(_last_error(msg)))

    tests = report.get("tests", []) or []
    target = _match(tests, test_id)
    if target is None:
        if exit_code in (USAGE, NO_TESTS) or not tests:
            return RunOutcome("missing", duration, _tail(output), "")
        target = tests[0]

    outcome = target.get("outcome", "error")
    for phase in ("setup", "call", "teardown"):
        ph = target.get(phase) or {}
        if ph.get("outcome") == "failed":
            crash = ph.get("crash") or {}
            msg = crash.get("message") or ph.get("longrepr") or ""
            kind = "failed" if phase == "call" else "error"
            return RunOutcome(kind, duration, msg[:4000], signature(msg))
    if outcome == "passed":
        return RunOutcome("passed", duration)
    if outcome in ("skipped", "xfailed"):
        return RunOutcome("missing", duration, f"test {outcome}", "")
    return RunOutcome("error", duration, outcome, outcome)


def suite_outcomes(report: dict | None) -> dict[str, str]:
    if not report:
        return {}
    return {t["nodeid"]: t.get("outcome", "error") for t in report.get("tests", []) or []}


def _match(tests: list[dict], test_id: str) -> dict | None:
    for t in tests:
        if t.get("nodeid") == test_id:
            return t
    tail = test_id.split("::")[-1]
    for t in tests:
        if t.get("nodeid", "").endswith("::" + tail):
            return t
    return None


def _tail(text: str, n: int = 3000) -> str:
    return (text or "")[-n:]


def _last_error(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("E ") or "Error" in line:
            return line.removeprefix("E").strip()
    return (text or "").strip().splitlines()[-1] if text and text.strip() else ""
