# travel-api-fastapi

FastAPI backend for travel places/courses — stores user-tagged map markers, generates AI-friendly course prompt JSON, integrates with S3 (media) and Playwright (URL preview/scraping).

> 이 파일이 본 레포의 canonical 가이드입니다. `AGENTS.md` 는 codex 호환용 stub 입니다.

## 워크스페이스 대원칙 (canonical)

이 레포는 `../CLAUDE.md` 의 **DEVELOPMENT PRINCIPLES** 섹션을 따른다. 핵심 재진술:

1. **인증** — 현재 인증 없음. 워크스페이스 루트 `.idea/TRAVEL_IDEA.md` 에 "추후 AUTHENTICATION 통합 시점에 구현 예정" 으로 명시됨. `auth-api-nest` 가 운영에 올라간 뒤 그쪽으로 연동.
2. **기능 단위 커밋** — 한 기능이 계획-구현-검토를 통과하면 즉시 1개의 커밋. 여러 기능을 묶지 않는다.
3. **Agent co-author 제외** — Codex, Claude, OmX 등 agent/tool 저자를 `Co-authored-by` trailer 로 추가하지 않는다. 사용자가 명시적으로 요청한 경우만 예외.
4. **계획 → 구현 → 검토** — 계획 단계에서 검토 통과 기준(어떤 테스트/명령이 통과해야 "done"인지)을 명시한다.
5. **Docker 빌드 가능** — DEPLOY. 루트 `docker-compose.yml` 의 `travel-fastapi` 서비스 (포트 8010). mysql (healthy) + localstack (started) 의존. Dockerfile 유지 필수.
6. **Cross-repo 영향 보고** — 이 레포의 변경이 다른 repo, 공통 API 계약, auth claim/permission, env var, Docker/deploy 설정, 공통 문서에 영향을 준다고 판단되면 현재 orchestrator 에게 반드시 보고한다. 직접 보고할 수 없으면 워크스페이스 루트 `../.idea/` 에 `{REPO_NAME}_CROSS_REPO_IMPACT_{YYYYMMDD}.md` 형식의 handoff 문서를 남긴다.
7. **사용자 결정 필요사항 에스컬레이션** — 사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않고 작업을 중단한 뒤 현재 orchestrator 에게 전달하여 결정받고 진행한다. orchestrator 에 보고할 수 없으면 workspace root `../.idea/` 에 handoff 문서를 남긴다.

## Feature Workflow (대원칙 #3 의 이 레포 적용)

1. `.idea/` 또는 신규 계획 문서에서 기능을 선택
2. 계획서 작성 — 변경 파일, 인터페이스, **검토 통과 기준** 명시
3. 구현
4. 통과 기준 만족 여부 직접 실행/테스트 (`uvicorn src.__main__:app --port 8010` 또는 `docker compose up travel-fastapi`)
5. 통과 시 1개의 커밋으로 마무리

## STRUCTURE

```
src/                # FastAPI 엔트리 (todo-api-fastapi 와 같은 single-file + connectors 패턴)
tests/
Dockerfile
requirements.txt
```

> 모듈 분할이나 라우터 구조가 todo-api-fastapi 와 다르게 진화하면 이 섹션을 그때 갱신하세요.

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
# or
docker compose up travel-fastapi
```

## ENVIRONMENT

- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` — MySQL (`teddy-mysql` 컨테이너, DB 이름 `travelnote`)
- `S3_ENDPOINT_URL`, `S3_PUBLIC_BASE_URL`, `S3_BUCKET_NAME`, `S3_REGION`
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — LocalStack 또는 실제 S3
- 운영 S3 public base: `https://s3.lafamila.xyz` / 로컬: `http://localstack:4566`

## NOTES

- todo-api-fastapi 와 같은 스택/패턴. 같은 mysql 컨테이너 공유 (다른 DB 이름: `travelnote`).
- `.idea/` 는 현재 비어있음. 새 계획은 `/idea-new` 후 `/idea-build` 로 이쪽 `.idea/` 에 들어옴.
- 도메인 모델/엔드포인트가 잡히면 이 가이드에 ENDPOINTS 표를 추가하세요 (todo-api-fastapi/CLAUDE.md 참조).
