"""
Trade Plan - Standardized trade plan output.
"""

from typing import Optional
from .position_sizer import calculate_rr


def create_trade_plan(
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    position_size: float,
    leverage: float,
    pos_mult: float,
    current_price: float = None,
    risk_level: str = "MODERATE",
    trade_type: str = "continuation",
) -> dict:
    """
    Create standardized trade plan.

    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')
        direction: 'BUY' or 'SELL'
        entry: Entry price
        stop: Stop loss price
        target: Take profit price
        position_size: Position size in quote currency
        leverage: Leverage to use
        pos_mult: Position multiplier
        current_price: Current price (optional)
        risk_level: Risk level
        trade_type: 'reversal' or 'continuation'

    Returns:
        Complete trade plan dict
    """
    # Calculate RR
    rr_result = calculate_rr(entry, stop, target)

    # Stop distance
    stop_distance = abs(entry - stop) / entry

    # TP levels (TP1 at 50% of target, TP2 at full target)
    tp1_price = entry + (target - entry) * 0.5
    tp1_size_pct = 50  # Close 50% at TP1
    tp2_price = target
    tp2_size_pct = 50  # Close remaining 50% at TP2

    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "stop_distance_pct": stop_distance * 100,
        "tp1": {
            "price": tp1_price,
            "size_pct": tp1_size_pct,
        },
        "tp2": {
            "price": tp2_price,
            "size_pct": tp2_size_pct,
        },
        "target": target,
        "position_size": position_size,
        "leverage": leverage,
        "pos_mult": pos_mult,
        "risk_level": risk_level,
        "trade_type": trade_type,
        "rr_gross": rr_result["rr_gross"],
        "rr_net": rr_result["rr_net"],
    }


def format_trade_plan(plan: dict) -> str:
    """
    Format trade plan into readable markdown.

    Args:
        plan: Trade plan dict

    Returns:
        Formatted markdown string
    """
    direction = plan.get("direction", "N/A")
    symbol = plan.get("symbol", "N/A")

    output = f"# Trade Plan - {symbol} {direction}\n\n"

    # Decision
    output += "## Decision\n"
    output += f"- **{direction} {symbol}**\n"
    output += f"- Type: {plan.get('trade_type', 'N/A')}\n"
    output += f"- Risk Level: {plan.get('risk_level', 'N/A')}\n\n"

    # Entry/Exit
    output += "## Entry/Exit\n"
    output += f"- Entry: ${plan['entry']:,.2f}\n"
    output += f"- Stop: ${plan['stop']:,.2f}\n"
    output += f"- Stop Distance: {plan.get('stop_distance_pct', 0):.2f}%\n"
    output += f"- TP1: ${plan['tp1']['price']:,.2f} ({plan['tp1']['size_pct']}% close)\n"
    output += f"- TP2: ${plan['tp2']['price']:,.2f} ({plan['tp2']['size_pct']}% close)\n\n"

    # Position
    output += "## Position\n"
    output += f"- Position Size: ${plan['position_size']:,.2f}\n"
    output += f"- Leverage: {plan['leverage']:.1f}x\n"
    output += f"- Position Multiplier: {plan['pos_mult']:.1f}x\n\n"

    # Risk/Reward
    output += "## Risk/Reward\n"
    output += f"- RR (gross): {plan.get('rr_gross', 0):.2f}\n"
    output += f"- RR (net): {plan.get('rr_net', 0):.2f}\n\n"

    # Invalidation
    output += "## Invalidation\n"
    if direction == "BUY":
        output += f"- **Invalidation: price closes below ${plan['stop']:,.2f}**\n"
    else:
        output += f"- **Invalidation: price closes above ${plan['stop']:,.2f}**\n"

    output += "\n"

    # Warning for small stops
    stop_dist = plan.get("stop_distance_pct", 0)
    if stop_dist < 0.7:
        output += "⚠️ **WARNING:** Stop distance is very small (<0.7%). Consider wider stop or smaller position.\n"

    return output


def validate_trade_plan(plan: dict) -> dict:
    """
    Validate trade plan meets requirements.

    Per CLAUDE.md: stop distance must be >= 0.5% OR use stricter pos_mult (0.3 instead of 0.4)

    Args:
        plan: Trade plan dict

    Returns:
        Dict with validation result
    """
    errors = []
    warnings = []

    # Check stop distance
    # Per CLAUDE.md: stop >= 0.5% OR pos_mult <= 0.3
    stop_dist = plan.get("stop_distance_pct", 0)
    pos_mult = plan.get("pos_mult", 1.0)

    if stop_dist < 0.5:
        if pos_mult > 0.3:
            errors.append(
                f"Stop distance too small: {stop_dist:.2f}%. "
                f"Use pos_mult <= 0.3 or stop >= 0.5%"
            )
        else:
            warnings.append(
                f"Small stop {stop_dist:.2f}% allowed with strict pos_mult={pos_mult}"
            )

    # Check RR (must be >= 1.5 per CLAUDE.md)
    rr_net = plan.get("rr_net", 0)
    if rr_net < 1.5:
        warnings.append(f"RR below 1.5: {rr_net:.2f}")

    # Check leverage
    leverage = plan.get("leverage", 0)
    if leverage > 100:
        warnings.append(f"Very high leverage: {leverage:.0f}x")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
