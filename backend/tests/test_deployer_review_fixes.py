"""Regression tests for the PR #3 review findings (Copilot) — no Docker / gcloud needed.

1. a failed Cloud Run service lookup must not be treated as a new service (would skip --no-traffic)
2. a missing revision must stop before verify/promote; promotion targets the verified revision
3. a failed local deploy must not leave its container (or tunnel) running
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import deployer
from app.deployer import Config, Outcome, Runner, StepError

SAMPLE_APPS = Path(__file__).resolve().parents[2] / "sample-apps"


def _cp(cmd, rc=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def _cloud_cfg() -> Config:
    return Config(project="demo-proj", service="svc", tag="t1", skip_preflight=True)


# --------------------------------------------------------------------------- #
# 1. serving_revision fails closed
# --------------------------------------------------------------------------- #
def _fake_gcloud(monkeypatch, *, list_rc=0, list_out="svc", describe_out="svc-00001-abc"):
    def fake_run(cmd, **_):
        if "list" in cmd:
            return _cp(cmd, list_rc, list_out, "ERROR: (gcloud) transient" if list_rc else "")
        return _cp(cmd, 0, describe_out)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_lookup_failure_raises_instead_of_looking_like_a_new_service(monkeypatch):
    _fake_gcloud(monkeypatch, list_rc=1)
    with pytest.raises(StepError) as exc:
        deployer.serving_revision("svc", _cloud_cfg(), Runner())
    assert exc.value.stage == "deploy"


def test_missing_service_means_nothing_to_protect(monkeypatch):
    _fake_gcloud(monkeypatch, list_out="")
    assert deployer.serving_revision("svc", _cloud_cfg(), Runner()) is None


def test_existing_service_returns_serving_revision(monkeypatch):
    _fake_gcloud(monkeypatch)
    assert deployer.serving_revision("svc", _cloud_cfg(), Runner()) == "svc-00001-abc"


# --------------------------------------------------------------------------- #
# 2. revision must be known; promote the verified revision
# --------------------------------------------------------------------------- #
def _stub_cloudrun(monkeypatch, *, latest: str | None):
    commands: list[list[str]] = []
    monkeypatch.setattr(deployer, "build_image", lambda *a, **k: None)
    monkeypatch.setattr(deployer, "push_image", lambda *a, **k: None)
    monkeypatch.setattr(deployer, "serving_revision", lambda *a, **k: "svc-00001-old")
    monkeypatch.setattr(deployer, "latest_revision", lambda *a, **k: latest)
    monkeypatch.setattr(deployer, "tagged_url", lambda *a, **k: "https://candidate---svc.run.app")
    monkeypatch.setattr(deployer, "wait_up", lambda *a, **k: (True, "HTTP 200"))

    def fake_run(self, name, cmd, **_):
        commands.append(cmd)
        return _cp(cmd, 0, "https://svc.run.app" if name == "service url" else "")

    monkeypatch.setattr(Runner, "run", fake_run)
    return commands


def test_missing_revision_stops_before_verify_and_promote(monkeypatch, tmp_path):
    commands = _stub_cloudrun(monkeypatch, latest=None)
    monkeypatch.setattr(deployer, "tagged_url", lambda *a, **k: pytest.fail("must not look up a tag"))
    with pytest.raises(StepError) as exc:
        deployer.deploy_cloudrun(tmp_path, _cloud_cfg(), Runner(), Outcome(target="cloudrun"))
    assert exc.value.stage == "expose"
    assert not any("update-traffic" in c for c in commands)


def test_promotes_exactly_the_verified_revision(monkeypatch, tmp_path):
    commands = _stub_cloudrun(monkeypatch, latest="svc-00002-new")
    out = Outcome(target="cloudrun")
    deployer.deploy_cloudrun(tmp_path, _cloud_cfg(), Runner(), out)
    promote = next(c for c in commands if "update-traffic" in c)
    assert "--to-revisions=svc-00002-new=100" in promote and "--to-latest" not in promote
    assert out.url == "https://svc.run.app"


# --------------------------------------------------------------------------- #
# 3. failed local deploy cleans up its container
# --------------------------------------------------------------------------- #
def test_failed_local_verify_removes_container(monkeypatch, tmp_path):
    cleaned: list[dict] = []
    monkeypatch.setattr(deployer, "cleanup_local", lambda handles: cleaned.append(dict(handles)) or [])
    monkeypatch.setattr(deployer, "build_image", lambda *a, **k: None)
    monkeypatch.setattr(Runner, "run", lambda self, name, cmd, **_: _cp(cmd, 0, "cid123"))
    monkeypatch.setattr(deployer, "wait_up", lambda *a, **k: (False, "timeout after 1s"))
    monkeypatch.setattr(deployer, "container_state", lambda name: ("running", 0))
    monkeypatch.setattr(deployer, "container_logs", lambda name: "Listening at: http://0.0.0.0:5000")
    (tmp_path / deployer.STATE_DIR).mkdir()
    cfg = Config(tunnel=False, tag="t1", verify_timeout=1)
    out = Outcome(target="local")

    with pytest.raises(StepError):
        deployer.deploy_local(tmp_path, cfg, Runner(), out)

    assert cleaned[-1]["container_name"] == f"{deployer.slug(tmp_path.name)}-t1"
    assert "container_name" not in out.handles and out.url is None


def _containers(prefix: str) -> set[str]:
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return set(ps.stdout.split())


@pytest.mark.skipif(not os.environ.get("DEPLOYER_IT"), reason="set DEPLOYER_IT=1 to run against local Docker")
def test_local_broken_port_leaves_no_container(tmp_path):
    src = tmp_path / "broken-port"
    shutil.copytree(SAMPLE_APPS / "broken-port", src)
    before = _containers("broken-port-local")
    res = deployer.deploy(str(src), "", "local", tunnel=False, verify_timeout=10)
    assert res.success is False
    assert _containers("broken-port-local") - before == set()
