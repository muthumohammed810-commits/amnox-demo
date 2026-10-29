FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    APP_ENV=demo \
    AMNOX_DB_PATH=/tmp/amnox-demo.sqlite3 \
    AMNOX_SEED_DEMO_DATA=true

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN useradd --system --uid 10001 --no-create-home amnox \
    && mkdir -p /data \
    && chown 10001:10001 /data /app

COPY server.py container-entrypoint.py ./
COPY web ./web

EXPOSE 10000
ENTRYPOINT ["python", "/app/container-entrypoint.py"]
