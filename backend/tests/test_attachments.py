from __future__ import annotations

import io
import json
from datetime import date
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from ag_ui.core import RunAgentInput
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select

from app.api import router as chat_router
from app.api_files import router
from app.config import FILE_UPLOAD_MAX_BYTES as MAX_FILE_BYTES, Settings
from app.database import get_db
from app.event_time import now_utc
from app.models import Conversation, ConversationAttachment, Message, ObjectDeletion, User
from app.schemas import ConversationOut
from app.security import current_user
from app.services import attachments, files
from app.services.file_cleanup import cleanup_files
from app.services.agui import normalize_run_input
from app.services.attachment_validation import validate_file
from app.services.attachment_tables import summarize_table
from app.services.attachment_tools import AttachmentContext
from app.services.conversation import handle_chat
from app.services.conversation import _recent_complete_turn_context


@pytest.fixture()
def setup(db, monkeypatch, file_store):
    user = db.scalar(select(User))
    thread = Conversation(user_id=user.id, title="Files")
    second = Conversation(user_id=user.id, title="Other thread")
    db.add_all([thread, second])
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.include_router(chat_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return SimpleNamespace(db=db, user=user, thread=thread, second=second, client=TestClient(app), settings=Settings(), store=file_store)


def save(setup, filename="notes.txt", content=b"Paid 42.25 for lunch"):
    row = attachments.reserve_upload(setup.db, setup.user.id, setup.thread.id, filename, len(content))
    setup.db.commit()
    files.store_upload(setup.db, row, io.BytesIO(content), setup.settings)
    setup.db.commit()
    return row


def bind(setup, row):
    message = attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id, [str(row.id)], "Read this file")
    setup.db.commit()
    return message


def test_backend_upload_saves_immutable_bytes_and_identical_retry_is_idempotent(setup):
    initial = setup.client.post("/files", json={"purpose": "conversation", "conversation_id": str(setup.thread.id), "filename": "../note.txt", "byte_size": 4})
    assert initial.status_code == 201
    data = initial.json()
    assert "upload_url" not in data and "storage_key" not in data
    row = setup.db.get(ConversationAttachment, UUID(data["id"]))
    endpoint = f"/files/{row.id}/content"
    def upload(content):
        return setup.client.post(endpoint, content=content, headers={"Content-Type": "application/octet-stream"})
    done = upload(b"note")
    assert done.status_code == 200, done.text
    assert done.json()["filename"] == "note.txt"
    assert done.json()["read_mode"] == "native"
    assert upload(b"note").status_code == 200
    assert upload(b"evil").status_code == 409
    assert setup.store.objects[row.storage_key] == b"note"
    downloaded = setup.client.get(endpoint)
    assert downloaded.status_code == 200 and downloaded.content == b"note"
    assert "location" not in downloaded.headers
    assert downloaded.headers["Cache-Control"] == "private, no-store"


def test_message_and_refresh_preserve_original_attachment(setup):
    row = save(setup)
    message = bind(setup, row)
    response = handle_chat(setup.db, setup.user, setup.thread, message.content, source_user_message=message)
    assert response.user_message_id == message.id
    assert len(list(setup.db.scalars(select(Message).where(Message.role == "user")))) == 1
    setup.db.expire_all()
    conversation = setup.db.get(Conversation, setup.thread.id)
    serialized = ConversationOut.model_validate(conversation).model_dump(mode="json")
    assert serialized["messages"][0]["attachments"][0]["id"] == str(row.id)
    assert row.expires_at is None


def test_draft_and_other_thread_files_cannot_enter_agent_context(setup):
    row = save(setup)
    context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, uuid4())
    assert context.manifest() == []
    with pytest.raises(attachments.AttachmentError):
        context.read_attachment(str(row.id))
    bind(setup, row)
    other = AttachmentContext(setup.db, setup.user.id, setup.second.id, uuid4())
    with pytest.raises(attachments.AttachmentError):
        other.read_attachment(str(row.id))
    with pytest.raises(attachments.AttachmentError):
        attachments.bind_to_message(setup.db, setup.user.id, setup.second.id, [str(row.id)], "Read")
    with pytest.raises(attachments.AttachmentError):
        attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id, [str(row.id)], "Read again")


def test_queued_attachment_turns_keep_reply_order_and_exclude_future_files(setup):
    first_file = save(setup, "first.txt", b"First document")
    first = bind(setup, first_file)
    first_reply = Message(conversation_id=setup.thread.id, role="assistant", content="", widgets=[], citations=[])
    setup.db.add(first_reply)
    setup.db.flush()
    second_file = save(setup, "second.txt", b"Later document")
    second = bind(setup, second_file)
    second_reply = Message(conversation_id=setup.thread.id, role="assistant", content="", widgets=[], citations=[])
    setup.db.add(second_reply)
    setup.db.commit()

    earlier_context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, first.id)
    assert [item["id"] for item in earlier_context.manifest()] == [str(first_file.id)]
    with pytest.raises(attachments.AttachmentError, match="after the current request"):
        earlier_context.read_attachment(str(second_file.id))
    handle_chat(setup.db, setup.user, setup.thread, first.content,
                source_user_message=first, source_assistant_message=first_reply)
    history = _recent_complete_turn_context(setup.db, setup.thread, second)
    assert [item["content"] for item in history] == [first.content, first_reply.content]
    handle_chat(setup.db, setup.user, setup.thread, second.content,
                source_user_message=second, source_assistant_message=second_reply)
    ids = list(setup.db.scalars(select(Message.id).where(Message.conversation_id == setup.thread.id).order_by(Message.created_at, Message.id)))
    assert ids == [first.id, first_reply.id, second.id, second_reply.id]


def test_other_user_cannot_read_download_or_upload(setup):
    row = save(setup)
    setup.client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=uuid4())
    base = f"/files/{row.id}"
    for route, method in [(base + "/content", "get"), (base + "/content", "post"), (base, "delete")]:
        assert getattr(setup.client, method)(route).status_code == 404


def test_native_csv_read_and_exact_whole_file_arithmetic(setup):
    row = save(setup, "statement.csv", ("merchant,amount\n" + "Cafe,0.10\n" * 1000 + "Shop,1.99\nCafe,unknown\n").encode())
    message = bind(setup, row)
    context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, message.id)
    result = context.read_attachment(str(row.id))
    assert result.files[0].content == setup.store.objects[row.storage_key]
    assert json.loads(result.content)["status"] == "original_attached"
    total = context.summarize_attachment_table(str(row.id), "amount")
    assert total["sum"] == "101.99"
    assert total["matchedRows"] == 1002
    assert total["invalidOrEmptyRows"] == 1
    assert context.summarize_attachment_table(str(row.id), "amount", "merchant", "Cafe")["sum"] == "100.00"


def test_csv_aggregation_preserves_precision_across_large_amounts(setup):
    row = save(setup, "amounts.csv", ("amount\n" + "999999999999999999.12345678\n" * 1000).encode())
    message = bind(setup, row)
    result = AttachmentContext(setup.db, setup.user.id, setup.thread.id, message.id).summarize_attachment_table(str(row.id), "amount")
    assert result["sum"] == "999999999999999999123.45678000"
    assert result["invalidOrEmptyRows"] == 0


def test_table_computation_streams_original_rows_without_expanding_repeated_headers():
    content = ("h" * 128 + "\n" + "1\n" * 10_000).encode()
    assert validate_file("table.csv", content).read_mode == "native"
    result = summarize_table(content, "text/csv", "h" * 128)
    assert result["rowsRead"] == 10_000
    assert result["sum"] == "10000"


def test_original_pdf_image_and_text_reach_one_operator_call(setup, monkeypatch):
    from agno.run.agent import RunOutput
    from app.services import agents
    from agno.models.openai import OpenAIResponses
    from agno.models.message import Message as ModelMessage
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    pdf = save(setup, "scan.pdf", buffer.getvalue())
    image_buffer = io.BytesIO()
    Image.new("RGB", (20, 20)).save(image_buffer, format="PNG")
    image = save(setup, "receipt.png", image_buffer.getvalue())
    note = save(setup, "notes.txt", b"Payment details")
    message = attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id,
                                         [str(pdf.id), str(image.id), str(note.id)], "Read these files")
    setup.db.commit()
    context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, message.id)
    calls = []
    class Operator:
        def run(self, prompt, **kwargs):
            calls.append(kwargs)
            return iter([RunOutput(status="COMPLETED", content="Read the original files.")])
    monkeypatch.setattr(agents, "build_operator", lambda *args, **kwargs: Operator())
    monkeypatch.setattr("agno.agent.Agent.run", lambda *args, **kwargs: pytest.fail("No separate reader model is allowed"))
    result = agents.run_operator(message.content, [], date(2026, 9, 10), "Asia/Kolkata", [], attachments=context)
    assert len(calls) == 1
    assert [file.content for file in calls[0]["files"]] == [buffer.getvalue(), b"Payment details"]
    assert calls[0]["images"][0].content == image_buffer.getvalue()
    assert {source.id for source in result.attachment_sources} == {str(pdf.id), str(image.id), str(note.id)}
    assert result.tool_grounding == []
    model = OpenAIResponses(id="test", api_key="test-key")
    formatted = model._format_messages([ModelMessage(role="user", content="Read", files=calls[0]["files"], images=calls[0]["images"])])
    assert [block["type"] for block in formatted[0]["content"]] == ["input_text", "input_image", "input_file", "input_file"]


def test_followup_file_tool_reaches_the_same_model_as_native_media(setup):
    from agno.models.openai import OpenAIResponses
    from agno.models.message import Message as ModelMessage
    from agno.tools.function import ToolResult
    from app.services import agents
    from app.services.agent_tools import bind_existing_tool
    row = save(setup)
    bind(setup, row)
    followup = attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id, [], "What does the earlier note say?")
    setup.db.commit()
    context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, followup.id)
    assert context.initial_inputs().files == []
    result = bind_existing_tool(context.read_attachment).entrypoint(attachment_id=str(row.id))
    assert isinstance(result, ToolResult)
    assert result.files[0].content == setup.store.objects[row.storage_key]
    model = OpenAIResponses(id="test", api_key="test-key")
    tool_message = ModelMessage(role="tool", tool_call_id="call_read", content=result.content, files=result.files)
    messages = [tool_message]
    model._handle_function_call_media(messages, [tool_message])
    formatted = model._format_messages(messages)
    assert formatted[0]["type"] == "function_call_output"
    assert formatted[1]["role"] == "user"
    assert formatted[1]["content"][1]["type"] == "input_file"
    assert json.loads(context.read_attachment(str(row.id)).content)["status"] == "already_in_context"
    evidence = agents._runtime_tool_grounding(SimpleNamespace(tools=[SimpleNamespace(tool_name="read_attachment", result=result.content)]), context.tools())
    assert evidence == []


def test_generic_unsupported_file_is_saved_with_honest_read_status(setup):
    row = save(setup, "archive.zip", b"PK\x03\x04archive")
    assert row.status == "ready" and row.read_mode == "unavailable"
    assert row.read_error
    assert setup.client.get(f"/files/{row.id}/content", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("claims_write", [False, True])
def test_native_document_observations_have_provenance_without_authorizing_writes(setup, monkeypatch, claims_write):
    from app.config import get_settings
    from app.models import Transaction
    from app.services import conversation as conversation_service
    from app.services.agents import OperatorResult
    row = save(setup, "receipt.txt", b"Receipt total: INR 42.25")
    message = attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id, [str(row.id)],
                                         "What amount is written in the attached receipt?")
    setup.db.commit()
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    get_settings.cache_clear()
    reply = "I saved a transaction for INR 42.25." if claims_write else "The uploaded receipt.txt shows INR 42.25."
    def operator(*args, **kwargs):
        context = kwargs["attachments"]
        assert context.initial_inputs().files[0].content == b"Receipt total: INR 42.25"
        return OperatorResult(reply=reply, attachment_sources=context.sources)
    monkeypatch.setattr(conversation_service, "run_operator", operator)
    try:
        response = handle_chat(setup.db, setup.user, setup.thread, message.content, source_user_message=message)
        assert setup.db.scalar(select(Transaction)) is None
        if claims_write:
            assert response.message != reply
        else:
            assert response.message == reply
            assert response.citations[0].entity_ids == [str(row.id)]
            assert response.citations[0].query["source_kind"] == "user_attachment"
            assert response.citations[0].query["interpretation"] == "native_model"
    finally:
        get_settings.cache_clear()


def test_native_file_presence_does_not_bypass_ledger_evidence_validation(setup, monkeypatch):
    from app.config import get_settings
    from app.services import conversation as conversation_service
    from app.services.agents import OperatorResult, ToolGrounding

    row = save(setup, "note.txt", b"An unrelated document amount: INR 999")
    message = attachments.bind_to_message(setup.db, setup.user.id, setup.thread.id, [str(row.id)],
                                         "How much did I spend in my saved transactions?")
    setup.db.commit()
    monkeypatch.setenv("PRIMARY_AGENT_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    get_settings.cache_clear()
    verified_reply = "You spent ₹120 across 2 transactions from 2026-08-01 through 2026-08-14."

    def operator(*args, **kwargs):
        context = kwargs["attachments"]
        context.initial_inputs()
        return OperatorResult(
            reply="You spent ₹999 across 2 transactions.",
            attachment_sources=context.sources,
            tool_grounding=[ToolGrounding(name="run_financial_analysis", arguments={}, result={
                "kind": "governed_analysis", "message": verified_reply,
                "query_results": [{"name": "Total spend", "rows": [{"value": 12_000, "count": 2}]}],
            })],
        )

    monkeypatch.setattr(conversation_service, "run_operator", operator)
    try:
        response = handle_chat(setup.db, setup.user, setup.thread, message.content, source_user_message=message)
        assert response.message == verified_reply
        assert response.error_code == "unsupported_money_claim"
        assert response.task_status == "degraded"
        assert response.citations[0].query["tool"] == "run_financial_analysis"
        assert response.citations[-1].entity_ids == [str(row.id)]
        assert response.citations[-1].query["interpretation"] == "native_model"
    finally:
        get_settings.cache_clear()


def test_expiry_and_deletion_remove_rows_and_queue_r2_cleanup(setup):
    row = save(setup)
    key = row.storage_key
    row.expires_at = now_utc() - timedelta(seconds=1)
    setup.db.commit()
    cleanup_files(setup.db, setup.settings)
    assert key not in setup.store.objects
    assert setup.db.get(ConversationAttachment, row.id) is None


def test_conversation_deletion_queues_objects_without_losing_cleanup_record(setup):
    row = save(setup)
    bind(setup, row)
    key = row.storage_key
    assert setup.client.delete(f"/conversations/{setup.thread.id}").status_code == 204
    assert setup.db.get(ConversationAttachment, row.id) is None
    assert setup.db.scalar(select(ObjectDeletion).where(ObjectDeletion.storage_key == key))
    cleanup_files(setup.db, setup.settings)
    assert key not in setup.store.objects


@pytest.mark.parametrize("ids", [["bad"], [str(uuid4())] * 2, [str(uuid4()) for _ in range(6)], "bad"])
def test_agui_rejects_invalid_attachment_handoff(ids):
    value = RunAgentInput(thread_id=str(uuid4()), run_id=str(uuid4()), state={}, tools=[], context=[],
        forwarded_props={"fynAttachmentIds": ids}, messages=[{"id": "m", "role": "user", "content": "Read"}])
    with pytest.raises(ValueError):
        normalize_run_input(value)


def test_invalid_and_oversized_inputs_do_not_upload(setup):
    for size in (0, MAX_FILE_BYTES + 1):
        result = setup.client.post("/files", json={"purpose": "conversation", "conversation_id": str(setup.thread.id), "filename": "file.txt", "byte_size": size})
        assert result.status_code == 422
    boundary = setup.client.post("/files", json={"purpose": "conversation", "conversation_id": str(setup.thread.id), "filename": "file.txt", "byte_size": MAX_FILE_BYTES})
    assert boundary.status_code == 201
    with pytest.raises(ValueError):
        validate_file("fake.png", b"not an image")
    assert validate_file("bad.csv", b"a,b\n1,2,3").read_mode == "native"
    with pytest.raises(ValueError, match="No partial total"):
        summarize_table(b"a,b\n1,2,3", "text/csv", "a")


def test_saved_csv_import_uses_explicit_review_and_preserves_attachment(setup):
    row = save(setup, "bank.csv", b"date,description,debit,credit\n2026-09-01,Lunch,50,\n")
    result = setup.client.post("/imports/csv", json={"file_id": str(row.id), "conversation_id": str(setup.thread.id)})
    assert result.status_code == 200
    assert result.json()["agentResponse"]["pendingAction"] is not None
    assert setup.db.get(ConversationAttachment, row.id).message_id is not None
    from app.models import Transaction
    assert setup.db.scalar(select(Transaction)) is None


def test_failed_import_preview_does_not_leave_a_partial_job_or_bind_the_file(setup, monkeypatch):
    from app.models import Import as ImportJob, ImportRecord
    from app.services import import_previews

    content = b"date,description,debit,credit\n2026-09-01,Lunch,50,\n"
    row = save(setup, "bank.csv", content)

    def fail_reply(*args, **kwargs):
        raise RuntimeError("Reply persistence failed")

    monkeypatch.setattr(import_previews, "persist_agent_response", fail_reply)
    with pytest.raises(RuntimeError, match="Reply persistence failed"):
        import_previews.preview_csv_import(setup.db, setup.user, setup.thread, row.filename, content, attachment=row)
    setup.db.rollback()
    assert setup.db.scalar(select(ImportJob)) is None
    assert setup.db.scalar(select(ImportRecord)) is None
    setup.db.refresh(row)
    assert row.message_id is None
    assert row.status == "ready"


def test_agent_tools_hide_authorization_parameters(setup):
    row = save(setup)
    message = bind(setup, row)
    context = AttachmentContext(setup.db, setup.user.id, setup.thread.id, message.id)
    tools = context.tools()
    assert {tool.name for tool in tools} == {"read_attachment", "summarize_attachment_table"}
    assert all("user_id" not in tool.parameters.get("properties", {}) for tool in tools)


def test_visual_input_uses_native_provider_file_block():
    from agno.media import File
    from agno.models.message import Message as ModelMessage
    from agno.models.openai import OpenAIResponses
    model = OpenAIResponses(id="test", api_key="test-key")
    formatted = model._format_messages([ModelMessage(role="user", content="Read", files=[File(content=b"%PDF-test", mime_type="application/pdf", filename="page.pdf")])])
    assert formatted[0]["content"][1]["type"] == "input_file"
    assert formatted[0]["content"][1]["file_data"].startswith("data:application/pdf;base64,")
