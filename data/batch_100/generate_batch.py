#!/usr/bin/env python3
"""Answer-first batch generator (ToolGrad-style: expected chain first, email annotated to match).

Reads archetype specs, fills slots, renders `emails/` and `expected.json`, and self-checks
before writing (subject/Message-ID uniqueness, no leftover placeholders, signal coverage).

Usage:  python data/batch_100/generate_batch.py
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

from content_a import ARCHETYPES as A
from content_b import ARCHETYPES as B

HERE = Path(__file__).resolve().parent
EMAILS = HERE / "emails"
EXPECTED = HERE / "expected.json"
ARCHETYPES = A + B

NAMES = [
    "Nadia Rahman", "Victor Osei", "Lucy Bennett", "Omar Haddad", "Freya Lindqvist",
    "Daniel Okafor", "Mei Chen", "Tomas Rivera", "Aisha Bello", "Peter Novak",
    "Clara Fontaine", "Jin Park", "Marta Silva", "Ben Carter", "Yara Haddadin",
    "Igor Petrov", "Sana Malik", "Felix Braun", "Rosa Jimenez", "Ethan Brooks",
    "Lena Vogt", "Arjun Desai", "Chloe Martin", "Kwame Mensah",
]
PRODUCTS = [
    "standing desk", "monitor arm", "USB-C docking station", "smart thermostat", "electric kettle",
    "blender", "10-inch tablet", "noise-cancelling headphones", "mechanical keyboard", "desk lamp",
    "wireless webcam", "portable SSD", "Bluetooth speaker", "laptop stand",
]
AMOUNTS = ["$49.99", "$89.00", "$129.99", "$179.00", "$249.99", "$319.00", "$429.00", "$59.50"]
COMPANIES = ["Meridian", "Northlake", "Vertex", "Blue Harbor"]
DATES = [
    "Fri, 09 Oct 2026 08:12:00 +0530", "Sat, 10 Oct 2026 23:10:00 +0530", "Sun, 11 Oct 2026 07:45:00 +0000",
    "Mon, 12 Oct 2026 09:05:00 +0100", "Mon, 12 Oct 2026 19:40:00 -0400", "Tue, 13 Oct 2026 14:20:00 +0200",
    "Wed, 14 Oct 2026 11:30:00 +0000", "Wed, 14 Oct 2026 21:15:00 +0900", "Thu, 15 Oct 2026 16:55:00 +0000",
    "Fri, 16 Oct 2026 18:30:00 +0100", "Fri, 16 Oct 2026 22:05:00 +0530", "Sat, 17 Oct 2026 03:11:00 +0000",
]
DATES_SHORT = [
    "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-12", "2026-10-13",
    "2026-10-14", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-16", "2026-10-17",
]
CLOSINGS = ["Thanks,\n{name}", "Regards,\n{name}", "Best regards,\n{name}", "Kind regards,\n{name}", "Thank you,\n{name}"]
FORMAT_TAGS = {
    "eml": [], "html": ["format:html-only"],
    "txt": ["format:txt-blank-separator"], "txt_dash": ["format:txt-dash-separator"],
    "eml_empty": ["parser:empty-body"],
}
FORMAT_EXT = {"eml": "eml", "html": "eml", "txt": "txt", "txt_dash": "txt", "eml_empty": "eml"}


def slugify(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]+", ".", folded.lower()).strip(".")


def render(fmt: str, name: str, addr: str, subject: str, date: str, date_short: str, msgid: str, body: str) -> str:
    if fmt in {"txt", "txt_dash"}:
        sep = "\n---\n" if fmt == "txt_dash" else "\n"
        return f"From: {addr}\nSubject: {subject}\nDate: {date_short}{sep}{body}\n"
    headers = (
        f'From: "{name}" <{addr}>\n'
        f"To: support@aurora-electronics.example\n"
        f"Subject: {subject}\n"
        f"Date: {date}\n"
        f"Message-ID: {msgid}\n"
        f"MIME-Version: 1.0\n"
    )
    if fmt == "eml_empty":
        return headers + 'Content-Type: text/plain; charset="utf-8"\n\n'
    if fmt == "html":
        html_body = "<html><body>" + "".join(f"<p>{p.strip()}</p>" for p in body.split("\n\n")) + "</body></html>"
        return headers + 'Content-Type: text/html; charset="utf-8"\n\n' + html_body + "\n"
    return headers + 'Content-Type: text/plain; charset="utf-8"\nContent-Transfer-Encoding: 8bit\n\n' + body + "\n"


def main() -> int:
    items, rendered = [], {}
    subjects_seen, ids_seen, problems = {}, set(), []
    total = sum(a["count"] for a in ARCHETYPES)
    gidx = -1

    for arch in ARCHETYPES:
        for idx in range(arch["count"]):
            gidx += 1
            ov = arch.get("overrides", {}).get(str(idx), {})
            name = ov.get("name", NAMES[gidx % len(NAMES)])
            slots = {
                "name": name,
                "prod": PRODUCTS[gidx % len(PRODUCTS)],
                "amt": AMOUNTS[gidx % len(AMOUNTS)],
                "company": COMPANIES[gidx % len(COMPANIES)],
                "days": [3, 4, 5, 6, 8, 11, 14][gidx % 7],
                "ord": f"AUR-{31000 + gidx}",
                "ord2": f"AUR-{31200 + gidx}",
                "ord3": f"AUR-{31400 + gidx}",
                "ticket": f"{5200 + gidx}",
                "short": f"{30000 + gidx}",
            }
            subject = arch["subjects"][idx].format(**slots)

            if "bodies" in arch:
                body_template = arch["bodies"][idx]
            else:
                core_count = arch.get("core_count")
                if core_count:
                    stride = arch.get("core_stride", 1)
                    cores = [arch["cores"][(idx * stride + k) % len(arch["cores"])] for k in range(core_count)]
                    opening = arch["openings"][0]
                else:
                    pairs = [(i, j) for i in range(len(arch["openings"])) for j in range(len(arch["cores"]))]
                    oi, ci = pairs[idx % len(pairs)]
                    opening, cores = arch["openings"][oi], [arch["cores"][ci]]
                if ov.get("core"):
                    cores = [ov["core"]]
                parts = [opening, *cores, arch["asks"][idx % len(arch["asks"])]]
                if ov.get("core_append"):
                    parts.append(ov["core_append"])
                body_template = "\n\n".join(parts) + "\n\n" + CLOSINGS[idx % len(CLOSINGS)]
            body = body_template.format(**slots)

            fmt = ov.get("fmt", arch.get("fmt", "eml"))
            flags = list(ov.get("flags", arch["flags"])) + ov.get("flags_extra", [])
            coverage = list(arch["coverage"]) + FORMAT_TAGS[fmt] + ov.get("coverage_extra", [])
            conf = ov.get("conf", arch["confidence_floor"])
            date = ov.get("date", DATES[gidx % len(DATES)])
            date_short = DATES_SHORT[gidx % len(DATES_SHORT)]
            language = ov.get("language", arch.get("language", "en"))
            signals = arch.get("signals", []) + ov.get("signals_extra", [])

            filename = f"e{gidx + 1:03d}_{arch['key']}_{idx + 1:02d}.{FORMAT_EXT[fmt]}"
            msgid = f"<2026{gidx:04d}.{idx:02d}.e{gidx + 1}@mail.example.com>"
            addr = f"{slugify(name)}@example.{['com', 'net', 'org'][gidx % 3]}"

            if "{" in subject:
                problems.append(f"{filename}: leftover placeholder in subject")
            if subject in subjects_seen:
                problems.append(f"{filename}: duplicate subject with {subjects_seen[subject]}")
            subjects_seen[subject] = filename
            if msgid in ids_seen:
                problems.append(f"{filename}: duplicate Message-ID")
            ids_seen.add(msgid)
            missing = [s for s in signals if not re.search(s, body, re.I)]
            if missing:
                problems.append(f"{filename}: missing signal(s) {missing}")

            rendered[filename] = render(fmt, name, addr, subject, date, date_short, msgid, body)
            items.append({
                "file": filename,
                "scenario": arch["scenario"],
                "coverage": coverage,
                "classification": {
                    "sentiment": arch["sentiment"], "urgency": arch["urgency"], "priority": arch["priority"],
                    "category": arch["category"], "flags": flags,
                    "confidence_floor": conf, "notes": arch["notes"],
                },
                "schedule": {"bucket": arch["bucket"], "notes": arch["schedule_notes"]},
                "draft": {"language": language, "required_key_points": arch["key_points"], "must_not": arch["must_not"]},
                "review": {"expected_status": arch.get("review_status", "approved"), "must_check": arch["must_check"]},
                "traps": arch["traps"],
            })

    if problems:
        print(f"SELF-CHECK FAILED ({len(problems)}):")
        for p in problems:
            print("  ", p)
        return 1
    if gidx + 1 != 100:
        print(f"SELF-CHECK FAILED: expected 100 items, built {gidx + 1}")
        return 1

    EMAILS.mkdir(parents=True, exist_ok=True)
    for old in EMAILS.iterdir():
        old.unlink()
    for filename, text in rendered.items():
        (EMAILS / filename).write_bytes(text.encode("utf-8"))

    meta = {
        "method": "ToolGrad-style answer-first generation (expected chain first, email annotated to match)",
        "generated": "2026-10-11",
        "generator": "data/batch_100/generate_batch.py (deterministic, template-composed from chain specs)",
        "verifier": "data/verify_batch.py data/batch_100",
        "archetypes": [a["key"] for a in ARCHETYPES],
        "count": len(items),
    }
    EXPECTED.write_bytes((json.dumps({"_meta": meta, "items": items}, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def dist(fn):
        out: dict[str, int] = {}
        for it in items:
            k = fn(it)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    print(f"items: {len(items)} | files: {len(rendered)} | archetypes: {len(ARCHETYPES)}")
    print("sentiments:", dist(lambda i: i["classification"]["sentiment"]))
    print("urgencies: ", dist(lambda i: i["classification"]["urgency"]))
    print("buckets:   ", dist(lambda i: i["schedule"]["bucket"]))
    print("categories:", dist(lambda i: i["classification"]["category"]))
    print("formats:   ", dist(lambda i: next((t for t in i["coverage"] if t.startswith(("format:", "parser:"))), "eml-plain")))
    print("wrote:", EXPECTED.relative_to(HERE.parent.parent), "and", len(rendered), "emails")
    return 0


if __name__ == "__main__":
    sys.exit(main())
