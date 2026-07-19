import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.auth import CurrentUser
from src.exceptions import BusinessRuleException, IdempotencyConflictException
from src.routers import credit_memos, invoices


class Result:
    def __init__(self, value=None): self.value = value
    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value


USER = CurrentUser(user_id="u", tenant_id="t", entity_id="e", roles=["cfo"])


class IdempotencyHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_completed_processing_and_changed_payload(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        self.assertIsNone(await invoices.check_idempotency(db, "k", "t", "e", "endpoint", "hash"))

        completed = SimpleNamespace(request_hash="hash", status="COMPLETED")
        db.execute.return_value = Result(completed)
        self.assertIs(await invoices.check_idempotency(db, "k", "t", "e", "endpoint", "hash"), completed)

        processing = SimpleNamespace(request_hash="hash", status="PROCESSING")
        db.execute.return_value = Result(processing)
        with self.assertRaises(IdempotencyConflictException):
            await invoices.check_idempotency(db, "k", "t", "e", "endpoint", "hash")

        changed = SimpleNamespace(request_hash="other", status="COMPLETED")
        db.execute.return_value = Result(changed)
        with self.assertRaises(IdempotencyConflictException):
            await invoices.check_idempotency(db, "k", "t", "e", "endpoint", "hash")

    async def test_create_and_complete(self):
        db = Mock()
        db.add = Mock()
        db.flush = AsyncMock()
        await invoices.create_idempotency_key(db, "k", "t", "e", "endpoint", "hash")
        record = db.add.call_args.args[0]
        self.assertEqual(record.status, "PROCESSING")

        db.execute = AsyncMock(return_value=Result(record))
        await invoices.complete_idempotency_key(db, "k", "t", "e", "endpoint", 201, {"id": "x"})
        self.assertEqual(record.status, "COMPLETED")
        self.assertEqual(record.response_status, 201)


class CreditCommandGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_payload_guards(self):
        db = AsyncMock()
        invalid = [
            {"reason_code": "NOPE", "amount": 1},
            {"reason_code": "RETURN", "amount": 0},
            {"reason_code": "RETURN", "amount": 1, "include_tax": "yes"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(BusinessRuleException):
                await credit_memos.create_credit_memo("i", payload, "k", USER, db)

    async def test_valid_credit_missing_invoice_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(None)]
        with (
            patch.object(credit_memos, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as context,
        ):
            await credit_memos.create_credit_memo(
                "i", {"reason_code": "RETURN", "amount": 10, "include_tax": False},
                "k", USER, db,
            )
        self.assertEqual(context.exception.status_code, 404)

    async def test_writeoff_guards_and_missing_invoice(self):
        db = AsyncMock()
        with self.assertRaises(BusinessRuleException):
            await credit_memos.write_off_invoice("i", {"reason_code": "NOPE"}, "k", USER, db)
        db.execute.side_effect = [Result(), Result(None)]
        with (
            patch.object(credit_memos, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException),
        ):
            await credit_memos.write_off_invoice("i", {"reason_code": "BAD_DEBT"}, "k", USER, db)

    async def test_void_guards_and_missing_invoice(self):
        db = AsyncMock()
        db.execute.return_value = Result()
        with self.assertRaises(BusinessRuleException):
            await credit_memos.void_invoice("i", {"reason_code": "NOPE"}, "k", USER, db)
        db.execute.side_effect = [Result(), Result(), Result(None)]
        with (
            patch.object(credit_memos, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException),
        ):
            await credit_memos.void_invoice("i", {"reason_code": "DATA_ERROR"}, "k", USER, db)

    async def test_cached_commands_return_without_mutation(self):
        cached = SimpleNamespace(response_status=201, response_body={"id": "cached"})
        db = AsyncMock()
        db.execute.return_value = Result()
        with patch.object(credit_memos, "check_idempotency", new=AsyncMock(return_value=cached)):
            credit = await credit_memos.create_credit_memo(
                "i", {"reason_code": "RETURN", "amount": 10, "include_tax": False}, "k", USER, db
            )
            writeoff = await credit_memos.write_off_invoice(
                "i", {"reason_code": "BAD_DEBT"}, "k", USER, db
            )
            voided = await credit_memos.void_invoice(
                "i", {"reason_code": "DATA_ERROR"}, "k", USER, db
            )
        self.assertEqual((credit.status_code, writeoff.status_code, voided.status_code), (201, 201, 201))


if __name__ == "__main__":
    unittest.main()
