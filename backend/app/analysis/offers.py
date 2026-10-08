"""LLM이 제안하는 코드 수정 목록.

두 종류가 있다.
- 후보를 늘리는 수정: 탈락 원인이 된 코드를 고쳐서 유형을 다시 연다 (SQLite → Postgres 등)
- 배포 준비: 어느 유형으로 가든 필요한 수정 (환경변수, 헬스체크, Dockerfile)

순서가 곧 적용 순서다. Dockerfile은 다른 수정이 끝난 코드를 보고 만들어야 해서 마지막에 둔다.

사용자가 고른 유형으로 가는 데 필요한 수정만 plan()이 고른다.
작은 수정(SMALL)은 동작을 바꾸지 않는 배포 준비라 자동으로 적용하고, 나머지는 사용자에게 묻는다.
"""
from .rules import RULES, apply_rules

NODE_PY = {'node', 'python'}

OFFERS = [
    {
        'id': 'sqlite', 'signals': ['sqlite'],
        'question': 'SQLite를 Postgres로 옮길까요?',
        'prompt': 'SQLite를 PostgreSQL로 옮긴다. 접속 정보는 DATABASE_URL 환경변수에서 읽는다. '
                  'SQL 문법 차이(자동 증가 키, 플레이스홀더 ? → %s 등)도 고치고, 드라이버를 의존성 파일에 추가한다.',
    },
    {
        'id': 'storage', 'signals': ['file_write'],
        'question': '로컬 디스크에 저장하는 파일을 오브젝트 스토리지(S3 호환)로 옮길까요?',
        'prompt': '로컬 디스크에 영구 저장하는 파일(업로드, 생성 파일)을 S3 호환 오브젝트 스토리지에 저장하도록 바꾼다. '
                  '버킷은 S3_BUCKET, 엔드포인트는 S3_ENDPOINT(선택) 환경변수에서 읽는다. '
                  'Python은 boto3, Node는 @aws-sdk/client-s3를 쓰고 의존성 파일에 추가한다. 임시 파일(/tmp)은 그대로 둔다.',
    },
    {
        'id': 'redis', 'signals': ['memory_state'],
        'question': '메모리에 든 상태(전역 변수, 메모리 세션)를 Redis로 옮길까요?',
        'prompt': '모듈 전역 변수나 메모리 세션 저장소에 든 상태를 Redis로 옮긴다. 접속 정보는 REDIS_URL 환경변수에서 읽는다. '
                  '카운터는 INCR, 목록은 LIST, 맵은 HASH를 쓴다. express-session이면 connect-redis 저장소를 붙인다. 의존성 파일도 고친다.',
    },
    {
        'id': 'scheduler', 'signals': ['scheduler'],
        'question': '프로세스 안의 주기 작업을 HTTP 엔드포인트로 분리하고, 클라우드 스케줄러가 호출하게 바꿀까요?',
        'prompt': '프로세스 안에서 도는 주기 작업(cron 라이브러리, setInterval, apscheduler 등)을 없애고, '
                  '같은 일을 하는 POST /tasks/<작업 이름> 엔드포인트로 바꾼다. 엔드포인트는 Authorization: Bearer <CRON_TOKEN 환경변수>가 맞을 때만 실행한다. '
                  '쓰지 않게 된 스케줄러 의존성은 의존성 파일에서 뺀다. 만든 엔드포인트 경로와 원래 주기(cron 식)를 schedules에 적는다 (클라우드 스케줄러에 등록할 값).',
    },
    {
        'id': 'crossplatform', 'signals': ['os_windows'],
        'question': 'Windows 전용 코드를 리눅스에서도 도는 라이브러리로 바꿀까요?',
        'prompt': 'Windows에서만 도는 코드(win32com, winreg, pywin32 등)를 리눅스에서도 도는 방식으로 바꾼다. '
                  '예: Excel COM 자동화 → openpyxl, 레지스트리 → 환경변수나 설정 파일. 같은 기능을 낼 수 없으면 edits를 비우고 summary에 이유를 적는다.',
    },
    {
        'id': 'config', 'signals': ['port_hardcoded', 'bind_localhost', 'hardcoded_config'],
        'question': '코드에 박힌 포트·바인딩 주소·DB 주소·비밀값을 환경변수로 뺄까요?',
        'prompt': '포트는 PORT 환경변수(없으면 기존 값)로 받고, 바인딩 주소는 0.0.0.0으로 바꾼다. '
                  '코드에 박힌 DB 주소, IP, 비밀번호, API 키는 환경변수로 빼고 기존 값은 코드에 남기지 않는다. '
                  '새로 쓰는 환경변수를 .env.example에 값 없이(또는 예시 값으로) 적는다.',
        'creates': {'.env.example'},
    },
    {
        'id': 'health', 'signals': ['no_health'],
        'question': '헬스체크 엔드포인트(GET /health)를 추가할까요?',
        'prompt': 'GET /health 엔드포인트를 추가한다. 앱이 DB나 Redis에 의존하면 가벼운 연결 확인(SELECT 1, PING)을 하고, '
                  '정상이면 200과 {"ok": true}, 실패하면 500을 돌려준다.',
    },
    {
        'id': 'faas_adapter', 'signals': ['has_server'],
        'question': '서버리스 함수(FaaS)에서 돌 수 있게 핸들러를 추가할까요? (기존 서버 실행 방식은 그대로)',
        'prompt': '기존 웹 앱을 그대로 감싸는 서버리스 함수 핸들러를 새 파일로 추가한다. 기존 실행 방식은 바꾸지 않는다. '
                  'Python ASGI(FastAPI, Starlette)는 mangum, WSGI(Flask, Django)는 serverless-wsgi로 lambda_handler.py에 handler를 만든다. '
                  'Node는 serverless-http로 handler.js에 handler를 만들고, 앱 파일이 listen을 바로 부르면 require.main 확인으로 감싸 app을 export한다. 의존성 파일도 고친다.',
        'creates': {'lambda_handler.py', 'handler.js'},
    },
    {
        'id': 'dockerfile', 'signals': ['no_dockerfile'],
        'question': 'Dockerfile을 만들까요?',
        'prompt': '이 앱을 실행하는 프로덕션용 Dockerfile과 .dockerignore를 만든다. 공식 slim 이미지를 쓰고, 의존성 설치를 소스 복사보다 먼저 해 캐시를 살린다. '
                  'PORT 환경변수로 포트를 받고 0.0.0.0에 바인딩해 실행한다 (Python은 gunicorn/uvicorn, Node는 npm start 등). '
                  'OS 패키지가 필요하면(예: puppeteer → chromium, opencv → libgl1) apt로 설치한다. root가 아닌 사용자로 실행한다.',
        'creates': {'Dockerfile', '.dockerignore'},
    },
]

BY_ID = {o['id']: o for o in OFFERS}


# 탈락 원인을 없애는 수정 / 유형별 배포 준비 / 자동으로 적용해도 되는 작은 수정
UNBLOCK = ['sqlite', 'storage', 'redis', 'scheduler', 'crossplatform']
PREP_FOR = {1: ['config', 'health', 'faas_adapter'], 2: ['config', 'health'], 3: ['config', 'health'], 4: ['config', 'health'], 5: []}
SMALL = {'config', 'health'}


def plan(target: int, signals: dict, languages: list) -> tuple[list[dict], list[str]]:
    """유형 target으로 가는 데 필요한 수정 목록. 수정으로 풀 수 없는 탈락 원인이 있으면 ([], 이유 목록)."""
    values = {k: v['value'] for k, v in signals.items()}
    firing = [(reason, sig) for _, when, types, reason, sig in RULES if target in types and when(values)]
    fixes = [o for o in OFFERS if o['id'] in UNBLOCK and {sig for _, sig in firing} & set(o['signals'])]
    fixable = {n for o in fixes for n in o['signals']}
    reasons = [reason for reason, sig in firing if sig not in fixable]
    for oid in PREP_FOR[target]:
        o = BY_ID[oid]
        if oid == 'faas_adapter':
            need = values['has_server'] == 'yes' and values['faas_adapter'] != 'yes'
            if need and not NODE_PY & set(languages):
                reasons.append('FaaS 어댑터는 Node·Python 앱만 자동으로 만들 수 있습니다')
        else:
            need = any(values[n] == 'yes' for n in o['signals'])
        if need:
            fixes.append(o)
    return ([], reasons) if reasons else (fixes, [])


def hits(offer: dict, signals: dict) -> list:
    out = []
    for n in offer['signals']:
        if signals[n]['value'] == 'yes':
            out += [h for h in signals[n]['hits'] if h not in out]
    return out


def _without(signals: dict, names) -> dict:
    return {k: {**v, 'value': 'no'} if k in names else v for k, v in signals.items()}


def effect(offer: dict, signals: dict, others=()) -> str:
    """이 수정을 하면 무엇이 달라지는지 한 줄로. 해당 신호를 no로 바꿔 규칙을 다시 돌려 본다.
    others는 함께 제안된 다른 수정들. 혼자서는 안 열리지만 같이 고치면 열리는 경우를 알려 준다."""
    if offer['id'] == 'faas_adapter':
        return '유형 1(FaaS)의 "어댑터 필요" 태그가 사라집니다'
    before = set(apply_rules(signals)['candidates'])
    opened = sorted(set(apply_rules(_without(signals, offer['signals']))['candidates']) - before)
    if opened:
        return f'유형 {", ".join(map(str, opened))}이(가) 다시 열립니다'
    if offer['id'] in ('config', 'health', 'dockerfile'):
        return '배포 준비'
    # 막힌 유형마다, 같이 고쳐야 하는 다른 제안을 모아 같은 조합끼리 묶는다
    values = {k: v['value'] for k, v in signals.items()}
    partners = [o for o in others if o['id'] != offer['id'] and o['id'] not in ('config', 'health', 'dockerfile', 'faas_adapter')]
    groups = {}
    for t in sorted({t for _, when, types, _, sig in RULES if sig in offer['signals'] and when(values) for t in types} - before):
        blockers = {sig for _, when, types, _, sig in RULES if t in types and when(values)} - set(offer['signals'])
        need = [o['id'] for o in partners if blockers & set(o['signals'])]
        if blockers <= {n for o in partners for n in o['signals']}:  # 제안만으로 풀 수 있는 유형만
            groups.setdefault(tuple(need), []).append(t)
    if groups:
        return ' / '.join(f'유형 {", ".join(map(str, ts))}은(는) {", ".join(need)}도 고치면 열립니다' for need, ts in groups.items())
    return '다른 탈락 원인이 남아 있어 이것만으로는 후보가 늘지 않습니다'
