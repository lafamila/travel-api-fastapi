from __future__ import annotations

import unittest

from src.services.google_maps_links import parse_google_maps_url


class GoogleMapsLinkParserTests(unittest.TestCase):
    def test_parses_coordinates_from_maps_short_link_destination(self):
        url = (
            "https://www.google.com/maps/place/"
            "%EB%8D%94%EC%88%B2+%EC%95%84%EC%B9%B4%EB%8D%B0%EB%AF%B8%ED%95%98%EC%9A%B0%EC%8A%A4/"
            "data=!4m6!3m5!1s0x357ca18eccd46885:0x8724d1f4254d66f!8m2"
            "!3d37.6400262!4d127.0002986!16s%2Fg%2F1tf18pqz"
        )

        parsed = parse_google_maps_url(url)

        self.assertEqual(parsed.query_text, "더숲 아카데미하우스")
        self.assertEqual(parsed.latitude, 37.6400262)
        self.assertEqual(parsed.longitude, 127.0002986)

    def test_parses_coordinates_from_viewport_url(self):
        parsed = parse_google_maps_url(
            "https://www.google.com/maps/place/Test/@37.5,127.25,17z"
        )

        self.assertEqual(parsed.latitude, 37.5)
        self.assertEqual(parsed.longitude, 127.25)

    def test_parses_coordinates_from_ll_query(self):
        parsed = parse_google_maps_url(
            "https://maps.google.com/?q=Test&ll=37.5%2C127.25"
        )

        self.assertEqual(parsed.latitude, 37.5)
        self.assertEqual(parsed.longitude, 127.25)

    def test_rejects_out_of_range_coordinates(self):
        parsed = parse_google_maps_url(
            "https://www.google.com/maps/place/Test/@137.5,227.25,17z"
        )

        self.assertIsNone(parsed.latitude)
        self.assertIsNone(parsed.longitude)


if __name__ == "__main__":
    unittest.main()
