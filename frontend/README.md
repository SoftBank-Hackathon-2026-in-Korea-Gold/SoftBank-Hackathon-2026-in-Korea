# frontend (owner: 유예인)

React + Tailwind 대시보드 (Vite 8 + React 19, lint: oxlint). 현재 `src/App.jsx`는 목업 단계 진행이며 백엔드 SSE 연동 전.

## Backend 연동 계약
- `POST /deploy` `{ "source": "...", "targets": ["local", "cloudrun"] }` → `{ "deployment_id" }`
- `GET /deploy/{deployment_id}/events` → SSE (`EventSource`)
  - `stage` → 파이프라인 애니메이션 단계
  - `heal_diff` → **AI 수정 전/후 비교 카드** (`before`, `after`, `diff`, `rationale`, `category`)
  - `done` → Live URL 버튼
- 상세 스키마: [`../docs/interfaces.md`](../docs/interfaces.md)

## 로컬 실행
Node `^20.19` 또는 `>=22.12` 필요 (Vite 8 요구사항). 패키지 매니저는 npm (`package-lock.json`).

```bash
cd frontend
npm ci            # 의존성 설치 (lockfile 기준)
npm run dev       # 개발 서버 http://localhost:5173
npm run build     # 프로덕션 빌드 → dist/
npm run lint      # oxlint
```

Tailwind는 현재 `index.html`의 CDN 스크립트(`cdn.tailwindcss.com`)로 로드합니다 — 시연용이며 정식 설치는 후속 작업.
