import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Optional

import boto3
import httpx
from botocore.config import Config
from sqlalchemy import and_, case, select, update

from src.database import AsyncSessionLocal
from src.models import DeliveryOutbox, Invoice


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SQS_ENDPOINT_URL = os.getenv("SQS_ENDPOINT_URL") or None
DELIVERY_QUEUE_NAME = os.getenv("DELIVERY_QUEUE_NAME", "invoice-delivery")
DELIVERY_SERVICE_URL = os.getenv("DELIVERY_SERVICE_URL", "http://stub:9000")
WAIT_TIME_SECONDS = int(os.getenv("SQS_WAIT_TIME_SECONDS", "2"))
VISIBILITY_TIMEOUT_SECONDS = int(
    os.getenv("SQS_VISIBILITY_TIMEOUT_SECONDS", "3")
)
DELIVERY_LEASE_SECONDS = int(os.getenv("DELIVERY_LEASE_SECONDS", "30"))


def utc_now() -> datetime:
    """Return naive UTC to match the schema's TIMESTAMP columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def create_sqs_client():
    return boto3.client(
        "sqs",
        region_name=AWS_REGION,
        endpoint_url=SQS_ENDPOINT_URL,
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
    )


async def resolve_queue_url(sqs_client) -> str:
    while True:
        try:
            response = await asyncio.to_thread(
                sqs_client.get_queue_url,
                QueueName=DELIVERY_QUEUE_NAME,
            )
            return response["QueueUrl"]
        except Exception as exc:
            logger.warning("Waiting for SQS queue %s: %s", DELIVERY_QUEUE_NAME, exc)
            await asyncio.sleep(2)


async def claim_delivery(
    envelope: dict,
    receive_count: int,
) -> tuple[str, Optional[dict]]:
    """Claim the durable event before performing its external side effect."""
    required_fields = {
        "event_id", "tenant_id", "entity_id", "invoice_id", "event_type"
    }
    if not required_fields.issubset(envelope):
        return "INVALID", None

    now = utc_now()
    stale_lock = now - timedelta(seconds=DELIVERY_LEASE_SECONDS)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                select(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.id == envelope["event_id"],
                        DeliveryOutbox.tenant_id == envelope["tenant_id"],
                        DeliveryOutbox.entity_id == envelope["entity_id"],
                        DeliveryOutbox.invoice_id == envelope["invoice_id"],
                        DeliveryOutbox.event_type == envelope["event_type"],
                    )
                )
                .with_for_update()
            )
            event = result.scalar_one_or_none()
            if event is None:
                return "INVALID", None
            if event.status == "DELIVERED":
                return "DELIVERED", {"id": event.id}
            if event.status == "DEAD":
                return "DEAD", {"id": event.id}
            if (
                event.status == "DELIVERING"
                and event.locked_at
                and event.locked_at >= stale_lock
            ):
                return "BUSY", {"id": event.id}

            event.attempt_count = max(event.attempt_count, receive_count)
            if event.attempt_count > event.max_attempts:
                event.status = "DEAD"
                event.locked_at = None
                event.last_error = "SQS receive count exceeded delivery limit"
                event.updated_at = now
                return "DEAD", {"id": event.id}

            event.status = "DELIVERING"
            event.locked_at = now
            event.updated_at = now
            await db.flush()
            return "CLAIMED", {
                "id": event.id,
                "tenant_id": event.tenant_id,
                "entity_id": event.entity_id,
                "invoice_id": event.invoice_id,
                "payload": event.payload,
                "attempt_count": event.attempt_count,
                "max_attempts": event.max_attempts,
            }


async def mark_delivered(event: dict) -> None:
    now = utc_now()
    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                update(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.id == event["id"],
                        DeliveryOutbox.status == "DELIVERING",
                    )
                )
                .values(
                    status="DELIVERED",
                    delivered_at=now,
                    locked_at=None,
                    last_error=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return

            # Delivery never overwrites a later payment status.
            await db.execute(
                update(Invoice)
                .where(
                    and_(
                        Invoice.id == event["invoice_id"],
                        Invoice.tenant_id == event["tenant_id"],
                        Invoice.entity_id == event["entity_id"],
                        Invoice.sent_at.is_(None),
                    )
                )
                .values(
                    status=case(
                        (Invoice.status == "APPROVED", "SENT"),
                        else_=Invoice.status,
                    ),
                    sent_at=now,
                    version=Invoice.version + 1,
                    updated_at=now,
                )
            )


async def mark_failed(event: dict, error: Exception) -> None:
    now = utc_now()
    terminal = event["attempt_count"] >= event["max_attempts"]
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.id == event["id"],
                        DeliveryOutbox.status == "DELIVERING",
                    )
                )
                .values(
                    status="DEAD" if terminal else "PUBLISHED",
                    locked_at=None,
                    last_error=str(error)[:2000],
                    updated_at=now,
                )
            )


async def delete_message(sqs_client, queue_url: str, receipt_handle: str) -> None:
    await asyncio.to_thread(
        sqs_client.delete_message,
        QueueUrl=queue_url,
        ReceiptHandle=receipt_handle,
    )


async def process_message(
    sqs_client,
    queue_url: str,
    client: httpx.AsyncClient,
    message: dict,
) -> None:
    receipt_handle = message["ReceiptHandle"]
    try:
        envelope = json.loads(message["Body"])
    except (TypeError, json.JSONDecodeError) as exc:
        logger.error("Invalid SQS delivery message %s: %s", message.get("MessageId"), exc)
        return

    receive_count = int(
        message.get("Attributes", {}).get("ApproximateReceiveCount", "1")
    )
    claim_status, event = await claim_delivery(envelope, receive_count)

    if claim_status == "DELIVERED":
        await delete_message(sqs_client, queue_url, receipt_handle)
        logger.info("Discarded duplicate message for delivered event %s", event["id"])
        return
    if claim_status == "BUSY":
        await asyncio.to_thread(
            sqs_client.change_message_visibility,
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
        )
        return
    if claim_status in {"INVALID", "DEAD"}:
        # Do not delete poison/dead messages; SQS redrive moves them to the DLQ.
        logger.warning(
            "Leaving %s delivery message %s for SQS redrive",
            claim_status.lower(),
            message.get("MessageId"),
        )
        return

    try:
        response = await client.post(
            f"{DELIVERY_SERVICE_URL}/stub/send-invoice",
            json=event["payload"],
            headers={"X-Idempotency-Key": str(event["id"])},
        )
        response.raise_for_status()
        await mark_delivered(event)
        await delete_message(sqs_client, queue_url, receipt_handle)
        logger.info(
            "Delivered SQS event %s for invoice %s",
            event["id"],
            event["invoice_id"],
        )
    except Exception as exc:
        logger.warning(
            "Delivery failed for event %s (receive %s/%s): %s",
            event["id"],
            event["attempt_count"],
            event["max_attempts"],
            exc,
        )
        await mark_failed(event, exc)


async def run_worker() -> None:
    sqs_client = create_sqs_client()
    queue_url = await resolve_queue_url(sqs_client)
    logger.info(
        "SQS delivery consumer started; queue=%s target=%s",
        queue_url,
        DELIVERY_SERVICE_URL,
    )
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            response = await asyncio.to_thread(
                sqs_client.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=WAIT_TIME_SECONDS,
                VisibilityTimeout=VISIBILITY_TIMEOUT_SECONDS,
                AttributeNames=["ApproximateReceiveCount"],
            )
            for message in response.get("Messages", []):
                await process_message(sqs_client, queue_url, client, message)


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("SQS delivery consumer stopped")
