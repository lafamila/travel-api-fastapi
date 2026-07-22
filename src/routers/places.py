from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth_utils import get_current_user, require_admin
from ..connectors import get_db_connection
from ..schemas import (
    GoogleMapsLinkResolution,
    MapLinkResolution,
    ResolveGoogleMapsLinkRequest,
    ResolveMapLinkRequest,
    TravelPlace,
    TravelPlaceCreateRequest,
    TravelPlaceUpdateRequest,
    TravelReview,
    TravelReviewCreateRequest,
)
from ..services.authorization import (
    are_friends,
    can_manage_place,
    can_review_place,
    can_view_place,
)
from ..services.place_links import (
    PlaceLinkError,
    PlaceLinkResult,
    detect_map_provider,
    resolve_place_link as resolve_supported_place_link,
)
from ..utils import dump_json, generate_id, parse_json_list, to_mysql_datetime

router = APIRouter(prefix="/api/places", tags=["places"])
logger = logging.getLogger(__name__)

PLACE_COLUMNS = """
    id, name, category, latitude, longitude, address, description,
    opening_hours, special_notes, cover_image_url, photo_urls_json, tags_json,
    owner_account_id, owner_login_id, owner_name, owner_email, visibility,
    created_at, updated_at
"""
REVIEW_COLUMNS = """
    id, place_id, rating, headline, body, visited_at, photo_urls_json,
    author_account_id, author_login_id, author_name, author_email,
    created_at, updated_at
"""


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
        authorAccountId=row["author_account_id"],
        authorLoginId=row["author_login_id"],
        authorName=row["author_name"],
        authorEmail=row.get("author_email"),
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
        visibility=row["visibility"],
        ownerAccountId=row["owner_account_id"],
        ownerLoginId=row["owner_login_id"],
        ownerName=row["owner_name"],
        ownerEmail=row.get("owner_email"),
        createdAt=row["created_at"].isoformat(),
        updatedAt=row["updated_at"].isoformat(),
        reviews=reviews or [],
    )


def _fetch_place_row(cursor, place_id: str, user: dict) -> dict:
    cursor.execute(
        f"SELECT {PLACE_COLUMNS} FROM travel_places WHERE id = %s", (place_id,)
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Place not found")
    friendship = are_friends(cursor, user["account_id"], row["owner_account_id"])
    if not can_view_place(user, row, friendship):
        raise HTTPException(status_code=404, detail="Place not found")
    return row


@router.get("", response_model=list[TravelPlace])
async def get_places(
    sw_lat: float | None = None,
    sw_lng: float | None = None,
    ne_lat: float | None = None,
    ne_lng: float | None = None,
    user: dict = Depends(get_current_user),
):
    conditions = []
    values: list = []
    if user["permission"] != "superadmin":
        conditions.append(
            """
            (p.owner_account_id = %s OR (
                p.visibility = 'public' AND EXISTS (
                    SELECT 1 FROM travel_friendships f
                    WHERE (f.account_a_id = %s AND f.account_b_id = p.owner_account_id)
                       OR (f.account_b_id = %s AND f.account_a_id = p.owner_account_id)
                )
            ))
            """
        )
        values.extend([user["account_id"]] * 3)
    bounds = (sw_lat, sw_lng, ne_lat, ne_lng)
    if all(value is not None for value in bounds):
        conditions.extend(
            [
                "p.latitude BETWEEN %s AND %s",
                "p.longitude BETWEEN %s AND %s",
            ]
        )
        values.extend([sw_lat, ne_lat, sw_lng, ne_lng])
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {PLACE_COLUMNS}
                FROM travel_places p
                {where_clause}
                ORDER BY p.updated_at DESC
                """,
                values,
            )
            return [_map_place(row) for row in cursor.fetchall()]


@router.post("", response_model=TravelPlace, status_code=status.HTTP_201_CREATED)
async def create_place(
    data: TravelPlaceCreateRequest,
    user: dict = Depends(get_current_user),
):
    place_id = generate_id("place")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO travel_places (
                    id, name, category, latitude, longitude, address, description,
                    opening_hours, special_notes, cover_image_url,
                    photo_urls_json, tags_json, visibility,
                    owner_account_id, owner_login_id, owner_name, owner_email
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s)
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
                    data.visibility,
                    user["account_id"],
                    user["login_id"],
                    user["name"],
                    user.get("email"),
                ),
            )
            cursor.execute(
                f"SELECT {PLACE_COLUMNS} FROM travel_places WHERE id = %s",
                (place_id,),
            )
            return _map_place(cursor.fetchone())


async def _resolve_place_link(data_url: str, request: Request) -> PlaceLinkResult:
    try:
        return await resolve_supported_place_link(
            data_url,
            browser=getattr(request.app.state, "browser", None),
        )
    except PlaceLinkError as error:
        logger.warning("resolve-map-link failed: %s", error)
        raise HTTPException(
            status_code=400,
            detail=f"Failed to resolve map link: {error}",
        ) from error


@router.post("/resolve-map-link", response_model=MapLinkResolution)
async def resolve_map_link(
    data: ResolveMapLinkRequest,
    request: Request,
    user: dict = Depends(require_admin),
):
    _ = user
    result = await _resolve_place_link(data.url, request)
    return MapLinkResolution(
        provider=result.provider,
        resolvedUrl=result.resolved_url,
        sourcePlaceId=result.source_place_id,
        name=result.name,
        address=result.address,
        latitude=result.latitude,
        longitude=result.longitude,
        openingHours=result.opening_hours,
        primaryType=result.primary_type,
        coverImageUrl=result.cover_image_url,
    )


@router.post("/resolve-google-link", response_model=GoogleMapsLinkResolution)
async def resolve_google_link(
    data: ResolveGoogleMapsLinkRequest,
    request: Request,
    user: dict = Depends(require_admin),
):
    _ = user
    try:
        provider = detect_map_provider(data.url)
    except PlaceLinkError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if provider != "google":
        raise HTTPException(
            status_code=400,
            detail="This compatibility endpoint accepts Google Maps links only",
        )
    result = await _resolve_place_link(data.url, request)
    return GoogleMapsLinkResolution(
        resolvedUrl=result.resolved_url,
        googlePlaceId=result.source_place_id,
        googleMapsUri=result.resolved_url,
        name=result.name,
        address=result.address,
        latitude=result.latitude,
        longitude=result.longitude,
        openingHours=result.opening_hours,
        primaryType=result.primary_type,
    )


@router.get("/{place_id}", response_model=TravelPlace)
async def get_place(
    place_id: str,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            row = _fetch_place_row(cursor, place_id, user)
            cursor.execute(
                f"""
                SELECT {REVIEW_COLUMNS}
                FROM travel_place_reviews
                WHERE place_id = %s
                ORDER BY created_at DESC
                """,
                (place_id,),
            )
            reviews = [_map_review(review) for review in cursor.fetchall()]
            return _map_place(row, reviews)


@router.put("/{place_id}", response_model=TravelPlace)
async def update_place(
    place_id: str,
    data: TravelPlaceUpdateRequest,
    user: dict = Depends(get_current_user),
):
    update_fields = data.model_dump(exclude_none=True)
    if not update_fields:
        return await get_place(place_id, user)
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
        "visibility": "visibility",
    }
    clauses = []
    values = []
    for key, value in update_fields.items():
        clauses.append(f"{field_map[key]} = %s")
        values.append(dump_json(value) if key in {"photoUrls", "tags"} else value)
    values.append(place_id)
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {PLACE_COLUMNS} FROM travel_places WHERE id = %s",
                (place_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Place not found")
            if not can_manage_place(user, row):
                raise HTTPException(
                    status_code=403, detail="Place owner access required"
                )
            cursor.execute(
                f"UPDATE travel_places SET {', '.join(clauses)} WHERE id = %s",
                values,
            )
    return await get_place(place_id, user)


@router.delete("/{place_id}")
async def delete_place(
    place_id: str,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {PLACE_COLUMNS} FROM travel_places WHERE id = %s",
                (place_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Place not found")
            if not can_manage_place(user, row):
                raise HTTPException(
                    status_code=403, detail="Place owner access required"
                )
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
    return {"message": "Place deleted"}


@router.post(
    "/{place_id}/reviews",
    response_model=TravelReview,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    place_id: str,
    data: TravelReviewCreateRequest,
    user: dict = Depends(get_current_user),
):
    review_id = generate_id("review")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {PLACE_COLUMNS} FROM travel_places WHERE id = %s",
                (place_id,),
            )
            place = cursor.fetchone()
            if not place:
                raise HTTPException(status_code=404, detail="Place not found")
            friendship = are_friends(
                cursor, user["account_id"], place["owner_account_id"]
            )
            if not can_review_place(user, place, friendship):
                raise HTTPException(
                    status_code=403, detail="Place review access denied"
                )
            cursor.execute(
                """
                INSERT INTO travel_place_reviews (
                    id, place_id, rating, headline, body, visited_at, photo_urls_json,
                    author_account_id, author_login_id, author_name, author_email
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review_id,
                    place_id,
                    data.rating,
                    data.headline,
                    data.body,
                    to_mysql_datetime(data.visitedAt),
                    dump_json(data.photoUrls),
                    user["account_id"],
                    user["login_id"],
                    user["name"],
                    user.get("email"),
                ),
            )
            cursor.execute(
                f"SELECT {REVIEW_COLUMNS} FROM travel_place_reviews WHERE id = %s",
                (review_id,),
            )
            return _map_review(cursor.fetchone())
