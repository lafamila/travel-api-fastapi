from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi import HTTPException

from src.connectors import _extend_import_tables
from src.routers.imports import get_import_asset_thumbnail
from src.services.import_processor import ImportProcessor
from src.services.import_repository import _map_asset


class _SchemaCursor:
    def __init__(
        self,
        existing_columns: set[tuple[str, str]],
        *,
        nullable_columns: set[tuple[str, str]] | None = None,
        existing_indexes: set[tuple[str, str]] | None = None,
    ) -> None:
        self.existing_columns = existing_columns
        self.nullable_columns = nullable_columns or set()
        self.existing_indexes = existing_indexes or set()
        self.result: dict = {"count": 0}
        self.statements: list[str] = []

    def execute(self, query, parameters=None) -> None:
        self.statements.append(query)
        key = (parameters[1], parameters[2]) if parameters else None
        if "IS_NULLABLE" in query:
            self.result = {
                "is_nullable": "YES" if key in self.nullable_columns else "NO"
            }
        elif "information_schema.COLUMNS" in query:
            self.result = {"count": int(key in self.existing_columns)}
        elif "information_schema.STATISTICS" in query:
            self.result = {"count": int(key in self.existing_indexes)}

    def fetchone(self) -> dict:
        return self.result


def _mock_database_row(get_db_connection: Mock, row: dict | None) -> Mock:
    connection = MagicMock()
    cursor = MagicMock()
    get_db_connection.return_value.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.return_value = row
    return cursor


class ImportThumbnailSchemaTests(unittest.TestCase):
    def test_thumbnail_column_extension_is_idempotent(self) -> None:
        existing = {
            ("travel_import_assets", "thumbnail_key"),
            ("travel_import_assets", "manual_exclusion_reason"),
            ("travel_import_clusters", "map_link"),
            ("travel_import_clusters", "draft_opening_hours"),
            ("travel_import_clusters", "draft_special_notes"),
            ("travel_import_clusters", "draft_tags_json"),
            ("travel_import_review_drafts", "cluster_id"),
        }
        cursor = _SchemaCursor(
            existing,
            nullable_columns={("travel_import_review_drafts", "asset_id")},
        )

        _extend_import_tables(cursor)

        self.assertFalse(any("ALTER TABLE" in query for query in cursor.statements))

    def test_thumbnail_column_is_added_to_an_existing_import_table(self) -> None:
        cursor = _SchemaCursor(
            {
                ("travel_import_assets", "manual_exclusion_reason"),
                ("travel_import_clusters", "map_link"),
                ("travel_import_clusters", "draft_opening_hours"),
                ("travel_import_clusters", "draft_special_notes"),
                ("travel_import_clusters", "draft_tags_json"),
                ("travel_import_review_drafts", "cluster_id"),
            },
            nullable_columns={("travel_import_review_drafts", "asset_id")},
        )

        _extend_import_tables(cursor)

        alterations = [query for query in cursor.statements if "ALTER TABLE" in query]
        self.assertEqual(len(alterations), 1)
        self.assertIn("`thumbnail_key` VARCHAR(1500) NULL", alterations[0])

    def test_asset_review_schema_is_migrated_idempotently(self) -> None:
        cursor = _SchemaCursor(
            {
                ("travel_import_assets", "thumbnail_key"),
                ("travel_import_assets", "manual_exclusion_reason"),
                ("travel_import_clusters", "map_link"),
                ("travel_import_clusters", "draft_opening_hours"),
                ("travel_import_clusters", "draft_special_notes"),
                ("travel_import_clusters", "draft_tags_json"),
            },
            existing_indexes={
                ("travel_import_review_drafts", "uq_import_review_batch_cluster")
            },
        )

        _extend_import_tables(cursor)

        statements = "\n".join(cursor.statements)
        self.assertIn("ADD COLUMN `cluster_id` VARCHAR(50) NULL", statements)
        self.assertIn("MODIFY COLUMN `asset_id` VARCHAR(50) NULL", statements)
        self.assertIn(
            "INSERT INTO travel_import_review_draft_assets (draft_id, asset_id)",
            statements,
        )
        self.assertIn("SELECT review.id, review.asset_id", statements)
        self.assertNotIn("keeper_id", statements)
        self.assertNotIn("SET review.cluster_id = NULL", statements)
        self.assertIn("SET asset_id = NULL", statements)
        self.assertLess(
            statements.index(
                "INSERT INTO travel_import_review_draft_assets (draft_id, asset_id)"
            ),
            statements.index("SET asset_id = NULL"),
        )
        self.assertIn("ON DUPLICATE KEY UPDATE draft_id = VALUES(draft_id)", statements)
        self.assertIn("DROP INDEX `uq_import_review_batch_cluster`", statements)

    def test_cluster_draft_columns_are_added_idempotently(self) -> None:
        cursor = _SchemaCursor(
            {
                ("travel_import_assets", "thumbnail_key"),
                ("travel_import_assets", "manual_exclusion_reason"),
                ("travel_import_clusters", "map_link"),
                ("travel_import_review_drafts", "cluster_id"),
            },
            nullable_columns={("travel_import_review_drafts", "asset_id")},
        )

        _extend_import_tables(cursor)

        statements = "\n".join(cursor.statements)
        self.assertIn(
            "ADD COLUMN `draft_opening_hours` TEXT NULL",
            statements,
        )
        self.assertIn(
            "ADD COLUMN `draft_special_notes` TEXT NULL",
            statements,
        )
        self.assertIn(
            "ADD COLUMN `draft_tags_json` TEXT NULL",
            statements,
        )


class ImportThumbnailProcessorTests(unittest.TestCase):
    def test_image_and_video_thumbnails_use_bounded_jpeg_and_deterministic_key(
        self,
    ) -> None:
        processor = ImportProcessor()

        with tempfile.TemporaryDirectory() as raw:
            work_root = Path(raw)
            for suffix in (".jpg", ".mp4"):
                with self.subTest(suffix=suffix):
                    source = work_root / f"source{suffix}"
                    source.write_bytes(b"source")
                    commands: list[list[str]] = []

                    def create_output(command: list[str], destination: Path) -> None:
                        commands.append(command)
                        destination.write_bytes(b"jpeg")

                    with (
                        patch.object(
                            processor,
                            "_run_preview_command",
                            side_effect=create_output,
                        ),
                        patch(
                            "src.services.import_processor.upload_path_to_key"
                        ) as upload,
                    ):
                        key = processor._create_thumbnail(
                            "batch-1",
                            {"id": "asset-1"},
                            source,
                            work_root,
                        )

                    self.assertEqual(key, "imports/batch-1/thumbnails/asset-1.jpg")
                    ffmpeg = commands[-1]
                    self.assertEqual(ffmpeg[0], "ffmpeg")
                    self.assertIn(
                        "scale=480:480:force_original_aspect_ratio=decrease",
                        ffmpeg,
                    )
                    self.assertEqual(ffmpeg[ffmpeg.index("-frames:v") + 1], "1")
                    upload.assert_called_once_with(
                        work_root / "thumbnail-asset-1.jpg",
                        "imports/batch-1/thumbnails/asset-1.jpg",
                        "image/jpeg",
                    )

    def test_process_backfills_processed_asset_without_thumbnail(self) -> None:
        processor = ImportProcessor()
        batch = {"id": "batch-1", "source_type": "upload"}
        asset = {
            "id": "asset-1",
            "original_name": "photo.jpg",
            "processed_at": "2026-07-22T00:00:00",
            "thumbnail_key": None,
        }
        processor._load_batch = Mock(return_value=batch)
        processor._expand_uploaded_zips = Mock()
        processor._load_assets = Mock(return_value=[asset])
        processor._backfill_thumbnail = Mock()
        processor._update_oldest_captured_at = Mock()
        processor._replace_clusters = Mock(return_value=[])
        processor._organize_assets = Mock()
        processor._persist_manifest = Mock()

        with patch("src.services.import_processor.update_job_progress"):
            processor.process({"id": "job-1", "batch_id": "batch-1"})

        processor._backfill_thumbnail.assert_called_once()
        call = processor._backfill_thumbnail.call_args.args
        self.assertEqual(call[0], batch)
        self.assertEqual(call[1], asset)
        processor._replace_clusters.assert_not_called()
        processor._organize_assets.assert_not_called()
        processor._persist_manifest.assert_not_called()


class ImportThumbnailContractTests(unittest.TestCase):
    def test_asset_contract_reports_thumbnail_url_and_availability(self) -> None:
        available = _map_asset(
            {
                "id": "asset-1",
                "batch_id": "batch-1",
                "original_name": "photo.jpg",
                "thumbnail_key": "imports/batch-1/thumbnails/asset-1.jpg",
            }
        )
        missing = _map_asset(
            {
                "id": "asset-2",
                "batch_id": "batch-1",
                "original_name": "photo.jpg",
            }
        )

        self.assertTrue(available["thumbnailAvailable"])
        self.assertEqual(
            available["thumbnailUrl"],
            "/api/imports/batch-1/assets/asset-1/thumbnail",
        )
        self.assertFalse(missing["thumbnailAvailable"])
        self.assertIsNone(missing["thumbnailUrl"])


class ImportThumbnailRouteTests(unittest.IsolatedAsyncioTestCase):
    @patch("src.routers.imports.get_object")
    @patch("src.routers.imports.get_db_connection")
    async def test_thumbnail_route_streams_only_thumbnail_key_with_safe_headers(
        self, get_db_connection: Mock, get_object: Mock
    ) -> None:
        cursor = _mock_database_row(
            get_db_connection,
            {"thumbnail_key": "imports/batch-1/thumbnails/asset-1.jpg"},
        )
        get_object.return_value = {"Body": io.BytesIO(b"jpeg")}

        response = await get_import_asset_thumbnail("batch-1", "asset-1")

        self.assertIn("SELECT thumbnail_key", cursor.execute.call_args.args[0])
        get_object.assert_called_once_with("imports/batch-1/thumbnails/asset-1.jpg")
        self.assertEqual(response.media_type, "image/jpeg")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            response.headers["content-security-policy"],
            "sandbox; default-src 'none'",
        )
        self.assertEqual(response.headers["cache-control"], "private, max-age=3600")

    @patch("src.routers.imports.get_object")
    @patch("src.routers.imports.get_db_connection")
    async def test_thumbnail_route_does_not_fall_back_to_original(
        self, get_db_connection: Mock, get_object: Mock
    ) -> None:
        _mock_database_row(get_db_connection, {"thumbnail_key": None})

        with self.assertRaises(HTTPException) as raised:
            await get_import_asset_thumbnail("batch-1", "asset-1")

        self.assertEqual(raised.exception.status_code, 404)
        get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
