from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from src.services.place_links import (
    PlaceLinkError,
    detect_map_provider,
    parse_kakao_place_html,
    parse_naver_place_html,
    resolve_redirect_url,
)


class PlaceLinkParserTests(unittest.TestCase):
    def test_detects_supported_providers(self):
        cases = {
            "https://maps.app.goo.gl/example": "google",
            "https://www.google.com/maps/place/Test": "google",
            "https://place.map.kakao.com/1564616308": "kakao",
            "https://map.naver.com/p/entry/place/2061355349": "naver",
            "https://naver.me/example": "naver",
        }

        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_map_provider(url), expected)

    def test_rejects_lookalike_and_non_http_hosts(self):
        rejected = (
            "https://maps.app.goo.gl.example.com/place",
            "https://place.map.kakao.com.example.com/123",
            "file:///etc/passwd",
        )

        for url in rejected:
            with self.subTest(url=url), self.assertRaises(PlaceLinkError):
                detect_map_provider(url)

    def test_short_link_rejects_unsupported_redirect_before_next_request(self):
        response = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=(
                "HTTP/1.1 302 Found\r\n"
                "Location: http://127.0.0.1/private\r\n\r\n"
                "__MAP_LINK_STATUS__:302"
            ),
            stderr="",
        )

        with patch("src.services.place_links.subprocess.run", return_value=response) as run:
            with self.assertRaises(PlaceLinkError):
                resolve_redirect_url("https://maps.app.goo.gl/example")

        run.assert_called_once()

    def test_parses_kakao_open_graph_metadata(self):
        html = """
        <html><head>
          <meta property="og:title" content="더숲아카데미하우스 카페">
          <meta property="og:description" content="서울 강북구 4.19로 135 1,4층">
          <meta property="og:image" content="//img.example/place.jpg">
          <meta name="twitter:image"
                content="http://staticmap.kakao.com/staticmap/og?m=127.00139588304545%2C37.64046045528658">
        </head></html>
        """

        parsed = parse_kakao_place_html(
            "https://place.map.kakao.com/1564616308", html
        )

        self.assertEqual(parsed.provider, "kakao")
        self.assertEqual(parsed.source_place_id, "1564616308")
        self.assertEqual(parsed.name, "더숲아카데미하우스")
        self.assertEqual(parsed.primary_type, "카페")
        self.assertEqual(parsed.address, "서울 강북구 4.19로 135 1,4층")
        self.assertEqual(parsed.latitude, 37.64046045528658)
        self.assertEqual(parsed.longitude, 127.00139588304545)
        self.assertEqual(parsed.cover_image_url, "https://img.example/place.jpg")

    def test_parses_naver_apollo_state(self):
        state = {
            "PlaceDetailBase:2061355349": {
                "id": "2061355349",
                "name": "더숲아카데미하우스",
                "category": "복합문화공간",
                "roadAddress": "서울 강북구 4.19로 135",
                "address": "서울 강북구 수유동 산76",
                "coordinate": {"x": "127.0013935", "y": "37.6404244"},
            }
        }
        html = (
            '<meta property="og:image" content="https://img.example/naver.jpg">'
            f"<script>window.__APOLLO_STATE__ = {json.dumps(state)};</script>"
        )

        parsed = parse_naver_place_html(
            "https://map.naver.com/p/entry/place/2061355349",
            "2061355349",
            html,
        )

        self.assertEqual(parsed.provider, "naver")
        self.assertEqual(parsed.source_place_id, "2061355349")
        self.assertEqual(parsed.name, "더숲아카데미하우스")
        self.assertEqual(parsed.primary_type, "복합문화공간")
        self.assertEqual(parsed.address, "서울 강북구 4.19로 135")
        self.assertEqual(parsed.latitude, 37.6404244)
        self.assertEqual(parsed.longitude, 127.0013935)
        self.assertEqual(parsed.cover_image_url, "https://img.example/naver.jpg")


if __name__ == "__main__":
    unittest.main()
