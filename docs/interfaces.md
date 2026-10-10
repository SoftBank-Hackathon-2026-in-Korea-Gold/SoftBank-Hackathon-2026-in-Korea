# Module Interface Contracts (v0.1 — 10/7 3차 미팅 합의 대상)

> Source of truth: [`backend/app/schemas.py`](../backend/app/schemas.py). 변경 시 팀 합의 후 이 문서와 함께 수정.

## Pipeline

```
POST /deploy ─▶ analyzer.analyze(src_dir) ──AnalysisResult──▶ deployer.deploy(src_dir, dockerfile, target)
                                                                    │
                                     success ◀──DeployResult────────┤
                                                                    │ failure
                                                                    ▼
                  healer.heal(src_dir, target, failed, dockerfile, deploy_fn=deployer.deploy)
                    classify ─▶ patch ─▶ redeploy ─┐   (retry_count ≤ MAX_RETRIES = 3)
                       ▲───────────────────────────┘
                                                                    ▼
                                                               HealReport ──▶ SSE ──▶ frontend
```

## Functions

| Module | Owner | Signature | Notes |
|---|---|---|---|
| `analyzer.py` | 이요환 | `analyze(src_dir: str) -> AnalysisResult` | 초기 Dockerfile은 `0.0.0.0:$PORT` 바인딩 |
| `deployer.py` | 전동훈 | `deploy(src_dir: str, dockerfile: str, target: "local"\|"cloudrun") -> DeployResult` | **실패 시 raise 금지** → `success=False, stderr=...` 반환. `stderr`에는 CLI 출력뿐 아니라 **컨테이너 런타임 로그(`docker logs` / Cloud Run revision logs)** 포함 필수 |
| `healer.py` | 박재현 | `heal(src_dir, target, failed: DeployResult, dockerfile, deploy_fn, llm_patch_fn=None, emit=None, suggest_fn=None) -> HealReport` | 재배포 루프를 healer가 소유. 규칙 패치 우선, LLM 폴백(Claude / OpenAI / 로컬 OpenAI 호환 서버 중 `.env`에 채워진 것, 실패 시 다음 제공자). **Dockerfile만 자동 수정**: verify 단계에서 앱 코드가 던진 예외(Traceback의 마지막 사용자 프레임)는 재배포·LLM 없이 즉시 중단하고 `summary`가 `Not auto-healable: application code error`로 시작. import 시점 크래시면 LLM 소스 수정 제안 diff를 summary와 `log` 이벤트(`payload.kind=source_suggestion`)로 리포트만 함 (적용 안 함) |
| `main.py` | 백락원 | `POST /deploy`, `GET /deploy/{id}/events` (SSE), `POST /deploy/{id}/env`, `POST /deploy/{id}/cancel`, `POST /projects/{name}/stop` | 파이프라인 오케스트레이션 · 진행 중 배포 중단 · 떠 있는 앱 중지. `ask_env: true`(대시보드)면 분석 뒤 `required_env`를 `input_required`로 묻고 `POST /deploy/{id}/env {values}`를 기다린다(15분). 값은 이벤트로 내보내지 않으며, healer 재배포에도 같은 값이 간다. `name`을 준 배포는 값을 `env_store`(Secret Manager `cloudmorph-env-<name>-<소스 해시>`, `GCP_PROJECT_ID` 없으면 저장 안 함)에 이름과 소스(저장소 URL 또는 경로) 단위로 저장하고, 다음 배포에서는 저장된 값을 먼저 쓴다: 대시보드는 빠진 이름만 묻고, `ask_env` 없는 호출(GitHub push)은 저장된 값으로 배포한다. 무작위로 만든 값도 저장해 배포마다 바뀌지 않는다. 같은 이름이라도 다른 저장소에서 온 배포는 저장된 값을 받지 못하고 처음부터 다시 묻는다 |

## Data types

**AnalysisResult** — `target`, `service_models[]` (`caas|paas|faas|iaas`, 추천 순), `language`, `framework?`, `port` (default 8080), `entrypoint?`, `dockerfile`, `notes[]`, `required_env[EnvRequirement]`

**EnvRequirement** — `name`, `scope` (`build|runtime`), `secret`, `generate` (비우면 무작위 값), `reason`, `evidence["path:line"]`, `resource` (`postgres|mysql|redis|s3`: 외부 저장소 접속 정보 — 사용자가 자기 서버를 넣을 수 있어 항상 묻는다), `auto` (비우면 deployer가 만들어 연결: 지금은 Postgres가 감지된 앱의 `DATABASE_URL`만. main.py가 표시). 사용자가 `DATABASE_URL`을 넣으면 deployer는 Postgres/Cloud SQL을 만들지 않는다. deployer 전달: build+secret → `docker build --secret id=NAME,env=NAME` (BuildKit 필요, 값은 명령줄·레이어에 안 남음), build → `--build-arg`, runtime → 컨테이너 env

**DeployResult** — `success`, `target`, `exit_code`, `stdout`, `stderr`, `url?`, `duration_sec?`

**HealState** (LangGraph) — `src_dir`, `target`, `error_log`, `error_category`, `current_dockerfile`, `diff`, `history[PatchRecord]`, `retry_count`, `status` (`healing|healed|gave_up`), `last_result`

**ErrorCategory** — `port_binding` · `missing_dependency` · `bad_entrypoint` · `build_failure` · `unknown`

**PatchRecord** — `attempt`, `category`, `error_excerpt`, `before`, `after`, `diff` (unified), `rationale`, `source` (`rule|llm`)

**HealReport** — `success`, `target`, `attempts`, `records[PatchRecord]`, `final_dockerfile`, `final_url?`, `summary`

## SSE events (main.py → frontend)

`event:` = `type`, `data:` = `PipelineEvent` JSON

```json
{ "type": "stage | log | heal_diff | input_required | done | error",
  "stage": "analyze | deploy | heal | redeploy | null",
  "payload": { },
  "ts": 1791380000.123 }
```

| type | payload |
|---|---|
| `stage` | `{ "target"?, "attempt"?, "stderr"? }` — 진행 단계 표시 |
| `log` | `{ "line": "..." }` — 빌드/배포 로그 스트리밍 |
| `heal_diff` | `PatchRecord` — **AI 수정 전/후 비교 카드** |
| `input_required` | `{ "items": [EnvRequirement], "timeout_sec" }` — 배포 전에 사용자에게 받을 값 (`ask_env`일 때만) |
| `done` | `{ "target", "url" }` 또는 `HealReport` |
| `error` | `{ "message" }` 또는 `HealReport` (gave_up) |

## Open questions for 3차 미팅
1. `src_dir` 전달 방식: 업로드 zip → 서버 임시 디렉토리 경로로 통일?
2. 로컬 배포의 Public URL 발급 방식 (Cloudflare Quick Tunnel 등) — deployer 담당
3. 멀티 타깃(local + cloudrun) 순차 vs 병렬
4. AWS 2nd target 여부 (클라우드 활용 30점)
5. **deployer stderr 범위**: Cloud Run은 크래시 traceback이 revision logs에만 남음 → `gcloud logging read` / `docker logs`로 런타임 로그를 `stderr`에 합칠 것 (안 하면 패키지 누락이 포트 에러로 오분류되어 데모 실패)
6. `main.py` CORS 설정(프론트 dev 서버 origin) — 백락원
