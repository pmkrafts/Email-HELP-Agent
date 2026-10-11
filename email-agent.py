#!/usr/bin/env python3
"""Launcher: run the CLI without installing (adds src/ to sys.path).

Usage:
    python email-agent.py run --input data/sample_emails
    python email-agent.py eval --show-misses
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from email_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
