"""
LLM Client for market analysis.
"""

import hashlib
import json
import os
import random
import re
import requests
import time
import warnings
from typing import Optional, List, Dict


# Default settings - can be overridden via environment variables
DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimaxi.com/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "MiniMax-M2.7")


class LLMClient:
    """Client for LLM API (MiniMax)."""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None, cache_ttl: int = 300):
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "LLM_API_KEY environment variable is not set. "
                "Please set it before using the LLM client."
            )
        self.base_url = base_url or DEFAULT_BASE_URL
        self.model = model or DEFAULT_MODEL
        self._response_cache: dict = {}
        self._cache_ttl = cache_ttl

    def _compute_cache_key(self, messages: List[Dict], temperature: float) -> str:
        content = "".join(m.get("content", "") for m in messages)
        return hashlib.md5(f"{content}:{temperature}".encode()).hexdigest()

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_retries: int = 3,
    ) -> str:
        """Send chat request with exponential backoff for rate limits/errors."""
        # Check cache first
        cache_key = self._compute_cache_key(messages, temperature)
        if cache_key in self._response_cache:
            cached = self._response_cache[cache_key]
            if time.time() - cached['timestamp'] < self._cache_ttl:
                return cached['response']

        url = f"{self.base_url}/text/chatcompletion_v2"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        for attempt in range(max_retries):
            try:
                # SSL is verified by default (verify=True). Add verify="/path/to/cert.pem"
                # for self-signed certs if needed.
                response = requests.post(url, json=payload, headers=headers, timeout=60)

                # P0-FIX: handle HTTP errors with appropriate backoff
                if response.status_code == 429:
                    # Honour Retry-After header; minimum 60s for LLM providers
                    retry_after = int(response.headers.get("Retry-After", 60))
                    if attempt < max_retries - 1:
                        time.sleep(retry_after * (0.5 + random.random()))
                        continue
                elif response.status_code >= 500:
                    # Server errors: scaled exponential backoff
                    if attempt < max_retries - 1:
                        time.sleep((2 ** attempt) * 5 * (0.5 + random.random()))
                        continue

                response.raise_for_status()  # unified: any non-200 raises
                data = response.json()

                if "choices" in data and len(data["choices"]) > 0:
                    result = data["choices"][0]["message"]["content"]
                    # Cache the result
                    self._response_cache[cache_key] = {'response': result, 'timestamp': time.time()}
                    return result
                elif "base_resp" in data:
                    raise Exception(f"API Error: {data['base_resp']['status_msg']}")
                else:
                    raise Exception(f"Unexpected response: {data}")

            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) * 5)  # scale up for network errors too
                    continue
                raise Exception(f"Network error after {max_retries} attempts: {e}")

        raise Exception("Max retries exceeded")

    async def async_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_retries: int = 3,
    ) -> str:
        """Async version of chat using aiohttp."""
        import aiohttp
        import asyncio

        url = f"{self.base_url}/text/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "messages": messages, "temperature": temperature}

        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, headers=headers, timeout=60) as response:
                        if response.status == 429:
                            retry_after = int(response.headers.get("Retry-After", 60))
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_after * (0.5 + random.random()))
                                continue
                        elif response.status >= 500:
                            if attempt < max_retries - 1:
                                await asyncio.sleep((2 ** attempt) * 5 * (0.5 + random.random()))
                                continue
                        response.raise_for_status()
                        data = await response.json()
                        if "choices" in data and len(data["choices"]) > 0:
                            return data["choices"][0]["message"]["content"]
                        elif "base_resp" in data:
                            raise Exception(f"API Error: {data['base_resp']['status_msg']}")
                        else:
                            raise Exception(f"Unexpected response: {data}")
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep((2 ** attempt) * 5)
                    continue
                raise Exception(f"Network error after {max_retries} attempts: {e}")
        raise Exception("Max retries exceeded")


# Default client - requires LLM_API_KEY environment variable to be set
llm_client = LLMClient()


# ─── VALID ENUMS (enforced by prompt + parser) ────────────────────────────────

VALID_MACRO_REGIME = {"RISK_ON", "RISK_OFF", "NEUTRAL"}
VALID_BTC_STRENGTH = {"STRONG", "WEAK", "NEUTRAL"}
VALID_VOLATILITY  = {"HIGH", "LOW"}

# ─── FALLBACK (returned on parse/network error) ──────────────────────────────

FALLBACK_CONTEXT = {
    "macro_regime":     "NEUTRAL",
    "btc_strength":     "NEUTRAL",
    "volatility_warning": "LOW",
    "_error":          None,
    "_block_reason":   None,
}


def _norm(v: str, valid_set: set = None) -> str:
    """Normalize a value and validate it against an optional set; return safe fallback if invalid."""
    v = (v or "").strip().upper()
    if valid_set and v not in valid_set:
        return "NEUTRAL"  # safe fallback
    return v


def _parse_macro_response(raw: str) -> dict:
    """
    Extract and validate the strict JSON schema from LLM raw text.

    Schema: {"macro_regime": "...", "btc_strength": "...", "volatility_warning": "..."}
    """
    start = raw.find('{')
    end = raw.rfind('}')
    
    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in LLM response")

    parsed = json.loads(raw[start:end+1])

    macro_regime = _norm(parsed.get("macro_regime", ""), VALID_MACRO_REGIME)
    btc_strength = _norm(parsed.get("btc_strength", ""), VALID_BTC_STRENGTH)
    volatility   = _norm(parsed.get("volatility_warning", ""), VALID_VOLATILITY)

    return {
        "macro_regime":       macro_regime,
        "btc_strength":       btc_strength,
        "volatility_warning": volatility,
        "_error":            None,
        "_block_reason":     None,
    }


# ─── Plutus V2: Macro Risk Officer ──────────────────────────────────────────

def get_llm_macro_context(
    btc_analysis: dict,
    target_symbol: str,
    target_analysis: dict,
    market_overview: dict,
    provider: str = "minimax",
) -> dict:
    """
    Plutus V2 — The LLM is promoted from Trade Signal Generator to
    Macro Risk Officer. It returns ONLY macro context; trade decisions
    are made by the mathematical rules in WorkflowStrategy.

    Returns:
        {{
            "macro_regime":       "RISK_ON" | "RISK_OFF" | "NEUTRAL",
            "btc_strength":       "STRONG"  | "WEAK"    | "NEUTRAL",
            "volatility_warning": "HIGH"    | "LOW",
            "_error":             str | None,   # network/parse error message
            "_block_reason":      str | None,   # non-blocking advisory note
        }}

    Enforced JSON schema — no BUY/SELL/Stop-loss output permitted.
    """
    # Build BTC section
    btc_trend   = btc_analysis.get("trend", "N/A") if btc_analysis else "N/A"
    btc_rsi     = btc_analysis.get("rsi", 0)       if btc_analysis else 0
    btc_price   = btc_analysis.get("current_price", 0) if btc_analysis else 0
    btc_support = btc_analysis.get("support", 0)    if btc_analysis else 0
    btc_resist  = btc_analysis.get("resistance", 0) if btc_analysis else 0

    # Build target section
    tgt_trend   = target_analysis.get("trend", "N/A")       if target_analysis else "N/A"
    tgt_rsi     = target_analysis.get("rsi", 0)              if target_analysis else 0
    tgt_price   = target_analysis.get("current_price", 0)    if target_analysis else 0

    # Fear & Greed
    fg_raw = market_overview.get("fear_greed_index", "NA") if market_overview else "NA"
    fg_val: Optional[int] = None
    if isinstance(fg_raw, int):
        fg_val = fg_raw
    elif isinstance(fg_raw, str):
        try:
            fg_val = int(fg_raw.strip())
        except Exception:
            fg_val = None

    if isinstance(fg_val, int):
        if fg_val <= 25:
            fg_desc = f"Extreme Fear ({fg_val})"
        elif fg_val >= 75:
            fg_desc = f"Extreme Greed ({fg_val})"
        else:
            fg_desc = f"Neutral ({fg_val})"
    else:
        fg_desc = "NA"

    # Market cap, dominance
    total_cap  = market_overview.get("total_market_cap", 0) if market_overview else 0
    btc_dom    = market_overview.get("btc_dominance", 0)    if market_overview else 0
    volume_24h = market_overview.get("volume_24h", 0)       if market_overview else 0

    prompt = f"""You are the Macro Risk Officer for a systematic crypto hedge fund.

Your ONLY job is to assess the macro environment. Do NOT output trade signals (BUY/SELL/Stop-Loss/TP). Do NOT calculate position sizes. Your output is consumed by an automated Execution Gate — be precise and concise.

You MUST respond with ONLY a single valid JSON object. No markdown, no explanation, no preamble. The JSON must match this exact schema:

{{"macro_regime": "<RISK_ON|RISK_OFF|NEUTRAL>", "btc_strength": "<STRONG|WEAK|NEUTRAL>", "volatility_warning": "<HIGH|LOW>"}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GUIDANCE FOR EACH FIELD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

macro_regime — overall market risk appetite:
  RISK_ON   = Fear & Greed >= 55, risk assets outperforming, no major macro headwinds, crypto sentiment positive
  RISK_OFF  = Fear & Greed <= 35, DXY strengthening, equity markets selling off, geopolitical tension, crypto sentiment negative
  NEUTRAL   = Everything in between

btc_strength — BTC's trend and relative dominance:
  STRONG    = BTC in clear uptrend (EMA50 > EMA200), BTC dominance stable or rising, price above key EMAs
  WEAK      = BTC in downtrend (EMA50 < EMA200), BTC dominance falling, price below key EMAs, liquidity grabs evident
  NEUTRAL   = BTC ranging, mixed signals

volatility_warning — current market volatility regime:
  HIGH      = ATR elevated vs 20-bar average (>1.5x), Fear & Greed at extremes (<25 or >80), major news events (FOMC/CPI/war/regulation) within 24-48h, unusual volume spikes
  LOW       = Normal volatility environment, no extreme Fear & Greed, no scheduled high-impact events

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BTC Analysis:
- Price:   ${btc_price:,.2f}
- Trend:   {btc_trend}
- RSI(14): {btc_rsi:.1f}
- Support: ${btc_support:,.2f}
- Resist:  ${btc_resist:,.2f}

Target Symbol ({target_symbol}):
- Price:   ${tgt_price:,.2f}
- Trend:   {tgt_trend}
- RSI(14): {tgt_rsi:.1f}

Market Overview:
- Fear & Greed: {fg_desc}
- Total Market Cap: ${total_cap/1e12:.2f}T
- BTC Dominance: {btc_dom:.1f}%
- 24h Volume: ${volume_24h/1e9:.1f}B

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output ONLY the JSON object. Nothing else. Example valid output:
{{"macro_regime": "RISK_ON", "btc_strength": "STRONG", "volatility_warning": "LOW"}}
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response = llm_client.chat(messages)
        return _parse_macro_response(response)
    except (json.JSONDecodeError, ValueError) as e:
        # P0-FIX: _parse_macro_response raises ValueError for missing braces;
        # catches both error types and safely falls through to FALLBACK_CONTEXT.
        # Also removed unsafe reference to 'response' variable in exception scope.
        result = dict(FALLBACK_CONTEXT)
        result["_error"] = f"Parse error: {type(e).__name__}: {e}"
        return result
    except Exception as e:
        result = dict(FALLBACK_CONTEXT)
        result["_error"] = f"LLM call failed: {e}"
        return result


# ─── Legacy: Trade Signal Generator (kept for llm_strategy.py compat) ─────────

def analyze_market(
    btc_data: dict,
    target_data: dict,
    market_overview: dict,
    multi_tf: dict = None,
    volume_zone: str = "MID_RANGE",
) -> dict:
    """
    [LEGACY] Use LLM to analyze market and generate trading decision.
    Includes multi-timeframe analysis and volume profile.

    NOTE: This function is DEPRECATED for Plutus V2. The LLM should
    only output macro context via get_llm_macro_context().
    Kept here for backward compatibility with llm_strategy.py.
    """
    warnings.warn(
        "analyze_market() is deprecated. "
        "Use get_llm_macro_context() + WorkflowAnalyzer instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Build multi-timeframe section
    tf_section = ""
    if multi_tf:
        for tf, data in multi_tf.items():
            tf_section += f"""
{tf.upper()} Timeframe:
- Trend: {data.get('trend', 'N/A')}
- Price: ${data.get('current', 0):,.2f}
- EMA50: ${data.get('ema50', 0):,.2f}
- EMA200: ${data.get('ema200', 0):,.2f}
- RSI: {data.get('rsi', 0):.1f}
- Support: ${data.get('support', 0):,.2f}
- Resistance: ${data.get('resistance', 0):,.2f}
"""

    # Determine risk state based on Fear & Greed
    fg_raw = market_overview.get("fear_greed_index", "NA")
    fg_val = None
    if isinstance(fg_raw, int):
        fg_val = fg_raw
    elif isinstance(fg_raw, float) and fg_raw.is_integer():
        fg_val = int(fg_raw)
    elif isinstance(fg_raw, str):
        try:
            fg_val = int(fg_raw.strip())
        except Exception:
            fg_val = None

    if isinstance(fg_val, int):
        if fg_val <= 25:
            risk_state = "risk_off"
            fg_desc = f"Extreme Fear ({fg_val})"
        elif fg_val >= 75:
            risk_state = "risk_on"
            fg_desc = f"Extreme Greed ({fg_val})"
        else:
            risk_state = "neutral"
            fg_desc = f"Neutral ({fg_val})"
    else:
        risk_state = "neutral"
        fg_desc = "NA"

    prompt = f"""You are a professional crypto trader. Analyze the current market and give a trading decision.

EXPLICIT TRADING RULES (follow exactly):

RISK_STATE: {risk_state} (Fear & Greed: {fg_desc})

EXPLICIT PERMISSION MATRIX:
- If risk_on or neutral: ALL directions allowed (BTC/ETH/ALT longs & shorts)
- If risk_off (Fear <= 25):
  - BTC: SHORT allowed, LONG allowed if price near support AND RSI oversold (<35)
  - ETH/ALT: SHORT allowed only if BTC trend = down AND correlation high
  - ETH/ALT: LONG forbidden in risk_off

TIERED ASSET PRIORITY (BTC > ETH > ALT):
- Always check BTC first
- If BTC has good setup, trade BTC
- Only trade alts if BTC has no setup AND BTC trend is up (risk_on)

STOP LOSS RULES (CRITICAL):
- Valid stop distance: 0.5% <= stop_distance <= 2.5%
- Stop MUST be at NEAREST valid level (recent swing low for longs, recent swing high for shorts)
- NEVER use far-away resistance/support as stop anchor
- If no valid stop in 0.5-2.5% range → NO_TRADE

RR REQUIREMENT:
- Minimum RR: 1.5
- Maximum stop distance: 2.5%

BTC Analysis (1h):
- Trend: {btc_data.get('trend', 'N/A')}
- Price: ${btc_data.get('current_price', 0):,.2f}
- RSI: {btc_data.get('rsi', 0):.1f}
- Signal: {btc_data.get('signal', 'N/A')}
- Support: ${btc_data.get('support', 0):,.2f}
- Resistance: ${btc_data.get('resistance', 0):,.2f}

TARGET SYMBOL Analysis (what you are deciding to trade):
- Trend: {target_data.get('trend', 'N/A')}
- Price: ${target_data.get('current_price', 0):,.2f}
- RSI: {target_data.get('rsi', 0):.1f}
- Support: ${target_data.get('support', 0):,.2f}
- Resistance: ${target_data.get('resistance', 0):,.2f}

Multi-Timeframe Analysis (for TARGET SYMBOL):
{tf_section}

Volume Profile:
- Current price is in: {volume_zone} (HVN = high volume node / LVN = low volume node)
- Current price distance to high: {multi_tf.get('1h', {}).get('current', 0) / max(multi_tf.get('1h', {}).get('resistance', 1), 1) * 100 - 100 if multi_tf.get('1h', {}).get('resistance') else 0:.1f}% from resistance
- Current price distance to low: {100 - multi_tf.get('1h', {}).get('current', 0) / max(multi_tf.get('1h', {}).get('support', 1), 1) * 100 if multi_tf.get('1h', {}).get('support') else 0:.1f}% from support
- Status: {"Near resistance (HVN)" if multi_tf.get('1h', {}).get('current', 0) > multi_tf.get('1h', {}).get('resistance', 0) * 0.995 else "Near support (LVN)" if multi_tf.get('1h', {}).get('current', 0) < multi_tf.get('1h', {}).get('support', 0) * 1.005 else "Mid-range"}

Market:
- Fear & Greed: {fg_desc}

Decision Process:
1. What is the risk state? (risk_on / risk_off / neutral)
2. Based on permission matrix, what directions are ALLOWED for this symbol?
3. Among allowed directions, is there a valid setup with:
   - Stop distance 0.5% - 2.5%
   - RR >= 1.5
   - Nearest swing level for invalidation (NOT far resistance/support)
4. Pick the BEST valid setup if multiple exist, otherwise NO_TRADE

Respond in JSON format:
{{"decision": "BUY/SELL/NO_TRADE",
 "symbol": "BTC/ETH/ALT/NONE",
 "order_type": "MARKET" or "LIMIT",
 "limit_price": null or number (if LIMIT),
 "invalidation": number (price that closes below/above = WRONG),
 "stop_loss": number (MUST be 0.5-2.5% from entry),
 "take_profit": number (MUST give RR >= 1.5),
 "risk_level": "LOW/MODERATE/HIGH",
 "reason": "short reasoning..."}}"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response = llm_client.chat(messages)
        # Parse JSON from response
        start = response.find('{')
        end = response.rfind('}')
        if start == -1 or end == -1 or end < start:
            return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": "LLM response parse error"}
        parsed = json.loads(response[start:end+1])

        # Validate required fields
        if parsed.get("decision") not in {"BUY", "SELL", "NO_TRADE"}:
            return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": "Invalid decision from LLM"}
        stop_loss = parsed.get("stop_loss")
        if stop_loss is not None and (not isinstance(stop_loss, (int, float)) or stop_loss <= 0):
            return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": "Invalid stop_loss from LLM"}

        return parsed
    except (requests.exceptions.RequestException, json.JSONDecodeError, TypeError, ValueError) as e:
        return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": f"LLM error: {type(e).__name__}: {e}"}
