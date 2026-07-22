from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []

def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def to_mysql_datetime(value: str | datetime | None) -> str | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value

    return parsed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
