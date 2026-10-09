## 요약 (Summary)
<!-- 무엇을, 왜 바꿨는지 2~4줄 -->

## 관련 이슈 (Related)
<!-- 예: Closes #1 -->

## 변경 모듈 (Area)
<!-- 해당 항목에 x. area:* 라벨은 변경 경로를 보고 자동으로 붙습니다. -->
- [ ] analyzer
- [ ] deployer
- [ ] healer
- [ ] api (main.py)
- [ ] contract (schemas.py / docs/interfaces.md)
- [ ] frontend
- [ ] sample-apps
- [ ] infra / docs

## 테스트 방법 (How to test)
<!-- 실행한 명령, 확인한 화면, 실측 배포 결과 등 -->

## 체크리스트 (Checklist)
- [ ] `cd backend && uv run ruff check app tests && uv run ruff format --check app tests && uv run pytest -q` 통과 (backend 변경 시)
- [ ] `ref/`, `.env`, API 키, GCP 자격증명이 포함되지 않음
- [ ] `schemas.py`를 바꿨다면 `docs/interfaces.md`를 동기화하고 팀 합의를 거침
- [ ] 실측 배포(Docker / Cloud Run)를 했다면 컨테이너·서비스 정리 완료
