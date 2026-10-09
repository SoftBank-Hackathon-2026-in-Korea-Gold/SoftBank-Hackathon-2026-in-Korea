"""CLOUDMORPH_API_TOKEN guard on /deploy*, /fleet*, /projects* (used while the demo is exposed
through a tunnel). Unset token fails closed for anything that is not a direct loopback client."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)


def _client(monkeypatch, token: str | None, client: tuple[str, int] = LOCAL):
    if token is None:
        monkeypatch.delenv("CLOUDMORPH_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLOUDMORPH_API_TOKEN", token)
    from app import main

    importlib.reload(main)
    return TestClient(main.create_app(), client=client)


def test_no_token_allows_direct_loopback(monkeypatch):
    client = _client(monkeypatch, None)
    assert client.get("/health").status_code == 200
    assert client.get("/deploy/nope").status_code == 404  # reached the route, not blocked


def test_no_token_fails_closed_through_tunnel(monkeypatch):
    client = _client(monkeypatch, None)
    # cloudflared connects from loopback but adds Cf-Connecting-Ip / X-Forwarded-For
    tunnel = {"Cf-Connecting-Ip": "198.51.100.1"}
    assert client.get("/deploy/nope", headers=tunnel).status_code == 503
    assert client.get("/deploy/nope", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 503
    assert client.get("/fleet", headers=tunnel).status_code == 503
    assert client.get("/health", headers=tunnel).status_code == 200


def test_no_token_fails_closed_for_remote_client(monkeypatch):
    client = _client(monkeypatch, None, client=REMOTE)
    assert client.get("/deploy/nope").status_code == 503
    assert client.post("/deploy", json={"source": "/tmp/x", "targets": ["local"]}).status_code == 503
    assert client.get("/health").status_code == 200


def test_token_required_on_deploy_routes(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    assert client.get("/health").status_code == 200  # probes stay open
    assert client.get("/deploy/nope").status_code == 401
    assert client.post("/deploy", json={"source": "/tmp/x", "targets": ["local"]}).status_code == 401
    assert client.get("/deploy/nope", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/deploy/nope", headers={"X-API-Token": "s3cret"}).status_code == 404
    assert client.get("/deploy/nope/events?token=s3cret").status_code == 404  # SSE: query param works


def test_token_required_on_fleet_and_projects(monkeypatch):
    client = _client(monkeypatch, "s3cret", client=REMOTE)
    for path in ("/fleet", "/fleet/app/scale/2", "/projects"):
        assert client.get(path).status_code == 401, path
        # past the guard -> whatever the route says (404/405 until #13/#17 add these routes)
        assert client.get(path, headers={"X-API-Token": "s3cret"}).status_code != 401, path


def test_unprotected_paths_bypass_guard(monkeypatch):
    client = _client(monkeypatch, None, client=REMOTE)
    # the GitHub webhook is HMAC-verified by its own handler; lookalike prefixes are not protected
    assert client.post("/webhook/github", content=b"{}").status_code != 503
    assert client.get("/deployments-lookalike").status_code != 503
