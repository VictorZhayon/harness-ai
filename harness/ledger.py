"""Mistake Ledger — persistent record of agent failures and their corrections.

# HARNESS LAYER: Mistake Ledger
Every verification failure is written here, together with a correction the
agent should learn. On the next run, uninjected corrections are appended to
AGENTS.md (see harness/patcher.py) so the agent does not repeat the mistake.

This module talks only to Supabase. It is completely decoupled from model
and GitHub calls — it never imports anything from the LLM stack or PyGithub.
"""

import os
from functools import lru_cache

from supabase import Client, create_client

TABLE = "mistake_ledger"

VALID_FAILURE_TYPES = {"hallucination", "incomplete", "wrong_format", "unverified_claim"}


@lru_cache(maxsize=1)
def _client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def log_mistake(
    failure_type: str,
    description: str,
    correction: str,
    run_id: str | None = None,
) -> dict:
    """Record a failure + correction. The originating run is folded into the
    description (the ledger schema is intentionally minimal). Raises on
    invalid failure_type so bad data never enters the ledger."""
    if failure_type not in VALID_FAILURE_TYPES:
        raise ValueError(f"failure_type must be one of {sorted(VALID_FAILURE_TYPES)}, got '{failure_type}'")

    if run_id:
        description = f"[run {run_id}] {description}"

    row = {
        "failure_type": failure_type,
        "description": description,
        "correction": correction,
        "injected_into_agents_md": False,
    }
    result = _client().table(TABLE).insert(row).execute()
    return result.data[0] if result.data else row


def get_uninjected_corrections() -> list[dict]:
    """Corrections logged since the last run that AGENTS.md hasn't absorbed yet."""
    result = (
        _client()
        .table(TABLE)
        .select("*")
        .eq("injected_into_agents_md", False)
        .order("created_at")
        .execute()
    )
    return result.data or []


def mark_injected(entry_ids: list) -> None:
    """Flip the injected flag once corrections have been patched into AGENTS.md."""
    if not entry_ids:
        return
    _client().table(TABLE).update({"injected_into_agents_md": True}).in_("id", entry_ids).execute()


def get_all_entries() -> list[dict]:
    result = _client().table(TABLE).select("*").order("created_at", desc=True).execute()
    return result.data or []
