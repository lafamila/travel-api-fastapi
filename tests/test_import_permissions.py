from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from src.auth_utils import require_admin
from src.routers.imports import router
from src.services.import_repository import lock_mutable_batch


class _BatchCursor:
    def __init__(self, batch):
        self.batch = batch
        self.query = ""

    def execute(self, query, _parameters):
        self.query = query

    def fetchone(self):
        return self.batch


class ImportRoutePermissionTests(unittest.TestCase):
    def test_every_import_operation_requires_admin_dependency(self) -> None:
        routes = [route for route in router.routes if isinstance(route, APIRoute)]
        self.assertTrue(routes)
        for route in routes:
            dependencies = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_admin, dependencies, route.path)

    def test_draft_mutation_lock_allows_only_explicit_lifecycle_states(self) -> None:
        for status in ("draft", "failed", "ready"):
            cursor = _BatchCursor({"id": "batch-1", "status": status})
            self.assertEqual(lock_mutable_batch(cursor, "batch-1")["status"], status)
            self.assertIn("FOR UPDATE", cursor.query)

        for status in ("queued", "processing", "publishing", "published"):
            with self.assertRaises(ValueError):
                lock_mutable_batch(
                    _BatchCursor({"id": "batch-1", "status": status}),
                    "batch-1",
                )

    def test_upload_lock_rejects_ready_batch(self) -> None:
        with self.assertRaises(ValueError):
            lock_mutable_batch(
                _BatchCursor({"id": "batch-1", "status": "ready"}),
                "batch-1",
                allowed_statuses=frozenset({"draft", "failed"}),
            )


if __name__ == "__main__":
    unittest.main()
