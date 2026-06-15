"""Agentic loop orchestrator — the heart of the harness.

Implements the full harness flow, in order:
   1. Load AGENTS.md → inject learned corrections from the ledger
   2. Fetch requested files from GitHub → store in run-scoped memory
   3. Initialize the LangChain agent (tools + Gemini + system prompt)
   4. Run the agentic loop: agent calls tools, stages doc sections in memory
   5. Intercept raw output → run the verification loop
   6. Verification fails → log to ledger → raise rejection (HTTP 422)
   7. Run guardrails → fail → raise rejection (HTTP 422)
   8. Call pr_writer.create_docs_pr() → get the PR URL
   9. Log the full run to the observability table
  10. Return the metadata envelope with the PR URL

Gemini is called ONLY here and in agent/verifier.py. The single model
instance created in _get_llm() is shared by both the agentic loop and the
self-critique call. GitHub is touched only via github_integration/.
"""

import time
import uuid
from functools import lru_cache

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.guides import load_guide
from agent.tools import TOOLS, RunContext, set_run_context
from agent.verifier import verify_output
from github_integration.fetcher import crawl_repo_tree, fetch_files
from github_integration.pr_writer import create_docs_pr
from harness.guardrails import build_envelope, enforce_guardrails
from harness.ledger import log_mistake
from harness.observability import log_run

# Safety valve for auto_crawl on large repos: documenting an unbounded number
# of files in one run would blow the context window and the GitHub rate limit.
MAX_AUTO_CRAWL_FILES = 10


class HarnessRejection(Exception):
    """Raised when the harness blocks output. Carries the 422 response body."""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("reason", "harness rejection"))


@lru_cache(maxsize=1)
def _get_llm() -> ChatGoogleGenerativeAI:
    # The frozen model. One instance serves both the agentic loop here and
    # the self-critique call in verifier.py.
    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.1)


def _as_text(output) -> str:
    """Gemini can return content as a list of parts; normalize to a string."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for part in output:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(part["text"])
        return "".join(parts)
    return str(output)


def run_harnessed_agent(
    repo_full_name: str,
    file_paths: list[str],
    auto_crawl: bool = False,
) -> dict:
    run_id = str(uuid.uuid4())
    started = time.perf_counter()

    # HARNESS LAYER: Tool Orchestration — run-scoped memory + audit context.
    ctx = RunContext(run_id=run_id)
    set_run_context(ctx)

    def _duration_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    def _log(verification_passed: bool, confidence: float, failure_type: str | None, pr_url: str | None) -> None:
        # HARNESS LAYER: Observability — every path through the harness logs.
        log_run(
            run_id=run_id,
            repo_full_name=repo_full_name,
            files_requested=file_paths,
            files_fetched=sorted(ctx.fetched_files),
            tools_called=ctx.tools_called,
            verification_passed=verification_passed,
            confidence_score=confidence,
            failure_type=failure_type,
            pr_url=pr_url,
            duration_ms=_duration_ms(),
        )

    # ------------------------------------------------------------------
    # STEP 1 — HARNESS LAYER: Guides
    # Load AGENTS.md with all learned corrections injected from the ledger.
    # ------------------------------------------------------------------
    guide = load_guide()

    # ------------------------------------------------------------------
    # STEP 2 — GITHUB INTEGRATION: File Fetcher
    # One fetch per run; the agent's tools work on these in-memory copies
    # and never reach GitHub themselves.
    # ------------------------------------------------------------------
    if auto_crawl:
        file_paths = crawl_repo_tree(repo_full_name)[:MAX_AUTO_CRAWL_FILES]
    ctx.fetched_files = fetch_files(repo_full_name, file_paths)

    # ------------------------------------------------------------------
    # STEP 3 — Initialize the LangChain agent: tools + Gemini + AGENTS.md
    # as the system prompt. Braces are escaped so guide content is never
    # mistaken for template variables.
    # ------------------------------------------------------------------
    llm = _get_llm()
    system_prompt = guide.replace("{", "{{").replace("}", "}}")
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )
    agent = create_tool_calling_agent(llm, TOOLS, prompt)
    executor = AgentExecutor(agent=agent, tools=TOOLS, max_iterations=15)

    # ------------------------------------------------------------------
    # STEP 4 — Run the agentic loop. The agent fetches snippets, checks
    # existing docs, and stages sections via write_doc_section.
    # ------------------------------------------------------------------
    file_list = "\n".join(f"- {p}" for p in ctx.fetched_files)
    task = (
        f"Generate API documentation for repository '{repo_full_name}'.\n"
        f"The following files have been fetched and are available to your tools:\n"
        f"{file_list}\n\n"
        "Follow your guide. For each file: inspect its functions with "
        "fetch_code_snippet, check search_existing_docs for overlap, then stage "
        "one section per file with write_doc_section. Finish with a short "
        "summary of what you documented."
    )
    result = executor.invoke({"input": task})
    summary = _as_text(result.get("output", ""))

    # Everything headed for the PR must be verified — the staged sections are
    # the real deliverable, the summary is the cover letter.
    draft = summary
    if ctx.staged_docs:
        draft = "\n\n".join([summary, *ctx.staged_docs.values()])
    else:
        # Agent answered inline without staging: the summary becomes the doc,
        # so the PR is never empty.
        ctx.staged_docs["generated_documentation"] = summary

    # ------------------------------------------------------------------
    # STEP 5 — HARNESS LAYER: Verification Loop
    # Intercept the raw output; nothing goes to GitHub or the user yet.
    # ------------------------------------------------------------------
    verification = verify_output(llm, draft, ctx.fetched_files)

    # ------------------------------------------------------------------
    # STEP 6 — Verification failed → ledger + observability + 422.
    # ------------------------------------------------------------------
    if not verification.passed:
        if verification.hallucinated_names:
            failure_type = "hallucination"
            description = (
                f"Draft for {repo_full_name} referenced names that exist in none of the "
                f"fetched files: {', '.join(verification.hallucinated_names)}"
            )
            correction = (
                "Never mention functions or classes that do not appear in the fetched "
                f"files. Nonexistent names used: {', '.join(verification.hallucinated_names)}. "
                "Confirm every symbol with fetch_code_snippet before documenting it."
            )
        else:
            failure_type = "unverified_claim"
            description = (
                f"Self-critique confidence {verification.confidence:.2f} for {repo_full_name} "
                f"was below the 0.75 threshold. Issues: {'; '.join(verification.issues) or 'none listed'}"
            )
            correction = (
                "Only state facts directly supported by the fetched code. Do not infer "
                "behavior, defaults, or side effects the code does not show."
            )

        # HARNESS LAYER: Mistake Ledger — the failure becomes a lesson.
        log_mistake(failure_type, description, correction, run_id=run_id)
        _log(False, verification.confidence, failure_type, pr_url=None)

        raise HarnessRejection(
            {
                "reason": "verification_failed",
                "failure_type": failure_type,
                "hallucinated_names": verification.hallucinated_names,
                "issues": verification.issues,
                **build_envelope(
                    run_id, [c["tool"] for c in ctx.tools_called], verification, pr_url=None, output=None
                ),
            }
        )

    # ------------------------------------------------------------------
    # STEP 7 — HARNESS LAYER: Guardrails
    # ------------------------------------------------------------------
    guardrails = enforce_guardrails(draft, ctx.fetched_files, verification)
    if not guardrails.passed:
        _log(True, verification.confidence, "wrong_format", pr_url=None)
        raise HarnessRejection(
            {
                "reason": "guardrails_failed",
                "failure_type": "wrong_format",
                "violations": guardrails.violations,
                **build_envelope(
                    run_id, [c["tool"] for c in ctx.tools_called], verification, pr_url=None, output=None
                ),
            }
        )

    # ------------------------------------------------------------------
    # STEP 8 — GITHUB INTEGRATION: PR Writer
    # Only verified, guardrail-clean sections ever reach a pull request.
    # ------------------------------------------------------------------
    pr_url = create_docs_pr(
        repo_full_name,
        ctx.staged_docs,
        run_id,
        confidence=verification.confidence,
        files_documented=sorted(ctx.fetched_files),
    )

    # ------------------------------------------------------------------
    # STEP 9 — HARNESS LAYER: Observability — record the successful run.
    # ------------------------------------------------------------------
    _log(True, verification.confidence, None, pr_url=pr_url)

    # ------------------------------------------------------------------
    # STEP 10 — Return the metadata envelope with the PR URL.
    # ------------------------------------------------------------------
    return build_envelope(
        run_id, [c["tool"] for c in ctx.tools_called], verification, pr_url=pr_url, output=summary
    )
