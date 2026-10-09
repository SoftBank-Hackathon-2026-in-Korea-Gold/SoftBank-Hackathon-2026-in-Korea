import pytest

from app.healer import _llm_context, classify_error, heal
from app.schemas import MAX_RETRIES, DeployResult, ErrorCategory

BROKEN_DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
COPY . .
EXPOSE 5000
CMD ["gunicorn", "-b", "127.0.0.1:5000", "app:app"]
"""

MISSING_FLASK = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'flask'"
PORT_ERROR = (
    "The user-provided container failed to start and listen on the port defined "
    "provided by the PORT=8080 environment variable."
)


def _failed(stderr: str) -> DeployResult:
    return DeployResult(success=False, target="cloudrun", exit_code=1, stderr=stderr)


def test_classify_error_detects_demo_categories():
    assert classify_error(MISSING_FLASK) == ErrorCategory.MISSING_DEPENDENCY
    assert classify_error(PORT_ERROR) == ErrorCategory.PORT_BINDING
    assert (
        classify_error("Error: listen EADDRINUSE: address already in use :::3000")
        == ErrorCategory.PORT_BINDING
    )
    assert classify_error("something odd happened") == ErrorCategory.UNKNOWN


def test_heals_missing_package_then_port_binding():
    """Demo scenario: missing package -> patch -> port error -> patch -> live URL."""
    responses = iter(
        [
            _failed(PORT_ERROR),
            DeployResult(success=True, target="cloudrun", exit_code=0, url="https://demo-xyz.a.run.app"),
        ]
    )
    calls: list[str] = []

    def fake_deploy(src_dir, dockerfile, target):
        calls.append(dockerfile)
        return next(responses)

    report = heal("/tmp/app", "cloudrun", _failed(MISSING_FLASK), BROKEN_DOCKERFILE, fake_deploy)

    assert report.success
    assert report.attempts == 2
    assert [r.category for r in report.records] == [
        ErrorCategory.MISSING_DEPENDENCY,
        ErrorCategory.PORT_BINDING,
    ]
    assert all(r.source == "rule" for r in report.records)
    assert "RUN pip install --no-cache-dir flask" in report.final_dockerfile
    assert "0.0.0.0:8080" in report.final_dockerfile and "ENV PORT=8080" in report.final_dockerfile
    assert report.final_url == "https://demo-xyz.a.run.app"
    assert len(calls) == 2


def test_gives_up_after_max_retries():
    counter = {"n": 0}

    def always_fail(src_dir, dockerfile, target):
        return _failed("mysterious failure")

    def fake_llm(dockerfile, error_log, category):
        counter["n"] += 1
        return dockerfile + f"# llm attempt {counter['n']}\n"

    report = heal(
        "/tmp/app", "local", _failed("mysterious failure"), BROKEN_DOCKERFILE, always_fail, fake_llm
    )

    assert not report.success
    assert report.attempts == MAX_RETRIES
    assert counter["n"] == MAX_RETRIES
    assert all(r.source == "llm" for r in report.records)


def test_gives_up_immediately_when_no_patch_available():
    def never_called(src_dir, dockerfile, target):
        raise AssertionError("deploy must not run without a patch")

    report = heal(
        "/tmp/app", "local", _failed("mysterious failure"), BROKEN_DOCKERFILE, never_called, lambda *_: None
    )

    assert not report.success
    assert report.records == []
    assert report.attempts == 0


PREFLIGHT_ERROR = (
    "[deployer] stage=preflight kind=user_action_required target=cloudrun\n"
    "--- error ---\n"
    "- no active gcloud account (gcloud auth login)"
)


def test_gives_up_without_redeploy_when_deployer_needs_human_action():
    def never_called(*_):
        raise AssertionError("must not redeploy or call the LLM for a human-only failure")

    report = heal(
        "/tmp/app", "cloudrun", _failed(PREFLIGHT_ERROR), BROKEN_DOCKERFILE, never_called, never_called
    )

    assert not report.success
    assert report.attempts == 0 and report.records == []
    assert "kind=user_action_required" in report.summary and "stage=preflight" in report.summary


def test_stops_mid_loop_when_redeploy_hits_unhealable_failure():
    calls: list[str] = []

    def push_fails(src_dir, dockerfile, target):
        calls.append(dockerfile)
        return _failed("[deployer] stage=push kind=push_error target=cloudrun\n--- error ---\ndenied")

    def never_called(*_):
        raise AssertionError("LLM must not run after an unhealable failure")

    report = heal("/tmp/app", "cloudrun", _failed(MISSING_FLASK), BROKEN_DOCKERFILE, push_fails, never_called)

    assert not report.success
    assert len(calls) == 1
    assert [r.category for r in report.records] == [ErrorCategory.MISSING_DEPENDENCY]
    assert "kind=push_error" in report.summary


def test_llm_context_keeps_deployer_header_and_tail():
    header = "[deployer] stage=verify kind=runtime_error target=local"
    diagnosis = "app is listening on PORT=8080 but answered an error (HTTP 500); see runtime logs"
    body = [f"  frame {i}" for i in range(100)]
    log = "\n".join([header, diagnosis, "--- error ---", "status=running", "--- runtime logs ---", *body])

    ctx = _llm_context(log, tail=40)

    assert ctx.startswith(f"{header}\n{diagnosis}\n...")
    assert "  frame 99" in ctx and "  frame 60" in ctx
    assert "  frame 59" not in ctx


def test_llm_context_returns_short_log_unchanged():
    log = "[deployer] stage=build kind=build_error target=local\n--- error ---\nERROR [3/4] RUN pip install"
    assert _llm_context(log) == log


_LLM_ENV = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "LLM_BASE_URL",
    "HEALER_MODEL",
    "HEALER_MODEL_ANTHROPIC",
    "HEALER_MODEL_OPENAI",
    "HEALER_MODEL_LOCAL",
)


@pytest.fixture
def clean_llm_env(monkeypatch):
    for k in _LLM_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _fake_anthropic(monkeypatch, reply, created: dict):
    import anthropic

    class _Msg:
        def __init__(self):
            self.content = [type("B", (), {"type": "text", "text": reply})()]
            self.stop_reason = "end_turn"

    class _Messages:
        def create(self, **kwargs):
            created["create"] = kwargs
            if isinstance(reply, Exception):
                raise reply
            return _Msg()

    class DummyAnthropic:
        def __init__(self, **kwargs):
            created["client"] = kwargs
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", DummyAnthropic)


def _fake_chat_openai(monkeypatch, reply: str, created: dict):
    import langchain_openai

    class DummyChatOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def invoke(self, *args, **kwargs):
            return type("R", (), {"content": reply})()

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", DummyChatOpenAI)


def test_llm_patch_openai_api(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("OPENAI_API_KEY", "sk-test")
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM python:3.12-slim\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM python:3.12-slim\n"
    assert created["model"] == "gpt-4o" and created["api_key"] == "sk-test" and "base_url" not in created


def test_llm_patch_claude_api(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("ANTHROPIC_API_KEY", "test-key")
    created: dict = {}
    _fake_anthropic(clean_llm_env, "```dockerfile\nFROM python:3.12-slim\n```", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM python:3.12-slim\n"
    assert created["create"]["model"] == "claude-haiku-5-5"
    assert created["client"]["timeout"] == 30


def test_llm_patch_local_server_uses_custom_base_url(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("OPENAI_BASE_URL", "http://tailscale-dgx:30000/v1")
    clean_llm_env.setenv("HEALER_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM python:3.12-slim\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM python:3.12-slim\n"
    assert created["base_url"] == "http://tailscale-dgx:30000/v1"
    assert created["api_key"] == "EMPTY"
    assert created["model"] == "Qwen/Qwen2.5-Coder-32B-Instruct"


def test_llm_provider_ollama_has_default_url(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("LLM_PROVIDER", "ollama")
    clean_llm_env.setenv("HEALER_MODEL", "qwen2.5-coder:7b")
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM python:3.12-slim\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN)
    assert created["base_url"] == "http://localhost:11434/v1"


def test_local_server_without_model_name_is_skipped(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) is None


def test_mismatched_healer_model_uses_provider_default(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("ANTHROPIC_API_KEY", "test-key")
    clean_llm_env.setenv("HEALER_MODEL", "gpt-4o")  # left over from an OpenAI setup
    created: dict = {}
    _fake_anthropic(clean_llm_env, "FROM python:3.12-slim\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN)
    assert created["create"]["model"] == "claude-haiku-5-5"


def test_dead_claude_key_falls_through_to_openai(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("ANTHROPIC_API_KEY", "revoked")
    clean_llm_env.setenv("OPENAI_API_KEY", "sk-test")
    _fake_anthropic(clean_llm_env, RuntimeError("401 Unauthorized"), {})
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM python:3.12-slim\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM python:3.12-slim\n"
    assert created["model"] == "gpt-4o"


def test_llm_provider_forces_one(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("LLM_PROVIDER", "openai")
    clean_llm_env.setenv("ANTHROPIC_API_KEY", "test-key")
    clean_llm_env.setenv("OPENAI_API_KEY", "sk-test")
    claude: dict = {}
    _fake_anthropic(clean_llm_env, "FROM claude\n", claude)
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM openai\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM openai\n"
    assert "create" not in claude


def test_no_llm_configured_returns_none(clean_llm_env):
    from app.healer import default_llm_patch

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) is None
