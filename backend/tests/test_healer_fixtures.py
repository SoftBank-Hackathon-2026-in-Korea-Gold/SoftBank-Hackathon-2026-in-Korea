"""healer against real deployer stderr captured in PR #3 (tests/fixtures/*.stderr.txt).

Fixtures are the deployer's actual output for sample-apps/broken*, so these tests pin the
healer <-> deployer contract: header parsing, classification, and the rule-patch demo path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.healer import _llm_context, _unhealable, classify_error, deployer_header, heal
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


@pytest.mark.parametrize(
    "name",
    ["broken-requirements.local", "broken-import.local", "broken-import.cloudrun", "broken-health.local"],
)
def test_source_level_failures_need_llm(name):
    """No rule covers these yet: without an LLM patch healer gives up without redeploying."""

    def never_called(*_):
        raise AssertionError("must not redeploy without a patch")

    report = heal(
        "/tmp/app", _failed(name).target, _failed(name), _dockerfile(name), never_called, lambda *_: None
    )
    assert not report.success and report.attempts == 0
