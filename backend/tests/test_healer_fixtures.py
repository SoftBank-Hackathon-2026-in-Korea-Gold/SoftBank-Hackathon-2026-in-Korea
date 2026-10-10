"""healer against real deployer stderr captured in PR #3 (tests/fixtures/*.stderr.txt).

Fixtures are the deployer's actual output for sample-apps/broken*, so these tests pin the
healer <-> deployer contract: header parsing, classification, and the rule-patch demo path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import healer
from app.healer import (
    APP_CODE_STOP,
    RepoFix,
    _llm_context,
    _patch_unresolvable_pin,
    _source_file,
    _stops_on_app_code,
    _unhealable,
    app_code_failure,
    classify_error,
    deployer_header,
    heal,
    repo_edit_tool,
)
from app.schemas import DeployResult, ErrorCategory

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_APPS = Path(__file__).resolve().parents[2] / "sample-apps"

EXPECTED = {
    "broken.local": ErrorCategory.MISSING_DEPENDENCY,
    "broken-port.local": ErrorCategory.PORT_BINDING,
    "broken-requirements.local": ErrorCategory.BUILD_FAILURE,
    "broken-import.local": ErrorCategory.UNKNOWN,
    "broken-import.cloudrun": ErrorCategory.UNKNOWN,
    "broken-health.local": ErrorCategory.UNKNOWN,
}


def _stderr(name: str) -> str:
    return (FIXTURES / f"{name}.stderr.txt").read_text()


def _dockerfile(name: str) -> str:
    return (SAMPLE_APPS / name.split(".")[0] / "Dockerfile").read_text()


def _failed(name: str) -> DeployResult:
    target = name.split(".")[1]
    return DeployResult(success=False, target=target, exit_code=1, stderr=_stderr(name))


def test_every_fixture_has_an_expectation():
    assert {p.name.removesuffix(".stderr.txt") for p in FIXTURES.glob("*.stderr.txt")} == set(EXPECTED)


@pytest.mark.parametrize(("name", "category"), EXPECTED.items())
def test_classifies_real_deployer_stderr(name, category):
    assert classify_error(_stderr(name)) == category


@pytest.mark.parametrize("name", EXPECTED)
def test_header_is_parsed_and_app_failures_stay_healable(name):
    stage, kind = deployer_header(_stderr(name))
    assert stage in {"build", "verify"} and kind in {"build_error", "runtime_error"}
    assert _unhealable(_stderr(name)) is None


@pytest.mark.parametrize("name", EXPECTED)
def test_llm_context_keeps_header_and_diagnosis(name):
    lines = [line for line in _stderr(name).splitlines() if line.strip()]
    ctx = _llm_context(_stderr(name)).splitlines()
    assert ctx[0] == lines[0]  # "[deployer] stage=... kind=..."
    if not lines[1].startswith("--- "):
        assert ctx[1] == lines[1]  # one-line diagnosis
    assert ctx[-1] == lines[-1]


def test_broken_sample_heals_with_rules_only_on_real_stderr():
    """Demo path: ModuleNotFoundError -> pip install flask -> port failure -> rebind -> live URL."""
    redeploys = iter(
        [
            _failed("broken-port.local"),  # real 'listening on the wrong port' stderr
            DeployResult(success=True, target="local", exit_code=0, url="https://demo.trycloudflare.com"),
        ]
    )
    calls: list[str] = []

    def fake_deploy(src_dir, dockerfile, target):
        calls.append(dockerfile)
        return next(redeploys)

    def no_llm(*_):
        raise AssertionError("demo path must not need the LLM")

    report = heal(
        "/tmp/app", "local", _failed("broken.local"), _dockerfile("broken.local"), fake_deploy, no_llm
    )

    assert report.success and report.attempts == 2 and len(calls) == 2
    assert [r.category for r in report.records] == [
        ErrorCategory.MISSING_DEPENDENCY,
        ErrorCategory.PORT_BINDING,
    ]
    assert "RUN pip install --no-cache-dir flask" in report.final_dockerfile
    assert '"0.0.0.0:8080"' in report.final_dockerfile and "ENV PORT=8080" in report.final_dockerfile


def test_broken_port_sample_rebinds_cmd():
    report = heal(
        "/tmp/app",
        "local",
        _failed("broken-port.local"),
        _dockerfile("broken-port.local"),
        lambda *_: DeployResult(success=True, target="local", exit_code=0, url="https://x"),
        lambda *_: None,
    )
    assert report.success and report.records[0].source == "rule"
    assert "--bind 0.0.0.0:8080" in report.final_dockerfile


# --------------------------------------------------------------------------- #
# M2 demo contract: every real failure ends the same way on every run, without the LLM deciding it.
# "healed" = a rule patch fixed the Dockerfile; "stopped" = intended stop, nothing redeployed.
# --------------------------------------------------------------------------- #
OUTCOME = {
    "broken.local": "healed",
    "broken-port.local": "healed",
    "broken-requirements.local": "healed",
    "broken-import.local": "stopped",
    "broken-import.cloudrun": "stopped",
    "broken-health.local": "stopped",
}


def _forbidden(what: str):
    def fail(*_):
        raise AssertionError(f"{what} must not be called")

    return fail


def _succeed(target: str):
    return lambda *_: DeployResult(success=True, target=target, exit_code=0, url="https://x")


def test_every_fixture_has_an_outcome():
    assert set(OUTCOME) == set(EXPECTED)


@pytest.mark.parametrize(("name", "outcome"), OUTCOME.items())
def test_fixture_outcome_is_deterministic_and_llm_free(name, outcome):
    failed = _failed(name)
    deploy = _succeed(failed.target) if outcome == "healed" else _forbidden("redeploy")

    report = heal(
        "/tmp/app",
        failed.target,
        failed,
        _dockerfile(name),
        deploy,
        _forbidden("Dockerfile LLM patch"),
        suggest_fn=lambda *_: None,
    )

    if outcome == "healed":
        assert report.success and report.attempts >= 1
        assert all(r.source == "rule" for r in report.records)
    else:
        assert not report.success and report.attempts == 0 and report.records == []
        assert report.final_dockerfile == _dockerfile(name)
        assert report.summary.startswith(APP_CODE_STOP)


def test_broken_requirements_unpins_before_pip_install():
    report = heal(
        "/tmp/app",
        "local",
        _failed("broken-requirements.local"),
        _dockerfile("broken-requirements.local"),
        _succeed("local"),
        _forbidden("Dockerfile LLM patch"),
    )
    lines = report.final_dockerfile.splitlines()
    sed = next(i for i, line in enumerate(lines) if line.startswith("RUN sed -i -E"))
    assert "flask" in lines[sed] and "requirements.txt" in lines[sed]
    assert lines[sed + 1].startswith("RUN pip install") and lines[sed - 1].startswith("COPY requirements.txt")
    assert report.records[0].category == ErrorCategory.BUILD_FAILURE
    assert "flask==99.99.99" in report.records[0].rationale


def test_unpin_rule_handles_inline_pin_and_is_idempotent():
    stderr = _stderr("broken-requirements.local")
    inline = "FROM python:3.12-slim\nRUN pip install --no-cache-dir flask==99.99.99 gunicorn\nCMD gunicorn app:app\n"
    patched, _ = _patch_unresolvable_pin(inline, stderr)
    assert "pip install --no-cache-dir flask gunicorn" in patched

    once, _ = _patch_unresolvable_pin(_dockerfile("broken-requirements.local"), stderr)
    assert _patch_unresolvable_pin(once, stderr) is None  # same patch twice -> stop instead of looping


def test_unpin_rule_ignores_missing_package_without_pin():
    stderr = "ERROR: No matching distribution found for flaskk"
    assert _patch_unresolvable_pin(_dockerfile("broken-requirements.local"), stderr) is None


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("broken-import.local", ("/app/app.py", 5, "<module>")),
        ("broken-import.cloudrun", ("/app/app.py", 5, "<module>")),
        ("broken-health.local", ("/app/app.py", 20, "health")),
    ],
)
def test_app_code_failure_points_at_user_frame(name, where):
    error = app_code_failure(_stderr(name))
    assert error is not None and (error.path, error.line, error.func) == where
    assert error.exception.split(":")[0] in {"NameError", "RuntimeError"}


@pytest.mark.parametrize("name", ["broken.local", "broken-port.local", "broken-requirements.local"])
def test_app_code_check_never_overrides_rule_categories(name):
    assert _stops_on_app_code(_stderr(name)) is None


def test_import_crash_reports_suggested_source_diff_without_applying_it(tmp_path):
    app_py = tmp_path / "app.py"
    original = (SAMPLE_APPS / "broken-import" / "app.py").read_text()
    app_py.write_text(original)
    seen: list[str] = []
    events = []

    def suggest(path, source, traceback):
        seen.append(path)
        assert "NameError" in traceback
        return "from flask import Flask, jsonify\n" + source

    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-import.local"),
        _dockerfile("broken-import.local"),
        _forbidden("redeploy"),
        _forbidden("Dockerfile LLM patch"),
        emit=events.append,
        suggest_fn=suggest,
    )

    assert seen == ["app.py"]
    assert report.summary.startswith(APP_CODE_STOP)
    assert "Suggested source fix (not applied):" in report.summary
    assert "+from flask import Flask, jsonify" in report.summary
    assert app_py.read_text() == original  # suggestion only: the source is never modified
    assert [e.payload["kind"] for e in events if e.type == "log"] == ["source_suggestion"]


def test_request_time_error_gets_traceback_summary_only(tmp_path):
    (tmp_path / "app.py").write_text((SAMPLE_APPS / "broken-health" / "app.py").read_text())
    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-health.local"),
        _dockerfile("broken-health.local"),
        _forbidden("redeploy"),
        _forbidden("Dockerfile LLM patch"),
        suggest_fn=_forbidden("source suggestion"),
    )
    assert "RuntimeError: database handshake failed" in report.summary
    assert "Suggested source fix" not in report.summary


def test_missing_source_file_still_stops_cleanly(tmp_path):
    report = heal(
        str(tmp_path),  # empty dir: nothing to suggest against
        "local",
        _failed("broken-import.local"),
        _dockerfile("broken-import.local"),
        _forbidden("redeploy"),
        _forbidden("Dockerfile LLM patch"),
        suggest_fn=_forbidden("source suggestion"),
    )
    assert report.summary.startswith(APP_CODE_STOP) and "Suggested source fix" not in report.summary


@pytest.mark.parametrize("container_path", ["/app/../../etc/passwd", "/etc/passwd", "../outside.py"])
def test_source_mapping_refuses_paths_outside_src_dir(tmp_path, container_path):
    (tmp_path.parent / "outside.py").write_text("x = 1\n")
    assert _source_file(str(tmp_path), "FROM x\nWORKDIR /app\n", container_path) is None


def test_suggester_exception_does_not_break_the_stop(tmp_path):
    (tmp_path / "app.py").write_text((SAMPLE_APPS / "broken-import" / "app.py").read_text())

    def broken_suggester(*_):
        raise TimeoutError("LLM endpoint timed out")

    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-import.local"),
        _dockerfile("broken-import.local"),
        _forbidden("redeploy"),
        _forbidden("Dockerfile LLM patch"),
        suggest_fn=broken_suggester,
    )
    assert report.summary.startswith(APP_CODE_STOP) and report.attempts == 0


# --------------------------------------------------------------------------- #
# Repo agent: reads the source tree and may edit source files in src_dir (main.py's per-target copy).
# --------------------------------------------------------------------------- #
def test_repo_agent_fixes_import_crash_in_source_and_redeploys(tmp_path):
    app_py = tmp_path / "app.py"
    app_py.write_text((SAMPLE_APPS / "broken-import" / "app.py").read_text())
    dockerfile = _dockerfile("broken-import.local")

    def repair(src_dir, before, error_log, category):
        assert src_dir == str(tmp_path) and "NameError" in error_log
        edit_file, originals = repo_edit_tool(Path(src_dir))
        fixed = "import os\n\nfrom flask import Flask, jsonify\n"
        assert edit_file.invoke({"path": "app.py", "old": "import os\n", "new": fixed}) == "edited app.py"
        diff = "".join(f"--- {k}\n" for k in originals)  # healer only concatenates it
        return RepoFix(before, diff, "Flask was used without importing it")

    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-import.local"),
        dockerfile,
        _succeed("local"),
        repair_fn=repair,
    )

    assert report.success and report.attempts == 1
    assert report.records[0].source == "llm" and "--- app.py" in report.records[0].diff
    assert "from flask import Flask, jsonify" in app_py.read_text()


def test_repo_agent_that_changes_nothing_gives_up_without_redeploy(tmp_path):
    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-health.local"),
        _dockerfile("broken-health.local"),
        _forbidden("redeploy"),
        repair_fn=lambda src_dir, dockerfile, *_: RepoFix(dockerfile, "", "nothing to fix"),
    )
    assert not report.success and report.attempts == 0
    assert not report.summary.startswith(APP_CODE_STOP)  # the agent tried; this is not the policy stop


def test_repo_agent_is_the_default_with_a_claude_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = []
    monkeypatch.setattr(healer, "default_repo_repair", lambda *a: calls.append(a) or None)
    heal(str(tmp_path), "local", _failed("broken-import.local"), "FROM x\n", _forbidden("redeploy"))
    assert len(calls) == 1


def test_repo_edit_tool_edits_and_creates_inside_src_dir(tmp_path):
    (tmp_path / "app.py").write_text("a = 1\na = 1\nb = 2\n")
    edit_file, originals = repo_edit_tool(tmp_path)

    assert "exactly once" in edit_file.invoke({"path": "app.py", "old": "a = 1", "new": "a = 3"})
    assert edit_file.invoke({"path": "app.py", "old": "b = 2", "new": "b = 3"}) == "edited app.py"
    assert edit_file.invoke({"path": "pkg/new.py", "old": "", "new": "x = 1\n"}) == "edited pkg/new.py"
    assert "already exists" in edit_file.invoke({"path": "app.py", "old": "", "new": "x"})

    assert (tmp_path / "app.py").read_text() == "a = 1\na = 1\nb = 3\n"
    assert (tmp_path / "pkg" / "new.py").read_text() == "x = 1\n"
    assert originals == {"app.py": "a = 1\na = 1\nb = 2\n", "pkg/new.py": ""}


@pytest.mark.parametrize(
    "path", ["../outside.py", "/etc/passwd", ".env", "Dockerfile", ".deployer/state.json"]
)
def test_repo_edit_tool_refuses_paths_it_must_not_write(tmp_path, path):
    (tmp_path / "src").mkdir()
    edit_file, originals = repo_edit_tool(tmp_path / "src")
    assert edit_file.invoke({"path": path, "old": "", "new": "x"}).startswith("cannot edit")
    assert originals == {} and not (tmp_path / "outside.py").exists()


def _agent_fix(tmp_path):
    (tmp_path / "app.py").write_text((SAMPLE_APPS / "broken-import" / "app.py").read_text())

    def repair(src_dir, before, *_):
        edit_file, originals = repo_edit_tool(Path(src_dir))
        edit_file.invoke(
            {"path": "app.py", "old": "import os\n", "new": "import os\nfrom flask import Flask\n"}
        )
        return RepoFix(before, "--- app.py\n", "Flask was not imported", tuple(originals))

    return repair


def test_confirm_stop_ends_before_redeploy_with_its_summary(tmp_path):
    seen = []
    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-import.local"),
        _dockerfile("broken-import.local"),
        _forbidden("redeploy"),
        repair_fn=_agent_fix(tmp_path),
        confirm_fn=lambda src, fix: seen.append((src, fix.changed)) or "Stopped: PR is up",
    )
    assert seen == [(str(tmp_path), ("app.py",))]
    assert not report.success and report.summary == "Stopped: PR is up"
    assert report.attempts == 1 and "--- app.py" in report.records[0].diff  # the diff is still reported


def test_confirm_continue_redeploys_the_edited_source(tmp_path):
    report = heal(
        str(tmp_path),
        "local",
        _failed("broken-import.local"),
        _dockerfile("broken-import.local"),
        _succeed("local"),
        repair_fn=_agent_fix(tmp_path),
        confirm_fn=lambda *_: None,
    )
    assert report.success and "from flask import Flask" in (tmp_path / "app.py").read_text()


def test_rule_patches_never_ask(tmp_path):
    failed = _failed("broken-port.local")
    report = heal(
        str(tmp_path),
        "local",
        failed,
        _dockerfile("broken-port.local"),
        _succeed("local"),
        repair_fn=_forbidden("repo agent"),
        confirm_fn=_forbidden("confirm"),
    )
    assert report.success
