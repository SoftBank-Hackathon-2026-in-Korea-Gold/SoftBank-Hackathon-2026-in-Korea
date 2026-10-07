# sample-apps (owner: 전동훈)

시연용 배포 대상 앱. 앱 퀄리티는 무관 — 배포 플랫폼 시연이 목적.

| Dir | 목적 | 의도된 결함 |
|---|---|---|
| `healthy/` | 정상 배포 경로 시연 | 없음 |
| `broken/` | 자가치유 시연 | 패키지 누락(requirements 미기재) + 포트/호스트 하드코딩(`127.0.0.1:5000`) |

`broken/` 실행 시 실제 stderr를 `backend/tests/fixtures/`에 저장해 두면 healer 패턴 보강에 사용합니다.
