# sample-apps (owner: 전동훈)

시연용 배포 대상 앱. 앱 퀄리티는 무관 — 배포 플랫폼 시연이 목적. `healthy-node`(Express, `/`만)와 `healthy-fastapi`(FastAPI, `/api/items`만)를 제외하면 모두 Flask + gunicorn이고 `/`와 `/health` 두 경로를 가진다.

| Dir | 목적 | 의도된 결함 | deployer가 돌려주는 실패 |
|---|---|---|---|
| `healthy/` | 정상 배포 경로 시연 | 없음 | — (local ≈ 35초, cloudrun ≈ 30초) |
| `healthy-node/` | `/health` 없는 Express 앱 (`/`만) | 없음 | 성공: `/`가 200이면 정상으로 판정 |
| `healthy-fastapi/` | `/health`도 `/`도 없는 FastAPI (`/api/items`만) | 없음 | 성공: 404도 "서버 살아 있음"으로 판정 (5xx만 실패) |
| `guestbook/` | **DB가 있는 앱** (방명록). `DATABASE_URL` 있으면 Postgres, 없으면 SQLite | 없음 | local/node: Postgres 사이드카 자동, cloudrun: Cloud SQL 앱별 DB 자동 |
| `broken/` | 자가치유 2단계 시연 | flask가 requirements에 없음 + `127.0.0.1:5000` 하드코딩 | 1차 `ModuleNotFoundError` → 패치 후 2차 포트 바인딩 → 패치 후 성공 |
| `broken-requirements/` | 빌드 실패 | `flask==99.99.99` | stage=build, pip 오류 원문 |
| `broken-import/` | 기동 크래시 | `from flask import` 삭제 | stage=verify, 컨테이너 exited(3), `NameError` |
| `broken-port/` | 포트 불일치 | PORT 무시하고 5000 고정 | stage=verify, 컨테이너 running, "failed to start and listen on the port" 진단 |
| `broken-health/` | 요청 시 오류 | `/health`가 예외 | stage=verify, HTTP 500, 요청 Traceback |

실측 stderr 원문은 [`backend/tests/fixtures/`](../backend/tests/fixtures/)에 있다 (healer `ERROR_PATTERNS` 보강용).

```bash
cd backend
uv run python -m app.deployer ../sample-apps/healthy local            # 터널 포함
uv run python -m app.deployer ../sample-apps/broken local --no-tunnel
uv run python -m app.deployer cleanup ../sample-apps/healthy          # 컨테이너·터널 정리
```
