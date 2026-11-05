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
        default_factory=lambda: os.getenv("ANALYST_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    )
    guard_model: str = field(
        default_factory=lambda: os.getenv("ANALYST_GUARD_MODEL", "gemini-3.5-flash-lite")
    )
    max_bytes_per_query: int = field(
        default_factory=lambda: int(os.getenv("MAX_BYTES_PER_QUERY", "2000000000"))
    )
    query_timeout_s: int = field(default_factory=lambda: int(os.getenv("QUERY_TIMEOUT_S", "60")))
    guard_fail_closed: bool = field(default_factory=lambda: os.getenv("GUARD_FAIL_CLOSED", "false").lower() == "true")
    judge_model: str = field(default_factory=lambda: os.getenv("ANALYST_JUDGE_MODEL", "gemini-3.8-flash"))
    # Per-user daily budgets (0 = unlimited): tokens and BigQuery bytes summed over today's traces.
    daily_token_budget: int = field(default_factory=lambda: int(os.getenv("DAILY_TOKEN_BUDGET", "0")))
    daily_bytes_budget: int = field(default_factory=lambda: int(os.getenv("DAILY_BYTES_BUDGET", "0")))
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
