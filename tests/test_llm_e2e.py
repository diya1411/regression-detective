"""Full investigation with the LLM path ON, using a fake Gemini client that returns schema-valid JSON.

Proves every agent's Gemini branch (triage, hypothesis, explanation, patch, verifier, embeddings)
parses and acts on structured output, without a key or network.
"""
import hashlib
import json
from types import SimpleNamespace

CACHE_BLOCK = ("    if item.sku not in _line_cache:\n        _line_cache[item.sku] = item.unit_price * item.qty\n"
               "    return _line_cache[item.sku]")
FIXED = ("    key = (item.sku, item.unit_price, item.qty)\n    if key not in _line_cache:\n"
         "        _line_cache[key] = item.unit_price * item.qty\n    return _line_cache[key]")


class FakeModels:
    def __init__(self):
        self.roles = []

    def generate_content(self, model, contents, config):
        sysmsg = config.system_instruction
        role = next((r for r in ("Triage", "Hypothesis", "Explanation", "Patch") if f"{r} agent" in sysmsg),
                    "Verifier" if "Evidence Verifier" in sysmsg else "?")
        self.roles.append(role)
        if "Triage" in sysmsg:
            out = {"exception_type": "AssertionError", "assertion": "assert Decimal('118.80') == Decimal('120.00')",
                   "observed": "Decimal('118.80')", "expected": "Decimal('120.00')", "delta": 1.2,
                   "suspect_modules": ["checkout/pricing.py", "checkout/cart.py"],
                   "summary": "total stays at the pre-reprice value"}
        elif "Hypothesis agent" in sysmsg:
            pri = {"commit": .6, "flaky": .05, "dependency": .2, "environment": .1, "fixture": .05}
            out = {"hypotheses": [{"kind": k, "title": f"{k} hypothesis", "prior": p, "rationale": "base rates",
                                   "confirm_if": "x", "eliminate_if": "y"} for k, p in pri.items()]}
        elif "Explanation agent" in sysmsg:
            data = json.loads(contents)
            ids = [e["id"] for e in data["evidence"] if e["tool"] in ("git", "sandbox")][-3:]
            out = {"summary": "line totals are cached per SKU and survive a reprice",
                   "mechanism": "line_total memoises item.unit_price * item.qty keyed only by sku",
                   "why_assertion_breaks": "after reprice the cached 118.80 is returned instead of 120.00",
                   "classification": "defect", "classification_rationale": "PR #412 requires current prices",
                   "recommended_fix": "patch_code",
                   "claims": [{"text": "The cache key ignores the unit price.", "evidence": ids[:1], "kind": "inferred"},
                              {"text": "Reverting the commit makes the test pass.", "evidence": ids[-1:],
                               "kind": "observed"},
                              {"text": "Invented claim.", "evidence": ["E999"], "kind": "observed"}]}
        elif "Patch agent" in sysmsg:
            data = json.loads(contents)
            assert CACHE_BLOCK in data["files_at_head"]["checkout/pricing.py"]
            out = {"rationale": "key the cache by price and quantity too", "target": "code",
                   "edits": [{"path": "checkout/pricing.py", "search": CACHE_BLOCK, "replace": FIXED}]}
        else:   # verifier
            data = json.loads(contents)
            out = {"verdicts": [{"index": c["index"], "verdict": "supported", "reason": "matches"}
                                for c in data["claims"]]}
        return SimpleNamespace(text=json.dumps(out), usage_metadata=None)

    def embed_content(self, model, contents, config):
        vals = []
        for t in contents:
            h = hashlib.sha256(t.encode()).digest()
            vals.append(SimpleNamespace(values=[b / 255 for b in (h * 24)[:768]]))
        return SimpleNamespace(embeddings=vals)


def test_llm_path_end_to_end(tmp_path, monkeypatch):
    import google.genai
    from eval.build_fixtures import CASES
    from detective.config import Settings
    from detective.graph import Params, run_investigation

    fake = FakeModels()
    monkeypatch.setattr(google.genai, "Client", lambda api_key=None, **kw: SimpleNamespace(models=fake))
    d = tmp_path / "c1"
    d.mkdir()
    CASES["c1_single_defect"](d)
    truth = json.loads((d / "truth.json").read_text())
    s = Settings().with_(gemini_api_key="fake", offline=False, home=tmp_path / "home", gemini_models=("fake-model",))
    inv = run_investigation(Params(repo=str(d / "repo"), test=truth["test"], last_good=truth["last_good"],
                                   head=truth["head"], meta=str(d / "meta")), s)
    rep = inv.report
    assert rep["status"] == "resolved", rep.get("notes")
    assert [c["sha"] for c in rep["culprits"]] == truth["culprits"]
    assert rep["explanation"]["source"] == "gemini"
    assert rep["patch"]["status"] == "validated" and rep["patch"]["source"] == "gemini"
    assert "key = (item.sku" in rep["patch"]["diff"]
    # the invented evidence id was stripped, so that claim fails the deterministic citation check
    claims = rep["verification"]["claims"]
    invented = [c for c in claims if c["text"] == "Invented claim."]
    assert invented and invented[0]["verdict"] == "unsupported"
    # an "inferred" claim is never upgraded to supported by the auditor
    assert [c for c in claims if c["text"].startswith("The cache key")][0]["verdict"] == "inferred"
    assert {"Triage", "Hypothesis", "Explanation", "Patch", "Verifier"} <= set(fake.roles)
    assert rep["budget"]["llm"]["calls"] >= 5
