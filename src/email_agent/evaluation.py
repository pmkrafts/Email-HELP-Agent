"""Golden-set harness: agreement metrics against the labeled batches.

Supports both label schemas:
  - `expected.json`  ({"items": [{file, classification:{sentiment,urgency,priority,flags}, ...}]})
  - `ground_truth.json` ({"emails": [{file, sentiment, urgency, priority, flags, ...}]})

Metrics: priority / sentiment / urgency agreement, flag micro precision-recall, schedule-bucket
agreement, confidence-floor compliance. Prints the misses so disagreements are actionable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .parser import parse_dir
from .rules import decide_schedule, score_email

INTERNAL_FLAGS = {"low_priority_language", "attachment_reference", "escalation"}

BUCKET_FROM_PRIORITY = {
    "Critical": "immediate",
    "High": "same_business_day",
    "Medium": "within_24h",
    "Low": "next_business_day",
}


def load_labels(batch_dir: str | Path) -> dict[str, dict]:
    d = Path(batch_dir)
    labels: dict[str, dict] = {}
    if (d / "expected.json").exists():
        data = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        for item in data["items"]:
            cls = item.get("classification", {})
            labels[item["file"]] = {
                "sentiment": cls.get("sentiment"),
                "urgency": cls.get("urgency"),
                "priority": cls.get("priority"),
                "flags": set(cls.get("flags", [])),
                "confidence_floor": cls.get("confidence_floor"),
                "bucket": (item.get("schedule") or {}).get("bucket"),
            }
    elif (d / "ground_truth.json").exists():
        data = json.loads((d / "ground_truth.json").read_text(encoding="utf-8"))
        for item in data["emails"]:
            labels[item["file"]] = {
                "sentiment": item.get("sentiment"),
                "urgency": item.get("urgency"),
                "priority": item.get("priority"),
                "flags": set(item.get("flags", [])),
                "confidence_floor": None,
                "bucket": None,
            }
    else:
        raise FileNotFoundError(f"no expected.json or ground_truth.json in {batch_dir}")
    return labels


def _emails_dir(batch_dir: Path) -> Path:
    return batch_dir / "emails" if (batch_dir / "emails").is_dir() else batch_dir


def evaluate(settings: Settings, batch_dir: str | Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    d = Path(batch_dir)
    labels = load_labels(d)
    parsed, failures = parse_dir(d)
    by_name = {Path(email.raw_path).name: email for email in parsed}

    total = 0
    hit = {"priority": 0, "sentiment": 0, "urgency": 0, "bucket": 0}
    bucket_total = 0
    floor_ok = 0
    floor_total = 0
    flag_tp = flag_fp = flag_fn = 0
    misses: list[str] = []
    rows: list[dict] = []

    for filename, expected in sorted(labels.items()):
        email = by_name.get(filename)
        if email is None:
            misses.append(f"{filename}: not parsed")
            continue
        total += 1
        result = score_email(settings.rules, email.body_text, email.subject, warnings=email.warnings)
        detected = set(result.flags) - INTERNAL_FLAGS
        expected_flags = set(expected["flags"]) - INTERNAL_FLAGS
        flag_tp += len(detected & expected_flags)
        flag_fp += len(detected - expected_flags)
        flag_fn += len(expected_flags - detected)

        ok_priority = result.priority == expected["priority"]
        ok_sentiment = result.sentiment == expected["sentiment"]
        ok_urgency = result.urgency == expected["urgency"]
        hit["priority"] += ok_priority
        hit["sentiment"] += ok_sentiment
        hit["urgency"] += ok_urgency

        ok_bucket = None
        if expected["bucket"]:
            bucket_total += 1
            escalated = "legal_threat" in expected_flags
            decided_bucket = (
                "junk" if "spam" in detected
                else BUCKET_FROM_PRIORITY.get(result.priority, "next_business_day")
            )
            if escalated:
                decided_bucket = BUCKET_FROM_PRIORITY.get(result.priority, "immediate")
            ok_bucket = decided_bucket == expected["bucket"]
            hit["bucket"] += ok_bucket

        if expected["confidence_floor"] is not None:
            floor_total += 1
            if result.confidence >= expected["confidence_floor"]:
                floor_ok += 1

        rows.append({
            "file": filename,
            "expected": (expected["priority"], expected["sentiment"], expected["urgency"]),
            "got": (result.priority, result.sentiment, result.urgency),
            "score": result.priority_score,
            "ok": (ok_priority, ok_sentiment, ok_urgency),
        })
        got_value = {"priority": result.priority, "sentiment": result.sentiment, "urgency": result.urgency}
        for dimension, ok in (("priority", ok_priority), ("sentiment", ok_sentiment), ("urgency", ok_urgency)):
            if not ok:
                misses.append(
                    f"{filename} [{dimension}]: expected {expected[dimension]!r}, got {got_value[dimension]!r} "
                    f"(score {result.priority_score}, flags {sorted(detected)})"
                )

    precision = flag_tp / (flag_tp + flag_fp) if (flag_tp + flag_fp) else 0.0
    recall = flag_tp / (flag_tp + flag_fn) if (flag_tp + flag_fn) else 0.0
    return {
        "batch": str(d),
        "n": total,
        "parse_failures": failures,
        "priority_agreement": round(hit["priority"] / total, 3) if total else 0.0,
        "sentiment_agreement": round(hit["sentiment"] / total, 3) if total else 0.0,
        "urgency_agreement": round(hit["urgency"] / total, 3) if total else 0.0,
        "bucket_agreement": round(hit["bucket"] / bucket_total, 3) if bucket_total else None,
        "flag_precision": round(precision, 3),
        "flag_recall": round(recall, 3),
        "confidence_floor_compliance": round(floor_ok / floor_total, 3) if floor_total else None,
        "misses": misses,
        "rows": rows,
    }
