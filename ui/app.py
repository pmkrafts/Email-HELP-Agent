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

from email_agent.config import load_settings  # noqa: E402
from email_agent.report import REQUIREMENTS_COVERAGE, build_report  # noqa: E402

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


def apply_tier(tier: str, refine_mode: str) -> None:
    """Translate the sidebar choice into environment overrides, then load settings.

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
    run = st.sidebar.button("Run pipeline", type="primary")

    apply_tier(tier, refine_mode)
    settings = load_settings(None)
    st.sidebar.subheader("Tier status")
    primary_ok = settings.llm_provider != "none"
    judge_ok = settings.judge_provider != "none"
    primary_state = "active" if primary_ok else ("off" if tier == TIERS[0] else "key missing")
    judge_state = "active" if judge_ok else ("off" if tier != TIERS[2] else "key missing")
    st.sidebar.markdown(
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
                "Scheduled": fmt_time(row["send_at"]),
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

    if controls["run"] and controls["input_dir"]:
        with st.spinner(f"Processing {controls['label']} in {controls['tier']} mode…"):
            report = build_report(controls["input_dir"], controls["settings"], limit=controls["limit"])
        st.session_state["report"] = report
        st.session_state["report_label"] = controls["label"]

    report = st.session_state.get("report")
    if report is None:
        st.info("Pick a batch in the sidebar and press **Run pipeline**. "
                "Rules-only mode is free and offline; GPT/JEV tiers use the keys from your `.env`.")
        requirements_panel()
        return

    st.caption(f"Batch: **{st.session_state.get('report_label')}** · "
               f"mode: `{report['mode']['primary']}` primary / `{report['mode']['judge']}` judge · "
               f"generated at {fmt_time(report['generated_at'])}")
    metrics(report)
    requirements_panel()

    if report["failures"]:
        with st.expander(f"{len(report['failures'])} file(s) failed to parse"):
            st.table(report["failures"])

    st.divider()
    rows = filters(report["rows"])
    st.caption(f"{len(rows)} of {len(report['rows'])} emails shown (batch: {report['input_dir']})")
    view = st.radio("View", ["Table + inspector", "All emails (full content)", "Drafts only"],
                    horizontal=True, label_visibility="collapsed")

    if view == "Table + inspector":
        table(rows)
        if rows:
            labels = [f"{row['file']} — {row['subject'][:70]}" for row in rows]
            selected = st.selectbox("Inspect an email", labels)
            detail(rows[labels.index(selected)])
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
