"""Opt-in: one real call to each healer LLM path (Dockerfile fallback + source suggestion).

    HEALER_LIVE_LLM=1 uv run pytest -q tests/test_healer_live_llm.py -s

Reads ANTHROPIC_API_KEY / HEALER_MODEL from the repo .env. Two short Claude calls (shared team key).
Assertions are loose on purpose: the model's exact wording varies, the shape must not.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.healer import _llm_context, default_llm_patch, default_source_suggestion
from app.schemas import ErrorCategory

pytestmark = pytest.mark.skipif(
    not os.environ.get("HEALER_LIVE_LLM"), reason="set HEALER_LIVE_LLM=1 to call the real LLM endpoint"
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True, scope="module")
def _env():
    load_dotenv(ROOT / ".env")  # tests import app.healer, never app.main, so load it here
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY in env or .env")


def test_dockerfile_fallback_returns_a_changed_dockerfile():
    """No rule covers a typo'd executable, so this is exactly the case the LLM fallback exists for."""
    dockerfile = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nRUN pip install --no-cache-dir flask gunicorn\n"
        "ENV PORT=8080\nCMD exec gunicron --bind 0.0.0.0:$PORT app:app\n"
    )
    stderr = (
        "[deployer] stage=verify kind=runtime_error target=local\n"
        "container exited during startup; see runtime logs\n--- runtime logs ---\n"
        "/bin/sh: 1: exec: gunicron: not found\n"
    )

    patched = default_llm_patch(dockerfile, stderr, ErrorCategory.UNKNOWN)

    print("\n--- LLM Dockerfile patch ---\n" + (patched or "<None>"))
    assert patched and "FROM" in patched and patched != dockerfile
    assert "gunicorn" in patched.split("CMD", 1)[-1]


def test_source_suggestion_for_import_crash():
    source = (ROOT / "sample-apps" / "broken-import" / "app.py").read_text()
    traceback = _llm_context((FIXTURES / "broken-import.local.stderr.txt").read_text())

    suggested = default_source_suggestion("app.py", source, traceback)

    assert suggested and suggested.strip() != source.strip()
    diff = "".join(
        difflib.unified_diff(source.splitlines(True), suggested.splitlines(True), "before", "after")
    )
    print("\n--- LLM source suggestion diff ---\n" + diff)
    assert "Flask" in diff
