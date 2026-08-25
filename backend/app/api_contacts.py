"""Authenticated, privacy-bounded lookup for reusable person pickers."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .contact_schemas import ContactSuggestionOut
from .database import get_db
from .models import User
from .security import current_user
from .services.contacts import search_contacts
from .services.identity import IdentityError


router = APIRouter(tags=["contacts"])


@router.get("/contacts", response_model=list[ContactSuggestionOut])
def contacts(
    channel: Literal["email", "phone"],
    q: str = Query(min_length=3, max_length=320),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ContactSuggestionOut]:
    try:
        suggestions = search_contacts(db, user_id=user.id, channel=channel, query=q)
    except IdentityError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    return [ContactSuggestionOut.model_validate(item.__dict__) for item in suggestions]
