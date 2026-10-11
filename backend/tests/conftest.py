"""Keep tests away from the real project registry (backend/.cloudmorph/projects.json)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_decision_models(monkeypatch):
    """A developer's .env may point at a running clef-flash or carry a Jev key; unit tests use fakes."""
    for name in ("CLEF_BASE_URL", "TYPESAFE_API_KEY", "ANALYZER_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDMORPH_GUARD", "0")  # no background probing of real containers in tests


@pytest.fixture(autouse=True)
def _isolated_project_registry(tmp_path, monkeypatch):
    # Host .env credentials must never turn unit tests into telemetry exports or
    # writes to the persistent repair memory. Feature tests opt in explicitly.
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    monkeypatch.setenv("CLOUDMORPH_HEAL_MEMORY", "0")
    # env var survives importlib.reload(app.main) in tests that reload the module
    monkeypatch.setenv("CLOUDMORPH_PROJECTS_FILE", str(tmp_path / "projects.json"))
    from app import main

    monkeypatch.setattr(main, "PROJECTS_FILE", tmp_path / "projects.json")
    saved = dict(main._projects)
    main._projects.clear()
    yield
    main._projects.clear()
    main._projects.update(saved)
