from analyst.llm import FakeLLM, LLMResponse, ToolCall


def _seed(rt):
    rt.reports.save("alice", "conv1", "Q1 report for Client X", "revenue up")
    rt.reports.save("alice", "conv1", "Churn deep dive", "churn 5%")
    rt.reports.save("alice", "old", "Client X onboarding", "older")
    rt.reports.save("bob", "conv1", "Client X men's view", "bob's")


def test_delete_requires_confirmation_and_only_own_reports(rt, make_agent, alice):
    _seed(rt)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"mentioning": "Client X"})]),
        LLMResponse(text="Deleted 2 reports."),
    ])
    seen = {}

    def confirm(preview):
        seen.update(preview)
        return True

    r = make_agent(alice, llm).handle("delete all reports mentioning Client X", confirm=confirm)
    assert r.status == "ok"
    assert seen["matched"] == 2 and {x["title"] for x in seen["reports"]} == {"Q1 report for Client X", "Client X onboarding"}
    titles = {x["title"] for x in rt.reports.find("alice")}
    assert titles == {"Churn deep dive"}
    assert len(rt.reports.find("bob")) == 1  # bob untouched
    tool_result = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]["result"]
    assert tool_result["deleted"] == 2


def test_delete_cancelled_deletes_nothing(rt, make_agent, alice):
    _seed(rt)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"this_conversation": True})]),
        LLMResponse(text="Cancelled."),
    ])
    make_agent(alice, llm).handle("delete all the reports we made in this conversation", confirm=lambda p: False)
    assert len(rt.reports.find("alice")) == 3
    tool_result = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]["result"]
    assert tool_result["deleted"] == 0 and tool_result["cancelled"]


def test_this_conversation_scope(rt, make_agent, alice):
    _seed(rt)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"this_conversation": True})]),
        LLMResponse(text="done"),
    ])
    previews = []
    make_agent(alice, llm, conversation_id="conv1").handle("delete all the reports we made in this conversation", confirm=lambda p: previews.append(p) or True)
    assert previews[0]["matched"] == 2
    assert {x["title"] for x in rt.reports.find("alice")} == {"Client X onboarding"}


def test_confirmation_is_single_use_and_owner_bound(rt):
    _seed(rt)
    p = rt.reports.request_delete("alice", "Client X", None)
    assert rt.reports.confirm_delete("bob", p["confirmation_id"])["deleted"] == 0
    assert rt.reports.confirm_delete("alice", p["confirmation_id"])["deleted"] == 2
    assert rt.reports.confirm_delete("alice", p["confirmation_id"])["deleted"] == 0


def test_no_match_needs_no_confirmation(rt, make_agent, alice):
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"mentioning": "nothing"})]),
        LLMResponse(text="none"),
    ])
    called = []
    make_agent(alice, llm).handle("delete reports about nothing", confirm=lambda p: called.append(1) or True)
    assert called == []


def test_report_written_without_save_call_is_saved_anyway(rt, make_agent, alice):
    llm = FakeLLM(script=[LLMResponse(text="# Q1 Brand Report\n\nRevenue up 10%.\n\n## Action items\n1. Do X")])
    r = make_agent(alice, llm).handle("create a report for Q1 with action items")
    saved = rt.reports.find("alice")
    assert len(saved) == 1 and saved[0]["title"] == "Q1 Brand Report"
    assert "Saved to your report library as #1" in r.text


def test_plain_answer_to_report_question_is_not_saved(rt, make_agent, alice):
    llm = FakeLLM(script=[LLMResponse(text="You have no reports yet.")])
    make_agent(alice, llm).handle("show me my reports")
    assert rt.reports.find("alice") == []


def test_save_report_files_golden_candidate(rt, make_agent, alice):
    from analyst.bq import FakeBigQuery, QueryResult

    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("run_sql", {"sql": "SELECT 1 AS x FROM orders", "purpose": "p"})]),
        LLMResponse(calls=[ToolCall("save_report", {"title": "Q1", "markdown": "# Q1\n- action 1"})]),
        LLMResponse(text="saved"),
    ])
    bq = FakeBigQuery(responses=[QueryResult(rows=[{"x": 1}], row_count=1, bytes_processed=1, duration_ms=1)])
    make_agent(alice, llm, bq).handle("create a Q1 report with action items")
    assert [r["title"] for r in rt.reports.find("alice")] == ["Q1"]
    cands = rt.golden.list("candidate")
    assert len(cands) == 1 and cands[0]["sql"] == "SELECT 1 AS x FROM orders"
    assert rt.golden.approve(cands[0]["id"]) and rt.golden.list("approved")[-1]["id"] == cands[0]["id"]


def test_delete_is_written_to_the_audit_table(rt, make_agent, alice):
    _seed(rt)
    llm = FakeLLM(script=[LLMResponse(calls=[ToolCall("request_delete_reports", {"mentioning": "Client X"})]), LLMResponse(text="done")])
    r = make_agent(alice, llm).handle("delete all reports mentioning Client X", confirm=lambda p: True)
    row = rt.conn.execute("SELECT user_id, trace_id, report_ids, titles FROM deletions").fetchone()
    assert row["user_id"] == "alice" and row["trace_id"] == r.trace_id
    assert "Client X onboarding" in row["titles"] and len(__import__("json").loads(row["report_ids"])) == 2


def test_mentioning_treats_wildcards_as_text(rt):
    rt.reports.save("alice", "c", "100% sure", "a")
    rt.reports.save("alice", "c", "other", "b")
    assert [r["title"] for r in rt.reports.find("alice", "%")] == ["100% sure"]
    assert rt.reports.find("alice", "_") == []


def test_turn_pauses_without_callback_and_resumes_after_yes(rt, make_agent, alice):
    """The API shape: handle() returns needs_confirmation, resume(True) finishes the same turn and trace."""
    _seed(rt)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"mentioning": "Client X"})]),
        LLMResponse(text="Deleted 2 reports."),
    ])
    agent = make_agent(alice, llm)
    r = agent.handle("delete all reports mentioning Client X")
    assert r.status == "needs_confirmation" and r.pending["matched"] == 2
    assert len(rt.reports.find("alice")) == 3  # nothing deleted yet
    assert len(llm.calls) == 1
    r2 = agent.resume(True)
    assert r2.status == "ok" and r2.trace_id == r.trace_id and r2.text == "Deleted 2 reports."
    assert {x["title"] for x in rt.reports.find("alice")} == {"Churn deep dive"}
    from analyst import tracing

    t = tracing.get_trace(rt.conn, r.trace_id)
    tool = next(s for s in t["spans"] if s["kind"] == "tool")
    assert tool["payload"]["approved"] is True and tool["payload"]["result"]["deleted"] == 2
    assert agent.resume(True).text.startswith("There is nothing waiting")


def test_paused_turn_cancels_on_no_or_on_a_new_message(rt, make_agent, alice):
    _seed(rt)
    llm = FakeLLM(script=[
        LLMResponse(calls=[ToolCall("request_delete_reports", {"this_conversation": True})]),
        LLMResponse(text="Cancelled."),
        LLMResponse(calls=[ToolCall("request_delete_reports", {"this_conversation": True})]),
        LLMResponse(text="Cancelled again."),
        LLMResponse(text="hello"),
    ])
    agent = make_agent(alice, llm)
    agent.handle("delete all the reports we made in this conversation")
    assert agent.resume(False).text == "Cancelled."
    assert agent.handle("delete all the reports we made in this conversation").status == "needs_confirmation"
    r = agent.handle("hello")  # a new message abandons the open confirmation
    assert r.status == "ok" and r.text == "hello" and len(rt.reports.find("alice")) == 3
    assert rt.conn.execute("SELECT COUNT(*) FROM pending_deletes").fetchone()[0] == 0


def test_confirmation_survives_a_new_process(rt):
    """Pending confirmations live in the database, so another process (or request) can complete them."""
    from analyst.reports import ReportLibrary

    _seed(rt)
    p = ReportLibrary(rt.conn).request_delete("alice", "Client X", None)
    other = ReportLibrary(rt.conn)
    assert other.confirm_delete("alice", p["confirmation_id"])["deleted"] == 2


def test_conversation_continues_in_a_new_agent(rt, make_agent, alice):
    llm1 = FakeLLM(script=[LLMResponse(calls=[ToolCall("get_schema", {})]), LLMResponse(text="first answer")])
    make_agent(alice, llm1, conversation_id="conv9").handle("what data is there")
    llm2 = FakeLLM(script=[LLMResponse(text="second")])
    agent2 = make_agent(alice, llm2, conversation_id="conv9")
    roles = [m["role"] for m in agent2.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    agent2.handle("and more?")
    sent = llm2.calls[0]["messages"]
    assert sent[0]["text"] == "what data is there" and sent[-2]["text"] == "first answer"
    assert sent[2]["result"]["dataset"].startswith("bigquery-public-data")
    # another user cannot read that conversation
    from analyst.config import User

    assert make_agent(User(id="bob", name="B", role="r", brands=["Diesel"]), FakeLLM(), conversation_id="conv9").history == []
