"""Local fallback: one virtualenv per requirement set, tests run as subprocesses with a timeout.

NO isolation: historical commits execute with the user's privileges and network. It exists for
machines without Docker and is refused unless RD_ALLOW_UNSAFE_LOCAL=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .base import MARK, RUNNER, BuildFailed, BuildInfo, Sandbox
from .deps import DepSpec


class LocalSandbox(Sandbox):
    kind = "local"
    isolation = "NONE (subprocess + virtualenv; network and filesystem are not restricted)"

    def _venv(self, key: str) -> Path:
        return self.settings.cache_dir / "venvs" / key

    def is_cached(self, spec: DepSpec) -> bool:
        return (self._venv(self.spec_key(spec)) / ".rd-ready").exists()

    def build(self, spec: DepSpec) -> BuildInfo:
        key = self.spec_key(spec)
        venv = self._venv(key)
        t0 = time.time()
        with self.key_lock(key):
            if (venv / ".rd-ready").exists():
                return BuildInfo(key, str(venv), True, time.time() - t0, self.load_freeze(key) or {})
            venv.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, "-m", "venv", "--clear", str(venv)], check=True, capture_output=True)
            py = str(venv / "bin" / "python")
            req = venv / "requirements.rd.txt"
            req.write_text("\n".join(spec.lines() + ["pytest>=7", "pytest-json-report>=1.5"]) + "\n")
            args = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)]
            if self.mirror and self.mirror.exists():
                args += ["--find-links", str(self.mirror)]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=self.settings.build_timeout_s)
            if proc.returncode != 0:
                raise BuildFailed("install_error", proc.stdout + proc.stderr)
            fr = subprocess.run([py, "-m", "pip", "freeze", "--all"], capture_output=True, text=True)
            freeze = {}
            for line in fr.stdout.splitlines():
                if "==" in line:
                    n, v = line.split("==", 1)
                    from packaging.utils import canonicalize_name
                    freeze[canonicalize_name(n)] = v
            self.save_freeze(key, freeze)
            (venv / ".rd-ready").write_text("ok")
            return BuildInfo(key, str(venv), False, time.time() - t0, freeze)

    def _execute(self, tree: Path, build: BuildInfo, cfg: dict, timeout: int):
        cfg = {**cfg, "workdir": str(tree)}
        env = {"PATH": os.environ.get("PATH", ""), "RD_CFG": json.dumps(cfg), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": f"{tree}:{tree / 'src'}", "HOME": str(tree), "TMPDIR": str(tree)}
        try:
            proc = subprocess.run([str(Path(build.image) / "bin" / "python"), str(RUNNER)], cwd=tree, env=env,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"probe exceeded the hard timeout of {timeout}s"
        logs = proc.stdout + proc.stderr
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):]), logs
        return None, logs
