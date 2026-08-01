from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth_utils import get_current_user
from ..connectors import get_db_connection
from ..schemas import (
    CourseExportRequest,
    CourseExportResponse,
    CourseImportPayload,
    TravelCourse,
    TravelCourseCreateRequest,
    TravelCourseStop,
)
from ..services.authorization import (
    are_friends,
    can_access_course,
    can_view_place,
)
from ..services.course_contract import (
    OUTPUT_FORMAT_VERSION,
    build_export_payload,
    build_prompt_text,
    validate_import_payload,
)
from ..utils import dump_json, generate_id, to_mysql_datetime

router = APIRouter(prefix="/api/courses", tags=["courses"])

COURSE_COLUMNS = """
    id, title, start_location, trip_start_at, trip_end_at,
    transport_mode, summary, prompt_text, output_format_version,
    source_payload_json, import_payload_json,
    owner_account_id, owner_login_id, owner_name, owner_email,
    created_at, updated_at
"""


def _map_stop(row: dict) -> TravelCourseStop:
    return TravelCourseStop(
        placeId=row["place_id"],
        placeName=row["place_name"],
        order=row["stop_order"],
        scheduledAt=row["scheduled_at"].isoformat() if row["scheduled_at"] else None,
        note=row["note"],
        reasoningText=row["reasoning_text"],
        transitHint=row["transit_hint"],
    )


def _fetch_course(cursor, course_id: str, user: dict) -> TravelCourse:
    cursor.execute(
        f"SELECT {COURSE_COLUMNS} FROM travel_courses WHERE id = %s", (course_id,)
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")
    if not can_access_course(user, row):
        raise HTTPException(status_code=404, detail="Course not found")
    cursor.execute(
        """
        SELECT id, course_id, place_id, place_name, stop_order, scheduled_at,
               note, reasoning_text, transit_hint
        FROM travel_course_stops
        WHERE course_id = %s
        ORDER BY stop_order ASC
        """,
        (course_id,),
    )
    return TravelCourse(
        id=row["id"],
        title=row["title"],
        startLocation=row["start_location"],
        tripStartAt=row["trip_start_at"].isoformat() if row["trip_start_at"] else None,
        tripEndAt=row["trip_end_at"].isoformat() if row["trip_end_at"] else None,
        transportMode=row["transport_mode"],
        summary=row["summary"],
        promptText=row["prompt_text"],
        outputFormatVersion=row["output_format_version"],
        stops=[_map_stop(stop) for stop in cursor.fetchall()],
        ownerAccountId=row["owner_account_id"],
        ownerLoginId=row["owner_login_id"],
        ownerName=row["owner_name"],
        ownerEmail=row.get("owner_email"),
        createdAt=row["created_at"].isoformat(),
        updatedAt=row["updated_at"].isoformat(),
    )


def _require_accessible_place_id(cursor, place_id: str, user: dict) -> str:
    cursor.execute(
        """
        SELECT id, owner_account_id, visibility
        FROM travel_places
        WHERE id = %s AND deleted_at IS NULL
        FOR UPDATE
        """,
        (place_id,),
    )
    place = cursor.fetchone()
    if not place:
        raise HTTPException(
            status_code=400,
            detail=f"Referenced placeId does not exist: {place_id}",
        )
    friendship = are_friends(cursor, user["account_id"], place["owner_account_id"])
    if not can_view_place(user, place, friendship):
        raise HTTPException(
            status_code=403,
            detail=f"Referenced placeId is not accessible: {place_id}",
        )
    return place["id"]


def _persist_course(
    cursor,
    payload: TravelCourseCreateRequest,
    user: dict,
    source_payload: dict | None = None,
    import_payload: dict | None = None,
) -> str:
    validated_stops = [
        (_require_accessible_place_id(cursor, stop.placeId, user), stop)
        for stop in payload.stops
    ]
    course_id = generate_id("course")
    cursor.execute(
        """
        INSERT INTO travel_courses (
            id, title, start_location, trip_start_at, trip_end_at, transport_mode,
            summary, prompt_text, output_format_version, source_payload_json,
            import_payload_json,
            owner_account_id, owner_login_id, owner_name, owner_email
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s)
        """,
        (
            course_id,
            payload.title,
            payload.startLocation,
            to_mysql_datetime(payload.tripStartAt),
            to_mysql_datetime(payload.tripEndAt),
            payload.transportMode,
            payload.summary,
            payload.promptText,
            payload.outputFormatVersion,
            dump_json(source_payload) if source_payload else None,
            dump_json(import_payload) if import_payload else None,
            user["account_id"],
            user["login_id"],
            user["name"],
            user.get("email"),
        ),
    )
    for validated_place_id, stop in validated_stops:
        cursor.execute(
            """
            INSERT INTO travel_course_stops (
                id, course_id, place_id, place_name, stop_order,
                scheduled_at, note, reasoning_text, transit_hint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                generate_id("stop"),
                course_id,
                validated_place_id,
                stop.placeName,
                stop.order,
                to_mysql_datetime(stop.scheduledAt),
                stop.note,
                stop.reasoningText,
                stop.transitHint,
            ),
        )
    return course_id


@router.get("", response_model=list[TravelCourse])
async def get_courses(user: dict = Depends(get_current_user)):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            if user["permission"] == "superadmin":
                cursor.execute("SELECT id FROM travel_courses ORDER BY updated_at DESC")
            else:
                cursor.execute(
                    """
                    SELECT id FROM travel_courses
                    WHERE owner_account_id = %s
                    ORDER BY updated_at DESC
                    """,
                    (user["account_id"],),
                )
            return [_fetch_course(cursor, row["id"], user) for row in cursor.fetchall()]


@router.post("", response_model=TravelCourse, status_code=status.HTTP_201_CREATED)
async def create_course(
    data: TravelCourseCreateRequest,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            course_id = _persist_course(cursor, data, user)
            return _fetch_course(cursor, course_id, user)


@router.post("/export", response_model=CourseExportResponse)
async def export_course_prompt(
    data: CourseExportRequest,
    user: dict = Depends(get_current_user),
):
    _ = user
    payload = build_export_payload(
        trip_window=data.tripWindow.model_dump(),
        course_start=data.courseStart.model_dump(exclude_none=True),
        selected_places=[
            place.model_dump(exclude_none=True) for place in data.selectedPlaces
        ],
        selection_context=data.selectionContext,
    )
    return CourseExportResponse(payload=payload, promptText=build_prompt_text(payload))


@router.post(
    "/import", response_model=TravelCourse, status_code=status.HTTP_201_CREATED
)
async def import_course(
    data: CourseImportPayload,
    user: dict = Depends(get_current_user),
):
    raw_payload = data.model_dump()
    try:
        validate_import_payload(raw_payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    course = raw_payload["course"]
    payload = TravelCourseCreateRequest(
        title=course["title"],
        startLocation=course.get("startLocation"),
        tripStartAt=course.get("tripWindow", {}).get("startAt"),
        tripEndAt=course.get("tripWindow", {}).get("endAt"),
        transportMode=course.get("transportMode"),
        summary=course.get("summary"),
        promptText=course.get("promptText"),
        outputFormatVersion=raw_payload.get(
            "outputFormatVersion", OUTPUT_FORMAT_VERSION
        ),
        stops=[
            TravelCourseStop(
                placeId=stop["placeId"],
                placeName=stop["placeName"],
                order=index,
                scheduledAt=stop.get("scheduledAt"),
                note=stop.get("note"),
                reasoningText=stop.get("reasoningText"),
                transitHint=stop.get("transitHint"),
            )
            for index, stop in enumerate(course["stops"], start=1)
        ],
    )
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            course_id = _persist_course(
                cursor, payload, user, import_payload=raw_payload
            )
            return _fetch_course(cursor, course_id, user)


@router.get("/{course_id}", response_model=TravelCourse)
async def get_course(
    course_id: str,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            return _fetch_course(cursor, course_id, user)


@router.delete("/{course_id}")
async def delete_course(
    course_id: str,
    user: dict = Depends(get_current_user),
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {COURSE_COLUMNS} FROM travel_courses WHERE id = %s",
                (course_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Course not found")
            if not can_access_course(user, row):
                raise HTTPException(
                    status_code=403, detail="Course owner access required"
                )
            cursor.execute("DELETE FROM travel_courses WHERE id = %s", (course_id,))
    return {"message": "Course deleted"}
