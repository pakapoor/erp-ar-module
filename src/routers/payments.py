import hashlib
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, require_role
from src.models import (
    Invoice, Payment, PaymentAllocation,
    JournalEntry, JournalEntryLine, GLAccount,
    IdempotencyKey, Customer, ExchangeRate,
)
from src.schemas import PaymentCreate
from src.exceptions import (
    BusinessRuleException,
    IdempotencyConflictException,
)
from src.routers.invoices import (
    check_idempotency,
    create_idempotency_key,
    complete_idempotency_key,
    GL_AR,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# GL account codes for payment
GL_CASH = "1100"          # Cash / Bank
GL_FX_GAIN_LOSS = "4300"  # FX Gain/Loss

# Invoice statuses that can receive payment
PAYABLE_STATUSES = ["APPROVED", "SENT", "PARTIALLY_PAID"]


# ============================================================
# HELPER: Get exchange rate for date
# Falls back to most recent rate if exact date not found
# ============================================================
async def get_exchange_rate(
    db: AsyncSession,
    tenant_id: str,
    from_currency: str,
    to_currency: str,
    rate_date,
) -> tuple[Decimal, str, str | None]:
    """
    Returns (rate, rate_date_used, warning_message)
    """
    if from_currency == to_currency:
        return Decimal("1"), str(rate_date), None

    # Try exact date first
    result = await db.execute(
        select(ExchangeRate).where(
            and_(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
                ExchangeRate.effective_date == rate_date,
            )
        )
    )
    rate_record = result.scalar_one_or_none()

    if rate_record:
        return rate_record.rate, str(rate_date), None

    # Fall back to most recent rate
    result = await db.execute(
        select(ExchangeRate).where(
            and_(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
                ExchangeRate.effective_date < rate_date,
            )
        ).order_by(ExchangeRate.effective_date.desc()).limit(1)
    )
    rate_record = result.scalar_one_or_none()

    if rate_record:
        warning = (
            f"Rate from {rate_record.effective_date} used "
            f"({rate_date} not available)"
        )
        return rate_record.rate, str(rate_record.effective_date), warning

    # No rate found — use 1.0 with warning
    warning = f"No exchange rate found for {from_currency}→{to_currency}. Using 1.0"
    return Decimal("1"), str(rate_date), warning


# ============================================================
# HELPER: AUTO allocation (FIFO — oldest due date first)
# ============================================================
async def auto_allocate(
    db: AsyncSession,
    tenant_id: str,
    customer_id: str,
    payment_amount: Decimal,
) -> List[dict]:
    """
    Allocate payment to invoices oldest due date first.
    Returns list of allocation dicts.
    """
    # Fetch open invoices ordered by due_date ASC (oldest first)
    result = await db.execute(
        select(Invoice).where(
            and_(
                Invoice.tenant_id == tenant_id,
                Invoice.customer_id == customer_id,
                Invoice.status.in_(PAYABLE_STATUSES),
                Invoice.balance_amount > 0,
            )
        ).order_by(Invoice.due_date.asc())
    )
    invoices = result.scalars().all()

    if not invoices:
        raise BusinessRuleException(
            "NO_OPEN_INVOICES",
            "No open invoices found for this customer"
        )

    allocations = []
    remaining = payment_amount

    for invoice in invoices:
        if remaining <= 0:
            break

        allocate = min(remaining, invoice.balance_amount)
        allocations.append({
            "invoice": invoice,
            "amount": allocate,
        })
        remaining -= allocate

    return allocations, remaining


# ============================================================
# HELPER: MANUAL allocation
# ============================================================
async def manual_allocate(
    db: AsyncSession,
    tenant_id: str,
    customer_id: str,
    payment_amount: Decimal,
    allocation_items: list,
) -> List[dict]:
    """
    Allocate payment per client-specified amounts.
    Validates each invoice exists and belongs to customer.
    """
    allocations = []
    remaining = payment_amount

    for item in allocation_items:
        # Fetch and lock invoice
        result = await db.execute(
            select(Invoice).where(
                and_(
                    Invoice.id == item.invoice_id,
                    Invoice.tenant_id == tenant_id,
                    Invoice.customer_id == customer_id,
                    Invoice.status.in_(PAYABLE_STATUSES),
                )
            ).with_for_update()  # lock row for serializable
        )
        invoice = result.scalar_one_or_none()

        if not invoice:
            raise BusinessRuleException(
                "INVOICE_NOT_FOUND",
                f"Invoice {item.invoice_id} not found or not payable"
            )

        if item.amount > invoice.balance_amount:
            raise BusinessRuleException(
                "ALLOCATION_EXCEEDS_BALANCE",
                f"Allocation {item.amount} exceeds invoice balance {invoice.balance_amount}"
            )

        allocations.append({
            "invoice": invoice,
            "amount": Decimal(str(item.amount)),
        })
        remaining -= Decimal(str(item.amount))

    return allocations, remaining


# ============================================================
# API4: POST /payments
# FR4 — Payment recording and allocation
# ============================================================
@router.post("/payments", status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("payment_recorder", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        json.dumps(payload.model_dump(), default=str).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id,
        "POST /payments", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    # ── SERIALIZABLE isolation for payment allocation ──────
    # Prevents double allocation if two payments hit same invoice
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))

    # ── Validate customer ──────────────────────────────────
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

    # ── Get exchange rate ──────────────────────────────────
    exchange_rate, rate_date_used, fx_warning = await get_exchange_rate(
        db,
        current_user.tenant_id,
        payload.currency,
        payload.currency,  # simplified: same currency for prototype
        payload.payment_date,
    )

    base_amount = Decimal(str(payload.amount)) * exchange_rate

    # ── Begin idempotency key ──────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id,
        "POST /payments", request_hash
    )

    # ── Allocate payment ───────────────────────────────────
    payment_amount = Decimal(str(payload.amount))

    if payload.allocation_mode == "AUTO":
        allocations, remaining = await auto_allocate(
            db, current_user.tenant_id, payload.customer_id, payment_amount
        )
    else:
        allocations, remaining = await manual_allocate(
            db, current_user.tenant_id, payload.customer_id,
            payment_amount, payload.allocations
        )

    overpayment_amount = max(remaining, Decimal("0"))

    # ── Create payment record ──────────────────────────────
    allocated_total = sum(a["amount"] for a in allocations)
    payment = Payment(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        customer_id=payload.customer_id,
        payment_reference=payload.payment_reference,
        payment_date=payload.payment_date,
        transaction_currency=payload.currency,
        exchange_rate=exchange_rate,
        base_currency=payload.currency,
        amount=payment_amount,
        base_amount=base_amount,
        allocated_amount=allocated_total,
        unallocated_amount=overpayment_amount,
        payment_method=payload.payment_method,
        status="APPLIED" if overpayment_amount == 0 else "PARTIALLY_APPLIED",
        allocation_mode=payload.allocation_mode,
        idempotency_key=x_idempotency_key,
        created_by=current_user.user_id,
    )
    db.add(payment)
    await db.flush()

    # ── Apply allocations + update invoice balances ────────
    allocation_results = []
    for alloc in allocations:
        invoice = alloc["invoice"]
        amount = alloc["amount"]

        balance_before = invoice.balance_amount
        balance_after = balance_before - amount

        # Calculate FX gain/loss
        # Invoice rate vs payment rate → difference
        fx_gain_loss = Decimal("0")
        if invoice.exchange_rate != exchange_rate:
            fx_gain_loss = amount * (exchange_rate - invoice.exchange_rate)

        # Create payment allocation record
        pa = PaymentAllocation(
            tenant_id=current_user.tenant_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount_allocated=amount,
            fx_gain_loss=fx_gain_loss,
            created_by=current_user.user_id,
        )
        db.add(pa)

        # Update invoice balance and status
        invoice.balance_amount = balance_after
        invoice.updated_at = datetime.utcnow()

        if balance_after == 0:
            invoice.status = "PAID"
        elif balance_after < invoice.total_amount:
            invoice.status = "PARTIALLY_PAID"

        invoice.version += 1

        allocation_results.append({
            "invoice_id": invoice.id,
            "amount_allocated": str(amount),
            "invoice_balance_before": str(balance_before),
            "invoice_balance_after": str(balance_after),
            "invoice_status": invoice.status,
        })

    await db.flush()

    # ── Fetch GL accounts ──────────────────────────────────
    gl_result = await db.execute(
        select(GLAccount).where(
            and_(
                GLAccount.entity_id == current_user.entity_id,
                GLAccount.account_code.in_([GL_CASH, GL_AR, GL_FX_GAIN_LOSS]),
            )
        )
    )
    gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

    # ── Generate GL journal entry ──────────────────────────
    # One GL entry for entire payment:
    # Debit  Cash        total_payment
    # Credit AR          total_payment (- FX adjustment if any)
    # Credit/Debit FX Gain/Loss (if multi-currency)
    total_fx = sum(
        Decimal(str(a["amount_allocated"])) *
        (exchange_rate - alloc["invoice"].exchange_rate)
        for a, alloc in zip(allocation_results, allocations)
        if alloc["invoice"].exchange_rate != exchange_rate
    )

    journal_entry = JournalEntry(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        reference_type="PAYMENT",
        reference_id=payment.id,
        entry_date=payload.payment_date,
        description=f"Payment {payload.payment_reference} from customer {payload.customer_id}",
        currency=payload.currency,
        created_by=current_user.user_id,
    )
    db.add(journal_entry)
    await db.flush()

    # DR: Cash
    if GL_CASH in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_CASH].id,
            description="Cash received",
            debit_amount=payment_amount,
            credit_amount=Decimal("0"),
            base_debit_amount=base_amount,
            base_credit_amount=Decimal("0"),
        ))

    # CR: AR (at original invoice rates)
    ar_credit = allocated_total - total_fx
    if GL_AR in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_AR].id,
            description="Accounts Receivable cleared",
            debit_amount=Decimal("0"),
            credit_amount=ar_credit,
            base_debit_amount=Decimal("0"),
            base_credit_amount=ar_credit,
        ))

    # CR/DR: FX Gain/Loss (if applicable)
    if total_fx != 0 and GL_FX_GAIN_LOSS in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_FX_GAIN_LOSS].id,
            description="FX Gain/Loss",
            debit_amount=max(-total_fx, Decimal("0")),
            credit_amount=max(total_fx, Decimal("0")),
            base_debit_amount=max(-total_fx, Decimal("0")),
            base_credit_amount=max(total_fx, Decimal("0")),
        ))

    await db.flush()

    # ── Build response ─────────────────────────────────────
    response_body = {
        "id": payment.id,
        "idempotency_key": x_idempotency_key,
        "status": payment.status,
        "customer_id": payload.customer_id,
        "payment_reference": payload.payment_reference,
        "payment_date": str(payload.payment_date),
        "amount": str(payment_amount),
        "currency": payload.currency,
        "payment_method": payload.payment_method,
        "allocation_mode": payload.allocation_mode,
        "allocations": allocation_results,
        "allocated_amount": str(allocated_total),
        "unallocated_amount": str(overpayment_amount),
        "overpayment_amount": str(overpayment_amount),
        "overpayment_action": "ON_ACCOUNT" if overpayment_amount > 0 else None,
        "exchange_rate_used": str(exchange_rate),
        "exchange_rate_date": rate_date_used,
        "exchange_rate_warning": fx_warning,
        "journal_entry_id": journal_entry.id,
        "created_at": payment.created_at.isoformat(),
    }

    await complete_idempotency_key(db, x_idempotency_key, 201, response_body)
    await db.commit()

    logger.info(
        f"Payment {payment.id} recorded: {payload.amount} {payload.currency} "
        f"from customer {payload.customer_id}, "
        f"allocated to {len(allocation_results)} invoices"
    )
    return JSONResponse(status_code=201, content=response_body)
