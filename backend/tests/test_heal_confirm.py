"""Source fix -> PR -> ask the user (main.source_fix_confirm, POST /deploy/{id}/answer) and fixpr against a fake GitHub."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import fixpr, main
from app.healer import RepoFix

FIX = RepoFix("FROM x\n", "--- app.py (before)\n+++ app.py (after)\n", "Flask was not imported", ("app.py",))
REPO = "https://github.com/acme/shop.git"


def _confirm(monkeypatch, source=REPO, trigger="api", pr=lambda *a: "https://github.com/acme/shop/pull/7"):
    monkeypatch.setattr(fixpr, "open_pr", pr)
    job, events = main.Job(), []
    return job, events, main.source_fix_confirm(job, events.append, source, None, trigger)


def _answer_when_asked(job, choice):
    def run():
        while not job.questions:
            time.sleep(0.01)
        (qid,) = job.questions
        job.questions.pop(qid).put(choice)

    threading.Thread(target=run, daemon=True).start()


def _payloads(events, kind):
    return [e.payload for e in events if e.payload.get("kind") == kind]


def test_stop_keeps_the_pr_and_tells_the_user_to_merge(monkeypatch):
    job, events, confirm = _confirm(monkeypatch)
    _answer_when_asked(job, "stop")

    stop = confirm("/src", FIX)

    (question,) = _payloads(events, "question")
    assert question["ask"] and question["pr_url"] == "https://github.com/acme/shop/pull/7"
    assert question["diff"] == FIX.source_diff and question["rationale"] == FIX.rationale
    assert _payloads(events, "answer")[0]["choice"] == "stop"
    assert "pull/7" in stop and "Merge it" in stop
    assert job.questions == {}


def test_continue_redeploys_and_later_targets_reuse_the_answer(monkeypatch):
    opened = []
    job, events, confirm = _confirm(monkeypatch, pr=lambda *a: opened.append(a) or "https://x/pull/1")
    _answer_when_asked(job, "continue")

    assert confirm("/src-local", FIX) is None
    assert confirm("/src-cloudrun", FIX) is None  # second target: no second PR, no second question
    assert len(opened) == 1 and len(_payloads(events, "question")) == 1
    assert opened[0][1:4] == (None, "/src-local", ["app.py"])


def test_github_push_stops_without_asking(monkeypatch):
    job, events, confirm = _confirm(monkeypatch, trigger="github-push")
    stop = confirm("/src", FIX)
    assert not _payloads(events, "question")[0]["ask"] and job.questions == {}
    assert _payloads(events, "answer")[0]["reason"] == "github-push"
    assert "pull/7" in stop


def test_no_answer_times_out_to_stop(monkeypatch):
    monkeypatch.setattr(main, "ANSWER_TIMEOUT_S", 0.05)
    _job, events, confirm = _confirm(monkeypatch)
    assert confirm("/src", FIX)
    assert _payloads(events, "answer")[0] == {
        "kind": "answer",
        "question_id": _payloads(events, "question")[0]["question_id"],
        "choice": "stop",
        "reason": "timeout",
    }


def test_local_source_asks_without_a_pr(monkeypatch):
    job, events, confirm = _confirm(monkeypatch, source="/home/me/app", pr=lambda *a: pytest.fail("no PR"))
    _answer_when_asked(job, "stop")
    stop = confirm("/src", FIX)
    assert _payloads(events, "question")[0]["pr_url"] is None
    assert "Apply the diff" in stop


def test_pr_failure_still_asks(monkeypatch):
    def broken(*_):
        raise RuntimeError("GITHUB_TOKEN is not set")

    job, events, confirm = _confirm(monkeypatch, pr=broken)
    _answer_when_asked(job, "continue")
    assert confirm("/src", FIX) is None
    assert "GITHUB_TOKEN" in _payloads(events, "question")[0]["pr_error"]


def test_answer_endpoint_delivers_the_choice_once(monkeypatch):
    monkeypatch.delenv("CLOUDMORPH_API_TOKEN", raising=False)
    client = TestClient(main.create_app(), client=("127.0.0.1", 50000))
    job = main.Job()
    answers = main.SimpleQueue()
    job.questions["q1"] = answers
    main._jobs["dep1"] = job
    try:
        body = {"question_id": "q1", "choice": "continue"}
        assert client.post("/deploy/dep1/answer", json=body).status_code == 200
        assert answers.get_nowait() == "continue"
        assert client.post("/deploy/dep1/answer", json=body).status_code == 409
        assert client.post("/deploy/nope/answer", json=body).status_code == 404
        bad = {"question_id": "q1", "choice": "maybe"}
        assert client.post("/deploy/dep1/answer", json=bad).status_code == 422
    finally:
        main._jobs.pop("dep1", None)


def test_open_pr_commits_the_files_on_a_new_branch(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text("from flask import Flask\n")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    calls = []
    replies = {
        "/repos/acme/shop": {"default_branch": "main"},
        "/repos/acme/shop/git/ref/heads/main": {"object": {"sha": "base1"}},
        "/repos/acme/shop/git/commits/base1": {"tree": {"sha": "tree0"}},
        "/repos/acme/shop/git/trees": {"sha": "tree1"},
        "/repos/acme/shop/git/commits": {"sha": "abc1234def"},
        "/repos/acme/shop/git/refs": {},
        "/repos/acme/shop/pulls": {"html_url": "https://github.com/acme/shop/pull/9"},
    }

    def fake_call(method, path, token, body=None):
        calls.append((method, path, body))
        return replies[path]

    monkeypatch.setattr(fixpr, "_call", fake_call)

    url = fixpr.open_pr(REPO, None, str(tmp_path), ["app.py"], "Flask was not imported", "+from flask")

    assert url == "https://github.com/acme/shop/pull/9"
    bodies = {path: body for _, path, body in calls}
    assert bodies["/repos/acme/shop/git/trees"]["tree"][0]["content"] == "from flask import Flask\n"
    assert bodies["/repos/acme/shop/git/commits"]["parents"] == ["base1"]
    assert bodies["/repos/acme/shop/git/refs"] == {
        "ref": "refs/heads/cloudmorph/heal-abc1234",
        "sha": "abc1234def",
    }
    pull = bodies["/repos/acme/shop/pulls"]
    assert (
        pull["head"] == "cloudmorph/heal-abc1234" and pull["base"] == "main" and "+from flask" in pull["body"]
    )


def test_open_pr_needs_a_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        fixpr.open_pr(REPO, None, "/src", ["app.py"], "r", "d")


@pytest.mark.parametrize(
    ("url", "slug"),
    [("https://github.com/acme/shop.git", "acme/shop"), ("https://github.com/acme/shop", "acme/shop")],
)
def test_repo_slug(url, slug):
    assert fixpr.repo_slug(url) == slug
