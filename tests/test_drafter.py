"""Drafting templates: category-specific next steps, no cross-category policy leakage."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.agents.drafter import Drafter  # noqa: E402
from email_agent.config import load_settings  # noqa: E402
from email_agent.models import AgentState, Classification, ParsedEmail  # noqa: E402
from email_agent.retrieval import KnowledgeBase  # noqa: E402

ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL", "EMAIL_AGENT_MODEL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_BASE_URL", "EMAIL_AGENT_JUDGE_API_KEY",
    "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


def _state(category: str, sentiment: str = "neutral", flags: list[str] | None = None) -> AgentState:
    email = ParsedEmail(
        message_id="t", from_address="a@example.com", from_name="Alex", to_addresses=[],
        subject="Test subject", body_text="Hello, I have a question about my order AUR-1234.",
        date=None, raw_path="t.eml", source_format="eml",
    )
    state = AgentState(email=email)
    state.classification = Classification(
        sentiment=sentiment, urgency="medium", priority="Medium", priority_score=4.0,
        category=category, confidence=0.6, reasoning="test", flags=flags or [],
    )
    return state


class DrafterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ISOLATED_KEYS}
        for key in ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")
        self.settings = load_settings(None)
        self.drafter = Drafter(self.settings, KnowledgeBase.load(ROOT / "data" / "knowledge_base"), client=None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_feedback_draft_does_not_leak_returns_policy(self) -> None:
        draft = self.drafter.run(_state("feedback", sentiment="positive"))
        lowered = draft.text.lower()
        self.assertNotIn("returns policy", lowered)
        self.assertNotIn("refund", lowered)
        self.assertIn("shared your kind words", draft.text)

    def test_account_draft_mentions_reset_link(self) -> None:
        draft = self.drafter.run(_state("account"))
        self.assertIn("reset link", draft.text.lower())

    def test_unknown_category_uses_generic_step(self) -> None:
        draft = self.drafter.run(_state("mystery-category"))
        self.assertIn("concrete next step", draft.text.lower())

    def test_legal_threat_draft_avoids_commitments(self) -> None:
        draft = self.drafter.run(_state("complaint", sentiment="angry", flags=["legal_threat", "refund_request"]))
        lowered = draft.text.lower()
        self.assertTrue("ownership of this case" in lowered or "concrete plan" in lowered)
        for banned in ("guarantee", "we will refund", "instant refund"):
            self.assertNotIn(banned, lowered)


if __name__ == "__main__":
    unittest.main()
