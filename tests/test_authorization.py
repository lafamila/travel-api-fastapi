from __future__ import annotations

import unittest

from src.services.authorization import (
    can_access_course,
    can_manage_place,
    can_review_place,
    can_view_place,
)


OWNER = {"account_id": "owner", "permission": "user"}
FRIEND = {"account_id": "friend", "permission": "user"}
STRANGER = {"account_id": "stranger", "permission": "user"}
SUPERADMIN = {"account_id": "root", "permission": "superadmin"}
PUBLIC_PLACE = {"owner_account_id": "owner", "visibility": "public"}
PRIVATE_PLACE = {"owner_account_id": "owner", "visibility": "private"}


class PlaceAuthorizationTests(unittest.TestCase):
    def test_owner_can_view_manage_and_review_private_place(self) -> None:
        self.assertTrue(can_view_place(OWNER, PRIVATE_PLACE, False))
        self.assertTrue(can_manage_place(OWNER, PRIVATE_PLACE))
        self.assertTrue(can_review_place(OWNER, PRIVATE_PLACE, False))

    def test_friend_can_view_and_review_public_but_cannot_manage(self) -> None:
        self.assertTrue(can_view_place(FRIEND, PUBLIC_PLACE, True))
        self.assertTrue(can_review_place(FRIEND, PUBLIC_PLACE, True))
        self.assertFalse(can_manage_place(FRIEND, PUBLIC_PLACE))

    def test_friend_cannot_view_or_review_private_place(self) -> None:
        self.assertFalse(can_view_place(FRIEND, PRIVATE_PLACE, True))
        self.assertFalse(can_review_place(FRIEND, PRIVATE_PLACE, True))

    def test_non_friend_cannot_view_public_place_or_review(self) -> None:
        self.assertFalse(can_view_place(STRANGER, PUBLIC_PLACE, False))
        self.assertFalse(can_review_place(STRANGER, PUBLIC_PLACE, False))

    def test_superadmin_has_full_place_access(self) -> None:
        self.assertTrue(can_view_place(SUPERADMIN, PRIVATE_PLACE, False))
        self.assertTrue(can_manage_place(SUPERADMIN, PRIVATE_PLACE))
        self.assertTrue(can_review_place(SUPERADMIN, PRIVATE_PLACE, False))


class CourseAuthorizationTests(unittest.TestCase):
    def test_course_is_private_to_owner(self) -> None:
        course = {"owner_account_id": "owner"}
        self.assertTrue(can_access_course(OWNER, course))
        self.assertFalse(can_access_course(FRIEND, course))
        self.assertTrue(can_access_course(SUPERADMIN, course))


if __name__ == "__main__":
    unittest.main()
