import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, require_role
from src.models import Tenant
from src.schemas import TenantCreate, TenantUpdate, TenantResponse
from src.exceptions import VersionConflictException
from src.routers.invoices import (
    check_idempotency,
    create_idempotency_key,
    complete_idempotency_key,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def serialize_tenant(tenant: Tenant) -> dict:
    return json.loads(TenantResponse.model_validate(tenant).model_dump_json())


# ============================================================
# POST /tenants
# Tenant is the top of the hierarchy — there is no existing
# tenant context to scope this under, so it is restricted to
# system_admin and is not RLS-scoped (the tenant table itself
# carries no tenant_id column).
# ============================================================
@router.post("/tenants", status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("system_admin")),
    db: AsyncSession = Depends(get_db),
):
    request_hash = hashlib.sha256(
        json.dumps(payload.model_dump(), default=str).encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        "POST /tenants", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    result = await db.execute(select(Tenant).where(Tenant.name == payload.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Tenant name already exists")

    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        "POST /tenants", request_hash
    )

    tenant = Tenant(name=payload.name, base_currency=payload.base_currency)
    db.add(tenant)
    await db.flush()

    response_body = serialize_tenant(tenant)

    await complete_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        "POST /tenants", 201, response_body
    )
    await db.commit()

    return JSONResponse(status_code=201, content=response_body)


# ============================================================
# GET /tenants
# Listing across tenants is a platform-admin operation.
# ============================================================
@router.get("/tenants")
async def list_tenants(
    is_active: Optional[bool] = None,
    current_user: CurrentUser = Depends(require_role("system_admin")),
    db: AsyncSession = Depends(get_db),
):
    query = select(Tenant)
    if is_active is not None:
        query = query.where(Tenant.is_active == is_active)
    query = query.order_by(Tenant.name)

    result = await db.execute(query)
    tenants = result.scalars().all()

    return JSONResponse(
        status_code=200,
        content={"tenants": [serialize_tenant(t) for t in tenants]},
    )


# ============================================================
# GET /tenants/{tenant_id}
# ============================================================
@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    current_user: CurrentUser = Depends(require_role("system_admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return JSONResponse(
        status_code=200,
        content=serialize_tenant(tenant),
        headers={"ETag": str(tenant.version)},
    )


# ============================================================
# PATCH /tenants/{tenant_id}
# ============================================================
@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("system_admin")),
    db: AsyncSession = Depends(get_db),
):
    request_hash = hashlib.sha256(
        f"{tenant_id}:{json.dumps(payload.model_dump(exclude_unset=True), default=str)}".encode()
    ).hexdigest()

    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        f"PATCH /tenants/{tenant_id}", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if str(tenant.version) != if_match:
        raise VersionConflictException(
            "Tenant was modified since last viewed. Please refresh."
        )

    update_data = payload.model_dump(exclude_unset=True)

    if "name" in update_data and update_data["name"] != tenant.name:
        name_check = await db.execute(
            select(Tenant).where(
                and_(Tenant.name == update_data["name"], Tenant.id != tenant_id)
            )
        )
        if name_check.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Tenant name already exists")

    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        f"PATCH /tenants/{tenant_id}", request_hash
    )

    now = datetime.utcnow()
    values = {**update_data, "version": Tenant.version + 1, "updated_at": now}
    claimed = await db.execute(
        update(Tenant)
        .where(and_(Tenant.id == tenant_id, Tenant.version == tenant.version))
        .values(**values)
    )
    if claimed.rowcount == 0:
        raise VersionConflictException(
            "Tenant was modified since last viewed. Please refresh."
        )

    await db.refresh(tenant)
    response_body = serialize_tenant(tenant)

    await complete_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, current_user.entity_id,
        f"PATCH /tenants/{tenant_id}", 200, response_body
    )
    await db.commit()

    return JSONResponse(
        status_code=200,
        content=response_body,
        headers={"ETag": str(tenant.version)},
    )
