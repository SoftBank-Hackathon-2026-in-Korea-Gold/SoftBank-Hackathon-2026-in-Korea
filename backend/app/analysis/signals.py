"""1단계: 코드에서 배포 유형을 가를 신호를 뽑는다.

신호 값은 yes / no / unknown 세 가지다. unknown은 규칙으로 판정할 수 없다는 뜻이고, LLM 단계로 넘어간다.
각 신호에는 근거(파일:줄 + 그 줄의 코드)를 남긴다.

규칙은 넓게 잡는다. 맥락을 봐야 하는 패턴(파일 쓰기, 전역 변수 등)은 'check'로 적는다.
check만 걸린 신호는 yes이되 sure=False라서 LLM이 검증한다. LLM이 없으면 그대로 yes(위험한 쪽)로 둔다.
"""

import json
import re
from pathlib import Path

# 후보를 거르는 신호
SIGNALS = [
    "has_server",  # 요청을 받는 서버 코드가 있다
    "is_spa",  # 빌드해서 정적 파일로 배포하는 프런트엔드다
    "long_running",  # WebSocket, 백그라운드 워커 등 상주 작업이 있다
    "scheduler",  # 프로세스 안에서 도는 주기 작업(cron, setInterval)이 있다
    "memory_state",  # 메모리에만 든 상태(전역 카운터, 메모리 세션)가 있다
    "sqlite",  # SQLite 파일 DB를 쓴다
    "file_write",  # 그 밖에 로컬 디스크에 영구 저장한다 (업로드 폴더 등)
    "gpu",  # GPU가 필요하다
    "system_packages",  # OS 패키지(ffmpeg, chromium 등)가 필요하다
    "os_windows",  # Windows에서만 돈다
    "os_macos",  # macOS에서만 돈다
]

# 후보와 상관없이 배포 전에 손볼 것
PREP = [
    "port_hardcoded",  # 포트를 PORT 환경변수로 받지 않는다
    "bind_localhost",  # 127.0.0.1에만 바인딩한다
    "hardcoded_config",  # DB 주소·비밀값이 코드에 박혀 있다
    "no_health",  # 헬스체크 엔드포인트가 없다
    "no_dockerfile",  # Dockerfile이 없다
    "faas_adapter",  # FaaS 어댑터(mangum, serverless-http)가 이미 있다
]

SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    "vendor",
    ".next",
}
MAX_FILE_BYTES = 200_000
# 테스트 코드는 배포해서 돌리지 않으니 신호의 근거로 쓰지 않는다
TEST_DIRS = {"test", "tests", "__tests__", "e2e"}
TEST_FILE = re.compile(
    r"test_.*\.py|.*_test\.(?:py|go)|conftest\.py|.*\.(?:test|spec)\.[jt]sx?|.*Tests?\.(?:java|kt)"
)

# 언어 감지: 매니페스트 파일 → 소스 확장자
LANGS = {
    "node": (["package.json"], (".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx")),
    "python": (["requirements.txt", "pyproject.toml", "Pipfile"], (".py",)),
    "go": (["go.mod"], (".go",)),
    "java": (["pom.xml", "build.gradle", "build.gradle.kts"], (".java", ".kt")),
    "ruby": (["Gemfile"], (".rb",)),
    "php": (["composer.json"], (".php",)),
    "rust": (["Cargo.toml"], (".rs",)),
    "dotnet": (["*.csproj"], (".cs", ".csproj")),
}

# 의존성 이름 → 신호. (신호, 값, [의존성 이름])
DEPS = {
    "node": [
        ("has_server", "yes", ["express", "fastify", "koa", "hono", "next", "@nestjs/core", "@hapi/hapi"]),
        ("is_spa", "yes", ["vite", "react-scripts", "@angular/cli", "@vue/cli-service"]),
        ("long_running", "yes", ["socket.io", "ws", "bull", "bullmq", "agenda"]),
        ("scheduler", "yes", ["node-cron", "cron", "node-schedule"]),
        ("sqlite", "yes", ["sqlite3", "better-sqlite3"]),
        ("gpu", "yes", ["@tensorflow/tfjs-node-gpu"]),
        ("system_packages", "yes", ["puppeteer", "fluent-ffmpeg", "canvas", "node-tesseract-ocr"]),
        ("os_windows", "yes", ["node-windows", "winreg", "edge-js"]),
        ("os_macos", "yes", ["node-mac-permissions", "run-applescript"]),
        (
            "faas_adapter",
            "yes",
            ["serverless-http", "@vendia/serverless-express", "@codegenie/serverless-express"],
        ),
    ],
    "python": [
        (
            "has_server",
            "yes",
            ["flask", "fastapi", "django", "starlette", "aiohttp", "sanic", "tornado", "bottle"],
        ),
        ("long_running", "yes", ["celery", "rq", "flask-socketio", "channels", "dramatiq"]),
        ("scheduler", "yes", ["apscheduler", "schedule"]),
        ("sqlite", "yes", ["aiosqlite"]),
        ("gpu", "yes", ["tensorflow-gpu", "cupy"]),
        ("gpu", "unknown", ["torch", "tensorflow", "jax"]),  # CPU로만 쓰는 경우도 많다
        (
            "system_packages",
            "yes",
            ["opencv-python", "pdf2image", "pytesseract", "weasyprint", "psycopg2", "playwright", "selenium"],
        ),
        ("os_windows", "yes", ["pywin32", "pywinauto", "wmi", "comtypes"]),
        ("os_macos", "yes", ["pyobjc", "pyobjc-core", "rumps", "appscript"]),
        ("faas_adapter", "yes", ["mangum", "serverless-wsgi", "apig-wsgi"]),
    ],
    "go": [
        (
            "has_server",
            "yes",
            [
                "github.com/gin-gonic/gin",
                "github.com/labstack/echo",
                "github.com/gofiber/fiber",
                "github.com/go-chi/chi",
                "github.com/gorilla/mux",
            ],
        ),
        ("long_running", "yes", ["github.com/gorilla/websocket"]),
        ("scheduler", "yes", ["github.com/robfig/cron"]),
        ("sqlite", "yes", ["github.com/mattn/go-sqlite3", "modernc.org/sqlite"]),
    ],
    "java": [
        (
            "has_server",
            "yes",
            [
                "spring-boot-starter-web",
                "spring-boot-starter-webflux",
                "quarkus-resteasy",
                "micronaut-http-server",
                "javalin",
            ],
        ),
        ("long_running", "yes", ["spring-boot-starter-websocket"]),
        ("scheduler", "yes", ["quartz"]),
        ("sqlite", "yes", ["sqlite-jdbc"]),
    ],
    "ruby": [
        ("has_server", "yes", ["rails", "sinatra", "hanami", "roda"]),
        ("long_running", "yes", ["sidekiq", "actioncable", "resque"]),
        ("scheduler", "yes", ["whenever", "rufus-scheduler"]),
        ("sqlite", "yes", ["sqlite3"]),
        ("system_packages", "yes", ["mini_magick", "rmagick", "grover"]),
    ],
    "php": [
        ("has_server", "yes", ["laravel/framework", "symfony/framework-bundle", "slim/slim"]),
        ("long_running", "yes", ["cboden/ratchet", "beyondcode/laravel-websockets"]),
        ("sqlite", "yes", ["ext-pdo_sqlite"]),
    ],
    "rust": [
        ("has_server", "yes", ["actix-web", "axum", "rocket", "warp"]),
        ("long_running", "yes", ["tokio-tungstenite"]),
        ("scheduler", "yes", ["tokio-cron-scheduler"]),
        ("sqlite", "yes", ["rusqlite"]),
        ("gpu", "yes", ["cudarc"]),
    ],
    "dotnet": [
        ("has_server", "yes", ["Microsoft.NET.Sdk.Web", "Microsoft.AspNetCore"]),
        ("long_running", "yes", ["Microsoft.AspNetCore.SignalR"]),
        ("scheduler", "yes", ["Hangfire", "Quartz"]),
        ("sqlite", "yes", ["Microsoft.Data.Sqlite", "Microsoft.EntityFrameworkCore.Sqlite"]),
    ],
}

# 의존성 → 프레임워크. 언어마다 위에서부터 처음 맞는 것 (Next.js가 React보다, NestJS가 Express보다 먼저)
FRAMEWORKS = {
    "node": [
        ("Next.js", ["next"]),
        ("NestJS", ["@nestjs/core"]),
        ("Express", ["express"]),
        ("Fastify", ["fastify"]),
        ("Koa", ["koa"]),
        ("Hono", ["hono"]),
        ("React (Vite)", ["react", "vite"]),
        ("Vue (Vite)", ["vue", "vite"]),
        ("React (CRA)", ["react-scripts"]),
        ("Angular", ["@angular/core"]),
        ("Svelte", ["svelte"]),
    ],
    "python": [
        ("Django", ["django"]),
        ("FastAPI", ["fastapi"]),
        ("Flask", ["flask"]),
        ("Starlette", ["starlette"]),
        ("aiohttp", ["aiohttp"]),
        ("Sanic", ["sanic"]),
        ("Tornado", ["tornado"]),
        ("Bottle", ["bottle"]),
    ],
    "go": [
        ("Gin", ["github.com/gin-gonic/gin"]),
        ("Echo", ["github.com/labstack/echo"]),
        ("Fiber", ["github.com/gofiber/fiber"]),
        ("Chi", ["github.com/go-chi/chi"]),
        ("Gorilla", ["github.com/gorilla/mux"]),
    ],
    "java": [
        ("Spring Boot", ["spring-boot-starter-web"]),
        ("Spring Boot (WebFlux)", ["spring-boot-starter-webflux"]),
        ("Quarkus", ["quarkus-resteasy"]),
        ("Micronaut", ["micronaut-http-server"]),
        ("Javalin", ["javalin"]),
    ],
    "ruby": [("Rails", ["rails"]), ("Sinatra", ["sinatra"])],
    "php": [
        ("Laravel", ["laravel/framework"]),
        ("Symfony", ["symfony/framework-bundle"]),
        ("Slim", ["slim/slim"]),
    ],
    "rust": [("Actix Web", ["actix-web"]), ("Axum", ["axum"]), ("Rocket", ["rocket"])],
    "dotnet": [("ASP.NET Core", ["Microsoft.NET.Sdk.Web"])],
}

ALL = None  # 모든 언어에 적용
SYS_TOOLS = r"(?:ffmpeg|ffprobe|convert|magick|tesseract|pdftoppm|libreoffice|soffice|chromium|wkhtmltopdf)"

# 코드 패턴 → 신호. (언어들, 신호, 값, 정규식). 값: yes(확실) / check(맥락을 봐야 함) / unknown(판정 못 함)
CODE = [
    (("node",), "has_server", "yes", r"\bhttp\.createServer\(|\.listen\(\s*(?:port|PORT|\d)"),
    (("python",), "has_server", "yes", r"\bHTTPServer\(|\bweb\.run_app\("),
    (("go",), "has_server", "yes", r"\bhttp\.ListenAndServe\("),
    (("node",), "scheduler", "yes", r"\bcron\.schedule\("),
    (("node",), "scheduler", "check", r"^setInterval\("),  # 캐시 갱신 같은 것일 수도 있다
    (("node",), "scheduler", "unknown", r"^\s+setInterval\("),
    (("java",), "scheduler", "yes", r"@Scheduled\b|@EnableScheduling\b"),
    (
        ("python",),
        "long_running",
        "yes",
        r"^\s*(?:import|from) websockets\b|@\w+\.websocket\(|\bflask_socketio\b",
    ),
    (("python",), "long_running", "unknown", r"\bthreading\.Thread\(|\bwhile True:"),
    (ALL, "sqlite", "yes", r"sqlite:/{2,3}"),
    (("python",), "sqlite", "yes", r"^\s*(?:import|from) sqlite3\b"),
    # 파일 쓰기는 캐시·로그·임시 파일일 수도 있어 check다. 업로드 저장 폴더(multer dest)는 확실하다
    (
        ("python",),
        "file_write",
        "check",
        r'\bopen\([^)]*,\s*[\'"][wax]b?\+?[\'"]|\.write_text\(|\.write_bytes\(',
    ),
    (("python",), "file_write", "unknown", r'\.save\(\s*(?:os\.path\.join|f?[\'"]|UPLOAD)'),
    (
        ("node",),
        "file_write",
        "check",
        r"\bfs\.(?:promises\.)?(?:writeFile|appendFile|createWriteStream)(?:Sync)?\(",
    ),
    (("node",), "file_write", "yes", r"\bmulter\(\s*\{\s*dest"),
    (
        ("java",),
        "file_write",
        "check",
        r"\bFiles\.(?:write|writeString|copy|newOutputStream|newBufferedWriter)\(|\bnew File(?:OutputStream|Writer)\(",
    ),
    (
        ("java",),
        "file_write",
        "unknown",
        r"\.transferTo\(",
    ),  # 업로드 파일 저장일 수도, 스트림 복사일 수도 있다
    (("python",), "gpu", "yes", r'\.cuda\(|device\s*=\s*[\'"]cuda|torch\.device\(\s*[\'"]cuda'),
    (
        ALL,
        "system_packages",
        "yes",
        r'(?:subprocess\.\w+|os\.system|exec(?:Sync|File)?|spawn)\(\s*\[?\s*[\'"`]' + SYS_TOOLS + r"\b",
    ),
    (
        ("python",),
        "os_windows",
        "yes",
        r"^\s*(?:import|from) (?:winreg|win32api|win32com|win32con|wmi|msvcrt)\b|\bos\.startfile\(|\bctypes\.windll\b",
    ),
    (
        ("dotnet",),
        "os_windows",
        "yes",
        r"<TargetFramework>net[\d.]+-windows|<UseWindowsForms>true|<UseWPF>true",
    ),
    (("python",), "os_macos", "yes", r"^\s*(?:import|from) (?:AppKit|Foundation|objc|Quartz)\b"),
    (ALL, "os_macos", "yes", r"\bosascript\b"),
    (ALL, "os_windows", "unknown", r'[\'"][A-Za-z]:\\\\'),
    (ALL, "os_macos", "unknown", r'[\'"]/(?:Users|Applications|opt/homebrew)/'),
    # 배포 준비
    (("node",), "port_hardcoded", "yes", r"\.listen\(\s*\d{2,5}\b"),
    (("python",), "port_hardcoded", "yes", r"\.run\([^)]*\bport\s*=\s*\d+"),
    (("go",), "port_hardcoded", "yes", r'(?:ListenAndServe|\.Run)\(\s*"[^"]*:\d+"'),
    (
        ALL,
        "bind_localhost",
        "yes",
        r'(?:host\s*=\s*|\.listen\([^)]*,\s*)[\'"](?:127\.0\.0\.1|localhost)[\'"]',
    ),
    # {user}·${user}처럼 변수로 조립한 주소는 박힌 값이 아니다
    (
        ALL,
        "hardcoded_config",
        "yes",
        r'[\'"](?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\'"\s{$]*@',
    ),
    # IP와 비밀값처럼 보이는 문자열은 예시·테스트·로컬 기본값일 수 있다
    (
        ALL,
        "hardcoded_config",
        "check",
        r'[\'"](?!127\.0\.0\.1|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?[\'"]',
    ),
    (
        ALL,
        "hardcoded_config",
        "check",
        r'(?i)\b\w*(?:secret|password|passwd|api_key|token)\w*\s*[:=]\s*[\'"][^\'"\s]{6,}[\'"]',
    ),
]

READS_PORT_ENV = re.compile(r'process\.env\.PORT|environ(?:\.get\(|\[)\s*[\'"]PORT|[gG]etenv\(\s*[\'"]PORT')
HEALTH_ROUTE = re.compile(r'[\'"]/health(?:z)?[\'"]')

# 메모리 상태: 모듈 최상위에 선언한 가변 값을 핸들러 안에서 바꾸는지 본다
PY_GLOBAL = re.compile(
    r"^(\w+)\s*(?::[^=]+)?=\s*(\{\}|\[\]|dict\(\)|list\(\)|set\(\)|defaultdict\(.*\)|Counter\(\)|\d+)\s*$"
)
JS_GLOBAL = re.compile(r"^(?:let|var|const)\s+(\w+)\s*=\s*(\{\}|\[\]|new (?:Map|Set)\(\)|\d+)\s*;?\s*$")
# 자바는 필드(정적 필드, 싱글턴 빈의 필드)다. 접근 제어자나 static이 붙어야 필드로 본다 (지역 변수는 뺀다)
JAVA_FIELD = re.compile(
    r"^\s*(?:private|protected|public|static)\s+(?:(?:static|final|volatile)\s+)*[\w<>,.?\[\] ]+?\s+(\w+)\s*=\s*"
    r"(?:new\s+(?:[\w.]+\.)?(?:HashMap|ConcurrentHashMap|LinkedHashMap|TreeMap|ArrayList|LinkedList|HashSet"
    r"|CopyOnWriteArrayList|AtomicInteger|AtomicLong)\b|-?\d+\s*;)"
)
MEMORY_DECL = {"python": PY_GLOBAL, "node": JS_GLOBAL, "java": JAVA_FIELD}

# Spring 설정 파일 (application.properties, application-prod.yml 등)과 거기 박힌 비밀값. ${...}는 환경변수라 괜찮다
CONFIG_FILES = re.compile(r"application[\w-]*\.(?:properties|ya?ml)")
CONFIG_SECRET = re.compile(
    r'(?i)^\s*[\w.-]*(?:password|secret|token|api[-_]?key)[\w.-]*\s*[=:]\s*(?![\'"]?\$\{)\S'
)


def config_files(root: Path) -> list[Path]:
    """application*.yml과, spring.config.import로 나눠 불러오는 resources 폴더의 설정 파일 (db.yml, minio.yml …)"""
    return [
        p
        for p in _walk(root)
        if CONFIG_FILES.fullmatch(p.name)
        or (p.suffix in (".yml", ".yaml", ".properties") and "resources" in p.relative_to(root).parts)
    ]


def _mutates(name: str, lang: str, line: str) -> bool:
    n = re.escape(name)
    if lang == "python":
        return bool(
            re.search(
                rf"\bglobal\b.*\b{n}\b|\b{n}\[[^\]]*\]\s*[+\-]?=|\b{n}\.(?:append|add|update|setdefault|pop|extend|insert)\(",
                line,
            )
        )
    if lang == "java":
        return bool(
            re.search(
                rf"\b{n}\s*(?:\+\+|--|[+\-*]?=(?!=))|(?:\+\+|--){n}\b|\b{n}\.(?:put|putIfAbsent|add|addAll|remove|clear|set"
                rf"|incrementAndGet|getAndIncrement|decrementAndGet|addAndGet|compute\w*|merge)\(",
                line,
            )
        )
    return bool(
        re.search(
            rf"\b{n}\s*(?:\+\+|--|[+\-]?=(?!=))|\b{n}\[[^\]]*\]\s*=(?!=)|\b{n}\.(?:set|add|push|delete)\(",
            line,
        )
    )


def read_text(path: Path) -> str:
    """BOM을 보고 인코딩을 고른다. Windows PowerShell에서 `pip freeze > requirements.txt`를 하면 UTF-16으로 저장된다."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    return raw.decode("utf-8-sig", errors="ignore")


def _walk(root: Path):
    for p in sorted(root.iterdir()):
        if p.is_dir():
            if p.name not in SKIP_DIRS | TEST_DIRS and not p.name.startswith("."):
                yield from _walk(p)
        elif p.is_file() and p.stat().st_size <= MAX_FILE_BYTES and not TEST_FILE.fullmatch(p.name):
            yield p


def _manifests(root: Path, lang: str):
    names, _ = LANGS[lang]
    return [p for n in names for p in root.glob(n) if p.is_file()]


def detect_languages(root: Path) -> list[str]:
    return [lang for lang in LANGS if _manifests(root, lang)]


def _hit(path: Path, root: Path, lineno: int, line: str):
    return {"evidence": f"{path.relative_to(root)}:{lineno}", "text": line.strip()[:200]}


def _find_line(path: Path, needle: str):
    for i, line in enumerate(read_text(path).splitlines(), 1):
        if needle in line:
            return i, line
    return 1, needle


def _dep_pattern(name: str):
    # 다른 이름의 일부로 걸리지 않게 앞뒤 경계를 둔다 (psycopg2 ≠ psycopg2-binary)
    return re.compile(r"(?<![\w./-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE)


def _node_deps(root: Path) -> dict:
    p = root / "package.json"
    if not p.is_file():
        return {}
    pkg = json.loads(read_text(p))
    return {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}


def _dep_hits(root: Path, lang: str, table=DEPS):
    """(신호, 값, hit) 목록. node는 package.json을 파싱하고, 나머지는 매니페스트 본문에서 이름을 찾는다."""
    out = []
    for manifest in _manifests(root, lang):
        if lang == "node":
            names = _node_deps(root)
            for signal, value, deps in table.get(lang, []):
                for d in deps:
                    if d in names:
                        out.append((signal, value, _hit(manifest, root, *_find_line(manifest, f'"{d}"'))))
            continue
        lines = read_text(manifest).splitlines()
        for signal, value, deps in table.get(lang, []):
            for d in deps:
                pat = _dep_pattern(d)
                for i, line in enumerate(lines, 1):
                    if pat.search(line) and not line.lstrip().startswith("#"):
                        out.append((signal, value, _hit(manifest, root, i, line)))
                        break
    return out


def has_dep(root: Path, lang: str, name: str) -> bool:
    return bool(_dep_hits(root, lang, {lang: [(name, "", [name])]}))


def detect_frameworks(root: Path, langs: list[str]) -> dict:
    """{언어: 프레임워크}. 못 찾은 언어는 뺀다."""
    out = {}
    for lang in langs:
        name = next(
            (n for n, deps in FRAMEWORKS.get(lang, []) if all(has_dep(root, lang, d) for d in deps)), None
        )
        if name:
            out[lang] = name
    return out


def _source_files(root: Path, langs: list[str]):
    exts = {ext: lang for lang in langs for ext in LANGS[lang][1]}
    for path in _walk(root):
        if path.suffix in exts:
            yield path, exts[path.suffix], read_text(path).splitlines()


def _code_hits(root: Path, langs: list[str]):
    rules = [(set(ls) if ls else None, s, v, re.compile(rx)) for ls, s, v, rx in CODE]
    out = []
    for path, lang, lines in _source_files(root, langs):
        for i, line in enumerate(lines, 1):
            for ls, signal, value, rx in rules:
                if (ls is None or lang in ls) and rx.search(line):
                    out.append((signal, value, _hit(path, root, i, line)))
    return out


def _memory_hits(root: Path, langs: list[str]):
    out = []
    for path, lang, lines in _source_files(root, langs):
        if lang not in MEMORY_DECL:
            continue
        for i, line in enumerate(lines, 1):
            m = MEMORY_DECL[lang].match(line)
            if not m:
                continue
            # 선언 줄을 뺀 나머지 중 들여쓰기된(함수 안) 줄에서 값을 바꾸면 상태로 본다
            if any(
                j != i and l[:1] in (" ", "\t") and _mutates(m.group(1), lang, l)
                for j, l in enumerate(lines, 1)
            ):
                out.append(("memory_state", "check", _hit(path, root, i, line)))  # 잃어도 되는 캐시일 수 있다
    # express-session에 store를 안 주면 기본값이 메모리 저장소다
    if (
        "node" in langs
        and "express-session" in _node_deps(root)
        and not any(
            re.search(r"\bstore\s*:", l) for _, _, lines in _source_files(root, ["node"]) for l in lines
        )
    ):
        manifest = root / "package.json"
        out.append(("memory_state", "yes", _hit(manifest, root, *_find_line(manifest, '"express-session"'))))
    return out


def _config_hits(root: Path):
    out = []
    for path in config_files(root):
        for i, line in enumerate(read_text(path).splitlines(), 1):
            if CONFIG_SECRET.search(line) and not line.lstrip().startswith("#"):
                out.append(("hardcoded_config", "check", _hit(path, root, i, line)))  # 로컬 기본값일 수 있다
    return out


def _node_build_script(root: Path) -> bool:
    p = root / "package.json"
    return p.is_file() and "build" in json.loads(read_text(p)).get("scripts", {})


def scan(path: str) -> dict:
    """프로젝트 폴더를 읽어 {'languages': [...], 'frameworks': {언어: 이름}, 'signals': {신호: {...}}}를 돌려준다."""
    root = Path(path).resolve()
    langs = detect_languages(root)
    hits = (
        [h for lang in langs for h in _dep_hits(root, lang)]
        + _code_hits(root, langs)
        + _memory_hits(root, langs)
        + _config_hits(root)
    )

    signals = {}
    for name in SIGNALS + PREP:
        yes = [h for s, v, h in hits if s == name and v in ("yes", "check")]
        unknown = [h for s, v, h in hits if s == name and v == "unknown"]
        value = "yes" if yes else "unknown" if unknown else "no"
        sure = any(s == name and v == "yes" for s, v, _ in hits)  # check만 걸렸으면 LLM이 검증한다
        signals[name] = {"value": value, "hits": yes or unknown, "by": "rule", "reason": "", "sure": sure}

    # SPA는 서버 없이 빌드 스크립트가 있을 때만. 매니페스트 없이 index.html만 있으면 정적 사이트다.
    spa = signals["is_spa"]
    if spa["value"] == "yes" and (signals["has_server"]["value"] == "yes" or not _node_build_script(root)):
        spa.update(value="no", hits=[])
    if not langs and (root / "index.html").is_file():
        spa.update(value="yes", hits=[{"evidence": "index.html:1", "text": "매니페스트 없는 정적 사이트"}])

    # 서버 프레임워크도 SPA도 못 찾으면 웹 서비스인지 규칙으로는 알 수 없다
    if signals["has_server"]["value"] == "no" and spa["value"] != "yes":
        signals["has_server"]["value"] = "unknown"

    sources = [l for _, _, lines in _source_files(root, langs) for l in lines]
    # pip freeze처럼 설치된 패키지를 전부 적은 의존성 파일이 많다. 코드·설정 어디에도 sqlite가 없으면 쓰는지 알 수 없다
    configs = [l for p in config_files(root) for l in read_text(p).splitlines()]
    if signals["sqlite"]["value"] == "yes" and not any("sqlite" in l.lower() for l in sources + configs):
        signals["sqlite"]["value"] = "unknown"

    # 배포 준비 신호는 서버가 있을 때만 의미가 있다
    if any(READS_PORT_ENV.search(l) for l in sources):
        signals["port_hardcoded"].update(value="no", hits=[])
    if signals["has_server"]["value"] == "yes":
        # Spring actuator는 /actuator/health를 만들어 준다
        if not any(HEALTH_ROUTE.search(l) for l in sources) and not has_dep(
            root, "java", "spring-boot-starter-actuator"
        ):
            signals["no_health"].update(
                value="yes", sure=True, hits=[{"evidence": "-", "text": "/health 경로를 찾지 못함"}]
            )
        if not (root / "Dockerfile").is_file():
            signals["no_dockerfile"].update(
                value="yes", sure=True, hits=[{"evidence": "-", "text": "Dockerfile 없음"}]
            )

    return {"languages": langs, "frameworks": detect_frameworks(root, langs), "signals": signals}
