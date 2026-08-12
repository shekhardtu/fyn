from __future__ import annotations

from typing import Annotated

from pydantic import Field


SEMANTIC_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
DATA_FIELD_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
DATA_RESOURCE_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"


# Shared wire-format primitives. These aliases keep validation policy attached
# to the value wherever it crosses an agent, semantic, or widget boundary.
SemanticIdentifier = Annotated[str, Field(pattern=SEMANTIC_IDENTIFIER_PATTERN)]
DataFieldKey = Annotated[str, Field(pattern=DATA_FIELD_KEY_PATTERN)]
DataResourceId = Annotated[str, Field(pattern=DATA_RESOURCE_ID_PATTERN)]
