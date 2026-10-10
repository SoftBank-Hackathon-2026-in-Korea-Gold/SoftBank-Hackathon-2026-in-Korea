"""Per-app env store on Secret Manager (gcloud mocked)."""

from __future__ import annotations

import json

import pytest

from app import env_store


class FakeGcloud:
    """Just enough of `gcloud secrets` to round-trip values; records every call."""

    def __init__(self, secrets: dict[str, str] | None = None, fail: str | None = None):
        self.secrets = dict(secrets or {})
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail = fail

    def __call__(self, cmd, input=None, **kw):
        self.calls.append((cmd, input))
        args = cmd[2 : cmd.index("--project")]
        rc, out = 0, ""
        if args[0] == self.fail:
            rc = 1
        elif args[0] == "list":
            sid = args[1].split(":", 1)[1]
            out = f"projects/123/secrets/{sid}\n" if sid in self.secrets else ""
        elif args[:2] == ["versions", "access"]:
            out = self.secrets[args[3].split("=", 1)[1]]
        elif args[:2] == ["versions", "add"] or args[0] == "create":
            self.secrets[args[2] if args[0] == "versions" else args[1]] = input
        return type("CP", (), {"returncode": rc, "stdout": out, "stderr": "denied" if rc else ""})()


def test_round_trip_creates_once_then_adds_versions(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    gc = FakeGcloud()
    monkeypatch.setattr(env_store.subprocess, "run", gc)

    assert env_store.load("guestbook") == {}
    env_store.save("guestbook", {"OPENAI_API_KEY": "sk-1"})
    env_store.save("guestbook", {"OPENAI_API_KEY": "sk-1", "SECRET_KEY": "s"})
    assert env_store.load("guestbook") == {"OPENAI_API_KEY": "sk-1", "SECRET_KEY": "s"}

    verbs = [c[2] if c[2] != "versions" else f"versions {c[3]}" for c, _ in gc.calls]
    assert verbs.count("create") == 1 and verbs.count("versions add") == 1
    # values never reach the command line
    assert all("sk-1" not in " ".join(c) for c, _ in gc.calls)


def test_prefix_match_is_not_mistaken_for_the_app(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    gc = FakeGcloud({"cloudmorph-env-guestbook-v2": json.dumps({"A": "1"})})
    monkeypatch.setattr(env_store.subprocess, "run", gc)
    assert env_store.load("guestbook") == {}


def test_unset_project_stores_nothing(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setattr(env_store.subprocess, "run", lambda *a, **k: pytest.fail("gcloud called"))
    assert env_store.load("guestbook") == {}
    env_store.save("guestbook", {"A": "1"})


def test_lookup_errors_raise_instead_of_looking_like_a_first_deploy(monkeypatch):
    # {} would make the deploy regenerate SECRET_KEY and re-ask every value
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setattr(env_store.subprocess, "run", FakeGcloud(fail="list"))
    with pytest.raises(env_store.EnvStoreError):
        env_store.load("guestbook")
