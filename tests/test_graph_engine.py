"""LangGraph engine tests: topology, parity with the sequential engine, branches, durability.

Skipped entirely when `langgraph` is not installed (it is an optional extra), so the default
stdlib-only install still runs the full suite.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from email_agent.graph import GraphOrchestrator

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - optional dependency
    HAS_LANGGRAPH = False

from email_agent.config import load_settings  # noqa: E402
from email_agent.orchestrator import Orchestrator, build_orchestrator  # noqa: E402
from email_agent.parser import parse_file  # noqa: E402

SAMPLES = ROOT / "data" / "sample_emails"
GEN = ROOT / "data" / "generated_batch" / "emails"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)

ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_ENGINE", "EMAIL_AGENT_GRAPH_CHECKPOINT", "EMAIL_AGENT_ENV_FILE",
    "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


class _FakeChat:
    """Minimal LLMClient stand-in: returns canned JSON and records the calls."""

    def __init__(self, payload: dict):
        self.payload = dict(payload)
        self.calls: list[str] = []

    def chat_json(self, system: str, user: str, temperature: float = 0.2) -> dict:
        self.calls.append(user)
        return dict(self.payload)


class _BrokenDrafter:
    def run(self, *args, **kwargs):  # noqa: ANN002,ANN003
        raise ValueError("boom")

    client = object()  # makes the orchestrator think a real drafter is available


class _GraphFixture(unittest.TestCase):
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


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
class TopologyTests(_GraphFixture):
    def test_compiled_graph_declares_every_stage(self) -> None:
        graph = GraphOrchestrator(self.settings).graph
        nodes = set(graph.get_graph().nodes)
        for stage in ("classify", "retrieve", "draft", "review", "revise", "finalize_review",
                      "schedule", "persist"):
            self.assertIn(stage, nodes)
        diagram = GraphOrchestrator(self.settings).diagram()
        self.assertTrue(diagram.startswith("---"))
        for marker in ("classify", "revise", "persist", "halt"):  # halt = error-isolation edge
            self.assertIn(marker, diagram)

    def test_factory_selects_engines(self) -> None:
        self.assertIsInstance(build_orchestrator(self.settings), Orchestrator)
        graph_settings = load_settings(None)
        graph_settings.engine = "graph"
        self.assertIsInstance(build_orchestrator(graph_settings), GraphOrchestrator)


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
class ParityTests(_GraphFixture):
    def test_graph_matches_sequential_engine_on_golden_set(self) -> None:
        sequential = Orchestrator(self.settings)
        graph = GraphOrchestrator(self.settings)
        for name in ("001_angry_refund_legal_threat.eml", "002_late_order_deadline.eml",
                     "005_vip_bulk_order_complaint.eml", "010_empty_subject.eml", "011_long_thread_escalation.eml"):
            with self.subTest(email=name):
                parsed = parse_file(SAMPLES / name)
                a = sequential.process(parsed, now=NOW)
                b = graph.process(parsed, now=NOW)
                self.assertEqual(a.status, b.status)
                self.assertEqual(a.classification.priority, b.classification.priority)  # type: ignore[union-attr]
                self.assertEqual(a.classification.sentiment, b.classification.sentiment)  # type: ignore[union-attr]
                self.assertEqual(a.classification.urgency, b.classification.urgency)  # type: ignore[union-attr]
                self.assertEqual(a.classification.flags, b.classification.flags)  # type: ignore[union-attr]
                self.assertEqual(a.draft.text, b.draft.text)  # type: ignore[union-attr]
                self.assertEqual(a.draft.version, b.draft.version)  # type: ignore[union-attr]
                self.assertEqual(a.review.status, b.review.status)  # type: ignore[union-attr]
                self.assertEqual(a.review.issues, b.review.issues)  # type: ignore[union-attr]
                self.assertEqual(a.schedule.status, b.schedule.status)  # type: ignore[union-attr]
                self.assertEqual(a.schedule.send_at, b.schedule.send_at)  # type: ignore[union-attr]

    def test_full_batch_runs_through_the_graph(self) -> None:
        states, failures, skipped = GraphOrchestrator(self.settings).run_batch(SAMPLES, now=NOW)
        self.assertEqual(failures, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(states), 12)
        self.assertTrue(all(state.status == "scheduled" for state in states),
                        [state.error for state in states])


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
class BranchTests(_GraphFixture):
    def test_spam_short_circuits_retrieval_and_drafting(self) -> None:
        state = GraphOrchestrator(self.settings).process(parse_file(GEN / "g15_crypto_spam.txt"), now=NOW)
        self.assertEqual(state.status, "scheduled")
        self.assertEqual(state.schedule.status, "junk")  # type: ignore[union-attr]
        self.assertIsNone(state.draft)
        self.assertEqual(state.context, [])

    def test_rewrite_loop_revises_once_then_escalates(self) -> None:
        os.environ.update({
            "EMAIL_AGENT_LLM": "openai", "EMAIL_AGENT_API_KEY": "k",
            "EMAIL_AGENT_BASE_URL": "http://127.0.0.1:9/v1", "EMAIL_AGENT_MODEL": "fake",
        })
        settings = load_settings(None)
        self.assertEqual(settings.llm_provider, "openai")  # a client exists, so the loop can run
        orchestrator = GraphOrchestrator(settings)
        fake = _FakeChat({"text": "We will refund the full amount immediately.", "confidence": 0.9,
                          "language": "en", "tone_used": "neutral", "key_points_addressed": ["refund"]})
        orchestrator.client = fake          # type: ignore[assignment]
        orchestrator.classifier.client = fake  # type: ignore[assignment]
        orchestrator.drafter.client = fake  # type: ignore[assignment]

        state = orchestrator.process(parse_file(SAMPLES / "004_password_reset.txt"), now=NOW)
        assert state.draft and state.review and state.schedule
        self.assertEqual(state.draft.version, 2)          # initial draft + one revision (max_rewrites = 2)
        self.assertEqual(state.review.status, "escalate")  # limit reached -> human
        self.assertEqual(state.schedule.status, "escalated")
        self.assertTrue(any("max rewrite attempts" in issue for issue in state.review.issues))
        self.assertEqual(len(fake.calls), 3)               # refine + draft + revise

    def test_node_failure_is_isolated_per_email(self) -> None:
        orchestrator = GraphOrchestrator(self.settings)
        orchestrator.drafter = _BrokenDrafter()  # type: ignore[assignment]
        state = orchestrator.process(parse_file(SAMPLES / "003_positive_feedback.eml"), now=NOW)
        self.assertEqual(state.status, "error")
        self.assertIn("boom", state.error or "")
        states, failures, _ = orchestrator.run_batch(SAMPLES, now=NOW)
        self.assertEqual(len(states), 12)                       # the batch survives
        self.assertEqual(failures, [])                          # parse failures are separate
        spam = [s for s in states if s.classification and "spam" in s.classification.flags]
        self.assertEqual(len(spam), 1)
        self.assertEqual(spam[0].status, "scheduled")            # short-circuit never reaches the drafter
        broken = [s for s in states if s is not spam[0]]
        self.assertTrue(all(s.status == "error" for s in broken))
        self.assertTrue(all("boom" in (s.error or "") for s in broken))


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
class DurabilityTests(_GraphFixture):
    def test_checkpointer_keeps_per_email_state(self) -> None:
        os.environ["EMAIL_AGENT_GRAPH_CHECKPOINT"] = "1"
        settings = load_settings(None)
        self.assertTrue(settings.graph_checkpoint)
        orchestrator = GraphOrchestrator(settings)
        email = parse_file(SAMPLES / "003_positive_feedback.eml")
        orchestrator.process(email, now=NOW)
        snapshot = orchestrator.snapshot(email.message_id)
        assert snapshot is not None
        self.assertEqual(snapshot["status"], "scheduled")
        self.assertEqual(snapshot["classification"]["priority"], "Low")
        self.assertIsNotNone(snapshot["schedule"])

    def test_snapshot_is_none_without_checkpointer(self) -> None:
        orchestrator = GraphOrchestrator(self.settings)
        self.assertIsNone(orchestrator.snapshot("whatever"))


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
class CliTests(_GraphFixture):
    def test_graph_command_prints_mermaid(self) -> None:
        from email_agent.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--engine", "graph", "graph"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("classify", output)
        self.assertIn("persist", output)

    def test_graph_command_without_graph_engine_explains_itself(self) -> None:
        from email_agent.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["graph"])
        self.assertEqual(code, 2)
        self.assertIn("sequential engine has no graph diagram", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
