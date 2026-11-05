"""Settings (from .env / environment) and user scope loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATASET = "bigquery-public-data.thelook_ecommerce"
TABLES = ("orders", "order_items", "products", "users")


@dataclass
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    project: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "") or None)
    model: str = field(default_factory=lambda: os.getenv("ANALYST_MODEL", "gemini-3.8-flash"))
    fallback_model: str = field(
        default_factory=lambda: os.getenv("ANALYST_FALLBACK_MODEL", "gemini-3.5-flash,gemini-3.5-flash-lite")
    )  # comma-separated chain, tried in order (all Gemini: the client's choice)
    guard_model: str = field(
        default_factory=lambda: os.getenv("ANALYST_GUARD_MODEL", "gemini-3.5-flash-lite")
    )
    max_bytes_per_query: int = field(
        default_factory=lambda: int(os.getenv("MAX_BYTES_PER_QUERY", "2000000000"))
    )
    query_timeout_s: int = field(default_factory=lambda: int(os.getenv("QUERY_TIMEOUT_S", "60")))
    # One model call may take at most this long; a hung primary falls back at once and is skipped for the cooldown.
    llm_timeout_s: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_S", "25")))
    llm_primary_cooldown_s: float = field(default_factory=lambda: float(os.getenv("LLM_PRIMARY_COOLDOWN_S", "300")))
    guard_fail_closed: bool = field(default_factory=lambda: os.getenv("GUARD_FAIL_CLOSED", "false").lower() == "true")
    judge_model: str = field(default_factory=lambda: os.getenv("ANALYST_JUDGE_MODEL", "gemini-3.8-flash"))
    # Per-user daily budgets (0 = unlimited): tokens and BigQuery bytes summed over today's traces.
    daily_token_budget: int = field(default_factory=lambda: int(os.getenv("DAILY_TOKEN_BUDGET", "0")))
    daily_bytes_budget: int = field(default_factory=lambda: int(os.getenv("DAILY_BYTES_BUDGET", "0")))
    # Cost model (client: assume $1 per question, easily configurable). The estimate per turn is stored in
    # the trace; a turn above the cap is flagged (`budget/over_question_cost` span), not blocked.
    cost_per_question_usd: float = field(default_factory=lambda: float(os.getenv("COST_PER_QUESTION_USD", "1.0")))
    price_per_m_input_tokens: float = field(default_factory=lambda: float(os.getenv("PRICE_PER_M_INPUT_TOKENS", "0.30")))
    price_per_m_output_tokens: float = field(default_factory=lambda: float(os.getenv("PRICE_PER_M_OUTPUT_TOKENS", "2.50")))
    price_per_tb_bigquery: float = field(default_factory=lambda: float(os.getenv("PRICE_PER_TB_BIGQUERY", "6.25")))
    # Retention (client: chat history 90 days, audit log 1 year, reports until the user deletes them).
    chat_retention_days: int = field(default_factory=lambda: int(os.getenv("CHAT_RETENTION_DAYS", "90")))
    audit_retention_days: int = field(default_factory=lambda: int(os.getenv("AUDIT_RETENTION_DAYS", "365")))
    # Fallback stays inside the Gemini family (client default). A second provider is configurable but off.
    second_provider: str = field(default_factory=lambda: os.getenv("ANALYST_SECOND_PROVIDER", "anthropic-vertex"))
    second_provider_enabled: bool = field(
        default_factory=lambda: os.getenv("ANALYST_SECOND_PROVIDER_ENABLED", "false").lower() == "true"
    )
    data_dir: Path = field(
        default_factory=lambda: (ROOT / os.getenv("ANALYST_DATA_DIR", "./data")).resolve()
    )
    prompts_dir: Path = ROOT / "prompts"
    golden_path: Path = ROOT / "golden" / "trios.jsonl"
    users_path: Path = ROOT / "config" / "users.yaml"
    # Cost caps
    max_llm_calls_per_turn: int = 6  # client asked for roughly 2-5 model calls per query; the 6th is a forced no-tools answer
    max_sql_attempts: int = 3
    max_rows: int = 200
    rows_for_model: int = 50
    history_messages: int = 30


@dataclass
class User:
    id: str
    name: str
    role: str
    brands: list[str] = field(default_factory=list)  # empty = all brands (CEO)

    def scope_text(self) -> str:
        return "brands: " + (", ".join(self.brands) if self.brands else "all brands")


def load_users(path: Path) -> dict[str, User]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users: dict[str, User] = {}
    for uid, u in (raw.get("users") or {}).items():
        users[uid] = User(
            id=uid,
            name=u.get("name", uid),
            role=u.get("role", ""),
            brands=list(u.get("brands") or []),
        )
    return users


def get_user(settings: Settings, user_id: str) -> User:
    users = load_users(settings.users_path)
    if user_id not in users:
        raise KeyError(f"unknown user '{user_id}', known: {', '.join(users)}")
    return users[user_id]
