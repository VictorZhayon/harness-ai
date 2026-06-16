# DocAgent Harness (GitHub Edition)

A GitHub-connected AI documentation agent wrapped in a full outer harness —
a demonstration of **Harness Engineering**. Point it at a repo; it fetches
real source files, writes documentation grounded in them, verifies every
claim, and opens a pull request with the result.

## What is Harness Engineering?

An AI agent is not just a model: **Agent = Model + Harness**. The model
(here, a frozen `gemini-2.5-flash`) provides raw capability, while the
harness provides everything that makes it dependable — constraints, tools,
verification, guardrails, and a memory of past mistakes. Because the model is
frozen, all improvement happens in the harness: when the agent fails, the
harness records the failure and permanently changes the agent's instructions
so it doesn't fail the same way twice.

## The Layers

| Layer | Where | What it does |
|---|---|---|
| **1. Guides** | `AGENTS.md`, `agent/guides.py` | The agent's contract, loaded as the system prompt on every run. The harness appends learned corrections to it automatically. |
| **2. Tool Orchestration** | `agent/tools.py` | Every capability is a mediated LangChain tool (`fetch_code_snippet`, `search_existing_docs`, `write_doc_section`). Tools operate on run-scoped memory and record an audit trail. |
| **3. Verification Loop** | `agent/verifier.py` | Drafts are never trusted. A deterministic hallucination check cross-references every mentioned symbol against the fetched files, then a second Gemini call self-critiques the draft and scores confidence. Below 0.75, or any hallucinated name → rejected. |
| **4. Guardrails** | `harness/guardrails.py` | Last gate before anything leaves the system: code examples must be traceable to fetched files, verification must not have been skipped, and every response gets a `{verified, confidence, tools_used, run_id, pr_url}` envelope. |
| **5. Mistake Ledger + Observability** | `harness/ledger.py`, `harness/patcher.py`, `harness/observability.py` | Failures are written to Supabase with a correction; on the next run they are patched into `AGENTS.md` — the self-correction loop. Every run (pass or fail) is logged to `agent_runs`. |
| **GitHub Integration** | `github_integration/` | The only code that touches GitHub: `client.py` (PAT auth), `fetcher.py` (file fetch + repo crawl), `pr_writer.py` (opens the docs PR — only ever called after verification and guardrails pass). |

The harness flow (implemented in [`agent/runner.py`](agent/runner.py)):

```
load guide + inject corrections → fetch files from GitHub → init agent
  → agentic loop (stage doc sections in memory) → verify
     → fail: log to ledger → 422
     → pass: guardrails → fail: 422
                        → pass: open docs PR → log run → envelope → 200
```

Unverified documentation can never reach a pull request: the PR writer runs
*after* both verification and guardrails, by construction.

## Setup

1. **Install**

   ```bash
   cd docagent-harness
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Create a GitHub PAT** — github.com → Settings → Developer settings →
   Personal access tokens. The token only needs the **`repo`** scope
   (classic PAT) or, for fine-grained tokens, **Contents: read/write** and
   **Pull requests: read/write** on the target repos. The agent must be able
   to create branches and open PRs on the repo you point it at.

3. **Create the Supabase tables** — open the Supabase SQL editor and run
   [`supabase_schema.sql`](supabase_schema.sql).

4. **Configure environment**

   ```bash
   cp .env.example .env
   # GOOGLE_API_KEY=        your Google AI Studio key
   # SUPABASE_URL=          https://<project>.supabase.co
   # SUPABASE_SERVICE_KEY=  the service-role key
   # GITHUB_PAT=            the token from step 2
   ```

5. **Run**

   ```bash
   uvicorn main:app --reload
   ```

   Then open **http://localhost:8000** for the web UI, or use the API directly
   (see below).

## Web UI

The dashboard at `http://localhost:8000` exposes all four operations without
needing curl:

**Generate** — enter a repo, list file paths (one per line), and optionally
enable Auto Crawl. The agent runs synchronously; on completion you see
confidence score, verified status, tools used, and a direct link to the opened
PR. Harness rejections (hallucination caught, guardrail violation, etc.) are
displayed as structured error panels rather than raw HTTP errors.

**Recent Runs** — table of the last 20 executions from the observability log,
with confidence badges, pass/fail status, and PR links.

**Ledger** — table of all recorded mistakes and their corrections, including
whether each has been patched into `AGENTS.md`. Includes a form to submit a
correction manually: it calls `POST /ledger/correct` and patches `AGENTS.md`
immediately so the very next run benefits.

## API

### POST /generate-docs — full harnessed run, ends in a PR

```bash
curl -X POST http://localhost:8000/generate-docs \
  -H "Content-Type: application/json" \
  -d '{
    "repo_full_name": "your-org/payments-service",
    "file_paths": ["src/payments.py", "src/auth.py"],
    "auto_crawl": false
  }'
```

Set `"auto_crawl": true` to ignore `file_paths` and crawl the repo tree for
source files instead (capped at 10 files per run; `node_modules`, `.git`,
`__pycache__`, `dist`, `build` are skipped).

Success response (`200`):

```json
{
  "verified": true,
  "confidence": 0.91,
  "tools_used": ["fetch_code_snippet", "search_existing_docs", "write_doc_section"],
  "run_id": "7f3c9a1e-...",
  "pr_url": "https://github.com/your-org/payments-service/pull/42",
  "output": "Documented src/payments.py (3 functions) and src/auth.py (3 functions)..."
}
```

### GET /runs — observability log

```bash
curl http://localhost:8000/runs
```

### GET /ledger — mistake ledger entries

```bash
curl http://localhost:8000/ledger
```

### POST /ledger/correct — log a correction, patch AGENTS.md immediately

```bash
curl -X POST http://localhost:8000/ledger/correct \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "7f3c9a1e-...",
    "failure_type": "hallucination",
    "description": "Doc omitted the idempotency_key parameter",
    "correction": "Always document every parameter shown in the fetched signature, including optional ones."
  }'
```

## Mistake Ledger in Action

The self-correction loop, end to end:

**1. A run fails.** The agent documents `src/payments.py` but mentions a
`validate_card()` helper that exists nowhere in the fetched files. The
deterministic hallucination check catches it — no PR is opened, and the API
returns `422`:

```json
{
  "detail": {
    "reason": "verification_failed",
    "failure_type": "hallucination",
    "hallucinated_names": ["validate_card"],
    "issues": ["The draft claims card validation occurs, which is not shown in the code."],
    "verified": false,
    "confidence": 0.55,
    "pr_url": null,
    "run_id": "b41d..."
  }
}
```

**2. The failure becomes a ledger entry** (written automatically, alongside
any corrections a human submits via `POST /ledger/correct` or the web UI):

| failure_type | correction | injected_into_agents_md |
|---|---|---|
| hallucination | Never mention functions or classes that do not appear in the fetched files. Nonexistent names used: validate_card. Confirm every symbol with fetch_code_snippet before documenting it. | false |

**3. The next run starts smarter.** Before the agent initializes,
`agent/guides.py` pulls every uninjected correction and `harness/patcher.py`
appends it to `AGENTS.md`:

```markdown
## Learned Corrections

- **[hallucination]** Never mention functions or classes that do not appear
  in the fetched files. Nonexistent names used: validate_card. Confirm every
  symbol with fetch_code_snippet before documenting it.
```

The entry is marked `injected_into_agents_md = true`, the rerun documents
only real symbols, passes verification at confidence 0.91, and ends with an
open PR. Same model, better agent — the harness did the learning.

## Demo Flow

Point it at a small public repo you have write access to (fork one first —
the PAT owner needs permission to create branches). Example with a fork of
`psf/requests-html`:

```bash
curl -X POST http://localhost:8000/generate-docs \
  -H "Content-Type: application/json" \
  -d '{
    "repo_full_name": "<your-username>/requests-html",
    "file_paths": ["requests_html.py"],
    "auto_crawl": false
  }'
```

What happens:

1. `fetcher.py` pulls `requests_html.py` from the default branch.
2. The agent inspects functions with `fetch_code_snippet` and stages a doc
   section in memory.
3. The verifier cross-checks every mentioned symbol against the fetched file
   and self-critiques the draft.
4. `pr_writer.py` creates branch `docagent/run-<id>`, commits
   `docs/generated/requests_html.md`, and opens
   **"docs: AI-generated documentation [<id>]"** — the PR body lists the
   files documented, the confidence score, and the run ID for the
   observability log.
5. The response (or the web UI) gives you the `pr_url`; review and merge like
   any PR.

## Project Layout

```
docagent-harness/
├── main.py                  # FastAPI entrypoint + web UI route
├── static/
│   └── index.html           # Single-page dashboard (Generate, Runs, Ledger)
├── agent/
│   ├── runner.py            # Agentic loop orchestrator (10-step harness flow)
│   ├── guides.py            # Loads AGENTS.md + injects mistake ledger
│   ├── tools.py             # LangChain tools over run-scoped repo memory
│   └── verifier.py          # Verification loop (hallucination + confidence)
├── harness/
│   ├── ledger.py            # Mistake Ledger — Supabase read/write
│   ├── observability.py     # Structured run logging
│   ├── guardrails.py        # Output validation + metadata envelope
│   └── patcher.py           # Injects corrections into AGENTS.md
├── github_integration/
│   ├── client.py            # PAT auth + repo client
│   ├── fetcher.py           # File fetching + repo tree crawl
│   └── pr_writer.py         # Opens the docs PR
├── AGENTS.md                # Live agent guide (updated by the harness)
└── supabase_schema.sql      # Tables for the ledger + observability
```

## Design Constraints

- Gemini is called **only** in `agent/runner.py` and `agent/verifier.py`,
  through one shared `ChatGoogleGenerativeAI(model="gemini-2.5-flash")`
  instance.
- GitHub is called **only** inside `github_integration/`.
- The harness modules (`ledger`, `observability`, `guardrails`, `patcher`)
  are fully decoupled from both the model and GitHub — they see plain data.
- The harness fails closed: an unparseable self-critique counts as zero
  confidence, and skipped verification is a guardrail violation.
- The PR writer is only reachable through the runner's step 8, after
  verification and guardrails have passed.
- No hardcoded secrets; everything comes from `.env` via `python-dotenv`.
