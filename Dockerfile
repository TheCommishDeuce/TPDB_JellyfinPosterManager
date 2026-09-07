FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=5001

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/cache /app/logs

EXPOSE 5001
VOLUME ["/app/data", "/app/cache", "/app/logs"]

CMD ["python", "app.py"]