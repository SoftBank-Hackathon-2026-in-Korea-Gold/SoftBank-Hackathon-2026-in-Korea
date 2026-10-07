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
| `healer.py` | 박재현 | `heal(src_dir, target, failed: DeployResult, dockerfile, deploy_fn, llm_patch_fn=None, emit=None) -> HealReport` | 재배포 루프를 healer가 소유. 규칙 패치 우선, GPT-4o 폴백 |
| `main.py` | 백락원 | `POST /deploy`, `GET /deploy/{id}/events` (SSE) | 파이프라인 오케스트레이션 |

## Data types

**AnalysisResult** — `target`, `language`, `framework?`, `port` (default 8080), `entrypoint?`, `dockerfile`, `notes[]`

**DeployResult** — `success`, `target`, `exit_code`, `stdout`, `stderr`, `url?`, `duration_sec?`

**HealState** (LangGraph) — `src_dir`, `target`, `error_log`, `error_category`, `current_dockerfile`, `diff`, `history[PatchRecord]`, `retry_count`, `status` (`healing|healed|gave_up`), `last_result`

**ErrorCategory** — `port_binding` · `missing_dependency` · `bad_entrypoint` · `build_failure` · `unknown`

**PatchRecord** — `attempt`, `category`, `error_excerpt`, `before`, `after`, `diff` (unified), `rationale`, `source` (`rule|llm`)

**HealReport** — `success`, `target`, `attempts`, `records[PatchRecord]`, `final_dockerfile`, `final_url?`, `summary`

## SSE events (main.py → frontend)

`event:` = `type`, `data:` = `PipelineEvent` JSON

```json
{ "type": "stage | log | heal_diff | done | error",
  "stage": "analyze | deploy | heal | redeploy | null",
  "payload": { },
  "ts": 1791380000.123 }
```

| type | payload |
|---|---|
| `stage` | `{ "target"?, "attempt"?, "stderr"? }` — 진행 단계 표시 |
| `log` | `{ "line": "..." }` — 빌드/배포 로그 스트리밍 |
| `heal_diff` | `PatchRecord` — **AI 수정 전/후 비교 카드** |
| `done` | `{ "target", "url" }` 또는 `HealReport` |
| `error` | `{ "message" }` 또는 `HealReport` (gave_up) |

## Open questions for 3차 미팅
1. `src_dir` 전달 방식: 업로드 zip → 서버 임시 디렉토리 경로로 통일?
2. 로컬 배포의 Public URL 발급 방식 (Cloudflare Quick Tunnel 등) — deployer 담당
3. 멀티 타깃(local + cloudrun) 순차 vs 병렬
4. AWS 2nd target 여부 (클라우드 활용 30점)
5. **deployer stderr 범위**: Cloud Run은 크래시 traceback이 revision logs에만 남음 → `gcloud logging read` / `docker logs`로 런타임 로그를 `stderr`에 합칠 것 (안 하면 패키지 누락이 포트 에러로 오분류되어 데모 실패)
6. `main.py` CORS 설정(프론트 dev 서버 origin) — 백락원
