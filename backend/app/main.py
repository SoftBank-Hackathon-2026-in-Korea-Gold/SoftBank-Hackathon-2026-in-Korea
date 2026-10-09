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
import json
import os
import re
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
from sse_starlette.sse import EventSourceResponse

from app import analyzer, deployer, healer, webhooks
from app import fleet as fleetmod
from app import nodes as nodepool
from app.schemas import AnalysisResult, DeployRequest, DeployTarget, PipelineEvent

load_dotenv()

# Single-process, in-memory demo job registry. Run uvicorn with --workers 1.
MAX_JOBS = 30
MAX_EVENTS = 1000
MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_SOURCE_FILES = 10_000  # Includes directories to bound traversal as well.
try:
    MAX_CONCURRENT_GIT_CLONES = int(os.getenv("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES", "2"))
except ValueError as exc:
    raise ValueError("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES must be a positive integer") from exc
if MAX_CONCURRENT_GIT_CLONES < 1:
    raise ValueError("CLOUDMORPH_MAX_CONCURRENT_GIT_CLONES must be a positive integer")
GIT_CLONE_WAIT_SECONDS = 5
_git_clone_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_GIT_CLONES)
SOURCE_IGNORE = {".git", ".venv", "node_modules", ".deployer", "__pycache__"}


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
        if len(members) > MAX_SOURCE_FILES:
            raise ValueError("ZIP contents exceed file count limit")
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


def _inspect_source(source: str | Path, destination: Path | None = None) -> None:
    """Validate or copy a bounded tree without following links, including during races.

    Directory descriptors anchor traversal; O_NOFOLLOW also rejects entries
    replaced by symlinks after inspection. Both modes skip SOURCE_IGNORE entries.
    """
    total = count = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(directory_fd: int, output: Path | None) -> None:
        nonlocal total, count
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if entry.name in SOURCE_IGNORE:
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
                        visit(child_fd, child_output)
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
        visit(root_fd, destination)
    finally:
        os.close(root_fd)


@contextmanager
def prepare_source(source: str, ref: str | None = None) -> Iterator[str]:
    """Prepare a local directory, local ZIP, or allowlisted public Git URL.

    Source is treated as trusted input: this is a hackathon demo API and must
    not be exposed publicly without authentication and isolation.
    """
    path = Path(source).expanduser()
    if not path.is_absolute() and not path.exists() and (REPO_ROOT / source).exists():
        path = REPO_ROOT / source  # dashboard presets like "sample-apps/guestbook" are relative to the repo
    if path.is_dir():
        _inspect_source(path)
        yield str(path.resolve())
        return
    with tempfile.TemporaryDirectory(prefix="cloudmorph-source-") as temp:
        root = Path(temp)
        if path.is_file() and path.suffix.lower() == ".zip":
            extracted = _prepare_zip(path, root)
            _inspect_source(extracted)
            yield extracted
            return
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
            _inspect_source(dest)
            yield str(dest)
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
                # Separate target workspaces because deployer writes Dockerfile and may leave background
                # local container/tunnel artifacts. The folder name is the app/service name the deployer uses,
                # so a stable `name` means "update the same service" (CD) instead of "create another one".
                work_root = Path(tempfile.mkdtemp(prefix="cloudmorph-deploy-"))
                for target in targets:
                    try:
                        target_dir = _copy_for_target(source_dir, work_root / f"{app_name}-{target}")
                        run_pipeline(target_dir, target, emit, analysis=analysis)
                    except Exception as exc:  # noqa: BLE001 — isolate target failures
                        emit(
                            PipelineEvent(
                                type="error",
                                payload={"target": target, "message": f"{type(exc).__name__}: {exc}"},
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
        return {"deployment_id": app.state.launch(req.source, req.targets, name=req.name, ref=req.ref)}

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
