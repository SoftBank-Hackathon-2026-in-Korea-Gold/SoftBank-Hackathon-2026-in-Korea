"""Keep tests away from the real project registry (backend/.cloudmorph/projects.json)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_project_registry(tmp_path, monkeypatch):
    # env var survives importlib.reload(app.main) in tests that reload the module
    monkeypatch.setenv("CLOUDMORPH_PROJECTS_FILE", str(tmp_path / "projects.json"))
    from app import main

    monkeypatch.setattr(main, "PROJECTS_FILE", tmp_path / "projects.json")
    saved = dict(main._projects)
    main._projects.clear()
    yield
    main._projects.clear()
    main._projects.update(saved)
