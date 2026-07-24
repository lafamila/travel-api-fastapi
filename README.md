# travel-api-fastapi

Account-scoped FastAPI backend for travel places and courses. It owns the
`auth-api-nest` OIDC session, friend relationships, friend-only public-place
sharing, S3 media uploads, and static Google/Kakao/Naver map-link resolution.

- Lifecycle: `DEPLOY`
- Port: `8010`
- Auth service key: `travel`
- Public production API: `https://map.lafamila.xyz/api/*`

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
brew install exiftool
cp .env.example .env
uvicorn src.__main__:app --port 8010
```

Run the resumable photo-import worker separately from the same checkout/image:

```bash
python -m src.import_worker
```

`exiftool` is mandatory for worker metadata extraction. `ffmpeg` is required
for bounded JPEG thumbnails, including first-frame video thumbnails, and
`heif-convert` is the HEIC fallback (macOS: `brew install libheif ffmpeg`). The
Docker image contains all three tools.

Map links are parsed without a browser by default. Kakao uses Open Graph
metadata, Naver uses its server-rendered place state, and Google uses the
resolved place URL. Set `TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK=true` and run
`playwright install chromium` only when Google address/opening-hours enrichment
is required.

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
- `admin`: `user` access plus Google/Kakao/Naver map-link crawling.
- `admin` and `superadmin`: all `/api/imports` photo-import operations.
- `superadmin`: unrestricted travel data management and crawling.
- Public places are visible only to accepted friends, not to arbitrary users.
- Courses remain private to their owner; stops may reference own places or an
  accepted friend's public places.

## Photo import playground API

Import batches and jobs are persisted in MariaDB; originals and browser-ready
HEIC previews are staged in S3. The API process only enqueues work. Exactly one
worker claims jobs atomically and holds a MariaDB advisory lock while it runs.
Local imports are disabled unless `TRAVEL_IMPORT_LOCAL_ROOT` and
`TRAVEL_IMPORT_OUTPUT_ROOT` are configured; requests use paths relative to the
allowlisted root and originals are copied, never moved.

All routes below require `admin` or `superadmin`:

- `GET/POST /api/imports`
- `POST /api/imports/{id}/files` (multipart `files`, individual files or ZIP)
- `POST /api/imports/{id}/process`, `GET/DELETE /api/imports/{id}`
- `GET /api/imports/{id}/manifest`
- `GET /api/imports/{id}/assets/{assetId}/preview`
- `GET /api/imports/{id}/assets/{assetId}/thumbnail`
- `PATCH /api/imports/{id}/assets/{assetId}`
- `POST /api/imports/{id}/assets/unassign`
- `POST /api/imports/{id}/clusters`
- `PATCH /api/imports/{id}/clusters/{clusterId}`
- `POST /api/imports/{id}/clusters/{clusterId}/reviews`
- `PATCH/DELETE /api/imports/{id}/reviews/{reviewId}`
- `POST /api/imports/{id}/clusters/{clusterId}/assets/assign`
- `POST /api/imports/{id}/clusters/merge`
- `POST /api/imports/{id}/clusters/split`
- `POST /api/imports/{id}/validate`
- `POST /api/imports/{id}/publish`

Batch detail and manifests expose cluster review drafts in the top-level
`reviewDrafts` array. A cluster may have multiple drafts, and each draft may
link multiple assets from that same cluster.

Session helpers are `POST /api/session/import-access-application` (requests the
`admin` permission with a Korean rationale), `GET` on the same path for status,
and `POST /api/session/refresh` to force OIDC token refresh. The existing
`POST /api/session/service-application` continues to request `user`.

Publishing is off by default. Development uses LocalStack with
`STORAGE_BACKEND=localstack`; production uses the pre-created private R2 bucket
with `STORAGE_BACKEND=r2`. Persistent rows store `travel_media` IDs and object
keys, while API responses resolve them to short-lived read URLs. Set
`TRAVEL_IMPORT_PUBLISH_ENABLED=true` only in production after final storage and
database configuration is verified. Incomplete
review drafts are validation warnings and are omitted from review publishing;
their photos remain attached to the place. The importer never generates rating
or review text.

## Test

```bash
python -m unittest discover -s tests
```

## Docker

The canonical Synology build, remove, run, and Nginx gateway commands are in
[`docs/synology-deployment.md`](docs/synology-deployment.md).

```bash
docker build -t travel-api-fastapi .
docker run --rm --env-file .env -p 8010:8010 travel-api-fastapi
docker run --rm --env-file .env travel-api-fastapi python -m src.import_worker
```

The image includes Playwright Chromium for the optional Google fallback and
uses `GET /docs` for its liveness check. MySQL/MariaDB and LocalStack/R2 remain
external dependencies configured through `.env.example`.
