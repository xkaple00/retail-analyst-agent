"""Wires the pieces together for the CLI (and for tests, with fakes injected)."""

from __future__ import annotations

from dataclasses import dataclass

from .agent import Agent
from .bq import BigQueryRunner, QueryRunner
from .config import Settings, User, get_user
from .golden import GoldenStore
from .llm import LLM, GeminiLLM
from .reports import Conversations, Preferences, ReportLibrary
from .store import connect, purge_expired, purge_jsonl
from .tracing import Prices, Tracer


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
    purge_expired(conn, s.chat_retention_days, s.audit_retention_days)  # retention: chat 90 d, audit 1 y, reports never
    purge_jsonl(s.data_dir / "traces.jsonl", s.audit_retention_days)
    prices = Prices(s.price_per_m_input_tokens, s.price_per_m_output_tokens, s.price_per_tb_bigquery, s.cost_per_question_usd)
    return Runtime(
        settings=s,
        conn=conn,
        tracer=Tracer(conn, s.data_dir / "traces.jsonl", prices),
        golden=GoldenStore(s.golden_path, s.data_dir / "golden_candidates.jsonl"),
        reports=ReportLibrary(conn),
        prefs=Preferences(conn),
        conversations=Conversations(conn),
    )


def build_live_agent(user_id: str, settings: Settings | None = None, conversation_id: str | None = None) -> tuple[Runtime, Agent]:
    rt = build_runtime(settings)
    s = rt.settings
    user = get_user(s, user_id)
    if s.second_provider_enabled:
        # Client default: fall back between Gemini models only. The switch exists so the trade-off is
        # explicit (see ARCHITECTURE.md §9); wiring a second provider is a new LLM class behind the same interface.
        raise RuntimeError(
            f"ANALYST_SECOND_PROVIDER_ENABLED=true but provider '{s.second_provider}' is not wired in this "
            "prototype; set it to false (Gemini-only fallback) or add an LLM implementation for it"
        )
    llm = GeminiLLM(s.gemini_api_key, s.model, s.fallback_model, timeout_s=s.llm_timeout_s, primary_cooldown_s=s.llm_primary_cooldown_s)
    bq = BigQueryRunner(s.project, s.max_bytes_per_query)
    return rt, rt.agent(user, llm, bq, conversation_id)
