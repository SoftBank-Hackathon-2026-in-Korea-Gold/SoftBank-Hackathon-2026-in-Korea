"""2단계: 신호로 배포 유형 후보를 거른다.

규칙은 후보를 지우기만 한다. 그래서 규칙 순서는 결과에 영향을 주지 않는다.
규칙은 신호가 yes일 때만 발동한다. unknown은 "확인 필요"로 남는다.
"""

from .signals import SIGNALS

TYPES = {
    1: '서버리스 함수 (FaaS)',
    2: '서버리스 컨테이너',
    3: 'PaaS',
    4: '가상 서버 (VM)',
    5: '정적 호스팅',
}

# 유형마다 장단점과 클라우드별 대표 서비스 (화면 표시용 예시다. 실제로 어느 서비스에 올릴지는 백엔드가 고른다)
TYPE_INFO = {
    1: {'pros': '요청이 없으면 비용이 거의 들지 않습니다', 'cons': '실행 시간 제한과 콜드 스타트가 있습니다',
        'services': {'aws': 'Lambda', 'gcp': 'Cloud Run functions', 'azure': 'Azure Functions'}},
    2: {'pros': '컨테이너라 클라우드를 옮기기 쉽고, 서버 관리 없이 자동으로 늘고 줄어듭니다', 'cons': '요청 하나의 처리 시간에 제한이 있습니다',
        'services': {'aws': 'ECS Fargate', 'gcp': 'Cloud Run', 'azure': 'Container Apps'}},
    3: {'pros': '플랫폼이 빌드·실행·확장을 맡습니다', 'cons': '플랫폼마다 설정이 달라 옮기기 번거롭습니다',
        'services': {'aws': 'Elastic Beanstalk', 'gcp': 'App Engine', 'azure': 'App Service'}},
    4: {'pros': '제약이 없습니다 (메모리 상태, GPU, 오래 도는 작업)', 'cons': 'OS 패치·확장·장애 대응을 직접 해야 합니다',
        'services': {'aws': 'EC2', 'gcp': 'Compute Engine', 'azure': 'Virtual Machines', 'onprem': '사내 서버 (Docker)'}},
    5: {'pros': 'CDN으로 빠르고 가장 저렴합니다', 'cons': '서버 코드를 실행할 수 없습니다',
        'services': {'aws': 'S3 + CloudFront', 'gcp': 'Cloud Storage + Cloud CDN', 'azure': 'Static Web Apps'}},
}
# 추천 순서: 운영 부담이 적고 옮기기 쉬운 쪽부터. FaaS는 제약이 커서 VM 바로 앞에 둔다
PREFERENCE = [5, 2, 3, 1, 4]

# (id, 발동 조건, 지울 유형, 이유, 근거로 쓸 신호)
RULES = [
    ('R1', lambda s: s['is_spa'] == 'yes' and s['has_server'] != 'yes', {1, 2, 3, 4},
     '서버 코드가 없어 정적 호스팅으로 충분합니다', 'is_spa'),
    ('R2', lambda s: s['has_server'] == 'yes', {5},
     '정적 호스팅은 서버 코드를 실행할 수 없습니다', 'has_server'),
    ('R3', lambda s: s['long_running'] == 'yes', {1},
     '실행 시간 제한이 있고 상주 연결을 유지할 수 없습니다', 'long_running'),
    ('R10', lambda s: s['scheduler'] == 'yes', {1},
     '요청이 없으면 멈추는 환경이라 프로세스 안의 주기 작업이 돌지 않습니다', 'scheduler'),
    ('R11', lambda s: s['memory_state'] == 'yes', {1, 2, 3},
     '인스턴스가 여러 대로 늘면 메모리 상태가 갈라지고, 재시작하면 사라집니다', 'memory_state'),
    ('R4', lambda s: s['sqlite'] == 'yes', {1, 2, 3},
     'SQLite 파일이 재시작·재배포 때 사라집니다', 'sqlite'),
    ('R5', lambda s: s['file_write'] == 'yes', {1, 2, 3},
     '로컬 디스크에 쓴 파일이 재시작·재배포 때 사라집니다', 'file_write'),
    ('R6', lambda s: s['gpu'] == 'yes', {1, 2, 3, 5},
     'GPU는 가상 서버에서만 쓸 수 있습니다', 'gpu'),
    ('R7', lambda s: s['system_packages'] == 'yes', {1, 3},
     'OS 패키지를 설치할 수 없거나 까다롭습니다', 'system_packages'),
    ('R8', lambda s: s['os_windows'] == 'yes', {1, 2, 3, 5},
     'Windows 전용 코드는 리눅스 환경에서 돌지 않습니다', 'os_windows'),
    ('R9', lambda s: s['os_macos'] == 'yes', {1, 2, 3, 4, 5},
     'macOS 전용 코드는 클라우드에서 돌지 않습니다', 'os_macos'),
]

# (발동 조건, 붙일 유형, 태그)
TAGS = [
    (lambda s: s['has_server'] == 'yes' and s['faas_adapter'] != 'yes', 1, '기존 서버 코드를 함수 형태로 바꾸는 어댑터가 필요합니다'),
    (lambda s: s['system_packages'] == 'yes', 2, 'Dockerfile에 OS 패키지 설치를 추가해야 합니다'),
    (lambda s: s['os_windows'] == 'yes', 4, 'Windows VM이 필요합니다'),
]


def apply_rules(signals: dict) -> dict:
    values = {k: v['value'] for k, v in signals.items()}
    removed = {}
    for rid, when, types, reason, signal in RULES:
        if when(values):
            for t in sorted(types):
                removed.setdefault(t, []).append({'rule': rid, 'reason': reason, 'hits': signals[signal]['hits']})

    candidates = [t for t in TYPES if t not in removed]
    tags = {}
    for when, t, tag in TAGS:
        if when(values) and t in candidates:
            tags.setdefault(t, []).append(tag)

    review = {k: v['hits'] for k, v in signals.items() if v['value'] == 'unknown' and k in SIGNALS}
    blocked = None
    if not candidates:
        blocked = '배포할 수 있는 유형이 없습니다. 탈락 근거의 코드를 먼저 고쳐야 합니다.'
    return {'candidates': candidates, 'removed': removed, 'tags': tags, 'review': review, 'blocked': blocked}
