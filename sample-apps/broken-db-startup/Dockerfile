FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py migrate.py ./

ENV PORT=8080
EXPOSE 8080

# migrations must finish before the server starts
CMD ["sh", "-c", "python migrate.py && exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 app:app"]
