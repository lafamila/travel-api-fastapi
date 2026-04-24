from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# ─── DOM Selectors (using aria-label and data-item-id for stability) ─────────
# These rely on accessibility attributes, not CSS class names.
# CSS classes (e.g., rogA2c, DkEaL) are minified and change across builds.
# aria-label and data-item-id are accessibility/semantic attributes and far more stable.


@dataclass
class GoogleMapsLinkParseResult:
    resolved_url: str
    query_text: str | None
    latitude: float | None
    longitude: float | None


def resolve_google_maps_url(url: str) -> str:
    import subprocess

    # Use curl -L for reliable redirect following (urllib can fail in Docker)
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{url_effective}", url],
            capture_output=True, text=True, timeout=15,
        )
        final_url = result.stdout.strip()
        if final_url and final_url.startswith("http"):
            return final_url
    except Exception:
        pass

    # Fallback to urllib
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36",
        },
    )
    with urlopen(request, timeout=10) as response:
        return response.geturl()


def parse_google_maps_url(url: str) -> GoogleMapsLinkParseResult:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    text_candidates = []
    for key in ("q", "query"):
        if query.get(key):
            text_candidates.append(query[key][0])

    place_match = re.search(r"/place/([^/@]+)", parsed.path)
    if place_match:
        text_candidates.append(unquote(place_match.group(1)).replace("+", " "))

    lat = None
    lng = None
    coords_match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if coords_match:
        lat = float(coords_match.group(1))
        lng = float(coords_match.group(2))

    query_text = next((candidate for candidate in text_candidates if candidate), None)

    return GoogleMapsLinkParseResult(
        resolved_url=url,
        query_text=query_text,
        latitude=lat,
        longitude=lng,
    )


async def crawl_google_maps_place(
    browser: Any,
    resolved_url: str,
    query_text: str | None,
    latitude: float | None,
    longitude: float | None,
) -> dict | None:
    """Crawl a Google Maps place page using a persistent Playwright browser.

    Returns a dict with name, address, latitude, longitude, openingHours,
    primaryType — or None if crawling fails entirely.
    """
    page = None
    try:
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
            viewport={"width": 1280, "height": 900},
        )

        await page.goto(resolved_url, wait_until="domcontentloaded", timeout=15000)

        # Wait for the place title to appear
        try:
            await page.wait_for_selector("h1", timeout=10000)
        except Exception:
            logger.warning("h1 not found, attempting fallback extraction")

        # ── Extract name (h1 is stable) ──
        name = None
        try:
            el = await page.query_selector("h1")
            if el:
                name = (await el.inner_text()).strip()
                if name.endswith(" - Google Maps"):
                    name = name[: -len(" - Google Maps")].strip()
                if name == "Google Maps":
                    name = None
        except Exception:
            pass
        if not name:
            name = query_text

        # ── Extract address (data-item-id="address" is stable) ──
        address = None
        try:
            el = await page.query_selector("[data-item-id='address']")
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 3:
                    address = text
        except Exception:
            pass
        # Fallback: aria-label starting with "주소:"
        if not address:
            try:
                el = await page.query_selector("[aria-label^='주소:']")
                if el:
                    aria = await el.get_attribute("aria-label")
                    if aria:
                        address = aria.replace("주소: ", "").replace("주소:", "").strip()
            except Exception:
                pass

        # ── Extract category / primaryType (button with "개요" aria-label sibling) ──
        primary_type = None
        try:
            # Category is typically a button near the rating, before "개요"
            # Use JS to find text content of the category element
            primary_type = await page.evaluate("""() => {
                // Look for the category text near the title
                const buttons = document.querySelectorAll('button[jsaction]');
                for (const btn of buttons) {
                    const text = btn.textContent?.trim();
                    // Category buttons are short labels like "스시/초밥집", "카페"
                    if (text && text.length > 1 && text.length < 20
                        && !text.includes('개요') && !text.includes('정보')
                        && !text.includes('리뷰') && !text.includes('사진')) {
                        // Check if it's near the h1 (within the header area)
                        const rect = btn.getBoundingClientRect();
                        if (rect.top < 400 && rect.top > 50) {
                            return text;
                        }
                    }
                }
                return null;
            }""")
        except Exception:
            pass

        # ── Extract opening hours (aria-label based, stable) ──
        opening_hours = None
        try:
            # Collect all hours-related aria-labels
            hours_elements = await page.query_selector_all(
                "[aria-label*='영업시간'], "
                "[aria-label*='hours']"
            )
            # Find the expandable hours section and get sibling time data
            if hours_elements:
                # Collect day-by-day schedule from aria-labels
                day_labels = await page.query_selector_all("td[aria-label]")
                lines = []
                for td in day_labels:
                    aria = await td.get_attribute("aria-label")
                    if aria and (":" in aria or "시" in aria or "휴무" in aria):
                        lines.append(aria)
                if lines:
                    opening_hours = "\n".join(lines)

            # Simpler fallback: single aria-label with full schedule
            if not opening_hours:
                for el in hours_elements:
                    aria = await el.get_attribute("aria-label")
                    if aria and len(aria) > 10 and "영업시간" not in aria:
                        opening_hours = aria
                        break
        except Exception:
            pass

        # Coordinates from URL parsing (most reliable)
        if not name and latitude is None:
            return None

        return {
            "name": name or "Unknown Place",
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
            "openingHours": opening_hours,
            "primaryType": primary_type,
        }

    except Exception as e:
        logger.warning("Playwright crawling failed: %s", e)
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
