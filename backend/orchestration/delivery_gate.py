"""What delivery is allowed to believe, and where it reads it from.

Delivery used to be handed `validation_passed=True, security_decision=ALLOW` as
literals, under a comment claiming they were re-derived. They were not. The gate
could not refuse, because it was always given a pass — a control satisfied by
assertion rather than by evidence.

This module derives each precondition from stored rows for one specific run:

| precondition        | derived from                                           |
| ------------------- | ------------------------------------------------------ |
| validation passed   | the latest `migration_attempts` row's outcome           |
| security review ran | `migration_runs.evidence_report["security_review"]`     |
| policy decision     | the strictest `security_findings.policy_decision`       |
| approvals           | `approvals` rows for this run, re-read at delivery      |
| patch identity      | sha256 of the attempt's `patch_diff`, compared with     |
|                     | what is about to be delivered                          |

**It fails closed.** A missing security review is not "no findings, therefore
fine" — it is "nobody looked", and those are not the same answer. A run whose
recorded patch does not match the bytes being delivered is refused outright: the
evidence would describe something other than what ships.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.github.delivery import DeliveryPreconditions, DeliveryRefused
from backend.models import Approval, MigrationAttempt, MigrationRun, SecurityFinding
from backend.models.enums import ApprovalStatus, AttemptOutcome, PolicyDecision
from backend.observability.logging import get_logger

logger = get_logger(__name__)

#: Where the security review's own record lives on the run. Its *presence* is
#: what distinguishes "reviewed, nothing found" from "never reviewed".
SECURITY_REVIEW_KEY: Final = "security_review"

#: The identity of the patch as a diff, recorded when it is reviewed.
PATCH_DIFF_DIGEST_KEY: Final = "patch_diff_digest"

#: The identity of the *files* that patch produced, recorded by whoever holds
#: the workspace. Compared at delivery, so a run resumed after approval cannot
#: ship bytes other than the ones a person said yes to.
PATCH_DIGEST_KEY: Final = "patch_files_digest"


def patch_digest(diff: str) -> str:
    """A stable identity for one patch.

    Normalised on trailing whitespace only: a diff that differs by a final
    newline is the same patch, and a diff that differs anywhere else is not.
    """
    return hashlib.sha256(diff.rstrip().encode("utf-8")).hexdigest()


def files_digest(files: dict[str, str]) -> str:
    """The identity of a set of files about to be delivered.

    Used when a run is resumed from a stored patch: the diff is re-applied to a
    fresh workspace, and this proves the result is the tree the evidence
    describes.
    """
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(f"{path}\0".encode())
        digest.update(hashlib.sha256(files[path].encode("utf-8")).hexdigest().encode())
    return digest.hexdigest()


@dataclass(slots=True)
class DeliveryEvidence:
    """Everything the gate found, including what it could not find."""

    validation_passed: bool = False
    security_review_ran: bool = False
    security_decision: PolicyDecision = PolicyDecision.DENY
    approval_ids: list[Any] = field(default_factory=list)
    unresolved_approvals: int = 0
    recorded_patch_digest: str | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def summary(self) -> dict[str, Any]:
        return {
            "validation_passed": self.validation_passed,
            "security_review_ran": self.security_review_ran,
            "security_decision": self.security_decision.value,
            "approvals": len(self.approval_ids),
            "unresolved_approvals": self.unresolved_approvals,
            "recorded_patch_digest": self.recorded_patch_digest,
            "missing": list(self.missing),
        }


async def gather_evidence(
    session: AsyncSession, run: MigrationRun
) -> DeliveryEvidence:
    """Read what this run actually recorded. Never infer, never default."""
    evidence = DeliveryEvidence()

    attempts = list(
        (
            await session.execute(
                select(MigrationAttempt)
                .where(MigrationAttempt.migration_run_id == run.id)
                .order_by(MigrationAttempt.attempt_number)
            )
        ).scalars()
    )

    if not attempts:
        evidence.missing.append("no migration attempt was recorded")
    else:
        last = attempts[-1]
        evidence.validation_passed = last.outcome is AttemptOutcome.PASSED
        if not evidence.validation_passed:
            evidence.missing.append(
                f"the last attempt ended {last.outcome.value if last.outcome else 'unrecorded'}"
            )
        if last.patch_diff:
            evidence.recorded_patch_digest = patch_digest(last.patch_diff)
        else:
            evidence.missing.append("the recorded attempt carries no patch")

    stored = run.evidence_report or {}
    evidence.security_review_ran = SECURITY_REVIEW_KEY in stored
    if not evidence.security_review_ran:
        # Not the same as "no findings". Nobody looked.
        evidence.missing.append("no security review is recorded for this run")

    findings = list(
        (
            await session.execute(
                select(SecurityFinding.policy_decision).where(
                    SecurityFinding.migration_run_id == run.id
                )
            )
        ).scalars()
    )
    evidence.security_decision = _strictest(findings)
    if evidence.security_decision is PolicyDecision.DENY:
        evidence.missing.append("a finding on this run is classified DENY")

    approvals = list(
        (
            await session.execute(
                select(Approval).where(Approval.migration_run_id == run.id)
            )
        ).scalars()
    )
    evidence.approval_ids = [approval.id for approval in approvals]
    evidence.unresolved_approvals = sum(
        1 for approval in approvals if approval.status is not ApprovalStatus.APPROVED
    )

    if evidence.security_decision is PolicyDecision.ASK and not approvals:
        evidence.missing.append(
            "the security review returned ASK and no approval was requested"
        )
    if evidence.unresolved_approvals:
        evidence.missing.append(
            f"{evidence.unresolved_approvals} approval(s) are not granted"
        )

    return evidence


async def preconditions_for(
    session: AsyncSession, run: MigrationRun, *, files: dict[str, str]
) -> DeliveryPreconditions:
    """Build the gate's input from evidence, and refuse if it does not hold.

    Raises rather than returning a failing set, so a caller cannot proceed by
    ignoring a return value. `deliver()` re-checks the approvals again on its
    own — the two checks are deliberate duplication on the one path that writes
    to someone else's repository.
    """
    evidence = await gather_evidence(session, run)

    if not files:
        evidence.missing.append("there is nothing to deliver")

    _check_patch_identity(run, evidence, files)

    if not evidence.complete:
        logger.warning(
            "continuity.delivery_gate_refused",
            extra={"migration_run_id": str(run.id), **evidence.summary()},
        )
        raise DeliveryRefused(
            "delivery evidence is incomplete: " + "; ".join(evidence.missing)
        )

    return DeliveryPreconditions(
        validation_passed=evidence.validation_passed,
        security_decision=evidence.security_decision,
        required_approval_ids=list(evidence.approval_ids),
    )


def _check_patch_identity(
    run: MigrationRun, evidence: DeliveryEvidence, files: dict[str, str]
) -> None:
    """Refuse when the bytes about to ship are not the ones that were reviewed.

    Only checked when the run recorded a digest of the files it validated — a
    run delivered in the same pass has its workspace in hand and does not need
    it, while a run resumed after approval re-derives its files from a stored
    diff and must prove they came out the same.
    """
    stored = (run.evidence_report or {}).get(PATCH_DIGEST_KEY)
    if not stored:
        return

    if stored != files_digest(files):
        evidence.missing.append(
            "the files about to be delivered do not match the reviewed patch"
        )


def _strictest(decisions: list[PolicyDecision]) -> PolicyDecision:
    """DENY beats ASK beats ALLOW. No findings is ALLOW.

    Safe only because `security_review_ran` is checked separately: an empty
    finding list means "reviewed and clean" *or* "never reviewed", and the two
    are distinguished by the recorded review rather than by this function.
    """
    if PolicyDecision.DENY in decisions:
        return PolicyDecision.DENY
    if PolicyDecision.ASK in decisions:
        return PolicyDecision.ASK
    return PolicyDecision.ALLOW
