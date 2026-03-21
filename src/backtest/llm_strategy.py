"""
LLM-Powered Strategy - Following TRADING_WORKFLOW.md with LLM analysis.
"""

import os
import json
from datetime import datetime
from typing import Dict, List, Optional
from .engine import BacktestEngine, TradeDirection
from ..analysis import indicators
from ..execution import position_sizer
from ..data.llm_client import analyze_market
from ..data import coingecko_client
from ..data.coin_tiers import is_major, normalize_symbol
from .decision_logger import save_llm_decision, save_backtest_result

# LLM Decision Logger
LLM_LOG_FILE = "logs/llm_decisions.json"

def log_llm_decision(decision: dict, symbol: str, result: str = None):
    """Log LLM decision for future analysis."""
    os.makedirs("logs", exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "symbol": symbol,
        "decision": decision.get("decision"),
        "order_type": decision.get("order_type"),
        "entry_price": decision.get("limit_price") or "MARKET",
        "stop_loss": decision.get("stop_loss"),
        "take_profit": decision.get("take_profit"),
        "invalidation": decision.get("invalidation"),
        "rr": decision.get("rr"),
        "risk_level": decision.get("risk_level"),
        "reason": decision.get("reason"),
        "result": result,  # WIN/LOSS
    }
    try:
        with open(LLM_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


class LLMWorkflowStrategy:
    """
    Strategy using LLM for market analysis as per TRADING_WORKFLOW.md.
    """

    def __init__(
        self,
        risk_pct: float = 0.02,
        max_leverage: float = 50,
        use_llm: bool = True,
        llm_interval: int = 4,  # Deprecated, use llm_trigger instead
        llm_trigger: str = "level",  # "level", "interval", or "both"
        level_threshold: float = 0.005,  # 0.5% from highs/lows triggers LLM
    ):
        self.risk_pct = risk_pct
        self.max_leverage = max_leverage
        self.use_llm = use_llm
        self.llm_interval = llm_interval
        self.llm_trigger = llm_trigger
        self.level_threshold = level_threshold

        # Track candles for LLM
        self.candle_count = 0
        self.last_llm_decision = None

        # BTC > ETH > ALT enforcement
        self.btc_trend = "SIDEWAYS"
        self.btc_strength = "neutral"

        # LLM conditional triggers
        self.last_trend = None
        self.last_price = None
        self.last_rsi = None
        self.last_support = None
        self.last_resistance = None

        # Track open trades for logging
        self.open_trade_decisions = {}  # symbol -> LLM decision

        # Fear & Greed cache (only updates once per day)
        self._fear_greed_value = 50  # Default to neutral
        self._fear_greed_last_fetch = None

    def _should_trigger_llm(self, candles: list, analysis: dict) -> bool:
        """
        Conditional triggers for LLM based on config.

        Config options:
        - "level": Trigger when price near key levels (support/resistance/highs/lows)
        - "interval": Trigger every N candles (legacy)
        - "both": Trigger on either condition
        """
        # First run - always trigger
        if self.last_trend is None:
            return True

        if self.llm_trigger == "level":
            return self._should_trigger_llm_at_level(candles, analysis)

        elif self.llm_trigger == "interval":
            self.candle_count += 1
            return self.candle_count % self.llm_interval == 0

        else:  # "both"
            # Check level trigger
            level_trigger = self._should_trigger_llm_at_level(candles, analysis)

            # Check interval trigger
            self.candle_count += 1
            interval_trigger = self.candle_count % self.llm_interval == 0

            return level_trigger or interval_trigger

    def _should_trigger_llm_at_level(self, candles: list, analysis: dict) -> bool:
        """
        Trigger LLM when price is near key levels.

        Triggers:
        - Price within threshold of recent high (near resistance)
        - Price within threshold of recent low (near support)
        - Price within threshold of S/R levels
        """
        if not candles or not analysis:
            return False

        current = analysis.get("current", 0)
        if current <= 0:
            return False

        threshold = self.level_threshold

        # Get recent highs/lows
        recent_candles = candles[-20:] if len(candles) >= 20 else candles
        highs = [c["high"] for c in recent_candles]
        lows = [c["low"] for c in recent_candles]

        if not highs or not lows:
            return False

        recent_high = max(highs)
        recent_low = min(lows)

        # Check near recent high (resistance)
        if current >= recent_high * (1 - threshold):
            return True

        # Check near recent low (support)
        if current <= recent_low * (1 + threshold):
            return True

        # Check near S/R levels from analysis
        support = analysis.get("support")
        resistance = analysis.get("resistance")

        if support and current <= support * (1 + threshold):
            return True

        if resistance and current >= resistance * (1 - threshold):
            return True

        return False

    def _update_btc_context(self, data: Dict[str, List[dict]]):
        """Update BTC context for BTC > ETH > ALT enforcement."""
        # Data structure is {'BTCUSDT': {'1h': [...]}, ...}
        btc_data = data.get("BTCUSDT", {})
        btc_candles = btc_data.get("1h", [])
        if btc_candles:
            btc_analysis = self.analyze(btc_candles)
            if btc_analysis:
                self.btc_trend = btc_analysis.get("trend", "SIDEWAYS")
                if self.btc_trend == "UPTREND":
                    self.btc_strength = "strength"
                elif self.btc_trend == "DOWNTREND":
                    self.btc_strength = "weakness"
                else:
                    self.btc_strength = "neutral"

    def _can_trade_alt(self, symbol: str) -> bool:
        """Enforce BTC > ETH > ALT rule."""
        n = normalize_symbol(symbol)
        if n == "BTCUSDT":
            return True
        elif n == "ETHUSDT":
            # ETH allowed if BTC is not weak
            return self.btc_strength != "weakness"
        else:
            # ALT coins: allowed if BTC is NOT weak
            return self.btc_strength != "weakness"

    def analyze(self, candles: List[dict], period: int = 200) -> Optional[dict]:
        """Technical analysis for given period."""
        if len(candles) < period:
            return None

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current = closes[-1]

        try:
            ema50 = indicators.calculate_ema(closes, 50)
            ema200 = indicators.calculate_ema(closes, 200)
            rsi = indicators.calculate_rsi(closes, 14)
        except Exception:
            return None

        trend = indicators.detect_trend(ema50, ema200)
        sr = indicators.find_support_resistance(closes, highs, lows)

        return {
            "trend": trend,
            "signal": indicators.get_signal(ema50, ema200, rsi)["signal"],
            "rsi": rsi,
            "current": current,
            "support": sr["low"],
            "resistance": sr["high"],
            "ema50": ema50,
            "ema200": ema200,
        }

    def _find_swing_stop(
        self,
        candles: List[dict],
        direction: TradeDirection,
        entry: float,
        min_dist: float = 0.005,
        max_dist: float = 0.025,
        lookback: int = 80,
    ) -> Optional[float]:
        if not candles or entry <= 0:
            return None

        recent = candles[-lookback:] if len(candles) > lookback else candles
        if direction == TradeDirection.LONG:
            lows = [c.get("low") for c in recent if isinstance(c.get("low"), (int, float))]
            candidates = [p for p in lows if p < entry and min_dist <= (entry - p) / entry <= max_dist]
            return max(candidates) if candidates else None
        else:
            highs = [c.get("high") for c in recent if isinstance(c.get("high"), (int, float))]
            candidates = [p for p in highs if p > entry and min_dist <= (p - entry) / entry <= max_dist]
            return min(candidates) if candidates else None

    def get_multi_tf_analysis(self, symbol: str, data: Dict[str, List[dict]]) -> dict:
        """Get multi-timeframe analysis including 5m."""
        result = {}
        timeframes = ["5m", "15m", "1h", "4h"]

        for tf in timeframes:
            candles = data.get(symbol, {}).get(tf, [])
            if candles:
                # Get recent 50 candles for this timeframe
                recent = candles[-50:] if len(candles) >= 50 else candles
                analysis = self.analyze(recent)
                if analysis:
                    result[tf] = analysis

        return result

    def _get_fear_greed(self):
        """Get Fear & Greed index with caching (only updates once per day).

        Returns:
            int: Fear & Greed value (0-100)
            None: If unavailable
        """
        from datetime import datetime, timedelta

        now = datetime.now()
        # Check if we need to fetch new data (cache for 24 hours)
        if self._fear_greed_last_fetch is None or (now - self._fear_greed_last_fetch) > timedelta(hours=24):
            try:
                fg = coingecko_client.get_fear_greed_index()
                if fg and fg.get("value"):
                    self._fear_greed_value = fg["value"]
                    self._fear_greed_last_fetch = now
                    print(f"Fetched Fear & Greed: {self._fear_greed_value}")
                else:
                    self._fear_greed_value = self._fear_greed_value if self._fear_greed_value is not None else 50
                    self._fear_greed_last_fetch = now
            except Exception as e:
                print(f"Failed to fetch Fear & Greed: {e}")
                self._fear_greed_value = self._fear_greed_value if self._fear_greed_value is not None else 50

        return self._fear_greed_value

    def get_volume_profile_zone(self, candles: List[dict]) -> str:
        """
        Determine if price is in HVN or LVN based on volume profile.
        """
        if not candles or len(candles) < 20:
            return "MID_RANGE"

        try:
            from ..analysis import volume_profile
            levels = volume_profile.get_key_levels(candles[-50:])
            current = candles[-1]["close"]

            # Check if near HVN (high volume = resistance)
            hvn_levels = levels.get("hvn", [])
            lvn_levels = levels.get("lvn", [])

            # Simple check: if close to HVN, it's in HVN zone
            for hvn in hvn_levels:
                if abs(current - hvn) / current < 0.02:  # within 2%
                    return "HVN"

            for lvn in lvn_levels:
                if abs(current - lvn) / current < 0.02:  # within 2%
                    return "LVN"

            return "MID_RANGE"
        except Exception:
            return "MID_RANGE"

    def execute(
        self,
        engine: BacktestEngine,
        symbol: str,
        data: Dict[str, List[dict]],
        timestamp: datetime,
        ts_int: int
    ):
        """Execute strategy."""
        # Update BTC context for BTC > ETH > ALT enforcement
        self._update_btc_context(data)

        # Check if we can trade this symbol
        if not self._can_trade_alt(symbol):
            print(f'DEBUG: {symbol} blocked by BTC>ETH>ALT')
            return  # Blocked by BTC > ETH > ALT rule

        # Data structure is {'SYMBOL': {'1h': [...]}}
        symbol_data = data.get(symbol, {})
        candles = symbol_data.get("1h", [])
        if len(candles) < 200:
            print(f'DEBUG: {symbol} not enough candles: {len(candles)}')
            return

        self.candle_count += 1

        # Analyze
        analysis = self.analyze(candles)
        if not analysis:
            return

        current = analysis["current"]

        # Check open positions
        if symbol in engine.open_trades:
            trade = engine.open_trades[symbol]
            reason = engine.check_stop_take(symbol, current, timestamp)

            # Check if trade was closed
            if symbol not in engine.open_trades:
                # Trade closed - log result
                pnl = trade.pnl
                result = "WIN" if pnl > 0 else "LOSS"
                if symbol in self.open_trade_decisions:
                    decision = self.open_trade_decisions.pop(symbol)
                    decision["result"] = result
                    decision["pnl"] = pnl
                    log_llm_decision(decision, symbol, result)
                    print(f"Trade {symbol}: {result} | PnL: ${pnl:.2f}")

            # Exit on trend reversal
            elif analysis["trend"] == "DOWNTREND" and trade.direction == TradeDirection.LONG:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            elif analysis["trend"] == "UPTREND" and trade.direction == TradeDirection.SHORT:
                engine.close_trade(symbol, current, timestamp, "TREND_REVERSAL")
            return

        # Use LLM for BTC/ETH, technical signals for ALTs
        # Use conditional triggers for LLM instead of numbered intervals
        llm_triggered = False
        llm_result = None
        entry_price = None  # Track if we have an entry price from LLM

        # BTC and ETH always use LLM if enabled
        # ALTs use technical signals (or could also use LLM)
        is_btc_eth = is_major(symbol)

        # Default: use technical signals
        use_technical_signals = True

        if is_btc_eth and self.use_llm and self._should_trigger_llm(candles, analysis):
            # Try LLM first for BTC/ETH
            use_technical_signals = False

            # Get Fear & Greed (cached - only fetches once per day)
            fear_greed = self._get_fear_greed()

            # Get BTC context for macro
            btc_data = analysis if symbol == "BTCUSDT" else self._get_btc_analysis(candles, engine)

            # Get TARGET symbol analysis (this is what we're trading!)
            target_data = analysis  # This is the symbol we're actually trading

            # Get multi-timeframe for TARGET symbol (not BTC!)
            multi_tf = self.get_multi_tf_analysis(symbol, data)

            # Get volume profile for TARGET symbol
            volume_zone = self.get_volume_profile_zone(candles)

            llm_result = analyze_market(
                btc_data={"current_price": btc_data.get("current", 0), "trend": btc_data.get("trend", "SIDEWAYS"), "rsi": btc_data.get("rsi", 50), "signal": btc_data.get("signal", "NEUTRAL"), "support": btc_data.get("support", 0), "resistance": btc_data.get("resistance", 0)},
                target_data={"current_price": target_data.get("current", 0), "trend": target_data.get("trend", "SIDEWAYS"), "rsi": target_data.get("rsi", 50), "support": target_data.get("support", 0), "resistance": target_data.get("resistance", 0)},
                market_overview={"fear_greed_index": fear_greed if fear_greed is not None else "NA"},
                multi_tf=multi_tf,
                volume_zone=volume_zone,
            )

            self.last_llm_decision = llm_result

            # Log EVERY LLM decision (including NO_TRADE)
            candle_data = {
                "datetime": timestamp.isoformat() if timestamp else None,
                "current_price": current,
                "trend": analysis.get("trend") if analysis else None,
                "rsi": analysis.get("rsi") if analysis else None,
                "support": analysis.get("support") if analysis else None,
                "resistance": analysis.get("resistance") if analysis else None,
                "fear_greed": fear_greed,  # Real value or None
            }
            save_llm_decision(llm_result, symbol, candle_data)

            # Execute LLM decision
            if llm_result.get("decision") == "BUY":
                direction = TradeDirection.LONG
            elif llm_result.get("decision") == "SELL":
                direction = TradeDirection.SHORT
            else:
                # LLM said NO_TRADE - fall back to technical signals
                print(f"LLM said NO_TRADE for {symbol} - falling back to technical signals")
                use_technical_signals = True

            # Only proceed with LLM if we have a valid direction
            if not use_technical_signals:
                # Get order type from LLM
                order_type = llm_result.get("order_type", "MARKET")
                limit_price = llm_result.get("limit_price")

                # Get SL/TP from LLM
                llm_stop_loss = llm_result.get("stop_loss")
                llm_take_profit = llm_result.get("take_profit")
                llm_rr = llm_result.get("rr", 0)

                # Verify RR >= 1.5 (TRADING_WORKFLOW.md requirement)
                try:
                    rr_value = float(llm_rr) if llm_rr else 0
                except Exception:
                    rr_value = 0
                if rr_value < 1.5:
                    print(f"LLM RR too low: {llm_rr} < 1.5 - falling back to technical")
                    use_technical_signals = True
                else:
                    # Use limit price if LLM specified it, otherwise use current
                    if order_type == "LIMIT" and limit_price:
                        entry_price = limit_price
                    else:
                        entry_price = current

                    # Log LLM decision with full details
                    reason = llm_result.get("reason", "")
                    sl = llm_result.get("stop_loss", "N/A")
                    tp = llm_result.get("take_profit", "N/A")
                    inv = llm_result.get("invalidation", "N/A")
                    print(f"LLM TRADE: {direction.value} {symbol} | Entry: {entry_price} | SL: {sl} | TP: {tp} | RR: {rr_value}x | Inv: {inv}")

        if use_technical_signals:
            # Use technical signals (fallback for BTC/ETH, or primary for ALTs)
            if analysis["trend"] == "UPTREND" and analysis["rsi"] < 65:
                direction = TradeDirection.LONG
            elif analysis["trend"] == "DOWNTREND" and analysis["rsi"] > 35:
                direction = TradeDirection.SHORT
            else:
                return  # No trade signal

            # For ALTs with LLM enabled, also call LLM for confirmation (optional - skip for now)
            if self.use_llm and not is_btc_eth:
                # Skip LLM confirmation for ALTs to simplify - use pure technical
                pass

        # Calculate stop/target
        stop_pct = 0.02

        # Use entry_price from LLM if set, otherwise use current
        if entry_price is not None:
            entry = entry_price
        else:
            entry = current

        # ===== ENGINE VALIDATION (not trusting LLM arithmetic) =====
        MIN_STOP_DIST = 0.005  # 0.5%
        MAX_STOP_DIST = 0.025  # 2.5%
        MIN_RR = 1.5

        support = analysis.get("support") if analysis else None
        resistance = analysis.get("resistance") if analysis else None
        loc_guard = 0.01
        if direction == TradeDirection.LONG and isinstance(resistance, (int, float)) and resistance > 0:
            if (resistance - entry) / entry < loc_guard:
                return
        if direction == TradeDirection.SHORT and isinstance(support, (int, float)) and support > 0:
            if (entry - support) / entry < loc_guard:
                return

        swing_stop = self._find_swing_stop(
            candles=candles,
            direction=direction,
            entry=entry,
            min_dist=MIN_STOP_DIST,
            max_dist=MAX_STOP_DIST,
        )
        if swing_stop is None:
            return

        llm_take_profit = llm_result.get("take_profit") if llm_result else None
        stop = swing_stop
        if isinstance(llm_take_profit, (int, float)):
            candidate_target = llm_take_profit
        else:
            candidate_target = None

        if direction == TradeDirection.LONG:
            min_target = entry + (entry - stop) * MIN_RR
            if candidate_target is None or candidate_target <= entry:
                target = min_target
            else:
                target = candidate_target if (candidate_target - entry) / (entry - stop) >= MIN_RR else min_target
        else:
            min_target = entry - (stop - entry) * MIN_RR
            if candidate_target is None or candidate_target >= entry:
                target = min_target
            else:
                target = candidate_target if (entry - candidate_target) / (stop - entry) >= MIN_RR else min_target

        actual_stop_dist = None
        actual_rr = None

        # Validate stop loss is on correct side and calculate engine RR
        if direction == TradeDirection.LONG:
            if stop and stop >= entry:
                print(f"REJECT: Long stop {stop} must be below entry {entry}")
                return
            # Calculate actual RR from ENGINE (not LLM)
            if stop and target:
                actual_stop_dist = abs(entry - stop) / entry
                actual_rr = abs(target - entry) / abs(entry - stop)
                # Validate stop distance (0.5% - 2.5%)
                if actual_stop_dist < MIN_STOP_DIST:
                    print(f"REJECT: Long stop distance {actual_stop_dist*100:.2f}% < 0.5% min")
                    return
                if actual_stop_dist > MAX_STOP_DIST:
                    print(f"REJECT: Long stop distance {actual_stop_dist*100:.2f}% > 2.5% max")
                    return
                # Validate RR
                if actual_rr < (MIN_RR - 1e-9):
                    print(f"REJECT: Long RR {actual_rr:.2f} < 1.5 (SL: {stop}, TP: {target})")
                    return
        else:  # SHORT
            if stop and stop <= entry:
                print(f"REJECT: Short stop {stop} must be above entry {entry}")
                return
            # Calculate actual RR from ENGINE (not LLM)
            if stop and target:
                actual_stop_dist = abs(entry - stop) / entry
                actual_rr = abs(entry - target) / abs(stop - entry)
                # Validate stop distance (0.5% - 2.5%)
                if actual_stop_dist < MIN_STOP_DIST:
                    print(f"REJECT: Short stop distance {actual_stop_dist*100:.2f}% < 0.5% min")
                    return
                if actual_stop_dist > MAX_STOP_DIST:
                    print(f"REJECT: Short stop distance {actual_stop_dist*100:.2f}% > 2.5% max")
                    return
                # Validate RR
                if actual_rr < (MIN_RR - 1e-9):
                    print(f"REJECT: Short RR {actual_rr:.2f} < 1.5 (SL: {stop}, TP: {target})")
                    return

        # Position sizing
        coin_type = "major" if is_major(symbol) else "small"
        position = position_sizer.calculate_position_size(
            equity=engine.equity,
            risk_pct=self.risk_pct,
            stop_distance=actual_stop_dist if actual_stop_dist else stop_pct,
            pos_mult=1.0,
            coin_type=coin_type,
            training_mode=False,
        )

        if not position["valid"]:
            return

        size = position["max_position"] / entry
        leverage = min(position["recommended_leverage"], self.max_leverage)

        engine.open_trade(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            size=size,
            leverage=leverage,
            stop_loss=stop,
            take_profit=target,
            timestamp=timestamp
        )

        # Track decision for logging when trade closes
        if llm_result:
            self.open_trade_decisions[symbol] = llm_result

    def _get_btc_analysis(self, candles, engine):
        """Get BTC analysis for ETH decisions."""
        return {"current": 0, "trend": "SIDEWAYS", "rsi": 50, "signal": "NEUTRAL", "support": 0, "resistance": 0}

    def _get_eth_analysis(self, candles, engine):
        """Get ETH analysis."""
        return self.analyze(candles)


def run_llm_backtest(
    symbols=None,
    start_date='2025-01-01',  # Fixed: was '2025-09-01' which was only 6 months!
    end_date='2026-01-01',
    initial_equity=10000,
    **kwargs
):
    """Run LLM-powered backtest."""
    from .simple_fetch import fetch_binance_history
    from .engine import format_results

    if symbols is None:
        from ..data.coin_tiers import ALL_TIERS
        symbols = ALL_TIERS[:30]  # Top 30 coins like production_strategy

    print(f"Running backtest: {start_date} to {end_date} ({len(symbols)} symbols)")

    # Fetch data
    timeframes = ["5m", "15m", "1h", "4h"]
    data = {}
    for sym in symbols:
        data[sym] = {}
        for tf in timeframes:
            print(f'Fetching {sym} {tf}...')
            data[sym][tf] = fetch_binance_history(sym, tf, start_date, end_date, 5000)

    engine = BacktestEngine(initial_equity)
    strategy = LLMWorkflowStrategy(**kwargs)

    min_len = min(len(data[s].get("1h", [])) for s in symbols)
    pointers = {sym: {tf: 0 for tf in timeframes} for sym in symbols}

    for i in range(200, min_len):  # Need 200 candles for EMA200
        ts = data[symbols[0]]["1h"][i]['timestamp']
        current_time = data[symbols[0]]["1h"][i]['datetime']

        # Build full data dict for all symbols (needed for BTC > ETH > ALT)
        full_data = {}
        for sym in symbols:
            sym_key = normalize_symbol(sym)
            full_data[sym_key] = {}
            for tf in timeframes:
                candles = data[sym].get(tf, [])
                j = pointers[sym][tf]
                while j < len(candles) and candles[j].get("timestamp") is not None and candles[j]["timestamp"] <= ts:
                    j += 1
                pointers[sym][tf] = j
                full_data[sym_key][tf] = candles[:j]

        for sym in symbols:
            sym_key = normalize_symbol(sym)
            strategy.execute(engine, sym_key, full_data, current_time, ts)

    result = engine.get_results()

    # Save backtest result
    result_summary = {
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "win_rate": result.win_rate,
        "total_pnl": result.total_pnl,
        "total_pnl_pct": result.total_pnl_pct,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "profit_factor": result.profit_factor,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
        "avg_holding_period": result.avg_holding_period,
    }

    params = {
        "symbols": symbols,
        "start_date": start_date,
        "end_date": end_date,
        "initial_equity": initial_equity,
        "risk_pct": kwargs.get("risk_pct"),
        "max_leverage": kwargs.get("max_leverage"),
        "use_llm": kwargs.get("use_llm"),
        "llm_trigger": kwargs.get("llm_trigger"),
        "level_threshold": kwargs.get("level_threshold"),
    }

    save_backtest_result(result_summary, params)

    return format_results(result)
