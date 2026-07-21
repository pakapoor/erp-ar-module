from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from fastapi import HTTPException

from src.exceptions import BusinessRuleException
from src.routers import payments
from src.schemas import AllocationItem


class Result:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


class GetExchangeRateTests(IsolatedAsyncioTestCase):
    async def test_same_currency_short_circuits_without_db_lookup(self):
        db = AsyncMock()
        rate_id, rate, rate_date_used, warning = await payments.get_exchange_rate(
            db, "t", "INR", "INR", date(2026, 7, 1)
        )
        db.execute.assert_not_awaited()
        self.assertIsNone(rate_id)
        self.assertEqual(rate, Decimal("1"))
        self.assertIsNone(warning)

    async def test_missing_rate_raises_503(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException) as ctx:
            await payments.get_exchange_rate(db, "t", "USD", "INR", date(2026, 7, 1))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("FX_RATE_UNAVAILABLE", ctx.exception.detail)

    async def test_prior_business_day_rate_returns_warning(self):
        rate_record = SimpleNamespace(
            id="rate-1", rate=Decimal("83.5"), effective_date=date(2026, 6, 29),
        )
        db = AsyncMock()
        db.execute.return_value = Result(rate_record)
        rate_id, rate, rate_date_used, warning = await payments.get_exchange_rate(
            db, "t", "USD", "INR", date(2026, 7, 1)
        )
        self.assertEqual(rate_id, "rate-1")
        self.assertEqual(rate, Decimal("83.5"))
        self.assertIn("not available", warning)

    async def test_same_day_rate_has_no_warning(self):
        rate_record = SimpleNamespace(
            id="rate-1", rate=Decimal("83.5"), effective_date=date(2026, 7, 1),
        )
        db = AsyncMock()
        db.execute.return_value = Result(rate_record)
        _, _, _, warning = await payments.get_exchange_rate(
            db, "t", "USD", "INR", date(2026, 7, 1)
        )
        self.assertIsNone(warning)


class AutoAllocateTests(IsolatedAsyncioTestCase):
    async def test_no_open_invoices_raises_business_rule(self):
        db = AsyncMock()
        db.execute.return_value = Result(rows=[])
        with self.assertRaises(BusinessRuleException) as ctx:
            await payments.auto_allocate(db, "t", "e", "cust-1", Decimal("100"), "INR")
        self.assertEqual(ctx.exception.code, "NO_OPEN_INVOICES")

    async def test_fifo_allocates_oldest_first_and_stops_when_exhausted(self):
        inv1 = SimpleNamespace(id="inv-1", balance_amount=Decimal("50"), due_date=date(2026, 1, 1))
        inv2 = SimpleNamespace(id="inv-2", balance_amount=Decimal("80"), due_date=date(2026, 2, 1))
        db = AsyncMock()
        db.execute.return_value = Result(rows=[inv1, inv2])
        allocations, remaining = await payments.auto_allocate(
            db, "t", "e", "cust-1", Decimal("100"), "INR"
        )
        self.assertEqual(len(allocations), 2)
        self.assertEqual(allocations[0]["invoice"].id, "inv-1")
        self.assertEqual(allocations[0]["amount"], Decimal("50"))
        self.assertEqual(allocations[1]["amount"], Decimal("50"))
        self.assertEqual(remaining, Decimal("0"))

    async def test_fifo_leaves_later_invoices_untouched_once_paid_off(self):
        inv1 = SimpleNamespace(id="inv-1", balance_amount=Decimal("174000"), due_date=date(2026, 1, 1))
        inv2 = SimpleNamespace(id="inv-2", balance_amount=Decimal("80000"), due_date=date(2026, 2, 1))
        db = AsyncMock()
        db.execute.return_value = Result(rows=[inv1, inv2])
        allocations, remaining = await payments.auto_allocate(
            db, "t", "e", "cust-1", Decimal("100000"), "INR"
        )
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]["invoice"].id, "inv-1")
        self.assertEqual(remaining, Decimal("0"))


class ManualAllocateTests(IsolatedAsyncioTestCase):
    async def test_invoice_not_found_raises_business_rule(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(BusinessRuleException) as ctx:
            await payments.manual_allocate(
                db, "t", "e", "cust-1", Decimal("100"), "INR",
                [AllocationItem(invoice_id="missing", amount=Decimal("100"))],
            )
        self.assertEqual(ctx.exception.code, "INVOICE_NOT_FOUND")

    async def test_allocation_exceeding_balance_raises_business_rule(self):
        invoice = SimpleNamespace(id="inv-1", balance_amount=Decimal("50"))
        db = AsyncMock()
        db.execute.return_value = Result(invoice)
        with self.assertRaises(BusinessRuleException) as ctx:
            await payments.manual_allocate(
                db, "t", "e", "cust-1", Decimal("100"), "INR",
                [AllocationItem(invoice_id="inv-1", amount=Decimal("100"))],
            )
        self.assertEqual(ctx.exception.code, "ALLOCATION_EXCEEDS_BALANCE")

    async def test_valid_manual_allocation_across_two_invoices(self):
        inv1 = SimpleNamespace(id="inv-1", balance_amount=Decimal("100"))
        inv2 = SimpleNamespace(id="inv-2", balance_amount=Decimal("60"))
        db = AsyncMock()
        db.execute.side_effect = [Result(inv1), Result(inv2)]
        allocations, remaining = await payments.manual_allocate(
            db, "t", "e", "cust-1", Decimal("160"), "INR",
            [
                AllocationItem(invoice_id="inv-1", amount=Decimal("100")),
                AllocationItem(invoice_id="inv-2", amount=Decimal("60")),
            ],
        )
        self.assertEqual(len(allocations), 2)
        self.assertEqual(remaining, Decimal("0"))


class RetryablePaymentConflictTests(IsolatedAsyncioTestCase):
    async def test_recognizes_retryable_sqlstate(self):
        original = SimpleNamespace(sqlstate="40001")
        exc = type("FakeDBAPIError", (Exception,), {})()
        exc.orig = original
        self.assertTrue(payments.is_retryable_payment_conflict(exc))

    async def test_non_retryable_sqlstate_is_false(self):
        original = SimpleNamespace(sqlstate="23505")
        exc = type("FakeDBAPIError", (Exception,), {})()
        exc.orig = original
        self.assertFalse(payments.is_retryable_payment_conflict(exc))


if __name__ == "__main__":
    import unittest
    unittest.main()
