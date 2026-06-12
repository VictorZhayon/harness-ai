"""Final output validation before anything reaches the API response.

# HARNESS LAYER: Guardrails
Guardrails are the last gate. Even a verified draft is blocked if:
  - it contains code blocks that cannot be traced back to a real
    fetch_code_snippet call (fabricated examples), or
  - the verification step was skipped entirely.

This module is pure validation logic — it never calls a model.
"""

import re
from dataclasses import dataclass, field

from agent.tools import RunContext
from agent.verifier import VerificationResult


@dataclass
class GuardrailResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


def _extract_code_blocks(text: str) -> list[str]:
    """Pull the contents of all fenced code blocks out of the draft."""
    return [m.group(1) for m in re.finditer(r"```[a-zA-Z0-9_-]*\n(.*?)```", text, re.DOTALL)]


def _block_is_derived_from_snippets(block: str, fetched_snippets: list[str]) -> bool:
    """A code block is legitimate if every non-trivial line in it appears in a
    snippet the agent actually fetched. Comparing stripped lines tolerates
    re-indentation while still catching invented code."""
    snippet_lines = set()
    for snippet in fetched_snippets:
        snippet_lines.update(line.strip() for line in snippet.splitlines() if line.strip())

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped not in snippet_lines:
            return False
    return True


# ---------------------------------------------------------------------------
# HARNESS LAYER: Guardrails — main gate
# ---------------------------------------------------------------------------
def enforce_guardrails(
    draft: str,
    ctx: RunContext,
    verification: VerificationResult | None,
) -> GuardrailResult:
    violations: list[str] = []

    # Gate 1: verification must have actually run. A missing result means a
    # code path skipped the verifier — fail closed.
    if verification is None:
        violations.append("Verification step was skipped; output cannot be released.")

    # Gate 2: every code block must be derived from a real fetched snippet.
    code_blocks = _extract_code_blocks(draft)
    if code_blocks and not ctx.fetched_snippets:
        violations.append(
            "Output contains code blocks but fetch_code_snippet was never called — "
            "code examples are fabricated."
        )
    else:
        for i, block in enumerate(code_blocks):
            if not _block_is_derived_from_snippets(block, ctx.fetched_snippets):
                violations.append(
                    f"Code block #{i + 1} contains lines that do not appear in any "
                    "fetched snippet — possible fabricated example."
                )

    return GuardrailResult(passed=not violations, violations=violations)


# ---------------------------------------------------------------------------
# HARNESS LAYER: Guardrails — metadata envelope
# Every API response carries machine-readable provenance, so consumers can
# always tell whether output was verified and how it was produced.
# ---------------------------------------------------------------------------
def build_envelope(
    ctx: RunContext,
    verification: VerificationResult | None,
    output: str | None,
) -> dict:
    return {
        "verified": bool(verification and verification.passed),
        "confidence": verification.confidence if verification else 0.0,
        "tools_used": [call["tool"] for call in ctx.tools_called],
        "run_id": ctx.run_id,
        "output": output,
    }
