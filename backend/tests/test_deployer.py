"""deployer contract tests (no Docker needed except the opt-in integration test)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app import deployer
from app.deployer import Outcome, Step, diagnose_not_reachable, slug
from app.healer import classify_error
from app.schemas import ErrorCategory

SAMPLE_APPS = Path(__file__).resolve().parents[2] / "sample-apps"


def test_slug_is_docker_and_cloudrun_safe():
    assert slug("Hello Flask_App") == "hello-flask-app"
    assert slug("업로드") == "app"


def test_stderr_carries_runtime_logs_so_healer_can_classify():
    out = Outcome(
        target="local",
        stage="verify",
        kind="runtime_error",
        error="container status=exited exit_code=3; process exited",
        app_logs="Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'flask'",
        steps=[Step("docker run", "docker run ...", 0, 0.1, "", "")],
    )
    res = out.to_contract()
    assert res.success is False
    assert res.exit_code == 1
    assert res.url is None
    assert res.stderr.startswith("[deployer] stage=verify kind=runtime_error target=local")
    assert "--- runtime logs ---" in res.stderr
    assert classify_error(res.stderr) == ErrorCategory.MISSING_DEPENDENCY


def test_build_failure_uses_failing_step_exit_code():
    out = Outcome(
        target="cloudrun",
        stage="build",
        kind="build_error",
        error='ERROR: failed to solve: process "/bin/sh -c pip install" did not complete successfully: exit code: 1',
        steps=[
            Step("docker info", "docker info", 0, 0.1, "", ""),
            Step("docker build", "docker build", 1, 2.0, "", ""),
        ],
    )
    res = out.to_contract()
    assert res.exit_code == 1
    assert classify_error(res.stderr) == ErrorCategory.BUILD_FAILURE


def test_wrong_port_diagnosis_is_classified_as_port_binding():
    logs = "[INFO] Starting gunicorn 22.0.0\n[INFO] Listening at: http://127.0.0.1:5000 (1)"
    diag = diagnose_not_reachable(8080, "timeout after 20s (last probe: ConnectionResetError)", logs)
    assert "PORT=8080" in diag
    assert classify_error(diag) == ErrorCategory.PORT_BINDING
    # a crash must NOT be mislabelled as a port problem, even though gunicorn's master logged "Listening at"
    crash_logs = "[INFO] Listening at: http://0.0.0.0:8080 (1)\nNameError: name 'Flask' is not defined\n[ERROR] Worker failed to boot."
    crash = diagnose_not_reachable(8080, "process exited (last probe: URLError)", crash_logs, exited=True)
    assert classify_error(crash) == ErrorCategory.UNKNOWN
    assert classify_error(diagnose_not_reachable(8080, "process exited", crash_logs)) == ErrorCategory.UNKNOWN
    # an app that answers 500 is listening fine: not a port problem either
    assert (
        classify_error(diagnose_not_reachable(8080, "timeout (last probe: HTTP 500)", logs))
        == ErrorCategory.UNKNOWN
    )


def test_success_result_has_url_and_no_error():
    out = Outcome(target="local", stage="verify", ok=True, url="https://x.trycloudflare.com", elapsed=12.34)
    res = out.to_contract()
    assert res.success and res.url == "https://x.trycloudflare.com" and res.exit_code == 0
    assert res.duration_sec == 12.3


def test_deploy_never_raises_and_writes_dockerfile(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no docker / cloudflared / gcloud
    res = deployer.deploy(str(tmp_path), "FROM scratch\n", "local")
    assert res.success is False
    assert res.exit_code != 0
    assert "docker" in res.stderr
    assert (tmp_path / "Dockerfile").read_text() == "FROM scratch\n"
    assert (tmp_path / ".deployer" / "last_result.json").exists()


def test_unknown_option_is_a_programming_error(tmp_path):
    with pytest.raises(TypeError):
        deployer.deploy(str(tmp_path), "", "local", nonsense=True)


@pytest.mark.skipif(not os.environ.get("DEPLOYER_IT"), reason="set DEPLOYER_IT=1 to run against local Docker")
def test_local_healthy_sample_end_to_end():
    res = deployer.deploy(str(SAMPLE_APPS / "healthy"), "", "local", tunnel=False, verify_timeout=60)
    try:
        assert res.success, res.stderr
        assert res.url and res.url.startswith("http://127.0.0.1:")
    finally:
        deployer.cleanup(str(SAMPLE_APPS / "healthy"))


@pytest.mark.skipif(not os.environ.get("DEPLOYER_IT"), reason="set DEPLOYER_IT=1 to run against local Docker")
def test_local_broken_sample_reports_missing_dependency():
    res = deployer.deploy(str(SAMPLE_APPS / "broken"), "", "local", tunnel=False, verify_timeout=20)
    try:
        assert res.success is False
        assert classify_error(res.stderr) == ErrorCategory.MISSING_DEPENDENCY
    finally:
        deployer.cleanup(str(SAMPLE_APPS / "broken"))
