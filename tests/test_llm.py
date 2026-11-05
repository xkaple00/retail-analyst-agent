import pytest

from analyst.errors import LLMUnavailable
from analyst.llm import _PRIMARY_DOWN_UNTIL, GeminiLLM, LLMResponse


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


class _Timeout(Exception):
    pass


def _llm(outcomes, monkeypatch, attempts=3):
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = GeminiLLM.__new__(GeminiLLM)
    llm._client = _FakeClient(outcomes)
    llm.model, llm.fallback_model, llm.max_attempts = "primary", "fallback", attempts
    llm.primary_cooldown_s = 300.0
    _PRIMARY_DOWN_UNTIL.clear()
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


def test_hung_primary_is_not_retried_and_falls_back_at_once(monkeypatch):
    """A client-side timeout means the primary is hanging: retrying it would hang again."""
    llm = _llm([_Timeout("The operation has timed out"), None], monkeypatch)
    r = llm.chat("s", [], [])
    assert r.fallback_used and llm._client.seen_models == ["primary", "fallback"]


def test_primary_breaker_skips_the_primary_during_cooldown(monkeypatch):
    """After one failure the next calls go straight to the fallback until the cooldown ends."""
    llm = _llm([_Err(503), _Err(503), None, None, None], monkeypatch, attempts=2)
    llm.chat("s", [], [])
    assert llm._client.seen_models == ["primary", "primary", "fallback"]
    r = llm.chat("s", [], [])  # breaker open: no primary attempt at all
    assert r.fallback_used and r.retries == 0 and llm._client.seen_models[-1] == "fallback" and len(llm._client.seen_models) == 4
    _PRIMARY_DOWN_UNTIL.clear()  # cooldown over
    llm.chat("s", [], [])
    assert llm._client.seen_models[-1] == "primary"


def test_explicit_model_bypasses_the_breaker(monkeypatch):
    """classify()/judge pass a model explicitly; the breaker is only for the default primary."""
    llm = _llm([None], monkeypatch)
    _PRIMARY_DOWN_UNTIL["primary"] = 10**12
    llm.chat("s", [], [], model="primary")
    assert llm._client.seen_models == ["primary"]


def test_breaker_is_shared_between_client_instances(monkeypatch):
    """An agent (and its LLM client) is built per request; the second request must not pay the timeout again."""
    a = _llm([_Timeout("timed out"), None], monkeypatch)
    a.chat("s", [], [])
    b = GeminiLLM.__new__(GeminiLLM)
    b._client = _FakeClient([None]); b.model, b.fallback_model, b.max_attempts, b.primary_cooldown_s = "primary", "fallback", 2, 300.0
    r = b.chat("s", [], [])
    assert r.fallback_used and b._client.seen_models == ["fallback"]


def test_fallback_chain_tries_models_in_order(monkeypatch):
    """ANALYST_FALLBACK_MODEL may be a comma list: primary -> a -> b, each skipped for the cooldown after a failure."""
    llm = _llm([_Err(503), _Err(503), _Err(503), _Err(503), None], monkeypatch, attempts=2)
    llm.fallback_model = "fb1,fb2"
    r = llm.chat("s", [], [])
    assert r.fallback_used and r.model == "fb2"
    assert llm._client.seen_models == ["primary", "primary", "fb1", "fb1", "fb2"]
    llm._client.outcomes.append(None)
    r2 = llm.chat("s", [], [])  # both earlier models cooling down: straight to fb2
    assert r2.model == "fb2" and llm._client.seen_models[-1] == "fb2" and len(llm._client.seen_models) == 6


def test_whole_chain_down_names_every_model(monkeypatch):
    llm = _llm([_Err(503)] * 6, monkeypatch, attempts=2)
    llm.fallback_model = "fb1,fb2"
    with pytest.raises(LLMUnavailable, match="primary, fb1, fb2"):
        llm.chat("s", [], [])
