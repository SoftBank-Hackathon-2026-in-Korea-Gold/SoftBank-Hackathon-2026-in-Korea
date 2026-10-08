# Visit Counter

Express + PostgreSQL 방문자 카운터입니다. `/` 에 접속할 때마다 방문 기록을 남기고 총 방문 수를 보여줍니다.

## 환경 변수

- `DATABASE_URL` — PostgreSQL 연결 문자열 (예: `postgres://user:pass@localhost:5432/visits`)
- `SESSION_SECRET` — 세션 시크릿
- `PORT` — 포트 (기본 3000)

## 실행

```bash
npm install
npm start
```

헬스 체크: `GET /health`
