"""Open a GitHub pull request with the healer's source fix, so the repo catches up with what was deployed.

    base branch HEAD ──(tree with the edited files)──▶ commit ──▶ branch cloudmorph/heal-<sha> ──▶ PR

Needs GITHUB_TOKEN with write access (contents + pull requests) to the source repository.
Uses the REST API directly: the deploy copy has no .git, and one tree + one commit keeps it to one commit.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

API = "https://api.github.com"
TIMEOUT_S = 20


def repo_slug(clone_url: str) -> str:
    """'https://github.com/owner/repo.git' -> 'owner/repo'."""
    parts = urlparse(clone_url).path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"not a GitHub repository URL: {clone_url}")
    return "/".join(parts)


def _call(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def open_pr(
    clone_url: str, ref: str | None, src_dir: str, paths: list[str], rationale: str, diff: str
) -> str:
    """Commit `paths` (read from src_dir) on top of `ref` (default branch if None) and open a PR. Returns its URL."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set")
    repo = repo_slug(clone_url)
    base = ref or _call("GET", f"/repos/{repo}", token)["default_branch"]
    head = _call("GET", f"/repos/{repo}/git/ref/heads/{quote(base)}", token)["object"]["sha"]
    base_tree = _call("GET", f"/repos/{repo}/git/commits/{head}", token)["tree"]["sha"]
    files = [
        {"path": p, "mode": "100644", "type": "blob", "content": (Path(src_dir) / p).read_text()}
        for p in paths
    ]
    tree = _call("POST", f"/repos/{repo}/git/trees", token, {"base_tree": base_tree, "tree": files})
    title = f"fix: {rationale}"[:72]
    commit = _call(
        "POST",
        f"/repos/{repo}/git/commits",
        token,
        {"message": f"{title}\n\n{rationale}", "tree": tree["sha"], "parents": [head]},
    )
    branch = f"cloudmorph/heal-{commit['sha'][:7]}"
    _call("POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    body = (
        "CloudMorph's self-healing agent fixed this to get the deploy working.\n\n"
        f"**Why:** {rationale}\n\n```diff\n{diff}```\n"
    )
    pr = _call(
        "POST", f"/repos/{repo}/pulls", token, {"title": title, "head": branch, "base": base, "body": body}
    )
    return pr["html_url"]
