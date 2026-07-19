import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    String, Numeric, Boolean, Date, DateTime,
    ForeignKey, Integer, Text, ARRAY, Index,
    UniqueConstraint, CheckConstraint, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


# ============================================================
# Helper: generate UUID default
# ============================================================
def gen_uuid():
    return str(uuid.uuid4())


# ============================================================
# TENANT
# ============================================================
class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    entities: Mapped[List["Entity"]] = relationship("Entity", back_populates="tenant")


# ============================================================
# ENTITY (subsidiary)
# ============================================================
class Entity(Base):
    __tablename__ = "entity"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    parent_entity_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="entities")
    children: Mapped[List["Entity"]] = relationship("Entity", back_populates="parent")
    parent: Mapped[Optional["Entity"]] = relationship("Entity", back_populates="children", remote_side="Entity.id")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="entity_name_tenant_unique"),
        Index("idx_entity_tenant", "tenant_id"),
    )


# ============================================================
# APP USER
# ============================================================
class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    manager_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list] = mapped_column(ARRAY(String), nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="user_email_tenant_unique"),
        Index("idx_user_tenant", "tenant_id"),
        Index("idx_user_entity", "entity_id"),
    )


# ============================================================
# CUSTOMER
# ============================================================
class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    billing_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tax_identifier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # KMS encrypted
    tax_identifier_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    payment_terms: Mapped[str] = mapped_column(String(20), nullable=False, default="NET30")
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)

    invoices: Mapped[List["Invoice"]] = relationship("Invoice", back_populates="customer")

    __table_args__ = (
        Index("idx_customer_tenant", "tenant_id"),
        Index("idx_customer_entity", "entity_id"),
    )


# ============================================================
# GL ACCOUNT
# ============================================================
class GLAccount(Base):
    __tablename__ = "gl_account"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    account_code: Mapped[str] = mapped_column(String(20), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("entity_id", "account_code", name="gl_account_code_entity_unique"),
        Index("idx_gl_account_tenant", "tenant_id"),
        Index("idx_gl_account_entity", "entity_id"),
    )


# ============================================================
# ACCOUNTING PERIOD
# ============================================================
class AccountingPeriod(Base):
    __tablename__ = "accounting_period"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    period_name: Mapped[str] = mapped_column(String(50), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    closed_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "entity_id", "start_date", name="period_unique"),
        Index("idx_period_tenant", "tenant_id"),
        Index("idx_period_status", "tenant_id", "entity_id", "status"),
    )


# ============================================================
# EXCHANGE RATE
# ============================================================
class ExchangeRate(Base):
    __tablename__ = "exchange_rate"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="XE")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "from_currency", "to_currency", "effective_date",
                         name="exchange_rate_unique"),
        Index("idx_exchange_rate_lookup", "tenant_id", "from_currency", "to_currency", "effective_date"),
    )


# ============================================================
# INVOICE
# ============================================================
class Invoice(Base):
    __tablename__ = "invoice"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("customer.id"), nullable=False)
    period_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("accounting_period.id"), nullable=True)
    po_reference: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    is_submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_to: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Currency
    transaction_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=1)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Amounts (transaction currency)
    subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    balance_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)

    # Base/reporting currency amounts
    base_subtotal_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    base_tax_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    base_total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)

    # Dates
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_terms: Mapped[str] = mapped_column(String(20), nullable=False, default="NET30")

    # Intercompany
    is_intercompany: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    receiver_entity_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=True)

    # Audit
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=False)
    approved_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    customer: Mapped["Customer"] = relationship("Customer", back_populates="invoices")
    line_items: Mapped[List["InvoiceLineItem"]] = relationship("InvoiceLineItem", back_populates="invoice",
                                                                cascade="all, delete-orphan")
    journal_entries: Mapped[List["JournalEntry"]] = relationship(
        "JournalEntry",
        primaryjoin="and_(Invoice.id == foreign(JournalEntry.reference_id), "
                    "JournalEntry.reference_type == 'INVOICE')",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_invoice_tenant", "tenant_id"),
        Index("idx_invoice_customer", "customer_id"),
        Index("idx_invoice_status", "tenant_id", "status"),
        Index("idx_invoice_due_date", "tenant_id", "due_date"),
    )


# ============================================================
# INVOICE LINE ITEM
# ============================================================
class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_item"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    invoice_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("invoice.id", ondelete="CASCADE"), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, default=0)
    tax_jurisdiction: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    total_price: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    invoice: Mapped["Invoice"] = relationship("Invoice", back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number", name="line_item_line_number_unique"),
        Index("idx_line_item_invoice", "invoice_id"),
    )


# ============================================================
# PAYMENT
# ============================================================
class Payment(Base):
    __tablename__ = "payment"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("customer.id"), nullable=False)
    payment_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Currency
    transaction_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=1)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)

    # Amounts
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    unallocated_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)

    payment_method: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    allocation_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="AUTO")
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)

    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    allocations: Mapped[List["PaymentAllocation"]] = relationship("PaymentAllocation", back_populates="payment")

    __table_args__ = (
        UniqueConstraint("tenant_id", "customer_id", "payment_reference",
                         name="payment_reference_unique"),
        Index("idx_payment_tenant", "tenant_id"),
        Index("idx_payment_customer", "customer_id"),
        Index("idx_payment_idempotency", "idempotency_key"),
    )


# ============================================================
# PAYMENT ALLOCATION
# ============================================================
class PaymentAllocation(Base):
    __tablename__ = "payment_allocation"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    payment_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("payment.id"), nullable=False)
    invoice_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("invoice.id"), nullable=False)
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    fx_gain_loss: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=False)

    payment: Mapped["Payment"] = relationship("Payment", back_populates="allocations")
    invoice: Mapped["Invoice"] = relationship("Invoice")

    __table_args__ = (
        UniqueConstraint("payment_id", "invoice_id", name="allocation_unique"),
        Index("idx_allocation_payment", "payment_id"),
        Index("idx_allocation_invoice", "invoice_id"),
    )


# ============================================================
# JOURNAL ENTRY
# ============================================================
class JournalEntry(Base):
    __tablename__ = "journal_entry"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    period_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("accounting_period.id"), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(50), nullable=False)  # INVOICE/PAYMENT/CREDIT_MEMO/MANUAL
    reference_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    adjusts_period_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounting_period.id"), nullable=True
    )
    adjustment_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    is_reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reversed_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("journal_entry.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=False)
    approved_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    lines: Mapped[List["JournalEntryLine"]] = relationship("JournalEntryLine", back_populates="journal_entry")

    __table_args__ = (
        Index("idx_je_tenant", "tenant_id"),
        Index("idx_je_reference", "reference_type", "reference_id"),
        Index("idx_je_date", "tenant_id", "entry_date"),
    )


# ============================================================
# JOURNAL ENTRY LINE
# ============================================================
class JournalEntryLine(Base):
    __tablename__ = "journal_entry_line"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    journal_entry_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("journal_entry.id"), nullable=False)
    gl_account_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("gl_account.id"), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    debit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    credit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    base_debit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    base_credit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    journal_entry: Mapped["JournalEntry"] = relationship("JournalEntry", back_populates="lines")
    gl_account: Mapped["GLAccount"] = relationship("GLAccount")

    __table_args__ = (
        Index("idx_je_line_entry", "journal_entry_id"),
        Index("idx_je_line_account", "gl_account_id"),
    )


# ============================================================
# CREDIT MEMO
# ============================================================
class CreditMemo(Base):
    __tablename__ = "credit_memo"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    entity_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("entity.id"), nullable=False)
    invoice_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("invoice.id"), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    created_by: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=False)
    approved_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("app_user.id"), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_cm_tenant", "tenant_id"),
        Index("idx_cm_invoice", "invoice_id"),
    )


# ============================================================
# IDEMPOTENCY KEY
# ============================================================
class IdempotencyKey(Base):
    __tablename__ = "idempotency_key"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("tenant.id"), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PROCESSING")
    request_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # SHA-256
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_idempotency_tenant", "tenant_id"),
        Index("idx_idempotency_expires", "expires_at"),
    )


# ============================================================
# AUDIT LOG
# ============================================================
class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    table_name: Mapped[str] = mapped_column(String(100), nullable=False)
    record_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index("idx_audit_tenant", "tenant_id"),
        Index("idx_audit_table_record", "table_name", "record_id"),
        Index("idx_audit_changed_at", "changed_at"),
    )
