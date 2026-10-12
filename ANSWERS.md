# ANSWERS — the assignment requirements, point by point

**Email-HELP-Agent**: an agentic pipeline that reads a batch of customer emails, prioritizes them,
drafts replies, reviews them and records a dispatch time. **No email is ever sent** — the schedule is
the output (verified: no `smtp`/`sendmail`/`sendgrid`/API-send call exists anywhere in `src/`, `ui/`).

This branch runs the pipeline as a **LangGraph state graph** (`src/email_agent/graph.py`); the model
layer is optional and off by default, so the whole thing runs offline with zero tokens.

```bash
pip install -r requirements.txt
python run.py                      # env check → 71 tests → 12-email batch → agreement eval
pip install streamlit && streamlit run ui/app.py     # the same pipeline in a browser console
```

| # | Requirement | Answer | Section |
|---|---|---|---|
| 1 | Read a batch of `.eml` / `.txt` | stdlib MIME parser, both formats, batch directory | §1 |
| 2 | Prioritize by sentiment / urgency | rules scoring pinned in `config/rules.json`, LLM refinement + judge channel | §2 |
| 3 | Draft personalized responses | policy-grounded drafting in EN/ES/DE/FR offline, GPT tier when configured | §3 |
| 4 | Schedule for dispatch (no sending) | business-hours scheduler, four buckets, SQLite only | §4 |
| 5 | Optional: sub-agents | Classifier · Drafting · Review (+ Retrieval, Scheduler, Orchestrator) | §5 |
| 6 | Any LLM/SLM, local or API | OpenAI-compatible seam, provider-agnostic, offline default | §6 |
| 7 | Optional RAG | keyword/IDF retrieval over 6 knowledge-base documents, citations in the draft | §7 |
| 8 | Simple UI or CLI | Streamlit console + CLI, confidence scores at every stage | §8 |

---

## 1. Read a batch of incoming customer emails (`.eml` / `.txt`)

**Answer.** `src/email_agent/parser.py` parses a directory of files with the Python standard library
(`email` + MIME): `.eml` with headers/HTML/plain bodies, and the `.txt` format from the design docs
(both separator conventions). It normalises CRLF, handles non-UTF-8 encodings, strips HTML to text,
records parser notes instead of failing, and falls back to a synthetic `message_id` when the header is
missing. Unparsable files become a `failures` list — one bad file never stops the batch.

**Proof.**

```bash
python email-agent.py run --input data/sample_emails
# processed 12  failed 0   (three bundled batches: 12 golden / 20 adversarial / 100 generated)
python email-agent.py run --input data/batch_100
# processed 100  failed 0
```

## 2. Prioritize emails based on sentiment and urgency

**Answer.** Two layers, deterministic first:

- **Rules engine** (`src/email_agent/rules.py`) extracts signals (legal threat, refund request,
  outage, delivery delay, damage claim, VIP, deadline pressure, repeat contact, multi-issue, spam,
  prompt injection, language) and scores them:
  `score = urgency·0.5 + sentiment·0.3 + tier·0.2 + boosts (capped at +2.0)`.
  Every scale is pinned in `config/rules.json`: urgency 2.5/5.5/7.0/9.0, sentiment 1.0/3.0/6.5/8.5,
  bands `Critical ≥ 8`, `High ≥ 6`, `Medium ≥ 4`, per-signal boosts, urgency floors, and policy
  overrides applied after banding (`legal_threat ⇒ Critical`, `spam ⇒ Low`).
- **Optional model layer** (`agents/classifier.py` + `judges.py`): a cheap judge channel verifies the
  proposal and a primary model refines it; neither may invent signals, and a judge outage degrades to
  the tier below rather than failing.

**Proof.** Priority agreement against the labelled batches (acceptance bar ≥ 80%):

```
data/sample_emails   (n=12)   priority 92%   sentiment 75%   urgency 92%
data/generated_batch (n=20)   priority 100%  sentiment 85%   urgency 85%
data/batch_100       (n=100)  priority 94%   sentiment 72%   urgency 92%
```

`python email-agent.py eval` reproduces this; `view --priority Critical` shows the ranking in action
(a legal-threat email lands at Critical, score 9.45, confidence 0.91).

## 3. Draft personalized responses

**Answer.** `src/email_agent/agents/drafter.py`. Offline it composes a real reply from the retrieved
knowledge chunks, the extracted case facts (order IDs, deadlines, names) and the detected language
(greetings in EN/ES/DE/FR); it is policy-bounded — it never writes a promise the validator would flag.
With `EMAIL_AGENT_LLM=openai` the model drafts instead, grounded by the same retrieved context and
prompted by `config/prompts/drafter.md`, and the deterministic drafter stays as the fallback.

**Proof.** Every draft carries text, confidence, tone, language, key points addressed and the
knowledge-base citations it used — visible in the UI's *Draft response* tab and in the JSON export.
`python email-agent.py run --input data/sample_emails` prints one line per email with confidence and
the dispatch time it earned.

## 4. Schedule them for dispatch (no actual sending required)

**Answer.** `src/email_agent/agents/scheduler.py` + the `scheduling` block of `config/rules.json`:
Mon–Fri 09:00–18:00 business hours, response targets of 1 h (critical) / 4 h (high) / 24 h (medium),
criticals never deferred to the next business morning, spam/junk and escalations get **no** send time.
The decision is persisted to SQLite (`schedules`: `status`, `send_at`, `reason`) and rendered as the
*Dispatch schedule* view — grouped into `scheduled` (ordered by send time), `pending_human`,
`escalated`, `junk`. There is no transport layer at all: the schedule is the deliverable.

## 5. Optional: break the agent into sub-agents

**Answer.** Yes — four agents plus the orchestration:

| Sub-agent | File | Responsibility |
|---|---|---|
| Classifier | `agents/classifier.py` | sentiment, urgency, priority, category, confidence, signals (+ optional LLM refinement and judge verification) |
| Retrieval (RAG step) | `retrieval.py` | top-k knowledge chunks for the case |
| Drafting | `agents/drafter.py` | the reply, with citations and confidence |
| Review | `agents/reviewer.py` | policy bans (refund/legal/internal promises), confidence gates, rewrite loop (max 2 → escalate) |
| Scheduler | `agents/scheduler.py` | dispatch time and bucket |
| Orchestrator | `graph.py` | the LangGraph `StateGraph` that wires them |

The graph makes the coordination explicit: `classify → retrieve → draft → review → revise ↺ →
finalize → schedule → persist`, with the spam short-circuit and a `halt` edge that isolates a failing
node to that one email. `python email-agent.py graph` prints the topology (checked in at
`docs/graph.mmd`); the UI shows it in an expander.

## 6. Use any LLM or SLM — locally or through APIs

**Answer.** The model layer is a seam, not a dependency (`src/email_agent/llm.py`): OpenAI-compatible
chat completions over stdlib `urllib`, configured by `EMAIL_AGENT_LLM` / `EMAIL_AGENT_BASE_URL` /
`EMAIL_AGENT_MODEL` / `EMAIL_AGENT_API_KEY`. Because the base URL is configurable, OpenAI, Mistral,
Groq, a LiteLLM/OpenRouter gateway in front of Claude, or a local Ollama/vLLM server all work without
code changes; the judge channel accepts any OpenAI-compatible chat endpoint too. Default is
`none` — the rules engine and templates are first-class, so the project also satisfies "works locally"
by running fully offline with no model at all.

**Proof.** The judge tier was exercised live against OpenCode Zen (JEV) on the golden set and the
100-email batch (e.g. `judge verdicts {'agreed': 69, 'overridden': 31}`), and `python email-agent.py
check` pings whichever tiers are configured before a run.

## 7. Optional: RAG for the internal knowledge base

**Answer.** `src/email_agent/retrieval.py` — dependency-free keyword/IDF scoring over
`data/knowledge_base/` (6 documents: refund policy, shipping delays, billing charges, password reset,
two historical replies). The top 3 chunks are injected into the drafting prompt and cited in the
draft, and they are shown per email in the *Retrieved context (RAG)* tab. A vector store can be
swapped in behind the same `retrieve(query, top_k)` call.

## 8. Simple UI or CLI to view emails, responses and confidence scores

**Answer.** Both.

- **CLI** (`email-agent.py`): `run` prints a per-email line with priority, sentiment, urgency,
  confidence and dispatch time; `view` lists stored results; `summary` aggregates; `eval` scores the
  labelled batches; `graph` exports the topology.
- **Streamlit console** (`ui/app.py`): sidebar for batch/tier/cap → metrics row (priority mix, average
  classification and draft confidence, scheduled/junk/escalated/pending-human, judge verdicts) → four
  views (table + inspector, all emails in full, drafts only, dispatch schedule) → per email six tabs
  (incoming, classification with score and reasoning, retrieved context, draft, review + schedule,
  agent trace with per-stage latency) → one-click JSON export.

**Proof.** Confidence appears at every stage and is stored: classification confidence in
`classifications`, draft confidence in `drafts`, review outcome in `reviews` (`storage/agent.db`).

---

## What the brief asks the evaluator to focus on

| Focus area | Where the answer lives |
|---|---|
| Agent orchestration | `src/email_agent/graph.py` (LangGraph `StateGraph`, topology at `docs/graph.mmd`), §5 above |
| Prioritization logic (sentiment + urgency + confidence) | `src/email_agent/rules.py` + `config/rules.json` (pinned scales, bands, boosts, floors, overrides), measured at 92 / 100 / 94 % agreement, §2 above |
| Quality and personalization of drafts | grounded templates + KB citations + language detection, policy validator and rewrite loop, §3 above |
| Clarity of code, README and explanations | `README.md` (architecture, prioritization formula), `HOW-TO-RUN.md` (two commands to run), 71 unit tests, `MY_DOCS/UNDERSTANDING.md` (decisions/ambiguities log) |

## Honest limitations

- Priority agreement is the calibrated gate (92–94 %); **sentiment** agreement is 72–85 % — the rules
  engine is deliberately conservative on mixed-tone emails.
- The offline drafter is policy-correct but plain; the GPT tier is what lifts draft confidence to
  0.90–1.00. Without it the review agent correctly parks drafts at `pending_human`.
- The checkpointer is in-memory (fine for a batch run); durable state would need a persistent saver.
- The UI is a review console, not a mail client: it has no send action, by design.
