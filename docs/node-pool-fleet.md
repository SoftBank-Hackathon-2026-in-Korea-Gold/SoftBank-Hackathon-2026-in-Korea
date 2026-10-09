# 노드 풀과 자동 확장 (target `node`, `app/nodes.py`, `app/fleet.py`) — 전동훈, 10/9

## 한 줄 요약
어느 클라우드의 VM이든 **SSH + Docker만 되면 노드**다. deployer는 가장 한가한 노드를 골라 컨테이너를 올리고,
라우터(Caddy)가 앱마다 고정 호스트 이름을 주며, 감시 루프가 복제본 CPU를 재다가 뜨거우면 **다른 노드(다른 회사여도 됨)** 에 복제한다.

```
사용자 ──▶ 라우터 VM (Caddy :80)  ──round robin──▶  앱 복제본 @ gcp-node-1
            <app>.<router-ip>.nip.io                    앱 복제본 @ oci-node-1   ← 자동 확장으로 추가됨
                                                        ...
노트북(백엔드) ──SSH──▶ 노드들: probe(부하) / docker load / docker run / docker exec cat cpu.stat
```

## 구성 요소
| 파일 | 역할 |
|---|---|
| `backend/nodes.json` (gitignored, 예시는 `nodes.example.json`) | 노드·라우터 목록: host, user, key, arch, 포트 범위 |
| `app/nodes.py` | SSH 실행기, 노드 부하 측정(`/proc/loadavg`, 메모리, 컨테이너 수 → score), 최소 부하 노드 선택, 이미지 전송(`docker save \| ssh docker load`), 원격 실행·상태·로그·CPU 누적값 |
| `app/deployer.py::deploy_node` | 노드 선택 → 노드 아키텍처로 빌드 → 전송 → 실행 → 헬스체크 → 라우터 등록 → 공개 호스트 이름 반환 |
| `app/fleet.py` | 라우터(Caddy admin API) 설정 생성·반영, 앱/복제본 상태(`.fleet/fleet.json`), 감시 루프(scale-out/in), 수동 scale, 부하 생성기 |
| `app/main.py` | `GET /fleet` (노드 부하·앱·복제본·최근 이벤트), `POST /fleet/{app}/scale/{n}`, `CLOUDMORPH_FLEET_WATCH=1`이면 감시 스레드 자동 시작 |

## 실측 (10/9, GCP e2-small 노드 2대 + e2-micro 라우터)
- 노드 배포: 22초 (이미지 전송 15초가 대부분). 공개 주소 `http://healthy.34.50.17.157.nip.io`, 응답 헤더 `X-Cloudmorph-Upstream`에 처리 노드 표시.
- 자동 확장: keep-alive 32 동시 요청(≈1,100 rps) → 복제본 CPU 69% 감지 → **약 20초 만에 node-2에 복제** → 트래픽 40k:29k로 분산, p95 202ms, 확장 순간 오류 31건(0.04%).
- 풀 소진 시 `scale_out_blocked: no eligible node` 이벤트. 노드를 더 등록하면 그쪽으로 간다.

## 겪은 함정 (설계 판단의 근거)
1. **COS 호스트 방화벽**: Container-Optimized OS는 INPUT 기본 DROP. `--network host`로 띄운 Caddy는 밖에서 안 보임. Docker 포트 매핑(`-p`)은 다른 체인을 타므로 포트 매핑 방식으로 변경.
2. **Caddy `--config`와 `--resume` 동시 사용 금지**: resume이 이기고 config가 무시됨. 이전 볼륨의 설정(관리 API가 컨테이너 localhost에만 바인딩)이 복원되어 reload가 "connection reset".
3. **수동 헬스체크 제거**: 업스트림이 하나뿐일 때 과부하 타임아웃을 "다운"으로 판정해 10초간 전부 거부. 확장 전 단계에서 치명적.
4. **CPU 측정**: `docker stats`는 1초 순간값이라 0~72%를 오감. cgroup `cpu.stat usage_usec`를 두 번 읽어 구간 평균으로 계산. 노드 1분 부하(/core)도 보조 신호.
5. **부하 생성기 자체 결함**: 요청마다 새 TCP 연결 → 맥 임시 포트 고갈 → 주기적 0 rps. 서버 장애처럼 보였음. keep-alive로 수정하니 1,100 rps.
6. **노드 간 이미지 복사**: 노드에는 SSH 키를 두지 않는다. 확장 시 이미지는 노트북이 중계(`ssh A docker save | ssh B docker load`), 노트북에 이미지가 남아 있으면 바로 전송.

## 노드 추가 (오라클 / AWS)
1. VM 생성(리눅스, Docker 설치, 사용자를 docker 그룹에), 보안 그룹에서 22와 8000-8100 개방.
2. `nodes.json`의 `nodes`에 항목 추가. `arch`는 `uname -m`(ARM이면 `arm64`), `key`는 접속 키 경로.
3. `uv run python -m app.fleet status`로 probe가 되는지 확인. 끝. 코드 변경 없음.

## 데모 순서
```bash
uv run python -m app.fleet init-router                     # 라우터 1회
uv run python -m app.deployer ../sample-apps/healthy node  # → http://healthy.<ip>.nip.io
uv run python -m app.fleet watch --interval 5              # 터미널 1
uv run python -m app.fleet stress healthy --seconds 120 --concurrency 32   # 터미널 2 → 20초쯤 뒤 scale_out
uv run python -m app.fleet status
```

## 한계와 다음 단계
- 상태(DB)가 있는 앱은 복제하면 안 된다 → analyzer의 "상태 있음" 신호를 받아 `max_replicas=1`로 고정 (미구현).
- scale-in은 최신 복제본이 2분 이상 살아 있고 CPU < 10%일 때만. 보수적.
- 라우터가 단일 장애점. 도메인이 생기면 Cloudflare DNS 뒤에 두고, 라우터를 2대로.
- 노드 장애 감지·재배치는 미구현(설계: probe 실패 N회 → 해당 노드 복제본을 다른 노드로).

---

## 데이터베이스 프로비저닝 (10/10 추가) — "SQLite → 관리형 Postgres"

앱이 `DATABASE_URL`을 읽거나 Postgres 드라이버를 쓰면(`detect_database`) deployer가 타깃에 맞는 DB를 만들어 주입한다.
SQLite 경로를 하드코딩한 앱은 대상이 아니다(읽지도 않는 URL을 넣는 것은 눈속임이므로). 그런 앱은 analyzer/healer가 코드를 고칠 영역.

| 타깃 | DB | 방법 | 데이터 보존 |
|---|---|---|---|
| local | `postgres:16-alpine` 사이드카 `<app>-db` | docker network `cloudmorph`, 볼륨 `<app>-dbdata` | 재배포(healer 재시도) 후에도 유지. `cleanup`에서만 삭제 |
| node | 같은 방식을 노드에서 SSH로 | 노드마다 사이드카 | 앱이 `stateful`로 표시되어 **자동 확장 제외** |
| cloudrun | Cloud SQL 인스턴스 `cloudmorph-pg`(공유) | `gcloud sql databases/users create` → `--add-cloudsql-instances` + `DATABASE_URL=postgresql://u:p@/db?host=/cloudsql/<conn>` | 관리형 |

실측(sample-apps/guestbook): local 9.7초, cloudrun 44초(앱별 DB·계정 생성 포함), node 20초. 세 곳 모두 글 쓰기 → 읽기 확인, local은 재배포 후 데이터 유지 확인.
Cloud SQL 생성 시 `--edition=ENTERPRISE`를 줘야 `db-f1-micro`를 쓸 수 있고, Cloud Run 서비스 계정에 `roles/cloudsql.client`가 필요하다.
