"""
CoinGecko API client for fetching market metrics.
"""

import requests
import time
from typing import Optional
from .. import config


def get_global_data() -> Optional[dict]:
    """
    Get global market data from CoinGecko.

    Returns:
        Dict with total_market_cap, btc_dominance, total_volume, etc. or None on error
    """
    url = f"{config.COINGECKO_BASE_URL}/global"

    for attempt in range(3):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()["data"]
            return {
                "total_market_cap": data["total_market_cap"]["usd"],
                "total_volume": data["total_volume"]["usd"],
                "btc_dominance": data["btc_dominance"],
                "active_cryptocurrencies": data["active_cryptocurrencies"],
                "market_cap_change_24h": data["market_cap_change_percentage_24h_usd"],
            }

        except requests.exceptions.RequestException as e:
            if attempt < 2:
                wait_time = 2 ** attempt
                time.sleep(wait_time)
            else:
                print(f"Failed to fetch global data after 3 attempts: {e}")
                return None


def get_fear_greed_index() -> Optional[dict]:
    """
    Get Fear & Greed Index (from alternative.me crypto API).

    Returns:
        Dict with value (0-100), classification, or None if unavailable
    """
    # Using alternative.me API for Fear & Greed
    url = "https://api.alternative.me/fng/"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()["data"][0]

        value = int(data["value"])
        classification = data["value_classification"]

        # Convert to same format as CoinGecko uses
        return {
            "value": value,
            "classification": classification,
        }
    except Exception as e:
        print(f"Failed to fetch Fear & Greed Index: {e}")
        return None


def get_market_overview() -> Optional[dict]:
    """
    Get comprehensive market overview.

    Returns:
        Combined market data or None on error
    """
    global_data = get_global_data()
    if global_data is None:
        return None

    fear_greed = get_fear_greed_index()

    result = {
        "total_market_cap": global_data["total_market_cap"],
        "total_volume": global_data["total_volume"],
        "btc_dominance": global_data.get("btc_dominance", 0),
        "market_cap_change_24h": global_data.get("market_cap_change_24h", 0),
    }

    if fear_greed:
        result["fear_greed_index"] = fear_greed["value"]
        result["fear_greed_classification"] = fear_greed["classification"]

    return result
