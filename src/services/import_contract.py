from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

MANIFEST_VERSION = "travel-import.v1"
CLUSTER_RADIUS_METERS = 100.0

PHOTO_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".erf",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".sr2",
    ".srf",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}


@dataclass(frozen=True)
class GeoPoint:
    id: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class PhotoClassification:
    classification: str
    reason: str | None
    excluded: bool


def resolve_local_source(root: Path | None, relative_path: str) -> Path:
    if root is None:
        raise ValueError("Local photo import is disabled")
    raw = Path(relative_path)
    if raw.is_absolute() or not relative_path.strip():
        raise ValueError("Local source path must be a non-empty relative path")
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / raw).resolve(strict=True)
    if not candidate.is_relative_to(resolved_root):
        raise ValueError("Local source path escapes the configured allowlist root")
    if not candidate.is_dir():
        raise ValueError("Local source path must identify a directory")
    return candidate


def safe_segment(value: str | None, fallback: str = "unknown") -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return normalized.strip("._-") or fallback


def coordinate_folder(latitude: float, longitude: float) -> str:
    return f"{latitude:.6f}_{longitude:.6f}"


def extract_safe_zip(
    archive_path: Path,
    destination: Path,
    *,
    max_files: int,
    max_expanded_bytes: int,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    extracted: list[Path] = []
    expanded_bytes = 0

    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Invalid ZIP archive") from exc

    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > max_files:
            raise ValueError(f"ZIP contains more than {max_files} files")
        for member in members:
            path = PurePosixPath(member.filename.replace("\\", "/"))
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or re.match(r"^[A-Za-z]:", path.parts[0])
            ):
                raise ValueError(f"Unsafe ZIP member path: {member.filename}")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP symlinks are not allowed: {member.filename}")
            if Path(path.name).suffix.lower() in ARCHIVE_EXTENSIONS:
                raise ValueError(f"Nested archives are not allowed: {member.filename}")
            if member.file_size < 0:
                raise ValueError("ZIP member has an invalid size")
            expanded_bytes += member.file_size
            if expanded_bytes > max_expanded_bytes:
                raise ValueError(
                    f"ZIP expands beyond the {max_expanded_bytes}-byte limit"
                )

        streamed_bytes = 0
        for member in members:
            relative = Path(*PurePosixPath(member.filename.replace("\\", "/")).parts)
            target = (destination_root / relative).resolve()
            if not target.is_relative_to(destination_root):
                raise ValueError(f"Unsafe ZIP member path: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    streamed_bytes += len(chunk)
                    if streamed_bytes > max_expanded_bytes:
                        raise ValueError(
                            f"ZIP expands beyond the {max_expanded_bytes}-byte limit"
                        )
                    output.write(chunk)
            extracted.append(target)
    return extracted


def parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    raw = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", raw)
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def normalize_exif(metadata: dict, filename: str) -> dict:
    captured_at = None
    for key in (
        "SubSecDateTimeOriginal",
        "DateTimeOriginal",
        "CreateDate",
        "MediaCreateDate",
    ):
        captured_at = parse_exif_datetime(metadata.get(key))
        if captured_at:
            break

    latitude = _float_or_none(metadata.get("GPSLatitude"))
    longitude = _float_or_none(metadata.get("GPSLongitude"))
    if latitude is not None and not -90 <= latitude <= 90:
        latitude = None
    if longitude is not None and not -180 <= longitude <= 180:
        longitude = None

    return {
        "filename": filename,
        "fileType": metadata.get("FileType"),
        "mimeType": metadata.get("MIMEType"),
        "capturedAt": captured_at,
        "latitude": latitude,
        "longitude": longitude,
        "width": _int_or_none(metadata.get("ImageWidth")),
        "height": _int_or_none(metadata.get("ImageHeight")),
        "make": metadata.get("Make"),
        "model": metadata.get("Model"),
        "software": metadata.get("Software"),
        "description": metadata.get("Description"),
        "comment": metadata.get("Comment"),
        "raw": metadata,
    }


def classify_photo(filename: str, normalized: dict) -> PhotoClassification:
    extension = Path(filename).suffix.lower()
    if extension in RAW_EXTENSIONS:
        return PhotoClassification("raw", "raw", False)
    if extension in VIDEO_EXTENSIONS:
        return PhotoClassification("video", "video", False)
    if extension in ARCHIVE_EXTENSIONS:
        return PhotoClassification("archive", "archive", True)
    if extension not in PHOTO_EXTENSIONS:
        return PhotoClassification("invalid", "unsupported-file", False)
    if not normalized.get("fileType") and not normalized.get("mimeType"):
        return PhotoClassification("invalid", "invalid-metadata", False)
    if is_screenshot(filename, normalized):
        return PhotoClassification("screenshot", "screenshot", True)
    if normalized.get("latitude") is None or normalized.get("longitude") is None:
        return PhotoClassification("no-gps", "no-gps", False)
    return PhotoClassification("photo", None, False)


def is_screenshot(filename: str, normalized: dict) -> bool:
    haystack = " ".join(
        str(value or "")
        for value in (
            filename,
            normalized.get("software"),
            normalized.get("description"),
            normalized.get("comment"),
            normalized.get("raw", {}).get("UserComment"),
        )
    ).lower()
    return any(
        marker in haystack
        for marker in ("screenshot", "screen shot", "스크린샷", "스크린 샷")
    )


def haversine_meters(first: GeoPoint, second: GeoPoint) -> float:
    earth_radius = 6_371_000.0
    lat1 = math.radians(first.latitude)
    lat2 = math.radians(second.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(second.longitude - first.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return earth_radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def choose_medoid(points: Iterable[GeoPoint]) -> GeoPoint:
    candidates = sorted(points, key=lambda point: point.id)
    if not candidates:
        raise ValueError("Cannot choose a medoid for an empty cluster")
    return min(
        candidates,
        key=lambda candidate: (
            sum(haversine_meters(candidate, other) for other in candidates),
            candidate.id,
        ),
    )


def cluster_points(
    points: Iterable[GeoPoint], radius_meters: float = CLUSTER_RADIUS_METERS
) -> list[tuple[GeoPoint, list[GeoPoint]]]:
    remaining = sorted(points, key=lambda point: point.id)
    clusters: list[tuple[GeoPoint, list[GeoPoint]]] = []
    while remaining:
        members = [remaining.pop(0)]
        index = 0
        while index < len(remaining):
            candidate_members = members + [remaining[index]]
            representative = choose_medoid(candidate_members)
            if all(
                haversine_meters(representative, point) <= radius_meters
                for point in candidate_members
            ):
                members.append(remaining.pop(index))
            else:
                index += 1
        representative = choose_medoid(members)
        clusters.append((representative, sorted(members, key=lambda point: point.id)))
    return clusters


def validate_cluster_radius(
    points: Iterable[GeoPoint], radius_meters: float = CLUSTER_RADIUS_METERS
) -> GeoPoint:
    members = list(points)
    representative = choose_medoid(members)
    if any(
        haversine_meters(representative, point) > radius_meters for point in members
    ):
        raise ValueError(
            f"Merged cluster cannot keep every asset within {radius_meters:.0f}m "
            "of its representative"
        )
    return representative


def build_manifest(
    batch: dict,
    assets: list[dict],
    clusters: list[dict],
    review_drafts: list[dict] | None = None,
) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "batch": {
            "id": batch["id"],
            "name": batch["name"],
            "sourceType": batch["source_type"],
            "status": batch["status"],
            "oldestCapturedAt": _iso(batch.get("oldest_captured_at")),
        },
        "assets": [
            {
                "id": asset["id"],
                "originalName": asset["original_name"],
                "sha256": asset.get("sha256"),
                "capturedAt": _iso(asset.get("captured_at")),
                "latitude": asset.get("latitude"),
                "longitude": asset.get("longitude"),
                "classification": asset.get("classification"),
                "reason": asset.get("classification_reason"),
                "exclusionReason": asset.get("manual_exclusion_reason"),
                "role": asset.get("role"),
                "excluded": bool(asset.get("excluded")),
                "clusterId": asset.get("cluster_id"),
                "organizedPath": asset.get("organized_path"),
            }
            for asset in sorted(assets, key=lambda item: item["id"])
        ],
        "clusters": [
            {
                "id": cluster["id"],
                "representativeAssetId": cluster.get("representative_asset_id"),
                "latitude": cluster["latitude"],
                "longitude": cluster["longitude"],
                "countryCode": cluster.get("country_code"),
                "country": cluster.get("country"),
                "city": cluster.get("city"),
                "address": cluster.get("address"),
                "suggestedName": cluster.get("suggested_name"),
                "mapLink": cluster.get("map_link"),
                "publishAction": cluster.get("publish_action"),
                "existingPlaceId": cluster.get("existing_place_id"),
                "publishedPlaceId": cluster.get("published_place_id"),
                "draft": {
                    "name": cluster.get("draft_name"),
                    "category": cluster.get("draft_category"),
                    "address": cluster.get("draft_address"),
                    "description": cluster.get("draft_description"),
                    "visibility": cluster.get("draft_visibility"),
                },
                "assetIds": sorted(cluster.get("asset_ids", [])),
            }
            for cluster in sorted(
                clusters, key=lambda item: (item.get("sort_order", 0), item["id"])
            )
        ],
        "reviewDrafts": [
            {
                "id": review["id"],
                "batchId": review["batch_id"],
                "clusterId": review["cluster_id"],
                "rating": review.get("rating"),
                "headline": review.get("headline"),
                "body": review.get("body"),
                "visitedAt": _iso(review.get("visited_at")),
                "assetIds": sorted(review.get("asset_ids", [])),
                "publishedReviewId": review.get("published_review_id"),
                "createdAt": _iso(review.get("created_at")),
                "updatedAt": _iso(review.get("updated_at")),
            }
            for review in sorted(review_drafts or [], key=lambda item: item["id"])
        ],
    }


def write_place_json(folder: Path, payload: dict, *, output_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="travel-place-json-") as raw:
        temporary_root = Path(raw)
        temporary = temporary_root / "place.json"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        copy_original(
            temporary,
            folder / "place.json",
            source_root=temporary_root,
            destination_root=output_root,
        )


@contextmanager
def open_confined_file(root: Path, path: Path):
    root_resolved = root.resolve(strict=True)
    relative = _relative_to_configured_root(root, root_resolved, path)
    if not relative.parts:
        raise ValueError("A regular file path is required")

    root_fd = os.open(root_resolved, os.O_RDONLY | os.O_DIRECTORY)
    directory_fd = root_fd
    file_fd = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("A regular file is required")
        with os.fdopen(file_fd, "rb") as file_obj:
            file_fd = None
            yield file_obj
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


def copy_original(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
    destination_root: Path,
) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    root_resolved = destination_root.resolve(strict=True)
    relative = _relative_to_configured_root(
        destination_root, root_resolved, destination
    )
    if not relative.parts:
        raise ValueError("A destination filename is required")

    with open_confined_file(source_root, source) as source_obj:
        root_fd = os.open(root_resolved, os.O_RDONLY | os.O_DIRECTORY)
        directory_fd = root_fd
        output_fd = None
        try:
            for part in relative.parts[:-1]:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                if directory_fd != root_fd:
                    os.close(directory_fd)
                directory_fd = next_fd
            output_fd = os.open(
                relative.parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o644,
                dir_fd=directory_fd,
            )
            with os.fdopen(output_fd, "wb") as output:
                output_fd = None
                while chunk := source_obj.read(1024 * 1024):
                    output.write(chunk)
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if directory_fd != root_fd:
                os.close(directory_fd)
            os.close(root_fd)


def _relative_to_configured_root(
    configured_root: Path, resolved_root: Path, path: Path
) -> Path:
    candidate = Path(os.path.abspath(path))
    bases = (Path(os.path.abspath(configured_root)), resolved_root)
    for base in bases:
        try:
            return candidate.relative_to(base)
        except ValueError:
            continue
    raise ValueError("File is outside the configured root")


def _float_or_none(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")
