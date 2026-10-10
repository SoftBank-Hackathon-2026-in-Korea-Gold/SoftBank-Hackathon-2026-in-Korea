"""CloudMorph FastAPI orchestrator (owner: 백락원).

Existing API contracts are preserved:
 POST /deploy -> {deployment_id}
 GET /deploy/{deployment_id}/events -> SSE PipelineEvent JSON

Optional read-only endpoint:
 GET /deploy/{deployment_id} -> job status and target results.

This module orchestrates the team's analyzer, deployer, and healer unchanged.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import analyzer, deployer, env_store, fixpr, healer, webhooks
from app import fleet as fleetmod
from app import nodes as nodepool
from app.schemas import AnalysisResult, DeployRequest, DeployTarget, EnvInput, EnvRequirement, PipelineEvent

load_dotenv()

# Single-process, in-memory demo job registry. Run uvicorn with --workers 1.
MAX_JOBS = 30
MAX_EVENTS = 1000
MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_SOURCE_FILES = 10_000  # Includes directories to bound traversal as well.
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_SOURCE_BYTES
MAX_ZIP_ENTRIES = MAX_SOURCE_FILES
try:
    MAX_CONCURRENT_GIT_CLONES = int(os.getenv("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES", "2"))
except ValueError as exc:
    raise ValueError("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES must be a positive integer") from exc
if MAX_CONCURRENT_GIT_CLONES < 1:
    raise ValueError("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES must be a positive integer")
GIT_CLONE_WAIT_SECONDS = 5
ENV_INPUT_TIMEOUT_SECONDS = 15 * 60  # how long an `ask_env` job waits for POST /deploy/{id}/env
_git_clone_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_GIT_CLONES)
SOURCE_IGNORE = {".git", ".venv", "node_modules", ".deployer", "__pycache__"}
SENSITIVE_DIRS = {".aws", ".azure", ".gnupg", ".kube", ".ssh"}
SENSITIVE_CONFIG_DIRS = {(".config", "gcloud"), (".config", "gh")}
SAFE_ENV_TEMPLATE_NAMES = {".env.example"}
SENSITIVE_FILE_NAMES = {
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}
SENSITIVE_FILE_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".p7b", ".p7c", ".p8", ".pem", ".pfx")
_LOCAL_SOURCE_DENY_ROOTS = tuple(
    Path(p) for p in ("/", "/dev", "/etc", "/proc", "/root", "/run", "/sys", "/var/run")
)
_logger = logging.getLogger(__name__)
# API token guard: every path under these prefixes needs CLOUDMORPH_API_TOKEN. /health and the
# GitHub webhook (HMAC-verified on its own) stay open.
PROTECTED_PREFIXES = ("/deploy", "/fleet", "/projects")
# "testclient" is Starlette TestClient's client host; ASGI servers only ever report IPs here.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "forwarded")


def _is_protected_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES)


def _is_direct_local_request(request: Request) -> bool:
    """True only for a loopback client that did not come through a tunnel or reverse proxy."""
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK_HOSTS:
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


@dataclass
class Job:
    created_at: float = field(default_factory=time.time)
    state: str = "queued"
    events: deque[PipelineEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    subscribers: set[asyncio.Queue[PipelineEvent | None]] = field(default_factory=set)
    targets: dict[str, dict] = field(default_factory=dict)
    error: str | None = None
    completed: bool = False
    questions: dict[str, SimpleQueue[str]] = field(default_factory=dict)  # open question_id -> answer
    # ask_env: what the job is waiting for, and the user's answer (memory only, never put in events)
    required_env: list[EnvRequirement] = field(default_factory=list)
    env_values: dict[str, str] | None = None
    env_ready: threading.Event = field(default_factory=threading.Event)


_jobs: dict[str, Job] = {}
REPO_ROOT = Path(__file__).resolve().parents[2]
# name -> last deployment of that app (what a GitHub push updates). Persisted to a JSON file so the
# dashboard's project list survives a backend restart (jobs/events themselves stay in memory).
PROJECTS_FILE = Path(
    os.getenv(
        "CLOUDMORPH_PROJECTS_FILE", Path(__file__).resolve().parents[1] / ".cloudmorph" / "projects.json"
    )
)


def _load_projects() -> dict[str, dict]:
    try:
        return json.loads(PROJECTS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_projects() -> None:
    try:
        PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_FILE.write_text(json.dumps(_projects, ensure_ascii=False, indent=2))
    except OSError:
        pass  # persistence is best-effort; the in-memory copy is still correct


_projects: dict[str, dict] = _load_projects()
# One deployment per app name at a time: two quick pushes would otherwise race on the same Cloud Run
# service (candidate tag / promotion) and the same local container name. Later pushes wait their turn.
_app_locks: dict[str, threading.Lock] = {}


def _validate_https_git_url(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Git source must be a public HTTPS URL")
    if parsed.port not in (None, 443):
        raise ValueError("Nonstandard Git HTTPS ports are not supported")
    # This service is intended for trusted hackathon users only. An allowlist
    # prevents passing arbitrary internal HTTP endpoints to git.
    allowed = set(os.getenv("CLOUDMORPH_GIT_HOSTS", "github.com").lower().split(","))
    if parsed.hostname.lower() not in {h.strip() for h in allowed}:
        raise ValueError("Git host is not on CLOUDMORPH_GIT_HOSTS allowlist")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IP-address Git URLs are not permitted")
    if parsed.query or parsed.fragment or not parsed.path.strip("/"):
        raise ValueError("Invalid Git repository URL")
    return source


def _extract_zip_safe(zip_path: Path, destination: Path) -> Path:
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP contents exceed file count limit")
        total = 0
        safe_members = []
        for item in members:
            name = item.filename.replace("\\", "/")
            parts = Path(name).parts
            if (
                name.startswith("/")
                or ".." in parts
                or any(part.endswith(":") for part in parts)
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe ZIP entry detected")
            total += item.file_size
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ValueError("ZIP contents exceed size limit")
            if not _is_sensitive_source_path(parts):
                safe_members.append(item)
        archive.extractall(destination, safe_members)
    top = list(destination.iterdir())
    return top[0] if len(top) == 1 and top[0].is_dir() else destination


def _is_sensitive_source_path(parts: tuple[str, ...]) -> bool:
    lowered_parts = tuple(part.lower() for part in parts)
    if any(part in SENSITIVE_DIRS for part in lowered_parts):
        return True
    if any(
        lowered_parts[index : index + 2] in SENSITIVE_CONFIG_DIRS for index in range(len(lowered_parts) - 1)
    ):
        return True
    if any(
        lowered_parts[index : index + 2] == (".docker", "config.json")
        for index in range(len(lowered_parts) - 1)
    ):
        return True

    name = lowered_parts[-1]
    return (
        name == ".env"
        or (name.startswith(".env.") and name not in SAFE_ENV_TEMPLATE_NAMES)
        or name in SENSITIVE_FILE_NAMES
        or name.startswith(("id_rsa", "id_ecdsa", "id_ed25519", "id_dsa", "terraform.tfstate."))
        or name.endswith(SENSITIVE_FILE_SUFFIXES)
        or ("service-account" in name and name.endswith(".json"))
    )


def _validate_local_source(path: Path) -> Path:
    if path.is_symlink():
        raise OSError(errno.ELOOP, "Local source root must not be a symlink", str(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Local source does not exist or cannot be resolved") from exc

    for denied in _LOCAL_SOURCE_DENY_ROOTS:
        if (denied == Path("/") and resolved == denied) or (
            denied != Path("/") and (resolved == denied or denied in resolved.parents)
        ):
            raise ValueError("Local source is under a protected system directory")
    resolved_parts = tuple(part.lower() for part in resolved.parts)
    if any(part in SENSITIVE_DIRS for part in resolved_parts) or any(
        resolved_parts[index : index + 2] in SENSITIVE_CONFIG_DIRS for index in range(len(resolved_parts) - 1)
    ):
        raise ValueError("Local source is under a protected credentials directory")
    return resolved


def _inspect_source(source: str | Path, destination: Path | None = None) -> None:
    """Validate or copy a bounded tree without following links, including during races.

    Directory descriptors anchor traversal; O_NOFOLLOW also rejects entries
    replaced by symlinks after inspection. Sensitive files are omitted from copied trees.
    """
    total = count = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(directory_fd: int, output: Path | None, relative_parts: tuple[str, ...]) -> None:
        nonlocal total, count
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                entry_parts = (*relative_parts, entry.name)
                if entry.name in SOURCE_IGNORE or _is_sensitive_source_path(entry_parts):
                    continue
                count += 1
                if count > MAX_SOURCE_FILES:
                    raise ValueError("Source exceeds file count limit")
                info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                    try:
                        child_output = output / entry.name if output is not None else None
                        if child_output is not None:
                            child_output.mkdir()
                        visit(child_fd, child_output, entry_parts)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode):
                    if output is None:
                        total += info.st_size
                        if total > MAX_SOURCE_BYTES:
                            raise ValueError("Source exceeds size limit")
                        continue
                    file_fd = os.open(
                        entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
                    )
                    with os.fdopen(file_fd, "rb") as incoming:
                        opened = os.fstat(incoming.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            raise ValueError("Source contains a non-regular file")
                        with (output / entry.name).open("xb") as outgoing:
                            while chunk := incoming.read(1024 * 1024):
                                total += len(chunk)
                                if total > MAX_SOURCE_BYTES:
                                    raise ValueError("Source exceeds size limit")
                                outgoing.write(chunk)
                        (output / entry.name).chmod(stat.S_IMODE(opened.st_mode) & 0o777)
                else:
                    raise ValueError("Source contains a symlink or non-regular file")

    root_fd = os.open(source, directory_flags)
    try:
        if destination is not None:
            destination.mkdir()
        visit(root_fd, destination, ())
    finally:
        os.close(root_fd)


def _source_identity(source: str) -> str:
    """Stable id of where an app's code comes from, so saved env values only go back to the same source.
    Spellings of one repo agree ('https://GitHub.com/Octo/App.git/' == 'https://github.com/octo/app');
    the branch is left out so every branch of a repo shares its values."""
    if source.startswith("https://"):
        parsed = urlparse(source)
        path = parsed.path.strip("/").removesuffix(".git").lower()
        return f"git:{(parsed.hostname or '').lower()}/{path}"
    path = Path(source).expanduser()
    if not path.is_absolute() and not path.exists() and (REPO_ROOT / source).exists():
        path = REPO_ROOT / source  # same preset rule as prepare_source
    return f"path:{path.resolve()}"


@contextmanager
def prepare_source(source: str, ref: str | None = None) -> Iterator[str]:
    """Prepare a local directory, local ZIP, or allowlisted public Git URL.

    Source is treated as trusted input: this is a hackathon demo API and must
    not be exposed publicly without authentication and isolation.
    """
    path = None if source.startswith("https://") else Path(source).expanduser()
    if path is not None and not path.is_absolute() and not path.exists() and (REPO_ROOT / source).exists():
        path = REPO_ROOT / source  # dashboard presets like "sample-apps/guestbook" are relative to the repo
    with tempfile.TemporaryDirectory(prefix="cloudmorph-source-") as temp:
        root = Path(temp)
        if source.startswith("https://"):
            url = _validate_https_git_url(source)
            dest = root / "project"
            if not _git_clone_semaphore.acquire(timeout=GIT_CLONE_WAIT_SECONDS):
                raise ValueError("Git clone capacity reached; retry later")
            try:
                try:
                    subprocess.run(
                        [
                            "git",
                            "-c",
                            "protocol.file.allow=never",
                            "clone",
                            "--depth",
                            "1",
                            *(["--branch", ref] if ref else []),
                            "--",
                            url,
                            str(dest),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    raise ValueError(f"Git clone failed: {type(exc).__name__}") from exc
            finally:
                _git_clone_semaphore.release()
            yield _copy_for_target(str(dest), root / "prepared")
            return
        if path is not None and path.is_dir():
            local_source = _validate_local_source(path)
            yield _copy_for_target(str(local_source), root / "prepared")
            return
        if path is not None and path.is_file() and path.suffix.lower() == ".zip":
            zip_source = _validate_local_source(path)
            extracted = _prepare_zip(zip_source, root)
            yield _copy_for_target(extracted, root / "prepared")
            return
        raise ValueError("Source must be an existing directory, local ZIP, or allowed HTTPS Git URL")


def _prepare_zip(path: Path, root: Path) -> str:
    dest = root / "source"
    dest.mkdir(parents=True)
    return str(_extract_zip_safe(path, dest))


def _copy_for_target(source: str, destination: Path) -> str:
    """Deployer writes Dockerfile and .deployer state; isolate each target."""
    _inspect_source(source, destination)
    return str(destination)


def _remove_workspace(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError:
        _logger.warning("Could not remove completed Cloud Run workspace %s", path, exc_info=True)


ANSWER_TIMEOUT_S = 300  # no answer by then -> stop; the PR stays open


class AnswerRequest(BaseModel):
    """POST /deploy/{deployment_id}/answer body."""

    question_id: str
    choice: Literal["stop", "continue"]


PR_MERGE_HINT = (
    "Merge it, then deploy again (if the repo's GitHub webhook points here, the merge redeploys by itself)."
)


def source_fix_confirm(
    job: Job, emit: Callable[[PipelineEvent], None], source: str, ref: str | None, trigger: str
) -> healer.ConfirmFn:
    """healer's confirm_fn for one job: open a PR with the source fix, then ask the user whether to stop
    or deploy the edited copy now. Asked once per job; later targets reuse the answer.
    A GitHub-push deployment has nobody watching, so it stops without asking."""
    decided: dict = {}

    def confirm(src_dir: str, fix: healer.RepoFix) -> str | None:
        if not decided:
            pr_url = pr_error = None
            if source.startswith("https://"):
                try:
                    pr_url = fixpr.open_pr(
                        source, ref, src_dir, list(fix.changed), fix.rationale, fix.source_diff
                    )
                except Exception as exc:  # no PR still leaves the question
                    _logger.warning("could not open the fix PR", exc_info=True)
                    pr_error = f"{type(exc).__name__}: {exc}"
            question_id = uuid.uuid4().hex[:12]
            answers: SimpleQueue[str] = SimpleQueue()
            ask = trigger != "github-push"
            if ask:
                job.questions[question_id] = answers
            emit(
                PipelineEvent(
                    type="log",
                    stage="heal",
                    payload={
                        "kind": "question",
                        "question_id": question_id,
                        "ask": ask,
                        "pr_url": pr_url,
                        "pr_error": pr_error,
                        "rationale": fix.rationale,
                        "diff": fix.source_diff,
                        "choices": ["stop", "continue"],
                        "timeout_s": ANSWER_TIMEOUT_S,
                    },
                )
            )
            choice, reason = "stop", "github-push"
            if ask:
                try:
                    choice, reason = answers.get(timeout=ANSWER_TIMEOUT_S), "user"
                except Empty:
                    reason = "timeout"
                finally:
                    job.questions.pop(question_id, None)
            decided.update(choice=choice, pr_url=pr_url)
            emit(
                PipelineEvent(
                    type="log",
                    stage="heal",
                    payload={
                        "kind": "answer",
                        "question_id": question_id,
                        "choice": choice,
                        "reason": reason,
                    },
                )
            )
        if decided["choice"] == "continue":
            return None
        if decided["pr_url"]:
            return f"Stopped: the source fix is up as a PR ({decided['pr_url']}). {PR_MERGE_HINT}"
        return (
            "Stopped: the source fix was not deployed. Apply the diff to your repository, then deploy again."
        )

    return confirm


def _env_overrides(required: list[EnvRequirement], values: dict[str, str]) -> dict:
    """deployer.deploy options for the user's values (blank ones are left out)."""
    env: dict[str, str] = {}
    build_args: dict[str, str] = {}
    build_secrets: dict[str, str] = {}
    for item in required:
        value = values.get(item.name)
        if not value:
            continue
        if item.scope == "runtime":
            env[item.name] = value
        elif item.secret:
            build_secrets[item.name] = value
        else:
            build_args[item.name] = value
    options = {"env": env, "build_args": build_args, "build_secrets": build_secrets}
    return {k: v for k, v in options.items() if v}


def run_pipeline(
    source: str,
    target: DeployTarget,
    emit: Callable[[PipelineEvent], None],
    analysis: AnalysisResult | None = None,
    confirm: healer.ConfirmFn | None = None,
    overrides: dict | None = None,
) -> bool:
    """Run one target synchronously; healer owns all retry attempts.
    `overrides` (deployer options such as env/build_secrets) apply to the healer's redeploys too."""
    if analysis is None:  # Preserve backward compatibility for direct calls.
        emit(PipelineEvent(type="stage", stage="analyze"))
        analysis = analyzer.analyze(source)

    deploy_fn = functools.partial(deployer.deploy, **overrides) if overrides else deployer.deploy
    emit(PipelineEvent(type="stage", stage="deploy", payload={"target": target}))
    result = deploy_fn(source, analysis.dockerfile, target)
    if result.success:
        emit(PipelineEvent(type="done", payload={"target": target, "url": result.url}))
        return True

    emit(
        PipelineEvent(
            type="stage",
            stage="heal",
            payload={
                "target": target,
                "stderr": result.stderr[-2000:],
            },
        )
    )
    report = healer.heal(
        source, target, result, analysis.dockerfile, deploy_fn, emit=emit, confirm_fn=confirm
    )
    payload = report.model_dump(mode="json")
    emit(PipelineEvent(type="done" if report.success else "error", payload=payload))
    return report.success


def _publish(job: Job, event: PipelineEvent | None) -> None:
    if event is None:
        job.completed = True
    else:
        job.events.append(event)
        if event.type in ("done", "error"):
            target = event.payload.get("target")
            if target:
                job.targets[target] = {
                    "success": event.type == "done",
                    "url": event.payload.get("url") or event.payload.get("final_url"),
                    "detail": event.payload,
                }
    for subscriber in list(job.subscribers):
        subscriber.put_nowait(event)


def create_app() -> FastAPI:
    app = FastAPI(title="CloudMorph", version="0.2.0")
    origins = [
        x.strip()
        for x in os.getenv("CLOUDMORPH_CORS_ORIGINS", "http://localhost:5173,http://localhost:5174").split(
            ","
        )
        if x.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Token"],
    )

    # Demo guard: with CLOUDMORPH_API_TOKEN set, every PROTECTED_PREFIXES call must carry it.
    # EventSource cannot set headers, so ?token= is accepted too. Unset -> fail closed: only a
    # direct loopback client (local dev) passes; anything via a tunnel/proxy or remote gets 503.
    @app.middleware("http")
    async def require_api_token(request: Request, call_next):
        if request.method == "OPTIONS" or not _is_protected_path(request.url.path):
            return await call_next(request)
        token = os.getenv("CLOUDMORPH_API_TOKEN")
        if not token:
            if _is_direct_local_request(request):
                return await call_next(request)
            return JSONResponse(
                {"detail": "API token not configured; set CLOUDMORPH_API_TOKEN"}, status_code=503
            )
        supplied = request.headers.get("x-api-token") or request.query_params.get("token") or ""
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse({"detail": "missing or invalid X-API-Token"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- node pool / fleet (target "node") -------------------------------------------------------
    fleet_events: deque[dict] = deque(maxlen=200)

    @app.on_event("startup")
    async def start_fleet_watcher() -> None:
        # Opt-in: CLOUDMORPH_FLEET_WATCH=1 starts the auto scale-out watcher inside the API process.
        if os.getenv("CLOUDMORPH_FLEET_WATCH") == "1" and nodepool.load_registry().nodes:
            fleetmod.start_background_watcher(
                float(os.getenv("CLOUDMORPH_FLEET_INTERVAL", "10")), emit=fleet_events.append
            )

    @app.get("/fleet")
    async def fleet_status() -> dict:
        """Nodes (with live load), apps, replicas and recent scale events — for the dashboard."""
        reg = nodepool.load_registry()
        state = fleetmod.load_state()
        metrics = await asyncio.to_thread(
            lambda: nodepool.metrics_dict([nodepool.probe(n) for n in reg.nodes])
        )
        return {
            "router": {"host": reg.router.host, "public_port": reg.router.public_port}
            if reg.router
            else None,
            "nodes": [
                {**m, "provider": n.provider, "arch": n.arch, "host": n.host}
                for n, m in zip(reg.nodes, metrics, strict=True)
            ],
            "apps": [
                {
                    "name": a.name,
                    "url": f"http://{a.hostname}",
                    "image": a.image,
                    "replicas": [
                        {"node": r.node, "upstream": r.upstream, "cpu": r.last_cpu} for r in a.replicas
                    ],
                    "events": a.events[-10:],
                }
                for a in state.apps.values()
            ],
            "watch": list(fleet_events)[-20:],
        }

    @app.post("/fleet/{app_name}/scale/{n}")
    async def fleet_scale(app_name: str, n: int, request: Request) -> dict:
        token = os.getenv("CLOUDMORPH_API_TOKEN")
        if token and request.headers.get("x-api-token") != token:
            raise HTTPException(status_code=401, detail="missing or invalid X-API-Token")
        if app_name not in fleetmod.load_state().apps:
            raise HTTPException(status_code=404, detail="unknown app")
        result = await asyncio.to_thread(fleetmod.scale, app_name, n)
        return {"app": app_name, "replicas": [r.upstream for r in result.replicas]}

    def _launch(
        source: str,
        targets: list[str],
        *,
        name: str | None = None,
        ref: str | None = None,
        intro: list[str] | None = None,
        trigger: str = "api",
        ask_env: bool = False,
    ) -> str:
        """Create a job and run analyze -> deploy -> heal for every target in a worker thread.
        Shared by POST /deploy (user click) and POST /webhook/github (push)."""
        if not targets or len(targets) != len(set(targets)):
            raise HTTPException(status_code=422, detail="targets must be non-empty and unique")
        if name is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise HTTPException(status_code=422, detail="name must match [a-z0-9][a-z0-9-]{0,39}")
        if len(_jobs) >= MAX_JOBS:
            # Retire oldest *completed* job; never evict a running job.
            finished = [key for key, val in _jobs.items() if val.completed]
            if not finished:
                raise HTTPException(status_code=503, detail="deployment capacity reached")
            _jobs.pop(min(finished, key=lambda key: _jobs[key].created_at))

        deployment_id = uuid.uuid4().hex[:12]
        app_name = name or f"cloudmorph-{deployment_id}"
        job = Job(targets={t: {"success": None, "url": None} for t in targets})
        _jobs[deployment_id] = job
        project = _projects.setdefault(
            app_name, {"name": app_name, "source": source, "ref": ref, "history": []}
        )
        project.update(
            source=source,
            ref=ref,
            targets=targets,
            last_deployment_id=deployment_id,
            last_status="running",
            trigger=trigger,
            updated_at=time.time(),
        )
        project["history"] = [*project["history"][-19:], deployment_id]
        _save_projects()
        loop = asyncio.get_running_loop()

        def emit(event: PipelineEvent) -> None:
            loop.call_soon_threadsafe(_publish, job, event)

        def execute() -> None:
            for line in intro or []:
                emit(PipelineEvent(type="log", payload={"line": line}))
            lock = _app_locks.setdefault(app_name, threading.Lock())
            if not lock.acquire(blocking=False):
                emit(
                    PipelineEvent(
                        type="log",
                        payload={
                            "line": f"queue: waiting for the previous deployment of {app_name} to finish"
                        },
                    )
                )
                lock.acquire()
            try:
                _run_locked()
            finally:
                lock.release()

        def _run_locked() -> None:
            with prepare_source(source, ref) as source_dir:
                emit(PipelineEvent(type="stage", stage="analyze"))
                analysis = analyzer.analyze(source_dir)
                emit(
                    PipelineEvent(
                        type="log",
                        stage="analyze",
                        payload={
                            "line": f"analyzer: target={analysis.target} language={analysis.language} framework={analysis.framework} "
                            f"port={analysis.port} service_models={analysis.service_models}"
                        },
                    )
                )
                for note in analysis.notes[:8]:
                    emit(PipelineEvent(type="log", stage="analyze", payload={"line": f"analyzer: {note}"}))
                overrides = _env_overrides(analysis.required_env, _resolve_env(analysis, source_dir))
                # Separate target workspaces because deployer writes Dockerfile and may leave background
                # local container/tunnel artifacts. The folder name is the app/service name the deployer uses,
                # so a stable `name` means "update the same service" (CD) instead of "create another one";
                # the default name is per-deployment unique. Local state is retained for container/tunnel cleanup.
                work_root = Path(tempfile.mkdtemp(prefix="cloudmorph-deploy-"))
                confirm = source_fix_confirm(job, emit, source, ref, trigger)
                for target in targets:
                    target_dir = work_root / f"{app_name}-{target}"
                    try:
                        target_dir = Path(_copy_for_target(source_dir, target_dir))
                        run_pipeline(
                            target_dir, target, emit, analysis=analysis, overrides=overrides, confirm=confirm
                        )
                    except Exception as exc:  # noqa: BLE001 — isolate target failures
                        emit(
                            PipelineEvent(
                                type="error",
                                payload={"target": target, "message": f"{type(exc).__name__}: {exc}"},
                            )
                        )
                    finally:
                        if target == "cloudrun":
                            _remove_workspace(target_dir)
                if "local" not in targets:
                    _remove_workspace(work_root)

        def _resolve_env(analysis: AnalysisResult, source_dir: str) -> dict[str, str]:
            """Values saved for this app (named deploys only) first; the dashboard is asked only for the rest.
            A blank runtime secret that may be random gets one, and new values are saved for the next deploy,
            so a GitHub push redeploy keeps the keys and SECRET_KEY stays the same. Values are kept per
            name *and* source: another repo deployed under the same name is asked afresh, never handed these."""
            if not analysis.required_env:
                return {}
            owner = _source_identity(source)
            values = env_store.load(app_name, owner) if name else {}
            saved = dict(values)
            if used := sorted({e.name for e in analysis.required_env if values.get(e.name)}):
                emit(
                    PipelineEvent(
                        type="log", stage="analyze", payload={"line": f"env: 저장된 값 {', '.join(used)}"}
                    )
                )
            missing = [e for e in analysis.required_env if not values.get(e.name)]
            if not missing:
                return values
            if not ask_env:  # scripts and GitHub push: nobody to ask
                lacking = ", ".join(sorted({e.name for e in missing}))
                emit(
                    PipelineEvent(
                        type="log", stage="analyze", payload={"line": f"env: 저장된 값 없음 {lacking}"}
                    )
                )
                return values
            values |= _ask_env(missing, source_dir)
            for e in missing:
                if e.scope == "runtime" and e.generate and not values.get(e.name):
                    values[e.name] = secrets.token_urlsafe(32)
            if name and values != saved:
                try:
                    env_store.save(app_name, owner, values)
                except env_store.EnvStoreError as exc:
                    line = f"env: 값을 저장하지 못해 다음 배포에서 다시 묻습니다 ({exc})"
                    emit(PipelineEvent(type="log", stage="analyze", payload={"line": line}))
            return values

        def _ask_env(items: list[EnvRequirement], source_dir: str) -> dict[str, str]:
            """Pause until the user answers POST /deploy/{id}/env. Only names and evidence go out, never values.
            `auto` marks what the deployer provisions when left blank, so the card can say so honestly."""
            postgres = deployer.wants_database(Path(source_dir), deployer.Config.from_env()) == "postgres"
            job.required_env = [
                e.model_copy(update={"auto": True}) if postgres and e.name == "DATABASE_URL" else e
                for e in items
            ]
            job.state = "waiting_input"
            emit(
                PipelineEvent(
                    type="input_required",
                    stage="analyze",
                    payload={
                        "items": [e.model_dump() for e in job.required_env],
                        "timeout_sec": ENV_INPUT_TIMEOUT_SECONDS,
                    },
                )
            )
            if not job.env_ready.wait(ENV_INPUT_TIMEOUT_SECONDS):
                raise TimeoutError(
                    f"배포에 필요한 값을 {ENV_INPUT_TIMEOUT_SECONDS // 60}분 안에 받지 못해 배포를 멈췄습니다"
                )
            job.state = "running"
            values, job.env_values = job.env_values or {}, None
            given = sorted(values)
            emit(
                PipelineEvent(
                    type="log",
                    stage="analyze",
                    payload={"line": f"env: 입력받은 값 {', '.join(given) if given else '없음'}"},
                )
            )
            return values

        async def worker() -> None:
            job.state = "running"
            try:
                await asyncio.to_thread(execute)
                # callbacks queued by execute are processed by the event loop
                await asyncio.sleep(0)
                if all(v.get("success") is True for v in job.targets.values()):
                    job.state = "completed"
                elif any(v.get("success") is True for v in job.targets.values()):
                    job.state = "partial_failure"
                else:
                    job.state = "failed"
            except Exception as exc:  # noqa: BLE001 — record unexpected pipeline failures
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                _publish(job, PipelineEvent(type="error", payload={"message": job.error}))
            finally:
                project.update(
                    last_status=job.state,
                    urls={t: v.get("url") for t, v in job.targets.items()},
                    updated_at=time.time(),
                )
                _save_projects()
                _publish(job, None)

        asyncio.create_task(worker())
        return deployment_id

    app.state.launch = _launch  # tests swap this for a fake

    @app.post("/deploy")
    async def start_deploy(req: DeployRequest) -> dict[str, str]:
        return {
            "deployment_id": app.state.launch(
                req.source, req.targets, name=req.name, ref=req.ref, ask_env=req.ask_env
            )
        }

    @app.post("/deploy/{deployment_id}/env")
    async def submit_env(deployment_id: str, body: EnvInput) -> dict:
        """Answer an `input_required` event. Blank values are skipped; the job resumes right away."""
        job = _jobs.get(deployment_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")
        if job.state != "waiting_input" or job.env_ready.is_set():
            raise HTTPException(status_code=409, detail="deployment is not waiting for input")
        wanted = {e.name for e in job.required_env}
        job.env_values = {k: v for k, v in body.values.items() if k in wanted and v}
        job.env_ready.set()
        return {"accepted": sorted(job.env_values)}

    @app.get("/projects")
    async def projects() -> dict:
        """Apps known to this server (by stable name) with their last deployment — what CD updates."""
        return {"projects": sorted(_projects.values(), key=lambda p: -p.get("updated_at", 0))}

    @app.post("/webhook/github")
    async def github_webhook(request: Request) -> dict:
        """GitHub push -> redeploy the repository under its own name (CD). Secret-signed; default branch only."""
        secret = os.getenv("CLOUDMORPH_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(status_code=503, detail="CLOUDMORPH_WEBHOOK_SECRET not configured")
        body = await request.body()
        if not webhooks.verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(status_code=401, detail="bad signature")
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"ok": True, "event": "ping"}
        if event != "push":
            return {"ok": True, "ignored": event}
        info = webhooks.parse_push(json.loads(body))
        if info is None or info.deleted:
            return {"ok": True, "ignored": "not a branch push"}
        if not webhooks.branch_allowed(info):
            return {"ok": True, "ignored": f"branch {info.branch} (not in CD branches)"}
        targets = webhooks.cd_targets()
        intro = [
            f"github push by {info.pusher}: {info.repo_full_name}@{info.branch} {info.sha} — {info.message}"
        ]
        deployment_id = app.state.launch(
            info.clone_url, targets, name=info.app_name, ref=info.branch, intro=intro, trigger="github-push"
        )
        return {
            "ok": True,
            "deployment_id": deployment_id,
            "name": info.app_name,
            "targets": targets,
            "branch": info.branch,
            "sha": info.sha,
        }

    @app.get("/deploy/{deployment_id}")
    async def deploy_status(deployment_id: str) -> dict:
        job = _jobs.get(deployment_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")
        return {
            "deployment_id": deployment_id,
            "status": job.state,
            "targets": job.targets,
            "error": job.error,
            "completed": job.completed,
        }

    @app.post("/deploy/{deployment_id}/answer")
    async def answer(deployment_id: str, req: AnswerRequest) -> dict[str, str]:
        """Answer a healer question (see source_fix_confirm): stop here, or continue with the edited source."""
        job = _jobs.get(deployment_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")
        answers = job.questions.pop(req.question_id, None)
        if answers is None:
            raise HTTPException(status_code=409, detail="question is not open (answered or timed out)")
        answers.put(req.choice)
        return {"choice": req.choice}

    @app.get("/deploy/{deployment_id}/events")
    async def events(deployment_id: str) -> EventSourceResponse:
        job = _jobs.get(deployment_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")

        async def stream():
            queue: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
            # Snapshot + subscription are atomic on the single event loop.
            history = list(job.events)
            if not job.completed:
                job.subscribers.add(queue)
            try:
                for event in history:
                    yield {"event": event.type, "data": event.model_dump_json()}
                if job.completed:
                    return
                while (event := await queue.get()) is not None:
                    yield {"event": event.type, "data": event.model_dump_json()}
            finally:
                job.subscribers.discard(queue)

        return EventSourceResponse(stream(), ping=15)

    return app


app = create_app()
