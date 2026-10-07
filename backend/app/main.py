"""FastAPI orchestrator (owner: 백락원).

POST /deploy                 -> start pipeline, returns deployment_id
GET  /deploy/{id}/events     -> SSE stream of PipelineEvent (see docs/interfaces.md)

Pipeline: analyze -> deploy -> (fail) heal (redeploys internally, <= MAX_RETRIES) -> done
"""

from __future__ import annotations

import asyncio
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from app import analyzer, deployer, healer
from app.schemas import DeployRequest, DeployTarget, PipelineEvent

load_dotenv()

_queues: dict[str, asyncio.Queue[PipelineEvent | None]] = {}


def run_pipeline(source: str, target: DeployTarget, emit) -> None:
    """Blocking pipeline for one target. Runs in a worker thread.

    TODO(백락원): source fetch (git clone / zip extract), multi-target fan-out, error handling.
    """
    emit(PipelineEvent(type="stage", stage="analyze"))
    analysis = analyzer.analyze(source)

    emit(PipelineEvent(type="stage", stage="deploy", payload={"target": target}))
    result = deployer.deploy(source, analysis.dockerfile, target)
    if result.success:
        emit(PipelineEvent(type="done", payload={"target": target, "url": result.url}))
        return

    emit(PipelineEvent(type="stage", stage="heal", payload={"stderr": result.stderr[-2000:]}))
    report = healer.heal(source, target, result, analysis.dockerfile, deployer.deploy, emit=emit)
    final_type = "done" if report.success else "error"
    emit(PipelineEvent(type=final_type, payload=report.model_dump(mode="json")))


def create_app() -> FastAPI:
    app = FastAPI(title="CloudMorph", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/deploy")
    async def start_deploy(req: DeployRequest) -> dict[str, str]:
        deployment_id = uuid.uuid4().hex[:12]
        queue: asyncio.Queue[PipelineEvent | None] = asyncio.Queue()
        _queues[deployment_id] = queue
        loop = asyncio.get_running_loop()

        def emit(event: PipelineEvent | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def worker() -> None:
            try:
                for target in req.targets:
                    await asyncio.to_thread(run_pipeline, req.source, target, emit)
            except Exception as exc:  # noqa: BLE001 — surface failures to the UI instead of dying silently
                emit(PipelineEvent(type="error", payload={"message": str(exc)}))
            finally:
                emit(None)

        asyncio.create_task(worker())
        return {"deployment_id": deployment_id}

    @app.get("/deploy/{deployment_id}/events")
    async def events(deployment_id: str) -> EventSourceResponse:
        queue = _queues.get(deployment_id)
        if queue is None:
            raise HTTPException(status_code=404, detail="unknown deployment_id")

        async def stream():
            while (event := await queue.get()) is not None:
                yield {"event": event.type, "data": event.model_dump_json()}

        return EventSourceResponse(stream())

    return app


app = create_app()
