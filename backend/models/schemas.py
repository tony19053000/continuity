"""Pydantic domain schemas.

These are the validated shapes that cross boundaries: agent output contracts,
API bodies, and structured columns stored as JSON. SQLAlchemy models hold the
rows; these define what may go into them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import (
    ChangeType,
    Confidence,
    EvidenceKind,
    SourceKind,
)


class SourceRef(BaseModel):
    """Where a piece of external information came from.

    Every provider document carries one. It is the single place external
    provenance lives, so a claim about a provider can always be traced back to
    a fetched document and the moment it was fetched.
    """

    model_config = ConfigDict(frozen=True)

    kind: SourceKind
    url: str | None = None
    document_hash: str | None = Field(
        default=None, description="Content hash of the stored document"
    )
    retrieved_at: datetime | None = None


class Evidence(BaseModel):
    """What supports a claim.

    Repository evidence sets `file_path` and a line span. External evidence sets
    `source_ref`. `confidence` records whether the claim was derived
    deterministically (`CONFIRMED`) or proposed by a model (`INFERRED`); the
    distinction is preserved end to end and surfaced in the UI.
    """

    model_config = ConfigDict(frozen=True)

    kind: EvidenceKind
    confidence: Confidence
    file_path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    source_ref: SourceRef | None = None
    excerpt: str | None = Field(
        default=None,
        max_length=4000,
        description="Bounded and secret-filtered before storage",
    )


class ProviderChange(BaseModel):
    """A single normalized provider change (`02_ARCHITECTURE.md` §10).

    `source` is required: a change Continuity cannot attribute to a document is
    a change Continuity does not report.
    """

    provider_id: str
    old_version: str
    new_version: str
    change_type: ChangeType
    resource: str = Field(description="Endpoint path, event name, or scope")
    old_contract: dict[str, object] | None = None
    new_contract: dict[str, object] | None = None
    breaking: bool
    security_relevant: bool
    authentication_relevant: bool
    source: SourceRef
    evidence: Evidence


class TransitionEvidence(BaseModel):
    """Why a run moved between states.

    Persisted with every transition, so the run timeline in the UI is a direct
    read of recorded history rather than a reconstruction.
    """

    reason: str
    actor: str = Field(description="Agent role, 'system', or a user id")
    detail: dict[str, object] = Field(default_factory=dict)
