FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY openclaw_v1/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY openclaw_v1/ /app/openclaw_v1/

ENV TRADE_DB_PATH=/data/trades.db \
    CLAW_MODE=paper \
    CLAW_VERSION=dev \
    PORT=8080

EXPOSE 8080

WORKDIR /app/openclaw_v1
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
