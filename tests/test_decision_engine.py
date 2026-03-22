"""
Tests for src/execution/decision_engine.py
"""
import pytest
from src.execution.decision_engine import (
    DecisionEngine,
    Phase,
    format_execution_gate,
    format_decision,
)


class TestDecisionEngine:
    def test_initial_phase_is_no_movement(self):
        engine = DecisionEngine()
        assert engine.current_phase == Phase.NO_MOVEMENT

    def test_phase_no_trigger(self):
        engine = DecisionEngine()
        phase = engine.update_phase(has_trigger=False)
        assert phase == Phase.NO_MOVEMENT

    def test_phase_trigger_without_move_is_shock(self):
        engine = DecisionEngine()
        phase = engine.update_phase(has_trigger=True, price_moved=False)
        assert phase == Phase.SHOCK

    def test_phase_trigger_with_move_is_confirmation(self):
        engine = DecisionEngine()
        phase = engine.update_phase(has_trigger=True, price_moved=True)
        assert phase == Phase.CONFIRMATION

    def test_define_trigger(self):
        engine = DecisionEngine()
        engine.define_trigger(trigger_price=67000, trigger_condition="breakout")
        assert engine.trigger_price == 67000
        assert engine.trigger_defined is True


class TestExecutionGate:
    def test_all_pass_returns_true(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
            min_rr=1.5,
        )
        assert result["pass"] is True
        assert result["failed_check"] is None

    def test_rr_below_minimum_fails(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=1.0,  # Below 1.5 minimum
            min_rr=1.5,
        )
        assert result["pass"] is False
        assert result["failed_check"] == "rr_adequate"

    def test_structure_not_broken_fails(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=False,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["pass"] is False
        assert result["failed_check"] == "structure_break"

    def test_macro_not_aligned_fails(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=True,
            macro_aligned=False,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["pass"] is False
        assert result["failed_check"] == "macro_aligned"

    def test_invalidation_unclear_fails(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=False,
            rr=2.0,
        )
        assert result["pass"] is False
        assert result["failed_check"] == "invalidation_clear"

    def test_checks_dict_included(self):
        engine = DecisionEngine()
        result = engine.check_execution_gate(
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert "checks" in result
        assert isinstance(result["checks"], dict)


class TestMakeDecision:
    def test_no_movement_returns_no_trade(self):
        engine = DecisionEngine()
        result = engine.make_decision(phase=Phase.NO_MOVEMENT)
        assert result["decision"] == "NO TRADE"

    def test_shock_returns_wait(self):
        engine = DecisionEngine()
        result = engine.make_decision(phase=Phase.SHOCK)
        assert result["decision"] == "WAIT"

    def test_confirmation_with_gate_pass_returns_execute(self):
        engine = DecisionEngine()
        result = engine.make_decision(phase=Phase.CONFIRMATION, execution_gate_passed=True)
        assert result["decision"] == "EXECUTE TRADE"

    def test_confirmation_with_gate_fail_returns_skip(self):
        engine = DecisionEngine()
        result = engine.make_decision(phase=Phase.CONFIRMATION, execution_gate_passed=False)
        assert result["decision"] == "SKIP"

    def test_skip_includes_reason(self):
        engine = DecisionEngine()
        result = engine.make_decision(phase=Phase.CONFIRMATION, execution_gate_passed=False, skip_reason="test reason")
        assert result["reason"] == "test reason"


class TestAntiAvoidance:
    def test_skip_without_justification_is_avoidance(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="SKIP",
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["is_avoidance"] is True
        assert "AVOIDANCE BEHAVIOR" in result["reason"]

    def test_no_trade_with_no_justification_is_avoidance(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="NO TRADE",
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["is_avoidance"] is True

    def test_no_trade_with_structure_not_broken_is_justified(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="SKIP",
            structure_break=False,  # Justified
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["is_avoidance"] is False

    def test_no_trade_with_low_rr_is_justified(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="SKIP",
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=1.0,  # Below threshold
        )
        assert result["is_avoidance"] is False

    def test_no_trade_with_macro_not_aligned_is_justified(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="SKIP",
            structure_break=True,
            macro_aligned=False,  # Justified
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["is_avoidance"] is False

    def test_execute_trade_not_flagged_as_avoidance(self):
        engine = DecisionEngine()
        result = engine.check_anti_avoidance(
            decision="EXECUTE TRADE",
            structure_break=True,
            macro_aligned=True,
            invalidation_clear=True,
            rr=2.0,
        )
        assert result["is_avoidance"] is False


class TestAssessTradeType:
    def test_insufficient_candles_returns_reversal(self):
        engine = DecisionEngine()
        result = engine.assess_trade_type(
            candles=[{"close": 100}] * 10,
            ema50=105,
            ema200=100,
        )
        assert result == "reversal"

    def test_continuation_in_uptrend(self):
        engine = DecisionEngine()
        # 50 candles: first half lower, second half higher (uptrend)
        candles = [{"close": 100 + i * 0.5} for i in range(25)] + \
                  [{"close": 112.5 + i * 0.5} for i in range(25)]
        result = engine.assess_trade_type(candles, ema50=120, ema200=110)
        assert result == "continuation"

    def test_continuation_in_downtrend(self):
        engine = DecisionEngine()
        # 50 candles: first half higher, second half lower (downtrend)
        candles = [{"close": 200 - i * 0.5} for i in range(25)] + \
                  [{"close": 187.5 - i * 0.5} for i in range(25)]
        result = engine.assess_trade_type(candles, ema50=180, ema200=190)
        assert result == "continuation"

    def test_reversal_mixed_trend(self):
        engine = DecisionEngine()
        candles = [{"close": 100 + i} for i in range(50)]
        result = engine.assess_trade_type(candles, ema50=120, ema200=130)  # EMA50 > EMA200 but downtrend
        assert result == "reversal"


class TestFormatFunctions:
    def test_format_decision(self):
        result = format_decision({"decision": "NO TRADE", "phase": "未动", "reason": "No trigger"})
        assert "NO TRADE" in result
        assert "未动" in result

    def test_format_execution_gate_pass(self):
        result = format_execution_gate({"pass": True, "checks": {}, "failed_check": None})
        assert "PASS" in result

    def test_format_execution_gate_fail(self):
        result = format_execution_gate({
            "pass": False,
            "checks": {"structure_break": False},
            "failed_check": "structure_break",
        })
        assert "FAIL" in result
        assert "structure_break" in result
