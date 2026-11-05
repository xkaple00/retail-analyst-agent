import pytest

from analyst.errors import LLMUnavailable
from analyst.llm import GeminiLLM, LLMResponse


class _Err(Exception):
    def __init__(self, code):
        super().__init__(f"http {code}")
        self.code = code


class _FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.models = self
        self.seen_models = []

    def generate_content(self, model, contents, config):
        self.seen_models.append(model)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o

        class P:  # minimal response shape
            text = "hi"
            function_call = None

        class C:
            class content:
                parts = [P()]

        class R:
            candidates = [C()]
            usage_metadata = type("U", (), {"prompt_token_count": 3, "candidates_token_count": 1})()
            text = "ALLOW"

        return R()


def _llm(outcomes, monkeypatch, attempts=3):
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = GeminiLLM.__new__(GeminiLLM)
    llm._client = _FakeClient(outcomes)
    llm.model, llm.fallback_model, llm.max_attempts = "primary", "fallback", attempts
    monkeypatch.setattr(GeminiLLM, "_to_contents", lambda self, m: [])
    monkeypatch.setattr(GeminiLLM, "_config", lambda self, s, t, temperature: None)
    return llm


def test_retry_then_success(monkeypatch):
    llm = _llm([_Err(429), _Err(503), None], monkeypatch)
    r = llm.chat("s", [], [])
    assert isinstance(r, LLMResponse) and r.text == "hi" and r.retries == 2 and not r.fallback_used
    assert llm._client.seen_models == ["primary"] * 3


def test_fallback_after_retries(monkeypatch):
    llm = _llm([_Err(503)] * 3 + [None], monkeypatch)
    r = llm.chat("s", [], [])
    assert r.fallback_used and r.model == "fallback"


def test_bad_request_is_not_retried_on_the_fallback(monkeypatch):
    """400/401/403 would only be repeated by the second model: fail fast with the reason."""
    llm = _llm([_Err(400), None], monkeypatch)
    with pytest.raises(LLMUnavailable, match="rejected the request"):
        llm.chat("s", [], [])
    assert llm._client.seen_models == ["primary"]


def test_unknown_model_falls_back(monkeypatch):
    """A wrong ANALYST_MODEL name (404) is exactly when the fallback model helps."""
    llm = _llm([_Err(404), None], monkeypatch)
    r = llm.chat("s", [], [])
    assert r.fallback_used and llm._client.seen_models == ["primary", "fallback"]


def test_history_keeps_thought_signatures_for_replay():
    """Gemini 3 rejects a replayed function call without its thought_signature (400), on the fallback
    model too, so the model's own content is replayed verbatim everywhere."""
    from google.genai import types

    llm = GeminiLLM.__new__(GeminiLLM)
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="run_sql", args={"sql": "SELECT 1"}), thought_signature=b"sig")])
    out = llm._to_contents([{"role": "user", "text": "hi"}, {"role": "assistant", "text": "", "calls": [], "raw": raw}])
    assert out[1] is raw and out[1].parts[0].thought_signature == b"sig"


def test_history_raw_content_restored_from_dict_is_replayed():
    from google.genai import types

    llm = GeminiLLM.__new__(GeminiLLM)
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="get_schema", args={}), thought_signature=b"s")])
    as_dict = raw.model_dump(mode="json", exclude_none=True)
    out = llm._to_contents([{"role": "assistant", "text": "", "raw": as_dict}])
    assert out[0].parts[0].function_call.name == "get_schema" and out[0].parts[0].thought_signature == b"s"



def test_total_outage_raises(monkeypatch):
    llm = _llm([_Err(503)] * 6, monkeypatch)
    with pytest.raises(LLMUnavailable):
        llm.chat("s", [], [])


def test_default_is_one_retry_then_fallback(monkeypatch):
    """Production setting (max_attempts=2): one retry on the primary, then the fallback model."""
    llm = _llm([_Err(429), _Err(503), None], monkeypatch, attempts=2)
    r = llm.chat("s", [], [])
    assert r.retries == 1 and r.fallback_used and llm._client.seen_models == ["primary", "primary", "fallback"]
