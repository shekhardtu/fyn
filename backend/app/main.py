from __future__ import annotations

import asyncio
from contextlib import suppress
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .api_auth import router as auth_router
from .config import get_settings, require_production_auth_config
from .database import Base, SessionLocal, engine
from .event_time import now_utc
from .seed import seed_system_taxonomy
from .services.agui import (
    DurableEventPublisher,
    agent_recovery_backlog_exists,
    claim_agent_recovery_work,
    execute_run as execute_agui_run,
    renew_agent_recovery_claim,
)
from .services.analysis_harness import delete_obsolete_analysis_templates
from .services.analysis_seeds import seed_analysis_templates
from .services.manifest import ensure_native_manifest
from .operations import operation_catalog
from watchfiles import awatch


_agent_recovery_task: asyncio.Task | None = None
_operation_watch_task: asyncio.Task | None = None


async def _watch_operations() -> None:
    manager = operation_catalog()
    paths = [manager.core_root]
    if manager.managed_root:
        # A configured shared directory is an operational contract. Create is
        # deliberately left to deployment; a missing mount should be visible.
        paths.append(manager.managed_root)
    existing = [path for path in paths if Path(path).exists()]
    if not existing:
        return
    async for _changes in awatch(
        *existing,
        debounce=settings.operation_watch_debounce_ms,
    ):
        manager.load()


async def _drain_agent_recovery_backlog(created_before: datetime) -> None:
    """Drain old runs with fixed concurrency and leased database claims.

    There are never more worker tasks or simultaneous suggestion model calls
    than the configured concurrency, regardless of backlog size.
    """
    async def worker() -> None:
        while True:
            work = await asyncio.to_thread(
                claim_agent_recovery_work,
                SessionLocal,
                created_before=created_before,
                claim_ttl_seconds=settings.agent_recovery_claim_ttl_seconds,
                max_postprocess_attempts=settings.agent_recovery_max_postprocess_attempts,
            )
            if work is None:
                remaining = await asyncio.to_thread(
                    agent_recovery_backlog_exists,
                    SessionLocal,
                    created_before=created_before,
                )
                if not remaining:
                    return
                await asyncio.sleep(settings.agent_recovery_idle_poll_seconds)
                continue
            if work.executable:
                run_id = work.run_id
                user_id = work.user_id
                # ``executable`` guarantees both; this branch narrows their
                # optional annotations for static checking.
                if run_id is None or user_id is None:
                    continue
                publisher = DurableEventPublisher(
                    SessionLocal,
                    run_id,
                    user_id,
                    work.last_sequence,
                    lambda _seq, _event: None,
                )
                async def renew_lease() -> None:
                    interval = max(settings.agent_recovery_claim_ttl_seconds // 3, 1)
                    while True:
                        await asyncio.sleep(interval)
                        alive = await asyncio.to_thread(
                            renew_agent_recovery_claim,
                            SessionLocal,
                            run_id,
                            user_id,
                        )
                        if not alive:
                            return

                heartbeat = asyncio.create_task(renew_lease())
                try:
                    await asyncio.to_thread(
                        execute_agui_run,
                        SessionLocal,
                        run_id,
                        user_id,
                        publisher,
                    )
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
            if settings.agent_recovery_min_interval_ms:
                await asyncio.sleep(settings.agent_recovery_min_interval_ms / 1000)

    await asyncio.gather(*(
        worker()
        for _ in range(settings.agent_recovery_max_concurrency)
    ))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _agent_recovery_task, _operation_watch_task
    # Development-only authentication settings must never reach a real database,
    # so this is checked before the first request rather than at the first
    # sign-in attempt.
    require_production_auth_config(settings)
    # Fail startup if the protected catalog is incomplete. Definitions remain
    # memory-only and successful watch reloads atomically replace this snapshot.
    operation_catalog().load(initial=True)
    if settings.operations_watch_enabled:
        _operation_watch_task = asyncio.create_task(_watch_operations())
    # Alembic exclusively owns PostgreSQL schema evolution. SQLite remains a
    # disposable local/test convenience and can safely bootstrap itself.
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
    # Reference taxonomy is part of an operable empty installation, not demo
    # data. It is system-scoped and creates no account, so the first real user
    # sees useful categories without inheriting a placeholder identity.
    with SessionLocal() as db:
        seed_system_taxonomy(db)
        # Curated analysis seed templates follow the same pattern: reference
        # data for an operable empty installation. A semantic-registry bump
        # flushes stale templates first so the reseed lands in one startup.
        delete_obsolete_analysis_templates(db)
        seed_analysis_templates(db)
        # The native ledger's manifest is reference data with the same startup
        # contract: content-addressed, so an unchanged registry is a no-op and
        # a registry bump posts the next manifest version.
        ensure_native_manifest(db)
    # The cutoff excludes runs accepted by this process: their request handlers
    # already own execution, so recovery can never race them for the same row.
    _agent_recovery_task = asyncio.create_task(_drain_agent_recovery_backlog(now_utc()))
    try:
        yield
    finally:
        if _agent_recovery_task:
            _agent_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await _agent_recovery_task
            _agent_recovery_task = None
        if _operation_watch_task:
            _operation_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await _operation_watch_task
            _operation_watch_task = None


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"],
)
app.include_router(auth_router)
app.include_router(router)
