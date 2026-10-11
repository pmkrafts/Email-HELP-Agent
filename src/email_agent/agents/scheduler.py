"""Scheduler agent: maps priority + review state to a send time (docs 06-Rules section 4)."""
from __future__ import annotations

from datetime import datetime

from ..config import Settings
from ..models import AgentState, ScheduleDecision
from ..rules import decide_schedule


class Scheduler:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, state: AgentState, now: datetime) -> ScheduleDecision:
        assert state.classification is not None
        junk = "spam" in state.classification.flags
        review_status = state.review.status if state.review else None
        if review_status == "needs_revision":
            # a draft the review agent wants changed must not get an automatic dispatch time
            return ScheduleDecision(
                send_at=None,
                reason="human review required before dispatch (draft flagged needs_revision)",
                status="pending_human",
            )
        escalated = review_status == "escalate"
        send_at, reason, status = decide_schedule(
            state.classification.priority, now, self.settings.rules, escalated=escalated, junk=junk
        )
        return ScheduleDecision(send_at=send_at, reason=reason, status=status)  # type: ignore[arg-type]
