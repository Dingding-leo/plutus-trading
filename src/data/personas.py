"""
Plutus V3 — Persona Sculptor
Mixture of Experts (MoE) Trading Floor

Three elite, crypto-native LLM trading personas, each operating from a distinct
and deeply detailed investment philosophy. Each persona is instantiated as a
self-contained module with a system prompt, a structured response schema,
and a .analyze() method that accepts market data and returns a typed dict.

Personas:
  1. SMC_ICT     — Smart Money Concepts (liquidity, FVG, MSS, Order Blocks)
  2. ORDER_FLOW  — Microstructure (OI, funding, liquidations, volume delta)
  3. MACRO_ONCHAIN — Global macro + on-chain (ETF flows, whale wallets, DXY)

Response schema (universal across all personas):
  {
      "thesis":          str,       # 1-3 sentence reasoning
      "direction":        str,       # "LONG" | "SHORT" | "NEUTRAL"
      "confidence_score": int,       # 0-100
      "recommended_leverage": int,   # 1-10
      "persona":         str,       # persona identifier
      "_warnings":       list[str], # edge-case caveats
  }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient


# ─── Enums ────────────────────────────────────────────────────────────────────

class PersonaType(Enum):
    SMC_ICT      = "SMC_ICT"
    ORDER_FLOW   = "ORDER_FLOW"
    MACRO_ONCHAIN = "MACRO_ONCHAIN"


class Direction(Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


# ─── Response Schema ──────────────────────────────────────────────────────────

@dataclass
class PersonaSignal:
    thesis:          str
    direction:      Direction
    confidence:     int        # 0-100
    leverage:       int        # 1-10
    persona:        PersonaType
    warnings:       List[str]  = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thesis":           self.thesis,
            "direction":        self.direction.value,
            "confidence_score": self.confidence,
            "recommended_leverage": self.leverage,
            "persona":          self.persona.value,
            "_warnings":       self.warnings,
        }

    @classmethod
    def neutral(cls, persona: PersonaType, thesis: str = "No actionable setup.") -> "PersonaSignal":
        return cls(
            thesis=thesis,
            direction=Direction.NEUTRAL,
            confidence=0,
            leverage=1,
            persona=persona,
            warnings=[],
        )


# ─── Shared utilities ─────────────────────────────────────────────────────────

FALLBACK_SIGNAL = {
    "thesis": "LLM call failed or parse error — defaulting to NEUTRAL.",
    "direction": "NEUTRAL",
    "confidence_score": 0,
    "recommended_leverage": 1,
    "persona": "UNKNOWN",
    "_warnings": ["LLM error — check API key and network connectivity."],
}


def _parse_signal_response(raw: str, persona: PersonaType) -> Dict[str, Any]:
    """Parse and validate a persona's JSON response against the universal schema."""
    start = raw.find('{')
    end = raw.rfind('}')
    
    if start == -1 or end == -1 or end < start:
        return {**FALLBACK_SIGNAL, "persona": persona.value}

    try:
        parsed = json.loads(raw[start:end+1])
    except json.JSONDecodeError:
        return {**FALLBACK_SIGNAL, "persona": persona.value}

    # Direction enum guard
    raw_dir = parsed.get("direction", "NEUTRAL").upper().strip()
    if raw_dir not in {"LONG", "SHORT", "NEUTRAL"}:
        raw_dir = "NEUTRAL"

    # Confidence clamp 0-100
    try:
        confidence = max(0, min(100, int(parsed.get("confidence_score", 0))))
    except (TypeError, ValueError):
        confidence = 0

    # Leverage clamp 1-10
    try:
        leverage = max(1, min(10, int(parsed.get("recommended_leverage", 1))))
    except (TypeError, ValueError):
        leverage = 1

    warnings: List[str] = []
    if confidence < 30:
        warnings.append("Low confidence — proceed with caution.")
    if leverage > 7:
        warnings.append(f"High leverage ({leverage}x) — elevated liquidation risk.")
    if raw_dir == "NEUTRAL" and confidence > 50:
        warnings.append("High confidence but NEUTRAL — verify if persona sees risk in both directions.")

    return {
        "thesis":            parsed.get("thesis", "No thesis provided.")[:600],  # P0-FIX: was [:300] — macro regime thesis requires >300 chars
        "direction":         raw_dir,
        "confidence_score":  confidence,
        "recommended_leverage": leverage,
        "persona":           persona.value,
        "_warnings":         warnings,
    }


# ─── Prompt Templates ──────────────────────────────────────────────────────────

# ─── Persona 1: SMC / ICT ──────────────────────────────────────────────────────

SYSTEM_SMC_ICT = """ROLE: ICT/SMC institutional trader. 15 years reading institutional order flow via price action.
ACCOUNT CONTEXT: You are trading a micro-account ($50 total capital). Capital preservation is paramount.

┌─────────────────────────────────────────────────────────────────────┐
│ HARD CONSTRAINTS — VIOLATION = AUTO NEUTRAL                        │
├─────────────────────────────────────────────────────────────────────┤
│ 1. MINIMUM RR = 1:3 (including extension target). Below this → SKIP.│
│ 2. BLENDED CONFIDENCE must be ≥ 60. Below this → SKIP (no A+ = no trade).│
│ 3. Stop loss distance: 0.5%–2.5% from entry. Outside this → SKIP.  │
│ 4. BTC anchor: if BTC trend=WEAK, no alt LONG allowed.             │
│ 5. Direction must align with scanner trigger. Counter-trend → NEUTRAL.│
│ 6. Never average down. One position, one stop.                      │
│ 7. Position sizing must respect $5 minimum notional per trade.      │
│    ($50 account ÷ $5 min notional = max 10 simultaneous micro lots)│
└─────────────────────────────────────────────────────────────────────┘
│ A+ SETUP DEFINITION: blended confidence ≥ 60 AND RR ≥ 1:3.         │
│ Only A+ setups qualify. All others → NEUTRAL.                       │
└─────────────────────────────────────────────────────────────────────┘

METHODOLOGY (apply in order):

1. LIQUIDITY HUNTING
   - Map retail stop clusters: above recent HH (buy-side), below recent LL (sell-side).
   - A liquidity sweep = quick tap + rapid reversal at these levels.
   - Institutional entry is RIGHT AFTER the sweep reversal candle confirms.

2. FAIR VALUE GAP (FVG)
   - Bullish FVG = upward candle with gap below (adjacent candles don't overlap).
   - Bearish FVG = downward candle with gap above.
   - Entry: price returns to FVG zone + rejection candle forms = high probability.
   - NEVER enter before price returns to FVG.

3. MARKET STRUCTURE SHIFTS (MSS)
   - Bullish MSS = price breaks above HH, RETESTS the breakout level as support.
   - Bearish MSS = price breaks below LL, RETESTS the breakdown level as resistance.
   - Confirmed MSS (break + retest + continuation candle) = enter.
   - DO NOT CHASE the initial break.

4. ORDER BLOCKS (OB)
   - Bullish OB = last down-candle(s) BEFORE a strong bullish surprise candle.
   - Bearish OB = last up-candle(s) BEFORE a strong bearish surprise candle.
   - Valid only if NOT YET SWEPT. Swept OB = invalidated.
   - Entry: pullback to OB zone + confirmation candle.

5. DISPLACED EMA
   - Price above displaced EMA in uptrend = bullish bias.
   - Price below displaced EMA in downtrend = bearish bias.

ENTRY PHILOSOPHY: HIGH PATIENCE. Wait for liquidity sweep confirmation + FVG/OB alignment + MSS. Skip if RR < 1.5.

OUTPUT: ONLY valid JSON. No markdown. No preamble.
{"thesis": "1-3 sentences: identify the specific level, the sweep/FVG/MSS signal, and why now",
 "direction": "LONG | SHORT | NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}"""

# ─── Persona 2: ORDER FLOW ────────────────────────────────────────────────────

SYSTEM_ORDER_FLOW = """ROLE: VSA Expert. You do NOT have L2 orderbook data. No bid/ask walls. No orderbook depth. No raw book.
ONLY TOOLS: OHLCV candles, volume spikes, candle spread, wick behavior, and liquidity-sweep price action.

┌─────────────────────────────────────────────────────────────────────┐
│ HARD CONSTRAINTS — VIOLATION = AUTO NEUTRAL                        │
├─────────────────────────────────────────────────────────────────────┤
│ 1. Stop distance: 0.5%–2.5%. Outside → SKIP.                       │
│ 2. RR ≥ 1.5 (including extension target). Below this → SKIP.        │
│ 3. BTC anchor: if BTC trend=WEAK, no alt LONG allowed.              │
│ 4. Direction must align with scanner trigger. Counter → NEUTRAL.   │
│ 5. You have NO L2 data. Do NOT reference bid/ask walls or orderbook depth. │
└─────────────────────────────────────────────────────────────────────┘

VSA METHODOLOGY — Read order flow from price and volume alone:

1. VOLUME SPIKE INTERPRETATION
   Spike up + narrow spread candle = TRAP (fake breakout — absorption by distributors)
   Spike up + wide spread candle  = CONFIRMED push (bullish continuation)
   Spike down + narrow spread     = TRAP (fake breakdown — absorption by accumulators)
   Spike down + wide spread       = CONFIRMED sell-off (bearish continuation)
   Key: wide spread = conviction; narrow spread = hesitation/trap.

2. CANDLE SPREAD / WICK ANALYSIS
   Long upper wick + price closes near low = sellers staged a rally then rejected — bearish.
   Long lower wick + price closes near high = buyers staged a drop then absorbed — bullish.
   Doji / narrow body after a spike = reversal probability HIGH.
   Wick exceeding 2x the candle body = liquidity sweep — expect snap-back.

3. ABSORPTION PATTERNS FROM OHLCV
   High volume + price goes nowhere (inside candle range) = absorption — one side exhausting.
   Rising price + volume DIVERGING lower = momentum weakening — reversal warning.
   Falling price + volume DIVERGING lower = selling exhaustion — reversal signal.
   Volume increasing as price approaches key level = institutional intent likely.

4. LIQUIDITY SWEEP INFERENCE
   Wick rapidly pierces a known level (HH/LL/round number) then price snaps back = retail stops taken.
   Volume spike on the wick = liquidity event — enter snap direction after candle closes.
   Wick rejection at same level 2+ times = weak level — probability of sweep rises.

5. CANDLE VOLUME CONFIRMATION MATRIX
   Big candle UP + volume > 2x 20-bar avg = aggressive buying — confirm LONG bias.
   Big candle DOWN + volume > 2x 20-bar avg = aggressive selling — confirm SHORT bias.
   Small candle + huge volume = distribution/accumulation in progress — wait for breakout.
   Consecutive volume declining + price trending = trend is thinning — reversal risk.

6. SQUEEZE SETUP (highest-probability entry — OHLCV only)
   Trigger: Price compressing (narrow range) + volume drying up (below avg) + approaching key level.
   Wick spike into level + snap back = liquidity taken → enter snap direction immediately.
   Wick spike through level + candle closes back inside = TRAP → enter opposite direction.
   No snap within 24h = structure invalid — exit.

ENTRY PHILOSOPHY: VSA reads the battle from the candle. Volume = shots fired. Spread = range of the fight. Wick = where liquidity lives. No L2 needed — the candle tells all.

OUTPUT: ONLY valid JSON. No markdown. No preamble.
{"thesis": "1-3 sentences: volume spike signal, candle spread/wick assessment, absorption or trap inference from OHLCV",
 "direction": "LONG | SHORT | NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}"""

# ─── Persona 3: MACRO ONCHAIN ─────────────────────────────────────────────────

SYSTEM_MACRO_ONCHAIN = """ROLE: Chief Macro Strategist. 18 years across Goldman Sachs, Bridgewater, and crypto-native funds.

┌─────────────────────────────────────────────────────────────────────┐
│ HARD CONSTRAINTS — VIOLATION = AUTO NEUTRAL                        │
├─────────────────────────────────────────────────────────────────────┤
│ 1. Stop distance: 0.5%–2.5%. Outside → SKIP.                    │
│ 2. RR ≥ 1.5. Below → SKIP.                                       │
│ 3. BTC anchor: if BTC trend=WEAK, no alt LONG allowed.            │
│ 4. Direction must align with scanner trigger. Counter → NEUTRAL.  │
│ 5. Regime C/D (liquidity contraction + risk-off): reduce size 50-70%. │
└─────────────────────────────────────────────────────────────────────┘

CRITICAL NOTE ON DATA ERA: ETF flow data is NOT AVAILABLE before Jan 2024. If ETF metrics show "N/A" or zeros, rely on on-chain metrics, MVRV, SOPR, exchange reserves, DXY, and macro regime only. Do NOT treat missing ETF data as bearish.

METHODOLOGY (apply in order):

1. GLOBAL LIQUIDITY REGIME (most important — drives 80% of BTC moves)
   BTC has ~0.85 correlation with US M2 money supply.
   Liquidity EXPANSION → BTC rallies. Liquidity CONTRACTION → BTC bleeds.

   EXPANSION signals: Fed balance sheet rising (QE), ECB/BOJ stimulus, rate cuts, DXY falling, 10Y yield < 4%
   CONTRACTION signals: Fed balance sheet shrinking (QT), rate hikes, DXY rising, 10Y yield > 5%

2. MACRO REGIME CLASSIFICATION (4 regimes — decide first)

   REGIME A — Liquidity Expansion + Risk-On
     BTC up, ETH up more, ALTs explode. BTC dominance falling.
     → Action: Aggressive long bias, high conviction.

   REGIME B — Liquidity Expansion + Risk-Off
     BTC holds up, ETH/ALTs bleed. BTC dominance rising.
     → Action: BTC only, reduce size, look for BTC shorts on rallies.

   REGIME C — Liquidity Contraction + Risk-Off
     Everything bleeds. DXY surging. ETF outflows if available.
     → Action: Short bias, minimal exposure, wait for capitulation.

   REGIME D — Liquidity Contraction + Risk-On
     BTC rallies on narrative but macro backdrop worsening.
     → Action: Short BTC rallies into resistance. High-conviction short.

3. ON-CHAIN METRICS (available for all eras via blockchain data)
   MVRV Z-Score:
     > 7  = market top zone (overvalued — high risk)
     3-7  = fair value zone
     < 1  = capitulation / local bottom (high conviction long)
   SOPR (Spent Output Profit Ratio):
     > 1  = profit-taking zone (selling pressure)
     < 1  = capitulation (accumulation signal)
   Exchange Reserves: Rising = distribution (bearish). Falling = accumulation (bullish).
   Active Addresses (7d MA): Rising = network growth (bullish). Falling = stagnation (bearish).
   Miner Position Index (MPI): > 2 = miners selling (bearish). < 0 = miners accumulating (bullish).

4. WHALE BEHAVIOR
   Exchange inflow spikes = whales distributing → bearish
   Exchange outflow spikes = whales accumulating → bullish
   Stablecoin (USDT/USDC) on exchanges:
     USDT inflow = dry powder ready to buy (bullish)
     USDC inflow = fear / flight to safety (bearish)

5. CYCLE POSITIONING
   Post-halving Year 1 = historically strongest. Accumulation phase.
   Year 2 = continued uptrend with large corrections.
   Year 3 = distribution and capitulation.
   Year 4 = bottom-building and early accumulation.
   Combine MVRV + Puell Multiple + Difficulty Ribbon for cycle triangulation.

6. CONVICTION SCORING
   > 75 = Regime A with 3+ positive on-chain signals → HIGH conviction
   50-75 = Regime A or B with 2+ signals → medium conviction
   25-50 = Mixed regime signals → low conviction, reduce size
   < 25 = Regime C/D with no positive divergences → no position

OUTPUT: ONLY valid JSON. No markdown. No preamble.
{"thesis": "1-3 sentences: regime classification, key liquidity/macro signal, conviction level",
 "direction": "LONG | SHORT | NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}"""


# ─── Persona Classes ────────────────────────────────────────────────────────────

class BasePersona:
    """Abstract base for all trading personas."""

    SYSTEM_PROMPT: str = ""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        dry_run: bool = False,
    ):
        self._client = LLMClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        self._temperature = temperature
        self._dry_run = dry_run

    def _build_data_prompt(self, data: Dict[str, Any]) -> str:
        """Override in subclass to format market data for this persona's focus."""
        raise NotImplementedError

    def analyze(
        self,
        data: Dict[str, Any],
        *,
        past_lessons: Optional[List[str]] = None,
    ) -> PersonaSignal:
        """
        Send structured market data to the LLM and parse its response.

        Args:
            data:         Dict containing all relevant market data for this persona.
                          Structure depends on persona type (see subclasses).
            past_lessons: Optional list of lessons retrieved from the Memory Bank
                          (via RAG). If provided, they are injected into the system
                          prompt so the LLM avoids repeating past mistakes.

        Returns:
            PersonaSignal with direction, confidence, leverage, and thesis.
        """
        prompt = self._build_data_prompt(data)
        system = self._inject_lessons_into_system(self.SYSTEM_PROMPT, past_lessons)
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]

        try:
            raw = self._client.chat(messages, temperature=self._temperature)
            parsed = _parse_signal_response(raw, self.persona_type)
            
            try:
                direction_enum = Direction(parsed["direction"])
            except ValueError:
                direction_enum = Direction.NEUTRAL

            return PersonaSignal(
                thesis=parsed["thesis"],
                direction=direction_enum,
                confidence=parsed["confidence_score"],
                leverage=parsed["recommended_leverage"],
                persona=self.persona_type,
                warnings=parsed["_warnings"],
            )
        except Exception as e:
            return PersonaSignal(
                thesis=f"Persona {self.persona_type.value} error: {e}",
                direction=Direction.NEUTRAL,
                confidence=0,
                leverage=1,
                persona=self.persona_type,
                warnings=["Persona analysis failed — defaulting to NEUTRAL."],
            )

    def _inject_lessons_into_system(
        self,
        system_prompt: str,
        past_lessons: Optional[List[str]],
    ) -> str:
        """
        Append Memory Bank lessons to the system prompt as a hard constraint block.

        The injected block is placed at the very end of the system prompt so
        it is the last thing the model reads before responding.
        """
        if not past_lessons:
            return system_prompt

        lessons_block = (
            "\n\n"
            "══════════════════════════════════════════════════════════════\n"
            "CRITICAL PAST LESSONS — YOU MUST NOT REPEAT THESE MISTAKES:\n"
            "══════════════════════════════════════════════════════════════\n"
            + "\n".join(f"- {lesson}" for lesson in past_lessons)
            + "\n══════════════════════════════════════════════════════════════\n"
            "IMPORTANT: The rules above were learned from real losses. Apply them.\n"
            "══════════════════════════════════════════════════════════════\n"
        )
        return system_prompt + lessons_block

    def reflect_on_loss(
        self,
        anomaly_type: str,
        thesis: str,
        pnl: float,
        market_context: str = "",
        past_lessons: Optional[List[str]] = None,
    ) -> str:
        """
        The Psychologist: After a losing trade, force the LLM to write a
        1-sentence rule about why it failed.

        In dry_run mode (backtesting) this returns a synthetic fallback rule
        immediately — no LLM call is made.

        Args:
            anomaly_type:  The scanner event type (e.g. "LIQUIDITY_SWEEP")
            thesis:        The persona's stated thesis when entering the trade
            pnl:           Signed % loss (negative value, e.g. -2.3)
            market_context: Optional human-readable market data snapshot
            past_lessons: Optional list of past lessons to inject

        Returns:
            A 1-sentence strict rule string (or fallback in dry_run).
        """
        if self._dry_run:
            return (
                f"[DRY_RUN reflexion] {self.persona_type.value} would normally "
                f"reflect on a {pnl:.1f}% loss from a {anomaly_type} setup "
                f"(thesis: {thesis[:60]}...) but no LLM call is made in dry_run."
            )

        reflexion_prompt = (
            f"You are a {self.persona_type.value} trading persona.\n"
            f"You recently took a LOSS of {pnl:.2f}% on a {anomaly_type} setup.\n"
            f"Your thesis entering the trade was:\n"
            f"  \"{thesis}\"\n"
            f"Market context:\n"
            f"  {market_context or 'Not available.'}\n\n"
            f"Analyze what went wrong. Output ONLY a single, strict, actionable "
            f"1-sentence rule that you will NEVER violate again.\n"
            f"Output format: just the sentence, no quotes, no explanation.\n"
            f'Example: "Never enter a long if the sweep candle closes below the '
            f"20-bar rolling low within 3 candles of a major resistance zone.\"\n"
        )

        system_prompt = self._inject_lessons_into_system(self.SYSTEM_PROMPT, past_lessons)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": reflexion_prompt},
        ]

        try:
            raw = self._client.chat(messages, temperature=0.3)
            # Strip surrounding whitespace and any leading/trailing punctuation
            rule = raw.strip().strip('"').strip("'").strip(".")
            return rule if rule else "[Empty reflexion — no lesson saved]"
        except Exception as e:
            return f"[Reflexion failed: {e}]"

    @property
    def persona_type(self) -> PersonaType:
        raise NotImplementedError


class SMC_ICT_Persona(BasePersona):
    """
    Smart Money Concepts / ICT specialist persona.
    Focus: Liquidity sweeps, FVGs, Market Structure Shifts, Order Blocks.
    """

    SYSTEM_PROMPT = SYSTEM_SMC_ICT

    @property
    def persona_type(self) -> PersonaType:
        return PersonaType.SMC_ICT

    def _build_data_prompt(self, data: Dict[str, Any]) -> str:
        # Accept structured dict from the orchestrator / data pipeline
        btc     = data.get("btc",     {})
        target  = data.get("target",  {})
        multi   = data.get("multi_tf", {})
        levels  = data.get("key_levels", {})
        fg_raw  = data.get("fear_greed_index", "NA")
        fg_str  = str(fg_raw) if fg_raw is not None else "NA"

        def _safe(v, default="N/A"):
            return f"${v:,.2f}" if isinstance(v, (int, float)) else default

        tf_lines = []
        for tf_name, tf_data in multi.items():
            if not tf_data:
                continue
            tf_lines.append(
                f"{tf_name.upper()}:\n"
                f"  Price: {_safe(tf_data.get('close'))}\n"
                f"  Trend: {tf_data.get('trend', 'N/A')}\n"
                f"  Support: {_safe(tf_data.get('support'))} | Resistance: {_safe(tf_data.get('resistance'))}\n"
                f"  EMA50: {_safe(tf_data.get('ema50'))} | EMA200: {_safe(tf_data.get('ema200'))}\n"
                f"  RSI: {tf_data.get('rsi', 'N/A')}\n"
            )

        return f"""Market Data for ICT/SMC Analysis

SYMBOL: {data.get('symbol', 'BTCUSDT')}
CURRENT ANOMALY TRIGGER: You are evaluating a {data.get('anomaly_type', 'UNKNOWN')}
Fear & Greed: {fg_str}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRICE DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Target Symbol:
  Current Price: {_safe(target.get('close'))}
  4H High: {_safe(target.get('high_4h'))} | 4H Low: {_safe(target.get('low_4h'))}
  EMA50: {_safe(target.get('ema50'))} | EMA200: {_safe(target.get('ema200'))}
  RSI: {target.get('rsi', 'N/A')}
  Trend: {target.get('trend', 'N/A')}

BTC Anchor:
  Current Price: {_safe(btc.get('close'))}
  Trend: {btc.get('trend', 'N/A')} | RSI: {btc.get('rsi', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MULTI-TIMEFRAME STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join(tf_lines) if tf_lines else "Multi-TF data not available."}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KEY LEVELS (Highs / Lows / FVG Zones)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{levels.get('summary', 'Levels not available.')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Using ICT/SMC methodology:
1. Identify any liquidity sweep zones
2. Locate Fair Value Gaps (FVGs)
3. Look for Market Structure Shifts (MSS)
4. Note any valid Order Blocks
5. Provide your directional thesis, confidence, and recommended leverage.

Respond with ONLY the required JSON object."""


class OrderFlowPersona(BasePersona):
    """
    Order Flow / Microstructure specialist persona.
    Focus: OI, funding rates, liquidation clusters, volume delta.
    """

    SYSTEM_PROMPT = SYSTEM_ORDER_FLOW

    @property
    def persona_type(self) -> PersonaType:
        return PersonaType.ORDER_FLOW

    def _build_data_prompt(self, data: Dict[str, Any]) -> str:
        deriv  = data.get("derivatives", {})
        liq    = data.get("liquidations", {})
        vol    = data.get("volume", {})
        basis  = data.get("basis", {})

        def _pct(v, default="N/A"):
            return f"{v:+.4f}%" if isinstance(v, (int, float)) else default

        return f"""Market Data for Order Flow / Microstructure Analysis

SYMBOL: {data.get('symbol', 'BTCUSDT')}
CURRENT ANOMALY TRIGGER: You are evaluating a {data.get('anomaly_type', 'UNKNOWN')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DERIVATIVES MARKET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Open Interest (OI):
  Binance OI: ${deriv.get('binance_oi_usd', 'N/A')}
  Bybit OI:   ${deriv.get('bybit_oi_usd', 'N/A')}
  OI 24h Change: {_pct(deriv.get('oi_change_pct'))}
  OI Trend: {deriv.get('oi_trend', 'N/A')}  # rising / falling / neutral

Funding Rates (8h):
  Binance: {_pct(deriv.get('binance_funding'))}
  Bybit:   {_pct(deriv.get('bybit_funding'))}
  OKX:     {_pct(deriv.get('okx_funding'))}
  Avg Funding (composite): {_pct(deriv.get('avg_funding'))}

Long/Short Ratio:
  Binance: {deriv.get('long_short_ratio_binance', 'N/A')}
  Bybit:   {deriv.get('long_short_ratio_bybit', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LIQUIDATION CLUSTERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
24h Liquidations:
  Long Liqs:  ${liq.get('long_liquidations_24h', 'N/A')}
  Short Liqs: ${liq.get('short_liquidations_24h', 'N/A')}
  Total:      ${liq.get('total_liquidations_24h', 'N/A')}

Key Cluster Levels (approx price zones where stops cluster):
  Cluster 1: ${liq.get('cluster_1', 'N/A')}
  Cluster 2: ${liq.get('cluster_2', 'N/A')}
  Cluster 3: ${liq.get('cluster_3', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VOLUME DELTA & TAPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
24h Volume: ${vol.get('volume_24h', 'N/A')}
Buy Volume %: {vol.get('buy_volume_pct', 'N/A')}%
Volume vs 7d Avg: {vol.get('volume_vs_avg', 'N/A')}
Large Trades (> $500K): {vol.get('large_trades_count', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BASIS / CONTANGO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BTC Perpetual Price: ${basis.get('perp_price', 'N/A')}
BTC Quarterly Future Price: ${basis.get('quarterly_price', 'N/A')}
Basis: {_pct(basis.get('basis_pct'))}  (positive = contango, negative = backwardation)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Using order flow / microstructure methodology:
1. Assess OI trend and direction (rising/falling + price movement)
2. Evaluate funding rate extremes and squeeze probability
3. Identify proximity to liquidation clusters
4. Note volume delta and tape-reading signals
5. Provide directional thesis, confidence, and recommended leverage.

Respond with ONLY the required JSON object."""


class MacroOnChainPersona(BasePersona):
    """
    Macro / On-Chain specialist persona.
    Focus: Global liquidity, ETF flows, whale wallets, MVRV, cycle position.
    """

    SYSTEM_PROMPT = SYSTEM_MACRO_ONCHAIN

    @property
    def persona_type(self) -> PersonaType:
        return PersonaType.MACRO_ONCHAIN

    def _build_data_prompt(self, data: Dict[str, Any]) -> str:
        etf   = data.get("etf",           {})
        whale = data.get("whale",         {})
        macro = data.get("macro",         {})
        oc    = data.get("onchain",       {})
        cycle = data.get("cycle",         {})

        def _billions(v, default="N/A"):
            return f"${v/1e9:.2f}B" if isinstance(v, (int, float)) else default

        def _pct(v, default="N/A"):
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else default

        return f"""Market Data for Macro / On-Chain Analysis

SYMBOL: {data.get('symbol', 'BTCUSDT')}
CURRENT ANOMALY TRIGGER: You are evaluating a {data.get('anomaly_type', 'UNKNOWN')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ETF FLOWS (Bitcoin Spot ETFs)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IBIT (BlackRock) 7-day net flow: {_billions(etf.get('ibit_7d_flow', 0))}
FBTC (Fidelity)  7-day net flow: {_billions(etf.get('fbtc_7d_flow', 0))}
GBTC (Grayscale) 7-day net flow: {_billions(etf.get('gbtc_7d_flow', 0))}
Total Market 7-day flow:         {_billions(etf.get('total_7d_flow', 0))}
AUM (total ETF market):           {_billions(etf.get('total_aum', 0))}
Premium/Discount to NAV:          {_pct(etf.get('nav_premium'))}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GLOBAL MACRO INDICATORS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DXY (US Dollar Index): {macro.get('dxy', 'N/A')}
10Y US Treasury Yield: {macro.get('us10y_yield', 'N/A')}%
Fed Balance Sheet (M2): {_billions(macro.get('m2_supply', 0))}
M2 7-day change:        {_pct(macro.get('m2_change_pct'))}
Risk Sentiment:        {macro.get('risk_sentiment', 'N/A')}  # risk_on / risk_off / neutral

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHALE & EXCHANGE FLOWS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Exchange BTC Reserves:    {whale.get('exchange_reserves_btc', 'N/A')} BTC
Exchange Reserves 7d Δ:  {_pct(whale.get('exchange_reserves_change_pct'))}
Whale Transaction Count: {whale.get('whale_tx_count', 'N/A')}  # >$1M txs
Stablecoin (USDT+USDC) on exchanges: {whale.get('stablecoin_exchange_balance', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ON-CHAIN METRICS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MVRV Z-Score:    {oc.get('mvrv_z', 'N/A')}  (>7 = top zone, <1 = bottom zone)
SOPR (7d MA):    {oc.get('sopr', 'N/A')}     (>1 = profit-taking zone, <1 = capitulation)
Exchange Reserves: {oc.get('exchange_reserves_btc', 'N/A')} BTC
Active Addresses (7d MA): {oc.get('active_addresses_7d', 'N/A')}
Hash Rate:       {oc.get('hash_rate', 'N/A')} EH/s
Miner Position Index (MPI): {oc.get('mpi', 'N/A')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CYCLE INDICATORS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Days Since Halving: {cycle.get('days_since_halving', 'N/A')}
Post-Halving Year:  {cycle.get('halving_year', 'N/A')}  # Year 1/2/3/4
Puell Multiple:      {cycle.get('puell_multiple', 'N/A')}
RHODL Ratio:        {cycle.get('rhodl_ratio', 'N/A')}
Difficulty Ribbon:  {cycle.get('difficulty_ribbon', 'N/A')}  # compression / expansion / flip

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR TASK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Using macro / on-chain methodology:
1. Classify the current liquidity regime (A/B/C/D)
2. Assess ETF flow direction and conviction
3. Evaluate whale behavior and MVRV zone
4. Determine cycle position and conviction
5. Provide directional thesis, confidence, and recommended leverage.

Respond with ONLY the required JSON object."""


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_persona(
    persona_type: PersonaType,
    api_key: Optional[str] = None,
    **kwargs,
) -> BasePersona:
    """Factory: instantiate the correct persona class by enum."""
    mapping = {
        PersonaType.SMC_ICT:       SMC_ICT_Persona,
        PersonaType.ORDER_FLOW:    OrderFlowPersona,
        PersonaType.MACRO_ONCHAIN: MacroOnChainPersona,
    }
    cls = mapping.get(persona_type)
    if cls is None:
        raise ValueError(f"Unknown persona type: {persona_type}")
    return cls(api_key=api_key, **kwargs)


# ─── Quick test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Plutus V3 — Persona Sculptor")
    print("=" * 50)
    print("SMC_ICT persona system prompt preview:")
    print(SYSTEM_SMC_ICT[:200], "...")
    print()
    print("ORDER_FLOW persona system prompt preview:")
    print(SYSTEM_ORDER_FLOW[:200], "...")
    print()
    print("MACRO_ONCHAIN persona system prompt preview:")
    print(SYSTEM_MACRO_ONCHAIN[:200], "...")
    print()
    # Dry-run instantiation
    smc  = create_persona(PersonaType.SMC_ICT)
    of   = create_persona(PersonaType.ORDER_FLOW)
    macro = create_persona(PersonaType.MACRO_ONCHAIN)
    print(f"SMC_ICT:       {type(smc).__name__} ✓")
    print(f"ORDER_FLOW:    {type(of).__name__} ✓")
    print(f"MACRO_ONCHAIN: {type(macro).__name__} ✓")
    print("Persona Sculptor ready.")
