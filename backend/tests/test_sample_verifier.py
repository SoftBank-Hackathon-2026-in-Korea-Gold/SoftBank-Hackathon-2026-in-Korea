"""The Docker verifier must remain model-free even in an authenticated developer shell."""

import importlib.util
from pathlib import Path

from app import deployer, healer
from app.schemas import DeployResult


def test_import_stop_never_calls_source_suggestion_provider(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("sample_verifier", root / "scripts/verify-sample-apps.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(healer, "_ask_llm", lambda *a, **k: calls.append(a) or "from flask import Flask")
    failed = DeployResult(
        success=False,
        target="local",
        exit_code=1,
        stderr=(root / "backend/tests/fixtures/broken-import.local.stderr.txt").read_text(),
    )
    monkeypatch.setattr(deployer, "deploy", lambda *a, **k: failed)
    monkeypatch.setattr(deployer, "cleanup", lambda *_: None)
    passed, outcome, redeploys, _ = verifier.verify_sample("broken-import")
    assert passed and outcome == "app-code stop" and redeploys == 0
    assert calls == []
