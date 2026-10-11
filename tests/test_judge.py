"""Judge-channel tests against local mock endpoints — stdlib only.

Covers both channel types and the cost-ladder contract:

  Chat judge   * agree -> one call, cheap path kept
               * disagree, no primary -> correction applied ("overridden")
               * disagree + primary -> single adjudication ("adjudicated")
               * endpoint down -> judge=error, rules baseline untouched
  System One   * request shape (model/state/typed atomic questions)
  (Jev)        * agree -> cheap path; disagree -> corrections from choice answers
               * unusable response -> judge=error
               * draft review -> needs_revision when a policy check fails
  Config       * jev defaults (model jev-1.13, OpenCode Zen base URL), OpenCode key fallback
               * escalations_only spends zero premium calls on agreement
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.agents.classifier import Classifier  # noqa: E402
from email_agent.config import DEFAULT_JUDGE_BASE_URL, load_settings  # noqa: E402
from email_agent.judges import ChatJudge, SystemOneJudge, build_judge  # noqa: E402
from email_agent.llm import LLMClient, SystemOneClient  # noqa: E402
from email_agent.models import ParsedEmail  # noqa: E402

ENV_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)
# tests must never read a developer's real .env (keys, gateways) - point the loader at a missing file
NO_ENV_FILE = str(Path(__file__).resolve().parent / ".no-env-file")


class _MockState:
    def __init__(self) -> None:
        self.responses: list[dict] = []           # chat completions queue
        self.requests: list[dict] = []
        self.systemone_responses: list[dict] = []  # system one queue
        self.systemone_requests: list[dict] = []


def _make_handler(state: _MockState):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path.rstrip("/").endswith("/systemone"):
                state.systemone_requests.append(payload)
                verdict = state.systemone_responses.pop(0) if state.systemone_responses else {"answers": {}}
                self._send(verdict)
                return
            state.requests.append(payload)
            verdict = state.responses.pop(0) if state.responses else {"agree": True, "reason": "default"}
            self._send({"choices": [{"message": {"content": json.dumps(verdict)}}]})

        def _send(self, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return Handler


def _systemone_response(nouls: dict, choices: dict | None = None) -> dict:
    answers: dict = {key: {"type": "noul", "noul": value} for key, value in nouls.items()}
    for key, (choice, confidence) in (choices or {}).items():
        answers[key] = {"type": "choice", "choice": choice, "confidence": confidence,
                        "probabilities": {choice: confidence}}
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 42, "output_tokens": 7}}


def _email(body: str, subject: str = "Test") -> ParsedEmail:
    return ParsedEmail(
        message_id="test-1", from_address="a@example.com", from_name="Alex", to_addresses=[],
        subject=subject, body_text=body, date=None, raw_path="test.eml", source_format="eml",
    )


class _ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = _MockState()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.state))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        cls.primary_state = _MockState()
        cls.primary_server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.primary_state))
        threading.Thread(target=cls.primary_server.serve_forever, daemon=True).start()
        cls.primary_base_url = f"http://127.0.0.1:{cls.primary_server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.primary_server.shutdown()
        cls.primary_server.server_close()

    def setUp(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = NO_ENV_FILE  # ignore any real .env
        self.state.responses.clear()
        self.state.requests.clear()
        self.state.systemone_responses.clear()
        self.state.systemone_requests.clear()
        self.primary_state.responses.clear()
        self.primary_state.requests.clear()
        self.settings = load_settings(None)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # helpers
    def _chat_judge(self, model: str = "jev-chat") -> ChatJudge:
        return ChatJudge(
            LLMClient(api_key="test-key", model=model, base_url=self.base_url),
            prompts_dir=str(self.settings.prompts_dir),
        )

    def _system_one_judge(self) -> SystemOneJudge:
        return SystemOneJudge(SystemOneClient(api_key="test-key", model="jev-1.13", base_url=self.base_url))

    def _primary(self) -> LLMClient:
        return LLMClient(api_key="k", model="gpt-mock", base_url=self.primary_base_url)


class ChatJudgeTests(_ServerFixture):
    def test_agrees_keeps_cheap_path(self) -> None:
        self.state.responses.append({"agree": True, "reason": "labels defensible"})
        result = Classifier(self.settings, client=None, judge=self._chat_judge()).run(
            _email("My order AUR-1001 is late and I would like a refund.")
        )
        self.assertEqual(result.judge_verdict, "agreed")
        self.assertEqual(result.engine, "rules")
        self.assertEqual(len(self.state.requests), 1)

    def test_disagrees_override_without_primary(self) -> None:
        self.state.responses.append({
            "agree": False, "priority": "High", "urgency": "high", "sentiment": "negative",
            "confidence": 0.7, "reason": "deadline language",
        })
        result = Classifier(self.settings, client=None, judge=self._chat_judge()).run(
            _email("Hello, quick question about the invoice for order AUR-1002.")
        )
        self.assertEqual(result.judge_verdict, "overridden")
        self.assertEqual(result.priority, "High")
        self.assertTrue(result.engine.endswith("+judge-override"))

    def test_disagreement_routes_single_email_to_primary(self) -> None:
        # default refine mode ("always"): primary refines first, then the judge, then one adjudication
        self.primary_state.responses.append({"priority": "Medium", "sentiment": "neutral", "urgency": "medium", "confidence": 0.6})
        self.state.responses.append({"agree": False, "priority": "High", "reason": "deadline"})
        self.primary_state.responses.append({"priority": "High", "sentiment": "negative", "urgency": "high", "confidence": 0.8})
        result = Classifier(self.settings, client=self._primary(), judge=self._chat_judge()).run(
            _email("Hello, quick question about the invoice for order AUR-1003.")
        )
        self.assertEqual(result.judge_verdict, "adjudicated")
        self.assertIn("adjudicated", result.engine)
        self.assertEqual(len(self.state.requests), 1)          # the judge call
        self.assertEqual(len(self.primary_state.requests), 2)  # refine + adjudication

    def test_endpoint_down_falls_back(self) -> None:
        from email_agent.rules import score_email

        body = "My order AUR-1004 has not arrived and the tracking has not moved."
        baseline = score_email(self.settings.rules, body, "")
        down = ChatJudge(
            LLMClient(api_key="x", model="jev", base_url="http://127.0.0.1:9/v1", max_retries=0, timeout=1.0),
            prompts_dir=str(self.settings.prompts_dir),
        )
        result = Classifier(self.settings, client=None, judge=down).run(_email(body))
        self.assertEqual(result.judge_verdict, "error")
        self.assertEqual(result.priority, baseline.priority)


class SystemOneJudgeTests(_ServerFixture):
    def test_agreement_keeps_cheap_path_and_sends_typed_questions(self) -> None:
        self.state.systemone_responses.append(_systemone_response(
            {"sentiment_ok": 0.91, "urgency_ok": 0.84, "priority_ok": 0.78}
        ))
        result = Classifier(self.settings, client=None, judge=self._system_one_judge()).run(
            _email("My order AUR-2001 is late and I want a refund.", subject="Late order")
        )
        self.assertEqual(result.judge_verdict, "agreed")
        self.assertEqual(result.engine, "rules")
        request = self.state.systemone_requests[0]
        self.assertEqual(request["model"], "jev-1.13")
        self.assertIn("Late order", request["state"])
        self.assertEqual(request["questions"]["priority_ok"]["type"], "noul")
        self.assertEqual(request["questions"]["priority"]["type"], "choice")
        self.assertIn("Critical", request["questions"]["priority"]["criteria"])

    def test_disagreement_applies_choice_corrections(self) -> None:
        from email_agent.rules import score_email

        body = "The site is down and I need to order today."
        baseline = score_email(self.settings.rules, body, "", warnings=[])
        self.state.systemone_responses.append(_systemone_response(
            {"sentiment_ok": 0.93, "urgency_ok": 0.22, "priority_ok": 0.18},
            choices={"urgency": ("critical", 0.81), "priority": ("Critical", 0.77)},
        ))
        result = Classifier(self.settings, client=None, judge=self._system_one_judge()).run(_email(body))
        self.assertEqual(result.judge_verdict, "overridden")
        self.assertEqual(result.priority, "Critical")
        self.assertEqual(result.urgency, "critical")
        self.assertEqual(result.sentiment, baseline.sentiment)  # untouched: the sentiment check passed
        self.assertEqual(result.confidence, 0.77)
        self.assertIn("urgency->critical", result.reasoning)

    def test_disagreement_with_primary_adjudicates(self) -> None:
        os.environ["EMAIL_AGENT_REFINE_MODE"] = "escalations_only"  # primary reserved for disputes
        settings = load_settings(None)
        self.state.systemone_responses.append(_systemone_response(
            {"sentiment_ok": 0.9, "urgency_ok": 0.9, "priority_ok": 0.2}, choices={"priority": ("Critical", 0.8)},
        ))
        self.primary_state.responses.append({"priority": "High", "sentiment": "negative", "urgency": "high", "confidence": 0.8})
        result = Classifier(settings, client=self._primary(), judge=self._system_one_judge()).run(
            _email("The site is down and I need to order today.")
        )
        self.assertEqual(result.judge_verdict, "adjudicated")
        self.assertEqual(len(self.primary_state.requests), 1)  # exactly the adjudication

    def test_unusable_response_is_recorded_as_error(self) -> None:
        self.state.systemone_responses.append({"model": "jev-1.13.0", "answers": {}})
        result = Classifier(self.settings, client=None, judge=self._system_one_judge()).run(
            _email("My order AUR-2002 has not arrived.")
        )
        self.assertEqual(result.judge_verdict, "error")

    def test_draft_review_flags_missing_next_step(self) -> None:
        judge = self._system_one_judge()
        self.state.systemone_responses.append(_systemone_response(
            {"no_promises_outside_policy": 0.95, "no_prohibited_content": 0.97, "has_next_step": 0.2},
            choices={"verdict": ("needs_revision", 0.85)},
        ))
        review = judge.review_draft(_email("My order is late."), "Dear customer, we are sorry.", "policy text")
        assert review is not None
        self.assertEqual(review["status"], "needs_revision")
        self.assertTrue(any("next step" in issue for issue in review["issues"]))
        self.assertEqual(review["final_confidence"], 0.85)

        self.state.systemone_responses.append(_systemone_response(
            {"no_promises_outside_policy": 0.9, "no_prohibited_content": 0.9, "has_next_step": 0.8},
            choices={"verdict": ("approved", 0.9)},
        ))
        review = judge.review_draft(_email("My order is late."), "Dear customer, next step: reply with your order id.", "policy")
        assert review is not None
        self.assertEqual(review["status"], "approved")


class ConfigAndCostTests(_ServerFixture):
    def test_jev_defaults_and_provider_detection(self) -> None:
        self.assertEqual(load_settings(None).judge_model, "jev-1.13")

        os.environ["EMAIL_AGENT_JUDGE_LLM"] = "jev"
        os.environ["OPENCODE_API_KEY"] = "zen-key"  # OpenCode Zen key satisfies the judge channel
        settings = load_settings(None)
        self.assertEqual(settings.judge_provider, "jev")
        self.assertEqual(settings.judge_api_key, "zen-key")
        self.assertEqual(settings.judge_base_url, DEFAULT_JUDGE_BASE_URL)
        channel = build_judge(settings)
        self.assertIsInstance(channel, SystemOneJudge)
        assert isinstance(channel, SystemOneJudge)
        self.assertEqual(channel.client.model, "jev-1.13")
        self.assertEqual(channel.client.base_url, DEFAULT_JUDGE_BASE_URL)

        # without any key the judge disables itself instead of failing mid-batch
        os.environ.pop("OPENCODE_API_KEY")
        self.assertEqual(load_settings(None).judge_provider, "none")

    def test_chat_api_override_still_supported(self) -> None:
        os.environ.update({
            "EMAIL_AGENT_JUDGE_LLM": "some-cheap-model",
            "EMAIL_AGENT_JUDGE_API": "chat",
            "EMAIL_AGENT_JUDGE_BASE_URL": self.base_url,
            "EMAIL_AGENT_JUDGE_API_KEY": "k",
        })
        channel = build_judge(load_settings(None))
        self.assertIsInstance(channel, ChatJudge)

    def test_escalations_only_spends_nothing_when_judge_agrees(self) -> None:
        os.environ.update({
            "EMAIL_AGENT_REFINE_MODE": "escalations_only",
            "EMAIL_AGENT_LLM": "openai", "EMAIL_AGENT_API_KEY": "k",
            "EMAIL_AGENT_BASE_URL": self.primary_base_url, "EMAIL_AGENT_MODEL": "gpt-mock",
            "EMAIL_AGENT_JUDGE_LLM": "jev", "EMAIL_AGENT_JUDGE_BASE_URL": self.base_url,
            "EMAIL_AGENT_JUDGE_API_KEY": "k", "EMAIL_AGENT_JUDGE_API": "chat",
        })
        settings = load_settings(None)
        self.assertEqual(settings.refine_mode, "escalations_only")

        self.state.responses.append({"agree": True, "reason": "defensible"})
        result = Classifier(settings, client=self._primary(), judge=self._chat_judge()).run(
            _email("My order AUR-1005 is late.")
        )
        self.assertEqual(result.judge_verdict, "agreed")
        self.assertEqual(result.engine, "rules")
        self.assertEqual(len(self.state.requests), 1)          # one cheap judge call
        self.assertEqual(len(self.primary_state.requests), 0)  # premium untouched

    def test_escalations_only_spends_one_call_on_disagreement(self) -> None:
        os.environ.update({
            "EMAIL_AGENT_REFINE_MODE": "escalations_only",
            "EMAIL_AGENT_LLM": "openai", "EMAIL_AGENT_API_KEY": "k",
            "EMAIL_AGENT_BASE_URL": self.primary_base_url, "EMAIL_AGENT_MODEL": "gpt-mock",
            "EMAIL_AGENT_JUDGE_LLM": "jev", "EMAIL_AGENT_JUDGE_BASE_URL": self.base_url,
            "EMAIL_AGENT_JUDGE_API_KEY": "k",
        })
        settings = load_settings(None)
        self.state.systemone_responses.append(_systemone_response(
            {"sentiment_ok": 0.9, "urgency_ok": 0.9, "priority_ok": 0.2}, choices={"priority": ("Critical", 0.8)},
        ))
        self.primary_state.responses.append({"priority": "Critical", "sentiment": "negative", "urgency": "critical", "confidence": 0.8})
        result = Classifier(settings, client=self._primary(), judge=self._system_one_judge()).run(
            _email("The site is down and I need to order today.")
        )
        self.assertEqual(result.judge_verdict, "adjudicated")
        self.assertEqual(len(self.state.systemone_requests), 1)
        self.assertEqual(len(self.primary_state.requests), 1)  # exactly one adjudication


if __name__ == "__main__":
    unittest.main()
