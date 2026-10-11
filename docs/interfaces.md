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
| `deployer.py` | 전동훈 | `deploy(src_dir: str, dockerfile: str, target: DeployTarget, **overrides) -> DeployResult` | `local`·`cloudrun`·`node`·`function` 지원 (`agent`는 오케스트레이터 dispatch). **실패 시 raise 금지** → `success=False, stderr=...` 반환. `stderr`에는 CLI 출력뿐 아니라 **컨테이너 런타임 로그(`docker logs` / Cloud Run revision logs)** 포함 필수 |
| `healer.py` | 박재현 | `heal(src_dir, target, failed: DeployResult, dockerfile, deploy_fn, llm_patch_fn=None, emit=None, suggest_fn=None, repair_fn=None, confirm_fn=None) -> HealReport` | 재배포 루프를 healer가 소유. 규칙 패치 우선, LLM 폴백(Claude / OpenAI / 로컬 OpenAI 호환 서버 중 `.env`에 채워진 것, 실패 시 다음 제공자). **repo agent 비활성 경로**: verify 단계에서 앱 코드가 던진 예외(Traceback의 마지막 사용자 프레임)는 재배포·Dockerfile LLM 없이 즉시 중단하고 `summary`가 `Not auto-healable: application code error`로 시작. import 시점 크래시면 LLM 소스 수정 제안 diff를 summary와 `log` 이벤트(`payload.kind=source_suggestion`)로 리포트만 함 (적용 안 함). **repo agent 활성 경로**: 작업 사본의 소스도 수정 가능. **코드 수정 동의**: 소스 수정과 저장소에 원래 있던 Dockerfile의 수정은 사용자 코드라서 `confirm_fn`(main.py)이 먼저 묻고 (`log` `kind=question`, 답: `POST /deploy/{id}/answer` `continue`=PR 후 계속 · `pr_only`=PR만 · `stop`=아무것도 안 함), 동의한 뒤에만 PR을 연다. 수정 내용이 다르면 다시 묻는다. 아무도 보지 않는 배포(GitHub push, 장애 대응)는 PR로만 제안하고 멈춘다. analyzer가 만든 Dockerfile은 CloudMorph의 빌드 설정이라 묻지 않고 고친다. 필수 설정·기동 서비스·진단 없는 크래시는 어느 경로든 agent 호출 전에 중단 |
| `main.py` | 백락원 | `POST /deploy`, `GET /deploy/{id}/events` (SSE), `POST /deploy/{id}/env`, `POST /deploy/{id}/cancel`, `POST /projects/{name}/stop`, `GET /incidents`, `POST /projects/{name}/drill` ([장애 대응](incident.md)), `POST /projects/{name}/resources` (DB 끌어다 놓기), `POST /projects/{name}/move` (내 컴퓨터 ↔ 클라우드, [대시보드](dashboard.md)) | 파이프라인 오케스트레이션 · 진행 중 배포 중단 · 떠 있는 앱 중지. `ask_env: true`(대시보드)면 분석 뒤 `required_env`를 `input_required`로 묻고 `POST /deploy/{id}/env {values}`를 기다린다(15분). 값은 이벤트로 내보내지 않으며, healer 재배포에도 같은 값이 간다. `name`을 준 배포는 값을 `env_store`(Secret Manager `cloudmorph-env-<name>-<소스 해시>`, `GCP_PROJECT_ID` 없으면 저장 안 함)에 이름과 소스(저장소 URL 또는 경로) 단위로 저장하고, 다음 배포에서는 저장된 값을 먼저 쓴다: 대시보드는 빠진 이름만 묻고, `ask_env` 없는 호출(GitHub push)은 저장된 값으로 배포한다. 무작위로 만든 값도 저장해 배포마다 바뀌지 않는다. 같은 이름이라도 다른 저장소에서 온 배포는 저장된 값을 받지 못하고 처음부터 다시 묻는다 |

## Data types

**AnalysisResult** — `target`, `service_models[]` (`caas|paas|faas|iaas`, 추천 순), `language`, `framework?`, `port` (default 8080), `entrypoint?`, `dockerfile`, `notes[]`, `required_env[EnvRequirement]`

**EnvRequirement** — `name`, `scope` (`build|runtime`), `secret`, `generate` (비우면 무작위 값), `reason`, `evidence["path:line"]`, `resource` (`postgres|mysql|redis|s3`: 외부 저장소 접속 정보 — 사용자가 자기 서버를 넣을 수 있어 항상 묻는다), `auto` (비우면 deployer가 만들어 연결: `deployer.provisioned_env` — Postgres 앱의 `DATABASE_URL`(function 제외), MySQL 앱의 `DATABASE_URL`과 `REDIS_URL`은 모든 타깃이 local/node일 때만. main.py가 표시). 사용자가 `DATABASE_URL`을 넣으면 deployer는 Postgres/Cloud SQL을 만들지 않는다. deployer 전달: build+secret → `docker build --secret id=NAME,env=NAME` (BuildKit 필요, 값은 명령줄·레이어에 안 남음), build → `--build-arg`, runtime → 컨테이너 env

**DeployResult** — `success`, `target`, `exit_code`, `stdout`, `stderr`, `url?`, `duration_sec?`

**HealState** (LangGraph) — `src_dir`, `target`, `error_log`, `error_category`, `current_dockerfile`, `diff`, `history[PatchRecord]`, `retry_count`, `status` (`healing|healed|gave_up`), `last_result`

**ErrorCategory** — `port_binding` · `missing_dependency` · `bad_entrypoint` · `build_failure` · `out_of_memory` · `unknown`

**인스턴스 사양** — deployer는 cpu/memory를 의존성(`detect_size`: torch·transformers → 2 vCPU/4Gi, playwright·puppeteer → 2Gi, numpy·pandas·JVM → 1Gi) < `DEPLOYER_CPU`/`DEPLOYER_MEMORY`(또는 `deploy(..., cpu=, memory=)`) < Dockerfile `LABEL cloudmorph.cpu="2" cloudmorph.memory="1Gi"` 순으로 정한다. cloudrun/function은 `--cpu`/`--memory`(Cloud Run이 받는 조합으로 올려 맞춤), local/node는 `docker run --cpus/--memory`. 실패한 배포의 `stderr`에는 `[deployer] instance cpu=… memory=…` 줄이 붙는다. healer는 `out_of_memory`(OOMKilled, Cloud Run `Memory limit of … exceeded`, `java.lang.OutOfMemoryError`, `MemoryError`)를 만나면 `LABEL cloudmorph.memory`를 두 배로 올려(최대 8Gi) 재배포한다

> 이슈 #7 실패 유형은 enum을 늘리지 않고 기존 카테고리에 흡수했다: 공유 라이브러리 누락(`cannot open shared object file`) → `missing_dependency`, 비root `USER`의 쓰기 권한 → `unknown` + WORKDIR 내부의 안전한 경로에만 적용하는 chown 규칙, 필수 환경변수 누락 · 외부 서비스(DB) 접속 불가 · 로그 없는 크래시 → `unknown` + 고정 summary 접두어의 중단(`CONFIG_STOP` / `SERVICE_STOP` / `CRASH_STOP`, repo agent가 켜져 있어도 LLM 전에 멈춤). `Connection refused`는 더 이상 `port_binding`이 아니다. 별도 카테고리 후보(`permission` · `config_missing` · `system_dependency`)는 팀 합의 후 추가.

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
| `log` | `{ "line": "..." }` — 빌드/배포 로그 스트리밍. `{ "kind": "decision", service, target, label, explain, probabilities, ms, prep_ms, engine, why }`는 Jev나 clef-flash가 1초 안에 고른 서비스 (분석이 끝나기 전에 온다) |
| `heal_diff` | `PatchRecord` — **AI 수정 전/후 비교 카드** |
| `input_required` | `{ "items": [EnvRequirement], "timeout_sec", "choose_target"? }` — 배포 전에 사용자에게 받을 값 (`ask_env`일 때만). `targets: []`(CloudMorph가 고름)이면 `choose_target: { recommended, options, decision, held_back, risks }`도 온다. 답(`POST /deploy/{id}/env`)의 `targets`로 고르며, 위험 때문에 미뤄 둔 곳을 고르면 그 위험에 동의한 것이다 |
| `done` | `{ "target", "url" }` 또는 `HealReport` |
| `error` | `{ "message" }` 또는 `HealReport` (gave_up) |

## Open questions for 3차 미팅
1. `src_dir` 전달 방식: 업로드 zip → 서버 임시 디렉토리 경로로 통일?
2. 로컬 배포의 Public URL 발급 방식 (Cloudflare Quick Tunnel 등) — deployer 담당
3. 멀티 타깃(local + cloudrun) 순차 vs 병렬
4. AWS 2nd target 여부 (클라우드 활용 30점)
5. **deployer stderr 범위**: Cloud Run은 크래시 traceback이 revision logs에만 남음 → `gcloud logging read` / `docker logs`로 런타임 로그를 `stderr`에 합칠 것 (안 하면 패키지 누락이 포트 에러로 오분류되어 데모 실패)
6. `main.py` CORS 설정(프론트 dev 서버 origin) — 백락원
