"""DocAgent Harness (GitHub Edition) — FastAPI entrypoint.

The API surface of the harness. Endpoints never touch the model or GitHub
directly; they go through the runner (which owns the full harness flow) or
read the harness's own stores (ledger, observability).
"""

import logging

from dotenv import load_dotenv

load_dotenv()  # must run before any module reads env vars

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.runner import HarnessRejection, run_harnessed_agent
from github_integration.client import GitHubIntegrationError
from harness.ledger import VALID_FAILURE_TYPES, get_all_entries, log_mistake, mark_injected
from harness.observability import get_recent_runs
from harness.patcher import patch_agents_md

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(
    title="DocAgent Harness (GitHub Edition)",
    description="A documentation agent wrapped in a full outer harness: "
    "guide system, mistake ledger, verification loop, guardrails, "
    "observability — publishing verified docs as GitHub pull requests.",
    version="2.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("static/index.html")


class GenerateDocsRequest(BaseModel):
    repo_full_name: str = Field(..., description="GitHub repo, e.g. 'owner/repo-name'")
    file_paths: list[str] = Field(default_factory=list, description="Files to document")
    auto_crawl: bool = Field(False, description="Ignore file_paths and crawl the repo tree")


class CorrectionRequest(BaseModel):
    run_id: str
    failure_type: str
    description: str
    correction: str


@app.post("/generate-docs")
def generate_docs(body: GenerateDocsRequest):
    """Run the fully harnessed agent; returns the metadata envelope with the
    docs PR URL on success."""
    if not body.auto_crawl and not body.file_paths:
        raise HTTPException(status_code=422, detail="Provide file_paths or set auto_crawl=true.")

    try:
        return run_harnessed_agent(body.repo_full_name, body.file_paths, body.auto_crawl)
    except HarnessRejection as rejection:
        # HARNESS LAYER: Guardrails — blocked output never reaches the user
        # as a success; the caller gets a structured explanation instead.
        raise HTTPException(status_code=422, detail=rejection.detail)
    except GitHubIntegrationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


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

    # HARNESS LAYER: Guides — patch AGENTS.md right away so the very next
    # run already benefits from the correction.
    injected = patch_agents_md([entry])
    if entry.get("id") is not None:
        mark_injected([entry["id"]])

    return {"logged": entry, "injected_into_agents_md": injected == 1}
