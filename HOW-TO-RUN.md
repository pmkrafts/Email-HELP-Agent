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

## 3. Optional LLM mode

```bash
cp .env.example .env          # then set OPENAI_API_KEY=... and EMAIL_AGENT_LLM=openai
python run.py --with-llm
```

Without a key the project silently stays in deterministic rules mode — that is the mode all
numbers above were produced in. The client (`src/email_agent/llm.py`) uses the OpenAI-compatible
chat-completions API with JSON mode and retries.

## 4. Tests and data verification

```bash
python -m unittest discover -s tests       # 33 tests: parser edge cases, rules, end-to-end
python data/verify_batch.py data/batch_100     # deterministic gates on the labelled data
python data/verify_batch.py data/generated_batch
```

## 5. Artifacts

| Path | Contents |
|---|---|
| `storage/run-demo.db` | SQLite results (emails, classifications, drafts, reviews, schedules, logs) |
| `storage/logs/run-*.jsonl` | one structured JSON line per processed email |

Both are always recreated by `run.py` (fresh database each run) and are gitignored.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `python3` opens the Microsoft Store | Use `python` (or `run.bat`); `run.sh` already probes interpreters itself |
| `input directory not found` | Run from the repo root, or pass `--input <absolute path>` |
| `OPENAI_API_KEY not set - falling back ...` | Expected without a key; rules mode is fully functional |
| Emoji/accents look garbled in an old console | Output is UTF-8; run `chcp 65001` (Windows) for display only |
| Want a clean slate | Delete `storage/` — `run.py` rebuilds it anyway |
| Tests fail after editing rules | The calibration harness is strict: re-run `python email-agent.py eval --show-misses` for per-file diffs |
