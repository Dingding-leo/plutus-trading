"""
Tests for src/data/llm_client.py
"""
import pytest
from unittest.mock import patch, Mock
from src.data.llm_client import (
    LLMClient,
    _norm,
    _parse_macro_response,
    FALLBACK_CONTEXT,
    VALID_MACRO_REGIME,
    VALID_BTC_STRENGTH,
    VALID_VOLATILITY,
    get_llm_macro_context,
)


class TestNorm:
    def test_normalizes_lowercase(self):
        assert _norm("risk_on") == "RISK_ON"
        assert _norm("  STRONG  ") == "STRONG"

    def test_returns_empty_for_none(self):
        assert _norm(None) == ""

    def test_strips_whitespace(self):
        assert _norm("  RISK_ON  ") == "RISK_ON"

    def test_empty_string(self):
        assert _norm("") == ""


class TestParseMacroResponse:
    def test_valid_response(self):
        raw = '{"macro_regime": "RISK_ON", "btc_strength": "WEAK", "volatility_warning": "HIGH"}'
        result = _parse_macro_response(raw)
        assert result["macro_regime"] == "RISK_ON"
        assert result["btc_strength"] == "WEAK"
        assert result["volatility_warning"] == "HIGH"
        assert result["_error"] is None

    def test_invalid_macro_regime_falls_back_to_neutral(self):
        # macro_regime "INVALID" → "NEUTRAL"
        raw = '{"macro_regime": "INVALID", "btc_strength": "STRONG", "volatility_warning": "LOW"}'
        result = _parse_macro_response(raw)
        assert result["macro_regime"] == "NEUTRAL"

    def test_invalid_btc_strength_falls_back_to_neutral(self):
        # btc_strength "GARBAGE" → "NEUTRAL"
        raw = '{"macro_regime": "RISK_ON", "btc_strength": "GARBAGE", "volatility_warning": "LOW"}'
        result = _parse_macro_response(raw)
        assert result["btc_strength"] == "NEUTRAL"

    def test_invalid_volatility_falls_back_to_neutral(self):
        # _norm returns "NEUTRAL" as safe fallback for any invalid enum value
        raw = '{"macro_regime": "RISK_ON", "btc_strength": "STRONG", "volatility_warning": "MAYBE"}'
        result = _parse_macro_response(raw)
        assert result["volatility_warning"] == "NEUTRAL"

    def test_missing_braces_raises_valueerror(self):
        with pytest.raises(ValueError, match="No JSON object found"):
            _parse_macro_response("No JSON here")

    def test_empty_response_raises_valueerror(self):
        with pytest.raises(ValueError):
            _parse_macro_response("")

    def test_partial_json_raises_valueerror(self):
        with pytest.raises(ValueError):
            _parse_macro_response('{"macro_regime": "RISK_ON"')  # missing closing brace

    def test_valid_neutral_response(self):
        raw = '{"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL", "volatility_warning": "LOW"}'
        result = _parse_macro_response(raw)
        assert result["macro_regime"] == "NEUTRAL"
        assert result["btc_strength"] == "NEUTRAL"
        assert result["volatility_warning"] == "LOW"

    def test_risk_off_regime(self):
        raw = '{"macro_regime": "RISK_OFF", "btc_strength": "STRONG", "volatility_warning": "LOW"}'
        result = _parse_macro_response(raw)
        assert result["macro_regime"] == "RISK_OFF"


class TestFallbackContext:
    def test_fallback_has_all_fields(self):
        assert "_error" in FALLBACK_CONTEXT
        assert "_block_reason" in FALLBACK_CONTEXT
        assert FALLBACK_CONTEXT["macro_regime"] == "NEUTRAL"
        assert FALLBACK_CONTEXT["btc_strength"] == "NEUTRAL"
        assert FALLBACK_CONTEXT["volatility_warning"] == "LOW"

    def test_fallback_error_is_none(self):
        assert FALLBACK_CONTEXT["_error"] is None
        assert FALLBACK_CONTEXT["_block_reason"] is None


class TestValidEnumSets:
    def test_valid_macro_regime_values(self):
        assert "RISK_ON" in VALID_MACRO_REGIME
        assert "RISK_OFF" in VALID_MACRO_REGIME
        assert "NEUTRAL" in VALID_MACRO_REGIME
        assert len(VALID_MACRO_REGIME) == 3

    def test_valid_btc_strength_values(self):
        assert "STRONG" in VALID_BTC_STRENGTH
        assert "WEAK" in VALID_BTC_STRENGTH
        assert "NEUTRAL" in VALID_BTC_STRENGTH
        assert len(VALID_BTC_STRENGTH) == 3

    def test_valid_volatility_values(self):
        assert "HIGH" in VALID_VOLATILITY
        assert "LOW" in VALID_VOLATILITY
        assert len(VALID_VOLATILITY) == 2


class TestLLMClient:
    @patch("src.data.llm_client.requests.post")
    def test_successful_chat(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL", "volatility_warning": "LOW"}'}}]
        }
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        client = LLMClient(api_key="test-key")
        result = client.chat([{"role": "user", "content": "test"}])
        assert "NEUTRAL" in result

    @patch("src.data.llm_client.requests.post")
    def test_rate_limit_retry(self, mock_post):
        mock_429 = Mock()
        mock_429.status_code = 429
        mock_429.headers = {"Retry-After": "1"}
        mock_429.raise_for_status = Mock(side_effect=Exception("429"))

        mock_200 = Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {
            "choices": [{"message": {"content": '{"macro_regime": "NEUTRAL"}'}}]
        }
        mock_200.raise_for_status = Mock()

        mock_post.side_effect = [mock_429, mock_200]

        client = LLMClient(api_key="test-key")
        result = client.chat([{"role": "user", "content": "test"}], max_retries=2)
        assert mock_post.call_count == 2

    @patch("src.data.llm_client.requests.post")
    def test_raises_after_final_attempt_fails(self, mock_post):
        # When raise_for_status raises on the final attempt,
        # the exception propagates (not "Max retries exceeded")
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "0"}
        mock_response.raise_for_status = Mock(side_effect=Exception("429"))
        mock_post.return_value = mock_response

        client = LLMClient(api_key="test-key")
        with pytest.raises(Exception) as exc_info:
            client.chat([{"role": "user", "content": "test"}], max_retries=3)
        # The exception message comes from raise_for_status
        assert "429" in str(exc_info.value)

    @patch("src.data.llm_client.requests.post")
    def test_server_error_retries(self, mock_post):
        mock_500 = Mock()
        mock_500.status_code = 500
        mock_500.raise_for_status = Mock(side_effect=Exception("500"))

        mock_200 = Mock()
        mock_200.status_code = 200
        mock_200.json.return_value = {
            "choices": [{"message": {"content": '{"macro_regime": "NEUTRAL"}'}}]
        }
        mock_200.raise_for_status = Mock()

        mock_post.side_effect = [mock_500, mock_200]

        client = LLMClient(api_key="test-key")
        result = client.chat([{"role": "user", "content": "test"}], max_retries=2)
        assert mock_post.call_count == 2

    @patch("src.data.llm_client.requests.post")
    def test_unexpected_response_structure_raises(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"base_resp": {"status_msg": "invalid request"}}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        client = LLMClient(api_key="test-key")
        with pytest.raises(Exception, match="API Error"):
            client.chat([{"role": "user", "content": "test"}])

    def test_client_uses_env_var_api_key(self):
        with patch.dict("os.environ", {"LLM_API_KEY": "env-api-key"}):
            client = LLMClient()
            assert client.api_key == "env-api-key"

    def test_client_default_base_url(self):
        client = LLMClient()
        assert "api" in client.base_url.lower() or "minimax" in client.base_url


class TestGetLlmMacroContext:
    @patch("src.data.llm_client.llm_client")
    def test_returns_fallback_on_parse_error(self, mock_llm_client):
        # llm_client.chat returns something that _parse_macro_response cannot parse
        mock_llm_client.chat.return_value = "not json at all"

        result = get_llm_macro_context(
            btc_analysis={"trend": "UPTREND", "rsi": 50, "current_price": 67000,
                          "support": 66000, "resistance": 68000},
            target_symbol="ETHUSDT",
            target_analysis={"trend": "UPTREND", "rsi": 55, "current_price": 2000},
            market_overview={"fear_greed_index": 50, "total_market_cap": 2e12,
                             "btc_dominance": 55, "volume_24h": 1e11},
        )
        assert result["macro_regime"] == "NEUTRAL"
        assert result["_error"] is not None

    @patch("src.data.llm_client.llm_client")
    def test_returns_parsed_response_on_success(self, mock_llm_client):
        mock_llm_client.chat.return_value = '{"macro_regime": "RISK_ON", "btc_strength": "STRONG", "volatility_warning": "LOW"}'

        result = get_llm_macro_context(
            btc_analysis={"trend": "UPTREND", "rsi": 50, "current_price": 67000,
                          "support": 66000, "resistance": 68000},
            target_symbol="ETHUSDT",
            target_analysis={"trend": "UPTREND", "rsi": 55, "current_price": 2000},
            market_overview={"fear_greed_index": 60, "total_market_cap": 2e12,
                             "btc_dominance": 55, "volume_24h": 1e11},
        )
        assert result["macro_regime"] == "RISK_ON"
        assert result["btc_strength"] == "STRONG"
        assert result["volatility_warning"] == "LOW"
        assert result["_error"] is None

    @patch("src.data.llm_client.llm_client")
    def test_handles_none_analysis(self, mock_llm_client):
        mock_llm_client.chat.return_value = '{"macro_regime": "NEUTRAL", "btc_strength": "NEUTRAL", "volatility_warning": "LOW"}'

        result = get_llm_macro_context(
            btc_analysis=None,
            target_symbol="ETHUSDT",
            target_analysis=None,
            market_overview=None,
        )
        assert result["macro_regime"] == "NEUTRAL"

    @patch("src.data.llm_client.llm_client")
    def test_network_error_returns_fallback(self, mock_llm_client):
        mock_llm_client.chat.side_effect = Exception("Network error")

        result = get_llm_macro_context(
            btc_analysis={"trend": "UPTREND"},
            target_symbol="ETHUSDT",
            target_analysis={"trend": "UPTREND"},
            market_overview={},
        )
        assert result["macro_regime"] == "NEUTRAL"
        assert result["_error"] is not None
