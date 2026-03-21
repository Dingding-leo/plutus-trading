"""
LLM Client for market analysis.
"""

import os
import requests
from typing import Optional, List, Dict


# Default settings - can be overridden via environment variables
DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.minimaxi.com/v1")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "MiniMax-M2.5")


class LLMClient:
    """Client for LLM API (MiniMax)."""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = base_url or DEFAULT_BASE_URL
        self.model = model or DEFAULT_MODEL

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
    ) -> str:
        """Send chat request."""
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

        response = requests.post(url, json=payload, headers=headers, timeout=60)
        data = response.json()

        if "choices" in data and len(data["choices"]) > 0:
            return data["choices"][0]["message"]["content"]
        elif "base_resp" in data:
            raise Exception(f"API Error: {data['base_resp']['status_msg']}")
        else:
            raise Exception(f"Unexpected response: {data}")


# Default client - requires LLM_API_KEY environment variable to be set
llm_client = LLMClient()


def analyze_market(
    btc_data: dict,
    target_data: dict,
    market_overview: dict,
    multi_tf: dict = None,
    volume_zone: str = "MID_RANGE",
) -> dict:
    """
    Use LLM to analyze market and generate trading decision.
    Includes multi-timeframe analysis and volume profile.
    """
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
        import json
        import re

        # Find JSON in response
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            return json.loads(match.group())
        else:
            return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": "LLM response parse error"}
    except Exception as e:
        return {"decision": "NO_TRADE", "symbol": "NONE", "risk_level": "MODERATE", "reason": f"LLM error: {str(e)}"}
