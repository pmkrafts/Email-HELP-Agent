"""Inbox watcher: poll a folder, process new emails, keep SQLite live for the UI.

    python email-agent.py watch --input data/inbox --interval 2

Each sweep parses `*.eml` / `*.txt`, skips whatever is already stored (idempotent by `message_id`),
*claims* the email so a second watcher cannot double-process it, and runs the graph pipeline — which
writes every stage to SQLite as it completes. That is exactly what the Streamlit live view reads.
A file that cannot be parsed is recorded once as an error row and the sweep continues.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
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


@dataclass
class SweepResult:
    """What one pass over the folder did — the CLI prints this so nothing is silently skipped."""

    processed: list[AgentState] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # already stored: use --force or a fresh --db
    errors: list[tuple[str, str]] = field(default_factory=list)


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
    input_dir: str | Path | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    force: bool = False,
    files: list[Path] | None = None,
) -> SweepResult:
    """One pass over a folder (or an explicit file list): process, and report skips and errors."""
    processed_ids = repo.processed_message_ids()
    result = SweepResult()
    for path in files if files is not None else inbox_files(input_dir or ""):
        if limit is not None and len(result.processed) >= limit:
            break
        try:
            email = parse_file(path)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the sweep
            error = f"{type(exc).__name__}: {exc}"
            repo.record_file_error(path, error)
            result.errors.append((path.name, error))
            continue
        if not force and email.message_id in processed_ids:
            result.skipped.append(path.name)
            continue
        repo.claim_email(email)  # live marker: the UI shows the email before it is processed
        result.processed.append(orchestrator.process(email, now=now))
        processed_ids.add(email.message_id)
    return result


def process_once(
    settings: Settings,
    input_dir: str | Path | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    force: bool = False,
    files: list[Path] | None = None,
    repo: Repository | None = None,
    logger: RunLogger | None = None,
    orchestrator: GraphOrchestrator | None = None,
) -> SweepResult:
    """One sweep with its own repository — what the console's live mode calls on every tick."""
    owns_repo = repo is None
    repo = repo if repo is not None else Repository(settings.db_path)
    try:
        logger = logger if logger is not None else RunLogger(settings.root / "storage" / "logs")
        orchestrator = orchestrator if orchestrator is not None else GraphOrchestrator(settings, repo, logger)
        return sweep(orchestrator, repo, input_dir, now=now, limit=limit, force=force, files=files)
    finally:
        if owns_repo:
            repo.close()


def stored_files(repo: Repository, limit: int = 50) -> list[Path]:
    """The files behind the stored emails that still exist — what a reprocess would re-run."""
    rows = repo.conn.execute(
        "SELECT raw_path FROM emails WHERE raw_path IS NOT NULL ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return [Path(row["raw_path"]) for row in rows if row["raw_path"] and Path(row["raw_path"]).is_file()]


def copy_samples(inbox_dir: str | Path, source_dir: str | Path, limit: int = 12) -> list[Path]:
    """Copy bundled sample emails into the inbox (demo affordance) — existing files are left alone."""
    import shutil

    target = Path(inbox_dir)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in sorted(Path(source_dir).iterdir()):
        if len(copied) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        destination = target / path.name
        if destination.exists():
            continue
        shutil.copy(path, destination)
        copied.append(destination)
    return copied


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
    on_sweep: Callable[[SweepResult], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    max_sweeps: int | None = None,
) -> list[AgentState]:
    """Poll `input_dir` until interrupted (`once=True` does a single sweep).

    `on_sweep` reports what each pass did (processed / skipped / unparsable); `max_sweeps` and
    `sleep` exist for tests: they make the loop deterministic without waiting.
    """
    owns_repo = repo is None
    repo = repo if repo is not None else Repository(settings.db_path)
    logger = logger if logger is not None else RunLogger(settings.root / "storage" / "logs")
    orchestrator = orchestrator if orchestrator is not None else GraphOrchestrator(settings, repo, logger)
    states: list[AgentState] = []
    sweeps = 0
    try:
        while True:
            result = sweep(orchestrator, repo, input_dir, now=now, limit=limit, force=force)
            states.extend(result.processed)
            for state in result.processed:
                if on_email is not None:
                    on_email(state)
            if on_sweep is not None:
                on_sweep(result)
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
