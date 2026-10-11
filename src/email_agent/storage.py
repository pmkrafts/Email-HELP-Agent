"""SQLite persistence (docs 09-Backend-Schema) with idempotent writes by message_id."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import AgentState

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    message_id TEXT UNIQUE,
    from_address TEXT,
    from_name TEXT,
    subject TEXT,
    body_text TEXT,
    received_at TEXT,
    raw_path TEXT,
    warnings TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY,
    email_id INTEGER REFERENCES emails(id),
    sentiment TEXT, urgency TEXT, priority TEXT, priority_score REAL,
    category TEXT, confidence REAL, reasoning TEXT, flags TEXT, engine TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    email_id INTEGER REFERENCES emails(id),
    draft_text TEXT, confidence REAL, key_points TEXT, tone TEXT,
    citations TEXT, language TEXT, version INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER REFERENCES drafts(id),
    status TEXT, issues TEXT, suggested_changes TEXT, final_confidence REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    email_id INTEGER REFERENCES emails(id),
    send_at TEXT, reason TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY,
    email_id INTEGER REFERENCES emails(id),
    agent TEXT, event TEXT, details TEXT, latency_ms INTEGER, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_classifications_priority ON classifications(priority);
CREATE INDEX IF NOT EXISTS idx_schedules_send_at ON schedules(send_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def processed_message_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT message_id FROM emails").fetchall()
        return {r["message_id"] for r in rows}

    def save_state(self, state: AgentState) -> None:
        """Idempotent: re-processing the same message_id replaces its rows."""
        email = state.email
        existing = self.conn.execute("SELECT id FROM emails WHERE message_id = ?", (email.message_id,)).fetchone()
        if existing:
            email_id = existing["id"]
            for table in ("agent_logs", "schedules", "reviews", "drafts", "classifications"):
                if table == "reviews":
                    self.conn.execute(
                        "DELETE FROM reviews WHERE draft_id IN (SELECT id FROM drafts WHERE email_id = ?)", (email_id,)
                    )
                else:
                    self.conn.execute(f"DELETE FROM {table} WHERE email_id = ?", (email_id,))
            self.conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))

        cur = self.conn.execute(
            "INSERT INTO emails (message_id, from_address, from_name, subject, body_text, received_at, raw_path, warnings, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                email.message_id, email.from_address, email.from_name, email.subject, email.body_text,
                email.date.isoformat() if email.date else None, email.raw_path, json.dumps(email.warnings), _now(),
            ),
        )
        email_id = cur.lastrowid

        if state.classification:
            c = state.classification
            self.conn.execute(
                "INSERT INTO classifications (email_id, sentiment, urgency, priority, priority_score, category, confidence, reasoning, flags, engine, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (email_id, c.sentiment, c.urgency, c.priority, c.priority_score, c.category, c.confidence, c.reasoning,
                 json.dumps(c.flags), c.engine, _now()),
            )

        draft_id = None
        if state.draft:
            d = state.draft
            cur = self.conn.execute(
                "INSERT INTO drafts (email_id, draft_text, confidence, key_points, tone, citations, language, version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (email_id, d.text, d.confidence, json.dumps(d.key_points_addressed), d.tone_used,
                 json.dumps(d.citations), d.language, d.version, _now()),
            )
            draft_id = cur.lastrowid
        if state.review and draft_id:
            r = state.review
            self.conn.execute(
                "INSERT INTO reviews (draft_id, status, issues, suggested_changes, final_confidence, created_at) VALUES (?,?,?,?,?,?)",
                (draft_id, r.status, json.dumps(r.issues), r.suggested_changes, r.final_confidence, _now()),
            )
        if state.schedule:
            s = state.schedule
            self.conn.execute(
                "INSERT INTO schedules (email_id, send_at, reason, status, created_at) VALUES (?,?,?,?,?)",
                (email_id, s.send_at.isoformat() if s.send_at else None, s.reason, s.status, _now()),
            )
        for log in state.logs:
            self.conn.execute(
                "INSERT INTO agent_logs (email_id, agent, event, details, latency_ms, created_at) VALUES (?,?,?,?,?,?)",
                (email_id, log.get("agent"), log.get("event"), json.dumps(log), log.get("latency_ms"), _now()),
            )
        self.conn.commit()

    def rows_for_view(self, priority: str | None = None, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT e.message_id, e.subject, e.from_address, c.priority, c.priority_score, c.sentiment, c.urgency,"
            " c.confidence, c.flags, d.draft_text, d.confidence AS draft_confidence, r.status AS review_status,"
            " s.send_at, s.status AS schedule_status"
            " FROM emails e"
            " LEFT JOIN classifications c ON c.email_id = e.id"
            " LEFT JOIN drafts d ON d.email_id = e.id"
            " LEFT JOIN reviews r ON r.draft_id = d.id"
            " LEFT JOIN schedules s ON s.email_id = e.id"
        )
        params: list = []
        if priority:
            sql += " WHERE c.priority = ?"
            params.append(priority)
        sql += " ORDER BY CASE c.priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, e.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def summary(self) -> dict:
        out: dict = {}
        out["emails"] = self.conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]
        out["by_priority"] = {r["priority"]: r["n"] for r in self.conn.execute(
            "SELECT priority, COUNT(*) AS n FROM classifications GROUP BY priority").fetchall()}
        out["avg_classification_confidence"] = self.conn.execute(
            "SELECT AVG(confidence) AS v FROM classifications").fetchone()["v"]
        out["avg_draft_confidence"] = self.conn.execute("SELECT AVG(confidence) AS v FROM drafts").fetchone()["v"]
        out["review_status"] = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews GROUP BY status").fetchall()}
        out["human_review_flagged"] = self.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE status IN ('needs_revision','escalate')").fetchone()["n"]
        out["scheduled"] = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM schedules GROUP BY status").fetchall()}
        return out
