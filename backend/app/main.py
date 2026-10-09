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
import hmac
import ipaddress
import logging
import os
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
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app import analyzer, deployer, healer
from app.schemas import AnalysisResult, DeployRequest, DeployTarget, PipelineEvent

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


_jobs: dict[str, Job] = {}


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


@contextmanager
def prepare_source(source: str) -> Iterator[str]:
    """Prepare a local directory, local ZIP, or allowlisted public Git URL.

    Source is treated as trusted input: this is a hackathon demo API and must
    not be exposed publicly without authentication and isolation.
    """
    path = None if source.startswith("https://") else Path(source).expanduser()
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


def run_pipeline(
    source: str,
    target: DeployTarget,
    emit: Callable[[PipelineEvent], None],
    analysis: AnalysisResult | None = None,
) -> bool:
    """Run one target synchronously; healer owns all retry attempts."""
    if analysis is None:  # Preserve backward compatibility for direct calls.
        emit(PipelineEvent(type="stage", stage="analyze"))
        analysis = analyzer.analyze(source)

    emit(PipelineEvent(type="stage", stage="deploy", payload={"target": target}))
    result = deployer.deploy(source, analysis.dockerfile, target)
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
    report = healer.heal(source, target, result, analysis.dockerfile, deployer.deploy, emit=emit)
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

    @app.post("/deploy")
    async def start_deploy(req: DeployRequest) -> dict[str, str]:
        if not req.targets or len(req.targets) != len(set(req.targets)):
            raise HTTPException(status_code=422, detail="targets must be non-empty and unique")
        if len(_jobs) >= MAX_JOBS:
            # Retire oldest *completed* job; never evict a running job.
            finished = [key for key, val in _jobs.items() if val.completed]
            if not finished:
                raise HTTPException(status_code=503, detail="deployment capacity reached")
            _jobs.pop(min(finished, key=lambda key: _jobs[key].created_at))

        deployment_id = uuid.uuid4().hex[:12]
        job = Job(targets={t: {"success": None, "url": None} for t in req.targets})
        _jobs[deployment_id] = job
        loop = asyncio.get_running_loop()

        def emit(event: PipelineEvent) -> None:
            loop.call_soon_threadsafe(_publish, job, event)

        def execute() -> None:
            with prepare_source(req.source) as source_dir:
                emit(PipelineEvent(type="stage", stage="analyze"))
                analysis = analyzer.analyze(source_dir)
                # Separate target workspaces because deployer writes Dockerfile
                # and may leave background local container/tunnel artifacts.
                # Local deployment state is retained for container/tunnel cleanup.
                work_root = Path(tempfile.mkdtemp(prefix="cloudmorph-deploy-"))
                for target in req.targets:
                    target_dir = work_root / f"cloudmorph-{deployment_id}-{target}"
                    try:
                        target_dir = Path(_copy_for_target(source_dir, target_dir))
                        run_pipeline(target_dir, target, emit, analysis=analysis)
                    except Exception as exc:  # noqa: BLE001 — isolate target failures
                        emit(
                            PipelineEvent(
                                type="error",
                                payload={
                                    "target": target,
                                    "message": f"{type(exc).__name__}: {exc}",
                                },
                            )
                        )
                    finally:
                        if target == "cloudrun":
                            _remove_workspace(target_dir)
                if "local" not in req.targets:
                    _remove_workspace(work_root)

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
                _publish(job, None)

        asyncio.create_task(worker())
        return {"deployment_id": deployment_id}

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
