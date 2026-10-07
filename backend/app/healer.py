"""Self-healing agent (owner: 박재현).

Given a failed deploy (stderr + Dockerfile), classify the error, patch the
Dockerfile, redeploy, and repeat until success or MAX_RETRIES.

    classify --> patch --> redeploy --(success)--> END (healed)
       ^                      |
       +----(still failing)---+--(retry_count >= MAX_RETRIES)--> END (gave_up)

Design notes
- Deterministic rules first, LLM (GPT-4o) only as fallback: the demo cases
  (port binding, missing package) must not depend on an API call landing,
  and it keeps cost inside the team budget.
- The deployer is injected (`deploy_fn`) so healer owns the retry loop
  without importing deployer.py, and tests can use a fake deployer.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from collections.abc import Callable

from langgraph.graph import END, StateGraph

from app.schemas import (
    MAX_RETRIES,
    DeployResult,
    DeployTarget,
    ErrorCategory,
    HealReport,
    HealState,
    PatchRecord,
    PipelineEvent,
)

logger = logging.getLogger(__name__)

DeployFn = Callable[[str, str, DeployTarget], DeployResult]  # (src_dir, dockerfile, target) -> result
LlmPatchFn = Callable[
    [str, str, ErrorCategory], "str | None"
]  # (dockerfile, error_log, category) -> new dockerfile
EmitFn = Callable[[PipelineEvent], None]

DEFAULT_PORT = 8080  # Cloud Run injects PORT=8080 by default

# --------------------------------------------------------------------------- #
# 1. Classification (regex). Order matters: first match wins.
# TODO(박재현): extend with real stderr samples from 전동훈's broken sample app
#               and Cloud Run deploy logs.
# --------------------------------------------------------------------------- #
ERROR_PATTERNS: list[tuple[ErrorCategory, re.Pattern[str]]] = [
    (
        ErrorCategory.MISSING_DEPENDENCY,
        re.compile(r"ModuleNotFoundError: No module named|Cannot find module", re.IGNORECASE),
    ),
    (
        ErrorCategory.PORT_BINDING,
        re.compile(
            r"EADDRINUSE|address already in use|port is already allocated|"
            r"failed to start and listen on the port|Connection refused",
            re.IGNORECASE,
        ),
    ),
    (
        ErrorCategory.BAD_ENTRYPOINT,
        re.compile(
            r"executable file not found|Error loading ASGI app|Could not import module|exec format error",
            re.IGNORECASE,
        ),
    ),
    (
        ErrorCategory.BUILD_FAILURE,
        re.compile(r"failed to solve|returned a non-zero code|ERROR \[", re.IGNORECASE),
    ),
]

# Python import name -> pip package name, for the common mismatches.
PIP_NAME_MAP = {
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
}


def classify_error(error_log: str) -> ErrorCategory:
    for category, pattern in ERROR_PATTERNS:
        if pattern.search(error_log):
            return category
    return ErrorCategory.UNKNOWN


def _error_excerpt(error_log: str, max_lines: int = 5) -> str:
    """Last few non-empty stderr lines — what the frontend shows as 'the error'."""
    lines = [line for line in error_log.strip().splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


# --------------------------------------------------------------------------- #
# 2. Rule-based patches. Each returns (new_dockerfile, rationale) or None.
# --------------------------------------------------------------------------- #
def _insert_before_cmd(dockerfile: str, line: str) -> str:
    lines = dockerfile.rstrip("\n").splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if re.match(r"\s*(CMD|ENTRYPOINT)\b", lines[idx], re.IGNORECASE):
            return "\n".join([*lines[:idx], line, *lines[idx:]]) + "\n"
    return "\n".join([*lines, line]) + "\n"


def _patch_missing_dependency(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    py = re.search(r"No module named '([\w\.]+)'", error_log)
    if py:
        module = py.group(1).split(".")[0]
        package = PIP_NAME_MAP.get(module, module)
        line = f"RUN pip install --no-cache-dir {package}"
        if line in dockerfile:
            return None
        return _insert_before_cmd(
            dockerfile, line
        ), f"Python module '{module}' missing -> pip install {package}"

    node = re.search(r"Cannot find module '([^'./][^']*)'", error_log)
    if node:
        package = node.group(1)
        line = f"RUN npm install {package}"
        if line in dockerfile:
            return None
        return _insert_before_cmd(
            dockerfile, line
        ), f"Node module '{package}' missing -> npm install {package}"
    return None


# Separator between a flag and its value in shell form (`--port 5000`) or exec form (`"--port", "5000"`).
_ARG_SEP = r'(?:[ =]|",\s*")'


def _rebind_start_command(line: str) -> str:
    """Rewrite host/port inside a CMD/ENTRYPOINT line to 0.0.0.0:DEFAULT_PORT."""
    line = re.sub(r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+", f"0.0.0.0:{DEFAULT_PORT}", line)
    line = re.sub(rf"(--port{_ARG_SEP})\d+", rf"\g<1>{DEFAULT_PORT}", line)
    return re.sub(rf"(--host{_ARG_SEP})(?:127\.0\.0\.1|localhost)", r"\g<1>0.0.0.0", line)


def _patch_port_binding(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    patched = re.sub(
        r"^(\s*EXPOSE\s+)\d+", rf"\g<1>{DEFAULT_PORT}", dockerfile, flags=re.MULTILINE | re.IGNORECASE
    )
    patched = re.sub(
        r"^\s*(?:CMD|ENTRYPOINT)\b.*$",
        lambda m: _rebind_start_command(m.group(0)),
        patched,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not re.search(r"^\s*ENV\s+PORT\b", patched, re.MULTILINE | re.IGNORECASE):
        patched = _insert_before_cmd(patched, f"ENV PORT={DEFAULT_PORT}")
    if patched == dockerfile:
        return None
    return patched, f"Bind 0.0.0.0:{DEFAULT_PORT} and set ENV PORT (Cloud Run / container port contract)"


RULE_PATCHES: dict[ErrorCategory, Callable[[str, str], tuple[str, str] | None]] = {
    ErrorCategory.MISSING_DEPENDENCY: _patch_missing_dependency,
    ErrorCategory.PORT_BINDING: _patch_port_binding,
}


# --------------------------------------------------------------------------- #
# 3. LLM fallback (GPT-4o). Skipped silently when OPENAI_API_KEY is absent.
# --------------------------------------------------------------------------- #
LLM_SYSTEM_PROMPT = (
    "You are a container deployment repair agent. Given a Dockerfile and the stderr of a failed "
    "build/run, return ONLY the full corrected Dockerfile. The container must listen on 0.0.0.0 "
    f"and the port from $PORT (default {DEFAULT_PORT}). Make the smallest change that fixes the error."
)


def default_llm_patch(dockerfile: str, error_log: str, category: ErrorCategory) -> str | None:
    if not os.environ.get("OPENAI_API_KEY"):
        logger.info("OPENAI_API_KEY not set; skipping LLM patch")
        return None
    from langchain_openai import ChatOpenAI  # lazy: keep import cost off the rule path

    llm = ChatOpenAI(model=os.environ.get("HEALER_MODEL", "gpt-4o"), temperature=0)
    user = f"Error category: {category.value}\n\nstderr:\n{_error_excerpt(error_log, 40)}\n\nDockerfile:\n{dockerfile}"
    try:
        content = llm.invoke([("system", LLM_SYSTEM_PROMPT), ("user", user)]).content
    except Exception:  # network / quota errors must not crash the pipeline
        logger.exception("LLM patch failed")
        return None
    text = str(content).strip()
    fenced = re.search(r"```(?:dockerfile|Dockerfile)?\n(.*?)```", text, re.DOTALL)
    return (fenced.group(1) if fenced else text).strip() + "\n"


# --------------------------------------------------------------------------- #
# 4. Graph
# --------------------------------------------------------------------------- #
def _unified_diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            "Dockerfile (before)",
            "Dockerfile (after)",
        )
    )


def build_graph(deploy_fn: DeployFn, llm_patch_fn: LlmPatchFn | None = None, emit: EmitFn | None = None):
    llm_patch = llm_patch_fn or default_llm_patch

    def _emit(event: PipelineEvent) -> None:
        if emit:
            emit(event)

    def classify(state: HealState) -> HealState:
        return {"error_category": classify_error(state.get("error_log", ""))}

    def patch(state: HealState) -> HealState:
        before = state["current_dockerfile"]
        error_log = state.get("error_log", "")
        category = state["error_category"]
        attempt = state.get("retry_count", 0) + 1

        rule = RULE_PATCHES.get(category)
        result = rule(before, error_log) if rule else None
        source = "rule"
        if result is None:
            after = llm_patch(before, error_log, category)
            result = (after, "LLM-proposed fix") if after and after != before else None
            source = "llm"
        if result is None:
            return {"status": "gave_up", "retry_count": attempt}

        after, rationale = result
        diff = _unified_diff(before, after)
        record = PatchRecord(
            attempt=attempt,
            category=category,
            error_excerpt=_error_excerpt(error_log),
            before=before,
            after=after,
            diff=diff,
            rationale=rationale,
            source=source,
        )
        _emit(PipelineEvent(type="heal_diff", stage="heal", payload=record.model_dump(mode="json")))
        return {
            "current_dockerfile": after,
            "diff": diff,
            "history": [*state.get("history", []), record],
            "retry_count": attempt,
            "status": "healing",
        }

    def redeploy(state: HealState) -> HealState:
        _emit(PipelineEvent(type="stage", stage="redeploy", payload={"attempt": state["retry_count"]}))
        result = deploy_fn(state["src_dir"], state["current_dockerfile"], state["target"])
        if result.success:
            return {"status": "healed", "last_result": result}
        status = "gave_up" if state["retry_count"] >= MAX_RETRIES else "healing"
        return {"status": status, "last_result": result, "error_log": result.stderr}

    def after_patch(state: HealState) -> str:
        return END if state.get("status") == "gave_up" else "redeploy"

    def after_redeploy(state: HealState) -> str:
        return "classify" if state.get("status") == "healing" else END

    graph = StateGraph(HealState)
    graph.add_node("classify", classify)
    graph.add_node("patch", patch)
    graph.add_node("redeploy", redeploy)
    graph.set_entry_point("classify")
    graph.add_edge("classify", "patch")
    graph.add_conditional_edges("patch", after_patch, {"redeploy": "redeploy", END: END})
    graph.add_conditional_edges("redeploy", after_redeploy, {"classify": "classify", END: END})
    return graph.compile()


def heal(
    src_dir: str,
    target: DeployTarget,
    failed: DeployResult,
    dockerfile: str,
    deploy_fn: DeployFn,
    llm_patch_fn: LlmPatchFn | None = None,
    emit: EmitFn | None = None,
) -> HealReport:
    """Entry point for main.py: call after the first deploy failed."""
    app = build_graph(deploy_fn, llm_patch_fn, emit)
    initial: HealState = {
        "src_dir": src_dir,
        "target": target,
        "error_log": failed.stderr,
        "current_dockerfile": dockerfile,
        "history": [],
        "retry_count": 0,
        "status": "healing",
        "last_result": failed,
    }
    final: HealState = app.invoke(initial, config={"recursion_limit": 4 * MAX_RETRIES + 4})
    success = final.get("status") == "healed"
    records = final.get("history", [])
    last = final.get("last_result")
    summary = (
        f"Healed after {len(records)} patch(es): " + "; ".join(r.rationale for r in records)
        if success
        else f"Gave up after {final.get('retry_count', 0)} attempt(s) (max {MAX_RETRIES})"
    )
    return HealReport(
        success=success,
        target=target,
        attempts=final.get("retry_count", 0),
        records=records,
        final_dockerfile=final["current_dockerfile"],
        final_url=last.url if (success and last) else None,
        summary=summary,
    )
