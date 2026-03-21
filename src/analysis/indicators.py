"""
Technical indicators module for trading analysis.
"""

import math
from typing import Optional


def calculate_ema(prices: list[float], period: int) -> float:
    """
    Calculate Exponential Moving Average.

    Args:
        prices: List of closing prices
        period: EMA period (e.g., 50, 200)

    Returns:
        Current EMA value
    """
    if len(prices) < period:
        raise ValueError(f"Not enough data: need {period}, got {len(prices)}")

    # Use simple moving average for first EMA value
    multiplier = 2 / (period + 1)

    # Calculate initial SMA
    sma = sum(prices[:period]) / period

    # Calculate EMA
    ema = sma
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_sma(prices: list[float], period: int) -> float:
    """
    Calculate Simple Moving Average.

    Args:
        prices: List of closing prices
        period: SMA period

    Returns:
        Current SMA value
    """
    if len(prices) < period:
        raise ValueError(f"Not enough data: need {period}, got {len(prices)}")

    return sum(prices[-period:]) / period


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """
    Calculate Relative Strength Index using Wilder's smoothing method.

    Args:
        prices: List of closing prices
        period: RSI period (default 14)

    Returns:
        RSI value (0-100)
    """
    if len(prices) < period + 1:
        raise ValueError(f"Not enough data: need {period + 1}, got {len(prices)}")

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    # Use Wilder's smoothing method (exponential moving average)
    # First value: simple average of first 'period' values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Subsequent values: Wilder's smoothing
    # Start from index 'period' and continue to end
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def detect_trend(ema50: float, ema200: float) -> str:
    """
    Detect trend based on EMA crossover.

    Args:
        ema50: 50-period EMA
        ema200: 200-period EMA

    Returns:
        'UPTREND', 'DOWNTREND', or 'SIDEWAYS'
    """
    if ema200 == 0:
        return "SIDEWAYS"

    diff_pct = abs(ema50 - ema200) / ema200 * 100

    if diff_pct < 1:  # Within 1% = sideways
        return "SIDEWAYS"
    elif ema50 > ema200:
        return "UPTREND"
    else:
        return "DOWNTREND"


def find_support_resistance(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    lookback: int = 200
) -> dict:
    """
    Find support and resistance levels.

    Args:
        closes: List of closing prices
        highs: List of high prices
        lows: List of low prices
        lookback: Number of candles to look back

    Returns:
        Dict with high, low, position_in_range
    """
    if lookback > len(closes):
        lookback = len(closes)

    recent_closes = closes[-lookback:]
    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]

    high_200 = max(recent_highs)
    low_200 = min(recent_lows)
    current = closes[-1]

    # Position in range (0% = at support, 100% = at resistance)
    if high_200 == low_200:
        position = 50
    else:
        position = (current - low_200) / (high_200 - low_200) * 100

    return {
        "high": high_200,
        "low": low_200,
        "position_in_range": position,
        "current": current,
    }


def calculate_momentum(closes: list[float], periods: list[int] = None) -> dict:
    """
    Calculate price momentum over various periods.

    Args:
        closes: List of closing prices
        periods: List of periods to calculate (default: [24, 168])

    Returns:
        Dict with momentum for each period
    """
    if periods is None:
        periods = [24, 168]  # 24h (1h candles), 168h (7d)

    result = {}
    current = closes[-1]

    for period in periods:
        if len(closes) <= period:
            result[f"change_{period}h"] = None
            continue

        past = closes[-period]
        change_pct = (current - past) / past * 100
        result[f"change_{period}h"] = change_pct

    return result


def calculate_volatility(closes: list[float], period: int = 30) -> float:
    """
    Calculate volatility (standard deviation / mean).

    Args:
        closes: List of closing prices
        period: Period for calculation

    Returns:
        Volatility as percentage
    """
    if len(closes) < period:
        period = len(closes)

    if period < 2:
        return 0

    recent = closes[-period:]
    mean = sum(recent) / period

    # Calculate standard deviation
    variance = sum((x - mean) ** 2 for x in recent) / period
    std_dev = math.sqrt(variance)

    volatility = (std_dev / mean) * 100
    return volatility


def calculate_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14
) -> float:
    """
    Calculate Average True Range.

    Args:
        highs: List of high prices
        lows: List of low prices
        closes: List of closing prices
        period: ATR period

    Returns:
        ATR value
    """
    if len(highs) < period + 1:
        return 0

    true_ranges = []
    for i in range(1, len(highs)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])
        true_range = max(high_low, high_close, low_close)
        true_ranges.append(true_range)

    # Use last 'period' values
    atr = sum(true_ranges[-period:]) / period
    return atr


def get_signal(ema50: float, ema200: float, rsi: float = None) -> dict:
    """
    Generate trading signal based on indicators.

    Args:
        ema50: 50-period EMA
        ema200: 200-period EMA
        rsi: Optional RSI value

    Returns:
        Dict with signal type and reasoning
    """
    signals = []
    reasons = []

    # EMA crossover signals
    if ema50 is None or ema200 is None:
        diff_pct = 0
    else:
        diff_pct = (ema50 - ema200) / ema200 * 100

    if ema200 is not None and diff_pct > 2:
        signals.append("BUY")
        reasons.append("Golden Cross (EMA50 above EMA200)")
    elif ema200 is not None and diff_pct < -2:
        signals.append("SELL")
        reasons.append("Death Cross (EMA50 below EMA200)")

    # RSI signals
    if rsi is not None:
        if rsi < 30:
            signals.append("OVERSOLD")
            reasons.append(f"RSI at {rsi:.1f}")
        elif rsi > 70:
            signals.append("OVERBOUGHT")
            reasons.append(f"RSI at {rsi:.1f}")

    # Overextension
    if ema200 is not None and diff_pct > 5:
        signals.append("CAUTION")
        reasons.append("Price overextended above EMA200")
    elif ema200 is not None and diff_pct < -5:
        signals.append("CAUTION")
        reasons.append("Price overextended below EMA200")

    # Determine primary signal
    if "SELL" in signals:
        primary = "SELL"
    elif "BUY" in signals:
        primary = "BUY"
    elif "CAUTION" in signals:
        primary = "CAUTION"
    else:
        primary = "NEUTRAL"

    return {
        "signal": primary,
        "reasons": reasons,
        "ema_diff_pct": diff_pct,
    }


def analyze_symbol(
    symbol: str,
    candles: list[dict]
) -> dict:
    """
    Complete technical analysis for a symbol.

    Args:
        symbol: Trading pair
        candles: List of candle dicts

    Returns:
        Complete analysis dict
    """
    # Input validation
    if not candles:
        return {"symbol": symbol, "error": "No candles provided"}

    # Validate candle structure
    required_keys = {"close", "high", "low"}
    for i, c in enumerate(candles[:5]):  # Check first 5 candles
        if not isinstance(c, dict):
            return {"symbol": symbol, "error": f"Candle {i} is not a dict"}
        missing = required_keys - set(c.keys())
        if missing:
            return {"symbol": symbol, "error": f"Candle {i} missing keys: {missing}"}

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    # Validate numeric values
    if not all(isinstance(x, (int, float)) for x in closes):
        return {"symbol": symbol, "error": "Non-numeric values in candles"}

    # Calculate indicators
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200) if len(closes) >= 200 else None
    rsi = calculate_rsi(closes, 14)

    # Support/Resistance
    sr = find_support_resistance(closes, highs, lows)

    # Momentum
    momentum = calculate_momentum(closes)

    # Volatility
    volatility = calculate_volatility(closes)

    # Trend
    trend = detect_trend(ema50, ema200) if ema200 else "SIDEWAYS"

    # Signal
    signal = get_signal(ema50, ema200, rsi) if ema200 else {"signal": "NEUTRAL", "reasons": ["No EMA200 yet"]}

    return {
        "symbol": symbol,
        "current_price": closes[-1],
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "trend": trend,
        "signal": signal["signal"],
        "signal_reasons": signal["reasons"],
        "resistance": sr["high"],
        "support": sr["low"],
        "position_in_range": sr["position_in_range"],
        "momentum": momentum,
        "volatility": volatility,
    }
