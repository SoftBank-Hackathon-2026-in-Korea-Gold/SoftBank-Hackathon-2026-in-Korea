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


def test_app_code_failure_bare_exception_and_custom_name():
    from app.healer import app_code_failure

    bare_err_log = (
        "[deployer] stage=verify kind=runtime_error target=local\n"
        "Traceback (most recent call last):\n"
        '  File "/app/main.py", line 42, in run\n'
        "    raise RuntimeError\n"
        "RuntimeError\n"
    )
    res = app_code_failure(bare_err_log)
    assert res is not None
    assert res.exception == "RuntimeError"
    assert res.line == 42
    assert res.path == "/app/main.py"

    custom_err_log = (
        "[deployer] stage=verify kind=runtime_error target=local\n"
        "Traceback (most recent call last):\n"
        '  File "/app/server.py", line 15, in <module>\n'
        '    raise CustomFatalCrash("fatal")\n'
        "CustomFatalCrash: fatal\n"
    )
    res_custom = app_code_failure(custom_err_log)
    assert res_custom is not None
    assert res_custom.exception == "CustomFatalCrash: fatal"
    assert res_custom.line == 15
    assert res_custom.path == "/app/server.py"


def test_forced_local_provider_without_base_url_never_hits_openai(clean_llm_env):
    from app.healer import default_llm_patch

    clean_llm_env.setenv("LLM_PROVIDER", "vllm")
    clean_llm_env.setenv("OPENAI_API_KEY", "sk-secret-real-key")
    # No OPENAI_BASE_URL set!
    created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM leak\n", created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) is None
    assert not created  # Must never call OpenAI


def test_openai_falls_through_to_local_when_both_configured(clean_llm_env):
    import langchain_openai

    from app.healer import default_llm_patch

    clean_llm_env.setenv("OPENAI_API_KEY", "sk-test")
    clean_llm_env.setenv("OPENAI_BASE_URL", "http://local:8000/v1")
    clean_llm_env.setenv("HEALER_MODEL", "qwen")
    clean_llm_env.setenv("HEALER_MODEL_OPENAI", "gpt-4o")

    calls = []

    class MockChatOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.base_url = kwargs.get("base_url")

        def invoke(self, *args, **kwargs):
            if not self.base_url:
                raise RuntimeError("OpenAI rate limit 429")
            return type("R", (), {"content": "FROM local\n"})()

    clean_llm_env.setattr(langchain_openai, "ChatOpenAI", MockChatOpenAI)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) == "FROM local\n"
    assert len(calls) == 2
    assert "base_url" not in calls[0]
    assert calls[1]["base_url"] == "http://local:8000/v1"


def test_deadline_skips_further_providers_on_timeout(clean_llm_env, monkeypatch):
    import time

    from app.healer import default_llm_patch

    clean_llm_env.setenv("ANTHROPIC_API_KEY", "test-key")
    clean_llm_env.setenv("OPENAI_API_KEY", "sk-test")

    start_time = 1000.0
    times = [start_time, start_time + 35.0, start_time + 35.0]

    def mock_monotonic():
        return times.pop(0) if times else start_time + 50.0

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    _fake_anthropic(clean_llm_env, RuntimeError("timeout"), {})
    openai_created: dict = {}
    _fake_chat_openai(clean_llm_env, "FROM openai\n", openai_created)

    assert default_llm_patch("FROM scratch", "err", ErrorCategory.UNKNOWN) is None
    assert not openai_created  # OpenAI was skipped because deadline was exceeded


# --------------------------------------------------------------------------- #
# issue #7: rebind edge cases, classification boundaries, repeated rule patches
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            'CMD ["sh", "-c", "python prestart.py && exec gunicorn -b :5000 app:app"]',
            'CMD ["sh", "-c", "python prestart.py && exec gunicorn -b 0.0.0.0:8080 app:app"]',
        ),
        ("CMD gunicorn --bind :5000 app:app", "CMD gunicorn --bind 0.0.0.0:8080 app:app"),
        ("CMD gunicorn --bind=:5000 app:app", "CMD gunicorn --bind=0.0.0.0:8080 app:app"),
        ("CMD gunicorn -b=:5000 app:app", "CMD gunicorn -b=0.0.0.0:8080 app:app"),
        ('CMD ["gunicorn", "-b", ":5000", "app:app"]', 'CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]'),
        # existing behaviour stays
        (
            'CMD ["gunicorn", "-b", "127.0.0.1:5000", "app:app"]',
            'CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]',
        ),
        (
            "CMD uvicorn main:app --host 127.0.0.1 --port 5000",
            "CMD uvicorn main:app --host 0.0.0.0 --port 8080",
        ),
    ],
)
def test_rebind_start_command_handles_ip_omitted_binds(before, after):
    from app.healer import _rebind_start_command

    assert _rebind_start_command(before) == after


@pytest.mark.parametrize(
    "line",
    [
        'CMD ["sh", "-c", "wait-for db:5432 && exec gunicorn --bind 0.0.0.0:8080 app:app"]',
        'CMD ["sh", "-c", "./wait-for :5432 && exec python app.py"]',  # no -b/--bind: leave it
        "CMD myserver --web-bind :9000",  # -b inside another flag name
    ],
)
def test_rebind_leaves_other_host_ports_alone(line):
    from app.healer import _rebind_start_command

    assert _rebind_start_command(line) == line


def test_connection_refused_alone_is_not_a_port_binding_error():
    assert classify_error("waiting for database at localhost:5432: [Errno 111] Connection refused") == (
        ErrorCategory.UNKNOWN
    )
    assert classify_error("ImportError: libGL.so.1: cannot open shared object file") == (
        ErrorCategory.MISSING_DEPENDENCY
    )


def test_same_rule_patch_twice_stops_before_max_retries():
    """The port rule already applied, the redeploy fails the same way: one redeploy, then stop."""
    calls: list[str] = []

    def still_broken(src_dir, dockerfile, target):
        calls.append(dockerfile)
        return _failed(PORT_ERROR)

    report = heal(
        "/tmp/app", "cloudrun", _failed(PORT_ERROR), BROKEN_DOCKERFILE, still_broken, lambda *_: None
    )

    assert not report.success and len(calls) == 1 and report.attempts == 1
    assert report.attempts < MAX_RETRIES


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../../etc", "uploads/../etc", "uploads;id", "$(id)", "uploads`id`", "-x"]
)
def test_permission_does_not_interpolate_or_escape_workdir(path):
    from app.healer import _patch_permission

    stderr = f"[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n    os.makedirs(path)\nPermissionError: [Errno 13] Permission denied: '{path}'\n"
    dockerfile = "FROM python:3.12-slim\nWORKDIR /app\nUSER app\nCMD app\n"
    if path == "-x":  # Absolute target and -- separators make a leading dash safe.
        assert " /app/-x" in _patch_permission(dockerfile, stderr)[0]
    else:
        assert _patch_permission(dockerfile, stderr) is None


@pytest.mark.parametrize(
    "middle",
    [
        "USER app;id",
        "USER $APP_USER",
        "USER root",
        "USER 0",
        "USER first\nUSER second",
        "USER app\nWORKDIR /app",
    ],
)
def test_permission_requires_safe_literal_owner_and_root_insertion(middle):
    from app.healer import _patch_permission

    stderr = "[deployer] stage=verify kind=runtime_error target=local\nPermissionError: [Errno 13] Permission denied: 'uploads'"
    assert _patch_permission(f"FROM python:3.12-slim\nWORKDIR /app\n{middle}\nCMD app\n", stderr) is None


@pytest.mark.parametrize(
    "base", ["scratch", "alpine:3", "ubuntu:24.04", "custom-debian", "$BASE", "python:3.12-alpine", "build"]
)
def test_native_rule_rejects_unproven_bases_and_inherited_user(base):
    from app.healer import _patch_shared_library

    dockerfile = f"FROM python:3.12-slim AS build\nUSER app\nFROM {base}\nCMD app\n"
    assert _patch_shared_library(dockerfile, "libgomp.so.1: cannot open shared object file") is None


def test_native_rule_uses_final_runtime_stage_not_build_failure():
    from app.healer import _patch_shared_library

    dockerfile = "FROM python:3.12-slim AS build\nUSER app\nFROM python:3.12-slim\nUSER app\nCMD app\n"
    patched, _ = _patch_shared_library(dockerfile, "libgomp.so.1: cannot open shared object file")
    assert patched.index("libgomp1") > patched.index("FROM python:3.12-slim\n")
    assert patched.index("libgomp1") < patched.rindex("USER app")
    assert (
        _patch_shared_library(
            dockerfile,
            "[deployer] stage=build kind=build_error\nlibgomp.so.1: cannot open shared object file",
        )
        is None
    )


def test_toggle_patch_stops_before_redeploying_initial_image():
    changed = BROKEN_DOCKERFILE + "# changed\n"
    report = heal(
        "/tmp/app",
        "local",
        _failed("mystery"),
        BROKEN_DOCKERFILE,
        lambda *_: _failed("mystery"),
        lambda dockerfile, *_: changed if dockerfile == BROKEN_DOCKERFILE else BROKEN_DOCKERFILE,
    )
    assert report.attempts == 1 and report.final_dockerfile == changed


def test_source_only_missing_environment_stops_before_repo_agent(tmp_path):
    from app.healer import CONFIG_STOP

    (tmp_path / "app.py").write_text("import os\nprofile = os.environ['APP_PROFILE']\n")
    stderr = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nTraceback (most recent call last):\n  File \"/app/app.py\", line 2, in <module>\nKeyError: 'APP_PROFILE'\n"

    def forbidden(*_):
        raise AssertionError("early stop must precede repair, LLM and redeploy")

    report = heal(
        str(tmp_path),
        "local",
        _failed(stderr),
        "FROM python:3.12-slim\nWORKDIR /app\n",
        forbidden,
        forbidden,
        repair_fn=forbidden,
    )
    assert report.attempts == 0 and report.summary.startswith(CONFIG_STOP)


@pytest.mark.parametrize(
    "logs",
    [
        "environment variable API_TOKEN is missing; continuing\nTraceback (most recent call last):\nNameError: broken\n",
        "Connection refused\nconnected to database\nRuntimeError: unrelated\n",
        "Traceback (most recent call last):\n    PROFILES[os.environ.get('APP_ENV')]\nKeyError: 'APP_ENV'\n",
    ],
)
def test_optional_or_dictionary_failures_do_not_trigger_human_stop(logs):
    from app.healer import needs_human_stop

    assert (
        needs_human_stop(
            "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n" + logs
        )
        is None
    )


def test_permission_warning_does_not_mask_final_name_error():
    from app.healer import _patch_permission

    log = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n    os.makedirs(path)\nPermissionError: [Errno 13] Permission denied: 'uploads'\nTraceback (most recent call last):\nNameError: broken\n"
    assert _patch_permission("FROM python:3.12-slim\nWORKDIR /app\nUSER app\n", log) is None


@pytest.mark.parametrize(
    "logs,prefix",
    [
        (
            "KeyError: 'APP_PROFILE'\n    os.environ['APP_PROFILE']",
            "Not auto-healable: missing runtime configuration",
        ),
        ("ConnectionRefusedError: localhost:5432 Connection refused", "Not auto-healable: external service"),
        ("Fatal Python error: Segmentation fault", "Not auto-healable: silent crash"),
    ],
)
def test_cloudrun_revision_logs_stop_despite_port_wrapper(logs, prefix):
    from app.healer import needs_human_stop

    # Put environ evidence before the exception as a real traceback does.
    logs = logs.replace(
        "KeyError: 'APP_PROFILE'\n    os.environ['APP_PROFILE']",
        "    os.environ['APP_PROFILE']\nKeyError: 'APP_PROFILE'",
    )
    summary = needs_human_stop(
        "[deployer] stage=deploy kind=runtime_error target=cloudrun\nfailed to start and listen on the port\n--- cloudrun revision logs ---\n"
        + logs
    )
    assert summary.startswith(prefix)


def test_service_summary_does_not_echo_dsn_credentials():
    from app.healer import needs_human_stop

    log = '[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nConnection refused at server at "postgres://alice:secret@database" (x), port 5432\n'
    summary = needs_human_stop(log)
    assert "secret" not in summary and "alice" not in summary


def test_rebind_keeps_local_database_endpoint_in_shell_chain():
    from app.healer import _rebind_start_command

    line = 'CMD ["sh", "-c", "wait-for localhost:5432 && gunicorn -b :5000 app:app"]'
    patched = _rebind_start_command(line)
    assert "localhost:5432" in patched and "-b 0.0.0.0:8080" in patched


@pytest.mark.parametrize(
    "logs",
    [
        "ConnectionRefusedError: Connection refused\nThe above exception was the direct cause of the following exception:\nRuntimeError: database startup failed\n",
        "    os.environ['APP_PROFILE']\nKeyError: 'APP_PROFILE'\nThe above exception was the direct cause of the following exception:\nRuntimeError: startup failed\n",
    ],
)
def test_chained_startup_cause_stops(logs):
    from app.healer import needs_human_stop

    assert needs_human_stop(
        "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n" + logs
    )


def test_service_summary_redacts_numeric_password_dsn():
    from app.healer import needs_human_stop

    summary = needs_human_stop(
        "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nConnection refused postgres://alice:12345@database:5432/db\n"
    )
    assert "alice" not in summary and "12345" not in summary and "database:5432" in summary


def test_quiet_crash_with_ok_log_still_stops():
    from app.healer import CRASH_STOP, needs_human_stop

    log = "[deployer] stage=verify kind=runtime_error target=local\ncontainer exited during startup\n--- error ---\ncontainer status=exited exit_code=139\n--- runtime logs ---\nOK\n"
    assert needs_human_stop(log).startswith(CRASH_STOP)


def test_optional_warning_does_not_become_chained_exception_cause():
    from app.healer import needs_human_stop

    logs = "environment variable API_TOKEN is missing; optional\nConnection refused; retrying\nValueError: bad input\nThe above exception was the direct cause of the following exception:\nRuntimeError: unrelated crash\n"
    assert (
        needs_human_stop(
            "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n" + logs
        )
        is None
    )


def test_optional_cache_warning_does_not_mask_fatal_database_chain():
    from app.healer import SERVICE_STOP

    log = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\ncache: Connection refused; optional cache disabled\nTraceback (most recent call last):\nConnectionRefusedError: Connection refused to database:5432\nThe above exception was the direct cause of the following exception:\nRuntimeError: database startup failed\n"

    def forbidden(*_):
        raise AssertionError("fatal database startup must stop before agent or redeploy")

    report = heal(
        "/tmp/app", "local", _failed(log), BROKEN_DOCKERFILE, forbidden, forbidden, repair_fn=forbidden
    )
    assert report.attempts == 0 and report.summary.startswith(SERVICE_STOP)
    assert "database:5432" in report.summary


def test_permission_patches_terminal_directory_not_optional_warning():
    from app.healer import _patch_permission

    log = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nPermissionError: [Errno 13] Permission denied: 'cache'\nContinuing without cache\nTraceback (most recent call last):\n    os.makedirs('uploads')\nPermissionError: [Errno 13] Permission denied: 'uploads'\n"
    patched, _ = _patch_permission("FROM python:3.12-slim\nWORKDIR /app\nUSER app\nCMD app\n", log)
    assert "/app/uploads" in patched and "/app/cache" not in patched


def test_permission_file_open_failure_never_creates_a_directory():
    from app.healer import _patch_permission

    log = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nTraceback (most recent call last):\n    open('state.json', 'w')\nPermissionError: [Errno 13] Permission denied: 'state.json'\n"
    assert _patch_permission("FROM python:3.12-slim\nWORKDIR /app\nUSER app\n", log) is None


def test_config_summary_omits_optional_warning_before_actual_missing_key():
    from app.healer import needs_human_stop

    log = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nenvironment variable OPTIONAL_TOKEN is missing; continuing\n    os.environ['SECRET_KEY']\nKeyError: 'SECRET_KEY'\n"
    summary = needs_human_stop(log)
    assert "SECRET_KEY" in summary and "OPTIONAL_TOKEN" not in summary


@pytest.mark.parametrize(
    ("log", "expected_target", "expected_evidence"),
    [
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                'django.db.utils.OperationalError: connection to server at "db.prod.internal" (10.0.0.1), port 5432 failed: '
                'FATAL: password authentication failed for user "app_user"'
            ),
            "db.prod.internal:5432",
            "password authentication failed",
        ),
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                "pymysql.err.OperationalError: (2003, \"Can't connect to MySQL server on 'mysql.internal' ([Errno 111] Connection refused)\")"
            ),
            "mysql.internal:3306",
            "Can't connect to MySQL server",
        ),
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                "pymysql.err.OperationalError: (1045, \"Access denied for user 'app_user'@'localhost' (using password: YES)\")"
            ),
            None,
            "Access denied for user",
        ),
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                'psycopg2.OperationalError: connection to server on host "postgres.infra.internal", port 5432 failed: Operation timed out'
            ),
            "postgres.infra.internal:5432",
            "Operation timed out",
        ),
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                "django.db.utils.OperationalError: could not connect to server: Network is unreachable"
            ),
            None,
            "could not connect to server",
        ),
        (
            (
                "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
                'django.db.utils.OperationalError: connection to server at "db.internal", port 5432 failed: '
                'FATAL: database "sample" does not exist'
            ),
            "db.internal:5432",
            'database "sample" does not exist',
        ),
    ],
)
def test_database_client_error_variants_trigger_service_stop(log, expected_target, expected_evidence):
    from app.healer import SERVICE_STOP, needs_human_stop

    summary = needs_human_stop(log)
    assert summary is not None and summary.startswith(SERVICE_STOP)
    assert expected_evidence in summary
    if expected_target:
        assert expected_target in summary


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            'CMD ["python", "manage.py", "runserver", "127.0.0.1:8000"]',
            'CMD ["python", "manage.py", "runserver", "0.0.0.0:8080"]',
        ),
        ("CMD php -S localhost:8000 -t public", "CMD php -S 0.0.0.0:8080 -t public"),
        (
            "CMD waitress-serve --listen=127.0.0.1:5000 app:app",
            "CMD waitress-serve --listen=0.0.0.0:8080 app:app",
        ),
        (
            'CMD ["sh", "-c", "wait-for localhost:5432 && python manage.py runserver 127.0.0.1:8000"]',
            'CMD ["sh", "-c", "wait-for localhost:5432 && python manage.py runserver 0.0.0.0:8080"]',
        ),
    ],
)
def test_rebind_rewrites_localhost_endpoint_without_bind_flag(line, expected):
    from app.healer import _rebind_start_command

    assert _rebind_start_command(line) == expected


LOCAL_LISTENING_ON_LOOPBACK = (
    "[deployer] stage=verify kind=runtime_error target=local\n"
    "container started but failed to start and listen on the port defined by PORT=8080 "
    "(timeout after 15s (last probe: URLError)); the app is probably bound to another port or to 127.0.0.1\n"
    "--- runtime logs ---\n"
    "[INFO] Starting gunicorn 23.0.0\n"
    "[INFO] Listening at: http://127.0.0.1:5000 (1)\n"
)


@pytest.mark.parametrize(
    "warning",
    [
        "Warning: environment variable SENTRY_DSN not set, error reporting disabled",
        "WARNING:root:cache: Error 111 connecting to redis:6379. Connection refused. caching disabled",
    ],
)
def test_local_loopback_bind_with_harmless_warning_is_repaired_not_stopped(warning):
    log = LOCAL_LISTENING_ON_LOOPBACK + warning + "\n"
    responses = iter([DeployResult(success=True, target="local", exit_code=0, url="http://127.0.0.1:49152")])

    def fake_deploy(src_dir, dockerfile, target):
        return next(responses)

    def forbidden(*_):
        raise AssertionError("the port rule must fix this without the agent or LLM")

    report = heal(
        "/tmp/app",
        "local",
        DeployResult(success=False, target="local", exit_code=1, stderr=log),
        BROKEN_DOCKERFILE,
        fake_deploy,
        forbidden,
        repair_fn=forbidden,
    )
    assert report.success and report.attempts == 1
    assert report.records[0].category == ErrorCategory.PORT_BINDING
    assert "0.0.0.0:8080" in report.final_dockerfile


def test_cloudrun_port_wrapper_with_database_auth_failure_still_stops():
    from app.healer import SERVICE_STOP, needs_human_stop

    log = (
        "[deployer] stage=deploy kind=runtime_error target=cloudrun\n"
        "The user-provided container failed to start and listen on the port defined by the PORT=8080 environment variable.\n"
        "--- cloudrun revision logs ---\n"
        'psycopg2.OperationalError: connection to server at "db.internal" (10.0.0.5), port 5432 failed: '
        'FATAL: password authentication failed for user "app_user"\n'
    )
    assert needs_human_stop(log).startswith(SERVICE_STOP)


@pytest.mark.parametrize(
    "logs",
    [
        "exec /app/entrypoint.sh: no such file or directory",
        "/usr/bin/env: 'python\\r': No such file or directory",
    ],
)
def test_visible_exec_error_is_not_reported_as_quiet_crash(logs):
    from app.healer import needs_human_stop

    log = (
        "[deployer] stage=verify kind=runtime_error target=local\ncontainer exited during startup\n"
        "--- error ---\ncontainer status=exited exit_code=255\n--- runtime logs ---\n" + logs + "\n"
    )
    assert needs_human_stop(log) is None


def test_optional_shared_library_warning_does_not_mask_port_binding():
    log = (
        "W tensorflow/stream_executor/platform/default/dso_loader.cc:64] Could not load dynamic library "
        "'libcudart.so.11.0'; dlerror: libcudart.so.11.0: cannot open shared object file: No such file or directory\n"
        + PORT_ERROR
    )
    assert classify_error(log) == ErrorCategory.PORT_BINDING
    assert (
        classify_error("ImportError: libxslt.so.1: cannot open shared object file: No such file or directory")
        == ErrorCategory.MISSING_DEPENDENCY
    )


def test_service_summary_redacts_schemeless_credentials():
    from app.healer import needs_human_stop

    summary = needs_human_stop(
        "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
        "connection to admin:1234@db.internal:5432 failed: Connection refused\n"
    )
    assert "admin" not in summary and "1234" not in summary and "db.internal:5432" in summary
