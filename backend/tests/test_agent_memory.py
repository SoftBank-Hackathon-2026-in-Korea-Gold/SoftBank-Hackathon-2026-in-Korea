"""Persistence, isolation, safety, and the actual healer's memory fallback."""

from __future__ import annotations

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import agent_memory, healer
from app.schemas import AnalysisResult, DeployResult

BASE = 'FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nCMD ["python", "app.py"]\n'
FIXED = BASE.replace("CMD", "RUN pip install --no-cache-dir redis\nCMD")
ERROR = "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\nRuntimeError: plugin unavailable"
SCOPE = agent_memory.MemoryScope("alice", "shop", "git:github.com/alice/shop")


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "app.py").write_text("print('hello')\n")
    return source


@pytest.fixture
def store(tmp_path):
    return agent_memory.MemoryStore(tmp_path / "memory.sqlite3")


def _remember(store, scope=SCOPE, base=BASE, after=FIXED, error=ERROR, snapshot="source", target="local"):
    return store.remember(scope, target, base, after, error, snapshot)


def _recall(store, scope=SCOPE, base=BASE, error=ERROR, snapshot="source", target="local"):
    return store.recall(scope, target, base, error, snapshot)


def _success(src, dockerfile, target):
    return DeployResult(success=True, target=target, exit_code=0, url="http://test.invalid")


def _heal(checkout, store, llm, deploy=_success, **options):
    return healer.heal(
        str(checkout),
        "local",
        DeployResult(success=False, target="local", exit_code=1, stderr=ERROR),
        BASE,
        deploy,
        llm_patch_fn=llm,
        memory_scope=SCOPE,
        memory_store=store,
        **options,
    )


def test_successful_llm_fix_is_reused_without_a_second_model_call(checkout, store):
    calls, events = [], []

    def llm(*args):
        calls.append(args)
        return FIXED

    assert _heal(checkout, store, llm).success
    assert _heal(checkout, store, llm, emit=events.append).success
    assert len(calls) == 1
    assert any(e.payload.get("action") == "hit" for e in events)
    # Reopening the store simulates a new backend process.
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _recall(agent_memory.MemoryStore(store.path), snapshot=snapshot) == FIXED


def test_named_pipeline_reuses_fix_when_realistic_deployer_writes_dockerfile(checkout, monkeypatch, tmp_path):
    from app import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY", "on")
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY_FILE", str(tmp_path / "pipeline-memory.db"))
    models, deploys = [], []
    monkeypatch.setattr(healer, "default_llm_patch", lambda *args: models.append(args) or FIXED)

    def deploy(directory, dockerfile, target):
        # The real deployer materialises the current recipe into its source copy.
        Path(directory, "Dockerfile").write_text(dockerfile)
        deploys.append(dockerfile)
        return DeployResult(
            success=dockerfile == FIXED,
            target=target,
            exit_code=0 if dockerfile == FIXED else 1,
            stderr=ERROR,
        )

    analysis = AnalysisResult(target="local", language="python", dockerfile=BASE)
    events = []
    for _ in range(2):
        assert main.run_pipeline(
            str(checkout), "local", events.append, analysis=analysis, deploy_fn=deploy, memory_scope=SCOPE
        )
    assert deploys == [BASE, FIXED, BASE, FIXED]
    assert len(models) == 1
    assert any(event.payload.get("action") == "hit" for event in events)


def test_failed_fix_is_not_learned(checkout, store):
    def failed(src, dockerfile, target):
        return DeployResult(success=False, target=target, exit_code=1, stderr=ERROR)

    assert not _heal(checkout, store, lambda *args: FIXED, failed).success
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _recall(store, snapshot=snapshot) is None


def test_recalled_fix_that_fails_is_invalidated(checkout, store):
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _remember(store, snapshot=snapshot)

    def failed(src, dockerfile, target):
        return DeployResult(success=False, target=target, exit_code=1, stderr=ERROR)

    assert not _heal(checkout, store, lambda *args: None, failed).success
    assert _recall(store, snapshot=snapshot) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", agent_memory.MemoryScope("bob", "shop", SCOPE.source)),
        ("scope", agent_memory.MemoryScope("alice", "blog", SCOPE.source)),
        ("scope", agent_memory.MemoryScope("alice", "shop", "git:github.com/mallory/shop")),
        ("target", "cloudrun"),
        ("base", BASE + "# changed\n"),
        ("snapshot", "different source"),
        ("error", "ValueError: new failure"),
    ],
)
def test_memory_never_crosses_identity_target_source_recipe_or_failure(store, field, value):
    assert _remember(store)
    assert _recall(store, **{field: value}) is None
    assert _recall(store) == FIXED


def test_source_changes_reject_a_previous_fix(checkout, store):
    assert _heal(checkout, store, lambda *args: FIXED).success
    (checkout / "app.py").write_text("print('new version')\n")
    calls = []
    assert _heal(checkout, store, lambda *args: calls.append(args) or FIXED).success
    assert len(calls) == 1


def test_secret_files_are_not_read_or_retained(checkout, store):
    snapshot = agent_memory.source_fingerprint(str(checkout))
    (checkout / ".env").write_text("API_KEY=sk-private-value\n")
    assert agent_memory.source_fingerprint(str(checkout)) == snapshot
    base = BASE + "ENV API_KEY=sk-private-existing-value\n"
    after = base.replace("CMD", "RUN pip install --no-cache-dir redis\nCMD")
    assert _remember(store, base=base, after=after, error="secret sk-private-log-value")
    contents = store.path.read_bytes()
    assert b"sk-private" not in contents and b"alice" not in contents and b"github.com" not in contents
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "instruction",
    [
        "ENV API_KEY=sk-private-value",
        "ARG PASSWORD=private",
        "RUN echo private-token",
        "RUN pip install https://user:password@private.example/pkg",
        "RUN pip install my-secret-package",
        'CMD ["curl", "https://private.example"]',
        "LABEL private=secret",
    ],
)
def test_secret_or_arbitrary_edits_are_never_retained(store, instruction):
    assert not _remember(store, after=BASE + instruction + "\n")


def test_deletions_cannot_remove_security_settings(store):
    assert not _remember(store, base=BASE + "USER nonroot\n", after=BASE)


def test_runtime_configuration_stop_precedes_memory_and_llm(checkout, store):
    log = (
        "[deployer] stage=verify kind=runtime_error target=local\n--- runtime logs ---\n"
        "missing required environment variable: DATABASE_URL"
    )
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _remember(store, error=log, snapshot=snapshot)

    def never(*args):
        pytest.fail("safety stop must precede deploy/model calls")

    report = healer.heal(
        str(checkout),
        "local",
        DeployResult(success=False, target="local", exit_code=1, stderr=log),
        BASE,
        never,
        llm_patch_fn=never,
        memory_scope=SCOPE,
        memory_store=store,
    )
    assert not report.success and report.attempts == 0
    assert report.summary.startswith(healer.CONFIG_STOP)


def test_source_dockerfile_memory_replay_still_requires_confirmation(checkout, store):
    (checkout / "Dockerfile").write_text(BASE)
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _remember(store, snapshot=snapshot)
    confirmations = []

    def confirm(src, fix):
        confirmations.append(fix)
        return "User declined Dockerfile edit"

    report = _heal(
        checkout,
        store,
        lambda *args: pytest.fail("memory should avoid LLM"),
        confirm_fn=confirm,
        dockerfile_is_source=True,
    )
    assert not report.success and len(confirmations) == 1
    assert report.summary == "User declined Dockerfile edit"


def test_source_edits_are_never_learned_even_after_success(checkout, store):
    def repair(src, dockerfile, error, category):
        (checkout / "app.py").write_text("print('fixed')\n")
        return healer.RepoFix(FIXED, "--- app.py\n+++ app.py\n", "source fix")

    report = _heal(checkout, store, None, repair_fn=repair)
    assert report.success
    assert _recall(store, snapshot=agent_memory.source_fingerprint(str(checkout))) is None


def test_store_bounds_and_expiry(tmp_path, monkeypatch):
    store = agent_memory.MemoryStore(tmp_path / "bounded.db", max_entries=4, max_per_scope=2, ttl_days=1)
    for i in range(6):
        _remember(store, error=f"RuntimeError: failure {i}")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fixes").fetchone()[0] == 2
    for i in range(8):
        _remember(store, scope=agent_memory.MemoryScope(f"user{i}", "app", "source"))
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fixes").fetchone()[0] == 4
    assert _remember(store)
    monkeypatch.setattr(agent_memory.time, "time", lambda: 1e12)
    assert _recall(store) is None


def test_concurrent_writes_preserve_each_project(store):
    scopes = [agent_memory.MemoryScope(f"user{i}", "app", "source") for i in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(pool.map(lambda scope: _remember(store, scope=scope), scopes))
    assert all(_recall(store, scope=scope) == FIXED for scope in scopes)


def test_corrupt_store_fails_open_for_the_pipeline(checkout, store):
    store.path.write_text("corrupt sqlite database")
    assert _heal(checkout, store, lambda *args: FIXED).success


def test_tampered_patch_cannot_bypass_admission_policy(store):
    assert _remember(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE fixes SET edits='[[0, 999, []]]'")
    assert _recall(store) is None


@pytest.mark.parametrize("value", [123, "[" * 2000 + "]" * 2000, '{"unexpected": 1}'])
def test_malformed_sqlite_row_is_ignored(store, value):
    assert _remember(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE fixes SET edits=?", (value,))
    assert _recall(store) is None


def test_symlinked_checkout_content_disables_reuse(checkout, tmp_path):
    destination = tmp_path / "external"
    destination.mkdir()
    (checkout / "linked").symlink_to(destination, target_is_directory=True)
    assert agent_memory.source_fingerprint(str(checkout)) is None


@pytest.mark.parametrize("directory", [".config", "dist", "build", "vendor", "target", ".next"])
def test_deployable_hidden_and_generated_content_invalidates_memory(checkout, store, directory):
    app_file = checkout / directory / "plugin.py"
    app_file.parent.mkdir()
    app_file.write_text("version one")
    snapshot = agent_memory.source_fingerprint(str(checkout))
    assert _remember(store, snapshot=snapshot)
    assert _recall(store, snapshot=snapshot) == FIXED
    app_file.write_text("version two")
    changed = agent_memory.source_fingerprint(str(checkout))
    assert changed and changed != snapshot
    assert _recall(store, snapshot=changed) is None


def test_memory_configuration_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDMORPH_HEAL_MEMORY", raising=False)
    assert agent_memory.configured_store() is None
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY", "on")
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY_FILE", str(tmp_path / "memory.db"))
    assert agent_memory.configured_store().path == tmp_path / "memory.db"
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY", "off")
    assert agent_memory.configured_store() is None


def test_fix_learned_across_rule_and_llm_is_reused_on_subsequent_run(checkout, store):
    calls = []
    rule_error = (
        "[deployer] stage=verify kind=runtime_error target=local\n"
        "container started but failed to start and listen on the port defined by PORT=8080 "
        "(timeout after 15s); the app is probably bound to another port or to 127.0.0.1\n"
        "--- runtime logs ---\n"
        "[INFO] Listening at: http://127.0.0.1:5000\n"
    )
    llm_error = (
        "[deployer] stage=verify kind=runtime_error target=local\n"
        "--- runtime logs ---\n"
        "RuntimeError: plugin unavailable\n"
    )

    rule_fixed, _ = healer._patch_port_binding(BASE, rule_error)
    final_fixed = rule_fixed.replace("CMD", "RUN pip install --no-cache-dir redis\nCMD")

    def deploy(directory, dockerfile, target):
        if dockerfile == BASE:
            return DeployResult(success=False, target=target, exit_code=1, stderr=rule_error)
        if dockerfile == rule_fixed:
            return DeployResult(success=False, target=target, exit_code=1, stderr=llm_error)
        if dockerfile == final_fixed:
            return DeployResult(success=True, target=target, exit_code=0, url="http://test.invalid")
        return DeployResult(success=False, target=target, exit_code=1, stderr="unexpected dockerfile")

    def llm(*args):
        calls.append(args)
        return final_fixed

    report1 = healer.heal(
        str(checkout),
        "local",
        DeployResult(success=False, target="local", exit_code=1, stderr=rule_error),
        BASE,
        deploy,
        llm_patch_fn=llm,
        memory_scope=SCOPE,
        memory_store=store,
    )
    assert report1.success
    assert len(calls) == 1

    events = []
    report2 = healer.heal(
        str(checkout),
        "local",
        DeployResult(success=False, target="local", exit_code=1, stderr=rule_error),
        BASE,
        deploy,
        llm_patch_fn=llm,
        memory_scope=SCOPE,
        memory_store=store,
        emit=events.append,
    )
    assert report2.success
    assert len(calls) == 1
    assert any(e.payload.get("action") == "hit" for e in events)
