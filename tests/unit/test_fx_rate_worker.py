import unittest
from decimal import Decimal

from src.fx_rate_worker import derive_base_rates, parse_ecb_csv


HEADER = "CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
ROWS = (
    "CAD,EUR,2026-07-17,1.6035,A\n"
    "CHF,EUR,2026-07-17,0.9228,A\n"
    "CNY,EUR,2026-07-17,7.7501,A\n"
    "GBP,EUR,2026-07-17,0.85098,A\n"
    "INR,EUR,2026-07-17,110.102,A\n"
    "JPY,EUR,2026-07-17,185.65,A\n"
    "USD,EUR,2026-07-17,1.1435,A\n"
)


class ECBParserTests(unittest.TestCase):
    def test_parses_and_derives_expected_inr_rates(self) -> None:
        provider_date, quotes = parse_ecb_csv(HEADER + ROWS)
        rates = derive_base_rates(quotes)

        self.assertEqual(str(provider_date), "2026-07-17")
        self.assertEqual(rates["EUR"]["rate"], Decimal("110.10200000"))
        self.assertEqual(rates["USD"]["rate"], Decimal("96.28508964"))
        self.assertFalse(rates["EUR"]["is_derived"])
        self.assertTrue(rates["USD"]["is_derived"])

    def test_rejects_missing_configured_currency(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing currencies: USD"):
            parse_ecb_csv(HEADER + ROWS.replace("USD,EUR,2026-07-17,1.1435,A\n", ""))

    def test_rejects_mixed_provider_dates(self) -> None:
        mixed = ROWS.replace(
            "USD,EUR,2026-07-17,1.1435,A",
            "USD,EUR,2026-07-16,1.1435,A",
        )
        with self.assertRaisesRegex(ValueError, "one coherent provider date"):
            parse_ecb_csv(HEADER + mixed)

    def test_rejects_nonpositive_quote(self) -> None:
        invalid = ROWS.replace(
            "USD,EUR,2026-07-17,1.1435,A",
            "USD,EUR,2026-07-17,0,A",
        )
        with self.assertRaisesRegex(ValueError, "positive and finite"):
            parse_ecb_csv(HEADER + invalid)

    def test_rejects_missing_csv_columns(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required CSV columns"):
            parse_ecb_csv("CURRENCY,OBS_VALUE\nUSD,1.1435\n")

    def test_rejects_non_eur_denominator(self) -> None:
        invalid = ROWS.replace("USD,EUR,2026-07-17,1.1435,A", "USD,USD,2026-07-17,1.1435,A")
        with self.assertRaisesRegex(ValueError, "denominator for USD is not EUR"):
            parse_ecb_csv(HEADER + invalid)

    def test_rejects_duplicate_currency_quote(self) -> None:
        duplicated = HEADER + ROWS + "USD,EUR,2026-07-17,1.2,A\n"
        with self.assertRaisesRegex(ValueError, "duplicate USD quote"):
            parse_ecb_csv(duplicated)

    def test_rejects_unparseable_quote_value(self) -> None:
        invalid = ROWS.replace("USD,EUR,2026-07-17,1.1435,A", "USD,EUR,2026-07-17,not-a-number,A")
        with self.assertRaisesRegex(ValueError, "Invalid ECB quote for USD"):
            parse_ecb_csv(HEADER + invalid)

    def test_rejects_unavailable_obs_status(self) -> None:
        invalid = ROWS.replace("USD,EUR,2026-07-17,1.1435,A", "USD,EUR,2026-07-17,1.1435,M")
        with self.assertRaisesRegex(ValueError, "not available"):
            parse_ecb_csv(HEADER + invalid)

    def test_derive_base_rates_requires_inr_anchor(self) -> None:
        quotes = {"USD": Decimal("1.1435"), "CAD": Decimal("1.6035")}
        with self.assertRaisesRegex(ValueError, "missing the INR anchor"):
            derive_base_rates(quotes)


if __name__ == "__main__":
    unittest.main()
