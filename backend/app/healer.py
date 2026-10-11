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
- With ANTHROPIC_API_KEY the fallback is a repo agent: it reads the whole source tree, may edit
  source files in `src_dir` (main.py's per-target copy, never the user's checkout), and returns
  the Dockerfile. App-code crashes are then repaired instead of stopping.
- Failures only a human can fix (missing env var, unreachable DB/service, silent native crash) stop
  before any LLM or redeploy, with or without the repo agent: see needs_human_stop.
"""

from __future__ import annotations

import ast
import difflib
import logging
import os
import posixpath
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

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
RepairFn = Callable[
    [str, str, str, ErrorCategory], "RepoFix | None"
]  # (src_dir, dockerfile, error_log, category) -> fix; may edit files under src_dir
ConfirmFn = Callable[[str, "RepoFix"], "str | None"]  # (src_dir, fix) -> None = redeploy, str = stop summary

DEFAULT_PORT = 8080  # Cloud Run injects PORT=8080 by default

# --------------------------------------------------------------------------- #
# 1. Classification (regex). Order matters: first match wins.
# TODO(박재현): extend with real stderr samples from 전동훈's broken sample app
#               and Cloud Run deploy logs.
# --------------------------------------------------------------------------- #
ERROR_PATTERNS: list[tuple[ErrorCategory, re.Pattern[str]]] = [
    (
        ErrorCategory.MISSING_DEPENDENCY,
        re.compile(
            # Shared objects only from the raised error or the loader: optional "Could not load dynamic
            # library ... cannot open shared object file" warnings (e.g. CUDA) must not mask the real cause.
            r"ModuleNotFoundError: No module named|Cannot find module|"
            r"^(?:ImportError|OSError)\b.*cannot open shared object file|error while loading shared libraries",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        # Not "Connection refused": that is the app failing to reach a DB/API (see needs_human_stop).
        # The deployer's own diagnosis says "failed to start and listen on the port" for a real bind bug.
        ErrorCategory.PORT_BINDING,
        re.compile(
            r"EADDRINUSE|address already in use|port is already allocated|"
            r"failed to start and listen on the port",
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
# function_error: Cloud Run functions are built from source by buildpacks; a Dockerfile patch cannot fix them
UNHEALABLE_KINDS = frozenset(
    {"user_action_required", "push_error", "internal_error", "timeout", "function_error"}
)


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


_EXCEPTION_LINE = re.compile(
    r"^(?!Traceback|During|The above)[A-Za-z_][\w.]*(?:Error|Exception|[A-Z][A-Za-z0-9_]*)(?::\s*.*)?$",
    re.MULTILINE,
)
_FRAME_LINE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (\S+)', re.MULTILINE)
_LIBRARY_PATH = re.compile(r"site-packages|dist-packages|^<|/usr/(?:local/)?lib/python")


def app_code_failure(error_log: str) -> AppCodeError | None:
    """A traceback ending in the app's own code at the verify/deploy stage: a source bug, not a Dockerfile one.

    Only consulted for `unknown` errors, so dependency/port failures keep their rule patches.
    """
    header = deployer_header(error_log)
    if not header or header[0] not in {"verify", "deploy"}:
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


# --------------------------------------------------------------------------- #
# 1b. App-level "needs a human" stops. Neither a Dockerfile patch nor the repo agent can fix these
#     (it would have to invent a secret, a database or a crash it cannot see), so they stop even
#     when repair_fn is set. Read runtime/revision logs at verify or deploy, before category rules.
# --------------------------------------------------------------------------- #
SERVICE_STOP = "Not auto-healable: external service unreachable at startup"
CONFIG_STOP = "Not auto-healable: missing runtime configuration"
CRASH_STOP = "Not auto-healable: silent crash during startup"

_CONNECTION_ERROR = re.compile(
    r"Connection refused|ECONNREFUSED|Name or service not known|Temporary failure in name resolution|"
    r"could not translate host name|No route to host|Connection timed out|Operation timed out|"
    r"Network is unreachable|ENETUNREACH|ETIMEDOUT|getaddrinfo ENOTFOUND|"
    r"could not connect to server|Can't connect to MySQL server|"
    r"password authentication failed|Access denied for user|"
    r'database "[^"]+" does not exist|Unknown database',
    re.IGNORECASE,
)
_HOST_PORT = re.compile(r"(?<![\w.:-])((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z][\w.-]*):(\d{2,5})\b")
_PG_TARGET = re.compile(
    r'server (?:at|on host) ["\']([^"\']+)["\'](?: \([^)]*\))?, port (\d+)', re.IGNORECASE
)  # libpq / psycopg wording
_MYSQL_TARGET = re.compile(r"Can't connect to MySQL server on ['\"]([^'\":]+)(?::(\d+))?['\"]", re.IGNORECASE)
# KeyError: 'NAME' counts only when raised by an environ/getenv read: a plain dict KeyError is a code bug.
_KEY_ERROR = re.compile(r"^KeyError: '([A-Z_][A-Z0-9_]*)'\s*$", re.MULTILINE)
_ENV_ACCESS = re.compile(r"\bos\.environ\[|<frozen os>.*__getitem__")
_ENV_MESSAGE = re.compile(
    r"(?i:environment variable|env var)\s+['\"`]?([A-Z_][A-Z0-9_]*)['\"`]?\s+"
    r"(?i:is\s+)?(?i:not set|missing|required|undefined|not defined)"
    r"|(?i:missing (?:required )?(?:environment variables?|env vars?)):?\s+['\"`]?([A-Z_][A-Z0-9_]*)"
)
_ENV_REQUIRED_READ = re.compile(r"""environ\[\s*['"]([A-Z_][A-Z0-9_]*)['"]\s*\]""")
_EXIT_CODE = re.compile(r"exit_code=(\d+)")
_ERROR_WORDS = re.compile(
    r"error|exception|traceback|fatal|panic|denied|not found|fail|no such file|not executable", re.IGNORECASE
)
_SIGNALS = {4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 9: "SIGKILL", 11: "SIGSEGV"}


def _section(error_log: str, name: str) -> str:
    """Body of a deployer stderr section ("--- runtime logs ---" ...) up to the next section."""
    m = re.search(
        rf"^--- {re.escape(name)} ---$\n?(.*?)(?=^--- [^\n]+ ---$|\Z)", error_log, re.MULTILINE | re.DOTALL
    )
    return m.group(1) if m else ""


def _diagnosis(error_log: str) -> str:
    """The deployer's one-line diagnosis: lines between the header and the first '--- ' section."""
    head = error_log.split("\n--- ", 1)[0]
    return "\n".join(line for line in head.splitlines() if not line.startswith("[deployer]"))


def _missing_env_names(logs: str) -> list[str]:
    names = []
    for m in _KEY_ERROR.finditer(logs):
        before = [line for line in logs[: m.start()].splitlines() if line.strip()][-3:]
        if any(_ENV_ACCESS.search(line) for line in before):
            names.append(m.group(1))
    if not names:
        for line in logs.splitlines():
            if not re.search(r"optional|continuing", line, re.IGNORECASE):
                names += [a or b for a, b in _ENV_MESSAGE.findall(line)]
    return list(dict.fromkeys(names))


def _required_env_in_source(src_dir: str | None, dockerfile: str, error_log: str) -> list[str]:
    """Other os.environ["X"] reads (no default) in the file that raised. Names only, never values."""
    error = app_code_failure(error_log)
    path = _source_file(src_dir, dockerfile, error.path) if (src_dir and error) else None
    return _ENV_REQUIRED_READ.findall(path.read_text(errors="replace")) if path else []


def _config_stop(logs: str, src_dir: str | None, dockerfile: str, error_log: str) -> str | None:
    exceptions = list(_EXCEPTION_LINE.finditer(logs))
    final = exceptions[-1].group(0) if exceptions else ""
    chained = (
        "direct cause of the following exception" in logs or "During handling of the above exception" in logs
    ) and any(_KEY_ERROR.fullmatch(m.group(0)) or _ENV_MESSAGE.search(m.group(0)) for m in exceptions[:-1])
    if final and not (_KEY_ERROR.fullmatch(final) or _ENV_MESSAGE.search(final)) and not chained:
        return None  # Optional config warnings are not the cause of an unrelated exception.
    names = _missing_env_names(logs)
    if _KEY_ERROR.fullmatch(final):
        names = [name for name in names if name == _KEY_ERROR.fullmatch(final).group(1)]
    if not names and _KEY_ERROR.fullmatch(final):
        error = app_code_failure(error_log)
        path = _source_file(src_dir, dockerfile, error.path) if src_dir and error else None
        if path:
            try:
                tree = ast.parse(path.read_text(errors="replace"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Subscript) and node.lineno == error.line:
                        value = node.value
                        if (
                            isinstance(value, ast.Attribute)
                            and value.attr == "environ"
                            and isinstance(value.value, ast.Name)
                            and value.value.id == "os"
                            and isinstance(node.slice, ast.Constant)
                            and node.slice.value == _KEY_ERROR.fullmatch(final).group(1)
                        ):
                            names.append(node.slice.value)
            except (OSError, SyntaxError):
                pass
    if not names:
        return None
    names = list(dict.fromkeys([*names, *_required_env_in_source(src_dir, dockerfile, error_log)]))
    return (
        f"{CONFIG_STOP}: required environment variable(s) not set: {', '.join(names)}. "
        "A Dockerfile change cannot fix this and the healer will not invent values: set them as runtime "
        "env vars for the deploy (secrets via the platform's secret store, never baked into the image), "
        "then redeploy."
    )


def _service_stop(logs: str) -> str | None:
    lines = [line for line in logs.splitlines() if _CONNECTION_ERROR.search(line)]
    if not lines:
        return None
    exceptions = [m for m in _EXCEPTION_LINE.finditer(logs) if ":" in m.group(0)]
    chained = (
        "direct cause of the following exception" in logs or "During handling of the above exception" in logs
    ) and any(_CONNECTION_ERROR.search(m.group(0)) for m in exceptions[:-1])
    if exceptions and not _CONNECTION_ERROR.search(exceptions[-1].group(0)) and not chained:
        return None  # crashed on something else (e.g. an optional cache was down): not the cause
    last_connection = max(i for i, line in enumerate(logs.splitlines()) if _CONNECTION_ERROR.search(line))
    later = "\n".join(logs.splitlines()[last_connection + 1 :])
    if re.search(
        r"\b(?:connected|connection established|ready|healthy|continuing without)\b", later, re.IGNORECASE
    ):
        return None
    if re.search(r"continuing without|optional|recovered", lines[-1], re.IGNORECASE):
        return None
    target = None
    for line in reversed(lines):
        line = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/]*@", "", line)
        line = re.sub(r"[^\s:@'\"]+:[^\s@'\"]*@", "", line)  # scheme-less user:pass@host
        m = _HOST_PORT.search(line) or _PG_TARGET.search(line)
        if m:
            host = m.group(1)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
                target = f"{host}:{m.group(2)}"
                break
        m_mysql = _MYSQL_TARGET.search(line)
        if m_mysql:
            host = m_mysql.group(1)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
                port = m_mysql.group(2) or "3306"
                target = f"{host}:{port}"
                break
    evidence_match = _CONNECTION_ERROR.search(lines[-1]) or _CONNECTION_ERROR.search(lines[0])
    evidence = evidence_match.group(0) if evidence_match else "unreachable"
    summary = (
        f"{SERVICE_STOP}: the app could not reach {target or 'an external service'} ({evidence}). "
        "A Dockerfile change cannot fix this; set the DB/network configuration (host, port, credentials) "
        "for the deploy, make sure that service is reachable from the container, then redeploy."
    )
    if target and target.split(":")[0] in {"localhost", "127.0.0.1"}:
        summary += " Note: inside a container localhost is the container itself, not the host machine."
    return summary


def _crash_stop(logs: str, error_log: str) -> str | None:
    native_crash = re.search(
        r"Fatal Python error: (?:Segmentation fault|Aborted|Bus error|Illegal instruction)", logs
    )
    if native_crash:
        return f"{CRASH_STOP}: {native_crash.group(0)}. Inspect the native extension stack and runtime libraries before redeploying."
    if "exited during startup" not in _diagnosis(error_log):
        return None
    if _ERROR_WORDS.search(logs) or any(":" in m.group(0) for m in _EXCEPTION_LINE.finditer(logs)):
        return None  # there is something to read: leave it to the app-code stop / LLM
    code = _EXIT_CODE.search(_section(error_log, "error"))
    exit_code = int(code.group(1)) if code else 0
    if exit_code == 0:
        return None
    signal = _SIGNALS.get(exit_code - 128) if exit_code > 128 else None
    return (
        f"{CRASH_STOP}: the container exited with exit_code={exit_code}"
        f"{f' ({signal})' if signal else ''} and left no traceback or error message. "
        "A Dockerfile patch cannot fix a crash it cannot see, so the healer stops instead of guessing. "
        "To see where it dies, add ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1 (flushes buffered output and "
        "dumps the native stack), redeploy, then fix the crashing code or native extension."
    )


def needs_human_stop(error_log: str, src_dir: str | None = None, dockerfile: str = "") -> str | None:
    """Summary of an app-level failure only a human can fix (missing env, unreachable DB, silent crash)."""
    header = deployer_header(error_log)
    if not header or header[0] not in {"verify", "deploy"}:
        return None
    # The local deployer writes this only when the app logged that it is listening and did not crash,
    # so env/connection warnings in its logs are not the cause; leave it to the port rule. Not keyed on
    # the category: Cloud Run reports every startup crash as "failed to start and listen on the port".
    if "probably bound to another port or to 127.0.0.1" in _diagnosis(error_log):
        return None
    logs = _section(error_log, "runtime logs") or _section(error_log, "cloudrun revision logs")
    return (
        _config_stop(logs, src_dir, dockerfile, error_log)
        or _service_stop(logs)
        or _crash_stop(logs, error_log)
    )


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
    return _patch_shared_library(dockerfile, error_log)


# Only stable Debian package mappings exercised by the deterministic rule.
SONAME_PACKAGES = {
    "libxslt.so.1": "libxslt1.1",
    "libexslt.so.0": "libxslt1.1",
    "libpq.so.5": "libpq5",
    "libgomp.so.1": "libgomp1",
}
_MISSING_SONAME = re.compile(r"([\w.+-]+\.so(?:\.\d+)*): cannot open shared object file")
_FROM_LINE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)
_DEBIAN_ROOT_BASE = re.compile(
    r"(?:python:3\.\d+(?:\.\d+)?-slim(?:-(?:trixie|bookworm|bullseye))?"
    r"|debian:(?:trixie|bookworm|bullseye)(?:-slim)?)"
)


def _known_root_base(lines: list[str], start: int) -> bool:
    if start < 0:
        return False
    base = _FROM_LINE.match(lines[start]).group(1)
    aliases = re.findall(r"\sAS\s+(\S+)\s*$", "\n".join(lines[:start]), re.MULTILINE | re.IGNORECASE)
    return base.lower() not in {a.lower() for a in aliases} and bool(_DEBIAN_ROOT_BASE.fullmatch(base))


def _final_stage(lines: list[str]) -> int:
    """Index of the last FROM line (the stage that becomes the image), or -1."""
    starts = [i for i, line in enumerate(lines) if _FROM_LINE.match(line)]
    return starts[-1] if starts else -1


def _patch_shared_library(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    header = deployer_header(error_log)
    if header and header[0] not in {"verify", "deploy"}:
        return None
    sonames = list(dict.fromkeys(_MISSING_SONAME.findall(error_log)))
    packages = [SONAME_PACKAGES.get(s) for s in sonames]
    if not sonames or None in packages:
        return None  # unknown soname: left to the LLM / repo agent
    lines = dockerfile.rstrip("\n").splitlines()
    start = _final_stage(lines)
    if not _known_root_base(lines, start):
        return None
    install = " ".join(dict.fromkeys(packages))
    # Exact generated instruction avoids treating comments or COPY filenames as installed packages.
    line = f"RUN apt-get update && apt-get install -y --no-install-recommends {install} && rm -rf /var/lib/apt/lists/*"
    if line in lines[start:]:
        return None
    patched = "\n".join([*lines[: start + 1], line, *lines[start + 1 :]]) + "\n"
    return patched, f"Shared library {', '.join(sonames)} missing at runtime -> apt-get install {install}"


# Separator between a flag and its value in shell form (`--port 5000`) or exec form (`"--port", "5000"`).
_ARG_SEP = r'(?:[ =]|",\s*")'


def _rebind_start_command(line: str) -> str:
    """Rewrite host/port inside a CMD/ENTRYPOINT line to 0.0.0.0:DEFAULT_PORT."""
    original = line
    line = re.sub(
        rf"((?<![\w-])(?:-b|--bind){_ARG_SEP})(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+",
        rf"\g<1>0.0.0.0:{DEFAULT_PORT}",
        line,
    )
    # gunicorn with the IP omitted (`-b :5000`, `--bind=:5000`); anchored to the flag so another
    # service's host:port in an `sh -c` chain is left alone.
    line = re.sub(rf"((?<![\w-])(?:-b|--bind){_ARG_SEP}):\d+", rf"\g<1>0.0.0.0:{DEFAULT_PORT}", line)
    line = re.sub(rf"(--port{_ARG_SEP})\d+", rf"\g<1>{DEFAULT_PORT}", line)
    line = re.sub(rf"(--host{_ARG_SEP})(?:127\.0\.0\.1|localhost)", r"\g<1>0.0.0.0", line)
    if line != original:
        return line
    # No flag to anchor on (`runserver 127.0.0.1:8000`, `php -S localhost:8000`, `--listen=...`): rewrite
    # only the last localhost endpoint, the server at the end of a chain, so `wait-for localhost:5432` stays.
    endpoints = list(re.finditer(r"(?<![\w.-])(?:127\.0\.0\.1|localhost):\d+\b", line))
    if not endpoints:
        return line
    last = endpoints[-1]
    return f"{line[: last.start()]}0.0.0.0:{DEFAULT_PORT}{line[last.end() :]}"


def _patch_port_binding(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    exposed = re.sub(
        r"^(\s*EXPOSE\s+)\d+", rf"\g<1>{DEFAULT_PORT}", dockerfile, flags=re.MULTILINE | re.IGNORECASE
    )
    patched = re.sub(
        r"^\s*(?:CMD|ENTRYPOINT)\b.*$",
        lambda m: _rebind_start_command(m.group(0)),
        exposed,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    rebound = patched != exposed
    has_env = re.search(r"^\s*ENV\s+PORT\b", patched, re.MULTILINE | re.IGNORECASE)
    if not has_env:
        patched = _insert_before_cmd(patched, f"ENV PORT={DEFAULT_PORT}")
    if patched == dockerfile:
        return None
    # Say what actually changed: the user sees this next to the diff.
    changes = [
        f"start command rebound to 0.0.0.0:{DEFAULT_PORT}"
        if rebound
        else "start command unchanged (no host/port bind found in CMD/ENTRYPOINT)",
        *([f"EXPOSE {DEFAULT_PORT}"] if exposed != dockerfile else []),
        *([f"ENV PORT={DEFAULT_PORT} added"] if not has_env else []),
    ]
    return patched, f"Port contract (listen on 0.0.0.0:$PORT, default {DEFAULT_PORT}): " + "; ".join(changes)


# Runtime write denied for a non-root USER. Category stays `unknown` (no contract change); handled here.
_PERMISSION_DENIED = re.compile(
    r"PermissionError: \[Errno 13\] Permission denied: '([^']+)'"
    r"|EACCES: permission denied, \w+ '([^']+)'"
    r"|(attempt to write a readonly database)"
)
_USER_LINE = re.compile(r"^\s*USER\s+(\S+)\s*$", re.IGNORECASE)
_WORKDIR_LINE = re.compile(r"^\s*WORKDIR\s+(\S+)", re.IGNORECASE)


def _patch_permission(dockerfile: str, error_log: str) -> tuple[str, str] | None:
    header = deployer_header(error_log)
    m = _PERMISSION_DENIED.search(error_log) if header and header[0] == "verify" else None
    if not m:
        return None  # build-stage "Permission denied" (e.g. a non-executable script) is not this
    exceptions = list(_EXCEPTION_LINE.finditer(_section(error_log, "runtime logs") or error_log))
    if exceptions and not _PERMISSION_DENIED.search(exceptions[-1].group(0)):
        return None
    if exceptions:
        m = _PERMISSION_DENIED.search(exceptions[-1].group(0))
    logs = _section(error_log, "runtime logs") or error_log
    # A directory-creation failure has a deterministic mkdir fix; opening a denied file does not.
    context = logs[: exceptions[-1].start()] if exceptions else logs
    context = context.rsplit("Traceback (most recent call last):", 1)[-1]
    if not re.search(r"\b(?:makedirs|mkdir)\b", context) or re.search(
        r"\b(?:open|write_text|write_bytes)\s*\(", context
    ):
        return None
    path = m.group(1) or m.group(2)
    if not path or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        return None  # No path for SQLite, or shell syntax in untrusted logs: do not guess.
    if any(part in {".", ".."} for part in path.split("/")):
        return None
    lines = dockerfile.rstrip("\n").splitlines()
    start = _final_stage(lines)
    if not _known_root_base(lines, start):
        return None  # An inherited USER in an arbitrary base/stage may be non-root.
    users = [i for i in range(start + 1, len(lines)) if _USER_LINE.match(lines[i])]
    if not users:
        return None
    idx = users[-1]
    user = _USER_LINE.match(lines[idx]).group(1)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?::[A-Za-z0-9_][A-Za-z0-9_-]*)?", user):
        return None
    if user.split(":")[0] in {"root", "0"}:
        return None
    if any(_USER_LINE.match(lines[i]).group(1) not in {"root", "0", "0:0", "root:root"} for i in users[:-1]):
        return None  # Inserting before the final USER would run under the preceding non-root user.
    workdirs = [
        (i, w.group(1)) for i, line in enumerate(lines[start:], start) if (w := _WORKDIR_LINE.match(line))
    ]
    if not workdirs or workdirs[-1][0] > idx:
        return None
    workdir = workdirs[-1][1]
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", workdir) or any(
        part in {".", ".."} for part in workdir.split("/")
    ):
        return None
    workdir = posixpath.normpath(workdir)
    if workdir in {"/", "/etc", "/usr", "/var", "/bin", "/sbin", "/proc", "/sys", "/dev"}:
        return None
    target = posixpath.normpath(posixpath.join(workdir, path))
    if not target.startswith(workdir + "/") or "/" in target[len(workdir) + 1 :]:
        return None  # Do not mkdir through an unverified intermediate symlink.
    # One directory only. -h avoids changing a symlink target; never recursively chown the app.
    line = f"RUN mkdir -p -- {target} && chown -h -- {user} {target}"
    if line in lines[start:]:
        return None
    patched = "\n".join([*lines[:idx], line, *lines[idx:]]) + "\n"
    return patched, f"USER {user} cannot write {path} -> create and chown only {target} before USER"


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
    ErrorCategory.UNKNOWN: _patch_permission,  # runs before the repo agent / LLM like any rule
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
            if provider == "local" and not _base_url():
                logger.warning("LLM_PROVIDER=%r selected but no base URL configured; skipping", forced)
                return []
            return [provider]
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    available = {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(openai_key and openai_key != "EMPTY"),
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


def _call_anthropic(system: str, user: str, model: str, timeout: float = LLM_TIMEOUT_S) -> str | None:
    import anthropic  # lazy: keep import cost off the rule path

    client = anthropic.Anthropic(timeout=timeout, max_retries=0)
    r = client.messages.create(
        model=model, max_tokens=8000, system=system, messages=[{"role": "user", "content": user}]
    )
    # Read text blocks by type: the reply can start with thinking blocks
    text = "".join(getattr(b, "text", "") for b in r.content if b.type == "text").strip()
    if r.stop_reason == "refusal":
        return None
    return text


def _call_openai_compatible(
    system: str, user: str, model: str, provider: str, timeout: float = LLM_TIMEOUT_S
) -> str | None:
    from langchain_openai import ChatOpenAI  # lazy: keep import cost off the rule path

    kwargs: dict = {"model": model, "temperature": 0, "timeout": timeout, "max_retries": 0}
    if provider == "local":
        base_url = _base_url()
        if not base_url:
            logger.warning("Local provider selected but no base_url configured; skipping")
            return None
        kwargs["base_url"] = base_url
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
    deadline = time.monotonic() + LLM_TIMEOUT_S
    for provider in providers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("Overall LLM deadline exceeded; skipping provider %s", provider)
            break
        model = _model_for(provider)
        if not model:
            logger.warning("LLM provider %s needs HEALER_MODEL (served model name); skipping", provider)
            continue
        try:
            timeout = max(1, round(remaining))
            if provider == "anthropic":
                text = _call_anthropic(system, user, model, timeout=timeout)
            else:
                text = _call_openai_compatible(system, user, model, provider, timeout=timeout)
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
# 3b. Repo agent (Claude only: needs tool use). Reads the source tree with analyzer's read tools
#     plus edit_file, so it can fix the code itself instead of guessing from stderr + Dockerfile.
# --------------------------------------------------------------------------- #
REPAIR_SYSTEM_PROMPT = (
    "You are a container deployment repair agent with the app's repository open. A deploy failed. "
    "Read the files you need to find the root cause. If the bug is in the source, fix it with edit_file; "
    "if it is in the Dockerfile, change the Dockerfile you return. The container must listen on 0.0.0.0 "
    f"and the port from $PORT (default {DEFAULT_PORT}). Traceback paths are inside the container's WORKDIR. "
    "Make the smallest change that fixes the error; do not touch tests or unrelated files."
)
REPAIR_TIMEOUT_S = 120  # per model call; the agent takes several turns, so LLM_TIMEOUT_S is too short


class RepoFix(NamedTuple):
    dockerfile: str
    source_diff: str  # unified diff of files the agent edited ("" if none)
    rationale: str
    changed: tuple[str, ...] = ()  # paths (relative to src_dir) of the files behind source_diff


class _RepairAnswer(BaseModel):
    dockerfile: str = Field(
        description="The full Dockerfile to deploy with (unchanged if the fix is in the source)"
    )
    rationale: str = Field(description="One sentence: the root cause and what you changed")


def repo_edit_tool(root: Path):
    """edit_file for the repo agent, plus the original text of every file it touched (for the diff)."""
    from langchain_core.tools import tool

    from app.analyzer import SECRET_FILE, SKIP_DIRS, _inside, read_text

    root = root.resolve()
    originals: dict[str, str] = {}

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace `old` with `new` in a repo file; `old` must appear exactly once. Empty `old` creates a new file.
        The Dockerfile is not edited here: return it instead."""
        p = root / path
        if not _inside(root, p):
            return f"cannot edit outside the repository: {path}"
        rel = p.resolve().relative_to(root)
        if (
            any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts)
            or SECRET_FILE.fullmatch(rel.name)
            or rel.name == "Dockerfile"
        ):
            return f"cannot edit this file: {path}"
        key = rel.as_posix()
        text = read_text(p) if p.is_file() else ""
        if old:
            if text.count(old) != 1:
                return f"`old` must appear exactly once in {key} (found {text.count(old)})"
            updated = text.replace(old, new)
        elif p.exists():
            return f"{key} already exists; pass the text to replace as `old`"
        else:
            updated = new
        originals.setdefault(key, text)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(updated)
        return f"edited {key}"

    return edit_file, originals


def default_repo_repair(
    src_dir: str, dockerfile: str, error_log: str, category: ErrorCategory
) -> RepoFix | None:
    import anthropic

    from app.analyzer import RepoReader, read_text

    root = Path(src_dir).resolve()
    edit_file, originals = repo_edit_tool(root)
    reader = RepoReader(
        anthropic.Anthropic(timeout=REPAIR_TIMEOUT_S, max_retries=0), _model_for("anthropic"), str(root)
    )
    reader.tools.append(edit_file)
    reader.specs.append(
        {
            "name": edit_file.name,
            "description": edit_file.description,
            "input_schema": edit_file.tool_call_schema.model_json_schema(),
        }
    )
    user = (
        f"Error category: {category.value}\n\nstderr:\n{_llm_context(error_log)}\n\nDockerfile:\n{dockerfile}"
    )
    try:
        answer = reader.ask(_RepairAnswer, [("system", REPAIR_SYSTEM_PROMPT), ("user", user)])
    except Exception:  # network / quota / turn limit must not crash the pipeline
        logger.exception("repo repair failed")
        return None
    source_diff = "".join(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                read_text(root / path).splitlines(keepends=True),
                f"{path} (before)",
                f"{path} (after)",
            )
        )
        for path, before in originals.items()
    )
    return RepoFix(answer.dockerfile.strip() + "\n", source_diff, answer.rationale, tuple(originals))


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


def build_graph(
    deploy_fn: DeployFn,
    llm_patch_fn: LlmPatchFn | None = None,
    emit: EmitFn | None = None,
    repair_fn: RepairFn | None = None,
    confirm_fn: ConfirmFn | None = None,
):
    llm_patch = llm_patch_fn or default_llm_patch

    def _emit(event: PipelineEvent) -> None:
        if emit:
            emit(event)

    def classify(state: HealState) -> HealState:
        error_log = state.get("error_log", "")
        update: HealState = {"error_category": classify_error(error_log)}
        # The permission rule fixes a traceback that would otherwise read as an app-code error.
        app_stop = (
            not repair_fn
            and _stops_on_app_code(error_log)
            and _patch_permission(state.get("current_dockerfile", ""), error_log) is None
        )
        # needs_human_stop applies even with the repo agent: it must not invent secrets or a DB.
        if (
            _unhealable(error_log)
            or needs_human_stop(error_log, state["src_dir"], state["current_dockerfile"])
            or app_stop
        ):
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
        source_diff = ""
        fix = None
        if result is None and repair_fn:
            fix = repair_fn(state["src_dir"], before, error_log, category)
            if fix and (fix.dockerfile != before or fix.source_diff):
                result, source_diff = (fix.dockerfile, fix.rationale), fix.source_diff
            source = "llm"
        elif result is None:
            after = llm_patch(before, error_log, category)
            result = (after, "LLM-proposed fix") if after and after != before else None
            source = "llm"
        if result is None:
            return {"status": "gave_up"}  # no patch applied -> attempt not counted

        after, rationale = result
        history = state.get("history", [])
        seen = {before.strip(), *(r.before.strip() for r in history), *(r.after.strip() for r in history)}
        if not source_diff and after.strip() in seen:
            return {"status": "gave_up"}  # unchanged patch or toggle back to an already failed image
        diff = _unified_diff(before, after) + source_diff
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
        # A source edit goes out only after confirm_fn (main.py: PR + ask the user) says so.
        stop = confirm_fn(state["src_dir"], fix) if (confirm_fn and source_diff) else None
        return {
            "current_dockerfile": after,
            "diff": diff,
            "history": [*state.get("history", []), record],
            "retry_count": attempt,
            "status": "gave_up" if stop else "healing",
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
    repair_fn: RepairFn | None = None,
    confirm_fn: ConfirmFn | None = None,
) -> HealReport:
    """Entry point for main.py: call after the first deploy failed.

    Policy: with the repo agent (default when ANTHROPIC_API_KEY is set and no `llm_patch_fn` is
    injected) the Dockerfile and the source in `src_dir` are both patched; a source edit is
    redeployed only if `confirm_fn` (when given) does not stop it. Without the agent only the
    Dockerfile is patched; a crash in the app's own code stops at once, and for import-time crashes
    a source fix is suggested as a diff in the summary, never applied.
    """
    if repair_fn is None and llm_patch_fn is None and os.environ.get("ANTHROPIC_API_KEY"):
        repair_fn = default_repo_repair
    stopped: list[str] = []

    def confirm(src: str, fix: RepoFix) -> str | None:
        stop = confirm_fn(src, fix)
        if stop:
            stopped.append(stop)
        return stop

    app = build_graph(deploy_fn, llm_patch_fn, emit, repair_fn, confirm if confirm_fn else None)
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
    human = (
        None if (success or blocked) else needs_human_stop(error_log, src_dir, final["current_dockerfile"])
    )
    app_error = None if (success or blocked or human or repair_fn) else _stops_on_app_code(error_log)
    if success:
        summary = f"Healed after {len(records)} patch(es): " + "; ".join(r.rationale for r in records)
    elif stopped:
        summary = stopped[-1]
    elif human:
        summary = human + (f" (after {len(records)} patch(es))" if records else "")
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
