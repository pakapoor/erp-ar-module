import os
import logging
from typing import Optional
from functools import lru_cache

import httpx
from fastapi import Depends, HTTPException, Header, status
from jose import jwt, JWTError
from jose.backends import RSAKey
from pydantic import BaseModel

logger = logging.getLogger(__name__)

JWKS_URL = os.getenv("JWT_JWKS_URL", "http://stub:9000/.well-known/jwks.json")
ALGORITHM = "RS256"


# ============================================================
# Current user context — extracted from JWT
# ============================================================
class CurrentUser(BaseModel):
    user_id: str
    tenant_id: str
    entity_id: str
    roles: list[str]
    email: Optional[str] = None


# ============================================================
# JWKS cache — fetch public keys from auth server
# lru_cache = in-memory cache (refreshed on app restart)
# In production: add TTL-based cache with refresh on unknown kid
# ============================================================
@lru_cache(maxsize=1)
def _get_cached_jwks() -> dict:
    """Fetch JWKS from auth server and cache in memory"""
    try:
        response = httpx.get(JWKS_URL, timeout=5.0)
        response.raise_for_status()
        logger.info(f"JWKS fetched from {JWKS_URL}")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch JWKS: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth server unavailable"
        )


def _get_public_key(kid: str):
    """Get public key for given kid from JWKS"""
    jwks = _get_cached_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    # kid not found → refresh cache and retry once
    _get_cached_jwks.cache_clear()
    jwks = _get_cached_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unknown key ID in JWT"
    )


# ============================================================
# JWT validation — Zero Trust
# Called on EVERY request in AR App
# Even though gateway already validated signature
# ============================================================
def validate_jwt(token: str) -> CurrentUser:
    """
    Validate JWT independently (Zero Trust).
    1. Decode header → get kid
    2. Fetch public key from JWKS
    3. Verify signature
    4. Check expiry
    5. Extract claims
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Step 1: decode header only (no verification yet)
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise credentials_exception

        # Step 2: fetch public key by kid
        public_key = _get_public_key(kid)

        # Step 3+4: verify signature + expiry
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            options={"verify_exp": True},
        )

        # Step 5: extract claims
        user_id = payload.get("user_id") or payload.get("sub")
        tenant_id = payload.get("tenant_id")
        entity_id = payload.get("entity_id")
        roles = payload.get("roles", [])

        if not all([user_id, tenant_id, entity_id]):
            raise credentials_exception

        return CurrentUser(
            user_id=user_id,
            tenant_id=tenant_id,
            entity_id=entity_id,
            roles=roles,
            email=payload.get("email"),
        )

    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise credentials_exception


# ============================================================
# FastAPI dependency — extracts + validates JWT from header
# Usage: current_user: CurrentUser = Depends(get_current_user)
# ============================================================
async def get_current_user(
    authorization: str = Header(..., alias="Authorization")
) -> CurrentUser:
    """Extract Bearer token and validate JWT"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    token = authorization.replace("Bearer ", "")
    return validate_jwt(token)


# ============================================================
# RBAC — role check helpers
# Usage: require_role("invoice_creator")(current_user)
# ============================================================
def require_role(*required_roles: str):
    """
    Returns a FastAPI dependency that checks user has required role.
    Usage: Depends(require_role("invoice_creator"))
    """
    async def _check_role(
        current_user: CurrentUser = Depends(get_current_user)
    ) -> CurrentUser:
        if not any(role in current_user.roles for role in required_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {' or '.join(required_roles)}"
            )
        return current_user
    return _check_role


# ============================================================
# FOR PROTOTYPE / DEVELOPMENT:
# Simple JWT generator for testing without real auth server
# ============================================================
def create_test_token(
    user_id: str,
    tenant_id: str,
    entity_id: str,
    roles: list[str],
) -> str:
    """
    Generate a test JWT for development.
    NOT for production use.
    Uses symmetric HS256 instead of RS256.
    """
    import time
    payload = {
        "sub": user_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "entity_id": entity_id,
        "roles": roles,
        "exp": int(time.time()) + 3600,  # 1 hour
        "iat": int(time.time()),
    }
    # In development, stub serves JWKS with test keys
    return jwt.encode(payload, "dev-secret-key", algorithm="HS256")
