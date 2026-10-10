"""Shared interface contracts for the CloudMorph pipeline.

Every module imports its input/output types from here so that
analyzer -> deployer -> healer -> deployer stay decoupled.

    [analyzer.analyze] --AnalysisResult--> [deployer.deploy] --DeployResult-->
        success? -> done
        failure? -> [healer.heal] --(patched dockerfile)--> [deployer.deploy] ... (<= MAX_RETRIES)

Change this file only after team agreement (it is the contract).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

# Upper bound on heal -> redeploy cycles. Caps API cost and prevents infinite loops.
MAX_RETRIES = 3

# node = SSH-reachable Docker host in the node pool (any cloud); see docs/node-pool-fleet.md
DeployTarget = Literal["local", "cloudrun", "node", "function"]  # function = Cloud Run functions (gen2)
# Cloud service models an app can run on (IaaS / container / PaaS / function)
ServiceModel = Literal["iaas", "caas", "paas", "faas"]


# --------------------------------------------------------------------------- #
# analyzer.py (이요환) -> deployer.py / main.py
# --------------------------------------------------------------------------- #
class EnvRequirement(BaseModel):
    """A value only the user can supply: a build input (private registry token …) or a runtime env var."""

    name: str
    scope: Literal["build", "runtime"] = Field(
        description="build: passed to docker build (secret -> BuildKit --secret, else --build-arg); "
        "runtime: container env"
    )
    secret: bool = Field(description="Token/password. Never echoed in events or logs")
    generate: bool = Field(default=False, description="Runtime secret that may be random: blank -> generated")
    reason: str = Field(default="", description="What the value is for")
    resource: str | None = Field(
        default=None,
        description="postgres|mysql|redis|s3 when this is a connection setting of an external store "
        "(the user may point it at their own server)",
    )
    auto: bool = Field(default=False, description="Left blank, the deployer provisions it (set by main.py)")
    evidence: list[str] = Field(default_factory=list, description="'path:line' where the code reads it")


class AnalysisResult(BaseModel):
    """Static analysis output: where and how to deploy the user's app."""

    target: DeployTarget = Field(description="Recommended deploy target")
    service_models: list[ServiceModel] = Field(
        default_factory=list,
        description="Every service model the code can run on, most preferred first (caas > paas > faas > iaas). "
        "Static sites are wrapped in an nginx container, so they get caas and iaas.",
    )
    language: str = Field(description="e.g. 'python', 'node', 'java'")
    framework: str | None = Field(default=None, description="e.g. 'fastapi', 'flask', 'express'")
    port: int = Field(default=8080, description="Port the app should listen on inside the container")
    entrypoint: str | None = Field(default=None, description="e.g. 'uvicorn main:app'")
    dockerfile: str = Field(description="Generated initial Dockerfile content (full text)")
    notes: list[str] = Field(default_factory=list, description="Human-readable reasons for the decisions")
    required_env: list[EnvRequirement] = Field(
        default_factory=list, description="Values to ask the user for before deploying"
    )


# --------------------------------------------------------------------------- #
# deployer.py (전동훈) -> healer.py / main.py
# --------------------------------------------------------------------------- #
class DeployResult(BaseModel):
    """Outcome of a single build+run attempt on one target."""

    success: bool
    target: DeployTarget
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    url: str | None = Field(default=None, description="Live public URL when success is True")
    duration_sec: float | None = None


# --------------------------------------------------------------------------- #
# healer.py (박재현)
# --------------------------------------------------------------------------- #
class ErrorCategory(str, Enum):
    PORT_BINDING = "port_binding"
    MISSING_DEPENDENCY = "missing_dependency"
    BAD_ENTRYPOINT = "bad_entrypoint"
    BUILD_FAILURE = "build_failure"
    UNKNOWN = "unknown"


class PatchRecord(BaseModel):
    """One heal attempt: what was wrong, what changed, and why."""

    attempt: int
    category: ErrorCategory
    error_excerpt: str = Field(description="The stderr lines that triggered this patch")
    before: str = Field(description="Dockerfile before the patch")
    after: str = Field(description="Dockerfile after the patch")
    diff: str = Field(description="Unified diff (before -> after)")
    rationale: str = Field(description="Why this patch should fix the error")
    source: Literal["rule", "llm"] = "rule"


class HealState(TypedDict, total=False):
    """LangGraph state for the self-healing loop."""

    src_dir: str
    target: DeployTarget
    error_log: str  # latest stderr from deployer
    error_category: ErrorCategory
    current_dockerfile: str
    diff: str  # diff of the most recent patch
    history: list[PatchRecord]
    retry_count: int
    status: Literal["healing", "healed", "gave_up"]
    last_result: DeployResult


class HealReport(BaseModel):
    """Before/after report handed to main.py and rendered by the frontend."""

    success: bool
    target: DeployTarget
    attempts: int
    records: list[PatchRecord] = Field(default_factory=list)
    final_dockerfile: str
    final_url: str | None = None
    summary: str = ""


# --------------------------------------------------------------------------- #
# main.py (백락원) -> frontend/ (유예인) : SSE event envelope
# --------------------------------------------------------------------------- #
PipelineStage = Literal["analyze", "deploy", "heal", "redeploy"]
EventType = Literal["stage", "log", "heal_diff", "input_required", "done", "error"]


class PipelineEvent(BaseModel):
    """One Server-Sent Event. `data:` line = PipelineEvent.model_dump_json()."""

    type: EventType
    stage: PipelineStage | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class DeployRequest(BaseModel):
    """POST /deploy body."""

    source: str = Field(description="Git URL or server-side path of the uploaded source")
    targets: list[DeployTarget] = Field(default_factory=lambda: ["local", "cloudrun"])
    name: str | None = Field(
        default=None,
        description="Stable app/service name (lowercase, [a-z0-9-]). Redeploys with the same name update the same "
        "Cloud Run service / node app instead of creating new ones. Default: derived from the deployment id.",
    )
    ref: str | None = Field(default=None, description="Git branch or tag to clone when `source` is a Git URL")
    ask_env: bool = Field(
        default=False,
        description="Pause after analysis with an `input_required` event until POST /deploy/{id}/env answers "
        "(the dashboard sets this; scripts and CD keep running without asking)",
    )


class EnvInput(BaseModel):
    """POST /deploy/{deployment_id}/env body. Names not in the job's `required_env` are ignored."""

    values: dict[str, str] = Field(default_factory=dict)
