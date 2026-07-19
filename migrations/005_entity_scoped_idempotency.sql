-- Scope idempotency records by entity as well as tenant. Without this, two
-- entities in the same tenant can collide on a client-generated key and one
-- entity could receive the other's cached response.
BEGIN;

ALTER TABLE idempotency_key
  ADD COLUMN IF NOT EXISTS entity_id UUID REFERENCES entity(id);

-- Existing prototype data predates entity-scoped keys. Each existing tenant in
-- this schema has at least one entity; choose its first entity for the backfill.
UPDATE idempotency_key key
SET entity_id = (
  SELECT entity.id
  FROM entity
  WHERE entity.tenant_id = key.tenant_id
  ORDER BY entity.created_at, entity.id
  LIMIT 1
)
WHERE key.entity_id IS NULL;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM idempotency_key WHERE entity_id IS NULL) THEN
    RAISE EXCEPTION 'Cannot entity-scope idempotency keys: tenant has no entity';
  END IF;
END;
$$;

ALTER TABLE idempotency_key
  ALTER COLUMN entity_id SET NOT NULL;

ALTER TABLE idempotency_key
  DROP CONSTRAINT IF EXISTS idempotency_scope_unique;
ALTER TABLE idempotency_key
  ADD CONSTRAINT idempotency_scope_unique
  UNIQUE (tenant_id, entity_id, endpoint, key);

CREATE INDEX IF NOT EXISTS idx_idempotency_entity
  ON idempotency_key(entity_id);

COMMIT;
