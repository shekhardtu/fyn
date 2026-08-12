from datetime import datetime, timedelta, timezone

from app.models import Conversation, Message, User
from app.schemas import ConversationOut
from app.services.conversation import user_conversation


def test_conversation_messages_are_loaded_and_serialized_oldest_first(db):
    user = User(email="ordered-thread@example.com", display_name="Order Test")
    db.add(user)
    db.flush()
    conversation = Conversation(user_id=user.id, title="Ordered thread")
    db.add(conversation)
    db.flush()
    start = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)

    # Insert deliberately out of chronological order. PostgreSQL is free to
    # return either insertion or physical order unless the relationship orders.
    later = Message(conversation_id=conversation.id, role="assistant", content="Second", created_at=start + timedelta(seconds=2))
    earlier = Message(conversation_id=conversation.id, role="user", content="First", created_at=start)
    db.add_all([later, earlier])
    db.commit()
    user_id = user.id
    conversation_id = conversation.id
    db.expunge_all()

    loaded = user_conversation(db, user_id, conversation_id, with_messages=True)
    assert loaded is not None
    assert [message.content for message in loaded.messages] == ["First", "Second"]
    serialized = ConversationOut.model_validate(loaded)
    assert [message.content for message in serialized.messages] == ["First", "Second"]
