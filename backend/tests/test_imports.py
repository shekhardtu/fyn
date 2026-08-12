from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import current_user, router
from app.database import get_db
from app.models import Import, Transaction, User
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

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[current_user] = lambda: user

    def upload(client):
        return client.post(
            "/api/imports/csv",
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
