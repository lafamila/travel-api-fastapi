# travel-api-fastapi

FastAPI backend for travel places/courses — stores user-tagged map markers, generates AI-friendly course prompt JSON, integrates with S3 (media) and Playwright (URL preview/scraping).

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

- **Lifecycle**: DEPLOY
- **Status**: active
- **Port**: 8010
- **Auth**: NO_AUTH (beer-house BFF 뒤 내부 API — auth-api-nest 운영 후 통합 예정)

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — 현재 인증 없음. 워크스페이스 루트 `.idea/TRAVEL_IDEA.md` 에 "추후 AUTHENTICATION 통합 시점에 구현 예정" 으로 명시됨. `auth-api-nest` 가 운영에 올라간 뒤 그쪽으로 연동.
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
├── __main__.py                 # FastAPI app 생성(lifespan: init_db + ensure_bucket + Playwright browser), 라우터 등록
├── connectors/__init__.py      # MySQL config, get_db_connection(), init_db() DDL
├── routers/
│   ├── places.py               # /api/places — CRUD, 리뷰, resolve-google-link
│   ├── courses.py              # /api/courses — CRUD, export/import
│   └── uploads.py              # /api/uploads — S3 미디어 업로드
├── schemas.py                  # Pydantic request/response 모델
├── services/
│   ├── storage.py              # S3/LocalStack 클라이언트, ensure_bucket, presign
│   ├── google_maps_links.py    # Google Maps 링크 해석 (Playwright)
│   └── course_contract.py      # AI-friendly course prompt JSON 계약
└── utils.py
tests/                          # unittest — course_contract, storage
Dockerfile                      # Playwright Chromium 포함, HEALTHCHECK 포함
requirements.txt
```

## ENDPOINTS

`src/routers/` 의 라우트 정의가 canonical 이다 (총 14개, 전부 `/api/*`):

| Router | Routes |
|--------|--------|
| `places.py` (`/api/places`) | `GET/POST /api/places`, `GET/PUT/DELETE /api/places/{place_id}`, `POST /api/places/resolve-google-link`, `POST /api/places/{place_id}/reviews` |
| `courses.py` (`/api/courses`) | `GET/POST /api/courses`, `GET/DELETE /api/courses/{course_id}`, `POST /api/courses/export`, `POST /api/courses/import` |
| `uploads.py` (`/api/uploads`) | `POST /api/uploads` |

전용 health 엔드포인트는 없다 (Docker HEALTHCHECK 는 `GET /docs` 사용).

## DATABASE

`src/connectors/__init__.py` `init_db()` 가 canonical — `travel_places`, `travel_place_reviews`, `travel_courses`, `travel_course_stops` 4개 테이블 (기본 DB 이름 `travelnote`).

## DEPENDENCIES (requirements.txt)

- `fastapi`, `uvicorn`, `pymysql`, `pydantic`, `python-dotenv` — todo-api-fastapi 와 같은 스택
- `cryptography`
- `boto3` — S3 (운영) / LocalStack (로컬)
- `python-multipart` — 파일 업로드
- `playwright` — URL 미리보기 / 외부 페이지 스크래핑

## COMMANDS

```bash
pip install -r requirements.txt
uvicorn src.__main__:app --port 8010
docker build -t travel-api-fastapi .
# deploy/run example
docker run --rm --env-file .env -p 8010:8010 travel-api-fastapi
```

## ENVIRONMENT

- `.env.example` 를 기준으로 env shape 를 맞춘다.
- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` — shared root MySQL/MariaDB 또는 외부 운영 DB. 로컬 기본 DB 이름은 `travelnote`.
- `S3_ENDPOINT_URL`, `S3_PUBLIC_BASE_URL`, `S3_BUCKET_NAME`, `S3_REGION`
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — shared root LocalStack 또는 실제 S3
- `LOCALSTACK_STATE_URL`, `S3_SAVE_STATE_AFTER_UPLOAD`, `S3_STATE_SAVE_STRICT` — LocalStack state save 를 쓸 때만 설정
- 로컬에서는 shared root infra 의 MySQL/MariaDB + LocalStack 을 재사용하고, 독립 배포 시에는 해당 값을 운영 DB/S3 endpoint 로 교체한다.

## NOTES

- todo-api-fastapi 와 같은 스택/패턴. 같은 종류 third-party infra 는 앱별 전용 컨테이너 대신 shared root infra 또는 외부 managed infra 를 우선 사용한다.
- beer-house 쪽 `TRAVEL_API_BASE_URL` 같은 cross-repo 호출 계약은 유지되며, 이 레포 변경으로 env key/port contract 는 바뀌지 않는다.
- `.idea/` 에 repo execution plan 이 들어올 수 있다. 새 계획도 `/idea-new` 후 `/idea-build` 로 이쪽 `.idea/` 에 누적된다.
- 도메인 모델/엔드포인트가 잡히면 이 가이드에 ENDPOINTS 표를 추가하세요 (todo-api-fastapi/CLAUDE.md 참조).
