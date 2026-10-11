"""Classifier agent — three tiers, cheapest first (cost ladder).

Tier 0 (always):  deterministic rules engine (`rules.score_email`) — zero tokens.
Tier 1 (optional): primary LLM refinement (OpenAI etc.) over the rules baseline.
Tier 2 (optional): **judge channel** (JEV or any cheap OpenAI-compatible endpoint) reviews the
                   proposal. Agreement keeps the cheap path; disagreement routes that single email
                   to the primary model for adjudication (or applies the judge's correction when no
                   primary model exists). This is the cost-control design: premium spend scales with
                   the *disagreement rate*, not with volume.

Every tier failure degrades to the tier below — a broken judge never blocks a batch.
"""
from __future__ import annotations

import json

from ..config import Settings
from ..judges import JudgeChannel
from ..llm import LLMClient, LLMError, load_prompt
from ..models import Classification, ParsedEmail
from ..rules import score_email

SENTIMENTS = {"positive", "neutral", "negative", "angry"}
URGENCIES = {"low", "medium", "high", "critical"}
PRIORITIES = {"Low", "Medium", "High", "Critical"}


class Classifier:
    def __init__(self, settings: Settings, client: LLMClient | None = None, judge: JudgeChannel | None = None):
        self.settings = settings
        self.client = client
        self.judge = judge

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
        # Escalation ladder. `always`: the strong model refines every email (premium quality),
        # then the judge verifies it. `escalations_only`: the strong model is *reserved for
        # disagreements* — agreement keeps the rules/judge path and premium spend stays at zero.
        if self.client is not None and self.settings.refine_mode == "always":
            classification = self._refine(email, classification)
        if self.judge is not None:
            classification = self._judge(email, classification)
        return classification

    # -- tier 1: primary refinement ----------------------------------------
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
            data = self.client.chat_json(system, user, temperature=0.2)  # type: ignore[union-attr]
        except LLMError:
            return base
        return self._merge(base, data, engine="rules+llm", note="llm")

    # -- tier 2: judge channel (JEV) ---------------------------------------
    def _judge(self, email: ParsedEmail, proposal: Classification) -> Classification:
        data = self.judge.verdict(email, proposal)
        if data is None:
            proposal.judge_verdict = "error"
            return proposal
        if bool(data.get("agree", True)):
            proposal.judge_verdict = "agreed"
            proposal.reasoning = f"{proposal.reasoning} | judge: agreed ({str(data.get('reason', ''))[:120]})"
            return proposal

        # disagreement: escalate this single email to the primary model when available
        if self.client is not None:
            adjudicated = self._adjudicate(email, proposal, data)
            if adjudicated is not None:
                adjudicated.judge_verdict = "adjudicated"
                return adjudicated

        corrected = self._apply_judge_correction(proposal, data)
        if corrected is not None:
            corrected.judge_verdict = "overridden"
            return corrected
        proposal.judge_verdict = "agreed"  # judge disagreed but returned nothing usable
        proposal.reasoning = f"{proposal.reasoning} | judge: unusable correction, kept proposal"
        return proposal

    def _adjudicate(self, email: ParsedEmail, proposal: Classification, judge_data: dict) -> Classification | None:
        try:
            system = load_prompt(self.settings.prompts_dir, "classifier")
            user = json.dumps({
                "subject": email.subject,
                "body": email.body_text[:6000],
                "rules_baseline": {
                    "sentiment": proposal.sentiment,
                    "urgency": proposal.urgency,
                    "priority": proposal.priority,
                    "priority_score": proposal.priority_score,
                    "category": proposal.category,
                    "flags": proposal.flags,
                    "confidence": proposal.confidence,
                },
                "judge_disagreement": {
                    "reason": judge_data.get("reason", ""),
                    "judge_labels": {
                        "sentiment": judge_data.get("sentiment"),
                        "urgency": judge_data.get("urgency"),
                        "priority": judge_data.get("priority"),
                    },
                },
                "instruction": "A cheap judge disagreed with the proposal. Decide the final labels.",
            }, ensure_ascii=False)
            data = self.client.chat_json(system, user, temperature=0.2)  # type: ignore[union-attr]
        except LLMError:
            return None
        merged = self._merge(proposal, data, engine=f"{proposal.engine}+adjudicated", note="adjudicator")
        merged.reasoning = f"{merged.reasoning} | judge: disagreed ({str(judge_data.get('reason', ''))[:120]})"
        return merged

    # -- shared helpers -----------------------------------------------------
    def _merge(self, base: Classification, data: dict, engine: str, note: str) -> Classification:
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
            reasoning=f"{base.reasoning} | {note}: {str(data.get('reasoning', data.get('reason', '')))[:200]}",
            flags=base.flags,
            engine=engine,
            judge_verdict=base.judge_verdict,
        )

    def _apply_judge_correction(self, proposal: Classification, judge_data: dict) -> Classification | None:
        sentiment = judge_data.get("sentiment")
        urgency = judge_data.get("urgency")
        priority = judge_data.get("priority")
        if not (
            (sentiment in SENTIMENTS) or (urgency in URGENCIES) or (priority in PRIORITIES)
        ):
            return None
        confidence = judge_data.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = min(proposal.confidence, 0.5)
        if not judge_data.get("calibrated"):
            confidence = min(float(confidence), 0.65)  # uncalibrated chat judges get capped
        return Classification(
            sentiment=sentiment if sentiment in SENTIMENTS else proposal.sentiment,
            urgency=urgency if urgency in URGENCIES else proposal.urgency,
            priority=priority if priority in PRIORITIES else proposal.priority,
            priority_score=proposal.priority_score,
            category=proposal.category,
            confidence=round(min(float(confidence), 1.0), 2),
            reasoning=(
                f"{proposal.reasoning} | judge override ({str(judge_data.get('reason', ''))[:120]}): "
                f"{proposal.sentiment}/{proposal.urgency}/{proposal.priority} -> "
                f"{sentiment if sentiment in SENTIMENTS else proposal.sentiment}/"
                f"{urgency if urgency in URGENCIES else proposal.urgency}/"
                f"{priority if priority in PRIORITIES else proposal.priority}"
            ),
            flags=proposal.flags,
            engine=f"{proposal.engine}+judge-override",
            judge_verdict="overridden",
        )
