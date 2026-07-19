import hashlib
import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.auth import CurrentUser, get_current_user, require_role
from src.models import (
    Invoice, InvoiceLineItem, Customer, AccountingPeriod,
    JournalEntry, JournalEntryLine, GLAccount,
    IdempotencyKey, DeliveryOutbox, AuditLog,
    PaymentAllocation, Payment, CreditMemo
)
from src.schemas import (
    InvoiceCreate, InvoiceResponse, InvoiceApprove,
    InvoiceApproveResponse, LineItemResponse,
    PaymentHistoryItem, CreditMemoHistoryItem, StatusHistoryItem
)
from src.exceptions import (
    BusinessRuleException, PeriodClosedException,
    IdempotencyConflictException, VersionConflictException
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ============================================================
# Payment terms → days mapping
# ============================================================
PAYMENT_TERMS_DAYS = {
    "NET15": 15, "NET30": 30, "NET45": 45,
    "NET60": 60, "NET90": 90, "IMMEDIATE": 0,
}

# ============================================================
# GL Account codes (standard chart of accounts)
# ============================================================
GL_AR = "1200"          # Accounts Receivable
GL_REVENUE = "3100"     # Sales Revenue
GL_TAX_PAYABLE = "2200" # Tax Payable


# ============================================================
# HELPER: Idempotency check
# ============================================================
async def check_idempotency(
    db: AsyncSession,
    key: str,
    tenant_id: str,
    endpoint: str,
    request_hash: str,
) -> Optional[IdempotencyKey]:
    """
    Check if idempotency key exists.
    Returns existing record if found, None if new.
    Raises IdempotencyConflictException on conflicts.
    """
    result = await db.execute(
        select(IdempotencyKey).where(
            and_(
                IdempotencyKey.tenant_id == tenant_id,
                IdempotencyKey.endpoint == endpoint,
                IdempotencyKey.key == key,
            )
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictException(
                "Idempotency key reused with different payload",
                "IDEMPOTENCY_KEY_REUSED"
            )
        if existing.status == "COMPLETED":
            # Safe replay — return cached response
            return existing
        if existing.status == "PROCESSING":
            raise IdempotencyConflictException(
                "Request is already being processed",
                "REQUEST_IN_PROGRESS"
            )
    return None


async def create_idempotency_key(
    db: AsyncSession,
    key: str,
    tenant_id: str,
    endpoint: str,
    request_hash: str,
):
    """Insert idempotency key with PROCESSING status"""
    idem = IdempotencyKey(
        key=key,
        tenant_id=tenant_id,
        endpoint=endpoint,
        status="PROCESSING",
        request_hash=request_hash,
        expires_at=datetime.utcnow() + timedelta(hours=24),
    )
    db.add(idem)
    await db.flush()


async def complete_idempotency_key(
    db: AsyncSession,
    key: str,
    tenant_id: str,
    endpoint: str,
    response_status: int,
    response_body: dict,
):
    """Mark idempotency key as COMPLETED with cached response"""
    result = await db.execute(
        select(IdempotencyKey).where(
            and_(
                IdempotencyKey.tenant_id == tenant_id,
                IdempotencyKey.endpoint == endpoint,
                IdempotencyKey.key == key,
            )
        )
    )
    idem = result.scalar_one()
    idem.status = "COMPLETED"
    idem.response_status = response_status
    idem.response_body = response_body
    idem.updated_at = datetime.utcnow()
    await db.flush()


# ============================================================
# HELPER: Calculate due date from payment terms
# ============================================================
def calculate_due_date(invoice_date: date, payment_terms: str) -> date:
    days = PAYMENT_TERMS_DAYS.get(payment_terms, 30)
    return invoice_date + timedelta(days=days)


# ============================================================
# HELPER: Calculate line item totals
# ============================================================
def calculate_line_totals(line):
    subtotal = line.quantity * line.unit_price
    tax_amount = subtotal * (line.tax_rate / 100)
    total_price = subtotal + tax_amount
    return subtotal, tax_amount, total_price


# ============================================================
# API1: POST /invoices
# FR1 — Invoice creation
# ============================================================
@router.post("/invoices", status_code=status.HTTP_201_CREATED)
async def create_invoice(
    payload: InvoiceCreate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("invoice_creator", "cfo", "system_admin")),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        text("""
            SELECT
                set_config('app.current_user_id', :user_id, true),
                set_config('app.tenant_id', :tenant_id, true)
        """),
        {"user_id": current_user.user_id, "tenant_id": current_user.tenant_id},
    )

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        json.dumps(payload.model_dump(), default=str).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        "POST /invoices", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    # ── Validate customer exists and belongs to tenant ─────
    result = await db.execute(
        select(Customer).where(
            and_(
                Customer.id == payload.customer_id,
                Customer.tenant_id == current_user.tenant_id,
                Customer.is_active == True,
            )
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    # ── Begin ACID transaction ─────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        "POST /invoices", request_hash
    )

    # ── Calculate totals ───────────────────────────────────
    subtotal_total = 0
    tax_total = 0
    line_items_data = []

    for i, line in enumerate(payload.line_items, start=1):
        subtotal, tax_amount, total_price = calculate_line_totals(line)
        subtotal_total += subtotal
        tax_total += tax_amount
        line_items_data.append({
            "line_number": i,
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "total_price": total_price,
        })

    grand_total = subtotal_total + tax_total
    due_date = calculate_due_date(payload.invoice_date, payload.payment_terms)

    # ── Credit limit check ─────────────────────────────────
    # Sum outstanding AR for this customer
    outstanding_result = await db.execute(
        select(text("COALESCE(SUM(balance_amount), 0)")).select_from(Invoice).where(
            and_(
                Invoice.customer_id == payload.customer_id,
                Invoice.tenant_id == current_user.tenant_id,
                Invoice.status.not_in(["PAID", "VOID", "WRITTEN_OFF"]),
            )
        )
    )
    outstanding = outstanding_result.scalar() or 0
    if customer.credit_limit > 0 and (outstanding + grand_total) > customer.credit_limit:
        raise BusinessRuleException(
            "CREDIT_LIMIT_EXCEEDED",
            f"Credit limit of {customer.credit_limit} would be exceeded"
        )

    # ── Get exchange rate ──────────────────────────────────
    # For prototype: use 1.0 if same currency as tenant base
    exchange_rate = 1  # TODO: fetch from exchange_rate table

    # ── Create invoice ─────────────────────────────────────
    invoice = Invoice(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        customer_id=payload.customer_id,
        po_reference=payload.po_reference,
        status="DRAFT",
        transaction_currency=payload.currency,
        exchange_rate=exchange_rate,
        base_currency=payload.currency,  # simplified for prototype
        subtotal_amount=subtotal_total,
        tax_amount=tax_total,
        total_amount=grand_total,
        balance_amount=grand_total,
        base_subtotal_amount=subtotal_total,
        base_tax_amount=tax_total,
        base_total_amount=grand_total,
        invoice_date=payload.invoice_date,
        due_date=due_date,
        payment_terms=payload.payment_terms,
        version=1,
        created_by=current_user.user_id,
    )
    db.add(invoice)
    await db.flush()  # get invoice.id

    # ── Create line items ──────────────────────────────────
    for i, (line, totals) in enumerate(zip(payload.line_items, line_items_data)):
        line_item = InvoiceLineItem(
            tenant_id=current_user.tenant_id,
            invoice_id=invoice.id,
            line_number=totals["line_number"],
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
            subtotal=totals["subtotal"],
            tax_rate=line.tax_rate,
            tax_jurisdiction=line.tax_jurisdiction,
            tax_amount=totals["tax_amount"],
            total_price=totals["total_price"],
        )
        db.add(line_item)

    await db.flush()

    line_items_result = await db.execute(
        select(InvoiceLineItem)
        .where(InvoiceLineItem.invoice_id == invoice.id)
        .order_by(InvoiceLineItem.line_number)
    )
    saved_line_items = line_items_result.scalars().all()

    # ── Build response ─────────────────────────────────────
    response_body = {
        "id": invoice.id,
        "status": invoice.status,
        "version": invoice.version,
        "customer": {"id": customer.id, "name": customer.name},
        "po_reference": invoice.po_reference,
        "invoice_date": str(invoice.invoice_date),
        "due_date": str(invoice.due_date),
        "payment_terms": invoice.payment_terms,
        "currency": invoice.transaction_currency,
        "exchange_rate": str(invoice.exchange_rate),
        "subtotal_amount": str(invoice.subtotal_amount),
        "tax_amount": str(invoice.tax_amount),
        "total_amount": str(invoice.total_amount),
        "balance_amount": str(invoice.balance_amount),
        "created_by": invoice.created_by,
        "approved_by": None,
        "created_at": invoice.created_at.isoformat(),
        "approved_at": None,
        "line_items": [
            {
                "id": li.id,
                "line_number": li.line_number,
                "description": li.description,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "subtotal": str(li.subtotal),
                "tax_rate": str(li.tax_rate),
                "tax_jurisdiction": li.tax_jurisdiction,
                "tax_amount": str(li.tax_amount),
                "total_price": str(li.total_price),
            }
            for li in saved_line_items
        ],
    }

    # ── Complete idempotency key ───────────────────────────
    await complete_idempotency_key(
        db,
        x_idempotency_key,
        current_user.tenant_id,
        "POST /invoices",
        201,
        response_body,
    )
    await db.commit()

    logger.info(f"Invoice {invoice.id} created by {current_user.user_id}")
    return JSONResponse(status_code=201, content=response_body)


# ============================================================
# API2: GET /invoices/{id}
# FR1 — Invoice retrieval with full history
# ============================================================
@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # ── Fetch invoice with line items ──────────────────────
    result = await db.execute(
        select(Invoice)
        .options(selectinload(Invoice.line_items))
        .where(
            and_(
                Invoice.id == invoice_id,
                Invoice.tenant_id == current_user.tenant_id,
            )
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ── Fetch customer ─────────────────────────────────────
    customer_result = await db.execute(
        select(Customer).where(Customer.id == invoice.customer_id)
    )
    customer = customer_result.scalar_one()

    # ── Fetch payment history ──────────────────────────────
    alloc_result = await db.execute(
        select(PaymentAllocation, Payment)
        .join(Payment, Payment.id == PaymentAllocation.payment_id)
        .where(PaymentAllocation.invoice_id == invoice_id)
        .order_by(Payment.payment_date)
    )
    payment_history = [
        {
            "payment_id": alloc.id,
            "payment_reference": payment.payment_reference,
            "payment_date": str(payment.payment_date),
            "payment_method": payment.payment_method,
            "amount_allocated": str(alloc.amount_allocated),
            "currency": payment.transaction_currency,
        }
        for alloc, payment in alloc_result.all()
    ]

    # ── Fetch credit memo history ──────────────────────────
    cm_result = await db.execute(
        select(CreditMemo)
        .where(
            and_(
                CreditMemo.invoice_id == invoice_id,
                CreditMemo.status == "APPLIED",
            )
        )
        .order_by(CreditMemo.applied_at)
    )
    credit_memo_history = [
        {
            "credit_memo_id": cm.id,
            "reason_code": cm.reason_code,
            "amount": str(cm.amount),
            "applied_at": cm.applied_at.isoformat() if cm.applied_at else None,
        }
        for cm in cm_result.scalars().all()
    ]

    # ── Fetch status history from audit log ────────────────
    audit_result = await db.execute(
        select(AuditLog)
        .where(
            and_(
                AuditLog.table_name == "invoice",
                AuditLog.record_id == invoice_id,
                AuditLog.action == "UPDATE",
            )
        )
        .order_by(AuditLog.changed_at)
    )
    status_history = []
    for log in audit_result.scalars().all():
        old_status = (log.old_value or {}).get("status")
        new_status = (log.new_value or {}).get("status")
        if new_status and new_status != old_status:
            status_history.append({
                "status": new_status,
                "changed_by": str(log.changed_by) if log.changed_by else "system",
                "changed_at": log.changed_at.isoformat(),
            })

    # ── Build response ─────────────────────────────────────
    response_body = {
        "id": invoice.id,
        "status": invoice.status,
        "version": invoice.version,
        "customer": {"id": customer.id, "name": customer.name},
        "po_reference": invoice.po_reference,
        "invoice_date": str(invoice.invoice_date),
        "due_date": str(invoice.due_date),
        "payment_terms": invoice.payment_terms,
        "currency": invoice.transaction_currency,
        "exchange_rate": str(invoice.exchange_rate),
        "subtotal_amount": str(invoice.subtotal_amount),
        "tax_amount": str(invoice.tax_amount),
        "total_amount": str(invoice.total_amount),
        "balance_amount": str(invoice.balance_amount),
        "line_items": [
            {
                "id": li.id,
                "line_number": li.line_number,
                "description": li.description,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "subtotal": str(li.subtotal),
                "tax_rate": str(li.tax_rate),
                "tax_jurisdiction": li.tax_jurisdiction,
                "tax_amount": str(li.tax_amount),
                "total_price": str(li.total_price),
            }
            for li in sorted(invoice.line_items, key=lambda x: x.line_number)
        ],
        "payment_history": payment_history,
        "credit_memo_history": credit_memo_history,
        "status_history": status_history,
        "created_by": invoice.created_by,
        "approved_by": invoice.approved_by,
        "created_at": invoice.created_at.isoformat(),
        "approved_at": invoice.approved_at.isoformat() if invoice.approved_at else None,
    }

    # ── ETag = version number ──────────────────────────────
    return JSONResponse(
        status_code=200,
        content=response_body,
        headers={"ETag": str(invoice.version)},
    )


# ============================================================
# API3: POST /invoices/{id}/approve
# FR2 — Invoice approval with GL entry generation
# ============================================================
@router.post("/invoices/{invoice_id}/approve")
async def approve_invoice(
    invoice_id: str,
    payload: InvoiceApprove,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("invoice_approver", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    # Must be the first DB statement in this transaction.
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    await db.execute(
        text("""
            SELECT
                set_config('app.current_user_id', :user_id, true),
                set_config('app.tenant_id', :tenant_id, true)
        """),
        {"user_id": current_user.user_id, "tenant_id": current_user.tenant_id},
    )

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        f"{invoice_id}:{current_user.user_id}".encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        f"POST /invoices/{invoice_id}/approve", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    # ── Fetch invoice with Repeatable Read ─────────────────
    result = await db.execute(
        select(Invoice).where(
            and_(
                Invoice.id == invoice_id,
                Invoice.tenant_id == current_user.tenant_id,
            )
        )
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ── Optimistic locking — ABA prevention ───────────────
    if str(invoice.version) != if_match:
        raise VersionConflictException(
            "Invoice was modified since last viewed. Please refresh."
        )

    # ── Status check ───────────────────────────────────────
    if invoice.status != "DRAFT":
        raise BusinessRuleException(
            "INVALID_STATUS",
            f"Cannot approve invoice with status: {invoice.status}"
        )

    # ── SOX: creator ≠ approver ────────────────────────────
    if invoice.created_by == current_user.user_id:
        raise BusinessRuleException(
            "SOX_VIOLATION",
            "Invoice creator cannot approve their own invoice"
        )

    # ── Period close check ─────────────────────────────────
    period_result = await db.execute(
        select(AccountingPeriod).where(
            and_(
                AccountingPeriod.tenant_id == current_user.tenant_id,
                AccountingPeriod.entity_id == current_user.entity_id,
                AccountingPeriod.start_date <= invoice.invoice_date,
                AccountingPeriod.end_date >= invoice.invoice_date,
            )
        )
    )
    period = period_result.scalar_one_or_none()
    if not period:
        raise PeriodClosedException(
            f"No accounting period found for invoice date {invoice.invoice_date}"
        )
    if period.status != "OPEN":
        raise PeriodClosedException(
            f"Period {period.period_name} is {period.status}. Cannot post."
        )

    # ── Begin idempotency key ──────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        f"POST /invoices/{invoice_id}/approve", request_hash
    )

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

    if GL_AR not in gl_accounts:
        raise BusinessRuleException("GL_ACCOUNT_MISSING", "AR GL account (1200) not found")
    if GL_REVENUE not in gl_accounts:
        raise BusinessRuleException("GL_ACCOUNT_MISSING", "Revenue GL account (3100) not found")
    if GL_TAX_PAYABLE not in gl_accounts:
        raise BusinessRuleException("GL_ACCOUNT_MISSING", "Tax Payable GL account (2200) not found")

    # ── Generate GL journal entry ──────────────────────────
    # Debit  AR            total_amount
    # Credit Revenue       subtotal_amount
    # Credit Tax Payable   tax_amount
    now = datetime.utcnow()
    journal_entry = JournalEntry(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        period_id=period.id,
        reference_type="INVOICE",
        reference_id=invoice.id,
        document_date=invoice.invoice_date,
        entry_date=invoice.invoice_date,
        description=f"Invoice {invoice.id} approved",
        currency=invoice.transaction_currency,
        created_by=current_user.user_id,
    )
    db.add(journal_entry)
    await db.flush()

    # DR: Accounts Receivable
    db.add(JournalEntryLine(
        tenant_id=current_user.tenant_id,
        journal_entry_id=journal_entry.id,
        gl_account_id=gl_accounts[GL_AR].id,
        description="Accounts Receivable",
        debit_amount=invoice.total_amount,
        credit_amount=0,
        base_debit_amount=invoice.base_total_amount,
        base_credit_amount=0,
    ))

    # CR: Sales Revenue
    db.add(JournalEntryLine(
        tenant_id=current_user.tenant_id,
        journal_entry_id=journal_entry.id,
        gl_account_id=gl_accounts[GL_REVENUE].id,
        description="Sales Revenue",
        debit_amount=0,
        credit_amount=invoice.subtotal_amount,
        base_debit_amount=0,
        base_credit_amount=invoice.base_subtotal_amount,
    ))

    # CR: Tax Payable
    if invoice.tax_amount > 0:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_TAX_PAYABLE].id,
            description="Tax Payable",
            debit_amount=0,
            credit_amount=invoice.tax_amount,
            base_debit_amount=0,
            base_credit_amount=invoice.base_tax_amount,
        ))

    # ── Update invoice status ──────────────────────────────
    invoice.status = "APPROVED"
    invoice.approved_by = current_user.user_id
    invoice.approved_at = now
    invoice.period_id = period.id
    invoice.version += 1
    invoice.updated_at = now

    # The outbox event commits atomically with approval and its GL entry.
    # Delivery itself happens after commit in a separate worker.
    customer_result = await db.execute(
        select(Customer).where(
            and_(
                Customer.id == invoice.customer_id,
                Customer.tenant_id == current_user.tenant_id,
            )
        )
    )
    customer = customer_result.scalar_one()
    delivery_event = DeliveryOutbox(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        invoice_id=invoice.id,
        event_type="INVOICE_APPROVED",
        payload={
            "invoice_id": str(invoice.id),
            "customer_id": str(invoice.customer_id),
            "customer_email": customer.email,
            "tenant_id": str(current_user.tenant_id),
            "total_amount": str(invoice.total_amount),
            "currency": invoice.transaction_currency,
            "due_date": str(invoice.due_date),
        },
    )
    db.add(delivery_event)

    await db.flush()

    # ── Build response ─────────────────────────────────────
    response_body = {
        "id": invoice.id,
        "status": invoice.status,
        "version": invoice.version,
        "approved_by": invoice.approved_by,
        "approved_at": invoice.approved_at.isoformat(),
        "notes": payload.notes,
        "journal_entry_id": journal_entry.id,
        "delivery_status": "QUEUED",
    }

    await complete_idempotency_key(
        db,
        x_idempotency_key,
        current_user.tenant_id,
        f"POST /invoices/{invoice_id}/approve",
        200,
        response_body,
    )
    await db.commit()

    logger.info(
        f"Invoice {invoice.id} approved by {current_user.user_id}, "
        f"GL entry {journal_entry.id} created"
    )
    return JSONResponse(status_code=200, content=response_body)
