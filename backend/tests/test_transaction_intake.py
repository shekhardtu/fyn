from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models import Account, AgentEvent, AgentInterrupt, Message, Transaction, TransactionDraft, User
from app.seed import DEFAULT_USER_EMAIL
from app.event_time import local_date
from app.operation_types import ContextRelationship, RequestedEffect
from app.services import conversation as conversation_service
from app.services.agents import CopilotDecision, TransactionInterpretation
from app.services.capabilities import capability_for_primitive
from app.services.conversation import get_or_create_conversation, handle_action, handle_chat
from app.services.extraction import extract_transaction
from app.services.turn_policy import resolve_turn_intent
from test_agui_runtime import _execute


def _thread(db):
    user = db.scalar(select(User).where(User.email == DEFAULT_USER_EMAIL))
    return user, get_or_create_conversation(db, user)


def _terminal(db, run, live):
    events = list(db.scalars(select(AgentEvent).where(AgentEvent.run_id == run.id).order_by(AgentEvent.sequence)))
    assert [item.sequence for item in events] == list(range(1, len(events) + 1))
    assert [(item.sequence, item.payload) for item in events] == live
    assert [item.event_type for item in events if item.event_type in {"RUN_FINISHED", "RUN_ERROR"}] == ["RUN_FINISHED"]


def _resume_account(db, user, conversation, run, *, action_id="select_account", **values):
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == run.id, AgentInterrupt.status == "open"))
    widget = interrupt.metadata_payload["widget"]
    action = next(item for item in widget["actions"] if item["action"] == action_id)
    payload = {"kind": "resume", "entries": [{
        "interruptId": str(interrupt.id), "status": "resolved", "payload": {
            "approved": True, "editedArgs": {
                "widgetId": widget["id"], "action": action["action"], "payload": {**action["payload"], **values},
            },
        },
    }]}
    resumed, live = _execute(db, user, conversation, payload)
    _terminal(db, resumed, live)
    return resumed, payload


def test_transfer_selector_excludes_other_side_and_custom_duplicate_can_be_corrected(db):
    user, conversation = _thread(db)
    source = Account(user_id=user.id, name="Primary Bank", currency="INR")
    destination = Account(user_id=user.id, name="Savings", currency="INR")
    db.add_all([source, destination])
    db.commit()
    run, _live = _execute(db, user, conversation, {"kind": "message", "text": "Transferred ₹5,000 today"})
    run, source_payload = _resume_account(db, user, conversation, run, optionId=str(source.id))
    widget = next(item for item in db.get(Message, run.final_message_id).widgets if item["type"] == "account_selector")
    assert {item["id"] for item in widget["data"]["options"]} == {str(destination.id)}

    rejected, duplicate_payload = _resume_account(db, user, conversation, run, accountName="  PRIMARY   BANK  ")
    assert rejected.task_status == "needs_input"
    message = db.get(Message, rejected.final_message_id)
    assert "different" in message.content.lower()
    assert db.scalar(select(Transaction)) is None
    draft = db.scalar(select(TransactionDraft))
    assert draft.destination_account_id is None
    assert len(list(db.scalars(select(Account)))) == 2

    latest_interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.status == "open"))
    replayed, live = _execute(db, user, conversation, source_payload)
    _terminal(db, replayed, live)
    assert live[-1][1]["outcome"]["interrupts"][0]["id"] == str(latest_interrupt.id)
    assert replayed.final_message_id == run.final_message_id

    replayed, live = _execute(db, user, conversation, duplicate_payload)
    _terminal(db, replayed, live)
    assert replayed.final_message_id == rejected.final_message_id
    assert len(list(db.scalars(select(AgentInterrupt).where(AgentInterrupt.status == "open")))) == 1
    saved, _payload = _resume_account(db, user, conversation, rejected, optionId=str(destination.id))
    assert saved.task_status == "succeeded"
    transactions = list(db.scalars(select(Transaction)))
    assert len(transactions) == 1
    assert transactions[0].account_id == source.id
    assert transactions[0].destination_account_id == destination.id
    replayed, live = _execute(db, user, conversation, duplicate_payload)
    _terminal(db, replayed, live)
    assert not list(db.scalars(select(AgentInterrupt).where(AgentInterrupt.status == "open")))
    assert len(list(db.scalars(select(Transaction)))) == 1


def test_legacy_text_account_input_uses_the_same_distinctness_guard(db):
    user, conversation = _thread(db)
    handle_chat(db, user, conversation, "Transferred ₹5,000 today")
    handle_chat(db, user, conversation, "Primary Bank")
    response = handle_chat(db, user, conversation, " PRIMARY   BANK ")
    assert response.widgets[0].type == "account_selector"
    assert "different" in response.message.lower()
    assert db.scalar(select(Transaction)) is None


def test_source_selector_also_excludes_existing_destination(db):
    user, conversation = _thread(db)
    response = handle_chat(db, user, conversation, "Transferred ₹5,000 today")
    draft = db.get(TransactionDraft, UUID(response.widgets[0].data["draftId"]))
    destination = Account(user_id=user.id, name="Savings", currency="INR")
    db.add(destination)
    db.flush()
    draft.destination_account_id = destination.id
    draft.destination_account_name = destination.name
    response = handle_action(db, user, conversation, "revisit_transaction_step", {"draftId": str(draft.id), "step": "source_account"})
    assert str(destination.id) not in {item["id"] for item in response.widgets[0].data["options"]}


@pytest.mark.parametrize(("text", "kind"), [
    ("Paid 14000 salary to my maid", "expense"),
    ("I paid Alex 14000 as salary", "expense"),
    ("Got my salary of 14000 today", "income"),
    ("My employer paid me 14000 salary", "income"),
    ("Add 14000 maid salary", "unknown"),
    ("Add 14000 contractor salary", "unknown"),
    ("Transferred 20k to my wife, add this to matrimonial conflicts", "unknown"),
    ("Transferred 20k to Alex", "unknown"),
    ("Add expense of 20k transferred to Alex", "expense"),
    ("Moved 20k from HDFC to SBI", "transfer"),
])
def test_transaction_direction_uses_event_roles_not_keyword_precedence(text, kind):
    result = extract_transaction(text, today=date(2026, 9, 9))
    assert result.transaction_type == kind
    if kind == "unknown":
        assert "transaction_type" not in result.explicit_fields
        assert result.category_slug is None


@pytest.mark.parametrize(("text", "expected"), [
    ("Add 20k expense on 28th july", date(2026, 7, 28)),
    ("On 28 July, paid 2000 for coffee", date(2026, 7, 28)),
    ("Paid 2000 for coffee on 2026-07-28", date(2026, 7, 28)),
    ("Paid 2000 for coffee last night", date(2026, 9, 8)),
])
def test_explicit_event_date_is_not_replaced_with_today(text, expected):
    result = extract_transaction(text, today=date(2026, 9, 9))
    assert result.transaction_date == expected
    assert "transaction_date" in result.explicit_fields
    assert result.amount_minor == (2_000_000 if "20k" in text else 200_000)


@pytest.mark.parametrize("text", ["Add 14000 maid salary", "Transferred 20k to Alex"])
def test_ambiguous_direction_asks_before_creating_accounts_or_records(db, text):
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": text})
    _terminal(db, run, live)
    assert run.task_status == "needs_input"
    draft = db.scalar(select(TransactionDraft))
    assert draft.transaction_type == "unknown"
    assert draft.category_id is None
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(Account)) is None


def test_model_cannot_override_explicit_direction_or_date():
    decision = CopilotDecision(
        tool=capability_for_primitive("transaction.record@1"), reason="Model interpretation",
        transaction=TransactionInterpretation(
            transaction_type="income", amount_minor=1_400_000,
            transaction_date=date(2026, 9, 9), category_slug="income", subcategory_slug="salary",
            explicit_fields=["transaction_type", "transaction_date", "category", "subcategory"],
            source_account="Invented Source", destination_account="Invented Recipient",
        ),
    )
    result = conversation_service._extracted_from_decision("Paid 14000 salary on 28 July", decision, date(2026, 9, 9), "INR")
    assert result.transaction_type == "expense"
    assert result.transaction_date == date(2026, 7, 28)
    assert result.category_slug != "income"
    assert result.source_account is None
    assert result.destination_account is None
    result = conversation_service._extracted_from_decision("Add 14000 maid salary", decision, date(2026, 9, 9), "INR")
    assert result.transaction_type == "unknown"
    assert result.category_slug is None
    assert result.source_account is None
    assert result.destination_account is None


@pytest.mark.parametrize(("text", "option_id", "custom"), [
    ("Paid 2000 for coffee on 04/05/2026", "day_month_year", None),
    ("Paid 2000 for coffee on 31 February 2026", "custom", "2026-05-04"),
    ("Paid 2000 for coffee on 28 July and 29 July", "custom", "2026-05-04"),
])
def test_uncertain_dates_are_resumable_without_a_model_or_silent_default(db, text, option_id, custom):
    user, conversation = _thread(db)
    run, live = _execute(db, user, conversation, {"kind": "message", "text": text})
    _terminal(db, run, live)
    assert run.task_status == "needs_input"
    assert db.scalar(select(Transaction)) is None
    assert db.scalar(select(TransactionDraft)) is None
    values = {"optionId": option_id}
    if custom:
        values["customText"] = custom
    saved, payload = _resume_account(db, user, conversation, run, action_id="resolve_clarification", **values)
    assert saved.task_status == "succeeded"
    transaction = db.scalar(select(Transaction))
    assert transaction.amount_minor == 200_000
    assert local_date(transaction.transaction_at, user.timezone) == date(2026, 5, 4)
    replayed, live = _execute(db, user, conversation, payload)
    _terminal(db, replayed, live)
    assert len(list(db.scalars(select(Transaction)))) == 1


@pytest.mark.parametrize(("text", "expected_day", "expected_type", "expected_category"), [
    ("Similarly add 600", date(2026, 9, 9), "expense", "food"),
    ("Similarly add 600 on 28 July", date(2026, 7, 28), "expense", "food"),
    ("Similarly add 600 income on 28 July", date(2026, 7, 28), "income", "income"),
    ("Similarly add 600 expense for taxi on 28 July", date(2026, 7, 28), "expense", "travel"),
])
def test_similar_entry_uses_only_preceding_receipt_and_explicit_fields_win(db, monkeypatch, text, expected_day, expected_type, expected_category):
    from datetime import datetime, timezone
    from app.models import Category
    monkeypatch.setattr(conversation_service, "now_utc", lambda: datetime(2026, 9, 9, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(conversation_service, "_local_today", lambda _user: date(2026, 9, 9))
    user, conversation = _thread(db)
    prior_run, _ = _execute(db, user, conversation, {"kind": "message", "text": "Paid 2000 for coffee on 12 July"})
    assert prior_run.task_status == "succeeded"
    first_id = db.scalar(select(Transaction.id))
    run, live = _execute(db, user, conversation, {"kind": "message", "text": text})
    _terminal(db, run, live)
    assert run.task_status == "succeeded", db.get(Message, run.final_message_id).content
    transaction = db.scalar(select(Transaction).where(Transaction.id != first_id))
    assert transaction.amount_minor == 60_000
    assert transaction.transaction_type == expected_type
    assert local_date(transaction.transaction_at, user.timezone) == expected_day
    assert db.get(Category, transaction.category_id).slug == expected_category
    if text == "Similarly add 600":
        draft = db.scalar(select(TransactionDraft).where(TransactionDraft.amount_minor == 60_000))
        assert draft.field_provenance["category"]["basis"] == "preceding_transaction"
        assert draft.field_provenance["category"]["sourceMessageId"] == str(prior_run.final_message_id)


def test_similarly_does_not_reach_past_an_unrelated_response_or_another_thread(db):
    user, conversation = _thread(db)
    handle_chat(db, user, conversation, "Paid 2000 for coffee")
    conversation_service.persist_agent_response(db, conversation, "A separate discussion.")
    result = conversation_service._similar_transaction_extraction(db, user, conversation, "Similarly add 600", date(2026, 9, 9))
    assert result.transaction_type == "unknown"
    assert result.category_slug is None
    another = get_or_create_conversation(db, user)
    result = conversation_service._similar_transaction_extraction(db, user, another, "Similarly add 600", date(2026, 9, 9))
    assert result.transaction_type == "unknown"
    assert result.context_message_id is None
    assert conversation_service._similar_transaction_extraction(db, user, conversation, "Similarly show expenses above 600", date(2026, 9, 9)) is None
    assert len(list(db.scalars(select(Transaction)))) == 1


@pytest.mark.parametrize(("text", "effect"), [
    ("Similarly add 600", RequestedEffect.MUTATION),
    ("Likewise, please add 600", RequestedEffect.MUTATION),
    ("Similarly show expenses above 600", RequestedEffect.NONE),
    ("Similarly, could you show my expenses", RequestedEffect.NONE),
    ("Similarly do not add 600", RequestedEffect.UNKNOWN),
    ("Similarly I might add 600", RequestedEffect.UNKNOWN),
    ("Similarly 600", RequestedEffect.UNKNOWN),
])
def test_continuation_marker_does_not_supply_write_authority(text, effect):
    intent = resolve_turn_intent(text, ContextRelationship.FOLLOW_UP)
    assert intent.requested_effect == effect


def test_invalid_date_custom_value_retains_interrupt_and_can_be_corrected(db):
    user, conversation = _thread(db)
    run, _ = _execute(db, user, conversation, {"kind": "message", "text": "Paid 2000 for coffee on 31 February 2026"})
    interrupt = db.scalar(select(AgentInterrupt).where(AgentInterrupt.run_id == run.id, AgentInterrupt.status == "open"))
    widget = interrupt.metadata_payload["widget"]
    action = next(item for item in widget["actions"] if item["id"] == "custom")
    bad_payload = {"kind": "resume", "entries": [{"interruptId": str(interrupt.id), "status": "resolved", "payload": {
        "approved": True, "editedArgs": {"widgetId": widget["id"], "action": action["action"], "payload": {**action["payload"], "customText": "2026-02-31"}},
    }}]}
    rejected, live = _execute(db, user, conversation, bad_payload)
    assert rejected.error_code == "invalid_resume_payload"
    assert live[-1][1]["type"] == "RUN_ERROR"
    assert db.get(AgentInterrupt, interrupt.id).status == "open"
    assert db.scalar(select(Transaction)) is None
    saved, _ = _resume_account(db, user, conversation, run, action_id="resolve_clarification", optionId="custom", customText="2026-02-28")
    assert saved.task_status == "succeeded"


def test_model_clarification_cannot_erase_an_unresolved_date():
    from app.services.agents import ClarificationRequest
    seed = conversation_service._transaction_clarification_seed(
        "Paid 2000 on 31 February 2026",
        ClarificationRequest(question="Which category?", reason="Missing category", conflict_fields=["category"], allow_custom=True),
        date(2026, 9, 9), "INR",
    )
    assert seed.transaction_date is None
    assert "transaction_date" in seed.missing_fields


def test_date_resolution_on_similar_entry_keeps_original_context_anchor(db):
    user, conversation = _thread(db)
    prior_run, _ = _execute(db, user, conversation, {"kind": "message", "text": "Paid 2000 for coffee"})
    run, _ = _execute(db, user, conversation, {"kind": "message", "text": "Similarly add 600 on 31 February 2026"})
    saved, _ = _resume_account(db, user, conversation, run, action_id="resolve_clarification", optionId="custom", customText="2026-02-28")
    assert saved.task_status == "succeeded"
    draft = db.scalar(select(TransactionDraft).where(TransactionDraft.amount_minor == 60_000))
    assert draft.field_provenance["category"]["sourceMessageId"] == str(prior_run.final_message_id)
    assert local_date(draft.transaction_at, user.timezone) == date(2026, 2, 28)
