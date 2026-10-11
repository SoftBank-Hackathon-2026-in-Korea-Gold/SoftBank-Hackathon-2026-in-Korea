"""Tracing opt-in, privacy, context and fail-open behavior; no external servers."""

from __future__ import annotations

import atexit
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from app import llmtrace


@pytest.fixture(autouse=True)
def clean_trace_state(monkeypatch):
    for key in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_TRACING_ENABLED",
        "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for name, value in (
        ("_client", None),
        ("_provider", None),
        ("_initialized", False),
        ("_stopped", False),
        ("_shutdown_done", None),
        ("_warned", set()),
    ):
        monkeypatch.setattr(llmtrace, name, value)
    yield
    llmtrace.shutdown(0.5)
    atexit.unregister(llmtrace.shutdown)


def enable(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


class FakeClient:
    def __init__(self, fail=None):
        self.records, self.fail, self.stops = [], fail, 0
        self.current = contextvars.ContextVar("fake_observation", default=None)

    def start_as_current_observation(self, **kwargs):
        if self.fail == "start":
            raise RuntimeError("secret error at start")
        rec = NS(kwargs=kwargs, parent=self.current.get(), updates=[], exits=[])
        self.records.append(rec)
        client = self

        class Manager:
            def __enter__(self):
                self.token = client.current.set(rec)
                return self

            def update(self, **kw):
                if client.fail == "update":
                    raise RuntimeError("secret error at update")
                rec.updates.append(kw)

            def __exit__(self, *args):
                rec.exits.append(args)
                client.current.reset(self.token)
                if client.fail == "end":
                    raise RuntimeError("secret error at end")

        return Manager()

    def shutdown(self):
        self.stops += 1


@pytest.fixture
def client(monkeypatch):
    enable(monkeypatch)
    client = FakeClient()
    monkeypatch.setattr(llmtrace, "_make_client", lambda: client)
    return client


class FakeAnthropic:
    def __init__(self, reply):
        self.messages, self.reply, self.requests = self, reply, []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def test_healer_calls_export_only_metadata_and_usage(client, monkeypatch):
    import anthropic
    import langchain_openai

    from app import healer

    private = "private-prompt-secret"
    anthropic_reply = NS(
        content=[NS(type="text", text="FROM scratch")],
        stop_reason="end_turn",
        usage=NS(input_tokens=7, output_tokens=3),
    )
    fake = FakeAnthropic(anthropic_reply)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake)
    assert healer._call_anthropic(private, private, "claude-test") == "FROM scratch"
    reply = NS(
        content="FROM scratch", usage_metadata={"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}
    )
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kwargs: NS(invoke=lambda _: reply))
    assert healer._call_openai_compatible(private, private, "gpt-test", "openai") == "FROM scratch"
    assert len(client.records) == 2
    assert all(record.kwargs["name"] == "healer.dockerfile_patch" for record in client.records)
    assert [record.kwargs["metadata"]["provider"] for record in client.records] == ["anthropic", "openai"]
    assert client.records[0].updates[0]["usage_details"]["input"] == 7
    assert client.records[1].updates[0]["usage_details"]["total"] == 16
    exported = json.dumps([{"kwargs": r.kwargs, "updates": r.updates} for r in client.records])
    assert private not in exported and "FROM scratch" not in exported


def test_keys_alone_do_not_enable_or_initialize(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(llmtrace, "_make_client", lambda: pytest.fail("must not initialize SDK"))
    fn = lambda: 1
    client = FakeAnthropic(NS())
    assert not llmtrace.enabled()
    assert llmtrace.wrap_anthropic(client) is client
    assert llmtrace.bind(fn) is fn
    with llmtrace.deployment("d") as observation:
        observation.update(output="private")
        assert llmtrace.current_trace_id() is None
        with llmtrace.generation("analyzer.llm", provider="anthropic") as generation:
            generation.record_langchain(NS(content="private"))
    assert llmtrace._client is None


def test_both_keys_and_explicit_opt_in_are_required(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    assert not llmtrace.enabled()
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert llmtrace.enabled()
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    assert not llmtrace.enabled()


def test_metadata_only_even_if_callers_pass_secrets(client, monkeypatch):
    monkeypatch.setenv("CUSTOM_API_KEY", "private-model-key")
    reply = NS(content="private reply", usage=NS(input_tokens=12, output_tokens=3))
    raw = FakeAnthropic(reply)
    with llmtrace.deployment("private-repo-id", app_name="private repo", owner="private user") as root:
        root.update(
            input="source",
            output="stderr",
            status_message="token",
            metadata={
                "success": True,
                "attempts": 2,
                "state": "completed",
                "password": "private password",
                "target": "https://private-url",
                "source": "private source",
                "attempt": -1,
            },
        )
        wrapped = llmtrace.wrap_anthropic(raw, "healer.repo_agent")
        assert (
            wrapped.messages.create(
                model="private-model-key",
                system="private system",
                messages=[{"role": "user", "content": "private prompt"}],
            )
            is reply
        )
    payload = json.dumps([{"kwargs": r.kwargs, "updates": r.updates} for r in client.records])
    assert "private" not in payload
    assert "source" not in payload and "stderr" not in payload and "password" not in payload
    assert raw.requests[0]["system"] == "private system"  # LLM requests remain untouched.
    assert client.records[0].updates == [{"metadata": {"success": True, "attempts": 2, "state": "completed"}}]
    assert client.records[1].updates == [{"usage_details": {"input": 12, "output": 3}}]


def test_worker_context_joins_deployment_and_context_restores(client):
    with llmtrace.deployment("deployment-id"):
        parent_id = llmtrace.current_trace_id()
        with ThreadPoolExecutor(2) as pool:

            def worker():
                with llmtrace.generation("analyzer.llm", provider="anthropic", model="claude-haiku-5-5"):
                    return llmtrace.current_trace_id()

            bound = llmtrace.bind(worker, span_name="analyzer.inspect:malicious")
            assert list(pool.map(lambda _: bound(), range(4))) == [parent_id] * 4
    assert llmtrace.current_trace_id() is None
    root, *children = client.records
    inspectors = [r for r in children if r.kwargs["as_type"] == "span"]
    assert all(r.parent is root for r in inspectors)
    assert root.kwargs["trace_context"]["trace_id"] == llmtrace.trace_id_for("deployment-id")


@pytest.mark.parametrize("failure", ["start", "update", "end"])
def test_sdk_errors_do_not_change_result(client, failure, caplog):
    client.fail = failure
    reply = NS()
    assert llmtrace.wrap_anthropic(FakeAnthropic(reply)).messages.create(model="m") is reply
    assert "secret error" not in caplog.text


def test_original_llm_exception_propagates_without_exporting_message(client):
    error = RuntimeError("postgres://user:secret-password@db private source")
    with pytest.raises(RuntimeError) as caught:
        llmtrace.wrap_anthropic(FakeAnthropic(error)).messages.create(model="m")
    assert caught.value is error
    rec = client.records[0]
    assert rec.updates == [{"level": "ERROR", "status_message": "operation failed"}]
    assert rec.exits == [(None, None, None)]


def test_missing_extra_is_fail_open_and_only_initialized_once(monkeypatch, caplog):
    enable(monkeypatch)
    calls = []

    def missing():
        calls.append(1)
        raise ImportError("secret value")

    monkeypatch.setattr(llmtrace, "_make_client", missing)
    for _ in range(3):
        with llmtrace.span("analyze"):
            pass
    assert len(calls) == 1
    assert "secret value" not in caplog.text
    assert len(caplog.records) == 1


def test_usage_records_only_nonnegative_integer_counts(client):
    with llmtrace.generation("healer.dockerfile_patch", provider="openai", model="gpt-4o") as gen:
        gen.record_langchain(
            NS(content="source", usage_metadata={"input_tokens": 7, "output_tokens": 4, "total_tokens": 11})
        )
        gen.update(usage_details={"input": -1, "output": True, "total": "password", "unknown": 3})
    assert client.records[0].updates == [{"usage_details": {"input": 7, "output": 4, "total": 11}}]


def test_real_sdk_factory_is_private_and_filters_instrumentation(monkeypatch):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry import trace

    enable(monkeypatch)
    captured = {}
    before = trace.get_tracer_provider()

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(langfuse, "Langfuse", factory)
    monkeypatch.setenv("LANGFUSE_RELEASE", "private-release-env")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private.key=secret,service.name=private-project")
    llmtrace._make_client()
    assert trace.get_tracer_provider() is before
    assert dict(captured["tracer_provider"].resource.attributes) == {"service.name": "cloudmorph"}
    assert captured["release"] == "0.1.0" and captured["debug"] is False
    assert captured["timeout"] == 2
    assert captured["should_export_span"](NS(instrumentation_scope=NS(name="langfuse-sdk")))
    assert not captured["should_export_span"](NS(instrumentation_scope=NS(name="opentelemetry.http")))
    assert not captured["should_export_span"](NS(instrumentation_scope=None))
    assert captured["mask"](data="private source") == {}
    captured["tracer_provider"].shutdown()
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://user:private-password@langfuse.example")
    with pytest.raises(ValueError, match="invalid tracing endpoint"):
        llmtrace._make_client()


def test_shutdown_is_bounded_idempotent_and_disables_new_spans(client):
    gate = threading.Event()
    entered = threading.Event()

    def slow():
        client.stops += 1
        entered.set()
        gate.wait(2)

    client.shutdown = slow
    with llmtrace.span("analyze"):
        pass
    started = time.monotonic()
    try:
        assert not llmtrace.shutdown(0.02)
        assert entered.is_set()
        assert time.monotonic() - started < 0.5
        with llmtrace.span("heal"):
            pass
        assert len(client.records) == 1
        assert not llmtrace.shutdown(0.01)
        assert client.stops == 1
    finally:
        gate.set()
        assert llmtrace.shutdown(1)


def test_real_sdk_api_and_trace_tree_without_network(monkeypatch):
    langfuse = pytest.importorskip("langfuse")
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    enable(monkeypatch)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "cloudmorph-test"}), shutdown_on_exit=False)
    sdk = langfuse.Langfuse(
        public_key="pk-lf-cloudmorph-sdk-test",
        secret_key="sk-lf-cloudmorph-sdk-test",
        tracer_provider=provider,
        span_exporter=exporter,
        mask=llmtrace._mask,
        environment="test",
        release="test",
    )
    monkeypatch.setattr(llmtrace, "_make_client", lambda: sdk)
    monkeypatch.setattr(llmtrace, "_provider", provider)
    with llmtrace.deployment("sdk-deployment"), ThreadPoolExecutor(1) as pool:

        def worker():
            with llmtrace.generation("analyzer.llm", provider="anthropic", model="claude-haiku-5-5"):
                raise RuntimeError("must not export this private error")

        with pytest.raises(RuntimeError):
            pool.submit(llmtrace.bind(worker, span_name="analyzer.inspect:malicious")).result()
    sdk.flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    by_name = {s.name: s for s in spans}
    root, inspector, generation = (
        by_name[n] for n in ("cloudmorph.deploy", "analyzer.inspect:malicious", "analyzer.llm")
    )
    assert inspector.parent.span_id == root.context.span_id
    assert generation.parent.span_id == inspector.context.span_id
    assert {s.context.trace_id for s in spans} == {int(llmtrace.trace_id_for("sdk-deployment"), 16)}
    assert not any(s.events for s in spans)
    assert "private" not in json.dumps([dict(s.attributes) for s in spans])
    assert llmtrace.shutdown(2)
