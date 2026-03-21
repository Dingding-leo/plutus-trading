"""
Market context module for risk classification and macro assessment.
"""

from typing import Optional
from .. import config


def classify_risk_level(
    has_major_news: bool = False,
    has_war_news: bool = False,
    has_macro_news: bool = False,
    has_regulation_news: bool = False,
    atr_multiplier: float = None,
    structure_broken: bool = False,
    fear_greed_index: int = None,
) -> str:
    """
    Classify market risk level.

    Args:
        has_major_news: Any major news
        has_war_news: Geopolitical war news
        has_macro_news: Macro news (Fed, CPI, etc.)
        has_regulation_news: Regulatory news
        atr_multiplier: Current ATR / average ATR
        structure_broken: Structure broken
        fear_greed_index: Fear & Greed Index (0-100)

    Returns:
        Risk level: 'LOW', 'MODERATE', or 'HIGH'
    """
    # HIGH RISK triggers
    high_risk_triggers = 0

    if has_war_news:
        high_risk_triggers += 2

    if has_macro_news:  # CPI, FOMC, NFP
        high_risk_triggers += 1

    if has_regulation_news:
        high_risk_triggers += 1

    if atr_multiplier is not None and atr_multiplier >= config.HIGH_RISK_ATR_MULTIPLIER:
        high_risk_triggers += 1

    if structure_broken:
        high_risk_triggers += 1

    if fear_greed_index is not None:
        if fear_greed_index < config.EXTREME_FEAR_THRESHOLD:
            high_risk_triggers += 1
        elif fear_greed_index > config.EXTREME_GREED_THRESHOLD:
            high_risk_triggers += 1

    # Determine risk level
    if high_risk_triggers >= 2:
        return "HIGH"
    elif high_risk_triggers == 1:
        return "MODERATE"
    else:
        return "LOW"


def determine_macro_state(
    btc_analysis: dict,
    market_overview: dict = None,
) -> str:
    """
    Determine macro state (risk_on or risk_off).

    Args:
        btc_analysis: BTC analysis dict
        market_overview: Market overview dict

    Returns:
        'risk_on' or 'risk_off'
    """
    trend = btc_analysis.get("trend", "SIDEWAYS")
    momentum = btc_analysis.get("momentum", {})
    change_24h = momentum.get("change_24h", 0)

    # DOWNTREND always = risk_off
    if trend == "DOWNTREND":
        return "risk_off"

    # SIDEWAYS with negative momentum = risk_off
    if trend == "SIDEWAYS" and change_24h < 0:
        return "risk_off"

    # Everything else = risk_on (UPTREND, SIDEWAYS with positive/neutral momentum)
    return "risk_on"


def assess_btc_strength(
    btc_analysis: dict,
    recent_candles: list[dict] = None,
) -> str:
    """
    Assess BTC strength/weakness.

    Args:
        btc_analysis: BTC analysis dict
        recent_candles: Recent candles for pattern recognition

    Returns:
        'strength', 'neutral', or 'weakness'
    """
    trend = btc_analysis.get("trend", "SIDEWAYS")
    momentum = btc_analysis.get("momentum", {})
    change_24h = momentum.get("change_24h", 0)
    signal = btc_analysis.get("signal", "NEUTRAL")

    # Weakness signals
    weakness_signals = 0

    if trend == "DOWNTREND":
        weakness_signals += 1

    if change_24h < -3:
        weakness_signals += 2
    elif change_24h < 0:
        weakness_signals += 1

    if signal == "SELL":
        weakness_signals += 1

    # Strength signals
    strength_signals = 0

    if trend == "UPTREND":
        strength_signals += 1

    if change_24h > 3:
        strength_signals += 2
    elif change_24h > 0:
        strength_signals += 1

    if signal == "BUY":
        strength_signals += 1

    # Determine
    if weakness_signals >= 2:
        return "weakness"
    elif strength_signals >= 2:
        return "strength"
    else:
        return "neutral"


def get_valid_trading_answers(
    macro_state: str,
    btc_strength: str,
) -> dict:
    """
    Get valid trading answers based on macro and BTC status.

    Args:
        macro_state: 'risk_on' or 'risk_off'
        btc_strength: 'strength', 'neutral', or 'weakness'

    Returns:
        Dict with valid answers and forbidden trades
    """
    result = {
        "valid_answers": [],
        "forbidden": [],
        "recommendation": None,
    }

    # Risk-off + BTC weakness
    if macro_state == "risk_off" and btc_strength == "weakness":
        result["valid_answers"] = ["NO TRADE", "BTC SHORT"]
        result["forbidden"] = ["ALT LONG"]
        result["recommendation"] = "NO TRADE or BTC SHORT only"

    # Risk-off + BTC neutral
    elif macro_state == "risk_off" and btc_strength == "neutral":
        result["valid_answers"] = ["NO TRADE", "BTC SHORT"]
        result["forbidden"] = ["ALT LONG"]
        result["recommendation"] = "NO TRADE or BTC SHORT only"

    # Risk-off + BTC strength
    # Per CLAUDE.md: BTC is market anchor, even in risk-off BTC can be traded
    # but ALT longs are forbidden in risk-off
    elif macro_state == "risk_off" and btc_strength == "strength":
        result["valid_answers"] = ["NO TRADE", "BTC LONG", "BTC SHORT"]
        result["forbidden"] = ["ALT LONG", "ETH LONG"]
        result["recommendation"] = "BTC > ETH > ALT hierarchy: Only BTC trades allowed in risk-off"

    # Risk-on
    elif macro_state == "risk_on":
        result["valid_answers"] = ["BTC LONG", "ETH LONG", "ALT LONG", "NO TRADE"]
        result["forbidden"] = []
        result["recommendation"] = "All directions allowed, follow structure"

    return result


def format_market_context(
    btc_analysis: dict,
    eth_analysis: dict,
    market_overview: dict,
    risk_level: str,
    macro_state: str,
    btc_strength: str,
) -> str:
    """
    Format market context into readable string.

    Args:
        btc_analysis: BTC analysis dict
        eth_analysis: ETH analysis dict
        market_overview: Market overview dict
        risk_level: Risk level
        macro_state: Macro state
        btc_strength: BTC strength

    Returns:
        Formatted markdown string
    """
    output = "## Market Context\n\n"

    # Overview
    output += f"### Overview\n"
    output += f"- Risk Level: **{risk_level}**\n"
    output += f"- Macro: **{macro_state.upper()}**\n"
    output += f"- BTC Status: **{btc_strength.upper()}**\n\n"

    if "fear_greed_index" in market_overview:
        fgi = market_overview["fear_greed_index"]
        fgc = market_overview.get("fear_greed_classification", "N/A")
        output += f"- Fear & Greed: {fgi} ({fgc})\n\n"

    # Market cap
    if "total_market_cap" in market_overview:
        cap = market_overview["total_market_cap"]
        output += f"\n- Total Market Cap: ${cap/1e12:.2f}T\n"

    if "btc_dominance" in market_overview:
        dom = market_overview.get("btc_dominance", 0)
        output += f"- BTC Dominance: {dom:.1f}%\n"

    # BTC
    output += f"\n### BTC/USDT\n"
    output += f"- Trend: {btc_analysis.get('trend', 'N/A')}\n"
    output += f"- Signal: {btc_analysis.get('signal', 'N/A')}\n"
    output += f"- Support: ${btc_analysis.get('support', 0):,.0f}\n"
    output += f"- Resistance: ${btc_analysis.get('resistance', 0):,.0f}\n"

    # ETH
    output += f"\n### ETH/USDT\n"
    output += f"- Trend: {eth_analysis.get('trend', 'N/A')}\n"
    output += f"- Signal: {eth_analysis.get('signal', 'N/A')}\n"

    return output
