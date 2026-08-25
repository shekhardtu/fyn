"""Public contracts for resolving people by sign-in identifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


def _camel_case(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class ContactContract(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        alias_generator=_camel_case,
        serialize_by_alias=True,
    )


class ContactSuggestionOut(ContactContract):
    channel: Literal["email", "phone"]
    identifier: str
    display_name: str
    match_kind: Literal["exact", "previously_shared"]
