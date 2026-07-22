from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.routers.places import resolve_google_link, resolve_map_link
from src.schemas import ResolveGoogleMapsLinkRequest, ResolveMapLinkRequest
from src.services.place_links import PlaceLinkResult


class PlaceLinkRouteTests(unittest.TestCase):
    def test_generic_route_maps_service_result_to_response(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        result = PlaceLinkResult(
            provider="naver",
            resolved_url="https://map.naver.com/p/entry/place/2061355349",
            source_place_id="2061355349",
            name="더숲아카데미하우스",
            address="서울 강북구 4.19로 135",
            latitude=37.6404244,
            longitude=127.0013935,
            primary_type="복합문화공간",
        )

        with patch(
            "src.routers.places._resolve_place_link",
            new_callable=AsyncMock,
            return_value=result,
        ) as resolver:
            response = asyncio.run(
                resolve_map_link(
                    ResolveMapLinkRequest(
                        url="https://map.naver.com/p/entry/place/2061355349"
                    ),
                    request,
                    {"permission": "admin"},
                )
            )

        self.assertEqual(response.provider, "naver")
        self.assertEqual(response.sourcePlaceId, "2061355349")
        resolver.assert_awaited_once()

    def test_legacy_google_route_rejects_other_providers_before_fetching(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

        with patch(
            "src.routers.places._resolve_place_link", new_callable=AsyncMock
        ) as resolver:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    resolve_google_link(
                        ResolveGoogleMapsLinkRequest(
                            url="https://place.map.kakao.com/1564616308"
                        ),
                        request,
                        {"permission": "admin"},
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        resolver.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
