# builder: compile lxml from source against the libxml2/libxslt headers
FROM python:3.12-slim-trixie AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev pkg-config libxml2-dev libxslt1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-binary lxml -w /wheels -r requirements.txt

# runtime: slim image, only the built wheels are copied over
FROM python:3.12-slim-trixie
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt && rm -rf /wheels

COPY app.py .

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 app:app
