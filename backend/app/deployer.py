"""Deployer (owner: 전동훈).

Build and run the container locally (Docker) or on Google Cloud Run.
Signature matches healer.DeployFn so healer can call it for redeploys.
Contract: see app/schemas.py::DeployResult and docs/interfaces.md.
"""

from __future__ import annotations

from app.schemas import DeployResult, DeployTarget


def deploy(src_dir: str, dockerfile: str, target: DeployTarget) -> DeployResult:
    """Write `dockerfile` into `src_dir`, build and run on `target`.

    Must NOT raise on build/run failure: return DeployResult(success=False, stderr=...)
    so that healer can read the stderr.

    TODO(전동훈):
    - local:    subprocess `docker build` + `docker run -p` -> public URL (e.g. tunnel)
    - cloudrun: subprocess `gcloud run deploy --source ... --region $GCP_REGION` -> *.run.app URL
    - capture stdout/stderr, exit_code, duration_sec; add a timeout
    """
    raise NotImplementedError("deployer.deploy is not implemented yet (owner: 전동훈)")
