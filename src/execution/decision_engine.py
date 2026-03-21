"""
Decision Engine - Three-phase trading decision framework.
"""

from typing import Optional
from enum import Enum


class Phase(Enum):
    """Three phases of market movement."""
    NO_MOVEMENT = "未动"      # No trigger
    SHOCK = "冲击"            # News or fake breakout
    CONFIRMATION = "确认"     # Trigger hit


class DecisionEngine:
    """
    Three-phase decision engine following the workflow.
    """

    def __init__(self):
        self.current_phase = Phase.NO_MOVEMENT
        self.trigger_defined = False
        self.trigger_price = None

    def update_phase(self, has_trigger: bool, price_moved: bool = False) -> Phase:
        """
        Update phase based on market conditions.

        Args:
            has_trigger: Whether there's a trigger
            price_moved: Whether price has moved significantly

        Returns:
            Current phase
        """
        if not has_trigger:
            self.current_phase = Phase.NO_MOVEMENT
        elif has_trigger and not price_moved:
            self.current_phase = Phase.SHOCK
        elif has_trigger and price_moved:
            self.current_phase = Phase.CONFIRMATION

        return self.current_phase

    def define_trigger(self, trigger_price: float, trigger_condition: str):
        """
        Define trigger for phase 3.

        Args:
            trigger_price: Price level for trigger
            trigger_condition: Description of trigger
        """
        self.trigger_price = trigger_price
        self.trigger_defined = True

    def check_execution_gate(
        self,
        structure_break: bool,
        macro_aligned: bool,
        invalidation_clear: bool,
        rr: float = 0,
        min_rr: float = 1.5,
    ) -> dict:
        """
        Check if trade meets execution criteria.

        Args:
            structure_break: Did structure break?
            macro_aligned: Is macro aligned?
            invalidation_clear: Is invalidation clear?
            rr: Risk/reward ratio
            min_rr: Minimum required RR

        Returns:
            Dict with gate result
        """
        checks = {
            "structure_break": structure_break,
            "macro_aligned": macro_aligned,
            "invalidation_clear": invalidation_clear,
            "rr_adequate": rr >= min_rr,
        }

        all_passed = all(checks.values())

        return {
            "pass": all_passed,
            "checks": checks,
            "failed_check": None if all_passed else next(
                (k for k, v in checks.items() if not v), None
            ),
        }

    def make_decision(
        self,
        phase: Phase,
        execution_gate_passed: bool = False,
        skip_reason: str = None,
    ) -> dict:
        """
        Make trading decision based on phase.

        Args:
            phase: Current phase
            execution_gate_passed: Whether execution gate passed
            skip_reason: Reason for skipping (if any)

        Returns:
            Decision dict
        """
        if phase == Phase.NO_MOVEMENT:
            return {
                "decision": "NO TRADE",
                "reason": "No trigger - no edge",
                "phase": phase.value,
            }

        elif phase == Phase.SHOCK:
            return {
                "decision": "WAIT",
                "reason": "Waiting for confirmation",
                "phase": phase.value,
                "trigger_price": self.trigger_price,
            }

        elif phase == Phase.CONFIRMATION:
            if execution_gate_passed:
                return {
                    "decision": "EXECUTE TRADE",
                    "reason": "All criteria met",
                    "phase": phase.value,
                }
            else:
                return {
                    "decision": "SKIP",
                    "reason": skip_reason or "Execution gate failed",
                    "phase": phase.value,
                }

    def check_anti_avoidance(
        self,
        decision: str,
        structure_break: bool,
        macro_aligned: bool,
        invalidation_clear: bool,
        rr: float,
    ) -> dict:
        """
        Anti-avoidance check - prevent finding excuses.

        Args:
            decision: Current decision
            structure_break: Structure broken
            macro_aligned: Macro aligned
            invalidation_clear: Invalidation clear
            rr: Risk/reward

        Returns:
            Dict with avoidance check result
        """
        is_no_trade = decision == "SKIP" or decision == "NO TRADE"

        if not is_no_trade:
            return {
                "is_avoidance": False,
                "reason": None,
            }

        # Check if NO TRADE is justified
        can_justify = (
            (not structure_break) or
            (not macro_aligned) or
            (not invalidation_clear) or
            (rr < 1.5)
        )

        return {
            "is_avoidance": not can_justify,
            "reason": "AVOIDANCE BEHAVIOR - criteria met but finding excuse" if not can_justify else None,
        }

    def assess_trade_type(
        self,
        candles: list[dict],
        ema50: float,
        ema200: float,
    ) -> str:
        """
        Determine if this is reversal or continuation setup.

        Args:
            candles: Recent candles
            ema50: 50 EMA
            ema200: 200 EMA

        Returns:
            'reversal' or 'continuation'
        """
        if len(candles) < 50:
            return "reversal"

        recent_closes = [c["close"] for c in candles[-50:]]

        # Calculate trend direction
        first_half_avg = sum(recent_closes[:25]) / 25
        second_half_avg = sum(recent_closes[25:]) / 25

        if ema50 > ema200 and second_half_avg > first_half_avg:
            return "continuation"
        elif ema50 < ema200 and second_half_avg < first_half_avg:
            return "continuation"
        else:
            return "reversal"


def format_decision(decision: dict) -> str:
    """
    Format decision into readable string.

    Args:
        decision: Decision dict

    Returns:
        Formatted string
    """
    output = "## Trading Decision\n\n"
    output += f"- Phase: {decision.get('phase', 'N/A')}\n"
    output += f"- Decision: **{decision.get('decision', 'N/A')}**\n"
    output += f"- Reason: {decision.get('reason', 'N/A')}\n"

    if "trigger_price" in decision and decision["trigger_price"]:
        output += f"- Trigger Price: ${decision['trigger_price']:,.2f}\n"

    return output


def format_execution_gate(gate_result: dict) -> str:
    """
    Format execution gate check results.

    Args:
        gate_result: Gate result dict

    Returns:
        Formatted string
    """
    output = "### Execution Gate Check\n\n"

    checks = gate_result.get("checks", {})
    for check_name, check_result in checks.items():
        status = "✅" if check_result else "❌"
        output += f"- {status} {check_name.replace('_', ' ').title()}\n"

    output += f"\n**Result:** {'PASS' if gate_result['pass'] else 'FAIL'}\n"

    if gate_result.get("failed_check"):
        output += f"Failed: {gate_result['failed_check']}\n"

    return output


# Global decision engine instance
decision_engine = DecisionEngine()
