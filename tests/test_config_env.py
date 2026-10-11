"""`.env` loader tests (stdlib-only dotenv semantics)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.config import load_dotenv  # noqa: E402


class DotenvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ("DOTENV_TEST_A", "DOTENV_TEST_B", "DOTENV_TEST_C")}
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_parses_values_and_skips_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_bytes(
                b"# comment line\r\n"
                b"\r\n"
                b"DOTENV_TEST_A=plain-value\r\n"
                b'DOTENV_TEST_B="quoted value"  # trailing comment\r\n'
                b"DOTENV_TEST_C='single'\r\n"
                b"NOT_A_PAIR\r\n"
            )
            applied = load_dotenv(env_file)
            self.assertEqual(applied["DOTENV_TEST_A"], "plain-value")
            self.assertEqual(os.environ["DOTENV_TEST_B"], "quoted value")
            self.assertEqual(os.environ["DOTENV_TEST_C"], "single")

    def test_real_environment_wins(self) -> None:
        os.environ["DOTENV_TEST_A"] = "from-shell"
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("DOTENV_TEST_A=from-file\nDOTENV_TEST_B=file-only\n", encoding="utf-8")
            load_dotenv(env_file)
            self.assertEqual(os.environ["DOTENV_TEST_A"], "from-shell")  # not overridden
            self.assertEqual(os.environ["DOTENV_TEST_B"], "file-only")

    def test_missing_file_is_harmless(self) -> None:
        self.assertEqual(load_dotenv(Path("does-not-exist.env")), {})


if __name__ == "__main__":
    unittest.main()
