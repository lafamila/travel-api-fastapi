from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request
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

    async def test_import_access_application_requests_admin_with_korean_message(self) -> None:
        self.service._sessions["session-1"] = session_auth.TravelSession(
            id="session-1",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            session_expires_at=9999999999,
            user={"id": "account-1", "account_id": "account-1"},
        )
        captured = {}

        def fake_request(method, url, body, headers):
            captured.update({"method": method, "url": url, "body": body, "headers": headers})
            return session_auth._HttpResponse(201, {}, {"id": "application-1"})

        self.service._request_json = fake_request
        result = await self.service.create_import_access_application(
            _request_with_cookie("teddy_travel_session", "session-1")
        )

        self.assertEqual(result, {"id": "application-1"})
        self.assertEqual(captured["body"]["requestedPermissionKey"], "admin")
        self.assertIn("관리자 권한", captured["body"]["message"])

    async def test_existing_service_application_still_requests_user(self) -> None:
        self.service._sessions["session-1"] = session_auth.TravelSession(
            id="session-1",
            access_token="access-token",
            refresh_token="refresh-token",
            access_token_expires_at=9999999999,
            session_expires_at=9999999999,
            user={"id": "account-1", "account_id": "account-1"},
        )
        captured = {}
        self.service._request_json = lambda method, url, body, headers: (
            captured.update(body) or session_auth._HttpResponse(201, {}, body)
        )
        await self.service.create_service_application(
            _request_with_cookie("teddy_travel_session", "session-1"), None
        )
        self.assertEqual(captured["requestedPermissionKey"], "user")

    async def test_force_refresh_rotates_even_unexpired_access_token(self) -> None:
        original = session_auth.TravelSession(
            id="session-1",
            access_token="old-access",
            refresh_token="old-refresh",
            access_token_expires_at=9999999999,
            session_expires_at=9999999999,
            user={"id": "account-1", "account_id": "account-1"},
        )
        self.service._sessions[original.id] = original
        requests = []
        self.service._request_token = lambda body: requests.append(body) or {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        self.service._create_session = lambda token, session_id, refresh_lock: (
            session_auth.TravelSession(
                id=session_id,
                access_token=token["access_token"],
                refresh_token=token["refresh_token"],
                access_token_expires_at=9999999999,
                session_expires_at=9999999999,
                user={"id": "account-1", "account_id": "account-1", "permission": "admin"},
                refresh_lock=refresh_lock,
            )
        )

        async def fake_get_user(_request):
            return {"permission": self.service._sessions["session-1"].user["permission"]}

        self.service.get_user = fake_get_user
        result = await self.service.force_refresh(
            _request_with_cookie("teddy_travel_session", "session-1")
        )

        self.assertEqual(result["permission"], "admin")
        self.assertEqual(requests[0]["grant_type"], "refresh_token")
        self.assertEqual(self.service._sessions["session-1"].access_token, "new-access")


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


def _request_with_cookie(name: str, value: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"cookie", f"{name}={value}".encode())],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )


if __name__ == "__main__":
    unittest.main()
