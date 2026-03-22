# ── Execution Node Dockerfile ────────────────────────────────────────────────
# Stage: Order execution and management — talks directly to Binance
# Base: Python 3.11 slim

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 appgroup \
 && useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Core networking
RUN pip install --no-cache-dir \
    httpx==0.27.0 \
    aiohttp==3.9.5 \
    requests==2.32.3

# Redis client
RUN pip install --no-cache-dir \
    redis==5.0.6

# TimescaleDB / PostgreSQL
RUN pip install --no-cache-dir \
    asyncpg==0.29.0 \
    psycopg2-binary==2.9.9 \
    sqlalchemy==2.0.31

# Binance SDK
RUN pip install --no-cache-dir \
    python-binance==1.0.19

# Numerics
RUN pip install --no-cache-dir \
    numpy==1.26.4 \
    pandas==2.2.2

# Logging
RUN pip install --no-cache-dir \
    loguru==0.7.2

# Configuration
RUN pip install --no-cache-dir \
    pydantic==2.7.4 \
    pydantic-settings==2.3.0

# Copy source — build context is the project root, so src/ is directly accessible.
COPY src /app/src

USER appuser

# Execution node listens on port 8001 for health + metrics
# Subscribes to scanner.events (Redis pub/sub) and executes live trades
ENTRYPOINT ["python", "-m", "src.execution"]
CMD ["--mode", "test"]
