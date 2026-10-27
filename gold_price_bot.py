#!/usr/bin/env python3
"""Daily Sudan gold-price Telegram bot."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from paper_trading import PAPER_CHECK_SECONDS, PaperTradingEngine, PaperTradingError


SOURCE_URL = "https://www.goldrate24.com/gold-prices/middle-east/sudan/"
TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TIMEZONE = ZoneInfo("Africa/Khartoum")
SEND_HOUR = 9
RETRY_SECONDS = 60 * 60
STATE_PATH = Path(__file__).parent / "bot_data" / "state.json"
REQUEST_TIMEOUT_SECONDS = 35

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
ARABIC_NUMBER_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
WEEKDAYS_AR = (
    "الإثنين",
    "الثلاثاء",
    "الأربعاء",
    "الخميس",
    "الجمعة",
    "السبت",
    "الأحد",
)
MONTHS_AR = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)

PRICE_ROW_LABELS = {
    "gold gram 21k": "21k",
    "gold gram 24k": "24k",
    "gold ounce 24k": "ounce",
}
PRICE_VALUE_PATTERN = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*$")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gold-price-bot")


class PriceSourceError(RuntimeError):
    """Raised when a usable set of prices cannot be read from the source."""


class TelegramAPIError(RuntimeError):
    """A Telegram Bot API request failed without exposing request credentials."""


class GoldPricePageParser(HTMLParser):
    """Read the labeled SDG price cells from the public Sudan rates page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._inside_row = False
        self._inside_cell = False
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._inside_row = True
            self._current_row = []
        elif self._inside_row and tag in ("th", "td"):
            self._inside_cell = True
            self._current_cell = []
        elif self._inside_cell and tag == "br":
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._inside_cell:
            self._current_row.append(" ".join("".join(self._current_cell).split()))
            self._inside_cell = False
            self._current_cell = []
        elif tag == "tr" and self._inside_row:
            if self._current_row:
                self.rows.append(self._current_row)
            self._inside_row = False
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._inside_cell:
            self._current_cell.append(data)


def normalize_digits(value: str) -> str:
    return value.translate(ARABIC_NUMBER_MAP).replace("٫", ".").replace("٬", ",")


def _clean_price(raw_value: str) -> str:
    normalized = normalize_digits(raw_value).replace(",", "").replace(" ", "").strip()
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise PriceSourceError("The source included an unreadable price.") from exc
    if not value.is_finite() or value <= 0:
        raise PriceSourceError("The source included an invalid price.")
    return format(value, "f")


def fetch_source_html(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SudanGoldPriceBot/1.0)"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read(1_500_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise PriceSourceError("Could not reach the public gold-price page.") from exc


def scrape_prices_from_html(html: str) -> dict[str, str]:
    parser = GoldPricePageParser()
    parser.feed(html)
    prices: dict[str, str] = {}
    for row in parser.rows:
        if len(row) < 2:
            continue
        key = PRICE_ROW_LABELS.get(" ".join(row[0].lower().split()))
        if not key or key in prices:
            continue
        normalized = normalize_digits(row[1])
        match = PRICE_VALUE_PATTERN.fullmatch(normalized)
        if match:
            prices[key] = _clean_price(match.group(1))

    missing = [key for key in ("21k", "24k", "ounce") if key not in prices]
    if missing:
        raise PriceSourceError(
            "GoldRate24 did not expose all three Sudan rates (21k, 24k and ounce)."
        )
    return prices


def fetch_prices() -> dict[str, str]:
    """Fetch the latest GoldRate24 Sudan 21k, 24k gram and 24k ounce prices in SDG."""
    return scrape_prices_from_html(fetch_source_html(SOURCE_URL))


def format_price(value: str) -> str:
    number = Decimal(value)
    formatted = f"{number:,.2f}".rstrip("0").rstrip(".")
    return formatted.replace(",", "٬").replace(".", "٫").translate(ARABIC_DIGITS)


def format_arabic_date(moment: datetime) -> str:
    day = str(moment.day).translate(ARABIC_DIGITS)
    year = str(moment.year).translate(ARABIC_DIGITS)
    return f"{WEEKDAYS_AR[moment.weekday()]}، {day} {MONTHS_AR[moment.month - 1]} {year}"


def build_daily_message(prices: dict[str, str], moment: datetime) -> str:
    return "\n".join(
        (
            "أسعار الذهب في السودان",
            format_arabic_date(moment),
            "",
            f"عيار ٢١ للغرام: {format_price(prices['21k'])} جنيه سوداني",
            f"عيار ٢٤ للغرام: {format_price(prices['24k'])} جنيه سوداني",
            f"أونصة ٢٤ قيراط: {format_price(prices['ounce'])} جنيه سوداني",
            "",
            "الأسعار خام وقد تختلف أسعار البيع بحسب المصنعية والرسوم.",
            f"المصدر: {SOURCE_URL}",
        )
    )


def load_state() -> dict[str, Any]:
    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"subscribers": [], "last_sent_date": ""}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bot state file is invalid; refusing to discard subscribers.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("subscribers"), list):
        raise RuntimeError("Bot state file has an unexpected format.")
    state.setdefault("last_sent_date", "")
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=STATE_PATH.parent,
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(state, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_path = temporary_file.name
        os.replace(temporary_path, STATE_PATH)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


class TelegramBot:
    def __init__(self, token: str, state: dict[str, Any]) -> None:
        self._token = token
        self.state = state
        self.paper_trader = PaperTradingEngine(notify=self.send_message)

    def api_call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{TELEGRAM_API_BASE}{self._token}/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TelegramAPIError(f"Telegram API {method} failed (HTTP {exc.code}).") from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise TelegramAPIError(f"Telegram API {method} could not be completed.") from None
        if not result.get("ok"):
            error_code = result.get("error_code", "unknown")
            raise TelegramAPIError(f"Telegram API {method} returned error {error_code}.")
        return result.get("result")

    def send_message(self, chat_id: int | str, text: str) -> None:
        self.api_call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )

    def subscribe(self, chat_id: int) -> None:
        subscribers = self.state["subscribers"]
        if chat_id not in subscribers:
            subscribers.append(chat_id)
            save_state(self.state)

    def unsubscribe(self, chat_id: int) -> None:
        subscribers = self.state["subscribers"]
        if chat_id in subscribers:
            subscribers.remove(chat_id)
            save_state(self.state)

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = str(message.get("text") or "").strip()
        if chat_id is None or not text.startswith("/"):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

        if command == "/startbot":
            self.send_message(chat_id, self.paper_trader.start(int(chat_id)))
        elif command == "/stopbot":
            self.send_message(chat_id, self.paper_trader.stop(int(chat_id)))
        elif command == "/balance":
            self.send_message(chat_id, self.paper_trader.balance_message(int(chat_id)))
        elif command == "/closeall":
            self.send_message(chat_id, self.paper_trader.close_all(int(chat_id)))
        elif command == "/start":
            self.subscribe(chat_id)
            self.send_message(
                chat_id,
                "تم الاشتراك في تحديث أسعار الذهب اليومي. "
                "ستصلك الأسعار يومياً الساعة ٩ صباحاً بتوقيت الخرطوم.\n"
                "استخدم /price لطلب الأسعار الآن أو /stop لإلغاء الاشتراك. "
                "للتداول التجريبي استخدم /startbot.",
            )
        elif command == "/stop":
            self.unsubscribe(chat_id)
            self.send_message(chat_id, "تم إلغاء الاشتراك في تحديثات أسعار الذهب.")
        elif command == "/price":
            try:
                self.send_message(chat_id, build_daily_message(fetch_prices(), datetime.now(TIMEZONE)))
            except PriceSourceError:
                self.send_message(
                    chat_id,
                    "تعذر قراءة الأسعار من المصدر حالياً. لن أرسل أسعاراً غير مؤكدة؛ يرجى المحاولة لاحقاً.",
                )
        elif command == "/help":
            self.send_message(
                chat_id,
                "أرسل /start للاشتراك في الأسعار اليومية، /price لطلبها الآن، "
                "أو /stop لإلغاء الاشتراك.\n"
                "للتداول التجريبي: /startbot للتشغيل، /stopbot لإيقاف الدخول، "
                "/balance لعرض الرصيد، و /closeall لإغلاق الصفقة المفتوحة. "
                "التداول المحاكى لا يرسل أوامر إلى وسيط.",
            )


def send_daily_update(bot: TelegramBot, state: dict[str, Any], today: str) -> None:
    prices = fetch_prices()
    message = build_daily_message(prices, datetime.now(TIMEZONE))
    subscribers = list(state["subscribers"])
    if not subscribers:
        logger.info("Prices were read for %s; there are no subscribers yet.", today)
    for chat_id in subscribers:
        try:
            bot.send_message(chat_id, message)
        except TelegramAPIError as exc:
            logger.warning("Could not send the daily update to a subscriber: %s", exc)
            if "error 403" in str(exc):
                bot.unsubscribe(chat_id)
    state["last_sent_date"] = today
    save_state(state)
    logger.info("Daily price update completed for %s.", today)


def run() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Replit Secrets.")

    state = load_state()
    bot = TelegramBot(token, state)
    identity = bot.api_call("getMe")
    username = identity.get("username", "unknown") if isinstance(identity, dict) else "unknown"
    logger.info("Connected to Telegram as @%s.", username)
    logger.info(
        "Daily delivery is set for %02d:00 Africa/Khartoum; %d subscriber(s) saved.",
        SEND_HOUR,
        len(state["subscribers"]),
    )
    logger.info(
        "Paper trading is available per chat; market checks run every %d seconds.",
        PAPER_CHECK_SECONDS,
    )

    offset: int | None = None
    next_source_attempt = 0.0
    next_paper_check = 0.0
    while True:
        now = datetime.now(TIMEZONE)
        today = now.date().isoformat()
        if now.hour >= SEND_HOUR and state.get("last_sent_date") != today:
            if time.monotonic() >= next_source_attempt:
                try:
                    send_daily_update(bot, state, today)
                except PriceSourceError as exc:
                    logger.warning("Daily update postponed: %s", exc)
                    next_source_attempt = time.monotonic() + RETRY_SECONDS
                except TelegramAPIError as exc:
                    logger.warning("Daily update could not be delivered: %s", exc)
                    next_source_attempt = time.monotonic() + RETRY_SECONDS

        try:
            polling_payload: dict[str, Any] = {
                "timeout": 25,
                "allowed_updates": ["message"],
            }
            if offset is not None:
                polling_payload["offset"] = offset
            updates = bot.api_call("getUpdates", polling_payload)
            for update in updates or []:
                offset = int(update["update_id"]) + 1
                try:
                    bot.handle_update(update)
                except (PriceSourceError, TelegramAPIError) as exc:
                    logger.warning("Could not handle a bot command: %s", exc)
        except TelegramAPIError as exc:
            logger.warning("Bot polling will retry: %s", exc)
            time.sleep(5)

        if time.monotonic() >= next_paper_check or bot.paper_trader.check_requested:
            try:
                bot.paper_trader.run_cycle()
            except PaperTradingError as exc:
                logger.warning("Paper-trading cycle skipped: %s", exc)
            except Exception:
                logger.exception("Paper-trading cycle failed unexpectedly.")
            finally:
                next_paper_check = time.monotonic() + PAPER_CHECK_SECONDS


if __name__ == "__main__":
    run()
