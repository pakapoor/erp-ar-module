import unittest
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import delivery_publisher as publisher
from src import delivery_worker as worker
from src import fx_rate_worker as fx


class AsyncContext:
    def __init__(self, value=None): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *_args): return False


class Result:
    def __init__(self, value=None, rowcount=1):
        self.value = value
        self.rowcount = rowcount
    def scalar_one_or_none(self): return self.value
    def scalars(self): return self
    def __iter__(self): return iter(self.value or [])


def fake_session(*results):
    db = Mock()
    db.begin.return_value = AsyncContext()
    db.execute = AsyncMock(side_effect=list(results))
    db.flush = AsyncMock()
    return db, Mock(return_value=AsyncContext(db))


class PublisherPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_none_and_event(self):
        db, factory = fake_session(Result(None))
        with patch.object(publisher, "AsyncSessionLocal", factory):
            self.assertIsNone(await publisher.claim_event())

        event = SimpleNamespace(
            id="ev", tenant_id="t", entity_id="e", invoice_id="i",
            event_type="TYPE", payload={"id": "i"}, publish_attempt_count=0,
            max_publish_attempts=3, status="PENDING", locked_at=None, updated_at=None,
        )
        db, factory = fake_session(Result(event))
        with patch.object(publisher, "AsyncSessionLocal", factory):
            claimed = await publisher.claim_event()
        self.assertEqual(claimed["publish_attempt_count"], 1)
        self.assertEqual(event.status, "PROCESSING")
        db.flush.assert_awaited_once()

    async def test_mark_published_and_failure_paths(self):
        event = {"id": "ev", "publish_attempt_count": 1, "max_publish_attempts": 3}
        db, factory = fake_session(Result())
        with patch.object(publisher, "AsyncSessionLocal", factory):
            await publisher.mark_published(event, "message")
        db.execute.assert_awaited_once()

        for attempt, maximum in ((1, 3), (3, 3)):
            event = {"id": "ev", "publish_attempt_count": attempt, "max_publish_attempts": maximum}
            db, factory = fake_session(Result())
            with patch.object(publisher, "AsyncSessionLocal", factory):
                await publisher.mark_publish_failed(event, RuntimeError("x" * 3000))
            db.execute.assert_awaited_once()


class WorkerPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def envelope(self):
        return {"event_id": "ev", "tenant_id": "t", "entity_id": "e", "invoice_id": "i", "event_type": "TYPE"}

    async def test_claim_rejects_missing_fields_and_unknown_event(self):
        self.assertEqual(await worker.claim_delivery({}, 1), ("INVALID", None))
        db, factory = fake_session(Result(None))
        with patch.object(worker, "AsyncSessionLocal", factory):
            self.assertEqual(await worker.claim_delivery(self.envelope(), 1), ("INVALID", None))

    async def test_claim_terminal_busy_and_success_states(self):
        cases = [
            (SimpleNamespace(id="ev", status="DELIVERED"), "DELIVERED"),
            (SimpleNamespace(id="ev", status="DEAD"), "DEAD"),
            (SimpleNamespace(id="ev", status="DELIVERING", locked_at=datetime.max), "BUSY"),
        ]
        for event, expected in cases:
            db, factory = fake_session(Result(event))
            with patch.object(worker, "AsyncSessionLocal", factory):
                status, _ = await worker.claim_delivery(self.envelope(), 1)
            self.assertEqual(status, expected)

        exceeded = SimpleNamespace(
            id="ev", status="PUBLISHED", locked_at=None, attempt_count=4,
            max_attempts=3, last_error=None, updated_at=None,
        )
        db, factory = fake_session(Result(exceeded))
        with patch.object(worker, "AsyncSessionLocal", factory):
            status, _ = await worker.claim_delivery(self.envelope(), 5)
        self.assertEqual(status, "DEAD")

        event = SimpleNamespace(
            id="ev", tenant_id="t", entity_id="e", invoice_id="i", payload={},
            status="PUBLISHED", locked_at=None, attempt_count=0, max_attempts=3,
            updated_at=None,
        )
        db, factory = fake_session(Result(event))
        with patch.object(worker, "AsyncSessionLocal", factory):
            status, claimed = await worker.claim_delivery(self.envelope(), 2)
        self.assertEqual(status, "CLAIMED")
        self.assertEqual(claimed["attempt_count"], 2)

    async def test_mark_delivery_mutations(self):
        event = {"id": "ev", "invoice_id": "i", "tenant_id": "t", "entity_id": "e", "attempt_count": 1, "max_attempts": 3}
        db, factory = fake_session(Result(rowcount=1), Result())
        with patch.object(worker, "AsyncSessionLocal", factory):
            await worker.mark_delivered(event)
        self.assertEqual(db.execute.await_count, 2)

        db, factory = fake_session(Result(rowcount=0))
        with patch.object(worker, "AsyncSessionLocal", factory):
            await worker.mark_delivered(event)
        self.assertEqual(db.execute.await_count, 1)

        for attempt in (1, 3):
            event["attempt_count"] = attempt
            db, factory = fake_session(Result())
            with patch.object(worker, "AsyncSessionLocal", factory):
                await worker.mark_failed(event, RuntimeError("delivery failed"))
            db.execute.assert_awaited_once()


class FXPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_and_claim(self):
        db, factory = fake_session(Result())
        with patch.object(fx, "AsyncSessionLocal", factory):
            await fx.enqueue_import(date(2026, 7, 20))
        db.execute.assert_awaited_once()

        db, factory = fake_session(Result(None))
        with patch.object(fx, "AsyncSessionLocal", factory):
            self.assertIsNone(await fx.claim_job())

        job = SimpleNamespace(
            id="j", requested_date=date(2026, 7, 20), attempt_count=0,
            max_attempts=3, status="PENDING", locked_at=None, updated_at=None,
        )
        db, factory = fake_session(Result(job))
        with patch.object(fx, "AsyncSessionLocal", factory):
            claimed = await fx.claim_job()
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertEqual(job.status, "PROCESSING")

    async def test_store_rates_and_age_guards(self):
        job = {"id": "j", "requested_date": date(2026, 7, 20)}
        with self.assertRaises(ValueError):
            await fx.store_rates(job, "raw", date(2026, 7, 21), {})
        with self.assertRaises(ValueError):
            await fx.store_rates(job, "raw", date(2026, 7, 1), {})

        tenant_result = Result(["tenant-1"])
        db, factory = fake_session(tenant_result, Result(), Result())
        rates = {"USD": {"rate": Decimal("90"), "raw_quote_rate": Decimal("1.1"), "is_derived": True}}
        with (
            patch.object(fx, "AsyncSessionLocal", factory),
            patch.object(fx, "derive_base_rates", return_value=rates),
        ):
            count = await fx.store_rates(job, "raw", date(2026, 7, 20), {"INR": Decimal("90")})
        self.assertEqual(count, 1)
        self.assertEqual(db.execute.await_count, 3)

    async def test_mark_failed_retry_and_dead(self):
        for attempt in (1, 3):
            job = {"id": "j", "attempt_count": attempt, "max_attempts": 3}
            db, factory = fake_session(Result())
            with patch.object(fx, "AsyncSessionLocal", factory):
                await fx.mark_failed(job, RuntimeError("bad"))
            db.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
