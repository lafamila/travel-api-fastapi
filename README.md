# travel-api-fastapi

FastAPI backend for travel places/courses — user-tagged map markers, AI-friendly course prompt JSON, S3 media uploads, Playwright 기반 Google Maps 링크 해석.

- Lifecycle: `DEPLOY` · Port: `8010` · Auth: `NO_AUTH` (beer-house BFF 뒤 내부 API)
- 상세 가이드(구조, 엔드포인트, DB, 원칙)는 `CLAUDE.md` 참조.

## Run (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # 값 채우기 (.env 는 커밋 금지)
uvicorn src.__main__:app --port 8010
```

## Test

```bash
python -m unittest discover -s tests
```

## Docker

```bash
docker build -t travel-api-fastapi .
docker run --rm --env-file .env -p 8010:8010 travel-api-fastapi
```

이미지에는 Playwright Chromium 과 `GET /docs` 기반 HEALTHCHECK 가 포함된다.

## Dependencies

- **MySQL/MariaDB** — `DB_*` env. 로컬은 shared root infra compose 재사용, 기본 DB 이름 `travelnote`. 스키마는 startup 시 `init_db()` 가 자동 생성.
- **S3 (LocalStack)** — `S3_*` / `AWS_*` env. 로컬은 shared root LocalStack(`http://localhost:4566`), 운영은 실제 S3 endpoint 로 교체. startup 시 `ensure_bucket()` 이 버킷을 보장한다.
- **Playwright Chromium** — URL 미리보기/Google Maps 링크 해석용. lifespan 에서 브라우저를 1회 기동해 공유한다.

env 키 전체 목록은 `.env.example` 이 canonical 이다.
