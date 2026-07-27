from __future__ import annotations

from dataclasses import dataclass

from ..connectors import get_db_connection
from ..utils import dump_json, generate_id
from .authorization import can_manage_place
from .import_repository import lock_mutable_batch
from .import_review_drafts import detach_review_assets


@dataclass(frozen=True)
class ImportAssignmentError(Exception):
    status_code: int
    detail: str


def assign_assets_to_cluster(
    *, batch_id: str, cluster_id: str, asset_ids: list[str]
) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_batch(cursor, batch_id)
            cursor.execute(
                "SELECT id FROM travel_import_clusters "
                "WHERE id = %s AND batch_id = %s FOR UPDATE",
                (cluster_id, batch_id),
            )
            if not cursor.fetchone():
                raise ImportAssignmentError(404, "Import cluster not found")
            assets = _lock_assets(cursor, batch_id, asset_ids)
            selected_covers = [
                asset["id"] for asset in assets if asset["role"] == "cover"
            ]
            if len(selected_covers) > 1:
                raise ImportAssignmentError(
                    422, "assetIds may contain at most one cover-role asset"
                )
            placeholders = ",".join(["%s"] * len(asset_ids))
            _clear_moved_asset_representatives(
                cursor,
                batch_id=batch_id,
                destination_cluster_id=cluster_id,
                asset_ids=asset_ids,
            )
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = %s "
                f"WHERE batch_id = %s AND id IN ({placeholders})",
                (cluster_id, batch_id, *asset_ids),
            )
            detach_review_assets(
                cursor,
                asset_ids=asset_ids,
                retained_cluster_id=cluster_id,
            )
            if selected_covers:
                synchronize_cluster_representative(
                    cursor,
                    batch_id=batch_id,
                    cluster_id=cluster_id,
                    representative_asset_id=selected_covers[0],
                )


def unassign_assets(*, batch_id: str, asset_ids: list[str]) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_batch(cursor, batch_id)
            _lock_assets(cursor, batch_id, asset_ids)
            placeholders = ",".join(["%s"] * len(asset_ids))
            _clear_moved_asset_representatives(
                cursor,
                batch_id=batch_id,
                destination_cluster_id=None,
                asset_ids=asset_ids,
            )
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = NULL "
                f"WHERE batch_id = %s AND id IN ({placeholders})",
                (batch_id, *asset_ids),
            )
            detach_review_assets(
                cursor,
                asset_ids=asset_ids,
            )


def create_reassignment_cluster(*, batch_id: str, asset_ids: list[str]) -> str:
    cluster_id = generate_id("cluster")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_batch(cursor, batch_id)
            assets = _lock_assets(cursor, batch_id, asset_ids)
            selected_covers = [
                asset["id"] for asset in assets if asset["role"] == "cover"
            ]
            if len(selected_covers) > 1:
                raise ImportAssignmentError(
                    422, "assetIds may contain at most one cover-role asset"
                )
            coordinates = [
                (asset.get("latitude"), asset.get("longitude")) for asset in assets
            ]
            if any(
                latitude is None or longitude is None
                for latitude, longitude in coordinates
            ):
                raise ImportAssignmentError(
                    422, "All selected assets must have coordinates"
                )
            latitude = sum(float(point[0]) for point in coordinates) / len(coordinates)
            longitude = sum(float(point[1]) for point in coordinates) / len(coordinates)
            cursor.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_sort_order "
                "FROM travel_import_clusters WHERE batch_id = %s",
                (batch_id,),
            )
            sort_order = cursor.fetchone()["next_sort_order"]
            _clear_moved_asset_representatives(
                cursor,
                batch_id=batch_id,
                destination_cluster_id=None,
                asset_ids=asset_ids,
            )
            cursor.execute(
                """
                INSERT INTO travel_import_clusters (
                    id, batch_id, sort_order, representative_asset_id,
                    latitude, longitude, draft_category, draft_visibility,
                    publish_action
                ) VALUES (%s, %s, %s, NULL, %s, %s, 'other', 'public', 'create')
                """,
                (cluster_id, batch_id, sort_order, latitude, longitude),
            )
            placeholders = ",".join(["%s"] * len(asset_ids))
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = %s "
                f"WHERE batch_id = %s AND id IN ({placeholders})",
                (cluster_id, batch_id, *asset_ids),
            )
            detach_review_assets(
                cursor,
                asset_ids=asset_ids,
                retained_cluster_id=cluster_id,
            )
            synchronize_cluster_representative(
                cursor,
                batch_id=batch_id,
                cluster_id=cluster_id,
                representative_asset_id=(
                    selected_covers[0] if selected_covers else None
                ),
            )
    return cluster_id


def create_cluster_with_assets(
    *,
    batch_id: str,
    asset_ids: list[str],
    latitude: float,
    longitude: float,
    name: str | None,
    category: str | None,
    address: str | None,
    description: str | None,
    opening_hours: str | None,
    special_notes: str | None,
    tags: list[str] | None,
    visibility: str | None,
    map_link: str | None,
    publish_action: str | None,
    existing_place_id: str | None,
    representative_asset_id: str | None,
    suggested_name: str | None,
    resolved_address: str | None,
    user: dict,
) -> str:
    cluster_id = generate_id("cluster")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_batch(cursor, batch_id)
            assets = _lock_assets(cursor, batch_id, asset_ids)
            assigned_ids = [
                asset["id"] for asset in assets if asset["cluster_id"] is not None
            ]
            if assigned_ids:
                raise ImportAssignmentError(
                    409,
                    "New clusters can only be created from currently unassigned assets",
                )
            selected_covers = [
                asset["id"] for asset in assets if asset["role"] == "cover"
            ]
            if len(selected_covers) > 1:
                raise ImportAssignmentError(
                    422, "assetIds may contain at most one cover-role asset"
                )
            if representative_asset_id and representative_asset_id not in set(
                asset_ids
            ):
                raise ImportAssignmentError(
                    422, "Representative asset must be one of the selected assets"
                )
            selected_by_id = {asset["id"]: asset for asset in assets}
            if representative_asset_id and selected_by_id[representative_asset_id][
                "role"
            ] not in {"gallery", "cover"}:
                raise ImportAssignmentError(
                    422, "Representative asset must have gallery or cover role"
                )
            effective_action = publish_action or "create"
            if effective_action == "merge":
                if not existing_place_id:
                    raise ImportAssignmentError(
                        422, "existingPlaceId is required for merge"
                    )
                _require_manageable_place(cursor, existing_place_id, user)
            cursor.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_sort_order "
                "FROM travel_import_clusters WHERE batch_id = %s",
                (batch_id,),
            )
            sort_order = cursor.fetchone()["next_sort_order"]
            cursor.execute(
                """
                INSERT INTO travel_import_clusters (
                    id, batch_id, sort_order, representative_asset_id,
                    latitude, longitude, address, suggested_name,
                    draft_name, draft_category, draft_address, draft_description,
                    draft_opening_hours, draft_special_notes, draft_tags_json,
                    draft_visibility, map_link, publish_action, existing_place_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cluster_id,
                    batch_id,
                    sort_order,
                    None,
                    latitude,
                    longitude,
                    resolved_address,
                    suggested_name,
                    name,
                    category,
                    address,
                    description,
                    opening_hours,
                    special_notes,
                    dump_json(tags) if tags is not None else None,
                    visibility or "public",
                    map_link,
                    effective_action,
                    existing_place_id,
                ),
            )
            placeholders = ",".join(["%s"] * len(asset_ids))
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = %s "
                f"WHERE batch_id = %s AND cluster_id IS NULL "
                f"AND id IN ({placeholders})",
                (cluster_id, batch_id, *asset_ids),
            )
            if cursor.rowcount != len(asset_ids):
                raise ImportAssignmentError(
                    409, "One or more assets are no longer unassigned"
                )
            effective_representative_id = representative_asset_id or (
                selected_covers[0] if selected_covers else None
            )
            synchronize_cluster_representative(
                cursor,
                batch_id=batch_id,
                cluster_id=cluster_id,
                representative_asset_id=effective_representative_id,
            )
    return cluster_id


def demote_other_cluster_covers(
    cursor, *, batch_id: str, cluster_id: str, keep_asset_id: str
) -> None:
    cursor.execute(
        "UPDATE travel_import_assets SET role = 'gallery' "
        "WHERE batch_id = %s AND cluster_id = %s "
        "AND role = 'cover' AND id <> %s",
        (batch_id, cluster_id, keep_asset_id),
    )


def synchronize_cluster_representative(
    cursor,
    *,
    batch_id: str,
    cluster_id: str,
    representative_asset_id: str | None,
) -> None:
    if representative_asset_id is None:
        cursor.execute(
            "UPDATE travel_import_assets SET role = 'gallery' "
            "WHERE batch_id = %s AND cluster_id = %s AND role = 'cover'",
            (batch_id, cluster_id),
        )
    else:
        demote_other_cluster_covers(
            cursor,
            batch_id=batch_id,
            cluster_id=cluster_id,
            keep_asset_id=representative_asset_id,
        )
        cursor.execute(
            "UPDATE travel_import_assets "
            "SET role = 'cover', excluded = 0, manual_exclusion_reason = NULL "
            "WHERE id = %s AND batch_id = %s AND cluster_id = %s",
            (representative_asset_id, batch_id, cluster_id),
        )
    cursor.execute(
        "UPDATE travel_import_clusters SET representative_asset_id = %s "
        "WHERE id = %s AND batch_id = %s",
        (representative_asset_id, cluster_id, batch_id),
    )


def _clear_moved_asset_representatives(
    cursor,
    *,
    batch_id: str,
    destination_cluster_id: str | None,
    asset_ids: list[str],
) -> None:
    placeholders = ",".join(["%s"] * len(asset_ids))
    destination_clause = ""
    parameters: list[str] = [batch_id, *asset_ids]
    if destination_cluster_id is not None:
        destination_clause = " AND id <> %s"
        parameters.append(destination_cluster_id)
    cursor.execute(
        f"UPDATE travel_import_clusters SET representative_asset_id = NULL "
        f"WHERE batch_id = %s AND representative_asset_id IN ({placeholders})"
        f"{destination_clause}",
        tuple(parameters),
    )


def _lock_batch(cursor, batch_id: str) -> dict:
    try:
        return lock_mutable_batch(cursor, batch_id)
    except KeyError as error:
        raise ImportAssignmentError(404, "Import batch not found") from error
    except ValueError as error:
        raise ImportAssignmentError(
            409,
            "Queued, processing, publishing, or published batches are immutable",
        ) from error


def _lock_assets(cursor, batch_id: str, asset_ids: list[str]) -> list[dict]:
    placeholders = ",".join(["%s"] * len(asset_ids))
    cursor.execute(
        f"SELECT id, cluster_id, role, latitude, longitude "
        f"WHERE batch_id = %s AND id IN ({placeholders}) FOR UPDATE",
        (batch_id, *asset_ids),
    )
    assets = cursor.fetchall()
    if {asset["id"] for asset in assets} != set(asset_ids):
        raise ImportAssignmentError(
            422, "One or more assets do not belong to this batch"
        )
    return assets


def _require_manageable_place(cursor, place_id: str, user: dict) -> None:
    cursor.execute("SELECT * FROM travel_places WHERE id = %s", (place_id,))
    place = cursor.fetchone()
    if not place or not can_manage_place(user, place):
        raise ImportAssignmentError(404, "Merge target not found")
