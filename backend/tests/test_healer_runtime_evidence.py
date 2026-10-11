"""Synthetic provider wording regressions; these are not measured cloud logs."""

import pytest

from app import healer
from app.schemas import DeployResult, ErrorCategory

BASE = 'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD ["gunicorn", "-b", "127.0.0.1:5000", "app:app"]\n'
WRAPPER = "The user-provided container failed to start and listen on the port defined by PORT=8080."


def _log(runtime="", diagnosis=WRAPPER, stage="deploy", kind="deploy_error"):
    return (
        f"[deployer] stage={stage} kind={kind} target=cloudrun\n{diagnosis}\n--- runtime logs ---\n{runtime}"
    )


@pytest.mark.parametrize(
    "wording",
    [
        WRAPPER,
        "Container failed to start. Failed to start and then listen on the port defined by PORT environment variable.",
        "The user provided container failed to start and listen on port defined by PORT=8080 environment variable.",
    ],
)
def test_cloud_run_port_wrapper_variants(wording):
    assert healer.classify_error(_log(diagnosis=wording)) == ErrorCategory.PORT_BINDING


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("ModuleNotFoundError: No module named 'flask'", ErrorCategory.MISSING_DEPENDENCY),
        (
            "2026-10-11T01:02:03Z\tOSError: libpq.so.5: cannot open shared object file",
            ErrorCategory.MISSING_DEPENDENCY,
        ),
        ("NameError: name 'missing' is not defined", ErrorCategory.UNKNOWN),
        ("Memory limit of 512 MiB exceeded", ErrorCategory.OUT_OF_MEMORY),
    ],
)
def test_runtime_cause_takes_precedence_over_port_wrapper(runtime, expected):
    assert healer.classify_error(_log(runtime)) == expected


def test_timestamped_runtime_config_stops_before_model_or_retry():
    log = _log(
        "2026-10-11T01:02:03Z\tTraceback (most recent call last):\n"
        '2026-10-11T01:02:03Z\t  File "<frozen os>", line 679, in __getitem__\n'
        "2026-10-11T01:02:03Z\tKeyError: 'API_KEY'"
    )
    assert healer.needs_human_stop(log).startswith(healer.CONFIG_STOP)


def test_cloud_run_app_crash_stops_instead_of_becoming_a_port_fix():
    log = _log(
        'Traceback (most recent call last):\n  File "/app/app.py", line 5, in <module>\nNameError: broken'
    )

    def never(*args):
        pytest.fail("application error must not be redeployed or guessed")

    report = healer.heal(
        "/tmp/nonexistent",
        "cloudrun",
        DeployResult(success=False, target="cloudrun", exit_code=1, stderr=log),
        BASE,
        never,
        llm_patch_fn=never,
        suggest_fn=lambda *args: None,
    )
    assert report.attempts == 0 and report.summary.startswith(healer.APP_CODE_STOP)


def test_startup_wrapper_with_correct_command_and_no_evidence_stops():
    correct = BASE.replace("127.0.0.1:5000", "0.0.0.0:8080")
    assert healer.needs_human_stop(_log(), dockerfile=correct).startswith(healer.STARTUP_STOP)


def test_silent_cloud_run_failure_with_no_editable_command_stops_with_diagnosis():
    correct = BASE.replace('CMD ["gunicorn", "-b", "127.0.0.1:5000", "app:app"]', 'CMD ["python", "app.py"]')
    summary = healer.needs_human_stop(_log(), dockerfile=correct)
    assert summary.startswith(healer.STARTUP_STOP) and "revision logs" in summary


def test_explicit_port_contract_is_used_for_rebinding():
    patched, rationale = healer._patch_port_binding(BASE, _log(diagnosis=WRAPPER.replace("8080", "9000")))
    assert "0.0.0.0:9000" in patched and "ENV PORT=9000" in patched and "9000" in rationale


def test_existing_env_port_is_updated_to_the_actual_contract():
    patched, _ = healer._patch_port_binding(BASE.replace("CMD", "ENV PORT=5000\nCMD"), _log())
    assert "ENV PORT=8080" in patched and "ENV PORT=5000" not in patched


def test_expose_failure_is_an_infrastructure_stop():
    log = _log("", diagnosis="Cloudflare tunnel unavailable", stage="expose")

    def never(*args):
        pytest.fail("exposure failure cannot be healed through a Dockerfile")

    report = healer.heal(
        "/tmp/nonexistent",
        "cloudrun",
        DeployResult(success=False, target="cloudrun", exit_code=1, stderr=log),
        BASE,
        never,
        llm_patch_fn=never,
    )
    assert report.attempts == 0 and "stage=expose" in report.summary


def test_node_esm_dependency_is_not_mistaken_for_buildkit_error():
    log = "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'express' imported from /app/server.js"
    assert healer.classify_error(log) == ErrorCategory.MISSING_DEPENDENCY
    assert (
        "RUN npm install express"
        in healer._patch_missing_dependency("FROM node:20\nCMD node server.js\n", log)[0]
    )
    assert healer.classify_error("TypeError [ERR_INVALID_ARG_TYPE]: wrong type") == ErrorCategory.UNKNOWN


@pytest.mark.parametrize("module,create_local", [("app", False), ("config", True)])
def test_local_python_modules_do_not_trigger_dependency_confusion(tmp_path, module, create_local):
    if create_local:
        (tmp_path / f"{module}.py").write_text("value = 1\n")
    log = _log(f"ModuleNotFoundError: No module named '{module}'")

    def never(*args):
        pytest.fail("a local app module must not cause pip installation")

    report = healer.heal(
        str(tmp_path),
        "cloudrun",
        DeployResult(success=False, target="cloudrun", exit_code=1, stderr=log),
        BASE,
        never,
        llm_patch_fn=never,
    )
    assert report.attempts == 0 and report.summary.startswith(healer.LOCAL_IMPORT_STOP)
