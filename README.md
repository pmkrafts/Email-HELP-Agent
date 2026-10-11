# Email-HELP-Agent — Agentic AI for Email Automation

Deterministic-first POC of a multi-agent support system: **parse → classify → retrieve → draft →
review → schedule**, with an optional LLM refinement layer behind a provider seam.
**No email is ever sent** — results are persisted with a scheduled dispatch time only.

Status: working POC (assignment scope, 3 days). Zero third-party dependencies — Python 3.11+ stdlib only.

---

## Quick start (no install, no API key)

```bash
python run.py                     # one command: tests -> pipeline -> agreement eval
```

or drive the CLI directly:

```bash
python --version                                            # 3.11+
python email-agent.py run --input data/sample_emails --now 2026-10-12T09:00:00+00:00
python email-agent.py run --input data/batch_100            # 100-email batch (~0.5 s offline)
python email-agent.py view --priority Critical
python email-agent.py summary
python email-agent.py eval --show-misses                    # score against all labelled batches
python -m unittest discover -s tests                        # 33 tests
```

See [HOW-TO-RUN.md](HOW-TO-RUN.md) for the full guide (options, troubleshooting, expected output).

`pip install -e .` exposes the same commands as `email-agent`.

## Pipeline

```
input dir ──► Parser ──► Classifier ──► Retriever ──► Drafter ──► Reviewer ──► Scheduler ──► SQLite
             (.eml/.txt)  (rules+LLM)   (KB, top-k)   (grounded)  (+max-2      (business
                                                                  rewrite loop) hours)
```

- **Orchestrator** (`src/email_agent/orchestrator.py`): sequential pipeline over a shared
  `AgentState`; per-email error isolation (dead-letter list); idempotent by `message_id`.
- **Classifier**: rules engine first (always available offline); optional LLM refinement never
  invents signals. Spam short-circuits to the junk queue — no reply is drafted.
- **Retriever**: dependency-free keyword/IDF retrieval over `data/knowledge_base/` (6 policy/FAQ/past-reply docs).
- **Drafter**: policy-grounded templates in EN/ES/DE/FR that never promise beyond the knowledge base.
- **Reviewer**: deterministic policy bans (refund/legal/internal promises) + confidence gates +
  rewrite loop (max 2, then escalate). Legal threats always escalate to a human.
- **Scheduler**: business hours Mon–Fri 09:00–18:00 (configurable); criticals are queued
  immediately regardless of hours; spam/junk and escalations get no send time.

## Prioritization logic

```
priority_score = urgency_base·0.5 + sentiment_weight·0.3 + tier·0.2 + boosts (capped at +2.0)
Bands: Critical ≥ 8 · High ≥ 6 · Medium ≥ 4 · Low < 4
```

All scales, boosts, urgency floors, business hours and thresholds are **pinned in
[`config/rules.json`](config/rules.json)** (the POC's answer to the previously undefined formula units).
Policy overrides applied after banding: `legal_threat ⇒ Critical`, `spam ⇒ Low`.
Signals detected from the text: refund requests, chargebacks, outages, legal language, delays,
damage, deadlines, VIP/commercial accounts, repeat contact, multi-issue, non-English, spam,
prompt injection, policy-exception requests, low-priority language.
Parser-derived signals count too: empty body / very short body ⇒ `missing_info`, clock-skewed
headers ⇒ `date_anomaly`.

**Prompt-injection handling:** sentences carrying injection payloads are stripped before signal
detection, so payload text cannot influence priority; the attempt itself is flagged, never followed.

## Evaluation (deterministic mode)

`python email-agent.py eval --now 2026-10-12T09:00:00+00:00`

| Batch | n | Priority agreement* | Sentiment | Urgency | Buckets |
|---|---|---|---|---|---|
| `data/sample_emails` (golden set) | 12 | **92%** | 75% | 92% | – |
| `data/generated_batch` (adversarial) | 20 | **100%** | 85% | 85% | 90% |
| `data/batch_100` (machine-generated) | 100 | **94%** | 72% | 92% | 86% |

\* Acceptance criterion from the test plan: ≥ 80% priority agreement. Flags: precision 0.65 /
recall 0.89 on batch_100. Confidence-floor compliance is 50–65% — the rules engine is deliberately
more conservative than the label authors' expectations (see the local scorecard for the gap list).

## Model routing — JEV + GPT together

The pipeline is a cost ladder: premium spend scales with the *disagreement rate*, not with volume.

| Tier | Engine | Cost profile |
|---|---|---|
| 0 | deterministic rules | zero tokens — the default, and how the accuracy numbers above were achieved |
| 1 | cheap judge — **JEV `jev-1.13`** (any OpenAI-compatible endpoint) | one cheap call per email; verifies the proposal and the draft |
| 2 | strong model — **OpenAI GPT** | only where it buys something: drafting, plus adjudication when the judge disagrees |

| Pairing | Config | Calls per email (happy path) |
|---|---|---|
| Rules only | default | 0 |
| Rules + JEV | `--with-judge` | 1 cheap judge call; JEV corrects directly when no strong model is configured |
| **GPT + JEV, cost-first (recommended)** | `EMAIL_AGENT_REFINE_MODE=escalations_only` + both models | 1 JEV judge + 1 JEV draft-review + 1 GPT draft; **0 GPT classification calls** |
| GPT + JEV, verify-everything | `EMAIL_AGENT_REFINE_MODE=always` (default) | adds 1 GPT classification per email, which JEV then verifies |

Judgement is deliberately cross-provider: JEV never shares weights with the model it is checking, so
"agreement" is information rather than self-confirmation. On disagreement, *only that email* is
escalated to GPT for adjudication.

```bash
# GPT as the strong model, JEV as the judge (two gateways; one shared key is fine)
EMAIL_AGENT_LLM=openai
EMAIL_AGENT_BASE_URL=https://api.openai.com/v1
EMAIL_AGENT_MODEL=gpt-4o-mini
EMAIL_AGENT_API_KEY=<openai key>

EMAIL_AGENT_JUDGE_LLM=jev
EMAIL_AGENT_JUDGE_BASE_URL=https://<your-gateway>/v1
EMAIL_AGENT_JUDGE_MODEL=jev-1.13          # default; _API_KEY falls back to EMAIL_AGENT_API_KEY

EMAIL_AGENT_REFINE_MODE=escalations_only   # GPT classifies only when JEV disagrees
python run.py --with-judge
```

Measured on the 12-email golden set with both gateways live (mock endpoints, same code path):
GPT = 11 drafts + 1 adjudication, JEV = 12 judge calls + 10 draft reviews, verdicts
**11 agreed / 1 adjudicated**, 0 failures.

Every tier degrades safely: a judge outage leaves the rules baseline untouched (`judge=error`),
and validity is enforced on all judge output. Batch summaries report
`agreed / overridden / adjudicated / error` counts; behaviour is tested against mock endpoints in
`tests/test_judge.py` (including the per-gateway call-attribution contract above).

## Data & verification

| Path | Contents |
|---|---|
| `data/sample_emails/` | 12 hand-written golden emails + `ground_truth.json` |
| `data/generated_batch/` | 20 hand-authored adversarial emails + `expected.json` + `REPORT.md` |
| `data/batch_100/` | 100 generated emails + `expected.json` + generator + `REPORT.md` |
| `data/knowledge_base/` | 6 policy/FAQ/past-reply docs for retrieval |
| `data/verify_batch.py` | deterministic gates: schema, rule consistency, signal presence, hygiene, coverage |

```bash
python data/verify_batch.py data/batch_100        # exit 0 = all gates green
```

Every batch passes its gates; sample data is synthetic (RFC 2606 domains), no real customer data.

## Layout

```
email-agent.py          # zero-install launcher
src/email_agent/        # parser, rules, agents, orchestrator, storage, retrieval, CLI, evaluation
config/                 # rules.json (pinned scales/policies) + prompts/
tests/                  # unittest suite (parser edge cases, rules, end-to-end pipeline)
data/                   # labelled batches, knowledge base, batch verifier
storage/                # runtime SQLite + JSONL logs (gitignored)
```

Design documentation (14-document suite: PRD, TRD, architecture, rules, schemas, test plan,
security, memory) is kept alongside the project in `MY_DOCS/email-agentic-automation/` and is
packaged into `docs/` for submission.
