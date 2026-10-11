"""View-model builder for the UI: runs the pipeline and shapes everything the screen shows.

Pure stdlib, no UI imports — so it is unit-testable and reusable from the CLI or a notebook.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .analytics import batch_summary
from .config import Settings
from .models import AgentState
from .orchestrator import Orchestrator
from .parser import parse_dir

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

TRACE_LABELS = {
    "classifier": "Classifier agent",
    "retrieval": "Context retrieval (RAG)",
    "drafter": "Drafting agent",
    "reviewer": "Review agent",
    "scheduler": "Scheduler",
    "orchestrator": "Orchestrator",
    "storage": "Storage",
}


def mode_description(settings: Settings) -> dict:
    """Which tiers are active (never includes key material)."""
    return {
        "primary": settings.llm_provider,
        "primary_model": settings.llm_model if settings.llm_provider != "none" else None,
        "judge": settings.judge_provider,
        "judge_model": settings.judge_model if settings.judge_provider != "none" else None,
        "judge_api": settings.judge_api,
        "refine_mode": settings.refine_mode,
        "primary_key_present": bool(settings.llm_api_key),
        "judge_key_present": bool(settings.judge_api_key and settings.judge_provider != "none"),
    }


def _row(state: AgentState) -> dict:
    email = state.email
    classification = state.classification
    draft = state.draft
    review = state.review
    schedule = state.schedule
    return {
        "file": Path(email.raw_path).name,
        "message_id": email.message_id,
        "subject": email.subject or "(no subject)",
        "from_name": email.from_name,
        "from_address": email.from_address,
        "date": email.date.isoformat() if email.date else None,
        "source_format": email.source_format,
        "body_text": email.body_text,
        "warnings": email.warnings,
        "status": state.status,
        "error": state.error,
        # classification
        "priority": classification.priority if classification else None,
        "priority_score": classification.priority_score if classification else None,
        "sentiment": classification.sentiment if classification else None,
        "urgency": classification.urgency if classification else None,
        "category": classification.category if classification else None,
        "confidence": classification.confidence if classification else None,
        "flags": classification.flags if classification else [],
        "reasoning": classification.reasoning if classification else None,
        "engine": classification.engine if classification else None,
        "judge_verdict": classification.judge_verdict if classification else None,
        # retrieval
        "context": [
            {"source": chunk.source, "title": chunk.title, "score": chunk.score, "content": chunk.content}
            for chunk in state.context
        ],
        # draft
        "draft_text": draft.text if draft else None,
        "draft_confidence": draft.confidence if draft else None,
        "draft_tone": draft.tone_used if draft else None,
        "draft_language": draft.language if draft else None,
        "draft_citations": draft.citations if draft else [],
        "draft_version": draft.version if draft else None,
        # review
        "review_status": review.status if review else None,
        "review_issues": review.issues if review else [],
        "review_suggestions": review.suggested_changes if review else None,
        "review_confidence": review.final_confidence if review else None,
        # schedule
        "send_at": schedule.send_at.isoformat() if schedule and schedule.send_at else None,
        "schedule_reason": schedule.reason if schedule else None,
        "schedule_status": schedule.status if schedule else None,
        # trace
        "trace": [
            {
                "agent": TRACE_LABELS.get(entry.get("agent", ""), entry.get("agent", "")),
                "event": entry.get("event"),
                "latency_ms": entry.get("latency_ms"),
                "details": {k: v for k, v in entry.items() if k not in {"agent", "event", "latency_ms"}},
            }
            for entry in state.logs
        ],
    }


def build_report(
    input_dir: str | Path,
    settings: Settings,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict:
    """Run the pipeline over `input_dir` and return rows + summary + mode info."""
    now = now or datetime.now(timezone.utc)
    parsed, failures = parse_dir(input_dir)
    if limit is not None:
        parsed = parsed[:limit]
    orchestrator = Orchestrator(settings, repo=None, logger=None)
    states = [orchestrator.process(email, now=now) for email in parsed]
    rows = [_row(state) for state in states]
    rows.sort(key=lambda row: (PRIORITY_ORDER.get(row["priority"], 9), -(row["priority_score"] or 0.0)))
    return {
        "rows": rows,
        "summary": batch_summary(states, failures),
        "failures": [{"path": path, "error": error} for path, error in failures],
        "mode": mode_description(settings),
        "input_dir": str(input_dir),
        "generated_at": now.isoformat(),
    }


REQUIREMENTS_COVERAGE = [
    ("Read a batch of incoming emails (.eml or .txt)",
     "Batch selector / uploader + 'Incoming email' tab",
     "12 golden + 20 adversarial + 100 generated samples; both formats, CRLF, latin-1, html-only"),
    ("Prioritize by sentiment + urgency",
     "'Classification' tab and the priority table",
     "Rules engine: score = urgency·0.5 + sentiment·0.3 + tier·0.2 + capped boosts, bands pinned in config/rules.json"),
    ("Draft personalized responses",
     "'Draft response' tab",
     "Grounded in the email + knowledge base (citations shown); uses the customer's name and issue"),
    ("Schedule for dispatch (no sending)",
     "'Review & schedule' tab",
     "send_at + business-hours reasoning recorded; nothing is transmitted"),
    ("Sub-agents (Classifier / Drafting / Review)",
     "'Agent trace' tab",
     "Classifier → Retrieval → Drafter → Reviewer → Scheduler, per-stage status + latency"),
    ("Any LLM or SLM (local or API)",
     "Sidebar 'Model tiers'",
     "Rules-only, GPT refinement, or JEV judge channel — all three switchable live"),
    ("Optional RAG over internal knowledge",
     "'Retrieved context (RAG)' tab",
     "IDF retrieval over data/knowledge_base with per-chunk scores and citations"),
    ("Confidence scores visible",
     "Metrics row, table columns, per-email tabs",
     "Classification, draft, review and judge confidences are all displayed"),
]
