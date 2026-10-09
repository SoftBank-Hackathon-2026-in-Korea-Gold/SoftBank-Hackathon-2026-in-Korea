"""배포 명세: 백엔드가 앱을 클라우드에 띄우는 데 필요한 정보.

코드를 바꾸는 일은 analyzer가, 코드를 감싸는 일(Dockerfile, zip, Procfile, systemd)은 백엔드가 한다.
그래서 명세에는 Dockerfile 대신 어느 플랫폼에도 묶이지 않는 실행 레시피(설치·빌드·시작 명령)를 담는다.
유형(1~5)을 실제 클라우드 서비스로 고르는 것도 백엔드 몫이다.

result['ready']는 "코드가 이 유형에 맞는가", 명세의 ready는 "백엔드가 지금 배포할 수 있는가"다.
레시피를 못 찾으면(시작 명령, 포트 등) 코드는 맞아도 명세는 ready가 아니다.

환경변수는 안내(무엇인지, 어디서 받는지, 무작위로 만들어도 되는지)만 한다. 값은 화면에서 받아 백엔드로 바로 보낸다.
"""

import json
import re
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import offers as offer_table
from .rules import TYPE_INFO, TYPES
from .signals import (
    HEALTH_ROUTE,
    READS_PORT_ENV,
    _dep_hits,
    _node_deps,
    _source_files,
    config_files,
    has_dep,
    read_text,
)


class App(BaseModel):
    language: str | None = None
    framework: str | None = None
    version: str | None = Field(default=None, description="런타임 버전 (매니페스트에 적힌 최소 버전)")
    install: str | None = Field(default=None, description="의존성 설치 명령")
    build: str | None = Field(default=None, description="빌드 명령")
    start: str | None = Field(default=None, description="서버 시작 명령. $PORT는 port_env 환경변수 값이다")
    handler: str | None = Field(default=None, description="FaaS 핸들러 (유형 1)")
    static_output: str | None = Field(default=None, description="올릴 정적 파일 폴더 (유형 5)")
    system_tools: list[str] = Field(
        default_factory=list, description="OS에 설치해야 하는 도구. 배포판별 패키지 이름은 백엔드가 고른다"
    )
    port_env: str | None = Field(default=None, description="포트를 받는 환경변수")
    port: int | None = Field(
        default=None, description="port_env가 있으면 그 기본값, 없으면 앱이 듣는 고정 포트"
    )
    health: str | None = Field(default=None, description="헬스체크 GET 경로. 없으면 포트가 열렸는지만 본다")
    existing_dockerfile: str | None = Field(default=None, description="저장소에 원래 있던 Dockerfile")


class Resource(BaseModel):
    kind: Literal["postgres", "mysql", "redis", "s3"]
    env: dict[str, str] = Field(
        description="역할 → 환경변수 이름. 역할은 url, host, port, user, password, database, "
        "bucket, endpoint, region, access_key, secret_key"
    )
    scheme: str | None = Field(
        default=None, description="url 값의 형식. jdbc:·r2dbc:로 시작하면 계정은 user·password로 따로 넣는다"
    )
    evidence: str


class EnvVar(BaseModel):
    name: str
    kind: Literal["secret", "config"]
    required: bool = Field(
        description="배포할 때 값을 넣어야 한다. 비밀값은 코드에 기본값이 있어도 개발용이라 넣어야 한다"
    )
    default: str | None = Field(
        default=None, description="코드에 있는 기본값 (설정값만. 비밀값의 기본값은 내보내지 않는다)"
    )
    example: str | None = Field(default=None, description="넣을 만한 값의 예")
    description: str | None = Field(default=None, description="무엇인지")
    how: str | None = Field(default=None, description="값을 어떻게 정하거나 어디서 받는지")
    generate: bool | None = Field(
        default=None, description="무작위 값을 만들어 넣어도 된다 (generate_secret()). None은 모름"
    )
    evidence: str


class Job(BaseModel):
    path: str
    cron: str
    auth_env: str = Field(
        default="CRON_TOKEN", description="Authorization: Bearer <이 환경변수 값>으로 호출한다"
    )


class Constraints(BaseModel):
    single_instance: bool = Field(
        description="여러 대로 늘리면 안 된다 (메모리 상태, 로컬 파일, 프로세스 안 주기 작업)"
    )
    persistent_paths: list[str] = Field(description="재시작해도 남아야 하는 파일·폴더 (작업 폴더 기준)")
    long_connections: bool = Field(description="WebSocket처럼 오래 붙어 있는 연결이 있다")
    gpu: bool
    os: Literal["linux", "windows"]


class DeploySpec(BaseModel):
    version: Literal[1] = 1
    ready: bool
    blockers: list[str] = Field(description="지금 배포하면 안 되는 이유. 비어 있어야 ready")
    path: str
    target: int = Field(description="배포 유형 1~5. 실제 클라우드 서비스는 백엔드가 고른다")
    target_name: str
    services: dict[str, str] = Field(description="이 유형의 클라우드별 대표 서비스 (화면 표시용 예시)")
    app: App
    resources: list[Resource]
    env: list[EnvVar] = Field(description="사용자가 값을 정할 환경변수. 자원 접속 정보와 PORT는 뺐다")
    schedules: list[Job]
    constraints: Constraints
    steps: list[str] = Field(description="사람이 읽는 배포 절차")


# 자원 의존성. _dep_hits 표 모양에 맞춘다: (자원, 값(안 씀), [의존성 이름])
RESOURCE_DEPS = {
    "node": [
        ("postgres", "", ["pg", "postgres", "pg-promise"]),
        ("mysql", "", ["mysql", "mysql2"]),
        ("redis", "", ["redis", "ioredis"]),
        ("s3", "", ["@aws-sdk/client-s3"]),
    ],
    "python": [
        ("postgres", "", ["psycopg", "psycopg2", "psycopg2-binary", "asyncpg"]),
        ("mysql", "", ["pymysql", "mysqlclient", "mysql-connector-python", "aiomysql"]),
        ("redis", "", ["redis"]),
        ("s3", "", ["boto3"]),
    ],
    "go": [
        ("postgres", "", ["github.com/lib/pq", "github.com/jackc/pgx/v5"]),
        ("mysql", "", ["github.com/go-sql-driver/mysql"]),
        ("redis", "", ["github.com/redis/go-redis/v9"]),
        ("s3", "", ["github.com/aws/aws-sdk-go-v2/service/s3"]),
    ],
    "java": [
        ("postgres", "", ["postgresql"]),
        ("mysql", "", ["mysql-connector-j", "mysql-connector-java"]),
        ("redis", "", ["spring-boot-starter-data-redis", "jedis"]),
        ("s3", "", ["s3"]),
    ],
}
# SQLAlchemy URL에 붙는 드라이버 이름
SQLALCHEMY_DRIVERS = {"postgres": ("psycopg", "asyncpg"), "mysql": ("pymysql", "aiomysql")}

# (환경변수를 읽는 코드, 바로 뒤에 기본값이 오는 모양). 기본값이 문자열·숫자면 그 값을 잡는다
NODE_DEFAULT = r'\s*(?:\|\||\?\?)\s*(?:[\'"`]([^\'"`]*)[\'"`]|(\d+))?'
PY_DEFAULT = r'\s*,\s*(?:[\'"]([^\'"]*)[\'"]|(\d+))?'
CODE_ENV = [
    (re.compile(rx), dflt and re.compile(dflt))
    for rx, dflt in [
        (r"process\.env\.([A-Z_][A-Z0-9_]*)", NODE_DEFAULT),
        (r'process\.env\[[\'"]([A-Z_][A-Z0-9_]*)[\'"]\]', NODE_DEFAULT),
        (r'os\.(?:environ\.get|getenv)\(\s*[\'"]([A-Z_][A-Z0-9_]*)[\'"]', PY_DEFAULT),
        (r'os\.environ\[[\'"]([A-Z_][A-Z0-9_]*)[\'"]\]', None),
        (r'os\.(?:Getenv|LookupEnv)\(\s*"([A-Z_][A-Z0-9_]*)"', None),
        (r'System\.getenv\(\s*"([A-Z_][A-Z0-9_]*)"', None),
    ]
]
SPRING_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::([^}]*))?\}")  # ${NAME} 또는 ${NAME:기본값}
SECRET = re.compile(r"(?:^|_)(?:SECRET|PASSWORD|PASSWD|TOKEN|KEY|CREDENTIALS?)(?:_|$)")
POINTER = re.compile(
    r"_(?:PATH|FILE|DIR|URL)$"
)  # JWT_PRIVATE_KEY_PATH는 비밀값이 아니라 비밀값 파일의 위치다

# 이 이름으로 시작하는 환경변수는 그 외부 서비스에서 받은 값이다
SERVICES = {
    "OPENAI": "OpenAI",
    "ANTHROPIC": "Anthropic",
    "GEMINI": "Google Gemini",
    "GOOGLE": "Google",
    "STRIPE": "Stripe",
    "GITHUB": "GitHub",
    "SLACK": "Slack",
    "DISCORD": "Discord",
    "SENDGRID": "SendGrid",
    "MAILGUN": "Mailgun",
    "TWILIO": "Twilio",
    "SENTRY": "Sentry",
    "FIREBASE": "Firebase",
    "SUPABASE": "Supabase",
    "NOTION": "Notion",
    "CLOUDINARY": "Cloudinary",
    "KAKAO": "카카오",
    "NAVER": "네이버",
    "TOSS": "토스페이먼츠",
}
# (이름, 설명, 정하는 법, 무작위로 만들어도 되나, 예시). 위에서부터 처음 맞는 것
GUIDES = [
    (re.compile(rx), *rest)
    for rx, *rest in [
        (
            r"AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)",
            "AWS 접근 키",
            "클라우드에서는 IAM 역할을 붙이고 비워 두는 편이 안전합니다",
            False,
            None,
        ),
        (
            r"(?:\w+_)?CLIENT_(?:ID|SECRET)",
            "OAuth 클라이언트 정보",
            "로그인 제공자(Google, GitHub 등) 개발자 콘솔에서 발급받습니다",
            False,
            None,
        ),
        (
            r"(?:\w+_)?API_KEY",
            "API 키",
            "외부 서비스 키면 그 서비스에서 발급받고, 이 앱이 나눠 주는 키면 무작위 값이면 됩니다",
            None,
            None,
        ),
        (
            r"(?:\w+_)?(?:SECRET_KEY|SECRET|APP_KEY)",
            "세션·토큰 서명용 비밀값",
            "무작위 값이면 됩니다. 바꾸면 기존 로그인이 풀립니다",
            True,
            None,
        ),
        (
            r"(?:\w+_)?TOKEN",
            "이 앱이 요청을 확인할 때 쓰는 토큰",
            "무작위 값을 만들고, 이 앱을 부르는 쪽에도 같은 값을 알려 줍니다",
            True,
            None,
        ),
        (
            r"(?:\w+_)?(?:PASSWORD|PASSWD)",
            "비밀번호",
            "쓰는 곳(DB, 외부 서비스)에 맞는 값을 넣습니다",
            None,
            None,
        ),
        (r"NODE_ENV", "실행 모드", None, None, "production"),
        (r"(?:APP_|FLASK_|DJANGO_)?ENV(?:IRONMENT)?", "실행 환경 이름", None, None, "production"),
        (r"LOG_LEVEL", "로그 수준", None, None, "info"),
        (r"DEBUG", "디버그 모드", "운영에서는 꺼야 합니다", None, "false"),
        (r"TZ", "시간대", None, None, "Asia/Seoul"),
        (
            r"\w+_(?:URL|URI|HOST|ENDPOINT)",
            "다른 서비스의 주소",
            "연결할 서비스의 주소를 넣습니다",
            None,
            None,
        ),
    ]
]
RESOURCE_NAMES = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "redis": "Redis",
    "s3": "오브젝트 스토리지 버킷",
}

PORT_DEFAULT = re.compile(r'PORT\b[\'"]?\]?\s*(?:\|\||\?\?|,)\s*[\'"]?(\d{2,5})')
PORT_FIXED = re.compile(r'\.listen\(\s*(\d{2,5})|\bport\s*=\s*(\d{2,5})|[\'"]:(\d{2,5})[\'"]')
PERSIST = re.compile(
    r'sqlite:/{2,3}([^\'"\s]+)|[\'"]([\w./-]+\.(?:db|sqlite3?))[\'"]'
    r'|\bdest\s*:\s*[\'"]([^\'"]+)[\'"]|UPLOAD\w*\s*=\s*[\'"]([^\'"]+)[\'"]'
)
# system_packages 근거에 이 이름이 있으면 → 필요한 도구
TOOLS = {
    "puppeteer": "chromium",
    "playwright": "chromium",
    "selenium": "chromium",
    "grover": "chromium",
    "chromium": "chromium",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffmpeg",
    "canvas": "cairo",
    "tesseract": "tesseract",
    "opencv-python": "libgl",
    "pdf2image": "poppler",
    "pdftoppm": "poppler",
    "weasyprint": "pango",
    "psycopg2": "libpq",
    "magick": "imagemagick",
    "convert": "imagemagick",
    "libreoffice": "libreoffice",
    "soffice": "libreoffice",
    "wkhtmltopdf": "wkhtmltopdf",
}

PY_APP = re.compile(r"^(\w+)\s*=\s*(FastAPI|Starlette|Flask)\(", re.MULTILINE)
PY_FACTORY = re.compile(
    r"^(\w+)\s*=\s*\w*(?:create|make|build|get)_app\(", re.MULTILINE
)  # app = create_app()
PY_CTOR = re.compile(r"\b(FastAPI|Starlette|Flask)\(")
PY_MAIN = re.compile(r'^if __name__ == [\'"]__main__[\'"]', re.MULTILINE)

# 접속 정보를 URL 하나가 아니라 나눠 받는 경우 (DB_HOST, DB_USER …): 역할 → 이름 뒷부분
DB_PREFIX = r"(?:DB|DATABASE|POSTGRES(?:QL)?|PG|MYSQL)_?"
DB_PARTS = {
    "host": "HOST",
    "port": "PORT",
    "user": "USER(?:NAME)?",
    "password": "PASS(?:WORD)?",
    "database": "(?:NAME|DB|DATABASE)",
}
REDIS_PARTS = {"host": "HOST", "port": "PORT", "password": "PASS(?:WORD)?", "database": "DB"}


def _first(pattern: str, text: str, flags=0) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else None


def _read(p: Path) -> str:
    return read_text(p) if p.is_file() else ""


def _spring(root: Path) -> bool:
    return "org.springframework.boot" in _read(root / "pom.xml") + _read(root / "build.gradle") + _read(
        root / "build.gradle.kts"
    )


# ---------- 언어별 실행 레시피 ----------


def _node(root: Path, signals: dict, sources: list) -> App:
    pkg = json.loads(_read(root / "package.json") or "{}")
    scripts = pkg.get("scripts", {})
    runner = (
        "pnpm" if (root / "pnpm-lock.yaml").is_file() else "yarn" if (root / "yarn.lock").is_file() else "npm"
    )
    if runner == "npm":
        install = "npm ci" if (root / "package-lock.json").is_file() else "npm install"
    else:
        install = f"{runner} install --frozen-lockfile"
    app = App(
        language="node",
        version=_first(r"(\d+)", pkg.get("engines", {}).get("node", "") or _read(root / ".nvmrc")),
        install=install,
        build=f"{runner} run build" if "build" in scripts else None,
        handler="handler.handler" if (root / "handler.js").is_file() else None,
    )
    if signals["is_spa"]["value"] == "yes":
        app.static_output = "build" if "react-scripts" in _node_deps(root) else "dist"
    elif "start" in scripts:
        app.start = f"{runner} start"
    else:
        main = next(
            (f for f in (pkg.get("main"), "server.js", "index.js", "app.js") if f and (root / f).is_file()),
            None,
        )
        app.start = main and f"node {main}"
    return app


def _python(root: Path, signals: dict, sources: list) -> App:
    pyproject = _read(root / "pyproject.toml")
    if (root / "uv.lock").is_file():
        install = "uv sync --frozen"
    elif (root / "requirements.txt").is_file():
        install = "pip install -r requirements.txt"
    elif (root / "Pipfile").is_file():
        install = "pipenv install --deploy --system"
    else:
        install = "pip install ." if pyproject else None
    app = App(
        language="python",
        install=install,
        version=_first(r"(3\.\d+)", _read(root / ".python-version"))
        or _first(r'requires-python\s*=\s*"[^"\d]*(3\.\d+)', pyproject),
        handler="lambda_handler.handler" if (root / "lambda_handler.py").is_file() else None,
    )

    def has(name):
        return has_dep(root, "python", name)

    # 얕은 경로의 파일부터 본다 (app.py가 tests/app.py보다 먼저)
    files = sorted(
        (
            (p.relative_to(root).as_posix(), "\n".join(lines))
            for p, lang, lines in sources
            if lang == "python"
        ),
        key=lambda f: (f[0].count("/"), f[0]),
    )
    for rel, text in files:
        if m := PY_APP.search(text):
            var, kind = m.groups()
        elif (f := PY_FACTORY.search(text)) and (c := PY_CTOR.search(text)):
            var, kind = f.group(1), c.group(1)
        else:
            continue
        obj = f"{rel[:-3].replace('/', '.')}:{var}"
        if kind == "Flask":
            app.start = (
                f"gunicorn -b 0.0.0.0:$PORT {obj}"
                if has("gunicorn")
                else f"flask --app {obj} run --host 0.0.0.0 --port $PORT"
            )
        elif has("uvicorn") or has("fastapi[standard]"):
            app.start = f"uvicorn {obj} --host 0.0.0.0 --port $PORT"
        break
    if not app.start and (root / "manage.py").is_file():
        wsgi = next(root.glob("*/wsgi.py"), None)
        app.start = (
            f"gunicorn -b 0.0.0.0:$PORT {wsgi.parent.name}.wsgi"
            if wsgi and has("gunicorn")
            else "python manage.py runserver 0.0.0.0:$PORT"
        )
    if app.start:
        app.port_env = "PORT"
    else:  # 프레임워크를 못 찾으면 직접 실행하는 파일 (포트는 코드에서 찾는다)
        main = next((rel for rel, text in files if PY_MAIN.search(text)), None)
        app.start = main and f"python {main}"
    return app


def _go(root: Path, signals: dict, sources: list) -> App:
    go = [(p, lines) for p, lang, lines in sources if lang == "go"]
    mains = [p for p, lines in go if any(line.startswith("package main") for line in lines)]
    pkg = (
        "."
        if any(p.parent == root for p in mains)
        else "./" + mains[0].parent.relative_to(root).as_posix()
        if mains
        else None
    )
    app = App(
        language="go",
        version=_first(r"^go (\d+\.\d+)", _read(root / "go.mod"), re.MULTILINE),
        install="go mod download",
        build=pkg and f"go build -o app {pkg}",
        start=pkg and "./app",
    )
    # gin의 Run()은 인자가 없으면 PORT 환경변수를 읽고, 없으면 8080을 쓴다
    if has_dep(root, "go", "github.com/gin-gonic/gin") and any(
        ".Run()" in line for _, lines in go for line in lines
    ):
        app.port_env, app.port = "PORT", 8080
    return app


def _maven_jar(pom: Path) -> str:
    project = ET.parse(pom).getroot()
    ns = project.tag[: project.tag.find("}") + 1]  # '{http://maven.apache.org/POM/4.0.0}' 또는 ''

    def get(*path):
        return (project.findtext("/".join(ns + p for p in path)) or "").strip()

    final = get("build", "finalName")
    if final and "$" not in final:
        return f"target/{final}.jar"
    return f"target/{get('artifactId')}-{get('version') or get('parent', 'version')}.jar"


def _java(root: Path, signals: dict, sources: list) -> App:
    spring = _spring(root)
    pom = root / "pom.xml"
    if pom.is_file():
        text = read_text(pom)
        tool = "./mvnw" if (root / "mvnw").is_file() else "mvn"
        version = _first(
            r"<(?:java\.version|maven\.compiler\.release|maven\.compiler\.source|release)>(\d+)", text
        )
        build, jar = f"{tool} -q -DskipTests package", _maven_jar(pom)
    else:
        text = _read(root / "build.gradle") or _read(root / "build.gradle.kts")
        tool = "./gradlew" if (root / "gradlew").is_file() else "gradle"
        version = _first(r"JavaLanguageVersion\.of\((\d+)\)", text) or _first(
            r"sourceCompatibility\s*=\s*\D*(\d+)", text
        )
        name = (
            _first(
                r'rootProject\.name\s*=\s*[\'"]([^\'"]+)',
                _read(root / "settings.gradle") + _read(root / "settings.gradle.kts"),
            )
            or root.name
        )
        ver = _first(r'^version\s*=\s*[\'"]([^\'"]+)', text, re.MULTILINE)
        build = f"{tool} bootJar" if spring else f"{tool} build -x test"
        jar = f"build/libs/{name}-{ver}.jar" if ver else f"build/libs/{name}.jar"
    # Spring Boot는 server.port 시스템 속성이 설정 파일보다 우선한다
    app = App(
        language="java",
        version=version,
        build=build,
        start=f"java -Dserver.port=$PORT -jar {jar}" if spring else f"java -jar {jar}",
    )
    if spring:
        app.port_env = "PORT"
    return app


RECIPES = {"node": _node, "python": _python, "go": _go, "java": _java}


def _app(root: Path, langs: list, signals: dict, sources: list) -> App:
    if not langs:  # 매니페스트 없는 정적 사이트
        return App(static_output="." if signals["is_spa"]["value"] == "yes" else None)
    apps = [RECIPES[lang](root, signals, sources) for lang in langs if lang in RECIPES]
    return next((a for a in apps if a.start or a.static_output), apps[0] if apps else App(language=langs[0]))


# ---------- 환경변수·자원 ----------


def _env_reads(root: Path, sources: list) -> dict:
    """{이름: {'evidence': 처음 나온 곳, 'default': 기본값이 한 번이라도 있었나, 'value': 기본값, 'usage': 읽는 줄 (최대 3개),
    'in_code': 코드·설정 파일에서 읽나 (.env.example에만 있으면 False)}}"""
    found = {}

    def add(name, path, lineno, line, default, value=None, in_code=True):
        e = found.setdefault(
            name,
            {
                "evidence": f"{path.relative_to(root)}:{lineno}",
                "default": False,
                "value": None,
                "usage": [],
                "in_code": False,
            },
        )
        e["default"] |= default and value != ""  # os.getenv("X", "")의 빈 문자열은 기본값이 아니다
        e["in_code"] |= in_code
        if e["value"] is None and value != "":
            e["value"] = value
        if len(e["usage"]) < 3:
            e["usage"].append(line.strip()[:200])

    for path, _, lines in sources:
        for i, line in enumerate(lines, 1):
            for rx, dflt in CODE_ENV:
                for m in rx.finditer(line):
                    d = dflt and dflt.match(line, m.end())
                    if (
                        d and line[m.start() - 1 : m.start()] == "!"
                    ):  # !process.env.X || ...는 기본값이 아니라 "없으면" 조건이다
                        d = None
                    add(
                        m.group(1),
                        path,
                        i,
                        line,
                        bool(d),
                        d and next((g for g in d.groups() if g is not None), None),
                    )
    for path in config_files(root):
        for i, line in enumerate(read_text(path).splitlines(), 1):
            for m in SPRING_ENV.finditer(line):
                add(m.group(1), path, i, line, m.group(2) is not None, m.group(2))
    example = root / ".env.example"
    for i, line in enumerate(_read(example).splitlines(), 1):
        if m := re.match(r"([A-Z_][A-Z0-9_]*)=", line):
            add(m.group(1), example, i, line, False, in_code=False)
    return found


def _guide(name: str, kind: str) -> tuple:
    """(설명, 정하는 법, 무작위로 만들어도 되나, 예시). 규칙으로 모르면 전부 None"""
    service = next((v for k, v in SERVICES.items() if name.startswith(k + "_")), None)
    if service and kind == "secret":
        return f"{service}에서 받은 값", f"{service} 콘솔에서 발급받아 넣습니다", False, None
    if service:  # GEMINI_MODEL 같은 설정값
        return f"{service} 설정값", None, None, None
    return next((tuple(g[1:]) for g in GUIDES if g[0].fullmatch(name)), (None, None, None, None))


def _env_vars(envs: dict, used: set) -> list[EnvVar]:
    """LLM이 없을 때: 패턴으로 찾은 환경변수와 이름 규칙으로 쓴 안내"""
    out = []
    for name, e in envs.items():
        if name in used:
            continue
        kind = "secret" if SECRET.search(name) and not POINTER.search(name) else "config"
        description, how, generate, example = _guide(name, kind)
        if not e["in_code"]:  # 코드가 다른 이름을 읽는 경우가 많다 (.env.example이 낡음)
            how = "코드에서 읽는 곳을 찾지 못했습니다 (.env.example에만 있음). 코드가 쓰는 이름과 맞는지 확인하세요"
        out.append(
            EnvVar(
                name=name,
                kind=kind,
                required=e["in_code"] and (kind == "secret" or not e["default"]),
                default=e["value"] if kind == "config" else None,
                example=example,
                description=description,
                how=how,
                generate=generate if kind == "secret" else None,
                evidence=e["evidence"],
            )
        )
    return out


def _resources(root: Path, langs: list, names: list) -> list[Resource]:
    def pick(*patterns):
        return next((n for p in patterns for n in names if re.fullmatch(p, n)), None)

    def parts(prefix, roles):  # URL이 없을 때: 호스트를 찾아야 나눠 받는 방식으로 본다
        found = {role: pick(prefix + rx) for role, rx in roles.items()}
        return {k: v for k, v in found.items() if v} if found["host"] else {}

    out = []
    for lang in langs:
        for kind, _, hit in _dep_hits(root, lang, RESOURCE_DEPS):
            if any(r.kind == kind for r in out):
                continue
            spring = lang == "java" and _spring(root)
            r2dbc = spring and has_dep(
                root, "java", "spring-boot-starter-data-r2dbc"
            )  # 리액티브 DB 접속 (WebFlux)
            if kind in ("postgres", "mysql"):
                url = pick("DATABASE_URL", DB_PREFIX + "(?:URL|URI|DSN)")
                env = {"url": url} if url or spring else parts(DB_PREFIX, DB_PARTS)
                if spring:  # 환경변수가 설정 파일 값을 덮어쓴다 (relaxed binding)
                    prop = "SPRING_R2DBC" if r2dbc else "SPRING_DATASOURCE"
                    owner = r"\w*(?:DB|DATABASE|DATASOURCE|R2DBC|POSTGRES|MYSQL)\w*_"
                    env = {
                        "url": url or f"{prop}_URL",
                        "user": pick(owner + "USER(?:NAME)?") or f"{prop}_USERNAME",
                        "password": pick(owner + "(?:PASSWORD|PASSWD|PW)") or f"{prop}_PASSWORD",
                    }
                base = "postgresql" if kind == "postgres" else "mysql"
                if lang == "java":
                    scheme = f"r2dbc:{base}" if r2dbc else f"jdbc:{base}"
                elif lang == "python" and has_dep(root, "python", "sqlalchemy"):
                    driver = next((d for d in SQLALCHEMY_DRIVERS[kind] if has_dep(root, "python", d)), None)
                    scheme = f"{base}+{driver}" if driver else base
                else:
                    scheme = base
                if "url" not in env:  # 나눠 받으면 URL 형식이 없다
                    scheme = None
            elif kind == "redis":
                url = pick(r"\w*REDIS\w*_(?:URL|URI)") or ("SPRING_DATA_REDIS_URL" if spring else None)
                env = {"url": url} if url else parts("REDIS_", REDIS_PARTS)
                scheme = "redis" if url else None
            else:  # S3 호환 저장소 (AWS S3, MinIO 등)
                owner = r"\w*(?:S3|MINIO|STORAGE|AWS)\w*_"
                env = {
                    "bucket": pick(r"\w*BUCKET\w*"),
                    "endpoint": pick(owner + r"ENDPOINT\w*", r"AWS_ENDPOINT_URL\w*"),
                    "region": pick(owner + "REGION"),
                    "access_key": pick(owner + "ACCESS_KEY(?:_ID)?"),
                    "secret_key": pick(owner + "SECRET(?:_ACCESS)?_KEY"),
                }
                scheme = None
            out.append(
                Resource(
                    kind=kind,
                    env={k: v for k, v in env.items() if v},
                    scheme=scheme,
                    evidence=hit["evidence"],
                )
            )
    return out


def _port(rx, lines) -> int | None:
    for line in lines:
        if m := rx.search(line):
            return int(next(g for g in m.groups() if g))
    return None


# ---------- 명세 ----------


def _steps(t: int, app: App, resources: list, env: list, jobs: list, constraints: Constraints) -> list[str]:
    steps = []
    for r in resources:
        driver = (r.scheme or "").split(":")[0]
        form = (
            f" ({driver.upper()} 형식, 계정은 따로)"
            if driver in ("jdbc", "r2dbc")
            else f" ({r.scheme}://…)"
            if r.scheme
            else ""
        )
        steps.append(f"{RESOURCE_NAMES[r.kind]} 만들기 → {', '.join(r.env.values())}에 연결{form}")
    if need := [e for e in env if e.required]:
        steps.append(
            "환경변수 입력: " + ", ".join(e.name + (" (자동 생성 가능)" if e.generate else "") for e in need)
        )
    if app.system_tools:
        steps.append(f"OS 도구 설치: {', '.join(app.system_tools)}")
    runtime = " ".join(filter(None, [app.language, app.version]))
    if app.install:
        steps.append(f"의존성 설치: {app.install}")
    if app.build:
        steps.append(f"빌드: {app.build}" + (f" ({runtime})" if runtime else ""))
    if t == 5:
        steps.append(f"{app.static_output} 폴더를 정적 호스팅에 올리기")
    elif t == 1 and app.handler:
        steps.append(f"함수 핸들러로 등록: {app.handler}")
    elif app.start:
        port = (
            f" ({app.port_env} 환경변수로 포트 전달)"
            if app.port_env
            else f" (포트 {app.port})"
            if app.port
            else ""
        )
        steps.append(f"실행: {app.start}{port}")
    if constraints.persistent_paths:
        steps.append(f"영구 디스크 연결: {', '.join(constraints.persistent_paths)}")
    if constraints.single_instance and t != 5:
        steps.append("인스턴스를 1대로 고정 (메모리 상태·로컬 파일·프로세스 안 주기 작업이 있음)")
    steps += [f"스케줄러 등록: {j.cron} → POST {j.path} (Authorization: Bearer {j.auth_env})" for j in jobs]
    if app.health:
        steps.append(f"헬스체크: GET {app.health}")
    elif t in (2, 3, 4):
        steps.append("헬스체크: 포트가 열리는지만 확인 (헬스체크 경로 없음)")
    return steps


def env_example(spec: DeploySpec) -> str:
    """.env.example 내용. 비밀값은 비워 두고, 설정값만 기본값·예시를 채운다."""
    lines = []
    for r in spec.resources:
        lines.append(
            f"# {RESOURCE_NAMES[r.kind]}: 배포할 때 백엔드가 채웁니다"
            + (f" ({r.scheme} 형식)" if r.scheme else "")
        )
        lines += [f"{name}=" for name in r.env.values()]
    for e in spec.env:
        if note := " — ".join(filter(None, [e.description, e.how])):
            lines.append(f"# {note}")
        lines.append(f"{e.name}=" + ((e.default or e.example or "") if e.kind == "config" else ""))
    if spec.app.port_env:
        lines += ["# 앱이 듣는 포트", f"{spec.app.port_env}={spec.app.port or ''}"]
    return "\n".join(lines) + "\n"


def generate_secret() -> str:
    """generate가 True인 환경변수에 넣을 무작위 값"""
    return secrets.token_urlsafe(32)


def _first_evidence(hits: list) -> str:
    return hits[0]["evidence"] if hits else "-"


def build_spec(state: dict, reasons: list, remaining: list) -> DeploySpec:
    """finish 단계의 상태로 명세를 만든다. reasons는 수정으로 풀 수 없는 탈락 이유, remaining은 남은 수정 id.
    AI 검사관 보고(state['reports'])가 있으면 실행 방법·외부 저장소·환경변수를 거기서 가져오고, 없으면 패턴 레시피를 쓴다."""
    root = Path(state["path"])
    signals, langs, t = state["signals"], state["languages"], state["target"]
    reports = state.get("reports") or {}

    def yes(name):
        return signals[name]["value"] == "yes"

    sources = list(_source_files(root, langs))
    lines = [line for _, _, ls in sources for line in ls]
    stack = reports.get("stack")
    if stack:
        app = App(
            **{k: stack.get(k) for k in App.model_fields if k not in ("system_tools", "existing_dockerfile")}
        )
    else:
        app = _app(root, langs, signals, sources)
        app.framework = state.get("frameworks", {}).get(app.language)
    app.existing_dockerfile = "Dockerfile" if (root / "Dockerfile").is_file() else None
    if reports.get("system_packages"):
        app.system_tools = reports["system_packages"]["tools"] if yes("system_packages") else []
    elif yes("system_packages"):
        for h in signals["system_packages"]["hits"]:
            app.system_tools += [
                tool for key, tool in TOOLS.items() if key in h["text"] and tool not in app.system_tools
            ]
    if not stack and not app.static_output:
        if app.port_env is None:
            if any(READS_PORT_ENV.search(line) for line in lines):
                app.port_env, app.port = "PORT", _port(PORT_DEFAULT, lines)
            else:
                app.port = _port(PORT_FIXED, lines)
        m = next((m for line in lines if (m := HEALTH_ROUTE.search(line))), None)
        if m:
            app.health = m.group()[1:-1]
        elif app.language == "java" and has_dep(root, "java", "spring-boot-starter-actuator"):
            app.health = "/actuator/health"

    jobs = [Job(**s) for c in state.get("conversions", []) if c["applied"] for s in c.get("schedules", [])]
    envs = _env_reads(root, sources)
    if reports.get("resources"):
        resources = [
            Resource(kind=r["kind"], env=r["env"], scheme=r["scheme"], evidence=_first_evidence(r["hits"]))
            for r in reports["resources"]["items"]
        ]
    else:
        resources = _resources(root, langs, list(envs))
    used = {"PORT"} | {n for r in resources for n in r.env.values()} | ({"CRON_TOKEN"} if jobs else set())
    if reports.get("env"):
        env = [
            EnvVar(
                **{
                    k: e[k] for k in ("name", "kind", "required", "default", "description", "how", "generate")
                },
                evidence=_first_evidence(e["hits"]),
            )
            for e in reports["env"]["items"]
            if e["name"] not in used
        ]
    else:
        env = _env_vars(envs, used)

    paths = []
    if yes("sqlite") or yes("file_write"):
        for line in lines:
            for m in PERSIST.finditer(line):
                p = next(g for g in m.groups() if g)
                if p not in paths:
                    paths.append(p)

    blockers = list(reasons) + [
        f"적용하지 않은 수정이 남았습니다: {i}" for i in remaining if i not in offer_table.SMALL
    ]
    if t in (2, 3, 4):
        if not (app.start or app.existing_dockerfile):
            blockers.append("시작 명령을 찾지 못했습니다")
        if not (app.port_env or app.port):
            blockers.append("앱이 듣는 포트를 찾지 못했습니다")
        if yes("bind_localhost"):
            blockers.append("127.0.0.1에만 바인딩해 밖에서 접속할 수 없습니다 (config 수정 필요)")
    if t == 1 and not app.handler and "faas_adapter" not in remaining:
        blockers.append("FaaS 핸들러를 찾지 못했습니다")
    if t == 5 and not app.static_output:
        blockers.append("올릴 정적 파일 폴더를 찾지 못했습니다")
    for r in resources:
        if not ({"bucket"} if r.kind == "s3" else {"url", "host"}) & set(r.env):
            blockers.append(
                f"{r.kind} 접속 정보를 받는 환경변수를 찾지 못했습니다 (코드에 박혀 있을 수 있습니다)"
            )

    constraints = Constraints(
        single_instance=any(yes(n) for n in ("memory_state", "sqlite", "file_write", "scheduler")),
        persistent_paths=paths,
        long_connections=yes("long_running"),
        gpu=yes("gpu"),
        os="windows" if yes("os_windows") else "linux",
    )
    return DeploySpec(
        ready=not blockers,
        blockers=blockers,
        path=str(root),
        target=t,
        target_name=TYPES[t],
        services=TYPE_INFO[t]["services"],
        app=app,
        resources=resources,
        env=env,
        schedules=jobs,
        constraints=constraints,
        steps=_steps(t, app, resources, env, jobs, constraints),
    )
