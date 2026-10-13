"""The Gemini code paths, exercised with a fake client (no network, no key)."""
from types import SimpleNamespace

from detective.config import Settings
from detective.llm import LLM, _loads


def make_llm(responder, models=("m-missing", "m-ok")):
    s = Settings().with_(offline=False, gemini_models=models)
    llm = LLM(s.with_(gemini_api_key="x"))
    calls = []

    def generate_content(model, contents, config):
        calls.append(model)
        return responder(model, contents, config)

    llm.enabled = True
    llm.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    return llm, calls


def test_model_fallback_and_schema_config():
    from google.genai import errors

    def responder(model, contents, config):
        if model == "m-missing":
            raise errors.ClientError(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema["required"] == ["a"]
        return SimpleNamespace(text='{"a": 1}', usage_metadata=None)

    llm, calls = make_llm(responder)
    out = llm.json("t", "sys", "prompt with token ghp_" + "x" * 36, {"type": "object", "required": ["a"]})
    assert out == {"a": 1}
    assert calls == ["m-missing", "m-ok"] and llm.model == "m-ok"
    # the working model is remembered
    llm.json("t", "sys", "p", {"type": "object", "required": ["a"]})
    assert calls[-1] == "m-ok"


def test_redaction_before_send():
    seen = {}

    def responder(model, contents, config):
        seen["c"] = contents
        return SimpleNamespace(text="{}", usage_metadata=None)

    llm, _ = make_llm(responder, models=("m",))
    llm.json("t", "s", "API_KEY=abcdef123456", {"type": "object"})
    assert "abcdef123456" not in seen["c"]


def test_budget_and_offline():
    llm, calls = make_llm(lambda m, c, k: SimpleNamespace(text="{}", usage_metadata=None), models=("m",))
    llm.settings = llm.settings.with_(budget=llm.settings.budget.__class__(max_llm_calls=1))
    assert llm.json("t", "s", "p", {}) == {}
    assert llm.json("t", "s", "p", {}) is None      # budget exhausted
    off = LLM(Settings().with_(offline=True, gemini_api_key="x"))
    assert off.json("t", "s", "p", {}) is None


def test_lenient_json():
    assert _loads('```json\n{"x": 2}\n```') == {"x": 2}
    assert _loads('noise {"y": 3} trailing') == {"y": 3}
    assert _loads("not json") is None
