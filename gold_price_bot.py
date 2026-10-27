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
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SOURCE_URL = "https://t.me/s/SudaGoldPrice/12"
LATEST_CHANNEL_URL = "https://t.me/s/SudaGoldPrice"
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

PRICE_LABELS = {
    "21k": re.compile(
        r"(?:\b21\s*(?:k|karat|carat)\b|(?:عيار|قيراط)\s*21\b|\b21\s*عيار\b)",
        re.IGNORECASE,
    ),
    "24k": re.compile(
        r"(?:\b24\s*(?:k|karat|carat)\b|(?:عيار|قيراط)\s*24\b|\b24\s*عيار\b)",
        re.IGNORECASE,
    ),
    "ounce": re.compile(r"(?:الأونصة|الاونصة|أونصة|اوقية|أوقية|ounce|oz)", re.IGNORECASE),
}
NUMBER_PATTERN = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[,٬\s]\d{3})+|\d+)(?:[.٫]\d+)?"
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("gold-price-bot")


class PriceSourceError(RuntimeError):
    """Raised when a usable set of prices cannot be read from the source."""


class TelegramAPIError(RuntimeError):
    """A Telegram Bot API request failed without exposing request credentials."""


class TelegramPostParser(HTMLParser):
    """Collect Telegram public-preview message text from the page HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[str] = []
        self._inside_message = False
        self._div_depth = 0
        self._current: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if not self._inside_message and tag == "div" and "tgme_widget_message_text" in classes:
            self._inside_message = True
            self._div_depth = 1
            self._current = []
            return

        if self._inside_message:
            if tag == "div":
                self._div_depth += 1
            elif tag == "br":
                self._current.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self._inside_message or tag != "div":
            return
        self._div_depth -= 1
        if self._div_depth == 0:
            message = "".join(self._current).strip()
            if message:
                self.messages.append(message)
            self._inside_message = False
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._inside_message:
            self._current.append(data)


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


def extract_prices(text: str) -> dict[str, str]:
    """Extract 21k, 24k and ounce values from a Telegram post's plain text."""
    normalized = normalize_digits(text)
    matches: list[tuple[int, int, str]] = []
    for key, pattern in PRICE_LABELS.items():
        match = pattern.search(normalized)
        if match:
            matches.append((match.start(), match.end(), key))
    matches.sort()

    prices: dict[str, str] = {}
    for index, (start, end, key) in enumerate(matches):
        if key in prices:
            continue
        next_label_start = matches[index + 1][0] if index + 1 < len(matches) else len(normalized)
        value_region = normalized[end : min(next_label_start, end + 180)]
        number_match = NUMBER_PATTERN.search(value_region)
        if number_match:
            prices[key] = _clean_price(number_match.group(0))

    missing = [key for key in PRICE_LABELS if key not in prices]
    if missing:
        raise PriceSourceError(
            "The source page does not expose all three prices (21k, 24k and ounce)."
        )
    return prices


def fetch_source_html(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SudanGoldPriceBot/1.0)"},
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read(1_500_000).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise PriceSourceError("Could not reach the Telegram price page.") from exc


def scrape_prices_from_html(html: str) -> dict[str, str]:
    parser = TelegramPostParser()
    parser.feed(html)
    if not parser.messages:
        raise PriceSourceError(
            "Telegram returned a contact page, not readable channel posts."
        )
    return extract_prices("\n".join(parser.messages))


def fetch_prices() -> dict[str, str]:
    """Try the supplied post URL first, then the public channel's latest-post page."""
    errors: list[str] = []
    for url in (SOURCE_URL, LATEST_CHANNEL_URL):
        try:
            return scrape_prices_from_html(fetch_source_html(url))
        except PriceSourceError as exc:
            errors.append(str(exc))
    raise PriceSourceError(errors[-1] if errors else "No price source is configured.")


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
            f"عيار ٢١: {format_price(prices['21k'])}",
            f"عيار ٢٤: {format_price(prices['24k'])}",
            f"الأونصة: {format_price(prices['ounce'])}",
            "",
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

        if command == "/start":
            self.subscribe(chat_id)
            self.send_message(
                chat_id,
                "تم الاشتراك في تحديث أسعار الذهب اليومي. "
                "ستصلك الأسعار يومياً الساعة ٩ صباحاً بتوقيت الخرطوم.\n"
                "استخدم /price لطلب الأسعار الآن أو /stop لإلغاء الاشتراك.",
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
                "أو /stop لإلغاء الاشتراك.",
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

    offset: int | None = None
    next_source_attempt = 0.0
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


if __name__ == "__main__":
    run()
