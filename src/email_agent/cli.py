"""Command-line interface (stdlib argparse): run | view | summary | eval."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .analytics import RunLogger, batch_summary
from .config import load_settings, project_root
from .evaluation import evaluate
from .orchestrator import Orchestrator
from .storage import Repository

BATCHES = ["data/sample_emails", "data/generated_batch", "data/batch_100"]


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fmt(value, width: int, default: str = "-") -> str:
    text = str(value) if value is not None else default
    return text[: width - 1].ljust(width)[:width]


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    now = _parse_now(args.now)
    repo = Repository(settings.db_path)
    logger = RunLogger(settings.root / "storage" / "logs")
    orchestrator = Orchestrator(settings, repo, logger)
    states, failures, skipped = orchestrator.run_batch(args.input, now=now, force=args.force)

    print(f"Processing: {args.input}  (mode: {settings.llm_provider})")
    for index, state in enumerate(states, 1):
        c = state.classification
        schedule = state.schedule
        send_at = schedule.send_at.strftime("%a %H:%M") if schedule and schedule.send_at else "-"
        print(
            f"[{index:>3}/{len(states)}] {_fmt(state.email.subject or '(no subject)', 46)} "
            f"{_fmt(c.priority if c else '?', 9)} {_fmt(c.sentiment if c else '?', 9)} {_fmt(c.urgency if c else '?', 9)} "
            f"conf={c.confidence if c else 0:.2f} -> {send_at} ({schedule.status if schedule else state.status})"
        )
    summary = batch_summary(states, failures)
    print("\nBatch summary:")
    print(json.dumps(summary, indent=2, default=str))
    if skipped:
        print(f"Skipped (already processed): {len(skipped)}")
    repo.close()
    return 0 if not failures or states else 1


def cmd_view(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    repo = Repository(settings.db_path)
    rows = repo.rows_for_view(priority=args.priority, limit=args.limit)
    if not rows:
        print("No processed emails yet. Run: python -m email_agent run --input data/sample_emails")
        return 0
    print(f"{_fmt('Priority', 9)}{_fmt('Sent', 9)}{_fmt('Conf', 6)}{_fmt('Review', 16)}{_fmt('Send at', 18)}Subject")
    for row in rows:
        conf = row.get("confidence")
        conf_text = f"{conf:.2f}" if conf is not None else None
        print(
            f"{_fmt(row.get('priority'), 9)}{_fmt(row.get('sentiment'), 9)}"
            f"{_fmt(conf_text, 6)}"
            f"{_fmt(row.get('review_status'), 16)}{_fmt(row.get('send_at'), 18)}{row.get('subject') or '(no subject)'}"
        )
    if args.show_id:
        print("\nMessage IDs:")
        for row in rows:
            print(f"  {row.get('message_id')}  {row.get('subject') or '(no subject)'}")
    repo.close()
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    repo = Repository(settings.db_path)
    print(json.dumps(repo.summary(), indent=2, default=str))
    repo.close()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    batches = args.batches or BATCHES
    now = _parse_now(args.now)
    failed = False
    for batch in batches:
        path = Path(batch)
        if not path.exists():
            print(f"{batch}: skipped (not found)")
            continue
        result = evaluate(settings, path, now=now)
        n = result["n"]
        print(f"\n== {batch}  (n={n}, parse failures={len(result['parse_failures'])})")
        print(f"   priority agreement : {result['priority_agreement']:.0%}  (acceptance >= 80%)")
        print(f"   sentiment agreement: {result['sentiment_agreement']:.0%}")
        print(f"   urgency agreement  : {result['urgency_agreement']:.0%}")
        if result["bucket_agreement"] is not None:
            print(f"   bucket agreement   : {result['bucket_agreement']:.0%}")
        print(f"   flags precision/recall: {result['flag_precision']:.2f} / {result['flag_recall']:.2f}")
        if result["confidence_floor_compliance"] is not None:
            print(f"   confidence-floor compliance: {result['confidence_floor_compliance']:.0%}")
        if args.show_misses and result["misses"]:
            print(f"   misses ({len(result['misses'])}):")
            for miss in result["misses"]:
                print(f"     - {miss}")
        if result["priority_agreement"] < 0.8:
            failed = True
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="email_agent", description="Agentic email automation (POC)")
    parser.add_argument("--db", help="SQLite path (default: storage/agent.db)")
    parser.add_argument("--llm", choices=["none", "openai"], help="LLM provider for refinement (default: none)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="process a batch of emails")
    run_p.add_argument("--input", required=True, help="directory with .eml/.txt files (or a batch dir with emails/)")
    run_p.add_argument("--now", help="ISO timestamp to use as 'now' (deterministic runs/tests)")
    run_p.add_argument("--force", action="store_true", help="re-process emails already stored")
    run_p.set_defaults(func=cmd_run)

    view_p = sub.add_parser("view", help="list processed emails")
    view_p.add_argument("--priority", choices=["Low", "Medium", "High", "Critical"])
    view_p.add_argument("--limit", type=int)
    view_p.add_argument("--show-id", action="store_true", help="print message ids")
    view_p.set_defaults(func=cmd_view)

    sum_p = sub.add_parser("summary", help="aggregate stats from the database")
    sum_p.set_defaults(func=cmd_summary)

    eval_p = sub.add_parser("eval", help="score against labeled batches")
    eval_p.add_argument("--batches", nargs="*", help=f"batch dirs (default: {' '.join(BATCHES)})")
    eval_p.add_argument("--now", help="ISO timestamp for scheduling checks")
    eval_p.add_argument("--show-misses", action="store_true", help="list every label disagreement")
    eval_p.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
