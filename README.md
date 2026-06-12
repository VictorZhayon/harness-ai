# DocAgent Harness

A production-grade AI documentation agent wrapped in a full outer harness —
a demonstration of **Harness Engineering**.

## What is Harness Engineering?

An AI agent is not just a model: **Agent = Model + Harness**. The model
(here, a frozen `gemini-2.0-flash`) provides raw capability, while the
harness provides everything that makes it dependable — constraints, tools,
verification, guardrails, and a memory of past mistakes. Because the model is
frozen, all improvement happens in the harness: when the agent fails, the
harness records the failure and permanently changes the agent's instructions
so it doesn't fail the same way twice.

## The 5 Harness Layers

| Layer | Where | What it does |
|---|---|---|
| **1. Guide System** | `AGENTS.md`, `agent/guides.py` | The agent's contract, loaded as the system prompt on every run. The harness appends learned corrections to it automatically. |
| **2. Tool Orchestration** | `agent/tools.py` | Every capability is a mediated LangChain tool (`fetch_code_snippet`, `search_existing_docs`, `write_doc_section`). Tools record an audit trail of everything the agent saw and did. |
| **3. Verification Loop** | `agent/verifier.py` | Drafts are never trusted. A deterministic hallucination check cross-references every mentioned symbol against the real codebase, then a second Gemini call self-critiques the draft and scores confidence. Below 0.75, or any hallucinated name → rejected. |
| **4. Guardrails** | `agent/guardrails.py` | Last gate before the API response: code blocks must be traceable to a real `fetch_code_snippet` call, verification must not have been skipped, and every response gets a `{verified, confidence, tools_used, run_id}` metadata envelope. |
| **5. Mistake Ledger + Observability** | `harness/ledger.py`, `harness/patcher.py`, `harness/observability.py` | Failures are written to Supabase with a correction. On the next run, uninjected corrections are patched into `AGENTS.md` — the self-correction loop. Every run (pass or fail) is logged to `agent_runs`. |

The harness flow (implemented in [`agent/runner.py`](agent/runner.py)):

```
load guide + inject corrections → init agent → agentic loop → verify
   → fail: log to ledger → 422          → pass: guardrails
                                            → fail: 422
                                            → pass: log run → envelope → 200
```

## Setup

1. **Clone and install**

   ```bash
   cd docagent-harness
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Create the Supabase tables** — open the Supabase SQL editor and run
   [`supabase_schema.sql`](supabase_schema.sql).

3. **Configure environment**

   ```bash
   cp .env.example .env
   # then fill in:
   # GOOGLE_API_KEY=        your Google AI Studio key
   # SUPABASE_URL=          https://<project>.supabase.co
   # SUPABASE_SERVICE_KEY=  the service-role key
   ```

4. **Run**

   ```bash
   uvicorn main:app --reload
   ```

## API

### Generate documentation (full harnessed run)

```bash
curl -X POST http://localhost:8000/generate-docs \
  -H "Content-Type: application/json" \
  -d '{
    "request": "Document the payment processing function",
    "file_path": "payments.py",
    "function_name": "process_payment"
  }'
```

Success response (`200`):

```json
{
  "verified": true,
  "confidence": 0.92,
  "tools_used": ["search_existing_docs", "fetch_code_snippet", "write_doc_section"],
  "run_id": "7f3c9a1e-...",
  "output": "## process_payment\n\nCharges a customer and records the transaction..."
}
```

### Inspect recent runs (observability)

```bash
curl http://localhost:8000/runs
```

### Inspect the mistake ledger

```bash
curl http://localhost:8000/ledger
```

### Log a manual correction (patches AGENTS.md immediately)

```bash
curl -X POST http://localhost:8000/ledger/correct \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "7f3c9a1e-...",
    "failure_type": "incomplete",
    "description": "Doc omitted the idempotency_key parameter",
    "correction": "Always document every parameter shown in the fetched signature, including optional ones."
  }'
```

## Mistake Ledger in Action

The self-correction loop, end to end:

**1. A run fails verification.** The agent documents `process_payment` but
mentions a `validate_card()` helper that doesn't exist in the codebase. The
deterministic hallucination check catches it and the API returns `422`:

```json
{
  "detail": {
    "reason": "verification_failed",
    "failure_type": "hallucination",
    "hallucinated_names": ["validate_card"],
    "issues": ["The draft claims card validation occurs, which is not shown in the snippet."],
    "verified": false,
    "confidence": 0.55,
    "tools_used": ["fetch_code_snippet"],
    "run_id": "b41d...",
    "output": null
  }
}
```

**2. The failure becomes a ledger entry.** The harness writes to
`mistake_ledger` automatically:

| failure_type | correction | injected_into_agents_md |
|---|---|---|
| hallucination | Never mention functions or classes that were not returned by fetch_code_snippet. Nonexistent names used: validate_card. Fetch and confirm every symbol before documenting it. | false |

**3. The next run starts smarter.** Before the agent initializes,
`agent/guides.py` pulls every uninjected correction and
`harness/patcher.py` appends it to `AGENTS.md`:

```markdown
## Learned Corrections

- **[hallucination]** Never mention functions or classes that were not
  returned by fetch_code_snippet. Nonexistent names used: validate_card.
  Fetch and confirm every symbol before documenting it.
```

The entry is marked `injected_into_agents_md = true`, and the correction is
now part of the agent's system prompt on every future run. Same model,
better agent — that's the harness doing the learning.

## Project Layout

```
docagent-harness/
├── main.py                  # FastAPI entrypoint
├── agent/
│   ├── runner.py            # Agentic loop orchestrator (the 9-step harness flow)
│   ├── guides.py            # Loads AGENTS.md + injects mistake ledger
│   ├── tools.py             # LangChain tools (fetch, search, write)
│   ├── verifier.py          # Verification loop (hallucination + confidence check)
│   └── guardrails.py        # Output validation before returning to user
├── harness/
│   ├── ledger.py            # Mistake Ledger — Supabase read/write
│   ├── observability.py     # Structured run logging
│   └── patcher.py           # Injects corrections into AGENTS.md after failures
├── sample_codebase/         # The "real" code the agent documents
├── docs/                    # Existing docs + agent output (docs/output/)
├── AGENTS.md                # Live agent guide (updated by the harness)
└── supabase_schema.sql      # Tables for the ledger + observability
```

## Design Constraints

- Gemini is called **only** in `agent/runner.py` and `agent/verifier.py`,
  through one shared `ChatGoogleGenerativeAI(model="gemini-2.0-flash")`
  instance.
- The harness modules (`ledger`, `observability`, `patcher`) are fully
  decoupled from model calls — they only touch Supabase and the filesystem.
- The harness fails closed: an unparseable self-critique counts as zero
  confidence, and skipped verification is a guardrail violation.
- No hardcoded secrets; everything comes from `.env` via `python-dotenv`.
