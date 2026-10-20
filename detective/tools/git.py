"""Git tool. Read-only against the investigated repository: every probe works on an exported copy.

The only writes the agent ever performs happen in a separate clone made for a draft PR
(see tools/forge.py), and that path refuses to touch the default branch.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path

_SEP = "\x1f"


class GitError(RuntimeError):
    pass


@dataclass
class Commit:
    sha: str
    short: str
    subject: str
    author: str
    date: str          # ISO 8601
    timestamp: int
    parents: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class GitRepo:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not (self.path / ".git").exists() and not (self.path / "HEAD").exists():
            raise GitError(f"not a git repository: {self.path}")

    # ------------------------------------------------------------------ plumbing
    def _run(self, *args: str, input: bytes | None = None, check: bool = True) -> bytes:
        proc = subprocess.run(["git", "-C", str(self.path), *args], input=input,
                              capture_output=True, timeout=120)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def _text(self, *args: str, check: bool = True) -> str:
        return self._run(*args, check=check).decode("utf-8", errors="replace")

    @classmethod
    def clone(cls, url: str, dest: Path) -> "GitRepo":
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and (dest / ".git").exists():
            subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin"],
                           capture_output=True, timeout=600)
            return cls(dest)
        proc = subprocess.run(["git", "clone", "--quiet", url, str(dest)],
                              capture_output=True, timeout=900)
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode(errors="replace"))
        return cls(dest)

    # ------------------------------------------------------------------ queries
    def rev_parse(self, ref: str) -> str:
        return self._text("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def commit(self, ref: str) -> Commit:
        fmt = _SEP.join(["%H", "%h", "%s", "%an", "%aI", "%at", "%P"])
        out = self._text("show", "-s", f"--format={fmt}", ref).rstrip("\n")
        sha, short, subject, author, date, ts, parents = out.split(_SEP)
        return Commit(sha, short[:7], subject, author, date, int(ts), parents.split())

    def first_parent_range(self, good: str, bad: str) -> list[Commit]:
        """[good, c1, ..., bad] along the first-parent chain (merge commits count as one step)."""
        shas = self._text("rev-list", "--first-parent", "--reverse", f"{good}..{bad}").split()
        return [self.commit(good)] + [self.commit(s) for s in shas]

    def ancestors(self, sha: str, n: int) -> list[str]:
        """First-parent ancestors of sha, nearest first, excluding sha itself."""
        out = self._text("rev-list", "--first-parent", f"--max-count={n + 1}", sha).split()
        return out[1:]

    def is_ancestor(self, a: str, b: str) -> bool:
        proc = subprocess.run(["git", "-C", str(self.path), "merge-base", "--is-ancestor", a, b],
                              capture_output=True)
        return proc.returncode == 0

    def show_file(self, sha: str, path: str) -> str | None:
        proc = subprocess.run(["git", "-C", str(self.path), "show", f"{sha}:{path}"],
                              capture_output=True, timeout=60)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")

    def ls_files(self, sha: str) -> list[str]:
        return self._text("ls-tree", "-r", "--name-only", sha).splitlines()

    def changed_files(self, sha: str) -> list[tuple[str, str]]:
        """(status, path) for a commit against its first parent."""
        c = self.commit(sha)
        base = c.parents[0] if c.parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree
        out = self._text("diff", "--name-status", "--no-renames", base, sha)
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append((parts[0][0], parts[-1]))
        return rows

    def commit_diff(self, sha: str, context: int = 3) -> str:
        c = self.commit(sha)
        base = c.parents[0] if c.parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        return self._text("diff", f"-U{context}", "--no-color", base, sha)

    def commit_patch(self, sha: str) -> bytes:
        """Binary-safe patch of a commit against its first parent (used for revert experiments)."""
        c = self.commit(sha)
        base = c.parents[0] if c.parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        return self._run("diff", "--binary", "--no-color", base, sha)

    def diff(self, a: str, b: str, paths: list[str] | None = None) -> str:
        args = ["diff", "--no-color", a, b]
        if paths:
            args += ["--", *paths]
        return self._text(*args)

    def log_for_paths(self, paths: list[str], until: str, n: int = 10) -> list[Commit]:
        if not paths:
            return []
        fmt = "%H"
        shas = self._text("log", f"--format={fmt}", f"-n{n}", until, "--", *paths).split()
        return [self.commit(s) for s in shas]

    def message(self, sha: str) -> str:
        return self._text("show", "-s", "--format=%B", sha).strip()

    # ------------------------------------------------------------------ export
    def export(self, sha: str, dest: Path) -> Path:
        """Materialise a commit's tree into dest without touching the repository's worktree."""
        dest.mkdir(parents=True, exist_ok=True)
        data = self._run("archive", "--format=tar", sha)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            safe = [m for m in tf.getmembers() if not _unsafe_member(m)]
            try:
                tf.extractall(dest, members=safe, filter="data")
            except TypeError:  # Python without tarfile extraction filters
                tf.extractall(dest, members=safe)
        return dest


def _unsafe_member(m: tarfile.TarInfo) -> bool:
    name = m.name
    return name.startswith("/") or ".." in Path(name).parts or m.issym() and (
        m.linkname.startswith("/") or ".." in Path(m.linkname).parts)


def apply_patch(tree: Path, patch: bytes, reverse: bool = False) -> tuple[bool, str]:
    """Apply a git-format patch to an exported tree (no repository needed)."""
    args = ["git", "apply", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    # Without a ceiling, git would discover an enclosing repository and silently skip "outside" paths.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(tree).resolve().parent)}
    proc = subprocess.run(args, cwd=tree, input=patch, capture_output=True, timeout=60, env=env)
    if proc.returncode != 0:
        # later commits may have shifted the surrounding lines: retry with one line of context
        proc2 = subprocess.run(args + ["-C1"], cwd=tree, input=patch, capture_output=True, timeout=60, env=env)
        if proc2.returncode == 0:
            return True, "applied with reduced context (-C1)"
    ok = proc.returncode == 0
    return ok, proc.stderr.decode(errors="replace")


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text: str) -> dict[str, dict]:
    """{path: {"old": [(start, end)], "new": [(start, end)]}}: ranges of lines actually changed
    (context lines excluded). A pure deletion marks the line it sits next to on the new side."""
    files: dict[str, dict] = {}
    cur_old = cur_new = key = None
    o = n = 0
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            cur_old = None if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("a/")
            continue
        if line.startswith("+++ "):
            cur_new = None if line[4:].strip() == "/dev/null" else line[4:].strip().removeprefix("b/")
            key = cur_new or cur_old
            if key:
                files.setdefault(key, {"old": [], "new": [], "old_path": cur_old, "new_path": cur_new})
            continue
        m = _HUNK.match(line)
        if m:
            o, n = int(m[1]), int(m[3])
            continue
        if key is None or line.startswith("\\"):
            continue
        if line.startswith("-"):
            _add(files[key]["old"], o)
            _add(files[key]["new"], max(n, 1))
            o += 1
        elif line.startswith("+"):
            _add(files[key]["new"], n)
            n += 1
        else:
            o += 1
            n += 1
    return files


def _add(ranges: list[tuple[int, int]], line: int) -> None:
    if ranges and ranges[-1][1] in (line - 1, line):
        ranges[-1] = (ranges[-1][0], line)
    else:
        ranges.append((line, line))
