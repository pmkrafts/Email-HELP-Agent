# HOW TO RUN — Email-HELP-Agent

Agentic email pipeline: **parse → classify → retrieve → draft → review → schedule**.
One dependency (**LangGraph** — the pipeline is a state graph). No API key needed: the default mode is
the offline rules engine. Nothing is ever sent — dispatch times are recorded only.

## 1. Run it (two commands)

```bash
pip install -r requirements.txt
python run.py
```

That is the whole project: environment check → 71 unit tests → the golden-set batch → agreement
evaluation against the labels. It reads `.env` automatically, so with keys configured the GPT/JEV
tiers are used and you pay per call; without keys everything stays offline.

```
== Email-HELP-Agent - one-command run ==
[1/4] Python 3.13.13 OK  |  langgraph 1.2.14  |  input: batch 'samples'
[2/4] Running unit tests (unittest, offline) ...
      Ran 71 tests in 7.680s
      OK
[3/4] Pipeline: batch 'samples' (engine: graph | mode: none | judge: none | refine: escalations_only)
      processed 12  failed 0  priorities {'Critical': 1, 'High': 4, 'Low': 5, 'Medium': 2}
      reviews {'escalate': 1, 'approved': 10}  schedules {'escalated': 1, 'scheduled': 10, 'junk': 1}
      avg confidence: classification 0.749 / draft 0.609
      … (per-email lines)
[4/4] Agreement vs labels (n=12):
      priority  92%  (acceptance >= 80%)  PASS
      sentiment 75%   urgency 92%   flags P/R 0.40/0.67
== summary ==
   tests: PASS  |  pipeline: PASS  |  eval: PASS
   artifacts: <repo>/storage
== DONE - all stages green ==
```

(`refine:` echoes your `.env` — a fresh checkout shows `always`; timings vary per machine.)

`run.bat` (Windows) and `./run.sh` do the same. Useful flags: `--batch 100|adversarial`,
`--input <dir>`, `--with-llm`, `--with-judge`, `--now <iso>`, `--skip-tests --skip-eval`
(`python run.py --help`).

## 2. Or run it in the browser (optional)

```bash
pip install streamlit
streamlit run ui/app.py      # http://localhost:8501
```

The console is a view layer over the same LangGraph engine — its sidebar reports `engine: graph` and
the compiled topology sits in an expander next to the metrics. Pick a batch and a tier, press
**Run pipeline**, then explore: a sortable table with a per-email drill-down (classification, RAG
context, draft, review + schedule, agent trace), a full-content view, a drafts-only view, a dispatch
schedule grouped by bucket, and one-click JSON export.

## 3. How the engine works (LangGraph)

```
START → classify ─(spam)→ schedule
        classify → retrieve → draft → review ─(needs_revision, attempts left)→ revise → review
                                    review ─(otherwise)→ finalize_review → schedule → persist → END
```

A failing node routes that email straight to `END` with `status="error"`, so one bad email never stops
a batch. `python email-agent.py graph` prints this topology (it is checked in at `docs/graph.mmd`).
`EMAIL_AGENT_GRAPH_CHECKPOINT=1` keeps each email's state in a checkpointer, readable with
`GraphOrchestrator.snapshot(message_id)` — the hook for human-in-the-loop resume.

If LangGraph is missing, the CLI prints `needs LangGraph: pip install langgraph` and the runner says
`LangGraph is required - install it` — both exit with code 2 instead of failing halfway.

## 4. Model tiers (optional)

```
Tier 0  rules engine        always on, zero tokens      ← all accuracy numbers were produced here
Tier 1  judge channel       cheap verification (JEV)     ~1-2 calls per email
Tier 2  primary model       drafting + adjudication      OpenAI GPT
```

Copy `.env.example` to `.env` and fill in the keys (`EMAIL_AGENT_LLM=openai`, `EMAIL_AGENT_JUDGE_LLM=jev`,
`EMAIL_AGENT_REFINE_MODE=escalations_only` for the cost-first pairing); `python email-agent.py check`
confirms both tiers before a big run. Judge verdicts are stored per classification, and a judge outage
degrades to the tier below instead of failing the batch.

## 5. Other commands (rarely needed)

```bash
python email-agent.py run --input data/sample_emails   # CLI instead of the runner
python email-agent.py view --priority Critical         # inspect stored results
python email-agent.py eval --show-misses               # score the labelled batches
python -m unittest discover -s tests                   # the 71 tests
```

`pip install -e .` exposes them as `email-agent …`. Results land in `storage/` (SQLite plus JSONL logs
under `storage/logs/`); `run.py` rebuilds `storage/run-demo.db` on every run.

## 6. When something goes wrong

| Symptom | Fix |
|---|---|
| `python3` opens the Microsoft Store | Use `python` (or `run.bat`) |
| `LangGraph is required` / `needs LangGraph: pip install langgraph` | `pip install -r requirements.txt` |
| `streamlit: command not found` · port 8501 busy | `pip install streamlit` · `streamlit run ui/app.py --server.port 8502` |
| `input directory not found` | Run from the repo root, or pass an absolute `--input` path |
| Emails sit at `pending_human` | Review outcome, not a failure: the judge flagged the draft (see the Review & schedule tab) — enable the GPT tier so drafts can be rewritten automatically |
| `judge: error` on an email | `python email-agent.py check`; the classification reasoning carries the HTTP reason |
| `.env` edits seem ignored | Reloaded per command; in the UI press **R**/refresh |
| Want a clean slate | Delete `storage/` — it is rebuilt on the next run |
