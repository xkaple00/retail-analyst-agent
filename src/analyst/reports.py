"""Saved reports library with a strict, single-use confirmation flow for deletes, plus preferences
and the conversation message log. All state is in SQLite, so a confirmation started by one process
(or API request) can be completed by another."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time


class ReportLibrary:
    TTL_S = 300

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- CRUD, always scoped to the owner ------------------------------
    def save(self, user_id: str, conversation_id: str, title: str, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reports(user_id, conversation_id, title, content) VALUES (?,?,?,?)",
            (user_id, conversation_id, title.strip()[:200], content),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def find(self, user_id: str, mentioning: str | None = None, conversation_id: str | None = None) -> list[dict]:
        sql = "SELECT id, title, content, created_at, conversation_id FROM reports WHERE user_id = ?"
        args: list = [user_id]
        if conversation_id:
            sql += " AND conversation_id = ?"
            args.append(conversation_id)
        if mentioning:
            like = "%" + re.sub(r"([%_\\])", r"\\\1", mentioning) + "%"  # user text is data, not a pattern
            sql += " AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
            args += [like, like]
        sql += " ORDER BY id DESC"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get(self, user_id: str, report_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM reports WHERE id = ? AND user_id = ?", (report_id, user_id)).fetchone()
        return dict(r) if r else None

    # --- destructive flow ------------------------------------------------
    def request_delete(self, user_id: str, mentioning: str | None, conversation_id: str | None) -> dict:
        """Never deletes. Returns a preview and a confirmation id, or an empty match."""
        matches = self.find(user_id, mentioning, conversation_id)
        if not matches:
            return {"needs_confirmation": False, "matched": 0, "message": "no matching reports of yours"}
        cid = secrets.token_hex(4)
        self.conn.execute(
            "INSERT INTO pending_deletes(confirmation_id, user_id, report_ids, titles, created) VALUES (?,?,?,?,?)",
            (cid, user_id, json.dumps([m["id"] for m in matches]), json.dumps([m["title"] for m in matches]), time.time()),
        )
        self.conn.commit()
        return {
            "needs_confirmation": True,
            "confirmation_id": cid,
            "matched": len(matches),
            "reports": [{"id": m["id"], "title": m["title"], "created_at": m["created_at"]} for m in matches],
            "message": "ask the user to confirm; nothing has been deleted",
        }

    def confirm_delete(self, user_id: str, confirmation_id: str, trace_id: str = "") -> dict:
        p = self.conn.execute("SELECT * FROM pending_deletes WHERE confirmation_id = ?", (confirmation_id,)).fetchone()
        if p is None:
            return {"deleted": 0, "error": "unknown or already used confirmation id"}
        if p["user_id"] != user_id:
            return {"deleted": 0, "error": "confirmation belongs to another user"}
        self.cancel(confirmation_id)  # single use
        if time.time() - p["created"] > self.TTL_S:
            return {"deleted": 0, "error": "confirmation expired, ask again"}
        ids, titles = json.loads(p["report_ids"]), json.loads(p["titles"])
        q = ",".join("?" * len(ids))
        cur = self.conn.execute(f"DELETE FROM reports WHERE user_id = ? AND id IN ({q})", [user_id, *ids])
        self.conn.execute(  # audit log: who deleted what, in which turn
            "INSERT INTO deletions(user_id, trace_id, report_ids, titles) VALUES (?,?,?,?)",
            (user_id, trace_id, json.dumps(ids), json.dumps(titles)),
        )
        self.conn.commit()
        return {
            "deleted": cur.rowcount,
            "report_ids": ids,
            "status": "DONE: the user already confirmed and these reports are permanently deleted. Tell the user it is done; do not ask for confirmation again.",
        }

    def cancel(self, confirmation_id: str) -> None:
        self.conn.execute("DELETE FROM pending_deletes WHERE confirmation_id = ?", (confirmation_id,))
        self.conn.commit()


class Preferences:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def set(self, user_id: str, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO preferences(user_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (user_id, key.strip().lower()[:50], value.strip()[:200]),
        )
        self.conn.commit()

    def all(self, user_id: str) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM preferences WHERE user_id = ?", (user_id,))}


class Conversations:
    """Message log per conversation, so a conversation can continue in another process or request."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(self, conversation_id: str, user_id: str, message: dict) -> None:
        raw = message.get("raw")
        raw_json = None
        if raw is not None:
            dump = getattr(raw, "model_dump", None)
            raw_json = json.dumps(dump(mode="json", exclude_none=True) if dump else raw, default=str)
        self.conn.execute(
            "INSERT INTO messages(conversation_id, user_id, role, name, text, calls, result, raw) VALUES (?,?,?,?,?,?,?,?)",
            (
                conversation_id, user_id, message["role"], message.get("name"), message.get("text"),
                json.dumps([{"name": c.name, "args": c.args} for c in message.get("calls", [])]) if message.get("calls") else None,
                json.dumps(message.get("result"), default=str) if "result" in message else None,
                raw_json,
            ),
        )
        self.conn.commit()

    def load(self, conversation_id: str, user_id: str, last: int) -> list[dict]:
        from .llm import ToolCall

        rows = self.conn.execute(
            "SELECT role, name, text, calls, result, raw FROM messages WHERE conversation_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, user_id, last),
        ).fetchall()
        out: list[dict] = []
        for r in reversed(rows):
            m: dict = {"role": r["role"], "text": r["text"] or ""}
            if r["name"]:
                m["name"] = r["name"]
            if r["calls"]:
                m["calls"] = [ToolCall(c["name"], c["args"]) for c in json.loads(r["calls"])]
            if r["result"] is not None:
                m["result"] = json.loads(r["result"])
            if r["raw"]:
                m["raw"] = json.loads(r["raw"])  # rebuilt into provider content by the LLM client
            out.append(m)
        return out
