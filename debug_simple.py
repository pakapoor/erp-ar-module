#!/usr/bin/env python3
"""
Debug script: Interactive walkthrough of POST /invoices
Runs inside Docker container with pdb debugger.
"""
import asyncio
import json
import sys
import pdb
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from io import StringIO

# ── Prepare test data ──────────────────────────────────────────────────
print("=" * 80)
print("POST /invoices - INTERACTIVE DEBUG WALKTHROUGH")
print("=" * 80)

from src.schemas import InvoiceCreate, LineItemCreate
from src.auth import CurrentUser

print("\n[STEP 1] Prepare test payload")
print("─" * 80)

payload = InvoiceCreate(
    customer_id="00000000-0000-0000-0000-000000000005",
    po_reference="PO-DEBUG-001",
    invoice_date=date(2026, 7, 21),
    currency="USD",
    payment_terms="NET30",
    line_items=[
        LineItemCreate(
            description="Debug Widget",
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            tax_rate=Decimal("18"),
            tax_jurisdiction="IN",
        )
    ]
)

print("Payload:")
for key, val in payload.model_dump().items():
    print(f"  {key}: {val}")

print("\n[STEP 2] Prepare current_user (from JWT)")
print("─" * 80)

current_user = CurrentUser(
    user_id="00000000-0000-0000-0000-000000000003",
    tenant_id="00000000-0000-0000-0000-000000000001",
    entity_id="00000000-0000-0000-0000-000000000002",
    roles=["invoice_creator"]
)

print(f"User ID: {current_user.user_id}")
print(f"Tenant ID: {current_user.tenant_id}")
print(f"Entity ID: {current_user.entity_id}")
print(f"Roles: {current_user.roles}")

print("\n[STEP 3] Mock database and call create_invoice()")
print("─" * 80)
print("Setting up mocks for DB interactions...")

# Mock setup
db = AsyncMock()

# Step through the mocks
class MockResult:
    def __init__(self, value=None):
        self.value = value
    def scalar_one_or_none(self):
        return self.value
    def scalar_one(self):
        return self.value
    def scalar(self):
        return self.value
    def scalars(self):
        class Scalars:
            def all(self):
                return []
        return Scalars()

# Mock customer
customer = Mock()
customer.id = payload.customer_id
customer.name = "Acme Corp"
customer.credit_limit = Decimal("100000")

# Mock FX rate
fx_rate = Mock()
fx_rate.id = "fx-rate-123"
fx_rate.rate = Decimal("1.0")

# Setup execute mock
async def execute_mock(query):
    await asyncio.sleep(0.01)  # Simulate DB delay
    return MockResult(None)

db.execute = Mock(side_effect=execute_mock)
db.flush = AsyncMock()
db.commit = AsyncMock()
db.add = Mock()

print("✓ Mocks configured")

async def run_debug():
    print("\n" + "=" * 80)
    print("CALLING create_invoice()...")
    print("=" * 80)
    print("\nYou can now inspect the code flow.")
    print("Looking at src/routers/invoices.py line 228...\n")
    
    # Set a breakpoint
    pdb.set_trace()
    
    from src.routers.invoices import create_invoice
    
    try:
        result = await create_invoice(
            payload=payload,
            x_idempotency_key="debug-idem-123",
            current_user=current_user,
            db=db,
        )
        print("\n✓ Invoice created successfully!")
        print(f"Response status: {result.status_code if hasattr(result, 'status_code') else 'N/A'}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_debug())
