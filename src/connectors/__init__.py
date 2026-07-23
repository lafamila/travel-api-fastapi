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
            _create_media_table(cursor)
            _create_import_tables(cursor)
            _extend_existing_tables(cursor)
            _extend_import_tables(cursor)
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


def _create_media_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_media (
            id VARCHAR(50) PRIMARY KEY,
            bucket_name VARCHAR(255) NOT NULL,
            object_key VARCHAR(1500) NOT NULL,
            content_type VARCHAR(255) NOT NULL DEFAULT 'application/octet-stream',
            original_name VARCHAR(500) NULL,
            byte_size BIGINT NULL,
            owner_account_id VARCHAR(64) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'temporary',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_travel_media_object (bucket_name(100), object_key(600)),
            INDEX idx_travel_media_owner_status (owner_account_id, status, created_at)
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
            "cover_media_id": "VARCHAR(50) NULL",
            "photo_media_ids_json": "LONGTEXT NULL",
        },
        "travel_place_reviews": {
            "author_account_id": "VARCHAR(64) NULL",
            "author_login_id": "VARCHAR(255) NULL",
            "author_name": "VARCHAR(255) NULL",
            "author_email": "VARCHAR(320) NULL",
            "photo_media_ids_json": "LONGTEXT NULL",
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


def _create_import_tables(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_batches (
            id VARCHAR(50) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            source_type VARCHAR(20) NOT NULL,
            source_path VARCHAR(1000) NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'draft',
            progress_current INT NOT NULL DEFAULT 0,
            progress_total INT NOT NULL DEFAULT 0,
            error_text TEXT NULL,
            oldest_captured_at DATETIME NULL,
            manifest_version VARCHAR(50) NOT NULL DEFAULT 'travel-import.v1',
            manifest_json LONGTEXT NULL,
            owner_account_id VARCHAR(64) NOT NULL,
            owner_login_id VARCHAR(255) NOT NULL,
            owner_name VARCHAR(255) NOT NULL,
            owner_email VARCHAR(320) NULL,
            published_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_import_batch_owner_created (owner_account_id, created_at),
            INDEX idx_import_batch_status (status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_clusters (
            id VARCHAR(50) PRIMARY KEY,
            batch_id VARCHAR(50) NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            representative_asset_id VARCHAR(50) NULL,
            latitude DOUBLE NOT NULL,
            longitude DOUBLE NOT NULL,
            country_code VARCHAR(10) NULL,
            country VARCHAR(255) NULL,
            city VARCHAR(255) NULL,
            address VARCHAR(1000) NULL,
            suggested_name VARCHAR(255) NULL,
            draft_name VARCHAR(255) NULL,
            draft_category VARCHAR(50) NULL,
            draft_address VARCHAR(1000) NULL,
            draft_description TEXT NULL,
            draft_visibility VARCHAR(20) NULL DEFAULT 'public',
            map_link VARCHAR(2000) NULL,
            publish_action VARCHAR(20) NOT NULL DEFAULT 'create',
            existing_place_id VARCHAR(50) NULL,
            published_place_id VARCHAR(50) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES travel_import_batches(id) ON DELETE CASCADE,
            INDEX idx_import_cluster_batch (batch_id, sort_order)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_assets (
            id VARCHAR(50) PRIMARY KEY,
            batch_id VARCHAR(50) NOT NULL,
            cluster_id VARCHAR(50) NULL,
            source_ref VARCHAR(255) NOT NULL,
            original_name VARCHAR(500) NOT NULL,
            media_type VARCHAR(100) NULL,
            byte_size BIGINT NULL,
            storage_kind VARCHAR(20) NOT NULL,
            staging_key VARCHAR(1500) NULL,
            local_source_path VARCHAR(1500) NULL,
            organized_path VARCHAR(1500) NULL,
            preview_key VARCHAR(1500) NULL,
            thumbnail_key VARCHAR(1500) NULL,
            sha256 CHAR(64) NULL,
            captured_at DATETIME NULL,
            latitude DOUBLE NULL,
            longitude DOUBLE NULL,
            metadata_json LONGTEXT NULL,
            classification VARCHAR(40) NOT NULL DEFAULT 'pending',
            classification_reason VARCHAR(255) NULL,
            manual_exclusion_reason VARCHAR(40) NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'gallery',
            excluded TINYINT(1) NOT NULL DEFAULT 0,
            duplicate_of_asset_id VARCHAR(50) NULL,
            processed_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES travel_import_batches(id) ON DELETE CASCADE,
            UNIQUE KEY uq_import_asset_source (batch_id, source_ref),
            INDEX idx_import_asset_batch_cluster (batch_id, cluster_id),
            INDEX idx_import_asset_batch_sha (batch_id, sha256),
            INDEX idx_import_asset_role (batch_id, role)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_review_drafts (
            id VARCHAR(50) PRIMARY KEY,
            batch_id VARCHAR(50) NOT NULL,
            asset_id VARCHAR(50) NOT NULL,
            rating INT NULL,
            headline VARCHAR(255) NULL,
            body TEXT NULL,
            visited_at DATETIME NULL,
            published_review_id VARCHAR(50) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES travel_import_batches(id) ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES travel_import_assets(id) ON DELETE CASCADE,
            UNIQUE KEY uq_import_review_asset (asset_id),
            INDEX idx_import_review_batch (batch_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_geocode_cache (
            cache_key VARCHAR(100) PRIMARY KEY,
            latitude DOUBLE NOT NULL,
            longitude DOUBLE NOT NULL,
            response_json LONGTEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_import_jobs (
            id VARCHAR(50) PRIMARY KEY,
            batch_id VARCHAR(50) NOT NULL,
            job_type VARCHAR(30) NOT NULL DEFAULT 'process',
            status VARCHAR(20) NOT NULL DEFAULT 'queued',
            progress_current INT NOT NULL DEFAULT 0,
            progress_total INT NOT NULL DEFAULT 0,
            attempts INT NOT NULL DEFAULT 0,
            worker_id VARCHAR(255) NULL,
            error_text TEXT NULL,
            claimed_at DATETIME NULL,
            heartbeat_at DATETIME NULL,
            completed_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (batch_id) REFERENCES travel_import_batches(id) ON DELETE CASCADE,
            INDEX idx_import_job_claim (status, created_at),
            INDEX idx_import_job_batch (batch_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


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


def _extend_import_tables(cursor) -> None:
    _ensure_column(
        cursor,
        "travel_import_assets",
        "thumbnail_key",
        "VARCHAR(1500) NULL",
    )
    _ensure_column(
        cursor,
        "travel_import_assets",
        "manual_exclusion_reason",
        "VARCHAR(40) NULL",
    )
    _ensure_column(
        cursor,
        "travel_import_clusters",
        "map_link",
        "VARCHAR(2000) NULL",
    )
    cursor.execute(
        "UPDATE travel_import_clusters SET draft_visibility = 'public' "
        "WHERE draft_visibility IS NULL OR draft_visibility = ''"
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
