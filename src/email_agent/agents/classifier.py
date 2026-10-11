"""Classifier agent: deterministic rules engine first, optional LLM refinement second.

The rules engine always produces a complete classification (offline mode). When an LLM is
configured, it may *refine* sentiment/urgency/priority/category — never invent new signals.
"""
from __future__ import annotations

import json

from ..config import Settings
from ..llm import LLMClient, LLMError, load_prompt
from ..models import Classification, ParsedEmail
from ..rules import score_email

SENTIMENTS = {"positive", "neutral", "negative", "angry"}
URGENCIES = {"low", "medium", "high", "critical"}
PRIORITIES = {"Low", "Medium", "High", "Critical"}


class Classifier:
    def __init__(self, settings: Settings, client: LLMClient | None = None):
        self.settings = settings
        self.client = client

    def run(self, email: ParsedEmail) -> Classification:
        result = score_email(self.settings.rules, email.body_text, email.subject, warnings=email.warnings)
        classification = Classification(
            sentiment=result.sentiment,
            urgency=result.urgency,
            priority=result.priority,
            priority_score=result.priority_score,
            category=result.category,
            confidence=result.confidence,
            reasoning=result.reasoning,
            flags=result.flags,
            engine="rules",
        )
        if self.client is None:
            return classification
        return self._refine(email, classification)

    def _refine(self, email: ParsedEmail, base: Classification) -> Classification:
        try:
            system = load_prompt(self.settings.prompts_dir, "classifier")
            user = json.dumps({
                "subject": email.subject,
                "body": email.body_text[:6000],
                "rules_baseline": {
                    "sentiment": base.sentiment,
                    "urgency": base.urgency,
                    "priority": base.priority,
                    "priority_score": base.priority_score,
                    "category": base.category,
                    "flags": base.flags,
                    "confidence": base.confidence,
                },
            }, ensure_ascii=False)
            data = self.client.chat_json(system, user, temperature=0.2)
        except LLMError:
            return base
        sentiment = data.get("sentiment") if data.get("sentiment") in SENTIMENTS else base.sentiment
        urgency = data.get("urgency") if data.get("urgency") in URGENCIES else base.urgency
        priority = data.get("priority") if data.get("priority") in PRIORITIES else base.priority
        confidence = data.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = base.confidence
        return Classification(
            sentiment=sentiment,
            urgency=urgency,
            priority=priority,
            priority_score=base.priority_score,
            category=str(data.get("category") or base.category),
            confidence=round(float(confidence), 2),
            reasoning=f"{base.reasoning} | llm: {str(data.get('reasoning', ''))[:200]}",
            flags=base.flags,
            engine="rules+llm",
        )
