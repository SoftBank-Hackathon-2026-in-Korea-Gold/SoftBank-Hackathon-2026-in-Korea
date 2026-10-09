"""analyzer 테스트. AI 검사관과 Dockerfile 작성자는 가짜를 넣는다.
실제 OpenAI는 맨 아래 test_live_openai 하나만 부르고, ANALYZER_LIVE=1일 때만 돈다."""

import os
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app import analyzer as an
from app.analyzer import (
    App,
    Evidence,
    SignalReport,
    _dockerfile,
    analyze,
    analyze_with,
    apply_rules,
    check_dockerfile,
    render_dockerfile,
)

# ---------- 정책: 신호 → 배포 유형 ----------


def values(**given):
    return {name: given.get(name, "no") for name in an.SIGNAL_CRITERIA}


@pytest.mark.parametrize(
    "given, candidates",
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
def test_rules(given, candidates):
    assert apply_rules(values(**given))[0] == candidates


# ---------- 가짜 검사관 ----------


def ev(evidence, text):
    return Evidence(evidence=evidence, text=text)


@pytest.fixture
def flask_app(tmp_path):
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


SERVER = SignalReport(value="yes", reason="Flask 서버", evidence=[ev("app.py:2", "app = Flask(__name__)")])
FILE_WRITE = ev("app.py:9", 'open("last.json", "w").write("{}")')
MADE_UP = ev("app.py:2", "torch.cuda.is_available()")  # 그런 줄이 없다
STACK = an.StackReport(
    language="python",
    framework="Flask",
    install="pip install -r requirements.txt",
    start="gunicorn -b 0.0.0.0:$PORT app:app",
    port_env="PORT",
    reason="Flask 앱",
    evidence=[ev("app.py:2", "app = Flask(__name__)")],
)


def fake_inspector(answers=None, calls=None):
    """answers: {검사관: 보고 또는 예외}. 주지 않으면 서버 코드는 Flask 서버, 실행 방법은 STACK,
    다른 위험 신호는 '위험 아님·근거 없음', 나머지 명세 검사관은 빈 보고로 답한다"""
    answers = {"has_server": SERVER, "stack": STACK, **(answers or {})}

    def inspect(name):
        if calls is not None:
            calls.append(name)
        if name in answers:
            if isinstance(answers[name], Exception):
                raise answers[name]
            return answers[name]
        if name in an.SIGNAL_CRITERIA:
            return SignalReport(value="no", reason="", evidence=[])
        return {
            "resources": an.ResourcesReport(items=[]),
            "env": an.EnvReport(items=[]),
            "risks": an.RisksReport(items=[]),
        }[name]

    return inspect


def keep_draft(app, port, draft):
    return an.DockerfileReport(dockerfile=draft, changes=[])


def run(root, answers=None, write=keep_draft, calls=None):
    return analyze_with(str(root), fake_inspector(answers, calls), write)


def snapshot(root):
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------- 팀 계약: analyze(src_dir) -> AnalysisResult ----------


def test_every_inspector_runs_once_and_code_is_untouched(flask_app):
    calls, before = [], snapshot(flask_app)
    r = run(flask_app, calls=calls)
    assert sorted(calls) == sorted(an.INSPECTORS)  # 찾는 것마다 검사관이 한 번씩
    assert (r.target, r.language, r.framework, r.port, r.entrypoint) == (
        "cloudrun",
        "python",
        "Flask",
        8080,
        "gunicorn -b 0.0.0.0:$PORT app:app",
    )
    assert r.notes[0] == "서버리스 컨테이너 유형으로 판단해 cloudrun에 배포합니다."
    assert r.dockerfile.splitlines()[:3] == ["FROM python:3.12-slim", "WORKDIR /app", "ENV PORT=8080"]
    assert r.dockerfile.rstrip().endswith("CMD gunicorn -b 0.0.0.0:$PORT app:app")
    assert "수정 제안: 헬스체크 엔드포인트(GET /health)를 추가할까요?" in r.notes
    assert snapshot(flask_app) == before  # 파이프라인에서는 코드를 고치지 않는다


def test_data_loss_risk_goes_local(flask_app):
    r = run(flask_app, {"file_write": SignalReport(value="yes", reason="결과 파일", evidence=[FILE_WRITE])})
    assert r.target == "local"
    assert r.notes[0].startswith("서버리스 컨테이너로는 갈 수 없어 local(Docker)에 배포합니다: 로컬 디스크에")
    assert "재시작해도 남아야 하는 데이터를 쓰는 곳: app.py:9" in r.notes


def test_fatal_risk_stays_without_consent(flask_app):
    r = run(
        flask_app,
        {
            "file_write": SignalReport(value="no", reason="캐시라 잃어도 됩니다", evidence=[FILE_WRITE]),
            "hardcoded_config": SignalReport(
                value="no", reason="예시 주소입니다", evidence=[ev("app.py:4", 'UPSTREAM = "10.0.0.5"')]
            ),
        },
    )
    assert r.target == "local"  # 동의를 받지 못했으니 파일 쓰기는 위험으로 둔다
    assert any(
        n.startswith("동의를 받지 못해 위험으로 두었습니다: 로컬 파일 쓰기") and "캐시라 잃어도 됩니다" in n
        for n in r.notes
    )
    assert not any("박힌" in n for n in r.notes)  # 치명적이지 않으면 AI 판정을 바로 쓴다


@pytest.mark.parametrize(
    "found, models",
    [
        ({}, ["caas", "paas", "faas", "iaas"]),
        ({"long_running": ev("app.py:1", "from flask import Flask")}, ["caas", "paas", "iaas"]),
        ({"system_packages": ev("app.py:1", "from flask import Flask")}, ["caas", "iaas"]),
        ({"file_write": FILE_WRITE}, ["iaas"]),
    ],
)
def test_service_models_the_code_can_run_on(flask_app, found, models):
    answers = {n: SignalReport(value="yes", reason="", evidence=[e]) for n, e in found.items()}
    assert run(flask_app, answers).service_models == models  # 추천 순서대로, deployer가 지원하는 것을 고른다


@pytest.mark.parametrize("name, target", [("gpu", "cloudrun"), ("sqlite", "local")])
def test_risk_without_checked_evidence(flask_app, name, target):
    r = run(flask_app, {name: SignalReport(value="yes", reason="?", evidence=[MADE_UP])})
    assert r.target == target  # 근거를 확인 못 한 위험: 판정을 미루되, 치명적인 것은 위험으로 둔다


@pytest.mark.parametrize("name, target", [("gpu", "cloudrun"), ("file_write", "local")])
def test_inspector_failure(flask_app, name, target):
    r = run(flask_app, {name: RuntimeError("rate limited")})
    assert r.target == target  # 판정하지 못한 치명적 위험은 위험으로 둔다
    assert r.notes[-1].endswith(f"{name}: rate limited")


def test_notes_from_spec_inspectors(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\npsycopg[binary]\n")
    (tmp_path / "main.py").write_text(
        'import os\nfrom fastapi import FastAPI\n\nDB = os.environ["DATABASE_URL"]\n'
        'KEY = os.environ["SECRET_KEY"]\napp = FastAPI()\n'
    )
    server = SignalReport(value="yes", reason="FastAPI 서버", evidence=[ev("main.py:6", "app = FastAPI()")])
    stack = STACK.model_copy(
        update={
            "framework": "FastAPI",
            "start": "uvicorn main:app --host 0.0.0.0 --port $PORT",
            "health": "/",
        }
    )
    resources = an.ResourcesReport(
        items=[
            an.ResourceItem(
                kind="postgres",
                evidence=[ev("main.py:4", 'DB = os.environ["DATABASE_URL"]')],
                env=[
                    an.EnvRole(role="url", name="DATABASE_URL"),
                    an.EnvRole(role="password", name="MADE_UP_PW"),
                ],
            )
        ]
    )

    def env(name, kind="secret", generate=False):
        return an.EnvItem(name=name, kind=kind, required=True, generate=generate, evidence=[])

    answers = {
        "has_server": server,
        "stack": stack,
        "resources": resources,
        "env": an.EnvReport(
            items=[env("DATABASE_URL"), env("SECRET_KEY", generate=True), env("GHOST_VAR", "config", True)]
        ),
        "risks": an.RisksReport(
            items=[
                an.Risk(
                    title="DB 없이 시작하면 죽음", reason="", evidence=[ev("main.py:4", "DB = os.environ")]
                ),
                an.Risk(title="지어낸 위험", reason="", evidence=[MADE_UP]),
            ]
        ),
    }
    r = run(tmp_path, answers)
    assert r.target == "cloudrun" and r.entrypoint == "uvicorn main:app --host 0.0.0.0 --port $PORT"
    # 저장소에 없는 이름은 버리고, 자원 접속 정보는 사용자 입력에서 뺀다
    assert "외부 저장소가 필요합니다: postgres (DATABASE_URL). 배포 전에 만들어 연결해야 합니다." in r.notes
    assert "넣어야 하는 환경변수: SECRET_KEY (무작위 값 가능)" in r.notes
    assert [n for n in r.notes if n.startswith("기타 위험")] == ["기타 위험: DB 없이 시작하면 죽음 — "]
    assert not any(n.startswith("수정 제안") for n in r.notes)


def test_fixed_port_app(flask_app):
    stack = STACK.model_copy(update={"start": "python app.py", "port_env": None, "port": 5000})
    r = run(flask_app, {"stack": stack})
    assert r.port == 5000 and "ENV PORT=5000" in r.dockerfile
    assert "수정 제안: 코드에 박힌 포트·바인딩 주소·DB 주소·비밀값을 환경변수로 뺄까요?" in r.notes


def test_static_site_is_served_by_nginx_on_port(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "vite build"}}\n')
    (tmp_path / "index.html").write_text('<div id="root"></div>\n')
    spa = SignalReport(value="yes", reason="Vite", evidence=[ev("package.json:1", '"build": "vite build"')])
    stack = an.StackReport(
        language="node", install="npm ci", build="npm run build", static_output="dist", reason="", evidence=[]
    )
    r = run(
        tmp_path,
        {"has_server": SignalReport(value="no", reason="", evidence=[]), "is_spa": spa, "stack": stack},
    )
    assert r.target == "cloudrun" and r.notes[0] == "정적 호스팅 유형으로 판단해 cloudrun에 배포합니다."
    assert r.service_models == ["caas", "iaas"]  # nginx 컨테이너로 감싸 보낸다
    assert "FROM node:20-slim AS build" in r.dockerfile and "RUN npm run build" in r.dockerfile
    assert "COPY --from=build /app/dist/ /usr/share/nginx/html/" in r.dockerfile
    assert "listen ${PORT};" in r.dockerfile  # nginx가 시작할 때 PORT를 채운다
    # CMD는 nginx 이미지에 있으니 거절하지 않는다
    assert not any(n.startswith("AI가 쓴 Dockerfile을 쓰지 않았습니다") for n in r.notes)


def test_os_tools_go_into_dockerfile(flask_app):
    tools = SignalReport(
        value="yes", reason="ffmpeg", evidence=[ev("app.py:1", "from flask import Flask")], tools=["ffmpeg"]
    )
    r = run(flask_app, {"system_packages": tools})
    assert r.target == "cloudrun" and "apt-get install -y --no-install-recommends ffmpeg &&" in r.dockerfile


def test_nothing_deployable(flask_app):
    mac = SignalReport(value="yes", reason="AppKit", evidence=[ev("app.py:1", "from flask import Flask")])
    with pytest.raises(ValueError, match="local·cloudrun 어디에도 배포할 수 없습니다"):
        run(flask_app, {"os_macos": mac})


def test_windows_only_code_cannot_go_to_linux_docker(flask_app):
    # 유형 4는 Windows VM이어야 하는데 local은 리눅스 Docker다
    win = SignalReport(value="yes", reason="win32", evidence=[ev("app.py:1", "from flask import Flask")])
    with pytest.raises(ValueError, match="local·cloudrun 어디에도 배포할 수 없습니다.*Windows"):
        run(flask_app, {"os_windows": win})


def test_no_openai_key_cannot_analyze(monkeypatch, flask_app):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert an.load_model(str(flask_app)) is None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        analyze(str(flask_app))


# ---------- 울타리 ----------


def test_evidence_must_be_real_line_outside_tests(flask_app):
    (flask_app / "tests").mkdir()
    (flask_app / "tests" / "test_app.py").write_text("import sqlite3\n")
    # 줄 번호가 두 줄까지 어긋나는 것은 봐준다
    assert an.check_finding(flask_app, "app.py:4", "app = Flask(__name__)") == {
        "evidence": "app.py:2",
        "text": "app = Flask(__name__)",
    }
    assert an.check_finding(flask_app, "app.py:2", "import torch") is None
    assert an.check_finding(flask_app, "tests/test_app.py:1", "import sqlite3") is None
    assert an.check_finding(flask_app, "../app.py:2", "app = Flask(__name__)") is None


def test_repo_reader_reads_only_inside_repo(flask_app):
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

    reader = an.RepoReader(FakeChat(), str(flask_app))
    assert an.make_inspector(reader)("file_write").value == "no"
    tool_out = {m.tool_call_id: m.content for m in reader.model.seen if isinstance(m, ToolMessage)}
    assert tool_out["a"] == 'app.py:9: open("last.json", "w").write("{}")'
    assert tool_out["b"].startswith("읽을 수 없는 경로")  # 저장소 밖은 막는다


@pytest.fixture
def outside(tmp_path_factory):
    d = tmp_path_factory.mktemp("outside")
    (d / "secret.txt").write_text("OUTSIDE_SECRET=1\n")
    return d


def test_symlinks_cannot_read_outside_repo(flask_app, outside):
    (flask_app / "link.txt").symlink_to(outside / "secret.txt")
    (flask_app / "linkdir").symlink_to(outside, target_is_directory=True)
    (flask_app / "loop").symlink_to(flask_app, target_is_directory=True)  # 자기 자신을 가리킨다
    tools = {t.name: t for t in an.repo_tools(flask_app)}
    assert "link.txt" not in tools["list_files"].invoke({"pattern": "**/*"})
    assert tools["grep"].invoke({"pattern": "OUTSIDE_SECRET"}) == "(찾은 줄 없음)"
    assert tools["grep"].invoke({"pattern": "OUTSIDE_SECRET", "glob": "linkdir/*"}) == "(찾은 줄 없음)"
    for path in ("link.txt", "linkdir/secret.txt", os.path.relpath(outside / "secret.txt", flask_app)):
        assert tools["read_file"].invoke({"path": path}).startswith("읽을 수 없는 경로")
    assert "OUTSIDE_SECRET" not in an.repo_text(flask_app)  # 바로가기를 따라가지 않으니 끝없이 돌지도 않는다
    assert an.check_finding(flask_app, "link.txt:1", "OUTSIDE_SECRET=1") is None


def test_secret_files_are_hidden_from_ai(flask_app):
    (flask_app / ".env").write_text("OPENAI_API_KEY=sk-live-xyz\n")
    (flask_app / ".env.example").write_text("OPENAI_API_KEY=\n")
    (flask_app / "server.key").write_text("PRIVATE\n")
    (flask_app / ".envrc").write_text("export AWS_SECRET=sk-live-envrc\n")
    (flask_app / "prod.env").write_text("DB_PASSWORD=sk-live-prod\n")
    (flask_app / "public.txt").symlink_to(flask_app / ".env")  # 바로가기로 이름을 바꿔도 막는다
    tools = {t.name: t for t in an.repo_tools(flask_app)}
    listed = set(tools["list_files"].invoke({"pattern": "**/*"}).splitlines())
    assert ".env.example" in listed
    assert not {".env", "server.key", ".envrc", "prod.env", "public.txt"} & listed
    assert tools["grep"].invoke({"pattern": "sk-live|PRIVATE"}) == "(찾은 줄 없음)"
    assert tools["read_file"].invoke({"path": ".env"}).startswith("읽을 수 없는 경로")


def test_repo_dockerfile_pointing_outside_is_refused(flask_app, outside):
    (flask_app / "Dockerfile").symlink_to(outside / "secret.txt")
    with pytest.raises(ValueError, match="저장소 밖"):
        run(flask_app)


# ---------- Dockerfile: 템플릿 초안을 AI가 고친다 ----------


def test_render_java_without_wrapper_uses_build_tool_image():
    mvn = render_dockerfile(
        App(
            language="java",
            version="21",
            build="mvn -q -DskipTests package",
            start="java -jar target/app.jar",
        )
    )
    gradle = render_dockerfile(
        App(language="java", version="17", build="gradle bootJar", start="java -jar build/libs/app.jar")
    )
    assert mvn.startswith("FROM maven:3-eclipse-temurin-21\n") and gradle.startswith("FROM gradle:jdk17\n")
    java8 = render_dockerfile(
        App(language="java", version="1.8", build="./mvnw package", start="java -jar target/app.jar")
    )
    assert java8.startswith("FROM eclipse-temurin:8-jdk\n")  # 검사관은 매니페스트의 1.8을 그대로 적을 수 있다


def test_render_dockerfile_for_java_and_os_tools():
    java = render_dockerfile(
        App(
            language="java",
            version="21",
            build="./gradlew bootJar",
            start="java -Dserver.port=$PORT -jar build/libs/app.jar",
        )
    )
    assert "FROM eclipse-temurin:21-jdk" in java and "RUN chmod +x gradlew mvnw 2>/dev/null || true" in java
    node = render_dockerfile(
        App(language="node", install="npm ci", start="npm start", system_tools=["chromium", "unknown-tool"])
    )
    assert "apt-get install -y --no-install-recommends chromium &&" in node
    with pytest.raises(ValueError):
        render_dockerfile(App(language="elixir", start="mix run"))


NO_CACHE = "RUN pip install --no-cache-dir -r requirements.txt"


def fake_writer(dockerfile=None, changes=(), error=None, drafts=None):
    """dockerfile: 돌려줄 내용, 또는 초안을 고치는 함수. drafts에 받은 초안을 남긴다"""

    def write(app, port, draft):
        if drafts is not None:
            drafts.append(draft)
        if error:
            raise error
        text = dockerfile(draft) if callable(dockerfile) else dockerfile
        return an.DockerfileReport(dockerfile=text, changes=list(changes))

    return write


def test_ai_fixes_template_draft(flask_app):
    template, drafts = run(flask_app).dockerfile, []
    write = fake_writer(
        lambda d: d.replace("RUN pip install -r requirements.txt", NO_CACHE),
        ["pip 캐시를 이미지에 남기지 않습니다"],
        drafts=drafts,
    )
    r = run(flask_app, write=write)
    assert drafts == [template] and NO_CACHE in r.dockerfile
    assert "AI가 저장소를 읽고 Dockerfile을 고쳤습니다: pip 캐시를 이미지에 남기지 않습니다" in r.notes


@pytest.mark.parametrize(
    "bad, why",
    [
        (lambda d: d.replace("ENV PORT=8080\n", ""), "ENV PORT=8080"),
        (lambda d: d.replace("COPY . .", "COPY requirements-gpu.txt ."), "requirements-gpu.txt"),
        (lambda d: "", "FROM"),
        (lambda d: d.replace("CMD ", "# CMD "), "CMD"),
        (lambda d: d + "\nFROM python:3.12-slim\nENV PORT=8080\n", "CMD"),  # CMD가 앞 단계에만 있다
    ],
)
def test_ai_dockerfile_must_pass_fence(flask_app, bad, why):
    r = run(flask_app, write=fake_writer(bad))
    assert r.dockerfile == run(flask_app).dockerfile  # 울타리를 못 넘으면 템플릿을 쓴다
    assert any(n.startswith("AI가 쓴 Dockerfile을 쓰지 않았습니다") and why in n for n in r.notes)


def test_ai_dockerfile_error_falls_back_to_template(flask_app):
    r = run(flask_app, write=fake_writer(error=TimeoutError("응답 없음")))
    assert r.dockerfile == run(flask_app).dockerfile
    assert any("TimeoutError: 응답 없음" in n for n in r.notes)


def test_repo_dockerfile_is_kept(flask_app):
    (flask_app / "Dockerfile").write_text("FROM python:3.11\n")
    drafts = []
    r = run(flask_app, write=fake_writer("FROM python:3.12\n", drafts=drafts))
    assert r.dockerfile == "FROM python:3.11\n" and drafts == []
    assert "저장소에 있는 Dockerfile을 그대로 씁니다." in r.notes


def test_ai_writes_dockerfile_for_language_without_template(tmp_path):
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'sinatra'\n")
    ruby = App(language="ruby", start="bundle exec ruby app.rb -o 0.0.0.0 -p $PORT")
    text = (
        "FROM ruby:3.3-slim\nWORKDIR /app\nENV PORT=8080\nCOPY Gemfile ./\nRUN bundle install\nCOPY . .\n"
        "CMD bundle exec ruby app.rb -o 0.0.0.0 -p $PORT\n"
    )
    drafts = []
    dockerfile, _ = _dockerfile(tmp_path, ruby, 8080, fake_writer(text, drafts=drafts))
    assert dockerfile == text and drafts == [None]  # 템플릿이 없으면 처음부터 쓴다
    php = App(language="php", start="apache2-foreground")
    apache = "FROM php:8.3-apache\nENV PORT=8080\nCOPY . /var/www/html/\n"  # 베이스 이미지에 CMD가 있다
    assert _dockerfile(tmp_path, php, 8080, fake_writer(apache))[0] == apache
    with pytest.raises(ValueError):
        _dockerfile(tmp_path, ruby, 8080, fake_writer(error=TimeoutError()))  # AI도 실패하면 만들 수 없다


def test_check_dockerfile(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    ok = (
        "FROM node:20-slim AS build\nCOPY ./requirements*.txt ./\nCOPY --chown=1000 . .\n\n"
        "FROM nginx:alpine\nENV PORT=8080\nCOPY --from=build /app/dist/ /usr/share/nginx/html/\n"
    )
    assert check_dockerfile(ok, tmp_path, 8080) is None
    assert "ENV PORT=3000" in check_dockerfile(ok, tmp_path, 3000)
    assert "Pipfile" in check_dockerfile("FROM python:3.12\nENV PORT 8080\nCOPY Pipfile .\n", tmp_path, 8080)


# ---------- 실제 OpenAI: ANALYZER_LIVE=1일 때만 돈다 ----------

ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"  # 저장소 맨 위 .env (main.py가 읽는 곳)


@pytest.mark.skipif(
    not os.environ.get("ANALYZER_LIVE"), reason="실제 OpenAI를 부른다: ANALYZER_LIVE=1일 때만"
)
def test_live_openai(monkeypatch, tmp_path):
    for k, v in dotenv_values(ROOT_ENV).items():
        if v and k.startswith(("OPENAI_", "ANALYZER_")) and not os.environ.get(k):
            monkeypatch.setenv(k, v)
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip(f"OPENAI_API_KEY가 없습니다 ({ROOT_ENV})")
    (tmp_path / "requirements.txt").write_text("flask\ngunicorn\n")
    (tmp_path / "app.py").write_text(
        "import sqlite3\nfrom flask import Flask\n\napp = Flask(__name__)\n\n\n"
        '@app.post("/todos/<text>")\ndef add(text):\n'
        '    with sqlite3.connect("todo.db") as db:\n'
        '        db.execute("CREATE TABLE IF NOT EXISTS todo (text TEXT)")\n'
        '        db.execute("INSERT INTO todo VALUES (?)", (text,))\n'
        '    return "ok"\n'
    )
    started = time.monotonic()
    r = analyze(str(tmp_path))
    print(f"\n{time.monotonic() - started:.0f}초\n{r.model_dump_json(indent=2)}")  # 실측 결과는 -s로 본다
    assert (r.target, r.language) == ("local", "python")  # SQLite는 데이터 손실 위험이라 local
    assert check_dockerfile(r.dockerfile, tmp_path, r.port) is None
    assert not any(n.startswith("AI 검사관 일부가 실패") for n in r.notes)
