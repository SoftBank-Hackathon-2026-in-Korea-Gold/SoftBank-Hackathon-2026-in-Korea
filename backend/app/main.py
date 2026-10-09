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
import ipaddress
import os
import shutil
import subprocess
import tempfile
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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app import analyzer, deployer, healer
from app.schemas import AnalysisResult, DeployRequest, DeployTarget, PipelineEvent

load_dotenv()

# Single-process, in-memory demo job registry. Run uvicorn with --workers 1.
MAX_JOBS = 30
MAX_EVENTS = 1000
MAX_SOURCE_BYTES = 200 * 1024 * 1024


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
        total = 0
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
            if total > MAX_SOURCE_BYTES:
                raise ValueError("ZIP contents exceed size limit")
        archive.extractall(destination)
    top = list(destination.iterdir())
    return top[0] if len(top) == 1 and top[0].is_dir() else destination


@contextmanager
def prepare_source(source: str) -> Iterator[str]:
    """Prepare a local directory, local ZIP, or allowlisted public Git URL.

    Source is treated as trusted input: this is a hackathon demo API and must
    not be exposed publicly without authentication and isolation.
    """
    path = Path(source).expanduser()
    if path.is_dir():
        yield str(path.resolve())
        return
    with tempfile.TemporaryDirectory(prefix="cloudmorph-source-") as temp:
        root = Path(temp)
        if path.is_file() and path.suffix.lower() == ".zip":
            yield _prepare_zip(path, root)
            return
        if source.startswith("https://"):
            url = _validate_https_git_url(source)
            dest = root / "project"
            try:
                subprocess.run(
                    ["git", "-c", "protocol.file.allow=never", "clone", "--depth", "1", "--", url, str(dest)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                raise ValueError(f"Git clone failed: {type(exc).__name__}") from exc
            yield str(dest)
            return
        raise ValueError("Source must be an existing directory, local ZIP, or allowed HTTPS Git URL")


def _prepare_zip(path: Path, root: Path) -> str:
    dest = root / "source"
    dest.mkdir(parents=True)
    return str(_extract_zip_safe(path, dest))


def _copy_for_target(source: str, destination: Path) -> str:
    """Deployer writes Dockerfile and .deployer state; isolate each target."""
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", ".deployer", "__pycache__"),
    )
    return str(destination)


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
        CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"]
    )

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
                # Keep workspaces while deployments are running; they can be
                # removed by the operator after the demo.
                work_root = Path(tempfile.mkdtemp(prefix="cloudmorph-deploy-"))
                for target in req.targets:
                    try:
                        target_dir = _copy_for_target(source_dir, work_root / target)
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
