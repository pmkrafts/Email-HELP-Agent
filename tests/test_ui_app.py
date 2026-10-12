"""Headless UI smoke test using Streamlit's AppTest (skipped if streamlit is absent)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from streamlit.testing.v1 import AppTest

    HAS_STREAMLIT = True
except Exception:  # noqa: BLE001 - streamlit is optional
    HAS_STREAMLIT = False

from email_agent import watcher  # noqa: E402
from email_agent.config import load_settings  # noqa: E402
from email_agent.storage import Repository  # noqa: E402

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
        self.assertEqual(report["mode"]["engine"], "graph")
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

    def test_topology_panel_renders(self) -> None:
        app = AppTest.from_file(str(APP), default_timeout=90)
        app.run()
        self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
        self.assertFalse([radio for radio in app.sidebar.radio if radio.label == "Orchestration engine"])
        self.assertTrue(any("classify" in code.value and "persist" in code.value for code in app.code),
                        [code.value[:40] for code in app.code])

    def test_live_mode_renders_with_an_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = AppTest.from_file(str(APP), default_timeout=90)
            app.run()
            next(box for box in app.sidebar.checkbox if "Live mode" in box.label).set_value(True)
            app.run()
            next(item for item in app.sidebar.text_input if item.label == "Database").set_value(
                str(Path(tmp) / "empty.db")
            )
            next(item for item in app.sidebar.text_input if item.label == "Inbox folder").set_value(
                str(Path(tmp) / "inbox")
            )
            app.run()
            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            shown = " ".join(item.value for item in app.info)
            self.assertIn("Drop an", shown)
            # the Run button must be visibly disabled in live mode (it does nothing there)
            run_button = next(button for button in app.sidebar.button if "Run pipeline" in button.label)
            self.assertTrue(run_button.disabled)
            self.assertTrue(any("is disabled" in caption.value for caption in app.caption))

    def test_live_mode_processes_the_inbox_itself(self) -> None:
        """The dashboard watches the folder on its own — no separate watcher process needed."""
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            shutil.copy(ROOT / "data" / "sample_emails" / "003_positive_feedback.eml", inbox / "a.eml")
            db_path = Path(tmp) / "self.db"

            app = AppTest.from_file(str(APP), default_timeout=180)
            app.run()
            next(box for box in app.sidebar.checkbox if "Live mode" in box.label).set_value(True)
            app.run()
            next(item for item in app.sidebar.text_input if item.label == "Database").set_value(str(db_path))
            next(item for item in app.sidebar.text_input if item.label == "Inbox folder").set_value(str(inbox))
            app.run()   # this run's tick should pick the file up

            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            body = " ".join(item.value for item in app.markdown) + " ".join(item.value for item in app.caption)
            self.assertIn("Thank you - warranty claim sorted in 10 min", body)
            self.assertIn("last pass: processed 1", body)
            repo = Repository(db_path)
            try:
                self.assertEqual(repo.status_counts(), {"scheduled": 1})
            finally:
                repo.close()

    def test_load_sample_emails_feeds_the_dashboard(self) -> None:
        """The demo affordance: one click fills the inbox and processes it into the dashboard."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "seeded.db"
            inbox = Path(tmp) / "inbox"
            app = AppTest.from_file(str(APP), default_timeout=300)
            app.run()
            next(box for box in app.sidebar.checkbox if "Live mode" in box.label).set_value(True)
            app.run()
            next(item for item in app.sidebar.text_input if item.label == "Database").set_value(str(db_path))
            next(item for item in app.sidebar.text_input if item.label == "Inbox folder").set_value(str(inbox))
            app.run()
            next(button for button in app.sidebar.button if "Load the sample emails" in button.label).click()
            app.run()

            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            body = " ".join(item.value for item in app.success)
            self.assertIn("Copied 12 sample email(s)", body)
            repo = Repository(db_path)
            try:
                # every email reached the scheduled *stage*; the bucket lives in the schedule row
                self.assertEqual(repo.status_counts(), {"scheduled": 12})
                self.assertEqual(repo.summary()["scheduled"], {"scheduled": 10, "junk": 1, "escalated": 1})
            finally:
                repo.close()

    def test_live_row_opens_into_full_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            shutil.copy(ROOT / "data" / "sample_emails" / "001_angry_refund_legal_threat.eml", inbox / "a.eml")
            db_path = Path(tmp) / "detail.db"
            repo = Repository(db_path)
            try:
                watcher.process_once(load_settings(None), inbox, repo=repo)
            finally:
                repo.close()

            app = AppTest.from_file(str(APP), default_timeout=180)
            app.run()
            next(box for box in app.sidebar.checkbox if "Live mode" in box.label).set_value(True)
            app.run()
            next(item for item in app.sidebar.text_input if item.label == "Database").set_value(str(db_path))
            next(item for item in app.sidebar.text_input if item.label == "Inbox folder").set_value(
                str(Path(tmp) / "other-inbox")
            )
            app.run()

            picker = next(item for item in app.selectbox if item.label == "Open an email for the full detail")
            picker.set_value(picker.options[1])   # the first real row ("—" is the empty choice)
            app.run()

            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            body = " ".join(item.value for item in app.markdown) + " ".join(item.value for item in app.caption)
            self.assertIn("Classification", body)
            self.assertIn("Review & schedule", body)
            self.assertIn("Agent trace", body)
            self.assertIn("legal_threat", body)          # the signal list
            self.assertTrue(any(box.value for box in app.text_area), [box.label for box in app.text_area])

    def test_live_mode_shows_rows_from_the_watcher_database(self) -> None:
        """Seed a database the way the watcher does, then point the console at it."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.db"
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            shutil.copy(ROOT / "data" / "sample_emails" / "003_positive_feedback.eml", inbox / "a.eml")
            repo = Repository(db_path)
            try:
                watcher.watch(load_settings(None), inbox, repo=repo, once=True)
            finally:
                repo.close()

            app = AppTest.from_file(str(APP), default_timeout=90)
            app.run()
            next(box for box in app.sidebar.checkbox if "Live mode" in box.label).set_value(True)
            app.run()
            next(item for item in app.sidebar.text_input if item.label == "Database").set_value(str(db_path))
            next(item for item in app.sidebar.text_input if item.label == "Inbox folder").set_value(
                str(Path(tmp) / "empty-inbox")
            )
            app.run()

            self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
            body = " ".join(item.value for item in app.markdown) + " ".join(item.value for item in app.caption)
            self.assertIn("Thank you - warranty claim sorted in 10 min", body)
            self.assertIn("scheduled", body)
            self.assertIn("classification 0.", body)   # the confidence score is on screen
            self.assertTrue("last update" in body or "idle for" in body,
                            [item.value for item in app.caption])

    def test_a_stale_in_memory_module_gets_an_actionable_message(self) -> None:
        """A Streamlit server started before an update keeps the old package in memory."""
        import email_agent.report as report_module
        import email_agent.watcher as watcher_module

        cases = [(report_module, "idle_note"), (watcher_module, "copy_samples")]
        for module, attribute in cases:
            with self.subTest(attribute=attribute):
                saved = getattr(module, attribute)
                delattr(module, attribute)
                try:
                    app = AppTest.from_file(str(APP), default_timeout=90)
                    app.run()
                    self.assertFalse(list(app.exception), [str(e.value) for e in app.exception])
                    message = " ".join(error.value for error in app.error)
                    self.assertIn("Restart it", message)
                    self.assertIn(attribute, message)     # names what is missing
                finally:
                    setattr(module, attribute, saved)

    def test_missing_langgraph_shows_the_install_hint(self) -> None:
        """Without langgraph the run must surface the install hint, not a traceback."""
        import email_agent.report as report_module

        hint = "needs LangGraph: pip install langgraph"
        original = report_module.build_report
        report_module.build_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(hint))
        try:
            app = AppTest.from_file(str(APP), default_timeout=90)
            app.run()
            self._cap_run(app, 2)
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
