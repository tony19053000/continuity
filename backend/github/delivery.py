"""C8-03: deliver the migration, the only safe way there is.

A branch and a pull request. Nothing else. There is no force push here, no
history rewrite, no default-branch write, and no merge — and the reason is not
that those are refused but that `GitHubAppClient` has no method for them
(`CLAUDE.md` §3.4).

Delivery is **gated**, and each gate is checked immediately before the write
rather than assumed from earlier in the run:

* validation passed,
* security review reached ALLOW,
* every approval the review required is currently APPROVED.

The last one is re-read from the database at the moment of delivery, because a
person can revoke between granting and the push.

Every value in the pull request body comes from a stored record. Nothing in it
is generated at delivery time, and nothing is a model's account of what
happened — a PR body is the thing a reviewer trusts most, so it is assembled
entirely from rows.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from backend.approvals.service import ApprovalNotGranted, require_granted
from backend.github.client import GitHubAppClient
from backend.models import MigrationRun, PullRequest, Repository, RunState
from backend.models.enums import ActivityEventKind, PolicyDecision
from backend.models.schemas import TransitionEvidence
from backend.observability import events
from backend.observability.logging import get_logger
from backend.orchestration.state_machine import can_transition, transition
from backend.shared.errors import ContinuityError
from backend.shared.redaction import redact

logger = get_logger(__name__)

#: The one branch shape Continuity creates. Anything not matching this is a bug
#: in the caller, not a naming preference.
BRANCH_TEMPLATE: Final = "continuity/migrate-{provider}-{version}"
BRANCH_PATTERN: Final = re.compile(r"^continuity/migrate-[a-z0-9._-]+-[a-z0-9._-]+$")

_SLUG: Final = re.compile(r"[^a-z0-9._-]+")
#: `..` is meaningful to git and is refused in a ref name. Collapsing it here
#: rather than relying on GitHub to reject it keeps the refusal local and
#: testable — provider ids and version strings arrive from external documents.
_DOTS: Final = re.compile(r"\.{2,}")


class DeliveryRefused(ContinuityError):
    """A precondition for delivery was not met.

    Raised before any write. A refusal here means nothing was created — no
    branch, no commit, no pull request.
    """

    code = "delivery_refused"
    status_code = 409
    message = "This migration cannot be delivered yet."


def branch_name(provider_id: str, version: str) -> str:
    """The branch for one migration.

    Slugged rather than trusted: a provider id or version string reaches us from
    an external document, and `refs/heads/../..` is a real thing to be careful
    about.
    """
    def slug(value: str) -> str:
        cleaned = _SLUG.sub("-", value.strip().lower())
        cleaned = _DOTS.sub(".", cleaned)
        return cleaned.strip("-.")

    slug_provider = slug(provider_id)
    slug_version = slug(version)

    if not slug_provider or not slug_version:
        raise DeliveryRefused(
            f"cannot build a branch name from provider {provider_id!r} "
            f"version {version!r}"
        )

    name = BRANCH_TEMPLATE.format(provider=slug_provider, version=slug_version)
    if not BRANCH_PATTERN.match(name):  # pragma: no cover - slugging guarantees it
        raise DeliveryRefused(f"refusing to create a branch named {name!r}")
    return name


@dataclass(frozen=True, slots=True)
class DeliveryPreconditions:
    """What must be true before Continuity writes anything.

    Passed in rather than recomputed here: each value has an owner elsewhere —
    the repair loop, the security review, the approval service — and delivery's
    job is to refuse when they are not all true, not to re-derive them.
    """

    validation_passed: bool
    security_decision: PolicyDecision
    required_approval_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DeliveredPullRequest:
    number: int
    url: str
    branch: str
    title: str
    files_changed: int


async def _observe_environment(
    session: AsyncSession,
    run: MigrationRun,
    repository: Repository,
    *,
    client: GitHubAppClient,
) -> None:
    """Record what the environment does while the old code is still deployed.

    C9-02 needs a before to compare the after against, and this is the last
    moment one can honestly be taken. It is wrapped because a staging host that
    is down is not a reason to refuse a pull request — the run would then be
    stuck holding a validated patch over an unrelated outage. A missing
    observation makes the later comparison weaker, and the Guardian says so
    rather than guessing.
    """
    from backend.workers.post_merge_verify import observe_before_merge

    try:
        await observe_before_merge(session, run, repository, client=client)
    except Exception as exc:
        logger.warning(
            "continuity.pre_merge_observation_failed",
            extra={"migration_run_id": str(run.id), "error": type(exc).__name__},
        )


async def check_preconditions(
    session: AsyncSession,
    run: MigrationRun,
    preconditions: DeliveryPreconditions,
) -> None:
    """Refuse unless everything is true. Raises; never returns a boolean.

    A boolean return would let a caller forget to look at it.
    """
    if not preconditions.validation_passed:
        raise DeliveryRefused(
            "validation has not passed; Continuity does not open a pull request "
            "for a patch whose tests it has not seen succeed."
        )

    if preconditions.security_decision is PolicyDecision.DENY:
        # DENY is never negotiable. No approval clears it, because the actions
        # policy classifies DENY are the ones nobody is allowed to authorise.
        raise DeliveryRefused(
            "the security review returned deny; delivery is refused."
        )

    if preconditions.security_decision is PolicyDecision.ASK:
        # ASK means *ask*, not refuse. An unanswered question blocks delivery;
        # an answered one is the whole point of the approval flow, and each
        # approval is re-read below at the moment it is relied on.
        if not preconditions.required_approval_ids:
            raise DeliveryRefused(
                "the security review returned ask and no approval was requested."
            )

    for approval_id in preconditions.required_approval_ids:
        try:
            # Re-read now, not earlier. An approval granted at the start of the
            # run can be revoked before this line runs.
            await require_granted(session, approval_id, action="create_pull_request")
        except ApprovalNotGranted as refusal:
            raise DeliveryRefused(str(refusal)) from refusal


async def deliver(
    session: AsyncSession,
    run: MigrationRun,
    repository: Repository,
    *,
    client: GitHubAppClient,
    preconditions: DeliveryPreconditions,
    default_branch: str,
    files: dict[str, str],
    title: str,
    body: str,
    commit_message: str,
) -> DeliveredPullRequest:
    """Create the branch, commit the patch, and open the pull request."""
    await check_preconditions(session, run, preconditions)

    branch = branch_name(run.provider_id, run.to_version)

    if branch == default_branch or not default_branch:
        # Belt and braces: `branch_name` cannot produce a bare default branch
        # name, but a repository whose default branch really is
        # `continuity/migrate-...` would be a very unpleasant surprise.
        raise DeliveryRefused(
            f"refusing to target the default branch {default_branch!r}"
        )

    if not files:
        raise DeliveryRefused("there is nothing to deliver: the patch is empty.")

    # Enter PR_CREATING *before* the first write. If the process dies between
    # creating the branch and opening the pull request, the run is visibly
    # mid-delivery rather than sitting in PR_PENDING as though nothing had
    # happened — and something did: there is a branch on GitHub.
    await _begin(session, run)

    owner, name = repository.owner, repository.name
    base_sha = await client.get_ref(owner, name, f"heads/{default_branch}")

    await client.create_branch(owner, name, branch=branch, from_sha=base_sha)

    entries: list[dict[str, Any]] = []
    for path, content in sorted(files.items()):
        blob = await client.create_blob(owner, name, content=content)
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})

    tree = await client.create_tree(owner, name, base_tree=base_sha, entries=entries)
    commit = await client.create_commit(
        owner, name, message=redact(commit_message), tree=tree, parents=[base_sha]
    )
    await client.update_branch(owner, name, branch=branch, sha=commit)

    created = await client.create_pull_request(
        owner,
        name,
        title=redact(title),
        # Filtered again here even though the report is already filtered: this
        # is the last point before the text becomes public.
        body=redact(body),
        head=branch,
        base=default_branch,
    )

    delivered = DeliveredPullRequest(
        number=int(created["number"]),
        url=str(created["html_url"]),
        branch=branch,
        title=redact(title),
        files_changed=len(files),
    )

    session.add(
        PullRequest(
            migration_run_id=run.id,
            repository_id=repository.id,
            number=delivered.number,
            url=delivered.url,
            branch=branch,
            title=delivered.title,
            state="open",
            merged=False,
            files_changed=delivered.files_changed,
        )
    )
    await session.flush()

    await _record(session, run, delivered)
    await _observe_environment(session, run, repository, client=client)

    logger.info(
        "continuity.pull_request_opened",
        extra={
            "migration_run_id": str(run.id),
            "number": delivered.number,
            "branch": branch,
            "files_changed": delivered.files_changed,
        },
    )
    return delivered


async def _begin(session: AsyncSession, run: MigrationRun) -> None:
    """Move PR_PENDING -> PR_CREATING, before anything is written."""
    await _move(session, run, RunState.PR_CREATING, "opening a pull request")


async def _record(
    session: AsyncSession, run: MigrationRun, delivered: DeliveredPullRequest
) -> None:
    tracked = await session.get(MigrationRun, run.id) or run
    tracked.target_branch = delivered.branch
    await session.flush()

    reason = f"pull request #{delivered.number} opened"
    detail = {"number": delivered.number, "branch": delivered.branch}

    # PR_CREATED, then MERGE_WAITING. Two steps because they mean different
    # things: the pull request exists, and then nothing more will happen until a
    # person merges it. `ALLOWED_TRANSITIONS` has no edge that skips the first.
    await _move(session, run, RunState.PR_CREATED, reason, detail)
    await _move(session, run, RunState.MERGE_WAITING, reason, detail)

    await events.emit(
        session,
        kind=ActivityEventKind.PULL_REQUEST_CREATED,
        actor="delivery",
        summary=f"Opened pull request #{delivered.number}: {delivered.title}",
        project_id=run.project_id,
        migration_run_id=run.id,
    )


async def _move(
    session: AsyncSession,
    run: MigrationRun,
    to_state: RunState,
    reason: str,
    detail: dict[str, object] | None = None,
) -> None:
    tracked = await session.get(MigrationRun, run.id) or run
    if not can_transition(tracked.state, to_state):
        logger.info(
            "continuity.delivery_transition_skipped",
            extra={
                "migration_run_id": str(tracked.id),
                "from_state": tracked.state.value,
                "to_state": to_state.value,
            },
        )
        return

    await transition(
        session,
        from_state=tracked.state,
        to_state=to_state,
        evidence=TransitionEvidence(
            reason=reason, actor="delivery", detail=detail or {}
        ),
        project_id=tracked.project_id,
        migration_run_id=tracked.id,
    )
    tracked.state = to_state
    await session.flush()
