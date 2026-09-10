"""Repository scan job.

Drives `INITIAL_SCAN_PENDING → INITIAL_SCAN_RUNNING → INITIAL_SCAN_COMPLETE`,
emitting one activity event per step and persisting a summary.

The scan is **read-only, and that is a security guarantee rather than a
behaviour**. `tests/security/test_scan_read_only.py` hashes the source tree
before and after and asserts it is byte-identical, so a future change that
starts writing during analysis fails a security test rather than quietly
altering a user's code.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import ActivityEventKind, Project, RunState
from backend.models.schemas import TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import transition
from backend.repository.indexer import RepositoryIndex, build_index
from backend.repository.source import RepositorySource

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScanResult:
    project_id: uuid.UUID
    index: RepositoryIndex
    summary: dict[str, Any]


async def run_repository_scan(
    session: AsyncSession, project: Project, source: RepositorySource
) -> ScanResult:
    """Index a repository and record what was found.

    On failure the run moves to `RUN_FAILED` rather than being left mid-scan, so
    a partially-indexed project is never mistaken for a complete one.
    """
    await _advance(session, project, RunState.INITIAL_SCAN_RUNNING, "Repository scan started.")
    await events.emit(
        session,
        kind=ActivityEventKind.REPOSITORY_SCAN_STARTED,
        actor="system",
        summary=f"Scanning {source.identifier}.",
        project_id=project.id,
    )

    try:
        index = build_index(source)
    except Exception as exc:
        logger.exception("continuity.repository_scan_failed", extra={"project_id": str(project.id)})
        await _advance(
            session, project, RunState.RUN_FAILED, f"Scan failed: {type(exc).__name__}"
        )
        raise

    summary = index.summary()

    # Integrations are announced per provider candidate, so the activity feed
    # shows what was found rather than only that something was.
    for ecosystem in sorted(index.ecosystems):
        await events.emit(
            session,
            kind=ActivityEventKind.INTEGRATION_DETECTED,
            actor="system",
            summary=f"Detected a {ecosystem} dependency manifest.",
            project_id=project.id,
        )

    project.last_scanned_at = datetime.now(UTC)
    await _advance(
        session,
        project,
        RunState.INITIAL_SCAN_COMPLETE,
        f"Indexed {summary['files_indexed']} files.",
        detail=summary,
    )

    return ScanResult(project_id=project.id, index=index, summary=summary)


async def _advance(
    session: AsyncSession,
    project: Project,
    to_state: RunState,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    await transition(
        session,
        from_state=project.state,
        to_state=to_state,
        evidence=TransitionEvidence(reason=reason, actor="system", detail=detail or {}),
        project_id=project.id,
    )
    project.state = to_state
    await session.flush()


def hash_tree(root: Path) -> str:
    """Recursive content hash of a directory.

    Used by the read-only security test. Covers paths and bytes, so a
    modification, an addition, and a deletion are all detected.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
