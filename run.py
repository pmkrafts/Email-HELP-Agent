#!/usr/bin/env python3
"""One-command demo runner: environment check -> tests -> pipeline -> summary -> agreement eval.

    python run.py                          # golden set (12 emails), full check
    python run.py --batch 100              # 100-email batch
    python run.py --batch adversarial      # 20 adversarial emails
    python run.py --with-llm               # needs OPENAI_API_KEY (falls back to rules if absent)
    python run.py --skip-tests --skip-eval # just process the batch

Exit codes: 0 = all stages passed; 1 = a stage failed; 2 = environment unusable.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

BATCHES = {
    "samples": ROOT / "data" / "sample_emails",
    "adversarial": ROOT / "data" / "generated_batch",
    "100": ROOT / "data" / "batch_100",
}
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


def _say(step: str, message: str) -> None:
    print(f"[{step}] {message}")


def _run_tests() -> bool:
    _say("2/4", "Running unit tests (unittest, offline) ...")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, text=True, errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    tail = [line for line in proc.stdout.strip().splitlines() if line.strip()][-2:]
    for line in tail:
        print(f"      {line}")
    return proc.returncode == 0


def _run_pipeline(batch_dir: Path, db_path: Path, now: datetime, with_llm: bool, with_judge: bool = False,
                  engine: str | None = None) -> tuple[object, dict]:
    from email_agent.analytics import RunLogger, batch_summary
    from email_agent.config import load_settings
    from email_agent.orchestrator import build_orchestrator
    from email_agent.storage import Repository

    if db_path.exists():
        db_path.unlink()
    if with_judge:
        os.environ.setdefault("EMAIL_AGENT_JUDGE_LLM", "jev")
    if engine:
        os.environ["EMAIL_AGENT_ENGINE"] = engine
    settings = load_settings(None)
    if with_llm:
        os.environ["EMAIL_AGENT_LLM"] = "openai"
        settings = load_settings(None)
        if settings.llm_provider != "openai":
            print("      note: OPENAI_API_KEY not set - falling back to deterministic rules mode")
    if with_judge and settings.judge_provider == "none":
        print("      note: judge channel not configured - falling back to deterministic rules mode")
    repo = Repository(db_path)
    logger = RunLogger(ROOT / "storage" / "logs")
    try:
        orchestrator = build_orchestrator(settings, repo, logger)
    except RuntimeError as exc:
        print(f"      error: {exc}")
        return (None, False), {}
    states, failures, skipped = orchestrator.run_batch(batch_dir, now=now)
    summary = batch_summary(states, failures)
    repo.close()

    _say("3/4", f"Pipeline: {args_label(batch_dir)} (engine: {settings.engine} | mode: {settings.llm_provider} | judge: {settings.judge_provider} | refine: {settings.refine_mode})")
    print(f"      processed {summary['processed']}  failed {summary['failed']}  "
          f"priorities {summary['by_priority']}")
    if summary.get("judge_verdicts"):
        print(f"      judge verdicts {summary['judge_verdicts']}")
    print(f"      reviews {summary['review_status']}  schedules {summary['schedule_status']}")
    print(f"      avg confidence: classification {summary['avg_classification_confidence']} "
          f"/ draft {summary['avg_draft_confidence']}")
    top = sorted(
        [s for s in states if s.classification and s.classification.priority in {"Critical", "High"}],
        key=lambda s: (PRIORITY_ORDER[s.classification.priority], -s.classification.priority_score),
    )[:6]
    if top:
        print("      first critical/high items:")
        for state in top:
            subject = " ".join((state.email.subject or "(no subject)").split())[:52]
            send_at = state.schedule.send_at.strftime("%a %H:%M %Z") if state.schedule and state.schedule.send_at else "-"
            print(f"        {state.classification.priority:<8} {state.classification.sentiment:<8} "
                  f"conf={state.classification.confidence:.2f}  {subject}  -> {send_at}")
    ok = summary["failed"] == 0 and all(state.status != "error" for state in states)
    return (states, ok), summary


def _run_eval(batch_dir: Path, now: datetime) -> bool:
    from email_agent.config import load_settings
    from email_agent.evaluation import evaluate

    if not (batch_dir / "expected.json").exists() and not (batch_dir / "ground_truth.json").exists():
        _say("4/4", "Agreement eval: skipped (no labels in this input)")
        return True
    settings = load_settings(None)
    result = evaluate(settings, batch_dir, now=now)
    passed = result["priority_agreement"] >= 0.8
    _say("4/4", f"Agreement vs labels (n={result['n']}):")
    print(f"      priority  {result['priority_agreement']:.0%}  (acceptance >= 80%)  "
          f"{'PASS' if passed else 'FAIL'}")
    print(f"      sentiment {result['sentiment_agreement']:.0%}   urgency {result['urgency_agreement']:.0%}   "
          f"flags P/R {result['flag_precision']:.2f}/{result['flag_recall']:.2f}")
    return passed


def args_label(batch_dir: Path) -> str:
    for name, path in BATCHES.items():
        if path == batch_dir:
            return f"batch '{name}'"
    return str(batch_dir)


def main(argv: list[str] | None = None) -> int:
    try:  # Windows consoles default to cp1252; never crash on subjects with emoji/accents
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(prog="run.py", description="Run the whole project with one command.")
    parser.add_argument("--batch", choices=sorted(BATCHES), default="samples", help="which bundled batch to process")
    parser.add_argument("--input", help="custom input directory (overrides --batch)")
    parser.add_argument("--with-llm", action="store_true", help="enable OpenAI refinement (needs OPENAI_API_KEY)")
    parser.add_argument("--with-judge", action="store_true",
                        help="enable the cheap judge channel (needs EMAIL_AGENT_JUDGE_BASE_URL/_API_KEY)")
    parser.add_argument("--engine", choices=["sequential", "graph"],
                        help="orchestration engine: sequential (stdlib, default) or graph (LangGraph)")
    parser.add_argument("--now", help="ISO timestamp used as the scheduling clock")
    parser.add_argument("--db", help="SQLite path (default: storage/run-demo.db)")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    args = parser.parse_args(argv)

    print("== Email-HELP-Agent - one-command run ==")
    if sys.version_info < (3, 11):
        _say("1/4", f"Python {sys.version.split()[0]} found - 3.11+ is required")
        return 2
    batch_dir = Path(args.input).resolve() if args.input else BATCHES[args.batch]
    if not batch_dir.exists():
        _say("1/4", f"input directory not found: {batch_dir}")
        return 2
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    engine_note = "LangGraph engine" if args.engine == "graph" else "no dependencies"
    _say("1/4", f"Python {sys.version.split()[0]} OK  |  {engine_note}  |  input: {args_label(batch_dir)}")

    results: dict[str, bool] = {}
    results["tests"] = True if args.skip_tests else _run_tests()
    if not results["tests"]:
        print("      tests FAILED - fix them before trusting the pipeline output")

    (_, pipeline_ok), summary = _run_pipeline(batch_dir, Path(args.db) if args.db else ROOT / "storage" / "run-demo.db", now, args.with_llm, args.with_judge, args.engine)  # noqa: E501
    results["pipeline"] = pipeline_ok

    results["eval"] = True if args.skip_eval else _run_eval(batch_dir, now)

    print("== summary ==")
    print(f"   tests: {'SKIPPED' if args.skip_tests else ('PASS' if results['tests'] else 'FAIL')}  |  "
          f"pipeline: {'PASS' if results['pipeline'] else 'FAIL'}  |  "
          f"eval: {'SKIPPED' if args.skip_eval else ('PASS' if results['eval'] else 'FAIL')}")
    print(f"   artifacts: {ROOT / 'storage'}")
    ok = all(results.values())
    print("== DONE - all stages green ==" if ok else "== DONE - with failures (see above) ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
