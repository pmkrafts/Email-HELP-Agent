"""Parser tests: every edge case present in the sample batches."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from email_agent.parser import parse_dir, parse_file  # noqa: E402

SAMPLES = ROOT / "data" / "sample_emails"
GEN = ROOT / "data" / "generated_batch" / "emails"
B100 = ROOT / "data" / "batch_100" / "emails"


class ParserTests(unittest.TestCase):
    def test_eml_basic(self) -> None:
        email = parse_file(SAMPLES / "001_angry_refund_legal_threat.eml")
        self.assertIn("lawyer", email.body_text.lower())
        self.assertEqual(email.from_address, "daniel.reyes@example.com")
        self.assertTrue(email.message_id.startswith("<"))
        self.assertIsNotNone(email.date)

    def test_empty_subject(self) -> None:
        email = parse_file(SAMPLES / "010_empty_subject.eml")
        self.assertEqual(email.subject, "")
        self.assertIn("empty_subject", email.warnings)

    def test_empty_body(self) -> None:
        path = next(B100.glob("*vague_short_03*"))
        email = parse_file(path)
        self.assertEqual(email.body_text, "")
        self.assertIn("empty_body", email.warnings)

    def test_latin1_subject_repair(self) -> None:
        email = parse_file(SAMPLES / "012_latin1_encoding.eml")
        self.assertIn("Beschädigt", email.subject)
        self.assertIn("Rückerstattung", email.body_text)
        self.assertIn("mojibake_header_fixed", email.warnings)

    def test_html_only_body(self) -> None:
        email = parse_file(GEN / "g12_html_only.eml")
        self.assertIn("AUR-20677", email.body_text)
        self.assertIn("html_only_body", email.warnings)

    def test_txt_both_separators(self) -> None:
        dashed = parse_file(GEN / "g11_technical_questions.txt")
        blank = parse_file(GEN / "g05_positive_refund_thanks.txt")
        for email in (dashed, blank):
            self.assertTrue(email.subject)
            self.assertGreater(len(email.body_text.split()), 20)

    def test_txt_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crlf.txt"
            path.write_bytes(b"From: a@example.com\r\nSubject: CRLF test\r\nDate: 2026-10-11\r\n\r\nLine one.\r\nLine two.\r\n")
            email = parse_file(path)
            self.assertEqual(email.subject, "CRLF test")
            self.assertIn("Line two.", email.body_text)

    def test_mbox_prefix_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mbox.eml"
            path.write_bytes(b"From someone@example.com Thu Aug 22 12:36:23 2002\nFrom: a@example.com\nSubject: hi\n\nbody")
            email = parse_file(path)
            self.assertIn("mbox_prefix_stripped", email.warnings)
            self.assertEqual(email.subject, "hi")

    def test_future_date_flagged(self) -> None:
        email = parse_file(GEN / "g19_future_date.eml")
        self.assertIn("future_date", email.warnings)

    def test_parse_dir_counts(self) -> None:
        parsed, failures = parse_dir(SAMPLES)
        self.assertEqual(len(parsed), 12)
        self.assertEqual(failures, [])

    def test_parse_batch_dirs(self) -> None:
        for directory, expected in ((ROOT / "data" / "generated_batch", 20), (ROOT / "data" / "batch_100", 100)):
            parsed, failures = parse_dir(directory)
            self.assertEqual(len(parsed), expected, directory)
            self.assertEqual(failures, [], directory)


if __name__ == "__main__":
    unittest.main()
