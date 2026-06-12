"""Final output validation before anything leaves the system.

# HARNESS LAYER: Guardrails
Guardrails are the last gate. Even a verified draft is blocked if:
  - the verification step was skipped entirely, or
  - it contains code examples that cannot be traced back to the files
    actually fetched from GitHub this run.

Pure validation logic, deliberately decoupled: it imports nothing from the
model stack or GitHub integration. It receives plain strings/dicts and an
object with .passed/.confidence (the verifier's verdict) and judges them.
"""

import re
from dataclasses import dataclass, field


@dataclass
class GuardrailResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


def _extract_code_blocks(text: str) -> list[str]:
    """Pull the contents of all fenced code blocks out of the draft."""
    return [m.group(1) for m in re.finditer(r"```[a-zA-Z0-9_-]*\n(.*?)```", text, re.DOTALL)]


def _block_is_derived_from_files(block: str, fetched_files: dict[str, str]) -> bool:
    """A code block is legitimate if every non-trivial line in it appears in a
    file fetched from the repo. Comparing stripped lines tolerates
    re-indentation while still catching invented code."""
    file_lines: set[str] = set()
    for content in fetched_files.values():
        file_lines.update(line.strip() for line in content.splitlines() if line.strip())

    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        if stripped not in file_lines:
            return False
    return True


# ---------------------------------------------------------------------------
# HARNESS LAYER: Guardrails — main gate
# ---------------------------------------------------------------------------
def enforce_guardrails(draft: str, fetched_files: dict[str, str], verification) -> GuardrailResult:
    violations: list[str] = []

    # Gate 1: verification must have actually run. A missing verdict means a
    # code path skipped the verifier — fail closed.
    if verification is None:
        violations.append("Verification step was skipped; output cannot be released.")

    # Gate 2: every code example must be derived from a fetched file.
    code_blocks = _extract_code_blocks(draft)
    if code_blocks and not fetched_files:
        violations.append(
            "Output contains code blocks but no files were fetched from the repo — "
            "code examples are fabricated."
        )
    else:
        for i, block in enumerate(code_blocks):
            if not _block_is_derived_from_files(block, fetched_files):
                violations.append(
                    f"Code block #{i + 1} contains lines that do not appear in any "
                    "fetched file — possible fabricated example."
                )

    return GuardrailResult(passed=not violations, violations=violations)


# ---------------------------------------------------------------------------
# HARNESS LAYER: Guardrails — metadata envelope
# Every API response carries machine-readable provenance, so consumers can
# always tell whether output was verified and how it was produced.
# ---------------------------------------------------------------------------
def build_envelope(
    run_id: str,
    tools_used: list[str],
    verification,
    pr_url: str | None,
    output: str | None,
) -> dict:
    return {
        "verified": bool(verification and verification.passed),
        "confidence": verification.confidence if verification else 0.0,
        "tools_used": tools_used,
        "run_id": run_id,
        "pr_url": pr_url,
        "output": output,
    }
