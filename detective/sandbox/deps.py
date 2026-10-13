"""Static dependency discovery. Manifests are parsed, never executed (a historical setup.py can
contain anything), and the resulting requirement set keys the cached dependency image."""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ImportError:  # pragma: no cover - python < 3.11
    import tomli as tomllib  # type: ignore

REQ_FILES = ["requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements_dev.txt",
             "requirements/base.txt", "requirements/test.txt", "requirements/dev.txt", "test-requirements.txt"]
TEST_EXTRAS = ("test", "tests", "testing", "dev")
MANIFEST_FILES = REQ_FILES + ["pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "Pipfile.lock"]


@dataclass
class DepSpec:
    requirements: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    pins: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out, seen = [], set()
        for line in self.requirements:
            name = req_name(line)
            if name and name in self.pins:
                continue
            if line not in seen:
                seen.add(line)
                out.append(line)
        for name, ver in sorted(self.pins.items()):
            out.append(f"{name}=={ver}")
        return out

    def key(self, python_version: str, extra: str = "") -> str:
        h = hashlib.sha256()
        h.update(f"py{python_version}\n{extra}\n".encode())
        for line in sorted(self.lines()):
            h.update(line.encode() + b"\n")
        return h.hexdigest()[:16]

    def direct(self) -> dict[str, str]:
        """{canonical name: specifier string}"""
        out = {}
        for line in self.requirements:
            try:
                r = Requirement(line)
            except InvalidRequirement:
                continue
            out[canonicalize_name(r.name)] = str(r.specifier)
        return out

    def with_pins(self, pins: dict[str, str]) -> "DepSpec":
        return DepSpec(list(self.requirements), list(self.sources), {**self.pins, **pins})


def req_name(line: str) -> str | None:
    try:
        return canonicalize_name(Requirement(line).name)
    except InvalidRequirement:
        return None


def discover(read: Callable[[str], str | None]) -> DepSpec:
    spec = DepSpec()
    for f in REQ_FILES:
        txt = read(f)
        if txt is not None:
            spec.sources.append(f)
            spec.requirements += _parse_requirements(txt, f, read, depth=0)
    py = read("pyproject.toml")
    if py is not None:
        try:
            data = tomllib.loads(py)
        except Exception:
            data = {}
        proj = data.get("project", {})
        deps = list(proj.get("dependencies", []) or [])
        for extra, items in (proj.get("optional-dependencies") or {}).items():
            if extra in TEST_EXTRAS:
                deps += items
        poetry = (data.get("tool", {}) or {}).get("poetry", {}) or {}
        for name, v in (poetry.get("dependencies") or {}).items():
            if name.lower() != "python":
                deps.append(_poetry_req(name, v))
        if deps:
            spec.sources.append("pyproject.toml")
            spec.requirements += [d for d in deps if req_name(d)]
    setup = read("setup.py")
    if setup is not None and "setup.py" not in spec.sources:
        reqs = _static_setup_requires(setup)
        if reqs:
            spec.sources.append("setup.py (static parse)")
            spec.requirements += reqs
    spec.requirements = [r for r in spec.requirements if r.strip()]
    return spec


def _parse_requirements(txt: str, path: str, read, depth: int) -> list[str]:
    out = []
    for raw in txt.splitlines():
        line = raw.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ")) and depth < 3:
            inc = line.split(None, 1)[1].strip()
            base = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
            sub = read(base + inc) or read(inc)
            if sub:
                out += _parse_requirements(sub, base + inc, read, depth + 1)
            continue
        if line.startswith("-") or line in (".", "./") or line.startswith((".", "/", "file:", "git+")):
            continue  # editable installs / local paths / index options: the repo is on PYTHONPATH instead
        if req_name(line):
            out.append(line)
    return out


def _poetry_req(name: str, v) -> str:
    if isinstance(v, dict):
        v = v.get("version", "*")
    v = str(v).strip()
    if v in ("*", ""):
        return name
    if v.startswith("^"):
        base = v[1:]
        major = base.split(".")[0]
        return f"{name}>={base},<{int(major) + 1}" if major.isdigit() else name
    if v.startswith("~"):
        return f"{name}~={v.lstrip('~=')}"
    if re.match(r"^\d", v):
        return f"{name}=={v}"
    return f"{name}{v}"


def _static_setup_requires(src: str) -> list[str]:
    """Pull install_requires / tests_require out of setup.py *without running it*."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup":
            for kw in node.keywords:
                if kw.arg in ("install_requires", "tests_require"):
                    try:
                        val = ast.literal_eval(kw.value)
                        out += [v for v in val if isinstance(v, str)]
                    except ValueError:
                        pass
    return out
