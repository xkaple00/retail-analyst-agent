"""Wires the pieces together for the CLI (and for tests, with fakes injected)."""

from __future__ import annotations

from dataclasses import dataclass

from .agent import Agent
from .bq import BigQueryRunner, QueryRunner
from .config import Settings, User, get_user
from .golden import GoldenStore
from .llm import LLM, GeminiLLM
from .reports import Conversations, Preferences, ReportLibrary
from .store import connect
from .tracing import Tracer


@dataclass
class Runtime:
    settings: Settings
    conn: object
    tracer: Tracer
    golden: GoldenStore
    reports: ReportLibrary
    prefs: Preferences
    conversations: Conversations

    def agent(self, user: User, llm: LLM, bq: QueryRunner, conversation_id: str | None = None) -> Agent:
        return Agent(self.settings, user, llm, bq, self.tracer, self.golden, self.reports, self.prefs, conversation_id, self.conversations)


def build_runtime(settings: Settings | None = None) -> Runtime:
    s = settings or Settings()
    conn = connect(s.data_dir)
    return Runtime(
        settings=s,
        conn=conn,
        tracer=Tracer(conn, s.data_dir / "traces.jsonl"),
        golden=GoldenStore(s.golden_path, s.data_dir / "golden_candidates.jsonl"),
        reports=ReportLibrary(conn),
        prefs=Preferences(conn),
        conversations=Conversations(conn),
    )


def build_live_agent(user_id: str, settings: Settings | None = None, conversation_id: str | None = None) -> tuple[Runtime, Agent]:
    rt = build_runtime(settings)
    s = rt.settings
    user = get_user(s, user_id)
    llm = GeminiLLM(s.gemini_api_key, s.model, s.fallback_model)
    bq = BigQueryRunner(s.project, s.max_bytes_per_query)
    return rt, rt.agent(user, llm, bq, conversation_id)
