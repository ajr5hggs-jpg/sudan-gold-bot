import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gold_price_bot import (
    PriceSourceError,
    SOURCE_URL,
    build_daily_message,
    format_price,
    scrape_prices_from_html,
)


class GoldPriceParsingTests(unittest.TestCase):
    def test_extracts_prices_from_goldrate24_html(self) -> None:
        html = """
        <table>
          <tr><th>Gold Gram 24K</th><td>78,602.76</td><td>130.67</td></tr>
          <tr><th>Gold Gram 21K</th><td>68,777.41</td><td>114.34</td></tr>
          <tr><th>Gold Ounce 24K</th><td>2,444,545.74</td><td>4,063.88</td></tr>
          <tr><th>Gold Gram 24K</th><td>78,592.15</td><td>130.65</td></tr>
        </table>
        """
        self.assertEqual(
            scrape_prices_from_html(html),
            {"24k": "78602.76", "21k": "68777.41", "ounce": "2444545.74"},
        )

    def test_rejects_page_missing_a_required_rate(self) -> None:
        html = """
        <table>
          <tr><th>Gold Gram 21K</th><td>68,777.41</td></tr>
          <tr><th>Gold Gram 24K</th><td>78,602.76</td></tr>
        </table>
        """
        with self.assertRaisesRegex(PriceSourceError, "all three Sudan rates"):
            scrape_prices_from_html(html)

    def test_formats_prices_with_arabic_digits(self) -> None:
        self.assertEqual(format_price("2442829.85"), "٢٬٤٤٢٬٨٢٩٫٨٥")

    def test_daily_message_is_arabic_and_contains_source(self) -> None:
        moment = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Africa/Khartoum"))
        message = build_daily_message(
            {"21k": "68777.41", "24k": "78602.76", "ounce": "2444545.74"},
            moment,
        )
        self.assertIn("الثلاثاء", message)
        self.assertIn("عيار ٢١ للغرام: ٦٨٬٧٧٧٫٤١ جنيه سوداني", message)
        self.assertIn("عيار ٢٤ للغرام: ٧٨٬٦٠٢٫٧٦ جنيه سوداني", message)
        self.assertIn("أونصة ٢٤ قيراط: ٢٬٤٤٤٬٥٤٥٫٧٤ جنيه سوداني", message)
        self.assertIn(SOURCE_URL, message)


if __name__ == "__main__":
    unittest.main()
