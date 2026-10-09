"""초기 Dockerfile: 검사관이 찾은 실행 방법(명세의 app)으로 만든다.

팀 계약대로 0.0.0.0:$PORT로 받는다 (Cloud Run이 PORT=8080을 넣는다). 배포가 실패하면 healer가 이 Dockerfile을 고친다.
CMD는 셸 형식이라 실행할 때 $PORT가 풀린다.
"""

from __future__ import annotations

import re

from .spec import App

BASE = {
    "python": "python:{v}-slim",
    "node": "node:{v}-slim",
    "java": "eclipse-temurin:{v}-jdk",
    "go": "golang:{v}",
}
# 래퍼(mvnw·gradlew)가 없으면 빌드 도구가 든 이미지를 쓴다. JDK 이미지에는 mvn·gradle이 없다
JAVA_TOOL_BASE = {"mvn": "maven:3-eclipse-temurin-{v}", "gradle": "gradle:jdk{v}"}
VERSION = {"python": "3.12", "node": "20", "java": "21", "go": "1.22"}
# 검사관이 말한 OS 도구 → Debian 패키지
APT = {
    "chromium": "chromium",
    "ffmpeg": "ffmpeg",
    "tesseract": "tesseract-ocr",
    "poppler": "poppler-utils",
    "libreoffice": "libreoffice",
    "imagemagick": "imagemagick",
    "libgl": "libgl1",
    "cairo": "libcairo2",
    "pango": "libpango-1.0-0",
    "libpq": "libpq-dev",
    "wkhtmltopdf": "wkhtmltopdf",
}


def _version(app: App) -> str:
    m = re.search(r"\d+(?:\.\d+)?", app.version or "")
    v = m.group() if m else VERSION.get(app.language or "", VERSION["node"])
    if app.language == "java":
        v = v.removeprefix("1.")  # Java 8은 1.8로도 적는다
    return v.split(".")[0] if app.language in ("node", "java") else v


def render(app: App, port: int = 8080) -> str:
    if app.static_output:
        return _static(app, port)
    if app.language not in BASE or not app.start:
        raise ValueError(f"Dockerfile을 만들 수 없습니다 (언어: {app.language}, 시작 명령: {app.start})")
    base = BASE[app.language]
    if app.language == "java":
        base = JAVA_TOOL_BASE.get((app.build or "").split(" ")[0], base)
    lines = [f"FROM {base.format(v=_version(app))}", "WORKDIR /app", f"ENV PORT={port}"]
    if app.language == "python":
        lines.append("ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1")
    if tools := [APT[t] for t in app.system_tools if t in APT]:
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{' '.join(tools)} && rm -rf /var/lib/apt/lists/*"
        )
    lines.append("COPY . .")
    if app.language == "java":
        lines.append("RUN chmod +x gradlew mvnw 2>/dev/null || true")
    lines += [f"RUN {cmd}" for cmd in (app.install, app.build) if cmd]
    lines += [f"EXPOSE {port}", f"CMD {app.start}"]
    return "\n".join(lines) + "\n"


def _static(app: App, port: int) -> str:
    """정적 사이트: 빌드가 있으면 node로 빌드하고, nginx가 $PORT로 내보낸다 (nginx 이미지가 시작할 때 템플릿의 ${PORT}를 채운다)."""
    lines = []
    if app.build:
        lines += (
            [
                f"FROM node:{_version(app) if app.language == 'node' else VERSION['node']}-slim AS build",
                "WORKDIR /app",
                "COPY . .",
            ]
            + [f"RUN {cmd}" for cmd in (app.install, app.build) if cmd]
            + [""]
        )
    source = f"--from=build /app/{(app.static_output or '.').strip('./') or '.'}/" if app.build else ". "
    lines += [
        "FROM nginx:alpine",
        f"ENV PORT={port}",
        (
            "RUN mkdir -p /etc/nginx/templates && printf 'server {\\n  listen ${PORT};\\n  root /usr/share/nginx/html;\\n"
            "  location / { try_files $uri /index.html; }\\n}\\n' > /etc/nginx/templates/default.conf.template"
        ),
        f"COPY {source} /usr/share/nginx/html/",
        f"EXPOSE {port}",
    ]
    return "\n".join(lines) + "\n"
