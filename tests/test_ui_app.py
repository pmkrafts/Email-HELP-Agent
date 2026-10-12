"""Headless UI smoke test using Streamlit's AppTest (skipped if streamlit is absent)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from streamlit.testing.v1 import AppTest

    HAS_STREAMLIT = True
except Exception:  # noqa: BLE001 - streamlit is optional
    HAS_STREAMLIT = False

try:
    import langgraph  # noqa: F401

    HAS_LANGGRAPH = True
except Exception:  # noqa: BLE001 - optional extra
    HAS_LANGGRAPH = False

APP = ROOT / "ui" / "app.py"
ISOLATED_KEYS = (
    "EMAIL_AGENT_LLM", "EMAIL_AGENT_MODEL", "EMAIL_AGENT_API_KEY", "EMAIL_AGENT_BASE_URL",
    "EMAIL_AGENT_JUDGE_LLM", "EMAIL_AGENT_JUDGE_API", "EMAIL_AGENT_JUDGE_BASE_URL",
    "EMAIL_AGENT_JUDGE_API_KEY", "EMAIL_AGENT_JUDGE_MODEL", "EMAIL_AGENT_REFINE_MODE",
    "EMAIL_AGENT_ENV_FILE", "OPENAI_API_KEY", "OPENCODE_API_KEY",
)


@unittest.skipUnless(HAS_STREAMLIT, "streamlit is not installed")
class UiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ISOLATED_KEYS}
        for key in ISOLATED_KEYS:
            os.environ.pop(key, None)
        os.environ["EMAIL_AGENT_ENV_FILE"] = str(Path(__file__).resolve().parent / ".no-env-file")

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_app_renders_and_runs_a_batch(self) -> None:
        app = AppTest.from_file(str(APP), default_timeout=90)
        app.run()
        self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
        self.assertTrue(any("Run pipeline" in button.label for button in app.sidebar.button))

        # full batch by default now (checkbox pre-checked) - keep this run small anyway
        self._cap_run(app, 2)
        self._click_run(app)

        self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
        report = app.session_state["report"]
        self.assertEqual(report["summary"]["processed"], 2)
        self.assertEqual(report["mode"]["primary"], "none")  # rules-only default tier
        self.assertTrue(report["rows"][0]["subject"])

        # every view must render, including the full-content, drafts-only and schedule modes
        for view in ("All emails (full content)", "Drafts only", "Dispatch schedule", "Table + inspector"):
            view_radio = next(radio for radio in app.radio if radio.label == "View")
            view_radio.set_value(view)
            app.run()
            self.assertFalse(list(app.exception), f"{view}: {[str(e.value) for e in app.exception]}")
            expanders = getattr(app, "expander", None)
            if view != "Table + inspector" and expanders is not None:
                self.assertGreaterEqual(len(expanders), 1)

    def test_engine_defaults_to_sequential(self) -> None:
        app = AppTest.from_file(str(APP), default_timeout=90)
        app.run()
        engine_radio = next(radio for radio in app.sidebar.radio if radio.label == "Orchestration engine")
        self.assertTrue(engine_radio.value.startswith("Sequential"))

    @unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed (optional extra)")
    def test_graph_engine_runs_from_the_sidebar(self) -> None:
        app = AppTest.from_file(str(APP), default_timeout=180)
        app.run()
        self._cap_run(app, 2)
        engine_radio = next(radio for radio in app.sidebar.radio if radio.label == "Orchestration engine")
        engine_radio.set_value("LangGraph")
        app.run()
        self._click_run(app)
        self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
        report = app.session_state["report"]
        self.assertEqual(report["mode"]["engine"], "graph")
        self.assertEqual(report["summary"]["processed"], 2)
        self.assertTrue(any("classify" in code.value for code in app.code),   # topology panel
                        [code.value[:40] for code in app.code])

    def test_missing_graph_extra_shows_the_install_hint(self) -> None:
        """Without langgraph the run must surface the install hint, not a traceback."""
        import email_agent.report as report_module

        hint = "engine 'graph' needs LangGraph: pip install 'email-agent[graph]'"
        original = report_module.build_report
        report_module.build_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(hint))
        try:
            app = AppTest.from_file(str(APP), default_timeout=90)
            app.run()
            self._cap_run(app, 2)
            engine_radio = next(radio for radio in app.sidebar.radio if radio.label == "Orchestration engine")
            engine_radio.set_value("LangGraph")
            app.run()
            self._click_run(app)
            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            self.assertIn(hint, [error.value for error in app.error])
        finally:
            report_module.build_report = original

    def _cap_run(self, app, limit: int) -> None:
        """Uncheck 'entire batch' and set the cap, so the test bills nothing."""
        whole_batch = next(box for box in app.sidebar.checkbox if "entire batch" in box.label)
        whole_batch.set_value(False)
        app.run()
        app.sidebar.slider[0].set_value(limit)
        app.run()

    def _click_run(self, app) -> None:
        run_button = next(button for button in app.sidebar.button if "Run pipeline" in button.label)
        run_button.click()
        app.run()


if __name__ == "__main__":
    unittest.main()
