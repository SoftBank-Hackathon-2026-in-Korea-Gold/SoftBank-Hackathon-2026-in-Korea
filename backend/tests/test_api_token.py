"""CLOUDMORPH_API_TOKEN guard on /deploy* (used while the demo is exposed through a tunnel)."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch, token: str | None):
    if token is None:
        monkeypatch.delenv("CLOUDMORPH_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLOUDMORPH_API_TOKEN", token)
    from app import main

    importlib.reload(main)
    return TestClient(main.create_app())


def test_no_token_configured_means_open(monkeypatch):
    client = _client(monkeypatch, None)
    assert client.get("/health").status_code == 200
    assert client.get("/deploy/nope").status_code == 404  # reached the route, not blocked


def test_token_required_on_deploy_routes(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    assert client.get("/health").status_code == 200  # probes stay open
    assert client.get("/deploy/nope").status_code == 401
    assert client.post("/deploy", json={"source": "/tmp/x", "targets": ["local"]}).status_code == 401
    assert client.get("/deploy/nope", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/deploy/nope", headers={"X-API-Token": "s3cret"}).status_code == 404
    assert client.get("/deploy/nope/events?token=s3cret").status_code == 404  # SSE: query param works
