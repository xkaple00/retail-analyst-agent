"""One trace per user turn, spans for every LLM / tool / SQL / guard step.

A `Trace` is an explicit object owned by the turn (no shared "current trace" state, so several
agents can run side by side). Stored in SQLite (queryable from the CLI) and appended as JSON lines
(shippable to any log store).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Trace:
    trace_id: str
    conversation_id: str
    user_id: str
    started_at: str = field(default_factory=_now)
    t0: float = field(default_factory=time.perf_counter)
    status: str = "ok"
    guard_result: str = ""
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    sql_attempts: int = 0
    sql_failures: int = 0
    bytes_billed: int = 0
    retries: int = 0
    fallback_used: int = 0
    error: str = ""
    user_message: str = ""
    assistant_message: str = ""
    spans: list[dict] = field(default_factory=list)

    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)

    def event(self, kind: str, name: str, **payload: Any) -> None:
        """An instant span (no duration), e.g. the system prompt or an automatic save."""
        self.spans.append({"kind": kind, "name": name, "started_at": _now(), "status": "ok", "duration_ms": 0, "payload": payload})


class Tracer:
    def __init__(self, conn: sqlite3.Connection, jsonl_path: Path) -> None:
        self.conn = conn
        self.jsonl_path = jsonl_path
        self.last_trace_id: str | None = None

    def start(self, conversation_id: str, user_id: str, user_message: str) -> Trace:
        return Trace(uuid.uuid4().hex[:12], conversation_id, user_id, user_message=user_message)

    @contextmanager
    def span(self, t: Trace, kind: str, name: str, **payload: Any):
        rec = {"kind": kind, "name": name, "started_at": _now(), "status": "ok", "payload": payload}
        t0 = time.perf_counter()
        try:
            yield rec
        except Exception as e:
            rec["status"] = "error"
            rec["payload"]["error"] = str(e)[:500]
            raise
        finally:
            rec["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            t.spans.append(rec)

    def finish(self, t: Trace, assistant_message: str = "", status: str | None = None, error: str = "") -> Trace:
        t.assistant_message = assistant_message
        if status:
            t.status = status
        if error:
            t.error = error[:500]
        dur = t.duration_ms()
        self.conn.execute(
            "INSERT INTO traces(trace_id, conversation_id, user_id, started_at, duration_ms, status, guard_result, "
            "llm_calls, prompt_tokens, completion_tokens, tool_calls, sql_attempts, sql_failures, bytes_billed, retries, "
            "fallback_used, error, user_message, assistant_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t.trace_id, t.conversation_id, t.user_id, t.started_at, dur, t.status, t.guard_result,
                t.llm_calls, t.prompt_tokens, t.completion_tokens, t.tool_calls, t.sql_attempts, t.sql_failures,
                t.bytes_billed, t.retries, t.fallback_used, t.error, t.user_message, t.assistant_message,
            ),
        )
        for s in t.spans:
            self.conn.execute(
                "INSERT INTO spans(trace_id, kind, name, started_at, duration_ms, status, payload) VALUES (?,?,?,?,?,?,?)",
                (t.trace_id, s["kind"], s["name"], s["started_at"], s.get("duration_ms"), s["status"], json.dumps(s["payload"], default=str)),
            )
        self.conn.commit()
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**{k: v for k, v in t.__dict__.items() if k != "t0"}, "duration_ms": dur}, default=str) + "\n")
        self.last_trace_id = t.trace_id
        return t


# --- read side -----------------------------------------------------------
def usage_today(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Tokens and BigQuery bytes a user has spent since midnight UTC (for daily budgets)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) tokens, COALESCE(SUM(bytes_billed), 0) bytes "
        "FROM traces WHERE user_id = ? AND started_at >= ?",
        (user_id, day),
    ).fetchone()
    return {"tokens": int(r["tokens"]), "bytes": int(r["bytes"])}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(status='ok') ok, SUM(status='refused') refused, SUM(status='error') errors, "
        "SUM(llm_calls) llm_calls, SUM(prompt_tokens) pt, SUM(completion_tokens) ct, SUM(tool_calls) tools, "
        "SUM(sql_attempts) sql_attempts, SUM(sql_failures) sql_failures, SUM(bytes_billed) bytes, SUM(retries) retries, "
        "SUM(fallback_used) fallbacks FROM traces"
    ).fetchone()
    durs = [r[0] for r in conn.execute("SELECT duration_ms FROM traces WHERE duration_ms IS NOT NULL ORDER BY duration_ms")]

    def pct(p: float) -> int:
        if not durs:
            return 0
        return int(durs[min(len(durs) - 1, int(p * len(durs)))])

    n = row["n"] or 0
    return {
        "turns": n,
        "ok": row["ok"] or 0,
        "refused": row["refused"] or 0,
        "errors": row["errors"] or 0,
        "error_rate": round((row["errors"] or 0) / n, 3) if n else 0,
        "latency_p50_ms": pct(0.5),
        "latency_p95_ms": pct(0.95),
        "llm_calls": row["llm_calls"] or 0,
        "prompt_tokens": row["pt"] or 0,
        "completion_tokens": row["ct"] or 0,
        "tool_calls": row["tools"] or 0,
        "sql_attempts": row["sql_attempts"] or 0,
        "sql_failures": row["sql_failures"] or 0,
        "sql_self_correction_rate": round((row["sql_failures"] or 0) / (row["sql_attempts"] or 1), 3),
        "bytes_billed": row["bytes"] or 0,
        "llm_retries": row["retries"] or 0,
        "llm_fallbacks": row["fallbacks"] or 0,
    }


def recent(conn: sqlite3.Connection, n: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM traces ORDER BY started_at DESC LIMIT ?", (n,))]


def get_trace(conn: sqlite3.Connection, trace_id: str) -> dict | None:
    t = conn.execute("SELECT * FROM traces WHERE trace_id LIKE ?", (trace_id + "%",)).fetchone()
    if not t:
        return None
    spans = [dict(s) for s in conn.execute("SELECT * FROM spans WHERE trace_id = ? ORDER BY id", (t["trace_id"],))]
    for s in spans:
        s["payload"] = json.loads(s["payload"] or "{}")
    return {**dict(t), "spans": spans}
