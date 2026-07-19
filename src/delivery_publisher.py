import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Optional

import boto3
from botocore.config import Config
from sqlalchemy import and_, or_, select, update

from src.database import AsyncSessionLocal
from src.models import DeliveryOutbox


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SQS_ENDPOINT_URL = os.getenv("SQS_ENDPOINT_URL") or None
DELIVERY_QUEUE_NAME = os.getenv("DELIVERY_QUEUE_NAME", "invoice-delivery")
POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "1"))
LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "30"))


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


async def claim_event() -> Optional[dict]:
    """Claim one due outbox row; SKIP LOCKED permits many publishers."""
    now = utc_now()
    stale_lock = now - timedelta(seconds=LEASE_SECONDS)

    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                select(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.publish_attempt_count
                        < DeliveryOutbox.max_publish_attempts,
                        or_(
                            and_(
                                DeliveryOutbox.status == "PENDING",
                                DeliveryOutbox.next_attempt_at <= now,
                            ),
                            and_(
                                DeliveryOutbox.status == "PROCESSING",
                                DeliveryOutbox.locked_at < stale_lock,
                            ),
                        ),
                    )
                )
                .order_by(DeliveryOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            event = result.scalar_one_or_none()
            if event is None:
                return None

            event.status = "PROCESSING"
            event.publish_attempt_count += 1
            event.locked_at = now
            event.updated_at = now
            await db.flush()

            return {
                "id": event.id,
                "tenant_id": event.tenant_id,
                "entity_id": event.entity_id,
                "invoice_id": event.invoice_id,
                "event_type": event.event_type,
                "payload": event.payload,
                "publish_attempt_count": event.publish_attempt_count,
                "max_publish_attempts": event.max_publish_attempts,
            }


async def mark_published(event: dict, sqs_message_id: str) -> None:
    now = utc_now()
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.id == event["id"],
                        DeliveryOutbox.status == "PROCESSING",
                    )
                )
                .values(
                    status="PUBLISHED",
                    sqs_message_id=sqs_message_id,
                    published_at=now,
                    locked_at=None,
                    last_error=None,
                    updated_at=now,
                )
            )


async def mark_publish_failed(event: dict, error: Exception) -> None:
    now = utc_now()
    terminal = (
        event["publish_attempt_count"] >= event["max_publish_attempts"]
    )
    backoff_seconds = min(2 ** event["publish_attempt_count"], 60)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.id == event["id"],
                        DeliveryOutbox.status == "PROCESSING",
                    )
                )
                .values(
                    status="DEAD" if terminal else "PENDING",
                    next_attempt_at=now + timedelta(seconds=backoff_seconds),
                    locked_at=None,
                    last_error=f"SQS publish failed: {error}"[:2000],
                    updated_at=now,
                )
            )


async def publish_event(sqs_client, queue_url: str, event: dict) -> None:
    envelope = {
        "event_id": str(event["id"]),
        "event_type": event["event_type"],
        "tenant_id": str(event["tenant_id"]),
        "entity_id": str(event["entity_id"]),
        "invoice_id": str(event["invoice_id"]),
        "payload": event["payload"],
    }
    response = await asyncio.to_thread(
        sqs_client.send_message,
        QueueUrl=queue_url,
        MessageBody=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        MessageAttributes={
            "event_type": {
                "DataType": "String",
                "StringValue": event["event_type"],
            }
        },
    )
    await mark_published(event, response["MessageId"])
    logger.info(
        "Published outbox event %s to SQS message %s",
        event["id"],
        response["MessageId"],
    )


async def run_publisher() -> None:
    sqs_client = create_sqs_client()
    queue_url = await resolve_queue_url(sqs_client)
    logger.info(
        "Delivery outbox publisher started; queue=%s endpoint=%s",
        queue_url,
        SQS_ENDPOINT_URL or "AWS",
    )

    while True:
        event = await claim_event()
        if event is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        try:
            await publish_event(sqs_client, queue_url, event)
        except Exception as exc:
            logger.warning(
                "SQS publish failed for event %s (attempt %s/%s): %s",
                event["id"],
                event["publish_attempt_count"],
                event["max_publish_attempts"],
                exc,
            )
            await mark_publish_failed(event, exc)


if __name__ == "__main__":
    try:
        asyncio.run(run_publisher())
    except KeyboardInterrupt:
        logger.info("Delivery outbox publisher stopped")
