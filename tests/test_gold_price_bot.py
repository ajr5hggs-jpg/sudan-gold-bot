import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gold_price_bot import (
    PriceSourceError,
    build_daily_message,
    extract_prices,
    format_price,
    scrape_prices_from_html,
)


class GoldPriceParsingTests(unittest.TestCase):
    def test_extracts_latin_and_arabic_labels(self) -> None:
        prices = extract_prices(
            "21K: 125,000 SDG\nعيار ٢٤: 143,500 جنيه\nالأونصة: 4,325,000"
        )
        self.assertEqual(
            prices,
            {"21k": "125000", "24k": "143500", "ounce": "4325000"},
        )

    def test_extracts_price_values_from_telegram_post_html(self) -> None:
        html = """
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          سعر الذهب عيار 21: ١٢٥٬٠٠٠ جنيه<br>
          سعر الذهب عيار 24: ١٤٣٬٥٠٠ جنيه<br>
          سعر الأونصة: ٤٬٣٢٥٬٠٠٠ جنيه
        </div>
        """
        self.assertEqual(
            scrape_prices_from_html(html),
            {"21k": "125000", "24k": "143500", "ounce": "4325000"},
        )

    def test_rejects_contact_only_page(self) -> None:
        with self.assertRaisesRegex(PriceSourceError, "contact page"):
            scrape_prices_from_html("<html><title>Telegram: Contact</title></html>")

    def test_rejects_missing_price_categories(self) -> None:
        with self.assertRaisesRegex(PriceSourceError, "all three prices"):
            extract_prices("عيار 21: 125,000")

    def test_formats_prices_with_arabic_digits(self) -> None:
        self.assertEqual(format_price("4325000"), "٤٬٣٢٥٬٠٠٠")

    def test_daily_message_is_arabic_and_contains_source(self) -> None:
        moment = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Africa/Khartoum"))
        message = build_daily_message(
            {"21k": "125000", "24k": "143500", "ounce": "4325000"},
            moment,
        )
        self.assertIn("الثلاثاء", message)
        self.assertIn("عيار ٢١: ١٢٥٬٠٠٠", message)
        self.assertIn("عيار ٢٤: ١٤٣٬٥٠٠", message)
        self.assertIn("الأونصة: ٤٬٣٢٥٬٠٠٠", message)
        self.assertIn("https://t.me/s/SudaGoldPrice/12", message)


if __name__ == "__main__":
    unittest.main()
