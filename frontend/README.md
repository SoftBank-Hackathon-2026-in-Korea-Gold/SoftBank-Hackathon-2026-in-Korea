# frontend (owner: 유예인)

React + Tailwind 대시보드. 아직 초기화 전 — 스택/툴(vite 등) 선택은 담당자 결정.

## Backend 연동 계약
- `POST /deploy` `{ "source": "...", "targets": ["local", "cloudrun"] }` → `{ "deployment_id" }`
- `GET /deploy/{deployment_id}/events` → SSE (`EventSource`)
  - `stage` → 파이프라인 애니메이션 단계
  - `heal_diff` → **AI 수정 전/후 비교 카드** (`before`, `after`, `diff`, `rationale`, `category`)
  - `done` → Live URL 버튼
- 상세 스키마: [`../docs/interfaces.md`](../docs/interfaces.md)
