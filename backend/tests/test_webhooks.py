"""GitHub push -> CD: signature check, payload parsing, endpoint wiring (launcher is faked)."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main, webhooks

SECRET = "wh-secret"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(branch="main", default="main", deleted=False) -> dict:
    return {
        "ref": f"refs/heads/{branch}",
        "after": "abcdef1234567890",
        "deleted": deleted,
        "repository": {
            "full_name": "acme/Shop_API",
            "clone_url": "https://github.com/acme/Shop_API.git",
            "default_branch": default,
        },
        "head_commit": {"id": "abcdef1234567890", "message": "fix: bind 0.0.0.0\n\nlong body"},
        "pusher": {"name": "dev"},
    }


def test_signature_roundtrip_and_rejections():
    body = b'{"x":1}'
    assert webhooks.verify_signature(SECRET, body, _sig(body))
    assert not webhooks.verify_signature(SECRET, body, _sig(body, "other"))
    assert not webhooks.verify_signature(SECRET, body, None)
    assert not webhooks.verify_signature("", body, _sig(body, ""))


def test_parse_push_extracts_branch_name_and_slug(monkeypatch):
    info = webhooks.parse_push(_push())
    assert (
        info and info.branch == "main" and info.sha == "abcdef123456" and info.message == "fix: bind 0.0.0.0"
    )
    assert info.app_name == "shop-api"  # repo name -> stable app/service name
    assert webhooks.parse_push({"ref": "refs/tags/v1"}) is None
    monkeypatch.delenv("CLOUDMORPH_CD_BRANCHES", raising=False)
    assert webhooks.branch_allowed(info)
    assert not webhooks.branch_allowed(webhooks.parse_push(_push(branch="feature")))
    monkeypatch.setenv("CLOUDMORPH_CD_BRANCHES", "feature,main")
    assert webhooks.branch_allowed(webhooks.parse_push(_push(branch="feature")))


def _client(monkeypatch, secret=SECRET):
    if secret is None:
        monkeypatch.delenv("CLOUDMORPH_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("CLOUDMORPH_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("CLOUDMORPH_CD_TARGETS", "local")
    app = main.create_app()
    launched = []
    app.state.launch = lambda source, targets, **kw: launched.append((source, targets, kw)) or "dep123"
    return TestClient(app), launched


def test_webhook_disabled_without_secret(monkeypatch):
    client, _ = _client(monkeypatch, secret=None)
    assert client.post("/webhook/github", content=b"{}").status_code == 503


def test_webhook_rejects_bad_signature_and_accepts_ping(monkeypatch):
    client, launched = _client(monkeypatch)
    body = json.dumps({"zen": "hi"}).encode()
    assert client.post("/webhook/github", content=body, headers={"X-GitHub-Event": "ping"}).status_code == 401
    r = client.post(
        "/webhook/github", content=body, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sig(body)}
    )
    assert r.status_code == 200 and r.json()["event"] == "ping"
    assert launched == []


def test_webhook_push_launches_named_deployment_on_default_branch_only(monkeypatch):
    client, launched = _client(monkeypatch)
    body = json.dumps(_push()).encode()
    r = client.post(
        "/webhook/github", content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sig(body)}
    )
    assert r.status_code == 200 and r.json()["deployment_id"] == "dep123" and r.json()["name"] == "shop-api"
    source, targets, kw = launched[0]
    assert source == "https://github.com/acme/Shop_API.git" and targets == ["local"]
    assert kw["name"] == "shop-api" and kw["ref"] == "main" and kw["trigger"] == "github-push"
    assert "github push by dev" in kw["intro"][0]
    # a feature-branch push is acknowledged but not deployed
    body = json.dumps(_push(branch="feature")).encode()
    r = client.post(
        "/webhook/github", content=body, headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": _sig(body)}
    )
    assert r.status_code == 200 and "ignored" in r.json() and len(launched) == 1


def test_deploy_accepts_stable_name_and_ref(monkeypatch):
    client, launched = _client(monkeypatch)
    r = client.post(
        "/deploy", json={"source": "/tmp/x", "targets": ["local"], "name": "shop-api", "ref": "main"}
    )
    assert r.status_code == 200 and launched[0][2]["name"] == "shop-api" and launched[0][2]["ref"] == "main"


def test_projects_persist_across_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PROJECTS_FILE", tmp_path / "projects.json")
    main._projects.clear()
    main._projects["shop-api"] = {
        "name": "shop-api",
        "last_status": "completed",
        "history": ["d1"],
        "updated_at": 1.0,
    }
    main._save_projects()
    assert main._load_projects()["shop-api"]["last_status"] == "completed"
    main._projects.clear()


def test_relative_sample_paths_resolve_against_repo_root():
    with main.prepare_source("sample-apps/guestbook") as d:
        assert d.endswith("sample-apps/guestbook")


def test_project_status_is_persisted_when_the_job_finishes(monkeypatch, tmp_path):
    """Regression: the finish-time save was missing, so a restart showed finished apps as 'running'."""
    import asyncio

    monkeypatch.setattr(main, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(
        main, "prepare_source", lambda source, ref=None: __import__("contextlib").nullcontext(str(tmp_path))
    )
    monkeypatch.setattr(
        main.analyzer,
        "analyze",
        lambda d: type(
            "A",
            (),
            {
                "target": "local",
                "language": "python",
                "framework": None,
                "port": 8080,
                "service_models": [],
                "notes": [],
                "dockerfile": "FROM x",
            },
        )(),
    )
    monkeypatch.setattr(main, "_copy_for_target", lambda src, dst: str(dst))

    def fake_pipeline(target_dir, target, emit, analysis=None):
        emit(main.PipelineEvent(type="done", payload={"target": target, "url": "http://x"}))
        return True

    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    app = main.create_app()

    async def scenario():
        app.state.launch("/src", ["local"], name="persist-me")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if main._load_projects().get("persist-me", {}).get("last_status") == "completed":
                return True
        return False

    assert asyncio.run(scenario())
