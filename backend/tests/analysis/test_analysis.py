import shutil
from pathlib import Path

import pytest

from app.analysis import inspectors as ai
from app.analysis import llm, offers
from app.analysis.graph import report, run
from app.analysis.llm import Conversion, Evidence, FileEdit, Schedule, SignalReport
from app.analysis.rules import apply_rules
from app.analysis.signals import PREP, SIGNALS, scan
from app.analysis.spec import DeploySpec, env_example, generate_secret

NEW_FIXTURES = Path(__file__).resolve().parent / "fixtures"
APP_FIXTURES = NEW_FIXTURES / "apps"


@pytest.mark.parametrize(
    "path, candidates, review",
    [
        (APP_FIXTURES / "vite-react-landing", [5], []),
        (APP_FIXTURES / "node-pg-visits", [1, 2, 3, 4], []),
        (APP_FIXTURES / "express-redis-counter", [1, 2, 3, 4], []),
        (APP_FIXTURES / "fastapi-mysql-notes", [1, 2, 3, 4], []),
        (APP_FIXTURES / "flask-sqlite-todo", [4], []),
        (NEW_FIXTURES / "go-gin-ws", [2, 3, 4], []),
        (NEW_FIXTURES / "node-pdf-puppeteer", [2, 4], []),
        (NEW_FIXTURES / "py-win-report", [4], []),
        (NEW_FIXTURES / "py-torch-infer", [1, 2, 3, 4], ["gpu"]),
        (NEW_FIXTURES / "py-mac-menubar", [], ["has_server"]),
        (NEW_FIXTURES / "node-upload-session", [4], []),
        (NEW_FIXTURES / "py-memory-config", [4], []),
        (NEW_FIXTURES / "java-spring-pg", [1, 2, 3, 4], []),
    ],
)
def test_fixture_candidates(path, candidates, review):
    result = apply_rules(scan(path)["signals"])
    assert result["candidates"] == candidates
    assert list(result["review"]) == review


def signals(**values):
    return {
        name: {"value": values.get(name, "no"), "hits": [], "by": "rule", "reason": ""}
        for name in SIGNALS + PREP
    }


@pytest.mark.parametrize(
    "values, candidates",
    [
        ({"is_spa": "yes"}, [5]),
        ({"has_server": "yes"}, [1, 2, 3, 4]),
        ({"has_server": "yes", "long_running": "yes"}, [2, 3, 4]),
        ({"has_server": "yes", "sqlite": "yes"}, [4]),
        ({"has_server": "yes", "file_write": "yes"}, [4]),
        ({"has_server": "yes", "gpu": "yes"}, [4]),
        ({"has_server": "yes", "system_packages": "yes"}, [2, 4]),
        ({"has_server": "yes", "os_windows": "yes"}, [4]),
        ({"has_server": "yes", "os_macos": "yes"}, []),
        ({"has_server": "yes", "scheduler": "yes"}, [2, 3, 4]),
        ({"has_server": "yes", "memory_state": "yes"}, [4]),
        ({"has_server": "yes", "gpu": "unknown"}, [1, 2, 3, 4]),  # unknown은 지우지 않는다
    ],
)
def test_rules(values, candidates):
    assert apply_rules(signals(**values))["candidates"] == candidates


def test_macos_blocks_deploy():
    assert apply_rules(signals(has_server="yes", os_macos="yes"))["blocked"]


def test_dependency_name_boundary(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\npsycopg2-binary\n")
    assert scan(tmp_path)["signals"]["system_packages"]["value"] == "no"
    (tmp_path / "requirements.txt").write_text("flask\npsycopg2\n")
    assert scan(tmp_path)["signals"]["system_packages"]["value"] == "yes"


@pytest.mark.parametrize(
    "path, expected",
    [
        (
            NEW_FIXTURES / "node-upload-session",
            {
                "scheduler",
                "memory_state",
                "file_write",
                "port_hardcoded",
                "bind_localhost",
                "no_health",
                "no_dockerfile",
            },
        ),
        (
            NEW_FIXTURES / "py-memory-config",
            {"memory_state", "port_hardcoded", "bind_localhost", "hardcoded_config", "no_dockerfile"},
        ),
        (APP_FIXTURES / "node-pg-visits", {"no_dockerfile"}),  # PORT 환경변수를 읽고 /health도 있다
    ],
)
def test_prep_and_state_signals(path, expected):
    found = {k for k, v in scan(path)["signals"].items() if v["value"] == "yes"} - {"has_server"}
    assert found == expected


def test_memory_state_needs_mutation(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "app.py").write_text(
        'ROUTES = {}\nLIMIT = 10\n\ndef f():\n    return ROUTES.get("a", LIMIT)\n'
    )
    assert scan(tmp_path)["signals"]["memory_state"]["value"] == "no"
    (tmp_path / "app.py").write_text('ROUTES = {}\n\ndef f():\n    ROUTES["a"] = 1\n')
    assert scan(tmp_path)["signals"]["memory_state"]["value"] == "yes"


def test_express_session_with_store_is_fine(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"express": "4", "express-session": "1", "connect-redis": "7"}}'
    )
    (tmp_path / "index.js").write_text("app.use(session({ store: new RedisStore({ client }) }));\n")
    assert scan(tmp_path)["signals"]["memory_state"]["value"] == "no"


# ---------- 제안 ----------


def plan_ids(path, target):
    r = scan(path)
    fixes, reasons = offers.plan(target, r["signals"], r["languages"])
    return [o["id"] for o in fixes], reasons


@pytest.mark.parametrize(
    "path, target, ids, blocked",
    [
        (APP_FIXTURES / "flask-sqlite-todo", 4, ["health"], False),  # VM은 SQLite 그대로 간다
        (APP_FIXTURES / "flask-sqlite-todo", 2, ["sqlite", "health"], False),
        (APP_FIXTURES / "flask-sqlite-todo", 1, ["sqlite", "health", "faas_adapter"], False),
        (APP_FIXTURES / "flask-sqlite-todo", 5, [], True),  # 서버 코드라 정적 호스팅 불가
        (APP_FIXTURES / "node-pg-visits", 2, [], False),  # 고칠 것 없음
        (APP_FIXTURES / "vite-react-landing", 5, [], False),
        (
            NEW_FIXTURES / "node-upload-session",
            2,
            ["storage", "redis", "config", "health"],
            False,
        ),  # 주기 작업은 FaaS만 막는다
        (NEW_FIXTURES / "node-pdf-puppeteer", 3, [], True),  # OS 패키지는 수정으로 못 푼다
        (NEW_FIXTURES / "go-gin-ws", 1, [], True),
    ],
)
def test_plan_per_type(path, target, ids, blocked):
    got, reasons = plan_ids(path, target)
    assert got == ids
    assert bool(reasons) == blocked


def test_offer_effect_messages():
    r = scan(NEW_FIXTURES / "node-upload-session")
    todo = [offers.BY_ID[i] for i in ["storage", "redis", "scheduler", "config", "health", "dockerfile"]]
    effect = {o["id"]: offers.effect(o, r["signals"], todo) for o in todo}
    assert (
        effect["storage"]
        == "유형 1은(는) redis, scheduler도 고치면 열립니다 / 유형 2, 3은(는) redis도 고치면 열립니다"
    )
    assert effect["scheduler"] == "유형 1은(는) storage, redis도 고치면 열립니다"
    assert effect["dockerfile"] == "배포 준비"
    r = scan(APP_FIXTURES / "flask-sqlite-todo")
    assert offers.effect(offers.BY_ID["sqlite"], r["signals"]) == "유형 1, 2, 3이(가) 다시 열립니다"


# ---------- 그래프: LLM 자리에 가짜 함수를 넣는다 ----------


def copy_app(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    return dst


@pytest.fixture
def sqlite_app(tmp_path):
    return copy_app(APP_FIXTURES / "flask-sqlite-todo", tmp_path)


def fake_converter(offer, hits, files):
    """제안마다 정해진 수정을 흉내 낸다."""
    app = files.get("app.py", "")
    if offer["id"] == "sqlite":
        app = app.replace("import sqlite3\n", "import psycopg\n").replace(
            "sqlite3.connect(DB_PATH)", 'psycopg.connect(os.environ["DATABASE_URL"])'
        )
        return Conversion(
            edits=[
                FileEdit(path="app.py", content=app),
                FileEdit(path="requirements.txt", content=files["requirements.txt"] + "psycopg[binary]\n"),
            ],
            summary="sqlite3를 psycopg로 바꿨습니다.",
        )
    if offer["id"] == "health":
        return Conversion(
            edits=[
                FileEdit(
                    path="app.py",
                    content=app + '\n@app.get("/health")\ndef health():\n    return {"ok": True}\n',
                )
            ],
            summary="/health를 추가했습니다.",
        )
    if offer["id"] == "faas_adapter":
        return Conversion(
            edits=[
                FileEdit(path="lambda_handler.py", content="import serverless_wsgi\n"),
                FileEdit(path="requirements.txt", content=files["requirements.txt"] + "serverless-wsgi\n"),
            ],
            summary="핸들러를 추가했습니다.",
        )
    raise AssertionError(offer["id"])


def test_vm_applies_only_small_fix_without_asking(sqlite_app):
    asked = []
    out = run(sqlite_app, target=4, converter=fake_converter, ask=lambda p: asked.append(p) or [])
    assert asked == []  # 작은 수정뿐이라 묻지 않는다
    assert [c["offer"] for c in out["conversions"]] == ["health"]
    assert "import sqlite3" in (sqlite_app / "app.py").read_text()  # SQLite는 건드리지 않는다
    r = out["result"]
    assert (r["target"], r["ready"], r["remaining"]) == (4, True, [])


def test_big_fix_is_asked_then_applied(sqlite_app):
    asked = []
    out = run(sqlite_app, target=2, converter=fake_converter, ask=lambda p: asked.append(p) or "all")
    assert [p["kind"] for p in asked] == ["fixes"]
    assert [o["id"] for o in asked[0]["offers"]] == ["sqlite"]  # 작은 수정(health)은 묻지 않는다
    assert asked[0]["offers"][0]["effect"] == "유형 1, 2, 3이(가) 다시 열립니다"
    assert [c["offer"] for c in out["conversions"]] == ["sqlite", "health"]
    assert "+import psycopg" in out["conversions"][0]["diff"]
    assert out["result"]["ready"] and 2 in out["result"]["candidates"]


def test_faas_needs_adapter(sqlite_app):
    out = run(sqlite_app, target=1, accept="all", converter=fake_converter)
    assert [c["offer"] for c in out["conversions"]] == ["sqlite", "health", "faas_adapter"]
    assert (sqlite_app / "lambda_handler.py").is_file()
    assert out["result"]["ready"] and out["result"]["tags"] == {}


def test_declining_big_fix_goes_back_to_choose(sqlite_app):
    before = (sqlite_app / "app.py").read_text()
    answers = iter([2, [], 4])  # 유형 2 → sqlite 거절 → 유형 4
    seen = []
    out = run(sqlite_app, converter=fake_converter, ask=lambda p: seen.append(p) or next(answers))
    assert [p["kind"] for p in seen] == ["choose", "fixes", "choose"]
    blocked = {o["type"]: o["blocked"] for o in seen[2]["options"]}
    assert blocked[2] == ["거절한 수정이 필요합니다: sqlite"]
    assert [c["offer"] for c in out["conversions"]] == ["health"]  # 유형 4에서 health는 다시 계획된다
    assert (
        "import sqlite3" in (sqlite_app / "app.py").read_text()
        and before != (sqlite_app / "app.py").read_text()
    )
    assert out["result"]["target"] == 4 and out["result"]["ready"]


def test_choose_nothing_only_reports(sqlite_app):
    before = (sqlite_app / "app.py").read_text()
    out = run(sqlite_app, ask=lambda p: None)
    assert "conversions" not in out and "target" not in out["result"]
    assert out["result"]["candidates"] == [4]
    assert (sqlite_app / "app.py").read_text() == before


def test_choose_options(sqlite_app):
    seen = []
    run(sqlite_app, ask=lambda p: seen.append(p) or None)
    opts = {o["type"]: o for o in seen[0]["options"]}
    assert opts[4]["now"] and [f["id"] for f in opts[4]["fixes"]] == ["health"]
    assert not opts[2]["now"] and [(f["id"], f["small"]) for f in opts[2]["fixes"]] == [
        ("sqlite", False),
        ("health", True),
    ]
    assert opts[5]["blocked"] and opts[5]["fixes"] == []
    assert opts[4]["deploy"] == "로컬·사내 서버 Docker"


def test_large_small_fix_needs_confirmation(sqlite_app):
    def big_health(offer, hits, files):
        return Conversion(
            edits=[FileEdit(path="app.py", content=files["app.py"] + "# x\n" * 40)],
            summary="크게 바꿨습니다.",
        )

    before = (sqlite_app / "app.py").read_text()
    seen = []
    out = run(sqlite_app, target=4, converter=big_health, ask=lambda p: seen.append(p) or [])
    assert [p["kind"] for p in seen] == ["confirm"]
    assert (sqlite_app / "app.py").read_text() == before  # 거절하면 쓰지 않는다
    r = out["result"]
    assert r["ready"] and r["remaining"] == ["health"]  # 작은 수정이 남은 건 배포를 막지 않는다

    out = run(sqlite_app, target=4, accept="all", converter=big_health)
    assert (sqlite_app / "app.py").read_text() != before


def test_held_fix_is_not_overwritten_by_later_fix(tmp_path):
    """실전 테스트에서 나온 버그: config가 확인을 기다리는 동안 health가 같은 파일을 고쳤고, 확인 뒤 config가 그걸 덮어썼다"""
    app = copy_app(NEW_FIXTURES / "node-upload-session", tmp_path)
    seen = []

    def converter(offer, hits, files):
        seen.append(offer["id"])
        server = files["server.js"]
        if offer["id"] == "config":  # 변경이 커서 확인을 받는다
            server = server.replace(
                "app.listen(3000, '127.0.0.1');", "app.listen(process.env.PORT || 3000, '0.0.0.0');"
            )
            return Conversion(
                edits=[FileEdit(path="server.js", content="// config\n" * 35 + server)], summary=""
            )
        return Conversion(
            edits=[
                FileEdit(
                    path="server.js",
                    content=server + "app.get('/health', (req, res) => res.json({ ok: true }));\n",
                )
            ],
            summary="",
        )

    kinds = []
    out = run(app, target=4, converter=converter, ask=lambda p: kinds.append(p["kind"]) or "all")
    text = (app / "server.js").read_text()
    assert kinds == ["confirm"] and seen == ["config", "health"]
    assert "'0.0.0.0'" in text and "'/health'" in text  # 둘 다 남는다
    assert [(c["offer"], c["applied"]) for c in out["conversions"]] == [("config", True), ("health", True)]
    assert out["result"]["spec"]["app"]["health"] == "/health"


def test_later_offers_see_earlier_edits(sqlite_app):
    seen = []

    def converter(offer, hits, files):
        seen.append((offer["id"], "import psycopg" in files["app.py"]))
        return fake_converter(offer, hits, files)

    run(sqlite_app, target=2, accept="all", converter=converter)
    assert seen == [("sqlite", False), ("health", True)]


def test_accept_without_llm(sqlite_app):
    out = run(sqlite_app, target=2, accept="all")
    assert not any(c["applied"] for c in out["conversions"])
    r = out["result"]
    assert not r["ready"] and r["remaining"] == ["sqlite", "health"]


@pytest.mark.parametrize("path", ["../evil.py", "secret.py", ".git/config", "sub/Dockerfile"])
def test_converter_cannot_write_outside_allowed_files(sqlite_app, path):
    bad = lambda offer, hits, files: Conversion(edits=[FileEdit(path=path, content="x")], summary="")
    out = run(sqlite_app, target=2, accept="all", converter=bad)
    assert not out["conversions"][0]["applied"]
    assert not (sqlite_app / path).exists() and not (sqlite_app.parent / "evil.py").exists()


def test_rejected_edit_writes_nothing(sqlite_app):
    before = (sqlite_app / "app.py").read_text()
    mixed = lambda offer, hits, files: Conversion(
        edits=[FileEdit(path="app.py", content="changed"), FileEdit(path="../evil.py", content="x")],
        summary="",
    )
    run(sqlite_app, target=2, accept="all", converter=mixed)
    assert (sqlite_app / "app.py").read_text() == before


def test_llm_error_skips_only_that_offer(sqlite_app):
    def flaky(offer, hits, files):
        if offer["id"] == "health":
            raise RuntimeError("rate limited")
        return fake_converter(offer, hits, files)

    out = run(sqlite_app, target=2, accept="all", converter=flaky)
    assert [(c["offer"], c["applied"]) for c in out["conversions"]] == [("sqlite", True), ("health", False)]
    assert "rate limited" in out["conversions"][1]["summary"]


# ---------- 배포 명세 ----------


def spec_of(path, target, **kw):
    return run(path, target=target, accept=[], **kw)["result"]["spec"]


@pytest.mark.parametrize(
    "path, target, app",
    [
        (
            APP_FIXTURES / "node-pg-visits",
            2,
            {
                "install": "npm ci",
                "start": "npm start",
                "port_env": "PORT",
                "port": 3000,
                "health": "/health",
            },
        ),
        (
            APP_FIXTURES / "fastapi-mysql-notes",
            2,
            {"start": "uvicorn main:app --host 0.0.0.0 --port $PORT", "port_env": "PORT"},
        ),
        (
            APP_FIXTURES / "flask-sqlite-todo",
            4,
            {"start": "gunicorn -b 0.0.0.0:$PORT app:app", "health": None},
        ),
        (
            APP_FIXTURES / "vite-react-landing",
            5,
            {"build": "npm run build", "static_output": "dist", "start": None, "port": None},
        ),
        (
            NEW_FIXTURES / "go-gin-ws",
            4,
            {
                "version": "1.22",
                "build": "go build -o app .",
                "start": "./app",
                "port_env": "PORT",
                "port": 8080,
            },
        ),
        (NEW_FIXTURES / "node-pdf-puppeteer", 2, {"system_tools": ["chromium"]}),
        (
            NEW_FIXTURES / "java-spring-pg",
            2,
            {
                "version": "21",
                "install": None,
                "build": "mvn -q -DskipTests package",
                "start": "java -Dserver.port=$PORT -jar target/board-0.0.1-SNAPSHOT.jar",
                "port_env": "PORT",
                "health": "/actuator/health",
            },
        ),
    ],
)
def test_spec_recipe(path, target, app):
    spec = spec_of(path, target)
    assert spec["ready"] and spec["blockers"] == []
    assert {k: spec["app"][k] for k in app} == app


@pytest.mark.parametrize(
    "path, target, resources, env",
    [
        (
            APP_FIXTURES / "node-pg-visits",
            2,
            [("postgres", {"url": "DATABASE_URL"}, "postgresql")],
            [("SESSION_SECRET", "secret", True)],
        ),
        (APP_FIXTURES / "fastapi-mysql-notes", 2, [("mysql", {"url": "DATABASE_URL"}, "mysql+pymysql")], []),
        (
            APP_FIXTURES / "express-redis-counter",
            2,
            [("redis", {"url": "REDIS_URL"}, "redis")],
            [("API_TOKEN", "secret", True)],
        ),  # !X || ...는 기본값이 아니다
        (
            APP_FIXTURES / "flask-sqlite-todo",
            4,
            [],
            [("SECRET_KEY", "secret", True)],
        ),  # 기본값 "dev"는 개발용이라 비밀값은 넣어야 한다
        (
            NEW_FIXTURES / "java-spring-pg",
            2,  # 설정 파일에 박힌 접속 정보는 Spring 환경변수로 덮어쓴다
            [
                (
                    "postgres",
                    {
                        "url": "SPRING_DATASOURCE_URL",
                        "user": "SPRING_DATASOURCE_USERNAME",
                        "password": "SPRING_DATASOURCE_PASSWORD",
                    },
                    "jdbc:postgresql",
                )
            ],
            [("NOTICE", "config", False), ("ADMIN_TOKEN", "secret", True)],
        ),
    ],
)
def test_spec_resources_and_env(path, target, resources, env):
    spec = spec_of(path, target)
    assert [(r["kind"], r["env"], r["scheme"]) for r in spec["resources"]] == resources
    assert [(e["name"], e["kind"], e["required"]) for e in spec["env"]] == env


def test_spec_constraints_and_blockers():
    spec = spec_of(NEW_FIXTURES / "node-upload-session", 4)  # LLM이 없어 config 수정을 못 했다
    assert not spec["ready"]
    assert spec["blockers"] == ["127.0.0.1에만 바인딩해 밖에서 접속할 수 없습니다 (config 수정 필요)"]
    assert (spec["app"]["port_env"], spec["app"]["port"]) == (None, 3000)
    c = spec["constraints"]
    assert c["single_instance"] and c["persistent_paths"] == ["uploads/"]
    assert spec_of(APP_FIXTURES / "flask-sqlite-todo", 4)["constraints"]["persistent_paths"] == ["todo.db"]
    assert spec_of(NEW_FIXTURES / "go-gin-ws", 4)["constraints"]["long_connections"]


def test_spec_not_ready_when_big_fix_remains(sqlite_app):
    spec = run(sqlite_app, target=2, accept="all")["result"]["spec"]  # LLM이 없어 sqlite 수정을 못 했다
    assert not spec["ready"] and spec["blockers"] == ["적용하지 않은 수정이 남았습니다: sqlite"]


def test_spec_after_sqlite_to_postgres(sqlite_app):
    spec = run(sqlite_app, target=2, accept="all", converter=fake_converter)["result"]["spec"]
    assert spec["ready"]
    assert [(r["kind"], r["env"]) for r in spec["resources"]] == [("postgres", {"url": "DATABASE_URL"})]
    assert spec["constraints"]["persistent_paths"] == [] and not spec["constraints"]["single_instance"]
    assert spec["app"]["health"] == "/health"


def test_spec_faas_handler(sqlite_app):
    spec = run(sqlite_app, target=1, accept="all", converter=fake_converter)["result"]["spec"]
    assert spec["ready"] and spec["app"]["handler"] == "lambda_handler.handler"


def test_spec_schedules_come_from_scheduler_fix(tmp_path):
    app = copy_app(NEW_FIXTURES / "node-upload-session", tmp_path)

    def converter(offer, hits, files):
        if offer["id"] != "scheduler":
            return Conversion(edits=[], summary="")
        server = files["server.js"].replace(
            "cron.schedule('0 3 * * *', () => console.log('nightly cleanup'));",
            "app.post('/tasks/cleanup', (req, res) => res.json({ ok: true }));",
        )
        return Conversion(
            edits=[FileEdit(path="server.js", content=server)],
            summary="",
            schedules=[Schedule(path="/tasks/cleanup", cron="0 3 * * *")],
        )

    spec = run(app, target=1, accept="all", converter=converter)["result"]["spec"]
    assert spec["schedules"] == [{"path": "/tasks/cleanup", "cron": "0 3 * * *", "auth_env": "CRON_TOKEN"}]


def test_spec_gradle(tmp_path):
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'shop'\n")
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'org.springframework.boot' version '3.3.5' }\n"
        "version = '1.2.0'\n"
        "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }\n"
        "dependencies { implementation 'org.springframework.boot:spring-boot-starter-web' }\n"
    )
    app = spec_of(tmp_path, 4)["app"]
    assert (app["version"], app["build"], app["start"]) == (
        "17",
        "gradle bootJar",
        "java -Dserver.port=$PORT -jar build/libs/shop-1.2.0.jar",
    )


# ---------- 프레임워크 · 추천 · 환경변수 안내 ----------


@pytest.mark.parametrize(
    "path, frameworks",
    [
        (APP_FIXTURES / "node-pg-visits", {"node": "Express"}),
        (APP_FIXTURES / "vite-react-landing", {"node": "React (Vite)"}),
        (APP_FIXTURES / "flask-sqlite-todo", {"python": "Flask"}),
        (APP_FIXTURES / "fastapi-mysql-notes", {"python": "FastAPI"}),
        (NEW_FIXTURES / "go-gin-ws", {"go": "Gin"}),
        (NEW_FIXTURES / "java-spring-pg", {"java": "Spring Boot"}),
    ],
)
def test_frameworks(path, frameworks):
    assert scan(path)["frameworks"] == frameworks


def test_java_state_signals(tmp_path):
    app = copy_app(NEW_FIXTURES / "java-spring-pg", tmp_path)
    (app / "src/main/java/com/example/board/Jobs.java").write_text(
        "class Jobs {\n"
        "    static final AtomicInteger visits = new AtomicInteger();\n"
        "    private final Map<String, String> sessions = new java.util.HashMap<>();\n"
        "    private static final int MAX = 10;\n"
        '    @Scheduled(cron = "0 0 3 * * *")\n'
        "    void cleanup() { visits.set(0); sessions.clear(); }\n"
        '    List<String> names() { final List<String> out = new ArrayList<>(); out.add("a"); return out; }\n'
        '    void save(byte[] b) throws Exception { Files.write(Path.of("uploads/a.png"), b); }\n'
        "}\n"
    )
    s = scan(app)["signals"]
    assert [h["text"].split(" = ")[0] for h in s["memory_state"]["hits"]] == [
        "static final AtomicInteger visits",
        "private final Map<String, String> sessions",
    ]  # 상수와 지역 변수는 상태가 아니다
    assert s["scheduler"]["value"] == "yes" and s["file_write"]["value"] == "yes"
    assert apply_rules(s)["candidates"] == [4]


def test_java_config_file_secret_and_actuator():
    s = scan(NEW_FIXTURES / "java-spring-pg")["signals"]
    assert [h["text"] for h in s["hardcoded_config"]["hits"]] == [
        "spring.datasource.password=board"
    ]  # ${ADMIN_TOKEN}은 괜찮다
    assert s["no_health"]["value"] == "no"  # actuator가 /actuator/health를 만든다


@pytest.mark.parametrize(
    "path, rec, why",
    [
        (APP_FIXTURES / "vite-react-landing", 5, []),
        (APP_FIXTURES / "node-pg-visits", 2, []),
        (NEW_FIXTURES / "java-spring-pg", 2, []),
        (
            APP_FIXTURES / "flask-sqlite-todo",
            2,
            ["sqlite 수정이 필요합니다", "코드를 크게 고치지 않으려면 4. 가상 서버 (VM)"],
        ),
        (
            NEW_FIXTURES / "go-gin-ws",
            2,
            [
                "요청과 별개로 계속 도는 작업(WebSocket 같은 긴 연결, 백그라운드 작업)이 있어 연결 시간 제한과 최소 인스턴스 수를 확인해야 합니다"
            ],
        ),
        (
            NEW_FIXTURES / "py-win-report",
            4,
            ["Windows VM이 필요합니다"],
        ),  # 리눅스로 옮기는 수정은 위험해서 추천하지 않는다
        (
            NEW_FIXTURES / "node-upload-session",
            2,
            [
                "storage, redis 수정이 필요합니다",
                "코드를 크게 고치지 않으려면 4. 가상 서버 (VM)",
                "프로세스 안의 주기 작업이 있어 인스턴스를 1대로 고정하고 0대로 줄지 않게 해야 합니다",
            ],
        ),
        (NEW_FIXTURES / "py-mac-menubar", None, []),
    ],
)
def test_recommend(path, rec, why):
    r = run(path, ask=lambda p: None)["result"]["recommended"]
    assert (r and r["type"]) == rec
    assert r is None or r["why"][1:] == why


def test_choose_shows_stack_recommendation_and_services(sqlite_app):
    seen = []
    run(sqlite_app, ask=lambda p: seen.append(p) or None)
    p = seen[0]
    assert (p["languages"], p["frameworks"], p["recommended"]["type"]) == (["python"], {"python": "Flask"}, 2)
    opts = {o["type"]: o for o in p["options"]}
    assert opts[2]["services"] == {"aws": "ECS Fargate", "gcp": "Cloud Run", "azure": "Container Apps"}
    assert opts[4]["services"]["onprem"] == "사내 서버 (Docker)" and opts[4]["pros"] and opts[4]["cons"]


def test_env_guide_rules(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\ngunicorn\n")
    (tmp_path / "app.py").write_text(
        "import os\nfrom flask import Flask\napp = Flask(__name__)\n"
        'KEY = os.environ["OPENAI_API_KEY"]\n'
        'AWS = os.getenv("AWS_ACCESS_KEY_ID")\n'
        'CID = os.getenv("OAUTH_CLIENT_SECRET")\n'
        'JWT = os.environ["JWT_SECRET"]\n'
        'LEVEL = os.environ.get("LOG_LEVEL", "warning")\n'
        'MODE = os.environ["NODE_ENV"]\n'
        'PEM = os.environ["JWT_PRIVATE_KEY_PATH"]\n'
    )
    env = {e["name"]: e for e in spec_of(tmp_path, 4)["env"]}
    assert (env["OPENAI_API_KEY"]["description"], env["OPENAI_API_KEY"]["generate"]) == (
        "OpenAI에서 받은 값",
        False,
    )
    assert env["AWS_ACCESS_KEY_ID"]["how"].startswith("클라우드에서는 IAM 역할")
    assert (
        env["OAUTH_CLIENT_SECRET"]["generate"] is False
    )  # 로그인 제공자에서 받는 값이라 무작위로 만들면 안 된다
    assert env["JWT_SECRET"]["generate"] is True and env["JWT_SECRET"]["required"]
    assert (env["LOG_LEVEL"]["required"], env["LOG_LEVEL"]["default"], env["LOG_LEVEL"]["generate"]) == (
        False,
        "warning",
        None,
    )
    assert (env["NODE_ENV"]["required"], env["NODE_ENV"]["example"]) == (True, "production")
    jwt = env["JWT_PRIVATE_KEY_PATH"]
    assert (jwt["kind"], jwt["generate"]) == ("config", None)  # 비밀값 파일의 경로라 무작위로 만들면 안 된다


def test_secret_default_is_not_exposed(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"start": "node index.js"}, "dependencies": {"express": "4"}}'
    )
    (tmp_path / "index.js").write_text(
        "const key = process.env.SESSION_SECRET || 'hunter2-dev';\nconst port = process.env.PORT || 3000;\n"
        "const name = process.env.SITE_NAME ?? 'demo';\napp.listen(port);\n"
    )
    env = {e["name"]: e for e in spec_of(tmp_path, 4)["env"]}
    assert env["SESSION_SECRET"]["default"] is None and env["SESSION_SECRET"]["required"]
    assert (env["SITE_NAME"]["default"], env["SITE_NAME"]["required"]) == ("demo", False)


def test_spec_steps():
    steps = spec_of(NEW_FIXTURES / "java-spring-pg", 2)["steps"]
    assert steps == [
        "PostgreSQL 만들기 → SPRING_DATASOURCE_URL, SPRING_DATASOURCE_USERNAME, SPRING_DATASOURCE_PASSWORD에 연결 (JDBC 형식, 계정은 따로)",
        "환경변수 입력: ADMIN_TOKEN (자동 생성 가능)",
        "빌드: mvn -q -DskipTests package (java 21)",
        "실행: java -Dserver.port=$PORT -jar target/board-0.0.1-SNAPSHOT.jar (PORT 환경변수로 포트 전달)",
        "헬스체크: GET /actuator/health",
    ]
    steps = spec_of(APP_FIXTURES / "flask-sqlite-todo", 4)["steps"]
    assert "영구 디스크 연결: todo.db" in steps and any(s.startswith("인스턴스를 1대로 고정") for s in steps)
    assert spec_of(APP_FIXTURES / "vite-react-landing", 5)["steps"][-1] == "dist 폴더를 정적 호스팅에 올리기"


def test_env_example_and_generate_secret():
    text = env_example(DeploySpec(**spec_of(NEW_FIXTURES / "java-spring-pg", 2)))
    assert (
        "# PostgreSQL: 배포할 때 백엔드가 채웁니다 (jdbc:postgresql 형식)\nSPRING_DATASOURCE_URL=\n" in text
    )
    assert "NOTICE=Welcome\n" in text and "ADMIN_TOKEN=\n" in text and "PORT=\n" in text
    assert len(generate_secret()) >= 32 and generate_secret() != generate_secret()


def test_report_shows_steps_and_env():
    text = report(run(NEW_FIXTURES / "java-spring-pg", target=2, accept=[]))
    assert text.startswith("언어: java (Spring Boot)\n추천: 2. 서버리스 컨테이너")
    assert "[배포 절차]" in text and "ADMIN_TOKEN (자동 생성 가능)" in text


# ---------- 실제 저장소(MaechuriAIServer)에서 나온 경우들 ----------


@pytest.fixture
def fastapi_factory_app(tmp_path):
    """Windows에서 pip freeze로 만든 UTF-16 requirements, create_app() 팩토리, 나눠 받는 DB 접속 정보, 낡은 .env.example"""
    (tmp_path / "requirements.txt").write_text(
        "aiosqlite==0.22.1\nasyncpg==0.31.0\nfastapi==0.127.0\nredis==7.1.0\n"
        "SQLAlchemy==2.0.45\nuvicorn==0.40.0\n",
        encoding="utf-16",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\nimport uvicorn\n\ndef create_app():\n    app = FastAPI()\n    return app\n\napp = create_app()\n\n"
        'if __name__ == "__main__":\n    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)\n'
    )
    (tmp_path / "app" / "config.py").write_text(
        'import os\nDB_HOST = os.getenv("DB_HOST", "")\nDB_PORT = int(os.getenv("DB_PORT", ""))\nDB_NAME = os.getenv("DB_NAME", "")\n'
        'DB_USER = os.getenv("DB_USER", "")\nDB_PASSWORD = os.getenv("DB_PASSWORD", "")\nREDIS_HOST = os.getenv("REDIS_HOST", "")\n'
        'REDIS_PORT = int(os.getenv("REDIS_PORT", ""))\nGEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")\n'
        'GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")\nTIMEOUT = os.getenv("TIMEOUT", "")\n'
        'URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"\n'
    )
    (tmp_path / ".env.example").write_text("POSTGRES_USER=app\nPOSTGRES_HOST=localhost\nGEMINI_API_KEY=\n")
    (tmp_path / "app" / "test").mkdir()
    (tmp_path / "app" / "test" / "test_dump.py").write_text(
        'open("out.json", "w").write("{}")\nwhile True:\n    pass\n'
    )
    return tmp_path


def test_utf16_requirements_and_factory_app(fastapi_factory_app):
    r = scan(fastapi_factory_app)
    assert r["frameworks"] == {"python": "FastAPI"} and r["signals"]["has_server"]["value"] == "yes"
    s = r["signals"]
    assert s["sqlite"]["value"] == "unknown"  # aiosqlite는 깔려 있지만 코드에 sqlite가 없다 (pip freeze)
    assert (
        s["file_write"]["value"] == "no" and s["long_running"]["value"] == "no"
    )  # 테스트 코드는 근거로 쓰지 않는다
    assert [
        h["evidence"] for h in s["hardcoded_config"]["hits"]
    ] == []  # f-string으로 조립한 주소는 박힌 값이 아니다
    spec = spec_of(fastapi_factory_app, 4)
    assert spec["app"]["start"] == "uvicorn app.main:app --host 0.0.0.0 --port $PORT"


def test_split_connection_env_and_stale_env_example(fastapi_factory_app):
    spec = spec_of(fastapi_factory_app, 4)
    assert spec["ready"], spec["blockers"]
    assert [(r["kind"], r["env"], r["scheme"]) for r in spec["resources"]] == [
        (
            "postgres",
            {
                "host": "DB_HOST",
                "port": "DB_PORT",
                "user": "DB_USER",
                "password": "DB_PASSWORD",
                "database": "DB_NAME",
            },
            None,
        ),
        ("redis", {"host": "REDIS_HOST", "port": "REDIS_PORT"}, None),
    ]
    env = {e["name"]: e for e in spec["env"]}
    assert env["TIMEOUT"]["required"]  # os.getenv("TIMEOUT", "")의 빈 문자열은 기본값이 아니다
    assert (env["GEMINI_MODEL"]["description"], env["GEMINI_MODEL"]["default"]) == (
        "Google Gemini 설정값",
        "gemini-2.5-flash",
    )
    assert env["GEMINI_API_KEY"]["generate"] is False and env["GEMINI_API_KEY"]["required"]
    stale = env["POSTGRES_USER"]
    assert not stale["required"] and stale["how"].startswith("코드에서 읽는 곳을 찾지 못했습니다")


def test_no_static_recommendation_without_static_evidence(tmp_path):
    (tmp_path / "requirements.txt").write_text("numpy\n")
    (tmp_path / "job.py").write_text("print(1)\n")
    r = run(tmp_path, ask=lambda p: None)["result"]
    assert (
        5 in r["candidates"] and r["recommended"]["type"] != 5
    )  # 서버 근거를 못 찾았다고 정적 사이트로 추천하지 않는다


def test_spring_split_config_r2dbc_and_minio(tmp_path):
    """MaechuriMainServer 모양: spring.config.import로 나눈 설정 파일, R2DBC, MinIO(S3 호환)"""
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'main-server'\n")
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'org.springframework.boot' version '4.0.0' }\n"
        "dependencies {\n  implementation 'org.springframework.boot:spring-boot-starter-webflux'\n"
        "  implementation 'org.springframework.boot:spring-boot-starter-data-r2dbc'\n"
        "  implementation 'org.springframework.boot:spring-boot-starter-actuator'\n"
        "  runtimeOnly 'org.postgresql:postgresql'\n  implementation 'org.postgresql:r2dbc-postgresql'\n"
        "  implementation 'software.amazon.awssdk:s3:2.20.26'\n}\n"
    )
    res = tmp_path / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "application.yml").write_text(
        "server:\n  port: ${PORT:8080}\nspring:\n  config:\n    import:\n      - classpath:db.yml\n"
    )
    (res / "db.yml").write_text(
        "spring:\n  r2dbc:\n    url: ${DATABASE_URL}\n    username: ${DATABASE_USER}\n    password: ${DATABASE_PW}\n"
    )
    (res / "minio.yml").write_text(
        "minio:\n  access-key: ${MINIO_ACCESS_KEY}\n  secret-key: ${MINIO_SECRET_KEY}\n"
        "  endpoint: ${MINIO_ENDPOINT:http://localhost:9000}\n  bucket-name: ${MINIO_BUCKET_NAME:bucket}\n"
        "  region: ${MINIO_REGION:us-east-1}\n"
    )
    (res / "ai.yml").write_text(
        "leonardo:\n  api-key: ${LEONARDO_API_KEY}\njwt:\n  secret: hardcoded-secret-value\n"
    )
    kt = tmp_path / "src" / "main" / "kotlin"
    kt.mkdir(parents=True)
    (kt / "App.kt").write_text("@SpringBootApplication\nclass App\n")

    s = scan(tmp_path)["signals"]
    assert [h["evidence"] for h in s["hardcoded_config"]["hits"]] == [
        "src/main/resources/ai.yml:4"
    ]  # 나눈 설정 파일도 본다
    spec = spec_of(tmp_path, 2)
    assert [(r["kind"], r["env"], r["scheme"]) for r in spec["resources"]] == [
        (
            "postgres",
            {"url": "DATABASE_URL", "user": "DATABASE_USER", "password": "DATABASE_PW"},
            "r2dbc:postgresql",
        ),
        (
            "s3",
            {
                "bucket": "MINIO_BUCKET_NAME",
                "endpoint": "MINIO_ENDPOINT",
                "region": "MINIO_REGION",
                "access_key": "MINIO_ACCESS_KEY",
                "secret_key": "MINIO_SECRET_KEY",
            },
            None,
        ),
    ]
    assert [e["name"] for e in spec["env"]] == [
        "LEONARDO_API_KEY"
    ]  # 자원 접속 정보와 PORT는 사용자 입력에서 뺀다
    assert spec["ready"] and spec["app"]["health"] == "/actuator/health"


# ---------- AI 검사관: 가짜 검사관을 넣는다 ----------


@pytest.fixture
def cache_app(tmp_path):
    """패턴이 확인 필요(check)로 잡는 것들: 전역 dict, 파일 쓰기, 코드에 박힌 IP"""
    (tmp_path / "requirements.txt").write_text("flask\ngunicorn\n")
    (tmp_path / "app.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "CACHE = {}\n"
        'UPSTREAM = "10.0.0.5"\n'
        "\n"
        '@app.get("/")\n'
        "def index():\n"
        '    CACHE["hits"] = CACHE.get("hits", 0) + 1\n'
        '    open("last.json", "w").write("{}")\n'
        '    return "ok"\n'
    )
    return tmp_path


def ev(evidence, text):
    return Evidence(evidence=evidence, text=text)


FILE_WRITE = ev("app.py:9", 'open("last.json", "w").write("{}")')
STACK = llm.StackReport(
    language="python",
    framework="Flask",
    install="pip install -r requirements.txt",
    start="gunicorn -b 0.0.0.0:$PORT app:app",
    port_env="PORT",
    reason="Flask 앱",
    evidence=[ev("app.py:2", "app = Flask(__name__)")],
)


def fake_inspector(answers=None, calls=None):
    """answers: {검사관: 보고 또는 예외}. 주지 않은 위험 신호 검사관은 '위험 아님·근거 없음'으로, 명세 검사관은 빈 보고로 답한다"""
    answers = answers or {}

    def inspect(name, hints, files):
        if calls is not None:
            calls.append(name)
        if name in answers:
            if isinstance(answers[name], Exception):
                raise answers[name]
            return answers[name]
        if name == "has_server":
            return SignalReport(
                value="yes", reason="Flask 서버", evidence=[ev("app.py:2", "app = Flask(__name__)")]
            )
        if name in ai.SIGNAL_CRITERIA:
            return SignalReport(value="no", reason="", evidence=[])
        return {
            "stack": STACK,
            "resources": llm.ResourcesReport(items=[]),
            "env": llm.EnvReport(items=[]),
            "risks": llm.RisksReport(items=[]),
        }[name]

    return inspect


def test_check_signals_are_marked_unsure(cache_app):
    s = scan(cache_app)["signals"]
    assert {n for n, v in s.items() if v["value"] == "yes" and not v["sure"]} == {
        "memory_state",
        "file_write",
        "hardcoded_config",
    }
    assert apply_rules(s)["candidates"] == [4]  # LLM이 없으면 패턴 판정 (확인 필요도 위험으로 본다)


def test_every_inspector_runs_once_and_ai_decides(cache_app):
    calls = []
    inspect = fake_inspector(
        {
            "file_write": SignalReport(value="yes", reason="결과 파일을 남깁니다", evidence=[FILE_WRITE]),
            "hardcoded_config": SignalReport(
                value="no", reason="예시 주소입니다", evidence=[ev("app.py:4", 'UPSTREAM = "10.0.0.5"')]
            ),
        },
        calls,
    )
    asked = []
    out = run(
        cache_app,
        inspect=inspect,
        ask=lambda p: asked.append(p) or ("all" if p["kind"] == "consent" else None),
    )
    assert sorted(calls) == sorted(ai.INSPECTORS)  # 찾는 것마다 검사관이 한 번씩
    s = out["signals"]
    assert (s["file_write"]["value"], s["file_write"]["by"], s["file_write"]["hits"]) == (
        "yes",
        "ai",
        [{"evidence": "app.py:9", "text": 'open("last.json", "w").write("{}")'}],
    )
    assert (s["hardcoded_config"]["value"], s["hardcoded_config"]["by"]) == (
        "no",
        "ai",
    )  # 치명적이지 않으면 AI 판정을 바로 쓴다
    # AI는 메모리 상태를 근거 없이 '아니다'라고 했지만, 패턴 검색이 찾았으니 그 줄로 동의를 받는다
    assert [p["kind"] for p in asked] == ["consent", "choose"]
    assert [(it["signal"], it["hits"][0]["evidence"]) for it in asked[0]["items"]] == [
        ("memory_state", "app.py:3")
    ]
    assert (s["memory_state"]["value"], s["memory_state"]["by"]) == ("no", "ai+user")
    assert out["result"]["candidates"] == [4]  # 파일 쓰기는 AI가 위험으로 봤다


@pytest.mark.parametrize(
    "answer, value, by, candidates",
    [
        (["file_write"], "no", "ai+user", [1, 2, 3, 4]),
        ([], "yes", "user", [4]),
    ],
)
def test_fatal_release_needs_consent(cache_app, answer, value, by, candidates):
    inspect = fake_inspector(
        {"file_write": SignalReport(value="no", reason="캐시라 잃어도 됩니다", evidence=[FILE_WRITE])}
    )
    out = run(
        cache_app,
        inspect=inspect,
        ask=lambda p: answer + ["memory_state"] if p["kind"] == "consent" else None,
    )
    s = out["signals"]["file_write"]
    assert (s["value"], s["by"]) == (value, by)
    assert out["result"]["candidates"] == candidates


def test_consent_cannot_be_given_in_advance(cache_app):
    seen = []
    run(
        cache_app,
        target=4,
        accept="all",
        inspect=fake_inspector(),
        ask=lambda p: seen.append(p["kind"]) or [],
    )
    assert seen == ["consent"]  # --yes로 수정을 전부 승인해도 치명적 위험을 푸는 것은 따로 묻는다


def test_risk_without_evidence(cache_app):
    made_up = ev("app.py:2", "torch.cuda.is_available()")  # 그런 줄이 없다
    inspect = fake_inspector(
        {
            "gpu": SignalReport(value="yes", reason="cuda", evidence=[made_up]),
            "sqlite": SignalReport(value="yes", reason="sqlite", evidence=[made_up]),
        }
    )
    out = run(cache_app, inspect=inspect, ask=lambda p: "all" if p["kind"] == "consent" else None)
    assert out["signals"]["gpu"]["value"] == "unknown"  # 근거 없는 위험: 판정을 미룬다
    assert out["signals"]["sqlite"]["value"] == "yes"  # 치명적인 것은 위험으로 둔다
    assert {r["inspector"] for r in out["rejected"]} == {"gpu", "sqlite"}


def test_inspector_failure_falls_back_to_pattern(cache_app):
    inspect = fake_inspector({"file_write": RuntimeError("rate limited")})
    out = run(cache_app, inspect=inspect, ask=lambda p: "all" if p["kind"] == "consent" else None)
    s = out["signals"]["file_write"]
    assert (s["value"], s["by"]) == ("yes", "rule")
    assert out["llm_errors"] == [
        "file_write: rate limited"
    ]  # 동의 뒤에 다시 돌 때 실패한 검사관을 또 부르지 않는다


def test_only_related_inspectors_rerun_after_fix(cache_app):
    calls, asked = [], []

    def converter(offer, hits, files):  # health: 위쪽에 끼워 넣어 근거 줄 번호가 밀린다
        app = files["app.py"].replace(
            "app = Flask(__name__)\n",
            'app = Flask(__name__)\n\n@app.get("/health")\ndef health():\n    return "ok"\n',
        )
        return Conversion(edits=[FileEdit(path="app.py", content=app)], summary="/health를 추가했습니다.")

    healthy = STACK.model_copy(update={"health": "/health"})
    inspect = fake_inspector({"stack": STACK}, calls)

    def ask(p):
        asked.append(p["kind"])
        return "all"

    def inspect_then_healthy(name, hints, files):
        if name == "stack" and len(calls) >= len(ai.INSPECTORS):  # 고친 뒤 다시 볼 때는 /health가 있다
            calls.append(name)
            return healthy
        return inspect(name, hints, files)

    out = run(cache_app, target=4, accept="all", inspect=inspect_then_healthy, converter=converter, ask=ask)
    assert [c["offer"] for c in out["conversions"]] == ["health"]
    assert calls[len(ai.INSPECTORS) :] == ["stack"]  # health를 고친 뒤에는 실행 방법 검사관만 다시 본다
    assert asked == ["consent"]  # 줄이 밀려도 같은 코드는 다시 묻지 않는다
    assert out["result"]["spec"]["app"]["health"] == "/health"


def test_spec_comes_from_inspectors(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\npsycopg[binary]\n")
    (tmp_path / "main.py").write_text(
        'import os\nfrom fastapi import FastAPI\n\nDB = os.environ["DATABASE_URL"]\n'
        'KEY = os.environ["SECRET_KEY"]\napp = FastAPI()\n'
    )
    stack = llm.StackReport(
        language="python",
        framework="FastAPI",
        install="pip install -r requirements.txt",
        start="uvicorn main:app --host 0.0.0.0 --port $PORT",
        port_env="PORT",
        health=None,
        reason="FastAPI 앱",
        evidence=[ev("main.py:6", "app = FastAPI()")],
    )
    resources = llm.ResourcesReport(
        items=[
            llm.ResourceItem(
                kind="postgres",
                scheme="postgresql",
                evidence=[ev("main.py:4", 'DB = os.environ["DATABASE_URL"]')],
                env=[
                    llm.EnvRole(role="url", name="DATABASE_URL"),
                    llm.EnvRole(role="password", name="MADE_UP_PW"),
                ],
            )
        ]
    )
    env = llm.EnvReport(
        items=[
            llm.EnvItem(
                name="SECRET_KEY",
                kind="secret",
                required=True,
                default="dev",
                description="서명 키",
                how="무작위 값",
                generate=True,
                evidence=[ev("main.py:5", 'KEY = os.environ["SECRET_KEY"]')],
            ),
            llm.EnvItem(
                name="GHOST_VAR",
                kind="config",
                required=True,
                description="?",
                how="?",
                generate=False,
                evidence=[],
            ),
        ]
    )
    server = SignalReport(value="yes", reason="FastAPI 서버", evidence=[ev("main.py:6", "app = FastAPI()")])
    answers = {"has_server": server, "stack": stack, "resources": resources, "env": env}
    out = run(tmp_path, target=2, inspect=fake_inspector(answers), ask=lambda p: None)
    spec = out["result"]["spec"]
    assert (
        spec["app"]["start"] == "uvicorn main:app --host 0.0.0.0 --port $PORT"
        and spec["app"]["framework"] == "FastAPI"
    )
    assert [(r["kind"], r["env"]) for r in spec["resources"]] == [
        ("postgres", {"url": "DATABASE_URL"})
    ]  # 저장소에 없는 이름은 버린다
    assert [(e["name"], e["default"], e["generate"]) for e in spec["env"]] == [
        ("SECRET_KEY", None, True)
    ]  # 비밀값 기본값은 내보내지 않는다
    assert sorted(r["text"] for r in out["rejected"] if r["evidence"] == "-") == ["GHOST_VAR", "MADE_UP_PW"]
    assert (
        spec["ready"] and out["signals"]["no_health"]["value"] == "yes"
    )  # 헬스체크 없음은 실행 방법 보고에서 나온다


def test_risks_keep_only_checked_evidence(cache_app):
    risks = llm.RisksReport(
        items=[
            llm.Risk(title="결과 파일이 쌓임", reason="디스크가 찹니다", evidence=[FILE_WRITE]),
            llm.Risk(title="지어낸 위험", reason="", evidence=[ev("app.py:1", "import torch")]),
        ]
    )
    out = run(
        cache_app,
        inspect=fake_inspector({"risks": risks}),
        ask=lambda p: "all" if p["kind"] == "consent" else None,
    )
    assert [r["title"] for r in out["reports"]["risks"]["items"]] == ["결과 파일이 쌓임"]


def test_repo_reader_reads_only_inside_repo(cache_app):
    """OpenAI 모델에 붙이는 읽기 도구: 모델이 부른 도구를 실행해 결과를 돌려주고, 저장소 밖은 읽지 못한다"""
    from langchain_core.messages import AIMessage, ToolMessage

    class FakeChat:  # 도구를 두 번 부르고 끝낸 뒤 결론을 낸다
        def __init__(self):
            self.replies = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "grep", "args": {"pattern": r"open\("}, "id": "a"},
                        {"name": "read_file", "args": {"path": "../../etc/passwd"}, "id": "b"},
                    ],
                ),
                AIMessage(content="다 봤다"),
            ]

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return self.replies.pop(0)

        def with_structured_output(self, schema, method=None):
            chat = self

            class Final:
                def invoke(self, messages):
                    chat.seen = messages
                    return SignalReport(value="no", reason="캐시", evidence=[FILE_WRITE])

            return Final()

    reader = llm.RepoReader(FakeChat(), str(cache_app))
    got = ai.make_inspector(reader)("file_write", "- app.py:9: open(...)", {"app.py": "SECRET_SOURCE"})
    assert got.value == "no" and reader.calls == [{"kind": "file_write", "turns": 2, "ok": True}]
    tool_out = {m.tool_call_id: m.content for m in reader.model.seen if isinstance(m, ToolMessage)}
    assert tool_out["a"] == 'app.py:9: open("last.json", "w").write("{}")'
    assert tool_out["b"].startswith("읽을 수 없는 경로")  # 저장소 밖은 막는다
    assert all(
        "SECRET_SOURCE" not in str(m.content) for m in reader.model.seen
    )  # 파일 내용 대신 직접 읽게 한다


def test_no_openai_key_means_patterns_only(monkeypatch, cache_app):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert llm.load_model(str(cache_app)) is None


# ---------- 팀 계약: analyze(src_dir) -> AnalysisResult ----------

from app.analysis.dockerfile import render
from app.analysis.spec import App
from app.analyzer import analyze, analyze_with


def snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_contract_sqlite_app_goes_local_without_touching_code(no_key, sqlite_app):
    before = snapshot(sqlite_app)
    r = analyze(str(sqlite_app))
    assert (r.target, r.language, r.port, r.entrypoint) == (
        "local",
        "python",
        8080,
        "gunicorn -b 0.0.0.0:$PORT app:app",
    )
    assert r.dockerfile.splitlines()[:3] == ["FROM python:3.12-slim", "WORKDIR /app", "ENV PORT=8080"]
    assert r.dockerfile.rstrip().endswith("CMD gunicorn -b 0.0.0.0:$PORT app:app")
    assert r.notes[0].startswith("서버리스 컨테이너로는 갈 수 없어 local(Docker)에 배포합니다: SQLite 파일이")
    assert "수정 제안: 헬스체크 엔드포인트(GET /health)를 추가할까요?" in r.notes
    assert r.notes[-1].startswith("OPENAI_API_KEY가 없어")
    assert snapshot(sqlite_app) == before  # 파이프라인에서는 코드를 고치지 않는다


def test_contract_postgres_app_goes_cloudrun(no_key):
    r = analyze(str(APP_FIXTURES / "node-pg-visits"))
    assert (r.target, r.language, r.framework) == ("cloudrun", "node", "Express")
    assert (
        "FROM node:20-slim" in r.dockerfile
        and "RUN npm ci" in r.dockerfile
        and r.dockerfile.rstrip().endswith("CMD npm start")
    )
    assert "외부 저장소가 필요합니다: postgres (DATABASE_URL). 배포 전에 만들어 연결해야 합니다." in r.notes
    assert "넣어야 하는 환경변수: SESSION_SECRET (무작위 값 가능)" in r.notes


def test_contract_static_site_is_served_by_nginx_on_port(no_key):
    r = analyze(str(APP_FIXTURES / "vite-react-landing"))
    assert r.target == "cloudrun"
    assert "FROM node:20-slim AS build" in r.dockerfile and "RUN npm run build" in r.dockerfile
    assert "COPY --from=build /app/dist/ /usr/share/nginx/html/" in r.dockerfile
    assert "listen ${PORT};" in r.dockerfile  # nginx가 시작할 때 PORT를 채운다


def test_contract_keeps_repo_dockerfile(no_key, tmp_path):
    app = copy_app(APP_FIXTURES / "node-pg-visits", tmp_path)
    (app / "Dockerfile").write_text('FROM node:22\nCMD ["node", "app.js"]\n')
    r = analyze(str(app))
    assert (
        r.dockerfile == 'FROM node:22\nCMD ["node", "app.js"]\n'
        and "저장소에 있는 Dockerfile을 그대로 씁니다." in r.notes
    )


def test_contract_cannot_ask_so_fatal_risk_stays(cache_app):
    inspect = fake_inspector(
        {"file_write": SignalReport(value="no", reason="캐시라 잃어도 됩니다", evidence=[FILE_WRITE])}
    )
    r = analyze_with(str(cache_app), inspect)
    assert r.target == "local"  # 동의를 받지 못했으니 파일 쓰기는 위험으로 둔다
    assert any(
        n.startswith("동의를 받지 못해 위험으로 두었습니다: 로컬 파일 쓰기") and "캐시라 잃어도 됩니다" in n
        for n in r.notes
    )


def test_contract_nothing_deployable(no_key):
    with pytest.raises(ValueError, match="local·cloudrun 어디에도 배포할 수 없습니다"):
        analyze(str(NEW_FIXTURES / "py-mac-menubar"))


def test_render_dockerfile_for_java_and_os_tools():
    java = render(
        App(
            language="java",
            version="21",
            build="./gradlew bootJar",
            start="java -Dserver.port=$PORT -jar build/libs/app.jar",
        )
    )
    assert "FROM eclipse-temurin:21-jdk" in java and "RUN chmod +x gradlew mvnw 2>/dev/null || true" in java
    node = render(
        App(language="node", install="npm ci", start="npm start", system_tools=["chromium", "unknown-tool"])
    )
    assert "apt-get install -y --no-install-recommends chromium &&" in node
    with pytest.raises(ValueError):
        render(App(language="elixir", start="mix run"))
