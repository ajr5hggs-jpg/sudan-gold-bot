import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paper_trading import (
    Candle,
    MarketData,
    PaperTradingError,
    PaperTradingEngine,
    crossover_signal,
    ema_series,
    fetch_market_data,
    is_market_open,
    position_size_ounces,
    rsi_series,
)


class IndicatorTests(unittest.TestCase):
    def test_ema_uses_sma_seed_and_smoothing(self) -> None:
        self.assertEqual(ema_series([1.0, 2.0, 3.0, 4.0], 3), [None, None, 2.0, 3.0])

    def test_rsi_is_100_for_a_continuous_rise(self) -> None:
        values = [float(value) for value in range(1, 16)]
        result = rsi_series(values, 14)
        self.assertIsNone(result[13])
        self.assertEqual(result[14], 100.0)

    def test_crossover_requires_rsi_confirmation(self) -> None:
        fast = [1.0, 3.0]
        slow = [2.0, 2.5]
        self.assertEqual(crossover_signal(1, fast, slow, [40.0, 60.0]), "long")
        self.assertIsNone(crossover_signal(1, fast, slow, [40.0, 45.0]))
        self.assertEqual(
            crossover_signal(1, [3.0, 1.0], [2.0, 1.5], [60.0, 40.0]),
            "short",
        )

    def test_two_percent_risk_sizes_in_ounces(self) -> None:
        self.assertEqual(position_size_ounces(10_000, 1.50), 133.3333)

    def test_yfinance_tickers_are_tried_in_order_until_one_works(self) -> None:
        successful = MarketData(
            [Candle(index * 900, 4000.0, 4001.0, 3999.0, 4000.0) for index in range(200)],
            4000.0,
            "Yahoo GC=F",
            "GC=F",
        )
        with patch(
            "paper_trading._fetch_yahoo",
            side_effect=[PaperTradingError("no spot data"), successful],
        ) as fetch:
            result = fetch_market_data()

        self.assertIs(result, successful)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            ["XAUUSD=X", "GC=F"],
        )

    def test_paxg_is_the_last_yfinance_fallback(self) -> None:
        successful = MarketData(
            [Candle(index * 900, 4000.0, 4001.0, 3999.0, 4000.0) for index in range(200)],
            4000.0,
            "Yahoo PAXG-USD",
            "PAXG-USD",
        )
        with patch(
            "paper_trading._fetch_yahoo",
            side_effect=[
                PaperTradingError("no XAUUSD data"),
                PaperTradingError("no GC=F data"),
                successful,
            ],
        ) as fetch:
            result = fetch_market_data()

        self.assertIs(result, successful)
        self.assertEqual(
            [call.args[0] for call in fetch.call_args_list],
            ["XAUUSD=X", "GC=F", "PAXG-USD"],
        )


class SessionTests(unittest.TestCase):
    def test_gold_session_closes_for_weekend_and_daily_break(self) -> None:
        saturday = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
        sunday_before_open = datetime(2026, 10, 25, 21, 30, tzinfo=timezone.utc)
        sunday_after_open = datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc)
        monday_daily_break = datetime(2026, 10, 26, 21, 30, tzinfo=timezone.utc)
        monday_open = datetime(2026, 10, 26, 20, 30, tzinfo=timezone.utc)

        self.assertFalse(is_market_open(saturday))
        self.assertFalse(is_market_open(sunday_before_open))
        self.assertTrue(is_market_open(sunday_after_open))
        self.assertFalse(is_market_open(monday_daily_break))
        self.assertTrue(is_market_open(monday_open))


class PaperAccountTests(unittest.TestCase):
    def test_accounts_are_separate_per_chat_and_manual_close_updates_balance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notifications: list[tuple[int, str]] = []
            engine = PaperTradingEngine(
                notify=lambda chat_id, message: notifications.append((chat_id, message)),
                state_path=Path(directory) / "paper.json",
            )
            engine.start(101)
            engine.start(202)

            account = engine.accounts["101"]
            account["last_price"] = 4010.0
            account["position"] = {
                "side": "long",
                "entry_price": 4000.0,
                "stop_price": 3998.5,
                "target_price": 4003.0,
                "size_oz": 2.0,
            }
            message = engine.close_all(101)

            self.assertIn("$20.00", message)
            self.assertEqual(account["balance_usd"], 10_020.0)
            self.assertIsNone(account["position"])
            self.assertEqual(engine.accounts["202"]["balance_usd"], 10_000.0)
            self.assertEqual(notifications, [])

    def test_stop_first_when_a_candle_crosses_both_levels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = PaperTradingEngine(
                notify=lambda _chat_id, _message: None,
                state_path=Path(directory) / "paper.json",
            )
            position = {
                "side": "long",
                "entry_price": 100.0,
                "stop_price": 98.5,
                "target_price": 103.0,
                "size_oz": 1.0,
            }
            candle = Candle(900, 100.0, 104.0, 98.0, 101.0)
            self.assertEqual(
                engine._candle_exit(position, candle),
                (98.5, "وقف الخسارة"),
            )

    def test_new_m15_signal_opens_only_in_that_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notifications: list[tuple[int, str]] = []
            engine = PaperTradingEngine(
                notify=lambda chat_id, message: notifications.append((chat_id, message)),
                state_path=Path(directory) / "paper.json",
            )
            engine.start(303)
            engine.accounts["303"]["last_processed_candle"] = 0
            candles = [
                Candle(index * 900, 4000.0, 4000.5, 3999.5, 4000.0)
                for index in range(210)
            ]
            engine.process_market_data(
                MarketData(candles, 4000.0, "test-feed"),
                datetime.fromtimestamp(candles[-1].timestamp + 900, timezone.utc),
            )

            self.assertIsNone(engine.accounts["303"]["position"])
            self.assertEqual(notifications, [])

    def test_trade_command_evaluates_latest_closed_bar_and_opens_demo_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            notifications: list[tuple[int, str]] = []
            candles = [
                Candle(index * 900, 4000.0, 4000.5, 3999.5, 4000.0)
                for index in range(210)
            ]
            market = MarketData(candles, 4000.0, "Yahoo GC=F", "GC=F")
            engine = PaperTradingEngine(
                notify=lambda chat_id, message: notifications.append((chat_id, message)),
                state_path=Path(directory) / "paper.json",
                market_data_loader=lambda _preferred: market,
            )

            with (
                patch("paper_trading.is_market_open", return_value=True),
                patch("paper_trading.crossover_signal", return_value="long"),
            ):
                message = engine.trade_now(404)

            self.assertIn("شراء تجريبية", message)
            self.assertEqual(engine.accounts["404"]["position"]["side"], "long")
            self.assertEqual(engine.accounts["404"]["last_processed_candle"], candles[-1].timestamp)
            self.assertEqual(notifications, [])


if __name__ == "__main__":
    unittest.main()
