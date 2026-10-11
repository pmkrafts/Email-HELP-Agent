#!/usr/bin/env python3
"""Deterministic verifier for generated e-mail batches (ToolGrad loop: executor + textual-gradient source).

Works on any batch directory containing `emails/` and `expected.json`:
    python data/verify_batch.py data/generated_batch
    python data/verify_batch.py data/batch_100        (default = the given dir; if omitted, ../generated_batch)

Gate families:
  1. parsing        - every file parses; coverage tags are honoured (html-only, txt separator variants, empty body)
  2. schema         - chains complete, enums valid, no unknown keys
  3. consistency    - labels obey docs 06-Rules invariants
  4. signals        - every claimed flag is evidenced by the email text
  5. hygiene        - reserved domains, unique Message-IDs/subjects, no near-duplicate bodies
  6. coverage       - distribution minimums

Exit 0 = all gates pass. Failures print "[GRADIENT] ..." critique lines.
"""
from __future__ import annotations

import email
import json
import re
import sys
import difflib
from email import policy
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "generated_batch"

SENTIMENTS = {"positive", "neutral", "negative", "angry"}
URGENCIES = {"low", "medium", "high", "critical"}
PRIORITIES = {"Low", "Medium", "High", "Critical"}
BUCKETS = {"immediate", "same_business_day", "within_24h", "next_business_day", "junk"}
TOP_KEYS = {"file", "scenario", "coverage", "classification", "schedule", "draft", "review", "traps"}
CLASS_KEYS = {"sentiment", "urgency", "priority", "category", "flags", "confidence_floor", "notes"}
DRAFT_KEYS = {"language", "required_key_points", "must_not"}

# coverage minimums every batch must satisfy
MIN_CRITICAL = 2
MIN_CATEGORIES = 5
MIN_LONG_WORDS = 450

findings: list[str] = []


def fail(path: str, msg: str) -> None:
    findings.append(f"[GRADIENT] {path}: {msg}")


def normalize(text: str) -> str:
    t = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", t).strip()


def parse_txt(raw: str) -> tuple[dict[str, str], str]:
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    head, sep, body = raw.partition("\n---\n")
    if not sep:
        head, sep, body = raw.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers, body


def main() -> int:
    batch_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DIR
    emails_dir = batch_dir / "emails"
    expected = batch_dir / "expected.json"
    if not expected.exists():
        print(f"FAIL - no expected.json in {batch_dir}")
        return 1
    print(f"batch: {batch_dir}")

    data = json.loads(expected.read_text(encoding="utf-8"))
    items = data["items"]
    expected_empty = {it["file"] for it in items if "empty_body" in it["classification"].get("flags", [])}
    files = sorted(p.name for p in emails_dir.iterdir() if p.suffix in {".eml", ".txt"})
    listed = [it["file"] for it in items]

    if sorted(listed) != files:
        fail("expected.json", f"file set mismatch: missing={set(files) - set(listed)} extra={set(listed) - set(files)}")

    bodies: dict[str, str] = {}
    subjects: dict[str, str] = {}
    msg_ids: list[str] = []
    words: dict[str, int] = {}

    # ---- 1) parsing -------------------------------------------------------
    for name in files:
        p = emails_dir / name
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            fail(name, "not UTF-8 decodable")
            continue
        if p.suffix == ".eml":
            try:
                msg = email.message_from_bytes(raw, policy=policy.default)
            except Exception as exc:  # noqa: BLE001
                fail(name, f"stdlib parser crashed: {exc}")
                continue
            body_part = msg.get_body(preferencelist=("plain",))
            html_part = msg.get_body(preferencelist=("html",))
            bodies[name] = body_part.get_content() if body_part else ""
            if not bodies[name] and html_part is None and name not in expected_empty:
                fail(name, "no text and no html body (and not a deliberately empty sample)")
            subjects[name] = str(msg["subject"] or "")
            if msg["message-id"]:
                msg_ids.append(str(msg["message-id"]))
        else:
            headers, body = parse_txt(text)
            bodies[name] = body
            subjects[name] = headers.get("subject", "")
        words[name] = len(bodies[name].split())

    # coverage-tag driven format expectations
    for it in items:
        name, tags = it["file"], set(it.get("coverage", []))
        raw_text = (emails_dir / name).read_text(encoding="utf-8", errors="replace") if name in files else ""
        if "format:html-only" in tags:
            if not raw_text.lstrip().lower().startswith("from:") or "text/html" not in raw_text:
                fail(name, "tagged html-only but no text/html content type")
            if bodies.get(name):
                fail(name, "tagged html-only but a plain-text body was extracted")
        if "format:txt-dash-separator" in tags and "\n---\n" not in raw_text:
            fail(name, "tagged txt-dash-separator but no '---' separator found")
        if "format:txt-blank-separator" in tags and "\n---\n" in raw_text:
            fail(name, "tagged txt-blank-separator but file contains a '---' separator")

    # ---- 2) schema --------------------------------------------------------
    for it in items:
        f = it["file"]
        missing = TOP_KEYS - set(it)
        if missing:
            fail(f, f"missing top-level keys: {sorted(missing)}")
        extra = set(it) - TOP_KEYS
        if extra:
            fail(f, f"unknown top-level keys: {sorted(extra)}")
        c = it.get("classification", {})
        if c.get("sentiment") not in SENTIMENTS:
            fail(f, f"bad sentiment: {c.get('sentiment')!r}")
        if c.get("urgency") not in URGENCIES:
            fail(f, f"bad urgency: {c.get('urgency')!r}")
        if c.get("priority") not in PRIORITIES:
            fail(f, f"bad priority: {c.get('priority')!r}")
        if not isinstance(c.get("flags"), list):
            fail(f, "flags must be a list")
        if not 0.0 <= c.get("confidence_floor", -1) <= 1.0:
            fail(f, "confidence_floor out of range")
        extra_c = set(c) - CLASS_KEYS
        if extra_c:
            fail(f, f"unknown classification keys: {sorted(extra_c)}")
        extra_d = set(it.get("draft", {})) - DRAFT_KEYS
        if extra_d:
            fail(f, f"unknown draft keys: {sorted(extra_d)}")
        if it.get("schedule", {}).get("bucket") not in BUCKETS:
            fail(f, f"bad bucket: {it.get('schedule', {}).get('bucket')!r}")
        if it.get("review", {}).get("expected_status") not in {"approved", "needs_revision", "escalate"}:
            fail(f, f"bad review status: {it.get('review', {}).get('expected_status')!r}")

    # ---- 3+4) consistency gates and signal gates --------------------------
    for it in items:
        f = it["file"]
        c, d, r = it.get("classification", {}), it.get("draft", {}), it.get("review", {})
        flags = set(c.get("flags", []))
        body = bodies.get(f, "")
        must_not = " ".join(d.get("must_not", [])).lower()
        key_points = " ".join(d.get("required_key_points", [])).lower()
        checks = " ".join(r.get("must_check", [])).lower()

        if "legal_threat" in flags:
            if c.get("priority") != "Critical":
                fail(f, "legal_threat requires priority Critical")
            if not re.search(r"lawyer|sue|legal", body, re.I):
                fail(f, "legal_threat flag but no legal wording in the email text")
            if "liability" not in must_not and "conced" not in must_not:
                fail(f, "legal_threat requires a no-liability-admission prohibition in draft.must_not")
        if "chargeback_risk" in flags:
            if c.get("priority") not in {"High", "Critical"}:
                fail(f, "chargeback_risk requires priority High or Critical")
            if not re.search(r"chargeback|dispute", body, re.I):
                fail(f, "chargeback_risk flag but no chargeback/dispute wording in the email text")
        if "spam" in flags and (c.get("priority") != "Low" or it["schedule"]["bucket"] != "junk"):
            fail(f, "spam requires priority Low and bucket junk")
        if "spam" in flags and not re.search(r"unsubscribe|reward|btc|bitcoin|winner", body, re.I):
            fail(f, "spam flag but no spam markers in the email text")
        if "prompt_injection_attempt" in flags:
            if not re.search(r"ignore all previous|system prompt|developer mode", body, re.I):
                fail(f, "injection flag but no injection payload in the email text")
            if "instruction" not in must_not:
                fail(f, "injection requires draft.must_not to forbid following body instructions")
            if "prompt" not in checks and "leak" not in checks:
                fail(f, "injection requires review.must_check to cover prompt/credential leakage")
        if "refund_request" in flags:
            if c.get("sentiment") == "positive":
                fail(f, "refund_request with positive sentiment is contradictory")
            if not re.search(r"refund", body, re.I):
                fail(f, "refund_request flag but no refund wording in the email text")
        if "empty_body" in flags:
            if len(body.strip()) > 0:
                fail(f, "empty_body flag but body is not empty")
            if c.get("confidence_floor", 1.0) > 0.5:
                fail(f, "empty body must cap confidence_floor at 0.5")
        if "damage_claim" in flags and "seen the photos" not in must_not:
            fail(f, "damage claim needs an explicit ban on claiming to have seen attachments")
        if "damage_claim" in flags and not re.search(r"crack|damag", body, re.I):
            fail(f, "damage_claim flag but no damage wording in the email text")
        if "missing_attachment" in flags and not re.search(r"attach", body, re.I):
            fail(f, "missing_attachment flag but the email never references an attachment")
        if "non_english" in flags:
            if d.get("language") == "en":
                fail(f, "non_english requires a non-English draft language")
            if not re.search(r"[^\x00-\x7f]", body):
                fail(f, "non_english flag but body contains no non-ASCII characters (accented text expected)")
        if "multi_issue" in flags and "highest-impact" not in key_points:
            fail(f, "multi_issue requires the top-2 rule ('highest-impact') in required_key_points")
        if "policy_exception_request" in flags:
            if "promise a refund" not in must_not or "exception" not in must_not:
                fail(f, "policy exception item must forbid refund/exception promises")
        if "outage" in flags and not re.search(r"down|cannot access|504|downtime", body, re.I):
            fail(f, "outage flag but no outage wording in the email text")
        if "delivery_delay" in flags and not re.search(r"three weeks|delay|late\b|stuck|parked|supposed to arrive|label created|not arrived|frozen|nothing has moved|no movement", body, re.I):
            fail(f, "delivery_delay flag but no delay wording in the email text")
        if "repeat_contact" in flags and not re.search(r"fourth|third|four|every time|again", body, re.I):
            fail(f, "repeat_contact flag but no repetition wording in the email text")
        if "deadline_pressure" in flags and not re.search(r"urgent|deadline|monday|friday|today|tomorrow", body, re.I):
            fail(f, "deadline_pressure flag but no deadline wording in the email text")
        if "vip" in flags and not re.search(r"contract|purchase order|account|procurement", body, re.I):
            fail(f, "vip flag but no commercial-account wording in the email text")

    # header-level checks (date anomaly, domains, uniqueness)
    for name in files:
        if not name.endswith(".eml"):
            continue
        msg = email.message_from_bytes((emails_dir / name).read_bytes(), policy=policy.default)
        it = next((i for i in items if i["file"] == name), None)
        if it is None:
            continue
        if "date_anomaly" in it["classification"]["flags"]:
            year = int(str(msg["date"]).split()[-3])
            if year < 2027:
                fail(name, "date_anomaly flag but Date header is not in the future")
        for addr in re.findall(r"[\w.+-]+@([\w.-]+)", str(msg)):
            if not re.fullmatch(r"(([\w-]+\.)*example\.(com|net|org)|[\w-]+\.example)", addr):
                fail(name, f"non-reserved domain in headers: {addr}")

    if len(msg_ids) != len(set(msg_ids)):
        fail("batch", "duplicate Message-IDs")
    non_empty_subjects = [s for s in subjects.values() if s]
    if len(non_empty_subjects) != len(set(non_empty_subjects)):
        dupes = {s for s in non_empty_subjects if non_empty_subjects.count(s) > 1}
        fail("batch", f"duplicate subjects: {sorted(dupes)[:5]}")

    # near-duplicate bodies (template-clone detector)
    names = [n for n in files if n in bodies]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = normalize(bodies[names[i]]), normalize(bodies[names[j]])
            if not a or not b:
                continue
            shorter = min(len(a.split()), len(b.split()))
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if (shorter >= 40 and ratio > 0.93) or (shorter < 40 and ratio > 0.97 and a == b):
                fail("similarity", f"{names[i]} ~ {names[j]} (ratio {ratio:.2f}) - near-duplicate bodies")

    # ---- 6) coverage minimums --------------------------------------------
    def count(fn) -> dict:
        out: dict[str, int] = {}
        for it in items:
            k = fn(it)
            out[k] = out.get(k, 0) + 1
        return out

    sens = count(lambda i: i["classification"]["sentiment"])
    urg = count(lambda i: i["classification"]["urgency"])
    cats = count(lambda i: i["classification"]["category"])
    buckets = count(lambda i: i["schedule"]["bucket"])
    all_flags = {fl for i in items for fl in i["classification"]["flags"]}
    all_tags = {t for i in items for t in i.get("coverage", [])}

    min_reqs = [
        ("sentiment spread", all(sens.get(s, 0) >= 1 for s in SENTIMENTS), sens),
        (f"critical count >= {MIN_CRITICAL}", urg.get("critical", 0) >= MIN_CRITICAL, urg),
        (f">= {MIN_CATEGORIES} distinct categories", len(cats) >= MIN_CATEGORIES, cats),
        ("junk bucket present", buckets.get("junk", 0) >= 1, buckets),
        ("non-english item", "non_english" in all_flags, {}),
        ("injection item", "prompt_injection_attempt" in all_flags, {}),
        ("spam item", "spam" in all_flags, {}),
        ("empty-body item", "empty_body" in all_flags, {}),
        (f"long body >= {MIN_LONG_WORDS} words", max(words.values(), default=0) >= MIN_LONG_WORDS, {"max_words": max(words.values(), default=0)}),
        ("html-only format", "format:html-only" in all_tags, {}),
        ("txt dash-separator format", "format:txt-dash-separator" in all_tags, {}),
        ("txt blank-separator format", "format:txt-blank-separator" in all_tags, {}),
        ("date-anomaly item", "date_anomaly" in all_flags, {}),
    ]
    for label, ok, detail in min_reqs:
        if not ok:
            fail("coverage", f"{label} failed: {detail}")

    # ---- report -----------------------------------------------------------
    print(f"items: {len(items)} | email files: {len(files)}")
    print(f"sentiments: {sens}")
    print(f"urgencies:  {urg}")
    print(f"buckets:    {buckets}")
    print(f"categories: {cats}")
    if findings:
        print(f"\nFAIL - {len(findings)} finding(s):")
        for f in findings:
            print(" ", f)
        return 1
    print("\nPASS - all gates green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
