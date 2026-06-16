"""Verification loop for agent output.

# HARNESS LAYER: Verification Loop
The model's draft is never trusted. Two independent checks run on every draft:
  1. A deterministic hallucination check against the files actually fetched
     from GitHub this run (no model involved).
  2. A self-critique call to Gemini that scores confidence and lists issues.

NOTE: This module never constructs a Gemini client. The runner owns the model
instance and passes it in, so all API access stays in runner.py/verifier.py.
It also never touches GitHub — it only sees the in-memory fetched contents.
"""

import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

CONFIDENCE_THRESHOLD = 0.75

# Cap how much fetched code goes into the critique prompt; enough to ground
# the review without blowing the context on large auto-crawled repos.
MAX_CRITIQUE_CODE_CHARS = 24_000

SELF_CRITIQUE_PROMPT = (
    "Review this documentation draft. Identify any claims that cannot be "
    "verified from the provided code. Return JSON only, no markdown: "
    '{confidence: float, issues: list[str]}'
)

# Common identifiers that are *not* hallucinations even though they may not
# appear in the fetched files (builtins, stdlib, typing, doc boilerplate).
_KNOWN_SAFE_NAMES = {
    "print", "len", "str", "int", "float", "bool", "dict", "list", "set",
    "tuple", "range", "type", "isinstance", "enumerate", "zip", "sorted",
    "open", "repr", "hash", "format", "datetime", "timedelta", "uuid",
    "uuid4", "optional", "any", "union", "valueerror", "typeerror",
    "keyerror", "exception", "runtimeerror", "permissionerror", "none",
    "true", "false", "null", "undefined", "console", "promise", "json",
    "raises", "returns", "args", "kwargs", "self", "init", "main",
    # Agent tool names that may appear in prose descriptions
    "fetch_code_snippet", "write_doc_section", "search_existing_docs",
    # Common tech/language names that match CamelCase but are not code symbols
    "javascript", "typescript", "github", "markdown", "fastapi", "langchain",
    "gemini", "python", "chromadb", "openai", "pydantic", "uvicorn",
}


@dataclass
class VerificationResult:
    passed: bool
    confidence: float
    hallucinated_names: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HARNESS LAYER: Verification Loop — Step 1: deterministic hallucination check
# ---------------------------------------------------------------------------
def _extract_mentioned_names(text: str) -> set[str]:
    """Pull function/class-looking identifiers out of the draft.

    Heuristic: identifiers that are called like `name(...)`, wrapped in
    backticks, or bare CamelCase class references — and that look like code
    symbols (snake_case with an underscore, or CamelCase). Plain English
    words are ignored.
    """
    candidates: set[str] = set()
    candidates.update(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    candidates.update(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`", text))
    # Bare CamelCase words (two+ humps) read as class references even when
    # not called or backticked, e.g. "the MagicHelper class".
    candidates.update(re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", text))

    def looks_like_symbol(name: str) -> bool:
        if name.lower() in _KNOWN_SAFE_NAMES:
            return False
        snake = "_" in name
        camel = name[0].isupper() and any(c.islower() for c in name) and not name.isupper()
        return snake or camel

    return {n for n in candidates if looks_like_symbol(n)}


def check_hallucinated_names(draft: str, fetched_files: dict[str, str]) -> list[str]:
    """Return symbols mentioned in the draft that appear in none of the
    fetched files. A word-boundary search over real file contents works
    uniformly across Python, TypeScript, and JavaScript."""
    mentioned = _extract_mentioned_names(draft)
    hallucinated = []
    for name in mentioned:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if not any(pattern.search(content) for content in fetched_files.values()):
            hallucinated.append(name)
    return sorted(hallucinated)


# ---------------------------------------------------------------------------
# HARNESS LAYER: Verification Loop — Step 2: model self-critique
# ---------------------------------------------------------------------------
def _parse_critique_json(raw: str) -> tuple[float, list[str]]:
    """Parse {confidence, issues} from the critique response, tolerating
    markdown fences. An unparseable critique is treated as zero confidence —
    the harness fails closed, never open."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
        confidence = float(data.get("confidence", 0.0))
        issues = [str(i) for i in data.get("issues", [])]
        return confidence, issues
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0, [f"Self-critique response was not valid JSON: {raw[:200]}"]


def run_self_critique(llm, draft: str, fetched_files: dict[str, str]) -> tuple[float, list[str]]:
    """Second Gemini call: ask the model to audit the draft against the
    actual code fetched from the repository."""
    chunks: list[str] = []
    budget = MAX_CRITIQUE_CODE_CHARS
    for path, content in fetched_files.items():
        portion = content[: max(budget, 0)]
        chunks.append(f"### {path}\n{portion}")
        budget -= len(portion)
        if budget <= 0:
            chunks.append("(remaining files truncated)")
            break
    code = "\n\n".join(chunks) if chunks else "(no files were fetched)"

    message = (
        f"{SELF_CRITIQUE_PROMPT}\n\n"
        f"--- FETCHED CODE ---\n{code}\n\n"
        f"--- DOCUMENTATION DRAFT ---\n{draft}"
    )
    response = llm.invoke([HumanMessage(content=message)])
    content = response.content if isinstance(response.content, str) else str(response.content)
    return _parse_critique_json(content)


# ---------------------------------------------------------------------------
# HARNESS LAYER: Verification Loop — combined verdict
# ---------------------------------------------------------------------------
def verify_output(llm, draft: str, fetched_files: dict[str, str]) -> VerificationResult:
    """Run both verification steps and return a single verdict.

    Rejects when hallucinated names are found OR self-critique confidence
    is below the threshold (0.75).
    """
    hallucinated = check_hallucinated_names(draft, fetched_files)
    confidence, issues = run_self_critique(llm, draft, fetched_files)

    passed = not hallucinated and confidence >= CONFIDENCE_THRESHOLD
    return VerificationResult(
        passed=passed,
        confidence=confidence,
        hallucinated_names=hallucinated,
        issues=issues,
    )
