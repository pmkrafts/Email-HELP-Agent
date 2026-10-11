# Generated Batch — ToolGrad-Style Answer-First Generation
## 20 emails + expected output chains, produced with textual-gradient verification

**Date:** 2026-10-11 · **Method source:** [ToolGrad (Google Research, Sept 2026)](https://research.google/blog/toolgrad-efficient-tool-use-dataset-generation-with-textual-gradients/) — answer-first generation + textual gradients from [TextGrad](https://arxiv.org/abs/2406.07496)
**Artifacts:** `emails/` (20 files: 17 `.eml` + 3 `.txt`) · `expected.json` (20 chains) · `verify_batch.py` (deterministic gates)
**Companion:** `data/sample_emails/` (golden set v1, 12 items) — this batch adds adversarial and edge coverage v1 does not have.

---

## 1. Method mapping (ToolGrad → this batch)

| ToolGrad module | Adaptation here |
|---|---|
| **Answer-first / chain-first** | The expected output chain is defined **before** the email: classification → schedule bucket → draft requirements → review expectation. The email is then annotated to be the query that legitimately produces that chain. Rationale from the paper: an explicit solution disambiguates the annotation step. |
| **API Proposer** | Propose which capability the next item should exercise (e.g. "injection", "sarcasm", "outside-hours critical") from the coverage matrix — items are proposed by capability, not by content. |
| **API Executors** | `verify_batch.py` — 6 gate families (parsing, schema, rule consistency, signal presence, hygiene, coverage) executed per item. |
| **API Selector / textual gradient** | A failed gate does not just say "no" — it emits a directional critique naming the cause and the required change (see §4 for the actual gradients). |
| **LLM Updater** | The item (email and/or chain) is revised to satisfy the critique; the whole batch is re-executed. |

## 2. Run log (3 rounds to green)

| Round | Action | Result |
|---|---|---|
| 1 | Chains defined first, emails annotated (20 items, coverage matrix from `04-Test` + scorecard gaps) | 20 candidates drafted |
| 2 | Execute `verify_batch.py` | **0 evaluated / parse failure** — `expected.json` malformed (gradient G1) → repaired; rerun: **20 findings** across 17 items + 1 coverage gate (gradients G2–G5) |
| 3 | Apply 4 root-cause fixes (2 to the verifier, 2 to the data) | **PASS — all gates green, 20/20 items accepted** |

Net: 2 correction rounds, 5 textual gradients, 20/20 items passing. The 20 findings in round 2 collapsed to **4 root causes** — exactly the sample-efficiency argument the paper makes: critique by cause, not per-instance.

## 3. What the gates enforce (`verify_batch.py`)

1. **Parsing** — every file parses with the stdlib; format-specific expectations (html-only yields no text part; the empty-body sample is the only empty one; `.txt` parses under both separator conventions).
2. **Schema** — all chain keys present, enums valid, **no unknown keys**, confidence floors in range.
3. **Rule consistency** (docs `06-Rules`) — `legal_threat ⇒ Critical`; `chargeback_risk ⇒ High|Critical`; `spam ⇒ Low + junk bucket`; `refund_request ≠ positive sentiment`; `empty_body ⇒ confidence ≤ 0.5`; `non_english ⇒ non-English draft language`; `policy_exception_request ⇒ refund/exception promises banned`; …
4. **Signal presence** — every claimed flag must be evidenced in the email text (legal wording, chargeback wording, injection payload, outage wording, delay wording, deadline wording, attachment reference, …). No label may be asserted without a textual basis.
5. **Hygiene** — reserved test domains only (example.com/net/org + `*.example` subdomains), unique Message-IDs, unique subjects.
6. **Coverage minimums** — all four sentiments present, ≥2 criticals, ≥5 categories, junk bucket present, an injection item, a non-English item, a long-body item (≥450 words), an html-only item.

Re-run any time: `python data/verify_batch.py data/generated_batch` (exit 0 = green).

**Post-hoc note (verifier generalisation).** The verifier was later generalised to
`data/verify_batch.py` (works on any batch directory) with two extra gates: coverage-tag-driven
format expectations and a near-duplicate body detector. Re-running it over this batch surfaced
gradient **G6**: two `.txt` files (g05, g15) carried no `format:*` coverage tag, so the batch
could not prove both separator conventions were covered — fixed by tagging them
`format:txt-blank-separator`. Everything else passed unchanged.

## 4. The actual textual gradients applied (round 2 → 3)

| ID | Gradient (critique) | Fix |
|---|---|---|
| G1 | *"`expected.json` is not parseable: the g20 draft object closes with `]` instead of `}`, and carries an undocumented `must_not_repeat` key (schema allows only `language` / `required_key_points` / `must_not`)."* | Repaired the brace; removed the key |
| G2 | *"The domain gate rejects `mail.example.com` for all 17 `.eml` files, but subdomains of the reserved `example.com` are valid test infrastructure — the gate is over-strict, not the data."* | Pattern widened to `([\w-]+\.)*example\.(com\|net\|org)` |
| G3 | *"The empty-body gate cannot distinguish a broken extraction from a deliberately empty sample — it fires on g17, whose emptiness is the point of the item."* | Gate now consults the expected `empty_body` flag before failing |
| G4 | *"g07 is flagged `non_english` but the body contains no non-ASCII characters; the item does not test what it claims to test."* | Rewritten with real Spanish orthography (`envíos`, `México`, `cuánto`, `también`, `días`) — round-1 finding I had predicted, confirmed by the gate |
| G5 | *"g18 fails the long-body gate at 341 words; summarisation and truncation paths need ≥450 words to be exercised."* | Extended to 518 words with additional interaction history |

## 5. Final coverage (verified by the gates)

| Dimension | Distribution |
|---|---|
| Sentiment | neutral 9 · negative 8 · positive 2 · angry 1 |
| Urgency | medium 7 · low 6 · high 5 · critical 2 |
| Schedule buckets | next_business_day 6 · within_24h 6 · same_business_day 5 · immediate 2 · junk 1 |
| Categories | shipping 4 · complaint 4 · billing 4 · feedback 2 · technical 2 · returns 2 · spam 1 · other 1 |
| Flags (21 distinct) | injection, legal_threat, chargeback_risk, vip, spam, outage, non_english, date_anomaly, empty_body, missing_attachment, policy_exception_request, multi_issue, repeat_contact, escalation, deadline_pressure ×2, refund_request ×3, … |
| Review expectations | approved 19 · escalate 1 (legal threat) |
| Body sizes | 31–518 words (max: g18) · confidence floors 0.30 (empty body) – 0.85 (spam) |

**Adversarial/edge inventory:** prompt injection (g03) · self-inflicted damage + baseless legal threat (g02) · sarcasm (g06) · positive polarity with refund keyword (g05) · urgency keyword with low stakes (g01) · VIP with trivial content (g08) · critical on a Saturday night — forces the business-hours vs critical rule conflict (g09) · attachment claimed but absent (g13) · chargeback threat (g14) · future Date header (g19) · policy-exception pressure (g20) · HTML-only (g12) · empty body (g17) · `.txt` in both separator conventions (g11 vs g05/g15).

## 6. Known tensions this batch exposes (by design)

- **g09** — `06-Rules` says critical ⇒ ≤1 hour *and* outside hours ⇒ next business morning. The chain expects **immediate** (never defer an outage-critical), which forces that ambiguity to be resolved in `rules.yaml`
- **g01** — the +2 "urgent keyword" boost would lift a low-stakes request; the chain expects Medium, so the boost must be conditional on stakes, not keyword alone
- **g11** — docs `07` show the `.txt` `---` separator, batch v1 uses a blank line; the parser must accept both (the verifier enforces both parse)
- **g19** — Date headers must never drive SLA math (a 2027 header must not schedule into the future)

## 7. Reusing the loop (for JEV / OpenAI later)

To generate batch v2 with real models, feed these prompts (condensed here; full chains in `expected.json`):

1. **Proposer** — *"Given the coverage matrix (sentiment × urgency × category × trap), propose N capabilities the batch lacks; output one capability tag per item."*
2. **Chain-first generator** — *"For capability `<tag>`, first write the expected chain JSON (schema: expected.json), then write the `.eml`/`.txt` email that legitimately produces it. The email must contain the textual signals for every flag you claim."*
3. **Verifier** — run `verify_batch.py`, then feed every `[GRADIENT]` line back verbatim.
4. **Updater** — *"Apply each gradient. Change the minimum text needed; do not remove the capability."*
5. Repeat until exit 0; log rounds and gradients here.

This keeps the loop deterministic at the gate layer (free, instant) and reserves model calls for generation and critique — the cost profile the ToolGrad paper reports for its own pipeline.
