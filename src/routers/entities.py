import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, and_, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import CurrentUser, require_role
from src.models import Entity, Tenant
from src.schemas import EntityCreate, EntityUpdate, EntityResponse
from src.exceptions import VersionConflictException, BusinessRuleException
from src.routers.invoices import (
    check_idempotency,
    create_idempotency_key,
    complete_idempotency_key,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def serialize_entity(entity: Entity) -> dict:
    return json.loads(EntityResponse.model_validate(entity).model_dump_json())


async def set_rls_context(db: AsyncSession, current_user: CurrentUser):
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


# ============================================================
# POST /entities
# An entity is always created for the caller's own, existing
# tenant — tenant_id is derived from the JWT, never accepted
# from the client.
# ============================================================
@router.post("/entities", status_code=status.HTTP_201_CREATED)
async def create_entity(
    payload: EntityCreate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    current_user: CurrentUser = Depends(require_role("system_admin", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    await set_rls_context(db, current_user)

    request_hash = hashlib.sha256(
        json.dumps(payload.model_dump(), default=str).encode()
    ).hexdigest()

    # Entity creation is a tenant-level operation, not scoped to any
    # particular existing entity (and the caller's own current_user.entity_id
    # may not even exist yet, e.g. the first entity created for a brand-new
    # tenant) — so the idempotency key is recorded with entity_id=None.
    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id, None,
        "POST /entities", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    # ── Tenant must exist and be active ────────────────────
    tenant_result = await db.execute(
        select(Tenant).where(
            and_(Tenant.id == current_user.tenant_id, Tenant.is_active == True)
        )
    )
    if not tenant_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Tenant not found")

    # ── Parent entity, if given, must belong to the same tenant ─
    if payload.parent_entity_id:
        parent_result = await db.execute(
            select(Entity).where(
                and_(
                    Entity.id == payload.parent_entity_id,
                    Entity.tenant_id == current_user.tenant_id,
                )
            )
        )
        if not parent_result.scalar_one_or_none():
            raise BusinessRuleException(
                "PARENT_ENTITY_NOT_FOUND",
                "parent_entity_id does not exist within this tenant",
            )

    name_check = await db.execute(
        select(Entity).where(
            and_(Entity.tenant_id == current_user.tenant_id, Entity.name == payload.name)
        )
    )
    if name_check.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Entity name already exists for this tenant")

    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, None,
        "POST /entities", request_hash
    )

    entity = Entity(
        tenant_id=current_user.tenant_id,
        parent_entity_id=payload.parent_entity_id,
        name=payload.name,
        currency=payload.currency,
    )
    db.add(entity)
    await db.flush()

    response_body = serialize_entity(entity)

    await complete_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, None,
        "POST /entities", 201, response_body
    )
    await db.commit()

    return JSONResponse(status_code=201, content=response_body)


# ============================================================
# GET /entities
# Scoped to the caller's own tenant.
# ============================================================
@router.get("/entities")
async def list_entities(
    is_active: Optional[bool] = None,
    current_user: CurrentUser = Depends(require_role("system_admin", "cfo", "auditor")),
    db: AsyncSession = Depends(get_db),
):
    await set_rls_context(db, current_user)

    query = select(Entity).where(Entity.tenant_id == current_user.tenant_id)
    if is_active is not None:
        query = query.where(Entity.is_active == is_active)
    query = query.order_by(Entity.name)

    result = await db.execute(query)
    entities = result.scalars().all()

    return JSONResponse(
        status_code=200,
        content={"entities": [serialize_entity(e) for e in entities]},
    )


# ============================================================
# GET /entities/{entity_id}
# ============================================================
@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: str,
    current_user: CurrentUser = Depends(require_role("system_admin", "cfo", "auditor")),
    db: AsyncSession = Depends(get_db),
):
    await set_rls_context(db, current_user)

    result = await db.execute(
        select(Entity).where(
            and_(Entity.id == entity_id, Entity.tenant_id == current_user.tenant_id)
        )
    )
    entity = result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    return JSONResponse(
        status_code=200,
        content=serialize_entity(entity),
        headers={"ETag": str(entity.version)},
    )


# ============================================================
# PATCH /entities/{entity_id}
# ============================================================
@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    payload: EntityUpdate,
    x_idempotency_key: str = Header(..., alias="X-Idempotency-Key"),
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("system_admin", "cfo")),
    db: AsyncSession = Depends(get_db),
):
    await set_rls_context(db, current_user)

    request_hash = hashlib.sha256(
        f"{entity_id}:{json.dumps(payload.model_dump(exclude_unset=True), default=str)}".encode()
    ).hexdigest()

    # Scoped by the entity actually being patched, not the caller's own JWT
    # entity claim — an admin patching another entity may not have an
    # entity_id of their own that even exists in this tenant.
    existing = await check_idempotency(
        db, x_idempotency_key, current_user.tenant_id, entity_id,
        f"PATCH /entities/{entity_id}", request_hash
    )
    if existing:
        return JSONResponse(
            status_code=existing.response_status,
            content=existing.response_body
        )

    result = await db.execute(
        select(Entity).where(
            and_(Entity.id == entity_id, Entity.tenant_id == current_user.tenant_id)
        )
    )
    entity = result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if str(entity.version) != if_match:
        raise VersionConflictException(
            "Entity was modified since last viewed. Please refresh."
        )

    update_data = payload.model_dump(exclude_unset=True)

    if "parent_entity_id" in update_data and update_data["parent_entity_id"]:
        if update_data["parent_entity_id"] == entity_id:
            raise BusinessRuleException(
                "INVALID_PARENT", "Entity cannot be its own parent"
            )
        parent_result = await db.execute(
            select(Entity).where(
                and_(
                    Entity.id == update_data["parent_entity_id"],
                    Entity.tenant_id == current_user.tenant_id,
                )
            )
        )
        if not parent_result.scalar_one_or_none():
            raise BusinessRuleException(
                "PARENT_ENTITY_NOT_FOUND",
                "parent_entity_id does not exist within this tenant",
            )

    if "name" in update_data and update_data["name"] != entity.name:
        name_check = await db.execute(
            select(Entity).where(
                and_(
                    Entity.tenant_id == current_user.tenant_id,
                    Entity.name == update_data["name"],
                    Entity.id != entity_id,
                )
            )
        )
        if name_check.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Entity name already exists for this tenant")

    await create_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, entity_id,
        f"PATCH /entities/{entity_id}", request_hash
    )

    now = datetime.utcnow()
    values = {**update_data, "version": Entity.version + 1, "updated_at": now}
    claimed = await db.execute(
        update(Entity)
        .where(and_(Entity.id == entity_id, Entity.version == entity.version))
        .values(**values)
    )
    if claimed.rowcount == 0:
        raise VersionConflictException(
            "Entity was modified since last viewed. Please refresh."
        )

    await db.refresh(entity)
    response_body = serialize_entity(entity)

    await complete_idempotency_key(
        db, x_idempotency_key, current_user.tenant_id, entity_id,
        f"PATCH /entities/{entity_id}", 200, response_body
    )
    await db.commit()

    return JSONResponse(
        status_code=200,
        content=response_body,
        headers={"ETag": str(entity.version)},
    )
