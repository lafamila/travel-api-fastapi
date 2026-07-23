from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from src.import_schemas import (
    ImportAssetPatchRequest,
    ImportClusterCreateRequest,
    ImportClusterDraftPatchRequest,
    ImportClusterMergeRequest,
    ImportClusterSplitRequest,
)
from src.routers.imports import (
    _publish_batch,
    _resolve_new_cluster_location,
    _select_publish_cover_asset_id,
    merge_import_clusters,
    patch_import_asset,
    patch_import_cluster,
    split_import_cluster,
)
from src.services.import_cluster_assignments import (
    ImportAssignmentError,
    assign_assets_to_cluster,
    create_cluster_with_assets,
    unassign_assets,
)
from src.services.import_processor import ImportProcessor
from src.services.import_repository import _map_asset
from src.services.place_links import PlaceLinkError, PlaceLinkResult


def _mock_connection(get_db_connection: MagicMock) -> MagicMock:
    connection = MagicMock()
    cursor = MagicMock()
    get_db_connection.return_value.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    return cursor


class ImportClusterRequestValidationTests(unittest.TestCase):
    def test_new_cluster_requires_a_complete_coordinate_source(self) -> None:
        with self.assertRaises(ValidationError):
            ImportClusterCreateRequest(assetIds=["asset-1"])
        with self.assertRaises(ValidationError):
            ImportClusterCreateRequest(
                assetIds=["asset-1"],
                latitude=37.5,
            )

        direct = ImportClusterCreateRequest(
            assetIds=["asset-1"],
            latitude=37.5,
            longitude=127.0,
        )
        linked = ImportClusterCreateRequest(
            assetIds=["asset-1"],
            mapLink="https://map.naver.com/p/entry/place/1",
        )

        self.assertEqual((direct.latitude, direct.longitude), (37.5, 127.0))
        self.assertEqual(linked.publishAction, "create")
        self.assertEqual(linked.visibility, "public")

    def test_new_cluster_rejects_duplicate_asset_ids(self) -> None:
        with self.assertRaises(ValidationError):
            ImportClusterCreateRequest(
                assetIds=["asset-1", "asset-1"],
                latitude=37.5,
                longitude=127.0,
            )


class ImportClusterLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_coordinates_do_not_call_link_resolver(self) -> None:
        body = ImportClusterCreateRequest(
            assetIds=["asset-1"], latitude=37.5, longitude=127.0
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(browser=None))
        )

        with patch(
            "src.routers.imports.resolve_supported_place_link",
            new_callable=AsyncMock,
        ) as resolver:
            location = await _resolve_new_cluster_location(body, request)

        resolver.assert_not_awaited()
        self.assertEqual(location["latitude"], 37.5)
        self.assertEqual(location["longitude"], 127.0)
        self.assertIsNone(location["map_link"])

    async def test_map_link_resolution_is_authoritative(self) -> None:
        body = ImportClusterCreateRequest(
            assetIds=["asset-1"],
            mapLink="https://map.naver.com/p/entry/place/1",
            latitude=1.0,
            longitude=2.0,
        )
        browser = object()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(browser=browser))
        )
        result = PlaceLinkResult(
            provider="naver",
            resolved_url="https://map.naver.com/p/entry/place/1?canonical=true",
            source_place_id="1",
            name="Resolved place",
            address="Resolved address",
            latitude=37.6404,
            longitude=127.0013,
        )

        with patch(
            "src.routers.imports.resolve_supported_place_link",
            new=AsyncMock(return_value=result),
        ) as resolver:
            location = await _resolve_new_cluster_location(body, request)

        resolver.assert_awaited_once_with(body.mapLink, browser=browser)
        self.assertEqual(location["latitude"], result.latitude)
        self.assertEqual(location["longitude"], result.longitude)
        self.assertEqual(location["map_link"], result.resolved_url)
        self.assertEqual(location["suggested_name"], result.name)
        self.assertEqual(location["resolved_address"], result.address)

    async def test_map_link_resolution_failure_is_a_400(self) -> None:
        body = ImportClusterCreateRequest(
            assetIds=["asset-1"], mapLink="https://maps.example/unsupported"
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(browser=None))
        )

        with patch(
            "src.routers.imports.resolve_supported_place_link",
            new=AsyncMock(side_effect=PlaceLinkError("Unsupported map link")),
        ):
            with self.assertRaises(HTTPException) as raised:
                await _resolve_new_cluster_location(body, request)

        self.assertEqual(raised.exception.status_code, 400)


class ImportAssignmentTransactionTests(unittest.TestCase):
    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_new_cluster_representative_must_be_gallery_or_cover(
        self, get_db_connection: MagicMock, _lock_mutable_batch: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.return_value = [
            {"id": "asset-1", "cluster_id": None, "role": "review"}
        ]

        with self.assertRaises(ImportAssignmentError) as raised:
            create_cluster_with_assets(
                batch_id="batch-1",
                asset_ids=["asset-1"],
                latitude=37.5,
                longitude=127.0,
                name="Draft",
                category="other",
                address=None,
                description=None,
                visibility="public",
                map_link=None,
                publish_action="create",
                existing_place_id=None,
                representative_asset_id="asset-1",
                suggested_name=None,
                resolved_address=None,
                user={"account_id": "admin"},
            )

        self.assertEqual(raised.exception.status_code, 422)

    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_cover_role_assignment_sets_destination_representative(
        self, get_db_connection: MagicMock, _lock_mutable_batch: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {"id": "cluster-1"}
        cursor.fetchall.return_value = [
            {"id": "asset-cover", "cluster_id": None, "role": "cover"}
        ]

        assign_assets_to_cluster(
            batch_id="batch-1",
            cluster_id="cluster-1",
            asset_ids=["asset-cover"],
        )

        representative_update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET representative_asset_id = %s" in call.args[0]
        )
        self.assertEqual(
            representative_update.args[1],
            ("asset-cover", "cluster-1", "batch-1"),
        )

    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_bulk_assignment_rejects_multiple_cover_roles_with_422(
        self, get_db_connection: MagicMock, _lock_mutable_batch: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {"id": "cluster-1"}
        cursor.fetchall.return_value = [
            {"id": "cover-1", "cluster_id": None, "role": "cover"},
            {"id": "cover-2", "cluster_id": None, "role": "cover"},
        ]

        with self.assertRaises(ImportAssignmentError) as raised:
            assign_assets_to_cluster(
                batch_id="batch-1",
                cluster_id="cluster-1",
                asset_ids=["cover-1", "cover-2"],
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(
            any(
                "SET cluster_id = %s" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_existing_cluster_assignment_changes_only_cluster_id(
        self, get_db_connection: MagicMock, _lock_mutable_batch: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {"id": "cluster-1"}
        cursor.fetchall.return_value = [
            {"id": "asset-1", "cluster_id": None, "role": "review"}
        ]

        assign_assets_to_cluster(
            batch_id="batch-1",
            cluster_id="cluster-1",
            asset_ids=["asset-1"],
        )

        update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET cluster_id = %s" in call.args[0]
        )
        self.assertNotIn("role", update.args[0])
        self.assertEqual(update.args[1], ("cluster-1", "batch-1", "asset-1"))

    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_unassignment_updates_only_cluster_id(
        self, get_db_connection: MagicMock, _lock_mutable_batch: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.return_value = [
            {"id": "asset-1", "cluster_id": "cluster-1", "role": "review"}
        ]

        unassign_assets(batch_id="batch-1", asset_ids=["asset-1"])

        update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET cluster_id = NULL" in call.args[0]
        )
        self.assertNotIn("role", update.args[0])
        self.assertNotIn("review", update.args[0])
        self.assertEqual(update.args[1], ("batch-1", "asset-1"))

    @patch(
        "src.services.import_cluster_assignments.generate_id",
        return_value="cluster-new",
    )
    @patch("src.connectors.pymysql.connect")
    def test_new_cluster_and_assignments_roll_back_together_on_failure(
        self, connect: MagicMock, _generate_id: MagicMock
    ) -> None:
        connection = MagicMock()
        cursor = MagicMock()
        connect.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        state: dict[str, object] = {}

        def execute(query: str, _parameters=None) -> None:
            if "FROM travel_import_batches" in query:
                state["one"] = {"id": "batch-1", "status": "draft"}
            elif "SELECT id, cluster_id, role" in query:
                state["all"] = [
                    {"id": "asset-1", "cluster_id": None, "role": "gallery"}
                ]
            elif "MAX(sort_order)" in query:
                state["one"] = {"next_sort_order": 1}
            elif "UPDATE travel_import_assets SET cluster_id" in query:
                raise RuntimeError("simulated assignment failure")

        cursor.execute.side_effect = execute
        cursor.fetchone.side_effect = lambda: state.get("one")
        cursor.fetchall.side_effect = lambda: state.get("all", [])

        with self.assertRaisesRegex(RuntimeError, "assignment failure"):
            create_cluster_with_assets(
                batch_id="batch-1",
                asset_ids=["asset-1"],
                latitude=37.5,
                longitude=127.0,
                name="Draft",
                category="other",
                address=None,
                description=None,
                visibility="public",
                map_link=None,
                publish_action="create",
                existing_place_id=None,
                representative_asset_id=None,
                suggested_name=None,
                resolved_address=None,
                user={"account_id": "admin"},
            )

        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()
        self.assertTrue(
            any(
                "INSERT INTO travel_import_clusters" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    @patch(
        "src.services.import_cluster_assignments.generate_id",
        return_value="cluster-new",
    )
    @patch("src.services.import_cluster_assignments.lock_mutable_batch")
    @patch("src.services.import_cluster_assignments.get_db_connection")
    def test_new_cluster_adopts_single_unassigned_cover_role(
        self,
        get_db_connection: MagicMock,
        _lock_mutable_batch: MagicMock,
        _generate_id: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.return_value = [
            {"id": "asset-cover", "cluster_id": None, "role": "cover"}
        ]
        cursor.fetchone.return_value = {"next_sort_order": 1}
        cursor.rowcount = 1

        create_cluster_with_assets(
            batch_id="batch-1",
            asset_ids=["asset-cover"],
            latitude=37.5,
            longitude=127.0,
            name=None,
            category=None,
            address=None,
            description=None,
            visibility="public",
            map_link=None,
            publish_action="create",
            existing_place_id=None,
            representative_asset_id=None,
            suggested_name=None,
            resolved_address=None,
            user={"account_id": "admin"},
        )

        representative_update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET representative_asset_id = %s" in call.args[0]
        )
        self.assertEqual(representative_update.args[1][0], "asset-cover")


class ImportCoverAndClusteringTests(unittest.IsolatedAsyncioTestCase):
    @patch("src.routers.imports._require_batch")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_explicit_cover_demotes_previous_cluster_cover(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _require_batch: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {
            "id": "asset-new-cover",
            "batch_id": "batch-1",
            "cluster_id": "cluster-1",
            "classification": "photo",
            "role": "gallery",
            "captured_at": None,
        }

        await patch_import_asset(
            "batch-1",
            "asset-new-cover",
            ImportAssetPatchRequest(role="cover"),
        )

        statements = [call.args for call in cursor.execute.call_args_list]
        demotion = next(
            parameters
            for query, parameters in statements
            if "SET role = 'gallery'" in query
        )
        self.assertEqual(
            demotion,
            ("batch-1", "cluster-1", "asset-new-cover"),
        )
        representative_update = next(
            parameters
            for query, parameters in statements
            if "SET representative_asset_id = %s" in query
        )
        self.assertEqual(
            representative_update,
            ("asset-new-cover", "cluster-1", "batch-1"),
        )


class ImportRepresentativePatchTests(unittest.IsolatedAsyncioTestCase):
    @patch("src.routers.imports._require_batch")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_patch_representative_sets_cover_and_demotes_prior_cover(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _require_batch: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.side_effect = [
            {"publish_action": "create", "existing_place_id": None},
            {"id": "asset-new-cover", "role": "gallery"},
        ]

        await patch_import_cluster(
            "batch-1",
            "cluster-1",
            ImportClusterDraftPatchRequest(representativeAssetId="asset-new-cover"),
            user={"account_id": "admin"},
        )

        statements = [call.args for call in cursor.execute.call_args_list]
        self.assertTrue(
            any("SET role = 'gallery'" in query for query, _parameters in statements)
        )
        selected_cover = next(
            parameters
            for query, parameters in statements
            if "SET role = 'cover'" in query
        )
        self.assertEqual(
            selected_cover,
            ("asset-new-cover", "batch-1", "cluster-1"),
        )
        representative_update = next(
            parameters
            for query, parameters in statements
            if "SET representative_asset_id = %s" in query
        )
        self.assertEqual(
            representative_update,
            ("asset-new-cover", "cluster-1", "batch-1"),
        )

    @patch("src.routers.imports._require_batch")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_clearing_representative_demotes_cluster_covers(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _require_batch: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {
            "publish_action": "create",
            "existing_place_id": None,
        }

        await patch_import_cluster(
            "batch-1",
            "cluster-1",
            ImportClusterDraftPatchRequest(representativeAssetId=None),
            user={"account_id": "admin"},
        )

        statements = [call.args for call in cursor.execute.call_args_list]
        demotion = next(
            parameters
            for query, parameters in statements
            if "cluster_id = %s AND role = 'cover'" in query
        )
        self.assertEqual(demotion, ("batch-1", "cluster-1"))
        representative_update = next(
            parameters
            for query, parameters in statements
            if "SET representative_asset_id = %s" in query
        )
        self.assertEqual(representative_update[0], None)


class ImportMergeSplitRepresentativeTests(unittest.IsolatedAsyncioTestCase):
    @patch("src.routers.imports._require_batch")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_merge_keeps_target_explicit_representative_not_geographic_medoid(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _require_batch: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.side_effect = [
            [
                {
                    "id": "cluster-1",
                    "sort_order": 1,
                    "representative_asset_id": "asset-explicit",
                },
                {
                    "id": "cluster-2",
                    "sort_order": 2,
                    "representative_asset_id": "asset-other",
                },
            ],
            [
                {"id": "asset-explicit", "latitude": 37.5, "longitude": 127.0},
                {"id": "asset-medoid", "latitude": 37.5001, "longitude": 127.0001},
            ],
        ]

        await merge_import_clusters(
            "batch-1",
            ImportClusterMergeRequest(clusterIds=["cluster-1", "cluster-2"]),
        )

        representative_updates = [
            call.args[1]
            for call in cursor.execute.call_args_list
            if "SET representative_asset_id = %s" in call.args[0]
        ]
        self.assertIn(
            ("asset-explicit", "cluster-1", "batch-1"),
            representative_updates,
        )
        geographic_update = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "SET latitude = %s, longitude = %s" in call.args[0]
        )
        self.assertNotIn("representative_asset_id", geographic_update)

    @patch("src.routers.imports.generate_id", return_value="cluster-new")
    @patch("src.routers.imports._require_batch")
    @patch("src.routers.imports.get_batch_detail", return_value={"id": "batch-1"})
    @patch("src.routers.imports._refresh_manifest")
    @patch("src.routers.imports._lock_draft_mutation")
    @patch("src.routers.imports.get_db_connection")
    async def test_split_starts_new_cluster_null_and_clears_moved_representative(
        self,
        get_db_connection: MagicMock,
        _lock_draft_mutation: MagicMock,
        _refresh_manifest: MagicMock,
        _get_batch_detail: MagicMock,
        _require_batch: MagicMock,
        _generate_id: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = {
            "id": "cluster-1",
            "sort_order": 1,
            "representative_asset_id": "asset-explicit",
        }
        cursor.fetchall.return_value = [
            {"id": "asset-explicit", "latitude": 37.5, "longitude": 127.0},
            {"id": "asset-stays", "latitude": 37.5001, "longitude": 127.0001},
        ]

        await split_import_cluster(
            "batch-1",
            ImportClusterSplitRequest(
                clusterId="cluster-1", assetIds=["asset-explicit"]
            ),
        )

        cluster_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_import_clusters" in call.args[0]
        )
        self.assertIsNone(cluster_insert.args[1][3])
        representative_updates = [
            call.args[1]
            for call in cursor.execute.call_args_list
            if "SET representative_asset_id = %s" in call.args[0]
        ]
        self.assertIn((None, "cluster-new", "batch-1"), representative_updates)
        self.assertIn((None, "cluster-1", "batch-1"), representative_updates)


class ImportAutoClusteringTests(unittest.TestCase):
    @patch("src.services.import_processor.generate_id", return_value="cluster-1")
    @patch("src.services.import_processor.get_db_connection")
    def test_auto_cluster_assignment_resets_included_photos_to_gallery(
        self, get_db_connection: MagicMock, _generate_id: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.return_value = [
            {"id": "asset-1", "latitude": 37.5, "longitude": 127.0},
            {"id": "asset-2", "latitude": 37.5001, "longitude": 127.0001},
        ]

        ImportProcessor()._replace_clusters("batch-1")

        assignment = next(
            call
            for call in cursor.execute.call_args_list
            if "SET cluster_id = %s, role = 'gallery'" in call.args[0]
        )
        self.assertEqual(assignment.args[1][0], "cluster-1")
        self.assertEqual(set(assignment.args[1][1:]), {"asset-1", "asset-2"})
        self.assertFalse(
            any(
                "role = 'cover'" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )
        cluster_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_import_clusters" in call.args[0]
        )
        self.assertIsNone(cluster_insert.args[1][3])


class ImportPublishCoverTests(unittest.TestCase):
    @patch("src.routers.imports._publish_asset_file")
    @patch("src.routers.imports.get_db_connection")
    def test_publish_query_orders_fallback_and_persists_first_gallery_as_cover(
        self, get_db_connection: MagicMock, publish_asset_file: MagicMock
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
                {"id": "gallery-first", "role": "gallery"},
                {"id": "gallery-second", "role": "gallery"},
            ],
        ]
        cursor.fetchone.return_value = None
        publish_asset_file.side_effect = (
            lambda _cursor, _place_id, asset, _owner_id: f"media-{asset['id']}"
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

        asset_select = next(
            call.args[0]
            for call in cursor.execute.call_args_list
            if "FROM travel_import_assets asset" in call.args[0]
        )
        self.assertIn(
            "ORDER BY asset.captured_at ASC, asset.created_at ASC, asset.id ASC",
            asset_select,
        )
        place_insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO travel_places" in call.args[0]
        )
        self.assertEqual(
            place_insert.args[1][7],
            "media-gallery-first",
        )

    def test_publish_falls_back_to_first_ordered_gallery_without_mutating_role(
        self,
    ) -> None:
        assets = [
            {"id": "gallery-first", "role": "gallery"},
            {"id": "review", "role": "review"},
            {"id": "gallery-second", "role": "gallery"},
        ]

        selected = _select_publish_cover_asset_id(assets)

        self.assertEqual(selected, "gallery-first")
        self.assertEqual(
            [asset["role"] for asset in assets], ["gallery", "review", "gallery"]
        )

    def test_explicit_cover_wins_over_gallery_fallback(self) -> None:
        assets = [
            {"id": "gallery-first", "role": "gallery"},
            {"id": "explicit-cover", "role": "cover"},
        ]

        self.assertEqual(_select_publish_cover_asset_id(assets), "explicit-cover")


class ImportUnassignedAssetContractTests(unittest.TestCase):
    def test_unassigned_is_expressed_by_null_cluster_regardless_of_role(self) -> None:
        asset = _map_asset(
            {
                "id": "asset-1",
                "batch_id": "batch-1",
                "original_name": "review.jpg",
                "cluster_id": None,
                "role": "review",
                "classification": "photo",
            }
        )

        self.assertIsNone(asset["clusterId"])
        self.assertEqual(asset["role"], "review")
        self.assertEqual(asset["classification"], "photo")


if __name__ == "__main__":
    unittest.main()
