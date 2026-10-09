"""Code analyzer (owner: 이요환).

Scan the user's source, decide the deploy target, and generate the initial Dockerfile.
Contract: see app/schemas.py::AnalysisResult and docs/interfaces.md.

찾고 판정하는 일은 AI 검사관이 한다 (app/analysis/inspectors.py, OPENAI_API_KEY). 키가 없으면 패턴 규칙으로 판정한다.
파이프라인 안에서는 사용자에게 물을 수 없어서 안전한 쪽으로 정하고, 물어봐야 했던 것은 notes에 적는다.
- 치명적 위험(데이터 손실 계열)은 AI가 괜찮다고 해도 동의를 받지 못했으니 위험으로 둔다
- 큰 코드 수정이 필요한 유형은 고르지 않고, 코드는 고치지 않는다. 필요한 수정은 제안으로 적는다
타깃: 서버리스 컨테이너(유형 2)나 정적 사이트(유형 5)면 cloudrun, 아니면 local(Docker).
"""

from __future__ import annotations

from pathlib import Path

from app.analysis import graph, inspectors, llm, offers
from app.analysis.dockerfile import render
from app.analysis.rules import TYPES
from app.analysis.signals import read_text
from app.analysis.spec import App
from app.schemas import AnalysisResult

# 고를 유형 순서와 그 타깃
TARGETS = {2: "cloudrun", 5: "cloudrun", 4: "local"}


def analyze(src_dir: str) -> AnalysisResult:
    """Analyze `src_dir` and return the deploy decision + initial Dockerfile."""
    model = llm.load_model(src_dir)
    return analyze_with(src_dir, inspectors.make_inspector(model) if model else None)


def analyze_with(src_dir: str, inspect) -> AnalysisResult:
    """검사관(inspect)을 바꿔 끼울 수 있게 나눴다. 테스트는 가짜 검사관을 넣는다."""
    unconsented = []

    def answer(payload):  # 물어볼 사람이 없으니 안전한 쪽으로 답한다
        if payload["kind"] == "choose":
            open_types = {o["type"] for o in payload["options"] if not o["blocked"]}
            return next((t for t in TARGETS if t in open_types), None)
        if payload["kind"] == "consent":
            unconsented.extend(payload["items"])
        return []  # 동의·큰 수정 승인·큰 diff 확인: 모두 하지 않는다

    out = graph.run(src_dir, inspect=inspect, ask=answer)
    result = out["result"]
    # 유형 4(VM)는 Windows VM이면 되지만 local은 리눅스 Docker다
    if out["signals"]["os_windows"]["value"] == "yes":
        raise ValueError(
            "local·cloudrun 어디에도 배포할 수 없습니다. Windows 전용 코드는 리눅스 컨테이너에서 돌지 않습니다 "
            "(Windows VM이 필요합니다)"
        )
    if not result.get("spec"):
        reasons = [f"{TYPES[t]}: {w['reason']}" for t in TARGETS for w in result["removed"].get(t, [])]
        raise ValueError("local·cloudrun 어디에도 배포할 수 없습니다. " + "; ".join(dict.fromkeys(reasons)))

    t, spec = result["target"], result["spec"]
    app = App(**spec["app"])
    port = app.port if app.port and not app.port_env else 8080  # $PORT를 못 받는 앱은 고정 포트로 듣는다
    existing = Path(src_dir) / "Dockerfile"
    dockerfile = read_text(existing) if existing.is_file() else render(app, port)
    return AnalysisResult(
        target=TARGETS[t],
        language=app.language or "unknown",
        framework=app.framework,
        port=port,
        entrypoint=app.start,
        dockerfile=dockerfile,
        notes=_notes(out, t, spec, unconsented, existing.is_file(), inspect is not None),
    )


def _notes(out: dict, t: int, spec: dict, unconsented: list, own_dockerfile: bool, ai: bool) -> list[str]:
    result, app = out["result"], spec["app"]
    notes = []
    if t == 4:
        why = [w["reason"] for w in result["removed"].get(2, [])] or ["큰 코드 수정이 필요합니다"]
        notes.append(
            f"서버리스 컨테이너로는 갈 수 없어 local(Docker)에 배포합니다: {'; '.join(dict.fromkeys(why))}"
        )
    else:
        notes.append(f"{TYPES[t]}로 판단해 cloudrun에 배포합니다.")
    notes += [
        f"동의를 받지 못해 위험으로 두었습니다: {it['title']} — {it['risk']}. AI 판단: {it['ai_reason']} "
        "(사용자가 동의하면 풀 수 있습니다)"
        for it in unconsented
    ]
    notes.append(
        "저장소에 있는 Dockerfile을 그대로 씁니다."
        if own_dockerfile
        else f"Dockerfile을 만들었습니다. 시작 명령: {app['start'] or '-'}"
    )
    notes += [
        f"외부 저장소가 필요합니다: {r['kind']} ({', '.join(r['env'].values()) or '환경변수 못 찾음'}). "
        "배포 전에 만들어 연결해야 합니다."
        for r in spec["resources"]
    ]
    if need := [e for e in spec["env"] if e["required"]]:
        notes.append(
            "넣어야 하는 환경변수: "
            + ", ".join(e["name"] + (" (무작위 값 가능)" if e["generate"] else "") for e in need)
        )
    c = spec["constraints"]
    if c["single_instance"]:
        notes.append(
            "인스턴스를 1대로 고정해야 합니다 (메모리 상태·로컬 파일·프로세스 안 주기 작업이 있습니다)."
        )
    if c["persistent_paths"]:
        notes.append(f"재시작해도 남아야 하는 경로: {', '.join(c['persistent_paths'])}")
    notes += [f"수정 제안: {offers.BY_ID[i]['question']}" for i in result["remaining"]]
    notes += [f"배포 전에 해결할 것: {b}" for b in spec["blockers"]]
    notes += [
        f"기타 위험: {r['title']} — {r['reason']}"
        for r in ((out.get("reports") or {}).get("risks") or {}).get("items", [])
    ]
    if not ai:
        notes.append("OPENAI_API_KEY가 없어 패턴 규칙으로만 판정했습니다 (정확도가 낮습니다).")
    elif out.get("llm_errors"):
        notes.append(
            f"AI 검사관 일부가 실패해 그 항목은 패턴 규칙으로 판정했습니다: {'; '.join(out['llm_errors'])}"
        )
    return notes
