from __future__ import annotations

import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_callback_route_path(redirect_uri: str) -> str:
    path = urlsplit(redirect_uri).path or "/api/session/oidc/callback"
    if path == "/api":
        return "/"
    if path.startswith("/api/"):
        return path[len("/api") :]
    raise ValueError("TRAVEL_OIDC_REDIRECT_URI path must start with /api/")


AUTH_ISSUER_URL = os.getenv("AUTH_ISSUER_URL", "http://localhost:3032").rstrip("/")
AUTH_API_BASE_URL = os.getenv("AUTH_API_BASE_URL", AUTH_ISSUER_URL).rstrip("/")
AUTH_JWKS_URL = os.getenv("AUTH_JWKS_URL", f"{AUTH_ISSUER_URL}/oauth/jwks")
AUTH_AUDIENCE = os.getenv("AUTH_AUDIENCE", "service:travel")
AUTH_JWKS_CACHE_SECONDS = int(os.getenv("AUTH_JWKS_CACHE_SECONDS", "300"))

TRAVEL_ALLOWED_ORIGINS = _get_csv(
    "TRAVEL_ALLOWED_ORIGINS",
    "http://localhost:3043,http://127.0.0.1:3043,https://map.lafamila.xyz",
)
TRAVEL_WEB_BASE_URL = os.getenv("TRAVEL_WEB_BASE_URL", "http://localhost:3043").rstrip(
    "/"
)
TRAVEL_OIDC_CLIENT_ID = os.getenv("TRAVEL_OIDC_CLIENT_ID", "travel-api")
TRAVEL_OIDC_CLIENT_SECRET = os.getenv("TRAVEL_OIDC_CLIENT_SECRET")
TRAVEL_OIDC_REDIRECT_URI = os.getenv(
    "TRAVEL_OIDC_REDIRECT_URI",
    "http://localhost:8010/api/session/oidc/callback",
)
TRAVEL_OIDC_CALLBACK_ROUTE_PATH = _get_callback_route_path(TRAVEL_OIDC_REDIRECT_URI)
TRAVEL_SESSION_COOKIE_NAME = os.getenv(
    "TRAVEL_SESSION_COOKIE_NAME", "teddy_travel_session"
)
TRAVEL_SESSION_COOKIE_SECURE = _get_bool("TRAVEL_SESSION_COOKIE_SECURE", False)
TRAVEL_SESSION_COOKIE_SAMESITE = os.getenv("TRAVEL_SESSION_COOKIE_SAMESITE", "lax")
TRAVEL_SESSION_COOKIE_DOMAIN = os.getenv("TRAVEL_SESSION_COOKIE_DOMAIN") or None
TRAVEL_SESSION_MAX_AGE_SECONDS = int(
    os.getenv("TRAVEL_SESSION_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60))
)

AUTH_SERVICE_KEY_ID = os.getenv("AUTH_SERVICE_KEY_ID", "").strip()
AUTH_SERVICE_SECRET = os.getenv("AUTH_SERVICE_SECRET", "").strip()
TRAVEL_LEGACY_OWNER_LOGIN_ID = os.getenv(
    "TRAVEL_LEGACY_OWNER_LOGIN_ID", "lafamila"
).strip()
