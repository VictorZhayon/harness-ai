"""Agentic loop orchestrator — the heart of the harness.

Implements the full harness flow, in order:
  1. Load AGENTS.md → inject learned corrections from the ledger
  2. Initialize the LangChain agent (tools + Gemini + system prompt)
  3. Run the agentic loop with the user request
  4. Intercept raw output → run the verification loop
  5. Verification fails → log to ledger → raise rejection (HTTP 422)
  6. Verification passes → run guardrails
  7. Guardrails fail → raise rejection (HTTP 422)
  8. Log the full run to the observability table
  9. Return verified output wrapped in the metadata envelope

Gemini is called ONLY here and in agent/verifier.py. The single model
instance created in _get_llm() is shared by both the agentic loop and the
self-critique call.
"""

import time
import uuid
from functools import lru_cache

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.guardrails import build_envelope, enforce_guardrails
from agent.guides import load_guide
from agent.tools import TOOLS, RunContext, set_run_context
from agent.verifier import verify_output
from harness.ledger import log_mistake
from harness.observability import log_run


class HarnessRejection(Exception):
    """Raised when the harness blocks output. Carries the 422 response body."""

    def __init__(self, detail: dict):
        self.detail = detail
        super().__init__(detail.get("reason", "harness rejection"))


@lru_cache(maxsize=1)
def _get_llm() -> ChatGoogleGenerativeAI:
    # The frozen model. One instance serves both the agentic loop here and
    # the self-critique call in verifier.py.
    return ChatGoogleGenerativeAI(model="gemini-2.0-flash", temperature=0.1)


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


def run_harnessed_agent(request: str, file_path: str, function_name: str) -> dict:
    run_id = str(uuid.uuid4())
    started = time.perf_counter()

    # HARNESS LAYER: Tool Orchestration — per-run audit context. Everything
    # the tools do during this run is recorded here for later inspection.
    ctx = RunContext(run_id=run_id)
    set_run_context(ctx)

    def _duration_ms() -> int:
        return int((time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------------
    # STEP 1 — HARNESS LAYER: Guide System
    # Load AGENTS.md with all learned corrections injected from the ledger.
    # ------------------------------------------------------------------
    guide = load_guide()

    # ------------------------------------------------------------------
    # STEP 2 — Initialize the LangChain agent: tools + Gemini + AGENTS.md
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
    executor = AgentExecutor(agent=agent, tools=TOOLS, max_iterations=10)

    # ------------------------------------------------------------------
    # STEP 3 — Run the agentic loop with the user request.
    # ------------------------------------------------------------------
    task = (
        f"Documentation request: {request}\n"
        f"Target file: {file_path}\n"
        f"Target function: {function_name}\n\n"
        "Follow your guide. Fetch the real source code before writing anything, "
        "check for existing docs, then produce the documentation section."
    )
    result = executor.invoke({"input": task})
    draft = _as_text(result.get("output", ""))

    # ------------------------------------------------------------------
    # STEP 4 — HARNESS LAYER: Verification Loop
    # Intercept the raw output; it does NOT go to the user yet.
    # ------------------------------------------------------------------
    verification = verify_output(llm, draft, ctx.fetched_snippets)

    # ------------------------------------------------------------------
    # STEP 5 — Verification failed → ledger + observability + 422.
    # ------------------------------------------------------------------
    if not verification.passed:
        if verification.hallucinated_names:
            failure_type = "hallucination"
            description = (
                f"Draft referenced names that do not exist in the codebase: "
                f"{', '.join(verification.hallucinated_names)} (request: {request!r})"
            )
            correction = (
                "Never mention functions or classes that were not returned by "
                f"fetch_code_snippet. Nonexistent names used: {', '.join(verification.hallucinated_names)}. "
                "Fetch and confirm every symbol before documenting it."
            )
        else:
            failure_type = "unverified_claim"
            description = (
                f"Self-critique confidence {verification.confidence:.2f} was below the 0.75 "
                f"threshold (request: {request!r}). Issues: {'; '.join(verification.issues) or 'none listed'}"
            )
            correction = (
                "Only state facts directly supported by the fetched code snippet. "
                "Do not infer behavior, defaults, or side effects the code does not show."
            )

        # HARNESS LAYER: Mistake Ledger — the failure becomes a lesson.
        log_mistake(failure_type, description, correction, run_id=run_id)

        # HARNESS LAYER: Observability — failures are logged like any run.
        log_run(
            run_id=run_id,
            input_request=request,
            tools_called=ctx.tools_called,
            verification_passed=False,
            confidence_score=verification.confidence,
            failure_type=failure_type,
            final_output=draft,
            duration_ms=_duration_ms(),
        )

        raise HarnessRejection(
            {
                "reason": "verification_failed",
                "failure_type": failure_type,
                "hallucinated_names": verification.hallucinated_names,
                "issues": verification.issues,
                **build_envelope(ctx, verification, output=None),
            }
        )

    # ------------------------------------------------------------------
    # STEP 6 — HARNESS LAYER: Guardrails
    # ------------------------------------------------------------------
    guardrails = enforce_guardrails(draft, ctx, verification)

    # ------------------------------------------------------------------
    # STEP 7 — Guardrails failed → observability + 422.
    # ------------------------------------------------------------------
    if not guardrails.passed:
        log_run(
            run_id=run_id,
            input_request=request,
            tools_called=ctx.tools_called,
            verification_passed=True,
            confidence_score=verification.confidence,
            failure_type="wrong_format",
            final_output=draft,
            duration_ms=_duration_ms(),
        )
        raise HarnessRejection(
            {
                "reason": "guardrails_failed",
                "failure_type": "wrong_format",
                "violations": guardrails.violations,
                **build_envelope(ctx, verification, output=None),
            }
        )

    # ------------------------------------------------------------------
    # STEP 8 — HARNESS LAYER: Observability — record the successful run.
    # ------------------------------------------------------------------
    log_run(
        run_id=run_id,
        input_request=request,
        tools_called=ctx.tools_called,
        verification_passed=True,
        confidence_score=verification.confidence,
        failure_type=None,
        final_output=draft,
        duration_ms=_duration_ms(),
    )

    # ------------------------------------------------------------------
    # STEP 9 — Return verified output inside the metadata envelope.
    # ------------------------------------------------------------------
    return build_envelope(ctx, verification, output=draft)
