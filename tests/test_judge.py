"""Judge-channel (JEV) tests against a mock OpenAI-compatible endpoint — stdlib only.

Covers the cost-ladder contract:
  * judge agrees            -> cheap path kept, exactly one judge call, no primary call
  * judge disagrees, no primary -> judge correction applied (marked "overridden")
  * judge disagrees, primary present -> that single email is adjudicated ("adjudicated", 2 calls)
  * judge endpoint down     -> clean fallback to the rules baseline ("error"), batch never blocked
  * judge stays disabled unless a base URL + key are configured
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
from email_agent.config import load_settings  # noqa: E402
from email_agent.llm import LLMClient  # noqa: E402
from email_agent.models import ParsedEmail  # noqa: E402


class _MockState:
    def __init__(self) -> None:
        self.responses: list[dict] = []
        self.requests: list[dict] = []


def _make_handler(state: _MockState):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length", 0))
            state.requests.append(json.loads(self.rfile.read(length) or b"{}"))
            verdict = state.responses.pop(0) if state.responses else {"agree": True, "reason": "default"}
            body = json.dumps({"choices": [{"message": {"content": json.dumps(verdict)}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence request logging
            pass

    return Handler


def _email(body: str, subject: str = "Test") -> ParsedEmail:
    return ParsedEmail(
        message_id="test-1", from_address="a@example.com", from_name="Alex", to_addresses=[],
        subject=subject, body_text=body, date=None, raw_path="test.eml", source_format="eml",
    )


class JudgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.state = _MockState()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.state))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"
        # second endpoint standing in for the premium provider (OpenAI GPT)
        cls.primary_state = _MockState()
        cls.primary_server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(cls.primary_state))
        cls.primary_thread = threading.Thread(target=cls.primary_server.serve_forever, daemon=True)
        cls.primary_thread.start()
        cls.primary_base_url = f"http://127.0.0.1:{cls.primary_server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.primary_server.shutdown()
        cls.primary_server.server_close()

    def setUp(self) -> None:
        self.state.responses.clear()
        self.state.requests.clear()
        self.primary_state.responses.clear()
        self.primary_state.requests.clear()
        self.settings = load_settings(None)

    def _client(self, model: str = "jev-test") -> LLMClient:
        return LLMClient(api_key="test-key", model=model, base_url=self.base_url)

    def test_judge_agrees_keeps_cheap_path(self) -> None:
        self.state.responses.append({"agree": True, "reason": "labels defensible"})
        classifier = Classifier(self.settings, client=None, judge=self._client())
        result = classifier.run(_email("My order AUR-1001 is late and I would like a refund."))
        self.assertEqual(result.judge_verdict, "agreed")
        self.assertEqual(result.engine, "rules")
        self.assertEqual(len(self.state.requests), 1)
        payload = json.loads(self.state.requests[0]["messages"][1]["content"])
        self.assertIn("proposed", payload)

    def test_judge_disagrees_override_without_primary(self) -> None:
        self.state.responses.append({
            "agree": False, "priority": "High", "urgency": "high", "sentiment": "negative",
            "confidence": 0.7, "reason": "deadline language",
        })
        classifier = Classifier(self.settings, client=None, judge=self._client())
        result = classifier.run(_email("Hello, quick question about the invoice for order AUR-1002."))
        self.assertEqual(result.judge_verdict, "overridden")
        self.assertEqual(result.priority, "High")
        self.assertTrue(result.engine.endswith("+judge-override"))
        self.assertEqual(len(self.state.requests), 1)

    def test_judge_disagreement_routes_single_email_to_primary(self) -> None:
        # with the primary enabled the ladder is: refine (1) -> judge (2) -> adjudicate (3)
        self.state.responses.append({"priority": "Medium", "sentiment": "neutral", "urgency": "medium", "confidence": 0.6, "reasoning": "refined"})
        self.state.responses.append({"agree": False, "priority": "High", "reason": "deadline"})
        self.state.responses.append({"priority": "High", "sentiment": "negative", "urgency": "high", "confidence": 0.8, "reasoning": "adjudicated"})
        classifier = Classifier(self.settings, client=self._client("primary"), judge=self._client("jev"))
        result = classifier.run(_email("Hello, quick question about the invoice for order AUR-1003."))
        self.assertEqual(result.judge_verdict, "adjudicated")
        self.assertEqual(result.priority, "High")
        self.assertIn("adjudicated", result.engine)
        self.assertEqual(len(self.state.requests), 3)  # refine + judge + one adjudication only

    def test_judge_endpoint_down_falls_back(self) -> None:
        from email_agent.rules import score_email

        body = "My order AUR-1004 has not arrived and the tracking has not moved."
        baseline = score_email(self.settings.rules, body, "")
        down = LLMClient(api_key="x", model="jev", base_url="http://127.0.0.1:9/v1", max_retries=0, timeout=1.0)
        classifier = Classifier(self.settings, client=None, judge=down)
        result = classifier.run(_email(body))
        self.assertEqual(result.judge_verdict, "error")
        self.assertEqual(result.engine, "rules")
        self.assertEqual(result.priority, baseline.priority)  # baseline survives untouched

    def test_judge_disabled_without_endpoint_or_key(self) -> None:
        saved = {key: os.environ.get(key) for key in (
            "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_BASE_URL", "EMAIL_AGENT_JUDGE_API_KEY",
        )}
        try:
            os.environ["EMAIL_AGENT_JUDGE_LLM"] = "jev"
            os.environ.pop("EMAIL_AGENT_JUDGE_BASE_URL", None)
            os.environ.pop("EMAIL_AGENT_JUDGE_API_KEY", None)
            self.assertEqual(load_settings(None).judge_provider, "none")
            os.environ["EMAIL_AGENT_JUDGE_BASE_URL"] = self.base_url
            os.environ["EMAIL_AGENT_JUDGE_API_KEY"] = "test-key"
            self.assertEqual(load_settings(None).judge_provider, "jev")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_jev_model_id_and_generic_providers(self) -> None:
        from email_agent.llm import get_client, get_judge_client

        keys = ("EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
                "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_BASE_URL", "EMAIL_AGENT_JUDGE_API_KEY",
                "EMAIL_AGENT_JUDGE_MODEL", "OPENAI_API_KEY")
        saved = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            # judge defaults to the JEV model id shown in the gateway ("Jev 1.13")
            self.assertEqual(load_settings(None).judge_model, "jev-1.13")
            self.assertEqual(get_judge_client("jev", "k", "", "https://gateway.example/v1").model, "jev-1.13")

            # JEV can serve as the primary (classification/drafting) model too: provider names are free-form
            os.environ["EMAIL_AGENT_LLM"] = "jev"
            os.environ["EMAIL_AGENT_API_KEY"] = "k"
            os.environ["EMAIL_AGENT_BASE_URL"] = "https://gateway.example/v1"
            os.environ["EMAIL_AGENT_MODEL"] = "jev-1.13"
            settings = load_settings(None)
            self.assertEqual(settings.llm_provider, "jev")
            self.assertEqual(settings.llm_model, "jev-1.13")
            self.assertIsNotNone(get_client("jev", "k", "jev-1.13", "https://gateway.example/v1"))

            # without a key the provider disables itself instead of failing mid-batch
            os.environ.pop("EMAIL_AGENT_API_KEY")
            self.assertEqual(load_settings(None).llm_provider, "none")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


    def test_escalations_only_spends_nothing_when_judge_agrees(self) -> None:
        """The JEV + GPT pairing in cost-first mode: agreement == zero premium calls."""
        from email_agent.llm import get_client, get_judge_client

        saved = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith("EMAIL_AGENT_"):
                    os.environ.pop(key)
            os.environ.update({
                "EMAIL_AGENT_REFINE_MODE": "escalations_only",
                "EMAIL_AGENT_LLM": "openai", "EMAIL_AGENT_API_KEY": "k",
                "EMAIL_AGENT_BASE_URL": self.primary_base_url, "EMAIL_AGENT_MODEL": "gpt-mock",
                "EMAIL_AGENT_JUDGE_LLM": "jev", "EMAIL_AGENT_JUDGE_BASE_URL": self.base_url,
            })
            settings = load_settings(None)
            self.assertEqual(settings.refine_mode, "escalations_only")
            self.assertEqual(settings.judge_model, "jev-1.13")
            self.assertEqual(settings.judge_api_key, "k")  # falls back to the shared gateway key
            primary = get_client(settings.llm_provider, settings.llm_api_key, settings.llm_model, settings.llm_base_url)
            judge = get_judge_client(settings.judge_provider, settings.judge_api_key, settings.judge_model, settings.judge_base_url)

            self.state.responses.append({"agree": True, "reason": "defensible"})
            result = Classifier(settings, client=primary, judge=judge).run(_email("My order AUR-1005 is late."))
            self.assertEqual(result.judge_verdict, "agreed")
            self.assertEqual(result.engine, "rules")
            self.assertEqual(len(self.state.requests), 1)              # one cheap judge call
            self.assertEqual(len(self.primary_state.requests), 0)      # premium untouched
            self.assertEqual(self.state.requests[0]["model"], "jev-1.13")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_escalations_only_spends_one_call_on_disagreement(self) -> None:
        from email_agent.llm import get_client, get_judge_client

        saved = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith("EMAIL_AGENT_"):
                    os.environ.pop(key)
            os.environ.update({
                "EMAIL_AGENT_REFINE_MODE": "escalations_only",
                "EMAIL_AGENT_LLM": "openai", "EMAIL_AGENT_API_KEY": "k",
                "EMAIL_AGENT_BASE_URL": self.primary_base_url, "EMAIL_AGENT_MODEL": "gpt-mock",
                "EMAIL_AGENT_JUDGE_LLM": "jev", "EMAIL_AGENT_JUDGE_BASE_URL": self.base_url,
            })
            settings = load_settings(None)
            primary = get_client(settings.llm_provider, settings.llm_api_key, settings.llm_model, settings.llm_base_url)
            judge = get_judge_client(settings.judge_provider, settings.judge_api_key, settings.judge_model, settings.judge_base_url)

            self.state.responses.append({"agree": False, "priority": "High", "reason": "deadline language"})
            self.primary_state.responses.append({"priority": "High", "sentiment": "negative", "urgency": "high", "confidence": 0.8})
            result = Classifier(settings, client=primary, judge=judge).run(_email("My order AUR-1006 is late and I need it today."))
            self.assertEqual(result.judge_verdict, "adjudicated")
            self.assertEqual(len(self.state.requests), 1)              # the judge call
            self.assertEqual(len(self.primary_state.requests), 1)      # exactly one adjudication
            self.assertEqual(self.primary_state.requests[0]["model"], "gpt-mock")
        finally:
            os.environ.clear()
            os.environ.update(saved)


if __name__ == "__main__":
    unittest.main()
