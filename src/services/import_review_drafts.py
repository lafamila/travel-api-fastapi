from __future__ import annotations


def lock_review_draft_ids_for_assets(
    cursor,
    asset_ids: list[str],
) -> list[str]:
    if not asset_ids:
        return []
    placeholders = ",".join(["%s"] * len(asset_ids))
    cursor.execute(
        f"""
        SELECT review.id AS draft_id
        FROM travel_import_review_drafts review
        WHERE review.id IN (
            SELECT link.draft_id
            FROM travel_import_review_draft_assets link
            WHERE link.asset_id IN ({placeholders})
        )
        FOR UPDATE
        """,
        tuple(asset_ids),
    )
    return sorted(
        {row.get("draft_id") for row in cursor.fetchall() if row.get("draft_id")}
    )


def refresh_review_draft_visited_at(
    cursor,
    draft_ids: list[str],
) -> None:
    unique_ids = sorted(set(draft_ids))
    if not unique_ids:
        return
    placeholders = ",".join(["%s"] * len(unique_ids))
    cursor.execute(
        f"""
        UPDATE travel_import_review_drafts review
        JOIN (
            SELECT link.draft_id, MIN(asset.captured_at) AS oldest_captured_at
            FROM travel_import_review_draft_assets link
            JOIN travel_import_assets asset ON asset.id = link.asset_id
            WHERE link.draft_id IN ({placeholders})
              AND asset.captured_at IS NOT NULL
            GROUP BY link.draft_id
        ) capture ON capture.draft_id = review.id
        SET review.visited_at = capture.oldest_captured_at
        """,
        tuple(unique_ids),
    )


def detach_review_assets(
    cursor,
    *,
    asset_ids: list[str],
    retained_cluster_id: str | None = None,
) -> list[str]:
    affected_draft_ids = lock_review_draft_ids_for_assets(cursor, asset_ids)
    if not asset_ids:
        return affected_draft_ids
    placeholders = ",".join(["%s"] * len(asset_ids))
    if retained_cluster_id is None:
        cursor.execute(
            f"DELETE FROM travel_import_review_draft_assets "
            f"WHERE asset_id IN ({placeholders})",
            tuple(asset_ids),
        )
    else:
        cursor.execute(
            f"""
            DELETE link
            FROM travel_import_review_draft_assets link
            JOIN travel_import_review_drafts review ON review.id = link.draft_id
            WHERE link.asset_id IN ({placeholders})
              AND review.cluster_id <> %s
            """,
            (*asset_ids, retained_cluster_id),
        )
    refresh_review_draft_visited_at(cursor, affected_draft_ids)
    return affected_draft_ids
