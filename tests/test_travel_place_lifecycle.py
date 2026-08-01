from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from src.connectors import _create_tables, _extend_existing_tables
from src.routers import courses, imports, places
from src.schemas import TravelPlaceCreateRequest, TravelPlaceUpdateRequest
from src.services import import_cluster_assignments


OWNER = {
    "account_id": "owner-1",
    "login_id": "owner",
    "name": "Owner",
    "email": "owner@example.test",
    "permission": "user",
}
SUPERADMIN = {
    "account_id": "root-1",
    "login_id": "root",
    "name": "Root",
    "email": "root@example.test",
    "permission": "superadmin",
}
STRANGER = {
    "account_id": "stranger-1",
    "login_id": "stranger",
    "name": "Stranger",
    "email": "stranger@example.test",
    "permission": "user",
}


def _place_row(
    *,
    deleted_at: datetime | None = None,
    expectation: str = "confident",
) -> dict:
    now = datetime(2024, 1, 1, 12, 0, 0)
    return {
        "id": "place-1",
        "name": "Place",
        "category": "other",
        "latitude": 37.5,
        "longitude": 127.0,
        "address": None,
        "description": None,
        "opening_hours": None,
        "special_notes": None,
        "cover_image_url": None,
        "photo_urls_json": "[]",
        "cover_media_id": None,
        "photo_media_ids_json": "[]",
        "tags_json": "[]",
        "visibility": "public",
        "expectation": expectation,
        "deleted_at": deleted_at,
        "owner_account_id": OWNER["account_id"],
        "owner_login_id": OWNER["login_id"],
        "owner_name": OWNER["name"],
        "owner_email": OWNER["email"],
        "created_at": now,
        "updated_at": now,
    }


def _review_row(review_id: str) -> dict:
    now = datetime(2024, 1, 2, 12, 0, 0)
    return {
        "id": review_id,
        "place_id": "place-1",
        "rating": 5,
        "headline": None,
        "body": "Review",
        "visited_at": None,
        "photo_urls_json": "[]",
        "photo_media_ids_json": "[]",
        "author_account_id": OWNER["account_id"],
        "author_login_id": OWNER["login_id"],
        "author_name": OWNER["name"],
        "author_email": OWNER["email"],
        "created_at": now,
        "updated_at": now,
    }


def _mock_connection(get_db_connection: MagicMock) -> MagicMock:
    connection = MagicMock()
    cursor = MagicMock()
    get_db_connection.return_value.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    return cursor


class TravelPlaceSchemaAndMigrationTests(unittest.TestCase):
    def test_expectation_defaults_and_rejects_unknown_values(self) -> None:
        request = TravelPlaceCreateRequest(name="Place", latitude=37.5, longitude=127.0)
        self.assertEqual(request.expectation, "ordinary")
        self.assertEqual(
            TravelPlaceUpdateRequest(expectation="confident").expectation,
            "confident",
        )
        with self.assertRaises(ValidationError):
            TravelPlaceCreateRequest(
                name="Place",
                latitude=37.5,
                longitude=127.0,
                expectation="unknown",
            )

    def test_create_table_and_idempotent_extension_include_lifecycle_fields(
        self,
    ) -> None:
        cursor = MagicMock()
        _create_tables(cursor)
        create_place_table = cursor.execute.call_args_list[0].args[0]
        self.assertIn(
            "expectation ENUM('ordinary', 'confident') NOT NULL DEFAULT 'ordinary'",
            create_place_table,
        )
        self.assertIn("deleted_at DATETIME NULL", create_place_table)

        with (
            patch("src.connectors._ensure_column") as ensure_column,
            patch("src.connectors._ensure_index") as ensure_index,
        ):
            _extend_existing_tables(cursor)

        ensure_column.assert_any_call(
            cursor,
            "travel_places",
            "expectation",
            "ENUM('ordinary', 'confident') NOT NULL DEFAULT 'ordinary'",
        )
        ensure_column.assert_any_call(
            cursor, "travel_places", "deleted_at", "DATETIME NULL"
        )
        ensure_index.assert_any_call(
            cursor,
            "travel_places",
            "idx_place_owner_deleted",
            "owner_account_id, deleted_at",
        )


class TravelPlaceResponseTests(unittest.TestCase):
    @patch("src.routers.places.get_db_connection")
    def test_active_list_excludes_deleted_and_counts_reviews(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.side_effect = [
            [_place_row()],
            [
                {"place_id": "place-1", "photo_media_ids_json": "[]"},
                {"place_id": "place-1", "photo_media_ids_json": "[]"},
            ],
        ]

        result = asyncio.run(places.get_places(user=OWNER))

        self.assertEqual(result[0].expectation, "confident")
        self.assertEqual(result[0].reviewCount, 2)
        self.assertIsNone(result[0].deletedAt)
        list_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("p.deleted_at IS NULL", list_query)

    @patch("src.routers.places.get_db_connection")
    def test_active_list_filters_all_searchable_place_text(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.side_effect = [[], []]

        result = asyncio.run(places.get_places(q="  야경  ", user=OWNER))

        self.assertEqual(result, [])
        query, values = cursor.execute.call_args_list[0].args
        self.assertIn("LOWER(COALESCE(p.name, '')) LIKE %s", query)
        self.assertIn("LOWER(COALESCE(p.address, '')) LIKE %s", query)
        self.assertIn("LOWER(COALESCE(p.description, '')) LIKE %s", query)
        self.assertIn("LOWER(COALESCE(p.special_notes, '')) LIKE %s", query)
        self.assertEqual(values[-4:], ["%야경%"] * 4)

    @patch("src.routers.places.get_db_connection")
    def test_detail_counts_loaded_reviews_and_requires_active_place(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row()
        cursor.fetchall.return_value = [
            _review_row("review-1"),
            _review_row("review-2"),
        ]

        result = asyncio.run(places.get_place("place-1", OWNER))

        self.assertEqual(result.reviewCount, 2)
        self.assertEqual(len(result.reviews), 2)
        detail_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("deleted_at IS NULL", detail_query)

    @patch("src.routers.places.get_db_connection")
    def test_deleted_list_is_owner_scoped_and_latest_first(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        deleted_at = datetime(2024, 2, 1, 10, 30, 0)
        cursor.fetchall.side_effect = [
            [_place_row(deleted_at=deleted_at)],
            [{"place_id": "place-1", "photo_media_ids_json": "[]"}],
        ]

        result = asyncio.run(places.get_deleted_places(OWNER))

        self.assertEqual(result[0].reviewCount, 1)
        self.assertEqual(result[0].deletedAt, deleted_at.isoformat())
        deleted_query, values = cursor.execute.call_args_list[0].args
        self.assertIn("p.deleted_at IS NOT NULL", deleted_query)
        self.assertIn("p.owner_account_id = %s", deleted_query)
        self.assertIn("ORDER BY p.deleted_at DESC", deleted_query)
        self.assertEqual(values, [OWNER["account_id"]])

    def test_deleted_route_is_registered_before_dynamic_detail_route(self) -> None:
        get_paths = [
            route.path
            for route in places.router.routes
            if isinstance(route, APIRoute) and "GET" in route.methods
        ]
        self.assertLess(
            get_paths.index("/api/places/deleted"),
            get_paths.index("/api/places/{place_id}"),
        )


class TravelPlaceLifecycleRouteTests(unittest.TestCase):
    @patch("src.routers.places.cleanup_unreferenced_media")
    @patch("src.routers.places.get_db_connection")
    def test_delete_soft_deletes_without_course_or_media_cleanup(
        self,
        get_db_connection: MagicMock,
        cleanup_unreferenced_media: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row()

        result = asyncio.run(places.delete_place("place-1", OWNER))

        self.assertEqual(result, {"message": "Place deleted"})
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(
            any("SET deleted_at = CURRENT_TIMESTAMP" in sql for sql in statements)
        )
        self.assertFalse(any("travel_course_stops" in sql for sql in statements))
        self.assertFalse(any("DELETE FROM travel_places" in sql for sql in statements))
        cleanup_unreferenced_media.assert_not_called()

    @patch("src.routers.places.cleanup_unreferenced_media")
    @patch("src.routers.places.get_db_connection")
    def test_delete_is_idempotent_for_an_already_deleted_owned_place(
        self,
        get_db_connection: MagicMock,
        cleanup_unreferenced_media: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row(
            deleted_at=datetime(2024, 2, 1, 10, 30, 0)
        )

        result = asyncio.run(places.delete_place("place-1", OWNER))

        self.assertEqual(result, {"message": "Place deleted"})
        self.assertFalse(
            any(
                "SET deleted_at = CURRENT_TIMESTAMP" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )
        cleanup_unreferenced_media.assert_not_called()

    @patch("src.routers.places.get_place", new_callable=AsyncMock)
    @patch("src.routers.places.get_db_connection")
    def test_restore_allows_owner_and_returns_active_place(
        self, get_db_connection: MagicMock, get_place: AsyncMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row(
            deleted_at=datetime(2024, 2, 1, 10, 30, 0)
        )
        get_place.return_value = "restored"

        result = asyncio.run(places.restore_place("place-1", OWNER))

        self.assertEqual(result, "restored")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("SET deleted_at = NULL" in sql for sql in statements))
        get_place.assert_awaited_once_with("place-1", OWNER)

    @patch("src.routers.places.get_place", new_callable=AsyncMock)
    @patch("src.routers.places.get_db_connection")
    def test_restore_allows_superadmin(
        self, get_db_connection: MagicMock, get_place: AsyncMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row(
            deleted_at=datetime(2024, 2, 1, 10, 30, 0)
        )
        get_place.return_value = "restored"

        result = asyncio.run(places.restore_place("place-1", SUPERADMIN))

        self.assertEqual(result, "restored")
        get_place.assert_awaited_once_with("place-1", SUPERADMIN)

    @patch("src.routers.places.get_db_connection")
    def test_restore_rejects_non_owner(self, get_db_connection: MagicMock) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row(
            deleted_at=datetime(2024, 2, 1, 10, 30, 0)
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(places.restore_place("place-1", STRANGER))

        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(
            any(
                "SET deleted_at = NULL" in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    @patch("src.routers.places.cleanup_unreferenced_media")
    @patch("src.routers.places.get_place", new_callable=AsyncMock)
    @patch("src.routers.places.get_db_connection")
    def test_update_writes_expectation_only_for_active_place(
        self,
        get_db_connection: MagicMock,
        get_place: AsyncMock,
        cleanup_unreferenced_media: MagicMock,
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row()
        get_place.return_value = "updated"

        result = asyncio.run(
            places.update_place(
                "place-1",
                TravelPlaceUpdateRequest(expectation="ordinary"),
                OWNER,
            )
        )

        self.assertEqual(result, "updated")
        select_query = cursor.execute.call_args_list[0].args[0]
        update_query, values = cursor.execute.call_args_list[1].args
        self.assertIn("deleted_at IS NULL", select_query)
        self.assertIn("expectation = %s", update_query)
        self.assertEqual(values, ["ordinary", "place-1"])
        cleanup_unreferenced_media.assert_called_once_with([])

    @patch("src.routers.places.get_db_connection")
    def test_review_creation_rejects_deleted_place_lookup(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = None

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                places.create_review(
                    "place-1",
                    places.TravelReviewCreateRequest(rating=5, body="Review"),
                    OWNER,
                )
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("deleted_at IS NULL", cursor.execute.call_args.args[0])

    @patch("src.routers.places.generate_id", return_value="place-1")
    @patch("src.routers.places.get_db_connection")
    def test_create_persists_default_expectation(
        self, get_db_connection: MagicMock, _generate_id: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchone.return_value = _place_row(expectation="ordinary")

        result = asyncio.run(
            places.create_place(
                TravelPlaceCreateRequest(name="Place", latitude=37.5, longitude=127.0),
                OWNER,
            )
        )

        insert_call = cursor.execute.call_args_list[0]
        self.assertIn("expectation", insert_call.args[0])
        self.assertEqual(insert_call.args[1][15], "ordinary")
        self.assertEqual(result.expectation, "ordinary")
        self.assertEqual(result.reviewCount, 0)


class TravelPlaceActiveReferenceTests(unittest.TestCase):
    def test_course_and_import_manageability_queries_require_active_place(self) -> None:
        checks = (
            lambda cursor: courses._require_accessible_place_id(
                cursor, "place-1", OWNER
            ),
            lambda cursor: imports._require_manageable_place(cursor, "place-1", OWNER),
            lambda cursor: import_cluster_assignments._require_manageable_place(
                cursor, "place-1", OWNER
            ),
        )
        for check in checks:
            with self.subTest(check=check):
                cursor = MagicMock()
                cursor.fetchone.return_value = _place_row()
                check(cursor)
                self.assertIn("deleted_at IS NULL", cursor.execute.call_args.args[0])

    @patch("src.routers.places.get_db_connection")
    def test_deleted_list_superadmin_is_not_owner_scoped(
        self, get_db_connection: MagicMock
    ) -> None:
        cursor = _mock_connection(get_db_connection)
        cursor.fetchall.return_value = []

        result = asyncio.run(places.get_deleted_places(SUPERADMIN))

        self.assertEqual(result, [])
        query, values = cursor.execute.call_args.args
        self.assertNotIn("owner_account_id = %s", query)
        self.assertEqual(values, [])


if __name__ == "__main__":
    unittest.main()
