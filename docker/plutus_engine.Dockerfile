# ── Plutus Engine Dockerfile ──────────────────────────────────────────────────
# Stage: Build / Runtime for the LLM Brain + Scanner Worker
# Base: Python 3.11 slim for small image size and fast startup

FROM python:3.11-slim

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set a sane default umask (containers should not run as root, but be safe)
RUN groupadd --gid 1000 appgroup \
 && useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# Install system dependencies needed for some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Core / networking
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.1 \
    httpx==0.27.0 \
    aiohttp==3.9.5 \
    requests==2.32.3

# Data / numerics
RUN pip install --no-cache-dir \
    pandas==2.2.2 \
    numpy==1.26.4

# Redis
RUN pip install --no-cache-dir \
    redis==5.0.6

# TimescaleDB / PostgreSQL
RUN pip install --no-cache-dir \
    asyncpg==0.29.0 \
    psycopg2-binary==2.9.9 \
    sqlalchemy==2.0.31

# Binance
RUN pip install --no-cache-dir \
    python-binance==1.0.19

# Technical analysis
RUN pip install --no-cache-dir \
    ta==0.11.0 \
    ta-lib  # no binary wheel; may need ta fallback

# LLM / inference
RUN pip install --no-cache-dir \
    openai==1.30.5 \
    anthropic==0.25.8

# Logging
RUN pip install --no-cache-dir \
    loguru==0.7.2

# Copy source — build context is the project root, so src/ is directly accessible.
COPY src /app/src

# Switch to non-root user
USER appuser

# Expose FastAPI / Uvicorn port
EXPOSE 8000

# Entry point mirrors the module invocation convention used in the codebase
ENTRYPOINT ["python", "-m", "src.engine"]
CMD ["--help"]
