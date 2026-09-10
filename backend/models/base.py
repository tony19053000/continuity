"""SQLAlchemy declarative base and shared column conventions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, String, Uuid, func
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from backend.models.enums import (
    ActivityEventKind,
    AgentRole,
    ApprovalStatus,
    AttemptOutcome,
    ChangeType,
    Confidence,
    EdgeKind,
    FindingCategory,
    JobKind,
    JobStatus,
    NodeKind,
    PolicyDecision,
    RunState,
    Severity,
)

type JsonDict = dict[str, Any]


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrEnumType[E: object](TypeDecorator[E]):
    """Stores a `StrEnum` as a plain string and returns it as the enum.

    A native database ENUM would need a migration every time a member is added —
    and `RunState` has 45 members that will grow. Storing the value as text keeps
    schema changes free while `process_result_value` preserves type safety in
    Python: a column annotated `Mapped[RunState]` really does load a `RunState`,
    so `state is RunState.VERIFIED` behaves as written.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[E], length: int = 64) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(self.enum_class(value))  # type: ignore[call-arg]

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        if value is None:
            return None
        return self.enum_class(value)  # type: ignore[call-arg]


class Base(DeclarativeBase):
    """Declarative base.

    `Uuid`, `JSON`, and `StrEnumType` are all dialect-portable, so the same
    models run on SQLite in development and PostgreSQL in production without a
    second mapping.
    """

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        JsonDict: JSON,
        ActivityEventKind: StrEnumType(ActivityEventKind),
        AgentRole: StrEnumType(AgentRole),
        ApprovalStatus: StrEnumType(ApprovalStatus, 16),
        AttemptOutcome: StrEnumType(AttemptOutcome, 32),
        ChangeType: StrEnumType(ChangeType),
        Confidence: StrEnumType(Confidence, 16),
        EdgeKind: StrEnumType(EdgeKind, 32),
        FindingCategory: StrEnumType(FindingCategory),
        JobKind: StrEnumType(JobKind),
        JobStatus: StrEnumType(JobStatus, 32),
        NodeKind: StrEnumType(NodeKind, 32),
        PolicyDecision: StrEnumType(PolicyDecision, 16),
        RunState: StrEnumType(RunState),
        Severity: StrEnumType(Severity, 16),
    }


class UUIDPrimaryKey:
    """Client-generated UUID primary key.

    Generated in Python rather than by the database so that a caller can
    reference a row's id before the transaction commits — needed when building
    a graph of nodes and edges in one unit of work.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, sort_order=-100
    )


class Timestamps:
    """Creation and update timestamps, maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
        sort_order=101,
    )
