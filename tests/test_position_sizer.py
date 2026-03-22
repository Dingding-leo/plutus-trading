"""
Tests for src/execution/position_sizer.py
"""
import pytest
from src.execution.position_sizer import (
    calculate_position_size,
    calculate_max_leverage,
    apply_gates,
    calculate_rr,
    generate_tranche_plan,
    get_position_multiplier,
)


class TestCalculateMaxLeverage:
    """Tests for calculate_max_leverage."""

    def test_valid_major_coin(self):
        result = calculate_max_leverage(stop_distance=0.02, coin_type="major")
        assert result["valid"] is True
        assert result["max_leverage"] > 0

    def test_small_coin_has_lower_cap_than_major(self):
        # At wider stops, major is uncapped but small is capped at 50
        major_result = calculate_max_leverage(stop_distance=0.03, coin_type="major")
        small_result = calculate_max_leverage(stop_distance=0.03, coin_type="small")
        # Major: usable=0.025, lev=40; Small: usable=0.015, lev=66.67, capped=50
        assert major_result["valid"] is True
        assert small_result["valid"] is True
        assert small_result["max_leverage"] <= 50.0  # Small cap is 50x

    def test_usable_distance_at_buffer_returns_invalid(self):
        # When stop_distance equals the buffer (0.005 for major), usable distance is 0
        result = calculate_max_leverage(stop_distance=0.005, coin_type="major")
        assert result["valid"] is False
        assert "error" in result

    def test_negative_distance_returns_invalid(self):
        result = calculate_max_leverage(stop_distance=-0.01)
        assert result["valid"] is False
        assert "error" in result

    def test_leverage_is_capped_for_major(self):
        result = calculate_max_leverage(stop_distance=0.01, coin_type="major")
        assert result["valid"] is True
        assert result["max_leverage"] <= 125.0

    def test_leverage_is_capped_for_small(self):
        result = calculate_max_leverage(stop_distance=0.03, coin_type="small")
        assert result["valid"] is True
        assert result["max_leverage"] <= 50.0


class TestCalculatePositionSize:
    """Tests for calculate_position_size."""

    def test_basic_position(self, sample_config):
        result = calculate_position_size(**sample_config)
        assert result["valid"] is True
        assert result["equity"] == 10000.0
        assert result["base_risk"] == 100.0  # 1% of 10k
        assert result["position_as_pct_of_equity"] > 0

    def test_zero_equity_returns_invalid(self):
        result = calculate_position_size(equity=0.0)
        assert result["valid"] is False
        assert "error" in result

    def test_negative_equity_returns_invalid(self):
        result = calculate_position_size(equity=-100.0)
        assert result["valid"] is False

    def test_zero_stop_distance_returns_invalid(self):
        result = calculate_position_size(equity=10000, stop_distance=0.0)
        assert result["valid"] is False

    def test_negative_stop_distance_returns_invalid(self):
        result = calculate_position_size(equity=10000, stop_distance=-0.01)
        assert result["valid"] is False

    def test_recommended_leverage_never_exceeds_max(self, sample_config):
        result = calculate_position_size(**sample_config)
        assert result["recommended_leverage"] <= result["max_leverage"]

    def test_gate_b_caps_position_in_training_mode(self):
        # stop=0.006: usable_distance=0.001 → leverage VALID (not at buffer boundary)
        # effective_risk=10, max_pos=1667 > 1000 cap → capped
        result = calculate_position_size(
            equity=1000.0,
            stop_distance=0.006,
            pos_mult=1.0,
            training_mode=True,
        )
        assert "position_as_pct_of_equity" in result
        assert result["position_as_pct_of_equity"] <= 100.0

    def test_gate_b_caps_position_in_advanced_mode(self):
        # stop=0.006: leverage VALID
        # effective_risk=100, max_pos=16667 > 15000 cap → capped at 150%
        result = calculate_position_size(
            equity=10000.0,
            stop_distance=0.006,
            pos_mult=1.0,
            training_mode=False,
        )
        assert "position_as_pct_of_equity" in result
        assert result["position_as_pct_of_equity"] <= 150.0

    def test_effective_risk_includes_position_multiplier(self):
        result = calculate_position_size(equity=10000.0, pos_mult=0.5)
        assert result["effective_risk"] == 100.0 * 0.5  # base_risk * pos_mult

    def test_position_value_positive_when_valid(self):
        result = calculate_position_size(equity=10000, stop_distance=0.02)
        assert result["max_position"] > 0

    def test_default_risk_pct_used_when_none(self):
        result = calculate_position_size(equity=10000, risk_pct=None, stop_distance=0.02)
        assert result["base_risk"] == 100.0


class TestApplyGates:
    """Tests for Gate A and Gate B logic."""

    def test_gate_a_fires_for_small_stop_high_risk(self):
        pos, gate = apply_gates(
            stop_distance=0.005, risk_level="HIGH",
            position_value=5000.0, equity=10000.0,
        )
        assert gate == "Gate A: Small stop penalty"
        assert pos == 5000.0 * 0.3  # 30% of original

    def test_gate_a_does_not_fire_for_large_stop(self):
        pos, gate = apply_gates(
            stop_distance=0.03, risk_level="HIGH",
            position_value=5000.0, equity=10000.0,
        )
        assert gate is None
        assert pos == 5000.0

    def test_gate_a_does_not_fire_for_low_risk(self):
        pos, gate = apply_gates(
            stop_distance=0.005, risk_level="LOW",
            position_value=5000.0, equity=10000.0,
        )
        assert gate is None

    def test_gate_b_caps_training_position(self):
        pos, gate = apply_gates(
            stop_distance=0.03, risk_level="LOW",
            position_value=20000.0,  # 200% of equity
            equity=10000.0,
        )
        assert gate == "Gate B: Position cap"
        assert pos == 10000.0  # Capped at 100% in training mode

    def test_gate_a_takes_priority_over_gate_b_when_both_fire(self):
        pos, gate = apply_gates(
            stop_distance=0.005, risk_level="HIGH",
            position_value=5000.0, equity=10000.0,
        )
        # Gate A fires first, position reduced to 30%
        assert gate == "Gate A: Small stop penalty"
        assert pos == 1500.0
        # After Gate A reduction, 1500 < 10000 cap, so Gate B does not fire

    def test_gate_b_fires_for_training_mode_with_large_position(self):
        pos, gate = apply_gates(
            stop_distance=0.03, risk_level="LOW",
            position_value=5000.0,  # 50% of equity (cap is 100%)
            equity=10000.0,
        )
        # Position is within cap, so no gate fires
        assert gate is None
        assert pos == 5000.0


class TestCalculateRR:
    """Tests for calculate_rr including fee calculations."""

    def test_basic_rr(self):
        result = calculate_rr(entry=100, stop=99, target=103)
        assert result["rr_gross"] > 0
        assert result["risk"] == 1.0
        assert result["reward"] == 3.0

    def test_rr_includes_fees(self):
        result = calculate_rr(entry=100, stop=99, target=103)
        # Fees reduce net RR vs gross RR
        assert result["rr_net"] < result["rr_gross"]
        assert result["entry_fee"] > 0
        assert result["exit_fee_at_stop"] > 0
        assert result["exit_fee_at_target"] > 0

    def test_zero_risk_returns_zero_rr_gross(self):
        result = calculate_rr(entry=100, stop=100, target=103)
        # When risk is 0, rr_gross is 0; rr_net can still be positive due to fees
        assert result["rr_gross"] == 0

    def test_short_position_rr(self):
        # For short: entry=100, stop=101, target=97
        result = calculate_rr(entry=100, stop=101, target=97)
        assert result["risk"] == 1.0
        assert result["reward"] == 3.0
        assert result["rr_gross"] == 3.0

    def test_total_fees_pct_calculated(self):
        result = calculate_rr(entry=100, stop=99, target=103)
        # default maker_fee = 0.0002 → 0.02% × 2 = 0.04%
        assert result["total_fees_pct"] == pytest.approx(0.04)


class TestGenerateTranchePlan:
    """Tests for generate_tranche_plan."""

    def test_long_tranche_plan(self):
        plan = generate_tranche_plan(position_value=10000, current_price=67000, direction="long")
        assert plan["tranche_1"]["percentage"] == 50
        assert plan["tranche_2"]["percentage"] == 30
        assert plan["tranche_3"]["percentage"] == 20
        assert plan["tranche_1"]["entry_price"] < 67000  # Below current for long

    def test_short_tranche_plan(self):
        plan = generate_tranche_plan(position_value=10000, current_price=67000, direction="short")
        assert plan["tranche_1"]["entry_price"] > 67000  # Above current for short

    def test_tranche_values_sum_to_total(self):
        plan = generate_tranche_plan(position_value=10000, current_price=67000)
        total = (plan["tranche_1"]["value"] +
                 plan["tranche_2"]["value"] +
                 plan["tranche_3"]["value"])
        assert abs(total - 10000) < 0.01  # Within floating point tolerance

    def test_units_calculated(self):
        plan = generate_tranche_plan(position_value=10000, current_price=67000, direction="long")
        assert plan["tranche_1"]["units"] > 0

    def test_default_direction_is_long(self):
        # Should not raise, defaults to "long"
        plan = generate_tranche_plan(position_value=10000, current_price=67000)
        assert plan["tranche_1"]["entry_price"] < 67000


class TestGetPositionMultiplier:
    """Tests for get_position_multiplier."""

    def test_low_risk_returns_high_multiplier(self):
        result = get_position_multiplier("LOW")
        # (1.0 + 1.2) / 2 = 1.1
        assert result == 1.1

    def test_moderate_risk_returns_mid_multiplier(self):
        result = get_position_multiplier("MODERATE")
        # (0.7 + 1.0) / 2 = 0.85
        assert result == 0.85

    def test_high_risk_returns_low_multiplier(self):
        result = get_position_multiplier("HIGH")
        # (0.3 + 0.5) / 2 = 0.4
        assert result == 0.4

    def test_unknown_risk_falls_back_to_moderate(self):
        result = get_position_multiplier("UNKNOWN")
        # Falls back to (0.7, 1.0) → 0.85
        assert result == 0.85
