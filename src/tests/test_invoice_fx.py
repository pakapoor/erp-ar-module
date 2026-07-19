import unittest
from decimal import Decimal

from src.routers.invoices import convert_to_base
from src.schemas import InvoiceCreate


class InvoiceFXTests(unittest.TestCase):
    def test_converts_and_rounds_to_base_currency_precision(self) -> None:
        self.assertEqual(
            convert_to_base(Decimal("1180"), Decimal("96.28508964")),
            Decimal("113616.4058"),
        )

    def test_uses_half_up_rounding_for_financial_amounts(self) -> None:
        self.assertEqual(
            convert_to_base(Decimal("1"), Decimal("1.23445")),
            Decimal("1.2345"),
        )

    def test_currency_is_normalized_and_limited_to_supported_set(self) -> None:
        payload = InvoiceCreate.model_validate(
            {
                "customer_id": "customer-id",
                "currency": "usd",
                "line_items": [
                    {
                        "description": "Equipment",
                        "quantity": 1,
                        "unit_price": 1000,
                    }
                ],
            }
        )
        self.assertEqual(payload.currency, "USD")

        with self.assertRaisesRegex(ValueError, "unsupported currency"):
            InvoiceCreate.model_validate(
                {
                    "customer_id": "customer-id",
                    "currency": "AUD",
                    "line_items": [
                        {
                            "description": "Equipment",
                            "quantity": 1,
                            "unit_price": 1000,
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
