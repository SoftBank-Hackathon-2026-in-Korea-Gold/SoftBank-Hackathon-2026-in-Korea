# CloudMorph 시스템 요구사항 명세서 (Requirements Specification)

> **One Action, Infinite Clouds** — 사용자 코드를 정적 분석해 최적의 배포 환경(Local Docker vs Google Cloud Run)을 결정하고, 배포 실패 시 AI가 스스로 에러를 패치하는 **자가치유(Self-Healing)** 기능을 갖춘 시각적 원터치 배포 플랫폼.
>
> **SoftBank Hackathon 2026 in Korea · Term 2 · Team Gold**  
> **최종 갱신일:** 2026-10-09  
> **문서 버전:** v1.0.0

---

## 1. 프로젝트 개요 (Overview)

CloudMorph는 개발자가 소스코드 디렉토리 경로만 지정하면 **단 한 번의 액션(One Action)**으로 다음 전체 라이프사이클을 자동 수행합니다:

1. **정적 분석 (Analyzer)**: 소스코드와 설정을 스캔하여 런타임, 언어, 프레임워크, 포트를 판별하고 최적 배포 타깃(`local` vs `cloudrun`)을 결정하며 초기 Dockerfile을 자동 생성합니다.
2. **배포 (Deployer)**: 로컬 Docker 데몬 또는 Google Cloud Run에 블루그린 방식으로 컨테이너를 빌드·배포하고 실시간 런타임 헬스체크를 수행합니다.
3. **AI 자가치유 (Healer)**: 배포 또는 기동 실패 시 `stderr`와 컨테이너 런타임 로그를 포착하여 에러를 분류(규칙 기반 우선, LLM 폴백)하고, 소스코드/Dockerfile을 자동 수선하여 성공할 때까지 최대 3회 재배포 루프를 수행합니다.
4. **오케스트레이션 및 시각화 (Main & Dashboard)**: FastAPI 기반 작업 큐 및 SSE(Server-Sent Events) 스트림을 통해 실시간 단계, 로그, AI 수정 전/후 Diff, 최종 Live Public URL을 프론트엔드 대시보드에 표시합니다.

---

## 2. 시스템 실행 환경 요구사항 (Prerequisites)

CloudMorph 시스템을 로컬 및 클라우드 환경에서 정상 구동하기 위해 필요한 소프트웨어 요구사항입니다.

### 2.1 하드웨어 및 운영체제 (OS)
- **지원 OS**: macOS (Apple Silicon M1/M2/M3/M4 또는 Intel x86_64), Linux (Ubuntu 20.04 LTS 이상), Windows (WSL2 환경 권장)
- **최소 사양**: CPU 2 Cores 이상, RAM 8GB 이상, 디스크 여유 공간 10GB 이상 (Docker 컨테이너 이미지 캐시 고려)

### 2.2 필수 런타임 및 도구

| 도구명 | 요구 버전 | 용도 | 확인 명령어 |
|---|---|---|---|
| **Python** | `>= 3.10` (권장: `3.11` / `3.12`) | 백엔드 API, 분석기, 배포기, 자가치유 에이전트 런타임 | `python3 --version` |
| **uv** (권장) | `>= 0.4` | 초고속 가상환경 관리 및 의존성 동기화 (`uv sync`) | `uv --version` |
| **Node.js** | `>= 18.0.0` (LTS 권장) | 프론트엔드(React + Vite) 개발 및 빌드 환경 | `node -v` |
| **npm** | `>= 9.0.0` | 프론트엔드 패키지 매니저 | `npm -v` |
| **Docker** | `>= 24.0.0` (Docker Desktop / Engine) | 로컬 컨테이너 빌드, 실행, 격리 런타임 환경 (데몬 실행 필수) | `docker info` |
| **Google Cloud SDK (`gcloud`)** | 최신 버전 권장 | Google Cloud Run 배포, Artifact Registry 인증 및 배포 로그 수집 | `gcloud version` |
| **cloudflared CLI** (선택) | 최신 버전 | 로컬 배포 성공 시 외부 접속 가능한 Quick Tunnel 공개 URL 발급 | `cloudflared --version` |

### 2.3 외부 클라우드 및 API 계정 요구사항
- **LLM (셋 중 하나 이상)**:
  - Claude API (`ANTHROPIC_API_KEY`, 기본 `claude-haiku-5-5`) — analyzer는 현재 Claude 필수
  - OpenAI API (`OPENAI_API_KEY`, healer 기본 `gpt-4o`)
  - 로컬 서빙 vLLM / SGLang / Ollama / llama.cpp (`OPENAI_BASE_URL`, healer 전용, `HEALER_MODEL`에 모델명 지정)
  - Healer의 복합 에러 분석/수선 및 Analyzer의 AI 심층 검사관에서 사용.
- **Google Cloud Platform (GCP) 계정**:
  - GCP Project 생성 및 결제 계정(Billing) 연결 필수.
  - 필수 활성화 API:
    - Cloud Run Admin API (`run.googleapis.com`)
    - Artifact Registry API (`artifactregistry.googleapis.com`)
    - Cloud Build API (`cloudbuild.googleapis.com`)
  - Artifact Registry Docker 저장소 생성 (기본: `hackathon`, `asia-northeast3`).
  - 로컬 gcloud 인증: `gcloud auth login` 및 `gcloud auth configure-docker asia-northeast3-docker.pkg.dev`.

---

## 3. 환경 변수 요구사항 (.env Specification)

프로젝트 루트 디렉토리의 `.env` 파일에 정의되어야 하는 환경변수 명세입니다.  
(`cp .env.example .env` 명령으로 생성하며, 실제 비밀키가 포함된 `.env`는 `.gitignore`에 의해 절대 Git에 커밋되지 않습니다.)

| 환경변수명 | 필수 여부 | 기본값 | 사용 모듈 | 상세 설명 |
|---|:---:|---|---|---|
| `ANTHROPIC_API_KEY` | analyzer 필수 · healer 선택 | - | `healer`, `analyzer` | Anthropic(Claude) API 키. analyzer는 이 키가 없으면 분석하지 않습니다. |
| `OPENAI_API_KEY` | 선택 | - | `healer.py` | OpenAI API 키. 로컬 서버가 키를 요구하면 그 키를 넣습니다. |
| `OPENAI_BASE_URL` | 선택 | - | `healer.py` | 로컬/호환 서버 엔드포인트(vLLM, SGLang, Ollama, llama.cpp 등). 예: `http://localhost:8000/v1` |
| `LLM_PROVIDER` | 선택 | 자동 | `healer.py` | 제공자 고정: `anthropic`/`openai`/`local`/`vllm`/`sglang`/`ollama`/`llamacpp`. 비우면 Claude → OpenAI → 로컬 순서로 시도 |
| `HEALER_MODEL` | 선택 (로컬은 필수) | 제공자별 (`claude-haiku-5-5` / `gpt-4o`) | `healer.py` | healer 모델. 다른 제공자용 이름이면 무시하고 기본값 사용. 제공자별 지정: `HEALER_MODEL_ANTHROPIC` / `HEALER_MODEL_OPENAI` / `HEALER_MODEL_LOCAL` |
| `ANALYZER_MODEL` | 선택 | `claude-haiku-5-5` | `analyzer.py` | 소스코드 정적 분석 및 위험 탐지에 사용할 AI 검사관 모델. |
| `ANALYZER_MAX_LLM_CALLS` | 선택 | `6` | `analyzer.py` | 서버 전체에서 동시 실행 가능한 검사관 LLM 호출 수 상한(세마포어). |
| `ANALYZER_MAX_TURNS` | 선택 | `15` | `analyzer.py` | 검사관 에이전트의 저장소 파일 탐색 도구(tool calling) 최대 턴 수. |
| `GCP_PROJECT_ID` | **선택** (Cloud Run 배포 시 필수) | - | `deployer.py` | Google Cloud 프로젝트 식별자 (예: `bdai-n8n-2609161021`). |
| `GCP_REGION` | 선택 | `asia-northeast3` | `deployer.py` | Cloud Run 서비스 및 Artifact Registry 리전 (서울 리전). |
| `GCP_AR_REPO` | 선택 | `hackathon` | `deployer.py` | Artifact Registry의 Docker 저장소 이름. |
| `DEPLOYER_SERVICE_NAME` | 선택 | 배포 대상 디렉토리명 | `deployer.py` | Cloud Run 서비스명. 미지정 시 소스 디렉토리명을 기반으로 생성. |
| `DEPLOYER_TUNNEL` | 선택 | `1` | `deployer.py` | 로컬 Docker 배포 시 cloudflared quick tunnel 공개 URL 발급 여부 (`0`으로 비활성화). |
| `DEPLOYER_HEALTH_PATH` | 선택 | `/health` | `deployer.py` | 배포 완료 후 컨테이너 가동 여부를 점검할 HTTP 헬스체크 경로. |
| `DEPLOYER_PROBE_PATH` | 선택 | `None` (기본 TCP) | `deployer.py` | Cloud Run startup probe HTTP 경로. 설정 시 해당 경로로 HTTP 기동 검사 수행. |
| `DEPLOYER_VERIFY_TIMEOUT` | 선택 | `60` | `deployer.py` | 배포 후 서비스 엔드포인트 헬스체크 검증 대기 제한 시간(초). |

---

## 4. 소프트웨어 패키지 의존성 (Software Dependencies)

### 4.1 Backend (Python)
백엔드는 `backend/pyproject.toml` 및 `requirements.txt`에 명시되어 있으며, Python 3.10 이상을 지원합니다.

| 패키지 | 버전 명세 | 역할 및 용도 |
|---|---|---|
| `fastapi` | `>=0.115` | 비동기 웹 프레임워크, 오케스트레이션 REST 엔드포인트 제공 |
| `uvicorn[standard]` | `>=0.30` | 고성능 비동기 ASGI 웹 서버 |
| `pydantic` | `>=2.7` | 데이터 검증 및 모듈 간 데이터 계약(Schema) 직렬화 |
| `sse-starlette` | `>=2.1` | Server-Sent Events (SSE) 실시간 이벤트 스트리밍 지원 |
| `langgraph` | `>=0.2` | 순환 루프(StateGraph) 기반 에이전트 워크플로우 제어 (자연스러운 재배포 루프) |
| `anthropic` | `>=1.12.1` | Claude API 연동 (도구 호출, 구조화된 출력) |
| `python-dotenv` | `>=1.0` | `.env` 파일 로드 및 환경변수 주입 |
| `pytest` (dev) | `>=8` | 단위/통합 테스트 프레임워크 (fake deployer 기반 healer 루프 검증 등) |
| `ruff` (dev) | `>=0.6` | 고속 정적 린터 및 포매터 (PEP 8, 라인 길이 110 준수) |

### 4.2 Frontend (Node.js / React)
프론트엔드는 `frontend/package.json`에 정의되어 있습니다.

| 패키지 | 버전 명세 | 역할 및 용도 |
|---|---|---|
| `react` / `react-dom` | `^18.3.1` | UI 렌더링 라이브러리 |
| `vite` | `^5.4.2` | 고속 프론트엔드 개발 서버 및 번들러 |
| `lucide-react` | `^0.441.0` | 대시보드 아이콘 세트 (Cloud, Terminal, Wrench 등) |
| `tailwindcss` | `^3.4.1` | 유틸리티 기반 반응형 다크 테마 UI 스타일링 |

---

## 5. 기능적 요구사항 (Functional Requirements - FR)

### FR-1: 소스코드 정적 분석 (Analyzer - `app/analyzer.py`)
- **FR-1.1**: 입력받은 소스 디렉토리(`src_dir`) 내 파일 구조, 종속성 파일(`requirements.txt`, `package.json` 등), 소스 코드를 탐색한다.
- **FR-1.2**: 사용 언어, 프레임워크, 기동 명령(entrypoint), 내부 수신 포트(`port`, 기본 8080)를 자동 추출한다.
- **FR-1.3**: 프로젝트 성격에 따라 최적의 배포 타깃(`local` vs `cloudrun`)을 판정한다.
- **FR-1.4**: 소스 내 `Dockerfile`이 없을 경우, 판정된 런타임 환경에 맞춘 최적의 초기 `Dockerfile`을 자동 생성한다.
- **FR-1.5**: 분석 결과는 `AnalysisResult` 스키마 형태로 반환한다.

### FR-2: 멀티 타깃 컨테이너 배포 (Deployer - `app/deployer.py`)
- **FR-2.1**: **로컬 배포 (`local`)**:
  - Docker 데몬을 통해 소스 디렉토리와 생성된 Dockerfile 기반 이미지를 빌드한다.
  - 기존 실행 중인 동일 컨테이너를 정리하고 신규 컨테이너를 구동한다.
  - `cloudflared`를 통해 임시 Live Public URL을 발급하고 헬스체크를 수행한다.
- **FR-2.2**: **클라우드 배포 (`cloudrun`)**:
  - Cloud Run 아키텍처(`linux/amd64`)에 맞춰 교차 빌드 후 Artifact Registry에 푸시한다.
  - 블루그린 검증 전략(`--no-traffic --tag candidate`)으로 배포 후 태그 URL로 기동 검사를 완료한 뒤 100% 트래픽(`--to-latest`)으로 안전 승격한다.
- **FR-2.3**: **오류 로그 보존 계약**:
  - 배포 또는 기동 실패 시 예외를 발생(raise)시키지 않고 `DeployResult(success=False, ...)`를 반환한다.
  - 단순 CLI 에러 외에 **컨테이너 런타임 로그(`docker logs` 또는 Cloud Run revision logs)**를 반드시 `stderr`에 포함하여 자가치유 모듈에 전달한다.

### FR-3: AI 자가치유 에이전트 (Healer - `app/healer.py`)
- **FR-3.1**: 실패한 배포 결과의 `stderr`를 정규표현식 및 패턴 매칭으로 분석하여 5가지 카테고리로 1차 분류한다:
  - `port_binding` (포트 바인딩 불일치, 0.0.0.0 미수신)
  - `missing_dependency` (패키지 누락, ModuleNotFoundError 등)
  - `bad_entrypoint` (잘못된 실행 명령어, 파일명 불일치)
  - `build_failure` (빌드 타임 문법 오류/패키지 설치 불가)
  - `unknown` (기타 미분류 오류)
- **FR-3.2**: **규칙 우선(Deterministic Rules First)** 패치를 적용하여 데모 시연의 결정성과 속도를 보장하고, 미분류/복합 에러의 경우 Claude LLM 폴백 패치를 실행한다.
- **FR-3.3**: 수정 전/후 Dockerfile의 `unified diff`를 생성하고 시도별 이력(`PatchRecord`)을 기록한다.
- **FR-3.4**: 최대 재시도 횟수(`MAX_RETRIES = 3`) 내에서 수정된 Dockerfile로 재배포를 순환 실행한다.
- **FR-3.5**: 최종 결과는 `HealReport` 형태로 반환한다.

### FR-4: 중앙 파이프라인 오케스트레이션 (Main - `app/main.py`)
- **FR-4.1**: `POST /deploy` 엔드포인트를 통해 비동기 파이프라인 작업을 등록하고 `deployment_id`를 즉시 반환한다.
- **FR-4.2**: `GET /deploy/{deployment_id}/events` 엔드포인트를 통해 파이프라인 전 과정의 이벤트를 SSE로 실시간 스트리밍한다:
  - `stage`: 현재 진행 단계 (`analyze`, `deploy`, `heal`, `redeploy`)
  - `log`: 실시간 빌드/배포 로그
  - `heal_diff`: AI가 수정한 전/후 Diff 및 원인 분석
  - `done`: 배포 성공 및 최종 접속 URL
  - `error`: 자가치유 한도 초과 또는 치명적 에러

### FR-5: 웹 프론트엔드 대시보드 (Frontend - `frontend/`)
- **FR-5.1**: 배포할 로컬 프로젝트 경로 입력 및 타깃 환경 선택(`auto`, `cloud`, `local`) UI 제공.
- **FR-5.2**: 4단계 시각적 상태 카드 (1. 코드 분석 -> 2. 1차 배포 -> 3. AI 자가치유 -> 4. 최종 완료) 표시.
- **FR-5.3**: 터미널 스타일의 실시간 로그 뷰어 제공.
- **FR-5.4**: AI 자가치유 발생 시 수정 전/후 코드 Diff 카드 실시간 렌더링.
- **FR-5.5**: 배포 완료 시 외부 접속 가능한 라이브 URL 링크 제공.

---

## 6. 비기능적 요구사항 (Non-Functional Requirements - NFR)

1. **결함 허용 및 무중단 (Fault Tolerance)**:
   - 한 단계의 빌드 오류나 런타임 크래시가 전체 서버 프로세스 중단으로 이어지지 않아야 합니다.
   - `deployer.py`는 실패 시 예외를 던지지 않고 진단 정보를 담은 `DeployResult`를 반환해야 합니다.
2. **비용 및 자원 보호 (Resource & Cost Caps)**:
   - LLM 무한 루프 방지를 위해 자가치유 재시도는 최대 3회(`MAX_RETRIES = 3`)로 엄격히 제한됩니다.
   - 불필요한 LLM 비용을 아끼기 위해 규칙 기반 패치를 우선 적용합니다.
   - 동시 AI 분석 요청 시 세마포어(`ANALYZER_MAX_LLM_CALLS = 6`)로 동시성을 제어합니다.
3. **보안 및 시크릿 관리 (Security)**:
   - Anthropic API 키, GCP 서비스 계정 키 등 민감 정보는 형상 관리(Git)에 절대 노출되지 않아야 합니다.
   - `.gitignore`를 통해 `.env`, `*.key`, `*.pem` 파일의 커밋을 원천 차단합니다.
4. **모듈 간 디커플링 (Decoupling)**:
   - 각 모듈은 `backend/app/schemas.py`의 Pydantic 모델만을 통해 소통하며 상호 직접 임포트를 최소화합니다.
   - 단위 테스트 시 `fake deployer` 또는 목(Mock) 객체를 주입하여 외부 인프라(Docker, Cloud Run) 의존 없이도 독립 검증이 가능해야 합니다.

---

## 7. 설치 및 빠른 시작 가이드 (Quick Start)

### 7.1 백엔드 설치 및 실행

```bash
# 1. 저장소 루트 이동
cd one-action-cloudmorph

# 2. 환경변수 파일 준비
cp .env.example .env
# .env 파일을 열어 ANTHROPIC_API_KEY(analyzer 필수)와, 필요 시 OPENAI_API_KEY / OPENAI_BASE_URL(healer), GCP_PROJECT_ID 설정

# 3. 백엔드 디렉토리 이동 및 의존성 설치
cd backend

# 방법 A: uv 사용 (권장)
uv sync
uv run pytest -q                             # 테스트 실행
uv run uvicorn app.main:app --reload --port 8000

# 방법 B: 표준 pip 사용
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload --port 8000
```

### 7.2 프론트엔드 설치 및 실행

```bash
cd frontend
npm install
npm run dev
# 기본적으로 http://localhost:5173 에서 대시보드가 구동됩니다.
```

---

## 8. 관련 문서 링크

- 모듈 간 인터페이스 계약: [`docs/interfaces.md`](interfaces.md)
- 수동 배포 및 실측 기록: [`docs/deployer-manual-run-notes.md`](deployer-manual-run-notes.md)
- 공용 데이터 스키마: [`backend/app/schemas.py`](../backend/app/schemas.py)
