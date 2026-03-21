"""
Rule-Based "LLM" Analysis - Mimics LLM decision making per TRADING_WORKFLOW.md.
This system acts as an intelligent analyst following the workflow rules.
"""

from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class MarketAnalysis:
    """Market analysis result."""
    decision: str  # BUY, SELL, NO_TRADE
    symbol: str    # BTC, ETH, ALT, NONE
    risk_level: str  # LOW, MODERATE, HIGH
    reason: str
    confidence: float  # 0-1


class WorkflowAnalyzer:
    """
    Rule-based analyzer that mimics LLM following TRADING_WORKFLOW.md exactly.
    This is the "intelligent brain" of the system.
    """

    def __init__(self):
        self.last_analysis = None

    def analyze(
        self,
        btc_analysis: dict,
        eth_analysis: dict,
        alt_analyses: List[dict],
        market_overview: dict = None,
    ) -> MarketAnalysis:
        """
        Complete market analysis following TRADING_WORKFLOW.md Step 4 & 5.
        """
        # Step 1: Check macro (risk_on/risk_off)
        macro = self._determine_macro(btc_analysis)

        # Step 2: Check BTC status (strength/weakness)
        btc_status = self._assess_btc(btc_analysis)

        # Step 3: Determine valid answers per workflow Section 10
        valid_answers = self._get_valid_answers(macro, btc_status)

        # Step 4: Find best opportunity
        opportunity = self._find_opportunity(
            btc_analysis, eth_analysis, alt_analyses, macro, btc_status
        )

        # Step 5: Assess risk level
        risk_level = self._classify_risk(btc_analysis, market_overview)

        # Step 6: Generate decision
        decision = self._make_decision(
            opportunity, valid_answers, risk_level, btc_analysis
        )

        self.last_analysis = decision
        return decision

    def _determine_macro(self, btc: dict) -> str:
        """Determine macro state (Step 4.1)."""
        trend = btc.get("trend", "SIDEWAYS")
        momentum = btc.get("momentum", {})
        change_24h = momentum.get("change_24h", 0)

        if trend == "DOWNTREND" or change_24h < -3:
            return "risk_off"
        elif trend == "UPTREND" and change_24h > 0:
            return "risk_on"
        else:
            return "risk_off"  # Conservative

    def _assess_btc(self, btc: dict) -> str:
        """Assess BTC strength (Section 10.4)."""
        trend = btc.get("trend", "SIDEWAYS")
        signal = btc.get("signal", "NEUTRAL")
        momentum = btc.get("momentum", {})
        change_24h = momentum.get("change_24h", 0)

        weakness_signals = 0
        strength_signals = 0

        if trend == "DOWNTREND":
            weakness_signals += 2
        elif trend == "UPTREND":
            strength_signals += 2

        if change_24h < -2:
            weakness_signals += 2
        elif change_24h > 2:
            strength_signals += 1

        if signal == "SELL":
            weakness_signals += 1
        elif signal == "BUY":
            strength_signals += 1

        if weakness_signals >= 2:
            return "weakness"
        elif strength_signals >= 2:
            return "strength"
        else:
            return "neutral"

    def _get_valid_answers(self, macro: str, btc_status: str) -> dict:
        """Get valid trading answers per workflow Section 10.6."""
        if macro == "risk_off" or btc_status == "weakness":
            return {
                "allowed": ["BTC SHORT", "NO TRADE"],
                "forbidden": ["ALT LONG", "BTC LONG"],
            }
        else:  # risk_on and btc neutral/strong
            return {
                "allowed": ["BTC LONG", "ETH LONG", "ALT LONG", "NO TRADE"],
                "forbidden": [],
            }

    def _find_opportunity(
        self,
        btc: dict,
        eth: dict,
        alts: List[dict],
        macro: str,
        btc_status: str
    ) -> Optional[dict]:
        """Find best trading opportunity."""
        opportunities = []

        # Check BTC
        if btc.get("signal") == "BUY" and btc.get("trend") == "UPTREND":
            opportunities.append({
                "symbol": "BTC",
                "direction": "LONG",
                "score": self._calc_score(btc, is_major=True),
            })
        elif btc.get("signal") == "SELL" and btc.get("trend") == "DOWNTREND":
            opportunities.append({
                "symbol": "BTC",
                "direction": "SHORT",
                "score": self._calc_score(btc, is_major=True),
            })

        # Check ETH
        if macro == "risk_on" and eth.get("trend") == "UPTREND":
            opportunities.append({
                "symbol": "ETH",
                "direction": "LONG",
                "score": self._calc_score(eth, is_major=True),
            })

        # Check alts (only in risk_on)
        if macro == "risk_on":
            for alt in alts:
                if alt.get("trend") == "UPTREND":
                    opportunities.append({
                        "symbol": alt.get("symbol", "ALT"),
                        "direction": "LONG",
                        "score": self._calc_score(alt, is_major=False),
                    })

        if not opportunities:
            return None

        # Return highest scored opportunity
        opportunities.sort(key=lambda x: x["score"], reverse=True)
        return opportunities[0]

    def _calc_score(self, analysis: dict, is_major: bool) -> float:
        """Calculate opportunity score."""
        score = 0.0

        # Trend
        if analysis.get("trend") == "UPTREND":
            score += 3
        elif analysis.get("trend") == "DOWNTREND":
            score += 3

        # RSI
        rsi = analysis.get("rsi", 50)
        if 30 < rsi < 40:  # Oversold bounce
            score += 2
        elif 60 < rsi < 70:  # Overbought
            score += 1

        # Position in range
        pos = analysis.get("position_in_range", 50)
        if pos < 30:  # Near support
            score += 2
        elif pos > 70:  # Near resistance
            score += 1

        # Major coins get priority
        if is_major:
            score *= 1.5

        return score

    def _classify_risk(self, btc: dict, market: dict = None) -> str:
        """Classify risk level (Section 4.2)."""
        triggers = 0

        # Momentum
        momentum = btc.get("momentum", {})
        change_24h = abs(momentum.get("change_24h", 0))
        if change_24h > 5:
            triggers += 1

        # RSI extremes
        rsi = btc.get("rsi", 50)
        if rsi < 30 or rsi > 70:
            triggers += 1

        # Position extremes
        pos = btc.get("position_in_range", 50)
        if pos > 90 or pos < 10:
            triggers += 1

        if triggers >= 2:
            return "HIGH"
        elif triggers == 1:
            return "MODERATE"
        else:
            return "LOW"

    def _make_decision(
        self,
        opportunity: Optional[dict],
        valid_answers: dict,
        risk_level: str,
        btc: dict
    ) -> MarketAnalysis:
        """Make final trading decision."""
        if not opportunity:
            return MarketAnalysis(
                decision="NO_TRADE",
                symbol="NONE",
                risk_level=risk_level,
                reason="No clear opportunity",
                confidence=0.5
            )

        symbol = opportunity["symbol"]
        direction = opportunity["direction"]

        # Check if allowed
        trade_str = f"{symbol} {direction}"
        if trade_str in valid_answers["forbidden"]:
            return MarketAnalysis(
                decision="NO_TRADE",
                symbol="NONE",
                risk_level=risk_level,
                reason=f"{trade_str} forbidden in current macro",
                confidence=0.7
            )

        return MarketAnalysis(
            decision=direction,
            symbol=symbol,
            risk_level=risk_level,
            reason=f"Best opportunity: {trade_str}",
            confidence=opportunity["score"] / 10
        )


# Global analyzer instance
analyzer = WorkflowAnalyzer()


def analyze_market_rule_based(
    btc_analysis: dict,
    eth_analysis: dict,
    alt_analyses: List[dict] = None,
    market_overview: dict = None,
) -> MarketAnalysis:
    """Main entry point for market analysis."""
    if alt_analyses is None:
        alt_analyses = []

    return analyzer.analyze(btc_analysis, eth_analysis, alt_analyses, market_overview)
