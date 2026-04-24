from __future__ import annotations

import os
from contextlib import contextmanager

import pymysql
from dotenv import load_dotenv

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


def init_db():
    connection = pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        charset="utf8mb4",
    )

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {DB_CONFIG['database']} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE {DB_CONFIG['database']}")

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
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_place_coordinates (latitude, longitude),
                    INDEX idx_place_category (category)
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
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (place_id) REFERENCES travel_places(id) ON DELETE CASCADE,
                    INDEX idx_review_place (place_id),
                    INDEX idx_review_visited (visited_at)
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
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_course_dates (trip_start_at, trip_end_at)
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

            try:
                cursor.execute(
                    "ALTER TABLE travel_course_stops DROP FOREIGN KEY travel_course_stops_ibfk_2"
                )
            except Exception:
                pass

            try:
                cursor.execute(
                    """
                    ALTER TABLE travel_course_stops
                    ADD CONSTRAINT fk_travel_course_stops_place
                    FOREIGN KEY (place_id) REFERENCES travel_places(id) ON DELETE RESTRICT
                    """
                )
            except Exception:
                pass

        connection.commit()
    finally:
        connection.close()
