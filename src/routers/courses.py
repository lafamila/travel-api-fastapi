from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..connectors import get_db_connection
from ..schemas import (
    CourseExportRequest,
    CourseExportResponse,
    CourseImportPayload,
    TravelCourse,
    TravelCourseCreateRequest,
    TravelCourseStop,
)
from ..services.course_contract import (
    OUTPUT_FORMAT_VERSION,
    build_export_payload,
    build_prompt_text,
    validate_import_payload,
)
from ..utils import dump_json, generate_id, to_mysql_datetime

router = APIRouter(prefix="/api/courses", tags=["courses"])


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


def _fetch_course(cursor, course_id: str) -> TravelCourse:
    cursor.execute(
        """
        SELECT id, title, start_location, trip_start_at, trip_end_at,
               transport_mode, summary, prompt_text, output_format_version,
               source_payload_json, import_payload_json, created_at, updated_at
        FROM travel_courses
        WHERE id = %s
        """,
        (course_id,),
    )
    row = cursor.fetchone()
    if not row:
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
    stops = [_map_stop(stop_row) for stop_row in cursor.fetchall()]

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
        stops=stops,
        createdAt=row["created_at"].isoformat(),
        updatedAt=row["updated_at"].isoformat(),
    )


def _require_existing_place_id(cursor, place_id: str) -> str:
    cursor.execute("SELECT id FROM travel_places WHERE id = %s", (place_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=400,
            detail=f"Referenced placeId does not exist: {place_id}",
        )
    return row["id"]


def _persist_course(
    cursor,
    payload: TravelCourseCreateRequest,
    source_payload: dict | None = None,
    import_payload: dict | None = None,
) -> str:
    course_id = generate_id("course")
    cursor.execute(
        """
        INSERT INTO travel_courses (
            id, title, start_location, trip_start_at, trip_end_at, transport_mode,
            summary, prompt_text, output_format_version, source_payload_json, import_payload_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )

    for stop in payload.stops:
        validated_place_id = _require_existing_place_id(cursor, stop.placeId)
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
async def get_courses():
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM travel_courses ORDER BY updated_at DESC")
            course_ids = [row["id"] for row in cursor.fetchall()]
            return [_fetch_course(cursor, course_id) for course_id in course_ids]


@router.post("", response_model=TravelCourse, status_code=status.HTTP_201_CREATED)
async def create_course(data: TravelCourseCreateRequest):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            course_id = _persist_course(cursor, data)
            return _fetch_course(cursor, course_id)


@router.post("/export", response_model=CourseExportResponse)
async def export_course_prompt(data: CourseExportRequest):
    payload = build_export_payload(
        trip_window=data.tripWindow.model_dump(),
        course_start=data.courseStart.model_dump(exclude_none=True),
        selected_places=[place.model_dump(exclude_none=True) for place in data.selectedPlaces],
        selection_context=data.selectionContext,
    )
    prompt_text = build_prompt_text(payload)
    return CourseExportResponse(payload=payload, promptText=prompt_text)


@router.post("/import", response_model=TravelCourse, status_code=status.HTTP_201_CREATED)
async def import_course(data: CourseImportPayload):
    raw_payload = data.model_dump()
    try:
        validate_import_payload(raw_payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    course = raw_payload["course"]

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            payload = TravelCourseCreateRequest(
                title=course["title"],
                startLocation=course.get("startLocation"),
                tripStartAt=course.get("tripWindow", {}).get("startAt"),
                tripEndAt=course.get("tripWindow", {}).get("endAt"),
                transportMode=course.get("transportMode"),
                summary=course.get("summary"),
                promptText=course.get("promptText"),
                outputFormatVersion=raw_payload.get("outputFormatVersion", OUTPUT_FORMAT_VERSION),
                stops=[
                    TravelCourseStop(
                        placeId=_require_existing_place_id(cursor, stop["placeId"]),
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
            course_id = _persist_course(
                cursor,
                payload,
                source_payload=None,
                import_payload=raw_payload,
            )
            return _fetch_course(cursor, course_id)


@router.get("/{course_id}", response_model=TravelCourse)
async def get_course(course_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            return _fetch_course(cursor, course_id)


@router.delete("/{course_id}")
async def delete_course(course_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM travel_courses WHERE id = %s", (course_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Course not found")

    return {"message": "Course deleted"}
