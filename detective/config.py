"""Runtime settings, read from the environment (and .env if present)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env_list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


@dataclass(frozen=True)
class Budget:
    """Declared resource budget for one investigation (see report §9, resource ethics)."""

    max_probes: int = 16          # sandboxed test executions (bisection, confirmation, pin tests)
    max_seconds: float = 1800.0   # wall clock
    max_llm_calls: int = 40
    max_patch_attempts: int = 3


@dataclass(frozen=True)
class Settings:
    # Outside ~/Desktop and ~/Documents on purpose: those are privacy-protected on macOS and Docker's
    # bind mounts of probe workspaces would block on a permission prompt.
    home: Path = field(default_factory=lambda: Path(os.environ.get(
        "RD_HOME", "~/.regression-detective")).expanduser().resolve())
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
                                or os.environ.get("GOOGLE_API_KEY", ""))
    gemini_models: tuple[str, ...] = field(default_factory=lambda: tuple(_env_list(
        "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-2.5-flash")))
    gemini_embed_model: str = field(default_factory=lambda: os.environ.get(
        "GEMINI_EMBED_MODEL", "gemini-embedding-001"))
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    sandbox: str = field(default_factory=lambda: os.environ.get("RD_SANDBOX", "docker"))
    allow_unsafe_local: bool = field(default_factory=lambda: os.environ.get(
        "RD_ALLOW_UNSAFE_LOCAL", "") == "1")
    python_version: str = field(default_factory=lambda: os.environ.get("RD_PYTHON", "3.12"))

    offline: bool = False            # never call an LLM, even if a key is present
    use_history_index: bool = True   # RAG retrieval point one (priors from CI archive)
    flaky_runs: int = 10             # N runs at HEAD and N at last-good
    confidence_threshold: float = 0.9
    report_threshold: float = 0.7    # verifier: below this, the patch proposal is suppressed
    max_parallel: int = 2
    probe_timeout_s: int = 120       # hard wall clock per sandboxed test run
    build_timeout_s: int = 600
    mem_limit: str = "1g"
    cpus: float = 2.0
    open_pr: bool = False
    budget: Budget = field(default_factory=Budget)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gemini_api_key) and not self.offline

    @property
    def investigations_dir(self) -> Path:
        return self.home / "investigations"

    @property
    def work_dir(self) -> Path:
        return self.home / "work"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    def with_(self, **kw) -> "Settings":
        return replace(self, **kw)
