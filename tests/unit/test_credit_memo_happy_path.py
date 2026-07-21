from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from src.auth import CurrentUser
from src.models import CreditMemo, JournalEntry
from src.routers import credit_memos


class Result:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


APPROVER = CurrentUser(user_id="priya", tenant_id="t", entity_id="e", roles=["invoice_approver"])


def make_invoice(**overrides):
    defaults = dict(
        id="inv-1", created_by="rahul", status="SENT",
        transaction_currency="INR", exchange_rate=Decimal("1"),
        subtotal_amount=Decimal("100"), tax_amount=Decimal("18"),
        balance_amount=Decimal("118"), base_balance_amount=Decimal("118"),
        version=2, updated_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_gl_account(code):
    return SimpleNamespace(id=f"gl-{code}", account_code=code)


class CreateCreditMemoHappyPathTests(IsolatedAsyncioTestCase):
    async def test_full_credit_reverses_revenue_tax_and_clears_ar(self):
        invoice = make_invoice()
        period = SimpleNamespace(id="period-1")
        gl_accounts = [make_gl_account(c) for c in ("3100", "2200", "1200", "2100")]

        added_objects = []

        def track_add(obj):
            added_objects.append(obj)
            if isinstance(obj, CreditMemo) and obj.id is None:
                obj.id = "cm-1"
            if isinstance(obj, JournalEntry) and obj.id is None:
                obj.id = "je-1"

        db = AsyncMock()
        db.add = Mock(side_effect=track_add)
        db.execute.side_effect = [
            Result(),                     # set_audit_context
            Result(invoice),              # invoice fetch (with_for_update)
            Result(rows=[]),              # prior reversal aggregate (none yet)
            Result(period),               # open period lookup
            Result(rows=gl_accounts),     # GL accounts lookup
        ]

        payload = {
            "reason_code": "RETURN",
            "description": "Damaged goods",
            "amount": 118,
            "include_tax": True,
        }

        with (
            patch.object(credit_memos, "check_idempotency", new=AsyncMock(side_effect=[None, None])),
            patch.object(credit_memos, "create_idempotency_key", new=AsyncMock()),
            patch.object(credit_memos, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await credit_memos.create_credit_memo(
                "inv-1", payload, "idem-1", APPROVER, db,
            )

        self.assertEqual(response.status_code, 201)
        body = response.body.decode().replace(" ", "")
        self.assertIn('"invoice_status":"PAID"', body)
        self.assertIn('"invoice_balance_after":"0"', body)
        self.assertIn('"ar_reduction":"118"', body)
        self.assertIn('"customer_credit_amount":"0"', body)

        cm = next(o for o in added_objects if isinstance(o, CreditMemo))
        self.assertEqual(cm.status, "APPLIED")
        self.assertEqual(cm.created_by, "rahul")
        self.assertEqual(cm.approved_by, "priya")

    async def test_credit_beyond_balance_creates_customer_credit_liability(self):
        invoice = make_invoice(balance_amount=Decimal("50"), base_balance_amount=Decimal("50"))
        period = SimpleNamespace(id="period-1")
        gl_accounts = [make_gl_account(c) for c in ("3100", "2200", "1200", "2100")]

        db = AsyncMock()
        db.add = Mock(side_effect=lambda obj: setattr(obj, "id", obj.id or "generated-id"))
        db.execute.side_effect = [
            Result(),
            Result(invoice),
            Result(rows=[]),
            Result(period),
            Result(rows=gl_accounts),
        ]

        payload = {"reason_code": "OVERCHARGE", "amount": 118, "include_tax": True}

        with (
            patch.object(credit_memos, "check_idempotency", new=AsyncMock(side_effect=[None, None])),
            patch.object(credit_memos, "create_idempotency_key", new=AsyncMock()),
            patch.object(credit_memos, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await credit_memos.create_credit_memo(
                "inv-1", payload, "idem-1", APPROVER, db,
            )

        body = response.body.decode().replace(" ", "")
        self.assertIn('"ar_reduction":"50"', body)
        self.assertIn('"customer_credit_amount":"68"', body)
        self.assertIn('"invoice_status":"PAID"', body)


if __name__ == "__main__":
    import unittest
    unittest.main()
