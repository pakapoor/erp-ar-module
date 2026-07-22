#!/usr/bin/env python3
"""
One-time bootstrap: creates a minimal tenant + entity + system_admin user
directly in the DB, solely so 0_create_tenant.sh has a real, already-existing
tenant/entity to mint a system_admin JWT against.

Why this can't just call POST /tenants: creating a tenant requires a
system_admin JWT, and that JWT's tenant_id/entity_id claims must reference a
row that already exists (the idempotency_key table has NOT NULL foreign keys
to tenant/entity). There is no way to satisfy that FK before any tenant
exists, so one small DB-seeded "bootstrap" tenant is unavoidable -- everything
downstream of it (the real session tenants/entities/invoices) goes through
the real CRUD APIs.

Idempotent: reruns reuse the same bootstrap tenant if it already exists.
Writes debug/.bootstrap_session as JSON.
"""
import asyncio
import json
import uuid
from pathlib import Path

from sqlalchemy import text
import sys
sys.path.insert(0, "/app")
from src.database import engine

SESSION_FILE = Path("/tmp/.bootstrap_session")
BOOTSTRAP_TENANT_NAME = "Debug Bootstrap Tenant"


def new_id() -> str:
    return str(uuid.uuid4())


async def setup() -> dict:
    async with engine.begin() as conn:
        existing = await conn.execute(
            text("SELECT id FROM tenant WHERE name = :name"),
            {"name": BOOTSTRAP_TENANT_NAME},
        )
        row = existing.first()

        if row:
            tenant_id = str(row[0])
            entity_result = await conn.execute(
                text("SELECT id FROM entity WHERE tenant_id = :tid LIMIT 1"),
                {"tid": tenant_id},
            )
            entity_id = str(entity_result.scalar_one())
            admin_result = await conn.execute(
                text("""
                    SELECT id FROM app_user
                    WHERE tenant_id = :tid AND 'system_admin' = ANY(roles)
                    LIMIT 1
                """),
                {"tid": tenant_id},
            )
            admin_id = str(admin_result.scalar_one())
        else:
            tenant_id = new_id()
            entity_id = new_id()
            admin_id = new_id()

            await conn.execute(text("""
                INSERT INTO tenant (id, name, base_currency, is_active, created_at, updated_at)
                VALUES (:id, :name, 'USD', true, NOW(), NOW())
            """), {"id": tenant_id, "name": BOOTSTRAP_TENANT_NAME})

            await conn.execute(text("""
                INSERT INTO entity (id, tenant_id, name, currency, is_active, created_at, updated_at)
                VALUES (:id, :tenant_id, 'Bootstrap Entity', 'USD', true, NOW(), NOW())
            """), {"id": entity_id, "tenant_id": tenant_id})

            await conn.execute(text("""
                INSERT INTO app_user (id, tenant_id, entity_id, name, email, roles, is_active, created_at)
                VALUES (:id, :tid, :eid, 'Bootstrap Admin', 'bootstrap-admin@debug.com', ARRAY['system_admin'], true, NOW())
            """), {"id": admin_id, "tid": tenant_id, "eid": entity_id})

    await engine.dispose()

    session = {
        "tenant_id": tenant_id,
        "entity_id": entity_id,
        "admin_user_id": admin_id,
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    print(json.dumps(session, indent=2))
    print(f"\nBootstrap session saved to {SESSION_FILE}")
    return session


if __name__ == "__main__":
    asyncio.run(setup())
