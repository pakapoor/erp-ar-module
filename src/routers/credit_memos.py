import hashlib
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, require_role
from src.models import (
    Invoice, CreditMemo, JournalEntry, JournalEntryLine,
    GLAccount, AccountingPeriod, IdempotencyKey
)
from src.exceptions import (
    BusinessRuleException, PeriodClosedException,
    IdempotencyConflictException
)
from src.routers.invoices import (
    check_idempotency, create_idempotency_key,
    complete_idempotency_key,
    GL_AR, GL_REVENUE, GL_TAX_PAYABLE
)

logger = logging.getLogger(__name__)
router = APIRouter()

# GL account for bad debt (used in write-off)
GL_BAD_DEBT = "4100"

# Invoice statuses that allow credit memo
CM_ALLOWED_STATUSES = ["APPROVED", "SENT", "PARTIALLY_PAID", "PAID"]

# Invoice statuses that allow write-off
WO_ALLOWED_STATUSES = ["APPROVED", "SENT", "PARTIALLY_PAID"]

# Invoice statuses that allow void
VOID_ALLOWED_STATUSES = ["DRAFT", "APPROVED", "SENT"]

# Valid credit memo reason codes
CM_REASONS = ["OVERCHARGE", "RETURN", "DUPLICATE", "CANCEL"]

# Valid write-off reason codes
WO_REASONS = ["BANKRUPTCY", "ABSCONDING", "UNCOLLECTIBLE", "BAD_DEBT"]

# Valid void reason codes
VOID_REASONS = ["DUPLICATE", "WRONG_CUSTOMER", "DATA_ERROR"]


async def set_audit_context(db: AsyncSession, current_user: CurrentUser) -> None:
    """Provide verified actor scope to database audit triggers."""
    await db.execute(
        text("""
            SELECT
                set_config('app.current_user_id', :user_id, true),
                set_config('app.tenant_id', :tenant_id, true),
                set_config('app.entity_id', :entity_id, true)
        """),
        {
            "user_id": current_user.user_id,
            "tenant_id": current_user.tenant_id,
            "entity_id": current_user.entity_id,
        },
    )


# ============================================================
# API: POST /invoices/{id}/credit-memos
# FR-B1 — Credit Memo
# ============================================================
@router.post("/invoices/{invoice_id}/credit-memos", status_code=status.HTTP_201_CREATED)
async def create_credit_memo(
    invoice_id: str,
    payload: dict,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("invoice_approver", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    """
    FR-B1 — Create and approve a credit memo against an invoice.

    GL entry on approval:
        Debit  Revenue       subtotal_amount   <- un-earn revenue
        Debit  Tax Payable   tax_amount        <- reverse tax liability
        Credit AR            total_amount      <- reduce receivable

    Allowed against: APPROVED, SENT, PARTIALLY_PAID, PAID
    Not allowed against: DRAFT, VOID, WRITTEN_OFF
    """
    endpoint_name = f"POST /invoices/{invoice_id}/credit-memos"
    reason_code = payload.get("reason_code", "").upper()
    description = payload.get("description", "")
    amount = Decimal(str(payload.get("amount", 0)))
    include_tax = payload.get("include_tax", False)

    if reason_code not in CM_REASONS:
        raise BusinessRuleException(
            "INVALID_REASON_CODE",
            f"reason_code must be one of: {CM_REASONS}"
        )
    if amount <= 0:
        raise BusinessRuleException("INVALID_AMOUNT", "Credit memo amount must be positive")

    await set_audit_context(db, current_user)

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        json.dumps({"invoice_id": invoice_id, "amount": str(amount),
                    "reason": reason_code}, default=str).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/credit-memos", request_hash
    )
    if existing:
        return JSONResponse(status_code=existing.response_status,
                            content=existing.response_body)

    # ── Fetch invoice ──────────────────────────────────────
    result = await db.execute(
        select(Invoice).where(
            and_(Invoice.id == invoice_id,
                 Invoice.tenant_id == current_user.tenant_id)
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status not in CM_ALLOWED_STATUSES:
        raise BusinessRuleException(
            "INVALID_STATUS",
            f"Credit memo not allowed against invoice in status: {invoice.status}"
        )

    if amount > invoice.total_amount:
        raise BusinessRuleException(
            "AMOUNT_EXCEEDS_INVOICE",
            f"Credit memo amount {amount} exceeds invoice total {invoice.total_amount}"
        )

    # ── Period check ───────────────────────────────────────
    period_result = await db.execute(
        select(AccountingPeriod).where(
            and_(
                AccountingPeriod.tenant_id == current_user.tenant_id,
                AccountingPeriod.entity_id == current_user.entity_id,
                AccountingPeriod.status == "OPEN",
            )
        ).order_by(AccountingPeriod.start_date.desc()).limit(1)
    )
    period = period_result.scalar_one_or_none()
    if not period:
        raise PeriodClosedException("No open accounting period found")

    # ── Fetch GL accounts ──────────────────────────────────
    gl_result = await db.execute(
        select(GLAccount).where(
            and_(
                GLAccount.entity_id == current_user.entity_id,
                GLAccount.account_code.in_([GL_AR, GL_REVENUE, GL_TAX_PAYABLE]),
            )
        )
    )
    gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

    # ── Begin idempotency ──────────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/credit-memos", request_hash
    )

    # ── Calculate tax portion ──────────────────────────────
    # Tax is proportional to the credit amount vs invoice total
    if include_tax and invoice.total_amount > 0:
        tax_ratio = invoice.tax_amount / invoice.total_amount
        tax_credit = (amount * tax_ratio).quantize(Decimal("0.0001"))
        subtotal_credit = amount - tax_credit
    else:
        tax_credit = Decimal("0")
        subtotal_credit = amount

    rate = invoice.exchange_rate or Decimal("1")
    base_credit = (amount * rate).quantize(Decimal("0.0001"))
    base_tax_credit = (tax_credit * rate).quantize(Decimal("0.0001"))
    base_subtotal_credit = base_credit - base_tax_credit

    now = datetime.utcnow()

    # ── Create credit memo record ──────────────────────────
    cm = CreditMemo(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        invoice_id=invoice_id,
        reason_code=reason_code,
        description=description,
        amount=amount,
        base_amount=base_credit,
        currency=invoice.transaction_currency,
        status="APPLIED",
        created_by=invoice.created_by,   # invoice creator raises the CM
        approved_by=current_user.user_id,  # approver approves it (SOX: different person)
        approved_at=now,
        applied_at=now,
    )
    db.add(cm)
    await db.flush()

    # ── Generate GL journal entry ──────────────────────────
    # Dr Revenue       subtotal_credit   <- un-earn
    # Dr Tax Payable   tax_credit        <- reverse tax (if applicable)
    # Cr AR            amount            <- reduce receivable
    journal_entry = JournalEntry(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        period_id=period.id,
        reference_type="CREDIT_MEMO",
        reference_id=cm.id,
        entry_date=now.date(),
        document_date=now.date(),
        description=f"Credit memo {cm.id} against invoice {invoice_id}: {reason_code}",
        currency=invoice.transaction_currency,
        created_by=current_user.user_id,
    )
    db.add(journal_entry)
    await db.flush()

    # Dr: Revenue (un-earn)
    if GL_REVENUE in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_REVENUE].id,
            description="Revenue reversed",
            debit_amount=subtotal_credit,
            credit_amount=Decimal("0"),
            base_debit_amount=base_subtotal_credit,
            base_credit_amount=Decimal("0"),
        ))

    # Dr: Tax Payable (reverse if applicable)
    if tax_credit > 0 and GL_TAX_PAYABLE in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_TAX_PAYABLE].id,
            description="Tax payable reversed",
            debit_amount=tax_credit,
            credit_amount=Decimal("0"),
            base_debit_amount=base_tax_credit,
            base_credit_amount=Decimal("0"),
        ))

    # Cr: AR (reduce receivable)
    if GL_AR in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_AR].id,
            description="AR reduced by credit memo",
            debit_amount=Decimal("0"),
            credit_amount=amount,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_credit,
        ))

    # ── Update invoice balance ─────────────────────────────
    balance_before = invoice.balance_amount
    base_balance_before = invoice.base_balance_amount
    new_balance = max(balance_before - amount, Decimal("0"))
    new_base_balance = max(base_balance_before - base_credit, Decimal("0"))
    invoice.balance_amount = new_balance
    invoice.base_balance_amount = new_base_balance
    invoice.version += 1
    invoice.updated_at = now
    if new_balance == 0 and invoice.status != "PAID":
        invoice.status = "PAID"

    await db.flush()

    # ── Build response ─────────────────────────────────────
    response_body = {
        "id": cm.id,
        "invoice_id": invoice_id,
        "reason_code": reason_code,
        "description": description,
        "amount": str(amount),
        "subtotal_reversed": str(subtotal_credit),
        "tax_reversed": str(tax_credit),
        "currency": invoice.transaction_currency,
        "status": "APPLIED",
        "invoice_balance_before": str(balance_before),
        "invoice_balance_after": str(new_balance),
        "base_amount": str(base_credit),
        "base_balance_before": str(base_balance_before),
        "base_balance_after": str(new_base_balance),
        "invoice_status": invoice.status,
        "journal_entry_id": journal_entry.id,
        "approved_by": current_user.user_id,
        "applied_at": now.isoformat(),
    }

    await complete_idempotency_key(db, x_idempotency_key, current_user.tenant_id, current_user.entity_id, endpoint_name, 201, response_body)
    await db.commit()

    logger.info(f"Credit memo {cm.id} applied to invoice {invoice_id}: {amount} {reason_code}")
    return JSONResponse(status_code=201, content=response_body)


# ============================================================
# API: POST /invoices/{id}/writeoff
# FR-B2 — Write-off
# ============================================================
@router.post("/invoices/{invoice_id}/writeoff", status_code=status.HTTP_201_CREATED)
async def write_off_invoice(
    invoice_id: str,
    payload: dict,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("cfo")),
    db: AsyncSession = Depends(get_db),
):
    """
    FR-B2 — Write off an invoice outstanding balance.
    Only CFO can approve write-offs (revenue impact).

    GL entry:
        Debit  Bad Debt Expense  outstanding_balance
        Credit AR                outstanding_balance

    Write-off applies to OUTSTANDING BALANCE only.
    Already-paid amount is never reversed.
    """
    endpoint_name = f"POST /invoices/{invoice_id}/writeoff"
    reason_code = payload.get("reason_code", "").upper()
    description = payload.get("description", "")
    notes = payload.get("notes", "")

    if reason_code not in WO_REASONS:
        raise BusinessRuleException(
            "INVALID_REASON_CODE",
            f"reason_code must be one of: {WO_REASONS}"
        )

    await set_audit_context(db, current_user)

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        f"{invoice_id}:{reason_code}:{current_user.user_id}".encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/writeoff", request_hash
    )
    if existing:
        return JSONResponse(status_code=existing.response_status,
                            content=existing.response_body)

    # ── Fetch invoice ──────────────────────────────────────
    result = await db.execute(
        select(Invoice).where(
            and_(Invoice.id == invoice_id,
                 Invoice.tenant_id == current_user.tenant_id)
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status not in WO_ALLOWED_STATUSES:
        raise BusinessRuleException(
            "INVALID_STATUS",
            f"Write-off not allowed against invoice in status: {invoice.status}"
        )

    if invoice.balance_amount <= 0:
        raise BusinessRuleException(
            "NO_OUTSTANDING_BALANCE",
            "Invoice has no outstanding balance to write off"
        )

    # ── Period check ───────────────────────────────────────
    period_result = await db.execute(
        select(AccountingPeriod).where(
            and_(
                AccountingPeriod.tenant_id == current_user.tenant_id,
                AccountingPeriod.entity_id == current_user.entity_id,
                AccountingPeriod.status == "OPEN",
            )
        ).order_by(AccountingPeriod.start_date.desc()).limit(1)
    )
    period = period_result.scalar_one_or_none()
    if not period:
        raise PeriodClosedException("No open accounting period found")

    # ── Fetch GL accounts ──────────────────────────────────
    gl_result = await db.execute(
        select(GLAccount).where(
            and_(
                GLAccount.entity_id == current_user.entity_id,
                GLAccount.account_code.in_([GL_AR, GL_BAD_DEBT]),
            )
        )
    )
    gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

    writeoff_amount = invoice.balance_amount
    base_writeoff_amount = invoice.base_balance_amount
    now = datetime.utcnow()

    # ── Begin idempotency ──────────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/writeoff", request_hash
    )

    # ── Generate GL journal entry ──────────────────────────
    # Dr Bad Debt Expense  balance_amount
    # Cr AR                balance_amount
    journal_entry = JournalEntry(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        period_id=period.id,
        reference_type="WRITE_OFF",
        reference_id=invoice_id,
        entry_date=now.date(),
        document_date=now.date(),
        description=f"Write-off invoice {invoice_id}: {reason_code} — {description}",
        currency=invoice.transaction_currency,
        created_by=current_user.user_id,
    )
    db.add(journal_entry)
    await db.flush()

    # Dr: Bad Debt Expense
    if GL_BAD_DEBT in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_BAD_DEBT].id,
            description=f"Bad debt expense: {reason_code}",
            debit_amount=writeoff_amount,
            credit_amount=Decimal("0"),
            base_debit_amount=base_writeoff_amount,
            base_credit_amount=Decimal("0"),
        ))

    # Cr: AR
    if GL_AR in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_AR].id,
            description="AR cleared by write-off",
            debit_amount=Decimal("0"),
            credit_amount=writeoff_amount,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_writeoff_amount,
        ))

    # ── Update invoice ─────────────────────────────────────
    invoice.status = "WRITTEN_OFF"
    invoice.balance_amount = Decimal("0")
    invoice.base_balance_amount = Decimal("0")
    invoice.version += 1
    invoice.updated_at = now

    await db.flush()

    response_body = {
        "invoice_id": invoice_id,
        "status": "WRITTEN_OFF",
        "reason_code": reason_code,
        "description": description,
        "notes": notes,
        "writeoff_amount": str(writeoff_amount),
        "base_writeoff_amount": str(base_writeoff_amount),
        "previously_paid": str(invoice.total_amount - writeoff_amount),
        "currency": invoice.transaction_currency,
        "journal_entry_id": journal_entry.id,
        "approved_by": current_user.user_id,
        "written_off_at": now.isoformat(),
    }

    await complete_idempotency_key(db, x_idempotency_key, current_user.tenant_id, current_user.entity_id, endpoint_name, 201, response_body)
    await db.commit()

    logger.info(f"Invoice {invoice_id} written off: {writeoff_amount} {reason_code}")
    return JSONResponse(status_code=201, content=response_body)


# ============================================================
# API: POST /invoices/{id}/void
# FR-B3 — Invoice Void
# ============================================================
@router.post("/invoices/{invoice_id}/void", status_code=status.HTTP_200_OK)
async def void_invoice(
    invoice_id: str,
    payload: dict,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("invoice_approver", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    """
    FR-B3 — Void an invoice.
    Invoice was raised in error — should never have existed.

    GL entry (only if invoice was already APPROVED/SENT):
        Reverse original approval entry:
        Debit  Revenue      subtotal_amount   <- reverse revenue
        Debit  Tax Payable  tax_amount        <- reverse tax
        Credit AR           total_amount      <- remove receivable

    Allowed against: DRAFT, APPROVED, SENT
    Not allowed against: PAID, PARTIALLY_PAID, WRITTEN_OFF
    """
    endpoint_name = f"POST /invoices/{invoice_id}/void"
    # PostgreSQL requires the isolation level before the first query.
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))

    reason_code = payload.get("reason_code", "").upper()
    description = payload.get("description", "")
    void_reference = payload.get("void_reference", "")  # e.g. duplicate of invoice id

    if reason_code not in VOID_REASONS:
        raise BusinessRuleException(
            "INVALID_REASON_CODE",
            f"reason_code must be one of: {VOID_REASONS}"
        )

    await set_audit_context(db, current_user)

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        f"{invoice_id}:{reason_code}:{current_user.user_id}".encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/void", request_hash
    )
    if existing:
        return JSONResponse(status_code=existing.response_status,
                            content=existing.response_body)

    # ── Fetch invoice ──────────────────────────────────────
    result = await db.execute(
        select(Invoice).where(
            and_(Invoice.id == invoice_id,
                 Invoice.tenant_id == current_user.tenant_id)
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status not in VOID_ALLOWED_STATUSES:
        raise BusinessRuleException(
            "INVALID_STATUS",
            f"Cannot void invoice in status: {invoice.status}. "
            f"Use credit memo for PARTIALLY_PAID/PAID invoices."
        )

    needs_gl_reversal = invoice.status in ["APPROVED", "SENT"]
    now = datetime.utcnow()

    # ── Begin idempotency ──────────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        f"POST /invoices/{invoice_id}/void", request_hash
    )

    journal_entry_id = None

    if needs_gl_reversal:
        # ── Period check ───────────────────────────────────
        period_result = await db.execute(
            select(AccountingPeriod).where(
                and_(
                    AccountingPeriod.tenant_id == current_user.tenant_id,
                    AccountingPeriod.entity_id == current_user.entity_id,
                    AccountingPeriod.status == "OPEN",
                )
            ).order_by(AccountingPeriod.start_date.desc()).limit(1)
        )
        period = period_result.scalar_one_or_none()
        if not period:
            raise PeriodClosedException("No open accounting period found")

        # ── Fetch GL accounts ──────────────────────────────
        gl_result = await db.execute(
            select(GLAccount).where(
                and_(
                    GLAccount.entity_id == current_user.entity_id,
                    GLAccount.account_code.in_([GL_AR, GL_REVENUE, GL_TAX_PAYABLE]),
                )
            )
        )
        gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

        # ── Reverse original GL entry ──────────────────────
        # Dr Revenue      subtotal_amount
        # Dr Tax Payable  tax_amount
        # Cr AR           total_amount
        journal_entry = JournalEntry(
            tenant_id=current_user.tenant_id,
            entity_id=current_user.entity_id,
            period_id=period.id,
            reference_type="VOID",
            reference_id=invoice_id,
            entry_date=now.date(),
            document_date=invoice.invoice_date,  # preserve original date
            description=f"Void invoice {invoice_id}: {reason_code}",
            currency=invoice.transaction_currency,
            created_by=current_user.user_id,
        )
        db.add(journal_entry)
        await db.flush()
        journal_entry_id = journal_entry.id

        # Dr: Revenue reversed
        if GL_REVENUE in gl_accounts:
            db.add(JournalEntryLine(
                tenant_id=current_user.tenant_id,
                journal_entry_id=journal_entry.id,
                gl_account_id=gl_accounts[GL_REVENUE].id,
                description="Revenue reversed on void",
                debit_amount=invoice.subtotal_amount,
                credit_amount=Decimal("0"),
                base_debit_amount=invoice.base_subtotal_amount,
                base_credit_amount=Decimal("0"),
            ))

        # Dr: Tax Payable reversed
        if invoice.tax_amount > 0 and GL_TAX_PAYABLE in gl_accounts:
            db.add(JournalEntryLine(
                tenant_id=current_user.tenant_id,
                journal_entry_id=journal_entry.id,
                gl_account_id=gl_accounts[GL_TAX_PAYABLE].id,
                description="Tax payable reversed on void",
                debit_amount=invoice.tax_amount,
                credit_amount=Decimal("0"),
                base_debit_amount=invoice.base_tax_amount,
                base_credit_amount=Decimal("0"),
            ))

        # Cr: AR removed
        if GL_AR in gl_accounts:
            db.add(JournalEntryLine(
                tenant_id=current_user.tenant_id,
                journal_entry_id=journal_entry.id,
                gl_account_id=gl_accounts[GL_AR].id,
                description="AR removed on void",
                debit_amount=Decimal("0"),
                credit_amount=invoice.total_amount,
                base_debit_amount=Decimal("0"),
                base_credit_amount=invoice.base_total_amount,
            ))

    # ── Update invoice ─────────────────────────────────────
    invoice.status = "VOID"
    invoice.balance_amount = Decimal("0")
    invoice.base_balance_amount = Decimal("0")
    invoice.version += 1
    invoice.updated_at = now

    await db.flush()

    response_body = {
        "invoice_id": invoice_id,
        "status": "VOID",
        "reason_code": reason_code,
        "description": description,
        "void_reference": void_reference,
        "gl_reversal": needs_gl_reversal,
        "journal_entry_id": journal_entry_id,
        "voided_by": current_user.user_id,
        "voided_at": now.isoformat(),
    }

    await complete_idempotency_key(db, x_idempotency_key, current_user.tenant_id, current_user.entity_id, endpoint_name, 200, response_body)
    await db.commit()

    logger.info(f"Invoice {invoice_id} voided: {reason_code}")
    return JSONResponse(status_code=200, content=response_body)
