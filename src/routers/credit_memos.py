import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, require_role
from src.models import (
    Invoice, CreditMemo, JournalEntry, JournalEntryLine,
    GLAccount, AccountingPeriod
)
from src.exceptions import BusinessRuleException, PeriodClosedException
from src.routers.invoices import (
    check_idempotency, create_idempotency_key,
    complete_idempotency_key,
    GL_AR, GL_REVENUE, GL_TAX_PAYABLE
)

logger = logging.getLogger(__name__)
router = APIRouter()

# GL account for bad debt (used in write-off)
GL_BAD_DEBT = "4100"
GL_CUSTOMER_CREDIT = "2100"

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
        Credit AR            outstanding part  <- reduce receivable
        Credit Customer Credit paid part       <- liability/refund due

    Allowed against: APPROVED, SENT, PARTIALLY_PAID, PAID
    Not allowed against: DRAFT, VOID, WRITTEN_OFF
    """
    endpoint_name = f"POST /invoices/{invoice_id}/credit-memos"
    reason_code = payload.get("reason_code", "").upper()
    description = str(payload.get("description") or "")
    amount = Decimal(str(payload.get("amount", 0)))
    include_tax = payload.get("include_tax", False)

    if reason_code not in CM_REASONS:
        raise BusinessRuleException(
            "INVALID_REASON_CODE",
            f"reason_code must be one of: {CM_REASONS}"
        )
    if amount <= 0:
        raise BusinessRuleException("INVALID_AMOUNT", "Credit memo amount must be positive")
    if not isinstance(include_tax, bool):
        raise BusinessRuleException("INVALID_INCLUDE_TAX", "include_tax must be true or false")

    await set_audit_context(db, current_user)

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "invoice_id": invoice_id,
                "amount": str(amount),
                "reason_code": reason_code,
                "description": description,
                "include_tax": include_tax,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        endpoint_name, request_hash
    )
    if existing:
        return JSONResponse(status_code=existing.response_status,
                            content=existing.response_body)

    # ── Fetch invoice ──────────────────────────────────────
    result = await db.execute(
        select(Invoice)
        .where(
            and_(
                Invoice.id == invoice_id,
                Invoice.tenant_id == current_user.tenant_id,
                Invoice.entity_id == current_user.entity_id,
            )
        )
        .with_for_update()
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # A concurrent request can pass the first idempotency read before the
    # winning request commits. Re-check after acquiring the invoice row lock.
    existing = await check_idempotency(
        db,
        x_idempotency_key,
        current_user.tenant_id,
        current_user.entity_id,
        endpoint_name,
        request_hash,
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body,
        )

    if invoice.status not in CM_ALLOWED_STATUSES:
        raise BusinessRuleException(
            "INVALID_STATUS",
            f"Credit memo not allowed against invoice in status: {invoice.status}"
        )

    if invoice.created_by == current_user.user_id:
        raise BusinessRuleException(
            "SOX_VIOLATION",
            "Credit memo approver cannot be the invoice creator",
        )

    # Prior applied credit memos are read while this invoice row is locked, so
    # two concurrent requests cannot reverse more than the original invoice.
    prior_reversal_result = await db.execute(
        select(
            GLAccount.account_code,
            func.sum(JournalEntryLine.debit_amount),
        )
        .select_from(CreditMemo)
        .join(
            JournalEntry,
            and_(
                JournalEntry.reference_type == "CREDIT_MEMO",
                JournalEntry.reference_id == CreditMemo.id,
            ),
        )
        .join(
            JournalEntryLine,
            JournalEntryLine.journal_entry_id == JournalEntry.id,
        )
        .join(GLAccount, GLAccount.id == JournalEntryLine.gl_account_id)
        .where(
            and_(
                CreditMemo.invoice_id == invoice_id,
                CreditMemo.tenant_id == current_user.tenant_id,
                CreditMemo.entity_id == current_user.entity_id,
                CreditMemo.status == "APPLIED",
                GLAccount.account_code.in_([GL_REVENUE, GL_TAX_PAYABLE]),
            )
        )
        .group_by(GLAccount.account_code)
    )
    prior_reversals = {
        account_code: Decimal(str(reversed_amount))
        for account_code, reversed_amount in prior_reversal_result.all()
    }
    remaining_subtotal = max(
        invoice.subtotal_amount - prior_reversals.get(GL_REVENUE, Decimal("0")),
        Decimal("0"),
    )
    remaining_tax = max(
        invoice.tax_amount - prior_reversals.get(GL_TAX_PAYABLE, Decimal("0")),
        Decimal("0"),
    )
    remaining_creditable = remaining_subtotal + remaining_tax

    if amount > remaining_creditable:
        raise BusinessRuleException(
            "AMOUNT_EXCEEDS_CREDITABLE",
            f"Credit memo amount {amount} exceeds remaining creditable amount {remaining_creditable}",
        )

    # Split a tax-inclusive credit using the remaining unreversed composition.
    # A final credit consumes the exact residuals, avoiding rounding drift.
    if include_tax and remaining_creditable > 0:
        if amount == remaining_creditable:
            subtotal_credit = remaining_subtotal
            tax_credit = remaining_tax
        else:
            tax_ratio = remaining_tax / remaining_creditable
            tax_credit = (amount * tax_ratio).quantize(Decimal("0.0001"))
            subtotal_credit = amount - tax_credit
    else:
        tax_credit = Decimal("0")
        subtotal_credit = amount

    if subtotal_credit > remaining_subtotal or tax_credit > remaining_tax:
        raise BusinessRuleException(
            "CREDIT_COMPONENT_EXCEEDED",
            "Credit memo would reverse more revenue or tax than remains on the invoice",
        )

    rate = invoice.exchange_rate or Decimal("1")
    base_credit = (amount * rate).quantize(Decimal("0.0001"))
    base_tax_credit = (tax_credit * rate).quantize(Decimal("0.0001"))
    base_subtotal_credit = base_credit - base_tax_credit

    balance_before = invoice.balance_amount
    base_balance_before = invoice.base_balance_amount
    ar_credit = min(amount, balance_before)
    customer_credit = amount - ar_credit
    if ar_credit == balance_before:
        base_ar_credit = base_balance_before
    else:
        base_ar_credit = min(
            (ar_credit * rate).quantize(Decimal("0.0001")),
            base_balance_before,
        )
    base_customer_credit = base_credit - base_ar_credit
    new_balance = balance_before - ar_credit
    new_base_balance = base_balance_before - base_ar_credit

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
                GLAccount.tenant_id == current_user.tenant_id,
                GLAccount.entity_id == current_user.entity_id,
                GLAccount.account_code.in_([
                    GL_AR,
                    GL_REVENUE,
                    GL_TAX_PAYABLE,
                    GL_CUSTOMER_CREDIT,
                ]),
            )
        )
    )
    gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

    required_accounts = {GL_REVENUE: "Sales Revenue GL account (3100)"}
    if tax_credit > 0:
        required_accounts[GL_TAX_PAYABLE] = "Tax Payable GL account (2200)"
    if ar_credit > 0:
        required_accounts[GL_AR] = "Accounts Receivable GL account (1200)"
    if customer_credit > 0:
        required_accounts[GL_CUSTOMER_CREDIT] = "Customer Credit liability GL account (2100)"
    for account_code, account_label in required_accounts.items():
        if account_code not in gl_accounts:
            raise BusinessRuleException(
                "GL_ACCOUNT_MISSING",
                f"{account_label} not found for entity {current_user.entity_id}",
            )

    # ── Begin idempotency ──────────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        current_user.entity_id,
        endpoint_name, request_hash
    )

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
    # Cr AR            outstanding part  <- reduce receivable
    # Cr Customer Credit paid part       <- liability/refund due
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
    if tax_credit > 0:
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
    if ar_credit > 0:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_AR].id,
            description="AR reduced by credit memo",
            debit_amount=Decimal("0"),
            credit_amount=ar_credit,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_ar_credit,
        ))

    # A credit beyond outstanding AR is money owed back to the customer. Keep
    # it as a liability until refunded or applied to another invoice.
    if customer_credit > 0:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_CUSTOMER_CREDIT].id,
            description="Credit memo held on customer account",
            debit_amount=Decimal("0"),
            credit_amount=customer_credit,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_customer_credit,
        ))

    # ── Update invoice balance ─────────────────────────────
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
        "ar_reduction": str(ar_credit),
        "base_ar_reduction": str(base_ar_credit),
        "customer_credit_amount": str(customer_credit),
        "base_customer_credit_amount": str(base_customer_credit),
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
