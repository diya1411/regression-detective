"""Sandbox interface and the probe logic shared by the Docker and local backends."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Settings
from ..tools.git import GitRepo, apply_patch
from ..tools.pytest_report import RunOutcome, parse_run, suite_outcomes
from .deps import DepSpec, discover

RUNNER = Path(__file__).with_name("rd_runner.py")
MARK = "@@RD_RESULT@@"


class BuildFailed(Exception):
    def __init__(self, kind: str, log: str):
        super().__init__(kind)
        self.kind = kind
        self.log = log


@dataclass
class BuildInfo:
    key: str
    image: str
    cached: bool
    seconds: float
    freeze: dict[str, str] = field(default_factory=dict)


@dataclass
class ProbeRequest:
    sha: str
    tests: list[str]
    repeat: int = 1
    patches: list[tuple[str, bytes, bool]] = field(default_factory=list)  # (label, patch, reverse)
    file_edits: dict[str, str] = field(default_factory=dict)
    deps: DepSpec | None = None          # explicit dependency set (pin experiments)
    deps_from: str | None = None         # use another commit's manifests (repair probes)
    full_suite: bool = False
    timeout: int | None = None
    label: str = ""


@dataclass
class ProbeResult:
    sha: str
    verdict: str                         # pass | fail | skip
    skip_kind: str | None = None         # install_error | collection_error | test_missing | patch_conflict | sandbox_error
    runs: list[RunOutcome] = field(default_factory=list)
    passes: int = 0
    fails: int = 0
    signature: str = ""
    message: str = ""
    log: str = ""
    build: BuildInfo | None = None
    seconds: float = 0.0
    suite: dict[str, str] = field(default_factory=dict)
    label: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["runs"] = [r.to_dict() if hasattr(r, "to_dict") else r for r in self.runs]
        return d


class Sandbox:
    kind = "abstract"
    isolation = "none"

    def __init__(self, settings: Settings, repo: GitRepo, mirror: Path | None = None):
        self.settings = settings
        self.repo = repo
        self.mirror = mirror
        self._spec_cache: dict[str, DepSpec] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        # Throwaway probe workspaces live in the system temp dir, never inside a git repository:
        # git and pytest both search parent directories and would pick up the enclosing project.
        self.trees_dir = Path(tempfile.gettempdir()) / "regression-detective-probes"
        self.trees_dir.mkdir(parents=True, exist_ok=True)
        (settings.cache_dir / "deps").mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- backend hooks
    def build(self, spec: DepSpec) -> BuildInfo:
        raise NotImplementedError

    def is_cached(self, spec: DepSpec) -> bool:
        raise NotImplementedError

    def _execute(self, tree: Path, build: BuildInfo, cfg: dict, timeout: int) -> tuple[list[dict] | None, str]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"kind": self.kind, "isolation": self.isolation}

    def runner_packages(self) -> set[str]:
        """Packages the sandbox itself installs (pytest and friends): never repo dependencies."""
        return {"pip", "setuptools", "wheel", "pytest", "pluggy", "iniconfig", "packaging", "pygments",
                "pytest-json-report", "pytest-metadata", "exceptiongroup", "tomli", "colorama"}

    # ---------------------------------------------------------------- shared
    def key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def dep_spec(self, sha: str) -> DepSpec:
        if sha not in self._spec_cache:
            self._spec_cache[sha] = discover(lambda p: self.repo.show_file(sha, p))
        return self._spec_cache[sha]

    def mirror_tag(self) -> str:
        if not self.mirror or not self.mirror.exists():
            return ""
        return ",".join(sorted(p.name for p in self.mirror.iterdir()))

    def spec_key(self, spec: DepSpec) -> str:
        return spec.key(self.settings.python_version, self.mirror_tag())

    def save_freeze(self, key: str, freeze: dict[str, str]) -> None:
        (self.settings.cache_dir / "deps" / f"{key}.json").write_text(json.dumps(freeze))

    def load_freeze(self, key: str) -> dict[str, str] | None:
        p = self.settings.cache_dir / "deps" / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def probe(self, req: ProbeRequest) -> ProbeResult:
        t0 = time.time()
        spec = req.deps or self.dep_spec(req.deps_from or req.sha)
        try:
            build = self.build(spec)
        except BuildFailed as e:
            return ProbeResult(req.sha, "skip", e.kind, log=e.log[-6000:], message="dependency install failed",
                               seconds=time.time() - t0, label=req.label)

        tree = self.trees_dir / f"{req.sha[:10]}-{uuid.uuid4().hex[:8]}"
        try:
            self.repo.export(req.sha, tree)
            for label, patch, reverse in req.patches:
                ok, err = apply_patch(tree, patch, reverse=reverse)
                if not ok:
                    return ProbeResult(req.sha, "skip", "patch_conflict", message=f"{label}: {err.strip()[:400]}",
                                       build=build, seconds=time.time() - t0, label=req.label)
            for path, content in req.file_edits.items():
                target = (tree / path).resolve()
                if tree.resolve() not in target.parents:
                    raise PermissionError(f"edit escapes the tree: {path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            per_run = req.timeout or self.settings.probe_timeout_s
            cfg = {"tests": [] if req.full_suite else req.tests, "repeat": req.repeat, "timeout": per_run}
            raw, log = self._execute(tree, build, cfg, timeout=per_run * req.repeat + 60)
        except BuildFailed as e:
            return ProbeResult(req.sha, "skip", e.kind, log=e.log[-6000:], build=build,
                               seconds=time.time() - t0, label=req.label)
        finally:
            shutil.rmtree(tree, ignore_errors=True)

        if raw is None:
            return ProbeResult(req.sha, "skip", "sandbox_error", log=log[-6000:], message="sandbox produced no result",
                               build=build, seconds=time.time() - t0, label=req.label)

        target = req.tests[0] if req.tests else ""
        runs = [parse_run(r.get("report"), r.get("exit", -1), target, r.get("duration", 0.0),
                          r.get("output", ""), r.get("timed_out", False)) for r in raw]
        suite = suite_outcomes(raw[-1].get("report")) if req.full_suite else {}
        res = ProbeResult(req.sha, "skip", runs=runs, build=build, seconds=time.time() - t0, suite=suite,
                          log=(raw[-1].get("output", "") or log)[-6000:], label=req.label)
        res.passes = sum(r.outcome == "passed" for r in runs)
        res.fails = sum(r.outcome in ("failed", "error", "timeout") for r in runs)
        sigs = Counter(r.signature for r in runs if r.signature and r.outcome != "passed")
        if sigs:
            res.signature = sigs.most_common(1)[0][0]
            res.message = next(r.message for r in runs if r.signature == res.signature)
        kinds = Counter(r.outcome for r in runs)
        if res.fails:
            res.verdict = "fail"
        elif res.passes:
            res.verdict = "pass"
        elif kinds.get("collection_error"):
            res.verdict, res.skip_kind = "skip", "collection_error"
            res.message = next(r.message for r in runs if r.outcome == "collection_error")
        else:
            res.verdict, res.skip_kind = "skip", "test_missing"
        return res
