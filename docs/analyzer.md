# analyzer (이요환)

`analyze(src_dir) -> AnalysisResult` — 소스를 읽고 배포 타깃(`local` / `cloudrun`)과 초기 Dockerfile을 정한다.
계약은 [`interfaces.md`](interfaces.md) 그대로이고, 코드는 `backend/app/analyzer.py` 하나에 있다.

## 어떻게 판정하나

```
AI 검사관 17개 (Claude, 동시에)  →  울타리 (코드)  →  타깃  →  Dockerfile (AI, 울타리)
```

- **AI 검사관**: 찾는 것마다 각자의 기준으로 판정한다. 위험 신호 13개(서버 코드, 상주 작업, 주기 작업, 메모리 상태, SQLite,
  로컬 파일 쓰기, GPU, OS 패키지 …)와 명세 4개(실행 방법, 외부 저장소, 환경변수, 기타 위험).
  모델에 읽기 도구(파일 목록·읽기·검색)만 주고 저장소를 직접 읽게 한다. 저장소 밖은 읽지 못하고, 쓰기·명령 실행은 없다.
- **울타리**: 검사관이 낸 근거(파일:줄 + 코드)와 환경변수 이름이 실제 저장소에 없으면 버린다.
  신호 → 배포 유형은 정해진 규칙(`RULES`)이 정한다. AI가 타깃을 고르지 않는다.
- **패턴 규칙은 쓰지 않는다.** 키가 없으면 분석하지 않고 `ValueError`를 낸다.

실제 저장소 2개(MaechuriAIServer, MaechuriMainServer)로 두 번씩 돌렸을 때 위험 신호 13개 판정이 모두 같았다
(검사관에게 패턴 검색 줄을 참고로 주던 때 잰 것이다).
분석 1건에 LLM 18~20회 (Dockerfile 작성 1회 포함), 약 1분 30초.

## 파이프라인에서의 기본값 (물어볼 수 없어서 안전한 쪽)

- 데이터 손실 계열(SQLite, 로컬 파일 쓰기, 메모리 상태, 주기 작업)은 AI가 괜찮다고 해도 **사용자 동의 없이는 풀지 않는다**.
  파이프라인에는 동의를 받을 자리가 없으니 위험으로 두고, `notes`에 "동의하면 풀 수 있다"고 적는다.
  검사관이 실패했거나 근거를 파일에서 확인하지 못했을 때도 데이터 손실 계열은 위험으로 둔다.
- 위험 신호로 지워진 유형은 고르지 않는다. **코드는 고치지 않고** 필요한 수정은 제안만 `notes`에 적는다.
- 타깃: 서버리스 컨테이너나 정적 사이트로 갈 수 있으면 `cloudrun`, 아니면 `local`.
- `service_models`: 코드가 갈 수 있는 서비스 모델 전부를 추천 순서(`caas` → `paas` → `faas` → `iaas`)로 보낸다.
  deployer는 이 중 지원하는 것을 고른다. 정적 사이트는 nginx 컨테이너로 감싸므로 `caas`, `iaas`다.
  `paas`나 `faas`가 있으면 `caas`도 반드시 있다 (`caas`를 지우는 규칙은 모두 `paas`·`faas`도 지운다).
  `faas`는 코드 제약만 본 것이라, 서버 앱을 함수로 감싸는 어댑터는 따로 필요할 수 있다.
- Dockerfile: 저장소에 있으면 그대로 쓴다. 없으면 실행 방법으로 템플릿을 만들고, AI가 저장소를 읽고 그 초안을 고친다
  (예: GPU를 안 쓰는 torch는 CPU 휠, go.sum이 없으면 go mod tidy). 템플릿이 없는 언어(ruby, php …)는 AI가 처음부터 쓴다.
  AI가 쓴 것은 울타리(`FROM`, `ENV PORT=<port>`, `COPY` 대상이 저장소에 있는지, 템플릿에 `CMD`가 있으면 마지막 단계에 `CMD`/`ENTRYPOINT`가 있는지)를
  넘어야 쓰고, 못 넘거나 AI가 실패하면 템플릿을 쓴다.
  고친 이유는 `notes`에 적는다. 모두 `0.0.0.0:$PORT`로 받는다. 배포가 실패하면 healer가 고친다.

## 환경변수 (`.env`)

| 이름 | 기본값 | 뜻 |
|---|---|---|
| `ANTHROPIC_API_KEY` | | healer와 같은 키. 없으면 analyzer는 분석하지 않는다 |
| `ANALYZER_MODEL` | `claude-haiku-5-5` | 검사관 모델 |
| `ANALYZER_MAX_LLM_CALLS` | `6` | 서버 전체에서 동시에 도는 검사관 호출 수. 분석이 여러 건 겹치면 나머지는 기다린다 |
| `ANALYZER_MAX_TURNS` | `15` | 검사관 하나가 파일을 읽으며 주고받는 최대 횟수 |

실제 Claude로 확인: `cd backend && ANALYZER_LIVE=1 uv run pytest tests/analysis -k live -s` (키는 저장소 맨 위 `.env`에서 읽는다).
평소 `pytest`에서는 건너뛴다.

## 팀에 제안 (합의 필요)

1. **`AnalysisResult` 넓히기**: analyzer는 이미 아래 정보를 만들지만 계약에 칸이 없어 `notes` 문장으로만 넘긴다.
   - `resources`: 필요한 DB·Redis·버킷과 접속 환경변수 이름 (deployer가 Cloud SQL 등을 만들거나 연결할 때 필요)
   - `env`: 사용자가 넣어야 할 환경변수 (무작위 값을 넣어도 되는지)
   - `risks`: 기타 위험 (예: 시작할 때 큰 모델 로드, 이미지에 없는 파일)
2. **동의를 화면에서 받기**: SSE에 질문 이벤트를 두고 답을 받는 API(`POST /deploy/{id}/answer`)를 두면,
   데이터 손실 위험 해제를 사용자에게 받을 수 있다. 지금은 안전한 쪽으로만 간다.
3. **소스 받기 (`main.py`)**: 얕은 클론(`git clone --depth 1 --filter=blob:limit=1m`)을 추천. 큰 파일은 분석에 필요 없어 받지 않는다.
   받기 전에 GitHub API로 저장소 크기를 보고 상한을 넘으면 거절한다. 비공개 저장소는 `GITHUB_TOKEN`이 필요하다.
