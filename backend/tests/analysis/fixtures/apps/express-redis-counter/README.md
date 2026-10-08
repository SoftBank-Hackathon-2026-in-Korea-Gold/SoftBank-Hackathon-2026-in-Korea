# Redis 페이지 카운터

Express + Redis로 페이지 조회수를 세는 앱입니다.

## 환경 변수

- `REDIS_URL` — 예: `redis://localhost:6379`
- `API_TOKEN` — `/admin` 접근용 토큰 (`Authorization: Bearer <토큰>` 헤더)
- `PORT` — 포트 (기본 3000)

## 실행

```bash
npm install
npm start
```

- `GET /` 조회수 증가 및 표시
- `GET /health` Redis 연결 확인
- `GET /admin` 조회수 JSON (토큰 필요)
