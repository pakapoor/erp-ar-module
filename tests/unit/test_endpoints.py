import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.responses import Response

from src.auth import CurrentUser, require_role
from src.exceptions import (
    BusinessRuleException,
    IdempotencyConflictException,
    PeriodClosedException,
    VersionConflictException,
)
from src.routers import aging, delivery, journal_entries


class Result:
    def __init__(self, value=None, rows=None, rowcount=1):
        self.value = value
        self.rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value
    def scalar(self): return self.value
    def fetchone(self): return self.value
    def fetchall(self): return self.rows
    def one(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


USER = CurrentUser(user_id="u", tenant_id="t", entity_id="e", roles=["cfo"])


class AgingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_missing(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException):
            await aging.get_customer_aging("c", None, USER, db)

    async def test_mv_response(self):
        customer = SimpleNamespace(id="c", name="Customer")
        row = SimpleNamespace(
            as_of=datetime(2026, 7, 20), current_amount=Decimal("10"), current_count=1,
            days_30_amount=0, days_30_count=0, days_60_amount=0, days_60_count=0,
            days_90_plus_amount=0, days_90_plus_count=0, total_outstanding=Decimal("10"),
        )
        db = AsyncMock()
        db.execute.side_effect = [Result(customer), Result("INR"), Result(row)]
        with patch.object(aging, "_get_bucket_invoice_ids", new=AsyncMock(return_value={"current": ["i"]})):
            response = await aging.get_customer_aging("c", None, USER, db)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"invoice_count":1', response.body)

    async def test_empty_mv_uses_live_fallback(self):
        customer = SimpleNamespace(id="c", name="Customer")
        db = AsyncMock()
        db.execute.side_effect = [Result(customer), Result("INR"), Result(None)]
        fallback = Response(status_code=200)
        with patch.object(aging, "_compute_aging_live", new=AsyncMock(return_value=fallback)) as live:
            self.assertIs(await aging.get_customer_aging("c", None, USER, db), fallback)
            live.assert_awaited_once()


class JournalEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoice_is_tenant_scoped(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException) as context:
            await journal_entries.get_journal_entries("i", 1, 20, USER, db)
        self.assertEqual(context.exception.status_code, 404)

    async def test_full_paginated_journal_response(self):
        invoice = SimpleNamespace(base_currency="INR")
        entry = SimpleNamespace(
            id="je", reference_type="INVOICE", reference_id="i",
            document_date=date(2026, 7, 20), entry_date=date(2026, 7, 20),
            description="approved", created_by="u", currency="INR",
        )
        summary = SimpleNamespace(
            total_debited_ar=Decimal("100"), total_credited_ar=Decimal("20"),
            base_total_debited_ar=Decimal("100"), base_total_credited_ar=Decimal("20"),
        )
        lines = [
            SimpleNamespace(account_code="1200", account_name="AR", debit_amount=Decimal("100"), credit_amount=Decimal("0"), base_debit_amount=Decimal("100"), base_credit_amount=Decimal("0")),
            SimpleNamespace(account_code="3100", account_name="Revenue", debit_amount=Decimal("0"), credit_amount=Decimal("100"), base_debit_amount=Decimal("0"), base_credit_amount=Decimal("100")),
        ]
        db = AsyncMock()
        db.execute.side_effect = [
            Result(invoice), Result(1), Result(rows=[entry]), Result(summary), Result(rows=lines),
        ]
        response = await journal_entries.get_journal_entries("i", 1, 20, USER, db)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"balanced":true', response.body)
        self.assertIn(b'"net_ar_balance":"80"', response.body)


class DeliveryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_delivery_missing_and_success(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException):
            await delivery.get_invoice_delivery("i", USER, db)
        event = SimpleNamespace(
            id="ev", event_type="TYPE", status="PUBLISHED", publish_attempt_count=1,
            attempt_count=0, sqs_message_id="m", last_error=None,
            created_at=datetime(2026, 7, 20), published_at=None, delivered_at=None,
        )
        db.execute.side_effect = [Result("i"), Result(rows=[event])]
        response = await delivery.get_invoice_delivery("i", USER, db)
        self.assertIn(b'"events":[', response.body)

    async def test_retry_states(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException):
            await delivery.retry_delivery_event("e", USER, db)

        delivered = SimpleNamespace(status="DELIVERED")
        db.execute.return_value = Result(delivered)
        with self.assertRaises(IdempotencyConflictException):
            await delivery.retry_delivery_event("e", USER, db)

        dead = SimpleNamespace(
            id="e", invoice_id="i", status="DEAD", attempt_count=3,
            publish_attempt_count=2, next_attempt_at=None, locked_at=datetime.now(),
            sqs_message_id="m", published_at=datetime.now(), delivered_at=None,
            last_error="failed", updated_at=None,
        )
        db.execute.return_value = Result(dead)
        response = await delivery.retry_delivery_event("e", USER, db)
        self.assertEqual(dead.status, "PENDING")
        db.commit.assert_awaited_once()
        self.assertEqual(response.status_code, 202)


class MainAndRBACUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rbac_success_and_denial(self):
        checker = require_role("cfo")
        self.assertIs(await checker(USER), USER)
        with self.assertRaises(HTTPException) as context:
            await checker(CurrentUser(user_id="u", tenant_id="t", entity_id="e", roles=[]))
        self.assertEqual(context.exception.status_code, 403)

    async def test_exception_handlers_and_trace_middleware(self):
        from src import main

        request = SimpleNamespace(state=SimpleNamespace(trace_id="trace-1"), headers={"X-Request-ID": "request-1"})
        cases = [
            (main.period_closed_handler, PeriodClosedException("closed"), 423),
            (main.idempotency_conflict_handler, IdempotencyConflictException("duplicate", "DUP"), 409),
            (main.business_rule_handler, BusinessRuleException("RULE", "bad"), 422),
            (main.version_conflict_handler, VersionConflictException("stale"), 409),
        ]
        for handler, error, status in cases:
            with self.subTest(status=status):
                self.assertEqual((await handler(request, error)).status_code, status)

        async def call_next(_request):
            return Response(status_code=204)

        response = await main.trace_id_middleware(request, call_next)
        self.assertEqual(response.headers["X-Trace-ID"], "request-1")


if __name__ == "__main__":
    unittest.main()
