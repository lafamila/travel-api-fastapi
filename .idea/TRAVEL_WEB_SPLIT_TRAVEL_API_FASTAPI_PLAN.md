---
status: PREPARED
summary: "travel-api를 auth-api OIDC session 기반 계정별 API로 전환하고 친구 공개 장소 공유를 구현한다."
---

# TRAVEL WEB SPLIT — travel-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TRAVEL_WEB_SPLIT_PLAN.md`

## Repo Responsibility
`travel-api-fastapi`는 travel 데이터, auth session, 권한 검사, 친구 관계, 공개 장소 공유 규칙을 소유한다. `travel-web-next`는 이 API만 호출하고 beer-house BFF는 거치지 않는다.

## Inputs / Dependencies
- Auth provider: `auth-api-nest`
- Auth serviceKey: `travel`
- OIDC client id: `travel-api`
- OIDC redirect URI local: `http://localhost:8010/api/session/oidc/callback`
- OIDC redirect URI prod: `https://map.lafamila.xyz/api/session/oidc/callback`
- Service credential scopes: `account.search`, `permission.read`
- Legacy owner login id: `lafamila`
- Existing DB: `travelnote`, tables `travel_places`, `travel_place_reviews`, `travel_courses`, `travel_course_stops`

## Work Items
1. Auth config를 추가한다.
   - `AUTH_ISSUER_URL`, `AUTH_API_BASE_URL`, `AUTH_JWKS_URL`, `AUTH_AUDIENCE`, `AUTH_JWKS_CACHE_SECONDS`
   - `TRAVEL_ALLOWED_ORIGINS`, `TRAVEL_WEB_BASE_URL`
   - `TRAVEL_OIDC_CLIENT_ID`, `TRAVEL_OIDC_CLIENT_SECRET`, `TRAVEL_OIDC_REDIRECT_URI`
   - `TRAVEL_SESSION_COOKIE_*`, `TRAVEL_SESSION_MAX_AGE_SECONDS`
   - `AUTH_SERVICE_KEY_ID`, `AUTH_SERVICE_SECRET`
   - `TRAVEL_LEGACY_OWNER_LOGIN_ID=lafamila`
2. OIDC session module을 구현한다.
   - `POST /api/session/oidc/start`
   - `GET /api/session/oidc/callback`
   - `GET /api/session/me`
   - `POST /api/session/logout`
   - `POST /api/session/service-application`
   - Authorization Code + PKCE + confidential client secret 사용
   - refresh token은 서버 메모리 세션에만 보관한다.
   - cookie는 HttpOnly로 설정한다.
3. JWT verifier/auth utility를 구현한다.
   - JWKS `kid` cache/refetch
   - issuer/audience 검증
   - namespaced service claim key가 `travel`인지 검증
   - `visitor`, `user`, `admin`, `superadmin` mapping
4. DB schema를 idempotent하게 확장한다.
   - `travel_places`: owner account id/login id/name/email, visibility
   - `travel_place_reviews`: author account id/login id/name/email
   - `travel_courses`: owner account id/login id/name/email
   - friend requests / friendships table 추가
   - 필요한 index와 unique constraint 추가
5. Legacy data migration을 구현한다.
   - `TRAVEL_LEGACY_OWNER_LOGIN_ID` 기본값 `lafamila`
   - auth internal account search에서 exact `loginId === "lafamila"` account를 찾는다.
   - owner가 비어 있는 기존 places/courses/reviews를 해당 account로 채운다.
   - 기존 places visibility는 `public`으로 채운다.
   - migration은 재실행해도 중복/변형이 없어야 한다.
6. Places API 권한 규칙을 적용한다.
   - list/detail은 본인 장소 + 친구의 public 장소 + superadmin 전체
   - create는 `user` 이상
   - update/delete는 owner 또는 superadmin
   - visibility create/update 지원, 기본값 public
7. Reviews API 권한 규칙을 적용한다.
   - owner는 본인 장소에 후기 작성 가능
   - 친구는 public 장소에 후기 작성 가능
   - 친구가 아닌 사용자는 작성 불가
   - author metadata를 response에 포함한다.
8. Courses API 권한 규칙을 적용한다.
   - courses는 owner 또는 superadmin만 조회/삭제 가능
   - course stops는 본인 장소 또는 친구 public 장소만 참조 가능
   - course 자체는 친구에게 공유하지 않는다.
9. Google Maps link crawling 권한을 `admin`/`superadmin`으로 제한한다.
10. Friend API를 추가한다.
    - user search
    - send friend request
    - incoming/outgoing request list
    - accept/reject friend request
    - friend list
    - remove friend
11. CORS를 env 기반 origin allowlist로 바꾼다.
12. tests를 추가/수정한다.
    - auth utility unit tests
    - DB migration idempotency tests where practical
    - visibility/friendship authorization tests
    - Google Maps crawling permission tests
13. README, CLAUDE.md, `.env.example`를 구현 결과에 맞춰 갱신한다.

## Acceptance Criteria
- `python -m unittest discover -s tests` passes.
- `uvicorn src.__main__:app --port 8010`로 local 실행 가능.
- `visitor` session은 protected places/courses write를 거부한다.
- `user`는 자기 장소/코스를 만들 수 있다.
- `admin`은 Google Maps link crawling endpoint를 사용할 수 있다.
- 기존 row가 `lafamila` owner와 public visibility로 이관된다.
- 친구 수락 후 친구 public place 조회와 review 작성이 가능하다.
- 친구가 아닌 account는 public place라도 조회/후기 작성이 불가하다.
- friend는 place 원본 update/delete가 불가하다.
- course는 owner 개인 데이터로 유지된다.

## Report Back To Orchestrator
- auth onboarding approval 후 발급받아야 하는 env secret 목록
- legacy owner account search 실패 또는 여러 exact match 같은 migration blocker
- travel-web-next가 맞춰야 하는 API response shape 변경
- 운영 reverse proxy에서 필요한 cookie/CORS 설정

## Decision Escalation
사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.
