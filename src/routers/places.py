from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from ..connectors import get_db_connection
from ..schemas import (
    GoogleMapsLinkResolution,
    ResolveGoogleMapsLinkRequest,
    TravelPlace,
    TravelPlaceCreateRequest,
    TravelPlaceUpdateRequest,
    TravelReview,
    TravelReviewCreateRequest,
)
from ..services.google_maps_links import (
    crawl_google_maps_place,
    parse_google_maps_url,
    resolve_google_maps_url,
)
from ..utils import dump_json, generate_id, parse_json_list, to_mysql_datetime

router = APIRouter(prefix="/api/places", tags=["places"])


def _map_review(row: dict) -> TravelReview:
    return TravelReview(
        id=row["id"],
        placeId=row["place_id"],
        rating=row["rating"],
        headline=row["headline"],
        body=row["body"],
        visitedAt=row["visited_at"].isoformat() if row["visited_at"] else None,
        photoUrls=parse_json_list(row.get("photo_urls_json")),
        createdAt=row["created_at"].isoformat(),
        updatedAt=row["updated_at"].isoformat(),
    )


def _map_place(row: dict, reviews: list[TravelReview] | None = None) -> TravelPlace:
    return TravelPlace(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        address=row["address"],
        description=row["description"],
        openingHours=row["opening_hours"],
        specialNotes=row["special_notes"],
        coverImageUrl=row["cover_image_url"],
        photoUrls=parse_json_list(row.get("photo_urls_json")),
        tags=parse_json_list(row.get("tags_json")),
        createdAt=row["created_at"].isoformat(),
        updatedAt=row["updated_at"].isoformat(),
        reviews=reviews or [],
    )


@router.get("", response_model=list[TravelPlace])
async def get_places(
    sw_lat: float | None = None,
    sw_lng: float | None = None,
    ne_lat: float | None = None,
    ne_lng: float | None = None,
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            if (
                sw_lat is not None
                and sw_lng is not None
                and ne_lat is not None
                and ne_lng is not None
            ):
                cursor.execute(
                    """
                    SELECT id, name, category, latitude, longitude, address, description,
                           opening_hours, special_notes, cover_image_url,
                           photo_urls_json, tags_json, created_at, updated_at
                    FROM travel_places
                    WHERE latitude BETWEEN %s AND %s
                      AND longitude BETWEEN %s AND %s
                    ORDER BY updated_at DESC
                    """,
                    (sw_lat, ne_lat, sw_lng, ne_lng),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, name, category, latitude, longitude, address, description,
                           opening_hours, special_notes, cover_image_url,
                           photo_urls_json, tags_json, created_at, updated_at
                    FROM travel_places
                    ORDER BY updated_at DESC
                    """
                )
            rows = cursor.fetchall()

    return [_map_place(row) for row in rows]


@router.post("", response_model=TravelPlace, status_code=status.HTTP_201_CREATED)
async def create_place(data: TravelPlaceCreateRequest):
    place_id = generate_id("place")

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO travel_places (
                    id, name, category, latitude, longitude, address, description,
                    opening_hours, special_notes, cover_image_url,
                    photo_urls_json, tags_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    place_id,
                    data.name,
                    data.category,
                    data.latitude,
                    data.longitude,
                    data.address,
                    data.description,
                    data.openingHours,
                    data.specialNotes,
                    data.coverImageUrl,
                    dump_json(data.photoUrls),
                    dump_json(data.tags),
                ),
            )
            cursor.execute(
                """
                SELECT id, name, category, latitude, longitude, address, description,
                       opening_hours, special_notes, cover_image_url,
                       photo_urls_json, tags_json, created_at, updated_at
                FROM travel_places
                WHERE id = %s
                """,
                (place_id,),
            )
            row = cursor.fetchone()

    return _map_place(row)


@router.post("/resolve-google-link", response_model=GoogleMapsLinkResolution)
async def resolve_google_link(
    data: ResolveGoogleMapsLinkRequest, request: Request
):
    import asyncio

    import logging
    logger = logging.getLogger(__name__)

    # Resolve redirects in a thread to avoid blocking the event loop
    try:
        resolved_url = await asyncio.to_thread(resolve_google_maps_url, data.url)
        parsed = parse_google_maps_url(resolved_url)
        logger.info(
            "resolve-google-link: resolved=%s query=%s lat=%s lng=%s",
            resolved_url, parsed.query_text, parsed.latitude, parsed.longitude,
        )
    except Exception as error:
        logger.warning("resolve-google-link failed: %s", error)
        raise HTTPException(
            status_code=400,
            detail=f"Failed to resolve Google Maps link: {error}",
        ) from error

    # Must have at least a name OR coordinates
    if parsed.latitude is None or parsed.longitude is None:
        if not parsed.query_text:
            raise HTTPException(
                status_code=400,
                detail="Could not extract coordinates or place name from the Google Maps link",
            )
        # Has name but no coords — still try crawling, which may extract coords from the page
        logger.info("No coords from URL parse, will attempt crawling for name: %s", parsed.query_text)

    # Try Playwright crawling for enriched data
    crawled = None
    browser = getattr(request.app.state, "browser", None)
    if browser:
        crawled = await crawl_google_maps_place(
            browser,
            resolved_url,
            parsed.query_text,
            parsed.latitude,
            parsed.longitude,
        )
        logger.info("crawl result: %s", crawled)

    # Determine final values: prefer crawled data, fallback to URL-parsed
    final_name = (crawled or {}).get("name") or parsed.query_text or "Unknown Place"
    final_lat = (crawled or {}).get("latitude") or parsed.latitude
    final_lng = (crawled or {}).get("longitude") or parsed.longitude
    final_address = (crawled or {}).get("address")
    final_hours = (crawled or {}).get("openingHours")
    final_type = (crawled or {}).get("primaryType")

    # After all attempts, must have coordinates
    if final_lat is None or final_lng is None:
        raise HTTPException(
            status_code=400,
            detail="Could not extract coordinates from the Google Maps link",
        )

    return GoogleMapsLinkResolution(
        resolvedUrl=resolved_url,
        googlePlaceId=None,
        googleMapsUri=None,
        name=final_name,
        address=final_address,
        latitude=final_lat,
        longitude=final_lng,
        openingHours=final_hours,
        primaryType=final_type,
    )


@router.get("/{place_id}", response_model=TravelPlace)
async def get_place(place_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, category, latitude, longitude, address, description,
                       opening_hours, special_notes, cover_image_url,
                       photo_urls_json, tags_json, created_at, updated_at
                FROM travel_places
                WHERE id = %s
                """,
                (place_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Place not found")

            cursor.execute(
                """
                SELECT id, place_id, rating, headline, body, visited_at,
                       photo_urls_json, created_at, updated_at
                FROM travel_place_reviews
                WHERE place_id = %s
                ORDER BY created_at DESC
                """,
                (place_id,),
            )
            reviews = [_map_review(review_row) for review_row in cursor.fetchall()]

    return _map_place(row, reviews)


@router.put("/{place_id}", response_model=TravelPlace)
async def update_place(place_id: str, data: TravelPlaceUpdateRequest):
    update_fields = data.model_dump(exclude_none=True)
    if not update_fields:
        return await get_place(place_id)

    field_map = {
        "name": "name",
        "category": "category",
        "latitude": "latitude",
        "longitude": "longitude",
        "address": "address",
        "description": "description",
        "openingHours": "opening_hours",
        "specialNotes": "special_notes",
        "coverImageUrl": "cover_image_url",
        "photoUrls": "photo_urls_json",
        "tags": "tags_json",
    }
    clauses = []
    values = []
    for key, value in update_fields.items():
        column = field_map[key]
        clauses.append(f"{column} = %s")
        if key in {"photoUrls", "tags"}:
            values.append(dump_json(value))
        else:
            values.append(value)
    values.append(place_id)

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM travel_places WHERE id = %s", (place_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Place not found")

            cursor.execute(
                f"UPDATE travel_places SET {', '.join(clauses)} WHERE id = %s",
                values,
            )

    return await get_place(place_id)


@router.delete("/{place_id}")
async def delete_place(place_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM travel_course_stops WHERE place_id = %s LIMIT 1",
                (place_id,),
            )
            if cursor.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="Place is referenced by an existing course and cannot be deleted",
                )

            cursor.execute("DELETE FROM travel_places WHERE id = %s", (place_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Place not found")

    return {"message": "Place deleted"}


@router.post(
    "/{place_id}/reviews",
    response_model=TravelReview,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(place_id: str, data: TravelReviewCreateRequest):
    review_id = generate_id("review")

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM travel_places WHERE id = %s", (place_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="Place not found")

            cursor.execute(
                """
                INSERT INTO travel_place_reviews (
                    id, place_id, rating, headline, body, visited_at, photo_urls_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review_id,
                    place_id,
                    data.rating,
                    data.headline,
                    data.body,
                    to_mysql_datetime(data.visitedAt),
                    dump_json(data.photoUrls),
                ),
            )
            cursor.execute(
                """
                SELECT id, place_id, rating, headline, body, visited_at,
                       photo_urls_json, created_at, updated_at
                FROM travel_place_reviews
                WHERE id = %s
                """,
                (review_id,),
            )
            row = cursor.fetchone()

    return _map_review(row)
