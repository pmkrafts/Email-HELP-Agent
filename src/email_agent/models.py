"""Domain models (stdlib dataclasses; mirrors docs 07-Data-Schema).

Kept dependency-free on purpose: the POC must run on a bare Python 3.11 install.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Sentiment = Literal["positive", "neutral", "negative", "angry"]
Urgency = Literal["low", "medium", "high", "critical"]
Priority = Literal["Low", "Medium", "High", "Critical"]
Status = Literal["pending", "classified", "drafted", "reviewed", "scheduled", "error"]


@dataclass
class ParsedEmail:
    message_id: str
    from_address: str
    from_name: str | None
    to_addresses: list[str]
    subject: str
    body_text: str
    date: datetime | None
    raw_path: str
    source_format: str  # "eml" | "txt"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "from_address": self.from_address,
            "from_name": self.from_name,
            "to_addresses": self.to_addresses,
            "subject": self.subject,
            "body_text": self.body_text,
            "date": self.date.isoformat() if self.date else None,
            "raw_path": self.raw_path,
            "source_format": self.source_format,
            "warnings": self.warnings,
        }


@dataclass
class Classification:
    sentiment: Sentiment
    urgency: Urgency
    priority: Priority
    priority_score: float
    category: str
    confidence: float
    reasoning: str
    flags: list[str] = field(default_factory=list)
    engine: str = "rules"  # rules | llm | rules+llm


@dataclass
class ContextChunk:
    source: str
    title: str
    content: str
    score: float


@dataclass
class DraftResponse:
    text: str
    confidence: float
    key_points_addressed: list[str] = field(default_factory=list)
    tone_used: str = ""
    citations: list[str] = field(default_factory=list)
    language: str = "en"
    version: int = 1


@dataclass
class ReviewResult:
    status: Literal["approved", "needs_revision", "escalate"]
    issues: list[str] = field(default_factory=list)
    suggested_changes: str | None = None
    final_confidence: float = 0.0


@dataclass
class ScheduleDecision:
    send_at: datetime | None
    reason: str
    status: Literal["scheduled", "pending_human", "escalated", "junk"] = "scheduled"


@dataclass
class AgentState:
    email: ParsedEmail
    classification: Classification | None = None
    context: list[ContextChunk] = field(default_factory=list)
    draft: DraftResponse | None = None
    review: ReviewResult | None = None
    schedule: ScheduleDecision | None = None
    logs: list[dict[str, Any]] = field(default_factory=list)
    status: Status = "pending"
    error: str | None = None

    def log(self, agent: str, event: str, **details: Any) -> None:
        self.logs.append({"agent": agent, "event": event, **details})
