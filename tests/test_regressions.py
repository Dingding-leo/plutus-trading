import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


class TestCliHelp(unittest.TestCase):
    def test_backtest_help_does_not_crash(self):
        repo_root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "src.backtest", "--help"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}",
        )


class TestBinanceClient(unittest.TestCase):
    @mock.patch("src.data.binance_client.requests.get")
    def test_fetch_klines_invalid_json_raises_clear_error(self, mock_get):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = json.JSONDecodeError("Expecting value", "doc", 0)
        mock_get.return_value = response

        from src.data.binance_client import fetch_klines

        with self.assertRaisesRegex(Exception, "Invalid JSON response from Binance"):
            fetch_klines(symbol="BTCUSDT", interval="1h", limit=10, retries=1)


class TestConfig(unittest.TestCase):
    def test_project_root_points_to_repo_root(self):
        repo_root = Path(__file__).resolve().parent.parent
        from src import config

        self.assertEqual(config.PROJECT_ROOT.resolve(), repo_root.resolve())


class TestPositionSizer(unittest.TestCase):
    def test_recommended_leverage_never_negative(self):
        from src.execution.position_sizer import calculate_position_size

        position = calculate_position_size(
            equity=10_000,
            risk_pct=0.01,
            stop_distance=0.30,
            pos_mult=1.0,
            coin_type="major",
            training_mode=True,
        )
        self.assertTrue(position["valid"])
        self.assertGreaterEqual(position["max_leverage"], 0)
        self.assertGreaterEqual(position["recommended_leverage"], 0)
        self.assertLessEqual(position["recommended_leverage"], position["max_leverage"])
