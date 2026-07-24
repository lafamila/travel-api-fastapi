from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from src.connectors import _create_import_tables
from src.import_schemas import (
    ImportReviewDraftCreateRequest,
    ImportReviewDraftPatchRequest,
)
from src.routers.imports import (
    _claim_publish_batch,
    _publish_batch,
    _validate_batch,
    create_import_review_draft,
    delete_import_review_draft,
    patch_import_review_draft,
)
from src.services.import_repository import _map_review_draft


def _mock_connection(get_db_connection: MagicMock) -> MagicMock:
    connection = MagicMock()
    cursor = MagicMock()
    get_db_connection.return_value.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    return cursor


class ImportReviewSchemaTests(unittest.TestCase):
    def test_schema_allows_multiple_cluster_drafts_and_one_draft_per_asset(
        self,
    ) -> None:
        cursor = MagicMock()

        _create_import_tables(cursor)

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        review_table = next(
            statement
            for statement in statements
            if "CREATE TABLE IF NOT EXISTS travel_import_review_drafts (" in statement
        )
        junction_table = next(
            statement
            for statement in statements
            if "CREATE TABLE IF NOT EXISTS travel_import_review_draft_assets"
            in statement
        )
        self.assertIn("cluster_id VARCHAR(50) NULL", review_table)
        self.assertNotIn(
            "uq_import_review_batch_cluster",
            review_table,
        )
        self.assertIn("PRIMARY KEY (asset_id)", junction_table)
        self.assertIn("ON DELETE CASCADE", junction_table)


class ImportReviewSerializationTests(unittest.TestCase):
    def test_maps_top_level_review_draft_contract(self) -> None:
        mapped = _map_review_draft(
            {
                "id": "draft-1",
                "batch_id": "batch-1",
                "cluster_id": "cluster-1",
                "rating": 5,
                "headline": "Great",
                "body": "Loved it",
                "visited_at": datetime(2024, 2, 3, 4, 5, 6),
                "published_review_id": "review-1",
                "created_at": datetime(2024, 2, 4, 4, 5, 6),
                "updated_at": datetime(2024, 2, 5, 4, 5, 6),
            },
            ["asset-2", "asset-1"],
        )

        self.assertEqual(
            mapped,
            {
                "id": "draft-1",
                "batchId": "batch-1",
                "clusterId": "cluster-1",
                "rating": 5,
                "headline": "Great",
                "body": "Loved it",
                "visitedAt": "2024-02-03T04:05:06",
                "assetIds": ["asset-1", "asset-2"],
                "publishedReviewId": "review-1",
                "createdAt": "2024-02-04T04:05:06",
                "updatedAt": "2024-02-05T04:05:06",
            },
        )


class ImportReviewCrudTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "src.routers.imports.generate_id",
        side_effect=["draft-1", "draft-2"],
    )
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_create_allows_two_drafts_in_the_same_cluster(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _generate_id: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.side_effect = [
            {"id": "cluster-1"},
            {"id": "cluster-1"},
        ]

        await create_import_review_draft(
            "batch-1",
            "cluster-1",
            ImportReviewDraftCreateRequest(
                rating=5,
                body="First review",
                assetIds=[],
            ),
        )
        await create_import_review_draft(
            "batch-1",
            "cluster-1",
            ImportReviewDraftCreateRequest(
                rating=4,
                body="Second review",
                assetIds=[],
            ),
        )

        inserts = [
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_import_review_drafts" in call.args[0]
        ]
        self.assertEqual(len(inserts), 2)
        self.assertEqual(
            [(call.args[1][0], call.args[1][2], call.args[1][5]) for call in inserts],
            [
                ("draft-1", "cluster-1", "First review"),
                ("draft-2", "cluster-1", "Second review"),
            ],
        )
        self.assertFalse(
            any(
                "SELECT id FROM travel_import_review_drafts" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    @patch("src.routers.imports.generate_id", return_value="draft-1")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_create_links_multiple_assets_and_forces_oldest_capture(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _generate_id: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.side_effect = [{"id": "cluster-1"}, None]
        cursor.fetchall.side_effect = [
            [
                {"id": "asset-1", "captured_at": datetime(2024, 3, 2, 12, 0, 0)},
                {"id": "asset-2", "captured_at": datetime(2024, 3, 1, 9, 0, 0)},
            ],
            [],
        ]

        result = await create_import_review_draft(
            "batch-1",
            "cluster-1",
            ImportReviewDraftCreateRequest(
                rating=5,
                headline="Trip",
                body="Good",
                visitedAt=datetime(2024, 3, 10, 0, 0, 0),
                assetIds=["asset-1", "asset-2"],
            ),
        )

        self.assertEqual(result, {"id": "batch-1"})
        cursor.executemany.assert_called_once()
        junction_sql, junction_values = cursor.executemany.call_args.args
        self.assertIn("ON DUPLICATE KEY UPDATE draft_id", junction_sql)
        self.assertEqual(
            junction_values,
            [("draft-1", "asset-1"), ("draft-1", "asset-2")],
        )
        role_update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET role = 'review', excluded = 0" in call.args[0]
        )
        self.assertEqual(role_update.args[1], ("asset-1", "asset-2"))
        visited_update = next(
            call
            for call in cursor.execute.call_args_list
            if "MIN(asset.captured_at) AS oldest_captured_at" in call.args[0]
        )
        self.assertEqual(visited_update.args[1], ("draft-1",))

    @patch("src.routers.imports.generate_id", return_value="draft-1")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_create_rejects_asset_from_another_cluster(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _generate_id: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.side_effect = [{"id": "cluster-1"}, None]
        cursor.fetchall.return_value = [{"id": "asset-1", "captured_at": None}]

        with self.assertRaises(HTTPException) as raised:
            await create_import_review_draft(
                "batch-1",
                "cluster-1",
                ImportReviewDraftCreateRequest(assetIds=["asset-1", "asset-other"]),
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(
            any(
                "INSERT INTO travel_import_review_drafts" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    @patch("src.routers.imports._require_batch")
    async def test_patch_replaces_links_without_demoting_removed_review_assets(
        self,
        _require_batch: MagicMock,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {
            "id": "draft-1",
            "batch_id": "batch-1",
            "cluster_id": "cluster-1",
        }
        cursor.fetchall.side_effect = [
            [{"id": "asset-2", "captured_at": datetime(2024, 4, 2, 8, 0, 0)}],
            [{"draft_id": "draft-old"}],
        ]

        await patch_import_review_draft(
            "batch-1",
            "draft-1",
            ImportReviewDraftPatchRequest(assetIds=["asset-2"]),
        )

        self.assertTrue(
            any(
                "DELETE FROM travel_import_review_draft_assets WHERE draft_id = %s"
                in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )
        self.assertFalse(
            any(
                "SET role = 'gallery'" in call.args[0]
                or "SET role = 'excluded'" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )
        visited_update = next(
            call
            for call in cursor.execute.call_args_list
            if "MIN(asset.captured_at) AS oldest_captured_at" in call.args[0]
        )
        self.assertEqual(visited_update.args[1], ("draft-1", "draft-old"))
        calls = cursor.execute.call_args_list
        lock_index = next(
            index
            for index, call in enumerate(calls)
            if "SELECT review.id AS draft_id" in call.args[0]
        )
        delete_index = next(
            index
            for index, call in enumerate(calls)
            if "DELETE FROM travel_import_review_draft_assets WHERE draft_id = %s"
            in call.args[0]
        )
        self.assertLess(lock_index, delete_index)
        self.assertLess(delete_index, calls.index(visited_update))

    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_delete_keeps_asset_roles_unchanged(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.rowcount = 1

        await delete_import_review_draft("batch-1", "draft-1")

        self.assertFalse(
            any(
                "UPDATE travel_import_assets" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )


class ImportReviewValidationTests(unittest.TestCase):
    @patch("src.routers.imports.get_batch_detail")
    def test_warns_per_draft_and_for_unlinked_review_assets(
        self, get_batch_detail: MagicMock
    ) -> None:
        get_batch_detail.return_value = {
            "status": "ready",
            "clusters": [
                {
                    "id": "cluster-1",
                    "publishAction": "create",
                    "draft": {"name": "Place", "category": "other"},
                    "assetIds": ["asset-1", "asset-2"],
                }
            ],
            "assets": [
                {
                    "id": "asset-1",
                    "clusterId": "cluster-1",
                    "role": "review",
                    "excluded": False,
                },
                {
                    "id": "asset-2",
                    "clusterId": "cluster-1",
                    "role": "review",
                    "excluded": False,
                },
            ],
            "reviewDrafts": [
                {
                    "id": "draft-1",
                    "clusterId": "cluster-1",
                    "rating": None,
                    "body": "",
                    "assetIds": ["asset-1"],
                }
            ],
        }

        result = _validate_batch("batch-1")

        self.assertTrue(result["valid"])
        self.assertEqual(
            result["warnings"],
            [
                {
                    "code": "incomplete-review",
                    "reviewId": "draft-1",
                    "clusterId": "cluster-1",
                    "missingFields": ["rating", "body"],
                },
                {
                    "code": "unlinked-review-asset",
                    "assetId": "asset-2",
                    "clusterId": "cluster-1",
                },
            ],
        )
        self.assertNotIn("assetId", result["warnings"][0])


class ImportReviewPublishTests(unittest.TestCase):
    @patch("src.routers.imports.generate_id", side_effect=["place-1", "review-1"])
    @patch("src.routers.imports.get_db_connection")
    def test_publish_reserves_complete_review_by_draft(
        self, get_db_connection: MagicMock, _generate_id: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {"status": "ready"}
        cursor.fetchall.side_effect = [
            [
                {
                    "id": "cluster-1",
                    "publish_action": "create",
                    "published_place_id": None,
                }
            ],
            [{"id": "draft-1", "published_review_id": None}],
        ]

        claimed = _claim_publish_batch(
            "batch-1",
            {"account_id": "admin-1"},
        )

        self.assertTrue(claimed)
        reservation_query = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "SELECT review.id, review.published_review_id" in call.args[0]
        )
        self.assertIn("JOIN travel_import_clusters", reservation_query)
        self.assertNotIn("travel_import_assets", reservation_query)
        self.assertTrue(
            any(
                call.args[1] == ("review-1", "draft-1")
                for call in cursor.execute.call_args_list
                if "SET published_review_id" in call.args[0]
            )
        )

    @patch("src.routers.imports._publish_asset_file")
    @patch("src.routers.imports.get_db_connection")
    def test_publish_creates_one_review_with_all_linked_media(
        self,
        get_db_connection: MagicMock,
        publish_asset_file: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.side_effect = [
            [
                {
                    "id": "cluster-1",
                    "publish_action": "create",
                    "published_place_id": "place-1",
                    "draft_name": "Draft place",
                    "draft_category": "other",
                    "latitude": 37.5,
                    "longitude": 127.0,
                    "draft_address": None,
                    "address": None,
                    "draft_description": None,
                    "draft_visibility": "public",
                }
            ],
            [
                {"id": "asset-1", "role": "review"},
                {"id": "asset-2", "role": "review"},
            ],
            [
                {
                    "id": "draft-1",
                    "rating": 5,
                    "headline": "Great",
                    "body": "Loved it",
                    "visited_at": datetime(2024, 5, 1, 10, 0, 0),
                    "published_review_id": "review-1",
                }
            ],
            [{"asset_id": "asset-1"}, {"asset_id": "asset-2"}],
        ]
        cursor.fetchone.return_value = None
        publish_asset_file.side_effect = lambda _cursor, _place_id, asset, _owner_id: (
            f"media-{asset['id']}"
        )

        _publish_batch(
            "batch-1",
            {
                "account_id": "admin-1",
                "login_id": "admin",
                "name": "Admin",
                "email": "admin@example.com",
            },
        )

        review_inserts = [
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_place_reviews" in call.args[0]
        ]
        self.assertEqual(len(review_inserts), 1)
        self.assertEqual(
            json.loads(review_inserts[0].args[1][6]),
            ["media-asset-1", "media-asset-2"],
        )
        self.assertEqual(review_inserts[0].args[1][0], "review-1")
        place_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_places" in call.args[0]
        )
        self.assertEqual(json.loads(place_insert.args[1][10]), [])
        self.assertTrue(
            any(
                call.args[1] == ("review-1", "draft-1")
                for call in cursor.execute.call_args_list
                if "SET published_review_id" in call.args[0]
            )
        )


if __name__ == "__main__":
    unittest.main()
