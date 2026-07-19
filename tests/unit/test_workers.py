import asyncio
import json
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from src import delivery_publisher as publisher
from src import delivery_worker as worker
from src import fx_rate_worker as fx


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_queue_url(self):
        client = Mock()
        client.get_queue_url.return_value = {"QueueUrl": "queue-url"}
        self.assertEqual(await publisher.resolve_queue_url(client), "queue-url")

    async def test_resolve_queue_url_retries(self):
        client = Mock()
        client.get_queue_url.side_effect = [RuntimeError("not ready"), {"QueueUrl": "q"}]
        with patch.object(publisher.asyncio, "sleep", new=AsyncMock()) as sleep:
            self.assertEqual(await publisher.resolve_queue_url(client), "q")
            sleep.assert_awaited_once()

    async def test_publish_event_builds_stable_envelope(self):
        event = {
            "id": "event-1", "event_type": "INVOICE_APPROVED",
            "tenant_id": "tenant-1", "entity_id": "entity-1",
            "invoice_id": "invoice-1", "payload": {"amount": "10"},
        }
        client = Mock()
        client.send_message.return_value = {"MessageId": "message-1"}
        with patch.object(publisher, "mark_published", new=AsyncMock()) as marked:
            await publisher.publish_event(client, "queue-url", event)
        body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(body["event_id"], "event-1")
        self.assertEqual(body["payload"], {"amount": "10"})
        marked.assert_awaited_once_with(event, "message-1")

    def test_client_and_clock_helpers(self):
        self.assertIsNone(publisher.utc_now().tzinfo)
        with patch.object(publisher.boto3, "client", return_value="client") as factory:
            self.assertEqual(publisher.create_sqs_client(), "client")
            factory.assert_called_once()


class DeliveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    def message(self, body=None):
        return {
            "ReceiptHandle": "receipt-1",
            "MessageId": "message-1",
            "Body": body if body is not None else json.dumps({"event_id": "event-1"}),
            "Attributes": {"ApproximateReceiveCount": "2"},
        }

    async def test_invalid_json_is_left_for_redrive(self):
        with patch.object(worker, "claim_delivery", new=AsyncMock()) as claim:
            await worker.process_message(Mock(), "q", AsyncMock(), self.message("{"))
            claim.assert_not_awaited()

    async def test_delivered_duplicate_is_deleted(self):
        with (
            patch.object(worker, "claim_delivery", new=AsyncMock(return_value=("DELIVERED", {"id": "e"}))),
            patch.object(worker, "delete_message", new=AsyncMock()) as deleted,
        ):
            await worker.process_message(Mock(), "q", AsyncMock(), self.message())
            deleted.assert_awaited_once()

    async def test_busy_message_visibility_is_extended(self):
        sqs = Mock()
        with patch.object(worker, "claim_delivery", new=AsyncMock(return_value=("BUSY", {"id": "e"}))):
            await worker.process_message(sqs, "q", AsyncMock(), self.message())
        sqs.change_message_visibility.assert_called_once()

    async def test_invalid_and_dead_messages_are_not_deleted(self):
        for state in ("INVALID", "DEAD"):
            with (
                self.subTest(state=state),
                patch.object(worker, "claim_delivery", new=AsyncMock(return_value=(state, None))),
                patch.object(worker, "delete_message", new=AsyncMock()) as deleted,
            ):
                await worker.process_message(Mock(), "q", AsyncMock(), self.message())
                deleted.assert_not_awaited()

    async def test_claimed_message_is_delivered_and_deleted(self):
        event = {"id": "e", "invoice_id": "i", "payload": {"id": "i"}, "attempt_count": 1, "max_attempts": 3}
        response = Mock()
        response.raise_for_status.return_value = None
        client = AsyncMock()
        client.post.return_value = response
        with (
            patch.object(worker, "claim_delivery", new=AsyncMock(return_value=("CLAIMED", event))),
            patch.object(worker, "mark_delivered", new=AsyncMock()) as delivered,
            patch.object(worker, "delete_message", new=AsyncMock()) as deleted,
        ):
            await worker.process_message(Mock(), "q", client, self.message())
        delivered.assert_awaited_once_with(event)
        deleted.assert_awaited_once()

    async def test_delivery_failure_is_persisted(self):
        event = {"id": "e", "invoice_id": "i", "payload": {}, "attempt_count": 2, "max_attempts": 3}
        client = AsyncMock()
        client.post.side_effect = RuntimeError("downstream")
        with (
            patch.object(worker, "claim_delivery", new=AsyncMock(return_value=("CLAIMED", event))),
            patch.object(worker, "mark_failed", new=AsyncMock()) as failed,
        ):
            await worker.process_message(Mock(), "q", client, self.message())
        failed.assert_awaited_once()

    async def test_delete_and_resolve_helpers(self):
        sqs = Mock()
        sqs.get_queue_url.return_value = {"QueueUrl": "q"}
        self.assertEqual(await worker.resolve_queue_url(sqs), "q")
        await worker.delete_message(sqs, "q", "receipt")
        sqs.delete_message.assert_called_once()
        self.assertIsNone(worker.utc_now().tzinfo)


class FXWorkerControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_and_process_job(self):
        response = Mock(text="csv")
        response.raise_for_status.return_value = None
        client = AsyncMock()
        client.get.return_value = response
        with patch.object(fx, "parse_ecb_csv", return_value=(date(2026, 7, 20), {"INR": Decimal("100")})):
            payload, provider_date, quotes = await fx.fetch_ecb_rates(client)
        self.assertEqual(payload, "csv")
        self.assertEqual(provider_date, date(2026, 7, 20))
        with (
            patch.object(fx, "fetch_ecb_rates", new=AsyncMock(return_value=(payload, provider_date, quotes))),
            patch.object(fx, "store_rates", new=AsyncMock(return_value=7)) as store,
        ):
            await fx.process_job(client, {"id": "job"})
            store.assert_awaited_once()

    async def test_run_once_no_job(self):
        with patch.object(fx, "claim_job", new=AsyncMock(return_value=None)):
            self.assertFalse(await fx.run_once())

    async def test_run_once_success_and_enqueue(self):
        job = {"id": "j", "attempt_count": 1, "max_attempts": 3}
        with (
            patch.object(fx, "enqueue_import", new=AsyncMock()) as enqueue,
            patch.object(fx, "claim_job", new=AsyncMock(return_value=job)),
            patch.object(fx, "process_job", new=AsyncMock()),
        ):
            self.assertTrue(await fx.run_once(enqueue=True))
            enqueue.assert_awaited_once()

    async def test_run_once_marks_failure(self):
        job = {"id": "j", "attempt_count": 1, "max_attempts": 3}
        with (
            patch.object(fx, "claim_job", new=AsyncMock(return_value=job)),
            patch.object(fx, "process_job", new=AsyncMock(side_effect=RuntimeError("bad feed"))),
            patch.object(fx, "mark_failed", new=AsyncMock()) as failed,
        ):
            self.assertFalse(await fx.run_once())
            failed.assert_awaited_once()

    def test_clock_helpers(self):
        self.assertIsNone(fx.utc_now().tzinfo)
        self.assertIsInstance(fx.india_today(), date)


if __name__ == "__main__":
    unittest.main()
