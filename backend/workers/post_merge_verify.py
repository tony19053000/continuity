"""C9-02: running a project's own checks against its own environment.

`backend/workers/post_merge.py` answers "is this merge the one this run
produced?" from records Continuity already holds. This module answers a question
records cannot: "does the application still work?" — and it can only answer it
because the project declared what working means, in
`.continuity/verification.json` (see `backend/verification/environment.py`).

Two observations, deliberately:

* **before the merge**, taken when the pull request is opened, while the old
  code is still what the environment runs;
* **after the merge**, taken when post-merge verification runs.

The comparison is what makes the result mean anything. A check failing in both
is the project's existing problem; a check that passed before and fails now is
this migration's regression. `backend/agents/release_guardian.py` does the
comparison and explains it.

**Three honest outcomes, and none of them is silent:**

| situation | what is recorded | what happens |
| --- | --- | --- |
| no manifest | `verification: not_configured` | the run reaches `VERIFIED`, claiming nothing about a deployment |
| manifest present, checks pass | `verification: passed` | the run reaches `VERIFIED` |
| manifest present, something regressed | `verification: failed` | `POST_MERGE_VERIFICATION_FAILED` → `HUMAN_REVIEW_REQUIRED` |

**Nothing here rolls anything back.** The Guardian may recommend one; a person
performs it. `tests/security/test_release_guardian_surface.py` asserts over this
module's source that no revert, reset, force-push, or redeploy path exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.release_guardian import ReleaseAssessment, assess_release
from backend.models import MigrationRun, Repository
from backend.observability.logging import get_logger
from backend.shared.config import Settings, get_settings
from backend.verification.environment import (
    MANIFEST_PATH,
    CheckOutcome,
    ManifestInvalid,
    Observation,
    VerificationEnvironment,
    parse_manifest,
    run_checks,
)

logger = get_logger(__name__)

#: Where the pre-merge observation is kept, so the post-merge one has something
#: to be compared against.
PRE_MERGE_OBSERVATION_KEY: Final = "pre_merge_observation"

#: Where the Guardian's verdict is kept.
RELEASE_VERIFICATION_KEY: Final = "release_verification"

NOT_CONFIGURED: Final = "not_configured"
MANIFEST_INVALID: Final = "manifest_invalid"
DISABLED: Final = "disabled"
PASSED: Final = "passed"
FAILED: Final = "failed"


@dataclass(slots=True)
class EnvironmentVerification:
    """What the environment said, or why nothing was asked."""

    verification: str = NOT_CONFIGURED
    detail: str = ""
    assessment: ReleaseAssessment | None = None
    checks_run: int = 0

    @property
    def configured(self) -> bool:
        return self.verification in {PASSED, FAILED}

    @property
    def blocks(self) -> bool:
        """Whether this must stop the run reaching `VERIFIED`.

        Only a configured environment that actually failed. An absent manifest,
        a broken one, or a disabled feature all mean nothing was checked — and
        nothing checked must not read as something failed, any more than it
        reads as something passed.
        """
        return self.verification == FAILED

    def summary(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "verification": self.verification,
            "detail": self.detail,
            "checks_run": self.checks_run,
        }
        if self.assessment is not None:
            report["assessment"] = self.assessment.report()
        return report


async def observe_before_merge(
    session: AsyncSession,
    run: MigrationRun,
    repository: Repository,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
    transport: Any | None = None,
) -> Observation | None:
    """Record what the environment does *before* the merge lands.

    Called when the pull request is opened, which is the last moment the old
    code is definitely what is deployed. Failing here never fails delivery: a
    missing baseline makes the later comparison weaker and says so, while a
    refused pull request over an unreachable staging host would be absurd.
    """
    settings = settings or get_settings()
    if not settings.RELEASE_VERIFICATION_ENABLED:
        # The same opt-in gates both observations. Without this, delivery would
        # make outbound requests to an address written in a repository while the
        # feature that is supposed to authorise them is switched off.
        return None

    environment = await load_environment(repository, client=client, settings=settings)
    if environment is None:
        return None

    observation = await run_checks(
        environment,
        transport=transport,
        allow_private_hosts=settings.RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS,
    )
    await _store(session, run, PRE_MERGE_OBSERVATION_KEY, observation.summary())
    logger.info(
        "continuity.pre_merge_observation",
        extra={
            "migration_run_id": str(run.id),
            "reached": observation.reached,
            "failures": len(observation.failures),
        },
    )
    return observation


async def verify_environment(
    session: AsyncSession,
    run: MigrationRun,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
    model_provider: Any | None = None,
    transport: Any | None = None,
) -> EnvironmentVerification:
    """Run the project's checks against its environment and judge the result."""
    settings = settings or get_settings()

    if not settings.RELEASE_VERIFICATION_ENABLED:
        return EnvironmentVerification(
            verification=DISABLED,
            detail=(
                "RELEASE_VERIFICATION_ENABLED is off, so no request was made to "
                "any environment."
            ),
        )

    repository = await _repository_for(session, run)
    if repository is None:  # pragma: no cover - foreign key
        return EnvironmentVerification(detail="the run has no repository")

    try:
        environment = await load_environment(
            repository, client=client, settings=settings, strict=True
        )
    except ManifestInvalid as exc:
        # Recorded, not fatal. A typo in a manifest must not hold every future
        # merge hostage, and it must not be reported as a passing check either.
        logger.warning(
            "continuity.verification_manifest_invalid",
            extra={"migration_run_id": str(run.id), "reason": str(exc)},
        )
        result = EnvironmentVerification(
            verification=MANIFEST_INVALID, detail=str(exc)
        )
        await _store(session, run, RELEASE_VERIFICATION_KEY, result.summary())
        return result

    if environment is None:
        result = EnvironmentVerification(
            verification=NOT_CONFIGURED,
            detail=f"no {MANIFEST_PATH} in this repository",
        )
        await _store(session, run, RELEASE_VERIFICATION_KEY, result.summary())
        return result

    after = await run_checks(
        environment,
        transport=transport,
        allow_private_hosts=settings.RELEASE_VERIFICATION_ALLOW_PRIVATE_HOSTS,
    )
    before = _stored_observation(run)

    assessment = await assess_release(
        provider_id=run.provider_id,
        from_version=run.from_version,
        to_version=run.to_version,
        before=before,
        after=after,
        changed_files=_changed_files(run),
        model_provider=model_provider,
    )

    result = EnvironmentVerification(
        verification=PASSED if assessment.passed else FAILED,
        detail=assessment.summary,
        assessment=assessment,
        checks_run=len(after.outcomes),
    )
    await _store(session, run, RELEASE_VERIFICATION_KEY, result.summary())
    return result


async def load_environment(
    repository: Repository,
    *,
    client: Any | None,
    settings: Settings,
    strict: bool = False,
) -> VerificationEnvironment | None:
    """Read the manifest from the repository, or return None if there is none.

    Two sources, because Continuity supports two kinds of repository: a local
    checkout in development reads from disk, and a GitHub repository reads the
    default branch through the App's existing read permission. Neither one adds
    a new grant.

    `strict` re-raises `ManifestInvalid` so the caller can record *why* a
    manifest that exists was not used. Off by default, because the pre-merge
    observation must never fail a delivery.
    """
    text: str | None = None

    if repository.local_path:
        candidate = Path(repository.local_path) / MANIFEST_PATH
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
    elif client is not None:
        try:
            text = await client.read_file(
                repository.owner,
                repository.name,
                MANIFEST_PATH,
                repository.default_branch,
            )
        except Exception:
            # Absent is the overwhelmingly common case and is not an error.
            text = None

    if text is None:
        return None

    try:
        return parse_manifest(text)
    except ManifestInvalid:
        if strict:
            raise
        logger.warning(
            "continuity.verification_manifest_unusable",
            extra={"repository": f"{repository.owner}/{repository.name}"},
        )
        return None


def _stored_observation(run: MigrationRun) -> Observation | None:
    """Rebuild the pre-merge observation from what was stored."""
    stored = (run.evidence_report or {}).get(PRE_MERGE_OBSERVATION_KEY)
    if not isinstance(stored, dict):
        return None

    observation = Observation(
        reached=bool(stored.get("reached")),
        note=str(stored.get("note", "")),
    )
    for item in stored.get("checks", []):
        if not isinstance(item, dict):
            continue
        observation.outcomes.append(
            CheckOutcome(
                name=str(item.get("name", "")),
                ok=bool(item.get("ok")),
                status=item.get("status"),
                reason=str(item.get("reason", "")),
                excerpt=str(item.get("excerpt", "")),
            )
        )
    return observation


def _changed_files(run: MigrationRun) -> list[str]:
    stored = (run.evidence_report or {}).get("security_review")
    if isinstance(stored, dict):
        findings = stored.get("findings")
        if isinstance(findings, list):
            return sorted(
                {
                    str(item.get("file_path"))
                    for item in findings
                    if isinstance(item, dict) and item.get("file_path")
                }
            )
    return []


async def _repository_for(
    session: AsyncSession, run: MigrationRun
) -> Repository | None:
    from backend.models import Project

    project = await session.get(Project, run.project_id)
    if project is None:  # pragma: no cover - foreign key
        return None
    return await session.get(Repository, project.repository_id)


async def _store(
    session: AsyncSession, run: MigrationRun, key: str, value: dict[str, Any]
) -> None:
    tracked = await session.get(MigrationRun, run.id) or run
    stored = dict(tracked.evidence_report or {})
    stored[key] = value
    tracked.evidence_report = stored
    await session.flush()
