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

        # keep the run small, then press the button
        app.sidebar.slider[0].set_value(2)
        run_button = next(button for button in app.sidebar.button if "Run pipeline" in button.label)
        run_button.click()
        app.run()

        self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
        report = app.session_state["report"]
        self.assertEqual(report["summary"]["processed"], 2)
        self.assertEqual(report["mode"]["primary"], "none")  # rules-only default tier
        self.assertTrue(report["rows"][0]["subject"])


if __name__ == "__main__":
    unittest.main()
