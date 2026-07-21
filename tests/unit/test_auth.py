import asyncio
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from jose import jwt

from src import auth


TEST_KEY = "dev-secret-key"
TEST_JWK = {
    "kty": "oct",
    "k": "ZGV2LXNlY3JldC1rZXk",
    "alg": "HS256",
    "kid": "dev-key-001",
}


def make_token(**overrides):
    payload = {
        "sub": "user-1",
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "entity_id": "entity-1",
        "roles": ["invoice_creator"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    payload.update(overrides)
    return jwt.encode(
        payload,
        TEST_KEY,
        algorithm="HS256",
        headers={"kid": "dev-key-001"},
    )


class JWTNegativeTests(unittest.TestCase):
    def assert_unauthorized(self, operation):
        with self.assertRaises(HTTPException) as context:
            operation()
        self.assertEqual(context.exception.status_code, 401)

    def validate(self, token):
        with (
            patch.object(auth, "ENVIRONMENT", "development"),
            patch.object(auth, "_get_public_key", return_value=TEST_JWK),
        ):
            return auth.validate_jwt(token)

    def test_malformed_token_is_rejected(self):
        self.assert_unauthorized(lambda: self.validate("not-a-jwt"))

    def test_token_without_kid_is_rejected(self):
        token = jwt.encode(
            {"sub": "user-1", "exp": int(time.time()) + 300},
            TEST_KEY,
            algorithm="HS256",
        )
        self.assert_unauthorized(lambda: self.validate(token))

    def test_wrong_signature_is_rejected(self):
        token = jwt.encode(
            {"sub": "user-1", "exp": int(time.time()) + 300},
            "wrong-secret",
            algorithm="HS256",
            headers={"kid": "dev-key-001"},
        )
        self.assert_unauthorized(lambda: self.validate(token))

    def test_expired_token_is_rejected(self):
        self.assert_unauthorized(
            lambda: self.validate(make_token(exp=int(time.time()) - 1))
        )

    def test_missing_tenant_claim_is_rejected(self):
        self.assert_unauthorized(lambda: self.validate(make_token(tenant_id=None)))

    def test_roles_string_cannot_crash_validation(self):
        self.assert_unauthorized(lambda: self.validate(make_token(roles="cfo")))

    def test_wrong_authorization_scheme_is_rejected(self):
        self.assert_unauthorized(
            lambda: asyncio.run(auth.get_current_user("Basic abc"))
        )

    def test_empty_bearer_token_is_rejected(self):
        self.assert_unauthorized(
            lambda: asyncio.run(auth.get_current_user("Bearer   "))
        )

    def test_valid_token_still_returns_context(self):
        user = self.validate(make_token())
        self.assertEqual(user.user_id, "user-1")
        self.assertEqual(user.roles, ["invoice_creator"])


class JWKSFetchTests(unittest.TestCase):
    def setUp(self):
        auth._get_cached_jwks.cache_clear()

    def tearDown(self):
        auth._get_cached_jwks.cache_clear()

    def test_jwks_fetch_failure_raises_503(self):
        with patch.object(auth.httpx, "get", side_effect=Exception("network down")):
            with self.assertRaises(HTTPException) as ctx:
                auth._get_cached_jwks()
        self.assertEqual(ctx.exception.status_code, 503)

    def test_unknown_kid_after_refresh_raises_401(self):
        with patch.object(auth, "_get_cached_jwks", return_value={"keys": [TEST_JWK]}):
            with self.assertRaises(HTTPException) as ctx:
                auth._get_public_key("unknown-kid")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_public_key_finds_key_by_kid(self):
        with patch.object(auth, "_get_cached_jwks", return_value={"keys": [TEST_JWK]}):
            key = auth._get_public_key("dev-key-001")
        self.assertEqual(key, TEST_JWK)


class CreateTestTokenTests(unittest.TestCase):
    def test_create_test_token_round_trips_through_validate_jwt(self):
        token = auth.create_test_token("user-9", "tenant-9", "entity-9", ["cfo"])
        with (
            patch.object(auth, "ENVIRONMENT", "development"),
            patch.object(auth, "_get_public_key", return_value=TEST_JWK),
        ):
            user = auth.validate_jwt(token)
        self.assertEqual(user.user_id, "user-9")
        self.assertEqual(user.tenant_id, "tenant-9")
        self.assertEqual(user.roles, ["cfo"])


if __name__ == "__main__":
    unittest.main()
