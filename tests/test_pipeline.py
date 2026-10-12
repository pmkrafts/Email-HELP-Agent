"""End-to-end pipeline tests on real sample files (temp SQLite, no LLM)."""
from __future__ import annotations

import os
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
GEN = ROOT / "data" / "generated_batch" / "emails"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)  # Monday 09:00 UTC

_ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        # never touch a developer's real .env or ambient keys: the pipeline must run offline here
        self._saved_env = {key: os.environ.get(key) for key in _ISOLATED_KEYS}
        for key in _ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")
        self.settings = load_settings(None)
        self.orch = GraphOrchestrator(self.settings, repo=None, logger=None)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_legal_threat_end_to_end(self) -> None:
        state = self.orch.process(parse_file(SAMPLES / "001_angry_refund_legal_threat.eml"), now=NOW)
        self.assertEqual(state.status, "scheduled")
        assert state.classification and state.draft and state.review and state.schedule
        self.assertEqual(state.classification.priority, "Critical")
        self.assertEqual(state.review.status, "escalate")
        self.assertEqual(state.schedule.status, "escalated")
        self.assertGreater(len(state.draft.text), 80)

    def test_spam_shortcut_has_no_draft(self) -> None:
        state = self.orch.process(parse_file(GEN / "g15_crypto_spam.txt"), now=NOW)
        self.assertIsNone(state.draft)
        assert state.schedule
        self.assertEqual(state.schedule.status, "junk")
        self.assertIsNone(state.schedule.send_at)

    def test_runner_batch_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repository(Path(tmp) / "test.db")
            orch = GraphOrchestrator(self.settings, repo=repo, logger=None)
            states, failures, skipped = orch.run_batch(SAMPLES, now=NOW)
            self.assertEqual(failures, [])
            self.assertEqual(skipped, [])
            self.assertEqual(len(states), 12)
            self.assertTrue(all(state.status == "scheduled" for state in states), [s.error for s in states])
            self.assertEqual(repo.summary()["emails"], 12)

            # idempotency: second run skips everything
            states2, _, skipped2 = orch.run_batch(SAMPLES, now=NOW)
            self.assertEqual(states2, [])
            self.assertEqual(len(skipped2), 12)

            # force re-run replaces rows instead of duplicating them
            orch.run_batch(SAMPLES, now=NOW, force=True)
            self.assertEqual(repo.summary()["emails"], 12)
            repo.close()

    def test_draft_ignores_injection(self) -> None:
        state = self.orch.process(parse_file(GEN / "g03_prompt_injection.eml"), now=NOW)
        assert state.draft and state.classification
        lowered = state.draft.text.lower()
        self.assertNotIn("system prompt", lowered)
        self.assertNotIn("api key", lowered)
        self.assertIn("prompt_injection_attempt", state.classification.flags)


if __name__ == "__main__":
    unittest.main()
