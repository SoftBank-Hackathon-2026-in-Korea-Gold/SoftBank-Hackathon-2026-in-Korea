# Flask 할 일 목록

Flask + SQLite로 만든 간단한 할 일 목록 앱입니다. 데이터는 `todo.db` 파일에 저장되며, 처음 실행할 때 테이블이 자동으로 생성됩니다.

## 환경 변수

- `SECRET_KEY` — Flask 세션/flash 메시지용 시크릿 키

## 실행

```bash
pip install -r requirements.txt
gunicorn app:app --bind 0.0.0.0:5000
```

브라우저에서 http://localhost:5000 접속
