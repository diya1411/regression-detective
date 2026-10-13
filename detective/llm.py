"""Gemini wrapper: structured JSON output against a schema, model fallback, redaction, call budget.

When no key is configured (or --offline is set) every call returns None and each agent falls back
to its deterministic path, so the whole system still runs end to end.
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .redact import redact


class LLM:
    def __init__(self, settings: Settings, log_dir: Path | None = None, bus=None):
        self.settings = settings
        self.enabled = settings.llm_enabled
        self.models = list(settings.gemini_models)
        self.model: str | None = None
        self.calls = 0
        self.failures = 0
        self.tokens = {"prompt": 0, "output": 0}
        self.log_dir = log_dir
        self.bus = bus
        self._lock = threading.Lock()
        self.client = None
        if self.enabled:
            from google import genai

            self.client = genai.Client(api_key=settings.gemini_api_key)
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        return {"enabled": self.enabled, "model": self.model or (self.models[0] if self.models else None),
                "calls": self.calls, "failures": self.failures, "tokens": self.tokens}

    def json(self, role: str, system: str, prompt: str, schema: dict, temperature: float = 0.2,
             max_tokens: int = 8192) -> dict | None:
        if not self.enabled or self.client is None:
            return None
        with self._lock:
            if self.calls >= self.settings.budget.max_llm_calls:
                return None
            self.calls += 1
            n = self.calls
        from google.genai import errors, types

        prompt = redact(prompt)
        models = [self.model] if self.model else list(self.models)
        last_err = ""
        for model in models:
            for attempt in range(4):
                try:
                    t0 = time.time()
                    resp = self.client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system, temperature=temperature, max_output_tokens=max_tokens,
                            response_mime_type="application/json", response_json_schema=schema))
                    text = resp.text or ""
                    data = _loads(text)
                    um = getattr(resp, "usage_metadata", None)
                    if um:
                        self.tokens["prompt"] += getattr(um, "prompt_token_count", 0) or 0
                        self.tokens["output"] += getattr(um, "candidates_token_count", 0) or 0
                    self._log(n, role, model, system, prompt, text, time.time() - t0)
                    if data is None:
                        last_err = "response was not valid JSON"
                        continue
                    self.model = model
                    return data
                except errors.APIError as e:
                    last_err = f"{e.code} {e.status}: {e.message}"
                    if e.code in (429, 500, 502, 503, 504):
                        time.sleep(min(30, 2 ** attempt + random.random()))
                        continue
                    break   # 400/403/404: try the next model in the list
                except Exception as e:  # network etc.
                    last_err = repr(e)
                    time.sleep(1 + attempt)
            if self.model:  # a previously working model failed: re-open the fallback list
                self.model = None
                models = [m for m in self.models if m != model]
        self.failures += 1
        self._log(n, role, "-", system, prompt, f"ERROR: {last_err}", 0)
        if self.bus:
            self.bus.emit("llm.error", role=role, error=last_err[:300])
        return None

    def _log(self, n: int, role: str, model: str, system: str, prompt: str, out: str, secs: float) -> None:
        if not self.log_dir:
            return
        (self.log_dir / f"{n:03d}-{role}.json").write_text(json.dumps(
            {"role": role, "model": model, "seconds": round(secs, 2), "system": system, "prompt": prompt,
             "response": out}, indent=1))


def _loads(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                return None
    return None
