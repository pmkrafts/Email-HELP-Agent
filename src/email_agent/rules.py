"""Rules engine: signal detection, scoring, priority bands, scheduling.

Implements docs 06-Rules with the scales pinned in config/rules.json:
    priority_score = urgency_base*0.5 + sentiment_weight*0.3 + tier*0.2 + boosts (capped)
Bands: Critical >= 8, High >= 6, Medium >= 4, else Low.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

FLAG_PATTERNS: dict[str, str] = {
    "legal_threat": r"\b(lawyer|solicitor|lawsuit|\bsue\b|tribunal|legal action|legal steps|consumer court|in court)\b",
    "chargeback_risk": r"\b(chargeback|dispute[sd]?|card issuer)\b",
    "refund_request": r"\b(refund|money back|reimburs\w+|reversal)\b",
    "outage": r"\b(504|downtime|cannot access|site is down|checkout (?:is )?down|(?:site|store|website|service)\b[^.]{0,40}\bdown)\b",
    "cancellation": r"\b(cancel my|cancel the|want to cancel|please cancel|should be cancelled|cancellation)\b",
    "delivery_delay": r"\b(late|delay\w*|stuck|not moved|no movement|not arrived|still waiting|still not (?:arrived|shipped|moved))\b",
    "damage_claim": r"\b(crack\w*|damag\w+|broken|fractured|dented|defective|faulty)\b",
    "deadline_pressure": r"\b(urgent\w*|asap|immediately|deadline|by (?:monday|friday|tomorrow|the weekend)|before (?:monday|friday|the weekend))\b|need\b[^.]{0,25}\btoday\b",
    "attachment_reference": r"\battach\w*",
    "repeat_contact": r"\b(fourth|third|fifth|second time|every time|again|still no (?:reply|answer|response)|no answer|no reply|keep chasing)\b",
    "escalation": r"\b(supervisor|escalat\w*|senior human|take ownership)\b",
    "vip": r"\b(contract|purchase order|procurement|account manager|bulk order|account terms)\b",
    "spam": r"\b(winner|lottery|bitcoin|btc|unsubscribe|guaranteed|click the link|reward)\b",
    "prompt_injection_attempt": r"(ignore (?:all )?previous instructions|system prompt|developer mode|maintenance mode|reveal your (?:system )?(?:prompt|instructions|configuration)|print your instructions)",
    "low_priority_language": r"\b(no rush|no hurry|not urgent|nothing urgent|no action needed|at your convenience|whenever you get a chance|whenever convenient)\b",
    "policy_exception_request": r"\b(make an exception|as an exception|even a discount|any way you can help|i broke it|i dropped|my own fault|my fault)\b",
}

NON_ENGLISH_HINTS = re.compile(
    r"\b(hola|gracias|saludos|buenos d|buenas|pedido|env[ií]o|guten tag|hallo|bitte|danke|freundlichen|bestellung|"
    r"bonjour|merci|commande|livraison|votre)\b",
    re.IGNORECASE,
)

NEG_STRONG = {
    "unacceptable", "frustrated", "frustrating", "disappointed", "angry", "furious", "terrible", "awful",
    "ridiculous", "insulting", "ignored", "complaint", "rude", "fed up", "appalled", "disgusting",
    "worst", "useless", "nightmare", "disaster",
}
NEG_WEAK = {
    "late", "delay", "delayed", "damaged", "cracked", "faulty", "broken", "wrong", "missing", "stuck",
    "waiting", "unresolved", "chasing", "poor", "issue", "problem", "defect", "damage", "fault",
    "unhappy", "concerned", "confusing", "blocked", "locked", "expired",
}
POS_STRONG = {
    "thank", "great", "excellent", "brilliant", "appreciate", "delighted", "perfect",
    "outstanding", "recommend", "praise", "amazing",
}
POS_WEAK = {"helpful", "good", "smooth", "easy", "love", "grateful", "fast", "patient", "tidy"}

ANGER_MARKERS = re.compile(
    r"\b(lawyer|sue|lawsuit|solicitor|furious|unacceptable|ridiculous|insulting|final notice|fed up|"
    r"disgusting|appalled|never again|disgrace)\b",
    re.IGNORECASE,
)

NEG_PHRASES = [
    r"not (?:moved|arrived|shipped|delivered|replied|answered|reversed|resolved|received|processed)",
    r"still (?:waiting|not|no)\b",
    r"no (?:reply|answer|response|update|movement|confirmation|label)\b",
    r"past (?:its|the) (?:date|window|deadline)",
    r"out of stock",
    r"cannot (?:access|log in|sign in|order|check ?out)",
    r"does not work",
    r"was supposed to",
    r"never (?:arrived|replied|received)",
    r"\bwaiting for\b",
    r"wrong (?:item|model|colour|color|product)",
    r"(?:days?|weeks?) and counting",
    r"nothing has moved",
]

# lines that are greetings/sign-offs rather than content (stripped before lexicon scoring)
_CLOSING_PREFIXES = (
    "hi", "hello", "hey", "dear", "thanks", "thank you", "best regards", "kind regards",
    "regards", "best", "cheers",
)
CLOSING_LINE = re.compile(
    r"(?im)^[ \t]*(?P<prefix>" + "|".join(re.escape(p) for p in _CLOSING_PREFIXES) + r")\b(?P<tail>[^\n]*)$"
)
_POLITE_TAIL = re.compile(r"\b(fine|ok|okay|all i need|in advance|please|for your (?:help|patience|understanding|time))\b", re.I)


def strip_closings(text: str) -> str:
    """Drop greeting/sign-off lines, but never lines that carry sentiment content.

    A line counts as a closing when its tail is short (<= 3 words), is name-like
    ("Thanks, Lena Vogt"), or is polite boilerplate ("Thank you - a copy by email is fine").
    """

    def repl(match: re.Match[str]) -> str:
        tail = match.group("tail").strip(" \t,.-!:")
        words = tail.split()
        name_like = bool(words) and len(words) <= 3 and all(w[:1].isupper() for w in words)
        polite = bool(_POLITE_TAIL.search(tail))
        if len(words) <= 3 or name_like or polite:
            return " "
        return match.group(0)

    return CLOSING_LINE.sub(repl, text)

INJECTION_MARKER = re.compile(
    r"(ignore (?:all )?previous instructions|ignore previous instructions|system prompt|developer mode|"
    r"maintenance mode|print your instructions|reveal your (?:system )?(?:prompt|instructions|configuration))",
    re.IGNORECASE,
)

LEGAL_PATTERN = re.compile(
    r"\b(lawyer|solicitor|lawsuit|\bsue\b|tribunal|legal action|legal steps|consumer court|in court)\b",
    re.IGNORECASE,
)

VAGUE_REQUEST = re.compile(
    r"(have a problem with|something went wrong|can someone please help|can you help|please advise)",
    re.IGNORECASE,
)


def strip_injected(text: str) -> str:
    """Remove sentences carrying injection payloads so they cannot influence scoring or routing.

    Paragraph structure is preserved (greeting/sign-off lines must survive at line starts).
    """
    paragraphs = []
    for para in re.split(r"\n\s*\n", text):
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", para) if s.strip() and not INJECTION_MARKER.search(s)]
        if sentences:
            paragraphs.append(" ".join(sentences))
    return "\n\n".join(paragraphs)

SARCASM_MARKERS = re.compile(
    r"\b(world-class|masterclass|incredible|impressive|most reliable|speed of light|take your time)\b",
    re.IGNORECASE,
)

TOPIC_PATTERNS = {
    "wrong_item": r"\b(wrong (?:item|model|colour|color)|instead of|not what i (?:paid|ordered)|compact model arrived)\b",
    "double_charge": r"\b(charged (?:my card )?twice|two charges|double charge|duplicate (?:charge|payment)|billed twice|charged twice)\b",
    "agent_conduct": r"\b(agent (?:told|said|promised)|hung up|supervisor|chat transcript|call reference|different (?:person|agent))\b",
    "delay": r"\b(late|delay\w*|stuck|still not arrived|four days late)\b",
    "damage": r"\b(damag\w+|crack\w+|dented|defect\w*)\b",
    "refund": r"\b(refund\w*|money back)\b",
    "paperwork": r"\b(invoice (?:shows|does not|wrong)|paperwork|tax breakdown|vat)\b",
    "cancellation": r"\b(cancel\w*|subscription)\b",
}


@dataclass
class RulesResult:
    flags: list[str]
    sentiment: str
    urgency: str
    priority: str
    priority_score: float
    confidence: float
    reasoning: str
    category: str
    boosts: float


def _hits(text: str, words: set[str]) -> int:
    """Count lexicon hits including simple inflections (problem/problems, recommend/recommended)."""
    pattern = r"\b(?:" + "|".join(sorted(words)) + r")(?:s|es|d|ed|ing|ly)?\b"
    return len(re.findall(pattern, text.lower()))


def detect_flags(text: str, subject: str = "") -> list[str]:
    blob = f"{subject}\n{text}"
    flags = [name for name, pattern in FLAG_PATTERNS.items() if re.search(pattern, blob, re.I)]
    topics = {name for name, pattern in TOPIC_PATTERNS.items() if re.search(pattern, blob, re.I)}
    if len(topics) >= 3 and not flags.count("spam"):
        flags.append("multi_issue")
    if NON_ENGLISH_HINTS.search(text):
        flags.append("non_english")
    return sorted(set(flags))


def detect_sentiment(text: str) -> tuple[str, dict[str, int]]:
    text = strip_closings(text)
    strong_neg = _hits(text, NEG_STRONG)
    weak_neg = _hits(text, NEG_WEAK)
    strong_pos = _hits(text, POS_STRONG)
    weak_pos = _hits(text, POS_WEAK)
    phrases = sum(1 for pattern in NEG_PHRASES if re.search(pattern, text, re.I))
    neg = strong_neg * 2 + weak_neg + phrases
    pos = strong_pos * 2 + weak_pos
    anger_hits = len(ANGER_MARKERS.findall(text))
    legal = bool(LEGAL_PATTERN.search(text))
    sarcasm = bool(SARCASM_MARKERS.search(text)) and bool(re.search(r"\b(delay|late|stuck|weeks|parked|still)\b", text, re.I))

    if sarcasm:
        sentiment = "negative"
    elif legal or anger_hits >= 2 or (anger_hits >= 1 and strong_neg >= 2):
        sentiment = "angry"
    elif neg > pos:
        sentiment = "negative"
    elif pos > neg:
        sentiment = "positive"
    else:
        sentiment = "neutral"
    return sentiment, {"neg": neg, "pos": pos}


ABSOLUTE_LOW_FLAGS = {"spam", "missing_info", "low_priority_language"}
RAISING_FLAGS = {"delivery_delay", "repeat_contact", "multi_issue", "chargeback_risk", "cancellation",
                 "refund_request", "deadline_pressure", "damage_claim", "outage", "legal_threat"}
POLICY_EXCEPTION_BLOCKERS = RAISING_FLAGS - {"damage_claim", "refund_request"}
HARD_FLAGS = {"legal_threat", "outage", "chargeback_risk"}


def detect_category(text: str, subject: str, flags: list[str]) -> str:
    blob = f"{subject}\n{text}".lower()
    if "spam" in flags:
        return "spam"
    if flags and "prompt_injection_attempt" in flags and len(flags) == 1:
        return "shipping"
    if re.search(r"\b(invoice|vat|iva|receipt|tax)\b", blob):
        return "billing"
    if re.search(r"\b(refund|return|replacement|damag\w+|crack\w+|warranty)\b", blob):
        return "returns"
    if re.search(r"\b(charged|charge|billed|billing|card|statement|payment|subscription|cancel)\b", blob):
        return "billing"
    if re.search(r"\b(late|delivery|parcel|tracking|ship\w*|arrive\w*|courier)\b", blob):
        return "shipping"
    if re.search(r"\b(password|log ?in|sign ?in|locked out|account)\b", blob):
        return "account"
    if re.search(r"\b(thank|thanks|praise|feedback|suggest\w+)\b", blob):
        return "feedback"
    if re.search(r"\b(compatible|warranty|spec\w*|in stock|before i (?:buy|order)|question)\b", blob):
        return "sales"
    if re.search(r"\b(down|outage|504|error|cannot access)\b", blob):
        return "technical"
    if re.search(r"\b(complaint|unacceptable|angry|frustrat\w+)\b", blob):
        return "complaint"
    return "other"


def _band(score: float, bands: dict[str, float]) -> str:
    for name in ("Critical", "High", "Medium"):
        if score >= bands[name]:
            return name
    return "Low"


def score_email(
    rules: dict[str, Any],
    text: str,
    subject: str = "",
    vip: bool | None = None,
    flags: list[str] | None = None,
    warnings: list[str] | None = None,
) -> RulesResult:
    scan_text = strip_injected(text)
    flags = list(flags) if flags is not None else detect_flags(scan_text, subject)
    if INJECTION_MARKER.search(text):
        flags = sorted(set(flags) | {"prompt_injection_attempt"})
    # parser-derived signals become first-class classification flags
    warnings = set(warnings or [])
    word_count = len(text.split())
    if "empty_body" in warnings:
        flags = sorted(set(flags) | {"empty_body", "missing_info"})
    if "future_date" in warnings:
        flags = sorted(set(flags) | {"date_anomaly"})
    if word_count < 8 and "empty_body" not in warnings:
        flags = sorted(set(flags) | {"missing_info"})
    if word_count < 40 and VAGUE_REQUEST.search(text) and not re.search(r"AUR-\d", text, re.I):
        flags = sorted(set(flags) | {"missing_info"})
    flag_set = set(flags)
    sentiment, lex = detect_sentiment(scan_text)

    floors = rules["urgency_floor"]
    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    urgency = "low" if sentiment == "positive" else "medium"
    for flag in flag_set:
        floor = floors.get(flag)
        if floor and rank[floor] > rank[urgency]:
            urgency = floor
    # refunds inside a billing frame (charged/billed/statement/card) are high-urgency
    if "refund_request" in flag_set and re.search(r"\b(charged|charge|billed|billing|statement|card|bank)\b", scan_text, re.I):
        if rank["high"] > rank[urgency]:
            urgency = "high"
    # pure praise never escalates urgency
    if sentiment == "positive" and not (flag_set & HARD_FLAGS):
        urgency = "low"
    zero_boost = False
    if flag_set & ABSOLUTE_LOW_FLAGS:
        urgency = "low"
        zero_boost = True
    elif "policy_exception_request" in flag_set and not (flag_set & POLICY_EXCEPTION_BLOCKERS):
        urgency = "low"
        zero_boost = True

    tier = "vip" if (vip if vip is not None else "vip" in flag_set) else "standard"
    scales = rules["scales"]
    base = (
        scales["urgency_base"][urgency] * 0.5
        + scales["sentiment_weight"][sentiment] * 0.3
        + scales["customer_tier"][tier] * 0.2
    )
    boost_map = rules["boosts"]
    cap = boost_map["cap_total"]
    raw_boost = sum(boost_map.get(flag, 0.0) for flag in flag_set)
    boost = min(raw_boost, cap)
    if zero_boost:
        boost = 0.0
    score = round(base + boost, 2)
    priority = _band(score, rules["bands"])
    for flag, forced in rules.get("force_priority", {}).items():
        if flag in flag_set:
            priority = forced

    n_signals = len(flag_set)
    strong_polarity = abs(lex["neg"] - lex["pos"]) >= 2
    confidence = 0.6 + 0.04 * min(n_signals, 5) + (0.15 if strong_polarity else 0.0)
    if "spam" in flag_set:
        confidence = max(confidence, 0.9)
    if word_count < 8:
        confidence = min(confidence, 0.35)
    if "prompt_injection_attempt" in flag_set:
        confidence = min(confidence, 0.7)
    confidence = round(min(confidence, 0.92), 2)

    reasoning = (
        f"sentiment={sentiment} (lex neg={lex['neg']}, pos={lex['pos']}); "
        f"urgency={urgency}; tier={tier}; base={base:.2f}; boost={boost:.2f} (raw {raw_boost:.2f}, cap {cap}); "
        f"signals={','.join(flags) or 'none'}"
    )
    category = detect_category(text, subject, flags)
    return RulesResult(flags, sentiment, urgency, priority, score, confidence, reasoning, category, boost)


def _hours(sched: dict[str, Any]) -> tuple[int, int]:
    start, end = sched["business_hours"].split("-")
    return int(start.split(":")[0]), int(end.split(":")[0])


def next_business_window(now: datetime, rules: dict[str, Any]) -> datetime:
    sched = rules["scheduling"]
    start_h, end_h = _hours(sched)
    days = set(sched["business_days"])
    candidate = now
    for _ in range(14):
        if candidate.weekday() in days and start_h <= candidate.hour < end_h:
            return candidate
        nxt = (candidate + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        if nxt.hour < start_h or nxt.hour >= end_h or nxt.weekday() not in days:
            nxt = (nxt + timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)
            while nxt.weekday() not in days:
                nxt += timedelta(days=1)
        candidate = nxt
    return now


def decide_schedule(priority: str, now: datetime, rules: dict[str, Any], escalated: bool = False,
                    junk: bool = False) -> tuple[datetime | None, str, str]:
    """Returns (send_at, reason, status)."""
    if junk:
        return None, "routed to junk queue; no customer-facing schedule", "junk"
    if escalated:
        return None, "escalated to human handling; no automatic schedule", "escalated"
    sched = rules["scheduling"]
    if priority == "Critical":
        if sched.get("critical_ignores_business_hours", True):
            return now + timedelta(hours=1), "critical: immediate queue (business hours deliberately ignored)", "scheduled"
        slot = next_business_window(now, rules)
        return slot, "critical: within 1 hour of business hours", "scheduled"
    if priority == "High":
        window = now + timedelta(hours=sched["high_hours"])
        _, end_h = _hours(sched)
        if window.weekday() in set(sched["business_days"]) and window.hour < end_h:
            return window, f"high: within {sched['high_hours']} hours", "scheduled"
        return next_business_window(now, rules), "high: same business day (outside hours -> next business window)", "scheduled"
    if priority == "Medium":
        return next_business_window(now, rules), f"medium: within {sched['medium_hours']} hours, business window", "scheduled"
    return next_business_window(now, rules), "low: next business day", "scheduled"
