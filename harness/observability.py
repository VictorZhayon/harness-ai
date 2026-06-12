"""Structured run logging.

# HARNESS LAYER: Observability
Every agent run — success or failure — is recorded twice:
  1. A structured JSON line to stdout (for local tailing / log shippers).
  2. A row in the Supabase `agent_runs` table (for the /runs endpoint).

Telemetry must never break a run: Supabase write failures are logged and
swallowed. Like the ledger, this module is fully decoupled from model and
GitHub calls.
"""

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from supabase import Client, create_client

logger = logging.getLogger("docagent.observability")

TABLE = "agent_runs"


@lru_cache(maxsize=1)
def _client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def log_run(
    run_id: str,
    repo_full_name: str,
    files_requested: list[str],
    files_fetched: list[str],
    tools_called: list[dict],
    verification_passed: bool,
    confidence_score: float,
    failure_type: str | None,
    pr_url: str | None,
    duration_ms: int,
) -> None:
    record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo_full_name": repo_full_name,
        "files_requested": files_requested,
        "files_fetched": files_fetched,
        "tools_called": tools_called,
        "verification_passed": verification_passed,
        "confidence_score": confidence_score,
        "failure_type": failure_type,
        "pr_url": pr_url,
        "duration_ms": duration_ms,
    }

    # Structured stdout log — always emitted, even if Supabase is down.
    logger.info(json.dumps({"event": "agent_run", **record}, default=str))

    try:
        _client().table(TABLE).insert(record).execute()
    except Exception:
        # Observability is best-effort by design: a telemetry outage must not
        # turn a verified, successful run into a 500.
        logger.exception("Failed to persist run %s to Supabase", run_id)


def get_recent_runs(limit: int = 20) -> list[dict]:
    result = _client().table(TABLE).select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []
