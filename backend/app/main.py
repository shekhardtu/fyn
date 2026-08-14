from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .api_auth import router as auth_router
from .config import get_settings, require_production_auth_config
from .database import Base, SessionLocal, engine
from .seed import seed_system_taxonomy
from .services.agui import DurableEventPublisher, execute_run as execute_agui_run, recover_agent_runs


_recovered_agui_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Development-only authentication settings must never reach a real database,
    # so this is checked before the first request rather than at the first
    # sign-in attempt.
    require_production_auth_config(settings)
    # Alembic exclusively owns PostgreSQL schema evolution. SQLite remains a
    # disposable local/test convenience and can safely bootstrap itself.
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    # Reference taxonomy is part of an operable empty installation, not demo
    # data. It is system-scoped and creates no account, so the first real user
    # sees useful categories without inheriting a placeholder identity.
    with SessionLocal() as db:
        seed_system_taxonomy(db)
    for run_id, user_id, last_sequence in recover_agent_runs(SessionLocal):
        publisher = DurableEventPublisher(SessionLocal, run_id, user_id, last_sequence, lambda _seq, _event: None)
        task = asyncio.create_task(asyncio.to_thread(execute_agui_run, SessionLocal, run_id, user_id, publisher))
        _recovered_agui_tasks.add(task)
        task.add_done_callback(_recovered_agui_tasks.discard)
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # `X-Client` is how a caller declares it cannot hold a cookie and needs its
    # session token in the response body instead. Native builds never preflight,
    # so this entry exists for the Expo web target during development.
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key", "Last-Event-ID", "X-Client"],
)
app.include_router(auth_router)
app.include_router(router)
