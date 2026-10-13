"""AST tool: semantic chunking, diff-to-function mapping, import closure, and static I/O scans."""
from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

_IDENT_SPLIT = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


@dataclass
class Chunk:
    path: str
    qualname: str
    kind: str          # module | class | function | method
    start: int
    end: int
    text: str
    docstring: str = ""
    identifiers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def split_identifier(name: str) -> list[str]:
    parts = []
    for piece in re.split(r"[_\W]+", name):
        parts.extend(p.lower() for p in _IDENT_SPLIT.findall(piece))
    return parts


def chunk_python(source: str, path: str) -> list[Chunk]:
    """Function/class-level chunks, so a retrieved chunk is a semantic unit rather than a window."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [Chunk(path, path, "module", 1, source.count("\n") + 1, source[:6000])]
    lines = source.splitlines()
    chunks: list[Chunk] = []

    def text_of(node) -> str:
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        return "\n".join(lines[start - 1: node.end_lineno])

    def idents(node) -> list[str]:
        names = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
        return sorted(names)

    def visit(body, prefix: str, in_class: bool):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{node.name}"
                chunks.append(Chunk(path, q, "method" if in_class else "function", node.lineno,
                                    node.end_lineno, text_of(node), ast.get_docstring(node) or "",
                                    idents(node)))
            elif isinstance(node, ast.ClassDef):
                q = f"{prefix}{node.name}"
                header_end = node.body[0].lineno - 1 if node.body else node.end_lineno
                head = "\n".join(lines[node.lineno - 1: max(header_end, node.lineno)])
                chunks.append(Chunk(path, q, "class", node.lineno, node.end_lineno, head,
                                    ast.get_docstring(node) or "", [node.name]))
                visit(node.body, q + ".", True)

    visit(tree.body, "", False)
    # module-level remainder (imports, constants) as one chunk
    covered = set()
    for c in chunks:
        if c.kind in ("function", "class"):
            covered.update(range(c.start, c.end + 1))
    rest = [l for i, l in enumerate(lines, 1) if i not in covered and l.strip()]
    if rest:
        chunks.append(Chunk(path, f"{path}:<module>", "module", 1, len(lines), "\n".join(rest)[:4000],
                            ast.get_docstring(tree) or "", []))
    return chunks


def defs_overlapping(source: str, path: str, ranges: list[tuple[int, int]]) -> list[Chunk]:
    out = []
    for c in chunk_python(source, path):
        if c.kind == "class" or c.kind == "module":
            continue
        if any(a <= c.end and b >= c.start for a, b in ranges):
            out.append(c)
    return out


# ----------------------------------------------------------------------------- imports
def module_map(paths: Iterable[str]) -> dict[str, str]:
    """dotted module name -> repo path, handling src/ layouts and packages."""
    m: dict[str, str] = {}
    for p in paths:
        if not p.endswith(".py"):
            continue
        for root in ("", "src/"):
            if root and not p.startswith(root):
                continue
            rel = p[len(root):]
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            m.setdefault(mod, p)
    return m


def imported_modules(source: str, path: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    pkg_parts = path[:-3].split("/")[:-1]
    if pkg_parts and pkg_parts[0] == "src":
        pkg_parts = pkg_parts[1:]
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - node.level + 1] if node.level > 1 else pkg_parts
                prefix = ".".join(base)
                mod = f"{prefix}.{node.module}" if node.module else prefix
            else:
                mod = node.module or ""
            if mod:
                mods.add(mod)
                for a in node.names:
                    mods.add(f"{mod}.{a.name}")
    return mods


def import_closure(entry_files: list[str], all_paths: list[str],
                   read: Callable[[str], str | None], limit: int = 400) -> set[str]:
    """Repo files statically reachable from the entry files (test file + its conftests)."""
    mm = module_map(all_paths)
    seen: set[str] = set()
    queue = [p for p in entry_files if p in set(all_paths)]
    while queue and len(seen) < limit:
        p = queue.pop()
        if p in seen:
            continue
        seen.add(p)
        src = read(p)
        if src is None:
            continue
        for mod in imported_modules(src, p):
            parts = mod.split(".")
            for i in range(len(parts), 0, -1):  # longest prefix that is a repo module
                target = mm.get(".".join(parts[:i]))
                if target:
                    if target not in seen:
                        queue.append(target)
                    # importing a.b.c also executes a/__init__ and a/b/__init__
                    for j in range(1, i):
                        init = mm.get(".".join(parts[:j]))
                        if init and init not in seen:
                            queue.append(init)
                    break
    return seen


def conftests_for(test_path: str, all_paths: list[str]) -> list[str]:
    parts = test_path.split("/")[:-1]
    out = []
    for i in range(len(parts) + 1):
        cand = "/".join(parts[:i] + ["conftest.py"])
        if cand in all_paths:
            out.append(cand)
    return out


# ----------------------------------------------------------------------------- static scans
_NETWORK_MODULES = {"requests", "httpx", "urllib", "urllib3", "aiohttp", "socket", "http.client",
                    "ftplib", "boto3", "botocore", "google.cloud", "pymongo", "psycopg2", "redis"}
_NONDET_CALLS = {"random", "randint", "choice", "shuffle", "uniform", "sample", "now", "today",
                 "time", "monotonic", "perf_counter", "uuid4", "urandom", "sleep", "getrandbits"}


def external_io_scan(sources: dict[str, str]) -> list[dict]:
    """Find network access, environment reads and out-of-repo file reads in the given files."""
    findings = []
    for path, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for n in names:
                    if any(n == m or n.startswith(m + ".") for m in _NETWORK_MODULES):
                        findings.append({"file": path, "line": node.lineno, "kind": "network-import",
                                         "detail": n})
            elif isinstance(node, ast.Call):
                fn = _call_name(node.func)
                if fn in ("os.getenv", "os.environ.get", "getenv"):
                    findings.append({"file": path, "line": node.lineno, "kind": "env-read", "detail": fn})
                elif fn in ("open", "Path.open", "read_text", "read_bytes", "json.load", "np.load"):
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("/"):
                        findings.append({"file": path, "line": node.lineno, "kind": "absolute-path-read",
                                         "detail": arg.value})
            elif isinstance(node, ast.Subscript) and _call_name(node.value) == "os.environ":
                findings.append({"file": path, "line": node.lineno, "kind": "env-read", "detail": "os.environ[...]"})
    return findings


def nondeterminism_scan(sources: dict[str, str]) -> list[dict]:
    findings = []
    for path, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        seeded = "seed(" in src
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = _call_name(node.func)
                leaf = fn.split(".")[-1]
                if leaf in _NONDET_CALLS and (fn.startswith(("random", "time", "datetime", "uuid", "os",
                                                              "secrets", "np.random", "numpy.random"))
                                              or fn == leaf):
                    findings.append({"file": path, "line": node.lineno, "kind": "nondeterministic-call",
                                     "detail": fn, "seeded": seeded})
    return findings


def _call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def function_source(source: str, name: str) -> str | None:
    """Source of a (possibly Class.method) definition, or None."""
    for c in chunk_python(source, "?"):
        if c.qualname == name or c.qualname.endswith("." + name):
            return c.text
    return None
