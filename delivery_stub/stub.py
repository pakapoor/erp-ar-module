"""
Delivery Stub — Phase 2 placeholder

Receives invoice delivery requests from AR App.
Logs to console for prototype.

Production replacement:
→ Transactional Outbox → SQS → Email / EDI / IRP
"""
import logging
from datetime import datetime
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ERP Delivery Stub", version="1.0.0")


class InvoiceDeliveryPayload(BaseModel):
    invoice_id: str
    customer_id: str
    customer_email: Optional[str]
    tenant_id: str
    total_amount: str
    currency: str
    due_date: str


class JWKSKey(BaseModel):
    kid: str = "dev-key-001"
    kty: str = "oct"
    alg: str = "HS256"
    k: str = "ZGV2LXNlY3JldC1rZXk="  # base64 of "dev-secret-key"


# ============================================================
# Invoice delivery endpoint
# Called by AR App after invoice approval (async, non-blocking)
# ============================================================
@app.post("/stub/send-invoice")
async def receive_invoice(payload: InvoiceDeliveryPayload):
    logger.info(
        f"\n{'='*50}\n"
        f"📧 INVOICE DELIVERY STUB\n"
        f"Invoice ID:  {payload.invoice_id}\n"
        f"Customer:    {payload.customer_id}\n"
        f"Email:       {payload.customer_email or 'N/A'}\n"
        f"Amount:      {payload.total_amount} {payload.currency}\n"
        f"Due Date:    {payload.due_date}\n"
        f"Received at: {datetime.utcnow().isoformat()}\n"
        f"{'='*50}\n"
        f"✅ Invoice logged. Production: Email/EDI/IRP\n"
    )
    return {
        "status": "delivered",
        "invoice_id": payload.invoice_id,
        "delivered_at": datetime.utcnow().isoformat(),
        "method": "stub_console",
    }


# ============================================================
# JWKS endpoint — serves public keys for JWT validation
# In prototype: uses symmetric HS256 for simplicity
# Production: RSA keypair, proper JWKS format
# ============================================================
@app.get("/.well-known/jwks.json")
async def jwks():
    return {
        "keys": [
            {
                "kid": "dev-key-001",
                "kty": "oct",
                "alg": "HS256",
                "k": "ZGV2LXNlY3JldC1rZXk=",
                "use": "sig",
            }
        ]
    }


# ============================================================
# Health check for stub
# ============================================================
@app.get("/health")
async def stub_health():
    return {
        "status": "healthy",
        "service": "delivery-stub",
        "note": "Production: replace with Email/EDI/IRP service",
    }
