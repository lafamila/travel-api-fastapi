# travel-api-fastapi

Account-scoped FastAPI backend for travel places and courses. It owns the
`auth-api-nest` OIDC session, friend relationships, friend-only public-place
sharing, S3 media uploads, and Playwright-based Google Maps link resolution.

- Lifecycle: `DEPLOY`
- Port: `8010`
- Auth service key: `travel`
- Public production API: `https://map.lafamila.xyz/api/*`

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
cp .env.example .env
uvicorn src.__main__:app --port 8010
```

The auth service must have the `travel` confidential OIDC client and service
credential described in `CLAUDE.md`. The browser stores only the opaque
`teddy_travel_session` HttpOnly cookie; access tokens, refresh tokens, the OIDC
client secret, and service credentials remain in this API process.

At startup the schema is extended idempotently. If legacy places, reviews, or
courses have no account metadata, the API searches auth for the exact
`TRAVEL_LEGACY_OWNER_LOGIN_ID` (default `lafamila`) and migrates those rows. A
missing or non-unique exact account match fails startup instead of assigning
legacy data to the wrong owner.

## Authorization model

- `visitor`: session and access-application endpoints only.
- `user`: own places/courses; friends' public places and reviews.
- `admin`: `user` access plus Google Maps link crawling.
- `superadmin`: unrestricted travel data management and crawling.
- Public places are visible only to accepted friends, not to arbitrary users.
- Courses remain private to their owner; stops may reference own places or an
  accepted friend's public places.

## Test

```bash
python -m unittest discover -s tests
```

## Docker

```bash
docker build -t travel-api-fastapi .
docker run --rm --env-file .env -p 8010:8010 travel-api-fastapi
```

The image includes Playwright Chromium and uses `GET /docs` for its liveness
check. MySQL/MariaDB and S3/LocalStack remain external dependencies configured
through `.env.example`.
