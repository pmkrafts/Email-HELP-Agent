# HOW TO RUN — Email-HELP-Agent

Everything in this guide is verified against the current repo state.
**No installation, no dependencies, no API key required** — Python 3.11+ stdlib only.

---

## 1. The one command

| Shell | Command |
|---|---|
| Any (recommended) | `python run.py` |
| Windows cmd | `run.bat` |
| Git Bash / POSIX | `./run.sh` |

That single command runs the whole project: environment check → unit tests → pipeline over the
golden-set emails → agreement evaluation against the labels.

### What you should see (actual output)

```
== Email-HELP-Agent - one-command run ==
[1/4] Python 3.13.13 OK  |  no dependencies  |  input: batch 'samples'
[2/4] Running unit tests (unittest, offline) ...
      Ran 33 tests in 0.284s
      OK
[3/4] Pipeline: batch 'samples' (mode: none)
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

Exit codes: `0` all stages passed · `1` a stage failed · `2` environment unusable (no Python 3.11+).

### Common variations

| Command | What it does |
|---|---|
| `python run.py --batch 100` | process the 100-email generated batch |
| `python run.py --batch adversarial` | process the 20 adversarial emails |
| `python run.py --input path/to/folder` | process your own `.eml` / `.txt` directory |
| `python run.py --with-llm` | enable OpenAI refinement (needs `OPENAI_API_KEY`; falls back to rules without it) |
| `python run.py --now 2026-10-12T09:00:00+00:00` | fix the scheduling clock (reproducible output) |
| `python run.py --skip-tests --skip-eval` | just run the pipeline |
| `python run.py --db storage/other.db` | use a different SQLite file |

---

## 2. Manual commands (same pipeline, finer control)

```bash
python email-agent.py run --input data/sample_emails --now 2026-10-12T09:00:00+00:00
python email-agent.py view --priority Critical        # inspect stored results
python email-agent.py summary                          # aggregate stats
python email-agent.py eval --show-misses               # score all three labelled batches
```

Expected highlights: `eval` reports **priority agreement 92% / 100% / 94%** on the three batches
(acceptance bar ≥ 80%). `run` prints one line per email with priority, sentiment, confidence and
the scheduled send time.

`pip install -e .` turns these into `email-agent …` if you prefer.

## 3. The Streamlit UI

```bash
pip install streamlit        # optional dependency, install once
streamlit run ui/app.py      # http://localhost:8501
```

In the sidebar pick a batch (12 / 20 / 100 bundled emails, or upload your own `.eml`/`.txt`),
choose the model tiers (**Rules only** is free and offline; **GPT refinement** / **GPT + JEV judge**
use the keys from `.env`), then press **Run pipeline**.

The screen then shows: metrics (priority mix, both average confidences, scheduled/junk/escalated,
judge verdicts) · an expander mapping every assignment requirement to where it is answered ·
a priority-sorted, filterable table · and per-email tabs for the **incoming email**,
**classification** (priority/sentiment/urgency/confidence/signals/reasoning), **retrieved context
(RAG)**, **draft response**, **review & schedule** (`send_at` + reason; nothing is transmitted)
and the **agent trace** (per-stage status and latency). Results export as JSON with one click.

## 4. Optional model tiers (rules → judge → primary)

The classifier is a cost ladder; every tier has a fallback to the one below it:

```
Tier 0  rules engine            always on, zero tokens   (the mode all numbers above were produced in)
Tier 1  primary model           optional refinement      (premium quality per email)
Tier 2  judge channel ("JEV")   optional verification    (cheap model reviews the proposal)
```

| Mode | Enable with | Behaviour |
|---|---|---|
| Rules only (default) | — | nothing to configure |
| Primary refinement | `OPENAI_API_KEY` + `--with-llm` | every email refined by the premium model |
| **Judge / JEV** | `EMAIL_AGENT_JUDGE_BASE_URL` + `_API_KEY` + `--with-judge` | JEV (`jev-1.13`) reviews each proposal; agreement keeps the cheap path, disagreement routes **that single email** to the primary model (or applies the judge's correction when no primary is set) |
| **GPT + JEV (recommended)** | both of the above + `EMAIL_AGENT_REFINE_MODE=escalations_only` | cost-first: JEV judges every email and reviews every draft; GPT is spent only on drafting and on disagreements |

```bash
cp .env.example .env
# Judge tier — Jev (System One) on OpenCode Zen. Jev evaluates typed questions (no chat);
# the code builds them. Verify with:  python email-agent.py check
#   EMAIL_AGENT_JUDGE_LLM=jev
#   EMAIL_AGENT_JUDGE_BASE_URL=https://opencode.ai/zen/v1
#   EMAIL_AGENT_JUDGE_MODEL=jev-1.13        (or jev-1.13-free)
#   OPENCODE_API_KEY=<zen key>              (or EMAIL_AGENT_JUDGE_API_KEY)
# Strong tier — OpenAI GPT (Jev cannot be the primary: it generates no text):
#   EMAIL_AGENT_LLM=openai
#   EMAIL_AGENT_BASE_URL=https://api.openai.com/v1
#   EMAIL_AGENT_MODEL=gpt-4o-mini
#   EMAIL_AGENT_API_KEY=<openai key>
# Cost-first pairing (GPT classifies only when Jev disagrees):
#   EMAIL_AGENT_REFINE_MODE=escalations_only
python run.py --with-judge
```

Measured on the golden set with mock gateways (same code path): GPT = 11 drafts + 1 adjudication,
Jev = 12 verdicts + 10 draft reviews, 11 agreed / 1 adjudicated, 0 failures.
A different cheap OpenAI-compatible judge still works via `EMAIL_AGENT_JUDGE_API=chat`.

Judge verdicts are reported per batch (`agreed` / `overridden` / `adjudicated` / `error`) and logged
per email; a judge outage degrades to the tier below instead of failing the batch. Endpoint
behaviour is covered by `tests/test_judge.py` against a local mock server — no key needed.
The client (`src/email_agent/llm.py`) speaks the OpenAI-compatible chat-completions API with JSON mode.

## 5. Tests and data verification

```bash
python -m unittest discover -s tests       # 33 tests: parser edge cases, rules, end-to-end
python data/verify_batch.py data/batch_100     # deterministic gates on the labelled data
python data/verify_batch.py data/generated_batch
```

## 6. Artifacts

| Path | Contents |
|---|---|
| `storage/run-demo.db` | SQLite results (emails, classifications, drafts, reviews, schedules, logs) |
| `storage/logs/run-*.jsonl` | one structured JSON line per processed email |

Both are always recreated by `run.py` (fresh database each run) and are gitignored.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `python3` opens the Microsoft Store | Use `python` (or `run.bat`); `run.sh` already probes interpreters itself |
| `input directory not found` | Run from the repo root, or pass `--input <absolute path>` |
| `OPENAI_API_KEY not set - falling back ...` | Expected without a key; rules mode is fully functional |
| Emoji/accents look garbled in an old console | Output is UTF-8; run `chcp 65001` (Windows) for display only |
| Want a clean slate | Delete `storage/` — `run.py` rebuilds it anyway |
| Tests fail after editing rules | The calibration harness is strict: re-run `python email-agent.py eval --show-misses` for per-file diffs |
