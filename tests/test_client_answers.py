"""Checks for the client's answers of 2026-11-05 (Harry): PII list, masking before the model, strict
delete flow, retention, cost per question, Gemini-only fallback."""

import pytest

from analyst import tracing
from analyst.bq import FakeBigQuery, QueryResult
from analyst.config import Settings
from analyst.errors import UnsafeSQL
from analyst.llm import FakeLLM, LLMResponse, ToolCall
from analyst.pii import PII_COLUMNS, SAFE_USER_COLUMNS
from analyst.runtime import build_live_agent, build_runtime
from analyst.scope import make_safe_sql
from analyst.store import purge_expired
from analyst.tracing import Prices


def ok_rows(rows):
    return QueryResult(rows=rows, row_count=len(rows), bytes_processed=1234, duration_ms=5)


# --- 5. PII: never first/last name, email, street address, postal code, exact coordinates --------
def test_pii_deny_list_matches_the_client_list():
    client_list = {"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude"}
    assert PII_COLUMNS["users"] == client_list
    assert not (set(SAFE_USER_COLUMNS) & client_list)


@pytest.mark.parametrize("col", sorted({"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude"}))
def test_every_pii_column_is_rejected_before_bigquery(alice, col):
    with pytest.raises(UnsafeSQL, match="personal data"):
        make_safe_sql(f"SELECT {col} FROM users", alice)
    with pytest.raises(UnsafeSQL, match="personal data"):
        make_safe_sql(f"SELECT u.{col} FROM orders o JOIN users u ON u.id = o.user_id", alice)


def test_select_star_on_users_only_yields_safe_columns(alice):
    sql = make_safe_sql("SELECT * FROM users", alice)
    for col in PII_COLUMNS["users"]:
        assert col not in sql
    assert "traffic_source" in sql and "state" in sql


def test_tool_results_are_masked_before_they_reach_the_model(make_agent, alice):
    """PII must never reach the LLM: even if a row somehow carried an email, the model sees a mask."""
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT state, COUNT(*) n FROM users GROUP BY 1", "purpose": "x"})]),
        LLMResponse(text="Done."),
    ])
    bq = FakeBigQuery(responses=[ok_rows([{"state": "note: jane@shop.com lives at 12 Main Street, 37.7749, -122.4194", "n": 1}])])
    make_agent(alice, llm, bq).handle("customers by state")
    tool_result = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]["result"]
    flat = str(tool_result)
    assert "jane@shop.com" not in flat and "Main Street" not in flat and "37.7749" not in flat
    assert "[email]" in flat and "[address]" in flat and "[coordinates]" in flat


# --- 6. Delete: exact list shown first, hard delete, author only ----------------------------------
def test_delete_shows_exact_list_then_hard_deletes_only_own_reports(rt, alice, bob, make_agent):
    rt.reports.save("alice", "conv1", "Alice Q1", "a")
    rt.reports.save("alice", "conv1", "Alice Q2", "b")
    rt.reports.save("bob", "conv9", "Bob Q1", "c")
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"mentioning": "Q"})]),
        LLMResponse(text="Deleted 2 reports."),
    ])
    agent = make_agent(alice, llm)
    r = agent.handle("delete my Q reports")
    assert r.status == "needs_confirmation"
    titles = {x["title"] for x in r.pending["reports"]}
    assert titles == {"Alice Q1", "Alice Q2"}  # the exact list, and never Bob's
    r2 = agent.resume(True)
    assert r2.status == "ok"
    assert rt.reports.find("alice") == []
    assert [x["title"] for x in rt.reports.find("bob")] == ["Bob Q1"]
    assert rt.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1  # hard delete, no soft flag


# --- 10. Retention: chat 90 days, audit 1 year, reports until deleted ------------------------------
def test_retention_purges_old_chat_and_audit_but_never_reports(rt):
    c = rt.conn
    rt.reports.save("alice", "old", "Old report", "kept forever")
    c.execute("UPDATE reports SET created_at = '2020-01-01T00:00:00.000Z'")
    c.execute("INSERT INTO messages(conversation_id, user_id, role, text, created_at) VALUES ('old','alice','user','hi','2020-01-01T00:00:00.000Z')")
    c.execute("INSERT INTO messages(conversation_id, user_id, role, text) VALUES ('new','alice','user','hi')")
    c.execute("INSERT INTO traces(trace_id, conversation_id, user_id, started_at) VALUES ('t-old','old','alice','2020-01-01T00:00:00.000Z')")
    c.execute("INSERT INTO spans(trace_id, kind, name, started_at) VALUES ('t-old','llm','x','2020-01-01T00:00:00.000Z')")
    c.execute("INSERT INTO traces(trace_id, conversation_id, user_id, started_at) VALUES ('t-new','new','alice',strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    c.execute("INSERT INTO deletions(user_id, report_ids, titles, deleted_at) VALUES ('alice','[1]','[\"x\"]','2020-01-01T00:00:00.000Z')")
    c.commit()
    out = purge_expired(c, chat_days=90, audit_days=365)
    assert out == {"messages": 1, "spans": 1, "traces": 1, "deletions": 1}
    assert c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert [r[0] for r in c.execute("SELECT trace_id FROM traces")] == ["t-new"]
    assert c.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1  # reports are never purged


def test_retention_runs_at_startup(settings):
    rt = build_runtime(settings)
    rt.conn.execute("INSERT INTO messages(conversation_id, user_id, role, text, created_at) VALUES ('old','alice','user','hi','2020-01-01T00:00:00.000Z')")
    rt.conn.commit()
    rt2 = build_runtime(settings)  # same data dir: startup purge removes the 2020 message
    assert rt2.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_retention_zero_disables_purge(rt):
    rt.conn.execute("INSERT INTO messages(conversation_id, user_id, role, text, created_at) VALUES ('old','alice','user','hi','2020-01-01T00:00:00.000Z')")
    rt.conn.commit()
    assert purge_expired(rt.conn, chat_days=0, audit_days=0) == {}
    assert rt.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


# --- 9. Budget: $1 per question, configurable, estimate stored per turn --------------------------
def test_cost_estimate_per_turn_is_in_the_trace_and_stats(make_agent, alice, rt):
    rt.tracer.prices = Prices(input_per_m=1.0, output_per_m=10.0, bq_per_tb=1000.0, question_cap=1.0)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT COUNT(*) n FROM orders", "purpose": "x"})], prompt_tokens=100_000, completion_tokens=10_000),
        LLMResponse(text="Done.", prompt_tokens=100_000, completion_tokens=10_000),
    ])
    bq = FakeBigQuery(responses=[QueryResult(rows=[{"n": 1}], row_count=1, bytes_processed=10**12, duration_ms=5)])
    r = make_agent(alice, llm, bq).handle("how many orders")
    t = tracing.get_trace(rt.conn, r.trace_id)
    # 200k input * $1/M = 0.2, 20k output * $10/M = 0.2, 1 TB * $1000 = 1000 -> over the $1 cap
    assert t["estimated_cost_usd"] == pytest.approx(1000.4)
    assert any(s["kind"] == "budget" and s["name"] == "over_question_cost" for s in t["spans"])
    assert tracing.stats(rt.conn)["estimated_cost_usd"] == pytest.approx(1000.4)


def test_cheap_turn_is_not_flagged(make_agent, alice, rt):
    r = make_agent(alice, FakeLLM(script=[LLMResponse(text="Hi.", prompt_tokens=1000, completion_tokens=100)])).handle("hello")
    t = tracing.get_trace(rt.conn, r.trace_id)
    assert 0 < t["estimated_cost_usd"] < 0.01
    assert not any(s["kind"] == "budget" for s in t["spans"])


def test_cost_settings_come_from_env(monkeypatch):
    monkeypatch.setenv("COST_PER_QUESTION_USD", "0.25")
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "7")
    s = Settings()
    assert s.cost_per_question_usd == 0.25 and s.chat_retention_days == 7 and s.audit_retention_days == 365


# --- 8. Fallback between Gemini models only; a second provider is configurable but off -----------
def test_second_provider_is_off_by_default_and_refuses_when_enabled_but_unwired(settings):
    assert settings.second_provider_enabled is False
    assert settings.fallback_model.startswith("gemini") and settings.model.startswith("gemini")
    settings.second_provider_enabled = True
    with pytest.raises(RuntimeError, match="not wired"):
        build_live_agent("alice", settings)
