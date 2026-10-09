"""analyzer 내부 (owner: 이요환). 바깥에서는 app.analyzer.analyze()만 쓴다.

- signals.py   패턴 검색: 검사관에게 주는 참고 줄 (LLM이 없을 때는 이걸로 판정)
- inspectors.py AI 검사관: 찾는 것마다 각자의 기준으로 판정하고, 근거는 실제 파일로 확인한다
- rules.py     신호 → 배포 유형 정책 (AI가 정하지 않는다)
- offers.py    유형마다 필요한 코드 수정
- graph.py     분석 흐름 (LangGraph). 중간 질문(동의·유형·수정 승인)은 interrupt로 받는다
- spec.py      배포 명세: 실행 방법, 외부 저장소, 환경변수, 제약
- dockerfile.py 명세의 실행 방법으로 초기 Dockerfile 템플릿을 만든다. AI가 쓴 Dockerfile의 울타리(check)도 여기 있다
- llm.py       OpenAI 연결과 저장소 읽기 도구
"""
