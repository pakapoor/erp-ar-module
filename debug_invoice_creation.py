#!/usr/bin/env python3
"""
Debug script: Step through POST /invoices with breakpoints
Run: python debug_invoice_creation.py
"""
import asyncio
import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, MagicMock
import sys

# Add project root to path
sys.path.insert(0, '/Users/pankajkapoor/projects/erp-ar-module')

from src.schemas import InvoiceCreate, LineItemCreate
from src.auth import CurrentUser
from src.routers.invoices import create_invoice

# Mock database and dependencies
async def debug_create_invoice():
    """
    Step through POST /invoices interactively.
    This will pause at breakpoint() calls.
    """
    
    print("=" * 80)
    print("DEBUG: POST /invoices - Invoice Creation Flow")
    print("=" * 80)
    
    # ── Prepare test data ──────────────────────────────────────────────────
    print("\n[SETUP] Creating test payload...")
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
    print(f"✓ Payload created:\n  {json.dumps(payload.model_dump(mode='json'), indent=2)}")
    
    # ── Mock current user ──────────────────────────────────────────────────
    print("\n[SETUP] Creating mock current_user...")
    current_user = CurrentUser(
        user_id="00000000-0000-0000-0000-000000000003",
        tenant_id="00000000-0000-0000-0000-000000000001",
        entity_id="00000000-0000-0000-0000-000000000002",
        roles=["invoice_creator"]
    )
    print(f"✓ User: {current_user.user_id} (tenant={current_user.tenant_id})")
    
    # ── Mock database session ──────────────────────────────────────────────
    print("\n[SETUP] Creating mock AsyncSession...")
    db = AsyncMock()
    
    # Mock customer lookup
    customer_mock = Mock()
    customer_mock.id = payload.customer_id
    customer_mock.name = "Acme Corp"
    customer_mock.credit_limit = 100000
    
    # Mock entity lookup
    entity_mock = Mock()
    entity_mock.currency = "USD"
    
    # Mock exchange rate lookup
    fx_mock = Mock()
    fx_mock.id = "fx-rate-snapshot-123"
    fx_mock.rate = Decimal("1.0")  # USD to USD = 1:1
    
    # Mock idempotency key lookup (first request)
    idem_result = Mock()
    idem_result.scalar_one_or_none = Mock(return_value=None)
    
    # Mock outstanding AR sum (no previous invoices)
    outstanding_result = Mock()
    outstanding_result.scalar = Mock(return_value=None)
    
    # Configure db.execute to return proper mocks based on query
    call_count = [0]
    
    async def mock_execute(query):
        call_count[0] += 1
        print(f"\n📝 DB CALL #{call_count[0]}: {type(query).__name__}")
        
        # Return appropriate mock based on call sequence
        result = Mock()
        
        # Call 1: set_config for RLS
        if call_count[0] == 1:
            print("   → Setting session config (RLS)")
            result.scalar = Mock(return_value=None)
        
        # Call 2: idempotency key check
        elif call_count[0] == 2:
            print("   → Checking idempotency key (should be new)")
            result.scalar_one_or_none = Mock(return_value=None)
        
        # Call 3: customer lookup
        elif call_count[0] == 3:
            print("   → Looking up customer")
            result.scalar_one_or_none = Mock(return_value=customer_mock)
        
        # Call 4: entity currency lookup
        elif call_count[0] == 4:
            print("   → Getting entity base currency")
            result.scalar_one = Mock(return_value="USD")
        
        # Call 5: outstanding AR sum
        elif call_count[0] == 5:
            print("   → Summing outstanding AR (credit limit check)")
            result.scalar = Mock(return_value=0)
        
        # Call 6: FX rate lookup
        elif call_count[0] == 6:
            print("   → Resolving FX rate (USD->USD)")
            result.scalar_one_or_none = Mock(return_value=fx_mock)
        
        # Call 7+: line items fetch
        else:
            print("   → Fetching saved line items")
            line_item_mock = Mock()
            line_item_mock.id = "li-123"
            line_item_mock.line_number = 1
            line_item_mock.description = "Debug Widget"
            line_item_mock.quantity = Decimal("10")
            line_item_mock.unit_price = Decimal("100")
            line_item_mock.subtotal = Decimal("1000")
            line_item_mock.tax_rate = Decimal("18")
            line_item_mock.tax_jurisdiction = "IN"
            line_item_mock.tax_amount = Decimal("180")
            line_item_mock.total_price = Decimal("1180")
            result.scalars = Mock(return_value=Mock(all=Mock(return_value=[line_item_mock])))
        
        return result
    
    db.execute = mock_execute
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = Mock()
    
    # ── Call create_invoice ────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("CALLING: create_invoice()")
    print("=" * 80)
    print("\n>>> Pausing at breakpoint. Open another terminal to inspect step by step.")
    print(">>> Or press 'c' to continue through to next breakpoint.\n")
    
    breakpoint()  # ← STOP HERE - You can now inspect variables
    
    try:
        response = await create_invoice(
            payload=payload,
            x_idempotency_key="debug-key-123",
            current_user=current_user,
            db=db,
        )
        
        print("\n" + "=" * 80)
        print("RESPONSE: HTTP 201")
        print("=" * 80)
        print(json.dumps(response.body.decode() if hasattr(response, 'body') else response, 
                         indent=2, default=str))
        
    except Exception as e:
        print(f"\n❌ ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(debug_create_invoice())
