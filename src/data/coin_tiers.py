"""
Coin Categorization System - Categorizes all coins and assigns parameters.
OKX Leverage Tiers: 100x, 50x, 20x
"""

# Tier 1: Blue chip / Major (100x - OKX highest leverage)
TIER_1 = [
    "BTCUSDT", "ETHUSDT",
]

# Tier 2: Large cap alts (50x - OKX mid leverage)
TIER_2 = [
    "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "AVAXUSDT",
    "DOTUSDT", "MATICUSDT", "LINKUSDT", "UNIUSDT", "ATOMUSDT",
    "LTCUSDT", "ETCUSDT", "XLMUSDT", "NEARUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "FILUSDT", "ICPUSDT", "HBARUSDT",
]

# Tier 3: All other coins (20x - OKX lowest leverage)
TIER_3 = [
    "VETUSDT", "ALGOUSDT", "FTMUSDT", "SANDUSDT", "MANAUSDT",
    "AAVEUSDT", "AXSUSDT", "THETAUSDT", "EOSUSDT", "XTZUSDT",
    "PEPEUSDT", "WIFUSDT", "BONKUSDT", "SUIUSDT", "SEIUSDT",
    "INJUSDT", "TIAUSDT", "BLURUSDT", "IMXUSDT", "LDOUSDT",
    "QNTUSDT", "RNDRUSDT", "STXUSDT", "KASUSDT", "CKBUSDT",
    "ORDIUSDT", "CRVUSDT", "RUNEUSDT", "MKRUSDT",
    "SNXUSDT", "COMPUSDT", "1INCHUSDT", "BATUSDT", "ENJUSDT",
    "ZECUSDT", "KAVAUSDT", "NEOUSDT", "GRTUSDT", "CAKEUSDT",
]

# Tier 4: Empty (use TIER_3 params)
TIER_4 = []

# All coins combined
ALL_TIERS = TIER_1 + TIER_2 + TIER_3


# Parameters per tier (OKX requirements: 100x, 50x, 20x)
# Balanced for ~25% drawdown with decent returns
TIER_PARAMS = {
    "TIER_1": {
        "max_leverage": 100,
        "risk_pct": 0.022,
        "stop_pct": 0.008,
        "target_mult": 8.5,
        "min_quality": 3,
    },
    "TIER_2": {
        "max_leverage": 50,
        "risk_pct": 0.018,
        "stop_pct": 0.01,
        "target_mult": 7.5,
        "min_quality": 3,
    },
    "TIER_3": {
        "max_leverage": 20,
        "risk_pct": 0.014,
        "stop_pct": 0.012,
        "target_mult": 6.5,
        "min_quality": 3,
    },
    "TIER_4": {
        "max_leverage": 20,
        "risk_pct": 0.01,
        "stop_pct": 0.015,
        "target_mult": 5.5,
        "min_quality": 3,
    },
}


def get_tier(symbol: str) -> str:
    """Get tier for a symbol."""
    n = normalize_symbol(symbol)
    if n in TIER_1:
        return "TIER_1"
    elif n in TIER_2:
        return "TIER_2"
    elif n in TIER_3:
        return "TIER_3"
    else:
        return "TIER_3"  # Default to 20x (most conservative)


def get_params(symbol: str) -> dict:
    """Get trading parameters for a symbol."""
    tier = get_tier(symbol)
    return TIER_PARAMS[tier].copy()


def get_all_symbols() -> list:
    """Get all available symbols."""
    return ALL_TIERS.copy()


def normalize_symbol(symbol: str) -> str:
    """
    Normalize symbol to standard format (no hyphen).

    Handles: BTC-USDT, BTCUSDT, ETH-USDT, ETHUSDT -> BTCUSDT, ETHUSDT

    Args:
        symbol: Symbol in any format

    Returns:
        Normalized symbol (e.g., 'BTCUSDT')
    """
    if not symbol:
        return symbol

    # Remove hyphens and common separators, uppercase
    normalized = symbol.replace("-", "").replace("_", "").upper()

    # Handle special cases
    # BTCUSDT / ETHUSDT etc. - already normalized
    # Just ensure it ends with USDT
    if not normalized.endswith("USDT"):
        normalized += "USDT"

    return normalized


def is_major(symbol: str) -> bool:
    """Check if symbol is a major coin (BTC or ETH)."""
    n = normalize_symbol(symbol)
    return n in ["BTCUSDT", "ETHUSDT"]


def is_alt(symbol: str) -> bool:
    """Check if symbol is an altcoin (not BTC or ETH)."""
    return not is_major(symbol)
