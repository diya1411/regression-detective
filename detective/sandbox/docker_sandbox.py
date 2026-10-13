"""Docker sandbox: a fresh, network-less, resource-capped container per probe.

Two phases (report §9):
  build  - dependencies are installed into a cached image keyed by the requirement set; this is the
           only phase with network access, and the build context contains only manifests (never repo code).
  test   - the exported commit is mounted read-only; network none, read-only rootfs, all capabilities
           dropped, non-root user, memory/CPU/PID caps, hard wall-clock timeout; container destroyed after.
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import time
from pathlib import Path

import docker
from docker.errors import APIError, BuildError, ImageNotFound

from ..config import Settings
from ..tools.git import GitRepo
from .base import MARK, RUNNER, BuildFailed, BuildInfo, Sandbox
from .deps import DepSpec

BASE_DOCKERFILE = """FROM python:{py}-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install "pytest>=7" "pytest-json-report>=1.5"
LABEL org.regression-detective.role=runner
"""

DEPS_DOCKERFILE = """FROM {base}
COPY requirements.rd.txt /rd-build/requirements.txt
{mirror_copy}
RUN pip install {find_links} -r /rd-build/requirements.txt && pip freeze --all > /rd-freeze.txt \\
    && echo RD_FREEZE_BEGIN && cat /rd-freeze.txt && echo RD_FREEZE_END
LABEL org.regression-detective.role=deps
"""


class DockerSandbox(Sandbox):
    kind = "docker"
    isolation = "container: network=none, read-only rootfs+source, cap_drop=ALL, uid 65534, mem/cpu/pid caps"

    def __init__(self, settings: Settings, repo: GitRepo, mirror: Path | None = None):
        super().__init__(settings, repo, mirror)
        self.client = docker.from_env(timeout=settings.build_timeout_s)
        self.client.ping()
        self.base_image = f"rd-runner:py{settings.python_version}"
        self.runner_dir = settings.work_dir / "runner"
        self.runner_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(RUNNER, self.runner_dir / "rd_runner.py")

    def describe(self) -> dict:
        return {"kind": self.kind, "isolation": self.isolation, "base_image": self.base_image,
                "mem_limit": self.settings.mem_limit, "cpus": self.settings.cpus,
                "timeout_s": self.settings.probe_timeout_s}

    def runner_packages(self) -> set[str]:
        self.ensure_base()
        if not hasattr(self, "_runner_pkgs"):
            self._runner_pkgs = set(self._read_freeze_cmd(self.base_image)) | super().runner_packages()
        return self._runner_pkgs

    def _read_freeze_cmd(self, tag: str) -> dict[str, str]:
        out = self.client.containers.run(tag, ["pip", "freeze", "--all"], remove=True, network_mode="none")
        return _freeze_dict(out.decode())

    # ------------------------------------------------------------------ images
    def _image_exists(self, tag: str) -> bool:
        try:
            self.client.images.get(tag)
            return True
        except ImageNotFound:
            return False

    def ensure_base(self) -> None:
        with self.key_lock("base"):
            if self._image_exists(self.base_image):
                return
            df = BASE_DOCKERFILE.format(py=self.settings.python_version)
            self._build_from({"Dockerfile": df.encode()}, self.base_image)

    def _build_from(self, files: dict[str, bytes], tag: str) -> str:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        buf.seek(0)
        logs = []
        try:
            for chunk in self.client.api.build(fileobj=buf, custom_context=True, tag=tag, rm=True,
                                               forcerm=True, decode=True, timeout=self.settings.build_timeout_s):
                if "stream" in chunk:
                    logs.append(chunk["stream"])
                if "error" in chunk:
                    logs.append(chunk["error"])
                    raise BuildError(chunk["error"], logs)
        except (BuildError, APIError) as e:
            raise BuildFailed("install_error", "".join(logs) + f"\n{e}")
        return "".join(logs)

    def spec_tag(self, spec: DepSpec) -> str:
        return f"rd-deps:{self.spec_key(spec)}"

    def is_cached(self, spec: DepSpec) -> bool:
        return self._image_exists(self.spec_tag(spec)) and self.load_freeze(self.spec_key(spec)) is not None

    def build(self, spec: DepSpec) -> BuildInfo:
        self.ensure_base()
        key = self.spec_key(spec)
        tag = f"rd-deps:{key}"
        t0 = time.time()
        with self.key_lock(key):
            if self._image_exists(tag):
                freeze = self.load_freeze(key)
                if freeze is None:
                    freeze = self._read_freeze(tag)
                    self.save_freeze(key, freeze)
                return BuildInfo(key, tag, True, time.time() - t0, freeze)
            files = {"requirements.rd.txt": ("\n".join(spec.lines()) + "\n").encode()}
            mirror_copy, find_links = "", ""
            if self.mirror and self.mirror.exists():
                for p in self.mirror.iterdir():
                    if p.suffix in (".whl", ".gz", ".json"):
                        files[f"mirror/{p.name}"] = p.read_bytes()
                mirror_copy = "COPY mirror/ /rd-build/mirror/"
                find_links = "--find-links /rd-build/mirror"
            files["Dockerfile"] = DEPS_DOCKERFILE.format(base=self.base_image, mirror_copy=mirror_copy,
                                                         find_links=find_links).encode()
            log = self._build_from(files, tag)
            freeze = _parse_freeze(log) or self._read_freeze(tag)
            self.save_freeze(key, freeze)
            return BuildInfo(key, tag, False, time.time() - t0, freeze)

    def _read_freeze(self, tag: str) -> dict[str, str]:
        out = self.client.containers.run(tag, ["cat", "/rd-freeze.txt"], remove=True, network_mode="none")
        return _freeze_dict(out.decode())

    # ------------------------------------------------------------------ execution
    def _execute(self, tree: Path, build: BuildInfo, cfg: dict, timeout: int):
        cfg = {**cfg, "workdir": "/work"}
        container = self.client.containers.run(
            build.image,
            command=["python", "/rd/rd_runner.py"],
            environment={"RD_CFG": json.dumps(cfg), "PYTHONDONTWRITEBYTECODE": "1",
                         "PYTHONPATH": "/work:/work/src", "HOME": "/tmp", "TMPDIR": "/tmp"},
            volumes={str(tree.resolve()): {"bind": "/work", "mode": "ro"},
                     str(self.runner_dir.resolve()): {"bind": "/rd", "mode": "ro"}},
            working_dir="/work",
            network_mode="none",
            read_only=True,
            tmpfs={"/tmp": "rw,size=512m,mode=1777"},
            mem_limit=self.settings.mem_limit,
            nano_cpus=int(self.settings.cpus * 1e9),
            pids_limit=512,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            user="65534:65534",
            detach=True,
            labels={"org.regression-detective.role": "probe"},
        )
        try:
            try:
                container.wait(timeout=timeout)
            except Exception:
                container.kill()
                return None, f"probe exceeded the hard timeout of {timeout}s and was killed"
            logs = container.logs(stdout=True, stderr=True).decode(errors="replace")
        finally:
            try:
                container.remove(force=True)
            except APIError:
                pass
        for line in reversed(logs.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):]), logs
        return None, logs


def _parse_freeze(log: str) -> dict[str, str]:
    if "RD_FREEZE_BEGIN" not in log:
        return {}
    body = log.split("RD_FREEZE_BEGIN", 1)[1].split("RD_FREEZE_END", 1)[0]
    return _freeze_dict(body)


def _freeze_dict(text: str) -> dict[str, str]:
    from packaging.utils import canonicalize_name

    out = {}
    for line in text.splitlines():
        line = line.strip()
        if "==" in line and not line.startswith("#"):
            name, ver = line.split("==", 1)
            out[canonicalize_name(name)] = ver.strip()
    return out


def docker_available() -> bool:
    try:
        docker.from_env(timeout=5).ping()
        return True
    except Exception:
        return False
