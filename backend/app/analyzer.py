"""Code analyzer (owner: 이요환).

Scan the user's source, decide the deploy target, and generate the initial Dockerfile.
Contract: see app/schemas.py::AnalysisResult and docs/interfaces.md.
"""

from __future__ import annotations

from app.schemas import AnalysisResult


def analyze(src_dir: str) -> AnalysisResult:
    """Analyze `src_dir` and return the deploy decision + initial Dockerfile.

    TODO(이요환):
    - detect language/framework (requirements.txt, package.json, pom.xml ...)
    - detect hard-coded ports / localhost binding (domain gap)
    - choose target: "cloudrun" vs "local"
    - generate initial Dockerfile (listen on 0.0.0.0:$PORT)
    """
    raise NotImplementedError("analyzer.analyze is not implemented yet (owner: 이요환)")
