from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from src.services.import_contract import (
    GeoPoint,
    build_manifest,
    classify_photo,
    cluster_points,
    copy_original,
    extract_safe_zip,
    haversine_meters,
    normalize_exif,
    open_confined_file,
    resolve_local_source,
)


class ImportClusteringTests(unittest.TestCase):
    def test_clusters_are_deterministic_and_bounded_by_representative(self) -> None:
        points = [
            GeoPoint("c", 37.0, 127.0018),
            GeoPoint("a", 37.0, 127.0),
            GeoPoint("b", 37.0, 127.0008),
        ]
        first = cluster_points(points)
        second = cluster_points(reversed(points))

        self.assertEqual(
            [(rep.id, [point.id for point in members]) for rep, members in first],
            [(rep.id, [point.id for point in members]) for rep, members in second],
        )
        for representative, members in first:
            for member in members:
                self.assertLessEqual(haversine_meters(representative, member), 100.0)

    def test_chain_does_not_create_cluster_beyond_medoid_radius(self) -> None:
        points = [
            GeoPoint("a", 37.0, 127.0),
            GeoPoint("b", 37.0, 127.0009),
            GeoPoint("c", 37.0, 127.0018),
            GeoPoint("d", 37.0, 127.0027),
        ]
        clusters = cluster_points(points)
        self.assertGreater(len(clusters), 1)
        for representative, members in clusters:
            self.assertTrue(
                all(haversine_meters(representative, member) <= 100 for member in members)
            )


class ImportExifTests(unittest.TestCase):
    def test_normalizes_exif_and_classifies_geotagged_heic(self) -> None:
        normalized = normalize_exif(
            {
                "FileType": "HEIC",
                "MIMEType": "image/heic",
                "DateTimeOriginal": "2024:03:02 10:11:12",
                "GPSLatitude": 37.5,
                "GPSLongitude": 127.1,
            },
            "photo.heic",
        )
        self.assertEqual(normalized["capturedAt"], datetime(2024, 3, 2, 10, 11, 12))
        self.assertEqual(classify_photo("photo.heic", normalized).classification, "photo")

    def test_screenshot_is_excluded(self) -> None:
        normalized = normalize_exif(
            {"FileType": "PNG", "MIMEType": "image/png", "Software": "Screenshot"},
            "Screenshot 2026-01-01.png",
        )
        classification = classify_photo("Screenshot 2026-01-01.png", normalized)
        self.assertEqual(classification.classification, "screenshot")
        self.assertTrue(classification.excluded)

    def test_no_gps_and_raw_are_classified_as_etc_reasons(self) -> None:
        normalized = normalize_exif(
            {"FileType": "JPEG", "MIMEType": "image/jpeg"}, "plain.jpg"
        )
        self.assertEqual(classify_photo("plain.jpg", normalized).reason, "no-gps")
        self.assertEqual(classify_photo("camera.nef", normalized).reason, "raw")


class ImportZipSafetyTests(unittest.TestCase):
    def test_extracts_regular_zip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "photos.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("day-one/photo.jpg", b"jpeg")
            files = extract_safe_zip(
                archive, root / "out", max_files=10, max_expanded_bytes=100
            )
            self.assertEqual(
                [path.relative_to((root / "out").resolve()).as_posix() for path in files],
                ["day-one/photo.jpg"],
            )

    def test_rejects_traversal_nested_archives_and_expansion_limit(self) -> None:
        cases = [
            ("../escape.jpg", b"x", 100),
            ("nested.zip", b"x", 100),
            ("large.jpg", b"12345", 4),
        ]
        for member, payload, limit in cases:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as zipped:
                    zipped.writestr(member, payload)
                with self.assertRaises(ValueError):
                    extract_safe_zip(
                        archive,
                        root / "out",
                        max_files=10,
                        max_expanded_bytes=limit,
                    )

    def test_rejects_zip_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "bad.zip"
            link = zipfile.ZipInfo("photo-link.jpg")
            link.create_system = 3
            link.external_attr = (0o120777 << 16) | 0xA000
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr(link, "target.jpg")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                extract_safe_zip(
                    archive, root / "out", max_files=10, max_expanded_bytes=100
                )


class ImportLocalRootTests(unittest.TestCase):
    def test_allows_only_existing_relative_directory_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            allowed = root / "photos"
            allowed.mkdir()
            self.assertEqual(resolve_local_source(root, "photos"), allowed.resolve())
            with self.assertRaises(ValueError):
                resolve_local_source(root, str(allowed))
            with self.assertRaises((OSError, ValueError)):
                resolve_local_source(root, "../outside")

    @unittest.skipIf(os.name == "nt", "symlink semantics differ on Windows")
    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as other:
            root = Path(raw)
            (root / "link").symlink_to(Path(other), target_is_directory=True)
            with self.assertRaises(ValueError):
                resolve_local_source(root, "link")

    def test_confined_copy_preserves_source_and_writes_under_output(self) -> None:
        with tempfile.TemporaryDirectory() as source_raw, tempfile.TemporaryDirectory() as output_raw:
            source_root = Path(source_raw)
            output_root = Path(output_raw)
            source = source_root / "day" / "photo.jpg"
            source.parent.mkdir()
            source.write_bytes(b"photo-bytes")
            destination = output_root / "KR" / "Seoul" / "photo.jpg"

            copy_original(
                source,
                destination,
                source_root=source_root,
                destination_root=output_root,
            )

            self.assertEqual(source.read_bytes(), b"photo-bytes")
            self.assertEqual(destination.read_bytes(), b"photo-bytes")
            with open_confined_file(output_root, destination) as copied:
                self.assertEqual(copied.read(), b"photo-bytes")

    @unittest.skipIf(os.name == "nt", "descriptor-relative nofollow is POSIX-only")
    def test_confined_copy_rejects_source_and_destination_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as source_raw, tempfile.TemporaryDirectory() as output_raw, tempfile.TemporaryDirectory() as other_raw:
            source_root = Path(source_raw)
            output_root = Path(output_raw)
            other = Path(other_raw)
            (other / "outside.jpg").write_bytes(b"outside")
            (source_root / "link.jpg").symlink_to(other / "outside.jpg")
            with self.assertRaises(OSError):
                copy_original(
                    source_root / "link.jpg",
                    output_root / "copy.jpg",
                    source_root=source_root,
                    destination_root=output_root,
                )

            (source_root / "safe.jpg").write_bytes(b"safe")
            (output_root / "linked").symlink_to(other, target_is_directory=True)
            with self.assertRaises(OSError):
                copy_original(
                    source_root / "safe.jpg",
                    output_root / "linked" / "copy.jpg",
                    source_root=source_root,
                    destination_root=output_root,
                )


class ImportManifestTests(unittest.TestCase):
    def test_builds_versioned_manifest_with_oldest_capture(self) -> None:
        manifest = build_manifest(
            {
                "id": "batch-1",
                "name": "Trip",
                "source_type": "upload",
                "status": "ready",
                "oldest_captured_at": datetime(2020, 1, 2, 3, 4, 5),
            },
            [
                {
                    "id": "asset-1",
                    "original_name": "one.jpg",
                    "captured_at": datetime(2020, 1, 2, 3, 4, 5),
                    "classification": "photo",
                    "role": "gallery",
                    "excluded": 0,
                    "cluster_id": "cluster-1",
                }
            ],
            [
                {
                    "id": "cluster-1",
                    "latitude": 37.0,
                    "longitude": 127.0,
                    "publish_action": "create",
                    "asset_ids": ["asset-1"],
                }
            ],
        )
        self.assertEqual(manifest["version"], "travel-import.v1")
        self.assertEqual(manifest["batch"]["oldestCapturedAt"], "2020-01-02T03:04:05")
        self.assertEqual(manifest["clusters"][0]["assetIds"], ["asset-1"])


if __name__ == "__main__":
    unittest.main()
