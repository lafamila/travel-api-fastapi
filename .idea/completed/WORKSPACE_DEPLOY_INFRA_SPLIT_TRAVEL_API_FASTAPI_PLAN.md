---
status: COMPLETED
completed_at: 2026-06-16
completion_reason: "Implemented infra-only root deployment model and repo deployment documentation."
summary: "travel-api-fastapi 를 root compose 앱 서비스가 아닌 독립 배포 API 로 문서화한다."
---

# WORKSPACE DEPLOY INFRA SPLIT — travel-api-fastapi execution plan

Canonical orchestration plan:

`../../.idea/WORKSPACE_DEPLOY_INFRA_SPLIT_PLAN.md`

## Repo Responsibility
`travel-api-fastapi` 는 travel places/courses API 이며 S3/LocalStack, MySQL/MariaDB 를 사용하는 독립 API 서비스다. root compose 의 `travel-fastapi` 앱 서비스 전제를 제거한다.

## Inputs / Dependencies
- DB 는 root infra MySQL/MariaDB 또는 운영 DB 를 사용한다.
- S3 는 local/dev 에서 LocalStack, 운영에서 실제 endpoint 를 사용할 수 있다.
- beer-house API 가 travel API 를 호출할 수 있으므로 endpoint/env contract 를 보고해야 한다.

## Work Items
1. `CLAUDE.md` 의 "루트 docker-compose travel-fastapi 서비스" 표현을 독립 배포 표현으로 바꾼다.
2. local run 과 Docker build/deploy check 를 분리한다.
3. `.env.example` 이 없다면 생성 여부를 검토하고, 있으면 DB/S3 env shape 를 확인한다.
4. README 에 root infra 사용과 운영 S3/DB 사용의 차이를 문서화한다.
5. `ted-yee-beer-house-api-nest` 가 사용하는 `TRAVEL_API_BASE_URL` contract 변경 여부를 보고한다.

## Acceptance Criteria
- root compose 앱 서비스 전제가 제거된다.
- DB/S3 env shape 가 문서화되어 있다.
- beer-house API 가 travel API URL 을 env 로 바꿀 수 있다는 계약이 유지된다.

## Report Back To Orchestrator
- `.env.example` 생성/수정 필요.
- beer-house API env/contract 변경 필요.
- Playwright/S3 배포 dependency 관련 후속 결정.

## Decision Escalation
사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.

