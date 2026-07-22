from datetime import datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.auth import CurrentUser
from src.exceptions import VersionConflictException
from src.models import Tenant
from src.routers import tenants
from src.schemas import TenantCreate, TenantUpdate


class Result:
    def __init__(self, value=None, rows=None, rowcount=1):
        self.value = value
        self.rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


ADMIN = CurrentUser(user_id="admin", tenant_id="t", entity_id="e", roles=["system_admin"])
NON_ADMIN = CurrentUser(user_id="rahul", tenant_id="t", entity_id="e", roles=["invoice_creator"])


def make_tenant(**overrides):
    defaults = dict(
        id="tenant-1", name="Reliance", base_currency="INR",
        is_active=True, version=1,
        created_at=datetime(2026, 7, 1), updated_at=datetime(2026, 7, 1),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class CreateTenantTests(IsolatedAsyncioTestCase):
    async def test_duplicate_name_is_409(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(make_tenant())]  # name check finds existing
        with (
            patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await tenants.create_tenant(
                TenantCreate(name="Reliance", base_currency="INR"), "idem-1", ADMIN, db
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_cached_idempotent_response_short_circuits(self):
        cached = SimpleNamespace(response_status=201, response_body={"id": "cached-tenant"})
        db = AsyncMock()
        with patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=cached)):
            response = await tenants.create_tenant(
                TenantCreate(name="Reliance", base_currency="INR"), "idem-1", ADMIN, db
            )
        self.assertEqual(response.status_code, 201)
        self.assertIn(b"cached-tenant", response.body)

    async def test_creates_tenant_with_defaults(self):
        def stamp_defaults(obj):
            if isinstance(obj, Tenant) and obj.id is None:
                obj.id = "new-tenant-1"
                obj.is_active = True
                obj.version = 1
                obj.created_at = datetime(2026, 7, 1)
                obj.updated_at = datetime(2026, 7, 1)

        db = AsyncMock()
        db.add = Mock(side_effect=stamp_defaults)
        db.execute.side_effect = [Result(None)]  # name check — no existing tenant

        with (
            patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(tenants, "create_idempotency_key", new=AsyncMock()),
            patch.object(tenants, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await tenants.create_tenant(
                TenantCreate(name="Acme Corp", base_currency="usd"), "idem-1", ADMIN, db
            )
        self.assertEqual(response.status_code, 201)
        added_tenant = db.add.call_args[0][0]
        self.assertEqual(added_tenant.name, "Acme Corp")
        self.assertEqual(added_tenant.base_currency, "USD")


class GetTenantTests(IsolatedAsyncioTestCase):
    async def test_missing_tenant_is_404(self):
        db = AsyncMock()
        db.execute.return_value = Result(None)
        with self.assertRaises(HTTPException) as ctx:
            await tenants.get_tenant("missing", ADMIN, db)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_etag_header_reflects_version(self):
        tenant = make_tenant(version=3)
        db = AsyncMock()
        db.execute.return_value = Result(tenant)
        response = await tenants.get_tenant("tenant-1", ADMIN, db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("ETag"), "3")


class ListTenantsTests(IsolatedAsyncioTestCase):
    async def test_lists_all_tenants(self):
        db = AsyncMock()
        db.execute.return_value = Result(rows=[make_tenant(), make_tenant(id="tenant-2", name="Tata")])
        response = await tenants.list_tenants(None, ADMIN, db)
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("Reliance", body)
        self.assertIn("Tata", body)


class UpdateTenantTests(IsolatedAsyncioTestCase):
    async def test_version_mismatch_raises_conflict(self):
        tenant = make_tenant(version=2)
        db = AsyncMock()
        db.execute.side_effect = [Result(tenant)]
        with (
            patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(VersionConflictException),
        ):
            await tenants.update_tenant(
                "tenant-1", TenantUpdate(name="New Name"), "idem-1", "1", ADMIN, db
            )

    async def test_missing_tenant_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(None)]
        with (
            patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await tenants.update_tenant(
                "missing", TenantUpdate(name="New Name"), "idem-1", "1", ADMIN, db
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_deactivates_tenant(self):
        tenant = make_tenant(version=1, is_active=True)
        db = AsyncMock()
        db.execute.side_effect = [
            Result(tenant),           # fetch
            Result(rowcount=1),       # claimed update
        ]
        db.refresh = AsyncMock()

        with (
            patch.object(tenants, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(tenants, "create_idempotency_key", new=AsyncMock()),
            patch.object(tenants, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await tenants.update_tenant(
                "tenant-1", TenantUpdate(is_active=False), "idem-1", "1", ADMIN, db
            )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    import unittest
    unittest.main()
