"""Structured JSONL logging + batch analytics (docs 05-Analytics)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import AgentState


class RunLogger:
    def __init__(self, log_dir: str | Path):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.dir / f"run-{stamp}.jsonl"

    def log(self, event: str, **fields) -> None:
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def batch_summary(states: list[AgentState], failures: list[tuple[str, str]]) -> dict:
    by_priority: dict[str, int] = {}
    review_status: dict[str, int] = {}
    schedule_status: dict[str, int] = {}
    class_conf: list[float] = []
    draft_conf: list[float] = []
    for state in states:
        if state.classification:
            by_priority[state.classification.priority] = by_priority.get(state.classification.priority, 0) + 1
            class_conf.append(state.classification.confidence)
        if state.review:
            review_status[state.review.status] = review_status.get(state.review.status, 0) + 1
        if state.draft:
            draft_conf.append(state.draft.confidence)
        if state.schedule:
            schedule_status[state.schedule.status] = schedule_status.get(state.schedule.status, 0) + 1

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    judge_counts: dict[str, int] = {}
    for state in states:
        verdict = state.classification.judge_verdict if state.classification else None
        if verdict:
            judge_counts[verdict] = judge_counts.get(verdict, 0) + 1

    return {
        "processed": len(states),
        "failed": len(failures),
        "by_priority": by_priority,
        "review_status": review_status,
        "schedule_status": schedule_status,
        "avg_classification_confidence": avg(class_conf),
        "avg_draft_confidence": avg(draft_conf),
        "judge_verdicts": judge_counts,
        "failures": failures,
    }
