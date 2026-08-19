"""Sign-in, sign-out, and the profile's linked sign-in methods."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .database import get_db
from .domain import IdentityProvider
from .models import User, UserIdentity
from .schemas import (
    AuthStatusOut,
    GoogleSignInIn,
    IdentityOut,
    OtpSentOut,
    OtpStartIn,
    OtpVerifyIn,
    ProfileOut,
    SignOutOut,
)
from .security import clear_session_cookie, current_user, optional_user, session_token, set_session_cookie
from .services.auth import SentCode, complete_link, complete_login, send_link_code, send_login_code, sign_in_with_google
from .services.google_identity import GoogleAuthError, GoogleUnavailable, google_sign_in_enabled
from .services.identity import IdentityConflict, IdentityError, detach_identity, identities_of, owned_identity
from .services.otp import OtpChannelUnavailable, OtpError, OtpRateLimited
from .services.otp_delivery import OtpDeliveryError
from .services.sessions import issue_session, revoke_session


router = APIRouter(prefix="/api")


@contextmanager
def _translated_errors():
    """Domain refusals as HTTP answers.

    The services raise in the vocabulary of the rules they enforce; only this
    boundary decides which status code each one deserves. The messages are
    written for the person reading them and are passed through unchanged.
    """
    try:
        yield
    except IdentityConflict as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except IdentityError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except OtpRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error
    except OtpChannelUnavailable as error:
        # Not the caller's mistake and not retryable by them — the same answer
        # an unconfigured Google client gets a few lines above.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except OtpError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except OtpDeliveryError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The code couldn't be sent right now. Try again in a moment.",
        ) from error
    except GoogleUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except GoogleAuthError as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)) from error


def _identity_value(identity: UserIdentity) -> str:
    """What to show for a linked method.

    A Google row is keyed by an opaque subject that means nothing to its owner,
    so it reports the address that account signs in with.
    """
    if identity.provider == IdentityProvider.GOOGLE.value:
        return identity.email or identity.identifier
    if identity.provider == IdentityProvider.EMAIL.value:
        return identity.email or identity.identifier
    return identity.identifier


def _profile(db: Session, user: User) -> ProfileOut:
    return ProfileOut.model_validate({
        "id": user.id,
        "display_name": user.display_name,
        "currency": user.currency,
        "timezone": user.timezone,
        "email": user.email,
        "phone": user.phone,
        "identities": [
            IdentityOut.model_validate({
                "id": identity.id,
                "provider": identity.provider,
                "value": _identity_value(identity),
                "source": identity.source,
                "verified_at": identity.verified_at,
                "last_login_at": identity.last_login_at,
            })
            for identity in identities_of(db, user.id)
        ],
        "google_sign_in_available": google_sign_in_enabled(),
    })


def _sent(code: SentCode) -> OtpSentOut:
    return OtpSentOut.model_validate({
        "challenge_id": code.challenge_id,
        "channel": code.channel,
        "destination_masked": code.destination_masked,
        "expires_in_seconds": code.expires_in_seconds,
        "resend_after_seconds": code.resend_after_seconds,
        "debug_code": code.debug_code,
    })


def _signed_in(db: Session, response: Response, user: User) -> AuthStatusOut:
    token = issue_session(db, user)
    set_session_cookie(response, token)
    return AuthStatusOut.model_validate({
        "authenticated": True,
        "profile": _profile(db, user),
        "google_sign_in_available": google_sign_in_enabled(),
    })


# ── Signing in ───────────────────────────────────────────────────────────────


@router.get("/auth/session", response_model=AuthStatusOut)
def auth_session(db: Session = Depends(get_db), user: User | None = Depends(optional_user)) -> AuthStatusOut:
    """Who the caller is, if anyone. Answers 200 either way so the app can route."""
    return AuthStatusOut.model_validate({
        "authenticated": user is not None,
        "profile": _profile(db, user) if user else None,
        "google_sign_in_available": google_sign_in_enabled(),
    })


@router.post("/auth/otp/start", response_model=OtpSentOut)
def start_sign_in_code(request: OtpStartIn, db: Session = Depends(get_db)) -> OtpSentOut:
    """Send a sign-in code.

    The answer is the same whether or not an account exists at that number or
    address; which one it is only becomes visible after the code is verified.
    """
    with _translated_errors():
        return _sent(send_login_code(db, request.channel, request.value))


@router.post("/auth/otp/verify", response_model=AuthStatusOut)
def verify_sign_in_code(
    request: OtpVerifyIn,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    with _translated_errors():
        user = complete_login(db, request.challenge_id, request.code)
    return _signed_in(db, response, user)


@router.post("/auth/google", response_model=AuthStatusOut)
def sign_in_google(
    request: GoogleSignInIn,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthStatusOut:
    with _translated_errors():
        user = sign_in_with_google(db, request.credential)
    return _signed_in(db, response, user)


@router.post("/auth/signout", response_model=SignOutOut)
def sign_out(
    response: Response,
    db: Session = Depends(get_db),
    token: str | None = Depends(session_token),
) -> SignOutOut:
    """Ends this browser's session only; other signed-in devices are untouched."""
    revoke_session(db, token)
    clear_session_cookie(response)
    return SignOutOut.model_validate({"signed_out": True})


# ── Profile ──────────────────────────────────────────────────────────────────


@router.get("/profile", response_model=ProfileOut)
def profile(db: Session = Depends(get_db), user: User = Depends(current_user)) -> ProfileOut:
    return _profile(db, user)


@router.post("/profile/identities/otp/start", response_model=OtpSentOut)
def start_link_code(
    request: OtpStartIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> OtpSentOut:
    """Send a code to a number or address this account wants to claim.

    Refuses before sending when the identifier belongs to another account, so a
    stranger's phone never receives a code it did not ask for.
    """
    with _translated_errors():
        return _sent(send_link_code(db, user, request.channel, request.value))


@router.post("/profile/identities/otp/verify", response_model=ProfileOut)
def verify_link_code(
    request: OtpVerifyIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProfileOut:
    """Link the verified identifier, replacing this account's previous one."""
    with _translated_errors():
        complete_link(db, user, request.challenge_id, request.code)
    return _profile(db, user)


@router.delete("/profile/identities/{identity_id}", response_model=ProfileOut)
def remove_identity(
    identity_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ProfileOut:
    identity = owned_identity(db, user.id, identity_id)
    if identity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That sign-in method is not on your account.")
    with _translated_errors():
        detach_identity(db, user, identity)
    db.commit()
    return _profile(db, user)
