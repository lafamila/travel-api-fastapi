from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from .google_maps_links import crawl_google_maps_place, parse_google_maps_url

MapProvider = Literal["google", "kakao", "naver"]

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SUPPORTED_HOSTS: dict[str, MapProvider] = {
    "maps.app.goo.gl": "google",
    "goo.gl": "google",
    "maps.google.com": "google",
    "google.com": "google",
    "www.google.com": "google",
    "maps.google.co.kr": "google",
    "www.google.co.kr": "google",
    "place.map.kakao.com": "kakao",
    "map.naver.com": "naver",
    "naver.me": "naver",
    "m.place.naver.com": "naver",
    "pcmap.place.naver.com": "naver",
}
_SHORT_LINK_HOSTS = {"maps.app.goo.gl", "goo.gl", "naver.me"}
_KAKAO_CATEGORY_SUFFIXES = (
    "복합문화공간",
    "관광명소",
    "미술관",
    "박물관",
    "음식점",
    "베이커리",
    "게스트하우스",
    "호텔",
    "펜션",
    "카페",
    "한식",
    "중식",
    "일식",
    "양식",
    "술집",
)


@dataclass(frozen=True)
class PlaceLinkResult:
    provider: MapProvider
    resolved_url: str
    source_place_id: str | None
    name: str
    address: str | None
    latitude: float
    longitude: float
    opening_hours: str | None = None
    primary_type: str | None = None
    cover_image_url: str | None = None


class PlaceLinkError(ValueError):
    pass


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "meta":
            return
        attributes = {key.lower(): value for key, value in attrs if value is not None}
        key = attributes.get("property") or attributes.get("name")
        content = attributes.get("content")
        if key and content and key not in self.values:
            self.values[key] = content.strip()


def detect_map_provider(url: str) -> MapProvider:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise PlaceLinkError("Map links must use http or https")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    provider = _SUPPORTED_HOSTS.get(hostname)
    if not provider:
        raise PlaceLinkError("Only Google Maps, Kakao Map, and Naver Map links are supported")
    return provider


def resolve_redirect_url(url: str) -> str:
    provider = detect_map_provider(url)
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname not in _SHORT_LINK_HOSTS:
        return url.strip()

    current_url = url.strip()
    for _ in range(6):
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "20",
                    "-D",
                    "-",
                    "-o",
                    "/dev/null",
                    "-w",
                    "\n__MAP_LINK_STATUS__:%{http_code}",
                    current_url,
                ],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PlaceLinkError("short link resolution could not run") from error
        if result.returncode != 0:
            message = result.stderr.strip() or "short link resolution failed"
            raise PlaceLinkError(message)

        status_match = re.search(r"__MAP_LINK_STATUS__:(\d{3})\s*$", result.stdout)
        if not status_match:
            raise PlaceLinkError("short link response status is missing")
        status_code = int(status_match.group(1))
        if 200 <= status_code < 300:
            return current_url
        if not 300 <= status_code < 400:
            raise PlaceLinkError(f"short link returned HTTP {status_code}")

        locations = re.findall(
            r"(?im)^location:\s*(.+?)\s*$",
            result.stdout[: status_match.start()],
        )
        if not locations:
            raise PlaceLinkError("short link redirect location is missing")
        next_url = urljoin(current_url, locations[-1].strip())
        if detect_map_provider(next_url) != provider:
            raise PlaceLinkError("Map short link redirected to an unsupported provider")
        current_url = next_url

    raise PlaceLinkError("Map short link exceeded the redirect limit")


def fetch_static_html(url: str) -> str:
    detect_map_provider(url)
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "--fail",
                "--connect-timeout",
                "5",
                "--max-time",
                "20",
                "--max-filesize",
                "2000000",
                "-A",
                _USER_AGENT,
                "-H",
                "Accept-Language: ko-KR,ko;q=0.9",
                url,
            ],
            capture_output=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PlaceLinkError("place page download could not run") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise PlaceLinkError(message or "place page download failed")
    return result.stdout.decode("utf-8", errors="replace")


def parse_kakao_place_html(url: str, html: str) -> PlaceLinkResult:
    place_id = _extract_place_id(url, "kakao")
    metadata = _parse_metadata(html)
    raw_name = metadata.get("og:title") or metadata.get("twitter:title")
    address = metadata.get("og:description") or metadata.get("twitter:description")
    static_map_url = metadata.get("twitter:image")
    if not raw_name or not static_map_url:
        raise PlaceLinkError("Kakao place metadata is incomplete")

    query = parse_qs(urlparse(static_map_url).query)
    coordinate_value = (query.get("m") or [None])[0]
    if not coordinate_value:
        raise PlaceLinkError("Kakao place coordinates are missing")
    coordinate_parts = coordinate_value.split(",")
    if len(coordinate_parts) != 2:
        raise PlaceLinkError("Kakao place coordinates are malformed")
    longitude, latitude = map(float, coordinate_parts)
    _validate_coordinates(latitude, longitude)

    name, primary_type = _split_kakao_title(raw_name)
    return PlaceLinkResult(
        provider="kakao",
        resolved_url=url,
        source_place_id=place_id,
        name=name,
        address=address,
        latitude=latitude,
        longitude=longitude,
        primary_type=primary_type,
        cover_image_url=_absolute_image_url(metadata.get("og:image")),
    )


def parse_naver_place_html(
    source_url: str, place_id: str, html: str
) -> PlaceLinkResult:
    metadata = _parse_metadata(html)
    marker = re.search(r"window\.__APOLLO_STATE__\s*=\s*", html)
    if not marker:
        raise PlaceLinkError("Naver place state is missing")
    try:
        state, _ = json.JSONDecoder().raw_decode(html, marker.end())
    except json.JSONDecodeError as error:
        raise PlaceLinkError("Naver place state is malformed") from error

    base = state.get(f"PlaceDetailBase:{place_id}")
    if not isinstance(base, dict):
        base = next(
            (
                value
                for value in state.values()
                if isinstance(value, dict)
                and str(value.get("id")) == place_id
                and isinstance(value.get("coordinate"), dict)
            ),
            None,
        )
    if not isinstance(base, dict):
        raise PlaceLinkError("Naver place details are missing")
    coordinate = base.get("coordinate")
    if not isinstance(coordinate, dict):
        raise PlaceLinkError("Naver place coordinates are missing")

    try:
        longitude = float(coordinate["x"])
        latitude = float(coordinate["y"])
    except (KeyError, TypeError, ValueError) as error:
        raise PlaceLinkError("Naver place coordinates are malformed") from error
    _validate_coordinates(latitude, longitude)

    name = _clean_text(base.get("name"))
    if not name:
        raise PlaceLinkError("Naver place name is missing")
    return PlaceLinkResult(
        provider="naver",
        resolved_url=source_url,
        source_place_id=place_id,
        name=name,
        address=_clean_text(base.get("roadAddress") or base.get("address")),
        latitude=latitude,
        longitude=longitude,
        opening_hours=_format_naver_opening_hours(base.get("openingHours")),
        primary_type=_clean_text(base.get("category")),
        cover_image_url=_absolute_image_url(metadata.get("og:image")),
    )


async def resolve_place_link(url: str, browser: Any | None = None) -> PlaceLinkResult:
    provider = detect_map_provider(url)
    resolved_url = await asyncio.to_thread(resolve_redirect_url, url)

    if provider == "google":
        parsed = parse_google_maps_url(resolved_url)
        crawled = None
        if browser:
            crawled = await crawl_google_maps_place(
                browser,
                resolved_url,
                parsed.query_text,
                parsed.latitude,
                parsed.longitude,
            )
        name = _clean_text((crawled or {}).get("name") or parsed.query_text)
        latitude = (crawled or {}).get("latitude")
        longitude = (crawled or {}).get("longitude")
        if latitude is None:
            latitude = parsed.latitude
        if longitude is None:
            longitude = parsed.longitude
        if not name or latitude is None or longitude is None:
            raise PlaceLinkError("Google Maps place name or coordinates are missing")
        _validate_coordinates(float(latitude), float(longitude))
        return PlaceLinkResult(
            provider="google",
            resolved_url=resolved_url,
            source_place_id=_extract_google_place_id(resolved_url),
            name=name,
            address=_clean_text((crawled or {}).get("address")),
            latitude=float(latitude),
            longitude=float(longitude),
            opening_hours=_clean_text((crawled or {}).get("openingHours")),
            primary_type=_clean_text((crawled or {}).get("primaryType")),
        )

    if provider == "kakao":
        html = await asyncio.to_thread(fetch_static_html, resolved_url)
        return parse_kakao_place_html(resolved_url, html)

    place_id = _extract_place_id(resolved_url, "naver")
    static_url = f"https://m.place.naver.com/place/{place_id}/home"
    html = await asyncio.to_thread(fetch_static_html, static_url)
    return parse_naver_place_html(resolved_url, place_id, html)


def _parse_metadata(html: str) -> dict[str, str]:
    parser = _MetadataParser()
    parser.feed(html)
    return parser.values


def _extract_place_id(url: str, provider: MapProvider) -> str:
    patterns = {
        "kakao": (r"/([0-9]+)(?:/|$)",),
        "naver": (r"/entry/place/([0-9]+)(?:/|$)", r"/place/([0-9]+)(?:/|$)"),
        "google": (),
    }
    path = urlparse(url).path
    for pattern in patterns[provider]:
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    raise PlaceLinkError(f"{provider.title()} place ID is missing")


def _extract_google_place_id(url: str) -> str | None:
    match = re.search(r"!1s([^!]+)", unquote(url))
    return match.group(1) if match else None


def _split_kakao_title(title: str) -> tuple[str, str | None]:
    cleaned = _clean_text(title) or title
    for suffix in _KAKAO_CATEGORY_SUFFIXES:
        marker = f" {suffix}"
        if cleaned.endswith(marker) and len(cleaned) > len(marker):
            return cleaned[: -len(marker)].strip(), suffix
    return cleaned, None


def _format_naver_opening_hours(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if not isinstance(value, dict):
        return None
    for key in ("description", "text", "businessHours"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return _clean_text(candidate)
    return None


def _absolute_image_url(url: str | None) -> str | None:
    cleaned = _clean_text(url)
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    return cleaned


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise PlaceLinkError("Place coordinates are outside the valid range")
