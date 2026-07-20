from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from jose import JWTError

from src import token_verifier
from src.auth_utils import require_admin
from src.services import session_auth


class TravelSessionServiceOidcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_auth.AUTH_API_BASE_URL = "http://auth.example"
        session_auth.TRAVEL_OIDC_CLIENT_ID = "travel-api"
        session_auth.TRAVEL_OIDC_CLIENT_SECRET = "travel-secret"
        session_auth.TRAVEL_OIDC_REDIRECT_URI = (
            "http://travel.example/api/session/oidc/callback"
        )
        session_auth.TRAVEL_OIDC_CALLBACK_ROUTE_PATH = "/session/oidc/callback"
        session_auth.TRAVEL_WEB_BASE_URL = "http://travel-web.example"
        session_auth.TRAVEL_SESSION_COOKIE_NAME = "teddy_travel_session"
        session_auth.TRAVEL_SESSION_COOKIE_SECURE = False
        session_auth.TRAVEL_SESSION_COOKIE_SAMESITE = "lax"
        session_auth.TRAVEL_SESSION_COOKIE_DOMAIN = None
        session_auth.TRAVEL_SESSION_MAX_AGE_SECONDS = 3600
        self.service = session_auth.TravelSessionService()

    async def test_start_login_uses_confidential_pkce_contract(self) -> None:
        payload = await self.service.start_login("/places?view=map")
        parsed = urlsplit(payload["authorizeUrl"])
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["client_id"], ["travel-api"])
        self.assertEqual(
            params["redirect_uri"],
            ["http://travel.example/api/session/oidc/callback"],
        )
        self.assertEqual(params["scope"], [session_auth.LOGIN_SCOPE])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        state = params["state"][0]
        self.assertEqual(
            self.service._login_transactions[state].return_to_path,
            "/places?view=map",
        )

    async def test_start_login_requires_confidential_client_secret(self) -> None:
        self.service.client_secret = None
        with self.assertRaises(HTTPException) as context:
            await self.service.start_login("/")
        self.assertEqual(context.exception.status_code, 503)

    async def test_callback_sets_http_only_session_cookie(self) -> None:
        payload = await self.service.start_login("/places")
        state = parse_qs(urlsplit(payload["authorizeUrl"]).query)["state"][0]

        self.service._exchange_code_for_token = lambda code, verifier: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        self.service._create_session = lambda token: session_auth.TravelSession(
            id="travel-session-123",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            session_expires_at=9999999999,
            user={"id": "account-1", "account_id": "account-1"},
        )

        response = await self.service.handle_oidc_callback(
            "auth-code", state, None, None
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"], "http://travel-web.example/places"
        )
        cookie = response.headers["set-cookie"]
        self.assertIn("teddy_travel_session=travel-session-123", cookie)
        self.assertIn("HttpOnly", cookie)

    async def test_absolute_return_to_is_rejected(self) -> None:
        payload = await self.service.start_login("https://evil.example/phish")
        state = parse_qs(urlsplit(payload["authorizeUrl"]).query)["state"][0]
        self.assertEqual(
            self.service._login_transactions[state].return_to_path,
            session_auth.TRAVEL_WEB_DEFAULT_RETURN_PATH,
        )

    def test_exact_legacy_account_lookup_rejects_missing_match(self) -> None:
        self.service.search_accounts_sync = lambda query: []
        with self.assertRaisesRegex(RuntimeError, "exactly one auth account"):
            self.service.find_exact_account_by_login_id("lafamila")

    def test_expired_server_session_is_discarded(self) -> None:
        self.service._sessions["expired"] = session_auth.TravelSession(
            id="expired",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            session_expires_at=0,
            user={"id": "account-1", "account_id": "account-1"},
        )
        self.assertIsNone(self.service._get_session_by_id("expired"))
        self.assertNotIn("expired", self.service._sessions)


class TravelTokenVerifierTests(unittest.TestCase):
    def test_builds_travel_superadmin(self) -> None:
        user = token_verifier.build_user_from_payload(
            {
                "sub": "account-1",
                "preferred_username": "lafamila",
                "name": "Lafamila",
                "email": "lafamila@example.test",
                token_verifier.SERVICE_CLAIM: {
                    "key": "travel",
                    "permission": "superadmin",
                    "permissionSchemaVersion": 1,
                },
            }
        )
        self.assertEqual(user["permission"], "superadmin")
        self.assertTrue(user["is_admin"])
        self.assertTrue(user["is_super_admin"])

    def test_rejects_token_for_another_service(self) -> None:
        with self.assertRaises(JWTError):
            token_verifier.get_permission_from_payload(
                {
                    token_verifier.SERVICE_CLAIM: {
                        "key": "todo",
                        "permission": "admin",
                    }
                }
            )


class TravelPermissionDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_cannot_use_admin_crawling_dependency(self) -> None:
        with self.assertRaises(HTTPException) as context:
            await require_admin({"permission": "user"})
        self.assertEqual(context.exception.status_code, 403)

    async def test_admin_and_superadmin_pass_crawling_dependency(self) -> None:
        for permission in ("admin", "superadmin"):
            user = {"permission": permission}
            self.assertIs(await require_admin(user), user)


if __name__ == "__main__":
    unittest.main()
