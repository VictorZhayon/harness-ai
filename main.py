"""DocAgent Harness — FastAPI entrypoint.

The API surface of the harness. Endpoints never touch the model directly;
they go through the runner (which owns the full harness flow) or read the
harness's own stores (ledger, observability).
"""

import logging

from dotenv import load_dotenv

load_dotenv()  # must run before any module reads env vars

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.runner import HarnessRejection, run_harnessed_agent
from harness.ledger import VALID_FAILURE_TYPES, get_all_entries, log_mistake
from harness.observability import get_recent_runs
from harness.patcher import patch_agents_md
from harness.ledger import mark_injected

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(
    title="DocAgent Harness",
    description="A documentation agent wrapped in a full outer harness: "
    "guide system, mistake ledger, verification loop, guardrails, observability.",
    version="1.0.0",
)


class GenerateDocsRequest(BaseModel):
    request: str = Field(..., description="What documentation to produce")
    file_path: str = Field(..., description="Source file, e.g. 'payments.py'")
    function_name: str = Field(..., description="Function to document, e.g. 'process_payment'")


class CorrectionRequest(BaseModel):
    run_id: str
    failure_type: str
    description: str
    correction: str


@app.post("/generate-docs")
def generate_docs(body: GenerateDocsRequest):
    """Run the fully harnessed agent and return verified documentation."""
    try:
        return run_harnessed_agent(body.request, body.file_path, body.function_name)
    except HarnessRejection as rejection:
        # HARNESS LAYER: Guardrails — blocked output never reaches the user
        # as a success; the caller gets a structured explanation instead.
        raise HTTPException(status_code=422, detail=rejection.detail)


@app.get("/runs")
def runs():
    """Last 20 agent runs from the observability log."""
    # HARNESS LAYER: Observability
    return {"runs": get_recent_runs(limit=20)}


@app.get("/ledger")
def ledger():
    """All mistake ledger entries, newest first."""
    # HARNESS LAYER: Mistake Ledger
    return {"entries": get_all_entries()}


@app.post("/ledger/correct")
def ledger_correct(body: CorrectionRequest):
    """Manually log a correction and patch it into AGENTS.md immediately."""
    if body.failure_type not in VALID_FAILURE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"failure_type must be one of {sorted(VALID_FAILURE_TYPES)}",
        )

    # HARNESS LAYER: Mistake Ledger — record the human-supplied correction.
    entry = log_mistake(
        failure_type=body.failure_type,
        description=body.description,
        correction=body.correction,
        run_id=body.run_id,
    )

    # HARNESS LAYER: Guide System — patch AGENTS.md right away so the very
    # next run already benefits from the correction.
    injected = patch_agents_md([entry])
    if entry.get("id") is not None:
        mark_injected([entry["id"]])

    return {"logged": entry, "injected_into_agents_md": injected == 1}
