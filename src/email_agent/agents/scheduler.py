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
        escalated = bool(state.review and state.review.status == "escalate")
        send_at, reason, status = decide_schedule(
            state.classification.priority, now, self.settings.rules, escalated=escalated, junk=junk
        )
        return ScheduleDecision(send_at=send_at, reason=reason, status=status)  # type: ignore[arg-type]
