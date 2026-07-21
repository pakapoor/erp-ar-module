from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.auth import CurrentUser
from src.exceptions import BusinessRuleException
from src.models import Invoice
from src.routers import invoices
from src.schemas import InvoiceCreate, LineItemCreate


class Result:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value
    def scalar(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


CREATOR = CurrentUser(user_id="rahul", tenant_id="t", entity_id="e", roles=["invoice_creator"])


def make_payload(**overrides):
    defaults = dict(
        customer_id="cust-1",
        po_reference="PO-1",
        invoice_date=date(2026, 7, 1),
        payment_terms="NET30",
        currency="INR",
        line_items=[
            LineItemCreate(description="Widget", quantity=Decimal("2"), unit_price=Decimal("100"), tax_rate=Decimal("18")),
        ],
    )
    defaults.update(overrides)
    return InvoiceCreate(**defaults)


def make_saved_line_item(**overrides):
    defaults = dict(
        id="li-1", line_number=1, description="Widget",
        quantity=Decimal("2"), unit_price=Decimal("100"),
        subtotal=Decimal("200"), tax_rate=Decimal("18"),
        tax_jurisdiction=None, tax_amount=Decimal("36"),
        total_price=Decimal("236"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class CreateInvoiceGuardTests(IsolatedAsyncioTestCase):
    async def test_missing_customer_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(None)]  # set_config, customer lookup
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await invoices.create_invoice(make_payload(), "idem-1", CREATOR, db)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_cached_idempotent_response_short_circuits(self):
        cached = SimpleNamespace(response_status=201, response_body={"id": "cached-inv"})
        db = AsyncMock()
        db.execute.return_value = Result()
        with patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=cached)):
            response = await invoices.create_invoice(make_payload(), "idem-1", CREATOR, db)
        self.assertEqual(response.status_code, 201)
        self.assertIn(b"cached-inv", response.body)

    async def test_credit_limit_exceeded_raises_business_rule(self):
        customer = SimpleNamespace(id="cust-1", name="Acme", credit_limit=Decimal("100"))
        db = AsyncMock()
        db.add = Mock()
        db.execute.side_effect = [
            Result(),                    # set_config
            Result(customer),            # customer fetch
            Result("INR"),               # entity currency
            Result(Decimal("50")),       # outstanding balance sum
        ]
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await invoices.create_invoice(make_payload(), "idem-1", CREATOR, db)
        self.assertEqual(ctx.exception.code, "CREDIT_LIMIT_EXCEEDED")


class CreateInvoiceHappyPathTests(IsolatedAsyncioTestCase):
    async def test_creates_draft_invoice_with_server_calculated_totals(self):
        customer = SimpleNamespace(id="cust-1", name="Acme", credit_limit=Decimal("0"))
        saved_line = make_saved_line_item()

        def stamp_created_at(obj):
            if isinstance(obj, Invoice) and obj.created_at is None:
                obj.created_at = datetime(2026, 7, 1, 12, 0, 0)

        db = AsyncMock()
        db.add = Mock(side_effect=stamp_created_at)
        db.execute.side_effect = [
            Result(),                    # set_config
            Result(customer),            # customer fetch
            Result("INR"),               # entity currency (same as invoice currency -> no FX lookup)
            Result(Decimal("0")),        # outstanding balance sum
            Result(rows=[saved_line]),   # re-fetch saved line items
        ]

        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(invoices, "create_idempotency_key", new=AsyncMock()),
            patch.object(invoices, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await invoices.create_invoice(make_payload(), "idem-1", CREATOR, db)

        self.assertEqual(response.status_code, 201)
        body = response.body.decode().replace(" ", "")
        self.assertIn('"status":"DRAFT"', body)
        self.assertIn('"version":1', body)
        # subtotal = 2 * 100 = 200, tax = 200 * 0.18 = 36, total = 236
        self.assertIn('"subtotal_amount":"200"', body)
        self.assertIn('"tax_amount":"36.00"', body)
        self.assertIn('"total_amount":"236.00"', body)
        self.assertIn('"due_date":"2026-07-31"', body)  # NET30 from 2026-07-01


if __name__ == "__main__":
    import unittest
    unittest.main()
