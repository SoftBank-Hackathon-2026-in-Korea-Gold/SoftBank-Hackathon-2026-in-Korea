# CloudMorph 서버 호스팅 (10/10)

서비스 자체를 GCE VM 한 대에 올렸다. 노트북마다 gcloud·Docker·키를 맞출 필요가 없고, 노트북을 꺼도 대시보드와 GitHub 웹훅이 계속 동작한다.

| 항목 | 값 |
|---|---|
| 주소 | https://cloudmorph.34-64-253-41.sslip.io (sslip.io 무료 DNS + Let's Encrypt 자동 인증서, 추가 비용 없음) |
| VM | `cloudmorph-control` · e2-standard-4 · Ubuntu 24.04 · 고정 IP 34.64.253.41 · 하루 약 4천 원 |
| 서비스 계정 | `cloudmorph-control@…` — run.admin, artifactregistry.writer, cloudsql.admin, iam.serviceAccountUser, logging.viewer, cloudfunctions.developer, cloudbuild.builds.editor, storage.admin, viewer, serviceUsageConsumer |
| 추가로 켠 API | Cloud Functions, **Cloud Billing**, **Cloud Resource Manager** (서비스 계정으로 쓰면 필요 — 실측으로 확인) |
| 방화벽 | `cloudmorph-control-web` 80·443 |

## 구성
- `deploy/server/bootstrap.sh` — 빈 VM에 Docker·uv·cloudflared, gcloud 설정, Docker→Artifact Registry 인증, 노드 풀용 SSH 키, 저장소 클론
- `deploy/server/cloudmorph-backend.service` — 백엔드 systemd 서비스. `.env`(API 키) + `~/cloudmorph-server.env`(토큰, 자동 확장 감시, 프로젝트)
- `deploy/server/Caddyfile` — HTTPS, 대시보드 정적 파일, `/deploy` `/projects` `/fleet` `/webhook` `/health`를 백엔드로 (SSE는 버퍼링 없이)
- `scripts/server-deploy.sh [ref]` — 노트북에서 서버 갱신: 코드 checkout → 의존성 → 대시보드 빌드·업로드 → 서비스 재시작 → 헬스체크

```bash
CLOUDMORPH_SERVER=cloudmorph@34.64.253.41 CLOUDMORPH_DOMAIN=cloudmorph.34-64-253-41.sslip.io scripts/server-deploy.sh main
```

## 서버에서 달라지는 점
- **local 타깃 = 서버 자신의 Docker** (온프레미스형). 내 노트북에 배포하는 장면이 필요하면 노트북에서 `scripts/demo-up.sh`로 같은 코드를 띄운다(노트북은 공인 IP가 없어 서버가 들어갈 수 없음).
- 소스는 **서버의 임시 폴더**로 가져온다. GitHub URL을 권장. 서버 경로(`sample-apps/...`)는 서버의 저장소 기준.
- 서버가 x86이라 Cloud Run용 amd64 빌드가 교차 빌드 없이 빠르다.
- 토큰은 서버에 항상 설정되어 있다(대시보드에서 입력). 웹훅은 저장소별 HMAC 서명.
- 자동 확장 감시 루프가 백엔드 안에서 상시 돈다(`CLOUDMORPH_FLEET_WATCH=1`).

## 실측 (서버 대시보드, Playwright + Chrome)
| 시나리오 | 결과 |
|---|---|
| 방명록 → local(서버) + cloudrun + node | 83초, 세 URL 모두 Postgres 연결 |
| function-hello → function | 70초, `served_by: cloud run functions (gen2)` |
| GitHub push → 서버 웹훅 | ping 200, push 후 Cloud Run에 새 버전(v8) |
| node 타깃 → 가장 한가한 노드 | **AWS EC2**가 선택됨, GCP 라우터 → AWS 노드로 트래픽 |

## 노드 풀
| 노드 | 회사 | 비고 |
|---|---|---|
| gcp-node-1, gcp-node-2 | GCP 서울 | e2-small |
| aws-node-1 | AWS 서울 | t3.small, `deploy/nodes/aws-node.sh`로 생성, 사용자 `ubuntu` |
| (오라클) | OCI | CLI 설정 후 추가 예정 |
