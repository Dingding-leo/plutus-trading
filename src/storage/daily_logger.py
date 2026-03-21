"""
Daily analysis logger for storing market analysis.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


def get_daily_file_path(date: datetime = None) -> Path:
    """
    Get path to daily analysis file.

    Args:
        date: Date for the file (default: today)

    Returns:
        Path to file
    """
    if date is None:
        date = datetime.now()

    filename = date.strftime("%Y-%m-%d.md")
    return config.DAILY_ANALYSIS_DIR / filename


def save_daily_analysis(
    content: str,
    date: datetime = None,
) -> Optional[Path]:
    """
    Save daily analysis to file.

    Args:
        content: Analysis content in markdown
        date: Date for the file (default: today)

    Returns:
        Path to saved file, or None on error
    """
    try:
        file_path = get_daily_file_path(date)

        # Ensure directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Add header if file doesn't exist
        if not file_path.exists():
            date_str = date.strftime("%Y-%m-%d") if date else datetime.now().strftime("%Y-%m-%d")
            header = f"# Daily Market Analysis - {date_str}\n\n"
            content = header + content

        # Append to file
        with open(file_path, "a") as f:
            f.write(content + "\n\n")

        return file_path

    except Exception as e:
        logger.error(f"Failed to save daily analysis: {e}")
        return None


def load_daily_analysis(date: datetime = None) -> Optional[str]:
    """
    Load daily analysis from file.

    Args:
        date: Date for the file (default: today)

    Returns:
        Content or None if not found/error
    """
    try:
        file_path = get_daily_file_path(date)

        if not file_path.exists():
            return None

        with open(file_path, "r") as f:
            return f.read()

    except Exception as e:
        logger.error(f"Failed to load daily analysis: {e}")
        return None


def format_market_data(
    btc_data: dict,
    eth_data: dict,
    market_overview: dict,
) -> str:
    """
    Format market data for storage.

    Args:
        btc_data: BTC analysis data
        eth_data: ETH analysis data
        market_overview: Market overview data

    Returns:
        Formatted markdown string
    """
    output = "## Market Data\n\n"

    # BTC
    output += "### BTC/USDT\n"
    if btc_data:
        output += f"- Price: ${btc_data.get('current_price', 0):,.2f}\n"
        output += f"- EMA50: ${btc_data.get('ema50', 0):,.2f}\n"
        if btc_data.get('ema200'):
            output += f"- EMA200: ${btc_data['ema200']:,.2f}\n"
        output += f"- Trend: {btc_data.get('trend', 'N/A')}\n"
        output += f"- Signal: {btc_data.get('signal', 'N/A')}\n"
        if btc_data.get('support'):
            output += f"- Support: ${btc_data['support']:,.2f}\n"
        if btc_data.get('resistance'):
            output += f"- Resistance: ${btc_data['resistance']:,.2f}\n"

    # ETH
    output += "\n### ETH/USDT\n"
    if eth_data:
        output += f"- Price: ${eth_data.get('current_price', 0):,.2f}\n"
        output += f"- Trend: {eth_data.get('trend', 'N/A')}\n"
        output += f"- Signal: {eth_data.get('signal', 'N/A')}\n"

    # Market context
    output += "\n### Market Context\n"
    if market_overview:
        if market_overview.get("total_market_cap"):
            output += f"- Total Cap: ${market_overview['total_market_cap']/1e12:.2f}T\n"
        if market_overview.get("total_volume"):
            output += f"- Volume: ${market_overview['total_volume']/1e9:.1f}B\n"
        if market_overview.get("btc_dominance"):
            output += f"- BTC Dominance: {market_overview['btc_dominance']:.1f}%\n"
        if market_overview.get("fear_greed_index"):
            output += f"- Fear & Greed: {market_overview['fear_greed_index']}\n"

    return output
