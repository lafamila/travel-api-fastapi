# travel-api-fastapi

FastAPI backend for travel places/courses — stores user-tagged map markers, generates AI-friendly course prompt JSON, integrates with S3 media and static Google/Kakao/Naver place-link parsing.

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

- **Lifecycle**: DEPLOY
- **Status**: active
- **Port**: 8010
- **Auth**: auth-api-nest OIDC session (`serviceKey=travel`)

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — 이 API가 confidential OIDC client와 HttpOnly cookie session을 소유한다. 브라우저에는 access/refresh token, client secret, service credential을 노출하지 않는다.
2. **기능 단위 커밋** — 한 기능이 계획-구현-검토를 통과하면 즉시 1개의 커밋. 여러 기능을 묶지 않는다.
3. **Agent co-author 제외** — Codex, Claude, OmX 등 agent/tool 저자를 `Co-authored-by` trailer 로 추가하지 않는다. 사용자가 명시적으로 요청한 경우만 예외.
4. **계획 → 구현 → 검토** — 계획 단계에서 검토 통과 기준(어떤 테스트/명령이 통과해야 "done"인지)을 명시한다.
5. **Docker 빌드 가능** — DEPLOY. 이 레포는 포트 `8010` 의 독립 배포 API 이며 Dockerfile 유지가 필수다. 앱 컨테이너는 root compose 에 묶지 않고, DB 와 S3 는 shared root infra 또는 외부 운영 infra 에 연결한다.
6. **Cross-repo 영향 보고** — 이 레포의 변경이 다른 repo, 공통 API 계약, auth claim/permission, env var, Docker/deploy 설정, 공통 문서에 영향을 준다고 판단되면 현재 orchestrator 에게 반드시 보고한다. 직접 보고할 수 없으면 워크스페이스 루트 `../.idea/` 에 `{REPO_NAME}_CROSS_REPO_IMPACT_{YYYYMMDD}.md` 형식의 handoff 문서를 남긴다.
7. **사용자 결정 필요사항 에스컬레이션** — 사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않고 작업을 중단한 뒤 현재 orchestrator 에게 전달하여 결정받고 진행한다. orchestrator 에 보고할 수 없으면 workspace root `../.idea/` 에 handoff 문서를 남긴다.

## Feature Workflow (대원칙 #3 의 이 레포 적용)

1. `.idea/` 또는 신규 계획 문서에서 기능을 선택
2. 계획서 작성 — 변경 파일, 인터페이스, **검토 통과 기준** 명시
3. 구현
4. 통과 기준 만족 여부 직접 실행/테스트 (`uvicorn src.__main__:app --port 8010`, 필요 시 `docker build -t travel-api-fastapi .`)
5. 통과 시 1개의 커밋으로 마무리

## STRUCTURE

routers/services/schemas 멀티모듈 구조다 (초기의 single-file 패턴에서 진화함):

```
src/
├── __main__.py                 # FastAPI app 생성(lifespan: init_db + ensure_bucket + optional Playwright), 라우터 등록
├── config.py                   # auth/session/CORS env 계약
├── token_verifier.py           # auth JWKS cache + issuer/audience/service claim 검증
├── auth_utils.py               # cookie/bearer current user, visitor/admin dependency
├── connectors/__init__.py      # MySQL DDL, idempotent ownership migration
├── routers/
│   ├── places.py               # /api/places — CRUD, soft delete/restore, 리뷰, resolve-map-link
│   ├── courses.py              # /api/courses — CRUD, export/import
│   ├── friends.py              # /api/friends — 검색/요청/수락/거절/삭제
│   ├── imports.py              # /api/imports — 사진 import batch/draft/publish API
│   ├── session.py              # /api/session — OIDC/session/access application
│   └── uploads.py              # /api/uploads — S3 미디어 업로드
├── schemas.py                  # Pydantic request/response 모델
├── services/
│   ├── storage.py              # LocalStack/R2 클라이언트, provider 분기, presign
│   ├── media.py                # media metadata, attach/resolve/orphan cleanup
│   ├── place_links.py          # Google/Kakao/Naver 정적 링크 해석
│   ├── google_maps_links.py    # Google URL 파싱 + optional Playwright 보강
│   ├── authorization.py        # 장소/코스 owner/friend/superadmin 규칙
│   ├── session_auth.py         # PKCE OIDC, in-memory refresh/session, auth internal API
│   ├── import_contract.py      # ZIP/local safety, EXIF normalization, clustering, manifest
│   ├── import_processor.py     # ExifTool/geocode/organize worker pipeline
│   ├── import_repository.py    # import batch/job persistence and atomic claims
│   └── course_contract.py      # AI-friendly course prompt JSON 계약
├── import_worker.py            # `python -m src.import_worker` 별도 worker entrypoint
└── utils.py
tests/                          # unittest — course_contract, storage
Dockerfile                      # Playwright Chromium 포함, HEALTHCHECK 포함
requirements.txt
```

## ENDPOINTS

`src/routers/` 의 라우트 정의가 canonical 이다 (전부 `/api/*`):

| Router | Routes |
|--------|--------|
| `session.py` (`/api/session`) | `POST /oidc/start`, `GET /oidc/callback`, `GET /me`, `POST /logout`, `POST /service-application` |
| `places.py` (`/api/places`) | `GET/POST /api/places`, `GET /api/places/deleted`, `GET/PUT/DELETE /api/places/{place_id}`, `POST /api/places/{place_id}/restore`, admin-only `POST /api/places/resolve-map-link`, compatibility `POST /api/places/resolve-google-link`, `POST /api/places/{place_id}/reviews` |
| `courses.py` (`/api/courses`) | `GET/POST /api/courses`, `GET/DELETE /api/courses/{course_id}`, `POST /api/courses/export`, `POST /api/courses/import` |
| `friends.py` (`/api/friends`) | `GET /search`, `POST /requests`, `GET /requests/incoming`, `GET /requests/outgoing`, `POST /requests/{id}/accept`, `POST /requests/{id}/reject`, `GET /api/friends`, `DELETE /api/friends/{account_id}` |
| `uploads.py` (`/api/uploads`) | `POST /api/uploads` |
| `imports.py` (`/api/imports`) | admin-only batch CRUD, files/process, asset preview/draft, cluster merge/split/draft, manifest/validate/publish |

전용 health 엔드포인트는 없다 (Docker HEALTHCHECK 는 `GET /docs` 사용).

## DATABASE

`src/connectors/__init__.py` `init_db()` 가 canonical — 기존 travel 테이블과 함께 `travel_import_batches`, `travel_import_assets`, `travel_import_clusters`, `travel_import_review_drafts`, `travel_import_geocode_cache`, `travel_import_jobs`를 idempotent하게 생성한다. `travel_places`는 `expectation` (`ordinary`/`confident`, 기본 `ordinary`)과 `deleted_at` 기반 soft delete를 사용한다. owner가 없는 legacy row는 auth internal account search의 정확한 `lafamila` account로 이관되며, 정확한 계정이 없으면 startup이 명확히 실패한다.

## DEPENDENCIES (requirements.txt)

- `fastapi`, `uvicorn`, `pymysql`, `pydantic`, `python-dotenv` — todo-api-fastapi 와 같은 스택
- `cryptography`
- `python-jose` — RS256/JWKS access token 검증
- `boto3` — S3 (운영) / LocalStack (로컬)
- `python-multipart` — 파일 업로드
- `playwright` — `TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK=true`일 때 Google 보조 필드 보강

## COMMANDS

```bash
pip install -r requirements.txt
uvicorn src.__main__:app --port 8010
docker build -t travel-api-fastapi .
# deploy/run example
docker run --rm --env-file .env -p 8010:8010 travel-api-fastapi
docker run --rm --env-file .env travel-api-fastapi python -m src.import_worker
```

## ENVIRONMENT

- `.env.example` 를 기준으로 env shape 를 맞춘다.
- `AUTH_ISSUER_URL`, `AUTH_API_BASE_URL`, `AUTH_JWKS_URL`, `AUTH_AUDIENCE`, `AUTH_JWKS_CACHE_SECONDS`
- `TRAVEL_ALLOWED_ORIGINS`, `TRAVEL_WEB_BASE_URL`, `TRAVEL_OIDC_*`, `TRAVEL_SESSION_COOKIE_*`, `TRAVEL_SESSION_MAX_AGE_SECONDS`
- `AUTH_SERVICE_KEY_ID`, `AUTH_SERVICE_SECRET` — auth internal `account.search`, `permission.read` credential. legacy migration과 친구 검색, 신청 상태 확인에 필요.
- `TRAVEL_LEGACY_OWNER_LOGIN_ID` — legacy row owner exact-match login id (기본 `lafamila`).
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` — shared root MySQL/MariaDB 또는 외부 운영 DB. 로컬 기본 DB 이름은 `travelnote`.
- `STORAGE_BACKEND` — `localstack`(dev) 또는 `r2`(prod). provider 동작을 명시적으로 분리한다.
- `S3_ENDPOINT_URL`, `S3_BUCKET_NAME`, `S3_REGION`, `S3_AUTO_CREATE_BUCKET`, `S3_SIGNED_URL_TTL_SECONDS`
- `S3_PUBLIC_BASE_URL` — LocalStack 브라우저 접근 URL에만 사용. R2에서는 비워두고 presigned URL을 사용한다.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — shared root LocalStack 또는 실제 S3
- `LOCALSTACK_STATE_URL`, `S3_SAVE_STATE_AFTER_UPLOAD`, `S3_STATE_SAVE_STRICT` — LocalStack state save 를 쓸 때만 설정
- `TRAVEL_MEDIA_TEMPORARY_TTL_HOURS` — 장소/후기에 연결되지 않은 임시 업로드 정리 기준(기본 24시간)
- `TRAVEL_IMPORT_LOCAL_ROOT`, `TRAVEL_IMPORT_OUTPUT_ROOT` — 둘 다 설정할 때만 상대경로 local import 활성화
- `TRAVEL_IMPORT_MAX_UPLOAD_BYTES`, `TRAVEL_IMPORT_MAX_ZIP_FILES`, `TRAVEL_IMPORT_MAX_ZIP_EXPANDED_BYTES` — upload/ZIP 안전 제한
- `TRAVEL_IMPORT_NOMINATIM_BASE_URL`, `TRAVEL_IMPORT_NOMINATIM_USER_AGENT` — reverse geocode 계약
- `TRAVEL_IMPORT_PUBLISH_ENABLED` — 운영 publish gate (기본 false)
- 로컬에서는 shared root infra 의 MySQL/MariaDB + LocalStack 을 재사용하고, 독립 배포 시에는 해당 값을 운영 DB/S3 endpoint 로 교체한다.

## NOTES

- todo-api-fastapi 의 OIDC session/JWKS 패턴을 travel `serviceKey`와 route에 맞춰 재사용한다. 같은 종류 third-party infra 는 앱별 전용 컨테이너 대신 shared root infra 또는 외부 managed infra 를 우선 사용한다.
- dev storage 는 LocalStack(`STORAGE_BACKEND=localstack`, bucket auto-create), prod storage 는 private Cloudflare R2(`STORAGE_BACKEND=r2`, bucket pre-created, presigned read URL)로 구분한다. DB에는 만료 URL이 아니라 `travel_media`의 object key/media id를 저장한다.
- travel web은 이 API를 same-origin `/api` 또는 local `http://localhost:8010/api`로 직접 호출하고 cookie 요청에 credentials를 포함해야 한다. beer-house BFF proxy 계약은 더 이상 사용하지 않는다.
- `.idea/` 에 repo execution plan 이 들어올 수 있다. 새 계획도 `/idea-new` 후 `/idea-build` 로 이쪽 `.idea/` 에 누적된다.
- 도메인 모델/엔드포인트가 잡히면 이 가이드에 ENDPOINTS 표를 추가하세요 (todo-api-fastapi/CLAUDE.md 참조).
