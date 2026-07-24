from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth_utils import require_admin
from ..config import (
    TRAVEL_IMPORT_MAX_UPLOAD_BYTES,
    TRAVEL_IMPORT_PUBLISH_ENABLED,
    import_local_root,
    import_output_root,
)
from ..connectors import get_db_connection
from ..import_schemas import (
    ImportAssetIdsRequest,
    ImportAssetPatchRequest,
    ImportBatchCreateRequest,
    ImportClusterCreateRequest,
    ImportClusterDraftPatchRequest,
    ImportClusterMergeRequest,
    ImportClusterSplitRequest,
    ImportReviewDraftCreateRequest,
    ImportReviewDraftPatchRequest,
)
from ..services.import_contract import (
    GeoPoint,
    build_manifest,
    open_confined_file,
    resolve_local_source,
    safe_segment,
    validate_cluster_radius,
)
from ..services.import_cluster_assignments import (
    ImportAssignmentError,
    assign_assets_to_cluster,
    create_cluster_with_assets,
    synchronize_cluster_representative,
    unassign_assets,
)
from ..services.authorization import can_manage_place
from ..services.import_repository import (
    add_uploaded_asset,
    create_batch,
    delete_batch,
    enqueue_process_job,
    get_batch_detail,
    get_batch_row,
    list_batches,
    lock_mutable_batch,
)
from ..services.import_review_drafts import (
    detach_review_assets,
    lock_review_draft_ids_for_assets,
    refresh_review_draft_visited_at,
)
from ..services.media import register_attached_object
from ..services.storage import (
    copy_object,
    delete_object,
    delete_prefix,
    get_object,
    upload_fileobj_to_key,
)
from ..services.place_links import (
    PlaceLinkError,
    resolve_place_link as resolve_supported_place_link,
)
from ..utils import dump_json, generate_id, parse_json_list, to_mysql_datetime

router = APIRouter(
    prefix="/api/imports",
    tags=["imports"],
    dependencies=[Depends(require_admin)],
)

_BROWSER_SAFE_PREVIEW_TYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@router.get("")
async def get_import_batches():
    return list_batches()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_import_batch(
    body: ImportBatchCreateRequest,
    user: dict = Depends(require_admin),
):
    source_path = None
    if body.sourceType == "local":
        try:
            source = resolve_local_source(
                import_local_root(), body.localRelativePath or ""
            )
            output_root = import_output_root()
            if output_root is None:
                raise ValueError("Local photo import output root is not configured")
            resolved_output = output_root.resolve()
            if resolved_output == source or resolved_output.is_relative_to(source):
                raise ValueError(
                    "Local import output root must be outside the source directory"
                )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source_path = body.localRelativePath
    return create_batch(
        name=body.name.strip(),
        source_type=body.sourceType,
        source_path=source_path,
        user=user,
    )


@router.post("/{batch_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_import_files(
    batch_id: str,
    files: list[UploadFile] = File(...),
):
    batch = _require_batch(batch_id)
    if batch["source_type"] != "upload":
        raise HTTPException(
            status_code=409, detail="Files can only be uploaded to upload batches"
        )
    if batch["status"] not in {"draft", "failed"}:
        raise HTTPException(
            status_code=409, detail="This batch no longer accepts files"
        )
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    declared_total = sum(file.size or 0 for file in files)
    if declared_total > TRAVEL_IMPORT_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail="Upload request exceeds configured limit"
        )
    scanned_files = []
    actual_total = 0
    for file in files:
        filename = Path(file.filename or "upload").name
        if filename in {"", ".", ".."}:
            raise HTTPException(status_code=400, detail="Invalid upload filename")
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            actual_total += len(chunk)
            if actual_total > TRAVEL_IMPORT_MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413, detail="Upload request exceeds configured limit"
                )
        await file.seek(0)
        scanned_files.append((file, filename, size))

    uploaded = []
    for file, filename, size in scanned_files:
        upload_token = generate_id("upload")
        key = f"imports/{batch_id}/staging/{upload_token}/{safe_segment(filename, 'upload')}"
        upload_fileobj_to_key(
            file.file,
            key,
            file.content_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
        )
        try:
            asset_id = add_uploaded_asset(
                batch_id=batch_id,
                source_ref=f"upload:{upload_token}:{filename}",
                original_name=filename,
                media_type=file.content_type,
                byte_size=size,
                staging_key=key,
            )
        except (KeyError, ValueError) as exc:
            try:
                delete_object(key)
            except Exception:
                pass
            raise HTTPException(
                status_code=409, detail="This batch no longer accepts files"
            ) from exc
        uploaded.append({"id": asset_id, "originalName": filename, "byteSize": size})
    return {"batchId": batch_id, "files": uploaded}


@router.post("/{batch_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def process_import_batch(batch_id: str):
    batch = _require_batch(batch_id)
    if batch["status"] in {"ready", "publishing", "published"}:
        raise HTTPException(
            status_code=409,
            detail="Reviewed or published batches cannot be reprocessed",
        )
    if batch["source_type"] == "upload":
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM travel_import_assets WHERE batch_id = %s",
                    (batch_id,),
                )
                if cursor.fetchone()["count"] == 0:
                    raise HTTPException(
                        status_code=409, detail="Upload at least one file first"
                    )
    try:
        return enqueue_process_job(batch_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{batch_id}")
async def get_import_batch(batch_id: str):
    try:
        return get_batch_detail(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Import batch not found") from exc


@router.get("/{batch_id}/manifest")
async def export_import_manifest(batch_id: str):
    detail = await get_import_batch(batch_id)
    if detail.get("manifest") is None:
        raise HTTPException(
            status_code=409,
            detail="Manifest is not available until processing completes",
        )
    return JSONResponse(
        detail["manifest"],
        headers={
            "Content-Disposition": f'attachment; filename="{batch_id}-travel-import.v1.json"'
        },
    )


@router.delete("/{batch_id}")
async def remove_import_batch(batch_id: str):
    try:
        deleted = delete_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Import batch not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Queued, processing, publishing, or published batches cannot be deleted",
        ) from exc
    delete_prefix(f"imports/{batch_id}/staging/")
    delete_prefix(f"imports/{batch_id}/previews/")
    delete_prefix(f"imports/{batch_id}/thumbnails/")
    delete_prefix(f"imports/{batch_id}/organized/")
    return {"deleted": deleted}


@router.get("/{batch_id}/assets/{asset_id}/preview")
async def get_import_asset_preview(batch_id: str, asset_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM travel_import_assets WHERE id = %s AND batch_id = %s",
                (asset_id, batch_id),
            )
            asset = cursor.fetchone()
    if not asset:
        raise HTTPException(status_code=404, detail="Import asset not found")
    key = asset.get("preview_key")
    suffix = Path(asset["original_name"]).suffix.lower()
    content_type = "image/jpeg" if key else _BROWSER_SAFE_PREVIEW_TYPES.get(suffix)
    if not key and suffix in {".heic", ".heif"}:
        raise HTTPException(
            status_code=409, detail="HEIC preview generation was unsuccessful"
        )
    if not content_type:
        raise HTTPException(
            status_code=415, detail="This file has no browser-safe preview"
        )
    security_headers = {
        "Cache-Control": "private, max-age=3600",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "X-Content-Type-Options": "nosniff",
    }
    if key or asset["storage_kind"] == "s3":
        object_key = key or asset.get("staging_key")
        if not object_key:
            raise HTTPException(status_code=404, detail="Preview object not found")
        response = get_object(object_key)
        return StreamingResponse(
            response["Body"],
            media_type=content_type
            or response.get("ContentType")
            or "application/octet-stream",
            headers=security_headers,
        )
    path = Path(asset.get("local_source_path") or "")
    root = import_local_root()
    if root is None:
        raise HTTPException(status_code=404, detail="Local preview file not found")
    try:
        with open_confined_file(root, path):
            pass
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404, detail="Local preview file not found"
        ) from exc
    return StreamingResponse(
        _stream_confined_file(root, path),
        media_type=content_type,
        headers=security_headers,
    )


@router.get("/{batch_id}/assets/{asset_id}/thumbnail")
async def get_import_asset_thumbnail(batch_id: str, asset_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT thumbnail_key FROM travel_import_assets "
                "WHERE id = %s AND batch_id = %s",
                (asset_id, batch_id),
            )
            asset = cursor.fetchone()
    if not asset:
        raise HTTPException(status_code=404, detail="Import asset not found")
    thumbnail_key = asset.get("thumbnail_key")
    if not thumbnail_key:
        raise HTTPException(status_code=404, detail="Import asset thumbnail not found")

    response = get_object(thumbnail_key)
    return StreamingResponse(
        response["Body"],
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": 'inline; filename="thumbnail.jpg"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/{batch_id}/assets/{asset_id}")
async def patch_import_asset(
    batch_id: str, asset_id: str, body: ImportAssetPatchRequest
):
    _require_batch(batch_id)
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            cursor.execute(
                "SELECT * FROM travel_import_assets WHERE id = %s AND batch_id = %s",
                (asset_id, batch_id),
            )
            asset = cursor.fetchone()
            if not asset:
                raise HTTPException(status_code=404, detail="Import asset not found")
            if (
                body.role
                and asset["classification"] in {"screenshot", "archive"}
                and body.role != "excluded"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Screenshots and archives must remain excluded",
                )
            effective_cluster_id = asset.get("cluster_id")
            if "clusterId" in body.model_fields_set:
                effective_cluster_id = body.clusterId
                if body.clusterId is not None:
                    cursor.execute(
                        "SELECT id FROM travel_import_clusters WHERE id = %s AND batch_id = %s",
                        (body.clusterId, batch_id),
                    )
                    if not cursor.fetchone():
                        raise HTTPException(
                            status_code=422,
                            detail="Cluster does not belong to this batch",
                        )
            effective_role = body.role or asset["role"]
            if body.role:
                cursor.execute(
                    "UPDATE travel_import_assets SET role = %s, excluded = %s, "
                    "manual_exclusion_reason = %s WHERE id = %s",
                    (
                        body.role,
                        int(body.role == "excluded"),
                        body.exclusionReason if body.role == "excluded" else None,
                        asset_id,
                    ),
                )
            elif "exclusionReason" in body.model_fields_set:
                cursor.execute(
                    "UPDATE travel_import_assets SET manual_exclusion_reason = %s WHERE id = %s",
                    (body.exclusionReason, asset_id),
                )
            if "clusterId" in body.model_fields_set:
                cursor.execute(
                    "UPDATE travel_import_assets SET cluster_id = %s WHERE id = %s",
                    (body.clusterId, asset_id),
                )
                source_cluster_id = asset.get("cluster_id")
                if source_cluster_id != effective_cluster_id and source_cluster_id:
                    cursor.execute(
                        "UPDATE travel_import_clusters "
                        "SET representative_asset_id = NULL "
                        "WHERE id = %s AND batch_id = %s "
                        "AND representative_asset_id = %s",
                        (source_cluster_id, batch_id, asset_id),
                    )
            if effective_role == "cover" and effective_cluster_id is not None:
                synchronize_cluster_representative(
                    cursor,
                    batch_id=batch_id,
                    cluster_id=effective_cluster_id,
                    representative_asset_id=asset_id,
                )
            elif body.role is not None and effective_cluster_id is not None:
                cursor.execute(
                    "UPDATE travel_import_clusters "
                    "SET representative_asset_id = NULL "
                    "WHERE id = %s AND batch_id = %s "
                    "AND representative_asset_id = %s",
                    (effective_cluster_id, batch_id, asset_id),
                )
            if "clusterId" in body.model_fields_set or (
                body.role is not None and body.role != "review"
            ):
                detach_review_assets(
                    cursor,
                    asset_ids=[asset_id],
                    retained_cluster_id=(
                        effective_cluster_id if effective_role == "review" else None
                    ),
                )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post(
    "/{batch_id}/clusters/{cluster_id}/reviews",
    status_code=status.HTTP_201_CREATED,
)
async def create_import_review_draft(
    batch_id: str,
    cluster_id: str,
    body: ImportReviewDraftCreateRequest,
):
    draft_id = generate_id("draft")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            _lock_review_cluster(cursor, batch_id, cluster_id)
            _lock_review_assets(
                cursor,
                batch_id=batch_id,
                cluster_id=cluster_id,
                asset_ids=body.assetIds,
            )
            cursor.execute(
                """
                INSERT INTO travel_import_review_drafts (
                    id, batch_id, cluster_id, rating, headline, body, visited_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    draft_id,
                    batch_id,
                    cluster_id,
                    body.rating,
                    body.headline,
                    body.body,
                    to_mysql_datetime(body.visitedAt),
                ),
            )
            _replace_review_draft_assets(
                cursor,
                draft_id=draft_id,
                asset_ids=body.assetIds,
            )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.patch("/{batch_id}/reviews/{review_id}")
async def patch_import_review_draft(
    batch_id: str,
    review_id: str,
    body: ImportReviewDraftPatchRequest,
):
    _require_batch(batch_id)
    values = body.model_dump(exclude_unset=True)
    if not values:
        return get_batch_detail(batch_id)
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            cursor.execute(
                "SELECT * FROM travel_import_review_drafts "
                "WHERE id = %s AND batch_id = %s AND cluster_id IS NOT NULL "
                "FOR UPDATE",
                (review_id, batch_id),
            )
            review = cursor.fetchone()
            if not review:
                raise HTTPException(
                    status_code=404, detail="Import review draft not found"
                )
            asset_ids = values.pop("assetIds", None)
            column_map = {
                "rating": "rating",
                "headline": "headline",
                "body": "body",
                "visitedAt": "visited_at",
            }
            if values:
                clauses = [f"{column_map[field]} = %s" for field in values]
                parameters = [
                    to_mysql_datetime(value) if field == "visitedAt" else value
                    for field, value in values.items()
                ]
                cursor.execute(
                    f"UPDATE travel_import_review_drafts SET {', '.join(clauses)} "
                    "WHERE id = %s",
                    (*parameters, review_id),
                )
            if asset_ids is not None:
                _lock_review_assets(
                    cursor,
                    batch_id=batch_id,
                    cluster_id=review["cluster_id"],
                    asset_ids=asset_ids,
                )
                _replace_review_draft_assets(
                    cursor,
                    draft_id=review_id,
                    asset_ids=asset_ids,
                )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.delete("/{batch_id}/reviews/{review_id}")
async def delete_import_review_draft(batch_id: str, review_id: str):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            cursor.execute(
                "DELETE FROM travel_import_review_drafts "
                "WHERE id = %s AND batch_id = %s AND cluster_id IS NOT NULL",
                (review_id, batch_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(
                    status_code=404, detail="Import review draft not found"
                )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post(
    "/{batch_id}/clusters/{cluster_id}/assets/assign",
)
async def assign_import_cluster_assets(
    batch_id: str,
    cluster_id: str,
    body: ImportAssetIdsRequest,
):
    try:
        assign_assets_to_cluster(
            batch_id=batch_id,
            cluster_id=cluster_id,
            asset_ids=body.assetIds,
        )
    except ImportAssignmentError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.detail
        ) from error
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post("/{batch_id}/assets/unassign")
async def unassign_import_assets(
    batch_id: str,
    body: ImportAssetIdsRequest,
):
    try:
        unassign_assets(batch_id=batch_id, asset_ids=body.assetIds)
    except ImportAssignmentError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.detail
        ) from error
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post("/{batch_id}/clusters", status_code=status.HTTP_201_CREATED)
async def create_import_cluster(
    batch_id: str,
    body: ImportClusterCreateRequest,
    request: Request,
    user: dict = Depends(require_admin),
):
    location = await _resolve_new_cluster_location(body, request)
    try:
        create_cluster_with_assets(
            batch_id=batch_id,
            asset_ids=body.assetIds,
            latitude=location["latitude"],
            longitude=location["longitude"],
            name=body.name,
            category=body.category,
            address=body.address,
            description=body.description,
            visibility=body.visibility,
            map_link=location["map_link"],
            publish_action=body.publishAction,
            existing_place_id=body.existingPlaceId,
            representative_asset_id=body.representativeAssetId,
            suggested_name=location["suggested_name"],
            resolved_address=location["resolved_address"],
            user=user,
        )
    except ImportAssignmentError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.detail
        ) from error
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.patch("/{batch_id}/clusters/{cluster_id}")
async def patch_import_cluster(
    batch_id: str,
    cluster_id: str,
    body: ImportClusterDraftPatchRequest,
    user: dict = Depends(require_admin),
):
    _require_batch(batch_id)
    values = body.model_dump(exclude_unset=True)
    if not values:
        return get_batch_detail(batch_id)
    representative_was_set = "representativeAssetId" in values
    representative_id = values.pop("representativeAssetId", None)
    column_map = {
        "name": "draft_name",
        "category": "draft_category",
        "address": "draft_address",
        "description": "draft_description",
        "visibility": "draft_visibility",
        "publishAction": "publish_action",
        "existingPlaceId": "existing_place_id",
        "latitude": "latitude",
        "longitude": "longitude",
        "mapLink": "map_link",
    }
    clauses = [f"{column_map[field]} = %s" for field in values]
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            cursor.execute(
                "SELECT publish_action, existing_place_id FROM travel_import_clusters "
                "WHERE id = %s AND batch_id = %s",
                (cluster_id, batch_id),
            )
            current = cursor.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Import cluster not found")
            effective_action = values.get("publishAction", current["publish_action"])
            effective_target = values.get(
                "existingPlaceId", current["existing_place_id"]
            )
            if effective_action == "merge" and not effective_target:
                raise HTTPException(
                    status_code=422, detail="existingPlaceId is required for merge"
                )
            if effective_action == "merge":
                _require_manageable_place(cursor, effective_target, user)
            if representative_was_set and representative_id is not None:
                cursor.execute(
                    "SELECT id, role FROM travel_import_assets "
                    "WHERE id = %s AND batch_id = %s AND cluster_id = %s",
                    (representative_id, batch_id, cluster_id),
                )
                representative_asset = cursor.fetchone()
                if not representative_asset:
                    raise HTTPException(
                        status_code=422,
                        detail="Representative asset must belong to the cluster",
                    )
                if representative_asset["role"] not in {"gallery", "cover"}:
                    raise HTTPException(
                        status_code=422,
                        detail="Representative asset must have gallery or cover role",
                    )
            if clauses:
                cursor.execute(
                    f"UPDATE travel_import_clusters SET {', '.join(clauses)} "
                    "WHERE id = %s AND batch_id = %s",
                    (*values.values(), cluster_id, batch_id),
                )
            if representative_was_set:
                synchronize_cluster_representative(
                    cursor,
                    batch_id=batch_id,
                    cluster_id=cluster_id,
                    representative_asset_id=representative_id,
                )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post("/{batch_id}/clusters/merge")
async def merge_import_clusters(batch_id: str, body: ImportClusterMergeRequest):
    _require_batch(batch_id)
    cluster_ids = sorted(set(body.clusterIds))
    if len(cluster_ids) < 2:
        raise HTTPException(
            status_code=422, detail="At least two distinct clusters are required"
        )
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            placeholders = ",".join(["%s"] * len(cluster_ids))
            cursor.execute(
                f"SELECT * FROM travel_import_clusters WHERE batch_id = %s "
                f"AND id IN ({placeholders}) ORDER BY sort_order, id",
                (batch_id, *cluster_ids),
            )
            clusters = cursor.fetchall()
            if len(clusters) != len(cluster_ids):
                raise HTTPException(
                    status_code=404, detail="One or more clusters were not found"
                )
            cursor.execute(
                f"SELECT id, latitude, longitude FROM travel_import_assets "
                f"WHERE batch_id = %s AND cluster_id IN ({placeholders})",
                (batch_id, *cluster_ids),
            )
            point_rows = cursor.fetchall()
            points = [
                GeoPoint(row["id"], row["latitude"], row["longitude"])
                for row in point_rows
            ]
            try:
                representative = validate_cluster_radius(points)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            target_id = clusters[0]["id"]
            target_representative_id = clusters[0].get("representative_asset_id")
            if target_representative_id not in {row["id"] for row in point_rows}:
                target_representative_id = None
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = %s "
                f"WHERE batch_id = %s AND cluster_id IN ({placeholders})",
                (target_id, batch_id, *cluster_ids),
            )
            cursor.execute(
                """
                UPDATE travel_import_clusters
                SET latitude = %s, longitude = %s
                WHERE id = %s
                """,
                (representative.latitude, representative.longitude, target_id),
            )
            synchronize_cluster_representative(
                cursor,
                batch_id=batch_id,
                cluster_id=target_id,
                representative_asset_id=target_representative_id,
            )
            _move_cluster_review_drafts(
                cursor,
                batch_id=batch_id,
                cluster_ids=cluster_ids,
                target_cluster_id=target_id,
            )
            delete_ids = [
                cluster_id for cluster_id in cluster_ids if cluster_id != target_id
            ]
            delete_placeholders = ",".join(["%s"] * len(delete_ids))
            cursor.execute(
                f"DELETE FROM travel_import_clusters WHERE id IN ({delete_placeholders})",
                delete_ids,
            )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post("/{batch_id}/clusters/split")
async def split_import_cluster(batch_id: str, body: ImportClusterSplitRequest):
    _require_batch(batch_id)
    selected_ids = sorted(set(body.assetIds))
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            _lock_draft_mutation(cursor, batch_id)
            cursor.execute(
                "SELECT * FROM travel_import_clusters WHERE id = %s AND batch_id = %s",
                (body.clusterId, batch_id),
            )
            cluster = cursor.fetchone()
            if not cluster:
                raise HTTPException(status_code=404, detail="Import cluster not found")
            cursor.execute(
                "SELECT id, latitude, longitude FROM travel_import_assets "
                "WHERE batch_id = %s AND cluster_id = %s ORDER BY id",
                (batch_id, body.clusterId),
            )
            rows = cursor.fetchall()
            row_by_id = {row["id"]: row for row in rows}
            if any(asset_id not in row_by_id for asset_id in selected_ids):
                raise HTTPException(
                    status_code=422, detail="Split assets must belong to the cluster"
                )
            remaining_ids = sorted(set(row_by_id) - set(selected_ids))
            if not remaining_ids:
                raise HTTPException(
                    status_code=422,
                    detail="Split must leave assets in the original cluster",
                )
            selected_points = [
                GeoPoint(
                    row_by_id[item]["id"],
                    row_by_id[item]["latitude"],
                    row_by_id[item]["longitude"],
                )
                for item in selected_ids
            ]
            remaining_points = [
                GeoPoint(
                    row_by_id[item]["id"],
                    row_by_id[item]["latitude"],
                    row_by_id[item]["longitude"],
                )
                for item in remaining_ids
            ]
            try:
                selected_rep = validate_cluster_radius(selected_points)
                remaining_rep = validate_cluster_radius(remaining_points)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            new_cluster_id = generate_id("cluster")
            original_representative_id = cluster.get("representative_asset_id")
            if original_representative_id not in remaining_ids:
                original_representative_id = None
            cursor.execute(
                """
                INSERT INTO travel_import_clusters (
                    id, batch_id, sort_order, representative_asset_id,
                    latitude, longitude, country_code, country, city, address,
                    suggested_name, draft_name, draft_category, draft_address,
                    draft_description, draft_visibility, publish_action
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s, 'create')
                """,
                (
                    new_cluster_id,
                    batch_id,
                    cluster["sort_order"] + 1,
                    None,
                    selected_rep.latitude,
                    selected_rep.longitude,
                    cluster.get("country_code"),
                    cluster.get("country"),
                    cluster.get("city"),
                    cluster.get("address"),
                    cluster.get("suggested_name"),
                    cluster.get("draft_name"),
                    cluster.get("draft_category"),
                    cluster.get("draft_address"),
                    cluster.get("draft_description"),
                    cluster.get("draft_visibility"),
                ),
            )
            selected_placeholders = ",".join(["%s"] * len(selected_ids))
            cursor.execute(
                f"UPDATE travel_import_assets SET cluster_id = %s "
                f"WHERE id IN ({selected_placeholders})",
                (new_cluster_id, *selected_ids),
            )
            detach_review_assets(cursor, asset_ids=selected_ids)
            cursor.execute(
                "UPDATE travel_import_clusters SET latitude = %s, longitude = %s "
                "WHERE id = %s",
                (remaining_rep.latitude, remaining_rep.longitude, body.clusterId),
            )
            synchronize_cluster_representative(
                cursor,
                batch_id=batch_id,
                cluster_id=new_cluster_id,
                representative_asset_id=None,
            )
            synchronize_cluster_representative(
                cursor,
                batch_id=batch_id,
                cluster_id=body.clusterId,
                representative_asset_id=original_representative_id,
            )
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


@router.post("/{batch_id}/validate")
async def validate_import_batch(batch_id: str):
    _require_batch(batch_id)
    return _validate_batch(batch_id)


@router.post("/{batch_id}/publish")
async def publish_import_batch(
    batch_id: str,
    user: dict = Depends(require_admin),
):
    if not TRAVEL_IMPORT_PUBLISH_ENABLED:
        raise HTTPException(status_code=403, detail="Import publishing is disabled")
    batch = _require_batch(batch_id)
    if batch["status"] == "published":
        return get_batch_detail(batch_id)
    if batch["status"] == "publishing":
        raise HTTPException(
            status_code=409, detail="Import batch is already publishing"
        )
    validation = _validate_batch(batch_id)
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail=validation)
    claimed = _claim_publish_batch(batch_id, user)
    if not claimed:
        return get_batch_detail(batch_id)
    try:
        _publish_batch(batch_id, user)
    except Exception as exc:
        _release_publish_batch(batch_id, str(exc))
        raise
    _refresh_manifest(batch_id)
    return get_batch_detail(batch_id)


def _require_batch(batch_id: str) -> dict:
    batch = get_batch_row(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


async def _resolve_new_cluster_location(
    body: ImportClusterCreateRequest, request: Request
) -> dict:
    map_link = (body.mapLink or "").strip()
    if not map_link:
        return {
            "latitude": body.latitude,
            "longitude": body.longitude,
            "map_link": None,
            "suggested_name": None,
            "resolved_address": None,
        }
    try:
        result = await resolve_supported_place_link(
            map_link,
            browser=getattr(request.app.state, "browser", None),
        )
    except PlaceLinkError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to resolve map link: {error}",
        ) from error
    return {
        "latitude": result.latitude,
        "longitude": result.longitude,
        "map_link": result.resolved_url,
        "suggested_name": result.name,
        "resolved_address": result.address,
    }


def _lock_draft_mutation(cursor, batch_id: str) -> dict:
    try:
        return lock_mutable_batch(cursor, batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Import batch not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Queued, processing, publishing, or published batches are immutable",
        ) from exc


def _lock_review_cluster(cursor, batch_id: str, cluster_id: str) -> dict:
    cursor.execute(
        "SELECT * FROM travel_import_clusters "
        "WHERE id = %s AND batch_id = %s FOR UPDATE",
        (cluster_id, batch_id),
    )
    cluster = cursor.fetchone()
    if not cluster:
        raise HTTPException(status_code=404, detail="Import cluster not found")
    return cluster


def _lock_review_assets(
    cursor,
    *,
    batch_id: str,
    cluster_id: str,
    asset_ids: list[str],
) -> list[dict]:
    if not asset_ids:
        return []
    placeholders = ",".join(["%s"] * len(asset_ids))
    cursor.execute(
        f"SELECT id, captured_at FROM travel_import_assets "
        f"WHERE batch_id = %s AND cluster_id = %s "
        f"AND id IN ({placeholders}) FOR UPDATE",
        (batch_id, cluster_id, *asset_ids),
    )
    assets = cursor.fetchall()
    if {asset["id"] for asset in assets} != set(asset_ids):
        raise HTTPException(
            status_code=422,
            detail="assetIds must belong to this batch and cluster",
        )
    return assets


def _replace_review_draft_assets(
    cursor,
    *,
    draft_id: str,
    asset_ids: list[str],
) -> None:
    previous_draft_ids = lock_review_draft_ids_for_assets(cursor, asset_ids)
    cursor.execute(
        "DELETE FROM travel_import_review_draft_assets WHERE draft_id = %s",
        (draft_id,),
    )
    if asset_ids:
        cursor.executemany(
            """
            INSERT INTO travel_import_review_draft_assets (draft_id, asset_id)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE draft_id = VALUES(draft_id)
            """,
            [(draft_id, asset_id) for asset_id in asset_ids],
        )
        placeholders = ",".join(["%s"] * len(asset_ids))
        cursor.execute(
            f"UPDATE travel_import_assets "
            f"SET role = 'review', excluded = 0, manual_exclusion_reason = NULL "
            f"WHERE id IN ({placeholders})",
            tuple(asset_ids),
        )
    refresh_review_draft_visited_at(
        cursor,
        [draft_id, *previous_draft_ids],
    )


def _move_cluster_review_drafts(
    cursor,
    *,
    batch_id: str,
    cluster_ids: list[str],
    target_cluster_id: str,
) -> None:
    placeholders = ",".join(["%s"] * len(cluster_ids))
    cursor.execute(
        f"UPDATE travel_import_review_drafts SET cluster_id = %s "
        f"WHERE batch_id = %s AND cluster_id IN ({placeholders})",
        (target_cluster_id, batch_id, *cluster_ids),
    )


def _require_manageable_place(cursor, place_id: str, user: dict) -> dict:
    cursor.execute("SELECT * FROM travel_places WHERE id = %s", (place_id,))
    place = cursor.fetchone()
    if not place or not can_manage_place(user, place):
        raise HTTPException(status_code=404, detail="Merge target not found")
    return place


def _claim_publish_batch(batch_id: str, user: dict) -> bool:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM travel_import_batches WHERE id = %s FOR UPDATE",
                (batch_id,),
            )
            batch = cursor.fetchone()
            if not batch:
                raise HTTPException(status_code=404, detail="Import batch not found")
            if batch["status"] == "published":
                return False
            if batch["status"] != "ready":
                raise HTTPException(
                    status_code=409,
                    detail="Import batch is not ready for publishing",
                )
            cursor.execute(
                "SELECT * FROM travel_import_clusters WHERE batch_id = %s FOR UPDATE",
                (batch_id,),
            )
            for cluster in cursor.fetchall():
                if cluster["publish_action"] == "skip":
                    continue
                place_id = cluster.get("published_place_id")
                if cluster["publish_action"] == "merge":
                    place_id = cluster.get("existing_place_id")
                    _require_manageable_place(cursor, place_id, user)
                elif not place_id:
                    place_id = generate_id("place")
                cursor.execute(
                    "UPDATE travel_import_clusters SET published_place_id = %s WHERE id = %s",
                    (place_id, cluster["id"]),
                )
            cursor.execute(
                """
                SELECT review.id, review.published_review_id
                FROM travel_import_review_drafts review
                JOIN travel_import_clusters cluster
                  ON cluster.id = review.cluster_id
                 AND cluster.batch_id = review.batch_id
                WHERE review.batch_id = %s
                  AND cluster.publish_action <> 'skip'
                  AND review.rating IS NOT NULL
                  AND NULLIF(TRIM(review.body), '') IS NOT NULL
                FOR UPDATE
                """,
                (batch_id,),
            )
            for review in cursor.fetchall():
                if not review.get("published_review_id"):
                    cursor.execute(
                        "UPDATE travel_import_review_drafts "
                        "SET published_review_id = %s WHERE id = %s",
                        (generate_id("review"), review["id"]),
                    )
            cursor.execute(
                "UPDATE travel_import_batches SET status = 'publishing', error_text = NULL "
                "WHERE id = %s",
                (batch_id,),
            )
            return True


def _release_publish_batch(batch_id: str, error_text: str) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE travel_import_batches SET status = 'ready', error_text = %s "
                "WHERE id = %s AND status = 'publishing'",
                (error_text[:65000], batch_id),
            )


def _validate_batch(batch_id: str) -> dict:
    detail = get_batch_detail(batch_id)
    errors = []
    warnings = []
    if detail["status"] not in {"ready", "published"}:
        errors.append(
            {"code": "batch-not-ready", "message": "Processing has not completed"}
        )
    asset_by_id = {asset["id"]: asset for asset in detail["assets"]}
    for cluster in detail["clusters"]:
        action = cluster["publishAction"]
        if action == "create":
            if not (cluster["draft"].get("name") or "").strip():
                errors.append(
                    {"code": "missing-place-name", "clusterId": cluster["id"]}
                )
            if not (cluster["draft"].get("category") or "").strip():
                errors.append(
                    {"code": "missing-place-category", "clusterId": cluster["id"]}
                )
        elif action == "merge" and not cluster.get("existingPlaceId"):
            errors.append({"code": "missing-merge-target", "clusterId": cluster["id"]})
        covers = [
            asset_id
            for asset_id in cluster["assetIds"]
            if asset_by_id[asset_id]["role"] == "cover"
        ]
        if len(covers) > 1:
            errors.append(
                {
                    "code": "multiple-covers",
                    "clusterId": cluster["id"],
                    "assetIds": covers,
                }
            )
    linked_review_asset_ids = {
        asset_id for review in detail["reviewDrafts"] for asset_id in review["assetIds"]
    }
    for review in detail["reviewDrafts"]:
        missing = []
        if review.get("rating") is None:
            missing.append("rating")
        if not (review.get("body") or "").strip():
            missing.append("body")
        if missing:
            warnings.append(
                {
                    "code": "incomplete-review",
                    "reviewId": review["id"],
                    "clusterId": review["clusterId"],
                    "missingFields": missing,
                }
            )
    for asset in detail["assets"]:
        if (
            asset["role"] == "review"
            and not asset["excluded"]
            and asset["id"] not in linked_review_asset_ids
        ):
            warnings.append(
                {
                    "code": "unlinked-review-asset",
                    "assetId": asset["id"],
                    "clusterId": asset["clusterId"],
                }
            )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _publish_batch(batch_id: str, user: dict) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM travel_import_clusters WHERE batch_id = %s ORDER BY sort_order, id",
                (batch_id,),
            )
            clusters = cursor.fetchall()
            for cluster in clusters:
                if cluster["publish_action"] == "skip":
                    continue
                place_id = cluster.get("published_place_id")
                if not place_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Publish reservation is missing for cluster {cluster['id']}",
                    )
                cursor.execute(
                    """
                    SELECT asset.*
                    FROM travel_import_assets asset
                    WHERE asset.cluster_id = %s AND asset.excluded = 0
                      AND asset.role IN ('cover', 'gallery', 'review')
                    ORDER BY asset.captured_at ASC, asset.created_at ASC, asset.id ASC
                    """,
                    (cluster["id"],),
                )
                assets = cursor.fetchall()
                final_media_by_asset = {
                    asset["id"]: _publish_asset_file(
                        cursor, place_id, asset, user["account_id"]
                    )
                    for asset in assets
                }
                photo_media_ids = list(dict.fromkeys(final_media_by_asset.values()))
                cover_asset_id = _select_publish_cover_asset_id(assets)
                cover_media_id = (
                    final_media_by_asset[cover_asset_id] if cover_asset_id else None
                )
                if cluster["publish_action"] == "create":
                    place_address = (
                        cluster.get("draft_address") or cluster.get("address") or ""
                    )[:500] or None
                    cursor.execute(
                        "SELECT id FROM travel_places WHERE id = %s", (place_id,)
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            """
                            INSERT INTO travel_places (
                                id, name, category, latitude, longitude, address,
                                description, cover_image_url, photo_urls_json,
                                cover_media_id, photo_media_ids_json, tags_json,
                                owner_account_id, owner_login_id, owner_name, owner_email,
                                visibility
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, '[]',
                                      %s, %s, '[]', %s, %s, %s, %s, %s)
                            """,
                            (
                                place_id,
                                cluster["draft_name"],
                                cluster["draft_category"],
                                cluster["latitude"],
                                cluster["longitude"],
                                place_address,
                                cluster.get("draft_description"),
                                cover_media_id,
                                dump_json(photo_media_ids),
                                user["account_id"],
                                user["login_id"],
                                user["name"],
                                user.get("email"),
                                cluster.get("draft_visibility") or "public",
                            ),
                        )
                else:
                    place = _require_manageable_place(cursor, place_id, user)
                    merged_media_ids = list(
                        dict.fromkeys(
                            parse_json_list(place.get("photo_media_ids_json"))
                            + photo_media_ids
                        )
                    )
                    cursor.execute(
                        "UPDATE travel_places SET photo_media_ids_json = %s, "
                        "cover_media_id = COALESCE(cover_media_id, %s) WHERE id = %s",
                        (dump_json(merged_media_ids), cover_media_id, place_id),
                    )
                cursor.execute(
                    """
                    SELECT *
                    FROM travel_import_review_drafts
                    WHERE batch_id = %s AND cluster_id = %s
                      AND rating IS NOT NULL
                      AND NULLIF(TRIM(body), '') IS NOT NULL
                    ORDER BY created_at, id
                    """,
                    (batch_id, cluster["id"]),
                )
                review_drafts = cursor.fetchall()
                for review in review_drafts:
                    cursor.execute(
                        "SELECT asset_id FROM travel_import_review_draft_assets "
                        "WHERE draft_id = %s ORDER BY created_at, asset_id",
                        (review["id"],),
                    )
                    review_asset_ids = [row["asset_id"] for row in cursor.fetchall()]
                    review_media_ids = list(
                        dict.fromkeys(
                            final_media_by_asset[asset_id]
                            for asset_id in review_asset_ids
                            if asset_id in final_media_by_asset
                        )
                    )
                    review_id = review.get("published_review_id")
                    if not review_id:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Review reservation is missing for draft {review['id']}",
                        )
                    cursor.execute(
                        "SELECT id FROM travel_place_reviews WHERE id = %s",
                        (review_id,),
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            """
                            INSERT INTO travel_place_reviews (
                                id, place_id, rating, headline, body, visited_at,
                                photo_urls_json, photo_media_ids_json,
                                author_account_id, author_login_id,
                                author_name, author_email
                            ) VALUES (%s, %s, %s, %s, %s, %s, '[]', %s,
                                      %s, %s, %s, %s)
                            """,
                            (
                                review_id,
                                place_id,
                                review["rating"],
                                review.get("headline"),
                                review["body"],
                                review.get("visited_at"),
                                dump_json(review_media_ids),
                                user["account_id"],
                                user["login_id"],
                                user["name"],
                                user.get("email"),
                            ),
                        )
                    cursor.execute(
                        "UPDATE travel_import_review_drafts "
                        "SET published_review_id = %s WHERE id = %s",
                        (review_id, review["id"]),
                    )
                cursor.execute(
                    "UPDATE travel_import_clusters SET published_place_id = %s WHERE id = %s",
                    (place_id, cluster["id"]),
                )
            cursor.execute(
                "UPDATE travel_import_batches SET status = 'published', error_text = NULL, "
                "published_at = UTC_TIMESTAMP() WHERE id = %s",
                (batch_id,),
            )


def _select_publish_cover_asset_id(assets: list[dict]) -> str | None:
    explicit_cover = next(
        (asset["id"] for asset in assets if asset["role"] == "cover"), None
    )
    if explicit_cover:
        return explicit_cover
    return next((asset["id"] for asset in assets if asset["role"] == "gallery"), None)


def _publish_asset_file(
    cursor, place_id: str, asset: dict, owner_account_id: str
) -> str:
    filename = f"{(asset.get('sha256') or asset['id'])[:16]}-{safe_segment(asset['original_name'], 'asset')}"
    key = f"places/{place_id}/{filename}"
    if asset["storage_kind"] == "local":
        if asset.get("organized_path"):
            root = import_output_root()
            source = Path(asset["organized_path"])
        else:
            root = import_local_root()
            source = Path(asset["local_source_path"])
        if root is None:
            raise HTTPException(
                status_code=409, detail="Local import root is unavailable"
            )
        try:
            with open_confined_file(root, source) as file_obj:
                upload_fileobj_to_key(
                    file_obj,
                    key,
                    asset.get("media_type") or "application/octet-stream",
                )
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Asset source is unsafe or missing: {asset['id']}",
            ) from exc
    else:
        source_key = asset.get("organized_path") or asset.get("staging_key")
        if not source_key:
            raise HTTPException(
                status_code=409, detail=f"Asset source is missing: {asset['id']}"
            )
        copy_object(source_key, key)
    return register_attached_object(
        cursor,
        object_key=key,
        content_type=asset.get("media_type") or "application/octet-stream",
        original_name=asset["original_name"],
        owner_account_id=owner_account_id,
        byte_size=asset.get("byte_size"),
    )


def _stream_confined_file(root: Path, path: Path):
    with open_confined_file(root, path) as file_obj:
        while chunk := file_obj.read(1024 * 1024):
            yield chunk


def _refresh_manifest(batch_id: str) -> None:
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM travel_import_batches WHERE id = %s", (batch_id,)
            )
            batch = cursor.fetchone()
            if not batch:
                return
            cursor.execute(
                "SELECT * FROM travel_import_assets WHERE batch_id = %s",
                (batch_id,),
            )
            assets = cursor.fetchall()
            cursor.execute(
                "SELECT * FROM travel_import_clusters WHERE batch_id = %s", (batch_id,)
            )
            clusters = cursor.fetchall()
            for cluster in clusters:
                cluster["asset_ids"] = [
                    asset["id"]
                    for asset in assets
                    if asset.get("cluster_id") == cluster["id"]
                ]
            cursor.execute(
                "SELECT * FROM travel_import_review_drafts "
                "WHERE batch_id = %s AND cluster_id IS NOT NULL",
                (batch_id,),
            )
            review_drafts = cursor.fetchall()
            cursor.execute(
                """
                SELECT link.draft_id, link.asset_id
                FROM travel_import_review_draft_assets link
                JOIN travel_import_review_drafts review ON review.id = link.draft_id
                WHERE review.batch_id = %s
                """,
                (batch_id,),
            )
            review_asset_ids: dict[str, list[str]] = {}
            for link in cursor.fetchall():
                review_asset_ids.setdefault(link["draft_id"], []).append(
                    link["asset_id"]
                )
            for review in review_drafts:
                review["asset_ids"] = review_asset_ids.get(review["id"], [])
            manifest = build_manifest(batch, assets, clusters, review_drafts)
            cursor.execute(
                "UPDATE travel_import_batches SET manifest_json = %s WHERE id = %s",
                (json.dumps(manifest, ensure_ascii=False), batch_id),
            )
