"""UI view-model tests (offline; no streamlit needed)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.config import load_settings  # noqa: E402
from email_agent.report import PRIORITY_ORDER, build_report  # noqa: E402

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ISOLATED_KEYS}
        for key in ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")
        self.settings = load_settings(None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_rows_are_shaped_for_the_ui(self) -> None:
        report = build_report(ROOT / "data" / "sample_emails", self.settings, now=NOW, limit=3)
        self.assertEqual(len(report["rows"]), 3)
        self.assertEqual(report["summary"]["processed"], 3)
        self.assertEqual(report["failures"], [])
        # priority-sorted (highest first) and every field the detail tabs need is present
        ranks = [PRIORITY_ORDER[row["priority"]] for row in report["rows"]]
        self.assertEqual(ranks, sorted(ranks))
        required = {"subject", "from_address", "body_text", "priority", "priority_score", "sentiment",
                    "urgency", "confidence", "reasoning", "flags", "engine", "context", "draft_text",
                    "draft_confidence", "draft_citations", "review_status", "send_at",
                    "schedule_reason", "trace", "warnings"}
        self.assertTrue(required.issubset(report["rows"][0].keys()))
        self.assertTrue(report["rows"][0]["reasoning"])

    def test_mode_description_masks_keys(self) -> None:
        import json

        report = build_report(ROOT / "data" / "sample_emails", self.settings, now=NOW, limit=1)
        mode = report["mode"]
        self.assertEqual(mode["primary"], "none")
        self.assertIsInstance(mode["primary_key_present"], bool)
        self.assertIsInstance(mode["judge_key_present"], bool)
        self.assertFalse(mode["primary_key_present"])
        self.assertNotIn("sk-", json.dumps(mode))  # flag booleans only, never key material

    def test_no_draft_for_spam(self) -> None:
        report = build_report(ROOT / "data" / "generated_batch", self.settings, now=NOW)
        spam_rows = [row for row in report["rows"] if "spam" in (row["flags"] or [])]
        self.assertTrue(spam_rows)
        for row in spam_rows:
            self.assertIsNone(row["draft_text"])
            self.assertEqual(row["schedule_status"], "junk")

    def test_empty_directory_is_graceful(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            report = build_report(tmp, self.settings, now=NOW)
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["summary"]["processed"], 0)


if __name__ == "__main__":
    unittest.main()
