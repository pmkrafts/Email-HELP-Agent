"""SQLite persistence (docs 09-Backend-Schema) with idempotent writes by message_id."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AgentState, ParsedEmail

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
    created_at TEXT,
    status TEXT, error TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY,
    email_id INTEGER REFERENCES emails(id),
    sentiment TEXT, urgency TEXT, priority TEXT, priority_score REAL,
    category TEXT, confidence REAL, reasoning TEXT, flags TEXT, engine TEXT,
    judge_verdict TEXT,
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
        self.conn.execute("PRAGMA busy_timeout = 5000")  # the watcher writes while the UI reads
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")  # readers never block the writer
        except sqlite3.Error:  # pragma: no cover - filesystems without WAL support
            pass
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(classifications)")}
        if "judge_verdict" not in columns:
            self.conn.execute("ALTER TABLE classifications ADD COLUMN judge_verdict TEXT")
        email_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(emails)")}
        for column in ("status", "error", "updated_at"):
            if column not in email_columns:
                self.conn.execute(f"ALTER TABLE emails ADD COLUMN {column} TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_status_updated ON emails(status, updated_at)")

    def close(self) -> None:
        self.conn.close()

    def processed_message_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT message_id FROM emails").fetchall()
        return {r["message_id"] for r in rows}

    #: pipeline stage -> the status shown in the live view
    STAGE_STATUS = {
        "classify": "classified", "draft": "drafted", "revise": "drafted",
        "review": "reviewed", "finalize": "reviewed", "schedule": "scheduled", "error": "error",
    }

    def claim_email(self, email: ParsedEmail, status: str = "processing") -> int:
        """Mark an email as seen before the pipeline runs — the watcher's live marker.

        `message_id` is UNIQUE, so a second watcher (or a re-drop of the same file) cannot create a
        second row; it just returns the existing id.
        """
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO emails (message_id, from_address, from_name, subject, body_text, received_at,"
            " raw_path, warnings, created_at, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                email.message_id, email.from_address, email.from_name, email.subject, email.body_text,
                email.date.isoformat() if email.date else None, email.raw_path, json.dumps(email.warnings),
                now, status, now,
            ),
        )
        row = self.conn.execute("SELECT id FROM emails WHERE message_id = ?", (email.message_id,)).fetchone()
        self.conn.commit()
        return int(row["id"])

    def record_file_error(self, path: str | Path, error: str) -> None:
        """An unparsable file still shows up in the live view (keyed by its file name)."""
        path = Path(path)
        now = _now()
        self.conn.execute(
            "INSERT OR IGNORE INTO emails (message_id, subject, raw_path, warnings, created_at, status, error, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"file:{path.name}", path.name, str(path), json.dumps([error]), now, "error", error, now),
        )
        self.conn.commit()

    def save_stage(self, state: AgentState, stage: str) -> None:
        """Persist one finished stage so the UI can follow an email while it is processed.

        Each stage replaces only its own rows (a re-run never duplicates anything) and bumps
        `emails.status` / `updated_at`. `save_state` remains the seal at the end of the email.
        """
        status = self.STAGE_STATUS.get(stage)  # None for stages without their own table (e.g. retrieve)
        email_id = self.claim_email(state.email, status=status or "processing")
        if stage == "classify" and state.classification:
            self.conn.execute("DELETE FROM classifications WHERE email_id = ?", (email_id,))
            self._insert_classification(email_id, state.classification)
        if stage in {"draft", "revise"} and state.draft:
            # a replaced draft invalidates the review that referred to it
            self.conn.execute(
                "DELETE FROM reviews WHERE draft_id IN (SELECT id FROM drafts WHERE email_id = ?)", (email_id,)
            )
            self.conn.execute("DELETE FROM drafts WHERE email_id = ?", (email_id,))
            self._insert_draft(email_id, state.draft)
        if stage in {"review", "finalize"} and state.review:
            row = self.conn.execute(
                "SELECT id FROM drafts WHERE email_id = ? ORDER BY id DESC LIMIT 1", (email_id,)
            ).fetchone()
            if row:
                self.conn.execute("DELETE FROM reviews WHERE draft_id = ?", (row["id"],))
                self._insert_review(int(row["id"]), state.review)
        if stage == "schedule" and state.schedule:
            self.conn.execute("DELETE FROM schedules WHERE email_id = ?", (email_id,))
            self._insert_schedule(email_id, state.schedule)
        self.conn.execute("DELETE FROM agent_logs WHERE email_id = ?", (email_id,))
        self._insert_logs(email_id, state.logs)
        self.conn.execute(
            "UPDATE emails SET status = COALESCE(?, status), error = ?, updated_at = ? WHERE id = ?",
            (status, state.error, _now(), email_id),
        )
        self.conn.commit()

    def save_state(self, state: AgentState) -> None:
        """Idempotent: re-processing the same message_id replaces its rows."""
        email = state.email
        existing = self.conn.execute("SELECT id FROM emails WHERE message_id = ?", (email.message_id,)).fetchone()
        if existing:
            self._delete_email_rows(int(existing["id"]))

        cur = self.conn.execute(
            "INSERT INTO emails (message_id, from_address, from_name, subject, body_text, received_at, raw_path,"
            " warnings, created_at, status, error, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                email.message_id, email.from_address, email.from_name, email.subject, email.body_text,
                email.date.isoformat() if email.date else None, email.raw_path, json.dumps(email.warnings),
                _now(), state.status, state.error, _now(),
            ),
        )
        email_id = int(cur.lastrowid)

        if state.classification:
            self._insert_classification(email_id, state.classification)
        draft_id = self._insert_draft(email_id, state.draft) if state.draft else None
        if state.review and draft_id:
            self._insert_review(draft_id, state.review)
        if state.schedule:
            self._insert_schedule(email_id, state.schedule)
        self._insert_logs(email_id, state.logs)
        self.conn.commit()

    # -- row helpers ---------------------------------------------------------
    def _delete_email_rows(self, email_id: int) -> None:
        for table in ("agent_logs", "schedules", "reviews", "drafts", "classifications"):
            if table == "reviews":
                self.conn.execute(
                    "DELETE FROM reviews WHERE draft_id IN (SELECT id FROM drafts WHERE email_id = ?)", (email_id,)
                )
            else:
                self.conn.execute(f"DELETE FROM {table} WHERE email_id = ?", (email_id,))
        self.conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))

    def _insert_classification(self, email_id: int, c) -> None:
        self.conn.execute(
            "INSERT INTO classifications (email_id, sentiment, urgency, priority, priority_score, category, confidence, reasoning, flags, engine, judge_verdict, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (email_id, c.sentiment, c.urgency, c.priority, c.priority_score, c.category, c.confidence, c.reasoning,
             json.dumps(c.flags), c.engine, c.judge_verdict, _now()),
        )

    def _insert_draft(self, email_id: int, d) -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts (email_id, draft_text, confidence, key_points, tone, citations, language, version, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (email_id, d.text, d.confidence, json.dumps(d.key_points_addressed), d.tone_used,
             json.dumps(d.citations), d.language, d.version, _now()),
        )
        return int(cur.lastrowid)

    def _insert_review(self, draft_id: int, r) -> None:
        self.conn.execute(
            "INSERT INTO reviews (draft_id, status, issues, suggested_changes, final_confidence, created_at) VALUES (?,?,?,?,?,?)",
            (draft_id, r.status, json.dumps(r.issues), r.suggested_changes, r.final_confidence, _now()),
        )

    def _insert_schedule(self, email_id: int, s) -> None:
        self.conn.execute(
            "INSERT INTO schedules (email_id, send_at, reason, status, created_at) VALUES (?,?,?,?,?)",
            (email_id, s.send_at.isoformat() if s.send_at else None, s.reason, s.status, _now()),
        )

    def _insert_logs(self, email_id: int, logs: list[dict]) -> None:
        for log in logs:
            self.conn.execute(
                "INSERT INTO agent_logs (email_id, agent, event, details, latency_ms, created_at) VALUES (?,?,?,?,?,?)",
                (email_id, log.get("agent"), log.get("event"), json.dumps(log), log.get("latency_ms"), _now()),
            )

    # -- live view -----------------------------------------------------------
    def live_rows(self, since: str | None = None, limit: int = 50) -> list[dict]:
        """Newest-first rows for the live view (one query, served by the status/updated index)."""
        sql = (
            "SELECT e.message_id, e.subject, e.from_address, e.status, e.error, e.updated_at, e.created_at,"
            " c.priority, c.priority_score, c.sentiment, c.urgency, c.confidence AS classification_confidence,"
            " c.flags, c.judge_verdict, d.draft_text, d.confidence AS draft_confidence,"
            " r.status AS review_status, r.final_confidence, s.send_at, s.status AS schedule_status"
            " FROM emails e"
            " LEFT JOIN classifications c ON c.email_id = e.id"
            " LEFT JOIN drafts d ON d.email_id = e.id"
            " LEFT JOIN reviews r ON r.draft_id = d.id"
            " LEFT JOIN schedules s ON s.email_id = e.id"
        )
        params: list = []
        if since:
            sql += " WHERE e.updated_at > ?"
            params.append(since)
        sql += " ORDER BY e.updated_at DESC, e.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def status_counts(self) -> dict[str, int]:
        """How many emails sit in each stage — the live view's status board."""
        rows = self.conn.execute(
            "SELECT COALESCE(status, 'legacy') AS s, COUNT(*) AS n FROM emails GROUP BY s"
        ).fetchall()
        return {r["s"]: r["n"] for r in rows}

    def latest_updated_at(self) -> str | None:
        """When the database last changed — the live view's heartbeat (None when empty)."""
        row = self.conn.execute("SELECT MAX(updated_at) AS t FROM emails").fetchone()
        return row["t"] if row and row["t"] else None

    def live_detail(self, message_id: str) -> dict | None:
        """Everything the console shows when a live row is opened (one email, all stages)."""
        email = self.conn.execute(
            "SELECT id, message_id, subject, from_address, from_name, body_text, raw_path, warnings,"
            " status, error, updated_at, created_at FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        if email is None:
            return None
        email_id = int(email["id"])
        classification = self.conn.execute(
            "SELECT * FROM classifications WHERE email_id = ? ORDER BY id DESC LIMIT 1", (email_id,)
        ).fetchone()
        draft = self.conn.execute(
            "SELECT * FROM drafts WHERE email_id = ? ORDER BY id DESC LIMIT 1", (email_id,)
        ).fetchone()
        review = None
        if draft is not None:
            review = self.conn.execute(
                "SELECT * FROM reviews WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (int(draft["id"]),)
            ).fetchone()
        schedule = self.conn.execute(
            "SELECT * FROM schedules WHERE email_id = ? ORDER BY id DESC LIMIT 1", (email_id,)
        ).fetchone()
        logs = self.conn.execute(
            "SELECT agent, event, latency_ms, created_at, details FROM agent_logs WHERE email_id = ? ORDER BY id",
            (email_id,),
        ).fetchall()

        def _json_list(value) -> list:
            try:
                parsed = json.loads(value) if value else []
            except (TypeError, ValueError):
                return [str(value)]
            return parsed if isinstance(parsed, list) else [parsed]

        return {
            "email": dict(email),
            "classification": dict(classification) if classification else None,
            "draft": dict(draft) if draft else None,
            "review": dict(review) if review else None,
            "schedule": dict(schedule) if schedule else None,
            "logs": [dict(log) for log in logs],
            "flags": _json_list(classification["flags"]) if classification else [],
            "citations": _json_list(draft["citations"]) if draft else [],
            "issues": _json_list(review["issues"]) if review else [],
            "warnings": _json_list(email["warnings"]),
        }

    def stalled_rows(self, older_than_seconds: int = 120, limit: int = 20) -> list[dict]:
        """Emails left in `processing` for too long (a crashed watcher must be visible, not silent)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        rows = self.conn.execute(
            "SELECT message_id, subject, updated_at FROM emails"
            " WHERE status = 'processing' AND updated_at < ? ORDER BY updated_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def rows_for_view(self, priority: str | None = None, limit: int | None = None) -> list[dict]:
        sql = (
            "SELECT e.message_id, e.subject, e.from_address, c.priority, c.priority_score, c.sentiment, c.urgency,"
            " c.confidence, c.flags, c.engine, c.judge_verdict, d.draft_text, d.confidence AS draft_confidence,"
            " r.status AS review_status, s.send_at, s.status AS schedule_status"
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
