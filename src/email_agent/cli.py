"""Command-line interface (stdlib argparse): run | view | summary | eval."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .analytics import RunLogger, batch_summary
from .config import ENGINE_NAME, load_settings, project_root
from .evaluation import evaluate
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


def _engine():
    """Import the engine lazily: a missing LangGraph should print a hint, not a traceback."""
    try:
        from .graph import GraphOrchestrator
    except ImportError as exc:  # noqa: TRY003
        raise RuntimeError(
            "needs LangGraph: pip install langgraph  (or: pip install -r requirements.txt)"
        ) from exc
    return GraphOrchestrator


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    now = _parse_now(args.now)
    repo = Repository(settings.db_path)
    logger = RunLogger(settings.root / "storage" / "logs")
    try:
        orchestrator = _engine()(settings, repo, logger)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    states, failures, skipped = orchestrator.run_batch(args.input, now=now, force=args.force)

    print(f"Processing: {args.input}  (engine: {ENGINE_NAME}, mode: {settings.llm_provider})")
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
    print(f"{_fmt('Priority', 9)}{_fmt('Sent', 9)}{_fmt('Conf', 6)}{_fmt('Review', 16)}{_fmt('Dispatch', 14)}{_fmt('Judge', 14)}Subject")
    for row in rows:
        conf = row.get("confidence")
        conf_text = f"{conf:.2f}" if conf is not None else None
        print(
            f"{_fmt(row.get('priority'), 9)}{_fmt(row.get('sentiment'), 9)}"
            f"{_fmt(conf_text, 6)}"
            f"{_fmt(row.get('review_status'), 16)}{_fmt(row.get('schedule_status'), 14)}"
            f"{_fmt(row.get('judge_verdict'), 14)}{row.get('subject') or '(no subject)'}"
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


def cmd_check(args: argparse.Namespace) -> int:
    """Connectivity check for the configured model tiers (no batch processing)."""
    from .judges import SystemOneJudge, build_judge
    from .llm import LLMError, get_client
    from .models import Classification, ParsedEmail

    settings = load_settings(args)
    print(f"primary: {settings.llm_provider} / {settings.llm_model or '-'} @ {settings.llm_base_url}")
    print(f"judge  : {settings.judge_provider} / {settings.judge_model or '-'} @ {settings.judge_base_url} "
          f"(api: {settings.judge_api}, refine: {settings.refine_mode})")
    ok = True

    primary = get_client(settings.llm_provider, settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    if primary is None:
        print("primary: not configured - set EMAIL_AGENT_LLM and EMAIL_AGENT_API_KEY")
    else:
        try:
            reply = primary.chat_json('Reply with JSON only: {"ping": "pong"}', "ping", temperature=0.0)
            print(f"primary: OK -> {reply}")
        except LLMError as exc:
            ok = False
            print(f"primary: FAILED -> {exc}")

    channel = build_judge(settings)
    if channel is None:
        print("judge  : not configured - set EMAIL_AGENT_JUDGE_LLM=jev and an OpenCode Zen key "
              "(OPENCODE_API_KEY or EMAIL_AGENT_JUDGE_API_KEY)")
    elif isinstance(channel, SystemOneJudge):
        try:
            response = channel.client.evaluate(
                "My order is late and I want a refund.",
                {"is_urgent": {"type": "noul", "instructions": "Does this message express urgency?"}},
            )
            print(f"judge  : OK -> answers={json.dumps(response.get('answers', {}))} usage={response.get('usage', {})}")
        except LLMError as exc:
            ok = False
            print(f"judge  : FAILED -> {exc}")
    else:
        probe = Classification(sentiment="neutral", urgency="medium", priority="Medium", priority_score=4.0,
                               category="other", confidence=0.5, reasoning="connectivity probe")
        email = ParsedEmail(message_id="probe", from_address="probe@example.com", from_name=None,
                            to_addresses=[], subject="Connectivity probe", body_text="My order is late.",
                            date=None, raw_path="probe", source_format="eml")
        verdict = channel.verdict(email, probe)
        if verdict is None:
            ok = False
            print("judge  : FAILED -> no usable verdict")
        else:
            print(f"judge  : OK -> {json.dumps(verdict, ensure_ascii=False)}")
    return 0 if ok else 1


def cmd_graph(args: argparse.Namespace) -> int:
    """Print (or save) the compiled pipeline topology as Mermaid source."""
    settings = load_settings(args)
    try:
        diagram = _engine()(settings).diagram()
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    if args.out:
        Path(args.out).write_text(diagram + "\n", encoding="utf-8")
        print(f"wrote {args.out} ({len(diagram)} chars)")
    else:
        print(diagram)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll a folder and process emails as they arrive — the live view reads the same database."""
    settings = load_settings(args)
    try:
        engine = _engine()
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    from .analytics import RunLogger
    from .storage import Repository
    from .watcher import inbox_files, watch

    repo = Repository(settings.db_path)
    logger = RunLogger(settings.root / "storage" / "logs")
    orchestrator = engine(settings, repo, logger)
    now = _parse_now(args.now)
    candidates = len(inbox_files(args.input))

    def report(state) -> None:
        classification = state.classification
        schedule = state.schedule
        send_at = schedule.send_at.strftime("%a %H:%M") if schedule and schedule.send_at else "-"
        stamp = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{stamp}] {_fmt(state.email.subject or '(no subject)', 44)} "
            f"{_fmt(classification.priority if classification else '?', 9)} "
            f"conf={classification.confidence if classification else 0:.2f} "
            f"-> {send_at} ({schedule.status if schedule else state.status})"
        )

    def report_sweep(result) -> None:
        if result.skipped:
            print(f"      skipped {len(result.skipped)} file(s) already stored in this database — "
                  f"use --force to process them again, or point --db at a fresh database")
        for name, error in result.errors:
            print(f"      unparsable  {name}: {error}")

    print(f"Watching {args.input} every {args.interval:g}s — db {settings.db_path} — Ctrl-C to stop")
    if candidates == 0:
        print("      (folder is empty — drop a .eml/.txt file into it)")
    try:
        states = watch(
            settings, args.input, interval=args.interval, now=now, once=args.once,
            limit=args.limit, force=args.force, repo=repo, logger=logger,
            orchestrator=orchestrator, on_email=report, on_sweep=report_sweep,
        )
    finally:
        repo.close()
    print(f"Processed {len(states)} email(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="email_agent", description="Agentic email automation (POC)")
    parser.add_argument("--db", help="SQLite path (default: storage/agent.db)")
    parser.add_argument("--llm", help="refinement provider name: openai, jev, ... or none (default: none)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="process a batch of emails")
    run_p.add_argument("--db", default=argparse.SUPPRESS, help="database path (same as the global --db)")
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

    check_p = sub.add_parser("check", help="verify the configured model tiers (primary + judge) with a live ping")
    check_p.set_defaults(func=cmd_check)

    graph_p = sub.add_parser("graph", help="print the LangGraph topology as Mermaid source")
    graph_p.add_argument("--out", help="write the diagram to this path instead of stdout")
    graph_p.set_defaults(func=cmd_graph)

    watch_p = sub.add_parser("watch", help="poll a folder and process emails as they arrive (live view)")
    watch_p.add_argument("--db", default=argparse.SUPPRESS, help="database path (same as the global --db)")
    watch_p.add_argument("--input", required=True, help="folder to poll for new .eml/.txt files")
    watch_p.add_argument("--interval", type=float, default=2.0, help="seconds between sweeps (default: 2)")
    watch_p.add_argument("--once", action="store_true", help="single sweep, then exit")
    watch_p.add_argument("--limit", type=int, help="max emails to process per sweep")
    watch_p.add_argument("--force", action="store_true", help="re-process files that are already stored")
    watch_p.add_argument("--now", help="ISO timestamp to use as 'now' (deterministic runs)")
    watch_p.set_defaults(func=cmd_watch)

    eval_p = sub.add_parser("eval", help="score against labeled batches")
    eval_p.add_argument("--batches", nargs="*", help=f"batch dirs (default: {' '.join(BATCHES)})")
    eval_p.add_argument("--now", help="ISO timestamp for scheduling checks")
    eval_p.add_argument("--show-misses", action="store_true", help="list every label disagreement")
    eval_p.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:  # Windows consoles default to cp1252; never crash on subjects with emoji/accents
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
