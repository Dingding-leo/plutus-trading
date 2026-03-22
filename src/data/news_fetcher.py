"""
News Fetching Module for Market Analysis.

Fetches news from multiple sources:
- Binance announcements (exchange news)
- CryptoPanic (community sentiment / major news)
- CoinGecko Fear & Greed Index
"""

from typing import Optional, List, Dict
from datetime import datetime, timedelta
from dataclasses import dataclass


@dataclass
class NewsItem:
    """Single news item."""
    title: str
    source: str
    published_at: str
    url: str
    sentiment: str = "neutral"  # positive, negative, neutral
    category: str = "general"     # crypto, macro, geopolitical, regulatory


class NewsService:
    """
    News service for fetching market-relevant news.

    Sources:
    - Binance Announcements API (official exchange news)
    - CryptoPanic API (community-driven major news)
    - Fear & Greed Index (via CoinGecko/alternative.me)
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key
        self._session = None

    def _get_session(self):
        """Lazy-initialize requests session."""
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": "Plutus/1.0"})
        return self._session

    # ─────────────────────────────────────────────────────────
    # Binance Announcements
    # ─────────────────────────────────────────────────────────

    def get_binance_announcements(self, limit: int = 10) -> List[NewsItem]:
        """
        Get recent Binance exchange announcements.

        Args:
            limit: Number of announcements to fetch

        Returns:
            List of news items
        """
        url = "https://www.binance.com/bapi/cms/v2/cms/announces"
        params = {"type": 1, "pageSize": limit, "pageNum": 1}

        try:
            session = self._get_session()
            resp = session.get(url, params=params, timeout=10)
            data = resp.json()

            items = []
            for item in data.get("data", {}).get("announceList", [])[:limit]:
                items.append(NewsItem(
                    title=item.get("title", ""),
                    source="Binance",
                    published_at=item.get("createTime", ""),
                    url=f"https://www.binance.com/en/support/announcement/{item.get('id', '')}",
                    sentiment=self._sentiment_from_title(item.get("title", "")),
                    category="crypto",
                ))
            return items

        except (requests.exceptions.RequestException, requests.exceptions.Timeout, OSError) as e:
            print(f"Error fetching Binance announcements: {e}")
            return []

    # ─────────────────────────────────────────────────────────
    # CryptoPanick (public API - no auth needed)
    # ─────────────────────────────────────────────────────────

    def get_cryptopanic_news(self, currencies: str = "BTC,ETH", limit: int = 10) -> List[NewsItem]:
        """
        Get news from CryptoPanic (public API).

        Args:
            currencies: Filter by currencies (comma-separated)
            limit: Number of posts to fetch

        Returns:
            List of news items
        """
        # Use the public CryptoPanic news endpoint
        url = "https://cryptopanic.com/api/v1/posts/"
        params = {
            "auth_token": "public",  # Public token for basic access
            "currencies": currencies,
            "kind": "news",
            "public": "true",
        }

        try:
            session = self._get_session()
            resp = session.get(url, params=params, timeout=10)
            data = resp.json()

            items = []
            for result in data.get("results", [])[:limit]:
                news = result.get("news", {})
                items.append(NewsItem(
                    title=news.get("title", ""),
                    source=news.get("source", {}).get("name", "Unknown"),
                    published_at=result.get("published_at", ""),
                    url=news.get("url", ""),
                    sentiment=self._sentiment_from_title(news.get("title", "")),
                    category="crypto",
                ))
            return items

        except (requests.exceptions.RequestException, requests.exceptions.Timeout, OSError) as e:
            print(f"Error fetching CryptoPanic news: {e}")
            return []

    # ─────────────────────────────────────────────────────────
    # Fear & Greed (already in coingecko_client, expose here too)
    # ─────────────────────────────────────────────────────────

    def get_fear_greed(self) -> Optional[dict]:
        """Get Fear & Greed Index."""
        from . import coingecko_client
        return coingecko_client.get_fear_greed_index()

    # ─────────────────────────────────────────────────────────
    # Combined Market News
    # ─────────────────────────────────────────────────────────

    def get_market_news(self, include_crypto: bool = True) -> Dict[str, List[NewsItem]]:
        """
        Fetch all relevant market news.

        Args:
            include_crypto: Whether to fetch crypto-specific news

        Returns:
            Dict with keys: 'critical', 'crypto', 'exchange'
        """
        result = {
            "critical": [],   # War, macro, regulatory
            "crypto": [],     # Crypto-specific
            "exchange": [],   # Binance/OKX announcements
        }

        # Binance announcements
        exchange_news = self.get_binance_announcements(limit=5)
        result["exchange"] = exchange_news

        # CryptoPanic news
        if include_crypto:
            crypto_news = self.get_cryptopanic_news(limit=10)
            result["crypto"] = crypto_news

            # Simple heuristic: classify as critical if mentions war, Fed, SEC, ETF
            critical_keywords = ["war", "iran", "israel", "fed", "federal reserve",
                                "sec", "regulation", "ban", "etf", "crash", "inflation"]
            for item in crypto_news:
                title_lower = item.title.lower()
                if any(kw in title_lower for kw in critical_keywords):
                    result["critical"].append(item)

        return result

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────

    def _sentiment_from_title(self, title: str) -> str:
        """Infer sentiment from news title (simple keyword-based)."""
        title_lower = title.lower()

        positive_keywords = ["surge", "rally", "bull", "gain", "record high",
                             "adoption", "upgrade", "partnership", "launch", "soar"]
        negative_keywords = ["crash", "plunge", "fall", "drop", "bear", "loss",
                            "hack", "fraud", "ban", "investigation", "selloff",
                            "war", "conflict", "regulation"]

        if any(kw in title_lower for kw in positive_keywords):
            return "positive"
        elif any(kw in title_lower for kw in negative_keywords):
            return "negative"
        return "neutral"


# ─────────────────────────────────────────────────────────────────
# Standalone convenience functions
# ─────────────────────────────────────────────────────────────────

_news_service: Optional[NewsService] = None


def get_news_service() -> NewsService:
    """Get or create singleton news service."""
    global _news_service
    if _news_service is None:
        import os
        api_key = os.environ.get("CRYPTOPANIC_API_KEY")
        _news_service = NewsService(api_key=api_key)
    return _news_service


def fetch_market_news(include_crypto: bool = True) -> Dict[str, List[NewsItem]]:
    """
    Fetch all market news.

    Returns:
        Dict with 'critical', 'crypto', 'exchange' keys
    """
    service = get_news_service()
    return service.get_market_news(include_crypto=include_crypto)


def get_fear_greed() -> Optional[dict]:
    """Get Fear & Greed Index."""
    return get_news_service().get_fear_greed()


def format_news_summary(news_data: Dict[str, List[NewsItem]]) -> str:
    """
    Format news data into workflow template.

    Args:
        news_data: Dict from fetch_market_news()

    Returns:
        Formatted markdown string
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    output = f"## News Summary - {date_str}\n\n"

    # Critical - Geopolitical / Macro / Regulatory
    if news_data.get("critical"):
        output += "### Critical (Read First!)\n"
        for item in news_data["critical"][:5]:
            output += f"- [{item.sentiment.upper()}] {item.title}\n"
        output += "\n"

    # Crypto News
    if news_data.get("crypto"):
        output += "### Crypto News\n"
        for item in news_data["crypto"][:5]:
            output += f"- {item.title}\n"
        output += "\n"

    # Exchange Announcements
    if news_data.get("exchange"):
        output += "### Exchange Announcements\n"
        for item in news_data["exchange"][:5]:
            output += f"- {item.title}\n"
        output += "\n"

    # Fear & Greed
    fg = news_data.get("fear_greed")
    if fg:
        output += f"### Sentiment\n- Fear & Greed: {fg.get('value', 'N/A')} ({fg.get('classification', 'N/A')})\n"

    return output


# ─────────────────────────────────────────────────────────────────
# NewsChecker - backward-compatible wrapper for existing code
# ─────────────────────────────────────────────────────────────────

class NewsChecker:
    """
    News checker for market risk assessment.
    Compatible with the workflow's NewsChecker interface.
    """

    def __init__(self):
        self.last_check = None
        self._news_cache: Dict[str, List[NewsItem]] = {}

    def check_for_major_events(self) -> dict:
        """
        Check for major news events that affect risk level.

        Returns:
            Dict with news categories and risk level
        """
        import os

        # Check for manual environment overrides first
        has_critical = os.environ.get("PLUTUS_HAS_CRITICAL_NEWS", "").lower() == "true"
        has_war = os.environ.get("PLUTUS_HAS_WAR_NEWS", "").lower() == "true"
        has_macro = os.environ.get("PLUTUS_HAS_MACRO_NEWS", "").lower() == "true"
        has_regulation = os.environ.get("PLUTUS_HAS_REGULATION_NEWS", "").lower() == "true"

        if has_critical or has_war or has_macro:
            return {
                "has_critical_news": has_critical,
                "has_war_news": has_war,
                "has_macro_news": has_macro,
                "has_regulation_news": has_regulation,
                "risk_level": "HIGH",
                "note": "Manual override via environment variable",
            }

        # Fetch real news
        try:
            news_data = fetch_market_news(include_crypto=True)
            critical_items = news_data.get("critical", [])

            has_war = any("war" in item.title.lower() or "iran" in item.title.lower()
                          for item in critical_items)
            has_macro = any(word in item.title.lower()
                           for item in critical_items
                           for word in ["fed", "inflation", "cpi", "rate"])
            has_regulation = any(word in item.title.lower()
                                for item in critical_items
                                for word in ["sec", "regulation", "ban", " lawsuit"])

            risk_level = "HIGH" if (has_war or (has_macro and has_regulation)) else \
                        "MODERATE" if (has_macro or has_regulation) else "LOW"

            self._news_cache = news_data
            self.last_check = datetime.now()

            return {
                "has_critical_news": has_war or has_macro or has_regulation,
                "has_war_news": has_war,
                "has_macro_news": has_macro,
                "has_regulation_news": has_regulation,
                "risk_level": risk_level,
                "critical_items": [
                    {"title": item.title, "sentiment": item.sentiment}
                    for item in critical_items[:5]
                ],
                "fear_greed": self.get_fear_greed(),
                "note": None,
            }

        except (OSError, ValueError, TypeError) as e:
            print(f"Error checking news: {e}")
            return {
                "has_critical_news": False,
                "has_war_news": False,
                "has_macro_news": False,
                "has_regulation_news": False,
                "risk_level": "MODERATE",
                "note": f"News check failed: {e}",
            }


# Singleton instance
news_checker = NewsChecker()


def format_news_summary_legacy(
    crypto_news: list[dict] = None,
    geopolitical_news: list[dict] = None,
    macro_news: list[dict] = None,
) -> str:
    """
    Legacy format_news_summary for backward compatibility.
    Prefer fetch_market_news() + format_news_summary() instead.
    """
    # Use the new service-based approach
    news_data = fetch_market_news()
    return format_news_summary(news_data)
