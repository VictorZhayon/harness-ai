"""LangChain tools available to the DocAgent.

# HARNESS LAYER: Tool Orchestration
Every capability the model has is mediated by a tool defined here. Tools
operate on the run-scoped memory (files already fetched from GitHub in step 2
of the harness flow) — the agent itself can never reach GitHub directly. The
tools also record an audit trail so verification and guardrails can inspect
exactly what the agent saw and did.
"""

import ast
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.tools import tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_DOCS_DIR = PROJECT_ROOT / "docs" / "generated"


# ---------------------------------------------------------------------------
# HARNESS LAYER: Tool Orchestration — run-scoped memory + audit context
# fetched_files holds the GitHub file contents for this run; staged_docs
# accumulates the sections the agent writes, to be handed to the PR writer
# only after verification and guardrails pass. A ContextVar keeps this safe
# under concurrent FastAPI requests.
# ---------------------------------------------------------------------------
@dataclass
class RunContext:
    run_id: str
    fetched_files: dict[str, str] = field(default_factory=dict)
    staged_docs: dict[str, str] = field(default_factory=dict)
    tools_called: list[dict] = field(default_factory=list)


_run_context: ContextVar[RunContext | None] = ContextVar("docagent_run_context", default=None)


def set_run_context(ctx: RunContext) -> None:
    _run_context.set(ctx)


def get_run_context() -> RunContext | None:
    return _run_context.get()


def _record_tool_call(tool_name: str, args: dict) -> None:
    ctx = get_run_context()
    if ctx is not None:
        ctx.tools_called.append({"tool": tool_name, "args": args})


def _resolve_file(ctx: RunContext, file_path: str) -> tuple[str, str] | None:
    """Find a fetched file by exact path, then by suffix/basename so the
    agent can say 'payments.py' for 'src/payments.py'."""
    if file_path in ctx.fetched_files:
        return file_path, ctx.fetched_files[file_path]
    matches = [p for p in ctx.fetched_files if p.endswith("/" + file_path) or Path(p).name == file_path]
    if len(matches) == 1:
        return matches[0], ctx.fetched_files[matches[0]]
    return None


def _extract_python(source: str, function_name: str) -> str | None:
    """Extract a function/class from Python source via AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == function_name:
            return ast.get_source_segment(source, node)
    return None


def _list_python_symbols(source: str) -> list[str]:
    """Return all top-level function and class names in a Python file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _list_js_symbols(source: str) -> list[str]:
    """Return function/class names found in a JS/TS file via declaration scan."""
    patterns = [
        re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", re.MULTILINE),
        re.compile(r"^[ \t]*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b", re.MULTILINE),
        re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(", re.MULTILINE),
        re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?function\b", re.MULTILINE),
    ]
    names: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in pat.finditer(source):
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _extract_js_like(source: str, function_name: str) -> str | None:
    """Extract a function/class from JS/TS source via declaration match +
    brace counting. Naive, but good enough to ground documentation."""
    name = re.escape(function_name)
    pattern = re.compile(
        rf"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
        rf"(?:function\s+{name}\s*\(|class\s+{name}\b|(?:const|let|var)\s+{name}\s*=)",
        re.MULTILINE,
    )
    match = pattern.search(source)
    if not match:
        return None
    brace = source.find("{", match.start())
    if brace == -1:
        return source[match.start():].split("\n", 1)[0]
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start(): i + 1]
    return source[match.start(): match.start() + 2000]


# ---------------------------------------------------------------------------
# Tool 1: fetch_code_snippet — reads from run memory, never from GitHub
# ---------------------------------------------------------------------------
@tool
def fetch_code_snippet(file_path: str, function_name: str) -> str:
    """Get the exact source code of a function or class from a fetched file.

    Args:
        file_path: Path of the file as fetched from the repo, e.g.
            "src/payments.py".
        function_name: Name of the function or class to extract.

    Returns the verbatim source code, or an error listing what is available.
    """
    _record_tool_call("fetch_code_snippet", {"file_path": file_path, "function_name": function_name})

    ctx = get_run_context()
    if ctx is None or not ctx.fetched_files:
        return "ERROR: no files have been fetched for this run."

    resolved = _resolve_file(ctx, file_path)
    if resolved is None:
        return f"ERROR: '{file_path}' was not fetched. Fetched files: {sorted(ctx.fetched_files)}"
    real_path, source = resolved

    if real_path.endswith(".py"):
        snippet = _extract_python(source, function_name)
    else:
        snippet = _extract_js_like(source, function_name)

    if snippet is None:
        if real_path.endswith(".py"):
            available = _list_python_symbols(source)
        else:
            available = _list_js_symbols(source)
        names_str = ", ".join(available) if available else "(none found)"
        return (
            f"ERROR: '{function_name}' not found in '{real_path}'. "
            f"Available symbols: {names_str}"
        )
    return f"# Source: {real_path} :: {function_name}\n{snippet}"


# ---------------------------------------------------------------------------
# Tool 2: search_existing_docs
# ---------------------------------------------------------------------------
@tool
def search_existing_docs(query: str) -> str:
    """Search previously generated documentation for relevant sections.

    Args:
        query: Free-text query, e.g. "payment refunds".

    Returns the best-matching doc files with a short excerpt, or a message
    saying nothing matched.
    """
    _record_tool_call("search_existing_docs", {"query": query})

    keywords = [w.lower() for w in query.split() if len(w) > 2]
    if not keywords:
        return "ERROR: query too short to search."

    GENERATED_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    scored: list[tuple[int, Path, str]] = []
    for doc in sorted(GENERATED_DOCS_DIR.glob("*.md")):
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
        results.append(f"--- {doc.name} (score={score}) ---\n{text.strip()[:400]}")
    return "\n\n".join(results)


# ---------------------------------------------------------------------------
# Tool 3: write_doc_section — stages in memory; the PR writer publishes it
# only after the harness has verified the whole run.
# ---------------------------------------------------------------------------
@tool
def write_doc_section(section_name: str, content: str) -> str:
    """Stage a finished documentation section for the docs pull request.

    Args:
        section_name: Short slug for the section, e.g. "process_payment".
        content: The full markdown content of the section.

    Returns a confirmation. Sections are committed to GitHub only after the
    run passes verification.
    """
    _record_tool_call("write_doc_section", {"section_name": section_name})

    ctx = get_run_context()
    if ctx is None:
        return "ERROR: no active run."
    ctx.staged_docs[section_name] = content
    return f"Staged section '{section_name}' ({len(content)} chars) for the docs PR."


TOOLS = [fetch_code_snippet, search_existing_docs, write_doc_section]
