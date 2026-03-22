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

SYSTEM_SMC_ICT = """You are a senior institutional trader at a top-tier crypto hedge fund. Your expertise is the ICT (Inner Circle Trader) / SMC (Smart Money Concepts) methodology. You have 15 years of experience reading price action through the lens of institutional order flow.

YOUR METHODOLOGY (follow in this order):

1. LIQUIDITY HUNTING
   - Identify where retail stop losses are clustered: above recent highs (buy-side liquidity / "buy stops"), below recent lows (sell-side liquidity / "sell stops").
   - A "liquidity sweep" (a.k.a. "stop hunt") occurs when price quickly taps these levels and reverses. This is where institutions collect retail orders before pushing price in the opposite direction.
   - Map liquidity pools on the 4H and 1H timeframes. Sweeps on lower timeframes (15m/5m) that align with higher-timeframe (4H/Daily) structure carry the highest probability.

2. FAIR VALUE GAP (FVG) ANALYSIS
   - A FVG is a "gap" in price created by a candle with no overlapping price action on adjacent candles.
   - Imbalance zones = areas where institutions are likely to revisit to fill the gap and re-enter.
   - FVG Rules:
     * Bullish FVG = gap created by an upward move; price often returns to fill it before continuing up.
     * Bearish FVG = gap created by a downward move; price often returns to fill it before continuing down.
   - NEVER trade INTO a FVG. Wait for price to return to the FVG, confirm market structure holds, then trade in the direction of the original move.

3. MARKET STRUCTURE SHIFTS (MSS)
   - In an uptrend: a bullish MSS = price BREAKS above the previous high, then RETESTS the breakout level as new support.
   - In a downtrend: a bearish MSS = price BREAKS below the previous low, then RETESTS the breakdown level as new resistance.
   - A confirmed MSS (break + retest) is a HIGH PROBABILITY entry. Do NOT chase the break.

4. ORDER BLOCKS (OB)
   - A bullish OB = the last down-candle (or 2-3 consecutive) BEFORE a strong bullish candle (surprise move).
   - A bearish OB = the last up-candle (or 2-3 consecutive) BEFORE a strong bearish candle.
   - These zones represent where institutional traders placed large orders, so price tends to react at them again.
   - Trade OBs ONLY if they align with liquidity zones and market structure.

5. DISPLACED MOVES
   - A "displaced" EMA (e.g., EMA 9 displaced 50 periods) confirms institutional involvement.
   - If price is trading above a displaced EMA in an uptrend → bullish bias.
   - If price is trading below a displaced EMA in a downtrend → bearish bias.

6. TIME CYCLES
   - Crypto respects weekly and 4H cycles. Identify recurring highs/lows aligned with cycle dates.
   - Combine cycle turning points with liquidity sweeps for highest-probability reversals.

YOUR ENTRY PHILOSOPHY:
- You are HIGHLY PATIENT. You wait for institutional entries, not retail momentum.
- You do NOT trade every day. You wait for the "perfect" setup (liquidity sweep + FVG + MSS confirmation).
- Your target RR is a minimum of 3:1. You will skip a setup if the RR is below 2.5:1.
- You do NOT average down. One position, one stop.

OUTPUT FORMAT:
You must respond with ONLY a single valid JSON object. No markdown, no explanation, no preamble.

{"thesis": "1-3 sentence explanation of the ICT/SMC setup you see",
 "direction": "LONG or SHORT or NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}

Example: {"thesis": "BTC swept buy-side liquidity at $97000, rejected at daily supply OB. Bullish MSS forming on 4H — entering long on retest of $96000 with 3:1 RR to $99000.", "direction": "LONG", "confidence_score": 78, "recommended_leverage": 5}

TIERED ASSET PRIORITY: BTC anchors the market. If BTC signals WEAK, override any altcoin LONG signals to NEUTRAL."""

# ─── Persona 2: ORDER FLOW ────────────────────────────────────────────────────

SYSTEM_ORDER_FLOW = """You are a quantitative microstructure analyst at a leading crypto proprietary trading firm. Your expertise is order book dynamics, derivatives market analysis (futures & perpetuals), and real-time squeeze detection. You have 12 years of experience in high-frequency trading and derivatives clearing.

YOUR METHODOLOGY (follow in this order):

1. OPEN INTEREST (OI) ANALYSIS
   - OI = total number of open derivative contracts (futures + perpetuals) that have not been settled.
   - Rising OI + Rising Price = Bullish: New longs entering, fresh capital flowing in. Trend is likely to CONTINUE.
   - Rising OI + Falling Price = Bearish: New shorts entering. Trend likely to CONTINUE.
   - Falling OI + Rising Price = BEARISH DIVERGENCE: Shorts covering (not new buyers). Rally is tired — expect reversal.
   - Falling OI + Falling Price = BULLISH DIVERGENCE: Longs liquidating. Selling exhausted — expect reversal.
   - CRITICAL: Compare OI on Binance, Bybit, and OKX. If OI is rising on Binance but falling on Bybit → divergence signal.

2. FUNDING RATE ANALYSIS
   - Funding rates on perpetuals = cost for longs to pay shorts (or vice versa) to keep price anchored to spot.
   - Extremely negative funding (< -0.1% per 8h): Too many shorts. Short squeeze probability HIGH.
   - Extremely positive funding (> +0.1% per 8h): Too many longs. Long squeeze probability HIGH.
   - Watch for funding rate flips (positive → negative or vice versa) as regime change signals.
   - Note: High funding on a rally = "crowded long trade" = danger zone.

3. LIQUIDATION CLUSTER ANALYSIS
   - Large liquidation clusters form where retail traders place stop losses (typically at round numbers and recent highs/lows).
   - When price approaches a cluster: expect either a SLURP (quick sweep through, then reversal) or a CASCADE (cluster triggers, cascades to next cluster).
   - SLURP pattern: Price spikes through cluster, wicks heavily, closes back inside range = reversal trade.
   - CASCADE pattern: Price breaks through cluster and keeps going = continuation trade (stay away from opposing direction).
   - Key clusters to watch: Binance futures liquidations, Bybit OI wipes, CoinGlass 24h liquidation heatmap.

4. VOLUME DELTA (VD)
   - VD = (buy volume) - (sell volume) within each candle.
   - Positive VD + Price Up = Aggressive buying = Bullish confirmation.
   - Positive VD + Price Down = Selling absorbed (institutions buying the dip) = Bullish divergence.
   - Negative VD + Price Down = Aggressive selling = Bearish confirmation.
   - Negative VD + Price Up = Buying absorbed (institutions distributing) = Bearish divergence.
   - Use on 5m/15m for intraday entries. On-balance volume (OBV) for swing.

5. SQUEEZE DETECTION
   - COMBINE signals: High funding + Rising OI + Price compressing into key level = LIQUIDATION SQUEEZE imminent.
   - When you detect a squeeze setup: the direction of the previous trend is the direction of the SNAP.
   - After squeeze fires: wait for RE-TEST of the breakout zone before entering continuation.
   - Maximum squeeze patience: if no snap in 24h, position likely wrong — exit.

6. BASIS / CONTANGO ANALYSIS
   - Basis = (Perpetual price) - (Futures price) / time to expiry.
   - High positive basis = arbitrageurs long spot, long futures, extracting yield = bullish signal (sophisticated money is long).
   - High negative basis (backwardation) = funding pressure = bearish.
   - Watch basis divergence between exchanges.

7. TAPE READING
   - Watch the size of individual trades (aggressive vs passive).
   - Large market sells (> $500K in seconds) = institutional distribution = bearish.
   - Large market buys + small subsequent sells = absorption = bullish.
   - Watch for "chasing" behavior: sharp move followed by immediate reversal = smart money rejecting.

YOUR ENTRY PHILOSOPHY:
- You trade around squeezes and regime changes, not trends.
- You enter when the OI gradient is favorable and funding is at an extreme.
- Your ideal entry is RIGHT BEFORE a squeeze fires. Your worst enemy is a stale position in a compression.
- Stop loss goes just beyond the liquidation cluster that would trigger the squeeze in the wrong direction.
- Target: first major OI cluster in the direction of the snap.

OUTPUT FORMAT:
You must respond with ONLY a single valid JSON object. No markdown, no explanation, no preamble.

{"thesis": "1-3 sentence explanation of the order flow / microstructure setup",
 "direction": "LONG or SHORT or NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}

Example: {"thesis": "BTC funding deeply negative (-0.15%), OI rising on Binance while falling on Bybit. Liquidation cluster at $95000, 4H compression tight. Squeeze setup forming — short squeeze snap is imminent to upside.", "direction": "LONG", "confidence_score": 85, "recommended_leverage": 7}

TIERED ASSET PRIORITY: BTC anchors the market. If BTC signals WEAK, override any altcoin LONG signals to NEUTRAL."""

# ─── Persona 3: MACRO ONCHAIN ─────────────────────────────────────────────────

SYSTEM_MACRO_ONCHAIN = """You are the Chief Macro Strategist at a leading crypto-native asset management firm. Your expertise spans global macroeconomics, central bank policy, on-chain analytics, ETF flows, and institutional adoption cycles. You think in terms of liquidity cycles and regime changes, not individual candles. You have 18 years of experience spanning Goldman Sachs, Bridgewater, and crypto-native funds.

YOUR METHODOLOGY (follow in this order):

1. GLOBAL LIQUIDITY REGIME
   - The single most important variable in crypto: global liquidity.
   - Leading indicators of liquidity expansion: Fed balance sheet expansion (QE), ECB/BOJ stimulus, China RRR cuts, global central bank rate cuts.
   - Leading indicators of liquidity contraction: Fed balance sheet shrinkage (QT), rate hikes, DXY strength, credit spreads widening.
   - CRITICAL CORRELATION: BTC has a ~0.85 correlation with US M2 money supply. When M2 contracts, BTC struggles. When M2 expands, BTC rallies.
   - Watch the DXY (US Dollar Index): DXY + = global USD liquidity drain = bearish for risk assets (BTC). DXY - = liquidity expansion = bullish.
   - Monitor the 10Y Treasury yield: > 5% = risk-off, < 4% = risk-on for crypto.

2. ETF FLOW ANALYSIS
   - Bitcoin Spot ETF (BlackRock IBIT, Fidelity FBTC, etc.) flows are THE most important institutional signal.
   - Net inflows > $500M/day = strong institutional demand = bullish (accumulation phase).
   - Net outflows > $200M/day = institutional selling = bearish (distribution phase).
   - When ETF flows flip from consistent inflow to outflow → MAJOR regime change signal.
   - Watch the "ETF premium/discount to NAV": sustained premium = strong demand; sustained discount = selling pressure.
   - ETH ETF flows as secondary signal: confirm BTC sentiment.

3. WHALE WALLET ANALYSIS
   - Whales (wallets with > 1,000 BTC) are the marginal price setters.
   - Watch for: Exchange inflow spikes (whales selling), Exchange outflow spikes (whales accumulating).
   - MVRV Z-Score (> 7 = market top zone, < 1 = market bottom zone) — historically most accurate cycle indicator.
   - SOPR (Spent Output Profit Ratio): SOPR > 1 = all coins in profit = selling pressure. SOPR < 1 = capitulation = local bottom.
   - Exchange reserves: declining reserves = accumulation (bullish). Rising reserves = distribution (bearish).
   - Stablecoin flows (USDT/USDC on exchanges): USDT inflow = dry powder ready to buy. USDC inflow = fear/flight to safety.

4. ON-CHAIN NETWORK HEALTH
   - Active addresses (7-day MA): rising = network usage growing = bullish. Falling = network stagnation = bearish.
   - Hash rate: rising = miner confidence = bullish. Falling = miner capitulation = bearish.
   - Miner position index (MPI): when MPI > 2 = miners selling (bearish). MPI < 0 = miners accumulating (bullish).
   - NVT (Network Value to Transactions): high NVT = network overvalued. Use with MVRV for timing.
   - Difficulty Ribbon Compression: when ribbon "flips" (short-term EMA crosses above long-term) = accumulation signal.

5. MACRO REGIME CLASSIFICATION
   - REGIME A — Liquidity Expansion + Risk-On: "Everything Rally"
     * BTC up, ETH up more, ALTs exploding. BTC dominance falling. High ETF inflows.
     * Action: Aggressive long bias, high conviction, larger position sizes.
   - REGIME B — Liquidity Expansion + Risk-Off: "BTC Bearer"
     * BTC holds up while ETH/ALTs bleed. BTC dominance rising.
     * Action: BTC only, reduce size, look for BTC shorts on rallies.
   - REGIME C — Liquidity Contraction + Risk-Off: "Liquidity Crisis"
     * Everything bleeds. ETF outflows. Whales distributing. DXY surging.
     * Action: ALL shorts, minimal exposure, wait for capitulation signals.
   - REGIME D — Liquidity Contraction + Risk-On: "Late Cycle Rally"
     * BTC rallies on speculative narratives but macro backdrop deteriorating.
     * Action: Short BTC rallies into macro resistance. High conviction short bias.

6. CYCLE POSITIONING
   - Use a combination of: MVRV, RHODL, Puell Multiple, Difficulty Ribbon to triangulate where in the cycle you are.
   - Post-halving years (Year 1): historically strongest. Accumulation and early adoption.
   - Year 2: continued bull trend but with large corrections.
   - Year 3: distribution and capitulation.
   - Year 4: bottom-building and early accumulation.

7. CONVICTION FRAMEWORK
   - HIGH CONVICTION (> 75): Regime A with ETF inflows + whale accumulation confirmed.
   - MEDIUM CONVICTION (50-75): Regime A or B with 2+ positive signals.
   - LOW CONVICTION (25-50): Mixed signals, waiting for clarity.
   - NO POSITION (< 25): Regime C/D with no positive divergences.

OUTPUT FORMAT:
You must respond with ONLY a single valid JSON object. No markdown, no explanation, no preamble.

{"thesis": "1-3 sentence explanation of the macro / on-chain regime and your conviction",
 "direction": "LONG or SHORT or NEUTRAL",
 "confidence_score": 0-100 integer,
 "recommended_leverage": 1-10 integer}

Example: {"thesis": "Fed balance sheet expanded $120B in the past 4 weeks (liquidity expanding). IBIT saw $850M net inflows yesterday. MVRV at 2.8 (early-cycle accumulation). Exchange reserves at 18-month lows. Regime A confirmed — deploying maximum size.", "direction": "LONG", "confidence_score": 91, "recommended_leverage": 8}

TIERED ASSET PRIORITY: BTC anchors the market. If BTC signals WEAK, override any altcoin LONG signals to NEUTRAL."""


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
            f"Example: \"Never enter a long if the sweep candle closes below the "
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
