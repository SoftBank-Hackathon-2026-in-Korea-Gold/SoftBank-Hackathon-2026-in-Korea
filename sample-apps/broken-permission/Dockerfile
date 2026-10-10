FROM python:3.12-slim

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8080
EXPOSE 8080

USER appuser
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 app:app
