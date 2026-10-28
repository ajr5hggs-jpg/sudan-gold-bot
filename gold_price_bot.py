#!/usr/bin/env python3
"""Daily Sudan gold-price Telegram bot."""

from __future__ import annotations

import asyncio
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
from telegram import BotCommand, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes


SOURCE_URL = "https://www.goldrate24.com/gold-prices/middle-east/sudan/"
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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("gold-price-bot")


class PriceSourceError(RuntimeError):
    """Raised when a usable set of prices cannot be read from the source."""


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


MENU_TEXT = (
    "الأوامر المتاحة:\n"
    "/price — أسعار الذهب السوداني الآن\n"
    "/balance — رصيد الحساب التجريبي (١٠٬٠٠٠ دولار)\n"
    "/trade — فحص إشارة EMA/RSI الآن\n"
    "/auto_on — تشغيل الفحص الآلي كل ٥ دقائق\n"
    "/auto_off — إيقاف فتح صفقات جديدة\n"
    "/closeall — إغلاق الصفقة التجريبية المفتوحة\n"
    "/stop — إلغاء تحديثات الأسعار اليومية فقط\n\n"
    "التداول تجريبي فقط ولا يرسل أوامر إلى وسيط."
)


def subscribe_chat(state: dict[str, Any], chat_id: int) -> None:
    subscribers = state["subscribers"]
    if chat_id not in subscribers:
        subscribers.append(chat_id)
        save_state(state)


def unsubscribe_chat(state: dict[str, Any], chat_id: int) -> None:
    subscribers = state["subscribers"]
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_state(state)


def paper_trader_from(context: ContextTypes.DEFAULT_TYPE) -> PaperTradingEngine:
    return context.application.bot_data["paper_trader"]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    chat_id = update.effective_chat.id
    state = context.application.bot_data["state"]
    subscribe_chat(state, chat_id)
    await update.effective_message.reply_text(
        "تم الاشتراك في أسعار الذهب اليومية الساعة ٩ صباحاً بتوقيت الخرطوم.\n\n"
        + MENU_TEXT
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(MENU_TEXT)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    unsubscribe_chat(context.application.bot_data["state"], update.effective_chat.id)
    await update.effective_message.reply_text(
        "تم إلغاء تحديثات أسعار الذهب اليومية. إعداد التداول التجريبي لم يتغير."
    )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    try:
        prices = await asyncio.to_thread(fetch_prices)
    except PriceSourceError:
        await update.effective_message.reply_text(
            "تعذر قراءة الأسعار من المصدر حالياً. لن أرسل أسعاراً غير مؤكدة؛ يرجى المحاولة لاحقاً."
        )
        return
    await update.effective_message.reply_text(
        build_daily_message(prices, datetime.now(TIMEZONE))
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    text = await asyncio.to_thread(
        paper_trader_from(context).balance_message, update.effective_chat.id
    )
    await update.effective_message.reply_text(text)


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    try:
        text = await asyncio.to_thread(
            paper_trader_from(context).trade_now, update.effective_chat.id
        )
    except PaperTradingError as exc:
        logger.warning("Manual paper trade could not be evaluated: %s", exc)
        text = "تعذر جلب شموع ١٥ دقيقة من yfinance؛ لم يتم فتح صفقة."
    await update.effective_message.reply_text(text)


async def auto_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    text = await asyncio.to_thread(
        paper_trader_from(context).start, update.effective_chat.id
    )
    context.application.bot_data["worker_wakeup"].set()
    await update.effective_message.reply_text(text)


async def auto_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    text = await asyncio.to_thread(
        paper_trader_from(context).stop, update.effective_chat.id
    )
    context.application.bot_data["worker_wakeup"].set()
    await update.effective_message.reply_text(text)


async def closeall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    text = await asyncio.to_thread(
        paper_trader_from(context).close_all, update.effective_chat.id
    )
    await update.effective_message.reply_text(text)


async def send_daily_update(
    application: Application, state: dict[str, Any], today: str
) -> None:
    prices = await asyncio.to_thread(fetch_prices)
    message = build_daily_message(prices, datetime.now(TIMEZONE))
    subscribers = list(state["subscribers"])
    if not subscribers:
        logger.info("Prices were read for %s; there are no subscribers yet.", today)
    for chat_id in subscribers:
        try:
            await application.bot.send_message(chat_id=chat_id, text=message)
        except Forbidden:
            logger.info("Removing a Telegram chat that no longer accepts messages.")
            unsubscribe_chat(state, chat_id)
        except TelegramError as exc:
            logger.warning("Could not send a daily price update: %s", exc)
    state["last_sent_date"] = today
    save_state(state)
    logger.info("Daily price update completed for %s.", today)


async def notification_worker(application: Application) -> None:
    queue = application.bot_data["paper_notifications"]
    while True:
        chat_id, text = await queue.get()
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("Could not send a paper-trading notification: %s", exc)
        finally:
            queue.task_done()


async def background_worker(application: Application) -> None:
    state = application.bot_data["state"]
    paper_trader = application.bot_data["paper_trader"]
    wakeup = application.bot_data["worker_wakeup"]
    next_source_attempt = 0.0
    next_paper_check = 0.0

    while True:
        now = datetime.now(TIMEZONE)
        today = now.date().isoformat()
        monotonic_now = time.monotonic()
        if (
            now.hour >= SEND_HOUR
            and state.get("last_sent_date") != today
            and monotonic_now >= next_source_attempt
        ):
            try:
                await send_daily_update(application, state, today)
            except PriceSourceError as exc:
                logger.warning("Daily update postponed: %s", exc)
                next_source_attempt = time.monotonic() + RETRY_SECONDS
            except TelegramError as exc:
                logger.warning("Daily update could not be delivered: %s", exc)
                next_source_attempt = time.monotonic() + RETRY_SECONDS

        if time.monotonic() >= next_paper_check or paper_trader.check_requested:
            try:
                await asyncio.to_thread(paper_trader.run_cycle)
            except PaperTradingError as exc:
                logger.warning("Paper-trading cycle skipped: %s", exc)
            except Exception:
                logger.exception("Paper-trading cycle failed unexpectedly.")
            finally:
                next_paper_check = time.monotonic() + PAPER_CHECK_SECONDS

        timeout = min(25.0, max(1.0, next_paper_check - time.monotonic()))
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        wakeup.clear()


async def post_init(application: Application) -> None:
    loop = asyncio.get_running_loop()
    notifications: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

    def enqueue_notification(chat_id: int, text: str) -> None:
        loop.call_soon_threadsafe(notifications.put_nowait, (chat_id, text))

    application.bot_data["paper_notifications"] = notifications
    application.bot_data["paper_trader"] = PaperTradingEngine(
        notify=enqueue_notification
    )
    application.bot_data["worker_wakeup"] = asyncio.Event()
    application.bot_data["background_tasks"] = [
        asyncio.create_task(
            notification_worker(application),
            name="paper-notifications",
        ),
        asyncio.create_task(
            background_worker(application),
            name="bot-scheduler",
        ),
    ]
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Menu and daily Sudan gold prices"),
            BotCommand("price", "Get Sudan gold prices now"),
            BotCommand("balance", "Show demo balance"),
            BotCommand("trade", "Check the current M15 signal"),
            BotCommand("auto_on", "Start five-minute paper trading"),
            BotCommand("auto_off", "Stop new paper entries"),
            BotCommand("closeall", "Close the open paper trade"),
            BotCommand("stop", "Unsubscribe from daily prices"),
            BotCommand("help", "Show commands"),
        ]
    )
    identity = await application.bot.get_me()
    logger.info("Connected to Telegram as @%s.", identity.username or "unknown")
    logger.info(
        "Daily Sudan gold prices remain scheduled for %02d:00 Africa/Khartoum.",
        SEND_HOUR,
    )
    logger.info("Paper-trading commands are ready; checks run every five minutes.")


async def post_shutdown(application: Application) -> None:
    tasks = application.bot_data.get("background_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled Telegram update error: %s", context.error)


def run() -> None:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing. Add it in Replit Secrets.")

    state = load_state()
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["state"] = state
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler(["auto_on", "startbot"], auto_on_command))
    application.add_handler(CommandHandler(["auto_off", "stopbot"], auto_off_command))
    application.add_handler(CommandHandler("closeall", closeall_command))
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
