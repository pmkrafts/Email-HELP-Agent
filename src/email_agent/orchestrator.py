"""Sequential orchestrator over a shared AgentState (docs 03-Architecture).

Flow: parse -> classify -> (spam? junk, no reply) -> retrieve -> draft -> review
(+max-2 rewrite loop) -> schedule -> persist.
Errors are contained per email (dead-letter list), never aborting the batch.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from .agents.classifier import Classifier
from .agents.drafter import Drafter
from .agents.reviewer import Reviewer
from .agents.scheduler import Scheduler
from .analytics import RunLogger
from .config import Settings
from .llm import LLMClient, get_client
from .models import AgentState, ParsedEmail
from .parser import parse_dir
from .retrieval import KnowledgeBase
from .storage import Repository


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class Orchestrator:
    def __init__(self, settings: Settings, repo: Repository | None = None, logger: RunLogger | None = None):
        self.settings = settings
        self.repo = repo
        self.logger = logger
        self.kb = KnowledgeBase.load(settings.root / "data" / "knowledge_base")
        client: LLMClient | None = get_client(
            settings.llm_provider, settings.llm_api_key, settings.llm_model, settings.llm_base_url
        )
        self.classifier = Classifier(settings, client)
        self.drafter = Drafter(settings, self.kb, client)
        self.reviewer = Reviewer(settings, client)
        self.scheduler = Scheduler(settings)

    def process(self, email: ParsedEmail, now: datetime | None = None) -> AgentState:
        now = now or datetime.now(timezone.utc)
        state = AgentState(email=email)
        try:
            start = time.perf_counter()
            state.classification = self.classifier.run(email)
            state.status = "classified"
            state.log("classifier", "completed", latency_ms=_ms(start),
                      priority=state.classification.priority, confidence=state.classification.confidence,
                      engine=state.classification.engine, flags=state.classification.flags)

            if "spam" in state.classification.flags:
                state.schedule = self.scheduler.run(state, now)
                state.status = "scheduled"
                state.log("orchestrator", "junk_routed", note="spam: no customer reply drafted")
            else:
                start = time.perf_counter()
                state.context = self.kb.retrieve(f"{email.subject}\n{email.body_text}", top_k=3)
                state.log("retrieval", "completed", latency_ms=_ms(start),
                          chunks=[chunk.title for chunk in state.context])

                start = time.perf_counter()
                state.draft = self.drafter.run(state)
                state.status = "drafted"
                state.log("drafter", "completed", latency_ms=_ms(start),
                          confidence=state.draft.confidence, language=state.draft.language)

                start = time.perf_counter()
                state.review = self.reviewer.run(state)
                attempts = 1
                while state.review.status == "needs_revision" and attempts < self.settings.max_rewrites:
                    feedback = state.review.suggested_changes or "; ".join(state.review.issues)
                    state.log("reviewer", "revision_requested", attempt=attempts, issues=state.review.issues)
                    state.draft = self.drafter.run(state, feedback=feedback)
                    state.review = self.reviewer.run(state)
                    attempts += 1
                if state.review.status == "needs_revision":
                    state.review.status = "escalate"
                    state.review.issues.append("max rewrite attempts reached; escalated to human review")
                state.status = "reviewed"
                state.log("reviewer", "completed", latency_ms=_ms(start),
                          status=state.review.status, attempts=attempts, issues=state.review.issues)

                state.schedule = self.scheduler.run(state, now)
                state.status = "scheduled"
                state.log("scheduler", "completed", priority=state.classification.priority,
                          status=state.schedule.status,
                          send_at=state.schedule.send_at.isoformat() if state.schedule.send_at else None)
        except Exception as exc:  # noqa: BLE001 - per-email isolation is the contract
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"
            state.log("orchestrator", "error", error=state.error)

        if self.repo is not None and state.status != "error":
            try:
                self.repo.save_state(state)
            except Exception as exc:  # noqa: BLE001
                state.log("storage", "error", error=f"{type(exc).__name__}: {exc}")
        if self.logger is not None:
            self.logger.log(
                "email_processed",
                message_id=state.email.message_id,
                subject=state.email.subject,
                status=state.status,
                error=state.error,
                priority=state.classification.priority if state.classification else None,
                confidence=state.classification.confidence if state.classification else None,
                review_status=state.review.status if state.review else None,
                schedule_status=state.schedule.status if state.schedule else None,
                send_at=state.schedule.send_at.isoformat() if state.schedule and state.schedule.send_at else None,
            )
        return state

    def run_batch(
        self, input_dir: str | Path, now: datetime | None = None, force: bool = False
    ) -> tuple[list[AgentState], list[tuple[str, str]], list[str]]:
        parsed, failures = parse_dir(input_dir)
        processed = self.repo.processed_message_ids() if self.repo else set()
        skipped: list[str] = []
        states: list[AgentState] = []
        for email in parsed:
            if not force and email.message_id in processed:
                skipped.append(email.message_id)
                continue
            states.append(self.process(email, now))
        return states, failures, skipped
