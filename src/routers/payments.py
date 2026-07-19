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
    IdempotencyKey, Customer, Entity, ExchangeRate, AccountingPeriod,
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
    convert_to_base,
    FX_RATE_MAX_AGE_DAYS,
    GL_AR,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# GL account codes for payment
GL_CASH = "1100"          # Cash / Bank
GL_CUSTOMER_CREDIT = "2100"  # Unapplied cash / customer overpayment liability
GL_FX_GAIN_LOSS = "4300"  # FX Gain/Loss

# Invoice statuses that can receive payment
PAYABLE_STATUSES = ["APPROVED", "SENT", "PARTIALLY_PAID"]


# ============================================================
# HELPER: Get the latest approved, fresh exchange rate for a payment date
# ============================================================
async def get_exchange_rate(
    db: AsyncSession,
    tenant_id: str,
    from_currency: str,
    to_currency: str,
    rate_date,
) -> tuple[str | None, Decimal, str, str | None]:
    """
    Returns (rate_id, rate, rate_date_used, warning_message).
    Missing or stale foreign rates fail closed.
    """
    if from_currency == to_currency:
        return None, Decimal("1"), str(rate_date), None

    result = await db.execute(
        select(ExchangeRate)
        .where(
            and_(
                ExchangeRate.tenant_id == tenant_id,
                ExchangeRate.from_currency == from_currency,
                ExchangeRate.to_currency == to_currency,
                ExchangeRate.rate_type == "DAILY_REFERENCE",
                ExchangeRate.status == "APPROVED",
                ExchangeRate.effective_date <= rate_date,
                ExchangeRate.effective_date >= (
                    rate_date - timedelta(days=FX_RATE_MAX_AGE_DAYS)
                ),
            )
        )
        .order_by(ExchangeRate.effective_date.desc(), ExchangeRate.approved_at.desc())
        .limit(1)
    )
    rate_record = result.scalar_one_or_none()

    if not rate_record:
        raise HTTPException(
            status_code=503,
            detail=(
                "FX_RATE_UNAVAILABLE: no approved "
                f"{from_currency}/{to_currency} rate on or before {rate_date} "
                f"within the {FX_RATE_MAX_AGE_DAYS}-day freshness limit"
            ),
        )

    warning = None
    if rate_record.effective_date != rate_date:
        warning = (
            f"Rate from {rate_record.effective_date} used "
            f"({rate_date} not available)"
        )
    return (
        rate_record.id,
        rate_record.rate,
        str(rate_record.effective_date),
        warning,
    )


# ============================================================
# HELPER: AUTO allocation (FIFO — oldest due date first)
# ============================================================
async def auto_allocate(
    db: AsyncSession,
    tenant_id: str,
    entity_id: str,
    customer_id: str,
    payment_amount: Decimal,
    payment_currency: str,
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
                Invoice.entity_id == entity_id,
                Invoice.customer_id == customer_id,
                Invoice.transaction_currency == payment_currency,
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
    entity_id: str,
    customer_id: str,
    payment_amount: Decimal,
    payment_currency: str,
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
                    Invoice.entity_id == entity_id,
                    Invoice.customer_id == customer_id,
                    Invoice.transaction_currency == payment_currency,
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
    # Must be the first DB statement in this transaction.
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
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

    # ── Idempotency check ──────────────────────────────────
    request_hash = hashlib.sha256(
        json.dumps(payload.model_dump(), default=str).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        "POST /payments", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    # ── Validate customer ──────────────────────────────────
    result = await db.execute(
        select(Customer).where(
            and_(
                Customer.id == payload.customer_id,
                Customer.tenant_id == current_user.tenant_id,
                Customer.entity_id == current_user.entity_id,
                Customer.is_active == True,
            )
        )
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    entity_currency_result = await db.execute(
        select(Entity.currency).where(
            and_(
                Entity.id == current_user.entity_id,
                Entity.tenant_id == current_user.tenant_id,
            )
        )
    )
    base_currency = entity_currency_result.scalar_one()

    # ── Payment posting period must be OPEN ────────────────
    period_result = await db.execute(
        select(AccountingPeriod).where(
            and_(
                AccountingPeriod.tenant_id == current_user.tenant_id,
                AccountingPeriod.entity_id == current_user.entity_id,
                AccountingPeriod.start_date <= payload.payment_date,
                AccountingPeriod.end_date >= payload.payment_date,
                AccountingPeriod.status == "OPEN",
            )
        )
    )
    period = period_result.scalar_one_or_none()
    if not period:
        raise BusinessRuleException(
            "PERIOD_NOT_OPEN",
            f"No OPEN accounting period for payment date {payload.payment_date}",
        )

    # ── Get exchange rate ──────────────────────────────────
    exchange_rate_id, exchange_rate, rate_date_used, fx_warning = await get_exchange_rate(
        db,
        current_user.tenant_id,
        payload.currency,
        base_currency,
        payload.payment_date,
    )

    # ── Begin idempotency key ──────────────────────────────
    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        "POST /payments", request_hash
    )

    # ── Allocate payment ───────────────────────────────────
    payment_amount = Decimal(str(payload.amount))

    if payload.allocation_mode == "AUTO":
        allocations, remaining = await auto_allocate(
            db, current_user.tenant_id, current_user.entity_id,
            payload.customer_id, payment_amount, payload.currency
        )
    else:
        allocations, remaining = await manual_allocate(
            db, current_user.tenant_id, current_user.entity_id, payload.customer_id,
            payment_amount, payload.currency, payload.allocations
        )

    overpayment_amount = max(remaining, Decimal("0"))

    # ── Create payment record ──────────────────────────────
    allocated_total = sum(a["amount"] for a in allocations)
    base_amount = convert_to_base(payment_amount, exchange_rate)
    base_unallocated_total = convert_to_base(overpayment_amount, exchange_rate)

    # Convert allocations separately for audit, then put any rounding residual
    # on the final allocation so the base parts exactly equal the Cash debit.
    allocation_base_payments = [
        convert_to_base(allocation["amount"], exchange_rate)
        for allocation in allocations
    ]
    target_base_allocated = base_amount - base_unallocated_total
    if allocation_base_payments:
        allocation_base_payments[-1] += (
            target_base_allocated - sum(allocation_base_payments)
        )
    base_allocated_total = sum(allocation_base_payments, Decimal("0"))
    payment = Payment(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        customer_id=payload.customer_id,
        payment_reference=payload.payment_reference,
        payment_date=payload.payment_date,
        transaction_currency=payload.currency,
        exchange_rate_id=exchange_rate_id,
        exchange_rate=exchange_rate,
        base_currency=base_currency,
        amount=payment_amount,
        base_amount=base_amount,
        allocated_amount=allocated_total,
        unallocated_amount=overpayment_amount,
        base_allocated_amount=base_allocated_total,
        base_unallocated_amount=base_unallocated_total,
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
    for allocation_index, alloc in enumerate(allocations):
        invoice = alloc["invoice"]
        amount = alloc["amount"]

        balance_before = invoice.balance_amount
        balance_after = balance_before - amount

        base_payment_amount = allocation_base_payments[allocation_index]
        # A final payment must clear the exact stored AR carrying value. This
        # avoids a one-unit rounding residue when the invoice total was built
        # from separately rounded revenue and tax components.
        if amount == balance_before:
            base_ar_amount = invoice.base_balance_amount
        else:
            base_ar_amount = min(
                convert_to_base(amount, invoice.exchange_rate),
                invoice.base_balance_amount,
            )
        base_balance_after = invoice.base_balance_amount - base_ar_amount

        # Calculate FX gain/loss
        # Positive = realized gain; negative = realized loss.
        fx_gain_loss = base_payment_amount - base_ar_amount

        # Create payment allocation record
        pa = PaymentAllocation(
            tenant_id=current_user.tenant_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount_allocated=amount,
            base_payment_amount=base_payment_amount,
            base_ar_amount=base_ar_amount,
            fx_gain_loss=fx_gain_loss,
            created_by=current_user.user_id,
        )
        db.add(pa)

        # Update invoice balance and status
        invoice.balance_amount = balance_after
        invoice.base_balance_amount = base_balance_after
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
            "base_payment_amount": str(base_payment_amount),
            "base_ar_amount": str(base_ar_amount),
            "fx_gain_loss": str(fx_gain_loss),
        })

    await db.flush()

    base_ar_credit = sum(
        (Decimal(result["base_ar_amount"]) for result in allocation_results),
        Decimal("0"),
    )
    total_fx = sum(
        (Decimal(result["fx_gain_loss"]) for result in allocation_results),
        Decimal("0"),
    )

    # ── Fetch GL accounts ──────────────────────────────────
    gl_result = await db.execute(
        select(GLAccount).where(
            and_(
                GLAccount.entity_id == current_user.entity_id,
                GLAccount.tenant_id == current_user.tenant_id,
                GLAccount.account_code.in_([
                    GL_CASH, GL_AR, GL_CUSTOMER_CREDIT, GL_FX_GAIN_LOSS,
                ]),
            )
        )
    )
    gl_accounts = {gl.account_code: gl for gl in gl_result.scalars().all()}

    required_accounts = {
        GL_CASH: "Cash GL account (1100)",
        GL_AR: "AR GL account (1200)",
    }
    if overpayment_amount > 0:
        required_accounts[GL_CUSTOMER_CREDIT] = (
            "Customer Credit liability GL account (2100)"
        )
    if total_fx != 0:
        required_accounts[GL_FX_GAIN_LOSS] = "FX Gain/Loss GL account (4300)"
    for account_code, account_label in required_accounts.items():
        if account_code not in gl_accounts:
            raise BusinessRuleException(
                "GL_ACCOUNT_MISSING",
                f"{account_label} not found for entity {current_user.entity_id}",
            )

    # ── Generate GL journal entry ──────────────────────────
    # One GL entry for entire payment:
    # Debit  Cash        total_payment
    # Credit AR          allocated amount (- FX adjustment if any)
    # Credit Customer Credit unapplied overpayment (liability)
    # Credit/Debit FX Gain/Loss (if multi-currency)
    journal_entry = JournalEntry(
        tenant_id=current_user.tenant_id,
        entity_id=current_user.entity_id,
        period_id=period.id,
        reference_type="PAYMENT",
        reference_id=payment.id,
        document_date=payload.payment_date,
        entry_date=payload.payment_date,
        description=f"Payment {payload.payment_reference} from customer {payload.customer_id}",
        currency=payload.currency,
        created_by=current_user.user_id,
    )
    db.add(journal_entry)
    await db.flush()

    # DR: Cash
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

    # CR: AR at each invoice's original carrying value. Transaction fields stay
    # in document currency; base fields hold the INR legal-book values.
    if allocated_total > 0:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_AR].id,
            description="Accounts Receivable cleared",
            debit_amount=Decimal("0"),
            credit_amount=allocated_total,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_ar_credit,
        ))

    # CR: Customer Credit — unapplied receipt remains a liability until it is
    # refunded or allocated to a future invoice.
    if overpayment_amount > 0:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_CUSTOMER_CREDIT].id,
            description="Customer overpayment held on account",
            debit_amount=Decimal("0"),
            credit_amount=overpayment_amount,
            base_debit_amount=Decimal("0"),
            base_credit_amount=base_unallocated_total,
        ))

    # CR/DR: FX Gain/Loss (if applicable)
    if total_fx != 0 and GL_FX_GAIN_LOSS in gl_accounts:
        db.add(JournalEntryLine(
            tenant_id=current_user.tenant_id,
            journal_entry_id=journal_entry.id,
            gl_account_id=gl_accounts[GL_FX_GAIN_LOSS].id,
            description="FX Gain/Loss",
            # The realized difference exists only in base currency. Putting
            # INR into these transaction columns would corrupt the USD view.
            debit_amount=Decimal("0"),
            credit_amount=Decimal("0"),
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
        "base_amount": str(base_amount),
        "base_currency": base_currency,
        "payment_method": payload.payment_method,
        "allocation_mode": payload.allocation_mode,
        "allocations": allocation_results,
        "allocated_amount": str(allocated_total),
        "unallocated_amount": str(overpayment_amount),
        "overpayment_amount": str(overpayment_amount),
        "overpayment_action": "ON_ACCOUNT" if overpayment_amount > 0 else None,
        "exchange_rate_id": exchange_rate_id,
        "exchange_rate_used": str(exchange_rate),
        "exchange_rate_date": rate_date_used,
        "exchange_rate_warning": fx_warning,
        "realized_fx_gain_loss": str(total_fx),
        "journal_entry_id": journal_entry.id,
        "created_at": payment.created_at.isoformat(),
    }

    await complete_idempotency_key(
        db,
        x_idempotency_key,
        current_user.tenant_id,
        current_user.entity_id,
        "POST /payments",
        201,
        response_body,
    )
    await db.commit()

    logger.info(
        f"Payment {payment.id} recorded: {payload.amount} {payload.currency} "
        f"from customer {payload.customer_id}, "
        f"allocated to {len(allocation_results)} invoices"
    )
    return JSONResponse(status_code=201, content=response_body)
