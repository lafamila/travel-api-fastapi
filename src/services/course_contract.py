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
    import_schema_json = json.dumps(
        {
            "outputFormatVersion": OUTPUT_FORMAT_VERSION,
            "course": {
                "title": "코스 제목",
                "startLocation": "출발 위치 또는 null",
                "tripWindow": {
                    "startAt": "ISO-8601 시작 일시 또는 null",
                    "endAt": "ISO-8601 종료 일시 또는 null",
                },
                "transportMode": "이동 방식 또는 null",
                "summary": "코스 요약 또는 null",
                "promptText": "생성 근거 요약 또는 null",
                "stops": [
                    {
                        "placeId": "selectedPlaces에 있는 placeId",
                        "placeName": "장소명",
                        "scheduledAt": "ISO-8601 방문 일시 또는 null",
                        "note": "방문 메모 또는 null",
                        "reasoningText": "선택 이유 또는 null",
                        "transitHint": "이전 장소에서 이동 힌트 또는 null",
                    }
                ],
            },
            "validation": {
                "warnings": [],
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "다음 입력 JSON을 기반으로 여행 코스를 제안해주세요.\n"
        "최종 응답은 import API에 바로 넣을 수 있는 JSON 객체 하나만 반환하세요. "
        "마크다운, 설명 문장, 코드펜스, 주석, trailing comma는 절대 포함하지 마세요.\n\n"
        "반환 JSON 형식은 아래 구조로 고정합니다.\n"
        f"{import_schema_json}\n\n"
        "필수 규칙:\n"
        f"- outputFormatVersion은 반드시 \"{OUTPUT_FORMAT_VERSION}\"이어야 합니다.\n"
        "- course.title은 비워둘 수 없습니다.\n"
        "- course.stops는 1개 이상이어야 하며, 배열 순서가 저장되는 코스 순서입니다. order 필드는 넣지 마세요.\n"
        "- 각 stop.placeId는 입력 JSON의 selectedPlaces[].placeId 중 하나를 그대로 사용해야 합니다. 새 ID를 만들거나 placeId를 변경하지 마세요.\n"
        "- 각 stop.placeName은 해당 placeId의 selectedPlaces[].name과 같은 장소를 가리켜야 합니다.\n"
        "- 선택값을 알 수 없으면 키를 생략하거나 null을 사용하세요. 단, course.tripWindow를 포함한다면 객체 형태를 유지하세요.\n"
        "- 입력 JSON의 selectedPlaces에 없는 장소는 stops에 포함하지 마세요.\n\n"
        "입력 JSON:\n"
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
