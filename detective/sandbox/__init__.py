from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..tools.git import GitRepo
from .base import BuildFailed, BuildInfo, ProbeRequest, ProbeResult, Sandbox
from .deps import DepSpec, discover


def make_sandbox(settings: Settings, repo: GitRepo, mirror: Path | None = None) -> Sandbox:
    from .local_sandbox import LocalSandbox

    return LocalSandbox(settings, repo, mirror)


__all__ = ["make_sandbox", "Sandbox", "ProbeRequest", "ProbeResult", "BuildInfo", "BuildFailed", "DepSpec",
           "discover"]
