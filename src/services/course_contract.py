from __future__ import annotations

import json
from typing import Any

OUTPUT_FORMAT_VERSION = "1.0"


def build_export_payload(
    trip_window: dict[str, str],
    course_start: dict[str, Any],
    selected_places: list[dict[str, Any]],
    selection_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "outputFormatVersion": OUTPUT_FORMAT_VERSION,
        "tripWindow": {
            "startAt": trip_window["startAt"],
            "endAt": trip_window["endAt"],
        },
        "courseStart": course_start,
        "selectedPlaces": selected_places,
        "selectionContext": selection_context or {},
    }


def build_prompt_text(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        "다음 JSON을 기반으로 여행 코스를 제안해주세요.\n"
        "응답은 JSON만 반환하고, outputFormatVersion은 그대로 유지해주세요.\n"
        "각 stop에는 반드시 대응되는 내부 placeId를 포함해야 하며, selectedPlaces에 전달된 placeId를 변경하거나 누락하면 안 됩니다.\n\n"
        f"{payload_json}\n"
    )


def validate_import_payload(import_payload: dict[str, Any]) -> None:
    if import_payload.get("outputFormatVersion") != OUTPUT_FORMAT_VERSION:
        raise ValueError("Unsupported outputFormatVersion")

    course = import_payload.get("course")
    if not isinstance(course, dict):
        raise ValueError("course must be an object")

    if not course.get("title"):
        raise ValueError("course.title is required")

    stops = course.get("stops")
    if not isinstance(stops, list) or not stops:
        raise ValueError("course.stops is required")

    for index, stop in enumerate(stops, start=1):
        if not isinstance(stop, dict):
            raise ValueError(f"course.stops[{index}] must be an object")
        if not stop.get("placeId"):
            raise ValueError(f"course.stops[{index}].placeId is required")
        if not stop.get("placeName"):
            raise ValueError(f"course.stops[{index}].placeName is required")
