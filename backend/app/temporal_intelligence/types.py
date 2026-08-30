"""Stage D — Temporal historical intelligence types (dataclasses only)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RecapWindow(str, Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    CUSTOM = "custom"
    ENTIRE_PROJECT = "entire_project"


@dataclass(frozen=True)
class TimeRange:
    start: datetime | None  # None = unbounded past
    end: datetime | None  # None = unbounded future / now
    window: RecapWindow
    label: str


@dataclass
class RecapEvidenceItem:
    """One durable evidence row. Never invented from model context."""

    kind: str
    id: uuid.UUID | str
    occurred_at: datetime
    title: str
    status: str | None = None
    owner_id: uuid.UUID | None = None
    source_table: str = ""
    refs: dict = field(default_factory=dict)
    summary: str | None = None


@dataclass
class RecapReport:
    range: TimeRange
    owner_id: uuid.UUID
    items: list[RecapEvidenceItem] = field(default_factory=list)
    counts_by_kind: dict[str, int] = field(default_factory=dict)
    repeated_titles: list[dict] = field(default_factory=list)
    evidence_only: bool = True
