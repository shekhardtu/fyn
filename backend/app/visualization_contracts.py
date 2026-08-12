from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .validation import DataResourceId, SemanticIdentifier


VisualMark = Literal["bar", "line", "area", "point", "rect", "arc", "tick"]
RequestedVisualMark = Union[Literal["auto"], VisualMark]
VisualFieldType = Literal["quantitative", "nominal", "ordinal", "temporal"]
VisualValueType = Literal["string", "number", "date", "datetime", "money_minor", "percentage", "category"]
VisualFieldRole = Literal["dimension", "measure"]
VisualSort = Literal["ascending", "descending"]
ORDERED_SERIES_MARKS = frozenset({"line", "area"})
ORDERED_VISUAL_FIELD_TYPES = frozenset({"temporal", "ordinal"})


class VisualFieldEncoding(BaseModel):
    """One governed semantic field bound to one renderer channel."""

    field: SemanticIdentifier
    type: VisualFieldType
    title: str | None = Field(default=None, max_length=100)
    value_type: VisualValueType = Field(default="number", alias="valueType")
    sort: VisualSort | None = None
    model_config = ConfigDict(populate_by_name=True)


class VisualEncodingContract(BaseModel):
    x: VisualFieldEncoding | None = None
    y: VisualFieldEncoding | None = None
    color: VisualFieldEncoding | None = None
    size: VisualFieldEncoding | None = None
    theta: VisualFieldEncoding | None = None
    row: VisualFieldEncoding | None = None
    column: VisualFieldEncoding | None = None
    tooltip: list[VisualFieldEncoding] = Field(default_factory=list, max_length=8)


class VisualizationView(BaseModel):
    id: DataResourceId
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=300)
    dataset: DataResourceId
    mark: VisualMark
    encoding: VisualEncodingContract
    height: int = Field(default=320, ge=180, le=720)


class VisualizationLayout(BaseModel):
    columns: int = Field(default=1, ge=1, le=3)
