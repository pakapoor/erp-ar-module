from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator


SUPPORTED_CURRENCIES = {"INR", "USD", "EUR", "CNY", "GBP", "JPY", "CHF", "CAD"}


def normalize_supported_currency(value: str) -> str:
    currency = value.upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"unsupported currency; expected one of {', '.join(sorted(SUPPORTED_CURRENCIES))}"
        )
    return currency


# ============================================================
# INVOICE LINE ITEM SCHEMAS
# ============================================================

class LineItemCreate(BaseModel):
    """What client sends for each line item"""
    description: str = Field(..., min_length=1, max_length=500)
    quantity: Decimal = Field(..., gt=0)
    unit_price: Decimal = Field(..., ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    tax_jurisdiction: Optional[str] = Field(default=None, max_length=20)


class LineItemResponse(BaseModel):
    """What server returns for each line item"""
    id: str
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    subtotal: Decimal
    tax_rate: Decimal
    tax_jurisdiction: Optional[str]
    tax_amount: Decimal
    total_price: Decimal

    model_config = {"from_attributes": True}


# ============================================================
# INVOICE SCHEMAS
# ============================================================

class InvoiceCreate(BaseModel):
    """POST /invoices — what client sends"""
    customer_id: str
    po_reference: Optional[str] = None
    invoice_date: date = Field(default_factory=date.today)
    payment_terms: str = Field(default="NET30")
    currency: str = Field(..., min_length=3, max_length=3)
    line_items: List[LineItemCreate] = Field(..., min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return normalize_supported_currency(value)

    # NOT in request body — server derives these:
    # tenant_id    ← from JWT
    # entity_id    ← from JWT
    # status       ← always DRAFT
    # total_amount ← calculated
    # due_date     ← invoice_date + payment_terms


class PaymentHistoryItem(BaseModel):
    payment_id: str
    payment_reference: str
    payment_date: date
    payment_method: str
    amount_allocated: Decimal
    currency: str


class CreditMemoHistoryItem(BaseModel):
    credit_memo_id: str
    reason_code: str
    amount: Decimal
    applied_at: Optional[datetime]


class StatusHistoryItem(BaseModel):
    status: str
    changed_by: str
    changed_at: datetime


class InvoiceResponse(BaseModel):
    """GET /invoices/{id} and POST /invoices — what server returns"""
    id: str
    status: str
    version: int
    customer: dict  # {id, name}
    po_reference: Optional[str]
    invoice_date: date
    due_date: date
    payment_terms: str
    currency: str
    base_currency: str
    exchange_rate_id: Optional[str]
    exchange_rate: Decimal
    line_items: List[LineItemResponse]
    subtotal_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    balance_amount: Decimal
    base_subtotal_amount: Decimal
    base_tax_amount: Decimal
    base_total_amount: Decimal
    base_balance_amount: Decimal
    # Only in GET response (not in create response)
    payment_history: Optional[List[PaymentHistoryItem]] = None
    credit_memo_history: Optional[List[CreditMemoHistoryItem]] = None
    status_history: Optional[List[StatusHistoryItem]] = None
    created_by: str
    approved_by: Optional[str]
    created_at: datetime
    approved_at: Optional[datetime]

    model_config = {"from_attributes": True}


class InvoiceApprove(BaseModel):
    """POST /invoices/{id}/approve — what client sends"""
    notes: Optional[str] = None
    # NOT in body:
    # If-Match header → version check
    # X-Idempotency-Key header → idempotency
    # JWT → approver identity + SOX check


class GLEntryLine(BaseModel):
    account_code: str
    account_name: str
    debit_amount: Decimal
    credit_amount: Decimal


class InvoiceApproveResponse(BaseModel):
    """POST /invoices/{id}/approve — what server returns"""
    id: str
    status: str
    version: int
    approved_by: str
    approved_at: datetime
    notes: Optional[str]
    journal_entry_id: str


# ============================================================
# PAYMENT SCHEMAS
# ============================================================

class AllocationItem(BaseModel):
    """Manual allocation — which invoice gets how much"""
    invoice_id: str
    amount: Decimal = Field(..., gt=0)


class PaymentCreate(BaseModel):
    """POST /payments — what client sends"""
    customer_id: str
    payment_reference: str = Field(..., min_length=1, max_length=100)
    payment_date: date = Field(default_factory=date.today)
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(..., min_length=3, max_length=3)
    payment_method: str = Field(..., pattern="^(NEFT|RTGS|SWIFT|CHEQUE|CARD|UPI|ACH)$")
    allocation_mode: str = Field(default="AUTO", pattern="^(AUTO|MANUAL)$")
    allocations: Optional[List[AllocationItem]] = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return normalize_supported_currency(value)

    @model_validator(mode="after")
    def validate_manual_allocations(self):
        if self.allocation_mode == "MANUAL" and not self.allocations:
            raise ValueError("allocations required when allocation_mode is MANUAL")
        if self.allocation_mode == "AUTO" and self.allocations:
            raise ValueError("allocations must be null when allocation_mode is AUTO")
        if self.allocations:
            total = sum(a.amount for a in self.allocations)
            if total > self.amount:
                raise ValueError("sum of allocations cannot exceed payment amount")
        return self


class AllocationResult(BaseModel):
    invoice_id: str
    amount_allocated: Decimal
    invoice_balance_before: Decimal
    invoice_balance_after: Decimal
    invoice_status: str


class PaymentResponse(BaseModel):
    """POST /payments — what server returns"""
    id: str
    idempotency_key: Optional[str]
    status: str
    customer_id: str
    payment_reference: str
    payment_date: date
    amount: Decimal
    currency: str
    payment_method: str
    allocation_mode: str
    allocations: List[AllocationResult]
    allocated_amount: Decimal
    unallocated_amount: Decimal
    overpayment_amount: Decimal
    overpayment_action: Optional[str]
    exchange_rate_used: Decimal
    exchange_rate_date: date
    exchange_rate_warning: Optional[str]
    journal_entry_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ============================================================
# AGING SCHEMAS
# ============================================================

class AgingBucket(BaseModel):
    amount: Decimal
    invoice_count: int
    invoices: List[str]  # list of invoice IDs


class AgingResponse(BaseModel):
    """GET /customers/{id}/aging — what server returns"""
    customer: dict  # {id, name}
    entity_id: str
    as_of: datetime
    data_freshness: str
    currency: str
    buckets: dict  # {current, days_30, days_60, days_90_plus}
    total_outstanding: Decimal


# ============================================================
# JOURNAL ENTRY SCHEMAS
# ============================================================

class JournalEntryLineResponse(BaseModel):
    account_code: str
    account_name: str
    debit_amount: Decimal
    credit_amount: Decimal


class JournalEntryResponse(BaseModel):
    id: str
    reference_type: str
    reference_id: str
    document_date: date
    entry_date: date
    description: str
    created_by: str
    lines: List[JournalEntryLineResponse]
    total_debits: Decimal
    total_credits: Decimal
    balanced: bool  # always True — alert if False!


class JournalEntriesResponse(BaseModel):
    """GET /journal-entries — what server returns"""
    invoice_id: str
    pagination: dict
    journal_entries: List[JournalEntryResponse]
    summary: dict  # {total_debited_ar, total_credited_ar, net_ar_balance}


# ============================================================
# COMMON SCHEMAS
# ============================================================

class ErrorResponse(BaseModel):
    """Standard error response format"""
    error: dict  # {code, message, details, request_id}


class HealthResponse(BaseModel):
    """GET /health — what server returns"""
    status: str
    version: str
    timestamp: datetime
    checks: dict
