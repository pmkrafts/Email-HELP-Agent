# HOW TO RUN — Email-HELP-Agent

Everything in this guide is verified against the current repo state (61 tests).
**No installation and no API key required** — the core is Python 3.11+ stdlib only.
No email is ever sent: dispatch times are recorded, never transmitted.

---

## 1. The one command

| Shell | Command |
|---|---|
| Any (recommended) | `python run.py` |
| Windows cmd | `run.bat` |
| Git Bash / POSIX | `./run.sh` |

That single command runs the whole project: environment check → unit tests → pipeline over the
golden-set emails → agreement evaluation against the labels. It reads `.env` automatically, so if
you have keys configured the live tiers are used and you pay per call (see §4).

### What you should see — offline / rules-only

```
== Email-HELP-Agent - one-command run ==
[1/4] Python 3.13.13 OK  |  no dependencies  |  input: batch 'samples'
[2/4] Running unit tests (unittest, offline) ...
      Ran 61 tests in 6.604s
      OK
[3/4] Pipeline: batch 'samples' (mode: none | judge: none | refine: always)
      processed 12  failed 0  priorities {'Critical': 1, 'High': 4, 'Low': 5, 'Medium': 2}
      reviews {'escalate': 1, 'approved': 10}  schedules {'escalated': 1, 'scheduled': 10, 'junk': 1}
      avg confidence: classification 0.749 / draft 0.609
      first critical/high items:
        Critical angry    conf=0.91  Refund immediately or I will contact my lawyer  -> -
        High     negative conf=0.83  Order AUR-10310 still not delivered - I needed it la  -> Mon 09:00 UTC
        ...
[4/4] Agreement vs labels (n=12):
      priority  92%  (acceptance >= 80%)  PASS
      sentiment 75%   urgency 92%   flags P/R 0.40/0.67
== summary ==
   tests: PASS  |  pipeline: PASS  |  eval: PASS
   artifacts: storage
== DONE - all stages green ==
```

(`refine:` echoes your `.env`; a fresh checkout shows `always`.)

### What changes when your `.env` has keys

Stage 3 reports the active tiers and each verdict, and the run takes seconds per email instead of
milliseconds (measured with JEV + refine mode `escalations_only`):

```
[3/4] Pipeline: batch 'samples' (mode: none | judge: jev | refine: escalations_only)
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

Exit codes: `0` all stages passed · `1` a stage failed · `2` environment unusable (no Python 3.11+).

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

## 2. Manual commands (same pipeline, finer control)

```bash
python email-agent.py check                                # live ping of both model tiers
python email-agent.py run --input data/sample_emails --now 2026-10-12T09:00:00+00:00
python email-agent.py view --priority Critical             # rows: priority, sentiment, conf, review, dispatch, judge
python email-agent.py summary                              # aggregate stats from the database
python email-agent.py eval --show-misses                   # score all three labelled batches
```

- Top-level options: `--db <path>` (default `storage/agent.db`), `--llm <provider>` (`none`, `openai`, …).
- `summary` reads the database, so run the pipeline first (an unused `--db` path is created empty and
  reports zeros; `run.py` writes `storage/run-demo.db`).
- The judge tier is configured through `.env` (`EMAIL_AGENT_JUDGE_LLM=jev`), not a CLI flag.
- `eval` uses the deterministic rules engine only, so the acceptance numbers stay comparable:
  **priority agreement 92% / 100% / 94%** on the three labelled batches (bar: ≥ 80%).
- `check` prints one line per tier and exits non-zero if a configured tier cannot be reached.

`pip install -e .` exposes the same commands as `email-agent …`.

## 3. The Streamlit UI

```bash
pip install streamlit        # optional dependency, install once
streamlit run ui/app.py      # http://localhost:8501
```

Sidebar: pick a batch (12 / 20 / 100 bundled emails, or upload your own `.eml`/`.txt`), choose the
model tiers (**Rules only** is free and offline; **GPT refinement** / **GPT + JEV judge** use the
keys from `.env`), optionally cap the run — **"Process the entire batch" is on by default** — then
press **Run pipeline**.

Main area: metrics (priority mix, both average confidences, scheduled / junk / escalated /
pending_human, judge verdicts) · a requirement-coverage expander · filters · and a **view switcher**:

| View | Shows |
|---|---|
| `Table + inspector` | all emails in one sortable table (includes a draft preview column) plus a detail panel for the selected email |
| `All emails (full content)` | every matching email rendered in sequence — each expander holds all six sections, nothing hidden behind a selection |
| `Drafts only` | every drafted reply in full, with confidence, tone, language, citations and review issues |

Per email the six tabs are: **Incoming email** · **Classification** (priority, score, sentiment,
urgency, confidence, signals, scoring reasoning) · **Retrieved context (RAG)** · **Draft response** ·
**Review & schedule** (`send_at` + reason; nothing is transmitted) · **Agent trace** (per-stage
status and latency). Results export as JSON with one click. After changing a source file, press
**R** in the browser (or refresh) to rerun the app.

The look follows the project design system (`ui/design.py`, `.streamlit/config.toml`): warm paper
canvas, one blue accent for the primary action, white hairline cards, Inter with tight display
tracking, sticker-palette dots for status. `tests/test_ui_design.py` keeps the UI on-spec.

## 4. Model tiers (rules → judge → primary)

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

## 5. Tests and data verification

```bash
python -m unittest discover -s tests           # 61 tests: parser edges, rules, agents, judge channels, UI smoke
python data/verify_batch.py data/batch_100     # deterministic gates on the labelled data
python data/verify_batch.py data/generated_batch
```

The UI has its own headless smoke test (`tests/test_ui_app.py`, Streamlit `AppTest`): it renders the
app, runs a two-email batch and switches through all three views.

## 6. Artifacts

| Path | Contents |
|---|---|
| `storage/run-demo.db` | SQLite results written by `run.py` (emails, classifications incl. judge verdicts, drafts, reviews, schedules, logs) |
| `storage/<name>.db` | one database per `--db` path used with the CLI |
| `storage/logs/run-*.jsonl` | one structured JSON line per processed email |

Runtime state is gitignored; `run.py` recreates its database on every run.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `python3` opens the Microsoft Store | Use `python` (or `run.bat`); `run.sh` already probes interpreters itself |
| `input directory not found` | Run from the repo root, or pass `--input <absolute path>` |
| `no text and no html body` in UI parser notes | Expected for the deliberately empty sample; other files should parse cleanly |
| `judge: error` on an email | Run `python email-agent.py check`; the classification reasoning carries the HTTP reason. Cloudflare's `403 / error 1010` (bare Python User-Agent) is fixed in the client — if it reappears, the key or base URL is wrong |
| Emails still show `pending_human` | That is the review outcome, not a failure: the judge flagged the draft; open the Review & schedule tab for the issues, or enable the GPT tier so drafts can be rewritten automatically |
| Judge seems to flag everything | You are drafting with the offline templates. Enable the GPT tier — real drafts pass review (live run: 0.90–1.00 confidence) |
| `.env` edits seem ignored | `.env` is reloaded per command; for the Streamlit app press **R**/refresh after editing |
| Emoji/accents look garbled in an old console | Output is UTF-8; run `chcp 65001` (Windows) for display only |
| Want a clean slate | Delete `storage/` — it is rebuilt on the next run |
| Tests fail after editing rules | The calibration harness is strict: re-run `python email-agent.py eval --show-misses` for per-file diffs |
