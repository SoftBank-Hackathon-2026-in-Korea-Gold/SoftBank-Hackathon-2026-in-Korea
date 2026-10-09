# GitHub push → 자동 재배포 (CD) — `POST /webhook/github`

## 흐름
```
개발자 git push ─▶ GitHub ─webhook(push, HMAC 서명)─▶ 터널 ─▶ 대시보드 프록시 ─▶ 백엔드 /webhook/github
   ─▶ 서명 검증 ─▶ 기본 브랜치인지 확인 ─▶ _launch(저장소 URL, 타깃, name=저장소이름, ref=브랜치)
   ─▶ 분석 → 배포(local + cloudrun) → 실패 시 자가치유 ─▶ 같은 이름의 서비스가 새 버전으로 갱신
```
핵심은 **안정적인 앱 이름**이다. `POST /deploy`는 지금까지 배포마다 새 서비스를 만들었다(`cloudmorph-<id>-<target>`).
CD는 저장소 이름을 앱 이름으로 쓰므로 푸시할 때마다 **같은 Cloud Run 서비스에 새 리비전**이 올라가고(블루그린),
Cloud SQL의 앱별 데이터베이스도 그대로 재사용되어 데이터가 남는다. `POST /deploy`에도 `name`을 주면 같은 동작을 한다.

## 설정
| 환경변수 | 뜻 | 기본 |
|---|---|---|
| `CLOUDMORPH_WEBHOOK_SECRET` | GitHub 웹훅 비밀값. 없으면 엔드포인트가 503으로 꺼짐 | (없음) |
| `CLOUDMORPH_CD_TARGETS` | 푸시 시 배포할 타깃 | `local,cloudrun` |
| `CLOUDMORPH_CD_BRANCHES` | 배포할 브랜치 목록 | 저장소의 기본 브랜치만 |

GitHub 쪽: 저장소 Settings → Webhooks → Payload URL `https://<공개 URL>/webhook/github`, Content type `application/json`,
Secret = 위 값, 이벤트 `push`. CLI로는:
```bash
gh api -X POST repos/<owner>/<repo>/hooks --input hook.json   # {"name":"web","active":true,"events":["push"],"config":{"url":...,"content_type":"json","secret":...}}
```
quick tunnel 주소는 매번 바뀌므로 **발표 직전에 등록**하거나 도메인을 붙여 고정 주소를 쓴다.

## 보안
- `X-Hub-Signature-256`을 HMAC-SHA256으로 검증(상수 시간 비교). 불일치 401, 비밀값 미설정 503
- `ping`과 push 외 이벤트는 200으로 무시. 태그 푸시·브랜치 삭제 무시. 기본 브랜치 외 푸시는 무시(설정으로 허용 가능)
- 저장소 URL은 main.py의 기존 HTTPS·호스트 허용 목록 검증을 그대로 거친다

## 실측 (10/10, Jeonsubb/cloudmorph-demo-guestbook)
| 푸시 | 감지 | 완료 | 결과 |
|---|---|---|---|
| v2 | 6초 | 61초 | local 터널 URL + Cloud Run URL. 분석 로그에 "postgres 필요, 인스턴스 1대 고정" |
| v3 | 수 초 | 79초 | **같은 Cloud Run 서비스**가 리비전 00002로 갱신, 이전에 쓴 방명록 글 유지 |

## 관측
- `GET /projects`: 앱 이름별 마지막 배포(트리거, 브랜치, 상태, URL, 이력). 대시보드가 표시할 수 있다
- 배포 이벤트 첫 줄에 `github push by <who>: <repo>@<branch> <sha> — <message>`가 들어간다

## 한계
- 동시 푸시가 겹치면 두 배포가 병렬로 돈다(같은 이름). 큐잉·취소는 미구현
- 상태는 메모리에만 있어 서버 재시작 시 `/projects`가 비워진다(`.fleet`, `.deployer` 상태 파일은 남음)
