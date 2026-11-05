import pytest

from analyst.golden import GoldenStore


def test_bm25_ranks_relevant_trio_first(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"id":"a","status":"approved","tags":["churn"],"question":"Why did churn spike last month?","sql":"s","report":"r"}\n'
        '{"id":"b","status":"approved","tags":["revenue"],"question":"Monthly revenue trend","sql":"s","report":"r"}\n'
        '{"id":"c","status":"candidate","tags":[],"question":"churn churn churn","sql":"s","report":"r"}\n',
        encoding="utf-8",
    )
    g = GoldenStore(p, tmp_path / "cands.jsonl")
    hits = g.search("why is our churn rate going up")
    assert [h["id"] for h in hits] == ["a"]  # candidate c is excluded
    assert "Golden" not in g.prompt_block("nonsense words zzz")




def test_candidates_live_apart_and_approve_moves_them_with_a_note(tmp_path):
    p, c = tmp_path / "t.jsonl", tmp_path / "cands.jsonl"
    g = GoldenStore(p, c)
    tid = g.add_candidate("why did churn spike", "SELECT 1", "# Churn report " + "x" * 5000, "alice", "trace1")
    assert not p.exists() and c.exists()  # the shipped file is untouched by live runs
    assert g.search("churn") == []  # candidates are never injected
    assert g.list("candidate")[0]["trace_id"] == "trace1"
    assert g.approve(tid, note="exclude cancelled orders, churn = no order in 90 days")
    g2 = GoldenStore(p, c)
    hit = g2.search("churn spike")[0]
    assert hit["id"] == tid and hit["note"].startswith("exclude cancelled") and g2.list("candidate") == []
    assert hit["report"].startswith("# Churn report") and hit["trace_id"] == "trace1"  # full trio kept
    assert "exclude cancelled" in g2.prompt_block("churn spike") and "xxxxxxxxxx" not in g2.prompt_block("churn spike")
    assert g2.approve(tid) is False  # already moved


def test_approve_without_note_truncates_the_report(tmp_path):
    g = GoldenStore(tmp_path / "t.jsonl", tmp_path / "c.jsonl")
    tid = g.add_candidate("q", "SELECT 1", "y" * 5000, "alice")
    g.approve(tid)
    assert len(g.list("approved")[0]["note"]) == 600 and len(g.list("approved")[0]["report"]) == 5000
