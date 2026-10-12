"""Inbox watcher: poll a folder, process new emails, keep SQLite live for the UI.

    python email-agent.py watch --input data/inbox --interval 2

Each sweep parses `*.eml` / `*.txt`, skips whatever is already stored (idempotent by `message_id`),
*claims* the email so a second watcher cannot double-process it, and runs the graph pipeline — which
writes every stage to SQLite as it completes. That is exactly what the Streamlit live view reads.
A file that cannot be parsed is recorded once as an error row and the sweep continues.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .analytics import RunLogger
from .config import Settings
from .graph import GraphOrchestrator
from .models import AgentState
from .parser import parse_file
from .storage import Repository

SUFFIXES = {".eml", ".txt"}


def inbox_files(input_dir: str | Path) -> list[Path]:
    """Candidate files, oldest first — a dropped backlog is processed in arrival order."""
    root = Path(input_dir)
    if not root.is_dir():
        return []
    files = [path for path in root.iterdir() if path.is_file() and path.suffix.lower() in SUFFIXES]
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def sweep(
    orchestrator: GraphOrchestrator,
    repo: Repository,
    input_dir: str | Path,
    now: datetime | None = None,
    limit: int | None = None,
    force: bool = False,
) -> list[AgentState]:
    """One pass over the folder; returns the emails processed in this pass."""
    processed = repo.processed_message_ids()
    produced: list[AgentState] = []
    for path in inbox_files(input_dir):
        if limit is not None and len(produced) >= limit:
            break
        try:
            email = parse_file(path)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the sweep
            repo.record_file_error(path, f"{type(exc).__name__}: {exc}")
            continue
        if not force and email.message_id in processed:
            continue
        repo.claim_email(email)  # live marker: the UI shows the email before it is processed
        produced.append(orchestrator.process(email, now=now))
        processed.add(email.message_id)
    return produced


def watch(
    settings: Settings,
    input_dir: str | Path,
    interval: float = 2.0,
    now: datetime | None = None,
    once: bool = False,
    limit: int | None = None,
    force: bool = False,
    repo: Repository | None = None,
    logger: RunLogger | None = None,
    orchestrator: GraphOrchestrator | None = None,
    on_email: Callable[[AgentState], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    max_sweeps: int | None = None,
) -> list[AgentState]:
    """Poll `input_dir` until interrupted (`once=True` does a single sweep).

    `max_sweeps` and `sleep` exist for tests: they make the loop deterministic without waiting.
    """
    owns_repo = repo is None
    repo = repo if repo is not None else Repository(settings.db_path)
    logger = logger if logger is not None else RunLogger(settings.root / "storage" / "logs")
    orchestrator = orchestrator if orchestrator is not None else GraphOrchestrator(settings, repo, logger)
    states: list[AgentState] = []
    sweeps = 0
    try:
        while True:
            produced = sweep(orchestrator, repo, input_dir, now=now, limit=limit, force=force)
            states.extend(produced)
            for state in produced:
                if on_email is not None:
                    on_email(state)
            sweeps += 1
            if once or (max_sweeps is not None and sweeps >= max_sweeps):
                break
            sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        pass
    finally:
        if owns_repo:
            repo.close()
    return states
