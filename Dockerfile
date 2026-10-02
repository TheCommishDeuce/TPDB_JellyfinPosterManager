FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME_BINARY=/usr/bin/chromium \
    HOME=/home/appuser \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=5001 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    APP_STATE_DIR=/app/data \
    CACHE_DIR=/app/cache \
    LOG_DIR=/app/logs

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y chromium chromium-driver tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/cache /app/logs /home/appuser \
    && chown -R root:root /app \
    && chmod -R go-w /app \
    && chown -R 99:100 /app/data /app/cache /app/logs /home/appuser

# The entrypoint initializes volume ownership, then drops privileges.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/docker-entrypoint.py"]

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/container-healthcheck.py"]

CMD ["python", "app.py"]
