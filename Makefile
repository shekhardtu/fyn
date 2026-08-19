VENV := ../.venv/bin

.PHONY: dev backend frontend db migrate prod deploy deploy-status deploy-logs deploy-setup

## Start Postgres, run migrations, then run backend + frontend dev servers.
dev: db migrate
	$(MAKE) -j2 backend frontend

backend:
	cd backend && $(VENV)/uvicorn app.main:app --reload

frontend:
	cd frontend && yarn dev

# Prefer the standalone expen-postgres container (Colima setups without the
# compose plugin); otherwise fall back to compose.
db:
	@if docker start expen-postgres >/dev/null 2>&1; then \
		until docker exec expen-postgres pg_isready >/dev/null 2>&1; do sleep 0.5; done; \
	else \
		docker compose up -d --wait postgres; \
	fi

migrate:
	cd backend && $(VENV)/alembic upgrade head

## Full production-style stack in containers.
prod:
	docker compose --env-file backend/.env up --build

## Deploy origin/main to the shared Hetzner box. See .claude/skills/deploy.
## Scope with ARGS:  make deploy ARGS=api   |   make deploy ARGS="web --no-cache"
deploy:
	./infra/deploy/deploy.sh $(ARGS)

deploy-status:
	./infra/deploy/status.sh

## make deploy-logs ARGS="backend 200"
deploy-logs:
	./infra/deploy/logs.sh $(ARGS)

deploy-setup:
	./infra/deploy/setup-server.sh
