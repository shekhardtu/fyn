"""Transactional-outbox delivery for reusable shared-record notifications."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from ..config import Settings, get_settings
from ..event_time import now_utc
from ..models import NotificationOutbox
from .otp_delivery import DELIVERY_TIMEOUT_SECONDS, MSG91_FLOW_URL, POSTMARK_EMAIL_URL
from .shared_records import decrypt_destination


class LendingNotificationError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _link(item: NotificationOutbox, settings: Settings) -> str:
    origin = settings.lending_web_url.rstrip("/")
    if item.topic == "shared_record.invitation":
        if not item.context_ciphertext:
            raise LendingNotificationError("missing_invitation_context")
        token = decrypt_destination(item.context_ciphertext)
        return f"{origin}/loan-invitations/{token}"
    loan_id = str(item.payload.get("loanId") or "")
    return f"{origin}/loans/{loan_id}" if loan_id else f"{origin}/loans"


def _copy(item: NotificationOutbox, settings: Settings) -> tuple[str, str, str]:
    sender = str(item.payload.get("senderName") or "Someone you know")
    link = _link(item, settings)
    if item.topic == "shared_record.invitation":
        subject = f"{sender} shared a repayment plan with you"
        body = (
            f"{sender} invited you to review a private shared repayment plan in fyn AI.\n\n"
            f"Review the plan: {link}\n\n"
            "Sign in with the phone number or email address that received this message. "
            "fyn AI records shared understanding and reminders; it does not move money or decide disputes."
        )
        return subject, body, link
    due_date = str(item.payload.get("dueDate") or "the agreed date")
    note = str(item.payload.get("note") or "").strip()
    note_copy = f"\n\nTheir note: {note}" if note else ""
    subject = "A friendly reminder about your shared repayment plan"
    body = (
        f"{sender} sent a friendly reminder about the repayment plan due {due_date}."
        f"{note_copy}\n\nOpen the shared record: {link}\n\n"
        "This is a record and reminder from fyn AI, not a collection notice."
    )
    return subject, body, link


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _deliver_email(destination: str, subject: str, body: str, settings: Settings) -> None:
    if settings.email_sender_name == "console":
        return
    if not (settings.postmark_server_token and settings.postmark_from_email):
        raise LendingNotificationError("email_provider_unconfigured")
    try:
        response = httpx.post(
            POSTMARK_EMAIL_URL,
            json={
                "From": settings.postmark_from_email,
                "To": destination,
                "Subject": subject,
                "TextBody": body,
                "MessageStream": settings.postmark_message_stream,
            },
            headers={"X-Postmark-Server-Token": settings.postmark_server_token, "Accept": "application/json"},
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise LendingNotificationError("email_provider_unreachable") from error
    if response.status_code >= 400:
        raise LendingNotificationError("email_provider_rejected")


def _deliver_sms(destination: str, item: NotificationOutbox, link: str, settings: Settings) -> None:
    if settings.sms_sender_name == "console":
        return
    if not (settings.msg91_auth_key and settings.msg91_lending_template_id):
        raise LendingNotificationError("sms_template_unconfigured")
    payload: dict[str, Any] = {
        "template_id": settings.msg91_lending_template_id,
        "short_url": "1",
        "recipients": [{
            "mobiles": destination.lstrip("+"),
            "NAME": str(item.payload.get("senderName") or "A contact"),
            "DUE_DATE": str(item.payload.get("dueDate") or "see plan"),
            "LINK": link,
        }],
    }
    if settings.msg91_sender_id:
        payload["sender"] = settings.msg91_sender_id
    try:
        response = httpx.post(
            MSG91_FLOW_URL,
            json=payload,
            headers={"authkey": settings.msg91_auth_key, "accept": "application/json"},
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise LendingNotificationError("sms_provider_unreachable") from error
    body = _json_or_none(response)
    if response.status_code >= 400 or (isinstance(body, dict) and str(body.get("type", "")).lower() == "error"):
        raise LendingNotificationError("sms_provider_rejected")


def deliver_item(item: NotificationOutbox, settings: Settings | None = None) -> None:
    active = settings or get_settings()
    destination = decrypt_destination(item.destination_ciphertext)
    subject, body, link = _copy(item, active)
    if item.channel == "email":
        _deliver_email(destination, subject, body, active)
    elif item.channel == "phone":
        _deliver_sms(destination, item, link, active)
    else:
        raise LendingNotificationError("unsupported_channel")


def deliver_one(session_factory: sessionmaker, settings: Settings | None = None) -> bool:
    """Claim and deliver one row; returns false when the queue is empty.

    Claiming commits before the network call. A crashed worker leaves the row
    reclaimable after its lease, while the provider-level dedupe key prevents a
    financial command retry from ever inserting a second notification.
    """
    active = settings or get_settings()
    now = now_utc()
    with session_factory() as db:
        item = db.scalar(
            select(NotificationOutbox)
            .where(
                or_(NotificationOutbox.state == "pending", NotificationOutbox.state == "processing"),
                NotificationOutbox.available_at <= now,
            )
            .order_by(NotificationOutbox.available_at, NotificationOutbox.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is None:
            return False
        item.state = "processing"
        item.attempts += 1
        item.available_at = now + timedelta(minutes=5)
        item_id = item.id
        db.commit()

    try:
        with session_factory() as db:
            item = db.get(NotificationOutbox, item_id)
            if item is None:
                return True
            deliver_item(item, active)
    except LendingNotificationError as error:
        with session_factory() as db:
            item = db.get(NotificationOutbox, item_id)
            if item is not None:
                item.last_error_code = error.code
                if item.attempts >= active.lending_notification_max_attempts:
                    item.state = "failed"
                else:
                    item.state = "pending"
                    item.available_at = now_utc() + timedelta(minutes=min(2 ** item.attempts, 60))
                db.commit()
        return True

    with session_factory() as db:
        item = db.get(NotificationOutbox, item_id)
        if item is not None:
            item.state = "sent"
            item.sent_at = now_utc()
            item.last_error_code = None
            db.commit()
    return True
