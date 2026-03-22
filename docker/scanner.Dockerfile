# docker/scanner.Dockerfile
# RT1: Binance WebSocket → Redis Stream producer service
# RT5: Separated from plutus_engine for independent scaling

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install websocket-client (required by BinanceWebsocketClient)
RUN pip install --no-cache-dir websocket-client redis[hiredis] loguru

COPY src/ /app/src/
COPY pyproject.toml uv.lock .env.example ./

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# RT1 fix: Runs BinanceConnector — WebSocket → XADD to Redis stream
# Listens on port 8002 for health checks
CMD ["python", "-m", "src.engine.scanner_cli"]
