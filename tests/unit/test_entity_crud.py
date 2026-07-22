from datetime import datetime
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.auth import CurrentUser
from src.exceptions import BusinessRuleException, VersionConflictException
from src.models import Entity
from src.routers import entities
from src.schemas import EntityCreate, EntityUpdate


class Result:
    def __init__(self, value=None, rows=None, rowcount=1):
        self.value = value
        self.rows = rows or []
        self.rowcount = rowcount

    def scalar_one_or_none(self): return self.value
    def scalar_one(self): return self.value
    def scalars(self): return self
    def all(self): return self.rows


ADMIN = CurrentUser(user_id="admin", tenant_id="tenant-1", entity_id="e", roles=["system_admin"])


def make_tenant(**overrides):
    defaults = dict(id="tenant-1", name="Reliance", base_currency="INR", is_active=True)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def make_entity(**overrides):
    defaults = dict(
        id="entity-1", tenant_id="tenant-1", parent_entity_id=None,
        name="Reliance Retail", currency="INR", is_active=True, version=1,
        created_at=datetime(2026, 7, 1), updated_at=datetime(2026, 7, 1),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class CreateEntityTests(IsolatedAsyncioTestCase):
    async def test_missing_tenant_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [
            Result(),          # set_config
            Result(None),       # tenant lookup — not found
        ]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await entities.create_entity(
                EntityCreate(name="New Entity", currency="INR"), "idem-1", ADMIN, db
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_missing_parent_entity_raises_business_rule(self):
        db = AsyncMock()
        db.execute.side_effect = [
            Result(),                 # set_config
            Result(make_tenant()),    # tenant lookup
            Result(None),             # parent entity lookup — not found
        ]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await entities.create_entity(
                EntityCreate(name="New Entity", currency="INR", parent_entity_id="parent-x"),
                "idem-1", ADMIN, db
            )
        self.assertEqual(ctx.exception.code, "PARENT_ENTITY_NOT_FOUND")

    async def test_duplicate_name_within_tenant_is_409(self):
        db = AsyncMock()
        db.execute.side_effect = [
            Result(),                  # set_config
            Result(make_tenant()),     # tenant lookup
            Result(make_entity()),     # name check — existing entity found
        ]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(HTTPException) as ctx,
        ):
            await entities.create_entity(
                EntityCreate(name="Reliance Retail", currency="INR"), "idem-1", ADMIN, db
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_cached_idempotent_response_short_circuits(self):
        cached = SimpleNamespace(response_status=201, response_body={"id": "cached-entity"})
        db = AsyncMock()
        db.execute.return_value = Result()
        with patch.object(entities, "check_idempotency", new=AsyncMock(return_value=cached)):
            response = await entities.create_entity(
                EntityCreate(name="New Entity", currency="INR"), "idem-1", ADMIN, db
            )
        self.assertEqual(response.status_code, 201)
        self.assertIn(b"cached-entity", response.body)

    async def test_idempotency_scoped_by_none_entity_not_callers_own_entity(self):
        # Regression guard: entity creation used to scope its idempotency key
        # by current_user.entity_id, which fails against a real DB (FK to
        # entity(id)) whenever the caller's own entity_id doesn't exist yet --
        # e.g. creating the very first entity in a brand-new tenant. Entity
        # creation is tenant-level, so it must pass entity_id=None instead.
        def stamp_defaults(obj):
            if isinstance(obj, Entity) and obj.id is None:
                obj.id = "new-entity-1"
                obj.is_active = True
                obj.version = 1
                obj.created_at = datetime(2026, 7, 1)
                obj.updated_at = datetime(2026, 7, 1)

        db = AsyncMock()
        db.add = Mock(side_effect=stamp_defaults)
        db.execute.side_effect = [
            Result(),
            Result(make_tenant()),
            Result(None),
        ]
        check_mock = AsyncMock(return_value=None)
        create_mock = AsyncMock()
        complete_mock = AsyncMock()
        with (
            patch.object(entities, "check_idempotency", new=check_mock),
            patch.object(entities, "create_idempotency_key", new=create_mock),
            patch.object(entities, "complete_idempotency_key", new=complete_mock),
        ):
            await entities.create_entity(
                EntityCreate(name="New Entity", currency="INR"), "idem-1", ADMIN, db
            )

        for call in (check_mock, create_mock, complete_mock):
            called_entity_id = call.call_args[0][3]
            self.assertIsNone(called_entity_id)

    async def test_creates_entity_for_callers_tenant(self):
        def stamp_defaults(obj):
            if isinstance(obj, Entity) and obj.id is None:
                obj.id = "new-entity-1"
                obj.is_active = True
                obj.version = 1
                obj.created_at = datetime(2026, 7, 1)
                obj.updated_at = datetime(2026, 7, 1)

        db = AsyncMock()
        db.add = Mock(side_effect=stamp_defaults)
        db.execute.side_effect = [
            Result(),                  # set_config
            Result(make_tenant()),     # tenant lookup
            Result(None),               # name check — no existing
        ]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(entities, "create_idempotency_key", new=AsyncMock()),
            patch.object(entities, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await entities.create_entity(
                EntityCreate(name="New Branch", currency="usd"), "idem-1", ADMIN, db
            )
        self.assertEqual(response.status_code, 201)
        added_entity = db.add.call_args[0][0]
        self.assertEqual(added_entity.tenant_id, "tenant-1")
        self.assertEqual(added_entity.name, "New Branch")
        self.assertEqual(added_entity.currency, "USD")


class GetEntityTests(IsolatedAsyncioTestCase):
    async def test_missing_entity_is_404(self):
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(None)]
        with self.assertRaises(HTTPException) as ctx:
            await entities.get_entity("missing", ADMIN, db)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_etag_header_reflects_version(self):
        entity = make_entity(version=4)
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(entity)]
        response = await entities.get_entity("entity-1", ADMIN, db)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("ETag"), "4")


class ListEntitiesTests(IsolatedAsyncioTestCase):
    async def test_lists_entities_for_tenant(self):
        db = AsyncMock()
        db.execute.side_effect = [
            Result(),
            Result(rows=[make_entity(), make_entity(id="entity-2", name="Branch 2")]),
        ]
        response = await entities.list_entities(None, ADMIN, db)
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("Reliance Retail", body)
        self.assertIn("Branch 2", body)


class UpdateEntityTests(IsolatedAsyncioTestCase):
    async def test_idempotency_scoped_by_target_entity_not_callers_own_entity(self):
        # Regression guard: an admin patching some other entity may not have
        # an entity_id of their own that exists in this tenant, so the
        # idempotency key must be scoped by the entity_id path param (the
        # entity actually being patched), not current_user.entity_id.
        entity = make_entity(version=1, is_active=True)
        db = AsyncMock()
        db.refresh = AsyncMock()
        db.execute.side_effect = [Result(), Result(entity), Result(rowcount=1)]
        check_mock = AsyncMock(return_value=None)
        create_mock = AsyncMock()
        complete_mock = AsyncMock()
        with (
            patch.object(entities, "check_idempotency", new=check_mock),
            patch.object(entities, "create_idempotency_key", new=create_mock),
            patch.object(entities, "complete_idempotency_key", new=complete_mock),
        ):
            await entities.update_entity(
                "entity-1", EntityUpdate(is_active=False), "idem-1", "1", ADMIN, db
            )

        for call in (check_mock, create_mock, complete_mock):
            called_entity_id = call.call_args[0][3]
            self.assertEqual(called_entity_id, "entity-1")

    async def test_version_mismatch_raises_conflict(self):
        entity = make_entity(version=2)
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(entity)]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(VersionConflictException),
        ):
            await entities.update_entity(
                "entity-1", EntityUpdate(name="New Name"), "idem-1", "1", ADMIN, db
            )

    async def test_self_parent_raises_business_rule(self):
        entity = make_entity(version=1)
        db = AsyncMock()
        db.execute.side_effect = [Result(), Result(entity)]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            self.assertRaises(BusinessRuleException) as ctx,
        ):
            await entities.update_entity(
                "entity-1", EntityUpdate(parent_entity_id="entity-1"), "idem-1", "1", ADMIN, db
            )
        self.assertEqual(ctx.exception.code, "INVALID_PARENT")

    async def test_deactivates_entity(self):
        entity = make_entity(version=1, is_active=True)
        db = AsyncMock()
        db.refresh = AsyncMock()
        db.execute.side_effect = [
            Result(),                # set_config
            Result(entity),          # fetch
            Result(rowcount=1),      # claimed update
        ]
        with (
            patch.object(entities, "check_idempotency", new=AsyncMock(return_value=None)),
            patch.object(entities, "create_idempotency_key", new=AsyncMock()),
            patch.object(entities, "complete_idempotency_key", new=AsyncMock()),
        ):
            response = await entities.update_entity(
                "entity-1", EntityUpdate(is_active=False), "idem-1", "1", ADMIN, db
            )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    import unittest
    unittest.main()
