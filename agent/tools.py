"""LangChain tools available to the DocAgent.

# HARNESS LAYER: Tool Orchestration
Every capability the model has is mediated by a tool defined here. The tools
also record what they did into a per-run context so the verification and
guardrail layers can audit the run afterwards.
"""

import ast
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CODEBASE_DIR = PROJECT_ROOT / "sample_codebase"
DOCS_DIR = PROJECT_ROOT / "docs"
OUTPUT_DIR = DOCS_DIR / "output"


# ---------------------------------------------------------------------------
# HARNESS LAYER: Tool Orchestration — per-run audit context
# The harness needs to know exactly which tools ran and which code snippets
# were actually fetched, so guardrails can reject fabricated code examples.
# A ContextVar keeps this safe under concurrent FastAPI requests.
# ---------------------------------------------------------------------------
@dataclass
class RunContext:
    run_id: str
    tools_called: list[dict] = field(default_factory=list)
    fetched_snippets: list[str] = field(default_factory=list)
    wrote_files: list[str] = field(default_factory=list)


_run_context: ContextVar[RunContext | None] = ContextVar("docagent_run_context", default=None)


def set_run_context(ctx: RunContext) -> None:
    _run_context.set(ctx)


def get_run_context() -> RunContext | None:
    return _run_context.get()


def _record_tool_call(tool_name: str, args: dict) -> None:
    ctx = get_run_context()
    if ctx is not None:
        ctx.tools_called.append({"tool": tool_name, "args": args})


# ---------------------------------------------------------------------------
# Tool 1: fetch_code_snippet
# ---------------------------------------------------------------------------
@tool
def fetch_code_snippet(file_path: str, function_name: str) -> str:
    """Fetch the exact source code of a function from the codebase.

    Args:
        file_path: Path of the source file relative to the codebase root,
            e.g. "payments.py" or "sample_codebase/payments.py".
        function_name: Name of the function (or class) to extract.

    Returns the verbatim source code including the docstring, or an error
    message if the file/function does not exist.
    """
    _record_tool_call("fetch_code_snippet", {"file_path": file_path, "function_name": function_name})

    # Normalize: accept "payments.py" or "sample_codebase/payments.py".
    rel = Path(file_path).name
    target = (SAMPLE_CODEBASE_DIR / rel).resolve()
    if not str(target).startswith(str(SAMPLE_CODEBASE_DIR.resolve())) or not target.exists():
        available = sorted(p.name for p in SAMPLE_CODEBASE_DIR.glob("*.py"))
        return f"ERROR: file '{file_path}' not found. Available files: {available}"

    source = target.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"ERROR: could not parse '{rel}': {exc}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == function_name:
            snippet = ast.get_source_segment(source, node) or ""
            # HARNESS LAYER: Guardrails (provenance) — remember every snippet
            # the model legitimately saw, so any code example in the final
            # output can be traced back to a real fetch.
            ctx = get_run_context()
            if ctx is not None:
                ctx.fetched_snippets.append(snippet)
            return f"# Source: {rel} :: {function_name}\n{snippet}"

    names = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return f"ERROR: '{function_name}' not found in '{rel}'. Defined names: {names}"


# ---------------------------------------------------------------------------
# Tool 2: search_existing_docs
# ---------------------------------------------------------------------------
@tool
def search_existing_docs(query: str) -> str:
    """Search existing documentation for relevant sections by keyword.

    Args:
        query: Free-text query, e.g. "payment refunds".

    Returns the best-matching doc files with a short excerpt, or a message
    saying nothing matched.
    """
    _record_tool_call("search_existing_docs", {"query": query})

    keywords = [w.lower() for w in query.split() if len(w) > 2]
    if not keywords:
        return "ERROR: query too short to search."

    scored: list[tuple[int, Path, str]] = []
    for doc in sorted(DOCS_DIR.glob("*.md")):
        text = doc.read_text()
        lower = text.lower()
        score = sum(lower.count(kw) for kw in keywords)
        if score > 0:
            scored.append((score, doc, text))

    if not scored:
        return f"No existing documentation matched '{query}'."

    scored.sort(key=lambda item: item[0], reverse=True)
    results = []
    for score, doc, text in scored[:3]:
        excerpt = text.strip()[:400]
        results.append(f"--- {doc.name} (score={score}) ---\n{excerpt}")
    return "\n\n".join(results)


# ---------------------------------------------------------------------------
# Tool 3: write_doc_section
# ---------------------------------------------------------------------------
@tool
def write_doc_section(section_name: str, content: str) -> str:
    """Persist a finished documentation section to the docs output folder.

    Args:
        section_name: Short slug for the section, e.g. "process_payment".
        content: The full markdown content of the section.

    Returns the path the section was written to.
    """
    _record_tool_call("write_doc_section", {"section_name": section_name})

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in section_name) or "section"
    path = OUTPUT_DIR / f"{slug}_{timestamp}.md"
    path.write_text(content)

    ctx = get_run_context()
    if ctx is not None:
        ctx.wrote_files.append(str(path))
    return f"Wrote section to {path}"


TOOLS = [fetch_code_snippet, search_existing_docs, write_doc_section]
