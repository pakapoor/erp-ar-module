import logging
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import get_db
from src.auth import CurrentUser, get_current_user
from src.models import Customer, Invoice

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/customers/{customer_id}/aging")
async def get_customer_aging(
    customer_id: str,
    entity_id: str = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /customers/{id}/aging
    FR8 — AR Aging Report

    Reads from ar_aging materialized view (pre-computed, refreshed every 5 min).
    Returns as_of timestamp so user knows data freshness.
    5 min staleness is acceptable for collections team.
    """
    # ── Validate customer belongs to tenant ────────────────
    result = await db.execute(
        select(Customer).where(
            and_(
                Customer.id == customer_id,
                Customer.tenant_id == current_user.tenant_id,
            )
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # ── Use entity_id from query param or JWT ──────────────
    eid = entity_id or current_user.entity_id

    # ── Read from materialized view ────────────────────────
    # ar_aging MV is refreshed every 5 mins by pg_cron
    mv_result = await db.execute(
        text("""
            SELECT
                customer_id,
                entity_id,
                as_of,
                current_amount,
                current_count,
                days_30_amount,
                days_30_count,
                days_60_amount,
                days_60_count,
                days_90_plus_amount,
                days_90_plus_count,
                total_outstanding
            FROM ar_aging
            WHERE tenant_id = :tenant_id
              AND entity_id = :entity_id
              AND customer_id = :customer_id
        """),
        {
            "tenant_id": current_user.tenant_id,
            "entity_id": eid,
            "customer_id": customer_id,
        }
    )
    row = mv_result.fetchone()

    # ── If MV has no data yet, compute live ───────────────
    # This handles edge case where MV hasn't refreshed yet
    if not row:
        return await _compute_aging_live(
            db, customer, eid, current_user.tenant_id
        )

    # ── Build response ─────────────────────────────────────
    # Get invoice IDs per bucket for detail
    bucket_invoices = await _get_bucket_invoice_ids(
        db, current_user.tenant_id, eid, customer_id
    )

    response_body = {
        "customer": {"id": customer.id, "name": customer.name},
        "entity_id": eid,
        "as_of": row.as_of.isoformat() if row.as_of else datetime.utcnow().isoformat(),
        "data_freshness": "5 minutes",
        "currency": customer.currency,
        "buckets": {
            "current": {
                "amount": str(row.current_amount or 0),
                "invoice_count": row.current_count or 0,
                "invoices": bucket_invoices.get("current", []),
            },
            "days_30": {
                "amount": str(row.days_30_amount or 0),
                "invoice_count": row.days_30_count or 0,
                "invoices": bucket_invoices.get("days_30", []),
            },
            "days_60": {
                "amount": str(row.days_60_amount or 0),
                "invoice_count": row.days_60_count or 0,
                "invoices": bucket_invoices.get("days_60", []),
            },
            "days_90_plus": {
                "amount": str(row.days_90_plus_amount or 0),
                "invoice_count": row.days_90_plus_count or 0,
                "invoices": bucket_invoices.get("days_90_plus", []),
            },
        },
        "total_outstanding": str(row.total_outstanding or 0),
    }

    return JSONResponse(status_code=200, content=response_body)


async def _get_bucket_invoice_ids(
    db: AsyncSession,
    tenant_id: str,
    entity_id: str,
    customer_id: str,
) -> dict:
    """Get invoice IDs per aging bucket for detail view"""
    result = await db.execute(
        text("""
            SELECT
                id,
                CASE
                    WHEN due_date >= CURRENT_DATE THEN 'current'
                    WHEN due_date >= CURRENT_DATE - INTERVAL '30 days' THEN 'days_30'
                    WHEN due_date >= CURRENT_DATE - INTERVAL '60 days' THEN 'days_60'
                    ELSE 'days_90_plus'
                END as bucket
            FROM invoice
            WHERE tenant_id = :tenant_id
              AND entity_id = :entity_id
              AND customer_id = :customer_id
              AND status NOT IN ('PAID', 'VOID', 'WRITTEN_OFF')
              AND balance_amount > 0
        """),
        {
            "tenant_id": tenant_id,
            "entity_id": entity_id,
            "customer_id": customer_id,
        }
    )
    buckets = {"current": [], "days_30": [], "days_60": [], "days_90_plus": []}
    for row in result.fetchall():
        buckets[row.bucket].append(str(row.id))
    return buckets


async def _compute_aging_live(
    db: AsyncSession,
    customer,
    entity_id: str,
    tenant_id: str,
) -> JSONResponse:
    """
    Fallback: compute aging live if MV not yet populated.
    Uses same bucket logic as the MV.
    """
    result = await db.execute(
        text("""
            SELECT
                id,
                balance_amount,
                CASE
                    WHEN due_date >= CURRENT_DATE THEN 'current'
                    WHEN due_date >= CURRENT_DATE - INTERVAL '30 days' THEN 'days_30'
                    WHEN due_date >= CURRENT_DATE - INTERVAL '60 days' THEN 'days_60'
                    ELSE 'days_90_plus'
                END as bucket
            FROM invoice
            WHERE tenant_id = :tenant_id
              AND entity_id = :entity_id
              AND customer_id = :customer_id
              AND status NOT IN ('PAID', 'VOID', 'WRITTEN_OFF')
              AND balance_amount > 0
        """),
        {
            "tenant_id": tenant_id,
            "entity_id": entity_id,
            "customer_id": customer.id,
        }
    )
    rows = result.fetchall()

    buckets = {
        "current": {"amount": Decimal("0"), "invoices": []},
        "days_30": {"amount": Decimal("0"), "invoices": []},
        "days_60": {"amount": Decimal("0"), "invoices": []},
        "days_90_plus": {"amount": Decimal("0"), "invoices": []},
    }
    total = Decimal("0")

    for row in rows:
        bucket = row.bucket
        buckets[bucket]["amount"] += row.balance_amount
        buckets[bucket]["invoices"].append(str(row.id))
        total += row.balance_amount

    return JSONResponse(
        status_code=200,
        content={
            "customer": {"id": customer.id, "name": customer.name},
            "entity_id": entity_id,
            "as_of": datetime.utcnow().isoformat(),
            "data_freshness": "live (MV not yet populated)",
            "currency": customer.currency,
            "buckets": {
                k: {
                    "amount": str(v["amount"]),
                    "invoice_count": len(v["invoices"]),
                    "invoices": v["invoices"],
                }
                for k, v in buckets.items()
            },
            "total_outstanding": str(total),
        }
    )


# ============================================================
