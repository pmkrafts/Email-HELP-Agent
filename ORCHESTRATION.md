# Agent orchestration strategy · prioritization & response generation logic

This document answers the two written submission items of the brief:

1. **Explanation of agent orchestration strategy** — how the agents are split, how they coordinate, how
   failures and model tiers are handled (§1).
2. **Email prioritization and response generation logic in detail** — the exact scoring rules and the
   drafting/review pipeline (§2 and §3).

Everything below is implemented in the repo; each claim names the file and function that owns it, and
§4 lists the commands that reproduce the numbers. Nothing is ever sent — the schedule is the output.

---

## 1. Agent orchestration strategy

### 1.1 Shape: a state graph, not a prompt chain

The orchestrator is a **LangGraph `StateGraph`** compiled once in `GraphOrchestrator.__init__`
(`src/email_agent/graph.py`). One invocation handles one email; `run_batch()` parses the input
directory and invokes the graph per email. The graph is the *only* place control flow lives — the
agents themselves are plain Python classes with a `run(...)` method, so they can be unit-tested
without the framework (see `tests/test_drafter.py`, `tests/test_judge.py`, `tests/test_rules.py`).

| Node | Agent / step | Produces |
|---|---|---|
| `classify` | `Classifier` (`agents/classifier.py`) | sentiment, urgency, priority, category, confidence, signals, judge verdict |
| `retrieve` | `KnowledgeBase.retrieve(query, top_k=3)` (`retrieval.py`) | top-3 knowledge chunks (RAG) |
| `draft` | `Drafter` (`agents/drafter.py`) | reply text, confidence, tone, language, citations, key points |
| `review` | `Reviewer` (`agents/reviewer.py`) | `approved` / `needs_revision` / `escalate` + issues |
| `revise` | `Drafter.run(state, feedback=…)` | a rewritten draft (version + 1) |
| `finalize_review` | `Reviewer` terminal rule | final review status (escalate when the cap is hit) |
| `schedule` | `Scheduler` (`agents/scheduler.py`) | `send_at`, bucket, reason |
| `persist` | `Repository.save_state` (`storage.py`) | rows in SQLite + JSONL log line |

### 1.2 State and its lifecycle

State is a plain `PipelineState` **TypedDict** (serialisable through msgpack so LangGraph can
checkpoint it); the richer dataclasses (`ParsedEmail`, `Classification`, `DraftResponse`,
`ReviewResult`, `ScheduleDecision`) are reconstructed inside each node by `_from_state()` and written
back with `_dump()`. Alongside the dataclasses the state carries three control fields: `attempts`
(starts at 1), `feedback` (the last reviewer feedback), and `now` (the scheduling clock, fixed per
batch so runs are reproducible).

### 1.3 The three branches

```
START → classify ─(spam)→ schedule
        classify → retrieve → draft → review ─(needs_revision, attempts < max_rewrites, client present)→ revise → review
                                    review ─(otherwise)→ finalize_review → schedule → persist → END
        <any node raises> ─halt→ END            (that email only; the batch continues)
```

- **Spam short-circuit** (`_after_classify`): a `spam` flag routes straight to `schedule` — junk queue,
  no retrieval, no draft, no review. A customer reply is never generated for junk.
- **Rewrite loop** (`_after_review`): `review → revise → review`, gated on three conditions — status is
  `needs_revision`, a model client exists (the offline template drafter cannot rewrite), and
  `attempts < settings.max_rewrites` (= 2, from `config/rules.json: thresholds.max_rewrite_loops`).
  The `revise` node re-drafts with `feedback = review.suggested_changes or "; ".join(review.issues)`
  and increments `attempts`.
- **Error isolation** (`_guard` + the `halt` routers): every node is wrapped; an exception becomes
  `{"status": "error", "error": "Type: message"}` and the email routes to `END`. The batch is never
  aborted, and a failing node's error is logged in that email's trace. Verified by
  `tests/test_graph_engine.py::test_node_failure_is_isolated_per_email`: with a deliberately broken
  drafter over the 12-email golden set, 11 emails end `error` and the spam email still schedules.

### 1.4 Batch semantics

`run_batch()` → `parse_dir()` → per email: skip if already stored, else `process()`.
Parse failures are returned as a separate `failures` list (file, reason) so a malformed file is
reported, not crashed on. Idempotency comes from `message_id` (the `Message-ID` header, or a synthetic
`eml:<file>` / `txt:<file>` when missing): re-running skips processed emails, `--force` re-processes
and replaces the rows. Ordering is input order, execution is sequential — deliberate, because a
deterministic run is worth more than throughput in a review tool (and model latency dominates anyway).

### 1.5 Model-tier orchestration *inside* the agents

The graph routes work; the agents decide how much model to spend. Three tiers, in cost order:

| Tier | Where | Behaviour |
|---|---|---|
| 0 rules | `Classifier` (always) | lexicon/pattern scoring, zero tokens, always available offline |
| 1 judge | `Classifier`, `Reviewer` via `judges.py` | cheap typed-question verification (JEV / any OpenAI-compatible endpoint) |
| 2 primary | `Classifier` refinement, `Drafter` | premium drafting and adjudication of judge disagreements |

`EMAIL_AGENT_REFINE_MODE` = `always` (refine every email) or `escalations_only` (spend the primary
model only on drafting and on disagreements). Every tier failure degrades to the tier below — a judge
outage records `judge: unavailable (…)` in the classification reasoning and the batch continues.
The judge is consulted for a draft review only when the deterministic reviewer already approved it
(`Reviewer.run`: the judge can only *raise* severity, never clear a policy ban).

### 1.6 Persistence, observability, durability

- `persist` writes to SQLite (`emails`, `classifications`, `drafts`, `reviews`, `schedules`,
  `agent_logs`) and `RunLogger` appends one JSONL line per email to `storage/logs/`.
- Every node appends a trace entry (`agent`, `event`, `latency_ms`, details), which is what the UI's
  **Agent trace** tab renders under human labels (`TRACE_LABELS` in `report.py`).
- `EMAIL_AGENT_GRAPH_CHECKPOINT=1` compiles the graph with an `InMemorySaver` and keys each run by
  `thread_id = message_id`; `GraphOrchestrator.snapshot(message_id)` reads the durable state back.
  That is the hook for human-in-the-loop resume (pause an escalated case, resume after a reviewer
  edits) — the topology is already shaped for `interrupt()`.

### 1.7 Why this design

- **Deterministic-first, model-optional**: the offline path is a first-class product, not a stub — the
  accuracy numbers were produced with it, and it costs zero tokens.
- **Explicit branches over implicit behaviour**: spam, rewrite and failure handling are graph edges,
  so the topology can be exported (`python email-agent.py graph`), reviewed and tested as data
  (`docs/graph.mmd`).
- **Framework at the control-flow layer only**: agents stay plain, mockable classes; the graph adds
  routing, isolation and checkpointing, not logic.

---

## 2. Email prioritization logic

### 2.1 Signals (`rules.py: FLAG_PATTERNS`, `TOPIC_PATTERNS`, plus parser-derived flags)

Detected by regex over subject + body (case-insensitive, `FLAG_PATTERNS`): `legal_threat`,
`chargeback_risk`, `refund_request`, `outage`, `cancellation`, `delivery_delay`, `damage_claim`,
`deadline_pressure`, `attachment_reference`, `repeat_contact`, `escalation`, `vip`, `spam`,
`prompt_injection_attempt`, `low_priority_language`, `policy_exception_request`.
Derived flags: `multi_issue` (≥ 3 distinct `TOPIC_PATTERNS` match), `non_english` (language hints),
plus parser-derived `empty_body`, `missing_info` (body < 8 words, or a vague request < 40 words without
an order ID) and `date_anomaly` (future dates). Injection markers are detected on the raw text and
stripped before scoring (`strip_injected`), so injected instructions cannot drive the score.

### 2.2 Sentiment (`detect_sentiment`)

Weighted lexicon hits with simple inflection matching, over text with closings removed:
`neg = 2·strong_neg + weak_neg + negation_phrases`, `pos = 2·strong_pos + weak_pos`. Then a
precedence ladder: sarcasm markers near a delay → `negative`; a legal phrase, ≥ 2 anger markers, or an
anger marker with ≥ 2 strong negatives → `angry`; else the sign of `neg − pos`; else `neutral`.

### 2.3 Urgency

Starts at `low` for positive sentiment, otherwise `medium`, then raised by the per-signal **floors**
in `config/rules.json: urgency_floor` (legal threat / outage → `critical`; chargeback risk / delivery
delay / repeat contact / multi-issue / cancellation → `high`; refund / damage / deadline / non-English
→ `medium`; missing info / spam / policy exception / low-priority language → `low`). Two refinements:
a refund request inside a billing frame (charged/billed/statement/card/bank) is raised to `high`; pure
praise never escalates (`HARD_FLAGS` excepted).
`ABSOLUTE_LOW_FLAGS` (spam, missing info, low-priority language) force `low` **and zero the boost**.

### 2.4 Score, bands, overrides (`score_email`)

```
priority_score = urgency_base·0.5 + sentiment_weight·0.3 + customer_tier·0.2 + min(Σ boosts, 2.0)

urgency_base     low 2.5 · medium 5.5 · high 7.0 · critical 9.0
sentiment_weight positive 1.0 · neutral 3.0 · negative 6.5 · angry 8.5
customer_tier    standard 2.0 · vip 2.0   (VIP is a +1.0 boost instead, so it is not double-counted)
boosts           legal_threat 3.0 · refund_request/outage/delivery_delay/multi_issue/repeat_contact 2.0
                 deadline_pressure/damage_claim 1.5 · vip 1.0 · cap_total 2.0
bands            Critical ≥ 8.0 · High ≥ 6.0 · Medium ≥ 4.0 · Low < 4.0
overrides        legal_threat ⇒ Critical · spam ⇒ Low   (applied after banding)
```

**Worked example** (the golden set's legal threat, reproduced in the UI as `Priority score 9.45`):
signals `legal_threat` + `refund_request`; urgency floored to `critical` (9.0), sentiment `angry` (8.5);
`4.5 + 2.55 + 0.4 + min(3.0 + 2.0, 2.0) = 9.45` → band `Critical`, override keeps it Critical,
review escalates (legal threats are never answered automatically).

### 2.5 Confidence and the human gate

```
confidence = 0.6 + 0.04·min(n_signals, 5) + 0.15 (if |neg − pos| ≥ 2), capped at 0.92
spam                      → at least 0.90
body < 8 words            → at most 0.35
prompt_injection_attempt  → at most 0.70
```

A classification below `thresholds.min_classification_confidence` (0.4) adds a "route to human triage"
suggestion; the reviewer also gates on draft confidence ≥ `human_review_confidence` (0.65). The score
itself, the confidence and the full reasoning string (`sentiment=… lex neg=…; urgency=…; base=…;
boost=…; signals=…`) are stored per email and shown in the UI's Classification tab.

### 2.6 Measured accuracy

`python email-agent.py eval` scores the rules engine against the labelled batches (acceptance bar ≥ 80 %
on priority):

| Batch | n | priority | sentiment | urgency |
|---|---|---|---|---|
| `data/sample_emails` (golden) | 12 | **92 %** | 75 % | 92 % |
| `data/generated_batch` (adversarial) | 20 | **100 %** | 85 % | 85 % |
| `data/batch_100` | 100 | **94 %** | 72 % | 92 % |

Priority is the calibrated gate; sentiment is deliberately conservative on mixed-tone emails, which is
why the review agent (not the classifier) is the last word before anything is scheduled.

### 2.7 Downstream effect of priority

Priority + review status decide dispatch (`decide_schedule`): `Critical` → `now + 1 h`, deliberately
ignoring business hours; `High` → within 4 h if that lands inside Mon–Fri 09:00–18:00, else the next
business window; `Medium` → next business window; `Low` → next business day. `junk` (spam) and
`escalated` get **no** send time, and `needs_revision` parks the email as `pending_human`.

---

## 3. Response generation logic

### 3.1 Inputs

The draft is a function of: the parsed email, the classification (priority, sentiment, urgency,
signals, language), the top-3 retrieved knowledge chunks, the case facts extracted from the email, the
tone table (`rules["tone"][sentiment]`), and the previous review feedback when it is a revision.

### 3.2 Offline path (`Drafter._template_draft`) — deterministic, policy-bounded

Composition order: language-appropriate greeting (`en/es/de/fr`) → signal-matched acknowledgement
(delay apology, damage claim, refund policy, …) → extracted facts (order references uppercased, a
deadline-sensitivity sentence, a photo-retention note for damage claims) → policy-grounded body drawn
from the knowledge base → next steps → signature. It cites the knowledge chunks it used and reports
its confidence honestly: **0.62** baseline, 0.50 for non-English templates, capped at 0.55 when the
classification confidence is low. It never writes a promise the policy validator would flag.

### 3.3 Model path (`Drafter._llm_draft`)

With `EMAIL_AGENT_LLM=openai` the primary model drafts from `config/prompts/drafter.md`, grounded by
the same retrieved context and facts; the reply comes back as validated JSON (`text`, `confidence`,
`language`, `tone_used`, `key_points_addressed`) and is turned into a `DraftResponse` with the chunk
citations attached. Any failure falls back to the offline template, so drafting never blocks a batch.
Live measurements on the golden set: GPT drafts score 0.90–1.00 confidence and pass review.

### 3.4 Review gate (`Reviewer.run`) — the policy layer

Runs deterministically on every draft, in this order:

1. **Policy bans** (regex, `POLICY_BANS`): promise language, instant-refund wording, refund
   commitments without approval, legal content in an automated reply, internal-information disclosure
   (system prompt / API key / internal policy), blaming language → each match is an `issue`.
2. **Confidence and quality suggestions**: draft confidence below 0.65, non-English offline template,
   a body that contained injected instructions → `suggested_changes` (fed to the rewrite loop).
3. **Escalation rules**: a `legal_threat` flag escalates unconditionally ("automated final replies are
   prohibited"); a classification confidence below 0.4 asks for human triage.
4. **Judge second opinion**: only when the draft would otherwise be approved, the judge tier reviews it
   against the retrieved knowledge and can raise it to `needs_revision` or `escalate`.

Status precedence: `escalate` > `needs_revision` > `approved`. `final_confidence` is
`min(draft confidence, classification confidence)` — the reply is only as trustworthy as the weakest
input.

### 3.5 Rewrite loop and escalation

`needs_revision` + a model client + `attempts < 2` → `revise` re-drafts with the reviewer's feedback
(the `feedback` field is carried in the graph state). Otherwise `finalize_review` is the terminal rule:
with a client it escalates ("max rewrite attempts reached; escalated to human review"); without one
(offline template drafter) the draft keeps `needs_revision`, gains the note *"offline template drafter
cannot auto-revise; enable the GPT tier or edit the draft manually"*, and the scheduler parks it as
`pending_human`. Observed in `tests/test_graph_engine.py::test_rewrite_loop_revises_once_then_escalates`:
draft version 2, review `escalate`, schedule `escalated`, 3 model calls (refine + draft + revise).

### 3.6 Guardrails, end to end

- Injected instructions are stripped before scoring and flagged (`prompt_injection_attempt`); the
  reviewer explicitly checks that the reply ignores them (`tests/test_pipeline.py::test_draft_ignores_injection`).
- No automated reply may contain legal content, refund commitments or internal details.
- Every draft carries confidence, tone, language, citations and key points addressed — the reviewer,
  the UI and the JSON export all show them.
- **Nothing is transmitted**: there is no SMTP/API-send code in the repository; a `schedules` row with
  `send_at`, `status` and `reason` is the final artifact.

---

## 4. Reproducing everything in this document

```bash
python run.py                                        # tests → pipeline → agreement eval
python email-agent.py eval --show-misses             # the priority/sentiment/urgency table (§2.6)
python email-agent.py view --priority Critical       # the ranking in action; the 9.45 score itself is
                                                     # on the UI Classification tab (score + reasoning)
python email-agent.py graph --out docs/graph.mmd     # the orchestration topology (§1.3)
python email-agent.py run --input data/batch_100     # 100-email batch, 0 failures
python -m unittest discover -s tests                 # 71 tests incl. the branch and isolation cases
EMAIL_AGENT_GRAPH_CHECKPOINT=1 python email-agent.py run --input data/sample_emails   # §1.6
```

In the Streamlit console the same facts are on screen: the metrics row (priority mix, average
classification and draft confidence, buckets), the Classification tab (score, confidence, signals,
reasoning), the Retrieved context tab (RAG), the Draft response tab (citations, tone, language), the
Review & schedule tab (issues, `send_at`, reason) and the Agent trace tab (per-node status + latency).
