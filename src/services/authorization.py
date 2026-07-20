from __future__ import annotations


def is_superadmin(user: dict) -> bool:
    return user.get("permission") == "superadmin"


def can_view_place(user: dict, place: dict, are_friends: bool) -> bool:
    return (
        is_superadmin(user)
        or place.get("owner_account_id") == user.get("account_id")
        or (place.get("visibility") == "public" and are_friends)
    )


def can_manage_place(user: dict, place: dict) -> bool:
    return is_superadmin(user) or place.get("owner_account_id") == user.get(
        "account_id"
    )


def can_review_place(user: dict, place: dict, are_friends: bool) -> bool:
    return can_view_place(user, place, are_friends)


def can_access_course(user: dict, course: dict) -> bool:
    return is_superadmin(user) or course.get("owner_account_id") == user.get(
        "account_id"
    )


def are_friends(cursor, account_id: str, other_account_id: str) -> bool:
    if account_id == other_account_id:
        return False
    account_a_id, account_b_id = sorted((account_id, other_account_id))
    cursor.execute(
        """
        SELECT 1
        FROM travel_friendships
        WHERE account_a_id = %s AND account_b_id = %s
        LIMIT 1
        """,
        (account_a_id, account_b_id),
    )
    return cursor.fetchone() is not None
