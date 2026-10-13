"""Evidence Verifier: every claim must trace to a recorded tool observation.

Two passes:
  1. deterministic - citations exist; structured claims (probe verdicts, run counts, suite results)
     are compared field-by-field with the observation they cite
  2. adversarial   - Gemini audits free-text claims against the cited excerpts (offline: a strict
     token-overlap check that can only ever downgrade a claim to "inferred")
Below the confidence threshold the patch proposal is suppressed and the output is downgraded to a
findings summary that lists what evidence is still missing.
"""
from __future__ import annotations

import json
import re

from ..state import Investigation
from .base import Ctx
from .prompts import VERIFIER_SCHEMA, VERIFIER_SYSTEM

_SPECIFIC = re.compile(r"\b[0-9a-f]{7,40}\b|\b\d+(?:\.\d+)?\b|\b\w+_\w+\b|\b\w+\.\w+\b")
WEIGHT = {"supported": 1.0, "inferred": 0.5, "unsupported": 0.0}


def run(ctx: Ctx, inv: Investigation) -> Investigation:
    claims = [dict(cl) for cl in inv.claims] + [dict(cl) for cl in (inv.explanation or {}).get("claims", [])]
    results = []
    pending = []
    for i, cl in enumerate(claims):
        v, reason = _deterministic(ctx, cl)
        results.append({**cl, "index": i, "verdict": v, "reason": reason, "checked_by": "deterministic"})
        if v is None:
            pending.append(i)

    if pending:
        items = []
        for i in pending:
            cl = claims[i]
            items.append({"index": i, "claim": cl["text"], "declared_kind": cl.get("kind"),
                          "evidence": [{"id": e, "summary": ctx.evidence.get(e).summary,
                                        "excerpt": ctx.evidence.raw(e)[:1800]} for e in cl["evidence"]]})
        out = ctx.llm.json("verifier", VERIFIER_SYSTEM, json.dumps({"claims": items}, indent=1), VERIFIER_SCHEMA,
                           temperature=0.0)
        by_idx = {v["index"]: v for v in (out or {}).get("verdicts", []) if isinstance(v.get("index"), int)}
        for i in pending:
            cl = claims[i]
            if i in by_idx and by_idx[i].get("verdict") in WEIGHT:
                v = by_idx[i]["verdict"]
                if cl.get("kind") == "inferred" and v == "supported":
                    v = "inferred"     # the author's own label wins when it is the weaker one
                results[i].update(verdict=v, reason=by_idx[i].get("reason", ""), checked_by="gemini-auditor")
            else:
                v, reason = _overlap(ctx, cl)
                results[i].update(verdict=v, reason=reason, checked_by="token-overlap")

    n = len(results) or 1
    counts = {k: sum(r["verdict"] == k for r in results) for k in WEIGHT}
    frac = sum(WEIGHT[r["verdict"]] for r in results) / n
    c = inv.conclusion or {}
    p = c.get("p", 0.0)
    conf = p * (0.7 + 0.3 * frac)
    if any(r["verdict"] == "unsupported" and r.get("kind") == "observed" for r in results):
        conf *= 0.85
    if not c.get("confirmed"):
        conf = min(conf, 0.6)
    conf = round(conf, 3)
    patch_ok = bool(inv.patch and inv.patch.get("status") == "validated")
    allowed = conf >= ctx.settings.report_threshold and c.get("confirmed", False) and patch_ok
    missing = [r["text"] for r in results if r["verdict"] == "unsupported"]
    if not c.get("confirmed"):
        top = inv.tree.h.get(c.get("hypothesis"))
        if top is not None:
            from ..belief import CONFIRMATION_RULE
            rule = CONFIRMATION_RULE.get(top.kind)
            if rule:
                missing.append(f"untested: {rule}")
    inv.verification = {"claims": results, "counts": counts, "supported_fraction": round(frac, 3),
                        "confidence": conf, "threshold": ctx.settings.report_threshold,
                        "patch_allowed": allowed, "downgraded": not allowed and patch_ok,
                        "missing_evidence": missing, "auditor": "gemini" if ctx.llm.enabled else "offline"}
    ctx.bus.emit("verification", **{k: v for k, v in inv.verification.items() if k != "claims"},
                 claims=[{k: r.get(k) for k in ("text", "evidence", "kind", "verdict", "reason", "agent", "checked_by")}
                         for r in results])
    ctx.bus.say("VERIFIER", f"Auditing {len(results)} claims against evidence log",
                f"{counts['supported']}/{len(results)} backed by recorded tool output · {counts['inferred']} inferred · "
                f"{counts['unsupported']} unsupported · confidence {conf:.2f}"
                + ("" if allowed or not patch_ok else " · below threshold → patch suppressed"))
    return inv


def _deterministic(ctx: Ctx, cl: dict) -> tuple[str | None, str]:
    ids = cl.get("evidence") or []
    if not ids:
        return "unsupported", "no evidence cited"
    unknown = [e for e in ids if ctx.evidence.get(e) is None]
    if unknown:
        return "unsupported", f"cites non-existent evidence {', '.join(unknown)}"
    chk = cl.get("check") or {}
    t = chk.get("type")
    if not t:
        return None, ""
    obs = ctx.evidence.get(chk.get("evidence", ids[0]))
    d = obs.data if obs else {}
    if t == "probe":
        ok = d.get("verdict") == chk.get("verdict") and (not chk.get("sha") or d.get("sha") == chk["sha"])
        return ("supported", f"{obs.id}: verdict={d.get('verdict')}") if ok else \
            ("unsupported", f"{obs.id} records verdict={d.get('verdict')}")
    if t == "runs":
        ok = d.get("fails") == chk.get("fails") and d.get("passes") == chk.get("passes")
        return ("supported", f"{obs.id}: {d.get('fails')} fail / {d.get('passes')} pass") if ok else \
            ("unsupported", f"{obs.id} records {d.get('fails')} fail / {d.get('passes')} pass")
    if t == "suite":
        ok = len(d.get("collateral", [])) == chk.get("collateral", 0)
        return ("supported", f"{obs.id}: {len(d.get('collateral', []))} newly broken") if ok else \
            ("unsupported", f"{obs.id} lists collateral {d.get('collateral')}")
    if t == "dep_diff":
        ok = sorted(d.get("suspects", {})) == sorted(chk.get("suspects", []))
        return ("supported", f"{obs.id}: suspects {sorted(d.get('suspects', {}))}") if ok else \
            ("unsupported", f"{obs.id} suspects differ")
    if t in ("env", "static", "window"):
        return "supported", f"{obs.id}: recorded {obs.tool} observation"
    return None, ""


def _overlap(ctx: Ctx, cl: dict) -> tuple[str, str]:
    if cl.get("kind") == "inferred":
        return "inferred", "declared as inference by its author"
    text = " ".join(f"{ctx.evidence.get(e).summary}\n{ctx.evidence.raw(e)}" for e in cl["evidence"])
    toks = set(_SPECIFIC.findall(cl["text"]))
    if not toks:
        return "inferred", "no checkable specifics in the claim"
    hit = {t for t in toks if t in text}
    if len(hit) / len(toks) >= 0.6:
        return "supported", f"{len(hit)}/{len(toks)} specifics found in cited output"
    return "inferred", f"only {len(hit)}/{len(toks)} specifics found in cited output"
