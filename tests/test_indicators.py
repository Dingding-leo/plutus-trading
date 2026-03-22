"""
Tests for src/analysis/indicators.py
"""
import pytest
import math
from src.analysis.indicators import (
    calculate_ema,
    calculate_sma,
    calculate_rsi,
    detect_trend,
    find_support_resistance,
    calculate_volatility,
    calculate_momentum,
    calculate_atr,
    get_signal,
    analyze_symbol,
)


class TestCalculateEMA:
    def test_ema_below_period_raises(self):
        with pytest.raises(ValueError, match="Not enough data"):
            calculate_ema([1, 2, 3], period=50)

    def test_ema_exact_period_accepted(self):
        prices = list(range(1, 51))
        result = calculate_ema(prices, period=50)
        assert isinstance(result, float)

    def test_ema_correct_value(self):
        # All identical prices → EMA = that price
        prices = [100.0] * 200
        result = calculate_ema(prices, period=50)
        assert abs(result - 100.0) < 0.01

    def test_ema_rising_prices(self):
        prices = list(range(50, 250))
        result = calculate_ema(prices, period=50)
        assert result > 150  # EMA of rising series

    def test_ema_falling_prices(self):
        prices = list(range(200, 50, -1))
        result = calculate_ema(prices, period=50)
        assert result < 150  # EMA of falling series


class TestCalculateSMA:
    def test_sma_below_period_raises(self):
        with pytest.raises(ValueError, match="Not enough data"):
            calculate_sma([1, 2, 3], period=50)

    def test_sma_exact_period_accepted(self):
        prices = list(range(1, 11))
        result = calculate_sma(prices, period=10)
        assert isinstance(result, float)

    def test_sma_simple_average(self):
        prices = [1.0] * 10
        result = calculate_sma(prices, period=10)
        assert result == 1.0

    def test_sma_calculation(self):
        prices = [10.0, 20.0, 30.0]
        result = calculate_sma(prices, period=3)
        assert result == 20.0


class TestCalculateRSI:
    def test_rsi_below_period_plus_one_raises(self):
        # Need period + 1 candles for RSI
        with pytest.raises(ValueError, match="Not enough data"):
            calculate_rsi([1, 2, 3], period=14)

    def test_rsi_exact_period_plus_one_accepted(self):
        prices = list(range(1, 16))
        result = calculate_rsi(prices, period=14)
        # Result must be numeric and in valid range
        assert isinstance(result, (int, float))
        assert 0 <= result <= 100

    def test_rsi_all_gains_returns_100(self):
        prices = [100 + i for i in range(15)]  # Strictly increasing
        result = calculate_rsi(prices, period=14)
        assert result == 100.0

    def test_rsi_all_losses_returns_0(self):
        prices = [100 - i for i in range(15)]  # Strictly decreasing
        result = calculate_rsi(prices, period=14)
        assert result == 0.0

    def test_rsi_returns_numeric_in_valid_range(self):
        prices = [100 + i + (i % 2) for i in range(15)]
        result = calculate_rsi(prices, period=14)
        # Result must be numeric and in valid range (handle int vs float)
        assert isinstance(result, (int, float))
        assert 0 <= result <= 100

    def test_rsi_default_period_is_14(self):
        prices = [100 + i for i in range(20)]
        result = calculate_rsi(prices)  # No period specified → default 14
        assert 0 <= result <= 100


class TestDetectTrend:
    def test_uptrend(self):
        assert detect_trend(ema50=105, ema200=100) == "UPTREND"

    def test_downtrend(self):
        assert detect_trend(ema50=95, ema200=100) == "DOWNTREND"

    def test_sideways_near_cross(self):
        assert detect_trend(ema50=100.5, ema200=100) == "SIDEWAYS"

    def test_sideways_when_ema200_zero(self):
        assert detect_trend(ema50=100, ema200=0) == "SIDEWAYS"

    def test_sideways_at_cross(self):
        # diff_pct < 1% → sideways
        assert detect_trend(ema50=100.4, ema200=100) == "SIDEWAYS"


class TestFindSupportResistance:
    def test_basic_sr(self, sample_candles_200):
        closes = [c["close"] for c in sample_candles_200]
        highs = [c["high"] for c in sample_candles_200]
        lows = [c["low"] for c in sample_candles_200]

        result = find_support_resistance(closes, highs, lows)
        assert "high" in result
        assert "low" in result
        assert "position_in_range" in result
        assert result["high"] >= result["low"]

    def test_position_in_range_bounds(self, sample_candles_200):
        closes = [c["close"] for c in sample_candles_200]
        highs = [c["high"] for c in sample_candles_200]
        lows = [c["low"] for c in sample_candles_200]

        result = find_support_resistance(closes, highs, lows)
        assert 0 <= result["position_in_range"] <= 100

    def test_lookback_truncation(self):
        closes = list(range(100, 200))
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        # lookback=200 exceeds len(100) → uses all available
        result = find_support_resistance(closes, highs, lows, lookback=200)
        assert result["high"] == max(highs)

    def test_includes_current_price(self):
        closes = list(range(100, 200))
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        result = find_support_resistance(closes, highs, lows)
        assert result["current"] == closes[-1]


class TestCalculateVolatility:
    def test_volatility_positive(self, sample_candles_200):
        closes = [c["close"] for c in sample_candles_200]
        result = calculate_volatility(closes)
        assert result > 0

    def test_volatility_zero_for_flat(self):
        result = calculate_volatility([100.0] * 30)
        assert result == 0.0

    def test_volatility_short_period_defaults_to_all_data(self):
        result = calculate_volatility([100.0] * 5)
        assert result == 0.0

    def test_volatility_handles_single_element(self):
        result = calculate_volatility([100.0])
        assert result == 0.0


class TestCalculateMomentum:
    def test_momentum_change_24h(self):
        closes = list(range(0, 50))  # 0..49
        result = calculate_momentum(closes, periods=[24])
        assert result["change_24h"] is not None
        assert result["change_24h"] > 0  # Prices rising

    def test_momentum_insufficient_data_returns_none(self):
        closes = list(range(10))
        result = calculate_momentum(closes, periods=[24])
        assert result["change_24h"] is None

    def test_momentum_default_periods(self):
        closes = list(range(200))
        result = calculate_momentum(closes)
        assert "change_24h" in result
        assert "change_168h" in result


class TestCalculateATR:
    def test_atr_returns_positive(self, sample_candles_200):
        highs = [c["high"] for c in sample_candles_200]
        lows = [c["low"] for c in sample_candles_200]
        closes = [c["close"] for c in sample_candles_200]
        result = calculate_atr(highs, lows, closes, period=14)
        assert result > 0

    def test_atr_insufficient_data_returns_zero(self):
        result = calculate_atr([100], [99], [98], period=14)
        assert result == 0

    def test_atr_default_period(self):
        highs = [100 + i for i in range(30)]
        lows = [90 + i for i in range(30)]
        closes = [95 + i for i in range(30)]
        result = calculate_atr(highs, lows, closes)
        assert result > 0


class TestGetSignal:
    def test_golden_cross_buy_signal(self):
        # diff_pct > 2% → BUY signal
        result = get_signal(ema50=105, ema200=100, rsi=50)
        assert result["signal"] == "BUY"

    def test_death_cross_sell_signal(self):
        # diff_pct < -2% → SELL signal
        result = get_signal(ema50=95, ema200=100, rsi=50)
        assert result["signal"] == "SELL"

    def test_rsi_oversold_not_flagged_as_buy(self):
        # RSI=10 is oversold; signal should NOT be BUY (which would be contradictory)
        result = get_signal(ema50=100, ema200=100, rsi=10)
        assert result["signal"] != "BUY"  # oversold contradicts a BUY signal
        assert "RSI at 10.0" in result["reasons"]

    def test_rsi_overbought_not_flagged_as_sell(self):
        # RSI=90 is overbought; signal should NOT be SELL (which would be contradictory)
        result = get_signal(ema50=100, ema200=100, rsi=90)
        assert result["signal"] != "SELL"  # overbought contradicts a SELL signal
        assert "RSI at 90.0" in result["reasons"]

    def test_neutral_when_no_signal(self):
        # diff_pct within 2% and RSI neutral
        result = get_signal(ema50=100, ema200=100, rsi=50)
        assert result["signal"] == "NEUTRAL"

    def test_caution_overextended_in_reasons(self):
        # diff_pct = 6% (> 5%) → "CAUTION" in signals and reasons
        result = get_signal(ema50=106, ema200=100, rsi=50)
        # "CAUTION" in signals; "Price overextended..." in reasons
        assert "Price overextended above EMA200" in result["reasons"]

    def test_signal_priority_buy_over_caution(self):
        # diff_pct > 2% → BUY; also diff_pct > 5% → CAUTION
        # BUY takes priority over CAUTION
        result = get_signal(ema50=106, ema200=100, rsi=50)
        assert result["signal"] == "BUY"


class TestAnalyzeSymbol:
    def test_analyze_symbol_returns_dict(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        assert isinstance(result, dict)
        assert result["symbol"] == "BTCUSDT"

    def test_analyze_symbol_includes_all_fields(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        expected_fields = [
            "symbol", "current_price", "ema50", "ema200", "rsi",
            "trend", "signal", "signal_reasons", "resistance",
            "support", "position_in_range", "momentum", "volatility",
        ]
        for field in expected_fields:
            assert field in result, f"Missing field: {field}"

    def test_analyze_symbol_empty_candles_returns_error(self):
        result = analyze_symbol("BTCUSDT", [])
        assert "error" in result

    def test_analyze_symbol_missing_keys_returns_error(self):
        result = analyze_symbol("BTCUSDT", [{"open": 100}])
        assert "error" in result

    def test_analyze_symbol_non_dict_candle_returns_error(self):
        result = analyze_symbol("BTCUSDT", ["not a dict"])
        assert "error" in result

    def test_analyze_symbol_trend_valid(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        assert result["trend"] in ["UPTREND", "DOWNTREND", "SIDEWAYS"]

    def test_analyze_symbol_signal_valid(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        assert result["signal"] in ["BUY", "SELL", "NEUTRAL", "CAUTION"]

    def test_analyze_symbol_ema_values_positive(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        assert result["ema50"] > 0
        assert result["ema200"] > 0

    def test_analyze_symbol_rsi_in_valid_range(self, sample_candles_200):
        result = analyze_symbol("BTCUSDT", sample_candles_200)
        assert 0 <= result["rsi"] <= 100
