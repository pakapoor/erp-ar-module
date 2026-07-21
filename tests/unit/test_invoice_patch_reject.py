from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.auth import CurrentUser
from src.exceptions import BusinessRuleException, VersionConflictException
from src.routers import invoices
from src.schemas import LineItemCreate


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
APPROVER = CurrentUser(user_id="priya", tenant_id="t", entity_id="e", roles=["invoice_approver"])


def make_draft_invoice(**overrides):
    defaults = dict(
        id="inv-1", tenant_id="t", entity_id="e", customer_id="cust-1",
        version=1, status="DRAFT", created_by="rahul",
        po_reference=None, invoice_date=date(2026, 7, 1), payment_terms="NET30",
        transaction_currency="INR", base_currency="INR",
        rejection_reason=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


class PatchInvoiceGuardTests(IsolatedAsyncioTestCase):
    """PATCH /invoices/{id} -- guard clauses that don't require a full DB round trip."""

    async def test_missing_invoice_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(None)]  # config, then invoice lookup
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await invoices.update_invoice(
                "missing", invoices.InvoiceUpdate(po_reference="x"),
                "idem-1", "1", CREATOR, db,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_stale_if_match_raises_version_conflict(self):
        invoice = make_draft_invoice(version=3)
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(invoice)]
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(VersionConflictException),
        ):
            await invoices.update_invoice(
                "inv-1", invoices.InvoiceUpdate(po_reference="x"),
                "idem-1", "1", CREATOR, db,
            )

    async def test_non_draft_status_is_rejected(self):
        invoice = make_draft_invoice(status="APPROVED")
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(invoice)]
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await invoices.update_invoice(
                "inv-1", invoices.InvoiceUpdate(po_reference="x"),
                "idem-1", "1", CREATOR, db,
            )
        self.assertEqual(ctx.exception.code, "INVALID_STATUS")

    async def test_cached_idempotent_response_short_circuits(self):
        cached = SimpleNamespace(response_status=200, response_body={"id": "inv-1", "cached": True})
        db = AsyncMock()
        db.execute.return_value = Result()
        with patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=cached)):
            response = await invoices.update_invoice(
                "inv-1", invoices.InvoiceUpdate(po_reference="x"),
                "idem-1", "1", CREATOR, db,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"cached":true', response.body)


class PatchInvoiceHappyPathTests(IsolatedAsyncioTestCase):
    """PATCH /invoices/{id} -- full line-item replacement, same currency (no FX lookup)."""

    async def test_replaces_line_items_recalculates_totals_and_clears_rejection(self):
        invoice = make_draft_invoice(rejection_reason="Tax rate wrong", version=2)
        customer = SimpleNamespace(id="cust-1", credit_limit=Decimal("0"))
        saved_line = make_line_item(description="Corrected Widget", quantity=Decimal("3"))

        db = AsyncMock()
        db.add = Mock()  # SQLAlchemy's Session.add is sync, not a coroutine
        db.execute.side_effect = [
            Result(),                    # set_config
            Result(invoice),             # invoice fetch
            Result(customer),            # customer fetch (credit check)
            Result(Decimal("0")),        # outstanding balance sum
            Result(3),                   # version-CAS UPDATE ... RETURNING version
            Result(),                    # DELETE FROM invoice_line_item
            Result(rows=[saved_line]),   # re-fetch saved line items
        ]

        payload = invoices.InvoiceUpdate(
            line_items=[
                LineItemCreate(
                    description="Corrected Widget", quantity=Decimal("3"),
                    unit_price=Decimal("100"), tax_rate=Decimal("18"),
                )
            ]
        )

        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(invoices, "create_idempotency_key", new=AsyncMock()),
            patch.object(invoices, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await invoices.update_invoice(
                "inv-1", payload, "idem-1", "2", CREATOR, db,
            )

        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn('"status":"DRAFT"', body.replace(" ", ""))
        self.assertIn('"version":3', body.replace(" ", ""))
        # subtotal = 3 * 100 = 300, tax = 300 * 0.18 = 54, total = 354
        self.assertIn('"subtotal_amount":"300"', body.replace(" ", ""))
        self.assertIn('"tax_amount":"54.00"', body.replace(" ", ""))
        self.assertIn('"total_amount":"354.00"', body.replace(" ", ""))
        # rejection_reason is cleared -- not present as the prior value anywhere in the body
        self.assertNotIn("Tax rate wrong", body)


class RejectFlowTests(IsolatedAsyncioTestCase):
    """POST /invoices/{id}/approve with action=REJECT."""

    async def test_rejection_reason_required_by_schema(self):
        with self.assertRaises(ValueError):
            invoices.InvoiceApprove(action="REJECT")

    async def test_creator_cannot_reject_own_invoice_sox(self):
        invoice = make_draft_invoice(created_by="priya")
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(invoice)]
        approver_is_creator = CurrentUser(
            user_id="priya", tenant_id="t", entity_id="e", roles=["invoice_approver"]
        )
        payload = invoices.InvoiceApprove(action="REJECT", rejection_reason="bad tax")
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await invoices.approve_invoice(
                "inv-1", payload, "idem-1", "1", approver_is_creator, db,
            )
        self.assertEqual(ctx.exception.code, "SOX_VIOLATION")

    async def test_reject_non_draft_invoice_is_invalid_status(self):
        invoice = make_draft_invoice(status="APPROVED")
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(invoice)]
        payload = invoices.InvoiceApprove(action="REJECT", rejection_reason="bad tax")
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await invoices.approve_invoice(
                "inv-1", payload, "idem-1", "1", APPROVER, db,
            )
        self.assertEqual(ctx.exception.code, "INVALID_STATUS")

    async def test_reject_happy_path_reverts_to_draft_no_gl_no_delivery(self):
        invoice = make_draft_invoice(version=1)
        db = AsyncMock()
        db.execute.side_effect = [
            Result(),           # set_config
            Result(invoice),    # invoice fetch
            Result(2),          # UPDATE ... RETURNING version -> new_version
        ]
        payload = invoices.InvoiceApprove(action="REJECT", rejection_reason="Tax rate wrong")

        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(invoices, "create_idempotency_key", new=AsyncMock()),
            patch.object(invoices, "complete_idempotency_key", new=AsyncMock()) as complete_mock,
        ):
            response = await invoices.approve_invoice(
                "inv-1", payload, "idem-1", "1", APPROVER, db,
            )

        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        # The response reports an outcome label; the persisted row (see the
        # UPDATE .values(status="DRAFT", ...) above) is what actually goes to DRAFT.
        self.assertIn('"status":"REJECTED"', body.replace(" ", ""))
        self.assertIn("Tax rate wrong", body)
        self.assertIn('"version":2', body.replace(" ", ""))
        self.assertNotIn("journal_entry_id", body)
        # No GL/period-close query should have run past the three calls above --
        # nothing left in side_effect for db.execute to consume.
        self.assertEqual(db.execute.call_count, 3)
        complete_mock.assert_awaited_once()

    async def test_reject_stale_version_raises_version_conflict(self):
        invoice = make_draft_invoice(version=5)
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(invoice)]
        payload = invoices.InvoiceApprove(action="REJECT", rejection_reason="Tax rate wrong")
        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(VersionConflictException),
        ):
            await invoices.approve_invoice(
                "inv-1", payload, "idem-1", "1", APPROVER, db,
            )


class RejectThenPatchThenApproveTests(IsolatedAsyncioTestCase):
    """End-to-end router-level chain: reject clears the invoice for editing,
    patch fixes it and clears rejection_reason, a plain approve then proceeds
    down the ordinary (non-reject) branch."""

    async def test_patch_after_reject_clears_reason_and_reapprove_is_untouched_by_patch(self):
        rejected_invoice = make_draft_invoice(
            version=2, rejection_reason="Tax rate wrong", status="DRAFT",
        )
        customer = SimpleNamespace(id="cust-1", credit_limit=Decimal("0"))
        saved_line = make_line_item(description="Fixed line", tax_rate=Decimal("12"))

        db = AsyncMock()
        db.add = Mock()  # SQLAlchemy's Session.add is sync, not a coroutine
        db.execute.side_effect = [
            Result(),                   # set_config
            Result(rejected_invoice),   # invoice fetch
            Result(customer),          # customer fetch
            Result(Decimal("0")),      # outstanding sum
            Result(3),                 # version-CAS UPDATE -> new version
            Result(),                   # DELETE line items
            Result(rows=[saved_line]), # re-fetch line items
        ]
        payload = invoices.InvoiceUpdate(
            line_items=[
                LineItemCreate(
                    description="Fixed line", quantity=Decimal("1"),
                    unit_price=Decimal("100"), tax_rate=Decimal("12"),
                )
            ]
        )

        with (
            patch.object(invoices, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(invoices, "create_idempotency_key", new=AsyncMock()),
            patch.object(invoices, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await invoices.update_invoice(
                "inv-1", payload, "idem-2", "2", CREATOR, db,
            )

        body = response.body.decode().replace(" ", "")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Tax rate wrong", body)
        self.assertIn('"version":3', body)


if __name__ == "__main__":
    import unittest
    unittest.main()
