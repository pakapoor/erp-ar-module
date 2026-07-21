import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from src.auth import CurrentUser
from src.exceptions import BusinessRuleException, IdempotencyConflictException
from src.routers import aging, delivery, health, invoices, payments
from src.schemas import AllocationItem, InvoiceCreate, InvoiceUpdate, LineItemCreate, PaymentCreate


class Result:
    def __init__(self, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar

    def one(self):
        return self.scalar

    def scalar(self):
        return self.scalar

    def fetchone(self):
        return self.scalar

    def fetchall(self):
        return self.rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class InvoiceHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_financial_math(self):
        line = LineItemCreate(description="item", quantity=Decimal("2"), unit_price=Decimal("10"), tax_rate=Decimal("18"))
        self.assertEqual(invoices.calculate_line_totals(line), (Decimal("20"), Decimal("3.6"), Decimal("23.6")))
        self.assertEqual(invoices.calculate_due_date(date(2026, 1, 1), "NET30"), date(2026, 1, 31))
        self.assertEqual(invoices.calculate_due_date(date(2026, 1, 1), "UNKNOWN"), date(2026, 1, 31))
        self.assertEqual(invoices.convert_to_base(Decimal("1.23456"), Decimal("2")), Decimal("2.4691"))

    async def test_same_currency_invoice_rate(self):
        result = await invoices.resolve_invoice_exchange_rate(Mock(), "t", "INR", "INR", date.today())
        self.assertEqual(result, (None, Decimal("1")))

    async def test_missing_and_prior_invoice_rate(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException) as context:
            await invoices.resolve_invoice_exchange_rate(db, "t", "USD", "INR", date(2026, 7, 20))
        self.assertEqual(context.exception.status_code, 503)

        rate = SimpleNamespace(id="r", rate=Decimal("90"), effective_date=date(2026, 7, 19))
        db.execute.return_value = Result(rate)
        result = await invoices.resolve_invoice_exchange_rate(db, "t", "USD", "INR", date(2026, 7, 20))
        self.assertEqual(result, ("r", Decimal("90")))


class PaymentHelperTests(unittest.IsolatedAsyncioTestCase):
    def db_error(self, sqlstate):
        original = RuntimeError("db")
        original.sqlstate = sqlstate
        return DBAPIError("statement", {}, original, False)

    def test_retryable_conflicts(self):
        retryable = self.db_error("40001")
        self.assertTrue(payments.is_retryable_payment_conflict(retryable))
        with self.assertRaises(IdempotencyConflictException):
            payments.raise_payment_conflict(retryable)
        ordinary = self.db_error("23505")
        self.assertFalse(payments.is_retryable_payment_conflict(ordinary))
        with self.assertRaises(DBAPIError):
            payments.raise_payment_conflict(ordinary)

    async def test_exchange_rate_paths(self):
        self.assertEqual(
            await payments.get_exchange_rate(Mock(), "t", "INR", "INR", date(2026, 7, 20)),
            (None, Decimal("1"), "2026-07-20", None),
        )
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException):
            await payments.get_exchange_rate(db, "t", "USD", "INR", date(2026, 7, 20))
        rate = SimpleNamespace(id="r", rate=Decimal("91"), effective_date=date(2026, 7, 19))
        db.execute.return_value = Result(rate)
        value = await payments.get_exchange_rate(db, "t", "USD", "INR", date(2026, 7, 20))
        self.assertIn("not available", value[3])

    async def test_auto_allocate_fifo_and_empty(self):
        db = AsyncMock()
        one = SimpleNamespace(balance_amount=Decimal("30"))
        two = SimpleNamespace(balance_amount=Decimal("50"))
        db.execute.return_value = Result(rows=[one, two])
        allocations, remainder = await payments.auto_allocate(db, "t", "e", "c", Decimal("60"), "INR")
        self.assertEqual([a["amount"] for a in allocations], [Decimal("30"), Decimal("30")])
        self.assertEqual(remainder, Decimal("0"))
        db.execute.return_value = Result(rows=[])
        with self.assertRaises(BusinessRuleException):
            await payments.auto_allocate(db, "t", "e", "c", Decimal("10"), "INR")

    async def test_manual_allocate_validation(self):
        db = AsyncMock()
        invoice = SimpleNamespace(balance_amount=Decimal("50"))
        db.execute.return_value = Result(invoice)
        items = [AllocationItem(invoice_id="i", amount=Decimal("20"))]
        allocations, remainder = await payments.manual_allocate(db, "t", "e", "c", Decimal("30"), "INR", items)
        self.assertEqual(allocations[0]["amount"], Decimal("20"))
        self.assertEqual(remainder, Decimal("10"))
        db.execute.return_value = Result(None)
        with self.assertRaises(BusinessRuleException):
            await payments.manual_allocate(db, "t", "e", "c", Decimal("30"), "INR", items)
        db.execute.return_value = Result(invoice)
        too_much = [AllocationItem(invoice_id="i", amount=Decimal("60"))]
        with self.assertRaises(BusinessRuleException):
            await payments.manual_allocate(db, "t", "e", "c", Decimal("60"), "INR", too_much)


class ReportingAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_aging_builder(self):
        db = AsyncMock()
        db.execute.return_value = Result(rows=[
            SimpleNamespace(id="i1", bucket="current", base_balance_amount=Decimal("10")),
            SimpleNamespace(id="i2", bucket="days_30", base_balance_amount=Decimal("20")),
        ])
        response = await aging._compute_aging_live(db, SimpleNamespace(id="c", name="Customer"), "e", "t", "INR")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"total_outstanding":"30"', response.body)

    async def test_bucket_ids(self):
        db = AsyncMock()
        db.execute.return_value = Result(rows=[SimpleNamespace(id="i", bucket="days_60")])
        buckets = await aging._get_bucket_invoice_ids(db, "t", "e", "c")
        self.assertEqual(buckets["days_60"], ["i"])

    async def test_aging_rejects_entity_override(self):
        user = CurrentUser(user_id="u", tenant_id="t", entity_id="e", roles=[])
        with self.assertRaises(HTTPException) as context:
            await aging.get_customer_aging("c", "other", user, AsyncMock())
        self.assertEqual(context.exception.status_code, 404)

    def test_delivery_serialization(self):
        now = datetime(2026, 7, 20)
        event = SimpleNamespace(
            id="e", event_type="TYPE", status="DELIVERED", publish_attempt_count=1,
            attempt_count=2, sqs_message_id="m", last_error=None, created_at=now,
            published_at=now, delivered_at=None,
        )
        serialized = delivery.serialize_event(event)
        self.assertEqual(serialized["delivery_attempt_count"], 2)
        self.assertIsNone(serialized["delivered_at"])
        self.assertIsNone(delivery.utc_now().tzinfo)

    async def test_health_happy_and_mismatch(self):
        fresh = SimpleNamespace(age_minutes=1)
        matched = SimpleNamespace(max_entity_diff=Decimal("0"), mismatched_entities=0, subledger_balance=0, gl_balance=0)
        db = AsyncMock()
        db.execute.side_effect = [Result(1), Result(fresh), Result(matched)]
        response = await health.health_check(db)
        self.assertEqual(response.status_code, 200)
        mismatch = SimpleNamespace(age_minutes=20)
        bad = SimpleNamespace(max_entity_diff=Decimal("10"), mismatched_entities=1, subledger_balance=10, gl_balance=0)
        db.execute.side_effect = [Result(1), Result(mismatch), Result(bad)]
        response = await health.health_check(db)
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'MISMATCH', response.body)


class SchemaNegativeTests(unittest.TestCase):
    def test_invoice_schema_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            InvoiceCreate(customer_id="c", currency="BTC", line_items=[])
        with self.assertRaises(ValueError):
            LineItemCreate(description="", quantity=0, unit_price=-1, tax_rate=101)

    def test_payment_schema_rules(self):
        valid = PaymentCreate(
            customer_id="c", payment_reference="p", amount=Decimal("10"),
            currency="usd", payment_method="SWIFT", allocation_mode="AUTO",
        )
        self.assertEqual(valid.currency, "USD")
        with self.assertRaises(ValueError):
            PaymentCreate(
                customer_id="c", payment_reference="p", amount=Decimal("10"),
                currency="USD", payment_method="CASH", allocation_mode="AUTO",
            )

    def test_manual_allocation_requires_allocations(self):
        with self.assertRaisesRegex(ValueError, "allocations required when allocation_mode is MANUAL"):
            PaymentCreate(
                customer_id="c", payment_reference="p", amount=Decimal("10"),
                currency="USD", payment_method="SWIFT", allocation_mode="MANUAL",
            )

    def test_auto_allocation_forbids_allocations(self):
        with self.assertRaisesRegex(ValueError, "allocations must be null when allocation_mode is AUTO"):
            PaymentCreate(
                customer_id="c", payment_reference="p", amount=Decimal("10"),
                currency="USD", payment_method="SWIFT", allocation_mode="AUTO",
                allocations=[AllocationItem(invoice_id="i", amount=Decimal("10"))],
            )

    def test_manual_allocations_cannot_exceed_payment_amount(self):
        with self.assertRaisesRegex(ValueError, "sum of allocations cannot exceed payment amount"):
            PaymentCreate(
                customer_id="c", payment_reference="p", amount=Decimal("10"),
                currency="USD", payment_method="SWIFT", allocation_mode="MANUAL",
                allocations=[AllocationItem(invoice_id="i", amount=Decimal("15"))],
            )

    def test_invoice_update_currency_none_is_left_unset(self):
        update = InvoiceUpdate(po_reference="new-po")
        self.assertIsNone(update.currency)


if __name__ == "__main__":
    unittest.main()
