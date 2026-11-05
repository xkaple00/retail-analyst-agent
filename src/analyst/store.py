"""SQLite storage for reports, preferences, traces, spans and the delete audit log."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS preferences (
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS traces (
  trace_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  duration_ms INTEGER,
  status TEXT,
  guard_result TEXT,
  llm_calls INTEGER DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0,
  sql_attempts INTEGER DEFAULT 0,
  sql_failures INTEGER DEFAULT 0,
  bytes_billed INTEGER DEFAULT 0,
  retries INTEGER DEFAULT 0,
  fallback_used INTEGER DEFAULT 0,
  error TEXT,
  user_message TEXT,
  assistant_message TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  duration_ms INTEGER,
  status TEXT,
  payload TEXT
);
CREATE TABLE IF NOT EXISTS deletions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  trace_id TEXT,
  report_ids TEXT NOT NULL,
  titles TEXT NOT NULL,
  deleted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS pending_deletes (
  confirmation_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  report_ids TEXT NOT NULL,
  titles TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  name TEXT,
  text TEXT,
  calls TEXT,
  result TEXT,
  raw TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, user_id);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
"""


def connect(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "analyst.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
