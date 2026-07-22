from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import parse, request

from ..config import (
    TRAVEL_IMPORT_MAX_ZIP_EXPANDED_BYTES,
    TRAVEL_IMPORT_MAX_ZIP_FILES,
    TRAVEL_IMPORT_NOMINATIM_BASE_URL,
    TRAVEL_IMPORT_NOMINATIM_USER_AGENT,
    import_local_root,
    import_output_root,
)
from ..connectors import get_db_connection
from ..utils import generate_id
from .import_contract import (
    GeoPoint,
    PHOTO_EXTENSIONS,
    RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    build_manifest,
    classify_photo,
    cluster_points,
    coordinate_folder,
    copy_original,
    extract_safe_zip,
    normalize_exif,
    open_confined_file,
    resolve_local_source,
    safe_segment,
    write_place_json,
)
from .import_repository import update_job_progress
from .storage import (
    copy_object,
    download_object_to_path,
    upload_path_to_key,
)

logger = logging.getLogger(__name__)

THUMBNAIL_MAX_EDGE = 480


class ImportProcessor:
    def __init__(self) -> None:
        self._last_nominatim_request_at = 0.0

    def process(self, job: dict) -> None:
        batch_id = job["batch_id"]
        with tempfile.TemporaryDirectory(prefix=f"travel-import-{batch_id}-") as raw:
            work_root = Path(raw)
            batch = self._load_batch(batch_id)
            if batch["source_type"] == "local":
                self._discover_local_assets(batch)
            elif batch["source_type"] == "upload":
                self._expand_uploaded_zips(batch_id, work_root)
            else:
                raise RuntimeError(f"Unsupported import source type: {batch['source_type']}")

            assets = self._load_assets(batch_id)
            requires_full_processing = any(
                not asset.get("processed_at") for asset in assets
            )
            trailing_steps = 3 if requires_full_processing else 0
            total = len(assets) + trailing_steps
            completed = 0
            update_job_progress(job["id"], batch_id, completed, total)
            for asset in assets:
                if not asset.get("processed_at"):
                    self._process_asset(batch, asset, work_root)
                elif not asset.get("thumbnail_key"):
                    self._backfill_thumbnail(batch, asset, work_root)
                completed += 1
                update_job_progress(job["id"], batch_id, completed, total)

            if not requires_full_processing:
                return

            self._update_oldest_captured_at(batch_id)
            clusters = self._replace_clusters(batch_id)
            completed += 1
            update_job_progress(job["id"], batch_id, completed, total)

            for cluster in clusters:
                geocode = self._reverse_geocode(
                    cluster["latitude"], cluster["longitude"]
                )
                self._apply_geocode(cluster["id"], geocode)
            completed += 1
            update_job_progress(job["id"], batch_id, completed, total)

            self._organize_assets(batch, work_root)
            self._persist_manifest(batch_id)
            completed += 1
            update_job_progress(job["id"], batch_id, completed, total)

    def _load_batch(self, batch_id: str) -> dict:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_import_batches WHERE id = %s", (batch_id,)
                )
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError(f"Import batch not found: {batch_id}")
                return row

    def _load_assets(self, batch_id: str) -> list[dict]:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_import_assets WHERE batch_id = %s "
                    "ORDER BY created_at, id",
                    (batch_id,),
                )
                return cursor.fetchall()

    def _discover_local_assets(self, batch: dict) -> None:
        source = resolve_local_source(import_local_root(), batch["source_path"])
        configured_root = import_local_root()
        if configured_root is None:
            raise RuntimeError("Local import root is not configured")
        root = configured_root.resolve(strict=True)
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                for directory, dirnames, filenames in os.walk(source, followlinks=False):
                    dirnames[:] = sorted(
                        name
                        for name in dirnames
                        if not (Path(directory) / name).is_symlink()
                    )
                    for filename in sorted(filenames):
                        path = Path(directory) / filename
                        if path.is_symlink() or not path.is_file():
                            continue
                        resolved = path.resolve(strict=True)
                        if not resolved.is_relative_to(root):
                            continue
                        relative = resolved.relative_to(root).as_posix()
                        source_ref = _source_ref("local", relative)
                        cursor.execute(
                            """
                            INSERT INTO travel_import_assets (
                                id, batch_id, source_ref, original_name, media_type,
                                byte_size, storage_kind, local_source_path,
                                classification, role, excluded
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'local', %s,
                                      'pending', 'gallery', 0)
                            ON DUPLICATE KEY UPDATE
                                byte_size = VALUES(byte_size),
                                local_source_path = VALUES(local_source_path)
                            """,
                            (
                                generate_id("asset"),
                                batch["id"],
                                source_ref,
                                filename,
                                mimetypes.guess_type(filename)[0],
                                resolved.stat().st_size,
                                str(resolved),
                            ),
                        )

    def _expand_uploaded_zips(self, batch_id: str, work_root: Path) -> None:
        for asset in self._load_assets(batch_id):
            if Path(asset["original_name"]).suffix.lower() != ".zip":
                continue
            if asset.get("processed_at"):
                continue
            archive_path = work_root / f"archive-{asset['id']}.zip"
            if not asset.get("staging_key"):
                raise RuntimeError(f"ZIP asset has no staging key: {asset['id']}")
            download_object_to_path(asset["staging_key"], archive_path)
            destination = work_root / f"zip-{asset['id']}"
            extracted = extract_safe_zip(
                archive_path,
                destination,
                max_files=TRAVEL_IMPORT_MAX_ZIP_FILES,
                max_expanded_bytes=TRAVEL_IMPORT_MAX_ZIP_EXPANDED_BYTES,
            )
            for path in extracted:
                relative = path.relative_to(destination).as_posix()
                staging_key = (
                    f"imports/{batch_id}/staging/extracted/{asset['id']}/{relative}"
                )
                upload_path_to_key(
                    path,
                    staging_key,
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
                self._upsert_extracted_asset(
                    batch_id=batch_id,
                    parent_asset_id=asset["id"],
                    relative=relative,
                    path=path,
                    staging_key=staging_key,
                )
            with get_db_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE travel_import_assets
                        SET classification = 'archive',
                            classification_reason = 'expanded-zip',
                            role = 'excluded', excluded = 1,
                            processed_at = UTC_TIMESTAMP()
                        WHERE id = %s
                        """,
                        (asset["id"],),
                    )

    def _upsert_extracted_asset(
        self,
        *,
        batch_id: str,
        parent_asset_id: str,
        relative: str,
        path: Path,
        staging_key: str,
    ) -> None:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO travel_import_assets (
                        id, batch_id, source_ref, original_name, media_type,
                        byte_size, storage_kind, staging_key,
                        classification, role, excluded
                    ) VALUES (%s, %s, %s, %s, %s, %s, 's3', %s,
                              'pending', 'gallery', 0)
                    ON DUPLICATE KEY UPDATE
                        byte_size = VALUES(byte_size),
                        staging_key = VALUES(staging_key)
                    """,
                    (
                        generate_id("asset"),
                        batch_id,
                        _source_ref(f"zip:{parent_asset_id}", relative),
                        Path(relative).name,
                        mimetypes.guess_type(relative)[0],
                        path.stat().st_size,
                        staging_key,
                    ),
                )

    def _process_asset(self, batch: dict, asset: dict, work_root: Path) -> None:
        source = self._materialize_asset(asset, work_root)
        sha256 = _sha256(source)
        duplicate_id = self._find_duplicate(batch["id"], asset["id"], sha256)
        metadata = self._read_exif(source)
        normalized = normalize_exif(metadata, asset["original_name"])
        classification = classify_photo(asset["original_name"], normalized)
        role = "excluded" if classification.excluded or duplicate_id else "gallery"
        excluded = bool(classification.excluded or duplicate_id)
        reason = "duplicate" if duplicate_id else classification.reason
        preview_key = self._create_preview(batch["id"], asset, source, work_root)
        thumbnail_key = self._create_thumbnail(
            batch["id"], asset, source, work_root
        )
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE travel_import_assets
                    SET sha256 = %s, captured_at = %s, latitude = %s,
                        longitude = %s, metadata_json = %s,
                        classification = %s, classification_reason = %s,
                        role = %s, excluded = %s, duplicate_of_asset_id = %s,
                        preview_key = %s, thumbnail_key = %s,
                        processed_at = UTC_TIMESTAMP()
                    WHERE id = %s
                    """,
                    (
                        sha256,
                        normalized["capturedAt"],
                        normalized["latitude"],
                        normalized["longitude"],
                        json.dumps(metadata, ensure_ascii=False, default=str),
                        classification.classification,
                        reason,
                        role,
                        int(excluded),
                        duplicate_id,
                        preview_key,
                        thumbnail_key,
                        asset["id"],
                    ),
                )

    def _backfill_thumbnail(
        self, batch: dict, asset: dict, work_root: Path
    ) -> None:
        if not self._supports_thumbnail(asset["original_name"]):
            return
        source = self._materialize_asset(asset, work_root)
        thumbnail_key = self._create_thumbnail(
            batch["id"], asset, source, work_root
        )
        if not thumbnail_key:
            return
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE travel_import_assets
                    SET thumbnail_key = %s
                    WHERE id = %s AND batch_id = %s
                      AND processed_at IS NOT NULL AND thumbnail_key IS NULL
                    """,
                    (thumbnail_key, asset["id"], batch["id"]),
                )

    def _materialize_asset(self, asset: dict, work_root: Path) -> Path:
        if asset["storage_kind"] == "local":
            path = Path(asset["local_source_path"])
            configured_root = import_local_root()
            if configured_root is None:
                raise RuntimeError("Local import root is not configured")
            suffix = Path(asset["original_name"]).suffix
            destination = work_root / f"{asset['id']}{suffix}"
            try:
                with open_confined_file(configured_root, path) as source:
                    with destination.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    "Local asset escaped the configured allowlist root"
                ) from exc
            return destination
        if not asset.get("staging_key"):
            raise RuntimeError(f"Uploaded asset has no staging key: {asset['id']}")
        suffix = Path(asset["original_name"]).suffix
        destination = work_root / f"{asset['id']}{suffix}"
        download_object_to_path(asset["staging_key"], destination)
        return destination

    def _find_duplicate(
        self, batch_id: str, asset_id: str, sha256: str
    ) -> str | None:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM travel_import_assets
                    WHERE batch_id = %s AND sha256 = %s AND id <> %s
                      AND processed_at IS NOT NULL
                    ORDER BY created_at, id LIMIT 1
                    """,
                    (batch_id, sha256, asset_id),
                )
                row = cursor.fetchone()
                return row["id"] if row else None

    def _read_exif(self, path: Path) -> dict:
        command = [
            "exiftool",
            "-json",
            "-n",
            "-SubSecDateTimeOriginal",
            "-DateTimeOriginal",
            "-CreateDate",
            "-MediaCreateDate",
            "-GPSLatitude",
            "-GPSLongitude",
            "-MIMEType",
            "-FileType",
            "-ImageWidth",
            "-ImageHeight",
            "-Make",
            "-Model",
            "-Software",
            "-Description",
            "-Comment",
            "-UserComment",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ExifTool is required by the import worker but was not found"
            ) from exc
        if completed.returncode not in {0, 1}:
            raise RuntimeError(
                f"ExifTool failed for {path.name}: {completed.stderr.strip()}"
            )
        try:
            payload = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return {}
        return payload[0] if isinstance(payload, list) and payload else {}

    def _create_preview(
        self, batch_id: str, asset: dict, source: Path, work_root: Path
    ) -> str | None:
        extension = source.suffix.lower()
        if extension not in {".heic", ".heif"} and extension not in RAW_EXTENSIONS:
            return None
        preview = work_root / f"preview-{asset['id']}.jpg"
        if not self._extract_embedded_preview(source, preview):
            if extension in {".heic", ".heif"}:
                self._run_preview_command(
                    ["heif-convert", str(source), str(preview)], preview
                )
            if not preview.exists():
                self._run_preview_command(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        str(preview),
                    ],
                    preview,
                )
        if not preview.exists() or preview.stat().st_size == 0:
            logger.warning("No browser preview could be generated for %s", source.name)
            return None
        key = f"imports/{batch_id}/previews/{asset['id']}.jpg"
        upload_path_to_key(preview, key, "image/jpeg")
        return key

    def _create_thumbnail(
        self, batch_id: str, asset: dict, source: Path, work_root: Path
    ) -> str | None:
        if not self._supports_thumbnail(source.name):
            return None

        thumbnail = work_root / f"thumbnail-{asset['id']}.jpg"
        candidates = [source]
        extension = source.suffix.lower()
        if extension in RAW_EXTENSIONS or extension in {".heic", ".heif"}:
            converted = work_root / f"thumbnail-source-{asset['id']}.jpg"
            if self._extract_embedded_preview(source, converted):
                candidates.insert(0, converted)
            elif extension in {".heic", ".heif"}:
                self._run_preview_command(
                    ["heif-convert", str(source), str(converted)], converted
                )
                if converted.exists():
                    candidates.insert(0, converted)

        for candidate in candidates:
            thumbnail.unlink(missing_ok=True)
            self._run_preview_command(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(candidate),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    (
                        f"scale={THUMBNAIL_MAX_EDGE}:{THUMBNAIL_MAX_EDGE}:"
                        "force_original_aspect_ratio=decrease"
                    ),
                    "-q:v",
                    "4",
                    str(thumbnail),
                ],
                thumbnail,
            )
            if thumbnail.exists() and thumbnail.stat().st_size > 0:
                key = f"imports/{batch_id}/thumbnails/{asset['id']}.jpg"
                upload_path_to_key(thumbnail, key, "image/jpeg")
                return key

        logger.warning("No thumbnail could be generated for %s", source.name)
        return None

    @staticmethod
    def _supports_thumbnail(filename: str) -> bool:
        extension = Path(filename).suffix.lower()
        return extension in PHOTO_EXTENSIONS | RAW_EXTENSIONS | VIDEO_EXTENSIONS

    @staticmethod
    def _extract_embedded_preview(source: Path, destination: Path) -> bool:
        for tag in ("PreviewImage", "JpgFromRaw", "ThumbnailImage"):
            try:
                completed = subprocess.run(
                    ["exiftool", "-b", f"-{tag}", str(source)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            except FileNotFoundError:
                return False
            if completed.returncode == 0 and completed.stdout:
                destination.write_bytes(completed.stdout)
                return True
        return False

    @staticmethod
    def _run_preview_command(command: list[str], destination: Path) -> None:
        try:
            subprocess.run(command, capture_output=True, timeout=120, check=False)
        except FileNotFoundError:
            return
        if destination.exists() and destination.stat().st_size == 0:
            destination.unlink()

    def _update_oldest_captured_at(self, batch_id: str) -> None:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE travel_import_batches
                    SET oldest_captured_at = (
                        SELECT MIN(captured_at) FROM travel_import_assets
                        WHERE batch_id = %s AND captured_at IS NOT NULL
                          AND classification IN ('photo', 'raw', 'no-gps')
                    )
                    WHERE id = %s
                    """,
                    (batch_id, batch_id),
                )

    def _replace_clusters(self, batch_id: str) -> list[dict]:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, latitude, longitude FROM travel_import_assets
                    WHERE batch_id = %s AND classification = 'photo'
                      AND excluded = 0 AND latitude IS NOT NULL AND longitude IS NOT NULL
                    ORDER BY id
                    """,
                    (batch_id,),
                )
                points = [
                    GeoPoint(row["id"], row["latitude"], row["longitude"])
                    for row in cursor.fetchall()
                ]
                cursor.execute(
                    "UPDATE travel_import_assets SET cluster_id = NULL WHERE batch_id = %s",
                    (batch_id,),
                )
                cursor.execute(
                    "DELETE FROM travel_import_clusters WHERE batch_id = %s",
                    (batch_id,),
                )
                result = []
                for order, (representative, members) in enumerate(
                    cluster_points(points), start=1
                ):
                    cluster_id = generate_id("cluster")
                    cursor.execute(
                        """
                        INSERT INTO travel_import_clusters (
                            id, batch_id, sort_order, representative_asset_id,
                            latitude, longitude, publish_action, draft_visibility
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'create', 'public')
                        """,
                        (
                            cluster_id,
                            batch_id,
                            order,
                            None,
                            representative.latitude,
                            representative.longitude,
                        ),
                    )
                    member_ids = [member.id for member in members]
                    placeholders = ",".join(["%s"] * len(member_ids))
                    cursor.execute(
                        f"UPDATE travel_import_assets "
                        f"SET cluster_id = %s, role = 'gallery' "
                        f"WHERE id IN ({placeholders})",
                        (cluster_id, *member_ids),
                    )
                    result.append(
                        {
                            "id": cluster_id,
                            "latitude": representative.latitude,
                            "longitude": representative.longitude,
                        }
                    )
                return result

    def _reverse_geocode(self, latitude: float, longitude: float) -> dict:
        cache_key = f"{latitude:.6f},{longitude:.6f}"
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT response_json FROM travel_import_geocode_cache "
                    "WHERE cache_key = %s",
                    (cache_key,),
                )
                cached = cursor.fetchone()
                if cached:
                    return json.loads(cached["response_json"])

        elapsed = time.monotonic() - self._last_nominatim_request_at
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        query = parse.urlencode(
            {
                "format": "jsonv2",
                "lat": f"{latitude:.7f}",
                "lon": f"{longitude:.7f}",
                "zoom": "18",
                "addressdetails": "1",
                "namedetails": "1",
            }
        )
        req = request.Request(
            f"{TRAVEL_IMPORT_NOMINATIM_BASE_URL}/reverse?{query}",
            headers={
                "User-Agent": TRAVEL_IMPORT_NOMINATIM_USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Nominatim reverse geocoding failed: {exc}") from exc
        finally:
            self._last_nominatim_request_at = time.monotonic()
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO travel_import_geocode_cache (
                        cache_key, latitude, longitude, response_json
                    ) VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE cache_key = VALUES(cache_key)
                    """,
                    (
                        cache_key,
                        latitude,
                        longitude,
                        json.dumps(result, ensure_ascii=False),
                    ),
                )
        return result

    def _apply_geocode(self, cluster_id: str, geocode: dict) -> None:
        address = geocode.get("address") or {}
        namedetails = geocode.get("namedetails") or {}
        city = next(
            (
                address.get(key)
                for key in ("city", "town", "village", "municipality", "county")
                if address.get(key)
            ),
            None,
        )
        suggested_name = (
            namedetails.get("name")
            or geocode.get("name")
            or (geocode.get("display_name") or "").split(",", 1)[0]
            or None
        )
        if suggested_name:
            suggested_name = str(suggested_name)[:255]
        display_address = str(geocode.get("display_name") or "") or None
        draft_address = display_address[:500] if display_address else None
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE travel_import_clusters
                    SET country_code = %s, country = %s, city = %s,
                        address = %s, suggested_name = %s,
                        draft_name = COALESCE(draft_name, %s),
                        draft_address = COALESCE(draft_address, %s)
                    WHERE id = %s
                    """,
                    (
                        (address.get("country_code") or "").upper() or None,
                        address.get("country"),
                        city,
                        display_address,
                        suggested_name,
                        suggested_name,
                        draft_address,
                        cluster_id,
                    ),
                )

    def _organize_assets(self, batch: dict, work_root: Path) -> None:
        assets = self._load_assets(batch["id"])
        cluster_by_id = self._load_cluster_map(batch["id"])
        for asset in assets:
            if asset.get("excluded"):
                continue
            if asset["classification"] == "archive":
                continue
            cluster = cluster_by_id.get(asset.get("cluster_id"))
            if cluster:
                relative_folder = Path(
                    safe_segment(cluster.get("country_code"), "XX"),
                    safe_segment(cluster.get("city"), "unknown-city"),
                    coordinate_folder(cluster["latitude"], cluster["longitude"]),
                )
            else:
                relative_folder = Path(
                    "etc", safe_segment(asset.get("classification_reason"), "unknown")
                )
            prefix = (asset.get("sha256") or asset["id"])[0:12]
            filename = f"{prefix}-{safe_segment(asset['original_name'], 'asset')}"
            if batch["source_type"] == "local":
                output_root = import_output_root()
                if output_root is None:
                    raise RuntimeError("TRAVEL_IMPORT_OUTPUT_ROOT is required for local imports")
                destination = output_root.resolve() / relative_folder / filename
                source_root = import_local_root()
                if source_root is None:
                    raise RuntimeError("TRAVEL_IMPORT_LOCAL_ROOT is required for local imports")
                copy_original(
                    Path(asset["local_source_path"]),
                    destination,
                    source_root=source_root,
                    destination_root=output_root,
                )
                organized_path = str(destination)
            else:
                if not asset.get("staging_key"):
                    continue
                key = (
                    f"imports/{batch['id']}/organized/"
                    f"{relative_folder.as_posix()}/{filename}"
                )
                copy_object(asset["staging_key"], key)
                organized_path = key
            with get_db_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE travel_import_assets SET organized_path = %s WHERE id = %s",
                        (organized_path, asset["id"]),
                    )

        if batch["source_type"] == "local":
            self._write_local_place_files(batch["id"])

    def _load_cluster_map(self, batch_id: str) -> dict[str, dict]:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_import_clusters WHERE batch_id = %s",
                    (batch_id,),
                )
                return {row["id"]: row for row in cursor.fetchall()}

    def _write_local_place_files(self, batch_id: str) -> None:
        output_root = import_output_root()
        if output_root is None:
            return
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_import_clusters WHERE batch_id = %s",
                    (batch_id,),
                )
                clusters = cursor.fetchall()
                for cluster in clusters:
                    cursor.execute(
                        """
                        SELECT id, original_name, captured_at, organized_path, role
                        FROM travel_import_assets WHERE cluster_id = %s ORDER BY id
                        """,
                        (cluster["id"],),
                    )
                    assets = cursor.fetchall()
                    folder = output_root.resolve() / Path(
                        safe_segment(cluster.get("country_code"), "XX"),
                        safe_segment(cluster.get("city"), "unknown-city"),
                        coordinate_folder(cluster["latitude"], cluster["longitude"]),
                    )
                    write_place_json(
                        folder,
                        {
                            "version": "travel-import.v1",
                            "clusterId": cluster["id"],
                            "name": cluster.get("suggested_name"),
                            "countryCode": cluster.get("country_code"),
                            "city": cluster.get("city"),
                            "address": cluster.get("address"),
                            "latitude": cluster["latitude"],
                            "longitude": cluster["longitude"],
                            "assets": [
                                {
                                    "id": asset["id"],
                                    "originalName": asset["original_name"],
                                    "capturedAt": asset["captured_at"],
                                    "path": asset["organized_path"],
                                    "role": asset["role"],
                                }
                                for asset in assets
                            ],
                        },
                        output_root=output_root,
                    )

    def _persist_manifest(self, batch_id: str) -> None:
        with get_db_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_import_batches WHERE id = %s", (batch_id,)
                )
                batch = cursor.fetchone()
                batch["status"] = "ready"
                cursor.execute(
                    "SELECT * FROM travel_import_assets WHERE batch_id = %s",
                    (batch_id,),
                )
                assets = cursor.fetchall()
                cursor.execute(
                    "SELECT * FROM travel_import_clusters WHERE batch_id = %s",
                    (batch_id,),
                )
                clusters = cursor.fetchall()
                for cluster in clusters:
                    cluster["asset_ids"] = sorted(
                        asset["id"]
                        for asset in assets
                        if asset.get("cluster_id") == cluster["id"]
                    )
                manifest = build_manifest(batch, assets, clusters)
                cursor.execute(
                    """
                    UPDATE travel_import_batches
                    SET status = 'ready', manifest_version = 'travel-import.v1',
                        manifest_json = %s
                    WHERE id = %s
                    """,
                    (json.dumps(manifest, ensure_ascii=False), batch_id),
                )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        while chunk := file_obj.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_ref(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"
