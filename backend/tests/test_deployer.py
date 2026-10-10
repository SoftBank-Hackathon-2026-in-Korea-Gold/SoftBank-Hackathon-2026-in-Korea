"""deployer contract tests (no Docker needed except the opt-in integration test)."""

from __future__ import annotations

import os
import shutil
import subprocess
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


def _record_runs(monkeypatch, failing=()):
    runs = []

    def fake_run(cmd, **kwargs):
        runs.append((cmd, kwargs.get("env")))
        return subprocess.CompletedProcess(cmd, 1 if tuple(cmd[:2]) in failing else 0, "", "")

    monkeypatch.setattr(deployer.subprocess, "run", fake_run)
    return runs


def test_build_secrets_stay_off_the_command_line(tmp_path, monkeypatch):
    runs = _record_runs(monkeypatch)
    cfg = deployer.Config(
        build_args={"GITHUB_USERNAME": "octo"}, build_secrets={"GITHUB_TOKEN": "ghp_s3cret"}
    )
    r = deployer.Runner()
    deployer.build_image(tmp_path, "app:1", None, cfg, r)
    ((cmd, env),) = runs
    assert cmd == [
        "docker", "build", "-t", "app:1",
        "--build-arg", "GITHUB_USERNAME=octo",
        "--secret", "id=GITHUB_TOKEN,env=GITHUB_TOKEN",
        str(tmp_path),
    ]  # fmt: skip
    assert env["GITHUB_TOKEN"] == "ghp_s3cret" and env["DOCKER_BUILDKIT"] == "1"
    assert "ghp_s3cret" not in r.steps[0].cmd  # the step (and last_result.json) never sees the value


def test_build_without_inputs_is_unchanged(tmp_path, monkeypatch):
    runs = _record_runs(monkeypatch)
    deployer.build_image(tmp_path, "app:1", "linux/amd64", deployer.Config(), deployer.Runner())
    assert runs == [(["docker", "build", "-t", "app:1", "--platform", "linux/amd64", str(tmp_path)], None)]


def test_build_secrets_without_buildx_ask_the_user(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    _record_runs(monkeypatch, failing={("docker", "buildx")})
    cfg = deployer.Config(tunnel=False, build_secrets={"GITHUB_TOKEN": "x"})
    with pytest.raises(deployer.StepError) as e:
        deployer.preflight("local", tmp_path, cfg, deployer.Runner())
    assert e.value.kind == "user_action_required" and "buildx" in e.value.error


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
