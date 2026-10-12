# HOW TO RUN — Email-HELP-Agent

Everything in this guide is verified against the current repo state (**71 tests**).
One dependency: **LangGraph** — the orchestrator is a state graph (`src/email_agent/graph.py`).
No API key is needed: the default mode is the offline rules engine; GPT/JEV tiers are optional (§5).
Two ways in: the one-command runner (§1) or the Streamlit console (§2) — both run the same engine.
No email is ever sent — dispatch times are recorded, never transmitted.

---

## 1. Install and run (one command)

| Shell | Command |
|---|---|
| Any (recommended) | `pip install -r requirements.txt`, then `python run.py` |
| Windows cmd | `run.bat` (after the install above) |
| Git Bash / POSIX | `./run.sh` (after the install above) |

Prefer the browser? The same pipeline runs in the Streamlit console on the same LangGraph engine —
see §2:

```bash
pip install streamlit        # optional, one extra package
streamlit run ui/app.py      # http://localhost:8501
```

That single command runs the whole project: environment check → unit tests → pipeline over the
golden-set emails → agreement evaluation against the labels. It reads `.env` automatically, so if you
have keys configured the live tiers are used and you pay per call (see §5).

### What you should see — offline / rules-only

```
== Email-HELP-Agent - one-command run ==
[1/4] Python 3.13.13 OK  |  langgraph 1.2.14  |  input: batch 'samples'
[2/4] Running unit tests (unittest, offline) ...
      Ran 71 tests in 6.534s
      OK
[3/4] Pipeline: batch 'samples' (engine: graph | mode: none | judge: none | refine: always)
      processed 12  failed 0  priorities {'Critical': 1, 'High': 4, 'Low': 5, 'Medium': 2}
      reviews {'escalate': 1, 'approved': 10}  schedules {'escalated': 1, 'scheduled': 10, 'junk': 1}
      avg confidence: classification 0.749 / draft 0.609
      first critical/high items:
        Critical angry    conf=0.91  Refund immediately or I will contact my lawyer  -> -
        High     negative conf=0.83  Order AUR-10310 still not delivered - I needed it la  -> Mon 13:00 UTC
        High     negative conf=0.83  Bulk order AUR-10880 - delay and invoice error befor  -> Mon 13:00 UTC
        ...
[4/4] Agreement vs labels (n=12):
      priority  92%  (acceptance >= 80%)  PASS
      sentiment 75%   urgency 92%   flags P/R 0.40/0.67
== summary ==
   tests: PASS  |  pipeline: PASS  |  eval: PASS
   artifacts: storage
== DONE - all stages green ==
```

(`refine:` echoes your `.env`; a fresh checkout shows `always`. `run.py --skip-eval` stops after stage 3.)

### What changes when your `.env` has keys

Stage 3 reports the active tiers and each verdict, and the run takes seconds per email instead of
milliseconds (measured with JEV + refine mode `escalations_only`):

```
[3/4] Pipeline: batch 'samples' (engine: graph | mode: none | judge: jev | refine: escalations_only)
      processed 12  failed 0  priorities {'Critical': 1, 'High': 6, 'Low': 4, 'Medium': 1}
      judge verdicts {'agreed': 8, 'overridden': 4}
      reviews {'escalate': 1, 'needs_revision': 10}  schedules {'escalated': 1, 'pending_human': 10, 'junk': 1}
      avg confidence: classification 0.729 / draft 0.609
```

`pending_human` is deliberate: a draft the review agent wants changed never gets an automatic
dispatch time. Note the difference the drafting tier makes — with the **offline templates** the
judge flags most drafts (`needs_revision` → `pending_human`), while **GPT drafts** pass review
(live run: 0.90–1.00 confidence, one email scheduled). Verdict counts vary slightly per run; that
is the judge doing real work, not noise.

Exit codes: `0` all stages passed · `1` a stage failed · `2` environment unusable (no Python 3.11+,
or LangGraph not installed).

### Common variations

| Command | What it does |
|---|---|
| `python run.py --batch 100` | process the 100-email generated batch |
| `python run.py --batch adversarial` | process the 20 adversarial emails |
| `python run.py --input path/to/folder` | process your own `.eml` / `.txt` directory |
| `python run.py --with-llm` | force the primary (GPT) tier on |
| `python run.py --with-judge` | force the judge (JEV) tier on |
| `python run.py --now 2026-10-12T09:00:00+00:00` | fix the scheduling clock (reproducible output) |
| `python run.py --skip-tests --skip-eval` | just run the pipeline |
| `python run.py --db storage/other.db` | use a different SQLite file |

---

## 2. The Streamlit UI (same LangGraph engine)

The console is a view layer over the same pipeline: it builds the report with the same
`GraphOrchestrator`, the same agents and the same model tiers as the CLI. The sidebar shows what it
is running (`engine: graph`), and the compiled topology is rendered in an expander.

```bash
pip install streamlit        # optional dependency, install once
streamlit run ui/app.py      # http://localhost:8501
```

Sidebar: pick a batch (12 / 20 / 100 bundled emails, or upload your own `.eml`/`.txt`), choose the
model tiers (**Rules only** is free and offline; **GPT refinement** / **GPT + JEV judge** use the
keys from `.env`), optionally cap the run — **"Process the entire batch" is on by default** — then
press **Run pipeline**.

```
Tier status                          Main area
• engine: graph                      Emails processed 12 · 0 failed · avg confidence 0.75
• primary: none — off                Critical + High 5 · Scheduled 10 (junk 1 / escalated 1)
• judge: none — off                  ▸ LangGraph topology (Mermaid source of the compiled graph)
• refine mode: always                1 email(s) need a human: 001_angry_refund_legal_threat.eml
```

Main area: metrics (priority mix, both average confidences, scheduled / junk / escalated /
pending_human, judge verdicts) · a requirement-coverage expander · the compiled LangGraph topology ·
filters · and a **view switcher**:

| View | Shows |
|---|---|
| `Table + inspector` | all emails in one sortable table (includes a draft preview column) plus a detail panel for the selected email |
| `All emails (full content)` | every matching email rendered in sequence — each expander holds all six sections, nothing hidden behind a selection |
| `Drafts only` | every drafted reply in full, with confidence, tone, language, citations and review issues |
| `Dispatch schedule` | the dispatch plan grouped by bucket — **scheduled** (sorted by send time, with the next dispatch at the top), **pending human review**, **escalated**, **junk** — each row showing send time, priority, review status, subject, sender and the scheduler's reason |

Per email the six tabs are: **Incoming email** · **Classification** (priority, score, sentiment,
urgency, confidence, signals, scoring reasoning) · **Retrieved context (RAG)** · **Draft response** ·
**Review & schedule** (`send_at` + reason; nothing is transmitted) · **Agent trace** (per-stage
status and latency). Results export as JSON with one click. After changing a source file, press
**R** in the browser (or refresh) to rerun the app.

## 3. Manual commands (same pipeline, finer control)

```bash
python email-agent.py check                                # live ping of both model tiers
python email-agent.py run --input data/sample_emails --now 2026-10-12T09:00:00+00:00
python email-agent.py view --priority Critical             # rows: priority, sentiment, conf, review, dispatch, judge
python email-agent.py summary                              # aggregate stats from the database
python email-agent.py eval --show-misses                   # score all three labelled batches
python email-agent.py graph --out docs/graph.mmd           # export the compiled topology (§4)
```

- Top-level options: `--db <path>` (default `storage/agent.db`), `--llm <provider>` (`none`, `openai`, …).
- `summary` reads the database, so run the pipeline first (an unused `--db` path is created empty and
  reports zeros; `run.py` writes `storage/run-demo.db`).
- The judge tier is configured through `.env` (`EMAIL_AGENT_JUDGE_LLM=jev`), not a CLI flag.
- `eval` uses the deterministic rules engine only, so the acceptance numbers stay comparable:
  **priority agreement 92% / 100% / 94%** on the three labelled batches (bar: ≥ 80%).
- `check` prints one line per tier and exits non-zero if a configured tier cannot be reached.

`pip install -e .` exposes the same commands as `email-agent …` (and installs LangGraph).

---

## 4. Orchestration (LangGraph)

The pipeline is a LangGraph `StateGraph` (`src/email_agent/graph.py`). Parser, rules engine, the four
agents, retrieval, SQLite storage, prompts and the model tiers are ordinary Python; the control flow
is the graph:

```
START → classify ─(spam)→ schedule
        classify → retrieve → draft → review ─(needs_revision, attempts left)→ revise → review
                                    review ─(otherwise)→ finalize_review → schedule → persist → END
```

Every conditional edge also carries a `halt` branch: a failing node routes that email straight to `END`
with `status="error"`, so one bad email never stops a batch (the rest of the batch continues).

### Inspect the topology

```bash
python email-agent.py graph                 # Mermaid source to stdout
python email-agent.py graph --out docs/graph.mmd
```

The compiled graph is checked in at `docs/graph.mmd`, and the Streamlit UI shows it in an expander.

### Durable state (checkpointing)

```bash
EMAIL_AGENT_GRAPH_CHECKPOINT=1 python email-agent.py run --input data/sample_emails
```

Each email's state is kept in a LangGraph checkpointer (in-memory), keyed by `message_id`, and can be
read back with `GraphOrchestrator.snapshot(message_id)`. That is the hook for human-in-the-loop resume
(pause an escalated case, resume after a reviewer edits) — the topology is already shaped for it.

If LangGraph is missing, every entry point says so instead of failing halfway through a batch:
`error: needs LangGraph: pip install langgraph  (or: pip install -r requirements.txt)` (exit code 2).

## 5. Model tiers (rules → judge → primary)

```
Tier 0  rules engine            always on, zero tokens   (the mode all accuracy numbers were produced in)
Tier 1  judge channel (JEV)     cheap verification       (typed questions; ~1-2 calls per email)
Tier 2  primary model (GPT)     premium work             (drafting + adjudicating disagreements)
```

| Mode | Enable with | Behaviour |
|---|---|---|
| Rules only (default) | — | nothing to configure |
| Primary refinement | `EMAIL_AGENT_LLM=openai` + key | every email refined by the premium model |
| Judge / JEV | `EMAIL_AGENT_JUDGE_LLM=jev` + key | JEV reviews each proposal and each draft; disagreement routes **that single email** to the primary model (or applies the judge's correction when no primary is set) |
| **GPT + JEV (recommended)** | both + `EMAIL_AGENT_REFINE_MODE=escalations_only` | cost-first: GPT is spent only on drafting and on disagreements |

```bash
cp .env.example .env
# Judge tier — Jev (System One) on OpenCode Zen. Jev evaluates typed questions (no chat); the
# code builds them. Jev cannot be the primary model: it generates no text.
#   EMAIL_AGENT_JUDGE_LLM=jev
#   EMAIL_AGENT_JUDGE_BASE_URL=https://opencode.ai/zen/v1
#   EMAIL_AGENT_JUDGE_MODEL=jev-1.13        (or jev-1.13-free)
#   OPENCODE_API_KEY=<zen key>              (or EMAIL_AGENT_JUDGE_API_KEY)
# Strong tier — OpenAI GPT:
#   EMAIL_AGENT_LLM=openai
#   EMAIL_AGENT_BASE_URL=https://api.openai.com/v1
#   EMAIL_AGENT_MODEL=gpt-4o-mini
#   EMAIL_AGENT_API_KEY=<openai key>        (or OPENAI_API_KEY)
# Cost-first pairing:
#   EMAIL_AGENT_REFINE_MODE=escalations_only
python email-agent.py check                 # confirm both tiers before a big run
python run.py --with-judge
```

Environment notes:

- `.env` is read automatically (real environment variables win). Point `EMAIL_AGENT_ENV_FILE` at a
  non-existent file to ignore `.env` entirely (used by the test suite).
- Judge verdicts are reported per batch (`agreed` / `overridden` / `adjudicated` / `error`) and stored
  on every classification; a judge outage degrades to the tier below instead of failing the batch,
  and the failure reason is written into the classification reasoning
  (`judge: unavailable (HTTP 403: …)`).
- A different cheap OpenAI-compatible judge still works: `EMAIL_AGENT_JUDGE_API=chat` +
  `EMAIL_AGENT_JUDGE_BASE_URL`/`_MODEL`/`_API_KEY`.
- Measured live on the golden set with GPT + JEV: verdicts `agreed / adjudicated / agreed`,
  GPT draft confidence 0.90–1.00, one email scheduled, one escalated after two rewrites, 0 failures.

## 6. Tests and data verification

```bash
python -m unittest discover -s tests           # 71 tests: parser edges, rules, agents, judge channels, engine, UI smoke
python data/verify_batch.py data/batch_100     # deterministic gates on the labelled data
python data/verify_batch.py data/generated_batch
```

`tests/test_graph_engine.py` covers the engine: topology, branches (spam short-circuit, capped rewrite
loop), per-email error isolation, checkpointing, the CLI export. `tests/test_pipeline.py` runs real
emails end-to-end (storage, idempotency, injection resistance). The UI has its own headless smoke test
(`tests/test_ui_app.py`, Streamlit `AppTest`): it renders the app, runs a two-email batch and switches
through all four views.

## 7. Artifacts

| Path | Contents |
|---|---|
| `storage/run-demo.db` | SQLite results written by `run.py` (emails, classifications incl. judge verdicts, drafts, reviews, schedules, logs) |
| `storage/<name>.db` | one database per `--db` path used with the CLI |
| `storage/logs/run-*.jsonl` | one structured JSON line per processed email |
| `docs/graph.mmd` | the compiled LangGraph topology (regenerate with `python email-agent.py graph --out docs/graph.mmd`) |

Runtime state is gitignored; `run.py` recreates its database on every run.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `python3` opens the Microsoft Store | Use `python` (or `run.bat`); `run.sh` already probes interpreters itself |
| `LangGraph is required - install it` (runner) or `needs LangGraph: pip install langgraph` (CLI/UI) | `pip install -r requirements.txt` (or `pip install langgraph`) |
| `streamlit: command not found` | `pip install streamlit` — optional; the pipeline and CLI never need it |
| Port 8501 already in use | `streamlit run ui/app.py --server.port 8502` (or any free port) |
| `input directory not found` | Run from the repo root, or pass `--input <absolute path>` |
| `no text and no html body` in UI parser notes | Expected for the deliberately empty sample; other files should parse cleanly |
| `judge: error` on an email | Run `python email-agent.py check`; the classification reasoning carries the HTTP reason. Cloudflare's `403 / error 1010` (bare Python User-Agent) is fixed in the client — if it reappears, the key or base URL is wrong |
| Emails still show `pending_human` | That is the review outcome, not a failure: the judge flagged the draft; open the Review & schedule tab for the issues, or enable the GPT tier so drafts can be rewritten automatically |
| Judge seems to flag everything | You are drafting with the offline templates. Enable the GPT tier — real drafts pass review (live run: 0.90–1.00 confidence) |
| `.env` edits seem ignored | `.env` is reloaded per command; for the Streamlit app press **R**/refresh after editing |
| Emoji/accents look garbled in an old console | Output is UTF-8; run `chcp 65001` (Windows) for display only |
| Want a clean slate | Delete `storage/` — it is rebuilt on the next run |
| Tests fail after editing rules | The calibration harness is strict: re-run `python email-agent.py eval --show-misses` for per-file diffs |
