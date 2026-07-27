from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

from ..config import TRAVEL_IMPORT_PUBLISH_ENABLED
from ..connectors import get_db_connection
from ..utils import generate_id

logger = logging.getLogger(__name__)


class UploadAssetBusyError(RuntimeError):
    """Raised when another request is currently uploading the same client file."""


class UploadedAssetClaim:
    def __init__(self, connection, cursor, existing: dict | None) -> None:
        self._connection = connection
        self._cursor = cursor
        self.existing = existing
        self._saved = False

    def save(
        self,
        *,
        batch_id: str,
        source_ref: str,
        original_name: str,
        media_type: str | None,
        byte_size: int,
        staging_key: str,
    ) -> dict:
        if self.existing is not None:
            return self.existing
        asset_id = add_uploaded_asset(
            batch_id=batch_id,
            source_ref=source_ref,
            original_name=original_name,
            media_type=media_type,
            byte_size=byte_size,
            staging_key=staging_key,
            cursor=self._cursor,
        )
        self._connection.commit()
        self._saved = True
        self.existing = {
            "id": asset_id,
            "batch_id": batch_id,
            "source_ref": source_ref,
            "original_name": original_name,
            "media_type": media_type,
            "byte_size": byte_size,
            "staging_key": staging_key,
        }
        return self.existing


@contextmanager
def lock_uploaded_asset(
    batch_id: str,
    source_ref: str,
) -> Iterator[UploadedAssetClaim]:
    lock_digest = hashlib.sha256(f"{batch_id}\0{source_ref}".encode()).hexdigest()
    lock_name = f"travel-import-upload:{lock_digest[:40]}"
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (lock_name,))
            acquired = cursor.fetchone()
            if not acquired or acquired["acquired"] != 1:
                raise UploadAssetBusyError(source_ref)
            try:
                cursor.execute(
                    """
                    SELECT id, batch_id, source_ref, original_name, media_type,
                           byte_size, staging_key
                    FROM travel_import_assets
                    WHERE batch_id = %s AND source_ref = %s
                    """,
                    (batch_id, source_ref),
                )
                claim = UploadedAssetClaim(connection, cursor, cursor.fetchone())
                try:
                    yield claim
                    if not claim._saved:
                        connection.rollback()
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                try:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                except Exception:
                    logger.warning(
                        "Failed to release upload advisory lock %s",
                        lock_name,
                        exc_info=True,
                    )


def create_batch(
    *, name: str, source_type: str, source_path: str | None, user: dict
) -> dict:
    batch_id = generate_id("import")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO travel_import_batches (
                    id, name, source_type, source_path,
                    owner_account_id, owner_login_id, owner_name, owner_email
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch_id,
                    name,
                    source_type,
                    source_path,
                    user["account_id"],
                    user["login_id"],
                    user["name"],
                    user.get("email"),
                ),
            )
    return get_batch_detail(batch_id)


def list_batches() -> list[dict]:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch.*,
                       COALESCE(stats.asset_count, 0) AS asset_count,
                       COALESCE(stats.processed_asset_count, 0) AS processed_asset_count,
                       COALESCE(stats.excluded_asset_count, 0) AS excluded_asset_count
                FROM travel_import_batches batch
                LEFT JOIN (
                    SELECT batch_id,
                           COUNT(*) AS asset_count,
                           SUM(processed_at IS NOT NULL) AS processed_asset_count,
                           SUM(role = 'excluded') AS excluded_asset_count
                    FROM travel_import_assets
                    GROUP BY batch_id
                ) stats ON stats.batch_id = batch.id
                ORDER BY batch.created_at DESC
                """
            )
            return [_map_batch(row) for row in cursor.fetchall()]


def get_batch_row(batch_id: str, *, cursor=None) -> dict | None:
    if cursor is not None:
        cursor.execute("SELECT * FROM travel_import_batches WHERE id = %s", (batch_id,))
        return cursor.fetchone()
    with get_db_connection() as connection:
        with connection.cursor() as own_cursor:
            return get_batch_row(batch_id, cursor=own_cursor)


def get_batch_detail(batch_id: str) -> dict:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            batch = get_batch_row(batch_id, cursor=cursor)
            if not batch:
                raise KeyError(batch_id)
            cursor.execute(
                "SELECT * FROM travel_import_assets WHERE batch_id = %s "
                "ORDER BY created_at, id",
                (batch_id,),
            )
            assets = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM travel_import_clusters WHERE batch_id = %s "
                "ORDER BY sort_order, id",
                (batch_id,),
            )
            clusters = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM travel_import_review_drafts "
                "WHERE batch_id = %s AND cluster_id IS NOT NULL "
                "ORDER BY created_at, id",
                (batch_id,),
            )
            review_drafts = cursor.fetchall()
            cursor.execute(
                """
                SELECT link.draft_id, link.asset_id
                FROM travel_import_review_draft_assets link
                JOIN travel_import_review_drafts review ON review.id = link.draft_id
                WHERE review.batch_id = %s
                ORDER BY link.created_at, link.asset_id
                """,
                (batch_id,),
            )
            review_asset_links = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM travel_import_jobs WHERE batch_id = %s "
                "ORDER BY created_at DESC",
                (batch_id,),
            )
            jobs = cursor.fetchall()

    asset_ids_by_cluster: dict[str, list[str]] = {}
    for asset in assets:
        if asset.get("cluster_id"):
            asset_ids_by_cluster.setdefault(asset["cluster_id"], []).append(asset["id"])
    result = _map_batch(batch)
    role_counts = {"cover": 0, "gallery": 0, "review": 0, "excluded": 0}
    for asset in assets:
        role = asset.get("role")
        if role in role_counts:
            role_counts[role] += 1
    result["counts"] = {
        "total": len(assets),
        "processed": sum(asset.get("processed_at") is not None for asset in assets),
        **role_counts,
    }
    result["assets"] = [_map_asset(asset) for asset in assets]
    result["clusters"] = [
        _map_cluster(cluster, asset_ids_by_cluster.get(cluster["id"], []))
        for cluster in clusters
    ]
    review_asset_ids: dict[str, list[str]] = {}
    for link in review_asset_links:
        review_asset_ids.setdefault(link["draft_id"], []).append(link["asset_id"])
    result["reviewDrafts"] = [
        _map_review_draft(review, review_asset_ids.get(review["id"], []))
        for review in review_drafts
    ]
    result["jobs"] = [_map_job(job) for job in jobs]
    manifest = _load_json(batch.get("manifest_json"))
    if isinstance(manifest, dict):
        manifest["reviewDrafts"] = result["reviewDrafts"]
        for asset in manifest.get("assets", []):
            if isinstance(asset, dict):
                asset.pop("review", None)
    result["manifest"] = manifest
    return result


def lock_mutable_batch(
    cursor,
    batch_id: str,
    *,
    allowed_statuses: set[str] | frozenset[str] = frozenset(
        {"draft", "failed", "ready"}
    ),
) -> dict:
    cursor.execute(
        "SELECT * FROM travel_import_batches WHERE id = %s FOR UPDATE",
        (batch_id,),
    )
    batch = cursor.fetchone()
    if not batch:
        raise KeyError(batch_id)
    if batch["status"] not in allowed_statuses:
        raise ValueError(f"Batch status {batch['status']} does not allow this mutation")
    return batch


def delete_batch(batch_id: str) -> bool:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            lock_mutable_batch(cursor, batch_id)
            cursor.execute(
                "DELETE FROM travel_import_batches WHERE id = %s", (batch_id,)
            )
            return cursor.rowcount > 0


def add_uploaded_asset(
    *,
    batch_id: str,
    source_ref: str,
    original_name: str,
    media_type: str | None,
    byte_size: int,
    staging_key: str,
    cursor=None,
) -> str:
    asset_id = generate_id("asset")
    if cursor is not None:
        return _add_uploaded_asset(
            cursor,
            asset_id=asset_id,
            batch_id=batch_id,
            source_ref=source_ref,
            original_name=original_name,
            media_type=media_type,
            byte_size=byte_size,
            staging_key=staging_key,
        )
    with get_db_connection() as connection:
        with connection.cursor() as own_cursor:
            return _add_uploaded_asset(
                own_cursor,
                asset_id=asset_id,
                batch_id=batch_id,
                source_ref=source_ref,
                original_name=original_name,
                media_type=media_type,
                byte_size=byte_size,
                staging_key=staging_key,
            )


def _add_uploaded_asset(
    cursor,
    *,
    asset_id: str,
    batch_id: str,
    source_ref: str,
    original_name: str,
    media_type: str | None,
    byte_size: int,
    staging_key: str,
) -> str:
    lock_mutable_batch(
        cursor,
        batch_id,
        allowed_statuses=frozenset({"draft", "failed"}),
    )
    cursor.execute(
        """
        INSERT INTO travel_import_assets (
            id, batch_id, source_ref, original_name, media_type,
            byte_size, storage_kind, staging_key, classification,
            role, excluded
        ) VALUES (%s, %s, %s, %s, %s, %s, 's3', %s, 'pending',
                  'gallery', 0)
        ON DUPLICATE KEY UPDATE
            media_type = VALUES(media_type),
            byte_size = VALUES(byte_size),
            staging_key = VALUES(staging_key)
        """,
        (
            asset_id,
            batch_id,
            source_ref,
            original_name,
            media_type,
            byte_size,
            staging_key,
        ),
    )
    if cursor.rowcount != 1:
        cursor.execute(
            "SELECT id FROM travel_import_assets "
            "WHERE batch_id = %s AND source_ref = %s",
            (batch_id, source_ref),
        )
        asset_id = cursor.fetchone()["id"]
    return asset_id


def enqueue_process_job(batch_id: str) -> dict:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM travel_import_batches WHERE id = %s FOR UPDATE",
                (batch_id,),
            )
            batch = cursor.fetchone()
            if not batch:
                raise KeyError(batch_id)
            if batch["status"] in {"ready", "publishing", "published"}:
                raise ValueError(
                    "A reviewed or published batch cannot be processed again"
                )
            cursor.execute(
                """
                SELECT * FROM travel_import_jobs
                WHERE batch_id = %s AND job_type = 'process'
                  AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (batch_id,),
            )
            existing = cursor.fetchone()
            if existing:
                return _map_job(existing)
            job_id = generate_id("job")
            cursor.execute(
                """
                INSERT INTO travel_import_jobs (id, batch_id, job_type, status)
                VALUES (%s, %s, 'process', 'queued')
                """,
                (job_id, batch_id),
            )
            cursor.execute(
                """
                UPDATE travel_import_batches
                SET status = 'queued', progress_current = 0, error_text = NULL
                WHERE id = %s
                """,
                (batch_id,),
            )
            cursor.execute("SELECT * FROM travel_import_jobs WHERE id = %s", (job_id,))
            return _map_job(cursor.fetchone())


def claim_next_job(worker_id: str, stale_seconds: int) -> dict | None:
    stale_before = datetime.utcnow() - timedelta(seconds=stale_seconds)
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE travel_import_jobs
                SET status = 'queued', worker_id = NULL, claimed_at = NULL,
                    heartbeat_at = NULL,
                    error_text = 'Recovered after stale worker claim'
                WHERE status = 'running' AND heartbeat_at < %s
                """,
                (stale_before,),
            )
            cursor.execute(
                """
                SELECT id FROM travel_import_jobs
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1 FOR UPDATE
                """
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                """
                UPDATE travel_import_jobs
                SET status = 'running', worker_id = %s, claimed_at = UTC_TIMESTAMP(),
                    heartbeat_at = UTC_TIMESTAMP(), attempts = attempts + 1,
                    error_text = NULL
                WHERE id = %s AND status = 'queued'
                """,
                (worker_id, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            cursor.execute(
                "SELECT * FROM travel_import_jobs WHERE id = %s", (row["id"],)
            )
            job = cursor.fetchone()
            cursor.execute(
                """
                UPDATE travel_import_batches
                SET status = 'processing', error_text = NULL
                WHERE id = %s
                """,
                (job["batch_id"],),
            )
            return job


def update_job_progress(job_id: str, batch_id: str, current: int, total: int) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE travel_import_jobs
                SET progress_current = %s, progress_total = %s,
                    heartbeat_at = UTC_TIMESTAMP()
                WHERE id = %s
                """,
                (current, total, job_id),
            )
            cursor.execute(
                """
                UPDATE travel_import_batches
                SET progress_current = %s, progress_total = %s
                WHERE id = %s
                """,
                (current, total, batch_id),
            )


def complete_job(job_id: str, batch_id: str) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE travel_import_jobs
                SET status = 'completed', completed_at = UTC_TIMESTAMP(),
                    heartbeat_at = UTC_TIMESTAMP(),
                    progress_current = progress_total
                WHERE id = %s
                """,
                (job_id,),
            )
            cursor.execute(
                """
                UPDATE travel_import_batches
                SET status = 'ready', progress_current = progress_total,
                    error_text = NULL
                WHERE id = %s
                """,
                (batch_id,),
            )


def fail_job(job_id: str, batch_id: str, error_text: str) -> None:
    message = error_text[:65000]
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE travel_import_jobs
                SET status = 'failed', completed_at = UTC_TIMESTAMP(),
                    heartbeat_at = UTC_TIMESTAMP(), error_text = %s
                WHERE id = %s
                """,
                (message, job_id),
            )
            cursor.execute(
                "UPDATE travel_import_batches SET status = 'failed', error_text = %s "
                "WHERE id = %s",
                (message, batch_id),
            )


def _map_batch(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "sourceType": row["source_type"],
        "localRelativePath": row.get("source_path"),
        "status": row["status"],
        "progress": {
            "current": row.get("progress_current", 0),
            "total": row.get("progress_total", 0),
        },
        "error": row.get("error_text"),
        "oldestCapturedAt": _iso(row.get("oldest_captured_at")),
        "displayDate": _iso(row.get("oldest_captured_at")),
        "manifestVersion": row.get("manifest_version"),
        "publishEnabled": bool(
            TRAVEL_IMPORT_PUBLISH_ENABLED and row.get("status") == "ready"
        ),
        "counts": {
            "total": int(row.get("asset_count") or 0),
            "processed": int(row.get("processed_asset_count") or 0),
            "excluded": int(row.get("excluded_asset_count") or 0),
        },
        "ownerAccountId": row["owner_account_id"],
        "publishedAt": _iso(row.get("published_at")),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def _map_asset(row: dict) -> dict:
    thumbnail_available = bool(row.get("thumbnail_key"))
    return {
        "id": row["id"],
        "clusterId": row.get("cluster_id"),
        "originalName": row["original_name"],
        "mediaType": row.get("media_type"),
        "byteSize": row.get("byte_size"),
        "sha256": row.get("sha256"),
        "capturedAt": _iso(row.get("captured_at")),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "classification": row.get("classification"),
        "classificationReason": row.get("classification_reason"),
        "exclusionReason": row.get("manual_exclusion_reason"),
        "role": row.get("role"),
        "excluded": bool(row.get("excluded")),
        "duplicateOfAssetId": row.get("duplicate_of_asset_id"),
        "organizedPath": row.get("organized_path"),
        "previewAvailable": bool(row.get("preview_key") or row.get("staging_key")),
        "thumbnailAvailable": thumbnail_available,
        "thumbnailUrl": (
            f"/api/imports/{row['batch_id']}/assets/{row['id']}/thumbnail"
            if thumbnail_available
            else None
        ),
        "metadata": _load_json(row.get("metadata_json")),
        "processedAt": _iso(row.get("processed_at")),
    }


def _map_cluster(row: dict, asset_ids: list[str]) -> dict:
    return {
        "id": row["id"],
        "representativeAssetId": row.get("representative_asset_id"),
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "countryCode": row.get("country_code"),
        "country": row.get("country"),
        "city": row.get("city"),
        "address": row.get("address"),
        "suggestedName": row.get("suggested_name"),
        "mapLink": row.get("map_link"),
        "geocodingStatus": (
            "resolved" if row.get("address") or row.get("country_code") else "idle"
        ),
        "draft": {
            "name": row.get("draft_name"),
            "category": row.get("draft_category"),
            "address": row.get("draft_address"),
            "description": row.get("draft_description"),
            "openingHours": row.get("draft_opening_hours"),
            "specialNotes": row.get("draft_special_notes"),
            "tags": _load_json(row.get("draft_tags_json")) or [],
            "visibility": row.get("draft_visibility"),
        },
        "publishAction": row.get("publish_action"),
        "existingPlaceId": row.get("existing_place_id"),
        "publishedPlaceId": row.get("published_place_id"),
        "assetIds": sorted(asset_ids),
    }


def _map_review_draft(row: dict, asset_ids: list[str]) -> dict:
    return {
        "id": row["id"],
        "batchId": row["batch_id"],
        "clusterId": row["cluster_id"],
        "rating": row.get("rating"),
        "headline": row.get("headline"),
        "body": row.get("body"),
        "visitedAt": _iso(row.get("visited_at")),
        "assetIds": sorted(asset_ids),
        "publishedReviewId": row.get("published_review_id"),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
    }


def _map_job(row: dict) -> dict:
    return {
        "id": row["id"],
        "type": row["job_type"],
        "status": row["status"],
        "progress": {
            "current": row.get("progress_current", 0),
            "total": row.get("progress_total", 0),
        },
        "attempts": row.get("attempts", 0),
        "error": row.get("error_text"),
        "createdAt": _iso(row.get("created_at")),
        "completedAt": _iso(row.get("completed_at")),
    }


def _load_json(value: Any) -> Any:
    if not value:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None
