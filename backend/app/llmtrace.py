"""Opt-in, metadata-only Langfuse tracing using the supported Python SDK.

Install with ``uv sync --extra tracing`` and explicitly enable
LANGFUSE_TRACING_ENABLED with both Langfuse keys. Without that opt-in this module
does not import the SDK, start threads, or make network requests. Prompts, model
responses, repository names/paths, stderr and environment values are never sent.
Only fixed operation names, hashed deployment IDs, token counts and an allowlist
of operational metadata are exported. Tracing failures never change app behavior.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import logging
import os
import re
import threading
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_client = None
_provider = None
_initialized = False
_stopped = False
_lock = threading.Lock()
_shutdown_done: threading.Event | None = None
_warned: set[str] = set()
_trace_id = contextvars.ContextVar("cloudmorph_trace_id", default=None)

_NAMES = {
    "cloudmorph.deploy",
    "analyze",
    "deploy",
    "heal",
    "cleanup",
    "analyzer.llm",
    "analyzer.dockerfile_writer",
    "analyzer.dockerfile_judge",
    "healer.dockerfile_patch",
    "healer.repo_agent",
}
_ENUMS = {
    "target": {"local", "cloudrun", "aws", "node", "function", "agent"},
    "provider": {"anthropic", "openai", "local", "vllm", "sglang", "ollama", "llamacpp", "jev"},
    "trigger": {"api", "webhook", "redeploy"},
    "state": {"pending", "analyzing", "deploying", "healing", "completed", "failed", "blocked", "cancelled"},
}
_COUNTS = {"attempts", "attempt", "max_tokens", "files_changed"}
_USAGE = {"input", "output", "total", "cache_read_input_tokens", "cache_creation_input_tokens"}


def enabled() -> bool:
    """Keys alone do not enable tracing; an explicit true/1/yes/on is required."""
    return (
        not _stopped
        and os.environ.get("LANGFUSE_TRACING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        and bool(os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip())
        and bool(os.environ.get("LANGFUSE_SECRET_KEY", "").strip())
    )


def _warning(kind: str) -> None:
    # Never log SDK exceptions: they can include URLs, credentials or payloads.
    if kind not in _warned:
        _warned.add(kind)
        logger.warning("Langfuse %s failed; tracing ignored", kind)


def _metadata(data) -> dict:
    if not isinstance(data, dict):
        return {}
    clean = {}
    for key, value in data.items():
        if (
            key in _ENUMS
            and isinstance(value, str)
            and value in _ENUMS[key]
            or key in _COUNTS
            and type(value) is int
            and 0 <= value <= 1_000_000
            or key == "success"
            and type(value) is bool
        ):
            clean[key] = value
    return clean


def _mask(*, data, **_) -> dict:
    # Defense in depth for SDK input/output/metadata attributes. No content opt-in.
    return _metadata(data)


def _make_client():
    global _provider
    from langfuse import Langfuse
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    # Keep third-party HTTP/LLM instrumentation (which may contain source or auth)
    # away from this exporter. Never install a global OTel provider.
    base_url = os.environ.get("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid tracing endpoint")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid tracing endpoint")
    provider = TracerProvider(resource=Resource({"service.name": "cloudmorph"}), shutdown_on_exit=False)
    client = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"].strip(),
        secret_key=os.environ["LANGFUSE_SECRET_KEY"].strip(),
        base_url=base_url,
        timeout=2,
        tracing_enabled=True,
        environment="cloudmorph",
        release="0.1.0",
        debug=False,
        flush_at=64,
        flush_interval=1,
        tracer_provider=provider,
        should_export_span=lambda s: (
            bool(s.instrumentation_scope) and s.instrumentation_scope.name == "langfuse-sdk"
        ),
        mask=_mask,
    )
    _provider = provider
    return client


def _get_client():
    global _client, _initialized
    if not enabled():
        return None
    with _lock:
        if not _initialized:
            _initialized = True
            try:
                _client = _make_client()
                # Runs before the SDK atexit hook (LIFO). SDK shutdown unregisters
                # its own hook before flushing; our caller waits at most 2 seconds.
                atexit.register(shutdown)
            except Exception:  # noqa: BLE001 — optional telemetry must fail open
                _warning("initialization (install the tracing extra)")
        return _client


def trace_id_for(deployment_id: str) -> str:
    return hashlib.sha256(f"cloudmorph:{deployment_id}".encode()).hexdigest()[:32]


def current_trace_id() -> str | None:
    return _trace_id.get()


def _name(name: str) -> str:
    if name in _NAMES:
        return name
    # Inspector names come from analyzer.INSPECTORS, never repository content.
    if re.fullmatch(r"analyzer\.inspect:[a-z_]{1,40}", name):
        return name
    return "operation"


def _model(model) -> str | None:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", model):
        return None
    # Model identifiers are configuration, but a mistakenly supplied secret must
    # still be omitted (including custom, nonstandard API keys).
    if model.startswith(("sk-", "pk-", "ghp_", "github_pat_", "AKIA")):
        return None
    for key, value in os.environ.items():
        if (
            re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", key, re.IGNORECASE)
            and value
            and value in model
        ):
            return None
    return model


class Observation:
    """A safe facade: arbitrary input/output/status messages are discarded."""

    def __init__(self, observation=None):
        self._observation = observation

    def update(self, *, metadata=None, usage_details=None, **_):
        if self._observation is None:
            return
        values = {}
        if clean := _metadata(metadata):
            values["metadata"] = clean
        if isinstance(usage_details, dict):
            usage = {
                k: v
                for k, v in usage_details.items()
                if k in _USAGE and type(v) is int and 0 <= v <= 1_000_000_000
            }
            if usage:
                values["usage_details"] = usage
        try:
            if values:
                self._observation.update(**values)
        except Exception:  # noqa: BLE001 — optional telemetry must fail open
            _warning("update")

    def record_langchain(self, reply):
        try:
            usage = getattr(reply, "usage_metadata", None) or {}
            self.update(
                usage_details={
                    "input": usage.get("input_tokens"),
                    "output": usage.get("output_tokens"),
                    "total": usage.get("total_tokens"),
                }
            )
        except Exception:  # noqa: BLE001 — optional telemetry must fail open
            _warning("usage")

    def _failed(self):
        try:
            if self._observation is not None:
                self._observation.update(level="ERROR", status_message="operation failed")
        except Exception:  # noqa: BLE001 — optional telemetry must fail open
            _warning("update")


@contextmanager
def _observe(name, kind="span", *, trace_context=None, model=None, metadata=None):
    manager, observation = None, Observation()
    client = _get_client()
    if client is not None:
        try:
            kwargs = {"name": _name(name), "as_type": kind, "metadata": _metadata(metadata)}
            if trace_context:
                kwargs["trace_context"] = trace_context
            if kind == "generation" and (safe_model := _model(model)):
                kwargs["model"] = safe_model
            manager = client.start_as_current_observation(**kwargs)
            observation = Observation(manager.__enter__())
        except Exception:  # noqa: BLE001 — optional telemetry must fail open
            manager = None
            _warning("start")
    try:
        yield observation
    except BaseException:
        observation._failed()
        raise
    finally:
        if manager is not None:
            try:
                # Do not pass the real exception to OTel: automatic exception
                # events include error text/stack traces and can leak credentials.
                manager.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 — optional telemetry must fail open
                _warning("end")


@contextmanager
def deployment(deployment_id: str, **metadata):
    tid = trace_id_for(deployment_id) if enabled() else None
    token = _trace_id.set(tid) if tid else None
    try:
        with _observe(
            "cloudmorph.deploy", trace_context={"trace_id": tid} if tid else None, metadata=metadata
        ) as observation:
            yield observation
    finally:
        if token is not None:
            _trace_id.reset(token)


def span(name: str, **metadata):
    return _observe(name, metadata=metadata)


def generation(name: str, *, provider: str, model=None):
    return _observe(name, "generation", model=model, metadata={"provider": provider})


def bind(fn, *, span_name=None):
    """Carry the deployment/OTel context into analyzer's worker threads."""
    if not enabled():
        return fn
    context = contextvars.copy_context()

    @wraps(fn)
    def bound(*args, **kwargs):
        def run():
            if span_name:
                with span(span_name):
                    return fn(*args, **kwargs)
            return fn(*args, **kwargs)

        return context.copy().run(run)

    return bound


class _AnthropicMessages:
    def __init__(self, messages, name):
        self._messages, self._name = messages, name

    def __getattr__(self, name):
        return getattr(self._messages, name)

    def create(self, **kwargs):
        with generation(self._name, provider="anthropic", model=kwargs.get("model")) as observation:
            reply = self._messages.create(**kwargs)
            try:
                usage = getattr(reply, "usage", None)
                observation.update(
                    usage_details={
                        "input": getattr(usage, "input_tokens", None),
                        "output": getattr(usage, "output_tokens", None),
                        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
                    }
                )
            except Exception:  # noqa: BLE001 — optional telemetry must fail open
                _warning("usage")
            return reply


class _AnthropicClient:
    def __init__(self, client, name):
        self._client = client
        self.messages = _AnthropicMessages(client.messages, name)

    def __getattr__(self, name):
        return getattr(self._client, name)


def wrap_anthropic(client, name="analyzer.llm"):
    return _AnthropicClient(client, name) if enabled() else client


def shutdown(timeout=2.0) -> bool:
    """Drain the SDK once in a daemon thread; wait no longer than timeout seconds."""
    global _stopped, _shutdown_done
    with _lock:
        _stopped = True
        if _client is None:
            return True
        if _shutdown_done is None:
            _shutdown_done = threading.Event()
            client, done = _client, _shutdown_done

            def stop():
                try:
                    client.shutdown()
                    if _provider is not None:
                        _provider.shutdown()
                except Exception:  # noqa: BLE001 — optional telemetry must fail open
                    _warning("shutdown")
                finally:
                    done.set()

            threading.Thread(target=stop, name="cloudmorph-langfuse-shutdown", daemon=True).start()
        done = _shutdown_done
    return done.wait(max(0.0, min(float(timeout), 5.0)))
