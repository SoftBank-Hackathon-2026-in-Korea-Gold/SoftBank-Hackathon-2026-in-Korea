# FastAPI 메모장

FastAPI + SQLAlchemy + MySQL 메모 앱입니다. 시작 시 `notes` 테이블을 자동으로 생성합니다.

## 환경 변수

- `DATABASE_URL` — 예: `mysql+pymysql://user:pass@localhost:3306/notes`

## 실행

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

- `GET /` 메모 목록
- `POST /notes` 메모 작성 (form: `title`, `author_email`)
- `GET /health` 헬스 체크
