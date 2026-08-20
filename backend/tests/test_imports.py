from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.domain import AgentInterruptStatus, AgentRunStatus, ImportStatus, WidgetActionId
from app.models import AgentInterrupt, AgentRun, Import, Message, Transaction, User
from app.schemas import Widget, WidgetAction, WidgetType
from app.seed import DEFAULT_USER_EMAIL
from app.services.conversation import get_or_create_conversation, handle_action


STATEMENT = b"date,description,debit,credit,transaction id\n2026-08-10,TOIT POS,2000,,bank-1\n2026-08-10,Salary,,300000,bank-2\n"


def test_csv_is_staged_confirmed_and_idempotent(db):
    """Driven over HTTP, because the camelCase keys asserted below only exist
    there. Calling the route function directly returns the model and proves
    nothing about whether the response survives serialization — which is how
    this endpoint once answered 500 to every real caller with the test green."""
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    conversation = get_or_create_conversation(db, user)

    # Reproduce the collision that used to deadlock the thread: an older AG-UI
    # decision remains open when the user deliberately attaches a statement.
    stale_widget = Widget(
        id="stale-before-upload",
        type=WidgetType.IMPORT_REVIEW,
        data={
            "title": "Older review",
            "importId": str(uuid4()),
            "status": ImportStatus.AWAITING_CONFIRMATION,
            "total": 0,
            "highConfidence": 0,
            "needsReview": 0,
            "duplicates": 0,
            "idempotentReplay": False,
        },
        actions=[WidgetAction(
            id="cancel",
            label="Cancel",
            action=WidgetActionId.CANCEL_PENDING_ACTION,
            style="ghost",
            payload={"resourceId": str(uuid4())},
        )],
    )
    db.add(Message(
        conversation_id=conversation.id,
        role="assistant",
        content="Review the older request.",
        widgets=[stale_widget.model_dump(mode="json")],
        citations=[],
    ))
    stale_run = AgentRun(
        user_id=user.id,
        conversation_id=conversation.id,
        status=AgentRunStatus.INTERRUPTED.value,
        cancel_requested=False,
        input_payload={"kind": "message", "text": "older request"},
        last_sequence=0,
    )
    db.add(stale_run)
    db.flush()
    stale_interrupt = AgentInterrupt(
        run_id=stale_run.id,
        tool_call_id=f"stale-upload-{uuid4()}",
        widget_id=stale_widget.id,
        reason="tool_call",
        message="Review the older request",
        response_schema={},
        metadata_payload={},
        status=AgentInterruptStatus.OPEN.value,
    )
    db.add(stale_interrupt)
    db.commit()

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user

    def upload(client):
        return client.post(
            "/imports/csv",
            data={"conversation_id": str(conversation.id)},
            files={"file": ("statement.csv", STATEMENT, "text/csv")},
        )

    with TestClient(application) as client:
        staged = upload(client)
        assert staged.status_code == 200, staged.text
        preview = staged.json()
        assert preview["status"] == "awaiting_confirmation"
        assert preview["highConfidence"] == 2
        assert preview["agentResponse"]["widgets"][0]["type"] == "import_review"
        stale_update = next(
            update for update in preview["agentResponse"]["widgetUpdates"]
            if update["widgetId"] == stale_widget.id
        )
        assert stale_update["widget"]["data"]["lifecycle"] == "cancelled"
        db.refresh(stale_interrupt)
        assert stale_interrupt.status == AgentInterruptStatus.CANCELLED.value
        assert client.get(f"/agent/threads/{conversation.id}").json()["interrupts"] == []
        assert list(db.scalars(select(Transaction))) == []

        action = preview["agentResponse"]["widgets"][0]["actions"][0]
        response = handle_action(db, user, conversation, action["action"], action["payload"])
        assert response.widgets[0].data["status"] == "completed"
        assert len(list(db.scalars(select(Transaction)))) == 2

        replayed = upload(client)
        assert replayed.status_code == 200, replayed.text
        replay = replayed.json()
        assert replay["idempotentReplay"] is True
        assert replay["agentResponse"]["widgets"][0]["actions"] == []
        assert len(list(db.scalars(select(Transaction)))) == 2
        assert db.scalar(select(Import)).status == "completed"
