"""
Volume Profile module for identifying LVN (Low Volume Nodes) and HVN (High Volume Nodes).
"""

from typing import Optional
from .. import config


def calculate_volume_profile(
    prices: list[float],
    volumes: list[float],
    highs: list[float] = None,
    lows: list[float] = None,
    bins: int = None
) -> dict[float, float]:
    """
    Calculate volume at each price level.

    Args:
        prices: List of closing prices
        volumes: List of volumes
        highs: List of high prices (optional, for proper volume distribution)
        lows: List of low prices (optional, for proper volume distribution)
        bins: Number of price bins (default from config)

    Returns:
        Dict mapping price level to volume
    """
    if bins is None:
        bins = config.VOLUME_PROFILE_BINS

    if len(prices) == 0 or len(volumes) == 0:
        return {}

    # Determine price range
    if highs is not None and lows is not None:
        # Use high-low range for proper volume distribution
        price_min = min(lows)
        price_max = max(highs)
    else:
        price_min = min(prices)
        price_max = max(prices)

    if price_min == price_max:
        return {price_min: sum(volumes)}

    bin_size = (price_max - price_min) / bins

    profile = {}
    for i in range(bins):
        bin_low = price_min + i * bin_size
        bin_high = bin_low + bin_size
        profile[bin_low] = 0.0

    # Assign volume to bins
    if highs is not None and lows is not None and len(highs) == len(prices) and len(lows) == len(prices):
        # Distribute volume across high-low range (proper method)
        for i, (close_price, high_price, low_price, volume) in enumerate(zip(prices, highs, lows, volumes)):
            if high_price == low_price:
                # Single price point
                bin_index = int((close_price - price_min) / bin_size)
                bin_index = max(0, min(bin_index, bins - 1))
                bin_price = price_min + bin_index * bin_size
                profile[bin_price] = profile.get(bin_price, 0) + volume
            else:
                # Distribute volume across the high-low range
                start_bin = int((low_price - price_min) / bin_size)
                end_bin = int((high_price - price_min) / bin_size)
                start_bin = max(0, start_bin)
                end_bin = max(0, min(end_bin, bins - 1))

                if end_bin >= start_bin:
                    bins_covered = end_bin - start_bin + 1
                    volume_per_bin = volume / bins_covered
                    for b in range(start_bin, end_bin + 1):
                        bin_price = price_min + b * bin_size
                        profile[bin_price] = profile.get(bin_price, 0) + volume_per_bin
    else:
        # Fallback: use close prices only (legacy behavior)
        for price, volume in zip(prices, volumes):
            bin_index = int((price - price_min) / bin_size)
            if bin_index >= bins:
                bin_index = bins - 1
            if bin_index < 0:
                bin_index = 0

            bin_price = price_min + bin_index * bin_size
            profile[bin_price] = profile.get(bin_price, 0) + volume

    return profile


def find_lvn(
    profile: dict[float, float],
    threshold_percentile: int = None,
    num_nodes: int = 3
) -> list[dict]:
    """
    Find Low Volume Nodes (support zones).

    Args:
        profile: Volume profile dict
        threshold_percentile: Percentile threshold (default from config)
        num_nodes: Number of top LVN to return

    Returns:
        List of LVN dicts with price and volume
    """
    if threshold_percentile is None:
        threshold_percentile = config.LVN_THRESHOLD

    if not profile:
        return []

    volumes = sorted(profile.values())
    if not volumes:
        return []

    threshold_idx = int(len(volumes) * threshold_percentile / 100)
    # Ensure index is within bounds
    threshold_idx = max(0, min(threshold_idx, len(volumes) - 1))
    threshold = volumes[threshold_idx]

    lvns = [
        {"price": price, "volume": volume}
        for price, volume in profile.items()
        if volume <= threshold
    ]

    # Sort by volume (lowest first) and return top num_nodes
    lvns.sort(key=lambda x: x["volume"])
    return lvns[:num_nodes]


def find_hvn(
    profile: dict[float, float],
    threshold_percentile: int = None,
    num_nodes: int = 3
) -> list[dict]:
    """
    Find High Volume Nodes (resistance zones).

    Args:
        profile: Volume profile dict
        threshold_percentile: Percentile threshold (default from config)
        num_nodes: Number of top HVN to return

    Returns:
        List of HVN dicts with price and volume
    """
    if threshold_percentile is None:
        threshold_percentile = config.HVN_THRESHOLD

    if not profile:
        return []

    volumes = sorted(profile.values())
    if not volumes:
        return []

    threshold_idx = int(len(volumes) * threshold_percentile / 100)
    # Ensure index is within bounds
    threshold_idx = max(0, min(threshold_idx, len(volumes) - 1))
    threshold = volumes[threshold_idx]

    hvns = [
        {"price": price, "volume": volume}
        for price, volume in profile.items()
        if volume >= threshold
    ]

    # Sort by volume (highest first) and return top num_nodes
    hvns.sort(key=lambda x: x["volume"], reverse=True)
    return hvns[:num_nodes]


def get_key_levels(
    candles: list[dict],
    bins: int = None
) -> dict:
    """
    Get all key levels from candles.

    Args:
        candles: List of candle dicts
        bins: Number of bins for volume profile

    Returns:
        Dict with highs, lows, LVN, HVN
    """
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # Recent highs/lows
    recent_closes = closes[-50:]
    recent_highs = highs[-50:]
    recent_lows = lows[-50:]

    # Volume profile (now using high-low range for proper distribution)
    profile_data = closes[-200:], volumes[-200:], highs[-200:], lows[-200:]
    profile = calculate_volume_profile(*profile_data, bins)
    lvns = find_lvn(profile)
    hvns = find_hvn(profile)

    return {
        "recent_high": max(recent_highs),
        "recent_low": min(recent_lows),
        "lvn": lvns,
        "hvn": hvns,
    }


def check_multi_timeframe_resonance(
    levels_5m: dict,
    levels_15m: dict,
    levels_30m: dict,
    tolerance_pct: float = 0.5
) -> dict:
    """
    Check if multiple timeframes agree on key levels.

    Args:
        levels_5m: Key levels from 5m timeframe
        levels_15m: Key levels from 15m timeframe
        levels_30m: Key levels from 30m timeframe
        tolerance_pct: Price tolerance for matching levels

    Returns:
        Dict with resonance status and matching levels
    """
    def get_all_levels(levels: dict) -> list[float]:
        lvl = []
        if "recent_high" in levels:
            lvl.append(levels["recent_high"])
        if "recent_low" in levels:
            lvl.append(levels["recent_low"])
        lvl.extend(lvn["price"] for lvn in levels.get("lvn", []))
        lvl.extend(hvn["price"] for hvn in levels.get("hvn", []))
        return lvl

    all_5m = get_all_levels(levels_5m)
    all_15m = get_all_levels(levels_15m)
    all_30m = get_all_levels(levels_30m)

    # Bucket by price tolerance to find matches in O(n) instead of O(n³)
    bucket_size = tolerance_pct / 100

    def bucket_prices(prices: list[float]) -> dict[int, list[float]]:
        buckets = {}
        for p in prices:
            key = round(p / bucket_size)
            if key not in buckets:
                buckets[key] = []
            buckets[key].append(p)
        return buckets

    b5 = bucket_prices(all_5m)
    b15 = bucket_prices(all_15m)
    b30 = bucket_prices(all_30m)

    common_keys = set(b5) & set(b15) & set(b30)
    matches = []
    for key in common_keys:
        prices = b5[key] + b15[key] + b30[key]
        matches.append(sum(prices) / len(prices))

    strength = "STRONG" if len(matches) >= 3 else ("MEDIUM" if len(matches) >= 1 else "NONE")

    return {
        "resonance_strength": strength,
        "matching_levels": matches,
        "level_count": len(matches),
    }


def find_entry_target(
    current_price: float,
    levels: dict,
    direction: str = "long"
) -> dict:
    """
    Find optimal entry and target based on key levels.

    Args:
        current_price: Current price
        levels: Key levels dict
        direction: 'long' or 'short'

    Returns:
        Dict with entry, target, stop
    """
    if direction == "long":
        # Entry at LVN (support), target at HVN (resistance)
        lvns = levels.get("lvn", [])
        hvns = levels.get("hvn", [])

        # Find nearest LVN below current price
        entry = None
        for lvn in lvns:
            if lvn["price"] < current_price:
                if entry is None or lvn["price"] > entry:
                    entry = lvn["price"]

        # If no LVN found, use recent low
        if entry is None:
            entry = levels.get("recent_low", current_price * 0.98)

        # Find nearest HVN above entry
        target = None
        for hvn in hvns:
            if hvn["price"] > entry:
                if target is None or hvn["price"] < target:
                    target = hvn["price"]

        # If no HVN, use recent high
        if target is None:
            target = levels.get("recent_high", current_price * 1.05)

        # Stop below entry
        stop = entry * 0.99  # 1% below entry

    else:  # short
        # Entry at HVN (resistance), target at LVN (support)
        hvns = levels.get("hvn", [])
        lvns = levels.get("lvn", [])

        # Find nearest HVN above current price
        entry = None
        for hvn in hvns:
            if hvn["price"] > current_price:
                if entry is None or hvn["price"] < entry:
                    entry = hvn["price"]

        # If no HVN found, use recent high
        if entry is None:
            entry = levels.get("recent_high", current_price * 1.02)

        # Find nearest LVN below entry
        target = None
        for lvn in lvns:
            if lvn["price"] < entry:
                if target is None or lvn["price"] > target:
                    target = lvn["price"]

        # If no LVN, use recent low
        if target is None:
            target = levels.get("recent_low", current_price * 0.95)

        # Stop above entry
        stop = entry * 1.01  # 1% above entry

    return {
        "entry": entry,
        "target": target,
        "stop": stop,
    }
