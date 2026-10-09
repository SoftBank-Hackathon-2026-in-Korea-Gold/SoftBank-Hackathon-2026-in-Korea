# 최종 발표 대본 (5분, 슬라이드 없음 · 설계 문서 + 데모)

> 실측 기준(10/10): GitHub URL → 3타깃 98초, 고장 앱 자가치유 84초, 고장 커밋 push → 자동 추적 9초 → 치유·배포 196초,
> 연속 push는 순서대로 처리, 노드 풀 부하 시 20초 만에 다른 VM으로 복제.

## 0. 발표 30분 전 준비
```bash
cd SoftBank-Hackathon-2026-in-Korea
export CLOUDMORPH_WEBHOOK_REPOS=Jeonsubb/cloudmorph-demo-guestbook
# 5173을 다른 앱이 쓰고 있으면: export FRONT_PORT=5174
scripts/demo-up.sh --public          # 백엔드+대시보드+터널, 웹훅을 새 터널 주소로 자동 재등록, 토큰 출력
```
- 출력된 공개 URL을 휴대폰으로 열어 토큰 입력 → 심사위원에게 보여 줄 화면 확인
- 대시보드에서 `sample-apps/guestbook` → local 한 번 미리 배포(Docker 이미지 캐시 데우기)
- 터미널 두 개: ① 데모 저장소 폴더(`git push`용) ② `uv run python -m app.fleet watch --interval 5`
- 데모 저장소가 정상 상태인지 확인(requirements에 flask 있음)

## 1. 설계 문서 (2분) — 화면: Notion 설계 문서
| 시간 | 말할 것 |
|---|---|
| 0:00–0:30 | **무엇**: "코드를 넣거나 GitHub에 push만 하면, AI가 읽고 어디에 어떻게 띄울지 정해서 로컬·Cloud Run·멀티 클라우드 VM에 배포하고, 실패하면 스스로 고쳐 다시 배포합니다." |
| 0:30–1:00 | **왜**: "배포가 어려운 이유는 환경 차이입니다. 포트, 바인딩 주소, 아키텍처, 그리고 DB. 이걸 사람이 아니라 파이프라인이 흡수하게 했습니다." |
| 1:00–2:00 | **어떻게** (구조도 9-1 가리키며): ① Claude 검사관 분석 ② deployer: Cloud Run 블루그린(검증 전엔 트래픽 0%), 노드 풀(SSH+Docker면 어느 클라우드든), DB 자동 프로비저닝(Cloud SQL 앱별 DB) ③ healer: stderr 분류→Dockerfile 패치→재배포 ④ GitHub push 웹훅 CD |

## 2. 데모 (3분) — 화면: 대시보드
| 시간 | 행동 | 화면에서 짚을 것 |
|---|---|---|
| 2:00 | 데모 저장소에서 `requirements.txt`의 flask 한 줄 지우고 `git push` (미리 커밋해 두고 push만) | — |
| 2:10 | 대시보드에 "GitHub push 감지" 배너가 뜸 | **사람이 버튼을 안 눌렀다**는 점 |
| 2:40 | 분석 카드 | "Postgres 필요, 인스턴스 1대 고정, 127.0.0.1 바인딩 위험" — AI가 코드를 읽고 판단 |
| 3:00 | 로그에 빨간 ERROR → 펼치면 `ModuleNotFoundError` | 실패 원문을 그대로 AI에 넘김 |
| 3:10 | 노란 HEAL, 오른쪽 diff `+RUN pip install flask` | **자가치유**. 규칙 우선, 모르면 LLM |
| 3:40 | 엔드포인트 카드 "자가치유됨" → Cloud Run URL 열기 → 방명록에 글 쓰기 | DB까지 붙은 실제 서비스. 이전 글이 남아 있음(Cloud SQL) |
| 4:10 | 터미널 ②에서 `uv run python -m app.fleet stress scale-demo-node --seconds 40` | 노드 패널에서 CPU 막대 빨개짐 → **확장 gcp-node-2** 이벤트 |
| 4:40 | 마무리 | "같은 코드가 로컬·서버리스·VM 세 환경에서 돌고, 고장 나면 스스로 고치고, 몰리면 번집니다. One Action, Infinite Clouds." |

## 3. 예상 질문
- **Vercel/Render와 차이?** 그들은 자기 인프라에 올린다. 우리는 *사용자의 GCP 프로젝트와 PC*에 AI가 대신 올린다(BYOC). 데이터·비용이 사용자 계정에 남는다.
- **AWS/오라클은?** 노드 풀은 SSH+Docker 추상화라 VM을 `nodes.json`에 한 줄 추가하면 같은 코드로 동작한다. Cloud Run 같은 고유 서비스는 타깃 함수를 추가하는 구조.
- **고장 리비전이 서비스되면?** 블루그린: 새 리비전은 트래픽 0% + candidate 태그 URL에서 검증 통과 후에만 승격. 공개 URL은 한 번도 깨지지 않는다.
- **동시에 여러 번 push하면?** 같은 앱은 순서대로 처리(잠금). 실측에서 v7이 v6 종료 1초 뒤 시작, 최종 v7.
- **DB 있는 앱을 확장하면 데이터가 갈라지지 않나?** DB가 붙은 앱은 stateful로 표시되어 자동 확장에서 제외된다.
- **인증은?** 대시보드·배포 API는 토큰, 웹훅은 HMAC 서명. 사용자 계정은 범위 밖으로 결정.

## 4. 사고 대비
| 상황 | 대응 |
|---|---|
| 공개 터널 DNS가 늦음 | 노트북 화면(`http://127.0.0.1:5173`, `FRONT_PORT`를 줬다면 그 포트)으로 진행, 휴대폰은 나중에 |
| GitHub 웹훅이 안 옴 | 대시보드 소스에 저장소 URL 넣고 수동 배포(같은 이름이면 같은 서비스 갱신) |
| Cloud Run이 느림 | local 타깃만으로 자가치유 시연(84초), Cloud Run은 미리 받아 둔 결과 화면 |
| LLM API 장애 | 데모 시나리오(flask 누락, 포트)는 규칙 패치라 API 없이도 동작 |
