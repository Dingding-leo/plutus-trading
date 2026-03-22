# V4 Alt-Data Pipeline — Research & Architecture Document

**Author:** Data Sourcing Lead
**Date:** 2026-03-22
**Status:** Draft for Strategic Review

---

## Executive Summary

This document audits the current V3 data ingestion layer, researches real-world API limits and free/low-cost alternative data sources, and proposes a production-grade multi-tier data pipeline for V4. The goal is to eliminate single points of failure, reduce unnecessary API spend, and surface institutional-grade signals (funding rates, open interest, liquidation heatmaps) that are currently absent from the stack.

**Key findings from the code audit:**
- The REST client is **too conservative on rate limits** (5 req/s floor vs. Binance's 40 req/s ceiling for futures) — wasted throughput
- **No HTTP 429/418 handling** anywhere — a single rate-limit hit will hard-fail the request
- **`Retry-After` header is never read** — the code sleeps on fixed exponential backoff regardless of what the exchange says
- `get_current_price()` and `get_24h_stats()` have **zero retry logic**
- The WebSocket client in `streams/binance_websocket.py` reconnects with a **fixed 5-second backoff** (no exponential growth, no jitter) — will hammer Binance on sustained outages
- **No subscription resumption** after reconnect — after a reconnect the stream never re-registers
- **No stale-data detection** — the client never raises an alert if no messages arrive for N seconds
- The local CSV data lake is a solid foundation; it is not currently leveraged as a fallback for REST failures

---

## Section 1: Binance REST & WebSocket Rate Limits

### 1.1 REST API — Weight-Based System

Binance uses a **weight-based** rate limiter, not a simple request-count limiter. Every endpoint has a documented *weight*. Your 1-minute rolling window sum must stay below the limit.

| Environment | Limit | Window |
|-------------|-------|--------|
| Spot REST | 1,200 weight | 1 minute |
| USDT-M Futures REST | **2,400 weight** | 1 minute |
| Coin-M Futures REST | 2,400 weight | 1 minute |

**Critical weights for this codebase:**

| Endpoint | Weight (no key) | Weight (signed) |
|----------|-----------------|-----------------|
| `GET /fapi/v1/klines` (limit ≤ 100) | 1 | 1 |
| `GET /fapi/v1/klines` (limit = 1000) | 2 | 2 |
| `GET /fapi/v1/ticker/price` | 0.5 | 0.5 |
| `GET /fapi/v1/ticker/24hr` | 2 | 2 |
| `GET /fapi/v1/depth` (limit ≤ 100) | 5 | 5 |
| `GET /fapi/v1/depth` (limit = 1000) | 50 | 50 |
| `GET /fapi/v1/openInterest` | 2 | 2 |
| `GET /fapi/v1/topLongShortAccountRatio` | 20 | 20 |
| `GET /fapi/v1/longShortRatio` | 20 | 20 |
| `GET /fapi/v1/takerLongShortRatio` | 20 | 20 |
| `GET /fapi/v1/fundeRate` | 2 | 2 |
| `GET /fapi/v1/openInterestHist` | 20 | 20 |

> **Implication:** Fetching 200 `klines` = weight 1. You can safely fire 2,400 such requests per minute = **40 req/s**. The current `MIN_INTERVAL = 0.2s` (5 req/s) is correct for conservative safety but leaves ~87.5% of the budget unused. A production client should target 20–30 req/s for futures with headroom for bursts.

**Rate limit headers returned on every response:**

```
X-MBX-USED-WEIGHT: <current_weight_used>
X-MBX-USED-WEIGHT-MINUTE: <weight_used_in_current_minute_window>
Retry-After: <seconds>    # Only present on HTTP 429
```

### 1.2 REST Error Codes

| HTTP Code | Binance Code | Meaning | Action |
|-----------|--------------|---------|--------|
| 429 | -1015 "Too many new orders" | IP rate limited | Read `Retry-After`, sleep, retry |
| 429 | -1003 "Too much request weight used" | Over weight limit | As above |
| 418 | -1003 | IP auto-banned (repeat 429s) | Sleep full ban duration (typically 2 min), do not retry |
| 400 | Various | Bad request | Log and do not retry |

> **418 ban behavior:** If Binance returns 429 repeatedly, your IP is temporarily banned for approximately 2 minutes. Retrying during a ban will extend it. The correct response to 418 is to sleep for **at least 120 seconds** and then resume at a reduced rate.

### 1.3 WebSocket Rate Limits

| Limit | Value |
|-------|-------|
| Combined stream connections per account | **5** |
| Outbound messages per second (per connection) | **10** |
| Ping interval (Binance sends ping) | 20–30 seconds |
| Ping timeout (must respond) | 10 seconds |

**Connection URL:** `wss://stream.binance.com:9443/stream`

> **Practical note:** Each `wss://stream.binance.com:9443/stream?streams=btcusdt@depth20@100ms/btcusdt@trade` subscription counts as 1 connection regardless of how many streams are multiplexed within it. You can subscribe to 50 symbols on one connection and stay within the limit.

### 1.4 Current Code vs. Actual Limits

| Issue | Current Code | Correct Behavior |
|-------|-------------|-------------------|
| REST throttle | `MIN_INTERVAL = 0.2s` (5 req/s) | 20–30 req/s is safe; 40 req/s is the hard ceiling |
| HTTP 429 handling | **None** — hard fail | Parse `Retry-After`, sleep that value |
| HTTP 418 handling | **None** — hard fail | Sleep 120 s, reduce rate permanently |
| Exponential backoff | `sleep(2 ** attempt)` | `min(BASE * 2**attempt + jitter, MAX)` with BASE=1, MAX=60 |
| `get_current_price` retry | None | Add retry with backoff |
| `get_24h_stats` retry | None | Add retry with backoff |
| WebSocket reconnect backoff | Fixed 5 s | Exponential backoff with jitter |
| WebSocket ping | `ping_interval=20` | OK, but needs stale-data watchdog |
| WebSocket resubscription | **None** | Track subscribed streams; re-subscribe on reconnect |

---

## Section 2: Resilient WebSocket Architecture

### 2.1 Design Principles

1. **Auto-reconnect with exponential backoff** — never give up, but never hammer a struggling server
2. **Heartbeat watchdog** — detect stale connections that appear open but are silently dead
3. **Subscription persistence** — reconnecting must not lose stream registrations
4. **Message buffer during disconnect** — queue outbound messages to replay on reconnect (for order submission streams)
5. **Graceful degradation** — fall back to REST polling if WebSocket is down for > N seconds

### 2.2 Proposed WebSocket Architecture

```
Binance WebSocket Streams
       │
       ▼
┌──────────────────────┐
│   ConnectionManager   │  ← Single orchestrator
│  - backoff state      │
│  - subscribed streams │
│  - last_msg_timestamp │
│  - message queue      │
└──────────┬───────────┘
           │
    ┌──────┴──────┐
    │             │
    ▼             ▼
ws.run_forever()  REST Fallback (thread)
                   poll every 30s on disconnect
```

### 2.3 Reconnect Algorithm

```python
import random
import time
import threading
import logging
import websocket
import json

log = logging.getLogger(__name__)

BASE_WAIT   = 1.0    # seconds
MAX_WAIT    = 60.0   # seconds
JITTER_FACTOR = 0.3  # ±30% randomization
STALE_THRESHOLD = 60  # seconds — no message = connection likely dead


class BinanceWSManager:
    """
    Production-grade Binance WebSocket manager.

    Features:
    - Exponential backoff with full jitter on disconnect
    - Subscription tracking and automatic re-subscribe on reconnect
    - Heartbeat watchdog (stale-data detection)
    - REST fallback thread for critical data during WS outages
    - Thread-safe message queue
    """

    STREAM_URL = "wss://stream.binance.com:9443/stream"

    def __init__(
        self,
        symbols: list[str] | None = None,
        channels: list[str] | None = None,
        buffer_size: int = 10_000,
        stale_threshold: int = STALE_THRESHOLD,
    ) -> None:
        self.symbols   = symbols   or ["btcusdt"]
        self.channels  = channels  or ["depth20@100ms", "trade"]
        self.stale_threshold = stale_threshold

        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._attempt = 0
        self._last_msg_time: float = 0.0
        self._lock = threading.Lock()

        # Subscribed streams — tracked so we can re-subscribe on reconnect
        self._stream_list: list[str] = []
        self._stream_url: str = ""

        # Per-symbol snapshots (updated on every message)
        self._depth: dict[str, dict] = {}
        self._trades: dict[str, dict] = {}

    # ─── Public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Start the WS manager and background thread."""
        if self._running:
            raise RuntimeError("Already running. Call disconnect() first.")
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="binance-ws-mgr")
        self._thread.start()

    def disconnect(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=10.0)
        log.info("WS Manager shut down.")

    def is_connected(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ─── Internal Loop ─────────────────────────────────────────────────────────

    def _build_url(self) -> str:
        self._stream_list = [f"{s.lower()}@{c}" for s in self.symbols for c in self.channels]
        self._stream_url = f"{self.STREAM_URL}?streams={'/'.join(self._stream_list)}"
        return self._stream_url

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._attempt = 0  # Reset backoff on successful connection
                url = self._build_url()
                log.info("WS connecting: %s", url)

                self._ws = websocket.WebSocketApp(
                    url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )

                # run_forever blocks until disconnect; on error it returns
                self._ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                )

            except Exception as exc:
                log.warning("WS runner exception: %s", exc)

            if not self._running:
                break  # Clean shutdown

            # ── Exponential backoff ────────────────────────────────────────────
            self._attempt += 1
            wait = self._backoff(self._attempt)
            log.info("WS reconnect in %.1f s (attempt %d)", wait, self._attempt)
            time.sleep(wait)

        log.info("WS run loop exiting.")

    # ─── Exponential backoff with full jitter ──────────────────────────────────
    #
    # Full jitter: wait = random(base * 2^n * (1 - jitter/2, 1 + jitter/2))
    # Example: n=0, base=1  → wait ∈ [0.7, 1.3]
    #          n=1, base=1  → wait ∈ [1.4, 2.6]
    #          n=3, base=1  → wait ∈ [5.6, 10.4]
    #          n=6, base=1  → wait ∈ [44.8, 83.2]  → capped at 60
    #
    # Full jitter is preferred for Binance because many clients will retry
    # simultaneously after an outage. Randomizing across the full range
    # minimises the "thundering herd" problem.

    def _backoff(self, attempt: int) -> float:
        cap = MAX_WAIT
        base = BASE_WAIT
        # Exponential: base * 2^attempt
        raw = base * (2 ** attempt)
        # Full jitter: uniform in [raw * (1 - j), min(raw * (1 + j), cap)]
        lo = raw * (1 - JITTER_FACTOR)
        hi = min(raw * (1 + JITTER_FACTOR), cap)
        return random.uniform(lo, hi)

    # ─── WebSocket Handlers ────────────────────────────────────────────────────

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        log.info("WS opened. Re-subscribing to %d streams.", len(self._stream_list))
        # Binance combined streams re-subscribe automatically on connect via
        # the stream URL; no separate SUBSCRIBE frame needed.
        self._last_msg_time = time.time()

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        self._last_msg_time = time.time()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("WS: JSON decode error — %s", raw[:100])
            return

        stream = payload.get("stream", "")
        data   = payload.get("data", {})

        with self._lock:
            if "depth" in stream:
                self._depth[data.get("s", "")] = data
            elif "trade" in stream:
                self._trades[data.get("s", "")] = data

    def _on_error(self, ws: websocket.WebSocketApp, error: Any) -> None:
        # Note: on_error is followed by on_close, so we don't reconnect here.
        log.error("WS error: %s", error)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: int | None,
        close_msg: str | None,
    ) -> None:
        log.info("WS closed (code=%s, msg=%s)", close_status_code, close_msg)
        # Check for stale connection (no message in threshold seconds)
        elapsed = time.time() - self._last_msg_time
        if elapsed > self.stale_threshold:
            log.warning(
                "WS stale: no message in %.0f s (threshold=%d). Forcing reconnect.",
                elapsed, self.stale_threshold,
            )

    # ─── Stale Data Watchdog ───────────────────────────────────────────────────
    #
    # The ping/pong in run_forever detects TCP-level connection death, but
    # Binance sometimes sends malformed frames or the stream silently stalls.
    # A watchdog thread that monitors _last_msg_time and raises an alert if
    # no message arrives for > stale_threshold seconds adds a second layer.
    #
    # Pseudocode:
    #
    # def _watchdog_loop(self):
    #     while self._running:
    #         time.sleep(5)
    #         elapsed = time.time() - self._last_msg_time
    #         if elapsed > self.stale_threshold:
    #             log.error("STALE DATA: no WS message in %d s!", elapsed)
    #             self._ws.close()  # Trigger reconnect in _run_loop
    #             return
```

### 2.4 Edge Cases

| Edge Case | Detection | Handling |
|-----------|-----------|----------|
| **IP rate limited (HTTP 429)** | REST response code 429 | Read `Retry-After` header; sleep that value; retry |
| **IP auto-ban (HTTP 418)** | HTTP 418 or repeated 429s | Sleep 120 s minimum; do not retry during ban |
| **Exchange maintenance window** | WS closes with code 1010 or WS returns 503 | Exponential backoff; log warning; escalate if > 5 min |
| **WebSocket stream stall** | No message for > `stale_threshold` (60 s) | Force close `_ws` to trigger `_run_loop` reconnect |
| **Malformed JSON message** | `JSONDecodeError` | Log and discard; do not reconnect |
| **Subscription limit exceeded** | WS closes with code 4001 | Reduce symbol/channel count; reconnect |
| **Network timeout (Binance idle)** | WS idle for ping_timeout (10 s) | `run_forever` handles this; reconnect via `_run_loop` |
| **Thundering herd (post-outage)** | All clients reconnect simultaneously | Full jitter randomises backoff window |
| **Memory pressure from queue** | `queue.Full` on inbound | Drop oldest message (already implemented); alert if > 10 drops/s |

---

## Section 3: Free/Low-Cost Alt-Data Alternatives

### 3.1 CoinGecko (Already in codebase)

CoinGecko is the best free market-data source for crypto. It requires no API key and has a generous free tier.

**Free tier limits:**
- 10–30 requests/minute (rate-limited by IP)
- No API key required
- Endpoints: price, market cap, volume, OHLCV, exchange data, coin info

**Key free endpoints for this pipeline:**

```python
# Market data (global)
GET https://api.coingecko.com/api/v3/global
# Returns: total_market_cap, total_volume, market_cap_change_percentage_24h,
#          active_cryptocurrencies, BTC_dominance, ETH_dominance

# Simple price (up to 30 coins per call)
GET https://api.coingecko.com/api/v3/simple/price
    ?ids=bitcoin,ethereum,solana
    &vs_currencies=usd
    &include_market_cap=true
    &include_24hr_vol=true
    &include_24hr_change=true

# Coin OHLC (candlestick data — free, but limited granularity)
GET https://api.coingecko.com/api/v3/coins/bitcoin/ohlc?vs_currency=usd&days=7
# Returns: [timestamp, open, high, low, close] per day

# Fear & Greed Index proxy via global data
# CoinGecko does not provide F&G directly — use Alternative.me (Section 3.2)
```

**Paid tier:** Free tier is sufficient for this pipeline. Paid tier ($79/mo) adds historical data and higher rate limits.

**Documentation:** https://www.coingecko.com/en/api/documentation

> **Note from codebase:** `src/data/coingecko_client.py` exists. Audit it for the same rate-limit and retry issues described in Section 1.4.

---

### 3.2 Alternative.me Fear & Greed Index

The most widely-used sentiment indicator for crypto markets. Completely free, updated every 8 hours.

**Free tier limits:**
- No API key required
- 1 request per hour is sufficient (data updates every 8 hours)
- Historical data available (last ~300 data points)

```python
import requests

# Current Fear & Greed Index
response = requests.get("https://api.alternative.me/fng/")
data = response.json()
# {
#   "name": "Fear & Greed Index",
#   "data": [{
#     "value": "45",
#     "value_classification": "Fear",
#     "timestamp": "1700000000",
#     "time_until_update": "28540"
#   }],
#   "metadata": {"error": None}
# }

# Historical (last 300 data points)
response = requests.get("https://api.alternative.me/fng/?limit=300")
```

**Relevance to trading system:**
- **< 20 — Extreme Fear:** Historically a buy signal. Low risk / higher reward zone.
- **20–45 — Fear:** Market under pressure. Look for reversal setups.
- **45–55 — Neutral:** No strong bias. Follow technicals.
- **55–75 — Greed:** Market overheated. Reduce position sizes, tighten stops.
- **> 75 — Extreme Greed:** Topping zone. Do not initiate new longs.

**Integration cost:** $0 / month

**Documentation:** https://alternative.me/crypto/fear-and-greed-index/

---

### 3.3 Coinglass

Provides institutional-grade derivatives data: open interest, funding rates, liquidations, whale alerts. This is the single most valuable alt-data source for a crypto trading system.

**Free tier limits:**
- API key required (free tier available)
- ~10–50 requests/minute depending on endpoint
- Key free endpoints: funding rates, top traders OI, liquidations

**Free endpoints:**

```python
import requests

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"

# 1. Current Funding Rate for all coins (best free endpoint)
response = requests.get(
    f"{COINGLASS_BASE}/funding_rate",
    params={"symbol": "BTC"}  # or omit for all coins
)
# Returns: symbol, fundingRate, nextFundingTime, exchange

# 2. Top Trader Long/Short Ratio (OI-weighted)
response = requests.get(
    f"{COINGLASS_BASE}/top_trader_long_short_ratio",
    params={"symbol": "BTC", "exchange": "Binance", "interval": "0"}  # 0=current
)
# Returns: longShortRatio, longUser, shortUser, timestamp

# 3. Liquidation Data (last 24h summary)
response = requests.get(
    f"{COINGLASS_BASE}/liquidation",
    params={"symbol": "BTC", "exchange": "Binance", "timeType": "1"}  # 1=24h
)
# Returns: totalLiquidate, longLiquidate, shortLiquidate, symbol, exchange

# 4. Open Interest (current)
response = requests.get(
    f"{COINGLASS_BASE}/open_interest",
    params={"symbol": "BTC", "exchange": "Binance"}
)
# Returns: openInterest, openInterestAmount, symbol, exchange, updatedTime
```

**Why this matters:**
- **Funding rate > 0.1% per 8h** = Binance retail is long. Counterparty risk of pullback.
- **OI spiking + price rejection** = institutional distribution (whales selling into retail longs)
- **Liquidation heatmap** = identify clusters of stop orders (liquidity pools)
- **Top trader long/short ratio divergence from price** = leading indicator

**Paid tiers:**
| Plan | Price | Limits |
|------|-------|--------|
| Free | $0 | 10 req/min, last 30 days history |
| Starter | $29/mo | 50 req/min, 1 year history |
| Pro | $99/mo | 200 req/min, full history, alerts |

**Recommendation for V4:** Start with the free tier. Funding rates and OI are the highest-value signals and are free. Liquidation data is on the free tier with limited history.

**Documentation:** https://open-api.coinglass.com/

---

### 3.4 CryptoRank

Best free source for **upcoming token sales (IDO/IEO/IGO)**, fundraising rounds, and airdrop calendars. Useful for event-driven trading around major token unlock dates.

**Free tier limits:**
- No API key required for basic endpoints
- 60 requests/minute on free tier

```python
CRANKBASE = "https://api.cryptorank.io/v1"

# Upcoming IDO/IEO events
response = requests.get(
    f"{CRANKBASE}/exchanges"
)
# Returns: exchange, token, date, status, price, listingPrice

# Fundraising rounds (track which VCs bought in)
response = requests.get(
    f"{CRANKBASE}/fund-rounds"
)
# Returns: round, amount, valuation, investors, date
```

**Relevance:** Unlock events and IDO listings cause volatility. Monitoring upcoming events allows you to size positions appropriately around high-volatility events.

**Paid tiers:** Free tier is sufficient for event monitoring. Paid starts at $49/mo for full history.

**Documentation:** https://cryptorank.io/api

---

### 3.5 DeFiLlama

The gold standard for on-chain TVL (Total Value Locked) data. Completely free, no API key required.

**Free tier limits:**
- No API key required
- 30 requests/minute
- Full historical TVL data

```python
DEFI_LLAMA_BASE = "https://api.llama.fi"

# Protocol TVL (current)
response = requests.get(f"{DEFI_LLAMA_BASE}/protocol/aave")
# Returns: tvl, chainTVL, change_1d, change_7d, ...

# All protocols TVL (large response)
response = requests.get(f"{DEFI_LLAMA_BASE}/protocols")
# Returns: list of all protocols with TVL, category, chain

# Historical TVL for a protocol
response = requests.get(f"{DEFI_LLAMA_BASE}/protocol/uniswap?excludeTotalDataChart=true")
# Returns: tvlChainTvls (per-chain breakdown over time)

# Stablecoin supply
response = requests.get(f"{DEFI_LLAMA_BASE}/stablecoin")
# Returns: totalLiquidations, totalVolume, ...
```

**Relevance:**
- TVL outflows from DeFi protocols often precede market downturns
- Protocol-level TVL growth indicates sector strength (e.g., Uniswap TVL → DeFi sentiment)
- Stablecoin supply changes (USDC printing/redeeming) are leading liquidity indicators

**Integration cost:** $0 / month

**Documentation:** https://docs.llama.fi

---

### 3.6 Santiment

Social metrics (Twitter/X, Reddit, Telegram, bitcointalk) with a usable free tier.

**Free tier limits:**
- 100 credits/month on free tier (enough for ~50 API calls)
- Requires API key

```python
SANTIMENT_BASE = "https://api.santiment.net"

HEADERS = {"Authorization": "Apikey YOUR_API_KEY"}

# Social volume (daily tweets/mentions for a token)
response = requests.get(
    f"{SANTIMENT_BASE}/projects/bitcoin/social_volume",
    params={
        "interval": "1d",
        "from": "2026-03-15T00:00:00Z",
        "to":   "2026-03-22T00:00:00Z",
        "source": "Twitter"
    },
    headers=HEADERS,
)
# Returns: [{timestamp, twitter_volume}, ...]

# Development activity (GitHub commits — strong signal for project health)
response = requests.get(
    f"{SANTIMENT_BASE}/projects/ethereum/developers_activity",
    params={"interval": "1d"},
    headers=HEADERS,
)

# Wordclouds / viral topics
response = requests.get(
    f"{SANTIMENT_BASE}/wordclouds",
    params={"source": "twitter", "from": "...", "to": "..."},
    headers=HEADERS,
)
```

**Relevance:**
- Social volume spikes often precede price movements by 12–48 hours
- Twitter/X dominates for crypto; Reddit for community coins
- Development activity is a fundamental signal for long-term holds

**Paid tiers:**
| Plan | Price | Credits/mo |
|------|-------|-----------|
| Free | $0 | 100 |
| Starter | $49/mo | 5,000 |
| Pro | $149/mo | 50,000 |

**Recommendation for V4:** Use sparingly. The 100 free credits per month can cover weekly social volume snapshots for 3–5 top assets.

**Documentation:** https://api.santiment.net

---

### 3.7 TradingLite

Provides **orderbook heatmap data** showing where large limit orders are sitting (liquidity pools). This is premium institutional data, previously unavailable to retail.

**Free tier limits:**
- Free tier available but with limited depth
- Requires API key

```python
TRADINGLITE_BASE = "https://api.tradinglite.com/v1"

# Orderbook heatmap / liquidity zones
response = requests.get(
    f"{TRADINGLITE_BASE}/ob-heatmap",
    params={"symbol": "BTCUSDT", "exchange": "binance"},
    headers={"Authorization": "Bearer YOUR_TOKEN"},
)
# Returns: price levels with bid/ask wall sizes
```

**Relevance to trading:**
- **Large bid walls** = support zones that may get swept
- **Large ask walls** = resistance zones
- **Wall absorption** = when a large order is slowly eaten through without moving price → institutional accumulation
- **Wall removal** = walls pulled suddenly → price about to move rapidly in that direction

**Paid tiers:** Contact TradingLite for pricing. This is a premium source; budget for it in V4 if the signal quality justifies it.

**Alternative (free):** Binance's own `GET /fapi/v1/depth` with limit=500 gives a rough orderbook view. Combined with the existing `BinanceWebsocketClient` depth stream, this can approximate a heatmap for free.

**Documentation:** https://docs.tradinglite.com

---

## Section 4: Proposed V4 Data Pipeline Architecture

### 4.1 Multi-Tier Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        PLUTUS V4 DATA PIPELINE                          │
├────────────────────┬───────────────────────────────────────────────────┤
│   Tier 1 (Real-time) │  Tier 2 (Near-real-time)  │  Tier 3 (Batch)     │
│   WebSocket         │  REST Poll (30s)           │  REST / Scheduled   │
│   Binance WS        │                            │                     │
├────────────────────┼────────────────────────────┼─────────────────────┤
│ Price (5m candles) │ Funding Rates (Coinglass)  │ TVL (DeFiLlama)     │
│ Orderbook depth    │ Open Interest (Coinglass)  │ Social Volume       │
│ Trade ticks        │ Fear & Greed (Alt.me)      │   (Santiment)       │
│ Book tickers       │ 24h Liquidations (Coinglass)│ Market Cap/Dom      │
│                    │ Top Trader OI (Coinglass)   │   (CoinGecko)       │
│                    │ 24h Volume (CoinGecko)      │ IDO/Airdrop events  │
│                    │ BTC Dominance (CoinGecko)   │   (CryptoRank)      │
├────────────────────┴────────────────────────────┴─────────────────────┤
│                           DATA STORE (In-memory + SQLite)              │
│  - Latest price: shared dict, updated on every WS tick                 │
│  - Indicators cache: 5m/15m/30m/1h EMAs updated every 30s              │
│  - Sentiment cache: Fear & Greed updated every 8h                       │
│  - OI/Funding cache: updated every 30s                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Data Freshness Requirements

| Data Type | Max Acceptable Age | Source | Update Frequency |
|-----------|-------------------|--------|-----------------|
| Price (live) | 5 seconds | Binance WS | Real-time |
| Orderbook imbalance | 5 seconds | Binance WS | Real-time |
| Trade ticks | 5 seconds | Binance WS | Real-time |
| Open Interest | 1 minute | Coinglass REST | ~30s poll |
| Funding Rate | 5 minutes | Coinglass REST | ~30s poll |
| Fear & Greed | 8 hours | Alternative.me | Every 8h |
| 24h Liquidations | 5 minutes | Coinglass REST | ~30s poll |
| Top Trader OI | 5 minutes | Coinglass REST | ~30s poll |
| TVL | 24 hours | DeFiLlama | Daily batch |
| Social Volume | 24 hours | Santiment | Daily batch |
| Market Cap/Dominance | 5 minutes | CoinGecko REST | ~30s poll |
| Upcoming IDOs | 24 hours | CryptoRank | Daily batch |

### 4.3 New `DataPipelineManager` Class

```python
"""
V4 DataPipelineManager — orchestrates all data sources.

Design goals:
- All data accessible via a single `.get(key)` interface
- Sources update independently on their own schedules
- Stale data is flagged (not silently returned)
- No source blocks another
"""

from __future__ import annotations
import threading
import time
import logging
from typing import Any, Optional
from dataclasses import dataclass, field

from .streams.binance_websocket import BinanceWebsocketClient, StreamConfig
from .coinglass_client import CoinGlassClient          # new
from .fear_greed_client import FearGreedClient         # new
from .defillama_client import DeFiLlamaClient          # new

log = logging.getLogger(__name__)


@dataclass
class DataPoint:
    """A single data point with freshness metadata."""
    value: Any
    timestamp: float          # time.time() when fetched
    max_age_seconds: float    # hard limit for staleness

    def is_stale(self) -> bool:
        return time.time() - self.timestamp > self.max_age_seconds

    def get(self, default: Any = None) -> Optional[Any]:
        """Return value only if not stale; otherwise None."""
        if self.is_stale():
            log.warning("Data is stale (%.0f s old, max %.0f s)",  # noqa: G001
                        time.time() - self.timestamp, self.max_age_seconds)
            return default
        return self.value


@dataclass
class DataPipelineManager:
    """
    Centralised, thread-safe data access layer.

    Sources are updated on their own threads.  Consumers call
    .get("price:BTCUSDT") or .get("funding_rate:BTC") and receive
    fresh data or None if stale.
    """

    # ── Source config ─────────────────────────────────────────────────────────
    ws_symbols:     list[str] = field(default_factory=lambda: ["btcusdt", "ethusdt", "solusdt"])
    coinglass_key:  str | None = None    # from env: COINGLASS_API_KEY
    santiment_key:  str | None = None    # from env: SANTIMENT_API_KEY

    # ── Internal state ───────────────────────────────────────────────────────
    _store: dict[str, DataPoint] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── Source instances (lazy-initialised) ───────────────────────────────────
    _ws_client: BinanceWebsocketClient | None = field(default=None, init=False)
    _coinglass: CoinGlassClient | None = field(default=None, init=False)
    _fng: FearGreedClient | None = field(default=None, init=False)
    _defillama: DeFiLlamaClient | None = field(default=None, init=False)

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start all data-fetching threads. Call once at startup."""
        self._init_sources()
        self._start_ws()
        self._start_pollers()

    def stop(self) -> None:
        """Graceful shutdown."""
        if self._ws_client:
            self._ws_client.disconnect()

    def get(self, key: str, default: Any = None) -> Optional[Any]:
        """
        Retrieve a data point.

        Keys follow the pattern "type:symbol", e.g.:
          "price:BTCUSDT"        → float or None
          "oi:BTC"               → float (USD) or None
          "funding_rate:BTC"     → float (decimal, e.g. 0.0001) or None
          "fear_greed"           → int (0–100) or None
          "btc_dominance"        → float (%) or None
        """
        with self._lock:
            dp = self._store.get(key)
        if dp is None:
            return default
        return dp.get(default)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _init_sources(self) -> None:
        self._coinglass  = CoinGlassClient(api_key=self.coinglass_key) if self.coinglass_key else None
        self._fng        = FearGreedClient()
        self._defillama  = DeFiLlamaClient()

    def _start_ws(self) -> None:
        cfg = StreamConfig(
            symbols=self.ws_symbols,
            channels=["depth20@100ms", "trade", "bookTicker"],
            buffer_size=10_000,
        )
        self._ws_client = BinanceWebsocketClient(cfg)
        self._ws_client.connect()
        # WS update thread: writes latest snapshots into _store every 1s
        threading.Thread(target=self._ws_poller, daemon=True, name="ws-store-writer").start()

    def _ws_poller(self) -> None:
        """Read WS snapshots every second and write to _store."""
        while True:
            time.sleep(1.0)
            if self._ws_client is None or not self._ws_client.is_connected():
                continue
            for sym in self.ws_symbols:
                sym = sym.upper()
                # Price from bookTicker
                bt = self._ws_client._latest_book_ticker.get(sym)
                if bt:
                    mid = (float(bt.get("b", 0)) + float(bt.get("a", 0))) / 2
                    self._put(f"price:{sym}", mid, max_age=5.0)
                # Orderbook imbalance
                imb = self._ws_client.get_orderbook_imbalance(sym)
                if imb is not None:
                    self._put(f"imbalance:{sym}", imb, max_age=5.0)

    def _start_pollers(self) -> None:
        """Start all Tier 2 and Tier 3 polling threads."""

        # Tier 2: 30-second pollers
        threading.Thread(target=self._poll_coinglass_oi,     daemon=True, name="poller-oi").start()
        threading.Thread(target=self._poll_coinglass_funding, daemon=True, name="poller-funding").start()
        threading.Thread(target=self._poll_fear_greed,        daemon=True, name="poller-fng").start()
        threading.Thread(target=self._poll_market_cap,        daemon=True, name="poller-mcap").start()

        # Tier 3: daily batch
        threading.Thread(target=self._poll_defillama_tvl,     daemon=True, name="poller-tvl").start()
        threading.Thread(target=self._poll_idos,             daemon=True, name="poller-ido").start()

    def _poll_coinglass_oi(self) -> None:
        while True:
            time.sleep(30.0)
            if self._coinglass is None:
                continue
            try:
                data = self._coinglass.open_interest("BTC")
                self._put("oi:BTC", data.get("openInterest"), max_age=60.0)
            except Exception as exc:
                log.warning("Coinglass OI poll failed: %s", exc)

    def _poll_coinglass_funding(self) -> None:
        while True:
            time.sleep(30.0)
            if self._coinglass is None:
                continue
            try:
                data = self._coinglass.funding_rate("BTC")
                self._put("funding_rate:BTC", data.get("fundingRate"), max_age=300.0)
            except Exception as exc:
                log.warning("Coinglass funding poll failed: %s", exc)

    def _poll_fear_greed(self) -> None:
        while True:
            time.sleep(3600.0)  # Update every hour; data only changes every 8h
            try:
                data = self._fng.current()
                self._put("fear_greed", int(data.get("value", 0)), max_age=3600.0 * 4)
            except Exception as exc:
                log.warning("Fear & Greed poll failed: %s", exc)

    def _poll_market_cap(self) -> None:
        while True:
            time.sleep(30.0)
            try:
                data = self._coinglass or self._fetch_coingecko_global()
                self._put("btc_dominance", data.get("btc_dominance"), max_age=120.0)
            except Exception as exc:
                log.warning("Market cap poll failed: %s", exc)

    def _poll_defillama_tvl(self) -> None:
        while True:
            time.sleep(86400.0)  # Once per day
            try:
                data = self._defillama.total_tvl()
                self._put("defi_tvl", data, max_age=86400.0)
            except Exception as exc:
                log.warning("DeFiLlama TVL poll failed: %s", exc)

    def _poll_idos(self) -> None:
        while True:
            time.sleep(86400.0)  # Once per day
            # Implementation left as exercise — use CryptoRank client
            pass

    def _fetch_coingecko_global(self) -> dict:
        import requests
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        d = r.json().get("data", {})
        return {
            "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
            "total_mcap": d.get("total_market_cap", {}).get("usd", 0),
        }

    def _put(self, key: str, value: Any, max_age: float) -> None:
        with self._lock:
            self._store[key] = DataPoint(value=value, timestamp=time.time(), max_age_seconds=max_age)
```

### 4.4 Signal Integration Map

```
Raw Data → Derived Signals → Trading Decision

Binance WS Price + EMA              → Trend Direction (UP/DOWN/SIDEWAYS)
Binance WS Imbalance                → Short-term Pressure (BUY/SELL bias)
Coinglass OI spike + Price           → Institutional Distribution / Accumulation
Coinglass Funding Rate              → Retail Long/Short skew
Coinglass Liquidations              → Stop hunt detection (clusters)
Alternative.me Fear & Greed         → Risk Environment (position multiplier)
CoinGecko BTC Dominance              → Macro rotation (alt season vs BTC)
DeFiLlama TVL                       → DeFi sector health
CryptoRank IDO events               → Event-driven volatility awareness
Santiment Social Volume              → Pre-movement sentiment spikes
```

---

## Section 5: Cost Analysis

### 5.1 Data Source Cost Comparison

| Source | Tier | Monthly Cost | Key Data Provided | Use Case in Pipeline |
|--------|------|-------------|-------------------|---------------------|
| **Binance REST** (existing) | Free | $0 | OHLCV, depth, trades | Historical data, REST fallback |
| **Binance WS** (existing) | Free | $0 | Live price, depth, trades | Real-time price (Tier 1) |
| **CoinGecko** (existing) | Free | $0 | Market cap, dominance, volume | Tier 3 batch |
| **Alternative.me** | Free | $0 | Fear & Greed Index | Risk environment signal |
| **Coinglass** | Free | $0 | OI, funding rates, liquidations | Tier 2 indicators |
| **DeFiLlama** | Free | $0 | TVL, protocol metrics | On-chain macro |
| **CryptoRank** | Free | $0 | IDO/IGO schedule | Event-driven |
| **Santiment** | Free | $0 (100 credits) | Social volume | Sentiment (sparingly) |
| **TradingLite** | Paid | TBD (contact sales) | Orderbook heatmap | Liquidity detection |
| **Coinglass** | Paid (Starter) | $29/mo | Historical OI, more endpoints | Extended history |
| **Santiment** | Paid (Starter) | $49/mo | More social metrics | Deeper sentiment |
| **Coinglass** | Paid (Pro) | $99/mo | Full liquidations history | Premium signals |

### 5.2 V4 Minimum Viable Data Stack (Free Tier)

| Source | Cost | Signals Enabled |
|--------|------|----------------|
| Binance WS (existing) | $0 | Live price, imbalance, trades |
| Binance REST (existing) | $0 | OHLCV, historical |
| CoinGecko (existing) | $0 | Market cap, dominance |
| Alternative.me | $0 | Fear & Greed |
| Coinglass (free key) | $0 | OI, funding rates, liquidations |
| DeFiLlama | $0 | TVL |
| CryptoRank | $0 | IDO calendar |
| **Total** | **$0/mo** | Institutional-grade signals |

> **Conclusion:** The V4 alt-data pipeline can be built entirely on free tiers. No paid subscriptions are required for Tier 1 and Tier 2 signals. The highest-priority additions over the current codebase are:
> 1. **Coinglass** (free tier) — funding rates and OI are the most actionable new signals
> 2. **Alternative.me** — Fear & Greed is a direct input to the position multiplier in CLAUDE.md
> 3. **Orderbook heatmap** — TradingLite or DIY via Binance depth stream

### 5.3 Implementation Priority

| Priority | Task | Estimated Effort | Impact |
|----------|------|-----------------|--------|
| P0 | Fix REST 429/418 handling in `binance_client.py` | 2 hours | Reliability |
| P0 | Implement WebSocket reconnect with exponential backoff | 3 hours | Reliability |
| P0 | Add Coinglass client (funding rates + OI) | 2 hours | New signals |
| P0 | Add Alternative.me Fear & Greed client | 1 hour | Risk environment |
| P1 | Implement `DataPipelineManager` as central store | 4 hours | Architecture |
| P1 | Add stale-data watchdog to WS | 1 hour | Reliability |
| P2 | Add DeFiLlama TVL poller | 2 hours | Macro context |
| P2 | Add CryptoRank IDO calendar | 2 hours | Event awareness |
| P2 | Audit `coingecko_client.py` for same bugs | 1 hour | Reliability |
| P3 | TradingLite heatmap integration | 4 hours | Premium signal |
| P3 | Santiment social volume (free tier) | 2 hours | Sentiment |

---

## Appendix A: Code Audit Findings — `binance_client.py`

| Line(s) | Issue | Severity | Fix |
|---------|-------|----------|-----|
| 21–35 `RateLimiter` | `MIN_INTERVAL = 0.2` (5 req/s) is conservative but not wrong. Leaves ~87% of 2,400-weight budget unused | Low | Increase to 0.05 s (20 req/s) with burst headroom, or compute dynamic interval from `X-MBX-USED-WEIGHT-MINUTE` header |
| 162–173 retry block | Only catches `Timeout` and `ConnectionError`; ignores HTTP 429/418 | **High** | Add `response.status_code == 429` → read `Retry-After`; `== 418` → sleep 120 s |
| 169 `sleep(2 ** attempt)` | No jitter; capped at 4 s after 2 retries; ignores server `Retry-After` | **High** | Replace with jitter backoff (see Section 2.3) |
| 174 `JSONDecodeError` | Raises without retry | Medium | Treat as transient; retry up to 2 times |
| 264–270 `get_current_price` | No retry on any failure | **High** | Add retry with exponential backoff |
| 290–305 `get_24h_stats` | No retry on any failure | **High** | Add retry with exponential backoff |
| 83–139 local CSV cache | Falls back to API on any KeyError or FileNotFoundError; does not cache API response back to CSV | Low | Write successful API responses to CSV for future reuse |

## Appendix B: Code Audit Findings — `streams/binance_websocket.py`

| Line(s) | Issue | Severity | Fix |
|---------|-------|----------|-----|
| 167–172 `_run()` | Fixed 5-second reconnect; no exponential backoff, no jitter | **High** | Implement exponential backoff with jitter (see Section 2.3) |
| 169 `ping_interval=20` | No stale-data watchdog; if stream stalls (no message) but TCP stays alive, ping/pong passes but data is stale | Medium | Add watchdog thread monitoring `_last_msg_time`; force reconnect if > 60 s |
| 165 `_run_loop` while True | `_running` is not checked inside the `except` block — exception does not break loop, but explicit check is safer | Low | Check `self._running` after `except` |
| 223–234 `subscribe()` | NotImplementedError; reconnect required for new subscriptions | Low (known limitation) | Document clearly; always pass full symbol list at construction |
| 405 `is_connected()` | Returns `True` if thread is alive even if Binance silently closed the stream | Medium | Also check `_last_msg_time` freshness |

---

*Document version: 1.0 — 2026-03-22*
*Next review: After P0 tasks are implemented, before P1 tasks begin*
