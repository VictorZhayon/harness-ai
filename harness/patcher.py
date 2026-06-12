"""Patches AGENTS.md with corrections learned from past failures.

# HARNESS LAYER: Mistake Ledger (self-correction loop)
The patcher closes the loop: failures recorded in the mistake ledger become
permanent instructions in the agent's guide. The agent literally cannot start
a run without reading its own accumulated corrections.

Pure file manipulation — no model calls, no Supabase calls. The ledger decides
*what* to inject; the patcher only knows *how*.
"""

from pathlib import Path

AGENTS_MD_PATH = Path(__file__).resolve().parent.parent / "AGENTS.md"

CORRECTIONS_HEADER = "## Learned Corrections"


def _format_correction(entry: dict) -> str:
    failure_type = entry.get("failure_type", "unknown")
    correction = (entry.get("correction") or "").strip()
    description = (entry.get("description") or "").strip()
    line = f"- **[{failure_type}]** {correction}"
    if description:
        line += f" _(from failure: {description})_"
    return line


def patch_agents_md(corrections: list[dict], path: Path = AGENTS_MD_PATH) -> int:
    """Append corrections as bullets under the '## Learned Corrections'
    section. Returns the number of corrections injected."""
    if not corrections:
        return 0

    text = path.read_text()
    if CORRECTIONS_HEADER not in text:
        # Guide was edited and lost the section — recreate it at the end so
        # corrections are never silently dropped.
        text = text.rstrip() + f"\n\n{CORRECTIONS_HEADER}\n"

    lines = text.splitlines()
    header_idx = next(i for i, line in enumerate(lines) if line.strip() == CORRECTIONS_HEADER)

    # The section ends at the next "## " header or end of file.
    end_idx = len(lines)
    for i in range(header_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            end_idx = i
            break

    new_bullets = [_format_correction(entry) for entry in corrections]
    # Insert at the end of the section, keeping existing bullets in order.
    while end_idx > header_idx + 1 and not lines[end_idx - 1].strip():
        end_idx -= 1
    lines[end_idx:end_idx] = new_bullets

    path.write_text("\n".join(lines) + "\n")
    return len(new_bullets)
