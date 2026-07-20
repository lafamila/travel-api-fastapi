from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth_utils import get_current_user
from ..connectors import get_db_connection
from ..schemas import (
    FriendAccount,
    FriendRequestCreateRequest,
    FriendRequestView,
    FriendshipView,
    FriendSearchResult,
)
from ..services.authorization import are_friends
from ..services.session_auth import get_session_service
from ..utils import generate_id

router = APIRouter(prefix="/api/friends", tags=["friends"])


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _account_from_row(row: dict, prefix: str) -> FriendAccount:
    return FriendAccount(
        accountId=row[f"{prefix}_account_id"],
        loginId=row[f"{prefix}_login_id"],
        name=row[f"{prefix}_name"],
        email=row.get(f"{prefix}_email"),
    )


def _friendship_account_from_row(row: dict, prefix: str) -> FriendAccount:
    return FriendAccount(
        accountId=row[f"{prefix}_id"],
        loginId=row[f"{prefix}_login_id"],
        name=row[f"{prefix}_name"],
        email=row.get(f"{prefix}_email"),
    )


def _map_request(row: dict) -> FriendRequestView:
    return FriendRequestView(
        id=row["id"],
        status=row["status"],
        requester=_account_from_row(row, "requester"),
        addressee=_account_from_row(row, "addressee"),
        createdAt=_iso(row["created_at"]),
        respondedAt=_iso(row.get("responded_at")),
    )


def _user_account(user: dict) -> dict:
    return {
        "accountId": user["account_id"],
        "loginId": user["login_id"],
        "name": user["name"],
        "email": user.get("email"),
    }


async def _lookup_target(account_id: str, login_id: str) -> dict:
    accounts = await get_session_service().search_accounts(login_id)
    matches = [
        account
        for account in accounts
        if account.get("accountId") == account_id
        and account.get("loginId") == login_id
        and account.get("status") == "active"
    ]
    if len(matches) != 1:
        raise HTTPException(status_code=404, detail="Account not found")
    target = matches[0]
    if target.get("permission") == "visitor" and not target.get("isSuperAdmin"):
        raise HTTPException(
            status_code=409, detail="Account does not have Travel access"
        )
    return target


@router.get("/search", response_model=list[FriendSearchResult])
async def search_users(
    q: str = Query(min_length=1),
    user: dict = Depends(get_current_user),
):
    accounts = await get_session_service().search_accounts(q)
    return [
        FriendSearchResult(**account)
        for account in accounts
        if account.get("accountId") != user["account_id"]
        and account.get("status") == "active"
        and (account.get("permission") != "visitor" or account.get("isSuperAdmin"))
    ]


@router.post(
    "/requests",
    response_model=FriendRequestView,
    status_code=status.HTTP_201_CREATED,
)
async def send_friend_request(
    body: FriendRequestCreateRequest,
    user: dict = Depends(get_current_user),
):
    if body.accountId == user["account_id"]:
        raise HTTPException(
            status_code=400, detail="Cannot send a friend request to self"
        )
    target = await _lookup_target(body.accountId, body.loginId)
    requester = _user_account(user)

    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            if are_friends(cursor, user["account_id"], target["accountId"]):
                raise HTTPException(
                    status_code=409, detail="Accounts are already friends"
                )
            cursor.execute(
                """
                SELECT id FROM travel_friend_requests
                WHERE status = 'pending'
                  AND ((requester_account_id = %s AND addressee_account_id = %s)
                    OR (requester_account_id = %s AND addressee_account_id = %s))
                LIMIT 1
                """,
                (
                    user["account_id"],
                    target["accountId"],
                    target["accountId"],
                    user["account_id"],
                ),
            )
            if cursor.fetchone():
                raise HTTPException(
                    status_code=409, detail="A pending friend request already exists"
                )
            request_id = generate_id("friend_request")
            cursor.execute(
                """
                INSERT INTO travel_friend_requests (
                    id,
                    requester_account_id, requester_login_id, requester_name, requester_email,
                    addressee_account_id, addressee_login_id, addressee_name, addressee_email
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    request_id,
                    requester["accountId"],
                    requester["loginId"],
                    requester["name"],
                    requester["email"],
                    target["accountId"],
                    target["loginId"],
                    target["name"],
                    target.get("email"),
                ),
            )
            cursor.execute(
                "SELECT * FROM travel_friend_requests WHERE id = %s", (request_id,)
            )
            return _map_request(cursor.fetchone())


@router.get("/requests/incoming", response_model=list[FriendRequestView])
async def list_incoming_requests(user: dict = Depends(get_current_user)):
    return _list_requests("addressee_account_id", user["account_id"])


@router.get("/requests/outgoing", response_model=list[FriendRequestView])
async def list_outgoing_requests(user: dict = Depends(get_current_user)):
    return _list_requests("requester_account_id", user["account_id"])


def _list_requests(account_column: str, account_id: str) -> list[FriendRequestView]:
    if account_column not in {"requester_account_id", "addressee_account_id"}:
        raise ValueError("Invalid friend request account column")
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT * FROM travel_friend_requests
                WHERE {account_column} = %s AND status = 'pending'
                ORDER BY created_at DESC
                """,
                (account_id,),
            )
            return [_map_request(row) for row in cursor.fetchall()]


@router.post("/requests/{request_id}/accept", response_model=FriendshipView)
async def accept_friend_request(
    request_id: str, user: dict = Depends(get_current_user)
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM travel_friend_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            row = cursor.fetchone()
            _require_pending_addressee(row, user["account_id"])
            requester = _account_from_row(row, "requester")
            addressee = _account_from_row(row, "addressee")
            first, second = sorted(
                (requester, addressee), key=lambda account: account.accountId
            )
            friendship_id = generate_id("friendship")
            cursor.execute(
                """
                INSERT INTO travel_friendships (
                    id,
                    account_a_id, account_a_login_id, account_a_name, account_a_email,
                    account_b_id, account_b_login_id, account_b_name, account_b_email
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    friendship_id,
                    first.accountId,
                    first.loginId,
                    first.name,
                    first.email,
                    second.accountId,
                    second.loginId,
                    second.name,
                    second.email,
                ),
            )
            cursor.execute(
                """
                UPDATE travel_friend_requests
                SET status = 'accepted', responded_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (request_id,),
            )
            cursor.execute(
                "SELECT created_at FROM travel_friendships WHERE id = %s",
                (friendship_id,),
            )
            friendship = cursor.fetchone()
            return FriendshipView(
                friend=requester, createdAt=_iso(friendship["created_at"])
            )


@router.post("/requests/{request_id}/reject", response_model=FriendRequestView)
async def reject_friend_request(
    request_id: str, user: dict = Depends(get_current_user)
):
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM travel_friend_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            row = cursor.fetchone()
            _require_pending_addressee(row, user["account_id"])
            cursor.execute(
                """
                UPDATE travel_friend_requests
                SET status = 'rejected', responded_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (request_id,),
            )
            cursor.execute(
                "SELECT * FROM travel_friend_requests WHERE id = %s", (request_id,)
            )
            return _map_request(cursor.fetchone())


def _require_pending_addressee(row: dict | None, account_id: str) -> None:
    if not row:
        raise HTTPException(status_code=404, detail="Friend request not found")
    if row["addressee_account_id"] != account_id:
        raise HTTPException(status_code=403, detail="Friend request access denied")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail="Friend request is not pending")


@router.get("", response_model=list[FriendshipView])
async def list_friends(user: dict = Depends(get_current_user)):
    account_id = user["account_id"]
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM travel_friendships
                WHERE account_a_id = %s OR account_b_id = %s
                ORDER BY created_at DESC
                """,
                (account_id, account_id),
            )
            results = []
            for row in cursor.fetchall():
                prefix = (
                    "account_b" if row["account_a_id"] == account_id else "account_a"
                )
                results.append(
                    FriendshipView(
                        friend=_friendship_account_from_row(row, prefix),
                        createdAt=_iso(row["created_at"]),
                    )
                )
            return results


@router.delete("/{account_id}")
async def remove_friend(account_id: str, user: dict = Depends(get_current_user)):
    account_a_id, account_b_id = sorted((user["account_id"], account_id))
    with get_db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM travel_friendships
                WHERE account_a_id = %s AND account_b_id = %s
                """,
                (account_a_id, account_b_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Friendship not found")
    return {"message": "Friend removed"}
