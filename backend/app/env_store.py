"""Per-app env values kept across deploys in GCP Secret Manager.

The user types an API key once. Redeploys and GitHub-push deploys read it back instead of asking again
(or shipping without it), and generated secrets (SECRET_KEY ...) stay the same from one deploy to the next.

    one secret per app: cloudmorph-env-<app>, payload = JSON {NAME: value}, each change = a new version

Env:
    GCP_PROJECT_ID   project that holds the secrets (unset -> nothing is stored; values last one deploy)

Values only travel through gcloud's stdin/stdout, never on a command line or into a log.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

PREFIX = "cloudmorph-env-"


class EnvStoreError(RuntimeError):
    """Secret Manager could not be read or written (gcloud missing, API disabled, no permission ...)."""


def enabled() -> bool:
    return bool(os.getenv("GCP_PROJECT_ID"))


def secret_id(app: str) -> str:
    return PREFIX + (re.sub(r"[^a-zA-Z0-9_-]", "-", app)[:200] or "app")


def _gcloud(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["gcloud", "secrets", *args, "--project", os.environ["GCP_PROJECT_ID"], "--quiet"]
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise EnvStoreError(f"gcloud secrets {args[0]}: {exc}") from None


def _exists(sid: str) -> bool:
    # rc=0 with no matching line means "not found", so a lookup error is never mistaken for it
    cp = _gcloud("list", f"--filter=name:{sid}", "--format=value(name)")
    if cp.returncode != 0:
        raise EnvStoreError(f"could not list secrets: {cp.stderr[-300:]}")
    return any(line.rsplit("/", 1)[-1] == sid for line in cp.stdout.split())


def load(app: str) -> dict[str, str]:
    """Values saved for `app`; {} on the first deploy or when GCP_PROJECT_ID is unset."""
    if not enabled():
        return {}
    sid = secret_id(app)
    if not _exists(sid):
        return {}
    cp = _gcloud("versions", "access", "latest", f"--secret={sid}")
    if cp.returncode != 0:
        raise EnvStoreError(f"could not read {sid}: {cp.stderr[-300:]}")
    try:
        values = json.loads(cp.stdout)
    except ValueError:
        raise EnvStoreError(f"{sid} does not hold a JSON object") from None
    if not isinstance(values, dict):
        raise EnvStoreError(f"{sid} does not hold a JSON object")
    return {str(k): str(v) for k, v in values.items()}


def save(app: str, values: dict[str, str]) -> None:
    """Store `values` as the latest version for `app` (creates the secret on first use)."""
    if not enabled():
        return
    sid = secret_id(app)
    payload = json.dumps(values, sort_keys=True)
    if _exists(sid):
        cp = _gcloud("versions", "add", sid, "--data-file=-", stdin=payload)
    else:
        cp = _gcloud("create", sid, "--replication-policy=automatic", "--data-file=-", stdin=payload)
    if cp.returncode != 0:
        raise EnvStoreError(f"could not save {sid}: {cp.stderr[-300:]}")
