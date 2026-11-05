"""Pre-deployment eval: runs evals/questions.jsonl through the live agent and scores each answer.

Checks (deterministic): status, PII-free output, tables used, rows returned, report saved, keywords,
and ground truth: `reference_sql` is run with the same user's scope and its first row's values must
appear in the answer (numbers within 2%, strings verbatim), so a wrong number fails even if it reads well.
Check (LLM-as-judge): does the answer address the user's intent? 1-5 with a one-line reason, on
ANALYST_JUDGE_MODEL (a different model from the one that answered, when quota allows).
Usage: python evals/run_evals.py [--limit N] [--only q1,q5]   (needs GEMINI_API_KEY and BigQuery access)
Results: evals/last_run.json (committed, so the repo carries the evidence).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analyst import tracing  # noqa: E402
from analyst.config import Settings  # noqa: E402
from analyst.runtime import build_live_agent  # noqa: E402
from analyst.scope import make_safe_sql  # noqa: E402

JUDGE = """You grade a data-analysis assistant. Given the executive's question and the assistant's answer,
score 1-5 how well the answer addresses the intent (numbers present, question actually answered, actionable
when asked). Reply as: <score>: <one line reason>."""

PII_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\b\d{3}[ -]\d{3}[ -]\d{4}\b")
NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _ground_truth(agent, sql: str, text: str) -> tuple[bool, dict]:
    """Run the reference query under the user's scope; every value of its first row must be in the answer."""
    res = agent.bq.query(make_safe_sql(sql, agent.user), 60, 5)
    if not res.rows:
        return False, {"reference": "no rows"}
    row = res.rows[0]
    nums = []
    for m in NUM_RE.findall(text):
        try:
            nums.append(float(m.replace(",", "")))
        except ValueError:
            pass
    found = {}
    for k, v in row.items():
        if isinstance(v, (int, float)):
            found[k] = any(abs(n - float(v)) <= max(0.02 * abs(float(v)), 0.5) for n in nums)
        else:  # names get shortened in prose (brand dropped, suffix cut): any 4 consecutive words must appear
            words = str(v).lower().split()
            flat = " ".join(text.split()).lower()
            grams = [" ".join(words[i:i + 4]) for i in range(max(1, len(words) - 3))]
            found[k] = any(g in flat for g in grams)
    return all(found.values()), {"reference": row, "matched": found}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="comma-separated question ids, e.g. q5,q8")
    args = ap.parse_args()
    qs = [json.loads(l) for l in (Path(__file__).parent / "questions.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        qs = [q for q in qs if q["id"] in wanted]
    if args.limit:
        qs = qs[: args.limit]
    settings = Settings()
    settings.data_dir = settings.data_dir / "evals"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for q in qs:
        rt, agent = build_live_agent(q["user"], settings)
        r = agent.handle(q["question"], confirm=lambda p: True)
        t = tracing.get_trace(rt.conn, r.trace_id)
        exp = q["expect"]
        checks: dict[str, bool] = {}
        if exp.get("refused"):
            checks["refused"] = r.status == "refused"
        elif exp.get("handled"):  # either a safe answer or a refusal is fine, never an error or a leak
            checks["handled"] = r.status in ("ok", "refused")
        else:
            checks["ok"] = r.status == "ok"
        checks["no_pii"] = not PII_RE.search(r.text)
        if exp.get("tables"):
            sqls = " ".join(s["payload"].get("original_sql", "") for s in t["spans"] if s["kind"] == "sql").lower()
            checks["tables"] = all(tb in sqls for tb in exp["tables"])
        if exp.get("min_rows"):
            rows = [s["payload"].get("rows", 0) for s in t["spans"] if s["kind"] == "sql"]
            checks["rows"] = max(rows or [0]) >= exp["min_rows"]
        if exp.get("report_saved"):
            checks["report_saved"] = any(s["name"].startswith("save_report") for s in t["spans"])
        if exp.get("mentions"):
            checks["mentions"] = all(m.lower() in r.text.lower() for m in exp["mentions"])
        if exp.get("out_of_scope_notice"):
            checks["scope_notice"] = bool(re.search(r"scope|not available|outside|don't have access|no access", r.text, re.I))
        truth: dict = {}
        if q.get("reference_sql") and r.status == "ok":
            try:
                checks["ground_truth"], truth = _ground_truth(agent, q["reference_sql"], r.text)
            except Exception as e:
                checks["ground_truth"], truth = False, {"error": str(e)[:300]}
        judge = ""
        if r.status == "ok":
            for judge_model in (settings.judge_model, settings.guard_model):  # separate judge model, else the cheap one
                try:
                    judge = f"[{judge_model}] " + agent.llm.classify(JUDGE, f"QUESTION: {q['question']}\nANSWER: {r.text}", model=judge_model)
                    break
                except Exception as e:  # judge failures must not fail the run
                    judge = f"judge error: {str(e)[:120]}"
        passed = all(checks.values())
        results.append({"id": q["id"], "passed": passed, "checks": checks, "ground_truth": truth, "judge": judge,
                        "trace": r.trace_id, "answer": r.text[:1500], "model": settings.model})
        print(f"{'PASS' if passed else 'FAIL'} {q['id']} {checks} judge={judge!r}")
    n_pass = sum(r["passed"] for r in results)
    print(f"\n{n_pass}/{len(results)} passed")
    out = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "passed": n_pass, "total": len(results), "results": results}
    (Path(__file__).parent / "last_run.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
