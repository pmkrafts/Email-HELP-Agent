"""Live progress: stage-level persistence and the queries the real-time view reads."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.config import load_settings  # noqa: E402
from email_agent.graph import GraphOrchestrator  # noqa: E402
from email_agent.parser import parse_file  # noqa: E402
from email_agent.storage import Repository  # noqa: E402

SAMPLES = ROOT / "data" / "sample_emails"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

_ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_GRAPH_CHECKPOINT", "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


class _BrokenDrafter:
    """A drafter that raises — used to check that a failure is visible in the live view."""

    client = object()

    def run(self, *args, **kwargs):  # noqa: ANN002,ANN003
        raise ValueError("boom")


class _LiveFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ISOLATED_KEYS}
        for key in _ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")
        self.settings = load_settings(None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = Repository(self.tmp / "live.db")

    def tearDown(self) -> None:
        self.repo.close()
        self._tmp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class StagePersistenceTests(_LiveFixture):
    def test_stages_land_in_the_database_while_the_email_runs(self) -> None:
        orchestrator = GraphOrchestrator(self.settings, repo=self.repo)
        state = orchestrator.process(parse_file(SAMPLES / "003_positive_feedback.eml"), now=NOW)
        self.assertEqual(state.status, "scheduled")

        rows = self.repo.live_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "scheduled")          # the live view's stage
        self.assertEqual(row["priority"], "Low")
        self.assertIsNotNone(row["classification_confidence"])  # confidence as soon as it exists
        self.assertIsNotNone(row["draft_confidence"])
        self.assertTrue(row["draft_text"])
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["schedule_status"], "scheduled")
        self.assertEqual(self.repo.status_counts(), {"scheduled": 1})

    def test_the_trace_carries_every_stage_with_latency(self) -> None:
        GraphOrchestrator(self.settings, repo=self.repo).process(
            parse_file(SAMPLES / "003_positive_feedback.eml"), now=NOW
        )
        logs = self.repo.conn.execute(
            "SELECT agent, event, latency_ms FROM agent_logs ORDER BY id"
        ).fetchall()
        agents = [row["agent"] for row in logs]
        for stage in ("classifier", "retrieval", "drafter", "reviewer", "scheduler"):
            self.assertIn(stage, agents)
        self.assertTrue(all(row["latency_ms"] is not None for row in logs))

    def test_a_failure_is_visible_with_its_trace(self) -> None:
        orchestrator = GraphOrchestrator(self.settings, repo=self.repo)
        orchestrator.drafter = _BrokenDrafter()  # type: ignore[assignment]
        state = orchestrator.process(parse_file(SAMPLES / "003_positive_feedback.eml"), now=NOW)

        self.assertEqual(state.status, "error")
        row = self.repo.live_rows()[0]
        self.assertEqual(row["status"], "error")
        self.assertIn("boom", row["error"] or "")
        self.assertIsNotNone(row["classification_confidence"])  # the work before the failure survives
        agents = {r["agent"] for r in self.repo.conn.execute("SELECT agent FROM agent_logs")}
        self.assertIn("classifier", agents)
        self.assertIn("drafter", agents)

    def test_claim_is_idempotent_and_marks_the_email_as_seen(self) -> None:
        email = parse_file(SAMPLES / "003_positive_feedback.eml")
        first = self.repo.claim_email(email)
        second = self.repo.claim_email(email)
        self.assertEqual(first, second)                                  # UNIQUE(message_id)
        self.assertEqual(self.repo.status_counts(), {"processing": 1})
        self.assertIn(email.message_id, self.repo.processed_message_ids())  # blocks double processing

    def test_stalled_processing_rows_are_reported(self) -> None:
        self.repo.claim_email(parse_file(SAMPLES / "003_positive_feedback.eml"))
        self.assertEqual(len(self.repo.stalled_rows(older_than_seconds=0)), 1)
        self.assertEqual(self.repo.stalled_rows(older_than_seconds=3600), [])

    def test_live_rows_are_newest_first_and_filterable(self) -> None:
        orchestrator = GraphOrchestrator(self.settings, repo=self.repo)
        for name in ("003_positive_feedback.eml", "001_angry_refund_legal_threat.eml"):
            orchestrator.process(parse_file(SAMPLES / name), now=NOW)

        rows = self.repo.live_rows()
        self.assertEqual(len(rows), 2)
        self.assertGreaterEqual(rows[0]["updated_at"], rows[1]["updated_at"])
        self.assertEqual(len(self.repo.live_rows(limit=1)), 1)
        self.assertEqual(self.repo.live_rows(since="2999-01-01T00:00:00+00:00"), [])

    def test_live_detail_returns_every_stage(self) -> None:
        email = parse_file(SAMPLES / "001_angry_refund_legal_threat.eml")
        GraphOrchestrator(self.settings, repo=self.repo).process(email, now=NOW)
        detail = self.repo.live_detail(email.message_id)
        assert detail is not None
        self.assertEqual(detail["email"]["status"], "scheduled")
        self.assertEqual(detail["classification"]["priority"], "Critical")
        self.assertIn("legal_threat", detail["flags"])
        self.assertTrue(detail["draft"]["draft_text"])
        self.assertTrue(detail["citations"])
        self.assertEqual(detail["review"]["status"], "escalate")
        self.assertEqual(detail["schedule"]["status"], "escalated")
        stages = {log["agent"] for log in detail["logs"]}
        self.assertIn("classifier", stages)
        self.assertIn("scheduler", stages)
        self.assertIsNone(self.repo.live_detail("no-such-message"))

    def test_unparsable_file_is_recorded_once(self) -> None:
        broken = self.tmp / "broken.eml"
        broken.write_text("not an email at all", encoding="utf-8")
        self.repo.record_file_error(broken, "no text and no html body")
        self.repo.record_file_error(broken, "no text and no html body")   # idempotent
        rows = self.repo.live_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("no text", rows[0]["error"] or "")


class MigrationTests(_LiveFixture):
    def test_live_columns_are_added_to_an_older_database(self) -> None:
        """A database created before the live view must still open and gain the new columns."""
        old = self.tmp / "old.db"
        connection = sqlite3.connect(old)
        connection.execute(
            "CREATE TABLE emails (id INTEGER PRIMARY KEY, message_id TEXT UNIQUE, from_address TEXT,"
            " from_name TEXT, subject TEXT, body_text TEXT, received_at TEXT, raw_path TEXT,"
            " warnings TEXT, created_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE classifications (id INTEGER PRIMARY KEY, email_id INTEGER, sentiment TEXT,"
            " urgency TEXT, priority TEXT, priority_score REAL, category TEXT, confidence REAL,"
            " reasoning TEXT, flags TEXT, engine TEXT)"
        )
        connection.commit()
        connection.close()

        repo = Repository(old)  # runs the schema + migration
        try:
            columns = {row["name"] for row in repo.conn.execute("PRAGMA table_info(emails)")}
            self.assertTrue({"status", "error", "updated_at"} <= columns)
            self.assertEqual(repo.status_counts(), {})
            self.assertEqual(repo.live_rows(), [])
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
