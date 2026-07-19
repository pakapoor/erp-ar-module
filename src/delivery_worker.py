import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import and_, case, or_, select, update

from src.database import AsyncSessionLocal
from src.models import DeliveryOutbox, Invoice


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DELIVERY_SERVICE_URL = os.getenv("DELIVERY_SERVICE_URL", "http://stub:9000")
POLL_INTERVAL_SECONDS = float(os.getenv("OUTBOX_POLL_INTERVAL_SECONDS", "2"))
LEASE_SECONDS = int(os.getenv("OUTBOX_LEASE_SECONDS", "60"))


def utc_now() -> datetime:
    """Return UTC as a naive value to match the schema's UTC TIMESTAMP columns."""
    return datetime.now(UTC).replace(tzinfo=None)


async def claim_event() -> Optional[dict]:
    """Atomically claim one due event; SKIP LOCKED allows many workers."""
    now = utc_now()
    stale_lock = now - timedelta(seconds=LEASE_SECONDS)

    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                select(DeliveryOutbox)
                .where(
                    and_(
                        DeliveryOutbox.attempt_count < DeliveryOutbox.max_attempts,
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
            event.attempt_count += 1
            event.locked_at = now
            event.updated_at = now
            await db.flush()

            return {
                "id": event.id,
                "tenant_id": event.tenant_id,
                "invoice_id": event.invoice_id,
                "payload": event.payload,
                "attempt_count": event.attempt_count,
                "max_attempts": event.max_attempts,
            }


async def mark_delivered(event: dict) -> None:
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
                    status="DELIVERED",
                    delivered_at=now,
                    locked_at=None,
                    last_error=None,
                    updated_at=now,
                )
            )
            # Delivery is not allowed to overwrite a later payment status.
            await db.execute(
                update(Invoice)
                .where(
                    and_(
                        Invoice.id == event["invoice_id"],
                        Invoice.tenant_id == event["tenant_id"],
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
    backoff_seconds = min(2 ** event["attempt_count"], 60)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(DeliveryOutbox)
                .where(DeliveryOutbox.id == event["id"])
                .values(
                    status="DEAD" if terminal else "PENDING",
                    next_attempt_at=now + timedelta(seconds=backoff_seconds),
                    locked_at=None,
                    last_error=str(error)[:2000],
                    updated_at=now,
                )
            )


async def deliver_event(client: httpx.AsyncClient, event: dict) -> None:
    response = await client.post(
        f"{DELIVERY_SERVICE_URL}/stub/send-invoice",
        json=event["payload"],
        headers={"X-Idempotency-Key": str(event["id"])},
    )
    response.raise_for_status()
    await mark_delivered(event)
    logger.info(
        "Delivered outbox event %s for invoice %s",
        event["id"],
        event["invoice_id"],
    )


async def run_worker() -> None:
    logger.info("Delivery outbox worker started; target=%s", DELIVERY_SERVICE_URL)
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            event = await claim_event()
            if event is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            try:
                await deliver_event(client, event)
            except Exception as exc:
                logger.warning(
                    "Delivery failed for event %s (attempt %s/%s): %s",
                    event["id"],
                    event["attempt_count"],
                    event["max_attempts"],
                    exc,
                )
                await mark_failed(event, exc)


if __name__ == "__main__":
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Delivery outbox worker stopped")
