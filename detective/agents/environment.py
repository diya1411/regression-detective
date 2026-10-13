"""Cheap eliminations: CI environment comparison (H4) and static scan for external fixtures (H5)."""
from __future__ import annotations

import json

from ..policy import Action
from ..state import ActionResult, Investigation
from ..tools.ast_tools import external_io_scan
from .base import Ctx

CI_CONFIG = (".github/workflows/", ".gitlab-ci.yml", "tox.ini", "noxfile.py", ".python-version", "runtime.txt",
             "Dockerfile", ".circleci/", "azure-pipelines.yml", "Jenkinsfile", ".travis.yml")
ENV_KEYS = ("runner_image", "image_digest", "python", "os", "labels", "env")


def env_check(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    repo, lg, head = ctx.repo, inv.last_good, inv.head
    cfg_files = sorted({p for p in repo.ls_files(lg.sha) + repo.ls_files(head.sha)
                        if any(p.startswith(c) or p == c for c in CI_CONFIG)})
    changed_cfg = [p for p in cfg_files if repo.show_file(lg.sha, p) != repo.show_file(head.sha, p)]
    run_lg, run_head = ctx.forge.run_for_sha(lg.sha), ctx.forge.run_for_sha(head.sha)
    if run_head is None:
        run_head = next((r for r in ctx.forge.ci_runs() if r.status == "failure"), None)
    env_lg = getattr(ctx.forge, "run_env", lambda r: r.env)(run_lg) if run_lg else {}
    env_head = getattr(ctx.forge, "run_env", lambda r: r.env)(run_head) if run_head else {}
    env_diff = {k: (env_lg.get(k), env_head.get(k)) for k in sorted(set(env_lg) | set(env_head))
                if k in ENV_KEYS and env_lg.get(k) != env_head.get(k)}
    have_runs = bool(env_lg and env_head)
    changed = bool(changed_cfg or env_diff)
    inv.env = {"ci_config_files": cfg_files, "changed_config": changed_cfg, "env_last_good": env_lg,
               "env_failing": env_head, "env_diff": env_diff, "runner_metadata": have_runs}
    raw = json.dumps(inv.env, indent=1, default=str) + "\n\n" + "\n".join(
        repo.diff(lg.sha, head.sha, [p]) for p in changed_cfg)
    summary = (f"CI config changed: {', '.join(changed_cfg)}" if changed_cfg else "CI config unchanged") + \
              (f"; runner metadata differs: {', '.join(env_diff)}" if env_diff else
               ("; runner metadata unchanged" if have_runs else "; no runner metadata available"))
    obs = ctx.evidence.record("ENVIRONMENT", "git+ci", "compare CI environment", summary, inv.env, raw,
                              kind="api")
    inv.claim("ENVIRONMENT", summary + ".", [obs.id], check={"type": "env", "evidence": obs.id, "changed": changed})
    if changed:
        what = (list(env_diff) + changed_cfg)[0]
        return ActionResult(a, "changed", [obs.id], "CI environment differs across window", "no",
                            {"detail": summary, "tree_label": f"{str(what)[:18]}|changed"})
    label = "runner digest|unchanged" if have_runs and any(k in env_lg for k in ("image_digest", "runner_image")) \
        else "CI config|unchanged"
    return ActionResult(a, "unchanged", [obs.id], "CI environment unchanged", "dim",
                        {"detail": summary, "tree_label": label})


def fixture_scan(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    sources = {p: ctx.repo.show_file(inv.head.sha, p) or "" for p in inv.import_closure}
    findings = external_io_scan(sources)
    external = [f for f in findings if f["kind"] in ("network-import", "absolute-path-read")]
    raw = "\n".join(f"{f['file']}:{f['line']} {f['kind']} {f['detail']}" for f in findings) or "no findings"
    raw += "\n\nscanned files:\n" + "\n".join(sources)
    summary = (f"{len(external)} external I/O site(s) in the test path: " +
               ", ".join(f"{f['file']}:{f['line']} {f['detail']}" for f in external[:3])) if external else \
        f"no network or absolute-path reads in {len(sources)} files reachable from the test"
    obs = ctx.evidence.record("ENVIRONMENT", "ast", "scan test path for external fixtures", summary,
                              {"findings": findings, "files": list(sources)}, raw, kind="static")
    inv.claim("ENVIRONMENT", summary + ".", [obs.id], kind="observed",
              check={"type": "static", "evidence": obs.id, "external": len(external)})
    if external:
        return ActionResult(a, "external", [obs.id], "External data found in test path", "no",
                            {"detail": summary, "tree_label": f"{external[0]['detail'][:16]}|external I/O"})
    return ActionResult(a, "none", [obs.id], "No external fixtures in test path", "dim",
                        {"detail": "static analysis only — marked unreachable, not tested",
                         "tree_label": "no network|fixtures"})
