from app.healer import classify_error, heal
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
