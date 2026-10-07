# CloudMorph — One Action, Infinite Clouds

> 사용자 코드를 정적 분석해 최적의 배포 환경을 결정하고, 배포 실패 시 AI가 스스로 에러를 패치하는 **자가치유(Self-Healing)** 기능을 갖춘, 로컬(Docker)과 클라우드(Google Cloud Run) 배포가 자유로운 시각적 원터치 배포 플랫폼

SoftBank Hackathon 2026 in Korea · Term 2 · Team Gold

## Pipeline

```
[analyzer] 코드 분석·타깃 결정·Dockerfile 생성
    → [deployer] Docker / Cloud Run 배포
        → 실패 시 [healer] stderr 분류 → Dockerfile 패치 → 재배포 (최대 3회)
            → Live Public URL + AI 수정 전/후 리포트 (SSE → dashboard)
```

인터페이스 계약: [`docs/interfaces.md`](docs/interfaces.md) · 소스: [`backend/app/schemas.py`](backend/app/schemas.py)

## Repository layout

| Path | Owner | Role |
|---|---|---|
| `backend/app/main.py` | 백락원 | FastAPI 오케스트레이션, SSE 스트리밍 |
| `backend/app/analyzer.py` | 이요환 | 코드·의존성 분석, 배포 타깃 결정, 초기 Dockerfile |
| `backend/app/deployer.py` | 전동훈 | Docker 빌드/실행, Cloud Run 배포 |
| `backend/app/healer.py` | 박재현 | LangGraph 자가치유 루프, diff 리포트 |
| `backend/app/schemas.py` | 전원 (합의) | 모듈 간 공용 계약 |
| `frontend/` | 유예인 | React + Tailwind 대시보드 |
| `sample-apps/` | 전동훈 | 시연용 정상/고장 샘플 앱 |
| `docs/` | 박재현 | 인터페이스·설계 문서 (Notion 원본 미러) |

## Quick start (backend)

```bash
cd backend
uv sync
cp ../.env.example ../.env   # 키는 각자 로컬에만
uv run pytest -q             # healer 루프 테스트 (fake deployer)
uv run uvicorn app.main:app --reload
```

## Workflow

- `main` 직접 push 금지 → `feat/<module>-<topic>` 브랜치 + PR (리뷰 1인)
- `schemas.py` 변경은 팀 합의 후에만
- 키·서비스 계정 JSON은 절대 커밋하지 않기 (`.env`, GitHub Secrets)

## Next steps

- [ ] analyzer / deployer 구현 및 healer 연결 (10/8)
- [ ] 고장 샘플 앱 stderr 실측 → `healer.ERROR_PATTERNS` 보강
- [ ] 프론트 SSE 연동 + 수정 전/후 diff 카드
- [ ] 로컬 Public URL (Cloudflare Quick Tunnel), CI (pytest + ruff)
- [ ] Notion ADR: Cloud Run 선택, Docker, LangGraph 순환 루프, 규칙 우선·LLM 폴백
- [ ] (stretch) Agent Memory, Langfuse 트레이싱, AWS 2nd target, 롤백
