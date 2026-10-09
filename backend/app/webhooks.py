"""GitHub push -> redeploy (CD).

    GitHub ──POST /webhook/github (X-Hub-Signature-256)──▶ verify HMAC ──▶ parse push ──▶ launch deployment
                                                                                   (same app name every time
                                                                                    => same Cloud Run service /
                                                                                       node app gets the new version)

Env:
    CLOUDMORPH_WEBHOOK_SECRET   shared secret configured on the GitHub webhook (required; unset -> endpoint disabled)
    CLOUDMORPH_CD_TARGETS       comma list, default "local,cloudrun"
    CLOUDMORPH_CD_BRANCHES      comma list of branches to deploy; default: the repository's default branch only
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """GitHub sends `X-Hub-Signature-256: sha256=<hex hmac of the raw body>`."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256=") :])


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40]
    return s or "app"


@dataclass
class PushInfo:
    repo_full_name: str
    clone_url: str
    branch: str
    default_branch: str
    sha: str
    message: str
    pusher: str
    deleted: bool

    @property
    def app_name(self) -> str:
        return slug(self.repo_full_name.split("/")[-1])


def parse_push(payload: dict) -> PushInfo | None:
    """Extract what we need from a GitHub `push` event; None when it is not a branch push."""
    ref = payload.get("ref") or ""
    if not ref.startswith("refs/heads/"):
        return None  # tags etc.
    repo = payload.get("repository") or {}
    head = payload.get("head_commit") or {}
    return PushInfo(
        repo_full_name=repo.get("full_name") or repo.get("name") or "unknown/unknown",
        clone_url=repo.get("clone_url") or repo.get("html_url", "") + ".git",
        branch=ref[len("refs/heads/") :],
        default_branch=repo.get("default_branch") or "main",
        sha=(payload.get("after") or head.get("id") or "")[:12],
        message=(head.get("message") or "").splitlines()[0][:120] if head.get("message") else "",
        pusher=(payload.get("pusher") or {}).get("name") or (payload.get("sender") or {}).get("login") or "?",
        deleted=bool(payload.get("deleted")),
    )


def cd_targets() -> list[str]:
    raw = os.getenv("CLOUDMORPH_CD_TARGETS", "local,cloudrun")
    return [t.strip() for t in raw.split(",") if t.strip()]


def branch_allowed(info: PushInfo) -> bool:
    raw = os.getenv("CLOUDMORPH_CD_BRANCHES", "").strip()
    allowed = {b.strip() for b in raw.split(",") if b.strip()} or {info.default_branch}
    return info.branch in allowed
