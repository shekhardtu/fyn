"""Where a one-time code actually goes.

Two providers, one seam. Everything above this module — challenge lifetimes,
attempt limits, linking rules — is identical whether a code travels as an SMS or
as an email, so the only thing that varies here is the request that carries it.

The console sender exists so a fresh checkout and the test suite can exercise
the whole sign-in flow without provider accounts. Startup refuses to leave it
active once ENVIRONMENT is "production": silently printing sign-in codes to a
server log is worse than failing to start.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from ..config import Settings, get_settings
from ..domain import OtpChannel


MSG91_FLOW_URL = "https://control.msg91.com/api/v5/flow/"
POSTMARK_EMAIL_URL = "https://api.postmarkapp.com/email"
DELIVERY_TIMEOUT_SECONDS = 10.0


class OtpDeliveryError(Exception):
    """The provider did not accept the message."""


class OtpSender(Protocol):
    def send(self, destination: str, code: str, settings: Settings) -> None:
        ...


def _minutes(settings: Settings) -> int:
    return max(settings.otp_ttl_seconds // 60, 1)


class ConsoleSender:
    """Prints the code. Local development and tests only."""

    def send(self, destination: str, code: str, settings: Settings) -> None:
        print(f"[otp] {destination} -> {code} (valid {_minutes(settings)}m)", flush=True)


class Msg91Sender:
    """MSG91 Flow API.

    The code is passed as a template variable rather than as message text, which
    is what DLT-registered Indian templates require; the variable name has to
    match the registered template and is therefore configurable.
    """

    def send(self, destination: str, code: str, settings: Settings) -> None:
        if not (settings.msg91_auth_key and settings.msg91_template_id):
            raise OtpDeliveryError("MSG91 is not configured.")
        recipient: dict[str, str] = {
            # MSG91 expects the country code without a leading plus.
            "mobiles": destination.lstrip("+"),
            settings.msg91_otp_variable: code,
        }
        payload: dict[str, object] = {
            "template_id": settings.msg91_template_id,
            "short_url": "0",
            "recipients": [recipient],
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
            raise OtpDeliveryError("Could not reach the SMS provider.") from error
        if response.status_code >= 400:
            raise OtpDeliveryError("The SMS provider rejected the message.")
        # MSG91 answers 200 with {"type": "error"} for template and DLT
        # problems, so the status code alone does not mean it was sent.
        body = _json_or_none(response)
        if isinstance(body, dict) and str(body.get("type", "")).lower() == "error":
            raise OtpDeliveryError("The SMS provider rejected the message.")


class PostmarkSender:
    def send(self, destination: str, code: str, settings: Settings) -> None:
        if not (settings.postmark_server_token and settings.postmark_from_email):
            raise OtpDeliveryError("Postmark is not configured.")
        minutes = _minutes(settings)
        try:
            response = httpx.post(
                POSTMARK_EMAIL_URL,
                json={
                    "From": settings.postmark_from_email,
                    "To": destination,
                    "Subject": settings.otp_email_subject,
                    "TextBody": (
                        f"Your fyn AI code is {code}.\n\n"
                        f"It expires in {minutes} minutes. If you didn't ask for it, ignore this email — "
                        "nothing has changed on your account."
                    ),
                    "MessageStream": settings.postmark_message_stream,
                },
                headers={
                    "X-Postmark-Server-Token": settings.postmark_server_token,
                    "Accept": "application/json",
                },
                timeout=DELIVERY_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            raise OtpDeliveryError("Could not reach the email provider.") from error
        if response.status_code >= 400:
            raise OtpDeliveryError("The email provider rejected the message.")


def _json_or_none(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


SMS_SENDERS: dict[str, OtpSender] = {"console": ConsoleSender(), "msg91": Msg91Sender()}
EMAIL_SENDERS: dict[str, OtpSender] = {"console": ConsoleSender(), "postmark": PostmarkSender()}


def sender_for(channel: OtpChannel, settings: Settings) -> OtpSender:
    registry, name = (
        (SMS_SENDERS, settings.sms_sender_name)
        if channel is OtpChannel.PHONE
        else (EMAIL_SENDERS, settings.email_sender_name)
    )
    sender = registry.get(name)
    if sender is None:
        raise OtpDeliveryError(f"Unknown {channel.value} provider: {name}")
    return sender


def deliver_code(channel: OtpChannel, destination: str, code: str) -> None:
    settings = get_settings()
    sender_for(channel, settings).send(destination, code, settings)
