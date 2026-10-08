"""AI 검사관: 찾는 것마다 각자의 기준으로 AI가 저장소를 직접 읽고 판정한다.

패턴 규칙은 저장소마다 새 문법이 나와 끝없이 늘어났다 (Kotlin, 나눠 둔 Spring 설정, R2DBC …).
그래서 찾고 판단하는 일은 검사관에게 맡기고, 코드는 울타리만 친다.
- 근거 확인: 검사관이 낸 근거(파일:줄 + 그 줄 코드)가 실제 파일에 있어야 한다. 없으면 버린다
- 이름 확인: 환경변수 이름은 저장소에 실제로 있어야 받는다 (Spring의 SPRING_* 규칙 이름만 예외)
- 정책: 신호 → 배포 유형은 rules.py가 정한다. 검사관은 유형을 고르지 않는다
- 동의: 치명적 위험을 '찾았지만 괜찮다'고 하면 사용자 동의를 받는다 (analyzer.py)
패턴 규칙(signals.py)은 검사관에게 주는 참고 줄과, LLM이 없을 때의 판정에만 쓴다. 더 늘리지 않는다.
"""
import re
from pathlib import Path

from . import llm
from .signals import TEST_DIRS, TEST_FILE, _walk, read_text

# 위험 신호 검사관: (제목, 판정 기준). value는 yes(위험) / no
SIGNAL_CRITERIA = {
    'has_server': ('서버 코드', ('HTTP 요청을 받는 서버 프로세스를 띄우는 코드가 있는가. 웹 프레임워크(Express, FastAPI, Spring 등)로 라우트를 등록하거나 '
                   '포트를 여는 코드면 yes. 빌드해서 정적 파일만 내보내는 프런트엔드, CLI, 배치 스크립트, 라이브러리면 no.')),
    'is_spa': ('정적 프런트엔드', ('빌드 결과가 정적 파일(HTML·JS·CSS)뿐이라 웹 서버 없이 CDN에 올리면 되는 프런트엔드인가. '
               'SSR(Next.js 서버 모드 등)이나 API 서버가 함께 있으면 no.')),
    'long_running': ('상주 작업', ('요청 처리와 별개로 프로세스가 계속 살아 있어야 하는 작업이 있는가: WebSocket·SSE처럼 오래 붙어 있는 연결, '
                     '백그라운드 워커·큐 소비자, 시작할 때 띄워 계속 도는 루프·코루틴. 요청 안에서 끝나는 비동기 처리는 no.')),
    'scheduler': ('주기 작업', ('프로세스 안에서 정해진 주기로 실행되는 작업이 있는가 (cron 라이브러리, @Scheduled, setInterval, APScheduler 등). '
                  '안 돌면 기능이 깨지는 작업이면 yes. 캐시 갱신처럼 안 돌아도 되는 것뿐이면 근거는 적되 value는 no.')),
    'memory_state': ('메모리 상태', ('여러 요청에 걸쳐 유지돼야 하는 값을 프로세스 메모리에만 두는가 (전역 변수·싱글턴 필드의 맵·리스트·카운터, '
                     '메모리 세션 저장소). 재시작하거나 여러 대로 늘면 사용자에게 보이는 데이터가 사라지거나 갈라지면 yes. '
                     '다시 계산할 수 있는 캐시, 상수, 설정값뿐이면 근거는 적되 value는 no.')),
    'sqlite': ('SQLite', 'SQLite 파일을 데이터 저장소로 쓰는가. 의존성에만 있고 코드에서 쓰지 않거나, 테스트에서만 쓰면 no.'),
    'file_write': ('로컬 파일 쓰기', ('재시작·재배포·여러 대로 늘었을 때 사라지면 안 되는 파일을 로컬 디스크에 쓰는가 (업로드 저장, 만든 결과물 보관 등). '
                   '파일을 쓰는 코드는 모두 근거로 적고, 캐시·로그·임시 파일처럼 잃어도 되는 것뿐이면 value는 no.')),
    'gpu': ('GPU', '실행에 GPU가 꼭 필요한가. cuda를 강제하거나 GPU 전용 라이브러리를 쓰면 yes. CPU로도 도는 추론(device 기본값이 cpu 등)이면 no.'),
    'system_packages': ('OS 패키지', ('실행에 언어 패키지 매니저로는 안 깔리는 OS 패키지가 필요한가 (ffmpeg, chromium, tesseract, libreoffice, '
                        '네이티브 라이브러리 등). 필요하면 tools에 도구 이름을 적는다.')),
    'os_windows': ('Windows 전용', 'Windows에서만 동작하는 코드인가 (win32 API, 레지스트리, COM, Windows 전용 .NET 등).'),
    'os_macos': ('macOS 전용', 'macOS에서만 동작하는 코드인가 (AppKit, osascript 등).'),
    'hardcoded_config': ('박힌 비밀값·설정', ('운영에서 바꿔야 하거나 드러나면 안 되는 값(비밀번호, API 키, 운영 DB·서버 주소)이 코드나 설정 파일에 '
                         '그대로 적혀 있는가. 환경변수로 덮어쓸 수 있는 로컬 개발 기본값, 예시 값, 테스트 값뿐이면 근거는 적되 value는 no.')),
    'bind_localhost': ('localhost 바인딩', '서버가 127.0.0.1이나 localhost에만 바인딩해서 컨테이너 밖에서 접속할 수 없는가.'),
}

# 명세를 채우는 검사관: (제목, 기준, 보고 형식)
SPEC_INSPECTORS = {
    'stack': ('실행 방법', ('이 앱을 리눅스 컨테이너에서 설치·빌드·실행하는 방법. 저장소에 있는 매니페스트·스크립트·진입점을 근거로 쓴다. '
              '서버면 start는 0.0.0.0에 바인딩하고 PORT 환경변수로 포트를 받게 쓴다: 코드가 이미 PORT를 읽으면 실행 명령만, '
              '아니면 실행 옵션으로 준다 (예: uvicorn app.main:app --host 0.0.0.0 --port $PORT, java -Dserver.port=$PORT -jar …). '
              '그렇게 했으면 port_env는 "PORT". 포트를 바꿀 수 없으면 port_env는 null, port는 고정 포트. '
              'health는 이미 있는 헬스체크 GET 경로 (Spring actuator가 있으면 /actuator/health), 없으면 null. '
              '빌드 결과가 정적 파일뿐이면 start는 null, static_output은 결과 폴더. FaaS 핸들러가 이미 있으면 handler.'),
              llm.StackReport),
    'resources': ('외부 저장소', ('앱이 연결하는 외부 저장소(PostgreSQL, MySQL, Redis, S3 호환 저장소 — MinIO 포함)와, 접속 정보를 받는 환경변수 이름. '
                  '역할마다 코드·설정이 실제로 읽는 이름을 쓴다. URL 하나로 받으면 url, 나눠 받으면 host·port·user·password·database. '
                  '설정 파일에 자리표시자가 없어 프레임워크 규칙으로 덮어써야 하면 그 이름을 쓴다 (Spring: SPRING_DATASOURCE_URL, SPRING_R2DBC_URL …). '
                  'scheme은 url 값의 형식. 의존성에만 있고 쓰지 않는 저장소와 SQLite는 뺀다.'),
                  llm.ResourcesReport),
    'env': ('환경변수', ('앱이 실행 중에 읽는 환경변수 전부 (코드의 getenv·process.env 등, 설정 파일의 ${...} 자리표시자). '
            '빌드·CI에서만 쓰는 것과 PORT는 뺀다. 비밀번호·키·토큰은 secret, 그 밖(비밀값 파일의 경로 포함)은 config. '
            '기본값은 설정값만 적는다 (빈 문자열은 기본값이 아니다). 비밀값은 개발용 기본값이 있어도 required. '
            'generate는 세션 서명 키, 이 앱이 발급하는 토큰처럼 무작위 값이면 되는 비밀값만 true.'),
            llm.EnvReport),
    'risks': ('기타 위험', ('다른 검사관이 보는 항목(위험 신호, 실행 방법, 외부 저장소, 환경변수) 말고도 배포에 중요한 위험: '
              '시작할 때 큰 모델·데이터를 메모리에 올림, 이미지에 없는 파일이 필요함, 환경변수가 없으면 시작하자마자 죽음, '
              '운영에 맞지 않는 설정 등. 코드에서 확인한 것만 적고, 없으면 빈 목록.'),
              llm.RisksReport),
}

INSPECTORS = list(SIGNAL_CRITERIA) + list(SPEC_INSPECTORS)

# 코드를 고친 뒤 다시 돌릴 검사관
RECHECK = {
    'sqlite': ['sqlite', 'resources', 'env'],
    'storage': ['file_write', 'resources', 'env'],
    'redis': ['memory_state', 'resources', 'env'],
    'scheduler': ['scheduler', 'env'],
    'crossplatform': ['os_windows', 'system_packages'],
    'config': ['hardcoded_config', 'bind_localhost', 'stack', 'env'],
    'health': ['stack'],
    'faas_adapter': ['stack'],
    'dockerfile': [],
}


def make_inspector(model):
    """inspect(name, hints, files) -> 보고서. hints는 패턴 검색이 찾은 참고 줄 (틀릴 수 있다)."""
    def inspect(name: str, hints: str, files: dict):
        if name in SIGNAL_CRITERIA:
            (title, criterion), schema = SIGNAL_CRITERIA[name], llm.SignalReport
        else:
            title, criterion, schema = SPEC_INSPECTORS[name]
        return llm.structured(model, schema, name).invoke([
            ('system', (f'너는 배포 분석의 검사관 중 하나다. 맡은 항목 하나만 본다: {title}.\n판정 기준: {criterion}\n'
                       '저장소 파일을 직접 읽고 판단한다. 근거는 실제 파일:줄과 그 줄의 코드를 고치지 않고 그대로 적는다. '
                       '근거에는 이 항목에 해당할 수 있는 코드만 적고, 해당하는 코드가 없으면 비워 둔다. '
                       f'추측하지 않는다. 테스트 코드는 뺀다. {llm.GUARD}')),
            ('user', (f'참고: 패턴 검색이 찾은 줄 (틀리거나 빠진 것이 있을 수 있다)\n{hints or "(없음)"}\n\n'
                     f'프로젝트 파일:\n{llm.sources(model, files)}')),
        ])

    return inspect


# ---------- 울타리: 검사관 보고를 실제 파일로 확인한다 ----------

def check_finding(root: Path, evidence: str, text: str) -> dict | None:
    """근거가 실제 파일에 있는지 확인한다. 있으면 hit, 없거나 테스트 코드면 None. 줄 번호가 두 줄까지 어긋나는 것은 봐준다."""
    path, _, line = evidence.rpartition(':')
    p = (root / path).resolve()
    if not line.isdigit() or not p.is_relative_to(root) or not p.is_file():
        return None
    rel = p.relative_to(root)
    if any(part in TEST_DIRS for part in rel.parts) or TEST_FILE.fullmatch(p.name):
        return None
    want, lines, n = ' '.join(text.split()), read_text(p).splitlines(), int(line)
    for i in range(max(1, n - 2), min(len(lines), n + 2) + 1):
        got = ' '.join(lines[i - 1].split())
        if want and got and (want in got or (len(got) >= 8 and got in want)):
            return {'evidence': f'{rel}:{i}', 'text': lines[i - 1].strip()[:200]}
    return None


def repo_text(root: Path) -> str:
    """이름 확인용: 테스트를 뺀 저장소 전체 텍스트 (.env.example 포함)"""
    return '\n'.join(read_text(p) for p in _walk(root))


def _hits(root: Path, items, name: str, dropped: list) -> list:
    hits = []
    for ev in items:
        hit = check_finding(root, ev.evidence, ev.text)
        if hit and hit not in hits:
            hits.append(hit)
        elif not hit:
            dropped.append({'inspector': name, 'evidence': ev.evidence, 'text': ev.text,
                            'why': '파일에서 그 줄을 찾지 못했거나 테스트 코드입니다'})
    return hits


def validate(name: str, report, root: Path, text: str) -> tuple[dict, list]:
    """(확인한 보고서 dict, 버린 것들). 근거는 실제 줄로 바꾸고, 저장소에 없는 이름은 버린다."""
    dropped = []

    def known(env_name: str) -> bool:
        if re.search(rf'(?<![A-Za-z0-9_]){re.escape(env_name)}(?![A-Za-z0-9_])', text) or env_name.startswith('SPRING_'):
            return True
        dropped.append({'inspector': name, 'evidence': '-', 'text': env_name, 'why': '저장소에 없는 환경변수 이름입니다'})
        return False

    if name in SIGNAL_CRITERIA:
        return {'value': report.value, 'reason': report.reason, 'hits': _hits(root, report.evidence, name, dropped),
                'tools': report.tools}, dropped
    if name == 'stack':
        out = report.model_dump(exclude={'evidence'})
        if out['health'] and not out['health'].startswith('/'):
            out['health'] = None
        out['hits'] = _hits(root, report.evidence, name, dropped)
        return out, dropped
    if name == 'resources':
        return {'items': [{'kind': r.kind, 'env': {e.role: e.name for e in r.env if known(e.name)}, 'scheme': r.scheme,
                           'hits': _hits(root, r.evidence, name, dropped)} for r in report.items]}, dropped
    if name == 'env':
        items = []
        for e in report.items:
            if not known(e.name):
                continue
            items.append({**e.model_dump(exclude={'evidence'}), 'default': e.default if e.kind == 'config' else None,
                          'generate': e.generate if e.kind == 'secret' else None,
                          'hits': _hits(root, e.evidence, name, dropped)})
        return {'items': items}, dropped
    # risks: 근거가 확인된 것만 남긴다
    items = []
    for r in report.items:
        hits = _hits(root, r.evidence, name, dropped)
        if hits:
            items.append({'title': r.title, 'reason': r.reason, 'hits': hits})
    return {'items': items}, dropped
