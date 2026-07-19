import logging
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import get_db
from src.auth import CurrentUser, get_current_user
from src.models import Invoice

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/journal-entries")
async def get_journal_entries(
    invoice_id: str = Query(
        ...,
        alias="invoice",
        description="Required — invoice to fetch GL entries for",
    ),
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
                Invoice.entity_id == current_user.entity_id,
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
              AND entity_id = :entity_id
              AND (
                (reference_type = 'INVOICE' AND reference_id = :invoice_id)
                OR id IN (
                    SELECT je.id
                    FROM journal_entry je
                    JOIN payment_allocation pa ON pa.payment_id = je.reference_id
                    WHERE je.reference_type = 'PAYMENT'
                      AND pa.invoice_id = :invoice_id
                      AND je.tenant_id = :tenant_id
                      AND je.entity_id = :entity_id
                )
              )
        """),
        {
            "tenant_id": current_user.tenant_id,
            "entity_id": current_user.entity_id,
            "invoice_id": invoice_id,
        }
    )
    total = count_result.scalar()

    # Fetch entries
    entries_result = await db.execute(
        text("""
            SELECT id, reference_type, reference_id,
                   document_date, entry_date, description, created_by, currency
            FROM journal_entry
            WHERE tenant_id = :tenant_id
              AND entity_id = :entity_id
              AND (
                (reference_type = 'INVOICE' AND reference_id = :invoice_id)
                OR id IN (
                    SELECT je.id
                    FROM journal_entry je
                    JOIN payment_allocation pa ON pa.payment_id = je.reference_id
                    WHERE je.reference_type = 'PAYMENT'
                      AND pa.invoice_id = :invoice_id
                      AND je.tenant_id = :tenant_id
                      AND je.entity_id = :entity_id
                )
              )
            ORDER BY entry_date ASC, created_at ASC, id ASC
            LIMIT :limit OFFSET :offset
        """),
        {
            "tenant_id": current_user.tenant_id,
            "entity_id": current_user.entity_id,
            "invoice_id": invoice_id,
            "limit": page_size,
            "offset": offset,
        }
    )
    entries = entries_result.fetchall()

    # Summary is invoice-scoped, not page-scoped. Otherwise page_size=1 would
    # misleadingly show only the approval debit on page 1 and only the payment
    # credit on page 2.
    summary_result = await db.execute(
        text("""
            WITH invoice_entries AS (
                SELECT id
                FROM journal_entry
                WHERE tenant_id = :tenant_id
                  AND entity_id = :entity_id
                  AND (
                    (reference_type = 'INVOICE' AND reference_id = :invoice_id)
                    OR id IN (
                        SELECT je.id
                        FROM journal_entry je
                        JOIN payment_allocation pa
                          ON pa.payment_id = je.reference_id
                        WHERE je.reference_type = 'PAYMENT'
                          AND pa.invoice_id = :invoice_id
                          AND je.tenant_id = :tenant_id
                          AND je.entity_id = :entity_id
                    )
                  )
            )
            SELECT
                COALESCE(SUM(jel.debit_amount), 0) AS total_debited_ar,
                COALESCE(SUM(jel.credit_amount), 0) AS total_credited_ar
            FROM invoice_entries entries
            JOIN journal_entry_line jel ON jel.journal_entry_id = entries.id
            JOIN gl_account ga ON ga.id = jel.gl_account_id
            WHERE ga.account_code = '1200'
              AND ga.tenant_id = :tenant_id
              AND ga.entity_id = :entity_id
        """),
        {
            "tenant_id": current_user.tenant_id,
            "entity_id": current_user.entity_id,
            "invoice_id": invoice_id,
        },
    )
    summary = summary_result.one()
    total_debited_ar = Decimal(str(summary.total_debited_ar))
    total_credited_ar = Decimal(str(summary.total_credited_ar))

    # ── Fetch lines for each entry ─────────────────────────
    journal_entries_response = []

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

        journal_entries_response.append({
            "id": str(entry.id),
            "reference_type": entry.reference_type,
            "reference_id": str(entry.reference_id),
            "document_date": str(entry.document_date),
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
