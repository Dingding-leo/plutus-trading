"""
Position sizing module for risk-based position management.
"""

from typing import Optional
from .. import config


def get_position_multiplier(risk_level: str) -> float:
    """
    Get position multiplier based on risk level.

    Args:
        risk_level: 'LOW', 'MODERATE', or 'HIGH'

    Returns:
        Position multiplier (midpoint)
    """
    multipliers = config.RISK_MULTIPLIERS.get(risk_level, (0.7, 1.0))
    # Return midpoint
    return (multipliers[0] + multipliers[1]) / 2


def calculate_max_leverage(
    stop_distance: float,
    coin_type: str = "major",
    max_cap: float = 125.0
) -> float:
    """
    Calculate maximum leverage based on stop distance.

    Args:
        stop_distance: Stop loss distance as decimal (e.g., 0.02 = 2%)
        coin_type: 'major' or 'small'
        max_cap: Maximum leverage cap (default 125x for major, 50x for small)

    Returns:
        Max leverage as multiple (e.g., 50 for 50x)
    """
    buffer = config.LEVERAGE_BUFFERS.get(coin_type, 0.005)

    # Cap based on coin type
    if coin_type == "major":
        max_cap = min(max_cap, 125.0)
    else:
        max_cap = min(max_cap, 50.0)

    # Max leverage = (stop_distance - buffer) / stop_distance * 100
    usable_distance = stop_distance - buffer

    if usable_distance <= 0:
        return 0

    # Convert to leverage and cap
    leverage = 1 / usable_distance
    leverage = min(leverage, max_cap)

    return leverage


def calculate_position_size(
    equity: float,
    risk_pct: float = None,
    stop_distance: float = 0.02,
    pos_mult: float = 1.0,
    coin_type: str = "major",
    training_mode: bool = True,
) -> dict:
    """
    Calculate position size based on risk parameters.

    Args:
        equity: Total account equity
        risk_pct: Risk percentage (default 1%)
        stop_distance: Stop loss distance as decimal
        pos_mult: Position multiplier from risk level
        coin_type: 'major' or 'small'
        training_mode: If True, cap at 1x; if False, cap at 1.5x

    Returns:
        Dict with position details
    """
    if risk_pct is None:
        risk_pct = config.DEFAULT_RISK_PCT

    # Base risk
    base_risk = equity * risk_pct

    # Effective risk after multiplier
    effective_risk = base_risk * pos_mult

    # Calculate max position value
    if stop_distance <= 0:
        return {
            "valid": False,
            "error": "Invalid stop distance",
        }

    max_position = effective_risk / stop_distance

    # Apply Gate B: Position cap
    cap = config.POSITION_CAP_TRAINING if training_mode else config.POSITION_CAP_ADVANCED
    max_cap = equity * cap

    if max_position > max_cap:
        max_position = max_cap

    # Calculate max leverage
    max_leverage = calculate_max_leverage(stop_distance, coin_type)

    # Determine recommended leverage (use slightly less than max for safety)
    if max_leverage <= 0:
        recommended_leverage = 0
    else:
        recommended_leverage = max_leverage * 0.8
        recommended_leverage = min(recommended_leverage, max_leverage)
        if max_leverage < 1:
            recommended_leverage = max_leverage
        else:
            recommended_leverage = max(recommended_leverage, 1.0)

    return {
        "valid": True,
        "equity": equity,
        "base_risk": base_risk,
        "effective_risk": effective_risk,
        "pos_mult": pos_mult,
        "stop_distance": stop_distance,
        "max_position": max_position,
        "max_leverage": max_leverage,
        "recommended_leverage": recommended_leverage,
        "position_as_pct_of_equity": max_position / equity * 100,
    }


def apply_gates(
    stop_distance: float,
    risk_level: str,
    position_value: float,
    equity: float,
) -> tuple[float, str]:
    """
    Apply Gate A and Gate B checks.

    Args:
        stop_distance: Stop loss distance as decimal
        risk_level: 'LOW', 'MODERATE', or 'HIGH'
        position_value: Calculated position value
        equity: Total equity

    Returns:
        Tuple of (adjusted_position, gate_applied)
    """
    gate_applied = None

    # Gate A: Small stop penalty
    if stop_distance < config.SMALL_STOP_THRESHOLD and risk_level == "HIGH":
        # Force smaller position
        position_value = position_value * 0.3
        gate_applied = "Gate A: Small stop penalty"

    # Gate B: Position cap
    cap = config.POSITION_CAP_TRAINING
    max_allowed = equity * cap

    if position_value > max_allowed:
        position_value = max_allowed
        gate_applied = "Gate B: Position cap"

    return position_value, gate_applied


def generate_tranche_plan(
    position_value: float,
    current_price: float,
    direction: str = "long",
) -> dict:
    """
    Generate entry plan with tranches.

    Args:
        position_value: Total position value
        current_price: Current price
        direction: 'long' or 'short'

    Returns:
        Dict with tranche entries
    """
    # Tranche sizes: 50% / 30% / 20%
    tranche_1 = position_value * 0.5
    tranche_2 = position_value * 0.3
    tranche_3 = position_value * 0.2

    if direction == "long":
        # Entry prices (limit orders below current)
        entry_1 = current_price * 0.995  # 0.5% below
        entry_2 = current_price * 0.99   # 1% below
        entry_3 = current_price * 0.985  # 1.5% below
    else:
        # Short entries
        entry_1 = current_price * 1.005  # 0.5% above
        entry_2 = current_price * 1.01   # 1% above
        entry_3 = current_price * 1.015  # 1.5% above

    return {
        "tranche_1": {
            "percentage": 50,
            "value": tranche_1,
            "entry_price": entry_1,
            "units": tranche_1 / entry_1,
        },
        "tranche_2": {
            "percentage": 30,
            "value": tranche_2,
            "entry_price": entry_2,
            "units": tranche_2 / entry_2,
        },
        "tranche_3": {
            "percentage": 20,
            "value": tranche_3,
            "entry_price": entry_3,
            "units": tranche_3 / entry_3,
        },
    }


def calculate_rr(
    entry: float,
    stop: float,
    target: float,
    maker_fee: float = 0.0002,
) -> dict:
    """
    Calculate risk/reward ratio including fees.

    Args:
        entry: Entry price
        stop: Stop loss price
        target: Take profit price
        maker_fee: Maker fee rate (default 0.02% = 0.0002)

    Returns:
        Dict with RR calculation
    """
    # Gross risk and reward per unit
    risk = abs(entry - stop)
    reward = abs(target - entry)

    # Gross RR
    rr_gross = reward / risk if risk > 0 else 0

    # Fee calculation:
    # Entry fee: paid once at entry (maker fee on entry price)
    # Exit fee: paid once at exit (maker fee on exit price)
    # Total round-trip fees = (entry + exit_price) * maker_fee
    entry_fee = entry * maker_fee
    exit_fee_at_stop = stop * maker_fee
    exit_fee_at_target = target * maker_fee

    # Net loss at stop = risk + entry_fee + exit_fee_at_stop
    # Net profit at target = reward - entry_fee - exit_fee_at_target
    net_loss = risk + entry_fee + exit_fee_at_stop
    net_profit = reward - entry_fee - exit_fee_at_target

    # Net RR = net_profit / net_loss
    rr_net = net_profit / net_loss if net_loss > 0 else 0

    return {
        "risk": risk,
        "reward": reward,
        "rr_gross": rr_gross,
        "rr_net": rr_net,
        "total_fees_pct": maker_fee * 2 * 100,  # Entry + exit as percentage
        "entry_fee": entry_fee,
        "exit_fee_at_stop": exit_fee_at_stop,
        "exit_fee_at_target": exit_fee_at_target,
    }


def format_position_size(
    position: dict,
    direction: str = "LONG",
    symbol: str = "BTCUSDT",
) -> str:
    """
    Format position details into readable string.

    Args:
        position: Position dict from calculate_position_size
        direction: 'LONG' or 'SHORT'
        symbol: Trading pair

    Returns:
        Formatted string
    """
    if not position.get("valid", False):
        return f"INVALID: {position.get('error', 'Unknown error')}"

    output = f"## Position Sizing - {symbol} {direction}\n\n"
    output += f"- Equity: ${position['equity']:,.2f}\n"
    output += f"- Base Risk (1%): ${position['base_risk']:,.2f}\n"
    output += f"- Effective Risk ({position['pos_mult']:.1f}x): ${position['effective_risk']:,.2f}\n"
    output += f"- Stop Distance: {position['stop_distance']*100:.2f}%\n"
    output += f"- Max Position: ${position['max_position']:,.2f}\n"
    output += f"- Max Leverage: {position['max_leverage']:.1f}x\n"
    output += f"- Recommended Leverage: {position['recommended_leverage']:.1f}x\n"
    output += f"- Position as % of Equity: {position['position_as_pct_of_equity']:.1f}%\n"

    return output
