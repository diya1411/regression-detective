"""Dependency agent: lockfile/manifest diff, registry time-travel, and the pin-and-rerun experiment."""
from __future__ import annotations

from datetime import datetime, timezone

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ..policy import Action
from ..sandbox import BuildFailed, ProbeRequest
from ..sandbox.deps import MANIFEST_FILES
from ..state import ActionResult, Investigation
from .base import Ctx, probe_raw

# The sandbox runner installs its own test tooling; those packages are only suspects if the repo declares them.
INFRA = {"pip", "setuptools", "wheel", "pytest", "pluggy", "iniconfig", "packaging", "pytest-json-report",
         "pytest-metadata"}
MAX_PACKAGES = 40


def diff(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    repo, lg, head = ctx.repo, inv.last_good, inv.head
    spec_lg, spec_head = ctx.sandbox.dep_spec(lg.sha), ctx.sandbox.dep_spec(head.sha)
    manifest_diffs = {}
    for f in MANIFEST_FILES:
        a_txt, b_txt = repo.show_file(lg.sha, f), repo.show_file(head.sha, f)
        if a_txt != b_txt:
            manifest_diffs[f] = repo.diff(lg.sha, head.sha, [f]) if (a_txt and b_txt) else \
                f"{f}: {'added' if b_txt else 'removed'}"
    try:
        freeze = ctx.sandbox.build(spec_head).freeze
    except BuildFailed:
        freeze = {}

    lg_when = datetime.fromtimestamp(lg.timestamp, tz=timezone.utc)
    head_when = datetime.fromtimestamp(head.timestamp, tz=timezone.utc)
    declared_lg, declared_head = spec_lg.direct(), spec_head.direct()
    names = list(dict.fromkeys(list(declared_head) + list(declared_lg) +
                               list(freeze)))[:MAX_PACKAGES * 2]
    infra = INFRA | ctx.sandbox.runner_packages()
    names = [n for n in names if n not in infra or n in declared_head or n in declared_lg]
    suspects, table = {}, []
    for name in names:
        now = freeze.get(name)
        spec = declared_lg.get(name, "")
        pinned = _pinned(spec)
        then = pinned or ctx.registry.version_at(name, spec, lg_when)
        in_window = [r.version for r in ctx.registry.releases_between(name, lg_when, head_when)]
        row = {"package": name, "spec_at_last_good": spec or "(transitive)", "version_at_last_good": then,
               "installed_today_at_head": now, "released_in_window": in_window}
        table.append(row)
        if now and then and now != then:
            suspects[name] = {"lg_version": then, "head_version": now, "released_in_window": in_window}

    inv.dep = {"suspects": suspects, "table": table, "manifest_diffs": list(manifest_diffs),
               "lg_date": lg_when.isoformat(), "head_date": head_when.isoformat()}
    raw = "\n".join(f"{r['package']:<28} last-good spec {r['spec_at_last_good']:<18} "
                    f"then={r['version_at_last_good']} now={r['installed_today_at_head']} "
                    f"released in window: {', '.join(r['released_in_window']) or '-'}" for r in table)
    raw += "\n\nregistry calls: " + ", ".join(ctx.registry.calls[-60:])
    raw += "\n\n" + "\n".join(manifest_diffs.values())
    obs = ctx.evidence.record("DEPENDENCY", "pypi+git", "diff manifests and registry across window",
                              (f"{len(suspects)} package(s) differ from last-good: " +
                               ", ".join(f"{k} {v['lg_version']} → {v['head_version']}" for k, v in suspects.items()))
                              if suspects else f"no package differs from its last-good version ({len(table)} checked)",
                              inv.dep, raw, kind="api")
    if suspects:
        txt = ", ".join(f"{k} {v['lg_version']} → {v['head_version']}" for k, v in list(suspects.items())[:3])
        inv.claim("DEPENDENCY", f"Package versions differ between last-good and HEAD environments: {txt}.",
                  [obs.id], check={"type": "dep_diff", "evidence": obs.id, "suspects": sorted(suspects)})
        return ActionResult(a, "suspects", [obs.id], "Lockfile diff across window", "",
                            {"detail": txt + (" released inside window" if any(v["released_in_window"] for v in suspects.values()) else ""),
                             "tree_label": f"{len(suspects)} suspect pkg|{list(suspects)[0][:16]}"})
    inv.claim("DEPENDENCY", f"No checked package version differs from what last-good would have installed "
                            f"({len(table)} packages).", [obs.id], check={"type": "dep_diff", "evidence": obs.id,
                                                                           "suspects": []})
    return ActionResult(a, "none", [obs.id], "Lockfile diff across window", "dim",
                        {"detail": f"no dependency drift across {len(table)} packages", "tree_label": "no drift|in window"})


def pin_test(ctx: Ctx, inv: Investigation, a: Action) -> ActionResult:
    suspects = inv.dep.get("suspects", {})
    pins = {k: v["lg_version"] for k, v in suspects.items()}
    r, obs = _pinned_run(ctx, inv, pins, "pin all suspects")
    if r.verdict == "skip":
        return ActionResult(a, None, [obs.id], f"pinned environment not installable ({r.skip_kind})", "dim",
                            {"tree_label": "pin|uninstallable"})
    pin_txt = ", ".join(f"{k}=={v}" for k, v in pins.items())
    passed = r.verdict == "pass"
    inv.claim("DEPENDENCY", f"With {pin_txt} at unchanged HEAD code, the test {'passes' if passed else 'still fails'}.",
              [obs.id], check={"type": "probe", "evidence": obs.id, "sha": inv.head.sha, "verdict": r.verdict})
    ev = [obs.id]
    if passed:
        culprits = list(pins)
        if len(pins) > 1:   # isolate: one pin at a time
            culprits = []
            for name, ver in list(pins.items())[:4]:
                r1, o1 = _pinned_run(ctx, inv, {name: ver}, f"pin {name} only")
                ev.append(o1.id)
                if r1.verdict == "pass":
                    culprits.append(name)
            culprits = culprits or list(pins)
        inv.dep["culprit_packages"] = culprits
        return ActionResult(a, "pass", ev, f"Pinned {pin_txt}, re-ran test → <ok>passes</ok>", "ok",
                            {"detail": "dependency confirmed by experiment; repository code is not to blame",
                             "tree_label": f"pin {list(pins)[0][:14]}|test passes"})
    return ActionResult(a, "fail", ev, f"Pinned {pin_txt} back, re-ran test → <no>still fails</no>", "dim",
                        {"detail": "H3 eliminated by experiment, not by reasoning",
                         "tree_label": f"pin {list(pins)[0][:14]}|still fails"})


def _pinned_run(ctx: Ctx, inv: Investigation, pins: dict, label: str):
    spec = ctx.sandbox.dep_spec(inv.head.sha).with_pins(pins)
    r = ctx.sandbox.probe(ProbeRequest(inv.head.sha, [inv.test_id], deps=spec, label=label))
    inv.probe_count += 1
    if r.build and not r.build.cached:
        inv.cost.observe_build(r.build.seconds)
    obs = ctx.evidence.record("DEPENDENCY", "sandbox", f"{label} at HEAD {inv.head.short}",
                              f"HEAD with {', '.join(f'{k}=={v}' for k, v in pins.items())}: {r.verdict.upper()}",
                              {"sha": inv.head.sha, "pins": pins, "verdict": r.verdict, "skip_kind": r.skip_kind},
                              probe_raw(r))
    return r, obs


def _pinned(spec: str) -> str | None:
    if spec.startswith("==") and "*" not in spec and "," not in spec:
        return spec[2:]
    return None


def pin_line(name: str, version: str, manifest_text: str) -> tuple[str, str] | None:
    """(old_line, new_line) that pins `name` to `version` in a requirements file, or None."""
    for line in manifest_text.splitlines():
        s = line.split("#")[0].strip()
        if not s:
            continue
        try:
            if canonicalize_name(Requirement(s).name) == canonicalize_name(name):
                return line, f"{Requirement(s).name}=={version}  # pinned by Regression Detective"
        except Exception:
            continue
    return None
