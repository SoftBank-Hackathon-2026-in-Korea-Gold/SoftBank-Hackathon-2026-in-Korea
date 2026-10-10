"""Cloud Run functions target: shape detection, preflight, and the gcloud command it runs (gcloud mocked)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import deployer
from app.deployer import Config, Outcome, StepError, deploy_function, detect_function

SAMPLE = Path(__file__).resolve().parents[2] / "sample-apps" / "function-hello"


def _src(tmp_path: Path, **files: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    return tmp_path


def test_detects_python_decorated_function_and_the_sample():
    assert detect_function(SAMPLE) == ("python312", "hello")


def test_detects_plain_request_handler_with_functions_framework(tmp_path):
    src = _src(
        tmp_path,
        **{"main.py": "def handle(request):\n    return 'ok'\n", "requirements.txt": "functions-framework\n"},
    )
    assert detect_function(src) == ("python312", "handle")


def test_detects_node_function(tmp_path):
    src = _src(
        tmp_path,
        **{
            "package.json": json.dumps(
                {"main": "index.js", "dependencies": {"@google-cloud/functions-framework": "^3"}}
            ),
            "index.js": "const functions = require('@google-cloud/functions-framework');\nfunctions.http('greet', (req, res) => res.send('hi'));\n",
        },
    )
    assert detect_function(src) == ("nodejs20", "greet")


def test_web_apps_are_not_functions():
    assert detect_function(SAMPLE.parent / "guestbook") is None
    assert detect_function(SAMPLE.parent / "healthy") is None


def test_preflight_rejects_non_function_source_as_user_action(tmp_path, monkeypatch):
    monkeypatch.setattr(deployer.shutil, "which", lambda name: None)
    with pytest.raises(StepError) as e:
        deployer.preflight(
            "function", _src(tmp_path, **{"app.py": "print(1)"}), Config(project="p"), deployer.Runner()
        )
    assert e.value.kind == "user_action_required" and "function-shaped" in e.value.error


class FakeRunner:
    def __init__(self, deploy_rc=0, url="https://fn-abc-du.a.run.app"):
        self.steps, self.calls, self.deploy_rc, self.url = [], [], deploy_rc, url

    def run(self, name, cmd, **kw):
        self.calls.append(cmd)
        rc, out, err = 0, "", ""
        if name == "gcloud functions deploy":
            rc, err = (
                self.deploy_rc,
                ""
                if self.deploy_rc == 0
                else "ERROR: (gcloud.functions.deploy) Build failed: pip install error",
            )
        elif name == "function url":
            out = self.url + "\n"
        return type("CP", (), {"returncode": rc, "stdout": out, "stderr": err})()


def test_deploy_function_runs_gen2_http_deploy_and_verifies(tmp_path, monkeypatch):
    src = _src(
        tmp_path / "Function_Hello",
        **{"main.py": (SAMPLE / "main.py").read_text(), "requirements.txt": "functions-framework\n"},
    )
    monkeypatch.setattr(deployer, "wait_up", lambda url, path, timeout, **kw: (True, "HTTP 200"))
    r, out = FakeRunner(), Outcome(target="function")
    deploy_function(src, Config(project="p", region="asia-northeast3"), r, out)
    cmd = r.calls[0]
    assert cmd[:4] == ["gcloud", "functions", "deploy", "function-hello"]
    assert {"--gen2", "--trigger-http", "--allow-unauthenticated"} <= set(cmd)
    assert cmd[cmd.index("--entry-point") + 1] == "hello" and cmd[cmd.index("--runtime") + 1] == "python312"
    assert out.url == "https://fn-abc-du.a.run.app"
    assert "Dockerfile" in (src / ".gcloudignore").read_text()


def test_function_build_failure_is_reported_as_unhealable_function_error(tmp_path, monkeypatch):
    src = _src(tmp_path / "f", **{"main.py": (SAMPLE / "main.py").read_text()})
    with pytest.raises(StepError) as e:
        deploy_function(src, Config(project="p"), FakeRunner(deploy_rc=1), Outcome(target="function"))
    assert e.value.kind == "function_error" and "Build failed" in e.value.error
    from app.healer import UNHEALABLE_KINDS

    assert "function_error" in UNHEALABLE_KINDS
