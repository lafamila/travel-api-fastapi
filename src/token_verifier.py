from __future__ import annotations

import json
import re
from time import time
from urllib.request import urlopen

from jose import JWTError, jwt

from .config import (
    AUTH_AUDIENCE,
    AUTH_ISSUER_URL,
    AUTH_JWKS_CACHE_SECONDS,
    AUTH_JWKS_URL,
)

SERVICE_CLAIM = "https://lafamila.xyz/claims/service"
SERVICE_PERMISSIONS = {"superadmin", "admin", "user", "visitor"}
ADMIN_PERMISSIONS = {"superadmin", "admin"}

_jwks_cache: dict | None = None
_jwks_cache_expires_at = 0.0


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "user"


def decode_auth_api_token(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    key = _find_jwk(kid)
    if key is None:
        clear_jwks_cache()
        key = _find_jwk(kid)
    if key is None:
        raise JWTError("Signing key not found")
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        issuer=AUTH_ISSUER_URL,
        audience=AUTH_AUDIENCE,
        options={"verify_at_hash": False},
    )


def get_permission_from_payload(payload: dict) -> str:
    service_claim = payload.get(SERVICE_CLAIM) or {}
    if service_claim.get("key") != "travel":
        raise JWTError("Invalid service permission")
    permission = service_claim.get("permission", "visitor")
    if permission not in SERVICE_PERMISSIONS:
        raise JWTError("Invalid service permission")
    return permission


def build_user_from_payload(payload: dict) -> dict:
    permission = get_permission_from_payload(payload)
    account_id = payload.get("sub")
    if not account_id:
        raise JWTError("Invalid token")
    login_id = (
        payload.get("preferred_username")
        or payload.get("username")
        or payload.get("email")
        or account_id
    )
    display_name = (
        payload.get("name")
        or payload.get("preferred_username")
        or payload.get("username")
        or account_id
    )
    return {
        "id": str(account_id),
        "account_id": str(account_id),
        "login_id": str(login_id),
        "name": str(display_name),
        "email": payload.get("email"),
        "permission": permission,
        "slug": slugify(str(login_id)),
        "is_admin": permission in ADMIN_PERMISSIONS,
        "is_super_admin": permission == "superadmin",
        "is_active": permission != "visitor",
    }


def serialize_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "accountId": user.get("account_id") or user["id"],
        "loginId": user.get("login_id") or user["id"],
        "name": user.get("name") or user["id"],
        "email": user.get("email"),
        "slug": user.get("slug") or slugify(user.get("login_id") or user["id"]),
        "permission": user.get("permission", "visitor"),
        "isAdmin": bool(user.get("is_admin")),
        "isSuperAdmin": bool(user.get("is_super_admin")),
    }


def _find_jwk(kid: str | None) -> dict | None:
    jwks = _get_jwks()
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _get_jwks() -> dict:
    global _jwks_cache, _jwks_cache_expires_at
    now = time()
    if _jwks_cache and _jwks_cache_expires_at > now:
        return _jwks_cache
    with urlopen(AUTH_JWKS_URL, timeout=5) as response:
        raw = response.read().decode("utf-8")
    _jwks_cache = json.loads(raw)
    _jwks_cache_expires_at = now + AUTH_JWKS_CACHE_SECONDS
    return _jwks_cache


def clear_jwks_cache() -> None:
    global _jwks_cache, _jwks_cache_expires_at
    _jwks_cache = None
    _jwks_cache_expires_at = 0.0
