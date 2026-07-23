from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Iterable

from fastapi import HTTPException

from ..connectors import get_db_connection
from ..utils import generate_id, parse_json_list
from .storage import (
    S3_BUCKET_NAME,
    delete_object,
    object_access_expires_at,
    object_access_url,
    sanitize_folder,
    upload_fileobj_to_key,
)

logger = logging.getLogger(__name__)

TRAVEL_MEDIA_TEMPORARY_TTL_HOURS = int(
    os.getenv("TRAVEL_MEDIA_TEMPORARY_TTL_HOURS", "24")
)


def create_uploaded_media(
    *,
    file_obj: BinaryIO,
    filename: str,
    content_type: str,
    folder: str,
    owner_account_id: str,
    byte_size: int | None = None,
) -> dict:
    media_id = generate_id("media")
    extension = os.path.splitext(filename)[1].lower()
    key = (
        f"uploads/{sanitize_folder(folder)}/{owner_account_id}/"
        f"{media_id}{extension}"
    )
    upload_fileobj_to_key(file_obj, key, content_type)
    try:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO travel_media (
                        id, bucket_name, object_key, content_type, original_name,
                        byte_size, owner_account_id, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'temporary')
                    """,
                    (
                        media_id,
                        S3_BUCKET_NAME,
                        key,
                        content_type,
                        filename,
                        byte_size,
                        owner_account_id,
                    ),
                )
    except Exception:
        try:
            delete_object(key)
        except Exception:
            logger.exception("Failed to roll back uploaded object %s", key)
        raise
    return serialize_media(
        {
            "id": media_id,
            "bucket_name": S3_BUCKET_NAME,
            "object_key": key,
            "content_type": content_type,
            "original_name": filename,
        }
    )


def register_attached_object(
    cursor,
    *,
    object_key: str,
    content_type: str,
    original_name: str,
    owner_account_id: str,
    byte_size: int | None = None,
) -> str:
    cursor.execute(
        """
        SELECT id FROM travel_media
        WHERE bucket_name = %s AND object_key = %s
        LIMIT 1
        """,
        (S3_BUCKET_NAME, object_key),
    )
    existing = cursor.fetchone()
    if existing:
        cursor.execute(
            "UPDATE travel_media SET status = 'attached' WHERE id = %s",
            (existing["id"],),
        )
        return existing["id"]

    media_id = generate_id("media")
    cursor.execute(
        """
        INSERT INTO travel_media (
            id, bucket_name, object_key, content_type, original_name,
            byte_size, owner_account_id, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'attached')
        """,
        (
            media_id,
            S3_BUCKET_NAME,
            object_key,
            content_type,
            original_name,
            byte_size,
            owner_account_id,
        ),
    )
    return media_id


def attach_media(cursor, media_ids: Iterable[str], user: dict) -> list[str]:
    normalized = list(dict.fromkeys(item for item in media_ids if item))
    if not normalized:
        return []
    placeholders = ", ".join(["%s"] * len(normalized))
    cursor.execute(
        f"""
        SELECT id, owner_account_id
        FROM travel_media
        WHERE id IN ({placeholders})
        """,
        normalized,
    )
    rows = {row["id"]: row for row in cursor.fetchall()}
    missing = [media_id for media_id in normalized if media_id not in rows]
    if missing:
        raise HTTPException(status_code=400, detail="Unknown media attachment")
    if user.get("permission") != "superadmin" and any(
        row["owner_account_id"] != user["account_id"] for row in rows.values()
    ):
        raise HTTPException(status_code=403, detail="Media owner access required")
    cursor.execute(
        f"UPDATE travel_media SET status = 'attached' WHERE id IN ({placeholders})",
        normalized,
    )
    return normalized


def resolve_media(cursor, media_ids: Iterable[str]) -> list[dict]:
    normalized = list(dict.fromkeys(item for item in media_ids if item))
    if not normalized:
        return []
    placeholders = ", ".join(["%s"] * len(normalized))
    cursor.execute(
        f"""
        SELECT id, bucket_name, object_key, content_type, original_name
        FROM travel_media
        WHERE id IN ({placeholders})
        """,
        normalized,
    )
    rows = {row["id"]: row for row in cursor.fetchall()}
    return [rows[media_id] for media_id in normalized if media_id in rows]


def resolve_media_urls(cursor, media_ids: Iterable[str]) -> list[str]:
    return [
        object_access_url(row["object_key"], row["bucket_name"])
        for row in resolve_media(cursor, media_ids)
    ]


def serialize_media(row: dict) -> dict:
    return {
        "id": row["id"],
        "key": row["object_key"],
        "url": object_access_url(row["object_key"], row["bucket_name"]),
        "contentType": row.get("content_type") or "application/octet-stream",
        "filename": row.get("original_name"),
        "expiresAt": object_access_expires_at(),
    }


def cleanup_unreferenced_media(media_ids: Iterable[str]) -> None:
    candidates = list(dict.fromkeys(item for item in media_ids if item))
    if not candidates:
        return
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            for media_id in candidates:
                if _is_media_referenced(cursor, media_id):
                    continue
                cursor.execute(
                    "SELECT bucket_name, object_key FROM travel_media WHERE id = %s",
                    (media_id,),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                try:
                    delete_object(row["object_key"], row["bucket_name"])
                except Exception:
                    logger.exception("Failed to delete unreferenced media %s", media_id)
                    continue
                cursor.execute("DELETE FROM travel_media WHERE id = %s", (media_id,))


def cleanup_stale_temporary_media() -> int:
    threshold = (
        datetime.now(UTC) - timedelta(hours=TRAVEL_MEDIA_TEMPORARY_TTL_HOURS)
    ).replace(tzinfo=None)
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, bucket_name, object_key FROM travel_media
                WHERE status = 'temporary' AND created_at < %s
                ORDER BY created_at
                LIMIT 500
                """,
                (threshold,),
            )
            rows = cursor.fetchall()
            deleted = 0
            for row in rows:
                try:
                    delete_object(row["object_key"], row["bucket_name"])
                except Exception:
                    logger.exception(
                        "Failed to delete stale temporary media %s", row["id"]
                    )
                    continue
                cursor.execute("DELETE FROM travel_media WHERE id = %s", (row["id"],))
                deleted += 1
            return deleted


def _is_media_referenced(cursor, media_id: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM travel_places WHERE cover_media_id = %s LIMIT 1",
        (media_id,),
    )
    if cursor.fetchone():
        return True
    for table, column in (
        ("travel_places", "photo_media_ids_json"),
        ("travel_place_reviews", "photo_media_ids_json"),
    ):
        cursor.execute(
            f"SELECT {column} FROM {table} WHERE {column} LIKE %s",
            (f"%{media_id}%",),
        )
        if any(media_id in parse_json_list(row.get(column)) for row in cursor.fetchall()):
            return True
    return False
