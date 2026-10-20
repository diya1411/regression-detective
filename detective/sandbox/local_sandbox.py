"""Process sandbox: one cached virtualenv per dependency set; each probe is a fresh subprocess.

Every probe runs on a throwaway `git archive` export in the system temp directory (never inside a
git repository), with:
  * a scrubbed environment (no host variables, tokens or credentials are inherited)
  * HTTP(S) proxies pointed at a dead port, so standard HTTP clients cannot reach the network
  * its own process group, killed as a whole on the hard wall-clock timeout
  * rlimits: CPU seconds, max file size, open files, no core dumps
Dependency installation (the only step that needs the network) happens once per requirement set,
from manifests only; the repository's own setup.py is never executed.

This is process-level containment, not a VM: raw sockets and reads of world-readable files remain
possible, which is stated in every report's safety section.
"""
from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

from packaging.utils import canonicalize_name

from .base import MARK, RUNNER, BuildFailed, BuildInfo, Sandbox
from .deps import DepSpec

DEAD_PROXY = "http://127.0.0.1:9"   # discard port: connections are refused immediately
MAX_FILE_BYTES = 512 * 1024 * 1024


class LocalSandbox(Sandbox):
    kind = "local"
    isolation = ("process sandbox: throwaway export outside any repo, scrubbed env, HTTP(S) proxied to a dead "
                 "port, own process group, CPU/file-size/open-file rlimits, hard timeout")

    def describe(self) -> dict:
        return {"kind": self.kind, "isolation": self.isolation, "python": sys.version.split()[0],
                "timeout_s": self.settings.probe_timeout_s}

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
                    freeze[canonicalize_name(n)] = v
            self.save_freeze(key, freeze)
            (venv / ".rd-ready").write_text("ok")
            return BuildInfo(key, str(venv), False, time.time() - t0, freeze)

    def _execute(self, tree: Path, build: BuildInfo, cfg: dict, timeout: int):
        cfg = {**cfg, "workdir": str(tree)}
        home = tree.parent / f"{tree.name}-home"
        home.mkdir(exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "RD_CFG": json.dumps(cfg), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": f"{tree}:{tree / 'src'}", "HOME": str(home), "TMPDIR": str(home),
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
               "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY, "all_proxy": DEAD_PROXY,
               "NO_PROXY": "", "no_proxy": "", "PIP_NO_INDEX": "1"}
        cpu = int(timeout) + 5

        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
            except (ValueError, OSError):
                pass

        proc = subprocess.Popen([str(Path(build.image) / "bin" / "python"), str(RUNNER)], cwd=tree, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True, preexec_fn=limits)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)   # the whole process group, not just the runner
            except ProcessLookupError:
                pass
            proc.communicate()
            return None, f"probe exceeded the hard timeout of {timeout}s and its process group was killed"
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
        for line in reversed(out.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):]), out + err
        return None, out + err
