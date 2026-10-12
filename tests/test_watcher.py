"""Watcher tests: pickup, idempotency, the claim, error rows and the poll loop."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.config import load_settings  # noqa: E402
from email_agent.storage import Repository  # noqa: E402
from email_agent import watcher  # noqa: E402

SAMPLES = ROOT / "data" / "sample_emails"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

_ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_GRAPH_CHECKPOINT", "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


class _WatcherFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ISOLATED_KEYS}
        for key in _ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")
        self.settings = load_settings(None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()
        self.db = self.tmp / "watch.db"
        self.repo = Repository(self.db)

    def tearDown(self) -> None:
        self.repo.close()
        self._tmp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def drop(self, name: str, source: str = "003_positive_feedback.eml") -> Path:
        target = self.inbox / name
        shutil.copy(SAMPLES / source, target)
        return target

    def watch(self, **kwargs):
        return watcher.watch(self.settings, self.inbox, now=NOW, repo=self.repo, sleep=lambda _: None, **kwargs)


class SweepTests(_WatcherFixture):
    def test_new_file_is_processed_and_visible(self) -> None:
        self.drop("a.eml")
        states = self.watch(once=True)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].status, "scheduled")

        rows = self.repo.live_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "scheduled")
        self.assertIsNotNone(rows[0]["classification_confidence"])
        self.assertTrue(rows[0]["draft_text"])

    def test_redropping_the_same_email_changes_nothing(self) -> None:
        self.drop("a.eml")
        self.assertEqual(len(self.watch(once=True)), 1)
        self.drop("b.eml")  # same Message-ID, different file name
        self.assertEqual(self.watch(once=True), [])
        self.assertEqual(len(self.repo.live_rows()), 1)

    def test_force_reprocesses_without_duplicating(self) -> None:
        self.drop("a.eml")
        self.assertEqual(len(self.watch(once=True)), 1)
        self.assertEqual(len(self.watch(once=True, force=True)), 1)
        self.assertEqual(len(self.repo.live_rows()), 1)

    def test_a_broken_file_becomes_an_error_row_and_the_sweep_continues(self) -> None:
        good = self.drop("good.eml", "001_angry_refund_legal_threat.eml")
        bad = self.inbox / "bad.eml"
        bad.write_text("irrelevant", encoding="utf-8")
        original = watcher.parse_file

        def flaky(path, now=None):
            if Path(path).name == "bad.eml":
                raise ValueError("no text and no html body")
            return original(path, now=now)

        watcher.parse_file = flaky
        try:
            states = self.watch(once=True)
        finally:
            watcher.parse_file = original

        self.assertEqual([state.email.raw_path for state in states], [str(good)])
        rows = {row["subject"]: row for row in self.repo.live_rows()}
        self.assertEqual(len(rows), 2)
        broken = next(row for row in rows.values() if row["status"] == "error")
        self.assertIn("no text and no html body", broken["error"] or "")
        self.assertIn("bad.eml", broken["subject"] or "")

    def test_a_claim_from_another_watcher_blocks_processing(self) -> None:
        from email_agent.parser import parse_file

        path = self.drop("a.eml")
        self.repo.claim_email(parse_file(path))  # a second watcher got there first
        self.assertEqual(self.watch(once=True), [])
        self.assertEqual(self.repo.status_counts(), {"processing": 1})

    def test_process_once_and_stored_files(self) -> None:
        """What the console's live tick and its reprocess button call."""
        self.drop("a.eml")
        result = watcher.process_once(self.settings, self.inbox, repo=self.repo, limit=5)
        self.assertEqual(len(result.processed), 1)
        files = watcher.stored_files(self.repo)
        self.assertEqual([path.name for path in files], ["a.eml"])

        again = watcher.process_once(self.settings, files=files, repo=self.repo, force=True)
        self.assertEqual(len(again.processed), 1)          # a reprocess upgrades the row
        self.assertEqual(len(self.repo.live_rows()), 1)

    def test_limit_caps_a_sweep(self) -> None:
        self.drop("a.eml", "003_positive_feedback.eml")
        self.drop("b.eml", "011_long_thread_escalation.eml")
        self.assertEqual(len(self.watch(once=True, limit=1)), 1)
        self.assertEqual(len(self.watch(once=True)), 1)
        self.assertEqual(len(self.repo.live_rows()), 2)

    def test_a_second_sweep_reports_the_skipped_file(self) -> None:
        """Silent skipping is what makes the live view look broken — the sweep reports it."""
        self.drop("a.eml")
        self.watch(once=True)
        results: list = []
        self.watch(once=True, on_sweep=results.append)
        self.assertEqual(results[0].processed, [])
        self.assertEqual(results[0].skipped, ["a.eml"])

    def test_the_loop_picks_up_a_file_dropped_between_sweeps(self) -> None:
        self.drop("a.eml", "003_positive_feedback.eml")

        def drop_second(state) -> None:  # fires after the first sweep's email
            self.drop("b.eml", "011_long_thread_escalation.eml")

        states = self.watch(max_sweeps=2, on_email=drop_second)
        self.assertEqual(len(states), 2)
        self.assertEqual(len(self.repo.live_rows()), 2)


class CliWatchTests(_WatcherFixture):
    def test_watch_once_from_the_cli(self) -> None:
        from email_agent.cli import main

        self.drop("a.eml")
        code = main(["--db", str(self.db), "watch", "--input", str(self.inbox), "--once",
                     "--now", NOW.isoformat()])
        self.assertEqual(code, 0)
        rows = self.repo.live_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "scheduled")

    def test_the_cli_says_why_nothing_happened(self) -> None:
        from email_agent.cli import main

        self.drop("a.eml")
        main(["--db", str(self.db), "watch", "--input", str(self.inbox), "--once"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--db", str(self.db), "watch", "--input", str(self.inbox), "--once"])
        self.assertEqual(code, 0)
        self.assertIn("skipped 1 file(s) already stored", buffer.getvalue())
        self.assertIn("--force", buffer.getvalue())

    def test_the_db_flag_works_before_or_after_the_subcommand(self) -> None:
        from email_agent.cli import main

        self.drop("a.eml")
        self.assertEqual(main(["watch", "--input", str(self.inbox), "--once", "--db", str(self.db)]), 0)
        self.assertEqual(len(self.repo.live_rows()), 1)


if __name__ == "__main__":
    unittest.main()
