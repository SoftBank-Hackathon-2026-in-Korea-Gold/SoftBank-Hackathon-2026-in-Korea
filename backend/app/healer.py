"""Self-healing agent (owner: 박재현).

Given a failed deploy (stderr + Dockerfile), classify the error, patch the
Dockerfile, redeploy, and repeat until success or MAX_RETRIES.

    classify --> patch --> redeploy --(success)--> END (healed)
       ^                      |
       +----(still failing)---+--(retry_count >= MAX_RETRIES)--> END (gave_up)

Design notes
- Deterministic rules first, LLM (OpenAI / Claude / local OpenAI-compatible) only as fallback: the demo cases
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
from pathlib import Path
from typing import NamedTuple

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
SourceSuggestFn = Callable[[str, str, str], "str | None"]  # (path, source, traceback) -> fixed source

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


# deployer stderr header: "[deployer] stage=<stage> kind=<kind> target=<target>" (docs/interfaces.md)
_DEPLOYER_HEADER = re.compile(r"^\[deployer\] stage=(\S+) kind=(\S+)", re.MULTILINE)

# Failures a Dockerfile patch cannot fix: credentials/billing/APIs, registry push, deployer bugs,
# CLI timeouts (a retry costs up to the full build/deploy timeout). Stop and ask a human instead.
UNHEALABLE_KINDS = frozenset({"user_action_required", "push_error", "internal_error", "timeout"})


def deployer_header(error_log: str) -> tuple[str, str] | None:
    """(stage, kind) from the deployer header, or None when the stderr has no header."""
    m = _DEPLOYER_HEADER.search(error_log)
    return (m.group(1), m.group(2)) if m else None


def _unhealable(error_log: str) -> tuple[str, str] | None:
    header = deployer_header(error_log)
    return header if header and header[1] in UNHEALABLE_KINDS else None


class AppCodeError(NamedTuple):
    """The app's own code raised: the deepest user frame plus the exception line."""

    exception: str  # "NameError: name 'Flask' is not defined"
    path: str  # path inside the container, e.g. "/app/app.py"
    line: int
    func: str  # "<module>" = crashed while importing


_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception): .*$", re.MULTILINE)
_FRAME_LINE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)', re.MULTILINE)
_LIBRARY_PATH = re.compile(r"site-packages|dist-packages|^<|/usr/(?:local/)?lib/python")


def app_code_failure(error_log: str) -> AppCodeError | None:
    """A traceback ending in the app's own code at the verify stage: a source bug, not a Dockerfile one.

    Only consulted for `unknown` errors, so dependency/port failures keep their rule patches.
    """
    header = deployer_header(error_log)
    if not header or header[0] != "verify":
        return None
    exceptions = list(_EXCEPTION_LINE.finditer(error_log))
    if not exceptions:
        return None
    last = exceptions[-1]
    frames = [
        m for m in _FRAME_LINE.finditer(error_log, 0, last.start()) if not _LIBRARY_PATH.search(m.group(1))
    ]
    if not frames:
        return None
    frame = frames[-1]
    return AppCodeError(last.group(0).strip(), frame.group(1), int(frame.group(2)), frame.group(3))


def _stops_on_app_code(error_log: str) -> AppCodeError | None:
    return app_code_failure(error_log) if classify_error(error_log) == ErrorCategory.UNKNOWN else None


def _llm_context(error_log: str, tail: int = 40) -> str:
    """Deployer header + diagnosis (lines before the first '--- ' section) + the last `tail` lines.

    The header and diagnosis sit at the top of the stderr, so a plain tail drops them on long logs.
    """
    lines = [line for line in error_log.strip().splitlines() if line.strip()]
    head: list[str] = []
    if lines and lines[0].startswith("[deployer]"):
        for line in lines[:3]:
            if line.startswith("--- "):
                break
            head.append(line)
    if len(head) + tail >= len(lines):
        return "\n".join(lines)
    return "\n".join([*head, "...", *lines[-tail:]])


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


# pip: "No matching distribution found for flask==99.99.99" -> the pinned version does not exist.
# Without a pin the package itself is missing; unpinning cannot help there, so only `==` is handled.
_UNRESOLVABLE_PIN = re.compile(r"No matching distribution found for ([A-Za-z0-9][\w.-]*)(\[[^\]]*\])?==(\S+)")
_PIP_REQUIREMENTS_RUN = re.compile(
    r"^[ \t]*RUN\b.*\bpip3?\s+install\b.*?(?:-r|--requirement)[ =](\S+)", re.MULTILINE | re.IGNORECASE
)


def _patch_unresolvable_pin(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    m = _UNRESOLVABLE_PIN.search(error_log)
    if not m:
        return None
    name, extras, version = m.group(1), m.group(2) or "", m.group(3)
    rationale = f"{name}=={version} does not exist on PyPI -> unpin {name} (install the latest release)"

    # Pin written straight into the Dockerfile: drop the version in place.
    inline = re.compile(
        rf"(?<![\w.-]){re.escape(name + extras)}=={re.escape(version)}(?![\w.])", re.IGNORECASE
    )
    if inline.search(dockerfile):
        return inline.sub(name + extras, dockerfile), rationale

    # Pin in a requirements file: rewrite that line right before `pip install -r <file>` runs.
    req = _PIP_REQUIREMENTS_RUN.search(dockerfile)
    if not req:
        return None
    pattern = re.escape(name).replace("\\-", "-")  # sed -E: `.` must stay escaped, `-` must not be
    line = f"RUN sed -i -E 's/^({pattern}(\\[[^]]*\\])?)[[:space:]]*==.*$/\\1/I' {req.group(1)}"
    if line in dockerfile:
        return None
    lines = dockerfile.rstrip("\n").splitlines()
    idx = dockerfile[: req.start()].count("\n")
    return "\n".join([*lines[:idx], line, *lines[idx:]]) + "\n", rationale


RULE_PATCHES: dict[ErrorCategory, Callable[[str, str], tuple[str, str] | None]] = {
    ErrorCategory.MISSING_DEPENDENCY: _patch_missing_dependency,
    ErrorCategory.PORT_BINDING: _patch_port_binding,
    ErrorCategory.BUILD_FAILURE: _patch_unresolvable_pin,
}


# --------------------------------------------------------------------------- #
# 3. LLM fallback: OpenAI / Claude / local (vLLM, SGLang, Ollama, llama.cpp ...). Skipped when none is set.
# --------------------------------------------------------------------------- #
LLM_SYSTEM_PROMPT = (
    "You are a container deployment repair agent. Given a Dockerfile and the stderr of a failed "
    "build/run, return ONLY the full corrected Dockerfile. The container must listen on 0.0.0.0 "
    f"and the port from $PORT (default {DEFAULT_PORT}). Make the smallest change that fixes the error."
)


SOURCE_SYSTEM_PROMPT = (
    "You fix application source code. Given one source file and the traceback of the crash it caused, "
    "return ONLY the full corrected file. Make the smallest change that fixes the error."
)

LLM_TIMEOUT_S = 30  # a slow endpoint must not freeze the demo; on timeout the caller just gets None

# Provider is picked from whatever is in .env, so any of the three works without code changes:
#   anthropic : ANTHROPIC_API_KEY                         (Claude API, Anthropic SDK)
#   openai    : OPENAI_API_KEY                            (OpenAI API)
#   local     : OPENAI_BASE_URL / LLM_BASE_URL            (vLLM, SGLang, Ollama, llama.cpp, LM Studio,
#                                                          or any OpenAI-compatible proxy)
# LLM_PROVIDER forces one (anthropic|claude|openai|local|vllm|sglang|ollama|llamacpp). Otherwise every
# configured provider is tried in the order below until one answers, so a dead key falls through.
PROVIDER_ORDER = ("anthropic", "openai", "local")
DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-4o"}
_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "gpt": "openai",
    "local": "local",
    "vllm": "local",
    "sglang": "local",
    "ollama": "local",
    "llamacpp": "local",
    "llama.cpp": "local",
    "lmstudio": "local",
    "compatible": "local",
}
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


def _base_url() -> str | None:
    url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or os.environ.get("LLM_BASE_URL")
    )
    if not url and (os.environ.get("LLM_PROVIDER") or "").strip().lower() == "ollama":
        url = OLLAMA_DEFAULT_BASE_URL
    return url


def _configured_providers() -> list[str]:
    forced = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if forced:
        provider = _PROVIDER_ALIASES.get(forced)
        if provider is None:
            logger.warning("Unknown LLM_PROVIDER=%r; falling back to auto-detect", forced)
        else:
            return [provider]
    available = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")) and not _base_url(),
        "local": bool(_base_url()),
    }
    return [p for p in PROVIDER_ORDER if available[p]]


def _model_for(provider: str) -> str | None:
    """HEALER_MODEL_<PROVIDER> > HEALER_MODEL (if it fits the provider) > provider default."""
    specific = os.environ.get(f"HEALER_MODEL_{provider.upper()}")
    if specific:
        return specific
    model = os.environ.get("HEALER_MODEL")
    if model:
        name = model.lower()
        # A model id meant for another provider would just 404; use this provider's default instead.
        if provider == "anthropic" and not name.startswith("claude"):
            logger.info("HEALER_MODEL=%s is not a Claude model; using %s", model, DEFAULT_MODELS["anthropic"])
        elif provider == "openai" and name.startswith("claude"):
            logger.info("HEALER_MODEL=%s is not an OpenAI model; using %s", model, DEFAULT_MODELS["openai"])
        else:
            return model
    return DEFAULT_MODELS.get(provider)  # local has no default: the served model name must be given


def _call_anthropic(system: str, user: str, model: str) -> str | None:
    import anthropic  # lazy: keep import cost off the rule path

    client = anthropic.Anthropic(timeout=LLM_TIMEOUT_S, max_retries=1)
    r = client.messages.create(
        model=model, max_tokens=8000, system=system, messages=[{"role": "user", "content": user}]
    )
    # Read text blocks by type: the reply can start with thinking blocks
    text = "".join(getattr(b, "text", "") for b in r.content if b.type == "text").strip()
    if r.stop_reason == "refusal":
        return None
    return text


def _call_openai_compatible(system: str, user: str, model: str, provider: str) -> str | None:
    from langchain_openai import ChatOpenAI  # lazy: keep import cost off the rule path

    kwargs: dict = {"model": model, "temperature": 0, "timeout": LLM_TIMEOUT_S, "max_retries": 1}
    if provider == "local":
        kwargs["base_url"] = _base_url()
        kwargs["api_key"] = os.environ.get("OPENAI_API_KEY") or "EMPTY"  # local servers ignore the key
    else:
        kwargs["api_key"] = os.environ.get("OPENAI_API_KEY")
    content = ChatOpenAI(**kwargs).invoke([("system", system), ("user", user)]).content
    return str(content).strip()


def _ask_llm(system: str, user: str, fence_langs: str) -> str | None:
    """One LLM round trip over the configured providers; returns the fenced (or bare) body, or None."""
    providers = _configured_providers()
    if not providers:
        logger.info(
            "No LLM configured (ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENAI_BASE_URL); rule patches only"
        )
        return None
    for provider in providers:
        model = _model_for(provider)
        if not model:
            logger.warning("LLM provider %s needs HEALER_MODEL (served model name); skipping", provider)
            continue
        try:
            if provider == "anthropic":
                text = _call_anthropic(system, user, model)
            else:
                text = _call_openai_compatible(system, user, model, provider)
        except Exception:  # network / auth / quota / timeout / bad model id must not crash the pipeline
            logger.exception("LLM call failed (provider=%s, model=%s)", provider, model)
            continue
        if text:
            fenced = re.search(rf"```(?:{fence_langs})?\n(.*?)```", text, re.DOTALL)
            return (fenced.group(1) if fenced else text).strip() + "\n"
        logger.warning("LLM returned no text (provider=%s, model=%s)", provider, model)
    return None


def default_llm_patch(dockerfile: str, error_log: str, category: ErrorCategory) -> str | None:
    user = (
        f"Error category: {category.value}\n\nstderr:\n{_llm_context(error_log)}\n\nDockerfile:\n{dockerfile}"
    )
    return _ask_llm(LLM_SYSTEM_PROMPT, user, "dockerfile|Dockerfile")


def default_source_suggestion(path: str, source: str, traceback: str) -> str | None:
    """Proposed full replacement for `path`. Reported as a diff only; healer never applies it."""
    user = f"File: {path}\n\nTraceback:\n{traceback}\n\nSource:\n{source}"
    return _ask_llm(SOURCE_SYSTEM_PROMPT, user, "python|py")


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
        error_log = state.get("error_log", "")
        update: HealState = {"error_category": classify_error(error_log)}
        if _unhealable(error_log) or _stops_on_app_code(error_log):
            update["status"] = "gave_up"  # no Dockerfile patch can fix it: skip LLM + redeploy
        return update

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
            return {"status": "gave_up"}  # no patch applied -> attempt not counted

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

    def after_classify(state: HealState) -> str:
        return END if state.get("status") == "gave_up" else "patch"

    def after_patch(state: HealState) -> str:
        return END if state.get("status") == "gave_up" else "redeploy"

    def after_redeploy(state: HealState) -> str:
        return "classify" if state.get("status") == "healing" else END

    graph = StateGraph(HealState)
    graph.add_node("classify", classify)
    graph.add_node("patch", patch)
    graph.add_node("redeploy", redeploy)
    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", after_classify, {"patch": "patch", END: END})
    graph.add_conditional_edges("patch", after_patch, {"redeploy": "redeploy", END: END})
    graph.add_conditional_edges("redeploy", after_redeploy, {"classify": "classify", END: END})
    return graph.compile()


MAX_SOURCE_BYTES = 64 * 1024
APP_CODE_STOP = "Not auto-healable: application code error"  # fixed prefix: marks an intended stop


def _source_file(src_dir: str, dockerfile: str, container_path: str) -> Path | None:
    """Map a traceback path inside the container back to a file under `src_dir`.

    The path comes from container logs (untrusted), so the result must stay inside `src_dir`.
    """
    workdirs = re.findall(r"^\s*WORKDIR\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)
    workdir = (workdirs[-1] if workdirs else "/").rstrip("/") + "/"
    if container_path.startswith(workdir):
        relative = container_path[len(workdir) :]
    elif not container_path.startswith("/"):
        relative = container_path
    else:
        return None  # outside the copied app directory (e.g. a system file)
    root = Path(src_dir).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    if candidate.stat().st_size > MAX_SOURCE_BYTES:
        return None
    return candidate


def _suggest_source_diff(
    src_dir: str, dockerfile: str, error: AppCodeError, error_log: str, suggest_fn: SourceSuggestFn
) -> str | None:
    """Unified diff of an LLM-proposed source fix, or None. Never written to disk."""
    path = _source_file(src_dir, dockerfile, error.path)
    if path is None:
        return None
    relative = path.relative_to(Path(src_dir).resolve()).as_posix()
    before = path.read_text(errors="replace")
    try:
        after = suggest_fn(relative, before, _llm_context(error_log))
    except Exception:  # an injected suggester must not turn an intended stop into a crash
        logger.exception("source suggestion failed")
        return None
    if not after or after.strip() == before.strip():
        return None
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            f"{relative} (before)",
            f"{relative} (suggested)",
        )
    )
    return diff or None


def _app_code_summary(error: AppCodeError, suggestion: str | None) -> str:
    summary = (
        f"{APP_CODE_STOP} at {error.path}:{error.line} in {error.func} — {error.exception}. "
        "A Dockerfile change cannot fix this; fix the source."
    )
    if suggestion:
        summary += f"\n\nSuggested source fix (not applied):\n{suggestion}"
    return summary


def heal(
    src_dir: str,
    target: DeployTarget,
    failed: DeployResult,
    dockerfile: str,
    deploy_fn: DeployFn,
    llm_patch_fn: LlmPatchFn | None = None,
    emit: EmitFn | None = None,
    suggest_fn: SourceSuggestFn | None = None,
) -> HealReport:
    """Entry point for main.py: call after the first deploy failed.

    Policy: only the Dockerfile is patched automatically. A crash in the app's own code stops at
    once; for import-time crashes a source fix is suggested as a diff in the summary, never applied.
    """
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
    error_log = final.get("error_log", "")
    blocked = None if success else _unhealable(error_log)
    app_error = None if (success or blocked) else _stops_on_app_code(error_log)
    if success:
        summary = f"Healed after {len(records)} patch(es): " + "; ".join(r.rationale for r in records)
    elif app_error:
        suggestion = None
        # Import-time crash = a code bug worth a fix suggestion; a request-time 5xx may be runtime state.
        if app_error.func == "<module>":
            suggestion = _suggest_source_diff(
                src_dir,
                final["current_dockerfile"],
                app_error,
                error_log,
                suggest_fn or default_source_suggestion,
            )
        if suggestion and emit:
            emit(
                PipelineEvent(
                    type="log",
                    stage="heal",
                    payload={"kind": "source_suggestion", "path": app_error.path, "diff": suggestion},
                )
            )
        summary = _app_code_summary(app_error, suggestion)
    elif blocked:
        stage, kind = blocked
        summary = (
            f"Not auto-healable: deployer reported kind={kind} at stage={stage}; needs human action "
            f"(after {len(records)} patch(es))"
        )
    else:
        summary = f"Gave up after {final.get('retry_count', 0)} attempt(s) (max {MAX_RETRIES})"
    return HealReport(
        success=success,
        target=target,
        attempts=final.get("retry_count", 0),
        records=records,
        final_dockerfile=final["current_dockerfile"],
        final_url=last.url if (success and last) else None,
        summary=summary,
    )
