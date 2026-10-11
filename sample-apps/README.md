# sample-apps (owner: 전동훈)

시연용 배포 대상 앱. 앱 퀄리티는 무관 — 배포 플랫폼 시연이 목적. `healthy-node`(Express, `/`만)와 `healthy-fastapi`(FastAPI, `/api/items`만)를 제외하면 모두 Flask + gunicorn이고 `/`와 `/health` 두 경로를 가진다.

| Dir | 목적 | 의도된 결함 | deployer가 돌려주는 실패 | healer 결과 |
|---|---|---|---|---|
| `healthy/` | 정상 배포 경로 시연 | 없음 | — (local ≈ 35초, cloudrun ≈ 30초) | — |
| `healthy-node/` | `/health` 없는 Express 앱 (`/`만) | 없음 | 성공: `/`가 200이면 정상으로 판정 | — |
| `healthy-fastapi/` | `/health`도 `/`도 없는 FastAPI (`/api/items`만) | 없음 | 성공: 404도 "서버 살아 있음"으로 판정 (5xx만 실패) | — |
| `guestbook/` | **DB가 있는 앱** (방명록). `DATABASE_URL` 있으면 Postgres, 없으면 SQLite | 없음 | local/node: Postgres 사이드카 자동, cloudrun: Cloud SQL 앱별 DB 자동 | — |
| `broken/` | 자가치유 2단계 시연 | flask가 requirements에 없음 + `127.0.0.1:5000` 하드코딩 | 1차 `ModuleNotFoundError` → 패치 후 2차 포트 바인딩 → 패치 후 성공 | **healed** (규칙 2회: pip install → 포트 rebind) |
| `broken-requirements/` | 빌드 실패 | `flask==99.99.99` | stage=build, pip 오류 원문 | **healed** (규칙: 존재하지 않는 버전 고정 해제) |
| `broken-import/` | 기동 크래시 | `from flask import` 삭제 | stage=verify, 컨테이너 exited(3), `NameError` | **stopped** (repo agent 비활성) — 앱 코드 오류, 재배포 없음 + 소스 수정 제안 diff(LLM, 적용 안 함) |
| `broken-port/` | 포트 불일치 | PORT 무시하고 5000 고정 | stage=verify, 컨테이너 running, "failed to start and listen on the port" 진단 | **healed** (규칙: 포트 rebind) |
| `broken-health/` | 요청 시 오류 | `/health`가 예외 | stage=verify, HTTP 500, 요청 Traceback | **stopped** (repo agent 비활성) — 앱 코드 오류(요청 시 5xx), Traceback 요약만 |
| `broken-bind-chain/` | 기동 체인 속 포트 불일치 | `sh -c "python prestart.py && exec gunicorn -b :5000 ..."` (IP 생략 bind) + `EXPOSE 5000` | stage=verify, 컨테이너 running, "failed to start and listen on the port" 진단, 로그 `Listening at: http://0.0.0.0:5000` | **healed** (규칙: 체인 안의 `-b :5000` → `-b 0.0.0.0:8080` rebind) |
| `broken-native-lib/` | 네이티브 라이브러리 누락 | 멀티스테이지: builder에서 lxml 소스 빌드, 최종 slim 이미지에 `libxslt1.1` 없음 | stage=verify, 컨테이너 exited(3), `ImportError: libxslt.so.1: cannot open shared object file` | **healed** (규칙: 지원되는 Debian 이미지의 런타임 스테이지에 `apt-get install libxslt1.1`) |
| `broken-permission/` | 비root 사용자 쓰기 권한 | `USER appuser`인데 import 시 `uploads/` 생성 (`/app`은 root 소유) | stage=verify, 컨테이너 exited(3), `PermissionError: [Errno 13] Permission denied: 'uploads'` | **healed** (규칙: 비root `USER`로 전환하기 전에 root로 `/app/uploads`만 생성·소유권 변경. WORKDIR 밖·시스템 경로는 규칙 대상에서 제외) |
| `broken-db-startup/` | 기동 전 DB 대기 | `migrate.py`가 `localhost:5432`에 5회 접속 시도 후 종료 | stage=verify, 컨테이너 exited(1), `Connection refused` 반복 + `ConnectionRefusedError`. verify 6초면 running + 재시도 로그만 (`.local.slow`) | **stopped** — 외부 서비스 접속 불가: DB/네트워크 설정(host·port·자격 증명) 안내, 재배포 없음 |
| `broken-missing-env/` | 필수 환경변수 누락 | production 프로필이 `os.environ["SECRET_KEY"]`, `os.environ["DB_HOST"]`를 요구 | stage=verify, 컨테이너 exited(3), `<frozen os>` `__getitem__` 뒤 `KeyError: 'SECRET_KEY'` | **stopped** — 런타임 설정 누락: 변수 이름(`SECRET_KEY, DB_HOST`)만 안내, 값은 만들지 않음 |
| `broken-quiet-crash/` | 로그 없는 네이티브 크래시 | `warmup.py`가 ctypes로 SIGSEGV, 버퍼에 남은 print 출력은 유실 | stage=verify, 컨테이너 exited(139), 런타임 로그는 `Segmentation fault` 한 줄 | **stopped** — 조용한 크래시: exit_code=139(SIGSEGV) + `PYTHONUNBUFFERED=1`/`PYTHONFAULTHANDLER=1` 안내 |

실측 stderr 원문은 [`backend/tests/fixtures/`](../backend/tests/fixtures/)에 있다 (healer `ERROR_PATTERNS` 보강용).
표의 마지막 6종(이슈 #7)은 local deployer로만 실측했다. Cloud Run 검증은 하지 않았다(선택 사항). healed 3종은 healer가 만든
Dockerfile을 실제 deployer로 재배포해 `/health`·`/` 응답까지 확인했다. stopped 3종은 repo agent(`ANTHROPIC_API_KEY`)가
켜져 있어도 같은 결과다(비밀값·DB를 지어내지 않도록 LLM 전에 멈춘다).

```bash
cd backend
uv run python -m app.deployer ../sample-apps/healthy local            # 터널 포함
uv run python -m app.deployer ../sample-apps/broken local --no-tunnel
uv run python -m app.deployer cleanup ../sample-apps/healthy          # 컨테이너·터널 정리

# 6종 자가치유/조기중단 검증 (Docker E2E 재현 러너)
uv run --project backend python ../scripts/verify-sample-apps.py
```
