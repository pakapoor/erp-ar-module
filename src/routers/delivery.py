from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, get_current_user, require_role
from src.database import get_db
from src.exceptions import IdempotencyConflictException
from src.models import DeliveryOutbox, Invoice


router = APIRouter()


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def serialize_event(event: DeliveryOutbox) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "status": event.status,
        "publish_attempt_count": event.publish_attempt_count,
        "delivery_attempt_count": event.attempt_count,
        "sqs_message_id": event.sqs_message_id,
        "last_error": event.last_error,
        "created_at": event.created_at.isoformat(),
        "published_at": (
            event.published_at.isoformat() if event.published_at else None
        ),
        "delivered_at": (
            event.delivered_at.isoformat() if event.delivered_at else None
        ),
    }


@router.get("/invoices/{invoice_id}/delivery")
async def get_invoice_delivery(
    invoice_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    invoice_result = await db.execute(
        select(Invoice.id).where(
            and_(
                Invoice.id == invoice_id,
                Invoice.tenant_id == current_user.tenant_id,
                Invoice.entity_id == current_user.entity_id,
            )
        )
    )
    if invoice_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Invoice not found")

    event_result = await db.execute(
        select(DeliveryOutbox)
        .where(
            and_(
                DeliveryOutbox.invoice_id == invoice_id,
                DeliveryOutbox.tenant_id == current_user.tenant_id,
                DeliveryOutbox.entity_id == current_user.entity_id,
            )
        )
        .order_by(DeliveryOutbox.created_at)
    )
    events = event_result.scalars().all()
    return JSONResponse(
        status_code=200,
        content={
            "invoice_id": invoice_id,
            "events": [serialize_event(event) for event in events],
        },
    )


@router.post(
    "/delivery-events/{event_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_delivery_event(
    event_id: str,
    current_user: CurrentUser = Depends(require_role("cfo")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DeliveryOutbox)
        .where(
            and_(
                DeliveryOutbox.id == event_id,
                DeliveryOutbox.tenant_id == current_user.tenant_id,
                DeliveryOutbox.entity_id == current_user.entity_id,
            )
        )
        .with_for_update()
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Delivery event not found")

    if event.status == "DELIVERED":
        raise IdempotencyConflictException(
            "Delivered events cannot be retried",
            "DELIVERY_ALREADY_COMPLETED",
        )

    if event.status == "DEAD":
        now = utc_now()
        event.status = "PENDING"
        event.attempt_count = 0
        event.publish_attempt_count = 0
        event.next_attempt_at = now
        event.locked_at = None
        event.sqs_message_id = None
        event.published_at = None
        event.delivered_at = None
        event.last_error = None
        event.updated_at = now
        await db.commit()

    # Repeated retry calls while the event is active are harmless and return
    # the current state instead of creating another outbox row or SQS message.
    return JSONResponse(
        status_code=202,
        content={
            "id": event.id,
            "invoice_id": event.invoice_id,
            "status": event.status,
            "message": "Delivery event queued for asynchronous retry",
        },
    )
