"""Failure-isolated optional enrichment for completed agent answers.

This module is deliberately outside the AG-UI executor. The executor performs
one cheap outbox insert in the same commit as its restart-safe answer
checkpoint; a separate bounded worker owns every model call, retry, and
failure. Removing this module therefore removes suggestions, not answers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..domain import AgentEnrichmentStatus, ExecutionStatus
from ..event_time import local_date, now_utc
from ..models import AIAction, AgentEnrichment, AgentRun, Conversation, Message, User
from ..schemas import AgentEnrichmentOut, AgentResponse, Widget, WidgetType
from .agent_run_metrics import (
    agent_metric_snapshot,
    begin_agent_metric_collection,
    end_agent_metric_collection,
)
from .agents import suggest_related_questions
from .runtime_tools import capability_notes


RELATED_QUESTIONS_KIND = "related_questions"


@dataclass(frozen=True)
class AgentEnrichmentWork:
    enrichment_id: UUID | None
    user_id: UUID | None = None

    @property
    def executable(self) -> bool:
        return self.enrichment_id is not None and self.user_id is not None


def _owned_enrichment(db: Session, enrichment_id: UUID, user_id: UUID) -> AgentEnrichment | None:
    return db.scalar(
        select(AgentEnrichment).where(
            AgentEnrichment.id == enrichment_id,
            AgentEnrichment.user_id == user_id,
        )
    )


def enqueue_related_questions(
    db: Session,
    run: AgentRun,
    response: AgentResponse,
) -> None:
    """Install optional work without adding a query or commit to the run path."""
    if not response.message.strip() or response.pending_action is not None:
        return
    widget_id = f"related-questions-{response.message_id}"
    if any(
        widget.type == WidgetType.RELATED_QUESTIONS or widget.id == widget_id
        for widget in response.widgets
    ):
        return
    db.add(AgentEnrichment(
        user_id=run.user_id,
        conversation_id=run.conversation_id,
        run_id=run.id,
        message_id=response.message_id,
        kind=RELATED_QUESTIONS_KIND,
        status=AgentEnrichmentStatus.PENDING.value,
        attempts=0,
        available_at=now_utc(),
        metrics={},
    ))


def claim_agent_enrichment_work(
    session_factory: sessionmaker[Session],
    *,
    claim_ttl_seconds: int,
    max_attempts: int,
) -> AgentEnrichmentWork | None:
    """Lease one eligible enrichment without materializing the queue."""
    now = now_utc()
    lease_before = now - timedelta(seconds=claim_ttl_seconds)
    with session_factory() as db:
        item = db.scalar(
            select(AgentEnrichment)
            .where(
                or_(
                    (
                        (AgentEnrichment.status == AgentEnrichmentStatus.PENDING.value)
                        & (AgentEnrichment.available_at <= now)
                    ),
                    (
                        (AgentEnrichment.status == AgentEnrichmentStatus.RUNNING.value)
                        & or_(
                            AgentEnrichment.claimed_at.is_(None),
                            AgentEnrichment.claimed_at <= lease_before,
                        )
                    ),
                )
            )
            .order_by(AgentEnrichment.available_at, AgentEnrichment.created_at, AgentEnrichment.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is None:
            return None
        if item.attempts >= max_attempts:
            item.status = AgentEnrichmentStatus.FAILED.value
            item.finished_at = now
            item.claimed_at = None
            item.error_code = item.error_code or "attempt_limit"
            db.commit()
            return AgentEnrichmentWork(None, None)
        item.status = AgentEnrichmentStatus.RUNNING.value
        item.attempts += 1
        item.claimed_at = now
        item.error_code = None
        db.commit()
        return AgentEnrichmentWork(item.id, item.user_id)


def _stored_related_widget(message: Message) -> Widget | None:
    widget_id = f"related-questions-{message.id}"
    raw = next(
        (
            item
            for item in (message.widgets or [])
            if item.get("type") == WidgetType.RELATED_QUESTIONS.value or item.get("id") == widget_id
        ),
        None,
    )
    return Widget.model_validate(raw) if raw is not None else None


def _failed_turn_questions(user: User) -> list[str]:
    """Guaranteed-answerable recovery prompts without another model call."""
    today = local_date(now_utc(), user.timezone)
    current_month = today.strftime("%B %Y")
    previous_month = (today.replace(day=1) - timedelta(days=1)).strftime("%B %Y")
    return [
        f"Which {current_month} categories cost the most?",
        f"Which {current_month} expenses were discretionary?",
        f"How did {current_month} spending compare with {previous_month}?",
    ]


def _suggestion_context(
    db: Session,
    run: AgentRun,
    user: User,
    conversation: Conversation,
    message: Message,
) -> tuple[str, str, list[dict[str, str]], Any, str]:
    """Copy the bounded context needed by the provider, then release the DB."""
    if run.task_status == "failed":
        raise ValueError("Failed turns use deterministic recovery questions")
    rows = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(8)
        )
    )
    recent_turns = [
        {"role": row.role, "content": row.content}
        for row in reversed(rows)
        if row.content.strip()
    ]
    return (
        str(run.input_payload.get("text") or ""),
        message.content,
        recent_turns,
        local_date(now_utc(), user.timezone),
        user.timezone,
    )


def _complete(
    db: Session,
    item: AgentEnrichment,
    *,
    status: AgentEnrichmentStatus,
    metrics: dict[str, Any],
    error_code: str | None = None,
) -> None:
    item.status = status.value
    item.claimed_at = None
    item.finished_at = now_utc()
    item.error_code = error_code
    item.metrics = metrics
    db.commit()


def _record_failure(
    session_factory: sessionmaker[Session],
    enrichment_id: UUID,
    user_id: UUID,
    error: Exception,
    *,
    max_attempts: int,
    retry_seconds: int,
    metrics: dict[str, Any],
) -> None:
    """Persist diagnostics best-effort; never let reporting kill the worker."""
    try:
        with session_factory() as db:
            item = _owned_enrichment(db, enrichment_id, user_id)
            if item is None:
                return
            code = type(error).__name__[:80]
            terminal = item.attempts >= max_attempts
            item.status = (
                AgentEnrichmentStatus.FAILED.value
                if terminal
                else AgentEnrichmentStatus.PENDING.value
            )
            item.claimed_at = None
            item.available_at = now_utc() + timedelta(seconds=retry_seconds)
            item.finished_at = now_utc() if terminal else None
            item.error_code = code
            item.metrics = metrics
            db.add(AIAction(
                user_id=item.user_id,
                conversation_id=item.conversation_id,
                action_type="suggester",
                payload_redacted={"errorType": code, "message": str(error)[:300]},
                status=ExecutionStatus.FAILED,
            ))
            db.commit()
    except Exception:
        return


def process_agent_enrichment(
    session_factory: sessionmaker[Session],
    enrichment_id: UUID,
    user_id: UUID,
    *,
    max_attempts: int,
    retry_seconds: int,
) -> None:
    """Run one leased enrichment independently from any AG-UI execution."""
    metric_token = begin_agent_metric_collection()
    started = time.perf_counter()
    try:
        questions: list[str]
        suggestion_context: tuple[str, str, list[dict[str, str]], Any, str] | None = None
        with session_factory() as db:
            item = _owned_enrichment(db, enrichment_id, user_id)
            if item is None or item.status != AgentEnrichmentStatus.RUNNING.value:
                return
            run = db.scalar(select(AgentRun).where(AgentRun.id == item.run_id, AgentRun.user_id == user_id))
            user = db.get(User, item.user_id)
            conversation = db.scalar(select(Conversation).where(Conversation.id == item.conversation_id, Conversation.user_id == user_id))
            message = db.scalar(select(Message).where(Message.id == item.message_id, Message.conversation_id == item.conversation_id))
            if run is None or user is None or conversation is None or message is None:
                metrics = agent_metric_snapshot()
                metrics["enrichmentDurationMs"] = round((time.perf_counter() - started) * 1000, 1)
                _complete(db, item, status=AgentEnrichmentStatus.SKIPPED, metrics=metrics, error_code="source_missing")
                return
            widget = _stored_related_widget(message)
            if widget is not None:
                metrics = agent_metric_snapshot()
                metrics["enrichmentDurationMs"] = round((time.perf_counter() - started) * 1000, 1)
                _complete(db, item, status=AgentEnrichmentStatus.COMPLETED, metrics=metrics)
                return
            if run.task_status == "failed":
                questions = _failed_turn_questions(user)
            else:
                questions = []
                suggestion_context = _suggestion_context(db, run, user, conversation, message)

        # Never reserve a database connection while waiting on the provider.
        if suggestion_context is not None:
            question, answer, recent_turns, current_date, user_timezone = suggestion_context
            questions = suggest_related_questions(
                question,
                answer,
                recent_turns,
                capability_notes(),
                current_date,
                user_timezone,
            )

        with session_factory() as db:
            item = _owned_enrichment(db, enrichment_id, user_id)
            if item is None or item.status != AgentEnrichmentStatus.RUNNING.value:
                return
            message = db.scalar(select(Message).where(Message.id == item.message_id, Message.conversation_id == item.conversation_id))
            widget = _stored_related_widget(message) if message is not None else None
            if widget is None and message is not None and questions:
                widget = Widget(
                    id=f"related-questions-{message.id}",
                    type=WidgetType.RELATED_QUESTIONS,
                    data={"questions": questions},
                )
                message.widgets = [*message.widgets, widget.model_dump(mode="json")]
            metrics = agent_metric_snapshot()
            metrics["enrichmentDurationMs"] = round((time.perf_counter() - started) * 1000, 1)
            _complete(
                db,
                item,
                status=(
                    AgentEnrichmentStatus.COMPLETED
                    if widget is not None
                    else AgentEnrichmentStatus.SKIPPED
                ),
                metrics=metrics,
            )
    except Exception as error:
        metrics = agent_metric_snapshot()
        metrics["enrichmentDurationMs"] = round((time.perf_counter() - started) * 1000, 1)
        _record_failure(
            session_factory,
            enrichment_id,
            user_id,
            error,
            max_attempts=max_attempts,
            retry_seconds=retry_seconds,
            metrics=metrics,
        )
    finally:
        end_agent_metric_collection(metric_token)


def related_questions_status(
    db: Session,
    *,
    run_id: UUID,
    user_id: UUID,
) -> AgentEnrichmentOut | None:
    item = db.scalar(
        select(AgentEnrichment).where(
            AgentEnrichment.run_id == run_id,
            AgentEnrichment.user_id == user_id,
            AgentEnrichment.kind == RELATED_QUESTIONS_KIND,
        )
    )
    if item is None:
        return None
    message = db.get(Message, item.message_id)
    widget = _stored_related_widget(message) if message is not None else None
    return AgentEnrichmentOut(
        run_id=item.run_id,
        message_id=item.message_id,
        kind=RELATED_QUESTIONS_KIND,
        status=item.status,
        widget=widget,
    )
