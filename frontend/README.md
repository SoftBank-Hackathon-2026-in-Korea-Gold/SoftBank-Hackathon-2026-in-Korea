# frontend (owner: 유예인)

React + Tailwind 대시보드 (Vite 8 + React 19, lint: oxlint). 백엔드 SSE로 배포 상태를 표시하며, 기본 화면은 한 뷰포트에 맞춘 3D 무대입니다.

배포 여정·노드 풀 탭으로 장면을 전환하고, 상단에서 배포 상태를, 하단에서 핵심 로그·공개 URL을 확인합니다. 새 배포·상세·프로젝트·노드·설정은 우측 패널(모바일에서는 하단 패널)에 있습니다. 필요한 값이나 자가치유 선택 요청이 오면 상세 패널이 자동으로 열립니다. 패널을 닫아도 입력 내용은 유지됩니다.

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
npm test          # vitest (src/lib 순수 로직 테스트)
npx playwright install chromium # 최초 브라우저 설치
npm run test:e2e   # 화면 크기, 패널, SSE, 축하, WebGL fallback 회귀 검사
```

3D 시각화(배포 여정 · 노드 풀 3D · 자가치유 축하) 설치와 연동: [`../docs/3d-visuals.md`](../docs/3d-visuals.md)

Tailwind는 현재 `index.html`의 CDN 스크립트(`cdn.tailwindcss.com`)로 로드합니다 — 시연용이며 정식 설치는 후속 작업.
