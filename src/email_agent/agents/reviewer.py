"""Review agent: policy validation + confidence gating (docs 06-Rules section 3).

Deterministic validator first (regex policy bans), then confidence thresholds and escalation
rules. When an LLM is configured it can add a second opinion, but the deterministic checks
always run — the review layer is never fully delegated.
"""
from __future__ import annotations

import json
import re

from ..config import Settings
from ..llm import LLMClient, LLMError, load_prompt
from ..models import AgentState, ReviewResult

POLICY_BANS: list[tuple[str, str]] = [
    (r"\b(guarantee|guaranteed|we promise|i promise)\b", "promise language without a policy basis"),
    (r"\binstant(?:ly)?\s+refund", "instant refund promise"),
    (r"\bwe will refund\b|\bi have refunded\b|\brefund (?:is|has been) approved\b", "refund commitment made without approval"),
    (r"\b(sue|lawyer|legal action|tribunal|court)\b", "legal content in an automated reply"),
    (r"\b(system prompt|api key|internal (?:policy|configuration))\b", "internal information disclosure"),
    (r"\b(your (?:own )?fault|you should have)\b", "blaming language"),
]


class Reviewer:
    def __init__(self, settings: Settings, client: LLMClient | None = None, judge: LLMClient | None = None):
        self.settings = settings
        self.client = client
        # The LLM second opinion is a verification step, so the cheap judge runs it first-class;
        # the premium model is never spent on it when a judge is configured.
        self.judge = judge or client

    def run(self, state: AgentState) -> ReviewResult:
        assert state.classification is not None and state.draft is not None
        issues: list[str] = []
        suggestions: list[str] = []
        draft_text = state.draft.text
        lowered = draft_text.lower()

        for pattern, message in POLICY_BANS:
            if re.search(pattern, lowered):
                issues.append(f"policy: {message}")

        threshold = self.settings.review_confidence_threshold
        if state.draft.confidence < threshold:
            suggestions.append(
                f"draft confidence {state.draft.confidence:.2f} is below {threshold:.2f}; consider a second pass or human review"
            )
        if state.draft.language != "en":
            suggestions.append("draft produced by the offline template in a non-English language; validate wording before sending")
        if "prompt_injection_attempt" in state.classification.flags:
            suggestions.append("email body contained injected instructions; confirm the reply ignores them and leaks nothing")

        escalate = False
        if "legal_threat" in state.classification.flags:
            escalate = True
            issues.append("legal threat: automated final replies are prohibited (rules section 5)")
        if state.classification.confidence < float(self.settings.rules["thresholds"]["min_classification_confidence"]):
            suggestions.append("classification confidence is very low; route to human triage")

        if escalate:
            status = "escalate"
        elif issues:
            status = "needs_revision"
        else:
            status = "approved"

        if self.judge is not None and status == "approved":
            llm_result = self._llm_second_opinion(state)
            if llm_result is not None:
                verdict = llm_result.get("status")
                if verdict in {"needs_revision", "escalate"}:
                    status = verdict
                    issues.extend(str(x) for x in llm_result.get("issues", [])[:3])

        final_confidence = round(min(state.draft.confidence, state.classification.confidence), 2)
        return ReviewResult(
            status=status,  # type: ignore[arg-type]
            issues=issues,
            suggested_changes="; ".join(suggestions) or None,
            final_confidence=final_confidence,
        )

    def _llm_second_opinion(self, state: AgentState) -> dict | None:
        try:
            system = load_prompt(self.settings.prompts_dir, "reviewer")
            user = json.dumps({
                "email": {
                    "subject": state.email.subject,
                    "body": state.email.body_text[:4000],
                },
                "classification": {
                    "sentiment": state.classification.sentiment,  # type: ignore[union-attr]
                    "priority": state.classification.priority,  # type: ignore[union-attr]
                    "flags": state.classification.flags,  # type: ignore[union-attr]
                },
                "draft": state.draft.text[:4000],  # type: ignore[union-attr]
                "knowledge": "\n\n".join(c.content for c in state.context)[:4000],
            }, ensure_ascii=False)
            return self.judge.chat_json(system, user, temperature=0.2)  # type: ignore[union-attr]
        except LLMError:
            return None
