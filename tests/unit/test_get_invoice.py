from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from fastapi import HTTPException

from src.auth import CurrentUser
from src.routers import invoices


class Result:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


USER = CurrentUser(user_id="rahul", tenant_id="t", entity_id="e", roles=["invoice_creator"])


def make_line_item(**overrides):
    defaults = dict(
        id="li-1", line_number=1, description="Widget",
        quantity=Decimal("2"), unit_price=Decimal("100"),
        subtotal=Decimal("200"), tax_rate=Decimal("18"),
        tax_jurisdiction=None, tax_amount=Decimal("36"),
        total_price=Decimal("236"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_invoice(**overrides):
    defaults = dict(
        id="inv-1", customer_id="cust-1", status="APPROVED", version=2,
        po_reference="PO-1", invoice_date=date(2026, 7, 1), due_date=date(2026, 7, 31),
        payment_terms="NET30", transaction_currency="INR", base_currency="INR",
        exchange_rate_id=None, exchange_rate=Decimal("1"),
        subtotal_amount=Decimal("200"), tax_amount=Decimal("36"),
        total_amount=Decimal("236"), balance_amount=Decimal("236"),
        base_subtotal_amount=Decimal("200"), base_tax_amount=Decimal("36"),
        base_total_amount=Decimal("236"), base_balance_amount=Decimal("236"),
        line_items=[make_line_item()],
        created_by="rahul", approved_by="priya",
        created_at=datetime(2026, 7, 1, 10, 0, 0),
        approved_at=datetime(2026, 7, 1, 11, 0, 0),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class GetInvoiceTests(IsolatedAsyncioTestCase):
    async def test_missing_invoice_is_404(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException) as ctx:
            await invoices.get_invoice("missing", USER, db)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_full_response_includes_all_histories(self):
        invoice = make_invoice()
        customer = SimpleNamespace(id="cust-1", name="Acme Corp")
        payment = SimpleNamespace(
            payment_reference="PAY-1", payment_date=date(2026, 7, 10),
            payment_method="NEFT", transaction_currency="INR",
        )
        alloc = SimpleNamespace(id="alloc-1", amount_allocated=Decimal("100"))
        credit_memo = SimpleNamespace(
            id="cm-1", reason_code="RETURN", amount=Decimal("10"),
            applied_at=datetime(2026, 7, 12),
        )
        audit_log = SimpleNamespace(
            old_value={"status": "DRAFT"}, new_value={"status": "APPROVED"},
            changed_by="priya", changed_at=datetime(2026, 7, 5),
        )

        db = AsyncMock()
        db.execute.side_effect = [
            Result(invoice),                       # invoice fetch
            Result(customer),                      # customer fetch
            Result(rows=[(alloc, payment)]),        # payment history
            Result(rows=[credit_memo]),            # credit memo history
            Result(rows=[audit_log]),              # audit/status history
        ]

        response = await invoices.get_invoice("inv-1", USER, db)
        self.assertEqual(response.status_code, 200)
        body = response.body.decode().replace(" ", "")
        self.assertIn('"status":"APPROVED"', body)
        self.assertIn('"payment_reference":"PAY-1"', body)
        self.assertIn('"reason_code":"RETURN"', body)
        self.assertIn('"changed_by":"priya"', body)
        self.assertIn('"customer":{"id":"cust-1","name":"AcmeCorp"}', body)

    async def test_status_history_skips_entries_with_no_status_change(self):
        invoice = make_invoice()
        customer = SimpleNamespace(id="cust-1", name="Acme Corp")
        unrelated_log = SimpleNamespace(
            old_value={"po_reference": "OLD"}, new_value={"po_reference": "NEW"},
            changed_by="rahul", changed_at=datetime(2026, 7, 3),
        )

        db = AsyncMock()
        db.execute.side_effect = [
            Result(invoice),
            Result(customer),
            Result(rows=[]),
            Result(rows=[]),
            Result(rows=[unrelated_log]),
        ]

        response = await invoices.get_invoice("inv-1", USER, db)
        body = response.body.decode().replace(" ", "")
        self.assertIn('"status_history":[]', body)

    async def test_etag_header_reflects_version(self):
        invoice = make_invoice(version=5)
        customer = SimpleNamespace(id="cust-1", name="Acme Corp")
        db = AsyncMock()
        db.execute.side_effect = [
            Result(invoice), Result(customer), Result(rows=[]), Result(rows=[]), Result(rows=[]),
        ]
        response = await invoices.get_invoice("inv-1", USER, db)
        self.assertEqual(response.headers.get("ETag"), "5")


if __name__ == "__main__":
    import unittest
    unittest.main()
