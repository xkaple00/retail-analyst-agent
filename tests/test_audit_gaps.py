"""Independent audit of 2026-11-05: gaps closed the same day."""

import json

from analyst import tracing
from analyst.bq import FakeBigQuery, QueryResult
from analyst.llm import FakeLLM, LLMResponse, ToolCall
from analyst.store import purge_jsonl


def ok_rows(rows):
    return QueryResult(rows=rows, row_count=len(rows), bytes_processed=10, duration_ms=1)


def test_no_golden_candidate_when_the_turn_needed_a_sql_fix(make_agent, alice, rt):
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT nme FROM products", "purpose": "x"})]),
        LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT name FROM products", "purpose": "x"})]),
        LLMResponse(calls=[ToolCall("save_report", {"title": "T", "markdown": "report"})]),
        LLMResponse(text="Saved."),
    ])
    bq = FakeBigQuery(responses=[RuntimeError("Unrecognized name: nme"), ok_rows([{"name": "A"}])])
    r = make_agent(alice, llm, bq).handle("report on product names")
    assert r.status == "ok"
    assert rt.golden.list("candidate") == []
    t = tracing.get_trace(rt.conn, r.trace_id)
    assert any(s["kind"] == "golden" and s["name"] == "candidate_skipped" for s in t["spans"])


def test_injected_trios_are_recorded_and_feedback_attaches_to_them(make_agent, alice, rt):
    llm = FakeLLM(script=[LLMResponse(text="Revenue was 5.")])
    r = make_agent(alice, llm).handle("monthly revenue last 6 months")
    t = tracing.get_trace(rt.conn, r.trace_id)
    inj = next(s for s in t["spans"] if s["kind"] == "golden" and s["name"] == "injected")
    assert inj["payload"]["ids"] == ["g1"]
    res = tracing.record_feedback(rt.conn, r.trace_id[:6], "alice", "down", "wrong month")
    assert res["golden_ids"] == ["g1"]
    assert tracing.trio_feedback(rt.conn) == {"g1": {"up": 0, "down": 1}}
    st = tracing.stats(rt.conn)
    assert st["feedback_down"] == 1 and st["feedback_up"] == 0


def test_feedback_is_owner_only(make_agent, alice, rt):
    r = make_agent(alice, FakeLLM(script=[LLMResponse(text="hi")])).handle("hello there")
    try:
        tracing.record_feedback(rt.conn, r.trace_id, "bob", "up")
    except KeyError:
        pass
    else:
        raise AssertionError("bob must not rate alice's answer")


def test_stats_counts_the_alert_signals(make_agent, alice, rt):
    # a PII mask hit
    make_agent(alice, FakeLLM(script=[LLMResponse(text="mail jane@shop.com")])).handle("who is the top customer this year")
    # attempt budget exhausted
    call = LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT bad FROM products", "purpose": "x"})])
    llm = FakeLLM(script=[call, call, call, call, LLMResponse(text="could not")])
    make_agent(alice, llm, FakeBigQuery(responses=[RuntimeError("boom")] * 10)).handle("something impossible")
    st = tracing.stats(rt.conn)
    assert st["pii_mask_hits"] == 1 and st["attempt_budget_exhausted"] == 1 and st["turns_over_cost_cap"] == 0


def test_jsonl_traces_obey_audit_retention(tmp_path):
    p = tmp_path / "traces.jsonl"
    p.write_text(
        json.dumps({"trace_id": "old", "started_at": "2020-01-01T00:00:00+00:00"}) + "\n"
        + json.dumps({"trace_id": "new", "started_at": "2099-01-01T00:00:00+00:00"}) + "\n"
        + "not json\n",
        encoding="utf-8",
    )
    assert purge_jsonl(p, 365) == 1
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "new" in lines[0] and lines[1] == "not json"
    assert purge_jsonl(p, 0) == 0
