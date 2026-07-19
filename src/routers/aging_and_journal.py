from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, get_current_user
from src.models import Customer, Invoice, JournalEntry, JournalEntryLine, GLAccount

import logging
logger = logging.getLogger(__name__)

# ============================================================
# AGING ROUTER
# ============================================================
aging_router = APIRouter()


@aging_router.get("/customers/{customer_id}/aging")
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
        buckets[row.bucket].append(row.id)
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
        buckets[bucket]["invoices"].append(row.id)
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
# JOURNAL ENTRIES ROUTER
# ============================================================
journal_router = APIRouter()


@journal_router.get("/journal-entries")
async def get_journal_entries(
    invoice_id: str = Query(..., description="Required — invoice to fetch GL entries for"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    GET /journal-entries?invoice_id={id}
    FR9 — GL Journal Entries

    Live query — journal entries are immutable but must never appear
    missing to SOX auditors. No cache. No MV.
    Indexed on reference_type + reference_id for fast lookup.
    Page-based pagination (max ~20 entries per invoice in practice).
    """
    # ── Validate invoice belongs to tenant ─────────────────
    inv_result = await db.execute(
        select(Invoice).where(
            and_(
                Invoice.id == invoice_id,
                Invoice.tenant_id == current_user.tenant_id,
            )
        )
    )
    invoice = inv_result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ── Fetch journal entries for this invoice ─────────────
    # Uses idx_je_reference index: reference_type + reference_id
    offset = (page - 1) * page_size

    # Count total
    count_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM journal_entry
            WHERE tenant_id = :tenant_id
              AND (
                (reference_type = 'INVOICE' AND reference_id = :invoice_id)
                OR id IN (
                    SELECT je.id
                    FROM journal_entry je
                    JOIN payment_allocation pa ON pa.payment_id = je.reference_id
                    WHERE je.reference_type = 'PAYMENT'
                      AND pa.invoice_id = :invoice_id
                      AND je.tenant_id = :tenant_id
                )
              )
        """),
        {"tenant_id": current_user.tenant_id, "invoice_id": invoice_id}
    )
    total = count_result.scalar()

    # Fetch entries
    entries_result = await db.execute(
        text("""
            SELECT id, reference_type, reference_id,
                   entry_date, description, created_by, currency
            FROM journal_entry
            WHERE tenant_id = :tenant_id
              AND (
                (reference_type = 'INVOICE' AND reference_id = :invoice_id)
                OR id IN (
                    SELECT je.id
                    FROM journal_entry je
                    JOIN payment_allocation pa ON pa.payment_id = je.reference_id
                    WHERE je.reference_type = 'PAYMENT'
                      AND pa.invoice_id = :invoice_id
                      AND je.tenant_id = :tenant_id
                )
              )
            ORDER BY entry_date ASC
            LIMIT :limit OFFSET :offset
        """),
        {
            "tenant_id": current_user.tenant_id,
            "invoice_id": invoice_id,
            "limit": page_size,
            "offset": offset,
        }
    )
    entries = entries_result.fetchall()

    # ── Fetch lines for each entry ─────────────────────────
    journal_entries_response = []
    total_debited_ar = Decimal("0")
    total_credited_ar = Decimal("0")

    for entry in entries:
        lines_result = await db.execute(
            text("""
                SELECT jel.debit_amount, jel.credit_amount,
                       ga.account_code, ga.account_name
                FROM journal_entry_line jel
                JOIN gl_account ga ON ga.id = jel.gl_account_id
                WHERE jel.journal_entry_id = :je_id
                ORDER BY jel.debit_amount DESC
            """),
            {"je_id": entry.id}
        )
        lines = lines_result.fetchall()

        total_debits = sum(Decimal(str(l.debit_amount)) for l in lines)
        total_credits = sum(Decimal(str(l.credit_amount)) for l in lines)
        balanced = abs(total_debits - total_credits) < Decimal("0.01")

        if not balanced:
            logger.error(
                f"UNBALANCED JOURNAL ENTRY DETECTED: {entry.id} "
                f"debits={total_debits} credits={total_credits}"
            )

        # Track AR movements for summary
        for line in lines:
            if line.account_code == "1200":  # AR account
                total_debited_ar += Decimal(str(line.debit_amount))
                total_credited_ar += Decimal(str(line.credit_amount))

        journal_entries_response.append({
            "id": entry.id,
            "reference_type": entry.reference_type,
            "reference_id": str(entry.reference_id),
            "entry_date": str(entry.entry_date),
            "description": entry.description,
            "created_by": str(entry.created_by),
            "lines": [
                {
                    "account_code": line.account_code,
                    "account_name": line.account_name,
                    "debit_amount": str(line.debit_amount),
                    "credit_amount": str(line.credit_amount),
                }
                for line in lines
            ],
            "total_debits": str(total_debits),
            "total_credits": str(total_credits),
            "balanced": balanced,
        })

    # ── Build response ─────────────────────────────────────
    response_body = {
        "invoice_id": invoice_id,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
        "journal_entries": journal_entries_response,
        "summary": {
            "total_debited_ar": str(total_debited_ar),
            "total_credited_ar": str(total_credited_ar),
            "net_ar_balance": str(total_debited_ar - total_credited_ar),
        },
    }

    return JSONResponse(status_code=200, content=response_body)
