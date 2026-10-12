"""Streamlit UI for the agentic email pipeline.

    pip install streamlit
    streamlit run ui/app.py

Shows the whole story the assignment asks for: the incoming batch, prioritization
(sentiment/urgency/priority + confidence), retrieved knowledge (RAG), the drafted reply,
the review outcome and the scheduled dispatch time — with the agent trace and a live
model-tier switch (rules / GPT / JEV judge). No email is ever sent.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataclasses import replace  # noqa: E402

from email_agent import report as _report_module  # noqa: E402
from email_agent.config import ENGINE_NAME, load_settings  # noqa: E402
from email_agent.report import PRIORITY_ORDER, REQUIREMENTS_COVERAGE, build_report  # noqa: E402
from email_agent.storage import Repository  # noqa: E402
from email_agent.watcher import inbox_files, process_once, stored_files  # noqa: E402

if not hasattr(_report_module, "idle_note") or not hasattr(Repository, "latest_updated_at"):
    # A long-running Streamlit server keeps imported modules cached: after updating the code the
    # script re-runs against the old package. Say what to do instead of raising ImportError.
    st.error(
        "This console is holding an older `email_agent` in memory (Streamlit caches imported "
        "modules). Restart it: **Ctrl-C** in the terminal, then `streamlit run ui/app.py`."
    )
    st.stop()

idle_note = _report_module.idle_note

BATCHES = {
    "Golden set — 12 hand-written emails": ROOT / "data" / "sample_emails",
    "Adversarial set — 20 edge cases": ROOT / "data" / "generated_batch",
    "Generated batch — 100 emails": ROOT / "data" / "batch_100",
}
TIERS = ["Rules only (offline, free)", "GPT refinement", "GPT + JEV judge"]
PRIORITY_COLOURS = {"Critical": "#d64545", "High": "#e08c2a", "Medium": "#b8a51f", "Low": "#3f8f4f"}
REVIEW_COLOURS = {"approved": "#3f8f4f", "needs_revision": "#e08c2a", "escalate": "#d64545"}


def badge(text: str, colour: str) -> str:
    return (
        f"<span style='background:{colour}20;border:1px solid {colour};color:{colour};"
        f"border-radius:6px;padding:1px 8px;font-size:0.85rem;font-weight:600'>{text}</span>"
    )


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M %Z")
    except ValueError:
        return iso


def apply_env(tier: str, refine_mode: str) -> None:
    """Translate the sidebar choices into environment overrides, then load settings.

    Values are set explicitly (not just popped) because real environment variables win over
    `.env` — otherwise a `.env` judge key would silently re-enable the judge while the UI says
    "Rules only".
    """
    os.environ["EMAIL_AGENT_LLM"] = "none" if tier == TIERS[0] else "openai"
    os.environ["EMAIL_AGENT_JUDGE_LLM"] = "jev" if tier == TIERS[2] else "none"
    os.environ["EMAIL_AGENT_REFINE_MODE"] = refine_mode


def sidebar() -> dict:
    st.sidebar.title("Email-HELP-Agent")
    st.sidebar.caption("Agentic AI for email automation — batch in, prioritized drafts out. "
                       "Schedules are recorded only; nothing is sent.")

    choice = st.sidebar.selectbox("Incoming batch", list(BATCHES) + ["Upload .eml / .txt files"])
    upload_dir: Path | None = None
    if choice == "Upload .eml / .txt files":
        files = st.sidebar.file_uploader("Drop email files", type=["eml", "txt"], accept_multiple_files=True)
        if files:
            (ROOT / "storage").mkdir(exist_ok=True)
            upload_dir = Path(tempfile.mkdtemp(prefix="upload_", dir=ROOT / "storage"))
            for uploaded in files:
                (upload_dir / uploaded.name).write_bytes(uploaded.getbuffer())
            st.sidebar.caption(f"{len(files)} file(s) staged in {upload_dir.name}/")

    st.sidebar.divider()
    st.sidebar.subheader("Model tiers")
    tier = st.sidebar.radio("Active tiers", TIERS, index=0, help=(
        "Rules only: zero tokens. GPT refinement: premium model classifies/drafts/reviews. "
        "GPT + JEV judge: Jev verifies every classification and draft; GPT adjudicates disagreements."
    ))
    refine_mode = "always"
    if tier != "Rules only (offline, free)":
        refine_mode = st.sidebar.selectbox(
            "Strong-model spending", ["always", "escalations_only"],
            help="escalations_only reserves GPT classification calls for judge disagreements",
        )
    st.sidebar.divider()
    whole_batch = st.sidebar.checkbox("Process the entire batch", value=True,
                                      help="Uncheck to cap the run; LLM tiers bill per email")
    limit = None
    if not whole_batch:
        limit = st.sidebar.slider("Max emails in this run", 1, 120, 10)
    if tier != TIERS[0]:
        st.sidebar.caption("LLM tiers bill per email: rules ~0 calls, GPT ~3, JEV ~1-2 cheap calls.")
    st.sidebar.divider()
    live = st.sidebar.checkbox(
        "Live mode (dashboard)", value=False,
        help="A live dashboard: watch a folder for new emails and open any row for full detail.",
    )
    live_interval = 2
    live_db = str(ROOT / "storage" / "agent.db")
    live_inbox = str(ROOT / "data" / "inbox")
    live_watch = True
    live_force = False
    process_now = False
    reprocess = False
    if live:
        live_inbox = st.sidebar.text_input("Inbox folder", value=live_inbox,
                                          help="New .eml/.txt files dropped here are picked up automatically")
        live_watch = st.sidebar.checkbox(
            "Process this folder here", value=True,
            help="Each refresh picks up new files (up to 2 per tick) — no separate watcher process needed",
        )
        live_force = st.sidebar.checkbox("Reprocess emails already stored", value=False,
                                         help="Ignore the already-processed check for files you re-drop")
        live_db = st.sidebar.text_input("Database", value=live_db,
                                        help="Where results are written and read (an external watcher can use the same one)")
        live_interval = st.sidebar.selectbox("Refresh every", [1, 2, 5], index=1,
                                             format_func=lambda seconds: f"{seconds}s")
        process_now = st.sidebar.button("Process the inbox now")
        stored_count = _stored_file_count(live_db)
        reprocess = st.sidebar.button(f"Reprocess stored emails ({stored_count})", disabled=stored_count == 0,
                                      help="Re-run the emails already in this database from their files")

    st.sidebar.divider()
    run = st.sidebar.button("Run pipeline", type="primary", disabled=live,
                            help="Live mode is on — untick it to run a batch" if live else None)

    apply_env(tier, refine_mode)
    settings = load_settings(None)
    st.sidebar.subheader("Tier status")
    primary_ok = settings.llm_provider != "none"
    judge_ok = settings.judge_provider != "none"
    primary_state = "active" if primary_ok else ("off" if tier == TIERS[0] else "key missing")
    judge_state = "active" if judge_ok else ("off" if tier != TIERS[2] else "key missing")
    st.sidebar.markdown(
        f"- engine: **{ENGINE_NAME}**\n"
        f"- primary: **{settings.llm_provider}**"
        f"{f' · `{settings.llm_model}`' if primary_ok else ''} — {primary_state}\n"
        f"- judge: **{settings.judge_provider}**"
        f"{f' · `{settings.judge_model}`' if judge_ok else ''} — {judge_state}\n"
        f"- refine mode: `{settings.refine_mode}`"
    )
    if tier != TIERS[0] and not primary_ok:
        st.sidebar.warning("No API key found for the GPT tier — set OPENAI_API_KEY in .env. Running offline rules mode.")
    if tier == TIERS[2] and not judge_ok:
        st.sidebar.warning("Jev judge not configured — set EMAIL_AGENT_JUDGE_LLM=jev and OPENCODE_API_KEY in .env.")

    return {
        "input_dir": upload_dir or BATCHES.get(choice),
        "label": choice if upload_dir is None else f"uploaded files ({upload_dir.name})",
        "limit": limit,
        "run": run,
        "tier": tier,
        "live": live,
        "live_interval": live_interval,
        "live_db": live_db,
        "live_inbox": live_inbox,
        "live_watch": live_watch,
        "live_force": live_force,
        "process_now": process_now,
        "reprocess": reprocess,
        "settings": settings,
    }


def metrics(report: dict) -> None:
    summary = report["summary"]
    rows = report["rows"]
    by_priority = summary.get("by_priority", {})
    critical_high = by_priority.get("Critical", 0) + by_priority.get("High", 0)
    judge = summary.get("judge_verdicts") or {}
    # two rows of three: six metrics in one row truncate their labels at laptop widths
    top = st.columns(3)
    top[0].metric("Emails processed", summary.get("processed", 0), f"{summary.get('failed', 0)} failed")
    top[1].metric("Critical + High", critical_high,
                  f"{by_priority.get('Medium', 0)} medium / {by_priority.get('Low', 0)} low")
    class_conf = summary.get("avg_classification_confidence")
    top[2].metric("Avg classification confidence", f"{class_conf:.2f}" if class_conf else "—")

    draft_conf = summary.get("avg_draft_confidence")
    scheduled = (summary.get("schedule_status") or {}).get("scheduled", 0)
    bottom = st.columns(3)
    bottom[0].metric("Avg draft confidence", f"{draft_conf:.2f}" if draft_conf else "—")
    bottom[1].metric("Scheduled", scheduled,
                     f"junk {(summary.get('schedule_status') or {}).get('junk', 0)} / "
                     f"escalated {(summary.get('schedule_status') or {}).get('escalated', 0)}")
    if judge:
        agreed = judge.get("agreed", 0)
        escalated = sum(v for k, v in judge.items() if k != "agreed")
        bottom[2].metric("Judge verdicts", f"{agreed} agreed", f"{escalated} escalated")
    else:
        bottom[2].metric("Judge verdicts", "—", "judge tier off")

    if not rows:
        return
    human = [row for row in rows if row["review_status"] in {"needs_revision", "escalate"}]
    if human:
        st.info(f"{len(human)} email(s) need a human: " + ", ".join(f"`{row['file']}`" for row in human[:6]))


def requirements_panel() -> None:
    with st.expander("How this UI answers each assignment requirement", expanded=False):
        st.table([
            {"Requirement": req, "Where to look": where, "Implementation": how}
            for req, where, how in REQUIREMENTS_COVERAGE
        ])


def graph_panel() -> None:
    """The compiled pipeline topology, as Mermaid source."""
    try:
        from email_agent.graph import GraphOrchestrator

        diagram = GraphOrchestrator(load_settings(None)).diagram()
    except (ImportError, RuntimeError) as exc:
        st.warning(f"LangGraph diagram unavailable: {exc}")
        return
    with st.expander("LangGraph topology (Mermaid source of the compiled graph)", expanded=False):
        st.code(diagram, language="mermaid")


LIVE_STAGES = ["processing", "classified", "drafted", "reviewed", "scheduled", "error"]
LIVE_STAGE_COLOURS = {
    "processing": "#6b7280", "classified": "#3b6fb5", "drafted": "#7a5fb5",
    "reviewed": "#3f8f4f", "scheduled": "#3f8f4f", "error": "#d64545", "legacy": "#6b7280",
}


def _stored_file_count(db_path: str) -> int:
    """How many stored emails still have their source file on disk (what a reprocess would re-run)."""
    from email_agent.watcher import stored_files

    try:
        repo = Repository(db_path)
    except Exception:  # noqa: BLE001 - a bad path just disables the button
        return 0
    try:
        return len(stored_files(repo))
    except Exception:  # noqa: BLE001
        return 0
    finally:
        repo.close()


def live_board(counts: dict[str, int], summary: dict, stalled: list[dict]) -> None:
    """Status board: where every email currently sits, plus the confidence averages."""
    stages = [*LIVE_STAGES, "legacy"] if counts.get("legacy") else list(LIVE_STAGES)
    columns = st.columns(len(stages) + 1)
    for column, stage in zip(columns, stages):
        column.metric(stage.capitalize(), counts.get(stage, 0))
    columns[-1].metric("Emails seen", sum(counts.values()))
    if stalled:
        st.warning(
            f"{len(stalled)} email(s) look stalled in `processing` (watcher stopped mid-email?): "
            + ", ".join(row.get("subject") or "?" for row in stalled[:3])
        )
    average_classification = summary.get("avg_classification_confidence") or 0.0
    average_draft = summary.get("avg_draft_confidence") or 0.0
    st.caption(f"Average confidence — classification {average_classification:.2f} · draft {average_draft:.2f}")


def live_row(row: dict, is_new: bool) -> None:
    """One email in the feed: what it is, how far it got, and its confidence scores."""
    status = row.get("status") or "legacy"
    priority = row.get("priority")
    chips = [
        badge(priority, PRIORITY_COLOURS.get(priority or "", "#6b7280")) if priority else "",
        badge(status, LIVE_STAGE_COLOURS.get(status, "#6b7280")),
        badge("NEW", "#3f8f4f") if is_new else "",
    ]
    st.markdown(
        f"{' '.join(chip for chip in chips if chip)} **{row.get('subject') or '(no subject)'}**  \n"
        f"<span style='color:#8a8a8a'>{row.get('from_address') or ''} · "
        f"updated {fmt_time(row.get('updated_at'))}</span>",
        unsafe_allow_html=True,
    )
    scores = []
    if row.get("classification_confidence") is not None:
        scores.append(f"classification {row['classification_confidence']:.2f}")
    if row.get("draft_confidence") is not None:
        scores.append(f"draft {row['draft_confidence']:.2f}")
    if row.get("final_confidence") is not None:
        scores.append(f"final {row['final_confidence']:.2f}")
    if scores:
        st.caption("Confidence — " + " · ".join(scores))
    if row.get("error"):
        st.error(row["error"])
    if row.get("draft_text"):
        preview = " ".join(row["draft_text"].split())
        st.caption("Draft — " + (preview[:240] + "…" if len(preview) > 240 else preview))
    if row.get("review_status") or row.get("schedule_status"):
        send_at = fmt_time(row.get("send_at")) if row.get("send_at") else "—"
        st.caption(f"Review `{row.get('review_status') or '—'}` · "
                   f"dispatch `{row.get('schedule_status') or '—'}` · send {send_at}")


def _watch_tick(settings, inbox_dir: str, force: bool, cap: int = 2) -> None:
    """Pick up new files from the folder on each refresh — the in-console watcher.

    Cheap when idle: the folder's file count + newest mtime are compared with the previous tick, so
    nothing is parsed unless something changed.
    """
    try:
        files = inbox_files(inbox_dir)
    except Exception as exc:  # noqa: BLE001 - a bad folder must not kill the dashboard
        st.warning(f"Cannot read `{inbox_dir}`: {exc}")
        return
    stamp = (len(files), max((path.stat().st_mtime for path in files), default=0.0))
    if st.session_state.get("live_folder_stamp") == stamp:
        return
    st.session_state["live_folder_stamp"] = stamp
    if not files:
        return
    try:
        result = process_once(settings, inbox_dir, limit=cap, force=force)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the dashboard
        st.warning(f"Processing failed: {type(exc).__name__}: {exc}")
        return
    st.session_state["live_tick"] = (
        f"processed {len(result.processed)} · skipped {len(result.skipped)} · errors {len(result.errors)}"
    )


def live_detail_panel(detail: dict) -> None:
    """Full detail for one live row: classification, draft, review, schedule and the trace."""
    email = detail["email"]
    classification = detail["classification"] or {}
    review = detail["review"] or {}
    schedule = detail["schedule"] or {}
    draft = detail["draft"] or {}
    st.markdown(f"### {email['subject'] or '(no subject)'}")
    st.caption(f"`{email['status'] or 'legacy'}` · {email['from_name'] or ''} "
               f"<{email['from_address'] or ''}> · updated {fmt_time(email.get('updated_at'))}")
    if email.get("error"):
        st.error(email["error"])

    left, right = st.columns(2)
    with left:
        st.markdown("**Classification**")
        st.markdown(
            f"- priority `{classification.get('priority') or '—'}` · score {classification.get('priority_score') or '—'}\n"
            f"- sentiment `{classification.get('sentiment') or '—'}` · urgency `{classification.get('urgency') or '—'}`\n"
            f"- confidence {classification.get('confidence') if classification.get('confidence') is not None else '—'}"
            f" · engine `{classification.get('engine') or '—'}`\n"
            f"- judge `{classification.get('judge_verdict') or '—'}` · category `{classification.get('category') or '—'}`"
        )
        st.caption("Signals: " + (", ".join(f"`{flag}`" for flag in detail["flags"]) or "—"))
        if classification.get("reasoning"):
            st.caption(classification["reasoning"])
    with right:
        st.markdown("**Review & schedule**")
        st.markdown(
            f"- review `{review.get('status') or '—'}` · final confidence "
            f"{review.get('final_confidence') if review.get('final_confidence') is not None else '—'}\n"
            f"- dispatch `{schedule.get('status') or '—'}` · send {fmt_time(schedule.get('send_at'))}\n"
            f"- reason: {schedule.get('reason') or '—'}"
        )
        if detail["issues"]:
            st.caption("Issues: " + "; ".join(str(issue) for issue in detail["issues"]))

    if draft.get("draft_text"):
        st.text_area("Draft reply", draft["draft_text"], height=220, disabled=True,
                     key=f"live_draft_{email['message_id']}")
        st.caption(f"Draft confidence {draft.get('confidence')} · tone `{draft.get('tone')}` · "
                   f"language `{draft.get('language')}` · version {draft.get('version')} · citations: "
                   + (", ".join(f"`{citation}`" for citation in detail["citations"]) or "—"))
    if detail["logs"]:
        st.markdown("**Agent trace**")
        st.dataframe(
            [
                {"stage": log["agent"], "event": log["event"], "latency_ms": log["latency_ms"],
                 "at": fmt_time(log["created_at"])}
                for log in detail["logs"]
            ],
            hide_index=True,
        )
    if email.get("body_text"):
        with st.expander("Incoming email"):
            st.text(email["body_text"][:4000])


def live_panel(db_path: str, interval: int, settings, inbox_dir: str, watch_here: bool, force: bool) -> None:
    """The live dashboard: watch the folder, then show everything the database holds."""
    if watch_here:
        _watch_tick(settings, inbox_dir, force=force)

    try:
        repo = Repository(db_path)
    except Exception as exc:  # noqa: BLE001 - a bad path must not crash the console
        st.error(f"Cannot open `{db_path}`: {exc}")
        return
    try:
        rows = repo.live_rows(limit=50)
        counts = repo.status_counts()
        summary = repo.summary()
        stalled = repo.stalled_rows()
        heartbeat = repo.latest_updated_at()
    finally:
        repo.close()

    tick = st.session_state.get("live_tick")
    legacy = sum(1 for row in rows if not row.get("status"))
    st.caption(f"Live · watching `{db_path}` · refreshing every {interval}s · "
               f"{sum(counts.values())} email(s) seen · {idle_note(heartbeat)}"
               + (f" · last pass: {tick}" if tick else ""))
    if legacy:
        st.caption(f"{legacy} row(s) here predate the live view (no stage recorded) — "
                   "use **Reprocess stored emails** in the sidebar to give them stages.")
    if not rows:
        st.info(
            "Nothing yet. Drop an `.eml`/`.txt` file into the inbox folder and it will be picked up "
            "here automatically:\n\n"
            f"```\n{inbox_dir}\n```\n\n"
            "The sidebar can also point at a database an external watcher writes to "
            "(`python email-agent.py --db … watch --input …`)."
        )
        return

    live_board(counts, summary, stalled)
    st.divider()
    last_seen = st.session_state.get("live_last_seen")
    for row in rows:
        with st.container(border=True):
            live_row(row, is_new=bool(last_seen and row.get("updated_at") and row["updated_at"] > last_seen))
    newest = rows[0].get("updated_at")
    if newest:
        st.session_state["live_last_seen"] = newest

    options: dict[str, str] = {}
    for row in rows:
        label = f"{row.get('subject') or '(no subject)'} — {row.get('status') or 'legacy'}"
        while label in options:  # keep the selectbox keys unique
            label += " "
        options[label] = row["message_id"]
    picked = st.selectbox("Open an email for the full detail", ["—", *options], index=0)
    if picked != "—":
        try:
            repo = Repository(db_path)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Cannot open `{db_path}`: {exc}")
            return
        try:
            detail = repo.live_detail(options[picked])
        finally:
            repo.close()
        if detail:
            st.divider()
            live_detail_panel(detail)


def live_section(db_path: str, interval: int, settings, inbox_dir: str, watch_here: bool, force: bool) -> None:
    """Run the live dashboard as an auto-refreshing fragment — the rest of the page stays put.

    Streamlit older than 1.37 has no `st.fragment(run_every=…)`; those installs get a manual
    refresh button instead of an error.
    """
    def render() -> None:
        live_panel(db_path, interval, settings, inbox_dir, watch_here, force)

    fragment = getattr(st, "fragment", None)
    if fragment is not None:
        try:
            fragment(run_every=f"{interval}s")(render)()
            return
        except TypeError:  # a fragment implementation without run_every
            pass
    live_panel(db_path, interval)
    st.caption("Auto-refresh needs Streamlit 1.37+ (`pip install -U streamlit`) — "
               "use the button to update manually.")
    if st.button("Refresh now"):
        st.rerun()


def schedule_view(rows: list[dict]) -> None:
    """The dispatch plan, grouped by bucket and ordered by send time."""
    summary = {}
    for row in rows:
        summary[row["schedule_status"]] = summary.get(row["schedule_status"], 0) + 1
    next_send = min((row["send_at"] for row in rows if row["send_at"]), default=None)
    st.caption(
        "Recorded dispatch plan — nothing is transmitted. "
        f"{summary.get('scheduled', 0)} scheduled"
        + (f" (next: {fmt_time(next_send)})" if next_send else "")
        + f" · {summary.get('pending_human', 0)} pending human"
        + f" · {summary.get('escalated', 0)} escalated"
        + f" · {summary.get('junk', 0)} junk. "
        "Buckets follow the scheduler rules: critical within 1 h, high within 4 h or same business "
        "day, medium within 24 h, low next business day; business hours Mon–Fri 09:00–18:00."
    )
    buckets = [
        ("scheduled", "Scheduled for dispatch"),
        ("pending_human", "Pending human review (no dispatch time yet)"),
        ("escalated", "Escalated to a human (no automatic reply)"),
        ("junk", "Junk (no reply drafted)"),
    ]
    for status, title in buckets:
        group = [row for row in rows if row["schedule_status"] == status]
        if not group:
            continue
        if status == "scheduled":
            group = sorted(group, key=lambda row: row["send_at"] or "")
        else:
            group = sorted(group, key=lambda row: (PRIORITY_ORDER.get(row["priority"], 9)))
        st.subheader(f"{title} — {len(group)}")
        st.dataframe(
            [
                {
                    "Send at": fmt_time(row["send_at"]) if row["send_at"] else "—",
                    "Priority": row["priority"],
                    "Review": row["review_status"],
                    "Subject": row["subject"],
                    "From": row["from_address"],
                    "Why": row["schedule_reason"],
                }
                for row in group
            ],
            hide_index=True,
        )


def filters(rows: list[dict]) -> list[dict]:
    columns = st.columns([1, 1, 1, 2])
    priorities = columns[0].multiselect("Priority", ["Critical", "High", "Medium", "Low"],
                                        default=["Critical", "High", "Medium", "Low"])
    sentiments = columns[1].multiselect("Sentiment", ["angry", "negative", "neutral", "positive"],
                                        default=["angry", "negative", "neutral", "positive"])
    reviews = columns[2].multiselect("Review", ["approved", "needs_revision", "escalate"],
                                     default=["approved", "needs_revision", "escalate"])
    search = columns[3].text_input("Search subject / sender / body", "")
    filtered = [
        row for row in rows
        if row["priority"] in priorities and row["sentiment"] in sentiments
        and row["review_status"] in reviews
        and (not search or search.lower() in f"{row['subject']} {row['from_address']} {row['body_text']}".lower())
    ]
    return filtered


def table(rows: list[dict]) -> None:
    st.dataframe(
        [
            {
                "Priority": row["priority"],
                "Subject": row["subject"],
                "From": row["from_address"],
                "Sentiment": row["sentiment"],
                "Urgency": row["urgency"],
                "Class conf": row["confidence"],
                "Draft conf": row["draft_confidence"],
                "Review": row["review_status"],
                "Dispatch": fmt_time(row["send_at"]) if row["send_at"] else row["schedule_status"],
                "Engine": row["engine"],
                "Judge": row["judge_verdict"] or "—",
                "Draft preview": " ".join((row["draft_text"] or "(no draft)").split())[:160],
            }
            for row in rows
        ],
        hide_index=True,
    )


def detail(row: dict) -> None:
    st.subheader(f"{row['subject']}")
    priority = row["priority"]
    review = row["review_status"]
    chips = (
        f"{badge(priority, PRIORITY_COLOURS.get(priority, '#888888'))} "
        f"{badge(row['sentiment'], '#4a6fa5')} {badge(row['urgency'], '#6b4fa5')} "
        f"{badge(f'review: {review}', REVIEW_COLOURS.get(review, '#888888'))}"
    )
    if row["judge_verdict"]:
        chips += f" {badge('judge: ' + row['judge_verdict'], '#3f8f4f')}"
    st.markdown(chips, unsafe_allow_html=True)
    tabs = st.tabs(["Incoming email", "Classification", "Retrieved context (RAG)", "Draft response",
                    "Review & schedule", "Agent trace"])

    with tabs[0]:
        st.markdown(f"**From:** {row['from_name'] or ''} <{row['from_address']}>  \n"
                    f"**Date:** {row['date'] or '—'}  ·  **Format:** `{row['source_format']}`  ·  "
                    f"**File:** `{row['file']}`  ·  **Message-ID:** `{row['message_id']}`")
        if row["warnings"]:
            st.caption("Parser notes: " + ", ".join(f"`{warning}`" for warning in row["warnings"]))
        st.text_area("Original email body", row["body_text"] or "(empty body)", height=220, disabled=True)

    with tabs[1]:
        cols = st.columns(5)
        cols[0].metric("Priority", row["priority"] or "—")
        cols[1].metric("Priority score", f"{row['priority_score']:.2f}" if row["priority_score"] else "—")
        cols[2].metric("Sentiment", row["sentiment"] or "—")
        cols[3].metric("Urgency", row["urgency"] or "—")
        cols[4].metric("Confidence", f"{row['confidence']:.2f}" if row["confidence"] else "—")
        st.markdown(f"**Category:** `{row['category']}` · **engine:** `{row['engine']}`"
                    f"{' · **judge:** `' + row['judge_verdict'] + '`' if row['judge_verdict'] else ''}")
        if row["flags"]:
            st.markdown("**Signals detected:** " + " ".join(f"`{flag}`" for flag in row["flags"]))
        st.markdown("**Scoring reasoning**")
        st.code(row["reasoning"] or "—", language=None)
        if row["judge_verdict"] == "error":
            st.warning("The judge channel could not be reached for this email — the rules baseline "
                       "was kept. Check the endpoint/key with `python email-agent.py check`.")

    with tabs[2]:
        if not row["context"]:
            st.caption("No knowledge chunks matched this email.")
        for chunk in row["context"]:
            with st.expander(f"{chunk['title']} — {chunk['source']} (score {chunk['score']})", expanded=False):
                st.markdown(chunk["content"])

    with tabs[3]:
        if not row["draft_text"]:
            st.caption("No draft for this email (spam is routed to the junk queue without a reply).")
        else:
            cols = st.columns(4)
            cols[0].metric("Draft confidence", f"{row['draft_confidence']:.2f}")
            cols[1].metric("Tone", row["draft_tone"] or "—")
            cols[2].metric("Language", row["draft_language"] or "en")
            cols[3].metric("Version", row["draft_version"] or 1)
            st.text_area("Draft reply", row["draft_text"], height=280, disabled=True)
            if row["draft_citations"]:
                st.caption("Grounded in: " + ", ".join(f"`{cite}`" for cite in row["draft_citations"]))

    with tabs[4]:
        cols = st.columns(3)
        cols[0].metric("Review status", row["review_status"] or "—")
        cols[1].metric("Final confidence", f"{row['review_confidence']:.2f}" if row["review_confidence"] else "—")
        cols[2].metric("Dispatch", row["schedule_status"] or "—")
        if row["review_issues"]:
            st.markdown("**Issues raised by the review agent**")
            for issue in row["review_issues"]:
                st.markdown(f"- {issue}")
        if row["review_suggestions"]:
            st.caption(f"Suggested changes: {row['review_suggestions']}")
        st.markdown(f"**Scheduled for:** {fmt_time(row['send_at'])}")
        st.caption(row["schedule_reason"] or "—")
        st.warning("Scheduling is recorded for review only — this POC never transmits email.", icon=None)

    with tabs[5]:
        st.dataframe(row["trace"], hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Email-HELP-Agent", layout="wide")
    controls = sidebar()

    st.title("Incoming email batch → prioritized drafts")
    st.caption("Classifier → retrieval → drafter → reviewer → scheduler, with confidence scores "
               "at every step. Dispatch times are recorded; nothing is sent.")

    if controls["live"]:
        st.caption("Live dashboard — new emails in the inbox folder are picked up automatically, and any "
                   "row below opens into its full detail. **Run pipeline** is disabled in this mode.")
        live_settings = replace(controls["settings"], db_path=Path(controls["live_db"]))
        if controls["process_now"]:
            with st.spinner("Processing the inbox…"):
                result = process_once(live_settings, controls["live_inbox"], limit=10,
                                      force=controls["live_force"])
            st.success(f"Processed {len(result.processed)} · skipped {len(result.skipped)} · "
                       f"errors {len(result.errors)}")
        if controls["reprocess"]:
            with st.spinner("Reprocessing the stored emails…"):
                repo = Repository(controls["live_db"])
                try:
                    files = stored_files(repo)
                finally:
                    repo.close()
                result = process_once(live_settings, files=files, limit=20, force=True)
            st.success(f"Reprocessed {len(result.processed)} of {len(files)} stored file(s).")
        live_section(controls["live_db"], controls["live_interval"], live_settings,
                     controls["live_inbox"], controls["live_watch"], controls["live_force"])
        graph_panel()
        return

    if controls["run"] and controls["input_dir"]:
        with st.spinner(f"Processing {controls['label']} in {controls['tier']} mode…"):
            report = None
            progress = st.progress(0.0, text="Starting…")
            preview = st.empty()
            streamed: list[dict] = []

            def on_email(row: dict, index: int, total: int) -> None:
                streamed.append(row)
                progress.progress(min(index / max(total, 1), 1.0),
                                  text=f"{index}/{total} · {row['subject'][:60]}")
                preview.dataframe(
                    [
                        {
                            "Priority": item["priority"],
                            "Subject": item["subject"][:60],
                            "Confidence": item["confidence"],
                            "Review": item["review_status"],
                            "Dispatch": item["schedule_status"],
                        }
                        for item in streamed[-8:]
                    ],
                    hide_index=True,
                )

            try:
                report = build_report(controls["input_dir"], controls["settings"],
                                      limit=controls["limit"], on_email=on_email)
            except RuntimeError as exc:  # e.g. LangGraph missing from the environment
                st.error(str(exc))
            progress.empty()
            preview.empty()
        if report is not None:
            st.session_state["report"] = report
            st.session_state["report_label"] = controls["label"]

    report = st.session_state.get("report")
    if report is None:
        st.info("Pick a batch in the sidebar and press **Run pipeline**. "
                "Rules-only mode is free and offline; GPT/JEV tiers use the keys from your `.env`.")
        requirements_panel()
        graph_panel()
        return

    st.caption(f"Batch: **{st.session_state.get('report_label')}** · "
               f"engine: `{report['mode']['engine']}` · "
               f"mode: `{report['mode']['primary']}` primary / `{report['mode']['judge']}` judge · "
               f"generated at {fmt_time(report['generated_at'])}")
    metrics(report)
    requirements_panel()
    graph_panel()

    if report["failures"]:
        with st.expander(f"{len(report['failures'])} file(s) failed to parse"):
            st.table(report["failures"])

    st.divider()
    rows = filters(report["rows"])
    st.caption(f"{len(rows)} of {len(report['rows'])} emails shown (batch: {report['input_dir']})")
    view = st.radio("View", ["Table + inspector", "All emails (full content)", "Drafts only", "Dispatch schedule"],
                    horizontal=True, label_visibility="collapsed")

    if view == "Table + inspector":
        table(rows)
        if rows:
            labels = [f"{row['file']} — {row['subject'][:70]}" for row in rows]
            selected = st.selectbox("Inspect an email", labels)
            detail(rows[labels.index(selected)])
    elif view == "Dispatch schedule":
        schedule_view(rows)
    elif view == "All emails (full content)":
        st.caption("Every matching email with classification, retrieved context, draft, review and trace.")
        for row in rows:
            conf = f" — conf {row['confidence']:.2f}" if row["confidence"] else ""
            with st.expander(f"{row['priority']} · {row['subject']}{conf}", expanded=len(rows) <= 3):
                detail(row)
    else:
        drafted = [row for row in rows if row["draft_text"]]
        st.caption(f"{len(drafted)} drafted repl{'y' if len(drafted) == 1 else 'ies'} "
                   f"(spam/junk emails have no draft by design)")
        for row in drafted:
            with st.expander(f"{row['priority']} · {row['subject']} — draft conf {row['draft_confidence']:.2f}",
                             expanded=False):
                st.markdown(f"**To:** {row['from_name'] or ''} <{row['from_address']}> · "
                            f"**review:** `{row['review_status']}` · **send:** {fmt_time(row['send_at'])}")
                st.text_area("Draft reply", row["draft_text"], height=260, disabled=True,
                             key=f"draft_{row['file']}")
                st.caption("Citations: " + (", ".join(f"`{c}`" for c in row["draft_citations"]) or "—")
                           + f" · tone `{row['draft_tone']}` · language `{row['draft_language']}`")
                if row["review_issues"]:
                    st.caption("Review issues: " + "; ".join(row["review_issues"]))

    st.download_button("Download results (JSON)", json.dumps(report["rows"], indent=2, ensure_ascii=False),
                       file_name="email_agent_results.json", mime="application/json")
    st.divider()
    st.caption("CLI equivalents: `python run.py`, `python email-agent.py eval --show-misses`, "
               "`python data/verify_batch.py data/batch_100`")


if __name__ == "__main__":
    main()
