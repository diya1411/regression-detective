from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..tools.git import GitRepo
from .base import BuildFailed, BuildInfo, ProbeRequest, ProbeResult, Sandbox
from .deps import DepSpec, discover


def make_sandbox(settings: Settings, repo: GitRepo, mirror: Path | None = None) -> Sandbox:
    if settings.sandbox == "docker":
        from .docker_sandbox import DockerSandbox, docker_available

        if docker_available():
            return DockerSandbox(settings, repo, mirror)
        if not settings.allow_unsafe_local:
            raise RuntimeError("Docker is not reachable. Start Docker, or set RD_SANDBOX=local and "
                               "RD_ALLOW_UNSAFE_LOCAL=1 to run historical commits WITHOUT isolation.")
    if settings.sandbox == "local" or settings.allow_unsafe_local:
        if not settings.allow_unsafe_local:
            raise RuntimeError("The local sandbox has no isolation; set RD_ALLOW_UNSAFE_LOCAL=1 to accept that.")
        from .local_sandbox import LocalSandbox

        return LocalSandbox(settings, repo, mirror)
    raise RuntimeError(f"unknown sandbox {settings.sandbox!r}")


__all__ = ["make_sandbox", "Sandbox", "ProbeRequest", "ProbeResult", "BuildInfo", "BuildFailed", "DepSpec",
           "discover"]
