VENV := ../.venv/bin

.PHONY: dev backend frontend db migrate prod

## Start Postgres, run migrations, then run backend + frontend dev servers.
dev: db migrate
	$(MAKE) -j2 backend frontend

backend:
	cd backend && $(VENV)/uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

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
