from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Callable

import pymysql
from dotenv import load_dotenv

from ..config import TRAVEL_LEGACY_OWNER_LOGIN_ID

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "travelnote"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


@contextmanager
def get_db_connection():
    connection = pymysql.connect(**DB_CONFIG)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(
    legacy_account_resolver: Callable[[str], dict] | None = None,
) -> None:
    database = _quote_identifier(DB_CONFIG["database"])
    connection = pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {database} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE {database}")
            _create_tables(cursor)
            _extend_existing_tables(cursor)
            cursor.execute(
                "UPDATE travel_places SET visibility = 'public' "
                "WHERE visibility IS NULL OR visibility = ''"
            )
        connection.commit()

        if _legacy_ownership_is_missing(connection):
            resolver = legacy_account_resolver or _default_legacy_account_resolver
            try:
                owner = resolver(TRAVEL_LEGACY_OWNER_LOGIN_ID)
            except Exception as exc:
                raise RuntimeError(
                    "Legacy travel ownership migration could not resolve auth account "
                    f"loginId={TRAVEL_LEGACY_OWNER_LOGIN_ID!r}: {exc}"
                ) from exc
            migrate_legacy_ownership(connection, owner)
    finally:
        connection.close()


def migrate_legacy_ownership(connection, owner: dict) -> None:
    account_id = str(owner.get("accountId") or "").strip()
    login_id = str(owner.get("loginId") or "").strip()
    name = str(owner.get("name") or "").strip()
    email = owner.get("email") or None
    if not account_id or not login_id or not name:
        raise RuntimeError("Legacy owner account response is missing required metadata")

    try:
        with connection.cursor() as cursor:
            owner_values = (account_id, login_id, name, email)
            cursor.execute(
                """
                UPDATE travel_places
                SET owner_account_id = COALESCE(owner_account_id, %s),
                    owner_login_id = COALESCE(owner_login_id, %s),
                    owner_name = COALESCE(owner_name, %s),
                    owner_email = COALESCE(owner_email, %s),
                    visibility = COALESCE(NULLIF(visibility, ''), 'public')
                WHERE owner_account_id IS NULL OR owner_login_id IS NULL
                   OR owner_name IS NULL
                """,
                owner_values,
            )
            cursor.execute(
                """
                UPDATE travel_place_reviews
                SET author_account_id = COALESCE(author_account_id, %s),
                    author_login_id = COALESCE(author_login_id, %s),
                    author_name = COALESCE(author_name, %s),
                    author_email = COALESCE(author_email, %s)
                WHERE author_account_id IS NULL OR author_login_id IS NULL
                   OR author_name IS NULL
                """,
                owner_values,
            )
            cursor.execute(
                """
                UPDATE travel_courses
                SET owner_account_id = COALESCE(owner_account_id, %s),
                    owner_login_id = COALESCE(owner_login_id, %s),
                    owner_name = COALESCE(owner_name, %s),
                    owner_email = COALESCE(owner_email, %s)
                WHERE owner_account_id IS NULL OR owner_login_id IS NULL
                   OR owner_name IS NULL
                """,
                owner_values,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _create_tables(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_places (
            id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            category VARCHAR(50) NOT NULL DEFAULT 'other',
            latitude DOUBLE NOT NULL,
            longitude DOUBLE NOT NULL,
            address VARCHAR(500) NULL,
            description TEXT NULL,
            opening_hours TEXT NULL,
            special_notes TEXT NULL,
            cover_image_url VARCHAR(1000) NULL,
            photo_urls_json LONGTEXT NULL,
            tags_json TEXT NULL,
            owner_account_id VARCHAR(64) NOT NULL,
            owner_login_id VARCHAR(255) NOT NULL,
            owner_name VARCHAR(255) NOT NULL,
            owner_email VARCHAR(320) NULL,
            visibility VARCHAR(20) NOT NULL DEFAULT 'public',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_place_coordinates (latitude, longitude),
            INDEX idx_place_category (category),
            INDEX idx_place_owner_visibility (owner_account_id, visibility)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_place_reviews (
            id VARCHAR(50) PRIMARY KEY,
            place_id VARCHAR(50) NOT NULL,
            rating INT NOT NULL,
            headline VARCHAR(255) NULL,
            body TEXT NOT NULL,
            visited_at DATETIME NULL,
            photo_urls_json LONGTEXT NULL,
            author_account_id VARCHAR(64) NOT NULL,
            author_login_id VARCHAR(255) NOT NULL,
            author_name VARCHAR(255) NOT NULL,
            author_email VARCHAR(320) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (place_id) REFERENCES travel_places(id) ON DELETE CASCADE,
            INDEX idx_review_place (place_id),
            INDEX idx_review_visited (visited_at),
            INDEX idx_review_author (author_account_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_courses (
            id VARCHAR(50) PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            start_location VARCHAR(255) NULL,
            trip_start_at DATETIME NULL,
            trip_end_at DATETIME NULL,
            transport_mode VARCHAR(50) NULL,
            summary TEXT NULL,
            prompt_text LONGTEXT NULL,
            output_format_version VARCHAR(20) NOT NULL DEFAULT '1.0',
            source_payload_json LONGTEXT NULL,
            import_payload_json LONGTEXT NULL,
            owner_account_id VARCHAR(64) NOT NULL,
            owner_login_id VARCHAR(255) NOT NULL,
            owner_name VARCHAR(255) NOT NULL,
            owner_email VARCHAR(320) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_course_dates (trip_start_at, trip_end_at),
            INDEX idx_course_owner (owner_account_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_course_stops (
            id VARCHAR(50) PRIMARY KEY,
            course_id VARCHAR(50) NOT NULL,
            place_id VARCHAR(50) NOT NULL,
            place_name VARCHAR(255) NOT NULL,
            stop_order INT NOT NULL,
            scheduled_at DATETIME NULL,
            note TEXT NULL,
            reasoning_text TEXT NULL,
            transit_hint VARCHAR(255) NULL,
            FOREIGN KEY (course_id) REFERENCES travel_courses(id) ON DELETE CASCADE,
            FOREIGN KEY (place_id) REFERENCES travel_places(id) ON DELETE RESTRICT,
            INDEX idx_place_id (place_id),
            INDEX idx_course_stop_order (course_id, stop_order)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_friend_requests (
            id VARCHAR(50) PRIMARY KEY,
            requester_account_id VARCHAR(64) NOT NULL,
            requester_login_id VARCHAR(255) NOT NULL,
            requester_name VARCHAR(255) NOT NULL,
            requester_email VARCHAR(320) NULL,
            addressee_account_id VARCHAR(64) NOT NULL,
            addressee_login_id VARCHAR(255) NOT NULL,
            addressee_name VARCHAR(255) NOT NULL,
            addressee_email VARCHAR(320) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            responded_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_friend_request_incoming (addressee_account_id, status, created_at),
            INDEX idx_friend_request_outgoing (requester_account_id, status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_friendships (
            id VARCHAR(50) PRIMARY KEY,
            account_a_id VARCHAR(64) NOT NULL,
            account_a_login_id VARCHAR(255) NOT NULL,
            account_a_name VARCHAR(255) NOT NULL,
            account_a_email VARCHAR(320) NULL,
            account_b_id VARCHAR(64) NOT NULL,
            account_b_login_id VARCHAR(255) NOT NULL,
            account_b_name VARCHAR(255) NOT NULL,
            account_b_email VARCHAR(320) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_friendship_pair (account_a_id, account_b_id),
            INDEX idx_friendship_account_b (account_b_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def _extend_existing_tables(cursor) -> None:
    columns = {
        "travel_places": {
            "owner_account_id": "VARCHAR(64) NULL",
            "owner_login_id": "VARCHAR(255) NULL",
            "owner_name": "VARCHAR(255) NULL",
            "owner_email": "VARCHAR(320) NULL",
            "visibility": "VARCHAR(20) NULL DEFAULT 'public'",
        },
        "travel_place_reviews": {
            "author_account_id": "VARCHAR(64) NULL",
            "author_login_id": "VARCHAR(255) NULL",
            "author_name": "VARCHAR(255) NULL",
            "author_email": "VARCHAR(320) NULL",
        },
        "travel_courses": {
            "owner_account_id": "VARCHAR(64) NULL",
            "owner_login_id": "VARCHAR(255) NULL",
            "owner_name": "VARCHAR(255) NULL",
            "owner_email": "VARCHAR(320) NULL",
        },
    }
    for table, table_columns in columns.items():
        for column, definition in table_columns.items():
            _ensure_column(cursor, table, column, definition)
    _ensure_index(
        cursor,
        "travel_places",
        "idx_place_owner_visibility",
        "owner_account_id, visibility",
    )
    _ensure_index(
        cursor, "travel_place_reviews", "idx_review_author", "author_account_id"
    )
    _ensure_index(cursor, "travel_courses", "idx_course_owner", "owner_account_id")


def _ensure_column(cursor, table: str, column: str, definition: str) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """,
        (DB_CONFIG["database"], table, column),
    )
    if cursor.fetchone()["count"] == 0:
        cursor.execute(
            f"ALTER TABLE {_quote_identifier(table)} "
            f"ADD COLUMN {_quote_identifier(column)} {definition}"
        )


def _ensure_index(cursor, table: str, index: str, columns: str) -> None:
    cursor.execute(
        """
        SELECT COUNT(*) AS count
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
        """,
        (DB_CONFIG["database"], table, index),
    )
    if cursor.fetchone()["count"] == 0:
        cursor.execute(
            f"CREATE INDEX {_quote_identifier(index)} "
            f"ON {_quote_identifier(table)} ({columns})"
        )


def _legacy_ownership_is_missing(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM travel_places
               WHERE owner_account_id IS NULL OR owner_login_id IS NULL OR owner_name IS NULL)
              +
              (SELECT COUNT(*) FROM travel_place_reviews
               WHERE author_account_id IS NULL OR author_login_id IS NULL OR author_name IS NULL)
              +
              (SELECT COUNT(*) FROM travel_courses
               WHERE owner_account_id IS NULL OR owner_login_id IS NULL OR owner_name IS NULL)
              AS missing_count
            """
        )
        return int(cursor.fetchone()["missing_count"]) > 0


def _default_legacy_account_resolver(login_id: str) -> dict:
    from ..services.session_auth import get_session_service

    return get_session_service().find_exact_account_by_login_id(login_id)


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f"`{value}`"
