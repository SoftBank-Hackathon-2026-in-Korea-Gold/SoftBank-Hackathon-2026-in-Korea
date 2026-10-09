# CloudMorph 발표 대본 (확정본, 10/10)

> **One Action, Infinite Clouds** — 코드를 넣거나 push만 하면, AI가 읽고 어디에 어떻게 띄울지 정해 배포하고, 실패하면 스스로 고쳐 다시 배포한다.
>
> 슬라이드 없이 **설계 문서(Notion) + 라이브 대시보드**로 진행한다. 원본 초안: [`demo-script.md`](demo-script.md) (전동훈).

## 0. 실측 기록 (`main` = `b2ea447`, 10/10 08:15~08:35)

| 시나리오 | 결과 | 소요 | 검증 |
|---|---|---|---|
| `sample-apps/broken` → local | 자가치유 2회(flask 누락 → 포트 바인딩) 후 Live URL 200 | 128초 | ✅ 오늘 |
| `sample-apps/broken` → cloudrun | 자가치유 2회 후 Cloud Run URL 200 | 약 160초 | ✅ 오늘 |
| `sample-apps/guestbook` → local | AI가 Python/Flask, Postgres 필요, SQLite 유실 위험 감지 → Postgres 사이드카 자동 | 36초 | ✅ 오늘 |
| 공개 터널 인증 | 토큰 없음 401 · 틀린 토큰 401 · 토큰 있음 통과 · `/health`·대시보드 200 | — | ✅ 오늘 |
| 토큰 미설정 + 터널 경유 | 503 (fail-closed) | — | ✅ 오늘 |
| GitHub push → 웹훅 자동 재배포 | 감지 9초 → 치유·배포 196초 | — | ⚠️ 전동훈 노트북 실측 |
| 노드 풀 부하 → 다른 VM 복제 | 20초 | — | ⚠️ 전동훈 노트북 실측 (VM·`nodes.json` 필요) |

## 1. 발표 30분 전 체크리스트

```bash
cd SoftBank-Hackathon-2026-in-Korea
echo "${ANTHROPIC_BASE_URL:-OK(비어 있음)}"   # 값이 있으면 unset — 로컬 프록시 주소면 AI 분석이 Connection error로 실패함
git pull && scripts/demo-up.sh --public      # 백엔드+대시보드+터널, 토큰 출력
```

- [ ] 출력된 **공개 URL**을 휴대폰으로 열고 **토큰** 입력
- [ ] 대시보드에서 `sample-apps/guestbook` → local 1회 미리 배포 (Docker 캐시 데우기, 36초)
- [ ] `gcloud auth login` 상태 확인 (Cloud Run 타깃)
- [ ] (웹훅·노드 풀 장면을 쓸 때만) 전동훈 노트북: `CLOUDMORPH_WEBHOOK_REPOS`, `CLOUDMORPH_WEBHOOK_SECRET`, `backend/nodes.json` 준비
- [ ] Notion 설계 문서 탭 열어 두기: [설계 자료](https://app.notion.com/p/da58bee9ada482a79abb010aad08c0f0)

## 2. 중간보고 버전 (3분)

| 시간 | 화면 | 말할 것 |
|---|---|---|
| 0:00–0:30 | Notion 개요 | **문제**: 배포가 어려운 이유는 환경 차이(포트·바인딩·의존성·DB). 이걸 사람 대신 파이프라인이 흡수한다. |
| 0:30–1:00 | Notion 구조도 | **구조**: ① analyzer(Claude)가 코드를 읽고 타깃 결정 → ② deployer가 local Docker / Cloud Run 블루그린 배포 → ③ 실패 시 healer가 stderr 분류 → Dockerfile 패치 → 최대 3회 재배포 |
| 1:00–2:30 | 대시보드 | `sample-apps/broken` → local 배포 버튼. 빨간 ERROR(`ModuleNotFoundError`) → 노란 HEAL diff `+RUN pip install flask` → 두 번째 실패(127.0.0.1 바인딩) → 포트 패치 → **Live URL 열기** |
| 2:30–3:00 | 대시보드 | **현재 진행률과 남은 계획**: Cloud Run·DB 자동 생성·push 자동 배포·노드 풀까지 통합 완료. 오후에는 시연 안정화와 설계 이유 문서화 |

> 배포가 2분 넘게 걸리므로 **1:00에 버튼을 먼저 누르고** 구조 설명을 이어 가도 된다.

## 3. 최종 발표 버전 (5분)

### 3-1. 설계 (2분) — 화면: Notion
| 시간 | 말할 것 |
|---|---|
| 0:00–0:30 | **무엇**: "코드를 넣거나 GitHub에 push만 하면, AI가 읽고 어디에 어떻게 띄울지 정해서 로컬·Cloud Run·VM에 배포하고, 실패하면 스스로 고쳐 다시 배포합니다." |
| 0:30–1:00 | **왜**: 배포 실패의 대부분은 코드가 아니라 환경 차이(포트, 바인딩 주소, 누락 의존성, DB). 반복되는 패턴이라 자동화할 수 있다. |
| 1:00–2:00 | **어떻게**: ① Claude 검사관 분석(언어·프레임워크·상태 저장 여부·필요한 DB) ② Cloud Run 블루그린(검증 전 트래픽 0%) + DB 자동 프로비저닝 ③ healer: **규칙 우선, 모르면 LLM** — 데모 시나리오는 규칙만으로 고쳐서 API 장애에도 동작 ④ push 웹훅 CD ⑤ 토큰 인증 + 웹훅 HMAC |

### 3-2. 데모 (3분) — 화면: 대시보드
**A안 (전동훈 노트북, 웹훅·노드 풀 준비됨)** — [`demo-script.md`](demo-script.md) §2 그대로: flask 지운 커밋 push → "GitHub push 감지" 배너 → 분석 카드 → ERROR → HEAL diff → Cloud Run URL에서 방명록 글쓰기 → `app.fleet stress`로 확장 이벤트.

**B안 (어느 노트북이든, 오늘 검증된 경로)**
| 시간 | 행동 | 짚을 것 |
|---|---|---|
| 2:00 | 대시보드에서 `sample-apps/broken`, 타깃 local+cloudrun, 배포 | "버튼 한 번" |
| 2:15 | 분석 카드 | AI가 Flask·포트 8080·환경변수 분리 제안까지 읽음 |
| 2:40 | 빨간 ERROR 펼치기 → `ModuleNotFoundError` | 실패 원문을 그대로 healer에 넘김 |
| 3:00 | 노란 HEAL diff 두 번 | **자가치유**: 의존성 → 포트 바인딩, 규칙 기반 |
| 3:40 | 엔드포인트 카드 "자가치유됨" → local URL, Cloud Run URL 열기 | 같은 코드가 두 환경에서 동작 |
| 4:10 | 미리 배포한 `guestbook` 결과 | DB가 필요한 앱은 Postgres를 자동으로 붙임 |
| 4:40 | 마무리 | "고장 나면 스스로 고치고, 필요한 인프라는 알아서 붙입니다. One Action, Infinite Clouds." |

## 4. 예상 질문
- **Vercel/Render와 차이?** 그들은 자기 인프라에 올린다. 우리는 *사용자의 GCP 프로젝트와 PC*에 AI가 대신 올린다(BYOC). 데이터·비용이 사용자 계정에 남는다.
- **AI 판단을 그대로 믿나? 사용자 승인은?** 지금은 분석 결과와 위험(상태 저장, DB 필요)을 대시보드에 그대로 보여 주고, 실제 변경은 규칙 우선 패치로 제한했다. 비용이 드는 선택(VM 여러 대 등)에 대한 승인 단계는 다음 단계 과제다. *(팀 미결정 — 발표 전 합의 필요)*
- **Lambda 같은 서버리스는?** analyzer가 `service_models`(caas/paas/faas/iaas)로 적합한 형태를 판정하지만, 현재 파이프라인은 컨테이너(Dockerfile) 경로만 구현했다. FaaS 분기는 향후 계획.
- **고장 리비전이 서비스되면?** 블루그린: 새 리비전은 트래픽 0% + candidate URL 검증을 통과해야 승격. 공개 URL은 깨지지 않는다.
- **인증은?** 시연은 API 토큰 1개(`/deploy`·`/fleet`·`/projects`), 웹훅은 HMAC 서명. 토큰을 설정하지 않으면 터널·원격 요청은 503으로 막힌다(fail-closed). OIDC/JWT 사용자 인증은 향후 계획.
- **비밀값이 LLM으로 가지 않나?** 소스 복사 단계에서 `.env`·키 파일·클라우드 자격증명을 제외한다. 일반 소스 안의 비밀값 마스킹은 후속 과제로 잡아 두었다.
- **DB 있는 앱을 확장하면?** stateful로 표시되어 자동 확장에서 제외된다.

## 5. 사고 대비
| 상황 | 대응 |
|---|---|
| 분석 카드에 "AI 검사관 일부가 실패: Connection error" | `ANTHROPIC_BASE_URL`이 설정돼 있는지 확인 → `unset` 후 `scripts/demo-down.sh && scripts/demo-up.sh --public`. 자가치유 자체는 규칙 기반이라 계속 동작 |
| 공개 터널 DNS가 늦음(첫 요청 000) | 10~20초 기다리거나 노트북 화면 `http://127.0.0.1:5173`으로 진행 |
| GitHub 웹훅이 안 옴 | B안으로 전환: 대시보드에서 수동 배포 |
| Cloud Run이 느림 | local 타깃만으로 자가치유 시연(약 2분), Cloud Run은 미리 받아 둔 결과 화면 |
| 대시보드가 401 | 토큰 재입력 (`.demo/token`) |
| LLM API 장애 | 데모 시나리오(flask 누락, 포트)는 규칙 패치라 API 없이 동작 |
