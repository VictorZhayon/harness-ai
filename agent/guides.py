"""Loads the agent guide (AGENTS.md) and injects learned corrections.

# HARNESS LAYER: Guide System
AGENTS.md is the agent's contract: what it may do, what it must never do, and
— crucially — every correction the harness has learned from past failures.
This module guarantees the agent never starts a run with a stale guide.
"""

import logging

from harness.ledger import get_uninjected_corrections, mark_injected
from harness.patcher import AGENTS_MD_PATH, patch_agents_md

logger = logging.getLogger("docagent.guides")


def load_guide() -> str:
    """Return the up-to-date AGENTS.md content for use as the system prompt.

    Before reading, pull any corrections from the mistake ledger that haven't
    been injected yet, patch them into the '## Learned Corrections' section,
    and mark them injected. The agent therefore always sees the lessons from
    every previous failure.
    """
    corrections = get_uninjected_corrections()
    if corrections:
        injected = patch_agents_md(corrections)
        mark_injected([entry["id"] for entry in corrections])
        logger.info("Injected %d new correction(s) into AGENTS.md", injected)

    return AGENTS_MD_PATH.read_text()
