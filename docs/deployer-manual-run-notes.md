# deployer 수동 배포 기록 (10/7~10/8, 전동훈)

> deployer.py를 쓰기 전에 전 과정을 손으로 끝까지 해 본 기록. 경로 표기 중 `../deployer.py`, `fixtures/`는 작업 당시 로컬 폴더 기준이며, 저장소에서는 `backend/app/deployer.py`, `sample-apps/`에 해당한다.


- 날짜: 2026-10-07
- 환경: macOS 26.5.2, Apple Silicon (arm64), Docker 설치됨, gcloud/cloudflared 미설치 상태에서 시작
- GCP 리전: asia-northeast3
- 목적: 로컬 Docker → cloudflared 터널 → Artifact Registry → Cloud Run 배포 → 고의 실패 → 정리까지 한 번 손으로 끝내고 기록한다.

---

## 1단계. 최소 Flask 앱 + Dockerfile 작성

### 실행한 명령어
```bash
mkdir -p hello-flask && cd hello-flask
# app.py, requirements.txt, Dockerfile, .dockerignore 작성 (내용은 저장소 참고)
python3 -m py_compile app.py   # 문법 검사
```

### 만든 파일
| 파일 | 역할 |
|---|---|
| app.py | `/` 와 `/health` 두 경로. PORT 환경변수를 읽고 기본값 8080 |
| requirements.txt | flask 3.0.3, gunicorn 22.0.0 |
| Dockerfile | python:3.12-slim 기반, gunicorn으로 `$PORT`에 바인딩 |
| .dockerignore | 캐시, NOTES.md 등 이미지에서 제외 |

### 설계 메모
- Cloud Run은 컨테이너에 `PORT` 환경변수를 주입하고 그 포트로 요청을 보낸다. 그래서 포트를 하드코딩하지 않고 환경변수에서 읽는다.
- Dockerfile의 CMD는 shell 형식(`exec gunicorn ... $PORT`)으로 써야 `$PORT`가 치환된다. JSON 배열 형식이면 환경변수가 치환되지 않는다.
- `exec`를 앞에 붙이면 gunicorn이 PID 1이 되어 Cloud Run이 보내는 SIGTERM을 직접 받는다.
- `/` 응답에 `arch`(platform.machine())를 넣어 두었다. 5단계에서 Cloud Run 응답이 `x86_64`로 나오면 교차 빌드가 제대로 된 것이다.
- Flask 개발 서버 대신 gunicorn을 쓴다. 개발 서버는 단일 스레드이고 운영용 경고가 뜬다.

### 걸린 시간
- 약 3분

### 막혔던 부분
- 없음

---

## 2단계. 로컬 Docker 빌드 및 실행

### 실행한 명령어
```bash
docker info --format '{{.ServerVersion}} / {{.OSType}}/{{.Architecture}}'   # 데몬 확인 → 29.6.2 / linux/aarch64
docker build -t hello-flask:local .                                          # 약 28초
docker run -d --name hello-local -p 8080:8080 hello-flask:local
curl -s -w "\nHTTP %{http_code}\n" http://localhost:8080/
curl -s -w "\nHTTP %{http_code}\n" http://localhost:8080/health
# PORT 환경변수 반영 검증
docker run -d --name hello-local-9090 -e PORT=9090 -p 9090:9090 hello-flask:local
curl -s http://localhost:9090/health
docker rm -f hello-local-9090
docker logs hello-local
```

### 결과
- `GET /` → `{"arch":"aarch64","message":"hello from flask","port":8080}` HTTP 200
- `GET /health` → `{"status":"ok"}` HTTP 200
- `PORT=9090`으로 띄운 컨테이너도 9090에서 정상 응답 → 환경변수 기반 포트 바인딩 확인
- 로그에 gunicorn이 PID 1로 `0.0.0.0:8080`에서 리스닝하는 것 확인
- `arch`가 `aarch64`로 나옴. 즉 이 이미지는 Apple Silicon용이며 Cloud Run(x86_64)에서는 그대로 실행할 수 없다. 5단계에서 `--platform linux/amd64`로 다시 빌드해야 한다.

### 걸린 시간
- 빌드 약 30초, 실행·검증 약 1분

### 막혔던 부분
- 없음. 빌드 시 `JSONArgsRecommended` 경고가 뜨지만 `$PORT` 치환을 위해 셸 형식 CMD를 의도적으로 사용한 것이므로 무시.

### deployer.py 메모
- 로컬 배포의 성공 판정은 "컨테이너 기동 후 `/health`가 200을 돌려주는가"로 하면 된다. 기동 직후에는 몇 초 대기 또는 재시도가 필요하다.
- 실패 지점 구분: `docker build` 실패(Dockerfile/의존성 문제) vs `docker run` 직후 종료(앱 런타임 문제). 후자는 `docker logs`로 원인 수집.

---

## 3단계. cloudflared 임시 터널로 외부 공개

### 실행한 명령어
```bash
brew install cloudflared                      # 약 13초, 버전 2026.10.0
nohup cloudflared tunnel --url http://localhost:8080 > cloudflared.log 2>&1 &
grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' cloudflared.log | head -n 1   # 주소 추출
curl -s -w "\nHTTP %{http_code}\n" "$URL/"
curl -s -w "\nHTTP %{http_code}\n" "$URL/health"
# 종료할 때
pkill -f "cloudflared tunnel --url"
```

### 결과
- 발급 주소: https://intensity-believes-physician-pay.trycloudflare.com (프로세스를 끄면 소멸하는 임시 주소)
- 외부 주소 `GET /` → HTTP 200, `GET /health` → HTTP 200
- 주소 발급까지 약 2초, 실제 접속 가능까지 추가 몇 초

### 걸린 시간
- 설치 포함 약 2분

### 막혔던 부분
- 없음. 다만 로그에 "it may take some time to be reachable"라고 나오듯 주소 발급 직후 바로 요청하면 실패할 수 있어 3초 대기 후 요청했다.

### deployer.py 메모
- 로컬 타깃의 "공개 주소"는 cloudflared 로그를 파싱해서 얻는다. 정규식: `https://[a-z0-9-]+\.trycloudflare\.com`
- cloudflared 프로세스는 배포 서비스가 살아 있는 동안 유지해야 하므로 PID를 기록해 두고, 정리 단계에서 종료한다.
- 계정 없는 Quick Tunnel은 가동 보장이 없다. 데모용으로는 충분하지만 발표 직전에 새로 띄우는 편이 안전하다.
- 주소가 매번 바뀐다. 고정 주소가 필요하면 Cloudflare 계정으로 Named Tunnel을 만들어야 한다 (이번 범위 밖).

---

## 4단계. gcloud CLI 설치 및 GCP 프로젝트 준비

### 실행한 명령어 (설치 부분)
```bash
brew install --cask gcloud-cli     # 약 52초, Google Cloud SDK 588.0.0
gcloud --version
gcloud auth list                   # 로그인 계정 확인
gcloud config get-value project    # 현재 프로젝트 확인
```

### 로그인·프로젝트 설정
```bash
gcloud auth login                        # 브라우저 로그인 (대학교 Workspace 계정)
gcloud auth application-default login    # 라이브러리/도구용 자격 증명
gcloud config set project sylvan-task-457107-e5
gcloud config set run/region asia-northeast3
gcloud config set artifacts/location asia-northeast3
gcloud projects describe sylvan-task-457107-e5     # ACTIVE 확인
gcloud billing projects describe sylvan-task-457107-e5   # billingEnabled: False → 결제 연결 필요
gcloud billing accounts list                        # 비어 있음
```

### 막혔던 부분
- 프로젝트는 있지만 결제 계정이 연결되어 있지 않아 Cloud Run / Artifact Registry API를 켤 수 없는 상태.
- 로그인 계정이 대학교 Google Workspace 계정이라, 결제 계정 생성 가능 여부는 학교 관리자 정책에 따라 달라짐.
  → 콘솔(https://console.cloud.google.com/billing)에서 결제 계정 생성·연결을 시도. 차단되면 개인 Gmail 계정으로 전환.

### 계정 전환 (학교 계정 → 개인 계정)
- 학교 계정에서 결제 계정 생성 시 ₩40,000 일회성 선결제(보증금, 해지 시 환불) 안내가 나옴. 개인 계정으로 전환하기로 결정.
- `gcloud auth login`을 다시 실행하면 로그아웃 없이 계정이 추가되고 활성 계정이 바뀐다. `gcloud auth application-default login`도 같은 계정으로 재실행 필요.
```bash
gcloud auth login
gcloud auth application-default login
gcloud auth list                                   # 활성 계정 확인
gcloud config set project weighty-forest-457108-r2
gcloud billing projects describe weighty-forest-457108-r2   # billingEnabled: False, 결제 계정은 연결되어 있음
gcloud billing accounts list                                 # 결제 계정 2개 모두 open: False (닫힘)
```

### 막혔던 부분
- 새 프로젝트에 결제 계정이 연결은 되어 있으나 그 결제 계정이 닫힌 상태(open: false)라 결제가 비활성.
  가입 절차(선결제/카드 확인)가 완료되지 않은 것으로 추정. 콘솔 결제 화면에서 계정을 다시 열거나 새로 만들어야 함.

### 결제 연결 후 진행 (개인 계정, 프로젝트 weighty-forest-457108-r2)
```bash
gcloud billing projects describe weighty-forest-457108-r2        # billingEnabled: True 확인 후 진행
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com   # 약 16초
gcloud artifacts repositories create hackathon \
  --repository-format=docker --location=asia-northeast3 \
  --description="SoftBank hackathon images"                       # 약 24초
gcloud artifacts repositories list --location=asia-northeast3
gcloud auth configure-docker asia-northeast3-docker.pkg.dev --quiet   # ~/.docker/config.json 에 credHelper 추가
```

### 결과
- API 3개 활성화 확인 (run, artifactregistry, cloudbuild)
- 저장소 `asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon` 생성
- Docker credHelpers에 `asia-northeast3-docker.pkg.dev: gcloud` 등록됨

### 걸린 시간
- 설치·로그인 포함 전체 4단계 약 40분. 이 중 대부분은 결제 계정 문제 해결에 소요. 순수 명령 실행 시간은 2분 미만.

### deployer.py 메모
- 사전 조건 점검 함수가 필요: gcloud 설치 여부, 활성 계정 존재, 프로젝트 설정, `billingEnabled`, 필요한 API 활성화, 저장소 존재, credHelper 등록. 하나라도 빠지면 배포 전에 명확한 메시지로 중단.
- 결제 미연결은 코드로 해결할 수 없는 "사용자 개입 필요" 실패 유형이다. 자동 재시도 대상이 아니다.

---

## 5단계. amd64 이미지 빌드 → Artifact Registry 업로드 → Cloud Run 배포

### 실행한 명령어
```bash
IMAGE=asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon/hello-flask:v1
docker build --platform linux/amd64 -t "$IMAGE" .          # 약 32초
docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}'   # linux/amd64 확인
docker push "$IMAGE"                                        # 약 37초
gcloud artifacts docker images list asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon --include-tags
gcloud run deploy hello-flask --image "$IMAGE" --region asia-northeast3 \
  --allow-unauthenticated --port 8080 --quiet                # 약 18초
gcloud run services describe hello-flask --region asia-northeast3 --format='value(status.url)'
curl -s -w "\nHTTP %{http_code}\n" "$URL/"
curl -s -w "\nHTTP %{http_code}\n" "$URL/health"
gcloud run services logs read hello-flask --region asia-northeast3 --limit 10   # 앱 로그 확인
gcloud run revisions list --service hello-flask --region asia-northeast3
```

### 결과
- 서비스 URL: https://hello-flask-5xcwktil7a-du.a.run.app
- 배포 출력에 찍힌 URL: https://hello-flask-321519301455.asia-northeast3.run.app (프로젝트 번호 기반 형식, 둘 다 유효)
- `GET /` → `{"arch":"x86_64","message":"hello from flask","port":8080}` HTTP 200
- `GET /health` → HTTP 200
- `arch`가 로컬(aarch64)과 달리 x86_64 → 교차 빌드가 Cloud Run에서 실제로 실행됨을 확인
- 리비전 hello-flask-00001-57x 가 트래픽 100%

### 걸린 시간
- 빌드 32초 + 업로드 37초 + 배포 18초 = 약 1분 30초. 명령 입력과 확인 포함 약 5분.

### 막혔던 부분
- 없음. 결제와 API가 준비된 뒤에는 한 번에 통과.

### deployer.py 메모
- Cloud Run 타깃의 단계 분리: (a) amd64 빌드 (b) push (c) deploy (d) URL 조회 (e) /health 확인. 각 단계의 명령 반환 코드로 실패 지점을 식별.
- `--allow-unauthenticated` 없이 배포하면 403이 나온다. 심사 기준 "누구나 접근 가능"을 위해 필수.
- 성공 URL은 `gcloud run services describe ... --format='value(status.url)'`로 안정적으로 얻는다. 배포 출력 파싱보다 확실하다.
- `--quiet`를 붙여야 대화형 프롬프트(리전 선택, 미인증 허용 여부 등)가 뜨지 않아 자동화에 적합하다.


---

## 6단계. 고장 낸 버전(v2) 배포로 실패 유도 및 오류 수집

### 고장 방식
- `broken/app.py`: 원본에서 `from flask import Flask, jsonify` 한 줄 삭제 → 모듈 import 시점에 NameError.
- 같은 Dockerfile로 `hello-flask:v2` 태그를 amd64로 빌드해 저장소에 업로드.

### 실행한 명령어
```bash
mkdir -p broken && cp Dockerfile requirements.txt .dockerignore broken/
sed '/^from flask import Flask, jsonify$/d' app.py > broken/app.py
IMAGE2=asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon/hello-flask:v2
docker build --platform linux/amd64 -t "$IMAGE2" broken/
# 6-a. 로컬에서 실패 재현
docker run -d --name hello-broken --platform linux/amd64 -p 8081:8080 "$IMAGE2"; sleep 6
docker inspect hello-broken --format '{{.State.Status}} (exit {{.State.ExitCode}})'   # exited (exit 3)
docker logs hello-broken > broken_local_logs.txt 2>&1
docker rm -f hello-broken
docker push "$IMAGE2"
# 6-b. Cloud Run 배포 (기동 검사 없음) → 배포는 "성공"하지만 서비스는 500
gcloud run deploy hello-flask --image "$IMAGE2" --region asia-northeast3 --port 8080 --quiet > deploy_v2.log 2>&1; echo $?   # 0
curl -s -w "\nHTTP %{http_code}\n" https://hello-flask-5xcwktil7a-du.a.run.app/health                                  # HTTP 500
gcloud run services logs read hello-flask --region asia-northeast3 --limit 60       # 사람이 읽기 좋은 로그
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="hello-flask" AND resource.labels.revision_name="hello-flask-00002-bs5" AND severity>=ERROR' --limit 20 --freshness 30m --format='value(timestamp,severity,textPayload)'   # 자동화용 구조화 조회
# 6-c. 롤백
gcloud run services update-traffic hello-flask --to-revisions hello-flask-00001-57x=100 --region asia-northeast3 --quiet   # 약 7초, /health 200 복귀
# 6-d. 기동 검사(startup probe)를 붙여 다시 실패 유도 → 이번에는 배포 명령 자체가 실패
PROBE="httpGet.path=/health,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=2,timeoutSeconds=2,failureThreshold=3"
gcloud run deploy hello-flask --image "$IMAGE1" --region asia-northeast3 --port 8080 --quiet --startup-probe="$PROBE"   # 정상 v1에 probe 적용 (리비전 00004)
gcloud run services update-traffic hello-flask --to-latest --region asia-northeast3 --quiet                           # 트래픽이 최신 리비전을 따라가도록 복귀
gcloud run deploy hello-flask --image "$IMAGE2" --region asia-northeast3 --port 8080 --quiet --startup-probe="$PROBE" > deploy_v2_probe.log 2>&1; echo $?   # 1 (약 8초)
gcloud run revisions describe hello-flask-00005-ckv --region asia-northeast3 --format='value(status.conditions[0].reason)'   # HealthCheckContainerError
```

### 결과 요약
| 시도 | 배포 명령 종료 코드 | 트래픽 | 서비스 /health | 실패를 알 수 있는 곳 |
|---|---|---|---|---|
| 6-a 로컬 docker run | (컨테이너 exit 3) | - | 응답 없음 | `docker inspect` 종료 코드 + `docker logs` |
| 6-b Cloud Run, probe 없음 | **0 (성공으로 보임)** | v2로 100% 이동 | **HTTP 500** | 배포 후 curl, Cloud Run 로그 |
| 6-c 롤백 | 0 | v1 100% | HTTP 200 | - |
| 6-d Cloud Run, HTTP probe | **1 (실패)** | v1(00004)에 그대로 | HTTP 200 유지 | 배포 명령 stderr + 리비전 조건 + Cloud Run 로그 |

### 핵심 발견
1. **배포 명령의 성공은 앱의 정상 동작을 보장하지 않는다.** gunicorn 마스터가 포트를 먼저 열고 워커가 나중에 앱을 import 하므로, Cloud Run 기본 기동 검사(TCP 포트 확인)는 통과한다. 그 결과 고장 난 리비전이 트래픽 100%를 받았다.
2. **HTTP 기동 검사(`--startup-probe=httpGet.path=/health`)를 붙이면 Cloud Run이 앱 응답까지 확인**하고, 실패 시 배포 명령이 종료 코드 1로 끝나며 트래픽은 이전 리비전에 남는다. 서비스 중단 없이 실패를 감지할 수 있다.
3. 트래픽을 특정 리비전에 고정(`--to-revisions`)한 상태에서는 새 리비전의 인스턴스가 뜨지 않아 기동 검사가 실행되지 않는다(리비전 00003이 로그 없이 Retired). 기동 검사로 실패를 잡으려면 트래픽이 `LATEST`를 따라가는 상태여야 한다.
4. 롤백은 `update-traffic --to-revisions <이전 리비전>=100` 한 줄, 약 7초.

### 걸린 시간
- 로컬 재현 + 업로드 약 2분, Cloud Run 배포·로그 수집·롤백 약 5분, 기동 검사 실험 약 5분. 중간에 gcloud 프로세스가 출력 없이 20분 이상 멈춘 일이 있어 강제 종료 후 재시도(아래 참고).

### 막혔던 부분
- 6-b에서 배포가 성공으로 끝나 당황. 원인은 위 "핵심 발견 1". 해결은 HTTP 기동 검사 추가.
- 6-d 첫 시도에서 리비전 00003이 기동 검사 없이 바로 Retired 됨. 원인은 트래픽 고정. `--to-latest`로 복귀 후 해결.
- 6-d 재시도 중 `gcloud run deploy`가 리비전도 만들지 않고 출력 없이 22분 멈춤(백그라운드 작업 종료 코드 143). `pkill`로 종료 후 같은 명령을 다시 실행하니 8초 만에 정상 실패. 원인 미상(네트워크 추정). deployer에서는 배포 명령에 타임아웃을 반드시 둬야 한다.

### 수집한 오류 출력 원문

#### (1) 로컬 `docker logs hello-broken` (컨테이너 exit 3)
```text
[2026-10-07 10:01:39 +0000] [1] [INFO] Starting gunicorn 22.0.0
[2026-10-07 10:01:39 +0000] [1] [INFO] Listening at: http://0.0.0.0:8080 (1)
[2026-10-07 10:01:39 +0000] [1] [INFO] Using worker: gthread
[2026-10-07 10:01:39 +0000] [7] [INFO] Booting worker with pid: 7
[2026-10-07 10:01:39 +0000] [7] [ERROR] Exception in worker process
Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/gunicorn/arbiter.py", line 609, in spawn_worker
    worker.init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/gthread.py", line 95, in init_process
    super().init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 134, in init_process
    self.load_wsgi()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 146, in load_wsgi
    self.wsgi = self.app.wsgi()
                ^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/base.py", line 67, in wsgi
    self.callable = self.load()
                    ^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 58, in load
    return self.load_wsgiapp()
           ^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 48, in load_wsgiapp
    return util.import_app(self.app_uri)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/util.py", line 371, in import_app
    mod = importlib.import_module(module)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1331, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 935, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 999, in exec_module
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
  File "/app/app.py", line 5, in <module>
    app = Flask(__name__)
          ^^^^^
NameError: name 'Flask' is not defined
[2026-10-07 10:01:39 +0000] [7] [INFO] Worker exiting (pid: 7)
[2026-10-07 10:01:39 +0000] [1] [ERROR] Worker (pid:7) exited with code 3
[2026-10-07 10:01:39 +0000] [1] [ERROR] Shutting down: Master
[2026-10-07 10:01:39 +0000] [1] [ERROR] Reason: Worker failed to boot.
```

#### (2) 기동 검사 없는 Cloud Run 배포 명령 출력 (`deploy_v2.log`, 종료 코드 0)
```text
Deploying container to Cloud Run service [hello-flask] in project [weighty-forest-457108-r2] region [asia-northeast3]
Deploying...
Creating Revision.....................................................................done
Routing traffic.....done
Done.
Service [hello-flask] revision [hello-flask-00002-bs5] has been deployed and is serving 100 percent of traffic.
Service URL: https://hello-flask-321519301455.asia-northeast3.run.app
```

#### (3) Cloud Run 로그 — `gcloud run services logs read` (리비전 00002, 재시작 반복 부분)
```text
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/gthread.py", line 95, in init_process
    super().init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 134, in init_process
    self.load_wsgi()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 146, in load_wsgi
    self.wsgi = self.app.wsgi()
                ^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/base.py", line 67, in wsgi
    self.callable = self.load()
                    ^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 58, in load
    return self.load_wsgiapp()
           ^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 48, in load_wsgiapp
    return util.import_app(self.app_uri)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/util.py", line 371, in import_app
    mod = importlib.import_module(module)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1331, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 935, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 999, in exec_module
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
  File "/app/app.py", line 5, in <module>
    app = Flask(__name__)
          ^^^^^
NameError: name 'Flask' is not defined
2026-10-07 10:05:57 [2026-10-07 10:05:57 +0000] [2] [INFO] Worker exiting (pid: 2)
2026-10-07 10:05:57 [2026-10-07 10:05:57 +0000] [1] [ERROR] Worker (pid:2) exited with code 3
2026-10-07 10:05:57 [2026-10-07 10:05:57 +0000] [1] [ERROR] Shutting down: Master
2026-10-07 10:05:57 [2026-10-07 10:05:57 +0000] [1] [ERROR] Reason: Worker failed to boot.
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [INFO] Starting gunicorn 22.0.0
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [INFO] Listening at: http://0.0.0.0:8080 (1)
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [INFO] Using worker: gthread
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [12] [INFO] Booting worker with pid: 12
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [12] [ERROR] Exception in worker process
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [12] [INFO] Worker exiting (pid: 12)
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [ERROR] Worker (pid:12) exited with code 3
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [ERROR] Shutting down: Master
2026-10-07 10:05:59 [2026-10-07 10:05:59 +0000] [1] [ERROR] Reason: Worker failed to boot.
```

#### (4) Cloud Run 로그 — `gcloud logging read ... severity>=ERROR` (리비전 00002, 구조화 조회 앞부분)
```text
2026-10-07T10:05:59.212488Z	ERROR	Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/gunicorn/arbiter.py", line 609, in spawn_worker
    worker.init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/gthread.py", line 95, in init_process
    super().init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 134, in init_process
    self.load_wsgi()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 146, in load_wsgi
    self.wsgi = self.app.wsgi()
                ^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/base.py", line 67, in wsgi
    self.callable = self.load()
                    ^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 58, in load
    return self.load_wsgiapp()
           ^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/app/wsgiapp.py", line 48, in load_wsgiapp
    return util.import_app(self.app_uri)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/site-packages/gunicorn/util.py", line 371, in import_app
    mod = importlib.import_module(module)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.12/importlib/__init__.py", line 90, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1387, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1331, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 935, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 999, in exec_module
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
  File "/app/app.py", line 5, in <module>
    app = Flask(__name__)
          ^^^^^
NameError: name 'Flask' is not defined
2026-10-07T10:05:57.244642Z	ERROR	Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/gunicorn/arbiter.py", line 609, in spawn_worker
    worker.init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/gthread.py", line 95, in init_process
    super().init_process()
  File "/usr/local/lib/python3.12/site-packages/gunicorn/workers/base.py", line 134, in init_process
    self.load_wsgi()
  File "/usr/local/lib
```

#### (5) HTTP 기동 검사 붙인 Cloud Run 배포 명령 출력 (`deploy_v2_probe.log`, 종료 코드 1)
```text
Deploying container to Cloud Run service [hello-flask] in project [weighty-forest-457108-r2] region [asia-northeast3]
Deploying...
Creating Revision...................................................failed
Deployment failed
ERROR: (gcloud.run.deploy) The user-provided container failed the configured startup probe checks. Logs for this revision might contain more information.

Logs URL: https://console.cloud.google.com/logs/viewer?project=weighty-forest-457108-r2&resource=cloud_run_revision/service_name/hello-flask/revision_name/hello-flask-00005-ckv&advancedFilter=resource.type%3D%22cloud_run_revision%22%0Aresource.labels.service_name%3D%22hello-flask%22%0Aresource.labels.revision_name%3D%22hello-flask-00005-ckv%22 
For more troubleshooting guidance, see https://cloud.google.com/run/docs/troubleshooting#container-failed-to-start
```

#### (6) 실패 리비전 00005의 상태 조건
```text
Ready  False  HealthCheckContainerError
The user-provided container failed the configured startup probe checks. Logs for this revision might contain more information.
```

### deployer.py 메모
- 실패 유형을 세 층으로 구분해 반환한다: `build`(docker build 실패) / `push`(인증·네트워크) / `deploy`(gcloud 종료 코드 ≠ 0, stderr 원문) / `runtime`(배포는 됐지만 /health 비정상, Cloud Run 로그 원문).
- 오류 원문 수집 명령: 로컬은 `docker logs <컨테이너>`, Cloud Run은 `gcloud logging read` 에 리비전 이름 필터 + `severity>=ERROR`. 리비전 이름은 배포 stderr 또는 `status.latestCreatedRevisionName`에서 얻는다.
- AI 자가 수정에 넘길 때는 (4)처럼 Traceback의 마지막 줄(`NameError: name 'Flask' is not defined`)과 파일·행 번호(`/app/app.py, line 5`)만 뽑아도 충분하다.
- Cloud Run 배포 시 항상 `--startup-probe=httpGet.path=/health...`를 붙이고, 배포 명령에 타임아웃(예: 300초)을 건다.

---

## 7단계. 리소스 정리 명령 (실행은 사용자가 결정)

### 로컬 (비용 없음, 언제든 실행 가능)
```bash
pkill -f "cloudflared tunnel --url"          # 임시 터널 종료 (공개 주소 소멸)
docker rm -f hello-local                     # 로컬 컨테이너 중지·삭제
docker rmi hello-flask:local \
  asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon/hello-flask:v1 \
  asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon/hello-flask:v2   # 로컬 이미지 삭제
```

### Cloud Run (서비스 유지 시 요청 없으면 과금 0, 삭제하면 URL 소멸)
```bash
gcloud run services delete hello-flask --region asia-northeast3 --project weighty-forest-457108-r2 --quiet
```

### Artifact Registry (저장 용량만큼 소액 과금. 이미지만 지우거나 저장소째 삭제)
```bash
# 이미지만 삭제
gcloud artifacts docker images delete \
  asia-northeast3-docker.pkg.dev/weighty-forest-457108-r2/hackathon/hello-flask --delete-tags --quiet
# 저장소째 삭제 (안의 이미지 모두 삭제됨)
gcloud artifacts repositories delete hackathon --location asia-northeast3 --project weighty-forest-457108-r2 --quiet
```

### 정리하지 않아도 되는 것
- API 사용 설정: 켜 둔 상태 자체는 무료. 해커톤 본 개발에서 다시 쓴다.
- Docker credHelper 설정(`~/.docker/config.json`): 로컬 설정이며 다시 쓴다.
- gcloud 로그인·프로젝트 설정: 다시 쓴다.

### 권장
- 해커톤 당일(10/10~11)까지 Cloud Run 서비스와 저장소는 **유지**하는 편이 유리하다. 데모 리허설에 그대로 쓸 수 있고, 유지 비용은 사실상 0원이다.
- 해커톤이 끝나면 위 명령으로 전부 삭제하고, 결제 계정도 닫으면 선결제가 있었다면 환불된다.

---

## 마무리 정리

### 로컬 배포와 Cloud Run 배포의 차이

| 항목 | 로컬 Docker (+ cloudflared) | Cloud Run |
|---|---|---|
| 이미지 아키텍처 | 호스트 그대로 (arm64) | **linux/amd64 필수**. `--platform linux/amd64`로 교차 빌드 |
| 포트 | `-p 8080:8080`으로 내가 지정. 컨테이너 안은 PORT 환경변수 | Cloud Run이 PORT를 주입. `--port 8080`으로 알려 줌 |
| 공개 주소 | cloudflared 로그에서 `*.trycloudflare.com` 추출. 매번 바뀌고 프로세스 종료 시 소멸 | `gcloud run services describe --format='value(status.url)'`. 서비스가 있는 한 고정 |
| 접근 제어 | 터널이 열려 있으면 누구나 | `--allow-unauthenticated` 없으면 403 |
| 성공 판정 | 컨테이너 상태 running + `/health` 200 | 배포 종료 코드 0 **그리고** `/health` 200 (둘 다 필요) |
| 실패 감지 | `docker inspect` 종료 코드, `docker logs` | HTTP startup probe → 배포 명령 종료 코드 1, 리비전 조건 `HealthCheckContainerError`, `gcloud logging read` |
| 로그 확인 | `docker logs <컨테이너>` | `gcloud run services logs read` (읽기용), `gcloud logging read` + 리비전 필터 (자동화용) |
| 롤백 | 이전 이미지로 컨테이너 재실행 | `update-traffic --to-revisions <이전>=100`, 약 7초 |
| 소요 시간 (앱 변경 1회 기준) | 빌드 ~30초 + 실행 ~3초 ≈ **35초** | amd64 빌드 ~32초 + push ~37초 + deploy ~18초 ≈ **1분 30초** |
| 사전 준비 | Docker, cloudflared | gcloud 로그인, 프로젝트, **결제 연결**, API 3개, 저장소, credHelper (처음 1회, 이번엔 약 40분) |
| 종료 시 과금 | 없음 | 요청 없으면 0. 저장소 용량만 소액 |

### deploy(project_dir, target) 함수 단계 제안

```
deploy(project_dir, target) -> DeployResult
  target: "local" | "cloudrun"

  0. preflight(target)                      # 사전 조건 점검. 실패하면 "사용자 개입 필요"로 즉시 반환
       공통: Dockerfile 존재, docker 데몬 응답
       local: cloudflared 설치
       cloudrun: gcloud 설치·활성 계정·프로젝트·billingEnabled·API 3개·저장소·credHelper
  1. build(project_dir, image_tag, platform)  # local: 호스트 아키텍처 / cloudrun: linux/amd64
       실패 → stage="build", error=docker build stderr
  2. (cloudrun만) push(image_tag)
       실패 → stage="push"
  3. run_or_deploy
       local: docker run -d -e PORT -p <호스트포트>:<PORT>  → container_id
       cloudrun: gcloud run deploy --quiet --port --allow-unauthenticated
                 --startup-probe=httpGet.path=/health,...  (타임아웃 300초)
       실패 → stage="deploy", error=stderr 원문 (+ cloudrun은 리비전 이름 추출)
  4. expose
       local: cloudflared tunnel --url → 로그에서 trycloudflare URL 추출, PID 보관
       cloudrun: services describe → status.url
  5. verify(url)                            # /health 200을 최대 N초 동안 재시도
       실패 → stage="runtime", error=
           local: docker logs <container_id>
           cloudrun: gcloud logging read (revision 필터, severity>=ERROR)
       cloudrun은 verify 실패 시 자동 rollback(update-traffic → 이전 리비전) 옵션
  6. return DeployResult(ok, url, stage, error, logs, started_at, elapsed, handles)
       handles: container_id / tunnel_pid / revision_name  (정리·롤백에 사용)

  각 단계는 (명령어, 종료 코드, 소요 시간, stdout/stderr 요약)을 배포 로그에 기록한다.
  → 심사 기준 "배포 작업 로그 기록"에 대응.
```

### AI 자가 수정 루프에 넘길 때
- `stage`로 수정 대상을 고른다: build → Dockerfile/requirements, runtime → 앱 코드, deploy(probe 실패) → 앱 코드 또는 포트 설정, push/preflight → 코드 수정으로 해결 불가(사용자 안내).
- `error`는 원문 전체를 보관하되, 모델에 넘기는 요약은 Traceback 마지막 줄 + 파일·행 번호로 충분했다 (`NameError: name 'Flask' is not defined`, `/app/app.py line 5`).
- 재배포 시 태그를 v1, v2, v3…으로 올려야 Cloud Run이 새 리비전을 만들고 롤백 대상이 보존된다.

---

## 2일차 (10/8). deployer.py 작성 및 실패 유형 수집

### 만든 것
- `../deployer.py` (약 530줄, 표준 라이브러리만 사용)
  - `deploy(project_dir, target, config=None, **overrides) -> DeployResult`
  - 단계: preflight → build → (push) → deploy → expose → verify
  - 실패 종류(error_kind): user_action_required / build_error / push_error / deploy_error / runtime_error / timeout / internal_error
  - 결과는 `<project_dir>/.deployer/last_result.json`에 항상 저장. CLI `python deployer.py cleanup <dir>`로 local 컨테이너·터널 정리
- `../fixtures/` 고장 앱 4종 (hello-flask에서 한 줄씩만 바꿈)

### 실행한 명령어
```bash
python3 deployer.py hello-flask local                      # 성공 경로
python3 deployer.py fixtures/broken-import local --no-tunnel --verify-timeout 20
python3 deployer.py fixtures/broken-requirements local --no-tunnel --verify-timeout 20
python3 deployer.py fixtures/broken-port local --no-tunnel --verify-timeout 20
python3 deployer.py fixtures/broken-health local --no-tunnel --verify-timeout 20
python3 deployer.py cleanup <dir>                          # 각 테스트 뒤 정리
```

### 실패 유형 매트릭스 (local 타깃 실측)
| fixture | 고장 내용 | stage | error_kind | 판별 근거 | 소요 |
|---|---|---|---|---|---|
| broken-requirements | `flask==99.99.99` | build | build_error | docker build 종료 코드 1, pip 오류 원문 | 1.6초 |
| broken-import | `from flask import` 삭제 | verify | runtime_error | 컨테이너 exited(3), 로그에 `NameError` | 3.9초 |
| broken-port | PORT 무시, 5000 고정 | verify | runtime_error | 컨테이너 running인데 ConnectionReset, 로그에 `Listening at :5000` | 21초(타임아웃) |
| broken-health | `/health`가 예외 | verify | runtime_error | HTTP 500, 로그에 요청 처리 Traceback | 21초(타임아웃) |

- 같은 runtime_error라도 `container status`(exited vs running)와 `last probe`(URLError / ConnectionReset / HTTP 500)로 세 가지가 구분된다. AI 모듈이 수정 대상을 고르는 데 쓸 수 있는 신호다.

### 막혔던 부분과 해결
1. **공개 주소 검증 실패(URLError)**: 터널은 등록됐고 IP로 직접 붙으면 200인데 호스트 이름 해석이 안 됨. 새 `*.trycloudflare.com` 이름이 이 네트워크 DNS(8.8.8.8)에 전파되는 데 1분 이상 걸렸고, 이후 저절로 해결됨.
   → 공개 주소는 60초 재시도 후, 실패해도 터널 로그에 `Registered tunnel connection`이 있으면 **성공 + warnings**로 처리. 로컬 헬스체크는 이미 통과한 상태이므로 배포 실패로 보지 않는다. 데모 때는 터널을 미리 띄워 두는 것이 안전.
2. **cleanup 하위 명령 오류**: argparse의 target choices와 충돌. argparse 진입 전에 `cleanup`을 분기해 해결.

### 걸린 시간
- 작성 약 20분, 테스트·디버깅 약 25분

### 아직 안 한 것
- cloudrun 타깃은 코드만 있고 실측 전. 새 리비전이 생기므로 실행 전 확인 필요.

### cloudrun 타깃 실측 (서비스 hello-flask, 리비전 00006~00009 생성)
```bash
python3 deployer.py hello-flask cloudrun --tag v3
python3 deployer.py fixtures/broken-import cloudrun --service hello-flask --tag v4-broken-import
python3 deployer.py fixtures/broken-health cloudrun --service hello-flask --tag v6-broken-health --no-probe --verify-timeout 30
```

| 사례 | 기동 검사 | stage | error_kind | 결과 | 소요 |
|---|---|---|---|---|---|
| hello-flask v3 | HTTP /health | verify | - | ok, URL 발급, 리비전 00006 | 27초 |
| broken-import | HTTP /health | deploy | deploy_error | gcloud 종료 코드 1, 리비전 00007 `HealthCheckContainerError`, 로그에서 NameError 수집, 트래픽은 00006 유지 | 64초 |
| broken-health | TCP(기본) | verify | runtime_error | 배포는 성공(00009), /health 500 → 30초 후 **자동 롤백 → 00006**, 로그에서 RuntimeError 수집 | 81초 |

- 세 경우 모두 서비스 URL은 끊기지 않고 정상 버전이 응답했다.
- `gcloud logging read`가 20~45초 걸린다. 실패 시 총 소요의 절반이 로그 수집이다. 필요하면 재시도 횟수를 줄이거나 비동기로 돌릴 수 있다.

### 막혔던 부분과 해결 (cloudrun)
3. **`--no-probe`가 효과 없음**: Cloud Run은 기동 검사 설정을 이전 리비전에서 상속한다. 플래그를 빼는 것만으로는 안 꺼진다.
   - 처음에 `--clear-startup-probe`를 넣었는데 존재하지 않는 옵션이었다(도움말 확인을 건너뛴 실수. 리비전은 안 생김).
   - 도움말에서 `tcpSocket.port` 키를 확인하고, 끌 때는 `--startup-probe=tcpSocket.port=8080,...`으로 **TCP 검사로 덮어쓰는** 방식으로 해결. Cloud Run 기본 동작과 같다.
   - 교훈: 서비스 설정은 리비전 간에 누적된다. deployer가 매번 명시적으로 전체 설정을 넘겨야 결과가 예측 가능하다.

### deployer.py 최종 동작 요약
- local: build → run → verify(localhost) → tunnel → verify(public, 실패해도 터널 등록되면 warning으로 성공)
- cloudrun: preflight(7항목) → amd64 build → push → deploy(기동 검사 포함) → url → verify → 실패 시 이전 리비전으로 자동 롤백
- 모든 결과는 `.deployer/last_result.json`에 저장. `error`와 `app_logs`는 원문 그대로.

---

## 3일차 (10/9). 팀 계약(`DeployResult`)에 맞춰 이식 + Cloud Run 블루그린

- `backend/app/deployer.py`: `deploy(src_dir, dockerfile, target) -> DeployResult`. 예외를 던지지 않고 `stderr`에
  `[deployer] stage=.. kind=..` 헤더 + 진단 한 줄 + CLI 오류 + 런타임 로그를 합쳐 돌려준다. healer의 정규식 분류가 그대로 동작한다.
- **진단 휴리스틱 수정**: gunicorn은 마스터가 `Listening at`을 먼저 찍고 워커가 나중에 죽는다. "수신 중" 로그만 보고 포트 문제로
  진단하면 import 크래시가 port_binding으로 오분류된다. 컨테이너 종료 여부와 HTTP 5xx 여부를 함께 보도록 고쳤다.
- **트래픽 고정 함정**: 어제 `update-traffic --to-revisions X=100`으로 롤백한 뒤에는 새 배포가 리비전만 만들고 트래픽을 못 받는다.
  검증이 옛 리비전을 보고 "성공"으로 오판했다(리비전 00010, 00011).
- **해결 = 블루그린**: `gcloud run deploy --no-traffic --tag candidate` → candidate 태그 URL에서 헬스체크 → 통과 시
  `update-traffic --to-latest`로 승격. 실패하면 트래픽을 건드리지 않으므로 롤백 자체가 필요 없다. 실측: 정상 샘플 24초 승격,
  고장 샘플은 candidate(00017)에 머물고 공개 URL 무중단.
- 고장 샘플 5종의 실제 stderr: `backend/tests/fixtures/*.stderr.txt`. healer 분류 결과: broken → missing_dependency,
  broken-requirements → build_failure, broken-port → port_binding, broken-import / broken-health → unknown(LLM 폴백 대상).
