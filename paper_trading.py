"""Paper-only XAUUSD strategy and market-data helpers."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yfinance as yf


YAHOO_TICKERS = ("XAUUSD=X", "GC=F", "PAXG-USD")
PAPER_STATE_PATH = Path(__file__).parent / "bot_data" / "paper_accounts.json"
BAR_SECONDS = 60 * 60
YAHOO_PERIOD = "60d"
YAHOO_INTERVAL = "1h"
PAPER_CHECK_SECONDS = 5 * 60
MIN_HISTORY_BARS = 200
STARTING_BALANCE_USD = 10_000.0
RISK_FRACTION = 0.02
STOP_POINTS = 150
TAKE_PROFIT_POINTS = 300
POINT_SIZE_USD = 0.01
MAX_QUOTE_AGE_SECONDS = 2 * BAR_SECONDS
REQUEST_TIMEOUT_SECONDS = 20
NEW_YORK = ZoneInfo("America/New_York")

logger = logging.getLogger("paper-trader")


def synchronized(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: PaperTradingEngine, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class PaperTradingError(RuntimeError):
    """Raised when paper trading cannot safely get usable market data."""


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class MarketData:
    candles: list[Candle]
    price: float
    source: str
    yahoo_ticker: str | None = None


def is_market_open(moment: datetime | None = None) -> bool:
    """Use the standard Sunday-evening through Friday gold-session window."""
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(NEW_YORK)
    clock = local.timetz().replace(tzinfo=None)

    if local.weekday() == 5:
        return False
    if local.weekday() == 6:
        return clock >= wall_time(18, 0)
    if local.weekday() == 4:
        return clock < wall_time(17, 0)
    return not (wall_time(17, 0) <= clock < wall_time(18, 0))


def ema_series(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("EMA period must be positive.")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1.0 - alpha)
        result[index] = current
    return result


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("RSI period must be positive.")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def calculate_rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        relative_strength = gain / loss
        return 100.0 - 100.0 / (1.0 + relative_strength)

    result[period] = calculate_rsi(average_gain, average_loss)
    for change_index in range(period, len(changes)):
        average_gain = (
            average_gain * (period - 1) + gains[change_index]
        ) / period
        average_loss = (
            average_loss * (period - 1) + losses[change_index]
        ) / period
        result[change_index + 1] = calculate_rsi(average_gain, average_loss)
    return result


def crossover_signal(
    index: int,
    fast_ema: list[float | None],
    slow_ema: list[float | None],
    rsi: list[float | None],
) -> str | None:
    """Return a direction only on a new EMA crossover with RSI confirming 50."""
    if index < 1 or index >= len(fast_ema):
        return None
    previous_fast = fast_ema[index - 1]
    previous_slow = slow_ema[index - 1]
    current_fast = fast_ema[index]
    current_slow = slow_ema[index]
    current_rsi = rsi[index]
    if None in (previous_fast, previous_slow, current_fast, current_slow, current_rsi):
        return None

    if previous_fast <= previous_slow and current_fast > current_slow and current_rsi > 50:
        return "long"
    if previous_fast >= previous_slow and current_fast < current_slow and current_rsi < 50:
        return "short"
    return None


def position_size_ounces(balance_usd: float, stop_distance_usd: float) -> float:
    if balance_usd <= 0 or stop_distance_usd <= 0:
        return 0.0
    risk_amount = balance_usd * RISK_FRACTION
    raw_size = risk_amount / stop_distance_usd
    return math.floor(raw_size * 10_000) / 10_000


def _finite_float(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Market data contains a non-finite price.")
    return number


def _field_series(frame: Any, name: str, ticker: str) -> Any:
    series = frame[name]
    if getattr(series, "ndim", 1) > 1:
        columns = getattr(series, "columns", [])
        series = series[ticker] if ticker in columns else series.iloc[:, 0]
    return series


def _parse_yahoo_frame(frame: Any, ticker: str, now_epoch: float) -> MarketData:
    if frame is None or frame.empty:
        raise PaperTradingError("Yahoo returned no candles.")

    opens = _field_series(frame, "Open", ticker)
    highs = _field_series(frame, "High", ticker)
    lows = _field_series(frame, "Low", ticker)
    closes = _field_series(frame, "Close", ticker)
    volumes = _field_series(frame, "Volume", ticker)

    candles: list[Candle] = []
    latest_price: float | None = None
    latest_timestamp: int | None = None
    for index, timestamp_value in enumerate(frame.index):
        moment = timestamp_value.to_pydatetime()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        timestamp = int(moment.timestamp())
        try:
            open_price = _finite_float(opens.iloc[index])
            high_price = _finite_float(highs.iloc[index])
            low_price = _finite_float(lows.iloc[index])
            close_price = _finite_float(closes.iloc[index])
            try:
                volume = _finite_float(volumes.iloc[index])
            except (TypeError, ValueError, OverflowError):
                volume = 0.0
        except (TypeError, ValueError, OverflowError):
            continue
        if close_price <= 0 or high_price < low_price:
            continue
        if latest_timestamp is None or timestamp >= latest_timestamp:
            latest_timestamp = timestamp
            latest_price = close_price
        if timestamp % BAR_SECONDS != 0 or timestamp + BAR_SECONDS > now_epoch:
            continue
        candles.append(
            Candle(timestamp, open_price, high_price, low_price, close_price, volume)
        )

    if latest_price is None or latest_timestamp is None:
        raise PaperTradingError("Yahoo returned no valid price.")
    if now_epoch - latest_timestamp > MAX_QUOTE_AGE_SECONDS:
        raise PaperTradingError("Yahoo's latest price is too old to trade safely.")
    candles.sort(key=lambda candle: candle.timestamp)
    if len(candles) < MIN_HISTORY_BARS:
        raise PaperTradingError("Yahoo returned too few completed H1 candles.")

    if ticker == "GC=F":
        source = "Yahoo GC=F (gold-futures proxy, not spot XAUUSD)"
    elif ticker == "PAXG-USD":
        source = "Yahoo PAXG-USD (tokenized-gold proxy)"
    else:
        source = "Yahoo XAUUSD=X (spot)"
    return MarketData(candles, latest_price, source, ticker)


def _fetch_yahoo(ticker: str, now_epoch: float) -> MarketData:
    frame = yf.download(
        ticker,
        period=YAHOO_PERIOD,
        interval=YAHOO_INTERVAL,
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
        prepost=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return _parse_yahoo_frame(frame, ticker, now_epoch)


def fetch_market_data(preferred_yahoo_ticker: str | None = None) -> MarketData:
    """Use the first working yfinance ticker in the configured order."""
    now_epoch = time.time()
    yahoo_order = [preferred_yahoo_ticker] if preferred_yahoo_ticker else []
    yahoo_order.extend(ticker for ticker in YAHOO_TICKERS if ticker not in yahoo_order)
    errors: list[str] = []

    for ticker in yahoo_order:
        try:
            return _fetch_yahoo(ticker, now_epoch)
        except Exception as exc:
            errors.append(f"{ticker}: {type(exc).__name__}")
            logger.info("yfinance ticker %s is unavailable; trying the next ticker.", ticker)

    logger.warning("All yfinance tickers failed: %s", ", ".join(errors))
    raise PaperTradingError(
        "No usable H1 data was returned by XAUUSD=X, GC=F, or PAXG-USD."
    )


def _new_account() -> dict[str, Any]:
    return {
        "enabled": False,
        "enabled_at": 0.0,
        "balance_usd": STARTING_BALANCE_USD,
        "position": None,
        "last_processed_candle": 0,
        "timeframe_seconds": BAR_SECONDS,
        "last_price": None,
        "last_price_at": 0.0,
        "source": "",
        "closed_trades": 0,
    }


class PaperTradingEngine:
    """Separate persistent paper account for each Telegram chat."""

    def __init__(
        self,
        notify: Callable[[int, str], None],
        state_path: Path = PAPER_STATE_PATH,
        market_data_loader: Callable[[str | None], MarketData] = fetch_market_data,
    ) -> None:
        self.notify = notify
        self.state_path = state_path
        self.market_data_loader = market_data_loader
        self.accounts = self._load_accounts()
        self._lock = threading.RLock()
        self.last_market_data: MarketData | None = None
        self.preferred_yahoo_ticker: str | None = None
        self._check_requested = False

    @property
    def check_requested(self) -> bool:
        return self._check_requested

    def _load_accounts(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Paper-trading state is invalid; refusing to discard accounts.") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError("Paper-trading state has an unexpected format.")
        accounts: dict[str, dict[str, Any]] = {}
        for chat_id, account in loaded.items():
            if not isinstance(account, dict):
                raise RuntimeError("A saved paper account has an unexpected format.")
            normalized = _new_account()
            normalized.update(account)
            if account.get("timeframe_seconds") != BAR_SECONDS:
                # Re-seed old M15 cursors on the next H1 poll; never replay
                # historical H1 signals as if they were current entries.
                normalized["last_processed_candle"] = 0
                normalized["timeframe_seconds"] = BAR_SECONDS
            accounts[str(chat_id)] = normalized
        return accounts

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=".paper-accounts-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                json.dump(self.accounts, temporary_file, ensure_ascii=False, indent=2, allow_nan=False)
                temporary_file.write("\n")
                temporary_path = temporary_file.name
            os.replace(temporary_path, self.state_path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _account(self, chat_id: int) -> dict[str, Any]:
        key = str(chat_id)
        if key not in self.accounts:
            self.accounts[key] = _new_account()
            self._save()
        return self.accounts[key]

    @synchronized
    def start(self, chat_id: int) -> str:
        account = self._account(chat_id)
        already_enabled = bool(account["enabled"])
        account["enabled"] = True
        account["enabled_at"] = time.time()
        if self.last_market_data and self.last_market_data.candles:
            account["last_processed_candle"] = self.last_market_data.candles[-1].timestamp
        self._check_requested = True
        self._save()
        if already_enabled:
            return "حساب التداول التجريبي يعمل بالفعل، وسيستمر الفحص كل ٥ دقائق."
        return (
            "تم تشغيل التداول التجريبي لهذا الحساب.\n"
            "لا تُرسل أوامر حقيقية إلى وسيط. يبدأ الدخول عند إغلاق شمعة H1 مؤهلة؛ "
            "قد ينتظر الحساب اكتمال بيانات EMA 200.\n"
            "مصدر البيانات: yfinance بالترتيب XAUUSD=X ثم GC=F ثم PAXG-USD. "
            "استخدم /auto_off لإيقاف الدخول."
        )

    @synchronized
    def stop(self, chat_id: int) -> str:
        account = self._account(chat_id)
        account["enabled"] = False
        if self.last_market_data and self.last_market_data.candles:
            account["last_processed_candle"] = self.last_market_data.candles[-1].timestamp
        self._check_requested = account.get("position") is not None
        self._save()
        if account.get("position"):
            return (
                "تم إيقاف فتح صفقات جديدة. ستبقى الصفقة التجريبية المفتوحة تحت مراقبة "
                "وقف الخسارة والهدف؛ استخدم /closeall لإغلاقها الآن."
            )
        return "تم إيقاف التداول التجريبي لهذا الحساب."

    @synchronized
    def balance_message(self, chat_id: int) -> str:
        account = self._account(chat_id)
        balance = float(account["balance_usd"])
        position = account.get("position")
        if position:
            price = account.get("last_price")
            if price is None:
                price = position["entry_price"]
            pnl = self._unrealized_pnl(position, float(price))
            equity = balance + pnl
            side = "شراء" if position["side"] == "long" else "بيع"
            open_line = (
                f"الصفقة المفتوحة: {side} {position['size_oz']:.4f} أونصة\n"
                f"السعر الحالي/الأخير: ${float(price):,.2f}\n"
                f"الربح/الخسارة غير المحققة: ${pnl:,.2f}\n"
                f"وقف الخسارة: ${position['stop_price']:,.2f} | "
                f"الهدف: ${position['target_price']:,.2f}\n"
            )
        else:
            pnl = 0.0
            equity = balance
            open_line = "الصفقات المفتوحة: لا توجد\n"
        status = "يعمل" if account["enabled"] else "متوقف"
        return (
            "حساب تجريبي فقط — لا توجد أموال أو أوامر حقيقية.\n"
            f"الحالة: {status}\n"
            f"الرصيد المحقق: ${balance:,.2f}\n"
            f"حقوق الحساب التقديرية: ${equity:,.2f}\n"
            f"{open_line}"
            f"الصفقات المغلقة: {int(account['closed_trades'])}"
        )

    @synchronized
    def close_all(self, chat_id: int) -> str:
        account = self._account(chat_id)
        position = account.get("position")
        if not position:
            return "لا توجد صفقة تجريبية مفتوحة لإغلاقها."
        price = account.get("last_price")
        if price is None:
            return "لا يتوفر سعر بعد؛ لا يمكن إغلاق الصفقة التجريبية الآن."
        message = self._close_position(
            chat_id,
            account,
            float(price),
            "إغلاق يدوي",
            notify_chat=False,
        )
        self._save()
        return message

    @synchronized
    def trade_now(self, chat_id: int) -> str:
        account = self._account(chat_id)
        if account.get("position"):
            return "لديك صفقة تجريبية مفتوحة بالفعل؛ لن أفتح صفقة ثانية."
        if not is_market_open():
            return "سوق الذهب مغلق الآن؛ لم يتم فتح صفقة."

        market = self.market_data_loader(self.preferred_yahoo_ticker)
        self.last_market_data = market
        if market.yahoo_ticker:
            self.preferred_yahoo_ticker = market.yahoo_ticker
        account["last_price"] = market.price
        account["last_price_at"] = time.time()
        account["source"] = market.source

        candles = sorted(market.candles, key=lambda candle: candle.timestamp)
        if len(candles) < MIN_HISTORY_BARS:
            return "لا توجد بيانات كافية لحساب EMA 200؛ لم يتم فتح صفقة."
        latest_index = len(candles) - 1
        latest = candles[latest_index]
        if latest.timestamp <= int(account.get("last_processed_candle") or 0):
            self._save()
            return "تم فحص آخر شمعة H1 بالفعل؛ لا توجد إشارة جديدة للتنفيذ."

        closes = [candle.close for candle in candles]
        signal = crossover_signal(
            latest_index,
            ema_series(closes, 50),
            ema_series(closes, 200),
            rsi_series(closes, 14),
        )
        account["last_processed_candle"] = latest.timestamp
        if signal is None:
            self._save()
            return (
                "لا يوجد تقاطع جديد مؤكد بإشارة RSI على آخر شمعة H1 مكتملة؛ "
                "لم يتم فتح صفقة."
            )

        message = self._open_position(
            chat_id,
            account,
            latest,
            signal,
            market.source,
            notify_chat=False,
        )
        self._save()
        return message

    @synchronized
    def run_cycle(self, moment: datetime | None = None) -> None:
        self._check_requested = False
        current = moment or datetime.now(timezone.utc)
        if not is_market_open(current):
            return
        if not any(
            account.get("enabled") or account.get("position")
            for account in self.accounts.values()
        ):
            return

        market = self.market_data_loader(self.preferred_yahoo_ticker)
        self.last_market_data = market
        if market.yahoo_ticker:
            self.preferred_yahoo_ticker = market.yahoo_ticker
        self.process_market_data(market, current)

    @synchronized
    def process_market_data(self, market: MarketData, moment: datetime | None = None) -> None:
        current = moment or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        candles = sorted(market.candles, key=lambda candle: candle.timestamp)
        if not candles:
            raise PaperTradingError("No completed H1 candles are available.")

        closes = [candle.close for candle in candles]
        fast_ema = ema_series(closes, 50)
        slow_ema = ema_series(closes, 200)
        rsi = rsi_series(closes, 14)
        now_epoch = current.timestamp()

        for chat_key, account in self.accounts.items():
            chat_id = int(chat_key)
            account["last_price"] = market.price
            account["last_price_at"] = now_epoch
            account["source"] = market.source
            last_processed = int(account.get("last_processed_candle") or 0)

            if last_processed == 0:
                account["last_processed_candle"] = candles[-1].timestamp
                self._check_live_exit(chat_id, account, market.price)
                continue
            if last_processed < candles[0].timestamp - BAR_SECONDS:
                logger.info("Paper account history gap; reseeding the latest candle.")
                account["last_processed_candle"] = candles[-1].timestamp
                self._check_live_exit(chat_id, account, market.price)
                continue

            for index, candle in enumerate(candles):
                if candle.timestamp <= last_processed:
                    continue
                had_position = account.get("position") is not None
                if had_position:
                    exit_event = self._candle_exit(account["position"], candle)
                    if exit_event:
                        exit_price, reason = exit_event
                        message = self._close_position(
                            chat_id, account, exit_price, reason, notify_chat=True
                        )
                        logger.info("Paper position closed (%s).", reason)
                if (
                    account.get("position") is None
                    and not had_position
                    and account.get("enabled")
                    and candle.timestamp + BAR_SECONDS > float(account["enabled_at"])
                    and len(candles) >= MIN_HISTORY_BARS
                ):
                    signal = crossover_signal(index, fast_ema, slow_ema, rsi)
                    if signal:
                        self._open_position(chat_id, account, candle, signal, market.source)
                account["last_processed_candle"] = candle.timestamp

            self._check_live_exit(chat_id, account, market.price)

        self._save()

    def _open_position(
        self,
        chat_id: int,
        account: dict[str, Any],
        candle: Candle,
        side: str,
        source: str,
        notify_chat: bool = True,
    ) -> str:
        balance = float(account["balance_usd"])
        stop_distance = STOP_POINTS * POINT_SIZE_USD
        size = position_size_ounces(balance, stop_distance)
        if size <= 0:
            return "الرصيد التجريبي لا يكفي لفتح صفقة."
        entry = round(candle.close, 2)
        if side == "long":
            stop = round(entry - stop_distance, 2)
            target = round(entry + TAKE_PROFIT_POINTS * POINT_SIZE_USD, 2)
        else:
            stop = round(entry + stop_distance, 2)
            target = round(entry - TAKE_PROFIT_POINTS * POINT_SIZE_USD, 2)
        position = {
            "side": side,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "size_oz": size,
            "risk_usd": balance * RISK_FRACTION,
            "opened_at": candle.timestamp + BAR_SECONDS,
            "source": source,
        }
        account["position"] = position
        direction = "شراء" if side == "long" else "بيع"
        message = (
            f"فتح صفقة {direction} تجريبية على الذهب.\n"
            f"سعر الدخول: ${entry:,.2f}\n"
            f"الحجم: {size:.4f} أونصة\n"
            f"وقف الخسارة: ${stop:,.2f} ({STOP_POINTS} نقطة)\n"
            f"الهدف: ${target:,.2f} ({TAKE_PROFIT_POINTS} نقطة)\n"
            f"المخاطرة المخططة: ${balance * RISK_FRACTION:,.2f} (٢٪)\n"
            f"مصدر السعر: {source}\n"
            "محاكاة فقط؛ لا يتم إرسال أوامر حقيقية."
        )
        if notify_chat:
            self._notify(chat_id, message)
        logger.info("Paper position opened from a new H1 crossover.")
        return message

    def _candle_exit(self, position: dict[str, Any], candle: Candle) -> tuple[float, str] | None:
        stop = float(position["stop_price"])
        target = float(position["target_price"])
        if position["side"] == "long":
            if candle.low <= stop:
                return stop, "وقف الخسارة"
            if candle.high >= target:
                return target, "الهدف"
        else:
            if candle.high >= stop:
                return stop, "وقف الخسارة"
            if candle.low <= target:
                return target, "الهدف"
        return None

    def _check_live_exit(
        self, chat_id: int, account: dict[str, Any], current_price: float
    ) -> None:
        position = account.get("position")
        if not position:
            return
        stop = float(position["stop_price"])
        target = float(position["target_price"])
        side = position["side"]
        if side == "long" and current_price <= stop:
            self._close_position(chat_id, account, min(current_price, stop), "وقف الخسارة", True)
        elif side == "long" and current_price >= target:
            self._close_position(chat_id, account, target, "الهدف", True)
        elif side == "short" and current_price >= stop:
            self._close_position(chat_id, account, max(current_price, stop), "وقف الخسارة", True)
        elif side == "short" and current_price <= target:
            self._close_position(chat_id, account, target, "الهدف", True)

    @staticmethod
    def _unrealized_pnl(position: dict[str, Any], price: float) -> float:
        move = price - float(position["entry_price"])
        if position["side"] == "short":
            move = -move
        return move * float(position["size_oz"])

    def _close_position(
        self,
        chat_id: int,
        account: dict[str, Any],
        exit_price: float,
        reason: str,
        notify_chat: bool,
    ) -> str:
        position = account.get("position")
        if not position:
            return "لا توجد صفقة تجريبية مفتوحة لإغلاقها."
        exit_price = round(float(exit_price), 2)
        pnl = self._unrealized_pnl(position, exit_price)
        account["balance_usd"] = float(account["balance_usd"]) + pnl
        account["position"] = None
        account["closed_trades"] = int(account.get("closed_trades", 0)) + 1
        message = (
            f"إغلاق الصفقة التجريبية — {reason}.\n"
            f"سعر الخروج: ${exit_price:,.2f}\n"
            f"الربح/الخسارة: ${pnl:,.2f}\n"
            f"الرصيد المحقق: ${float(account['balance_usd']):,.2f}"
        )
        if notify_chat:
            self._notify(chat_id, message)
        return message

    def _notify(self, chat_id: int, message: str) -> None:
        try:
            self.notify(chat_id, message)
        except Exception as exc:
            logger.warning("Could not send a paper-trading notification (%s).", type(exc).__name__)
