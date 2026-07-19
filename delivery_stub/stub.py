"""
Delivery Stub — Phase 2 placeholder

Receives invoice delivery requests from AR App.
Logs to console for prototype.

Production replacement:
→ Transactional Outbox → SQS → Email / EDI / IRP
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ERP Delivery Stub", version="1.0.0")
delivery_lock = asyncio.Lock()
deliveries: dict[str, dict] = {}


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
async def receive_invoice(
    payload: InvoiceDeliveryPayload,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    delivery_id = x_idempotency_key or f"unkeyed:{payload.invoice_id}"
    payload_hash = hashlib.sha256(
        json.dumps(
            payload.model_dump(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    async with delivery_lock:
        existing = deliveries.get(delivery_id)
        if existing:
            if existing["payload_hash"] != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Delivery idempotency key reused with different payload",
                )
            existing["duplicate_count"] += 1
            logger.info("Deduplicated delivery event %s", delivery_id)
            return {
                **existing["response"],
                "deduplicated": True,
            }

        delivered_at = datetime.utcnow().isoformat()
        response_body = {
            "status": "delivered",
            "invoice_id": payload.invoice_id,
            "delivered_at": delivered_at,
            "method": "stub_console",
        }
        deliveries[delivery_id] = {
            "payload_hash": payload_hash,
            "payload": payload.model_dump(),
            "accepted_count": 1,
            "duplicate_count": 0,
            "response": response_body,
        }

    logger.info(
        f"\n{'='*50}\n"
        f"📧 INVOICE DELIVERY STUB\n"
        f"Invoice ID:  {payload.invoice_id}\n"
        f"Delivery ID: {delivery_id}\n"
        f"Customer:    {payload.customer_id}\n"
        f"Email:       {payload.customer_email or 'N/A'}\n"
        f"Amount:      {payload.total_amount} {payload.currency}\n"
        f"Due Date:    {payload.due_date}\n"
        f"Received at: {delivered_at}\n"
        f"{'='*50}\n"
        f"✅ Invoice logged. Production: Email/EDI/IRP\n"
    )
    return {**response_body, "deduplicated": False}


@app.get("/stub/deliveries/{delivery_id}")
async def get_delivery(delivery_id: str):
    """Test/operations view proving downstream idempotency behavior."""
    async with delivery_lock:
        delivery = deliveries.get(delivery_id)
        if not delivery:
            raise HTTPException(status_code=404, detail="Delivery not found")
        return {
            "delivery_id": delivery_id,
            "invoice_id": delivery["payload"]["invoice_id"],
            "accepted_count": delivery["accepted_count"],
            "duplicate_count": delivery["duplicate_count"],
            "delivered_at": delivery["response"]["delivered_at"],
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
