FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py warmup.py ./

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "python warmup.py && exec gunicorn --bind 0.0.0.0:8080 app:app"]
