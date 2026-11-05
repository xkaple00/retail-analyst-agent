"""Provider-neutral chat/tool-calling interface with a Gemini implementation and a fake.

Messages are plain dicts so the agent loop never depends on the SDK:
  {"role": "user"|"assistant"|"tool", "text": str, "calls": [ToolCall], "call_id"/"name"/"result"}
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import LLMUnavailable

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    retries: int = 0
    fallback_used: bool = False
    raw_parts: Any = None  # provider content to echo back into history


class LLM(Protocol):
    def chat(self, system: str, messages: list[dict], tools: list[dict], model: str | None = None) -> LLMResponse: ...
    def classify(self, system: str, text: str, model: str | None = None) -> str: ...


RETRYABLE_CODES = {408, 409, 429, 500, 502, 503, 504}
# Client-side errors that a second model would only repeat (bad request, bad key, no access).
NO_FALLBACK_CODES = {400, 401, 403}


def _status_code(err: Exception) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(err, attr, None)
        if isinstance(v, int):
            return v
    return None


class GeminiLLM:
    def __init__(self, api_key: str, model: str, fallback_model: str | None = None, max_attempts: int = 2) -> None:
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set (see .env.example)")
        from google import genai
        from google.genai import types

        # The SDK retries 5xx on its own for minutes; we own retry + fallback, so keep it to one attempt.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=60_000, retry_options=types.HttpRetryOptions(attempts=1)),
        )
        self.model = model
        self.fallback_model = fallback_model
        self.max_attempts = max_attempts

    # --- conversions -------------------------------------------------
    def _to_contents(self, messages: list[dict]):
        from google.genai import types

        contents = []
        for m in messages:
            role = m["role"]
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=m["text"])]))
            elif role == "assistant":
                raw = m.get("raw")
                if raw is not None:
                    # Replay the model's own Content verbatim: Gemini 3 needs the thought_signature
                    # on function-call parts, which a rebuilt part would lose. The same history is
                    # replayed to the fallback model: verified live (2026-11-04) that signatures from
                    # one Gemini 3 model are accepted by the other, while a stripped one is a 400.
                    # A dict is a Content restored from the message log.
                    contents.append(types.Content.model_validate(raw) if isinstance(raw, dict) else raw)
                    continue
                parts = []
                if m.get("text"):
                    parts.append(types.Part.from_text(text=m["text"]))
                for c in m.get("calls", []):
                    parts.append(types.Part.from_function_call(name=c.name, args=c.args))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_function_response(name=m["name"], response={"result": m["result"]})],
                    )
                )
        return contents

    def _config(self, system: str, tools: list[dict], temperature: float):
        from google.genai import types

        kwargs: dict[str, Any] = {"system_instruction": system, "temperature": temperature}
        if tools:
            decls = [
                types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=t["parameters"])
                for t in tools
            ]
            kwargs["tools"] = [types.Tool(function_declarations=decls)]
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        return types.GenerateContentConfig(**kwargs)

    # --- calls --------------------------------------------------------
    def _generate(self, model: str, contents, config) -> tuple[Any, int]:
        """Call with retry on transient errors. Returns (response, retries)."""
        retries = 0
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                return self._client.models.generate_content(model=model, contents=contents, config=config), retries
            except Exception as e:  # google.genai.errors.APIError and network errors
                code = _status_code(e)
                transient = code in RETRYABLE_CODES or code is None
                last = e
                if not transient or attempt == self.max_attempts - 1:
                    break
                retries += 1
                delay = min(8.0, (2**attempt) + random.random())
                hinted = re.search(r"retry in ([\d.]+)s", str(e))
                if code == 429 and hinted:  # honour the server's RetryInfo (free tier: 5 RPM)
                    delay = min(15.0, float(hinted.group(1)) + 0.5)
                log.warning("LLM call failed (%s), retry %d in %.1fs", code or type(e).__name__, retries, delay)
                time.sleep(delay)
        raise last if last else RuntimeError("unreachable")

    def chat(self, system: str, messages: list[dict], tools: list[dict], model: str | None = None) -> LLMResponse:
        contents = self._to_contents(messages)
        config = self._config(system, tools, temperature=0.2)
        primary = model or self.model
        fallback_used = False
        retries = self.max_attempts - 1  # retries spent on the primary before giving up on it
        try:
            resp, retries = self._generate(primary, contents, config)
        except Exception as e:
            if not self.fallback_model or self.fallback_model == primary:
                raise LLMUnavailable(f"model API unavailable: {e}") from e
            if _status_code(e) in NO_FALLBACK_CODES:
                raise LLMUnavailable(f"model API rejected the request ({_status_code(e)}): {e}") from e
            log.warning("falling back to %s after: %s", self.fallback_model, e)
            try:
                resp, more = self._generate(self.fallback_model, contents, config)
                retries += more
                fallback_used = True
            except Exception as e2:
                raise LLMUnavailable(f"model API unavailable (primary and fallback): {e2}") from e2

        out = LLMResponse(model=self.fallback_model if fallback_used else primary, retries=retries, fallback_used=fallback_used)
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            out.prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            out.completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        cand = (resp.candidates or [None])[0]
        out.raw_parts = getattr(cand, "content", None)
        parts = getattr(out.raw_parts, "parts", None) or []
        texts = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc is not None and fc.name:
                out.calls.append(ToolCall(name=fc.name, args=dict(fc.args or {}), id=getattr(fc, "id", "") or ""))
            elif getattr(p, "text", None):
                texts.append(p.text)
        out.text = "\n".join(texts).strip()
        return out

    def classify(self, system: str, text: str, model: str | None = None) -> str:
        from google.genai import types

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=text)])]
        # A one-line verdict, but with room for a thinking model's reasoning tokens (the free-tier
        # models do not accept thinking_budget=0); an empty answer is treated as "classifier unavailable".
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.0, max_output_tokens=1024)
        resp, _ = self._generate(model or self.model, contents, config)
        return (resp.text or "").strip()


@dataclass
class FakeLLM:
    """Scripted responses for tests. Each item is an LLMResponse or an Exception."""

    script: list[Any] = field(default_factory=list)
    classify_answers: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def chat(self, system: str, messages: list[dict], tools: list[dict], model: str | None = None) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        if not self.script:
            return LLMResponse(text="(no scripted response)")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def classify(self, system: str, text: str, model: str | None = None) -> str:
        if self.classify_answers:
            a = self.classify_answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return "ALLOW"

