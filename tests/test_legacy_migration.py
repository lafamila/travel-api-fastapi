from __future__ import annotations

import unittest

from src.connectors import migrate_legacy_ownership


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql: str, values: tuple) -> None:
        self.calls.append((" ".join(sql.split()), values))


class _Connection:
    def __init__(self) -> None:
        self.cursor_instance = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class LegacyOwnershipMigrationTests(unittest.TestCase):
    def test_migration_uses_conditional_coalesce_updates_and_is_repeatable(
        self,
    ) -> None:
        connection = _Connection()
        owner = {
            "accountId": "account-1",
            "loginId": "lafamila",
            "name": "Lafamila",
            "email": "lafamila@example.test",
        }

        migrate_legacy_ownership(connection, owner)
        migrate_legacy_ownership(connection, owner)

        self.assertEqual(connection.commits, 2)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(len(connection.cursor_instance.calls), 6)
        for sql, values in connection.cursor_instance.calls:
            self.assertIn("COALESCE", sql)
            self.assertIn("IS NULL", sql)
            self.assertEqual(values[0], "account-1")

    def test_migration_rejects_incomplete_owner(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "missing required metadata"):
            migrate_legacy_ownership(
                _Connection(), {"accountId": "account-1", "loginId": "lafamila"}
            )


if __name__ == "__main__":
    unittest.main()
