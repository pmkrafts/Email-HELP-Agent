# Batch 100 — ToolGrad-Style Answer-First Generation at Scale
## 100 emails + expected chains, generated deterministically and gated by the shared verifier

**Date:** 2026-10-11 · **Method:** [ToolGrad](https://research.google/blog/toolgrad-efficient-tool-use-dataset-generation-with-textual-gradients/) (answer-first + textual gradients), applied as code instead of prose
**Artifacts:** `emails/` (100 files) · `expected.json` (100 chains) · `generate_batch.py` (engine) · `content_a.py` + `content_b.py` (22 archetype specs)
**Verifier:** `python data/verify_batch.py data/batch_100` → **PASS, exit 0 (100/100)
**Family:** `data/sample_emails/` (12 hand-written golden set) + `data/generated_batch/` (20 hand-authored adversarial) + this batch (100 machine-composed) = **132 labeled samples**

---

## 1. Why a generator (and not 100 hand-written emails)

At 20 items, the ToolGrad loop ran on hand-authored content with a prose report. At 100+, the
*chain-first* step becomes the generator's contract: for every archetype the **expected chain is
declared as data** (sentiment, urgency, priority, flags, bucket, draft requirements, review
expectation), and the email is rendered from slot-filled blocks chosen so that the chain is
*provably* produced — the engine refuses to write a file whose required signal words are absent.

| ToolGrad module | At this scale |
|---|---|
| Answer-first | Archetype spec = chain; `generate_batch.py` renders the matching email |
| Proposer | Human/agent designs the archetype's coverage tag + trap; 22 archetypes total |
| Executors | Two layers: engine self-checks (pre-write) + `data/verify_batch.py` gates (post-write) |
| Selector → textual gradient | Every failed gate prints a `[GRADIENT]` line naming cause and required change |
| Updater | Archetype blocks / engine / verifier are revised; batch regenerated deterministically |

Zero LLM calls were needed for the loop itself: the executors are deterministic, so a full
regenerate + verify cycle costs milliseconds.

## 2. Run log (4 rounds to green)

| Round | Executor | Finding(s) | Gradient applied |
|---|---|---|---|
| 1 | engine self-check | 7 instances lost a required signal because the selected block combination did not contain it (late_delivery ×3, duplicate_charge ×1, long_wall ×1, address_change ×2) | **Anchor signals in every selectable block.** Opening/core texts were edited so the required wording exists in the opening *or* every core that can be picked — e.g. "is now late" moved into the shared opening, "duplicate" added to the blocks reachable by rotation. Also: 3 pre-run wording fixes (injection payload, chargeback core, long-wall signals) found by inspection |
| 2 | verifier | 9 `.txt` files extracted **empty bodies** → every flag-signal gate fired | **CRLF, not the content.** Python text-mode writes on Windows emitted `\r\n`, so the verifier's `\n---\n` / `\n\n` splits missed. Fixed on both sides: the generator now writes LF bytes; the verifier normalises CRLF before splitting — the real project parser must tolerate CRLF anyway, so this is a spec-strengthening, not a workaround |
| 3 | verifier | long-body gate: max 332 words < 450 | **Deepen the long-form archetype.** `core_count` 3 → 4 with stride 2 (better rotation diversity) |
| 4 | verifier | long-body gate: max 410 words < 450 | **Lengthen the blocks.** Each long-wall block extended by ~20 words of substance (order-confirmation detail, bank reference, call-back failure, time cost) → 495 words |

## 3. What the batches enforce together

`data/verify_batch.py` (shared by all batches) runs six gate families: parsing, schema,
rule-consistency (docs `06-Rules`), signal presence per flag, hygiene (reserved domains, unique
Message-IDs/subjects, **near-duplicate body detector at ratio > 0.93**), and coverage minimums
(all sentiments, ≥2 criticals, ≥5 categories, junk bucket, injection, non-English, spam, empty
body, ≥450-word body, html-only, both `.txt` separators, date anomaly).

Measured on this batch: **max pairwise body similarity 0.81** (`long_wall_01` vs `long_wall_03`, the
longest texts) — comfortably below the clone threshold; distinct flags: **21**.

## 4. Coverage (verified)

| Dimension | Distribution (100 items) |
|---|---|
| Sentiment | negative 50 · neutral 38 · positive 9 · angry 3 |
| Urgency | high 36 · medium 35 · low 23 · critical 6 |
| Buckets | same_business_day 36 · within_24h 32 · next_business_day 20 · immediate 6 · junk 6 |
| Categories | shipping 22 · billing 16 · returns 15 · complaint 15 · feedback 9 · spam 6 · account 5 · sales 5 · other 4 · technical 3 |
| Formats | `.eml` plain 80 · `.txt` dash 8 · `.txt` blank 8 · html-only 2 · future-dated 1 · empty body 1 |
| Body size | 0–495 words (avg 68) |
| Top flags | refund_request 20 · delivery_delay 10 · repeat_contact 8 · chargeback_risk 6 · invoice_request 6 · spam 6 · deadline_pressure 5 · non_english 5 |

## 5. Archetype catalogue (22 specs, 100 instances)

| Archetype | n | Trait |
|---|---|---|
| faulty_item_refund | 7 | refund flag, three fault narratives, txt variants |
| late_delivery | 7 | stalled tracking, expedite/cancel options |
| duplicate_charge | 6 | two instances escalate to chargeback |
| invoice_request | 6 | html-only, txt dash, txt blank, **future-dated** variants |
| login_problem | 5 | account access, one txt variant |
| product_question | 5 | pre-sales; html + txt variants |
| positive_feedback | 6 | emoji variant; txt variants; no action |
| vip_bulk_issue | 4 | commercial stakes (not the trivial-VIP trap — that lives in v1 g08) |
| legal_threat | 3 | Critical + escalate, no liability admission |
| spam | 6 | full fixed bodies (crypto, SEO, lottery, refund bait, loan, investment) |
| prompt_injection | 4 | four distinct payloads (fake SYSTEM, developer mode, priority override, prompt leak) |
| cancel_chargeback | 4 | two with explicit deadlines (Monday/Friday) |
| damage_missing_attachment | 4 | photos claimed but absent |
| policy_exception | 4 | self-inflicted damage, pressure for an exception |
| long_wall | 4 | 450–495 words, 4 rotating issue blocks, top-2 rule |
| non_english | 5 | ES/ES/DE/DE/FR, accented bodies |
| vague_short | 4 | vague ×2, short ×1, **empty body ×1** |
| sarcastic | 3 | surface-praise with negative intent |
| outage | 3 | Critical, late-night/weekend timestamps |
| repeat_contact | 4 | fourth/third follow-ups + supervisor request |
| address_change | 3 | "urgent" framing, dispatch-window constraint |
| positive_refund_thanks | 3 | 'refund' keyword with positive polarity (no refund workflow) |

## 6. Fidelity notes (what this batch is and is not)

- Slot reuse is deliberate and documented: 24 names, 14 products and 8 amounts cycle across
  instances, so sample emails read as a consistent synthetic world rather than 100 distinct lives.
  Cross-instance similarity stays ≤ 0.81 — no template clones.
- The batch is **chained to the same rule system** as v1: priorities, buckets and review
  expectations follow `06-Rules` conventions, so both sets can be scored by the same harness.
- Known open item it inherits: the priority-score scales (`urgency_base` etc.) are still undefined
  in `rules.yaml`; labels here are band-level judgments, same as v1 (`REQUIREMENTS-SCORECARD` P0-2).

## 7. Reuse

1. Add or edit an archetype in `content_a.py` / `content_b.py` (count, chain fields, blocks,
   overrides).
2. `python data/batch_100/generate_batch.py` — engine self-checks run before anything is written.
3. `python data/verify_batch.py data/batch_100` — gates; any `[GRADIENT]` line names the fix.
4. Repeat until exit 0; the generator is deterministic, so the same input always reproduces the
   same batch.
