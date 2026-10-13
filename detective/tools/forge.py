"""Forge tool: CI history, issues/PRs and the CI failure archive; opening a draft PR.

Two adapters with one interface:
  * GitHubForge  - GitHub REST API (Actions runs, issues, pulls) via httpx
  * LocalForge   - the same data exported as JSON files (used by the eval corpus and offline demos)
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..redact import redact

PR_BRANCH_PREFIX = "regression-detective/"


@dataclass
class CIRun:
    id: str
    sha: str
    status: str                    # success | failure
    started_at: str
    env: dict = field(default_factory=dict)
    failed_tests: list[str] = field(default_factory=list)
    log: str = ""


class Forge:
    name = "none"

    def ci_runs(self) -> list[CIRun]:
        return []

    def history_items(self) -> list[dict]:
        return []

    def ci_archive(self) -> list[dict]:
        return []

    def failure_log(self, test_id: str, sha: str | None = None) -> str | None:
        for run in self.ci_runs():
            if run.status == "failure" and (sha is None or run.sha == sha) and run.log:
                if not run.failed_tests or test_id in run.failed_tests:
                    return run.log
        return None

    def last_green(self, test_id: str, before_sha: str | None, is_ancestor) -> CIRun | None:
        """Most recent successful run whose commit is an ancestor of the failing commit."""
        for run in self.ci_runs():  # newest first
            if run.status != "success":
                continue
            if before_sha and not is_ancestor(run.sha, before_sha):
                continue
            if run.sha == before_sha:
                continue
            return run
        return None

    def run_for_sha(self, sha: str) -> CIRun | None:
        for run in self.ci_runs():
            if run.sha == sha:
                return run
        return None

    def open_draft_pr(self, **kw) -> dict:
        raise NotImplementedError("this forge cannot open pull requests")


class LocalForge(Forge):
    """Reads <meta>/ci_runs.json, <meta>/history.json, <meta>/ci_archive.json."""

    name = "local"

    def __init__(self, meta_dir: Path):
        self.meta = Path(meta_dir)

    def _load(self, name: str) -> list:
        p = self.meta / name
        return json.loads(p.read_text()) if p.exists() else []

    def ci_runs(self) -> list[CIRun]:
        runs = [CIRun(**{k: v for k, v in r.items() if k in CIRun.__dataclass_fields__})
                for r in self._load("ci_runs.json")]
        return sorted(runs, key=lambda r: r.started_at, reverse=True)

    def history_items(self) -> list[dict]:
        return self._load("history.json")

    def ci_archive(self) -> list[dict]:
        return self._load("ci_archive.json")

    def open_draft_pr(self, repo_path: Path, work_dir: Path, base_sha: str, branch: str, title: str,
                      body: str, patch: bytes) -> dict:
        """No remote: commit the patch on a new local branch in a scratch clone and write the PR body."""
        clone = _scratch_clone(str(repo_path), work_dir, branch, base_sha, patch, title)
        (work_dir / "PR_BODY.md").write_text(body)
        return {"kind": "local-branch", "branch": branch, "clone": str(clone),
                "url": None, "body_path": str(work_dir / "PR_BODY.md")}


class GitHubForge(Forge):
    name = "github"

    def __init__(self, repo_url: str, token: str = "", branch: str = "main"):
        m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", repo_url)
        if not m:
            raise ValueError(f"not a GitHub URL: {repo_url}")
        self.owner, self.repo = m.group(1), m.group(2)
        self.url = repo_url
        self.token = token
        self.branch = branch
        self._runs: list[CIRun] | None = None
        self._client = httpx.Client(
            base_url="https://api.github.com", timeout=30,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     **({"Authorization": f"Bearer {token}"} if token else {})})

    def _get(self, path: str, **params):
        r = self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def ci_runs(self) -> list[CIRun]:
        if self._runs is None:
            try:
                data = self._get(f"/repos/{self.owner}/{self.repo}/actions/runs", branch=self.branch,
                                 per_page=60, event="push")
            except httpx.HTTPError:
                data = {"workflow_runs": []}
            self._runs = []
            for r in data.get("workflow_runs", []):
                if r.get("conclusion") not in ("success", "failure"):
                    continue
                self._runs.append(CIRun(id=str(r["id"]), sha=r["head_sha"], status=r["conclusion"],
                                        started_at=r.get("run_started_at") or r.get("created_at", ""),
                                        env={"workflow": r.get("name"), "path": r.get("path")}))
        return self._runs

    def run_env(self, run: CIRun) -> dict:
        try:
            jobs = self._get(f"/repos/{self.owner}/{self.repo}/actions/runs/{run.id}/jobs").get("jobs", [])
        except httpx.HTTPError:
            return run.env
        env = dict(run.env)
        if jobs:
            env["labels"] = sorted({l for j in jobs for l in j.get("labels", [])})
            env["runner_group"] = jobs[0].get("runner_group_name")
        return env

    def history_items(self) -> list[dict]:
        items = []
        try:
            issues = self._get(f"/repos/{self.owner}/{self.repo}/issues", state="closed", per_page=100,
                               sort="updated")
        except httpx.HTTPError:
            return items
        for it in issues:
            is_pr = "pull_request" in it
            files: list[str] = []
            if is_pr and len([i for i in items if i["kind"] == "pr"]) < 30:
                try:
                    files = [f["filename"] for f in
                             self._get(f"/repos/{self.owner}/{self.repo}/pulls/{it['number']}/files", per_page=100)]
                except httpx.HTTPError:
                    pass
            items.append({"kind": "pr" if is_pr else "issue", "number": it["number"], "title": it["title"],
                          "body": redact(it.get("body") or "")[:6000], "files": files,
                          "closed_at": it.get("closed_at"), "url": it.get("html_url"),
                          "labels": [l["name"] for l in it.get("labels", [])]})
        return items

    def ci_archive(self) -> list[dict]:
        """Past failures on the branch paired with the run that turned it green again."""
        runs = list(reversed(self.ci_runs()))  # oldest first
        out = []
        for i, r in enumerate(runs):
            if r.status != "failure" or (i and runs[i - 1].status == "failure"):
                continue
            fix = next((x for x in runs[i + 1:] if x.status == "success"), None)
            out.append({"id": r.id, "test_id": "", "message": f"workflow {r.env.get('workflow')} failed",
                        "log": "", "resolution": "unknown", "fix_commit": fix.sha if fix else None,
                        "notes": ""})
        return out

    def open_draft_pr(self, repo_path: Path, work_dir: Path, base_sha: str, branch: str, title: str,
                      body: str, patch: bytes, base_branch: str | None = None) -> dict:
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN is required to open a draft PR")
        remote = f"https://x-access-token:{self.token}@github.com/{self.owner}/{self.repo}.git"
        clone = _scratch_clone(self.url, work_dir, branch, base_sha, patch, title)
        _guarded_push(clone, remote, branch)
        r = self._client.post(f"/repos/{self.owner}/{self.repo}/pulls",
                              json={"title": title, "head": branch, "base": base_branch or self.branch,
                                    "body": body, "draft": True})
        r.raise_for_status()
        d = r.json()
        return {"kind": "github-draft-pr", "number": d["number"], "url": d["html_url"], "branch": branch}


# ----------------------------------------------------------------------------- repository safety
def _git(cwd: Path, *args: str, input: bytes | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, input=input, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {redact(proc.stderr.decode(errors='replace'))}")
    return proc.stdout.decode(errors="replace")


def _scratch_clone(source: str, work_dir: Path, branch: str, base_sha: str, patch: bytes, title: str) -> Path:
    if not branch.startswith(PR_BRANCH_PREFIX):
        raise PermissionError(f"refusing branch {branch!r}: agent branches must start with {PR_BRANCH_PREFIX}")
    clone = work_dir / "pr-clone"
    if clone.exists():
        subprocess.run(["rm", "-rf", str(clone)])
    work_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--quiet", "--no-checkout", source, str(clone)], check=True,
                   capture_output=True, timeout=600)
    _git(clone, "checkout", "--quiet", "-b", branch, base_sha)
    _git(clone, "apply", "--index", "--whitespace=nowarn", input=patch)
    _git(clone, "-c", "user.name=Regression Detective", "-c", "user.email=regression-detective@localhost",
         "commit", "--quiet", "-m", title)
    return clone


def _guarded_push(clone: Path, remote: str, branch: str) -> None:
    protected = {"main", "master", "develop", "trunk"}
    if branch in protected or not branch.startswith(PR_BRANCH_PREFIX):
        raise PermissionError("the agent may only push its own regression-detective/* branches")
    # no --force, no refspec tricks: exactly one new branch
    _git(clone, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
