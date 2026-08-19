"""The identity boundary every request passes through.

`current_user` is the single place the application learns who is calling. Before
authentication it answered with a seeded local account; it now answers with the
account behind the session cookie, or refuses. Every route that already depended
on it became protected by that change alone.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from .config import SESSION_COOKIE_NAME, get_settings
from .database import get_db
from .models import User
from .services.sessions import resolve_session, session_lifetime


def session_token(request: Request) -> str | None:
    """This request's session cookie.

    The browser is handed an `httponly` cookie it cannot read, which is what
    keeps script on the page from lifting the session and what lets the
    mutating routes skip a separate CSRF token.
    """
    return request.cookies.get(SESSION_COOKIE_NAME)


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in account, or None. For routes that answer either way."""
    return resolve_session(db, session_token(request))


def current_user(user: User | None = Depends(optional_user)) -> User:
    """The signed-in account, or a refusal."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue.",
        )
    return user


def set_session_cookie(response: Response, token: str) -> None:
    """Hand the browser a session it cannot read.

    `httponly` keeps script on the page from lifting it, and `samesite` keeps
    another site from riding it: cookies are withheld from cross-site writes, so
    the mutating routes need no separate CSRF token. The app and the API are
    expected to sit on sibling subdomains of one host, which is same-site.
    """
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(session_lifetime().total_seconds()),
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.session_cookie_samesite,
    )


def clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        domain=settings.session_cookie_domain,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite=settings.session_cookie_samesite,
    )
