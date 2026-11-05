"""Golden bucket: question -> SQL -> analyst notes trios, BM25 retrieval, candidate capture.

Two files: the approved trios shipped with the repo (`golden/trios.jsonl`) and the candidates
filed at runtime (`data/golden_candidates.jsonl`). Approving moves a candidate into the first
file with a short analyst note, so the prompt never receives a whole report.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "by", "is", "are", "was", "were", "what", "which", "how", "our", "we", "me", "my", "do", "does", "did", "with", "vs", "versus", "last", "this"}
NOTE_CHARS = 600
SQL_CHARS = 1500


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class GoldenStore:
    def __init__(self, path: Path, candidates_path: Path | None = None) -> None:
        self.path = path
        self.candidates_path = candidates_path or path.with_name("candidates.jsonl")
        self.trios: list[dict] = []
        self.candidates: list[dict] = []
        self.reload()

    def reload(self) -> None:
        self.trios = _read(self.path)
        self.candidates = _read(self.candidates_path)

    # --- retrieval -----------------------------------------------------
    def search(self, question: str, k: int = 3) -> list[dict]:
        docs = [t for t in self.trios if t.get("status", "approved") == "approved"]
        q = _tokens(question)
        if not docs or not q:
            return []
        tokenised = [_tokens(d["question"] + " " + " ".join(d.get("tags", []))) for d in docs]
        n = len(docs)
        avgdl = sum(len(t) for t in tokenised) / n
        df: Counter = Counter()
        for toks in tokenised:
            df.update(set(toks))
        k1, b = 1.5, 0.75
        scored = []
        for d, toks in zip(docs, tokenised):
            tf = Counter(toks)
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(toks) / avgdl))
            if score > 0:
                scored.append((score, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:k]]

    def prompt_block(self, question: str, k: int = 3) -> str:
        hits = self.search(question, k)
        if not hits:
            return ""
        lines = ["Examples of how our analysts answered similar questions (adapt, do not copy blindly):"]
        for h in hits:
            lines.append(f"\n### {h['question']}\nSQL:\n{h['sql'][:SQL_CHARS]}\nAnalyst notes: {h.get('note') or h['report'][:NOTE_CHARS]}")
        return "\n".join(lines)

    # --- learning loop ---------------------------------------------------
    def add_candidate(self, question: str, sql: str, report: str, user_id: str, trace_id: str = "") -> str:
        tid = "c" + uuid.uuid4().hex[:6]
        self.candidates.append(
            {"id": tid, "status": "candidate", "tags": [], "question": question, "sql": sql, "report": report,
             "author": user_id, "trace_id": trace_id}
        )
        _write(self.candidates_path, self.candidates)
        return tid

    def approve(self, trio_id: str, note: str | None = None) -> bool:
        """Move a candidate into the approved set: the full report is kept (the trio stays question, SQL,
        report) and `note` is what the prompt receives (default: the first lines of the report)."""
        for c in self.candidates:
            if c["id"] == trio_id:
                self.candidates.remove(c)
                self.trios.append({
                    "id": trio_id, "status": "approved", "tags": c.get("tags", []), "question": c["question"],
                    "sql": c["sql"], "report": c["report"], "note": (note or c["report"]).strip()[:NOTE_CHARS],
                    "author": c.get("author", ""), "trace_id": c.get("trace_id", ""),
                })
                _write(self.path, self.trios)
                _write(self.candidates_path, self.candidates)
                return True
        return False

    def list(self, status: str | None = None) -> list[dict]:
        rows = self.trios + self.candidates
        return [t for t in rows if status is None or t.get("status") == status]
